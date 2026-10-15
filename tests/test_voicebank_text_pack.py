import io
import importlib.util
import tarfile
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound

import src.api.voicebank_cache as voicebank_cache

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_voicebank_text_packs", ROOT / "scripts" / "build_voicebank_text_packs.py"
)
packer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(packer)

VOICEBANK_ID = "Test Voice"
ARCHIVE_OBJECT = "assets/voicebanks/Test_Voice.tar.gz"
FILES = {
    "Test Voice/dsconfig.yaml": b"phonemes: phonemes.json\n",
    "Test Voice/character.yaml": b"name: Test\n",
    "Test Voice/phonemes.json": b'{"a": 1}',
    "Test Voice/dsdur/dsdict-en.yaml": b"entries: []\n",
    "Test Voice/dsmain/acoustic.onnx": b"\0" * 4096,
    "Test Voice/sample.wav": b"RIFF" + b"\0" * 1024,
}


def _tar_gz(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_a_text_pack_keeps_only_the_text_files_at_their_paths(tmp_path):
    pack_path = tmp_path / "pack.tar.gz"

    files, size = packer.build_text_pack(io.BytesIO(_tar_gz(FILES)), pack_path)
    packer.check_text_pack(pack_path)

    with tarfile.open(pack_path, "r:gz") as pack:
        names = sorted(pack.getnames())
    assert names == [
        "Test Voice/character.yaml",
        "Test Voice/dsconfig.yaml",
        "Test Voice/dsdur/dsdict-en.yaml",
        "Test Voice/phonemes.json",
    ]
    assert files == 4
    assert size == sum(len(FILES[name]) for name in names)


def test_a_text_pack_without_a_voicebank_root_is_refused(tmp_path):
    pack_path = tmp_path / "pack.tar.gz"
    packer.build_text_pack(
        io.BytesIO(_tar_gz({"Test Voice/notes.txt": b"x"})), pack_path
    )

    with pytest.raises(ValueError, match="no voicebank root"):
        packer.check_text_pack(pack_path)


class _FakeBlob:
    def __init__(self, store, name, downloads):
        self._store = store
        self._name = name
        self._downloads = downloads

    def download_to_filename(self, path):
        if self._name not in self._store:
            raise NotFound(self._name)
        self._downloads.append(self._name)
        Path(path).write_bytes(self._store[self._name])


class _FakeBucket:
    def __init__(self, store):
        self.store = store
        self.downloads = []

    def blob(self, name):
        return _FakeBlob(self.store, name, self.downloads)


def _text_pack_bytes():
    return _tar_gz({k: v for k, v in FILES.items() if voicebank_cache.is_voicebank_text_file(k)})


@pytest.fixture
def voicebanks(monkeypatch, tmp_path):
    """A voicebank cache whose full-voicebank loaders record calls instead of loading."""
    full_loads = []
    monkeypatch.setattr(voicebank_cache, "_check_voicebank_id", lambda voicebank_id: None)
    monkeypatch.setattr(
        voicebank_cache,
        "get_manifest_voicebank_metadata",
        lambda voicebank_id: {"storage_object": ARCHIVE_OBJECT},
    )
    monkeypatch.setattr(
        voicebank_cache,
        "_ensure_cached_voicebank",
        lambda voicebank_id: full_loads.append(("storage", voicebank_id)) or tmp_path / "full",
    )
    monkeypatch.setattr(
        voicebank_cache,
        "_resolve_local_voicebank",
        lambda voicebank_id: full_loads.append(("local", voicebank_id)) or tmp_path / "full",
    )
    monkeypatch.setenv("VOICEBANK_TEXT_CACHE_DIR", str(tmp_path / "voicebank-text"))
    return full_loads


@pytest.fixture
def in_prod(monkeypatch, voicebanks):
    bucket = _FakeBucket({voicebank_cache.voicebank_text_pack_object(ARCHIVE_OBJECT): _text_pack_bytes()})
    monkeypatch.setattr(voicebank_cache, "is_prod_env", lambda: True)
    monkeypatch.setattr(voicebank_cache, "_voicebank_bucket", lambda: "bucket")
    monkeypatch.setattr(voicebank_cache, "get_bucket", lambda name: bucket)
    return bucket


@pytest.fixture
def in_local(monkeypatch, voicebanks, tmp_path):
    """A local project whose only copy of the voicebank's text is the pack beside its archive."""
    project = tmp_path / "project"
    local_pack = project / voicebank_cache.voicebank_text_pack_object(ARCHIVE_OBJECT)
    local_pack.parent.mkdir(parents=True)
    local_pack.write_bytes(_text_pack_bytes())
    monkeypatch.setattr(voicebank_cache, "is_prod_env", lambda: False)
    monkeypatch.setattr(voicebank_cache, "_project_root", lambda: project)
    return local_pack


def test_in_prod_text_tools_download_the_small_pack_once_and_never_the_archive(in_prod, voicebanks):
    first = voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)
    second = voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)

    assert first == second
    assert (first / "dsconfig.yaml").is_file()
    assert (first / "dsdur" / "dsdict-en.yaml").is_file()
    assert not list(first.rglob("*.onnx"))
    assert not list(first.parents[1].rglob("*.tar.gz"))
    assert in_prod.downloads == [voicebank_cache.voicebank_text_pack_object(ARCHIVE_OBJECT)]
    assert voicebanks == []
    # It still resolves to its manifest voicebank, as the full voicebank does.
    assert VOICEBANK_ID in [parent.name for parent in (first, *first.parents)]


def test_locally_text_tools_read_the_pack_beside_the_archive(in_local, voicebanks):
    resolved = voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)

    assert (resolved / "dsdur" / "dsdict-en.yaml").is_file()
    assert not list(resolved.rglob("*.onnx"))
    assert voicebanks == []


def test_a_rebuilt_local_pack_is_extracted_again(in_local, voicebanks):
    import os

    first = voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)
    os.utime(in_local, ns=(in_local.stat().st_atime_ns, in_local.stat().st_mtime_ns + 1_000_000))
    second = voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)

    assert first != second
    assert (second / "dsconfig.yaml").is_file()


@pytest.mark.parametrize("environment", ["in_prod", "in_local"])
def test_a_missing_pack_is_an_error_never_a_full_download(request, voicebanks, caplog, environment):
    setup = request.getfixturevalue(environment)
    if environment == "in_prod":
        setup.store.clear()
    else:
        setup.unlink()

    with pytest.raises(voicebank_cache.VoicebankTextPackMissingError) as missing:
        voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)

    # A deployment miss: nothing falls back to the full voicebank.
    assert voicebanks == []
    assert "build_voicebank_text_packs.py" in str(missing.value)
    assert any(
        record.levelname == "ERROR" and "voicebank_text_pack_missing" in record.message
        for record in caplog.records
    )


def test_the_birthday_name_tool_reads_the_text_pack(monkeypatch):
    import src.api.birthday_name as birthday_name
    import src.mcp.handlers as handlers

    resolved = []
    monkeypatch.setattr(
        handlers, "resolve_voicebank_text_id", lambda voicebank: resolved.append(voicebank) or Path("/t")
    )
    monkeypatch.setattr(
        handlers, "resolve_voicebank_id", lambda voicebank: pytest.fail("loaded the full voicebank")
    )
    monkeypatch.setattr(
        birthday_name, "replace_birthday_name", lambda *args, **kwargs: {"voicebank_path": kwargs["voicebank_path"]}
    )
    monkeypatch.setattr(handlers, "resolve_project_path", lambda value: Path(value))

    result = handlers.handle_replace_birthday_name(
        {"source_musicxml_path": "a.xml", "output_musicxml_path": "b.xml", "voicebank": VOICEBANK_ID},
        "cpu",
    )

    assert resolved == [VOICEBANK_ID]
    assert result == {"voicebank_path": Path("/t")}


LIEE = "Diffsinger LIEE Immortal Idol (JubiLIEE 2025)"
LIEE_PACK = ROOT / "assets" / "voicebanks" / "Diffsinger_LIEE_Immortal_Idol_JubiLIEE_2025.text.tar.gz"
HAPPY_BIRTHDAY = ROOT / "ui" / "public" / "demo-scores" / "happy-birthday.xml"


@pytest.mark.skipif(not LIEE_PACK.is_file(), reason="needs the local LIEE text pack (build_voicebank_text_packs.py)")
def test_happy_birthday_names_are_checked_against_the_liee_text_pack_alone(monkeypatch, tmp_path):
    """With the full voicebank out of reach, the real tool still checks and writes the name."""
    import src.mcp.handlers as handlers

    def no_full_voicebank(voicebank_id):
        raise AssertionError(f"loaded the full voicebank {voicebank_id}")

    monkeypatch.setattr(voicebank_cache, "is_prod_env", lambda: False)
    monkeypatch.setattr(voicebank_cache, "_resolve_local_voicebank", no_full_voicebank)
    monkeypatch.setattr(voicebank_cache, "_ensure_cached_voicebank", no_full_voicebank)
    monkeypatch.setenv("VOICEBANK_TEXT_CACHE_DIR", str(tmp_path / "voicebank-text"))
    monkeypatch.setattr(handlers, "resolve_project_path", lambda value: Path(value))
    resolved = []
    real_resolve = handlers.resolve_voicebank_text_id
    monkeypatch.setattr(
        handlers,
        "resolve_voicebank_text_id",
        lambda voicebank: resolved.append(real_resolve(voicebank)) or resolved[-1],
    )

    def sing_to(name, sung_text):
        return handlers.handle_replace_birthday_name(
            {
                "source_musicxml_path": str(HAPPY_BIRTHDAY),
                "output_musicxml_path": str(tmp_path / f"{name}.xml"),
                "name": name,
                "sung_text": sung_text,
                "voicebank": LIEE,
            },
            "cpu",
        )

    sarah = sing_to("Sarah", "Sa-rah")
    mismatch = sing_to("Christopher", "Chris-topher")

    assert sarah["status"] == "name_ready" and sarah["sung_text"] == "Sa-rah"
    assert "<text>Sa</text>" in (tmp_path / "Sarah.xml").read_text()
    # The check really ran on the pack's dictionary: a wrong split is caught.
    assert mismatch["status"] == "action_required"
    assert mismatch["code"] == "name_syllables_mismatch"
    text_cache = (tmp_path / "voicebank-text").resolve()
    assert all(text_cache in path.resolve().parents for path in resolved)
    assert all(not list(path.rglob("*.onnx")) for path in resolved)


def test_the_packer_keeps_each_pack_locally_at_its_storage_path(monkeypatch, tmp_path):
    import json
    import sys

    archive = _tar_gz(FILES)
    uploads = []

    class _PackerBlob:
        def __init__(self, name):
            self.name = name

        def open(self, mode):
            assert self.name == ARCHIVE_OBJECT and mode == "rb"
            return io.BytesIO(archive)

        def upload_from_filename(self, path, content_type=None):
            uploads.append((self.name, Path(path).read_bytes(), content_type))

    class _PackerBucket:
        def blob(self, name):
            return _PackerBlob(name)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"voicebanks": [{"id": VOICEBANK_ID, "storage_object": ARCHIVE_OBJECT}]}))
    monkeypatch.setattr(packer, "get_bucket", lambda name: _PackerBucket())
    local_pack = tmp_path / voicebank_cache.voicebank_text_pack_object(ARCHIVE_OBJECT)
    common = ["build", "--manifest", str(manifest), "--out-root", str(tmp_path)]

    monkeypatch.setattr(sys, "argv", common + ["--dry-run"])
    assert packer.main() == 0
    assert local_pack.is_file()
    assert not local_pack.with_name(local_pack.name + ".partial").exists()
    assert uploads == []

    monkeypatch.setattr(sys, "argv", common)
    assert packer.main() == 0
    ((name, data, content_type),) = uploads
    assert name == "assets/voicebanks/Test_Voice.text.tar.gz"
    assert data == local_pack.read_bytes()
    assert content_type == "application/gzip"
