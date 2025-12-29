import base64
import unittest
from pathlib import Path
from unittest import mock

from src.mcp_server import _handle_request


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).resolve().parents[1]
        self.voicebank_id = "Raine_Rena_2.01"
        self.voicebank_path = self.root_dir / "assets/voicebanks" / self.voicebank_id
        self.score_path = "assets/test_data/amazing-grace-satb-verse1.xml"
        self.device = "cpu"

    def _call_tool(self, name, arguments):
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = _handle_request(request, self.device)
        self.assertIn("result", response)
        return response["result"]

    def test_initialize(self):
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1.0"},
        }
        response = _handle_request(request, self.device)
        self.assertIn("result", response)
        result = response["result"]
        self.assertIn("serverInfo", result)
        self.assertIn("capabilities", result)

    def test_tools_list(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = _handle_request(request, self.device)
        self.assertIn("result", response)
        tools = response["result"]["tools"]
        self.assertTrue(any(tool["name"] == "parse_score" for tool in tools))

    def test_parse_score(self):
        with mock.patch("src.mcp.handlers.parse_score") as mock_parse:
            mock_parse.return_value = {"title": "ok"}
            result = self._call_tool(
                "parse_score",
                {"file_path": self.score_path, "expand_repeats": False},
            )
            self.assertEqual(result, {"title": "ok"})
            args, kwargs = mock_parse.call_args
            self.assertTrue(isinstance(args[0], Path))

    def test_modify_score(self):
        with mock.patch("src.mcp.handlers.modify_score") as mock_modify:
            mock_modify.return_value = {"modified": True}
            result = self._call_tool(
                "modify_score",
                {"score": {"parts": []}, "code": "score['parts']"},
            )
            self.assertEqual(result, {"modified": True})

    def test_phonemize(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        with mock.patch("src.mcp.handlers.phonemize") as mock_phonemize:
            mock_phonemize.return_value = {"phonemes": ["a"]}
            result = self._call_tool(
                "phonemize",
                {"lyrics": ["la"], "voicebank": self.voicebank_id, "language": "en"},
            )
            self.assertEqual(result, {"phonemes": ["a"]})
            args, _ = mock_phonemize.call_args
            self.assertTrue(isinstance(args[1], Path))

    def test_align_phonemes_to_notes(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        with mock.patch("src.mcp.handlers.align_phonemes_to_notes") as mock_align:
            mock_align.return_value = {"phoneme_ids": [1]}
            result = self._call_tool(
                "align_phonemes_to_notes",
                {"score": {"parts": []}, "voicebank": self.voicebank_id},
            )
            self.assertEqual(result, {"phoneme_ids": [1]})

    def test_predict_durations(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        with mock.patch("src.mcp.handlers.predict_durations") as mock_predict:
            mock_predict.return_value = {"durations": [1], "total_frames": 1}
            result = self._call_tool(
                "predict_durations",
                {
                    "phoneme_ids": [1],
                    "word_boundaries": [1],
                    "word_durations": [1],
                    "word_pitches": [60],
                    "voicebank": self.voicebank_id,
                },
            )
            self.assertEqual(result, {"durations": [1], "total_frames": 1})
            _, kwargs = mock_predict.call_args
            self.assertEqual(kwargs["device"], self.device)

    def test_predict_pitch(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        with mock.patch("src.mcp.handlers.predict_pitch") as mock_predict:
            mock_predict.return_value = {"f0": [440.0]}
            result = self._call_tool(
                "predict_pitch",
                {
                    "phoneme_ids": [1],
                    "durations": [1],
                    "note_pitches": [60],
                    "note_durations": [1],
                    "note_rests": [False],
                    "voicebank": self.voicebank_id,
                },
            )
            self.assertEqual(result, {"f0": [440.0]})
            _, kwargs = mock_predict.call_args
            self.assertEqual(kwargs["device"], self.device)

    def test_predict_variance(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        with mock.patch("src.mcp.handlers.predict_variance") as mock_predict:
            mock_predict.return_value = {"breathiness": [0.0]}
            result = self._call_tool(
                "predict_variance",
                {
                    "phoneme_ids": [1],
                    "durations": [1],
                    "f0": [440.0],
                    "voicebank": self.voicebank_id,
                },
            )
            self.assertEqual(result, {"breathiness": [0.0]})
            _, kwargs = mock_predict.call_args
            self.assertEqual(kwargs["device"], self.device)

    def test_synthesize_audio(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        with mock.patch("src.mcp.handlers.synthesize_audio") as mock_synth:
            mock_synth.return_value = {"waveform": [0.0], "sample_rate": 44100}
            result = self._call_tool(
                "synthesize_audio",
                {
                    "phoneme_ids": [1],
                    "durations": [1],
                    "f0": [440.0],
                    "voicebank": self.voicebank_id,
                    "vocoder_path": "assets/vocoders/pc_nsf_hifigan_44.1k",
                },
            )
            self.assertEqual(result, {"waveform": [0.0], "sample_rate": 44100})
            _, kwargs = mock_synth.call_args
            self.assertEqual(kwargs["device"], self.device)
            self.assertTrue(isinstance(kwargs["vocoder_path"], Path))

    def test_save_audio(self):
        output_rel = "tests/output/mcp_audio.wav"
        output_abs = (self.root_dir / output_rel).resolve()
        output_abs.parent.mkdir(parents=True, exist_ok=True)
        audio_bytes = b"RIFFTEST"
        output_abs.write_bytes(audio_bytes)

        with mock.patch("src.mcp.handlers.save_audio") as mock_save:
            mock_save.return_value = {
                "path": str(output_abs),
                "duration_seconds": 0.01,
                "sample_rate": 44100,
            }
            result = self._call_tool(
                "save_audio",
                {"waveform": [0.0], "output_path": output_rel, "sample_rate": 44100},
            )
            self.assertEqual(
                result["audio_base64"],
                base64.b64encode(audio_bytes).decode("ascii"),
            )
            self.assertEqual(result["sample_rate"], 44100)
            args, kwargs = mock_save.call_args
            self.assertTrue(isinstance(args[1], Path))

    def test_synthesize(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        with mock.patch("src.mcp.handlers.synthesize") as mock_syn:
            mock_syn.return_value = {"waveform": [0.0], "sample_rate": 44100}
            result = self._call_tool(
                "synthesize",
                {"score": {"parts": []}, "voicebank": self.voicebank_id},
            )
            self.assertEqual(result, {"waveform": [0.0], "sample_rate": 44100})
            _, kwargs = mock_syn.call_args
            self.assertEqual(kwargs["device"], self.device)

    def test_list_voicebanks(self):
        with mock.patch("src.mcp.handlers.list_voicebanks") as mock_list:
            mock_list.return_value = [
                {"id": "VoiceA", "name": "Voice A", "path": "assets/voicebanks/VoiceA"}
            ]
            result = self._call_tool("list_voicebanks", {})
            self.assertEqual(
                result,
                [{"id": "VoiceA", "name": "Voice A", "path": "assets/voicebanks/VoiceA"}],
            )

    def test_get_voicebank_info(self):
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        with mock.patch("src.mcp.handlers.get_voicebank_info") as mock_info:
            mock_info.return_value = {
                "name": "Raine Rena",
                "path": "assets/voicebanks/Raine_Rena_2.01",
                "sample_rate": 44100,
            }
            result = self._call_tool(
                "get_voicebank_info", {"voicebank": self.voicebank_id}
            )
            self.assertEqual(result, {"name": "Raine Rena", "sample_rate": 44100})


if __name__ == "__main__":
    unittest.main(verbosity=2)
