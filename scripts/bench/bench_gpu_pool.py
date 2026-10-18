"""Benchmark how many takes one GPU renders at once through the GPU MCP pool.

Not part of the application image: Dockerfile.bench layers this script on the
production image, and a Cloud Run job runs it (scripts/bench/README.md). It
makes the same synthesize calls a confirmed take makes, straight into the
worker's MCP router: no API, LLM, Firestore or credits.

Settings (environment):
  MCP_GPU_WORKERS   GPU processes (render slots), as on the worker
  BENCH_TAKES       takes started together in each round (default: MCP_GPU_WORKERS)
  BENCH_VOICES      comma-separated voicebank ids, cycled over the takes
  BENCH_PARTS       comma-separated part ids, cycled over the takes (default S, A, T, B)
  BENCH_SCORE       MusicXML to sing (default: the Amazing Grace demo, 29.4 s per part)
  BENCH_ROUNDS      rounds (default 2: the first loads voicebanks and models, the next is warm)
  BENCH_SAMPLE_SECONDS  GPU memory sampling interval (default 0.5)

Output: one stdout line "bench_result {json}" with per-round wall time, per-take
times, GPU memory peaks and CPU use, for reading from the job's logs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.backend.config import Settings  # noqa: E402
from src.backend.mcp_client import McpRouter  # noqa: E402

DEFAULT_SCORE = ROOT / "bench" / "amazing-grace.xml"
DEFAULT_PARTS = "Sopran,Alt,Tenor,Bass"
DEFAULT_VOICE = "Diffsinger LIEE Immortal Idol (JubiLIEE 2025)"
_MIB = 1024 * 1024


def _env_list(name: str, default: str) -> List[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


class GpuMemorySampler:
    """Sample GPU memory every few tenths of a second; keep the peaks."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.available = False
        self.peak_used_mb = 0
        self.total_mb = 0
        self.peak_process_mb: Dict[int, int] = {}
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.total_mb = pynvml.nvmlDeviceGetMemoryInfo(self._handle).total // _MIB
            self.available = True
        except Exception:
            self._nvml = None

    def __enter__(self) -> "GpuMemorySampler":
        if self.available:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                used = self._nvml.nvmlDeviceGetMemoryInfo(self._handle).used // _MIB
                self.peak_used_mb = max(self.peak_used_mb, used)
                for process in self._nvml.nvmlDeviceGetComputeRunningProcesses(self._handle):
                    memory = getattr(process, "usedGpuMemory", None)
                    if isinstance(memory, int):
                        pid = int(process.pid)
                        self.peak_process_mb[pid] = max(self.peak_process_mb.get(pid, 0), memory // _MIB)
            except Exception:
                pass
            self._stop.wait(self._interval)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "total_mb": self.total_mb,
            "peak_used_mb": self.peak_used_mb,
            "peak_per_process_mb": sorted(self.peak_process_mb.values(), reverse=True),
        }


def _cpu_times() -> Optional[tuple[int, int]]:
    """Return (busy, total) CPU jiffies from /proc/stat, or None off Linux."""
    try:
        fields = [int(value) for value in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    except Exception:
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields) - idle, sum(fields)


def _takes(score_path: Path, router: McpRouter) -> List[Dict[str, Any]]:
    """Build the synthesize arguments of each take: one parsed score per part."""
    count = int(os.getenv("BENCH_TAKES", os.getenv("MCP_GPU_WORKERS", "1")))
    parts = _env_list("BENCH_PARTS", DEFAULT_PARTS)
    voices = _env_list("BENCH_VOICES", DEFAULT_VOICE)
    summary = router.call_tool("parse_score", {"file_path": str(score_path)})["score_summary"]
    selections = {
        part["part_id"]: part["lyric_selections"][0]
        for part in summary["parts"]
        if part.get("lyric_selections")
    }
    scores: Dict[str, Dict[str, Any]] = {}
    takes = []
    for index in range(count):
        part_id = parts[index % len(parts)]
        selection = {key: selections[part_id][key] for key in ("id", "number", "name")}
        if part_id not in scores:
            scores[part_id] = router.call_tool(
                "parse_score", {"file_path": str(score_path), "lyric_selection": selection}
            )
        takes.append(
            {
                "score": scores[part_id],
                "part_id": part_id,
                "voicebank": voices[index % len(voices)],
                "language": "en",
                "lyric_selection": selection,
                "quote_id": "benchmark",
                "expand_repeats": False,
            }
        )
    return takes


def _render(router: McpRouter, arguments: Dict[str, Any]) -> Dict[str, Any]:
    start = time.monotonic()
    try:
        result = router.call_tool("synthesize", arguments)
        return {
            "part_id": arguments["part_id"],
            "voicebank": arguments["voicebank"],
            "seconds": round(time.monotonic() - start, 3),
            "audio_seconds": round(float(result.get("duration_seconds") or 0.0), 2),
            "ok": True,
        }
    except Exception as exc:
        return {
            "part_id": arguments["part_id"],
            "voicebank": arguments["voicebank"],
            "seconds": round(time.monotonic() - start, 3),
            "ok": False,
            "error": str(exc)[:300],
        }


def main() -> int:
    # The router logs each call with the GPU process that served it.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    score_path = Path(os.getenv("BENCH_SCORE", str(DEFAULT_SCORE)))
    rounds = int(os.getenv("BENCH_ROUNDS", "2"))
    interval = float(os.getenv("BENCH_SAMPLE_SECONDS", "0.5"))
    router = McpRouter(settings)
    started = time.monotonic()
    router.start()
    result: Dict[str, Any] = {
        "gpu_workers": settings.mcp_gpu_workers,
        "max_loaded_voices": os.getenv("SYNTHESIS_MAX_LOADED_VOICES", ""),
        "startup_seconds": round(time.monotonic() - started, 2),
        "cpu_count": os.cpu_count(),
        "rounds": [],
    }
    try:
        takes = _takes(score_path, router)
        result["takes"] = len(takes)
        for number in range(1, rounds + 1):
            cpu_before = _cpu_times()
            with GpuMemorySampler(interval) as sampler:
                round_start = time.monotonic()
                with ThreadPoolExecutor(max_workers=len(takes)) as pool:
                    renders = list(pool.map(lambda arguments: _render(router, arguments), takes))
                wall = time.monotonic() - round_start
            cpu_after = _cpu_times()
            cpu_percent = None
            if cpu_before and cpu_after and cpu_after[1] > cpu_before[1]:
                cpu_percent = round(
                    100.0 * (cpu_after[0] - cpu_before[0]) / (cpu_after[1] - cpu_before[1]), 1
                )
            result["rounds"].append(
                {
                    "round": number,
                    "wall_seconds": round(wall, 3),
                    "takes": renders,
                    "failed": sum(1 for render in renders if not render["ok"]),
                    "cpu_percent": cpu_percent,
                    "gpu_memory": sampler.snapshot(),
                }
            )
    finally:
        router.stop()
    print("bench_result " + json.dumps(result, sort_keys=True), flush=True)
    return 0 if all(round_["failed"] == 0 for round_ in result["rounds"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
