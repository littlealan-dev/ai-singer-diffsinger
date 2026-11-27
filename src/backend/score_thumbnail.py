from __future__ import annotations

"""A Song's score thumbnail: the top of page 1, for share link previews and My songs rows.

Verovio engraves the stored MusicXML to SVG and resvg turns it into a PNG. Both run on the server,
so the image can only ever show the score (docs/specs/share-link-preview-spec.md §2).
"""

import base64
import io
import re
import os
import tempfile
import threading
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from PIL import Image, ImageChops

WIDTH, HEIGHT = 1200, 630
SMALL_WIDTH, SMALL_HEIGHT = 240, 126
BADGE_SIZE, BADGE_MARGIN = 64, 20
# Verovio lays out every measure it's given, though the thumbnail shows only the top of page 1.
# Lines fill left to right, so later measures can't change the first lines; 40 covers the top
# of page 1 even for a single-staff lead sheet with short lines.
MEASURE_LIMIT = 40
ThumbnailSize = Literal["large", "small"]

_BADGE_PATH = Path(__file__).resolve().parent / "images" / "thumbnail_badge.png"
# Verovio lays out an A4 page in tenths of a millimetre; scale 40 keeps staves readable at 1200 wide.
_VEROVIO_OPTIONS = {
    "pageWidth": 2100, "pageHeight": 2970, "scale": 40,
    "header": "auto", "footer": "none", "breaks": "auto", "svgViewBox": True,
}


# Renders run in worker threads; the first ones of a process would otherwise all convert the fonts.
_FONT_LOCK = threading.Lock()


class ThumbnailError(RuntimeError):
    """The score could not be rendered."""


def _verovio_data_dir() -> Path:
    import verovio

    return Path(verovio.__file__).resolve().parent / "data"


@lru_cache(maxsize=1)
def _bundled_font_files() -> tuple[str, ...]:
    """Verovio's own fonts as TTF files: Leipzig (music text, e.g. the tempo's note) and Liberation (text).

    Verovio ships them as WOFF2 inside CSS, which resvg can't read. Converted once per process. The
    server image adds CJK and Thai system fonts for other scripts.
    """
    from fontTools.ttLib import TTFont

    data_dir = _verovio_data_dir()
    out_dir = Path(tempfile.mkdtemp(prefix="verovio-fonts-"))
    files: list[str] = []
    for css_name in ("Leipzig.css", "Liberation.css"):
        css = (data_dir / css_name).read_text(encoding="utf-8")
        for index, encoded in enumerate(re.findall(r"base64,([A-Za-z0-9+/=]+)", css)):
            font = TTFont(io.BytesIO(base64.b64decode(encoded)))
            font.flavor = None
            path = out_dir / f"{css_name[:-4]}-{index}.ttf"
            font.save(str(path))
            files.append(str(path))
    return tuple(files)


def render_thumbnail(musicxml: str) -> tuple[bytes, bytes]:
    """The large (1200×630) and small (240×126) PNGs of a score's first page top."""
    import resvg_py
    import verovio

    with _FONT_LOCK:
        font_files = list(_bundled_font_files())
    verovio.enableLog(verovio.LOG_OFF)
    # Off the main thread (the server renders in a worker thread) Verovio's default resource path
    # is a build-time folder, so its fonts fail to load: point it at the package's data.
    toolkit = verovio.toolkit(False)
    toolkit.setResourcePath(str(_verovio_data_dir()))
    toolkit.setOptions(_VEROVIO_OPTIONS)
    if not toolkit.loadData(first_measures(musicxml)) or toolkit.getPageCount() < 1:
        raise ThumbnailError("Verovio could not read the score.")
    svg = toolkit.renderToSVG(1)
    png = bytes(resvg_py.svg_to_bytes(
        svg_string=svg, width=WIDTH, background="white",
        font_files=font_files, serif_family="Liberation Serif",
    ))
    page = Image.open(io.BytesIO(png)).convert("RGB")
    top = page.crop((0, 0, WIDTH, min(HEIGHT, page.height)))
    large = Image.new("RGB", (WIDTH, HEIGHT), "white")
    large.paste(top, (0, _vertical_offset(top)))
    _paste_badge(large)
    small = large.resize((SMALL_WIDTH, SMALL_HEIGHT), Image.LANCZOS)
    return _png_bytes(large), _png_bytes(small)


def first_measures(musicxml: str, limit: Optional[int] = None) -> str:
    """The score cut to its first measures; a file this can't read is passed on unchanged."""
    keep = MEASURE_LIMIT if limit is None else limit
    try:
        root = ET.fromstring(musicxml.encode("utf-8"))
    except ET.ParseError:
        return musicxml
    if root.tag == "score-partwise":
        holders = root.findall("part")
    elif root.tag == "score-timewise":
        holders = [root]
    else:
        return musicxml
    cut = False
    for holder in holders:
        for measure in holder.findall("measure")[keep:]:
            holder.remove(measure)
            cut = True
    return ET.tostring(root, encoding="unicode") if cut else musicxml


def _vertical_offset(top: Image.Image) -> int:
    """Centre a short score's music in the frame; a taller one starts at the top of the page."""
    box = ImageChops.difference(top, Image.new("RGB", top.size, "white")).getbbox()
    if box is None:
        return 0
    content_height = box[3] - box[1]
    return max(0, (HEIGHT - content_height) // 2 - box[1])


def _paste_badge(image: Image.Image) -> None:
    """The SightSinger icon in the bottom-right corner (D2). Without the file, the thumbnail has none."""
    if not _BADGE_PATH.is_file():
        return
    badge = Image.open(_BADGE_PATH).convert("RGBA").resize((BADGE_SIZE, BADGE_SIZE), Image.LANCZOS)
    position = (image.width - BADGE_SIZE - BADGE_MARGIN, image.height - BADGE_SIZE - BADGE_MARGIN)
    image.paste(badge, position, badge)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def thumbnail_object_path(session_id: str, version: int, size: ThumbnailSize) -> str:
    """Where a thumbnail lives: in the bucket, or under the session's folder without storage."""
    suffix = "" if size == "large" else "-small"
    return f"thumbnails/{session_id}/v{version}{suffix}.png"


def store_thumbnail(settings, session_dir: Path, session_id: str, version: int,
                    large: bytes, small: bytes) -> None:
    from src.backend.storage_client import upload_bytes

    for size, data in (("large", large), ("small", small)):
        path = thumbnail_object_path(session_id, version, size)
        if settings.backend_use_storage:
            upload_bytes(settings.storage_bucket, data, path, "image/png")
        else:
            target = session_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write aside, then rename: a request reading the same Song's thumbnail meanwhile sees
            # the old file or the new one, never half of one.
            handle, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.replace(temp, target)


def load_thumbnail(settings, session_dir: Path, session_id: str, version: int,
                   size: ThumbnailSize) -> Optional[bytes]:
    from google.api_core.exceptions import NotFound

    from src.backend.storage_client import download_bytes

    path = thumbnail_object_path(session_id, version, size)
    if settings.backend_use_storage:
        try:
            return download_bytes(settings.storage_bucket, path)
        except NotFound:
            return None
    target = session_dir / path
    return target.read_bytes() if target.is_file() else None


def stored_version(files: object) -> Optional[int]:
    """The score version a Song's thumbnail was made from, kept in its files map."""
    raw = files.get("thumbnail_version") if isinstance(files, dict) else None
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None
