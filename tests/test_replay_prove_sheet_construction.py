"""Offline construction proof on synthetic replay jobs only."""

from __future__ import annotations

import csv
import copy
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


CHORD = b'''<score-partwise version="3.1"><part-list><score-part id="P1">
<part-name>Choir</part-name></score-part></part-list><part id="P1"><measure number="1">
<attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time>
<clef><sign>G</sign><line>2</line></clef></attributes>
<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><voice>1</voice>
<type>quarter</type><lyric number="1"><text>la</text></lyric></note>
<note><chord/><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration>
<voice>1</voice><type>quarter</type></note></measure></part></score-partwise>'''


def test_temp_corpus_statuses_reports_and_input_purity(tmp_path):
    corpus = _corpus(tmp_path)
    # Five simultaneous notes in one voice require 5! joint rank assignments.
    # Keep this job freeform while the two-staff score now assembles.
    chord_notes = "".join(
        f'<note>{"<chord/>" if index else ""}<pitch><step>{step}</step>'
        '<octave>5</octave></pitch><duration>4</duration><voice>1</voice>'
        '<type>whole</type>'
        + ('<lyric><text>word</text></lyric>' if index == 0 else '') + '</note>'
        for index, step in enumerate("CDEFG")
    )
    (corpus / "c-freeform/input.xml").write_text(
        '<score-partwise version="3.1"><part-list><score-part id="P1">'
        '<part-name>P1-Staff1</part-name></score-part></part-list><part id="P1">'
        '<measure number="1"><attributes><divisions>1</divisions>'
        '<time><beats>4</beats><beat-type>4</beat-type></time>'
        '<clef><sign>G</sign><line>2</line></clef></attributes>'
        + chord_notes + '</measure></part></score-partwise>', encoding="utf-8",
    )
    out = tmp_path / "proof"
    rows = proof.prove(corpus, out)
    assert [row["job_id"] for row in rows] == ["a-satb", "b-unknown", "c-freeform", "d-error"]
    assert [row["status"] for row in rows] == [
        "assembled", "target_unresolved", "freeform_unresolved", "error",
    ]
    assert rows[0]["lint_clean"] and rows[0]["p0"] == 0
    assert rows[0]["lanes"] == 2 and rows[0]["segments"] > 0
    assert "target_unresolved" in rows[1]["issues"]
    freeform_sheet = _json(out / "sheets/c-freeform.json")
    assert rows[2]["freeform_slot_ids"] == [slot["slot_id"] for slot in freeform_sheet["slots"]]
    assert freeform_sheet["slots"][0]["evidence"]["reason"] == "voice_assignment_over_24_options"
    assert "voice_assignment_over_24_options" in rows[2]["error"]
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
    assert "Runs clean: 1 / 4" in summary
    assert "Lint-clean: 1 / 4" in summary
    assert "| assembled | 1 |" in summary
    assert "| target_unresolved | 1 |" in summary
    assert "| freeform_unresolved | 1 |" in summary
    assert "| error | 1 |" in summary
    assert "| — | none | 0 |" in summary
    assert "| c-freeform | freeform_unresolved |" in summary
    assert "## Jobs not runs-clean" in summary


def test_lyricless_job_stops_without_assembly_or_executor(tmp_path):
    corpus = tmp_path / "corpus"
    xml = CHORD.replace(b'<lyric number="1"><text>la</text></lyric>', b'')
    _job(corpus, "lyricless", xml, "Choir")
    out = tmp_path / "proof"
    rows = proof.prove(corpus, out)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "needs_lyrics"
    assert row["issues"] == ["score_has_no_lyrics"]
    assert row["lanes"] == row["segments"] == row["slots"] == 0
    assert row["executor_status"] is None and not row["runs_clean"]
    assert _json(out / "plans/lyricless.json") is None
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "Runs clean: 0 / 1 (plus 1 stopped: score has no lyrics)" in summary
    assert "| needs_lyrics | 1 |" in summary
    assert "## Stopped: score has no lyrics\n\n| Job |\n| --- |\n| lyricless |" in summary
    assert "| lyricless |" not in summary.split("## Jobs not runs-clean", 1)[1]


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


def test_action_required_is_lint_clean_but_not_runs_clean_and_no_messages(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    _job(corpus, "action", SATB.read_bytes(), "P1-Staff1")

    def executor(_score, *, request):
        assert request["plan"]["targets"]
        return {
            "status": "action_required", "action": "section_lyric_quality_failed",
            "message": "PRIVATE_EXECUTOR_MESSAGE_SENTINEL",
            "section_quality_issues": [
                {"code": "missing_word_lyric", "message": "PRIVATE_EXECUTOR_MESSAGE_SENTINEL"},
            ],
            "targets": [{"section_quality_issues": [
                {"code": "missing_word_lyric"}, {"code": "weak_alignment"},
            ]}],
        }

    monkeypatch.setattr(proof, "preprocess_voice_parts", executor)
    out = tmp_path / "report"
    row = proof.prove(corpus, out)[0]
    assert row["lint_clean"] is True and row["runs_clean"] is False
    assert row["executor_status"] == "action_required"
    assert row["executor_action"] == "section_lyric_quality_failed"
    assert row["executor_codes"] == ["missing_word_lyric", "weak_alignment"]
    assert row["unsung_chord_notes"] is None and row["unsung_chord_bars"] is None
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "Runs clean: 0 / 1" in summary and "Lint-clean: 1 / 1" in summary
    assert "| section_lyric_quality_failed | 1 |" in summary
    assert "| action | section_lyric_quality_failed |" in summary
    assert "| missing_word_lyric | 1 |" in summary
    with (out / "jobs.csv").open(newline="", encoding="utf-8") as file:
        record = next(csv.DictReader(file))
    assert list(record).index("runs_clean") == list(record).index("lint_clean") + 1
    assert record["executor_codes"] == "missing_word_lyric;weak_alignment"
    assert record["unsung_chord_notes"] == ""
    assert _json(out / "jobs/action.json")["unsung_chord_bars"] is None
    assert all(
        "PRIVATE_EXECUTOR_MESSAGE_SENTINEL" not in file.read_text(encoding="utf-8")
        for file in out.rglob("*") if file.is_file()
    )


def test_complete_split_reports_unsung_chord_notes(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    _job(corpus, "chord", CHORD, "Choir")

    def executor_for(pitches):
        def executor(score, *, request):
            assert len(request["plan"]["targets"]) == 2
            result_score = copy.deepcopy(score)
            targets = []
            for pitch in pitches:
                part_index = len(result_score["parts"])
                note = copy.deepcopy(score["parts"][0]["notes"][0])
                note["pitch_midi"] = pitch
                result_score["parts"].append({"notes": [note]})
                targets.append({
                    "appended_part_ref": {"part_index": part_index},
                    "derived_lane": {"source": {"part_index": 0}},
                    "hidden_default_lane": False,
                })
            return {"status": "ready", "score": result_score, "targets": targets}
        return executor

    monkeypatch.setattr(proof, "preprocess_voice_parts", executor_for([60.0, 60.0]))
    missing_out = tmp_path / "missing"
    missing = proof.prove(corpus, missing_out)[0]
    assert missing["unsung_chord_notes"] == 1 and missing["unsung_chord_bars"] == [1]
    assert missing["runs_clean"] is False
    missing_summary = (missing_out / "summary.md").read_text(encoding="utf-8")
    assert "| chord | 1 | 1 |" in missing_summary
    assert "| chord | unsung_chord_notes=1 |" in missing_summary

    monkeypatch.setattr(proof, "preprocess_voice_parts", executor_for([60.0, 64.0]))
    covered = proof.prove(corpus, tmp_path / "covered")[0]
    assert covered["unsung_chord_notes"] == 0 and covered["unsung_chord_bars"] == []
    assert covered["runs_clean"] is True


def test_unnumbered_source_notes_count_and_hidden_lanes_do_not(tmp_path):
    source = {"part_id": "Choir", "notes": [
        {"measure_number": 3, "offset_beats": 0, "pitch_midi": 60, "voice": "_default"},
        {"measure_number": 3, "offset_beats": 0, "pitch_midi": 64, "voice": "1"},
    ]}
    score = {"parts": [source]}
    plan = {"targets": [{"split_coverage": "complete", "source": {"part_id": "Choir"}}]}
    result = {"score": {"parts": [source,
                                   {"notes": [source["notes"][0]]},
                                   {"notes": [source["notes"][1]]}]},
              "targets": [
                  {"appended_part_ref": {"part_index": 1},
                   "derived_lane": {"source": {"part_index": 0}}},
                  {"appended_part_ref": {"part_index": 2},
                   "derived_lane": {"source": {"part_index": 0}},
                   "hidden_default_lane": True},
              ]}
    assert proof.unsung_chord_notes(score, plan, result) == (1, [3])


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


def test_assembly_error_and_intent_count(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    _job(corpus, "intent", SATB.read_bytes(), "P1-Staff1")
    path = corpus / "intent/job.json"
    job = _json(path)
    job["spec"]["planningContext"]["other_instruction"] = "keep the soprano"
    path.write_text(json.dumps(job), encoding="utf-8")

    def fail_assembly(_sheet, _choices):
        raise proof.AssemblyError("uncovered measure 2")

    monkeypatch.setattr(proof, "assemble_plan", fail_assembly)
    rows = proof.prove(corpus, tmp_path / "report")
    assert rows[0]["status"] == "assembly_error"
    assert rows[0]["error"] == "AssemblyError: uncovered measure 2"
    assert "intent_requires_llm" in rows[0]["issues"]
    assert "Intent requires LLM: 1" in (tmp_path / "report/summary.md").read_text(encoding="utf-8")
