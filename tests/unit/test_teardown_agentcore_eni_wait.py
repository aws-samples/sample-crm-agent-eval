# SPDX-License-Identifier: MIT
"""`scripts/teardown.sh` waits for AgentCore agentic_ai ENIs to release the Runtime SG, then retries.

Runs the script in the fake-`aws`/`terraform` sandbox from `test_teardown_deletion_protection.py`,
with the fakes extended to answer a second `state list`, `state show` for the Runtime SG, a
destroy-exit sequence and an ENI-count sequence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit import test_teardown_deletion_protection as sandbox
from tests.unit.test_teardown_deletion_protection import (
    _STATE_WITH_ALB,
    PROFILE,
    REGION,
    Run,
    _teardown,
)

RUNTIME_SG = "module.vpc.aws_security_group.agent_runtime"
SG_ID = "sg-0001"

_LOG_LINE = 'printf \'{tool} %s\\n\' "$*" >> "$CALL_LOG"\n'

_EXTRA_TERRAFORM = """_n() { local f="$(dirname "$CALL_LOG")/$1.n" n=0; [[ -f "$f" ]] && n=$(cat "$f"); n=$((n + 1)); echo "$n" > "$f"; echo "$n"; }
_nth() { local i=0 v last=""; for v in $2; do i=$((i + 1)); last="$v"; [[ "$i" -eq "$1" ]] && { echo "$v"; return; }; done; echo "$last"; }
if [[ "$1" == "state" && "$2" == "list" && -n "${FAKE_POST_STATE+x}" ]]; then
  if [[ "$(_n state)" -gt 1 ]]; then printf '%s\\n' "$FAKE_POST_STATE"; exit 0; fi
fi
if [[ "$1" == "state" && "$2" == "show" ]]; then
  printf '# %s:\\nresource "aws_security_group" "agent_runtime" {\\n    arn  = "arn"\\n    id   = "%s"\\n}\\n' "$3" "$FAKE_SG_ID"
  exit 0
fi
if [[ "$1" == "destroy" && -n "${FAKE_DESTROY_RCS:-}" ]]; then
  exit "$(_nth "$(_n destroy)" "$FAKE_DESTROY_RCS")"
fi
"""

_EXTRA_AWS = """_n() { local f="$(dirname "$CALL_LOG")/$1.n" n=0; [[ -f "$f" ]] && n=$(cat "$f"); n=$((n + 1)); echo "$n" > "$f"; echo "$n"; }
_nth() { local i=0 v last=""; for v in $2; do i=$((i + 1)); last="$v"; [[ "$i" -eq "$1" ]] && { echo "$v"; return; }; done; echo "$last"; }
if [[ "$1" == "ec2" && "$2" == "describe-network-interfaces" ]]; then
  v="$(_nth "$(_n eni)" "$FAKE_ENI_COUNTS")"
  if [[ "$v" == "denied" ]]; then echo "An error occurred (UnauthorizedOperation)" >&2; exit 254; fi
  echo "$v"; exit 0
fi
"""


def _extend(body: str, tool: str, extra: str) -> str:
    log_line = _LOG_LINE.format(tool=tool)
    assert body.count(log_line) == 1
    return body.replace(log_line, log_line + extra, 1)


@pytest.fixture
def eni_sandbox(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(
        sandbox, "_FAKE_TERRAFORM", _extend(sandbox._FAKE_TERRAFORM, "terraform", _EXTRA_TERRAFORM)
    )
    monkeypatch.setattr(sandbox, "_FAKE_AWS", _extend(sandbox._FAKE_AWS, "aws", _EXTRA_AWS))
    monkeypatch.setenv("FAKE_SG_ID", SG_ID)
    monkeypatch.setenv("TEARDOWN_ENI_POLL_SECONDS", "0")
    return monkeypatch


def _run(
    tmp_path: Path,
    env: pytest.MonkeyPatch,
    *,
    post_state: str,
    destroy_rcs: str,
    eni_counts: str,
    wait_seconds: str = "30",
) -> Run:
    env.setenv("FAKE_POST_STATE", post_state)
    env.setenv("FAKE_DESTROY_RCS", destroy_rcs)
    env.setenv("FAKE_ENI_COUNTS", eni_counts)
    env.setenv("TEARDOWN_ENI_WAIT_SECONDS", wait_seconds)
    return _teardown(tmp_path, state_list=f"{_STATE_WITH_ALB}\n{RUNTIME_SG}")


def _count(run: Run, prefix: str) -> int:
    return sum(call.startswith(prefix) for call in run.calls)


def test_enis_released_within_the_window_retry_destroy_once_and_succeed(
    tmp_path: Path, eni_sandbox: pytest.MonkeyPatch
) -> None:
    run = _run(tmp_path, eni_sandbox, post_state=RUNTIME_SG, destroy_rcs="1 0", eni_counts="2 2 0")
    assert run.returncode == 0, run.stderr
    assert _count(run, "terraform destroy") == 2
    assert _count(run, "aws ec2 describe-network-interfaces") == 3
    describe = next(c for c in run.calls if c.startswith("aws ec2 describe-network-interfaces"))
    assert f"Name=group-id,Values={SG_ID}" in describe
    assert "InterfaceType=='agentic_ai'" in describe
    assert f"--profile {PROFILE}" in describe and f"--region {REGION}" in describe
    assert "waiting up to 30s" in run.stdout
    assert "Teardown complete" in run.stdout


def test_enis_never_released_fail_with_a_message_naming_the_sg_and_count(
    tmp_path: Path, eni_sandbox: pytest.MonkeyPatch
) -> None:
    eni_sandbox.setenv("TEARDOWN_ENI_POLL_SECONDS", "1")
    run = _run(
        tmp_path,
        eni_sandbox,
        post_state=RUNTIME_SG,
        destroy_rcs="1",
        eni_counts="2",
        wait_seconds="2",
    )
    assert run.returncode == 1
    assert _count(run, "terraform destroy") == 1
    assert _count(run, "aws ec2 describe-network-interfaces") >= 2
    for fragment in (
        RUNTIME_SG,
        SG_ID,
        "2 AgentCore-managed ENI(s) (InterfaceType agentic_ai)",
        "after waiting 2s",
        "up to 8 hours",
        "cannot be detached or deleted by the account",
        "the only remedy is to wait for AgentCore to release them, then re-run scripts/teardown.sh",
    ):
        assert fragment in run.stderr, fragment
    assert "requester-managed" not in run.stderr
    assert "Teardown complete" not in run.stdout


@pytest.mark.parametrize(
    ("post_state", "eni_counts"),
    [
        pytest.param(
            f"module.api_origin.aws_lb_listener.this\n{RUNTIME_SG}", "2", id="other-resources-left"
        ),
        pytest.param(RUNTIME_SG, "0", id="no-agentic-enis"),
        pytest.param(RUNTIME_SG, "denied", id="eni-count-unreadable"),
    ],
)
def test_an_unrelated_destroy_failure_does_not_wait_or_retry(
    tmp_path: Path, eni_sandbox: pytest.MonkeyPatch, post_state: str, eni_counts: str
) -> None:
    run = _run(tmp_path, eni_sandbox, post_state=post_state, destroy_rcs="1", eni_counts=eni_counts)
    assert run.returncode == 1
    assert _count(run, "terraform destroy") == 1
    assert "waiting up to" not in run.stdout
    assert "terraform destroy failed or was declined (exit 1)" in run.stderr


def test_data_sources_left_in_state_do_not_block_the_wait(
    tmp_path: Path, eni_sandbox: pytest.MonkeyPatch
) -> None:
    post = f"data.aws_caller_identity.current\nmodule.vpc.data.aws_vpc.this\n{RUNTIME_SG}"
    run = _run(tmp_path, eni_sandbox, post_state=post, destroy_rcs="1 0", eni_counts="1 0")
    assert run.returncode == 0, run.stderr
    assert _count(run, "terraform destroy") == 2


@pytest.mark.parametrize("variable", ["TEARDOWN_ENI_WAIT_SECONDS", "TEARDOWN_ENI_POLL_SECONDS"])
def test_a_non_numeric_wait_setting_refuses_before_destroy(
    tmp_path: Path, eni_sandbox: pytest.MonkeyPatch, variable: str
) -> None:
    eni_sandbox.setenv(variable, "5m")
    run = _teardown(tmp_path)
    assert run.returncode == 1
    assert not run.has("terraform destroy")
    assert variable in run.stderr
