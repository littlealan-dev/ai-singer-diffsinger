import os


# Default local test behavior: keep v2 syllable alignment/timing enabled
# unless a test/run explicitly overrides them.
os.environ.setdefault("SYLLABLE_ALIGNER_V2", "1")
os.environ.setdefault("SYLLABLE_TIMING_V2", "1")
