"""Download the llama.cpp runtime (pinned release) and optionally a test model. Stdlib only.

  python scripts/setup.py                 # CPU build for this OS + Qwen2.5-0.5B test model
  python scripts/setup.py --backend vulkan  # GPU build (Windows/Linux, any vendor) - needed for allow_gpu
  python scripts/setup.py --model 3b      # also fetch a bigger model for real 2-laptop tests
"""
import argparse
import io
import json
import os
import platform
import re
import sys
import tarfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = "b12584"  # tested with this release
MODELS = {
    "0.5b": "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
    "3b": "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
    "7b": "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf",
}


def asset_pattern(backend):
    sysname, arch = platform.system(), platform.machine().lower()
    a = "arm64" if arch in ("arm64", "aarch64") else "x64"
    if sysname == "Windows":
        return rf"bin-win-{backend}(-[\d.]+)?-{a}\.zip$"
    if sysname == "Darwin":
        return rf"bin-macos-{a}\.(zip|tar\.gz)$"
    return rf"bin-ubuntu-{'' if backend == 'cpu' else backend + r'-([\d.]+-)?'}{a}\.(zip|tar\.gz)$"


def fetch(url, dest=None):
    print("downloading", url)
    req = urllib.request.Request(url, headers={"User-Agent": "meshram-setup"})
    with urllib.request.urlopen(req) as r:
        if dest is None:
            return r.read()
        total, done = int(r.headers.get("Content-Length", 0)), 0
        with open(dest + ".part", "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done >> 20}/{total >> 20} MiB", end="", flush=True)
        os.replace(dest + ".part", dest)
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="cpu", choices=["cpu", "vulkan", "cuda"])
    ap.add_argument("--model", default="0.5b", choices=[*MODELS, "none"])
    a = ap.parse_args()
    out = os.path.join(ROOT, "runtime", "llama")
    if not any(f.startswith("llama-server") for f in (os.listdir(out) if os.path.isdir(out) else [])):
        rel = json.loads(fetch(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{TAG}"))
        pat = asset_pattern(a.backend)
        assets = [x for x in rel["assets"] if x["name"].startswith("llama-") and re.search(pat, x["name"])]
        if not assets:
            sys.exit(f"no {a.backend} build for this platform in {TAG}; build llama.cpp with -DGGML_RPC=ON "
                     f"and set MESHRAM_LLAMA_DIR")
        pick = [assets[0]]
        if a.backend == "cuda" and platform.system() == "Windows":  # CUDA runtime DLLs ship separately
            pick += [x for x in rel["assets"] if x["name"] == "cudart-" + assets[0]["name"].replace(f"-{TAG}", "")]
        os.makedirs(out, exist_ok=True)
        for asset in pick:
            data = fetch(asset["browser_download_url"])
            if asset["name"].endswith(".zip"):
                zipfile.ZipFile(io.BytesIO(data)).extractall(out)
            else:
                tarfile.open(fileobj=io.BytesIO(data)).extractall(out, filter="data")
        # some archives nest binaries in a sub-folder (build/bin): flatten
        for root, _, files in os.walk(out):
            if root != out and any(f.startswith("llama-server") for f in files):
                for f in os.listdir(root):
                    os.replace(os.path.join(root, f), os.path.join(out, f))
        if os.name != "nt":
            for f in os.listdir(out):
                os.chmod(os.path.join(out, f), 0o755)
        print("runtime ready:", out)
    else:
        print("runtime already present:", out)
    if a.model != "none":
        os.makedirs(os.path.join(ROOT, "models"), exist_ok=True)
        dest = os.path.join(ROOT, "models", MODELS[a.model].rsplit("/", 1)[1])
        if not os.path.exists(dest):
            fetch(MODELS[a.model], dest)
        print("model ready:", dest)


if __name__ == "__main__":
    main()
