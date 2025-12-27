"""
Convenience synthesize API - runs the full pipeline.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import numpy as np

from src.api.score import parse_score
from src.api.inference import predict_durations, predict_pitch, predict_variance, synthesize_mel
from src.api.vocode import vocode
from src.api.voicebank import load_voicebank_config
from src.phonemizer.phonemizer import Phonemizer


class TimeAxis:
    """Tempo-aware time axis for converting beats to milliseconds."""
    
    def __init__(self, tempos: List[Dict[str, Any]]):
        """Build tempo axis from tempo events.
        
        Args:
            tempos: List of {"offset_beats": float, "bpm": float}
        """
        self.tempos = sorted(tempos, key=lambda t: t["offset_beats"])
        if not self.tempos:
            # Default 120 BPM
            self.tempos = [{"offset_beats": 0.0, "bpm": 120.0}]
        
        # Precompute ms offsets for each tempo change
        self.ms_offsets = [0.0]
        current_ms = 0.0
        for i in range(len(self.tempos) - 1):
            beats = self.tempos[i + 1]["offset_beats"] - self.tempos[i]["offset_beats"]
            duration_ms = beats * 60000.0 / self.tempos[i]["bpm"]
            current_ms += duration_ms
            self.ms_offsets.append(current_ms)
    
    def get_ms_at_beat(self, beat: float) -> float:
        """Convert beat position to elapsed milliseconds."""
        idx = 0
        for i, t in enumerate(self.tempos):
            if beat >= t["offset_beats"]:
                idx = i
            else:
                break
        
        tempo = self.tempos[idx]
        ms_offset = self.ms_offsets[idx]
        beat_offset = beat - tempo["offset_beats"]
        return ms_offset + (beat_offset * 60000.0 / tempo["bpm"])


def _compute_note_timing(
    notes: List[Dict[str, Any]],
    tempos: List[Dict[str, Any]],
    frame_ms: float,
) -> Tuple[List[int], List[int], List[int], List[float], List[bool]]:
    """Convert note beat positions into frame durations.
    
    Args:
        notes: List of note dicts with offset_beats, duration_beats, pitch_midi, is_rest
        tempos: List of tempo events
        frame_ms: Milliseconds per audio frame
        
    Returns:
        start_frames: List of note start frames
        end_frames: List of note end frames
        note_durations: List of frame counts per note
        note_pitches: List of MIDI pitches
        note_rests: List of is_rest flags
    """
    time_axis = TimeAxis(tempos)
    
    start_frames: List[int] = []
    end_frames: List[int] = []
    midi_raw: List[float] = []

    note_durations: List[int] = []
    note_rests: List[bool] = []
    
    for note in notes:
        offset = note.get("offset_beats", 0.0)
        duration = note.get("duration_beats", 1.0)
        
        start_ms = time_axis.get_ms_at_beat(offset)
        end_ms = time_axis.get_ms_at_beat(offset + duration)
        
        start_frame = int(round(start_ms / frame_ms))
        end_frame = int(round(end_ms / frame_ms))
        
        # Ensure at least 1 frame
        if end_frame <= start_frame:
            end_frame = start_frame + 1

        start_frames.append(start_frame)
        end_frames.append(end_frame)
        
        frame_count = end_frame - start_frame
        note_durations.append(frame_count)
        
        if note.get("is_rest", False):
            note_rests.append(True)
            midi_raw.append(-1.0)
        else:
            note_rests.append(False)
            midi_raw.append(float(note.get("pitch_midi", 0.0) or 0.0))

    midi = _fill_rest_midi(
        np.array(midi_raw, dtype=np.float32),
        np.array(note_rests, dtype=bool),
    )
    note_pitches = midi.tolist()
    return start_frames, end_frames, note_durations, note_pitches, note_rests


def _compute_pitch_note_durations(
    start_frames: List[int],
    end_frames: List[int],
) -> List[int]:
    if not start_frames:
        return []
    note_dur: List[int] = []
    for idx in range(len(start_frames) - 1):
        note_dur.append(max(1, start_frames[idx + 1] - start_frames[idx]))
    note_dur.append(max(1, end_frames[-1] - start_frames[-1]))
    return note_dur


def _fill_rest_midi(note_midi: np.ndarray, note_rest: np.ndarray) -> np.ndarray:
    if note_midi.size == 0:
        return note_midi
    if note_rest.all():
        return np.full_like(note_midi, 60.0, dtype=np.float32)
    idx = np.where(~note_rest)[0]
    values = note_midi[idx]
    interpolated = np.interp(np.arange(len(note_midi)), idx, values)
    return interpolated.astype(np.float32)


def _select_voice_notes(
    notes: List[Dict[str, Any]],
    voice_id: Optional[str],
) -> List[Dict[str, Any]]:
    voice_pitches: Dict[str, List[float]] = {}
    for note in notes:
        voice = note.get("voice")
        pitch_midi = note.get("pitch_midi")
        if note.get("is_rest") or voice is None or pitch_midi is None:
            continue
        voice_pitches.setdefault(str(voice), []).append(float(pitch_midi))

    selected_voice: Optional[str] = None
    if voice_id is not None:
        voice_key = str(voice_id)
        if voice_key in voice_pitches:
            selected_voice = voice_key
        else:
            label = voice_key.strip().lower()
            if label in ("soprano", "alto", "tenor", "bass") and voice_pitches:
                ordered = sorted(
                    voice_pitches.items(),
                    key=lambda item: sum(item[1]) / len(item[1]),
                    reverse=True,
                )
                if label == "bass":
                    selected_voice = ordered[-1][0]
                else:
                    idx = {"soprano": 0, "alto": 1, "tenor": 2}.get(label, 0)
                    selected_voice = ordered[min(idx, len(ordered) - 1)][0]
            else:
                raise ValueError(f"voice_id '{voice_id}' not found in the selected part.")
    elif "1" in voice_pitches:
        selected_voice = "1"
    elif len(voice_pitches) > 1:
        selected_voice = max(
            voice_pitches.items(),
            key=lambda item: sum(item[1]) / len(item[1]),
        )[0]
    elif len(voice_pitches) == 1:
        selected_voice = next(iter(voice_pitches.keys()))

    if selected_voice is not None:
        return [note for note in notes if str(note.get("voice")) == selected_voice]
    if voice_pitches:
        return [note for note in notes if note.get("voice") is not None]
    return notes


def _find_dictionary(voicebank_path: Path) -> Path:
    candidates = [
        voicebank_path / "dsvariance" / "dsdict.yaml",
        voicebank_path / "dsdur" / "dsdict.yaml",
        voicebank_path / "dsdur" / "dsdict-en.yaml",
        voicebank_path / "dsdict.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path.resolve()
    raise FileNotFoundError(
        f"Could not find phoneme dictionary in {voicebank_path}"
    )


def _init_phonemizer(voicebank_path: Path, language: str = "en") -> Phonemizer:
    config = load_voicebank_config(voicebank_path)
    phonemes_path = (voicebank_path / config.get("phonemes", "phonemes.json")).resolve()
    languages_path = None
    if "languages" in config:
        languages_path = (voicebank_path / config["languages"]).resolve()
    dictionary_path = _find_dictionary(voicebank_path)
    return Phonemizer(
        phonemes_path=phonemes_path,
        dictionary_path=dictionary_path,
        languages_path=languages_path,
        language=language,
        allow_g2p=True,
    )


def _build_phoneme_groups(
    notes: List[Dict[str, Any]],
    start_frames: List[int],
    end_frames: List[int],
    timing_midi: List[float],
    phonemizer: Phonemizer,
) -> Dict[str, Any]:
    word_groups: List[Dict[str, Any]] = []
    current_group: Optional[Dict[str, Any]] = None

    for idx, note in enumerate(notes):
        if note.get("is_rest", False):
            current_group = None
            word_groups.append(
                {
                    "notes": [note],
                    "note_indices": [idx],
                    "is_rest": True,
                }
            )
            continue

        is_continuation = bool(note.get("lyric_is_extended")) or note.get("tie_type") in ("stop", "continue")
        if current_group is None or not is_continuation:
            current_group = {
                "notes": [note],
                "note_indices": [idx],
                "is_rest": False,
            }
            word_groups.append(current_group)
        else:
            current_group["notes"].append(note)
            current_group["note_indices"].append(idx)

    sp_id = phonemizer._phoneme_to_id["SP"]

    phrase_groups: List[Dict[str, Any]] = []
    note_phonemes: Dict[int, List[str]] = {}
    for group in word_groups:
        notes_in_group = group["notes"]
        note_indices = group["note_indices"]
        if group["is_rest"]:
            note_idx = note_indices[0]
            phrase_groups.append(
                {
                    "position": start_frames[note_idx],
                    "ids": [sp_id],
                    "lang_ids": [0],
                    "tone": float(timing_midi[note_idx]),
                    "note_idx": note_idx,
                }
            )
            note_phonemes[note_idx] = ["SP"]
            continue

        lyric = notes_in_group[0].get("lyric", "") or ""
        ph_res = phonemizer.phonemize_tokens([lyric])
        phonemes = list(ph_res.phonemes) or ["SP"]
        ids = list(ph_res.ids) if ph_res.phonemes else [sp_id]
        lang_ids = list(ph_res.language_ids) if ph_res.phonemes else [0]

        is_vowel = [phonemizer.is_vowel(p) for p in phonemes]
        is_glide = [phonemizer.is_glide(p) for p in phonemes]
        is_start = [False] * len(phonemes)
        if not any(is_vowel):
            is_start[0] = True
        for i in range(len(phonemes)):
            if is_vowel[i]:
                if i >= 2 and is_glide[i - 1] and not is_vowel[i - 2]:
                    is_start[i - 1] = True
                else:
                    is_start[i] = True

        non_extension_indices = [note_indices[0]]
        word_entries: List[Dict[str, Any]] = [
            {
                "position": None,
                "phonemes": [],
                "ids": [],
                "lang_ids": [],
                "note_idx": None,
            }
        ]
        note_index = 0
        for idx, phoneme in enumerate(phonemes):
            if is_start[idx] and note_index < len(non_extension_indices):
                note_idx = non_extension_indices[note_index]
                note_index += 1
                word_entries.append(
                    {
                        "position": start_frames[note_idx],
                        "phonemes": [],
                        "ids": [],
                        "lang_ids": [],
                        "note_idx": note_idx,
                    }
                )
            word_entries[-1]["phonemes"].append(phoneme)
            word_entries[-1]["ids"].append(ids[idx])
            word_entries[-1]["lang_ids"].append(lang_ids[idx])

        if word_entries[0]["phonemes"]:
            if phrase_groups:
                prev_note_idx = phrase_groups[-1].get("note_idx")
                phrase_groups[-1]["ids"].extend(word_entries[0]["ids"])
                phrase_groups[-1]["lang_ids"].extend(word_entries[0]["lang_ids"])
                if prev_note_idx is not None:
                    note_phonemes.setdefault(prev_note_idx, []).extend(word_entries[0]["phonemes"])
            elif len(word_entries) > 1:
                note_idx = word_entries[1]["note_idx"]
                note_start = start_frames[note_idx]
                note_end = end_frames[note_idx]
                note_dur = max(1, note_end - note_start)
                prefix_frames = min(5, note_dur - 1) if note_dur > 1 else 0
                if prefix_frames > 0:
                    phrase_groups.append(
                        {
                            "position": note_start,
                            "ids": word_entries[0]["ids"],
                            "lang_ids": word_entries[0]["lang_ids"],
                            "tone": float(timing_midi[note_idx]),
                            "note_idx": note_idx,
                        }
                    )
                    note_phonemes.setdefault(note_idx, []).extend(word_entries[0]["phonemes"])
                    word_entries[1]["position"] = note_start + prefix_frames
                else:
                    word_entries[1]["phonemes"] = word_entries[0]["phonemes"] + word_entries[1]["phonemes"]
                    word_entries[1]["ids"] = word_entries[0]["ids"] + word_entries[1]["ids"]
                    word_entries[1]["lang_ids"] = word_entries[0]["lang_ids"] + word_entries[1]["lang_ids"]

        for entry in word_entries[1:]:
            if not entry["phonemes"]:
                continue
            note_idx = entry["note_idx"]
            phrase_groups.append(
                {
                    "position": entry["position"],
                    "ids": entry["ids"],
                    "lang_ids": entry["lang_ids"],
                    "tone": float(timing_midi[note_idx]) if note_idx is not None else 0.0,
                    "note_idx": note_idx,
                }
            )
            if note_idx is not None:
                note_phonemes.setdefault(note_idx, []).extend(entry["phonemes"])

    if not phrase_groups:
        return {
            "phoneme_ids": [],
            "language_ids": [],
            "word_boundaries": [],
            "word_durations": [],
            "word_pitches": [],
            "note_phonemes": {},
        }

    positions = [group["position"] for group in phrase_groups]
    positions.append(end_frames[-1])
    word_durations = [max(1, positions[i + 1] - positions[i]) for i in range(len(positions) - 1)]
    word_boundaries = [len(group["ids"]) for group in phrase_groups]
    phoneme_ids = [pid for group in phrase_groups for pid in group["ids"]]
    language_ids = [lid for group in phrase_groups for lid in group["lang_ids"]]
    word_pitches = [group["tone"] for group in phrase_groups]

    return {
        "phoneme_ids": phoneme_ids,
        "language_ids": language_ids,
        "word_boundaries": word_boundaries,
        "word_durations": word_durations,
        "word_pitches": word_pitches,
        "note_phonemes": note_phonemes,
    }


def synthesize(
    score: Dict[str, Any],
    voicebank: Union[str, Path],
    *,
    part_index: int = 0,
    voice_id: Optional[str] = None,
    device: str = "cpu",
) -> Dict[str, Any]:
    """
    Run the full synthesis pipeline (steps 2-7) in one call.
    
    This is a convenience API that chains:
    phonemize → predict_durations → predict_pitch → predict_variance → synthesize_mel → vocode
    
    Args:
        score: Score JSON dict (from parse_score)
        voicebank: Voicebank path or ID
        part_index: Which part to synthesize (default: 0)
        voice_id: Which voice to synthesize within a part (default: soprano)
        device: Device for inference
        
    Returns:
        Dict with:
        - waveform: Audio samples as list
        - sample_rate: Sample rate
        - duration_seconds: Audio duration
    """
    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)
    
    # Calculate frame duration in milliseconds
    sample_rate = config.get("sample_rate", 44100)
    hop_size = config.get("hop_size", 512)
    frame_ms = hop_size / sample_rate * 1000.0
    
    # Extract notes and tempos from score
    if not score.get("parts"):
        raise ValueError("Score has no parts")
    
    part = score["parts"][part_index]
    notes = part.get("notes", [])
    tempos = score.get("tempos", [{"offset_beats": 0.0, "bpm": 120.0}])
    
    if not notes:
        raise ValueError("Part has no notes")

    notes = _select_voice_notes(notes, voice_id)
    if not notes:
        raise ValueError("No notes left after applying voice selection")
    
    # Compute proper timing from tempo
    start_frames, end_frames, note_durations, note_pitches, note_rests = _compute_note_timing(
        notes,
        tempos,
        frame_ms,
    )
    pitch_note_durations = _compute_pitch_note_durations(start_frames, end_frames)

    phonemizer = _init_phonemizer(voicebank_path)
    ph_result = _build_phoneme_groups(
        notes,
        start_frames,
        end_frames,
        note_pitches,
        phonemizer,
    )

    if not ph_result["phoneme_ids"]:
        raise ValueError("No phonemes found after processing the selected notes.")

    computed_note_rests: List[bool] = []
    prev_rest = True
    for idx, note in enumerate(notes):
        is_extension = bool(note.get("lyric_is_extended")) or note.get("tie_type") in ("stop", "continue")
        if is_extension and idx > 0:
            computed_note_rests.append(prev_rest)
            continue
        phs = ph_result["note_phonemes"].get(idx, [])
        is_rest = (not phs) or all(
            ph == "AP" or ph == "SP" or not phonemizer.is_vowel(ph)
            for ph in phs
        )
        computed_note_rests.append(is_rest)
        prev_rest = is_rest
    
    # Step 3: Predict durations
    dur_result = predict_durations(
        phoneme_ids=ph_result["phoneme_ids"],
        word_boundaries=ph_result["word_boundaries"],
        note_durations=ph_result["word_durations"],
        note_pitches=ph_result["word_pitches"],
        voicebank=voicebank_path,
        language_ids=ph_result["language_ids"],
        device=device,
    )
    
    # Step 4: Predict pitch
    pitch_result = predict_pitch(
        phoneme_ids=ph_result["phoneme_ids"],
        durations=dur_result["durations"],
        note_pitches=note_pitches,
        note_durations=pitch_note_durations,
        note_rests=computed_note_rests,
        voicebank=voicebank_path,
        language_ids=ph_result["language_ids"],
        encoder_out=dur_result["encoder_out"],
        device=device,
    )
    
    # Step 5: Predict variance
    var_result = predict_variance(
        phoneme_ids=ph_result["phoneme_ids"],
        durations=dur_result["durations"],
        f0=pitch_result["f0"],
        voicebank=voicebank_path,
        language_ids=ph_result["language_ids"],
        encoder_out=dur_result["encoder_out"],
        device=device,
    )
    
    # Step 6: Synthesize mel
    mel_result = synthesize_mel(
        phoneme_ids=ph_result["phoneme_ids"],
        durations=dur_result["durations"],
        f0=pitch_result["f0"],
        voicebank=voicebank_path,
        breathiness=var_result["breathiness"],
        tension=var_result["tension"],
        voicing=var_result["voicing"],
        language_ids=ph_result["language_ids"],
        device=device,
    )
    
    # Step 7: Vocode
    audio_result = vocode(
        mel=mel_result["mel"],
        f0=pitch_result["f0"],
        voicebank=voicebank_path,
        device=device,
    )
    
    # Calculate duration
    waveform = audio_result["waveform"]
    duration = len(waveform) / sample_rate
    
    return {
        "waveform": waveform,
        "sample_rate": sample_rate,
        "duration_seconds": duration,
    }
