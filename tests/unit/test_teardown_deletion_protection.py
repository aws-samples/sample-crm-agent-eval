# SPDX-License-Identifier: MIT
"""`scripts/teardown.sh` disables deletion protection on this stack's ALB just before destroy.

Runs a copy of the script in a sandbox whose PATH holds fake `aws` and `terraform` executables that
append each call to one log, so call order is observable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
TEARDOWN_SH = REPO_ROOT / "scripts" / "teardown.sh"
RENDER_SCRIPT = REPO_ROOT / "scripts" / "render_agentcore_env.py"

ACCOUNT = "111122223333"
REGION = "eu-west-2"
PROFILE = "teardown-test-profile"
ALB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/crm-api-origin/50dc6c495c0c9188"
ALB_ADDRESS = "module.api_origin.aws_lb.this"

_STATE_WITH_ALB = "\n".join(
    [
        "module.api_origin.aws_lb_listener.this",
        ALB_ADDRESS,
        "module.api_origin.aws_lb_target_group.execute_api",
    ]
)

_FAKE_AWS = """#!/usr/bin/env bash
printf 'aws %s\\n' "$*" >> "$CALL_LOG"
if [[ "$*" == *"get-caller-identity"* ]]; then
  echo '{"Account": "111122223333"}'
  exit 0
fi
if [[ "$1" == "elbv2" && "$2" == "describe-load-balancers" ]]; then
  case "${FAKE_ALB_AFTER:-gone}" in
    gone) echo "An error occurred (LoadBalancerNotFound) when calling DescribeLoadBalancers" >&2; exit 254 ;;
    exists) echo '{"LoadBalancers": [{}]}'; exit 0 ;;
    *) echo "An error occurred (AccessDenied) when calling DescribeLoadBalancers" >&2; exit 254 ;;
  esac
fi
if [[ "$1" == "elbv2" ]]; then
  if [[ "$*" == *"Value=false"* && "${FAKE_MODIFY_FAILS:-}" == "1" ]]; then echo "AccessDenied" >&2; exit 254; fi
  if [[ "$*" == *"Value=true"* && "${FAKE_REPROTECT_FAILS:-}" == "1" ]]; then echo "AccessDenied" >&2; exit 254; fi
  echo '{"Attributes": []}'
  exit 0
fi
exit 0
"""

_FAKE_TERRAFORM = """#!/usr/bin/env bash
printf 'terraform %s\\n' "$*" >> "$CALL_LOG"
case "$1" in
  state)
    if [[ "${FAKE_STATE_FAILS:-}" == "1" ]]; then echo "backend unreachable" >&2; exit 1; fi
    if [[ -n "${FAKE_STATE_LIST_FILE:-}" ]]; then cat "$FAKE_STATE_LIST_FILE"; exit 0; fi
    printf '%s\\n' "$FAKE_STATE_LIST"; exit 0 ;;
  destroy) exit "${FAKE_DESTROY_RC:-0}" ;;
  output)
    if [[ "${FAKE_OUTPUT_FAILS:-}" == "1" ]]; then echo "no outputs" >&2; exit 1; fi
    printf '%s' "$FAKE_ALB_ARN"; exit 0 ;;
esac
exit 0
"""


@dataclass(frozen=True)
class Run:
    returncode: int
    stdout: str
    stderr: str
    calls: list[str]

    def index(self, prefix: str) -> int:
        matches = [i for i, call in enumerate(self.calls) if call.startswith(prefix)]
        assert len(matches) == 1, f"expected one {prefix!r} call, got {matches} in {self.calls}"
        return matches[0]

    def has(self, prefix: str) -> bool:
        return any(call.startswith(prefix) for call in self.calls)


def _teardown(
    tmp_path: Path,
    *,
    state_list: str = _STATE_WITH_ALB,
    state_list_file: Path | None = None,
    alb_arn: str = ALB_ARN,
    modify_fails: bool = False,
    output_fails: bool = False,
    state_fails: bool = False,
    destroy_rc: int = 0,
    alb_after: str = "gone",
    reprotect_fails: bool = False,
) -> Run:
    sandbox = tmp_path / "sandbox"
    scripts_dir = sandbox / "scripts"
    scripts_dir.mkdir(parents=True)
    (sandbox / "infra").mkdir()
    script = scripts_dir / "teardown.sh"
    script.write_text(TEARDOWN_SH.read_text(encoding="utf-8"), encoding="utf-8")
    (scripts_dir / "render_agentcore_env.py").write_text(
        RENDER_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("aws", _FAKE_AWS), ("terraform", _FAKE_TERRAFORM)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)

    call_log = tmp_path / "calls.log"
    call_log.touch()
    system_dirs = [p for p in ("/usr/bin", "/bin", "/usr/local/bin") if Path(p).is_dir()]
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
        }
    }
    env.update(
        PATH=os.pathsep.join([str(bin_dir), *system_dirs]),
        LOG_FILE=str(tmp_path / "teardown.log"),
        CALL_LOG=str(call_log),
        FAKE_STATE_LIST=state_list,
        FAKE_STATE_LIST_FILE=str(state_list_file) if state_list_file else "",
        FAKE_ALB_ARN=alb_arn,
        FAKE_MODIFY_FAILS="1" if modify_fails else "",
        FAKE_OUTPUT_FAILS="1" if output_fails else "",
        FAKE_STATE_FAILS="1" if state_fails else "",
        FAKE_DESTROY_RC=str(destroy_rc),
        FAKE_ALB_AFTER=alb_after,
        FAKE_REPROTECT_FAILS="1" if reprotect_fails else "",
    )
    result = proc_run.run(
        [
            "bash",
            str(script),
            "--auto-approve",
            "--profile",
            PROFILE,
            "--region",
            REGION,
            "--template",
            str(sandbox / "absent-template.yaml"),
        ],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return Run(
        result.returncode,
        result.stdout,
        result.stderr,
        call_log.read_text(encoding="utf-8").splitlines(),
    )


def test_protection_is_disabled_on_the_state_alb_before_destroy(tmp_path: Path) -> None:
    run = _teardown(tmp_path)
    assert run.returncode == 0, run.stderr
    modify = run.calls[run.index("aws elbv2 modify-load-balancer-attributes")]
    disable = run.index("aws elbv2 modify-load-balancer-attributes")
    assert disable < run.index("terraform destroy")
    assert run.index("terraform state list") < disable
    assert run.index("terraform output -raw api_origin_alb_arn") < disable
    assert run.index("terraform destroy") < run.index("aws elbv2 describe-load-balancers")
    assert not any("Value=true" in call for call in run.calls)
    argv = modify.split()
    assert argv[argv.index("--load-balancer-arn") + 1] == ALB_ARN
    assert argv[argv.index("--attributes") + 1] == "Key=deletion_protection.enabled,Value=false"
    assert argv[argv.index("--profile") + 1] == PROFILE
    assert argv[argv.index("--region") + 1] == REGION
    assert "--no-cli-pager" in argv


LARGE_STATE_LINES = 200_000


def test_an_alb_first_in_a_large_state_list_is_disabled(tmp_path: Path) -> None:
    """An ALB on line 1 of a state list far larger than a pipe buffer is still found."""
    state_file = tmp_path / "large-state.txt"
    filler = (f"module.filler.null_resource.r{i}" for i in range(LARGE_STATE_LINES - 1))
    state_file.write_text("\n".join([ALB_ADDRESS, *filler]) + "\n", encoding="utf-8")
    run = _teardown(tmp_path, state_list_file=state_file)
    assert run.returncode == 0, run.stderr
    assert run.index("aws elbv2 modify-load-balancer-attributes") < run.index("terraform destroy")
    assert f"{ALB_ADDRESS} is not in state" not in run.stdout


def test_an_alb_absent_from_state_is_a_no_op(tmp_path: Path) -> None:
    run = _teardown(tmp_path, state_list="module.api.aws_api_gateway_rest_api.this")
    assert run.returncode == 0, run.stderr
    assert not run.has("aws elbv2")
    assert not run.has("terraform output")
    assert run.has("terraform destroy")
    assert f"{ALB_ADDRESS} is not in state" in run.stdout


def test_a_similar_address_does_not_count_as_the_alb(tmp_path: Path) -> None:
    run = _teardown(tmp_path, state_list=f"{ALB_ADDRESS}_other\nmodule.x.{ALB_ADDRESS}")
    assert run.returncode == 0, run.stderr
    assert not run.has("aws elbv2")


def test_a_failed_modify_aborts_before_destroy(tmp_path: Path) -> None:
    run = _teardown(tmp_path, modify_fails=True)
    assert run.returncode != 0
    assert run.has("aws elbv2 modify-load-balancer-attributes")
    assert not run.has("terraform destroy")
    assert "Nothing has been destroyed" in run.stderr


def test_a_failed_output_read_aborts_before_destroy(tmp_path: Path) -> None:
    run = _teardown(tmp_path, output_fails=True)
    assert run.returncode != 0
    assert not run.has("aws elbv2")
    assert not run.has("terraform destroy")


@pytest.mark.parametrize(
    "arn",
    [
        ALB_ARN.replace(REGION, "us-east-1"),
        ALB_ARN.replace(ACCOUNT, "123456789012"),
        ALB_ARN.replace("loadbalancer/app/", "loadbalancer/net/"),
        "",
    ],
)
def test_an_arn_outside_this_stacks_region_or_account_aborts(tmp_path: Path, arn: str) -> None:
    run = _teardown(tmp_path, alb_arn=arn)
    assert run.returncode != 0
    assert not run.has("aws elbv2")
    assert not run.has("terraform destroy")


_GUARDED_STATE = f"{_STATE_WITH_ALB}\nmodule.vpc.aws_vpc.this"


def test_an_adopted_vpc_refusal_leaves_the_alb_protected(tmp_path: Path) -> None:
    run = _teardown(tmp_path, state_list=_GUARDED_STATE)
    assert run.returncode != 0
    assert "REFUSING TO DESTROY" in run.stderr
    assert not run.has("aws elbv2")
    assert not run.has("terraform destroy")


def test_a_failed_state_list_leaves_the_alb_protected(tmp_path: Path) -> None:
    run = _teardown(tmp_path, state_fails=True)
    assert run.returncode != 0
    assert not run.has("aws elbv2")
    assert not run.has("terraform destroy")


def _reprotects(run: Run) -> list[str]:
    return [c for c in run.calls if c.startswith("aws elbv2 modify") and "Value=true" in c]


@pytest.mark.parametrize("destroy_rc", [1, 2])
def test_a_failed_or_declined_destroy_re_protects_the_same_alb(
    tmp_path: Path, destroy_rc: int
) -> None:
    run = _teardown(tmp_path, destroy_rc=destroy_rc, alb_after="exists")
    assert run.returncode != 0
    reprotect = _reprotects(run)
    assert len(reprotect) == 1
    argv = reprotect[0].split()
    assert argv[argv.index("--load-balancer-arn") + 1] == ALB_ARN
    assert argv[argv.index("--profile") + 1] == PROFILE
    assert argv[argv.index("--region") + 1] == REGION
    assert run.calls.index(reprotect[0]) > run.index("terraform destroy")


def test_a_zero_exit_destroy_that_left_the_alb_re_protects_and_fails(tmp_path: Path) -> None:
    run = _teardown(tmp_path, alb_after="exists")
    assert run.returncode != 0
    assert len(_reprotects(run)) == 1


def test_an_unconfirmable_alb_after_destroy_is_re_protected(tmp_path: Path) -> None:
    run = _teardown(tmp_path, alb_after="error")
    assert run.returncode != 0
    assert len(_reprotects(run)) == 1


def test_a_failed_destroy_that_removed_the_alb_does_not_re_protect(tmp_path: Path) -> None:
    run = _teardown(tmp_path, destroy_rc=1, alb_after="gone")
    assert run.returncode != 0
    assert _reprotects(run) == []


def test_a_failed_re_protect_is_reported_and_still_fails(tmp_path: Path) -> None:
    run = _teardown(tmp_path, destroy_rc=1, alb_after="exists", reprotect_fails=True)
    assert run.returncode != 0
    assert "it is unprotected" in run.stderr


def test_no_alb_in_state_skips_the_post_destroy_check(tmp_path: Path) -> None:
    run = _teardown(tmp_path, state_list="module.api.aws_api_gateway_rest_api.this", destroy_rc=1)
    assert run.returncode != 0
    assert not run.has("aws elbv2")
