# SPDX-License-Identifier: MIT
"""`scripts/check_subprocess_funnel.py`: no process starts outside `scripts/proc_run.py`."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "check_subprocess_funnel.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_subprocess_funnel_for_test", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

_GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
}


def _git(root: Path, *args: str) -> None:
    proc_run.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A scratch git repository with `files` written and tracked."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if files:
        _git(root, "add", "-A")
    return root


_CLEAN = "import subprocess\n\nfrom scripts import proc_run\n\nproc_run.run(['true'])\n"


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['true'])\n",
        "import subprocess\nsubprocess.Popen(['true'])\n",
        "import subprocess\nsubprocess.call(['true'])\n",
        "import subprocess\nsubprocess.check_call(['true'])\n",
        "import subprocess\nsubprocess.check_output(['true'])\n",
        "def f(gate):\n    gate.subprocess.run(['true'])\n",
        "from subprocess import run\n",
        "from subprocess import Popen as P\n",
        "from subprocess import check_output\n",
        "from subprocess import *\n",
        "import subprocess as sp\n",
        "import os, subprocess as sp\n",
        "import subprocess\nreal_run = subprocess.run\n",
        "import subprocess\nlaunch: object = subprocess.Popen\n",
        "import subprocess\nif (launch := subprocess.check_output):\n    pass\n",
        "import subprocess\ngetattr(subprocess, 'run')(['true'])\n",
        "def f(gate):\n    getattr(gate.subprocess, 'Popen')\n",
    ],
    ids=[
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "attribute-chain",
        "from-run",
        "from-Popen-aliased",
        "from-check_output",
        "from-star",
        "aliased-import",
        "aliased-import-in-list",
        "bound-reference",
        "annotated-bound-reference",
        "walrus-bound-reference",
        "getattr-call",
        "getattr-attribute-chain",
    ],
)
def test_each_forbidden_form_is_reported(source: str) -> None:
    """Every launch form outside the funnel yields exactly one violation naming file and line."""
    violations = gate.find_violations(source, "tests/x.py")
    assert len(violations) == 1, violations
    assert violations[0].startswith("tests/x.py:")


@pytest.mark.parametrize(
    "source",
    [
        _CLEAN,
        "import subprocess\nsubprocess.CompletedProcess(['x'], 0, '', '')\n",
        "import subprocess\nx = subprocess.PIPE, subprocess.DEVNULL, subprocess.TimeoutExpired\n",
        "from subprocess import CompletedProcess, PIPE\n",
        "import subprocess\np: subprocess.Popen[str]\nisinstance(p, subprocess.Popen)\n",
        "x = 'subprocess.run([\"git\"])'\n",
        "run(['true'])\n",
    ],
    ids=[
        "proc_run",
        "CompletedProcess",
        "constants",
        "from-import-non-launcher",
        "annotation-and-isinstance",
        "string-literal",
        "bare-run",
    ],
)
def test_allowed_forms_are_not_reported(source: str) -> None:
    """Non-launch uses of `subprocess` and calls through `proc_run` pass."""
    assert gate.find_violations(source, "tests/x.py") == []


def test_a_violation_in_tests_fails_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tracked file under tests/ that calls subprocess directly exits 1 and is named."""
    root = _repo(
        tmp_path,
        {"tests/ok.py": _CLEAN, "tests/bad.py": "import subprocess\nsubprocess.run(['x'])\n"},
    )
    assert gate.main(["--repo-root", str(root)]) == 1
    assert "tests/bad.py:2" in capsys.readouterr().err


def test_a_violation_in_scripts_fails_the_gate(tmp_path: Path) -> None:
    """scripts/ is in scope as well as tests/."""
    root = _repo(tmp_path, {"scripts/bad.py": "from subprocess import run\n"})
    assert gate.main(["--repo-root", str(root)]) == 1


def test_proc_run_is_exempt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """scripts/proc_run.py may call subprocess; the same call anywhere else may not."""
    launcher = "import subprocess\nsubprocess.run(['x'])\nsubprocess.Popen(['x'])\n"
    root = _repo(tmp_path, {"scripts/proc_run.py": launcher, "tests/ok.py": _CLEAN})
    assert gate.main(["--repo-root", str(root)]) == 0
    assert "OK — 1 tracked file(s)" in capsys.readouterr().out


def test_the_exemption_is_by_exact_path(tmp_path: Path) -> None:
    """A file named proc_run.py outside scripts/ is not exempt."""
    root = _repo(tmp_path, {"tests/proc_run.py": "import subprocess\nsubprocess.run(['x'])\n"})
    assert gate.main(["--repo-root", str(root)]) == 1


def test_out_of_scope_and_untracked_files_are_not_read(tmp_path: Path) -> None:
    """Only tracked *.py under scripts/ and tests/ is the subject."""
    root = _repo(
        tmp_path,
        {
            "tests/ok.py": _CLEAN,
            "src/eval/launch.py": "import subprocess\nsubprocess.run(['x'])\n",
            "tests/notes.txt": "subprocess.run(['x'])\n",
        },
    )
    (root / "tests" / "untracked.py").write_text("from subprocess import run\n", encoding="utf-8")
    assert gate.main(["--repo-root", str(root)]) == 0


def test_an_unparsable_file_fails_rather_than_passing(tmp_path: Path) -> None:
    """A tracked file the gate cannot parse is a failure, never a silent pass."""
    root = _repo(tmp_path, {"tests/broken.py": "def broken(:\n"})
    assert gate.main(["--repo-root", str(root)]) == 1


def test_an_empty_scope_skips_with_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No tracked *.py in scope means nothing was verified: exit 3, never 0."""
    root = _repo(tmp_path, {"src/a.py": _CLEAN})
    assert gate.main(["--repo-root", str(root)]) == gate.SKIP_EXIT_STATUS == 3
    assert "[SKIP]" in capsys.readouterr().out


def test_a_non_git_directory_is_an_environment_error(tmp_path: Path) -> None:
    """Without a git index the scope is unknown, so the gate exits 2."""
    assert gate.main(["--repo-root", str(tmp_path)]) == 2


def test_the_current_tree_is_green(capsys: pytest.CaptureFixture[str]) -> None:
    """The repository passes, and the scope it read is non-trivial."""
    assert gate.main([]) == 0
    out = capsys.readouterr().out
    checked = int(out.split("OK — ", 1)[1].split(" ", 1)[0])
    assert checked > 100, out


def test_the_skip_status_matches_run_gates() -> None:
    """The gate's skip status is the one `run_gates.py` renders as [SKIP]."""
    spec = importlib.util.spec_from_file_location(
        "run_gates_for_funnel_test", REPO_ROOT / "scripts" / "run_gates.py"
    )
    assert spec is not None and spec.loader is not None
    run_gates = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_gates)
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS
    assert ("check_subprocess_funnel", [sys.executable, "scripts/check_subprocess_funnel.py"]) in [
        (name, argv) for name, argv in run_gates.GATES
    ]
