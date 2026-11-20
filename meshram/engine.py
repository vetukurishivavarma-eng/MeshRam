"""Workload engines.

Every workload kind decides for itself how it is split, because AI models do not all split the same way:
  llm-pipeline  : contiguous transformer layers per node, activations flow node->node per token  (implemented)
  image-tiles   : independent tiles / denoise batches per node, no inter-node activations      (future)
  video-frames  : independent frame ranges per node, stitched at the end                         (future)
An engine gets placements from the coordinator, asks workers for resources ONLY via the whitelisted worker
commands, and must report failure honestly when a required node disappears.
"""
import asyncio
import json
import os
import secrets
import time

import httpx

from . import runtime, tls
from .tunnel import TunnelClient


class LlmPipeline:
    """llama.cpp layer-split inference: llama-server on the coordinator drives ggml-rpc devices on workers."""
    kind = "llm-pipeline"

    def __init__(self, coord, model, plan, log_dir, native=False):
        self.c, self.model, self.plan = coord, model, plan
        self.id = secrets.token_hex(4)
        self.native = native  # True = single machine, no RPC, no network (baseline)
        self.state, self.error = "starting", None
        self.parts = []  # [{node_id, name, start, end, ip, tunnel, local_port}]
        self.proc, self.port = None, None
        self.log_path = os.path.join(log_dir, f"llama-server-{self.id}.log")
        self.t0 = time.time()
        self.load_seconds = None
        self.buffers = {}
        self.last_run = None
        self.busy = False

    def summary(self):
        return {"id": self.id, "kind": self.kind, "state": self.state, "error": self.error,
                "model": self.model["file"], "native": self.native, "ctx": self.plan["ctx"],
                "load_seconds": self.load_seconds, "started": self.t0, "busy": self.busy,
                "tensor_split": self.plan.get("tensor_split"),
                "parts": [{k: p.get(k) for k in ("node_id", "name", "start", "end", "est_bytes", "actual", "load_bytes")}
                          for p in self.parts],
                "host_actual": self.buffers.get("host"), "last_run": self.last_run}

    async def start(self):
        try:
            rpc_ports = []
            if not self.native:
                for a in self.plan["assignments"]:
                    node = self.c.live.get(a["node_id"])
                    if not node:
                        raise RuntimeError(f"node {a['name']} is offline")
                    token = secrets.token_hex(32)
                    r = await self.c.command(a["node_id"], "rpc_start",
                                             {"mem_bytes": a["est_bytes"], "token": token,
                                              "layers": f"{a['start']}-{a['end'] - 1}"}, timeout=30)
                    ctx = tls.pinned_client_ctx(self.c.st.data["nodes"][a["node_id"]]["cert_pem"])
                    t = TunnelClient(node.ip, r["tunnel_port"], ctx, token)
                    lp = await t.start()
                    self.parts.append({**a, "ip": node.ip, "tunnel": t, "local_port": lp, "actual": None})
                    rpc_ports.append(lp)
            self.port = runtime.free_port()
            threads = os.cpu_count() // 2 or 1
            self.proc = runtime.start_llama_server(self.model["path"], rpc_ports, self.plan.get("tensor_split"),
                                                   self.plan.get("head", "host") == "host",
                                                   self.plan["ctx"], self.plan["ubatch"], self.port, threads,
                                                   self.log_path)
            self.state = "loading"
            async with httpx.AsyncClient(timeout=2) as h:
                while True:
                    if self.proc.poll() is not None:
                        raise RuntimeError(f"llama-server exited during load: {self._log_tail()}")
                    if self.state != "loading":
                        return  # stopped while loading
                    try:
                        if (await h.get(f"http://127.0.0.1:{self.port}/health")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.time() - self.t0 > 900:
                        raise RuntimeError("model load timed out after 15 min")
                    await asyncio.sleep(0.5)
            self.load_seconds = round(time.time() - self.t0, 2)
            self.buffers = runtime.parse_buffers(open(self.log_path, errors="replace").read())
            for p in self.parts:
                p["actual"] = self.buffers.get(str(p["local_port"]))
                p["load_bytes"] = p["tunnel"].stats.tx  # weights uploaded to this node (counts against data caps)
            self.state = "ready"
            self.c.event(f"model {self.model['file']} ready in {self.load_seconds}s on "
                         + (", ".join(f"{p['name']}[{p['start']}-{p['end'] - 1}]" for p in self.parts) or "local CPU"))
        except Exception as e:
            await self.stop(f"failed: {e}", failed=True)

    def _log_tail(self, n=6):
        try:
            lines = [l for l in open(self.log_path, errors="replace").read().splitlines() if l.strip()]
            return " | ".join(lines[-n:])[-800:]
        except OSError:
            return ""

    async def stop(self, reason="stopped", failed=False):
        if self.state in ("stopped", "failed"):
            return
        self.state, self.error = ("failed", reason) if failed else ("stopped", None)
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        for p in self.parts:
            await p["tunnel"].stop()
            if p["node_id"] in self.c.live:
                try:
                    await self.c.command(p["node_id"], "rpc_stop", {}, timeout=10)
                except Exception:
                    pass
        self.c.event(f"deployment {self.id}: {reason}")

    def check(self):
        """Called periodically. Returns a failure reason if a required component is gone."""
        if self.state not in ("ready", "loading"):
            return None
        if self.proc and self.proc.poll() is not None:
            return f"inference process exited (code {self.proc.returncode}): {self._log_tail(3)}"
        for p in self.parts:
            n = self.c.live.get(p["node_id"])
            if not n:
                return f"node {p['name']} disconnected - layers {p['start']}-{p['end'] - 1} lost"
            if (n.status or {}).get("paused"):
                return f"node {p['name']} paused by owner ({n.status['paused']}) - layers {p['start']}-{p['end'] - 1} released"
        return None

    def traffic(self):
        return {p["node_id"]: {"tx": p["tunnel"].stats.tx, "rx": p["tunnel"].stats.rx} for p in self.parts}

    # ---------- inference ----------
    async def chat(self, messages, max_tokens, temperature):
        """Yields SSE lines for the dashboard: {"delta"} chunks, then {"done", timings}."""
        if self.state != "ready":
            yield _sse({"error": f"model not ready ({self.state})"})
            return
        self.busy = True
        t0 = time.time()
        traffic0 = self.traffic()
        body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": True,
                "timings_per_token": False}
        n, timings = 0, None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5)) as h:
                async with h.stream("POST", f"http://127.0.0.1:{self.port}/v1/chat/completions", json=body) as r:
                    if r.status_code != 200:
                        yield _sse({"error": f"llama-server HTTP {r.status_code}: {(await r.aread())[:300]!r}"})
                        return
                    async for line in r.aiter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        j = json.loads(line[6:])
                        if j.get("timings"):
                            timings = j["timings"]
                        for ch in j.get("choices", []):
                            d = (ch.get("delta") or {}).get("content")
                            if d:
                                n += 1
                                yield _sse({"delta": d})
        except httpx.HTTPError as e:
            reason = self.check()
            yield _sse({"error": reason or f"inference failed: {e}"})
            return
        finally:
            self.busy = False
        self.last_run = self._run_stats(t0, timings, traffic0)
        yield _sse({"done": True, "stats": self.last_run})

    async def complete(self, prompt, n_predict):
        """Deterministic (greedy) completion for benchmarks + correctness comparison."""
        self.busy = True
        t0, traffic0 = time.time(), self.traffic()
        try:
            async with httpx.AsyncClient(timeout=900) as h:
                r = await h.post(f"http://127.0.0.1:{self.port}/completion", json={
                    "prompt": prompt, "n_predict": n_predict, "temperature": 0, "top_k": 1, "seed": 1,
                    "cache_prompt": False, "ignore_eos": True})
                r.raise_for_status()
                j = r.json()
        finally:
            self.busy = False
        self.last_run = self._run_stats(t0, j.get("timings"), traffic0)
        return j.get("content", ""), self.last_run

    def _run_stats(self, t0, timings, traffic0):
        t1 = self.traffic()
        tx = sum(v["tx"] - traffic0.get(k, {}).get("tx", 0) for k, v in t1.items())
        rx = sum(v["rx"] - traffic0.get(k, {}).get("rx", 0) for k, v in t1.items())
        t = timings or {}
        gen = t.get("predicted_n") or 0
        return {"wall_seconds": round(time.time() - t0, 3),
                "prompt_tokens": t.get("prompt_n"), "prompt_tps": _r(t.get("prompt_per_second")),
                "gen_tokens": gen, "gen_tps": _r(t.get("predicted_per_second")),
                "gen_ms": _r(t.get("predicted_ms")), "net_tx": tx, "net_rx": rx,
                "net_bytes_per_token": int((tx + rx) / gen) if gen else None, "finished": time.time()}


def _r(x):
    return round(x, 2) if isinstance(x, (int, float)) else None


def _sse(o):
    return f"data: {json.dumps(o)}\n\n"

