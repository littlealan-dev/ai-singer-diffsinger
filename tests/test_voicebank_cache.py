import os
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.api.voicebank_cache import resolve_voicebank_path


class FakeBlob:
    def __init__(self, name: str, source_path: Path) -> None:
        self.name = name
        self._source_path = source_path

    def download_to_filename(self, filename: str) -> None:
        Path(filename).write_bytes(self._source_path.read_bytes())


class TestVoicebankCache(unittest.TestCase):
    def setUp(self) -> None:
        self.root_dir = Path(__file__).parent.parent
        self.voicebank_id = "Raine_Rena_2.01"

    def test_dev_env_uses_local_voicebank(self) -> None:
        with mock.patch.dict(os.environ, {"APP_ENV": "dev"}, clear=False):
            path = resolve_voicebank_path(self.voicebank_id)
        self.assertTrue((path / "dsconfig.yaml").exists())
        self.assertIn("assets/voicebanks", str(path))

    def test_prod_env_downloads_to_cache(self) -> None:
        with TemporaryDirectory() as gcs_root, TemporaryDirectory() as cache_root:
            gcs_root_path = Path(gcs_root)
            cache_root_path = Path(cache_root)
            registry_path = gcs_root_path / "registry.yaml"
            registry_path.write_text("voicebanks:\n  - id: TestBank\n", encoding="utf-8")
            source_dir = gcs_root_path / "tmp" / "TestBank"
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / "dsconfig.yaml").write_text("sample_rate: 44100\n", encoding="utf-8")
            (source_dir / "character.yaml").write_text("name: TestBank\n", encoding="utf-8")
            archive_path = gcs_root_path / "assets" / "voicebanks" / "TestBank.tar.gz"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "w:gz") as tar:
                for file_path in source_dir.rglob("*"):
                    if file_path.is_file():
                        tar.add(file_path, arcname=file_path.relative_to(source_dir))

            def fake_list_blobs(bucket_name: str, prefix: str):
                blobs = []
                for file_path in gcs_root_path.rglob("*"):
                    if not file_path.is_file():
                        continue
                    name = str(file_path.relative_to(gcs_root_path)).replace(os.sep, "/")
                    if not name.startswith(prefix):
                        continue
                    blobs.append(FakeBlob(name, file_path))
                return blobs

            with mock.patch.dict(
                os.environ,
                {
                    "APP_ENV": "prod",
                    "VOICEBANK_BUCKET": "dummy-bucket",
                    "VOICEBANK_PREFIX": "assets/voicebanks",
                    "VOICEBANK_CACHE_DIR": str(cache_root_path),
                    "VOICEBANK_REGISTRY_PATH": str(registry_path),
                },
                clear=False,
            ), mock.patch(
                "src.api.voicebank_cache.list_blobs",
                side_effect=fake_list_blobs,
            ):
                resolved = resolve_voicebank_path("TestBank")

            self.assertTrue((resolved / "dsconfig.yaml").exists())
            self.assertIn("TestBank", str(resolved))

    def test_prod_env_downloads_nested_voicebank_to_cache(self) -> None:
        with TemporaryDirectory() as gcs_root, TemporaryDirectory() as cache_root:
            gcs_root_path = Path(gcs_root)
            cache_root_path = Path(cache_root)
            registry_path = gcs_root_path / "registry.yaml"
            registry_path.write_text("voicebanks:\n  - id: NestedBank\n", encoding="utf-8")
            source_dir = gcs_root_path / "tmp" / "NestedBank" / "configs"
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / "dsconfig.yaml").write_text("sample_rate: 44100\n", encoding="utf-8")
            (source_dir / "character.yaml").write_text("name: Nested Bank\n", encoding="utf-8")
            archive_path = gcs_root_path / "assets" / "voicebanks" / "NestedBank.tar.gz"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "w:gz") as tar:
                for file_path in (gcs_root_path / "tmp" / "NestedBank").rglob("*"):
                    if file_path.is_file():
                        tar.add(file_path, arcname=file_path.relative_to(gcs_root_path / "tmp" / "NestedBank"))

            def fake_list_blobs(bucket_name: str, prefix: str):
                blobs = []
                for file_path in gcs_root_path.rglob("*"):
                    if not file_path.is_file():
                        continue
                    name = str(file_path.relative_to(gcs_root_path)).replace(os.sep, "/")
                    if not name.startswith(prefix):
                        continue
                    blobs.append(FakeBlob(name, file_path))
                return blobs

            with mock.patch.dict(
                os.environ,
                {
                    "APP_ENV": "prod",
                    "VOICEBANK_BUCKET": "dummy-bucket",
                    "VOICEBANK_PREFIX": "assets/voicebanks",
                    "VOICEBANK_CACHE_DIR": str(cache_root_path),
                    "VOICEBANK_REGISTRY_PATH": str(registry_path),
                },
                clear=False,
            ), mock.patch(
                "src.api.voicebank_cache.list_blobs",
                side_effect=fake_list_blobs,
            ):
                resolved = resolve_voicebank_path("NestedBank")

            self.assertEqual(resolved.resolve(), (cache_root_path / "NestedBank" / "configs").resolve())
            self.assertTrue((resolved / "dsconfig.yaml").exists())

    def test_dev_env_resolves_nested_registered_voicebank_root(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            voicebanks_root = tmp_root / "voicebanks"
            nested_root = voicebanks_root / "NestedBank" / "configs"
            nested_root.mkdir(parents=True, exist_ok=True)
            (nested_root / "dsconfig.yaml").write_text("sample_rate: 44100\n", encoding="utf-8")
            (nested_root / "character.yaml").write_text("name: Nested Bank\n", encoding="utf-8")
            registry_path = tmp_root / "registry.yaml"
            registry_path.write_text("voicebanks:\n  - id: NestedBank\n", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {
                    "APP_ENV": "dev",
                    "VOICEBANK_REGISTRY_PATH": str(registry_path),
                },
                clear=False,
            ), mock.patch("src.api.voicebank_cache._local_voicebanks_root", return_value=voicebanks_root):
                resolved = resolve_voicebank_path("NestedBank")

            self.assertEqual(resolved, nested_root.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
