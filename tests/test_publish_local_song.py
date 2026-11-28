"""Publish a made-up Song between two Firebase emulator projects."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import firebase_admin
import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore, storage

from scripts.export_local_song import export_song
from scripts.import_song_prod import import_song, remove_song
from src.backend import job_store, session, share_app, share_preview, storage_client
from tests.conftest import _stop_emulator

ROOT = Path(__file__).resolve().parents[1]
LOCAL_UID = "example-local-owner"
PROD_UID = "example-destination-owner"
SONG = "example-render"
TAKE = "example-vocal"
NOW = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
XML = "<score-partwise/>"
AUDIO = b"abcdefgh"
XML_PATH = "scores/test-score/upload/score.xml"
JSON_PATH = "scores/test-score/versions/1/score.json"
AUDIO_PATH = f"jobs/{TAKE}/output.wav"
MIDI_PATH = "instrumental-midi/written.mid"


def _listening(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module")
def publish_emulators(firestore_emulator, tmp_path_factory):
    """Add Auth and Storage to the suite's Firestore emulator."""
    for port in (9099, 9199, 4401, 4501):
        if _listening(port):
            pytest.fail(f"Publishing test needs port {port} to be free.", pytrace=False)
    path = tmp_path_factory.mktemp("publish-emulators")
    config = path / "firebase.json"
    config.write_text(json.dumps({
        "storage": {"rules": str(ROOT / "storage.rules")},
        "emulators": {"auth": {"port": 9099}, "storage": {"port": 9199},
                      "hub": {"port": 4401}, "logging": {"port": 4501},
                      "ui": {"enabled": False}, "singleProjectMode": False},
    }))
    with (path / "emulator.log").open("w+") as log:
        process = subprocess.Popen(
            ["npx", "firebase", "emulators:start", "--only", "auth,storage",
             "--project", "demo-publish-songs", "--config", str(config)],
            cwd=path, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 45
            while not all(_listening(port) for port in (9099, 9199)):
                if process.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    pytest.fail(log.read()[-3000:], pytrace=False)
                time.sleep(0.25)
            yield
        finally:
            _stop_emulator(process)


@pytest.fixture
def destination(publish_emulators, monkeypatch, tmp_path):
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", "http://127.0.0.1:9199")
    project = f"demo-dest-{uuid.uuid4().hex[:12]}"
    db = firestore.Client(project=project, credentials=AnonymousCredentials())
    storage_db = storage.Client(project=project, credentials=AnonymousCredentials())
    bucket = storage_db.bucket(project + ".appspot.com")
    app = firebase_admin.initialize_app(options={"projectId": project}, name=project)
    try:
        auth.create_user(uid=PROD_UID, app=app)
        monkeypatch.setattr(session, "get_firestore_client", lambda: db)
        monkeypatch.setattr(job_store, "get_firestore_client", lambda: db)
        monkeypatch.setattr(storage_client, "_storage_client", storage_db)
        store = session.FirestoreSessionStore(
            tmp_path, tmp_path / "dest-sessions", 3600, 10,
            backend_use_storage=True, storage_bucket=bucket.name,
        )
        yield db, bucket, store, partial(auth.get_user, app=app)
    finally:
        firebase_admin.delete_app(app)
        db.close()


def _seed_local_song(tmp_path, *, disk_only=False):
    project = f"demo-source-{uuid.uuid4().hex[:12]}"
    local_db = firestore.Client(project=project, credentials=AnonymousCredentials())
    client = storage.Client(project=project, credentials=AnonymousCredentials())
    bucket = client.bucket(project + ".appspot.com")
    prefix = f"sessions/{LOCAL_UID}/{SONG}/"
    sessions_dir = tmp_path / "local-sessions"
    local_score_path = sessions_dir / SONG / XML_PATH
    local_audio_path = sessions_dir / SONG / "render.wav"
    local_score_path.parent.mkdir(parents=True)
    local_score_path.write_text(XML)
    local_audio_path.write_bytes(AUDIO)
    files = {
        "musicxml_path": str(local_score_path.relative_to(tmp_path)),
        "instrumental_midi_original_path": prefix + MIDI_PATH,
    }
    if not disk_only:
        files["active_musicxml_storage_path"] = prefix + XML_PATH
        bucket.blob(prefix + XML_PATH).upload_from_string(XML, content_type="application/xml")
        bucket.blob(prefix + AUDIO_PATH).upload_from_string(AUDIO, content_type="audio/wav")
    bucket.blob(prefix + MIDI_PATH).upload_from_string(b"midi", content_type="audio/midi")
    score = {"source_musicxml_path": str(local_score_path)}
    bucket.blob(prefix + JSON_PATH).upload_from_string(json.dumps(score),
                                                       content_type="application/json")
    song = local_db.collection("sessions").document(SONG)
    song.create({
        "userId": LOCAL_UID, "title": "Example publication", "scoreId": "test-score",
        "songCreatedAt": NOW, "createdAt": NOW, "songListed": True, "currentScore": score,
        "currentScorePath": prefix + JSON_PATH, "currentScoreVersion": 1, "files": files,
        "scoreSummary": {"parts": [{"part_id": "P1", "take_signature": "score-signature"}]},
        "history": [{"nested": [None, {"bytes": b"\x00\xff", "when": NOW, "ownerUid": LOCAL_UID,
                                      "path": prefix + MIDI_PATH, "$firestore": "literal"}]}],
    })
    player = song.collection("player").document("current")
    player.create({
        "scoreId": "test-score", "revision": 1, "repeats": True, "updatedAt": NOW,
        "bus": {"levelDb": -6}, "vocals": [{
            "sourceJobId": TAKE, "key": "id:P1", "partId": "P1", "mode": "lyrics",
            "takeSignature": "score-signature", "label": "Vocal", "volume": 0.8,
        }],
    })
    local_db.collection("jobs").document(TAKE).create({
        "userId": LOCAL_UID, "sessionId": SONG, "status": "completed", "createdAt": NOW,
        "audioTrack": {"part_id": "P1"}, "audioUrl": "/example/local-audio",
        "outputPath": str(local_audio_path.relative_to(tmp_path)) if disk_only else prefix + AUDIO_PATH,
    })
    return local_db, bucket, sessions_dir


@pytest.mark.parametrize("disk_only", [False, True])
def test_publish_serves_score_vocals_and_preview(destination, monkeypatch, tmp_path, capsys, disk_only):
    db, bucket, store, get_user = destination
    local_db, local_bucket, sessions_dir = _seed_local_song(tmp_path, disk_only=disk_only)
    local_store = session.FirestoreSessionStore(tmp_path, sessions_dir, 3600, 10)
    local_store._client = local_db
    old_token = asyncio.run(local_store.share_on(SONG, LOCAL_UID))
    bundle = tmp_path / "bundle"
    report = export_song(local_db, local_bucket, SONG, bundle,
                         project_root=tmp_path, sessions_dir=sessions_dir)
    assert any(entry["field"] == "files.musicxml_path" for entry in report)
    assert any(entry["field"].endswith(".outputPath") for entry in report) == disk_only
    assert not (bundle / "shares").exists()
    local_db.close()

    assert import_song(db, bucket, store, bundle, PROD_UID, get_user=get_user, share=True) is None
    plan = capsys.readouterr().out
    assert "DRY_RUN=1" in plan and "shares/<new token>" in plan
    assert not list(db.collections())
    assert not list(bucket.list_blobs())

    token = import_song(db, bucket, store, bundle, PROD_UID,
                        get_user=get_user, dry_run=False, share=True)
    assert token != old_token
    assert f"https://sightsinger.app/s/{token}" in capsys.readouterr().out
    assert not db.collection("shares").document(old_token).get().exists
    assert db.collection("shares").document(token).get().to_dict()["ownerUid"] == PROD_UID
    prefix = f"sessions/{PROD_UID}/{SONG}/"
    assert sorted(blob.name for blob in bucket.list_blobs()) == sorted(
        prefix + path for path in (XML_PATH, JSON_PATH, AUDIO_PATH, MIDI_PATH)
    )
    exported = db.collection("sessions").document(SONG).get().to_dict()
    assert exported["userId"] == PROD_UID
    assert exported["songCreatedAt"] == NOW
    assert exported["files"]["active_musicxml_storage_path"] == prefix + XML_PATH
    assert exported["currentScorePath"] == prefix + JSON_PATH
    assert exported["history"][0]["nested"] == [
        None, {"bytes": b"\x00\xff", "when": NOW, "ownerUid": PROD_UID,
               "path": prefix + MIDI_PATH, "$firestore": "literal"},
    ]
    assert db.collection("jobs").document(TAKE).get().to_dict()["outputPath"] == prefix + AUDIO_PATH

    async def no_remote_shell(*_args, **_kwargs):
        return None

    monkeypatch.setattr(share_preview, "app_shell", no_remote_shell)
    monkeypatch.setattr(share_app, "FirestoreSessionStore", lambda **_: store)
    monkeypatch.setenv("SHARE_LINKS", "on")
    monkeypatch.setenv("SESSION_STORE", "firestore")
    monkeypatch.setenv("BACKEND_USE_STORAGE", "true")
    monkeypatch.setenv("STORAGE_BUCKET", bucket.name)
    monkeypatch.setenv("BACKEND_REQUIRE_APP_CHECK", "false")
    http = TestClient(share_app.create_share_app())
    base = f"/public/s/{token}"
    data = http.get(base)
    assert data.status_code == 200, data.text
    [vocal] = data.json()["player"]["vocals"]
    assert (vocal["key"], vocal["label"], vocal["volume"]) == ("id:P1", "Vocal", 0.8)
    score = http.get(f"{base}/score")
    assert (score.status_code, score.text) == (200, XML)
    assert http.get(vocal["audio_url"]).content == AUDIO
    ranged = http.get(vocal["audio_url"], headers={"Range": "bytes=2-4"})
    assert ranged.status_code == 206 and ranged.content == b"cde"
    assert 'property="og:title" content="Example publication · SightSinger"' in http.get(f"/s/{token}").text


def test_import_guards_and_undo(destination, tmp_path, capsys):
    db, bucket, store, get_user = destination
    local_db, local_bucket, sessions_dir = _seed_local_song(tmp_path)
    bundle = tmp_path / "bundle"
    export_song(local_db, local_bucket, SONG, bundle,
                project_root=tmp_path, sessions_dir=sessions_dir)
    local_db.close()
    with pytest.raises(ValueError, match="Auth user does not exist"):
        import_song(db, bucket, store, bundle, "example-missing-owner", get_user=get_user,
                    dry_run=False)
    assert not list(db.collections()) and not list(bucket.list_blobs())

    audio_path = bundle / "objects" / AUDIO_PATH
    audio_path.write_bytes(b"abcdEFGH")
    with pytest.raises(ValueError, match="corrupt object"):
        import_song(db, bucket, store, bundle, PROD_UID, get_user=get_user, dry_run=False)
    assert not list(db.collections()) and not list(bucket.list_blobs())
    audio_path.write_bytes(AUDIO)

    jobs = db.collection("jobs")
    jobs.document(TAKE).create({"userId": PROD_UID, "sessionId": "example-other-song"})
    with pytest.raises(ValueError, match="different Song"):
        import_song(db, bucket, store, bundle, PROD_UID, get_user=get_user,
                    dry_run=False, replace=True)
    assert not list(bucket.list_blobs())
    jobs.document(TAKE).delete()
    second = db.collection("sessions").document("example-other-song")
    second.create({"userId": PROD_UID, "songCreatedAt": NOW})
    second_job = jobs.document("example-other-take")
    second_job.create({"userId": PROD_UID, "sessionId": second.id})
    second_blob = bucket.blob(f"sessions/{PROD_UID}/{second.id}/file")
    second_blob.upload_from_string(b"keep")

    token = import_song(db, bucket, store, bundle, PROD_UID, get_user=get_user,
                        dry_run=False, share=True)
    with pytest.raises(ValueError, match="session exists"):
        import_song(db, bucket, store, bundle, PROD_UID, get_user=get_user)
    assert import_song(db, bucket, store, bundle, PROD_UID, get_user=get_user,
                       replace=True, dry_run=False, share=True) == token

    song = db.collection("sessions").document(SONG)
    player = song.collection("player").document("current")
    player.update({"vocals": []})
    unrelated = jobs.document("example-later-take")
    unrelated.create({"userId": PROD_UID, "sessionId": SONG})
    with pytest.raises(ValueError, match="different user"):
        remove_song(db, bucket, store, SONG, "example-another-owner", dry_run=False)
    remove_song(db, bucket, store, SONG, PROD_UID)
    assert "DRY_RUN=1. Planned removals" in capsys.readouterr().out
    assert song.get().exists and list(bucket.list_blobs(prefix=f"sessions/{PROD_UID}/{SONG}/"))
    remove_song(db, bucket, store, SONG, PROD_UID, dry_run=False)
    assert not song.get().exists
    assert not player.get().exists
    assert not jobs.document(TAKE).get().exists
    assert not db.collection("shares").document(token).get().exists
    assert not list(bucket.list_blobs(prefix=f"sessions/{PROD_UID}/{SONG}/"))
    assert unrelated.get().exists and second.get().exists and second_job.get().exists
    assert second_blob.download_as_bytes() == b"keep"


def test_wrappers_export_and_default_to_dry_run(destination, tmp_path):
    db, bucket, _, _ = destination
    local_db, local_bucket, _ = _seed_local_song(tmp_path)
    backend_env = tmp_path / "export.env"
    backend_env.write_text(
        f"GOOGLE_CLOUD_PROJECT={local_db.project}\nSTORAGE_BUCKET={local_bucket.name}\n"
    )
    env = {**os.environ, "SIGHTSINGER_PYTHON": sys.executable,
           "BACKEND_EXTRA_ENV_FILE": str(backend_env), "DRY_RUN": "1"}
    scripts = ROOT / "scripts"
    for wrapper in ("export_local_song.sh", "import_song_prod.sh"):
        subprocess.run(["bash", "-n", str(scripts / wrapper)], check=True)
    result = subprocess.run([str(scripts / "export_local_song.sh"), SONG, "bundle"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert "Exported Song" in result.stdout
    local_db.close()

    # Run the production wrapper, then point its Python process at fixture clients
    # after asserting that the wrapper cleared the inherited emulator settings.
    shim = tmp_path / "emulator-python"
    shim.write_text(f"#!{sys.executable}\n" + """
import os
import runpy
import sys

import firebase_admin
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore import Client
from src.backend import firebase_app

assert os.environ["DRY_RUN"] == "1"
for name in ("FIRESTORE_EMULATOR_HOST", "FIREBASE_AUTH_EMULATOR_HOST", "STORAGE_EMULATOR_HOST"):
    assert name not in os.environ
    os.environ[name] = os.environ["PUBLISH_TEST_" + name]
project = os.environ["PUBLISH_TEST_PROJECT"]
os.environ["GOOGLE_CLOUD_PROJECT"] = project
os.environ["STORAGE_BUCKET"] = project + ".appspot.com"
firebase_admin.initialize_app(options={"projectId": project})
firebase_app._firestore_client = Client(project=project, credentials=AnonymousCredentials())
assert sys.argv[1:3] == ["-m", "scripts.import_song_prod"]
sys.argv = sys.argv[2:]
runpy.run_module("scripts.import_song_prod", run_name="__main__")
""")
    shim.chmod(0o700)
    env.pop("DRY_RUN")
    env.update({f"PUBLISH_TEST_{name}": env[name] for name in (
        "FIRESTORE_EMULATOR_HOST", "FIREBASE_AUTH_EMULATOR_HOST", "STORAGE_EMULATOR_HOST"
    )})
    env.update(SIGHTSINGER_PYTHON=str(shim), PUBLISH_TEST_PROJECT=db.project)
    result = subprocess.run([str(scripts / "import_song_prod.sh"), "bundle",
                             "--prod-uid", PROD_UID, "--share"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert "DRY_RUN=1. Planned writes:" in result.stdout
    assert "update Song shareToken and create shares/<new token>" in result.stdout
    assert not list(db.collections()) and not list(bucket.list_blobs())
