"""Fast unit tests: policy, scheduler, GGUF parser, OS memory limit, tensor-split math."""
import bisect
import os
import struct
import subprocess
import sys

import pytest

from meshram import gguf, policy, runtime, scheduler

GiB = 2**30
MODEL = os.path.join(runtime.ROOT, "models", "qwen2.5-0.5b-instruct-q4_k_m.gguf")


def snap(total=8 * GiB, avail=6 * GiB, **kw):
    s = {"ram_total": total, "ram_available": avail, "ram_used": total - avail, "cpu_pct": 10, "cluster_cpu_pct": 0,
         "temp_c": None, "charging": None, "idle_s": 0, "cluster_rss": 0}
    s.update(kw)
    return s


# ---------------- policy ----------------
def test_policy_validation_rejects_bad_input():
    with pytest.raises(ValueError):
        policy.validate({"max_ram_gb": "lots"})
    with pytest.raises(ValueError):
        policy.validate({"rm_rf": True})
    with pytest.raises(ValueError):
        policy.validate({"max_cpu_pct": 500})
    with pytest.raises(ValueError):
        policy.validate({"allow_gpu": 1})
    assert policy.validate({"max_ram_gb": 3})["max_ram_gb"] == 3.0


def test_host_always_keeps_reserve():
    p = policy.validate({"max_ram_gb": 100})
    assert policy.contribution_cap(8 * GiB, p) == 6 * GiB          # 8 - max(2, 25%)
    assert policy.contribution_cap(32 * GiB, p) == 24 * GiB        # 32 - 8
    assert policy.contribution_cap(2 * GiB, p) == 0                # tiny machine contributes nothing


def test_admission_respects_cap_allocated_and_live_free_ram():
    p = policy.validate({"max_ram_gb": 2})
    assert policy.admit(1 * GiB, snap(), p, 0)[0]
    assert not policy.admit(3 * GiB, snap(), p, 0)[0]                # over cap
    assert not policy.admit(int(1.5 * GiB), snap(), p, 1 * GiB)[0]   # cap minus already allocated
    ok, why = policy.admit(1 * GiB, snap(avail=int(1.5 * GiB)), p, 0)  # owner is using the RAM right now
    assert not ok and "refused" in why
    assert not policy.admit(0, snap(), p, 0)[0]


def test_pause_reasons():
    p = policy.validate({"only_when_charging": True, "only_when_idle": True, "idle_minutes": 5, "max_temp_c": 80})
    assert "battery" in policy.pause_reason(snap(charging=False), p, 0)
    assert "owner active" in policy.pause_reason(snap(charging=True, idle_s=10), p, 0)
    assert "unavailable" in policy.pause_reason(snap(charging=True, idle_s=None), p, 0)
    assert "temperature" in policy.pause_reason(snap(charging=True, idle_s=900, temp_c=85), p, 0)
    assert policy.pause_reason(snap(charging=True, idle_s=900, temp_c=50), p, 0) is None
    p2 = policy.validate({})
    assert "CPU" in policy.pause_reason(snap(), p2, 3)
    assert "low on memory" in policy.pause_reason(snap(avail=100 * 2**20), p2, 0)
    assert policy.is_busy(snap(cpu_pct=95, cluster_cpu_pct=0), p2)
    assert not policy.is_busy(snap(cpu_pct=95, cluster_cpu_pct=50), p2)  # it's our own load, not the owner's


def test_threads_from_cpu_limit():
    assert policy.threads(policy.validate({"max_cpu_pct": 50}), 8) == 4
    assert policy.threads(policy.validate({"max_cpu_pct": 5}), 8) == 1


# ---------------- scheduler ----------------
def fake_model(n_layer=32, layer_mb=100):
    return {"file": "fake.gguf", "n_layer": n_layer, "layer_bytes": [layer_mb * 2**20] * n_layer,
            "kv_bytes_per_token_layer": 1024, "n_ff": 11008, "n_embd": 4096, "n_head": 32, "n_vocab": 32000,
            "output_bytes": 200 * 2**20, "embd_bytes": 200 * 2**20, "size_bytes": n_layer * layer_mb * 2**20}


def nodes(*gb):
    return [{"id": f"n{i}", "name": f"n{i}", "budget": int(g * GiB)} for i, g in enumerate(gb)]


def llama_cpp_layer_map(ts, n_layer):
    """Re-implementation of llama.cpp's device choice for slot il (llama-model.cpp, layer split)."""
    tot, cum, s = sum(ts), [], 0
    for x in ts:
        s += x
        cum.append(s / tot)
    n = n_layer + 1
    return [bisect.bisect_right(cum, il / n) for il in range(n_layer)]


@pytest.mark.parametrize("counts", [[12, 12], [11, 13], [1, 23], [8, 8, 8], [5, 9, 7, 3], [24]])
def test_tensor_split_lands_exactly(counts):
    m = llama_cpp_layer_map(scheduler.tensor_split(counts), sum(counts))
    expect = [i for i, c in enumerate(counts) for _ in range(c)]
    assert m == expect


def test_plan_fits_single_node_uses_one_node():
    p = scheduler.plan(fake_model(), nodes(8, 8, 8), ctx=2048)
    assert p["ok"] and len(p["assignments"]) == 1 and p["fits_single_node"]


def test_plan_needs_cluster_when_no_single_node_fits():
    m = fake_model()  # ~3.2 GB weights + overhead
    p = scheduler.plan(m, nodes(2, 2, 2), ctx=2048)
    assert p["ok"] and not p["fits_single_node"] and len(p["assignments"]) >= 2
    assert sum(a["end"] - a["start"] for a in p["assignments"]) == m["n_layer"]
    assert all(a["est_bytes"] <= a["budget"] for a in p["assignments"])
    # contiguous, ordered layer ranges
    for a, b in zip(p["assignments"], p["assignments"][1:]):
        assert a["end"] == b["start"]


def test_plan_reports_honest_failure():
    p = scheduler.plan(fake_model(), nodes(1, 1), ctx=2048)
    assert not p["ok"] and "cannot fit" in p["reason"]
    assert scheduler.plan(fake_model(), [], ctx=2048)["reason"].startswith("no online")


def test_spread_uses_all_nodes_proportionally():
    p = scheduler.plan(fake_model(), nodes(4, 4, 8), ctx=2048, strategy="spread")
    assert p["ok"] and len(p["assignments"]) == 3
    by = {a["node_id"]: a["end"] - a["start"] for a in p["assignments"]}
    assert by["n2"] > by["n0"]


def test_head_on_host_shrinks_node_cost():
    m = fake_model()
    a = scheduler.node_cost(m, 0, 32, 2048, 256, True)[0]
    b = scheduler.node_cost(m, 0, 32, 2048, 256, False)[0]
    assert a - b > m["output_bytes"]


# ---------------- gguf ----------------
def _write_gguf(path):
    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [("general.architecture", 8, s("llama")), ("llama.block_count", 4, struct.pack("<I", 2)),
          ("llama.embedding_length", 4, struct.pack("<I", 64)), ("llama.attention.head_count", 4, struct.pack("<I", 4)),
          ("llama.attention.head_count_kv", 4, struct.pack("<I", 2)), ("general.file_type", 4, struct.pack("<I", 15)),
          ("tokenizer.ggml.scores", 9, struct.pack("<IQ", 6, 3) + struct.pack("<3f", 1, 2, 3)),
          ("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 2) + s("a") + s("b"))]
    tensors = [("token_embd.weight", [64, 100], 0), ("blk.0.attn_q.weight", [64, 64], 1024),
               ("blk.1.attn_q.weight", [64, 64], 3072), ("output.weight", [64, 100], 5120)]
    h = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(kv))
    for k, t, v in kv:
        h += s(k) + struct.pack("<I", t) + v
    for name, dims, off in tensors:
        h += s(name) + struct.pack("<I", len(dims)) + b"".join(struct.pack("<Q", d) for d in dims) \
             + struct.pack("<I", 0) + struct.pack("<Q", off)
    h += b"\0" * ((-len(h)) % 32)
    with open(path, "wb") as f:
        f.write(h + b"\0" * 8192)


def test_gguf_parser_synthetic(tmp_path):
    p = tmp_path / "t.gguf"
    _write_gguf(p)
    m = gguf.read(str(p))
    assert m["n_layer"] == 2 and m["quant"] == "Q4_K_M"
    assert m["layer_bytes"] == [2048, 2048]
    assert m["embd_bytes"] == 1024 and m["n_vocab"] == 100
    assert m["kv_bytes_per_token_layer"] == 2 * (16 + 16) * 2


def test_gguf_rejects_non_gguf(tmp_path):
    p = tmp_path / "x.gguf"
    p.write_bytes(b"NOPE" + b"\0" * 100)
    with pytest.raises(ValueError):
        gguf.read(str(p))


@pytest.mark.skipif(not os.path.exists(MODEL), reason="test model not downloaded")
def test_gguf_real_model_matches_llama_cpp():
    m = gguf.read(MODEL)
    assert m["n_layer"] == 24 and m["quant"] == "Q4_K_M"
    # llama.cpp reported 110.84 MiB for layers 0-10 of this file
    assert abs(sum(m["layer_bytes"][:11]) / 2**20 - 110.84) < 0.01


# ---------------- OS memory limit ----------------
def test_os_memory_limit_is_enforced():
    """A child under a 200 MiB limit must fail to commit 600 MiB (real OS enforcement, not a check in Python)."""
    lim = runtime._MemLimit(200 * 2**20)
    code = "import time; time.sleep(0.5); b = bytearray(600 * 2**20); print('allocated')"
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         preexec_fn=lim.preexec())
    lim.attach(p.pid)
    out, err = p.communicate(timeout=30)
    assert p.returncode != 0 and b"allocated" not in out
    assert b"MemoryError" in err or p.returncode != 0
    ok = subprocess.run([sys.executable, "-c", "b = bytearray(50 * 2**20)"], timeout=30)
    assert ok.returncode == 0


def test_cluster_cpu_is_measured_not_mistaken_for_owner_load(tmp_path):
    """Regression: a fresh psutil.Process reports 0% CPU, which made the worker's own inference
    load look like the owner's and paused it mid-inference."""
    import time
    from meshram import hw
    from meshram.worker import Worker
    w = Worker(str(tmp_path))
    w.rpc = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        assert w.procs()[0] is w.procs()[0]  # cached between samples
        hw.snapshot(w.temp, w.procs())
        time.sleep(1.0)
        assert hw.snapshot(w.temp, w.procs())["cluster_cpu_pct"] > 1
    finally:
        w.rpc.kill()


def test_network_scope_allows_lan_and_overlay_vpn_but_not_internet():
    from meshram.coordinator import _lan
    for ip in ("192.168.1.5", "10.0.0.2", "127.0.0.1", "100.69.248.116", "100.127.255.1", "fd7a:115c:a1e0::1"):
        assert _lan(ip), ip  # LAN, loopback, Tailscale/Headscale (100.64/10, fd7a::/48)
    for ip in ("8.8.8.8", "100.128.0.1", "1.1.1.1", "not-an-ip"):
        assert not _lan(ip), ip
