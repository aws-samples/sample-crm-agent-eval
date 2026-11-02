# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/smoke/runtime_bedrock_iam.py`, against a fake IAM client.

The fake models a correctly-deployed grant; planted cases prove the ALLOW and DENY rows can fail.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.smoke import runtime_bedrock_iam as mod
from scripts.smoke.harness import context_for_test

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-runtime-role"


def _profile_arn(profile: str) -> str:
    return f"arn:aws:bedrock:{_REGION}:{_ACCOUNT}:inference-profile/{profile}"


class _FakeSts:
    @staticmethod
    def get_caller_identity() -> dict[str, str]:
        return {"Account": _ACCOUNT}


class _FakeIam:
    """Models the correctly-deployed grant; `overrides` force a specific (check-shape) decision."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.overrides = overrides or {}
        self.profiles = mod._intended_profiles()

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        # boto3 kwargs are PascalCase, accepted via **kwargs.
        action = kwargs["ActionNames"][0]
        resource = kwargs["ResourceArns"][0]
        context = kwargs.get("ContextEntries")
        decision = self._decide(action, resource, context)
        for key, forced in self.overrides.items():
            if key in resource or key in action:
                decision = forced
        return {"EvaluationResults": [{"EvalActionName": action, "EvalDecision": decision}]}

    def _decide(self, action: str, resource: str, context: list[dict[str, Any]] | None) -> str:
        profile_arns = {_profile_arn(p) for p in self.profiles}
        project_arn = f"arn:aws:bedrock:{_REGION}:{_ACCOUNT}:project/default"
        if resource in profile_arns or resource == project_arn:
            return "allowed"
        if ":foundation-model/" in resource:
            model = resource.rsplit("/", 1)[1]
            if f"global.{model}" not in self.profiles:
                return "implicitDeny"
            expected_ctx = _profile_arn(f"global.{model}")
            supplied = context[0]["ContextKeyValues"] if context else []
            return "allowed" if expected_ctx in supplied else "implicitDeny"
        return "implicitDeny"  # unlisted profile


def _run(overrides: dict[str, str] | None = None) -> list[Any]:
    ctx = context_for_test(
        clients={"iam": _FakeIam(overrides), "sts": _FakeSts()},
        outputs={"agentcore_runtime_role_arn": _ROLE_ARN},
        region=_REGION,
    )
    return list(mod.checks(ctx))


def _failed(results: list[Any]) -> dict[str, str]:
    return {r.check: r.detail for r in results if r.status == "fail"}


def test_a_correct_grant_passes_every_row() -> None:
    results = _run()
    assert results
    assert not _failed(results), _failed(results)
    # At least the derived minimum was emitted.
    assert (
        len(results)
        >= mod.expected_check_count(context_for_test(outputs={}, region=_REGION)).minimum
    )


def test_a_denied_profile_fails_its_allow_row() -> None:
    """If the deployed role does not grant one profile, its ALLOW row must red."""
    victim = mod._intended_profiles()[0]
    results = _run(overrides={victim: "implicitDeny"})
    failed = _failed(results)
    assert any(f"@{victim}" in check and check.startswith("allow:") for check in failed), failed


def test_an_fm_allowed_without_context_fails_the_deny_row() -> None:
    """A foundation model allowed with no inference-profile context fails the deny row."""

    class _NoConditionIam(_FakeIam):
        def _decide(self, action: str, resource: str, context: list[dict[str, Any]] | None) -> str:
            if ":foundation-model/" in resource:
                return "allowed"  # condition removed: reachable by any route
            return super()._decide(action, resource, context)

    ctx = context_for_test(
        clients={"iam": _NoConditionIam(), "sts": _FakeSts()},
        outputs={"agentcore_runtime_role_arn": _ROLE_ARN},
        region=_REGION,
    )
    failed = _failed(list(mod.checks(ctx)))
    assert any(check.startswith("deny_without_profile_context:") for check in failed), failed


def test_a_missing_role_output_is_a_failure_not_a_crash() -> None:
    ctx = context_for_test(clients={}, outputs={}, region=_REGION)
    results = list(mod.checks(ctx))
    assert len(results) == 1
    assert results[0].status == "fail"
    assert results[0].check == "terraform_output_present"


def test_no_evaluation_result_defers_rather_than_passing() -> None:
    class _NoResultIam(_FakeIam):
        def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
            return {"EvaluationResults": []}

    ctx = context_for_test(
        clients={"iam": _NoResultIam(), "sts": _FakeSts()},
        outputs={"agentcore_runtime_role_arn": _ROLE_ARN},
        region=_REGION,
    )
    results = list(mod.checks(ctx))
    assert results and all(r.status == "defer" for r in results), results
    assert all(r.defer_reason == mod._DEFER_NO_EVALUATION for r in results)


def test_every_retired_profile_has_both_deny_rows() -> None:
    """Each retired profile gets a profile deny row and a foundation-model-through-profile deny row."""
    checks = {r.check: r.status for r in _run()}
    for retired in mod.RETIRED_INFERENCE_PROFILES:
        assert checks[f"deny_retired:{mod.ACTION_INVOKE_MODEL}@{retired}"] == "pass"
        assert checks[f"deny_retired_via_profile_context:{mod.ACTION_INVOKE_MODEL}@{retired}"] == (
            "pass"
        )


def test_a_retired_profile_still_granted_reds_its_deny_rows() -> None:
    """A deployed grant that still names a retired profile fails both of its deny rows."""
    retired = mod.RETIRED_INFERENCE_PROFILES[0]

    class _StaleIam(_FakeIam):
        def __init__(self) -> None:
            super().__init__()
            self.profiles = [*self.profiles, retired]

    ctx = context_for_test(
        clients={"iam": _StaleIam(), "sts": _FakeSts()},
        outputs={"agentcore_runtime_role_arn": _ROLE_ARN},
        region=_REGION,
    )
    failed = _failed(list(mod.checks(ctx)))
    assert set(failed) == {
        f"deny_retired:{mod.ACTION_INVOKE_MODEL}@{retired}",
        f"deny_retired_via_profile_context:{mod.ACTION_INVOKE_MODEL}@{retired}",
    }, failed


def test_a_retired_profile_still_in_the_matrix_fails_instead_of_contradicting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matrix that still advertises a retired profile is a failing row, not a silent deny skip."""
    retired = mod.RETIRED_INFERENCE_PROFILES[0]
    advertised = [*mod._intended_profiles(), retired]
    monkeypatch.setattr(mod, "_intended_profiles", lambda: advertised)
    failed = _failed(_run())
    assert f"retired_profile_not_advertised:{retired}" in failed, failed


def test_no_retired_profile_is_advertised_by_the_matrix() -> None:
    """The retired constant and the capability matrix are disjoint."""
    assert mod.RETIRED_INFERENCE_PROFILES
    assert not set(mod.RETIRED_INFERENCE_PROFILES) & set(mod._intended_profiles())
