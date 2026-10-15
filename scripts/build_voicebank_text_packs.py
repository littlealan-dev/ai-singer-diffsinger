"""Build and upload each voicebank's text pack: its text and config files, no models.

Tools that read a voicebank's dictionaries but not its models (the birthday-name
check on the CPU-only API) fetch this pack instead of the full archive. The pack
keeps the archive's paths, so it resolves like the full voicebank; it is stored
next to the archive as <archive>.text.tar.gz. Each archive is read from Cloud
Storage as a stream, so nothing the size of a voicebank is written to disk.

Run once, and again after a voicebank archive changes:

    python scripts/build_voicebank_text_packs.py --dry-run   # build and check only
    python scripts/build_voicebank_text_packs.py             # build and upload

Needs credentials that can read and write the voicebank bucket
(gcloud auth application-default login).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.api.voicebank_cache import (  # noqa: E402
    discover_voicebank_root,
    is_voicebank_text_file,
    voicebank_text_pack_object,
)
from src.backend.storage_client import get_bucket  # noqa: E402

DEFAULT_MANIFEST = ROOT / "env" / "voicebank_manifest.prod.json"
DEFAULT_BUCKET = "sightsinger-app.firebasestorage.app"


def build_text_pack(archive_stream, pack_path: Path) -> tuple[int, int]:
    """Copy the text members of a streamed tar.gz into a new tar.gz; return (files, bytes)."""
    files = size = 0
    with tarfile.open(fileobj=archive_stream, mode="r|gz") as archive, tarfile.open(
        pack_path, "w:gz"
    ) as pack:
        for member in archive:
            if not member.isfile() or not is_voicebank_text_file(member.name):
                continue
            data = archive.extractfile(member).read()
            pack.addfile(member, io.BytesIO(data))
            files += 1
            size += len(data)
    return files, size


def check_text_pack(pack_path: Path) -> None:
    """Fail unless the pack unpacks to a voicebank root (dsconfig.yaml and character file)."""
    with tempfile.TemporaryDirectory() as unpacked, tarfile.open(pack_path, "r:gz") as pack:
        base = Path(unpacked).resolve()
        for member in pack.getmembers():
            target = (base / member.name).resolve()
            if base not in target.parents:
                raise ValueError(f"Unsafe path in pack: {member.name}")
        pack.extractall(unpacked)
        if discover_voicebank_root(Path(unpacked)) is None:
            raise ValueError("The pack has no voicebank root (dsconfig.yaml with a character file).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--voicebank", action="append", help="only this voicebank ID (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="build and check, do not upload")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    entries = manifest.get("voicebanks") if isinstance(manifest, dict) else manifest
    selected = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("enabled", True)
        and (not args.voicebank or entry.get("id") in args.voicebank)
    ]
    if not selected:
        print("No voicebanks selected.")
        return 1
    bucket = get_bucket(args.bucket)
    with tempfile.TemporaryDirectory() as work:
        for entry in selected:
            archive_object = entry["storage_object"]
            pack_object = voicebank_text_pack_object(archive_object)
            pack_path = Path(work) / Path(pack_object).name
            with bucket.blob(archive_object).open("rb") as archive_stream:
                files, size = build_text_pack(archive_stream, pack_path)
            check_text_pack(pack_path)
            packed = pack_path.stat().st_size
            line = (
                f"{entry['id']}: {files} files, {size / 1e6:.2f} MB unpacked, "
                f"{packed / 1e6:.2f} MB packed -> gs://{args.bucket}/{pack_object}"
            )
            if args.dry_run:
                print(line + " (dry run, not uploaded)")
                continue
            bucket.blob(pack_object).upload_from_filename(
                str(pack_path), content_type="application/gzip"
            )
            print(line + " (uploaded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
