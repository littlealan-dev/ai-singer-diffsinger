#!/usr/bin/env python3
from __future__ import annotations

"""List, and optionally delete, dev Gemini prompt caches.

Gemini prompt caches belong to the Google Cloud project, not the API key, so
the dev key also sees production's caches. This script only ever selects dev
caches:

- current format: prompt-cache:dev:{model}:{build_id}:{fingerprint}
- old format:     prompt-cache:{model}:{build_id}:{fingerprint}, where build_id
                  starts with "dev-" (dev builds are named dev-<epoch>)

It is a dry run unless --delete is given. Uses GEMINI_API_KEY (the dev key) and
GEMINI_BASE_URL from the environment.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEV_BUILD_PREFIX = "dev-"


@dataclass(frozen=True)
class PromptCache:
    name: str
    display_name: str
    model: str
    expire_time: str


def dev_cache_build_id(display_name: str) -> str | None:
    """Return the build id of a dev prompt cache, or None for any other cache."""
    parts = display_name.split(":")
    if not parts or parts[0] != "prompt-cache":
        return None
    if len(parts) == 5 and parts[1] == "dev":
        return parts[3]
    if len(parts) == 4 and parts[2].startswith(DEV_BUILD_PREFIX):
        return parts[2]
    return None


def select_dev_caches(caches: list[PromptCache], keep_builds: set[str]) -> list[PromptCache]:
    """Return dev caches whose build is not in keep_builds."""
    selected = []
    for cache in caches:
        build_id = dev_cache_build_id(cache.display_name)
        if build_id is None or build_id in keep_builds:
            continue
        selected.append(cache)
    return selected


def _request_json(method: str, url: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"x-goog-api-key": api_key}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {details}") from exc
    return json.loads(body) if body else {}


def list_caches(base_url: str, api_key: str) -> list[PromptCache]:
    caches: list[PromptCache] = []
    page_token = ""
    while True:
        params = {"pageSize": "100"}
        if page_token:
            params["pageToken"] = page_token
        payload = _request_json(
            "GET", f"{base_url}/cachedContents?{urllib.parse.urlencode(params)}", api_key
        )
        for entry in payload.get("cachedContents") or []:
            if not isinstance(entry, dict):
                continue
            caches.append(
                PromptCache(
                    name=str(entry.get("name") or ""),
                    display_name=str(entry.get("displayName") or ""),
                    model=str(entry.get("model") or ""),
                    expire_time=str(entry.get("expireTime") or ""),
                )
            )
        page_token = str(payload.get("nextPageToken") or "").strip()
        if not page_token:
            return caches


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List, and optionally delete, dev Gemini prompt caches (dry run by default)."
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the selected dev caches. Without it, only list them.",
    )
    parser.add_argument(
        "--keep-build",
        action="append",
        default=[],
        metavar="BUILD_ID",
        help="Keep caches from this dev build, e.g. the running server's dev-<epoch>. Repeatable.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY is not set. Export the dev key first.", file=sys.stderr)
        return 2
    base_url = os.getenv("GEMINI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    caches = list_caches(base_url, api_key)
    selected = select_dev_caches(caches, set(args.keep_build))
    print(f"Found {len(caches)} caches in the project; {len(selected)} are dev caches to remove.")
    for cache in selected:
        print(f"  {cache.name}  {cache.display_name}  expires={cache.expire_time}")

    if not args.delete:
        print("Dry run: nothing deleted. Re-run with --delete to delete the caches above.")
        return 0

    failures = 0
    for cache in selected:
        try:
            _request_json("DELETE", f"{base_url}/{cache.name}", api_key)
            print(f"Deleted {cache.name}")
        except RuntimeError as exc:
            failures += 1
            print(f"Failed to delete {cache.name}: {exc}", file=sys.stderr)
    print(f"Deleted {len(selected) - failures} of {len(selected)} dev caches.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
