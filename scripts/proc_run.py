# SPDX-License-Identifier: MIT
"""The one place scripts/ and tests/ start a child process.

`run` and `popen` take an argv sequence and never a shell: a `str` argv or any `shell` keyword
raises `TypeError` before anything is started. Otherwise they pass straight through to
`subprocess.run` / `subprocess.Popen`, so results, exceptions and timeouts are unchanged.

Import as `from scripts import proc_run` with the repository root on `sys.path`.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any


def _checked_argv(argv: Sequence[str]) -> list[str]:
    """`argv` as a list, refusing a bare string or bytes and any non-`str` element."""
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence of str, not a single string")
    items = list(argv)
    if not all(isinstance(item, str) for item in items):
        raise TypeError("every argv element must be a str")
    return items


def _refuse_shell(kwargs: Mapping[str, Any]) -> None:
    if "shell" in kwargs:
        raise TypeError("proc_run never runs a shell; the `shell` keyword is refused")


def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,
    capture_output: bool = False,
    text: bool | None = None,
    errors: str | None = None,
    check: bool = False,
    timeout: float | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """`subprocess.run` with an argv list and never a shell.

    Raises `TypeError` for a `str` argv or a `shell` keyword; otherwise behaves as `subprocess.run`.
    """
    _refuse_shell(kwargs)
    if kwargs:
        raise TypeError(f"unsupported keyword(s): {sorted(kwargs)}")
    command = _checked_argv(argv)
    launch = subprocess.run
    return launch(  # nosec B603 - argv list checked above, shell refused
        command,
        cwd=cwd,
        env=env,
        input=input,
        capture_output=capture_output,
        text=text,
        errors=errors,
        check=check,
        timeout=timeout,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )


def popen(argv: Sequence[str], **kwargs: Any) -> subprocess.Popen[Any]:
    """`subprocess.Popen` with the same argv rules as :func:`run`; `shell` is refused."""
    _refuse_shell(kwargs)
    command = _checked_argv(argv)
    launch = subprocess.Popen
    return launch(command, **kwargs)  # nosec B603 - argv list checked above, shell refused
