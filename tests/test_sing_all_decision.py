"""Step 6: a split or solfège failure pauses the job for the owner's decision."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.backend import main, sing_all_mail, sing_all_store
from src.backend.credits import get_or_create_credits
from src.backend.firebase_app import get_firestore_client
from src.backend.sing_all import DECISION_WINDOW, decide, decision_choice, progress
from tests.test_sing_all_mail import (  # noqa: F401
    OWNER_EMAIL, _outbox_ids, _settings, brevo, email_environment,
)
from tests.test_sing_all_mixes_credits import MIX_FILES, _parent, _runner, mixes_environment  # noqa: F401

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def _take(row_id, credits=2):
    return {"itemId": f"take:{row_id}:lyrics", "type": "take", "rowId": row_id,
            "mode": "lyrics", "childJobId": f"take-{row_id}", "status": "queued",
            "reason": None, "creditsReserved": credits, "creditsCharged": 0}


def _split_job():
    """Two split staffs (A with two lines, B with one) and one part sung as it is."""
    rows = [
        {"rowId": "a1", "sourcePartId": "A", "split": True, "include": True,
         "partName": "Made-up upper staff", "name": "a1"},
        {"rowId": "a2", "sourcePartId": "A", "split": True, "include": True,
         "partName": "Made-up upper staff", "name": "a2"},
        {"rowId": "b1", "sourcePartId": "B", "split": True, "include": True,
         "partName": "Made-up lower staff", "name": "b1"},
        {"rowId": "solo", "sourcePartId": "S", "partId": "S", "split": False, "include": True,
         "partName": "Made-up solo", "name": "Made-up solo"},
    ]
    items = [
        {"itemId": "split:A", "type": "split", "sourcePartId": "A", "childJobId": "split-a",
         "status": "queued", "creditsReserved": 0},
        {"itemId": "split:B", "type": "split", "sourcePartId": "B", "childJobId": "split-b",
         "status": "queued", "creditsReserved": 0},
        *[_take(row["rowId"]) for row in rows],
        {"itemId": "instrumental", "type": "instrumental", "reservationId": "job-instrumentals",
         "status": "queued", "creditsReserved": 1, "creditsCharged": 0},
        {"itemId": "mixes", "type": "mixes", "reservationId": "job-mixes", "files": 3,
         "status": "queued", "creditsReserved": 3, "creditsCharged": 0},
    ]
    return {"jobId": "job", "uid": "made-up-user", "sessionId": "made-up-song",
            "title": "Made-up tune", "status": "running", "stage": "splitting",
            "choices": {"verse": "1", "mode": "lyrics", "language": "en", "solfegeOnly": False,
                        "repeats": False},
            "quote": {"total": 12}, "rows": rows, "items": items, "createdAt": NOW}


def _step(job, children, **state):
    fields, actions = decide(job, children, {"now": NOW, **state})
    job.update(fields)
    return fields, actions


FAILED_A = {"status": "failed", "errorMessage": "Made-up planner note."}
REVIEW_B = {"status": "completed", "reviewRequired": True, "message": "Made-up review note.",
            "parts": [{"laneKey": "b1", "partId": "B1", "name": "Made-up low line"}]}


def _paused_job():
    job = _split_job()
    _step(job, {"split-a": FAILED_A})
    _step(job, {"split-a": FAILED_A})                      # starts split B
    _step(job, {"split-a": FAILED_A, "split-b": REVIEW_B})
    assert job["items"][1]["status"] == "review"
    fields, actions = _step(job, {"split-a": FAILED_A, "split-b": REVIEW_B})
    return job, fields, actions


def test_a_failed_split_lets_the_next_split_run_then_pauses_once():
    job = _split_job()
    fields, actions = _step(job, {"split-a": FAILED_A})
    assert fields["items"][0]["status"] == "failed" and actions == []
    assert fields["items"][0]["message"] == "Made-up planner note."
    # The second split still starts.
    fields, actions = _step(job, {"split-a": FAILED_A})
    assert actions == [{"kind": "start_split", "itemId": "split:B"}]

    job, fields, actions = _paused_job()
    assert fields["status"] == "needs_decision"
    assert actions == [{"kind": "append_pause_history"}, {"kind": "queue_email"}]
    decision = fields["decision"]
    assert (decision["pause"], decision["stage"], decision["answer"]) == (1, "splitting", None)
    assert decision["deadline"] == NOW + DECISION_WINDOW
    assert decision["parts"] == [
        {"sourcePartId": "A", "name": "Made-up upper staff", "state": "failed",
         "reason": "split_failed", "message": "Made-up planner note.", "madeParts": [],
         "rowIds": ["a1", "a2"]},
        {"sourcePartId": "B", "name": "Made-up lower staff", "state": "review",
         "reason": "split_review", "message": "Made-up review note.",
         "madeParts": ["Made-up low line"], "rowIds": ["b1"]},
    ]
    # Every reservation is still held during the pause.
    assert all(item["status"] == "queued" for item in job["items"] if item["type"] != "split")
    # The review split's rows already point at the parts it made.
    assert job["rows"][2]["partId"] == "B1"


def test_parts_other_than_quoted_count_as_a_failed_split():
    job = _split_job()
    wrong = {"status": "completed", "parts": [{"laneKey": "a1", "partId": "A1"}]}
    fields, _ = _step(job, {"split-a": wrong})
    assert (fields["items"][0]["status"], fields["items"][0]["reason"]) == ("failed", "parts_changed")


def test_a_split_the_planner_refused_fails_with_the_planners_reason():
    """The planner made parts but its own check refused them, so they carry no lane ids:
    the pause says why the planner refused, not that the parts differ from the quote."""
    job = _split_job()
    refused = {"status": "completed", "reviewRequired": True, "parts": [],
               "message": "Made-up chat summary.",
               "actionRequired": {"message": "Made-up planner check.", "targets": []}}
    fields, _ = _step(job, {"split-a": refused})
    item = fields["items"][0]
    assert (item["status"], item["reason"], item["message"]) == (
        "failed", "split_failed", "Made-up planner check.")


def test_the_card_numbers_follow_the_review_tick_box():
    job, _, _ = _paused_job()
    # Not paused: the solo take (2), the instrumentals (1) and the mixes (3).
    assert decision_choice(job, []) == {"rows": 1, "heldCredits": 6}
    assert decision_choice(job, ["B"]) == {"rows": 2, "heldCredits": 8}
    # A failed part can't be accepted.
    assert decision_choice(job, ["A"]) == {"rows": 1, "heldCredits": 6}
    public = progress(job, now=NOW + timedelta(seconds=30))
    assert public["status"] == "needs_decision"
    assert public["remainingS"] is None and public["receipt"] == []
    assert public["credits"] == {"reserved": 12, "charged": 0, "refunded": 0}
    assert public["decision"] == {
        "pause": 1, "stage": "splitting", "deadline": (NOW + DECISION_WINDOW).isoformat(),
        "parts": [
            {"sourcePartId": "A", "name": "Made-up upper staff", "state": "failed",
             "reason": "split_failed", "message": "Made-up planner note.", "madeParts": [],
             "rows": 2, "heldCredits": 4},
            {"sourcePartId": "B", "name": "Made-up lower staff", "state": "review",
             "reason": "split_review", "message": "Made-up review note.",
             "madeParts": ["Made-up low line"], "rows": 1, "heldCredits": 2},
        ],
        "keep": {"rows": 1, "heldCredits": 6},
    }


@pytest.mark.parametrize("accept,skipped", [(["B"], {"a1", "a2"}), ([], {"a1", "a2", "b1"})])
def test_continue_sings_accepted_review_parts_and_refunds_the_rest(accept, skipped):
    job, _, _ = _paused_job()
    job["decision"]["answer"] = {"action": "continue", "accept": accept, "heldCredits": 0}
    fields, actions = _step(job, {})
    assert (fields["status"], fields["stage"]) == ("running", "solfege")
    splits = {item["sourcePartId"]: item["status"] for item in job["items"] if item["type"] == "split"}
    assert splits == {"A": "failed", "B": "done" if accept else "failed"}
    takes = {item["rowId"]: (item["status"], item["reason"])
             for item in job["items"] if item["type"] == "take"}
    for row_id, (status, reason) in takes.items():
        if row_id in skipped:
            assert status == "skipped"
            assert reason == ("split_failed" if row_id.startswith("a") else "split_review")
        else:
            assert status == "queued"
    assert sorted(action["reservationId"] for action in actions) == sorted(
        f"take-{row_id}" for row_id in skipped)
    assert job["decision"]["answer"]["appliedAt"] == NOW
    # A later pass leaves the paused splits alone.
    fields, actions = _step(job, {})
    assert job["status"] == "running"


@pytest.mark.parametrize("case,reason,emailed", [
    ("cancel", "decision_cancelled", False),
    ("stop", "stopped", False),
    ("timeout", "decision_timeout", True),
])
def test_cancel_stop_and_the_timeout_refund_everything_unsung(case, reason, emailed):
    job, _, _ = _paused_job()
    state = {}
    if case == "cancel":
        job["decision"]["answer"] = {"action": "cancel", "accept": []}
    elif case == "stop":
        job["cancelRequested"] = True
    else:
        # Not swept, or swept before the deadline: nothing happens.
        assert decide(job, {}, {"now": NOW + timedelta(hours=30)}) == ({}, [])
        assert decide(job, {}, {"now": NOW + timedelta(hours=23), "sweep": True}) == ({}, [])
        state = {"sweep": True}
    fields, actions = decide(job, {}, {"now": NOW + timedelta(hours=25), **state})
    assert (fields["status"], fields["stage"], fields["endReason"]) == ("cancelled", "done", reason)
    released = sorted(action["reservationId"] for action in actions if action["kind"] == "release")
    assert released == sorted(["take-a1", "take-a2", "take-b1", "take-solo",
                               "job-instrumentals", "job-mixes"])
    assert ({"kind": "queue_email"} in actions) is emailed
    assert {"kind": "clear_active"} in actions


def test_every_row_paused_with_nothing_to_review_fails_without_asking():
    job = _split_job()
    job["rows"] = job["rows"][:2]
    job["items"] = [job["items"][0], _take("a1"), _take("a2")]
    _step(job, {"split-a": FAILED_A})
    fields, actions = _step(job, {"split-a": FAILED_A})
    assert (fields["status"], fields["endReason"]) == ("failed", "split_failed")
    assert {"kind": "queue_email"} in actions


def _solfege_job(failed_rows, mode="solfege"):
    rows = [{"rowId": row_id, "partId": row_id, "sourcePartId": row_id, "include": True,
             "name": f"Made-up {row_id}"} for row_id in ("p0", "p1")]
    items = [{**_take(row["rowId"]), "mode": "solfege"} for row in rows]
    return {"jobId": "job", "status": "running", "stage": "solfege", "solfegePrepared": True,
            "solfegeFailedRows": failed_rows, "choices": {"mode": mode},
            "rows": rows, "items": items, "decision": {"pause": 1, "answer": {"action": "continue"}}}


def test_solfege_only_pauses_for_some_rows_and_fails_for_all():
    job = _solfege_job(["p1"])
    fields, actions = _step(job, {})
    assert fields["status"] == "needs_decision"
    # A job can pause twice; each pause has its own number.
    assert (fields["decision"]["pause"], fields["decision"]["stage"]) == (2, "solfege")
    assert fields["decision"]["parts"] == [
        {"sourcePartId": "p1", "name": "Made-up p1", "state": "failed",
         "reason": "solfege_failed", "message": None, "madeParts": [], "rowIds": ["p1"]}]
    job["decision"]["answer"] = {"action": "continue", "accept": [], "heldCredits": 2}
    fields, actions = _step(job, {})
    assert (fields["status"], fields["stage"]) == ("running", "singing")
    assert [item["status"] for item in job["items"]] == ["queued", "skipped"]
    assert actions == [{"kind": "release", "reservationId": "take-p1"}]

    job = _solfege_job(["p0", "p1"])
    fields, _ = _step(job, {})
    assert (fields["status"], fields["endReason"]) == ("failed", "nothing_to_sing")

    # Lyrics also chosen: the solfège takes are refunded and the job goes on, as in step 3.
    job = _solfege_job(["p1"], mode="both")
    fields, _ = _step(job, {})
    assert fields["stage"] == "singing" and "status" not in fields


def test_a_job_whose_wanted_takes_are_all_made_is_completed():
    job = _solfege_job([])
    job.update(stage="packaging", package={"state": "done", "mixes": {"done": 0}})
    job["items"][0].update(status="done", creditsCharged=2)
    job["items"][1].update(status="skipped", reason="solfege_failed")
    fields, _ = _step(job, {})
    assert fields["status"] == "completed"


# The paused job through Firestore, the runner, the credits and the emails.

SELECTION = {"number": "1", "name": "Made-up line"}


def _paused_in_firestore(uid, session_id, job_id, monkeypatch, *, settings, takes_succeed=(True, True)):
    parent = _parent(uid, session_id, job_id)
    parent.update(stage="solfege", solfegePrepared=True, solfegeFailedRows=["P1"],
                  solfegeSelections={"P0": SELECTION, "P1": SELECTION},
                  notifyEmail=OWNER_EMAIL)
    parent["choices"].update(mode="solfege", solfegeOnly=True)
    for item in parent["items"]:
        if item["type"] == "take":
            item["mode"] = "solfege"
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    runner, _children = _runner(uid, job_id, takes_succeed=takes_succeed,
                                package={"state": "done", "made": 1}, monkeypatch=monkeypatch)
    history = []

    async def append_history(_session_id, role, message, **kwargs):
        history.append((role, message, kwargs))

    runner.orchestrator._sessions.append_history = append_history
    runner.orchestrator._settings = settings
    asyncio.run(runner.reconcile(job_id))
    return runner, history


def test_pause_email_then_continue_ends_completed_with_the_left_out_part_refunded(
    email_environment, monkeypatch, brevo,
):
    _db, uid, session_id, job_id = email_environment
    before = get_or_create_credits(uid, "").available_balance
    runner, history = _paused_in_firestore(uid, session_id, job_id, monkeypatch, settings=_settings())
    paused = sing_all_store.load(job_id)
    assert paused["status"] == "needs_decision"
    assert get_or_create_credits(uid, "").reserved == paused["quote"]["total"]
    assert len(history) == 1 and "Line 1" in history[0][1]
    # The one-active guard still holds: the same Song is locked and a second job is refused.
    assert sing_all_store.active_job_for_song(uid, session_id)["jobId"] == job_id
    assert job_id in sing_all_store.running_jobs()

    assert _outbox_ids(job_id) == [f"{job_id}-needs_decision-1"]
    pause_doc = (get_firestore_client().collection(sing_all_mail.OUTBOX)
                 .document(sing_all_mail.outbox_id(job_id, "needs_decision", 1)).get().to_dict())
    assert (pause_doc["state"], pause_doc["pause"]) == ("sent", 1)
    sent = brevo.requests[0]["json"]
    assert sent["subject"] == "Your song needs a decision: Made-up tune"
    assert sent["textContent"].startswith("1 of your parts can't be sung as quoted.")
    assert f"Choose how to go on: https://app.example.test/jobs/{job_id}" in sent["textContent"]

    choice = decision_choice(paused, [])
    assert sing_all_store.answer_decision(job_id, "someone-else", action="cancel", accept=[],
                                          held_credits=None)[0] == "job_not_found"
    assert sing_all_store.answer_decision(job_id, uid, action="continue", accept=["P0"],
                                          held_credits=choice["heldCredits"])[0] == "invalid_decision"
    assert sing_all_store.answer_decision(job_id, uid, action="continue", accept=[],
                                          held_credits=choice["heldCredits"] + 1)[0] == "decision_changed"
    outcome, answered = sing_all_store.answer_decision(
        job_id, uid, action="continue", accept=[], held_credits=choice["heldCredits"])
    assert outcome == "answered" and answered["decision"]["answer"]["action"] == "continue"
    assert sing_all_store.answer_decision(job_id, uid, action="cancel", accept=[],
                                          held_credits=None)[0] == "decision_closed"

    for _ in range(3):
        asyncio.run(runner.reconcile(job_id))
    finished = sing_all_store.load(job_id)
    assert (finished["status"], finished["stage"]) == ("completed", "done")
    takes = [item for item in finished["items"] if item["type"] == "take"]
    assert [(item["status"], item["reason"]) for item in takes] == [
        ("done", None), ("skipped", "solfege_failed")]
    public = progress(finished)
    assert get_or_create_credits(uid, "").reserved == 0
    assert get_or_create_credits(uid, "").available_balance == before - public["credits"]["charged"]
    assert public["counts"]["takes"]["skipped"] == 1
    ended = brevo.requests[-1]["json"]
    assert ended["subject"] == "Your song is ready: Made-up tune"
    assert ended["textContent"].startswith("Every part you chose to go on with is sung.")


@pytest.mark.parametrize("case", ["cancel", "stop", "timeout"])
def test_cancel_stop_and_timeout_in_firestore(email_environment, monkeypatch, brevo, case):
    _db, uid, session_id, job_id = email_environment
    before = get_or_create_credits(uid, "").available_balance
    runner, _history = _paused_in_firestore(uid, session_id, job_id, monkeypatch,
                                            settings=_settings())
    paused = sing_all_store.load(job_id)
    if case == "cancel":
        assert sing_all_store.answer_decision(job_id, uid, action="cancel", accept=[],
                                              held_credits=None)[0] == "answered"
        asyncio.run(runner.reconcile(job_id))
    elif case == "stop":
        assert sing_all_store.apply(job_id, {"cancelRequested": True}, expected_stage="solfege",
                                    expected_revision=paused["revision"],
                                    expected_status="needs_decision")
        asyncio.run(runner.reconcile(job_id))
    else:
        asyncio.run(runner.reconcile(job_id, sweep=True))
        assert sing_all_store.load(job_id)["status"] == "needs_decision"
        later = paused["decision"]["deadline"] + timedelta(minutes=1)
        asyncio.run(runner.reconcile(job_id, sweep=True, now=later))
    finished = sing_all_store.load(job_id)
    reason = {"cancel": "decision_cancelled", "stop": "stopped", "timeout": "decision_timeout"}[case]
    assert (finished["status"], finished["endReason"]) == ("cancelled", reason)
    assert get_or_create_credits(uid, "").reserved == 0
    assert get_or_create_credits(uid, "").available_balance == before
    assert not get_firestore_client().collection("users").document(uid).get().to_dict().get(
        "activeSingAllJobId")
    subjects = [request["json"]["subject"] for request in brevo.requests]
    if case == "timeout":
        assert subjects[-1] == "We cancelled your song: Made-up tune"
        text = brevo.requests[-1]["json"]["textContent"]
        assert text.startswith("You didn't choose how to go on within 24 hours")
        assert _outbox_ids(job_id) == sorted([f"{job_id}-cancelled", f"{job_id}-needs_decision-1"])
    else:
        assert subjects == ["Your song needs a decision: Made-up tune"]


def test_a_pause_email_answered_before_it_was_sent_is_not_sent(email_environment, monkeypatch, brevo):
    _db, uid, session_id, job_id = email_environment
    brevo.statuses = [503]
    runner, _history = _paused_in_firestore(uid, session_id, job_id, monkeypatch,
                                            settings=_settings())
    doc_id = sing_all_mail.outbox_id(job_id, "needs_decision", 1)
    assert get_firestore_client().collection(sing_all_mail.OUTBOX).document(doc_id).get().to_dict()[
        "state"] == "pending"
    paused = sing_all_store.load(job_id)
    sing_all_store.answer_decision(job_id, uid, action="continue", accept=[],
                                   held_credits=decision_choice(paused, [])["heldCredits"])
    asyncio.run(runner.reconcile(job_id, max_passes=1))
    asyncio.run(sing_all_mail.send_unsent(_settings()))
    assert get_firestore_client().collection(sing_all_mail.OUTBOX).document(doc_id).get().to_dict()[
        "state"] == "obsolete"
    assert len(brevo.requests) == 1


def _decision_route(monkeypatch, outcome, job=None):
    calls = []

    async def user_id(_request):
        return "owner"

    async def enqueue(job_id):
        calls.append(("enqueue", job_id))

    class Runner:
        def __init__(self, _orchestrator):
            pass

        async def reconcile(self, job_id, **kwargs):
            calls.append(("reconcile", job_id, kwargs))

    monkeypatch.setattr(main, "_get_user_id_or_401", user_id)
    monkeypatch.setattr(main, "answer_sing_all_decision_record",
                        lambda *args, **kwargs: (calls.append(("answer", args, kwargs)) or (outcome, job)))
    monkeypatch.setattr(main, "SingAllRunner", Runner)
    monkeypatch.setattr(main, "load_sing_all_job", lambda job_id: job)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        orchestrator=SimpleNamespace(_enqueue_sing_all_reconcile=enqueue))))
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == "/sing-all/jobs/{job_id}/decision")
    return endpoint, request, calls


@pytest.mark.parametrize("outcome,status,with_job", [
    ("job_not_found", 404, False), ("invalid_decision", 422, False),
    ("nothing_to_sing", 409, False), ("decision_closed", 409, True),
    ("decision_changed", 409, True),
])
def test_decision_route_answers_each_refusal(monkeypatch, outcome, status, with_job):
    job, _, _ = _paused_job()
    endpoint, request, calls = _decision_route(monkeypatch, outcome, job)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(endpoint("job", main.SingAllDecisionRequest(action="cancel"), request))
    assert raised.value.status_code == status
    assert raised.value.detail["code"] == outcome
    assert ("job" in raised.value.detail) is with_job
    assert [call[0] for call in calls] == ["answer"]


def test_decision_route_carries_out_the_answer_once_then_queues_the_rest(monkeypatch):
    job, _, _ = _paused_job()
    endpoint, request, calls = _decision_route(monkeypatch, "answered", job)
    result = asyncio.run(endpoint(
        "job", main.SingAllDecisionRequest(action="continue", accept=["B"], heldCredits=8), request))
    assert result["jobId"] == "job"
    assert calls[0][2] == {"action": "continue", "accept": ["B"], "held_credits": 8}
    assert calls[1] == ("reconcile", "job", {"max_passes": 1})
    assert calls[2] == ("enqueue", "job")
