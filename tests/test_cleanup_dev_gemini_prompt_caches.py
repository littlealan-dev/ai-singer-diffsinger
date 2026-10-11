from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_dev_gemini_prompt_caches.py"
_spec = importlib.util.spec_from_file_location("cleanup_dev_gemini_prompt_caches", _SCRIPT)
cleanup = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# Dataclasses look their module up in sys.modules while the class is built.
sys.modules[_spec.name] = cleanup
_spec.loader.exec_module(cleanup)


def _cache(display_name: str) -> "cleanup.PromptCache":
    return cleanup.PromptCache(
        name=f"cachedContents/{display_name}",
        display_name=display_name,
        model="models/gemini-3.1-flash-lite",
        expire_time="2036-10-08T17:42:03Z",
    )


def test_select_dev_caches_never_selects_production_caches() -> None:
    caches = [
        _cache("prompt-cache:gemini-3.1-flash-lite:dev-1791739725:117f69714c0be8df"),
        _cache("prompt-cache:dev:gemini-3.8-flash:dev-1791800000:68ebdd82e32db21e"),
        _cache("prompt-cache:gemini-3.1-flash-lite:sightsinger-api-00073-fsc:117f69714c0be8df"),
        _cache("prompt-cache:prod:gemini-3.1-flash-lite:sightsinger-api-00074-abc:117f69714c0be8df"),
        _cache("some-other-cache"),
    ]

    selected = cleanup.select_dev_caches(caches, keep_builds=set())

    assert [cache.display_name for cache in selected] == [
        "prompt-cache:gemini-3.1-flash-lite:dev-1791739725:117f69714c0be8df",
        "prompt-cache:dev:gemini-3.8-flash:dev-1791800000:68ebdd82e32db21e",
    ]


def test_select_dev_caches_keeps_named_builds() -> None:
    caches = [
        _cache("prompt-cache:dev:gemini-3.8-flash:dev-1:aaaa"),
        _cache("prompt-cache:dev:gemini-3.8-flash:dev-2:aaaa"),
    ]

    selected = cleanup.select_dev_caches(caches, keep_builds={"dev-2"})

    assert [cache.display_name for cache in selected] == [
        "prompt-cache:dev:gemini-3.8-flash:dev-1:aaaa"
    ]
