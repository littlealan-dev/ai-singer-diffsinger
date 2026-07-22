"""Syllable alignment for DiffSinger contract payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from src.api.phonemize import phonemize
from src.api.timing_errors import InfeasibleAnchorError
from src.api.voicebank_cache import (
    resolve_manifest_onset_anchor_adapters,
    resolve_manifest_pronunciation_adapters,
)
from src.mcp.logging_utils import get_logger
from src.phonemizer.phonemizer import Phonemizer


logger = get_logger(__name__)


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


def _resolve_duration_model_groups(
    phrase_groups: List[Dict[str, Any]],
    phonemes_flat: List[str],
) -> List[Dict[str, Any]]:
    """Return duration-model groups without ever reordering rendered phones.

    An onset adapter builds a preferred duration-model grouping before a later
    word can carry an onset into the finalized rendered stream.  When that
    carry changes the phone order, the finalized phrase groups are the only
    safe duration-model grouping.
    """
    duration_model_groups: List[Dict[str, Any]] = []
    for phrase_group in phrase_groups:
        override = phrase_group.get("duration_model_override")
        if override:
            duration_model_groups.extend(override)
        elif not phrase_group.get("duration_model_override_skip"):
            duration_model_groups.append(phrase_group)
    duration_model_groups.sort(
        key=lambda value: (int(value.get("note_idx", 0)), int(value["position"]))
    )
    duration_model_phones = [
        phone
        for model_group in duration_model_groups
        for phone in model_group["phonemes"]
    ]
    if duration_model_phones == phonemes_flat:
        return duration_model_groups

    logger.warning(
        "duration_model_override_stale; using finalized rendered groups"
    )
    return list(phrase_groups)


def _syllable_start_indices(
    phonemes: List[str],
    phonemizer: Phonemizer,
    logical_spans: Optional[List[Dict[str, Any]]] = None,
    forced_following_onsets: int = 0,
) -> List[int]:
    """Return phoneme start indices for syllable/note anchor mapping."""
    if not phonemes:
        return []
    is_vowel = [phonemizer.is_vowel(p) for p in phonemes]
    is_glide = [phonemizer.is_glide(p) for p in phonemes]
    logical_vowel_indices = {
        index
        for span in logical_spans or []
        for index in range(
            int(span.get("start", -1)) + 1,
            min(len(phonemes), int(span.get("end", -1)) + 1),
        )
        if index >= 0
    }
    remaining_forced_onsets = max(0, int(forced_following_onsets))
    starts = [False] * len(phonemes)
    if not any(is_vowel):
        starts[0] = True
    for i in range(len(phonemes)):
        # A logical rolled-R expansion contains virtual vowels plus its real
        # vowel.  It owns one scored syllable, so those placeholder vowels
        # must not consume anchors reserved for the following real syllables.
        if is_vowel[i] and i not in logical_vowel_indices:
            if i >= 2 and is_glide[i - 1] and not is_vowel[i - 2]:
                starts[i - 1] = True
            elif (
                remaining_forced_onsets > 0
                and i >= 1
                and not is_vowel[i - 1]
                and any(is_vowel[: i - 1])
            ):
                starts[i - 1] = True
                remaining_forced_onsets -= 1
            else:
                starts[i] = True
    for span in logical_spans or []:
        start = int(span.get("start", -1))
        end = int(span.get("end", -1))
        if start < 0 or start >= len(starts):
            continue
        # A rolled-r span is one logical syllable. Its virtual vowels and the
        # following real vowel must not create extra note anchors.
        starts[start] = True
        for index in range(start + 1, min(len(starts), end + 1)):
            starts[index] = False
        # The real vowel is immediately after the expanded prefix. If another
        # syllable follows, its onset belongs with its own carrier note instead
        # of being absorbed by the rolled-r syllable's vowel-led chunk.
        next_syllable = end + 1
        if next_syllable < len(starts):
            starts[next_syllable] = True
    out = [idx for idx, flag in enumerate(starts) if flag]
    return sorted(set(out)) if out else [0]


def _split_phonemes_into_syllable_chunks(
    *,
    phonemes: List[str],
    ids: List[int],
    lang_ids: List[int],
    phonemizer: Phonemizer,
    expected_chunks: int,
    attach_lead_to_first: bool = True,
    logical_spans: Optional[List[Dict[str, Any]]] = None,
    forced_following_onsets: int = 0,
) -> List[Dict[str, Any]]:
    """Split phoneme sequence into syllable chunks for note-level assignment."""
    if expected_chunks <= 1 or len(phonemes) <= 1:
        return [{"phonemes": phonemes, "ids": ids, "lang_ids": lang_ids}]

    start_indices = _syllable_start_indices(
        phonemes,
        phonemizer,
        logical_spans,
        forced_following_onsets,
    )

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
        if idx == 0 and lead_ph and attach_lead_to_first:
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


def _materialize_logical_pronunciation(
    data: Dict[str, Any],
    phonemizer: Phonemizer,
    adapters: List[Dict[str, Any]],
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Expand alignment-only markers into voicebank-supported runtime phones."""
    markers = {str(adapter["logical_symbol"]): adapter for adapter in adapters}
    phonemes = list(data.get("phonemes") or [])
    ids = list(data.get("ids") or [])
    lang_ids = list(data.get("lang_ids") or [])
    runtime_phonemes: List[str] = []
    runtime_ids: List[int] = []
    runtime_language_ids: List[int] = []
    spans: List[Dict[str, Any]] = []

    for index, phoneme in enumerate(phonemes):
        adapter = markers.get(phoneme)
        if adapter is None:
            runtime_phonemes.append(phoneme)
            runtime_ids.append(ids[index])
            runtime_language_ids.append(lang_ids[index])
            continue
        expansion = [str(value) for value in adapter["expand_phonemes"]]
        start = len(runtime_phonemes)
        for runtime_phone in expansion:
            runtime_phonemes.append(runtime_phone)
            runtime_ids.append(phonemizer._phoneme_to_id[runtime_phone])
            language_code = runtime_phone.split("/", 1)[0] if "/" in runtime_phone else ""
            runtime_language_ids.append(phonemizer._language_map.get(language_code, 0))
        spans.append(
            {
                "start": start,
                "end": start + len(expansion),
                "prefix_frames": list(adapter["prefix_frames"]),
                "main_onset_frames": int(adapter["main_onset_frames"]),
                "adapter_id": str(adapter["id"]),
            }
        )
    return (
        {
            **data,
            "phonemes": runtime_phonemes,
            "ids": runtime_ids,
            "lang_ids": runtime_language_ids,
        },
        spans,
    )


def _match_initial_onset_anchor_adapter(
    phonemes: List[str],
    adapters: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the exact manifest onset rule matching this phonemized word."""
    for adapter in adapters:
        prefix = adapter.get("prefix_phonemes")
        if isinstance(prefix, list) and phonemes[: len(prefix)] == prefix:
            return adapter
    return None


def _match_logical_onset_timing_adapter(
    phonemes: List[str],
    timing_rule: Optional[Dict[str, Any]],
    adapters: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Match a timing-only onset adapter to one materialized logical span.

    A logical pronunciation may begin after a scored syllable's lead phones
    (for example ``pe-rro``).  It must keep those original syllable anchors,
    but its virtual onset still needs an adaptive duration clamp.  Matching by
    logical adapter id prevents an ordinary identical phone sequence from
    receiving the special timing policy.
    """
    if not isinstance(timing_rule, dict):
        return None
    logical_adapter_id = timing_rule.get("adapter_id")
    if not isinstance(logical_adapter_id, str) or not logical_adapter_id:
        return None
    for adapter in adapters:
        if adapter.get("logical_adapter_id") != logical_adapter_id:
            continue
        prefix = adapter.get("prefix_phonemes")
        if isinstance(prefix, list) and phonemes[: len(prefix)] == prefix:
            return adapter
    return None


def _append_carried_prefix_to_duration_model_override(
    phrase_groups: List[Dict[str, Any]],
    *,
    note_idx: int,
    phonemes: List[str],
    ids: List[int],
    lang_ids: List[int],
) -> bool:
    """Keep a duration-model override in sync with a later prefix carry.

    A vowel-led word can carry its consonant prefix onto the preceding scored
    group.  If that group belongs to an onset-adapter override, the override
    was assembled before the carry and must receive the same phones to retain
    the rendered stream exactly.
    """
    for phrase_group in reversed(phrase_groups):
        override = phrase_group.get("duration_model_override")
        if not override:
            continue
        targets = [
            group
            for group in override
            if int(group.get("note_idx", -1)) == int(note_idx)
        ]
        if not targets:
            return False
        target = targets[-1]
        target["phonemes"].extend(phonemes)
        target["ids"].extend(ids)
        target["lang_ids"].extend(lang_ids)
        return True
    return False


def _build_onset_anchor_duration_model_groups(
    *,
    phonemes: List[str],
    ids: List[int],
    lang_ids: List[int],
    phonemizer: Phonemizer,
    carrier_indices: List[int],
    timing_midi: List[float],
    start_frames: List[int],
    logical_spans: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Recreate legacy duration-model context while retaining an exact onset prefix.

    The rendered stream attaches ``g+w`` to the first scored syllable.  The
    duration model instead receives its historic prefix/carry partition, with
    the otherwise-lost glide retained beside the leading consonant.  Both
    streams therefore contain the same phones in the same order.
    """
    if not carrier_indices:
        return []
    start_indices = _syllable_start_indices(phonemes, phonemizer, logical_spans)
    first_start = start_indices[0] if start_indices else 0
    outer_lead_ph = phonemes[:first_start]
    outer_lead_ids = ids[:first_start]
    outer_lead_lang = lang_ids[:first_start]
    core_ph = phonemes[first_start:]
    core_ids = ids[first_start:]
    core_lang = lang_ids[first_start:]
    core_spans = [
        {
            **span,
            "start": int(span["start"]) - first_start,
            "end": int(span["end"]) - first_start,
        }
        for span in logical_spans
        if int(span["start"]) >= first_start
    ]
    core_starts = _syllable_start_indices(core_ph, phonemizer, core_spans)
    core_first_start = core_starts[0] if core_starts else 0
    hidden_prefix_ph = core_ph[:core_first_start]
    hidden_prefix_ids = core_ids[:core_first_start]
    hidden_prefix_lang = core_lang[:core_first_start]
    chunks = _split_phonemes_into_syllable_chunks(
        phonemes=core_ph,
        ids=core_ids,
        lang_ids=core_lang,
        phonemizer=phonemizer,
        expected_chunks=max(1, len(carrier_indices)),
        attach_lead_to_first=False,
        logical_spans=core_spans,
    )
    first_note = int(carrier_indices[0])
    groups: List[Dict[str, Any]] = [
        {
            "position": start_frames[first_note],
            "phonemes": outer_lead_ph + hidden_prefix_ph,
            "ids": outer_lead_ids + hidden_prefix_ids,
            "lang_ids": outer_lead_lang + hidden_prefix_lang,
            "tone": float(timing_midi[first_note]),
            "note_idx": first_note,
        }
    ]
    for idx, chunk in enumerate(chunks):
        if not chunk["phonemes"]:
            continue
        note_idx = int(carrier_indices[min(idx, len(carrier_indices) - 1)])
        groups.append(
            {
                "position": start_frames[note_idx],
                "phonemes": list(chunk["phonemes"]),
                "ids": list(chunk["ids"]),
                "lang_ids": list(chunk["lang_ids"]),
                "tone": float(timing_midi[note_idx]),
                "note_idx": note_idx,
            }
        )
    return [group for group in groups if group["phonemes"]]


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


def _nearest_vowel_distance(index: int, vowel_indices: List[int]) -> int:
    if not vowel_indices:
        return 10**9
    return min(abs(index - v) for v in vowel_indices)


def _compress_group_to_anchor_budget(
    *,
    phonemes: List[str],
    ids: List[int],
    lang_ids: List[int],
    anchor_total: int,
    phonemizer: Phonemizer,
    group_index: int,
    note_index: int,
) -> tuple[List[str], List[int], List[int]]:
    """Fallback-only compression when phoneme count exceeds anchor frame budget."""
    if len(ids) <= anchor_total:
        return phonemes, ids, lang_ids

    keep = [True] * len(ids)

    while sum(1 for flag in keep if flag) > anchor_total:
        kept_indices = [idx for idx, flag in enumerate(keep) if flag]
        kept_vowels = [idx for idx in kept_indices if phonemizer.is_vowel(phonemes[idx])]
        kept_vowel_set = set(kept_vowels)
        kept_consonants = [idx for idx in kept_indices if idx not in kept_vowel_set]
        if not kept_consonants:
            break

        # Merge adjacent consonants first, then nearest-to-vowel, then left-to-right.
        consonant_set = set(kept_consonants)
        ranked: List[tuple[int, int, int]] = []
        for idx in kept_consonants:
            adjacent_consonant = int(
                (idx - 1 in consonant_set) or (idx + 1 in consonant_set)
            )
            rank_adjacent = 0 if adjacent_consonant else 1
            rank_distance = _nearest_vowel_distance(idx, kept_vowels)
            ranked.append((rank_adjacent, rank_distance, idx))
        ranked.sort()
        drop_idx = ranked[0][2]
        keep[drop_idx] = False

    filtered_indices = [idx for idx, flag in enumerate(keep) if flag]
    if len(filtered_indices) > anchor_total:
        raise InfeasibleAnchorError(
            stage="aligner_v2_anchor_budget",
            group_index=int(group_index),
            note_index=int(note_index),
            anchor_total=int(anchor_total),
            phoneme_count=int(len(filtered_indices)),
            detail="insufficient_anchor_budget_after_fallback",
        )

    return (
        [phonemes[idx] for idx in filtered_indices],
        [ids[idx] for idx in filtered_indices],
        [lang_ids[idx] for idx in filtered_indices],
    )


def _build_group_anchor_frames(
    *,
    phrase_groups: List[Dict[str, Any]],
    note_durations: List[int],
    phonemizer: Phonemizer,
) -> tuple[List[int], List[int], List[Dict[str, int]]]:
    """Build non-overlapping group anchor windows over note-frame budgets."""
    seq_starts: List[int] = [0]
    running_note = 0
    for note_dur in note_durations:
        running_note += int(note_dur)
        seq_starts.append(running_note)

    def _norm_note_index(raw_idx: Any) -> int:
        try:
            note_idx = int(raw_idx)
        except (TypeError, ValueError):
            note_idx = 0
        if note_idx < 0:
            return 0
        if note_idx >= len(note_durations):
            return max(0, len(note_durations) - 1)
        return note_idx

    positions: List[int] = []
    durations: List[int] = []
    group_anchor_frames: List[Dict[str, int]] = []
    i = 0
    while i < len(phrase_groups):
        run_start = i
        note_idx = _norm_note_index(phrase_groups[i].get("note_idx", 0))
        j = i + 1
        while j < len(phrase_groups):
            next_idx = _norm_note_index(phrase_groups[j].get("note_idx", 0))
            if next_idx != note_idx:
                break
            j += 1

        if j < len(phrase_groups):
            next_note_idx = _norm_note_index(phrase_groups[j].get("note_idx", len(note_durations)))
            next_note_idx = max(note_idx + 1, min(next_note_idx, len(note_durations)))
        else:
            next_note_idx = len(note_durations)
        span_start = int(seq_starts[note_idx])
        span_total = int(sum(int(x) for x in note_durations[note_idx:next_note_idx]))
        if span_total <= 0:
            span_total = 1

        run_groups = phrase_groups[run_start:j]
        while True:
            counts = [len(group.get("ids") or []) for group in run_groups]
            total_count = int(sum(counts))
            if total_count <= span_total:
                break

            candidates: List[tuple[int, int]] = []
            for local_idx, group in enumerate(run_groups):
                phs = list(group.get("phonemes") or [])
                if len(phs) <= 1:
                    continue
                consonant_only = all(not phonemizer.is_vowel(ph) for ph in phs)
                candidates.append((0 if consonant_only else 1, local_idx))
            candidates.sort(key=lambda item: (item[0], item[1]))

            reduced = False
            for _, local_idx in candidates:
                group = run_groups[local_idx]
                target = len(group.get("ids") or []) - 1
                if target < 1:
                    continue
                compressed = _compress_group_to_anchor_budget(
                    phonemes=list(group.get("phonemes") or []),
                    ids=list(group.get("ids") or []),
                    lang_ids=list(group.get("lang_ids") or []),
                    anchor_total=target,
                    phonemizer=phonemizer,
                    group_index=run_start + local_idx,
                    note_index=note_idx,
                )
                if len(compressed[1]) < len(group.get("ids") or []):
                    group["phonemes"], group["ids"], group["lang_ids"] = compressed
                    reduced = True
                    break

            if not reduced:
                raise InfeasibleAnchorError(
                    stage="aligner_v2_anchor_budget",
                    group_index=int(run_start),
                    note_index=int(note_idx),
                    anchor_total=int(span_total),
                    phoneme_count=int(total_count),
                    detail="insufficient_anchor_budget",
                )

        group_counts = [len(group.get("ids") or []) for group in run_groups]
        budgets = list(group_counts)
        remaining = span_total - int(sum(budgets))
        if remaining > 0 and budgets:
            budgets[-1] += remaining

        cursor = span_start
        for budget in budgets:
            start = int(cursor)
            end = start + int(max(1, budget))
            positions.append(start)
            durations.append(end - start)
            group_anchor_frames.append(
                {
                    "start_frame": start,
                    "end_frame": end,
                    "note_index": int(note_idx),
                }
            )
            cursor = end

        i = j

    return positions, durations, group_anchor_frames


def validate_ds_contract(payload: Dict[str, Any]) -> List[str]:
    """Validate DiffSinger alignment contract invariants."""
    errs: List[str] = []
    phoneme_ids = payload.get("phoneme_ids", [])
    language_ids = payload.get("language_ids", [])
    durations = payload.get("durations", [])
    positions = payload.get("positions", [])
    word_boundaries = payload.get("word_boundaries", [])
    vowel_flags = payload.get("vowel_flags", [])
    note_slur = payload.get("note_slur", [])
    note_durations = payload.get("note_durations", [])
    group_anchor_frames = payload.get("group_anchor_frames", [])

    n = len(phoneme_ids)
    if len(language_ids) != n or len(durations) != n or len(positions) != n:
        errs.append("length_mismatch phoneme_ids/language_ids/durations/positions")
    if vowel_flags and len(vowel_flags) != n:
        errs.append("length_mismatch vowel_flags/phoneme_ids")
    if any(int(d) < 1 for d in durations):
        errs.append("invalid_duration_nonpositive")
    if int(sum(int(x) for x in word_boundaries)) != n:
        errs.append("invalid_word_boundaries_sum")
    if note_slur and note_durations and len(note_slur) != len(note_durations):
        errs.append("length_mismatch note_slur/note_durations")
    if durations:
        sum_durations = int(sum(int(x) for x in durations))
        if group_anchor_frames:
            sum_anchor_frames = int(
                sum(
                    max(
                        0,
                        int(anchor.get("end_frame", 0))
                        - int(anchor.get("start_frame", 0)),
                    )
                    for anchor in group_anchor_frames
                )
            )
            if sum_durations != sum_anchor_frames:
                errs.append("frame_conservation_failed")
        elif note_durations and sum_durations != int(sum(int(x) for x in note_durations)):
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


def _contract_diagnostics(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return compact diagnostics for DS contract validation failures."""
    phoneme_ids = payload.get("phoneme_ids", [])
    durations = payload.get("durations", [])
    word_boundaries = payload.get("word_boundaries", [])
    note_durations = payload.get("note_durations", [])
    group_anchor_frames = payload.get("group_anchor_frames", [])
    return {
        "phoneme_count": len(phoneme_ids),
        "duration_count": len(durations),
        "word_group_count": len(word_boundaries),
        "note_count": len(note_durations),
        "sum_durations": int(sum(int(x) for x in durations)) if durations else 0,
        "sum_note_durations": int(sum(int(x) for x in note_durations)) if note_durations else 0,
        "anchor_group_count": len(group_anchor_frames),
        "sum_anchor_frames": int(
            sum(
                max(
                    0,
                    int(anchor.get("end_frame", 0)) - int(anchor.get("start_frame", 0)),
                )
                for anchor in group_anchor_frames
            )
        ) if group_anchor_frames else 0,
    }


def align(
    *,
    notes: List[Dict[str, Any]],
    start_frames: List[int],
    end_frames: List[int],
    timing_midi: List[float],
    note_durations: List[int],
    phonemizer: Phonemizer,
    voicebank_path: Path,
    language: str = "en",
    include_phonemes: bool = False,
    solfege_pronunciation_patch: bool = False,
) -> Dict[str, Any]:
    """Align score notes into DS-contract payload using syllable-based strategy."""
    sp_id = phonemizer._phoneme_to_id["SP"]
    groups = _group_notes(notes)

    lyrics: List[str] = []
    for group in groups:
        if group["is_rest"]:
            continue
        lyric = _resolve_group_lyric(group)
        # Mirror DiffSingerBasePhonemizer defaultPause behavior: missing lyrics still
        # contribute a silence token so timing budgets remain conserved.
        lyrics.append(lyric if lyric else "SP")

    word_phonemes: List[Dict[str, Any]] = []
    pronunciation_adapters = resolve_manifest_pronunciation_adapters(
        voicebank_path,
        language,
    )
    onset_anchor_adapters = resolve_manifest_onset_anchor_adapters(
        voicebank_path,
        language,
    )
    if lyrics:
        word_phonemes = _split_phonemize_result(
            phonemize(
                lyrics,
                voicebank_path,
                language=language,
                solfege_pronunciation_patch=solfege_pronunciation_patch,
                logical_pronunciation=bool(pronunciation_adapters),
            )
        )
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
        data, logical_spans = _materialize_logical_pronunciation(
            data,
            phonemizer,
            pronunciation_adapters,
        )
        phonemes = list(data.get("phonemes", [])) or ["SP"]
        ids = list(data.get("ids", [])) if data.get("phonemes") else [sp_id]
        lang_ids = list(data.get("lang_ids", [])) if data.get("phonemes") else [0]
        onset_adapter = _match_initial_onset_anchor_adapter(
            phonemes,
            onset_anchor_adapters,
        )

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

        start_indices = _syllable_start_indices(
            phonemes,
            phonemizer,
            logical_spans,
        )
        first_start = start_indices[0] if start_indices else 0
        lead_ph = phonemes[:first_start] if first_start > 0 else []
        lead_ids = ids[:first_start] if first_start > 0 else []
        lead_lang = lang_ids[:first_start] if first_start > 0 else []
        core_ph = phonemes[first_start:] if first_start > 0 else phonemes
        core_ids = ids[first_start:] if first_start > 0 else ids
        core_lang = lang_ids[first_start:] if first_start > 0 else lang_ids
        core_spans = [
            {
                **span,
                "start": int(span["start"]) - first_start,
                "end": int(span["end"]) - first_start,
            }
            for span in logical_spans
            if int(span["start"]) >= first_start
        ]

        forced_following_onsets = 0
        if onset_adapter is not None and onset_adapter.get(
            "preserve_following_syllable_onsets", False
        ):
            forced_following_onsets = max(0, len(carrier_indices) - 1)
            configured_limit = onset_adapter.get("max_forced_following_onsets")
            if configured_limit is not None:
                forced_following_onsets = min(
                    forced_following_onsets,
                    int(configured_limit),
                )
        chunks = _split_phonemes_into_syllable_chunks(
            phonemes=core_ph,
            ids=core_ids,
            lang_ids=core_lang,
            phonemizer=phonemizer,
            expected_chunks=max(1, len(carrier_indices)),
            attach_lead_to_first=onset_adapter is not None,
            logical_spans=core_spans,
            forced_following_onsets=forced_following_onsets,
        )

        # The manifest rule is an exact phone-sequence match. It routes just
        # that leading prefix to the first carrier; ordinary C+glide words
        # retain the legacy prefix behavior and duration-model grouping.
        attached_lead_count = 0
        if onset_adapter is not None and lead_ph and chunks:
            # ``core_spans`` remains indexed against ``core_ph``.  Keep track
            # of this presentation-only prepend so span matching below does
            # not shift a following logical pronunciation onto this carrier.
            attached_lead_count = len(lead_ph)
            chunks[0]["phonemes"] = lead_ph + chunks[0]["phonemes"]
            chunks[0]["ids"] = lead_ids + chunks[0]["ids"]
            chunks[0]["lang_ids"] = lead_lang + chunks[0]["lang_ids"]
            lead_ph, lead_ids, lead_lang = [], [], []

        duration_model_override: List[Dict[str, Any]] = []
        if onset_adapter is not None and len(carrier_indices) > 1:
            duration_model_override = _build_onset_anchor_duration_model_groups(
                phonemes=phonemes,
                ids=ids,
                lang_ids=lang_ids,
                phonemizer=phonemizer,
                carrier_indices=carrier_indices,
                timing_midi=timing_midi,
                start_frames=start_frames,
                logical_spans=logical_spans,
            )
            rendered_phones = [phone for chunk in chunks for phone in chunk["phonemes"]]
            model_phones = [
                phone
                for model_group in duration_model_override
                for phone in model_group["phonemes"]
            ]
            if rendered_phones != model_phones:
                raise ValueError(
                    "Onset anchor adapter must preserve the duration-model phoneme stream"
                )

        # ``core_spans`` is indexed before a declared onset adapter prepends
        # its lead phones to the first rendered chunk.  Start the rendered
        # cursor before zero to retain that coordinate system.  Otherwise a
        # logical /rr/ immediately after e.g. /t j e/ is assigned to ``tie``
        # instead of its intended ``rra`` carrier.
        chunk_offset = -attached_lead_count
        for chunk in chunks:
            chunk_end = chunk_offset + len(chunk["phonemes"])
            rule = next(
                (
                    span
                    for span in core_spans
                    if chunk_offset <= int(span["start"]) < chunk_end
                ),
                None,
            )
            if rule is not None:
                chunk["timing_rule"] = rule
            chunk_offset = chunk_end

        onset_timing = onset_adapter.get("onset_timing") if onset_adapter else None
        if isinstance(onset_timing, dict) and chunks and chunks[0]["phonemes"]:
            timing_rule = dict(chunks[0].get("timing_rule") or {})
            timing_rule.update(
                {
                    "adaptive_onset_prefix_count": len(onset_adapter["prefix_phonemes"]),
                    "adaptive_onset_frame_ratio": float(onset_timing["frame_ratio"]),
                    "adaptive_onset_min_frames": int(onset_timing["min_frames"]),
                    "adaptive_onset_max_frames": int(onset_timing["max_frames"]),
                }
            )
            chunks[0]["timing_rule"] = timing_rule

        # A materialized logical pronunciation can have a virtual onset inside
        # a word rather than at its first phoneme (for example ``pe-rro``).
        # Apply its adaptive onset budget to that exact logical span without
        # changing the word's existing syllable or duration-model grouping.
        for chunk in chunks:
            timing_rule = chunk.get("timing_rule")
            logical_timing_adapter = _match_logical_onset_timing_adapter(
                chunk["phonemes"],
                timing_rule,
                onset_anchor_adapters,
            )
            logical_onset_timing = (
                logical_timing_adapter.get("onset_timing")
                if logical_timing_adapter
                else None
            )
            if isinstance(logical_onset_timing, dict):
                updated_rule = dict(timing_rule or {})
                updated_rule.update(
                    {
                        "adaptive_onset_prefix_count": len(
                            logical_timing_adapter["prefix_phonemes"]
                        ),
                        "adaptive_onset_frame_ratio": float(
                            logical_onset_timing["frame_ratio"]
                        ),
                        "adaptive_onset_min_frames": int(
                            logical_onset_timing["min_frames"]
                        ),
                        "adaptive_onset_max_frames": int(
                            logical_onset_timing["max_frames"]
                        ),
                    }
                )
                chunk["timing_rule"] = updated_rule

        # A compact rolled-R prefix can leave a later, very short syllable
        # (for example ``mos`` in ``ro-ga-mos``) sharing its note with two
        # consonants. Reserve enough of that note for the vowel so the
        # duration model cannot turn it into a consonant-only transition.
        following_vowel_ratio = (
            onset_adapter.get("following_syllable_vowel_frame_ratio")
            if onset_adapter
            else None
        )
        if isinstance(following_vowel_ratio, (int, float)) and not isinstance(
            following_vowel_ratio, bool
        ):
            for chunk in chunks[1:]:
                timing_rule = dict(chunk.get("timing_rule") or {})
                timing_rule["min_vowel_frame_ratio"] = float(following_vowel_ratio)
                chunk["timing_rule"] = timing_rule

        # OpenUtau parity: carry pre-vowel prefix to previous anchor group when possible.
        if lead_ph:
            if phrase_groups:
                previous_group = phrase_groups[-1]
                prev_note_idx = int(previous_group.get("note_idx", -1))
                previous_group.setdefault("phonemes", []).extend(lead_ph)
                previous_group.setdefault("ids", []).extend(lead_ids)
                previous_group.setdefault("lang_ids", []).extend(lead_lang)
                if previous_group.get("duration_model_override_skip"):
                    _append_carried_prefix_to_duration_model_override(
                        phrase_groups,
                        note_idx=prev_note_idx,
                        phonemes=lead_ph,
                        ids=lead_ids,
                        lang_ids=lead_lang,
                    )
                if prev_note_idx >= 0:
                    note_phonemes.setdefault(prev_note_idx, []).extend(lead_ph)
            else:
                first_note_idx = int(carrier_indices[0]) if carrier_indices else int(note_indices[0])
                phrase_groups.append(
                    {
                        "position": start_frames[first_note_idx],
                        "phonemes": list(lead_ph),
                        "ids": list(lead_ids),
                        "lang_ids": list(lead_lang),
                        "tone": float(timing_midi[first_note_idx]),
                        "note_idx": first_note_idx,
                    }
                )
                note_phonemes.setdefault(first_note_idx, []).extend(lead_ph)

        for idx, note_idx in enumerate(carrier_indices):
            chunk = chunks[idx] if idx < len(chunks) else {"phonemes": [], "ids": [], "lang_ids": []}
            if not chunk["phonemes"]:
                continue
            phrase_group = {
                    "position": start_frames[note_idx],
                    "phonemes": chunk["phonemes"],
                    "ids": chunk["ids"],
                    "lang_ids": chunk["lang_ids"],
                    "timing_rule": chunk.get("timing_rule"),
                    "tone": float(timing_midi[note_idx]),
                    "note_idx": note_idx,
                }
            if idx == 0 and duration_model_override:
                phrase_group["duration_model_override"] = duration_model_override
            elif idx > 0 and duration_model_override:
                phrase_group["duration_model_override_skip"] = True
            phrase_groups.append(phrase_group)
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

    phrase_groups.sort(key=lambda g: (int(g.get("note_idx", 0)), int(g["position"])))

    # Build per-group anchor windows on note-order frame budgets, including
    # repeated note-index runs (e.g. phrase-initial prefix clusters).
    positions, durations, group_anchor_frames = _build_group_anchor_frames(
        phrase_groups=phrase_groups,
        note_durations=note_durations,
        phonemizer=phonemizer,
    )

    # Resolve coda-tail indices after anchor-budget compression so global offsets stay valid.
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

    # Recompute note_phonemes from finalized phrase_groups after compression/fallback.
    note_phonemes = {}
    for group in phrase_groups:
        note_idx = int(group.get("note_idx", -1))
        if note_idx < 0:
            continue
        note_phonemes.setdefault(note_idx, []).extend(list(group.get("phonemes") or []))

    word_boundaries = [len(group["ids"]) for group in phrase_groups]
    group_note_indices = [int(group.get("note_idx", -1)) for group in phrase_groups]
    phoneme_ids = [pid for group in phrase_groups for pid in group["ids"]]
    language_ids = [lid for group in phrase_groups for lid in group["lang_ids"]]
    tones = [float(group["tone"]) for group in phrase_groups]
    phoneme_timing_rules = [group.get("timing_rule") for group in phrase_groups]
    phonemes_flat = [ph for group in phrase_groups for ph in group["phonemes"]]
    vowel_flags = [bool(phonemizer.is_vowel(ph)) for ph in phonemes_flat]

    has_duration_model_override = any(
        group.get("duration_model_override") for group in phrase_groups
    )
    duration_model_boundaries: List[int] = []
    duration_model_durations: List[int] = []
    duration_model_pitches: List[float] = []
    if has_duration_model_override:
        duration_model_groups = _resolve_duration_model_groups(
            phrase_groups,
            phonemes_flat,
        )
        _, duration_model_durations, _ = _build_group_anchor_frames(
            phrase_groups=duration_model_groups,
            note_durations=note_durations,
            phonemizer=phonemizer,
        )
        duration_model_boundaries = [
            len(model_group["ids"]) for model_group in duration_model_groups
        ]
        duration_model_pitches = [
            float(model_group["tone"]) for model_group in duration_model_groups
        ]

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
        "vowel_flags": vowel_flags,
        "phoneme_timing_rules": phoneme_timing_rules,
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
    if has_duration_model_override:
        payload["duration_model_word_boundaries"] = duration_model_boundaries
        payload["duration_model_word_durations"] = duration_model_durations
        payload["duration_model_word_pitches"] = duration_model_pitches

    errors = validate_ds_contract(payload)
    if errors:
        raise ValueError(
            f"DS contract validation failed: {errors}; "
            f"diagnostics={_contract_diagnostics(payload)}"
        )
    return payload
