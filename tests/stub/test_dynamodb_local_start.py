# SPDX-License-Identifier: MIT
"""``tests._dynamodb_local.ensure_started`` starts the engine once for concurrent callers.

A stub start script stands in for ``scripts/run_local_dynamodb.sh``: it records each run and
marks the engine reachable. No engine, Docker or network is used.
"""

from __future__ import annotations

import stat
import threading
from pathlib import Path

from tests._dynamodb_local import ensure_started

_CALLERS = 2
_BARRIER_TIMEOUT_SECONDS = 10.0


def _stub_start_script(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write a start script that sleeps, records the run, then marks the engine up."""
    runs, up = tmp_path / "runs.log", tmp_path / "engine-up"
    script = tmp_path / "start.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        '[ "$1" = start ]\n'
        "sleep 0.3\n"
        f"echo run >> '{runs}'\n"
        f"touch '{up}'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, runs, up


def _run_concurrently(lock_dir: Path, script: Path, up: Path) -> list[bool]:
    """Call ``ensure_started`` from ``_CALLERS`` threads that all see the engine down first."""
    barrier = threading.Barrier(_CALLERS, timeout=_BARRIER_TIMEOUT_SECONDS)
    first_probe = threading.local()
    results: list[bool] = []
    results_lock = threading.Lock()

    def probe() -> bool:
        if not getattr(first_probe, "done", False):
            first_probe.done = True
            barrier.wait()
            return False
        return up.exists()

    def caller() -> None:
        outcome = ensure_started(lock_dir, start_script=script, probe=probe)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=caller) for _ in range(_CALLERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_concurrent_callers_sharing_a_lock_dir_run_the_start_script_once(tmp_path: Path) -> None:
    script, runs, up = _stub_start_script(tmp_path)

    results = _run_concurrently(tmp_path, script, up)

    assert results == [True] * _CALLERS
    assert runs.read_text().splitlines() == ["run"]


def test_a_failing_start_script_reports_unavailable_without_raising(tmp_path: Path) -> None:
    script = tmp_path / "start.sh"
    script.write_text("#!/usr/bin/env bash\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    assert ensure_started(tmp_path, start_script=script, probe=lambda: False) is False


def test_a_missing_start_script_reports_unavailable(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sh"

    assert ensure_started(tmp_path, start_script=missing, probe=lambda: False) is False


def test_a_reachable_engine_never_runs_the_start_script(tmp_path: Path) -> None:
    script, runs, _up = _stub_start_script(tmp_path)

    assert ensure_started(tmp_path, start_script=script, probe=lambda: True) is True
    assert not runs.exists()
