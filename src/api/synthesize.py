"""
Convenience synthesize API - runs the full pipeline.
"""

import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import json
import time
import numpy as np

from src.api.phonemize import phonemize
import src.api.syllable_alignment as syllable_alignment
from src.api.inference import (
    predict_durations,
    predict_pitch,
    predict_variance,
    synthesize_audio,
)
from src.api.voicebank import (
    load_voicebank_config,
    resolve_default_voice_color,
    resolve_voice_color_speaker,
)
from src.api.voice_parts import build_infeasible_anchor_action_required
from src.api.voice_parts import synthesize_preflight_action_required
from src.api.timing_errors import InfeasibleAnchorError
from src.phonemizer.phonemizer import Phonemizer
from src.mcp.logging_utils import get_logger, summarize_payload

logger = get_logger(__name__)


def _env_flag_enabled(name: str) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}

class TimeAxis:
    """Tempo-aware time axis for converting beats to milliseconds."""
    
    def __init__(self, tempos: List[Dict[str, Any]]):
        """Build tempo axis from tempo events.
        
        Args:
            tempos: List of {"offset_beats": float, "bpm": float}
        """
        # Keep tempo events ordered by beat offset.
        self.tempos = sorted(tempos, key=lambda t: t["offset_beats"])
        if not self.tempos:
            # Default 120 BPM
            self.tempos = [{"offset_beats": 0.0, "bpm": 120.0}]
        
        # Precompute ms offsets for each tempo change.
        self.ms_offsets = [0.0]
        current_ms = 0.0
        for i in range(len(self.tempos) - 1):
            beats = self.tempos[i + 1]["offset_beats"] - self.tempos[i]["offset_beats"]
            duration_ms = beats * 60000.0 / self.tempos[i]["bpm"]
            current_ms += duration_ms
            self.ms_offsets.append(current_ms)
    
    def get_ms_at_beat(self, beat: float) -> float:
        """Convert beat position to elapsed milliseconds."""
        # Find the last tempo event at or before this beat.
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
    # Convert beats to frame positions using the tempo map.
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
        
        # Ensure at least 1 frame.
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
    """Derive per-note durations for pitch curves from note frame positions."""
    if not start_frames:
        return []
    note_dur: List[int] = []
    for idx in range(len(start_frames) - 1):
        note_dur.append(max(1, start_frames[idx + 1] - start_frames[idx]))
    note_dur.append(max(1, end_frames[-1] - start_frames[-1]))
    return note_dur


def _fill_rest_midi(note_midi: np.ndarray, note_rest: np.ndarray) -> np.ndarray:
    """Fill rest MIDI values by interpolating nearby non-rest pitches."""
    if note_midi.size == 0:
        return note_midi
    if note_rest.all():
        return np.full_like(note_midi, 60.0, dtype=np.float32)
    idx = np.where(~note_rest)[0]
    values = note_midi[idx]
    interpolated = np.interp(np.arange(len(note_midi)), idx, values)
    return interpolated.astype(np.float32)


def _scale_curve(values: List[float], factor: float) -> List[float]:
    """Scale a curve by a constant factor."""
    if values is None:
        return values
    if factor == 1.0:
        return values
    scaled: List[float] = []
    for val in values:
        scaled.append(val * factor)
    return scaled


def _pad_curve_to_length(values: List[float], target_len: int) -> List[float]:
    """Pad or trim a curve to the target length."""
    if target_len <= 0:
        return []
    if not values:
        return [0.0] * target_len
    current_len = len(values)
    if current_len == target_len:
        return values
    if current_len > target_len:
        return values[:target_len]
    pad_value = values[-1]
    return values + [pad_value] * (target_len - current_len)


def _resolve_group_lyric(group: Dict[str, Any]) -> str:
    """Proxy to shared grouping logic used by V2 aligner."""
    return syllable_alignment._resolve_group_lyric(group)


def _trim_single_note_multisyllable(
    phonemes: List[str],
    ids: List[int],
    lang_ids: List[int],
    phonemizer: Phonemizer,
    *,
    lyric: str,
    note_idx: int,
) -> Tuple[List[str], List[int], List[int]]:
    """Reduce multi-syllable phonemes to a single note-friendly slice."""
    trimmed_ph, trimmed_ids, trimmed_lang = syllable_alignment._trim_single_note_multisyllable(
        phonemes=phonemes,
        ids=ids,
        lang_ids=lang_ids,
        phonemizer=phonemizer,
    )

    logger.warning(
        "trim_multisyllable_single_note lyric=%s note_idx=%s original=%s trimmed=%s",
        lyric,
        note_idx,
        phonemes,
        trimmed_ph,
    )
    return trimmed_ph, trimmed_ids, trimmed_lang


def _align_frame_curves(
    expected_frames: int,
    *,
    f0: List[float],
    breathiness: Optional[List[float]],
    tension: Optional[List[float]],
    voicing: Optional[List[float]],
) -> tuple[List[float], Optional[List[float]], Optional[List[float]], Optional[List[float]]]:
    """Ensure all frame-aligned curves match the expected length."""
    def _maybe_pad(name: str, values: Optional[List[float]]) -> Optional[List[float]]:
        if values is None:
            return None
        if len(values) != expected_frames:
            logger.warning(
                "frame_curve_mismatch name=%s expected=%s got=%s",
                name,
                expected_frames,
                len(values),
            )
            return _pad_curve_to_length(values, expected_frames)
        return values

    f0 = _maybe_pad("f0", f0) or []
    breathiness = _maybe_pad("breathiness", breathiness)
    tension = _maybe_pad("tension", tension)
    voicing = _maybe_pad("voicing", voicing)
    return f0, breathiness, tension, voicing


_PHONEME_MAP_CACHE: Dict[str, Dict[str, int]] = {}


def _load_phoneme_map(voicebank_path: Path) -> Dict[str, int]:
    """Load and cache the phoneme-to-id map for a voicebank."""
    key = str(voicebank_path.resolve())
    if key in _PHONEME_MAP_CACHE:
        return _PHONEME_MAP_CACHE[key]
    config = load_voicebank_config(voicebank_path)
    phonemes_path = (voicebank_path / config.get("phonemes", "phonemes.json")).resolve()
    data = json.loads(phonemes_path.read_text())
    _PHONEME_MAP_CACHE[key] = data
    return data


def _get_silence_phoneme_id(voicebank_path: Path) -> int:
    """Resolve the silence phoneme ID (SP/AP)."""
    phoneme_map = _load_phoneme_map(voicebank_path)
    if "SP" in phoneme_map:
        return int(phoneme_map["SP"])
    if "AP" in phoneme_map:
        return int(phoneme_map["AP"])
    raise KeyError("Missing SP/AP silence phoneme in phonemes.json.")


def _scale_durations(durations: List[int], factor: float) -> List[int]:
    """Scale duration list by a factor, keeping minimum of 1 frame."""
    if not durations:
        return durations
    if factor == 1.0:
        return list(durations)
    return [max(1, int(round(dur * factor))) for dur in durations]


def _adjust_durations_to_total(durations: List[int], target_total: int) -> List[int]:
    """Adjust durations to match a target total by nudging from the end."""
    if not durations:
        return durations
    total = sum(durations)
    diff = target_total - total
    if diff == 0:
        return durations
    step = 1 if diff > 0 else -1
    idx = len(durations) - 1
    while diff != 0 and idx >= 0:
        candidate = durations[idx] + step
        if candidate >= 1:
            durations[idx] = candidate
            diff -= step
        else:
            idx -= 1
    return durations


def _largest_remainder_round(weights: List[float], total: int) -> List[int]:
    """Distribute integer budget using largest remainder method."""
    if total <= 0 or not weights:
        return [0] * len(weights)
    weight_sum = float(sum(weights))
    if weight_sum <= 0:
        base = [0] * len(weights)
        for idx in range(total):
            base[idx % len(base)] += 1
        return base
    quotas = [(w / weight_sum) * float(total) for w in weights]
    floors = [int(np.floor(q)) for q in quotas]
    remain = total - sum(floors)
    remainders = sorted(
        ((quotas[i] - floors[i], i) for i in range(len(quotas))),
        key=lambda item: (-item[0], item[1]),
    )
    for _, idx in remainders[:remain]:
        floors[idx] += 1
    return floors


def _rescale_group_durations(
    predicted: List[float],
    anchor_total: int,
    *,
    vowel_flags: Optional[List[bool]] = None,
    group_index: int = -1,
    note_index: Optional[int] = None,
) -> List[int]:
    """Rescale predicted phoneme durations to match an anchor window exactly."""
    count = len(predicted)
    if count == 0:
        return []
    if anchor_total < count:
        raise InfeasibleAnchorError(
            stage="synthesize_timing",
            group_index=int(group_index),
            note_index=note_index,
            anchor_total=int(anchor_total),
            phoneme_count=int(count),
            detail="insufficient_anchor_budget",
        )
    if anchor_total == count:
        return [1] * count

    # Partitioned segment scaling:
    # - keep consonants close to model prediction,
    # - let vowels absorb most anchor elasticity.
    pred = [max(0.0, float(d)) for d in predicted]
    out = [max(1, int(np.round(value))) for value in pred]
    total = int(sum(out))

    valid_flags = bool(vowel_flags) and len(vowel_flags or []) == count
    vowel_indices = [idx for idx, flag in enumerate(vowel_flags or []) if bool(flag)] if valid_flags else []

    if valid_flags and vowel_indices:
        consonant_indices = [idx for idx in range(count) if idx not in set(vowel_indices)]
        if total < anchor_total:
            extra = anchor_total - total
            vowel_weights = [pred[idx] for idx in vowel_indices]
            if sum(vowel_weights) <= 0.0:
                vowel_weights = [1.0] * len(vowel_indices)
            add = _largest_remainder_round(vowel_weights, extra)
            for local_idx, idx in enumerate(vowel_indices):
                out[idx] += int(add[local_idx])
        elif total > anchor_total:
            remove = total - anchor_total

            def _remove_from(indices: List[int], remaining: int) -> int:
                if remaining <= 0:
                    return 0
                capacities = [max(0, out[idx] - 1) for idx in indices]
                cap_total = int(sum(capacities))
                if cap_total <= 0:
                    return remaining
                planned = min(remaining, cap_total)
                alloc = _largest_remainder_round([float(cap) for cap in capacities], planned)
                taken = 0
                for local_idx, idx in enumerate(indices):
                    cap = capacities[local_idx]
                    take = min(cap, int(alloc[local_idx]))
                    if take > 0:
                        out[idx] -= take
                        taken += take
                # Deterministically consume any rounding leftovers.
                left = planned - taken
                if left > 0:
                    for idx in indices:
                        if left <= 0:
                            break
                        cap = max(0, out[idx] - 1)
                        if cap <= 0:
                            continue
                        take = min(cap, left)
                        out[idx] -= take
                        taken += take
                        left -= take
                return remaining - taken

            remove = _remove_from(vowel_indices, remove)
            if remove > 0:
                remove = _remove_from(consonant_indices, remove)
            if remove > 0:
                # Final deterministic fallback.
                for idx in range(count - 1, -1, -1):
                    if remove <= 0:
                        break
                    cap = out[idx] - 1
                    if cap <= 0:
                        continue
                    take = min(cap, remove)
                    out[idx] -= take
                    remove -= take
    else:
        # Fallback: proportional scaling when vowel partition is unavailable.
        pred_sum = float(sum(pred))
        if pred_sum <= 0.0:
            base = [1] * count
            remain = anchor_total - count
            for idx in range(remain):
                base[idx % count] += 1
            return base

        ratio = float(anchor_total) / pred_sum
        scaled = [value * ratio for value in pred]
        out = [max(1, int(np.floor(value))) for value in scaled]
        total = int(sum(out))
        if total < anchor_total:
            remain = anchor_total - total
            ranked = sorted(
                ((scaled[i] - np.floor(scaled[i]), i) for i in range(count)),
                key=lambda item: (-item[0], item[1]),
            )
            for _, idx in ranked[:remain]:
                out[idx] += 1
        elif total > anchor_total:
            remove = total - anchor_total
            ranked = sorted(
                (
                    (out[i] - scaled[i], out[i], i)
                    for i in range(count)
                    if out[i] > 1
                ),
                key=lambda item: (-item[0], -item[1], item[2]),
            )
            ptr = 0
            while remove > 0 and ranked:
                _, _, idx = ranked[ptr]
                if out[idx] > 1:
                    out[idx] -= 1
                    remove -= 1
                ptr += 1
                if ptr >= len(ranked):
                    ptr = 0
                    ranked = sorted(
                        (
                            (out[i] - scaled[i], out[i], i)
                            for i in range(count)
                            if out[i] > 1
                        ),
                        key=lambda item: (-item[0], -item[1], item[2]),
                    )

    drift = anchor_total - int(sum(out))
    if drift != 0:
        idx = len(out) - 1
        while drift != 0 and idx >= 0:
            step = 1 if drift > 0 else -1
            candidate = out[idx] + step
            if candidate >= 1:
                out[idx] = candidate
                drift -= step
            else:
                idx -= 1
    return out


def _apply_anchor_constrained_timing(
    *,
    durations: List[float],
    word_boundaries: List[int],
    group_anchor_frames: List[Dict[str, int]],
    vowel_flags: Optional[List[bool]] = None,
) -> List[int]:
    """Apply note/group anchor-constrained timing to phoneme durations."""
    if not durations or not word_boundaries:
        return durations
    if len(group_anchor_frames) != len(word_boundaries):
        raise ValueError("group_anchor_frames/word_boundaries length mismatch")

    out: List[int] = []
    offset = 0
    if vowel_flags and len(vowel_flags) != len(durations):
        raise ValueError("vowel_flags/durations length mismatch")
    for idx, count in enumerate(word_boundaries):
        count = int(count)
        if count <= 0:
            continue
        group_start = offset
        group_end = offset + count
        group = [float(v) for v in durations[group_start:group_end]]
        if len(group) != count:
            raise ValueError("durations/word_boundaries misalignment")
        group_vowel_flags = (
            [bool(v) for v in (vowel_flags or [])[group_start:group_end]]
            if vowel_flags
            else None
        )
        offset = group_end

        anchor = group_anchor_frames[idx]
        start = int(anchor.get("start_frame", 0))
        end = int(anchor.get("end_frame", 0))
        anchor_total = end - start
        if anchor_total <= 0:
            raise ValueError(f"invalid_group_anchor_window idx={idx} start={start} end={end}")
        note_index = anchor.get("note_index")
        try:
            parsed_note_index = int(note_index) if note_index is not None else None
        except (TypeError, ValueError):
            parsed_note_index = None
        out.extend(
            _rescale_group_durations(
                group,
                anchor_total,
                vowel_flags=group_vowel_flags,
                group_index=idx,
                note_index=parsed_note_index,
            )
        )

    if offset != len(durations):
        raise ValueError("word_boundaries does not consume all durations")
    return out


def _build_slur_velocity_envelope(
    note_durations: List[int],
    slur_groups: List[List[int]],
    *,
    reference_peak: float = 1.0,
    attack_frames: int = 3,
    baseline: float = 1.0,
) -> List[float]:
    """Build a per-frame velocity envelope that re-attacks slur notes.

    Each note onset in the provided slur groups receives a short envelope that
    peaks at reference_peak and decays to baseline over attack_frames. The
    envelope never exceeds reference_peak.
    """
    if reference_peak < baseline:
        raise ValueError("reference_peak must be >= baseline.")
    if attack_frames < 1:
        attack_frames = 1

    total_frames = int(sum(note_durations))
    envelope = np.full(total_frames, baseline, dtype=np.float32)

    note_starts: List[int] = []
    cursor = 0
    for dur in note_durations:
        note_starts.append(cursor)
        cursor += int(dur)

    for group in slur_groups:
        for note_idx in group:
            if note_idx < 0 or note_idx >= len(note_starts):
                continue
            start = note_starts[note_idx]
            end = min(total_frames, start + attack_frames)
            if end <= start:
                continue
            ramp = np.linspace(reference_peak, baseline, num=end - start, dtype=np.float32)
            envelope[start:end] = np.minimum(envelope[start:end], ramp) if reference_peak == baseline else np.maximum(
                envelope[start:end], ramp
            )

    return envelope.tolist()


def _apply_coda_tail_durations(
    durations: List[int],
    coda_tails: List[Dict[str, int]],
    *,
    tail_frames: int = 3,
) -> List[int]:
    """Shorten vowel duration to reserve a brief coda tail without shifting timing."""
    if not coda_tails or tail_frames <= 0:
        return durations
    new_durations = list(durations)
    for tail in coda_tails:
        vowel_idx = int(tail["vowel_idx"])
        coda_start = int(tail["coda_start"])
        coda_len = int(tail["coda_len"])
        if coda_len <= 0:
            continue
        if vowel_idx < 0 or vowel_idx >= len(new_durations):
            continue
        if coda_start < 0 or coda_start + coda_len > len(new_durations):
            continue

        per_tail_frames = int(tail.get("tail_frames", tail_frames) or tail_frames)
        orig_coda_total = sum(new_durations[coda_start:coda_start + coda_len])
        desired = min(per_tail_frames, orig_coda_total)
        if desired < coda_len:
            desired = coda_len

        delta = desired - orig_coda_total
        if delta > 0:
            max_steal = new_durations[vowel_idx] - 1
            if max_steal <= 0:
                continue
            if delta > max_steal:
                desired = orig_coda_total + max_steal
                delta = max_steal
            new_durations[vowel_idx] -= delta
        elif delta < 0:
            new_durations[vowel_idx] -= delta

        for i in range(coda_len):
            new_durations[coda_start + i] = 1
        new_durations[coda_start + coda_len - 1] += desired - coda_len

    return new_durations


def _apply_articulation_gaps(
    phoneme_ids: List[int],
    language_ids: List[int],
    durations: List[int],
    word_boundaries: List[int],
    word_durations: List[int],
    voicebank_path: Path,
    articulation: float,
) -> Tuple[List[int], List[int], List[int]]:
    """Insert silence gaps between words for negative articulation values."""
    if articulation >= 0.0:
        return phoneme_ids, language_ids, durations
    gap_ratio = min(0.5, max(0.0, -0.5 * articulation))
    silence_id = _get_silence_phoneme_id(voicebank_path)
    new_phoneme_ids: List[int] = []
    new_language_ids: List[int] = []
    new_durations: List[int] = []
    offset = 0
    for word_idx, count in enumerate(word_boundaries):
        word_dur = word_durations[word_idx]
        word_ids = phoneme_ids[offset:offset + count]
        word_lang = language_ids[offset:offset + count]
        word_dur_list = durations[offset:offset + count]
        offset += count

        gap = int(round(word_dur * gap_ratio))
        if gap >= word_dur:
            gap = max(0, word_dur - 1)
        target = max(1, word_dur - gap)

        # Compress word durations to make room for the gap.
        scale = target / max(1, sum(word_dur_list))
        scaled = _scale_durations(word_dur_list, scale)
        scaled = _adjust_durations_to_total(scaled, target)

        new_phoneme_ids.extend(word_ids)
        new_language_ids.extend(word_lang)
        new_durations.extend(scaled)

        if gap > 0:
            new_phoneme_ids.append(silence_id)
            new_language_ids.append(word_lang[-1] if word_lang else 0)
            new_durations.append(gap)

    return new_phoneme_ids, new_language_ids, new_durations


def _select_voice_notes(
    notes: List[Dict[str, Any]],
    voice_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Choose notes for a specific voice or infer a default voice selection."""
    voice_pitches: Dict[str, List[float]] = {}
    for note in notes:
        voice = note.get("voice")
        pitch_midi = note.get("pitch_midi")
        if note.get("is_rest") or voice is None or pitch_midi is None:
            continue
        voice_pitches.setdefault(str(voice), []).append(float(pitch_midi))

    selected_voice: Optional[str] = None
    if voice_id is not None:
        # Honor explicit voice selection where possible.
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
    """Locate a phoneme dictionary within a voicebank."""
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
    """Create a phonemizer configured for a voicebank."""
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


def _group_notes(notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Proxy to shared grouping logic used by V2 aligner."""
    return syllable_alignment._group_notes(notes)


def _split_phonemize_result(phoneme_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Proxy to shared grouping logic used by V2 aligner."""
    return syllable_alignment._split_phonemize_result(phoneme_result)


def _build_phoneme_groups(
    word_groups: List[Dict[str, Any]],
    start_frames: List[int],
    end_frames: List[int],
    timing_midi: List[float],
    phonemizer: Phonemizer,
    word_phonemes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Construct aligned phoneme groups for duration/pitch prediction."""
    sp_id = phonemizer._phoneme_to_id["SP"]

    phrase_groups: List[Dict[str, Any]] = []
    note_phonemes: Dict[int, List[str]] = {}
    word_idx = 0
    for group in word_groups:
        notes_in_group = group["notes"]
        note_indices = group["note_indices"]
        is_slur_group = len(note_indices) > 1
        is_syllabic_chain = bool(group.get("is_syllabic_chain"))
        word_entries = [
            {
                "position": None,
                "phonemes": [],
                "ids": [],
                "lang_ids": [],
                "note_idx": None,
            }
        ]
        if group["is_rest"]:
            # Rests become explicit silence phoneme groups.
            note_idx = note_indices[0]
            phrase_groups.append(
                {
                    "position": start_frames[note_idx],
                    "phonemes": ["SP"],
                    "ids": [sp_id],
                    "lang_ids": [0],
                    "tone": float(timing_midi[note_idx]),
                    "note_idx": note_idx,
                }
            )
            note_phonemes[note_idx] = ["SP"]
            continue

        if word_idx >= len(word_phonemes):
            raise ValueError("Phoneme list does not match the selected notes.")
        word_data = word_phonemes[word_idx]
        word_idx += 1
        phonemes = list(word_data.get("phonemes", [])) or ["SP"]
        ids = list(word_data.get("ids", [])) if word_data.get("phonemes") else [sp_id]
        lang_ids = list(word_data.get("lang_ids", [])) if word_data.get("phonemes") else [0]
        if (
            len(group["notes"]) == 1
            and len(phonemes) > 1
            and any(phonemizer.is_vowel(ph) for ph in phonemes)
        ):
            # Trim multi-syllable words when a single note must carry them.
            note_idx = note_indices[0]
            lyric = group["notes"][0].get("lyric", "") or ""
            phonemes, ids, lang_ids = _trim_single_note_multisyllable(
                phonemes,
                ids,
                lang_ids,
                phonemizer,
                lyric=lyric,
                note_idx=note_idx,
            )

        if is_syllabic_chain:
            syllable_chunks = _split_phonemes_into_syllable_chunks(
                phonemes=phonemes,
                ids=ids,
                lang_ids=lang_ids,
                phonemizer=phonemizer,
                expected_chunks=len(note_indices),
            )
            word_entries = [
                {
                    "position": None,
                    "phonemes": [],
                    "ids": [],
                    "lang_ids": [],
                    "note_idx": None,
                }
            ]
            for idx, note_idx in enumerate(note_indices):
                if idx < len(syllable_chunks):
                    chunk = syllable_chunks[idx]
                else:
                    chunk = {"phonemes": [], "ids": [], "lang_ids": []}
                word_entries.append(
                    {
                        "position": start_frames[note_idx],
                        "phonemes": chunk["phonemes"],
                        "ids": chunk["ids"],
                        "lang_ids": chunk["lang_ids"],
                        "note_idx": note_idx,
                    }
                )
            is_slur_group = False

        if is_slur_group:
            # Check if this is a "pure tie" (sustain) or a "slur" (melisma).
            # A pure tie has unchanged pitch across all notes.
            # A slur changes pitch.
            group_pitches = [timing_midi[n_idx] for n_idx in note_indices]
            # Use a small epsilon for float comparison if needed, though timing_midi likely ints/rounded floats
            # But checking strict equality is usually fine for MIDI notes.
            first_pitch = group_pitches[0]
            is_same_pitch = all(p == first_pitch for p in group_pitches)

            if is_same_pitch and not is_syllabic_chain:
                # This is likely a tied note (sustain).
                # We should NOT distribute phonemes (which causes re-articulation).
                # Fall back to default logic which handles sustains by mapping to the first note/extending.
                is_slur_group = False

        if is_slur_group:
            # Try to use the phonemizer's specific slur distribution strategy (e.g. for English)
            slur_distribution = phonemizer.distribute_slur(phonemes, len(note_indices))
            if slur_distribution is not None:
                # STRATEGY PATH: Use the provided distribution
                # We construct word_entries such that word_entries[0] is empty (no prefix attachment),
                # and subsequent entries correspond to the distributed notes.
                word_entries = [
                    {
                        "position": None,
                        "phonemes": [],
                        "ids": [],
                        "lang_ids": [],
                        "note_idx": None,
                    }
                ]
                for i, ph_list in enumerate(slur_distribution):
                    if not ph_list:
                        # Should unlikely happen with our strategies, but safe to skip or handle empty notes
                        continue
                    
                    # Resolve IDs and Lang IDs
                    p_ids = [phonemizer._phoneme_to_id[p] for p in ph_list]
                    l_ids = []
                    for p in ph_list:
                        code = p.split("/")[0] if "/" in p else ""
                        l_ids.append(phonemizer._language_map.get(code, 0))

                    word_entries.append(
                        {
                            "position": start_frames[note_indices[i]],
                            "phonemes": ph_list,
                            "ids": p_ids,
                            "lang_ids": l_ids,
                            "note_idx": note_indices[i],
                        }
                    )
                # Skip the default logic below
                is_slur_group = False  # Prevent the coda logic block from running
                is_start = []  # invalidating to be safe, though we break/skip loop

        if len(word_entries) == 1:
            # DEFAULT PATH: Run existing logic if no strategy populated word_entries
            # Determine syllable starts for aligning phonemes to notes.
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

            slur_vowel_ph: Optional[str] = None
            slur_coda_len = 0
            slur_coda_tail_frames = 0
            if is_slur_group and any(is_vowel):
                last_note = notes_in_group[-1]
                last_lyric = last_note.get("lyric")
                last_is_slur = isinstance(last_lyric, str) and last_lyric.startswith("+")
                last_is_extension = (
                    bool(last_note.get("lyric_is_extended"))
                    or last_note.get("tie_type") in ("stop", "continue")
                    or last_is_slur
                )
                vowel_idx = next(idx for idx, v in enumerate(is_vowel) if v)
                slur_vowel_ph = phonemes[vowel_idx]
                if last_is_extension and vowel_idx < len(phonemes) - 1:
                    slur_coda_len = len(phonemes) - vowel_idx - 1
                    last_note_idx = note_indices[-1]
                    note_start = start_frames[last_note_idx]
                    note_end = end_frames[last_note_idx]
                    note_frames = max(1, note_end - note_start)
                    slur_coda_tail_frames = min(3, max(1, note_frames // 8))

            non_extension_indices = [note_indices[0]]
            # Reset word entries for default loop
            word_entries = [
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
    
            if is_slur_group and slur_vowel_ph:
                for note_idx in note_indices[1:]:
                    note_phonemes.setdefault(note_idx, []).append(slur_vowel_ph)
                for entry in reversed(word_entries):
                    if entry["phonemes"]:
                        if slur_coda_len > 0:
                            entry["coda_len"] = slur_coda_len
                            entry["coda_tail_frames"] = slur_coda_tail_frames
                        break

        if word_entries[0]["phonemes"]:
            # If we have a prefix cluster, attach to previous word when possible.
            if phrase_groups:
                prev_note_idx = phrase_groups[-1].get("note_idx")
                phrase_groups[-1].setdefault("phonemes", []).extend(word_entries[0]["phonemes"])
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
                            "phonemes": word_entries[0]["phonemes"],
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
                    "phonemes": entry["phonemes"],
                    "ids": entry["ids"],
                    "lang_ids": entry["lang_ids"],
                    "tone": float(timing_midi[note_idx]) if note_idx is not None else 0.0,
                    "note_idx": note_idx,
                    "coda_len": entry.get("coda_len", 0),
                    "coda_tail_frames": entry.get("coda_tail_frames", 0),
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
    phonemes = [ph for group in phrase_groups for ph in group.get("phonemes", [])]
    word_pitches = [group["tone"] for group in phrase_groups]
    coda_tails: List[Dict[str, int]] = []
    offset = 0
    for group in phrase_groups:
        group_len = len(group["ids"])
        coda_len = int(group.get("coda_len") or 0)
        if coda_len > 0 and group_len > coda_len:
            coda_start = offset + group_len - coda_len
            vowel_idx = offset + group_len - coda_len - 1
            tail_frames = int(group.get("coda_tail_frames") or 0)
            coda_tails.append(
                {
                    "vowel_idx": vowel_idx,
                    "coda_start": coda_start,
                    "coda_len": coda_len,
                    "tail_frames": tail_frames,
                }
            )
        offset += group_len

    return {
        "phoneme_ids": phoneme_ids,
        "phonemes": phonemes,
        "language_ids": language_ids,
        "word_boundaries": word_boundaries,
        "word_durations": word_durations,
        "word_pitches": word_pitches,
        "note_phonemes": note_phonemes,
        "coda_tails": coda_tails,
    }


def _split_phonemes_into_syllable_chunks(
    *,
    phonemes: List[str],
    ids: List[int],
    lang_ids: List[int],
    phonemizer: Phonemizer,
    expected_chunks: int,
) -> List[Dict[str, List[Any]]]:
    """Split a phoneme sequence into syllable chunks for note-by-note assignment."""
    if expected_chunks <= 1 or len(phonemes) <= 1:
        return [{"phonemes": phonemes, "ids": ids, "lang_ids": lang_ids}]

    is_vowel = [phonemizer.is_vowel(p) for p in phonemes]
    is_glide = [phonemizer.is_glide(p) for p in phonemes]
    starts = [False] * len(phonemes)
    if not any(is_vowel):
        starts[0] = True
    for i in range(len(phonemes)):
        if is_vowel[i]:
            if i >= 2 and is_glide[i - 1] and not is_vowel[i - 2]:
                starts[i - 1] = True
            else:
                starts[i] = True

    start_indices = [idx for idx, flag in enumerate(starts) if flag]
    if not start_indices:
        start_indices = [0]
    start_indices = sorted(set(start_indices))

    chunks: List[Dict[str, List[Any]]] = []
    first_start = start_indices[0]
    lead_ph = phonemes[:first_start] if first_start > 0 else []
    lead_ids = ids[:first_start] if first_start > 0 else []
    lead_lang = lang_ids[:first_start] if first_start > 0 else []
    for idx, start in enumerate(start_indices):
        end = start_indices[idx + 1] if idx + 1 < len(start_indices) else len(phonemes)
        chunk_ph = phonemes[start:end]
        chunk_ids = ids[start:end]
        chunk_lang = lang_ids[start:end]
        if idx == 0 and lead_ph:
            chunk_ph = lead_ph + chunk_ph
            chunk_ids = lead_ids + chunk_ids
            chunk_lang = lead_lang + chunk_lang
        chunks.append(
            {
                "phonemes": chunk_ph,
                "ids": chunk_ids,
                "lang_ids": chunk_lang,
            }
        )

    # Merge overflow chunks into the last requested chunk.
    if len(chunks) > expected_chunks:
        merged = chunks[: expected_chunks - 1]
        tail_ph: List[str] = []
        tail_ids: List[int] = []
        tail_lang: List[int] = []
        for chunk in chunks[expected_chunks - 1 :]:
            tail_ph.extend(chunk["phonemes"])
            tail_ids.extend(chunk["ids"])
            tail_lang.extend(chunk["lang_ids"])
        merged.append({"phonemes": tail_ph, "ids": tail_ids, "lang_ids": tail_lang})
        chunks = merged

    # Pad with empty chunks if we got fewer than expected.
    while len(chunks) < expected_chunks:
        chunks.append({"phonemes": [], "ids": [], "lang_ids": []})

    return chunks


def align_phonemes_to_notes(
    score: Dict[str, Any],
    voicebank: Union[str, Path],
    *,
    part_index: int = 0,
    voice_id: Optional[str] = None,
    include_phonemes: bool = False,
) -> Dict[str, Any]:
    """
    Align phonemes to notes and compute timing inputs for inference steps.
    
    Args:
        score: Score JSON dict (from parse_score)
        voicebank: Voicebank path or ID
        part_index: Which part to synthesize (default: 0)
        voice_id: Which voice to synthesize within a part (default: soprano)
        include_phonemes: Include phoneme strings in output
        
    Returns:
        Dict with:
        - phoneme_ids: Token IDs
        - language_ids: Language ID per phoneme
        - word_boundaries: Phoneme counts per word
        - word_durations: Frames per word group
        - word_pitches: MIDI pitch per word group
        - note_durations: Frames per note (for pitch)
        - note_pitches: MIDI pitch per note (for pitch)
        - note_rests: Rest flags per note (for pitch)
        - phonemes: Optional phoneme strings
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "align_phonemes_to_notes input=%s",
            summarize_payload(
                {
                    "score": score,
                    "voicebank": str(voicebank),
                    "part_index": part_index,
                    "voice_id": voice_id,
                    "include_phonemes": include_phonemes,
                }
            ),
        )
    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)

    # Compute frame duration from voicebank settings.
    sample_rate = config.get("sample_rate", 44100)
    hop_size = config.get("hop_size", 512)
    frame_ms = hop_size / sample_rate * 1000.0

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

    start_frames, end_frames, note_frame_durations, note_pitches, _ = _compute_note_timing(
        notes,
        tempos,
        frame_ms,
    )
    leading_silence_frames = max(0, int(start_frames[0])) if start_frames else 0
    use_v2_aligner = _env_flag_enabled("SYLLABLE_ALIGNER_V2")
    # Keep pitch timeline domain consistent with the active aligner mode.
    if use_v2_aligner:
        pitch_indices = list(range(len(notes)))
    else:
        # Exclude zero-duration notes from pitch curves in legacy aligner mode.
        pitch_indices = [
            idx for idx, note in enumerate(notes)
            if float(note.get("duration_beats") or 0.0) > 0.0
        ]
    if len(pitch_indices) != len(notes) and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "pitch_skip_zero_duration count=%s",
            len(notes) - len(pitch_indices),
        )
    pitch_start_frames = [start_frames[idx] for idx in pitch_indices]
    pitch_end_frames = [end_frames[idx] for idx in pitch_indices]
    pitch_note_pitches = [note_pitches[idx] for idx in pitch_indices]
    if use_v2_aligner:
        # Keep pitch note timeline consistent with the same note-frame domain used
        # by v2 alignment/group anchors.
        pitch_note_durations = [int(note_frame_durations[idx]) for idx in pitch_indices]
    else:
        pitch_note_durations = _compute_pitch_note_durations(
            pitch_start_frames,
            pitch_end_frames,
        )

    phonemizer = _init_phonemizer(voicebank_path)
    if use_v2_aligner:
        ph_result = syllable_alignment.align(
            notes=notes,
            start_frames=start_frames,
            end_frames=end_frames,
            timing_midi=note_pitches,
            note_durations=note_frame_durations,
            phonemizer=phonemizer,
            voicebank_path=voicebank_path,
            include_phonemes=include_phonemes,
        )
        word_groups: List[Dict[str, Any]] = []
    else:
        word_groups = _group_notes(notes)
        lyrics: List[str] = []
        for group in word_groups:
            if group["is_rest"]:
                continue
            lyric = _resolve_group_lyric(group)
            lyrics.append(lyric)

        word_phonemes: List[Dict[str, Any]] = []
        if lyrics:
            # Run phonemizer once per lyric word group.
            phoneme_result = phonemize(lyrics, voicebank_path)
            word_phonemes = _split_phonemize_result(phoneme_result)
            if len(word_phonemes) != len(lyrics):
                raise ValueError("Phonemize output does not match the selected notes.")

        ph_result = _build_phoneme_groups(
            word_groups,
            start_frames,
            end_frames,
            note_pitches,
            phonemizer,
            word_phonemes,
        )

    if not ph_result["phoneme_ids"]:
        raise ValueError("No phonemes found after processing the selected notes.")

    # Determine rest flags based on resolved phoneme content.
    computed_note_rests: List[bool] = []
    prev_rest = True
    for idx, note in enumerate(notes):
        lyric_value = note.get("lyric")
        is_slur = isinstance(lyric_value, str) and lyric_value.startswith("+")
        is_extension = (
            bool(note.get("lyric_is_extended"))
            or note.get("tie_type") in ("stop", "continue")
            or is_slur
        )
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

    pitch_note_rests = [computed_note_rests[idx] for idx in pitch_indices]
    pitch_index_map = {note_idx: pitch_idx for pitch_idx, note_idx in enumerate(pitch_indices)}
    slur_groups: List[List[int]] = []
    if use_v2_aligner:
        for note_indices in ph_result.get("slur_note_groups", []):
            pitch_group = [
                pitch_index_map[note_idx]
                for note_idx in note_indices
                if note_idx in pitch_index_map
            ]
            if len(pitch_group) > 1:
                slur_groups.append(pitch_group)
    else:
        for group in word_groups:
            if group.get("is_rest"):
                continue
            note_indices = group.get("note_indices") or []
            if len(note_indices) <= 1:
                continue
            pitch_group = [
                pitch_index_map[note_idx]
                for note_idx in note_indices
                if note_idx in pitch_index_map
            ]
            if len(pitch_group) > 1:
                slur_groups.append(pitch_group)
    result = {
        "phoneme_ids": ph_result["phoneme_ids"],
        "language_ids": ph_result["language_ids"],
        "word_boundaries": ph_result["word_boundaries"],
        "word_durations": ph_result["word_durations"],
        "word_pitches": ph_result["word_pitches"],
        "leading_silence_frames": leading_silence_frames,
        "vowel_flags": ph_result.get("vowel_flags", []),
        "group_anchor_frames": ph_result.get("group_anchor_frames", []),
        "group_note_indices": ph_result.get("group_note_indices", []),
        "note_durations": pitch_note_durations,
        "note_pitches": pitch_note_pitches,
        "note_rests": pitch_note_rests,
        "slur_groups": slur_groups,
        "coda_tails": ph_result.get("coda_tails", []),
    }
    # Debug contract fields from V2 aligner.
    if "durations" in ph_result:
        result["durations"] = ph_result["durations"]
    if "positions" in ph_result:
        result["positions"] = ph_result["positions"]
    if "durations_debug" in ph_result:
        result["durations_debug"] = ph_result["durations_debug"]
    if "positions_debug" in ph_result:
        result["positions_debug"] = ph_result["positions_debug"]
    if "note_slur" in ph_result:
        result["note_slur"] = ph_result["note_slur"]
    if include_phonemes:
        result["phonemes"] = ph_result.get("phonemes", [])
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("align_phonemes_to_notes output=%s", summarize_payload(result))
    return result


def synthesize(
    score: Dict[str, Any],
    voicebank: Union[str, Path],
    *,
    part_index: int = 0,
    voice_id: Optional[str] = None,
    voice_part_id: Optional[str] = None,
    allow_lyric_propagation: bool = False,
    source_voice_part_id: Optional[str] = None,
    source_part_index: Optional[int] = None,
    voice_color: Optional[str] = None,
    articulation: float = 0.0,
    airiness: float = 1.0,
    intensity: float = 0.5,
    clarity: float = 1.0,
    skip_voice_part_preprocess: bool = False,
    device: str = "cpu",
    progress_callback: Optional[Callable[[str, str, float], None]] = None,
) -> Dict[str, Any]:
    """
    Run the full synthesis pipeline (steps 2-6) in one call.
    
    This is a convenience API that chains:
    align_phonemes_to_notes → predict_durations → predict_pitch → predict_variance → synthesize_audio
    
    Args:
        score: Score JSON dict (from parse_score)
        voicebank: Voicebank path or ID
        part_index: Which part to synthesize (default: 0)
        voice_id: Which voice to synthesize within a part (default: soprano)
        voice_color: Voice color name (subbank color ID)
        articulation: Global legato/staccato adjustment (-1.0 to +1.0)
        airiness: Global breathiness multiplier (0.0 to 1.0)
        intensity: Global tension multiplier (0.0 to 1.0)
        clarity: Global voicing multiplier (0.0 to 1.0)
        device: Device for inference
        progress_callback: Optional callback for step updates
        
    Returns:
        Dict with either:
        - waveform/sample_rate/duration_seconds on success, or
        - status="action_required" with confirmation details when
          voice-part lyric propagation needs explicit user choice.
    """
    allowed_messages = {"Taking a breath for the take..."}

    def _notify(step: str, message: str, progress: float) -> None:
        # Best-effort progress callbacks.
        if not progress_callback:
            return
        if message not in allowed_messages:
            return
        try:
            progress_callback(step, message, progress)
        except Exception:
            logger.debug("progress_callback_failed step=%s", step, exc_info=True)

    def _log_step(step: str, start_time: float) -> None:
        # Emit timing metrics for each pipeline step.
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        logger.info("synthesize_step step=%s elapsed_ms=%.2f", step, elapsed_ms)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "synthesize input=%s",
            summarize_payload(
                {
                    "score": score,
                    "voicebank": str(voicebank),
                    "part_index": part_index,
                    "voice_id": voice_id,
                    "voice_part_id": voice_part_id,
                    "allow_lyric_propagation": allow_lyric_propagation,
                    "source_voice_part_id": source_voice_part_id,
                    "source_part_index": source_part_index,
                    "voice_color": voice_color,
                    "articulation": articulation,
                    "airiness": airiness,
                    "intensity": intensity,
                    "clarity": clarity,
                    "device": device,
                }
            ),
        )
    working_score = score
    effective_part_index = int(part_index)
    preflight = synthesize_preflight_action_required(
        working_score,
        part_index=effective_part_index,
    )
    if preflight is not None:
        return preflight
    # Legacy compatibility mode for callers that expect input-score mutation.
    if working_score is not score and _env_flag_enabled("VOICE_PART_LEGACY_INPLACE_MUTATION"):
        score.clear()
        score.update(deepcopy(working_score))

    voicebank_path = Path(voicebank)
    config = load_voicebank_config(voicebank_path)

    # Resolve voice color to an optional speaker embedding.
    sample_rate = config.get("sample_rate", 44100)
    hop_size = int(config.get("hop_size", 512))
    default_voice_color = resolve_default_voice_color(voicebank_path)
    selected_voice_color = voice_color or default_voice_color
    speaker_name = resolve_voice_color_speaker(voicebank_path, selected_voice_color)
    if speaker_name is None and voice_color is not None:
        speaker_name = resolve_voice_color_speaker(voicebank_path, default_voice_color)

    if airiness < 0.0 or airiness > 1.0:
        raise ValueError("airiness must be between 0.0 and 1.0.")
    if intensity < 0.0 or intensity > 1.0:
        raise ValueError("intensity must be between 0.0 and 1.0.")
    if clarity < 0.0 or clarity > 1.0:
        raise ValueError("clarity must be between 0.0 and 1.0.")

    if articulation < -1.0 or articulation > 1.0:
        raise ValueError("articulation must be between -1.0 and 1.0.")

    start_total = time.monotonic()

    start = time.monotonic()
    try:
        alignment = align_phonemes_to_notes(
            working_score,
            voicebank_path,
            part_index=effective_part_index,
            voice_id=voice_id,
            include_phonemes=False,
        )
    except InfeasibleAnchorError as exc:
        return build_infeasible_anchor_action_required(
            error=exc,
            part_index=effective_part_index,
            voice_id=voice_id,
            voice_part_id=voice_part_id,
        )
    _log_step("align", start)
    
    # Step 3: Predict durations.
    start = time.monotonic()
    use_timing_v2 = _env_flag_enabled("SYLLABLE_TIMING_V2")
    dur_result = predict_durations(
        phoneme_ids=alignment["phoneme_ids"],
        word_boundaries=alignment["word_boundaries"],
        word_durations=alignment["word_durations"],
        word_pitches=alignment["word_pitches"],
        voicebank=voicebank_path,
        language_ids=alignment["language_ids"],
        speaker_name=speaker_name,
        apply_word_alignment=not use_timing_v2,
        device=device,
    )
    _log_step("durations", start)
    phoneme_ids = alignment["phoneme_ids"]
    language_ids = alignment["language_ids"]
    durations: List[int] = list(dur_result["durations"])
    if use_timing_v2:
        anchor_frames = alignment.get("group_anchor_frames") or []
        predicted_raw = [
            float(v) for v in dur_result.get("durations_raw", dur_result["durations"])
        ]
        if anchor_frames:
            try:
                durations = _apply_anchor_constrained_timing(
                    durations=predicted_raw,
                    word_boundaries=alignment["word_boundaries"],
                    group_anchor_frames=anchor_frames,
                    vowel_flags=alignment.get("vowel_flags") or None,
                )
            except InfeasibleAnchorError as exc:
                return build_infeasible_anchor_action_required(
                    error=exc,
                    part_index=effective_part_index,
                    voice_id=voice_id,
                    voice_part_id=voice_part_id,
                )
    coda_tails = alignment.get("coda_tails") or []
    if coda_tails:
        durations = _apply_coda_tail_durations(
            durations,
            coda_tails,
            tail_frames=3,
        )
    if articulation < 0.0:
        # Insert short gaps to increase articulation.
        phoneme_ids, language_ids, durations = _apply_articulation_gaps(
            phoneme_ids,
            language_ids,
            durations,
            alignment["word_boundaries"],
            alignment["word_durations"],
            voicebank_path,
            articulation,
        )
    runtime_note_durations = list(alignment["note_durations"])
    use_aligner_v2 = _env_flag_enabled("SYLLABLE_ALIGNER_V2")
    if use_aligner_v2:
        runtime_note_durations = _adjust_durations_to_total(
            runtime_note_durations,
            int(sum(durations)),
        )
    
    # Step 4: Predict pitch.
    start = time.monotonic()
    pitch_result = predict_pitch(
        phoneme_ids=phoneme_ids,
        durations=durations,
        note_pitches=alignment["note_pitches"],
        note_durations=runtime_note_durations,
        note_rests=alignment["note_rests"],
        voicebank=voicebank_path,
        language_ids=language_ids,
        encoder_out=dur_result["encoder_out"],
        speaker_name=speaker_name,
        device=device,
    )
    _log_step("pitch", start)
    expected_frames = int(sum(durations))
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "frame_lengths_before_variance expected=%s f0=%s durations=%s",
            expected_frames,
            len(pitch_result["f0"]),
            len(durations),
        )
    if expected_frames > 0 and len(pitch_result["f0"]) != expected_frames:
        logger.warning(
            "pitch_length_mismatch expected=%s got=%s",
            expected_frames,
            len(pitch_result["f0"]),
        )
        pitch_result["f0"] = _pad_curve_to_length(pitch_result["f0"], expected_frames)
        if "pitch_midi" in pitch_result:
            pitch_result["pitch_midi"] = _pad_curve_to_length(
                pitch_result["pitch_midi"], expected_frames
            )

    slur_groups = alignment.get("slur_groups") or []
    velocity: Optional[List[float]] = None
    if slur_groups:
        velocity = _build_slur_velocity_envelope(
            runtime_note_durations,
            slur_groups,
            reference_peak=1.0,
            attack_frames=3,
            baseline=0.9,
        )
        if expected_frames > 0 and len(velocity) != expected_frames:
            velocity = _pad_curve_to_length(velocity, expected_frames)
    
    # Step 5: Predict variance.
    start = time.monotonic()
    var_result = predict_variance(
        phoneme_ids=phoneme_ids,
        durations=durations,
        f0=pitch_result["f0"],
        voicebank=voicebank_path,
        language_ids=language_ids,
        encoder_out=dur_result["encoder_out"],
        speaker_name=speaker_name,
        device=device,
    )
    _log_step("variance", start)
    breathiness = _scale_curve(var_result["breathiness"], airiness)
    tension = _scale_curve(var_result["tension"], intensity)
    voicing = _scale_curve(var_result["voicing"], clarity)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "frame_lengths_variance expected=%s breathiness=%s tension=%s voicing=%s",
            expected_frames,
            len(breathiness) if breathiness else 0,
            len(tension) if tension else 0,
            len(voicing) if voicing else 0,
        )
    if expected_frames > 0:
        # Ensure all frame-aligned curves match the expected frame count.
        pitch_result["f0"], breathiness, tension, voicing = _align_frame_curves(
            expected_frames,
            f0=pitch_result["f0"],
            breathiness=breathiness,
            tension=tension,
            voicing=voicing,
        )
    
    # Step 6: Synthesize audio.
    _notify("synthesize", "Taking a breath for the take...", 0.8)
    start = time.monotonic()
    audio_result = synthesize_audio(
        phoneme_ids=phoneme_ids,
        durations=durations,
        f0=pitch_result["f0"],
        voicebank=voicebank_path,
        breathiness=breathiness,
        tension=tension,
        voicing=voicing,
        velocity=velocity,
        language_ids=language_ids,
        speaker_name=speaker_name,
        device=device,
    )
    _log_step("synthesize", start)

    leading_silence_frames = int(alignment.get("leading_silence_frames", 0) or 0)
    waveform = np.asarray(audio_result["waveform"], dtype=np.float32)
    if leading_silence_frames > 0:
        pad_samples = max(0, leading_silence_frames * hop_size)
        if pad_samples > 0:
            waveform = np.pad(waveform, (pad_samples, 0), mode="constant")

    # Calculate duration.
    duration = len(waveform) / sample_rate
    
    result = {
        "waveform": waveform,
        "sample_rate": sample_rate,
        "duration_seconds": duration,
    }
    total_ms = (time.monotonic() - start_total) * 1000.0
    logger.info(
        "synthesize_total elapsed_ms=%.2f duration_seconds=%.2f",
        total_ms,
        duration,
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("synthesize output=%s", summarize_payload(result))
    return result
