"""Integration: real coordinator + real worker agents over TLS/WebSocket, in one event loop.

Covers registration, auth, approval, duplicate prevention, metrics, command whitelist, memory limits,
network test, disconnect detection, reconnection, revocation, tunnel auth, and (if llama.cpp + model are
present) real distributed inference, correctness vs single machine, node-loss failure and cancellation.
"""
import asyncio
import contextlib
import json
import os
import socket
import ssl

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from meshram import coordinator as cm
from meshram import runtime, tls
from meshram.tunnel import TunnelClient, TunnelServer
from meshram.worker import Worker

MODEL_FILE = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
HAVE_LLAMA = os.path.exists(os.path.join(runtime.ROOT, "models", MODEL_FILE))
try:
    runtime.binary("llama-server")
    runtime.binary("ggml-rpc-server", "rpc-server")
except FileNotFoundError:
    HAVE_LLAMA = False


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(cond, timeout=20, msg="condition"):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {msg}")


class Cluster:
    def __init__(self, tmp):
        self.tmp = tmp
        self.np, self.up = port(), port()
        self.tasks = []

    async def start_coordinator(self):
        self.c = cm.Coordinator(os.path.join(self.tmp, "coord"), node_port=self.np)
        self.servers = [
            uvicorn.Server(uvicorn.Config(cm.admin_app(self.c), "127.0.0.1", self.up, log_level="error")),
            uvicorn.Server(uvicorn.Config(cm.node_app(self.c), "127.0.0.1", self.np, log_level="error",
                                          ssl_certfile=self.c.crt, ssl_keyfile=self.c.key))]
        self.ctasks = [asyncio.create_task(s.serve()) for s in self.servers] + [asyncio.create_task(self.c.housekeeping())]
        await until(lambda: all(s.started for s in self.servers), msg="servers")
        self.admin = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.up}", headers={"X-MeshRAM": "1"}, timeout=120)

    async def stop_coordinator(self):
        for s in self.servers:
            s.should_exit = True
        await asyncio.sleep(0.3)
        for t in self.ctasks:
            t.cancel()
        await self.admin.aclose()

    def worker(self, name, ram_gb=1.5, code=None):
        w = Worker(os.path.join(self.tmp, name), name=name, coordinator=f"127.0.0.1:{self.np}",
                   pair_code=code or self.c.st.data["pairing_code"])
        w.set_policy(w.policy | {"max_ram_gb": ram_gb, "tunnel_port": port(), "pause_when_busy": False})
        self.tasks.append(asyncio.create_task(w.run()))
        return w

    async def approve_all(self, n):
        await until(lambda: len([p for p in self.c.pending.values() if p["status"] == "pending"]) >= n, msg="pending")
        for rid in [r for r, p in self.c.pending.items() if p["status"] == "pending"]:
            assert (await self.admin.post(f"/api/pending/{rid}/approve")).status_code == 200

    async def paired_workers(self, *names, ram_gb=1.5):
        ws = [self.worker(n, ram_gb) for n in names]
        await self.approve_all(len(ws))
        await until(lambda: len(self.c.live) == len(ws) and all(n.status for n in self.c.live.values()),
                    msg="workers online")
        return ws

    async def close(self):
        for t in self.tasks:
            t.cancel()
        for w in getattr(self, "_workers", []):
            await w._release(None)
        if self.c.deployment:
            await self.c.deployment.stop("test end")
        await self.stop_coordinator()


@contextlib.asynccontextmanager
async def cluster(tmp_path):
    cl = Cluster(str(tmp_path))
    await cl.start_coordinator()
    try:
        yield cl
    finally:
        await cl.close()


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 300))


def nid(w):
    return w.st.data["node_id"]


# ------------------------------------------------------------------
def test_registration_auth_and_duplicates(tmp_path):
    async def t():
        async with cluster(tmp_path) as cl:
            # wrong pairing code is refused
            bad = cl.worker("evil", code="000000" if cl.c.st.data["pairing_code"] != "000000" else "111111")
            await until(lambda: bad.st.data.get("coordinator") is None and any(
                "wrong code" in e["msg"] for e in cl.c.events), msg="wrong code event")
            assert not cl.c.pending
            # correct code -> pending, NOT trusted until a human approves
            w = cl.worker("laptop-a")
            await until(lambda: cl.c.pending, msg="pending")
            await asyncio.sleep(1)
            assert not cl.c.live, "node must not connect before approval"
            st = (await cl.admin.get("/api/state")).json()
            assert st["pending"][0]["fp"] == tls.short_fp(w.fp)  # same fingerprint the worker prints
            await cl.approve_all(1)
            await until(lambda: nid(w) in cl.c.live and cl.c.live[nid(w)].status, msg="online")
            st = (await cl.admin.get("/api/state")).json()
            node = st["nodes"][0]
            assert node["online"] and node["status"]["memory"]["total"] > 0
            assert node["status"]["memory"]["contribution_cap"] <= int(1.5 * 2**30)
            # duplicate registration of the same node id is refused
            async with httpx.AsyncClient(verify=False) as h:
                r = await h.post(f"https://127.0.0.1:{cl.np}/api/pair", json={
                    "code": cl.c.st.data["pairing_code"], "node_id": nid(w), "name": "dup",
                    "cert_pem": w.cert_pem, "poll_secret": "x"})
                assert r.status_code == 409
            # a stolen node id without the token cannot open the control channel
            ctx = tls.pinned_client_ctx(open(cl.c.crt).read())
            with pytest.raises(InvalidStatus):
                async with connect(f"wss://127.0.0.1:{cl.np}/ws", ssl=ctx, server_hostname="",
                                   additional_headers={"X-Node-Id": nid(w), "Authorization": "Bearer " + "0" * 64}):
                    pass
            # the dashboard API rejects cross-site writes and foreign Host headers
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{cl.up}") as h:
                assert (await h.post("/api/pairing/rotate")).status_code == 403
                assert (await h.get("/api/state", headers={"Host": "evil.example"})).status_code == 403
            # a coordinator with a different certificate is not trusted by the worker (pinning)
            other = tls.ensure_identity(os.path.join(cl.tmp, "other"))[0]
            with pytest.raises(ssl.SSLError):
                async with connect(f"wss://127.0.0.1:{cl.np}/ws", ssl=tls.pinned_client_ctx(open(other).read()),
                                   server_hostname=""):
                    pass
    run(t())


def test_commands_whitelist_memory_limits_and_network(tmp_path):
    async def t():
        async with cluster(tmp_path) as cl:
            w, = await cl.paired_workers("laptop-a", ram_gb=1.0)
            # not on the whitelist -> refused by the worker
            with pytest.raises(RuntimeError, match="not allowed"):
                await cl.c.command(nid(w), "shell", {"cmd": "whoami"})
            with pytest.raises(RuntimeError, match="must be an integer"):
                await cl.c.command(nid(w), "alloc_test", {"mb": "64; rm -rf /"})
            # real allocation inside the limit
            r = (await cl.admin.post(f"/api/nodes/{nid(w)}/alloctest", json={"mb": 128})).json()
            assert r["ok"] and r["verified"] and r["rss_delta"] > 100 * 2**20
            # beyond the owner's limit -> refused before anything is allocated
            r = (await cl.admin.post(f"/api/nodes/{nid(w)}/alloctest", json={"mb": 4096})).json()
            assert not r["ok"] and "refused" in r["error"]
            # rpc_start beyond limit refused too
            with pytest.raises(RuntimeError, match="refused"):
                await cl.c.command(nid(w), "rpc_start", {"mem_bytes": 8 * 2**30, "token": "a" * 64})
            r = (await cl.admin.post(f"/api/nodes/{nid(w)}/nettest")).json()
            assert r["mbit_s"] > 0
            await until(lambda: cl.c.live[nid(w)].rtts, msg="rtt")
    run(t())


def test_disconnect_reconnect_and_revocation(tmp_path):
    async def t():
        async with cluster(tmp_path) as cl:
            w, = await cl.paired_workers("laptop-a")
            # coordinator restart: worker reconnects by itself with its stored credentials
            await cl.stop_coordinator()
            await until(lambda: not w.connected, msg="worker notices disconnect")
            await cl.start_coordinator()
            await until(lambda: nid(w) in cl.c.live, timeout=40, msg="reconnect")
            # revoke: connection closed and credentials no longer accepted
            assert (await cl.admin.post(f"/api/nodes/{nid(w)}/revoke")).status_code == 200
            await until(lambda: nid(w) not in cl.c.live, msg="revoked offline")
            await until(lambda: not (w.st.data.get("coordinator") or {}).get("token")
                        or any("revoked" in e["msg"] or "pairing" in e["msg"] for e in w.events), timeout=40,
                        msg="worker sees revocation")
            assert nid(w) not in cl.c.st.data["nodes"]
    run(t())


def test_tunnel_requires_token_and_allowed_ip(tmp_path):
    async def t():
        async def echo(r, w):
            while (b := await r.read(65536)):
                w.write(b)
                await w.drain()
            w.close()
        es = await asyncio.start_server(echo, "127.0.0.1", 0)
        eport = es.sockets[0].getsockname()[1]
        crt, key = tls.ensure_identity(str(tmp_path))
        token = "ab" * 32
        tp = port()
        srv = TunnelServer(tp, tls.server_ctx(crt, key), eport, token, {"127.0.0.1"})
        await srv.start()
        pin = tls.pinned_client_ctx(open(crt).read())
        good = TunnelClient("127.0.0.1", tp, pin, token)
        lp = await good.start()
        r, wr = await asyncio.open_connection("127.0.0.1", lp)
        payload = os.urandom(300_000)
        wr.write(payload)
        await wr.drain()
        got = await asyncio.wait_for(r.readexactly(len(payload)), 10)
        assert got == payload
        assert good.stats.tx >= len(payload) and good.stats.rx >= len(payload)
        wr.close()
        bad = TunnelClient("127.0.0.1", tp, pin, "cd" * 32)
        lp2 = await bad.start()
        r2, w2 = await asyncio.open_connection("127.0.0.1", lp2)
        w2.write(b"hello")
        await w2.drain()
        assert await asyncio.wait_for(r2.read(10), 10) == b""
        await until(lambda: srv.rejected >= 1, msg="rejected")
        srv2 = TunnelServer(port(), tls.server_ctx(crt, key), eport, token, {"10.9.9.9"})
        await srv2.start()
        c3 = TunnelClient("127.0.0.1", srv2.port, pin, token)
        lp3 = await c3.start()
        r3, w3 = await asyncio.open_connection("127.0.0.1", lp3)
        w3.write(b"x")
        await w3.drain()
        assert await asyncio.wait_for(r3.read(10), 10) == b""
        assert srv2.rejected == 1
        for s in (srv, srv2):
            await s.stop()
        es.close()
    run(t())


# ------------------------- real inference -------------------------
@pytest.mark.skipif(not HAVE_LLAMA, reason="llama.cpp runtime or test model missing")
def test_distributed_inference_correctness_failure_and_cancel(tmp_path):
    async def t():
        async with cluster(tmp_path) as cl:
            a, b = await cl.paired_workers("node-a", "node-b", ram_gb=1.2)
            cl._workers = [a, b]
            m = cl.c.model(MODEL_FILE)
            prompt = "The three primary colors are"

            # 1) single machine, no network: reference output
            from meshram import scheduler
            d = await cl.c.deploy(m, {"ctx": 512, "ubatch": 128, "assignments": [], "strategy": "native"}, native=True)
            assert d.state == "ready", d.error
            ref, _ = await d.complete(prompt, 24)
            await d.stop()

            # 2) split across two nodes (layers really on different processes, activations over TLS)
            p = scheduler.plan(m, cl.c.budgets(), ctx=512, ubatch=128, strategy="spread")
            assert p["ok"] and len(p["assignments"]) == 2
            d = await cl.c.deploy(m, p)
            assert d.state == "ready", d.error
            for part in d.parts:  # llama.cpp confirms the split: KV for exactly our layer count on each node
                kv_layers = part["actual"]["KV"] / (512 * m["kv_bytes_per_token_layer"])
                assert round(kv_layers) == part["end"] - part["start"]
            out, stats = await d.complete(prompt, 24)
            assert out == ref, f"distributed output differs:\n{out!r}\nvs native\n{ref!r}"
            assert stats["gen_tokens"] == 24 and stats["net_tx"] > 0 and stats["net_rx"] > 0
            assert all(n.status["memory"]["in_use"] > 50 * 2**20 for n in cl.c.live.values())

            # 3) cancellation: client disconnects mid-stream -> generation stops, model stays usable
            async with cl.admin.stream("POST", "/api/chat", json={
                    "messages": [{"role": "user", "content": "Count from 1 to 500."}], "max_tokens": 1000}) as r:
                async for line in r.aiter_lines():
                    if '"delta"' in line:
                        break
            await until(lambda: not d.busy, msg="generation cancelled")
            again, _ = await d.complete(prompt, 8)
            assert again == ref[:len(again)] or again

            # 4) node loss: reported as failure (layers lost), never silently "continuing"
            cl.tasks[1].cancel()  # node-b agent dies
            await b._release(None)
            await until(lambda: d.state == "failed", timeout=30, msg="failure detected")
            assert "node-b" in d.error and "lost" in d.error
    run(t())
