# SPDX-License-Identifier: MIT
"""`scripts.proc_run`: argv-only, never-a-shell wrappers that otherwise behave as `subprocess`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import proc_run

PY = sys.executable


def test_run_returns_the_completed_process_of_an_argv_list() -> None:
    """A list argv runs, and output, return code and capture flags pass through."""
    result = proc_run.run(
        [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        capture_output=True,
        text=True,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def test_run_accepts_a_tuple_argv() -> None:
    """Any `Sequence[str]` is an argv, not only a list."""
    assert proc_run.run((PY, "-c", "pass")).returncode == 0


def test_run_passes_cwd_env_and_input_through(tmp_path: Path) -> None:
    """`cwd`, `env` and `input` reach the child unchanged."""
    result = proc_run.run(
        [
            PY,
            "-c",
            "import os, sys; print(os.getcwd()); print(os.environ['K']); print(sys.stdin.read())",
        ],
        cwd=tmp_path,
        env={"K": "v", "PATH": "/usr/bin:/bin"},
        input="fed",
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1:] == ["v", "fed"]


def test_run_passes_stdin_devnull_through() -> None:
    """`stdin=subprocess.DEVNULL` gives the child `/dev/null`, not a pipe."""
    result = proc_run.run(
        [
            PY,
            "-c",
            "import os, sys; print(os.fstat(0).st_rdev == os.stat(os.devnull).st_rdev);"
            " print(repr(sys.stdin.read()))",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == ["True", "''"]


def test_run_passes_a_file_stdin_through(tmp_path: Path) -> None:
    """A file object passed as `stdin` is what the child reads."""
    source = tmp_path / "in.txt"
    source.write_text("from a file", encoding="utf-8")
    with source.open("rb") as handle:
        result = proc_run.run(
            [PY, "-c", "import sys; print(sys.stdin.read())"],
            stdin=handle,
            capture_output=True,
            text=True,
        )
    assert result.stdout == "from a file\n"


def test_run_passes_errors_through_for_undecodable_output() -> None:
    """`errors="replace"` decodes invalid UTF-8 rather than raising."""
    result = proc_run.run(
        [PY, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    assert result.stdout == "\ufffd"


def test_run_returns_bytes_without_text() -> None:
    """With no `text`, captured output stays bytes."""
    assert proc_run.run([PY, "-c", "print('b')"], capture_output=True).stdout == b"b\n"


def test_run_check_raises_called_process_error() -> None:
    """`check=True` raises `CalledProcessError` exactly as `subprocess.run` does."""
    with pytest.raises(subprocess.CalledProcessError):
        proc_run.run([PY, "-c", "raise SystemExit(1)"], check=True)


def test_run_timeout_raises_timeout_expired() -> None:
    """`timeout` raises `TimeoutExpired` exactly as `subprocess.run` does."""
    with pytest.raises(subprocess.TimeoutExpired):
        proc_run.run([PY, "-c", "import time; time.sleep(10)"], timeout=0.2)


def _outcome(launch: Any) -> tuple[str, object]:
    """`("raised", exception type)` or `("returned", returncode)` for one launch."""
    try:
        completed = launch(["/nonexistent/definitely-not-a-binary"], capture_output=True)
    except Exception as exc:
        return "raised", type(exc)
    return "returned", completed.returncode


def test_run_missing_binary_behaves_as_subprocess_run() -> None:
    """An unresolvable executable gives exactly what `subprocess.run` gives on this host.

    Natively that is `FileNotFoundError`; under some emulators it is a returncode such as 127.
    """
    assert _outcome(proc_run.run) == _outcome(subprocess.run)


@pytest.mark.parametrize("argv", ["echo hi", b"echo hi"])
def test_run_refuses_a_string_argv(argv: Any) -> None:
    """A `str` or `bytes` argv raises `TypeError` before anything runs."""
    with pytest.raises(TypeError, match="sequence of str"):
        proc_run.run(argv)


def test_run_refuses_a_non_str_element() -> None:
    """Every argv element must be a `str`."""
    with pytest.raises(TypeError, match="must be a str"):
        proc_run.run([PY, Path("x")])  # type: ignore[list-item]


@pytest.mark.parametrize("value", [True, False])
def test_run_refuses_any_shell_keyword(value: bool) -> None:
    """`shell` is refused whatever its value, so `shell=False` cannot be smuggled in either."""
    with pytest.raises(TypeError, match="shell"):
        proc_run.run([PY, "-c", "pass"], shell=value)  # nosec B604 - asserts the refusal


def test_run_refuses_an_unknown_keyword() -> None:
    """A keyword outside the signature raises `TypeError` instead of being dropped."""
    with pytest.raises(TypeError, match="unsupported"):
        proc_run.run([PY, "-c", "pass"], bufsize=0)


def test_refusals_start_no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused call never reaches `subprocess.run` or `subprocess.Popen`."""
    started: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **_k: started.append(a))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **_k: started.append(a))
    for call in (
        lambda: proc_run.run("ls"),
        lambda: proc_run.run(["ls"], shell=False),
        lambda: proc_run.popen("ls"),
        lambda: proc_run.popen(["ls"], shell=True),  # noqa: S604  # nosec B604 - asserts the refusal
    ):
        with pytest.raises(TypeError):
            call()
    assert started == []


def test_popen_runs_an_argv_list() -> None:
    """`popen` returns a live `Popen` whose pipes and return code behave as usual."""
    with proc_run.popen([PY, "-c", "print('p')"], stdout=subprocess.PIPE, text=True) as process:
        out, _ = process.communicate(timeout=30)
    assert isinstance(process, subprocess.Popen)
    assert (process.returncode, out) == (0, "p\n")


@pytest.mark.parametrize("argv", ["echo hi", b"echo hi"])
def test_popen_refuses_a_string_argv(argv: Any) -> None:
    """`popen` applies the same string-argv refusal as `run`."""
    with pytest.raises(TypeError, match="sequence of str"):
        proc_run.popen(argv)


@pytest.mark.parametrize("value", [True, False])
def test_popen_refuses_any_shell_keyword(value: bool) -> None:
    """`popen` refuses `shell` whatever its value."""
    with pytest.raises(TypeError, match="shell"):
        proc_run.popen([PY, "-c", "pass"], shell=value)  # nosec B604 - asserts the refusal
