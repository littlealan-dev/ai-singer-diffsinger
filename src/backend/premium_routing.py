"""Decide whether a user's synthesis jobs go to the premium (RTX) worker.

With SYNTHESIS_PREMIUM_ROUTING=allowlist, a user is routed there when their
uid is in app_config/maintenance.allowedUids or their email in allowedEmails,
whether or not maintenance is on: the same lists that let testers through
maintenance mode let them try the premium worker.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# The lists change rarely; reading them once a minute keeps a burst of jobs
# from reading the same document once per job.
_CONFIG_CACHE_SECONDS = 60.0


def _load_maintenance_config() -> Dict[str, Any]:
    from src.backend.firebase_app import get_firestore_client

    snapshot = get_firestore_client().collection("app_config").document("maintenance").get()
    data = snapshot.to_dict() if snapshot.exists else None
    return data if isinstance(data, dict) else {}


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item.strip() for item in value if isinstance(item, str) and item.strip()}


class PremiumSynthesisRouting:
    """Answer "does this user's take go to the premium worker?" for one routing mode."""

    def __init__(
        self,
        routing: str,
        *,
        load_config: Callable[[], Dict[str, Any]] = _load_maintenance_config,
        cache_seconds: float = _CONFIG_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._routing = routing
        self._load_config = load_config
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._config: Optional[Dict[str, Any]] = None
        self._loaded_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._routing != "off"

    def is_premium(self, user_id: str, user_email: Optional[str]) -> bool:
        """True when this user's jobs go to the premium worker.

        If the lists cannot be read, the job goes to the standard worker.
        """
        if self._routing != "allowlist":
            return False
        try:
            config = self._cached_config()
        except Exception as exc:
            logger.warning("premium_routing_config_unavailable error=%s", exc)
            return False
        if user_id and user_id in _string_set(config.get("allowedUids")):
            return True
        email = (user_email or "").strip().lower()
        allowed_emails = {item.lower() for item in _string_set(config.get("allowedEmails"))}
        return bool(email) and email in allowed_emails

    def _cached_config(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            if self._config is None or now - self._loaded_at >= self._cache_seconds:
                self._config = self._load_config()
                self._loaded_at = now
            return self._config
