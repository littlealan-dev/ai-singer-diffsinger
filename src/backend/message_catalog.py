from __future__ import annotations

"""Simple backend message catalog loader for user-facing strings."""

from functools import lru_cache
from pathlib import Path


_CATALOG_DIR = Path(__file__).with_name("messages")


@lru_cache(maxsize=None)
def _load_catalog(locale: str = "en") -> dict[str, str]:
    path = _CATALOG_DIR / f"{locale}.properties"
    messages: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"Invalid message catalog line in {path}: {raw_line.rstrip()}")
            key, value = line.split("=", 1)
            messages[key.strip()] = value.strip()
    return messages


def backend_message(key: str, /, *, locale: str = "en", **kwargs: object) -> str:
    """Return a formatted backend message by key."""
    catalog = _load_catalog(locale)
    try:
        template = catalog[key]
    except KeyError as exc:
        raise KeyError(f"Missing backend message key: {key}") from exc
    return template.format(**kwargs)
