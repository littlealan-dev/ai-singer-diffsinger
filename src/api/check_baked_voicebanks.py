"""Fail an image build when an enabled voicebank is not baked into it.

Run from a Dockerfile after the voicebanks and the code are copied in:

    python3 -m src.api.check_baked_voicebanks <manifest path> <baked dir>

It applies the same rule the worker applies when it resolves a voicebank, so
an image that passes never hits voicebank_not_baked for an enabled voicebank.
"""

from __future__ import annotations

import sys
from pathlib import Path

from src.api.voicebank_cache import missing_baked_voicebanks


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 -m src.api.check_baked_voicebanks <manifest path> <baked dir>", file=sys.stderr)
        return 2
    manifest_path, baked_root = Path(argv[0]), Path(argv[1])
    missing = missing_baked_voicebanks(manifest_path, baked_root)
    if missing:
        for voicebank_id in missing:
            print(f"voicebank not baked: {voicebank_id} (expected under {baked_root / voicebank_id})", file=sys.stderr)
        return 1
    print(f"baked voicebanks ok: {', '.join(sorted(p.name for p in baked_root.iterdir() if p.is_dir()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
