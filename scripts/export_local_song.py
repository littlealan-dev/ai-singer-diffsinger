"""Export a rendered Song and its files from the local Firebase emulators."""

import argparse
import mimetypes
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore import Client

from scripts.local_song_bundle import (
    VERSION, bundle_path, encode, file_integrity, owned_job, path_component,
    require_song,
    storage_prefix, vocal_job_ids, write_json,
)
from src.backend.config import Settings
from src.backend.storage_client import get_bucket

_FILE_STORAGE_KEYS = {
    "musicxml_path": "active_musicxml_storage_path",
    "uploaded_musicxml_path": "musicxml_storage_path",
}


def _copy_disk_paths(fields, *, prefix, session_dir, project_root, objects_dir, objects,
                     report, document, job_id=None):
    """Resolve local file references only inside the exported session directory."""
    for key, value in fields.items():
        if not isinstance(value, str) or not value or value.startswith(prefix):
            continue
        recorded_path = Path(value)
        source = project_root / recorded_path
        storage_key = _FILE_STORAGE_KEYS.get(key)
        if not (job_id or storage_key or recorded_path.is_absolute()
                or source.is_relative_to(session_dir)):
            continue

        # Another local backend may have written the cached path. Prefer the
        # exact stored sibling when present, and never read outside this Song.
        existing = fields.get(storage_key) if storage_key else None
        source = source.resolve()
        if isinstance(existing, str) and existing.startswith(prefix) and existing[len(prefix):] in objects:
            target = existing[len(prefix):]
        elif source.is_relative_to(session_dir) and source.is_file():
            if job_id:
                filename = "source.wav" if key == "losslessOutputPath" else f"output{source.suffix.lower()}"
                target = f"jobs/{job_id}/{filename}"
            else:
                target = source.relative_to(session_dir).as_posix()
            path = bundle_path(objects_dir, target)
            if target not in objects:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, path)
                objects[target] = {"path": target, **file_integrity(path),
                                   "content_type": mimetypes.guess_type(path.name)[0]}
            elif file_integrity(source) != file_integrity(path):
                raise ValueError(f"{document}.{key} conflicts with a stored object.")
        else:
            report.append({"field": f"{document}.{key}", "local_path": value, "storage_path": None})
            if job_id or key == "musicxml_path":
                raise ValueError(f"{document}.{key} has no local file or storage object.")
            continue
        report.append({"field": f"{document}.{key}", "local_path": value,
                       "storage_path": prefix + target})


def export_song(db, bucket, sid, out_dir, *, project_root, sessions_dir):
    session_ref = db.collection("sessions").document(path_component(sid))
    session = session_ref.get().to_dict()
    require_song(session)
    uid = session["userId"]
    prefix = storage_prefix(uid, sid)
    player = session_ref.collection("player").document("current").get().to_dict()
    if player is None:
        raise ValueError("The Song has no player record.")
    jobs = {}
    for job_id in vocal_job_ids(player):
        job = db.collection("jobs").document(job_id).get().to_dict()
        if not job or not owned_job(job, uid, sid):
            raise ValueError("The player references a missing or unrelated job.")
        jobs[job_id] = job

    out_dir = out_dir.resolve()
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise ValueError("Choose a new or empty export directory.")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out_dir.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        objects_dir = bundle / "objects"
        (bundle / "jobs").mkdir(parents=True)
        objects_dir.mkdir()
        objects = {}
        for blob in bucket.list_blobs(prefix=prefix):
            relative = blob.name[len(prefix):]
            path = bundle_path(objects_dir, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(path), raw_download=True)
            objects[relative] = {"path": relative, **file_integrity(path),
                                 "content_type": blob.content_type,
                                 "content_encoding": blob.content_encoding}

        report = []
        session_dir = (sessions_dir / sid).resolve()
        _copy_disk_paths(session.get("files", {}), prefix=prefix, session_dir=session_dir,
                         project_root=project_root, objects_dir=objects_dir, objects=objects,
                         report=report, document="files")
        for job_id, job in jobs.items():
            paths = {key: job.get(key) for key in ("outputPath", "losslessOutputPath")}
            _copy_disk_paths(paths, prefix=prefix, session_dir=session_dir,
                             project_root=project_root, objects_dir=objects_dir, objects=objects,
                             report=report, document=f"jobs/{job_id}", job_id=job_id)
            write_json(bundle / "jobs" / f"{job_id}.json", encode(job))
        write_json(bundle / "session.json", encode(session))
        write_json(bundle / "player.json", encode(player))
        write_json(bundle / "manifest.json", {
            "version": VERSION, "local_uid": uid, "session_id": sid, "job_ids": list(jobs),
            "objects": [objects[path] for path in sorted(objects)], "local_paths": report,
        })
        bundle.rename(out_dir)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_session_id")
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    if not os.environ.get("FIRESTORE_EMULATOR_HOST") or not os.environ.get("STORAGE_EMULATOR_HOST"):
        parser.error("Both local emulators must be configured.")
    settings = Settings.from_env()
    db = Client(project=os.environ["GOOGLE_CLOUD_PROJECT"], credentials=AnonymousCredentials())
    try:
        report = export_song(db, get_bucket(settings.storage_bucket), args.local_session_id,
                             args.out_dir, project_root=settings.project_root,
                             sessions_dir=settings.sessions_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")
    print(f"Exported Song to {args.out_dir}")
    for entry in report:
        target = entry["storage_path"] or "file unavailable"
        print(f'{entry["field"]}: {entry["local_path"]} -> {target}')


if __name__ == "__main__":
    main()
