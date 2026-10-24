"""Offline construction proof on synthetic replay jobs only."""

from __future__ import annotations

import csv
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_preprocess_sheet import SATB, _two_staff_score


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/replay/prove_sheet_construction.py"
spec = importlib.util.spec_from_file_location("prove_sheet_construction", SCRIPT)
proof = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = proof
spec.loader.exec_module(proof)


def _job(corpus: Path, job_id: str, xml: bytes, part_id: str) -> None:
    entry = corpus / job_id
    entry.mkdir(parents=True)
    (entry / "input.xml").write_bytes(xml)
    (entry / "job.json").write_text(json.dumps({
        "job_id": job_id,
        "spec": {"planningContext": {"target": {"part_id": part_id, "verse_number": "1"}}},
    }), encoding="utf-8")


def _corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    _job(corpus, "a-satb", SATB.read_bytes(), "P1-Staff1")
    _job(corpus, "b-unknown", SATB.read_bytes(), "not-a-part")
    _two_staff_score(tmp_path)
    _job(corpus, "c-freeform", (tmp_path / "synthetic.xml").read_bytes(), "P1-Staff1")
    _job(corpus, "d-error", b"<score-partwise><broken>", "P1-Staff1")
    return corpus


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_temp_corpus_statuses_reports_and_input_purity(tmp_path):
    corpus = _corpus(tmp_path)
    out = tmp_path / "proof"
    rows = proof.prove(corpus, out)
    assert [row["job_id"] for row in rows] == ["a-satb", "b-unknown", "c-freeform", "d-error"]
    assert [row["status"] for row in rows] == [
        "assembled", "target_unresolved", "freeform_unresolved", "error",
    ]
    assert rows[0]["lint_clean"] and rows[0]["p0"] == 0
    assert rows[0]["lanes"] == 2 and rows[0]["segments"] > 0
    assert "target_unresolved" in rows[1]["issues"]
    assert rows[2]["freeform_slot_ids"] == [
        _json(out / "jobs/c-freeform.json")["freeform_slot_ids"][0]
    ]
    assert "freeform" in rows[2]["freeform_slot_ids"][0]
    assert rows[2]["slots_by_kind"] == {"freeform": 1}
    assert rows[3]["error"].startswith("ParseError:")
    assert "<score" not in rows[3]["error"]
    assert _json(out / "sheets/a-satb.json")["lanes"]
    assert _json(out / "plans/a-satb.json")["targets"]
    assert _json(out / "plans/c-freeform.json") is None
    assert _json(out / "sheets/d-error.json") is None
    assert all((out / "jobs" / f"{row['job_id']}.json").exists() for row in rows)
    assert not list(corpus.rglob("*.derived_*.xml"))
    assert (corpus / "a-satb/input.xml").read_bytes() == SATB.read_bytes()

    with (out / "jobs.csv").open(newline="", encoding="utf-8") as file:
        records = list(csv.DictReader(file))
    assert tuple(records[0]) == proof.JOB_COLUMNS
    assert [record["job_id"] for record in records] == [row["job_id"] for row in rows]
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "Jobs total: 4" in summary
    assert "Lint-clean: 1 / 4" in summary
    assert "| assembled | 1 |" in summary
    assert "| target_unresolved | 1 |" in summary
    assert "| freeform_unresolved | 1 |" in summary
    assert "| error | 1 |" in summary
    assert "| — | none | 0 |" in summary
    assert "| c-freeform | freeform_unresolved |" in summary


def test_selection_job_filter_and_repo_output_refusal(tmp_path):
    corpus = _corpus(tmp_path)
    selection = tmp_path / "selection.txt"
    selection.write_text("d-error\na-satb\nb-unknown\n", encoding="utf-8")
    rows = proof.prove(corpus, tmp_path / "selected", selection=selection)
    assert [row["job_id"] for row in rows] == ["a-satb", "b-unknown", "d-error"]
    rows = proof.prove(corpus, tmp_path / "intersection", selection=selection,
                       job_ids=["b-unknown", "c-freeform"])
    assert [row["job_id"] for row in rows] == ["b-unknown"]
    with pytest.raises(ValueError, match="outside the repository"):
        proof.prove(corpus, proof.PROJECT_ROOT / "proof-should-not-exist")
    assert not (proof.PROJECT_ROOT / "proof-should-not-exist").exists()

    env = {key: value for key, value in os.environ.items() if key != "GEMINI_API_KEY"}
    cli_out = tmp_path / "cli"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--corpus", str(corpus), "--out", str(cli_out),
         "--selection", str(selection), "--job", "a-satb", "--job", "c-freeform"],
        capture_output=True, text=True, env=env, check=True,
    )
    assert "summary.md" in completed.stdout
    assert [path.stem for path in (cli_out / "jobs").glob("*.json")] == ["a-satb"]
    assert "Lint-clean: 1 / 1" in (cli_out / "summary.md").read_text(encoding="utf-8")


def test_p0_p1_normalization_and_summary_job_counts(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    _job(corpus, "first", SATB.read_bytes(), "P1-Staff1")
    _job(corpus, "second", SATB.read_bytes(), "P1-Staff1")

    def failing_executor(_score, *, request):
        assert request["plan"]["targets"]
        return {
            "status": "action_required",
            "lint_findings": [{
                "rule": "section_timeline_contiguous_no_gaps", "severity": "error",
                "part_index": 0, "target_voice_part_id": "voice part 1",
                "impacted_ranges": [{"start": 1, "end": 2}],
            }],
            "warnings": [{"rule_metadata": {
                "rule": "partial_lyric_coverage", "impacted_measures": [2],
            }}],
            "targets": [{"part_index": 0, "target_voice_part_id": "voice part 1",
                         "failed_validation_rules": [{
                             "rule": "validation_failed_needs_review", "measure_range": [1, 2],
                         }]}],
        }

    monkeypatch.setattr(proof, "preprocess_voice_parts", failing_executor)
    out = tmp_path / "report"
    rows = proof.prove(corpus, out)
    assert all(row["status"] == "assembled" and not row["lint_clean"] for row in rows)
    assert all((row["p0"], row["p1"], row["p2"]) == (1, 1, 1) for row in rows)
    assert rows[0]["findings"][0] == {
        "rule": "section_timeline_contiguous_no_gaps", "severity": "P0",
        "domain": "STRUCTURAL", "target": "0:voice part 1", "measures": [1, 2],
    }
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "Jobs total: 2" in summary and "Lint-clean: 0 / 2" in summary
    assert "| P0 | section_timeline_contiguous_no_gaps | 2 |" in summary
    assert "| P1 | validation_failed_needs_review | 2 |" in summary
    assert "| first | section_timeline_contiguous_no_gaps |" in summary
    assert "| second | section_timeline_contiguous_no_gaps |" in summary
