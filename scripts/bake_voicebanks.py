#!/usr/bin/env python3
"""Download and extract the enabled voicebanks for an image build.

Cloud Build runs this before the Docker build of each synthesis worker image;
the Dockerfile copies the output folder to /app/voicebanks, where the worker
reads voicebanks in prod instead of downloading them. Standard library only:
it runs in the Cloud SDK image, which has python3 and gcloud but not the app's
packages.

    bake_voicebanks.py --manifest env/voicebank_manifest.prod.json --bucket <bucket> --out baked_voicebanks
    bake_voicebanks.py --manifest <manifest> --source-dir <folder of archives> --out <dir>   # no network (tests)

Each enabled voicebank is extracted into <out>/<id>/, the layout the worker
expects (src/api/voicebank_cache.py). Any failure exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path


def enabled_voicebanks(manifest_path: Path) -> list[tuple[str, str]]:
    """Return (id, storage_object) for each enabled manifest entry."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = []
    for entry in data["voicebanks"]:
        if entry.get("enabled") is not True:
            continue
        voicebank_id = entry.get("id")
        archive_object = entry.get("storage_object")
        if not isinstance(voicebank_id, str) or not voicebank_id or "/" in voicebank_id or "\\" in voicebank_id:
            raise ValueError(f"Bad voicebank id in manifest: {voicebank_id!r}")
        if not isinstance(archive_object, str) or not archive_object.strip():
            raise ValueError(f"Manifest entry missing storage_object for voicebank: {voicebank_id}")
        entries.append((voicebank_id, archive_object))
    return entries


def extract_tarball(archive_path: Path, dest_dir: Path) -> None:
    """Extract a tar.gz archive with the same path traversal guard as the app."""
    base_dir = dest_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            resolved = (dest_dir / member.name).resolve()
            if base_dir not in resolved.parents and resolved != base_dir:
                raise ValueError(f"Tar archive contains unsafe paths: {archive_path.name}")
        tar.extractall(dest_dir)


def fetch_archive(archive_object: str, destination: Path, bucket: str | None, source_dir: Path | None) -> None:
    """Copy one voicebank archive to destination, from the bucket or a local folder."""
    if source_dir is not None:
        source = source_dir / Path(archive_object).name
        if not source.is_file():
            raise FileNotFoundError(f"Voicebank archive not found: {source}")
        shutil.copyfile(source, destination)
        return
    subprocess.run(
        ["gcloud", "storage", "cp", f"gs://{bucket}/{archive_object}", str(destination)],
        check=True,
    )


def bake(manifest_path: Path, out_dir: Path, bucket: str | None, source_dir: Path | None) -> list[str]:
    """Extract every enabled voicebank into out_dir/<id>/ and return their IDs."""
    entries = enabled_voicebanks(manifest_path)
    if not entries:
        raise ValueError(f"No enabled voicebanks in {manifest_path}")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for voicebank_id, archive_object in entries:
        start = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / Path(archive_object).name
            fetch_archive(archive_object, archive_path, bucket, source_dir)
            size = archive_path.stat().st_size
            target = out_dir / voicebank_id
            target.mkdir()
            extract_tarball(archive_path, target)
        print(f"baked {voicebank_id} bytes={size} elapsed_s={time.monotonic() - start:.1f}", flush=True)
    return [voicebank_id for voicebank_id, _ in entries]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bucket")
    source.add_argument("--source-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        baked = bake(args.manifest, args.out, args.bucket, args.source_dir)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"bake_voicebanks failed: {exc}", file=sys.stderr)
        return 1
    print(f"baked {len(baked)} voicebanks into {args.out}: {', '.join(baked)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
