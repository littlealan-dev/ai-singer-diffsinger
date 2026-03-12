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
        response = _handle_request(request, self.device, "all")
        self.assertIn("result", response)
        return response["result"]

    def test_initialize(self):
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1.0"},
        }
        response = _handle_request(request, self.device, "all")
        self.assertIn("result", response)
        result = response["result"]
        self.assertIn("serverInfo", result)
        self.assertIn("capabilities", result)

    def test_tools_list(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = _handle_request(request, self.device, "all")
        self.assertIn("result", response)
        tools = response["result"]["tools"]
        self.assertTrue(any(tool["name"] == "parse_score" for tool in tools))
        self.assertTrue(any(tool["name"] == "reparse" for tool in tools))
        self.assertFalse(any(tool["name"] == "modify_score" for tool in tools))

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

    def test_reparse(self):
        with mock.patch("src.mcp.handlers.parse_score") as mock_parse:
            mock_parse.return_value = {"title": "ok"}
            result = self._call_tool(
                "reparse",
                {"file_path": self.score_path, "part_index": 0, "verse_number": 2},
            )
            self.assertEqual(result, {"title": "ok"})
            args, kwargs = mock_parse.call_args
            self.assertTrue(isinstance(args[0], Path))

    def test_preprocess_voice_parts(self):
        with mock.patch("src.mcp.handlers.preprocess_voice_parts") as mock_preprocess:
            mock_preprocess.return_value = {"status": "ready", "score": {"parts": []}, "part_index": 0}
            result = self._call_tool(
                "preprocess_voice_parts",
                {"score": {"parts": []}, "request": {"plan": {"targets": []}}},
            )
            self.assertEqual(result.get("status"), "ready")

    def test_preprocess_voice_parts_requires_plan(self):
        result = self._call_tool(
            "preprocess_voice_parts",
            {"score": {"parts": []}, "request": {}},
        )
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("code"), "preprocessing_plan_required")

    def test_preprocess_voice_parts_rejects_legacy_voice_id(self):
        result = self._call_tool(
            "preprocess_voice_parts",
            {"score": {"parts": []}, "request": {"plan": {"targets": []}, "voice_id": "soprano"}},
        )
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("code"), "deprecated_voice_id_input")

    def test_modify_score_not_available_publicly(self):
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "modify_score", "arguments": {"score": {}, "code": "pass"}},
        }
        response = _handle_request(request, self.device, "all")
        self.assertIn("result", response)
        self.assertIn("error", response["result"])
        self.assertIn("Tool not available in mode", response["result"]["error"]["message"])

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
                {
                    "score": {"parts": []},
                    "voicebank": self.voicebank_id,
                    "articulation": 0.25,
                    "airiness": 0.9,
                    "intensity": 0.9,
                    "clarity": 0.95,
                },
            )
            self.assertEqual(result, {"waveform": [0.0], "sample_rate": 44100})
            _, kwargs = mock_syn.call_args
            self.assertEqual(kwargs["device"], self.device)
            self.assertEqual(kwargs["articulation"], 0.25)
            self.assertEqual(kwargs["airiness"], 0.9)
            self.assertEqual(kwargs["intensity"], 0.9)
            self.assertEqual(kwargs["clarity"], 0.95)

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
