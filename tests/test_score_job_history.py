"""Score and job retention regressions, using a local emulator or memory."""

import asyncio
import os
import uuid

import pytest

from src.backend import session as module
from src.backend.job_store import JobStore


class MemoryJobs:
    def __init__(self):
        self.jobs = {}

    def create_job(self, **fields):
        assert fields["job_id"] not in self.jobs
        self.jobs[fields["job_id"]] = JobStore.build_job_payload(**fields)

    def update_job(self, job_id, **fields):
        self.jobs[job_id].update(fields)


@pytest.fixture(params=["memory", "firestore"])
def workspace(request, monkeypatch, tmp_path):
    if request.param == "firestore":
        if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
            pytest.skip("Requires local FIRESTORE_EMULATOR_HOST")
        from google.cloud.firestore import Client
        from google.auth.credentials import AnonymousCredentials
        client = Client(project="demo-score-history", credentials=AnonymousCredentials())
        monkeypatch.setattr(module, "get_firestore_client", lambda: client)
        store_type = module.FirestoreSessionStore
        jobs = JobStore()
        jobs._client = client
    else:
        store_type = module.SessionStore
        jobs = MemoryJobs()
    args = dict(project_root=tmp_path, sessions_dir=tmp_path / "sessions",
                ttl_seconds=3600, max_sessions=100)
    store = store_type(**args)
    return store, jobs, args


async def upload(store, sid, label="A"):
    score_id = uuid.uuid4().hex
    source = store.session_dir(sid) / f"{score_id}.xml"
    source.write_text(f"<score-partwise>{label}</score-partwise>")
    score = {"title": label, "source_musicxml_path": str(source)}
    await store.commit_uploaded_score(sid, score_id=score_id, score=score,
                                      summary={"title": label},
                                      files={"musicxml_path": str(source.relative_to(store._project_root))})
    return score_id, score


def test_reupload_retains_jobs_audio_and_immutable_versions(workspace):
    store, jobs, _ = workspace

    async def scenario():
        sid = (await store.create_session("history-user")).id
        a, score = await upload(store, sid)
        job_id = uuid.uuid4().hex
        await store.create_current_job(sid, jobs, job_id=job_id, user_id="history-user",
            status="queued", render_type="preprocess",
            provenance={"scoreId": a, "scoreVersionNo": 1, "provenanceStatus": "captured"})
        assert await store.set_score(sid, {**score, "derived": True}) == 2
        assert await store.set_score(sid, {**score, "solfege": True}) == 3
        audio = store.session_dir(sid) / "old.wav"
        audio.write_bytes(b"original audio")
        await store.set_audio(sid, audio, 10)
        await store.append_history(sid, "user", "Sing this")
        b, _ = await upload(store, sid)  # Identical content still creates another score.
        current = await store.get_snapshot(sid, "history-user")
        assert b != a
        assert current["current_score"]["version"] == 1
        assert current["current_job_id"] is None
        assert current["current_audio"] is None
        assert current["history"][0]["content"] == "Sing this"
        assert audio.read_bytes() == b"original audio"
        assert (store.session_dir(sid) / "scores" / a / "versions" / "3" / "input.xml").is_file()
        receipt = (jobs.jobs[job_id] if isinstance(jobs, MemoryJobs) else
                   jobs.get_job_by_id(job_id=job_id, user_id="history-user", session_id=sid)[1])
        assert receipt["scoreId"] == a
        assert receipt["scoreVersionNo"] == 1

    asyncio.run(scenario())


def test_upload_promotes_a_legacy_session_without_migrating_history(workspace):
    store, _, _ = workspace

    async def scenario():
        sid = (await store.create_session("history-user")).id
        if isinstance(store, module.FirestoreSessionStore):
            store._doc_ref(sid).update({"jobHistorySchemaVersion": 1})
        else:
            store._sessions[sid].job_history_schema_version = 1

        score_id, _ = await upload(store, sid)
        snapshot = await store.get_snapshot(sid, "history-user")
        assert snapshot["score_id"] == score_id
        assert snapshot["job_history_schema_version"] == 2

    asyncio.run(scenario())


def test_firestore_job_record_survives_reupload_and_is_readable_by_another_instance(workspace):
    store, jobs, args = workspace
    if not isinstance(store, module.FirestoreSessionStore):
        pytest.skip("Cross-instance persistence requires the Firestore store")

    async def scenario():
        sid = (await store.create_session("history-user")).id
        score_id, _ = await upload(store, sid)
        job_id = uuid.uuid4().hex
        await store.create_current_job(
            sid,
            jobs,
            job_id=job_id,
            user_id="history-user",
            status="queued",
            provenance={"scoreId": score_id, "scoreVersionNo": 1},
        )
        output_path = f"sessions/history-user/{sid}/jobs/{job_id}/output.mp3"
        jobs.update_job(
            job_id,
            status="completed",
            audioUrl=f"/sessions/{sid}/audio?file=output.mp3",
            outputPath=output_path,
        )

        other_store = module.FirestoreSessionStore(**args)
        other_jobs = JobStore()
        other_jobs._client = other_store._client
        await upload(other_store, sid, "B")

        restored = other_jobs.get_job_by_id(
            job_id=job_id,
            user_id="history-user",
            session_id=sid,
        )
        assert restored is not None
        assert restored[1]["status"] == "completed"
        assert restored[1]["outputPath"] == output_path

    asyncio.run(scenario())


def test_failed_storage_publication_preserves_active_score(workspace, monkeypatch):
    store, _, _ = workspace

    async def scenario():
        sid = (await store.create_session("history-user")).id
        a, score = await upload(store, sid)
        before = await store.get_snapshot(sid, "history-user")
        def fail(*args, **kwargs):
            raise OSError("storage unavailable")
        monkeypatch.setattr(store, "_stage_score", fail)
        with pytest.raises(OSError):
            await store.set_score(sid, {**score, "title": "changed"})
        after = await store.get_snapshot(sid, "history-user")
        assert after["score_id"] == a
        assert after["current_score"] == before["current_score"]
        with pytest.raises(OSError):
            await upload(store, sid, "B")
        after = await store.get_snapshot(sid, "history-user")
        assert after["score_id"] == a
        assert after["current_score"] == before["current_score"]

    asyncio.run(scenario())
