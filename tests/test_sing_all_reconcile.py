"""The Sing All state machine decides without Firestore or worker calls."""

from copy import deepcopy

from src.backend.sing_all import decide


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
