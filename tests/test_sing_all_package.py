"""Package layout, progress, failure, and sound routing for Sing All."""

import asyncio
import copy
import io
import zipfile
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from google.cloud import firestore

from src.backend import sing_all_package as package
from src.backend.config import Settings
from src.backend.sing_all_mix import TrackSound


def _fixture(monkeypatch, tmp_path, *, mixes="all", instrumental=True, second=True):
    session = tmp_path / "sessions" / "song"
    session.mkdir(parents=True, exist_ok=True)
    audio = np.full(44100, 0.08, dtype=np.float32)
    wavs = {}
    for name in ("first", "second"):
        path = session / f"{name}.wav"
        sf.write(path, audio, 44100)
        wavs[name] = path
    items = [{"type": "take", "status": "done", "rowId": "one", "mode": "lyrics",
              "childJobId": "first"}]
    if second:
        items.append({"type": "take", "status": "done", "rowId": "two", "mode": "lyrics",
                      "childJobId": "second"})
    job = {"jobId": "parent", "uid": "owner", "sessionId": "song",
           "title": "Song/Title", "choices": {"mode": "lyrics", "mixes": mixes,
                                           "repeats": False, "verse": 2},
           "rows": [{"rowId": "one", "partId": "P1", "name": "Soprano", "voice": "A"},
                    {"rowId": "two", "partId": "P2", "name": "Alto", "voice": "B"}],
           "items": items, "status": "completed", "stage": "done", "credits": {"total": 9}}
    summary = {"performance_midi": {"instrumental_parts": [
        {"eligible": True, "raw_part_id": "I1", "part_index": 0,
         "playback_preset": {"bank": 0, "program": 11}}
    ] if instrumental else []}}
    midi = session / "music.mid"
    midi.write_bytes(b"MThd")
    snapshot = {"score_summary": summary,
                "files": {"instrumental_midi_original_path": str(midi)}}
    children = {name: {"losslessOutputPath": str(path)} for name, path in wavs.items()}
    async def get_snapshot(*_):
        return snapshot
    async def restore_instrumental_midi(**_):
        return True
    reconciled = []
    async def reconcile(job_id):
        reconciled.append(job_id)
    orchestrator = SimpleNamespace(
        _settings=SimpleNamespace(backend_use_storage=False, project_root=tmp_path,
                                  storage_bucket="", sing_all_soundfont_path=tmp_path / "sf2"),
        _sessions=SimpleNamespace(get_snapshot=get_snapshot, session_dir=lambda _: session),
        _job_store=SimpleNamespace(get_job=lambda child_id: children.get(child_id)),
        restore_instrumental_midi=restore_instrumental_midi,
        _enqueue_sing_all_reconcile=reconcile,
    )
    writes = []
    monkeypatch.setattr(package.sing_all_store, "load", lambda _: job)
    monkeypatch.setattr(package, "_package_write", lambda _, value: writes.append(copy.deepcopy(value)))
    monkeypatch.setattr(package, "render_instruments",
                        lambda *_: np.full((44100, 2), 0.01, dtype=np.float32))
    monkeypatch.setattr(package, "_credit_file",
                        lambda voice, _: ("character.txt", f"Name: {voice}\n".encode()))
    return job, orchestrator, writes, reconciled, wavs


def _zip(writes):
    return zipfile.ZipFile(writes[-1]["zipPath"])


def test_all_mixes_zip_layout_and_progress(monkeypatch, tmp_path):
    job, orchestrator, writes, reconciled, wavs = _fixture(monkeypatch, tmp_path)
    asyncio.run(package.run_package("parent", orchestrator))
    result = writes[-1]
    assert (result["state"], result["mixes"]) == ("done", {"done": 3, "total": 3})
    assert [value["mixes"]["done"] for value in writes if value["state"] == "mixing"] == [0, 1, 2, 3]
    assert all(a["updatedAt"] <= b["updatedAt"] for a, b in zip(writes, writes[1:]))
    assert reconciled == ["parent"]
    assert (job["status"], job["stage"], job["credits"]) == ("completed", "done", {"total": 9})
    with _zip(writes) as archive:
        names = archive.namelist()
        root = "Song-Title - Sing All/"
        assert len(names) == 9
        assert archive.read(root + "1 Solo vocals/Soprano (lyrics).wav") == wavs["first"].read_bytes()
        assert archive.read(root + "1 Solo vocals/Alto (lyrics).wav") == wavs["second"].read_bytes()
        assert sum("2 Part-dominant mixes/" in name for name in names) == 2
        assert sum("3 Full mixes/" in name for name in names) == 1
        assert root + "4 Instrumentals/Song-Title.mid" in names
        assert root + "5 Voicebank credits/A/character.txt" in names
        assert root + "5 Voicebank credits/B/character.txt" in names
        assert b"Verse: 2" in archive.read(root + "README.txt")
        for name in names:
            if name.endswith((".wav", ".mp3")):
                assert archive.getinfo(name).compress_type == zipfile.ZIP_STORED
    assert len(list((tmp_path / "sessions/song/singAll/parent/mixes").glob("*.mp3"))) == 3
    assert len(result["files"]) == 9
    assert result["stats"]["rate"] == 44100
    assert len(result["stats"]["mixes"]) == 3
    assert [balance["mode"] for balance in result["stats"]["balances"]] == ["lyrics"]


def test_full_and_none_mixes_and_silent_instruments(monkeypatch, tmp_path):
    _, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path, mixes="full")
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["mixes"] == {"done": 1, "total": 1}
    with _zip(writes) as archive:
        assert not any("2 Part-dominant mixes" in name for name in archive.namelist())

    job, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path, mixes="none", instrumental=False)
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["mixes"] == {"done": 0, "total": 0}
    assert writes[-1]["stats"]["balances"][0]["stats"]
    with _zip(writes) as archive:
        assert not any("mixes/" in name or "Instrumentals/" in name for name in archive.namelist())
    assert job["choices"]["mixes"] == "none"


def test_each_mode_sung_gets_its_own_balance_without_mixes(monkeypatch, tmp_path):
    job, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path, mixes="none")
    job["choices"]["mode"] = "both"
    job["items"][1]["mode"] = "solfege"
    asyncio.run(package.run_package("parent", orchestrator))
    balances = writes[-1]["stats"]["balances"]
    assert [balance["mode"] for balance in balances] == ["lyrics", "solfege"]
    assert [len(balance["stats"]["vocals"]) for balance in balances] == [1, 1]
    assert "balance" not in writes[-1]["stats"]


def test_partial_job_includes_only_done_take(monkeypatch, tmp_path):
    job, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path)
    job["items"][1]["status"] = "failed"
    asyncio.run(package.run_package("parent", orchestrator))
    with _zip(writes) as archive:
        assert sum("1 Solo vocals/" in name for name in archive.namelist()) == 1
        assert not any("Alto" in name for name in archive.namelist())


def test_track_sounds_uses_first_row_and_mutes_assignable():
    parts = [
        {"eligible": True, "raw_part_id": "P", "part_index": 0,
         "playback_preset": {"bank": 0, "program": 41}},
        {"eligible": True, "raw_part_id": "P", "part_index": 0,
         "playback_preset": {"bank": 0, "program": 7}},
        {"eligible": True, "part_index": 2, "program_source": "user_assignable"},
        {"eligible": False, "part_index": 3},
        {"eligible": True, "part_index": 4, "percussion": True},
    ]
    assert package.track_sounds({"performance_midi": {"instrumental_parts": parts}}) == [
        TrackSound(0, 41), TrackSound(0, 41), None, TrackSound(128, 0)]


def test_missing_wav_render_and_upload_fail_without_public_paths(monkeypatch, tmp_path):
    for failure in ("missing", "render", "upload"):
        job, orchestrator, writes, reconciled, _ = _fixture(monkeypatch, tmp_path)
        if failure == "missing":
            orchestrator._job_store.get_job = lambda _: None
        elif failure == "render":
            def fail_render(*_):
                raise RuntimeError("/private/secret-path")
            monkeypatch.setattr(package, "render_instruments", fail_render)
        else:
            orchestrator._settings.backend_use_storage = True
            async def fake_take(*args):
                child = args[0]
                path = tmp_path / "sessions/song" / f"{child['jobId']}.wav"
                return path, path.read_bytes()
            monkeypatch.setattr(package, "_take_wav", fake_take)
            def fail_upload(*_):
                raise RuntimeError("/private/secret-path")
            monkeypatch.setattr(package, "upload_file", fail_upload)
        asyncio.run(package.run_package("parent", orchestrator))
        assert writes[-1]["state"] == "failed"
        assert "zipPath" not in writes[-1]
        assert "files" not in writes[-1]
        assert "/private/" not in writes[-1]["error"]
        assert reconciled == ["parent"]
        monkeypatch.undo()


def test_repeat_run_has_same_entries_and_file_metadata(monkeypatch, tmp_path):
    _, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path)
    asyncio.run(package.run_package("parent", orchestrator))
    first = copy.deepcopy(writes[-1])
    with _zip(writes) as archive:
        names = archive.namelist()
    asyncio.run(package.run_package("parent", orchestrator))
    with _zip(writes) as archive:
        assert archive.namelist() == names
    assert writes[-1]["files"] == first["files"]
    assert writes[-1]["stats"] == first["stats"]


def test_no_done_takes_records_failure_and_reconciles(monkeypatch, tmp_path):
    job, orchestrator, writes, reconciled, _ = _fixture(monkeypatch, tmp_path)
    job["items"] = []
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["error"] == "no_takes"
    assert reconciled == ["parent"]


def test_all_assignable_instruments_make_full_mix_only(monkeypatch, tmp_path):
    _, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path)
    async def snapshot(*_):
        return {"score_summary": {"performance_midi": {"instrumental_parts": [
            {"eligible": True, "part_index": 0, "program_source": "user_assignable"}]}},
                "files": {"instrumental_midi_original_path": str(tmp_path / "sessions/song/music.mid")}}
    orchestrator._sessions.get_snapshot = snapshot
    def should_not_render(*_):
        raise AssertionError("assignable track must be silent")
    monkeypatch.setattr(package, "render_instruments", should_not_render)
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["state"] == "done"
    assert writes[-1]["mixes"] == {"done": 1, "total": 1}


def test_a_cappella_score_packages_without_midi(monkeypatch, tmp_path):
    """Every part is listed but none is eligible, and the Song has no instrumental MIDI."""
    _, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path)
    async def snapshot(*_):
        return {"score_summary": {"performance_midi": {"instrumental_parts": [
            {"eligible": False, "part_id": "P1", "raw_part_id": "P1"},
            {"eligible": False, "part_id": "P2", "raw_part_id": "P2"}]}},
                "files": {"instrumental_midi_original_path": "",
                          "instrumental_midi_expanded_path": ""}}
    async def no_midi(**_):
        raise AssertionError("an a cappella score has no MIDI to restore")
    orchestrator._sessions.get_snapshot = snapshot
    orchestrator.restore_instrumental_midi = no_midi
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["state"] == "done"
    assert writes[-1]["mixes"]["total"] >= 1
    with _zip(writes) as archive:
        names = archive.namelist()
        assert not any("Instrumentals/" in name for name in names)
        assert any("3 Full mixes/" in name for name in names)


def test_title_is_sanitized_and_bounded():
    assert package._safe_name("/\\:*?\"<>|\x00") == "-" * 10
    assert package._safe_name("one\x85two") == "one-two"
    assert package._safe_name("  ") == "Song"
    assert len(package._safe_name("x" * 100)) == 80


def test_package_settings_validate_transport(monkeypatch, tmp_path):
    monkeypatch.delenv("SING_ALL_PACKAGE_TRANSPORT", raising=False)
    monkeypatch.delenv("SING_ALL_PACKAGE_JOB", raising=False)
    monkeypatch.delenv("SING_ALL_SOUNDFONT_PATH", raising=False)
    settings = Settings.from_env()
    assert settings.sing_all_package_transport == "inline"
    assert settings.sing_all_soundfont_path == settings.project_root / "ui/public/soundfonts/FluidR3_GM.sf2"
    monkeypatch.setenv("SING_ALL_PACKAGE_TRANSPORT", "invalid")
    with pytest.raises(ValueError, match="SING_ALL_PACKAGE_TRANSPORT"):
        Settings.from_env()
    monkeypatch.setenv("SING_ALL_PACKAGE_TRANSPORT", "job")
    with pytest.raises(ValueError, match="SING_ALL_PACKAGE_JOB"):
        Settings.from_env()
    monkeypatch.setenv("SING_ALL_PACKAGE_JOB", "projects/example/locations/test/jobs/package")
    monkeypatch.setenv("SING_ALL_SOUNDFONT_PATH", str(tmp_path / "custom.sf2"))
    settings = Settings.from_env()
    assert settings.sing_all_soundfont_path == tmp_path / "custom.sf2"


def test_start_package_inline_and_cloud_job(monkeypatch):
    from src.backend import main
    calls = []
    gate = None
    async def run(job_id, orchestrator):
        calls.append((job_id, orchestrator))
        await gate.wait()
    marker = object()
    monkeypatch.setattr(main.app.state, "orchestrator", marker, raising=False)
    monkeypatch.setattr(package, "run_package", run)
    async def inline():
        nonlocal gate
        gate = asyncio.Event()
        await package.start_package("parent", SimpleNamespace(sing_all_package_transport="inline"))
        assert len(package._package_tasks) == 1
        await asyncio.sleep(0)
        assert calls == [("parent", marker)]
        assert len(package._package_tasks) == 1
        gate.set()
        await asyncio.gather(*package._package_tasks)
        await asyncio.sleep(0)
        assert not package._package_tasks
    asyncio.run(inline())
    assert calls == [("parent", marker)]

    import google.auth
    import google.auth.transport.requests
    request = {}
    class Session:
        def __init__(self, credentials):
            assert credentials is marker
        def post(self, url, *, json, timeout):
            request.update(url=url, json=json, timeout=timeout)
            return SimpleNamespace(raise_for_status=lambda: None)
    monkeypatch.setattr(google.auth, "default", lambda **_: (marker, "project"))
    monkeypatch.setattr(google.auth.transport.requests, "AuthorizedSession", Session)
    settings = SimpleNamespace(sing_all_package_transport="job",
                               sing_all_package_job="projects/example/locations/test/jobs/package")
    asyncio.run(package.start_package("parent", settings))
    assert request["url"].endswith("/projects/example/locations/test/jobs/package:run")
    assert request["json"] == {"overrides": {"containerOverrides": [{"env": [
        {"name": "SING_ALL_JOB_ID", "value": "parent"}]}]}}


def test_storage_uploads_mixes_beside_zip_and_resamples(monkeypatch, tmp_path):
    job, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path)
    second = tmp_path / "sessions/song/second.wav"
    sf.write(second, np.full(48000, 0.08, dtype=np.float32), 48000)
    expanded = tmp_path / "sessions/song/expanded.mid"
    expanded.write_bytes(b"MThd-expanded")
    job["choices"]["repeats"] = True
    async def snapshot(*_):
        return {"score_summary": {"performance_midi": {"instrumental_parts": [
            {"eligible": True, "part_index": 0, "midi_program": 1}]}},
                "files": {"instrumental_midi_expanded_path": str(expanded)}}
    orchestrator._sessions.get_snapshot = snapshot
    orchestrator._settings.backend_use_storage = True
    kinds = []
    async def restore(**kwargs):
        kinds.append(kwargs["kind"])
        return True
    orchestrator.restore_instrumental_midi = restore
    async def fake_take(child, job, orchestrator, work_dir):
        path = tmp_path / "sessions/song" / f"{child['jobId']}.wav"
        return path, path.read_bytes()
    monkeypatch.setattr(package, "_take_wav", fake_take)
    uploaded = {}
    def upload(_, source, destination, _type):
        uploaded[destination] = source.read_bytes()
    monkeypatch.setattr(package, "upload_file", upload)
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["state"] == "done"
    assert writes[-1]["stats"]["rate"] == 44100
    assert kinds == ["expanded"]
    zip_path = "singAll/owner/parent/Song-Title - Sing All.zip"
    assert writes[-1]["zipPath"] == zip_path
    assert zip_path in uploaded
    assert sum(path.startswith("singAll/owner/parent/mixes/") for path in uploaded) == 3
    with zipfile.ZipFile(io.BytesIO(uploaded[zip_path])) as archive:
        assert archive.read("Song-Title - Sing All/4 Instrumentals/Song-Title.mid") == b"MThd-expanded"


def test_default_no_mixes_uses_wav_fallback_and_display_voice_name(monkeypatch, tmp_path):
    job, orchestrator, writes, _, wavs = _fixture(monkeypatch, tmp_path)
    job["choices"].pop("mixes")
    job["choices"]["mode"] = "solfege"
    job["items"][0]["mode"] = "solfege"
    job["items"][1]["status"] = "failed"
    job["rows"][0]["voiceName"] = "A/Display"
    orchestrator._job_store.get_job = lambda _: {
        "losslessOutputPath": str(wavs["first"].with_suffix(".mp3")),
        "outputPath": str(wavs["first"])}
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["state"] == "done"
    assert writes[-1]["mixes"] == {"done": 0, "total": 0}
    with _zip(writes) as archive:
        root = "Song-Title - Sing All/"
        assert archive.read(root + "1 Solo vocals/Soprano (solfège).wav") == wavs["first"].read_bytes()
        assert root + "5 Voicebank credits/A-Display/character.txt" in archive.namelist()
        readme = archive.read(root + "README.txt").decode()
        assert "Title: Song/Title" in readme
        assert "as written" in readme
        assert "A/Display / solfège" in readme


def test_package_write_replaces_only_package_map_in_firestore(monkeypatch, firestore_emulator):
    db = firestore.Client(project="demo-project")
    ref = db.collection("singAllJobs").document("synthetic-package-parent")
    ref.set({"status": "completed", "stage": "done", "credits": {"total": 3},
             "package": {"state": "done", "zipPath": "old.zip"}})
    monkeypatch.setattr(package, "get_firestore_client", lambda: db)
    package._package_write("synthetic-package-parent", {"state": "mixing",
                                                        "mixes": {"done": 0, "total": 1}})
    result = ref.get().to_dict()
    assert result == {"status": "completed", "stage": "done", "credits": {"total": 3},
                      "package": {"state": "mixing", "mixes": {"done": 0, "total": 1}}}


def test_voicebank_credit_prefers_own_text_then_yaml_and_falls_back(monkeypatch, tmp_path):
    root = tmp_path / "voicebanks"
    nested = root / "voice-a" / "nested"
    nested.mkdir(parents=True)
    (nested / "dsconfig.yaml").write_text("model: synthetic")
    (nested / "character.yaml").write_text("name: Synthetic")
    monkeypatch.setattr(package.voicebank_cache, "_local_voicebanks_root", lambda: root)
    pack = tmp_path / "pack" / "nested"
    pack.mkdir(parents=True)
    (pack / "dsconfig.yaml").write_text("model: synthetic")
    (pack / "character.txt").write_text("Name: Packed")
    calls = []
    def resolve(voice):
        calls.append(voice)
        if voice == "voice-b":
            return pack.parent
        raise FileNotFoundError("synthetic missing text pack")
    monkeypatch.setattr(package.voicebank_cache, "resolve_voicebank_text_path", resolve)
    assert package._credit_file("voice-a", "Display") == (
        "character.yaml", b"name: Synthetic")
    (nested / "character.txt").write_text("Name: Synthetic")
    assert package._credit_file("voice-a", "Display") == (
        "character.txt", b"Name: Synthetic")
    assert calls == []
    assert package._credit_file("voice-b", "Display") == (
        "character.txt", b"Name: Packed")
    assert package._credit_file("missing", "Display") == (
        "character.txt", b"Name: Display\n")
    assert calls == ["voice-b", "missing"]


def test_solo_and_part_mix_share_m1_safe_part_name(monkeypatch, tmp_path):
    job, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path, second=False)
    job["rows"][0]["name"] = "S/A"
    asyncio.run(package.run_package("parent", orchestrator))
    with _zip(writes) as archive:
        names = archive.namelist()
        assert "Song-Title - Sing All/1 Solo vocals/S_A (lyrics).wav" in names
        assert "Song-Title - Sing All/2 Part-dominant mixes/S_A (lyrics).mp3" in names
