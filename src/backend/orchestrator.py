from __future__ import annotations

"""Chat orchestration layer that bridges LLM decisions and MCP tools."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple
import asyncio
import logging
import copy
import json
import re
import uuid

from src.backend.config import Settings
from src.backend.credit_retry import retry_credit_op
from src.backend.job_store import build_progress_payload
from src.backend.llm_client import LlmClient
from src.backend.llm_prompt import LlmResponse, ToolCall, build_system_prompt, parse_llm_response
from src.backend.message_catalog import backend_message
from src.backend.mcp_client import McpRouter
from src.backend.job_store import JobStore
from src.backend.session import SessionStore
from src.backend.storage_client import copy_blob, upload_file
from src.api.voice_parts import (
    build_preprocessing_required_action,
    finalize_review_materialization,
    synthesize_preflight_action_required,
)
from src.mcp.logging_utils import clear_log_context, get_logger, set_log_context, summarize_payload
from src.mcp.tools import list_tools

TOOL_RESULT_PREFIX = "Interpret output and respond: <TOOL_OUTPUT_INTERNAL_v1>"
LLM_ERROR_FALLBACK = "LLM request failed. Please try again."
REVIEW_PENDING_KEY = "_preprocess_review_pending"
EXPLICIT_VERSE_METADATA_KEY = "explicit_verse_number"
MISSING_ORIGINAL_SCORE_MESSAGE = (
    "Session is missing the original parsed score baseline required for preprocessing. "
    "Please re-upload the MusicXML file."
)
MAX_OUT_OF_SCOPE_SECTIONS = 10
MAX_SECTION_CHANGE_RATIO = 0.7
MAX_MEASURE_CHANGE_RATIO = 0.8


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
        self._preprocess_tasks: Dict[str, asyncio.Task] = {}
        self._chat_locks_guard = asyncio.Lock()
        self._chat_locks: Dict[str, asyncio.Lock] = {}
        self._settle_fault_injection_remaining: Dict[str, int] = {}
        self._release_fault_injection_remaining: Dict[str, int] = {}

    async def handle_chat(
        self,
        session_id: str,
        message: str,
        *,
        user_id: str,
        user_email: str,
        selection: Optional[Dict[str, Any]] = None,
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
            existing_preprocess = self._preprocess_tasks.get(session_id)
            if existing_preprocess and not existing_preprocess.done():
                return {
                    "type": "chat_text",
                    "message": (
                        "I am still preparing the derived score from your previous request. "
                        "Please wait for it to finish before sending another message."
                    ),
                }
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
            explicit_verse_from_selection = self._normalize_verse_number(
                selection.get("verse_number")
                if isinstance(selection, dict)
                else None
            )
            if explicit_verse_from_selection:
                await self._sessions.set_metadata(
                    session_id,
                    EXPLICIT_VERSE_METADATA_KEY,
                    explicit_verse_from_selection,
                )
                files = snapshot.get("files")
                next_files = dict(files) if isinstance(files, dict) else {}
                next_files[EXPLICIT_VERSE_METADATA_KEY] = explicit_verse_from_selection
                snapshot = dict(snapshot)
                snapshot["files"] = next_files

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
                response_message = self._merge_thought_summary(
                    llm_response.final_message or response_message,
                    llm_response.thought_summary,
                    llm_response.tool_calls,
                )
                if self._should_start_preprocess_job(llm_response.tool_calls):
                    explicit_verse_number = self._normalize_verse_number(
                        (snapshot.get("files") or {}).get(EXPLICIT_VERSE_METADATA_KEY)
                        if isinstance(snapshot.get("files"), dict)
                        else None
                    )
                    requires_verse_selection = self._tool_calls_require_verse_selection(
                        llm_response.tool_calls,
                        score=current_score["score"],
                        score_summary=snapshot.get("score_summary"),
                        explicit_verse_number=explicit_verse_number,
                    )
                    if not requires_verse_selection:
                        await self._sessions.append_history(session_id, "assistant", response_message)
                        return await self._start_preprocess_job(
                            session_id,
                            current_score["score"],
                            llm_response.tool_calls,
                            initial_message=response_message,
                            initial_thought_summary=llm_response.thought_summary,
                            user_id=user_id,
                            user_email=user_email,
                        )

                response = await self._run_llm_tool_workflow(
                    session_id,
                    snapshot,
                    current_score["score"],
                    llm_response.tool_calls,
                    initial_response_message=response_message,
                    initial_include_score=llm_response.include_score,
                    initial_thought_block=self._format_thought_summary_block(
                        llm_response.thought_summary,
                        llm_response.tool_calls,
                    ),
                    initial_thought_summary=llm_response.thought_summary,
                    user_id=user_id,
                    user_email=user_email,
                )
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

    async def _start_preprocess_job(
        self,
        session_id: str,
        score: Dict[str, Any],
        tool_calls: List[ToolCall],
        *,
        initial_message: str,
        initial_thought_summary: str,
        user_id: str,
        user_email: str,
    ) -> Dict[str, Any]:
        """Create a background preprocess job and return a pollable response."""
        existing = self._preprocess_tasks.get(session_id)
        if existing and not existing.done():
            return {
                "type": "chat_text",
                "message": (
                    "I am still preparing the derived score from your previous request. "
                    "Please wait for it to finish before sending another message."
                ),
            }
        job_id = uuid.uuid4().hex
        await asyncio.to_thread(
            self._job_store.create_job,
            job_id=job_id,
            user_id=user_id,
            session_id=session_id,
            status="queued",
            render_type="preprocess",
        )
        await asyncio.to_thread(
            self._job_store.update_job,
            job_id,
            status="queued",
            step="preprocess",
            message=initial_message,
            progress=0.0,
            jobKind="preprocess",
        )
        task = asyncio.create_task(
            self._run_preprocess_job(
                session_id,
                copy.deepcopy(score),
                list(tool_calls),
                job_id,
                user_id,
                user_email,
                initial_message=initial_message,
                initial_thought_summary=initial_thought_summary,
            )
        )
        self._preprocess_tasks[session_id] = task

        def _cleanup(_: asyncio.Task) -> None:
            self._preprocess_tasks.pop(session_id, None)

        task.add_done_callback(_cleanup)
        return {
            "type": "chat_progress",
            "message": initial_message,
            "progress_url": f"/sessions/{session_id}/progress",
            "job_id": job_id,
        }

    @staticmethod
    def _release_result_allows_terminal_status(status: str) -> bool:
        return status in {"released", "already_released", "reservation_missing"}

    async def _mark_job_terminal_billing_state(
        self,
        *,
        job_id: str,
        status: str,
        step: str,
        message: str,
        error_message: str,
        output_path: Optional[str] = None,
    ) -> None:
        await asyncio.to_thread(
            self._job_store.update_job,
            job_id,
            status=status,
            step=step,
            message=message,
            progress=1.0,
            outputPath=output_path,
            errorMessage=error_message,
        )

    async def _mark_reservation_reconciliation_required(
        self,
        *,
        user_id: str,
        job_id: str,
        reservation_error: str,
        reservation_error_message: str,
    ) -> None:
        from src.backend.credits import mark_reservation_reconciliation_required

        await asyncio.to_thread(
            mark_reservation_reconciliation_required,
            user_id,
            job_id,
            last_error=reservation_error,
            last_error_message=reservation_error_message,
        )

    def _complete_job_and_settle_credits_with_retry_fault_injection(
        self,
        user_id: str,
        job_id: str,
        session_id: str,
        duration_seconds: float,
        *,
        output_path: Optional[str],
        audio_url: Optional[str],
    ):
        from src.backend.credits import (
            CompleteJobAndSettleCreditsResult,
            estimate_credits,
            settle_credits_and_complete_job,
        )

        if (
            self._settings.credit_retry_test_settle_fail_count > 0
            and self._settings.app_env.lower() in {"dev", "development", "local", "test"}
        ):
            remaining = self._settle_fault_injection_remaining.setdefault(
                job_id,
                self._settings.credit_retry_test_settle_fail_count,
            )
            if remaining > 0:
                next_remaining = remaining - 1
                self._settle_fault_injection_remaining[job_id] = next_remaining
                self._logger.warning(
                    "inject_settle_infra_error job=%s remaining_failures=%s",
                    job_id,
                    next_remaining,
                )
                return CompleteJobAndSettleCreditsResult(
                    status="infra_error",
                    actual_credits=estimate_credits(duration_seconds),
                    overdrafted=False,
                )
            self._settle_fault_injection_remaining.pop(job_id, None)
        return settle_credits_and_complete_job(
            user_id,
            job_id,
            session_id,
            duration_seconds,
            output_path=output_path,
            audio_url=audio_url,
        )

    def _release_credits_with_retry_fault_injection(
        self,
        user_id: str,
        job_id: str,
    ):
        from src.backend.credits import ReleaseCreditsResult, release_credits

        if (
            self._settings.credit_retry_test_release_fail_count > 0
            and self._settings.app_env.lower() in {"dev", "development", "local", "test"}
        ):
            remaining = self._release_fault_injection_remaining.setdefault(
                job_id,
                self._settings.credit_retry_test_release_fail_count,
            )
            if remaining > 0:
                next_remaining = remaining - 1
                self._release_fault_injection_remaining[job_id] = next_remaining
                self._logger.warning(
                    "inject_release_infra_error job=%s remaining_failures=%s",
                    job_id,
                    next_remaining,
                )
                return ReleaseCreditsResult(status="infra_error")
            self._release_fault_injection_remaining.pop(job_id, None)
        return release_credits(user_id, job_id)

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
            duration_seconds = response.get("duration_seconds", 0.0)
            output_path = response.get("output_storage_path") or response.get("output_path")
            settle_result = await retry_credit_op(
                self._complete_job_and_settle_credits_with_retry_fault_injection,
                user_id,
                job_id,
                session_id,
                duration_seconds,
                output_path=output_path,
                audio_url=response.get("audio_url"),
                max_attempts=self._settings.credit_retry_max_attempts,
                base_delay=self._settings.credit_retry_base_delay_seconds,
            )
            self._settle_fault_injection_remaining.pop(job_id, None)
            if settle_result.status in {
                "completed_and_settled",
                "already_completed_and_settled",
            }:
                return
            self._logger.error(
                "credit_settlement_unresolved session=%s job=%s status=%s",
                session_id,
                job_id,
                settle_result.status,
            )
            release_result = await retry_credit_op(
                self._release_credits_with_retry_fault_injection,
                user_id,
                job_id,
                max_attempts=self._settings.credit_retry_max_attempts,
                base_delay=self._settings.credit_retry_base_delay_seconds,
            )
            self._release_fault_injection_remaining.pop(job_id, None)
            if self._release_result_allows_terminal_status(release_result.status):
                await self._mark_job_terminal_billing_state(
                    job_id=job_id,
                    status="failed",
                    step="error",
                    message=backend_message("job.audio_generated_billing_failed_no_charge"),
                    error_message=(
                        "Billing finalization failed after audio generation. "
                        f"settle_status={settle_result.status} "
                        f"release_status={release_result.status}"
                    ),
                    output_path=output_path,
                )
                return
            await self._mark_reservation_reconciliation_required(
                user_id=user_id,
                job_id=job_id,
                reservation_error="settle_release_failed",
                reservation_error_message=(
                    "Billing finalization failed after audio generation and reservation rollback "
                    f"could not be completed. settle_status={settle_result.status} "
                    f"release_status={release_result.status}"
                ),
            )
            await self._mark_job_terminal_billing_state(
                job_id=job_id,
                status="failed",
                step="error",
                message=backend_message("job.audio_generated_billing_rollback_failed"),
                error_message=(
                    "Billing finalization failed after audio generation and billing rollback "
                    f"did not complete. settle_status={settle_result.status} "
                    f"release_status={release_result.status}"
                ),
                output_path=output_path,
            )
        except asyncio.CancelledError:
            # Release credits
            release_result = await retry_credit_op(
                self._release_credits_with_retry_fault_injection,
                user_id,
                job_id,
                max_attempts=self._settings.credit_retry_max_attempts,
                base_delay=self._settings.credit_retry_base_delay_seconds,
            )
            self._release_fault_injection_remaining.pop(job_id, None)
            if self._release_result_allows_terminal_status(release_result.status):
                await asyncio.to_thread(
                    self._job_store.update_job,
                    job_id,
                    status="cancelled",
                    step="cancelled",
                    message=backend_message("job.cancelled"),
                    progress=1.0,
                )
            else:
                await self._mark_reservation_reconciliation_required(
                    user_id=user_id,
                    job_id=job_id,
                    reservation_error="release_failed_after_cancel",
                    reservation_error_message=(
                        "Billing rollback failed after cancellation. "
                        f"status={release_result.status}"
                    ),
                )
                await self._mark_job_terminal_billing_state(
                    job_id=job_id,
                    status="cancelled",
                    step="cancelled",
                    message=backend_message("job.cancelled_billing_rollback_failed"),
                    error_message=(
                        "Billing rollback failed after cancellation. "
                        f"status={release_result.status}"
                    ),
                )
            raise
        except Exception as exc:
            # Release credits
            release_result = await retry_credit_op(
                self._release_credits_with_retry_fault_injection,
                user_id,
                job_id,
                max_attempts=self._settings.credit_retry_max_attempts,
                base_delay=self._settings.credit_retry_base_delay_seconds,
            )
            self._release_fault_injection_remaining.pop(job_id, None)
            self._logger.exception("synthesis_failed session=%s error=%s", session_id, exc)
            if self._release_result_allows_terminal_status(release_result.status):
                await asyncio.to_thread(
                    self._job_store.update_job,
                    job_id,
                    status="failed",
                    step="error",
                    message=backend_message("job.finish_failed"),
                    progress=1.0,
                    errorMessage=str(exc),
                )
            else:
                await self._mark_reservation_reconciliation_required(
                    user_id=user_id,
                    job_id=job_id,
                    reservation_error="release_failed_after_synthesis_error",
                    reservation_error_message=(
                        f"{exc} | billing_rollback_status={release_result.status}"
                    ),
                )
                await self._mark_job_terminal_billing_state(
                    job_id=job_id,
                    status="failed",
                    step="error",
                    message=backend_message("job.audio_generation_and_billing_rollback_failed"),
                    error_message=(
                        f"{exc} | billing_rollback_status={release_result.status}"
                    ),
                )
        finally:
            clear_log_context()

    async def _run_preprocess_job(
        self,
        session_id: str,
        score: Dict[str, Any],
        tool_calls: List[ToolCall],
        job_id: str,
        user_id: str,
        user_email: str,
        *,
        initial_message: str,
        initial_thought_summary: str,
    ) -> None:
        """Execute preprocess workflow in the background and publish completion message."""
        async def publish_attempt_messages(
            attempt_messages: List[Dict[str, Any]],
        ) -> None:
            await asyncio.to_thread(
                self._job_store.update_job,
                job_id,
                status="running",
                step="preprocess",
                message=initial_message,
                progress=0.05,
                jobKind="preprocess",
                details={"attempt_messages": copy.deepcopy(attempt_messages)},
            )

        try:
            set_log_context(session_id=session_id, job_id=job_id, user_id=user_id)
            await asyncio.to_thread(
                self._job_store.update_job,
                job_id,
                status="running",
                step="preprocess",
                message=initial_message,
                progress=0.05,
                jobKind="preprocess",
            )
            snapshot = await self._sessions.get_snapshot(session_id, user_id)
            response = await self._run_llm_tool_workflow(
                session_id,
                snapshot,
                score,
                tool_calls,
                initial_response_message=initial_message,
                initial_include_score=False,
                initial_thought_block=None,
                initial_thought_summary=initial_thought_summary,
                user_id=user_id,
                user_email=user_email,
                progress_callback=publish_attempt_messages,
            )
            message = str(response.get("message") or "").strip() or "Preprocess finished."
            await self._sessions.append_history(session_id, "assistant", message)
            if response.get("type") == "chat_error":
                await asyncio.to_thread(
                    self._job_store.update_job,
                    job_id,
                    status="failed",
                    step="error",
                    message=message,
                    errorMessage=message,
                    progress=1.0,
                    jobKind="preprocess",
                    details=response.get("details"),
                )
            else:
                warning_message = response.get("warning")
                await asyncio.to_thread(
                    self._job_store.update_job,
                    job_id,
                    status="completed",
                    step="review" if response.get("review_required") else "done",
                    message=message,
                    progress=1.0,
                    jobKind="preprocess",
                    reviewRequired=bool(response.get("review_required")),
                    actionRequired=response.get("action_required"),
                    details=response.get("details"),
                    warningMessage=warning_message,
                )
        except Exception as exc:
            self._logger.exception("preprocess_job_failed session=%s error=%s", session_id, exc)
            safe_message = "Couldn't finish preprocessing."
            await asyncio.to_thread(
                self._job_store.update_job,
                job_id,
                status="failed",
                step="error",
                message=safe_message,
                errorMessage=safe_message,
                progress=1.0,
                jobKind="preprocess",
            )
        finally:
            clear_log_context()

    async def _run_llm_tool_workflow(
        self,
        session_id: str,
        snapshot: Dict[str, Any],
        current_score: Dict[str, Any],
        tool_calls: List[ToolCall],
        *,
        initial_response_message: str,
        initial_include_score: bool,
        initial_thought_block: Optional[str],
        initial_thought_summary: Optional[str],
        user_id: str,
        user_email: str,
        progress_callback: Optional[Callable[[List[Dict[str, Any]]], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Execute an LLM-driven tool workflow with bounded repair turns."""
        include_score = initial_include_score
        response_message = initial_response_message
        thought_block_for_response = initial_thought_block
        pending_calls = list(tool_calls)
        working_score = current_score
        score_summary = snapshot.get("score_summary") if isinstance(snapshot, dict) else None
        review_required_pending = False
        response: Dict[str, Any] = {"type": "chat_text", "message": response_message}
        last_action_required_payload: Optional[Dict[str, Any]] = None
        best_valid_candidate: Optional[WorkflowCandidate] = None
        best_invalid_candidate: Optional[WorkflowCandidate] = None
        bootstrap_plan_baseline: Optional[BootstrapPlanBaseline] = None
        fixed_structural_issue_keys: set[str] = set()
        fixed_other_issue_keys: set[str] = set()
        current_repair_scopes: List[Dict[str, Any]] = []
        attempt_number = 0
        max_attempts = max(1, self._settings.preprocess_max_attempts)
        attempt_messages: List[Dict[str, Any]] = []
        if any(call.name == "preprocess_voice_parts" for call in pending_calls):
            initial_attempt_entry = self._build_attempt_message_entry(
                attempt_number=1,
                final_message=initial_response_message,
                thought_summary=initial_thought_summary or "",
            )
            if initial_attempt_entry is not None:
                attempt_messages.append(initial_attempt_entry)
                if progress_callback is not None:
                    await progress_callback(attempt_messages)

        while True:
            attempt_number += 1
            explicit_verse_number = self._normalize_verse_number(
                (snapshot.get("files") or {}).get(EXPLICIT_VERSE_METADATA_KEY)
                if isinstance(snapshot.get("files"), dict)
                else None
            )
            attempted_plan = self._extract_preprocess_plan_from_tool_calls(pending_calls)
            tool_result = await self._execute_tool_calls(
                session_id,
                working_score,
                pending_calls,
                user_id=user_id,
                score_summary=score_summary,
                user_email=user_email,
                explicit_verse_number=explicit_verse_number,
            )
            working_score = tool_result.score
            review_required_pending = review_required_pending or tool_result.review_required
            if tool_result.action_required_payload:
                last_action_required_payload = tool_result.action_required_payload
            if tool_result.explicit_verse_number is not None:
                snapshot = dict(snapshot)
                files = snapshot.get("files")
                next_files = dict(files) if isinstance(files, dict) else {}
                next_files[EXPLICIT_VERSE_METADATA_KEY] = tool_result.explicit_verse_number
                snapshot["files"] = next_files
            candidate = self._build_workflow_candidate(
                attempt_number=attempt_number,
                tool_result=tool_result,
                fallback_message=response_message,
                attempted_plan=attempted_plan,
                incumbent_best_plan=best_valid_candidate.plan if best_valid_candidate is not None else None,
                repair_scopes=current_repair_scopes,
            )
            action_required_payload = (
                tool_result.action_required_payload
                if isinstance(tool_result.action_required_payload, dict)
                else {}
            )
            action_name = str(action_required_payload.get("action") or "").strip()
            if (
                best_valid_candidate is None
                and isinstance(attempted_plan, dict)
                and action_name == "plan_lint_failed"
            ):
                baseline_scopes = self._build_repair_scopes(
                    action_required_payload,
                    baseline_plan=attempted_plan,
                )
                next_baseline = BootstrapPlanBaseline(
                    attempt_number=attempt_number,
                    plan=copy.deepcopy(attempted_plan),
                    action=action_name,
                    lint_findings=[
                        dict(item)
                        for item in action_required_payload.get("lint_findings", [])
                        if isinstance(item, dict)
                    ],
                    repair_scopes=copy.deepcopy(baseline_scopes),
                )
                if bootstrap_plan_baseline is None:
                    self._logger.info(
                        "bootstrap_plan_baseline_set session=%s attempt=%s baseline_source=plan_lint_failed",
                        session_id,
                        attempt_number,
                    )
                else:
                    self._logger.info(
                        "bootstrap_plan_baseline_replaced session=%s attempt=%s previous_attempt=%s baseline_source=plan_lint_failed",
                        session_id,
                        attempt_number,
                        bootstrap_plan_baseline.attempt_number,
                    )
                bootstrap_plan_baseline = next_baseline
            replaced_best_valid = False
            replaced_best_invalid = False
            candidate_decision_reason = ""
            if candidate is not None:
                if candidate.comparable:
                    is_structural_regression = self._candidate_is_structural_regression(
                        candidate,
                        fixed_structural_issue_keys,
                    )
                    is_full_rewrite = self._candidate_is_full_rewrite(candidate)
                    if is_structural_regression:
                        candidate_decision_reason = "rejected_regression"
                    elif is_full_rewrite:
                        candidate_decision_reason = "rejected_full_rewrite"
                    elif self._candidate_is_better(candidate, best_valid_candidate):
                        if best_valid_candidate is not None:
                            fixed_structural_issue_keys.update(
                                set(best_valid_candidate.structural_issue_keys)
                                - set(candidate.structural_issue_keys)
                            )
                            fixed_other_issue_keys.update(
                                set(best_valid_candidate.other_p1_issue_keys)
                                - set(candidate.other_p1_issue_keys)
                            )
                        candidate = replace(candidate, decision_reason="promoted")
                        best_valid_candidate = candidate
                        replaced_best_valid = True
                        candidate_decision_reason = "promoted"
                        if bootstrap_plan_baseline is not None:
                            self._logger.info(
                                "bootstrap_plan_baseline_cleared session=%s attempt=%s promoted_attempt=%s",
                                session_id,
                                attempt_number,
                                candidate.attempt_number,
                            )
                            bootstrap_plan_baseline = None
                    else:
                        candidate_decision_reason = "rejected_worse_than_best"
                elif self._candidate_is_better(candidate, best_invalid_candidate):
                    candidate = replace(candidate, decision_reason="promoted_invalid")
                    best_invalid_candidate = candidate
                    replaced_best_invalid = True
                    candidate_decision_reason = "promoted_invalid"
                else:
                    candidate_decision_reason = "rejected_worse_than_best_invalid"
                if candidate_decision_reason and candidate.decision_reason != candidate_decision_reason:
                    candidate = replace(candidate, decision_reason=candidate_decision_reason)
            if any(call.name == "preprocess_voice_parts" for call in pending_calls) or candidate is not None:
                await self._sessions.append_preprocess_attempt_summary(
                    session_id,
                    self._build_preprocess_attempt_summary(
                        attempt_number=attempt_number,
                        tool_calls=pending_calls,
                        tool_result=tool_result,
                        candidate=candidate,
                        replaced_best_valid=replaced_best_valid,
                        replaced_best_invalid=replaced_best_invalid,
                        baseline_plan_source_for_next_repair=(
                            self._select_repair_baseline(
                                best_candidate=best_valid_candidate,
                                bootstrap_baseline=bootstrap_plan_baseline,
                                latest_attempted_plan=attempted_plan,
                            )[0]
                        ),
                        used_bootstrap_plan_baseline=bootstrap_plan_baseline is not None,
                    ),
                )
            response = tool_result.audio_response or {
                "type": "chat_text",
                "message": response_message,
            }
            followup_prompt = tool_result.followup_prompt
            is_selection_blocker = action_name == "verse_selection_required"
            preprocess_iteration = any(
                call.name == "preprocess_voice_parts" for call in pending_calls
            ) or candidate is not None
            if is_selection_blocker:
                preprocess_iteration = False

            if followup_prompt is None:
                if response_message and (
                    response.get("type") == "chat_progress" or not response.get("message")
                ):
                    response["message"] = response_message
                if review_required_pending:
                    response["review_required"] = True
                break

            if self._tool_payload_has_error(followup_prompt):
                response["suppress_selector"] = True

            if preprocess_iteration:
                if best_valid_candidate is not None and best_valid_candidate.quality_class == 3:
                    best_valid_candidate = await self._materialize_review_candidate_if_needed(
                        session_id, best_valid_candidate
                    )
                    response = await self._render_selected_candidate_response(
                        session_id,
                        user_id,
                        working_score,
                        best_valid_candidate,
                        stop_reason="quality_class_3",
                    )
                    include_score = True
                    break

                if attempt_number >= max_attempts:
                    best_valid_candidate = await self._materialize_review_candidate_if_needed(
                        session_id, best_valid_candidate
                    )
                    candidate_response = await self._render_selected_candidate_response(
                        session_id,
                        user_id,
                        working_score,
                        best_valid_candidate,
                        stop_reason="attempt_budget_exhausted",
                    )
                    if candidate_response is not None:
                        response = candidate_response
                        include_score = True
                    elif best_invalid_candidate is not None:
                        response = self._build_invalid_candidate_error(best_invalid_candidate) or {
                            "type": "chat_error",
                            "message": (
                                f"Unable to produce synthesis-safe monophonic output after "
                                f"{max_attempts} attempts."
                            ),
                        }
                    else:
                        response = {
                            "type": "chat_text",
                            "message": (
                                f"I couldn't complete preprocessing after {max_attempts} attempts. "
                                "Please revise the request or plan details."
                            ),
                        }
                    break

                latest_snapshot = await self._sessions.get_snapshot(session_id, user_id)
                repair_response, repair_error = await self._decide_followup_with_llm(
                    latest_snapshot,
                    self._build_repair_planning_prompt(
                        best_valid_candidate,
                        bootstrap_plan_baseline,
                        candidate,
                        last_action_required_payload,
                        attempt_number=attempt_number,
                        max_attempts=max_attempts,
                        fixed_structural_issue_keys=fixed_structural_issue_keys,
                        fixed_other_issue_keys=fixed_other_issue_keys,
                    ),
                    working_score,
                )
                if repair_error:
                    best_valid_candidate = await self._materialize_review_candidate_if_needed(
                        session_id, best_valid_candidate
                    )
                    candidate_response = await self._render_selected_candidate_response(
                        session_id,
                        user_id,
                        working_score,
                        best_valid_candidate,
                        stop_reason="repair_planning_failed",
                    )
                    if candidate_response is not None:
                        candidate_response["warning"] = repair_error
                        response = candidate_response
                        include_score = True
                        break
                    invalid_error = self._build_invalid_candidate_error(best_invalid_candidate)
                    if invalid_error is not None:
                        return invalid_error
                    return {"type": "chat_error", "message": repair_error}

                if repair_response is None or not repair_response.tool_calls:
                    best_valid_candidate = await self._materialize_review_candidate_if_needed(
                        session_id, best_valid_candidate
                    )
                    candidate_response = await self._render_selected_candidate_response(
                        session_id,
                        user_id,
                        working_score,
                        best_valid_candidate,
                        stop_reason="repair_plan_missing",
                    )
                    if candidate_response is not None:
                        candidate_response["warning"] = (
                            "LLM did not return a repair preprocess plan."
                        )
                        response = candidate_response
                        include_score = True
                        break
                    invalid_error = self._build_invalid_candidate_error(best_invalid_candidate)
                    if invalid_error is not None:
                        return invalid_error
                    return {
                        "type": "chat_error",
                        "message": "LLM did not return a repair preprocess plan.",
                    }

                self._logger.debug(
                    "chat_llm_followup_repair session=%s response=%s",
                    session_id,
                    {
                        "tool_calls": [
                            {
                                "name": call.name,
                                "arguments": summarize_payload(call.arguments),
                            }
                            for call in repair_response.tool_calls
                        ],
                        "final_message": repair_response.final_message,
                        "include_score": repair_response.include_score,
                        "thought_summary": summarize_payload(repair_response.thought_summary),
                    },
                )
                include_score = include_score or repair_response.include_score
                response_message = self._merge_thought_summary(
                    repair_response.final_message or response_message,
                    repair_response.thought_summary,
                    repair_response.tool_calls,
                )
                thought_block_for_response = self._format_thought_summary_block(
                    repair_response.thought_summary,
                    repair_response.tool_calls,
                ) or thought_block_for_response
                repair_attempt_entry = self._build_attempt_message_entry(
                    attempt_number=attempt_number + 1,
                    final_message=repair_response.final_message or "",
                    thought_summary=repair_response.thought_summary,
                )
                if repair_attempt_entry is not None:
                    attempt_messages.append(repair_attempt_entry)
                    if progress_callback is not None:
                        await progress_callback(attempt_messages)
                current_repair_scopes = self._build_repair_scopes(
                    last_action_required_payload or {},
                    baseline_plan=self._select_repair_baseline(
                        best_candidate=best_valid_candidate,
                        bootstrap_baseline=bootstrap_plan_baseline,
                        latest_attempted_plan=attempted_plan,
                    )[1],
                )
                pending_calls = list(repair_response.tool_calls)
                continue

            latest_snapshot = await self._sessions.get_snapshot(session_id, user_id)
            followup_response, followup_error = await self._decide_followup_with_llm(
                latest_snapshot, followup_prompt, working_score
            )
            if followup_error:
                best_valid_candidate = await self._materialize_review_candidate_if_needed(
                    session_id, best_valid_candidate
                )
                candidate_response = self._build_candidate_response(best_valid_candidate)
                if candidate_response is not None:
                    candidate_response["warning"] = followup_error
                    response = candidate_response
                    include_score = True
                    break
                invalid_error = self._build_invalid_candidate_error(best_invalid_candidate)
                if invalid_error is not None:
                    return invalid_error
                return {"type": "chat_error", "message": followup_error}

            if followup_response is None:
                response["message"] = followup_prompt
                if review_required_pending:
                    response["review_required"] = True
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
            if (
                isinstance(last_action_required_payload, dict)
                and str(last_action_required_payload.get("action") or "").strip()
                == "verse_selection_required"
            ):
                response = {
                    "type": "chat_text",
                    "message": self._format_followup_message_text(response_message),
                }
                break
            thought_block_for_response = self._format_thought_summary_block(
                followup_response.thought_summary,
                followup_response.tool_calls,
            ) or thought_block_for_response

            if not followup_response.tool_calls:
                best_valid_candidate = await self._materialize_review_candidate_if_needed(
                    session_id, best_valid_candidate
                )
                candidate_response = self._build_candidate_response(best_valid_candidate)
                if candidate_response is not None:
                    response = candidate_response
                    response["message"] = self._format_followup_message_text(response_message)
                    response["review_required"] = True
                    include_score = True
                else:
                    response = {
                        "type": "chat_text",
                        "message": self._format_followup_message_text(response_message),
                    }
                    if review_required_pending:
                        response["review_required"] = True
                break

            pending_calls = list(followup_response.tool_calls)

        response = self._attach_attempt_messages(response, attempt_messages)
        if isinstance(last_action_required_payload, dict):
            response["action_required"] = copy.deepcopy(last_action_required_payload)
        if include_score or response.get("review_required"):
            updated_snapshot = await self._sessions.get_snapshot(session_id, user_id)
            updated_score = updated_snapshot.get("current_score")
            if updated_score is not None:
                response["current_score"] = updated_score
        return response

    def _build_repair_planning_prompt(
        self,
        best_candidate: Optional["WorkflowCandidate"],
        bootstrap_plan_baseline: Optional["BootstrapPlanBaseline"],
        latest_candidate: Optional["WorkflowCandidate"],
        action_required_payload: Optional[Dict[str, Any]],
        *,
        attempt_number: int,
        max_attempts: int,
        fixed_structural_issue_keys: Optional[set[str]] = None,
        fixed_other_issue_keys: Optional[set[str]] = None,
    ) -> str:
        """Build the next repair-planning prompt for a non-terminal preprocess candidate."""
        payload = (
            action_required_payload
            if isinstance(action_required_payload, dict)
            else latest_candidate.result_payload if latest_candidate is not None else {}
        )
        latest_attempted_plan = latest_candidate.plan if latest_candidate is not None else None
        baseline_source, baseline_plan = self._select_repair_baseline(
            best_candidate=best_candidate,
            bootstrap_baseline=bootstrap_plan_baseline,
            latest_attempted_plan=latest_attempted_plan,
        )
        repair_context = self._build_repair_context_summary(
            payload,
            baseline_plan=baseline_plan,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            latest_candidate=latest_candidate,
            best_candidate=best_candidate,
            fixed_structural_issue_keys=fixed_structural_issue_keys or set(),
            fixed_other_issue_keys=fixed_other_issue_keys or set(),
        )
        envelope = {
            "tool": "preprocess_voice_parts",
            "phase": "preprocess_repair_planning",
            "tool_result": payload,
            "baseline_plan_source": baseline_source,
            "baseline_plan": copy.deepcopy(baseline_plan) if isinstance(baseline_plan, dict) else None,
            "latest_attempted_plan_summary": (
                self._build_latest_attempted_plan_summary(latest_candidate, best_candidate)
                if best_candidate is not None
                else None
            ),
            "repair_context": repair_context,
        }
        return json.dumps(envelope, sort_keys=True)

    def _select_repair_baseline(
        self,
        *,
        best_candidate: Optional["WorkflowCandidate"],
        bootstrap_baseline: Optional["BootstrapPlanBaseline"],
        latest_attempted_plan: Optional[Dict[str, Any]],
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Select the concrete baseline plan for the next repair turn."""
        if best_candidate is not None and isinstance(best_candidate.plan, dict):
            return "best_plan_so_far", best_candidate.plan
        if bootstrap_baseline is not None:
            return "bootstrap_plan_baseline", bootstrap_baseline.plan
        if isinstance(latest_attempted_plan, dict):
            return "latest_attempted_plan", latest_attempted_plan
        return "none", None

    def _build_workflow_candidate(
        self,
        *,
        attempt_number: int,
        tool_result: "ToolExecutionResult",
        fallback_message: str,
        attempted_plan: Optional[Dict[str, Any]],
        incumbent_best_plan: Optional[Dict[str, Any]] = None,
        repair_scopes: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional["WorkflowCandidate"]:
        """Build a candidate summary from a preprocess tool result."""
        payload: Optional[Dict[str, Any]] = None
        review_required = False
        outcome_stage = "unknown"
        outcome_preference = 99
        comparable = False
        if isinstance(tool_result.action_required_payload, dict):
            payload = tool_result.action_required_payload
            action = str(payload.get("action") or "").strip()
            if action == "plan_lint_failed":
                return None
            if action == "validation_failed_needs_review":
                outcome_stage = "postflight_reviewable"
                outcome_preference = 2
                comparable = True
            else:
                outcome_stage = "postflight_unusable"
            target_results = self._extract_target_results(payload)
            issue_entries = self._collect_issue_entries_for_payload(
                payload,
                target_results=target_results,
            )
            if not issue_entries and not target_results:
                return None
            structurally_valid = self._targets_are_structurally_valid(target_results, issue_entries)
            review_required = structurally_valid
        elif tool_result.review_required and tool_result.followup_prompt:
            parsed = self._parse_followup_prompt_payload(
                tool_result.followup_prompt,
                attempt_number=attempt_number,
            )
            if parsed is None:
                return None
            payload = parsed
            target_results = self._extract_target_results(parsed)
            issue_entries = self._collect_issue_entries_for_payload(
                parsed,
                target_results=target_results,
            )
            structurally_valid = True
            review_required = True
            status = str(parsed.get("status") or "").strip()
            if status == "ready":
                outcome_stage = "ready"
                outcome_preference = 0
            else:
                outcome_stage = "ready_with_warnings"
                outcome_preference = 1
            comparable = True
        else:
            return None

        if target_results:
            structural_p1_measures = self._aggregate_target_impacted_measures(
                target_results, severity="P1", domain="STRUCTURAL"
            )
            other_p1_measures = self._aggregate_target_impacted_measures(
                target_results, severity="P1", domain="LYRIC"
            )
            p2_measures = self._aggregate_target_impacted_measures(
                target_results, severity="P2"
            )
        else:
            structural_p1_measures = self._count_impacted_measures(
                issue_entries, severity="P1", domain="STRUCTURAL"
            )
            other_p1_measures = self._count_impacted_measures(
                issue_entries, severity="P1", domain="LYRIC"
            )
            p2_measures = self._count_impacted_measures(issue_entries, severity="P2")
        quality_class = self._classify_candidate_quality(
            structural_p1_measures=structural_p1_measures,
            other_p1_measures=other_p1_measures,
            p2_measures=p2_measures,
        )
        issue_keys = self._issue_keys_for_issues(issue_entries)
        structural_issue_keys = [
            key
            for key, issue in issue_keys
            if str(issue.get("rule_severity") or "") == "P1"
            and str(issue.get("rule_domain") or "") == "STRUCTURAL"
        ]
        other_issue_keys = [
            key
            for key, issue in issue_keys
            if str(issue.get("rule_severity") or "") == "P1"
            and str(issue.get("rule_domain") or "") != "STRUCTURAL"
        ]
        out_of_scope_changed_section_count = 0
        plan_delta_size = 0
        section_change_ratio = 0.0
        measure_change_ratio = 0.0
        if comparable and isinstance(attempted_plan, dict) and isinstance(incumbent_best_plan, dict):
            change_metrics = self._compute_plan_change_metrics(
                best_plan=incumbent_best_plan,
                candidate_plan=attempted_plan,
                repair_scopes=repair_scopes or [],
            )
            out_of_scope_changed_section_count = change_metrics["out_of_scope_changed_section_count"]
            plan_delta_size = change_metrics["plan_delta_size"]
            section_change_ratio = change_metrics["section_change_ratio"]
            measure_change_ratio = change_metrics["measure_change_ratio"]
        candidate_tuple = (
            structural_p1_measures,
            other_p1_measures,
            p2_measures,
            out_of_scope_changed_section_count,
            plan_delta_size,
        )
        return WorkflowCandidate(
            attempt_number=attempt_number,
            score=tool_result.score,
            message=str(payload.get("message") or fallback_message),
            plan=copy.deepcopy(attempted_plan) if isinstance(attempted_plan, dict) else None,
            review_required=review_required,
            outcome_stage=outcome_stage,
            outcome_preference=outcome_preference,
            comparable=comparable,
            quality_class=quality_class,
            structurally_valid=structurally_valid,
            structural_p1_measures=structural_p1_measures,
            other_p1_measures=other_p1_measures,
            p2_measures=p2_measures,
            structural_issue_keys=structural_issue_keys,
            other_p1_issue_keys=other_issue_keys,
            candidate_tuple=candidate_tuple,
            issues=issue_entries,
            target_results=target_results,
            result_payload=dict(payload),
            action_required_payload=tool_result.action_required_payload,
            review_materialization=tool_result.review_materialization,
            out_of_scope_changed_section_count=out_of_scope_changed_section_count,
            plan_delta_size=plan_delta_size,
            section_change_ratio=section_change_ratio,
            measure_change_ratio=measure_change_ratio,
        )

    def _parse_followup_prompt_payload(
        self,
        followup_prompt: str,
        *,
        attempt_number: int,
    ) -> Optional[Dict[str, Any]]:
        """Parse structured follow-up prompt payload and log malformed content."""
        try:
            parsed = json.loads(followup_prompt)
        except json.JSONDecodeError as exc:
            self._logger.warning(
                "preprocess_followup_prompt_malformed_json attempt=%s error=%s payload=%s",
                attempt_number,
                exc,
                summarize_payload(followup_prompt),
            )
            return None
        if not isinstance(parsed, dict):
            self._logger.warning(
                "preprocess_followup_prompt_non_object attempt=%s payload=%s",
                attempt_number,
                summarize_payload(parsed),
            )
            return None
        return parsed

    def _collect_issue_entries_for_payload(
        self,
        payload: Dict[str, Any],
        *,
        target_results: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Collect normalized issues from visible targets when present, else payload-level issues."""
        effective_target_results = (
            target_results if isinstance(target_results, list) else self._extract_target_results(payload)
        )
        if effective_target_results:
            return self._aggregate_visible_target_issues(effective_target_results)
        return self._extract_issue_entries(payload)

    def _extract_issue_entries(
        self,
        payload: Dict[str, Any],
        *,
        default_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Collect normalized issue entries from preprocess result payloads."""
        issue_entries: List[Dict[str, Any]] = []
        context = default_context if isinstance(default_context, dict) else payload

        def _normalized_issue(entry: Dict[str, Any]) -> Dict[str, Any]:
            normalized = dict(entry)
            if "rule_severity" not in normalized and "severity" in normalized:
                normalized["rule_severity"] = normalized.get("severity")
            if "rule_domain" not in normalized and "domain" in normalized:
                normalized["rule_domain"] = normalized.get("domain")
            for field, fallback_key in (
                ("part_index", "part_index"),
                ("target_voice_part_id", "target_voice_part_id"),
                ("target_voice_part", "target_voice_part"),
                ("source_part_index", "source_part_index"),
                ("source_voice_part_id", "source_voice_part_id"),
            ):
                if normalized.get(field) is None and isinstance(context, dict):
                    fallback = context.get(field)
                    if fallback is None and field == "target_voice_part_id":
                        fallback = context.get("target_voice_part")
                    if fallback is None and field == "target_voice_part":
                        fallback = context.get("target_voice_part_id")
                    if fallback is not None:
                        normalized[field] = fallback
            return normalized
        failed_rules = payload.get("failed_validation_rules")
        if isinstance(failed_rules, list):
            for entry in failed_rules:
                if isinstance(entry, dict):
                    issue_entries.append(_normalized_issue(entry))
        warnings = payload.get("warnings")
        if isinstance(warnings, list):
            for entry in warnings:
                if not isinstance(entry, dict):
                    continue
                rule_metadata = entry.get("rule_metadata")
                if isinstance(rule_metadata, dict):
                    issue_entries.append(_normalized_issue(rule_metadata))
        lint_findings = payload.get("lint_findings")
        if isinstance(lint_findings, list):
            for entry in lint_findings:
                if isinstance(entry, dict):
                    issue_entries.append(_normalized_issue(entry))
        return issue_entries

    def _extract_target_results(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Collect normalized per-target preprocess results from payload."""
        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, list):
            return []
        normalized_targets: List[Dict[str, Any]] = []
        for entry in raw_targets:
            if not isinstance(entry, dict):
                continue
            normalized = dict(entry)
            issues = normalized.get("issues")
            if isinstance(issues, list):
                normalized["issues"] = [
                    self._normalize_issue_for_candidate(item)
                    for item in issues
                    if isinstance(item, dict)
                ]
            else:
                normalized["issues"] = self._extract_issue_entries(
                    normalized,
                    default_context={
                        "part_index": normalized.get("part_index"),
                        "target_voice_part_id": normalized.get("target_voice_part_id"),
                        "target_voice_part": normalized.get("target_voice_part"),
                        "source_part_index": normalized.get("source_part_index"),
                        "source_voice_part_id": normalized.get("source_voice_part_id"),
                    },
                )
            if "visible" not in normalized:
                normalized["visible"] = not bool(normalized.get("hidden_default_lane"))
            if "structurally_valid" not in normalized:
                normalized["structurally_valid"] = not self._has_structural_p0(
                    normalized["issues"]
                )
            if "structural_p1_measures" not in normalized:
                normalized["structural_p1_measures"] = self._count_impacted_measures(
                    normalized["issues"], severity="P1", domain="STRUCTURAL"
                )
            if "other_p1_measures" not in normalized:
                normalized["other_p1_measures"] = self._count_impacted_measures(
                    normalized["issues"], severity="P1", domain="LYRIC"
                )
            if "p2_measures" not in normalized:
                normalized["p2_measures"] = self._count_impacted_measures(
                    normalized["issues"], severity="P2"
                )
            if "quality_class" not in normalized:
                normalized["quality_class"] = self._classify_candidate_quality(
                    structural_p1_measures=int(normalized["structural_p1_measures"]),
                    other_p1_measures=int(normalized["other_p1_measures"]),
                    p2_measures=int(normalized["p2_measures"]),
                )
            normalized_targets.append(normalized)
        return normalized_targets

    def _normalize_issue_for_candidate(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize issue keys for candidate ranking."""
        normalized = dict(entry)
        if "rule_severity" not in normalized and "severity" in normalized:
            normalized["rule_severity"] = normalized.get("severity")
        if "rule_domain" not in normalized and "domain" in normalized:
            normalized["rule_domain"] = normalized.get("domain")
        return normalized

    def _visible_target_results(self, targets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return visible targets, or all targets if none are marked visible."""
        visible = [target for target in targets if bool(target.get("visible", True))]
        return visible or list(targets)

    def _aggregate_visible_target_issues(self, targets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Flatten issues from visible targets only."""
        aggregated: List[Dict[str, Any]] = []
        for target in self._visible_target_results(targets):
            issues = target.get("issues")
            if isinstance(issues, list):
                aggregated.extend(
                    self._normalize_issue_for_candidate(
                        {
                            **issue,
                            "part_index": issue.get("part_index", target.get("part_index")),
                            "target_voice_part_id": issue.get(
                                "target_voice_part_id",
                                target.get("target_voice_part_id"),
                            )
                            or target.get("target_voice_part"),
                            "target_voice_part": issue.get(
                                "target_voice_part",
                                target.get("target_voice_part"),
                            )
                            or target.get("target_voice_part_id"),
                            "source_part_index": issue.get(
                                "source_part_index",
                                target.get("source_part_index"),
                            ),
                            "source_voice_part_id": issue.get(
                                "source_voice_part_id",
                                target.get("source_voice_part_id"),
                            ),
                        }
                    )
                    for issue in issues
                    if isinstance(issue, dict)
                )
        return aggregated

    def _targets_are_structurally_valid(
        self,
        targets: List[Dict[str, Any]],
        fallback_issues: List[Dict[str, Any]],
    ) -> bool:
        """Return True if all visible targets are free of structural P0 issues."""
        if not targets:
            return not self._has_structural_p0(fallback_issues)
        return all(
            not self._has_structural_p0(target.get("issues") or [])
            for target in self._visible_target_results(targets)
        )

    def _aggregate_target_impacted_measures(
        self,
        targets: List[Dict[str, Any]],
        *,
        severity: str,
        domain: Optional[str] = None,
    ) -> int:
        """Return union-count of impacted measures across visible targets."""
        impacted: set[int] = set()
        for target in self._visible_target_results(targets):
            issues = target.get("issues")
            if not isinstance(issues, list):
                continue
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                if str(issue.get("rule_severity") or "") != severity:
                    continue
                if domain is not None and str(issue.get("rule_domain") or "") != domain:
                    continue
                issue_measures = issue.get("impacted_measures")
                if isinstance(issue_measures, list):
                    for measure in issue_measures:
                        if isinstance(measure, int):
                            impacted.add(measure)
                issue_ranges = issue.get("impacted_ranges")
                if isinstance(issue_ranges, list):
                    for item in issue_ranges:
                        if not isinstance(item, dict):
                            continue
                        start = item.get("start")
                        end = item.get("end")
                        if isinstance(start, int) and isinstance(end, int) and start <= end:
                            impacted.update(range(start, end + 1))
        return len(impacted)

    def _has_structural_p0(self, issues: List[Dict[str, Any]]) -> bool:
        """Return True if any issue is P0 STRUCTURAL."""
        return any(
            str(issue.get("rule_severity") or "") == "P0"
            and str(issue.get("rule_domain") or "") == "STRUCTURAL"
            for issue in issues
        )

    def _count_impacted_measures(
        self,
        issues: List[Dict[str, Any]],
        *,
        severity: str,
        domain: Optional[str] = None,
    ) -> int:
        """Return union-count of impacted measures for matching issue bucket."""
        impacted: set[int] = set()
        for issue in issues:
            if str(issue.get("rule_severity") or "") != severity:
                continue
            if domain is not None and str(issue.get("rule_domain") or "") != domain:
                continue
            issue_measures = issue.get("impacted_measures")
            if isinstance(issue_measures, list):
                for measure in issue_measures:
                    if isinstance(measure, int):
                        impacted.add(measure)
            issue_ranges = issue.get("impacted_ranges")
            if isinstance(issue_ranges, list):
                for item in issue_ranges:
                    if not isinstance(item, dict):
                        continue
                    start = item.get("start")
                    end = item.get("end")
                    if isinstance(start, int) and isinstance(end, int) and start <= end:
                        impacted.update(range(start, end + 1))
        return len(impacted)

    def _classify_candidate_quality(
        self,
        *,
        structural_p1_measures: int,
        other_p1_measures: int,
        p2_measures: int,
    ) -> int:
        """Return quality class 3/2/1 for a structurally valid candidate."""
        if structural_p1_measures > 0 or other_p1_measures > 0:
            return 1
        if p2_measures > 0:
            return 2
        return 3

    def _candidate_is_better(
        self,
        candidate: "WorkflowCandidate",
        incumbent: Optional["WorkflowCandidate"],
    ) -> bool:
        """Return True when candidate outranks incumbent."""
        if incumbent is None:
            return True
        if candidate.candidate_tuple != incumbent.candidate_tuple:
            return candidate.candidate_tuple < incumbent.candidate_tuple
        if candidate.outcome_preference != incumbent.outcome_preference:
            return candidate.outcome_preference < incumbent.outcome_preference
        return candidate.attempt_number < incumbent.attempt_number

    def _build_repair_context_summary(
        self,
        payload: Dict[str, Any],
        *,
        baseline_plan: Optional[Dict[str, Any]],
        attempt_number: int,
        max_attempts: int,
        latest_candidate: Optional["WorkflowCandidate"],
        best_candidate: Optional["WorkflowCandidate"],
        fixed_structural_issue_keys: set[str],
        fixed_other_issue_keys: set[str],
    ) -> Dict[str, Any]:
        """Build normalized repair context for the next preprocess repair turn."""
        target_results = self._extract_target_results(payload)
        if target_results:
            issues = self._aggregate_visible_target_issues(target_results)
        else:
            issues = self._extract_issue_entries(payload)
        repair_scopes = self._build_repair_scopes(payload, baseline_plan=baseline_plan)
        issue_keys = [key for key, _ in self._issue_keys_for_issues(issues)]
        return {
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
            "quality_class": latest_candidate.quality_class if latest_candidate is not None else None,
            "current_issue_keys": issue_keys,
            "fixed_structural_p1_issue_keys": sorted(fixed_structural_issue_keys),
            "fixed_other_p1_issue_keys": sorted(fixed_other_issue_keys),
            "repair_scopes": repair_scopes,
            "best_plan_quality_summary": (
                self._build_plan_quality_summary(best_candidate)
                if best_candidate is not None
                else None
            ),
        }

    def _build_latest_attempted_plan_summary(
        self,
        latest_candidate: Optional["WorkflowCandidate"],
        best_candidate: Optional["WorkflowCandidate"],
    ) -> Optional[Dict[str, Any]]:
        """Build compact summary for the latest attempted plan."""
        if latest_candidate is None:
            return None
        summary: Dict[str, Any] = {
            "outcome_stage": latest_candidate.outcome_stage,
            "main_failure_rules": [
                str(issue.get("rule") or issue.get("code") or "")
                for issue in latest_candidate.issues
            ],
            "structurally_valid": latest_candidate.structurally_valid,
        }
        if best_candidate is not None:
            summary["changed_sections_vs_best"] = latest_candidate.plan_delta_size
            summary["out_of_scope_changed_sections_vs_best"] = (
                latest_candidate.out_of_scope_changed_section_count
            )
            if latest_candidate.decision_reason:
                summary["not_promoted_reason"] = latest_candidate.decision_reason
            elif not latest_candidate.comparable:
                summary["not_promoted_reason"] = "non_comparable_candidate"
        return summary

    def _build_plan_quality_summary(
        self, candidate: Optional["WorkflowCandidate"]
    ) -> Optional[Dict[str, Any]]:
        """Build plan-quality summary for comparator/debug payloads."""
        if candidate is None:
            return None
        return {
            "structural_p1_union_affected_measure_count": candidate.structural_p1_measures,
            "other_p1_union_affected_measure_count": candidate.other_p1_measures,
            "p2_union_affected_measure_count": candidate.p2_measures,
            "out_of_scope_changed_section_count": candidate.out_of_scope_changed_section_count,
            "plan_delta_size": candidate.plan_delta_size,
            "candidate_tuple": list(candidate.candidate_tuple),
            "outcome_stage": candidate.outcome_stage,
        }

    def _issue_keys_for_issues(
        self, issues: List[Dict[str, Any]]
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Build normalized issue fingerprints for regression tracking."""
        keyed: List[Tuple[str, Dict[str, Any]]] = []
        for issue in issues:
            keyed.append((self._normalize_issue_key(issue), issue))
        return keyed

    def _normalize_issue_key(self, issue: Dict[str, Any]) -> str:
        """Return normalized scoped issue fingerprint."""
        spans = self._normalize_issue_spans(issue)
        payload = {
            "rule_code": str(issue.get("rule") or issue.get("code") or ""),
            "severity_bucket": str(issue.get("rule_severity") or issue.get("severity") or ""),
            "part_index": issue.get("part_index"),
            "target_voice_part_id": issue.get("target_voice_part_id")
            or issue.get("target_voice_part")
            or issue.get("voice_part_id"),
            "source_part_index": issue.get("source_part_index"),
            "source_voice_part_id": issue.get("source_voice_part_id"),
            "affected_spans": spans,
        }
        return json.dumps(payload, sort_keys=True)

    def _normalize_issue_spans(self, issue: Dict[str, Any]) -> List[Dict[str, int]]:
        """Normalize measure spans from issue metadata."""
        spans: List[Dict[str, int]] = []
        impacted_ranges = issue.get("impacted_ranges")
        if isinstance(impacted_ranges, list):
            for item in impacted_ranges:
                if not isinstance(item, dict):
                    continue
                start = item.get("start")
                end = item.get("end")
                if isinstance(start, int) and isinstance(end, int) and start <= end:
                    spans.append({"start": start, "end": end})
        if spans:
            return self._normalize_spans(spans)
        measures = issue.get("impacted_measures")
        if isinstance(measures, list):
            normalized_measures = sorted({int(m) for m in measures if isinstance(m, int)})
            return self._collapse_measures_to_spans(normalized_measures)
        return []

    def _normalize_spans(self, spans: List[Dict[str, int]]) -> List[Dict[str, int]]:
        """Sort and merge span dictionaries."""
        points: List[Tuple[int, int]] = []
        for span in spans:
            start = span.get("start")
            end = span.get("end")
            if isinstance(start, int) and isinstance(end, int) and start <= end:
                points.append((start, end))
        points.sort()
        merged: List[Dict[str, int]] = []
        for start, end in points:
            if not merged or start > merged[-1]["end"] + 1:
                merged.append({"start": start, "end": end})
            else:
                merged[-1]["end"] = max(merged[-1]["end"], end)
        return merged

    def _collapse_measures_to_spans(self, measures: List[int]) -> List[Dict[str, int]]:
        """Collapse sorted measures to inclusive spans."""
        if not measures:
            return []
        spans: List[Dict[str, int]] = []
        start = measures[0]
        end = measures[0]
        for measure in measures[1:]:
            if measure == end + 1:
                end = measure
                continue
            spans.append({"start": start, "end": end})
            start = end = measure
        spans.append({"start": start, "end": end})
        return spans

    def _build_repair_scopes(
        self,
        payload: Dict[str, Any],
        *,
        baseline_plan: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build repair scopes from failing payload and current baseline plan."""
        scopes: List[Dict[str, Any]] = []
        target_results = self._extract_target_results(payload)
        if target_results:
            for target in self._visible_target_results(target_results):
                target_identity = self._repair_scope_target_identity(target, fallback_payload=payload)
                failing_spans = self._repair_scope_spans_from_payload(target) or self._repair_scope_spans_from_payload(payload)
                if not failing_spans:
                    continue
                scopes.append(
                    {
                        **target_identity,
                        "failing_spans": failing_spans,
                        "anchor_sections": self._find_anchor_sections(
                            baseline_plan,
                            target_identity,
                            failing_spans,
                        ),
                    }
                )
            if scopes:
                return scopes
        target_identity = self._repair_scope_target_identity(payload, fallback_payload=payload)
        failing_spans = self._repair_scope_spans_from_payload(payload)
        if not failing_spans:
            return []
        return [
            {
                **target_identity,
                "failing_spans": failing_spans,
                "anchor_sections": self._find_anchor_sections(
                    baseline_plan,
                    target_identity,
                    failing_spans,
                ),
            }
        ]

    def _repair_scope_target_identity(
        self,
        payload: Dict[str, Any],
        *,
        fallback_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Extract target identity for repair scope logic."""
        return {
            "part_index": payload.get("part_index", fallback_payload.get("part_index") if isinstance(fallback_payload, dict) else None),
            "target_voice_part_id": payload.get("target_voice_part_id")
            or payload.get("target_voice_part")
            or (fallback_payload.get("target_voice_part") if isinstance(fallback_payload, dict) else None),
        }

    def _repair_scope_spans_from_payload(self, payload: Dict[str, Any]) -> List[Dict[str, int]]:
        """Extract normalized failing spans from payload."""
        spans = payload.get("failing_ranges")
        if isinstance(spans, list):
            normalized = self._normalize_spans([item for item in spans if isinstance(item, dict)])
            if normalized:
                return normalized
        issues = payload.get("issues")
        if isinstance(issues, list):
            issue_spans: List[Dict[str, int]] = []
            for issue in issues:
                if isinstance(issue, dict):
                    issue_spans.extend(self._normalize_issue_spans(issue))
            if issue_spans:
                return self._normalize_spans(issue_spans)
        failed_rules = payload.get("failed_validation_rules")
        if isinstance(failed_rules, list):
            issue_spans = []
            for issue in failed_rules:
                if isinstance(issue, dict):
                    issue_spans.extend(self._normalize_issue_spans(issue))
            if issue_spans:
                return self._normalize_spans(issue_spans)
        return []

    def _find_anchor_sections(
        self,
        baseline_plan: Optional[Dict[str, Any]],
        target_identity: Dict[str, Any],
        failing_spans: List[Dict[str, int]],
    ) -> List[Dict[str, int]]:
        """Return all baseline sections on the same target that intersect failing spans."""
        if not isinstance(baseline_plan, dict):
            return []
        anchors: List[Dict[str, int]] = []
        for section in self._iter_plan_sections(baseline_plan):
            if section["part_index"] != target_identity.get("part_index"):
                continue
            if section["target_voice_part_id"] != target_identity.get("target_voice_part_id"):
                continue
            for failing_span in failing_spans:
                if self._spans_overlap(
                    section["start_measure"],
                    section["end_measure"],
                    int(failing_span["start"]),
                    int(failing_span["end"]),
                ):
                    anchors.append(
                        {"start": section["start_measure"], "end": section["end_measure"]}
                    )
                    break
        return self._normalize_spans(anchors)

    def _iter_plan_sections(self, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Flatten plan targets/sections into diffable section entries."""
        results: List[Dict[str, Any]] = []
        raw_targets = plan.get("targets")
        if not isinstance(raw_targets, list):
            return results
        for target_entry in raw_targets:
            if not isinstance(target_entry, dict):
                continue
            target = target_entry.get("target")
            if not isinstance(target, dict):
                continue
            part_index = target.get("part_index")
            voice_part_id = target.get("voice_part_id")
            sections = target_entry.get("sections")
            if not isinstance(sections, list):
                continue
            for section in sections:
                if not isinstance(section, dict):
                    continue
                start = section.get("start_measure")
                end = section.get("end_measure")
                if not isinstance(start, int) or not isinstance(end, int):
                    continue
                results.append(
                    {
                        "part_index": part_index,
                        "target_voice_part_id": voice_part_id,
                        "start_measure": start,
                        "end_measure": end,
                        "identity": (part_index, str(voice_part_id or ""), start, end),
                        "semantics": self._normalize_section_semantics(section),
                    }
                )
        return results

    def _normalize_section_semantics(self, section: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize execution-relevant section fields for semantic diffing."""
        return {
            key: copy.deepcopy(value)
            for key, value in section.items()
            if key not in {"start_measure", "end_measure"}
        }

    def _compute_plan_change_metrics(
        self,
        *,
        best_plan: Dict[str, Any],
        candidate_plan: Dict[str, Any],
        repair_scopes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Compute semantic plan diff and out-of-scope counts against best plan."""
        best_sections = {entry["identity"]: entry for entry in self._iter_plan_sections(best_plan)}
        candidate_sections = {entry["identity"]: entry for entry in self._iter_plan_sections(candidate_plan)}
        all_keys = set(best_sections) | set(candidate_sections)
        changed_entries: List[Dict[str, Any]] = []
        for key in all_keys:
            best_entry = best_sections.get(key)
            candidate_entry = candidate_sections.get(key)
            if best_entry is None or candidate_entry is None:
                entry = best_entry or candidate_entry
                assert entry is not None
                changed_entries.append(entry)
                continue
            if best_entry["semantics"] != candidate_entry["semantics"]:
                changed_entries.append(candidate_entry)
        out_of_scope = 0
        changed_measures: set[int] = set()
        total_measures: set[int] = set()
        for entry in best_sections.values():
            total_measures.update(range(entry["start_measure"], entry["end_measure"] + 1))
        for entry in candidate_sections.values():
            total_measures.update(range(entry["start_measure"], entry["end_measure"] + 1))
        for entry in changed_entries:
            changed_measures.update(range(entry["start_measure"], entry["end_measure"] + 1))
            if not self._section_change_is_in_scope(entry, repair_scopes):
                out_of_scope += 1
        total_section_count = max(1, max(len(best_sections), len(candidate_sections)))
        total_measure_count = max(1, len(total_measures))
        return {
            "plan_delta_size": len(changed_entries),
            "out_of_scope_changed_section_count": out_of_scope,
            "section_change_ratio": round(len(changed_entries) / total_section_count, 4),
            "measure_change_ratio": round(len(changed_measures) / total_measure_count, 4),
        }

    def _section_change_is_in_scope(
        self,
        section_entry: Dict[str, Any],
        repair_scopes: List[Dict[str, Any]],
    ) -> bool:
        """Return True when section change falls within same-target failing span or anchor section."""
        if not repair_scopes:
            return True
        for scope in repair_scopes:
            if section_entry["part_index"] != scope.get("part_index"):
                continue
            if section_entry["target_voice_part_id"] != scope.get("target_voice_part_id"):
                continue
            for span in scope.get("failing_spans") or []:
                if self._spans_overlap(
                    section_entry["start_measure"],
                    section_entry["end_measure"],
                    int(span["start"]),
                    int(span["end"]),
                ):
                    return True
            for anchor in scope.get("anchor_sections") or []:
                if self._spans_overlap(
                    section_entry["start_measure"],
                    section_entry["end_measure"],
                    int(anchor["start"]),
                    int(anchor["end"]),
                ):
                    return True
        return False

    def _spans_overlap(self, start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
        """Return True when two inclusive spans overlap."""
        return start_a <= end_b and start_b <= end_a

    def _candidate_is_structural_regression(
        self,
        candidate: "WorkflowCandidate",
        fixed_structural_issue_keys: set[str],
    ) -> bool:
        """Return True when candidate reintroduces a previously fixed structural issue."""
        return bool(set(candidate.structural_issue_keys) & fixed_structural_issue_keys)

    def _candidate_is_full_rewrite(
        self, candidate: "WorkflowCandidate"
    ) -> bool:
        """Return True when candidate rewrites too much of the baseline plan."""
        return (
            candidate.out_of_scope_changed_section_count > MAX_OUT_OF_SCOPE_SECTIONS
            or candidate.section_change_ratio > MAX_SECTION_CHANGE_RATIO
            or candidate.measure_change_ratio > MAX_MEASURE_CHANGE_RATIO
        )

    def _build_candidate_response(
        self, candidate: Optional["WorkflowCandidate"]
    ) -> Optional[Dict[str, Any]]:
        """Convert the selected candidate into a user-facing response payload."""
        if candidate is None or not candidate.structurally_valid:
            return None
        response = {
            "type": "chat_text",
            "message": candidate.message,
            "details": self._build_candidate_details(candidate),
        }
        if candidate.review_required:
            response["review_required"] = True
        return response

    def _serialize_other_p1_measures(self, value: int) -> Dict[str, int]:
        """Return compatibility fields for the non-structural P1 bucket."""
        return {
            "lyric_p1_measures": value,
            "other_p1_measures": value,
        }

    def _detect_followup_prompt_internal_error(
        self, tool_result: "ToolExecutionResult"
    ) -> str:
        """Return structured internal reason for malformed follow-up payloads."""
        if not tool_result.review_required or not tool_result.followup_prompt:
            return ""
        try:
            parsed = json.loads(tool_result.followup_prompt)
        except json.JSONDecodeError:
            return "malformed_followup_prompt_json"
        if not isinstance(parsed, dict):
            return "non_object_followup_prompt"
        return ""

    async def _materialize_review_candidate_if_needed(
        self,
        session_id: str,
        candidate: Optional["WorkflowCandidate"],
    ) -> Optional["WorkflowCandidate"]:
        """Materialize and persist the selected review candidate once, on final selection."""
        if candidate is None or candidate.review_materialization is None:
            return candidate
        finalized = finalize_review_materialization(candidate.review_materialization)
        score = finalized.get("score")
        if not isinstance(score, dict):
            return candidate
        metadata = candidate.review_materialization.get("metadata")
        if isinstance(metadata, dict):
            final_metadata = finalized.setdefault("metadata", {})
            final_metadata.update(metadata)
        review_score = self._mark_review_pending(score, finalized)
        await self._sessions.set_score(session_id, review_score)
        updated_payload = dict(candidate.result_payload)
        for key in (
            "score",
            "part_index",
            "transform_id",
            "score_fingerprint",
            "transform_hash",
            "appended_part_ref",
            "modified_musicxml_path",
            "reused_transform",
            "hidden_default_lane",
            "warnings",
            "validation",
            "metadata",
        ):
            if key in finalized:
                updated_payload[key] = finalized[key]
        return replace(
            candidate,
            score=review_score,
            result_payload=updated_payload,
            review_materialization=None,
        )

    async def _render_selected_candidate_response(
        self,
        session_id: str,
        user_id: str,
        current_score: Dict[str, Any],
        candidate: Optional["WorkflowCandidate"],
        *,
        stop_reason: str,
    ) -> Optional[Dict[str, Any]]:
        """Render a selected reviewable candidate with one final LLM explanation step."""
        if candidate is None or not candidate.structurally_valid:
            return None
        candidate_response = self._build_candidate_response(candidate)
        if candidate_response is None:
            return None
        summary_payload = dict(candidate.result_payload)
        summary_payload["stop_reason"] = stop_reason
        summary_payload["quality_class"] = candidate.quality_class
        latest_snapshot = await self._sessions.get_snapshot(session_id, user_id)
        followup_response, followup_error = await self._decide_followup_with_llm(
            latest_snapshot,
            self._build_terminal_candidate_prompt(summary_payload),
            current_score,
        )
        if followup_error:
            return candidate_response
        if followup_response is None:
            return candidate_response
        candidate_response["message"] = self._format_followup_message_text(
            followup_response.final_message or candidate_response["message"]
        )
        candidate_response["review_required"] = True
        return candidate_response

    def _build_terminal_candidate_prompt(self, payload: Dict[str, Any]) -> str:
        """Build the final explanation prompt for the selected candidate."""
        serialized = json.dumps(payload, sort_keys=True)
        return (
            f"{serialized}\n\n"
            "Explain this selected preprocess candidate to the user. "
            "Do not call any tools. "
            "Summarize what remains unresolved, why the workflow is stopping here, "
            "and ask the user to review the score or request revisions."
        )

    def _build_invalid_candidate_error(
        self, candidate: Optional["WorkflowCandidate"]
    ) -> Optional[Dict[str, Any]]:
        """Convert the selected best-invalid candidate into a structured error payload."""
        if candidate is None or candidate.structurally_valid:
            return None
        payload = candidate.action_required_payload if isinstance(candidate.action_required_payload, dict) else {}
        diagnostics: Dict[str, Any] = {
            "attempt_number": candidate.attempt_number,
            "quality_class": candidate.quality_class,
            "structural_p1_measures": candidate.structural_p1_measures,
            "p2_measures": candidate.p2_measures,
            "targets": candidate.target_results,
        }
        diagnostics.update(self._serialize_other_p1_measures(candidate.other_p1_measures))
        validation = payload.get("validation")
        if isinstance(validation, dict):
            diagnostics["validation"] = validation
        failing_ranges = payload.get("failing_ranges")
        if isinstance(failing_ranges, list):
            diagnostics["failing_ranges"] = failing_ranges
        return {
            "type": "chat_error",
            "message": (
                f"Unable to produce synthesis-safe monophonic output after "
                f"{self._settings.preprocess_max_attempts} attempts."
            ),
            "details": {
                "best_invalid_candidate": diagnostics,
                "failed_validation_rules": candidate.issues,
            },
        }

    def _build_candidate_details(self, candidate: "WorkflowCandidate") -> Dict[str, Any]:
        """Return normalized details for a selected candidate."""
        payload = candidate.result_payload if isinstance(candidate.result_payload, dict) else {}
        details: Dict[str, Any] = {
            "attempt_number": candidate.attempt_number,
            "quality_class": candidate.quality_class,
            "structurally_valid": candidate.structurally_valid,
            "structural_p1_measures": candidate.structural_p1_measures,
            "p2_measures": candidate.p2_measures,
            "out_of_scope_changed_section_count": candidate.out_of_scope_changed_section_count,
            "plan_delta_size": candidate.plan_delta_size,
            "candidate_tuple": list(candidate.candidate_tuple),
            "outcome_stage": candidate.outcome_stage,
            "issues": candidate.issues,
            "targets": candidate.target_results,
        }
        details.update(self._serialize_other_p1_measures(candidate.other_p1_measures))
        warnings = payload.get("warnings")
        if isinstance(warnings, list):
            details["warnings"] = warnings
        validation = payload.get("validation")
        if isinstance(validation, dict):
            details["validation"] = validation
        failing_ranges = payload.get("failing_ranges")
        if isinstance(failing_ranges, list):
            details["failing_ranges"] = failing_ranges
        return details

    def _build_preprocess_attempt_summary(
        self,
        *,
        attempt_number: int,
        tool_calls: List[ToolCall],
        tool_result: "ToolExecutionResult",
        candidate: Optional["WorkflowCandidate"],
        replaced_best_valid: bool,
        replaced_best_invalid: bool,
        baseline_plan_source_for_next_repair: str,
        used_bootstrap_plan_baseline: bool,
    ) -> Dict[str, Any]:
        """Build a lightweight persisted summary for one preprocess attempt."""
        payload = tool_result.action_required_payload if isinstance(tool_result.action_required_payload, dict) else None
        issue_entries = candidate.issues if candidate is not None else self._extract_issue_entries(payload or {})
        internal_error_reason = self._detect_followup_prompt_internal_error(tool_result)
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempt_number": attempt_number,
            "tool_names": [call.name for call in tool_calls],
            "result_status": (
                str(payload.get("status") or "")
                if payload is not None
                else ("review_required" if tool_result.review_required else "completed")
            ),
            "result_action": str(payload.get("action") or "") if payload is not None else "",
            "candidate_present": candidate is not None,
            "comparable": candidate.comparable if candidate is not None else False,
            "outcome_stage": candidate.outcome_stage if candidate is not None else "",
            "decision_reason": candidate.decision_reason if candidate is not None else "",
            "internal_error_reason": internal_error_reason,
            "structurally_valid": candidate.structurally_valid if candidate is not None else False,
            "quality_class": candidate.quality_class if candidate is not None else None,
            "structural_p1_measures": candidate.structural_p1_measures if candidate is not None else 0,
            "p2_measures": candidate.p2_measures if candidate is not None else 0,
            "out_of_scope_changed_section_count": (
                candidate.out_of_scope_changed_section_count if candidate is not None else 0
            ),
            "plan_delta_size": candidate.plan_delta_size if candidate is not None else 0,
            "candidate_tuple": list(candidate.candidate_tuple) if candidate is not None else None,
            "replaced_best_valid": replaced_best_valid,
            "replaced_best_invalid": replaced_best_invalid,
            "baseline_plan_source_for_next_repair": baseline_plan_source_for_next_repair,
            "used_bootstrap_plan_baseline": used_bootstrap_plan_baseline,
            "issue_codes": [str(issue.get("rule") or issue.get("code") or "") for issue in issue_entries],
            "issue_severities": [str(issue.get("rule_severity") or "") for issue in issue_entries],
            "issue_domains": [str(issue.get("rule_domain") or "") for issue in issue_entries],
            **self._serialize_other_p1_measures(candidate.other_p1_measures if candidate is not None else 0),
        }

    def _build_attempt_message_entry(
        self,
        *,
        attempt_number: int,
        final_message: str,
        thought_summary: str,
    ) -> Optional[Dict[str, Any]]:
        """Build a UI-facing preprocess attempt entry for the chat bubble."""
        message = str(final_message or "").strip()
        thought = str(thought_summary or "").strip()
        if not message and not thought:
            return None
        entry: Dict[str, Any] = {"attempt_number": attempt_number}
        if message:
            entry["message"] = message
        if thought:
            entry["thought_summary"] = thought
        return entry

    def _attach_attempt_messages(
        self,
        response: Dict[str, Any],
        attempt_messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Attach preprocess attempt display entries to response details."""
        if not attempt_messages:
            return response
        next_response = dict(response)
        details = next_response.get("details")
        next_details: Dict[str, Any]
        if isinstance(details, dict):
            next_details = dict(details)
        else:
            next_details = {}
        next_details["attempt_messages"] = copy.deepcopy(attempt_messages)
        next_response["details"] = next_details
        return next_response

    def _should_start_preprocess_job(self, tool_calls: List[ToolCall]) -> bool:
        """Return True when the turn should switch to background preprocess progress."""
        if not tool_calls:
            return False
        tool_names = {call.name for call in tool_calls}
        return "preprocess_voice_parts" in tool_names and "synthesize" not in tool_names

    def _selection_requested(
        self,
        part_id: Optional[str],
        part_index: Optional[int],
        verse_number: Optional[object],
    ) -> bool:
        """Return True if any selection parameters were provided."""
        return part_id is not None or part_index is not None or verse_number is not None

    def _available_verses(
        self, score_summary: Optional[Dict[str, Any]]
    ) -> List[str]:
        """Return normalized available verses from score summary."""
        if not isinstance(score_summary, dict):
            return []
        raw = score_summary.get("available_verses")
        if not isinstance(raw, list):
            return []
        out: List[str] = []
        for entry in raw:
            text = self._normalize_verse_number(entry)
            if text and text not in out:
                out.append(text)
        return out

    def _extract_call_requested_verse(self, call: ToolCall) -> Optional[str]:
        """Extract explicit requested verse from a tool call when provided."""
        if call.name == "synthesize":
            return self._normalize_verse_number(call.arguments.get("verse_number"))
        if call.name == "reparse":
            return self._normalize_verse_number(call.arguments.get("verse_number"))
        if call.name == "preprocess_voice_parts":
            direct = self._normalize_verse_number(call.arguments.get("verse_number"))
            if direct:
                return direct
            request = call.arguments.get("request")
            if isinstance(request, dict):
                request_verse = self._normalize_verse_number(request.get("verse_number"))
                if request_verse:
                    return request_verse
                plan = request.get("plan")
                if isinstance(plan, dict):
                    plan_verse = self._normalize_verse_number(plan.get("verse_number"))
                    if plan_verse:
                        return plan_verse
                    targets = plan.get("targets")
                    if isinstance(targets, list):
                        for entry in targets:
                            if not isinstance(entry, dict):
                                continue
                            target_verse = self._normalize_verse_number(entry.get("verse_number"))
                            if target_verse:
                                return target_verse
        return None

    def _tool_calls_require_verse_selection(
        self,
        tool_calls: List[ToolCall],
        *,
        score: Dict[str, Any],
        score_summary: Optional[Dict[str, Any]],
        explicit_verse_number: Optional[str],
    ) -> bool:
        """Return True when render-path tool calls must be blocked for verse choice."""
        available_verses = self._available_verses(score_summary)
        if len(available_verses) <= 1:
            return False
        selected_verse_number = self._score_selected_verse_number(score)
        for call in tool_calls:
            if call.name not in {"preprocess_voice_parts", "synthesize"}:
                continue
            requested_verse_number = self._extract_call_requested_verse(call)
            if requested_verse_number:
                return False
            if (
                explicit_verse_number
                and selected_verse_number
                and explicit_verse_number == selected_verse_number
            ):
                return False
            return True
        return False

    def _build_verse_selection_required_action(
        self,
        *,
        score: Dict[str, Any],
        score_summary: Optional[Dict[str, Any]],
        tool_attempted: str,
        explicit_verse_number: Optional[str],
    ) -> Dict[str, Any]:
        """Build action_required payload when explicit verse selection is required."""
        available_verses = self._available_verses(score_summary)
        selected_verse_number = self._score_selected_verse_number(score)
        return {
            "status": "action_required",
            "action": "verse_selection_required",
            "code": "verse_selection_required",
            "reason": "multi_verse_selection_required_before_render",
            "message": "Select a verse before preprocessing or synthesis.",
            "available_verses": available_verses,
            "selected_verse_number": selected_verse_number,
            "failed_validation_rules": [
                "verse_selection.required_for_multi_verse_render"
            ],
            "diagnostics": {
                "tool_attempted": tool_attempted,
                "requested_verse_number": None,
                "selected_verse_number": selected_verse_number,
                "available_verses_count": len(available_verses),
                "explicit_verse_number": explicit_verse_number,
            },
        }

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
        if "timed out" in message.lower():
            return message
        json_start = message.find("{")
        json_message = message if json_start == 0 else message[json_start:] if json_start != -1 else ""
        if not json_message:
            return message
        try:
            payload = json.loads(json_message)
        except json.JSONDecodeError:
            return message
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                detail = error.get("message")
                if code is not None and detail:
                    return f"LLM error {code}: {detail}"
        return message

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
            last_preprocess_plan = snapshot.get("last_preprocess_plan")
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
                parsed_score_json=(
                    planning_score
                    if (
                        self._settings.inject_full_parsed_score_json
                        and isinstance(planning_score, dict)
                    )
                    else None
                ),
                voice_part_signals=voice_part_signals,
                preprocess_mapping_context=preprocess_mapping_context,
                last_preprocess_plan=(
                    last_preprocess_plan
                    if isinstance(last_preprocess_plan, dict)
                    else None
                ),
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
            self._logger.warning(
                "llm_response_parse_failed raw_text=%s",
                summarize_payload(text),
            )
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
            last_preprocess_plan = snapshot.get("last_preprocess_plan")
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
                parsed_score_json=(
                    planning_score
                    if (
                        self._settings.inject_full_parsed_score_json
                        and isinstance(planning_score, dict)
                    )
                    else None
                ),
                voice_part_signals=voice_part_signals,
                preprocess_mapping_context=preprocess_mapping_context,
                last_preprocess_plan=(
                    last_preprocess_plan
                    if isinstance(last_preprocess_plan, dict)
                    else None
                ),
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
            self._logger.warning(
                "llm_followup_parse_failed raw_text=%s",
                summarize_payload(text),
            )
            prose_fallback = self._extract_followup_prose_fallback(text)
            if prose_fallback:
                return (
                    LlmResponse(
                        tool_calls=[],
                        final_message=prose_fallback,
                        include_score=False,
                    ),
                    None,
                )
            return None, tool_summary
        return response, None

    def _extract_followup_prose_fallback(self, text: str) -> str:
        """Extract a safe user-facing followup message from malformed LLM output."""
        cleaned = text.strip()
        if not cleaned:
            return ""
        final_message_match = re.search(
            r'"final_message"\s*:\s*"((?:\\.|[^"\\])*)"',
            cleaned,
            flags=re.DOTALL,
        )
        if final_message_match:
            encoded_message = f"\"{final_message_match.group(1)}\""
            try:
                parsed_message = json.loads(encoded_message)
            except json.JSONDecodeError:
                parsed_message = final_message_match.group(1)
            if isinstance(parsed_message, str) and parsed_message.strip():
                return parsed_message.strip()
        if cleaned.startswith("{") and '"tool_calls"' in cleaned:
            return "Preprocessing is still being adjusted. Please try again."
        return cleaned

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
        explicit_verse_number: Optional[str],
    ) -> "ToolExecutionResult":
        """Execute allowed tool calls and update session state."""
        current_score = score
        audio_response: Optional[Dict[str, Any]] = None
        reparse_completed_this_batch = False
        reparse_selected_verse: Optional[str] = None
        reparse_noop_this_batch = False
        selected_explicit_verse_number = explicit_verse_number
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
                normalized_reparse_verse = self._normalize_verse_number(reparse_verse_number)
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
                    if normalized_reparse_verse:
                        selected_explicit_verse_number = normalized_reparse_verse
                        await self._sessions.set_metadata(
                            session_id,
                            EXPLICIT_VERSE_METADATA_KEY,
                            normalized_reparse_verse,
                        )
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
                    if normalized_reparse_verse:
                        selected_explicit_verse_number = normalized_reparse_verse
                        await self._sessions.set_metadata(
                            session_id,
                            EXPLICIT_VERSE_METADATA_KEY,
                            normalized_reparse_verse,
                        )
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
                if self._tool_calls_require_verse_selection(
                    [call],
                    score=current_score,
                    score_summary=score_summary,
                    explicit_verse_number=selected_explicit_verse_number,
                ):
                    action_required = self._build_verse_selection_required_action(
                        score=current_score,
                        score_summary=score_summary,
                        tool_attempted=call.name,
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": ""},
                        followup_prompt=json.dumps(action_required, sort_keys=True),
                        action_required_payload=action_required,
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                preprocess_args = dict(call.arguments)
                requested_plan = self._extract_preprocess_plan(preprocess_args)
                if requested_plan is not None:
                    # Keep only the latest attempted preprocess plan in prompt context.
                    await self._sessions.set_last_preprocess_plan(
                        session_id, requested_plan
                    )
                try:
                    preprocess_score = await self._resolve_preprocess_score(
                        session_id,
                        user_id=user_id,
                    )
                except ValueError as exc:
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": str(exc)},
                        explicit_verse_number=selected_explicit_verse_number,
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
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response=None,
                        followup_prompt=json.dumps(result, sort_keys=True),
                        review_required=True,
                        explicit_verse_number=selected_explicit_verse_number,
                    )
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
                    review_materialization = result.pop("review_materialization", None)
                    followup_prompt = json.dumps(result, sort_keys=True)
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": ""},
                        followup_prompt=followup_prompt,
                        action_required_payload=result,
                        review_materialization=review_materialization,
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                continue
            if call.name == "synthesize":
                if self._tool_calls_require_verse_selection(
                    [call],
                    score=current_score,
                    score_summary=score_summary,
                    explicit_verse_number=selected_explicit_verse_number,
                ):
                    action_required = self._build_verse_selection_required_action(
                        score=current_score,
                        score_summary=score_summary,
                        tool_attempted=call.name,
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={"type": "chat_text", "message": ""},
                        followup_prompt=json.dumps(action_required, sort_keys=True),
                        action_required_payload=action_required,
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                if self._score_has_review_pending(current_score):
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
                            "message": backend_message("account.locked_negative_balance"),
                        },
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                if user_credits.is_expired:
                    return ToolExecutionResult(
                        score=current_score, 
                        audio_response={
                            "type": "chat_text", 
                            "message": backend_message("account.free_trial_expired"),
                        },
                        explicit_verse_number=selected_explicit_verse_number,
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
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                if requested_verse_number is not None:
                    selected_explicit_verse_number = requested_verse_number
                    await self._sessions.set_metadata(
                        session_id,
                        EXPLICIT_VERSE_METADATA_KEY,
                        requested_verse_number,
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
                        explicit_verse_number=selected_explicit_verse_number,
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
                reserve_result = await retry_credit_op(
                    reserve_credits,
                    user_id,
                    job_id,
                    est_credits,
                    self._settings.session_ttl_seconds,
                    session_id=session_id,
                    max_attempts=self._settings.credit_retry_max_attempts,
                    base_delay=self._settings.credit_retry_base_delay_seconds,
                )
                if reserve_result.status in {"insufficient_balance", "overdrafted"}:
                    followup_prompt = json.dumps(
                        {
                            "error": {
                                "type": "insufficient_credits",
                                "message": backend_message(
                                    "account.insufficient_credits",
                                    estimated_credits=est_credits,
                                    available_credits=user_credits.available_balance,
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
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                if reserve_result.status == "expired":
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={
                            "type": "chat_text",
                            "message": backend_message("account.free_trial_expired"),
                        },
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                if reserve_result.status not in {"reserved", "reservation_exists"}:
                    self._logger.error(
                        "credit_reservation_failed session=%s job=%s status=%s",
                        session_id,
                        job_id,
                        reserve_result.status,
                    )
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={
                            "type": "chat_text",
                            "message": backend_message("billing.setup_failed_retry"),
                        },
                        explicit_verse_number=selected_explicit_verse_number,
                    )
                try:
                    audio_response = await self._start_synthesis_job(
                        session_id, current_score, synth_args, user_id=user_id, job_id=job_id
                    )
                except Exception as exc:
                    from src.backend.credits import (
                        mark_reservation_reconciliation_required,
                    )

                    release_result = await retry_credit_op(
                        self._release_credits_with_retry_fault_injection,
                        user_id,
                        job_id,
                        max_attempts=self._settings.credit_retry_max_attempts,
                        base_delay=self._settings.credit_retry_base_delay_seconds,
                    )
                    self._logger.exception(
                        "synthesis_job_start_failed session=%s job=%s error=%s",
                        session_id,
                        job_id,
                        exc,
                    )
                    if not self._release_result_allows_terminal_status(release_result.status):
                        await asyncio.to_thread(
                            mark_reservation_reconciliation_required,
                            user_id,
                            job_id,
                            last_error="start_synthesis_job_failed",
                            last_error_message=str(exc),
                        )
                        message = backend_message(
                            "job.start_failed_billing_rollback_pending"
                        )
                    else:
                        message = backend_message("job.start_failed_retry")
                    return ToolExecutionResult(
                        score=current_score,
                        audio_response={
                            "type": "chat_text",
                            "message": message,
                        },
                        explicit_verse_number=selected_explicit_verse_number,
                    )
        # If a reparse succeeded but no downstream tool produced a terminal response in this
        # batch, issue one internal follow-up LLM turn with refreshed score context and let
        # the model decide whether to synthesize directly or preprocess for the new verse.
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
                    ),
                    "selected_verse_number": reparse_selected_verse,
                },
                sort_keys=True,
            )
            return ToolExecutionResult(
                score=current_score,
                audio_response={"type": "chat_text", "message": ""},
                followup_prompt=reparse_prompt,
                explicit_verse_number=selected_explicit_verse_number,
            )
        return ToolExecutionResult(
            score=current_score,
            audio_response=audio_response,
            explicit_verse_number=selected_explicit_verse_number,
        )

    def _extract_preprocess_plan(self, preprocess_args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the request.plan object from a preprocess tool call, if present."""
        request = preprocess_args.get("request")
        if not isinstance(request, dict):
            return None
        plan = request.get("plan")
        if not isinstance(plan, dict):
            return None
        return copy.deepcopy(plan)

    def _extract_preprocess_plan_from_tool_calls(
        self, tool_calls: List[ToolCall]
    ) -> Optional[Dict[str, Any]]:
        """Return the first preprocess plan from a tool-call batch, if present."""
        for call in tool_calls:
            if call.name != "preprocess_voice_parts":
                continue
            if not isinstance(call.arguments, dict):
                continue
            plan = self._extract_preprocess_plan(call.arguments)
            if isinstance(plan, dict):
                return plan
        return None

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
    review_required: bool = False
    action_required_payload: Optional[Dict[str, Any]] = None
    review_materialization: Optional[Dict[str, Any]] = None
    explicit_verse_number: Optional[str] = None


@dataclass(frozen=True)
class WorkflowCandidate:
    """A preprocess candidate evaluated during bounded repair."""
    attempt_number: int
    score: Dict[str, Any]
    message: str
    plan: Optional[Dict[str, Any]]
    review_required: bool
    outcome_stage: str
    outcome_preference: int
    comparable: bool
    quality_class: int
    structurally_valid: bool
    structural_p1_measures: int
    other_p1_measures: int
    p2_measures: int
    structural_issue_keys: List[str]
    other_p1_issue_keys: List[str]
    candidate_tuple: Tuple[int, int, int, int, int]
    issues: List[Dict[str, Any]]
    target_results: List[Dict[str, Any]]
    result_payload: Dict[str, Any]
    action_required_payload: Optional[Dict[str, Any]] = None
    review_materialization: Optional[Dict[str, Any]] = None
    out_of_scope_changed_section_count: int = 0
    plan_delta_size: int = 0
    section_change_ratio: float = 0.0
    measure_change_ratio: float = 0.0
    decision_reason: str = ""


@dataclass(frozen=True)
class BootstrapPlanBaseline:
    """Concrete lint-failed preprocess plan used before any comparable candidate exists."""
    attempt_number: int
    plan: Dict[str, Any]
    action: str
    lint_findings: List[Dict[str, Any]]
    repair_scopes: List[Dict[str, Any]]


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
