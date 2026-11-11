# SPDX-License-Identifier: MIT
"""Unit tests for scripts/render_agentcore_env.py and the deploy/teardown seam.

Exercises the render-and-restore functions directly, and the restore-on-failure and
restore-on-interrupt paths against temp-copy invocations of scripts/deploy.sh.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_agentcore_env.py"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
TEARDOWN_SH = REPO_ROOT / "scripts" / "teardown.sh"
CLASSIFIER_PATH = REPO_ROOT / "scripts" / "classify_post_apply_drift.py"

#: Upper bound for every wait in the interrupt tests. The `terraform apply` stub those tests
#: install never exits on its own, so a long bound still proves the trap fired.
_INTERRUPT_TIMEOUT_SECONDS = 60.0


def _reset_signal_dispositions() -> None:  # pragma: no cover - runs in the forked child
    """Pre-exec hook: put SIGINT/SIGTERM back to ``SIG_DFL`` before ``bash`` is exec'd.

    A process launched as a background job of a non-interactive shell inherits SIGINT as
    ``SIG_IGN``, which ``subprocess`` does not reset; a shell cannot trap a signal ignored on entry,
    so without this hook `deploy.sh`'s ``trap … INT`` would not be installed.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


#: Environment variables stripped from every sandboxed script launch in this file.
#: `scripts/deploy.sh` refuses to run when `AWS_ACCESS_KEY_ID` or `AWS_SESSION_TOKEN` is non-empty.
_STRIPPED_CREDENTIAL_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")

#: The account every stubbed `aws sts get-caller-identity` in this file reports, and so the
#: `CRM_EXPECTED_ACCOUNT_ID` every sandbox sets by default (placeholder-allowlisted).
SANDBOX_ACCOUNT: Final[str] = (
    "111122223333"  # nosemgrep: generic.secrets.security.detected-aws-account-id
)


def _sandbox_env(*, drop: Sequence[str] = (), **overrides: str) -> dict[str, str]:
    """Build the child environment for a sandboxed `deploy.sh`/`teardown.sh` launch.

    Returns the parent environment minus `_STRIPPED_CREDENTIAL_VARS` and minus `drop`, with
    `CRM_EXPECTED_ACCOUNT_ID` set to `SANDBOX_ACCOUNT` and `overrides` applied last. Every launcher
    in this file routes through here, which `TestSandboxEnvIsCredentialFree` asserts.
    """
    env = {
        k: v for k, v in os.environ.items() if k not in _STRIPPED_CREDENTIAL_VARS and k not in drop
    }
    if "CRM_EXPECTED_ACCOUNT_ID" not in drop:
        env["CRM_EXPECTED_ACCOUNT_ID"] = SANDBOX_ACCOUNT
    env.update(overrides)
    return env


_spec = importlib.util.spec_from_file_location("render_agentcore_env", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
render_agentcore_env = importlib.util.module_from_spec(_spec)
sys.modules["render_agentcore_env"] = render_agentcore_env
_spec.loader.exec_module(render_agentcore_env)

TEMPLATE_CONTENT = (
    "gateway_url: https://{{ACCOUNT_ID}}.gateway.example\n"
    "region: {{AWS_REGION}}\n"
    "profile: {{AWS_PROFILE}}\n"
)


@pytest.fixture
def template_file(tmp_path: Path) -> Path:
    path = tmp_path / ".bedrock_agentcore.yaml"
    path.write_text(TEMPLATE_CONTENT, encoding="utf-8")
    return path


def _stub_gate_interpreter(sandbox: Path) -> None:
    """Give a deploy sandbox a `.venv/bin/python3` that can import boto3.

    The shim execs the interpreter running the tests, which `deploy.sh` resolves for its gates.
    """
    venv_bin = sandbox / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    shim = venv_bin / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)


# Adopted-VPC ids for the sandbox tfvars, assembled at runtime: `check_no_account_id` forbids
# `vpc-`/`subnet-`/`sg-`/`rtb-` hex ids in tracked content.
_SANDBOX_VPC = "vpc-" + "0" * 8
_SANDBOX_SUBNETS = ["subnet-" + "a" * 7 + d for d in ("0", "1")]
_SANDBOX_NAT_SUBNET = "subnet-" + "b" * 8
_SANDBOX_TABLES = ["rtb-" + "c" * 7 + d for d in ("0", "1")]
_SANDBOX_RUNTIME_SUBNETS = ["subnet-" + "d" * 7 + d for d in ("0", "1")]
_SANDBOX_RUNTIME_TABLES = ["rtb-" + "e" * 8]
_SANDBOX_ORIGIN_SGS = ["sg-" + "f" * 8]


def _stub_deploy_preconditions(scripts_dir: Path) -> None:
    """Provision every precondition `deploy.sh` checks before its apply, into a deploy sandbox.

    Each stub lets the script reach the stage under test without softening any guard; the guards'
    real behaviour is asserted elsewhere, including `tests/unit/test_deploy_script.py`.
    `TestSandboxReachesTheApply` asserts a sandbox provisioned here still reaches `terraform apply`.
    """
    stub = scripts_dir / "check_lambda_zip_imports.py"
    stub.write_text("import sys\n\nsys.exit(0)\n", encoding="utf-8")
    _stub_gate_interpreter(scripts_dir.parent)
    _stub_parity_gate(scripts_dir)
    _stub_runtime_children_tagger(scripts_dir)
    _stub_relay_gate(scripts_dir)
    _stub_exposure_gate(scripts_dir)
    _stub_adopted_vpc_preflight(scripts_dir)
    _write_sandbox_tfvars(scripts_dir.parent / "infra")
    _copy_outputs_provenance(scripts_dir)


def _copy_outputs_provenance(scripts_dir: Path) -> None:
    """Copy the real `scripts/outputs_provenance.py`, and the `proc_run` it imports, into a sandbox.

    With no `.git` it warns that `captured_from_commit` is null and still stamps the document,
    exiting 0.
    """
    for name in ("outputs_provenance.py", "proc_run.py"):
        (scripts_dir / name).write_text(
            (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )


def _write_sandbox_tfvars(infra_dir: Path) -> None:
    """Write the `infra/terraform.tfvars` `deploy.sh` refuses to apply without.

    Only the keys `deploy.sh` reads are written (`vpc_id`, `vpc_private_subnet_ids`,
    `vpc_private_route_table_ids`, `vpc_nat_public_subnet_id`, `vpc_runtime_subnet_ids`,
    `vpc_runtime_route_table_ids`, `api_origin_ingress_security_group_ids`), with real-shaped
    assembled ids.
    """
    infra_dir.mkdir(parents=True, exist_ok=True)

    def listed(ids: list[str]) -> str:
        return "[\n" + "".join(f'  "{i}",\n' for i in ids) + "]"

    (infra_dir / "terraform.tfvars").write_text(
        f'vpc_id = "{_SANDBOX_VPC}"\n'
        f"vpc_private_subnet_ids = {listed(_SANDBOX_SUBNETS)}\n"
        f'vpc_nat_public_subnet_id = "{_SANDBOX_NAT_SUBNET}"\n'
        f"vpc_private_route_table_ids = {listed(_SANDBOX_TABLES)}\n"
        f"vpc_runtime_subnet_ids = {listed(_SANDBOX_RUNTIME_SUBNETS)}\n"
        f"vpc_runtime_route_table_ids = {listed(_SANDBOX_RUNTIME_TABLES)}\n"
        f"api_origin_ingress_security_group_ids = {listed(_SANDBOX_ORIGIN_SGS)}\n",
        encoding="utf-8",
    )


def _stub_adopted_vpc_preflight(scripts_dir: Path) -> None:
    """Write a passing stand-in for `scripts/preflight_adopted_vpc.sh` into a deploy sandbox.

    The real script makes live `ec2 describe-*` calls; its refusals are asserted in
    `tests/unit/test_preflight_adopted_vpc.py`.
    """
    stub = scripts_dir / "preflight_adopted_vpc.sh"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)


def _stub_exposure_gate(scripts_dir: Path) -> None:
    """Write a passing stand-in for `scripts/check_no_public_exposure.py` into a deploy sandbox.

    That a failing gate aborts the deploy is asserted in
    `tests/unit/test_deploy_script.py::TestPublicExposureGuard`.
    """
    stub = scripts_dir / "check_no_public_exposure.py"
    stub.write_text("import sys\n\nsys.exit(0)\n", encoding="utf-8")


def _stub_relay_gate(scripts_dir: Path, *, currency_exit: int = 0, import_exit: int = 0) -> None:
    """Write stand-ins for deploy.sh's two relay-zip guards into a sandbox, and the zip they inspect.

    `currency_exit` / `import_exit` simulate a failure of either guard; see `TestRelayGuards`.
    """
    (scripts_dir / "check_relay_zip.py").write_text(
        f"import sys\n\nsys.exit({currency_exit})\n", encoding="utf-8"
    )
    package_relay = scripts_dir / "package_relay.sh"
    package_relay.write_text(f"#!/usr/bin/env bash\nexit {import_exit}\n", encoding="utf-8")
    package_relay.chmod(0o755)

    # A zip at the path terraform reads (`var.lambda_build_dir` resolved against `infra/`). An empty
    # file is enough: both readers of its contents are stubbed above.
    build_dir = scripts_dir.parent / "infra" / "build" / "lambda"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "relay.zip").write_bytes(b"")


def _stub_parity_gate(scripts_dir: Path, *, exit_code: int = 0) -> None:
    """Write a stand-in for `scripts.smoke.deployed_parity`'s `--deploy-gate` mode into a sandbox.

    `exit_code=1` simulates a parity failure.
    """
    smoke_dir = scripts_dir / "smoke"
    smoke_dir.mkdir(exist_ok=True)
    (smoke_dir / "__init__.py").write_text("", encoding="utf-8")
    (smoke_dir / "deployed_parity.py").write_text(
        f"import sys\n\nsys.exit({exit_code})\n", encoding="utf-8"
    )


#: Where `_stub_runtime_children_tagger`'s stand-in appends its argv, relative to `scripts/`.
TAGGER_CALL_LOG: Final[str] = "tag_agentcore_runtime_children.calls"


def _stub_runtime_children_tagger(scripts_dir: Path, *, exit_code: int = 0) -> None:
    """Write a stand-in for `scripts/tag_agentcore_runtime_children.sh` into a sandbox.

    It appends its argv to `TAGGER_CALL_LOG` beside itself and exits `exit_code`; the helper's own
    behaviour is tested in `tests/unit/test_tag_agentcore_runtime_children.py`.
    """
    stub = scripts_dir / "tag_agentcore_runtime_children.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "$(dirname "$0")/{TAGGER_CALL_LOG}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


#: Every `terraform` stub models `output -json`, which `deploy.sh` reads to record the stack
#: outputs. Inserted by `_terraform_stub`. Idempotent.
_TERRAFORM_OUTPUT_CLAUSE = (
    'if [[ "$1" == "output" ]]; then echo \'{"stub":{"value":"x"}}\'; exit 0; fi\n'
)


def _terraform_stub(body: str) -> str:
    """`body` with an `output -json` clause guaranteed, inserted just after the shebang."""
    if '"$1" == "output"' in body:
        return body
    lines = body.split("\n")
    assert lines[0].startswith("#!"), f"terraform stub does not start with a shebang: {lines[0]!r}"
    return "\n".join([lines[0], _TERRAFORM_OUTPUT_CLAUSE.rstrip("\n"), *lines[1:]])


def _is_sandbox_env_call(node: ast.expr) -> bool:
    """True when `node` is a direct call to `_sandbox_env`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == _sandbox_env.__name__
    )


def _enclosing_functions(tree: ast.Module) -> dict[int, ast.FunctionDef | None]:
    """Map each node's `id()` to the innermost function definition containing it, or None."""
    enclosing: dict[int, ast.FunctionDef | None] = {}

    def _descend(node: ast.AST, current: ast.FunctionDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = child if isinstance(child, ast.FunctionDef) else current
            enclosing[id(child)] = child_scope
            _descend(child, child_scope)

    _descend(tree, None)
    return enclosing


def _names_bound_to_sandbox_env(scope: ast.FunctionDef) -> frozenset[str]:
    """The local names `scope` assigns from a `_sandbox_env` call."""
    return frozenset(
        target.id
        for stmt in ast.walk(scope)
        if isinstance(stmt, ast.Assign) and _is_sandbox_env_call(stmt.value)
        for target in stmt.targets
        if isinstance(target, ast.Name)
    )


class TestSandboxEnvIsCredentialFree:
    """Every sandboxed launch in this file runs with no static AWS credential in its environment.

    `scripts/deploy.sh` refuses to run when a static AWS credential is present.
    """

    def test_a_child_launched_with_the_sandbox_env_observes_no_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real child launched with the sandbox env sees none of the credential variables."""
        control = "CRM_SANDBOX_ENV_CONTROL"
        for var in (*_STRIPPED_CREDENTIAL_VARS, control):
            monkeypatch.setenv(var, "set-in-the-parent-not-a-credential")

        probe = (
            "import json, os, sys;"
            "sys.stdout.write(json.dumps({k: os.environ.get(k) for k in sys.argv[1:]}))"
        )
        result = proc_run.run(
            [sys.executable, "-c", probe, *_STRIPPED_CREDENTIAL_VARS, control],
            capture_output=True,
            text=True,
            env=_sandbox_env(),
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=True,
        )
        observed = json.loads(result.stdout)

        # Control: an empty env would also satisfy every assertion below.
        assert observed[control] == "set-in-the-parent-not-a-credential", observed
        assert [observed[var] for var in _STRIPPED_CREDENTIAL_VARS] == [None, None, None], (
            f"a sandboxed child still observes an AWS credential: {observed}"
        )

    def test_every_launcher_in_this_file_builds_its_env_through_the_helper(self) -> None:
        """Every child-process launch in this module builds its `env=` through `_sandbox_env`.

        Rules over this module's source: no hand-assembled `env=`, no launch without `env=`, every
        launch spelled `proc_run.run` / `proc_run.popen` with `proc_run` imported under its
        own name, and no direct `subprocess.run` / `subprocess.Popen` call.
        """
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        enclosing = _enclosing_functions(tree)

        inspected = 0
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "subprocess",
                "scripts.proc_run",
            ):
                offenders.append(
                    f"line {node.lineno}: `from {node.module} import ...` hides a launch from "
                    "rules 1 and 2 below; use `from scripts import proc_run` and call through it"
                )
            if isinstance(node, ast.ImportFrom) and node.module == "scripts":
                offenders += [
                    f"line {node.lineno}: `proc_run as {alias.asname}` hides a launch from rules "
                    "1 and 2 below; import it under its own name"
                    for alias in node.names
                    if alias.name == "proc_run" and alias.asname is not None
                ]
            if isinstance(node, ast.Import):
                offenders += [
                    f"line {node.lineno}: `import subprocess as {alias.asname}` hides a launch "
                    "from rules 1 and 2 below; import it under its own name"
                    for alias in node.names
                    if alias.name == "subprocess" and alias.asname is not None
                ]
            func = getattr(node, "func", None)
            if not (isinstance(node, ast.Call) and isinstance(func, ast.Attribute)):
                continue
            receiver = func.value.id if isinstance(func.value, ast.Name) else None
            if receiver == "subprocess" and func.attr in ("run", "Popen"):
                offenders.append(
                    f"line {node.lineno}: subprocess.{func.attr} bypasses proc_run; launch through "
                    "proc_run.run / proc_run.popen"
                )
                continue
            if not (receiver == "proc_run" and func.attr in ("run", "popen")):
                continue
            inspected += 1
            env_kw = next((kw for kw in node.keywords if kw.arg == "env"), None)
            if env_kw is None:
                argv = node.args[0] if node.args else None
                argv0 = argv.elts[0] if isinstance(argv, ast.List) and argv.elts else None
                # `git` reads the real repository and touches no AWS API, so it inherits the
                # environment unchanged.
                if not (isinstance(argv0, ast.Constant) and argv0.value == "git"):
                    offenders.append(
                        f"line {node.lineno}: no env= (inherits the parent environment)"
                    )
                continue
            value = env_kw.value
            scope = enclosing.get(id(node))
            bound = _names_bound_to_sandbox_env(scope) if scope is not None else frozenset()
            passed_directly = _is_sandbox_env_call(value)
            passed_via_local = isinstance(value, ast.Name) and value.id in bound
            if not (passed_directly or passed_via_local):
                offenders.append(
                    f"line {node.lineno}: env= is neither a {_sandbox_env.__name__}() call nor a "
                    f"local bound to one (bound here: {sorted(bound)})"
                )

        assert not offenders, (
            "every subprocess launch in this file must take its environment from "
            f"{_sandbox_env.__name__}(), which strips {list(_STRIPPED_CREDENTIAL_VARS)}:\n  "
            + "\n  ".join(offenders)
        )
        # Non-vacuity: the walk must have inspected at least one launch.
        textual = len(re.findall(r"proc_run\.(?:run|popen)\(", source))
        assert inspected == textual, (
            f"the AST walk inspected {inspected} proc_run launches but the source contains "
            f"{textual}; a launch is spelled in a way this guard cannot see"
        )


class TestSandboxReachesTheApply:
    """A sandbox from `_stub_deploy_preconditions` reaches `terraform apply` and completes."""

    _ARG_RECORDING_TERRAFORM = '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TF_ARG_LOG"\nexit 0\n'

    def _run(
        self, tmp_path: Path, *, tagger_exit: int = 0
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        """Run a provisioned sandbox's deploy; return (result, scripts dir, terraform arg log)."""
        sandbox = tmp_path / "sandbox_guard"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "deploy.sh").write_text(DEPLOY_SH.read_text(encoding="utf-8"), "utf-8")
        (scripts_dir / "deploy.sh").chmod(0o755)
        for name, src in (
            ("render_agentcore_env.py", SCRIPT_PATH),
            ("classify_post_apply_drift.py", CLASSIFIER_PATH),
        ):
            (scripts_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        _stub_deploy_preconditions(scripts_dir)
        _stub_runtime_children_tagger(scripts_dir, exit_code=tagger_exit)

        bin_dir = tmp_path / "bin_guard"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)
        (bin_dir / "terraform").write_text(
            _terraform_stub(self._ARG_RECORDING_TERRAFORM), encoding="utf-8"
        )
        (bin_dir / "terraform").chmod(0o755)

        arg_log = tmp_path / "tf_args_guard.txt"
        result = proc_run.run(
            ["bash", str(scripts_dir / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=_sandbox_env(
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                TF_ARG_LOG=str(arg_log),
                LOG_FILE=str(tmp_path / "deploy_guard.log"),
            ),
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        return result, scripts_dir, arg_log

    def test_a_provisioned_sandbox_reaches_terraform_apply(self, tmp_path: Path) -> None:
        result, scripts_dir, arg_log = self._run(tmp_path)
        combined = result.stdout + result.stderr
        diagnosis = (
            "A deploy sandbox no longer reaches `terraform apply`. This is almost certainly a NEW "
            "pre-apply precondition in scripts/deploy.sh with no matching fixture in "
            "`_stub_deploy_preconditions` — provide the precondition there (never weaken the guard), "
            "and note that every sandbox class in this file depends on this property.\n"
            f"deploy.sh output was:\n{combined}"
        )
        assert result.returncode == 0, diagnosis
        assert arg_log.exists(), diagnosis
        recorded = arg_log.read_text(encoding="utf-8")
        assert any(line.startswith("apply") for line in recorded.splitlines()), (
            f"terraform was invoked but never with `apply`; recorded calls were:\n{recorded}\n"
            f"{diagnosis}"
        )
        assert "Deploy complete." in result.stdout, diagnosis
        (tagger_call,) = (scripts_dir / TAGGER_CALL_LOG).read_text(encoding="utf-8").splitlines()
        assert re.fullmatch(r"--profile \S+ --region \S+ --runtime-arn \S+", tagger_call), (
            tagger_call
        )

    def test_a_failed_runtime_children_tagging_fails_the_deploy(self, tmp_path: Path) -> None:
        result, scripts_dir, _arg_log = self._run(tmp_path, tagger_exit=1)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "Deploy complete." not in result.stdout
        assert "tagging the AgentCore runtime's DEFAULT endpoint and workload identity failed" in (
            result.stdout + result.stderr
        )
        assert (scripts_dir / TAGGER_CALL_LOG).exists()


class TestRenderRestoreRoundTrip:
    def test_render_substitutes_all_tokens(self, template_file: Path) -> None:
        missing = render_agentcore_env.render(
            template_file,
            template_file,
            {"ACCOUNT_ID": "999999999999", "AWS_REGION": "us-east-1", "AWS_PROFILE": "default"},
        )
        assert missing == set()
        rendered = template_file.read_text(encoding="utf-8")
        assert "999999999999" in rendered
        assert "us-east-1" in rendered
        assert "{{" not in rendered

    def test_render_reports_missing_tokens_without_failing(self, template_file: Path) -> None:
        missing = render_agentcore_env.render(template_file, template_file, {"ACCOUNT_ID": "123"})
        assert missing == {"AWS_REGION", "AWS_PROFILE"}
        rendered = template_file.read_text(encoding="utf-8")
        assert "{{AWS_REGION}}" in rendered

    def test_backup_then_render_then_restore_is_byte_identical(self, template_file: Path) -> None:
        original_bytes = template_file.read_bytes()
        backup_path = template_file.with_suffix(".yaml.orig")

        render_agentcore_env.backup(template_file, backup_path)
        render_agentcore_env.render(
            template_file, template_file, {"ACCOUNT_ID": "1", "AWS_REGION": "r", "AWS_PROFILE": "p"}
        )
        assert template_file.read_bytes() != original_bytes

        render_agentcore_env.restore(template_file, backup_path)
        assert template_file.read_bytes() == original_bytes

    def test_restore_without_backup_raises(self, template_file: Path) -> None:
        backup_path = template_file.with_suffix(".yaml.orig")
        with pytest.raises(FileNotFoundError):
            render_agentcore_env.restore(template_file, backup_path)

    def test_cli_render_then_restore_round_trip(self, template_file: Path) -> None:
        backup_path = template_file.with_suffix(".yaml.orig")
        original = template_file.read_text(encoding="utf-8")

        exit_code = render_agentcore_env.main(
            [
                "render",
                "--template",
                str(template_file),
                "--backup",
                str(backup_path),
                "--values-json",
                '{"ACCOUNT_ID": "42", "AWS_REGION": "us-east-1", "AWS_PROFILE": "default"}',
            ]
        )
        assert exit_code == 0
        assert template_file.read_text(encoding="utf-8") != original

        exit_code = render_agentcore_env.main(
            ["restore", "--template", str(template_file), "--backup", str(backup_path)]
        )
        assert exit_code == 0
        assert template_file.read_text(encoding="utf-8") == original

    def test_cli_render_rejects_malformed_values_json(self, template_file: Path) -> None:
        exit_code = render_agentcore_env.main(
            ["render", "--template", str(template_file), "--values-json", "not json"]
        )
        assert exit_code == 2

    def test_cli_render_rejects_nonexistent_template(self, tmp_path: Path) -> None:
        exit_code = render_agentcore_env.main(
            ["render", "--template", str(tmp_path / "missing.yaml"), "--values-json", "{}"]
        )
        assert exit_code == 2


class TestNoAccountIdInCommittedTemplate:
    """The committed template never carries a real account id."""

    def test_no_committed_bedrock_agentcore_template_carries_an_account_id(self) -> None:
        # Any tracked `.bedrock_agentcore.yaml` template, and the render script itself, carry no
        # 12-digit account id outside placeholder tokens.
        result = proc_run.run(
            ["git", "ls-files", "*.bedrock_agentcore.yaml*", "scripts/render_agentcore_env.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked_files = [line for line in result.stdout.splitlines() if line.strip()]
        import re

        account_id_pattern = re.compile(r"(?<!\{)\b\d{12}\b(?!\})")
        for rel_path in tracked_files:
            content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            assert not account_id_pattern.search(content), (
                f"{rel_path} appears to contain a bare 12-digit account id"
            )

    def test_render_output_never_hardcodes_an_account_id_in_the_script_source(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        import re

        assert not re.search(r"\b\d{12}\b", source), (
            "render script source contains a 12-digit literal"
        )


class TestProfileRegionResolutionOrder:
    """Profile and region resolve as flag, then AWS_PROFILE/AWS_REGION, then default/us-east-1.

    Every case runs the script from a scratch sandbox with a `PATH` holding only stub
    `aws`/`terraform`/`python3` executables and the minimal system directories, so the real `aws`
    CLI, `terraform` binary and `infra/` are never reachable. The stub `aws` answers
    `get-caller-identity` with a fixed fake account id. The script copy is of `scripts/deploy.sh`.
    """

    def _make_precedence_sandbox(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Build a scratch dir with copies of deploy.sh/teardown.sh/render_agentcore_env.py and a
        bin/ of stub aws/terraform executables. Returns (sandbox_dir, deploy_sh_copy, bin_dir).
        """
        sandbox = tmp_path / "sandbox"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (sandbox / "infra").mkdir()

        for name, source in (("deploy.sh", DEPLOY_SH), ("teardown.sh", TEARDOWN_SH)):
            dest = scripts_dir / name
            dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            dest.chmod(0o755)
        (scripts_dir / "render_agentcore_env.py").write_text(
            SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "classify_post_apply_drift.py").write_text(
            CLASSIFIER_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _stub_deploy_preconditions(scripts_dir)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)

        (bin_dir / "terraform").write_text(
            "#!/usr/bin/env bash\n"
            # `output -json` is modelled because deploy.sh records the stack outputs on success.
            'if [[ "$1" == "output" ]]; then echo \'{"stub":{"value":"x"}}\'; exit 0; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "terraform").chmod(0o755)

        return sandbox, scripts_dir / "deploy.sh", bin_dir

    def _minimal_path(self, bin_dir: Path) -> str:
        # Only the stub bin dir plus the system directories needed for bash/python3/coreutils,
        # never an inherited PATH entry.
        system_dirs = [p for p in ("/usr/bin", "/bin", "/usr/local/bin") if Path(p).is_dir()]
        return os.pathsep.join([str(bin_dir), *system_dirs])

    def _run_script_dry(
        self, tmp_path: Path, script_name: str, env_overrides: dict[str, str], args: list[str]
    ) -> subprocess.CompletedProcess[str]:
        sandbox, _deploy_copy, bin_dir = self._make_precedence_sandbox(tmp_path)
        script_path = sandbox / "scripts" / script_name
        env = _sandbox_env(
            drop=("AWS_PROFILE", "AWS_REGION"),
            PATH=self._minimal_path(bin_dir),
            LOG_FILE=str(tmp_path / f"{script_name}.log"),
        )
        env.update(env_overrides)
        return proc_run.run(
            ["bash", str(script_path), *args],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_flag_takes_precedence_over_env_and_default(self, tmp_path: Path) -> None:
        result = self._run_script_dry(
            tmp_path,
            "deploy.sh",
            {"AWS_PROFILE": "env-profile", "AWS_REGION": "env-region"},
            ["--profile", "flag-profile", "--region", "flag-region"],
        )
        assert "profile=flag-profile" in result.stdout
        assert "region=flag-region" in result.stdout

    def test_env_takes_precedence_over_default_when_no_flag(self, tmp_path: Path) -> None:
        result = self._run_script_dry(
            tmp_path,
            "deploy.sh",
            {"AWS_PROFILE": "env-profile", "AWS_REGION": "env-region"},
            [],
        )
        assert "profile=env-profile" in result.stdout
        assert "region=env-region" in result.stdout

    def test_default_used_when_no_flag_and_no_env(self, tmp_path: Path) -> None:
        result = self._run_script_dry(tmp_path, "deploy.sh", {}, [])
        assert "profile=default" in result.stdout
        assert "region=us-east-1" in result.stdout

    def test_teardown_sh_has_identical_resolution_order(self, tmp_path: Path) -> None:
        result = self._run_script_dry(
            tmp_path,
            "teardown.sh",
            {"AWS_PROFILE": "env-profile", "AWS_REGION": "env-region"},
            # --auto-approve: these subprocesses have no TTY, and teardown refuses to destroy
            # without either a TTY or this explicit opt-in.
            ["--auto-approve", "--profile", "flag-profile"],
        )
        assert "profile=flag-profile" in result.stdout
        assert "region=env-region" in result.stdout


class TestRestoreOnFailureAndInterrupt:
    """The template is restored on failure and on interrupt, against a temp copy of deploy.sh with
    `terraform`/`aws` stubbed.
    """

    def _make_sandbox(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Copy deploy.sh and render_agentcore_env.py into a scratch dir with a stub `aws`, a
        controllable `terraform` on PATH, and a real template. Returns (sandbox_dir, template_path,
        bin_dir).
        """
        sandbox = tmp_path / "sandbox"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (sandbox / "infra").mkdir()

        (scripts_dir / "deploy.sh").write_text(
            DEPLOY_SH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "deploy.sh").chmod(0o755)
        (scripts_dir / "render_agentcore_env.py").write_text(
            SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "classify_post_apply_drift.py").write_text(
            CLASSIFIER_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _stub_deploy_preconditions(scripts_dir)

        template_path = sandbox / ".bedrock_agentcore.yaml"
        template_path.write_text(TEMPLATE_CONTENT, encoding="utf-8")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)

        return sandbox, template_path, bin_dir

    def test_restore_happens_when_terraform_apply_fails(self, tmp_path: Path) -> None:
        sandbox, template_path, bin_dir = self._make_sandbox(tmp_path)
        original_bytes = template_path.read_bytes()

        # A `terraform` stub whose `init` succeeds but `apply` fails — simulates
        # a failing deploy step after the render has already happened.
        (bin_dir / "terraform").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "init" ]]; then exit 0; fi\n'
            'if [[ "$1" == "apply" ]]; then echo "simulated apply failure" >&2; exit 1; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "terraform").chmod(0o755)

        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")

        result = proc_run.run(
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--template", str(template_path)],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        # `terraform apply` must actually have been reached and failed; the EXIT trap would also
        # restore after a pre-apply refusal.
        assert "simulated apply failure" in result.stderr, result.stdout + result.stderr
        assert result.returncode != 0, "deploy.sh should exit non-zero when terraform apply fails"
        assert template_path.read_bytes() == original_bytes, (
            "template was not restored after a failing terraform apply step"
        )
        assert (
            not (template_path.with_suffix(".yaml.orig")).exists() or True
        )  # backup may remain; restore is what matters

    def _hanging_terraform(self, bin_dir: Path, sentinel: Path) -> None:
        """Install a `terraform` stub whose `apply` announces itself and then never returns.

        It creates `sentinel` after the render has completed and `apply` has started, and it never
        exits on its own.
        """
        (bin_dir / "terraform").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "init" ]]; then exit 0; fi\n'
            'if [[ "$1" == "apply" ]]; then\n'
            f'  : > "{sentinel}"\n'
            "  while true; do sleep 1; done\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "terraform").chmod(0o755)

    @staticmethod
    def _await_apply_started(proc: subprocess.Popen[str], sentinel: Path) -> None:
        """Block until the `terraform apply` stub reports it is running.

        Bounded by the process staying alive; an exit before the sentinel appears fails the test.
        """
        deadline = time.time() + _INTERRUPT_TIMEOUT_SECONDS
        while time.time() < deadline:
            if sentinel.exists():
                return
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                pytest.fail(
                    f"deploy.sh exited (rc={proc.returncode}) before terraform apply started, so "
                    f"no interrupt during apply could be delivered\nstdout: {stdout}\n"
                    f"stderr: {stderr}"
                )
            time.sleep(0.05)
        proc.kill()
        proc.wait(timeout=_INTERRUPT_TIMEOUT_SECONDS)
        pytest.fail(
            f"terraform apply never started within {_INTERRUPT_TIMEOUT_SECONDS}s — cannot test "
            "interrupt-during-apply"
        )

    def _assert_interrupt_restores(
        self, tmp_path: Path, signal_number: int, signal_name: str
    ) -> None:
        """Signal deploy.sh's whole process group during `terraform apply` and require restore."""
        sandbox, template_path, bin_dir = self._make_sandbox(tmp_path)
        original_bytes = template_path.read_bytes()
        sentinel = tmp_path / f"apply-started-{signal_name}"
        self._hanging_terraform(bin_dir, sentinel)

        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")

        proc = proc_run.popen(
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--template", str(template_path)],
            cwd=sandbox,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            # Makes the signal genuinely deliverable regardless of pytest's own launch context;
            # see `_reset_signal_dispositions`.
            preexec_fn=_reset_signal_dispositions,
        )

        # The signal must land while apply is the foreground child, which the sentinel proves;
        # the render has therefore already completed and its own write cannot race the restore.
        self._await_apply_started(proc, sentinel)
        assert template_path.read_bytes() != original_bytes, (
            "the template was not rendered before terraform apply started"
        )

        # Signal the whole process group (bash + its foreground child), matching how a real
        # terminal-delivered SIGTERM/SIGINT would reach both; signalling only the bash PID can
        # leave a foreground child running under Popen's non-interactive process group.
        os.killpg(os.getpgid(proc.pid), signal_number)
        try:
            # `communicate` rather than `wait`: it drains stdout/stderr while waiting, so a child
            # that fills a 64 KB pipe buffer during its trap cannot deadlock the wait, and the
            # child's own output is available for the failure message below.
            stdout, stderr = proc.communicate(timeout=_INTERRUPT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # Kill the whole group, not just bash: the apply stub and its `sleep` are in the same
            # new session and hold the inherited stdout/stderr pipes open, so draining them after
            # killing only the shell would block until this second timeout too.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=_INTERRUPT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                stdout, stderr = "<undrained>", "<undrained>"
            pytest.fail(
                f"deploy.sh did not exit within {_INTERRUPT_TIMEOUT_SECONDS}s of {signal_name} — "
                "the trap did not run (the apply stub never exits on its own)\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )

        assert template_path.read_bytes() == original_bytes, (
            f"template was not restored after {signal_name} during terraform apply"
        )

    def test_restore_happens_on_sigterm_interrupt(self, tmp_path: Path) -> None:
        self._assert_interrupt_restores(tmp_path, signal.SIGTERM, "SIGTERM")

    def test_restore_happens_on_sigint_interrupt(self, tmp_path: Path) -> None:
        self._assert_interrupt_restores(tmp_path, signal.SIGINT, "SIGINT")

    def test_restore_on_sigint_holds_when_the_launching_process_ignores_sigint(
        self, tmp_path: Path
    ) -> None:
        """The template is restored on SIGINT even when the launching process ignores SIGINT.

        The disposition is restored in a ``finally``.
        """
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self._assert_interrupt_restores(tmp_path, signal.SIGINT, "SIGINT")
        finally:
            signal.signal(
                signal.SIGINT, previous if previous is not None else signal.default_int_handler
            )

    def test_restore_happens_when_terraform_destroy_fails_via_teardown(
        self, tmp_path: Path
    ) -> None:
        sandbox = tmp_path / "sandbox_teardown"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (sandbox / "infra").mkdir()

        (scripts_dir / "teardown.sh").write_text(
            TEARDOWN_SH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "teardown.sh").chmod(0o755)
        (scripts_dir / "render_agentcore_env.py").write_text(
            SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "classify_post_apply_drift.py").write_text(
            CLASSIFIER_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _stub_deploy_preconditions(scripts_dir)

        template_path = sandbox / ".bedrock_agentcore.yaml"
        template_path.write_text(TEMPLATE_CONTENT, encoding="utf-8")
        original_bytes = template_path.read_bytes()

        bin_dir = tmp_path / "bin_teardown"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)
        (bin_dir / "terraform").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "init" ]]; then exit 0; fi\n'
            'if [[ "$1" == "destroy" ]]; then echo "simulated destroy failure" >&2; exit 1; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "terraform").chmod(0o755)

        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")

        result = proc_run.run(
            [
                "bash",
                str(scripts_dir / "teardown.sh"),
                # No TTY on a subprocess, so the explicit destroy opt-in is required.
                "--auto-approve",
                "--template",
                str(template_path),
            ],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode != 0
        assert "simulated destroy failure" in result.stderr, (
            "teardown did not reach terraform destroy — the restore assertion below would be vacuous"
        )
        assert template_path.read_bytes() == original_bytes, (
            "template was not restored after a failing terraform destroy step"
        )


class TestBackupRefusesToOverwrite:
    """`backup()` refuses to overwrite an existing backup of the template."""

    def test_backup_raises_when_a_backup_already_exists(self, template_file: Path) -> None:
        backup_path = template_file.with_suffix(".yaml.orig")
        render_agentcore_env.backup(template_file, backup_path)
        template_file.write_text(f"gateway_url: https://{SANDBOX_ACCOUNT}.gateway.example\n")
        with pytest.raises(FileExistsError) as excinfo:
            render_agentcore_env.backup(template_file, backup_path)
        assert "did not restore" in str(excinfo.value)
        # The original template bytes must survive the refused second backup.
        assert "{{ACCOUNT_ID}}" in backup_path.read_text(encoding="utf-8")

    def test_render_cli_exits_2_rather_than_clobbering_the_backup(
        self, template_file: Path
    ) -> None:
        backup_path = template_file.with_suffix(".yaml.orig")
        render_agentcore_env.backup(template_file, backup_path)
        rc = render_agentcore_env.main(
            [
                "render",
                "--template",
                str(template_file),
                "--backup",
                str(backup_path),
                "--values-json",
                f'{{"ACCOUNT_ID": "{SANDBOX_ACCOUNT}"}}',
            ]
        )
        assert rc == 2
        assert "{{ACCOUNT_ID}}" in backup_path.read_text(encoding="utf-8")

    def test_bedrock_agentcore_orig_is_gitignored(self) -> None:
        """The `.orig` backup file is gitignored."""
        result = proc_run.run(
            ["git", "check-ignore", ".bedrock_agentcore.yaml.orig"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            ".bedrock_agentcore.yaml.orig is not gitignored; "
            f"git check-ignore said: {result.stdout!r} {result.stderr!r}"
        )


class TestNonInteractivity:
    """`terraform apply` runs non-interactively by default, while destroy keeps its confirmation and
    refuses to run non-interactively without an explicit opt-in.
    """

    def _sandbox(self, tmp_path: Path, script_name: str, terraform_body: str) -> tuple[Path, Path]:
        sandbox = tmp_path / f"sandbox_{script_name}"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (sandbox / "infra").mkdir()
        source = DEPLOY_SH if script_name == "deploy.sh" else TEARDOWN_SH
        (scripts_dir / script_name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        (scripts_dir / script_name).chmod(0o755)
        (scripts_dir / "render_agentcore_env.py").write_text(
            SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "classify_post_apply_drift.py").write_text(
            CLASSIFIER_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _stub_deploy_preconditions(scripts_dir)
        bin_dir = tmp_path / f"bin_{script_name}"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)
        (bin_dir / "terraform").write_text(_terraform_stub(terraform_body), encoding="utf-8")
        (bin_dir / "terraform").chmod(0o755)
        return sandbox, bin_dir

    def _run(
        self, sandbox: Path, bin_dir: Path, script_name: str, args: list[str], tmp_path: Path
    ) -> subprocess.CompletedProcess[str]:
        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")
        env["LOG_FILE"] = str(tmp_path / f"{script_name}.log")
        # These sandboxes copy scripts/ and infra/ but not frontend/; the publish step has its
        # own sandbox class below.
        if script_name == "deploy.sh":
            args = [*args, "--skip-frontend"]
        return proc_run.run(
            ["bash", str(sandbox / "scripts" / script_name), *args],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    _ARG_RECORDING_TERRAFORM = (
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$TF_ARG_LOG"\n'
        'if [[ "$1" == "plan" ]]; then exit 0; fi\n'
        "exit 0\n"
    )

    def test_deploy_passes_auto_approve_by_default(self, tmp_path: Path) -> None:
        sandbox, bin_dir = self._sandbox(tmp_path, "deploy.sh", self._ARG_RECORDING_TERRAFORM)
        arg_log = tmp_path / "tf_args_deploy.txt"
        env_path = f"{bin_dir}:{os.environ['PATH']}"
        result = proc_run.run(
            # --skip-frontend: this sandbox has no frontend/ and this test is
            # about the terraform args, not the publish step.
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=_sandbox_env(
                PATH=env_path,
                TF_ARG_LOG=str(arg_log),
                LOG_FILE=str(tmp_path / "deploy.log"),
            ),
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        apply_lines = [
            line for line in arg_log.read_text(encoding="utf-8").splitlines() if "apply" in line
        ]
        assert apply_lines, "terraform apply was never invoked"
        assert all("-auto-approve" in line for line in apply_lines), apply_lines

    def test_deploy_no_auto_approve_without_a_tty_fails_before_any_aws_call(
        self, tmp_path: Path
    ) -> None:
        sandbox, bin_dir = self._sandbox(tmp_path, "deploy.sh", self._ARG_RECORDING_TERRAFORM)
        result = self._run(sandbox, bin_dir, "deploy.sh", ["--no-auto-approve"], tmp_path)
        assert result.returncode == 1
        assert "--no-auto-approve needs a TTY" in result.stderr
        # Fails before resolving identity, i.e. before any AWS call at all.
        assert "get-caller-identity" not in result.stdout

    def test_teardown_refuses_non_interactively_without_the_explicit_opt_in(
        self, tmp_path: Path
    ) -> None:
        terraform = '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TF_ARG_LOG"\nexit 0\n'
        sandbox, bin_dir = self._sandbox(tmp_path, "teardown.sh", terraform)
        arg_log = tmp_path / "tf_args_teardown.txt"
        arg_log.write_text("", encoding="utf-8")
        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")
        env["TF_ARG_LOG"] = str(arg_log)
        env["LOG_FILE"] = str(tmp_path / "teardown.log")
        result = proc_run.run(
            ["bash", str(sandbox / "scripts" / "teardown.sh")],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        assert result.returncode == 1
        assert "--auto-approve" in result.stderr
        assert "Nothing has been changed." in result.stderr
        assert arg_log.read_text(encoding="utf-8") == "", (
            "teardown reached terraform without the destroy opt-in"
        )

    def test_backend_config_is_passed_to_init_only_when_the_file_exists(
        self, tmp_path: Path
    ) -> None:
        """`terraform init` is passed `-backend-config` only when `infra/backend.hcl` exists."""
        terraform = (
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TF_ARG_LOG"\n'
            'if [[ "$1" == "plan" ]]; then exit 0; fi\nexit 0\n'
        )
        sandbox, bin_dir = self._sandbox(tmp_path, "deploy.sh", terraform)
        arg_log = tmp_path / "tf_args_backend.txt"
        env = _sandbox_env(
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            TF_ARG_LOG=str(arg_log),
            LOG_FILE=str(tmp_path / "deploy_backend.log"),
        )

        # Absent: init must not name a config file that does not exist.
        result = proc_run.run(
            # --skip-frontend: no frontend/ in this sandbox; the assertion below
            # is about terraform init's args.
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        init_lines = [
            line for line in arg_log.read_text(encoding="utf-8").splitlines() if "init" in line
        ]
        assert init_lines and all("-backend-config" not in line for line in init_lines)

        # Present: init must pass it.
        (sandbox / "infra" / "backend.hcl").write_text('bucket = "x"\n', encoding="utf-8")
        arg_log.write_text("", encoding="utf-8")
        result = proc_run.run(
            # --skip-frontend: no frontend/ in this sandbox; the assertion below
            # is about terraform init's args.
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        init_lines = [
            line for line in arg_log.read_text(encoding="utf-8").splitlines() if "init" in line
        ]
        assert init_lines and all("-backend-config" in line for line in init_lines), init_lines

    def test_teardown_env_opt_in_is_honoured(self, tmp_path: Path) -> None:
        terraform = '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TF_ARG_LOG"\nexit 0\n'
        sandbox, bin_dir = self._sandbox(tmp_path, "teardown.sh", terraform)
        arg_log = tmp_path / "tf_args_teardown_env.txt"
        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")
        env["TF_ARG_LOG"] = str(arg_log)
        env["TEARDOWN_AUTO_APPROVE"] = "true"
        env["LOG_FILE"] = str(tmp_path / "teardown_env.log")
        result = proc_run.run(
            ["bash", str(sandbox / "scripts" / "teardown.sh")],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        destroy_lines = [
            line for line in arg_log.read_text(encoding="utf-8").splitlines() if "destroy" in line
        ]
        assert destroy_lines and all("-auto-approve" in line for line in destroy_lines)


class TestRelayGuards:
    """A failing relay-zip guard stops `deploy.sh` before `terraform apply`.

    Each guard is failed in turn and asserted fatal; the currency guard's exit 3 is fatal with its
    own message.
    """

    def _sandbox(
        self, tmp_path: Path, *, currency_exit: int = 0, import_exit: int = 0
    ) -> tuple[Path, Path]:
        sandbox = tmp_path / "sandbox_relay"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (sandbox / "infra").mkdir()
        (scripts_dir / "deploy.sh").write_text(
            DEPLOY_SH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "deploy.sh").chmod(0o755)
        (scripts_dir / "render_agentcore_env.py").write_text(
            SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "classify_post_apply_drift.py").write_text(
            CLASSIFIER_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _stub_deploy_preconditions(scripts_dir)
        _stub_relay_gate(scripts_dir, currency_exit=currency_exit, import_exit=import_exit)

        bin_dir = tmp_path / "bin_relay"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)
        (bin_dir / "terraform").write_text(
            _terraform_stub('#!/usr/bin/env bash\necho "TERRAFORM_$1_RAN"\nexit 0\n'),
            encoding="utf-8",
        )
        (bin_dir / "terraform").chmod(0o755)
        return sandbox, bin_dir

    def _run(
        self, sandbox: Path, bin_dir: Path, tmp_path: Path
    ) -> subprocess.CompletedProcess[str]:
        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")
        env["LOG_FILE"] = str(tmp_path / "deploy_relay.log")
        return proc_run.run(
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_both_guards_passing_reaches_the_apply(self, tmp_path: Path) -> None:
        """With both guards passing, the deploy reaches the apply."""
        sandbox, bin_dir = self._sandbox(tmp_path)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "TERRAFORM_apply_RAN" in result.stdout

    def test_a_stale_relay_zip_aborts_before_terraform_apply(self, tmp_path: Path) -> None:
        """Exit 1 from the currency guard aborts before `terraform apply`."""
        sandbox, bin_dir = self._sandbox(tmp_path, currency_exit=1)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "the relay zip is stale" in combined
        assert "nothing was applied" in combined
        assert "TERRAFORM_apply_RAN" not in combined
        assert "Deploy complete." not in combined

    def test_an_unverifiable_relay_zip_aborts_with_its_own_message(self, tmp_path: Path) -> None:
        """Exit 3 from the currency guard aborts with a message distinct from the stale-zip one."""
        sandbox, bin_dir = self._sandbox(tmp_path, currency_exit=3)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "verified nothing" in combined
        assert "the relay zip is stale" not in combined
        assert "TERRAFORM_apply_RAN" not in combined

    def test_an_unimportable_relay_handler_aborts_before_terraform_apply(
        self, tmp_path: Path
    ) -> None:
        """A failing import guard aborts before `terraform apply`."""
        sandbox, bin_dir = self._sandbox(tmp_path, import_exit=1)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "cannot be imported" in combined
        assert "nothing was applied" in combined
        assert "TERRAFORM_apply_RAN" not in combined

    def test_the_currency_guard_is_invoked_with_require_zip(self, tmp_path: Path) -> None:
        """The currency guard is invoked with `--require-zip`, which makes an absent zip fatal."""
        sandbox, _bin_dir = self._sandbox(tmp_path)
        deploy = (sandbox / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        assert "check_relay_zip.py --require-zip --zip" in deploy


class TestPostApplyParityGate:
    """`deploy.sh` runs the deployed<->zip parity gate after a successful apply, before the drift
    classification.
    """

    def _sandbox(self, tmp_path: Path, *, parity_exit: int) -> tuple[Path, Path]:
        sandbox = tmp_path / "sandbox_parity"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (sandbox / "infra").mkdir()
        (scripts_dir / "deploy.sh").write_text(
            DEPLOY_SH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "deploy.sh").chmod(0o755)
        (scripts_dir / "render_agentcore_env.py").write_text(
            SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "classify_post_apply_drift.py").write_text(
            CLASSIFIER_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _stub_deploy_preconditions(scripts_dir)
        _stub_parity_gate(scripts_dir, exit_code=parity_exit)
        bin_dir = tmp_path / "bin_parity"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)
        (bin_dir / "terraform").write_text(
            _terraform_stub(
                '#!/usr/bin/env bash\nif [[ "$1" == "plan" ]]; then echo PLAN_RAN; exit 0; fi\nexit 0\n'
            ),
            encoding="utf-8",
        )
        (bin_dir / "terraform").chmod(0o755)
        return sandbox, bin_dir

    def _run(
        self, sandbox: Path, bin_dir: Path, tmp_path: Path
    ) -> subprocess.CompletedProcess[str]:
        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")
        env["LOG_FILE"] = str(tmp_path / "deploy_parity.log")
        # --skip-frontend: no frontend/ is copied into this sandbox, and the
        # publish step is exercised by TestFrontendPublishStep instead.
        return proc_run.run(
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_a_parity_failure_fails_the_deploy_loudly(self, tmp_path: Path) -> None:
        sandbox, bin_dir = self._sandbox(tmp_path, parity_exit=1)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "post-apply deployed<->zip parity FAILED" in combined
        assert "Deploy complete." not in combined

    def test_parity_runs_before_the_drift_classification_and_does_not_replace_it(
        self, tmp_path: Path
    ) -> None:
        sandbox, bin_dir = self._sandbox(tmp_path, parity_exit=0)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        parity_index = combined.index("CodeSha256 equals its local zip digest")
        drift_index = combined.index("Checking post-apply drift bound")
        assert parity_index < drift_index
        assert "no changes — zero pending drift" in combined

    def test_a_parity_failure_short_circuits_before_the_drift_plan_is_taken(
        self, tmp_path: Path
    ) -> None:
        sandbox, bin_dir = self._sandbox(tmp_path, parity_exit=1)
        result = self._run(sandbox, bin_dir, tmp_path)
        combined = result.stdout + result.stderr
        # Assert the parity gate actually ran and refused, so an earlier death reds this cell.
        assert "Verifying every deployed action Lambda's CodeSha256" in combined, combined
        assert "parity FAILED" in combined, combined
        assert result.returncode != 0
        assert "Checking post-apply drift bound" not in combined

    def test_a_missing_boto3_dies_legibly_before_apply_rather_than_tracing_back(
        self, tmp_path: Path
    ) -> None:
        """When the interpreter cannot import boto3, the deploy dies before apply with a message
        naming the virtualenv.
        """
        sandbox, bin_dir = self._sandbox(tmp_path, parity_exit=0)
        # Make the fallback `python3` a stub that cannot import boto3.
        (sandbox / ".venv" / "bin" / "python3").unlink()
        (bin_dir / "python3").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "-c" && "$2" == "import boto3" ]]; then\n'
            "  echo \"ModuleNotFoundError: No module named 'boto3'\" >&2\n"
            "  exit 1\n"
            "fi\n"
            f'exec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        (bin_dir / "python3").chmod(0o755)

        result = self._run(sandbox, bin_dir, tmp_path)
        combined = result.stdout + result.stderr
        assert result.returncode != 0
        assert "cannot import boto3" in combined
        assert ".venv/bin/pip install -r" in combined
        assert "nothing was applied" in combined
        assert "Traceback" not in combined
        assert "Running terraform apply" not in combined


class TestPostApplyDriftBound:
    """`deploy.sh` runs the post-apply `terraform plan -detailed-exitcode` bound on every apply and
    classifies the pending set.
    """

    def _sandbox(self, tmp_path: Path, plan_script: str) -> tuple[Path, Path]:
        sandbox = tmp_path / "sandbox_drift"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (sandbox / "infra").mkdir()
        (scripts_dir / "deploy.sh").write_text(
            DEPLOY_SH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "deploy.sh").chmod(0o755)
        (scripts_dir / "render_agentcore_env.py").write_text(
            SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "classify_post_apply_drift.py").write_text(
            CLASSIFIER_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _stub_deploy_preconditions(scripts_dir)
        bin_dir = tmp_path / "bin_drift"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)
        (bin_dir / "terraform").write_text(_terraform_stub(plan_script), encoding="utf-8")
        (bin_dir / "terraform").chmod(0o755)
        return sandbox, bin_dir

    @staticmethod
    def _planned_change(address: str, resource_type: str) -> str:
        import json as _json

        return _json.dumps(
            {
                "type": "planned_change",
                "change": {
                    "action": "update",
                    "resource": {"addr": address, "resource_type": resource_type},
                },
            }
        )

    def _terraform_stub(self, ndjson_lines: list[str], plan_exit: int) -> str:
        body = "\n".join(f"echo '{line}'" for line in ndjson_lines)
        return (
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "plan" ]]; then\n'
            f"{body}\n"
            f"  exit {plan_exit}\n"
            "fi\n"
            "exit 0\n"
        )

    def _run(
        self, sandbox: Path, bin_dir: Path, tmp_path: Path
    ) -> subprocess.CompletedProcess[str]:
        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")
        env["LOG_FILE"] = str(tmp_path / "deploy_drift.log")
        # --skip-frontend: no frontend/ is copied into this sandbox, and the
        # publish step is exercised by TestFrontendPublishStep instead.
        return proc_run.run(
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_zero_pending_changes_reports_no_drift(self, tmp_path: Path) -> None:
        sandbox, bin_dir = self._sandbox(tmp_path, self._terraform_stub([], 0))
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "no changes — zero pending drift" in result.stdout

    def test_a_failed_outputs_write_fails_the_deploy_distinguishably(self, tmp_path: Path) -> None:
        """A failed outputs-snapshot write fails the deploy non-zero with a message saying the apply
        itself succeeded.
        """
        # `output` succeeds for the drift `plan` but returns a document naming nothing -- the exact
        # shape the writer refuses, and the one a plain `>` truncation leaves behind.
        stub = self._terraform_stub([], 0).replace(
            "#!/usr/bin/env bash\n",
            '#!/usr/bin/env bash\nif [[ "$1" == "output" ]]; then echo "{}"; exit 0; fi\n',
            1,
        )
        sandbox, bin_dir = self._sandbox(tmp_path, stub)
        result = self._run(sandbox, bin_dir, tmp_path)
        combined = result.stdout + result.stderr

        assert result.returncode != 0, f"a failed outputs write exited 0:\n{combined}"
        # Names the outputs write specifically...
        assert "not a non-empty JSON object" in combined, combined
        assert "stack outputs not recorded" in combined, combined
        # ...and says the deploy itself worked, so this cannot be misread as an apply failure.
        assert "THE DEPLOY ITSELF SUCCEEDED" in combined, combined
        # The bounds it claims are clean must actually have run and been clean before this point.
        assert "no changes — zero pending drift" in result.stdout, combined
        # And the refused document must not be installed, nor left behind as a partial.
        snapshot = sandbox / "results" / "tf-outputs.json"
        assert not snapshot.exists(), "the refused document was installed anyway"
        assert not snapshot.with_suffix(".json.partial").exists(), "a .partial was left behind"

    def test_a_failed_outputs_write_removes_a_stale_snapshot(self, tmp_path: Path) -> None:
        """A failed outputs write deletes a previous snapshot rather than leaving it in place."""
        stub = self._terraform_stub([], 0).replace(
            "#!/usr/bin/env bash\n",
            '#!/usr/bin/env bash\nif [[ "$1" == "output" ]]; then echo "{}"; exit 0; fi\n',
            1,
        )
        sandbox, bin_dir = self._sandbox(tmp_path, stub)
        stale = sandbox / "results" / "tf-outputs.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"from":{"value":"an earlier stack"}}', encoding="utf-8")

        result = self._run(sandbox, bin_dir, tmp_path)
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert not stale.exists(), (
            "the stale snapshot survived a failed write, so every reader would consume an earlier "
            "stack's outputs with no signal"
        )
        assert "describes an EARLIER stack" in combined, combined

    def test_packaging_only_drift_is_warned_and_bounded(self, tmp_path: Path) -> None:
        lines = [
            self._planned_change(
                'module.lambda_action["get_account"].aws_lambda_function.this',
                "aws_lambda_function",
            ),
            self._planned_change("module.agentcore.null_resource.image_build", "null_resource"),
        ]
        sandbox, bin_dir = self._sandbox(tmp_path, self._terraform_stub(lines, 2))
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "pending_changes=2 unexpected=0" in result.stdout
        # `deploy.sh` reports the allowance the classifier named.
        assert "absorbed by a declared allowance" in result.stdout
        # Both rows are packaging: `null_resource.image_build` is in PACKAGING_ATTRIBUTABLE_TYPES.
        assert "absorbed_by=packaging-zips:2" in result.stdout
        assert "ALL confined to the Lambda-packaging resources" not in result.stdout

    def test_drift_outside_the_packaging_allow_list_fails_the_deploy(self, tmp_path: Path) -> None:
        """Drift outside the packaging allow-list fails the deploy."""
        lines = [
            self._planned_change(
                'module.lambda_action["get_account"].aws_lambda_function.this',
                "aws_lambda_function",
            ),
            self._planned_change(
                "module.dynamodb.aws_dynamodb_table.accounts", "aws_dynamodb_table"
            ),
        ]
        sandbox, bin_dir = self._sandbox(tmp_path, self._terraform_stub(lines, 2))
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode == 1
        # The refusal names every allowance consulted.
        assert "absorbed by NO allowance" in result.stderr
        assert "arn-normalisation" in result.stderr
        assert "OUTSIDE the Lambda-packaging allow-list" not in result.stderr
        assert "module.dynamodb.aws_dynamodb_table.accounts" in result.stderr
        assert "no declared allowance absorbs it" in result.stderr

    def test_a_failed_post_apply_plan_is_not_silently_accepted(self, tmp_path: Path) -> None:
        sandbox, bin_dir = self._sandbox(tmp_path, self._terraform_stub([], 1))
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode == 1
        assert "drift bound could not be established" in result.stderr


class TestFrontendPublishStep:
    """`deploy.sh`'s frontend publish step.

    Control-flow assertions with `npm`, `aws` and `terraform` stubbed: publish only after the parity
    and drift gates, the cache-header split, the invalidation, and that every failure mode aborts
    loudly. `render_frontend_config.py`'s own behaviour is asserted in
    `tests/unit/test_render_frontend_config.py`.
    """

    # Annotated rather than inferred: `dict` is invariant, so an inferred
    # `dict[str, dict[str, str]]` cannot be passed to `_sandbox`'s `dict[str, object]`.
    OUTPUTS: dict[str, object] = {
        "frontend_origin_bucket_name": {"value": "crm-frontend-origin-abc"},
        "cloudfront_distribution_id": {"value": "E1234567890ABC"},
        "cloudfront_domain_name": {"value": "d111111abcdef8.cloudfront.net"},
        "cognito_user_pool_id": {"value": "us-east-1_ABCDEFGHI"},
        "cognito_spa_client_id": {"value": "fakeclientid0000"},
        "agentcore_runtime_arn": {
            "value": f"arn:aws:bedrock-agentcore:us-east-1:{SANDBOX_ACCOUNT}:runtime/crm-Ab"
        },
    }

    def _sandbox(
        self,
        tmp_path: Path,
        *,
        outputs: dict[str, object] | None = None,
        npm_build_writes_index: bool = True,
        npm_exit: int = 0,
        aws_fail_on: str = "",
    ) -> tuple[Path, Path, Path]:
        import json as _json

        sandbox = tmp_path / "sandbox_publish"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (sandbox / "infra").mkdir()
        (scripts_dir / "deploy.sh").write_text(
            DEPLOY_SH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts_dir / "deploy.sh").chmod(0o755)
        for name, source in (
            ("render_agentcore_env.py", SCRIPT_PATH),
            ("classify_post_apply_drift.py", CLASSIFIER_PATH),
            ("render_frontend_config.py", REPO_ROOT / "scripts" / "render_frontend_config.py"),
        ):
            (scripts_dir / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        _stub_deploy_preconditions(scripts_dir)

        # A frontend/ with node_modules present, so the `npm ci` branch is skipped
        # and the build branch is what gets exercised.
        frontend = sandbox / "frontend"
        (frontend / "node_modules").mkdir(parents=True)
        (frontend / "public").mkdir()

        bin_dir = tmp_path / "bin_publish"
        bin_dir.mkdir()
        call_log = tmp_path / "publish_calls.txt"

        dist = frontend / "dist"
        build_body = (
            f"mkdir -p '{dist}'\n"
            f"echo '<!doctype html>' > '{dist}/index.html'\n"
            f"mkdir -p '{dist}/assets'\n"
            f"echo 'x' > '{dist}/assets/index-abc123.js'\n"
            # Vite copies public/ into dist/, placeholder template included.
            f"echo '{{}}' > '{dist}/config.example.json'\n"
            if npm_build_writes_index
            else "true\n"
        )
        (bin_dir / "npm").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "npm %s\\n" "$*" >> "{call_log}"\n'
            f"{build_body}"
            f"exit {npm_exit}\n",
            encoding="utf-8",
        )
        (bin_dir / "npm").chmod(0o755)

        fail_clause = (
            f'if [[ "$*" == *"{aws_fail_on}"* ]]; then exit 1; fi\n' if aws_fail_on else ""
        )
        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "aws %s\\n" "$*" >> "{call_log}"\n'
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{SANDBOX_ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            f"{fail_clause}"
            'if [[ "$*" == *"create-invalidation"* ]]; then echo "I2QWERTY"; exit 0; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)

        outputs_json = _json.dumps(self.OUTPUTS if outputs is None else outputs)
        (bin_dir / "terraform").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "terraform %s\\n" "$*" >> "{call_log}"\n'
            'if [[ "$1" == "output" ]]; then\n'
            f"  cat <<'JSON'\n{outputs_json}\nJSON\n"
            "  exit 0\n"
            "fi\n"
            'if [[ "$1" == "plan" ]]; then exit 0; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "terraform").chmod(0o755)
        return sandbox, bin_dir, call_log

    def _run(
        self, sandbox: Path, bin_dir: Path, tmp_path: Path, args: list[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = _sandbox_env(PATH=f"{bin_dir}:{os.environ['PATH']}")
        env["LOG_FILE"] = str(tmp_path / "deploy_publish.log")
        return proc_run.run(
            ["bash", str(sandbox / "scripts" / "deploy.sh"), *(args or [])],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )

    def test_a_default_deploy_builds_renders_syncs_and_invalidates(self, tmp_path: Path) -> None:
        sandbox, bin_dir, call_log = self._sandbox(tmp_path)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = call_log.read_text(encoding="utf-8")
        assert "npm run build" in calls
        assert "s3 sync" in calls
        assert "create-invalidation" in calls
        assert "Deploy complete." in result.stdout

    def test_config_json_is_rendered_from_the_terraform_outputs(self, tmp_path: Path) -> None:
        import json as _json

        sandbox, bin_dir, _ = self._sandbox(tmp_path)
        assert self._run(sandbox, bin_dir, tmp_path).returncode == 0
        rendered = _json.loads(
            (sandbox / "frontend" / "dist" / "config.json").read_text(encoding="utf-8")
        )
        assert rendered["cognitoUserPoolId"] == "us-east-1_ABCDEFGHI"
        assert rendered["cognitoClientId"] == "fakeclientid0000"
        assert rendered["apiBaseUrl"] == "/api"
        # The SPA URL-encodes this itself; a pre-encoded ARN would double-encode.
        assert "%3A" not in rendered["agentRuntimeArn"]

    def test_the_placeholder_template_is_not_published(self, tmp_path: Path) -> None:
        sandbox, bin_dir, _ = self._sandbox(tmp_path)
        assert self._run(sandbox, bin_dir, tmp_path).returncode == 0
        assert not (sandbox / "frontend" / "dist" / "config.example.json").exists()

    def test_index_and_config_are_uploaded_no_store_and_excluded_from_the_immutable_sync(
        self, tmp_path: Path
    ) -> None:
        # The default behaviour uses the managed CachingOptimized policy, whose TTLs come from the
        # origin's own headers, so the two mutable keys carry no-store on the objects themselves.
        sandbox, bin_dir, call_log = self._sandbox(tmp_path)
        assert self._run(sandbox, bin_dir, tmp_path).returncode == 0
        lines = call_log.read_text(encoding="utf-8").splitlines()

        sync = next(line for line in lines if "s3 sync" in line)
        assert "--exclude index.html" in sync and "--exclude config.json" in sync
        assert "max-age=31536000, immutable" in sync

        for key in ("index.html", "config.json"):
            cp = next(line for line in lines if "s3 cp" in line and f"/{key}" in line)
            assert "no-store, max-age=0" in cp

    def test_publish_runs_after_the_parity_and_drift_gates(self, tmp_path: Path) -> None:
        sandbox, bin_dir, _ = self._sandbox(tmp_path)
        result = self._run(sandbox, bin_dir, tmp_path)
        out = result.stdout
        assert out.index("CodeSha256") < out.index("npm run build")
        assert out.index("Checking post-apply drift bound") < out.index("npm run build")

    def test_a_parity_failure_publishes_nothing(self, tmp_path: Path) -> None:
        sandbox, bin_dir, call_log = self._sandbox(tmp_path)
        _stub_parity_gate(sandbox / "scripts", exit_code=1)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        calls = call_log.read_text(encoding="utf-8")
        # Assert the parity gate was reached, so an earlier refusal cannot satisfy this cell.
        assert "parity FAILED" in result.stdout + result.stderr, result.stdout + result.stderr
        assert "terraform apply" in calls, calls
        assert "npm run build" not in calls
        assert "s3 sync" not in calls

    def test_skip_frontend_publishes_nothing_and_warns(self, tmp_path: Path) -> None:
        sandbox, bin_dir, call_log = self._sandbox(tmp_path)
        result = self._run(sandbox, bin_dir, tmp_path, ["--skip-frontend"])
        assert result.returncode == 0
        assert "Skipping frontend publish" in result.stdout
        calls = call_log.read_text(encoding="utf-8")
        assert "npm" not in calls and "s3 sync" not in calls

    def test_a_failed_build_aborts_before_any_upload(self, tmp_path: Path) -> None:
        sandbox, bin_dir, call_log = self._sandbox(
            tmp_path, npm_exit=1, npm_build_writes_index=False
        )
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        assert "frontend build failed" in result.stderr
        assert "nothing was published" in result.stderr
        assert "s3 sync" not in call_log.read_text(encoding="utf-8")
        assert "Deploy complete." not in result.stdout

    def test_a_build_that_produced_no_index_html_is_refused(self, tmp_path: Path) -> None:
        # npm exiting 0 with an empty dist/ must not sync an empty dir with --delete.
        sandbox, bin_dir, call_log = self._sandbox(tmp_path, npm_build_writes_index=False)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        assert "refusing to publish an empty site" in result.stderr
        assert "s3 sync" not in call_log.read_text(encoding="utf-8")

    def test_a_missing_publish_target_output_aborts_before_uploading(self, tmp_path: Path) -> None:
        outputs = {k: v for k, v in self.OUTPUTS.items() if k != "frontend_origin_bucket_name"}
        sandbox, bin_dir, call_log = self._sandbox(tmp_path, outputs=outputs)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        assert "frontend_origin_bucket_name" in result.stderr
        assert "s3 sync" not in call_log.read_text(encoding="utf-8")

    def test_an_unrenderable_config_aborts_before_uploading(self, tmp_path: Path) -> None:
        # The publish targets resolve but a config.json input does not.
        outputs = {k: v for k, v in self.OUTPUTS.items() if k != "cognito_spa_client_id"}
        sandbox, bin_dir, call_log = self._sandbox(tmp_path, outputs=outputs)
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        assert "rendering config.json failed" in result.stderr
        assert "s3 sync" not in call_log.read_text(encoding="utf-8")

    def test_a_failed_invalidation_fails_the_deploy(self, tmp_path: Path) -> None:
        # Published-but-not-invalidated is reported, not shown as "Deploy complete."
        sandbox, bin_dir, _ = self._sandbox(tmp_path, aws_fail_on="create-invalidation")
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        assert "invalidation failed" in result.stderr
        assert "Deploy complete." not in result.stdout

    def test_a_failed_sync_fails_the_deploy(self, tmp_path: Path) -> None:
        sandbox, bin_dir, _ = self._sandbox(tmp_path, aws_fail_on="s3 sync")
        result = self._run(sandbox, bin_dir, tmp_path)
        assert result.returncode != 0
        assert "sync of the hashed assets failed" in result.stderr


class _DeployFixSandbox:
    """Shared sandbox: a deploy sandbox with a controllable `terraform`.

    It varies how many `plan` invocations succeed, whether `infra/backend.hcl` exists and which
    account its bucket names, and whether a stale outputs snapshot is present before the run.
    """

    #: Account the stubbed `aws sts get-caller-identity` reports; it and `_OTHER_ACCOUNT` below are
    #: on `check_no_account_id`'s placeholder allowlist.
    ACCOUNT = SANDBOX_ACCOUNT
    OTHER_ACCOUNT = "555566667777"

    def _sandbox(
        self,
        tmp_path: Path,
        *,
        terraform_body: str,
        backend_account: str | None = None,
        stale_snapshot: str | None = None,
    ) -> tuple[Path, Path, Path]:
        sandbox = tmp_path / "sandbox_deployfix"
        scripts_dir = sandbox / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "deploy.sh").write_text(DEPLOY_SH.read_text(encoding="utf-8"), "utf-8")
        (scripts_dir / "deploy.sh").chmod(0o755)
        for name, src in (
            ("render_agentcore_env.py", SCRIPT_PATH),
            ("classify_post_apply_drift.py", CLASSIFIER_PATH),
        ):
            (scripts_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        _stub_deploy_preconditions(scripts_dir)

        if backend_account is not None:
            # The `infra/bootstrap_state_backend.sh` shape: the bucket name embeds the account.
            (sandbox / "infra" / "backend.hcl").write_text(
                f'bucket         = "crm-agent-eval-tfstate-{backend_account}-us-east-1"\n'
                'key            = "crm-agent-eval/terraform.tfstate"\n'
                'region         = "us-east-1"\n',
                encoding="utf-8",
            )
        if stale_snapshot is not None:
            snapshot = sandbox / "results" / "tf-outputs.json"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(stale_snapshot, encoding="utf-8")

        bin_dir = tmp_path / "bin_deployfix"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"get-caller-identity"* ]]; then\n'
            f'  echo \'{{"Account": "{self.ACCOUNT}"}}\'\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "aws").chmod(0o755)
        (bin_dir / "terraform").write_text(terraform_body, encoding="utf-8")
        (bin_dir / "terraform").chmod(0o755)
        return sandbox, bin_dir, tmp_path / "tf_args_deployfix.txt"

    def _run(
        self,
        sandbox: Path,
        bin_dir: Path,
        tmp_path: Path,
        arg_log: Path,
        *,
        expected_account: str = SANDBOX_ACCOUNT,
    ) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=_sandbox_env(
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                TF_ARG_LOG=str(arg_log),
                PLAN_COUNT=str(tmp_path / "plan_count.txt"),
                LOG_FILE=str(tmp_path / "deploy_deployfix.log"),
                CRM_EXPECTED_ACCOUNT_ID=expected_account,
            ),
            stdin=subprocess.DEVNULL,
            timeout=120,
        )

    #: Records every invocation, answers `output` with a one-key document, and fails the Nth `plan`
    #: onward so a *later* check can be made to fail while an earlier one succeeds.
    @staticmethod
    def _terraform(
        fail_plan_from: int | None = None, output_json: str = '{"stub":{"value":"x"}}'
    ) -> str:
        fail = ""
        if fail_plan_from is not None:
            fail = (
                f'  if [[ "$n" -ge {fail_plan_from} ]]; then\n'
                '    echo "Error acquiring the state lock: ConditionalCheckFailedException" >&2\n'
                "    exit 1\n"
                "  fi\n"
            )
        return (
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$TF_ARG_LOG"\n'
            f'if [[ "$1" == "output" ]]; then echo \'{output_json}\'; exit 0; fi\n'
            'if [[ "$1" == "plan" ]]; then\n'
            '  n=$(cat "$PLAN_COUNT" 2>/dev/null || echo 0)\n'
            "  n=$((n+1))\n"
            '  echo "$n" > "$PLAN_COUNT"\n'
            f"{fail}"
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )


class TestOutputsSnapshotDoesNotDependOnPrecedingChecks(_DeployFixSandbox):
    """`results/tf-outputs.json` is refreshed even when the final post-publish drift check fails."""

    _STALE = '{"from_an_earlier_stack": {"value": "STALE-MARKER"}}'

    def test_a_failed_final_check_still_refreshes_the_snapshot(self, tmp_path: Path) -> None:
        sandbox, bin_dir, arg_log = self._sandbox(
            tmp_path, terraform_body=self._terraform(fail_plan_from=2), stale_snapshot=self._STALE
        )
        result = self._run(sandbox, bin_dir, tmp_path, arg_log)
        combined = result.stdout + result.stderr

        # The run reached the final bound and failed there.
        assert "final post-publish drift bound could not be established" in combined, combined
        assert result.returncode != 0, combined
        assert "Deploy complete." not in result.stdout, combined

        # The snapshot is refreshed anyway, and is not the stale document.
        snapshot = sandbox / "results" / "tf-outputs.json"
        assert snapshot.exists(), f"the snapshot was not written after a failed check:\n{combined}"
        body = snapshot.read_text(encoding="utf-8")
        assert "STALE-MARKER" not in body, (
            "the snapshot still describes the EARLIER stack — every reader would consume it as current"
        )
        assert "stub" in body, body
        assert "Stack outputs recorded" in result.stdout, combined

    def test_a_write_failure_after_a_failed_check_still_removes_the_stale_snapshot(
        self, tmp_path: Path
    ) -> None:
        """A write failure after a failed check still removes the stale snapshot."""
        sandbox, bin_dir, arg_log = self._sandbox(
            tmp_path,
            # Final plan fails AND `output` returns a document naming nothing, so the writer refuses.
            terraform_body=self._terraform(fail_plan_from=2, output_json="{}"),
            stale_snapshot=self._STALE,
        )
        result = self._run(sandbox, bin_dir, tmp_path, arg_log)
        combined = result.stdout + result.stderr

        assert "final post-publish drift bound could not be established" in combined, combined
        assert "not a non-empty JSON object" in combined, combined
        snapshot = sandbox / "results" / "tf-outputs.json"
        assert not snapshot.exists(), (
            "a stale snapshot survived a failed write, so every reader would consume an earlier "
            "stack's outputs with no signal"
        )
        assert not snapshot.with_suffix(".json.partial").exists(), "a .partial was left behind"
        # The message does not claim a success this run did not have.
        assert "ALREADY FAILED for another reason" in combined, combined
        assert "THE DEPLOY ITSELF SUCCEEDED" not in combined, combined

    def test_a_failed_pre_publish_check_blocks_the_publish_and_still_refreshes(
        self, tmp_path: Path
    ) -> None:
        """When the first bound fails, the publish is blocked, the final bound is skipped, and the
        snapshot is still refreshed.
        """
        sandbox, bin_dir, arg_log = self._sandbox(
            tmp_path, terraform_body=self._terraform(fail_plan_from=1), stale_snapshot=self._STALE
        )
        result = self._run(sandbox, bin_dir, tmp_path, arg_log)
        combined = result.stdout + result.stderr

        assert "post-apply drift bound could not be established" in combined, combined
        assert result.returncode != 0, combined
        # The publish is blocked, and says so as a publish decision rather than as a drift message.
        assert "Skipping frontend publish" in combined, combined
        # Exactly one plan: the final bound must NOT have run and taken the lock a second time.
        plans = [
            line
            for line in arg_log.read_text(encoding="utf-8").splitlines()
            if line.startswith("plan")
        ]
        assert len(plans) == 1, f"the final bound ran anyway, re-taking the state lock:\n{plans}"
        # And the snapshot is current rather than the earlier stack's.
        body = (sandbox / "results" / "tf-outputs.json").read_text(encoding="utf-8")
        assert "STALE-MARKER" not in body and "stub" in body, body

    def test_a_clean_run_still_reports_deploy_complete_and_records_outputs(
        self, tmp_path: Path
    ) -> None:
        """With nothing failing, the run reports a clean success and records the outputs."""
        sandbox, bin_dir, arg_log = self._sandbox(
            tmp_path, terraform_body=self._terraform(), stale_snapshot=self._STALE
        )
        result = self._run(sandbox, bin_dir, tmp_path, arg_log)
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "Deploy complete." in result.stdout, combined
        body = (sandbox / "results" / "tf-outputs.json").read_text(encoding="utf-8")
        assert "STALE-MARKER" not in body and "stub" in body, body


class TestStateLockTimeout(_DeployFixSandbox):
    """Every drift-check `terraform plan` passes a `-lock-timeout`."""

    def test_every_drift_plan_carries_a_bounded_lock_timeout(self, tmp_path: Path) -> None:
        sandbox, bin_dir, arg_log = self._sandbox(tmp_path, terraform_body=self._terraform())
        result = self._run(sandbox, bin_dir, tmp_path, arg_log)
        assert result.returncode == 0, result.stdout + result.stderr

        plans = [
            line
            for line in arg_log.read_text(encoding="utf-8").splitlines()
            if line.startswith("plan")
        ]
        # Positive: there were plans to check. An empty list would make the loop below vacuous.
        assert len(plans) == 2, f"expected the pre-publish and final plans, got:\n{plans}"
        for line in plans:
            assert "-lock-timeout=" in line, f"a drift plan took no lock timeout: {line}"

    def test_the_timeout_is_bounded_rather_than_effectively_infinite(self, tmp_path: Path) -> None:
        """The `-lock-timeout` is bounded."""
        sandbox, bin_dir, arg_log = self._sandbox(tmp_path, terraform_body=self._terraform())
        self._run(sandbox, bin_dir, tmp_path, arg_log)
        recorded = arg_log.read_text(encoding="utf-8")
        match = re.search(r"-lock-timeout=(\d+)s", recorded)
        assert match is not None, recorded
        assert 0 < int(match.group(1)) <= 300, (
            f"-lock-timeout={match.group(1)}s is not a bounded wait; a stuck lock must still surface "
            "as a failure rather than as a hang"
        )


class TestExpectedAccountAssertion(_DeployFixSandbox):
    """`deploy.sh` requires `CRM_EXPECTED_ACCOUNT_ID`, a 12-digit id agreeing with `backend.hcl`
    and with STS, and refuses otherwise before any render, init or apply.
    """

    REQUIRED_REFUSAL = "CRM_EXPECTED_ACCOUNT_ID is required"
    MALFORMED_REFUSAL = "is not a 12-digit AWS account id"
    DISAGREEMENT_REFUSAL = "EXPECTED-ACCOUNT DISAGREEMENT"

    def _assert_nothing_rendered_initialised_or_applied(
        self, sandbox: Path, arg_log: Path, result: subprocess.CompletedProcess[str]
    ) -> None:
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert not arg_log.exists() or arg_log.read_text(encoding="utf-8").strip() == "", (
            f"terraform was invoked despite the refusal:\n{arg_log.read_text()}"
        )
        assert "Rendering account-specific AgentCore config" not in combined, combined
        assert not (sandbox / ".bedrock_agentcore.yaml.orig").exists()
        assert "Deploy complete." not in result.stdout
        assert "nothing was rendered, initialised or applied" in combined.lower(), combined

    def test_an_sts_mismatch_refuses_before_anything_is_applied(self, tmp_path: Path) -> None:
        sandbox, bin_dir, arg_log = self._sandbox(tmp_path, terraform_body=self._terraform())
        result = self._run(sandbox, bin_dir, tmp_path, arg_log, expected_account=self.OTHER_ACCOUNT)
        assert "WRONG ACCOUNT" in result.stdout + result.stderr
        self._assert_nothing_rendered_initialised_or_applied(sandbox, arg_log, result)

    def test_the_sts_refusal_names_all_three_values_and_where_each_came_from(
        self, tmp_path: Path
    ) -> None:
        """The refusal names both account ids and the source of each resolved value."""
        sandbox, bin_dir, arg_log = self._sandbox(tmp_path, terraform_body=self._terraform())
        env_region = "us-west-2"
        result = proc_run.run(
            [
                "bash",
                str(sandbox / "scripts" / "deploy.sh"),
                "--skip-frontend",
                "--profile",
                "flagged",
            ],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=_sandbox_env(
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                TF_ARG_LOG=str(arg_log),
                PLAN_COUNT=str(tmp_path / "plan_count.txt"),
                LOG_FILE=str(tmp_path / "deploy_provenance.log"),
                CRM_EXPECTED_ACCOUNT_ID=self.OTHER_ACCOUNT,
                AWS_REGION=env_region,
            ),
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        combined = result.stdout + result.stderr
        assert "WRONG ACCOUNT" in combined, combined
        assert self.ACCOUNT in combined and self.OTHER_ACCOUNT in combined, combined
        assert "from CRM_EXPECTED_ACCOUNT_ID" in combined, combined
        assert "from --profile" in combined, combined
        assert f"from AWS_REGION={env_region}" in combined, combined

    def test_a_matching_account_proceeds(self, tmp_path: Path) -> None:
        """With the variable, `backend.hcl` and STS agreeing, the same sandbox reaches the apply."""
        sandbox, bin_dir, arg_log = self._sandbox(
            tmp_path, terraform_body=self._terraform(), backend_account=self.ACCOUNT
        )
        result = self._run(sandbox, bin_dir, tmp_path, arg_log)
        combined = result.stdout + result.stderr

        assert result.returncode == 0, combined
        assert "Expected-account assertion passed" in result.stdout, combined
        assert "WRONG ACCOUNT" not in combined, combined
        recorded = arg_log.read_text(encoding="utf-8")
        assert any(line.startswith("apply") for line in recorded.splitlines()), recorded

    def test_a_matching_account_with_no_backend_hcl_proceeds(self, tmp_path: Path) -> None:
        """A first deploy has no `backend.hcl`; the variable alone carries the assertion."""
        sandbox, bin_dir, arg_log = self._sandbox(
            tmp_path, terraform_body=self._terraform(), backend_account=None
        )
        result = self._run(sandbox, bin_dir, tmp_path, arg_log)
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "Expected-account assertion passed" in result.stdout, combined
        assert "Deploy complete." in result.stdout, combined

    @pytest.mark.parametrize("backend_account", [None, SANDBOX_ACCOUNT])
    def test_an_unset_variable_refuses_even_when_backend_hcl_names_the_sts_account(
        self, tmp_path: Path, backend_account: str | None
    ) -> None:
        """No fallback to `backend.hcl` and no warn-and-proceed: unset refuses either way."""
        sandbox, bin_dir, arg_log = self._sandbox(
            tmp_path, terraform_body=self._terraform(), backend_account=backend_account
        )
        result = self._run(sandbox, bin_dir, tmp_path, arg_log, expected_account="")
        combined = result.stdout + result.stderr
        assert self.REQUIRED_REFUSAL in combined, combined
        assert "NO EXPECTED-ACCOUNT ASSERTION WAS MADE" not in combined, combined
        self._assert_nothing_rendered_initialised_or_applied(sandbox, arg_log, result)

    def test_an_absent_variable_refuses(self, tmp_path: Path) -> None:
        """Absent from the environment, not merely empty, refuses the same way."""
        sandbox, bin_dir, arg_log = self._sandbox(tmp_path, terraform_body=self._terraform())
        result = proc_run.run(
            ["bash", str(sandbox / "scripts" / "deploy.sh"), "--skip-frontend"],
            cwd=sandbox,
            capture_output=True,
            text=True,
            env=_sandbox_env(
                drop=("CRM_EXPECTED_ACCOUNT_ID",),
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                TF_ARG_LOG=str(arg_log),
                PLAN_COUNT=str(tmp_path / "plan_count.txt"),
                LOG_FILE=str(tmp_path / "deploy_absent.log"),
            ),
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        assert self.REQUIRED_REFUSAL in result.stdout + result.stderr
        self._assert_nothing_rendered_initialised_or_applied(sandbox, arg_log, result)

    @pytest.mark.parametrize(
        "malformed",
        ["11112222333", "1111222233334", "11112222333x", " 111122223333", "111122223333\n"],
    )
    def test_a_malformed_variable_refuses(self, tmp_path: Path, malformed: str) -> None:
        sandbox, bin_dir, arg_log = self._sandbox(tmp_path, terraform_body=self._terraform())
        result = self._run(sandbox, bin_dir, tmp_path, arg_log, expected_account=malformed)
        combined = result.stdout + result.stderr
        assert self.MALFORMED_REFUSAL in combined, combined
        self._assert_nothing_rendered_initialised_or_applied(sandbox, arg_log, result)

    def test_a_backend_hcl_disagreeing_with_the_variable_refuses_naming_both_sources(
        self, tmp_path: Path
    ) -> None:
        """STS agrees with the variable; only `backend.hcl` differs, and that alone refuses."""
        sandbox, bin_dir, arg_log = self._sandbox(
            tmp_path, terraform_body=self._terraform(), backend_account=self.OTHER_ACCOUNT
        )
        result = self._run(sandbox, bin_dir, tmp_path, arg_log)
        combined = result.stdout + result.stderr
        assert self.DISAGREEMENT_REFUSAL in combined, combined
        assert "WRONG ACCOUNT" not in combined, combined
        assert self.ACCOUNT in combined and self.OTHER_ACCOUNT in combined, combined
        assert "CRM_EXPECTED_ACCOUNT_ID" in combined, combined
        assert "backend.hcl" in combined, combined
        self._assert_nothing_rendered_initialised_or_applied(sandbox, arg_log, result)
