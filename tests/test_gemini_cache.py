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
