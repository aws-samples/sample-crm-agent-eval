# SPDX-License-Identifier: MIT
"""Tests for scripts/verify_deployment.sh.

Drives the script through `--dry-run` and a stubbed `python3`/`aws`, so no test needs an AWS
credential or writes into the repo's own `results/`. Proves:

1. `--dry-run` prints what would run per layer, invokes no AWS-calling entry point, and provisions
   no tenant.
2. Without `--i-understand-this-is-billable` the script refuses before any tenant or AWS call.
3. Layers run 4 -> 5 -> 6 in order, stop at the first failing layer, and finish the failing layer.
4. The smoke tenant is provisioned before and torn down after on success, layer failure and
   SIGTERM; a teardown failure is reported without masking a layer failure's exit code.
5. The aggregated inventory preserves every check from every script that ran, including a failure.
6. Every artifact path the script writes is matched by a `.gitignore` rule (`git check-ignore`).
7. The script's static layer -> module lists match the on-disk smoke modules and their `LAYER`.
8. A provisioning failure and an interrupt both exit non-zero.
9. The orchestrator resolves `scripts.*` from any working directory.
10. A bearer token is threaded as `--token`/`CRM_SMOKE_TOKEN`; skips and defers are tallied.
11. A script with no parseable stdout appears as a failed resource class; the stderr capture path
    is unique per run and named in the failure line.
12. The `LAYER`-drift gate allow-lists exactly `harness.py`/`tenant.py`/`__init__.py`.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import secrets
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_deployment.sh"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"
SMOKE_DIR = REPO_ROOT / "scripts" / "smoke"

# A recording stub standing in for `.venv/bin/python3`. It handles the inline `-c` snippets and the
# `-m <module>` smoke-script invocations verify_deployment.sh makes, appending each invocation to
# CALL_LOG. Behaviour is controlled by environment variables:
#
# STUB_FAIL_MODULES: space-separated substrings; a matching `-m <module>` exits 1 after emitting a
#   failing CheckResult.
# STUB_SKIP_MODULES: space-separated substrings; a matching `-m <module>` emits a "skip" CheckResult
#   (exit 0).
# STUB_DEFER_MODULES: space-separated substrings; a matching `-m <module>` emits a "defer"
#   CheckResult carrying a `defer_reason` (exit 0).
# STUB_MODE: "provision_fail" | "teardown_fail" | "empty_output" | ... selects failure behaviour of
#   the inline snippets and module invocations.
_PYTHON_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "${CALL_LOG:?CALL_LOG must be set}"

if [[ "${1:-}" == "-c" ]]; then
  snippet="$2"
  if [[ "$snippet" == *"allocate_smoke_tenant"* ]]; then
    if [[ "${STUB_MODE:-}" == "provision_fail" ]]; then
      echo "stub: simulated provisioning failure" >&2
      exit 1
    fi
    echo '{"run_id": "smoke-deadbeef", "tenant": "eval-smoke-deadbeef"}'
    exit 0
  fi
  if [[ "$snippet" == *"json.load(sys.stdin)[\"run_id\"]"* ]]; then
    # The here-string replaces python's stdin, so nothing consumes the pipe the caller writes into.
    # Draining it first keeps that writer off a broken pipe, which `set -o pipefail` makes fatal.
    cat >/dev/null
    python3_real -c "$snippet" <<< '{"run_id": "smoke-deadbeef", "tenant": "eval-smoke-deadbeef"}' 2>/dev/null \
      || echo "smoke-deadbeef"
    exit 0
  fi
  if [[ "$snippet" == *"json.load(sys.stdin)[\"tenant\"]"* ]]; then
    # Answered from stdin rather than echoed as a constant, so the mint's tenant reaches the
    # orchestrator's equality check. Echoing a constant here would make that check compare
    # eval-smoke-deadbeef against itself and pass whatever the mint actually returned.
    python3_real -c "$snippet" || echo "eval-smoke-deadbeef"
    exit 0
  fi
  if [[ "$snippet" == *"json.load(sys.stdin)[\"id_token\"]"* ]]; then
    python3_real -c "$snippet"
    exit $?
  fi
  if [[ "$snippet" == *"json.load(sys.stdin)[\"username\"]"* ]]; then
    python3_real -c "$snippet"
    exit $?
  fi
  if [[ "$snippet" == *"teardown_smoke_tenant"* ]]; then
    if [[ "${STUB_MODE:-}" == "teardown_fail" ]]; then
      echo "stub: simulated teardown failure" >&2
      exit 1
    fi
    exit 0
  fi
  if [[ "$snippet" == *"results = []"* ]]; then
    # Inventory JSON aggregation snippet: pass through to the venv's real interpreter isn't
    # available here, so re-implement the trivial passthrough this stub needs to support.
    python3_real -c "$snippet"
    exit $?
  fi
  if [[ "$snippet" == *"lines = "* ]]; then
    python3_real -c "$snippet"
    exit $?
  fi
  if [[ "$snippet" == *"sum(1 for c in data.get"* ]]; then
    python3_real -c "$snippet"
    exit $?
  fi
  if [[ "$snippet" == *"import ast"* ]]; then
    # TOKEN_ACCEPTING_MODULES derivation: pure AST introspection of the real repo tree, no AWS
    # call, so it can run for real exactly like the other aggregation snippets above.
    python3_real -c "$snippet"
    exit $?
  fi
  if [[ "$snippet" == *"removesuffix(\"eval_runs\")"* ]]; then
    # Table-prefix derivation for the inventory layer: no AWS call, but the stub answers it
    # directly so the test's tf-outputs fixture need not carry the full outputs document.
    echo "stub-prefix-"
    exit 0
  fi
  echo "python-stub: unrecognised -c snippet" >&2
  exit 98
fi

if [[ "${1:-}" == "scripts/consolidate_inventory.py" ]]; then
  echo "python-stub: consolidate_inventory invoked with: ${*:2}"
  exit 0
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "scripts.smoke.smoke_token" ]]; then
  # The CRM_SMOKE_TOKEN lifecycle helper. Not a check module: it emits no CheckResult, so it
  # is answered here rather than falling into the generic `-m` handler below, which would return a
  # CheckResult the orchestrator then tries to read an id_token out of.
  # The subcommand is located by SEARCHING the argv, not read from $3. argparse puts
  # --profile/--region/--outputs on the parent parser, so they precede the subcommand and its
  # position varies. Reading $3 is what made this stub answer any vector without parsing it, which
  # is how the real ordering defect reached a live run with 64 cells green.
  subcommand=""
  for arg in "$@"; do
    case "$arg" in
      mint|delete-user|assert-token) subcommand="$arg"; break ;;
    esac
  done
  case "${subcommand}" in
    mint)
      if [[ "${STUB_MODE:-}" == "mint_fail" ]]; then
        echo "stub: simulated mint failure" >&2
        exit 1
      fi
      # STUB_MINT_TENANT lets a test mint a token for the WRONG tenant, so the orchestrator's own
      # equality check has an arm that can fail.
      # STUB_MINT_SEQUENCE=1 numbers each mint's token and user, so a re-mint is observable.
      suffix=""
      if [[ "${STUB_MINT_SEQUENCE:-}" == "1" ]]; then
        mints=$(( $(cat "${CALL_LOG}.mints" 2>/dev/null || echo 0) + 1 ))
        echo "${mints}" > "${CALL_LOG}.mints"
        suffix="-${mints}"
      fi
      echo "{\"id_token\": \"stub-id-token${suffix}\", \"username\": \"stub-smoke-user${suffix}\", \"email\": \"stub@example.test\", \"tenant\": \"${STUB_MINT_TENANT:-eval-smoke-deadbeef}\"}"
      exit 0
      ;;
    delete-user)
      if [[ "${STUB_MODE:-}" == "user_teardown_fail" ]]; then
        echo "stub: simulated smoke-user teardown failure" >&2
        exit 1
      fi
      exit 0
      ;;
    assert-token)
      if [[ "${STUB_MODE:-}" == "supplied_token_wrong_tenant" ]]; then
        echo "stub: simulated wrong-tenant supplied token" >&2
        exit 1
      fi
      exit 0
      ;;
    *)
      echo "python-stub: no smoke_token subcommand found in argv: $*" >&2
      exit 97
      ;;
  esac
fi

if [[ "${1:-}" == "-m" ]]; then
  module="$2"
  resource_class="${module##*.}"
  layer=4
  case "$module" in
    *integration_*) layer=5 ;;
    *three_tier_chat) layer=5 ;;
    *eval_sfn) layer=5 ;;
    *admin_authz) layer=5 ;;
    *user_settings_api) layer=5 ;;
    *mcp_chat_gateway) layer=5 ;;
    *mcp_client_compat) layer=5 ;;
    scripts.smoke_*) layer=6 ;;
  esac
  if [[ "${STUB_MODE:-}" == "empty_output" ]]; then
    echo "stub: simulated uncaught traceback" >&2
    exit 1
  fi
  status="pass"
  for failmod in ${STUB_FAIL_MODULES:-}; do
    if [[ "$module" == *"$failmod"* ]]; then
      status="fail"
    fi
  done
  for skipmod in ${STUB_SKIP_MODULES:-}; do
    if [[ "$module" == *"$skipmod"* ]]; then
      status="skip"
    fi
  done
  for defermod in ${STUB_DEFER_MODULES:-}; do
    if [[ "$module" == *"$defermod"* ]]; then
      status="defer"
    fi
  done
  if [[ "$status" == "fail" ]]; then
    echo "{\"layer\": $layer, \"resource_class\": \"$resource_class\", \"checks\": [{\"resource\": \"$resource_class\", \"check\": \"probe\", \"status\": \"fail\", \"detail\": \"stub failure\"}]}"
    exit 1
  fi
  if [[ "$status" == "defer" ]]; then
    echo "{\"layer\": $layer, \"resource_class\": \"$resource_class\", \"checks\": [{\"resource\": \"$resource_class\", \"check\": \"probe\", \"status\": \"defer\", \"detail\": \"not verified, not enforced\", \"defer_reason\": \"missing_custom_acm_certificate\"}]}"
    exit 0
  fi
  echo "{\"layer\": $layer, \"resource_class\": \"$resource_class\", \"checks\": [{\"resource\": \"$resource_class\", \"check\": \"probe\", \"status\": \"$status\", \"detail\": \"\"}]}"
  exit 0
fi

echo "python-stub: unexpected invocation: $*" >&2
exit 99
"""

_AWS_STUB = """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "${AWS_STUB_CALL_LOG:?AWS_STUB_CALL_LOG must be set}"
echo "aws-stub: unexpected invocation: $*" >&2
exit 99
"""


def _write_stub_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Materialize the stub `.venv/bin/python3` and return a script copy that uses it.

    The script hardcodes `PYTHON_BIN`, so the copy written under `tmp_path` points it at the stub.
    `python3_real` inside the stub resolves from the real `PATH` for the local-only snippets.
    """
    stub_python = tmp_path / "python3_stub"
    stub_python.write_text(_PYTHON_STUB, encoding="utf-8")
    stub_python.chmod(0o755)

    real_python = sys.executable

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3_real").symlink_to(real_python)

    aws_stub = bin_dir / "aws"
    aws_stub.write_text(_AWS_STUB, encoding="utf-8")
    aws_stub.chmod(0o755)

    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    patched = script_text.replace(
        'PYTHON_BIN="${REPO_ROOT}/.venv/bin/python3"',
        f'PYTHON_BIN="{stub_python}"',
    )
    assert patched != script_text, "PYTHON_BIN assignment line not found to patch"
    # Under `tmp_path/scripts/`, so the script's `REPO_ROOT="${SCRIPT_DIR}/.."` is `tmp_path`.
    fake_scripts_dir = tmp_path / "scripts"
    fake_scripts_dir.mkdir()
    patched_script = fake_scripts_dir / "verify_deployment.sh"
    patched_script.write_text(patched, encoding="utf-8")
    patched_script.chmod(0o755)

    # The script's TOKEN_ACCEPTING_MODULES derivation AST-scans the smoke sources under REPO_ROOT.
    fake_smoke_dir = fake_scripts_dir / "smoke"
    fake_smoke_dir.mkdir()
    for py_path in SMOKE_DIR.glob("*.py"):
        (fake_smoke_dir / py_path.name).write_text(
            py_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    for py_path in (REPO_ROOT / "scripts").glob("smoke_*.py"):
        (fake_scripts_dir / py_path.name).write_text(
            py_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    call_log = tmp_path / "calls.log"
    call_log.write_text("", encoding="utf-8")
    aws_call_log = tmp_path / "aws_calls.log"
    aws_call_log.write_text("", encoding="utf-8")

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CALL_LOG": str(call_log),
        "AWS_STUB_CALL_LOG": str(aws_call_log),
    }
    return patched_script, env


def _run(
    script_path: Path,
    args: list[str],
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        ["bash", str(script_path), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def stubbed(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    return _write_stub_env(tmp_path)


@pytest.fixture
def outputs_file(tmp_path: Path) -> Path:
    path = tmp_path / "tf-outputs.json"
    path.write_text("{}", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1 & 2. Refusal and --dry-run mechanics.
# ---------------------------------------------------------------------------


def test_refuses_to_run_without_billable_flag_or_dry_run(
    stubbed: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, [], env, tmp_path)
    assert result.returncode != 0
    assert "billable" in result.stderr.lower()
    call_log = Path(env["CALL_LOG"])
    assert call_log.read_text(encoding="utf-8") == "", "no python invocation before refusal"
    aws_log = Path(env["AWS_STUB_CALL_LOG"])
    assert aws_log.read_text(encoding="utf-8") == "", "no AWS call before refusal"


def test_dry_run_requires_no_billable_flag(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, ["--dry-run", "--outputs", str(outputs_file)], env, tmp_path)
    assert result.returncode == 0, result.stderr


def test_dry_run_makes_no_aws_call_and_provisions_no_tenant(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, ["--dry-run", "--outputs", str(outputs_file)], env, tmp_path)
    assert result.returncode == 0, result.stderr

    call_log = Path(env["CALL_LOG"])
    call_log_text = call_log.read_text(encoding="utf-8")
    # One entry per `-c `/`-m ` invocation; the AST-scan snippet itself spans many lines.
    invocation_entries = re.findall(r"(?:^|\n)(-[cm] .*?)(?=\n-[cm] |\Z)", call_log_text, re.DOTALL)
    # Only two local-read `-c` snippets may run during --dry-run: the TOKEN_ACCEPTING_MODULES AST
    # scan and the outputs-region target check. Any other snippet or `-m <module>` fails.
    permitted_markers = ("-c \nimport ast", "-c \nimport sys\nfrom scripts.smoke.harness import")
    non_scan_entries = [
        entry for entry in invocation_entries if not entry.startswith(permitted_markers)
    ]
    assert non_scan_entries == [], (
        "an AWS-calling python invocation (tenant/smoke-script) ran during --dry-run: "
        + "\n---\n".join(non_scan_entries)
    )
    aws_log = Path(env["AWS_STUB_CALL_LOG"])
    assert aws_log.read_text(encoding="utf-8") == "", "aws stub was invoked during --dry-run"
    assert "provisioned smoke tenant" not in result.stdout


def test_dry_run_lists_every_layer_and_script(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, ["--dry-run", "--outputs", str(outputs_file)], env, tmp_path)
    assert result.returncode == 0, result.stderr
    for layer in ("4", "5", "6"):
        assert f"Layer {layer}" in result.stdout
    for module in (
        "scripts.smoke.lambda_actions",
        "scripts.smoke.dynamodb_tables",
        "scripts.smoke.integration_proposal_chain",
        "scripts.smoke_agent_stream",
        "scripts.smoke_proposal_card",
    ):
        assert module in result.stdout


def test_dry_run_layer_flag_lists_only_that_layer(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path, ["--dry-run", "--layer", "5", "--outputs", str(outputs_file)], env, tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert "Layer 5" in result.stdout
    assert "Layer 4" not in result.stdout
    assert "Layer 6" not in result.stdout


def test_layer_inventory_is_accepted_and_previewed_in_a_dry_run(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path,
        ["--dry-run", "--layer", "inventory", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "consolidate_inventory.py" in result.stdout
    # It re-runs no layer, so no smoke module may appear in the preview.
    assert "scripts.smoke." not in result.stdout
    call_log = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    # No python invocation other than the token-derivation scan may run, and no AWS call.
    assert "consolidate_inventory.py" not in call_log
    assert Path(env["AWS_STUB_CALL_LOG"]).read_text(encoding="utf-8") == ""


def test_layer_inventory_provisions_no_smoke_tenant(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """The inventory layer provisions no smoke tenant and passes `--outputs` to the consolidator."""
    script_path, env = stubbed
    result = _run(
        script_path,
        [
            "--i-understand-this-is-billable",
            "--layer",
            "inventory",
            "--outputs",
            str(outputs_file),
        ],
        env,
        tmp_path,
    )
    call_log = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    assert "allocate_smoke_tenant" not in call_log, (
        f"the inventory layer provisioned a smoke tenant: {call_log}"
    )
    assert "seed_smoke_tenant" not in call_log
    assert "consolidate_inventory.py" in call_log, (
        f"the inventory layer did not invoke the consolidator: {call_log}"
    )
    # `--outputs` makes the residue query refuse a wrong account or region.
    assert "--outputs" in call_log, (
        f"the residue query was invoked with no target assertion: {call_log}"
    )
    # The inventory layer returns before write_inventory().
    assert "wrote" not in result.stdout or "consolidate" in result.stdout


def test_bad_layer_value_is_a_usage_error(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path,
        ["--dry-run", "--layer", "7", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode == 2


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_flag_exits_zero_with_no_aws_call(
    flag: str, stubbed: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, [flag], env, tmp_path)
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_unknown_argument_is_a_usage_error(
    stubbed: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, ["--not-a-real-flag"], env, tmp_path)
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# 3. Layer ordering: fail-fast at the layer boundary, complete within a layer.
# ---------------------------------------------------------------------------


def test_layer_4_failure_stops_layers_5_and_6_but_all_layer_4_scripts_still_run(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    env = {**env, "STUB_FAIL_MODULES": "lambda_actions"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode != 0

    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    invoked_modules = re.findall(r"-m (\S+)", call_log_text)

    # every declared Layer 4 module ran, even though one of them failed
    for module in (
        "scripts.smoke.lambda_actions",
        "scripts.smoke.api_routes",
        "scripts.smoke.iam_roles",
        "scripts.smoke.gateway_tools",
        "scripts.smoke.agents",
        "scripts.smoke.dynamodb_tables",
        "scripts.smoke.cognito_claims",
        "scripts.smoke.cloudfront",
        "scripts.smoke.access_log_delivery",
    ):
        assert module in invoked_modules, f"{module} did not run despite being Layer 4"

    # no Layer 5 or Layer 6 module ran
    for module in (
        "scripts.smoke.integration_ui_api_ddb",
        "scripts.smoke.integration_gateway_scope",
        "scripts.smoke.integration_proposal_chain",
        "scripts.smoke_agent_stream",
        "scripts.smoke_proposal_card",
    ):
        assert module not in invoked_modules, f"{module} ran despite Layer 4 failing"


def test_layer_5_failure_stops_layer_6_but_layer_4_and_5_run_in_full(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    env = {**env, "STUB_FAIL_MODULES": "integration_gateway_scope"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode != 0

    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    invoked_modules = re.findall(r"-m (\S+)", call_log_text)

    for module in (
        "scripts.smoke.lambda_actions",
        "scripts.smoke.cloudfront",
        "scripts.smoke.integration_ui_api_ddb",
        "scripts.smoke.integration_gateway_scope",
        "scripts.smoke.integration_proposal_chain",
    ):
        assert module in invoked_modules, f"{module} did not run"

    for module in (
        "scripts.smoke_agent_stream",
        "scripts.smoke_proposal_card",
    ):
        assert module not in invoked_modules, f"{module} ran despite Layer 5 failing"


def test_all_layers_pass_runs_every_module_exactly_once_in_order(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr

    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    all_invocations = re.findall(r"-m (\S+)", call_log_text)
    # The token helper runs twice (mint, delete-user); its ordering is asserted separately below.
    helper_modules = {
        f"scripts.smoke.{name.removesuffix('.py')}" for name in _SMOKE_CLI_HELPER_MODULES
    }
    invoked_modules = [m for m in all_invocations if m not in helper_modules]
    # Every module in the on-disk inventory runs exactly once.
    assert len(invoked_modules) == len(set(invoked_modules)) == len(_EXPECTED_REAL_SMOKE_MODULES), (
        f"expected {len(_EXPECTED_REAL_SMOKE_MODULES)} unique module invocations, got "
        f"{len(invoked_modules)} ({len(set(invoked_modules))} unique): {invoked_modules}"
    )

    # The mint runs before the first check module and the user's removal after the last.
    mint_positions = [i for i, m in enumerate(all_invocations) if m in helper_modules]
    assert len(mint_positions) == 2, (
        f"expected the token helper to run exactly twice (mint, then delete-user), got "
        f"{len(mint_positions)}: {all_invocations}"
    )
    first_check = min(i for i, m in enumerate(all_invocations) if m not in helper_modules)
    last_check = max(i for i, m in enumerate(all_invocations) if m not in helper_modules)
    assert mint_positions[0] < first_check, (
        "the smoke token was minted after a check module already ran, so that module saw no token"
    )
    assert mint_positions[1] > last_check, (
        "the smoke user was deleted before the last check module ran, so later checks lost their token"
    )
    assert "mint" in call_log_text and "delete-user" in call_log_text, (
        f"expected both smoke_token subcommands in the call log: {call_log_text}"
    )

    def index_of(substr: str) -> int:
        for i, m in enumerate(invoked_modules):
            if substr in m:
                return i
        raise AssertionError(f"{substr} never invoked")

    # Layers ran strictly in ascending order.
    layer4_max = max(index_of(m) for m in ("lambda_actions", "api_routes", "iam_roles"))
    layer5_min = min(index_of(m) for m in ("integration_ui_api_ddb",))
    layer5_max = max(index_of(m) for m in ("integration_proposal_chain",))
    layer6_min = min(index_of(m) for m in ("smoke_agent_stream",))
    assert layer4_max < layer5_min
    assert layer5_max < layer6_min


def test_layer_flag_runs_only_that_layer(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--layer", "5", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    helper_modules = {
        f"scripts.smoke.{name.removesuffix('.py')}" for name in _SMOKE_CLI_HELPER_MODULES
    }
    # The token helper runs for every layer selection, so it is excluded here.
    invoked_modules = [m for m in re.findall(r"-m (\S+)", call_log_text) if m not in helper_modules]
    # Exactly the script's own LAYER_5_MODULES ran.
    assert set(invoked_modules) == _declared_layer_arrays_from_script()[5]


# ---------------------------------------------------------------------------
# The CRM_SMOKE_TOKEN guard: a caller-supplied token must name this run's tenant.
# ---------------------------------------------------------------------------


def _check_modules_in(call_log_text: str) -> list[str]:
    helpers = {f"scripts.smoke.{name.removesuffix('.py')}" for name in _SMOKE_CLI_HELPER_MODULES}
    return [m for m in re.findall(r"-m (\S+)", call_log_text) if m not in helpers]


def _throwaway_token() -> str:
    """Return a stand-in token assembled at runtime, so no secret-shaped literal trips gitleaks."""
    return "stub." + secrets.token_urlsafe(6) + ".not-a-real-jwt"


def test_a_supplied_token_naming_another_tenant_aborts_before_any_layer_runs(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A supplied token naming another tenant aborts the run before any check module runs.

    The run exits non-zero, runs no check module, and still tears the tenant down.
    """
    script_path, env = stubbed
    env = {**env, "CRM_SMOKE_TOKEN": _throwaway_token()}
    env["STUB_MODE"] = "supplied_token_wrong_tenant"

    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )

    assert result.returncode != 0, result.stdout
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    assert _check_modules_in(call_log_text) == [], (
        "a check module ran despite the supplied token being refused, so rows may have been written "
        f"beneath a tenant this run will not reap: {call_log_text}"
    )
    assert "teardown_smoke_tenant" in call_log_text, "the tenant must still be torn down on refusal"
    combined = result.stdout + result.stderr
    for token in ("PER RUN", "custom:tenant", "reps row", "omit it"):
        assert token in combined, (
            f"the refusal does not mention {token!r}, so it says the token was rejected without "
            f"saying why a supplied token cannot carry this run's tenant: {combined}"
        )


def test_a_supplied_token_naming_this_runs_tenant_is_accepted_and_no_user_is_minted(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A supplied token naming this run's tenant is accepted and suppresses the mint."""
    script_path, env = stubbed
    env = {**env, "CRM_SMOKE_TOKEN": _throwaway_token()}

    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    assert len(_check_modules_in(call_log_text)) == len(_EXPECTED_REAL_SMOKE_MODULES)
    assert "assert-token" in call_log_text, "the supplied token was never asserted at all"
    assert " mint " not in f" {call_log_text} ".replace("\n", " "), (
        f"a smoke user was minted although the caller supplied a usable token: {call_log_text}"
    )


def test_a_minted_token_naming_another_tenant_is_discarded_rather_than_threaded(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A minted token naming another tenant is discarded and never threaded as `--token`."""
    script_path, env = stubbed
    env["STUB_MINT_TENANT"] = "eval-smoke-someone-elses-run"

    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )

    combined = result.stdout + result.stderr
    assert "Discarding it" in combined, (
        f"the orchestrator threaded a token naming another tenant: {combined}"
    )
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    # Only `-m <module>` lines: the AST-derivation snippet's source also contains `'--token'`.
    threaded = [
        line for line in call_log_text.splitlines() if line.startswith("-m ") and "--token" in line
    ]
    assert threaded == [], f"a discarded token was still threaded to the modules: {threaded}"


# ---------------------------------------------------------------------------
# 4. Tenant lifecycle: provisioned before, torn down after -- happy path, failure, interrupt.
# ---------------------------------------------------------------------------


def test_tenant_provisioned_before_and_torn_down_after_happy_path(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    assert "allocate_smoke_tenant" in call_log_text
    assert "teardown_smoke_tenant" in call_log_text
    provision_idx = call_log_text.index("allocate_smoke_tenant")
    teardown_idx = call_log_text.index("teardown_smoke_tenant")
    first_module_idx = re.search(r"-m \S+", call_log_text)
    assert first_module_idx is not None
    assert provision_idx < first_module_idx.start(), (
        "tenant must be provisioned before any script runs"
    )
    assert teardown_idx > provision_idx, "teardown must happen after provisioning"


def test_tenant_torn_down_after_a_layer_failure(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    env = {**env, "STUB_FAIL_MODULES": "lambda_actions"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode != 0
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    assert "teardown_smoke_tenant" in call_log_text, "teardown must run even when a layer failed"
    # A successful teardown does not mask the layer failure's exit code.
    assert result.returncode == 1


#: Wall-clock bound on one stage of the stubbed run appearing in the call log.
_STAGE_TIMEOUT_SECONDS: Final = 60

#: Wall-clock bound on the child's SIGTERM trap completing after `terminate()`; killing the child
#: earlier would skip the teardown these tests assert on.
_TRAP_TIMEOUT_SECONDS: Final = 60


def _wait_for_call_log(
    proc: subprocess.Popen[str],
    call_log: Path,
    needle: str,
    stage: str,
    aws_log: Path | None = None,
) -> None:
    """Poll `call_log` until `needle` appears; on failure, report whether the child still runs.

    The failure message carries the child's exit status, stdout, stderr and the aws stub's log.
    """
    deadline = time.monotonic() + _STAGE_TIMEOUT_SECONDS
    exited_early: int | None = None
    while time.monotonic() < deadline:
        if needle in call_log.read_text(encoding="utf-8"):
            return
        exited_early = proc.poll()
        if exited_early is not None:
            # The child may have written the needle just before exiting.
            if needle in call_log.read_text(encoding="utf-8"):
                return
            break
        time.sleep(0.1)

    if exited_early is None:
        verdict = (
            f"the child was STILL RUNNING after {_STAGE_TIMEOUT_SECONDS}s, so this is a timeout "
            f"rather than an early exit"
        )
        proc.kill()
    else:
        verdict = (
            f"the child EXITED with returncode {exited_early} before reaching this stage, so the "
            f"bound is not the problem"
        )
    try:
        stdout, stderr = proc.communicate(timeout=_TRAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = "<not collected: child did not exit after kill>", ""
    # Only the python stub writes `call_log`; the aws stub's log shows progress through `aws` calls.
    aws_text = (
        aws_log.read_text(encoding="utf-8") if aws_log is not None and aws_log.exists() else ""
    )
    raise AssertionError(
        f"{stage} did not appear in the call log (looked for {needle!r}); {verdict}.\n"
        f"--- call log ---\n{call_log.read_text(encoding='utf-8')}"
        f"\n--- aws stub call log ---\n{aws_text}"
        f"\n--- child stdout ---\n{stdout}"
        f"\n--- child stderr ---\n{stderr}"
    )


def test_tenant_torn_down_on_sigterm_interrupt(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """SIGTERM after provisioning and the start of Layer 4 still tears the tenant down."""
    script_path, env = stubbed
    # Module invocations sleep, giving a window to send SIGTERM mid-run.
    slow_stub = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        echo "$*" >> "${CALL_LOG}"
        if [[ "${1:-}" == "-c" ]]; then
          if [[ "$2" == *"allocate_smoke_tenant"* ]]; then
            echo '{"run_id": "smoke-deadbeef", "tenant": "eval-smoke-deadbeef"}'
            exit 0
          fi
          if [[ "$2" == *'json.load(sys.stdin)["run_id"]'* ]]; then
            # Drained before answering: the orchestrator pipes its captured result into this
            # stub, and a reader that exits without consuming leaves the writer on a broken pipe.
            # Under `set -o pipefail` that SIGPIPE becomes the pipeline status and kills the caller.
            cat >/dev/null
            echo "smoke-deadbeef"
            exit 0
          fi
          if [[ "$2" == *'json.load(sys.stdin)["tenant"]'* ]]; then
            # Drained before answering: the orchestrator pipes its captured result into this
            # stub, and a reader that exits without consuming leaves the writer on a broken pipe.
            # Under `set -o pipefail` that SIGPIPE becomes the pipeline status and kills the caller.
            cat >/dev/null
            echo "eval-smoke-deadbeef"
            exit 0
          fi
          if [[ "$2" == *"teardown_smoke_tenant"* ]]; then
            exit 0
          fi
          exit 0
        fi
        if [[ "${1:-}" == "-m" ]]; then
          sleep 5
          exit 0
        fi
        exit 99
        """
    )
    stub_python = Path(env["PATH"].split(":")[0]) / "python3_stub_slow"
    stub_python.write_text(slow_stub, encoding="utf-8")
    stub_python.chmod(0o755)

    patched_text = script_path.read_text(encoding="utf-8")
    patched_text = re.sub(r'PYTHON_BIN="[^"]+"', f'PYTHON_BIN="{stub_python}"', patched_text)
    slow_script = script_path.parent / "verify_deployment_slow.sh"
    slow_script.write_text(patched_text, encoding="utf-8")
    slow_script.chmod(0o755)

    proc = proc_run.popen(
        [
            "bash",
            str(slow_script),
            "--i-understand-this-is-billable",
            "--outputs",
            str(outputs_file),
        ],
        cwd=tmp_path,
        env=env,
        # The stub reads stdin for two `-c` calls, so an inherited open stdin would block them.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    call_log = Path(env["CALL_LOG"])
    aws_log = Path(env["AWS_STUB_CALL_LOG"])
    _wait_for_call_log(proc, call_log, "allocate_smoke_tenant", "tenant provisioning", aws_log)
    # SIGTERM lands only after Layer 4 has started.
    _wait_for_call_log(proc, call_log, "-m scripts.smoke.", "Layer 4 invocation", aws_log)

    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=_TRAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)

    call_log_text = call_log.read_text(encoding="utf-8")
    assert "teardown_smoke_tenant" in call_log_text, (
        "teardown must run on SIGTERM/interrupt, not only on a clean exit: "
        + call_log_text
        + "\n--- stdout ---\n"
        + stdout
        + "\n--- stderr ---\n"
        + stderr
    )


def test_teardown_failure_is_reported_and_does_not_mask_a_passing_run(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A teardown failure after passing layers exits non-zero with a warning on stderr."""
    script_path, env = stubbed
    env = {**env, "STUB_MODE": "teardown_fail"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode != 0
    assert "teardown" in result.stderr.lower()
    assert "fail" in result.stderr.lower()


def test_teardown_failure_does_not_mask_a_real_layer_failure(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """When a layer and teardown both fail, the layer failure's exit code (1) wins."""
    script_path, env = stubbed
    env = {**env, "STUB_FAIL_MODULES": "lambda_actions", "STUB_MODE": "teardown_fail"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode == 1
    assert "teardown" in result.stderr.lower()


# ---------------------------------------------------------------------------
# 5. Inventory aggregation: every check from every script preserved, including a failing one.
# ---------------------------------------------------------------------------


def test_inventory_json_and_md_preserve_every_check_including_from_a_failing_script(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    env = {**env, "STUB_FAIL_MODULES": "lambda_actions"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode != 0

    # A layer run writes the per-run rendering, never the published document.
    inventory_json = tmp_path / "results" / "verification-inventory.run.json"
    inventory_md = tmp_path / "results" / "verification-inventory.run.md"
    assert not (tmp_path / "results" / "verification-inventory.md").exists(), (
        "a layer run overwrote the guarded published document"
    )
    assert inventory_json.exists(), result.stdout + result.stderr
    assert inventory_md.exists()

    data = json.loads(inventory_json.read_text(encoding="utf-8"))
    resource_classes = {entry["resource_class"] for entry in data}
    # Layer 4 resource classes are present, including the one that failed.
    for expected in (
        "lambda_actions",
        "api_routes",
        "iam_roles",
        "gateway_tools",
        "agents",
        "dynamodb_tables",
        "cognito_claims",
        "cloudfront",
    ):
        assert expected in resource_classes, f"{expected} missing from inventory"

    lambda_entry = next(e for e in data if e["resource_class"] == "lambda_actions")
    assert any(c["status"] == "fail" for c in lambda_entry["checks"])

    md_text = inventory_md.read_text(encoding="utf-8")
    assert "lambda_actions" in md_text
    assert "fail" in md_text


def test_run_rendering_is_labelled_unguarded_and_tallies_skip_and_defer(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A layer run leaves the published document untouched and its run rendering is labelled
    UNGUARDED, tallies skip and defer, and carries the deferred control's reason.
    """
    script_path, env = stubbed
    env = {
        **env,
        "STUB_SKIP_MODULES": "api_routes",
        "STUB_DEFER_MODULES": "cloudfront",
    }
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file), "--layer", "4"],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    published = tmp_path / "results" / "verification-inventory.md"
    run_md = tmp_path / "results" / "verification-inventory.run.md"
    assert run_md.exists(), result.stdout + result.stderr
    assert not published.exists(), "a --layer 4 run wrote the guarded published document"

    text = run_md.read_text(encoding="utf-8")
    assert "UNGUARDED" in text
    assert "publishable document is results/verification-inventory.md" in text
    assert "1 skipped, 1 deferred" in text, text.splitlines()[:6]
    assert "missing_custom_acm_certificate" in text, (
        "a deferred control's defer_reason is absent from the run rendering"
    )


def test_inventory_artifacts_are_never_written_into_the_real_repo_by_this_suite(
    stubbed: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """The staged script's REPO_ROOT is `tmp_path`, and no inventory artifact is tracked by git."""
    script_path, _ = stubbed
    assert script_path.parent.parent == tmp_path, (
        "the staged script's REPO_ROOT does not resolve to tmp_path, so a live run in this suite "
        "would write its inventory into the real repository"
    )
    script_text = script_path.read_text(encoding="utf-8")
    for variable in ("INVENTORY_JSON", "INVENTORY_MD", "RUN_INVENTORY_JSON", "RUN_INVENTORY_MD"):
        assert f'{variable}="${{REPO_ROOT}}/results/' in script_text, (
            f"{variable} is no longer derived from REPO_ROOT; the tmp_path isolation this suite "
            "relies on would no longer hold"
        )

    tracked = proc_run.run(
        [
            "git",
            "ls-files",
            "--",
            "results/verification-inventory.json",
            "results/verification-inventory.md",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.stdout.strip() == "", (
        "a consolidated inventory artifact is tracked by git; it must stay gitignored "
        f"(git ls-files returned: {tracked.stdout!r})"
    )


# ---------------------------------------------------------------------------
# 6. .gitignore coverage of every artifact path the script writes, proven via git check-ignore.
# ---------------------------------------------------------------------------


def _artifact_paths_from_script() -> list[str]:
    """Derive the artifact paths the script writes from its INVENTORY_JSON/INVENTORY_MD assignments
    and STDERR_LOG mktemp template, plus the results/verification-inventory/ and results/plan/
    artifact classes.
    """
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    paths = []
    for var in ("INVENTORY_JSON", "INVENTORY_MD"):
        match = re.search(rf'{var}="\$\{{REPO_ROOT\}}/([^"]+)"', text)
        assert match, f"could not find {var} assignment in {SCRIPT_PATH}"
        paths.append(match.group(1))
    # The mktemp template with its XXXXXX placeholder filled in.
    mktemp_match = re.search(r'mktemp "\$\{REPO_ROOT\}/(results/[^"]+XXXXXX[^"]*)"', text)
    assert mktemp_match, f"could not find the STDERR_LOG mktemp template in {SCRIPT_PATH}"
    paths.append(mktemp_match.group(1).replace("XXXXXX", "aB3xY9"))
    # Sibling artifact classes the script documents.
    paths.append("results/verification-inventory/some-run/detail.json")
    paths.append("results/plan/plan.json")
    return paths


#: The blanket `results/` catch-all at the end of `.gitignore`. It shadows every class-specific
#: `results/...` rule, so the class-coverage tests strip these lines in a scratch repository.
_BLANKET_RESULTS_RULES: tuple[str, ...] = ("results/*", "!results/matrix.md")


def _ignore_decision(path: str, cwd: Path) -> tuple[bool, str]:
    """Return `(is_ignored, matching_rule)` for `path` in the repository rooted at `cwd`.

    `git check-ignore -v` exits 0 on any match including a negation, so the decision comes from the
    plain form and only the rule from `-v`. `--no-index` ignores whether the path is tracked.
    """
    decision = proc_run.run(
        ["git", "check-ignore", "--no-index", path],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert decision.returncode in (0, 1), (
        f"git check-ignore failed for {path}: {decision.stdout}{decision.stderr}"
    )
    verbose = proc_run.run(
        ["git", "check-ignore", "--no-index", "-v", path],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return decision.returncode == 0, verbose.stdout.strip()


def _gitignore_without_blanket_results_rules() -> str:
    """Return the repository `.gitignore` with the blanket `results/` catch-all block removed.

    Fails if either catch-all line is absent or duplicated.
    """
    lines = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    for rule in _BLANKET_RESULTS_RULES:
        assert [line.strip() for line in lines].count(rule) == 1, (
            f"expected exactly one {rule!r} line in {GITIGNORE_PATH}; the blanket catch-all block "
            "has changed shape and these tests must be updated with it"
        )
    kept = [line for line in lines if line.strip() not in _BLANKET_RESULTS_RULES]
    return "\n".join(kept) + "\n"


def _scratch_repo_with_gitignore(tmp_path: Path, gitignore_text: str) -> Path:
    """Initialise an empty git repository under `tmp_path` carrying `gitignore_text`."""
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    proc_run.run(["git", "init", "-q"], cwd=repo, capture_output=True, text=True, check=True)
    (repo / ".gitignore").write_text(gitignore_text, encoding="utf-8")
    return repo


def test_every_artifact_path_the_script_writes_is_gitignored() -> None:
    artifact_paths = _artifact_paths_from_script()
    assert artifact_paths, "no artifact paths derived from the script"
    for path in artifact_paths:
        ignored, rule = _ignore_decision(path, REPO_ROOT)
        assert ignored, f"{path} is NOT ignored by any .gitignore rule (matched: {rule!r})"
        assert ".gitignore" in rule, rule


def test_every_artifact_path_is_covered_by_a_class_specific_rule_not_the_blanket_catch_all(
    tmp_path: Path,
) -> None:
    """Each artifact path is still ignored with the blanket `results/` catch-all removed."""
    repo = _scratch_repo_with_gitignore(tmp_path, _gitignore_without_blanket_results_rules())
    artifact_paths = _artifact_paths_from_script()
    assert artifact_paths, "no artifact paths derived from the script"
    for path in artifact_paths:
        ignored, rule = _ignore_decision(path, repo)
        assert ignored, (
            f"{path} is covered ONLY by the blanket results/ catch-all — no class-specific "
            f".gitignore rule ignores it (matched: {rule!r})"
        )


def test_gitignore_coverage_check_is_non_vacuous(tmp_path: Path) -> None:
    """Unignored witness paths fail both coverage assertions.

    The real-repository witness sits outside `results/`; the scratch-repository witness is under
    `results/` with the catch-all removed.
    """
    real_repo_witness = "definitely-not-covered-by-any-rule.tmp"
    ignored, rule = _ignore_decision(real_repo_witness, REPO_ROOT)
    assert not ignored, f"expected {real_repo_witness} to be unignored, but {rule!r} matched it"

    repo = _scratch_repo_with_gitignore(tmp_path, _gitignore_without_blanket_results_rules())
    uncovered_path = "results/definitely-not-covered-by-any-rule.tmp"
    scratch_ignored, scratch_rule = _ignore_decision(uncovered_path, repo)
    assert not scratch_ignored, (
        f"expected {uncovered_path} to be unignored by the class-specific rules alone, but "
        f"{scratch_rule!r} matched it"
    )


def test_gitignore_was_only_appended_to_not_reordered() -> None:
    """`.gitignore` carries the verification-inventory, plan and stderr-log rules."""
    text = GITIGNORE_PATH.read_text(encoding="utf-8")
    assert "results/verification-inventory*" in text
    assert "results/verification-inventory/" in text
    assert "results/plan/" in text
    assert "results/verify_deployment_stderr.*" in text


# ---------------------------------------------------------------------------
# 7. The static layer->script list in the shell script matches what's really on disk.
# ---------------------------------------------------------------------------


def _declared_layer(py_path: Path) -> int | None:
    """Return the module-level `LAYER = N` (or `LAYER: Final[int] = N`) in `py_path`, or None."""
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "LAYER":
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, int):
                        return value.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Name)
                and target.id == "LAYER"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, int)
            ):
                return node.value.value
    return None


def _real_layer_module_map() -> dict[int, set[str]]:
    """Map each declared `LAYER` to the `scripts/smoke/*.py` and `scripts/smoke_*.py` modules
    declaring it, excluding the `harness.py`/`tenant.py` libraries.
    """
    mapping: dict[int, set[str]] = {4: set(), 5: set(), 6: set()}
    for py_path in sorted(SMOKE_DIR.glob("*.py")):
        if py_path.name in ("__init__.py", "harness.py", "tenant.py"):
            continue
        layer = _declared_layer(py_path)
        if layer is None:
            continue
        module_name = f"scripts.smoke.{py_path.stem}"
        mapping[layer].add(module_name)

    for py_path in sorted((REPO_ROOT / "scripts").glob("smoke_*.py")):
        layer = _declared_layer(py_path)
        if layer is None:
            continue
        module_name = f"scripts.{py_path.stem}"
        mapping[layer].add(module_name)

    return mapping


def _declared_layer_arrays_from_script() -> dict[int, set[str]]:
    """Parse the LAYER_{4,5,6}_MODULES bash arrays out of the script's own source text."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    result: dict[int, set[str]] = {}
    for layer, varname in ((4, "LAYER_4_MODULES"), (5, "LAYER_5_MODULES"), (6, "LAYER_6_MODULES")):
        match = re.search(rf"{varname}=\(([^)]*)\)", text, re.DOTALL)
        assert match, f"could not find {varname} array in {SCRIPT_PATH}"
        entries = re.findall(r'"([^"]+)"', match.group(1))
        # Layer 6 entries may carry a trailing flag; only the module name is compared.
        modules = {entry.split()[0] for entry in entries}
        result[layer] = modules
    return result


def test_script_layer_lists_match_every_real_smoke_module_and_its_declared_layer() -> None:
    real = _real_layer_module_map()
    declared = _declared_layer_arrays_from_script()

    for layer in (4, 5, 6):
        assert declared[layer] == real[layer], (
            f"Layer {layer} mismatch between scripts/verify_deployment.sh's static list and "
            f"the real modules on disk with LAYER == {layer}.\n"
            f"  script declares: {sorted(declared[layer])}\n"
            f"  actually on disk: {sorted(real[layer])}\n"
            f"  missing from script: {sorted(real[layer] - declared[layer])}\n"
            f"  script names not found on disk (or with a different LAYER): "
            f"{sorted(declared[layer] - real[layer])}"
        )


def test_layer_list_drift_detection_is_non_vacuous(tmp_path: Path) -> None:
    """A synthetic extra Layer-4 module in a scratch smoke dir is flagged as missing."""
    scratch_smoke_dir = tmp_path / "smoke"
    scratch_smoke_dir.mkdir()
    for existing in SMOKE_DIR.glob("*.py"):
        (scratch_smoke_dir / existing.name).write_text(
            existing.read_text(encoding="utf-8"), encoding="utf-8"
        )
    new_script = scratch_smoke_dir / "brand_new_resource.py"
    new_script.write_text("LAYER = 4\nRESOURCE_CLASS = 'brand_new_resource'\n", encoding="utf-8")

    mapping: dict[int, set[str]] = {4: set(), 5: set(), 6: set()}
    for py_path in sorted(scratch_smoke_dir.glob("*.py")):
        if py_path.name in ("__init__.py", "harness.py", "tenant.py"):
            continue
        layer = _declared_layer(py_path)
        if layer is None:
            continue
        mapping[layer].add(f"scripts.smoke.{py_path.stem}")

    declared = _declared_layer_arrays_from_script()
    missing = mapping[4] - declared[4]
    assert missing == {"scripts.smoke.brand_new_resource"}, (
        f"expected exactly the injected new script to be flagged as missing from the static "
        f"list, got {missing}"
    )


# ---------------------------------------------------------------------------
# 8. A pre-layer failure and an interrupt both produce a real non-zero exit code.
# ---------------------------------------------------------------------------


def test_provisioning_failure_exits_nonzero_not_the_false_green_zero(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A provisioning failure exits non-zero rather than the initialised `LAYER_EXIT_CODE` of 0."""
    script_path, env = stubbed
    env = {**env, "STUB_MODE": "provision_fail"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode != 0, "provisioning failure must not report success"
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    assert "-m " not in call_log_text, "no layer script should run when provisioning fails"
    assert not (tmp_path / "results" / "verification-inventory.json").exists()


def test_sigterm_before_any_module_runs_exits_nonzero(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A SIGTERM delivered mid-run exits with the signal-derived code (143), not 0."""
    script_path, env = stubbed
    slow_stub = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        echo "$*" >> "${CALL_LOG}"
        if [[ "${1:-}" == "-c" ]]; then
          if [[ "$2" == *"allocate_smoke_tenant"* ]]; then
            echo '{"run_id": "smoke-deadbeef", "tenant": "eval-smoke-deadbeef"}'
            exit 0
          fi
          if [[ "$2" == *'json.load(sys.stdin)["run_id"]'* ]]; then
            # Drained before answering: the orchestrator pipes its captured result into this
            # stub, and a reader that exits without consuming leaves the writer on a broken pipe.
            # Under `set -o pipefail` that SIGPIPE becomes the pipeline status and kills the caller.
            cat >/dev/null
            echo "smoke-deadbeef"
            exit 0
          fi
          if [[ "$2" == *'json.load(sys.stdin)["tenant"]'* ]]; then
            # Drained before answering: the orchestrator pipes its captured result into this
            # stub, and a reader that exits without consuming leaves the writer on a broken pipe.
            # Under `set -o pipefail` that SIGPIPE becomes the pipeline status and kills the caller.
            cat >/dev/null
            echo "eval-smoke-deadbeef"
            exit 0
          fi
          if [[ "$2" == *"teardown_smoke_tenant"* ]]; then
            exit 0
          fi
          exit 0
        fi
        if [[ "${1:-}" == "-m" ]]; then
          sleep 5
          exit 0
        fi
        exit 99
        """
    )
    stub_python = tmp_path / "python3_stub_slow"
    stub_python.write_text(slow_stub, encoding="utf-8")
    stub_python.chmod(0o755)

    patched_text = script_path.read_text(encoding="utf-8")
    patched_text = re.sub(r'PYTHON_BIN="[^"]+"', f'PYTHON_BIN="{stub_python}"', patched_text)
    slow_script = script_path.parent / "verify_deployment_slow_exitcode.sh"
    slow_script.write_text(patched_text, encoding="utf-8")
    slow_script.chmod(0o755)

    proc = proc_run.popen(
        [
            "bash",
            str(slow_script),
            "--i-understand-this-is-billable",
            "--outputs",
            str(outputs_file),
        ],
        cwd=tmp_path,
        env=env,
        # The stub reads stdin for two `-c` calls, so an inherited open stdin would block them.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    call_log = Path(env["CALL_LOG"])
    aws_log = Path(env["AWS_STUB_CALL_LOG"])
    _wait_for_call_log(proc, call_log, "-m scripts.smoke.", "Layer 4 invocation", aws_log)

    proc.terminate()
    try:
        proc.communicate(timeout=_TRAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=5)

    assert proc.returncode != 0, "SIGTERM mid-run must not report the false-green exit code 0"


# ---------------------------------------------------------------------------
# 9. The orchestrator resolves scripts.* regardless of the caller's cwd.
# ---------------------------------------------------------------------------


def test_runs_correctly_when_invoked_from_outside_the_repo_tree(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """Invoked from outside the repo tree, the script carries its `cd "${REPO_ROOT}"` line and runs
    to completion.
    """
    script_path, env = stubbed
    outside_dir = tmp_path.parent
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        outside_dir,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    script_text = script_path.read_text(encoding="utf-8")
    assert 'cd "${REPO_ROOT}"' in script_text


def test_repo_root_module_resolution_depends_on_cd(tmp_path: Path) -> None:
    """Real `-m scripts.smoke.<mod> --help` fails with `ModuleNotFoundError` outside the repo and
    succeeds from the repo root, so the `cd "${REPO_ROOT}"` is operative.
    """
    python_bin = REPO_ROOT / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        pytest.skip(".venv not provisioned in this environment")

    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    outside_result = proc_run.run(
        [str(python_bin), "-m", "scripts.smoke.dynamodb_tables", "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert outside_result.returncode != 0
    assert "ModuleNotFoundError" in outside_result.stderr

    inside_result = proc_run.run(
        [str(python_bin), "-m", "scripts.smoke.dynamodb_tables", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert inside_result.returncode == 0, inside_result.stderr


# ---------------------------------------------------------------------------
# 10. --token/CRM_SMOKE_TOKEN threaded through; a skip never yields a pass.
# ---------------------------------------------------------------------------


def test_token_flag_is_threaded_through_only_to_token_accepting_module_invocations(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """The token is threaded to modules `TOKEN_ACCEPTING_MODULES` names and not to the others."""
    script_path, env = stubbed
    result = _run(
        script_path,
        [
            "--i-understand-this-is-billable",
            "--outputs",
            str(outputs_file),
            "--token",
            "fake-jwt-value",
        ],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    module_invocations = [line for line in call_log_text.splitlines() if line.startswith("-m ")]
    assert module_invocations, "no module invocations recorded"

    accepting_line = next(line for line in module_invocations if "scripts.smoke.api_routes" in line)
    assert "--token fake-jwt-value" in accepting_line, (
        f"--token not threaded through to a declaring module: {accepting_line}"
    )
    non_accepting_line = next(
        line for line in module_invocations if "scripts.smoke.lambda_actions" in line
    )
    assert "--token" not in non_accepting_line, (
        f"--token threaded to a non-declaring module (the exact C-new-2 regression): "
        f"{non_accepting_line}"
    )


def test_a_live_run_never_threads_the_user_token_to_a_gateway_module(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """Every Gateway module is invoked, and none of its invocations carries the user token.

    `api_routes` in the same run is the control: it does receive the token.
    """
    script_path, env = stubbed
    result = _run(
        script_path,
        [
            "--i-understand-this-is-billable",
            "--outputs",
            str(outputs_file),
            "--token",
            "fake-jwt-value",
        ],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    module_invocations = [line for line in call_log_text.splitlines() if line.startswith("-m ")]
    control = next(line for line in module_invocations if "scripts.smoke.api_routes" in line)
    assert "--token fake-jwt-value" in control
    for module in sorted(_GATEWAY_MODULES):
        lines = [line for line in module_invocations if line.split()[1] == module]
        assert len(lines) == 1, f"expected one invocation of {module}, got {lines}"
        assert "--token" not in lines[0].split() and "fake-jwt-value" not in lines[0], (
            f"the orchestrator handed its user token to the Gateway module {module}: {lines[0]}"
        )


def test_crm_smoke_token_env_var_is_honoured_when_no_flag_given(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    env = {**env, "CRM_SMOKE_TOKEN": "env-jwt-value"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file)],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    module_invocations = [line for line in call_log_text.splitlines() if line.startswith("-m ")]
    assert module_invocations
    accepting_line = next(line for line in module_invocations if "scripts.smoke.api_routes" in line)
    assert "--token env-jwt-value" in accepting_line
    non_accepting_line = next(
        line for line in module_invocations if "scripts.smoke.lambda_actions" in line
    )
    assert "--token" not in non_accepting_line


def test_token_flag_overrides_env_var(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    env = {**env, "CRM_SMOKE_TOKEN": "env-jwt-value"}
    result = _run(
        script_path,
        [
            "--i-understand-this-is-billable",
            "--outputs",
            str(outputs_file),
            "--token",
            "flag-jwt-value",
        ],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    assert "--token flag-jwt-value" in call_log_text
    assert "--token env-jwt-value" not in call_log_text


def test_help_mentions_token_flag_and_env_var(
    stubbed: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, ["--help"], env, tmp_path)
    assert result.returncode == 0
    assert "--token" in result.stdout
    assert "CRM_SMOKE_TOKEN" in result.stdout


def test_a_skip_is_visible_but_does_not_fail_the_aggregate_run(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A skip is counted and named by resource class but does not fail the aggregate run."""
    script_path, env = stubbed
    env = {**env, "STUB_SKIP_MODULES": "api_routes"}
    result = _run(
        script_path,
        [
            "--i-understand-this-is-billable",
            "--outputs",
            str(outputs_file),
            "--token",
            "jwt",
        ],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL RUN LAYERS PASSED" in result.stdout
    assert "1 check(s) skipped" in result.stderr
    assert "api_routes" in result.stderr


def test_a_defer_is_visible_and_never_reads_as_a_verified_control(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A deferred control does not fail the aggregate run, is counted separately from a skip, and
    the pass line discloses it."""
    script_path, env = stubbed
    env = {**env, "STUB_DEFER_MODULES": "cloudfront"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file), "--token", "jwt"],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 check(s) DEFERRED" in result.stderr
    assert "cloudfront" in result.stderr
    # A defer is not laundered into a skip, and the pass line discloses it.
    assert "check(s) skipped" not in result.stderr
    assert "WITH 1 DEFERRED CONTROL(S) NOT VERIFIED" in result.stdout


def test_a_genuine_failure_alongside_a_defer_still_fails_the_aggregate_run(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A deferred control does not suppress a real failure in the same run."""
    script_path, env = stubbed
    env = {
        **env,
        "STUB_DEFER_MODULES": "cloudfront",
        "STUB_FAIL_MODULES": "lambda_actions",
    }
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file), "--token", "jwt"],
        env,
        tmp_path,
    )
    assert result.returncode == 1
    assert "ONE OR MORE LAYERS FAILED" in result.stderr
    assert "ALL RUN LAYERS PASSED" not in result.stdout


def test_no_skips_still_yields_an_aggregate_pass(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A clean run with zero skips passes and prints no "N check(s) skipped" line."""
    script_path, env = stubbed
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file), "--token", "jwt"],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "ALL RUN LAYERS PASSED" in result.stdout
    assert "check(s) skipped" not in result.stderr


def test_a_genuine_failure_alongside_a_skip_still_fails_the_aggregate_run(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A skip does not suppress a real failure in the same run."""
    script_path, env = stubbed
    env = {**env, "STUB_SKIP_MODULES": "api_routes", "STUB_FAIL_MODULES": "lambda_actions"}
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file), "--token", "jwt"],
        env,
        tmp_path,
    )
    assert result.returncode != 0, "a genuine fail must still fail the run even with a skip present"
    assert "ALL RUN LAYERS PASSED" not in result.stdout
    assert "ONE OR MORE LAYERS FAILED" in result.stderr


# ---------------------------------------------------------------------------
# 10b. --token is threaded only to the real modules that declare it, derived at runtime by
#      AST-scanning each module's source. These tests invoke the real module CLIs with the
#      orchestrator's argument vector and a nonexistent --outputs path, so each fails fast at the
#      outputs stage with no AWS contact.
# ---------------------------------------------------------------------------


#: Modules under `scripts/smoke/` that are libraries the smoke scripts import, not runnable entry
#: points; `test_every_excluded_smoke_module_is_genuinely_a_library` checks each member.
_SMOKE_LIBRARY_MODULES: Final[tuple[str, ...]] = (
    "__init__.py",
    "harness.py",
    "tenant.py",
    "demo_tenant_guard.py",
    "mfa_user.py",
    "verified_claims.py",
    "runtime_invoke.py",
)

#: Runnable modules under `scripts/smoke/` that are not layer check modules: the orchestrator
#: invokes them by name for lifecycle work and they emit no `CheckResult`. `smoke_token.py` mints
#: the run-tenant-bound `CRM_SMOKE_TOKEN` and removes its user.
_SMOKE_CLI_HELPER_MODULES: Final[tuple[str, ...]] = ("smoke_token.py",)

#: Every runnable smoke module that must exist on disk, pinned by name.
_EXPECTED_REAL_SMOKE_MODULES: Final[frozenset[str]] = frozenset(
    {
        "scripts.smoke.access_log_delivery",
        "scripts.smoke.agents",
        "scripts.smoke.api_routes",
        "scripts.smoke.public_exposure",
        "scripts.smoke.cloudfront",
        "scripts.smoke.cognito_claims",
        "scripts.smoke.deployed_parity",
        "scripts.smoke.dynamodb_tables",
        "scripts.smoke.gateway_tools",
        "scripts.smoke.iam_roles",
        "scripts.smoke.iam_policy_budget",
        "scripts.smoke.integration_gateway_scope",
        "scripts.smoke.integration_proposal_chain",
        "scripts.smoke.integration_ui_api_ddb",
        "scripts.smoke.eval_sfn",
        "scripts.smoke.admin_authz",
        "scripts.smoke.user_settings_api",
        "scripts.smoke.chat_timeouts",
        "scripts.smoke.chat_path_iam",
        "scripts.smoke.mcp_chat_gateway",
        "scripts.smoke.mcp_client_compat",
        "scripts.smoke.eni_function_code_deny",
        "scripts.smoke.resource_protection",
        "scripts.smoke.dynamodb_principal_deny",
        "scripts.smoke.store_principal_deny",
        "scripts.smoke_text_delta_stream",
        "scripts.smoke.three_tier_chat",
        "scripts.smoke.lambda_actions",
        "scripts.smoke.relay_iam",
        "scripts.smoke.runtime_bedrock_iam",
        "scripts.smoke.runtime_inbound_auth",
        "scripts.smoke.runtime_network_posture",
        "scripts.smoke.integration_teardown_privilege",
        "scripts.smoke_agent_stream",
        "scripts.smoke_proposal_card",
    }
)


def _all_real_smoke_modules() -> list[str]:
    """Every real `scripts.smoke.<mod>` and `scripts.<mod>` (smoke_*) module name on disk.

    Independent of `LAYER`; excludes :data:`_SMOKE_LIBRARY_MODULES` and
    :data:`_SMOKE_CLI_HELPER_MODULES`.
    """
    modules = []
    for py_path in sorted(SMOKE_DIR.glob("*.py")):
        if py_path.name in _SMOKE_LIBRARY_MODULES or py_path.name in _SMOKE_CLI_HELPER_MODULES:
            continue
        modules.append(f"scripts.smoke.{py_path.stem}")
    for py_path in sorted((REPO_ROOT / "scripts").glob("smoke_*.py")):
        modules.append(f"scripts.{py_path.stem}")
    return modules


def _assert_smoke_module_inventory(modules: list[str]) -> None:
    """Assert the discovered smoke modules are exactly the pinned set, by name."""
    discovered = set(modules)
    assert discovered == set(_EXPECTED_REAL_SMOKE_MODULES), (
        "the runnable smoke-module inventory drifted from the pinned set; "
        f"missing={sorted(_EXPECTED_REAL_SMOKE_MODULES - discovered)} "
        f"unexpected={sorted(discovered - _EXPECTED_REAL_SMOKE_MODULES)}"
    )
    assert len(modules) == len(discovered), f"duplicate smoke module in {modules}"


def test_the_runnable_smoke_module_inventory_is_pinned_by_name() -> None:
    """Excluding a real smoke script fails the pin even when the count is unchanged."""
    _assert_smoke_module_inventory(_all_real_smoke_modules())

    dropped = "scripts.smoke.integration_gateway_scope"
    tampered = [m for m in _all_real_smoke_modules() if m != dropped]
    tampered.append("scripts.smoke.demo_tenant_guard")
    assert len(tampered) == len(_all_real_smoke_modules()), (
        "the simulated fail-open edit must keep the count identical, which is the whole point: a\n"
        "count assertion cannot tell this edit from a correct inventory"
    )
    with pytest.raises(AssertionError, match="inventory drifted"):
        _assert_smoke_module_inventory(tampered)


@pytest.mark.parametrize(
    "library", [name for name in _SMOKE_LIBRARY_MODULES if name != "__init__.py"]
)
def test_every_excluded_smoke_module_is_genuinely_a_library(library: str) -> None:
    """Each excluded library module has no `main` and no `LAYER`."""
    tree = ast.parse((SMOKE_DIR / library).read_text(encoding="utf-8"))
    top_level_functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    } | {
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert "main" not in top_level_functions, f"{library} defines main(); it is a runnable script"
    assert "LAYER" not in assigned, f"{library} declares LAYER; it is a runnable smoke script"


@pytest.mark.parametrize("helper", _SMOKE_CLI_HELPER_MODULES)
def test_every_excluded_module_is_a_cli_helper_and_not_a_forgotten_layer(helper: str) -> None:
    """Each CLI helper defines `main`, declares no `LAYER`, and is invoked by name in
    `verify_deployment.sh` outside the `LAYER_N_MODULES` arrays.
    """
    tree = ast.parse((SMOKE_DIR / helper).read_text(encoding="utf-8"))
    top_level_functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    } | {
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert "main" in top_level_functions, (
        f"{helper} defines no main(); it is a library and belongs in _SMOKE_LIBRARY_MODULES"
    )
    assert "LAYER" not in assigned, (
        f"{helper} declares LAYER, so it IS a layer check module and must appear in a "
        f"LAYER_N_MODULES array rather than being excluded from the inventory"
    )

    module_name = f"scripts.smoke.{helper.removesuffix('.py')}"
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert f"-m {module_name}" in script_text or module_name in script_text, (
        f"{helper} is excluded from the check-module inventory as an orchestrator CLI helper, but "
        f"{module_name!r} appears nowhere in {SCRIPT_PATH.name} — so nothing invokes it and the "
        f"exclusion is hiding a module rather than classifying one"
    )
    for array_name in ("LAYER_4_MODULES", "LAYER_5_MODULES", "LAYER_6_MODULES"):
        array_body = script_text.split(f"{array_name}=(", 1)
        assert len(array_body) == 2, f"could not find {array_name} in {SCRIPT_PATH.name}"
        assert module_name not in array_body[1].split(")", 1)[0], (
            f"{module_name} is listed in {array_name}, so it is a layer check module and must not "
            f"be excluded from the inventory"
        )


def _declares_token(module_name: str) -> bool:
    """Return whether `module_name`'s AST contains the string constant `"--token"`, as the
    orchestrator's runtime scan does.
    """
    if module_name.startswith("scripts.smoke."):
        py_path = SMOKE_DIR / f"{module_name.removeprefix('scripts.smoke.')}.py"
    else:
        py_path = REPO_ROOT / "scripts" / f"{module_name.removeprefix('scripts.')}.py"
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Constant) and node.value == "--token" for node in ast.walk(tree)
    )


def test_reproduces_the_real_modules_that_reject_an_unconditional_token(
    tmp_path: Path,
) -> None:
    """With `--token` threaded unconditionally, exactly the modules not declaring it fail with
    `unrecognized arguments: --token`.

    Uses a nonexistent --outputs path and a credential-neutered environment, so no AWS call is made.
    """
    python_bin = REPO_ROOT / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        pytest.skip(".venv not provisioned in this environment")

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    nonexistent_outputs = str(tmp_path / "does-not-exist.json")

    rejects_token: list[str] = []
    for module in _all_real_smoke_modules():
        result = proc_run.run(
            [str(python_bin), "-m", module, "--outputs", nonexistent_outputs, "--token", "x"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if result.returncode == 2 and "unrecognized arguments: --token" in result.stderr:
            rejects_token.append(module)

    expected_rejecters = {
        "scripts.smoke.access_log_delivery",
        "scripts.smoke.deployed_parity",
        "scripts.smoke.lambda_actions",
        "scripts.smoke.iam_roles",
        "scripts.smoke.dynamodb_tables",
        "scripts.smoke.cognito_claims",
        "scripts.smoke.cloudfront",
        "scripts.smoke.eval_sfn",
        "scripts.smoke.admin_authz",
        "scripts.smoke.user_settings_api",
        "scripts.smoke.chat_timeouts",
        "scripts.smoke.chat_path_iam",
        "scripts.smoke.mcp_chat_gateway",
        "scripts.smoke.mcp_client_compat",
        "scripts.smoke.eni_function_code_deny",
        "scripts.smoke.resource_protection",
        "scripts.smoke.dynamodb_principal_deny",
        "scripts.smoke.store_principal_deny",
        "scripts.smoke.three_tier_chat",
        "scripts.smoke_text_delta_stream",
        "scripts.smoke.relay_iam",
        "scripts.smoke.runtime_inbound_auth",
        "scripts.smoke.runtime_bedrock_iam",
        "scripts.smoke.runtime_network_posture",
        "scripts.smoke.public_exposure",
        "scripts.smoke.agents",
        "scripts.smoke.iam_policy_budget",
        "scripts.smoke.integration_teardown_privilege",
        # The Gateway modules take `--gateway-token` (a self-minted machine token), not `--token`.
        "scripts.smoke.gateway_tools",
        "scripts.smoke.integration_gateway_scope",
        "scripts.smoke_agent_stream",
        "scripts.smoke_proposal_card",
    }
    assert set(rejects_token) == expected_rejecters, (
        f"expected exactly {sorted(expected_rejecters)} to reject an unconditional --token, "
        f"got {sorted(rejects_token)}"
    )


def test_every_real_module_accepts_the_orchestrators_actual_argument_vector(
    tmp_path: Path,
) -> None:
    """With `--token` passed only to modules `_declares_token` selects, no real module exits with a
    usage error.

    A nonexistent --outputs path makes every module fail fast at the outputs stage with no AWS call.
    """
    python_bin = REPO_ROOT / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        pytest.skip(".venv not provisioned in this environment")

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    nonexistent_outputs = str(tmp_path / "does-not-exist.json")

    modules = _all_real_smoke_modules()
    _assert_smoke_module_inventory(modules)

    usage_errors: list[str] = []
    for module in modules:
        args = [str(python_bin), "-m", module, "--outputs", nonexistent_outputs]
        if _declares_token(module):
            args += ["--token", "x"]
        result = proc_run.run(
            args,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if result.returncode == 2 or "unrecognized arguments" in result.stderr:
            usage_errors.append(f"{module}: exit={result.returncode} stderr={result.stderr!r}")

    assert usage_errors == [], (
        "every real module must accept the orchestrator's own argument vector without a usage "
        "error; failures:\n" + "\n".join(usage_errors)
    )


#: `smoke_token`'s subcommands, every one of which the orchestrator must invoke.
_SMOKE_TOKEN_SUBCOMMANDS: Final[frozenset[str]] = frozenset({"mint", "delete-user", "assert-token"})


def _orchestrator_smoke_token_vectors() -> list[list[str]]:
    """Every `-m scripts.smoke.smoke_token ...` argument vector `verify_deployment.sh` emits.

    Shell variables are replaced with placeholders; only the order and flag names are kept.
    """
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    joined = re.sub(r"\\\n\s*", " ", text)  # collapse line continuations
    placeholders = {
        "${PYTHON_BIN}": "PYTHON",
        "${RESOLVED_PROFILE}": "default",
        "${RESOLVED_REGION}": "us-east-1",
        "${OUTPUTS_PATH}": "OUTPUTS",
        "${SMOKE_RUN_ID}": "smoke-0000",
        "${SMOKE_TENANT}": "eval-smoke-0000",
        "${SMOKE_USERNAME}": "someone",
        "${SMOKE_TOKEN}": "a.b.c",
    }
    vectors: list[list[str]] = []
    for line in joined.splitlines():
        if "-m scripts.smoke.smoke_token" not in line:
            continue
        fragment = line.split("-m scripts.smoke.smoke_token", 1)[1]
        fragment = fragment.split(">/dev/null", 1)[0].split(')"', 1)[0].split("; then", 1)[0]
        for shell_var, value in placeholders.items():
            fragment = fragment.replace(shell_var, value)
        tokens = [tok.strip('"') for tok in fragment.split() if tok not in ("\\", "")]
        if tokens:
            vectors.append(tokens)
    return vectors


def test_the_orchestrators_smoke_token_vectors_are_accepted_by_the_real_cli(
    tmp_path: Path,
) -> None:
    """The real `smoke_token` parser accepts every argument vector the orchestrator emits.

    `--profile/--region/--outputs` belong to the parent parser and must precede the subcommand. A
    nonexistent `--outputs` makes the module fail fast with no AWS call.
    """
    python_bin = REPO_ROOT / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        pytest.skip(".venv not provisioned in this environment")

    vectors = _orchestrator_smoke_token_vectors()
    assert len(vectors) == 3, (
        f"expected the mint, delete-user and assert-token invocations, parsed {len(vectors)}: "
        f"{vectors}"
    )
    subcommands = {tok for v in vectors for tok in v if tok in _SMOKE_TOKEN_SUBCOMMANDS}
    assert subcommands == _SMOKE_TOKEN_SUBCOMMANDS, (
        f"the orchestrator does not invoke every smoke_token subcommand: missing "
        f"{sorted(_SMOKE_TOKEN_SUBCOMMANDS - subcommands)}"
    )

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    nonexistent_outputs = str(tmp_path / "does-not-exist.json")

    usage_errors: list[str] = []
    for vector in vectors:
        args = [
            str(python_bin),
            "-m",
            "scripts.smoke.smoke_token",
            *[nonexistent_outputs if tok == "OUTPUTS" else tok for tok in vector],
        ]
        result = proc_run.run(
            args, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, check=False
        )
        if result.returncode == 2 or "unrecognized arguments" in result.stderr:
            usage_errors.append(
                f"{' '.join(vector)}: exit={result.returncode} stderr={result.stderr[-300:]!r}"
            )

    assert usage_errors == [], (
        "the orchestrator emits a smoke_token vector the real CLI rejects as a usage error:\n"
        + "\n".join(usage_errors)
    )


def test_no_embedded_python_snippet_contains_command_substitution() -> None:
    """No double-quoted embedded snippet contains a backtick or `$(`, which the shell would execute.

    `${...}` parameter expansion is allowed.
    """
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    offenders: list[str] = []
    for match in re.finditer(r'-c "\n(.*?)\n"\)?', text, re.DOTALL):
        snippet = match.group(1)
        start_line = text[: match.start()].count("\n") + 2
        for offset, line in enumerate(snippet.splitlines()):
            found = [syntax for syntax in ("`", "$(") if syntax in line]
            if found:
                offenders.append(
                    f"{SCRIPT_PATH.name}:{start_line + offset}: {found} in: {line.strip()}"
                )

    assert offenders == [], (
        "command substitution inside an embedded Python snippet — the shell will execute the "
        "enclosed text as a command before Python sees the source:\n" + "\n".join(offenders)
    )


def test_the_backtick_check_covers_the_expanded_snippet_and_only_it() -> None:
    """The extraction finds the one double-quoted snippet, and the provision/teardown snippets are
    single-quoted.
    """
    text = SCRIPT_PATH.read_text(encoding="utf-8")

    double_quoted = re.findall(r'-c "\n(.*?)\n"\)?', text, re.DOTALL)
    assert len(double_quoted) == 1, (
        f"expected exactly one double-quoted embedded snippet, found {len(double_quoted)}. If a "
        "second was added it is also shell-expanded and must be covered by the backtick check."
    )
    assert "import ast" in double_quoted[0], (
        "the extracted double-quoted snippet is not the TOKEN_ACCEPTING_MODULES AST scan, so the "
        "check is looking at the wrong block"
    )

    single_quoted = re.findall(r"-c '\n(.*?)\n' ", text, re.DOTALL)
    assert len(single_quoted) >= 2, (
        f"expected the provision/teardown snippets to be single-quoted, found {len(single_quoted)}; "
        "if one became double-quoted it is now shell-expanded and needs covering"
    )

    # The predicate discriminates: a backtick in a snippet is detected.
    assert "`" in "x = 1  # `whoami`"
    assert "`" not in double_quoted[0]


def test_the_smoke_token_vector_check_can_actually_fail(tmp_path: Path) -> None:
    """Parent options placed after the subcommand are rejected by the real parser."""
    python_bin = REPO_ROOT / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        pytest.skip(".venv not provisioned in this environment")

    result = proc_run.run(
        [
            str(python_bin),
            "-m",
            "scripts.smoke.smoke_token",
            "mint",
            "--run-id",
            "smoke-0000",
            "--tenant",
            "eval-smoke-0000",
            # The rejected ordering: parent-level options AFTER the subcommand.
            "--profile",
            "default",
            "--region",
            "us-east-1",
            "--outputs",
            str(tmp_path / "does-not-exist.json"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
        check=False,
    )
    assert result.returncode == 2 and "unrecognized arguments" in result.stderr, (
        "the pre-fix ordering no longer produces a usage error, so the cell above would pass "
        f"whatever the orchestrator emitted: exit={result.returncode} stderr={result.stderr!r}"
    )


def test_token_accepting_module_set_derived_by_orchestrator_matches_the_declaring_scripts() -> None:
    """`_declares_token` selects exactly the modules that declare `--token`, by either mechanism
    (`run_main(..., extra_arguments=[...])` or a module's own pre-strip).
    """
    expected = {
        "scripts.smoke.api_routes",
        "scripts.smoke.integration_ui_api_ddb",
        "scripts.smoke.integration_proposal_chain",
    }
    actual = {module for module in _all_real_smoke_modules() if _declares_token(module)}
    assert actual == expected, (
        f"AST-derived token-accepting set mismatch.\n  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(actual)}"
    )


#: Every module that calls the AgentCore Gateway. Each mints its own `client_credentials` token
#: carrying `gateway/invoke` and takes an operator override only as `--gateway-token`.
_GATEWAY_MODULES: Final[frozenset[str]] = frozenset(
    {
        "scripts.smoke.gateway_tools",
        "scripts.smoke.integration_gateway_scope",
    }
)


def test_the_orchestrator_never_hands_the_user_token_to_a_gateway_module() -> None:
    """No Gateway module appears in the derived token set, yet each stays in the module inventory.

    A user ID token carries no `gateway/invoke`, so handing one to a Gateway module displaces the
    machine token it mints and the Gateway refuses every call with `insufficient_scope`.
    `api_routes` is the control: it must stay token-accepting, so an empty derivation
    cannot pass this test.
    """
    modules = _all_real_smoke_modules()
    accepting = {module for module in modules if _declares_token(module)}
    assert "scripts.smoke.api_routes" in accepting, (
        "the control module api_routes is not token-accepting, so the derivation selected nothing "
        "and the exclusion below would pass vacuously"
    )
    leaked = sorted(_GATEWAY_MODULES & accepting)
    assert leaked == [], (
        f"{leaked} are in the orchestrator's derived token set, so the run will hand them a user "
        "ID token and displace the client_credentials token each mints for itself. Their flag must "
        "stay spelled --gateway-token."
    )
    assert _GATEWAY_MODULES.issubset(modules)


def test_the_gateway_modules_ignore_an_exported_crm_smoke_token() -> None:
    """No Gateway module reads `CRM_SMOKE_TOKEN`, so an operator export for `/api/*` work cannot
    poison them; each reads `CRM_SMOKE_GATEWAY_TOKEN` instead.
    """
    for module in sorted(_GATEWAY_MODULES):
        path = REPO_ROOT / Path(*module.split(".")).with_suffix(".py")
        constants = {
            node.value
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "CRM_SMOKE_TOKEN" not in constants, f"{module} still reads CRM_SMOKE_TOKEN"
        assert "CRM_SMOKE_GATEWAY_TOKEN" in constants, (
            f"{module} reads no CRM_SMOKE_GATEWAY_TOKEN, so the check above passed for a module "
            "that resolves no operator token at all"
        )


def test_orchestrators_runtime_token_derivation_matches_the_test_helpers_derivation(
    tmp_path: Path,
) -> None:
    """The orchestrator's embedded AST scan matches `_declares_token`."""
    python_bin = REPO_ROOT / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        pytest.skip(".venv not provisioned in this environment")

    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    match = re.search(
        r'TOKEN_ACCEPTING_MODULES="\$\("\$\{PYTHON_BIN\}" -c "\n(.*?)\n"\)"',
        script_text,
        re.DOTALL,
    )
    assert match, "could not find the TOKEN_ACCEPTING_MODULES derivation snippet in the script"
    snippet = match.group(1).replace("\\$", "$").replace('\\"', '"')
    snippet = snippet.replace("${REPO_ROOT}", str(REPO_ROOT))

    result = proc_run.run(
        [str(python_bin), "-c", snippet],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    from_script = set(result.stdout.split())
    from_helper = {module for module in _all_real_smoke_modules() if _declares_token(module)}
    assert from_script == from_helper, (
        f"script-embedded derivation ({sorted(from_script)}) diverges from this test's "
        f"independent re-implementation ({sorted(from_helper)})"
    )


# ---------------------------------------------------------------------------
# `--dry-run`'s previewed argument vector equals, module for module, what a live run executes:
# `--token` is previewed for exactly the modules that declare it.
# ---------------------------------------------------------------------------


def test_dry_run_preview_threads_token_only_to_declaring_modules_matching_a_live_run(
    tmp_path: Path,
) -> None:
    """In the real `--dry-run` preview with a token set, each module's line carries `--token` if and
    only if `_declares_token` says the module declares it.
    """
    modules = _all_real_smoke_modules()
    _assert_smoke_module_inventory(modules)
    expected_accepting = {m for m in modules if _declares_token(m)}
    assert expected_accepting == {
        "scripts.smoke.api_routes",
        "scripts.smoke.integration_ui_api_ddb",
        "scripts.smoke.integration_proposal_chain",
    }

    result = proc_run.run(
        ["bash", str(SCRIPT_PATH), "--dry-run"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CRM_SMOKE_TOKEN": "fake-preview-token",
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr

    preview_lines = [line for line in result.stdout.splitlines() if line.startswith("would run:")]
    assert len(preview_lines) == len(_EXPECTED_REAL_SMOKE_MODULES), (
        f"expected {len(_EXPECTED_REAL_SMOKE_MODULES)} preview lines, got "
        f"{len(preview_lines)}:\n" + "\n".join(preview_lines)
    )

    # A preview line's module is matched by substring; Layer 6 entries carry a trailing flag.
    line_by_module: dict[str, str] = {}
    for module in modules:
        matches = [
            line
            for line in preview_lines
            if f" -m {module} " in line or line.endswith(f" -m {module}")
        ]
        assert len(matches) == 1, (
            f"expected exactly one preview line for {module}, found {len(matches)}: {matches}"
        )
        line_by_module[module] = matches[0]

    token_present = {m for m, line in line_by_module.items() if "--token" in line}
    assert token_present == expected_accepting, (
        "the --dry-run preview's --token membership diverges from a live run's:\n"
        f"  previewed with --token:      {sorted(token_present)}\n"
        f"  should have --token (live):  {sorted(expected_accepting)}\n"
        f"  wrongly previewed WITH --token (would fail exit 2 if run live): "
        f"{sorted(token_present - expected_accepting)}\n"
        f"  wrongly previewed WITHOUT --token (would silently skip an authenticated check): "
        f"{sorted(expected_accepting - token_present)}"
    )

    # The secret value itself must never be echoed into the preview, only the flag.
    assert "fake-preview-token" not in result.stdout


def test_dry_run_preview_reproduces_the_pre_fix_regression_when_forced_unconditional(
    tmp_path: Path,
) -> None:
    """The modules that do not declare `--token` are exactly the pinned set, so an unconditional
    `--token` preview would put them in the token-bearing set the test above forbids.
    """
    modules = _all_real_smoke_modules()
    non_accepting = {m for m in modules if not _declares_token(m)}
    assert non_accepting == {
        "scripts.smoke.access_log_delivery",
        "scripts.smoke.eval_sfn",
        "scripts.smoke.admin_authz",
        "scripts.smoke.user_settings_api",
        "scripts.smoke.chat_timeouts",
        "scripts.smoke.chat_path_iam",
        "scripts.smoke.mcp_chat_gateway",
        "scripts.smoke.mcp_client_compat",
        "scripts.smoke.eni_function_code_deny",
        "scripts.smoke.resource_protection",
        "scripts.smoke.dynamodb_principal_deny",
        "scripts.smoke.store_principal_deny",
        "scripts.smoke.three_tier_chat",
        "scripts.smoke_text_delta_stream",
        "scripts.smoke.relay_iam",
        "scripts.smoke.runtime_inbound_auth",
        "scripts.smoke.runtime_bedrock_iam",
        "scripts.smoke.runtime_network_posture",
        "scripts.smoke.deployed_parity",
        "scripts.smoke.lambda_actions",
        "scripts.smoke.iam_roles",
        "scripts.smoke.dynamodb_tables",
        "scripts.smoke.cognito_claims",
        "scripts.smoke.cloudfront",
        "scripts.smoke.public_exposure",
        "scripts.smoke.agents",
        "scripts.smoke.iam_policy_budget",
        "scripts.smoke.integration_teardown_privilege",
        # The Gateway modules take `--gateway-token` (a self-minted machine token), not `--token`.
        "scripts.smoke.gateway_tools",
        "scripts.smoke.integration_gateway_scope",
        "scripts.smoke_agent_stream",
        "scripts.smoke_proposal_card",
    }


# ---------------------------------------------------------------------------
# 11. Unique-per-run stderr log (not a fixed /tmp path), named in the failure line; a
#     script with no parseable stdout still appears in the inventory as a failed resource class.
# ---------------------------------------------------------------------------


def test_stderr_log_path_is_unique_per_run_not_a_fixed_tmp_path(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path,
        ["--i-understand-this-is-billable", "--outputs", str(outputs_file), "--token", "jwt"],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    results_dir = tmp_path / "results"
    stderr_logs = list(results_dir.glob("verify_deployment_stderr.*"))
    assert len(stderr_logs) == 1, f"expected exactly one stderr log, found {stderr_logs}"
    assert "/tmp/verify_deployment_stderr.log" not in result.stdout + result.stderr  # nosec B108  # asserts absence
    # BSD mktemp substitutes only a trailing `XXXXXX`; assert the path is not the literal template.
    assert "XXXXXX" not in stderr_logs[0].name, (
        f"mktemp template was not substituted -- path is still fixed: {stderr_logs[0]}"
    )


def test_script_with_no_parseable_output_appears_in_inventory_as_a_failed_class(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """A script producing no parseable stdout still appears in the inventory as a failed resource
    class whose synthetic CheckResult names the stderr log.
    """
    script_path, env = stubbed
    env = {**env, "STUB_MODE": "empty_output"}
    result = _run(
        script_path,
        [
            "--i-understand-this-is-billable",
            "--outputs",
            str(outputs_file),
            "--layer",
            "4",
            "--token",
            "jwt",
        ],
        env,
        tmp_path,
    )
    assert result.returncode != 0

    inventory_json = tmp_path / "results" / "verification-inventory.run.json"
    assert inventory_json.exists(), result.stdout + result.stderr
    document = json.loads(inventory_json.read_text(encoding="utf-8"))
    assert document["partial"] is True and document["partial_selector"] == "--layer 4"
    data = document["results"]
    resource_classes = {entry["resource_class"] for entry in data}
    assert "lambda_actions" in resource_classes, (
        "a script with no parseable stdout must still appear in the inventory as a failed "
        f"resource class, not vanish from it. Inventory resource classes: {resource_classes}"
    )
    lambda_entry = next(e for e in data if e["resource_class"] == "lambda_actions")
    assert any(c["status"] == "fail" for c in lambda_entry["checks"])
    detail_text = " ".join(c.get("detail", "") for c in lambda_entry["checks"])
    assert "verify_deployment_stderr" in detail_text, (
        f"failing synthetic check must name the stderr log path: {detail_text}"
    )


def test_stderr_log_path_is_gitignored() -> None:
    """The stderr log path derived from the script's mktemp template is gitignored."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    mktemp_match = re.search(r'mktemp "\$\{REPO_ROOT\}/(results/[^"]+XXXXXX[^"]*)"', text)
    assert mktemp_match, f"could not find the STDERR_LOG mktemp template in {SCRIPT_PATH}"
    example_path = mktemp_match.group(1).replace("XXXXXX", "aB3xY9")

    ignored, rule = _ignore_decision(example_path, REPO_ROOT)
    assert ignored, f"{example_path} is NOT ignored by any .gitignore rule (matched: {rule!r})"
    assert ".gitignore" in rule, rule


def test_stderr_log_path_is_covered_by_its_own_rule_not_the_blanket_catch_all(
    tmp_path: Path,
) -> None:
    """The stderr-log rule must cover the path on its own, without the blanket catch-all."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    mktemp_match = re.search(r'mktemp "\$\{REPO_ROOT\}/(results/[^"]+XXXXXX[^"]*)"', text)
    assert mktemp_match, f"could not find the STDERR_LOG mktemp template in {SCRIPT_PATH}"
    example_path = mktemp_match.group(1).replace("XXXXXX", "aB3xY9")

    repo = _scratch_repo_with_gitignore(tmp_path, _gitignore_without_blanket_results_rules())
    ignored, rule = _ignore_decision(example_path, repo)
    assert ignored, (
        f"{example_path} is covered ONLY by the blanket results/ catch-all — no class-specific "
        f".gitignore rule ignores the stderr log (matched: {rule!r})"
    )


def test_stderr_log_gitignore_coverage_is_non_vacuous(tmp_path: Path) -> None:
    """Uncovered witness paths are reported unignored in the real repository and in the
    class-specific scratch repository.
    """
    real_repo_witness = "definitely-not-a-stderr-log.tmp"
    ignored, rule = _ignore_decision(real_repo_witness, REPO_ROOT)
    assert not ignored, f"expected {real_repo_witness} to be unignored, but {rule!r} matched it"

    repo = _scratch_repo_with_gitignore(tmp_path, _gitignore_without_blanket_results_rules())
    uncovered_path = "results/definitely-not-a-stderr-log.tmp"
    scratch_ignored, scratch_rule = _ignore_decision(uncovered_path, repo)
    assert not scratch_ignored, (
        f"expected {uncovered_path} to be unignored by the class-specific rules alone, but "
        f"{scratch_rule!r} matched it"
    )


# ---------------------------------------------------------------------------
# 12. The LAYER-drift gate allow-lists exactly the three known non-script modules and
#     fails on any other module lacking a LAYER constant.
# ---------------------------------------------------------------------------


def test_layer_drift_gate_fails_on_a_new_module_missing_layer_but_not_allow_listed(
    tmp_path: Path,
) -> None:
    """Any smoke module other than `harness.py`/`tenant.py`/`__init__.py` that lacks `LAYER` is a
    gate failure rather than skipped.
    """
    scratch_smoke_dir = tmp_path / "smoke"
    scratch_smoke_dir.mkdir()
    (scratch_smoke_dir / "__init__.py").write_text("", encoding="utf-8")
    (scratch_smoke_dir / "harness.py").write_text("X = 1\n", encoding="utf-8")
    (scratch_smoke_dir / "tenant.py").write_text("Y = 2\n", encoding="utf-8")
    (scratch_smoke_dir / "forgot_layer.py").write_text(
        "RESOURCE_CLASS = 'forgot_layer'\n", encoding="utf-8"
    )

    allow_listed = {"__init__.py", "harness.py", "tenant.py"}
    missing_layer_not_allow_listed = []
    for py_path in sorted(scratch_smoke_dir.glob("*.py")):
        if py_path.name in allow_listed:
            continue
        if _declared_layer(py_path) is None:
            missing_layer_not_allow_listed.append(py_path.name)

    assert missing_layer_not_allow_listed == ["forgot_layer.py"], (
        "a smoke script missing LAYER and not in the allow-list must be flagged as a gate "
        f"failure, got {missing_layer_not_allow_listed}"
    )


def test_layer_drift_gate_does_not_flag_the_allow_listed_library_modules(tmp_path: Path) -> None:
    """The three library modules without `LAYER` are never flagged."""
    scratch_smoke_dir = tmp_path / "smoke"
    scratch_smoke_dir.mkdir()
    (scratch_smoke_dir / "__init__.py").write_text("", encoding="utf-8")
    (scratch_smoke_dir / "harness.py").write_text("X = 1\n", encoding="utf-8")
    (scratch_smoke_dir / "tenant.py").write_text("Y = 2\n", encoding="utf-8")

    allow_listed = {"__init__.py", "harness.py", "tenant.py"}
    missing_layer_not_allow_listed = [
        py_path.name
        for py_path in sorted(scratch_smoke_dir.glob("*.py"))
        if py_path.name not in allow_listed and _declared_layer(py_path) is None
    ]
    assert missing_layer_not_allow_listed == []


# ---------------------------------------------------------------------------
# --only: a named-module subset, run in layer order and marked PARTIAL.
# ---------------------------------------------------------------------------


def _all_declared_modules() -> set[str]:
    return set().union(*_declared_layer_arrays_from_script().values())


def _billable(outputs_file: Path, *extra: str) -> list[str]:
    return ["--i-understand-this-is-billable", "--outputs", str(outputs_file), *extra]


def test_only_with_an_unknown_name_fails_before_any_aws_call_or_tenant(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path, _billable(outputs_file, "--only", "iam_roles,no_such_module"), env, tmp_path
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "'no_such_module'" in result.stderr
    assert "  layer 4 scripts.smoke.iam_roles\n" in result.stderr, "the known-module listing"
    assert "  layer 6 scripts.smoke_agent_stream\n" in result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    assert "allocate_smoke_tenant" not in call_log_text, "a tenant was provisioned"
    assert not re.findall(r"(?:^|\n)-m ", call_log_text), "a module ran"
    assert Path(env["AWS_STUB_CALL_LOG"]).read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("only", "expected"),
    [
        (["iam_roles"], ["scripts.smoke.iam_roles"]),
        (["scripts.smoke.iam_roles"], ["scripts.smoke.iam_roles"]),
        (["smoke_text_delta_stream"], ["scripts.smoke_text_delta_stream"]),
        (["scripts.smoke_agent_stream"], ["scripts.smoke_agent_stream"]),
    ],
    ids=["short", "full", "layer6-short", "layer6-full"],
)
def test_only_accepts_short_and_full_names(
    stubbed: tuple[Path, dict[str, str]],
    outputs_file: Path,
    tmp_path: Path,
    only: list[str],
    expected: list[str],
) -> None:
    script_path, env = stubbed
    result = _run(script_path, _billable(outputs_file, "--only", *only), env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert _check_modules_in(Path(env["CALL_LOG"]).read_text(encoding="utf-8")) == expected


def test_only_runs_the_selection_in_layer_order_whatever_the_argument_order(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    args = _billable(
        outputs_file, "--only", "smoke_text_delta_stream,eval_sfn", "--only", "iam_roles"
    )
    result = _run(script_path, args, env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert _check_modules_in(Path(env["CALL_LOG"]).read_text(encoding="utf-8")) == [
        "scripts.smoke.iam_roles",
        "scripts.smoke.eval_sfn",
        "scripts.smoke_text_delta_stream",
    ]


def test_only_dry_run_prints_exactly_the_selected_set(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path,
        ["--dry-run", "--outputs", str(outputs_file), "--only", "eval_sfn,iam_roles"],
        env,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    would_run = re.findall(r"^would run: \S+ -m (\S+)", result.stdout, re.MULTILINE)
    assert would_run == ["scripts.smoke.iam_roles", "scripts.smoke.eval_sfn"]
    assert "Layer 6" not in result.stdout
    assert "PARTIAL (--only)" in result.stdout


def test_only_with_layer_restricts_to_that_layer_and_refuses_a_module_outside_it(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    inside = _run(
        script_path, _billable(outputs_file, "--layer", "5", "--only", "eval_sfn"), env, tmp_path
    )
    assert inside.returncode == 0, inside.stderr
    assert _check_modules_in(Path(env["CALL_LOG"]).read_text(encoding="utf-8")) == [
        "scripts.smoke.eval_sfn"
    ]

    Path(env["CALL_LOG"]).write_text("", encoding="utf-8")
    outside = _run(
        script_path,
        _billable(outputs_file, "--layer", "4", "--only", "iam_roles,eval_sfn"),
        env,
        tmp_path,
    )
    assert outside.returncode == 2
    assert "'eval_sfn' is in layer 5, outside --layer 4" in outside.stderr
    assert "allocate_smoke_tenant" not in Path(env["CALL_LOG"]).read_text(encoding="utf-8")


def test_only_cannot_be_combined_with_layer_inventory(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path,
        _billable(outputs_file, "--layer", "inventory", "--only", "iam_roles"),
        env,
        tmp_path,
    )
    assert result.returncode == 2
    assert "--only cannot be combined with --layer inventory" in result.stderr
    assert Path(env["AWS_STUB_CALL_LOG"]).read_text(encoding="utf-8") == ""


def test_a_partial_run_is_marked_partial_and_lists_what_did_not_run(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, _billable(outputs_file, "--only", "iam_roles"), env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "ALL RUN LAYERS PASSED" not in result.stdout + result.stderr
    assert "PARTIAL RUN: SELECTED MODULES PASSED" in result.stdout
    assert "NOT a full-deployment verification" in result.stderr

    document = json.loads(
        (tmp_path / "results" / "verification-inventory.run.json").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict) and document["partial"] is True
    assert document["selected_modules"] == ["scripts.smoke.iam_roles"]
    assert set(document["not_run_modules"]) == _all_declared_modules() - {"scripts.smoke.iam_roles"}
    assert [entry["resource_class"] for entry in document["results"]] == ["iam_roles"]

    md_text = (tmp_path / "results" / "verification-inventory.run.md").read_text(encoding="utf-8")
    assert "**PARTIAL RUN (--only)**" in md_text and "scripts.smoke.eval_sfn" in md_text


def test_a_full_run_inventory_stays_an_unmarked_array(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, _billable(outputs_file), env, tmp_path)
    assert result.returncode == 0, result.stderr
    document = json.loads(
        (tmp_path / "results" / "verification-inventory.run.json").read_text(encoding="utf-8")
    )
    assert isinstance(document, list)
    assert "PARTIAL" not in result.stdout + result.stderr


def test_a_partial_run_that_stops_at_a_failing_layer_lists_the_unrun_selection(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    env = {**env, "STUB_FAIL_MODULES": "iam_roles"}
    result = _run(
        script_path, _billable(outputs_file, "--only", "iam_roles,eval_sfn"), env, tmp_path
    )
    assert result.returncode == 1
    assert _check_modules_in(Path(env["CALL_LOG"]).read_text(encoding="utf-8")) == [
        "scripts.smoke.iam_roles"
    ]
    document = json.loads(
        (tmp_path / "results" / "verification-inventory.run.json").read_text(encoding="utf-8")
    )
    assert "scripts.smoke.eval_sfn" in document["not_run_modules"]
    assert "PARTIAL RUN: ONE OR MORE SELECTED MODULES FAILED" in result.stderr
    assert "teardown" in result.stdout.lower() or "tearing down" in result.stdout.lower()


# ---------------------------------------------------------------------------
# `--layer N` is PARTIAL; the smoke token is re-minted once it is too old.
# ---------------------------------------------------------------------------


def test_a_layer_run_is_marked_partial_like_only(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(script_path, _billable(outputs_file, "--layer", "5"), env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "ALL RUN LAYERS PASSED" not in result.stdout + result.stderr
    assert "PARTIAL RUN: SELECTED MODULES PASSED" in result.stdout
    assert "PARTIAL RUN (--layer 5) — NOT a full-deployment verification." in result.stderr

    document = json.loads(
        (tmp_path / "results" / "verification-inventory.run.json").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict) and document["partial"] is True
    assert document["partial_selector"] == "--layer 5"
    layer_5 = {
        m for m in _all_declared_modules() if m.startswith("scripts.smoke.") and _layer_of(m) == 5
    }
    assert set(document["selected_modules"]) == layer_5
    assert set(document["not_run_modules"]) == _all_declared_modules() - layer_5
    md_text = (tmp_path / "results" / "verification-inventory.run.md").read_text(encoding="utf-8")
    assert "**PARTIAL RUN (--layer 5)**" in md_text


def test_a_layer_dry_run_says_partial(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    result = _run(
        script_path, ["--dry-run", "--layer", "4", "--outputs", str(outputs_file)], env, tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert "PARTIAL (--layer 4): would run scripts.smoke.lambda_actions" in result.stdout


def _layer_of(module: str) -> int:
    return int(importlib.import_module(module).LAYER)


def _token_vectors(call_log_text: str) -> dict[str, list[str]]:
    """`{module: [--token value, ...]}` for every check-module invocation in the call log."""
    vectors: dict[str, list[str]] = {}
    for line in call_log_text.splitlines():
        words = line.split()
        if len(words) < 2 or words[0] != "-m" or words[1] == "scripts.smoke.smoke_token":
            continue
        if "--token" in words:
            vectors.setdefault(words[1], []).append(words[words.index("--token") + 1])
    return vectors


def test_an_old_token_is_re_minted_before_each_token_taking_module(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """With a zero age limit, every token-taking module gets a fresh token and user."""
    script_path, env = stubbed
    env = {**env, "STUB_MINT_SEQUENCE": "1", "VERIFY_SMOKE_TOKEN_MAX_AGE_SECONDS": "0"}
    result = _run(script_path, _billable(outputs_file), env, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    call_log_text = Path(env["CALL_LOG"]).read_text(encoding="utf-8")
    tokens = _token_vectors(call_log_text)
    assert len(tokens) >= 3, tokens
    received = [values[0] for values in tokens.values()]
    assert len(set(received)) == len(received), f"a token was reused: {tokens}"
    assert "stub-id-token-1" not in received, "the first token reached a module after its limit"
    mints = sum(1 for line in call_log_text.splitlines() if " mint " in f" {line} ")
    deletes = sum(1 for line in call_log_text.splitlines() if " delete-user " in f" {line} ")
    assert mints == len(received) + 1 and deletes == mints, (mints, deletes, received)
    assert "re-minting before scripts.smoke.api_routes" in result.stdout


def test_a_fresh_token_is_not_re_minted(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    """Control: under the default limit, one mint serves every token-taking module."""
    script_path, env = stubbed
    env = {**env, "STUB_MINT_SEQUENCE": "1"}
    result = _run(script_path, _billable(outputs_file), env, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    tokens = _token_vectors(Path(env["CALL_LOG"]).read_text(encoding="utf-8"))
    assert {value for values in tokens.values() for value in values} == {"stub-id-token-1"}
    assert "re-minting" not in result.stdout


def test_a_non_numeric_token_age_limit_is_a_usage_error(
    stubbed: tuple[Path, dict[str, str]], outputs_file: Path, tmp_path: Path
) -> None:
    script_path, env = stubbed
    env = {**env, "VERIFY_SMOKE_TOKEN_MAX_AGE_SECONDS": "45m"}
    result = _run(script_path, _billable(outputs_file), env, tmp_path)
    assert result.returncode == 2
    assert "whole number of seconds" in result.stderr
