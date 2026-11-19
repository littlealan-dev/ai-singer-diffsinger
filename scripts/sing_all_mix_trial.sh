#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: scripts/sing_all_mix_trial.sh <folder> [trial arguments...]" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
folder="$(cd "$1" && pwd)"
shift
soundfont="$repo_root/ui/public/soundfonts/FluidR3_GM.sf2"
if [[ ! -f "$soundfont" ]]; then
  echo "Soundfont missing: $soundfont" >&2
  exit 2
fi

image="sightsinger-sing-all-package:local"
if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker build --build-context "trial-scripts=$repo_root/scripts" \
    -f "$repo_root/Dockerfile.sing-all-package" -t "$image" "$repo_root"
fi
docker run --rm \
  -v "$folder:/work" \
  -v "$soundfont:/soundfont/FluidR3_GM.sf2:ro" \
  "$image" python -m scripts.sing_all_mix_trial /work \
  --soundfont /soundfont/FluidR3_GM.sf2 "$@"
