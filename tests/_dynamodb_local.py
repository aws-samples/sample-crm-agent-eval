# SPDX-License-Identifier: MIT
"""Reach, and if needed start, the DynamoDB Local engine the integration and stub suites share.

:func:`ensure_started` probes the loopback port and, when nothing answers, runs
``scripts/run_local_dynamodb.sh start`` under an exclusive ``fcntl`` lock in a directory every
caller of one pytest run shares (:func:`start_lock_dir`), re-probing once the lock is held, so
concurrent callers start the engine at most once.
"""

from __future__ import annotations

import fcntl
import os
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

HOST: Final[str] = "127.0.0.1"
PORT: Final[int] = 8000
ENDPOINT_URL: Final[str] = f"http://{HOST}:{PORT}"
START_SCRIPT: Final[Path] = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_local_dynamodb.sh"
)
START_LOCK_NAME: Final[str] = "dynamodb-local-start.lock"

_CONNECT_TIMEOUT_SECONDS: Final[float] = 1.0
_START_TIMEOUT_SECONDS: Final[int] = 45


def reachable() -> bool:
    """Return whether something is listening on the DynamoDB Local loopback port."""
    try:
        with socket.create_connection((HOST, PORT), timeout=_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def start_lock_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The temp directory every xdist worker of this run shares; the base temp when serial."""
    base = tmp_path_factory.getbasetemp()
    return base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base


def _run_start_script(start_script: Path) -> bool:
    """Run ``start_script start``; ``False`` on a missing script, a failure or a timeout."""
    if not start_script.exists():
        return False
    try:
        result = proc_run.run(
            [str(start_script), "start"],
            capture_output=True,
            text=True,
            timeout=_START_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ensure_started(
    lock_dir: Path,
    *,
    start_script: Path = START_SCRIPT,
    probe: Callable[[], bool] = reachable,
) -> bool:
    """Return whether the engine is reachable, starting it under the shared lock if it is not.

    Args:
        lock_dir: Directory holding the lock file; callers that must not start the engine twice
            pass the same directory.
        start_script: The script run with ``start`` when the engine is unreachable.
        probe: Reachability check, run before the lock, after it is acquired and after a start.

    Returns:
        ``True`` once ``probe`` succeeds; ``False`` when the engine could not be started. Never
        raises for a missing Docker daemon or script.
    """
    if probe():
        return True
    with (lock_dir / START_LOCK_NAME).open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            if probe():
                return True
            return _run_start_script(start_script) and probe()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
