# Two-laptop test guide (Windows: same Wi-Fi, or different mobile networks via Tailscale)

Goal: run one model whose layers live on two physical laptops, then measure it.

## 0. Prerequisites (both laptops)
* Windows 10/11, Python 3.10+ (`python --version`), same Wi-Fi network marked **Private** (Settings → Network → Wi-Fi → Private).
* Copy this `meshram` folder to both laptops (USB or `git clone`).
* Coordinator laptop only: Node.js 18+ (to build the dashboard once).

## 1. Install
On **both** laptops, in the meshram folder:
```bat
install.bat
```
This installs Python deps, downloads the pinned llama.cpp CPU build into `runtime\llama`, and downloads the 0.5B test model.
To test the real point of the project (a model too big for one laptop's limit), also run on the coordinator:
```bat
python scripts\setup.py --model 3b       :: Qwen2.5-3B Q4_K_M, ~2 GB
```
Only the coordinator needs the model file. Workers receive their layers over the tunnel at load time.

## 2. Firewall (Private networks only)
Run in an **Administrator** terminal.

Coordinator (laptop A):
```bat
netsh advfirewall firewall add rule name="MeshRAM node API" dir=in action=allow protocol=TCP localport=7861 profile=private
```
Worker (laptop B):
```bat
netsh advfirewall firewall add rule name="MeshRAM tunnel" dir=in action=allow protocol=TCP localport=50052 profile=private
netsh advfirewall firewall add rule name="MeshRAM discovery" dir=in action=allow protocol=UDP localport=7862 profile=private
```
(If Windows pops up "allow Python", choose **Private** only.)

## 3. Start
Laptop A (coordinator, also contributes its own RAM):
```bat
start-coordinator.bat --max-ram-gb 2
```
It prints the pairing code, fingerprint and LAN IP. Open http://127.0.0.1:7860.

Laptop B (worker):
```bat
start-worker.bat --coordinator <A's IP> --code <pairing code> --max-ram-gb 2
```
(Omit `--coordinator` to auto-discover via UDP broadcast.) B prints both fingerprints.

## 4. Approve
In A's dashboard an **approval needed** banner appears. Check that its fingerprint matches what laptop B printed, then click
**Approve**. B shows up under *Computers* with live RAM/CPU/RTT. Click **Network test** to measure the real link.

## 5. Prove the cluster runs what one laptop can't
1. Under **Models & placement** pick `qwen2.5-3b-instruct-q4_k_m.gguf`, context 2048.
2. With both laptops at `--max-ram-gb 2`, the plan shows **"no single node can hold it"** and a layer bar split
   across A and B. If it says it fits on one node, lower `--max-ram-gb` (or the slider on B's page,
   http://127.0.0.1:7870) until it doesn't.
3. Click **Load model on cluster**. Watch *Actual allocation*: llama.cpp's own per-node buffer sizes.
4. Chat in **Inference**. Tokens stream, tok/s shows live, and the cluster map animates real tunnel traffic.
5. **Benchmarks → Run benchmark**: native (A alone, if A can spare the RAM), single node (skipped if nothing fits),
   distributed. The verdict line says whether the cluster was faster, slower-but-capable, or network/CPU limited.

## 6. Failure checks
* Close B's window while a model is loaded. Within ~10 s the dashboard says `node … disconnected – layers x–y lost` and
  marks the deployment **failed**. Restart B: it reconnects by itself (no re-pairing).
* On B, open http://127.0.0.1:7870 and switch on "Only while charging", then unplug. B pauses, releases its memory, and the
  coordinator reports why.
* Click **Remove** on B's card. B's credentials stop working and it must pair again.

## Variant: laptops on different networks (each on its own mobile hotspot)
1. Install Tailscale on both (https://tailscale.com/download), sign in with the **same account** on both.
2. On the coordinator: `tailscale ip -4` gives an address like `100.69.248.116`.
3. Worker: `start-worker.bat --coordinator 100.69.248.116 --code <code>`. Firewall rules from step 2 still apply
   (Tailscale traffic arrives on its own adapter; if Windows classifies it as Public, add `profile=any` to the two TCP rules).
4. Steps 4–6 are unchanged. Expect 30–100 ms RTT on each card, a latency warning, low tok/s, and a "Sent at load" figure
   that is drawn from your mobile data.
5. Check the path is direct: `tailscale ping <other machine>`. "via DERP" means relayed (slower). Direct is best.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Worker: `coordinator unreachable` | firewall rule on A (TCP 7861), Wi-Fi set to Private, correct IP |
| Load fails `tunnel to …:50052 failed` | firewall rule on B (TCP 50052) |
| `refused: … GiB requested … allowed` | that laptop's owner limit or free RAM is too small: raise `--max-ram-gb` or close apps |
| Very slow tok/s | expected on Wi-Fi: every token makes network round trips to each worker. Use 5 GHz Wi-Fi or Ethernet, fewer nodes ("Fewest nodes" placement) |
| Dashboard says "not built" | on A: `cd web && npm install && npm run build` |
