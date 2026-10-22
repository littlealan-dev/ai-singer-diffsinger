from __future__ import annotations

from dataclasses import replace

from src.backend.config import Settings
from src.backend.gemini_cache import (
    GeminiPromptCache,
    GeminiPromptCacheManager,
    prompt_cache_display_name,
    prompt_fingerprint,
)

MODEL = "gemini-3.1-flash-lite"
PROMPT = "static system prompt"


def _manager(app_env: str, *, delete_stale: bool = True) -> GeminiPromptCacheManager:
    settings = replace(
        Settings.from_env(),
        app_env=app_env,
        gemini_prompt_cache_enabled=True,
        gemini_prompt_cache_delete_stale=delete_stale,
    )
    return GeminiPromptCacheManager(settings, api_key="dummy")


def _cache(name: str, display_name: str) -> GeminiPromptCache:
    return GeminiPromptCache(name=name, display_name=display_name, model=f"models/{MODEL}")


def _install_fakes(manager, monkeypatch, caches):  # type: ignore[no-untyped-def]
    deleted: list[str] = []

    def fake_create_cache(*, model, display_name, static_prompt_text):  # type: ignore[no-untyped-def]
        return _cache("cachedContents/new", display_name)

    def fake_request_json(method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        assert method == "DELETE"
        deleted.append(url.rsplit("/", 1)[-1])
        return {}

    monkeypatch.setattr(manager, "_list_caches", lambda: list(caches))
    monkeypatch.setattr(manager, "_create_cache", fake_create_cache)
    monkeypatch.setattr(manager, "_request_json", fake_request_json)
    return deleted


def test_prompt_cache_display_name_includes_environment() -> None:
    fingerprint = prompt_fingerprint(PROMPT)

    assert prompt_cache_display_name("Dev", MODEL, "dev-1", fingerprint) == (
        f"prompt-cache:dev:{MODEL}:dev-1:{fingerprint[:16]}"
    )


def test_cache_creation_uses_minute_ttl_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GEMINI_PROMPT_CACHE_TTL_DAYS", "30")
    monkeypatch.setenv("GEMINI_PROMPT_CACHE_TTL_MINUTES", "30")
    manager = _manager("dev")
    payloads = []

    def fake_request_json(method, url, payload):  # type: ignore[no-untyped-def]
        payloads.append(payload)
        return {"name": "cachedContents/new", "model": f"models/{MODEL}"}

    monkeypatch.setattr(manager, "_request_json", fake_request_json)
    manager._create_cache(model=MODEL, display_name="dev-test", static_prompt_text=PROMPT)

    assert payloads[0]["ttl"] == "1800s"


def test_cache_creation_keeps_day_ttl_without_minute_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GEMINI_PROMPT_CACHE_TTL_DAYS", "30")
    monkeypatch.delenv("GEMINI_PROMPT_CACHE_TTL_MINUTES", raising=False)
    manager = _manager("prod")
    payloads = []

    def fake_request_json(method, url, payload):  # type: ignore[no-untyped-def]
        payloads.append(payload)
        return {"name": "cachedContents/new", "model": f"models/{MODEL}"}

    monkeypatch.setattr(manager, "_request_json", fake_request_json)
    manager._create_cache(model=MODEL, display_name="prod-test", static_prompt_text=PROMPT)

    assert payloads[0]["ttl"] == "2592000s"


def test_delete_stale_caches_only_deletes_same_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    manager = _manager("dev")
    caches = [
        _cache("old-dev", f"prompt-cache:dev:{MODEL}:dev-1:aaaa"),
        _cache("prod", f"prompt-cache:prod:{MODEL}:sightsinger-api-00073-fsc:aaaa"),
        _cache("legacy-prod", f"prompt-cache:{MODEL}:sightsinger-api-00073-fsc:aaaa"),
        _cache("other-model-dev", "prompt-cache:dev:gemini-3.8-flash:dev-1:aaaa"),
    ]
    deleted = _install_fakes(manager, monkeypatch, caches)

    name = manager.ensure_prompt_cache(model=MODEL, build_id="dev-2", static_prompt_text=PROMPT)

    assert name == "cachedContents/new"
    assert deleted == ["old-dev"]


def test_ensure_prompt_cache_ignores_other_environment_with_same_build(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    manager = _manager("dev")
    fingerprint = prompt_fingerprint(PROMPT)
    prod_cache = _cache(
        "prod",
        prompt_cache_display_name("prod", MODEL, "build-1", fingerprint),
    )
    deleted = _install_fakes(manager, monkeypatch, [prod_cache])

    name = manager.ensure_prompt_cache(model=MODEL, build_id="build-1", static_prompt_text=PROMPT)

    assert name == "cachedContents/new"
    assert deleted == []


def test_delete_stale_caches_disabled_deletes_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    manager = _manager("dev", delete_stale=False)
    caches = [_cache("old-dev", f"prompt-cache:dev:{MODEL}:dev-1:aaaa")]
    deleted = _install_fakes(manager, monkeypatch, caches)

    manager.ensure_prompt_cache(model=MODEL, build_id="dev-2", static_prompt_text=PROMPT)

    assert deleted == []


def test_a_cold_burst_of_requests_creates_the_prompt_cache_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import threading
    import time

    manager = _manager("dev")
    created: list[str] = []
    remote: list[GeminiPromptCache] = []

    def slow_list_caches():  # type: ignore[no-untyped-def]
        # The answer reflects the caches that existed when the request was sent.
        snapshot = list(remote)
        time.sleep(0.05)
        return snapshot

    def fake_create_cache(*, model, display_name, static_prompt_text):  # type: ignore[no-untyped-def]
        cache = _cache(f"cachedContents/new-{len(created)}", display_name)
        created.append(cache.name)
        remote.append(cache)
        return cache

    deleted: list[str] = []
    monkeypatch.setattr(manager, "_list_caches", slow_list_caches)
    monkeypatch.setattr(manager, "_create_cache", fake_create_cache)
    monkeypatch.setattr(
        manager, "_request_json", lambda method, url, *a, **k: deleted.append(url) or {}
    )
    names: list[str | None] = []

    def request() -> None:
        names.append(
            manager.ensure_prompt_cache(model=MODEL, build_id="dev-2", static_prompt_text=PROMPT)
        )

    threads = [threading.Thread(target=request) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert created == ["cachedContents/new-0"]
    assert names == ["cachedContents/new-0"] * 6
    assert deleted == []


def test_a_duplicate_made_by_another_instance_is_not_deleted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    manager = _manager("dev")
    current = prompt_cache_display_name("dev", MODEL, "dev-2", prompt_fingerprint(PROMPT))
    caches = [
        _cache("first", current),
        _cache("duplicate", current),
        _cache("old-build", f"prompt-cache:dev:{MODEL}:dev-1:aaaa"),
    ]
    deleted = _install_fakes(manager, monkeypatch, caches)

    name = manager.ensure_prompt_cache(model=MODEL, build_id="dev-2", static_prompt_text=PROMPT)

    assert name == "first"
    # Only the other build's cache is stale; the same-name duplicate may be in use.
    assert deleted == ["old-build"]
