"""Sing All parent creation uses the existing all-or-nothing credit transaction."""

from types import SimpleNamespace

from src.backend import sing_all_store
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

    def collection(self, name):
        return _Document(self.records, name)


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
    assert transaction.writes == [
        ("set", "singAllJobs/fake-parent", job),
        ("update", "users/fake-user", {"activeSingAllJobId": "fake-parent"}),
    ]


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
