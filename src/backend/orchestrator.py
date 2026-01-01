from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import asyncio
import logging
import json
import urllib.error
import urllib.request

from src.backend.config import Settings
from src.backend.llm_prompt import LlmResponse, ToolCall, build_system_prompt, parse_llm_response
from src.backend.mcp_client import McpRouter
from src.backend.session import SessionStore
from src.mcp.tools import list_tools


class Orchestrator:
    def __init__(self, router: McpRouter, sessions: SessionStore, settings: Settings) -> None:
        self._router = router
        self._sessions = sessions
        self._settings = settings
        self._logger = logging.getLogger(__name__)
        self._cached_voicebank: Optional[str] = None
        self._llm_tool_allowlist = {"modify_score", "synthesize"}
        self._llm_tools = list_tools(self._llm_tool_allowlist)

    async def handle_chat(self, session_id: str, message: str) -> Dict[str, Any]:
        await self._sessions.append_history(session_id, "user", message)
        snapshot = await self._sessions.get_snapshot(session_id)
        current_score = snapshot.get("current_score")
        response_message = "Acknowledged."
        include_score = self._should_include_score(message)

        if current_score is None:
            response_message = "Please upload a MusicXML file first."
            await self._sessions.append_history(session_id, "assistant", response_message)
            return {"type": "chat_text", "message": response_message}

        llm_response = await self._decide_with_llm(snapshot, score_available=True)
        if llm_response is not None:
            include_score = llm_response.include_score
            tool_result = await self._execute_tool_calls(
                session_id, current_score["score"], llm_response.tool_calls
            )
            response_message = llm_response.final_message or response_message
            response = tool_result.audio_response or {"type": "chat_text", "message": response_message}
            if response_message:
                response["message"] = response_message
            if include_score:
                updated_snapshot = await self._sessions.get_snapshot(session_id)
                updated_score = updated_snapshot.get("current_score")
                if updated_score is not None:
                    response["current_score"] = updated_score
            await self._sessions.append_history(session_id, "assistant", response["message"])
            return response
        if self._should_synthesize(message):
            response = await self._synthesize(session_id, current_score["score"], {})
            await self._sessions.append_history(session_id, "assistant", response["message"])
            return response

        if include_score:
            response = {
                "type": "chat_text",
                "message": response_message,
                "current_score": current_score,
            }
        else:
            response = {"type": "chat_text", "message": response_message}

        await self._sessions.append_history(session_id, "assistant", response_message)
        return response

    async def _synthesize(
        self, session_id: str, score: Dict[str, Any], arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        synth_args = dict(arguments)
        synth_args["score"] = score
        if "voicebank" not in synth_args:
            synth_args["voicebank"] = await self._resolve_voicebank()
        if "voice_id" not in synth_args and self._settings.default_voice_id:
            synth_args["voice_id"] = self._settings.default_voice_id

        self._logger.info("mcp_call tool=synthesize session=%s", session_id)
        synth_result = await asyncio.to_thread(
            self._router.call_tool, "synthesize", synth_args
        )
        waveform = synth_result["waveform"]
        sample_rate = synth_result["sample_rate"]
        output_path = self._sessions.session_dir(session_id) / "audio.wav"
        save_args = {
            "waveform": waveform,
            "output_path": str(
                (self._sessions.session_dir(session_id) / "audio.wav").relative_to(
                    self._settings.project_root
                )
            ),
            "sample_rate": sample_rate,
        }
        self._logger.info("mcp_call tool=save_audio session=%s", session_id)
        save_result = await asyncio.to_thread(self._router.call_tool, "save_audio", save_args)
        duration = save_result.get("duration_seconds", 0.0)
        await self._sessions.set_audio(session_id, output_path, duration)
        response = {
            "type": "chat_audio",
            "message": "Here is the rendered audio.",
            "audio_url": f"/sessions/{session_id}/audio",
        }
        return response

    async def _resolve_voicebank(self) -> str:
        if self._settings.default_voicebank:
            return self._settings.default_voicebank
        if self._cached_voicebank:
            return self._cached_voicebank
        self._logger.info("mcp_call tool=list_voicebanks")
        voicebanks = await asyncio.to_thread(self._router.call_tool, "list_voicebanks", {})
        if not voicebanks:
            raise RuntimeError("No voicebanks available.")
        self._cached_voicebank = voicebanks[0]["id"]
        return self._cached_voicebank

    def _should_synthesize(self, message: str) -> bool:
        lowered = message.lower()
        return any(keyword in lowered for keyword in ("synthesize", "render", "audio", "sing", "playback"))

    def _should_include_score(self, message: str) -> bool:
        lowered = message.lower()
        return any(keyword in lowered for keyword in ("score", "json", "notes"))

    async def _decide_with_llm(
        self, snapshot: Dict[str, Any], score_available: bool
    ) -> Optional[LlmResponse]:
        if not self._settings.gemini_api_key:
            return None
        history = snapshot.get("history", [])
        try:
            text = await asyncio.to_thread(self._call_gemini, history, score_available)
        except RuntimeError as exc:
            self._logger.warning("gemini_call_failed error=%s", exc)
            return None
        return parse_llm_response(text)

    def _call_gemini(self, history: list[Dict[str, str]], score_available: bool) -> str:
        url = (
            f"{self._settings.gemini_base_url}/models/"
            f"{self._settings.gemini_model}:generateContent"
            f"?key={self._settings.gemini_api_key}"
        )
        system_prompt = build_system_prompt(self._llm_tools, score_available)
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": self._history_to_contents(history),
            "generationConfig": {"temperature": 0.2},
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._settings.gemini_timeout_seconds
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Gemini HTTP error {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini connection error: {exc.reason}") from exc

        parsed = json.loads(body)
        candidates = parsed.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            raise RuntimeError("Gemini returned empty content parts.")
        text = parts[0].get("text", "")
        return text

    def _history_to_contents(self, history: list[Dict[str, str]]) -> list[Dict[str, Any]]:
        contents: list[Dict[str, Any]] = []
        for entry in history[-10:]:
            role = entry.get("role", "user")
            content_role = "model" if role == "assistant" else "user"
            contents.append({"role": content_role, "parts": [{"text": entry.get("content", "")}]})
        return contents

    async def _execute_tool_calls(
        self, session_id: str, score: Dict[str, Any], tool_calls: List[ToolCall]
    ) -> "ToolExecutionResult":
        current_score = score
        audio_response: Optional[Dict[str, Any]] = None
        for call in tool_calls:
            if call.name not in self._llm_tool_allowlist:
                self._logger.warning("llm_tool_not_allowed tool=%s", call.name)
                continue
            if call.name == "modify_score":
                code = call.arguments.get("code")
                if not isinstance(code, str) or not code.strip():
                    self._logger.warning("modify_score_missing_code")
                    continue
                arguments = {"score": current_score, "code": code}
                self._logger.info("mcp_call tool=modify_score session=%s", session_id)
                result = await asyncio.to_thread(
                    self._router.call_tool, "modify_score", arguments
                )
                if isinstance(result, dict):
                    current_score = result
                    await self._sessions.set_score(session_id, current_score)
                continue
            if call.name == "synthesize":
                synth_args = dict(call.arguments)
                synth_args.pop("score", None)
                audio_response = await self._synthesize(session_id, current_score, synth_args)
        return ToolExecutionResult(score=current_score, audio_response=audio_response)


@dataclass(frozen=True)
class ToolExecutionResult:
    score: Dict[str, Any]
    audio_response: Optional[Dict[str, Any]]
