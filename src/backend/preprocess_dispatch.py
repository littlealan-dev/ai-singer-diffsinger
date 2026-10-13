from __future__ import annotations

"""Line-preparation names for the generic job dispatch (src/backend/job_dispatch.py)."""

from typing import Any

from src.backend.config import Settings
from src.backend.job_dispatch import (
    PREPROCESS_JOB_KIND,
    JobDispatchError,
    JobDispatcher,
    job_route,
)

WORKER_TOKEN_HEADER = "X-Preprocess-Worker-Token"

PreprocessDispatchError = JobDispatchError


def preprocess_worker_endpoint(settings: Settings, job_id: str) -> str:
    return job_route(settings, PREPROCESS_JOB_KIND).endpoint(job_id)


class PreprocessDispatcher(JobDispatcher):
    """Start a preprocess job on the worker service."""

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        super().__init__(job_route(settings, PREPROCESS_JOB_KIND), **kwargs)
