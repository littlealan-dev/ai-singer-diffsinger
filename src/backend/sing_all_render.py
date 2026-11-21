"""Render-job slot queue and the Sing All Cloud Run Job entry point."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud import firestore

from src.backend.firebase_app import get_firestore_client
from src.backend.sing_all import ENDED_ITEM_STATUSES


_ENDED = {"completed", "failed", "cancelled", "stopped"}


def _slots(db: Any) -> Any:
    return db.collection("singAllRender").document("slots")


def _job(db: Any, job_id: str) -> Any:
    return db.collection("singAllJobs").document(job_id)


def _positions(transaction: Any, db: Any, waiting: list[dict[str, Any]]) -> None:
    for index, entry in enumerate(waiting, 1):
        transaction.update(_job(db, entry["jobId"]), {"render.placeInLine": index})


def enter_line(job_id: str, max_slots: int, *, db: Any = None,
               now: datetime | None = None) -> str:
    """Reserve a slot or join the FIFO line in one Firestore transaction."""
    db = db or get_firestore_client()
    now = now or datetime.now(timezone.utc)
    slot_ref, job_ref = _slots(db), _job(db, job_id)

    @firestore.transactional
    def _enter(transaction: Any) -> str:
        slots = slot_ref.get(transaction=transaction).to_dict() or {}
        current = job_ref.get(transaction=transaction).to_dict() or {}
        running = list(slots.get("running") or [])
        waiting = list(slots.get("waiting") or [])
        if current.get("status") != "running" or current.get("stage") != "singing":
            return "inactive"
        if job_id in running:
            return "running"
        if any(entry["jobId"] == job_id for entry in waiting):
            return "waiting"
        if len(running) < max_slots:
            running.append(job_id)
            transaction.set(slot_ref, {"running": running, "waiting": waiting})
            transaction.update(job_ref, {"render": {"state": "running", "attempt": 1,
                                                    "placeInLine": None,
                                                    "queuedAt": None, "operation": None,
                                                    "launchLeaseUntil": None,
                                                    "startedAt": now},
                                         "singingStartedAt": now})
            return "start"
        waiting.append({"jobId": job_id, "queuedAt": now})
        transaction.set(slot_ref, {"running": running, "waiting": waiting})
        transaction.update(job_ref, {"render": {"state": "waiting", "attempt": 0,
                                                "placeInLine": len(waiting),
                                                "queuedAt": now, "startedAt": None,
                                                "operation": None}})
        return "waiting"

    return _enter(db.transaction())


def record_execution(job_id: str, operation: str, *, db: Any = None) -> None:
    db = db or get_firestore_client()
    ref = _job(db, job_id)

    @firestore.transactional
    def _record(transaction: Any) -> None:
        current = ref.get(transaction=transaction).to_dict() or {}
        if (current.get("render") or {}).get("state") == "running":
            transaction.update(ref, {"render.operation": operation,
                                     "render.launchLeaseUntil": None})

    _record(db.transaction())


def reserve_launch(job_id: str, *, db: Any = None,
                   now: datetime | None = None) -> bool:
    """Give one caller a short lease to submit the Cloud Run execution."""
    db = db or get_firestore_client()
    now = now or datetime.now(timezone.utc)
    ref = _job(db, job_id)

    @firestore.transactional
    def _reserve(transaction: Any) -> bool:
        job = ref.get(transaction=transaction).to_dict() or {}
        render = job.get("render") or {}
        lease = render.get("launchLeaseUntil")
        if (render.get("state") != "running" or render.get("operation")
                or (isinstance(lease, datetime) and lease > now)):
            return False
        transaction.update(ref, {"render.launchLeaseUntil": now + timedelta(minutes=2)})
        return True

    return _reserve(db.transaction())


def remove_waiting(job_id: str, *, db: Any = None) -> bool:
    """Remove a stopped parent from the line and correct the visible positions."""
    db = db or get_firestore_client()
    slot_ref = _slots(db)

    @firestore.transactional
    def _remove(transaction: Any) -> bool:
        slots = slot_ref.get(transaction=transaction).to_dict() or {}
        waiting = list(slots.get("waiting") or [])
        if not any(entry["jobId"] == job_id for entry in waiting):
            return False
        waiting = [entry for entry in waiting if entry["jobId"] != job_id]
        transaction.update(_job(db, job_id),
                           {"render.state": "ended", "render.placeInLine": None})
        _positions(transaction, db, waiting)
        transaction.set(slot_ref, {"running": slots.get("running") or [],
                                   "waiting": waiting})
        return True

    return _remove(db.transaction())


def leave_slot(job_id: str, *, retry: bool, max_slots: int, db: Any = None,
               now: datetime | None = None) -> list[str]:
    """End an execution and promote the oldest waiting parent."""
    db = db or get_firestore_client()
    now = now or datetime.now(timezone.utc)
    slot_ref = _slots(db)

    @firestore.transactional
    def _leave(transaction: Any) -> list[str]:
        slots = slot_ref.get(transaction=transaction).to_dict() or {}
        running = list(slots.get("running") or [])
        waiting = list(slots.get("waiting") or [])
        current = _job(db, job_id).get(transaction=transaction).to_dict() or {}
        queued_jobs = {entry["jobId"]: _job(db, entry["jobId"]).get(
            transaction=transaction).to_dict() or {} for entry in waiting}
        if job_id not in running:
            return []
        running.remove(job_id)
        render = current.get("render") or {}
        retry_queued = (retry and current.get("status") == "running"
                        and not current.get("cancelRequested")
                        and int(render.get("attempt") or 0) < 2)
        if retry_queued:
            waiting.append({"jobId": job_id, "queuedAt": now})
            queued_jobs[job_id] = current
        promoted: list[str] = []
        while waiting and len(running) < max_slots:
            entry = waiting.pop(0)
            candidate = queued_jobs[entry["jobId"]]
            if candidate.get("status") != "running" or candidate.get("cancelRequested"):
                transaction.update(_job(db, entry["jobId"]),
                                   {"render.state": "ended", "render.placeInLine": None})
                continue
            candidate_id = entry["jobId"]
            running.append(candidate_id)
            promoted.append(candidate_id)
            attempt = int((candidate.get("render") or {}).get("attempt") or 0) + 1
            transaction.update(_job(db, candidate_id),
                               {"render.state": "running", "render.attempt": attempt,
                                "render.placeInLine": None, "render.operation": None,
                                "render.launchLeaseUntil": None,
                                "render.startedAt": now,
                                "singingStartedAt": now})
        if job_id not in promoted:
            transaction.update(_job(db, job_id),
                               {"render.state": "waiting" if any(
                                   entry["jobId"] == job_id for entry in waiting) else "ended",
                                "render.placeInLine": None, "render.operation": None,
                                **({"render.queuedAt": now} if retry_queued else {})})
        _positions(transaction, db, waiting)
        transaction.set(slot_ref, {"running": running, "waiting": waiting})
        return promoted

    return _leave(db.transaction())


async def run_parent(job_id: str, orchestrator: Any, *, concurrency: int,
                     launch_next: Any) -> None:
    """Prepare child specs sequentially, then run up to concurrency on the GPU."""
    from src.backend import sing_all_store
    from src.backend.sing_all_runner import SingAllRunner

    runner = SingAllRunner(orchestrator)
    parent = await asyncio.to_thread(sing_all_store.load, job_id)
    attempt = int(((parent or {}).get("render") or {}).get("attempt") or 1)
    try:
        items = [item for item in (parent or {}).get("items") or []
                 if item["type"] == "take" and item.get("status") not in _ENDED]
        for offset in range(0, len(items), concurrency):
            parent = await asyncio.to_thread(sing_all_store.load, job_id)
            if not parent or parent.get("status") != "running" or parent.get("cancelRequested"):
                break
            batch = []
            for item in items[offset:offset + concurrency]:
                child_id = item["childJobId"]
                child = await asyncio.to_thread(orchestrator._job_store.get_job, child_id)
                if child and child.get("status") in _ENDED:
                    continue
                if child is None or (child.get("status") == "queued"
                                     and not child.get("synthesisSpecJson")):
                    snapshot = await orchestrator._sessions.get_snapshot(
                        parent["sessionId"], parent["uid"])
                    await runner._start_take(parent, {"itemId": item["itemId"]}, snapshot,
                                             defer_dispatch=True)
                batch.append(child_id)

            async def run_child(child_id: str) -> None:
                worker_id = f"sing-all-render:{uuid.uuid4().hex}"
                route = orchestrator._synthesis_dispatcher.route
                for _ in range(2):
                    claim = await asyncio.to_thread(
                        orchestrator._job_store.claim_dispatched_job, child_id,
                        worker_id=worker_id, lease_seconds=route.lease_seconds,
                        max_attempts=route.max_dispatch_attempts)
                    if claim.outcome != "busy":
                        break
                    expiry = claim.job.get("leaseExpiresAt")
                    if not isinstance(expiry, datetime):
                        break
                    delay = max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds())
                    await asyncio.sleep(min(delay + 0.1, route.lease_seconds + 0.1))
                if claim.outcome == "claimed":
                    await orchestrator.run_dispatched_synthesis_job(
                        child_id, claim.job, attempt=claim.attempt, worker_id=worker_id)
                elif claim.outcome == "exhausted":
                    await orchestrator.fail_exhausted_synthesis_job(child_id)

            await asyncio.gather(*(run_child(child_id) for child_id in batch),
                                 return_exceptions=True)
            await runner.reconcile(job_id)
    finally:
        parent = await asyncio.to_thread(sing_all_store.load, job_id)
        missing = [] if not parent else [item for item in parent.get("items") or []
                                         if item["type"] == "take" and item["status"]
                                         not in ENDED_ITEM_STATUSES]
        retry = bool(missing and attempt < 2 and parent and parent.get("status") == "running"
                     and not parent.get("cancelRequested"))
        if missing and not retry and attempt >= 2:
            await fail_remaining(job_id, orchestrator)
        promoted = await asyncio.to_thread(
            leave_slot, job_id, retry=retry,
            max_slots=orchestrator._settings.max_render_slots)
        for next_id in promoted:
            await launch_next(next_id)
        await orchestrator._enqueue_sing_all_reconcile(job_id)


async def fail_remaining(job_id: str, orchestrator: Any) -> None:
    """End every take left after the second execution and release its hold."""
    from src.backend import sing_all_store
    from src.backend.credits import release_credits
    from src.backend.sing_all_runner import SingAllRunner

    for _ in range(8):
        parent = await asyncio.to_thread(sing_all_store.load, job_id)
        if not parent or parent.get("status") != "running":
            return
        missing = [item for item in parent.get("items") or []
                   if item["type"] == "take" and item["status"]
                   not in ENDED_ITEM_STATUSES]
        if not missing:
            break
        absent = []
        for item in missing:
            child = await asyncio.to_thread(orchestrator._job_store.get_job, item["childJobId"])
            if child:
                await orchestrator._fail_undelivered_synthesis_job(item["childJobId"])
            else:
                await asyncio.to_thread(release_credits, parent["uid"], item["childJobId"])
                absent.append(item["itemId"])
        if absent:
            for item in parent["items"]:
                if item["itemId"] in absent:
                    item.update(status="failed", reason="render_incomplete")
            if not await asyncio.to_thread(
                sing_all_store.apply, job_id, {"items": parent["items"]},
                expected_stage="singing", expected_revision=parent.get("revision", 0)):
                continue
        await SingAllRunner(orchestrator).reconcile(job_id)
        return


async def recover_execution(job_id: str, orchestrator: Any, *,
                            operation_done: Any, launch_next: Any) -> None:
    """Sweep a render execution whose container exited before releasing its slot."""
    from src.backend import sing_all_store
    from src.backend.sing_all_runner import SingAllRunner

    parent = await asyncio.to_thread(sing_all_store.load, job_id)
    if not parent or (parent.get("render") or {}).get("state") != "running":
        return
    operation = (parent.get("render") or {}).get("operation")
    if not operation or not await operation_done(operation):
        return
    await SingAllRunner(orchestrator).reconcile(job_id)
    parent = await asyncio.to_thread(sing_all_store.load, job_id)
    if not parent or (parent.get("render") or {}).get("state") != "running":
        return
    attempt = int(parent["render"].get("attempt") or 1)
    missing = [item for item in parent.get("items") or []
               if item["type"] == "take" and item["status"]
               not in ENDED_ITEM_STATUSES]
    retry = bool(missing and attempt < 2 and parent.get("status") == "running"
                 and not parent.get("cancelRequested"))
    if missing and attempt >= 2:
        await fail_remaining(job_id, orchestrator)
    promoted = await asyncio.to_thread(
        leave_slot, job_id, retry=retry,
        max_slots=orchestrator._settings.max_render_slots)
    for next_id in promoted:
        await launch_next(next_id)
    await orchestrator._enqueue_sing_all_reconcile(job_id)


async def _main() -> None:
    job_id = os.environ["SING_ALL_JOB_ID"]
    from src.backend.main import app
    from src.backend.sing_all_runner import start_render_execution

    async with app.router.lifespan_context(app):
        orchestrator = app.state.orchestrator
        await run_parent(job_id, orchestrator,
                         concurrency=app.state.settings.sing_all_render_concurrency,
                         launch_next=lambda next_id: start_render_execution(
                             next_id, app.state.settings))


if __name__ == "__main__":
    asyncio.run(_main())
