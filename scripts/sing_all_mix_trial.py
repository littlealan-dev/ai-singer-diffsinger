"""Render and inspect offline Sing All mixes from a folder of local takes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.api.score import parse_score
from src.backend import sing_all_mix as mix
from src.backend.audio_mix import _read_audio
from scripts.sing_all_sync_check import measure_offsets


def _path_in_folder(folder: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else folder / path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder containing the takes and optional MIDI")
    parser.add_argument(
        "--take", nargs=4, action="append", default=[],
        metavar=("PART_ID", "PART_NAME", "MODE", "FILE"),
        help="Repeat for each take; FILE is relative to folder or absolute.",
    )
    parser.add_argument("--midi", help="Instrument MIDI, relative to folder or absolute")
    parser.add_argument("--soundfont", type=Path, help="FluidR3_GM.sf2 (required with --midi)")
    parser.add_argument("--score", help="MusicXML score for the optional sync report")
    parser.add_argument("--repeats", action="store_true")
    parser.add_argument("--drop-track", type=int, action="append", default=[])
    parser.add_argument("--mixes", choices=("all", "full", "none"), default="all")
    parser.add_argument("--output", type=Path, help="Output folder (default: FOLDER/mixes)")
    for flag, default in (
        ("vocal-ref-lufs", mix.VOCAL_REF_LUFS),
        ("full-instruments-db", mix.FULL_INSTRUMENTS_DB),
        ("part-dominant-instruments-db", mix.PART_DOMINANT_INSTRUMENTS_DB),
        ("target-lufs", mix.TARGET_LUFS),
        ("peak-limit-db", mix.PEAK_LIMIT_DB),
        ("max-limiting-db", mix.MAX_LIMITING_DB),
        ("max-vocal-gain-db", mix.MAX_VOCAL_GAIN_DB),
        ("max-instrument-gain-db", mix.MAX_INSTRUMENT_GAIN_DB),
        ("release-tail-seconds", mix.RELEASE_TAIL_SECONDS),
    ):
        parser.add_argument(f"--{flag}", type=float, default=default)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if not folder.is_dir():
        parser.error(f"Folder not found: {folder}")
    if not args.take:
        parser.error("Provide at least one --take PART_ID PART_NAME MODE FILE.")
    if args.midi and not args.soundfont:
        parser.error("--soundfont is required with --midi.")
    if args.score and not args.midi:
        parser.error("--score requires --midi.")

    for flag in (
        "VOCAL_REF_LUFS", "FULL_INSTRUMENTS_DB", "PART_DOMINANT_INSTRUMENTS_DB",
        "TARGET_LUFS", "PEAK_LIMIT_DB", "MAX_LIMITING_DB",
        "MAX_VOCAL_GAIN_DB", "MAX_INSTRUMENT_GAIN_DB", "RELEASE_TAIL_SECONDS",
    ):
        setattr(mix, flag, getattr(args, flag.lower()))

    refs: list[mix.TakeRef] = []
    stems: dict[tuple[str, str], mix.Stem] = {}
    source_paths: dict[tuple[str, str], Path] = {}
    sample_rate: int | None = None
    for part_id, part_name, mode, file_name in args.take:
        if mode not in {"lyrics", "solfege"}:
            parser.error(f"Invalid mode {mode!r}; use lyrics or solfege.")
        path = _path_in_folder(folder, file_name)
        data, rate = _read_audio(path)
        if sample_rate is None:
            sample_rate = rate
        elif rate != sample_rate:
            parser.error(f"Take sample rate mismatch: {path} has {rate}, expected {sample_rate}.")
        ref = mix.TakeRef(part_id, part_name, mode)
        key = (part_id, mode)
        if key in stems:
            parser.error(f"Duplicate take for {part_id} ({mode}).")
        refs.append(ref)
        stems[key] = mix.Stem(f"{part_name} ({mode})", data)
        source_paths[key] = path
    assert sample_rate is not None

    instruments = None
    if args.midi:
        instruments = mix.render_instruments(
            _path_in_folder(folder, args.midi), args.soundfont, sample_rate,
            set(args.drop_track),
        )
    instruments_sound = instruments is not None and bool(
        float(abs(instruments).max(initial=0)) > 1e-6
    )
    output = args.output or folder / "mixes"
    for planned in mix.plan_mixes(refs, args.mixes, instruments_sound):
        selected = [
            stems[(ref.part_id, ref.mode)]
            for ref in refs
            if ref.mode == planned.mode
            and (planned.kind == "full" or ref.part_id == planned.part_id)
        ]
        instrument_db = (
            mix.FULL_INSTRUMENTS_DB if planned.kind == "full"
            else mix.PART_DOMINANT_INSTRUMENTS_DB
        )
        audio, stats = mix.balanced_mix(
            selected, instruments if instruments_sound else None,
            instrument_db, sample_rate,
        )
        path = output / planned.file_name
        mix.write_mp3(audio, sample_rate, path)
        print(json.dumps({"file": str(path), **stats}, ensure_ascii=False))

    if args.score:
        score = parse_score(str(_path_in_folder(folder, args.score)))
        for ref in refs:
            offsets = measure_offsets(
                source_paths[(ref.part_id, ref.mode)],
                _path_in_folder(folder, args.midi),
                score, ref.part_id, args.repeats,
            )
            print(json.dumps({
                "sync": f"{ref.part_name} ({ref.mode})", **offsets,
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
