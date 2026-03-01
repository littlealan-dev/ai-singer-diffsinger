"""Canonical lint rule metadata for voice-part preprocessing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class LintRuleSpec:
    code: str
    name: str
    definition: str
    fail_condition: str
    suggestion: str
    message_template: str


LINT_RULE_SPECS: Dict[str, LintRuleSpec] = {
    "plan_requires_sections": LintRuleSpec(
        code="plan_requires_sections",
        name="Complex Part Requires Sections",
        definition="Complex multi-voice or chordal material must be planned with explicit timeline sections.",
        fail_condition="The target part contains chord regions or split-needed regions, but the plan uses the simple non-section action path.",
        suggestion="Rewrite the target as a sections-based timeline plan and split behavior at the relevant measure boundaries.",
        message_template="Score complexity requires a section-by-section preprocess plan instead of the simple action path.",
    ),
    "mixed_region_requires_sections": LintRuleSpec(
        code="mixed_region_requires_sections",
        name="Mixed Region Qualities Require Sections",
        definition="A part with mixed resolved and unassigned regions needs explicit sectional handling.",
        fail_condition="The target part mixes resolved regions with unassigned/default-source regions, but the plan does not section them explicitly.",
        suggestion="Split the plan into sections so each region uses an explicit source or rest behavior.",
        message_template="Part contains mixed region qualities {region_statuses} which require explicit sectional handling.",
    ),
    "section_timeline_contiguous_no_gaps": LintRuleSpec(
        code="section_timeline_contiguous_no_gaps",
        name="Sections Must Be Contiguous",
        definition="Timeline sections for a target must fully cover the part span without gaps or overlaps.",
        fail_condition="A target's sections are out of order, overlap, or leave a gap in contiguous measure coverage.",
        suggestion="Rewrite the section boundaries so they are contiguous from start to end with no gaps or overlaps.",
        message_template="Target sections must be contiguous with no gaps or overlaps.",
    ),
    "trivial_method_requires_equal_chord_voice_part_count": LintRuleSpec(
        code="trivial_method_requires_equal_chord_voice_part_count",
        name="Trivial Split Requires Matching Chord Density",
        definition="The trivial split method is only valid when the target lane count matches the maximum simultaneous note count in the source section.",
        fail_condition="A section uses method=trivial for SPLIT_CHORDS_SELECT_NOTES, but the local source chord density does not match the target lane count.",
        suggestion="Use method=ranked with an explicit rank_index, or revise the section split so the target lane count matches the source section's maximum simultaneous note count.",
        message_template="Trivial chord splitting requires the target lane count to match the maximum simultaneous note count in the source section.",
    ),
    "cross_staff_melody_source_when_local_available": LintRuleSpec(
        code="cross_staff_melody_source_when_local_available",
        name="Cross-Staff Melody Source When Local Material Exists",
        definition="Melody sourcing should stay local to the target part when the target part already has sung material in the section.",
        fail_condition="A derive section pulls melody from another part even though the target part has local sung material in that range.",
        suggestion="Use a same-part melody source for that section unless the user explicitly asked for cross-part sourcing.",
        message_template="Selected melody source crosses parts even though local sung material exists in the target part.",
    ),
    "cross_staff_lyric_source_when_local_available": LintRuleSpec(
        code="cross_staff_lyric_source_when_local_available",
        name="Cross-Staff Lyric Source When Local Word Lyrics Exist",
        definition="Lyric sourcing should stay local to the target part when the target part already has word lyrics in the section.",
        fail_condition="A derive section pulls lyrics from another part even though the target part has local word-bearing lyrics in that range.",
        suggestion="Use a same-part lyric source with local word lyrics unless the user explicitly asked for cross-part lyric sourcing.",
        message_template="Selected lyric source crosses parts even though local word lyrics exist in the target part.",
    ),
    "extension_only_lyric_source_with_word_alternative": LintRuleSpec(
        code="extension_only_lyric_source_with_word_alternative",
        name="Extension-Only Lyric Source With Better Alternative",
        definition="A lyric source with only extension lyrics is invalid when another same-part source has real word lyrics in the same section.",
        fail_condition="The selected same-part lyric source has zero word lyrics and one or more extension lyrics, while another same-part source has word lyrics.",
        suggestion="Switch lyric_source to the suggested same-part source with real word lyrics.",
        message_template="Selected lyric source has only extension lyrics in this section, while another same-part source has real word lyrics.",
    ),
    "empty_lyric_source_with_word_alternative": LintRuleSpec(
        code="empty_lyric_source_with_word_alternative",
        name="Empty Lyric Source With Better Alternative",
        definition="A lyric source with no lyrics is invalid when another same-part source has real word lyrics in the same section.",
        fail_condition="The selected same-part lyric source has zero lyric notes, while another same-part source has word lyrics.",
        suggestion="Switch lyric_source to the suggested same-part source with real word lyrics.",
        message_template="Selected lyric source has no lyrics in this section, while another same-part source has real word lyrics.",
    ),
    "weak_lyric_source_with_better_alternative": LintRuleSpec(
        code="weak_lyric_source_with_better_alternative",
        name="Weak Lyric Source With Better Alternative",
        definition="A same-part lyric source is weak when its real word coverage is materially worse than another same-part alternative in the same section.",
        fail_condition="The selected same-part lyric source has low word-lyric coverage and another same-part source exceeds the configured word-count and coverage deltas.",
        suggestion="Switch lyric_source to the suggested same-part source with materially better real word coverage.",
        message_template="Selected lyric source has weak word-lyric coverage in this section, while another same-part source has materially better word-lyric coverage.",
    ),
    "lyric_source_without_target_notes": LintRuleSpec(
        code="lyric_source_without_target_notes",
        name="Lyric Source Without Target Notes",
        definition="Lyric-only propagation is invalid when the target lane has no native sung notes in the section.",
        fail_condition="A section specifies lyric_source without melody_source, and the target lane has no native sung notes in that range.",
        suggestion="Add melody_source for the section, choose a different target lane, or make the section rest if it should be silent.",
        message_template="Section uses lyric_source without melody_source, but target lane has no native sung notes in this range.",
    ),
    "no_rest_when_target_has_native_notes": LintRuleSpec(
        code="no_rest_when_target_has_native_notes",
        name="Rest Not Allowed Over Native Notes",
        definition="A target section cannot be set to rest when the target lane already has native sung notes in that measure range.",
        fail_condition="A rest section overlaps measures where the target lane has native notes.",
        suggestion="Change the overlapping section to derive mode or split the section so only truly silent measures use rest.",
        message_template="Rest mode overlaps measures where the target lane already has native sung notes.",
    ),
    "same_clef_claim_coverage": LintRuleSpec(
        code="same_clef_claim_coverage",
        name="Same-Part Claim Coverage",
        definition="Timeline targets must claim all sung measures in a part that is being materialized.",
        fail_condition="One or more sung measures in the part are not claimed by any derive section across the target lanes.",
        suggestion="Expand the derive/rest section coverage so every sung measure in the part is explicitly handled.",
        message_template="One or more sung measures in the part are not claimed by the current target timeline coverage.",
    ),
    "same_part_target_completeness": LintRuleSpec(
        code="same_part_target_completeness",
        name="Same-Part Target Completeness",
        definition="When one non-default sibling lane in a part is targeted, all non-default sibling lanes in that part must be included.",
        fail_condition="The plan targets only a subset of same-part sibling voice parts, leaving one or more expected sibling targets missing.",
        suggestion="Include all required same-part sibling targets using their canonical voice_part_id values.",
        message_template="The plan is missing one or more required same-part sibling voice-part targets.",
    ),
}


def get_lint_rule_spec(code: str) -> LintRuleSpec:
    return LINT_RULE_SPECS[code]


def render_lint_rules_for_prompt() -> str:
    lines = [
        "SVS Voice-Part Lint Rules (Canonical Runtime Validation)",
        "",
        "Use these runtime rules as the source of truth when planning or repairing preprocess plans.",
        "If a lint failure references one of these rule codes, fix the plan according to the rule suggestion and the reported failing attributes.",
    ]
    for spec in LINT_RULE_SPECS.values():
        lines.extend(
            [
                "",
                f"- Rule code: {spec.code}",
                f"  Name: {spec.name}",
                f"  Definition: {spec.definition}",
                f"  Fails when: {spec.fail_condition}",
                f"  Suggested fix: {spec.suggestion}",
            ]
        )
    return "\n".join(lines)
