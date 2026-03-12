# Design Doc: Bounded 3-Attempt SATB Split Planning + Validation + Domain-Priority Repair

## Objective

Implement a bounded planning and validation loop (max 3 attempts) for
splitting compact MusicXML into synthesis-safe monophonic target lanes.

Goals:

1.  Always attempt to resolve all detected issues.
2.  Never introduce new P0 (critical) issues.
3.  Prioritize STRUCTURAL issues over LYRIC issues when trade-offs
    conflict.
4.  Return the best valid result after 3 attempts.
5.  If no attempt passes structural P0 post-check, return an error (no
    fallback mode).

------------------------------------------------------------------------

# 1. Rule Classification

Each rule has:

-   Severity: P0 / P1 / P2
-   Domain: STRUCTURAL / LYRIC

## 1.1 Preflight Lint Rules

  --------------------------------------------------------------------------------------------------------------
  Rule Code                                                  Severity            Domain          Reason
  ---------------------------------------------------------- ------------------- --------------- ---------------
  section_timeline_contiguous_no_gaps                        P0                  STRUCTURAL      Prevent
                                                                                                 timeline
                                                                                                 gaps/overlaps

  same_clef_claim_coverage                                   P0                  STRUCTURAL      Ensures note
                                                                                                 completeness

  no_rest_when_target_has_native_notes                       P0                  STRUCTURAL      Prevent
                                                                                                 dropping native
                                                                                                 notes

  lyric_source_without_target_notes                          P0                  STRUCTURAL      Lyrics cannot
                                                                                                 exist without
                                                                                                 notes

  same_part_target_completeness                              P0 (or P1 if        STRUCTURAL      Required
                                                             relaxed)                            sibling lane
                                                                                                 completeness

  plan_requires_sections                                     P1                  STRUCTURAL      Complex regions
                                                                                                 require
                                                                                                 segmentation

  mixed_region_requires_sections                             P1                  STRUCTURAL      Prevent
                                                                                                 ambiguous
                                                                                                 region handling

  trivial_method_requires_equal_chord_voice_part_count       P1                  STRUCTURAL      Prevent invalid
                                                                                                 chord splitting

  cross_staff_melody_source_when_local_available             P1                  STRUCTURAL      Affects melody
                                                                                                 stability

  cross_staff_lyric_source_with_stronger_local_alternative   P1                  LYRIC           Suboptimal
                                                                                                 lyric source

  extension_only_lyric_source_with_word_alternative          P1                  LYRIC           Prefer real
                                                                                                 word lyrics

  empty_lyric_source_with_word_alternative                   P1                  LYRIC           Avoid empty
                                                                                                 lyric sources

  weak_lyric_source_with_better_alternative                  P1                  LYRIC           Improve lyric
                                                                                                 coverage
  --------------------------------------------------------------------------------------------------------------

## 1.2 Postflight Validation Rules

  ---------------------------------------------------------------------------------
  Validation                       Severity           Domain         Reason
  -------------------------------- ------------------ -------------- --------------
  structural_validation_failed     P0                 STRUCTURAL     Not monophonic
                                                                     / overlapping
                                                                     notes

  validation_failed_needs_review   P1                 LYRIC          Coverage below
                                                                     0.90 with
                                                                     missing lyrics

  word_lyric_coverage_too_low      P1                 LYRIC          Insufficient
                                                                     real word
                                                                     lyrics

  partial_lyric_coverage           P2                 LYRIC          Minor lyric
                                                                     gaps

  low_word_lyric_coverage          P2                 LYRIC          Slightly low
                                                                     word ratio
  ---------------------------------------------------------------------------------

------------------------------------------------------------------------

# 2. Domain Priority Rule (Repair Logic)

Repair objectives:

1.  Attempt to resolve all issues.

2.  Do not introduce new P0 issues.

3.  If resolving one issue causes another:

    -   Prefer lower severity.
    -   If severities equal, prefer STRUCTURAL over LYRIC.

Structural includes:

-   Synthesis safety
-   Note completeness (no dropped sung measures)
-   Timeline integrity
-   Melody stability

Product principle:

A structurally complete part with weaker lyrics is preferable to a part
with missing notes.

------------------------------------------------------------------------

# 3. Three-Attempt Loop

Max attempts: 3

Each attempt:

1.  Generate plan (LLM).
2.  Run preflight.
3.  If preflight has P0 → attempt fails (no execution).
4.  Execute deterministic split.
5.  Run postflight.
6.  If structural_validation_failed → candidate invalid.
7.  Otherwise candidate valid → classify quality.

------------------------------------------------------------------------

# 4. Candidate Quality Classification

Only valid candidates (postflight P0 passed) are eligible.

Quality Classes:

Class 3: - No P1 or P2 warnings.

Class 2: - No P1 warnings, but has P2.

Class 1: - Has P1 warnings.

## Selection Priority (Measure-Impact Based)

Selection is based on **how much of the score is impacted**, not on the number of issues triggered.

For each candidate, compute:

- `structural_p1_impacted_measures`  
  → Total number of **distinct (union)** measures impacted by STRUCTURAL P1 issues.

- `lyric_p1_impacted_measures`  
  → Total number of **distinct (union)** measures impacted by LYRIC P1 issues.

- `p2_impacted_measures`  
  → Total number of **distinct (union)** measures impacted by P2 issues.

### Important

- Use the **union of measure ranges** per bucket.
- Do **not** double-count overlapping measures.
- If two issues affect overlapping measures, count each measure only once.

#### Example

- Issue A affects measures 6–10  
- Issue B affects measures 8–12  
- Union = {6,7,8,9,10,11,12}  
- Total impacted measures = **7**

---

### Selection Order

Candidates are ranked using the following priority:

1. Higher Quality Class (3 > 2 > 1)
2. Smaller `structural_p1_impacted_measures`
3. Smaller `lyric_p1_impacted_measures`
4. Smaller `p2_impacted_measures`
5. Earliest attempt (final tie-breaker)

---

### Rationale

- The goal is to minimize how much of the score is affected.
- A plan that has more issues but confined to fewer measures is preferable to a plan that affects a larger span.
- Structural domain remains higher priority than Lyric within the same severity.

------------------------------------------------------------------------

# 5. Loop Exit Criteria

Exit early if:

-   Candidate is valid AND Class 3.

After 3 attempts:

-   If best_valid exists → return best_valid.

-   If none valid → return error:

    "Unable to produce synthesis-safe monophonic output after 3
    attempts."

Include diagnostics from best invalid attempt.

------------------------------------------------------------------------

# End of Document
