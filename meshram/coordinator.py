"""Coordinator: node registry, pairing/approval, control channel, scheduling, dashboard API.

Two listeners, on purpose:
  node API   https://0.0.0.0:7861   (TLS, LAN only)   workers pair + hold their control WebSocket here
  dashboard  http://127.0.0.1:7860  (loopback only)  the owner's UI; never reachable from the network
"""
import asyncio
import collections
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import discovery, gguf, hw, policy, runtime, scheduler, store, tls
from .engine import LlmPipeline

WEB_DIST = os.path.join(runtime.ROOT, "web", "dist")
HEARTBEAT_TIMEOUT = 10


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimit:
    def __init__(self, n, per):
        self.n, self.per, self.hits = n, per, collections.defaultdict(collections.deque)

    def ok(self, key):
        q, now = self.hits[key], time.time()
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        return True


class Live:
    """A connected, authenticated node."""

    def __init__(self, ws, ip):
        self.ws, self.ip = ws, ip
        self.status, self.static, self.fp = None, {}, None
        self.last_seen = time.time()
        self.connected_at = time.time()
        self.rtts = collections.deque(maxlen=20)
        self.futures = {}
        self.net = None   # last throughput test
        self.alloc = None  # last memory test
        self.lock = asyncio.Lock()


class Coordinator:
    def __init__(self, data_dir, node_port=7861, models_dir=None):
        self.dir = data_dir
        self.node_port = node_port
        self.models_dir = models_dir or os.path.join(runtime.ROOT, "models")
        self.st = store.Store(os.path.join(data_dir, "state.json"))
        self.st.data.setdefault("nodes", {})
        self.st.data.setdefault("pairing_code", _code())
        self.st.save()
        self.crt, self.key = tls.ensure_identity(data_dir)
        self.fp = tls.fingerprint_pem(open(self.crt).read())
        self.bench_store = store.Store(os.path.join(data_dir, "benchmarks.json"))
        self.bench_store.data.setdefault("runs", [])
        self.pending = {}
        self.live = {}
        self.events = collections.deque(maxlen=200)
        self.history = collections.deque(maxlen=180)
        self.deployment = None
        self.bench_job = None
        self.pair_rl = RateLimit(10, 60)
        self.auth_rl = RateLimit(30, 60)
        self.last_traffic, self.rates = {}, {}
        self.temp = hw.TempReader()
        self.log_dir = os.path.join(data_dir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)

    def event(self, msg, level="info"):
        self.events.append({"ts": time.time(), "msg": msg, "level": level})
        print(f"[meshram] {msg}", flush=True)

    # ---------------- control channel ----------------
    async def command(self, node_id, op, args, timeout=30):
        n = self.live.get(node_id)
        if not n:
            raise RuntimeError("node offline")
        cid = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        n.futures[cid] = fut
        try:
            await n.ws.send_text(json.dumps({"t": "cmd", "id": cid, "op": op, "args": args}))
            r = await asyncio.wait_for(fut, timeout)
        finally:
            n.futures.pop(cid, None)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "command failed"))
        return r.get("data")

    async def net_test(self, node_id, nbytes=16 * 2**20):
        n = self.live[node_id]
        async with n.lock:
            t0 = time.perf_counter()
            task = asyncio.create_task(self.command(node_id, "net_test", {"bytes": nbytes}, timeout=90))
            await asyncio.sleep(0.05)
            chunk = os.urandom(2**20)  # incompressible
            for _ in range(nbytes // len(chunk)):
                await n.ws.send_bytes(chunk)
            r = await task
            dt = time.perf_counter() - t0
            n.net = {"bytes": nbytes, "seconds": round(dt, 3), "mbit_s": round(nbytes * 8 / dt / 1e6, 1),
                     "rtt_ms": _avg(n.rtts), "ts": time.time()}
            self.event(f"network test {self._name(node_id)}: {n.net['mbit_s']} Mbit/s, RTT {n.net['rtt_ms'] if n.net['rtt_ms'] is not None else 'pending'} ms")
            return n.net

    def _name(self, nid):
        return self.st.data["nodes"].get(nid, {}).get("name", nid[:8])

    async def ws_session(self, ws: WebSocket):
        ip = ws.client.host if ws.client else "?"
        if not _lan(ip) or not self.auth_rl.ok(ip):
            return await ws.close(code=1008)
        nid = ws.headers.get("x-node-id", "")
        auth = ws.headers.get("authorization", "")
        rec = self.st.data["nodes"].get(nid)
        if not rec or not auth.startswith("Bearer ") or not hmac.compare_digest(_h(auth[7:]), rec["token_hash"]):
            self.event(f"rejected connection from {ip}: bad credentials", "warn")
            return await ws.close(code=1008)  # -> HTTP 403 before accept
        await ws.accept()
        old = self.live.get(nid)
        if old:  # duplicate session for same node: newest wins
            try:
                await old.ws.close(code=4000)
            except Exception:
                pass
        n = self.live[nid] = Live(ws, ip)
        self.event(f"node {rec['name']} online ({ip})")
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                n.last_seen = time.time()
                if m.get("text") is None:
                    continue
                j = json.loads(m["text"])
                t = j.get("t")
                if t == "metrics":
                    n.status = j.get("status")
                elif t == "hello":
                    n.static, n.fp = j.get("static") or {}, j.get("fp")
                    rec["static"] = n.static
                    self.st.save()
                    if j.get("rpc_running") and not self._in_deployment(nid):
                        asyncio.create_task(self.command(nid, "rpc_stop", {}))  # stale from before a restart
                    asyncio.create_task(self._initial_net(nid))
                elif t == "pong" and isinstance(j.get("ts"), (int, float)):
                    n.rtts.append(round((time.time() - j["ts"]) * 1000, 2))
                elif t == "result":
                    f = n.futures.get(j.get("id"))
                    if f and not f.done():
                        f.set_result(j)
                elif t == "released":
                    self.event(f"{rec['name']} released cluster memory: {j.get('reason')}", "warn")
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if self.live.get(nid) is n:
                del self.live[nid]
                self.event(f"node {rec['name']} offline", "warn")
            for f in n.futures.values():
                if not f.done():
                    f.set_result({"ok": False, "error": "node disconnected"})

    async def _initial_net(self, nid):
        await asyncio.sleep(3)
        try:
            await self.net_test(nid, 4 * 2**20)
        except Exception:
            pass

    def _in_deployment(self, nid):
        d = self.deployment
        return bool(d and d.state in ("loading", "ready", "starting") and any(p["node_id"] == nid for p in d.parts))

    async def housekeeping(self):
        """Heartbeats, latency pings, deployment health, traffic rates, history."""
        tick = 0
        while True:
            now = time.time()
            for nid, n in list(self.live.items()):
                if now - n.last_seen > HEARTBEAT_TIMEOUT:
                    self.event(f"node {self._name(nid)} missed heartbeats for {HEARTBEAT_TIMEOUT}s", "warn")
                    try:
                        await n.ws.close(code=4001)
                    except Exception:
                        pass
                    self.live.pop(nid, None)
                elif tick % 5 == 0:
                    try:
                        await n.ws.send_text(json.dumps({"t": "ping", "ts": time.time()}))
                    except Exception:
                        pass
            d = self.deployment
            if d:
                why = d.check()
                if why:
                    await d.stop(why, failed=True)
                    self.event(f"inference FAILED: {why}", "error")
                    asyncio.create_task(self._maybe_replan(d))
                tr = d.traffic()
                for k, v in tr.items():
                    p = self.last_traffic.get(k, v)
                    self.rates[k] = {"tx_bps": v["tx"] - p["tx"], "rx_bps": v["rx"] - p["rx"]}
                self.last_traffic = tr
            else:
                self.rates, self.last_traffic = {}, {}
            for rid, p in list(self.pending.items()):
                if now - p["created"] > 900:
                    p["status"] = "expired"
                if now - p["created"] > 1800:
                    del self.pending[rid]
            s = self.summary()
            self.history.append({"ts": now, "cpu": s["cpu_avg"], "in_use": s["ram_in_use"],
                                 "allocated": s["ram_allocated"],
                                 "tx": sum(r["tx_bps"] for r in self.rates.values()),
                                 "rx": sum(r["rx_bps"] for r in self.rates.values())})
            tick += 1
            await asyncio.sleep(1)

    async def _maybe_replan(self, failed):
        """Retry only if the remaining nodes can actually hold the whole model."""
        if failed.native or getattr(failed, "replanned", False) or self.bench_job:
            return
        await asyncio.sleep(3)
        lost = {p["node_id"] for p in failed.parts if p["node_id"] not in self.live
                or (self.live[p["node_id"]].status or {}).get("paused")}
        plan = scheduler.plan(failed.model, [n for n in self.budgets() if n["id"] not in lost],
                              ctx=failed.plan["ctx"], ubatch=failed.plan["ubatch"], strategy=failed.plan["strategy"])
        if not plan["ok"]:
            self.event(f"no automatic retry: remaining nodes can't hold the model ({plan['reason']})", "warn")
            return
        self.event("retrying deployment on remaining nodes (previous generation was lost)")
        d = await self.deploy(failed.model, plan)
        d.replanned = True

    # ---------------- cluster view ----------------
    def budgets(self, node_ids=None):
        out = []
        for nid, n in self.live.items():
            if node_ids and nid not in node_ids:
                continue
            st = n.status or {}
            mem = st.get("memory") or {}
            b = 0 if st.get("paused") else mem.get("budget", 0)
            if self._in_deployment(nid):
                b += mem.get("allocated", 0)  # its own current allocation would be released on redeploy
            out.append({"id": nid, "name": self._name(nid), "budget": b})
        return out

    def summary(self):
        nodes = [n.status or {} for n in self.live.values()]
        mem = [s.get("memory") or {} for s in nodes]
        gpus = [g for n in self.live.values() for g in (n.static or {}).get("gpus", [])]
        return {
            "nodes_online": len(self.live), "nodes_total": len(self.st.data["nodes"]),
            "ram_total": sum(m.get("total", 0) for m in mem),
            "ram_contributed": sum(m.get("contribution_cap", 0) for m in mem),
            "ram_budget_now": sum(b["budget"] for b in self.budgets()),
            "ram_allocated": sum(m.get("allocated", 0) for m in mem),
            "ram_in_use": sum(m.get("in_use", 0) for m in mem),
            "cpu_logical": sum((n.static or {}).get("cpu_logical", 0) for n in self.live.values()),
            "cpu_avg": round(_avg([(s.get("snap") or {}).get("cpu_pct", 0) for s in nodes]) or 0, 1),
            "gpu_count": len(gpus),
            "vram_total": sum(g.get("vram_total") or 0 for g in gpus),
            "paused": sum(1 for s in nodes if s.get("paused")),
            "active_tasks": sum(1 for s in nodes if s.get("task")),
            "status": self._status(),
        }

    def _status(self):
        d = self.deployment
        if not self.live:
            return "no nodes"
        if d and d.state == "failed":
            return "degraded"
        if d and d.state in ("loading", "starting"):
            return "loading model"
        if d and d.state == "ready":
            return "inference running" if d.busy else "model ready"
        return "idle"

    def models(self):
        out = []
        for p in sorted(glob.glob(os.path.join(self.models_dir, "*.gguf"))):
            try:
                m = gguf.read(p)
            except Exception as e:
                out.append({"file": os.path.basename(p), "error": str(e)})
                continue
            est = scheduler.node_cost(m, 0, m["n_layer"], 2048, 256, True)[0]
            out.append({k: m[k] for k in ("file", "name", "arch", "quant", "n_layer", "size_bytes", "train_ctx")}
                       | {"est_bytes_2k": est})
        return out

    def model(self, file):
        p = os.path.join(self.models_dir, os.path.basename(file))
        if not p.endswith(".gguf") or not os.path.exists(p):
            raise HTTPException(404, "model not found in models/ folder")
        return gguf.read(p)

    def state(self):
        nodes = []
        for nid, rec in self.st.data["nodes"].items():
            n = self.live.get(nid)
            nodes.append({"id": nid, "name": rec["name"], "fingerprint": rec.get("fp"),
                          "approved_at": rec.get("approved_at"), "online": bool(n), "local": rec.get("local", False),
                          "ip": n.ip if n else rec.get("last_ip"), "static": (n.static if n else rec.get("static")) or {},
                          "status": n.status if n else None,
                          "rtt_ms": _avg(n.rtts) if n else None, "net": n.net if n else None,
                          "alloc": n.alloc if n else None,
                          "traffic": self.rates.get(nid), "connected_at": n.connected_at if n else None})
        d = self.deployment
        return {"summary": self.summary(), "nodes": nodes,
                "pending": [{k: v for k, v in p.items() if k not in ("secret_hash", "token", "cert_pem", "fp_full")} | {"id": rid}
                            for rid, p in self.pending.items() if p["status"] == "pending"],
                "pairing_code": self.st.data["pairing_code"], "fingerprint": tls.short_fp(self.fp),
                "node_port": self.node_port, "lan_ips": _lan_ips(),
                "deployment": d.summary() if d else None,
                "bench": self.bench_job, "events": list(self.events)[-60:], "history": list(self.history)}

    # ---------------- deployments ----------------
    async def deploy(self, model, plan, native=False):
        if self.deployment:
            await self.deployment.stop("replaced by new deployment")
        d = LlmPipeline(self, model, plan, self.log_dir, native=native)
        self.deployment = d
        await d.start()
        return d

    async def benchmark(self, file, ctx, n_predict, prompt, node_ids, strategy):
        """Real runs: native single machine, best single node, and the distributed plan. Same greedy prompt."""
        model = self.model(file)
        job = self.bench_job = {"model": file, "started": time.time(), "step": "planning", "results": []}
        try:
            nodes = self.budgets(node_ids)
            dist = scheduler.plan(model, nodes, ctx=ctx, ubatch=256, strategy=strategy)
            single = scheduler.plan(model, sorted(nodes, key=lambda n: -n["budget"])[:1], ctx=ctx, ubatch=256)
            # native baseline runs on the coordinator machine itself; obey the same safety floor
            snap = hw.snapshot(self.temp)
            native_need = single["need_bytes"]
            native_ok = native_need <= snap["ram_available"] - policy.min_free(snap["ram_total"])
            runs = [("native (coordinator CPU, no network)", None, native_ok,
                     f"needs {native_need >> 20} MiB, this machine can spare "
                     f"{max(0, snap['ram_available'] - policy.min_free(snap['ram_total'])) >> 20} MiB"),
                    ("single node via RPC", single, single["ok"], single.get("reason")),
                    (f"distributed ({len(dist['assignments'])} nodes, {strategy})", dist,
                     dist["ok"] and len(dist["assignments"]) > 1,
                     dist.get("reason") or "plan uses only one node (select more nodes or use 'spread')")]
            ref = None
            for label, plan, ok, why in runs:
                r = {"mode": label, "ran": ok}
                job["results"].append(r)
                if not ok:
                    r["skipped"] = why
                    continue
                job["step"] = f"loading: {label}"
                p = plan or {"ctx": ctx, "ubatch": 256, "assignments": [], "strategy": "native"}
                d = await self.deploy(model, p, native=plan is None)
                if d.state != "ready":
                    r["error"] = d.error
                    continue
                job["step"] = f"generating: {label}"
                samples = []
                sampler = asyncio.create_task(self._sample(d, samples))
                try:
                    text, stats = await d.complete(prompt, n_predict)
                except Exception as e:
                    r["error"] = d.check() or f"{type(e).__name__}: {e}"
                    continue
                finally:
                    sampler.cancel()
                ref = text if ref is None else ref
                r.update(stats, load_seconds=d.load_seconds, output=text[:400],
                         matches_first=text == ref,
                         nodes=[{"name": x["name"], "layers": f"{x['start']}-{x['end'] - 1}",
                                 "actual_mib": {k: v >> 20 for k, v in (x["actual"] or {}).items()},
                                 "est_mib": x["est_bytes"] >> 20} for x in d.parts],
                         node_samples=_reduce(samples), host_actual_mib={k: v >> 20 for k, v in
                                                                         (d.buffers.get("host") or {}).items()})
                await d.stop("benchmark step done")
            run = {"ts": time.time(), "model": file, "quant": model["quant"], "size_bytes": model["size_bytes"],
                   "ctx": ctx, "n_predict": n_predict, "results": job["results"],
                   "fits_single_node": dist["fits_single_node"], "need_bytes": dist["need_bytes"],
                   "largest_node_budget": dist["largest_node_budget"], "verdict": verdict(job["results"], dist)}
            self.bench_store.data["runs"].append(run)
            self.bench_store.save()
            job["step"] = "done"
            self.event(f"benchmark done: {run['verdict']['headline']}")
        except Exception as e:
            job["step"] = f"error: {e}"
            self.event(f"benchmark error: {e}", "error")
        finally:
            job["finished"] = time.time()

    async def _sample(self, d, out):
        while True:
            for p in d.parts:
                n = self.live.get(p["node_id"])
                if n and n.status:
                    s = n.status["snap"]
                    out.append({"name": p["name"], "cpu": s["cpu_pct"], "cluster_cpu": s["cluster_cpu_pct"],
                                "rss": s["cluster_rss"], "threads_pct": n.status["policy"]["max_cpu_pct"]})
            await asyncio.sleep(1)


def verdict(results, dist):
    ran = {r["mode"].split(" (")[0]: r for r in results if r.get("ran") and r.get("gen_tps")}
    base = ran.get("native") or ran.get("single node via RPC")
    dist_r = next((r for r in results if r["mode"].startswith("distributed") and r.get("gen_tps")), None)
    lines = []
    if not dist["fits_single_node"]:
        head = ("CAPABILITY: the cluster runs a model no single node can hold "
                f"(needs {dist['need_bytes'] >> 20} MiB, largest node budget {dist['largest_node_budget'] >> 20} MiB)")
        if not dist_r:
            head = f"RAM-LIMITED: model does not fit even across the cluster ({dist.get('reason')})"
    elif dist_r and base:
        ratio = dist_r["gen_tps"] / base["gen_tps"]
        head = (f"distributed is {'FASTER' if ratio > 1.02 else 'SLOWER'}: {dist_r['gen_tps']} vs "
                f"{base['gen_tps']} tok/s ({ratio:.2f}x). Model also fits on one node, so splitting only adds network hops.")
    else:
        head = "inconclusive: not enough successful runs"
    if dist_r:
        ns = dist_r.get("node_samples") or {}
        busy = [v["cluster_cpu"] / max(v["threads_pct"], 1) for v in ns.values()]
        b = dist_r.get("net_bytes_per_token")
        if busy and max(busy) < 0.5:
            lines.append(f"LIMITED BY NETWORK/LATENCY: serving nodes used <50% of their allowed CPU while generating "
                         f"(nodes wait on per-token round trips; {b} bytes moved per token)")
        elif busy:
            lines.append("LIMITED BY CPU: serving nodes were near their allowed CPU while generating")
        if not dist_r.get("matches_first", True):
            lines.append("note: greedy output differed from the first run (float reduction order differs across devices)")
    return {"headline": head, "details": lines}


def _reduce(samples):
    out = {}
    for s in samples:
        o = out.setdefault(s["name"], {"n": 0, "cpu": 0, "cluster_cpu": 0, "peak_rss_mib": 0, "threads_pct": s["threads_pct"]})
        o["n"] += 1
        o["cpu"] += s["cpu"]
        o["cluster_cpu"] += s["cluster_cpu"]
        o["peak_rss_mib"] = max(o["peak_rss_mib"], s["rss"] >> 20)
    for o in out.values():
        o["cpu"] = round(o["cpu"] / o["n"], 1)
        o["cluster_cpu"] = round(o["cluster_cpu"] / o["n"], 1)
    return out


def _avg(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 2) if xs else None


def _code():
    return f"{secrets.randbelow(10**6):06d}"


# Overlay VPNs (Tailscale / Headscale use 100.64.0.0/10; ZeroTier/WireGuard usually RFC1918) let nodes on
# different mobile networks reach each other through carrier NAT. Public internet addresses stay rejected.
_OVERLAY = [ipaddress.ip_network("100.64.0.0/10")]


def _lan(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_link_local or any(a in n for n in _OVERLAY)
    except ValueError:
        return False


def _lan_ips():
    import socket
    import psutil
    return [a.address for addrs in psutil.net_if_addrs().values() for a in addrs
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254."))]


# ======================= HTTP apps =======================
def node_app(c: Coordinator):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/api/pair")
    async def pair(req: Request):
        ip = req.client.host
        if not _lan(ip):
            raise HTTPException(403, "LAN only")
        if not c.pair_rl.ok(ip):
            raise HTTPException(429, "too many pairing attempts")
        j = await req.json()
        code, nid, name, pem, secret = (j.get(k) for k in ("code", "node_id", "name", "cert_pem", "poll_secret"))
        if not all(isinstance(x, str) for x in (code, nid, name, pem, secret)) or len(name) > 64 or len(nid) > 64 \
                or len(pem) > 8192 or len(secret) > 128:
            raise HTTPException(400, "bad request")
        if not hmac.compare_digest(code.strip(), c.st.data["pairing_code"]):
            c.event(f"pairing attempt from {ip} with wrong code", "warn")
            raise HTTPException(403, "wrong pairing code")
        try:
            fp = tls.fingerprint_pem(pem)
        except Exception:
            raise HTTPException(400, "bad certificate")
        if nid in c.st.data["nodes"]:
            raise HTTPException(409, "this node is already registered; remove it in the dashboard to re-pair")
        if any(r.get("fp_full") == fp for r in c.st.data["nodes"].values()):
            raise HTTPException(409, "a node with this identity is already registered")
        for rid, p in list(c.pending.items()):  # one pending request per node
            if p["node_id"] == nid:
                del c.pending[rid]
        rid = secrets.token_hex(8)
        hwinfo = j.get("hw") if isinstance(j.get("hw"), dict) else {}
        c.pending[rid] = {"node_id": nid, "name": name.strip()[:64], "cert_pem": pem, "fp": tls.short_fp(fp),
                          "fp_full": fp, "ip": ip, "hw": {k: hwinfo.get(k) for k in
                                                          ("hostname", "os", "cpu_logical", "ram_total")},
                          "secret_hash": _h(secret), "created": time.time(), "status": "pending"}
        c.event(f"pairing request from {name} ({ip}) - fingerprint {tls.short_fp(fp)}; awaiting approval")
        return {"request_id": rid, "coordinator_fp": tls.short_fp(c.fp)}

    @app.get("/api/pair/{rid}")
    async def pair_status(rid: str, secret: str, req: Request):
        p = c.pending.get(rid)
        if not p or not hmac.compare_digest(_h(secret), p["secret_hash"]):
            return {"status": "unknown"}
        if p["status"] == "approved":
            del c.pending[rid]
            return {"status": "approved", "token": p.pop("token")}
        return {"status": p["status"]}

    @app.websocket("/ws")
    async def ws(w: WebSocket):
        await c.ws_session(w)

    return app


def admin_app(c: Coordinator):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(req: Request, call_next):
        # loopback-only server; also block DNS-rebinding (Host) and cross-site writes (custom header => CORS preflight)
        host = (req.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return JSONResponse({"error": "forbidden host"}, 403)
        if req.method != "GET" and req.headers.get("x-meshram") != "1":
            return JSONResponse({"error": "missing X-MeshRAM header"}, 403)
        return await call_next(req)

    @app.get("/api/mode")
    async def mode():
        return {"mode": "coordinator"}

    @app.get("/api/state")
    async def state():
        return c.state()

    @app.get("/api/models")
    async def models():
        return c.models()

    @app.post("/api/pending/{rid}/{action}")
    async def decide(rid: str, action: str):
        p = c.pending.get(rid)
        if not p or p["status"] != "pending":
            raise HTTPException(404, "no such pending request")
        if action == "reject":
            p["status"] = "rejected"
            c.event(f"rejected {p['name']}")
            return {"ok": True}
        if action != "approve":
            raise HTTPException(400)
        names = {r["name"] for r in c.st.data["nodes"].values()}
        name, i = p["name"], 2
        while name in names:
            name, i = f"{p['name']}-{i}", i + 1
        token = secrets.token_hex(32)
        c.st.data["nodes"][p["node_id"]] = {"name": name, "token_hash": _h(token), "cert_pem": p["cert_pem"],
                                            "fp": p["fp"], "fp_full": p["fp_full"], "approved_at": time.time(),
                                            "last_ip": p["ip"], "static": p["hw"]}
        c.st.save()
        p["status"], p["token"] = "approved", token
        c.event(f"approved node {name} ({p['ip']})")
        return {"ok": True}

    @app.post("/api/nodes/{nid}/revoke")
    async def revoke(nid: str):
        rec = c.st.data["nodes"].pop(nid, None)
        if not rec:
            raise HTTPException(404)
        c.st.save()
        if c._in_deployment(nid):
            await c.deployment.stop(f"node {rec['name']} revoked", failed=True)
        n = c.live.pop(nid, None)
        if n:
            try:
                await n.ws.send_text(json.dumps({"t": "cmd", "id": "x", "op": "rpc_stop", "args": {}}))
                await n.ws.close(code=4003)
            except Exception:
                pass
        c.event(f"revoked node {rec['name']}", "warn")
        return {"ok": True}

    @app.post("/api/nodes/{nid}/rename")
    async def rename(nid: str, req: Request):
        name = str((await req.json()).get("name", "")).strip()[:64]
        if not name or nid not in c.st.data["nodes"]:
            raise HTTPException(400)
        c.st.data["nodes"][nid]["name"] = name
        c.st.save()
        return {"ok": True}

    @app.post("/api/nodes/{nid}/nettest")
    async def nettest(nid: str):
        if nid not in c.live:
            raise HTTPException(404, "node offline")
        try:
            return await c.net_test(nid)
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/api/nodes/{nid}/alloctest")
    async def alloctest(nid: str, req: Request):
        mb = (await req.json()).get("mb")
        if nid not in c.live:
            raise HTTPException(404, "node offline")
        try:
            r = await c.command(nid, "alloc_test", {"mb": mb}, timeout=120)
            c.live[nid].alloc = r | {"ts": time.time(), "ok": True}
            c.event(f"memory test on {c._name(nid)}: {mb} MiB committed in {r['alloc_seconds']}s, "
                    f"RSS +{r['rss_delta'] >> 20} MiB, released")
        except Exception as e:
            c.live[nid].alloc = {"ok": False, "error": str(e), "mb": mb, "ts": time.time()}
            c.event(f"memory test on {c._name(nid)} refused/failed: {e}", "warn")
        return c.live[nid].alloc

    @app.post("/api/pairing/rotate")
    async def rotate():
        c.st.data["pairing_code"] = _code()
        c.st.save()
        return {"code": c.st.data["pairing_code"]}

    def _plan_args(j):
        ctx = int(j.get("ctx", 2048))
        if not 128 <= ctx <= 131072:
            raise HTTPException(400, "ctx out of range")
        strategy = j.get("strategy", "min-nodes")
        if strategy not in ("min-nodes", "spread"):
            raise HTTPException(400, "bad strategy")
        ids = j.get("nodes") or None
        return c.model(str(j.get("model", ""))), ctx, strategy, ids

    @app.post("/api/plan")
    async def plan(req: Request):
        m, ctx, strategy, ids = _plan_args(await req.json())
        return scheduler.plan(m, c.budgets(ids), ctx=ctx, strategy=strategy)

    @app.post("/api/deploy")
    async def deploy(req: Request):
        if c.bench_job and not c.bench_job.get("finished"):
            raise HTTPException(409, "benchmark running")
        m, ctx, strategy, ids = _plan_args(await req.json())
        p = scheduler.plan(m, c.budgets(ids), ctx=ctx, strategy=strategy)
        if not p["ok"]:
            raise HTTPException(409, p["reason"])
        asyncio.create_task(c.deploy(m, p))
        return {"ok": True, "plan": p}

    @app.post("/api/undeploy")
    async def undeploy():
        if c.deployment:
            await c.deployment.stop("stopped by user")
        return {"ok": True}

    @app.post("/api/chat")
    async def chat(req: Request):
        j = await req.json()
        msgs = j.get("messages")
        if not isinstance(msgs, list) or not 1 <= len(msgs) <= 64 or not all(
                isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
                and isinstance(m.get("content"), str) and len(m["content"]) <= 20000 for m in msgs):
            raise HTTPException(400, "bad messages")
        mt = j.get("max_tokens", 256)
        temp = j.get("temperature", 0.7)
        if not isinstance(mt, int) or not 1 <= mt <= 4096 or not isinstance(temp, (int, float)) or not 0 <= temp <= 2:
            raise HTTPException(400, "bad parameters")
        d = c.deployment
        if not d or d.state != "ready":
            raise HTTPException(409, "no model ready")
        if d.busy:
            raise HTTPException(409, "model busy")
        msgs = [{"role": m["role"], "content": m["content"]} for m in msgs]
        return StreamingResponse(d.chat(msgs, mt, float(temp)), media_type="text/event-stream")

    @app.post("/api/bench")
    async def bench(req: Request):
        if c.bench_job and not c.bench_job.get("finished"):
            raise HTTPException(409, "benchmark already running")
        j = await req.json()
        m, ctx, strategy, ids = _plan_args(j | {"strategy": j.get("strategy", "spread")})
        n = j.get("n_predict", 64)
        prompt = j.get("prompt") or "Explain in simple words why the sky looks blue."
        if not isinstance(n, int) or not 8 <= n <= 1024 or not isinstance(prompt, str) or len(prompt) > 4000:
            raise HTTPException(400, "bad parameters")
        c.bench_job = {"step": "queued", "results": []}
        asyncio.create_task(c.benchmark(m["file"], ctx, n, prompt, ids, strategy))
        return {"ok": True}

    @app.get("/api/benchmarks")
    async def benchmarks():
        return c.bench_store.data["runs"][-50:]

    _static(app)
    return app


def _static(app):
    if os.path.isdir(WEB_DIST):
        app.mount("/assets", StaticFiles(directory=os.path.join(WEB_DIST, "assets")), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str):
            f = os.path.join(WEB_DIST, path)
            if path and os.path.isfile(f) and os.path.realpath(f).startswith(os.path.realpath(WEB_DIST)):
                return FileResponse(f)
            return FileResponse(os.path.join(WEB_DIST, "index.html"))
    else:
        @app.get("/")
        async def nobuild():
            return JSONResponse({"error": "dashboard not built: cd web && npm install && npm run build"})
