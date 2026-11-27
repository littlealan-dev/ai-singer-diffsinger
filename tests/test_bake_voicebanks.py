import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bake_voicebanks.py"

sys.path.insert(0, str(ROOT / "scripts"))
import bake_voicebanks  # noqa: E402


def _manifest(tmp_path: Path, voicebanks: dict[str, bool]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "voicebanks": [
                    {"id": voicebank_id, "enabled": enabled, "storage_object": f"assets/voicebanks/{voicebank_id}.tar.gz"}
                    for voicebank_id, enabled in voicebanks.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _archive(source_dir: Path, voicebank_id: str, top_folder: str | None = None) -> None:
    """Write <id>.tar.gz holding a minimal voicebank, optionally under a top folder."""
    source_dir.mkdir(exist_ok=True)
    prefix = f"{top_folder}/" if top_folder else ""
    with tarfile.open(source_dir / f"{voicebank_id}.tar.gz", "w:gz") as tar:
        for name, text in (("dsconfig.yaml", "sample_rate: 44100\n"), ("character.yaml", f"name: {voicebank_id}\n")):
            data = text.encode()
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_bakes_each_enabled_voicebank_into_its_own_folder(tmp_path):
    archives = tmp_path / "archives"
    _archive(archives, "Flat")
    _archive(archives, "Nested", top_folder="Nested Bank v1")
    manifest = _manifest(tmp_path, {"Flat": True, "Nested": True})

    result = _run("--manifest", str(manifest), "--source-dir", str(archives), "--out", str(tmp_path / "out"))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "Flat" / "dsconfig.yaml").is_file()
    assert (tmp_path / "out" / "Nested" / "Nested Bank v1" / "dsconfig.yaml").is_file()
    assert "baked 2 voicebanks" in result.stdout


def test_skips_disabled_voicebanks(tmp_path):
    archives = tmp_path / "archives"
    _archive(archives, "Flat")
    manifest = _manifest(tmp_path, {"Flat": True, "Disabled": False})

    result = _run("--manifest", str(manifest), "--source-dir", str(archives), "--out", str(tmp_path / "out"))

    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["Flat"]


def test_fails_when_an_enabled_archive_is_missing(tmp_path):
    archives = tmp_path / "archives"
    _archive(archives, "Flat")
    manifest = _manifest(tmp_path, {"Flat": True, "Missing": True})

    result = _run("--manifest", str(manifest), "--source-dir", str(archives), "--out", str(tmp_path / "out"))

    assert result.returncode == 1
    assert "Missing.tar.gz" in result.stderr


def test_refuses_an_archive_that_writes_outside_its_folder(tmp_path):
    archives = tmp_path / "archives"
    archives.mkdir()
    with tarfile.open(archives / "Evil.tar.gz", "w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    manifest = _manifest(tmp_path, {"Evil": True})

    result = _run("--manifest", str(manifest), "--source-dir", str(archives), "--out", str(tmp_path / "out"))

    assert result.returncode == 1
    assert "unsafe paths" in result.stderr
    assert not (tmp_path / "out" / "escaped.txt").exists()


def test_downloads_from_the_bucket_with_gcloud(tmp_path, monkeypatch):
    archives = tmp_path / "archives"
    _archive(archives, "Flat")
    manifest = _manifest(tmp_path, {"Flat": True})
    calls = []

    def fake_run(command, check):
        calls.append(command)
        Path(command[-1]).write_bytes((archives / "Flat.tar.gz").read_bytes())

    monkeypatch.setattr(bake_voicebanks.subprocess, "run", fake_run)

    assert bake_voicebanks.main(["--manifest", str(manifest), "--bucket", "a-bucket", "--out", str(tmp_path / "out")]) == 0
    assert calls[0][:4] == ["gcloud", "storage", "cp", "gs://a-bucket/assets/voicebanks/Flat.tar.gz"]
    assert (tmp_path / "out" / "Flat" / "dsconfig.yaml").is_file()


@pytest.mark.parametrize("bad_id", ["../Escape", "a/b"])
def test_refuses_a_voicebank_id_that_is_a_path(tmp_path, bad_id):
    manifest = _manifest(tmp_path, {bad_id: True})

    result = _run("--manifest", str(manifest), "--source-dir", str(tmp_path), "--out", str(tmp_path / "out"))

    assert result.returncode == 1
    assert "Bad voicebank id" in result.stderr
