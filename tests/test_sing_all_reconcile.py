"""The Sing All state machine decides without Firestore or worker calls."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from src.backend.sing_all import decide, job_from_quote, summary_with_judged_instruments


def _job(stage="reading", *, instrumental=False):
    items = [
        {"itemId": "split:part-a", "type": "split", "sourcePartId": "part-a",
         "childJobId": "split-child", "status": "queued", "creditsReserved": 0},
        {"itemId": "take:lane-a:lyrics", "type": "take", "rowId": "lane-a",
         "mode": "lyrics", "childJobId": "take-a", "status": "queued",
         "creditsReserved": 2, "creditsCharged": 0},
        {"itemId": "take:lane-b:solfege", "type": "take", "rowId": "lane-b",
         "mode": "solfege", "childJobId": "take-b", "status": "queued",
         "creditsReserved": 2, "creditsCharged": 0},
    ]
    if instrumental:
        items.append({"itemId": "instrumental", "type": "instrumental",
                      "reservationId": "parent-instrumentals", "status": "queued",
                      "creditsReserved": 1, "creditsCharged": 0})
    return {"jobId": "parent", "uid": "fake-user", "scoreId": "fake-score",
            "status": "running", "stage": stage, "choices": {"verse": "1"},
            "rows": [{"rowId": "lane-a", "sourcePartId": "part-a", "split": True,
                      "include": True, "voice": "voice-a"},
                     {"rowId": "lane-b", "sourcePartId": "part-a", "split": True,
                      "include": True, "voice": "voice-b"}],
            "items": items}


def _score(job):
    return {"scoreId": "fake-score", "verse": "1", "rows": job["rows"]}


def test_quote_becomes_parent_with_fixed_child_and_reservation_ids():
    source = _job()
    quote = {"quoteId": "fake-quote", "uid": source["uid"],
             "sessionId": "fake-song", "scoreId": source["scoreId"],
             "scoreVersion": 2, "choicesHash": "fake-hash", "total": 4,
             "choices": source["choices"], "judgment": {"roles": {}},
             "rows": source["rows"], "items": source["items"]}
    parent = job_from_quote(quote, title="Example tune", job_id="fake-parent")
    assert parent["status"] == "running" and parent["stage"] == "reading"
    assert parent["quote"]["scoreVersion"] == 2
    ids = [item["childJobId"] for item in parent["items"]]
    assert len(ids) == len(set(ids))
    assert all(row["partId"] is None for row in parent["rows"])
    assert quote["items"][0]["childJobId"] == "split-child"


def test_reading_checks_score_and_saves_assignments_before_splitting():
    job = _job()
    fields, actions = decide(job, {}, _score(job))
    assert fields == {"stage": "splitting"}
    assert actions == [{"kind": "save_assignments"}]
    changed = _score(job)
    changed["rows"] = changed["rows"][:-1]
    fields, actions = decide(job, {}, changed)
    assert (fields["status"], fields["endReason"]) == ("failed", "quote_changed")
    assert {action["reservationId"] for action in actions if action["kind"] == "release"} == {
        "take-a", "take-b"}
    replaced = _score(job)
    replaced["scoreId"] = "another-score"
    assert decide(job, {}, replaced)[0]["endReason"] == "score_replaced"
    verse = _score(job)
    verse["verse"] = "2"
    assert decide(job, {}, verse) == ({}, [{"kind": "reparse_verse", "verse": "1"}])


def test_terminal_preparation_failure_releases_every_unsettled_item():
    job = _job("singing", instrumental=True)
    fields, actions = decide(job, {}, {"terminalFailure": "instrumental_failed"})
    assert (fields["status"], fields["endReason"], fields["stage"]) == (
        "failed", "instrumental_failed", "done")
    assert {action["reservationId"] for action in actions
            if action["kind"] == "release"} == {
                "take-a", "take-b", "parent-instrumentals"}


def test_splitting_uses_fixed_child_and_keeps_quoted_voices():
    job = _job("splitting")
    fields, actions = decide(job, {}, _score(job))
    assert actions == [{"kind": "start_split", "itemId": "split:part-a"}]
    assert fields["items"][0]["status"] == "running"
    job["items"] = fields["items"]
    assert decide(job, {}, _score(job))[1] == actions  # retry the same child id
    child = {"status": "completed", "parts": [
        {"laneKey": "lane-a", "partId": "new-a", "name": "Top"},
        {"laneKey": "lane-b", "partId": "new-b", "name": "Bottom"}]}
    fields, actions = decide(job, {"split-child": child}, _score(job))
    assert actions == []
    assert [(row["partId"], row["voice"]) for row in fields["rows"]] == [
        ("new-a", "voice-a"), ("new-b", "voice-b")]
    job.update(fields)
    assert decide(job, {"split-child": child}, _score(job)) == ({"stage": "solfege"}, [])


def test_split_start_lease_prevents_a_second_simultaneous_start():
    job = _job("splitting")
    now = datetime.now(timezone.utc)
    state = {**_score(job), "now": now}
    fields, actions = decide(job, {}, state)
    assert actions == [{"kind": "start_split", "itemId": "split:part-a"}]
    job.update(fields)
    assert decide(job, {}, state) == ({}, [])
    expired = {**state, "now": now + timedelta(seconds=61)}
    assert decide(job, {}, expired)[1] == actions
    queued = {"split-child": {"status": "queued"}}
    assert decide(job, queued, state) == ({}, [])
    assert decide(job, queued, expired)[1] == actions


def test_ticked_undeclared_part_is_saved_as_vocal_not_midi():
    route = {"score_instrument_id": "P2:default", "instrumental_candidate": True,
             "eligible_for_instrumental_midi": False, "instrumental_role": "undecided"}
    summary = {"parts": [{"part_id": "P2", "instruments": [route]}],
               "instrument_program_resolution": {
                   "unresolved_score_instrument_ids": ["P2:default"],
                   "instrumental_score_instrument_ids": [],
                   "unresolved_reasons": {"P2:default": "undeclared_instrument_role"}}}
    answer = {"roles": {"P2": "unsure"}, "sounds": {"P2:default": None}}
    vocal = summary_with_judged_instruments(summary, answer, sung_source_ids={"P2"})
    assert vocal["parts"][0]["instruments"][0]["instrumental_role"] == "not_instrumental"
    assert vocal["instrument_program_resolution"]["instrumental_score_instrument_ids"] == []
    accompaniment = summary_with_judged_instruments(summary, answer, sung_source_ids=set())
    assert accompaniment["parts"][0]["instruments"][0]["user_assignable"] is True


def test_split_failure_and_wrong_lanes_refund_every_take():
    job = _job("splitting")
    for child, reason in [
        ({"status": "failed"}, "split_failed"),
        ({"status": "completed", "reviewRequired": True, "parts": []}, "split_failed"),
        ({"status": "completed", "parts": [{"laneKey": "lane-a", "partId": "new-a"}]},
         "quote_changed"),
    ]:
        fields, actions = decide(job, {"split-child": child}, _score(job))
        assert fields["status"] == "failed" and fields["endReason"] == reason
        assert len([action for action in actions if action["kind"] == "release"]) == 2


def test_solfege_prepares_once_and_releases_a_failed_line():
    job = _job("solfege")
    assert decide(job, {}, _score(job)) == ({}, [{"kind": "add_solfege"}])
    job["solfegePrepared"] = True
    job["solfegeFailedRows"] = ["lane-b"]
    fields, actions = decide(job, {}, _score(job))
    assert fields["stage"] == "singing"
    assert fields["items"][2]["status"] == "failed"
    assert actions == [{"kind": "release", "reservationId": "take-b"}]


def test_solfege_start_lease_blocks_a_simultaneous_transform():
    job = _job("solfege")
    now = datetime.now(timezone.utc)
    state = {**_score(job), "now": now}
    fields, actions = decide(job, {}, state)
    assert actions == [{"kind": "add_solfege"}]
    assert fields["solfegeStartLeaseUntil"] == now + timedelta(seconds=60)
    job.update(fields)
    assert decide(job, {}, state) == ({}, [])
    expired = {**state, "now": now + timedelta(seconds=61)}
    assert decide(job, {}, expired)[1] == actions


def test_singing_starts_each_take_then_finishes_partial():
    job = _job("singing")
    job["instrumentalsPrepared"] = True
    job["items"][0]["status"] = "done"
    fields, actions = decide(job, {}, _score(job))
    assert actions == [{"kind": "start_take", "itemId": "take:lane-a:lyrics"}]
    job.update(fields)
    assert decide(job, {}, _score(job))[1] == actions
    fields, _ = decide(job, {"take-a": {"status": "completed", "creditsCharged": 2}}, _score(job))
    job.update(fields)
    assert job["items"][1]["status"] == "done"
    fields, _ = decide(job, {}, _score(job))
    job.update(fields)
    fields, _ = decide(job, {"take-b": {"status": "failed", "error": "render_failed"}}, _score(job))
    job.update(fields)
    fields, actions = decide(job, {}, _score(job))
    assert fields["status"] == "partial" and fields["stage"] == "done"
    assert [action["kind"] for action in actions] == ["clear_active", "append_end_history"]


def test_queued_take_is_retried_after_its_start_lease():
    job = _job("singing")
    job["instrumentalsPrepared"] = True
    job["items"][0]["status"] = "done"
    job["items"][2]["status"] = "done"
    now = datetime.now(timezone.utc)
    state = {**_score(job), "now": now}
    fields, actions = decide(job, {}, state)
    job.update(fields)
    queued = {"take-a": {"status": "queued"}}
    assert decide(job, queued, state) == ({}, [])
    expired = {**state, "now": now + timedelta(seconds=61)}
    assert decide(job, queued, expired)[1] == actions


def test_instrumentals_settle_after_any_take_and_refund_after_none():
    job = _job("singing", instrumental=True)
    job["instrumentalsPrepared"] = True
    job["items"][0]["status"] = "done"
    job["items"][1]["status"] = "failed"
    job["items"][2]["status"] = "done"
    fields, actions = decide(job, {}, _score(job))
    assert actions == [{"kind": "settle_instrumental"}]
    assert fields["items"][-1]["status"] == "running"
    job["items"][2]["status"] = "failed"
    fields, actions = decide(job, {}, _score(job))
    assert actions == [{"kind": "release", "reservationId": "parent-instrumentals"}]
    assert fields["items"][-1]["status"] == "failed"


def test_cancelled_job_winds_down_once():
    job = _job("splitting", instrumental=True)
    job["cancelRequested"] = True
    fields, actions = decide(job, {}, _score(job))
    assert fields["status"] == "cancelled"
    assert len([action for action in actions if action["kind"] == "release"]) == 3
    terminal = deepcopy(job)
    terminal.update(fields)
    assert decide(terminal, {}, _score(job)) == ({}, [])


def test_stop_while_splitting_keeps_a_late_split_and_refunds_all_takes():
    job = _job("splitting")
    job["cancelRequested"] = True
    job["items"][0]["status"] = "running"
    fields, actions = decide(job, {"split-child": {"status": "running"}}, _score(job))
    assert "status" not in fields
    assert fields["items"][0]["status"] == "running"
    assert {action["reservationId"] for action in actions if action["kind"] == "release"} == {
        "take-a", "take-b"}
    job.update(fields)
    fields, actions = decide(job, {"split-child": {"status": "completed"}}, _score(job))
    assert fields["status"] == "cancelled"
    assert fields["items"][0]["status"] == "done"


def test_stop_after_one_take_keeps_its_charge_and_refunds_the_rest():
    job = _job("singing")
    job["cancelRequested"] = True
    job["items"][0]["status"] = "done"
    job["items"][1].update(status="done", creditsCharged=2)
    fields, actions = decide(job, {}, _score(job))
    assert (fields["status"], fields["endReason"]) == ("cancelled", "stopped")
    assert fields["items"][1]["creditsCharged"] == 2
    assert fields["items"][2]["status"] == "stopped"
    assert {action["reservationId"] for action in actions
            if action["kind"] == "release"} == {"take-b"}


def test_stop_waits_for_a_take_past_its_stop_point_then_completes():
    job = _job("singing")
    job["cancelRequested"] = True
    job["items"][0]["status"] = "done"
    job["items"][1].update(status="done", creditsCharged=2)
    job["items"][2]["status"] = "running"
    fields, actions = decide(job, {"take-b": {"status": "running"}}, _score(job))
    assert fields == {} and actions == []
    fields, actions = decide(job, {"take-b": {"status": "completed", "creditsCharged": 2}},
                             _score(job))
    assert fields["status"] == "completed" and fields["endReason"] is None
    assert [item["creditsCharged"] for item in fields["items"] if item["type"] == "take"] == [2, 2]
    assert not any(action["kind"] == "release" for action in actions)


def test_sweep_expires_each_split_after_its_own_30_minutes():
    job = _job("splitting")
    now = datetime.now(timezone.utc)
    job["items"][0].update(status="running", startedAt=now - timedelta(minutes=31))
    fields, actions = decide(job, {"split-child": {"status": "running"}},
                             {**_score(job), "now": now, "sweep": True})
    assert fields["status"] == "failed" and fields["endReason"] == "stuck"
    assert fields["items"][0]["reason"] == "stuck"
    assert {action["reservationId"] for action in actions if action["kind"] == "release"} == {
        "take-a", "take-b"}
    assert any(action["kind"] == "fail_stuck_child" for action in actions)
    job["items"][0]["startedAt"] = now - timedelta(minutes=29)
    assert decide(job, {"split-child": {"status": "running"}},
                  {**_score(job), "now": now, "sweep": True}) == ({}, [])


def test_sweep_expires_stalled_takes_21_minutes_after_singing_starts():
    job = _job("singing")
    now = datetime.now(timezone.utc)
    job["items"][0]["status"] = "done"
    job["items"][1]["status"] = "running"
    job["singingStartedAt"] = now - timedelta(minutes=21)
    fields, actions = decide(job, {"take-a": {"status": "running"}},
                             {**_score(job), "now": now, "sweep": True})
    assert fields["status"] == "failed" and fields["endReason"] == "stuck"
    assert {action["reservationId"] for action in actions if action["kind"] == "release"} == {
        "take-a", "take-b"}
    job["lastTakeAt"] = now - timedelta(minutes=19)
    assert decide(job, {"take-a": {"status": "running"}},
                  {**_score(job), "now": now, "sweep": True})[0].get("endReason") != "stuck"
