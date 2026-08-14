from __future__ import annotations

"""Helpers for classifying GPU/CUDA infrastructure failures."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional


GPU_ERROR_CATEGORY = "infrastructure"
GPU_ERROR_PUBLIC_CODE = "render_worker_resource_exhausted"
GPU_ERROR_INTERNAL_CATEGORY = "gpu_runtime"
GPU_ERROR_INTERNAL_FAMILY = "onnxruntime_cuda"
GPU_ERROR_USER_MESSAGE = "Error in GPU / CUDA resource. Please try again later."

GPU_MEMORY_EXHAUSTED = "gpu_memory_exhausted"
GPU_LIBRARY_INIT_FAILED = "gpu_library_init_failed"
GPU_EXECUTION_PROVIDER_FAILED = "gpu_execution_provider_failed"
GPU_CUDA_RUNTIME_FAILED = "gpu_cuda_runtime_failed"
GPU_NATIVE_LIBRARY_FAILURE = "gpu_native_library_failure"

_GPU_INTERNAL_CODES = {
    GPU_MEMORY_EXHAUSTED,
    GPU_LIBRARY_INIT_FAILED,
    GPU_EXECUTION_PROVIDER_FAILED,
    GPU_CUDA_RUNTIME_FAILED,
    GPU_NATIVE_LIBRARY_FAILURE,
}


@dataclass(frozen=True)
class GpuInfrastructureErrorInfo:
    """Structured classification for retryable GPU worker-health failures."""

    code: str
    message: str
    matched_pattern: str

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "category": GPU_ERROR_CATEGORY,
            "publicCode": GPU_ERROR_PUBLIC_CODE,
            "internalCategory": GPU_ERROR_INTERNAL_CATEGORY,
            "internalFamily": GPU_ERROR_INTERNAL_FAMILY,
            "code": self.code,
            "retryable": True,
            "workerRestartRequired": True,
            "matchedPattern": self.matched_pattern,
        }


def classify_gpu_infrastructure_error(
    message: str,
    *,
    error_type: str | None = None,
    code: str | None = None,
) -> Optional[GpuInfrastructureErrorInfo]:
    """Return a GPU infra classification when the error is CUDA/ONNX related."""

    raw_message = _normalize_error_message(message)
    raw_type = (error_type or "").strip()
    raw_code = (code or "").strip()
    combined = " ".join(part for part in (raw_code, raw_type, raw_message) if part)
    lower = combined.lower()

    if raw_code in _GPU_INTERNAL_CODES:
        return GpuInfrastructureErrorInfo(
            code=raw_code,
            message=raw_message or raw_code,
            matched_pattern=raw_code,
        )

    memory_patterns = (
        "bfcaren",
        "allocaterawinternal",
        "failed to allocate memory",
        "cuda out of memory",
        "cuda_error_out_of_memory",
        "cudaerrormemoryallocation",
        "cublas_status_alloc_failed",
        "cudnn_status_alloc_failed",
    )
    for pattern in memory_patterns:
        if pattern in lower:
            return GpuInfrastructureErrorInfo(
                code=GPU_MEMORY_EXHAUSTED,
                message=raw_message or combined,
                matched_pattern=pattern,
            )

    init_patterns = (
        "cudnn_status_not_initialized",
        "cublas_status_not_initialized",
    )
    for pattern in init_patterns:
        if pattern in lower:
            return GpuInfrastructureErrorInfo(
                code=GPU_LIBRARY_INIT_FAILED,
                message=raw_message or combined,
                matched_pattern=pattern,
            )

    runtime_patterns = ("cuda_call.cc", "cuda failure", "cuda error")
    for pattern in runtime_patterns:
        if pattern in lower:
            return GpuInfrastructureErrorInfo(
                code=GPU_CUDA_RUNTIME_FAILED,
                message=raw_message or combined,
                matched_pattern=pattern,
            )

    native_patterns = ("cudnn failure", "cublas failure", "cudnn_status_", "cublas_status_")
    for pattern in native_patterns:
        if pattern in lower:
            return GpuInfrastructureErrorInfo(
                code=GPU_NATIVE_LIBRARY_FAILURE,
                message=raw_message or combined,
                matched_pattern=pattern,
            )

    provider_patterns = ("cudaexecutionprovider", "cuda execution provider")
    for pattern in provider_patterns:
        if pattern in lower:
            return GpuInfrastructureErrorInfo(
                code=GPU_EXECUTION_PROVIDER_FAILED,
                message=raw_message or combined,
                matched_pattern=pattern,
            )

    if "onnxruntimeerror" in lower and "cuda" in lower:
        return GpuInfrastructureErrorInfo(
            code=GPU_EXECUTION_PROVIDER_FAILED,
            message=raw_message or combined,
            matched_pattern="onnxruntimeerror+cuda",
        )

    return None


def classify_gpu_tool_error(payload: Mapping[str, Any]) -> Optional[GpuInfrastructureErrorInfo]:
    """Classify an MCP tool error payload as a GPU infra error when applicable."""

    return classify_gpu_infrastructure_error(
        str(payload.get("message") or ""),
        error_type=str(payload.get("type") or ""),
        code=str(payload.get("code") or ""),
    )


def gpu_tool_error_payload(exc: Exception) -> Optional[dict[str, Any]]:
    """Build MCP error fields for a raw exception, if it is GPU infra related."""

    info = classify_gpu_infrastructure_error(str(exc), error_type=exc.__class__.__name__)
    if info is None:
        return None
    payload = info.payload
    payload["message"] = info.message
    payload["type"] = exc.__class__.__name__
    return payload


def format_gpu_resource_error_message(payload: Mapping[str, Any]) -> str:
    """Build the user-facing GPU/CUDA resource error message."""

    code = str(payload.get("code") or GPU_EXECUTION_PROVIDER_FAILED).strip()
    message = _normalize_error_message(str(payload.get("message") or code))
    return (
        f"{GPU_ERROR_USER_MESSAGE}\n\n"
        f"Error Code: {code}\n"
        f"Error Message: {message}"
    )


def _normalize_error_message(message: str) -> str:
    normalized = " ".join(str(message or "").replace("\x00", "").split()).strip()
    if len(normalized) > 1000:
        return normalized[:997].rstrip() + "..."
    return normalized
