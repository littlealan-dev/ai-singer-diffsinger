from __future__ import annotations

from typing import Any, Dict, Iterable
from pathlib import Path
import logging

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency
    np = None


def summarize_payload(value: Any, *, max_list: int = 20, max_str: int = 200, depth: int = 3) -> Any:
    if depth <= 0:
        return f"<{type(value).__name__}>"
    if np is not None and isinstance(value, np.ndarray):
        return {"__ndarray__": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, dict):
        items = list(value.items())
        summarized: Dict[str, Any] = {}
        for key, val in items[:max_list]:
            summarized[str(key)] = summarize_payload(val, max_list=max_list, max_str=max_str, depth=depth - 1)
        if len(items) > max_list:
            summarized["__truncated__"] = True
            summarized["__len__"] = len(items)
        return summarized
    if isinstance(value, (list, tuple)):
        if len(value) > max_list:
            return {
                "__len__": len(value),
                "sample": [
                    summarize_payload(item, max_list=max_list, max_str=max_str, depth=depth - 1)
                    for item in value[:5]
                ],
            }
        return [
            summarize_payload(item, max_list=max_list, max_str=max_str, depth=depth - 1)
            for item in value
        ]
    if isinstance(value, str):
        if len(value) > max_str:
            return value[:max_str] + "...(truncated)"
        return value
    if isinstance(value, bytes):
        return {"__bytes__": len(value)}
    if isinstance(value, Path):
        return str(value)
    return value


DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def ensure_timestamped_handlers(logger_names: Iterable[str] | None = None) -> None:
    formatter = logging.Formatter(DEFAULT_LOG_FORMAT)
    if logger_names is None:
        logger_names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    for name in logger_names:
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            handler.setFormatter(formatter)


def get_logger(module_name: str) -> logging.Logger:
    logger = logging.getLogger(module_name)
    if getattr(logger, "_file_handler_attached", False):
        return logger
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    filename = module_name.replace(".", "_") + ".log"
    handler = logging.FileHandler(log_dir / filename, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT))
    logger.addHandler(handler)
    logger.propagate = True
    setattr(logger, "_file_handler_attached", True)
    return logger
