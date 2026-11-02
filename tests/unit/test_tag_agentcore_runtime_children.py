# SPDX-License-Identifier: MIT
"""Behaviour of ``scripts/tag_agentcore_runtime_children.sh`` against a stub ``aws`` on ``PATH``.

The stub records each argv as a JSON line and answers the reads the script parses; environment
knobs make individual calls fail or answer short. Also asserts ``scripts/deploy.sh`` runs the step
after the deployed-parity gate and before the drift bound, deferring its failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import proc_run

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "scripts" / "tag_agentcore_runtime_children.sh"
DEPLOY_SH: Final = REPO_ROOT / "scripts" / "deploy.sh"
ACCOUNT: Final = "123456789012"
REGION: Final = "us-east-1"
PROFILE: Final = "crm-test"
RUNTIME_ID: Final = "crm_agent_eval_runtime-AbCdEf1234"
RUNTIME_ARN: Final = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
ENDPOINT_ARN: Final = f"{RUNTIME_ARN}/runtime-endpoint/DEFAULT"
IDENTITY_ARN: Final = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default/"
    f"workload-identity/{RUNTIME_ID}"
)
RUNTIME_TAGS: Final = {
    "service": "crm-agent-eval",
    "environment": "dev",
    "owner": "crm-agent-eval-team",
    "cost-center": "crm-agent-eval",
    "data-classification": "confidential",
    "AppName": "crm-agent-eval",
}

STUB_AWS: Final = f"""#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_AWS_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
state_path = os.environ["STUB_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {{}}
fail = os.environ.get("STUB_FAIL", "")
op = argv[1] if len(argv) > 1 else ""
if op == fail:
    sys.stderr.write("AccessDeniedException\\n")
    sys.exit(254)
def arg(flag):
    return argv[argv.index(flag) + 1]
if op == "list-tags-for-resource":
    arn = arg("--resource-arn")
    if arn == "{RUNTIME_ARN}":
        print(json.dumps({{"tags": json.loads(os.environ["STUB_RUNTIME_TAGS"])}}))
    else:
        tags = state.get(arn, {{}})
        if os.environ.get("STUB_DROP_ON_READBACK") and arn in state:
            tags = {{k: v for k, v in tags.items() if k != os.environ["STUB_DROP_ON_READBACK"]}}
        print(json.dumps({{"tags": tags}}))
elif op == "get-agent-runtime-endpoint":
    print(json.dumps({{"agentRuntimeEndpointArn": "{ENDPOINT_ARN}", "name": "DEFAULT"}}))
elif op == "get-agent-runtime":
    if os.environ.get("STUB_NO_IDENTITY"):
        print(json.dumps({{"agentRuntimeId": "{RUNTIME_ID}"}}))
    else:
        print(json.dumps({{"workloadIdentityDetails": {{"workloadIdentityArn": "{IDENTITY_ARN}"}}}}))
elif op == "tag-resource":
    arn = arg("--resource-arn")
    state[arn] = {{**state.get(arn, {{}}), **json.loads(arg("--tags"))}}
    json.dump(state, open(state_path, "w"))
    print("{{}}")
else:
    sys.stderr.write("unexpected call\\n")
    sys.exit(255)
"""

Call = list[str]


def _run(
    tmp_path: Path,
    *args: str,
    runtime_tags: dict[str, str] | None = None,
    **env_extra: str,
) -> tuple[subprocess.CompletedProcess[str], list[Call]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "aws"
    stub.write_text(STUB_AWS, encoding="utf-8")
    stub.chmod(0o755)
    jq = shutil.which("jq")
    assert jq is not None, "jq is required by the script"
    log = tmp_path / "aws-calls.jsonl"
    log.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:{Path(jq).parent}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "STUB_AWS_LOG": str(log),
        "STUB_STATE": str(tmp_path / "state.json"),
        "STUB_RUNTIME_TAGS": json.dumps(RUNTIME_TAGS if runtime_tags is None else runtime_tags),
        **env_extra,
    }
    argv = list(args) or [
        "--profile",
        PROFILE,
        "--region",
        REGION,
        "--runtime-arn",
        RUNTIME_ARN,
    ]
    result = proc_run.run(
        ["bash", str(SCRIPT), *argv], capture_output=True, text=True, check=False, env=env
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return result, calls


def _value(call: Call, flag: str) -> str:
    return call[call.index(flag) + 1]


def _ops(calls: list[Call], op: str) -> list[Call]:
    return [c for c in calls if c[:2] == ["bedrock-agentcore-control", op]]


def _tagged(calls: list[Call]) -> dict[str, Any]:
    return {
        _value(c, "--resource-arn"): json.loads(_value(c, "--tags"))
        for c in _ops(calls, "tag-resource")
    }


class TestHappyPath:
    def test_both_children_get_exactly_the_runtime_tags(self, tmp_path: Path) -> None:
        result, calls = _run(tmp_path)
        assert result.returncode == 0, result.stderr
        assert _tagged(calls) == {ENDPOINT_ARN: RUNTIME_TAGS, IDENTITY_ARN: RUNTIME_TAGS}

    def test_the_children_are_discovered_from_the_runtime_api(self, tmp_path: Path) -> None:
        _result, calls = _run(tmp_path)
        (endpoint,) = _ops(calls, "get-agent-runtime-endpoint")
        assert _value(endpoint, "--agent-runtime-id") == RUNTIME_ID
        assert _value(endpoint, "--endpoint-name") == "DEFAULT"
        (runtime,) = _ops(calls, "get-agent-runtime")
        assert _value(runtime, "--agent-runtime-id") == RUNTIME_ID

    def test_each_child_is_read_back_after_it_is_tagged(self, tmp_path: Path) -> None:
        _result, calls = _run(tmp_path)
        for arn in (ENDPOINT_ARN, IDENTITY_ARN):
            indices = [
                i
                for i, c in enumerate(calls)
                if c[1] in ("tag-resource", "list-tags-for-resource")
                and _value(c, "--resource-arn") == arn
            ]
            assert [calls[i][1] for i in indices] == ["tag-resource", "list-tags-for-resource"]

    def test_every_call_pins_profile_region_and_pager(self, tmp_path: Path) -> None:
        _result, calls = _run(tmp_path)
        assert calls
        for call in calls:
            assert _value(call, "--profile") == PROFILE, call
            assert _value(call, "--region") == REGION, call
            assert "--no-cli-pager" in call, call

    def test_a_rerun_is_idempotent(self, tmp_path: Path) -> None:
        first, _ = _run(tmp_path)
        second, calls = _run(tmp_path)
        assert (first.returncode, second.returncode) == (0, 0)
        assert _tagged(calls) == {ENDPOINT_ARN: RUNTIME_TAGS, IDENTITY_ARN: RUNTIME_TAGS}


class TestFailsLoudly:
    @pytest.mark.parametrize(
        "op",
        [
            "list-tags-for-resource",
            "get-agent-runtime-endpoint",
            "get-agent-runtime",
            "tag-resource",
        ],
    )
    def test_a_failed_call_exits_one(self, tmp_path: Path, op: str) -> None:
        result, _calls = _run(tmp_path, STUB_FAIL=op)
        assert result.returncode == 1, (op, result.stdout, result.stderr)
        assert "tag_agentcore_runtime_children:" in result.stderr

    def test_an_untagged_runtime_is_refused_before_any_tagging(self, tmp_path: Path) -> None:
        result, calls = _run(tmp_path, runtime_tags={})
        assert result.returncode == 1
        assert "carries no tags" in result.stderr
        assert _ops(calls, "tag-resource") == []

    def test_a_runtime_without_a_workload_identity_fails(self, tmp_path: Path) -> None:
        result, _calls = _run(tmp_path, STUB_NO_IDENTITY="1")
        assert result.returncode == 1
        assert "workloadIdentityArn" in result.stderr

    def test_a_read_back_missing_a_tag_fails_naming_it(self, tmp_path: Path) -> None:
        result, _calls = _run(tmp_path, STUB_DROP_ON_READBACK="AppName")
        assert result.returncode == 1
        assert "AppName" in result.stderr

    @pytest.mark.parametrize(
        "argv",
        [
            ["--profile", PROFILE, "--region", REGION],
            ["--profile", PROFILE, "--region", "us-west-2", "--runtime-arn", RUNTIME_ARN],
            ["--profile", PROFILE, "--region", REGION, "--runtime-arn", ENDPOINT_ARN],
            ["--bogus"],
        ],
        ids=["no-arn", "arn-in-other-region", "not-a-runtime-arn", "unknown-flag"],
    )
    def test_usage_errors_exit_two_without_calling_aws(
        self, tmp_path: Path, argv: list[str]
    ) -> None:
        result, calls = _run(tmp_path, *argv)
        assert result.returncode == 2, result.stderr
        assert calls == []


class TestDeployRunsTheStep:
    TEXT: Final = DEPLOY_SH.read_text(encoding="utf-8")
    INVOCATION: Final = 'bash "${SCRIPT_DIR}/tag_agentcore_runtime_children.sh"'

    def test_deploy_invokes_the_helper_once_with_the_resolved_target(self) -> None:
        assert self.TEXT.count(self.INVOCATION) == 1
        start = self.TEXT.index(self.INVOCATION)
        call = self.TEXT[start : self.TEXT.index("\n\n", start)]
        assert '--profile "$RESOLVED_PROFILE"' in call
        assert '--region "$RESOLVED_REGION"' in call
        assert "terraform output -raw agentcore_runtime_arn" in self.TEXT[start - 300 : start]

    def test_it_runs_after_the_parity_gate_and_before_the_drift_bound(self) -> None:
        position = self.TEXT.index(self.INVOCATION)
        assert self.TEXT.index("-m scripts.smoke.deployed_parity --deploy-gate") < position
        assert position < self.TEXT.index('check_drift_bound "post-apply" ""')

    def test_its_failure_and_a_missing_output_are_both_deferred_failures(self) -> None:
        start = self.TEXT.index("terraform output -raw agentcore_runtime_arn")
        block = self.TEXT[start : self.TEXT.index("\nfi\n", start)]
        assert block.count("defer_failure ") == 2
        assert "|| true" not in block
