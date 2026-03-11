from __future__ import annotations

"""Explicit Gemini prompt cache management."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from src.backend.config import Settings


@dataclass(frozen=True)
class GeminiPromptCache:
    """Minimal metadata for a resolved Gemini cached content entry."""

    name: str
    display_name: str
    model: str
    expire_time: str | None = None


class GeminiPromptCacheManager:
    """Lazy, memoized manager for deployment-scoped prompt caches."""

    def __init__(self, settings: Settings, *, api_key: str) -> None:
        self._settings = settings
        self._api_key = api_key
        self._base_url = settings.gemini_base_url.rstrip("/")
        self._logger = logging.getLogger(__name__)
        self._cache_names_by_key: dict[tuple[str, str, str], str] = {}

    def ensure_prompt_cache(
        self,
        *,
        model: str,
        build_id: str,
        static_prompt_text: str,
    ) -> str | None:
        """Return a cachedContent name for the static prompt, creating it if needed."""
        if not self._settings.gemini_prompt_cache_enabled:
            return None
        fingerprint = prompt_fingerprint(static_prompt_text)
        cache_key = (model, build_id, fingerprint)
        cached_name = self._cache_names_by_key.get(cache_key)
        if cached_name:
            self._logger.info(
                "gemini_prompt_cache_memory_hit model=%s build_id=%s fingerprint_prefix=%s cache_name=%s",
                model,
                build_id,
                fingerprint[:12],
                cached_name,
            )
            return cached_name
        try:
            caches = self._list_caches()
            self._logger.info(
                "gemini_prompt_cache_lookup model=%s build_id=%s fingerprint_prefix=%s discovered=%s",
                model,
                build_id,
                fingerprint[:12],
                len(caches),
            )
            display_name = prompt_cache_display_name(model, build_id, fingerprint)
            matched = next(
                (
                    cache
                    for cache in caches
                    if cache.display_name == display_name and cache.model == _model_resource_name(model)
                ),
                None,
            )
            if matched is not None:
                self._cache_names_by_key[cache_key] = matched.name
                self._logger.info(
                    "gemini_prompt_cache_hit model=%s build_id=%s fingerprint_prefix=%s cache_name=%s",
                    model,
                    build_id,
                    fingerprint[:12],
                    matched.name,
                )
                self._delete_stale_caches(caches, model=model, keep_name=matched.name)
                return matched.name
            created = self._create_cache(
                model=model,
                display_name=display_name,
                static_prompt_text=static_prompt_text,
            )
            if created is None:
                return None
            self._cache_names_by_key[cache_key] = created.name
            self._delete_stale_caches(caches + [created], model=model, keep_name=created.name)
            return created.name
        except Exception as exc:
            self._logger.error(
                "gemini_prompt_cache_fallback_uncached model=%s build_id=%s error=%s",
                model,
                build_id,
                exc,
            )
            return None

    def _list_caches(self) -> list[GeminiPromptCache]:
        caches: list[GeminiPromptCache] = []
        page_token: str | None = None
        while True:
            params = {"pageSize": "100"}
            if page_token:
                params["pageToken"] = page_token
            url = f"{self._base_url}/cachedContents?{urllib.parse.urlencode(params)}"
            payload = self._request_json("GET", url)
            raw_caches = payload.get("cachedContents", [])
            if isinstance(raw_caches, list):
                for entry in raw_caches:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name") or "").strip()
                    display_name = str(entry.get("displayName") or "").strip()
                    model = str(entry.get("model") or "").strip()
                    if not name or not display_name or not model:
                        continue
                    caches.append(
                        GeminiPromptCache(
                            name=name,
                            display_name=display_name,
                            model=model,
                            expire_time=(
                                str(entry.get("expireTime")).strip()
                                if entry.get("expireTime") is not None
                                else None
                            ),
                        )
                    )
            page_token_value = payload.get("nextPageToken")
            if not isinstance(page_token_value, str) or not page_token_value.strip():
                break
            page_token = page_token_value.strip()
        return caches

    def _create_cache(
        self,
        *,
        model: str,
        display_name: str,
        static_prompt_text: str,
    ) -> GeminiPromptCache | None:
        ttl_seconds = max(1, self._settings.gemini_prompt_cache_ttl_days * 24 * 60 * 60)
        expire_time = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).isoformat().replace("+00:00", "Z")
        payload = {
            "model": _model_resource_name(model),
            "displayName": display_name,
            "systemInstruction": {"parts": [{"text": static_prompt_text}]},
            "ttl": f"{ttl_seconds}s",
        }
        try:
            body = self._request_json("POST", f"{self._base_url}/cachedContents", payload)
        except Exception as exc:
            self._logger.error(
                "gemini_prompt_cache_create_failed model=%s build_id=%s display_name=%s error=%s",
                model,
                self._settings.backend_build_id,
                display_name,
                exc,
            )
            return None
        name = str(body.get("name") or "").strip()
        if not name:
            self._logger.error(
                "gemini_prompt_cache_create_failed model=%s build_id=%s display_name=%s error=missing_name",
                model,
                self._settings.backend_build_id,
                display_name,
            )
            return None
        self._logger.info(
            "gemini_prompt_cache_create model=%s build_id=%s display_name=%s cache_name=%s expire_time=%s",
            model,
            self._settings.backend_build_id,
            display_name,
            name,
            body.get("expireTime") or expire_time,
        )
        return GeminiPromptCache(
            name=name,
            display_name=display_name,
            model=str(body.get("model") or _model_resource_name(model)),
            expire_time=(
                str(body.get("expireTime")).strip() if body.get("expireTime") is not None else expire_time
            ),
        )

    def _delete_stale_caches(
        self,
        caches: list[GeminiPromptCache],
        *,
        model: str,
        keep_name: str,
    ) -> None:
        if not self._settings.gemini_prompt_cache_delete_stale:
            return
        prefix = f"prompt-cache:{model}:"
        for cache in caches:
            if cache.name == keep_name:
                continue
            if not cache.display_name.startswith(prefix):
                continue
            try:
                self._request_json("DELETE", f"{self._base_url}/{cache.name}")
                self._logger.info(
                    "gemini_prompt_cache_delete_stale model=%s build_id=%s cache_name=%s display_name=%s",
                    model,
                    self._settings.backend_build_id,
                    cache.name,
                    cache.display_name,
                )
                for key, value in list(self._cache_names_by_key.items()):
                    if value == cache.name:
                        self._cache_names_by_key.pop(key, None)
            except Exception as exc:
                self._logger.error(
                    "gemini_prompt_cache_delete_failed model=%s build_id=%s cache_name=%s error=%s",
                    model,
                    self._settings.backend_build_id,
                    cache.name,
                    exc,
                )

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {
            "x-goog-api-key": self._api_key,
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._settings.gemini_timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            self._logger.error(
                "gemini_prompt_cache_api_error method=%s url=%s status=%s details=%s",
                method,
                url,
                exc.code,
                details,
            )
            raise RuntimeError(f"Gemini cache HTTP error {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            self._logger.error(
                "gemini_prompt_cache_api_error method=%s url=%s error=%s",
                method,
                url,
                exc.reason,
            )
            raise RuntimeError(f"Gemini cache connection error: {exc.reason}") from exc
        if not body:
            return {}
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise RuntimeError("Gemini cache response was not an object.")
        return parsed


def prompt_fingerprint(static_prompt_text: str) -> str:
    """Return a stable fingerprint for the static prompt body."""
    return hashlib.sha256(static_prompt_text.encode("utf-8")).hexdigest()


def prompt_cache_display_name(model: str, build_id: str, fingerprint: str) -> str:
    """Return the deterministic display name for a prompt cache."""
    return f"prompt-cache:{model}:{build_id}:{fingerprint[:16]}"


def _model_resource_name(model: str) -> str:
    """Normalize a model id into the Gemini resource name format."""
    normalized = model.strip()
    if normalized.startswith("models/"):
        return normalized
    return f"models/{normalized}"
