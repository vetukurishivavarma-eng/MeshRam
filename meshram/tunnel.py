"""Authenticated, encrypted transport for llama.cpp RPC traffic.

ggml-rpc has no authentication or encryption, so rpc-server is only ever bound to 127.0.0.1 on the worker.
The worker exposes a TLS listener that (1) only accepts the coordinator's IP, (2) requires a per-deployment
token as the first line, then forwards bytes to the local rpc-server. The coordinator runs the mirror
image: a loopback listener that llama-server connects to, which dials the worker over TLS with the worker's
certificate pinned. Bytes are counted so the dashboard shows real tensor traffic.
"""
import asyncio
import hmac
import logging
import socket

log = logging.getLogger("meshram.tunnel")
TOKEN_LEN = 64


def _nodelay(writer):
    s = writer.get_extra_info("socket")
    if s is not None:
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


class Stats:
    def __init__(self):
        self.tx = 0  # coordinator -> worker (weights at load, activations per token)
        self.rx = 0  # worker -> coordinator (activations / results)
        self.conns = 0


async def _pipe(r, w, stats, attr):
    try:
        while True:
            b = await r.read(1 << 16)
            if not b:
                break
            setattr(stats, attr, getattr(stats, attr) + len(b))
            w.write(b)
            await w.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def _splice(r1, w1, r2, w2, stats, a_to_b, b_to_a):
    _nodelay(w1)
    _nodelay(w2)
    stats.conns += 1
    try:
        await asyncio.gather(_pipe(r1, w2, stats, a_to_b), _pipe(r2, w1, stats, b_to_a))
    finally:
        stats.conns -= 1


class TunnelServer:
    """Worker side."""

    def __init__(self, port, ssl_ctx, target_port, token, allowed_ips):
        self.port, self.ssl, self.target, self.token = port, ssl_ctx, target_port, token.encode()
        self.allowed = set(allowed_ips)
        self.stats = Stats()
        self.server = None
        self.rejected = 0

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "0.0.0.0", self.port, ssl=self.ssl)

    async def stop(self):
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    async def _handle(self, r, w):
        ip = (w.get_extra_info("peername") or ("?",))[0]
        try:
            if ip not in self.allowed:
                raise PermissionError(f"ip {ip} not allowed")
            line = await asyncio.wait_for(r.readexactly(TOKEN_LEN + 1), 5)
            if not hmac.compare_digest(line[:TOKEN_LEN], self.token):
                raise PermissionError("bad token")
            tr, tw = await asyncio.open_connection("127.0.0.1", self.target)
        except Exception as e:
            self.rejected += 1
            log.warning("tunnel rejected %s: %s", ip, e)
            w.close()
            return
        await _splice(r, w, tr, tw, self.stats, "rx", "tx")  # from worker's view: rx = from coordinator


class TunnelClient:
    """Coordinator side: 127.0.0.1:<local_port> -> TLS -> worker."""

    def __init__(self, host, port, ssl_ctx, token):
        self.host, self.port, self.ssl, self.token = host, port, ssl_ctx, token.encode()
        self.stats = Stats()
        self.server = None
        self.local_port = None
        self.errors = 0

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.local_port = self.server.sockets[0].getsockname()[1]
        return self.local_port

    async def stop(self):
        if self.server:
            self.server.close()

    async def _handle(self, r, w):
        try:
            rr, rw = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=self.ssl, server_hostname=""), 10)
            rw.write(self.token + b"\n")
            await rw.drain()
        except Exception as e:
            self.errors += 1
            log.warning("tunnel to %s:%s failed: %s", self.host, self.port, e)
            w.close()
            return
        await _splice(r, w, rr, rw, self.stats, "tx", "rx")
