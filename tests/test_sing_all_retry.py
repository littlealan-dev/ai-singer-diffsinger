"""Step 6: Retry failed parts sings a job's failed takes again as a new job that finishes the song."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.backend import main, sing_all_mix, sing_all_store
from src.backend.credits import get_or_create_credits
from src.backend.sing_all import progress, retry_items, retry_job, retry_quote, retryable
from tests.test_sing_all_mail import (  # noqa: F401
    OWNER_EMAIL, _outbox_ids, _run, _settings, brevo, email_environment,
)
from tests.test_sing_all_mixes_credits import (  # noqa: F401
    MIX_FILES, TAKE_CREDITS, _runner, mixes_environment,
)


def _ended(status="partial", *, mixes_charged=1, instrumental=True):
    items = [
        {"itemId": "take:P0:lyrics", "type": "take", "rowId": "P0", "mode": "lyrics",
         "childJobId": "old-take-0", "status": "done", "reason": None,
         "creditsReserved": 2, "creditsCharged": 2},
        {"itemId": "take:P1:lyrics", "type": "take", "rowId": "P1", "mode": "lyrics",
         "childJobId": "old-take-1", "status": "failed", "reason": "stub_worker_failed",
         "creditsReserved": 3, "creditsCharged": 0, "startedAt": "made-up"},
        {"itemId": "take:P2:solfege", "type": "take", "rowId": "P2", "mode": "solfege",
         "childJobId": "old-take-2", "status": "failed", "reason": "solfege_failed",
         "creditsReserved": 2, "creditsCharged": 0},
        {"itemId": "mixes", "type": "mixes", "reservationId": "old-mixes", "files": 3,
         "status": "done", "filesMade": 1, "creditsReserved": 3, "creditsCharged": mixes_charged},
    ]
    if instrumental:
        items.append({"itemId": "instrumental", "type": "instrumental",
                      "reservationId": "old-instrumentals", "status": "done",
                      "creditsReserved": 1, "creditsCharged": 1})
    return {"jobId": "old", "uid": "made-up-user", "sessionId": "made-up-song",
            "scoreId": "made-up-score", "title": "Made-up tune", "status": status,
            "stage": "done", "instrumentalsPrepared": True, "finalScoreVersion": 4,
            "choices": {"mode": "lyrics", "mixes": "all", "language": "en", "solfegeOnly": False,
                        "verse": "1", "repeats": False},
            "createdAt": "2026-01-02T12:00:00+00:00", "quote": {"total": 10},
            "rows": [{"rowId": f"P{index}", "partId": f"P{index}", "name": f"Line {index}",
                      "voiceName": "Made-up voice"} for index in range(3)],
            "solfegeSelections": {"P2": {"number": "9"}}, "items": items}


@pytest.mark.parametrize("change,expected", [
    ({}, True),
    ({"status": "failed"}, True),
    ({"retriedBy": "newer"}, False),
    ({"status": "cancelled"}, False),
    ({"status": "completed"}, False),
    # Failed before singing (a split, the score changed): nothing a retry can repeat.
    ({"instrumentalsPrepared": False}, False),
])
def test_who_can_retry(change, expected):
    assert retryable({**_ended(), **change}) is expected
    assert progress({**_ended(), **change})["retryable"] is expected


def test_only_solfege_that_could_not_be_added_is_not_retried():
    job = _ended()
    job["items"][1].update(status="stopped", reason="stopped")
    assert retryable(job) is False


@pytest.mark.parametrize("mixes_charged,instrumental_required", [(1, False), (0, True)])
def test_retry_items_carry_the_made_takes_and_price_the_failed_ones_as_before(
    mixes_charged, instrumental_required,
):
    original = _ended(mixes_charged=mixes_charged)
    items = retry_items(original, job_id="new", instrumental_required=instrumental_required)
    by_id = {item["itemId"]: item for item in items}
    assert by_id["take:P0:lyrics"] == {**original["items"][0], "creditsReserved": 0,
                                       "creditsCharged": 0, "carried": True}
    again = by_id["take:P1:lyrics"]
    assert again["childJobId"] not in {"old-take-1", "old-take-0"}
    assert (again["status"], again["creditsReserved"], again["reason"]) == ("queued", 3, None)
    assert "startedAt" not in again and "carried" not in again
    assert "take:P2:solfege" not in by_id
    assert ("instrumental" in by_id) is instrumental_required
    if instrumental_required:
        assert by_id["instrumental"]["reservationId"] == "new-instrumentals"
    mixes = by_id["mixes"]
    assert mixes["reservationId"] == "new-mixes" and mixes["files"] == 3
    # Q1 A: free when the first job charged its mixes, the normal price otherwise.
    assert (mixes["included"], mixes["creditsReserved"]) == (
        (True, 0) if mixes_charged else (False, 3))
    quote = retry_quote(original, items)
    assert quote == {
        "jobId": "old",
        "takes": [{"rowId": "P1", "name": "Line 1", "mode": "lyrics",
                   "voiceName": "Made-up voice", "credits": 3}],
        "instrumentals": 1 if instrumental_required else 0,
        "mixes": {"files": 3, "credits": 0 if mixes_charged else 3, "included": bool(mixes_charged)},
        "total": 3 + (1 if instrumental_required else 0) + (0 if mixes_charged else 3),
    }
    job = retry_job(original, job_id="new", items=items, notify_email=OWNER_EMAIL)
    assert (job["retryOf"], job["stage"], job["status"]) == ("old", "singing", "running")
    assert job["quote"] == {"quoteId": None, "total": quote["total"], "scoreVersion": 4}
    assert job["solfegePrepared"] is True and job["notifyEmail"] == OWNER_EMAIL


def test_no_mixes_chosen_means_none_in_the_retry():
    original = _ended()
    original["items"] = [item for item in original["items"] if item["type"] != "mixes"]
    items = retry_items(original, job_id="new", instrumental_required=False)
    assert retry_quote(original, items)["mixes"] is None


# Through Firestore: the original is partial, the retry finishes the song.

def _retry_in_firestore(uid, session_id, job_id, monkeypatch, *, retry_succeeds):
    original = sing_all_store.load(job_id)
    new_id = f"{job_id}-retry"
    items = retry_items(original, job_id=new_id, instrumental_required=False)
    for item in items:
        if item["type"] == "take" and not item.get("carried"):
            # The stub runner reads the take's index from its child id.
            item["childJobId"] = f"{new_id}-take-{item['rowId'][1:]}"
    job = retry_job(original, job_id=new_id, items=items, notify_email=OWNER_EMAIL)
    assert sing_all_store.create_job_with_reservations(job).status == "reserved"
    runner, _children = _runner(uid, new_id, takes_succeed=(True, retry_succeeds),
                                package={"state": "done", "made": MIX_FILES},
                                monkeypatch=monkeypatch)

    async def instrumentals(_session_id, score_summary=None, **_kwargs):
        return score_summary, None

    runner.orchestrator._ensure_instrumental_midi_artifacts = instrumentals
    runner.orchestrator._settings = _settings()
    for _ in range(3):
        asyncio.run(runner.reconcile(new_id))
    return job, sing_all_store.load(new_id)


def test_a_retry_finishes_the_song_charging_only_the_takes_sung_again(
    email_environment, monkeypatch, brevo,
):
    _db, uid, session_id, job_id = email_environment
    _run(uid, session_id, job_id, monkeypatch, takes_succeed=(True, False),
         package={"state": "done", "made": 1}, settings=_settings())
    original = sing_all_store.load(job_id)
    assert original["status"] == "partial" and retryable(original)
    before = get_or_create_credits(uid, "").available_balance
    planned = []
    original_plan = sing_all_mix.plan_mixes

    def plan_mixes(refs, *args):
        planned.append(sorted(ref.part_id for ref in refs))
        return original_plan(refs, *args)

    monkeypatch.setattr(sing_all_mix, "plan_mixes", plan_mixes)

    job, finished = _retry_in_firestore(uid, session_id, job_id, monkeypatch, retry_succeeds=True)
    assert sing_all_store.load(job_id)["retriedBy"] == job["jobId"]
    assert not retryable(sing_all_store.load(job_id))
    # A second retry of the same job is refused in the reservation transaction.
    again = {**job, "jobId": f"{job_id}-again"}
    assert sing_all_store.create_job_with_reservations(again).reason == "not_retryable"

    assert (finished["status"], finished["stage"]) == ("completed", "done")
    public = progress(finished)
    assert get_or_create_credits(uid, "").reserved == 0
    assert public["credits"]["charged"] == TAKE_CREDITS
    assert get_or_create_credits(uid, "").available_balance == before - TAKE_CREDITS
    receipt = {(row["type"], row["rowId"]): row for row in public["receipt"]}
    assert (receipt[("take", "P0")]["outcome"], receipt[("take", "P0")]["credits"]) == ("earlier", 0)
    assert receipt[("take", "P1")]["outcome"] == "charged"
    assert receipt[("mixes", None)]["outcome"] == "included"
    assert public["rows"][0]["takes"][0]["carried"] is True
    # The mixes and zip are of the whole song: the carried take is mixed too.
    assert planned == [["P0", "P1"]]
    assert brevo.requests[-1]["json"]["subject"] == "Your song is ready: Made-up tune"
    assert "- Line 0: sung earlier" in brevo.requests[-1]["json"]["textContent"]


def test_a_retry_whose_takes_fail_again_ends_failed_and_can_be_retried(
    email_environment, monkeypatch, brevo,
):
    _db, uid, session_id, job_id = email_environment
    _run(uid, session_id, job_id, monkeypatch, takes_succeed=(True, False),
         package={"state": "done", "made": 1}, settings=_settings())
    before = get_or_create_credits(uid, "").available_balance
    _job, finished = _retry_in_firestore(uid, session_id, job_id, monkeypatch, retry_succeeds=False)
    assert (finished["status"], finished["stage"]) == ("failed", "done")
    assert finished.get("package") is None
    assert retryable(finished)
    assert get_or_create_credits(uid, "").reserved == 0
    assert get_or_create_credits(uid, "").available_balance == before


def _retry_route(monkeypatch, original, *, version=4, plan="solo_monthly", reserve=None):
    calls = []

    async def user_id(_request):
        return "made-up-user"

    async def user_context(_request):
        return "made-up-user", OWNER_EMAIL

    async def snapshot(_sessions, _session_id, _uid):
        return {"score_id": "made-up-score", "current_score": {"version": version}}

    async def enqueue(job_id):
        calls.append(("enqueue", job_id))

    class _Doc:
        def get(self):
            billing = ({"activePlanKey": plan, "billingInterval": "month", "stripeSubscriptionStatus": "active"}
                       if plan not in (None, "free") else {"activePlanKey": plan})
            return SimpleNamespace(to_dict=lambda: {"billing": billing})

    db = SimpleNamespace(collection=lambda _name: SimpleNamespace(document=lambda _id: _Doc()))
    monkeypatch.setattr(main, "_get_user_id_or_401", user_id)
    monkeypatch.setattr(main, "_get_user_context_or_401", user_context)
    monkeypatch.setattr(main, "_get_snapshot_or_404", snapshot)
    monkeypatch.setattr(main, "load_sing_all_job", lambda job_id: deepcopy(original))
    monkeypatch.setattr(main, "get_instrumental_charge_state", lambda scope: "paid")
    monkeypatch.setattr(main, "get_firestore_client", lambda: db)
    monkeypatch.setattr(main, "create_job_with_reservations", lambda job, **kwargs: (
        calls.append(("reserve", job)) or reserve or SimpleNamespace(status="reserved")))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        sessions=None, orchestrator=SimpleNamespace(_enqueue_sing_all_reconcile=enqueue))))
    routes = {(route.path, method): route.endpoint for route in main.app.routes
              if getattr(route, "path", "").endswith("/retry") for method in route.methods}
    return (routes[("/sing-all/jobs/{job_id}/retry", "GET")],
            routes[("/sing-all/jobs/{job_id}/retry", "POST")], request, calls)


def test_retry_routes_quote_then_start(monkeypatch):
    get, post, request, calls = _retry_route(monkeypatch, _ended())
    quote = asyncio.run(get("old", request))
    assert quote["total"] == 3 and calls == []
    with pytest.raises(HTTPException) as raised:
        asyncio.run(post("old", main.SingAllRetryRequest(total=2), request))
    assert raised.value.detail == {"code": "quote_changed", "quote": quote}
    result = asyncio.run(post("old", main.SingAllRetryRequest(total=3), request))
    assert result["retryOf"] == "old" and result["stage"] == "singing"
    reserved = calls[0][1]
    assert reserved["notifyEmail"] == OWNER_EMAIL and reserved["retryOf"] == "old"
    assert calls[1] == ("enqueue", reserved["jobId"])


@pytest.mark.parametrize("original,version,plan,reserve,status,code", [
    ({**_ended(), "uid": "someone-else"}, 4, "solo_monthly", None, 404, "job_not_found"),
    ({**_ended(), "retriedBy": "newer"}, 4, "solo_monthly", None, 409, "not_retryable"),
    (_ended(), 5, "solo_monthly", None, 409, "score_changed"),
    (_ended(), 4, "free", None, 409, "free_plan"),
    (_ended(), 4, None, None, 409, "free_plan"),
    (_ended(), 4, "solo_monthly", SimpleNamespace(status="insufficient_balance", reason=None),
     409, "not_enough_credits"),
])
def test_retry_route_refusals(monkeypatch, original, version, plan, reserve, status, code):
    get, post, request, _calls = _retry_route(monkeypatch, original, version=version,
                                              plan=plan, reserve=reserve)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(post("old", main.SingAllRetryRequest(total=3), request))
    assert (raised.value.status_code, raised.value.detail["code"]) == (status, code)
