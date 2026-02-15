"""Syllable alignment for DiffSinger contract payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from src.api.phonemize import phonemize
from src.phonemizer.phonemizer import Phonemizer


def _resolve_group_lyric(group: Dict[str, Any]) -> str:
    """Return a phonemizable lyric token for a grouped word section."""
    notes = group.get("notes") or []
    if not notes:
        return ""

    lyric_tokens: List[str] = []
    syllabic_tokens: List[str] = []
    fallback = str(notes[0].get("lyric", "") or "").strip()
    for note in notes:
        lyric = str(note.get("lyric", "") or "").strip()
        if not lyric or lyric.startswith("+"):
            continue
        lyric_tokens.append(lyric)
        syllabic = str(note.get("syllabic", "") or "").strip().lower()
        if syllabic:
            syllabic_tokens.append(syllabic)

    if not lyric_tokens:
        return "" if fallback.startswith("+") else fallback
    if len(lyric_tokens) == 1:
        return lyric_tokens[0]
    if any(token in {"begin", "middle", "end"} for token in syllabic_tokens):
        return "".join(token.replace("-", "") for token in lyric_tokens)
    return lyric_tokens[0]


def _group_notes(notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group notes into lyric-word groups plus continuation metadata."""
    groups: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for idx, note in enumerate(notes):
        if note.get("is_rest", False):
            current = None
            groups.append(
                {
                    "notes": [note],
                    "note_indices": [idx],
                    "is_rest": True,
                    "sustain_indices": [],
                    "carrier_indices": [],
                }
            )
            continue

        lyric = note.get("lyric")
        syllabic = str(note.get("syllabic", "") or "").strip().lower()
        is_sustain = (
            bool(note.get("lyric_is_extended"))
            or note.get("tie_type") in ("stop", "continue")
            or (isinstance(lyric, str) and lyric.startswith("+"))
        )
        is_syllabic_carrier = syllabic in {"single", "begin", "middle", "end"}
        is_continuation = is_sustain or syllabic in {"middle", "end"}

        if current is None or not is_continuation:
            current = {
                "notes": [note],
                "note_indices": [idx],
                "is_rest": False,
                "is_syllabic_chain": syllabic in {"begin", "middle", "end"},
                "sustain_indices": [idx] if is_sustain else [],
                "carrier_indices": [idx] if is_syllabic_carrier else [idx],
            }
            groups.append(current)
        else:
            current["notes"].append(note)
            current["note_indices"].append(idx)
            if syllabic in {"begin", "middle", "end"}:
                current["is_syllabic_chain"] = True
            if is_sustain:
                current["sustain_indices"].append(idx)
            if is_syllabic_carrier:
                current["carrier_indices"].append(idx)

    return groups


def _split_phonemize_result(phoneme_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split flat phonemizer output into per-word bundles."""
    phonemes = phoneme_result.get("phonemes", [])
    ids = phoneme_result.get("phoneme_ids", [])
    lang_ids = phoneme_result.get("language_ids", [])
    boundaries = phoneme_result.get("word_boundaries", [])

    out: List[Dict[str, Any]] = []
    offset = 0
    for count in boundaries:
        count = int(count)
        if count <= 0:
            out.append({"phonemes": [], "ids": [], "lang_ids": []})
            continue
        out.append(
            {
                "phonemes": phonemes[offset : offset + count],
                "ids": ids[offset : offset + count],
                "lang_ids": lang_ids[offset : offset + count],
            }
        )
        offset += count
    return out


def _split_phonemes_into_syllable_chunks(
    *,
    phonemes: List[str],
    ids: List[int],
    lang_ids: List[int],
    phonemizer: Phonemizer,
    expected_chunks: int,
) -> List[Dict[str, List[Any]]]:
    """Split phoneme sequence into syllable chunks for note-level assignment."""
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
        chunks.append({"phonemes": chunk_ph, "ids": chunk_ids, "lang_ids": chunk_lang})

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

    while len(chunks) < expected_chunks:
        chunks.append({"phonemes": [], "ids": [], "lang_ids": []})
    return chunks


def _phoneme_stress(phoneme: str) -> int:
    suffix = phoneme[-1:] if phoneme else ""
    if suffix.isdigit():
        return int(suffix)
    return 0


def _trim_single_note_multisyllable(
    *,
    phonemes: List[str],
    ids: List[int],
    lang_ids: List[int],
    phonemizer: Phonemizer,
) -> tuple[List[str], List[int], List[int]]:
    """Reduce multi-syllable phonemes for single-note fallback behavior."""
    vowel_indices = [idx for idx, ph in enumerate(phonemes) if phonemizer.is_vowel(ph)]
    if len(vowel_indices) <= 1:
        return phonemes, ids, lang_ids

    stressed = [idx for idx in vowel_indices if _phoneme_stress(phonemes[idx]) > 0]
    if stressed:
        chosen = max(stressed, key=lambda idx: _phoneme_stress(phonemes[idx]))
    else:
        scored: List[tuple[int, float]] = []
        for idx in vowel_indices:
            strength = phonemizer.vowel_strength(phonemes[idx])
            scored.append((idx, 1.0 if strength is None else float(strength)))
        chosen = max(scored, key=lambda item: item[1])[0]

    prev_vowel = max([v for v in vowel_indices if v < chosen], default=-1)
    next_vowel = next((v for v in vowel_indices if v > chosen), len(phonemes))
    start = prev_vowel + 1
    end = next_vowel
    trimmed_ph = phonemes[start:end] or [phonemes[chosen]]
    trimmed_ids = ids[start:end] or [ids[chosen]]
    trimmed_lang = lang_ids[start:end] or [lang_ids[chosen]]
    return trimmed_ph, trimmed_ids, trimmed_lang


def validate_ds_contract(payload: Dict[str, Any]) -> List[str]:
    """Validate DiffSinger alignment contract invariants."""
    errs: List[str] = []
    phoneme_ids = payload.get("phoneme_ids", [])
    language_ids = payload.get("language_ids", [])
    durations = payload.get("durations", [])
    positions = payload.get("positions", [])
    word_boundaries = payload.get("word_boundaries", [])
    note_slur = payload.get("note_slur", [])
    note_durations = payload.get("note_durations", [])
    group_anchor_frames = payload.get("group_anchor_frames", [])

    n = len(phoneme_ids)
    if len(language_ids) != n or len(durations) != n or len(positions) != n:
        errs.append("length_mismatch phoneme_ids/language_ids/durations/positions")
    if any(int(d) < 1 for d in durations):
        errs.append("invalid_duration_nonpositive")
    if int(sum(int(x) for x in word_boundaries)) != n:
        errs.append("invalid_word_boundaries_sum")
    if note_slur and note_durations and len(note_slur) != len(note_durations):
        errs.append("length_mismatch note_slur/note_durations")
    if durations and note_durations and int(sum(int(x) for x in durations)) != int(sum(int(x) for x in note_durations)):
        errs.append("frame_conservation_failed")
    if group_anchor_frames:
        if len(group_anchor_frames) != len(word_boundaries):
            errs.append("length_mismatch group_anchor_frames/word_boundaries")
        for anchor in group_anchor_frames:
            start = int(anchor.get("start_frame", 0))
            end = int(anchor.get("end_frame", 0))
            if end <= start:
                errs.append("invalid_group_anchor_window")
                break
    return errs


def align(
    *,
    notes: List[Dict[str, Any]],
    start_frames: List[int],
    end_frames: List[int],
    timing_midi: List[float],
    note_durations: List[int],
    phonemizer: Phonemizer,
    voicebank_path: Path,
    include_phonemes: bool = False,
) -> Dict[str, Any]:
    """Align score notes into DS-contract payload using syllable-based strategy."""
    sp_id = phonemizer._phoneme_to_id["SP"]
    groups = _group_notes(notes)

    lyrics: List[str] = []
    for group in groups:
        if group["is_rest"]:
            continue
        lyrics.append(_resolve_group_lyric(group))

    word_phonemes: List[Dict[str, Any]] = []
    if lyrics:
        word_phonemes = _split_phonemize_result(phonemize(lyrics, voicebank_path))
        if len(word_phonemes) != len(lyrics):
            raise ValueError("Phonemize output does not match grouped lyrics.")

    phrase_groups: List[Dict[str, Any]] = []
    note_phonemes: Dict[int, List[str]] = {}
    note_slur: List[int] = [0] * len(notes)

    word_idx = 0
    coda_tails: List[Dict[str, int]] = []
    pending_coda_specs: List[Dict[str, int]] = []
    slur_note_groups: List[List[int]] = []
    for group in groups:
        note_indices = group["note_indices"]
        if group["is_rest"]:
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
            raise ValueError("Phoneme list does not match grouped note count.")
        data = word_phonemes[word_idx]
        word_idx += 1
        phonemes = list(data.get("phonemes", [])) or ["SP"]
        ids = list(data.get("ids", [])) if data.get("phonemes") else [sp_id]
        lang_ids = list(data.get("lang_ids", [])) if data.get("phonemes") else [0]

        carrier_indices = group.get("carrier_indices") or []
        if not carrier_indices:
            non_sustain = [n for n in note_indices if n not in set(group.get("sustain_indices", []))]
            carrier_indices = non_sustain[:1] if non_sustain else [note_indices[0]]

        # V1-compatible fallback: trim multi-syllable words if only one carrier note.
        if (
            len(carrier_indices) == 1
            and len(phonemes) > 1
            and any(phonemizer.is_vowel(ph) for ph in phonemes)
        ):
            phonemes, ids, lang_ids = _trim_single_note_multisyllable(
                phonemes=phonemes,
                ids=ids,
                lang_ids=lang_ids,
                phonemizer=phonemizer,
            )

        chunks = _split_phonemes_into_syllable_chunks(
            phonemes=phonemes,
            ids=ids,
            lang_ids=lang_ids,
            phonemizer=phonemizer,
            expected_chunks=max(1, len(carrier_indices)),
        )

        for idx, note_idx in enumerate(carrier_indices):
            chunk = chunks[idx] if idx < len(chunks) else {"phonemes": [], "ids": [], "lang_ids": []}
            if not chunk["phonemes"]:
                continue
            phrase_groups.append(
                {
                    "position": start_frames[note_idx],
                    "phonemes": chunk["phonemes"],
                    "ids": chunk["ids"],
                    "lang_ids": chunk["lang_ids"],
                    "tone": float(timing_midi[note_idx]),
                    "note_idx": note_idx,
                }
            )
            note_phonemes.setdefault(note_idx, []).extend(chunk["phonemes"])

        sustain_indices = group.get("sustain_indices", [])
        if len(note_indices) > 1:
            slur_note_groups.append(list(note_indices))
        for sustain_idx in sustain_indices:
            if 0 <= sustain_idx < len(note_slur):
                note_slur[sustain_idx] = 1

        # Preserve V1-style coda-tail hint when a sustained final note extends trailing consonants.
        if sustain_indices and phonemes:
            is_vowel = [phonemizer.is_vowel(p) for p in phonemes]
            if any(is_vowel):
                vowel_idx = next(idx for idx, v in enumerate(is_vowel) if v)
                coda_len = len(phonemes) - vowel_idx - 1
                if coda_len > 0:
                    tail_note = sustain_indices[-1]
                    note_frames = max(1, int(end_frames[tail_note]) - int(start_frames[tail_note]))
                    coda_tail_frames = min(3, max(1, note_frames // 8))
                    # Defer global-index resolution until phrase_groups are sorted.
                    target_note_idx = None
                    for idx in reversed(carrier_indices):
                        if idx in note_phonemes and note_phonemes[idx]:
                            target_note_idx = idx
                            break
                    if target_note_idx is not None:
                        pending_coda_specs.append(
                            {
                                "note_idx": int(target_note_idx),
                                "coda_len": int(coda_len),
                                "tail_frames": int(coda_tail_frames),
                            }
                        )

    if not phrase_groups:
        raise ValueError("No phoneme groups produced by aligner.")

    phrase_groups.sort(key=lambda g: (int(g["position"]), int(g.get("note_idx", 0))))

    # Resolve coda-tail indices after sort so global phoneme offsets are stable.
    running = 0
    group_offsets: List[int] = []
    for group in phrase_groups:
        group_offsets.append(running)
        running += len(group["ids"])
    for spec in pending_coda_specs:
        note_idx = int(spec["note_idx"])
        matches = [idx for idx, group in enumerate(phrase_groups) if int(group.get("note_idx", -1)) == note_idx]
        if not matches:
            continue
        # Use latest entry for that note in sorted order.
        target_i = matches[-1]
        group_len = len(phrase_groups[target_i]["ids"])
        coda_len = int(spec["coda_len"])
        if coda_len <= 0 or group_len <= coda_len:
            continue
        coda_start = group_offsets[target_i] + (group_len - coda_len)
        vowel_idx = coda_start - 1
        coda_tails.append(
            {
                "vowel_idx": vowel_idx,
                "coda_start": coda_start,
                "coda_len": coda_len,
                "tail_frames": int(spec["tail_frames"]),
            }
        )
    positions = [int(group["position"]) for group in phrase_groups]
    positions_next = positions + [int(end_frames[-1])]
    durations = [max(1, positions_next[i + 1] - positions_next[i]) for i in range(len(positions))]
    word_boundaries = [len(group["ids"]) for group in phrase_groups]
    group_note_indices = [int(group.get("note_idx", -1)) for group in phrase_groups]
    group_anchor_frames = []
    for idx, start in enumerate(positions):
        end = positions_next[idx + 1]
        group_anchor_frames.append(
            {
                "start_frame": int(start),
                "end_frame": int(end),
            }
        )
    phoneme_ids = [pid for group in phrase_groups for pid in group["ids"]]
    language_ids = [lid for group in phrase_groups for lid in group["lang_ids"]]
    tones = [float(group["tone"]) for group in phrase_groups]
    phonemes_flat = [ph for group in phrase_groups for ph in group["phonemes"]]

    # Expand per-group positions/durations to per-phoneme axis.
    ph_positions: List[int] = []
    ph_durations: List[int] = []
    ph_tones: List[float] = []
    for i, group in enumerate(phrase_groups):
        count = len(group["ids"])
        if count <= 0:
            continue
        # Keep first count-1 phonemes as 1 frame and reserve tail duration for the final phoneme.
        dur = durations[i]
        if count == 1:
            unit_durs = [dur]
        else:
            head = [1] * (count - 1)
            tail = max(1, dur - (count - 1))
            unit_durs = head + [tail]
        cursor = positions[i]
        for d in unit_durs:
            ph_positions.append(cursor)
            ph_durations.append(d)
            ph_tones.append(float(group["tone"]))
            cursor += d

    payload = {
        "phoneme_ids": phoneme_ids,
        "language_ids": language_ids,
        "durations": ph_durations,
        "positions": ph_positions,
        "word_boundaries": word_boundaries,
        "word_durations": durations,
        "word_pitches": tones,
        "group_note_indices": group_note_indices,
        "group_anchor_frames": group_anchor_frames,
        "tones": ph_tones,
        "note_phonemes": note_phonemes,
        "note_slur": note_slur,
        "note_durations": note_durations,
        "coda_tails": coda_tails,
        "slur_note_groups": slur_note_groups,
        "durations_debug": ph_durations,
        "positions_debug": ph_positions,
    }
    if include_phonemes:
        payload["phonemes"] = phonemes_flat

    errors = validate_ds_contract(payload)
    if errors:
        raise ValueError(f"DS contract validation failed: {errors}")
    return payload
