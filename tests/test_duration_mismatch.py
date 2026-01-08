import unittest
from pathlib import Path

from src.api import parse_score
from src.api.inference import predict_durations
from src.api.synthesize import align_phonemes_to_notes, _compute_note_timing, _select_voice_notes
from src.api.voicebank import load_voicebank_config


class TestDurationMismatch(unittest.TestCase):
    """Diagnose where duration totals diverge from note timing."""

    def setUp(self) -> None:
        self.root_dir = Path(__file__).parent.parent
        self.voicebank_path = self.root_dir / "assets/voicebanks/Raine_Rena_2.01"
        self.score_path = self.root_dir / "assets/test_data/the-christmas-song.xml"
        if not self.voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {self.voicebank_path}")
        if not self.score_path.exists():
            self.skipTest(f"Test score not found at {self.score_path}")

    def test_duration_alignment_breakpoint(self) -> None:
        score = parse_score(self.score_path)
        alignment = align_phonemes_to_notes(score, self.voicebank_path)
        durations = predict_durations(
            phoneme_ids=alignment["phoneme_ids"],
            word_boundaries=alignment["word_boundaries"],
            word_durations=alignment["word_durations"],
            word_pitches=alignment["word_pitches"],
            voicebank=self.voicebank_path,
            language_ids=alignment["language_ids"],
        )["durations"]

        config = load_voicebank_config(self.voicebank_path)
        sample_rate = config.get("sample_rate", 44100)
        hop_size = config.get("hop_size", 512)
        frame_ms = hop_size / sample_rate * 1000.0
        tempos = score.get("tempos", [{"offset_beats": 0.0, "bpm": 120.0}])
        notes = _select_voice_notes(score["parts"][0]["notes"], None)
        start_frames, end_frames, _, _, _ = _compute_note_timing(notes, tempos, frame_ms)

        total_frames = int(sum(durations))
        note_total = int(sum(alignment["note_durations"]))
        word_total = int(sum(alignment["word_durations"]))
        if note_total == total_frames:
            return

        non_increasing = []
        for idx in range(1, len(start_frames)):
            if start_frames[idx] <= start_frames[idx - 1]:
                non_increasing.append(idx)

        if not non_increasing:
            self.fail(
                "duration mismatch detected but no overlapping note starts found "
                f"(total_frames={total_frames} note_total={note_total})"
            )

        mismatch_idx = non_increasing[0]
        note = notes[mismatch_idx]
        prev_note = notes[mismatch_idx - 1]
        detail = (
            "duration mismatch caused by overlapping note starts: "
            f"total_frames={total_frames} note_total={note_total} word_total={word_total} "
            f"diff={note_total - total_frames} "
            f"overlap_count={len(non_increasing)} "
            f"note_idx={mismatch_idx} start_frame={start_frames[mismatch_idx]} "
            f"prev_start_frame={start_frames[mismatch_idx - 1]} "
            f"lyric={note.get('lyric','')} pitch_midi={note.get('pitch_midi')} "
            f"prev_pitch_midi={prev_note.get('pitch_midi')} "
            f"offset_beats={note.get('offset_beats')} duration_beats={note.get('duration_beats')}"
        )
        self.fail(detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
