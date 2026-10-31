#!/usr/bin/env python3
"""Prove offline that first-option decision sheets construct runnable plans."""

from __future__ import annotations

import argparse
import copy
import csv
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api import parse_score, preprocess_voice_parts
from src.api.preprocess_assembler import (
    AssemblyError, FreeformUnresolved, assemble_plan, first_option_choices,
)
from src.api.preprocess_sheet import build_sheet
from src.api.voice_part_lint_rules import LINT_RULE_SPECS, POSTFLIGHT_VALIDATION_SPECS


JOB_COLUMNS = (
    "job_id", "status", "lint_clean", "runs_clean", "executor_status",
    "executor_action", "executor_codes", "unsung_chord_notes", "p0", "p1", "p2", "lanes", "segments",
    "slots", "freeform", "issues",
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_job_id(job_id: str) -> bool:
    return bool(job_id) and job_id not in {".", ".."} and "/" not in job_id and "\\" not in job_id


def _job_ids(corpus: Path, selection: Path | None, requested: list[str]) -> list[str]:
    selected = None
    if selection is not None:
        selected = {line.strip() for line in selection.read_text(encoding="utf-8").splitlines()
                    if line.strip()}
    if requested:
        wanted = set(requested)
        selected = wanted if selected is None else selected & wanted
    if selected is not None and not all(_safe_job_id(job_id) for job_id in selected):
        raise ValueError("selection contains an unsafe job id")
    return sorted(
        entry.name for entry in corpus.iterdir()
        if entry.is_dir() and _safe_job_id(entry.name)
        and (entry / "job.json").is_file() and (entry / "input.xml").is_file()
        and (selected is None or entry.name in selected)
    )


def _verse_number(context: dict) -> Any:
    target = context.get("target") or {}
    request = context.get("request") or {}
    return target.get("verse_number") if target.get("verse_number") is not None else request.get("verse_number")


def _measure_span(issue: dict) -> list[int] | None:
    values: list[int] = []
    attributes = issue.get("failing_attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    for source in (issue, attributes):
        for key in ("start_measure", "end_measure"):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                values.append(value)
    measure_range = issue.get("measure_range")
    if isinstance(measure_range, (list, tuple)) and len(measure_range) == 2:
        values.extend(value for value in measure_range if isinstance(value, int) and not isinstance(value, bool))
    impacted = issue.get("impacted_measures")
    if isinstance(impacted, list):
        values.extend(value for value in impacted if isinstance(value, int) and not isinstance(value, bool))
    ranges = issue.get("impacted_ranges")
    if isinstance(ranges, list):
        for item in ranges:
            if isinstance(item, dict):
                values.extend(value for value in (item.get("start"), item.get("end"))
                              if isinstance(value, int) and not isinstance(value, bool))
    return [min(values), max(values)] if values else None


def _finding(issue: dict, context: dict) -> dict:
    rule = next((issue[key] for key in ("rule", "code", "rule_code", "rule_id")
                 if isinstance(issue.get(key), str) and issue[key]), None)
    spec = LINT_RULE_SPECS.get(rule) or POSTFLIGHT_VALIDATION_SPECS.get(rule)
    if spec is None:
        raise ValueError(f"unknown lint/postflight rule: {rule or 'missing'}")
    part_index = issue.get("part_index", context.get("part_index"))
    voice_part = (issue.get("target_voice_part_id") or issue.get("target_voice_part")
                  or context.get("target_voice_part_id") or context.get("target_voice_part"))
    target = f"{part_index}:{voice_part}" if part_index is not None or voice_part is not None else None
    return {
        "rule": spec.code,
        "severity": spec.severity,
        "domain": spec.domain,
        "target": target,
        "measures": _measure_span(issue),
    }


def _issue_entries(payload: dict) -> list[dict]:
    entries: list[dict] = []
    for key in ("lint_findings", "failed_validation_rules"):
        for item in payload.get(key) or []:
            if isinstance(item, dict):
                entries.append(item)
    for warning in payload.get("warnings") or []:
        if isinstance(warning, dict) and isinstance(warning.get("rule_metadata"), dict):
            entries.append(warning["rule_metadata"])
    return entries


def _findings(result: dict) -> list[dict]:
    normalized: list[dict] = []
    seen: set[tuple] = set()
    payloads = [result]
    payloads.extend(item for item in result.get("targets") or [] if isinstance(item, dict))
    for payload in payloads:
        for issue in _issue_entries(payload):
            finding = _finding(issue, payload)
            key = (finding["rule"], finding["severity"], finding["domain"],
                   finding["target"], tuple(finding["measures"] or []))
            if key not in seen:
                seen.add(key)
                normalized.append(finding)
    return normalized


def _executor_codes(result: dict) -> list[str]:
    codes: set[str] = set()
    payloads = [result]
    payloads.extend(target for target in result.get("targets") or [] if isinstance(target, dict))
    for payload in payloads:
        for issue in payload.get("section_quality_issues") or []:
            if isinstance(issue, dict) and isinstance(issue.get("code"), str) and issue["code"]:
                codes.add(issue["code"])
    return sorted(codes)


def _pitched_notes_by_onset(part: dict) -> dict[tuple[int, float], set[float]]:
    onsets: dict[tuple[int, float], set[float]] = {}
    for note in part.get("notes") or []:
        if note.get("is_rest") or note.get("pitch_midi") is None:
            continue
        key = (int(note["measure_number"]), round(float(note.get("offset_beats") or 0.0), 6))
        onsets.setdefault(key, set()).add(float(note["pitch_midi"]))
    return onsets


def unsung_chord_notes(score: dict, plan: dict, result: dict) -> tuple[int, list[int]]:
    """Count original chord pitches absent from visible derived lanes of complete splits."""
    source_parts = {
        index for target in plan.get("targets") or []
        if target.get("split_coverage") == "complete"
        for index, part in enumerate(score["parts"])
        if part.get("part_id") == (target.get("source") or {}).get("part_id")
    }
    output_parts: dict[int, list[dict]] = {index: [] for index in source_parts}
    targets = result.get("targets")
    for target in targets if isinstance(targets, list) and targets else [result]:
        if not isinstance(target, dict) or target.get("hidden_default_lane"):
            continue
        ref = target.get("appended_part_ref")
        source = (target.get("derived_lane") or {}).get("source") or {}
        if not isinstance(ref, dict) or source.get("part_index") not in output_parts:
            continue
        output_parts[source["part_index"]].append(result["score"]["parts"][ref["part_index"]])

    missing = 0
    bars: set[int] = set()
    for source_index in sorted(source_parts):
        original = _pitched_notes_by_onset(score["parts"][source_index])
        sung: dict[tuple[int, float], set[float]] = {}
        for part in output_parts[source_index]:
            for key, pitches in _pitched_notes_by_onset(part).items():
                sung.setdefault(key, set()).update(pitches)
        for (bar, onset), pitches in original.items():
            if len(pitches) < 2:
                continue
            absent = pitches - sung.get((bar, onset), set())
            missing += len(absent)
            if absent:
                bars.add(bar)
    return missing, sorted(bars)[:20]


def _exception_text(exc: Exception) -> str:
    message = str(exc).splitlines()[0][:200]
    if "<" in message or ">" in message:
        message = "invalid score input"
    return f"{type(exc).__name__}: {message}"


def _run_job(corpus: Path, job_id: str) -> tuple[dict, Any, Any]:
    started = time.monotonic()
    row: dict[str, Any] = {
        "job_id": job_id, "status": "error", "error": None, "issues": [],
        "lanes": 0, "segments": 0, "slots": 0, "fixed": 0,
        "slots_by_kind": {}, "freeform_slot_ids": [], "findings": [],
        "p0": 0, "p1": 0, "p2": 0, "lint_clean": False, "runs_clean": False,
        "executor_status": None, "executor_action": None, "executor_codes": [],
        "unsung_chord_notes": None, "unsung_chord_bars": None, "wall_time_ms": 0,
    }
    sheet = None
    plan = None
    try:
        job = json.loads((corpus / job_id / "job.json").read_text(encoding="utf-8"))
        context = job["spec"]["planningContext"]
        if not isinstance(context, dict):
            raise ValueError("spec.planningContext must be an object")
        # The executor materializes derived XML beside its input. Isolate that
        # output so the replay corpus remains read-only.
        with tempfile.TemporaryDirectory(prefix="sheet-proof-") as temporary:
            xml_copy = Path(temporary) / "input.xml"
            xml_copy.write_bytes((corpus / job_id / "input.xml").read_bytes())
            score = parse_score(xml_copy, verse_number=_verse_number(context))
            sheet = build_sheet(score, context)
            row["issues"] = list(sheet.issues)
            row["lanes"] = len(sheet.lanes)
            row["segments"] = len(sheet.segments)
            row["slots"] = len(sheet.slots)
            row["fixed"] = len(sheet.fixed)
            row["slots_by_kind"] = {
                kind: sum(slot.kind == kind for slot in sheet.slots)
                for kind in sorted({slot.kind for slot in sheet.slots})
            }
            row["freeform_slot_ids"] = sorted(slot.slot_id for slot in sheet.slots
                                               if slot.kind == "freeform")
            if "target_unresolved" in sheet.issues:
                row["status"] = "target_unresolved"
            elif "score_has_no_lyrics" in sheet.issues:
                row["status"] = "needs_lyrics"
            else:
                plan = assemble_plan(sheet, first_option_choices(sheet))
                result = preprocess_voice_parts(copy.deepcopy(score), request={"plan": plan})
                row["executor_status"] = result.get("status")
                row["executor_action"] = result.get("action")
                row["executor_codes"] = _executor_codes(result)
                if row["executor_status"] == "ready" and any(
                    target.get("split_coverage") == "complete"
                    for target in plan.get("targets") or []
                ):
                    row["unsung_chord_notes"], row["unsung_chord_bars"] = unsung_chord_notes(
                        score, plan, result
                    )
                row["findings"] = _findings(result)
                row["status"] = "assembled"
    except FreeformUnresolved as exc:
        row["status"] = "freeform_unresolved"
        row["freeform_slot_ids"] = exc.slot_ids
        reasons = {
            slot.slot_id: slot.evidence.get("reason", "unspecified")
            for slot in sheet.slots if slot.slot_id in exc.slot_ids
        } if sheet is not None else {}
        row["error"] = str(exc) + "; reasons: " + ", ".join(
            f"{slot_id}={reasons.get(slot_id, 'unspecified')}" for slot_id in exc.slot_ids
        )
    except AssemblyError as exc:
        row["status"] = "assembly_error"
        row["error"] = _exception_text(exc)
    except Exception as exc:
        row["status"] = "error"
        row["error"] = _exception_text(exc)
    for severity in ("P0", "P1", "P2"):
        row[severity.lower()] = sum(item["severity"] == severity for item in row["findings"])
    row["lint_clean"] = row["status"] == "assembled" and row["p0"] == 0
    row["runs_clean"] = (
        row["lint_clean"] and row["executor_status"] == "ready"
        and row["unsung_chord_notes"] in (0, None)
    )
    row["wall_time_ms"] = round((time.monotonic() - started) * 1000)
    return row, asdict(sheet) if sheet is not None else None, plan


def _summary(rows: list[dict]) -> str:
    status_counts = {status: sum(row["status"] == status for row in rows)
                     for status in ("assembled", "target_unresolved", "needs_lyrics", "freeform_unresolved",
                                    "assembly_error", "error")}
    clean = sum(row["lint_clean"] for row in rows)
    runs_clean = sum(row["runs_clean"] for row in rows)
    intent = sum("intent_requires_llm" in row["issues"] for row in rows)
    lines = [
        "# Sheet construction proof", "",
        f"Jobs total: {len(rows)}",
        f"Runs clean: {runs_clean} / {len(rows)} "
        f"(plus {status_counts['needs_lyrics']} stopped: score has no lyrics)",
        f"Lint-clean: {clean} / {len(rows)}",
        f"Intent requires LLM: {intent}", "", "## Status counts", "",
        "| Status | Jobs |", "| --- | ---: |",
    ]
    lines.extend(f"| {status} | {count} |" for status, count in status_counts.items())
    rule_jobs: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        for finding in row["findings"]:
            if finding["severity"] in {"P0", "P1"}:
                rule_jobs.setdefault((finding["severity"], finding["rule"]), set()).add(row["job_id"])
    lines.extend(["", "## P0 and P1 rules", "", "| Severity | Rule | Jobs |", "| --- | --- | ---: |"])
    lines.extend(f"| {severity} | {rule} | {len(jobs)} |"
                 for (severity, rule), jobs in sorted(rule_jobs.items()))
    if not rule_jobs:
        lines.append("| — | none | 0 |")
    action_counts = {
        action: sum(row["executor_action"] == action for row in rows)
        for action in sorted({row["executor_action"] for row in rows if row["executor_action"]})
    }
    lines.extend(["", "## Executor result", "", "| Result | Jobs |", "| --- | ---: |",
                  f"| ready | {sum(row['executor_status'] == 'ready' for row in rows)} |"])
    lines.extend(f"| {action} | {count} |" for action, count in action_counts.items())
    lines.append(f"| not run | {sum(row['executor_status'] is None for row in rows)} |")
    code_jobs: dict[str, int] = {}
    for row in rows:
        for code in row["executor_codes"]:
            code_jobs[code] = code_jobs.get(code, 0) + 1
    lines.extend(["", "## Section quality codes", "", "| Code | Jobs |", "| --- | ---: |"])
    lines.extend(f"| {code} | {count} |" for code, count in sorted(code_jobs.items()))
    if not code_jobs:
        lines.append("| — | none | 0 |")
    lines.extend(["", "## Unsung chord notes", "", "| Job | Notes | Bars |", "| --- | ---: | --- |"])
    lines.extend(
        f"| {row['job_id']} | {row['unsung_chord_notes']} | {', '.join(map(str, row['unsung_chord_bars']))} |"
        for row in rows if row["unsung_chord_notes"] is not None and row["unsung_chord_notes"] > 0
    )
    lines.extend(["", "## Stopped: score has no lyrics", "", "| Job |", "| --- |"])
    lines.extend(f"| {row['job_id']} |" for row in rows if row["status"] == "needs_lyrics")
    lines.extend(["", "## Jobs not runs-clean", "", "| Job | First failing rule or status |", "| --- | --- |"])
    for row in rows:
        if not row["runs_clean"] and row["status"] != "needs_lyrics":
            failing = next((finding["rule"] for finding in row["findings"]
                            if finding["severity"] == "P0"), None)
            reason = (
                row["status"] if row["status"] != "assembled" else
                failing or row["executor_action"] or
                (f"unsung_chord_notes={row['unsung_chord_notes']}"
                 if row["unsung_chord_notes"] else row["executor_status"])
            )
            lines.append(f"| {row['job_id']} | {reason} |")
    return "\n".join(lines) + "\n"


def prove(corpus: Path, out: Path, *, selection: Path | None = None,
          job_ids: list[str] | None = None) -> list[dict]:
    corpus, out = Path(corpus).resolve(), Path(out).resolve()
    if out.is_relative_to(PROJECT_ROOT):
        raise ValueError("--out must be outside the repository")
    ids = _job_ids(corpus, selection, job_ids or [])
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for job_id in ids:
        row, sheet, plan = _run_job(corpus, job_id)
        rows.append(row)
        _write_json(out / "jobs" / f"{job_id}.json", row)
        _write_json(out / "sheets" / f"{job_id}.json", sheet)
        _write_json(out / "plans" / f"{job_id}.json", plan)
    with (out / "jobs.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=JOB_COLUMNS)
        writer.writeheader()
        for row in rows:
            values = {key: row[key] for key in JOB_COLUMNS if key not in {"freeform", "issues"}}
            values["freeform"] = len(row["freeform_slot_ids"])
            values["issues"] = ";".join(row["issues"])
            values["executor_codes"] = ";".join(row["executor_codes"])
            writer.writerow(values)
    (out / "summary.md").write_text(_summary(rows), encoding="utf-8")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--job", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        prove(args.corpus, args.out, selection=args.selection, job_ids=args.job)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print((args.out / "summary.md").resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
