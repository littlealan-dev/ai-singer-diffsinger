import json
import tempfile
import unittest
from pathlib import Path

from src.api.diffsinger_stage_tokens import build_stage_token_bundle
from src.api.voicebank_cache import (
    get_stage_phoneme_inventory,
    resolve_stage_phoneme_inventory_path,
)


class TestDiffSingerStageTokens(unittest.TestCase):
    def _write_yaml(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_stage_bundle_uses_stage_specific_pitch_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_yaml(root / "dsconfig.yaml", "phonemes: dsmain/phonemes.json\n")
            self._write_json(root / "dsmain" / "phonemes.json", {"SP": 6, "a": 80, "v": 108})

            self._write_yaml(
                root / "dspitch" / "dsconfig.yaml",
                "phonemes: dsmain/phonemes.json\npitch: pitch.onnx\n",
            )
            self._write_json(
                root / "dspitch" / "dsmain" / "phonemes.json",
                {"SP": 6, "a": 1, "v": 45},
            )

            bundle = build_stage_token_bundle(root, ["SP", "a", "v"])
            self.assertEqual(bundle.root_ids, [6, 80, 108])
            self.assertEqual(bundle.dur_ids, [6, 80, 108])
            self.assertEqual(bundle.pitch_ids, [6, 1, 45])

    def test_duration_stage_falls_back_to_root_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_yaml(root / "dsconfig.yaml", "phonemes: dsmain/phonemes.json\n")
            self._write_json(root / "dsmain" / "phonemes.json", {"SP": 6, "a": 7})

            bundle = build_stage_token_bundle(root, ["SP", "a"])
            self.assertEqual(bundle.root_ids, [6, 7])
            self.assertEqual(bundle.dur_ids, [6, 7])
            self.assertEqual(
                resolve_stage_phoneme_inventory_path(root, "dur").resolve(),
                (root / "dsmain" / "phonemes.json").resolve(),
            )

    def test_structural_ap_falls_back_to_stage_sp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_yaml(root / "dsconfig.yaml", "phonemes: dsmain/phonemes.json\n")
            self._write_json(root / "dsmain" / "phonemes.json", {"SP": 6, "AP": 2, "a": 9})

            self._write_yaml(
                root / "dspitch" / "dsconfig.yaml",
                "phonemes: dsmain/phonemes.json\npitch: pitch.onnx\n",
            )
            self._write_json(
                root / "dspitch" / "dsmain" / "phonemes.json",
                {"SP": 4, "a": 1},
            )

            bundle = build_stage_token_bundle(root, ["AP", "a"])
            self.assertEqual(bundle.pitch_ids, [4, 1])

    def test_missing_non_structural_symbol_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_yaml(root / "dsconfig.yaml", "phonemes: dsmain/phonemes.json\n")
            self._write_json(root / "dsmain" / "phonemes.json", {"SP": 6, "a": 9, "v": 10})

            self._write_yaml(
                root / "dspitch" / "dsconfig.yaml",
                "phonemes: dsmain/phonemes.json\npitch: pitch.onnx\n",
            )
            self._write_json(
                root / "dspitch" / "dsmain" / "phonemes.json",
                {"SP": 4, "a": 1},
            )

            with self.assertRaisesRegex(ValueError, "cannot encode symbol 'v'"):
                build_stage_token_bundle(root, ["SP", "v"])

    def test_inventory_loader_reads_compressed_pitch_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_yaml(root / "dsconfig.yaml", "phonemes: dsmain/phonemes.json\n")
            self._write_json(root / "dsmain" / "phonemes.json", {"SP": 6, "aa": 1, "ah": 2, "v": 10})

            self._write_yaml(
                root / "dspitch" / "dsconfig.yaml",
                "phonemes: dsmain/phonemes.json\npitch: pitch.onnx\n",
            )
            self._write_json(
                root / "dspitch" / "dsmain" / "phonemes.json",
                {"SP": 6, "aa": 1, "ah": 1, "v": 45},
            )

            inventory = get_stage_phoneme_inventory(root, "pitch")
            self.assertEqual(inventory["aa"], 1)
            self.assertEqual(inventory["ah"], 1)
            self.assertEqual(inventory["v"], 45)


if __name__ == "__main__":
    unittest.main()
