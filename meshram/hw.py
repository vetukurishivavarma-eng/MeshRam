"""Real hardware readings. Anything the OS doesn't expose is reported as None, never guessed."""
import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time

import psutil

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=_NO_WINDOW).stdout
    except Exception:
        return ""


def gpus():
    """NVIDIA via nvidia-smi (accurate VRAM); otherwise OS adapter list (VRAM as reported by the driver)."""
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits"])
        res = []
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) == 4:
                res.append({"name": p[0], "vram_total": int(p[1]) * 2**20, "vram_used": int(p[2]) * 2**20,
                            "util_pct": int(p[3]) if p[3].isdigit() else None, "source": "nvidia-smi"})
        if res:
            return res
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | Select Name,AdapterRAM | ConvertTo-Json"])
        try:
            data = json.loads(out)
            data = data if isinstance(data, list) else [data]
            # AdapterRAM is a 32-bit field (caps at 4 GiB) and is shared memory on iGPUs: label it as such
            return [{"name": d.get("Name"), "vram_total": d.get("AdapterRAM") or None, "vram_used": None,
                     "util_pct": None, "source": "wmi (driver-reported, may be shared/capped)"} for d in data]
        except Exception:
            return []
    return []


def _win_temp():
    out = _run(["powershell", "-NoProfile", "-Command",
                "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop"
                " | Select -First 1).CurrentTemperature"])
    try:
        return round(int(out.strip()) / 10 - 273.15, 1)
    except ValueError:
        return None


class TempReader:
    """Linux/macOS: psutil sensors. Windows: ACPI thermal zone (needs admin on most machines)."""

    def __init__(self):
        self.ok = True
        self.last, self.at = None, 0

    def read(self):
        if not self.ok:
            return None
        if hasattr(psutil, "sensors_temperatures"):
            try:
                temps = psutil.sensors_temperatures()
                vals = [t.current for arr in temps.values() for t in arr if t.current]
                return max(vals) if vals else None
            except Exception:
                return None
        if sys.platform == "win32":
            if time.time() - self.at > 30:
                self.last, self.at = _win_temp(), time.time()
                if self.last is None:
                    self.ok = False  # not available on this machine; stop spawning powershell
            return self.last
        return None


def idle_seconds():
    if sys.platform == "win32":
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000
    return None  # ponytail: Linux (X11/Wayland) and macOS (ioreg HIDIdleTime) idle not implemented


def static_info():
    vm = psutil.virtual_memory()
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_model": platform.processor() or platform.machine(),
        "cpu_physical": psutil.cpu_count(logical=False),
        "cpu_logical": psutil.cpu_count(),
        "ram_total": vm.total,
        "gpus": gpus(),
    }


def snapshot(temp_reader, procs=()):
    """Live readings. `procs` = our own child processes, so cluster RAM/CPU can be separated from the owner's."""
    vm = psutil.virtual_memory()
    cluster_rss, cluster_cpu = 0, 0.0
    for p in procs:
        try:
            cluster_rss += p.memory_info().rss
            cluster_cpu += p.cpu_percent(None) / (psutil.cpu_count() or 1)
        except psutil.Error:
            pass
    bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    return {
        "ts": time.time(),
        "ram_total": vm.total,
        "ram_used": vm.total - vm.available,
        "ram_available": vm.available,
        "cpu_pct": psutil.cpu_percent(None),
        "cluster_rss": cluster_rss,
        "cluster_cpu_pct": round(cluster_cpu, 1),
        "temp_c": temp_reader.read(),
        "battery_pct": bat.percent if bat else None,
        "charging": bat.power_plugged if bat else None,  # None = desktop / no battery
        "idle_s": idle_seconds(),
        "load_pid": os.getpid(),
    }
