# SPDX-License-Identifier: MIT
"""Tests for the guards `scripts/deploy.sh` runs before `terraform apply`.

Most assertions read the script text and check that each guard is present, runs before the apply,
and `die`s on failure:

* ``scripts/check_relay_zip.py --require-zip`` — the zip is the one the current `src/relay/` tree
  produces.
* ``scripts/package_relay.sh --verify-only`` — the handler imports from the zip.

The guards' own behaviour is tested in `tests/unit/test_check_relay_zip.py` and
`tests/unit/test_package_relay.py`.

The git-index classes run the script in a sandbox whose `PATH` holds symlinks to git and a few
coreutils and to neither `aws` nor `terraform`, against throwaway git repositories under `tmp_path`.
The script either refuses on the index or dies on `command -v aws`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, NamedTuple

import pytest
from scripts import proc_run
from scripts.render_vpc_tfvars import OUTPUT_TO_TFVARS

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"

#: The gates, by the substring of their real invocation; the bare script names also appear in
#: comments.
RELAY_CURRENCY_INVOCATION = "scripts/check_relay_zip.py --require-zip"
RELAY_IMPORT_INVOCATION = "bash scripts/package_relay.sh --verify-only"
PYTHON_ZIP_INVOCATION = "scripts/check_lambda_zip_imports.py --require-zips"
#: Anchored on the interpreter variable; the gate's bare name also appears in comments and messages.
EXPOSURE_INVOCATION = '"$EXPOSURE_CHECK_PYTHON" scripts/check_no_public_exposure.py'
PREFLIGHT_INVOCATION = 'bash scripts/preflight_adopted_vpc.sh "${PREFLIGHT_ARGS[@]}"'
TERRAFORM_APPLY = 'terraform apply "${TF_APPLY_ARGS[@]}"'
TERRAFORM_INIT = 'terraform init "${TF_INIT_ARGS[@]}"'

#: The index read, pinned to `$INDEX_ROOT`: the work-tree root git resolves from `$REPO_ROOT`.
UNMERGED_INVOCATION = 'git -C "$INDEX_ROOT" ls-files --unmerged'

#: The probe that decides whether an index exists and which one, pinned to `$REPO_ROOT`.
WORKTREE_PROBE = 'git -C "$REPO_ROOT" rev-parse --show-toplevel --show-prefix'

#: The probe prints the root then the prefix, one per line, so a second line means `$REPO_ROOT` sits
#: inside a work tree but not at its top. Raw string: the script holds `\n` inside `$'...'`.
PREFIX_PREDICATE: Final[str] = r"""if [[ "$WORKTREE_INFO" == *$'\n'* ]]; then"""

#: A mutant predicate comparing git's toplevel against the `$REPO_ROOT` spelling.
PATH_COMPARISON: Final[str] = r"""if [[ "${WORKTREE_INFO%%$'\n'*}" != "$REPO_ROOT" ]]; then"""

#: Every guard's failure path says this.
NO_HARM_PHRASE = "nothing was applied"


def _script() -> str:
    return DEPLOY_SH.read_text(encoding="utf-8")


def _index(script: str, needle: str) -> int:
    position = script.find(needle)
    assert position != -1, f"{needle!r} does not appear in scripts/deploy.sh"
    return position


def _sandboxed_syntax_check(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path, str]:
    """`bash -n` a copy of the deploy script under `tmp_path`, with a `PATH` holding neither `aws`
    nor `terraform`. Returns `(result, script_copy, path_env)`.
    """
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script_copy = sandbox / "deploy.sh"
    script_copy.write_text(_script(), encoding="utf-8")
    # `bash` itself must remain reachable; nothing else needs to be.
    path_env = f"{bin_dir}:/bin:/usr/bin"
    result = proc_run.run(
        ["bash", "-n", str(script_copy)],
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": path_env},
    )
    return result, script_copy, path_env


def test_the_script_is_syntactically_valid(tmp_path: Path) -> None:
    """The script parses under `bash -n`."""
    result, _script_copy, _path_env = _sandboxed_syntax_check(tmp_path)
    assert result.returncode == 0, result.stderr


class TestTheSyntaxCheckIsGenuinelySandboxed:
    """Each layer of the syntax check's sandbox holds, asserted by behaviour."""

    def test_it_parses_a_copy_and_never_the_repository_script(self, tmp_path: Path) -> None:
        _result, script_copy, _path_env = _sandboxed_syntax_check(tmp_path)
        assert script_copy.is_file()
        assert tmp_path in script_copy.parents, script_copy
        assert script_copy != DEPLOY_SH
        # The copy must still be the real content, or the syntax check checks nothing.
        assert script_copy.read_text(encoding="utf-8") == _script()

    def test_the_sandbox_path_cannot_reach_aws_or_terraform(self, tmp_path: Path) -> None:
        """Neither `aws` nor `terraform` is reachable from the sandbox PATH."""
        _result, _script_copy, path_env = _sandboxed_syntax_check(tmp_path)
        for executable in ("aws", "terraform"):
            assert shutil.which(executable, path=path_env) is None, (
                f"{executable} is reachable from the sandbox PATH {path_env!r}"
            )

    def test_the_check_is_parse_only(self) -> None:
        """The syntax check's call site passes `-n`."""
        source = Path(__file__).read_text(encoding="utf-8")
        call = source[source.index("result = proc_run.run(") :]
        call = call[: call.index(")\n")]
        assert '"-n"' in call, (
            "the syntax check no longer passes -n, so bash would EXECUTE the script"
        )

    def test_a_syntax_error_is_actually_detected(self, tmp_path: Path) -> None:
        """`bash -n` fails on a script with a syntax error."""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        broken = sandbox / "broken.sh"
        broken.write_text("if true; then\n  echo unterminated\n", encoding="utf-8")
        result = proc_run.run(
            ["bash", "-n", str(broken)], cwd=sandbox, capture_output=True, text=True, check=False
        )
        assert result.returncode != 0, "bash -n accepted an unterminated `if`"


def test_both_relay_guards_are_wired_in() -> None:
    script = _script()
    assert RELAY_CURRENCY_INVOCATION in script
    assert RELAY_IMPORT_INVOCATION in script


def test_the_relay_guards_run_before_terraform_apply() -> None:
    """Both relay guards run before `terraform apply`."""
    script = _script()
    apply_at = _index(script, TERRAFORM_APPLY)
    assert _index(script, RELAY_CURRENCY_INVOCATION) < apply_at
    assert _index(script, RELAY_IMPORT_INVOCATION) < apply_at


def test_currency_is_checked_before_importability() -> None:
    """The relay currency check runs before the import check."""
    script = _script()
    assert _index(script, RELAY_CURRENCY_INVOCATION) < _index(script, RELAY_IMPORT_INVOCATION)


def test_the_relay_guards_sit_alongside_the_python_zip_guard() -> None:
    """The relay guards follow the Python zip guard and precede `terraform init`."""
    script = _script()
    assert _index(script, PYTHON_ZIP_INVOCATION) < _index(script, RELAY_CURRENCY_INVOCATION)
    assert _index(script, RELAY_CURRENCY_INVOCATION) < _index(script, TERRAFORM_INIT)


def test_a_failing_relay_guard_aborts_the_deploy() -> None:
    """Each guard's failure path must `die`, and must say the apply did not happen."""
    script = _script()
    for gate in (RELAY_CURRENCY_INVOCATION, RELAY_IMPORT_INVOCATION):
        start = _index(script, gate)
        tail = script[start : start + 1400]
        assert "die " in tail, gate
        assert NO_HARM_PHRASE in tail, gate


class TestPublicExposureGuard:
    """An apply is refused while any resource is internet-facing."""

    def test_it_is_wired_in(self) -> None:
        assert EXPOSURE_INVOCATION in _script()

    def test_it_runs_before_terraform_apply(self) -> None:
        script = _script()
        assert _index(script, EXPOSURE_INVOCATION) < _index(script, TERRAFORM_APPLY)

    def test_it_runs_before_the_zip_guards(self) -> None:
        """The exposure guard runs before the zip guards."""
        script = _script()
        assert _index(script, EXPOSURE_INVOCATION) < _index(script, PYTHON_ZIP_INVOCATION)

    def test_a_violation_aborts_the_deploy(self) -> None:
        script = _script()
        tail = script[_index(script, EXPOSURE_INVOCATION) :][:1600]
        assert "die " in tail
        assert NO_HARM_PHRASE in tail

    def test_verifying_nothing_is_treated_as_a_failure(self) -> None:
        """The exposure gate's exit 3 `die`s with its own message."""
        script = _script()
        tail = script[_index(script, EXPOSURE_INVOCATION) :][:1600]
        assert "3)" in tail
        assert "verified nothing" in tail

    def test_the_refusal_states_there_is_no_waiver(self) -> None:
        """The exposure refusal states that there is no waiver."""
        script = _script()
        tail = script[_index(script, EXPOSURE_INVOCATION) :][:1600]
        assert "no waiver" in tail


def test_the_guards_name_the_command_that_fixes_the_failure() -> None:
    """Each relay guard's failure path names `scripts/package_relay.sh`."""
    script = _script()
    for gate in (RELAY_CURRENCY_INVOCATION, RELAY_IMPORT_INVOCATION):
        start = _index(script, gate)
        assert "scripts/package_relay.sh" in script[start : start + 1400], gate


def test_the_currency_gates_skip_status_is_treated_as_a_failure() -> None:
    """The currency gate's exit 3 is captured and `die`s with its own message."""
    script = _script()
    window = script[
        _index(script, RELAY_CURRENCY_INVOCATION) : _index(script, RELAY_IMPORT_INVOCATION)
    ]
    assert "RELAY_ZIP_CHECK_STATUS=$?" in window, (
        "the gate's exit status is not captured, so exit 3 cannot be distinguished from exit 1"
    )
    assert re.search(r"^\s*3\)", window, re.MULTILINE), "no dedicated branch for exit 3"
    assert "verified nothing" in window
    assert window.count("die ") >= 2, (
        "exit 3 and exit 1 must not share one message; they are different operator situations"
    )


def test_the_import_guard_is_given_an_explicit_zip_path() -> None:
    """The import guard receives `"$RELAY_ZIP"`, which honours `LAMBDA_BUILD_DIR`."""
    script = _script()
    start = _index(script, RELAY_IMPORT_INVOCATION)
    assert '"$RELAY_ZIP"' in script[start : start + 200]
    assignment = re.search(r'^RELAY_ZIP="([^"]+)"', script, re.MULTILINE)
    assert assignment is not None, "RELAY_ZIP is never assigned"
    assert assignment.group(1).endswith("relay.zip")
    assert "LAMBDA_BUILD_DIR" in script[assignment.start() : assignment.start() + 400], (
        "RELAY_ZIP ignores LAMBDA_BUILD_DIR, so an operator who overrode the build directory for "
        "packaging would have a different zip checked than the one they built"
    )


def test_the_checked_zip_is_the_one_terraform_reads() -> None:
    """`RELAY_ZIP` resolves under `infra/build/lambda/`, where Terraform reads it."""
    script = _script()
    assignment = re.search(r'^RELAY_ZIP="([^"]+)"', script, re.MULTILINE)
    assert assignment is not None
    assert "/infra/build/lambda/relay.zip" in assignment.group(1), assignment.group(1)


def test_the_currency_guard_runs_under_the_project_interpreter() -> None:
    """The currency guard runs under the same `$ZIP_CHECK_PYTHON` as the Python zip gate."""
    script = _script()
    start = _index(script, RELAY_CURRENCY_INVOCATION)
    assert '"$ZIP_CHECK_PYTHON"' in script[max(0, start - 200) : start]


class TestAdoptedVpcPreflightGuard:
    """`deploy.sh` invokes the adopted-VPC preflight before the apply and stops on its refusal."""

    def test_it_is_wired_in(self) -> None:
        assert PREFLIGHT_INVOCATION in _script(), (
            "deploy.sh does not invoke scripts/preflight_adopted_vpc.sh, so its route-table checks "
            "and default-SG check do not run before an apply"
        )

    def test_it_runs_before_terraform_apply(self) -> None:
        script = _script()
        assert _index(script, PREFLIGHT_INVOCATION) < _index(script, TERRAFORM_APPLY)

    def test_it_runs_before_the_zip_guards(self) -> None:
        """The preflight runs before the zip guards."""
        script = _script()
        assert _index(script, PREFLIGHT_INVOCATION) < _index(script, PYTHON_ZIP_INVOCATION)

    def test_a_refusal_aborts_the_deploy(self) -> None:
        script = _script()
        after = script[_index(script, PREFLIGHT_INVOCATION) :]
        assert "|| die" in after[:400], (
            "the preflight's exit status is not checked, so a refusal would not stop the apply"
        )
        assert "Nothing was applied" in after[:1200]

    def test_a_missing_tfvars_refuses_rather_than_planning_without_the_ids(self) -> None:
        """An absent tfvars file is refused before the apply."""
        script = _script()
        assert "no ${TFVARS}" in script
        assert "tracked content must not carry them" in script

    def test_placeholder_ids_are_refused(self) -> None:
        """A tfvars still carrying the example's placeholder ids is refused."""
        assert "still carries the placeholder ids" in _script()

    def test_an_empty_parsed_id_refuses_rather_than_examining_nothing(self) -> None:
        """An empty parsed id list is refused rather than passed to the preflight."""
        script = _script()
        assert "would then examine nothing and report OK" in script

    @pytest.mark.parametrize(
        "variable",
        [
            "vpc_id",
            "vpc_private_subnet_ids",
            "vpc_private_route_table_ids",
            "vpc_nat_public_subnet_id",
            "vpc_runtime_subnet_ids",
            "vpc_runtime_route_table_ids",
            "api_origin_ingress_security_group_ids",
        ],
    )
    def test_every_adopted_variable_is_read_from_tfvars(self, variable: str) -> None:
        assert f"tfvars_value {variable}" in _script(), (
            f"{variable} is never read, so the preflight is invoked without it"
        )

    def test_the_region_and_profile_are_passed_explicitly(self) -> None:
        """The preflight receives `--region` and `--profile` explicitly."""
        script = _script()
        after = script[_index(script, "PREFLIGHT_ARGS=(") :][:400]
        assert "--region" in after and '"$RESOLVED_REGION"' in after
        assert "--profile" in after and '"$RESOLVED_PROFILE"' in after


# ---------------------------------------------------------------------------
# The adopted-VPC tfvars block, driven rather than read.
# ---------------------------------------------------------------------------

#: The first and last statements of the shipped block that reads the tfvars and calls the preflight.
TFVARS_BLOCK_START: Final[str] = 'TFVARS="${INFRA_DIR}/terraform.tfvars"'
TFVARS_BLOCK_END: Final[str] = PREFLIGHT_INVOCATION

#: A fully filled tfvars, keyed by variable; lists render as multi-line HCL lists.
FILLED_TFVARS: Final[dict[str, str | list[str]]] = {
    "vpc_id": "vpc-0test",
    "vpc_private_subnet_ids": ["subnet-0p1", "subnet-0p2"],
    "vpc_nat_public_subnet_id": "subnet-0nat",
    "vpc_private_route_table_ids": ["rtb-0p1", "rtb-0p2"],
    "vpc_runtime_subnet_ids": ["subnet-0r1", "subnet-0r2"],
    "vpc_runtime_route_table_ids": ["rtb-0r1"],
    "vpc_nat_gateway_enabled": "true",
    "api_origin_ingress_security_group_ids": ["sg-0cf1"],
}

#: Every key the placeholder refusal covers.
G1_PLACEHOLDER_KEYS: Final[tuple[str, ...]] = (
    "vpc_id",
    "vpc_private_subnet_ids",
    "vpc_private_route_table_ids",
    "api_origin_ingress_security_group_ids",
    "vpc_runtime_subnet_ids",
    "vpc_runtime_route_table_ids",
    "vpc_nat_public_subnet_id",
)

PLACEHOLDER_LOOP_HEADER: Final = "for pair in "
PLACEHOLDER_LOOP_TEST: Final = '    if [[ "${pair#*:}" == *REPLACE_ME* ]]; then'
PLACEHOLDER_PAIR: Final = re.compile(r'"([a-z_]+):\$\{PF_[A-Z_]+\}"')


def _placeholder_loop_keys(block: str) -> list[str] | None:
    """Return the keys of the `for pair in ...; do` loop whose body tests for `REPLACE_ME`.

    The header may span backslash-continued lines and must hold only `"key:${PF_X}"` pairs; returns
    None when no such loop is present.
    """
    lines = block.split("\n")
    for start, line in enumerate(lines):
        if not line.startswith(PLACEHOLDER_LOOP_HEADER):
            continue
        header = line[len(PLACEHOLDER_LOOP_HEADER) :]
        end = start
        while header.endswith("\\") and end + 1 < len(lines):
            end += 1
            header = header[:-1] + " " + lines[end]
        if not header.endswith("; do") or end + 1 >= len(lines):
            continue
        if lines[end + 1] != PLACEHOLDER_LOOP_TEST:
            continue
        tokens = header[: -len("; do")].split()
        matches = [PLACEHOLDER_PAIR.fullmatch(token) for token in tokens]
        if not tokens or any(match is None for match in matches):
            continue
        return [match.group(1) for match in matches if match is not None]
    return None


PLACEHOLDER_REFUSAL = "still carries the placeholder ids"
EMPTY_REFUSAL = "sets no usable"


def _render_tfvars(values: Mapping[str, str | list[str]]) -> str:
    lines: list[str] = []
    for key, value in values.items():
        if isinstance(value, list):
            lines.append(f"{key} = [")
            lines.extend(f'  "{item}",' for item in value)
            lines.append("]")
        elif value in ("true", "false"):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f'{key} = "{value}"')
    return "\n".join(lines) + "\n"


def _tfvars_block() -> str:
    """The shipped tfvars-to-preflight block of `deploy.sh`, verbatim through the preflight's `|| die`."""
    script = _script()
    start = _index(script, TFVARS_BLOCK_START)
    end = script.index("\n\n", _index(script, TFVARS_BLOCK_END))
    return script[start:end]


class _PreflightRun(NamedTuple):
    result: subprocess.CompletedProcess[str]
    argv: list[str] | None


def _run_tfvars_block(tmp_path: Path, tfvars_text: str) -> _PreflightRun:
    """Run the shipped block against `tfvars_text` with a recording stub as the preflight.

    `argv` is the preflight's argument list, or `None` when the block refused before invoking it.
    """
    root = tmp_path / "repo"
    (root / "infra").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "infra" / "terraform.tfvars").write_text(tfvars_text, encoding="utf-8")
    argv_file = tmp_path / "preflight.argv"
    (root / "scripts" / "preflight_adopted_vpc.sh").write_text(
        f'printf \'%s\\n\' "$@" > "{argv_file}"\n', encoding="utf-8"
    )
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        "info() { :; }\n"
        "warn() { :; }\n"
        "die() { printf '%s\\n' \"$1\" >&2; exit 1; }\n"
        f'REPO_ROOT="{root}"\n'
        'INFRA_DIR="${REPO_ROOT}/infra"\n'
        "RESOLVED_REGION=us-east-1\n"
        "RESOLVED_PROFILE=p2-test\n" + _tfvars_block() + "\n",
        encoding="utf-8",
    )
    interpreter = shutil.which("bash")
    assert interpreter is not None, "bash is not on the host PATH"
    result = proc_run.run(
        [interpreter, str(harness)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    argv = argv_file.read_text(encoding="utf-8").splitlines() if argv_file.exists() else None
    return _PreflightRun(result=result, argv=argv)


class TestAdoptedVpcTfvarsBlockDriven:
    """The tfvars block refuses placeholders and an empty Runtime list, and passes the Runtime ids."""

    def test_the_block_is_the_tfvars_read_through_the_preflight_call(self) -> None:
        block = _tfvars_block()
        assert block.startswith(TFVARS_BLOCK_START)
        assert PREFLIGHT_INVOCATION in block and "|| die" in block
        assert "terraform " not in block.replace("terraform.tfvars", "")

    def test_a_filled_tfvars_reaches_the_preflight_with_every_id_flag(self, tmp_path: Path) -> None:
        run = _run_tfvars_block(tmp_path, _render_tfvars(FILLED_TFVARS))
        assert run.result.returncode == 0, run.result.stderr
        assert run.argv == [
            "--region",
            "us-east-1",
            "--vpc-id",
            "vpc-0test",
            "--private-subnet-ids",
            "subnet-0p1,subnet-0p2",
            "--route-table-ids",
            "rtb-0p1,rtb-0p2",
            "--runtime-subnet-ids",
            "subnet-0r1,subnet-0r2",
            "--profile",
            "p2-test",
            "--nat-subnet-id",
            "subnet-0nat",
            "--runtime-route-table-ids",
            "rtb-0r1",
        ]

    def test_the_placeholder_cells_cover_exactly_the_keys_the_shipped_loop_refuses(self) -> None:
        shipped = _placeholder_loop_keys(_tfvars_block())
        assert shipped is not None, "the placeholder loop is not in the shipped tfvars block"
        assert sorted(shipped) == sorted(G1_PLACEHOLDER_KEYS)

    def test_the_placeholder_loop_parser_rejects_an_adversarial_near_miss_in_bounded_time(
        self,
    ) -> None:
        """A long run of whitespace-padded pairs with no terminator is rejected, and fast."""
        near_miss = "for pair in " + '"vpc_id:${PF_VPC_ID}"   ' * 40 + "; done\n"
        started = time.monotonic()
        assert _placeholder_loop_keys(near_miss) is None
        assert time.monotonic() - started < 1.0

    @pytest.mark.parametrize(
        "loop",
        [
            'for pair in "vpc_id:${PF_VPC_ID}"; do\n    if [[ -z "${pair#*:}" ]]; then',
            'for pair in "vpc_id:${PF_VPC_ID}" stray; do\n' + PLACEHOLDER_LOOP_TEST,
            "for pair in ; do\n" + PLACEHOLDER_LOOP_TEST,
        ],
        ids=["other-body", "non-pair-token", "no-pairs"],
    )
    def test_the_placeholder_loop_parser_rejects_a_loop_that_is_not_the_refusal(
        self, loop: str
    ) -> None:
        assert _placeholder_loop_keys(loop) is None

    def test_the_placeholder_loop_parser_reads_a_continued_header(self) -> None:
        loop = (
            'for pair in "vpc_id:${PF_VPC_ID}" \\\n'
            '            "vpc_nat_public_subnet_id:${PF_NAT_SUBNET}"; do\n' + PLACEHOLDER_LOOP_TEST
        )
        assert _placeholder_loop_keys(loop) == ["vpc_id", "vpc_nat_public_subnet_id"]

    @pytest.mark.parametrize("key", G1_PLACEHOLDER_KEYS)
    def test_a_placeholder_in_the_key_refuses_before_the_preflight(
        self, tmp_path: Path, key: str
    ) -> None:
        values = dict(FILLED_TFVARS)
        current = values[key]
        if isinstance(current, list):
            values[key] = [*current[:-1], "id-REPLACE_ME_X"]
        else:
            values[key] = "id-REPLACE_ME_X"
        run = _run_tfvars_block(tmp_path, _render_tfvars(values))
        assert run.result.returncode == 1
        assert PLACEHOLDER_REFUSAL in run.result.stderr
        assert f"in {key}." in run.result.stderr
        assert NO_HARM_PHRASE in run.result.stderr
        assert run.argv is None, "the preflight ran on a placeholder id"

    def test_the_unedited_example_refuses_before_the_preflight(self, tmp_path: Path) -> None:
        example = (REPO_ROOT / "infra" / "terraform.tfvars.example").read_text(encoding="utf-8")
        run = _run_tfvars_block(tmp_path, example)
        assert run.result.returncode == 1
        assert PLACEHOLDER_REFUSAL in run.result.stderr
        assert run.argv is None

    def test_an_empty_runtime_subnet_list_refuses(self, tmp_path: Path) -> None:
        values = {**FILLED_TFVARS, "vpc_runtime_subnet_ids": []}
        run = _run_tfvars_block(tmp_path, _render_tfvars(values))
        assert run.result.returncode == 1
        assert f"{EMPTY_REFUSAL} vpc_runtime_subnet_ids" in run.result.stderr
        assert NO_HARM_PHRASE in run.result.stderr
        assert run.argv is None

    def test_an_absent_runtime_subnet_key_refuses(self, tmp_path: Path) -> None:
        values = {k: v for k, v in FILLED_TFVARS.items() if k != "vpc_runtime_subnet_ids"}
        run = _run_tfvars_block(tmp_path, _render_tfvars(values))
        assert run.result.returncode == 1
        assert f"{EMPTY_REFUSAL} vpc_runtime_subnet_ids" in run.result.stderr
        assert run.argv is None

    def test_an_empty_runtime_route_table_list_omits_its_flag(self, tmp_path: Path) -> None:
        values = {**FILLED_TFVARS, "vpc_runtime_route_table_ids": []}
        run = _run_tfvars_block(tmp_path, _render_tfvars(values))
        assert run.result.returncode == 0, run.result.stderr
        assert run.argv is not None
        assert "--runtime-route-table-ids" not in run.argv
        assert run.argv[run.argv.index("--runtime-subnet-ids") + 1] == "subnet-0r1,subnet-0r2"


# ---------------------------------------------------------------------------
# `render_vpc_tfvars.py` output -> the shipped tfvars block -> the preflight argv.
# ---------------------------------------------------------------------------

RENDERER = REPO_ROOT / "scripts" / "render_vpc_tfvars.py"
TFVARS_EXAMPLE = REPO_ROOT / "infra" / "terraform.tfvars.example"

#: Bootstrap-root output name -> whether it is a list, for the outputs the renderer reads.
RENDERER_OUTPUTS: Final[dict[str, tuple[str, bool]]] = {
    "vpc_id": ("vpc", False),
    "private_subnet_ids": ("subnet", True),
    "private_route_table_ids": ("rtb", True),
    "nat_public_subnet_id": ("subnet", False),
    "runtime_subnet_ids": ("subnet", True),
    "runtime_route_table_ids": ("rtb", True),
}

#: Preflight flag -> the bootstrap output whose value it must carry, in deploy.sh's order. Keyed by
#: output name rather than through the renderer's own mapping, so a renderer that writes an output
#: to the wrong key reds the cell.
PREFLIGHT_FLAG_OUTPUTS: Final[tuple[tuple[str, str], ...]] = (
    ("--vpc-id", "vpc_id"),
    ("--private-subnet-ids", "private_subnet_ids"),
    ("--route-table-ids", "private_route_table_ids"),
    ("--runtime-subnet-ids", "runtime_subnet_ids"),
)
PREFLIGHT_OPTIONAL_FLAG_OUTPUTS: Final[tuple[tuple[str, str], ...]] = (
    ("--nat-subnet-id", "nat_public_subnet_id"),
    ("--runtime-route-table-ids", "runtime_route_table_ids"),
)

SG_KEY: Final = "api_origin_ingress_security_group_ids"


def _fake_id(prefix: str, tag: str, ordinal: int) -> str:
    """A 17-hex-digit id with eight leading zeros, built at runtime."""
    return f"{prefix}-{'0' * 8}{tag * 8}{ordinal}"


def _bootstrap_outputs() -> dict[str, dict[str, object]]:
    """A `terraform output -json`-shaped document for the bootstrap root, two ids per list."""
    document: dict[str, dict[str, object]] = {}
    for index, (output, (prefix, is_list)) in enumerate(RENDERER_OUTPUTS.items()):
        tag = "abcdef"[index]
        value: object = (
            [_fake_id(prefix, tag, 1), _fake_id(prefix, tag, 2)]
            if is_list
            else _fake_id(prefix, tag, 1)
        )
        document[output] = {
            "value": value,
            "type": ["list", "string"] if is_list else "string",
            "sensitive": False,
        }
    return document


def _render_with_the_renderer(tmp_path: Path, document: Mapping[str, object]) -> str:
    """Run the renderer CLI on `document` from the shipped example, then fill the SG placeholder."""
    outputs_path = tmp_path / "bootstrap-outputs.json"
    outputs_path.write_text(json.dumps(document), encoding="utf-8")
    tfvars = tmp_path / "rendered.tfvars"
    result = proc_run.run(
        [
            sys.executable,
            str(RENDERER),
            "--outputs-json",
            str(outputs_path),
            "--tfvars",
            str(tfvars),
            "--example",
            str(TFVARS_EXAMPLE),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert result.returncode == 0, result.stderr
    text = tfvars.read_text(encoding="utf-8")
    filled, count = re.subn(
        rf"^{SG_KEY}\s*=.*$",
        f'{SG_KEY} = ["{_fake_id("sg", "9", 1)}"]',
        text,
        flags=re.MULTILINE,
    )
    assert count == 1, f"the rendered tfvars does not assign {SG_KEY} exactly once"
    return filled


def _expected_preflight_argv(document: Mapping[str, Mapping[str, object]]) -> list[str]:
    """The preflight argv deploy.sh should pass, derived from the outputs document."""

    def joined(output: str) -> str:
        value = document[output]["value"]
        return ",".join(value) if isinstance(value, list) else str(value)

    argv = ["--region", "us-east-1"]
    for flag, output in PREFLIGHT_FLAG_OUTPUTS:
        argv += [flag, joined(output)]
    argv += ["--profile", "p2-test"]
    for flag, output in PREFLIGHT_OPTIONAL_FLAG_OUTPUTS:
        argv += [flag, joined(output)]
    return argv


class TestRendererOutputReachesThePreflight:
    """What the renderer writes is what deploy.sh's tfvars block hands the preflight."""

    def test_the_fixture_covers_every_output_the_renderer_maps(self) -> None:
        assert set(RENDERER_OUTPUTS) == set(OUTPUT_TO_TFVARS)
        flagged = {output for _flag, output in PREFLIGHT_FLAG_OUTPUTS}
        flagged |= {output for _flag, output in PREFLIGHT_OPTIONAL_FLAG_OUTPUTS}
        assert flagged == set(OUTPUT_TO_TFVARS)

    def test_rendered_tfvars_reach_the_preflight_as_the_outputs_document_says(
        self, tmp_path: Path
    ) -> None:
        document = _bootstrap_outputs()
        rendered = _render_with_the_renderer(tmp_path, document)
        run = _run_tfvars_block(tmp_path, rendered)
        assert run.result.returncode == 0, run.result.stderr
        assert run.argv == _expected_preflight_argv(document)


# ---------------------------------------------------------------------------
# The unmerged-index refusal, driven rather than read.
# ---------------------------------------------------------------------------

#: The refusal under test, by a substring of its message.
UNMERGED_REFUSAL = "holds unmerged path(s)"
#: The two unknown-answer refusals in the same block.
UNREADABLE_INDEX_REFUSAL = "could not read the git index"
GIT_ABSENT_REFUSAL = "git not found on PATH"
#: The two non-refusing dispositions, each by a substring only it produces. Git resolved no work tree
#: at all in the first; in the second it resolved one whose root is not `$REPO_ROOT`.
NO_WORKTREE_WARNING = "resolved no work tree from"
NESTED_DISCLOSURE = "is not a checkout root of its own"
#: The remedy clause that is true of every state reaching the refusal.
ALWAYS_APPLICABLE_REMEDY = "the remedy that always applies is to resolve the listed path(s)"
#: The next `die` after the index guard, so its presence shows a run got past that guard.
PROGRESS_MARKER = "aws CLI not found on PATH"

#: The executables the script reaches before its first refusal, plus git, symlinked individually so
#: no system directory (which may hold `aws`) is on the sandbox PATH.
SANDBOX_BINARIES: Final[tuple[str, ...]] = (
    "git",
    "date",
    "tee",
    "cut",
    "sort",
    "tr",
    "cat",
    "dirname",
)

#: Every git operation that can leave an index unmerged.
CONFLICTING_OPERATIONS: Final[tuple[str, ...]] = (
    "merge",
    "rebase",
    "cherry-pick",
    "revert",
    "stash-apply",
    "am-3",
)

_Git = Callable[..., "subprocess.CompletedProcess[str]"]


class _Repo(NamedTuple):
    root: Path
    git: _Git
    branch: str


def _sandbox_bin(directory: Path, *, include_git: bool = True) -> Path:
    """A `PATH` holding only what the script needs before its first refusal. Never `aws`/`terraform`."""
    directory.mkdir(parents=True)
    for name in SANDBOX_BINARIES:
        if name == "git" and not include_git:
            continue
        real = shutil.which(name)
        assert real is not None, f"{name} is not on the host PATH, so the sandbox cannot be built"
        (directory / name).symlink_to(real)
    return directory


def _make_repo(root: Path, *, script_text: str | None = None) -> _Repo:
    """A throwaway git repo holding `scripts/deploy.sh`, so the script's own `REPO_ROOT` is this repo."""
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "deploy.sh").write_text(
        _script() if script_text is None else script_text, encoding="utf-8"
    )

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return proc_run.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)

    git("init", "-q", ".")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (root / "f.py").write_text("V = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    # Read rather than assumed: `init.defaultBranch` varies by host.
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return _Repo(root=root, git=git, branch=branch)


def _leave_index_unmerged(repo: _Repo, operation: str, scratch: Path) -> None:
    """Put `repo`'s index into an unmerged state by running the real git `operation`."""
    git, branch = repo.git, repo.branch
    target = repo.root / "f.py"

    def commit(text: str, message: str) -> None:
        target.write_text(text, encoding="utf-8")
        git("commit", "-qam", message)

    if operation == "merge":
        git("checkout", "-q", "-b", "theirs")
        commit("V = 3\n", "theirs")
        git("checkout", "-q", branch)
        commit("V = 2\n", "ours")
        git("merge", "theirs", check=False)
    elif operation == "rebase":
        git("checkout", "-q", "-b", "topic")
        commit("V = 3\n", "topic")
        git("checkout", "-q", branch)
        commit("V = 2\n", "diverged")
        git("checkout", "-q", "topic")
        git("rebase", branch, check=False)
    elif operation == "cherry-pick":
        git("checkout", "-q", "-b", "topic")
        commit("V = 3\n", "topic")
        picked = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "-q", branch)
        commit("V = 2\n", "diverged")
        git("cherry-pick", picked, check=False)
    elif operation == "revert":
        commit("V = 2\n", "second")
        reverted = git("rev-parse", "HEAD").stdout.strip()
        commit("V = 3\n", "third")
        git("revert", "--no-edit", reverted, check=False)
    elif operation == "stash-apply":
        target.write_text("V = 2\n", encoding="utf-8")
        git("stash", "-q")
        commit("V = 3\n", "third")
        git("stash", "apply", check=False)
    elif operation == "am-3":
        patches = scratch / "patches"
        git("checkout", "-q", "-b", "topic")
        commit("V = 3\n", "topic")
        git("format-patch", "-1", "-q", "-o", str(patches))
        git("checkout", "-q", branch)
        commit("V = 2\n", "diverged")
        git("am", "-3", *sorted(str(p) for p in patches.glob("*.patch")), check=False)
    else:  # pragma: no cover
        raise AssertionError(f"unknown operation {operation!r}")

    # The fixture's positive control: the operation must have left the index unmerged.
    unmerged = git("ls-files", "--unmerged").stdout
    assert unmerged.strip(), f"the {operation} fixture left the index clean, so it proves nothing"


def _make_nested_repo(
    root: Path, *, operation: str | None, scratch: Path, script_text: str | None = None
) -> Path:
    """A checkout at `root` whose subdirectory holds `scripts/deploy.sh` and has no `.git` of its own.

    Returns the inner directory, which `deploy.sh` resolves as its `REPO_ROOT`. `operation` is the git
    operation that leaves the outer index unmerged, or `None` for a clean parent; the conflicting path
    is outside the subtree.
    """
    repo = _make_repo(root)
    if operation is not None:
        _leave_index_unmerged(repo, operation, scratch)
    inner = root / "vendored"
    (inner / "scripts").mkdir(parents=True)
    (inner / "scripts" / "deploy.sh").write_text(
        _script() if script_text is None else script_text, encoding="utf-8"
    )

    # The fixture's controls: the subtree has no `.git` and resolves to the outer root at a non-empty
    # prefix.
    assert not (inner / ".git").exists(), (
        "the subtree has its own .git, so this is not the nested cell"
    )
    resolved = repo.git("-C", str(inner), "rev-parse", "--show-toplevel", "--show-prefix")
    lines = resolved.stdout.splitlines()
    assert len(lines) == 2 and lines[1], (
        f"{inner} did not resolve at a prefix inside {root}: {lines}"
    )
    assert Path(lines[0]).resolve() == root.resolve(), f"{inner} resolved to {lines[0]}, not {root}"
    if operation is not None:
        assert not repo.git("-C", str(inner), "ls-files", "--unmerged").stdout.strip(), (
            "the conflict is visible from the subtree, so a prefix-scoped question would have found "
            "it and this fixture proves nothing about prefix scoping"
        )
    return inner


def _repo_addressed_through_a_symlink(
    base: Path, *, script_text: str | None = None
) -> tuple[Path, Path]:
    """A flat checkout plus a second, symlink-bearing spelling of it. Returns `(logical, physical)`.

    `logical` is what `deploy.sh` resolves as its `$REPO_ROOT` when invoked through it (`cd`+`pwd`
    keeps the symlinked component); git answers with `physical`.
    """
    physical_parent = base / "physical"
    physical_parent.mkdir(parents=True)
    repo = _make_repo(physical_parent / "checkout", script_text=script_text)
    (base / "link").symlink_to(physical_parent, target_is_directory=True)
    logical = base / "link" / repo.root.name

    # The fixture's controls: two spellings of one directory, and git answers the physical one.
    assert str(logical) != str(repo.root)
    assert logical.samefile(repo.root), "the two spellings must name one directory"
    named = repo.git("-C", str(logical), "rev-parse", "--show-toplevel").stdout.strip()
    assert named != str(logical), f"git answered the logical spelling {named}, so nothing diverges"
    return logical, repo.root


def _run_deploy(
    script_root: Path,
    *,
    bin_dir: Path,
    cwd: Path,
    log: Path,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `script_root/scripts/deploy.sh` under the sandbox `PATH`. Reaches no AWS call by construction.

    `bash` is passed as an absolute path because the sandbox `PATH` does not contain it. The
    environment is built rather than inherited, so `extra_env` is the only thing that differs between
    two runs.
    """
    interpreter = shutil.which("bash")
    assert interpreter is not None, "bash is not on the host PATH"
    return proc_run.run(
        [interpreter, str(script_root / "scripts" / "deploy.sh")],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(bin_dir), "LOG_FILE": str(log), **(extra_env or {})},
    )


class TestUnmergedIndexRefusal:
    """A tree with an unmerged index is refused before `terraform apply`.

    An unusable `.git` refuses; a tree where git resolves no work tree warns and proceeds. A tree with
    `.git` moved aside is not refused.
    """

    def test_it_is_wired_in(self) -> None:
        assert UNMERGED_INVOCATION in _script(), (
            "deploy.sh does not read its own repository's index, so a tree mid-conflict can apply"
        )

    def test_it_runs_before_terraform_apply(self) -> None:
        script = _script()
        assert _index(script, UNMERGED_INVOCATION) < _index(script, TERRAFORM_APPLY)

    def test_it_runs_before_every_other_gate(self) -> None:
        """The index guard runs before the exposure, preflight and zip gates."""
        script = _script()
        at = _index(script, UNMERGED_INVOCATION)
        for later in (EXPOSURE_INVOCATION, PREFLIGHT_INVOCATION, PYTHON_ZIP_INVOCATION):
            assert at < _index(script, later), later

    def test_the_progress_marker_is_still_the_next_refusal(self) -> None:
        """`PROGRESS_MARKER` is present in the script and follows the index guard."""
        script = _script()
        assert PROGRESS_MARKER in script
        assert _index(script, UNMERGED_INVOCATION) < _index(script, PROGRESS_MARKER)

    @pytest.mark.parametrize("operation", CONFLICTING_OPERATIONS)
    def test_an_unmerged_index_refuses_whatever_left_it_that_way(
        self, tmp_path: Path, operation: str
    ) -> None:
        """The refusal fires for every git operation that can leave an unmerged index."""
        repo = _make_repo(tmp_path / "repo")
        _leave_index_unmerged(repo, operation, tmp_path)
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        result = _run_deploy(repo.root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert result.returncode != 0, output
        assert UNMERGED_REFUSAL in output, output
        assert NO_HARM_PHRASE in output, output
        assert ALWAYS_APPLICABLE_REMEDY in output, (
            f"the refusal for {operation} does not carry the remedy that applies to all six states"
        )
        assert PROGRESS_MARKER not in output, (
            f"the {operation} tree got past the index guard: it was refused later, or not at all"
        )

    def test_a_clean_index_is_not_refused(self, tmp_path: Path) -> None:
        """A clean tree reaches the next refusal while a conflicted tree is refused on its index.

        Both runs exit non-zero, so the message is the observable.
        """
        clean = _make_repo(tmp_path / "clean")
        dirty = _make_repo(tmp_path / "dirty")
        _leave_index_unmerged(dirty, "merge", tmp_path)
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        clean_result = _run_deploy(
            clean.root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "clean.log"
        )
        dirty_result = _run_deploy(
            dirty.root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "dirty.log"
        )
        clean_output = clean_result.stdout + clean_result.stderr
        dirty_output = dirty_result.stdout + dirty_result.stderr

        assert UNMERGED_REFUSAL not in clean_output, clean_output
        assert PROGRESS_MARKER in clean_output, clean_output
        assert UNMERGED_REFUSAL in dirty_output, dirty_output
        assert PROGRESS_MARKER not in dirty_output, dirty_output

    def test_the_refusal_is_what_stops_the_conflicted_tree(self, tmp_path: Path) -> None:
        """With the guard's git call neutered, the conflicted tree gets past the index guard."""
        script = _script()
        _index(script, UNMERGED_INVOCATION)  # loud if the invocation was renamed
        neutered = script.replace(UNMERGED_INVOCATION, "printf ''")
        assert neutered != script

        repo = _make_repo(tmp_path / "repo", script_text=neutered)
        _leave_index_unmerged(repo, "merge", tmp_path)
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        result = _run_deploy(repo.root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert UNMERGED_REFUSAL not in output, output
        assert PROGRESS_MARKER in output, (
            "the conflicted tree was still stopped with the guard's own git call removed, so this "
            "test is not measuring the guard"
        )

    def test_the_index_examined_is_the_scripts_own_repository(self, tmp_path: Path) -> None:
        """The script's own repository's index is examined, never the caller's cwd's."""
        conflicted = _make_repo(tmp_path / "conflicted")
        _leave_index_unmerged(conflicted, "merge", tmp_path)
        clean = _make_repo(tmp_path / "clean")
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        from_clean_cwd = _run_deploy(
            conflicted.root, bin_dir=bin_dir, cwd=clean.root, log=tmp_path / "a.log"
        )
        assert UNMERGED_REFUSAL in from_clean_cwd.stdout + from_clean_cwd.stderr

        from_conflicted_cwd = _run_deploy(
            clean.root, bin_dir=bin_dir, cwd=conflicted.root, log=tmp_path / "b.log"
        )
        output = from_conflicted_cwd.stdout + from_conflicted_cwd.stderr
        assert UNMERGED_REFUSAL not in output, output
        assert PROGRESS_MARKER in output, output

    def test_an_unreadable_index_refuses_rather_than_proceeding(self, tmp_path: Path) -> None:
        """A present but unusable `.git` is refused."""
        root = tmp_path / "corrupt"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "deploy.sh").write_text(_script(), encoding="utf-8")
        (root / ".git").mkdir()
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        result = _run_deploy(root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert result.returncode != 0
        assert UNREADABLE_INDEX_REFUSAL in output, output
        assert NO_HARM_PHRASE in output, output
        assert PROGRESS_MARKER not in output, output

    def test_a_tree_that_is_no_checkout_says_so_and_does_not_refuse(self, tmp_path: Path) -> None:
        """A tree where git resolves no work tree warns, says the question is UNAVAILABLE, and
        proceeds."""
        root = tmp_path / "no-checkout"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "deploy.sh").write_text(_script(), encoding="utf-8")
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")
        # The fixture's control: git resolves no work tree from here.
        assert (
            proc_run.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            != 0
        ), f"{root} is inside a git work tree, so this is the nested cell and not the no-index one"

        result = _run_deploy(root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert UNMERGED_REFUSAL not in output, output
        assert UNREADABLE_INDEX_REFUSAL not in output, output
        assert NO_WORKTREE_WARNING in output, output
        assert "UNAVAILABLE" in output, (
            "the warning does not say that the artifact/repository question goes unanswered, so it "
            "reads as an all-clear"
        )
        assert PROGRESS_MARKER in output, output

    def test_an_absent_git_refuses_rather_than_proceeding(self, tmp_path: Path) -> None:
        """A tree with `.git` and no git on PATH is refused."""
        repo = _make_repo(tmp_path / "repo")
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin", include_git=False)

        result = _run_deploy(repo.root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert result.returncode != 0
        assert GIT_ABSENT_REFUSAL in output, output
        assert NO_HARM_PHRASE in output, output
        assert PROGRESS_MARKER not in output, output

    def test_the_refusal_names_no_operation_specific_undo_command(self) -> None:
        """The refusal names no operation-specific undo command and gives the always-applicable
        remedy."""
        script = _script()
        # The refusal is one `die` on one line.
        refusal = script[_index(script, UNMERGED_REFUSAL) :]
        refusal = refusal[: refusal.index("\n")]
        assert "merge --abort" not in refusal, (
            "the refusal prescribes a merge-specific command for a state five other operations can "
            "also produce"
        )
        assert ALWAYS_APPLICABLE_REMEDY in refusal, (
            "the refusal does not give the remedy that applies to every state reaching it"
        )
        assert "an alternative only where an operation is in progress" in refusal, (
            "the refusal presents aborting unconditionally, which is false after `stash apply`"
        )
        assert "git status" not in refusal, (
            "the refusal sends the operator to `git status` for the operation and the command that "
            "continues or aborts it; after `stash apply` it names neither"
        )
        # The refusal marks its operation list as illustrative.
        assert "among others" in refusal

    def test_the_sandbox_cannot_reach_aws_or_terraform(self, tmp_path: Path) -> None:
        """Neither `aws` nor `terraform` is reachable from the sandbox PATH."""
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")
        for executable in ("aws", "terraform"):
            assert shutil.which(executable, path=str(bin_dir)) is None, (
                f"{executable} is reachable from the sandbox PATH {bin_dir}"
            )


class TestNestedCheckoutIsNotABypass:
    """A subtree of a larger checkout with no `.git` of its own is checked against the outer index.

    Every test here exits non-zero, so the observable is the message and which probe the run reached.
    """

    def test_git_is_asked_before_any_prefix_scoped_question(self) -> None:
        """`command -v git` precedes the work-tree probe, which precedes the index read."""
        script = _script()
        assert _index(script, "command -v git") < _index(script, WORKTREE_PROBE)
        assert _index(script, WORKTREE_PROBE) < _index(script, UNMERGED_INVOCATION)

    @pytest.mark.parametrize("operation", CONFLICTING_OPERATIONS)
    def test_a_conflict_outside_the_subtree_refuses(self, tmp_path: Path, operation: str) -> None:
        """A conflict outside the subtree refuses, naming the outer root, for every operation."""
        inner = _make_nested_repo(tmp_path / "outer", operation=operation, scratch=tmp_path)
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        result = _run_deploy(inner, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert result.returncode != 0, output
        assert NESTED_DISCLOSURE in output, output
        assert UNMERGED_REFUSAL in output, output
        assert NO_HARM_PHRASE in output, output
        assert str(inner.parent) in output, (
            "the refusal does not name the outer root whose index it read"
        )
        assert PROGRESS_MARKER not in output, output

    def test_a_clean_parent_checkout_is_disclosed_and_not_refused(self, tmp_path: Path) -> None:
        """A subtree of a clean parent is disclosed as nested and not refused, unlike a dirty one."""
        clean_inner = _make_nested_repo(tmp_path / "clean-outer", operation=None, scratch=tmp_path)
        dirty_inner = _make_nested_repo(
            tmp_path / "dirty-outer", operation="merge", scratch=tmp_path
        )
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        clean = _run_deploy(clean_inner, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "clean.log")
        dirty = _run_deploy(dirty_inner, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "dirty.log")
        clean_output = clean.stdout + clean.stderr
        dirty_output = dirty.stdout + dirty.stderr

        assert NESTED_DISCLOSURE in clean_output, clean_output
        assert UNMERGED_REFUSAL not in clean_output, clean_output
        assert PROGRESS_MARKER in clean_output, clean_output
        assert UNMERGED_REFUSAL in dirty_output, dirty_output
        assert PROGRESS_MARKER not in dirty_output, dirty_output

    def test_the_worktree_probe_is_what_sees_the_outer_conflict(self, tmp_path: Path) -> None:
        """With the work-tree probe neutered, the nested conflicted tree gets past the index guard."""
        script = _script()
        _index(script, WORKTREE_PROBE)  # loud if the probe was renamed rather than mutating nothing
        neutered = script.replace(WORKTREE_PROBE, "false")
        assert neutered != script

        inner = _make_nested_repo(
            tmp_path / "outer", operation="merge", scratch=tmp_path, script_text=neutered
        )
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        result = _run_deploy(inner, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert UNMERGED_REFUSAL not in output, output
        assert PROGRESS_MARKER in output, (
            "the nested conflicted tree was still refused with the toplevel probe removed, so this "
            "test is not measuring the probe"
        )

    def test_the_index_read_is_the_scripts_own_and_not_the_cwds(self, tmp_path: Path) -> None:
        """A conflict in the caller's cwd does not refuse a nested tree with a clean parent."""
        clean_inner = _make_nested_repo(tmp_path / "clean-outer", operation=None, scratch=tmp_path)
        conflicted = _make_repo(tmp_path / "elsewhere")
        _leave_index_unmerged(conflicted, "merge", tmp_path)
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        result = _run_deploy(
            clean_inner, bin_dir=bin_dir, cwd=conflicted.root, log=tmp_path / "deploy.log"
        )
        output = result.stdout + result.stderr

        assert UNMERGED_REFUSAL not in output, output
        assert PROGRESS_MARKER in output, output

    def test_an_absent_git_in_a_tree_with_no_dot_git_warns_rather_than_refusing(
        self, tmp_path: Path
    ) -> None:
        """With no git and no `.git`, the script warns that the question is UNAVAILABLE and
        proceeds."""
        root = tmp_path / "no-checkout"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "deploy.sh").write_text(_script(), encoding="utf-8")
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin", include_git=False)
        assert shutil.which("git", path=str(bin_dir)) is None, "the sandbox PATH still reaches git"

        result = _run_deploy(root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert GIT_ABSENT_REFUSAL not in output, output
        assert UNMERGED_REFUSAL not in output, output
        assert UNREADABLE_INDEX_REFUSAL not in output, output
        assert "git was not found on PATH" in output, output
        assert "UNAVAILABLE" in output, output
        assert PROGRESS_MARKER in output, output


class TestPathSpellingIsNotNesting:
    """A flat checkout reached through another spelling of its path is not disclosed as nested.

    The script decides top-level-ness from git's prefix being empty, not by comparing git's toplevel
    against the `cd`-logical `$REPO_ROOT`.
    """

    def test_the_prefix_predicate_is_the_decision(self) -> None:
        """The prefix predicate appears once, after the work-tree probe."""
        script = _script()
        assert _index(script, WORKTREE_PROBE) < _index(script, PREFIX_PREDICATE)
        assert script.count(PREFIX_PREDICATE) == 1

    def test_a_symlink_reached_root_is_not_disclosed_as_nested(self, tmp_path: Path) -> None:
        """A root reached through a symlink is not disclosed as nested."""
        script_root, physical = _repo_addressed_through_a_symlink(tmp_path)
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        result = _run_deploy(
            script_root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log"
        )
        output = result.stdout + result.stderr

        assert NESTED_DISCLOSURE not in output, output
        assert UNMERGED_REFUSAL not in output, output
        assert PROGRESS_MARKER in output, output
        assert str(physical) != str(script_root)

    def test_a_root_spelled_in_another_case_is_not_disclosed_as_nested(
        self, tmp_path: Path
    ) -> None:
        """A root spelled in another case is not disclosed as nested; skips on a case-sensitive
        volume."""
        repo = _make_repo(tmp_path / "spelled")
        alias = tmp_path / "SPELLED"
        if not alias.is_dir():
            pytest.skip(
                "this volume distinguishes 'spelled' from 'SPELLED', so one checkout cannot be "
                "addressed by both spellings here and the case axis is unreachable"
            )
        assert alias.samefile(repo.root), "the two spellings must name one directory"
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        result = _run_deploy(alias, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "deploy.log")
        output = result.stdout + result.stderr

        assert NESTED_DISCLOSURE not in output, output
        assert UNMERGED_REFUSAL not in output, output
        assert PROGRESS_MARKER in output, output

    def test_a_path_comparison_would_disclose_nesting_that_is_not_there(
        self, tmp_path: Path
    ) -> None:
        """The path-comparison mutant discloses nesting for a symlink-reached root and not for a
        plainly spelled flat checkout."""
        script = _script()
        mutated = script.replace(PREFIX_PREDICATE, PATH_COMPARISON)
        assert mutated != script, "the path-comparison mutation did not apply"
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")

        spelled_root, _physical = _repo_addressed_through_a_symlink(
            tmp_path / "diverging", script_text=mutated
        )
        diverging = _run_deploy(
            spelled_root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "diverging.log"
        )
        diverging_output = diverging.stdout + diverging.stderr

        flat = _make_repo(tmp_path / "agreeing", script_text=mutated)
        agreeing = _run_deploy(
            flat.root, bin_dir=bin_dir, cwd=tmp_path, log=tmp_path / "agreeing.log"
        )
        agreeing_output = agreeing.stdout + agreeing.stderr

        assert NESTED_DISCLOSURE in diverging_output, (
            "the path comparison did not report nesting for a symlink-reached root, so the two cells "
            f"above are not measuring the predicate: {diverging_output}"
        )
        assert NESTED_DISCLOSURE not in agreeing_output, (
            "the mutant reports nesting for a plainly-spelled flat checkout too, so it discloses "
            f"unconditionally and the assertion above is not about the spelling axis: {agreeing_output}"
        )
        assert PROGRESS_MARKER in agreeing_output, agreeing_output


# ---------------------------------------------------------------------------
# `-C "$REPO_ROOT"` does not decide WHICH index answers -- the environment can
# ---------------------------------------------------------------------------

#: The line that makes `cwd`/`-C` the only thing deciding which repository git reads.
GIT_ENV_SCRUB: Final[str] = "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE"

#: Each variable that redirects the toplevel or index git reads, mapped to the path suffix a cell
#: hands it for a foreign repository root (`""` meaning the root itself).
GIT_DISCOVERY_OVERRIDES: Final[dict[str, str]] = {
    "GIT_DIR": ".git",
    "GIT_WORK_TREE": "",
    "GIT_INDEX_FILE": ".git/index",
}


def _override(variable: str, root: Path) -> dict[str, str]:
    """`{variable: <spelling of `root` that variable takes>}`."""
    suffix = GIT_DISCOVERY_OVERRIDES[variable]
    return {variable: str(root / suffix if suffix else root)}


def _unmerged_as_the_script_would_read_it(root: Path, env: Mapping[str, str]) -> str:
    """The preflight's two-step read without the scrub: resolve the toplevel from `root`, then read
    that index's unmerged entries. Used as each cell's positive control."""
    environment = {**os.environ, **env}
    probe = proc_run.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel", "--show-prefix"],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    index_root = probe.stdout.splitlines()[0]
    unmerged: subprocess.CompletedProcess[str] = proc_run.run(
        ["git", "-C", index_root, "ls-files", "--unmerged"],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return unmerged.stdout


class TestGitDiscoveryOverridesCannotMoveTheIndexRead:
    """An exported `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` does not decide the refusal.

    Each variable is exercised in both directions. A repository-local `core.worktree` is not covered.
    """

    def test_the_scrub_is_wired_in_ahead_of_every_git_probe(self) -> None:
        """The scrub appears once, before the work-tree probe and the index read."""
        script = _script()
        assert GIT_ENV_SCRUB in script, (
            "deploy.sh does not scrub git's discovery overrides, so an exported GIT_DIR, "
            "GIT_WORK_TREE or GIT_INDEX_FILE decides which index its refusal reads"
        )
        assert script.count(GIT_ENV_SCRUB) == 1
        assert _index(script, GIT_ENV_SCRUB) < _index(script, WORKTREE_PROBE)
        assert _index(script, GIT_ENV_SCRUB) < _index(script, UNMERGED_INVOCATION)

    @pytest.mark.parametrize("variable", sorted(GIT_DISCOVERY_OVERRIDES))
    def test_a_conflicted_tree_is_refused_under_a_redirect_to_a_clean_repository(
        self, tmp_path: Path, variable: str
    ) -> None:
        """A conflicted tree is refused although the override points git at a clean repository."""
        dirty = _make_repo(tmp_path / "dirty")
        _leave_index_unmerged(dirty, "merge", tmp_path)
        clean = _make_repo(tmp_path / "clean")
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")
        override = _override(variable, clean.root)

        assert _unmerged_as_the_script_would_read_it(dirty.root, {}).strip(), (
            "the fixture's own index reads clean, so this cell proves nothing"
        )
        assert not _unmerged_as_the_script_would_read_it(dirty.root, override).strip(), (
            f"{variable} did not redirect the preflight's own two-step read on this host, so this "
            "cell measures nothing"
        )

        result = _run_deploy(
            dirty.root,
            bin_dir=bin_dir,
            cwd=tmp_path,
            log=tmp_path / "deploy.log",
            extra_env=override,
        )
        output = result.stdout + result.stderr

        assert result.returncode != 0, output
        assert UNMERGED_REFUSAL in output, (
            f"{variable} in the environment carried a conflicted tree past the refusal: {output}"
        )
        assert NO_HARM_PHRASE in output, output
        assert PROGRESS_MARKER not in output, output

    @pytest.mark.parametrize("variable", sorted(GIT_DISCOVERY_OVERRIDES))
    def test_a_clean_tree_is_not_refused_for_a_foreign_repositorys_conflict(
        self, tmp_path: Path, variable: str
    ) -> None:
        """A clean tree is not refused although the override points git at a conflicted repository."""
        clean = _make_repo(tmp_path / "clean")
        dirty = _make_repo(tmp_path / "dirty")
        _leave_index_unmerged(dirty, "merge", tmp_path)
        bin_dir = _sandbox_bin(tmp_path / "sandbox-bin")
        override = _override(variable, dirty.root)

        assert not _unmerged_as_the_script_would_read_it(clean.root, {}).strip()
        assert _unmerged_as_the_script_would_read_it(clean.root, override).strip(), (
            f"{variable} did not redirect the preflight's own two-step read on this host, so this "
            "cell measures nothing"
        )

        result = _run_deploy(
            clean.root,
            bin_dir=bin_dir,
            cwd=tmp_path,
            log=tmp_path / "deploy.log",
            extra_env=override,
        )
        output = result.stdout + result.stderr

        assert UNMERGED_REFUSAL not in output, (
            f"{variable} in the environment refused a clean tree for {dirty.root}'s conflict: {output}"
        )
        assert PROGRESS_MARKER in output, output
