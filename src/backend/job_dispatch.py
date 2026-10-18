from __future__ import annotations

"""Hand dispatched jobs (line preparation, synthesis) to the worker that runs them.

A worker runs a job inside its POST {endpoint}/{job_id} request, so the instance
is busy for the job's whole run, and a failed run is retried by whoever
dispatched it:

- "cloud_tasks": a Cloud Tasks queue calls the worker, signing the request as
  the job kind's invoker service account, and retries on any non-2xx.
- "http": this process calls the worker itself and retries the same way. It is
  the local stand-in for Cloud Tasks.

Each job kind has its own route (worker URL, queue, caller identity), read from
its own settings, so kinds can be deployed and switched over independently.
"""

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Optional, Set

import httpx

from src.backend.config import Settings


logger = logging.getLogger(__name__)

CLOUD_TASKS_API = "https://cloudtasks.googleapis.com/v2"
# Cloud Tasks caps an HTTP task's dispatch deadline at 30 minutes.
DISPATCH_DEADLINE_SECONDS = 1800
# Status codes that mean "try again later", as Cloud Tasks treats any non-2xx.
_RETRYABLE_STATUS = {408, 409, 425, 429}

PREPROCESS_JOB_KIND = "preprocess"
SYNTHESIS_JOB_KIND = "synthesis"


class JobDispatchError(RuntimeError):
    """The job could not be handed to the worker."""


@dataclass(frozen=True)
class JobRoute:
    """Where one kind of job is sent and how its worker checks the caller."""

    kind: str
    mode: str
    worker_url: str
    worker_enabled: bool
    worker_token: str
    invoker_service_account: str
    audience: str
    tasks_queue: str
    max_dispatch_attempts: int
    lease_seconds: float
    endpoint_path: str
    token_header: str
    invoker_setting_name: str
    # Which worker of the kind this route reaches: "standard", or "premium"
    # for the second synthesis worker. Task names and log events use the kind.
    variant: str = "standard"

    def endpoint(self, job_id: str) -> str:
        return f"{self.worker_url}{self.endpoint_path}/{job_id}"


def job_route(settings: Settings, kind: str) -> JobRoute:
    """Return the dispatch route for a job kind from its settings."""
    if kind == PREPROCESS_JOB_KIND:
        return JobRoute(
            kind=kind,
            mode=settings.preprocess_dispatch_mode,
            worker_url=settings.preprocess_worker_url,
            worker_enabled=settings.preprocess_worker_enabled,
            worker_token=settings.preprocess_worker_token,
            invoker_service_account=settings.preprocess_worker_invoker_service_account,
            audience=settings.preprocess_worker_audience,
            tasks_queue=settings.preprocess_tasks_queue,
            max_dispatch_attempts=settings.preprocess_job_max_dispatch_attempts,
            lease_seconds=settings.preprocess_job_lease_seconds,
            endpoint_path="/internal/preprocess-jobs",
            token_header="X-Preprocess-Worker-Token",
            invoker_setting_name="PREPROCESS_WORKER_INVOKER_SERVICE_ACCOUNT",
        )
    if kind == SYNTHESIS_JOB_KIND:
        return JobRoute(
            kind=kind,
            mode=settings.synthesis_dispatch_mode,
            worker_url=settings.synthesis_worker_url,
            worker_enabled=settings.synthesis_worker_enabled,
            worker_token=settings.synthesis_worker_token,
            invoker_service_account=settings.synthesis_worker_invoker_service_account,
            audience=settings.synthesis_worker_audience,
            tasks_queue=settings.synthesis_tasks_queue,
            max_dispatch_attempts=settings.synthesis_job_max_dispatch_attempts,
            lease_seconds=settings.synthesis_job_lease_seconds,
            endpoint_path="/internal/synthesis-jobs",
            token_header="X-Synthesis-Worker-Token",
            invoker_setting_name="SYNTHESIS_WORKER_INVOKER_SERVICE_ACCOUNT",
        )
    raise ValueError(f"Unknown job kind: {kind}")


def premium_synthesis_route(settings: Settings) -> Optional[JobRoute]:
    """Return the route to the premium synthesis worker, or None when routing is off.

    It is the synthesis route with the premium worker's URL, audience and
    queue; the caller identity, attempts and lease are shared.
    """
    if settings.synthesis_premium_routing == "off":
        return None
    return replace(
        job_route(settings, SYNTHESIS_JOB_KIND),
        worker_url=settings.synthesis_premium_worker_url,
        audience=settings.synthesis_premium_worker_audience,
        tasks_queue=settings.synthesis_premium_tasks_queue,
        variant="premium",
    )


class JobDispatcher:
    """Start a job of one kind on its worker service."""

    def __init__(
        self,
        route: JobRoute,
        *,
        on_give_up: Optional[Callable[[str], Awaitable[None]]] = None,
        retry_backoff_seconds: Optional[list[float]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._route = route
        self._on_give_up = on_give_up
        self._retry_backoff_seconds = (
            [2.0, 5.0, 10.0, 20.0, 30.0]
            if retry_backoff_seconds is None
            else retry_backoff_seconds
        )
        self._transport = transport
        self._http_tasks: Set[asyncio.Task[None]] = set()

    @property
    def route(self) -> JobRoute:
        return self._route

    @property
    def mode(self) -> str:
        return self._route.mode

    async def dispatch(self, job_id: str) -> None:
        """Hand the job over; raises JobDispatchError if that fails."""
        if self.mode == "cloud_tasks":
            await asyncio.to_thread(self._create_cloud_task, job_id)
        elif self.mode == "http":
            task = asyncio.create_task(
                self._post_until_done(job_id),
                name=f"{self._route.kind}-dispatch-{job_id}",
            )
            self._http_tasks.add(task)
            task.add_done_callback(self._http_tasks.discard)
        else:
            raise JobDispatchError(f"Dispatch mode {self.mode} has no worker.")

    async def _post_until_done(self, job_id: str) -> None:
        """Call the worker until it answers 2xx, like a Cloud Tasks queue would."""
        kind = self._route.kind
        url = self._route.endpoint(job_id)
        headers = {"Content-Type": "application/json"}
        if self._route.worker_token:
            headers[self._route.token_header] = self._route.worker_token
        timeout = httpx.Timeout(DISPATCH_DEADLINE_SECONDS, connect=10.0)
        attempts = len(self._retry_backoff_seconds) + 1
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = await client.post(url, headers=headers, content=b"{}")
                except httpx.HTTPError as exc:
                    logger.warning(
                        "%s_dispatch_http_error job_id=%s attempt=%s error=%s",
                        kind,
                        job_id,
                        attempt,
                        exc,
                    )
                else:
                    if response.is_success:
                        logger.info(
                            "%s_dispatch_done job_id=%s attempt=%s status=%s",
                            kind,
                            job_id,
                            attempt,
                            response.status_code,
                        )
                        return
                    if (
                        response.status_code < 500
                        and response.status_code not in _RETRYABLE_STATUS
                    ):
                        logger.error(
                            "%s_dispatch_rejected job_id=%s status=%s body=%s",
                            kind,
                            job_id,
                            response.status_code,
                            response.text[:300],
                        )
                        break
                    logger.warning(
                        "%s_dispatch_retry job_id=%s attempt=%s status=%s",
                        kind,
                        job_id,
                        attempt,
                        response.status_code,
                    )
                if attempt < attempts:
                    await asyncio.sleep(self._retry_backoff_seconds[attempt - 1])
        logger.error("%s_dispatch_gave_up job_id=%s", kind, job_id)
        if self._on_give_up is not None:
            await self._on_give_up(job_id)

    def _create_cloud_task(self, job_id: str) -> None:
        """Enqueue one HTTP task per job; the task name makes a repeat a no-op."""
        kind = self._route.kind
        queue = self._route.tasks_queue
        service_account = self._route.invoker_service_account
        if not service_account:
            raise JobDispatchError(
                f"{self._route.invoker_setting_name} is required for Cloud Tasks."
            )
        task = {
            "name": f"{queue}/tasks/{kind}-{job_id}",
            "dispatchDeadline": f"{DISPATCH_DEADLINE_SECONDS}s",
            "httpRequest": {
                "httpMethod": "POST",
                "url": self._route.endpoint(job_id),
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(b"{}").decode("ascii"),
                "oidcToken": {
                    "serviceAccountEmail": service_account,
                    "audience": self._route.audience,
                },
            },
        }
        try:
            response = httpx.post(
                f"{CLOUD_TASKS_API}/{queue}/tasks",
                headers={"Authorization": f"Bearer {_google_access_token()}"},
                json={"task": task},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise JobDispatchError(f"Cloud Tasks request failed: {exc}") from exc
        if response.status_code == 409:
            logger.info("%s_task_exists job_id=%s", kind, job_id)
            return
        if not response.is_success:
            raise JobDispatchError(
                f"Cloud Tasks returned {response.status_code}: {response.text[:300]}"
            )
        logger.info(
            "%s_task_created job_id=%s task=%s",
            kind,
            job_id,
            json.loads(response.text or "{}").get("name"),
        )


def _google_access_token() -> str:
    """Return an OAuth access token for this service's own credentials."""
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(Request())
    return str(credentials.token)
