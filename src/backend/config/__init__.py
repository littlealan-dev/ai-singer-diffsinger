from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from src.mcp.resolve import PROJECT_ROOT


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    sessions_dir: Path
    max_upload_bytes: int
    session_ttl_seconds: int
    max_sessions: int
    default_voicebank: str
    default_voice_id: str | None
    llm_provider: str
    gemini_api_key: str
    gemini_base_url: str
    gemini_model: str
    gemini_timeout_seconds: float
    mcp_cpu_device: str
    mcp_gpu_device: str
    mcp_timeout_seconds: float
    mcp_gpu_timeout_seconds: float
    mcp_startup_timeout_seconds: float
    mcp_debug: bool

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir_name = os.getenv("BACKEND_DATA_DIR", "data")
        data_dir = (PROJECT_ROOT / data_dir_name).resolve()
        if data_dir != PROJECT_ROOT and PROJECT_ROOT not in data_dir.parents:
            raise ValueError("BACKEND_DATA_DIR must stay within the project root.")
        sessions_dir = data_dir / "sessions"
        max_upload_mb = _env_int("BACKEND_MAX_UPLOAD_MB", 20)
        max_upload_bytes = max_upload_mb * 1024 * 1024
        session_ttl_seconds = _env_int("BACKEND_SESSION_TTL_SECONDS", 24 * 60 * 60)
        max_sessions = _env_int("BACKEND_MAX_SESSIONS", 200)
        default_voicebank = os.getenv("BACKEND_DEFAULT_VOICEBANK", "")
        default_voice_id = os.getenv("BACKEND_DEFAULT_VOICE_ID")
        llm_provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
        gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        gemini_base_url = os.getenv(
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta",
        )
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        gemini_timeout_seconds = _env_float("GEMINI_TIMEOUT_SECONDS", 30.0)
        mcp_cpu_device = os.getenv("MCP_CPU_DEVICE", "cpu")
        mcp_gpu_device = os.getenv("MCP_GPU_DEVICE", "cpu")
        mcp_timeout_seconds = _env_float("MCP_TIMEOUT_SECONDS", 60.0)
        mcp_gpu_timeout_seconds = _env_float("MCP_GPU_TIMEOUT_SECONDS", 300.0)
        mcp_startup_timeout_seconds = _env_float("MCP_STARTUP_TIMEOUT_SECONDS", 10.0)
        mcp_debug = os.getenv("MCP_DEBUG", "false").lower() in {"1", "true", "yes"}
        return cls(
            project_root=PROJECT_ROOT,
            data_dir=data_dir,
            sessions_dir=sessions_dir,
            max_upload_bytes=max_upload_bytes,
            session_ttl_seconds=session_ttl_seconds,
            max_sessions=max_sessions,
            default_voicebank=default_voicebank,
            default_voice_id=default_voice_id,
            llm_provider=llm_provider,
            gemini_api_key=gemini_api_key,
            gemini_base_url=gemini_base_url,
            gemini_model=gemini_model,
            gemini_timeout_seconds=gemini_timeout_seconds,
            mcp_cpu_device=mcp_cpu_device,
            mcp_gpu_device=mcp_gpu_device,
            mcp_timeout_seconds=mcp_timeout_seconds,
            mcp_gpu_timeout_seconds=mcp_gpu_timeout_seconds,
            mcp_startup_timeout_seconds=mcp_startup_timeout_seconds,
            mcp_debug=mcp_debug,
        )
