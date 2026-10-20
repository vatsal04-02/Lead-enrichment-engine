"""Cross-platform environment facts for reports. Never raises: anything unknown is reported as 'unknown'."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT, timeout=10)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown (git not available)"


def _cpu() -> str:
    try:
        return next(l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo", encoding="utf-8") if l.startswith("model name"))
    except (OSError, StopIteration):
        return platform.processor() or "unknown"


def _memory() -> str:
    try:
        kb = int(next(l for l in open("/proc/meminfo", encoding="utf-8") if l.startswith("MemTotal")).split()[1])
        return f"{kb / 1048576:.1f} GiB"
    except (OSError, StopIteration, ValueError):
        pass
    try:                                   # Windows
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
        return f"{st.ullTotalPhys / 2**30:.1f} GiB"
    except Exception:
        return "unknown"


def hardware(pg_version: str = "unknown") -> dict:
    return {"platform": platform.platform(), "cpu": _cpu(), "logical_cpus": os.cpu_count(), "memory": _memory(),
            "python": platform.python_version(), "postgres": pg_version, "git_commit": git_commit()}
