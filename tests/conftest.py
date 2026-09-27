import os
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# Default local test behavior: keep v2 syllable alignment/timing enabled
# unless a test/run explicitly overrides them.
os.environ.setdefault("SYLLABLE_ALIGNER_V2", "1")
os.environ.setdefault("SYLLABLE_TIMING_V2", "1")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_EMULATOR_HOST = "localhost:8080"
# firebase.json pins the Firestore emulator to this port, so an emulator this
# fixture starts can only serve a host that uses it.
_CONFIGURED_EMULATOR_PORT = "8080"
_EMULATOR_PROJECT = "demo-project"
_EMULATOR_START_TIMEOUT_SECONDS = 90
_EMULATOR_START_COMMAND = (
    f"npx firebase emulators:start --only firestore --project {_EMULATOR_PROJECT}"
)


def _firestore_emulator_answers(host: str) -> bool:
    """Return True when a Firestore emulator responds at host.

    Checked over HTTP rather than trusted: Firestore's gRPC client retries an
    unreachable emulator with long backoff, so a test that simply connects
    hangs for minutes instead of failing.
    """
    try:
        with urllib.request.urlopen(f"http://{host}/", timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _stop_emulator(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    # The CLI runs the emulator JVM as a child; signal the whole group.
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


@pytest.fixture(scope="session")
def firestore_emulator():
    """Provide a reachable Firestore emulator, or fail fast.

    Reuses an emulator that already answers, such as a running local stack.
    Otherwise starts one for the session and stops it afterwards, unless
    FIRESTORE_EMULATOR_AUTOSTART=0, in which case the tests fail immediately
    with the command to start it.
    """
    host = os.environ.get("FIRESTORE_EMULATOR_HOST") or _DEFAULT_EMULATOR_HOST
    os.environ["FIRESTORE_EMULATOR_HOST"] = host
    if _firestore_emulator_answers(host):
        yield host
        return

    unavailable = f"No Firestore emulator answers at {host}."
    if os.environ.get("FIRESTORE_EMULATOR_AUTOSTART", "1") == "0":
        pytest.fail(
            f"{unavailable} Start one with `{_EMULATOR_START_COMMAND}`, "
            "or unset FIRESTORE_EMULATOR_AUTOSTART to let the tests start it.",
            pytrace=False,
        )
    if host.rsplit(":", 1)[-1] != _CONFIGURED_EMULATOR_PORT:
        pytest.fail(
            f"{unavailable} An emulator started here would listen on port "
            f"{_CONFIGURED_EMULATOR_PORT} (firebase.json), not the requested host.",
            pytrace=False,
        )
    if shutil.which("npx") is None:
        pytest.fail(f"{unavailable} npx is not installed, so it cannot be started.", pytrace=False)

    log = tempfile.NamedTemporaryFile(
        prefix="firestore-emulator-", suffix=".log", delete=False
    )
    process = subprocess.Popen(
        _EMULATOR_START_COMMAND.split(),
        cwd=_REPO_ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + _EMULATOR_START_TIMEOUT_SECONDS
        while not _firestore_emulator_answers(host):
            if process.poll() is not None or time.monotonic() > deadline:
                log.flush()
                tail = Path(log.name).read_text(errors="replace")[-2000:]
                pytest.fail(
                    f"{unavailable} Starting it with `{_EMULATOR_START_COMMAND}` "
                    f"failed. Log {log.name}:\n{tail}",
                    pytrace=False,
                )
            time.sleep(0.5)
        yield host
    finally:
        _stop_emulator(process)
        log.close()
