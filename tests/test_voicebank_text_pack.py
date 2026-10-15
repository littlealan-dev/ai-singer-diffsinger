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


@pytest.fixture
def prod_voicebanks(monkeypatch, tmp_path):
    """A prod-mode voicebank cache backed by a fake bucket holding only the text pack."""
    pack_object = voicebank_cache.voicebank_text_pack_object(ARCHIVE_OBJECT)
    text_files = {k: v for k, v in FILES.items() if voicebank_cache.is_voicebank_text_file(k)}
    bucket = _FakeBucket({pack_object: _tar_gz(text_files)})
    full_downloads = []
    monkeypatch.setattr(voicebank_cache, "is_prod_env", lambda: True)
    monkeypatch.setattr(voicebank_cache, "_check_voicebank_id", lambda voicebank_id: None)
    monkeypatch.setattr(voicebank_cache, "_voicebank_bucket", lambda: "bucket")
    monkeypatch.setattr(
        voicebank_cache,
        "get_manifest_voicebank_metadata",
        lambda voicebank_id: {"storage_object": ARCHIVE_OBJECT},
    )
    monkeypatch.setattr(voicebank_cache, "get_bucket", lambda name: bucket)
    monkeypatch.setattr(
        voicebank_cache,
        "_ensure_cached_voicebank",
        lambda voicebank_id: full_downloads.append(voicebank_id) or tmp_path / "full",
    )
    monkeypatch.setenv("VOICEBANK_CACHE_DIR", str(tmp_path / "voicebanks"))
    monkeypatch.setenv("VOICEBANK_TEXT_CACHE_DIR", str(tmp_path / "voicebank-text"))
    return bucket, full_downloads, pack_object


def test_the_text_path_downloads_the_small_pack_once_and_never_the_archive(prod_voicebanks):
    bucket, full_downloads, pack_object = prod_voicebanks

    first = voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)
    second = voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)

    assert first == second
    assert (first / "dsconfig.yaml").is_file()
    assert (first / "dsdur" / "dsdict-en.yaml").is_file()
    assert not list(first.rglob("*.onnx"))
    # The pack itself is deleted once extracted.
    assert not list(first.parent.parent.rglob("*.tar.gz"))
    assert bucket.downloads == [pack_object]
    assert full_downloads == []
    # It resolves to its manifest voicebank like the full cache does.
    assert first.name == VOICEBANK_ID


def test_a_missing_text_pack_falls_back_to_the_full_archive(prod_voicebanks):
    bucket, full_downloads, pack_object = prod_voicebanks
    bucket.store.clear()

    voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID)

    assert full_downloads == [VOICEBANK_ID]


def test_a_voicebank_already_cached_in_full_is_used_without_a_download(prod_voicebanks, tmp_path):
    bucket, full_downloads, _ = prod_voicebanks
    full_root = tmp_path / "voicebanks" / VOICEBANK_ID
    full_root.mkdir(parents=True)
    (full_root / "dsconfig.yaml").write_text("x")
    (full_root / "character.yaml").write_text("name: Test")

    assert voicebank_cache.resolve_voicebank_text_path(VOICEBANK_ID) == full_root
    assert bucket.downloads == []
    assert full_downloads == []


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
