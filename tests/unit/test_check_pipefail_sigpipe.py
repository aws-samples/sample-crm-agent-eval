# SPDX-License-Identifier: MIT
"""`scripts/check_pipefail_sigpipe.py` flags early-exit pipe consumers under pipefail, and only those."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import time
from pathlib import Path

import pytest
from scripts import check_pipefail_sigpipe as gate
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
TEARDOWN_SH = REPO_ROOT / "scripts" / "teardown.sh"
FIXED_ALB_LINE = 'if grep -qxF "$ALB_STATE_ADDRESS" <<<"$STATE_LIST_OUTPUT"; then'
BROKEN_ALB_LINE = 'if printf \'%s\\n\' "$STATE_LIST_OUTPUT" | grep -qxF "$ALB_STATE_ADDRESS"; then'


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    proc_run.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    if files:
        proc_run.run(["git", "-C", str(root), "add", "--", *files], check=True, capture_output=True)
    return root


def _check(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = gate.main(["--repo-root", str(root)])
    return rc, capsys.readouterr().out


def _shell(body: str) -> list[gate.Finding]:
    return gate.check_shell_file("x.sh", "#!/usr/bin/env bash\nset -euo pipefail\n" + body)


def test_the_tracked_tree_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _check(REPO_ROOT, capsys)
    assert rc == 0, out
    assert "OK" in out


def test_re_adding_the_teardown_pipe_reds_the_gate_at_that_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = TEARDOWN_SH.read_text(encoding="utf-8")
    assert source.count(FIXED_ALB_LINE) == 1
    control = _git_repo(tmp_path / "control", {"scripts/teardown.sh": source})
    rc, out = _check(control, capsys)
    assert rc == 0, out

    mutated_text = source.replace(FIXED_ALB_LINE, BROKEN_ALB_LINE)
    mutated = _git_repo(tmp_path / "mutated", {"scripts/teardown.sh": mutated_text})
    rc, out = _check(mutated, capsys)
    expected_line = mutated_text.splitlines().index(BROKEN_ALB_LINE) + 1
    assert rc == 1
    finding_lines = [line for line in out.splitlines() if line.startswith("scripts/")]
    assert len(finding_lines) == 1, out
    assert finding_lines[0].startswith(f"scripts/teardown.sh:{expected_line}: `grep`")


def test_shellcheck_does_not_catch_the_mutation(tmp_path: Path) -> None:
    """Only this gate sees the defect: shellcheck passes the mutated script."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not on PATH")
    mutated = tmp_path / "teardown.sh"
    mutated.write_text(
        TEARDOWN_SH.read_text(encoding="utf-8").replace(FIXED_ALB_LINE, BROKEN_ALB_LINE),
        encoding="utf-8",
    )
    result = proc_run.run([shellcheck, str(mutated)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_a_file_without_pipefail_is_not_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = "#!/usr/bin/env bash\nset -eu\nprintf '%s\\n' \"$x\" | grep -q y\n"
    rc, out = _check(_git_repo(tmp_path, {"a.sh": body}), capsys)
    assert rc == 0, out


@pytest.mark.parametrize(
    ("line", "enables"),
    [
        ("set -o pipefail", True),
        ("  - set -eu -o pipefail", True),
        ("set +e -Eeuo pipefail;", True),
        ("set\t-o\tpipefail", True),
        ("set -o pipefailx", False),
        ("set -e foo -o pipefail", False),
        ("set -O pipefail", False),
        ("-set -o pipefail", False),
        ("echo set -o pipefail", False),
        ("set -o", False),
    ],
)
def test_the_pipefail_line_predicate(line: str, enables: bool) -> None:
    assert (gate._pipefail_offset(f"#!/bin/bash\n{line}\n") == len("#!/bin/bash\n")) is enables


@pytest.mark.parametrize(
    "line",
    [
        "set " + "-eo " * 50_000 + "x",
        "set -" + "o" * 200_000 + " x",
        "set" + " \t" * 100_000 + "-e",
        "seq 9 ||" + " " * 200_000 + "x",
    ],
    ids=["many-flags", "long-flag", "long-blanks", "long-mask-blanks"],
)
def test_a_pathological_line_is_checked_within_a_time_bound(line: str) -> None:
    start = time.perf_counter()
    gate.check_shell_file("x.sh", f"#!/usr/bin/env bash\n{line}\nseq 9 | head -1\n")
    _shell(f"{line}\n")
    assert time.perf_counter() - start < 2.0


def test_a_pipeline_before_pipefail_is_enabled_is_not_flagged() -> None:
    text = "#!/bin/bash\nseq 9 | head -1\nset -o pipefail\nseq 9 | head -1\n"
    assert [f.line for f in gate.check_shell_file("x.sh", text)] == [4]


@pytest.mark.parametrize(
    "line",
    [
        'grep -qxF target <<<"$S"',
        "echo 'a | grep -q b'",
        'echo "a | head -1"',
        "# a | grep -q b",
        "x=$(seq 9 | sed -n '1{s/1/2/; p;}')",
        "seq 9 | sed 's/q/x/'",
        "seq 9 | grep -e q",
        "seq 9 | grep -A6 x",
        "seq 9 | grep -c x",
        "seq 9 | tail -n1",
        "[ a ] || grep -q x f",
        'x="$(seq 9 | head -1 || true)"',
        "x=$(seq 9 | grep -m1 x || :)",
        "cat <<'EOF'\nseq 9 | grep -q x\nEOF",
    ],
)
def test_forms_that_read_all_input_or_are_masked_captures_are_not_flagged(line: str) -> None:
    assert _shell(line + "\n") == []


@pytest.mark.parametrize(
    ("line", "consumer"),
    [
        ("seq 9 | grep -q 1", "grep"),
        ("seq 9 | grep -qxF 1", "grep"),
        ("seq 9 | grep --quiet 1", "grep"),
        ("seq 9 | grep -m 1 x", "grep"),
        ("seq 9 | grep --max-count=1 x", "grep"),
        ("seq 9 | head -n1", "head"),
        ("seq 9 | head -n1 | sed s/a/b/", "head"),
        ("seq 9 | sed -n 1q", "sed"),
        ("seq 9 | sed '/x/q'", "sed"),
        ('x="$(seq 9 | head -1)"', "head"),
        ("if seq 9 | grep -q 1 || true; then :; fi", "grep"),
        ("seq 9 \\\n  | LC_ALL=C grep -qx 1", "grep"),
        ("seq 9 |& head -1", "head"),
    ],
)
def test_early_exit_consumers_under_pipefail_are_flagged(line: str, consumer: str) -> None:
    findings = _shell(line + "\n")
    assert [(f.line, f.consumer) for f in findings] == [(3, consumer)]


def test_a_makefile_recipe_is_flagged_only_when_its_shell_has_pipefail() -> None:
    plain = "t:\n\t@printf '%s\\n' \"$$x\" | grep -q y\n"
    assert gate.check_makefile("Makefile", plain) == []
    own = "t:\n\t@set -o pipefail; \\\n\t  printf x | grep -q y\n"
    assert [f.line for f in gate.check_makefile("Makefile", own)] == [3]
    forced = ".SHELLFLAGS := -eu -o pipefail -c\nt:\n\techo\n\tprintf x | head -1\n"
    assert [f.line for f in gate.check_makefile("Makefile", forced)] == [4]


def test_a_github_step_is_flagged_when_bash_is_its_shell_or_it_sets_pipefail() -> None:
    workflow = (
        "jobs:\n"
        "  j:\n"
        "    steps:\n"
        "      - run: seq 9 | head -1\n"
        "      - shell: bash\n"
        "        run: seq 8 | head -1\n"
        "      - run: |\n"
        "          set -euo pipefail\n"
        "          seq 7 | grep -q 7\n"
        "      - run: |\n"
        "          set -euo pipefail\n"
        '          v="$(seq 6 | head -1 || true)"\n'
    )
    findings = gate.check_ci_file(".github/workflows/w.yml", workflow)
    assert [(f.line, f.consumer) for f in findings] == [(6, "head"), (9, "grep")]


def test_a_gitlab_before_script_pipefail_covers_the_script() -> None:
    ci = (
        "job:\n"
        "  before_script:\n"
        "    - set -euo pipefail\n"
        "  script:\n"
        '    - v="$(seq 6 | head -1 || true)"\n'
        "    - seq 9 | grep -q 9\n"
        "plain:\n"
        "  script:\n"
        "    - seq 9 | grep -q 9 && echo found\n"
    )
    findings = gate.check_ci_file(".gitlab-ci.yml", ci)
    assert [(f.line, f.consumer) for f in findings] == [(6, "grep")]


def test_unparsable_ci_yaml_is_an_environment_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _git_repo(tmp_path, {"a.sh": "#!/bin/sh\n", ".gitlab-ci.yml": "a: [\n"})
    rc, _ = _check(root, capsys)
    assert rc == 2


def test_an_empty_shell_set_verified_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _check(_git_repo(tmp_path, {"README.md": "x\n"}), capsys)
    assert rc == gate.SKIP_EXIT_STATUS == 3
    assert "[SKIP]" in out


def test_a_shebang_file_without_a_suffix_is_in_scope(tmp_path: Path) -> None:
    root = _git_repo(
        tmp_path, {"hooks/pre": "#!/usr/bin/env bash\nset -o pipefail\nseq 9 | head -1\n"}
    )
    scope, findings = gate.run_check(root)
    assert scope.shell == ["hooks/pre"]
    assert [(f.path, f.line) for f in findings] == [("hooks/pre", 3)]


def test_a_directory_that_is_not_a_git_work_tree_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _check(tmp_path, capsys)
    assert rc == 2


def test_the_gate_is_registered_in_run_gates() -> None:
    spec = importlib.util.spec_from_file_location(
        "run_gates_for_sigpipe", REPO_ROOT / "scripts" / "run_gates.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_gates_for_sigpipe"] = module
    spec.loader.exec_module(module)
    argv = dict(module.GATES)["check_pipefail_sigpipe"]
    assert argv[1:] == ["scripts/check_pipefail_sigpipe.py"]
