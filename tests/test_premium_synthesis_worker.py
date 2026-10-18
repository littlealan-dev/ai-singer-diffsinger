"""The premium (RTX) synthesis worker: GPU process pool, routing, settings, GPU memory log."""

import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.backend.config import Settings
from src.backend.job_dispatch import SYNTHESIS_JOB_KIND, job_route, premium_synthesis_route
from src.backend.mcp_client import McpRouter
from src.backend.premium_routing import PremiumSynthesisRouting


class _Process:
    """A started MCP process stand-in that records the calls it serves."""

    def __init__(self, name: str, *, call_seconds: float = 0.0) -> None:
        self._name = name
        self.started = False
        self.calls: list[str] = []
        self._call_seconds = call_seconds

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def is_ready(self) -> bool:
        return self.started

    def call_tool(self, name, arguments):
        self.calls.append(name)
        time.sleep(self._call_seconds)
        return {"ok": True}


def _router(monkeypatch, gpu_workers: int, *, call_seconds: float = 0.0) -> McpRouter:
    monkeypatch.setenv("MCP_GPU_WORKERS", str(gpu_workers))
    router = McpRouter(Settings.from_env())
    router._cpu_pool = [_Process("mcp_cpu")]
    router._cpu_in_flight = [0]
    router._gpu_pool = [
        _Process("mcp_gpu" if index == 0 else f"mcp_gpu_{index + 1}", call_seconds=call_seconds)
        for index in range(gpu_workers)
    ]
    router._gpu_in_flight = [0] * gpu_workers
    return router


def test_gpu_workers_default_to_one_process(monkeypatch):
    monkeypatch.delenv("MCP_GPU_WORKERS", raising=False)
    settings = Settings.from_env()
    router = McpRouter(settings)

    assert settings.mcp_gpu_workers == 1
    assert [label for label, _ in router._processes()][-1:] == ["gpu"]
    assert len(router._gpu_pool) == 1


def test_gpu_workers_must_be_at_least_one(monkeypatch):
    monkeypatch.setenv("MCP_GPU_WORKERS", "0")
    with pytest.raises(ValueError, match="MCP_GPU_WORKERS"):
        Settings.from_env()


def test_gpu_pool_names_its_processes(monkeypatch):
    monkeypatch.setenv("MCP_GPU_WORKERS", "3")
    router = McpRouter(Settings.from_env())

    gpu_labels = [label for label, _ in router._processes() if label.startswith("gpu")]
    assert gpu_labels == ["gpu", "gpu_2", "gpu_3"]
    assert [process._name for process in router._gpu_pool] == ["mcp_gpu", "mcp_gpu_2", "mcp_gpu_3"]


def test_concurrent_gpu_calls_go_to_different_processes(monkeypatch):
    router = _router(monkeypatch, 2, call_seconds=0.1)
    threads = [
        threading.Thread(target=router._call_with_retry, args=("gpu", "synthesize", {}))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [process.calls for process in router._gpu_pool] == [["synthesize"], ["synthesize"]]
    assert router._gpu_in_flight == [0, 0]


def test_gpu_pool_starts_every_process_and_is_ready_only_when_all_are(monkeypatch):
    monkeypatch.setenv("MCP_GPU_ENABLED", "true")
    router = _router(monkeypatch, 2)

    router._start_pool(router._gpu_pool)
    assert all(process.started for process in router._gpu_pool)

    router._gpu_pool[1].started = False
    router._startup_ready.set()
    readiness = router.readiness()
    assert readiness["workers"]["gpu"] is False
    assert readiness["processes"] == {"cpu": 1, "gpu": 2}


def test_premium_route_is_off_unless_routing_is_set(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_PREMIUM_ROUTING", raising=False)
    assert premium_synthesis_route(Settings.from_env()) is None


def test_premium_route_reaches_the_premium_worker_with_the_shared_identity(monkeypatch):
    monkeypatch.setenv("SYNTHESIS_DISPATCH_MODE", "cloud_tasks")
    monkeypatch.setenv("SYNTHESIS_WORKER_URL", "https://standard.example")
    monkeypatch.setenv("SYNTHESIS_TASKS_QUEUE", "projects/p/locations/us-east4/queues/synthesis-jobs")
    monkeypatch.setenv("SYNTHESIS_WORKER_INVOKER_SERVICE_ACCOUNT", "invoker@p.iam.gserviceaccount.com")
    monkeypatch.setenv("SESSION_STORE", "firestore")
    monkeypatch.setenv("SYNTHESIS_PREMIUM_ROUTING", "allowlist")
    monkeypatch.setenv("SYNTHESIS_PREMIUM_WORKER_URL", "https://premium.example/")
    monkeypatch.setenv(
        "SYNTHESIS_PREMIUM_TASKS_QUEUE", "projects/p/locations/us-central1/queues/synthesis-jobs-premium"
    )
    settings = Settings.from_env()

    standard = job_route(settings, SYNTHESIS_JOB_KIND)
    premium = premium_synthesis_route(settings)

    assert premium is not None
    assert premium.kind == SYNTHESIS_JOB_KIND
    assert premium.variant == "premium" and standard.variant == "standard"
    assert premium.worker_url == premium.audience == "https://premium.example"
    assert premium.tasks_queue.endswith("/synthesis-jobs-premium")
    assert premium.endpoint("job-1") == "https://premium.example/internal/synthesis-jobs/job-1"
    assert premium.invoker_service_account == standard.invoker_service_account
    assert premium.max_dispatch_attempts == standard.max_dispatch_attempts


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"SYNTHESIS_PREMIUM_ROUTING": "sometimes"}, "must be one of off, allowlist"),
        ({"SYNTHESIS_PREMIUM_ROUTING": "allowlist"}, "SYNTHESIS_DISPATCH_MODE"),
        (
            {
                "SYNTHESIS_PREMIUM_ROUTING": "allowlist",
                "SYNTHESIS_DISPATCH_MODE": "http",
                "SYNTHESIS_WORKER_URL": "http://127.0.0.1:8003",
            },
            "SYNTHESIS_PREMIUM_WORKER_URL",
        ),
        (
            {
                "SYNTHESIS_PREMIUM_ROUTING": "allowlist",
                "SYNTHESIS_DISPATCH_MODE": "cloud_tasks",
                "SYNTHESIS_WORKER_URL": "https://standard.example",
                "SYNTHESIS_TASKS_QUEUE": "projects/p/locations/l/queues/q",
                "SYNTHESIS_PREMIUM_WORKER_URL": "https://premium.example",
            },
            "SYNTHESIS_PREMIUM_TASKS_QUEUE",
        ),
    ],
)
def test_premium_routing_settings_are_checked(monkeypatch, env, message):
    monkeypatch.setenv("SESSION_STORE", "firestore")
    for name in ("SYNTHESIS_DISPATCH_MODE", "SYNTHESIS_WORKER_URL", "SYNTHESIS_TASKS_QUEUE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings.from_env()


_LISTS = {
    "enabled": False,
    "allowedUids": ["uid-tester", " "],
    "allowedEmails": ["Tester@Example.com"],
}


@pytest.mark.parametrize(
    ("user_id", "email", "expected"),
    [
        ("uid-tester", None, True),
        ("someone", "tester@example.COM", True),
        ("someone", "other@example.com", False),
        ("", "", False),
    ],
)
def test_allowlist_matches_uids_or_emails_whether_or_not_maintenance_is_on(user_id, email, expected):
    routing = PremiumSynthesisRouting("allowlist", load_config=lambda: _LISTS)
    assert routing.is_premium(user_id, email) is expected


def test_routing_off_never_reads_the_lists():
    def must_not_load():
        raise AssertionError("the lists must not be read when routing is off")

    assert PremiumSynthesisRouting("off", load_config=must_not_load).is_premium("uid-tester", None) is False


def test_unreadable_lists_send_the_job_to_the_standard_worker():
    def failing_load():
        raise RuntimeError("firestore unavailable")

    assert PremiumSynthesisRouting("allowlist", load_config=failing_load).is_premium("uid-tester", None) is False


def test_the_lists_are_read_once_a_minute():
    loads = []
    now = [100.0]
    routing = PremiumSynthesisRouting(
        "allowlist", load_config=lambda: loads.append(1) or _LISTS, clock=lambda: now[0]
    )

    routing.is_premium("uid-tester", None)
    now[0] += 59
    routing.is_premium("someone", "tester@example.com")
    assert len(loads) == 1
    now[0] += 1
    routing.is_premium("uid-tester", None)
    assert len(loads) == 2


def test_gpu_memory_fields_are_left_out_without_nvml(monkeypatch):
    import builtins

    from src.api import gpu_memory

    monkeypatch.setattr(gpu_memory, "_nvml", None)
    monkeypatch.setattr(gpu_memory, "_handle", None)
    monkeypatch.setattr(gpu_memory, "_unavailable", False)
    real_import = builtins.__import__

    def no_pynvml(name, *args, **kwargs):
        if name == "pynvml":
            raise ImportError("no pynvml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pynvml)
    assert gpu_memory.gpu_memory_log_fields() == ""


def test_gpu_memory_fields_report_the_device_and_this_process(monkeypatch):
    import os
    import sys

    from src.api import gpu_memory

    mib = 1024 * 1024
    fake = SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlDeviceGetHandleByIndex=lambda index: "gpu0",
        nvmlDeviceGetMemoryInfo=lambda handle: SimpleNamespace(used=3000 * mib, total=97887 * mib),
        nvmlDeviceGetComputeRunningProcesses=lambda handle: [
            SimpleNamespace(pid=1, usedGpuMemory=500 * mib),
            SimpleNamespace(pid=os.getpid(), usedGpuMemory=1200 * mib),
        ],
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    monkeypatch.setattr(gpu_memory, "_nvml", None)
    monkeypatch.setattr(gpu_memory, "_handle", None)
    monkeypatch.setattr(gpu_memory, "_unavailable", False)

    assert gpu_memory.gpu_memory_log_fields() == (
        " gpu_mem_used_mb=3000 gpu_mem_total_mb=97887 gpu_process_mem_mb=1200"
    )
