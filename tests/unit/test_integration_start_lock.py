# SPDX-License-Identifier: MIT
"""``tests._dynamodb_local.ensure_started`` starts DynamoDB Local once however many processes find it
down.

Each case launches concurrent interpreters that call ``ensure_started`` with a file-backed
reachability probe and a stub start script, released together by a shared go-file. No engine,
Docker or network is used.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

from scripts import proc_run

_PROCESSES = 6
_READY_TIMEOUT_SECONDS = 60.0
_RUN_TIMEOUT_SECONDS = 60.0

#: Run by each child: ``argv`` is the lock directory, the state directory, the start script and the
#: child index.
_CHILD = """
import sys, time
from pathlib import Path
from tests._dynamodb_local import ensure_started

lock_dir, state, script = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
up = state / "engine-up"
(state / f"ready-{sys.argv[4]}").touch()
while not (state / "go").exists():
    time.sleep(0.005)
print(ensure_started(lock_dir, start_script=script, probe=up.exists))
"""

#: Stub for ``scripts/run_local_dynamodb.sh``: logs the run, then marks the engine up.
_START_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
[ "$1" = start ]
state={state}
echo "$$" >> "$state/starts"
sleep 0.5
touch "$state/engine-up"
"""


def _run_concurrently(tmp_path: Path) -> tuple[list[str], list[str]]:
    """Start the children, release them together, and return their outputs and logged starts."""
    lock_dir = tmp_path / "lock"
    state = tmp_path / "state"
    lock_dir.mkdir()
    state.mkdir()
    script = tmp_path / "start.sh"
    script.write_text(_START_SCRIPT.format(state=shlex.quote(str(state))), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    children = [
        proc_run.popen(
            [sys.executable, "-c", _CHILD, str(lock_dir), str(state), str(script), str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for index in range(_PROCESSES)
    ]
    try:
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while len(list(state.glob("ready-*"))) < _PROCESSES:
            assert time.monotonic() < deadline, "the children never all reached the go barrier"
            assert all(child.poll() is None for child in children), "a child exited early"
            time.sleep(0.01)
        (state / "go").touch()
        outputs = []
        for child in children:
            stdout, stderr = child.communicate(timeout=_RUN_TIMEOUT_SECONDS)
            assert child.returncode == 0, stderr
            outputs.append(stdout.strip())
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
    starts_log = state / "starts"
    starts = starts_log.read_text(encoding="utf-8").split() if starts_log.exists() else []
    return outputs, starts


def test_concurrent_callers_that_find_the_engine_down_start_it_exactly_once(
    tmp_path: Path,
) -> None:
    outputs, starts = _run_concurrently(tmp_path)

    assert len(starts) == 1, f"the start command ran {len(starts)} times: {starts}"
    assert outputs == ["True"] * _PROCESSES, outputs
