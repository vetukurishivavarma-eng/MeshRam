"""python -m meshram coordinator | worker   (see README.md)"""
import argparse
import asyncio
import logging
import os
import secrets
import sys
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import coordinator as coord_mod
from . import discovery, policy, tls
from .worker import Worker

HOME = os.path.join(os.path.expanduser("~"), ".meshram")


def worker_local_app(w: Worker):
    """Owner's own page on the worker machine: see exactly what is shared, change the limits."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(req: Request, call_next):
        if (req.headers.get("host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return JSONResponse({"error": "forbidden host"}, 403)
        if req.method != "GET" and req.headers.get("x-meshram") != "1":
            return JSONResponse({"error": "missing X-MeshRAM header"}, 403)
        return await call_next(req)

    @app.get("/api/mode")
    async def mode():
        return {"mode": "worker"}

    @app.get("/api/local")
    async def local():
        return w.status()

    @app.post("/api/local/policy")
    async def set_policy(req: Request):
        try:
            w.set_policy(await req.json())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return w.status()

    @app.post("/api/local/stop")
    async def stop():
        await w._release("stopped by owner")
        return {"ok": True}

    coord_mod._static(app)
    return app


def _policy_overrides(a):
    o = {}
    for flag, key in (("max_ram_gb", "max_ram_gb"), ("max_cpu", "max_cpu_pct"), ("max_temp", "max_temp_c"),
                      ("tunnel_port", "tunnel_port")):
        if getattr(a, flag) is not None:
            o[key] = getattr(a, flag)
    for flag, key in (("allow_gpu", "allow_gpu"), ("only_idle", "only_when_idle"), ("only_charging", "only_when_charging")):
        if getattr(a, flag):
            o[key] = True
    return o


def _apply(w, o):
    if o:
        w.set_policy(w.policy | o)


def _server(app, host, port, **kw):
    return uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", **kw))


async def run_coordinator(a):
    d = a.data or os.path.join(HOME, "coordinator")
    c = coord_mod.Coordinator(d, node_port=a.port)
    print(f"\n  MeshRAM coordinator")
    print(f"  Dashboard:      http://127.0.0.1:{a.ui_port}   (this computer only)")
    print(f"  Node API:       https://<this-ip>:{a.port}    LAN IPs: {', '.join(coord_mod._lan_ips()) or '-'}")
    print(f"  Pairing code:   {c.st.data['pairing_code']}")
    print(f"  Fingerprint:    {tls.short_fp(c.fp)}\n")
    tasks = [
        _server(coord_mod.admin_app(c), "127.0.0.1", a.ui_port).serve(),
        _server(coord_mod.node_app(c), "0.0.0.0", a.port, ssl_certfile=c.crt, ssl_keyfile=c.key).serve(),
        c.housekeeping(),
        discovery.beacon({"meshram": 1, "name": os.environ.get("COMPUTERNAME") or "coordinator", "port": a.port,
                          "fp": tls.short_fp(c.fp)}),
    ]
    if not a.no_contribute:
        w = Worker(os.path.join(d, "local-worker"), name=a.name, coordinator=f"127.0.0.1:{a.port}")
        _apply(w, _policy_overrides(a))
        register_local(c, w, a.port)
        w.extra_pids = lambda: [c.deployment.proc.pid] if c.deployment and c.deployment.proc             and c.deployment.proc.poll() is None else []
        tasks += [w.run(), _server(worker_local_app(w), "127.0.0.1", a.worker_ui_port).serve()]
        print(f"  This computer also contributes (limits: http://127.0.0.1:{a.worker_ui_port}); --no-contribute to opt out\n")
    await asyncio.gather(*tasks)


def register_local(c, w, port):
    """The coordinator's own machine is trusted directly (same owner, same process) - no network pairing."""
    nid = w.st.data["node_id"]
    rec = c.st.data["nodes"].get(nid)
    cw = w.st.data.get("coordinator") or {}
    if rec and cw.get("token") and rec["token_hash"] == coord_mod._h(cw["token"]):
        return
    token = secrets.token_hex(32)
    c.st.data["nodes"][nid] = {"name": w.st.data["name"] + " (coordinator)", "token_hash": coord_mod._h(token),
                               "cert_pem": w.cert_pem, "fp": tls.short_fp(w.fp), "fp_full": w.fp,
                               "approved_at": time.time(), "local": True, "last_ip": "127.0.0.1"}
    c.st.save()
    w.st.data["coordinator"] = {"addr": f"127.0.0.1:{port}", "cert_pem": open(c.crt).read(), "token": token}
    w.st.save()


async def run_worker(a):
    d = a.data or os.path.join(HOME, "worker")
    w = Worker(d, name=a.name, coordinator=a.coordinator, pair_code=a.code)
    _apply(w, _policy_overrides(a))
    s = w.status()
    m = s["memory"]
    print(f"\n  MeshRAM worker '{s['name']}'  fingerprint {s['fingerprint']}")
    print(f"  RAM total {m['total'] / 2**30:.1f} GiB | reserved for you {m['host_reserve'] / 2**30:.1f} GiB | "
          f"cluster cap {m['contribution_cap'] / 2**30:.1f} GiB")
    print(f"  Your controls: http://127.0.0.1:{a.ui_port}\n")
    await asyncio.gather(w.run(), _server(worker_local_app(w), "127.0.0.1", a.ui_port).serve())


def main():
    p = argparse.ArgumentParser(prog="meshram")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("coordinator", "worker"):
        s = sub.add_parser(name)
        s.add_argument("--data", help="state directory (default ~/.meshram/<role>)")
        s.add_argument("--name", help="display name for this computer")
        s.add_argument("--max-ram-gb", type=float)
        s.add_argument("--max-cpu", type=int, help="max CPU %% given to cluster work")
        s.add_argument("--max-temp", type=int)
        s.add_argument("--tunnel-port", type=int)
        s.add_argument("--allow-gpu", action="store_true")
        s.add_argument("--only-idle", action="store_true")
        s.add_argument("--only-charging", action="store_true")
        s.add_argument("-v", "--verbose", action="store_true")
        if name == "coordinator":
            s.add_argument("--port", type=int, default=7861)
            s.add_argument("--ui-port", type=int, default=7860)
            s.add_argument("--worker-ui-port", type=int, default=7870)
            s.add_argument("--no-contribute", action="store_true", help="don't run a local worker")
        else:
            s.add_argument("--coordinator", help="coordinator IP[:port]; omit to auto-discover on the LAN")
            s.add_argument("--code", help="pairing code (asked interactively if omitted)")
            s.add_argument("--ui-port", type=int, default=7870)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    if a.max_ram_gb is not None or a.max_cpu is not None:
        policy.validate(_policy_overrides(a))  # fail fast on bad flags
    try:
        asyncio.run(run_coordinator(a) if a.cmd == "coordinator" else run_worker(a))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
