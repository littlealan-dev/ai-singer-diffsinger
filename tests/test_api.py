"""
Tests for the SVS Backend API module.

Tests cover the core public APIs defined in api_design.md.
"""

import unittest
import tempfile
import zipfile
from pathlib import Path
import json
import os
from unittest import mock

from src.api import (
    parse_score,
    modify_score,
    phonemize,
    align_phonemes_to_notes,
    list_voicebanks,
    get_voicebank_info,
    save_audio,
)
from src.api.phonemize import _find_dictionary
from src.api.voicebank import resolve_vocoder_model_path
from src.api.synthesize import _apply_coda_tail_durations, _build_slur_velocity_envelope
from src.api.voice_parts import preprocess_voice_parts


ROOT_DIR = Path(__file__).parent.parent
VOICEBANK_PATH = ROOT_DIR / "assets/voicebanks/Raine_Rena_2.01"
TEST_XML = ROOT_DIR / "assets/test_data/amazing-grace-satb-verse1.xml"
TEST_MULTI_VERSE_XML = ROOT_DIR / "assets/test_data/o-holy-night.xml"
OUTPUT_DIR = ROOT_DIR / "tests/output"


class TestParseScore(unittest.TestCase):
    """Tests for parse_score API."""
    
    def test_parse_returns_dict(self):
        """parse_score should return a JSON-serializable dict."""
        score = parse_score(TEST_XML)
        self.assertIsInstance(score, dict)
        # Should be JSON-serializable
        json.dumps(score)
    
    def test_parse_has_required_keys(self):
        """Score dict should have title, tempos, parts, structure."""
        score = parse_score(TEST_XML)
        self.assertIn("title", score)
        self.assertIn("tempos", score)
        self.assertIn("parts", score)
        self.assertIn("structure", score)
        self.assertIn("voice_part_signals", score)
    
    def test_parse_extracts_title(self):
        """Should extract title from MusicXML."""
        score = parse_score(TEST_XML)
        self.assertIsNotNone(score["title"])
        self.assertIn("Amazing Grace", score["title"])
    
    def test_parse_extracts_notes(self):
        """Should extract notes from parts."""
        score = parse_score(TEST_XML)
        self.assertGreater(len(score["parts"]), 0)
        part = score["parts"][0]
        self.assertIn("notes", part)
        self.assertGreater(len(part["notes"]), 0)
    
    def test_parse_note_has_required_fields(self):
        """Each note should have offset, duration, pitch, etc."""
        score = parse_score(TEST_XML)
        note = score["parts"][0]["notes"][0]
        self.assertIn("offset_beats", note)
        self.assertIn("duration_beats", note)
        self.assertIn("pitch_midi", note)
        self.assertIn("lyric", note)

    def test_parse_exposes_voice_part_signals(self):
        """parse_score should expose multi-voice and missing lyric signals."""
        score = parse_score(TEST_XML)
        signals = score.get("voice_part_signals")
        self.assertIsInstance(signals, dict)
        self.assertIn("has_multi_voice_parts", signals)
        self.assertIn("has_missing_lyric_voice_parts", signals)
        self.assertIn("parts", signals)
        self.assertTrue(signals["parts"])
        part_signal = signals["parts"][0]
        self.assertIn("multi_voice_part", part_signal)
        self.assertIn("missing_lyric_voice_parts", part_signal)
        self.assertIn("has_missing_lyric_voice_parts", part_signal)
        self.assertIn("is_potentially_complex_for_preprocess", part_signal)

    def test_parse_exposes_extended_voice_part_signals(self):
        """parse_score should expose analyzer extensions for planning."""
        score = parse_score(TEST_XML, verse_number=1)
        signals = score.get("voice_part_signals")
        self.assertEqual(signals.get("requested_verse_number"), "1")
        self.assertIn("full_score_analysis", signals)
        part_signal = signals["parts"][0]
        self.assertIn("measure_lyric_coverage", part_signal)
        self.assertIn("source_candidate_hints", part_signal)

    def test_parse_persists_selected_verse_number_default(self):
        """parse_score should persist deterministic selected_verse_number."""
        if not TEST_MULTI_VERSE_XML.exists():
            self.skipTest(f"Test score not found: {TEST_MULTI_VERSE_XML}")
        score = parse_score(TEST_MULTI_VERSE_XML)
        self.assertEqual(score.get("selected_verse_number"), "1")
        self.assertEqual(
            (score.get("score_summary") or {}).get("selected_verse_number"),
            "1",
        )
        self.assertEqual(
            (score.get("voice_part_signals") or {}).get("requested_verse_number"),
            "1",
        )

    def test_parse_persists_selected_verse_number_explicit(self):
        """parse_score should persist explicitly requested verse selection."""
        if not TEST_MULTI_VERSE_XML.exists():
            self.skipTest(f"Test score not found: {TEST_MULTI_VERSE_XML}")
        score = parse_score(TEST_MULTI_VERSE_XML, verse_number=2)
        self.assertEqual(score.get("selected_verse_number"), "2")
        self.assertEqual(
            (score.get("score_summary") or {}).get("selected_verse_number"),
            "2",
        )
        self.assertEqual(
            (score.get("voice_part_signals") or {}).get("requested_verse_number"),
            "2",
        )

    def test_preprocess_invalid_plan_returns_action_required(self):
        """Invalid preprocessing plan should produce action_required payload."""
        score = parse_score(TEST_XML)
        result = preprocess_voice_parts(score, plan={"targets": "invalid"})
        self.assertEqual(result.get("status"), "action_required")
        self.assertEqual(result.get("action"), "invalid_plan_payload")

    def test_parse_rejects_invalid_mxl_archive(self):
        """parse_score should reject corrupt .mxl inputs deterministically."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            mxl_path = Path(tmp_dir) / "invalid.mxl"
            mxl_path.write_bytes(b"not-a-zip-file")
            with self.assertRaisesRegex(ValueError, "Invalid MXL archive\\."):
                parse_score(mxl_path)

    def test_parse_rejects_oversized_mxl_archive(self):
        """parse_score should reject oversized decompressed .mxl payloads."""
        original = os.environ.get("BACKEND_MAX_MXL_UNCOMPRESSED_MB")
        os.environ["BACKEND_MAX_MXL_UNCOMPRESSED_MB"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                mxl_path = Path(tmp_dir) / "oversized.mxl"
                large_score = (
                    b"<score-partwise version='3.1'>"
                    + (b"A" * (1024 * 1024 + 1))
                    + b"</score-partwise>"
                )
                with zipfile.ZipFile(mxl_path, "w") as archive:
                    archive.writestr(
                        "META-INF/container.xml",
                        (
                            b"<?xml version='1.0' encoding='UTF-8'?>"
                            b"<container version='1.0' "
                            b"xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                            b"<rootfiles><rootfile full-path='score.xml' "
                            b"media-type='application/vnd.recordare.musicxml+xml'/>"
                            b"</rootfiles></container>"
                        ),
                    )
                    archive.writestr("score.xml", large_score)
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    parse_score(mxl_path)
        finally:
            if original is None:
                os.environ.pop("BACKEND_MAX_MXL_UNCOMPRESSED_MB", None)
            else:
                os.environ["BACKEND_MAX_MXL_UNCOMPRESSED_MB"] = original

    def test_parse_chord_density_ignores_grace_and_non_positive_duration_notes(self):
        """Chord density should ignore grace notes and duration<=0 artifacts."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <grace slash="yes"/>
        <pitch><step>C</step><octave>5</octave></pitch>
        <voice>1</voice>
        <type>eighth</type>
      </note>
      <note>
        <pitch><step>D</step><octave>5</octave></pitch>
        <duration>1</duration>
        <voice>1</voice>
        <type>quarter</type>
      </note>
      <note>
        <pitch><step>E</step><octave>5</octave></pitch>
        <duration>0</duration>
        <voice>1</voice>
        <type>eighth</type>
      </note>
      <note>
        <pitch><step>F</step><octave>5</octave></pitch>
        <duration>1</duration>
        <voice>1</voice>
        <type>quarter</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            xml_path = Path(tmp_dir) / "grace-density.xml"
            xml_path.write_text(xml, encoding="utf-8")
            score = parse_score(xml_path)
        density = score["voice_part_signals"]["measure_chord_density"]
        voice_part = next(part for part in density if part["part_id"] == "P1")
        measure_1 = next(m for m in voice_part["measures"] if m["measure_number"] == "1")
        self.assertEqual(measure_1["max_simultaneous_notes"], 1)


class TestDictionarySelection(unittest.TestCase):
    """Tests for language-aware dictionary resolution."""

    def test_find_dictionary_prefers_language_specific_over_generic(self):
        """English lookup should prefer dsdict-en.yaml when it exists."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "dsdur").mkdir()
            generic = root / "dsdur" / "dsdict.yaml"
            english = root / "dsdur" / "dsdict-en.yaml"
            generic.write_text("entries: []\n", encoding="utf-8")
            english.write_text("entries: []\n", encoding="utf-8")

            resolved = _find_dictionary(root, language="en")
            self.assertEqual(resolved, english.resolve())

    def test_find_dictionary_falls_back_to_generic_when_language_specific_missing(self):
        """Lookup should fall back to generic dsdict.yaml when needed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "dsdur").mkdir()
            generic = root / "dsdur" / "dsdict.yaml"
            generic.write_text("entries: []\n", encoding="utf-8")

            resolved = _find_dictionary(root, language="en")
            self.assertEqual(resolved, generic.resolve())

    def test_find_dictionary_supports_non_english_suffix(self):
        """Hyphenated language codes should resolve dsdict-<lang>.yaml."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "dsdur").mkdir()
            yue = root / "dsdur" / "dsdict-zh-yue.yaml"
            generic = root / "dsdur" / "dsdict.yaml"
            yue.write_text("entries: []\n", encoding="utf-8")
            generic.write_text("entries: []\n", encoding="utf-8")

            resolved = _find_dictionary(root, language="zh-yue")
            self.assertEqual(resolved, yue.resolve())

    def test_preprocess_ready_with_warnings(self):
        """Propagation with 90% coverage should return ready_with_warnings."""
        notes = []
        for idx in range(10):
            notes.append(
                {
                    "offset_beats": float(idx),
                    "duration_beats": 1.0,
                    "pitch_midi": 64.0,
                    "lyric": f"L{idx}" if idx < 9 else None,
                    "syllabic": "single",
                    "lyric_is_extended": False,
                    "is_rest": False,
                    "voice": "1",
                    "measure_number": 1,
                }
            )
            notes.append(
                {
                    "offset_beats": float(idx),
                    "duration_beats": 1.0,
                    "pitch_midi": 55.0,
                    "lyric": None,
                    "syllabic": None,
                    "lyric_is_extended": False,
                    "is_rest": False,
                    "voice": "2",
                    "measure_number": 1,
                }
            )
        score = {
            "parts": [
                {
                    "part_id": "P1",
                    "part_name": "SOPRANO ALTO",
                    "notes": notes,
                }
            ]
        }
        result = preprocess_voice_parts(
            score,
            part_index=0,
            voice_part_id="alto",
            allow_lyric_propagation=True,
            source_part_index=0,
            source_voice_part_id="soprano",
        )
        self.assertEqual(result.get("status"), "ready_with_warnings")

    def test_preprocess_repair_loop(self):
        """Repair loop should retry and annotate output when enabled."""
        original = os.environ.get("VOICE_PART_REPAIR_LOOP_ENABLED")
        os.environ["VOICE_PART_REPAIR_LOOP_ENABLED"] = "1"
        try:
            score = {
                "parts": [
                    {
                        "part_id": "P1",
                        "part_name": "SOPRANO ALTO",
                        "notes": [
                            {
                                "offset_beats": 0.0,
                                "duration_beats": 1.0,
                                "pitch_midi": 64.0,
                                "lyric": "A",
                                "syllabic": "single",
                                "lyric_is_extended": False,
                                "is_rest": False,
                                "voice": "1",
                                "measure_number": 1,
                            },
                            {
                                "offset_beats": 0.5,
                                "duration_beats": 1.0,
                                "pitch_midi": 55.0,
                                "lyric": None,
                                "syllabic": None,
                                "lyric_is_extended": False,
                                "is_rest": False,
                                "voice": "2",
                                "measure_number": 1,
                            },
                        ],
                    }
                ]
            }
            plan = {
                "targets": [
                    {
                        "target": {"part_index": 0, "voice_part_id": "alto"},
                        "actions": [
                            {"type": "split_voice_part"},
                            {
                                "type": "propagate_lyrics",
                                "strategy": "strict_onset",
                                "source_priority": [
                                    {"part_index": 0, "voice_part_id": "soprano"}
                                ],
                            },
                        ],
                    }
                ]
            }
            result = preprocess_voice_parts(score, plan=plan)
            self.assertEqual(result.get("status"), "ready")
            self.assertIn("repair_loop", result.get("metadata", {}))
        finally:
            if original is None:
                os.environ.pop("VOICE_PART_REPAIR_LOOP_ENABLED", None)
            else:
                os.environ["VOICE_PART_REPAIR_LOOP_ENABLED"] = original


class TestModifyScore(unittest.TestCase):
    """Tests for modify_score API."""
    
    def test_modify_transposes_notes(self):
        """modify_score should be able to transpose notes."""
        score = parse_score(TEST_XML)
        original_pitch = score["parts"][0]["notes"][0]["pitch_midi"]
        
        # Note: RestrictedPython disallows augmented assignment (+=) for object items
        # So we use explicit assignment instead
        modify_score(score, """
for part in score['parts']:
    for note in part['notes']:
        if note['pitch_midi']:
            note['pitch_midi'] = note['pitch_midi'] + 12
        """)
        
        new_pitch = score["parts"][0]["notes"][0]["pitch_midi"]
        self.assertEqual(new_pitch, original_pitch + 12)

    
    def test_modify_sets_velocity(self):
        """modify_score should be able to set velocity."""
        score = parse_score(TEST_XML)
        
        modify_score(score, """
for part in score['parts']:
    for note in part['notes']:
        note['velocity'] = 0.8
        """)
        
        velocity = score["parts"][0]["notes"][0].get("velocity")
        self.assertEqual(velocity, 0.8)
    
    def test_modify_can_filter_parts(self):
        """modify_score should be able to filter parts."""
        score = parse_score(TEST_XML)
        
        modify_score(score, """
score['parts'] = [p for p in score['parts'] if 'SOPRANO' in (p.get('part_name') or '').upper()]
        """)
        
        self.assertGreater(len(score["parts"]), 0)
    
    def test_modify_syntax_error_raises(self):
        """Invalid code should raise SyntaxError."""
        score = parse_score(TEST_XML)
        with self.assertRaises(SyntaxError):
            modify_score(score, "this is not valid python!!!")


class TestPhonemize(unittest.TestCase):
    """Tests for phonemize API."""
    
    @classmethod
    def setUpClass(cls):
        if not VOICEBANK_PATH.exists():
            raise unittest.SkipTest(f"Voicebank not found at {VOICEBANK_PATH}")
        try:
            import nltk
            nltk.data.find("corpora/cmudict")
        except Exception:
            raise unittest.SkipTest("cmudict not available for g2p_en")
    
    def test_phonemize_returns_dict(self):
        """phonemize should return a dict with required keys."""
        result = phonemize(["hello", "world"], VOICEBANK_PATH)
        self.assertIsInstance(result, dict)
        self.assertIn("phonemes", result)
        self.assertIn("phoneme_ids", result)
        self.assertIn("language_ids", result)
        self.assertIn("word_boundaries", result)
    
    def test_phonemize_produces_phonemes(self):
        """phonemize should produce phoneme sequences."""
        result = phonemize(["amazing"], VOICEBANK_PATH)
        self.assertGreater(len(result["phonemes"]), 0)
        self.assertEqual(len(result["phonemes"]), len(result["phoneme_ids"]))
    
    def test_phonemize_word_boundaries_match(self):
        """Word boundaries should match input word count."""
        result = phonemize(["hello", "world", "test"], VOICEBANK_PATH)
        self.assertEqual(len(result["word_boundaries"]), 3)


class TestAlignPhonemesToNotes(unittest.TestCase):
    """Tests for align_phonemes_to_notes API."""

    @classmethod
    def setUpClass(cls):
        if not VOICEBANK_PATH.exists():
            raise unittest.SkipTest(f"Voicebank not found at {VOICEBANK_PATH}")

    def test_align_returns_required_keys(self):
        """align_phonemes_to_notes should return timing + phoneme inputs."""
        score = parse_score(TEST_XML)
        result = align_phonemes_to_notes(
            score,
            VOICEBANK_PATH,
            voice_id="soprano",
            include_phonemes=True,
        )

        self.assertIn("phoneme_ids", result)
        self.assertIn("phonemes", result)
        self.assertIn("language_ids", result)
        self.assertIn("word_boundaries", result)
        self.assertIn("word_durations", result)
        self.assertIn("word_pitches", result)
        self.assertIn("note_durations", result)
        self.assertIn("note_pitches", result)
        self.assertIn("note_rests", result)

    def test_align_lengths_are_consistent(self):
        """Returned arrays should align on expected dimensions."""
        score = parse_score(TEST_XML)
        result = align_phonemes_to_notes(score, VOICEBANK_PATH, voice_id="soprano")

        self.assertEqual(len(result["phoneme_ids"]), len(result["language_ids"]))
        self.assertEqual(len(result["word_boundaries"]), len(result["word_durations"]))
        self.assertEqual(len(result["word_boundaries"]), len(result["word_pitches"]))
        self.assertEqual(len(result["note_durations"]), len(result["note_pitches"]))
        self.assertEqual(len(result["note_durations"]), len(result["note_rests"]))
        self.assertEqual(sum(result["word_boundaries"]), len(result["phoneme_ids"]))


class TestVoicebankAPIs(unittest.TestCase):
    """Tests for list_voicebanks and get_voicebank_info APIs."""
    
    def test_list_voicebanks(self):
        """list_voicebanks should return a list."""
        voicebanks = list_voicebanks(ROOT_DIR / "assets/voicebanks")
        self.assertIsInstance(voicebanks, list)
        self.assertGreater(len(voicebanks), 0)
    
    def test_list_voicebanks_has_required_fields(self):
        """Each voicebank info should have id, name, path."""
        voicebanks = list_voicebanks(ROOT_DIR / "assets/voicebanks")
        vb = voicebanks[0]
        self.assertIn("id", vb)
        self.assertIn("name", vb)
        self.assertIn("path", vb)

    def test_list_voicebanks_only_returns_registered_voicebanks(self):
        voicebanks = list_voicebanks(ROOT_DIR / "assets/voicebanks")
        ids = {entry["id"] for entry in voicebanks}
        self.assertEqual(
            ids,
            {
                "Raine_Rena_2.01",
                "Raine_Reizo_2.01",
                "Katyusha_v170",
                "Keiro_Revenant_v170",
                "Liam_Thorne_v170",
                "SAiFA_v170",
            },
        )

    def test_list_voicebanks_discovers_nested_root_for_custom_search_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            search_root = Path(tmp_dir)
            nested_root = search_root / "NestedBank" / "configs"
            nested_root.mkdir(parents=True, exist_ok=True)
            (nested_root / "dsconfig.yaml").write_text("sample_rate: 44100\n", encoding="utf-8")
            (nested_root / "character.yaml").write_text("name: Nested Bank\n", encoding="utf-8")

            voicebanks = list_voicebanks(search_root)

        self.assertEqual(len(voicebanks), 1)
        self.assertEqual(voicebanks[0]["id"], "NestedBank")
        self.assertEqual(voicebanks[0]["name"], "Nested Bank")
        self.assertEqual(voicebanks[0]["path"], str(Path("NestedBank") / "configs"))
    
    def test_get_voicebank_info(self):
        """get_voicebank_info should return capabilities."""
        if not VOICEBANK_PATH.exists():
            self.skipTest(f"Voicebank not found at {VOICEBANK_PATH}")
        
        info = get_voicebank_info(VOICEBANK_PATH)
        self.assertIn("name", info)
        self.assertIn("has_pitch_model", info)
        self.assertIn("has_variance_model", info)
        self.assertIn("sample_rate", info)
        self.assertIn("voice_colors", info)
        self.assertIn("default_voice_color", info)
        voice_colors = info.get("voice_colors") or []
        if voice_colors:
            color_names = [entry.get("name") for entry in voice_colors]
            self.assertIn(info.get("default_voice_color"), color_names)

    def test_get_voicebank_info_includes_registry_gender_and_voice_type(self):
        if not VOICEBANK_PATH.exists():
            self.skipTest(f"Voicebank not found at {VOICEBANK_PATH}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "voicebank_registry.yaml"
            registry_path.write_text(
                "voicebanks:\n"
                "  - id: Raine_Rena_2.01\n"
                "    gender: female\n"
                "    voice_type: alto\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"VOICEBANK_REGISTRY_PATH": str(registry_path)}):
                info = get_voicebank_info("Raine_Rena_2.01")

        self.assertEqual(info["gender"], "female")
        self.assertEqual(info["voice_type"], "alto")

    def test_get_voicebank_info_includes_registry_metadata_for_nested_voicebank_path(self):
        nested_voicebank_path = ROOT_DIR / "assets/voicebanks/Katyusha_v170/configs"
        if not nested_voicebank_path.exists():
            self.skipTest(f"Voicebank not found at {nested_voicebank_path}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "voicebank_registry.yaml"
            registry_path.write_text(
                "voicebanks:\n"
                "  - id: Katyusha_v170\n"
                "    gender: female\n"
                "    voice_type: soprano\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"VOICEBANK_REGISTRY_PATH": str(registry_path)}):
                info = get_voicebank_info(nested_voicebank_path)

        self.assertEqual(info["gender"], "female")
        self.assertEqual(info["voice_type"], "soprano")

    def test_resolve_vocoder_model_path_uses_shared_assets(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            voicebank_root = root / "assets" / "voicebanks" / "SharedVocoderBank"
            shared_vocoder_dir = root / "assets" / "vocoders" / "pc_nsf_hifigan_44.1k"
            voicebank_root.mkdir(parents=True, exist_ok=True)
            shared_vocoder_dir.mkdir(parents=True, exist_ok=True)
            (voicebank_root / "dsconfig.yaml").write_text(
                "sample_rate: 44100\nvocoder: pc_nsf_hifigan_44.1k_hop512_128bin_2025.02\n",
                encoding="utf-8",
            )
            shared_model = shared_vocoder_dir / "pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.onnx"
            shared_model.write_bytes(b"onnx")

            with mock.patch("src.api.voicebank._project_root", return_value=root):
                resolved = resolve_vocoder_model_path(voicebank_root)

            self.assertEqual(resolved, shared_model.resolve())


class TestSlurVelocityEnvelope(unittest.TestCase):
    """Tests for the slur velocity envelope helper."""

    def test_slur_group_peaks_match_reference(self):
        note_durations = [10, 8, 6, 12]
        slur_groups = [[1, 2, 3]]
        reference_peak = 1.2
        attack_frames = 3

        envelope = _build_slur_velocity_envelope(
            note_durations,
            slur_groups,
            reference_peak=reference_peak,
            attack_frames=attack_frames,
            baseline=1.0,
        )

        note_starts = [0, 10, 18, 24]
        for idx in slur_groups[0]:
            start = note_starts[idx]
            self.assertAlmostEqual(envelope[start], reference_peak, places=6)

        self.assertEqual(envelope[note_starts[0]], 1.0)
        self.assertLessEqual(max(envelope), reference_peak + 1e-6)


class TestCodaTailDurations(unittest.TestCase):
    """Tests for coda tail duration adjustment."""

    def test_coda_tail_steals_from_vowel(self):
        durations = [5, 4, 3]
        coda_tails = [{"vowel_idx": 1, "coda_start": 2, "coda_len": 1, "tail_frames": 2}]
        adjusted = _apply_coda_tail_durations(durations, coda_tails, tail_frames=2)

        self.assertEqual(sum(adjusted), sum(durations))
        self.assertEqual(adjusted[2], 2)
        self.assertEqual(adjusted[1], 5)


class TestSaveAudio(unittest.TestCase):
    """Tests for save_audio API."""
    
    def setUp(self):
        OUTPUT_DIR.mkdir(exist_ok=True)
        self.output_file = OUTPUT_DIR / "test_output.wav"
        if self.output_file.exists():
            self.output_file.unlink()
    
    def test_save_audio_creates_file(self):
        """save_audio should create a WAV file."""
        import numpy as np
        waveform = np.sin(np.linspace(0, 100, 44100)).tolist()  # 1 second sine wave
        
        result = save_audio(waveform, self.output_file, sample_rate=44100)
        
        self.assertTrue(Path(result["path"]).exists())
        self.assertGreater(result["duration_seconds"], 0)
    
    def test_save_audio_returns_metadata(self):
        """save_audio should return path, duration, sample_rate."""
        import numpy as np
        waveform = np.zeros(44100).tolist()
        
        result = save_audio(waveform, self.output_file, sample_rate=44100)
        
        self.assertIn("path", result)
        self.assertIn("duration_seconds", result)
        self.assertIn("sample_rate", result)


if __name__ == "__main__":
    unittest.main()
