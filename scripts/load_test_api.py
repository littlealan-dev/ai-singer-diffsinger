"""Load-test a local API with simulated users, to size the CPU-only API's concurrency.

Each simulated user signs in through the Firebase Auth emulator, uploads a
score (parse on the CPU MCP pool), asks for solfege (a scripted LLM turn that
runs add_solfege_lyric_verse and re-parses), then makes plain chat turns,
reading the score after each one, as the UI does. A probe polls /readyz every
100 ms: it does no work, so its slow answers show the event loop stalling.

It needs an API in E2E test mode, so chat turns use the scripted LLM:

    APP_ENV=test BACKEND_E2E_TEST_MODE=1 LLM_PROVIDER=regression
    BACKEND_REQUIRE_APP_CHECK=false SESSION_STORE=firestore MCP_GPU_ENABLED=false

and the Auth, Firestore and Storage emulators. Run it, for example:

    python scripts/load_test_api.py --base-url http://127.0.0.1:8100 --users 10 20

Local only: emulator sign-in does not work against a deployed service, and a
laptop core is not a Cloud Run vCPU, so treat the numbers as relative.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = ROOT / "ui" / "e2e" / "fixtures" / "solfege-source.xml"


class Recorder:
    """Per-step latencies and failures."""

    def __init__(self) -> None:
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, list[str]] = defaultdict(list)

    async def timed(self, step: str, call):
        started = time.monotonic()
        try:
            response = await call
        except Exception as exc:  # timeouts, refused connections
            self.errors[step].append(type(exc).__name__)
            return None
        self.latencies[step].append(time.monotonic() - started)
        if response.status_code >= 400:
            self.errors[step].append(str(response.status_code))
            return None
        return response


async def sign_in(client: httpx.AsyncClient, auth_emulator: str, uid: str) -> str:
    """Sign a test identity in on the Auth emulator and return its ID token."""
    id_token = json.dumps({"sub": uid, "email": f"{uid}@example.test", "email_verified": True})
    response = await client.post(
        f"http://{auth_emulator}/identitytoolkit.googleapis.com/v1/accounts:signInWithIdp",
        params={"key": "fake-api-key"},
        json={
            "postBody": f"id_token={id_token}&providerId=google.com",
            "requestUri": "http://localhost",
            "returnSecureToken": True,
        },
    )
    response.raise_for_status()
    return response.json()["idToken"]


async def simulated_user(
    index: int,
    *,
    base_url: str,
    auth_emulator: str,
    fixture: Path,
    turns: int,
    recorder: Recorder,
    run_id: str,
) -> None:
    timeout = httpx.Timeout(180.0, connect=10.0)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        token = await sign_in(client, auth_emulator, f"e2e-load-{run_id}-{index}")
        client.headers["Authorization"] = f"Bearer {token}"
        # The first credits read gives an E2E account its credits.
        if await recorder.timed("credits", client.get("/credits")) is None:
            return
        created = await recorder.timed("create_session", client.post("/sessions"))
        if created is None:
            return
        session_id = created.json()["session_id"]
        uploaded = await recorder.timed(
            "upload",
            client.post(
                f"/sessions/{session_id}/upload",
                files={"file": (fixture.name, fixture.read_bytes(), "application/xml")},
            ),
        )
        if uploaded is None:
            return
        await recorder.timed("score", client.get(f"/sessions/{session_id}/score"))
        await recorder.timed(
            "chat_solfege",
            client.post(
                f"/sessions/{session_id}/chat",
                json={"message": "[e2e:solfege] add solfege to the solo part"},
            ),
        )
        await recorder.timed("score", client.get(f"/sessions/{session_id}/score"))
        for _ in range(turns):
            await recorder.timed(
                "chat_turn",
                client.post(
                    f"/sessions/{session_id}/chat",
                    json={"message": "[e2e:basic] what can you sing?"},
                ),
            )
            await recorder.timed("score", client.get(f"/sessions/{session_id}/score"))


async def probe_event_loop(base_url: str, stop: asyncio.Event, samples: list[float]) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        while not stop.is_set():
            started = time.monotonic()
            try:
                await client.get("/readyz")
                samples.append(time.monotonic() - started)
            except Exception:
                samples.append(30.0)
            await asyncio.sleep(0.1)


def _summary(values: list[float]) -> str:
    if not values:
        return "no samples"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return (
        f"n={len(ordered):4d}  p50={statistics.median(ordered):6.2f}s  "
        f"p95={p95:6.2f}s  max={ordered[-1]:6.2f}s"
    )


async def run_level(args: argparse.Namespace, users: int) -> None:
    recorder = Recorder()
    probe: list[float] = []
    stop = asyncio.Event()
    run_id = uuid.uuid4().hex[:8]
    prober = asyncio.create_task(probe_event_loop(args.base_url, stop, probe))
    started = time.monotonic()
    await asyncio.gather(
        *(
            simulated_user(
                index,
                base_url=args.base_url,
                auth_emulator=args.auth_emulator,
                fixture=args.fixture,
                turns=args.turns,
                recorder=recorder,
                run_id=run_id,
            )
            for index in range(users)
        )
    )
    elapsed = time.monotonic() - started
    stop.set()
    await prober
    print(f"\n=== {users} concurrent users: {elapsed:.1f}s wall time ===")
    for step in ("credits", "create_session", "upload", "chat_solfege", "chat_turn", "score"):
        errors = recorder.errors.get(step, [])
        error_text = f"  errors={len(errors)} {sorted(set(errors))}" if errors else ""
        print(f"  {step:15s} {_summary(recorder.latencies.get(step, []))}{error_text}")
    print(f"  {'readyz probe':15s} {_summary(probe)}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--auth-emulator", default="127.0.0.1:9099")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--turns", type=int, default=3, help="plain chat turns per user")
    parser.add_argument("--users", type=int, nargs="+", default=[5, 10, 20])
    args = parser.parse_args()
    for users in args.users:
        await run_level(args, users)


if __name__ == "__main__":
    asyncio.run(main())
