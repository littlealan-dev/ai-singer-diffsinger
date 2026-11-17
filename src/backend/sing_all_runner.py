"""Carry out the durable decisions for one Sing All parent job."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from src.api.sing_all_estimate import quote_facts
from src.backend import sing_all_store
from src.backend.credits import release_credits, settle_instrumental_reservation
from src.backend.message_catalog import backend_message
from src.backend.sing_all import (
    decide, materialize_rows, mark_voice_ranges, summary_with_judged_instruments,
)
from src.musicxml.solfege import GENERATED_LYRIC_NAME


class SingAllTerminalError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SingAllRunner:
    """Reconcile one parent; a second wake can safely observe the same state."""

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def reconcile(self, job_id: str, *, sweep: bool = False,
                        now: datetime | None = None) -> None:
        for _ in range(64):
            job = await asyncio.to_thread(sing_all_store.load, job_id)
            if not job or job.get("status") != "running":
                return
            if job.get("cancelRequested"):
                for item in job.get("items") or []:
                    if item.get("status") not in {"queued", "running"} or not item.get("childJobId"):
                        continue
                    child = await asyncio.to_thread(
                        self.orchestrator._job_store.get_job, item["childJobId"])
                    if child and child.get("status") in {"queued", "running"}:
                        await asyncio.to_thread(
                            self.orchestrator._job_store.stop_job, item["childJobId"])
            snapshot = await self.orchestrator._sessions.get_snapshot(
                job["sessionId"], job["uid"])
            if (job["stage"] == "splitting"
                    and any(item["type"] == "split" and item["status"] == "done"
                            for item in job.get("items") or [])
                    and self.orchestrator._score_has_review_pending(
                        (snapshot.get("current_score") or {}).get("score") or {})):
                accepted = self.orchestrator._clear_review_pending(
                    snapshot["current_score"]["score"])
                await self.orchestrator._sessions.set_score(job["sessionId"], accepted)
                snapshot = await self.orchestrator._sessions.get_snapshot(
                    job["sessionId"], job["uid"])
            children = await self._children(job, snapshot)
            score_state = await self._score_state(job, snapshot, now=now)
            score_state["sweep"] = sweep
            fields, actions = decide(job, children, score_state)
            if not fields and not actions:
                return

            # Releasing holds and saving score answers must succeed before the
            # parent can move past them. A repeat of either operation is safe.
            before = [action for action in actions if action["kind"] in {
                "release", "save_assignments", "fail_stuck_child"}]
            for action in before:
                try:
                    outcome = await self._action(job, action, snapshot)
                    if (action["kind"] == "release" and outcome == "already_settled"
                            and "items" in fields):
                        charged = await asyncio.to_thread(
                            sing_all_store.settled_credits, action["reservationId"])
                        if charged is not None:
                            for item in fields["items"]:
                                reservation_id = item.get("reservationId") or item.get("childJobId")
                                if reservation_id == action["reservationId"]:
                                    item.update(status="done", reason=None, creditsCharged=charged)
                            if fields.get("stage") == "done":
                                takes = [item for item in fields["items"]
                                         if item["type"] == "take"]
                                if job.get("cancelRequested") and all(
                                    item["status"] == "done" for item in takes
                                ):
                                    fields.update(status="completed", endReason=None)
                                elif fields.get("endReason") == "stuck" and any(
                                    item["status"] == "done" for item in takes
                                ):
                                    fields["status"] = "partial"
                except SingAllTerminalError as exc:
                    await self._terminal_failure(job_id, exc.reason)
                    return
            if fields:
                terminal = fields.get("stage") == "done"
                if terminal:
                    fields = {**fields, "finalScoreVersion":
                              (snapshot.get("current_score") or {}).get("version")}
                write = sing_all_store.finish if terminal else sing_all_store.apply
                if not await asyncio.to_thread(
                    write, job_id, fields, expected_stage=job["stage"],
                    expected_revision=job.get("revision", 0)):
                    continue
                job = {**job, **fields, "revision": int(job.get("revision", 0)) + 1}
            for action in actions:
                if action not in before and not (fields and fields.get("stage") == "done"
                                                 and action["kind"] in {"clear_active", "append_end_history"}):
                    try:
                        await self._action(job, action, snapshot)
                    except SingAllTerminalError as exc:
                        await self._terminal_failure(job_id, exc.reason)
                        return
            if job.get("status") != "running":
                return

    async def _terminal_failure(self, job_id: str, reason: str) -> None:
        """End a deterministic preparation failure before any take can start."""
        for _ in range(8):
            job = await asyncio.to_thread(sing_all_store.load, job_id)
            if not job or job.get("status") != "running":
                return
            fields, actions = decide(job, {}, {"terminalFailure": reason})
            for action in actions:
                if action["kind"] == "release":
                    await self._action(job, action, {})
            if await asyncio.to_thread(
                sing_all_store.finish, job_id, fields,
                expected_stage=job["stage"], expected_revision=job.get("revision", 0),
            ):
                return
        raise RuntimeError("Sing All parent changed throughout failure settlement.")

    async def _children(self, job: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        children: dict[str, dict[str, Any]] = {}
        score = (snapshot.get("current_score") or {}).get("score") or {}
        score_parts = score.get("parts") or []
        summary_parts = (snapshot.get("score_summary") or {}).get("parts") or []
        for item in job.get("items") or []:
            child_id = item.get("childJobId")
            if child_id:
                child = await asyncio.to_thread(self.orchestrator._job_store.get_job, child_id)
                if child:
                    child = dict(child)
                    if item["type"] == "split" and child.get("status") == "completed":
                        produced = []
                        for transform in (score.get("voice_part_transforms") or {}).values():
                            if not isinstance(transform, dict):
                                continue
                            source_index = transform.get("source_part_index")
                            if not isinstance(source_index, int) or source_index >= len(score_parts):
                                continue
                            source = score_parts[source_index]
                            if source.get("part_id") != item["sourcePartId"]:
                                continue
                            lane = transform.get("derived_lane") or {}
                            appended = transform.get("appended_part_ref") or {}
                            if not lane or appended.get("hidden_default_lane"):
                                continue
                            suffix = f":{transform.get('source_voice_part_id')}:slot{lane.get('slot')}"
                            matching = [row for row in job["rows"]
                                        if row.get("sourcePartId") == item["sourcePartId"]
                                        and row.get("split") and row["rowId"].endswith(suffix)]
                            if len(matching) != 1:
                                continue
                            derived_index = appended.get("part_index")
                            derived = next((part for part in summary_parts
                                            if part.get("part_index") == derived_index), None)
                            produced.append({"laneKey": matching[0]["rowId"],
                                             "partId": (derived or appended).get("part_id"),
                                             "name": (derived or appended).get("part_name")})
                        child["parts"] = produced
                        expected = {row["rowId"] for row in job["rows"]
                                    if row.get("split")
                                    and row.get("sourcePartId") == item["sourcePartId"]}
                        if (child.get("reviewRequired") and not child.get("actionRequired")
                                and {part["laneKey"] for part in produced} == expected):
                            # The chat workflow asks for human review after a
                            # valid split. Sing All already has the user's
                            # quoted lane choices and checks every produced lane.
                            child["reviewRequired"] = False
                    children[child_id] = child
        return children

    async def _score_state(self, job: dict[str, Any], snapshot: dict[str, Any], *,
                           now: datetime | None = None) -> dict[str, Any]:
        score = (snapshot.get("current_score") or {}).get("score") or {}
        state: dict[str, Any] = {
            "scoreId": snapshot.get("score_id"),
            "verse": score.get("selected_verse_number"),
            "now": now or datetime.now(timezone.utc),
        }
        if job["stage"] != "reading" or job.get("cancelRequested"):
            return state
        summary = snapshot.get("score_summary") or {}
        voices = await self.orchestrator._get_voicebank_details()
        facts = quote_facts(
            score, summary, voices, verse_number=job["choices"]["verse"],
            evidence_max_chars=self.orchestrator._settings.preprocess_chooser_evidence_max_chars,
        )
        rows = materialize_rows(facts, job["judgment"], job["choices"]["rows"])
        names = {str(part.get("part_id")): part.get("part_name")
                 for part in summary.get("parts") or [] if isinstance(part, dict)}
        for row in rows:
            row["partName"] = names.get(row["sourcePartId"]) or row["sourcePartId"]
        mark_voice_ranges(rows, {voice["id"]: voice for voice in voices})
        state["rows"] = rows
        return state

    async def _action(self, job: dict[str, Any], action: dict[str, Any],
                      snapshot: dict[str, Any]) -> str | None:
        kind = action["kind"]
        uid, session_id = job["uid"], job["sessionId"]
        if kind == "release":
            result = await asyncio.to_thread(release_credits, uid, action["reservationId"])
            if result.status not in {"released", "already_released", "already_settled",
                                     "reservation_missing"}:
                raise RuntimeError(f"Credit release: {result.status}")
            return result.status
        elif kind == "fail_stuck_child":
            await asyncio.to_thread(
                self.orchestrator._job_store.fail_job_if_active,
                action["childJobId"], message="Sing All child timed out.",
                error="stuck", job_kind=action["jobKind"],
            )
        elif kind == "clear_active":
            await asyncio.to_thread(sing_all_store.clear_active, uid, job["jobId"])
        elif kind == "append_end_history":
            takes = [item for item in job["items"] if item["type"] == "take"]
            await self.orchestrator._sessions.append_history(
                session_id, "assistant",
                backend_message("sing_all.ended_history", status=job["status"],
                                done=sum(item["status"] == "done" for item in takes),
                                takes=len(takes),
                                credits=sum(int(item.get("creditsCharged") or 0)
                                            for item in job["items"])),
                sing_all_job_id=job["jobId"],
            )
        elif kind == "save_assignments":
            summary = snapshot.get("score_summary") or {}
            try:
                amended = summary_with_judged_instruments(
                    summary, job["judgment"],
                    sung_source_ids={row["sourcePartId"] for row in job["rows"]
                                     if row.get("include")},
                )
            except ValueError as exc:
                raise SingAllTerminalError("quote_changed") from exc
            await self.orchestrator._sessions.set_score_summary(session_id, amended)
        elif kind == "reparse_verse":
            await self.orchestrator._reparse_score(
                session_id, part_id=None, part_index=None,
                verse_number=action["verse"], user_id=uid,
            )
        elif kind == "start_split":
            await self._start_split(job, action, snapshot)
        elif kind == "add_solfege":
            await self._add_solfege(job, snapshot)
        elif kind == "prepare_instrumentals":
            try:
                await self._prepare_instrumentals(job, snapshot)
            except Exception as exc:
                raise SingAllTerminalError("instrumental_failed") from exc
        elif kind == "start_take":
            try:
                await self._start_take(job, action, snapshot)
            except ValueError as exc:
                raise SingAllTerminalError("quote_changed") from exc
        elif kind == "settle_instrumental":
            await self._settle_instrumental(job)
        else:
            raise ValueError(f"Unknown Sing All action: {kind}")

    async def _start_split(self, job: dict[str, Any], action: dict[str, Any],
                           snapshot: dict[str, Any]) -> None:
        item = next(item for item in job["items"] if item["itemId"] == action["itemId"])
        part_id = item["sourcePartId"]
        request = {"part_id": part_id, "verse_number": job["choices"]["verse"],
                   "reason": "sing_all"}
        context = self.orchestrator._build_preprocess_workflow_planning_context(
            [], user_message="Sing All")
        context["target"] = {"part_id": part_id, "verse_number": job["choices"]["verse"]}
        context["request"] = request
        context["lane_layouts"] = {
            key: value for key, value in (job["judgment"].get("layouts") or {}).items()
            if key.startswith(f"{part_id}|")
        }
        result = await self.orchestrator._start_preprocess_job(
            job["sessionId"], snapshot["current_score"]["score"], [],
            initial_message="Preparing singing lines", initial_thought_summary="",
            user_id=job["uid"], user_email="", planning_context=context,
            parent={"jobId": job["jobId"], "itemId": item["itemId"],
                    "childJobId": item["childJobId"]},
        )
        if result.get("action_required"):
            raise SingAllTerminalError("split_failed")

    async def _add_solfege(self, job: dict[str, Any], snapshot: dict[str, Any]) -> None:
        session_id = job["sessionId"]
        rows = {item["rowId"] for item in job["items"]
                if item["type"] == "take" and item["mode"] == "solfege"}
        targets = {row["partId"] for row in job["rows"] if row["rowId"] in rows}
        source = await self.orchestrator._sessions.ensure_active_musicxml(session_id, job["uid"])
        output = self.orchestrator._sessions.session_dir(session_id) / f"score-solfege-{uuid.uuid4().hex}.xml"
        try:
            result = await asyncio.to_thread(
                self.orchestrator._router.call_tool, "add_solfege_lyric_verse",
                {"source_musicxml_path": self.orchestrator._mcp_musicxml_path(source),
                 "output_musicxml_path": str(output.relative_to(self.orchestrator._settings.project_root)),
                 "part_ids": sorted(targets),
                 "settings": dict(snapshot.get("solfege_settings") or {})},
            )
        except Exception:
            result = None
        if not isinstance(result, dict):
            result = {"skipped": [{"part_id": part_id} for part_id in targets]}
        if result.get("completed_targets"):
            await self.orchestrator._persist_solfege_result(session_id, result)
        skipped = {entry.get("part_id") for entry in result.get("skipped") or []}
        failed = [row["rowId"] for row in job["rows"] if row["partId"] in skipped]
        if not await asyncio.to_thread(
            sing_all_store.apply, job["jobId"],
            {"solfegePrepared": True, "solfegeFailedRows": failed,
             "solfegeSelections": {
                 target["part_id"]: target.get("lyric_selection")
                 for target in (result.get("completed_targets") or [])
                 + (result.get("already_present") or [])}},
            expected_stage="solfege", expected_revision=job.get("revision", 0),
        ):
            return

    async def _prepare_instrumentals(self, job: dict[str, Any],
                                     snapshot: dict[str, Any]) -> None:
        session_id = job["sessionId"]
        summary, publication = await self.orchestrator._ensure_instrumental_midi_artifacts(
            session_id, score_summary=snapshot.get("score_summary"), user_id=job["uid"],
            job_id=job["jobId"],
        )
        if publication:
            paths = await asyncio.to_thread(
                self.orchestrator._finalize_instrumental_midi_files, publication)
            await asyncio.to_thread(
                self.orchestrator._upload_instrumental_midi_files, publication, paths,
                user_id=job["uid"], session_id=session_id)
            await self.orchestrator._publish_instrumental_midi_session_state(
                session_id, publication, user_id=job["uid"])
        await asyncio.to_thread(
            sing_all_store.apply, job["jobId"],
            {"instrumentalsPrepared": True,
             "finalScoreVersion": (snapshot.get("current_score") or {}).get("version")},
            expected_stage="singing", expected_revision=job.get("revision", 0),
        )

    async def _start_take(self, job: dict[str, Any], action: dict[str, Any],
                          snapshot: dict[str, Any]) -> None:
        item = next(item for item in job["items"] if item["itemId"] == action["itemId"])
        row = next(row for row in job["rows"] if row["rowId"] == item["rowId"])
        controls = (job.get("style") or {}).get("controls", {}).get(row["rowId"]) or {}
        summary = snapshot.get("score_summary") or {}
        part = next((part for part in summary.get("parts") or []
                     if part.get("part_id") == row["partId"]), None)
        if part is None:
            raise ValueError("The quoted singing part is no longer present.")
        if item["mode"] == "solfege":
            selection = (job.get("solfegeSelections") or {}).get(row["partId"])
        else:
            lines = [line for line in part.get("lyric_selections") or []
                     if line.get("name") != GENERATED_LYRIC_NAME]
            matching = [line for line in lines
                        if str(line.get("number")) == str(job["choices"]["verse"])]
            def line_order(line: dict[str, Any]) -> tuple[int, int, str]:
                number = str(line.get("number") or "")
                try:
                    return (0, int(number), number)
                except ValueError:
                    return (1, 0, number)

            selection = (matching or sorted(lines, key=line_order) or [None])[0]
        if not isinstance(selection, dict):
            raise ValueError("The quoted lyric line is no longer present.")
        if (snapshot["current_score"]["score"].get("selected_lyric_selection") != selection):
            await self.orchestrator._reparse_score(
                job["sessionId"], part_id=None, part_index=None,
                verse_number=None, lyric_selection=selection, user_id=job["uid"],
            )
            snapshot = await self.orchestrator._sessions.get_snapshot(job["sessionId"], job["uid"])
        args = {
            "part_id": row["partId"], "voicebank": row["voice"],
            "language": job["choices"]["language"],
            "expand_repeats": job["choices"]["repeats"],
            "require_solfege_lyrics": item["mode"] == "solfege",
            "lyric_selection": selection,
            **{("voice_color" if key == "voiceColor" else key): value
               for key, value in controls.items()},
        }
        if item["mode"] == "solfege":
            args["solfege_pronunciation_patch"] = True
        args = await self.orchestrator._normalize_synthesize_voice_color(args)
        await self.orchestrator._start_synthesis_job(
            job["sessionId"], snapshot["current_score"]["score"], args,
            user_id=job["uid"], user_email="", billing_components=["vocal"],
            parent={"jobId": job["jobId"], "itemId": item["itemId"],
                    "childJobId": item["childJobId"]},
        )

    async def _settle_instrumental(self, job: dict[str, Any]) -> None:
        item = next(item for item in job["items"] if item["type"] == "instrumental")
        result = await asyncio.to_thread(
            settle_instrumental_reservation, job["uid"], item["reservationId"])
        if result.status not in {"settled", "already_settled"}:
            raise RuntimeError(f"Instrumental settlement: {result.status}")
        items = [dict(value) for value in job["items"]]
        for value in items:
            if value["type"] == "instrumental":
                value.update(status="done", creditsCharged=value["creditsReserved"])
        await asyncio.to_thread(
            sing_all_store.apply, job["jobId"], {"items": items},
            expected_stage="singing", expected_revision=job.get("revision", 0),
        )
