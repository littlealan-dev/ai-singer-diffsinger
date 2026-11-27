import os
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.api.voicebank_cache import (
    VoicebankNotBakedError,
    _load_voicebank_manifest_for_path,
    resolve_manifest_phonemizer_dictionary,
    resolve_voicebank_path,
)


class TestVoicebankCache(unittest.TestCase):
    def setUp(self) -> None:
        self.root_dir = Path(__file__).parent.parent
        self.voicebank_id = "PM-31_Commercial_Indigo"

    def test_dev_env_uses_local_voicebank(self) -> None:
        with mock.patch.dict(os.environ, {"APP_ENV": "dev"}, clear=False):
            path = resolve_voicebank_path(self.voicebank_id)
        self.assertTrue((path / "dsconfig.yaml").exists())
        self.assertIn("assets/voicebanks", str(path))

    def _write_manifest(self, path: Path, voicebanks: dict[str, bool]) -> Path:
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "voicebanks": [
                        {
                            "id": voicebank_id,
                            "enabled": enabled,
                            "storage_object": f"assets/voicebanks/{voicebank_id}.tar.gz",
                        }
                        for voicebank_id, enabled in voicebanks.items()
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def _write_voicebank(self, root: Path) -> Path:
        root.mkdir(parents=True)
        (root / "dsconfig.yaml").write_text("sample_rate: 44100\n", encoding="utf-8")
        (root / "character.yaml").write_text("name: TestBank\n", encoding="utf-8")
        return root

    def _resolve_in_prod(self, manifest_path: Path, baked_root: Path, voicebank_id: str) -> Path:
        # Prod must never reach Cloud Storage for a full voicebank.
        with mock.patch.dict(
            os.environ,
            {
                "APP_ENV": "prod",
                "VOICEBANK_BUCKET": "dummy-bucket",
                "BAKED_VOICEBANKS_DIR": str(baked_root),
                "VOICEBANK_MANIFEST_PATH": str(manifest_path),
            },
            clear=False,
        ), mock.patch(
            "src.backend.storage_client.get_storage_client",
            side_effect=AssertionError("reached Cloud Storage"),
        ), mock.patch(
            "src.api.voicebank_cache.get_bucket",
            side_effect=AssertionError("reached Cloud Storage"),
        ):
            return resolve_voicebank_path(voicebank_id)

    def test_prod_env_reads_baked_voicebank(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            manifest_path = self._write_manifest(tmp / "manifest.json", {"TestBank": True})
            baked = self._write_voicebank(tmp / "baked" / "TestBank")

            with self.assertLogs("src.api.voicebank_cache", level="INFO") as logs:
                resolved = self._resolve_in_prod(manifest_path, tmp / "baked", "TestBank")

            self.assertEqual(resolved, baked)
            self.assertTrue(any("voicebank_baked_hit voicebank=TestBank" in line for line in logs.output))

    def test_prod_env_reads_nested_baked_voicebank(self) -> None:
        # An archive with its own top folder bakes to <id>/<top folder>/.
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            manifest_path = self._write_manifest(tmp / "manifest.json", {"NestedBank": True})
            nested = self._write_voicebank(tmp / "baked" / "NestedBank" / "configs")
            (nested / "dsvocoder").mkdir()
            (nested / "dsvocoder" / "dsconfig.yaml").write_text("name: vocoder\n", encoding="utf-8")

            resolved = self._resolve_in_prod(manifest_path, tmp / "baked", "NestedBank")

            self.assertEqual(resolved, nested)

    def test_prod_env_fails_loudly_when_voicebank_is_not_baked(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            manifest_path = self._write_manifest(tmp / "manifest.json", {"TestBank": True, "OtherBank": True})
            self._write_voicebank(tmp / "baked" / "OtherBank")

            with self.assertLogs("src.api.voicebank_cache", level="ERROR") as logs:
                with self.assertRaises(VoicebankNotBakedError) as raised:
                    self._resolve_in_prod(manifest_path, tmp / "baked", "TestBank")

            self.assertIsInstance(raised.exception, FileNotFoundError)
            self.assertIn("TestBank", str(raised.exception))
            self.assertTrue(any("voicebank_not_baked voicebank=TestBank" in line for line in logs.output))

    def test_prod_env_fails_when_baked_folder_has_no_dsconfig(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            manifest_path = self._write_manifest(tmp / "manifest.json", {"TestBank": True})
            (tmp / "baked" / "TestBank").mkdir(parents=True)
            (tmp / "baked" / "TestBank" / "character.yaml").write_text("name: TestBank\n", encoding="utf-8")

            with self.assertRaises(VoicebankNotBakedError):
                self._resolve_in_prod(manifest_path, tmp / "baked", "TestBank")

    def test_prod_env_still_refuses_voicebank_not_in_manifest(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            manifest_path = self._write_manifest(tmp / "manifest.json", {"OtherBank": True})
            self._write_voicebank(tmp / "baked" / "TestBank")

            with self.assertRaisesRegex(FileNotFoundError, "not declared in manifest"):
                self._resolve_in_prod(manifest_path, tmp / "baked", "TestBank")

    def test_dev_env_resolves_nested_manifest_voicebank_root(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            voicebanks_root = tmp_root / "voicebanks"
            nested_root = voicebanks_root / "NestedBank" / "configs"
            nested_root.mkdir(parents=True, exist_ok=True)
            (nested_root / "dsconfig.yaml").write_text("sample_rate: 44100\n", encoding="utf-8")
            (nested_root / "character.yaml").write_text("name: Nested Bank\n", encoding="utf-8")
            manifest_path = tmp_root / "voicebank_manifest.dev.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "generated_at": "2026-03-27T00:00:00Z",
                        "voicebanks": [
                            {
                                "id": "NestedBank",
                                "enabled": True,
                                "storage_object": "assets/voicebanks/NestedBank.tar.gz",
                                "name": "Nested Bank",
                                "path_hint": "NestedBank/configs",
                                "languages": [],
                                "has_duration_model": False,
                                "has_pitch_model": False,
                                "has_variance_model": False,
                                "speakers": [],
                                "voice_colors": [],
                                "default_voice_color": None,
                                "sample_rate": 44100,
                                "hop_size": 512,
                                "use_lang_id": False,
                                "profile_gender": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "APP_ENV": "dev",
                    "VOICEBANK_MANIFEST_PATH": str(manifest_path),
                },
                clear=False,
            ), mock.patch("src.api.voicebank_cache._local_voicebanks_root", return_value=voicebanks_root):
                resolved = resolve_voicebank_path("NestedBank")

            self.assertEqual(resolved, nested_root.resolve())

    def test_prod_env_errors_when_manifest_is_missing(self) -> None:
        with TemporaryDirectory() as cache_root:
            with mock.patch.dict(
                os.environ,
                {
                    "APP_ENV": "prod",
                    "VOICEBANK_BUCKET": "dummy-bucket",
                    "BAKED_VOICEBANKS_DIR": str(Path(cache_root)),
                    "VOICEBANK_MANIFEST_PATH": str(Path(cache_root) / "missing.json"),
                },
                clear=False,
            ):
                with self.assertRaisesRegex(FileNotFoundError, "Voicebank manifest not found"):
                    resolve_voicebank_path("TestBank")

    def test_manifest_requires_complete_phonemizer_dictionary_mapping(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            manifest_path = Path(tmp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "voicebanks": [
                            {
                                "id": "TestBank",
                                "enabled": True,
                                "languages": ["en", "ja"],
                                "phonemizer_dictionaries": {
                                    "en": "dsdur/dsdict-en.yaml"
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "missing: ja"):
                _load_voicebank_manifest_for_path(str(manifest_path))

    def test_manifest_rejects_phonemizer_dictionary_path_traversal(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            manifest_path = Path(tmp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "voicebanks": [
                            {
                                "id": "TestBank",
                                "enabled": True,
                                "languages": ["en"],
                                "phonemizer_dictionaries": {"en": "../dsdict-en.yaml"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must stay within the voicebank root"):
                _load_voicebank_manifest_for_path(str(manifest_path))

    def test_resolve_manifest_phonemizer_dictionary_returns_declared_file(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            voicebank_root = Path(tmp_dir) / "TestBank"
            dictionary_path = voicebank_root / "dsdur" / "dsdict-en.yaml"
            dictionary_path.parent.mkdir(parents=True)
            dictionary_path.write_text("entries: []\n", encoding="utf-8")
            metadata = {
                "phonemizer_dictionaries": {"en": "dsdur/dsdict-en.yaml"}
            }

            with mock.patch(
                "src.api.voicebank_cache.resolve_manifest_voicebank_id",
                return_value="TestBank",
            ), mock.patch(
                "src.api.voicebank_cache.get_manifest_voicebank_metadata",
                return_value=metadata,
            ):
                resolved = resolve_manifest_phonemizer_dictionary(
                    voicebank_root,
                    "en",
                )

            self.assertEqual(resolved, dictionary_path.resolve())

    def test_resolve_manifest_phonemizer_dictionary_returns_none_when_undeclared(self) -> None:
        with mock.patch(
            "src.api.voicebank_cache.resolve_manifest_voicebank_id",
            return_value="LegacyBank",
        ), mock.patch(
            "src.api.voicebank_cache.get_manifest_voicebank_metadata",
            return_value={},
        ):
            resolved = resolve_manifest_phonemizer_dictionary(
                Path("/tmp/LegacyBank"),
                "en",
            )

        self.assertIsNone(resolved)

    def test_enabled_voicebanks_have_complete_existing_dictionary_mappings(self) -> None:
        expected_ids = {
            "PM-31_Commercial_Indigo",
            "PM-31_Commercial_Scarlet",
            "Qixuan_v2.7.0_DiffSinger_OpenUtau",
        }
        for environment in ("dev", "prod"):
            manifest_path = self.root_dir / "env" / f"voicebank_manifest.{environment}.json"
            manifest = _load_voicebank_manifest_for_path(str(manifest_path.resolve()))
            enabled = [entry for entry in manifest["voicebanks"] if entry["enabled"]]
            self.assertEqual({entry["id"] for entry in enabled}, expected_ids)
            for entry in enabled:
                self.assertEqual(
                    set(entry["phonemizer_dictionaries"]),
                    set(entry["languages"]),
                )
                voicebank_root = (
                    self.root_dir / "assets" / "voicebanks" / entry["path_hint"]
                )
                for dictionary_ref in entry["phonemizer_dictionaries"].values():
                    self.assertTrue(
                        (voicebank_root / dictionary_ref).is_file(),
                        f"Missing dictionary for {entry['id']}: {dictionary_ref}",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
