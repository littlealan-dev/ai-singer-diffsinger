"""Sing All quotes and cached judgments stay scoped to their Song and owner."""

from datetime import datetime, timedelta, timezone

from src.backend.sing_all_store import (
    latest_judgment, read_judgment, read_quote, read_style, save_judgment,
    save_quote, save_style,
)


class _Doc:
    def __init__(self, records, path):
        self.records, self.path = records, path

    def collection(self, name):
        return _Collection(self.records, self.path + "/" + name)

    def document(self, name):
        return _Doc(self.records, self.path + "/" + name)

    def get(self):
        return self

    def to_dict(self):
        return self.records.get(self.path)

    def set(self, value):
        self.records[self.path] = value


class _Collection(_Doc):
    pass


class _Db:
    def __init__(self):
        self.records = {}

    def collection(self, name):
        return _Collection(self.records, name)


def test_last_five_judgments_and_one_style_per_song():
    db = _Db()
    for number in range(6):
        save_judgment(db, "song-a", {"inputHash": f"h{number}", "voices": {}})
    assert read_judgment(db, "song-a", "h0") is None
    assert read_judgment(db, "song-a", "h1")["inputHash"] == "h1"
    assert latest_judgment(db, "song-a")["inputHash"] == "h5"
    assert latest_judgment(db, "song-b") is None
    save_judgment(db, "song-a", {"inputHash": "h3", "voices": {"row": "new"}})
    assert latest_judgment(db, "song-a")["voices"] == {"row": "new"}
    assert len(db.records["sessions/song-a/singAll/judgment"]["entries"]) == 5
    save_style(db, "song-a", {"inputHash": "style-1"})
    assert read_style(db, "song-a") == {"inputHash": "style-1"}
    assert read_style(db, "song-b") is None


def test_quote_expires_and_owner_is_checked():
    db = _Db()
    now = datetime.now(timezone.utc)
    quote = save_quote(db, {"uid": "owner", "sessionId": "song-a", "total": 3}, now=now)
    assert read_quote(db, quote["quoteId"], uid="owner") == quote
    assert read_quote(db, quote["quoteId"], uid="other") is None
    assert read_quote(db, "absent", uid="owner") is None
    db.records[f"sing_all_quotes/{quote['quoteId']}"]["expiresAt"] = now - timedelta(seconds=1)
    assert read_quote(db, quote["quoteId"], uid="owner") is None
