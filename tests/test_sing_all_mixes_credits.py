"""M6: the mixes are reserved with the takes, charged for the files made, and refunded otherwise."""

import asyncio
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.backend import main, sing_all_package, sing_all_store
from src.backend.credits import (
    ReservationItem, get_or_create_credits, release_credits, reserve_credits_for_items,
    settle_credits, settle_mixes_reservation,
)
from src.backend.firebase_app import get_firestore_client
from src.backend.sing_all import progress
from src.backend.sing_all_runner import SingAllRunner
from tests.test_sing_all_quote import _route as quote_route, _row


TAKE_CREDITS = 2
MIX_FILES = 3


@pytest.fixture
def mixes_environment(firestore_emulator, monkeypatch):
    monkeypatch.setenv("GCLOUD_PROJECT", "demo-project")
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8080")
    db = get_firestore_client()
    suffix = uuid.uuid4().hex
    uid, session_id, job_id = f"example-user-{suffix}", f"example-song-{suffix}", f"example-job-{suffix}"
    get_or_create_credits(uid, "")
    db.collection("sessions").document(session_id).set({"userId": uid, "history": []})
    yield db, uid, session_id, job_id
    db.collection("singAllJobs").document(job_id).delete()
    db.collection("sessions").document(session_id).delete()
    db.collection("users").document(uid).delete()
    for name in (f"{job_id}-take-0", f"{job_id}-take-1", f"{job_id}-mixes"):
        db.collection("credit_reservations").document(name).delete()


def _reservation(job_id):
    return get_firestore_client().collection("credit_reservations").document(job_id).get().to_dict()


def test_mixes_reservation_settles_for_files_made_and_frees_the_rest(mixes_environment):
    _db, uid, _session_id, job_id = mixes_environment
    before = get_or_create_credits(uid, "").available_balance
    reservation_id = f"{job_id}-mixes"
    result = reserve_credits_for_items(
        uid, [ReservationItem(job_id=reservation_id, estimated_credits=MIX_FILES,
                              billing_components=("mixes",), job_kind="sing_all_mixes")],
        guard=SimpleNamespace(read=lambda _transaction: None, write=lambda _transaction: None),
        session_id="example-song", score_id="example-score", score_version_no=1,
        parent_job_id=job_id)
    assert result.status == "reserved"
    assert get_or_create_credits(uid, "").reserved == MIX_FILES

    # A charge above the reservation is refused and changes nothing.
    assert settle_mixes_reservation(uid, reservation_id, MIX_FILES + 1).status == "reconciliation_required"
    assert _reservation(reservation_id)["status"] == "pending"
    assert get_or_create_credits(uid, "").reserved == MIX_FILES

    settled = settle_mixes_reservation(uid, reservation_id, 2)
    assert (settled.status, settled.actual_credits) == ("settled", 2)
    assert settle_mixes_reservation(uid, reservation_id, 2).status == "already_settled"
    assert release_credits(uid, reservation_id).status == "already_settled"
    record = _reservation(reservation_id)
    assert (record["status"], record["actualCredits"], record["jobKind"]) == ("settled", 2, "sing_all_mixes")
    credits = get_or_create_credits(uid, "")
    assert credits.reserved == 0
    assert credits.available_balance == before - 2


def _parent(uid, session_id, job_id, *, mixes_files=MIX_FILES):
    rows = [{"rowId": f"P{index}", "partId": f"P{index}", "sourcePartId": f"P{index}",
             "name": f"Line {index}", "voice": "example-voice", "include": True}
            for index in range(2)]
    items = [{
        "itemId": f"take:P{index}:lyrics", "type": "take", "rowId": f"P{index}",
        "mode": "lyrics", "childJobId": f"{job_id}-take-{index}",
        "creditsReserved": TAKE_CREDITS, "creditsCharged": 0, "estimatedSeconds": 59,
        "unitSeconds": 30, "status": "queued", "reason": None,
    } for index in range(2)]
    items.append({
        "itemId": "mixes", "type": "mixes", "files": mixes_files,
        "reservationId": f"{job_id}-mixes", "estimatedSeconds": 0, "unitSeconds": None,
        "range": None, "creditsReserved": mixes_files, "creditsCharged": 0,
        "status": "queued", "reason": None,
    })
    now = datetime.now(timezone.utc)
    return {
        "jobId": job_id, "uid": uid, "sessionId": session_id, "title": "Made-up tune",
        "scoreId": "example-score", "status": "running", "stage": "singing",
        "revision": 0, "instrumentalsPrepared": True,
        "quote": {"total": 2 * TAKE_CREDITS + mixes_files, "scoreVersion": 1},
        "items": items, "rows": rows, "cancelRequested": False,
        "choices": {"verse": 1, "language": "en", "solfegeOnly": False, "mode": "lyrics",
                    "repeats": False, "mixes": "all"},
        "createdAt": now, "updatedAt": now, "lastProgressAt": now,
    }


def _runner(uid, job_id, *, takes_succeed, monkeypatch, package=None, stop_before_start=False):
    """A runner whose takes and package run are stubs; credits and the parent are real."""
    selection = {"number": "1", "name": "Made-up line"}
    snapshot = {
        "score_id": "example-score",
        "current_score": {"version": 1, "score": {
            "selected_verse_number": 1, "selected_lyric_selection": selection}},
        "score_summary": {"parts": [
            {"part_id": f"P{index}", "lyric_selections": [selection]} for index in range(2)]},
    }
    children = {}

    async def get_snapshot(*_args):
        return snapshot

    async def start_take(_session_id, _score, _args, **kwargs):
        child_id = kwargs["parent"]["childJobId"]
        index = int(child_id.rsplit("-", 1)[1])
        if takes_succeed[index]:
            assert (await asyncio.to_thread(settle_credits, uid, child_id, 59)).status == "settled"
            children[child_id] = {"status": "completed", "creditsCharged": TAKE_CREDITS}
        else:
            assert (await asyncio.to_thread(release_credits, uid, child_id)).status == "released"
            children[child_id] = {"status": "failed", "error": "stub_worker_failed"}
        return {"job_id": child_id}

    async def start_package(package_job_id, _settings):
        if package is not None:
            get_firestore_client().collection("singAllJobs").document(package_job_id).update({
                "package.state": package["state"],
                "package.mixes.done": package["made"],
                "package.stats.balance": {},
                "package.updatedAt": datetime.now(timezone.utc),
            })

    monkeypatch.setattr(sing_all_package, "start_package", start_package)
    original_action = SingAllRunner._action

    async def action(runner, job, request, snapshot_value):
        if request["kind"] == "start_package":
            await sing_all_package.start_package(job["jobId"], None)
            return None
        if request["kind"] == "set_opening_balance":
            return None
        return await original_action(runner, job, request, snapshot_value)

    monkeypatch.setattr(SingAllRunner, "_action", action)
    orchestrator = SimpleNamespace(
        _sessions=SimpleNamespace(get_snapshot=get_snapshot),
        _job_store=SimpleNamespace(get_job=lambda child_id: children.get(child_id)),
        _normalize_synthesize_voice_color=lambda args: asyncio.sleep(0, result=args),
        _start_synthesis_job=start_take,
        _settings=SimpleNamespace(max_render_slots=1),
    )
    return SingAllRunner(orchestrator), children


def _assert_credits_add_up(uid, before, finished):
    public = progress(finished)
    credits = get_or_create_credits(uid, "")
    assert credits.reserved == 0
    assert public["credits"]["reserved"] == 0
    assert public["credits"]["charged"] + public["credits"]["refunded"] == finished["quote"]["total"]
    assert credits.available_balance == before - public["credits"]["charged"]
    return public


@pytest.mark.parametrize("ending,takes_succeed,package,status,charged_files", [
    ("completed", (True, True), {"state": "done", "made": MIX_FILES}, "completed", MIX_FILES),
    # One take failed: the package plans fewer files than the quote priced.
    ("partial", (True, False), {"state": "done", "made": 1}, "partial", 1),
    ("package_failed", (True, True), {"state": "failed", "made": 0}, "completed", 0),
    ("no_take_done", (False, False), None, "failed", 0),
])
def test_every_ending_charges_mixes_made_and_refunds_the_rest(
    mixes_environment, monkeypatch, ending, takes_succeed, package, status, charged_files,
):
    _db, uid, session_id, job_id = mixes_environment
    before = get_or_create_credits(uid, "").available_balance
    assert sing_all_store.create_job_with_reservations(
        _parent(uid, session_id, job_id)).status == "reserved"
    assert _reservation(f"{job_id}-mixes")["status"] == "pending"
    assert get_or_create_credits(uid, "").reserved == 2 * TAKE_CREDITS + MIX_FILES

    runner, _children = _runner(uid, job_id, takes_succeed=takes_succeed,
                                package=package, monkeypatch=monkeypatch)
    for _ in range(3):
        asyncio.run(runner.reconcile(job_id))
    finished = sing_all_store.load(job_id)
    assert (finished["status"], finished["stage"]) == (status, "done")
    public = _assert_credits_add_up(uid, before, finished)

    mixes_rows = [row for row in public["receipt"] if row["type"] == "mixes"]
    expected = []
    if charged_files:
        expected.append({"itemId": "mixes", "outcome": "charged",
                         "credits": charged_files, "files": charged_files})
    if MIX_FILES - charged_files:
        expected.append({"itemId": "mixes:refunded", "outcome": "refunded",
                         "credits": MIX_FILES - charged_files, "files": MIX_FILES - charged_files})
    assert [{key: row[key] for key in ("itemId", "outcome", "credits", "files")}
            for row in mixes_rows] == expected
    assert all(row["files"] is None for row in public["receipt"] if row["type"] != "mixes")
    reservation = _reservation(f"{job_id}-mixes")
    assert reservation["status"] == ("settled" if charged_files else "released")


def test_stop_before_packaging_refunds_the_mixes(mixes_environment, monkeypatch):
    _db, uid, session_id, job_id = mixes_environment
    before = get_or_create_credits(uid, "").available_balance
    parent = _parent(uid, session_id, job_id)
    assert sing_all_store.create_job_with_reservations(parent).status == "reserved"
    get_firestore_client().collection("singAllJobs").document(job_id).update({"cancelRequested": True})
    runner, _children = _runner(uid, job_id, takes_succeed=(True, True), package=None,
                                monkeypatch=monkeypatch)
    for _ in range(2):
        asyncio.run(runner.reconcile(job_id))
    finished = sing_all_store.load(job_id)
    assert (finished["status"], finished["endReason"]) == ("cancelled", "stopped")
    public = _assert_credits_add_up(uid, before, finished)
    assert public["credits"]["charged"] == 0
    assert _reservation(f"{job_id}-mixes")["status"] == "released"
    assert [row["itemId"] for row in public["receipt"] if row["type"] == "mixes"] == ["mixes:refunded"]


def test_stop_after_every_take_keeps_the_mixes_for_packaging():
    from src.backend.sing_all import decide

    job = _parent("example-user", "example-song", "example-job")
    for item in job["items"]:
        if item["type"] == "take":
            item["status"] = "running"
    job["cancelRequested"] = True
    children = {item["childJobId"]: {"status": "completed", "creditsCharged": TAKE_CREDITS}
                for item in job["items"] if item["type"] == "take"}
    fields, actions = decide(job, children, {"now": datetime.now(timezone.utc), "scoreSummary": {}})
    assert fields["stage"] == "packaging"
    mixes = next(item for item in fields["items"] if item["type"] == "mixes")
    assert mixes["status"] == "queued"
    assert {"kind": "release", "reservationId": "example-job-mixes"} not in actions
    assert {"kind": "start_package"} in actions


def test_stale_package_refunds_the_mixes():
    from src.backend.sing_all import decide

    job = _parent("example-user", "example-song", "example-job")
    for item in job["items"]:
        if item["type"] == "take":
            item.update(status="done", creditsCharged=TAKE_CREDITS)
    now = datetime.now(timezone.utc)
    job.update(stage="packaging", package={"state": "mixing", "mixes": {"done": 2, "total": 3},
                                           "updatedAt": now - timedelta(minutes=11)})
    fields, actions = decide(job, {}, {"now": now, "sweep": True})
    assert fields["package"]["state"] == "failed"
    mixes = next(item for item in fields["items"] if item["type"] == "mixes")
    assert (mixes["status"], mixes["reason"]) == ("failed", "package_failed")
    assert {"kind": "release", "reservationId": "example-job-mixes"} in actions
    assert not any(action["kind"] == "settle_mixes" for action in actions)


def test_quote_prices_the_mixes_and_counts_them_against_the_balance(monkeypatch):
    rows = [{**_row(), "rowId": f"line-{index}", "partId": f"P{index}",
             "sourcePartId": f"P{index}", "name": f"Line {index}"}
            for index in range(2)]
    endpoint, request, snapshot = quote_route(
        monkeypatch, user={"credits": {"balance": 400}}, rows=rows)
    snapshot["score_summary"]["performance_midi"] = {"instrumental_parts": [
        {"eligible": True, "raw_part_id": "piano", "program_source": "score",
         "playback_preset": {"bank": 0, "program": 0}}]}
    choices = {"mode": "lyrics", "rows": [{"rowId": row["rowId"], "include": True} for row in rows]}

    def quote(**extra):
        return asyncio.run(endpoint(main.SingAllQuoteRequest(
            songId="song-a", choices={**choices, **extra}), request))

    full_and_parts, full_only, none = quote(), quote(mixes="full"), quote(mixes="none")
    singing = next(line["credits"] for line in none["lines"] if line["kind"] == "singing")
    for result, files in ((full_and_parts, 3), (full_only, 1)):
        kinds = [line["kind"] for line in result["lines"]]
        assert kinds[-3:] == ["mixes", "total", "balance"]
        assert result["lines"][-3] == {"kind": "mixes", "files": files, "credits": files}
        assert result["total"] == singing + files
        assert result["lines"][-2] == {"kind": "total", "credits": result["total"]}
    assert "mixes" not in [line["kind"] for line in none["lines"]]
    assert none["total"] == singing

    # Enough for the takes but not for the mixes: blocked; without the mixes it can start.
    snapshot_user = {"credits": {"balance": singing + 1}}
    endpoint, request, snapshot = quote_route(monkeypatch, user=snapshot_user, rows=rows)
    snapshot["score_summary"]["performance_midi"] = deepcopy(
        {"instrumental_parts": [{"eligible": True, "raw_part_id": "piano",
                                 "program_source": "score",
                                 "playback_preset": {"bank": 0, "program": 0}}]})
    assert quote()["blocked"] == "not_enough_credits"
    assert quote(mixes="full")["blocked"] is None
