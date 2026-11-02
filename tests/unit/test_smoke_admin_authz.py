# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.admin_authz` and the helpers it shares.

The run API is a fake that applies the real handler's admin rule to a token's groups. Provisioners
and clients are fakes, so nothing provisions a user; tokens verify against a loopback JWKS.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from scripts.smoke import admin_authz as mod
from scripts.smoke.cognito_claims import ProvisionedSmokeUser
from scripts.smoke.harness import CheckResult, context_for_test

from crm.authz import ADMIN_GROUP
from tests.unit import _jwks_test_key as test_key

_TERMINAL_RUN = {
    "run_id": "run0001",
    "status": "completed",
    "is_terminal": True,
    "tasks_done": 40,
    "tasks_failed": 0,
    "tasks_skipped": 0,
    "cost_spent_usd": 0.0,
    "finished_at": "2026-10-08T00:00:00+00:00",
}


_POOL_OUTPUTS = {
    "cognito_user_pool_id": test_key.USER_POOL_ID,
    "cognito_spa_client_id": test_key.CLIENT_ID,
}


def _token(groups: list[str] | None) -> str:
    claims: dict[str, Any] = {"custom:tenant": "smoke-x"}
    if groups is not None:
        claims["cognito:groups"] = groups
    return test_key.id_token(**claims)


ADMIN_TOKEN = _token([ADMIN_GROUP])
USER_TOKEN = _token(None)


class FakeApi:
    """`/api/eval_trigger` with the admin-only rule; `enforce=False` drops the gate."""

    def __init__(self, *, enforce: bool = True, runs: list[dict[str, Any]] | None = None) -> None:
        self.enforce = enforce
        self.runs = [dict(r) for r in (runs if runs is not None else [_TERMINAL_RUN])]
        self.calls: list[tuple[str | None, dict[str, Any]]] = []

    def __call__(
        self, route: str, token: str | None, body: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        assert route == mod.EVAL_TRIGGER_ROUTE
        self.calls.append((token, dict(body)))
        if token is None:
            return 401, {"message": "Unauthorized"}
        admin = token == ADMIN_TOKEN
        action = body["action"]
        if action == "list":
            return 200, {"runs": self.runs, "next_cursor": None}
        gated = action in ("cancel", "unlock") or (
            action == "submit" and body.get("mode") == "live"
        )
        if gated and self.enforce and not admin:
            return 403, {
                "error": {
                    "code": "scope_denied",
                    "message": "requires an administrator",
                    "missing_scope": "eval:admin",
                }
            }
        if action == "submit":
            return 400, {"error": {"code": "validation_failed", "source": "confirmed"}}
        if action == "unlock":
            return 200, {"released": False, "holder_run_id": None}
        run = next(r for r in self.runs if r["run_id"] == body["run_id"])
        return 200, {"disposition": "already_terminal", "run": dict(run)}


class FakeProvisioner:
    def __init__(self, token: str, *, fail: bool = False) -> None:
        self.token = token
        self.fail = fail
        self.cleaned = False

    def provision(self) -> ProvisionedSmokeUser:
        if self.fail:
            raise RuntimeError("no pool")
        return ProvisionedSmokeUser(
            username="u",
            rep_id="u",
            tenant="smoke-x",
            book=frozenset(),
            id_token=self.token,
            detail="TOTP",
        )

    def cleanup(self) -> Iterator[CheckResult]:
        self.cleaned = True
        yield CheckResult(resource="smoke_user", check="cognito_user_deleted", status="pass")


class FakeIdp:
    def __init__(self, *, has_group: bool = True) -> None:
        self.has_group = has_group

    def get_group(self, *, GroupName: str, UserPoolId: str) -> dict[str, Any]:  # noqa: N803
        if not self.has_group:
            raise RuntimeError("ResourceNotFoundException: group not found")
        return {"Group": {"GroupName": GroupName}}


def _run(
    api: FakeApi,
    *,
    admin_token: str = ADMIN_TOKEN,
    idp: FakeIdp | None = None,
    admin: FakeProvisioner | None = None,
    non_admin: FakeProvisioner | None = None,
) -> tuple[list[CheckResult], dict[str, CheckResult]]:
    ctx = context_for_test(
        outputs=_POOL_OUTPUTS,
        clients={"cognito-idp": idp or FakeIdp()},
    )
    results = list(
        mod.checks(
            ctx,
            admin=admin or FakeProvisioner(admin_token),
            non_admin=non_admin or FakeProvisioner(USER_TOKEN),
            post=api,
            jwks_url=test_key.jwks_url(),
        )
    )
    return results, {r.check: r for r in results}


def test_a_correctly_gated_api_passes_every_check() -> None:
    results, by_check = _run(FakeApi())
    assert [r.check for r in results if r.resource == mod.RESOURCE_CLASS] == [
        "unauthenticated_request_denied",
        "admin_group_exists",
        "provision_admin_user",
        "provision_non_admin_user",
        "admin_token_groups_claim",
        "terminal_run_available",
        "non_admin_submit_live_denied",
        "non_admin_unlock_denied",
        "non_admin_cancel_denied",
        "admin_cancel_of_terminal_run_is_noop",
    ]
    assert {r.check: r.detail for r in results if r.status != "pass"} == {}


def test_a_missing_admin_gate_reds_every_non_admin_row() -> None:
    _, by_check = _run(FakeApi(enforce=False))
    reddened = {c for c, r in by_check.items() if r.status == "fail"}
    assert reddened == {
        "non_admin_submit_live_denied",
        "non_admin_unlock_denied",
        "non_admin_cancel_denied",
    }
    assert "HTTP 400" in by_check["non_admin_submit_live_denied"].detail


def test_the_live_probe_never_confirms_so_a_missing_gate_cannot_start_a_paid_sweep() -> None:
    api = FakeApi(enforce=False)
    _run(api)
    live = [body for _token, body in api.calls if body.get("mode") == "live"]
    assert live and all(body["confirmed"] is False for body in live)


def test_an_admin_token_without_the_group_fails_the_claim_row() -> None:
    _, by_check = _run(FakeApi(), admin_token=_token(["sales"]))
    assert by_check["admin_token_groups_claim"].status == "fail"


@pytest.mark.parametrize(
    "rendering", [[ADMIN_GROUP], f'["{ADMIN_GROUP}"]', f"[{ADMIN_GROUP}]", f"{ADMIN_GROUP},x"]
)
def test_the_groups_claim_rendering_is_parsed_and_recorded(rendering: object) -> None:
    row = _groups_row(test_key.id_token(**{"cognito:groups": rendering}))
    assert row.status == "pass" and type(rendering).__name__ in row.detail


def _groups_row(token: str) -> CheckResult:
    return mod.check_groups_claim(
        token,
        region=test_key.REGION,
        user_pool_id=test_key.USER_POOL_ID,
        client_id=test_key.CLIENT_ID,
        jwks_url=test_key.jwks_url(),
    )


def test_a_forged_admin_token_fails_the_claim_row() -> None:
    forged = test_key.TestKey().sign(test_key.id_token_claims(**{"cognito:groups": [ADMIN_GROUP]}))
    _, by_check = _run(FakeApi(), admin_token=forged)
    row = by_check["admin_token_groups_claim"]
    assert row.status == "fail" and "signature" in row.detail


def test_an_admin_token_for_another_client_fails_the_claim_row() -> None:
    row = _groups_row(test_key.id_token(aud="another-client", **{"cognito:groups": [ADMIN_GROUP]}))
    assert row.status == "fail" and "aud" in row.detail


def test_a_missing_group_in_the_pool_fails() -> None:
    _, by_check = _run(FakeApi(), idp=FakeIdp(has_group=False))
    assert by_check["admin_group_exists"].status == "fail"


def test_no_terminal_run_skips_the_cancel_rows_loudly() -> None:
    running = dict(_TERMINAL_RUN, is_terminal=False, status="running")
    _, by_check = _run(FakeApi(runs=[running]))
    assert by_check["terminal_run_available"].status == "fail"
    assert "non_admin_cancel_denied" not in by_check
    assert "admin_cancel_of_terminal_run_is_noop" not in by_check


def test_a_terminal_cancel_that_changes_the_run_fails() -> None:
    class Mutating(FakeApi):
        def __call__(
            self, route: str, token: str | None, body: Mapping[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            status, payload = super().__call__(route, token, body)
            if body["action"] == "cancel" and status == 200:
                payload["run"]["status"] = "cancelling"
            return status, payload

    _, by_check = _run(Mutating())
    row = by_check["admin_cancel_of_terminal_run_is_noop"]
    assert row.status == "fail" and "status" in row.detail


def test_a_provisioning_failure_cleans_up_both_users() -> None:
    admin = FakeProvisioner(ADMIN_TOKEN, fail=True)
    non_admin = FakeProvisioner(USER_TOKEN)
    _, by_check = _run(FakeApi(), admin=admin, non_admin=non_admin)
    assert by_check["provision_admin_user"].status == "fail"
    assert admin.cleaned and non_admin.cleaned
    assert "non_admin_submit_live_denied" not in by_check


def test_an_unauthenticated_success_fails() -> None:
    row = mod.unauthenticated_check(
        lambda _r, _t, _b: (200, {"runs": []}), route="eval_trigger", body={}, resource="x"
    )
    assert row.status == "fail"


# -- shared helpers ------------------------------------------------------------------------------


class _RecordingIdp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_set_user_password", kwargs))
        return {}

    def admin_add_user_to_group(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_add_user_to_group", kwargs))
        return {}


def test_the_admin_provisioner_joins_the_group_right_after_the_password_is_set() -> None:
    base = _RecordingIdp()
    ctx = context_for_test(outputs={"cognito_user_pool_id": "p"}, clients={"cognito-idp": base})
    provisioner = mod.RoleSmokeUserProvisioner(ctx, full_visibility=True, admin_group=ADMIN_GROUP)
    provisioner._idp().admin_set_user_password(
        UserPoolId="p",
        Username="u",
        Password="x",  # nosec B106  # test double argument, no credential
        Permanent=True,
    )
    assert [name for name, _ in base.calls] == [
        "admin_set_user_password",
        "admin_add_user_to_group",
    ]
    assert base.calls[1][1] == {"UserPoolId": "p", "Username": "u", "GroupName": ADMIN_GROUP}


def test_the_non_admin_provisioner_joins_no_group() -> None:
    base = _RecordingIdp()
    ctx = context_for_test(clients={"cognito-idp": base})
    provisioner = mod.RoleSmokeUserProvisioner(ctx, full_visibility=False)
    assert provisioner._idp() is base


@pytest.mark.parametrize("full_visibility", [True, False])
def test_the_provisioner_store_forces_full_visibility_on_reps_puts(full_visibility: bool) -> None:
    puts: list[tuple[str, dict[str, Any]]] = []

    class Base:
        def put_item(self, table: str, item: Mapping[str, Any]) -> None:
            puts.append((table, dict(item)))

    store = mod._RepsVisibilityStore(Base(), full_visibility=full_visibility)
    store.put_item("reps", {"rep_id": "u", "full_visibility": not full_visibility})
    store.put_item("accounts", {"account_id": "a"})
    assert puts == [
        ("reps", {"rep_id": "u", "full_visibility": full_visibility}),
        ("accounts", {"account_id": "a"}),
    ]


class _Iam:
    def __init__(self, decision: str | None, *, throttles: int = 0) -> None:
        self.decision = decision
        self.throttles = throttles
        self.requests: list[dict[str, Any]] = []

    def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        if self.throttles:
            self.throttles -= 1
            exc = RuntimeError("throttled")
            exc.response = {"Error": {"Code": "Throttling"}}  # type: ignore[attr-defined]
            raise exc
        if self.decision is None:
            return {"EvaluationResults": []}
        return {"EvaluationResults": [{"EvalDecision": self.decision}]}


_ALLOW = mod.SimulationRow("c", "role", "dynamodb:GetItem", "arn:t", True, ("usersettings#x",))
_DENY = mod.SimulationRow("c", "role", "dynamodb:GetItem", "arn:t", False)


@pytest.mark.parametrize(
    ("row", "decision", "status"),
    [
        (_ALLOW, "allowed", "pass"),
        (_ALLOW, "implicitDeny", "fail"),
        (_DENY, "implicitDeny", "pass"),
        (_DENY, "explicitDeny", "pass"),
        (_DENY, "allowed", "fail"),
        (_ALLOW, None, "fail"),
        (_DENY, None, "fail"),
    ],
)
def test_simulation_result_matches_expectation(
    row: mod.SimulationRow, decision: str | None, status: str
) -> None:
    assert mod.simulation_result(_Iam(decision), row, resource="r").status == status


def test_simulation_sends_leading_keys_and_retries_throttles() -> None:
    iam = _Iam("allowed", throttles=2)
    sleeps: list[float] = []
    assert mod.simulation_result(iam, _ALLOW, resource="r", sleeper=sleeps.append).status == "pass"
    assert len(iam.requests) == 3 and len(sleeps) == 2
    (entry,) = iam.requests[0]["ContextEntries"]
    assert entry["ContextKeyName"] == "dynamodb:LeadingKeys"
    assert entry["ContextKeyValues"] == ["usersettings#x"]
    unkeyed = _Iam("implicitDeny")
    mod.simulation_result(unkeyed, _DENY, resource="r")
    assert "ContextEntries" not in unkeyed.requests[0]


def test_function_name_prefix_requires_the_eval_trigger_suffix() -> None:
    good = context_for_test(outputs={"eval_trigger_function_name": "crm-agent-eval-eval-trigger"})
    assert mod.function_name_prefix(good) == "crm-agent-eval"
    bad = context_for_test(outputs={"eval_trigger_function_name": "renamed"})
    with pytest.raises(RuntimeError):
        mod.function_name_prefix(bad)


def test_load_json_object_tolerates_non_objects() -> None:
    assert mod.load_json_object(b"") == {}
    assert mod.load_json_object(b"[1]") == {}
    assert mod.load_json_object(b"not json") == {}
    assert mod.load_json_object(b'{"a": 1}') == {"a": 1}


def test_a_decision_reached_with_missing_context_values_fails() -> None:
    class Unkeyed(_Iam):
        def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
            return {
                "EvaluationResults": [
                    {
                        "EvalDecision": "implicitDeny",
                        "MissingContextValues": ["dynamodb:LeadingKeys"],
                    }
                ]
            }

    row = mod.simulation_result(Unkeyed("implicitDeny"), _DENY, resource="r")
    assert row.status == "fail" and "MissingContextValues" in row.detail
    assert mod.simulate_evaluation(Unkeyed(None), "role", "a", "r") == (
        "implicitDeny",
        ("dynamodb:LeadingKeys",),
    )


# -- lambda:SourceFunctionArn ------------------------------------------------------------


class _Meta:
    region_name = "us-east-1"


class _SourceFunctionAwareIam:
    """Reports `lambda:SourceFunctionArn` missing unless it is sent, and `also_missing` regardless."""

    meta = _Meta()

    def __init__(self, decision: str, also_missing: tuple[str, ...] = ()) -> None:
        self.decision = decision
        self.also_missing = also_missing
        self.requests: list[dict[str, Any]] = []

    def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        sent = {entry["ContextKeyName"] for entry in request.get("ContextEntries", [])}
        missing = [*self.also_missing]
        if "lambda:SourceFunctionArn" not in sent:
            missing.append("lambda:SourceFunctionArn")
        return {
            "EvaluationResults": [{"EvalDecision": self.decision, "MissingContextValues": missing}]
        }


_CT_ROLE = "arn:aws:iam::123456789012:role/crm-agent-eval-chat-target-role"
_CT_FUNCTION = "arn:aws:lambda:us-east-1:123456789012:function:crm-agent-eval-chat-target"
_REPS = "arn:aws:dynamodb:us-east-1:123456789012:table/crm-agent-evalreps"


def test_the_source_function_arn_is_the_one_the_role_name_implies() -> None:
    assert mod.source_function_arn(_CT_ROLE, "us-east-1") == _CT_FUNCTION
    assert mod.source_function_arn(_CT_ROLE, None) is None
    assert (
        mod.source_function_arn(
            "arn:aws:sts::123456789012:assumed-role/crm-agent-eval-x-role/session", "us-east-1"
        )
        is None
    )
    assert (
        mod.source_function_arn(
            "arn:aws:iam::123456789012:policy/crm-agent-eval-x-role", "us-east-1"
        )
        is None
    )
    assert mod.source_function_arn("arn:aws:iam::123456789012:role/no-suffix", "us-east-1") is None
    assert mod.source_function_arn("arn:aws:iam::123456789012:role/-role", "us-east-1") is None
    assert (
        mod.source_function_arn(
            "arn:aws:iam::123456789012:role/svc/crm-agent-eval-x-role", "eu-west-1"
        )
        == "arn:aws:lambda:eu-west-1:123456789012:function:crm-agent-eval-x"
    )


def test_every_simulation_sends_the_roles_own_function_arn() -> None:
    iam = _SourceFunctionAwareIam("implicitDeny")
    mod.simulate_evaluation(iam, _CT_ROLE, "dynamodb:PutItem", _REPS, leading_keys=["k"])
    (request,) = iam.requests
    entries = {e["ContextKeyName"]: e["ContextKeyValues"] for e in request["ContextEntries"]}
    assert entries == {"dynamodb:LeadingKeys": ["k"], "lambda:SourceFunctionArn": [_CT_FUNCTION]}


def test_an_expected_denial_passes_once_the_function_arn_is_supplied() -> None:
    row = mod.SimulationRow(
        check="ct_denies_put_usersettings",
        role_arn=_CT_ROLE,
        action="dynamodb:PutItem",
        resource_arn=_REPS,
        allowed=False,
        leading_keys=("usersettings#t#r",),
    )
    result = mod.simulation_result(_SourceFunctionAwareIam("implicitDeny"), row, resource="ct")
    assert result.status == "pass", result.detail
    assert "MissingContextValues" not in result.detail


def test_any_other_missing_context_key_still_fails_the_row() -> None:
    row = mod.SimulationRow(
        check="x", role_arn=_CT_ROLE, action="dynamodb:GetItem", resource_arn=_REPS, allowed=False
    )
    iam = _SourceFunctionAwareIam("implicitDeny", also_missing=("dynamodb:LeadingKeys",))
    result = mod.simulation_result(iam, row, resource="ct")
    assert result.status == "fail"
    assert "MissingContextValues=['dynamodb:LeadingKeys']" in result.detail


# -- another service's missing key is not decisive -----------------------------------


@pytest.mark.parametrize(
    ("action", "missing", "decisive"),
    [
        ("s3:PutObject", ("dynamodb:LeadingKeys",), ()),
        ("states:StopExecution", ("dynamodb:LeadingKeys",), ()),
        ("bedrock-agentcore:InvokeAgentRuntime", ("dynamodb:LeadingKeys",), ()),
        ("dynamodb:GetItem", ("dynamodb:LeadingKeys",), ("dynamodb:LeadingKeys",)),
        ("DynamoDB:GetItem", ("dynamodb:LeadingKeys",), ("dynamodb:LeadingKeys",)),
        ("s3:PutObject", ("aws:SourceVpce",), ("aws:SourceVpce",)),
        (
            "s3:PutObject",
            ("s3:x-amz-server-side-encryption",),
            ("s3:x-amz-server-side-encryption",),
        ),
        ("dynamodb:PutItem", ("lambda:SourceFunctionArn",), ("lambda:SourceFunctionArn",)),
        ("s3:PutObject", ("ec2:SourceInstanceARN",), ("ec2:SourceInstanceARN",)),
        ("s3:PutObject", ("unnamespaced",), ("unnamespaced",)),
        (
            "lambda:InvokeFunction",
            ("dynamodb:LeadingKeys", "aws:PrincipalTag/team"),
            ("aws:PrincipalTag/team",),
        ),
    ],
    ids=[
        "s3-ignores-dynamodb",
        "states-ignores-dynamodb",
        "agentcore-ignores-dynamodb",
        "own-service-decisive",
        "own-service-case-insensitive",
        "global-aws-decisive",
        "own-s3-key-decisive",
        "cross-service-lambda-source-decisive",
        "cross-service-ec2-source-decisive",
        "no-namespace-decisive",
        "mixed",
    ],
)
def test_only_another_services_keys_are_not_decisive(
    action: str, missing: tuple[str, ...], decisive: tuple[str, ...]
) -> None:
    assert mod.decisive_missing_keys(action, missing) == decisive


def test_a_non_dynamodb_denial_with_only_leading_keys_missing_passes_and_says_why() -> None:
    """The run-3 shape: s3:PutObject implicitDeny with dynamodb:LeadingKeys reported missing."""
    row = mod.SimulationRow(
        "control_denies_put_outside_prefix",
        "arn:aws:iam::123456789012:role/crm-agent-eval-sweep-control-role",
        "s3:PutObject",
        "arn:aws:s3:::bucket/outside-prefix/matrix.json",
        False,
    )
    iam = _SourceFunctionAwareIam("implicitDeny", also_missing=("dynamodb:LeadingKeys",))
    result = mod.simulation_result(iam, row, resource="r")
    assert result.status == "pass", result.detail
    assert "not decisive" in result.detail and "dynamodb:LeadingKeys" in result.detail
    assert "MissingContextValues" not in result.detail


def test_a_wrong_decision_still_fails_when_only_ignored_keys_are_missing() -> None:
    row = mod.SimulationRow("x", "role", "s3:PutObject", "arn:aws:s3:::b/k", False)

    class Allowing(_Iam):
        def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
            return {
                "EvaluationResults": [
                    {"EvalDecision": "allowed", "MissingContextValues": ["dynamodb:LeadingKeys"]}
                ]
            }

    assert mod.simulation_result(Allowing("allowed"), row, resource="r").status == "fail"
