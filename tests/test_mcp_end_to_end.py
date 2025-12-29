import base64
import json
import subprocess
import unittest
from pathlib import Path


class TestMcpEndToEnd(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).resolve().parents[1]
        self.python_path = self.root_dir / ".venv" / "bin" / "python"
        self.voicebank_id = "Raine_Rena_2.01"
        self.voicebank_path = self.root_dir / "assets/voicebanks" / self.voicebank_id
        self.score_path = "assets/test_data/amazing-grace-satb-verse1.xml"
        self.output_path = "tests/output/mcp_e2e.wav"
        self.output_path_base64 = "tests/output/mcp_e2e_base64.wav"
        self._next_id = 1
        self.proc = None

        if not self.python_path.exists():
            self.skipTest(f"venv python not found at {self.python_path}")
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        if not (self.root_dir / self.score_path).exists():
            self.skipTest(f"Test score not found at {self.score_path}")

        self.proc = subprocess.Popen(
            [str(self.python_path), "-m", "src.mcp_server", "--device", "cpu"],
            cwd=self.root_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self._send_request("initialize", {"protocolVersion": "1.0"})

    def tearDown(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def _send_request(self, method, params):
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self._next_id += 1
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read()
            raise RuntimeError(f"MCP server closed unexpectedly. stderr={stderr}")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(f"MCP error: {response['error']}")
        if "result" not in response:
            raise RuntimeError(f"Unexpected MCP response: {response}")
        return response["result"]

    def _call_tool(self, name, arguments):
        result = self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(f"MCP tool error: {result['error']}")
        return result

    def test_full_synthesis_via_mcp(self):
        score = self._call_tool(
            "parse_score",
            {"file_path": self.score_path, "expand_repeats": False},
        )
        score = self._call_tool(
            "modify_score",
            {
                "score": score,
                "code": (
                    "notes = score['parts'][0]['notes']\n"
                    "cut_index = None\n"
                    "for idx in range(len(notes)):\n"
                    "    note = notes[idx]\n"
                    "    lyric = note.get('lyric')\n"
                    "    if lyric and 'me' in lyric.lower():\n"
                    "        cut_index = idx\n"
                    "        break\n"
                    "half_index = max(0, (len(notes) // 2) - 1)\n"
                    "if cut_index is None:\n"
                    "    cut_index = half_index\n"
                    "else:\n"
                    "    cut_index = max(cut_index, half_index)\n"
                    "score['parts'][0]['notes'] = notes[:cut_index + 1]\n"
                ),
            },
        )
        synth_result = self._call_tool(
            "synthesize",
            {"score": score, "voicebank": self.voicebank_id, "voice_id": "soprano"},
        )
        self.assertIn("waveform", synth_result)
        self.assertIn("sample_rate", synth_result)
        self.assertGreater(len(synth_result["waveform"]), 0)

        save_result = self._call_tool(
            "save_audio",
            {
                "waveform": synth_result["waveform"],
                "output_path": self.output_path,
                "sample_rate": synth_result["sample_rate"],
            },
        )
        self.assertIn("audio_base64", save_result)
        audio_bytes = base64.b64decode(save_result["audio_base64"])
        self.assertTrue(audio_bytes.startswith(b"RIFF"))
        output_path = self.root_dir / self.output_path_base64
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio_bytes)
        self.assertGreater(output_path.stat().st_size, 0)
