import asyncio
from datetime import datetime, timezone

import pytest

import src.backend.session as session_module


class _FakeArrayUnion:
    def __init__(self, values):
        self.values = values


class _FakeServerTimestamp:
    pass


class _FakeDocSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._doc_id = doc_id

    def set(self, payload, merge=False):
        if not merge or self._doc_id not in self._store:
            self._store[self._doc_id] = dict(payload)
        else:
            self._store[self._doc_id].update(payload)

    def get(self):
        return _FakeDocSnapshot(self._store.get(self._doc_id))

    def update(self, fields):
        doc = self._store.get(self._doc_id)
        if doc is None:
            raise KeyError(self._doc_id)
        for key, value in fields.items():
            if isinstance(value, _FakeArrayUnion):
                doc.setdefault(key, [])
                for entry in value.values:
                    if entry not in doc[key]:
                        doc[key].append(entry)
                continue
            if isinstance(value, _FakeServerTimestamp):
                doc[key] = "server-ts"
                continue
            if "." in key:
                root, child = key.split(".", 1)
                doc.setdefault(root, {})
                doc[root][child] = value
            else:
                doc[key] = value


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)


class _FakeClient:
    def __init__(self, store):
        self._store = store

    def collection(self, _name):
        return _FakeCollection(self._store)


def test_firestore_session_store_roundtrip(monkeypatch, tmp_path):
    store = {}
    monkeypatch.setattr(session_module, "get_firestore_client", lambda: _FakeClient(store))
    monkeypatch.setattr(session_module.firestore, "ArrayUnion", _FakeArrayUnion)
    monkeypatch.setattr(session_module.firestore, "SERVER_TIMESTAMP", _FakeServerTimestamp())

    sessions = session_module.FirestoreSessionStore(
        project_root=tmp_path,
        sessions_dir=tmp_path / "sessions",
        ttl_seconds=3600,
        max_sessions=100,
    )

    session = asyncio.run(sessions.create_session(user_id="user-1"))
    asyncio.run(sessions.append_history(session.id, "user", "hi"))
    asyncio.run(sessions.set_metadata(session.id, "musicxml_name", "score.xml"))
    version = asyncio.run(sessions.set_score(session.id, {"title": "Test"}))
    assert version == 1
    snapshot = asyncio.run(sessions.get_snapshot(session.id, user_id="user-1"))

    assert snapshot["id"] == session.id
    assert snapshot["files"]["musicxml_name"] == "score.xml"
    assert snapshot["current_score"]["score"]["title"] == "Test"
    assert snapshot["score_context_updated"] is False

    asyncio.run(sessions.mark_score_context_updated(session.id))
    marked_snapshot = asyncio.run(sessions.get_snapshot(session.id, user_id="user-1"))
    assert marked_snapshot["score_context_updated"] is True

    asyncio.run(sessions.acknowledge_score_context_updated(session.id))
    acknowledged_snapshot = asyncio.run(sessions.get_snapshot(session.id, user_id="user-1"))
    assert acknowledged_snapshot["score_context_updated"] is False


@pytest.mark.parametrize("use_firestore", [False, True])
def test_history_preserves_repeated_turns_with_ids_and_utc_timestamps(
    monkeypatch, tmp_path, use_firestore
):
    store = {}
    monkeypatch.setattr(session_module, "get_firestore_client", lambda: _FakeClient(store))
    monkeypatch.setattr(session_module.firestore, "ArrayUnion", _FakeArrayUnion)
    monkeypatch.setattr(session_module.firestore, "SERVER_TIMESTAMP", _FakeServerTimestamp())
    now = datetime(2026, 9, 9, 6, 0, 0, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(session_module, "_utcnow", lambda: now)
    store_type = (
        session_module.FirestoreSessionStore if use_firestore else session_module.SessionStore
    )
    sessions = store_type(
        project_root=tmp_path,
        sessions_dir=tmp_path / "sessions",
        ttl_seconds=3600,
        max_sessions=100,
    )
    turns = [
        ("assistant", "Confirm this take?"),
        ("user", "Yes"),
        ("assistant", "Confirm this take?"),
        ("user", "Yes"),
    ]

    async def run():
        session = await sessions.create_session(user_id="user-1")
        for role, content in turns:
            await sessions.append_history(session.id, role, content)
        return await sessions.get_snapshot(session.id, "user-1")

    history = asyncio.run(run())["history"]
    assert [(entry["role"], entry["content"]) for entry in history] == turns
    assert len({entry["id"] for entry in history}) == len(turns)
    assert all(datetime.fromisoformat(entry["timestamp"]) == now for entry in history)
    assert all(entry["timestamp"].endswith("+00:00") for entry in history)


def test_firestore_history_keeps_legacy_entries_when_same_text_is_submitted(
    monkeypatch, tmp_path
):
    store = {}
    monkeypatch.setattr(session_module, "get_firestore_client", lambda: _FakeClient(store))
    monkeypatch.setattr(session_module.firestore, "ArrayUnion", _FakeArrayUnion)
    monkeypatch.setattr(session_module.firestore, "SERVER_TIMESTAMP", _FakeServerTimestamp())
    sessions = session_module.FirestoreSessionStore(
        project_root=tmp_path,
        sessions_dir=tmp_path / "sessions",
        ttl_seconds=3600,
        max_sessions=100,
    )
    legacy_entry = {"role": "user", "content": "Yes"}

    async def run():
        session = await sessions.create_session(user_id="user-1")
        store[session.id]["history"] = [legacy_entry.copy()]
        await sessions.append_history(session.id, "user", "Yes")
        return await sessions.get_snapshot(session.id, "user-1")

    history = asyncio.run(run())["history"]
    assert len(history) == 2
    assert history[0] == legacy_entry
    assert history[1]["content"] == "Yes"
    assert history[1]["id"]
    assert datetime.fromisoformat(history[1]["timestamp"]).tzinfo is not None
