import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from music21 import converter
from src.musicxml.parser import _parse_score

score = converter.parse("tests/output/voice_parts_e2e/my_tribute_women_voice_part_1_derived.xml")
score_data = _parse_score(score, part_id=None, part_index=None, verse_number=None, lyrics_only=False, keep_rests=False)

for part in score_data.parts:
    if "Derived" in str(part.part_name):
        print(f"PART: {part.part_name}")
        for n in part.notes:
            if n.measure_number == 25:
                lyric_str = n.lyric if n.lyric else "<none>"
                print(f" offset {n.offset_beats:8.2f}: pitch={n.pitch_step}{n.pitch_octave:1} voice={n.voice} lyric={lyric_str} extend={n.lyric_is_extended}")
