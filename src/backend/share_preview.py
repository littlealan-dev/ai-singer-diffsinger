from __future__ import annotations

"""Link preview tags for share links (docs/specs/share-link-preview-spec.md §1, §3).

WhatsApp, iMessage, Facebook, Reddit and Slack build a card from a page's <title> and og: tags
without running its JavaScript, so GET /s/{token} returns the app's own page with the Song's tags
in its <head>.
"""

import html
import re
import time
from typing import Any, Dict, Optional

import httpx

DESCRIPTION_LIMIT = 200
SHELL_CACHE_SECONDS = 60
GENERIC_TITLE = "SightSinger"
GENERIC_DESCRIPTION = "Listen to the sung parts and follow the score."
GENERIC_IMAGE_PATH = "/landing/ss_screenshot_og.jpg"

_shell_cache: Dict[str, Any] = {"url": None, "html": None, "at": 0.0}


def song_tags(*, title: Optional[str], info: Optional[Dict[str, Any]], share_url: str,
              image_url: Optional[str], base_url: str) -> Dict[str, Any]:
    """The card for a live share link: the Song's title, description and thumbnail."""
    return {
        "title": f"{(title or '').strip() or 'Untitled song'} · SightSinger",
        "description": song_description(info),
        "image": image_url or f"{base_url}{GENERIC_IMAGE_PATH}",
        "url": share_url,
    }


def generic_tags(*, share_url: str, base_url: str) -> Dict[str, Any]:
    """The card for a link that is off or unknown: never the Song's title."""
    return {"title": GENERIC_TITLE, "description": GENERIC_DESCRIPTION,
            "image": f"{base_url}{GENERIC_IMAGE_PATH}", "url": share_url}


def song_description(info: Optional[Dict[str, Any]]) -> str:
    """Song Info's description, or one built from the score's credits when it is empty (D1)."""
    info = info or {}
    description = " ".join(str(info.get("description") or "").split())
    if not description:
        credits = [str(info.get(key) or "").strip() for key in ("composer", "arranger")]
        description = " · ".join([value for value in credits if value] + [GENERIC_DESCRIPTION])
    if len(description) <= DESCRIPTION_LIMIT:
        return description
    cut = description[:DESCRIPTION_LIMIT].rsplit(" ", 1)[0].rstrip(" ,.;:·")
    return f"{cut}…"


def head_tags(tags: Dict[str, Any]) -> str:
    """The <title> and meta tags, every value escaped."""
    escape = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    title, description = escape(tags["title"]), escape(tags["description"])
    image, url = escape(tags["image"]), escape(tags["url"])
    lines = [
        f"<title>{title}</title>",
        f'<meta name="description" content="{description}" />',
        f'<meta property="og:title" content="{title}" />',
        f'<meta property="og:description" content="{description}" />',
        f'<meta property="og:image" content="{image}" />',
        '<meta property="og:image:width" content="1200" />',
        '<meta property="og:image:height" content="630" />',
        f'<meta property="og:url" content="{url}" />',
        '<meta property="og:type" content="website" />',
        '<meta property="og:site_name" content="SightSinger" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        f'<meta name="twitter:title" content="{title}" />',
        f'<meta name="twitter:description" content="{description}" />',
        f'<meta name="twitter:image" content="{image}" />',
    ]
    return "\n    ".join(lines)


def inject(shell: str, tags: Dict[str, Any]) -> str:
    """The app's page with its <title> replaced by the share link's tags."""
    block = head_tags(tags)
    without_title = re.sub(r"<title>.*?</title>", "", shell, count=1, flags=re.IGNORECASE | re.DOTALL)
    if re.search(r"</head>", without_title, flags=re.IGNORECASE):
        return re.sub(r"</head>", f"    {block}\n  </head>", without_title, count=1, flags=re.IGNORECASE)
    return f"<!doctype html><html><head>{block}</head><body>{without_title}</body></html>"


def fallback_page(tags: Dict[str, Any], *, app_url: str) -> str:
    """When the app's page can't be fetched: the tags, and a link that opens the Song in the app."""
    link = html.escape(app_url, quote=True)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\" />"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />"
        f"{head_tags(tags)}</head><body><p><a href=\"{link}\">Open the song</a></p></body></html>"
    )


async def app_shell(url: str, *, now: Optional[float] = None) -> Optional[str]:
    """The live app page (app.html), cached for a minute so a deploy shows up quickly."""
    current = time.monotonic() if now is None else now
    if (_shell_cache["url"] == url and _shell_cache["html"] is not None
            and current - _shell_cache["at"] < SHELL_CACHE_SECONDS):
        return _shell_cache["html"]
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"Accept": "text/html"})
        if response.status_code != 200 or "<head" not in response.text.lower():
            return None
    except httpx.HTTPError:
        return None
    _shell_cache.update(url=url, html=response.text, at=current)
    return response.text
