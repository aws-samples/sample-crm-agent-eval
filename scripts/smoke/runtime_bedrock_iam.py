# SPDX-License-Identifier: MIT
"""Layer 4 runtime-role Bedrock grant smoke.

Simulates the deployed `crm-agent-eval-runtime-role` against the Bedrock model grant. Rows, all
read/simulate-only:

* ALLOW — for each `global.` inference profile in `config/model_capabilities.yaml`,
  `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` must evaluate `allowed`; and
  `bedrock:InvokeModel` on `arn:aws:bedrock:{region}:{account}:project/default` must be `allowed`.
* FM-via-profile — each model's region-less foundation-model ARN with a
  `bedrock:InferenceProfileArn` context equal to its profile must be `allowed`.
* DENY — the same foundation-model ARN with no `bedrock:InferenceProfileArn` context must be denied,
  and an inference-profile ARN for a model the grant does not name (a `us.meta.*` profile) must be
  denied.
* RETIRED — `bedrock:InvokeModel` on each :data:`RETIRED_INFERENCE_PROFILES` profile must be
  denied.

Every deny row shares its action string with an ALLOW row proved allowable in the same run, since
`SimulatePrincipalPolicy` also returns `implicitDeny` for a misspelled action or a malformed ARN.

Usage:
    python3 -m scripts.smoke.runtime_bedrock_iam [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final

import yaml
from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    run_main,
)
from scripts.smoke.iam_roles import _is_throttle  # one spelling of the throttle predicate

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "runtime_bedrock_iam"

ACTION_INVOKE_MODEL: Final[str] = "bedrock:InvokeModel"
ACTION_INVOKE_MODEL_STREAM: Final[str] = "bedrock:InvokeModelWithResponseStream"
_INVOKE_ACTIONS: Final[tuple[str, ...]] = (ACTION_INVOKE_MODEL, ACTION_INVOKE_MODEL_STREAM)
_CONDITION_KEY: Final[str] = "bedrock:InferenceProfileArn"

#: A well-formed inference-profile ARN for a model the grant does not name.
_UNLISTED_PROFILE: Final[str] = "us.meta.llama3-1-70b-instruct-v1:0"

#: Profiles removed from the capability matrix; used only for the deny rows.
RETIRED_INFERENCE_PROFILES: Final[tuple[str, ...]] = (
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.openai.gpt-6-sol",
)

_ALLOWED: Final[frozenset[str]] = frozenset({"allowed"})
_DENIED: Final[frozenset[str]] = frozenset({"explicitDeny", "implicitDeny"})
_NO_RESULT: Final[str] = "no_result"
_DEFER_NO_EVALUATION: Final[str] = "simulate_returned_no_evaluation_result"
_SIMULATE_MAX_ATTEMPTS: Final[int] = 4
_SIMULATE_BACKOFF_SECONDS: Final[float] = 1.0

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_CAPABILITIES_YAML: Final[Path] = _REPO_ROOT / "config" / "model_capabilities.yaml"

__all__ = [
    "LAYER",
    "RESOURCE_CLASS",
    "RETIRED_INFERENCE_PROFILES",
    "checks",
    "expected_check_count",
    "main",
]


def _intended_profiles() -> list[str]:
    """The `global.` inference profiles the capability matrix advertises — the intended grant.

    Read from `config/model_capabilities.yaml`, not from the deployed policy.
    """
    data = yaml.safe_load(_CAPABILITIES_YAML.read_text(encoding="utf-8"))
    return [entry["inference_profile"] for entry in data["models"].values()]


def _simulate(
    ctx: SmokeContext,
    role_arn: str,
    action: str,
    resource_arn: str,
    context_entries: list[dict[str, Any]] | None = None,
) -> str:
    """Return the simulate decision, threading `ContextEntries` when the grant is condition-bound.

    Mirrors `iam_roles._simulate`, adding the optional `ContextEntries` the condition rows need.
    """
    import time

    iam = ctx.client("iam")
    kwargs: dict[str, Any] = {
        "PolicySourceArn": role_arn,
        "ActionNames": [action],
        "ResourceArns": [resource_arn],
    }
    if context_entries:
        kwargs["ContextEntries"] = context_entries
    for attempt in range(1, _SIMULATE_MAX_ATTEMPTS + 1):
        try:
            response = iam.simulate_principal_policy(**kwargs)
            break
        except Exception as exc:  # noqa: BLE001 - re-raised unless a retryable throttle
            if attempt == _SIMULATE_MAX_ATTEMPTS or not _is_throttle(exc):
                raise
            time.sleep(_SIMULATE_BACKOFF_SECONDS * attempt)
    results = response.get("EvaluationResults", [])
    if not results:
        return _NO_RESULT
    return str(results[0]["EvalDecision"])


def _profile_context(profile_arn: str) -> list[dict[str, Any]]:
    return [
        {
            "ContextKeyName": _CONDITION_KEY,
            "ContextKeyValues": [profile_arn],
            "ContextKeyType": "string",
        }
    ]


def _row(
    resource: str, check: str, decision: str, expected: frozenset[str], why: str
) -> CheckResult:
    if decision == _NO_RESULT:
        return CheckResult(
            resource=resource,
            check=check,
            status="defer",
            detail=(
                f"{check}: {resource} returned no evaluation result, so this row asserts nothing "
                "either way. Re-run before reading the inventory as complete"
            ),
            defer_reason=_DEFER_NO_EVALUATION,
        )
    satisfied = decision in expected
    return CheckResult(
        resource=resource,
        check=check,
        status="pass" if satisfied else "fail",
        detail=f"evaluated {decision}; expected one of {sorted(expected)} -- {why}",
    )


def checks(ctx: SmokeContext) -> Iterator[CheckResult]:
    """Yield one row per (profile, action) allow, per-model condition allow/deny, and the negatives."""
    try:
        role_arn = ctx.outputs.require_str("agentcore_runtime_role_arn")
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, not a crash
        yield CheckResult(
            resource="agentcore_runtime_role_arn",
            check="terraform_output_present",
            status="fail",
            detail=(
                f"required Terraform output 'agentcore_runtime_role_arn' is absent ({exc}); it is "
                "this module's whole subject. A missing output is a failure, not a skipped class: "
                "an ARN built from a template that no longer resolves returns implicitDeny and reads "
                "as a pass"
            ),
        )
        return

    account_id = str(ctx.client("sts").get_caller_identity()["Account"])
    region = ctx.region
    profiles = _intended_profiles()
    if not profiles:
        yield CheckResult(
            resource=str(_CAPABILITIES_YAML),
            check="intended_profiles_readable",
            status="fail",
            detail="config/model_capabilities.yaml advertised no inference_profile, so this module "
            "would assert nothing",
        )
        return

    def profile_arn(profile: str) -> str:
        return f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{profile}"

    # --- ALLOW: each profile, both invoke actions. These also establish expressibility. ---
    for profile in profiles:
        arn = profile_arn(profile)
        for action in _INVOKE_ACTIONS:
            decision = _simulate(ctx, role_arn, action, arn)
            yield _row(
                arn,
                f"allow:{action}@{profile}",
                decision,
                _ALLOWED,
                "the runtime role must invoke every advertised profile",
            )

    # --- ALLOW: the default Bedrock project. ---
    project_arn = f"arn:aws:bedrock:{region}:{account_id}:project/default"
    yield _row(
        project_arn,
        f"allow:{ACTION_INVOKE_MODEL}@project/default",
        _simulate(ctx, role_arn, ACTION_INVOKE_MODEL, project_arn),
        _ALLOWED,
        "the GPT-6 models require project/default alongside the inference profile",
    )

    # --- Per model: FM reachable WITH its profile context, DENIED without it. ---
    for profile in profiles:
        model = profile.removeprefix("global.")
        fm_arn = f"arn:aws:bedrock:::foundation-model/{model}"
        yield _row(
            fm_arn,
            f"allow_with_profile_context:{ACTION_INVOKE_MODEL}@{model}",
            _simulate(
                ctx, role_arn, ACTION_INVOKE_MODEL, fm_arn, _profile_context(profile_arn(profile))
            ),
            _ALLOWED,
            "the foundation model is reachable through its inference profile",
        )
        yield _row(
            fm_arn,
            f"deny_without_profile_context:{ACTION_INVOKE_MODEL}@{model}",
            _simulate(ctx, role_arn, ACTION_INVOKE_MODEL, fm_arn),
            _DENIED,
            "without the bedrock:InferenceProfileArn context the FM must be denied — the condition "
            "is what limits access to the named profiles; the ALLOW rows above proved the "
            "action is expressible, so this deny is about scope, not a typo",
        )

    # --- DENY: a profile for a model the grant does not name. ---
    unlisted_arn = profile_arn(_UNLISTED_PROFILE)
    yield _row(
        unlisted_arn,
        f"deny:{ACTION_INVOKE_MODEL}@{_UNLISTED_PROFILE}",
        _simulate(ctx, role_arn, ACTION_INVOKE_MODEL, unlisted_arn),
        _DENIED,
        "a model outside the advertised profiles must stay denied; the ALLOW rows proved the action is "
        "expressible for this role, so this implicitDeny is scoping and not a malformed pair",
    )

    # --- DENY: each retired profile, and its foundation model through that profile. ---
    for retired in RETIRED_INFERENCE_PROFILES:
        if retired in profiles:
            yield CheckResult(
                resource=str(_CAPABILITIES_YAML),
                check=f"retired_profile_not_advertised:{retired}",
                status="fail",
                detail=f"{retired} is retired but still advertised by the capability matrix, so "
                "its deny rows would contradict the allow rows above",
            )
            continue
        retired_arn = profile_arn(retired)
        yield _row(
            retired_arn,
            f"deny_retired:{ACTION_INVOKE_MODEL}@{retired}",
            _simulate(ctx, role_arn, ACTION_INVOKE_MODEL, retired_arn),
            _DENIED,
            "a retired model left the grant; the ALLOW rows proved the action is expressible",
        )
        retired_fm = f"arn:aws:bedrock:::foundation-model/{retired.removeprefix('global.')}"
        yield _row(
            retired_fm,
            f"deny_retired_via_profile_context:{ACTION_INVOKE_MODEL}@{retired}",
            _simulate(
                ctx, role_arn, ACTION_INVOKE_MODEL, retired_fm, _profile_context(retired_arn)
            ),
            _DENIED,
            "the retired foundation model stays unreachable even through its own profile",
        )


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """Derived from the profile set the capability matrix advertises, never hardcoded."""
    n = len(_intended_profiles())
    r = len(RETIRED_INFERENCE_PROFILES)
    # n profiles x 2 invoke actions (allow) + 1 project + n (FM allow-with-context) + n (FM
    # deny-without-context) + 1 (unlisted-profile deny) + r retired profiles x 2 (deny).
    minimum = n * 2 + 1 + n + n + 1 + r * 2
    return ExpectedCheckCount(
        minimum=minimum,
        basis=(
            f"{n} advertised profile(s) x 2 invoke actions (allow) + 1 project/default allow + "
            f"{n} FM allow-with-profile-context + {n} FM deny-without-context + 1 unlisted-profile "
            f"deny + {r} retired profile(s) x 2 deny, derived from config/model_capabilities.yaml "
            "and RETIRED_INFERENCE_PROFILES"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: emit the runtime-role Bedrock grant inventory, non-zero on any fail."""
    return run_main(
        argv,
        description=__doc__ or "",
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main())
