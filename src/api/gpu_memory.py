"""GPU memory figures for a take's log line, read through NVML where it exists.

NVML (the nvidia-ml-py package, the library behind nvidia-smi) is installed
only in GPU images. Without it, or without a GPU, the fields are left out.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

_lock = threading.Lock()
_nvml: Any = None
_handle: Any = None
_unavailable = False

_MIB = 1024 * 1024


def _device() -> Optional[Any]:
    """Return NVML and the first GPU's handle, initialising NVML once."""
    global _nvml, _handle, _unavailable
    with _lock:
        if _unavailable:
            return None
        if _handle is None:
            try:
                import pynvml

                pynvml.nvmlInit()
                _handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                _nvml = pynvml
            except Exception:
                _unavailable = True
                return None
        return _handle


def gpu_memory_log_fields() -> str:
    """Return " gpu_mem_used_mb=… gpu_mem_total_mb=… gpu_process_mem_mb=…", or "".

    Device memory covers every process on the GPU; the process figure is this
    one's, left out when NVML cannot match this process.
    """
    handle = _device()
    if handle is None:
        return ""
    try:
        memory = _nvml.nvmlDeviceGetMemoryInfo(handle)
        fields = f" gpu_mem_used_mb={memory.used // _MIB} gpu_mem_total_mb={memory.total // _MIB}"
        pid = os.getpid()
        for process in _nvml.nvmlDeviceGetComputeRunningProcesses(handle):
            used = getattr(process, "usedGpuMemory", None)
            if getattr(process, "pid", None) == pid and isinstance(used, int):
                fields += f" gpu_process_mem_mb={used // _MIB}"
                break
        return fields
    except Exception:
        return ""
