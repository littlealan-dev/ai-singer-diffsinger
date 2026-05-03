from __future__ import annotations

"""Cloud Monitoring metrics for billing credit refresh runs."""

from datetime import datetime, timezone
import os
from typing import Any

from src.mcp.logging_utils import get_logger

logger = get_logger(__name__)

_METRIC_PREFIX = "custom.googleapis.com/billing/refresh"


def emit_credit_refresh_metrics(result: dict[str, Any], *, duration_ms: int) -> None:
    project_id = _project_id()
    if not project_id:
        logger.warning("billing_refresh_metrics_skipped reason=missing_project_id")
        return
    if os.getenv("FIRESTORE_EMULATOR_HOST"):
        logger.info("billing_refresh_metrics_skipped reason=firestore_emulator")
        return
    try:
        from google.cloud import monitoring_v3
    except ImportError:
        logger.warning("billing_refresh_metrics_skipped reason=missing_google_cloud_monitoring")
        return

    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{project_id}"
    now = datetime.now(timezone.utc)
    labels = {
        "environment": os.getenv("APP_ENV", os.getenv("ENV", "dev")),
        "function_name": "refreshCredits",
    }
    series = [
        _time_series(monitoring_v3, "processed_count", int(result.get("processed", 0)), now, labels),
        _time_series(monitoring_v3, "skipped_reserved_count", int(result.get("skipped_reserved", 0)), now, labels),
        _time_series(monitoring_v3, "failed_count", int(result.get("failed", 0)), now, labels),
        _time_series(monitoring_v3, "scanned_count", int(result.get("scanned", 0)), now, labels),
        _time_series(monitoring_v3, "duration_ms", duration_ms, now, labels),
    ]
    try:
        client.create_time_series(name=project_name, time_series=series)
    except Exception:
        logger.exception("billing_refresh_metrics_emit_failed")


def _time_series(monitoring_v3, metric_name: str, value: int, at: datetime, labels: dict[str, str]):
    series = monitoring_v3.TimeSeries()
    series.metric.type = f"{_METRIC_PREFIX}/{metric_name}"
    series.metric.labels.update(labels)
    series.resource.type = "global"
    project_id = _project_id()
    if project_id:
        series.resource.labels["project_id"] = project_id

    interval = monitoring_v3.TimeInterval()
    interval.end_time.seconds = int(at.timestamp())
    interval.end_time.nanos = at.microsecond * 1000
    point = monitoring_v3.Point(
        interval=interval,
        value=monitoring_v3.TypedValue(int64_value=value),
    )
    series.points = [point]
    return series


def _project_id() -> str | None:
    return os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT") or os.getenv("PROJECT_ID")
