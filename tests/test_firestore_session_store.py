import asyncio

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
                doc[key].extend(value.values)
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
