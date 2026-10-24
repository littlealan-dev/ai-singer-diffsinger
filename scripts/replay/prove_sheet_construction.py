#!/usr/bin/env python3
"""Prove offline that first-option decision sheets construct lint-clean plans."""

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
    "job_id", "status", "lint_clean", "p0", "p1", "p2", "lanes", "segments",
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
        "p0": 0, "p1": 0, "p2": 0, "lint_clean": False, "wall_time_ms": 0,
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
            else:
                plan = assemble_plan(sheet, first_option_choices(sheet))
                result = preprocess_voice_parts(copy.deepcopy(score), request={"plan": plan})
                row["findings"] = _findings(result)
                row["status"] = "assembled"
    except FreeformUnresolved as exc:
        row["status"] = "freeform_unresolved"
        row["error"] = str(exc)
        row["freeform_slot_ids"] = exc.slot_ids
    except AssemblyError as exc:
        row["status"] = "assembly_error"
        row["error"] = _exception_text(exc)
    except Exception as exc:
        row["status"] = "error"
        row["error"] = _exception_text(exc)
    for severity in ("P0", "P1", "P2"):
        row[severity.lower()] = sum(item["severity"] == severity for item in row["findings"])
    row["lint_clean"] = row["status"] == "assembled" and row["p0"] == 0
    row["wall_time_ms"] = round((time.monotonic() - started) * 1000)
    return row, asdict(sheet) if sheet is not None else None, plan


def _summary(rows: list[dict]) -> str:
    status_counts = {status: sum(row["status"] == status for row in rows)
                     for status in ("assembled", "target_unresolved", "freeform_unresolved",
                                    "assembly_error", "error")}
    clean = sum(row["lint_clean"] for row in rows)
    intent = sum("intent_requires_llm" in row["issues"] for row in rows)
    lines = [
        "# Sheet construction proof", "",
        f"Jobs total: {len(rows)}", f"Lint-clean: {clean} / {len(rows)}",
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
    lines.extend(["", "## Jobs not lint-clean", "", "| Job | First failing rule or status |", "| --- | --- |"])
    for row in rows:
        if not row["lint_clean"]:
            failing = next((finding["rule"] for finding in row["findings"]
                            if finding["severity"] == "P0"), None)
            lines.append(f"| {row['job_id']} | {failing or row['status']} |")
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
