# MeshRAM AI — computers that work together as one AI machine

MeshRAM turns several ordinary computers on one LAN into a single cluster that can run an LLM too large for any
one of them. It doesn't fake "pooled RAM". It **splits the model's transformer layers across machines** and moves the
activations between them for every token, using llama.cpp's RPC backend underneath. On top of that it adds pairing,
approval, TLS, OS-enforced memory limits, owner controls, scheduling, failure handling, benchmarks and a dashboard.

```
            ┌──────────────── coordinator machine ────────────────┐
 browser ──▶│ dashboard  http://127.0.0.1:7860   (loopback only)   │
            │ node API   https://0.0.0.0:7861    (TLS, LAN only)   │◀── control WebSocket (heartbeat, metrics, whitelisted commands)
            │ llama-server: embeddings + output head, drives stages │
            │      │ 127.0.0.1 ─▶ TunnelClient ══TLS, token, pinned cert══╗
            └──────┼──────────────────────────────────────────────┘     ║
                   ▼                                                     ▼
   worker A: TunnelServer :50052 ─▶ rpc-server (127.0.0.1) layers 0–18  [Job Object memory limit]
   worker B: TunnelServer :50052 ─▶ rpc-server (127.0.0.1) layers 19–23 [Job Object memory limit]
```

## What actually works (verified on this machine, 2026-11-20)

| | Result |
|---|---|
| Distributed inference | Qwen2.5-0.5B Q4_K_M split 19/5 layers across two worker agents; llama.cpp's own allocation log confirms the split (per-node KV = exact layer count) |
| Correctness | Greedy output of the 2-node run is **byte-identical** to the single-machine run (automated test) |
| Primary success metric | Each node capped at 0.25 GiB (OS-enforced). Model needs 307 MiB → **no single node can hold it; the cluster runs it** |
| Efficiency (negative result, shown as-is) | 18.2 tok/s distributed vs 31.5 tok/s native (0.58×), classed *latency-bound*: nodes used < 50 % of their allowed CPU, 32 KB moved per token |
| Failure | Killing a worker mid-deployment → "node laptop-B disconnected – layers 0-11 lost", generation reported lost, auto re-plan onto remaining nodes when they can hold the model |
| Memory safety | A child under a 200 MiB Job Object limit cannot commit 600 MiB (test). rpc-server dies with its agent (kill-on-close) |
| Tests | 27 automated tests (unit + real TLS/WebSocket cluster + real inference) – `python -m pytest tests` |

The numbers above come from two agents on one laptop (i5-10210U, 8 GB). Over real Wi-Fi, expect latency to matter
much more. See [TESTING-2-LAPTOPS.md](TESTING-2-LAPTOPS.md).

## Quick start

```bat
install.bat                          :: pip deps + llama.cpp runtime + 0.5B test model + dashboard build
start-coordinator.bat                :: prints dashboard URL, pairing code, fingerprint
```
On each other computer:
```bat
install.bat
start-worker.bat --coordinator 192.168.1.31 --code 123456 --max-ram-gb 4
```
Then open http://127.0.0.1:7860 on the coordinator, compare fingerprints, click **Approve**, and pick a model under
**Models & placement**. Each worker owner gets their own control page at http://127.0.0.1:7870.

Linux/macOS: `pip install -r requirements.txt && python scripts/setup.py && python -m meshram coordinator|worker ...`
(cd `web` and run `npm install && npm run build` on the coordinator). Bigger models: `python scripts/setup.py --model 3b` or `--model 7b`,
or drop any `.gguf` into `models/`.

---

## Computers on different networks (mobile data, different homes)

Mobile networks put every device behind carrier-grade NAT, so neither side can accept an incoming connection, and
UDP discovery can't cross networks. MeshRAM doesn't reinvent NAT traversal. It runs over an **overlay VPN**:

* **Tailscale** (free personal plan, WireGuard-based): install it on every computer and log in with the same
  account. Each machine gets a stable `100.x.y.z` address that works across any networks (direct when NAT allows,
  via encrypted relay otherwise). Start workers with `--coordinator <coordinator's 100.x IP>`.
* **Headscale** (self-hosted Tailscale control server) if no third party should be involved, or ZeroTier /
  plain WireGuard via a cheap VPS: all work, since MeshRAM only needs IP reachability.

MeshRAM accepts LAN, loopback and overlay ranges (`100.64.0.0/10`, private IPv6) and still **rejects public internet
addresses**. Its own TLS + pairing + tokens run inside the VPN's encryption. Verified 2026-11-20: pairing, control
channel and layer tunnel over this machine's Tailscale address.

**Expect it to be slow.** Mobile round trips are 30–100 ms instead of ~3 ms on Wi-Fi, and every generated token waits on
round trips to each worker, so plan for **single-digit tokens/second, often ~1–3 tok/s**. Relayed Tailscale paths are slower still.
**Each model load sends that worker's layers over the link** (for example 187 MiB for 19 layers of the 0.5B model, ~1–2 GB for
a 3B/7B share), which counts against data caps. The dashboard shows "Sent at load" per node and flags links over 25 ms.
Load once and keep the model loaded, use "Fewest nodes" placement, and put the coordinator on the best connection.

## 1. Feasibility analysis (done before building)

* **Memory can't be pooled across machines.** Another computer's RAM is about 10,000× slower to reach than local
  RAM (Wi-Fi ≈ 1–5 ms RTT vs ~100 ns). Any design that pages arbitrary memory over the network is unusable for LLMs.
* **What does work is pipeline (layer) parallelism.** A transformer is a stack of layers. Node A keeps layers 0–k
  *resident in its own RAM*, computes them, and passes a small hidden-state vector on. Weights never move after load.
  Per token, only `n_embd` floats cross each hop (3.5 KB for Qwen2.5-0.5B, 14 KB for a 7B model).
* **Cost:** a single request visits the stages sequentially, so the cluster adds latency instead of parallel speed: every
  extra node adds network round trips to every token. The cluster is therefore **slower than a single machine that could
  fit the model**, and its value is running a model that *no single machine can fit*. The benchmark page measures
  exactly this and says so.

## 2. Inference engine: what MeshRAM implements vs what llama.cpp does

**llama.cpp RPC backend (`ggml-rpc`)** was checked and confirmed to support the required configuration:
`rpc-server` exposes a machine's CPU (or GPU) as a remote ggml device, and `llama-server --rpc a,b -ts x,y` places
whole layers on those devices and ships activations between them. MeshRAM uses it rather than reinventing
tensor kernels, quantization and tokenization.

| MeshRAM implements | llama.cpp provides |
|---|---|
| Discovery, pairing, approval, auth, revocation | Quantized kernels (Q4_K_M etc.) |
| Encrypted, authenticated tunnel for RPC traffic | Layer offload to RPC devices (`-ts`, `-dev`) |
| GGUF header parser → exact per-layer bytes | Activation transfer per token |
| Placement planner (min-nodes / spread, memory estimates) | Token generation, sampling, streaming API |
| OS memory limits, owner policy, pause/release | KV cache, compute buffers |
| Exact `-ts` math (half-layer offsets) so splits land on our boundaries | |
| Output-head-on-host optimisation (`-ot output.weight=CPU`): 16 MB → 93 KB return traffic per reply | |
| Health monitoring, failure detection, re-planning, benchmarks, dashboard | |

## 3. Limitations (Windows and the runtime)

* **ggml-rpc has no authentication or encryption.** rpc-server is never exposed. It binds to 127.0.0.1, and a worker only
  reaches it through MeshRAM's TLS tunnel, which checks the coordinator's IP and a fresh 256-bit per-deployment token.
* **All tensor traffic goes through the coordinator** (ggml-rpc has no direct device-to-device copy between servers),
  so the topology is a star, and the dashboard draws it that way.
* **The coordinator is required for inference**: it hosts the embeddings, output head and sampler. Workers compute their
  layers independently, but if the coordinator dies, the running generation is lost. Workers notice, release memory
  after 15 s, and reconnect when it returns. Node state persists across coordinator restarts.
* **There's no mid-generation recovery.** llama.cpp can't move a running KV cache. A lost node fails the generation
  (reported, never hidden), and MeshRAM re-plans onto the remaining nodes only if they can hold the whole model.
* **The rpc weight cache** (`rpc-server -c`) only caches tensors > 10 MB (llama.cpp rule), so small models re-send
  weights on each load. 7B-class models have per-layer tensors above that and *are* cached on the worker's disk.
* **Windows readings:** CPU temperature comes from ACPI WMI, which usually needs admin; without it, the reading shows as "n/a on this OS" and
  isn't guessed. Integrated-GPU "VRAM" is driver-reported shared memory, and is labelled as such. Idle time uses
  `GetLastInputInfo`. Linux/macOS idle detection isn't implemented, so "only when idle" pauses there with that reason.
* **GPU contribution is untested.** With `--allow-gpu` and a Vulkan/CUDA runtime (`setup.py --backend vulkan`), rpc-server is
  pinned to the first GPU device. The planner still budgets by the owner's RAM limit and doesn't model VRAM separately.
  Not exercised on real GPU hardware.
* **The Python tunnel** costs some throughput, which matters for load time, not per-token speed. The control-channel network test
  measures Python TLS WebSocket throughput (~70–80 Mbit/s on loopback here), which is a lower bound on the raw link.
* **Memory limits** apply to *commit* (Windows Job Object) or *address space* (Linux RLIMIT_AS, with slack); the agent
  additionally watches RSS. Disk swap is never used or reported as shared memory.

## 4. Architecture & code map

```
meshram/
  __main__.py     CLI: coordinator | worker; worker's loopback owner page
  coordinator.py  registry, pairing/approval, control channel, heartbeats, budgets, deploy, benchmarks, HTTP APIs
  worker.py       agent: pairing, reconnecting control loop, COMMAND WHITELIST, owner protection monitor
  engine.py       workload engines; LlmPipeline = llama.cpp layer split (start/stop/check/chat/complete)
  scheduler.py    placement planner + exact tensor-split math
  policy.py       owner policy, host reserve, admission, pause rules (pure functions)
  runtime.py      llama.cpp process control, Job Object / RLIMIT memory limits, log parser
  tunnel.py       TLS + token + IP-checked byte pipe with traffic counters
  tls.py          per-machine self-signed identity, cert pinning, fingerprints
  hw.py           psutil/WMI/nvidia-smi readings (None when unknown)
  gguf.py         GGUF header reader (no weights loaded)
  discovery.py    UDP broadcast beacon / listener
web/              React + TypeScript + Tailwind dashboard (same bundle serves the worker owner page)
tests/            unit + integration tests
scripts/setup.py  downloads pinned llama.cpp build + models
```

**Stack choices.** Python/FastAPI/asyncio for the control plane: psutil, ctypes for Job Objects, and good async
networking, with the heavy compute in llama.cpp's C++. WebSockets carry control traffic (JSON) and network tests
(binary). Raw TLS byte pipes carry tensors, because ggml-rpc is a raw TCP protocol. React/TS/Tailwind is used for
the UI. **No Tauri yet:** the dashboard is served by the coordinator itself on loopback, so there's nothing extra to
install or sign, and a Tauri shell would only wrap the same page. Add it when an installer/tray icon is wanted.

## 5. Security model

* **Joining** needs the pairing code, then the worker sends its certificate. Both screens show fingerprints, and the human
  approves only if they match. Rejected or expired requests are discarded. A node ID or certificate can't register twice.
* **Credentials:** a 256-bit token per node, stored hashed (SHA-256) on the coordinator. The coordinator certificate is
  pinned by the worker, and each worker certificate is pinned by the coordinator for tunnels. No passwords or keys live in
  code; identities are generated on first run.
* **Exposure:** the dashboard and owner page bind to 127.0.0.1, reject foreign `Host` headers (DNS rebinding), and
  require an `X-MeshRAM` header on writes (blocks cross-site requests). The node API rejects source IPs outside LAN/overlay-VPN ranges. Pairing is rate-limited.
* **Workers can't be told to run arbitrary things.** The only commands are `rpc_start`, `rpc_stop`, `alloc_test`
  and `net_test`, with type/range-checked arguments, executed inside the owner's policy. There's no shell, file write or arbitrary process.
* **Isolation:** rpc-server runs at below-normal priority, under an OS memory limit, killed with its agent.
* **Revocation:** **Remove** deletes the token, drops the connection and stops the node's model memory. The worker must re-pair.

## 6. Resource contribution

Defaults: the owner keeps `max(2 GiB, 25 %)` of RAM. The cluster may use at most `min(max_ram_gb, total − reserve)`,
and never pushes available RAM below `max(1 GiB, 10 %)`. CPU limit → compute thread count, at below-normal priority.
Pause rules (release memory immediately): on battery (if charging-only), owner active (if idle-only), temperature limit,
host low on memory, and owner CPU > threshold for 6 s (cluster load excluded). Every figure is live from the OS.

## 7. Future workloads (milestones 6–7: designed, not implemented)

`engine.py` defines the contract: an engine takes a placement, acquires worker resources only through whitelisted
commands, and reports failure honestly. Image/video models don't split like LLMs:

* **Image generation (diffusion):** the denoiser is too small and too iterative for layer splitting. Better to distribute *independent
  jobs* (different seeds/prompts per node), or tiles for upscaling (Real-ESRGAN/tile diffusion) with overlap blending. Engine: `image-tiles` using
  stable-diffusion.cpp per node; new worker command `run_tiles` with a fixed model allow-list.
* **Video:** distribute frame ranges (enhancement/upscaling/interpolation) and stitch with ffmpeg on the coordinator;
  generative video models need the whole latent at once, so they're feasible only as multi-GPU tensor parallel on
  fast links, which is out of scope for Wi-Fi laptops.
* **Embeddings / batch jobs:** embarrassingly parallel. Shard the input list across nodes, each with a full small model.

## 8. Milestone status

| # | Status |
|---|---|
| 1 Setup, dashboard, agent, monitoring, discovery, manual connect, auth, live metrics | done |
| 2 Contribution controls, memory safety, task system, network benchmark, memory allocation test | done |
| 3 Real local inference + RAM measurement | done (native mode, per-device buffers parsed from llama.cpp) |
| 4 Distributed layer partitioning, multi-node inference | done on two agents/one machine; **2-laptop run is the user's step** ([guide](TESTING-2-LAPTOPS.md)) |
| 5 Scheduler, auto selection, placement, optimisation | done: budget-aware min-nodes/spread planner, exact splits, head-on-host |
| 6–7 Image/video | researched/designed above, not built |
