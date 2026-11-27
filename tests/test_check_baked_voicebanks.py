import json
from pathlib import Path

from src.api.check_baked_voicebanks import main


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


def _bake(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "dsconfig.yaml").write_text("sample_rate: 44100\n", encoding="utf-8")
    (root / "character.yaml").write_text("name: Bank\n", encoding="utf-8")


def test_passes_when_every_enabled_voicebank_is_baked(tmp_path, capsys):
    manifest = _manifest(tmp_path, {"Flat": True, "Nested": True})
    _bake(tmp_path / "baked" / "Flat")
    _bake(tmp_path / "baked" / "Nested" / "TopFolder")

    assert main([str(manifest), str(tmp_path / "baked")]) == 0
    assert "baked voicebanks ok: Flat, Nested" in capsys.readouterr().out


def test_fails_naming_a_missing_enabled_voicebank(tmp_path, capsys):
    manifest = _manifest(tmp_path, {"Flat": True, "Missing": True})
    _bake(tmp_path / "baked" / "Flat")

    assert main([str(manifest), str(tmp_path / "baked")]) == 1
    err = capsys.readouterr().err
    assert "voicebank not baked: Missing" in err
    assert "Flat" not in err


def test_a_disabled_voicebank_need_not_be_baked(tmp_path):
    manifest = _manifest(tmp_path, {"Flat": True, "Disabled": False})
    _bake(tmp_path / "baked" / "Flat")

    assert main([str(manifest), str(tmp_path / "baked")]) == 0


def test_a_folder_without_dsconfig_counts_as_missing(tmp_path, capsys):
    manifest = _manifest(tmp_path, {"Broken": True})
    (tmp_path / "baked" / "Broken").mkdir(parents=True)
    (tmp_path / "baked" / "Broken" / "character.yaml").write_text("name: Broken\n", encoding="utf-8")

    assert main([str(manifest), str(tmp_path / "baked")]) == 1
    assert "voicebank not baked: Broken" in capsys.readouterr().err


def test_the_prod_manifest_check_runs_against_a_full_bake(tmp_path):
    # The Dockerfile runs the check on the real prod manifest: every enabled
    # voicebank there, baked, passes.
    manifest = Path(__file__).resolve().parents[1] / "env" / "voicebank_manifest.prod.json"
    entries = json.loads(manifest.read_text(encoding="utf-8"))["voicebanks"]
    for entry in entries:
        if entry.get("enabled") is True:
            _bake(tmp_path / "baked" / entry["id"])

    assert main([str(manifest), str(tmp_path / "baked")]) == 0
