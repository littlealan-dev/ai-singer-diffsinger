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

from src.backend.config import Settings
from src.backend.job_store import build_progress_payload
from src.backend.llm_client import LlmClient
from src.backend.llm_prompt import LlmResponse, ToolCall, build_system_prompt, parse_llm_response
from src.backend.mcp_client import McpRouter
from src.backend.job_store import JobStore
from src.backend.session import SessionStore
from src.backend.storage_client import copy_blob, upload_file
from src.api.voice_parts import (
    build_preprocessing_required_action,
    synthesize_preflight_action_required,
)
from src.mcp.logging_utils import clear_log_context, get_logger, set_log_context, summarize_payload
from src.mcp.tools import list_tools

TOOL_RESULT_PREFIX = "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"
LLM_ERROR_FALLBACK = "LLM request failed. Please try again."
MAX_INTERNAL_TOOL_REPAIRS = 3
REVIEW_PENDING_KEY = "_preprocess_review_pending"
MISSING_ORIGINAL_SCORE_MESSAGE = (
    "Session is missing the original parsed score baseline required for preprocessing. "
    "Please re-upload the MusicXML file."
)


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
        self._llm_tool_allowlist = {"reparse", "preprocess_voice_parts", "synthesize"}
        self._llm_tools = list_tools(self._llm_tool_allowlist)
        self._synthesis_tasks: Dict[str, asyncio.Task] = {}
        self._chat_locks_guard = asyncio.Lock()
        self._chat_locks: Dict[str, asyncio.Lock] = {}

    async def handle_chat(
        self,
        session_id: str,
        message: str,
        *,
        user_id: str,
        user_email: str,
    ) -> Dict[str, Any]:
        """Handle a chat message and return a response payload."""
        chat_lock = await self._get_chat_lock(session_id)
        if chat_lock.locked():
            return {
                "type": "chat_text",
                "message": (
                    "I am still processing your previous request. "
                    "Please wait for it to complete before sending another message."
                ),
            }
        async with chat_lock:
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
                return {"type": "chat_error", "message": response_message}
            if llm_response is not None:
                response_message = self._merge_thought_summary(
                    llm_response.final_message or response_message,
                    llm_response.thought_summary,
                    llm_response.tool_calls,
                )
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
                        "thought_summary": summarize_payload(llm_response.thought_summary),
                    },
                )
                if llm_response.thought_summary and any(
                    call.name == "preprocess_voice_parts" for call in llm_response.tool_calls
                ):
                    self._logger.debug(
                        "chat_llm_preprocess_thought_summary session=%s thought_summary=%s",
                        session_id,
                        llm_response.thought_summary,
                    )
            if llm_response is not None:
                # Execute tool calls and allow bounded internal repair loops.
                include_score = llm_response.include_score
                response_message = self._merge_thought_summary(
                    llm_response.final_message or response_message,
                    llm_response.thought_summary,
                    llm_response.tool_calls,
                )
                thought_block_for_response = self._format_thought_summary_block(
                    llm_response.thought_summary,
                    llm_response.tool_calls,
                )
                pending_calls = list(llm_response.tool_calls)
                working_score = current_score["score"]
                score_summary = snapshot.get("score_summary") if isinstance(snapshot, dict) else None
                followup_prompt: Optional[str] = None
                repairs_used = 0
                response: Dict[str, Any] = {"type": "chat_text", "message": response_message}

                while True:
                    tool_result = await self._execute_tool_calls(
                        session_id,
                        working_score,
                        pending_calls,
                        user_id=user_id,
                        score_summary=score_summary,
                        user_email=user_email,
                    )
                    working_score = tool_result.score
                    followup_prompt = tool_result.followup_prompt
                    response = tool_result.audio_response or {
                        "type": "chat_text",
                        "message": response_message,
                    }
                    if followup_prompt is None:
                        if response.get("review_required"):
                            review_message = response.get("message") or (
                                "Preprocessing completed. Please review the derived score and "
                                "reply 'proceed' to start audio generation, or describe revisions."
                            )
                            thought_block = thought_block_for_response or self._extract_thought_summary_block(
                                response_message
                            )
                            if thought_block:
                                response["message"] = f"{review_message}\n\n{thought_block}"
                            else:
                                response["message"] = review_message
                        elif response_message:
                            response["message"] = response_message
                        break

                    if self._tool_payload_has_error(followup_prompt):
                        response["suppress_selector"] = True

                    followup_response, followup_error = await self._decide_followup_with_llm(
                        snapshot, followup_prompt, working_score
                    )
                    if followup_error:
                        await self._sessions.append_history(session_id, "assistant", followup_error)
                        return {"type": "chat_error", "message": followup_error}

                    if followup_response is None:
                        response["message"] = followup_prompt
                        break

                    self._logger.debug(
                        "chat_llm_followup session=%s response=%s",
                        session_id,
                        {
                            "tool_calls": [
                                {
                                    "name": call.name,
                                    "arguments": summarize_payload(call.arguments),
                                }
                                for call in followup_response.tool_calls
                            ],
                            "final_message": followup_response.final_message,
                            "include_score": followup_response.include_score,
                            "thought_summary": summarize_payload(followup_response.thought_summary),
                        },
                    )
                    if followup_response.thought_summary and any(
                        call.name == "preprocess_voice_parts"
                        for call in followup_response.tool_calls
                    ):
                        self._logger.debug(
                            "chat_llm_followup_preprocess_thought_summary session=%s thought_summary=%s",
                            session_id,
                            followup_response.thought_summary,
                        )
                    include_score = include_score or followup_response.include_score
                    response_message = self._merge_thought_summary(
                        followup_response.final_message or response_message,
                        followup_response.thought_summary,
                        followup_response.tool_calls,
                    )
                    thought_block_for_response = self._format_thought_summary_block(
                        followup_response.thought_summary,
                        followup_response.tool_calls,
                    ) or thought_block_for_response

                    if not followup_response.tool_calls:
                        response["message"] = self._format_followup_message_text(
                            response_message
                        )
                        break

                    if repairs_used >= MAX_INTERNAL_TOOL_REPAIRS:
                        response["message"] = (
                            f"I couldn't complete preprocessing after {MAX_INTERNAL_TOOL_REPAIRS} repair attempts. "
                            "Please revise the request or plan details."
                        )
                        break

                    repairs_used += 1
                    pending_calls = list(followup_response.tool_calls)

                if include_score or response.get("review_required"):
                    updated_snapshot = await self._sessions.get_snapshot(session_id, user_id)
                    updated_score = updated_snapshot.get("current_score")
                    if updated_score is not None:
                        response["current_score"] = updated_score
                await self._sessions.append_history(
                    session_id, "assistant", str(response.get("message", ""))
                )
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

    async def _get_chat_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session chat lock, creating it lazily."""
        async with self._chat_locks_guard:
            lock = self._chat_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._chat_locks[session_id] = lock
            return lock

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
        # Verse selection is resolved at parse/reparse stage.
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
        if not isinstance(synth_result, dict):
            raise RuntimeError(
                f"Synthesize returned non-object result: {type(synth_result).__name__}"
            )
        if "waveform" not in synth_result or "sample_rate" not in synth_result:
            self._logger.warning(
                "synthesize_non_audio_result session=%s result=%s",
                session_id,
                summarize_payload(synth_result),
            )
            status = str(synth_result.get("status") or "").strip()
            reason = str(synth_result.get("reason") or "").strip()
            message = str(synth_result.get("message") or "").strip()
            hint = f" status={status}" if status else ""
            if reason:
                hint += f" reason={reason}"
            if message:
                hint += f" message={message}"
            raise RuntimeError(
                "Synthesize did not return audio waveform."
                + (hint if hint else f" result={summarize_payload(synth_result)}")
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
        requested_verse = self._normalize_verse_number(verse_number)
        if requested_verse is not None:
            selected_verse = self._score_selected_verse_number(score)
            if selected_verse != requested_verse:
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

    def _normalize_verse_number(self, raw_value: Optional[object]) -> Optional[str]:
        """Normalize optional verse selection to a non-empty string."""
        if raw_value is None:
            return None
        text = str(raw_value).strip()
        return text or None

    def _score_selected_verse_number(self, score: Dict[str, Any]) -> Optional[str]:
        """Return selected verse stored on score payload, if present."""
        selected = score.get("selected_verse_number")
        return self._normalize_verse_number(selected)

    def _is_reparse_noop(
        self,
        score: Dict[str, Any],
        *,
        part_id: Optional[str],
        part_index: Optional[int],
        verse_number: Optional[object],
        expand_repeats: bool,
    ) -> bool:
        """Return True when reparse request would not change current score context."""
        if expand_repeats:
            return False
        if part_id is not None or part_index is not None:
            return False
        requested_verse = self._normalize_verse_number(verse_number)
        selected_verse = self._score_selected_verse_number(score)
        if requested_verse is None or selected_verse is None:
            return False
        return requested_verse == selected_verse

    def _score_has_preprocessed_context(self, score: Dict[str, Any]) -> bool:
        """Return True when score already contains preprocess-derived context."""
        if self._score_has_review_pending(score):
            return True
        transforms = score.get("voice_part_transforms")
        if isinstance(transforms, dict) and bool(transforms):
            return True
        parts = score.get("parts")
        summary = score.get("score_summary")
        summary_parts = summary.get("parts") if isinstance(summary, dict) else None
        if isinstance(parts, list) and isinstance(summary_parts, list):
            return len(parts) > len(summary_parts)
        return False

    def _build_verse_change_requires_repreprocess_action(
        self,
        *,
        score: Dict[str, Any],
        requested_verse_number: str,
        selected_verse_number: Optional[str],
        part_index: int,
        reparse_applied: bool,
        reparsed_selected_verse_number: Optional[str],
    ) -> Dict[str, Any]:
        """Build action_required payload when verse changed after preprocess."""
        diagnostics = {
            "requested_verse_number": requested_verse_number,
            "selected_verse_number": selected_verse_number,
            "preprocessed_score_detected": self._score_has_preprocessed_context(score),
            "review_pending": self._score_has_review_pending(score),
            "reparse_applied": bool(reparse_applied),
            "reparsed_selected_verse_number": reparsed_selected_verse_number,
            "preprocessed_for_score_fingerprint": (
                score.get(REVIEW_PENDING_KEY, {}).get("preprocessed_for_score_fingerprint")
                if isinstance(score.get(REVIEW_PENDING_KEY), dict)
                else None
            ),
        }
        return build_preprocessing_required_action(
            part_index=part_index,
            reason="verse_change_requires_repreprocess",
            failed_validation_rules=[
                "verse_lock.requested_verse_differs_from_selected_verse",
                "workflow_restart.reparse_and_repreprocess_required",
            ],
            diagnostics=diagnostics,
            message=(
                "The requested verse differs from the currently loaded score verse. "
                "Call reparse with the requested verse first. If the target requires derived "
                "voice-part preprocessing, run preprocess_voice_parts before synthesis."
            ),
        )

    async def _reparse_score(
        self,
        session_id: str,
        *,
        part_id: Optional[str],
        part_index: Optional[int],
        verse_number: Optional[object],
        expand_repeats: bool = False,
        user_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Re-parse the current MusicXML file with new selection filters."""
        snapshot = await self._sessions.get_snapshot(session_id, user_id)
        files = snapshot.get("files") or {}
        file_path = files.get("musicxml_path")
        if not isinstance(file_path, str) or not file_path:
            return None
        parse_args: Dict[str, Any] = {"file_path": file_path, "expand_repeats": bool(expand_repeats)}
        if part_id is not None:
            parse_args["part_id"] = part_id
        elif part_index is not None:
            parse_args["part_index"] = part_index
        if verse_number is not None:
            parse_args["verse_number"] = verse_number
        result = await asyncio.to_thread(self._router.call_tool, "parse_score", parse_args)
        if not isinstance(result, dict):
            return None
        score_summary = result.get("score_summary") if isinstance(result, dict) else None
        score = dict(result)
        score.pop("score_summary", None)
        await self._sessions.set_score_summary(
            session_id, score_summary if isinstance(score_summary, dict) else None
        )
        await self._sessions.set_original_score(session_id, score)
        await self._sessions.set_score(session_id, score)
        return score

    def _resolve_llm_planning_score(
        self,
        snapshot: Dict[str, Any],
        current_score: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Return the score context the LLM should use for preprocess planning."""
        if not isinstance(current_score, dict):
            return current_score
        original_score = snapshot.get("original_score")
        if isinstance(original_score, dict):
            return original_score
        raise ValueError(MISSING_ORIGINAL_SCORE_MESSAGE)

    async def _resolve_preprocess_score(
        self,
        session_id: str,
        *,
        user_id: Optional[str],
    ) -> Dict[str, Any]:
        """Return the score baseline to use for preprocess execution."""
        snapshot = await self._sessions.get_snapshot(session_id, user_id)
        original_score = snapshot.get("original_score")
        if isinstance(original_score, dict):
            self._logger.info(
                "preprocess_baseline_ready session=%s source_musicxml_path=%s selected_verse=%s",
                session_id,
                original_score.get("source_musicxml_path"),
                original_score.get("selected_verse_number"),
            )
            return original_score
        raise ValueError(MISSING_ORIGINAL_SCORE_MESSAGE)

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

    def _format_llm_error(self, exc: RuntimeError) -> str:
        """Return a user-facing LLM error message."""
        message = str(exc).strip()
        if not message:
            return LLM_ERROR_FALLBACK
        json_start = message.find("{")
        json_message = message if json_start == 0 else message[json_start:] if json_start != -1 else ""
        if not json_message:
            return LLM_ERROR_FALLBACK
        try:
            payload = json.loads(json_message)
        except json.JSONDecodeError:
            return LLM_ERROR_FALLBACK
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                detail = error.get("message")
                if code is not None and detail:
                    return f"LLM error {code}: {detail}"
        return LLM_ERROR_FALLBACK

    def _is_llm_error_message(self, message: str) -> bool:
        """Return True if the message is an LLM error string."""
        cleaned = message.strip()
        return cleaned == LLM_ERROR_FALLBACK or cleaned.startswith("LLM error ")

    def _tool_payload_has_error(self, payload: str) -> bool:
        """Return True if the tool payload JSON includes an error object."""
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return False
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            return True
        return False

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
            current_score = snapshot.get("current_score")
            planning_score = None
            voice_part_signals = None
            preprocess_mapping_context = None
            if isinstance(current_score, dict):
                score_payload = current_score.get("score")
                planning_score = self._resolve_llm_planning_score(snapshot, score_payload)
                if isinstance(planning_score, dict):
                    voice_part_signals = planning_score.get("voice_part_signals")
                    preprocess_mapping_context = self._build_preprocess_mapping_context(
                        score_payload,
                        score_summary=snapshot.get("score_summary"),
                    )
            system_prompt = build_system_prompt(
                llm_tools,
                score_available,
                voicebank_ids,
                score_summary=snapshot.get("score_summary"),
                voice_part_signals=voice_part_signals,
                preprocess_mapping_context=preprocess_mapping_context,
                voicebank_details=voicebank_details,
            )
            text = await asyncio.to_thread(
                self._llm_client.generate, system_prompt, history
            )
        except ValueError as exc:
            self._logger.warning("llm_planning_context_failed error=%s", exc)
            return None, str(exc)
        except RuntimeError as exc:
            self._logger.warning("llm_call_failed error=%s", exc)
            return None, self._format_llm_error(exc)
        except Exception as exc:
            self._logger.exception("llm_call_unexpected error=%s", exc)
            return None, LLM_ERROR_FALLBACK
        response = parse_llm_response(text)
        if response is None:
            return None, "LLM returned an invalid response. Please try again."
        return response, None

    async def _render_tool_followup(
        self, snapshot: Dict[str, Any], tool_summary: str
    ) -> str:
        """Ask the LLM to turn a tool summary into a user-facing response."""
        response, error = await self._decide_followup_with_llm(snapshot, tool_summary)
        if error:
            return error
        if response is None or not response.final_message:
            return tool_summary
        return self._format_followup_message_text(response.final_message)

    async def _decide_followup_with_llm(
        self,
        snapshot: Dict[str, Any],
        tool_summary: str,
        current_score: Optional[Dict[str, Any]] = None,
    ) -> tuple[Optional[LlmResponse], Optional[str]]:
        """Ask the LLM to interpret tool output and optionally produce further tool calls."""
        if self._llm_client is None:
            return None, tool_summary
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
            planning_score = self._resolve_llm_planning_score(snapshot, current_score)
            voice_part_signals = (
                planning_score.get("voice_part_signals")
                if isinstance(planning_score, dict)
                else None
            )
            preprocess_mapping_context = (
                self._build_preprocess_mapping_context(
                    current_score,
                    score_summary=snapshot.get("score_summary"),
                )
                if isinstance(current_score, dict)
                else None
            )
            system_prompt = build_system_prompt(
                llm_tools,
                score_available=True,
                voicebank_ids=voicebank_ids,
                score_summary=snapshot.get("score_summary"),
                voice_part_signals=voice_part_signals,
                preprocess_mapping_context=preprocess_mapping_context,
                voicebank_details=voicebank_details,
            )
            text = await asyncio.to_thread(self._llm_client.generate, system_prompt, history)
        except ValueError as exc:
            self._logger.warning("llm_followup_context_failed error=%s", exc)
            return None, str(exc)
        except RuntimeError as exc:
            self._logger.warning("llm_followup_failed error=%s", exc)
            return None, self._format_llm_error(exc)
        except Exception as exc:
            self._logger.exception("llm_followup_unexpected error=%s", exc)
            return None, LLM_ERROR_FALLBACK
        response = parse_llm_response(text)
        if response is None:
            return None, tool_summary
        return response, None

    def _format_followup_message_text(self, message: str) -> str:
        """Format followup text payloads into a user-facing string."""
        cleaned = message.strip()
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                payload = json.loads(cleaned)
            except json.JSONDecodeError:
                return message
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
        return message

    def _merge_thought_summary(
        self, message: str, thought_summary: str, tool_calls: List[ToolCall]
    ) -> str:
        """Append Gemini thought summary to preprocess messages for dev inspection."""
        if not thought_summary:
            return message
        if not any(call.name == "preprocess_voice_parts" for call in tool_calls):
            return message
        cleaned_message = message.strip()
        cleaned_summary = thought_summary.strip()
        if not cleaned_summary:
            return message
        if not cleaned_message:
            return f"Thought summary:\n{cleaned_summary}"
        return f"{cleaned_message}\n\nThought summary:\n{cleaned_summary}"

    def _extract_thought_summary_block(self, message: str) -> str:
        """Return the appended thought summary block when present."""
        marker = "\n\nThought summary:\n"
        if marker in message:
            return "Thought summary:\n" + message.split(marker, 1)[1].strip()
        if message.startswith("Thought summary:\n"):
            return message.strip()
        return ""

    def _format_thought_summary_block(self, thought_summary: str, tool_calls: List[ToolCall]) -> str:
        """Return a standalone thought-summary block for preprocess messages."""
        if not thought_summary:
            return ""
        if not any(call.name == "preprocess_voice_parts" for call in tool_calls):
            return ""
        cleaned_summary = thought_summary.strip()
        if not cleaned_summary:
            return ""
        return f"Thought summary:\n{cleaned_summary}"

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
        score_summary: Optional[Dict[str, Any]],
        user_email: str,
    ) -> "ToolExecutionResult":
        """Execute allowed tool calls and update session state."""
        current_score = score
        audio_response: Optional[Dict[str, Any]] = None
        preprocess_completed_this_batch = False
        reparse_completed_this_batch = False
        reparse_selected_verse: Optional[str] = None
        reparse_noop_this_batch = False
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
            if call.name == "reparse":
                reparse_part_id = call.arguments.get("part_id")
                reparse_part_index = call.arguments.get("part_index")
                reparse_verse_number = call.arguments.get("verse_number")
                reparse_expand_repeats = bool(call.arguments.get("expand_repeats", False))
                if self._is_reparse_noop(
                    current_score,
                    part_id=reparse_part_id,
                    part_index=reparse_part_index,
                    verse_number=reparse_verse_number,
                    expand_repeats=reparse_expand_repeats,
                ):
                    reparse_completed_this_batch = True
                    reparse_noop_this_batch = True
                    reparse_selected_verse = self._score_selected_verse_number(current_score)
                    self._logger.info(
                        "reparse_noop session=%s selected_verse=%s requested_verse=%s",
                        session_id,
                        reparse_selected_verse,
                        self._normalize_verse_number(reparse_verse_number),
                    )
                    continue
                reparsed_score = await self._reparse_score(
                    session_id,
                    part_id=reparse_part_id,
                    part_index=reparse_part_index,
                    verse_number=reparse_verse_number,
                    expand_repeats=reparse_expand_repeats,
                    user_id=user_id,
                )
                if isinstance(reparsed_score, dict):
                    current_score = reparsed_score
                    reparse_completed_this_batch = True
                    reparse_selected_verse = self._score_selected_verse_number(current_score)
                    self._logger.info(
                        "reparse_ready session=%s selected_verse=%s",
                        session_id,
                        reparse_selected_verse,
                    )
                else:
                    followup_prompt = json.dumps(
                        {
                            "error": {
                                "type": "reparse_failed",
                                "message": (
                                    "Unable to reparse the current score context. "
                                    "Please retry reparse with a valid part/verse selection."
                                ),
                            }
                        },
                        sort_keys=True,
                    )
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": ""},
                        followup_prompt=followup_prompt,
                    )
                continue
            if call.name == "preprocess_voice_parts":
                preprocess_args = dict(call.arguments)
                try:
                    preprocess_score = await self._resolve_preprocess_score(
                        session_id,
                        user_id=user_id,
                    )
                except ValueError as exc:
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": str(exc)},
                    )
                preprocess_args["score"] = preprocess_score
                self._logger.info("mcp_call tool=preprocess_voice_parts session=%s", session_id)
                result = await asyncio.to_thread(
                    self._router.call_tool, "preprocess_voice_parts", preprocess_args
                )
                if not isinstance(result, dict):
                    continue
                self._logger.debug(
                    "preprocess_result session=%s result=%s",
                    session_id,
                    summarize_payload(result),
                )
                if result.get("status") in {"ready", "ready_with_warnings"} and isinstance(
                    result.get("score"), dict
                ):
                    current_score = self._mark_review_pending(result["score"], result)
                    await self._sessions.set_score(session_id, current_score)
                    mapping_context = self._build_preprocess_mapping_context(
                        current_score,
                        score_summary=score_summary,
                    )
                    self._logger.info(
                        "preprocess_ready session=%s status=%s derived_targets=%s",
                        session_id,
                        result.get("status"),
                        self._summarize_derived_targets(mapping_context),
                    )
                    audio_response = self._build_review_required_response(result)
                    preprocess_completed_this_batch = True
                    continue
                if result.get("status") == "action_required":
                    self._logger.info(
                        "preprocess_action_required session=%s reason=%s failed_rules=%s diagnostics=%s",
                        session_id,
                        result.get("reason"),
                        result.get("failed_validation_rules"),
                        summarize_payload(result.get("diagnostics")),
                    )
                    if result.get("action") == "plan_lint_failed":
                        self._logger.debug(
                            "preprocess_plan_lint_failed session=%s lint_findings=%s",
                            session_id,
                            json.dumps(result.get("lint_findings", []), ensure_ascii=True, sort_keys=True),
                        )
                    followup_prompt = json.dumps(result, sort_keys=True)
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": ""},
                        followup_prompt=followup_prompt,
                    )
                continue
            if call.name == "synthesize":
                if self._score_has_review_pending(current_score):
                    if preprocess_completed_this_batch:
                        # Review gate: do not allow synth in same tool batch where preprocess just succeeded.
                        return ToolExecutionResult(
                            score=current_score,
                            audio_response=self._build_review_required_response(None),
                        )
                    # Review progression is LLM-driven; synth tool call implies user-approved proceed.
                    current_score = self._clear_review_pending(current_score)
                    await self._sessions.set_score(session_id, current_score)
                # Check for overdraft before even starting
                from src.backend.credits import get_or_create_credits, reserve_credits
                user_credits = get_or_create_credits(user_id, user_email)
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
                requested_verse_number = self._normalize_verse_number(
                    synth_args.get("verse_number")
                )
                synth_args.pop("verse_number", None)
                selected_verse_number = self._score_selected_verse_number(current_score)
                if (
                    requested_verse_number is not None
                    and requested_verse_number != selected_verse_number
                ):
                    action_required = self._build_verse_change_requires_repreprocess_action(
                        score=current_score,
                        requested_verse_number=requested_verse_number,
                        selected_verse_number=selected_verse_number,
                        part_index=self._resolve_synthesize_part_index(
                            current_score,
                            part_id=synth_args.get("part_id"),
                            part_index=synth_args.get("part_index"),
                        ),
                        reparse_applied=False,
                        reparsed_selected_verse_number=selected_verse_number,
                    )
                    self._logger.info(
                        "synthesize_action_required_verse_change session=%s reason=%s diagnostics=%s",
                        session_id,
                        action_required.get("reason"),
                        summarize_payload(action_required.get("diagnostics")),
                    )
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": ""},
                        followup_prompt=json.dumps(action_required, sort_keys=True),
                    )
                mapping_context = self._build_preprocess_mapping_context(
                    current_score,
                    score_summary=score_summary,
                )
                self._logger.info(
                    "synthesize_request session=%s args=%s derived_targets=%s",
                    session_id,
                    summarize_payload(synth_args),
                    self._summarize_derived_targets(mapping_context),
                )

                # Stateless precheck: block complex raw parts before reserving credits.
                precheck_part_index = self._resolve_synthesize_part_index(
                    current_score,
                    part_id=synth_args.get("part_id"),
                    part_index=synth_args.get("part_index"),
                )
                precheck = synthesize_preflight_action_required(
                    current_score,
                    part_index=precheck_part_index,
                )
                if precheck is not None:
                    self._logger.info(
                        "synthesize_precheck_action_required session=%s part_index=%s reason=%s failed_rules=%s diagnostics=%s",
                        session_id,
                        precheck_part_index,
                        precheck.get("reason"),
                        precheck.get("failed_validation_rules"),
                        summarize_payload(precheck.get("diagnostics")),
                    )
                    followup_prompt = json.dumps(precheck, sort_keys=True)
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": ""},
                        followup_prompt=followup_prompt,
                    )
                
                from src.mcp.handlers import _calculate_score_duration
                from src.backend.credits import estimate_credits
                duration_seconds = None
                if isinstance(score_summary, dict):
                    duration_seconds = score_summary.get("duration_seconds")
                if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
                    duration_seconds = _calculate_score_duration(current_score)
                est_credits = estimate_credits(float(duration_seconds))
                
                job_id = uuid.uuid4().hex
                reserved = await asyncio.to_thread(
                    reserve_credits,
                    user_id,
                    job_id,
                    est_credits,
                    self._settings.session_ttl_seconds,
                )
                if not reserved:
                    followup_prompt = json.dumps(
                        {
                            "error": {
                                "type": "insufficient_credits",
                                "message": (
                                    f"Insufficient credits. This render requires ~{est_credits} credits, "
                                    f"but you only have {user_credits.available_balance} available."
                                ),
                                "estimated_credits": est_credits,
                                "available_credits": user_credits.available_balance,
                            }
                        },
                        sort_keys=True,
                    )
                    return ToolExecutionResult(
                        score=current_score, 
                        audio_response={
                            "type": "chat_text",
                            "message": "",
                        },
                        followup_prompt=followup_prompt,
                    )
                audio_response = await self._start_synthesis_job(
                    session_id, current_score, synth_args, user_id=user_id, job_id=job_id
                )
        # If a reparse succeeded but no downstream tool produced a terminal response in this
        # batch, force one internal follow-up LLM turn so it can continue with preprocess.
        if reparse_completed_this_batch and audio_response is None:
            reparse_prompt = json.dumps(
                {
                    "status": "reparse_ready",
                    "message": (
                        (
                            "Requested verse already matches current score context. "
                            "Reparse was skipped. "
                        )
                        if reparse_noop_this_batch
                        else "Score context has been reparsed for the requested verse. "
                    )
                    + (
                        "Continue with preprocess_voice_parts for this verse before synthesis."
                    ),
                    "selected_verse_number": reparse_selected_verse,
                },
                sort_keys=True,
            )
            return ToolExecutionResult(
                score=current_score,
                audio_response={"type": "chat_text", "message": ""},
                followup_prompt=reparse_prompt,
            )
        return ToolExecutionResult(score=current_score, audio_response=audio_response)

    def _build_review_required_response(
        self, preprocess_result: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Build chat response payload when user review is required before synthesis."""
        payload: Dict[str, Any] = {
            "type": "chat_text",
            "review_required": True,
            "message": (
                "Preprocessing completed. Please review the derived score. "
                "Reply 'proceed' to start audio generation, or describe revisions."
            ),
        }
        if isinstance(preprocess_result, dict):
            payload["preprocess_status"] = preprocess_result.get("status")
            if isinstance(preprocess_result.get("modified_musicxml_path"), str):
                payload["modified_musicxml_path"] = preprocess_result.get(
                    "modified_musicxml_path"
                )
        return payload

    def _mark_review_pending(
        self, score: Dict[str, Any], preprocess_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Return score payload with review-pending marker after successful preprocess."""
        updated = dict(score)
        selected_verse_number = self._score_selected_verse_number(score)
        updated[REVIEW_PENDING_KEY] = {
            "status": preprocess_result.get("status"),
            "modified_musicxml_path": preprocess_result.get("modified_musicxml_path"),
            "preprocessed_for_verse_number": selected_verse_number,
            "preprocessed_for_score_fingerprint": self._score_fingerprint(score),
        }
        return updated

    def _score_fingerprint(self, score: Dict[str, Any]) -> str:
        """Return stable fingerprint for current score selection context."""
        parts = score.get("parts")
        payload = {
            "source_musicxml_path": score.get("source_musicxml_path"),
            "selected_verse_number": self._score_selected_verse_number(score),
            "parts_count": len(parts) if isinstance(parts, list) else None,
            "part_ids": [
                str(part.get("part_id") or "")
                for part in parts
                if isinstance(part, dict)
            ]
            if isinstance(parts, list)
            else [],
        }
        return uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(payload, sort_keys=True)).hex

    def _score_has_review_pending(self, score: Dict[str, Any]) -> bool:
        """Return True when score has pending preprocess review marker."""
        return isinstance(score.get(REVIEW_PENDING_KEY), dict)

    def _clear_review_pending(self, score: Dict[str, Any]) -> Dict[str, Any]:
        """Return score payload without review-pending marker."""
        updated = dict(score)
        updated.pop(REVIEW_PENDING_KEY, None)
        return updated

    def _resolve_synthesize_part_index(
        self,
        score: Dict[str, Any],
        *,
        part_id: Optional[str],
        part_index: Optional[int],
    ) -> int:
        """Resolve part index for synth prechecks."""
        parts = score.get("parts") or []
        if part_id is not None:
            for idx, part in enumerate(parts):
                if part.get("part_id") == part_id:
                    return idx
        if isinstance(part_index, int):
            return part_index
        return 0

    def _build_preprocess_mapping_context(
        self,
        score: Dict[str, Any],
        *,
        score_summary: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Build compact preprocess mapping context for LLM planning."""
        transforms = score.get("voice_part_transforms")
        review = score.get(REVIEW_PENDING_KEY)
        has_transforms = isinstance(transforms, dict) and bool(transforms)
        has_review = isinstance(review, dict) and bool(review)
        if not has_transforms and not has_review:
            return None

        summary_parts = []
        if isinstance(score_summary, dict):
            raw_parts = score_summary.get("parts")
            if isinstance(raw_parts, list):
                summary_parts = [part for part in raw_parts if isinstance(part, dict)]

        def _lookup_source_part(part_index: Optional[int]) -> Dict[str, Any]:
            if not isinstance(part_index, int):
                return {}
            for part in summary_parts:
                if int(part.get("part_index", -1)) == part_index:
                    return {
                        "source_part_index": part_index,
                        "source_part_id": part.get("part_id"),
                        "source_part_name": part.get("part_name"),
                    }
            return {"source_part_index": part_index}

        targets: List[Dict[str, Any]] = []
        seen = set()
        if isinstance(transforms, dict):
            for value in transforms.values():
                if not isinstance(value, dict):
                    continue
                appended_ref = value.get("appended_part_ref")
                if not isinstance(appended_ref, dict):
                    continue
                derived_part_index = appended_ref.get("part_index")
                if not isinstance(derived_part_index, int):
                    continue
                derived_part_id = str(appended_ref.get("part_id") or "").strip()
                derived_part_name = str(appended_ref.get("part_name") or "").strip()
                target_voice_part_id = str(value.get("target_voice_part_id") or "").strip()
                source_part_index = value.get("source_part_index")
                source_voice_part_id = str(value.get("source_voice_part_id") or "").strip()
                key = (
                    derived_part_index,
                    derived_part_id,
                    target_voice_part_id,
                    source_part_index if isinstance(source_part_index, int) else None,
                    source_voice_part_id,
                )
                if key in seen:
                    continue
                seen.add(key)
                entry: Dict[str, Any] = {
                    "derived_part_index": derived_part_index,
                    "derived_part_id": derived_part_id or None,
                    "derived_part_name": derived_part_name or None,
                    "target_voice_part_id": target_voice_part_id or None,
                }
                entry.update(_lookup_source_part(source_part_index if isinstance(source_part_index, int) else None))
                if source_voice_part_id:
                    entry["source_voice_part_id"] = source_voice_part_id
                targets.append(entry)

        targets.sort(
            key=lambda item: (
                int(item.get("derived_part_index", -1)),
                str(item.get("target_voice_part_id") or ""),
                str(item.get("derived_part_id") or ""),
            )
        )
        context: Dict[str, Any] = {
            "original_parse": {
                "score_summary": score_summary if isinstance(score_summary, dict) else None,
                "selected_verse_number": self._score_selected_verse_number(score),
            }
        }
        if has_review:
            context["preprocess"] = {
                "status": review.get("status"),
                "modified_musicxml_path": review.get("modified_musicxml_path"),
                "preprocessed_for_verse_number": review.get("preprocessed_for_verse_number"),
            }
        if targets:
            context["derived_mapping"] = {"targets": targets}
        return context

    def _summarize_derived_targets(
        self, mapping_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Return compact derived mapping summary for logs."""
        if not isinstance(mapping_context, dict):
            return []
        derived_mapping = mapping_context.get("derived_mapping")
        if not isinstance(derived_mapping, dict):
            return []
        targets = derived_mapping.get("targets")
        if not isinstance(targets, list):
            return []
        summary: List[Dict[str, Any]] = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            summary.append(
                {
                    "derived_part_index": target.get("derived_part_index"),
                    "derived_part_id": target.get("derived_part_id"),
                    "target_voice_part_id": target.get("target_voice_part_id"),
                    "source_part_index": target.get("source_part_index"),
                    "source_voice_part_id": target.get("source_voice_part_id"),
                }
            )
        return summary


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
