# Review: OpenUtau DiffSinger English Phonemizer Parity (HLD & LLD) - Revised

## Overview

This document presents a unified review of the revised High-Level Design (HLD) and Low-Level Design (LLD) for the `OpenUtau DiffSinger English Phonemizer Parity` unit. 

The revised documents directly and comprehensively address all of the clarifications raised in the initial review. The proposed unit creates a clean, safe, and maintainable path specifically for English DiffSinger voicebanks, successfully balancing strict OpenUtau compatibility with the stability of the existing generic phonemizer pipeline.

---

## HLD Review (`openutau-diffsinger-english-phonemizer-parity-hld.md`)

### Strengths
* **Clear Problem Definition:** Accurately identifies the need for porting the supporting stack (dictionary loading, remapping, note distribution), not just the top-level phonemizer class.
* **Well-Defined Boundaries:** The "Integration Contract" and "Non-Goals" keep scope tightly controlled, avoiding full renderer functionality.
* **Safe Rollout Strategy:** Limiting activation strictly to voicebanks that declare this specific phonemizer shields the remainder of the system from regressions.
* **Resolved Clarifications:**
  * **Dictionary Override Policy:** The new section explicitly confirms that Phase 1 relies strictly on voicebank-local dictionaries. This correctly matches the existing architectural capabilities and avoids unnecessary complexity.
  * **Future Extension:** The rollout section now explicitly notes how `OpenUtauDiffSingerBase` is structured to cleanly support an eventual ARPA+ sibling without requiring a rewrite.

---

## LLD Review (`openutau-diffsinger-english-phonemizer-parity-lld.md`)

### Strengths
* **Logical Module Layout:** Breaking the parity logic into `_base`, `_g2p`, and `_english` correctly mirrors the problem space without introducing hard dependencies on OpenUtau binaries.
* **Explicit Integration Points:** Identifying `phonemize.py` and `synthesize.py` as injection points makes the implementation path highly actionable.
* **Robust Testing Plan:** The diagnostic logging plan forms a strong foundation for debugging subtle phonetic alignment issues.
* **Resolved Clarifications:**
  * **G2P Dependency Specification:** The LLD now explicitly mandates the use of the existing `g2p_en` library, addressing concerns about new dependencies and vocabulary differences.
  * **Note Distribution Algorithm:** The document now provides a direct pointer to the source of truth (`DiffSingerBasePhonemizer.cs` and `ProcessWord(...)`) and explicitly mandates transcribing the behavior rather than approximating it. This removes the risk of implementer drift.
  * **Caching / Performance:** The new section correctly mitigates initialization overhead by mandating the caching of the parsed dictionary and G2P objects while avoiding caching mutable per-request state.

---

## Conclusion

The revised HLD and LLD are excellent. They offer a highly detailed, bounded, and pragmatic approach to achieving parity with OpenUtau's English DiffSinger behavior. All previous concerns and clarifications have been explicitly incorporated into the design parameters. The specifications are fully actionable and ready for implementation.
