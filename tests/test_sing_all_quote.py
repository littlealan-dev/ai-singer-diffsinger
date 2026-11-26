"""The Sing All quote route checks ownership and blocks before spending work."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.backend import main
from src.backend.sing_all_judgment import QuoteUnavailable, StyleUnavailable


class _Snapshot:
    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return self.data


class _Db:
    def __init__(self, user):
        self.user = user

    def collection(self, name):
        assert name == "users"
        return self

    def document(self, name):
        assert name == "owner"
        return self

    def get(self):
        return _Snapshot(self.user)


def _route(monkeypatch, *, user, rows, style_fails=False):
    snapshot = {
        "song": {"id": "song-a"}, "score_id": "score-a",
        "current_score": {"score": {"parts": []}, "version": 1},
        "score_summary": {"parts": [], "duration_seconds": 59},
    }
    async def auth(request):
        return "owner"
    async def owned(sessions, song_id, user_id):
        assert (song_id, user_id) == ("song-a", "owner")
        return snapshot
    class _Orchestrator:
        async def _get_voicebank_details(self):
            return [{"id": "voice_a", "name": "Voice A", "languages": ["en"],
                     "voice_colors": [], "synthesis_control_defaults": {}}]
        async def _call_sing_all_quote(self, *args, **kwargs):
            raise AssertionError("stubbed judgment must avoid model")
    monkeypatch.setattr(main, "_get_user_id_or_401", auth)
    monkeypatch.setattr(main, "_get_snapshot_or_404", owned)
    monkeypatch.setattr(main, "get_firestore_client", lambda: _Db(user))
    monkeypatch.setattr(main, "quote_facts", lambda *args, **kwargs: {
        "rows": rows, "open_staffs": [], "notes": []})
    monkeypatch.setattr(main, "read_judgment", lambda *args: None)
    monkeypatch.setattr(main, "latest_judgment", lambda *args: None)
    monkeypatch.setattr(main, "save_judgment", lambda *args: None)
    monkeypatch.setattr(main, "read_style", lambda *args: None)
    monkeypatch.setattr(main, "save_style", lambda *args: None)
    monkeypatch.setattr(main, "get_instrumental_charge_state", lambda *args: "paid")
    monkeypatch.setattr(main, "save_quote", lambda *args: {"quoteId": "quote-a",
                                                              "choicesHash": "hash-a"})
    async def judgment(*args, **kwargs):
        return {"inputHash": "hash-a", "voices": {row["rowId"]: "voice_a" for row in rows},
                "roles": {}, "sounds": {}, "layouts": {}}
    monkeypatch.setattr(main, "quote_judgment", judgment)
    async def style(*args, **kwargs):
        if style_fails:
            raise StyleUnavailable("bad")
        return {"inputHash": "style-a", "controls": {}}
    monkeypatch.setattr(main, "style_judgment", style)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        sessions=object(), orchestrator=_Orchestrator(), settings=main.app.state.settings)))
    endpoint = next(route.endpoint for route in main.app.routes
                    if getattr(route, "path", None) == "/sing-all/quote")
    return endpoint, request, snapshot


def _row():
    return {"rowId": "a", "sourcePartId": "P1", "partId": "P1",
            "name": "Made-up lead", "split": False, "hasWords": True,
            "note": None, "voice": None, "voicePicked": "recommended"}


def test_already_running_blocks_before_quote_or_model(monkeypatch):
    endpoint, request, _ = _route(
        monkeypatch, user={"activeSingAllJobId": "job-a"}, rows=[_row()])
    monkeypatch.setattr(main, "load_sing_all_job", lambda job_id: {
        "jobId": job_id, "uid": "owner", "sessionId": "another-song", "status": "running"})
    monkeypatch.setattr(main, "quote_facts", lambda *args, **kwargs: pytest.fail("facts read"))
    result = asyncio.run(endpoint(main.SingAllQuoteRequest(songId="song-a"), request))
    assert result == {"blocked": "already_running", "activeJobId": "job-a",
                      "activeSongId": "another-song"}


def test_unsupported_language_blocks_without_model(monkeypatch):
    endpoint, request, _ = _route(monkeypatch, user={}, rows=[_row()])
    result = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices={"language": "ja"}), request))
    assert result == {"blocked": "no_voice_for_language", "language": "ja",
                      "supported": ["en"]}


@pytest.mark.parametrize("user,rows,choices,blocked", [
    ({"billing": {"activePlanKey": "free"}, "credits": {"balance": 10}},
     [_row()], {}, "free_plan"),
    ({"credits": {"balance": 10}}, [], {}, "nothing_to_sing"),
    ({"credits": {"balance": 1}}, [_row()], {}, "not_enough_credits"),
    ({"credits": {"balance": 10}}, [_row()], {"styleNote": "warmer lead"},
     "style_unavailable"),
])
def test_quote_blocked_values(monkeypatch, user, rows, choices, blocked):
    endpoint, request, _ = _route(
        monkeypatch, user=user, rows=rows,
        style_fails=blocked == "style_unavailable")
    result = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices=choices), request))
    assert result["blocked"] == blocked
    assert result["quoteId"] == "quote-a"


def test_solfege_only_forces_english_mode(monkeypatch):
    endpoint, request, _ = _route(
        monkeypatch, user={"credits": {"balance": 10}}, rows=[_row()])
    result = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices={"language": "ja", "solfegeOnly": True,
                                  "mode": "lyrics"}), request))
    assert result["options"]["mode"] == "solfege"
    assert result["options"]["languages"] == [{"code": "en", "label": None}]
    assert result["lines"][0]["credits"] == 2


@pytest.mark.parametrize("choices,repeats", [({}, False), ({"repeats": True}, True), ({"repeats": False}, False)])
def test_sing_all_sings_in_written_order_unless_repeats_are_turned_on(monkeypatch, choices, repeats):
    endpoint, request, _ = _route(monkeypatch, user={"credits": {"balance": 10}}, rows=[_row()])
    result = asyncio.run(endpoint(main.SingAllQuoteRequest(songId="song-a", choices=choices), request))
    assert (result["options"]["repeats"], result["options"]["repeatsAvailable"]) == (repeats, True)


def test_quote_contract_labels_repeats_and_structured_notes(monkeypatch):
    endpoint, request, snapshot = _route(
        monkeypatch, user={"credits": {"balance": 10}}, rows=[_row()])
    snapshot["score_summary"].update(
        repeat_expansion_available=False, available_verses=["3", "1"],
        parts=[{"part_id": "P1", "part_name": "Lead"},
               {"part_id": "P2", "part_name": "Piano"}],
    )
    async def voices():
        return [
            {"id": "voice_a", "name": "Voice A", "languages": ["en", "zh"],
             "language_details": {"zh": {"label": "Mandarin Chinese"}}},
            {"id": "voice_b", "name": "Voice B", "languages": ["ja"]},
        ]
    request.app.state.orchestrator._get_voicebank_details = voices
    monkeypatch.setattr(main, "quote_facts", lambda *args, **kwargs: {
        "rows": [_row()], "open_staffs": [],
        "notes": ["P1:cannot_split_without_lyrics"],
        "instruments": [{"partId": "P2", "route": {"score_instrument_id": "instrument-a"}}],
    })
    async def judgment(*args, **kwargs):
        return {"inputHash": "hash-a", "voices": {"a": "voice_a"},
                "roles": {}, "layouts": {}, "sounds": {"instrument-a": None}}
    monkeypatch.setattr(main, "quote_judgment", judgment)
    monkeypatch.setattr(main, "summary_with_judged_instruments",
                        lambda summary, *args, **kwargs: summary)
    saved = []
    monkeypatch.setattr(main, "save_quote", lambda db, record:
                        (saved.append(record) or {"quoteId": "quote-a",
                                                  "choicesHash": "hash-a"}))
    result = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices={"repeats": True}), request))
    assert result["voices"] == [{"id": "voice_a", "name": "Voice A"}]
    assert result["options"] == {
        "language": "en", "languages": [{"code": "en", "label": None},
                                        {"code": "ja", "label": None},
                                        {"code": "zh", "label": "Mandarin Chinese"}],
        "solfegeOnly": False, "mode": "lyrics", "verse": "1", "verses": ["1", "3"],
        "repeats": False, "repeatsAvailable": False,
        "mixes": "full", "mixFiles": {"all": 1, "full": 1},
    }
    assert result["notes"] == [
        {"code": "cannot_split_without_lyrics", "partId": "P1", "partName": "Lead"},
        {"code": "no_sound_chosen", "partId": "P2", "partName": "Piano"},
        {"code": "repeats_written_order", "partId": None, "partName": None},
    ]
    assert result["style"] == {"note": "", "controls": {}, "changes": {}}
    assert saved[0]["notes"] == result["notes"]
    assert saved[0]["choices"]["repeats"] is False


def test_failed_quote_call_returns_retryable_503(monkeypatch):
    endpoint, request, _ = _route(
        monkeypatch, user={"credits": {"balance": 10}}, rows=[_row()])
    async def failed(*args, **kwargs):
        raise QuoteUnavailable("quote_call_failed")
    monkeypatch.setattr(main, "quote_judgment", failed)
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(endpoint(main.SingAllQuoteRequest(songId="song-a"), request))
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "quote_unavailable"}


def test_rejected_instrument_assignments_return_retryable_503(monkeypatch):
    endpoint, request, _ = _route(
        monkeypatch, user={"credits": {"balance": 10}}, rows=[_row()])
    def rejected(*args, **kwargs):
        raise ValueError("invalid route")
    monkeypatch.setattr(main, "summary_with_judged_instruments", rejected)
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(endpoint(main.SingAllQuoteRequest(songId="song-a"), request))
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "quote_unavailable"}


def test_quote_one_model_call_then_cache_across_customise_and_score_change(monkeypatch):
    endpoint, request, snapshot = _route(
        monkeypatch, user={"credits": {"balance": 10}}, rows=[_row()])
    orchestrator = request.app.state.orchestrator
    async def voices():
        return [{"id": name, "name": name, "languages": ["en"]}
                for name in ("voice_a", "voice_b")]
    orchestrator._get_voicebank_details = voices
    calls = []
    async def model(question, *, response_schema, style=False):
        calls.append(question)
        assert style is False
        assert question["language"] == "en"
        return json.dumps({"voices": {"r1": "voice_a"}})
    orchestrator._call_sing_all_quote = model
    answer = {}
    monkeypatch.setattr(main, "quote_judgment", __import__(
        "src.backend.sing_all_judgment", fromlist=["quote_judgment"]).quote_judgment)
    monkeypatch.setattr(main, "read_judgment", lambda db, session, key: answer.get(key))
    monkeypatch.setattr(main, "latest_judgment", lambda db, session: next(iter(answer.values()), None))
    monkeypatch.setattr(main, "save_judgment", lambda db, session, value:
                        answer.__setitem__(value["inputHash"], value))
    first = asyncio.run(endpoint(main.SingAllQuoteRequest(songId="song-a"), request))
    assert first["rows"][0]["voice"] == "voice_a"
    assert len(calls) == 1
    customise = {"rows": [{"rowId": "a", "include": True,
                           "voice": "voice_b", "voicePicked": "user"}], "mode": "both"}
    second = asyncio.run(endpoint(main.SingAllQuoteRequest(
        songId="song-a", choices=customise), request))
    assert second["rows"][0]["voice"] == "voice_b"
    assert len(calls) == 1
    snapshot["current_score"]["version"] = 2
    asyncio.run(endpoint(main.SingAllQuoteRequest(songId="song-a"), request))
    assert len(calls) == 2
