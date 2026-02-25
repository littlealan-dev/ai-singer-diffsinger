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
            [
                str(self.python_path),
                "-m",
                "src.mcp_server",
                "--device",
                "cpu",
                "--debug",
            ],
            cwd=self.root_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
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
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout:
            self.proc.stdout.close()
        if self.proc.stderr:
            self.proc.stderr.close()

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
        response = None
        while response is None:
            line = self.proc.stdout.readline()
            if not line:
                stderr = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"MCP server closed unexpectedly. stderr={stderr}")
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("{"):
                print(stripped)
                continue
            try:
                response = json.loads(stripped)
            except json.JSONDecodeError:
                print(stripped)
                continue
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
        preprocess_plan = {
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "soprano"},
                    "actions": [
                        {
                            "type": "split_voice_part",
                            "split_shared_note_policy": "duplicate_to_all",
                        }
                    ],
                }
            ]
        }
        preprocess_result = self._call_tool(
            "preprocess_voice_parts",
            {"score": score, "request": {"plan": preprocess_plan}},
        )
        self.assertIn("status", preprocess_result)
        self.assertIn(preprocess_result["status"], {"ready", "ready_with_warnings"})
        score = preprocess_result.get("score", score)
        synth_part_index = preprocess_result.get("part_index", 0)
        notes = score["parts"][synth_part_index].get("notes", [])
        if notes:
            cut_index = None
            for idx, note in enumerate(notes):
                lyric = note.get("lyric")
                if lyric and "me" in lyric.lower():
                    cut_index = idx
                    break
            half_index = max(0, (len(notes) // 2) - 1)
            cut_index = max(cut_index, half_index) if cut_index is not None else half_index
            score["parts"][synth_part_index]["notes"] = notes[: cut_index + 1]
        synth_result = self._call_tool(
            "synthesize",
            {
                "score": score,
                "voicebank": self.voicebank_id,
                "part_index": synth_part_index,
                "voice_id": "soprano",
            },
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
