from __future__ import annotations

"""Chat orchestration layer that bridges LLM decisions and MCP tools."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import asyncio
import logging
import copy
import json
import uuid
from datetime import datetime, timezone

from src.backend.config import Settings
from src.backend.job_store import build_progress_payload
from src.backend.llm_client import LlmClient
from src.backend.llm_prompt import LlmResponse, ToolCall, build_system_prompt, parse_llm_response
from src.backend.mcp_client import McpRouter
from src.backend.job_store import JobStore
from src.backend.session import SessionStore
from src.backend.storage_client import copy_blob, upload_file
from src.mcp.logging_utils import clear_log_context, get_logger, set_log_context, summarize_payload
from src.mcp.tools import list_tools

TOOL_RESULT_PREFIX = "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"


class Orchestrator:
    """Coordinate sessions, LLM decisions, and synthesis tool calls."""
    def __init__(
        self,
        router: McpRouter,
        sessions: SessionStore,
        settings: Settings,
        llm_client: Optional[LlmClient],
    ) -> None:
        self._router = router
        self._sessions = sessions
        self._settings = settings
        self._llm_client = llm_client
        self._job_store = JobStore()
        self._logger = get_logger(__name__)
        self._logger.setLevel(logging.DEBUG)
        self._cached_voicebank: Optional[str] = None
        self._cached_voicebank_ids: Optional[List[str]] = None
        self._cached_voicebank_details: Optional[List[Dict[str, Any]]] = None
        self._llm_tool_allowlist = {"modify_score", "synthesize", "estimate_credits"}
        self._llm_tools = list_tools(self._llm_tool_allowlist)
        self._synthesis_tasks: Dict[str, asyncio.Task] = {}

    async def handle_chat(self, session_id: str, message: str, *, user_id: str) -> Dict[str, Any]:
        """Handle a chat message and return a response payload."""
        if len(message) > self._settings.llm_max_message_chars:
            return {
                "type": "chat_text",
                "message": (
                    "Message too long. Please keep instructions under "
                    f"{self._settings.llm_max_message_chars} characters."
                ),
            }
        if message.strip().startswith(TOOL_RESULT_PREFIX):
            return {
                "type": "chat_text",
                "message": "That request is not allowed.",
            }
        self._logger.debug("chat_user session=%s message=%s", session_id, message)
        await self._sessions.append_history(session_id, "user", message)
        snapshot = await self._sessions.get_snapshot(session_id, user_id)
        current_score = snapshot.get("current_score")
        response_message = "Acknowledged."
        include_score = self._should_include_score(message)

        if current_score is None:
            # Require a score before any synthesis steps.
            response_message = "Please upload a MusicXML file first."
            await self._sessions.append_history(session_id, "assistant", response_message)
            return {"type": "chat_text", "message": response_message}

        llm_response, llm_error = await self._decide_with_llm(snapshot, score_available=True)
        if llm_error:
            response_message = llm_error
            await self._sessions.append_history(session_id, "assistant", response_message)
            return {"type": "chat_text", "message": response_message}
        if llm_response is not None:
            self._logger.debug(
                "chat_llm session=%s response=%s",
                session_id,
                {
                    "tool_calls": [
                        {
                            "name": call.name,
                            "arguments": summarize_payload(call.arguments),
                        }
                        for call in llm_response.tool_calls
                    ],
                    "final_message": llm_response.final_message,
                    "include_score": llm_response.include_score,
                },
            )
        if llm_response is not None:
            # Execute tool calls and package response.
            include_score = llm_response.include_score
            session_files = snapshot.get("files") or {}
            tool_result = await self._execute_tool_calls(
                session_id,
                current_score["score"],
                llm_response.tool_calls,
                user_id=user_id,
                session_files=session_files,
                score_summary=snapshot.get("score_summary") if isinstance(snapshot, dict) else None,
            )
            response_message = llm_response.final_message or response_message
            response = tool_result.audio_response or {"type": "chat_text", "message": response_message}
            if tool_result.followup_prompt:
                followup_message = await self._render_tool_followup(snapshot, tool_result.followup_prompt)
                response["message"] = followup_message
            elif response_message:
                response["message"] = response_message
            if include_score:
                updated_snapshot = await self._sessions.get_snapshot(session_id, user_id)
                updated_score = updated_snapshot.get("current_score")
                if updated_score is not None:
                    response["current_score"] = updated_score
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
        self,
        session_id: str,
        score: Dict[str, Any],
        arguments: Dict[str, Any],
        *,
        job_id: Optional[str] = None,
        user_id: Optional[str] = None,
        output_storage_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the synthesize + save_audio flow for a session."""
        synth_args = dict(arguments)
        part_id = synth_args.get("part_id")
        part_index = synth_args.get("part_index")
        verse_number = synth_args.get("verse_number")
        if self._selection_requested(part_id, part_index, verse_number):
            if not self._selection_matches_current(score, part_id, part_index, verse_number):
                # Re-parse the score to honor updated selection parameters.
                updated_score = await self._reparse_score(
                    session_id,
                    part_id=part_id,
                    part_index=part_index,
                    verse_number=verse_number,
                    user_id=user_id,
                )
                if updated_score is not None:
                    score = updated_score
                    synth_args.pop("part_id", None)
                    synth_args.pop("part_index", None)
                    synth_args.pop("verse_number", None)
        synth_args["score"] = score
        if "voicebank" not in synth_args:
            synth_args["voicebank"] = await self._resolve_voicebank()
        if "voice_id" not in synth_args and self._settings.default_voice_id:
            synth_args["voice_id"] = self._settings.default_voice_id
        if job_id is not None:
            synth_args["progress_job_id"] = job_id
            synth_args["progress_user_id"] = user_id
        self._logger.info("mcp_call tool=synthesize session=%s", session_id)
        # Run synthesis on the MCP worker.
        synth_result = await asyncio.to_thread(
            self._router.call_tool, "synthesize", synth_args
        )
        waveform = synth_result["waveform"]
        sample_rate = synth_result["sample_rate"]
        audio_format = (self._settings.audio_format or "wav").lower()
        if audio_format != "mp3":
            audio_format = "wav"
        extension = "mp3" if audio_format == "mp3" else "wav"
        file_name = f"audio-{uuid.uuid4().hex}.{extension}"
        output_path = self._sessions.session_dir(session_id) / file_name
        save_args = {
            "waveform": waveform,
            "output_path": str(output_path.relative_to(self._settings.project_root)),
            "sample_rate": sample_rate,
            "format": audio_format,
        }
        if audio_format == "mp3":
            save_args["mp3_bitrate"] = self._settings.audio_mp3_bitrate
            save_args["keep_wav"] = bool(self._settings.backend_debug)
        if job_id is not None:
            await asyncio.to_thread(
                self._job_store.update_job,
                job_id,
                status="running",
                step="encode",
                message="Capturing the take...",
                progress=0.9,
            )
        self._logger.info("mcp_call tool=save_audio session=%s", session_id)
        save_result = await asyncio.to_thread(self._router.call_tool, "save_audio", save_args)
        duration = save_result.get("duration_seconds", 0.0)
        if self._settings.backend_use_storage and output_storage_path:
            await asyncio.to_thread(
                upload_file,
                self._settings.storage_bucket,
                output_path,
                output_storage_path,
                "audio/mpeg" if extension == "mp3" else "audio/wav",
            )
            await self._sessions.set_audio(
                session_id, output_path, duration, storage_path=output_storage_path
            )
        else:
            await self._sessions.set_audio(session_id, output_path, duration)
        response = {
            "type": "chat_audio",
            "message": "Here is the rendered audio.",
            "audio_url": f"/sessions/{session_id}/audio?file={file_name}",
            "output_path": str(output_path.relative_to(self._settings.project_root)),
            "output_storage_path": output_storage_path,
            "duration_seconds": duration,
        }
        return response

    async def _start_synthesis_job(
        self,
        session_id: str,
        score: Dict[str, Any],
        arguments: Dict[str, Any],
        *,
        user_id: str,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Firestore job and kick off synthesis in the background."""
        existing = self._synthesis_tasks.get(session_id)
        if existing and not existing.done():
            existing.cancel()
        if job_id is None:
            job_id = uuid.uuid4().hex
        snapshot = await self._sessions.get_snapshot(session_id, user_id)
        files = snapshot.get("files") or {}
        input_path = files.get("musicxml_path")
        storage_input_path = files.get("musicxml_storage_path")
        render_type = arguments.get("render_type")
        output_storage_path = None
        job_input_storage_path = None
        if self._settings.backend_use_storage:
            suffix = ".musicxml"
            if isinstance(input_path, str) and input_path:
                suffix = Path(input_path).suffix or suffix
            job_input_storage_path = _job_storage_input_path(
                user_id, session_id, job_id, suffix
            )
            output_storage_path = _job_storage_output_path(
                user_id, session_id, job_id, self._settings.audio_format
            )
        await asyncio.to_thread(
            self._job_store.create_job,
            job_id=job_id,
            user_id=user_id,
            session_id=session_id,
            status="queued",
            input_path=job_input_storage_path or input_path,
            render_type=render_type if isinstance(render_type, str) else None,
        )
        await asyncio.to_thread(
            self._job_store.update_job,
            job_id,
            status="queued",
            step="queued",
            message="Got it, getting ready to sing...",
            progress=0.0,
        )
        task = asyncio.create_task(
            self._run_synthesis_job(
                session_id,
                copy.deepcopy(score),
                arguments,
                job_id,
                user_id,
                input_path=input_path,
                storage_input_path=storage_input_path,
                job_input_storage_path=job_input_storage_path,
                output_storage_path=output_storage_path,
            )
        )
        self._synthesis_tasks[session_id] = task

        def _cleanup(_: asyncio.Task) -> None:
            self._synthesis_tasks.pop(session_id, None)

        task.add_done_callback(_cleanup)
        return {
            "type": "chat_progress",
            "message": "Give me a moment to prepare the take...",
            "progress_url": f"/sessions/{session_id}/progress",
            "job_id": job_id,
        }

    async def _run_synthesis_job(
        self,
        session_id: str,
        score: Dict[str, Any],
        arguments: Dict[str, Any],
        job_id: str,
        user_id: str,
        *,
        input_path: Optional[str],
        storage_input_path: Optional[str],
        job_input_storage_path: Optional[str],
        output_storage_path: Optional[str],
    ) -> None:
        """Execute a synthesis job and update status in Firestore."""
        try:
            set_log_context(session_id=session_id, job_id=job_id, user_id=user_id)
            if self._settings.backend_use_storage and job_input_storage_path:
                # Ensure job input is copied into storage when required.
                await asyncio.to_thread(
                    _ensure_job_input_storage,
                    self._settings.storage_bucket,
                    input_path,
                    storage_input_path,
                    job_input_storage_path,
                    self._settings.project_root,
                )
            await asyncio.to_thread(self._job_store.update_job, job_id, status="running")
            await asyncio.to_thread(
                self._job_store.update_job,
                job_id,
                status="running",
                step="prepare",
                message="Warming up the voice...",
                progress=0.05,
            )
            response = await self._synthesize(
                session_id,
                score,
                arguments,
                job_id=job_id,
                user_id=user_id,
                output_storage_path=output_storage_path,
            )
            await asyncio.to_thread(
                self._job_store.update_job,
                job_id,
                status="completed",
                step="done",
                message="Your take is ready.",
                progress=1.0,
                outputPath=response.get("output_storage_path") or response.get("output_path"),
                audioUrl=response.get("audio_url"),
            )
            # Settle credits
            from src.backend.credits import settle_credits
            duration_seconds = response.get("duration_seconds", 0.0)
            await asyncio.to_thread(settle_credits, user_id, job_id, duration_seconds)
        except asyncio.CancelledError:
            # Release credits
            from src.backend.credits import release_credits
            await asyncio.to_thread(release_credits, user_id, job_id)
            await asyncio.to_thread(
                self._job_store.update_job,
                job_id,
                status="cancelled",
                step="cancelled",
                message="That take was cancelled.",
                progress=1.0,
            )
            raise
        except Exception as exc:
            # Release credits
            from src.backend.credits import release_credits
            await asyncio.to_thread(release_credits, user_id, job_id)
            self._logger.exception("synthesis_failed session=%s error=%s", session_id, exc)
            await asyncio.to_thread(
                self._job_store.update_job,
                job_id,
                status="failed",
                step="error",
                message="Couldn't finish the take.",
                progress=1.0,
                errorMessage=str(exc),
            )
        finally:
            clear_log_context()

    def _selection_requested(
        self,
        part_id: Optional[str],
        part_index: Optional[int],
        verse_number: Optional[object],
    ) -> bool:
        """Return True if any selection parameters were provided."""
        return part_id is not None or part_index is not None or verse_number is not None

    def _selection_matches_current(
        self,
        score: Dict[str, Any],
        part_id: Optional[str],
        part_index: Optional[int],
        verse_number: Optional[object],
    ) -> bool:
        """Return True if the current score already matches the selection."""
        if verse_number is not None:
            return False
        parts = score.get("parts") or []
        if part_id is not None:
            if not parts:
                return False
            if len(parts) == 1:
                return parts[0].get("part_id") == part_id
            return any(part.get("part_id") == part_id for part in parts)
        if part_index is not None:
            return 0 <= part_index < len(parts)
        return True

    async def _reparse_score(
        self,
        session_id: str,
        *,
        part_id: Optional[str],
        part_index: Optional[int],
        verse_number: Optional[object],
        user_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Re-parse the current MusicXML file with new selection filters."""
        snapshot = await self._sessions.get_snapshot(session_id, user_id)
        files = snapshot.get("files") or {}
        file_path = files.get("musicxml_path")
        if not isinstance(file_path, str) or not file_path:
            return None
        parse_args: Dict[str, Any] = {"file_path": file_path, "expand_repeats": False}
        if part_id is not None:
            parse_args["part_id"] = part_id
        elif part_index is not None:
            parse_args["part_index"] = part_index
        if verse_number is not None:
            parse_args["verse_number"] = verse_number
        result = await asyncio.to_thread(self._router.call_tool, "parse_score", parse_args)
        if not isinstance(result, dict):
            return None
        score = dict(result)
        score.pop("score_summary", None)
        await self._sessions.set_score(session_id, score)
        return score

    async def _resolve_voicebank(self) -> str:
        """Resolve a default voicebank ID, using cached data when possible."""
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

    def _should_include_score(self, message: str) -> bool:
        """Return True if the user asked to see score data."""
        lowered = message.lower()
        return any(keyword in lowered for keyword in ("score", "json", "notes"))

    async def _decide_with_llm(
        self, snapshot: Dict[str, Any], score_available: bool
    ) -> tuple[Optional[LlmResponse], Optional[str]]:
        """Query the LLM to determine tool calls and response text."""
        if self._llm_client is None:
            return None, "LLM is not configured. Please try again later."
        history = snapshot.get("history", [])
        try:
            voicebank_ids = await self._get_voicebank_ids()
            voicebank_details = await self._get_voicebank_details()
            llm_tools = self._with_voicebank_enum(self._llm_tools, voicebank_ids)
            system_prompt = build_system_prompt(
                llm_tools,
                score_available,
                voicebank_ids,
                score_summary=snapshot.get("score_summary"),
                voicebank_details=voicebank_details,
            )
            text = await asyncio.to_thread(
                self._llm_client.generate, system_prompt, history
            )
        except RuntimeError as exc:
            self._logger.warning("llm_call_failed error=%s", exc)
            message = str(exc)
            if "HTTP error 429" in message or "RESOURCE_EXHAUSTED" in message:
                return None, "Gemini API usage limit exceeded. Please try again tomorrow."
            return None, "LLM request failed. Please try again."
        response = parse_llm_response(text)
        if response is None:
            return None, "LLM returned an invalid response. Please try again."
        return response, None

    async def _render_tool_followup(
        self, snapshot: Dict[str, Any], tool_summary: str
    ) -> str:
        """Ask the LLM to turn a tool summary into a user-facing response."""
        if self._llm_client is None:
            return tool_summary
        history = list(snapshot.get("history", []))
        history.append(
            {
                "role": "user",
                "content": f"{TOOL_RESULT_PREFIX}{tool_summary}",
            }
        )
        try:
            voicebank_ids = await self._get_voicebank_ids()
            voicebank_details = await self._get_voicebank_details()
            llm_tools = self._with_voicebank_enum(self._llm_tools, voicebank_ids)
            system_prompt = build_system_prompt(
                llm_tools,
                score_available=True,
                voicebank_ids=voicebank_ids,
                score_summary=snapshot.get("score_summary"),
                voicebank_details=voicebank_details,
            )
            text = await asyncio.to_thread(self._llm_client.generate, system_prompt, history)
        except RuntimeError as exc:
            self._logger.warning("llm_followup_failed error=%s", exc)
            return tool_summary
        response = parse_llm_response(text)
        if response is None or not response.final_message:
            return tool_summary
        cleaned = response.final_message.strip()
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                payload = json.loads(cleaned)
            except json.JSONDecodeError:
                return response.final_message
            estimated_credits = payload.get("estimated_credits")
            estimated_seconds = payload.get("estimated_seconds")
            current_balance = payload.get("current_balance")
            balance_after = payload.get("balance_after")
            if estimated_credits is not None and estimated_seconds is not None:
                return (
                    f"Estimated duration: ~{estimated_seconds} seconds\n"
                    f"Estimated cost: {estimated_credits} credits\n"
                    f"Your balance: {current_balance} credits\n"
                    f"Balance after generation: {balance_after} credits\n\n"
                    "Would you like me to proceed?"
                )
        return response.final_message

    async def _get_voicebank_ids(self) -> List[str]:
        """Return cached voicebank IDs or fetch them from the MCP server."""
        if self._cached_voicebank_ids is not None:
            return self._cached_voicebank_ids
        try:
            voicebanks = await asyncio.to_thread(self._router.call_tool, "list_voicebanks", {})
        except Exception as exc:
            self._logger.warning("voicebank_list_failed error=%s", exc)
            return []
        ids = []
        if isinstance(voicebanks, list):
            for entry in voicebanks:
                if isinstance(entry, dict):
                    voicebank_id = entry.get("id")
                    if isinstance(voicebank_id, str) and voicebank_id:
                        ids.append(voicebank_id)
        ids = sorted(set(ids))
        self._cached_voicebank_ids = ids
        return ids

    async def _get_voicebank_details(self) -> List[Dict[str, Any]]:
        """Return cached voicebank metadata for LLM prompts."""
        if self._cached_voicebank_details is not None:
            return self._cached_voicebank_details
        details: List[Dict[str, Any]] = []
        try:
            voicebanks = await asyncio.to_thread(self._router.call_tool, "list_voicebanks", {})
        except Exception as exc:
            self._logger.warning("voicebank_list_failed error=%s", exc)
            self._cached_voicebank_details = details
            return details
        if isinstance(voicebanks, list):
            for entry in voicebanks:
                if not isinstance(entry, dict):
                    continue
                voicebank_id = entry.get("id")
                if not isinstance(voicebank_id, str) or not voicebank_id:
                    continue
                try:
                    info = await asyncio.to_thread(
                        self._router.call_tool, "get_voicebank_info", {"voicebank": voicebank_id}
                    )
                except Exception as exc:
                    self._logger.warning("voicebank_info_failed id=%s error=%s", voicebank_id, exc)
                    continue
                if not isinstance(info, dict):
                    continue
                details.append(
                    {
                        "id": voicebank_id,
                        "name": info.get("name") or entry.get("name") or voicebank_id,
                        "voice_colors": info.get("voice_colors", []),
                        "default_voice_color": info.get("default_voice_color"),
                    }
                )
        self._cached_voicebank_details = details
        return details

    def _with_voicebank_enum(
        self, tools: List[Dict[str, Any]], voicebank_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """Inject a voicebank enum into the synthesize tool schema."""
        if not voicebank_ids:
            return tools
        updated: List[Dict[str, Any]] = []
        for tool in tools:
            if tool.get("name") != "synthesize":
                updated.append(tool)
                continue
            tool_copy = copy.deepcopy(tool)
            schema = tool_copy.get("inputSchema")
            if isinstance(schema, dict):
                props = schema.get("properties")
                if isinstance(props, dict) and isinstance(props.get("voicebank"), dict):
                    voicebank_schema = dict(props["voicebank"])
                    voicebank_schema["enum"] = voicebank_ids
                    props = dict(props)
                    props["voicebank"] = voicebank_schema
                    schema = dict(schema)
                    schema["properties"] = props
                    tool_copy["inputSchema"] = schema
            updated.append(tool_copy)
        return updated

    async def _execute_tool_calls(
        self,
        session_id: str,
        score: Dict[str, Any],
        tool_calls: List[ToolCall],
        *,
        user_id: str,
        session_files: Dict[str, Any],
        score_summary: Optional[Dict[str, Any]],
    ) -> "ToolExecutionResult":
        """Execute allowed tool calls and update session state."""
        current_score = score
        audio_response: Optional[Dict[str, Any]] = None
        estimate_handled = False
        followup_prompt: Optional[str] = None
        credit_estimate: Optional[Dict[str, Any]] = None
        raw_estimate = session_files.get("credit_estimate") if isinstance(session_files, dict) else None
        if isinstance(raw_estimate, str) and raw_estimate.strip():
            try:
                credit_estimate = json.loads(raw_estimate)
            except json.JSONDecodeError:
                credit_estimate = None
        for call in tool_calls:
            self._logger.debug(
                "mcp_call_args session=%s tool=%s arguments=%s",
                session_id,
                call.name,
                summarize_payload(call.arguments),
            )
            if call.name not in self._llm_tool_allowlist:
                self._logger.warning("llm_tool_not_allowed tool=%s", call.name)
                continue
            if call.name == "modify_score":
                # Apply score edits and persist updated score.
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
                if not credit_estimate:
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={
                            "type": "chat_text",
                            "message": "Please request an estimate first so I can confirm credit usage before rendering."
                        }
                    )
                # Check for overdraft before even starting
                from src.backend.credits import get_or_create_credits, reserve_credits
                user_credits = get_or_create_credits(user_id, "user@example.com")
                if user_credits.overdrafted:
                    return ToolExecutionResult(
                        score=current_score, 
                        audio_response={
                            "type": "chat_text", 
                            "message": "Your account is locked due to a negative credit balance. Please join the waiting list for more credits."
                        }
                    )
                if user_credits.is_expired:
                    return ToolExecutionResult(
                        score=current_score, 
                        audio_response={
                            "type": "chat_text", 
                            "message": "Your free trial credits have expired."
                        }
                    )

                # Launch an async synthesis job.
                synth_args = dict(call.arguments)
                synth_args.pop("score", None)
                
                est_credits = credit_estimate.get("estimated_credits")
                if not isinstance(est_credits, int) or est_credits <= 0:
                    from src.mcp.handlers import _calculate_score_duration
                    from src.backend.credits import estimate_credits
                    duration = _calculate_score_duration(current_score)
                    est_credits = estimate_credits(duration)
                
                job_id = uuid.uuid4().hex
                reserved = await asyncio.to_thread(
                    reserve_credits,
                    user_id,
                    job_id,
                    est_credits,
                    self._settings.session_ttl_seconds,
                )
                if not reserved:
                    return ToolExecutionResult(
                        score=current_score, 
                        audio_response={
                            "type": "chat_text", 
                            "message": f"Insufficient credits. This render requires ~{est_credits} credits, but you only have {user_credits.available_balance} available."
                        }
                    )
                await self._sessions.set_metadata(session_id, "credit_estimate", "")

                audio_response = await self._start_synthesis_job(
                    session_id, current_score, synth_args, user_id=user_id, job_id=job_id
                )
            if call.name == "estimate_credits":
                est_args = dict(call.arguments)
                est_args["score"] = current_score
                est_args["uid"] = user_id
                if isinstance(score_summary, dict):
                    duration_seconds = score_summary.get("duration_seconds")
                    if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
                        est_args["duration_seconds"] = float(duration_seconds)
                self._logger.info("mcp_call tool=estimate_credits session=%s", session_id)
                est_result = await asyncio.to_thread(
                    self._router.call_tool, "estimate_credits", est_args
                )
                estimate_payload = {
                    "estimated_credits": est_result.get("estimated_credits"),
                    "estimated_seconds": est_result.get("estimated_seconds"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                await self._sessions.set_metadata(
                    session_id, "credit_estimate", json.dumps(estimate_payload)
                )
                credit_estimate = estimate_payload
                followup_prompt = json.dumps(est_result, sort_keys=True)
                estimate_handled = True
        if estimate_handled:
            return ToolExecutionResult(
                score=current_score,
                audio_response=audio_response,
                followup_prompt=followup_prompt,
            )
        return ToolExecutionResult(score=current_score, audio_response=audio_response)


@dataclass(frozen=True)
class ToolExecutionResult:
    """Return value for tool execution: optional audio plus score."""
    score: Dict[str, Any]
    audio_response: Optional[Dict[str, Any]]
    followup_prompt: Optional[str] = None


def _job_storage_input_path(user_id: str, session_id: str, job_id: str, suffix: str) -> str:
    """Build the storage path for job input files."""
    safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return f"sessions/{user_id}/{session_id}/jobs/{job_id}/input{safe_suffix}"


def _job_storage_output_path(
    user_id: str, session_id: str, job_id: str, audio_format: str
) -> str:
    """Build the storage path for job output audio."""
    extension = "mp3" if audio_format.lower() == "mp3" else "wav"
    return f"sessions/{user_id}/{session_id}/jobs/{job_id}/output.{extension}"


def _ensure_job_input_storage(
    bucket_name: str,
    input_path: Optional[str],
    storage_input_path: Optional[str],
    job_input_storage_path: str,
    project_root: Path,
) -> None:
    """Ensure the job input file exists in storage, copying or uploading."""
    if storage_input_path:
        copy_blob(bucket_name, storage_input_path, job_input_storage_path)
        return
    if not input_path:
        raise RuntimeError("Missing input path for job storage copy.")
    local_path = (project_root / input_path).resolve()
    if not local_path.exists():
        raise RuntimeError("Local input file not found for storage copy.")
    upload_file(bucket_name, local_path, job_input_storage_path, "application/xml")
