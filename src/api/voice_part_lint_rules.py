"""Canonical lint rule metadata for voice-part preprocessing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class LintRuleSpec:
    code: str
    name: str
    severity: str
    domain: str
    definition: str
    fail_condition: str
    suggestion: str
    message_template: str


LINT_RULE_SPECS: Dict[str, LintRuleSpec] = {
    "plan_requires_sections": LintRuleSpec(
        code="plan_requires_sections",
        name="Complex Part Requires Sections",
        severity="P1",
        domain="STRUCTURAL",
        definition="Complex multi-voice or chordal material must be planned with explicit timeline sections.",
        fail_condition="The target part contains chord regions or split-needed regions, but the plan uses the simple non-section action path.",
        suggestion="Rewrite the target as a sections-based timeline plan and split behavior at the relevant measure boundaries.",
        message_template="Score complexity requires a section-by-section preprocess plan instead of the simple action path.",
    ),
    "mixed_region_requires_sections": LintRuleSpec(
        code="mixed_region_requires_sections",
        name="Mixed Region Qualities Require Sections",
        severity="P1",
        domain="STRUCTURAL",
        definition="A part with mixed resolved and unassigned regions needs explicit sectional handling.",
        fail_condition="The target part mixes resolved regions with unassigned/default-source regions, but the plan does not section them explicitly.",
        suggestion="Split the plan into sections so each region uses an explicit source or rest behavior.",
        message_template="Part contains mixed region qualities {region_statuses} which require explicit sectional handling.",
    ),
    "section_timeline_contiguous_no_gaps": LintRuleSpec(
        code="section_timeline_contiguous_no_gaps",
        name="Sections Must Be Contiguous",
        severity="P0",
        domain="STRUCTURAL",
        definition="Timeline sections for a target must fully cover the part span without gaps or overlaps.",
        fail_condition="A target's sections are out of order, overlap, or leave a gap in contiguous measure coverage.",
        suggestion="Rewrite the section boundaries so they are contiguous from start to end with no gaps or overlaps.",
        message_template="Target sections must be contiguous with no gaps or overlaps.",
    ),
    "trivial_method_requires_equal_chord_voice_part_count": LintRuleSpec(
        code="trivial_method_requires_equal_chord_voice_part_count",
        name="Trivial Split Requires Matching Chord Density",
        severity="P1",
        domain="STRUCTURAL",
        definition="The trivial split method is only valid when the target lane count matches the maximum simultaneous note count in the source section.",
        fail_condition="A section uses method=trivial for SPLIT_CHORDS_SELECT_NOTES, but the local source chord density does not match the target lane count.",
        suggestion="Use method=ranked with an explicit rank_index, or revise the section split so the target lane count matches the source section's maximum simultaneous note count.",
        message_template="Trivial chord splitting requires the target lane count to match the maximum simultaneous note count in the source section.",
    ),
    "cross_staff_melody_source_when_local_available": LintRuleSpec(
        code="cross_staff_melody_source_when_local_available",
        name="Cross-Staff Melody Source When Local Material Exists",
        severity="P1",
        domain="STRUCTURAL",
        definition="Melody sourcing should stay local to the target part when the target part already has sung material in the section.",
        fail_condition="A derive section pulls melody from another part even though the target part has local sung material in that range.",
        suggestion="Use a same-part melody source for that section unless the user explicitly asked for cross-part sourcing.",
        message_template="Selected melody source crosses parts even though local sung material exists in the target part.",
    ),
    "cross_staff_lyric_source_with_stronger_local_alternative": LintRuleSpec(
        code="cross_staff_lyric_source_with_stronger_local_alternative",
        name="Cross-Staff Lyric Source With Stronger Local Alternative",
        severity="P1",
        domain="LYRIC",
        definition="A cross-staff lyric source is weak when a same-part local lyric source can cover the target lane materially better in the same section.",
        fail_condition="The selected cross-staff lyric source has weaker target-note word coverage than a same-part local source by the configured stricter cross-staff thresholds.",
        suggestion="Switch lyric_source to the suggested same-part local source with materially stronger target-note word coverage.",
        message_template="Selected lyric source crosses parts, but a same-part local source can cover the target lane materially better in this section.",
    ),
    "cross_staff_weak_lyric_source_with_better_alternative": LintRuleSpec(
        code="cross_staff_weak_lyric_source_with_better_alternative",
        name="Cross-Staff Weak Lyric Source With Better Donor Alternative",
        severity="P1",
        domain="LYRIC",
        definition="When a lyric source is taken from another part, the selected donor lane must not be materially weaker than another donor lane in that same source part for the same section.",
        fail_condition="The selected cross-staff donor lane has materially weaker target-note word coverage than another donor lane in the same source part by the configured thresholds.",
        suggestion="Switch lyric_source to the suggested donor voice part in the same cross-staff source part with materially stronger target-note word coverage.",
        message_template="Selected cross-staff lyric source is weak for this section; another donor lane in the same source part has materially better target-note word coverage.",
    ),
    "extension_only_lyric_source_with_word_alternative": LintRuleSpec(
        code="extension_only_lyric_source_with_word_alternative",
        name="Extension-Only Lyric Source With Better Alternative",
        severity="P1",
        domain="LYRIC",
        definition="A lyric source with only extension lyrics is invalid when another same-part source has real word lyrics in the same section.",
        fail_condition="The selected same-part lyric source has zero word lyrics and one or more extension lyrics, while another same-part source has word lyrics.",
        suggestion="Switch lyric_source to the suggested same-part source with real word lyrics.",
        message_template="Selected lyric source has only extension lyrics in this section, while another same-part source has real word lyrics.",
    ),
    "empty_lyric_source_with_word_alternative": LintRuleSpec(
        code="empty_lyric_source_with_word_alternative",
        name="Empty Lyric Source With Better Alternative",
        severity="P1",
        domain="LYRIC",
        definition="A lyric source with no lyrics is invalid when another same-part source has real word lyrics in the same section.",
        fail_condition="The selected same-part lyric source has zero lyric notes, while another same-part source has word lyrics.",
        suggestion="Switch lyric_source to the suggested same-part source with real word lyrics.",
        message_template="Selected lyric source has no lyrics in this section, while another same-part source has real word lyrics.",
    ),
    "weak_lyric_source_with_better_alternative": LintRuleSpec(
        code="weak_lyric_source_with_better_alternative",
        name="Weak Lyric Source With Better Alternative",
        severity="P1",
        domain="LYRIC",
        definition="A same-part lyric source is weak when its target-note word coverage is materially worse than another same-part alternative in the same section.",
        fail_condition="The selected same-part lyric source has low target-note word coverage and another same-part source exceeds the configured word-count and coverage deltas.",
        suggestion="Switch lyric_source to the suggested same-part source with materially better target-note word coverage.",
        message_template="Selected lyric source has weak target-note word coverage in this section, while another same-part source has materially better target-note word coverage.",
    ),
    "lyric_source_without_target_notes": LintRuleSpec(
        code="lyric_source_without_target_notes",
        name="Lyric Source Without Target Notes",
        severity="P0",
        domain="STRUCTURAL",
        definition="Lyric-only propagation is invalid when the target lane has no native sung notes in the section.",
        fail_condition="A section specifies lyric_source without melody_source, and the target lane has no native sung notes in that range.",
        suggestion="Add melody_source for the section, choose a different target lane, or make the section rest if it should be silent.",
        message_template="Section uses lyric_source without melody_source, but target lane has no native sung notes in this range.",
    ),
    "no_rest_when_target_has_native_notes": LintRuleSpec(
        code="no_rest_when_target_has_native_notes",
        name="Rest Not Allowed Over Native Notes",
        severity="P0",
        domain="STRUCTURAL",
        definition="A target section cannot be set to rest when the target lane already has native sung notes in that measure range.",
        fail_condition="A rest section overlaps measures where the target lane has native notes.",
        suggestion="Change the overlapping section to derive mode or split the section so only truly silent measures use rest.",
        message_template="Rest mode overlaps measures where the target lane already has native sung notes.",
    ),
    "same_clef_claim_coverage": LintRuleSpec(
        code="same_clef_claim_coverage",
        name="Same-Part Claim Coverage",
        severity="P0",
        domain="STRUCTURAL",
        definition="For a same-part multi-target plan, every sung measure in the part must be claimed by at least one visible derived target that will appear in the exported reviewable score. A measure is not claimed if all visible sibling targets are `rest` for that measure, even if a hidden/helper/default lane covers it.",
        fail_condition="One or more sung measures in the part are unclaimed by all visible derive sections because the visible sibling targets leave that measure as `rest`, or the measure is covered only by hidden/helper/default lanes.",
        suggestion="Revise one or more visible sibling target sections so the failing measure range is carried by exported derived output rather than left as `rest` across all visible targets or delegated only to hidden/helper/default lanes.",
        message_template="One or more sung measures in the part are not claimed by the visible target timeline coverage.",
    ),
    "same_part_chord_source_underclaimed_by_visible_targets": LintRuleSpec(
        code="same_part_chord_source_underclaimed_by_visible_targets",
        name="Visible Targets Under-Claim Chordal Same-Part Source",
        severity="P1",
        domain="STRUCTURAL",
        definition="For a same-part chordal source range, visible derived targets must collectively claim enough target lanes to cover the source measure's maximum simultaneous note count. If fewer visible targets claim that source measure than the source measure's maximum simultaneous note count, note drop is guaranteed for that measure.",
        fail_condition="One or more measures in a same-part source voice part have chordal material with max_simultaneous_notes > 1, but the number of visible sibling targets claiming that source voice part as melody_source in those measures is smaller than the source measure's maximum simultaneous note count.",
        suggestion="Revise the visible sibling target plan so enough visible targets claim the chordal source range. If the source range is chordal, use split-aware derive planning across visible targets instead of leaving the range under-claimed.",
        message_template="Visible targets under-claim this chordal source range; the number of visible claimers is smaller than the source measure's maximum simultaneous note count, so note drop is guaranteed.",
    ),
    "same_part_target_completeness": LintRuleSpec(
        code="same_part_target_completeness",
        name="Same-Part Target Completeness",
        severity="P0",
        domain="STRUCTURAL",
        definition="When one non-default sibling lane in a part is targeted, all non-default sibling lanes in that part must be included.",
        fail_condition="The plan targets only a subset of same-part sibling voice parts, leaving one or more expected sibling targets missing.",
        suggestion="Include all required same-part sibling targets using their canonical voice_part_id values.",
        message_template="The plan is missing one or more required same-part sibling voice-part targets.",
    ),
}


POSTFLIGHT_VALIDATION_SPECS: Dict[str, LintRuleSpec] = {
    "structural_validation_failed": LintRuleSpec(
        code="structural_validation_failed",
        name="Structural Validation Failed",
        severity="P0",
        domain="STRUCTURAL",
        definition="The derived output must be synthesis-safe: monophonic and non-overlapping.",
        fail_condition="The transformed target lane contains simultaneous sung notes or overlapping note intervals.",
        suggestion="Narrow the failing ranges and revise the split or extraction method so the output becomes monophonic and non-overlapping.",
        message_template="Derived output is not synthesis-safe because the target lane is not monophonic or contains overlapping notes.",
    ),
    "validation_failed_needs_review": LintRuleSpec(
        code="validation_failed_needs_review",
        name="Lyric Coverage Needs Review",
        severity="P1",
        domain="LYRIC",
        definition="Lyric propagation must meet the minimum overall lyric coverage threshold to be accepted automatically.",
        fail_condition="The transformed lane still has missing sung-note lyrics and overall lyric coverage remains below the auto-accept threshold.",
        suggestion="Adjust lyric source, lyric strategy, or sectional boundaries for the failing ranges to improve target-note coverage.",
        message_template="Lyric propagation did not meet minimum coverage for automatic acceptance.",
    ),
    "word_lyric_coverage_too_low": LintRuleSpec(
        code="word_lyric_coverage_too_low",
        name="Word Lyric Coverage Too Low",
        severity="P1",
        domain="LYRIC",
        definition="Derived output should contain enough real word lyrics, not mostly extension-only lyrics.",
        fail_condition="Word-lyric coverage falls below the configured minimum ratio even though the source has available word lyrics.",
        suggestion="Use a stronger lyric source or split the section so word-bearing source material maps more effectively to the target notes.",
        message_template="Word-lyric coverage is too low; the output is dominated by extension-only lyrics.",
    ),
    "partial_lyric_coverage": LintRuleSpec(
        code="partial_lyric_coverage",
        name="Partial Lyric Coverage",
        severity="P2",
        domain="LYRIC",
        definition="Minor lyric gaps can be accepted with warning when overall lyric coverage remains high.",
        fail_condition="Some sung notes remain without lyrics, but overall lyric coverage stays above the warning acceptance threshold.",
        suggestion="Optionally refine the failing ranges, but this output is acceptable for review.",
        message_template="Minor lyric gaps remain, but overall lyric coverage is still acceptable with warning.",
    ),
    "low_word_lyric_coverage": LintRuleSpec(
        code="low_word_lyric_coverage",
        name="Low Word Lyric Coverage",
        severity="P2",
        domain="LYRIC",
        definition="Word-lyric coverage is somewhat low but still above the warning floor.",
        fail_condition="Word-lyric coverage is below the preferred minimum ratio but remains above the configured warning floor.",
        suggestion="Prefer a stronger word-bearing lyric source for the weak ranges if a better candidate exists.",
        message_template="Word-lyric coverage is somewhat low, but still acceptable with warning.",
    ),
}


def get_lint_rule_spec(code: str) -> LintRuleSpec:
    return LINT_RULE_SPECS[code]


def get_postflight_validation_spec(code: str) -> LintRuleSpec:
    return POSTFLIGHT_VALIDATION_SPECS[code]


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


def render_postflight_validation_rules_for_prompt() -> str:
    lines = [
        "SVS Postflight Validation Rules (Canonical Runtime Validation)",
        "",
        "These rules apply after deterministic preprocessing executes.",
        "If a postflight validation result references one of these rule codes, repair the failing ranges without introducing any new P0 structural issue.",
    ]
    for spec in POSTFLIGHT_VALIDATION_SPECS.values():
        lines.extend(
            [
                "",
                f"- Rule code: {spec.code}",
                f"  Name: {spec.name}",
                f"  Severity: {spec.severity}",
                f"  Domain: {spec.domain}",
                f"  Definition: {spec.definition}",
                f"  Fails when: {spec.fail_condition}",
                f"  Suggested fix: {spec.suggestion}",
            ]
        )
    return "\n".join(lines)
