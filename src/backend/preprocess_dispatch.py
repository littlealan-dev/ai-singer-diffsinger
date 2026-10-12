from __future__ import annotations

"""Hand line-preparation (preprocess) jobs to the worker that runs them.

The worker runs a job inside its POST /internal/preprocess-jobs/{job_id}
request, so the instance is busy for the job's whole run, and a failed run is
retried by whoever dispatched it:

- "cloud_tasks": a Cloud Tasks queue calls the worker, signing the request as
  PREPROCESS_WORKER_INVOKER_SERVICE_ACCOUNT, and retries on any non-2xx.
- "http": this process calls the worker itself and retries the same way. It is
  the local stand-in for Cloud Tasks.
"""

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable, Optional, Set

import httpx

from src.backend.config import Settings


logger = logging.getLogger(__name__)

CLOUD_TASKS_API = "https://cloudtasks.googleapis.com/v2"
# Cloud Tasks caps an HTTP task's dispatch deadline at 30 minutes.
DISPATCH_DEADLINE_SECONDS = 1800
WORKER_TOKEN_HEADER = "X-Preprocess-Worker-Token"
# Status codes that mean "try again later", as Cloud Tasks treats any non-2xx.
_RETRYABLE_STATUS = {408, 409, 425, 429}


class PreprocessDispatchError(RuntimeError):
    """The job could not be handed to the worker."""


def preprocess_worker_endpoint(settings: Settings, job_id: str) -> str:
    return f"{settings.preprocess_worker_url}/internal/preprocess-jobs/{job_id}"


class PreprocessDispatcher:
    """Start a preprocess job on the worker service."""

    def __init__(
        self,
        settings: Settings,
        *,
        on_give_up: Optional[Callable[[str], Awaitable[None]]] = None,
        retry_backoff_seconds: Optional[list[float]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._settings = settings
        self._on_give_up = on_give_up
        self._retry_backoff_seconds = (
            [2.0, 5.0, 10.0, 20.0, 30.0]
            if retry_backoff_seconds is None
            else retry_backoff_seconds
        )
        self._transport = transport
        self._http_tasks: Set[asyncio.Task[None]] = set()

    @property
    def mode(self) -> str:
        return self._settings.preprocess_dispatch_mode

    async def dispatch(self, job_id: str) -> None:
        """Hand the job over; raises PreprocessDispatchError if that fails."""
        if self.mode == "cloud_tasks":
            await asyncio.to_thread(self._create_cloud_task, job_id)
        elif self.mode == "http":
            task = asyncio.create_task(
                self._post_until_done(job_id), name=f"preprocess-dispatch-{job_id}"
            )
            self._http_tasks.add(task)
            task.add_done_callback(self._http_tasks.discard)
        else:
            raise PreprocessDispatchError(f"Dispatch mode {self.mode} has no worker.")

    async def _post_until_done(self, job_id: str) -> None:
        """Call the worker until it answers 2xx, like a Cloud Tasks queue would."""
        url = preprocess_worker_endpoint(self._settings, job_id)
        headers = {"Content-Type": "application/json"}
        if self._settings.preprocess_worker_token:
            headers[WORKER_TOKEN_HEADER] = self._settings.preprocess_worker_token
        timeout = httpx.Timeout(DISPATCH_DEADLINE_SECONDS, connect=10.0)
        attempts = len(self._retry_backoff_seconds) + 1
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = await client.post(url, headers=headers, content=b"{}")
                except httpx.HTTPError as exc:
                    logger.warning(
                        "preprocess_dispatch_http_error job_id=%s attempt=%s error=%s",
                        job_id,
                        attempt,
                        exc,
                    )
                else:
                    if response.is_success:
                        logger.info(
                            "preprocess_dispatch_done job_id=%s attempt=%s status=%s",
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
                            "preprocess_dispatch_rejected job_id=%s status=%s body=%s",
                            job_id,
                            response.status_code,
                            response.text[:300],
                        )
                        break
                    logger.warning(
                        "preprocess_dispatch_retry job_id=%s attempt=%s status=%s",
                        job_id,
                        attempt,
                        response.status_code,
                    )
                if attempt < attempts:
                    await asyncio.sleep(self._retry_backoff_seconds[attempt - 1])
        logger.error("preprocess_dispatch_gave_up job_id=%s", job_id)
        if self._on_give_up is not None:
            await self._on_give_up(job_id)

    def _create_cloud_task(self, job_id: str) -> None:
        """Enqueue one HTTP task per job; the task name makes a repeat a no-op."""
        queue = self._settings.preprocess_tasks_queue
        service_account = self._settings.preprocess_worker_invoker_service_account
        if not service_account:
            raise PreprocessDispatchError(
                "PREPROCESS_WORKER_INVOKER_SERVICE_ACCOUNT is required for Cloud Tasks."
            )
        task = {
            "name": f"{queue}/tasks/preprocess-{job_id}",
            "dispatchDeadline": f"{DISPATCH_DEADLINE_SECONDS}s",
            "httpRequest": {
                "httpMethod": "POST",
                "url": preprocess_worker_endpoint(self._settings, job_id),
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(b"{}").decode("ascii"),
                "oidcToken": {
                    "serviceAccountEmail": service_account,
                    "audience": self._settings.preprocess_worker_audience,
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
            raise PreprocessDispatchError(f"Cloud Tasks request failed: {exc}") from exc
        if response.status_code == 409:
            logger.info("preprocess_task_exists job_id=%s", job_id)
            return
        if not response.is_success:
            raise PreprocessDispatchError(
                f"Cloud Tasks returned {response.status_code}: {response.text[:300]}"
            )
        logger.info(
            "preprocess_task_created job_id=%s task=%s",
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
