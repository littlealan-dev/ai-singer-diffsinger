#!/usr/bin/env python3
"""Replay recorded preprocess jobs offline and record one result per job.

Each corpus entry (written by collect_preprocess_corpus.py) holds the exact
MusicXML a production preprocess job ran on and the request that started it.
This script runs that request through the current planner in-process, with the
production model settings, and writes a run directory that
compare_preprocess_runs.py reads:

    <out>/run.json
    <out>/jobs/<job_id>.json

It never touches production: sessions live in memory under data/replay/, the
job store is an in-memory stand-in, and Firestore is pointed at a closed local
port so an unexpected call fails instead of reaching a real project.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REPLAY_USER_ID = "replay-user"
REPLAY_USER_EMAIL = "replay@example.com"
REPLAY_DATA_DIR = "data/replay"

# Settings the replay always uses, whatever dev.env and local.env say: one
# in-process worker, in-memory sessions, no Cloud Storage, no GPU.
REPLAY_ENV_OVERRIDES = {
    "APP_ENV": "dev",
    "SESSION_STORE": "memory",
    "BACKEND_USE_STORAGE": "false",
    "BACKEND_DATA_DIR": REPLAY_DATA_DIR,
    "PREPROCESS_DISPATCH_MODE": "inline",
    "PREPROCESS_WORKER_ENABLED": "false",
    "SYNTHESIS_DISPATCH_MODE": "inline",
    "SYNTHESIS_WORKER_ENABLED": "false",
    "SYNTHESIS_PREMIUM_ROUTING": "off",
    "MCP_GPU_ENABLED": "false",
    "MCP_CPU_WORKERS": "1",
    # A closed port: any Firestore call fails fast instead of reaching a project.
    "FIRESTORE_EMULATOR_HOST": "127.0.0.1:9",
}

# Model and preprocess settings copied from env/prod.env, so the replay plans
# like production does. None of these is a secret.
PROD_ENV_KEYS = (
    "LLM_PROVIDER",
    "GEMINI_DEFAULT_MODEL",
    "GEMINI_DEFAULT_THINKING_LEVEL",
    "GEMINI_DEFAULT_TIMEOUT_SECONDS",
    "GEMINI_PREPROCESS_MODEL",
    "GEMINI_PREPROCESS_THINKING_LEVEL",
    "GEMINI_PREPROCESS_TIMEOUT_SECONDS",
    "GEMINI_INCLUDE_THOUGHT_SUMMARY",
    "GEMINI_PROMPT_CACHE_ENABLED",
    "PREPROCESS_MAX_ATTEMPTS",
    "PREPROCESS_INITIAL_PLAN_LLM_MAX_RESPONSES",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True, help="Corpus directory from collect_preprocess_corpus.py.")
    parser.add_argument("--out", required=True, help="Run directory to write (outside the repo).")
    parser.add_argument("--label", required=True, help="Run label, e.g. v1-baseline.")
    parser.add_argument("--planner", default="v1", choices=("v1", "v2"))
    parser.add_argument("--job", action="append", default=[], help="Replay only this job id (repeatable).")
    parser.add_argument("--limit", type=int, default=0, help="Replay at most this many jobs (0 = all).")
    parser.add_argument("--force", action="store_true", help="Replay jobs that already have a result.")
    parser.add_argument("--keep-sessions", action="store_true", help="Keep data/replay session folders.")
    parser.add_argument(
        "--static-llm-responses",
        help="Smoke-test mode: a JSON file with a list of fixed LLM replies, used instead of Gemini.",
    )
    return parser.parse_args()


def _load_env_file(path: Path) -> Dict[str, str]:
    """Return the variables a shell would set by sourcing the file (bash `set -a`)."""
    if not path.is_file():
        return {}
    completed = subprocess.run(
        ["bash", "-c", 'set -a; . "$1" >/dev/null 2>&1; env -0', "_", str(path)],
        check=True,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "")},
    )
    values: Dict[str, str] = {}
    for item in completed.stdout.split(b"\0"):
        if b"=" in item:
            key, _, value = item.partition(b"=")
            values[key.decode()] = value.decode()
    return values


def _read_prod_model_settings() -> Dict[str, str]:
    values: Dict[str, str] = {}
    prod_env = PROJECT_ROOT / "env" / "prod.env"
    for raw in prod_env.read_text(encoding="utf-8").splitlines():
        key, sep, value = raw.strip().partition("=")
        if sep and key in PROD_ENV_KEYS:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _prepare_environment(label: str, static_responses: Optional[str] = None) -> None:
    """Build the process environment before any backend module reads it."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key and static_responses is None:
        raise SystemExit(
            "GEMINI_API_KEY is not set. Run this from a shell that has the key; "
            "the replay never reads it from Secret Manager."
        )
    merged: Dict[str, str] = {}
    for name in ("dev.env", "local.env"):
        merged.update(_load_env_file(PROJECT_ROOT / "env" / name))
    merged.update(_read_prod_model_settings())
    merged.update(REPLAY_ENV_OVERRIDES)
    # One build id per label keeps one Gemini prompt cache for the whole run.
    merged["BACKEND_BUILD_ID"] = f"replay-{label}"
    for key, value in merged.items():
        if key == "GEMINI_API_KEY":
            continue
        os.environ[key] = value
    os.environ["GEMINI_API_KEY"] = gemini_key
    if static_responses is not None:
        # Smoke-test mode never builds a Gemini client.
        os.environ["LLM_PROVIDER"] = "none"
    for key in ("GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_SERVICE_ACCOUNT_FILE"):
        os.environ.pop(key, None)


DEFAULT_SMOKE_MESSAGE = {"tool_calls": [], "final_message": "Done.", "include_score": False}


class SequenceLlmClient:
    """Smoke-test LLM: returns fixed replies in order, one queue per role.

    The app's static client answers any tool-result prompt with plain text, and
    the planner's request arrives as one, so it cannot stand in for the planner.
    Queues are per role because a repair notice (default role) runs alongside
    the repair plan (preprocess role).
    """

    def __init__(self, replies: Any) -> None:
        if isinstance(replies, list):
            replies = {"preprocess": replies}
        self._queues = {
            role: [reply if isinstance(reply, str) else json.dumps(reply) for reply in items]
            for role, items in replies.items()
        }
        self._index: Dict[str, int] = {}
        self._lock = threading.Lock()

    def generate(self, *args: Any, **kwargs: Any) -> str:
        role = getattr(kwargs.get("role"), "value", "default")
        with self._lock:
            queue = self._queues.get(role) or [json.dumps(DEFAULT_SMOKE_MESSAGE)]
            index = self._index.get(role, 0)
            self._index[role] = index + 1
        return queue[min(index, len(queue) - 1)]


class FakeJobStore:
    """In-memory stand-in for the Firestore job store; keeps every update."""

    def __init__(self) -> None:
        self.updates: List[Dict[str, Any]] = []
        self.unexpected_calls: List[str] = []

    def update_job(self, job_id: str, **fields: Any) -> None:
        self.updates.append(copy.deepcopy(fields))

    def final_fields(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for update in self.updates:
            merged.update(update)
        return merged

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.unexpected_calls.append(name)
            return None

        return record


class UsageRecorder(logging.Handler):
    """Collect the gemini_usage log records the Gemini client emits per call."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "event", None) != "gemini_usage":
            return
        with self._lock:
            self.records.append(
                {
                    "role": getattr(record, "role", None),
                    "model": getattr(record, "model", None),
                    "prompt_tokens": getattr(record, "prompt_token_count", None) or 0,
                    "cached_tokens": getattr(record, "cached_content_token_count", None) or 0,
                    "thinking_tokens": getattr(record, "thoughts_token_count", None) or 0,
                    "output_tokens": getattr(record, "candidates_token_count", None) or 0,
                }
            )


class TimedLlmClient:
    """Wrap the LLM client to time each call and pair it with its usage record."""

    def __init__(self, inner: Any, usage: UsageRecorder, job: "JobRecorder") -> None:
        self._inner = inner
        self._usage = usage
        self._job = job

    def generate(self, *args: Any, **kwargs: Any) -> str:
        role = kwargs.get("role")
        role_name = getattr(role, "value", str(role or "default"))
        before = len(self._usage.records)
        started = time.monotonic()
        try:
            return self._inner.generate(*args, **kwargs)
        finally:
            latency = time.monotonic() - started
            # A repair notice (default role) can run alongside a repair plan
            # (preprocess role), so match the usage record by role as well.
            new_records = [
                record for record in self._usage.records[before:] if record.get("role") == role_name
            ]
            usage = new_records[-1] if new_records else {}
            self._job.record_llm_call(role_name, usage, latency)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class JobRecorder:
    """Per-job state: LLM calls, executed attempts and candidates, in order."""

    def __init__(self) -> None:
        self.llm_calls: List[Dict[str, Any]] = []
        self.candidates: List[Any] = []
        # attempt number -> issue entries from that attempt's tool result
        self.attempt_issues: Dict[int, List[Dict[str, Any]]] = {}

    def record_llm_call(self, role: str, usage: Dict[str, Any], latency: float) -> None:
        if role == "preprocess":
            phase = "repair" if self.attempt_issues else "initial"
        elif role == "default":
            phase = "message"
        else:
            phase = "other"
        self.llm_calls.append(
            {
                "role": role,
                "model": usage.get("model") or "",
                "phase": phase,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
                "thinking_tokens": int(usage.get("thinking_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "latency_s": round(latency, 3),
            }
        )


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _corpus_job_ids(corpus: Path, only: List[str], limit: int) -> List[str]:
    job_ids = sorted(
        entry.name
        for entry in corpus.iterdir()
        if (entry / "job.json").is_file() and (entry / "input.xml").is_file()
    )
    if only:
        wanted = set(only)
        job_ids = [job_id for job_id in job_ids if job_id in wanted]
    if limit > 0:
        job_ids = job_ids[:limit]
    return job_ids


def _finding_from_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    rule = issue.get("rule") or issue.get("code") or issue.get("rule_code") or issue.get("rule_id")
    severity = issue.get("rule_severity") or issue.get("severity")
    domain = issue.get("rule_domain") or issue.get("domain")
    part_index = issue.get("part_index")
    voice_part = issue.get("target_voice_part_id") or issue.get("target_voice_part")
    target = None
    if part_index is not None or voice_part is not None:
        target = f"{part_index}:{voice_part}"
    measures = None
    attributes = issue.get("failing_attributes") if isinstance(issue.get("failing_attributes"), dict) else {}
    start = issue.get("start_measure", attributes.get("start_measure"))
    end = issue.get("end_measure", attributes.get("end_measure"))
    if start is None and isinstance(issue.get("measure_range"), (list, tuple)) and len(issue["measure_range"]) == 2:
        start, end = issue["measure_range"]
    if start is not None:
        measures = [start, end if end is not None else start]
    return {
        "rule": str(rule or "unknown"),
        "severity": str(severity or "unknown"),
        "domain": str(domain or ""),
        "target": target,
        "measures": measures,
    }


def _note_signature(notes: List[Dict[str, Any]]) -> List[tuple]:
    return [
        (
            note.get("measure_number"),
            round(float(note.get("offset_beats") or 0.0), 4),
            note.get("pitch_midi"),
            round(float(note.get("duration_beats") or 0.0), 4),
            note.get("lyric"),
        )
        for note in notes
        if not note.get("is_rest")
    ]


def _lanes(original: Dict[str, Any], final: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the parts the job created or changed, as comparable lanes."""
    original_parts = {
        str(part.get("part_id")): _note_signature(part.get("notes") or [])
        for part in original.get("parts") or []
    }
    lanes: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for part in final.get("parts") or []:
        part_id = str(part.get("part_id"))
        notes = [note for note in part.get("notes") or [] if not note.get("is_rest")]
        if original_parts.get(part_id) == _note_signature(notes):
            continue
        # A derived part's id hashes its transform, so an equivalent plan written
        # differently gets another id; its name (source part and voice part) does not.
        part_name = str(part.get("part_name") or "")
        lane_id = part_name if part_id.startswith("P_DERIVED_") else f"{part_id}|{part_name}"
        seen[lane_id] = seen.get(lane_id, 0) + 1
        if seen[lane_id] > 1:
            lane_id = f"{lane_id} #{seen[lane_id]}"
        lanes.append(
            {
                "lane_id": lane_id,
                "notes": [
                    {
                        "measure": note.get("measure_number"),
                        "offset_beats": note.get("offset_beats"),
                        "pitch_midi": note.get("pitch_midi"),
                        "duration_beats": note.get("duration_beats"),
                        "lyric": note.get("lyric"),
                    }
                    for note in notes
                ],
            }
        )
    return lanes


async def _replay_job(
    *,
    job_id: str,
    corpus: Path,
    settings: Any,
    router: Any,
    llm_client: Any,
    usage: UsageRecorder,
    keep_sessions: bool,
) -> Dict[str, Any]:
    from src.backend import orchestrator as orchestrator_module
    from src.backend.orchestrator import Orchestrator, ToolCall
    from src.backend.session import SessionStore

    job = json.loads((corpus / job_id / "job.json").read_text(encoding="utf-8"))
    spec = job.get("spec") or {}
    recorder = JobRecorder()
    result: Dict[str, Any] = {
        "job_id": job_id,
        "status": "error",
        "error": None,
        "wall_time_s": 0.0,
        "attempts": 0,
        "quality_class": None,
        "review_required": False,
        "findings": [],
        "llm_calls": recorder.llm_calls,
        "lanes": [],
    }

    sessions = SessionStore(
        project_root=settings.project_root,
        sessions_dir=settings.sessions_dir,
        ttl_seconds=settings.session_ttl_seconds,
        max_sessions=settings.max_sessions,
        backend_use_storage=False,
        storage_bucket=settings.storage_bucket,
    )
    orchestrator = Orchestrator(router, sessions, settings, TimedLlmClient(llm_client, usage, recorder))
    job_store = FakeJobStore()
    orchestrator._job_store = job_store

    async def no_credits(_user_id: Optional[str]) -> None:
        return None

    orchestrator._get_current_credit_availability = no_credits  # type: ignore[method-assign]
    build_candidate = orchestrator._build_workflow_candidate

    def recording_build_candidate(*args: Any, **kwargs: Any) -> Any:
        candidate = build_candidate(*args, **kwargs)
        if candidate is not None:
            recorder.candidates.append(candidate)
        return candidate

    orchestrator._build_workflow_candidate = recording_build_candidate  # type: ignore[method-assign]
    persist_attempt = orchestrator._persist_preprocess_attempt_artifacts

    async def recording_persist_attempt(**kwargs: Any) -> Any:
        tool_result = kwargs.get("tool_result")
        if tool_result is not None:
            payload = tool_result.action_required_payload
            recorder.attempt_issues[int(kwargs["attempt_number"])] = (
                orchestrator._extract_issue_entries(payload) if isinstance(payload, dict) else []
            )
        return await persist_attempt(**kwargs)

    orchestrator._persist_preprocess_attempt_artifacts = recording_persist_attempt  # type: ignore[method-assign]

    session_id: Optional[str] = None
    try:
        session_id = (await sessions.create_session(REPLAY_USER_ID)).id
        upload_dir = sessions.session_dir(session_id) / "scores" / "replay" / "upload"
        upload_dir.mkdir(parents=True, exist_ok=True)
        musicxml_path = upload_dir / "score.xml"
        shutil.copyfile(corpus / job_id / "input.xml", musicxml_path)
        rel_path = str(musicxml_path.relative_to(settings.project_root))
        score = await asyncio.to_thread(
            router.call_tool, "parse_score", {"file_path": rel_path, "expand_repeats": False}
        )
        score = dict(score)
        score["source_musicxml_path"] = str(musicxml_path)
        summary = score.pop("score_summary", None)
        files = {
            "musicxml_path": rel_path,
            "uploaded_musicxml_path": rel_path,
            "musicxml_name": job.get("input_file_name") or "score.xml",
        }
        await sessions.commit_uploaded_score(
            session_id, score_id=uuid.uuid4().hex, score=score, summary=summary, files=files
        )
        original_score = copy.deepcopy(score)
        tool_calls = [
            ToolCall(name=str(entry["name"]), arguments=dict(entry.get("arguments") or {}))
            for entry in spec.get("toolCalls") or []
            if isinstance(entry, dict) and entry.get("name")
        ]
        planning_context = spec.get("planningContext")
        orchestrator_module._llm_turn_budget.set(
            orchestrator_module._LlmTurnBudget(
                reference=job_id[:8], started_at=time.monotonic(), calls=0
            )
        )
        started = time.monotonic()
        await orchestrator._run_preprocess_job(
            session_id,
            copy.deepcopy(score),
            tool_calls,
            job_id,
            REPLAY_USER_ID,
            REPLAY_USER_EMAIL,
            initial_message=str(spec.get("initialMessage") or ""),
            initial_thought_summary=str(spec.get("initialThoughtSummary") or ""),
            planning_context=planning_context if isinstance(planning_context, dict) else None,
        )
        result["wall_time_s"] = round(time.monotonic() - started, 3)

        final = job_store.final_fields()
        details = final.get("details") if isinstance(final.get("details"), dict) else {}
        result["status"] = "completed" if final.get("status") == "completed" else "failed"
        result["error"] = final.get("errorMessage")
        result["review_required"] = bool(final.get("reviewRequired"))
        result["attempts"] = max(recorder.attempt_issues, default=0)
        chosen = None
        attempt = details.get("attempt_number")
        for candidate in recorder.candidates:
            if attempt is not None and candidate.attempt_number == attempt:
                chosen = candidate
        if chosen is None and recorder.candidates:
            chosen = max(recorder.candidates, key=lambda c: (c.quality_class, c.attempt_number))
        if details.get("quality_class") is not None:
            result["quality_class"] = details.get("quality_class")
        elif chosen is not None:
            result["quality_class"] = chosen.quality_class
        issues = details.get("issues") if isinstance(details.get("issues"), list) else None
        if issues is None and chosen is not None:
            issues = chosen.issues
        if issues is None and recorder.attempt_issues:
            # No candidate was kept (e.g. every plan failed lint): report the last attempt.
            issues = recorder.attempt_issues[max(recorder.attempt_issues)]
        result["findings"] = [
            _finding_from_issue(issue) for issue in issues or [] if isinstance(issue, dict)
        ]
        snapshot = await sessions.get_snapshot(session_id, REPLAY_USER_ID)
        final_score = (snapshot.get("current_score") or {}).get("score") or {}
        result["lanes"] = _lanes(original_score, final_score)
        if job_store.unexpected_calls:
            result["unexpected_job_store_calls"] = sorted(set(job_store.unexpected_calls))
    except Exception as exc:  # noqa: BLE001 - one bad job must not stop the run.
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if session_id is not None and not keep_sessions:
            shutil.rmtree(sessions.session_dir(session_id), ignore_errors=True)
    return result


async def _main_async(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if out == PROJECT_ROOT or PROJECT_ROOT in out.parents:
        raise SystemExit("--out must be outside the repository: results contain user scores.")
    static_responses = None
    if args.static_llm_responses:
        static_responses = Path(args.static_llm_responses).read_text(encoding="utf-8")
    _prepare_environment(args.label, static_responses)

    from src.backend.config import Settings
    from src.backend.llm_factory import create_llm_client
    from src.backend.mcp_client import McpRouter

    settings = Settings.from_env()
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    usage = UsageRecorder()
    logging.getLogger("src.backend.llm_gemini").addHandler(usage)
    logging.getLogger("src.backend.llm_gemini").setLevel(logging.DEBUG)

    job_ids = _corpus_job_ids(corpus, args.job, args.limit)
    (out / "jobs").mkdir(parents=True, exist_ok=True)
    run_meta = {
        "label": args.label,
        "planner": args.planner,
        "git_sha": _git_sha(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.gemini_preprocess.model,
        "thinking_level": settings.gemini_preprocess.thinking_level,
        "corpus_dir": str(corpus),
        "job_ids": job_ids,
    }
    (out / "run.json").write_text(json.dumps(run_meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    router = McpRouter(settings)
    await asyncio.wrap_future(router.start_background())
    llm_client = (
        create_llm_client(settings)
        if static_responses is None
        else SequenceLlmClient(json.loads(static_responses))
    )
    try:
        for index, job_id in enumerate(job_ids, start=1):
            target = out / "jobs" / f"{job_id}.json"
            if target.exists() and not args.force:
                print(f"[{index}/{len(job_ids)}] {job_id} skipped (result exists)")
                continue
            result = await _replay_job(
                job_id=job_id,
                corpus=corpus,
                settings=settings,
                router=router,
                llm_client=llm_client,
                usage=usage,
                keep_sessions=args.keep_sessions,
            )
            target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(
                f"[{index}/{len(job_ids)}] {job_id} {result['status']} "
                f"q={result['quality_class']} attempts={result['attempts']} "
                f"llm_calls={len(result['llm_calls'])} {result['wall_time_s']}s"
                + (f" error={result['error']}" if result["status"] == "error" else "")
            )
    finally:
        router.stop()
    return 0


def main() -> int:
    return asyncio.run(_main_async(_parse_args()))


if __name__ == "__main__":
    sys.exit(main())
