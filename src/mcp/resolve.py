from __future__ import annotations

"""Path resolution helpers for MCP tool inputs."""

from pathlib import Path
from typing import Optional

from src.api.voicebank_cache import resolve_voicebank_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_project_path(path_value: str) -> Path:
    """Resolve a project-relative path with traversal protection."""
    if not path_value:
        raise ValueError("Path is required.")
    path = Path(path_value)
    if path.is_absolute():
        raise ValueError("Absolute paths are not allowed.")
    resolved = (PROJECT_ROOT / path).resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError("Path escapes project root.")
    return resolved


def resolve_voicebank_id(voicebank_id: str) -> Path:
    """Resolve a voicebank ID to a local path."""
    return resolve_voicebank_path(voicebank_id)


def resolve_optional_path(path_value: Optional[str]) -> Optional[Path]:
    """Resolve a project-relative path when provided."""
    if path_value is None:
        return None
    return resolve_project_path(path_value)
