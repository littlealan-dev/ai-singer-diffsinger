from __future__ import annotations

"""Shared MusicXML file reading helpers."""

from pathlib import Path
import os
import zipfile
from xml.etree import ElementTree


_CONTAINER_XML_MAX_BYTES = 1024 * 1024


class MusicXmlArchiveError(ValueError):
    """Raised when an MXL archive is malformed or missing score content."""


class MusicXmlArchiveTooLargeError(MusicXmlArchiveError):
    """Raised when an MXL archive entry exceeds the configured read limit."""


def read_musicxml_content(
    path: Path,
    *,
    max_mxl_uncompressed_bytes: int | None = None,
) -> str:
    """Read MusicXML content from .xml or bounded .mxl inputs."""
    if path.suffix.lower() != ".mxl":
        return path.read_text(encoding="utf-8", errors="replace")
    if max_mxl_uncompressed_bytes is None:
        max_mxl_uncompressed_bytes = _default_mxl_uncompressed_bytes()
    try:
        with zipfile.ZipFile(path) as archive:
            xml_name = _find_mxl_xml(archive, max_mxl_uncompressed_bytes)
            xml_bytes = _read_archive_entry_bounded(
                archive,
                xml_name,
                max_mxl_uncompressed_bytes,
            )
    except zipfile.BadZipFile as exc:
        raise MusicXmlArchiveError("Invalid MXL archive.") from exc
    return xml_bytes.decode("utf-8", errors="replace")


def _default_mxl_uncompressed_bytes() -> int:
    """Return the shared default max uncompressed MXL size in bytes."""
    value = os.getenv("BACKEND_MAX_MXL_UNCOMPRESSED_MB")
    mb = int(value) if value not in (None, "") else 20
    return mb * 1024 * 1024


def _find_mxl_xml(archive: zipfile.ZipFile, max_mxl_uncompressed_bytes: int) -> str:
    """Find the primary score XML entry inside an MXL archive."""
    try:
        container_bytes = _read_archive_entry_bounded(
            archive,
            "META-INF/container.xml",
            min(max_mxl_uncompressed_bytes, _CONTAINER_XML_MAX_BYTES),
        )
    except KeyError:
        return _first_mxl_xml(archive)
    try:
        root = ElementTree.fromstring(container_bytes)
    except ElementTree.ParseError:
        return _first_mxl_xml(archive)
    for elem in root.iter():
        if elem.tag.endswith("rootfile"):
            full_path = elem.attrib.get("full-path")
            if full_path and full_path in archive.namelist():
                return full_path
    return _first_mxl_xml(archive)


def _first_mxl_xml(archive: zipfile.ZipFile) -> str:
    """Return the first non-container XML entry in an MXL archive."""
    candidates = [
        name
        for name in archive.namelist()
        if name.lower().endswith(".xml") and not name.startswith("META-INF/")
    ]
    if not candidates:
        raise MusicXmlArchiveError("No MusicXML file found in archive.")
    return candidates[0]


def _read_archive_entry_bounded(
    archive: zipfile.ZipFile,
    entry_name: str,
    max_bytes: int,
) -> bytes:
    """Read a zip entry with both metadata and streamed size enforcement."""
    try:
        info = archive.getinfo(entry_name)
    except KeyError:
        raise
    if info.file_size > max_bytes:
        raise MusicXmlArchiveTooLargeError(
            f"MXL entry exceeds {max_bytes} bytes after decompression."
        )
    total = 0
    chunks: list[bytes] = []
    try:
        with archive.open(info, "r") as handle:
            while True:
                chunk = handle.read(min(64 * 1024, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise MusicXmlArchiveTooLargeError(
                        f"MXL entry exceeds {max_bytes} bytes after decompression."
                    )
                chunks.append(chunk)
    except MusicXmlArchiveTooLargeError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise MusicXmlArchiveError(f"Invalid MXL archive entry: {entry_name}") from exc
    return b"".join(chunks)
