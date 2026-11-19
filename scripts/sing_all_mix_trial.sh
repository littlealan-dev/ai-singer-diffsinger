#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: scripts/sing_all_mix_trial.sh <folder> [trial arguments...] | --test" >&2
  exit 2
fi

run_test=false
if [[ "$1" == "--test" ]]; then
  run_test=true
  shift
  if [[ $# -ne 0 ]]; then
    echo "--test takes no other arguments" >&2
    exit 2
  fi
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$run_test" == false ]]; then
  folder="$(cd "$1" && pwd)"
  shift
fi
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
if [[ "$run_test" == true ]]; then
  docker run --rm \
    -v "$repo_root/tests:/app/tests:ro" \
    -v "$repo_root/ui/public/soundfonts:/app/ui/public/soundfonts:ro" \
    "$image" sh -c \
    'python -m pip install --no-cache-dir pytest && python -m pytest tests/test_sing_all_mix.py -q -p no:cacheprovider'
  exit
fi
docker run --rm \
  -v "$folder:/work" \
  -v "$repo_root/ui/public/soundfonts:/soundfont:ro" \
  "$image" python -m scripts.sing_all_mix_trial /work \
  --soundfont /soundfont/FluidR3_GM.sf2 "$@"
