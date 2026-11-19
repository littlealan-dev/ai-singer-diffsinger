"""Package layout, progress, failure, and sound routing for Sing All."""

import asyncio
import copy
import zipfile
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

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
    assert result["stats"]["balance"]["mode"] == "lyrics"


def test_full_and_none_mixes_and_silent_instruments(monkeypatch, tmp_path):
    _, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path, mixes="full")
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["mixes"] == {"done": 1, "total": 1}
    with _zip(writes) as archive:
        assert not any("2 Part-dominant mixes" in name for name in archive.namelist())

    job, orchestrator, writes, _, _ = _fixture(monkeypatch, tmp_path, mixes="none", instrumental=False)
    asyncio.run(package.run_package("parent", orchestrator))
    assert writes[-1]["mixes"] == {"done": 0, "total": 0}
    assert writes[-1]["stats"]["balance"]["stats"]
    with _zip(writes) as archive:
        assert not any("mixes/" in name or "Instrumentals/" in name for name in archive.namelist())
    assert job["choices"]["mixes"] == "none"


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
    assert writes[-1]["error"] == "no_takes:ValueError"
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


def test_title_is_sanitized_and_bounded():
    assert package._safe_name("/\\:*?\"<>|\x00") == "-" * 10
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
    async def run(job_id, orchestrator):
        calls.append((job_id, orchestrator))
    marker = object()
    monkeypatch.setattr(main.app.state, "orchestrator", marker, raising=False)
    monkeypatch.setattr(package, "run_package", run)
    async def inline():
        await package.start_package("parent", SimpleNamespace(sing_all_package_transport="inline"))
        await asyncio.sleep(0)
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
