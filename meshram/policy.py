"""Owner-controlled contribution policy. Pure functions: easy to test, no side effects."""
GiB = 2**30

DEFAULTS = {
    "max_ram_gb": 2.0,          # hard cap on what the cluster may use on this machine
    "max_cpu_pct": 75,          # -> number of compute threads given to the runtime
    "allow_gpu": False,
    "only_when_idle": False,
    "idle_minutes": 5,
    "only_when_charging": False,
    "max_temp_c": 90,
    "pause_when_busy": True,    # owner starts heavy work -> release cluster resources
    "busy_cpu_pct": 85,
    "tunnel_port": 50052,
}


def validate(p):
    """Return a clean policy dict or raise ValueError (input arrives over HTTP)."""
    out = dict(DEFAULTS)
    for k, v in (p or {}).items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown policy field {k}")
        t = type(DEFAULTS[k])
        if t is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true/false")
        elif t in (int, float):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{k} must be a number")
            v = t(v)
        out[k] = v
    rng = {"max_ram_gb": (0, 1024), "max_cpu_pct": (5, 100), "idle_minutes": (1, 240),
           "max_temp_c": (40, 110), "busy_cpu_pct": (10, 100), "tunnel_port": (1024, 65535)}
    for k, (lo, hi) in rng.items():
        if not lo <= out[k] <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
    return out


def host_reserve(total):
    """RAM that always stays with the owner: max(2 GiB, 25%)."""
    return max(2 * GiB, total // 4)


def min_free(total):
    """Never let system-available RAM drop below this because of us."""
    return max(1 * GiB, total // 10)


def contribution_cap(total, policy):
    return max(0, min(int(policy["max_ram_gb"] * GiB), total - host_reserve(total)))


def budget(snap, policy, allocated):
    """Bytes the cluster may still allocate right now."""
    cap = contribution_cap(snap["ram_total"], policy) - allocated
    live = snap["ram_available"] - min_free(snap["ram_total"])
    return max(0, min(cap, live))


def admit(nbytes, snap, policy, allocated):
    if nbytes <= 0:
        return False, "invalid size"
    b = budget(snap, policy, allocated)
    if nbytes > b:
        return False, (f"refused: {nbytes / GiB:.2f} GiB requested, {b / GiB:.2f} GiB allowed "
                       f"(cap {contribution_cap(snap['ram_total'], policy) / GiB:.2f} GiB, "
                       f"already allocated {allocated / GiB:.2f} GiB, available {snap['ram_available'] / GiB:.2f} GiB)")
    return True, "ok"


def threads(policy, logical_cpus):
    return max(1, int((logical_cpus or 1) * policy["max_cpu_pct"] / 100))


def pause_reason(snap, policy, busy_streak):
    """Why this node must not contribute right now, or None. busy_streak = consecutive busy samples."""
    if policy["only_when_charging"] and snap["charging"] is False:
        return "on battery (owner allows charging-only)"
    if policy["only_when_idle"]:
        if snap["idle_s"] is None:
            return "idle detection unavailable on this OS (only-when-idle is on)"
        if snap["idle_s"] < policy["idle_minutes"] * 60:
            return f"owner active (idle {int(snap['idle_s'])}s < {policy['idle_minutes']} min)"
    if snap["temp_c"] is not None and snap["temp_c"] >= policy["max_temp_c"]:
        return f"temperature {snap['temp_c']}°C >= limit {policy['max_temp_c']}°C"
    if snap["ram_available"] < min_free(snap["ram_total"]) // 2:
        return "host is low on memory"
    if policy["pause_when_busy"] and busy_streak >= 3:
        return f"owner using CPU heavily (> {policy['busy_cpu_pct']}% excluding cluster)"
    return None


def is_busy(snap, policy):
    return snap["cpu_pct"] - snap["cluster_cpu_pct"] > policy["busy_cpu_pct"]
