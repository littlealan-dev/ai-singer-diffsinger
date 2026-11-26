"""Sing All parent creation uses the existing all-or-nothing credit transaction."""

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from src.backend import main, sing_all_store
from src.backend.orchestrator import Orchestrator
from src.backend.message_catalog import backend_message
from src.backend.session import _new_history_entry


class _Snapshot:
    def __init__(self, value):
        self.value = value
        self.exists = value is not None

    def to_dict(self):
        return self.value


class _Document:
    def __init__(self, records, path):
        self.records = records
        self.path = path

    def get(self, transaction=None):
        return _Snapshot(self.records.get(self.path))

    def document(self, name):
        return _Document(self.records, f"{self.path}/{name}")


class _Db:
    def __init__(self, records):
        self.records = records
        self.last_transaction = None

    def collection(self, name):
        return _Document(self.records, name)

    def transaction(self):
        self.last_transaction = _Transaction()
        return self.last_transaction


class _Transaction:
    def __init__(self):
        self.writes = []

    def set(self, ref, value):
        self.writes.append(("set", ref.path, value))

    def update(self, ref, value):
        self.writes.append(("update", ref.path, value))


def _job():
    return {
        "jobId": "fake-parent", "uid": "fake-user", "sessionId": "fake-song",
        "scoreId": "fake-score", "quote": {"scoreVersion": 2},
        "items": [
            {"type": "split", "childJobId": "fake-split", "creditsReserved": 0},
            {"type": "take", "childJobId": "fake-take-a", "creditsReserved": 3,
             "estimatedSeconds": 75, "unitSeconds": 30},
            {"type": "take", "childJobId": "fake-take-b", "creditsReserved": 3,
             "estimatedSeconds": 75, "unitSeconds": 30},
            {"type": "instrumental", "reservationId": "fake-parent-instrumentals",
             "creditsReserved": 1, "estimatedSeconds": 75, "unitSeconds": 120},
        ],
    }


def test_parent_reserves_only_chargeable_items_in_one_call(monkeypatch):
    seen = {}

    def reserve(uid, items, **kwargs):
        seen.update(uid=uid, items=items, **kwargs)
        return SimpleNamespace(status="reserved")

    monkeypatch.setattr(sing_all_store, "get_firestore_client", lambda: _Db({}))
    monkeypatch.setattr(sing_all_store, "reserve_credits_for_items", reserve)
    result = sing_all_store.create_job_with_reservations(_job(), instrumental_scope="fake-scope")
    assert result.status == "reserved"
    assert seen["uid"] == "fake-user"
    assert [item.job_id for item in seen["items"]] == [
        "fake-take-a", "fake-take-b", "fake-parent-instrumentals"]
    assert [item.billing_components for item in seen["items"]] == [
        ("vocal",), ("vocal",), ("instrumental",)]
    assert seen["items"][-1].instrumental_charge_scope_value == "fake-scope"
    assert seen["score_version_no"] == 2
    assert seen["parent_job_id"] == "fake-parent"


def test_parent_guard_refuses_running_job_and_replaces_finished_pointer():
    job = _job()
    records = {"users/fake-user": {"activeSingAllJobId": "old-parent"},
               "singAllJobs/old-parent": {"status": "running"}}
    guard = sing_all_store._JobReservationGuard(_Db(records), job)
    transaction = _Transaction()
    assert guard.read(transaction) == "sing_all_already_running"
    assert transaction.writes == []

    records["singAllJobs/old-parent"]["status"] = "completed"
    assert guard.read(transaction) is None
    guard.write(transaction)
    assert transaction.writes[:2] == [
        ("set", "singAllJobs/fake-parent", job),
        ("update", "users/fake-user", {"activeSingAllJobId": "fake-parent"}),
    ]
    assert transaction.writes[2][1] == "sessions/fake-song"
    assert transaction.writes[2][2]["history"]._values[0]["singAllJobId"] == "fake-parent"


def test_sing_all_history_has_link_and_factual_counts():
    started = backend_message("sing_all.started_history", takes=4, credits=8)
    ended = backend_message("sing_all.ended_history", status="completed",
                            done=4, takes=4, credits=8)
    assert "4 takes" in started and "8 credits" in started
    assert "4 of 4" in ended and "8 credits" in ended
    assert _new_history_entry("assistant", started, sing_all_job_id="fake-parent")[
        "singAllJobId"] == "fake-parent"
    assert "singAllJobId" not in _new_history_entry("assistant", "Ordinary message")


def test_running_parent_is_scoped_to_its_song(monkeypatch):
    records = {"users/fake-user": {"activeSingAllJobId": "fake-parent"},
               "singAllJobs/fake-parent": {"status": "running", "sessionId": "fake-song"}}
    monkeypatch.setattr(sing_all_store, "get_firestore_client", lambda: _Db(records))
    assert sing_all_store.active_job_for_song("fake-user", "fake-song") == records[
        "singAllJobs/fake-parent"]
    assert sing_all_store.active_job_for_song("fake-user", "other-song") is None
    records["singAllJobs/fake-parent"]["status"] = "completed"
    assert sing_all_store.active_job_for_song("fake-user", "fake-song") is None


def test_child_worker_requires_running_parent_and_listed_child(monkeypatch):
    parent = {"status": "running", "items": [
        {"itemId": "take:a:lyrics", "childJobId": "fake-child"}]}
    monkeypatch.setattr(sing_all_store, "load", lambda job_id: parent)
    assert sing_all_store.child_allowed("fake-parent", "take:a:lyrics", "fake-child")
    assert not sing_all_store.child_allowed("fake-parent", "take:b:lyrics", "fake-child")
    assert not sing_all_store.child_allowed("fake-parent", "take:a:lyrics", "another-child")
    parent["status"] = "failed"
    assert not sing_all_store.child_allowed("fake-parent", "take:a:lyrics", "fake-child")


def test_existing_queued_children_are_redispatched_without_creating_them_again():
    updates = []
    dispatches = []

    def no_create(**_kwargs):
        raise AssertionError("A queued child already exists")

    job_store = SimpleNamespace(
        get_job=lambda _job_id: {"status": "queued"},
        create_job=no_create,
        update_job=lambda job_id, **fields: updates.append((job_id, fields)),
    )

    async def snapshot(*_args):
        return {"score_summary": {}}

    async def metadata(_args):
        return {}

    async def capture(*_args):
        return "jobs/example/input.xml", None, {"scoreId": "example-score"}

    # Each call must fit the real method, as it does in production.
    async def dispatch_synthesis(session_id, job_id, **kwargs):
        inspect.signature(Orchestrator._dispatch_synthesis_job).bind(
            None, session_id, job_id, **kwargs)
        dispatches.append(("take", job_id))

    async def dispatch_split(session_id, job_id, calls, **kwargs):
        inspect.signature(Orchestrator._dispatch_preprocess_job).bind(
            None, session_id, job_id, calls, **kwargs)
        dispatches.append(("split", job_id))
        return {"job_id": job_id}

    orchestrator = SimpleNamespace(
        _job_store=job_store,
        _sessions=SimpleNamespace(get_snapshot=snapshot),
        _settings=SimpleNamespace(backend_use_storage=False),
        _synthesis_dispatcher=SimpleNamespace(mode="http"),
        _preprocess_dispatcher=SimpleNamespace(mode="http"),
        _synthesis_tasks={}, _preprocess_tasks={},
        _build_synthesis_voicebank_metadata=metadata,
        _build_synthesis_audio_track_metadata=lambda *_args, **_kwargs: {},
        _capture_job_input=capture,
        _dispatch_synthesis_job=dispatch_synthesis,
        _dispatch_preprocess_job=dispatch_split,
    )

    async def run():
        await Orchestrator._start_synthesis_job(
            orchestrator, "example-song", {}, {"part_id": "P1"},
            user_id="example-user", parent={
                "jobId": "example-parent", "itemId": "take:P1:lyrics",
                "childJobId": "example-take"},
        )
        await Orchestrator._start_preprocess_job(
            orchestrator, "example-song", {}, [], initial_message="Preparing lines",
            initial_thought_summary="", user_id="example-user", user_email="",
            parent={"jobId": "example-parent", "itemId": "split:P1",
                    "childJobId": "example-split"},
        )

    asyncio.run(run())
    assert dispatches == [("take", "example-take"), ("split", "example-split")]
    assert [(job_id, fields["parentJobId"], fields["parentItemId"])
            for job_id, fields in updates] == [
                ("example-take", "example-parent", "take:P1:lyrics"),
                ("example-split", "example-parent", "split:P1"),
            ]


def test_finish_updates_parent_guard_and_receipt_together(monkeypatch):
    db = _Db({"singAllJobs/fake-parent": {
        "uid": "fake-user", "sessionId": "fake-song", "status": "running",
        "stage": "singing", "revision": 4,
    }, "users/fake-user": {"activeSingAllJobId": "fake-parent"}})
    monkeypatch.setattr(sing_all_store, "get_firestore_client", lambda: db)
    monkeypatch.setattr(sing_all_store.firestore, "transactional", lambda function: function)
    assert sing_all_store.finish(
        "fake-parent", {"status": "completed", "stage": "done", "items": [
            {"type": "take", "status": "done", "creditsCharged": 2}]},
        expected_stage="singing", expected_revision=4)
    writes = db.last_transaction.writes
    assert [value[1] for value in writes] == [
        "singAllJobs/fake-parent", "users/fake-user", "sessions/fake-song"]
    assert writes[0][2]["revision"] == 5
    assert writes[2][2]["history"]._values[0]["singAllJobId"] == "fake-parent"


def _start_route(monkeypatch, *, changed=False, reserve_status="reserved",
                 reserve_reason="not_enough_credits", billing=None):
    row = {"rowId": "a", "sourcePartId": "P1", "partId": "P1",
           "partName": "Made-up lead", "name": "Made-up lead", "split": False,
           "hasWords": True, "include": True, "voice": "voice_a",
           "voicePicked": "recommended", "note": None}
    choices = {"language": "en", "solfegeOnly": False, "verse": "1", "mode": "lyrics",
               "repeats": True, "rows": []}
    quote = {"quoteId": "quote-a", "uid": "owner", "sessionId": "song-a",
             "scoreId": "score-a", "scoreVersion": 2, "choices": choices,
             "choicesHash": main.choices_hash(choices, {}),
             "judgment": {"inputHash": "facts-a", "voices": {"a": "voice_a"}},
             "style": {}, "rows": [row],
             "items": main.build_items([row], mode="lyrics", duration_seconds=59,
                                       instrumental_charge_required=False), "total": 2}
    snapshot = {"song": {"title": "Made-up song"}, "score_id": "score-a",
                "current_score": {"score": {"selected_verse_number": "1"},
                                  "version": 3 if changed else 2},
                "score_summary": {"parts": [{"part_id": "P1", "part_name": "Made-up lead"}],
                                  "duration_seconds": 59}}
    events = []
    async def auth(request):
        return "owner", "owner@example.test"
    async def owned(sessions, session_id, uid):
        return snapshot
    async def voices():
        return [{"id": "voice_a", "languages": ["en"]}]
    async def history(*args, **kwargs):
        events.append(("history", kwargs["sing_all_job_id"]))
    async def enqueue(job_id):
        events.append(("enqueue", job_id))
    monkeypatch.setattr(main, "_get_user_context_or_401", auth)
    monkeypatch.setattr(main, "_get_snapshot_or_404", owned)
    monkeypatch.setattr(main, "read_quote", lambda db, quote_id, uid: quote)
    user = {"billing": ({"activePlanKey": "solo_monthly", "billingInterval": "month",
                         "stripeSubscriptionStatus": "active"} if billing is None else billing)}
    if billing == {}:
        user = {}
    if reserve_reason == "sing_all_already_running":
        user["activeSingAllJobId"] = "other-parent"
    monkeypatch.setattr(main, "get_firestore_client", lambda: _Db({"users/owner": user}))
    monkeypatch.setattr(main, "load_sing_all_job", lambda job_id: {
        "jobId": job_id, "uid": "owner", "sessionId": "other-song", "status": "running"})
    monkeypatch.setattr(main, "quote_facts", lambda *args, **kwargs: {})
    monkeypatch.setattr(main, "input_hash", lambda *args, **kwargs: "facts-a")
    monkeypatch.setattr(main, "materialize_rows", lambda *args, **kwargs: [dict(row)])
    monkeypatch.setattr(main, "mark_voice_ranges", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "summary_with_judged_instruments", lambda summary, *args, **kwargs: summary)
    monkeypatch.setattr(main, "score_has_instrumental_parts", lambda summary: False)
    monkeypatch.setattr(main, "selected_score_duration", lambda *args, **kwargs: 59)
    monkeypatch.setattr(main, "create_job_with_reservations",
                        lambda job, **kwargs: (events.append(("reserve", job["jobId"]))
                                               or SimpleNamespace(status=reserve_status,
                                                                  reason=reserve_reason)))
    orchestrator = SimpleNamespace(_get_voicebank_details=voices,
                                   _enqueue_sing_all_reconcile=enqueue)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        sessions=SimpleNamespace(append_history=history), orchestrator=orchestrator,
        settings=main.app.state.settings)))
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == "/sing-all/jobs")
    return endpoint, request, events


def test_sing_it_reserves_then_starts_one_parent_without_model(monkeypatch):
    endpoint, request, events = _start_route(monkeypatch)
    result = asyncio.run(endpoint(main.SingAllJobRequest(quoteId="quote-a"), request))
    assert result["status"] == "running" and result["stage"] == "reading"
    assert result["songId"] == "song-a" and result["title"] == "Made-up song"
    assert result["rows"][0]["voiceName"] == "voice_a"
    assert result["counts"]["takes"] == {"done": 0, "failed": 0, "skipped": 0, "total": 1}
    assert [event[0] for event in events] == ["reserve", "enqueue"]


def test_sing_it_rejects_changed_score_before_reservation(monkeypatch):
    endpoint, request, events = _start_route(monkeypatch, changed=True)
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(endpoint(main.SingAllJobRequest(quoteId="quote-a"), request))
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "quote_changed"}
    assert events == []


@pytest.mark.parametrize("billing", [
    {"activePlanKey": "free"},
    {},
    # A paid plan whose subscription has ended is no paid plan.
    {"activePlanKey": "solo_monthly", "billingInterval": "month", "stripeSubscriptionStatus": "canceled"},
])
def test_sing_it_refuses_a_user_without_a_paid_plan_before_reserving(monkeypatch, billing):
    # The API refuses on its own: a client that skips the disabled button gets no job.
    endpoint, request, events = _start_route(monkeypatch, billing=billing)
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(endpoint(main.SingAllJobRequest(quoteId="quote-a"), request))
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "free_plan"}
    assert events == []


@pytest.mark.parametrize("reserve_status,reserve_reason,code", [
    ("insufficient_balance", None, "not_enough_credits"),
    ("refused", "sing_all_already_running", "sing_all_already_running"),
])
def test_sing_it_refuses_without_enqueuing_when_reservation_fails(
    monkeypatch, reserve_status, reserve_reason, code,
):
    endpoint, request, events = _start_route(
        monkeypatch, reserve_status=reserve_status, reserve_reason=reserve_reason)
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(endpoint(main.SingAllJobRequest(quoteId="quote-a"), request))
    assert caught.value.status_code == 409
    expected = {"code": code}
    if code == "sing_all_already_running":
        expected.update(activeJobId="other-parent", activeSongId="other-song")
    assert caught.value.detail == expected
    assert [event[0] for event in events] == ["reserve"]
