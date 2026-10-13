from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import src.backend.job_dispatch as dispatch_module
import src.backend.main as main_module
from src.backend.config import Settings
from src.backend.job_dispatch import (
    PREPROCESS_JOB_KIND,
    SYNTHESIS_JOB_KIND,
    JobDispatchError,
    JobDispatcher,
    job_route,
)

SYNTHESIS_QUEUE = "projects/p/locations/us-east4/queues/synthesis-jobs"


def _settings(**overrides) -> Settings:
    base = Settings.from_env()
    values = {
        "synthesis_dispatch_mode": "http",
        "synthesis_worker_url": "http://synthesis-worker.test",
        "synthesis_worker_token": "synthesis-token",
        "preprocess_dispatch_mode": "http",
        "preprocess_worker_url": "http://preprocess-worker.test",
        "preprocess_worker_token": "preprocess-token",
    }
    values.update(overrides)
    return replace(base, **values)


def test_each_kind_has_its_own_route():
    settings = _settings()

    synthesis = job_route(settings, SYNTHESIS_JOB_KIND)
    preprocess = job_route(settings, PREPROCESS_JOB_KIND)

    assert synthesis.endpoint("job-1") == "http://synthesis-worker.test/internal/synthesis-jobs/job-1"
    assert synthesis.token_header == "X-Synthesis-Worker-Token"
    assert preprocess.endpoint("job-1") == "http://preprocess-worker.test/internal/preprocess-jobs/job-1"
    assert preprocess.token_header == "X-Preprocess-Worker-Token"
    with pytest.raises(ValueError):
        job_route(settings, "export")


def test_http_dispatch_of_a_synthesis_job_posts_to_the_synthesis_worker():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    dispatcher = JobDispatcher(
        job_route(_settings(), SYNTHESIS_JOB_KIND),
        retry_backoff_seconds=[0.0],
        transport=httpx.MockTransport(handler),
    )

    async def run():
        await dispatcher.dispatch("job-1")
        await asyncio.gather(*dispatcher._http_tasks)

    asyncio.run(run())

    assert str(seen[0].url) == "http://synthesis-worker.test/internal/synthesis-jobs/job-1"
    assert seen[0].headers["X-Synthesis-Worker-Token"] == "synthesis-token"
    assert "X-Preprocess-Worker-Token" not in seen[0].headers


def test_cloud_tasks_dispatch_of_a_synthesis_job_uses_its_queue_and_invoker(monkeypatch):
    settings = _settings(
        synthesis_dispatch_mode="cloud_tasks",
        synthesis_worker_url="https://synthesis.example.run.app",
        synthesis_worker_audience="https://synthesis.example.run.app",
        synthesis_worker_invoker_service_account="invoker@p.iam.gserviceaccount.com",
        synthesis_tasks_queue=SYNTHESIS_QUEUE,
    )
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json["task"])
        return httpx.Response(200, json={"name": json["task"]["name"]})

    monkeypatch.setattr(dispatch_module, "_google_access_token", lambda: "t")
    monkeypatch.setattr(dispatch_module.httpx, "post", fake_post)

    asyncio.run(JobDispatcher(job_route(settings, SYNTHESIS_JOB_KIND)).dispatch("job-1"))

    task = calls[0]
    assert task["name"] == f"{SYNTHESIS_QUEUE}/tasks/synthesis-job-1"
    assert task["httpRequest"]["url"] == (
        "https://synthesis.example.run.app/internal/synthesis-jobs/job-1"
    )
    assert task["httpRequest"]["oidcToken"]["serviceAccountEmail"] == (
        "invoker@p.iam.gserviceaccount.com"
    )


def test_cloud_tasks_dispatch_names_the_missing_invoker_setting():
    settings = _settings(
        synthesis_dispatch_mode="cloud_tasks",
        synthesis_tasks_queue=SYNTHESIS_QUEUE,
        synthesis_worker_invoker_service_account="",
    )

    with pytest.raises(JobDispatchError, match="SYNTHESIS_WORKER_INVOKER_SERVICE_ACCOUNT"):
        asyncio.run(JobDispatcher(job_route(settings, SYNTHESIS_JOB_KIND)).dispatch("job-1"))


@pytest.mark.parametrize(
    "env, message",
    [
        ({"SYNTHESIS_DISPATCH_MODE": "queue"}, "SYNTHESIS_DISPATCH_MODE must be one of"),
        ({"SYNTHESIS_DISPATCH_MODE": "http"}, "SYNTHESIS_WORKER_URL is required"),
        (
            {"SYNTHESIS_DISPATCH_MODE": "cloud_tasks", "SYNTHESIS_WORKER_URL": "https://w"},
            "SYNTHESIS_TASKS_QUEUE is required",
        ),
        (
            {"SYNTHESIS_WORKER_ENABLED": "true", "SESSION_STORE": "memory"},
            "synthesis worker reads sessions",
        ),
    ],
)
def test_synthesis_dispatch_settings_are_checked(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValueError, match=message):
        Settings.from_env()


def test_synthesis_dispatch_settings_are_read(monkeypatch):
    monkeypatch.setenv("SESSION_STORE", "firestore")
    monkeypatch.setenv("SYNTHESIS_DISPATCH_MODE", "cloud_tasks")
    monkeypatch.setenv("SYNTHESIS_WORKER_URL", "https://synthesis.example.run.app/")
    monkeypatch.setenv("SYNTHESIS_TASKS_QUEUE", SYNTHESIS_QUEUE)
    monkeypatch.setenv("SYNTHESIS_WORKER_INVOKER_SERVICE_ACCOUNT", "Invoker@P.iam.gserviceaccount.com")

    settings = Settings.from_env()

    assert settings.synthesis_dispatch_mode == "cloud_tasks"
    assert settings.synthesis_worker_url == "https://synthesis.example.run.app"
    # The audience defaults to the worker URL.
    assert settings.synthesis_worker_audience == "https://synthesis.example.run.app"
    assert settings.synthesis_worker_invoker_service_account == "invoker@p.iam.gserviceaccount.com"
    assert settings.synthesis_job_max_dispatch_attempts == 3
    assert settings.synthesis_job_lease_seconds == 90.0
    assert settings.synthesis_worker_enabled is False


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, False),
        ({"PREPROCESS_WORKER_ENABLED": "true", "SESSION_STORE": "firestore"}, True),
        ({"MCP_START_ON_FIRST_CALL": "true"}, True),
        (
            {"PREPROCESS_WORKER_ENABLED": "true", "SESSION_STORE": "firestore", "MCP_START_ON_FIRST_CALL": "false"},
            False,
        ),
    ],
)
def test_mcp_start_on_first_call_defaults_to_the_preprocess_worker(monkeypatch, env, expected):
    monkeypatch.delenv("MCP_START_ON_FIRST_CALL", raising=False)
    monkeypatch.delenv("PREPROCESS_WORKER_ENABLED", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert Settings.from_env().mcp_start_on_first_call is expected


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        }
    )


def test_the_caller_check_uses_the_job_kinds_own_token():
    route = job_route(_settings(), SYNTHESIS_JOB_KIND)

    asyncio.run(
        main_module._require_worker_caller(
            _request({"X-Synthesis-Worker-Token": "synthesis-token"}), route
        )
    )
    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            main_module._require_worker_caller(
                # The other kind's token and header do not admit a synthesis caller.
                _request({"X-Preprocess-Worker-Token": "preprocess-token"}), route
            )
        )
    assert refused.value.status_code == 401


def test_the_caller_check_accepts_only_the_kinds_invoker(monkeypatch):
    settings = _settings(
        synthesis_worker_invoker_service_account="invoker@p.iam.gserviceaccount.com",
        synthesis_worker_audience="https://synthesis.example.run.app",
    )
    route = job_route(settings, SYNTHESIS_JOB_KIND)
    audiences = []

    def verify(token, audience):
        audiences.append(audience)
        return {"email": "someone-else@p.iam.gserviceaccount.com", "email_verified": True}

    monkeypatch.setattr(main_module, "_verify_google_oidc_token", verify)

    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            main_module._require_worker_caller(_request({"Authorization": "Bearer t"}), route)
        )

    assert refused.value.status_code == 403
    assert audiences == ["https://synthesis.example.run.app"]
