"""llama.cpp process control: rpc-server (worker) under an OS-enforced memory limit, llama-server (coordinator)."""
import os
import re
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = ".exe" if sys.platform == "win32" else ""


def llama_dir():
    return os.environ.get("MESHRAM_LLAMA_DIR") or os.path.join(ROOT, "runtime", "llama")


def binary(*names):
    for n in names:
        p = os.path.join(llama_dir(), n + EXE)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"{names[0]} not found in {llama_dir()} (run scripts/get_llama.py or set MESHRAM_LLAMA_DIR)")


def has_gpu_backend():
    try:
        files = os.listdir(llama_dir())
    except OSError:
        return False
    return any(re.match(r"(lib)?ggml-(cuda|vulkan|metal|hip|sycl)", f) for f in files)


def gpu_device():
    """First non-CPU ggml device name (e.g. Vulkan0, CUDA0) as listed by this llama.cpp build."""
    try:
        out = subprocess.run([binary("llama-server"), "--list-devices"], capture_output=True, text=True,
                             timeout=30, creationflags=0x08000000 if sys.platform == "win32" else 0).stdout
    except Exception:
        return None
    for m in re.finditer(r"^\s+([A-Za-z]+\d+):", out, re.M):
        if not m.group(1).startswith(("CPU", "RPC")):
            return m.group(1)
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- OS memory limit ----------
class _MemLimit:
    """Windows: Job Object with a per-process commit limit (allocations beyond it fail) + kill-on-close,
    so a crashed agent can never leave model memory behind. POSIX: RLIMIT_AS via preexec."""

    def __init__(self, limit_bytes):
        self.limit = int(limit_bytes)
        self.job = None

    def preexec(self):
        if sys.platform == "win32":
            return None
        import resource
        if not self.limit:
            return None
        limit = self.limit + 768 * 2**20  # address-space slack for thread stacks / libs (AS != RSS)

        def f():
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            os.nice(10)
        return f

    def attach(self, pid):
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes as wt

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.OpenProcess.restype = wt.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.Basic.LimitFlags = 0x2000 | (0x100 if self.limit else 0)  # KILL_ON_JOB_CLOSE | PROCESS_MEMORY
        info.ProcessMemoryLimit = self.limit
        if not k32.SetInformationJobObject(wt.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(f"SetInformationJobObject failed: {ctypes.get_last_error()}")
        h = k32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not k32.AssignProcessToJobObject(wt.HANDLE(job), wt.HANDLE(h)):
            raise OSError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")
        k32.CloseHandle(wt.HANDLE(h))
        self.job = job  # keep handle open: closing it kills the process


def _flags():
    return (0x00004000 | 0x08000000) if sys.platform == "win32" else 0  # BELOW_NORMAL priority, no window


def start_rpc_server(port, threads, mem_limit, cache_dir, cpu_only=True, log_path=None):
    args = [binary("ggml-rpc-server", "rpc-server"), "-H", "127.0.0.1", "-p", str(port), "-t", str(threads), "-c"]
    if has_gpu_backend():  # always exactly one device per node: the planner maps one -ts slot per node
        args += ["-d", (None if cpu_only else gpu_device()) or "CPU"]
    env = dict(os.environ, LLAMA_CACHE=cache_dir)
    lim = _MemLimit(mem_limit)
    logf = open(log_path, "wb") if log_path else subprocess.DEVNULL
    p = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, env=env, creationflags=_flags(),
                         preexec_fn=lim.preexec())
    try:
        lim.attach(p.pid)
    except Exception:
        p.kill()
        raise
    p._meshram_limit = lim  # keep job handle alive with the process object
    return p


def start_llama_server(model_path, rpc_ports, tensor_split, head_on_host, ctx, ubatch, port, threads, log_path):
    args = [binary("llama-server"), "-m", model_path, "--host", "127.0.0.1", "--port", str(port),
            "-c", str(ctx), "-ub", str(ubatch), "-b", str(max(ubatch, 512)), "-t", str(threads),
            "-fit", "off", "--no-webui", "-np", "1", "-lv", "4"]
    if rpc_ports:
        args += ["--rpc", ",".join(f"127.0.0.1:{p}" for p in rpc_ports),
                 "-dev", ",".join(f"RPC{i}" for i in range(len(rpc_ports))),
                 "-ngl", "999", "-ts", ",".join(str(x) for x in tensor_split)]
        if head_on_host:  # keep the vocab projection on the coordinator: only hidden states cross the network
            args += ["-ot", r"^output\.weight$=CPU"]
    else:
        args += ["-ngl", "0", "-dev", "none"]  # single machine, CPU only
    logf = open(log_path, "wb")
    p = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=0x08000000 if sys.platform == "win32" else 0)
    p._meshram_limit = _MemLimit(0)  # no memory cap, just die with the coordinator
    p._meshram_limit.attach(p.pid)
    return p


_BUF = re.compile(r"(RPC\d+\[127\.0\.0\.1:(\d+)\]|CPU_Mapped|CPU)\s+(model|KV|compute) buffer size =\s+([\d.]+) MiB")


def parse_buffers(log_text):
    """Actual per-device allocations reported by llama.cpp: {port|'host': {model, KV, compute}} in bytes."""
    out = {}
    for m in _BUF.finditer(log_text):
        key = m.group(2) or "host"
        out.setdefault(key, {})
        out[key][m.group(3)] = out[key].get(m.group(3), 0) + int(float(m.group(4)) * 2**20)
    return out
