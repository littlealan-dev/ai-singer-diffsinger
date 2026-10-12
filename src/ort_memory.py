"""GPU memory settings shared by the ONNX Runtime model wrappers.

By default each CUDA session keeps a memory pool (BFC arena) that grows in
powers of two and never shrinks, so one long render leaves several GiB held
until the process restarts. Sessions here grow the pool only by what a request
needs and hand unused pool memory back at the end of every run.
"""

from typing import Optional, Tuple

import onnxruntime as ort

CUDA_PROVIDER = "CUDAExecutionProvider"
_ARENA_SHRINKAGE_KEY = "memory.enable_memory_arena_shrinkage"


def cuda_provider() -> Tuple[str, dict]:
    """Return the CUDA provider entry with a pool that grows only as requested."""
    return (CUDA_PROVIDER, {"arena_extend_strategy": "kSameAsRequested"})


def run_options_for(session: ort.InferenceSession) -> Optional[ort.RunOptions]:
    """Return run options that shrink the CUDA pool after a run, for CUDA sessions."""
    if CUDA_PROVIDER not in session.get_providers():
        return None
    options = ort.RunOptions()
    options.add_run_config_entry(_ARENA_SHRINKAGE_KEY, "gpu:0")
    return options
