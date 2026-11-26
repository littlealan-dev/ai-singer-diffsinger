"""Count only successful share views, first-track plays and ZIP links."""

import asyncio
from datetime import datetime, timezone

from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore import Client
import pytest

from src.backend import main, session, sing_all_store, storage_client
from tests.test_backend_api import _create_session, _upload_score, client_with_env  # noqa: F401
from tests.test_songs_backend import _start_take


@pytest.mark.parametrize("client_with_env", [{"SHARE_LINKS": "on"}], indirect=True)
def test_share_page_and_audio_count_and_ignore_write_failure(client_with_env, monkeypatch):
    http, app = client_with_env
    store = app.state.sessions
    sid = _create_session(http)
    _upload_score(http, sid)
    audio = store.session_dir(sid) / "take.wav"
    audio.write_bytes(b"abcdefgh")
    take = _start_take(app, sid, "analytics-test-take",
                       outputPath=str(audio.relative_to(app.state.settings.project_root)))
    state = store._sessions[sid]
    state.score_summary["parts"][0]["take_signature"] = "example-music"
    take["audioTrack"]["take_signature"] = "example-music"
    asyncio.run(store.record_take(sid, "analytics-test-take", take))
    token = asyncio.run(store.share_on(sid, state.user_id))
    base = f"/public/s/{token}"

    async def viewer(request):
        return (state.user_id, "") if request.headers.get("authorization") else None

    monkeypatch.setattr(main, "_get_optional_verified_user_context", viewer)
    assert http.get(base).json()["is_owner"] is True
    assert not asyncio.run(store.get_share(token)).get("views")
    page = http.get(base, headers={"authorization": ""})
    assert page.status_code == 200
    assert not {"views", "plays", "lastViewedAt"} & page.json().keys()
    record = asyncio.run(store.get_share(token))
    assert record["views"] == 1 and isinstance(record["lastViewedAt"], datetime)

    [vocal] = page.json()["player"]["vocals"]
    url = vocal["audio_url"]
    assert http.get(url).status_code == 200
    assert http.get(url + "&first=2").status_code == 200
    assert http.get(url + "&first=1", headers={"range": "bytes=2-4"}).status_code == 206
    assert not asyncio.run(store.get_share(token)).get("plays")
    assert http.get(url + "&first=1").status_code == 200
    assert asyncio.run(store.get_share(token))["plays"] == 1

    async def write_fails(*args, **kwargs):
        raise RuntimeError("synthetic counter write failure")

    monkeypatch.setattr(store, "count_share_use", write_fails)
    assert http.get(base, headers={"authorization": ""}).status_code == 200
    assert http.get(url + "&first=1").status_code == 200


def test_zip_counts_only_links_and_ignores_write_failure(client_with_env, monkeypatch):
    from dataclasses import replace

    http, app = client_with_env
    sid = _create_session(http)
    _upload_score(http, sid)
    owner = app.state.sessions._sessions[sid].user_id
    job = {"uid": owner, "sessionId": sid, "package": {"state": "zipping"}}
    app.state.settings = replace(app.state.settings, backend_use_storage=True)
    monkeypatch.setattr(main, "load_sing_all_job", lambda _: job)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(storage_client, "signed_download_url", lambda *args: ("https://example.invalid/zip", now))
    writes = []

    def count_download(job_id):
        writes.append(job_id)
        if len(writes) == 2:
            raise RuntimeError("synthetic counter write failure")

    monkeypatch.setattr(main, "count_zip_download", count_download)
    url = "/sing-all/jobs/analytics-test-job/download"
    assert http.get(url).status_code == 409
    assert writes == []
    job["package"] = {"state": "done", "zipPath": f"singAll/{owner}/analytics-test-job/Example.zip"}
    for _ in range(2):
        assert http.get(url).json() == {
            "url": "https://example.invalid/zip", "fileName": "Example.zip", "expiresAt": now.isoformat(),
        }
    assert writes == ["analytics-test-job"] * 2


def test_firestore_share_and_zip_counters_are_atomic(firestore_emulator, monkeypatch, tmp_path):
    db = Client(project="demo-share-analytics", credentials=AnonymousCredentials())
    monkeypatch.setattr(session, "get_firestore_client", lambda: db)
    monkeypatch.setattr(sing_all_store, "get_firestore_client", lambda: db)
    store = session.FirestoreSessionStore(tmp_path, tmp_path / "sessions", 3600, 10)
    share = db.collection("shares").document("example-share")
    job = db.collection("singAllJobs").document("example-job")
    share.set({"createdAt": datetime.now(timezone.utc)})
    job.set({"revision": 4})

    async def count():
        await asyncio.gather(
            store.count_share_use("example-share"),
            store.count_share_use("example-share"),
            store.count_share_use("example-share", play=True),
            store.count_share_use("example-share", play=True),
            asyncio.to_thread(sing_all_store.count_zip_download, "example-job"),
            asyncio.to_thread(sing_all_store.count_zip_download, "example-job"),
        )

    try:
        asyncio.run(count())
        record = share.get().to_dict()
        assert record["views"] == record["plays"] == 2
        assert record["lastViewedAt"] >= record["createdAt"]
        assert job.get().to_dict() == {"revision": 4, "zipDownloads": 2}
    finally:
        share.delete()
        job.delete()
