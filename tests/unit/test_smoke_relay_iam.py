# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.relay_iam`: the relay IAM matrix and its positive controls.

Uses fake `iam`/`sts`/`lambda`/`bedrock-agentcore-control` clients and injected outputs. The tests
assert that a deny row cannot pass on an `(action, resource)` pair no positive control proved
expressible.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest
import scripts.smoke.relay_iam as module
from botocore.exceptions import ClientError
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.iam_roles import _function_grants, _load_matrix, _role_name
from scripts.smoke.relay_iam import (
    ACTION_BEDROCK_INVOKE_MODEL,
    ACTION_DYNAMODB_GET_ITEM,
    ACTION_DYNAMODB_PUT_ITEM,
    ACTION_INVOKE_RUNTIME,
    ACTION_INVOKE_RUNTIME_COMMAND,
    ACTION_INVOKE_RUNTIME_WS_STREAM,
    ACTION_INVOKE_RUNTIME_WS_STREAM_FOR_USER,
    ACTION_KMS_DECRYPT,
    ACTION_LAMBDA_INVOKE,
    ACTION_NEVER_SHIPPED,
    DEFER_REASON_NO_EVALUATION,
    DENIED,
    EXPLICIT_DENY_ONLY,
    checks,
    expected_check_count,
    name_from_arn,
    other_runtime_arn,
)

from crm.store.schema import TENANT_SCOPED_TABLES
from crm.toolspec.definitions import ALL_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[2]

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:runtime/crm-agent-eval-abc123"
_RELAY_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-agent-relay-role"
_CHAT_TARGET_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-chat-target-role"
_RUNTIME_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-runtime-role"
_RELAY_FUNCTION = "crm-agent-eval-agent-relay"
_LOG_GROUP = "/aws/lambda/crm-agent-eval-agent-relay"
_PROFILE_ARN = (
    f"arn:aws:bedrock:{_REGION}:{_ACCOUNT}:inference-profile/global.anthropic.claude-opus-5-5"
)
_ENV_CMK = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/aaaabbbb-cccc-dddd-eeee-ffff11112222"

#: The endpoint ARN as AWS reports it (segment `runtime-endpoint`), served by the fake.
_ENDPOINT_ARN = f"{_RUNTIME_ARN}/runtime-endpoint/DEFAULT"

#: The deployed `NotAction` Deny as `(permitted action, in-scope resources)`, applied by `_FakeIam`.
_NOT_ACTION_DENY: tuple[str, frozenset[str]] = (
    ACTION_INVOKE_RUNTIME,
    frozenset({_RUNTIME_ARN, _ENDPOINT_ARN}),
)

#: The ARN prefix covered by the deployed `DenyEveryHarness` statement; `_FakeIam` applies it.
_HARNESS_DENY_PREFIX: str = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:harness/"

_OUTPUTS: dict[str, Any] = {
    "agent_relay_role_arn": _RELAY_ROLE_ARN,
    "agentcore_runtime_arn": _RUNTIME_ARN,
    "agent_relay_function_name": _RELAY_FUNCTION,
    "agent_relay_log_group_name": _LOG_GROUP,
    "agentcore_runtime_role_arn": _RUNTIME_ROLE_ARN,
    # Every tenant-scoped table, derived from the schema.
    "dynamodb_table_names": {
        logical: f"crm-agent-eval-{logical}" for logical in sorted(TENANT_SCOPED_TABLES)
    },
}


class _PlantedInterruptionError(Exception):
    """Raised only by a consumer in this file, never by any module under test."""


class _FakeSts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": _ACCOUNT}


class _FakeAgentCoreControl:
    """Fake `list_agent_runtime_endpoints`, the only source of the endpoint ARN the module uses."""

    def __init__(self, *, arn: str = _ENDPOINT_ARN, qualifier: str = "DEFAULT") -> None:
        self._arn = arn
        self._qualifier = qualifier
        self.runtime_ids: list[str] = []

    def list_agent_runtime_endpoints(self, *, agentRuntimeId: str) -> dict[str, Any]:  # noqa: N803
        self.runtime_ids.append(agentRuntimeId)
        return {
            "runtimeEndpoints": [
                {
                    "name": self._qualifier,
                    "id": self._qualifier,
                    "agentRuntimeEndpointArn": self._arn,
                }
            ]
        }


class _FakeLambda:
    """`get_function_configuration` for the relay, and `list_functions` for the action Lambdas."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        cmk: str | None = _ENV_CMK,
        url_auth_type: str | None = "AWS_IAM",
    ) -> None:
        #: The Function URL `AuthType`; `None` makes `get_function_url_config` raise (no URL).
        self._url_auth_type = url_auth_type
        self._env = (
            env
            if env is not None
            else {
                "AGENT_RUNTIME_QUALIFIER": "DEFAULT",
                "COGNITO_AUDIENCE_PARAMETER": "/crm-agent-eval/relay/cognito-audience",
            }
        )
        self._cmk = cmk

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        configuration: dict[str, Any] = {
            "FunctionName": FunctionName,
            "Environment": {"Variables": dict(self._env)},
        }
        if self._cmk:
            configuration["KMSKeyArn"] = self._cmk
        return configuration

    def get_function_url_config(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        if self._url_auth_type is None:
            raise RuntimeError(f"ResourceNotFoundException: no url config for {FunctionName}")
        return {"FunctionName": FunctionName, "AuthType": self._url_auth_type}

    def list_functions(self, **kwargs: Any) -> dict[str, Any]:
        # One page: every function in the grant matrix, plus one outside the name prefix.
        from scripts.smoke.iam_roles import _NAME_PREFIX, _function_grants, _load_matrix

        functions = [
            {
                "FunctionName": f"{_NAME_PREFIX}-{key}",
                "FunctionArn": f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:{_NAME_PREFIX}-{key}",
            }
            for key in sorted(_function_grants(_load_matrix()))
        ]
        functions.append(
            {
                "FunctionName": "someone-elses-function",
                "FunctionArn": f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:someone-elses-function",
            }
        )
        return {"Functions": functions}


#: The Runtime role's inline policy statements that the D6/D9 rows read ARNs from.
_AGENT_LESS: tuple[str, ...] = tuple(sorted(t.name for t in ALL_TOOLS if not t.agents))


def _tool_arn(name: str) -> str:
    return f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:crm-agent-eval-{name}"


_RUNTIME_POLICY: dict[str, Any] = {
    "Statement": [
        {
            "Sid": "InvokeActionLambdas",
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": [
                f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:crm-agent-eval-{name}"
                for name in sorted(t.name for t in ALL_TOOLS if t.agents)
            ],
        },
        {
            "Sid": "BedrockInference",
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel"],
            "Resource": [_PROFILE_ARN],
        },
    ]
}


class _FakeIam:
    """Simulates IAM decisions from an explicit `{(role, action, resource): decision}` table.

    Anything not in the table, the wildcard map or the modelled Deny rules is `implicitDeny`.
    """

    def __init__(
        self,
        decisions: dict[tuple[str, str, str], str] | None = None,
        *,
        wildcard: dict[tuple[str, str], str] | None = None,
        policies: dict[str, dict[str, Any]] | None = None,
        get_role_fails: bool = False,
        not_action_deny: tuple[str, frozenset[str]] | None = _NOT_ACTION_DENY,
        not_action_deny_roles: frozenset[str] = frozenset({_RELAY_ROLE_ARN, _CHAT_TARGET_ROLE_ARN}),
        harness_deny: str | None = _HARNESS_DENY_PREFIX,
        missing_roles: frozenset[str] = frozenset(),
        custom_decision: str | None = "allowed",
    ) -> None:
        self.decisions = decisions or {}
        #: The `SimulateCustomPolicy` decision for a synthetic Allow; `None` raises instead.
        self.custom_decision = custom_decision
        self.custom: list[tuple[dict[str, Any], str, str]] = []
        self.missing_roles = missing_roles
        self.wildcard = wildcard or {}
        self.not_action_deny = not_action_deny
        self.not_action_deny_roles = not_action_deny_roles
        self.harness_deny = harness_deny
        self.policies = (
            policies
            if policies is not None
            else {name_from_arn(_RUNTIME_ROLE_ARN): _RUNTIME_POLICY}
        )
        self.get_role_fails = get_role_fails
        self.simulated: list[tuple[str, str, str]] = []

    def simulate_principal_policy(
        self,
        *,
        PolicySourceArn: str,  # noqa: N803 - boto3's own parameter names
        ActionNames: list[str],  # noqa: N803
        ResourceArns: list[str],  # noqa: N803
    ) -> dict[str, Any]:
        key = (PolicySourceArn, ActionNames[0], ResourceArns[0])
        self.simulated.append(key)
        if PolicySourceArn in self.missing_roles:
            raise ClientError(
                {"Error": {"Code": "NoSuchEntity", "Message": f"{PolicySourceArn} not found"}},
                "SimulatePrincipalPolicy",
            )
        decision = (
            self.decisions.get(key)
            or self.wildcard.get((PolicySourceArn, ActionNames[0]))
            or self._not_action_deny(PolicySourceArn, ActionNames[0], ResourceArns[0])
            or self._harness_deny(PolicySourceArn, ActionNames[0], ResourceArns[0])
            or "implicitDeny"
        )
        return {"EvaluationResults": [{"EvalActionName": ActionNames[0], "EvalDecision": decision}]}

    def simulate_custom_policy(
        self,
        *,
        PolicyInputList: list[str],  # noqa: N803 - boto3's own parameter names
        ActionNames: list[str],  # noqa: N803
        ResourceArns: list[str],  # noqa: N803
    ) -> dict[str, Any]:
        (document,) = PolicyInputList
        self.custom.append((json.loads(document), ActionNames[0], ResourceArns[0]))
        if self.custom_decision is None:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
                "SimulateCustomPolicy",
            )
        return {
            "EvaluationResults": [
                {"EvalActionName": ActionNames[0], "EvalDecision": self.custom_decision}
            ]
        }

    def _not_action_deny(self, role_arn: str, action: str, resource: str) -> str | None:
        """Apply the `NotAction` Deny: in scope, each Deny-carrying role is denied all but one action.

        Action names that do not exist are denied too.
        """
        if not self.not_action_deny:
            return None
        permitted, resources = self.not_action_deny
        if (
            role_arn not in self.not_action_deny_roles
            or resource not in resources
            or action == permitted
        ):
            return None
        return "explicitDeny"

    def _harness_deny(self, role_arn: str, action: str, resource: str) -> str | None:
        """Apply `DenyEveryHarness`: relay `bedrock-agentcore:*` calls on `harness/*` are denied."""
        if not self.harness_deny:
            return None
        if role_arn != _RELAY_ROLE_ARN or not resource.startswith(self.harness_deny):
            return None
        if not action.startswith("bedrock-agentcore:"):
            return None
        return "explicitDeny"

    def list_role_policies(self, *, RoleName: str) -> dict[str, Any]:  # noqa: N803
        return {"PolicyNames": ["inline"] if RoleName in self.policies else []}

    def get_role_policy(self, *, RoleName: str, PolicyName: str) -> dict[str, Any]:  # noqa: N803
        return {"PolicyDocument": json.dumps(self.policies[RoleName])}

    def get_role(self, *, RoleName: str) -> dict[str, Any]:  # noqa: N803
        if self.get_role_fails:
            raise RuntimeError(f"NoSuchEntity: {RoleName}")
        return {"Role": {"Arn": f"arn:aws:iam::{_ACCOUNT}:role/{RoleName}"}}


def _compliant_decisions() -> dict[tuple[str, str, str], str]:
    """Return the relay-role allow decisions of a correct deployment."""
    endpoint = _ENDPOINT_ARN
    log_group_arn = f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{_LOG_GROUP}"
    audience = f"arn:aws:ssm:{_REGION}:{_ACCOUNT}:parameter/crm-agent-eval/relay/cognito-audience"
    decisions: dict[tuple[str, str, str], str] = {
        (_RELAY_ROLE_ARN, ACTION_INVOKE_RUNTIME, _RUNTIME_ARN): "allowed",
        (_RELAY_ROLE_ARN, ACTION_INVOKE_RUNTIME, endpoint): "allowed",
        # Both log actions are granted on the log-stream form only, as the relay role's policy does.
        (_RELAY_ROLE_ARN, "logs:CreateLogStream", f"{log_group_arn}:*"): "allowed",
        (_RELAY_ROLE_ARN, "logs:PutLogEvents", f"{log_group_arn}:*"): "allowed",
        (_RELAY_ROLE_ARN, "ssm:GetParameter", audience): "allowed",
    }
    # The Runtime role holds no invoke on a tool no agent uses.
    for name in _AGENT_LESS:
        decisions[(_RUNTIME_ROLE_ARN, ACTION_LAMBDA_INVOKE, _tool_arn(name))] = "implicitDeny"
    for role_arn in _invoker_role_arns():
        decisions[(role_arn, ACTION_INVOKE_RUNTIME, _RUNTIME_ARN)] = "allowed"
        decisions[(role_arn, ACTION_INVOKE_RUNTIME, endpoint)] = "allowed"
    # Denies come from the rules `_FakeIam` applies (see `_NOT_ACTION_DENY`).
    return decisions


def _invoker_role_arns() -> list[str]:
    """The non-relay Runtime invoker roles, as the deployment names them."""
    return [
        f"arn:aws:iam::{_ACCOUNT}:role/{_role_name(function)}"
        for function in sorted(module._runtime_invoker_functions() - {"agent-relay"})
    ]


def _compliant_wildcard() -> dict[tuple[str, str], str]:
    return {
        (_RUNTIME_ROLE_ARN, ACTION_LAMBDA_INVOKE): "allowed",
        (_RUNTIME_ROLE_ARN, ACTION_BEDROCK_INVOKE_MODEL): "allowed",
    }


def _run(
    *,
    decisions: dict[tuple[str, str, str], str] | None = None,
    wildcard: dict[tuple[str, str], str] | None = None,
    outputs: dict[str, Any] | None = None,
    lambda_client: _FakeLambda | None = None,
    iam: _FakeIam | None = None,
    agentcore: _FakeAgentCoreControl | None = None,
) -> tuple[list[CheckResult], _FakeIam]:
    iam_client = iam or _FakeIam(
        decisions if decisions is not None else _compliant_decisions(),
        wildcard=wildcard if wildcard is not None else _compliant_wildcard(),
    )
    ctx = context_for_test(
        outputs=_OUTPUTS if outputs is None else outputs,
        clients={
            "iam": iam_client,
            "sts": _FakeSts(),
            "lambda": lambda_client or _FakeLambda(),
            "bedrock-agentcore-control": agentcore or _FakeAgentCoreControl(),
        },
        region=_REGION,
    )
    return list(checks(ctx)), iam_client


def _by_check(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {result.check: result for result in results}


class TestOtherRuntimeArn:
    def test_only_the_final_path_segment_differs(self) -> None:
        """The synthetic other-runtime ARN differs from the real one only in its final segment."""
        fake = other_runtime_arn(_RUNTIME_ARN)
        assert fake != _RUNTIME_ARN
        assert fake.rsplit("/", 1)[0] == _RUNTIME_ARN.rsplit("/", 1)[0]
        assert fake.split(":")[:5] == _RUNTIME_ARN.split(":")[:5]

    def test_an_arn_without_a_path_still_differs(self) -> None:
        assert other_runtime_arn("arn:aws:x:y:z:runtime") != "arn:aws:x:y:z:runtime"


class TestNameFromArn:
    def test_a_pathless_arn_yields_nothing_rather_than_the_whole_arn(self) -> None:
        """A pathless ARN yields an empty name, and a role ARN yields its role name."""
        assert name_from_arn("arn:aws:iam::123456789012:role") == ""
        assert name_from_arn(_RUNTIME_ROLE_ARN) == "crm-agent-eval-runtime-role"


class TestCompliantDeployment:
    def test_the_allow_rows_pass(self) -> None:
        results = _by_check(_run()[0])
        for row in ("A1_allowed", "A2_allowed", "A3_allowed", "A4_allowed", "A5_allowed"):
            assert results[row].status == "pass", results[row].detail

    def test_the_command_execution_rows_expect_an_explicit_deny(self) -> None:
        results = _by_check(_run()[0])
        for row in ("D1_relay_denied", "D2_relay_denied", "D3_relay_denied", "D4_relay_denied"):
            assert results[row].status == "pass", results[row].detail

    def test_an_implicit_deny_on_a_command_action_is_a_failure(self) -> None:
        """A command-execution row requires `explicitDeny`; `implicitDeny` fails it."""
        decisions = _compliant_decisions()
        decisions[(_RELAY_ROLE_ARN, ACTION_INVOKE_RUNTIME_COMMAND, _RUNTIME_ARN)] = "implicitDeny"
        results = _by_check(_run(decisions=decisions)[0])
        assert results["D1_relay_denied"].status == "fail"
        assert frozenset({"explicitDeny"}) == EXPLICIT_DENY_ONLY

    def test_a_no_result_defers_rather_than_sharing_fail_with_an_over_grant(self) -> None:
        """A missing evaluation result defers the deny row, with no severity in its detail."""
        decisions = _compliant_decisions()
        decisions[(_RELAY_ROLE_ARN, ACTION_INVOKE_RUNTIME_COMMAND, _RUNTIME_ARN)] = "no_result"
        result = _by_check(_run(decisions=decisions)[0])["D1_relay_denied"]
        assert result.status == "defer"
        assert result.defer_reason == DEFER_REASON_NO_EVALUATION
        assert "no_result" not in DENIED, "a no_result must never satisfy a deny row"
        assert "severity if allowed" not in result.detail, (
            "a severity qualifies a decision; there is no decision here for it to qualify"
        )

    def test_a_deferred_row_is_still_not_a_pass(self) -> None:
        """A deferred row is not a pass, and the other command rows still pass."""
        decisions = _compliant_decisions()
        decisions[(_RELAY_ROLE_ARN, ACTION_INVOKE_RUNTIME_COMMAND, _RUNTIME_ARN)] = "no_result"
        statuses = {r.check: r.status for r in _run(decisions=decisions)[0]}
        assert statuses["D1_relay_denied"] != "pass"
        for row in ("D2_relay_denied", "D3_relay_denied", "D4_relay_denied"):
            assert statuses[row] == "pass", row


class TestThePositiveControlMechanism:
    """A deny row must not pass on a pair no positive control proved expressible."""

    def test_d6_passes_when_the_runtime_role_holds_the_grant(self) -> None:
        results = _by_check(_run()[0])
        d6 = [key for key in results if key.startswith("D6[") and key.endswith("_positive_control")]
        assert d6, "no D6 positive control ran"
        assert all(results[key].status == "pass" for key in d6)

    def test_d6_fails_both_rows_when_the_positive_control_is_not_allowed(self) -> None:
        """A failed positive control fails both the control row and its deny row."""
        wildcard = dict(_compliant_wildcard())
        wildcard.pop((_RUNTIME_ROLE_ARN, ACTION_LAMBDA_INVOKE))
        results = _by_check(_run(wildcard=wildcard)[0])
        agent_rows = [k for k in results if not any(f"[{name}]" in k for name in _AGENT_LESS)]
        positive = [
            k for k in agent_rows if k.startswith("D6[") and k.endswith("_positive_control")
        ]
        deny = [k for k in agent_rows if k.startswith("D6[") and k.endswith("_relay_denied")]
        assert positive and deny
        assert all(results[k].status == "fail" for k in positive)
        assert all(results[k].status == "fail" for k in deny), (
            "the deny row reported pass on a pair nothing proved was expressible"
        )
        assert any("FAILED REGARDLESS" in results[k].detail for k in deny)

    def test_d9_fails_both_rows_when_the_runtime_role_cannot_invoke_the_model(self) -> None:
        wildcard = dict(_compliant_wildcard())
        wildcard.pop((_RUNTIME_ROLE_ARN, ACTION_BEDROCK_INVOKE_MODEL))
        results = _by_check(_run(wildcard=wildcard)[0])
        assert results["D9_positive_control"].status == "fail"
        assert results["D9_relay_denied"].status == "fail"

    def test_d9_denies_the_arn_the_granting_policy_itself_names(self) -> None:
        """D9 denies against the profile ARN read from the `BedrockInference` statement."""
        results = _by_check(_run()[0])
        assert results["D9_relay_denied"].resource == _PROFILE_ARN

    def test_d9_fails_when_the_runtime_policy_names_no_profile_arn(self) -> None:
        iam = _FakeIam(
            _compliant_decisions(),
            wildcard=_compliant_wildcard(),
            policies={
                name_from_arn(_RUNTIME_ROLE_ARN): {
                    "Statement": [
                        {"Sid": "InvokeActionLambdas", "Resource": ["arn:aws:lambda:::function:x"]}
                    ]
                }
            },
        )
        results = _by_check(_run(iam=iam)[0])
        assert results["D9_positive_control"].status == "fail"
        assert "no inference-profile ARN" in results["D9_positive_control"].detail
        assert "D9_relay_denied" not in results, "a deny row ran with no ARN to deny against"

    def test_a_matrix_function_with_no_deployed_lambda_is_a_failure(self) -> None:
        """A matrix function with no deployed Lambda fails its D6 positive control."""

        class _EmptyLambda(_FakeLambda):
            def list_functions(self, **kwargs: Any) -> dict[str, Any]:
                return {"Functions": []}

        results = _run(lambda_client=_EmptyLambda())[0]
        missing = [r for r in results if r.check == "D6_positive_control" and r.status == "fail"]
        assert missing, "no failure was reported for a matrix function with no deployed Lambda"
        assert "reads as a pass" in missing[0].detail

    def test_d12_asserts_the_target_role_resolves_since_no_positive_control_exists(self) -> None:
        results = _by_check(_run()[0])
        assert results["D12_target_role_resolves"].status == "pass"
        assert results["D12_relay_denied"].status == "pass"

    def test_d12_fails_when_the_target_role_does_not_resolve(self) -> None:
        iam = _FakeIam(_compliant_decisions(), wildcard=_compliant_wildcard(), get_role_fails=True)
        results = _by_check(_run(iam=iam)[0])
        assert results["D12_target_role_resolves"].status == "fail"
        assert "would read as a pass" in results["D12_target_role_resolves"].detail
        assert "D12_relay_denied" not in results


class TestOneSpellingPerAction:
    def test_the_inverse_matrix_uses_the_same_action_constant_as_the_allow_rows(self) -> None:
        """Every simulated InvokeAgentRuntime spelling is `ACTION_INVOKE_RUNTIME`."""
        _results, iam = _run()
        actions = {action for _role, action, _resource in iam.simulated}
        invoke_spellings = {a for a in actions if a.endswith("InvokeAgentRuntime")}
        assert invoke_spellings == {ACTION_INVOKE_RUNTIME}

    def test_the_relays_allow_rows_run_in_the_same_invocation_as_the_inverse_matrix(self) -> None:
        results = _by_check(_run()[0])
        assert results["A1_allowed"].status == "pass"
        inverse = [k for k in results if "_denied_invoking_runtime" in k]
        assert inverse, "the inverse matrix did not run"

    def test_every_non_relay_role_is_covered_by_the_inverse_matrix(self) -> None:
        from scripts.smoke.iam_roles import _function_grants, _load_matrix

        results = _by_check(_run()[0])
        covered = {k for k in results if k.startswith("D15[")}
        inverse = set(_function_grants(_load_matrix())) - module._runtime_invoker_functions()
        assert inverse
        assert len(covered) >= len(inverse)

    def test_the_sweep_roles_sit_on_their_designed_sides(self) -> None:
        """sweep-task invokes the Runtime (I1/I2); sweep-control must be refused it (D15/D16)."""
        assert "sweep-task" in module._runtime_invoker_functions()
        assert "sweep-control" not in module._runtime_invoker_functions()
        refused = module._non_relay_role_arns(_ACCOUNT)
        assert "crm-agent-eval-sweep-control-role" in refused
        assert "crm-agent-eval-sweep-task-role" not in refused
        results = _by_check(_run()[0])
        assert (
            results["D15[crm-agent-eval-sweep-control-role]_denied_invoking_runtime"].status
            == "pass"
        )
        assert any(k.startswith("I1[crm-agent-eval-sweep-task-role]") for k in results)

    def test_an_allowed_inverse_row_is_a_failure(self) -> None:
        decisions = _compliant_decisions()
        rogue = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-get_account-role"
        decisions[(rogue, ACTION_INVOKE_RUNTIME, _RUNTIME_ARN)] = "allowed"
        results = _by_check(_run(decisions=decisions)[0])
        row = results["D15[crm-agent-eval-get_account-role]_denied_invoking_runtime"]
        assert row.status == "fail"
        assert "a property of who can invoke the Runtime" in row.detail


#: `module "<name>" { ... source = "./modules/lambda_action" ... function_name = "<name>" }`, where
#: the name is a literal or `"${local.name_prefix}-<name>"`.
_MODULE_HEADER_RE = re.compile(r'^module\s+"(?P<module>[\w-]+)"\s*\{\s*$')
_LAMBDA_ACTION_SOURCE_RE = re.compile(r'^\s*source\s*=\s*"\./modules/lambda_action"\s*$')
_LITERAL_FUNCTION_NAME_RE = re.compile(r'^\s*function_name\s*=\s*"(?P<name>[\w-]+)"\s*$')


def _iac_rendered_action_role_names() -> set[str]:
    """Every execution-role name the root `infra/*.tf` files render for a `lambda_action` instance.

    Read from the IaC side only: each `function_name` in a `lambda_action` module block, plus the
    `for_each` tool keys, rendered through the module's own `"${var.function_name}-role"`. Modules
    listed in `local.runtime_invoker_role_arns` (admitted to the Runtime) and
    `iam_roles.PENDING_FUNCTIONS` (no matrix row) are left out.
    """
    from scripts.smoke.iam_roles import PENDING_FUNCTIONS, _terraform_tool_grants

    module_tf = (REPO_ROOT / "infra" / "modules" / "lambda_action" / "main.tf").read_text()
    assert '"${var.function_name}-role"' in module_tf, (
        "the lambda_action module no longer names its role `<function_name>-role`; re-derive this"
    )
    root_text = "\n".join(
        path.read_text() for path in sorted((REPO_ROOT / "infra").glob("*.tf"))
    ).replace("${local.name_prefix}", "crm-agent-eval")
    invokers = re.search(
        r"^\s*runtime_invoker_role_arns\s*=\s*\[([^\]]*)\]", root_text, re.MULTILINE
    )
    assert invokers is not None, "infra/*.tf no longer declares runtime_invoker_role_arns"
    invoker_modules = set(re.findall(r"module\.(\w+)\.role_arn", invokers.group(1)))
    assert invoker_modules, "runtime_invoker_role_arns names no module role"
    literal_names: set[str] = set()
    is_lambda_action = False
    module_name = ""
    pending_literal: str | None = None

    def _close() -> None:
        if (
            is_lambda_action
            and pending_literal is not None
            and module_name not in invoker_modules
            and pending_literal.removeprefix("crm-agent-eval-") not in PENDING_FUNCTIONS
        ):
            literal_names.add(pending_literal)

    for line in root_text.splitlines():
        header = _MODULE_HEADER_RE.match(line)
        if header is not None:
            _close()
            module_name, is_lambda_action, pending_literal = header.group("module"), False, None
            continue
        if not module_name:
            continue
        if _LAMBDA_ACTION_SOURCE_RE.match(line):
            is_lambda_action = True
        literal = _LITERAL_FUNCTION_NAME_RE.match(line)
        if literal is not None and pending_literal is None:
            pending_literal = literal.group("name")
    _close()
    tool_names = {f"crm-agent-eval-{key}" for key in _terraform_tool_grants()}
    return {f"{name}-role" for name in literal_names | tool_names}


class TestTheInverseMatrixEnumeratesOnlyRealRoles:
    """D15/D16 name exactly the roles the IaC renders, and one bad role drops nothing else."""

    def test_the_enumeration_equals_the_role_names_the_iac_renders(self) -> None:
        enumerated = set(module._non_relay_role_arns(_ACCOUNT))
        rendered = _iac_rendered_action_role_names()
        assert enumerated == rendered, (
            f"enumeration-only {sorted(enumerated - rendered)}, "
            f"IaC-only {sorted(rendered - enumerated)}"
        )

    def test_the_iac_reader_sees_the_literal_named_functions(self) -> None:
        """Non-vacuity: the IaC side covers the literal-named modules, not only the tool loop."""
        rendered = _iac_rendered_action_role_names()
        for literal in (
            "crm-agent-eval-identity-interceptor-role",
            "crm-agent-eval-pre-token-generation-role",
            "crm-agent-eval-capabilities-role",
            "crm-agent-eval-eval-trigger-role",
        ):
            assert literal in rendered
        assert "crm-agent-eval-user-settings-role" in rendered
        assert "crm-agent-eval-chat-identity-interceptor-role" in rendered
        matrix_functions = set(_function_grants(_load_matrix()))
        admitted = matrix_functions & module._runtime_invoker_functions()
        assert len(rendered) == len(matrix_functions - admitted)

    def test_every_non_relay_invoker_is_asserted_allowed_on_both_resources(self) -> None:
        """I1/I2: the invokers D15/D16 skip are positively simulated, so a dropped grant is red."""
        results = _by_check(_run()[0])
        roles = [arn.rpartition("/")[2] for arn in _invoker_role_arns()]
        assert roles == ["crm-agent-eval-chat-target-role", "crm-agent-eval-sweep-task-role"]
        for role_name in roles:
            for row_id in ("I1", "I2"):
                assert results[f"{row_id}[{role_name}]_allowed"].status == "pass"
        invoker_rows = [k for k in results if k.startswith(("I1[", "I2["))]
        assert len(invoker_rows) == 2 * len(roles)

    def test_a_removed_invoker_grant_fails_its_rows_only(self) -> None:
        decisions = _compliant_decisions()
        dropped = _invoker_role_arns()[0]
        del decisions[(dropped, ACTION_INVOKE_RUNTIME, _RUNTIME_ARN)]
        results = _by_check(_run(decisions=decisions)[0])
        name = dropped.rpartition("/")[2]
        assert results[f"I1[{name}]_allowed"].status == "fail"
        assert results[f"I2[{name}]_allowed"].status == "pass"
        for other in _invoker_role_arns()[1:]:
            assert results[f"I1[{other.rpartition('/')[2]}]_allowed"].status == "pass"

    def test_a_missing_invoker_role_fails_rather_than_raising(self) -> None:
        # sweep-task: no matrix row, so only I1/I2 ask IAM about it (chat-target is a D7/D8 owner).
        missing = f"arn:aws:iam::{_ACCOUNT}:role/{_role_name('sweep-task')}"
        assert missing in _invoker_role_arns()
        iam = _FakeIam(
            _compliant_decisions(),
            wildcard=_compliant_wildcard(),
            missing_roles=frozenset({missing}),
        )
        results = _by_check(_run(iam=iam)[0])
        row = results[f"I1[{missing.rpartition('/')[2]}]_allowed"]
        assert row.status == "fail"
        assert "NoSuchEntity" in row.detail
        assert results["U1_function_url_auth_type"].status == "pass"

    def test_the_floor_counts_the_invoker_rows(self) -> None:
        ctx = context_for_test(outputs=_OUTPUTS, clients={}, region=_REGION)
        floor = module.expected_check_count(ctx)
        assert f"+ {2 * len(_invoker_role_arns())} I1/I2 row(s)" in floor.basis

    def test_runtime_invokers_are_excluded_from_the_inverse_matrix(self) -> None:
        """Roles the Runtime policy admits are not asserted denied; every other role is."""
        invokers = module._runtime_invoker_functions()
        assert {"agent-relay", "chat-target", "sweep-task"} <= invokers
        enumerated = set(module._non_relay_role_arns(_ACCOUNT))
        assert "crm-agent-eval-chat-target-role" not in enumerated
        assert "crm-agent-eval-sweep-task-role" not in enumerated
        assert "crm-agent-eval-eval-trigger-role" in enumerated

    def test_every_enumerated_role_gets_both_rows_and_every_row_passes(self) -> None:
        results = _by_check(_run()[0])
        for role_name in module._non_relay_role_arns(_ACCOUNT):
            for row_id in ("D15", "D16"):
                assert results[f"{row_id}[{role_name}]_denied_invoking_runtime"].status == "pass"
        inverse_rows = [k for k in results if k.endswith("_denied_invoking_runtime")]
        assert len(inverse_rows) == 2 * len(_iac_rendered_action_role_names())
        assert results["U1_function_url_auth_type"].status == "pass"

    def test_a_missing_role_fails_only_its_own_rows(self) -> None:
        roles = module._non_relay_role_arns(_ACCOUNT)
        ordered = list(roles)
        # D7/D8's controls use a custom policy, so only the inverse matrix asks IAM about a role.
        candidates = ordered[:-1]
        missing_name = candidates[len(candidates) // 2]
        iam = _FakeIam(
            _compliant_decisions(),
            wildcard=_compliant_wildcard(),
            missing_roles=frozenset({roles[missing_name]}),
        )
        results = _by_check(_run(iam=iam)[0])
        for row_id in ("D15", "D16"):
            row = results[f"{row_id}[{missing_name}]_denied_invoking_runtime"]
            assert row.status == "fail"
            assert "NoSuchEntity" in row.detail
        later = ordered[ordered.index(missing_name) + 1 :]
        assert later, "the planted role sorts last, so nothing after it is exercised"
        for role_name in ordered:
            if role_name == missing_name:
                continue
            for row_id in ("D15", "D16"):
                assert results[f"{row_id}[{role_name}]_denied_invoking_runtime"].status == "pass"
        assert results["U1_function_url_auth_type"].status == "pass"

    def test_a_non_entity_simulate_error_also_fails_rather_than_passing(self) -> None:
        roles = module._non_relay_role_arns(_ACCOUNT)
        bad_name = next(iter(roles))

        class _BrokenIam(_FakeIam):
            def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
                if (
                    kwargs["PolicySourceArn"] == roles[bad_name]
                    and kwargs["ActionNames"][0] == ACTION_INVOKE_RUNTIME
                ):
                    raise RuntimeError("endpoint unreachable")
                return super().simulate_principal_policy(**kwargs)

        iam = _BrokenIam(_compliant_decisions(), wildcard=_compliant_wildcard())
        results = _by_check(_run(iam=iam)[0])
        row = results[f"D15[{bad_name}]_denied_invoking_runtime"]
        assert row.status == "fail"
        assert "RuntimeError" in row.detail
        assert results["U1_function_url_auth_type"].status == "pass"


class TestD14:
    def test_d14_runs_against_the_deployed_cmk_arn(self) -> None:
        results = _by_check(_run()[0])
        assert results["D14_relay_denied"].status == "pass"
        assert results["D14_relay_denied"].resource == _ENV_CMK

    def test_d14_is_skipped_not_passed_when_no_cmk_is_configured(self) -> None:
        """With no environment CMK configured, D14 is a skip, not a pass."""
        results = _by_check(_run(lambda_client=_FakeLambda(cmk=None))[0])
        assert results["D14_relay_denied"].status == "skip"
        assert "nothing to deny against" in results["D14_relay_denied"].detail

    def test_d14_is_a_failure_if_the_relay_role_can_decrypt_the_env_cmk(self) -> None:
        decisions = _compliant_decisions()
        decisions[(_RELAY_ROLE_ARN, ACTION_KMS_DECRYPT, _ENV_CMK)] = "allowed"
        results = _by_check(_run(decisions=decisions)[0])
        assert results["D14_relay_denied"].status == "fail"


class TestMissingOutputs:
    @pytest.mark.parametrize(
        "dropped",
        ["agent_relay_role_arn", "agentcore_runtime_arn", "agent_relay_function_name"],
    )
    def test_a_missing_subject_output_fails_the_whole_class(self, dropped: str) -> None:
        outputs = {k: v for k, v in _OUTPUTS.items() if k != dropped}
        results = _run(outputs=outputs)[0]
        assert [r.status for r in results] == ["fail"]
        assert "not a skipped resource class" in results[0].detail

    def test_a_missing_log_group_output_fails_and_does_not_construct_the_arn(self) -> None:
        outputs = {k: v for k, v in _OUTPUTS.items() if k != "agent_relay_log_group_name"}
        results = _by_check(_run(outputs=outputs)[0])
        assert results["terraform_output_present"].status == "fail"
        assert "A3_allowed" not in results, "A3 ran against a constructed log-group ARN"

    def test_a_missing_runtime_role_output_fails_the_paired_deny_rows(self) -> None:
        """A missing runtime-role output fails, and no D6 deny row runs."""
        outputs = {k: v for k, v in _OUTPUTS.items() if k != "agentcore_runtime_role_arn"}
        results = _by_check(_run(outputs=outputs)[0])
        assert results["terraform_output_present"].status == "fail"
        agent_d6 = [
            k
            for k in results
            if k.endswith("_relay_denied")
            and k.startswith("D6[")
            and not any(f"[{name}]" in k for name in _AGENT_LESS)
        ]
        assert not agent_d6
        assert not [k for k in results if k.startswith("R1[") and k.endswith("_runtime_denied")]

    def test_a_missing_audience_env_var_fails_a5_rather_than_constructing_a_parameter_arn(
        self,
    ) -> None:
        results = _by_check(
            _run(lambda_client=_FakeLambda(env={"AGENT_RUNTIME_QUALIFIER": "DEFAULT"}))[0]
        )
        assert results["A5_allowed"].status == "fail"
        assert "self-certifying" in results["A5_allowed"].detail


class TestCoverageByExclusion:
    """The `NotAction` Deny covers every action but the permitted one, including unnamed actions."""

    def test_the_previously_unnamed_invoke_routes_are_explicitly_denied(self) -> None:
        """D21-D23 (the WebSocket-stream invokes and the harness route) are explicitly denied."""
        results = _by_check(_run()[0])
        for row in ("D21_relay_denied", "D22_relay_denied", "D23_relay_denied"):
            assert row in results, f"{row} was not emitted at all"
            assert results[row].status == "pass", results[row].detail
        # InvokeHarness authorizes against `harness*`, so D23 probes a harness ARN.
        assert ":harness/" in results["D23_relay_denied"].resource, results[
            "D23_relay_denied"
        ].resource
        assert ":runtime/" not in results["D23_relay_denied"].resource

    def test_an_implicit_deny_on_a_previously_unnamed_route_is_a_failure(self) -> None:
        """`implicitDeny` on a D21-D23 route fails the row."""
        decisions = _compliant_decisions()
        decisions[(_RELAY_ROLE_ARN, ACTION_INVOKE_RUNTIME_WS_STREAM, _RUNTIME_ARN)] = "implicitDeny"
        results = _by_check(_run(decisions=decisions)[0])
        assert results["D21_relay_denied"].status == "fail"

    def test_a_fabricated_action_must_also_be_explicitly_denied(self) -> None:
        """A fabricated action is explicitly denied under the deployed `NotAction` Deny."""
        results = _by_check(_run()[0])
        control = results["deny_covers_actions_nobody_enumerated"]
        assert control.status == "pass"
        assert "not shipped yet" in control.detail

    def test_a_reverted_deny_fails_the_exclusion_control_and_nothing_else(self) -> None:
        """With the Deny as an action list, only the exclusion control fails."""
        iam = _FakeIam(_compliant_decisions(), wildcard=_compliant_wildcard(), not_action_deny=None)
        enumerated = {
            (_RELAY_ROLE_ARN, action, resource): "explicitDeny"
            for action, resource in (
                (ACTION_INVOKE_RUNTIME_COMMAND, _RUNTIME_ARN),
                (ACTION_INVOKE_RUNTIME_COMMAND, _ENDPOINT_ARN),
                ("bedrock-agentcore:InvokeAgentRuntimeCommandShell", _RUNTIME_ARN),
                ("bedrock-agentcore:InvokeAgentRuntimeForUser", _RUNTIME_ARN),
                (ACTION_INVOKE_RUNTIME_WS_STREAM, _RUNTIME_ARN),
                (ACTION_INVOKE_RUNTIME_WS_STREAM_FOR_USER, _RUNTIME_ARN),
                # D23 is denied by the separate `harness_deny` rule, which stays on.
            )
        }
        iam.decisions.update(enumerated)
        results = _by_check(_run(iam=iam)[0])
        assert results["deny_covers_actions_nobody_enumerated"].status == "fail"
        assert (
            "reverted to an action list" in results["deny_covers_actions_nobody_enumerated"].detail
        )
        for row in ("D1_relay_denied", "D21_relay_denied", "D23_relay_denied"):
            assert results[row].status == "pass", (
                f"{row} passed under a reverted Deny, which is the point: the named rows cannot "
                "detect the regression"
            )

    def test_the_permitted_action_is_still_allowed_after_the_shape_change(self) -> None:
        """A1/A2 still pass: the permitted action is outside the `NotAction` Deny."""
        results = _by_check(_run()[0])
        assert results["A1_allowed"].status == "pass"
        assert results["A2_allowed"].status == "pass"


class TestD17ExpressibilityIsNotTautological:
    """The D17-D19 expressibility guard follows A5's outcome, not the audience ARN's presence."""

    def test_the_guard_fails_when_its_positive_control_failed(self) -> None:
        """With A5 denied, the D17 expressibility guard fails."""
        decisions = _compliant_decisions()
        audience = (
            f"arn:aws:ssm:{_REGION}:{_ACCOUNT}:parameter/crm-agent-eval/relay/cognito-audience"
        )
        decisions[(_RELAY_ROLE_ARN, "ssm:GetParameter", audience)] = "implicitDeny"
        results = _by_check(_run(decisions=decisions)[0])
        assert results["A5_allowed"].status == "fail"
        assert results["D17_expressibility"].status == "fail"
        assert "did not pass" in results["D17_expressibility"].detail

    def test_the_guard_passes_when_a5_established_the_pair(self) -> None:
        results = _by_check(_run()[0])
        assert results["A5_allowed"].status == "pass"
        assert results["D17_expressibility"].status == "pass"

    def test_an_unresolvable_audience_arn_skips_the_three_rows_rather_than_dropping_them(
        self,
    ) -> None:
        lambda_client = _FakeLambda(
            env={
                "AGENT_RUNTIME_QUALIFIER": "DEFAULT",
            }
        )
        results = _by_check(_run(lambda_client=lambda_client)[0])
        for row in ("D17_relay_denied", "D18_relay_denied", "D19_relay_denied"):
            assert row in results, f"{row} vanished instead of being skipped"
            assert results[row].status == "skip"
            assert "self-certifying" in results[row].detail
        assert results["D17_expressibility"].status == "fail"


class TestThePartialInventoryGuarantee:
    """`checks` is a generator, so results yielded before an exception reach the harness."""

    @pytest.mark.parametrize(
        "module_name",
        ["relay_iam", "runtime_inbound_auth", "integration_teardown_privilege"],
    )
    def test_every_g9_modules_checks_is_a_generator_function(self, module_name: str) -> None:
        module = importlib.import_module(f"scripts.smoke.{module_name}")
        assert inspect.isgeneratorfunction(module.checks), (
            f"{module_name}.checks returns a built list, so the harness's documented partial-"
            "inventory guarantee is void for it"
        )

    def test_results_yielded_before_a_raise_survive_it(self) -> None:
        """Results yielded before a consumer-side exception are kept."""
        iam = _FakeIam(_compliant_decisions(), wildcard=_compliant_wildcard())
        ctx = context_for_test(
            outputs=_OUTPUTS,
            clients={
                "iam": iam,
                "sts": _FakeSts(),
                "lambda": _FakeLambda(),
                # `context_for_test` builds a real boto client for any service it is not given.
                "bedrock-agentcore-control": _FakeAgentCoreControl(),
            },
            region=_REGION,
        )
        emitted: list[CheckResult] = []
        interrupted = False
        try:
            for index, result in enumerate(checks(ctx)):
                emitted.append(result)
                if index == 4:
                    raise _PlantedInterruptionError
        except _PlantedInterruptionError:
            interrupted = True
        assert interrupted, "the generator ended before the plant, so nothing was interrupted"
        assert len(emitted) == 5, "the consumer received nothing before the raise"


class TestTheDerivationsOwnLowerBound:
    """The table-mapping guard asserts `dynamodb_table_names` covers every tenant-scoped table."""

    def test_a_complete_mapping_passes_the_guard(self) -> None:
        results = _by_check(_run()[0])
        guard = results["table_mapping_covers_every_tenant_scoped_table"]
        assert guard.status == "pass", guard.detail
        assert str(len(TENANT_SCOPED_TABLES)) in guard.detail

    @pytest.mark.parametrize("dropped", ["accounts", "proposals", "audit_records"])
    def test_dropping_any_tenant_scoped_table_fails_the_guard(self, dropped: str) -> None:
        """Dropping any tenant-scoped table from the output fails the guard."""
        tables = {k: v for k, v in _OUTPUTS["dynamodb_table_names"].items() if k != dropped}
        results = _by_check(_run(outputs={**_OUTPUTS, "dynamodb_table_names": tables})[0])
        guard = results["table_mapping_covers_every_tenant_scoped_table"]
        assert guard.status == "fail"
        assert dropped in guard.detail
        assert f"{dropped}]_positive_control" not in results, (
            "the D7 row ran against a dropped table"
        )

    def test_the_total_floor_alone_would_not_have_caught_it(self) -> None:
        """Emptying the table mapping loses fewer rows than remain, so a total floor misses it."""
        full = _run()[0]
        emptied = _run(outputs={**_OUTPUTS, "dynamodb_table_names": {}})[0]
        lost = len(full) - len(emptied)
        assert lost >= 2 * len(TENANT_SCOPED_TABLES), "the D7/D8 family is smaller than expected"
        assert lost < len(emptied), (
            "the loss is a small fraction of the inventory, which is precisely why a total floor "
            "cannot see it -- if this ever inverts, a floor would suffice and this can be revisited"
        )

    def test_the_shortfall_is_recorded_in_the_expectation_basis(self) -> None:
        """A short table mapping is recorded as a shortfall in the expectation basis."""
        tables = {"accounts": "crm-agent-eval-accounts"}
        ctx = context_for_test(
            outputs={**_OUTPUTS, "dynamodb_table_names": tables},
            clients={"iam": _FakeIam(), "sts": _FakeSts(), "lambda": _FakeLambda()},
            region=_REGION,
        )
        basis = module.expected_check_count(ctx).basis
        assert "SHORT by" in basis
        assert "cannot detect the missing rows" in basis


class TestTheDataPlaneDenyRows:
    """D7/D8: the relay is denied direct reads and writes on every table in `dynamodb_table_names`.

    An absent or malformed output fails rather than emitting zero rows.
    """

    @staticmethod
    def _emitted(results: list[CheckResult]) -> set[str]:
        return {result.check for result in results if result.check.startswith(("D7[", "D8["))}

    def test_a_row_is_emitted_for_every_table_the_output_names(self) -> None:
        emitted = self._emitted(_run()[0])
        assert emitted, "the D7/D8 class emitted nothing at all"
        for logical_name in _OUTPUTS["dynamodb_table_names"]:
            assert f"D7[{logical_name}]_positive_control" in emitted
            assert f"D8[{logical_name}]_positive_control" in emitted

    def test_every_row_runs_and_passes_against_a_compliant_deployment(self) -> None:
        """No D7/D8 row is skipped, usage_records included, and none is red."""
        results = _by_check(_run()[0])
        assert "usage_records" in _OUTPUTS["dynamodb_table_names"]
        for logical_name in _OUTPUTS["dynamodb_table_names"]:
            for row_id in ("D7", "D8"):
                for suffix in ("_positive_control", "_relay_denied"):
                    row = results[f"{row_id}[{logical_name}]{suffix}"]
                    assert row.status == "pass", f"{row.check}: {row.status} {row.detail}"

    def test_the_control_is_a_synthetic_allow_on_exactly_the_pair(self) -> None:
        """The control names only the row's action and table ARN, and no role's grant."""
        _results, iam = _run()
        data_plane = [c for c in iam.custom if c[1].startswith("dynamodb:")]
        assert len(data_plane) == 2 * len(_OUTPUTS["dynamodb_table_names"])
        for document, action, resource in data_plane:
            assert document["Statement"] == [
                {"Effect": "Allow", "Action": action, "Resource": resource}
            ]
            assert action in (ACTION_DYNAMODB_GET_ITEM, ACTION_DYNAMODB_PUT_ITEM)
            assert ":table/" in resource
        simulated_roles = {
            role for role, action, _r in iam.simulated if action.startswith("dynamodb:")
        }
        assert simulated_roles == {_RELAY_ROLE_ARN}

    @pytest.mark.parametrize("decision", ["implicitDeny", "explicitDeny", None], ids=str)
    def test_a_control_that_is_denied_or_errors_fails_both_rows(self, decision: str | None) -> None:
        iam = _FakeIam(
            _compliant_decisions(), wildcard=_compliant_wildcard(), custom_decision=decision
        )
        results = _by_check(_run(iam=iam)[0])
        for suffix in ("_positive_control", "_relay_denied"):
            row = results[f"D7[usage_records]{suffix}"]
            assert row.status == "fail", f"{row.check}: {row.detail}"
        if decision is None:
            assert "error (ClientError" in results["D7[usage_records]_positive_control"].detail

    @pytest.mark.parametrize("action", [ACTION_DYNAMODB_GET_ITEM, ACTION_DYNAMODB_PUT_ITEM])
    def test_a_relay_grant_on_usage_records_reds_its_deny_row(self, action: str) -> None:
        """The regression: a usage_records relay grant must red a T3 row."""
        table = _OUTPUTS["dynamodb_table_names"]["usage_records"]
        arn = f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/{table}"
        decisions = {**_compliant_decisions(), (_RELAY_ROLE_ARN, action, arn): "allowed"}
        results = _by_check(_run(decisions=decisions)[0])
        row_id = "D7" if action == ACTION_DYNAMODB_GET_ITEM else "D8"
        assert results[f"{row_id}[usage_records]_relay_denied"].status == "fail"
        assert results[f"{row_id}[usage_records]_positive_control"].status == "pass"

    def test_a_missing_table_names_output_fails_rather_than_emitting_zero_rows(self) -> None:
        outputs = {k: v for k, v in _OUTPUTS.items() if k != "dynamodb_table_names"}
        results = _run(outputs=outputs)[0]
        assert not self._emitted(results), "the rows cannot run without the tables they address"
        failures = [r for r in results if r.check == "terraform_output_present"]
        assert [r.status for r in failures] == ["fail"]
        assert "ZERO" in failures[0].detail

    def test_a_non_mapping_table_names_output_is_also_a_failure(self) -> None:
        """A non-mapping `dynamodb_table_names` output fails and emits no D7/D8 rows."""
        results = _run(outputs={**_OUTPUTS, "dynamodb_table_names": "crm-agent-evalaccounts"})[0]
        assert not self._emitted(results)
        assert [r.status for r in results if r.check == "terraform_output_present"] == ["fail"]


class TestEndpointResource:
    def test_the_endpoint_arn_uses_the_deployed_qualifier(self) -> None:
        """The endpoint ARN is looked up with the relay's deployed qualifier, not DEFAULT."""
        lambda_client = _FakeLambda(
            env={
                "AGENT_RUNTIME_QUALIFIER": "CANARY",
                "COGNITO_AUDIENCE_PARAMETER": "/p/a",
            }
        )
        canary_arn = f"{_RUNTIME_ARN}/runtime-endpoint/CANARY"
        agentcore = _FakeAgentCoreControl(arn=canary_arn, qualifier="CANARY")
        _results, iam = _run(lambda_client=lambda_client, agentcore=agentcore)
        # The simulated ARN is the one the endpoint listing returned, not one composed locally.
        assert agentcore.runtime_ids == ["crm-agent-eval-abc123"], agentcore.runtime_ids
        assert any(resource == canary_arn for _r, _a, resource in iam.simulated)


class TestU1TheDeployedFunctionUrlEnforcesIamAuth:
    """U1 passes only when the deployed Function URL's `AuthType` is exactly `AWS_IAM`."""

    def test_iam_auth_passes(self) -> None:
        results = _by_check(_run(lambda_client=_FakeLambda(url_auth_type="AWS_IAM"))[0])
        assert results["U1_function_url_auth_type"].status == "pass"

    def test_auth_type_none_fails_as_critical(self) -> None:
        """`AuthType: NONE` fails as a Critical naming the globally accessible resource."""
        results = _by_check(_run(lambda_client=_FakeLambda(url_auth_type="NONE"))[0])
        row = results["U1_function_url_auth_type"]
        assert row.status == "fail"
        assert "NONE" in row.detail
        assert "Critical" in row.detail
        assert "globally accessible resource" in row.detail, (
            "the finding must name the constraint it violates"
        )

    def test_an_absent_url_config_is_a_failure_and_not_a_skip(self) -> None:
        """An absent Function URL config fails U1 rather than skipping it."""
        row = _by_check(_run(lambda_client=_FakeLambda(url_auth_type=None))[0])[
            "U1_function_url_auth_type"
        ]
        assert row.status == "fail"

    def test_no_other_auth_type_is_accepted(self) -> None:
        """Any `AuthType` other than exactly `AWS_IAM` fails."""
        for auth_type in ("", "none", "aws_iam", "AWS_IAM_UNKNOWN"):
            row = _by_check(_run(lambda_client=_FakeLambda(url_auth_type=auth_type))[0])[
                "U1_function_url_auth_type"
            ]
            assert row.status == "fail", f"AuthType={auth_type!r} was accepted"

    def test_the_row_is_counted_by_the_expected_floor(self) -> None:
        """The expected-count floor names and counts U1."""
        results, _ = _run()
        ctx = context_for_test(outputs=_OUTPUTS, clients={}, region=_REGION)
        assert "U1" in expected_check_count(ctx).basis
        assert len(results) >= expected_check_count(ctx).minimum


class TestA3SimulatesTheResourceTheRoleActuallyGrants:
    """A3/A4 simulate the log-stream ARN form (`<log-group>:*`), which the relay role is granted."""

    @staticmethod
    def _log_arns() -> tuple[str, str]:
        bare = f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{_LOG_GROUP}"
        return bare, f"{bare}:*"

    def test_both_log_rows_simulate_the_log_stream_form(self) -> None:
        bare, stream = self._log_arns()
        _, iam = _run()
        asked = {
            (action, resource)
            for _role, action, resource in iam.simulated
            if action in ("logs:CreateLogStream", "logs:PutLogEvents")
        }
        assert ("logs:CreateLogStream", stream) in asked, (
            f"A3 did not simulate the log-stream form; it asked about {sorted(asked)}"
        )
        assert ("logs:PutLogEvents", stream) in asked
        assert not [pair for pair in asked if pair[1] == bare], (
            "a log row still simulates the bare log-group ARN, which this role does not grant, so it "
            f"reports implicitDeny on a correct deployment: {sorted(asked)}"
        )

    def test_both_log_rows_pass_against_a_deployment_shaped_grant(self) -> None:
        """A3 and A4 pass when only the `:*` form is granted."""
        results = _by_check(_run()[0])
        assert results["A3_allowed"].status == "pass", results["A3_allowed"].detail
        assert results["A4_allowed"].status == "pass", results["A4_allowed"].detail

    def test_a3_fails_when_the_stream_form_is_not_granted(self) -> None:
        """A3 fails when the log-stream form is not granted."""
        bare, stream = self._log_arns()
        decisions = {
            k: v for k, v in _compliant_decisions().items() if k[2] != stream or "logs:" not in k[1]
        }
        assert decisions != _compliant_decisions(), "control is vacuous: nothing was removed"
        assert _by_check(_run(decisions=decisions)[0])["A3_allowed"].status == "fail"


class TestD6EnumeratesActionToolsAndNotEveryDeployedFunction:
    """D6 enumerates the action tools in `ALL_TOOLS`, not every function in the grant matrix."""

    @staticmethod
    def _d6_subjects(results: list[CheckResult]) -> set[str]:
        return {
            check.split("[", 1)[1].split("]", 1)[0]
            for check in _by_check(results)
            if check.startswith("D6[")
        }

    def test_d6_covers_exactly_the_toolspec(self) -> None:
        from crm.toolspec.definitions import ALL_TOOLS

        subjects = self._d6_subjects(_run()[0])
        assert subjects == {tool.name for tool in ALL_TOOLS}

    def test_no_non_tool_function_gets_a_d6_row(self) -> None:
        """None of the matrix's non-tool functions gets a D6 row."""
        subjects = self._d6_subjects(_run()[0])
        for non_tool in (
            "capabilities",
            "eval-trigger",
            "identity-interceptor",
            "pre-token-generation",
        ):
            assert non_tool not in subjects, (
                f"{non_tool!r} is not an action tool, so D6's positive control asserts a grant the "
                "Runtime role must not hold and the row fails on a correct deployment"
            )

    def test_the_agent_only_tool_is_still_covered(self) -> None:
        """`get_page_context`, an action tool with no HTTP route, still gets a D6 row."""
        assert "get_page_context" in self._d6_subjects(_run()[0])

    def test_the_floor_and_the_loop_read_one_source(self) -> None:
        """The expected-count basis counts the same `ALL_TOOLS` the D6 loop iterates."""
        from crm.toolspec.definitions import ALL_TOOLS

        results, _ = _run()
        ctx = context_for_test(outputs=_OUTPUTS, clients={}, region=_REGION)
        basis = expected_check_count(ctx).basis
        assert f"{len(ALL_TOOLS)} action tool(s)" in basis, basis
        assert len(results) >= expected_check_count(ctx).minimum


class TestX1ChatTargetDenyCoversActionsNobodyEnumerated:
    """X1: chat-target's `NotAction` Deny is measured live by a fabricated action, like the relay's."""

    _ROW = "X1_chat_target_deny_covers_actions_nobody_enumerated"

    def _decisions_with_x1(self, decision: str) -> dict[tuple[str, str, str], str]:
        decisions = _compliant_decisions()
        decisions[(_CHAT_TARGET_ROLE_ARN, ACTION_NEVER_SHIPPED, _RUNTIME_ARN)] = decision
        return decisions

    def test_a_compliant_deployment_passes_x1_against_the_chat_target_role(self) -> None:
        results, iam = _run()
        row = _by_check(results)[self._ROW]
        assert row.status == "pass", row.detail
        assert (_CHAT_TARGET_ROLE_ARN, ACTION_NEVER_SHIPPED, _RUNTIME_ARN) in iam.simulated
        assert _CHAT_TARGET_ROLE_ARN in _invoker_role_arns()

    def test_x1_follows_its_positive_control(self) -> None:
        checks_in_order = [result.check for result in _run()[0]]
        positive = "I1[crm-agent-eval-chat-target-role]_allowed"
        assert checks_in_order.index(positive) < checks_in_order.index(self._ROW)

    @pytest.mark.parametrize("decision", ["allowed", "implicitDeny", "no_result"])
    def test_anything_but_an_explicit_deny_is_a_failure(self, decision: str) -> None:
        results = _by_check(_run(decisions=self._decisions_with_x1(decision))[0])
        row = results[self._ROW]
        assert row.status == "fail"
        assert decision in row.detail
        assert results["deny_covers_actions_nobody_enumerated"].status == "pass"

    def test_a_chat_target_role_without_its_deny_fails_x1_only(self) -> None:
        iam = _FakeIam(
            _compliant_decisions(),
            wildcard=_compliant_wildcard(),
            not_action_deny_roles=frozenset({_RELAY_ROLE_ARN}),
        )
        results = _by_check(_run(iam=iam)[0])
        assert results[self._ROW].status == "fail"
        assert "implicitDeny" in results[self._ROW].detail
        assert results["deny_covers_actions_nobody_enumerated"].status == "pass"
        assert results["I1[crm-agent-eval-chat-target-role]_allowed"].status == "pass"

    def test_a_simulate_client_error_fails_the_row_rather_than_raising(self) -> None:
        iam = _FakeIam(
            _compliant_decisions(),
            wildcard=_compliant_wildcard(),
            missing_roles=frozenset({_CHAT_TARGET_ROLE_ARN}),
        )
        results = _by_check(_run(iam=iam)[0])
        row = results[self._ROW]
        assert row.status == "fail"
        assert "ClientError" in row.detail and "NoSuchEntity" in row.detail
        assert results["U1_function_url_auth_type"].status == "pass"

    def test_chat_target_absent_from_the_invokers_fails_the_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        decisions = _compliant_decisions()
        monkeypatch.setattr(
            module, "_runtime_invoker_functions", lambda: frozenset({"agent-relay", "sweep-task"})
        )
        row = _by_check(_run(decisions=decisions)[0])[self._ROW]
        assert row.status == "fail"
        assert "not in local.runtime_invoker_role_arns" in row.detail

    def test_an_unreadable_invoker_list_fails_the_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unreadable() -> frozenset[str]:
            raise RuntimeError("infra/*.tf declares no runtime_invoker_role_arns list")

        monkeypatch.setattr(module, "_runtime_invoker_functions", unreadable)
        ctx = context_for_test(outputs=_OUTPUTS, clients={"iam": _FakeIam()}, region=_REGION)
        row = module._chat_target_exclusion_result(
            ctx,
            invokers=None,
            account_id=_ACCOUNT,
            runtime_arn=_RUNTIME_ARN,
            endpoint_arn=_ENDPOINT_ARN,
            positive_control_status=None,
        )
        assert row.status == "fail"
        assert "could not be read" in row.detail

    def test_the_floor_counts_x1(self) -> None:
        ctx = context_for_test(outputs=_OUTPUTS, clients={}, region=_REGION)
        assert "U1, X1)" in module.expected_check_count(ctx).basis

    def test_x1_probes_both_resources_and_the_command_action(self) -> None:
        """X1 simulates the fabricated action and the command action on the runtime and endpoint."""
        results, iam = _run()
        for action in (ACTION_NEVER_SHIPPED, ACTION_INVOKE_RUNTIME_COMMAND):
            for resource in (_RUNTIME_ARN, _ENDPOINT_ARN):
                assert (_CHAT_TARGET_ROLE_ARN, action, resource) in iam.simulated
        detail = _by_check(results)[self._ROW].detail
        assert "I1[crm-agent-eval-chat-target-role] passed in this run" in detail

    @pytest.mark.parametrize(
        ("action", "resource"),
        [
            (ACTION_NEVER_SHIPPED, _ENDPOINT_ARN),
            (ACTION_INVOKE_RUNTIME_COMMAND, _RUNTIME_ARN),
            (ACTION_INVOKE_RUNTIME_COMMAND, _ENDPOINT_ARN),
        ],
    )
    def test_a_probe_other_than_the_runtime_one_reds_x1(self, action: str, resource: str) -> None:
        decisions = _compliant_decisions()
        decisions[(_CHAT_TARGET_ROLE_ARN, action, resource)] = "implicitDeny"
        row = _by_check(_run(decisions=decisions)[0])[self._ROW]
        assert row.status == "fail"
        assert f"{action} on {resource} evaluated implicitDeny" in row.detail

    def test_x1_fails_when_its_positive_control_did_not_pass(self) -> None:
        """A Deny that also blocks the permitted invoke reds X1, not only I1."""
        decisions = _compliant_decisions()
        del decisions[(_CHAT_TARGET_ROLE_ARN, ACTION_INVOKE_RUNTIME, _RUNTIME_ARN)]
        results = _by_check(_run(decisions=decisions)[0])
        assert results["I1[crm-agent-eval-chat-target-role]_allowed"].status == "fail"
        row = results[self._ROW]
        assert row.status == "fail"
        assert "I1[crm-agent-eval-chat-target-role] reported fail" in row.detail


class TestI0AnUnreadableInvokerListIsAFailingRow:
    """An unreadable invoker list is one failing I0 row; rows not reading it still run."""

    @staticmethod
    def _unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
        def unreadable() -> frozenset[str]:
            raise RuntimeError("infra/*.tf declares no runtime_invoker_role_arns list")

        monkeypatch.setattr(module, "_runtime_invoker_functions", unreadable)

    def test_checks_reports_i0_instead_of_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        decisions = _compliant_decisions()
        self._unreadable(monkeypatch)
        results = _run(decisions=decisions)[0]
        i0 = [r for r in results if r.check == "I0_runtime_invokers_resolved"]
        assert [r.status for r in i0] == ["fail"]
        assert "runtime_invoker_role_arns" in i0[0].detail
        dependent = [
            r.check
            for r in results
            if r.check.startswith(("D15[", "D16[", "I1[", "I2[")) and r.check != i0[0].check
        ]
        assert dependent == []
        by_check = _by_check(results)
        assert by_check["X1_chat_target_deny_covers_actions_nobody_enumerated"].status == "fail"
        for independent in ("A1_allowed", "D5_relay_denied", "D14_relay_denied"):
            assert independent in by_check
        assert by_check["A1_allowed"].status == "pass"
        assert by_check["U1_function_url_auth_type"].status == "pass"

    def test_an_unreadable_matrix_fails_d15_d16_and_keeps_i1_i2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unreadable() -> object:
            raise FileNotFoundError("grant matrix")

        monkeypatch.setattr(module, "_load_matrix", unreadable)
        results = _by_check(_run()[0])
        assert results["D15_D16_roles_enumerated"].status == "fail"
        assert "FileNotFoundError" in results["D15_D16_roles_enumerated"].detail
        assert not [k for k in results if k.endswith("_denied_invoking_runtime")]
        assert results["I1[crm-agent-eval-chat-target-role]_allowed"].status == "pass"
        assert results["X1_chat_target_deny_covers_actions_nobody_enumerated"].status == "pass"

    def test_the_floor_names_the_resolution_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._unreadable(monkeypatch)
        ctx = context_for_test(outputs=_OUTPUTS, clients={}, region=_REGION)
        floor = module.expected_check_count(ctx)
        assert "1 failing I0/D15-D16 resolution row" in floor.basis
        assert "+ 0 D15/D16 row(s)" in floor.basis


class TestTheFloorIsExactForACompliantRun:
    """S7: the floor equals a compliant run's emission, so dropping any row family reds it."""

    def test_a_compliant_run_emits_exactly_the_floor(self) -> None:
        results = _run()[0]
        assert all(r.status in ("pass", "skip") for r in results)
        ctx = context_for_test(outputs=_OUTPUTS, clients={}, region=_REGION)
        assert len(results) == expected_check_count(ctx).minimum

    @pytest.mark.parametrize(
        "family", ["_inverse_matrix_results", "_invoker_allow_results", "_paired_deny_results"]
    )
    def test_dropping_a_row_family_falls_below_the_floor(
        self, monkeypatch: pytest.MonkeyPatch, family: str
    ) -> None:
        monkeypatch.setattr(module, family, lambda *_a, **_k: iter(()))
        results = _run()[0]
        ctx = context_for_test(outputs=_OUTPUTS, clients={}, region=_REGION)
        assert len(results) < expected_check_count(ctx).minimum

    def test_the_d14_skip_path_still_meets_the_floor(self) -> None:
        """An outcome-dependent skip emits the same row count, so it cannot red a correct run."""
        results = _run(lambda_client=_FakeLambda(cmk=None))[0]
        assert _by_check(results)["D14_relay_denied"].status == "skip"
        ctx = context_for_test(outputs=_OUTPUTS, clients={}, region=_REGION)
        assert len(results) == expected_check_count(ctx).minimum


class TestTheRuntimeRoleCannotInvokeAgentLessTools:
    """R1: the Runtime role holds no invoke on a tool no agent uses."""

    def test_the_agent_less_set_names_approve_and_reject(self) -> None:
        assert {"approve_proposal", "reject_proposal"} <= set(_AGENT_LESS)

    def test_a_compliant_run_passes_every_r1_row(self) -> None:
        results = _by_check(_run()[0])
        for name in _AGENT_LESS:
            for suffix in (
                "_positive_control",
                "_runtime_denied",
                "_not_granted_to_runtime_by_policy",
            ):
                row = results[f"R1[{name}]{suffix}"]
                assert row.status == "pass", f"{row.check}: {row.detail}"
            assert results[f"D6[{name}]_relay_denied"].status == "pass"

    def test_an_agent_tool_keeps_the_runtime_role_as_its_positive_control(self) -> None:
        results = _by_check(_run()[0])
        agent_tool = next(t.name for t in ALL_TOOLS if t.agents)
        assert results[f"D6[{agent_tool}]_positive_control"].status == "pass"
        assert f"R1[{agent_tool}]_runtime_denied" not in results

    def test_r1_fails_when_the_runtime_role_can_invoke_an_agent_less_tool(self) -> None:
        decisions = _compliant_decisions()
        decisions.pop((_RUNTIME_ROLE_ARN, ACTION_LAMBDA_INVOKE, _tool_arn(_AGENT_LESS[0])))
        results = _by_check(_run(decisions=decisions)[0])
        assert results[f"R1[{_AGENT_LESS[0]}]_runtime_denied"].status == "fail"

    @pytest.mark.parametrize("pattern", ["exact", "wildcard"])
    def test_the_policy_text_row_fails_when_the_grant_names_the_tool(self, pattern: str) -> None:
        resource = (
            _tool_arn(_AGENT_LESS[0])
            if pattern == "exact"
            else f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:crm-agent-eval-*"
        )
        policy = json.loads(json.dumps(_RUNTIME_POLICY))
        policy["Statement"][0]["Resource"] = [*policy["Statement"][0]["Resource"], resource]
        iam = _FakeIam(
            _compliant_decisions(),
            wildcard=_compliant_wildcard(),
            policies={name_from_arn(_RUNTIME_ROLE_ARN): policy},
        )
        results = _by_check(_run(iam=iam)[0])
        assert results[f"R1[{_AGENT_LESS[0]}]_not_granted_to_runtime_by_policy"].status == "fail"
