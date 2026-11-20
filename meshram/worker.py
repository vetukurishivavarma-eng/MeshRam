"""Worker agent. Runs on every contributing computer (including the coordinator's own, if it contributes).

It only ever executes the commands in COMMANDS, with validated arguments, inside the owner's policy.
There is no remote shell, no arbitrary file write, no arbitrary process launch.
"""
import asyncio
import json
import logging
import os
import secrets
import socket
import time
import uuid

import httpx
import psutil
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import discovery, hw, policy, runtime, store, tls
from .tunnel import TunnelServer

log = logging.getLogger("meshram.worker")


class Worker:
    def __init__(self, data_dir, name=None, coordinator=None, port=7861, pair_code=None):
        data_dir = os.path.abspath(data_dir)
        self.dir = data_dir
        self.st = store.Store(os.path.join(data_dir, "state.json"))
        s = self.st.data
        s.setdefault("node_id", str(uuid.uuid4()))
        s.setdefault("name", name or socket.gethostname())
        if name:
            s["name"] = name
        s["policy"] = policy.validate(s.get("policy"))
        self.st.save()
        self.crt, self.key = tls.ensure_identity(data_dir)
        self.cert_pem = open(self.crt).read()
        self.fp = tls.fingerprint_pem(self.cert_pem)
        self.target = coordinator  # "host" or "host:port" from CLI; None = discover
        self.port = port
        self.pair_code = pair_code
        self.static = hw.static_info()
        self.temp = hw.TempReader()
        self.rpc = None            # Popen of rpc-server
        self.rpc_limit = 0         # bytes admitted for it (OS-enforced)
        self.tunnel = None
        self.alloc_test_bytes = 0
        self.task = None           # human-readable current task
        self.paused = None
        self.busy_streak = 0
        self.connected = False
        self.coord_ip = None
        self.ws = None
        self.events = []
        self.last_snap = None
        self.disconnected_at = None
        self.net_rx = None
        self._ps = {}
        self.extra_pids = lambda: ()  # coordinator machine: its llama-server is cluster load too
        self.cache_dir = os.path.join(data_dir, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---------- local status (used by the owner's local page) ----------
    @property
    def policy(self):
        return self.st.data["policy"]

    def allocated(self):
        return self.rpc_limit + self.alloc_test_bytes

    def procs(self):
        """Cluster processes on this machine. psutil.Process objects are cached: cpu_percent() needs the
        previous sample, a fresh object always reports 0% (which made our own load look like the owner's)."""
        pids = ([self.rpc.pid] if self.rpc and self.rpc.poll() is None else []) + list(self.extra_pids())
        out = []
        for pid in pids:
            if pid not in self._ps:
                try:
                    self._ps[pid] = psutil.Process(pid)
                except psutil.Error:
                    continue
            out.append(self._ps[pid])
        self._ps = {p.pid: p for p in out}
        return out

    def event(self, msg):
        log.info(msg)
        self.events = (self.events + [{"ts": time.time(), "msg": msg}])[-50:]

    def status(self):
        snap = self.last_snap or hw.snapshot(self.temp, self.procs())
        total = snap["ram_total"]
        return {
            "node_id": self.st.data["node_id"], "name": self.st.data["name"], "fingerprint": tls.short_fp(self.fp),
            "static": self.static, "snap": snap, "policy": self.policy,
            "memory": {
                "total": total, "used": snap["ram_used"], "available": snap["ram_available"],
                "host_reserve": policy.host_reserve(total), "min_free": policy.min_free(total),
                "contribution_cap": policy.contribution_cap(total, self.policy),
                "allocated": self.allocated(), "in_use": snap["cluster_rss"],
                "budget": 0 if self.paused else policy.budget(snap, self.policy, self.allocated()),
            },
            "paused": self.paused, "task": self.task, "connected": self.connected,
            "coordinator": (self.st.data.get("coordinator") or {}).get("addr"),
            "paired": bool((self.st.data.get("coordinator") or {}).get("token")),
            "pending_pair": self.st.data.get("pending_pair"),
            "rpc_running": bool(self.rpc and self.rpc.poll() is None),
            "tunnel": vars(self.tunnel.stats) if self.tunnel else None,
            "gpu_backend": runtime.has_gpu_backend(),
            "cache_bytes": _dir_size(self.cache_dir),
            "events": self.events[-20:],
        }

    def set_policy(self, p):
        self.st.data["policy"] = policy.validate(p)
        self.st.save()
        self.event("owner updated contribution policy")

    # ---------- pairing ----------
    async def pair(self, host, port, code):
        """One-time: fetch coordinator cert, show its fingerprint, request approval, wait for it."""
        pem = await asyncio.to_thread(tls.fetch_peer_cert, host, port)
        cfp = tls.short_fp(tls.fingerprint_pem(pem))
        print(f"\n  Coordinator {host}:{port} fingerprint: {cfp}")
        print(f"  This node's fingerprint:           {tls.short_fp(self.fp)}")
        print("  Approve this node in the coordinator dashboard ONLY if both fingerprints match there.\n")
        secret = secrets.token_hex(16)
        ctx = tls.pinned_client_ctx(pem)
        async with httpx.AsyncClient(verify=ctx, timeout=10) as c:
            r = await c.post(f"https://{host}:{port}/api/pair", json={
                "code": code, "node_id": self.st.data["node_id"], "name": self.st.data["name"],
                "cert_pem": self.cert_pem, "poll_secret": secret, "hw": self.static})
            if r.status_code != 200:
                raise RuntimeError(f"pairing refused: {r.status_code} {r.text}")
            rid = r.json()["request_id"]
            self.st.data["pending_pair"] = {"addr": f"{host}:{port}", "coord_fp": cfp}
            self.st.save()
            self.event(f"pairing requested at {host}:{port}; waiting for approval")
            while True:
                await asyncio.sleep(2)
                r = await c.get(f"https://{host}:{port}/api/pair/{rid}", params={"secret": secret})
                j = r.json()
                if j.get("status") == "approved":
                    self.st.data["coordinator"] = {"addr": f"{host}:{port}", "cert_pem": pem, "token": j["token"]}
                    self.st.data.pop("pending_pair", None)
                    self.st.save()
                    self.event("approved by coordinator")
                    return
                if j.get("status") in ("rejected", "expired", "unknown"):
                    self.st.data.pop("pending_pair", None)
                    self.st.save()
                    raise RuntimeError(f"pairing {j.get('status')}")

    async def ensure_paired(self):
        c = self.st.data.get("coordinator")
        if c and c.get("token"):
            return
        if self.target:
            host, _, p = self.target.partition(":")
            port = int(p or self.port)
        else:
            print("Searching LAN for a MeshRAM coordinator (UDP broadcast)...")
            found = await discovery.listen(6)
            if not found:
                raise RuntimeError("no coordinator found on the LAN; pass --coordinator <ip>")
            (host, info), = list(found.items())[:1]
            port = info["port"]
            print(f"Found coordinator '{info.get('name')}' at {host}:{port} (beacon fp {info.get('fp')})")
        code = self.pair_code or input("Pairing code shown on the coordinator dashboard: ").strip()
        await self.pair(host, port, code)

    # ---------- main loop ----------
    async def run(self):
        asyncio.create_task(self._monitor())
        backoff = 1
        while True:
            try:
                await self.ensure_paired()
                await self._session()
                backoff = 1
            except InvalidStatus as e:
                if e.response.status_code in (401, 403):
                    self.event("coordinator rejected our credentials (revoked?) - pairing required again")
                    self.st.data.pop("coordinator", None)
                    self.st.save()
                    await self._release("revoked")
                    if not self.pair_code:
                        return
                else:
                    self.event(f"connect failed: HTTP {e.response.status_code}")
            except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
                self.event(f"coordinator unreachable: {type(e).__name__} {e}")
            except RuntimeError as e:
                self.event(str(e))
                if "pairing" in str(e) or "no coordinator" in str(e):
                    return
            self.connected = False
            if self.disconnected_at is None:
                self.disconnected_at = time.time()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _session(self):
        c = self.st.data["coordinator"]
        host, _, port = c["addr"].rpartition(":")
        ctx = tls.pinned_client_ctx(c["cert_pem"])
        hdrs = {"Authorization": f"Bearer {c['token']}", "X-Node-Id": self.st.data["node_id"]}
        async with connect(f"wss://{host}:{port}/ws", ssl=ctx, server_hostname="", additional_headers=hdrs,
                           max_size=8 * 2**20, open_timeout=10, ping_interval=10, ping_timeout=20) as ws:
            self.ws, self.connected, self.disconnected_at = ws, True, None
            self.coord_ip = socket.gethostbyname(host)
            self.event(f"connected to coordinator {host}:{port}")
            await self._send({"t": "hello", "static": self.static, "fp": tls.short_fp(self.fp),
                              "rpc_running": bool(self.rpc and self.rpc.poll() is None), "name": self.st.data["name"]})
            sender = asyncio.create_task(self._metrics_loop())
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        self._on_bytes(msg)
                    else:
                        asyncio.create_task(self._on_msg(json.loads(msg)))
            finally:
                sender.cancel()
                self.ws = None

    async def _send(self, obj):
        if self.ws:
            await self.ws.send(json.dumps(obj))

    async def _metrics_loop(self):
        while True:
            st = self.status()
            await self._send({"t": "metrics", "status": {k: st[k] for k in
                              ("snap", "memory", "policy", "paused", "task", "rpc_running", "tunnel",
                               "gpu_backend", "cache_bytes", "name")}})
            await asyncio.sleep(2)

    async def _monitor(self):
        """Owner protection: sample every 2 s, pause & release when the owner needs the machine."""
        while True:
            snap = hw.snapshot(self.temp, self.procs())
            self.last_snap = snap
            self.busy_streak = self.busy_streak + 1 if policy.is_busy(snap, self.policy) else 0
            reason = policy.pause_reason(snap, self.policy, self.busy_streak)
            if reason != self.paused:
                self.event(f"paused: {reason}" if reason else "resumed contributing")
                self.paused = reason
                if reason and self.rpc:
                    await self._release(f"paused: {reason}")
            # belt-and-braces: OS limit should make this impossible
            rpc_ps = self._ps.get(self.rpc.pid) if self.rpc else None
            try:
                rpc_rss = rpc_ps.memory_info().rss if rpc_ps else 0
            except psutil.Error:
                rpc_rss = 0
            if self.rpc_limit and rpc_rss > self.rpc_limit * 1.05:
                await self._release(f"rpc-server exceeded its memory limit "
                                    f"({rpc_rss >> 20} MiB > {self.rpc_limit >> 20} MiB)")
            if self.rpc and self.rpc.poll() is not None and self.rpc_limit:
                await self._release(f"rpc-server exited with code {self.rpc.returncode}")
            if not self.connected and self.disconnected_at and time.time() - self.disconnected_at > 15 \
                    and self.procs():
                await self._release("coordinator connection lost for 15s")
            await asyncio.sleep(2)

    # ---------- the ONLY things a coordinator can ask for ----------
    async def _on_msg(self, m):
        if m.get("t") == "ping":
            return await self._send({"t": "pong", "ts": m.get("ts")})
        if m.get("t") != "cmd":
            return
        op, cid, args = m.get("op"), m.get("id"), m.get("args") or {}
        fn = COMMANDS.get(op)
        try:
            if not fn:
                raise PermissionError(f"command '{op}' is not allowed")
            data = await fn(self, args)
            await self._send({"t": "result", "id": cid, "ok": True, "data": data})
        except Exception as e:
            await self._send({"t": "result", "id": cid, "ok": False, "error": f"{type(e).__name__}: {e}"})

    async def cmd_rpc_start(self, a):
        mem = _int(a, "mem_bytes", 1, 1 << 42)
        token = a.get("token")
        if not (isinstance(token, str) and len(token) == 64 and all(ch in "0123456789abcdef" for ch in token)):
            raise ValueError("bad token")
        if self.paused:
            raise PermissionError(f"node paused: {self.paused}")
        await self._release(None)
        ok, why = policy.admit(mem, self.last_snap or hw.snapshot(self.temp), self.policy, self.allocated())
        if not ok:
            raise PermissionError(why)
        port = runtime.free_port()
        threads = policy.threads(self.policy, self.static["cpu_logical"])
        self.rpc = runtime.start_rpc_server(port, threads, mem, self.cache_dir, cpu_only=not self.policy["allow_gpu"],
                                            log_path=os.path.join(self.dir, "rpc-server.log"))
        self.rpc_limit = mem
        await asyncio.sleep(0.7)
        if self.rpc.poll() is not None:
            self.rpc_limit = 0
            raise RuntimeError("rpc-server failed to start (see rpc-server.log)")
        self.tunnel = TunnelServer(self.policy["tunnel_port"], tls.server_ctx(self.crt, self.key), port, token,
                                   {self.coord_ip})
        try:
            await self.tunnel.start()
        except OSError as e:
            await self._release(None)
            raise RuntimeError(f"cannot listen on tunnel port {self.policy['tunnel_port']}: {e}")
        self.task = f"serving model layers {a.get('layers', '')}".strip()
        self.event(f"rpc-server started: {mem >> 20} MiB OS limit, {threads} threads, tunnel :{self.policy['tunnel_port']}")
        return {"tunnel_port": self.policy["tunnel_port"], "threads": threads}

    async def cmd_rpc_stop(self, a):
        await self._release("stopped by coordinator")
        return {}

    async def cmd_alloc_test(self, a):
        """Real allocation inside the contribution limit: commit + touch every page, measure, free."""
        nbytes = _int(a, "mb", 1, 1 << 20) * 2**20
        ok, why = policy.admit(nbytes, self.last_snap or hw.snapshot(self.temp), self.policy, self.allocated())
        if not ok:
            raise PermissionError(why)
        self.alloc_test_bytes = nbytes
        self.task = f"memory test {nbytes >> 20} MiB"
        try:
            return await asyncio.to_thread(_alloc_touch, nbytes)
        finally:
            self.alloc_test_bytes = 0
            self.task = None if not self.rpc else self.task

    async def cmd_net_test(self, a):
        n = _int(a, "bytes", 1, 512 * 2**20)
        self.net_rx = {"want": n, "got": 0, "t0": None, "done": asyncio.get_running_loop().create_future()}
        try:
            t = await asyncio.wait_for(self.net_rx["done"], 60)
        finally:
            self.net_rx = None
        return {"received": n, "seconds": t}

    def _on_bytes(self, b):
        r = self.net_rx
        if not r:
            return
        if r["t0"] is None:
            r["t0"] = time.perf_counter()
        r["got"] += len(b)
        if r["got"] >= r["want"] and not r["done"].done():
            r["done"].set_result(time.perf_counter() - r["t0"])

    async def _release(self, reason):
        if self.tunnel:
            await self.tunnel.stop()
            self.tunnel = None
        if self.rpc:
            if self.rpc.poll() is None:
                self.rpc.kill()
                await asyncio.to_thread(self.rpc.wait)
            self.rpc = None
        if self.rpc_limit and reason:
            self.event(f"released cluster memory ({self.rpc_limit >> 20} MiB): {reason}")
            await self._send({"t": "released", "reason": reason})
        self.rpc_limit = 0
        self.task = None


COMMANDS = {"rpc_start": Worker.cmd_rpc_start, "rpc_stop": Worker.cmd_rpc_stop,
            "alloc_test": Worker.cmd_alloc_test, "net_test": Worker.cmd_net_test}


def _int(a, k, lo, hi):
    v = a.get(k)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"{k} must be an integer in [{lo}, {hi}]")
    return v


def _alloc_touch(nbytes):
    p = psutil.Process()
    before = p.memory_info().rss
    t0 = time.perf_counter()
    buf = bytearray(nbytes)
    mv = memoryview(buf)
    for i in range(0, nbytes, 4096):  # force the OS to actually commit every page
        mv[i] = 1
    t_alloc = time.perf_counter() - t0
    peak = p.memory_info().rss
    ok = sum(mv[i] for i in range(0, nbytes, 4096 * 256)) == len(range(0, nbytes, 4096 * 256))
    del mv, buf
    after = p.memory_info().rss
    return {"bytes": nbytes, "alloc_seconds": round(t_alloc, 3), "rss_delta": peak - before,
            "rss_after_free_delta": after - before, "verified": ok,
            "gb_per_s": round(nbytes / t_alloc / 2**30, 2) if t_alloc else None}


def _dir_size(d):
    total = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
