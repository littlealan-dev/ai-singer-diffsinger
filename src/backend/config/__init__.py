from __future__ import annotations

"""Backend settings loader from environment variables."""

from dataclasses import dataclass
from pathlib import Path
import os

from src.mcp.resolve import PROJECT_ROOT


def _env_int(name: str, default: int) -> int:
    """Read an int env var with a default."""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    """Read a float env var with a default."""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var with a default."""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes"}


def _project_id() -> str | None:
    """Return the active GCP project ID if set."""
    for key in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "PROJECT_ID"):
        value = os.getenv(key)
        if value:
            return value
    return None


def _app_env() -> str:
    """Return the application environment name."""
    return os.getenv("APP_ENV") or os.getenv("ENV") or "dev"


@dataclass(frozen=True)
class Settings:
    """Configuration values parsed from the environment."""
    project_root: Path
    data_dir: Path
    sessions_dir: Path
    max_upload_bytes: int
    session_ttl_seconds: int
    max_sessions: int
    default_voicebank: str
    default_voice_id: str | None
    audio_format: str
    audio_mp3_bitrate: str
    backend_debug: bool
    llm_provider: str
    gemini_api_key: str
    gemini_api_key_secret: str
    gemini_api_key_secret_version: str
    gemini_base_url: str
    gemini_model: str
    gemini_timeout_seconds: float
    llm_max_message_chars: int
    llm_max_tool_code_chars: int
    llm_max_history_items: int
    mcp_cpu_device: str
    mcp_gpu_device: str
    mcp_timeout_seconds: float
    mcp_gpu_timeout_seconds: float
    mcp_startup_timeout_seconds: float
    mcp_debug: bool
    backend_auth_disabled: bool
    dev_user_id: str
    dev_user_email: str
    backend_use_storage: bool
    storage_bucket: str
    app_env: str
    project_id: str | None
    backend_require_app_check: bool

    @classmethod
    def from_env(cls) -> "Settings":
        """Construct settings from environment variables."""
        data_dir_name = os.getenv("BACKEND_DATA_DIR", "data")
        data_dir = (PROJECT_ROOT / data_dir_name).resolve()
        if data_dir != PROJECT_ROOT and PROJECT_ROOT not in data_dir.parents:
            raise ValueError("BACKEND_DATA_DIR must stay within the project root.")
        sessions_dir = data_dir / "sessions"
        max_upload_mb = _env_int("BACKEND_MAX_UPLOAD_MB", 20)
        max_upload_bytes = max_upload_mb * 1024 * 1024
        session_ttl_seconds = _env_int("BACKEND_SESSION_TTL_SECONDS", 5 * 24 * 60 * 60)
        max_sessions = _env_int("BACKEND_MAX_SESSIONS", 200)
        default_voicebank = os.getenv("BACKEND_DEFAULT_VOICEBANK", "Raine_Rena_2.01")
        default_voice_id = os.getenv("BACKEND_DEFAULT_VOICE_ID")
        audio_format = os.getenv("BACKEND_AUDIO_FORMAT", "mp3").strip().lower()
        audio_mp3_bitrate = os.getenv("BACKEND_AUDIO_MP3_BITRATE", "256k").strip()
        backend_debug = os.getenv("BACKEND_DEBUG", "").lower() in {"1", "true", "yes"}
        llm_provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
        gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        gemini_api_key_secret = os.getenv("GEMINI_API_KEY_SECRET", "GEMINI_API_KEY")
        gemini_api_key_secret_version = os.getenv("GEMINI_API_KEY_SECRET_VERSION", "latest")
        gemini_base_url = os.getenv(
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta",
        )
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
        gemini_timeout_seconds = _env_float("GEMINI_TIMEOUT_SECONDS", 30.0)
        llm_max_message_chars = _env_int("LLM_MAX_MESSAGE_CHARS", 2000)
        llm_max_tool_code_chars = _env_int("LLM_MAX_TOOL_CODE_CHARS", 4000)
        llm_max_history_items = _env_int("LLM_MAX_HISTORY_ITEMS", 12)
        mcp_cpu_device = os.getenv("MCP_CPU_DEVICE", "cpu")
        mcp_gpu_device = os.getenv("MCP_GPU_DEVICE", "cpu")
        mcp_timeout_seconds = _env_float("MCP_TIMEOUT_SECONDS", 60.0)
        mcp_gpu_timeout_seconds = _env_float("MCP_GPU_TIMEOUT_SECONDS", 300.0)
        mcp_startup_timeout_seconds = _env_float("MCP_STARTUP_TIMEOUT_SECONDS", 30.0)
        mcp_debug = _env_bool("MCP_DEBUG", False)
        backend_auth_disabled = _env_bool("BACKEND_AUTH_DISABLED", False)
        dev_user_id = os.getenv("BACKEND_DEV_USER_ID", "dev-user").strip()
        dev_user_email = os.getenv("BACKEND_DEV_USER_EMAIL", "user@example.com").strip()
        backend_use_storage = _env_bool("BACKEND_USE_STORAGE", False)
        project_id = _project_id()
        default_bucket = f"{project_id}.appspot.com" if project_id else ""
        storage_bucket = os.getenv("STORAGE_BUCKET", default_bucket)
        app_env = _app_env()
        app_env_lower = app_env.lower()
        backend_require_app_check = _env_bool(
            "BACKEND_REQUIRE_APP_CHECK",
            app_env_lower not in {"dev", "development", "local", "test"},
        )
        return cls(
            project_root=PROJECT_ROOT,
            data_dir=data_dir,
            sessions_dir=sessions_dir,
            max_upload_bytes=max_upload_bytes,
            session_ttl_seconds=session_ttl_seconds,
            max_sessions=max_sessions,
            default_voicebank=default_voicebank,
            default_voice_id=default_voice_id,
            audio_format=audio_format,
            audio_mp3_bitrate=audio_mp3_bitrate,
            backend_debug=backend_debug,
            llm_provider=llm_provider,
            gemini_api_key=gemini_api_key,
            gemini_api_key_secret=gemini_api_key_secret,
            gemini_api_key_secret_version=gemini_api_key_secret_version,
            gemini_base_url=gemini_base_url,
            gemini_model=gemini_model,
            gemini_timeout_seconds=gemini_timeout_seconds,
            llm_max_message_chars=llm_max_message_chars,
            llm_max_tool_code_chars=llm_max_tool_code_chars,
            llm_max_history_items=llm_max_history_items,
            mcp_cpu_device=mcp_cpu_device,
            mcp_gpu_device=mcp_gpu_device,
            mcp_timeout_seconds=mcp_timeout_seconds,
            mcp_gpu_timeout_seconds=mcp_gpu_timeout_seconds,
            mcp_startup_timeout_seconds=mcp_startup_timeout_seconds,
            mcp_debug=mcp_debug,
            backend_auth_disabled=backend_auth_disabled,
            dev_user_id=dev_user_id,
            dev_user_email=dev_user_email,
            backend_use_storage=backend_use_storage,
            storage_bucket=storage_bucket,
            app_env=app_env,
            project_id=project_id,
            backend_require_app_check=backend_require_app_check,
        )
