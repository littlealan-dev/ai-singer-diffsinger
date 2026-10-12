from __future__ import annotations

from pathlib import Path

import pytest

import src.backend.session as session_module
from src.backend.session import SessionMusicXmlUnavailableError, _ensure_active_musicxml_path

SESSION_ID = "a13842074d874190b5807cc3b5f5c144"
SCORE_ROOT = f"sessions/user-1/{SESSION_ID}/scores/score-1"
V1 = f"{SCORE_ROOT}/versions/1/input.xml"
V2 = f"{SCORE_ROOT}/versions/2/input.xml"


@pytest.fixture
def storage(monkeypatch):
    blobs = {V1: b"<v1/>", V2: b"<v2/>"}
    downloads: list[str] = []

    def download_bytes(bucket, path):
        downloads.append(path)
        return blobs[path]

    monkeypatch.setattr(session_module, "download_bytes", download_bytes)
    return downloads


def _restore(project_root: Path, files: dict) -> Path:
    return _ensure_active_musicxml_path(
        project_root=project_root,
        session_dir=project_root / "data" / "sessions" / SESSION_ID,
        files=files,
        backend_use_storage=True,
        storage_bucket="bucket",
    )


def test_a_stale_copy_at_the_recorded_path_is_not_taken_for_the_active_version(
    tmp_path, storage
):
    # The worker restored v1 for a first split; the API then recorded its own
    # restored copy's path, which on the worker still holds v1.
    stale = tmp_path / "data" / "sessions" / SESSION_ID / "active-rehydrated.xml"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"<v1/>")
    files = {
        "musicxml_path": f"data/sessions/{SESSION_ID}/active-rehydrated.xml",
        "active_musicxml_storage_path": V2,
    }

    path = _restore(tmp_path, files)

    assert path.read_bytes() == b"<v2/>"
    assert path == (tmp_path / "data/sessions" / SESSION_ID / "scores/score-1/versions/2/input.xml").resolve()
    assert files["musicxml_path"] == f"data/sessions/{SESSION_ID}/scores/score-1/versions/2/input.xml"
    assert storage == [V2]


def test_a_version_already_on_this_disk_is_used_without_downloading(tmp_path, storage):
    # Where _stage_score wrote it when this instance saved the version.
    saved = tmp_path / "data/sessions" / SESSION_ID / "scores/score-1/versions/2/input.xml"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"<v2/>")

    path = _restore(tmp_path, {"active_musicxml_storage_path": V2})
    again = _restore(tmp_path, {"active_musicxml_storage_path": V2})

    assert path == again == saved.resolve()
    assert storage == []


def test_an_object_outside_the_session_layout_gets_a_name_from_its_path(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "download_bytes", lambda bucket, path: b"<legacy/>")

    path = _restore(tmp_path, {"active_musicxml_storage_path": "legacy/uploads/score.xml"})

    assert path.read_bytes() == b"<legacy/>"
    assert path.parent == (tmp_path / "data/sessions" / SESSION_ID / "stored").resolve()
    assert path.suffix == ".xml"


def test_without_storage_the_recorded_local_file_is_used(tmp_path):
    local = tmp_path / "data/sessions" / SESSION_ID / "scores/score-1/upload/score.xml"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"<local/>")
    files = {"musicxml_path": str(local.relative_to(tmp_path))}

    path = _ensure_active_musicxml_path(
        project_root=tmp_path,
        session_dir=tmp_path / "data/sessions" / SESSION_ID,
        files=files,
        backend_use_storage=False,
        storage_bucket="",
    )

    assert path == local.resolve()
    with pytest.raises(SessionMusicXmlUnavailableError):
        _ensure_active_musicxml_path(
            project_root=tmp_path,
            session_dir=tmp_path / "data/sessions" / SESSION_ID,
            files={"musicxml_path": "data/sessions/missing.xml"},
            backend_use_storage=False,
            storage_bucket="",
        )
