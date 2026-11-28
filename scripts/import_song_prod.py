"""Validate and publish one exported Song, or remove a previously published Song."""

import argparse
import asyncio
import base64
import os
import shlex
from pathlib import Path

from firebase_admin import auth
from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed
from google.cloud import firestore

from scripts.local_song_bundle import (
    VERSION, bundle_path, decode, file_integrity, owned_job, path_component, read_json,
    require_song, storage_prefix, vocal_job_ids,
)
from src.backend.config import Settings
from src.backend.firebase_app import get_firestore_client
from src.backend.session import FirestoreSessionStore
from src.backend.storage_client import get_bucket

# Keep the copied job IDs for undo even if the player is edited after publishing.
_PUBLISHED_JOBS = "localPublishJobIds"
_SHARE_BASE = "https://sightsinger.app/s/"


def _read_documents(bundle, job_ids):
    jobs = {}
    for job_id in job_ids:
        path_component(job_id)
        jobs[job_id] = decode(read_json(bundle_path(bundle, f"jobs/{job_id}.json")))
    session = decode(read_json(bundle_path(bundle, "session.json")))
    player = decode(read_json(bundle_path(bundle, "player.json")))
    return session, player, jobs


def _load_bundle(bundle):
    manifest = read_json(bundle_path(bundle, "manifest.json"))
    if manifest["version"] != VERSION:
        raise ValueError("Unsupported bundle version.")
    local_uid, sid = manifest["local_uid"], manifest["session_id"]
    prefix = storage_prefix(local_uid, sid)
    objects = {}
    for entry in manifest["objects"]:
        name = entry["path"]
        path = bundle_path(bundle, "objects/" + name)
        actual = file_integrity(path)
        if name in objects or any(actual[key] != entry[key] for key in actual):
            raise ValueError(f"Duplicate or corrupt object in bundle: {name}")
        objects[name] = entry

    session, player, jobs = _read_documents(bundle, manifest["job_ids"])
    require_song(session)
    if (session["userId"] != local_uid or len(jobs) != len(manifest["job_ids"])
            or set(jobs) != set(vocal_job_ids(player))
            or any(not owned_job(job, local_uid, sid) for job in jobs.values())):
        raise ValueError("Bundle documents do not match the manifest and player.")

    fields = session.setdefault("files", {})
    for entry in manifest.get("local_paths", []):
        document, key = entry["field"].rsplit(".", 1)
        target = entry["storage_path"]
        record = fields if document == "files" else jobs[document.removeprefix("jobs/")]
        if record.get(key) != entry["local_path"]:
            raise ValueError("The local path report does not match the bundle documents.")
        if target is None:
            continue
        is_job = document != "files"
        if (not target.startswith(prefix) or target[len(prefix):] not in objects
                or (is_job and not target.startswith(f"{prefix}{document}/"))):
            raise ValueError("Local path has no corresponding Song object.")
        record[key] = target
        if document == "files" and key == "musicxml_path":
            record["active_musicxml_storage_path"] = target
        if document == "files" and key == "uploaded_musicxml_path":
            record["musicxml_storage_path"] = target

    for path in (fields.get("active_musicxml_storage_path"), session.get("currentScorePath")):
        if path and (not path.startswith(prefix) or path[len(prefix):] not in objects):
            raise ValueError("The Song references a score object missing from its bundle.")
    for job_id, job in jobs.items():
        for key in ("outputPath", "losslessOutputPath"):
            path = job.get(key)
            if path and (not path.startswith(f"{prefix}jobs/{job_id}/")
                         or path[len(prefix):] not in objects):
                raise ValueError(f"A copied job's {key} is missing from its bundle.")
    return sid, prefix, session, player, jobs, objects


def _rewrite(value, local_prefix, prod_prefix, uid, field, changes):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            child = f"{field}.{key}"
            if key in ("userId", "ownerUid"):
                result[key] = uid
                if item != uid:
                    changes[child] = uid
            else:
                result[key] = _rewrite(item, local_prefix, prod_prefix, uid, child, changes)
        return result
    if isinstance(value, list):
        return [_rewrite(item, local_prefix, prod_prefix, uid, f"{field}[{i}]", changes)
                for i, item in enumerate(value)]
    if isinstance(value, str) and value.startswith(local_prefix):
        rewritten = prod_prefix + value[len(local_prefix):]
        if rewritten != value:
            changes[field] = rewritten
        return rewritten
    return value


def _existing_share(db, session, sid, uid):
    token = session.get("shareToken")
    if not token:
        return None
    ref = db.collection("shares").document(path_component(token))
    share = ref.get().to_dict()
    if share and share.get("ownerUid") == uid and share.get("sessionId") == sid:
        return ref
    raise ValueError("The existing Song's share token does not match its share document.")


def _checked_user(uid, get_user):
    path_component(uid)
    try:
        get_user(uid)
    except auth.UserNotFoundError as exc:
        raise ValueError("The destination Firebase Auth user does not exist.") from exc


def _upload(bundle, bucket, prefix, objects, replace):
    for name, entry in objects.items():
        blob = bucket.blob(prefix + name)
        blob.content_encoding = entry.get("content_encoding")
        previous = bucket.get_blob(blob.name) if replace else None
        try:
            blob.upload_from_filename(
                str(bundle_path(bundle, "objects/" + name)),
                content_type=entry.get("content_type"),
                if_generation_match=previous.generation if previous else 0,
            )
        except PreconditionFailed:
            # An interrupted non-replacement import can already have staged this object.
            existing = bucket.get_blob(blob.name)
            expected_md5 = base64.b64encode(bytes.fromhex(entry["md5"])).decode("ascii")
            if (replace or existing is None or existing.size != entry["size"]
                    or existing.md5_hash != expected_md5):
                raise


def import_song(db, bucket, store, bundle, uid, *, get_user, dry_run=True,
                replace=False, share=False):
    sid, local_prefix, session, player, jobs, objects = _load_bundle(bundle)
    _checked_user(uid, get_user)
    prod_prefix = storage_prefix(uid, sid)
    session_ref = db.collection("sessions").document(sid)
    player_ref = session_ref.collection("player").document("current")
    previous = session_ref.get()
    existing = previous.to_dict() or {}
    if previous.exists and not replace:
        raise ValueError("The destination session exists. Use --replace to update it.")
    if previous.exists and existing.get("userId") != uid:
        raise ValueError("The destination session belongs to a different user.")
    share_ref = _existing_share(db, existing, sid, uid)
    if not previous.exists and player_ref.get().exists:
        raise ValueError("The destination contains a player without its session.")

    session.pop("shareToken", None)
    changes = {}
    session = _rewrite(session, local_prefix, prod_prefix, uid, session_ref.path, changes)
    player = _rewrite(player, local_prefix, prod_prefix, uid, player_ref.path, changes)
    if share_ref:
        session["shareToken"] = share_ref.id
    session[_PUBLISHED_JOBS] = sorted(set(existing.get(_PUBLISHED_JOBS, ())) | jobs.keys())

    job_writes = []
    for job_id, job in jobs.items():
        ref = db.collection("jobs").document(job_id)
        snapshot = ref.get()
        if snapshot.exists and not owned_job(snapshot.to_dict(), uid, sid):
            raise ValueError("A destination job belongs to a different Song or user.")
        job_writes.append((ref, snapshot.update_time,
                           _rewrite(job, local_prefix, prod_prefix, uid, ref.path, changes)))

    if dry_run:
        print("DRY_RUN=1. Planned writes:")
    for path, entry in objects.items():
        print(f"  upload gs://{bucket.name}/{prod_prefix}{path} ({entry['size']} bytes)")
    for path in [*(ref.path for ref, _, _ in job_writes), session_ref.path, player_ref.path]:
        print(f"  write {path}")
    if share and not share_ref:
        print("  update Song shareToken and create shares/<new token>")
    for field, value in changes.items():
        print(f"  {field} -> {value}")
    undo = shlex.join(["scripts/import_song_prod.sh", "--remove", sid, "--prod-uid", uid])
    if dry_run:
        print(f"Undo after publishing (dry run by default): {undo}")
        return None

    _upload(bundle, bucket, prod_prefix, objects, replace)

    @firestore.transactional
    def commit(transaction):
        if session_ref.get(transaction=transaction).update_time != previous.update_time:
            raise ValueError("The destination session changed during the import.")
        for ref, updated, _ in job_writes:
            if ref.get(transaction=transaction).update_time != updated:
                raise ValueError("A destination job changed during the import.")
        for ref, _, job in job_writes:
            transaction.set(ref, job)
        transaction.set(session_ref, session)
        transaction.set(player_ref, player)

    commit(db.transaction())
    token = asyncio.run(store.share_on(sid, uid)) if share else None
    print(f"Published one Song, {len(jobs)} jobs and {len(objects)} objects.")
    if token:
        print(_SHARE_BASE + token)
    print(f"Undo (dry run by default): {undo}")
    return token


def remove_song(db, bucket, store, sid, uid, *, get_user, dry_run=True):
    prefix = storage_prefix(uid, sid)
    _checked_user(uid, get_user)
    session_ref = db.collection("sessions").document(sid)
    snapshot = session_ref.get()
    session = snapshot.to_dict()
    require_song(session)
    if session.get("userId") != uid:
        raise ValueError("The destination session belongs to a different user.")
    if _PUBLISHED_JOBS not in session:
        raise ValueError("The destination Song was not published with this command.")
    share_ref = _existing_share(db, session, sid, uid)
    player_ref = session_ref.collection("player").document("current")
    jobs_to_delete = []
    for job_id in session[_PUBLISHED_JOBS]:
        ref = db.collection("jobs").document(path_component(job_id))
        job = ref.get().to_dict()
        if job and owned_job(job, uid, sid):
            jobs_to_delete.append(ref)
    blobs = list(bucket.list_blobs(prefix=prefix))

    if dry_run:
        print("DRY_RUN=1. Planned removals:")
    if share_ref:
        print(f"  turn sharing off and delete {share_ref.path}")
    for ref in [*jobs_to_delete, player_ref, session_ref]:
        print(f"  delete {ref.path}")
    for blob in blobs:
        print(f"  delete gs://{bucket.name}/{blob.name} ({blob.size} bytes)")
    if dry_run:
        return

    if share_ref:
        asyncio.run(store.share_off(sid, uid))
        snapshot = session_ref.get()
        if snapshot.to_dict() != {key: value for key, value in session.items() if key != "shareToken"}:
            raise ValueError("The destination session changed during removal.")
    for blob in blobs:
        try:
            blob.delete(if_generation_match=blob.generation)
        except NotFound:
            pass

    @firestore.transactional
    def commit(transaction):
        if session_ref.get(transaction=transaction).update_time != snapshot.update_time:
            raise ValueError("The destination session changed during removal.")
        owned_jobs = []
        for ref in jobs_to_delete:
            job = ref.get(transaction=transaction).to_dict()
            if job and owned_job(job, uid, sid):
                owned_jobs.append(ref)
        for ref in owned_jobs:
            transaction.delete(ref)
        transaction.delete(player_ref)
        transaction.delete(session_ref)

    commit(db.transaction())
    print(f"Removed one Song and {len(blobs)} objects.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("out_dir", type=Path, nargs="?")
    target.add_argument("--remove", metavar="SESSION_ID")
    parser.add_argument("--prod-uid", required=True)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--share", action="store_true")
    args = parser.parse_args()
    if args.remove and (args.replace or args.share):
        parser.error("--remove cannot be combined with --replace or --share.")
    dry_run = os.environ.get("DRY_RUN", "1")
    if dry_run not in {"0", "1"}:
        parser.error("DRY_RUN must be 0 or 1.")

    try:
        settings = Settings.from_env()
        db = get_firestore_client()
        bucket = get_bucket(settings.storage_bucket)
        store = FirestoreSessionStore(
            project_root=settings.project_root,
            sessions_dir=settings.sessions_dir,
            ttl_seconds=settings.session_ttl_seconds,
            max_sessions=settings.max_sessions,
            backend_use_storage=True,
            storage_bucket=settings.storage_bucket,
        )
        if args.remove:
            remove_song(db, bucket, store, args.remove, args.prod_uid,
                        get_user=auth.get_user, dry_run=dry_run == "1")
        else:
            import_song(db, bucket, store, args.out_dir, args.prod_uid,
                        get_user=auth.get_user, dry_run=dry_run == "1",
                        replace=args.replace, share=args.share)
    except (OSError, ValueError, KeyError, TypeError, GoogleAPICallError) as exc:
        parser.exit(1, f"Publish command failed: {exc}\n")


if __name__ == "__main__":
    main()
