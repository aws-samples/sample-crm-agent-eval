# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/check_plan_security_baseline.py`, the Layer 3 plan security gate.

Covers the compliant fixture (passes every control), one violation fixture per control (fails the
control it targets), and the `--self-check` coverage inventory.
"""

from __future__ import annotations

import copy
import functools
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from scripts import plan_fixtures, proc_run

from tests.unit._deferred_tally import EXPECTED_DEFERRED_BY_CONTROL

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_plan_security_baseline.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "plans"

if TYPE_CHECKING:
    # scripts/ is not a package, so mypy sees the module here and runtime loads it via importlib.
    import scripts.check_plan_security_baseline as gate
else:
    _SPEC = importlib.util.spec_from_file_location("check_plan_security_baseline", SCRIPT_PATH)
    assert _SPEC is not None and _SPEC.loader is not None
    gate = importlib.util.module_from_spec(_SPEC)
    sys.modules["check_plan_security_baseline"] = gate
    _SPEC.loader.exec_module(gate)


def _run_gates_module() -> Any:
    """Return the real `scripts/run_gates.py` module, which owns the defer vocabulary."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import run_gates  # type: ignore[import-not-found]

    return run_gates


def _gate_placeholder_account_ids() -> frozenset[str]:
    """The placeholder ids `check_no_account_id.sh` exempts, read via that gate's own accessor."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_untracked_artifact_secrets  # type: ignore[import-not-found]

    ids: frozenset[str] = check_untracked_artifact_secrets.placeholder_account_ids()
    return ids


@functools.cache
def fixture_path(name: str) -> Path:
    """The plan file for `name`: built by `scripts.plan_fixtures` for its names, else tracked."""
    if name in plan_fixtures.NAMES:
        return plan_fixtures.materialize(name)
    return FIXTURES_DIR / f"{name}.json"


def load_plan(name: str) -> gate.Plan:
    return gate.load_plan(fixture_path(name))


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def assert_clean_run(result: subprocess.CompletedProcess[str]) -> None:
    """Assert no control failed and the CLI exited 0, including against a stale fixture."""
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[FAIL]" not in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Compliant fixture: every control must pass.
# ---------------------------------------------------------------------------


class TestCompliantFixture:
    def test_passes_every_control(self) -> None:
        plan = load_plan("compliant")
        findings = gate.run_gate(plan)
        failed = [f for f in findings if f.status == "fail"]
        assert not failed, f"compliant fixture unexpectedly failed: {failed}"
        assert findings, "compliant fixture produced zero findings -- gate did not run"

    def test_passes_via_cli_exit_code(self) -> None:
        result = run_cli("--plan", str(fixture_path("compliant")))
        assert_clean_run(result)

    def test_no_account_id_leaked(self) -> None:
        # The allowed placeholders are read from the account-id gate itself.
        allowed = _gate_placeholder_account_ids()
        assert allowed, "could not read the placeholder allowlist from the account-id gate"
        text = plan_fixtures.BASE_PATH.read_text() + fixture_path("compliant").read_text()
        hits = {m for m in re.findall(r"\b\d{12}\b", text) if m not in allowed}
        assert not hits, f"compliant fixture leaks a non-placeholder 12-digit id: {hits}"

    def test_covers_every_registered_control(self) -> None:
        """The compliant fixture must actually exercise every control, not just avoid failing it."""
        plan = load_plan("compliant")
        findings = gate.run_gate(plan)
        exercised = {f.control for f in findings}
        control_ids = {c.control_id for c in gate.CONTROLS}
        missing = control_ids - exercised
        assert not missing, f"controls with zero findings against the compliant fixture: {missing}"


# ---------------------------------------------------------------------------
# Non-vacuity: each violation fixture must fail its targeted control, and no
# control fails silently by being untested.
# ---------------------------------------------------------------------------

VIOLATION_TARGETS: dict[str, str] = {
    "violation_relay_function_url_buffered": "relay_function_url_streams",
    "violation_dynamodb_no_cmk": "dynamodb_cmk",
    "violation_dynamodb_no_pitr": "dynamodb_pitr",
    "violation_dynamodb_wildcard_action": "dynamodb_no_wildcard_action",
    "violation_dynamodb_leading_keys_on_synthetic_key_table": "dynamodb_leading_keys_isolation",
    "violation_dynamodb_gsi_parity": "dynamodb_gsi_parity",
    "violation_resource_existence_counts": "resource_existence_counts",
    "violation_lambda_shared_role": "lambda_role_uniqueness",
    "violation_lambda_zero_concurrency": "lambda_reserved_concurrency_positive",
    "violation_lambda_concurrency_headroom": "lambda_reserved_concurrency_headroom",
    "violation_lambda_env_no_kms": "lambda_env_kms",
    "violation_lambda_wildcard_principal": "lambda_resource_policy_no_wildcard",
    "violation_api_route_unauthenticated": "api_authorizer_on_every_route",
    "violation_api_no_access_logging": "api_access_logging",
    "violation_api_no_throttle": "api_throttle",
    "violation_s3_bpa_disabled": "s3_block_public_access",
    "violation_s3_no_cmk": "s3_cmk_encryption",
    "violation_s3_no_tls_deny": "s3_tls_deny",
    "violation_s3_tls_deny_condition_inverted": "s3_tls_deny",
    "violation_s3_tls_deny_condition_deleted": "s3_tls_deny",
    "violation_s3_tls_deny_condition_narrowed": "s3_tls_deny",
    "violation_s3_tls_deny_actions_narrowed": "s3_tls_deny",
    "violation_s3_tls_deny_decoy_literal_bucket": "s3_tls_deny",
    "violation_cloudfront_no_oac": "cloudfront_oac",
    "violation_cognito_no_mfa": "cognito_mfa",
    "violation_cognito_weak_password_policy": "cognito_password_policy",  # nosec B105  # check id, not a credential  # pragma: allowlist secret
    "violation_cognito_lite_tier": "cognito_tier_and_trigger_version",
    "violation_cognito_write_attributes_scope_leak": "cognito_write_attributes_exclude_scope",
    "violation_gateway_no_custom_jwt": "gateway_custom_jwt",
    "violation_gateway_authorizer_constrains_nothing": "gateway_custom_jwt",
    "violation_gateway_no_interceptor": "gateway_interceptor_present",
    "violation_security_group_public_ingress": "no_public_ingress",
    "violation_log_group_no_retention": "log_group_retention",
    "violation_log_group_no_kms": "log_group_kms",
    "violation_cloudtrail_missing": "cloudtrail_enabled",
    "violation_missing_mandatory_tag": "mandatory_tags",
    "violation_cognito_no_account_recovery": "cognito_account_recovery",
    "violation_cloudfront_missing_behaviour": "cloudfront_three_behaviours",
    "violation_bedrock_static_credentials": "bedrock_no_static_credentials",
    "violation_lambda_gateway_role_wildcard_principal": "lambda_resource_policy_no_wildcard",
    "violation_lambda_gateway_role_wildcard_arn": "lambda_resource_policy_no_wildcard",
    "violation_lambda_gateway_role_root_principal": "lambda_resource_policy_no_wildcard",
    "violation_lambda_gateway_role_cross_account": "lambda_resource_policy_no_wildcard",
    "violation_lambda_gateway_role_wrong_statement_id": "lambda_resource_policy_no_wildcard",
    "violation_lambda_gateway_role_mismatched_role": "lambda_resource_policy_no_wildcard",
}


#: Plan-derived controls whose non-vacuity is proved by mutating the compliant plan **in memory**
#: rather than by a committed violation fixture. A distinct category from `SOURCE_DERIVED_CONTROLS`:
#: these DO read a plan, so a fixture could in principle violate them — the reason there is none is
#: recorded per entry, and the proof is named so it can be found. Same both-directions check.
IN_MEMORY_MUTATION_CONTROLS: dict[str, str] = {
    "cognito_admin_only_signup": "A violation fixture would be a near-copy of compliant.json with one "
    "attribute changed. `TestCognitoAdminOnlySignup` instead mutates the loaded document in memory and "
    "asserts a FAILURE for a false value, an absent block and an unknown block against a current plan, "
    "and for an absent/unknown block against a stale plan whose infra/ source lacks the literal; plus "
    "PASS on true, which the committed fixture carries.",
    "api_request_validators": "A violation fixture would be a near-copy of compliant.json with one "
    "method's request_validator_id or one POST's request_models removed — a second large artefact "
    "needing regeneration whenever the real plan changes, and a place for the two to drift. "
    "`TestRestApiControlsReadTheRestApi` instead builds a synthetic single-method plan and asserts a "
    "FAILURE for "
    "each of: a method with no request_validator_id (not even unknown-at-plan), a POST with no "
    "application/json model, and a GET carrying a body model; plus PASS for the compliant unknown-id "
    "POST and the model-less GET.",
    "lambda_permission_composition": "Its subject is a SET DIFFERENCE between the plan's grant names "
    "and infra/'s, so a violation fixture would be a near-copy of `compliant.json` with one grant "
    "renamed — a second large artefact needing regeneration whenever the real plan changes, and a "
    "place for the two to drift. `TestLambdaPermissionComposition` instead mutates the loaded document "
    "in memory and asserts a FAILURE (or, while the fixture is stale, a DEFERRAL that NAMES the "
    "divergence) for each of: a grant the plan has and infra/ does not, a grant infra/ has and the plan "
    "does not, **a one-for-one substitution that leaves the total unchanged** — the case a count cannot "
    "see and the reason this control exists — an empty plan, and an empty source tree. It also asserts "
    "the real repository's current, known divergence is reported by name rather than absorbed.",
    "resource_inventory_identity": "Its failing direction is a MISSING member, so a violation "
    "fixture would have to be a near-copy of `compliant.json` with one resource deleted — a second "
    "500-KB artefact per planted absence, each needing regeneration whenever the real plan changes, "
    "and each a place for the two to drift. `TestResourceInventoryIdentity` instead deletes the "
    "resource from the loaded document in memory and asserts the control fails: a missing action "
    "Lambda, a missing table, an action Lambda no tool declares, an undeclared absence, a stale "
    "declared absence, an exemption naming an unchecked type, and an empty plan. Ten tests, each "
    "asserting a FAILURE.",
}

#: Controls whose subject is the `infra/` source tree rather than a plan document, so no plan
#: fixture can violate them. Each entry names where its non-vacuity proof lives; checked in both
#: directions by `test_the_source_derived_exemption_cannot_become_a_rubber_stamp`.
#: The four relay IAM controls share one entry; their proofs are `TestRelayDeniesCommandExecution`,
#: `TestRelayInvokeScope`, `TestRelayNoForbiddenGrants` and `TestRuntimeResourcePolicySoleInvoker`.
_RELAY_IAM_REASON = (
    'Reads `data "aws_iam_policy_document"` bodies (and, for the resource-policy control, '
    "`aws_bedrockagentcore_resource_policy` blocks) from `infra/**/*.tf` instead of a plan document: "
    "an `aws_iam_policy_document`'s computed `.json` is unknown at plan time whenever it "
    "concatenates a same-apply reference, which these all do, so the plan fixture carries an "
    "unknown document, exactly as `dynamodb_no_wildcard_action` records. "
    "Each control carries its own coverage or count assertion in place of a "
    "`MINIMUM_RESOURCE_COUNTS` floor, so finding nothing is a failure rather than zero findings."
)

_SECURITY_GROUP_REASON = (
    "Reads `aws_security_group` and `aws_vpc_security_group_{in,e}gress_rule` blocks from "
    "`infra/**/*.tf`. The committed fixture predates the VPC (both types are in "
    "FIXTURE_ABSENT_TYPES), so a plan-derived form would report `deferred` and assert nothing. "
    "`check_no_public_ingress` keeps the plan-side view. "
    "Non-vacuity is proved by `TestSecurityGroupSourceControls`, which asserts a FAILURE for each of: an "
    "inline `ingress` block, an ingress rule sourced from `0.0.0.0/0`, an ingress rule with any CIDR "
    'instead of a referenced group, `ip_protocol = "-1"` on egress, an egress port range wider than '
    "443-443, an open CIDR on a port other than 443, an inline `egress` block, and -- the floor that "
    "matters most -- a tree with NO security group and NO egress rule, which must fail rather than pass "
    "over an empty examination. It also asserts the REAL `infra/` tree passes both controls."
)

#: Types declared in `FIXTURE_ABSENT_TYPES` until the compliant fixture is re-captured; empty while
#: the committed fixture is current, together with the gate's entries.
PENDING_RECAPTURE_ABSENT_TYPES: frozenset[str] = frozenset()

#: Types declared in `FIXTURE_LAGGING_COUNTS` until the compliant fixture is re-captured; emptied
#: together with the gate's entries.
PENDING_RECAPTURE_LAGGING_COUNTS: frozenset[str] = frozenset()

#: Grants declared in `FIXTURE_STALE_GRANTS` until the compliant fixture is re-captured; emptied
#: together with the gate's entries.
PENDING_RECAPTURE_STALE_GRANTS: frozenset[str] = frozenset()

#: Grants declared in `FIXTURE_PENDING_GRANTS` until the compliant fixture is re-captured; emptied
#: together with the gate's entries.
PENDING_RECAPTURE_PENDING_GRANTS: frozenset[str] = frozenset()

SOURCE_DERIVED_CONTROLS: dict[str, str] = {
    "no_json_syntax_terraform": "Its subject is the file listing of `infra/`, not a plan, so no plan "
    "fixture can violate it. `TestNoJsonSyntaxTerraform` plants a `.tf.json` in a tmp `infra/` and "
    "asserts a FAILURE naming it, and asserts a FAILURE for a missing `infra/`.",
    "adopted_network_wiring": 'Its subject is `infra/main.tf`\'s `module "vpc"` call and the vpc '
    "module's own source, not a plan: the VPC, subnets and route tables are ADOPTED, so none "
    "of them appears in a plan as a managed resource and a plan fixture could not violate this control at "
    "all. Its internal floor is a `fail` when zero adopted inputs or zero module source are "
    "examined. Non-vacuity is proved by `TestAdoptedNetworkWiring`, which points `INFRA_DIR` at a "
    "scratch copy and asserts a FAILURE for each of: an adopted input (VPC id and CIDR, private and "
    "Runtime subnet and route-table ids) not passed, wired to a "
    'literal, or commented out; no `module "vpc"` call; no vpc module source; and each adopted '
    "resource type declared in the module. It also asserts the real tree and the unmutated copy pass.",
    "declared_infra_block_counts": "Counts declared BLOCKS in `infra/**/*.tf` text, so no plan "
    "fixture can violate it; the fixture-derived `resource_existence_counts` is pinned to the "
    "committed capture. Non-vacuity lives in "
    "`TestDeclaredInfraBlockCounts`: a wrong expected count fails, and reading zero source files "
    "fails rather than passing over an empty count.",
    "s3_access_log_delivery_grant": "Reads `aws_s3_bucket_logging` blocks and their destination "
    "buckets' policy documents from `infra/**/*.tf`. Permanently source-derived, like the Bedrock "
    "entry and unlike `teardown_force_destroy`: both destination policies interpolate their own "
    "bucket's ARN, so the computed `.json` is unknown at plan time however current the fixture is. "
    "Non-vacuity is proved by `TestS3AccessLogDeliveryGrant`: "
    "`test_the_state_the_tree_was_actually_in_fails` asserts a FAILURE for buckets carrying a TLS "
    "deny and nothing else. Also asserted to fail: a Deny of the delivery principal, a "
    "grant of the wrong action, a grant to the wrong principal, a grant naming a different bucket, "
    "and access logging removed entirely (the coverage floor). Asserted to pass: the real tree, and "
    "an `aws_s3_bucket_acl`-based grant, since the control asserts delivery is permitted rather than "
    "which of the two mechanisms is used. Two further tests guard the *reporting*: the CloudFront "
    "half is declared in `PLAN_TIME_INEXPRESSIBLE` rather than covered by the S3 half's pass, and the "
    "origin bucket's `BucketOwnerEnforced` is pinned. A second arm reads `aws_lb` bodies for "
    "`access_logs { enabled = true }` and requires an ELB log-delivery grant (a DIFFERENT service "
    "principal from the S3 one) in the tree; `TestAlbAccessLogDeliveryGrant` proves it: "
    "`test_the_state_the_tree_was_actually_in_fails` asserts a FAILURE for a logs bucket granting "
    "only `logging.s3.amazonaws.com`. Also asserted to fail: a Deny, the wrong action, the legacy per-Region ELB "
    "account id in place of the service principal, and a grant present with no ALB enabling logs "
    "(the standing-permission direction). Asserted not to be demanded: an `aws_lb` with no "
    "`access_logs` block, and one with `enabled = false`.",
    "bedrock_inference_profile_scoping": "Reads the two `BedrockInference`/"
    "`BedrockInferenceFoundationModels` statements' `resources` (and the second's condition) from "
    "`infra/**/*.tf`. The attribute is a `concat()` of `for` "
    "comprehensions, so the document's computed `.json` is unknown at plan time regardless of how "
    "current the fixture is -- the same limit `dynamodb_no_wildcard_action` records. "
    "Non-vacuity is proved by `TestBedrockInferenceProfileScoping`, which asserts a FAILURE for each "
    'of: a bare `"*"`, an ARN ending `:*`, a `"*"` alongside the scoped entries, a dropped '
    "`resources` attribute, an ARN of none of the three shapes, inference-profile ARNs lost, "
    "foundation-model ARNs lost, a foundation-model grant with its bedrock:InferenceProfileArn "
    "condition dropped, a non-literal action list, and a tree with no Bedrock grant at all. It also "
    "asserts the REAL `infra/` tree passes, that a `project/default` ARN is accepted (not a false "
    "positive), and that the neighbouring legitimate `ecr:GetAuthorizationToken` "
    'wildcard and the policy\'s own comment mentioning `Resource = "*"` are not false positives.',
    "security_group_zero_inbound": _SECURITY_GROUP_REASON,
    "security_group_egress_scoped": _SECURITY_GROUP_REASON,
    "relay_denies_command_execution": _RELAY_IAM_REASON,
    "relay_invoke_scoped_to_runtime_and_endpoint": _RELAY_IAM_REASON,
    "relay_no_forbidden_grants": _RELAY_IAM_REASON,
    "runtime_resource_policy_sole_invoker": _RELAY_IAM_REASON,
    "store_principal_deny": "Reads the logs, CloudTrail, frontend and eval-artefacts key and "
    "bucket policy resources, the two root locals and the three module calls from source, not a "
    "plan: every allowlist interpolates same-apply role ARNs, so the rendered `policy` is unknown "
    "at plan time. `TestStorePrincipalDeny` plants each defect in a tmp copy of `infra/` and "
    "asserts a FAILURE on that row only.",
    "dynamodb_data_plane_principal_deny": "Reads the dynamodb module's table and CMK policy "
    'resources, the root data-plane local and the `module "dynamodb"` call from source, not a '
    "plan: both policies interpolate same-apply role ARNs, so the rendered `policy` is unknown at "
    "plan time. `TestDynamodbDataPlanePrincipalDeny` plants each defect in a tmp copy of `infra/` "
    "and asserts a FAILURE on that row only.",
    "runtime_network_mode_vpc": 'Reads the `runtime_network_mode = "VPC"` LITERAL at the '
    '`module "agentcore"` call site in `infra/main.tf`, not a plan: the module renders '
    "network_mode from a variable, and the Runtime subnet is owner-created and adopted by variable so "
    "it is no managed resource a plan fixture could carry. Non-vacuity is proved by "
    "`TestRuntimeVpcModeSourceControls`, which points `INFRA_DIR` at a synthetic tree and asserts a "
    'FAILURE for: network_mode PUBLIC, the subnet/SG wiring absent, and no `module "agentcore"` block '
    "at all; and a PASS for the compliant synthetic tree. Its internal floor is a `fail` when the "
    "block is missing.",
    "runtime_resource_policy_source_vpce_deny": "Reads the two `agent_runtime_*` "
    "`data aws_iam_policy_document` bodies from `infra/main.tf`, because the SourceVpce report-only "
    'Allow and the inbound Deny are `dynamic "statement"` blocks that `policy_document_statements` '
    "cannot see and the toggles gate off by default -- so a plan fixture cannot violate it and the "
    "live account state is outside this gate. Non-vacuity is proved by "
    "`TestRuntimeVpcModeSourceControls`, which asserts a FAILURE for each of: the Deny absent, the "
    "report-only Allow absent, the Deny missing its aws:SourceVpce condition, the Deny missing its "
    "aws:ViaAWSService condition, the Deny not gated on `runtime_source_vpce_deny_enabled`, and both "
    "documents absent (the coverage floor); and a PASS for the compliant synthetic tree.",
    "teardown_force_destroy": "Reads `infra/**/*.tf` source instead of a plan document. "
    'The compliant plan `plan_fixtures.build("compliant")` carries `force_destroy=True` on every '
    "bucket and `force_delete=True` on the ECR repository, but the control checks what a plan "
    "cannot show: that each flag is a literal `true` rather than a resolved conditional, and that "
    "every declared bucket has a `BUCKET_TEARDOWN_DISPOSITION` entry (and every entry a bucket). "
    "Non-vacuity is proved by `TestTeardownForceDestroy`, which points `INFRA_DIR` at "
    "synthetic trees and asserts a failure for each of: a required bucket without the flag, an ECR "
    "repo without it, an unclassified bucket, a forbidden bucket carrying it, a stale map entry, a "
    "conditional (non-literal) value, and an empty tree.",
}


class TestViolationFixturesAreNonVacuous:
    @pytest.mark.parametrize("fixture_name,control_id", sorted(VIOLATION_TARGETS.items()))
    def test_fixture_fails_its_targeted_control(self, fixture_name: str, control_id: str) -> None:
        plan = load_plan(fixture_name)
        findings = gate.run_gate(plan)
        matching = [f for f in findings if f.control == control_id]
        assert matching, f"{fixture_name}: no findings at all for control {control_id!r}"
        assert any(f.status == "fail" for f in matching), (
            f"{fixture_name}: control {control_id!r} did not fail -- assertion is vacuous "
            f"(findings: {matching})"
        )

    #: Every violation fixture's own mutation, as a fragment of the finding detail it produces, so
    #: a fixture cannot keep failing its control for an unrelated (stale-shape) reason alone.
    TARGETED_DETAILS = {
        "violation_api_no_access_logging": "access_log_settings=[]",
        "violation_api_no_throttle": "throttling_burst_limit': None",
        "violation_api_route_unauthenticated": "authorization='NONE'",
        "violation_bedrock_static_credentials": "static IAM access key created",
        "violation_cloudfront_missing_behaviour": "path_patterns={'/api/*'}",
        "violation_cloudfront_no_oac": "'origin_access_control_id': ''",
        "violation_cloudtrail_missing": "no trail present",
        "violation_cognito_lite_tier": "user_pool_tier='LITE'",
        "violation_cognito_no_account_recovery": "{'recovery_mechanism': []}",
        "violation_cognito_no_mfa": "mfa_configuration='OFF'",
        "violation_cognito_weak_password_policy": "'minimum_length': 6,",
        "violation_cognito_write_attributes_scope_leak": "leaked_scope_attrs=['custom:full_visibility']",
        "violation_dynamodb_gsi_parity": "plan_gsis=['gsi_tenant']",
        "violation_dynamodb_leading_keys_on_synthetic_key_table": "LeadingKeys present=True",
        "violation_dynamodb_no_cmk": "server_side_encryption={'enabled': False}",
        "violation_dynamodb_no_pitr": "point_in_time_recovery={'enabled': False}",
        "violation_dynamodb_wildcard_action": "wildcard GRANT",
        "violation_gateway_authorizer_constrains_nothing": "clients_set=False audience_set=False",
        "violation_gateway_no_custom_jwt": "authorizer_type='AWS_IAM'",
        "violation_gateway_no_interceptor": "interceptor_configuration missing",
        "violation_lambda_concurrency_headroom": "sum(reserved_concurrent_executions)=2400",
        "violation_lambda_env_no_kms": "kms_key_arn=None",
        "violation_lambda_gateway_role_cross_account": "principal is a cross-account role ARN",
        "violation_lambda_gateway_role_mismatched_role": "role/some-other-role'",
        "violation_lambda_gateway_role_root_principal": ":root' statement_id=",
        "violation_lambda_gateway_role_wildcard_arn": "role/crm-agent-eval-*' statement_id=",
        "violation_lambda_gateway_role_wildcard_principal": "principal='*' statement_id=",
        "violation_lambda_gateway_role_wrong_statement_id": "statement_id='AllowSomeOtherInvoke'",
        "violation_lambda_shared_role": "role name 'crm-agent-eval-get_account-role' shared with",
        "violation_lambda_wildcard_principal": "principal='s3.amazonaws.com'",
        "violation_lambda_zero_concurrency": "reserved_concurrent_executions=0",
        "violation_log_group_no_kms": "kms_key_id=None",
        "violation_log_group_no_retention": "retention_in_days=0",
        "violation_missing_mandatory_tag": "'cost-center'",
        "violation_relay_function_url_buffered": "invoke_mode='BUFFERED'",
        "violation_resource_existence_counts": "one per entry in `crm.store.schema`'s table registry",
        "violation_s3_bpa_disabled": "'block_public_acls': False",
        "violation_s3_no_cmk": "'sse_algorithm': 'AES256'",
        "violation_s3_no_tls_deny": "no statement denies non-TLS access",
        "violation_s3_tls_deny_actions_narrowed": "actions ['s3:getobject'] cover neither",
        "violation_s3_tls_deny_condition_deleted": "(conditions present: [])",
        "violation_s3_tls_deny_condition_inverted": "('Bool', 'aws:SecureTransport', ('true',))",
        "violation_s3_tls_deny_condition_narrowed": "('StringEquals', 'aws:PrincipalAccount',",
        "violation_s3_tls_deny_decoy_literal_bucket": "name a bucket other than the one this policy",
        "violation_security_group_public_ingress": "'cidr_blocks': ['0.0.0.0/0']",
    }

    @pytest.mark.parametrize("fixture_name,detail_fragment", sorted(TARGETED_DETAILS.items()))
    def test_the_named_mutation_is_what_fails_not_merely_the_control(
        self, fixture_name: str, detail_fragment: str
    ) -> None:
        """The fixture's own mutation, identified by its finding detail, is what fails the control."""
        control_id = VIOLATION_TARGETS[fixture_name]
        findings = gate.run_gate(load_plan(fixture_name))
        matching = [f for f in findings if f.control == control_id and detail_fragment in f.detail]
        assert matching, (
            f"{fixture_name}: {control_id!r} reports no finding whose detail contains "
            f"{detail_fragment!r}, so the mutation this fixture is named for is not what the control "
            f"is reading. Failing details: "
            f"{[f.detail[:120] for f in findings if f.control == control_id and f.status == 'fail'][:4]}"
        )
        assert any(f.status == "fail" for f in matching), matching

    def test_every_targeted_detail_names_a_registered_fixture(self) -> None:
        """The map above cannot drift into naming a fixture the suite does not run."""
        unknown = sorted(set(self.TARGETED_DETAILS) - set(VIOLATION_TARGETS))
        assert not unknown, (
            f"TARGETED_DETAILS names fixture(s) with no VIOLATION_TARGETS entry: {unknown}"
        )

    def test_every_violation_fixture_pins_its_own_mutation(self) -> None:
        """No fixture may fail its control without the detail its own mutation produces."""
        unpinned = sorted(set(VIOLATION_TARGETS) - set(self.TARGETED_DETAILS))
        assert not unpinned, f"VIOLATION_TARGETS fixture(s) with no TARGETED_DETAILS: {unpinned}"

    @pytest.mark.parametrize("fixture_name", sorted(VIOLATION_TARGETS))
    def test_fixture_fails_via_cli_exit_code(self, fixture_name: str) -> None:
        """The CLI exits 1 and lists the targeted control among its `[FAIL]` lines."""
        control_id = VIOLATION_TARGETS[fixture_name]
        result = run_cli("--plan", str(fixture_path(fixture_name)))
        assert result.returncode == 1, (
            f"{fixture_name} did not produce a non-zero exit: {result.stdout}{result.stderr}"
        )
        failed_controls = {
            line.split("]", 1)[1].split(":", 1)[0].strip()
            for line in result.stdout.splitlines()
            if line.startswith("[FAIL]") and "]" in line and ":" in line
        }
        assert control_id in failed_controls, (
            f"{fixture_name} exited 1, but {control_id!r} is not among the failing controls "
            f"({sorted(failed_controls)}) -- the exit code is coming from somewhere else, so this "
            "fixture no longer demonstrates the violation it is named for"
        )

    def test_every_registered_control_has_a_violation_fixture(self) -> None:
        # Every control id the gate can emit, including secondary `also_emits` ids.
        control_ids = set(gate.control_modules())
        tested = set(VIOLATION_TARGETS.values())
        untested = (
            control_ids - tested - set(SOURCE_DERIVED_CONTROLS) - set(IN_MEMORY_MUTATION_CONTROLS)
        )
        assert not untested, f"controls with no violating fixture (non-vacuity gap): {untested}"

    @pytest.mark.parametrize(
        "exemptions,label",
        [
            (SOURCE_DERIVED_CONTROLS, "SOURCE_DERIVED_CONTROLS"),
            (IN_MEMORY_MUTATION_CONTROLS, "IN_MEMORY_MUTATION_CONTROLS"),
        ],
    )
    def test_the_source_derived_exemption_cannot_become_a_rubber_stamp(
        self, exemptions: dict[str, str], label: str
    ) -> None:
        """Every exemption entry names a registered, fixture-untargeted control, a reason and a proof."""
        # Same derivation as above: every id the gate can emit.
        control_ids = set(gate.control_modules())
        fixture_targets = set(VIOLATION_TARGETS.values())
        assert exemptions, f"{label} is empty — this exemption path is now dead, delete it"
        for control_id, reason in exemptions.items():
            assert control_id in control_ids, f"{control_id} is not a registered control"
            assert control_id not in fixture_targets, (
                f"{control_id} has a violating plan fixture after all — remove the exemption"
            )
            assert reason.strip(), control_id

    def test_the_two_exemption_lists_do_not_overlap(self) -> None:
        """A control is either source-derived or plan-derived; claiming both hides which proof holds."""
        overlap = set(SOURCE_DERIVED_CONTROLS) & set(IN_MEMORY_MUTATION_CONTROLS)
        assert not overlap, overlap


# ---------------------------------------------------------------------------
# Runtime VPC-mode and SourceVpce-Deny source controls. Both read `infra/main.tf` rather than a
# plan, so non-vacuity is proved by pointing `INFRA_DIR` at a synthetic tree and asserting a
# FAILURE for each mutation, plus a PASS for the compliant synthetic tree.
# ---------------------------------------------------------------------------


def _agent_runtime_policy_doc(
    name: str,
    resource: str,
    *,
    report_only: bool,
    deny: bool,
    deny_source_vpce: bool = True,
    deny_via_service: bool = True,
    deny_gated: bool = True,
    report_allow_wildcard: bool = False,
) -> str:
    """One `data "aws_iam_policy_document"` mirroring the real agent_runtime_* docs, with knobs."""
    # Every attribute on its own line: the gate's `_hcl_scalar`/`_hcl_string_list` are line-anchored.
    star_principal = (
        '\n      principals {\n        type        = "AWS"\n        identifiers = ["*"]\n      }'
    )
    relay_principal = (
        '\n      principals {\n        type        = "AWS"\n'
        "        identifiers = [module.agent_relay.role_arn]\n      }"
    )
    # The compliant report-only Allow is scoped to the relay role; the wildcard is the failing case.
    report_principal = star_principal if report_allow_wildcard else relay_principal
    report = (
        f"""
  dynamic "statement" {{
    for_each = var.runtime_source_vpce_report_only_enabled ? [1] : []
    content {{
      sid     = "AllowInvokeViaOurVpcEndpoint"
      effect  = "Allow"
      actions = ["bedrock-agentcore:InvokeAgentRuntime"]{report_principal}
      resources = [{resource}]
      condition {{
        test     = "StringEquals"
        variable = "aws:SourceVpce"
        values   = [local.runtime_source_vpce_id]
      }}
    }}
  }}"""
        if report_only
        else ""
    )
    gate_expr = "var.runtime_source_vpce_deny_enabled ? [1] : []" if deny_gated else "[1]"
    vpce_cond = (
        '\n      condition {\n        test     = "StringNotEquals"\n'
        '        variable = "aws:SourceVpce"\n        values   = [local.runtime_source_vpce_id]\n      }'
        if deny_source_vpce
        else ""
    )
    via_cond = (
        '\n      condition {\n        test     = "Bool"\n'
        '        variable = "aws:ViaAWSService"\n        values   = ["false"]\n      }'
        if deny_via_service
        else ""
    )
    deny_block = (
        f"""
  dynamic "statement" {{
    for_each = {gate_expr}
    content {{
      sid     = "DenyInvokeUnlessViaOurVpcEndpoint"
      effect  = "Deny"
      actions = ["bedrock-agentcore:InvokeAgentRuntime"]{star_principal}
      resources = [{resource}]{vpce_cond}{via_cond}
    }}
  }}"""
        if deny
        else ""
    )
    return (
        f'data "aws_iam_policy_document" "{name}" {{\n'
        f"  statement {{\n"
        f'    sid     = "OnlyTheRelayRoleMayInvoke"\n'
        f'    effect  = "Allow"\n'
        f'    actions = ["bedrock-agentcore:InvokeAgentRuntime"]\n'
        f'    principals {{\n      type = "AWS"\n      identifiers = [module.agent_relay.role_arn]\n    }}\n'
        f"    resources = [{resource}]\n"
        f"  }}{report}{deny_block}\n}}\n"
    )


def _write_runtime_vpc_main_tf(
    root: Path,
    *,
    network_mode: str = "VPC",
    wire_network: bool = True,
    report_only: bool = True,
    deny: bool = True,
    **deny_knobs: bool,
) -> None:
    """Write a synthetic `infra/main.tf` with the agentcore block and both runtime policy docs."""
    wiring = (
        "\n  runtime_subnet_ids         = module.vpc.runtime_subnet_ids"
        "\n  runtime_security_group_ids = [module.vpc.runtime_security_group_id]"
        if wire_network
        else ""
    )
    agentcore = (
        f'module "agentcore" {{\n'
        f'  source = "./modules/agentcore"\n'
        f'  runtime_network_mode       = "{network_mode}"{wiring}\n'
        f"}}\n\n"
    )
    docs = (
        _agent_runtime_policy_doc(
            "agent_runtime_relay_only",
            "module.agentcore.agent_runtime_arn",
            report_only=report_only,
            deny=deny,
            **deny_knobs,
        )
        + "\n"
        + _agent_runtime_policy_doc(
            "agent_runtime_endpoint_relay_only",
            "local.agent_runtime_endpoint_arn",
            report_only=report_only,
            deny=deny,
            **deny_knobs,
        )
    )
    (root / "main.tf").write_text(agentcore + docs, encoding="utf-8")


class TestRuntimeVpcModeSourceControls:
    """Non-vacuity for `runtime_network_mode_vpc` and `runtime_resource_policy_source_vpce_deny`."""

    @staticmethod
    def _run(
        tmp_path: Path,
        fn: Callable[[gate.Plan], list[gate.Finding]],
        **kwargs: Any,
    ) -> list[gate.Finding]:
        _write_runtime_vpc_main_tf(tmp_path, **kwargs)
        original = gate.INFRA_DIR
        try:
            gate.INFRA_DIR = tmp_path
            return fn(gate.Plan(resource_changes=[]))
        finally:
            gate.INFRA_DIR = original

    def _failed(self, findings: list[Any]) -> set[str]:
        return {f.resource for f in findings if f.status == "fail"}

    # --- runtime_network_mode_vpc ---
    def test_compliant_tree_passes_network_mode(self, tmp_path: Path) -> None:
        findings = self._run(tmp_path, gate.check_runtime_network_mode_vpc)
        assert findings and not self._failed(findings), findings

    def test_public_network_mode_fails(self, tmp_path: Path) -> None:
        failed = self._failed(
            self._run(tmp_path, gate.check_runtime_network_mode_vpc, network_mode="PUBLIC")
        )
        assert any("runtime_network_mode" in r for r in failed), failed

    def test_unwired_network_fails(self, tmp_path: Path) -> None:
        failed = self._failed(
            self._run(tmp_path, gate.check_runtime_network_mode_vpc, wire_network=False)
        )
        assert any("VPC-mode wiring" in r for r in failed), failed

    def test_missing_agentcore_block_fails(self, tmp_path: Path) -> None:
        (tmp_path / "main.tf").write_text("locals { x = 1 }\n", encoding="utf-8")
        original = gate.INFRA_DIR
        try:
            gate.INFRA_DIR = tmp_path
            findings = gate.check_runtime_network_mode_vpc(gate.Plan(resource_changes=[]))
        finally:
            gate.INFRA_DIR = original
        assert self._failed(findings), findings

    # --- runtime_resource_policy_source_vpce_deny ---
    def test_compliant_tree_passes_deny(self, tmp_path: Path) -> None:
        findings = self._run(tmp_path, gate.check_runtime_resource_policy_source_vpce_deny)
        assert findings and not self._failed(findings), findings

    def test_missing_deny_fails(self, tmp_path: Path) -> None:
        failed = self._failed(
            self._run(tmp_path, gate.check_runtime_resource_policy_source_vpce_deny, deny=False)
        )
        assert failed, "a document with no SourceVpce Deny must fail"

    def test_missing_report_only_allow_fails(self, tmp_path: Path) -> None:
        failed = self._failed(
            self._run(
                tmp_path, gate.check_runtime_resource_policy_source_vpce_deny, report_only=False
            )
        )
        assert any("report-only" in r for r in failed), failed

    def test_deny_without_source_vpce_condition_fails(self, tmp_path: Path) -> None:
        failed = self._failed(
            self._run(
                tmp_path,
                gate.check_runtime_resource_policy_source_vpce_deny,
                deny_source_vpce=False,
            )
        )
        assert failed, "a Deny missing the aws:SourceVpce condition must fail"

    def test_deny_without_via_service_condition_fails(self, tmp_path: Path) -> None:
        failed = self._failed(
            self._run(
                tmp_path,
                gate.check_runtime_resource_policy_source_vpce_deny,
                deny_via_service=False,
            )
        )
        assert failed, "a Deny missing the aws:ViaAWSService condition must fail"

    def test_deny_not_gated_on_the_toggle_fails(self, tmp_path: Path) -> None:
        failed = self._failed(
            self._run(
                tmp_path, gate.check_runtime_resource_policy_source_vpce_deny, deny_gated=False
            )
        )
        assert failed, "a Deny whose for_each does not reference the deny toggle must fail"

    def test_wildcard_report_only_allow_fails(self, tmp_path: Path) -> None:
        """An Allow scoped to Principal "*" with only a network condition fails."""
        findings = self._run(
            tmp_path,
            gate.check_runtime_resource_policy_source_vpce_deny,
            report_allow_wildcard=True,
        )
        failed = self._failed(findings)
        assert any("no wildcard-principal Allow" in r for r in failed), failed

    def test_absent_documents_fail(self, tmp_path: Path) -> None:
        (tmp_path / "main.tf").write_text("locals { x = 1 }\n", encoding="utf-8")
        original = gate.INFRA_DIR
        try:
            gate.INFRA_DIR = tmp_path
            findings = gate.check_runtime_resource_policy_source_vpce_deny(
                gate.Plan(resource_changes=[])
            )
        finally:
            gate.INFRA_DIR = original
        assert self._failed(findings), "documents not found must fail, not pass vacuously"


# ---------------------------------------------------------------------------
# `s3_tls_deny` asserts the TLS condition itself, not only `Sid` and `Effect`. Each test mutates
# the compliant fixture in BOTH plan locations the control reads and asserts the control fails.
# ---------------------------------------------------------------------------

TLS_DENY_CONFIG_ADDRESS = "data.aws_iam_policy_document.logs_tls_deny"
TLS_DENY_DOC_ADDRESS = "module.frontend_cdn.data.aws_iam_policy_document.logs_tls_deny"
TLS_DENY_POLICY_ADDRESS = "module.frontend_cdn.aws_s3_bucket_policy.logs"


TLS_CONDITION_VARIABLE = "aws:SecureTransport"


def _tls_deny_statement_lists(plan_document: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Return the `(configuration, data-source)` statement lists for the logs TLS-deny document."""
    resources = plan_document["configuration"]["root_module"]["module_calls"]["frontend_cdn"][
        "module"
    ]["resources"]
    # Selected by address: the configuration resource list is ordered by Terraform.
    matches = [r for r in resources if r.get("address") == TLS_DENY_CONFIG_ADDRESS]
    assert len(matches) == 1, f"expected exactly one {TLS_DENY_CONFIG_ADDRESS}, got {len(matches)}"
    changes = [r for r in plan_document["resource_changes"] if r["address"] == TLS_DENY_DOC_ADDRESS]
    assert len(changes) == 1
    return (
        matches[0]["expressions"]["statement"],
        changes[0]["change"]["after"]["statement"],
    )


def _tls_deny_statements(plan_document: dict[str, Any]) -> tuple[Any, Any]:
    """Return the one `aws:SecureTransport` statement from each view of the TLS-deny document.

    Located by condition variable rather than list index, since the document holds more than one
    statement in Terraform's order.
    """
    config_list, data_list = _tls_deny_statement_lists(plan_document)

    def _one(statements: list[Any], variable_of: Any) -> Any:
        found = [
            s
            for s in statements
            if any(
                variable_of(c).lower() == TLS_CONDITION_VARIABLE.lower()
                for c in (s.get("condition") or [])
            )
        ]
        assert len(found) == 1, f"expected one {TLS_CONDITION_VARIABLE} statement, got {len(found)}"
        return found[0]

    return (
        _one(config_list, lambda c: str(c["variable"]["constant_value"])),
        _one(data_list, lambda c: str(c["variable"])),
    )


def _tls_deny_findings(plan_document: dict[str, Any]) -> list[gate.Finding]:
    plan = gate.Plan(
        resource_changes=plan_document["resource_changes"],
        configuration=plan_document.get("configuration", {}),
    )
    return gate.check_s3_tls_deny(plan)


def _mutated_compliant_plan() -> dict[str, Any]:
    return plan_fixtures.build("compliant")


class TestS3TlsDenyIsNotVacuous:
    def test_the_unmutated_document_passes(self) -> None:
        findings = _tls_deny_findings(_mutated_compliant_plan())
        assert findings, "control produced no findings at all"
        assert all(f.status == "pass" for f in findings), findings

    def test_an_inverted_condition_fails(self) -> None:
        """`aws:SecureTransport = "true"` denies HTTPS and permits plaintext -- the exact inverse."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["condition"][0]["values"]["constant_value"] = ["true"]
        data_source["condition"][0]["values"] = ["true"]
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "an inverted aws:SecureTransport condition passed the control"
        assert "aws:securetransport=false" in failed[0].detail

    def test_a_deleted_condition_fails(self) -> None:
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config.pop("condition")
        data_source["condition"] = []
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a deleted aws:SecureTransport condition passed the control"

    def test_narrowed_actions_fail(self) -> None:
        """`s3:GetObject` alone leaves plaintext writes and lists permitted."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["actions"]["constant_value"] = ["s3:GetObject"]
        data_source["actions"] = ["s3:GetObject"]
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a deny narrowed to s3:GetObject passed the control"
        assert "actions" in failed[0].detail

    def test_a_renamed_sid_with_an_inverted_condition_fails(self) -> None:
        """Renaming the Sid must not route the mutation past the per-statement reason branch."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["sid"]["constant_value"] = "LooksHarmless"
        data_source["sid"] = "LooksHarmless"
        config["condition"][0]["values"]["constant_value"] = ["true"]
        data_source["condition"][0]["values"] = ["true"]
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a renamed Sid with an inverted condition passed the control"

    def test_a_swapped_condition_operator_fails(self) -> None:
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["condition"][0]["test"]["constant_value"] = "StringEquals"
        data_source["condition"][0]["test"] = "StringEquals"
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a non-Bool condition operator passed the control"

    def test_both_condition_values_fail(self) -> None:
        """`["false", "true"]` denies every request rather than only plaintext ones."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["condition"][0]["values"]["constant_value"] = ["false", "true"]
        data_source["condition"][0]["values"] = ["false", "true"]
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a condition value set wider than ['false'] passed the control"

    def test_resources_narrowed_to_the_bucket_alone_fail(self) -> None:
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["resources"] = {"references": ["aws_s3_bucket.logs.arn", "aws_s3_bucket.logs"]}
        data_source["resources"] = [None]
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a deny covering the bucket but not its objects passed the control"

    def test_a_decoy_deny_on_another_bucket_does_not_satisfy_this_policy(self) -> None:
        """A correct-looking deny scoped to a *different* bucket must not stand in for this one."""
        document = _mutated_compliant_plan()
        config_list, data_list = _tls_deny_statement_lists(document)
        config, data_source = _tls_deny_statements(document)
        decoy_config = copy.deepcopy(config)
        decoy_data_source = copy.deepcopy(data_source)
        decoy_config["resources"] = {
            "references": [
                "aws_s3_bucket.origin.arn",
                "aws_s3_bucket.origin",
            ]
            * 2
        }
        config["condition"][0]["values"]["constant_value"] = ["true"]
        data_source["condition"][0]["values"] = ["true"]
        config_list.append(decoy_config)
        data_list.append(decoy_data_source)
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a decoy deny on another bucket satisfied this bucket's policy"

    def test_a_deny_scoped_to_the_wrong_bucket_fails(self) -> None:
        document = _mutated_compliant_plan()
        config, _data_source = _tls_deny_statements(document)
        config["resources"] = {
            "references": ["aws_s3_bucket.origin.arn", "aws_s3_bucket.origin"] * 2
        }
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a deny scoped to another bucket passed this bucket's policy"
        assert "does not include the bucket this policy attaches to" in failed[0].detail


# ---------------------------------------------------------------------------
# A literal ARN embeds the bucket name, so a deny whose `Resource` entries are literal ARNs of a
# different bucket must fail the same "this bucket" tie as a reference-form resource.
# ---------------------------------------------------------------------------


def _bucket_name(address: str) -> str:
    """Return the planned `bucket` of one `aws_s3_bucket`, read from the fixture."""
    document = plan_fixtures.build("compliant")
    matches = [c for c in document["resource_changes"] if c["address"] == address]
    assert len(matches) == 1, f"expected exactly one {address}, got {len(matches)}"
    name = (matches[0]["change"].get("after") or {}).get("bucket")
    assert isinstance(name, str) and name, f"{address} has no planned bucket name"
    return name


LOGS_BUCKET_NAME = _bucket_name("module.frontend_cdn.aws_s3_bucket.logs")
ORIGIN_BUCKET_NAME = _bucket_name("module.frontend_cdn.aws_s3_bucket.origin")


def _set_literal_resources(document: dict[str, Any], entries: list[str]) -> None:
    config, _data_source = _tls_deny_statements(document)
    config["resources"] = {"constant_value": entries}


class TestS3TlsDenyResourceEntryCounting:
    """The Resource entry count separates a deny covering `bucket` + `bucket/*` from one covering
    only the bucket.

    The count comes from the `data_source.after` view, whose list keeps one element per entry, and
    not from `resource_reference_count`: Terraform records deduplicated reference targets, so
    `"${X}/*"` and `X` are the same target.
    """

    def test_a_single_entry_deny_fails(self) -> None:
        """A single-entry deny referencing a resource attribute fails."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["resources"] = {"references": ["aws_s3_bucket.logs.arn", "aws_s3_bucket.logs"]}
        data_source["resources"] = [None]
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, (
            "a deny scoping ONE resource entry passed; its objects are reachable in plaintext"
        )
        assert any("fewer than the" in f.detail for f in failed), [f.detail for f in failed]

    def test_the_reference_count_is_no_longer_consulted_as_an_entry_count(self) -> None:
        """A reference set is not read as an entry count: the configuration view reports
        `resource_entries` as None."""
        stmt = gate._statement_from_terraform(
            {
                "sid": gate.TLS_DENY_SID,
                "resource_references": ["aws_s3_bucket.x.arn", "aws_s3_bucket.x"],
            },
            origin="configuration",
        )
        assert stmt.resource_entries is None, (
            "the configuration view cannot express an entry count and must report None rather than "
            "a reference-set size"
        )

    def test_an_uncountable_entry_set_defers_rather_than_passing_silently(self) -> None:
        """When no view can count the entries, the `s3_tls_deny` detail says so instead of passing."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["resources"] = {"references": ["local.bucket_arn", "local.bucket_arn"]}
        data_source.pop("resources", None)
        for change in document["resource_changes"]:
            if change["type"] == "aws_s3_bucket_policy":
                change["change"]["after"].pop("policy", None)
                change["change"].setdefault("after_unknown", {})["policy"] = True
        findings = _tls_deny_findings(document)
        # The count is a witness in `s3_tls_deny`'s own detail, not a separate registered control.
        primary = [f for f in findings if f.control == "s3_tls_deny"]
        assert primary, "no s3_tls_deny finding was emitted at all"
        assert any("Resource entries NOT counted" in f.detail for f in primary), [
            (f.status, f.detail[:120]) for f in primary
        ]
        assert any("only the corroborating count is missing" in f.detail for f in primary)

    def test_a_local_reference_is_not_read_as_a_foreign_bucket(self) -> None:
        """A statement scoping `local.bucket_arn` is not read as naming a foreign bucket."""
        document = _mutated_compliant_plan()
        config, _ = _tls_deny_statements(document)
        config["resources"] = {"references": ["local.bucket_arn", "local.bucket_arn"]}
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert not any("does not include the bucket" in f.detail for f in failed), failed

    def test_a_genuinely_foreign_resource_reference_still_fails(self) -> None:
        """A resource reference to another bucket still fails despite the local-indirection allowance."""
        document = _mutated_compliant_plan()
        config, _ = _tls_deny_statements(document)
        config["resources"] = {
            "references": ["aws_s3_bucket.someone_elses.arn", "aws_s3_bucket.someone_elses"]
        }
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert any("does not include the bucket" in f.detail for f in failed), failed


class TestS3TlsDenyLiteralResourceScope:
    def test_literal_arns_of_another_bucket_fail(self) -> None:
        """A correct-looking deny with literal ARNs of the wrong bucket fails."""
        document = _mutated_compliant_plan()
        _set_literal_resources(
            document,
            [f"arn:aws:s3:::{ORIGIN_BUCKET_NAME}", f"arn:aws:s3:::{ORIGIN_BUCKET_NAME}/*"],
        )
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "literal ARNs of a different bucket satisfied this bucket's policy"
        assert "name a bucket other than the one this policy attaches to" in failed[0].detail

    def test_literal_arns_of_this_bucket_pass(self) -> None:
        """Control case: the same literal form naming the right bucket must not false-fail."""
        document = _mutated_compliant_plan()
        _set_literal_resources(
            document, [f"arn:aws:s3:::{LOGS_BUCKET_NAME}", f"arn:aws:s3:::{LOGS_BUCKET_NAME}/*"]
        )
        findings = _tls_deny_findings(document)
        logs = [f for f in findings if f.resource == TLS_DENY_POLICY_ADDRESS]
        assert logs and all(f.status == "pass" for f in logs), logs

    def test_one_correct_and_one_foreign_literal_entry_fails(self) -> None:
        """A decoy entry added alongside the right one is still a scope this gate has not verified."""
        document = _mutated_compliant_plan()
        _set_literal_resources(
            document,
            [
                f"arn:aws:s3:::{LOGS_BUCKET_NAME}",
                f"arn:aws:s3:::{LOGS_BUCKET_NAME}/*",
                f"arn:aws:s3:::{ORIGIN_BUCKET_NAME}/*",
            ],
        )
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a foreign resource entry alongside the correct pair passed the control"

    def test_non_arn_resource_entries_fail(self) -> None:
        """`["*", "*/*"]` names no bucket, so the statement's scope cannot be tied to this one."""
        document = _mutated_compliant_plan()
        _set_literal_resources(document, ["*", "*/*"])
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "non-ARN resource entries passed the bucket cross-check"
        assert "not S3 resource ARNs" in failed[0].detail

    def test_a_literal_arn_of_an_unrelated_partition_still_compares_the_bucket(self) -> None:
        """Changing the partition/region segments must not route the bucket comparison past."""
        document = _mutated_compliant_plan()
        _set_literal_resources(
            document,
            [
                f"arn:aws-cn:s3:cn-north-1:123456789012:{ORIGIN_BUCKET_NAME}",
                f"arn:aws-cn:s3:cn-north-1:123456789012:{ORIGIN_BUCKET_NAME}/*",
            ],
        )
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a foreign bucket in another partition passed the cross-check"

    def test_literal_arns_with_no_knowable_bucket_name_fail(self) -> None:
        """When the policy's own bucket name is not plan-time knowable, literal scope fails closed."""
        document = _mutated_compliant_plan()
        _set_literal_resources(
            document, [f"arn:aws:s3:::{LOGS_BUCKET_NAME}", f"arn:aws:s3:::{LOGS_BUCKET_NAME}/*"]
        )
        for change in document["resource_changes"]:
            if change["type"] == "aws_s3_bucket":
                change["change"]["after"].pop("bucket", None)
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "literal ARNs with no knowable bucket name passed the cross-check"
        assert "no literal bucket name is knowable" in failed[0].detail

    def test_a_wildcard_bucket_segment_matching_this_bucket_passes(self) -> None:
        """Documented boundary: a glob covering this bucket is accepted, since it does deny it."""
        document = _mutated_compliant_plan()
        _set_literal_resources(
            document, ["arn:aws:s3:::crm-agent-evalfrontend-cdn-logs-*", "arn:aws:s3:::*-logs-*/*"]
        )
        logs = [f for f in _tls_deny_findings(document) if f.resource == TLS_DENY_POLICY_ADDRESS]
        assert logs and all(f.status == "pass" for f in logs), logs

    def test_an_uppercased_condition_key_still_passes(self) -> None:
        """IAM condition keys are case-insensitive; the control must not false-fail on casing."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["condition"][0]["variable"]["constant_value"] = "AWS:SecureTransport"
        data_source["condition"][0]["variable"] = "AWS:SecureTransport"
        assert not [f for f in _tls_deny_findings(document) if f.status == "fail"]

    def test_the_enumerated_minimum_action_set_still_passes(self) -> None:
        """An enumerated object-and-bucket action set is a legitimate alternative to `s3:*`."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        config["actions"]["constant_value"] = actions
        data_source["actions"] = actions
        assert not [f for f in _tls_deny_findings(document) if f.status == "fail"]

    def test_a_rendered_policy_document_is_asserted_the_same_way(self) -> None:
        """The known-`after.policy` branch must apply the same assertions, not weaker ones."""
        document = _mutated_compliant_plan()
        for resource in document["resource_changes"]:
            if resource["address"] != TLS_DENY_POLICY_ADDRESS:
                continue
            resource["change"]["after"]["policy"] = json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": gate.TLS_DENY_SID,
                            "Effect": "Deny",
                            "Principal": {"AWS": "*"},
                            "Action": "s3:*",
                            "Resource": ["arn:aws:s3:::b", "arn:aws:s3:::b/*"],
                            "Condition": {"Bool": {"aws:SecureTransport": "true"}},
                        }
                    ],
                }
            )
        failed = [
            f
            for f in _tls_deny_findings(document)
            if f.status == "fail" and f.resource == TLS_DENY_POLICY_ADDRESS
        ]
        assert failed, "an inverted condition in a rendered policy document passed the control"
        assert "after.policy" in failed[0].detail


# ---------------------------------------------------------------------------
# A non-TLS deny must reach every caller: its only tolerated condition shape is a restatement of the
# TLS test (an allowlist, so an unrecognised condition key fails closed), and its principal must be
# the wildcard with no `NotPrincipal` exclusion.
# ---------------------------------------------------------------------------

#: The one tolerated condition shape.
TLS_CONDITION_DATA_SOURCE = {
    "test": "Bool",
    "variable": "aws:SecureTransport",
    "values": ["false"],
}

#: Narrowing condition shapes, each `(label, {test, variable, values})`; every one must be rejected.
#: `123456789012` is the AWS-documentation placeholder account id.
NARROWING_CONDITION_SHAPES: list[tuple[str, dict[str, Any]]] = [
    (
        "principal_account",
        {"test": "StringEquals", "variable": "aws:PrincipalAccount", "values": ["123456789012"]},
    ),
    ("source_ip", {"test": "IpAddress", "variable": "aws:SourceIp", "values": ["10.0.0.0/8"]}),
    (
        "principal_arn",
        {
            "test": "ArnEquals",
            "variable": "aws:PrincipalArn",
            "values": ["arn:aws:iam::123456789012:role/anyone"],
        },
    ),
    ("string_like_prefix", {"test": "StringLike", "variable": "s3:prefix", "values": ["logs/*"]}),
    (
        "source_vpce",
        {
            "test": "StringEquals",
            "variable": "aws:SourceVpce",
            "values": ["vpce-0123456789abcdef0"],
        },
    ),
    (
        "for_any_value_qualifier",
        {
            "test": "ForAnyValue:StringEquals",
            "variable": "aws:PrincipalOrgPaths",
            "values": ["o-example/r-root/ou-example/"],
        },
    ),
    (
        "user_id",
        {"test": "StringEquals", "variable": "aws:userid", "values": ["AIDAEXAMPLEUSERID"]},
    ),
    (
        "inverted_operator",
        {"test": "NotIpAddress", "variable": "aws:SourceIp", "values": ["10.0.0.0/8"]},
    ),
    # A condition whose every field is a plan reference flattens to an empty block and is rejected.
    ("every_field_a_reference", {}),
]

#: Condition shapes that restate the TLS test and so must NOT false-fail. IAM condition keys and
#: `Bool` values are case-insensitive, and a duplicated identical test narrows nothing.
HARMLESS_CONDITION_SHAPES: list[tuple[str, dict[str, Any]]] = [
    ("duplicate_identical_test", dict(TLS_CONDITION_DATA_SOURCE)),
    (
        "case_varied_restatement",
        {"test": "BOOL", "variable": "AWS:SECURETRANSPORT", "values": ["False"]},
    ),
]


def _as_config_condition(shape: dict[str, Any]) -> dict[str, Any]:
    """Re-encode a condition shape in the `configuration` view's `constant_value` wrapping."""
    return {key: {"constant_value": value} for key, value in shape.items()}


#: Number of `DenyInsecureTransport` statements `_append_condition_to_every_tls_deny` must reach.
#: Pinned rather than derived from the mutating walk, so the count proves the mutation reached
#: every site. Basis: 5 in `configuration` (frontend_cdn's logs and origin documents,
#: observability's cloudtrail_bucket and cloudtrail_access_logs documents, eval_artifacts' bucket
#: document) plus the same 5 in `resource_changes`.
TLS_DENY_STATEMENT_SITES = 10


def _append_condition_to_every_tls_deny(document: dict[str, Any], shape: dict[str, Any]) -> int:
    """Append `shape` to every `DenyInsecureTransport` statement in BOTH terraform views.

    Returns the number of statements mutated.
    """
    mutated = 0
    for change in document["resource_changes"]:
        if change.get("type") != "aws_iam_policy_document":
            continue
        for statement in change["change"]["after"].get("statement") or []:
            if statement.get("sid") == gate.TLS_DENY_SID:
                statement["condition"].append(copy.deepcopy(shape))
                mutated += 1

    def walk(module: dict[str, Any]) -> None:
        nonlocal mutated
        for resource in module.get("resources", []):
            for statement in (resource.get("expressions") or {}).get("statement") or []:
                if not isinstance(statement, dict):
                    continue
                sid = statement.get("sid") or {}
                if sid.get("constant_value") == gate.TLS_DENY_SID:
                    statement["condition"].append(_as_config_condition(shape))
                    mutated += 1
        for call in (module.get("module_calls") or {}).values():
            walk(call.get("module") or {})

    walk(document["configuration"]["root_module"])
    return mutated


def _rendered_tls_deny_policy(**overrides: Any) -> str:
    """A minimal rendered bucket-policy document carrying one otherwise-valid non-TLS deny."""
    statement: dict[str, Any] = {
        "Sid": gate.TLS_DENY_SID,
        "Effect": "Deny",
        "Principal": {"AWS": "*"},
        "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{LOGS_BUCKET_NAME}", f"arn:aws:s3:::{LOGS_BUCKET_NAME}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }
    statement.update(overrides)
    return json.dumps({"Version": "2012-10-17", "Statement": [statement]})


def _findings_for_rendered_policy(policy: str) -> list[gate.Finding]:
    document = _mutated_compliant_plan()
    for resource in document["resource_changes"]:
        if resource["address"] == TLS_DENY_POLICY_ADDRESS:
            resource["change"]["after"]["policy"] = policy
    return [f for f in _tls_deny_findings(document) if f.resource == TLS_DENY_POLICY_ADDRESS]


class TestS3TlsDenyMustReachEveryCaller:
    def test_the_registered_narrowed_fixture_fails_in_both_terraform_views(self) -> None:
        """The registered narrowed-deny violation fixture fails in both terraform views."""
        findings = gate.run_gate(load_plan("violation_s3_tls_deny_condition_narrowed"))
        failed = [f for f in findings if f.control == "s3_tls_deny" and f.status == "fail"]
        assert failed, "the narrowed-deny fixture passed s3_tls_deny"
        for finding in failed:
            assert "aws:PrincipalAccount" in finding.detail, finding.detail
            assert "configuration:" in finding.detail, finding.detail
            assert "data_source.after:" in finding.detail, finding.detail

    @pytest.mark.parametrize(
        "label,shape",
        NARROWING_CONDITION_SHAPES,
        ids=[label for label, _ in NARROWING_CONDITION_SHAPES],
    )
    def test_a_narrowing_condition_alongside_the_tls_test_fails(
        self, label: str, shape: dict[str, Any]
    ) -> None:
        document = _mutated_compliant_plan()
        assert _append_condition_to_every_tls_deny(document, shape) == TLS_DENY_STATEMENT_SITES
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, f"a deny narrowed by {label} passed the control"
        assert "does not apply to every caller" in failed[0].detail, failed[0].detail

    def test_a_qualifier_on_the_tls_test_itself_fails(self) -> None:
        """`ForAnyValue:Bool` is not the `Bool` operator, so it is not the tolerated shape."""
        document = _mutated_compliant_plan()
        config, data_source = _tls_deny_statements(document)
        config["condition"][0]["test"]["constant_value"] = "ForAnyValue:Bool"
        data_source["condition"][0]["test"] = "ForAnyValue:Bool"
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a ForAnyValue: qualifier on the TLS test passed the control"

    @pytest.mark.parametrize(
        "label,shape",
        HARMLESS_CONDITION_SHAPES,
        ids=[label for label, _ in HARMLESS_CONDITION_SHAPES],
    )
    def test_a_restatement_of_the_tls_test_does_not_false_fail(
        self, label: str, shape: dict[str, Any]
    ) -> None:
        """The allowlist tolerates a restatement, so hardening does not break a valid policy."""
        document = _mutated_compliant_plan()
        assert _append_condition_to_every_tls_deny(document, shape) == TLS_DENY_STATEMENT_SITES
        assert not [f for f in _tls_deny_findings(document) if f.status == "fail"], label

    def test_the_rendered_policy_view_rejects_the_same_narrowing(self) -> None:
        """The rendered-policy view rejects the same narrowing as the two terraform views."""
        failed = [
            f
            for f in _findings_for_rendered_policy(
                _rendered_tls_deny_policy(
                    Condition={
                        "Bool": {"aws:SecureTransport": "false"},
                        "StringEquals": {"aws:PrincipalAccount": "123456789012"},
                    }
                )
            )
            if f.status == "fail"
        ]
        assert failed, "a narrowed deny in a rendered policy document passed the control"
        assert "after.policy" in failed[0].detail, failed[0].detail
        assert "aws:PrincipalAccount" in failed[0].detail, failed[0].detail

    def test_the_rendered_policy_view_accepts_the_unnarrowed_deny(self) -> None:
        """Control case for the rendered view: the same document, unnarrowed, must pass."""
        findings = _findings_for_rendered_policy(_rendered_tls_deny_policy())
        assert findings and all(f.status == "pass" for f in findings), findings

    def test_a_deny_narrowed_to_a_named_principal_fails(self) -> None:
        """Principal narrowing reaches the same end as condition narrowing, and is rejected too."""
        failed = [
            f
            for f in _findings_for_rendered_policy(
                _rendered_tls_deny_policy(
                    Principal={"AWS": "arn:aws:iam::123456789012:role/anyone"}
                )
            )
            if f.status == "fail"
        ]
        assert failed, "a deny naming one principal passed the control"
        assert "permitted for every other caller" in failed[0].detail, failed[0].detail

    def test_a_deny_excluding_a_principal_fails(self) -> None:
        failed = [
            f
            for f in _findings_for_rendered_policy(
                _rendered_tls_deny_policy(
                    NotPrincipal={"AWS": "arn:aws:iam::123456789012:role/anyone"}
                )
            )
            if f.status == "fail"
        ]
        assert failed, "a deny excluding a principal via NotPrincipal passed the control"
        assert "NotPrincipal" in failed[0].detail, failed[0].detail

    def test_a_second_variable_under_the_same_operator_fails(self) -> None:
        """A rendered document nests variables under one operator; the extra one still narrows."""
        failed = [
            f
            for f in _findings_for_rendered_policy(
                _rendered_tls_deny_policy(
                    Condition={
                        "Bool": {
                            "aws:SecureTransport": "false",
                            "aws:MultiFactorAuthPresent": "true",
                        }
                    }
                )
            )
            if f.status == "fail"
        ]
        assert failed, "a second variable under the same operator passed the control"
        assert "aws:MultiFactorAuthPresent" in failed[0].detail, failed[0].detail

    def test_a_wildcard_under_a_service_principal_type_fails(self) -> None:
        """`{"Service": "*"}` wildcards only AWS services, so it does not reach every caller."""
        failed = [
            f
            for f in _findings_for_rendered_policy(
                _rendered_tls_deny_policy(Principal={"Service": "*"})
            )
            if f.status == "fail"
        ]
        assert failed, "a wildcard under the Service principal type passed the control"
        assert "rather than every caller" in failed[0].detail, failed[0].detail

    def test_the_untyped_bare_wildcard_principal_passes(self) -> None:
        """`Principal: "*"` is the untyped equivalent of `{"AWS": "*"}` and must not false-fail."""
        findings = _findings_for_rendered_policy(_rendered_tls_deny_policy(Principal="*"))
        assert findings and all(f.status == "pass" for f in findings), findings

    def test_a_deny_stating_no_principal_fails_closed(self) -> None:
        document = _mutated_compliant_plan()
        for change in document["resource_changes"]:
            if change.get("type") != "aws_iam_policy_document":
                continue
            for statement in change["change"]["after"].get("statement") or []:
                if statement.get("sid") == gate.TLS_DENY_SID:
                    statement.pop("principals", None)
        failed = [f for f in _tls_deny_findings(document) if f.status == "fail"]
        assert failed, "a deny stating no principal at all passed the control"
        assert "states no principal" in failed[0].detail, failed[0].detail

    def test_the_pre_fix_presence_only_logic_would_have_passed_the_fixture(self) -> None:
        """The presence-only `any condition matches` rule accepts every TLS deny in the fixture.

        This shows the narrowed fixture really reproduces the narrowing rather than failing for an
        unrelated reason.
        """
        plan = load_plan("violation_s3_tls_deny_condition_narrowed")
        statements: list[gate.PolicyStatement] = []
        for resource in plan.by_type("aws_s3_bucket_policy"):
            address = resource["address"]
            for raw in (plan.static_policy_statements_for(address) or []) + (
                plan.data_source_statements_for(address) or []
            ):
                statement = gate._statement_from_terraform(raw, origin="test")
                if statement.sid == gate.TLS_DENY_SID:
                    statements.append(statement)
        assert len(statements) == 8, [s.sid for s in statements]
        for statement in statements:
            assert any(gate._is_tls_deny_condition(c) for c in statement.conditions), statement
            assert len(statement.conditions) > 1, statement


# ---------------------------------------------------------------------------
# --only <module> reproduces a single per-module check.
# ---------------------------------------------------------------------------


class TestOnlyFlag:
    @pytest.mark.parametrize("module", sorted({c.module for c in gate.CONTROLS}))
    def test_scopes_to_a_single_module(self, module: str) -> None:
        plan = load_plan("compliant")
        findings = gate.run_gate(plan, only=module)
        assert findings, f"--only {module} produced no findings"
        modules_of_controls = gate.control_modules()
        for f in findings:
            assert f.control in modules_of_controls, (
                f"{f.control} is emitted but registered under no ControlSpec -- add it to the "
                "owning spec's also_emits"
            )
            assert modules_of_controls[f.control] == module

    def test_via_cli(self) -> None:
        result = run_cli("--plan", str(fixture_path("compliant")), "--only", "dynamodb", "--json")
        assert_clean_run(result)
        payload = json.loads(result.stdout)
        assert payload["checks"], "no checks emitted for --only dynamodb"
        assert all(c["control"].startswith("dynamodb") for c in payload["checks"]), payload[
            "checks"
        ]


# ---------------------------------------------------------------------------
# --self-check: every baseline control must resolve to an assertion or a recorded
# plan-time-inexpressible reason.
# ---------------------------------------------------------------------------


class TestSelfCheck:
    def test_passes_with_full_registry(self) -> None:
        findings = gate.run_self_check()
        failed = [f for f in findings if f.status == "fail"]
        assert not failed, f"self-check found uncovered security-baseline rows: {failed}"

    def test_passes_via_cli_exit_code(self) -> None:
        result = run_cli("--self-check")
        assert_clean_run(result)

    def test_catches_a_removed_control(self) -> None:
        """Removing a live control fails the self-check."""
        original_controls = gate.CONTROLS
        try:
            gate.CONTROLS = [c for c in original_controls if c.control_id != "dynamodb_pitr"]
            findings = gate.run_self_check()
            failed = {f.resource for f in findings if f.status == "fail"}
            assert any(row.startswith("DynamoDB PITR |") for row in failed)
        finally:
            gate.CONTROLS = original_controls

    def test_catches_a_removed_inexpressible_reason(self) -> None:
        """Removing a recorded inexpressible-reason entry fails the self-check."""
        original = gate.PLAN_TIME_INEXPRESSIBLE
        try:
            gate.PLAN_TIME_INEXPRESSIBLE = [
                e for e in original if e["spec_row"] != "Secrets: no vendor API key anywhere, ever"
            ]
            findings = gate.run_self_check()
            failed = {f.resource for f in findings if f.status == "fail"}
            assert any("Secrets" in row for row in failed)
        finally:
            gate.PLAN_TIME_INEXPRESSIBLE = original

    def test_inexpressible_list_has_no_duplicate_spec_rows(self) -> None:
        rows = [e["spec_row"] for e in gate.PLAN_TIME_INEXPRESSIBLE]
        assert len(rows) == len(set(rows)), "duplicate spec_row entries in PLAN_TIME_INEXPRESSIBLE"

    def test_inexpressible_list_every_entry_has_reason_and_layer(self) -> None:
        for entry in gate.PLAN_TIME_INEXPRESSIBLE:
            assert entry.get("spec_row"), entry
            assert entry.get("reason"), entry
            assert entry.get("layer"), entry

    def test_the_logs_kms_row_is_asserted_by_the_log_group_kms_control(self) -> None:
        """The baseline's Logs KMS row passes only while `log_group_kms` is registered."""

        def kms_row(findings: list[Any]) -> Any:
            rows = [f for f in findings if f.resource.startswith("Logs |") and "KMS" in f.resource]
            assert len(rows) == 1, [f.resource for f in findings]
            return rows[0]

        assert kms_row(gate.run_self_check()).status == "pass"
        original_controls = gate.CONTROLS
        try:
            gate.CONTROLS = [c for c in original_controls if c.control_id != "log_group_kms"]
            assert kms_row(gate.run_self_check()).status == "fail"
        finally:
            gate.CONTROLS = original_controls


class TestLogGroupKms:
    """Every planned log group has a `kms_key_id`: a known non-empty value, or unknown at plan."""

    ADDRESS = "module.vpc.aws_cloudwatch_log_group.flow_logs[0]"

    @staticmethod
    def _plan_with_kms(after: dict[str, Any], after_unknown: dict[str, Any]) -> gate.Plan:
        return gate.Plan(
            resource_changes=[
                {
                    "address": "aws_cloudwatch_log_group.subject",
                    "type": "aws_cloudwatch_log_group",
                    "change": {
                        "actions": ["create"],
                        "after": after,
                        "after_unknown": after_unknown,
                    },
                }
            ]
        )

    def test_every_compliant_log_group_passes(self) -> None:
        plan = load_plan("compliant")
        findings = gate.check_log_group_kms(plan)
        assert len(findings) == len(plan.by_type("aws_cloudwatch_log_group")) > 0
        assert all(f.control == "log_group_kms" for f in findings)
        failed = [f for f in findings if f.status != "pass"]
        assert not failed, failed

    def test_the_violation_fixture_fails_naming_only_the_mutated_log_group(self) -> None:
        findings = gate.check_log_group_kms(load_plan("violation_log_group_no_kms"))
        assert [f.resource for f in findings if f.status == "fail"] == [self.ADDRESS]

    @pytest.mark.parametrize(
        "after,after_unknown",
        [
            ({"kms_key_id": None}, {}),
            ({}, {}),
            ({"kms_key_id": ""}, {}),
            ({"kms_key_id": "   "}, {"kms_key_id": False}),
            ({"kms_key_id": None}, {"kms_key_id": False}),
        ],
        ids=["null", "absent", "empty", "blank", "null-and-known"],
    )
    def test_a_null_absent_or_empty_key_fails(
        self, after: dict[str, Any], after_unknown: dict[str, Any]
    ) -> None:
        findings = gate.check_log_group_kms(self._plan_with_kms(after, after_unknown))
        assert [f.status for f in findings] == ["fail"]

    @pytest.mark.parametrize(
        "after,after_unknown",
        [
            ({}, {"kms_key_id": True}),
            ({"kms_key_id": "arn:aws:kms:us-east-1:123456789012:key/example"}, {}),
        ],
        ids=["unknown-at-plan", "known-arn"],
    )
    def test_an_unknown_or_known_key_passes(
        self, after: dict[str, Any], after_unknown: dict[str, Any]
    ) -> None:
        findings = gate.check_log_group_kms(self._plan_with_kms(after, after_unknown))
        assert [f.status for f in findings] == ["pass"]


# ---------------------------------------------------------------------------
# --json output shape
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_shape(self) -> None:
        result = run_cli("--plan", str(fixture_path("compliant")), "--json")
        payload = json.loads(result.stdout)
        assert set(payload.keys()) == {
            "checks",
            "total",
            "failed",
            "deferred",
            "deferred_by_control",
        }
        assert payload["failed"] == 0
        assert payload["total"] == len(payload["checks"])


# ---------------------------------------------------------------------------
# Removing every resource of a type family a control reads must fail rather than leave that
# control emitting zero findings.
# ---------------------------------------------------------------------------


def _plan_without_types(prefix: str) -> gate.Plan:
    plan = load_plan("compliant")
    return gate.Plan(
        resource_changes=[
            r for r in plan.resource_changes if not str(r.get("type", "")).startswith(prefix)
        ],
        configuration=plan.configuration,
    )


class TestResourceRemovalIsNotVacuous:
    """Executed removals, one per resource-type family a control reads."""

    @pytest.mark.parametrize(
        "prefix",
        [
            "aws_s3_",
            "aws_cloudfront",
            "aws_cloudwatch_log_group",
            "aws_kms_key",
            "aws_api_gateway_stage",
            "aws_api_gateway_method",
            "aws_iam_role",
            "aws_lambda_permission",
            "aws_lambda_function",
            "aws_dynamodb_table",
            "aws_cognito_user_pool",
            "aws_bedrockagentcore_gateway",
            "aws_cloudtrail",
        ],
    )
    def test_removing_a_resource_family_fails_the_gate(self, prefix: str) -> None:
        findings = gate.run_gate(_plan_without_types(prefix))
        failed = [f for f in findings if f.status == "fail"]
        assert failed, (
            f"removing every {prefix}* resource left the gate with zero failures — the "
            "per-attribute controls that read it are vacuous against its absence"
        )
        assert any(f.control == "resource_existence_counts" for f in failed), (
            f"the existence floor did not fire for {prefix}*: {sorted({f.control for f in failed})}"
        )


class TestControlSubjectFloors:
    """The meta-assertion: every control's subject type has a floor, not just the named ones."""

    def test_every_control_subject_type_has_a_floor_or_a_recorded_reason(self) -> None:
        findings = gate.check_control_subject_floors()
        assert findings, "no control subject types were derived — the AST derivation is vacuous"
        failed = [f for f in findings if f.status == "fail"]
        assert not failed, f"control subject types with no existence floor: {failed}"

    def test_subject_types_are_derived_from_source_not_hand_listed(self) -> None:
        subjects = gate.subject_resource_types()
        # Spot-check three controls whose subject type is unambiguous in the source.
        assert subjects["s3_block_public_access"] == ("aws_s3_bucket_public_access_block",)
        assert subjects["log_group_retention"] == ("aws_cloudwatch_log_group",)
        assert "aws_cloudfront_distribution" in subjects["cloudfront_oac"]

    def test_removing_a_floor_fails_the_meta_check(self) -> None:
        """The coverage check rejects a subject type with no floor."""
        original = gate.EXPECTED_RESOURCE_COUNTS
        try:
            gate.EXPECTED_RESOURCE_COUNTS = {
                k: v for k, v in original.items() if k != "aws_s3_bucket_public_access_block"
            }
            findings = gate.check_control_subject_floors()
            failed = {f.resource for f in findings if f.status == "fail"}
            assert any("aws_s3_bucket_public_access_block" in row for row in failed), failed
        finally:
            gate.EXPECTED_RESOURCE_COUNTS = original

    def test_an_absence_asserted_type_is_not_silently_exempt(self) -> None:
        """Every absence exemption carries a stated reason, so it cannot be a bare exclusion."""
        assert gate.ABSENCE_ASSERTED_TYPES
        for resource_type, reason in gate.ABSENCE_ASSERTED_TYPES.items():
            assert reason.strip(), resource_type
            assert resource_type not in gate.EXPECTED_RESOURCE_COUNTS, (
                f"{resource_type} is both expected-with-a-count and absence-asserted"
            )

    def test_a_control_with_no_subject_type_carries_a_stated_reason(self) -> None:
        """The empty-subject-set path is enumerated, not silent."""
        empty = {c for c, types in gate.subject_resource_types().items() if not types}
        assert empty, "no control derives an empty subject set — this exemption path is now dead"
        assert set(gate.NO_SUBJECT_TYPE_CONTROLS) == empty, (
            "NO_SUBJECT_TYPE_CONTROLS must enumerate exactly the controls deriving no subject type"
        )
        for control_id, reason in gate.NO_SUBJECT_TYPE_CONTROLS.items():
            assert reason.strip(), control_id

    def test_an_unreasoned_empty_subject_control_fails(self) -> None:
        """Dropping a control's stated reason fails the meta-check."""
        original = gate.NO_SUBJECT_TYPE_CONTROLS
        dropped = next(iter(sorted(original)))
        try:
            gate.NO_SUBJECT_TYPE_CONTROLS = {k: v for k, v in original.items() if k != dropped}
            failed = {f.resource for f in gate.check_control_subject_floors() if f.status == "fail"}
            assert any(dropped in row for row in failed), failed
        finally:
            gate.NO_SUBJECT_TYPE_CONTROLS = original

    def test_a_stale_no_subject_type_entry_fails(self) -> None:
        """The other direction: an entry for a control that does derive types must fail."""
        original = gate.NO_SUBJECT_TYPE_CONTROLS
        try:
            gate.NO_SUBJECT_TYPE_CONTROLS = {
                **original,
                "dynamodb_cmk": "bogus — this control derives aws_dynamodb_table",
            }
            failed = {f.resource for f in gate.check_control_subject_floors() if f.status == "fail"}
            assert any("dynamodb_cmk" in row for row in failed), failed
        finally:
            gate.NO_SUBJECT_TYPE_CONTROLS = original


# ---------------------------------------------------------------------------
# A compliant fixture stale against `infra/` — resource types declared in the source but absent
# from the plan — must be detected rather than passing.
# ---------------------------------------------------------------------------


class TestFixtureCurrency:
    def test_no_undeclared_divergence_between_infra_and_the_fixture(self) -> None:
        findings = gate.check_fixture_currency()
        failed = [f for f in findings if f.status == "fail"]
        assert not failed, (
            "a resource type is declared in infra/ but missing from the fixture with no recorded "
            f"reason, or a recorded reason is stale: {failed}"
        )

    def test_declared_types_are_read_from_real_hcl(self) -> None:
        declared = gate.declared_infra_resource_types()
        assert "aws_dynamodb_table" in declared
        assert "aws_cloudfront_distribution" in declared
        assert len(declared) > 20, declared

    #: A resource type no plan of this project will ever contain, used to plant a divergence.
    PLANTED_TYPE = "aws_quicksight_theme"

    def _planted_tree(self, root: Path, *relative_dirs: str) -> Path:
        infra = root / "infra"
        for relative in relative_dirs:
            directory = infra / relative
            directory.mkdir(parents=True)
            (directory / "main.tf").write_text(
                f'resource "{self.PLANTED_TYPE}" "planted" {{\n}}\n', encoding="utf-8"
            )
        return infra

    def test_a_type_declared_only_in_a_separate_root_is_not_counted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A type declared only under a `SEPARATE_ROOT_DIRS` root is outside the stack's types."""
        assert gate.SEPARATE_ROOT_DIRS, "no separate root is declared, so this proves nothing"
        infra = self._planted_tree(tmp_path, gate.SEPARATE_ROOT_DIRS[0])
        monkeypatch.setattr(gate, "INFRA_DIR", infra)
        assert self.PLANTED_TYPE not in gate.declared_infra_resource_types()

    def test_the_same_type_in_a_stack_module_is_still_counted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Negative control: the exclusion is by directory, not by type."""
        infra = self._planted_tree(tmp_path, gate.SEPARATE_ROOT_DIRS[0], "modules/planted")
        monkeypatch.setattr(gate, "INFRA_DIR", infra)
        assert self.PLANTED_TYPE in gate.declared_infra_resource_types()

    def test_every_separate_root_entry_is_a_real_terraform_root(self) -> None:
        """A stale `SEPARATE_ROOT_DIRS` entry would silently exempt nothing; it fails instead."""
        for name in gate.SEPARATE_ROOT_DIRS:
            directory = gate.INFRA_DIR / name
            assert directory.is_dir(), (
                f"SEPARATE_ROOT_DIRS names {name!r}, which is not under infra/"
            )
            assert sorted(directory.glob("*.tf")), f"infra/{name}/ holds no .tf file"

    def test_an_unreasoned_missing_type_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A planted type declared in infra/ and absent from the fixture, with no reason, fails."""
        monkeypatch.setattr(gate, "FIXTURE_ABSENT_TYPES", {})
        # Declared in infra/ (per the patched reader) but absent from the fixture.
        monkeypatch.setattr(
            gate, "declared_infra_resource_types", lambda: frozenset({self.PLANTED_TYPE})
        )
        failed = [f for f in gate.check_fixture_currency() if f.status == "fail"]
        assert any(self.PLANTED_TYPE in f.resource for f in failed), (
            f"a type declared in infra/ and missing from the fixture with no recorded reason must "
            f"fail the currency check; got {failed}"
        )

    def test_a_reason_excuses_a_missing_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The other half of the same mechanism: a recorded reason turns that failure into a pass."""
        monkeypatch.setattr(
            gate, "declared_infra_resource_types", lambda: frozenset({self.PLANTED_TYPE})
        )
        monkeypatch.setattr(
            gate, "FIXTURE_ABSENT_TYPES", {self.PLANTED_TYPE: "planted, with a recorded reason"}
        )
        findings = gate.check_fixture_currency()
        assert not [f for f in findings if f.status == "fail"], findings
        assert any(self.PLANTED_TYPE in f.resource and f.status == "pass" for f in findings), (
            f"the reasoned absence produced no passing finding: {findings}"
        )

    def test_a_stale_allow_list_entry_fails(self) -> None:
        """The other direction: an entry for a type that is present (or gone) must fail."""
        original = gate.FIXTURE_ABSENT_TYPES
        try:
            gate.FIXTURE_ABSENT_TYPES = {
                **original,
                "aws_dynamodb_table": "bogus — this type IS in the fixture",
            }
            findings = gate.check_fixture_currency()
            failed = {f.resource for f in findings if f.status == "fail"}
            assert any("aws_dynamodb_table" in row for row in failed), failed
        finally:
            gate.FIXTURE_ABSENT_TYPES = original

    def test_fixture_records_its_own_provenance(self) -> None:
        fixture = plan_fixtures.build("compliant")
        provenance = fixture.get("_fixture_provenance")
        assert isinstance(provenance, dict), "the fixture carries no provenance block"
        assert provenance.get("captured_infra_digest")
        assert "captured_from_commit" not in provenance
        assert provenance.get("captured_at")

    def test_the_basis_shows_the_asserted_fields_and_names_the_rest_without_echoing_them(
        self,
    ) -> None:
        """The `[PASS]` basis names descriptive fixture fields without echoing their content."""
        fixture = plan_fixtures.build("compliant")
        provenance = dict(fixture["_fixture_provenance"])
        provenance["some_note"] = "SENTINEL-must-not-be-republished"
        basis = gate._provenance_basis(provenance)

        assert "SENTINEL-must-not-be-republished" not in basis, (
            f"an unasserted provenance field's CONTENT reached the passing evidence: {basis}"
        )
        assert "some_note" in basis, "the suppressed field is not even named as suppressed"
        for field in gate._ASSERTED_PROVENANCE_FIELDS:
            assert field in basis, f"the basis dropped {field!r}, which the gate does assert"

    def test_the_capture_time_variables_are_recorded_because_counts_depend_on_them(self) -> None:
        """The fixture records `variables_at_capture`, on which several expected counts depend."""
        fixture = plan_fixtures.build("compliant")
        recorded = fixture["_fixture_provenance"]["variables_at_capture"]
        assert "vpc_nat_gateway_enabled" in recorded, recorded
        assert "variables_at_capture" in gate._ASSERTED_PROVENANCE_FIELDS, (
            "the field is no longer asserted by the gate, so a recapture could omit it silently"
        )

    def test_a_provenance_block_that_is_not_a_dict_is_still_rendered(self) -> None:
        assert "None" in gate._provenance_basis(None)


# ---------------------------------------------------------------------------
# Deferrals reach both renderers, and the per-control deferred tally is pinned so a control
# regressing to all-deferred is visible.


class TestDeferredAccounting:
    """The per-control deferred tally (`_deferred_tally.EXPECTED_DEFERRED_BY_CONTROL`) is pinned."""

    @staticmethod
    def _pending_recapture_deferred_types() -> set[str]:
        """Pending-recapture types that are counted yet absent from the committed fixture."""
        changes = load_plan("compliant").resource_changes
        present = {r.get("type") for r in changes}
        absent = (
            set(PENDING_RECAPTURE_ABSENT_TYPES) & set(gate.EXPECTED_RESOURCE_COUNTS)
        ) - present
        lagging = {
            t
            for t in PENDING_RECAPTURE_LAGGING_COUNTS
            if sum(1 for r in changes if r.get("type") == t) == gate.FIXTURE_LAGGING_COUNTS[t][0]
        }
        return absent | lagging

    def _expected(self) -> dict[str, int]:
        expected = dict(EXPECTED_DEFERRED_BY_CONTROL)
        pending = len(self._pending_recapture_deferred_types())
        if pending:
            expected["resource_existence_counts"] = pending
        return expected

    def test_per_control_deferred_tally_is_pinned(self) -> None:
        findings = gate.run_gate(load_plan("compliant"))
        assert gate.deferred_counts_by_control(findings) == self._expected(), (
            gate.deferred_counts_by_control(findings)
        )

    def test_text_renderer_reports_the_deferred_count(self) -> None:
        findings = gate.run_gate(load_plan("compliant"))
        summary = gate.render_text(findings).splitlines()[-1]
        assert "deferred" in summary
        assert f"{sum(self._expected().values())} deferred" in summary

    def test_json_renderer_reports_the_deferred_count(self) -> None:
        findings = gate.run_gate(load_plan("compliant"))
        payload = json.loads(gate.render_json(findings))
        assert payload["deferred"] == sum(self._expected().values())
        assert payload["deferred_by_control"] == self._expected()

    def test_every_expected_count_is_exercised_by_the_committed_fixture(self) -> None:
        """No existence count defers: every expected type is present in the re-captured fixture."""
        findings = gate.run_gate(load_plan("compliant"))
        deferred = {
            f.resource
            for f in findings
            if f.control == "resource_existence_counts" and f.status == "deferred"
        }
        assert deferred == self._pending_recapture_deferred_types()

    def test_a_control_regressing_to_all_deferred_is_visible(self) -> None:
        """The deferred tally changes when a control's dispositions change."""
        findings = gate.run_gate(load_plan("compliant"))
        mutated = [
            gate.Finding(f.control, f.resource, "deferred", f.detail)
            if f.control == "dynamodb_pitr"
            else f
            for f in findings
        ]
        counts = gate.deferred_counts_by_control(mutated)
        assert counts != self._expected()
        assert counts.get("dynamodb_pitr", 0) > 0


# ---------------------------------------------------------------------------
# Known-ARN gateway-role-invoke exemption. It has two plan-time-knowledge branches and both must
# pass and be reached: `compliant` is planned from zero (every principal unknown), and
# `compliant_gateway_role_known_arn` carries the known-literal branch.
# ---------------------------------------------------------------------------


class TestGatewayRoleKnownArnExemption:
    #: Which fixture carries which plan-time-knowledge state of the gateway role's ARN; each branch
    #: is asserted reached by the distinct detail it emits.
    KNOWLEDGE_STATES = {
        "compliant": "principal unknown at plan time",
        "compliant_gateway_role_known_arn": "known literal that exactly matches the plan's own",
    }

    @pytest.mark.parametrize("fixture_name,detail_fragment", sorted(KNOWLEDGE_STATES.items()))
    def test_both_gateway_role_knowledge_states_pass(
        self, fixture_name: str, detail_fragment: str
    ) -> None:
        findings = gate.run_gate(load_plan(fixture_name))
        failed = [f for f in findings if f.status == "fail"]
        assert not failed, f"{fixture_name} unexpectedly failed: {failed}"
        reached = [
            f
            for f in findings
            if f.control == "lambda_resource_policy_no_wildcard"
            and "gateway_target_role" in f.resource
            and detail_fragment in f.detail
        ]
        assert reached, (
            f"{fixture_name}: no gateway_target_role finding mentions {detail_fragment!r}, so this "
            "fixture is not in the knowledge state it is registered for"
        )

    @pytest.mark.parametrize("fixture_name", sorted(KNOWLEDGE_STATES))
    def test_both_gateway_role_knowledge_states_pass_via_cli(self, fixture_name: str) -> None:
        assert_clean_run(run_cli("--plan", str(fixture_path(fixture_name))))

    @pytest.mark.parametrize(
        "fixture_name,expected_detail_fragment",
        [
            ("violation_lambda_gateway_role_wildcard_principal", "wildcard character"),
            ("violation_lambda_gateway_role_wildcard_arn", "wildcard character"),
            ("violation_lambda_gateway_role_root_principal", "account-root principal"),
            ("violation_lambda_gateway_role_cross_account", "cross-account role ARN"),
            (
                "violation_lambda_gateway_role_mismatched_role",
                "does not match the plan's own gateway role ARN",
            ),
        ],
    )
    def test_known_arn_violation_fails_for_its_specific_reason(
        self, fixture_name: str, expected_detail_fragment: str
    ) -> None:
        plan = load_plan(fixture_name)
        findings = gate.run_gate(plan)
        matching = [
            f
            for f in findings
            if f.control == "lambda_resource_policy_no_wildcard" and f.status == "fail"
        ]
        assert matching, f"{fixture_name}: expected a failing finding, found none"
        assert any(expected_detail_fragment in f.detail for f in matching), (
            f"{fixture_name}: no failing finding mentions {expected_detail_fragment!r} "
            f"(details: {[f.detail for f in matching]})"
        )

    def test_wrong_statement_id_with_otherwise_valid_gateway_arn_still_fails(self) -> None:
        """A valid gateway role ARN on a different statement id is not exempted."""
        plan = load_plan("violation_lambda_gateway_role_wrong_statement_id")
        findings = gate.run_gate(plan)
        matching = [
            f
            for f in findings
            if f.control == "lambda_resource_policy_no_wildcard" and f.status == "fail"
        ]
        assert matching, "expected a failing finding for the wrong statement id"
        assert any("AllowSomeOtherInvoke" in f.detail for f in matching), [
            f.detail for f in matching
        ]

    def test_fails_closed_when_gateway_role_arn_cannot_be_resolved(self) -> None:
        """A known-literal principal on the exempt statement fails when the plan's gateway role ARN
        cannot be resolved; uses the known-literal fixture, where principals are literals."""
        plan = load_plan("compliant_gateway_role_known_arn")
        original_by_type = plan.by_type

        def by_type_without_gateway_role(resource_type: str) -> list[dict[str, object]]:
            results = original_by_type(resource_type)
            if resource_type == "aws_iam_role":
                return [r for r in results if r.get("name") != "agentcore_gateway"]
            return results

        plan.by_type = by_type_without_gateway_role  # type: ignore[method-assign]
        findings = gate.run_gate(plan)
        matching = [
            f
            for f in findings
            if f.control == "lambda_resource_policy_no_wildcard" and f.status == "fail"
        ]
        assert matching, "expected fail-closed behaviour when the gateway role can't be resolved"
        assert any("failing closed" in f.detail for f in matching), [f.detail for f in matching]


# ---------------------------------------------------------------------------
# The known-ARN exemption binds to the root-module `aws_iam_role.agentcore_gateway` plan address,
# not to any resource locally named `agentcore_gateway`.
# ---------------------------------------------------------------------------


class TestGatewayRoleKnownArnExemptionBindsToAddressNotName:
    def test_duplicate_role_in_an_unrelated_module_is_not_accepted(self) -> None:
        """A second `agentcore_gateway` role in an unrelated module, with every gateway-target
        permission repointed at its ARN, fails."""
        plan = load_plan("violation_lambda_gateway_role_duplicate_named_role")
        findings = gate.run_gate(plan)
        matching = [
            f
            for f in findings
            if f.control == "lambda_resource_policy_no_wildcard" and f.status == "fail"
        ]
        assert matching, "expected every gateway_target_role permission to fail"
        assert len(matching) == 24, [f.resource for f in matching]

    def test_relocating_the_real_role_into_a_module_is_not_accepted(self) -> None:
        """Relocating the real role into `module.unrelated` and repointing its ARN fails closed."""
        plan = load_plan("violation_lambda_gateway_role_relocated_to_module")
        findings = gate.run_gate(plan)
        matching = [
            f
            for f in findings
            if f.control == "lambda_resource_policy_no_wildcard" and f.status == "fail"
        ]
        assert matching, "expected every gateway_target_role permission to fail"
        assert len(matching) == 24, [f.resource for f in matching]
        assert any("failing closed" in f.detail for f in matching), [f.detail for f in matching]

    def test_pre_fix_name_only_logic_would_have_passed_both_fixtures(self) -> None:
        """A name-only `_gateway_role_arns` reports 0 failures on both fixtures the address-bound
        form rejects."""

        def old_gateway_role_arns(plan: gate.Plan) -> set[str]:
            arns: set[str] = set()
            for r in plan.by_type("aws_iam_role"):
                if r.get("name") != "agentcore_gateway":
                    continue
                after_unknown = plan.after_unknown(r)
                arn = plan.after(r).get("arn")
                if isinstance(arn, str) and not gate.is_unknown(after_unknown.get("arn")):
                    arns.add(arn)
            return arns

        for fixture_name in (
            "violation_lambda_gateway_role_duplicate_named_role",
            "violation_lambda_gateway_role_relocated_to_module",
        ):
            plan = load_plan(fixture_name)
            old_arns = old_gateway_role_arns(plan)
            principals = {
                r["change"]["after"]["principal"]
                for r in plan.by_type("aws_lambda_permission")
                if r["change"]["after"].get("statement_id") == "AllowGatewayTargetRoleInvoke"
            }
            assert principals, fixture_name
            assert principals <= old_arns, (
                f"{fixture_name}: pre-fix logic would NOT have accepted every principal "
                f"(principals={principals}, old_arns={old_arns}) -- fixture does not "
                "reproduce the reported defect"
            )

    def test_unknown_principal_exemption_still_works_unchanged(self) -> None:
        """The unknown-principal branch (role not yet created) still passes."""
        plan = load_plan("compliant")
        findings = gate.run_gate(plan)
        matching = [f for f in findings if f.control == "lambda_resource_policy_no_wildcard"]
        assert matching, "no findings for lambda_resource_policy_no_wildcard against compliant"
        assert all(f.status == "pass" for f in matching), [
            f for f in matching if f.status == "fail"
        ]


class TestCognitoMfaAcceptsOptionalButNotAHollowOne:
    """`cognito_mfa` accepts `"OPTIONAL"` but fails when no second factor is reachable.

    The failing shapes are `"OFF"`, `""`, an absent `mfa_configuration`, and `"OPTIONAL"` with the
    software-token factor disabled or absent.
    """

    @staticmethod
    def _findings(
        configuration: str | None, *, software_token_enabled: bool | None = True
    ) -> list[gate.Finding]:
        document = _mutated_compliant_plan()
        for resource in document["resource_changes"]:
            if resource["type"] != "aws_cognito_user_pool":
                continue
            after = resource["change"]["after"]
            if configuration is None:
                after.pop("mfa_configuration", None)
            else:
                after["mfa_configuration"] = configuration
            if software_token_enabled is None:
                after.pop("software_token_mfa_configuration", None)
            else:
                after["software_token_mfa_configuration"] = [{"enabled": software_token_enabled}]
        plan = gate.Plan(
            resource_changes=document["resource_changes"],
            configuration=document.get("configuration", {}),
        )
        findings = gate.check_cognito_mfa(plan)
        assert findings, "control produced no findings at all"
        return findings

    @pytest.mark.parametrize("configuration", ["ON", "OPTIONAL"])
    def test_a_factor_bearing_configuration_passes(self, configuration: str) -> None:
        assert all(f.status == "pass" for f in self._findings(configuration))

    @pytest.mark.parametrize("configuration", ["OFF", "", None])
    def test_no_enforcement_fails(self, configuration: str | None) -> None:
        assert all(f.status == "fail" for f in self._findings(configuration))

    def test_optional_with_the_software_token_factor_disabled_fails(self) -> None:
        """No user can enroll a factor, so `"OPTIONAL"` challenges nobody -- a hollow pass."""
        findings = self._findings("OPTIONAL", software_token_enabled=False)
        assert all(f.status == "fail" for f in findings), findings

    def test_optional_with_no_factor_block_at_all_fails(self) -> None:
        """A pool with no software-token factor block fails."""
        findings = self._findings("OPTIONAL", software_token_enabled=None)
        assert all(f.status == "fail" for f in findings), findings

    def test_the_detail_names_both_asserted_values(self) -> None:
        """A `fail` must say which of the two halves was wrong, not just that the control failed."""
        (finding,) = self._findings("OPTIONAL", software_token_enabled=False)
        assert "mfa_configuration='OPTIONAL'" in finding.detail
        assert "software_token_enabled=False" in finding.detail


_ABSENT = object()


def _expected_admin_only_disposition() -> str:
    """Return the disposition `cognito_admin_only_signup` must report on the committed fixture,
    derived from the fixture's pool value."""
    (pool,) = load_plan("compliant").by_type("aws_cognito_user_pool")
    value = (gate.Plan.after(pool).get("admin_create_user_config") or [{}])[0].get(
        "allow_admin_create_user_only"
    )
    return "pass" if value is True else "fail"


class TestCognitoAdminOnlySignup:
    """`cognito_admin_only_signup` passes only on a known `true`."""

    @staticmethod
    def _findings(
        value: object,
        *,
        unknown: bool = False,
        stale: bool = False,
        source_declares: bool = True,
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[gate.Finding]:
        """Run the control over the compliant plan with the pool's block set to `value`.

        `_ABSENT` removes the block from `after`; `unknown` marks it in `after_unknown`. Staleness and
        the source predicate are pinned so the disposition does not depend on the working tree.
        """
        document = _mutated_compliant_plan()
        for resource in document["resource_changes"]:
            if resource["type"] != "aws_cognito_user_pool":
                continue
            change = resource["change"]
            if value is _ABSENT:
                change["after"].pop("admin_create_user_config", None)
            else:
                change["after"]["admin_create_user_config"] = [
                    {"allow_admin_create_user_only": value, "invite_message_template": []}
                ]
            if unknown:
                change.setdefault("after_unknown", {})["admin_create_user_config"] = True
            else:
                change.setdefault("after_unknown", {}).pop("admin_create_user_config", None)
        monkeypatch.setattr(gate, "plan_is_stale_against_source", lambda: stale)
        monkeypatch.setattr(gate, "source_declares_admin_only_signup", lambda: source_declares)
        plan = gate.Plan(resource_changes=document["resource_changes"])
        findings = gate.check_cognito_admin_only_signup(plan)
        assert findings, "control produced no findings at all"
        return findings

    @pytest.mark.parametrize("stale", [False, True])
    def test_true_passes(self, stale: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        findings = self._findings(True, stale=stale, monkeypatch=monkeypatch)
        assert all(f.status == "pass" for f in findings), findings

    def test_true_fails_against_a_stale_plan_when_source_lacks_the_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `true` in a stale plan fails when the source no longer declares the literal block."""
        findings = self._findings(True, stale=True, source_declares=False, monkeypatch=monkeypatch)
        assert all(f.status == "fail" for f in findings), findings
        assert "plan is stale" in findings[0].detail

    def test_true_against_a_current_plan_does_not_consult_the_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A current plan was captured from today's source, so the plan value is authoritative."""
        findings = self._findings(True, stale=False, source_declares=False, monkeypatch=monkeypatch)
        assert all(f.status == "pass" for f in findings), findings

    @pytest.mark.parametrize("stale", [False, True])
    def test_false_fails_even_while_stale(
        self, stale: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        findings = self._findings(False, stale=stale, monkeypatch=monkeypatch)
        assert all(f.status == "fail" for f in findings), findings
        assert "allow_admin_create_user_only=False" in findings[0].detail

    @pytest.mark.parametrize(
        "value,unknown",
        [(_ABSENT, False), (_ABSENT, True), (None, False), (None, True)],
        ids=["block-absent", "block-unknown", "value-null", "value-null-unknown"],
    )
    def test_absent_or_unknown_fails_against_a_current_plan(
        self, value: object, unknown: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        findings = self._findings(value, unknown=unknown, monkeypatch=monkeypatch)
        assert all(f.status == "fail" for f in findings), findings

    @pytest.mark.parametrize("unknown", [False, True])
    def test_absent_fails_against_a_stale_plan_when_source_lacks_the_literal(
        self, unknown: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting the source line must turn the stale-fixture deferral back into a `fail`."""
        findings = self._findings(
            _ABSENT, unknown=unknown, stale=True, source_declares=False, monkeypatch=monkeypatch
        )
        assert all(f.status == "fail" for f in findings), findings

    def test_absent_defers_only_when_stale_and_source_declares_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        findings = self._findings(_ABSENT, unknown=True, stale=True, monkeypatch=monkeypatch)
        assert all(f.status == "deferred" for f in findings), findings
        assert "stale plan predates the source fix" in findings[0].detail

    def test_the_real_source_declares_the_literal(self) -> None:
        assert gate.source_declares_admin_only_signup()

    _STATIC_BLOCK = "  admin_create_user_config {\n    allow_admin_create_user_only = true\n  }\n"

    @pytest.mark.parametrize(
        "pool_body,declared",
        [
            ('  name = "p"\n', False),
            (_STATIC_BLOCK, True),
            (
                '  dynamic "admin_create_user_config" {\n    for_each = []\n    content {\n'
                "      allow_admin_create_user_only = true\n    }\n  }\n",
                False,
            ),
            (
                "  # admin_create_user_config {\n  #   allow_admin_create_user_only = true\n  # }\n",
                False,
            ),
            (
                "  admin_create_user_config {\n    # allow_admin_create_user_only = true\n  }\n",
                False,
            ),
            ("  /*\n" + _STATIC_BLOCK + "  */\n", False),
            (
                "  admin_create_user_config {\n    allow_admin_create_user_only = false\n  }\n",
                False,
            ),
            ("  # allow_admin_create_user_only = true\n", False),
            ('  description = "allow_admin_create_user_only = true"\n', False),
            (
                "  admin_create_user_config {\n    invite_message_template {\n"
                "      allow_admin_create_user_only = true\n    }\n  }\n",
                False,
            ),
        ],
        ids=[
            "no-block",
            "static-block",
            "dynamic-block",
            "commented-out-block",
            "commented-out-literal",
            "block-comment",
            "literal-false",
            "comment-elsewhere",
            "string-elsewhere",
            "nested-too-deep",
        ],
    )
    def test_the_source_predicate_requires_a_static_literal_block(
        self, pool_body: str, declared: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a static block with a literal `true` argument satisfies the source predicate."""
        (tmp_path / "main.tf").write_text(
            'resource "aws_cognito_user_pool" "this" {\n' + pool_body + "}\n", encoding="utf-8"
        )
        monkeypatch.setattr(gate, "INFRA_DIR", tmp_path)
        assert gate.source_declares_admin_only_signup() is declared

    def test_the_literal_in_another_resource_does_not_satisfy_the_pool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "main.tf").write_text(
            'resource "aws_cognito_user_pool" "this" {\n  name = "p"\n}\n'
            'resource "aws_other_thing" "x" {\n' + self._STATIC_BLOCK + "}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(gate, "INFRA_DIR", tmp_path)
        assert not gate.source_declares_admin_only_signup()

    def test_every_pool_must_declare_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "main.tf").write_text(
            'resource "aws_cognito_user_pool" "a" {\n' + self._STATIC_BLOCK + "}\n"
            'resource "aws_cognito_user_pool" "b" {\n  name = "p"\n}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(gate, "INFRA_DIR", tmp_path)
        assert not gate.source_declares_admin_only_signup()

    @pytest.mark.parametrize("pool_body", ["dynamic", "commented"])
    def test_a_stale_plan_fails_through_the_real_check_on_a_no_op_source(
        self, pool_body: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale plan fails through the real check when the source block is a no-op shape."""
        body = (
            '  dynamic "admin_create_user_config" {\n    for_each = []\n    content {\n'
            "      allow_admin_create_user_only = true\n    }\n  }\n"
            if pool_body == "dynamic"
            else "  # admin_create_user_config {\n  #   allow_admin_create_user_only = true\n  # }\n"
        )
        (tmp_path / "main.tf").write_text(
            'resource "aws_cognito_user_pool" "this" {\n' + body + "}\n", encoding="utf-8"
        )
        monkeypatch.setattr(gate, "INFRA_DIR", tmp_path)
        monkeypatch.setattr(gate, "plan_is_stale_against_source", lambda: True)
        for value in (True, _ABSENT):
            document = _mutated_compliant_plan()
            for resource in document["resource_changes"]:
                if resource["type"] == "aws_cognito_user_pool":
                    if value is _ABSENT:
                        resource["change"]["after"].pop("admin_create_user_config", None)
                    else:
                        resource["change"]["after"]["admin_create_user_config"] = [
                            {"allow_admin_create_user_only": True}
                        ]
            findings = gate.check_cognito_admin_only_signup(
                gate.Plan(resource_changes=document["resource_changes"])
            )
            assert findings and all(f.status == "fail" for f in findings), (value, findings)

    def test_an_infra_tree_with_no_pool_is_not_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gate, "INFRA_DIR", tmp_path)
        assert not gate.source_declares_admin_only_signup()

    def test_the_committed_fixture_passes_or_defers_and_never_fails(self) -> None:
        """The committed fixture passes or defers, as derived from its own pool value and
        freshness, and never fails."""
        findings = gate.check_cognito_admin_only_signup(load_plan("compliant"))
        assert len(findings) == 1, findings
        assert findings[0].status == _expected_admin_only_disposition(), findings


class TestNoJsonSyntaxTerraform:
    """`no_json_syntax_terraform`: the `*.tf` readers see all of `infra/`."""

    @staticmethod
    def _findings(infra_dir: Path, monkeypatch: pytest.MonkeyPatch) -> list[gate.Finding]:
        monkeypatch.setattr(gate, "INFRA_DIR", infra_dir)
        findings = gate.check_no_json_syntax_terraform(gate.Plan(resource_changes=[]))
        assert findings, "control produced no findings at all"
        return findings

    def test_the_real_tree_passes(self) -> None:
        findings = gate.check_no_json_syntax_terraform(gate.Plan(resource_changes=[]))
        assert [f.status for f in findings] == ["pass"], findings

    def test_an_hcl_only_tree_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control for the planted cases below: the same tree without the `.tf.json` passes."""
        (tmp_path / "modules" / "m").mkdir(parents=True)
        (tmp_path / "main.tf").write_text("", encoding="utf-8")
        (tmp_path / "modules" / "m" / "main.tf").write_text("", encoding="utf-8")
        assert [f.status for f in self._findings(tmp_path, monkeypatch)] == ["pass"]

    @pytest.mark.parametrize("relative", ["x.tf.json", "modules/m/override.tf.json"])
    def test_a_planted_tf_json_fails_and_is_named(
        self, relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "main.tf").write_text("", encoding="utf-8")
        planted = tmp_path / relative
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("{}", encoding="utf-8")
        findings = self._findings(tmp_path, monkeypatch)
        assert [f.status for f in findings] == ["fail"], findings
        assert findings[0].resource == f"infra/{relative}"

    def test_every_planted_file_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("a.tf.json", "b.tf.json"):
            (tmp_path / name).write_text("{}", encoding="utf-8")
        findings = self._findings(tmp_path, monkeypatch)
        assert sorted(f.resource for f in findings) == ["infra/a.tf.json", "infra/b.tf.json"]
        assert all(f.status == "fail" for f in findings), findings

    def test_a_tf_json_under_dot_terraform_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cached = tmp_path / ".terraform" / "modules" / "cached.tf.json"
        cached.parent.mkdir(parents=True)
        cached.write_text("{}", encoding="utf-8")
        assert [f.status for f in self._findings(tmp_path, monkeypatch)] == ["pass"]

    def test_a_missing_infra_dir_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        findings = self._findings(tmp_path / "absent", monkeypatch)
        assert [f.status for f in findings] == ["fail"], findings

    def test_unregistering_it_reds_the_self_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert not [f for f in gate.run_self_check() if f.status == "fail"]
        monkeypatch.setattr(
            gate,
            "CONTROLS",
            [c for c in gate.CONTROLS if c.control_id != "no_json_syntax_terraform"],
        )
        failed = [f for f in gate.run_self_check() if f.status == "fail"]
        assert failed, "removing the control left the self-check green"
        assert any("tf.json" in f.resource for f in failed), failed


class TestAdoptedNetworkWiring:
    """Every failure direction of `adopted_network_wiring`, against a scratch copy of its sources."""

    #: The only `infra/` files the control reads, relative to `infra/`; nothing else is copied.
    _SOURCES = ("main.tf", "modules/vpc/*.tf")

    #: Inputs of `module "vpc"` that must be passed as `var.` references.
    _ADOPTED_INPUTS = (
        "vpc_id",
        "vpc_cidr_block",
        "private_subnet_ids",
        "private_route_table_ids",
        "runtime_subnet_ids",
        "runtime_route_table_ids",
    )

    #: Adopted resource types the vpc module must not declare.
    _MUST_NOT_CREATE = (
        "aws_default_route_table",
        "aws_default_security_group",
        "aws_internet_gateway",
        "aws_route_table",
        "aws_route_table_association",
        "aws_subnet",
        "aws_vpc",
    )

    @classmethod
    def _scratch_infra(cls, tmp_path: Path) -> Path:
        """Copy the control's source files from the real `infra/` into `tmp_path/infra`."""
        infra = tmp_path / "infra"
        for pattern in cls._SOURCES:
            matches = sorted(gate.INFRA_DIR.glob(pattern))
            assert matches, f"infra/{pattern} matched nothing"
            for source in matches:
                target = infra / source.relative_to(gate.INFRA_DIR)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        return infra

    @staticmethod
    def _findings(infra: Path, monkeypatch: pytest.MonkeyPatch) -> list[gate.Finding]:
        monkeypatch.setattr(gate, "INFRA_DIR", infra)
        findings = gate.check_adopted_network_wiring(gate.Plan(resource_changes=[]))
        assert findings, "control produced no findings at all"
        return findings

    @staticmethod
    def _rewrite(path: Path, pattern: str, replacement: str) -> None:
        """Apply one regex substitution to `path`, asserting it changed exactly one place."""
        text, count = re.subn(pattern, replacement, path.read_text(encoding="utf-8"), flags=re.M)
        assert count == 1, f"{pattern!r} matched {count} times in {path}; the mutation is vacuous"
        path.write_text(text, encoding="utf-8")

    @classmethod
    def _rewrite_vpc_call(cls, infra: Path, pattern: str, replacement: str) -> None:
        """Apply one regex substitution inside `main.tf`'s `module "vpc"` block only."""
        main_tf = infra / "main.tf"
        text = main_tf.read_text(encoding="utf-8")
        call = re.search(r'(?ms)^module\s+"vpc"\s*\{.*?^\}', text)
        assert call is not None, 'no `module "vpc"` block in the copy'
        body, count = re.subn(pattern, replacement, call.group(0), flags=re.M)
        assert count == 1, (
            f"{pattern!r} matched {count} times in the vpc call; the mutation is vacuous"
        )
        main_tf.write_text(text[: call.start()] + body + text[call.end() :], encoding="utf-8")

    @staticmethod
    def _failed(findings: list[gate.Finding]) -> list[gate.Finding]:
        return [f for f in findings if f.status == "fail"]

    def test_the_real_tree_passes(self) -> None:
        findings = gate.check_adopted_network_wiring(gate.Plan(resource_changes=[]))
        assert not self._failed(findings), findings

    def test_an_unmutated_copy_passes_and_examines_every_input_and_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control for the cases below: the copy alone passes, over the full examined set."""
        findings = self._findings(self._scratch_infra(tmp_path), monkeypatch)
        assert not self._failed(findings), findings
        assert {f.resource for f in findings} == {
            *self._ADOPTED_INPUTS,
            *(f"(must-not-create) {t}" for t in self._MUST_NOT_CREATE),
        }

    @pytest.mark.parametrize("input_name", _ADOPTED_INPUTS)
    def test_an_adopted_input_not_passed_fails(
        self, input_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infra = self._scratch_infra(tmp_path)
        self._rewrite_vpc_call(infra, rf"^[ \t]*{input_name}[ \t]*=.*\n", "")
        failed = self._failed(self._findings(infra, monkeypatch))
        assert [f.resource for f in failed] == [input_name], failed
        assert "not passed by the root" in failed[0].detail

    @pytest.mark.parametrize("input_name", _ADOPTED_INPUTS)
    def test_an_adopted_input_wired_to_a_literal_fails(
        self, input_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infra = self._scratch_infra(tmp_path)
        self._rewrite_vpc_call(
            infra, rf"^([ \t]*{input_name}[ \t]*=[ \t]*)\S+", r'\1"adopted-literal"'
        )
        failed = self._failed(self._findings(infra, monkeypatch))
        assert [f.resource for f in failed] == [input_name], failed
        assert "literal id" in failed[0].detail

    def test_a_commented_out_input_counts_as_not_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infra = self._scratch_infra(tmp_path)
        self._rewrite_vpc_call(infra, r"^([ \t]*)(vpc_id[ \t]*=)", r"\1# \2")
        failed = self._failed(self._findings(infra, monkeypatch))
        assert [f.resource for f in failed] == ["vpc_id"], failed

    def test_no_vpc_module_call_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infra = self._scratch_infra(tmp_path)
        self._rewrite(infra / "main.tf", r'^module\s+"vpc"', 'module "network"')
        findings = self._findings(infra, monkeypatch)
        assert [(f.resource, f.status) for f in findings] == [('module "vpc"', "fail")], findings
        assert 'no `module "vpc"` call' in findings[0].detail

    def test_no_vpc_module_source_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infra = self._scratch_infra(tmp_path)
        for path in (infra / "modules" / "vpc").glob("*.tf"):
            path.unlink()
        findings = self._findings(infra, monkeypatch)
        assert [(f.resource, f.status) for f in findings] == [("modules/vpc/*.tf", "fail")]
        assert "no vpc module source was read" in findings[0].detail

    @pytest.mark.parametrize("resource_type", _MUST_NOT_CREATE)
    def test_the_vpc_module_declaring_an_adopted_type_fails(
        self, resource_type: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infra = self._scratch_infra(tmp_path)
        (infra / "modules" / "vpc" / "planted.tf").write_text(
            f'resource "{resource_type}" "planted" {{\n}}\n', encoding="utf-8"
        )
        failed = self._failed(self._findings(infra, monkeypatch))
        assert [f.resource for f in failed] == [f"(must-not-create) {resource_type}"], failed
        assert "declared as a RESOURCE in modules/vpc/" in failed[0].detail

    def test_a_commented_out_adopted_type_is_not_a_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infra = self._scratch_infra(tmp_path)
        (infra / "modules" / "vpc" / "planted.tf").write_text(
            '# resource "aws_vpc" "planted" {\n# }\n', encoding="utf-8"
        )
        assert not self._failed(self._findings(infra, monkeypatch))


# ---------------------------------------------------------------------------
# `teardown_force_destroy` reads `infra/` source and requires force-destroy on log buckets and the
# ECR repository, so a destroy cannot abort on BucketNotEmpty or RepositoryNotEmptyException.
# ---------------------------------------------------------------------------


class TestTeardownForceDestroy:
    """Every direction of `teardown_force_destroy`, exercised against a synthetic `infra/` tree."""

    @staticmethod
    def _write_tree(
        root: Path,
        *,
        bucket_flag: bool = True,
        repo_flag: bool = True,
        extra_bucket: str | None = None,
    ) -> None:
        module = root / "modules" / "observability"
        module.mkdir(parents=True)
        flag = "\n  force_destroy = true\n" if bucket_flag else "\n"
        (module / "cloudtrail.tf").write_text(
            f'resource "aws_s3_bucket" "cloudtrail" {{\n  bucket = "x"{flag}}}\n',
            encoding="utf-8",
        )
        if extra_bucket is not None:
            (module / "extra.tf").write_text(
                f'resource "aws_s3_bucket" "{extra_bucket}" {{\n  bucket = "y"\n}}\n',
                encoding="utf-8",
            )
        ecr = root / "modules" / "agentcore"
        ecr.mkdir(parents=True)
        repo = "\n  force_delete = true\n" if repo_flag else "\n"
        (ecr / "ecr_codebuild.tf").write_text(
            f'resource "aws_ecr_repository" "agent" {{\n  name = "r"{repo}}}\n',
            encoding="utf-8",
        )

    def _findings(
        self,
        tmp_path: Path,
        *,
        disposition: dict[str, str] | None = None,
        **tree: Any,
    ) -> list[Any]:
        self._write_tree(tmp_path, **tree)
        original_dir = gate.INFRA_DIR
        original_map = gate.BUCKET_TEARDOWN_DISPOSITION
        try:
            gate.INFRA_DIR = tmp_path
            gate.BUCKET_TEARDOWN_DISPOSITION = (
                {"modules/observability/aws_s3_bucket.cloudtrail": "required"}
                if disposition is None
                else disposition
            )
            return gate.check_teardown_force_destroy(gate.Plan(resource_changes=[]))
        finally:
            gate.INFRA_DIR = original_dir
            gate.BUCKET_TEARDOWN_DISPOSITION = original_map

    def test_a_fully_flagged_tree_passes(self, tmp_path: Path) -> None:
        findings = self._findings(tmp_path)
        assert findings
        assert not [f for f in findings if f.status == "fail"], findings

    def test_a_required_bucket_without_the_flag_fails(self, tmp_path: Path) -> None:
        """A required bucket without `force_destroy` fails."""
        failed = {
            f.resource for f in self._findings(tmp_path, bucket_flag=False) if f.status == "fail"
        }
        assert "modules/observability/aws_s3_bucket.cloudtrail" in failed, failed

    def test_an_ecr_repo_without_force_delete_fails(self, tmp_path: Path) -> None:
        """An ECR repository without `force_delete` fails."""
        failed = {
            f.resource for f in self._findings(tmp_path, repo_flag=False) if f.status == "fail"
        }
        assert "modules/agentcore/aws_ecr_repository.agent" in failed, failed

    def test_an_unclassified_bucket_fails(self, tmp_path: Path) -> None:
        """A new bucket cannot escape the control by simply not being in the disposition map."""
        findings = self._findings(tmp_path, extra_bucket="mystery")
        failed = {f.resource: f.detail for f in findings if f.status == "fail"}
        assert "modules/observability/aws_s3_bucket.mystery" in failed, failed
        assert "no default" in failed["modules/observability/aws_s3_bucket.mystery"]

    def test_a_forbidden_bucket_carrying_the_flag_fails(self, tmp_path: Path) -> None:
        """The safety direction: `force_destroy` on a data bucket is itself the defect."""
        findings = self._findings(
            tmp_path,
            disposition={"modules/observability/aws_s3_bucket.cloudtrail": "forbidden"},
        )
        failed = {f.resource: f.detail for f in findings if f.status == "fail"}
        assert "modules/observability/aws_s3_bucket.cloudtrail" in failed, failed
        assert "holds data" in failed["modules/observability/aws_s3_bucket.cloudtrail"]

    def test_a_forbidden_bucket_without_the_flag_passes(self, tmp_path: Path) -> None:
        findings = self._findings(
            tmp_path,
            bucket_flag=False,
            disposition={"modules/observability/aws_s3_bucket.cloudtrail": "forbidden"},
        )
        assert not [f for f in findings if f.status == "fail"], findings

    def test_a_stale_disposition_entry_fails(self, tmp_path: Path) -> None:
        """The map cannot rot into fiction: an entry for a bucket that no longer exists fails."""
        findings = self._findings(
            tmp_path,
            disposition={
                "modules/observability/aws_s3_bucket.cloudtrail": "required",
                "modules/gone/aws_s3_bucket.deleted": "required",
            },
        )
        failed = {f.resource for f in findings if f.status == "fail"}
        assert "(classified) modules/gone/aws_s3_bucket.deleted" in failed, failed

    def test_an_empty_infra_tree_fails_rather_than_passing_on_no_evidence(
        self, tmp_path: Path
    ) -> None:
        """The vacuity guard: finding no subjects is a failure, not a silent success."""
        empty = tmp_path / "empty"
        empty.mkdir()
        original_dir = gate.INFRA_DIR
        try:
            gate.INFRA_DIR = empty
            findings = gate.check_teardown_force_destroy(gate.Plan(resource_changes=[]))
        finally:
            gate.INFRA_DIR = original_dir
        assert [f for f in findings if f.status == "fail"], findings

    def test_a_conditional_flag_is_not_accepted_as_present(self, tmp_path: Path) -> None:
        """`force_destroy = var.x` is unknowable from source; reporting it compliant is a false pass."""
        self._write_tree(tmp_path, bucket_flag=False)
        path = tmp_path / "modules" / "observability" / "cloudtrail.tf"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '  bucket = "x"', '  bucket = "x"\n  force_destroy = var.allow_destroy'
            ),
            encoding="utf-8",
        )
        original_dir = gate.INFRA_DIR
        original_map = gate.BUCKET_TEARDOWN_DISPOSITION
        try:
            gate.INFRA_DIR = tmp_path
            gate.BUCKET_TEARDOWN_DISPOSITION = {
                "modules/observability/aws_s3_bucket.cloudtrail": "required"
            }
            findings = gate.check_teardown_force_destroy(gate.Plan(resource_changes=[]))
        finally:
            gate.INFRA_DIR = original_dir
            gate.BUCKET_TEARDOWN_DISPOSITION = original_map
        failed = {f.resource for f in findings if f.status == "fail"}
        assert "modules/observability/aws_s3_bucket.cloudtrail" in failed, failed

    def test_the_two_resource_types_use_their_correct_terraform_attribute_names(self) -> None:
        """Terraform spells these differently; swapping them would assert nothing at all."""
        assert gate.TEARDOWN_FLAG_ATTR["aws_s3_bucket"] == "force_destroy"
        assert gate.TEARDOWN_FLAG_ATTR["aws_ecr_repository"] == "force_delete"

    def test_block_extraction_is_not_confused_by_braces_in_strings_or_comments(
        self, tmp_path: Path
    ) -> None:
        """The block-body parser handles braces inside strings and comments without `terraform fmt`."""
        module = tmp_path / "modules" / "observability"
        module.mkdir(parents=True)
        (module / "cloudtrail.tf").write_text(
            'resource "aws_s3_bucket" "cloudtrail" {\n'
            '  bucket = "prefix-${var.name}-{not-a-brace"  # trailing } in a comment\n'
            "  force_destroy = true\n"
            "}\n"
            'resource "aws_s3_bucket" "second" {\n'
            '  bucket = "y"\n'
            "}\n",
            encoding="utf-8",
        )
        original_dir = gate.INFRA_DIR
        try:
            gate.INFRA_DIR = tmp_path
            blocks = gate.tf_resource_blocks("aws_s3_bucket")
        finally:
            gate.INFRA_DIR = original_dir
        names = [name for _module, name, _body in blocks]
        assert names == ["cloudtrail", "second"], names
        bodies = {name: body for _module, name, body in blocks}
        assert gate._declares_flag_true(bodies["cloudtrail"], "force_destroy")
        assert not gate._declares_flag_true(bodies["second"], "force_destroy")

    def test_the_real_disposition_map_classifies_every_real_bucket(self) -> None:
        """Binds the committed map to the committed tree, so neither drifts without a red test."""
        declared = {
            f"{module}/aws_s3_bucket.{name}"
            for module, name, _body in gate.tf_resource_blocks("aws_s3_bucket")
        }
        assert declared, "no aws_s3_bucket found in infra/ -- the header regex stopped matching"
        assert set(gate.BUCKET_TEARDOWN_DISPOSITION) == declared
        assert set(gate.BUCKET_TEARDOWN_DISPOSITION.values()) <= {"required", "forbidden"}


# ---------------------------------------------------------------------------
# The four source-derived relay IAM controls. Non-vacuity is proved against synthetic `infra/`
# trees (INFRA_DIR repointed).
# ---------------------------------------------------------------------------


_RELAY_INVOKE_TF = """
data "aws_iam_policy_document" "invoke_runtime" {
  statement {
    sid       = "InvokeOneRuntimeAndItsEndpoints"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [var.agent_runtime_arn, "${var.agent_runtime_arn}/runtime-endpoint/*"]
  }

  statement {
    sid         = "DenyEverythingButTheOnePermittedInvokeOnThisRuntime"
    effect      = "Deny"
    not_actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources   = [var.agent_runtime_arn, "${var.agent_runtime_arn}/runtime-endpoint/*"]
  }
}
"""

_RUNTIME_INVOKERS_LOCAL = """
locals {
  runtime_invoker_role_arns = [
    module.agent_relay.role_arn,
    module.lambda_action_sweep_task.role_arn,
    # NOTE-1
    module.lambda_action_chat_target.role_arn,
  ]
}
"""


def _runtime_principal_deny(resource: str) -> str:
    """The principal Deny statement each Runtime document must carry, on `resource`."""
    actions = "".join(f'      "{action}",\n' for action in gate.RUNTIME_DATA_PLANE_ACTIONS)
    return f"""
  statement {{
    sid    = "DenyRuntimeDataPlaneToAnyOtherPrincipal"
    effect = "Deny"
    actions = [
{actions}    ]

    principals {{
      type        = "AWS"
      identifiers = ["*"]
    }}

    resources = [{resource}]

    condition {{
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = local.runtime_invoker_role_arns
    }}
  }}
"""


_RUNTIME_DENY = _runtime_principal_deny("module.agentcore.agent_runtime_arn")
_ENDPOINT_DENY = _runtime_principal_deny("local.agent_runtime_endpoint_arn")

_RESOURCE_POLICY_TF = (
    _RUNTIME_INVOKERS_LOCAL
    + """
resource "aws_bedrockagentcore_resource_policy" "runtime" {
  resource_arn = module.agentcore.agent_runtime_arn
  policy       = data.aws_iam_policy_document.runtime_only.json
}

resource "aws_bedrockagentcore_resource_policy" "endpoint" {
  resource_arn = local.agent_runtime_endpoint_arn
  policy       = data.aws_iam_policy_document.endpoint_only.json
}

data "aws_iam_policy_document" "runtime_only" {
  statement {
    sid     = "OnlyTheRelayRoleMayInvokeThisRuntime"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]

    principals {
      type        = "AWS"
      identifiers = local.runtime_invoker_role_arns
    }

    resources = [module.agentcore.agent_runtime_arn]
  }
"""
    + _RUNTIME_DENY
    + """}

data "aws_iam_policy_document" "endpoint_only" {
  statement {
    sid     = "OnlyTheRelayRoleMayInvokeThisEndpoint"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]

    principals {
      type        = "AWS"
      identifiers = local.runtime_invoker_role_arns
    }

    resources = [local.agent_runtime_endpoint_arn]
  }
"""
    + _ENDPOINT_DENY
    + """}
"""
)


def _write_relay_tree(
    root: Path, *, relay_tf: str = _RELAY_INVOKE_TF, root_tf: str | None = None
) -> None:
    """A synthetic `infra/` holding a relay module and (optionally) the root resource policies."""
    module = root / "modules" / "agent_relay"
    module.mkdir(parents=True)
    (module / "main.tf").write_text(relay_tf, encoding="utf-8")
    (root / "main.tf").write_text(
        _RESOURCE_POLICY_TF if root_tf is None else root_tf, encoding="utf-8"
    )


def _relay_findings(
    root: Path, control_id: str, *, relay_tf: str = _RELAY_INVOKE_TF, root_tf: str | None = None
) -> list[Any]:
    _write_relay_tree(root, relay_tf=relay_tf, root_tf=root_tf)
    control = next(c for c in gate.CONTROLS if c.control_id == control_id)
    original = gate.INFRA_DIR
    try:
        gate.INFRA_DIR = root
        return control.fn(gate.Plan(resource_changes=[]))
    finally:
        gate.INFRA_DIR = original


def _failed(findings: list[Any]) -> dict[str, str]:
    return {f.resource: f.detail for f in findings if f.status == "fail"}


class TestRelayDeniesCommandExecution:
    """`relay_denies_command_execution` asserts the Deny is `not_actions`-shaped; an enumerated
    `actions` list fails as incomplete.

    The exclusion Deny is wrong in two independent directions, each planted below: a missing
    exception denies the relay its only action, and an unscoped `"*"` denies the role's logs and
    ssm grants.
    """

    def test_the_exclusion_shape_passes(self, tmp_path: Path) -> None:
        findings = _relay_findings(tmp_path, "relay_denies_command_execution")
        assert findings
        assert not _failed(findings), findings

    def test_reverting_to_an_enumerated_action_list_fails(self, tmp_path: Path) -> None:
        """An enumerated `actions` list in place of `not_actions` fails."""
        relay_tf = _RELAY_INVOKE_TF.replace(
            'not_actions = ["bedrock-agentcore:InvokeAgentRuntime"]',
            'actions     = ["bedrock-agentcore:InvokeAgentRuntimeCommand"]',
        )
        failed = _failed(
            _relay_findings(tmp_path, "relay_denies_command_execution", relay_tf=relay_tf)
        )
        assert any("shape" in resource for resource in failed), failed
        assert any("only as complete as its enumeration" in detail for detail in failed.values())

    def test_an_extra_exception_fails(self, tmp_path: Path) -> None:
        """Every additional entry in `not_actions` widens the runtime surface by one action."""
        relay_tf = _RELAY_INVOKE_TF.replace(
            'not_actions = ["bedrock-agentcore:InvokeAgentRuntime"]',
            'not_actions = ["bedrock-agentcore:InvokeAgentRuntime", '
            '"bedrock-agentcore:InvokeAgentRuntimeCommand"]',
        )
        failed = _failed(
            _relay_findings(tmp_path, "relay_denies_command_execution", relay_tf=relay_tf)
        )
        assert any("exception set" in resource for resource in failed), failed

    def test_dropping_the_permitted_action_from_the_exception_set_fails(
        self, tmp_path: Path
    ) -> None:
        """Dropping the permitted action from the exception set fails; the set is asserted exactly."""
        relay_tf = _RELAY_INVOKE_TF.replace(
            'not_actions = ["bedrock-agentcore:InvokeAgentRuntime"]',
            'not_actions = ["bedrock-agentcore:InvokeAgentRuntimeCommand"]',
        )
        failed = _failed(
            _relay_findings(tmp_path, "relay_denies_command_execution", relay_tf=relay_tf)
        )
        assert any("exception set" in resource for resource in failed), failed

    def test_an_unscoped_exclusion_deny_fails(self, tmp_path: Path) -> None:
        """An unscoped `"*"` exclusion Deny fails, since it would deny `logs:CreateLogStream` and
        `ssm:GetParameter` on this role too."""
        relay_tf = _RELAY_INVOKE_TF.replace(
            'resources   = [var.agent_runtime_arn, "${var.agent_runtime_arn}/runtime-endpoint/*"]',
            'resources   = ["*"]',
        )
        failed = _failed(
            _relay_findings(tmp_path, "relay_denies_command_execution", relay_tf=relay_tf)
        )
        assert any("resource scope" in resource for resource in failed), failed
        assert any("ACCOUNT-WIDE" in detail for detail in failed.values())

    def test_an_allow_instead_of_a_deny_fails(self, tmp_path: Path) -> None:
        """An Allow in place of the Deny fails through the coverage finding."""
        findings = _relay_findings(
            tmp_path,
            "relay_denies_command_execution",
            relay_tf=_RELAY_INVOKE_TF.replace('effect      = "Deny"', 'effect      = "Allow"'),
        )
        failed = _failed(findings)
        assert any("Deny statements" in resource for resource in failed), findings

    def test_a_second_deny_statement_fails_rather_than_picking_one(self, tmp_path: Path) -> None:
        """Two Deny statements fail rather than the control checking only the first."""
        relay_tf = _RELAY_INVOKE_TF.replace(
            "}\n",
            '}\n\n  statement {\n    effect      = "Deny"\n'
            '    not_actions = ["s3:GetObject"]\n    resources   = ["*"]\n  }\n',
            1,
        )
        failed = _failed(
            _relay_findings(tmp_path, "relay_denies_command_execution", relay_tf=relay_tf)
        )
        assert any("Deny statements" in resource for resource in failed), failed

    def test_an_interpolated_exception_set_is_not_accepted(self, tmp_path: Path) -> None:
        """An exception set assembled from an expression yields no entries and is not accepted."""
        relay_tf = _RELAY_INVOKE_TF.replace(
            'not_actions = ["bedrock-agentcore:InvokeAgentRuntime"]',
            "not_actions = var.permitted_actions",
        )
        failed = _failed(
            _relay_findings(tmp_path, "relay_denies_command_execution", relay_tf=relay_tf)
        )
        assert failed, "an unreadable exception set must not pass"

    def test_an_empty_module_fails_rather_than_finding_nothing(self, tmp_path: Path) -> None:
        findings = _relay_findings(tmp_path, "relay_denies_command_execution", relay_tf="\n")
        assert _failed(findings), findings


class TestRelayInvokeScope:
    """The relay invoke grant requires both ARNs and no prefix wildcard."""

    def test_runtime_plus_endpoint_passes(self, tmp_path: Path) -> None:
        findings = _relay_findings(tmp_path, "relay_invoke_scoped_to_runtime_and_endpoint")
        assert not _failed(findings), findings

    def test_runtime_only_fails(self, tmp_path: Path) -> None:
        """A runtime-only grant fails."""
        findings = _relay_findings(
            tmp_path,
            "relay_invoke_scoped_to_runtime_and_endpoint",
            relay_tf=_RELAY_INVOKE_TF.replace(
                '[var.agent_runtime_arn, "${var.agent_runtime_arn}/runtime-endpoint/*"]',
                "[var.agent_runtime_arn]",
            ),
        )
        assert _failed(findings), findings

    def test_a_bare_prefix_wildcard_fails(self, tmp_path: Path) -> None:
        """`"${arn}*"` also matches any sibling runtime sharing this one's ARN prefix."""
        findings = _relay_findings(
            tmp_path,
            "relay_invoke_scoped_to_runtime_and_endpoint",
            relay_tf=_RELAY_INVOKE_TF.replace(
                '[var.agent_runtime_arn, "${var.agent_runtime_arn}/runtime-endpoint/*"]',
                '[var.agent_runtime_arn, "${var.agent_runtime_arn}*"]',
            ),
        )
        failed = _failed(findings)
        assert failed, findings
        assert any("prefix_wildcards" in detail for detail in failed.values())

    def test_an_endpoint_of_a_different_runtime_fails(self, tmp_path: Path) -> None:
        findings = _relay_findings(
            tmp_path,
            "relay_invoke_scoped_to_runtime_and_endpoint",
            relay_tf=_RELAY_INVOKE_TF.replace(
                '"${var.agent_runtime_arn}/runtime-endpoint/*"',
                '"${var.other_runtime_arn}/runtime-endpoint/*"',
            ),
        )
        assert _failed(findings), findings

    def test_a_missing_grant_fails_rather_than_emitting_nothing(self, tmp_path: Path) -> None:
        findings = _relay_findings(
            tmp_path, "relay_invoke_scoped_to_runtime_and_endpoint", relay_tf="\n"
        )
        failed = _failed(findings)
        assert failed, findings
        assert any("no Allow statement grants" in detail for detail in failed.values())


class TestRelayNoForbiddenGrants:
    def test_the_real_shape_passes(self, tmp_path: Path) -> None:
        findings = _relay_findings(tmp_path, "relay_no_forbidden_grants")
        assert not _failed(findings), findings

    @pytest.mark.parametrize(
        "action",
        [
            "lambda:InvokeFunction",
            "dynamodb:GetItem",
            "bedrock:InvokeModel",
            "iam:PassRole",
            "secretsmanager:GetSecretValue",
            "bedrock-agentcore:*",
            "*",
        ],
    )
    def test_a_forbidden_allow_fails(self, tmp_path: Path, action: str) -> None:
        findings = _relay_findings(
            tmp_path,
            "relay_no_forbidden_grants",
            relay_tf=_RELAY_INVOKE_TF.replace(
                '["bedrock-agentcore:InvokeAgentRuntime"]',
                f'["bedrock-agentcore:InvokeAgentRuntime", "{action}"]',
            ),
        )
        failed = _failed(findings)
        assert failed, f"{action} was permitted"
        assert any(action in detail for detail in failed.values()), failed

    def test_the_same_action_denied_rather_than_allowed_is_fine(self, tmp_path: Path) -> None:
        """The control targets Allow statements; a Deny naming the same action is not a grant."""
        findings = _relay_findings(
            tmp_path,
            "relay_no_forbidden_grants",
            relay_tf=_RELAY_INVOKE_TF.replace(
                '      "bedrock-agentcore:InvokeAgentRuntimeForUser",\n',
                '      "bedrock-agentcore:InvokeAgentRuntimeForUser",\n      "lambda:InvokeFunction",\n',
            ),
        )
        assert not _failed(findings), findings

    def test_a_statement_with_no_effect_is_treated_as_allow(self, tmp_path: Path) -> None:
        """Terraform defaults `effect` to Allow; treating an omitted effect as Allow is the safe read."""
        findings = _relay_findings(
            tmp_path,
            "relay_no_forbidden_grants",
            relay_tf="""
data "aws_iam_policy_document" "sloppy" {
  statement {
    sid       = "NoEffectDeclared"
    actions   = ["dynamodb:Scan"]
    resources = ["*"]
  }
}
""",
        )
        assert _failed(findings), findings


class TestRuntimeResourcePolicySoleInvoker:
    """`runtime_resource_policy_sole_invoker`: each Runtime ARN has its own policy admitting exactly
    `local.runtime_invoker_role_arns`, which is exactly the relay, `sweep_task` and `chat_target`."""

    def test_two_policies_each_admitting_only_the_invoker_roles_passes(
        self, tmp_path: Path
    ) -> None:
        findings = _relay_findings(tmp_path, "runtime_resource_policy_sole_invoker")
        assert not _failed(findings), findings

    def test_only_one_policy_fails(self, tmp_path: Path) -> None:
        """A single policy leaves the other ARN open to every principal in the account."""
        root_tf = _RESOURCE_POLICY_TF[
            : _RESOURCE_POLICY_TF.index(
                'resource "aws_bedrockagentcore_resource_policy" "endpoint"'
            )
        ]
        findings = _relay_findings(
            tmp_path, "runtime_resource_policy_sole_invoker", root_tf=root_tf
        )
        failed = _failed(findings)
        assert failed, findings
        assert any("expected=2" in detail for detail in failed.values())

    def test_a_wildcard_principal_fails(self, tmp_path: Path) -> None:
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(
                "identifiers = local.runtime_invoker_role_arns", 'identifiers = ["*"]', 1
            ),
        )
        failed = _failed(findings)
        assert failed, findings
        assert any("admits every principal" in detail for detail in failed.values())

    def test_a_second_principal_fails(self, tmp_path: Path) -> None:
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(
                "identifiers = local.runtime_invoker_role_arns",
                "identifiers = [module.agent_relay.role_arn, aws_iam_role.someone_else.arn]",
                1,
            ),
        )
        assert _failed(findings), findings

    def test_a_fourth_invoker_in_the_local_fails(self, tmp_path: Path) -> None:
        """Exactly three roles; any further one can assert any identity."""
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(
                "    module.lambda_action_chat_target.role_arn,\n",
                "    module.lambda_action_chat_target.role_arn,\n    aws_iam_role.someone_else.arn,\n",
                1,
            ),
        )
        failed = _failed(findings)
        assert list(failed) == ["local.runtime_invoker_role_arns"], findings

    def test_a_missing_invoker_in_the_local_fails(self, tmp_path: Path) -> None:
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(
                "    module.lambda_action_sweep_task.role_arn,\n", "", 1
            ),
        )
        assert list(_failed(findings)) == ["local.runtime_invoker_role_arns"], findings

    def test_a_document_naming_a_role_instead_of_the_local_fails(self, tmp_path: Path) -> None:
        """A document bypassing the local is not governed by the local's membership check."""
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(
                "identifiers = local.runtime_invoker_role_arns",
                "identifiers = [module.agent_relay.role_arn]",
                1,
            ),
        )
        assert any("expected exactly local." in d for d in _failed(findings).values()), findings

    def test_an_absent_invoker_local_fails(self, tmp_path: Path) -> None:
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(_RUNTIME_INVOKERS_LOCAL, "", 1),
        )
        assert "local.runtime_invoker_role_arns" in _failed(findings), findings

    def test_a_document_without_the_principal_deny_fails(self, tmp_path: Path) -> None:
        """An Allow-only document lets any principal with an identity-policy Allow invoke."""
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(_ENDPOINT_DENY, "", 1),
        )
        failed = _failed(findings)
        assert list(failed) == ["./aws_bedrockagentcore_resource_policy.endpoint"], findings
        assert "0 Deny" in failed["./aws_bedrockagentcore_resource_policy.endpoint"]

    def test_an_invoker_dropped_from_the_deny_exclusion_fails(self, tmp_path: Path) -> None:
        """Negative control: the Deny must exclude exactly the local, not a narrower list."""
        narrowed = _RUNTIME_DENY.replace(
            "values   = local.runtime_invoker_role_arns",
            "values   = [module.agent_relay.role_arn, module.lambda_action_sweep_task.role_arn]",
        )
        assert narrowed != _RUNTIME_DENY
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(_RUNTIME_DENY, narrowed, 1),
        )
        failed = _failed(findings)
        assert list(failed) == ["./aws_bedrockagentcore_resource_policy.runtime"], findings
        assert "deny conditions=" in failed["./aws_bedrockagentcore_resource_policy.runtime"]

    @pytest.mark.parametrize(
        ("original", "replacement", "message"),
        [
            ('test     = "ArnNotEquals"', 'test     = "StringNotEquals"', "deny conditions="),
            ('variable = "aws:PrincipalArn"', 'variable = "aws:userid"', "deny conditions="),
            (
                '      "bedrock-agentcore:GetAgentCard",\n',
                "",
                "deny actions=",
            ),
            (
                'identifiers = ["*"]',
                "identifiers = local.runtime_invoker_role_arns",
                "deny principals=",
            ),
            (
                "resources = [module.agentcore.agent_runtime_arn]",
                "resources = [local.agent_runtime_endpoint_arn]",
                "deny resources=",
            ),
            (
                'sid    = "DenyRuntimeDataPlaneToAnyOtherPrincipal"',
                'sid    = "SomethingElse"',
                "deny sid=",
            ),
            (
                "  }\n  }\n",
                '  }\n\n    condition {\n      test     = "Bool"\n'
                '      variable = "aws:ViaAWSService"\n      values   = ["false"]\n    }\n  }\n',
                "deny conditions=",
            ),
        ],
        ids=[
            "string-operator",
            "other-key",
            "missing-action",
            "listed-principals",
            "wrong-resource",
            "other-sid",
            "extra-condition",
        ],
    )
    def test_a_weakened_principal_deny_fails(
        self, tmp_path: Path, original: str, replacement: str, message: str
    ) -> None:
        assert original in _RUNTIME_DENY, original
        weakened = _RUNTIME_DENY.replace(original, replacement, 1)
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(_RUNTIME_DENY, weakened, 1),
        )
        failed = _failed(findings)
        assert list(failed) == ["./aws_bedrockagentcore_resource_policy.runtime"], findings
        assert message in failed["./aws_bedrockagentcore_resource_policy.runtime"]

    def test_the_deny_action_set_is_every_supported_runtime_data_plane_action(self) -> None:
        """Pinned to AgentCore's documented runtime action list; no control-plane action."""
        assert len(set(gate.RUNTIME_DATA_PLANE_ACTIONS)) == 8
        for action in gate.RUNTIME_DATA_PLANE_ACTIONS:
            assert action.startswith("bedrock-agentcore:")
            assert not action.split(":", 1)[1].startswith(
                ("Create", "Update", "Delete", "Put", "List")
            ), action

    def test_both_policies_on_the_same_arn_fails(self, tmp_path: Path) -> None:
        """Two policies on one ARN leaves the other ARN with no policy at all."""
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(
                "resource_arn = local.agent_runtime_endpoint_arn",
                "resource_arn = module.agentcore.agent_runtime_arn",
                1,
            ),
        )
        failed = _failed(findings)
        assert failed, findings
        assert any("same ARN" in detail for detail in failed.values())

    def test_a_wider_action_fails(self, tmp_path: Path) -> None:
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(
                'actions = ["bedrock-agentcore:InvokeAgentRuntime"]',
                'actions = ["bedrock-agentcore:*"]',
                1,
            ),
        )
        assert _failed(findings), findings

    def test_a_resource_not_matching_its_own_resource_arn_fails(self, tmp_path: Path) -> None:
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=_RESOURCE_POLICY_TF.replace(
                "resources = [module.agentcore.agent_runtime_arn]", 'resources = ["*"]', 1
            ),
        )
        assert _failed(findings), findings

    def test_no_policies_at_all_fails(self, tmp_path: Path) -> None:
        findings = _relay_findings(tmp_path, "runtime_resource_policy_sole_invoker", root_tf="\n")
        assert _failed(findings), findings


# ---------------------------------------------------------------------------
# `resource_inventory_identity`: the action-Lambda inventory is asserted by identity, so a missing
# member fails even when the total count is unchanged.
# ---------------------------------------------------------------------------


class TestResourceInventoryIdentity:
    @staticmethod
    def _plan_without(address_fragment: str) -> gate.Plan:
        document = plan_fixtures.build("compliant")
        kept = [
            r for r in document["resource_changes"] if address_fragment not in r.get("address", "")
        ]
        assert len(kept) < len(document["resource_changes"]), (
            f"planted defect removed nothing: no address contains {address_fragment!r}"
        )
        return gate.Plan(resource_changes=kept, configuration=document.get("configuration", {}))

    def test_the_real_fixture_passes(self) -> None:
        findings = gate.check_resource_inventory_identity(load_plan("compliant"))
        assert findings
        assert not [f for f in findings if f.status == "fail"], findings

    def test_a_missing_action_lambda_fails(self) -> None:
        """Deleting one tool's action Lambda fails."""
        plan = self._plan_without('module.lambda_action["get_account"].aws_lambda_function.this')
        failed = {
            f.resource for f in gate.check_resource_inventory_identity(plan) if f.status == "fail"
        }
        assert "aws_lambda_function:get_account" in failed, failed

    def test_a_missing_table_fails(self) -> None:
        plan = self._plan_without("aws_dynamodb_table.accounts")
        failed = {
            f.resource for f in gate.check_resource_inventory_identity(plan) if f.status == "fail"
        }
        assert "aws_dynamodb_table:accounts" in failed, failed

    def test_an_action_lambda_no_tool_declares_fails(self) -> None:
        """The other direction: a function that outlives the tool declaring it."""
        document = plan_fixtures.build("compliant")
        document["resource_changes"].append(
            {
                "address": 'module.lambda_action["ghost_tool"].aws_lambda_function.this',
                "type": "aws_lambda_function",
                "change": {"after": {}, "after_unknown": {}},
            }
        )
        plan = gate.Plan(resource_changes=document["resource_changes"])
        failed = {
            f.resource: f.detail
            for f in gate.check_resource_inventory_identity(plan)
            if f.status == "fail"
        }
        assert "aws_lambda_function:ghost_tool" in failed, failed
        assert "not declared in ALL_TOOLS" in failed["aws_lambda_function:ghost_tool"]

    #: A tool whose action Lambda is removed from a plan COPY to plant a missing inventory member.
    #: A tool whose action Lambda is removed from a plan copy to plant a missing inventory member.
    PLANTED_MISSING_TOOL = "get_page_context"

    def test_an_undeclared_absence_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing member with no recorded reason must fail, not be tolerated."""
        monkeypatch.setattr(gate, "FIXTURE_ABSENT_INSTANCES", {})
        plan = self._plan_without(f'module.lambda_action["{self.PLANTED_MISSING_TOOL}"]')
        failed = {
            f.resource for f in gate.check_resource_inventory_identity(plan) if f.status == "fail"
        }
        assert f"aws_lambda_function:{self.PLANTED_MISSING_TOOL}" in failed, failed

    def test_a_recorded_reason_excuses_a_missing_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The allowance's own direction: with a reason recorded, the same absence passes."""
        member = f"aws_lambda_function:{self.PLANTED_MISSING_TOOL}"
        monkeypatch.setattr(
            gate,
            "FIXTURE_ABSENT_INSTANCES",
            {**gate.FIXTURE_ABSENT_INSTANCES, member: "planted reason"},
        )
        findings = gate.check_resource_inventory_identity(
            self._plan_without(f'module.lambda_action["{self.PLANTED_MISSING_TOOL}"]')
        )
        assert not [f for f in findings if f.status == "fail"], findings
        assert any(f.resource == member and f.status == "pass" for f in findings), findings

    def test_an_unexercised_declared_absence_defers_rather_than_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A declared absence whose member is present in the plan defers rather than fails.

        `test_an_entry_for_an_unchecked_type_fails` covers the genuinely-failing arm.
        """
        member = "aws_lambda_function:get_account"
        monkeypatch.setattr(
            gate,
            "FIXTURE_ABSENT_INSTANCES",
            {**gate.FIXTURE_ABSENT_INSTANCES, member: "get_account IS in the fixture"},
        )
        findings = gate.check_resource_inventory_identity(load_plan("compliant"))
        matching = [f for f in findings if f.resource == f"(declared-absent) {member}"]
        assert matching, [f.resource for f in findings]
        assert [f.status for f in matching] == ["deferred"], matching
        assert "not needed by THIS plan" in matching[0].detail, matching[0].detail

    def test_an_entry_for_an_unchecked_type_fails(self) -> None:
        """An exemption naming a type this control does not inspect would exempt nothing, silently."""
        original = gate.FIXTURE_ABSENT_INSTANCES
        try:
            gate.FIXTURE_ABSENT_INSTANCES = {
                **original,
                "aws_s3_bucket:whatever": "names a type this control never checks",
            }
            failed = {
                f.resource
                for f in gate.check_resource_inventory_identity(load_plan("compliant"))
                if f.status == "fail"
            }
            assert "(declared-absent) aws_s3_bucket:whatever" in failed, failed
        finally:
            gate.FIXTURE_ABSENT_INSTANCES = original

    def test_the_tool_inventory_is_derived_not_transcribed(self) -> None:
        """The tool inventory equals an independent load of `definitions.py`, and no tool name
        appears as a literal in the gate's source."""
        spec = importlib.util.spec_from_file_location(
            "_probe_definitions", REPO_ROOT / "src" / "crm" / "toolspec" / "definitions.py"
        )
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        sys.modules["_probe_definitions"] = probe
        spec.loader.exec_module(probe)
        expected = {tool.name for tool in probe.ALL_TOOLS}
        assert gate._tool_keys_from_definitions() == expected
        assert len(expected) >= 22, expected

        source = SCRIPT_PATH.read_text(encoding="utf-8")
        transcribed = sorted(name for name in expected if f'"{name}"' in source)
        assert not transcribed, (
            f"tool name(s) appear as literals in the gate source: {transcribed} -- the inventory "
            "must be derived from ALL_TOOLS, not restated beside it"
        )

    def test_the_table_inventory_is_derived_from_schema(self) -> None:
        derived = gate._load_dynamodb_schema_gsis()
        assert len(derived) == 11, sorted(derived)
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        transcribed = sorted(name for name in derived if f'"{name}"' in source)
        assert not transcribed, f"table name(s) restated in the gate source: {transcribed}"

    def test_an_empty_inventory_source_fails(self) -> None:
        """Guards the vacuity case: no tools derived means the control checked nothing."""
        findings = gate.check_resource_inventory_identity(gate.Plan(resource_changes=[]))
        assert [f for f in findings if f.status == "fail"], findings


# ---------------------------------------------------------------------------
# `plan_freshness`: a control reading a snapshot asserts the snapshot is current.
# ---------------------------------------------------------------------------


class TestPlanFreshness:
    """`plan_freshness` compares the recorded infra digest with the working tree's; no mtime, no
    git history."""

    @staticmethod
    def _fixture_with_provenance(tmp_path: Path, digest: str | None, name: str) -> Path:
        """Return a copy of the committed fixture whose `captured_infra_digest` is `digest`.

        `digest=None` removes the key, to exercise the no-provenance disposition.
        """
        document = plan_fixtures.build("compliant")
        provenance = dict(document.get("_fixture_provenance") or {})
        if digest is None:
            provenance.pop(gate.INFRA_DIGEST_KEY, None)
        else:
            provenance[gate.INFRA_DIGEST_KEY] = digest
        document["_fixture_provenance"] = provenance
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    @staticmethod
    def _clone(tmp_path: Path) -> Path:
        """A `--shared` clone of this repository, so probes never touch its own `infra/`."""
        clone = tmp_path / "freshness-probe"
        completed = proc_run.run(
            ["git", "clone", "--quiet", "--shared", str(REPO_ROOT), str(clone)],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        return clone

    def test_the_committed_fixture_freshness_is_reported_truthfully(self) -> None:
        """The gate's verdict on the committed fixture matches the digest comparison, either way."""
        fixture = fixture_path("compliant")
        document = json.loads(fixture.read_text(encoding="utf-8"))
        recorded = document["_fixture_provenance"][gate.INFRA_DIGEST_KEY]
        current = plan_fixtures.infra_digest(REPO_ROOT)

        finding = gate.plan_staleness_finding(fixture)
        assert finding is not None, "the control produced no finding at all"
        assert recorded in finding.detail, "the detail must name the digest it compared"
        if recorded != current:
            assert finding.status == "deferred", finding.detail
            assert "differs from the working tree" in finding.detail
        else:
            assert finding.status == "pass", finding.detail
            assert "equals the working tree" in finding.detail

    def test_a_fixture_recording_another_digest_is_deferred_and_names_both(
        self, tmp_path: Path
    ) -> None:
        """A fixture whose digest is not the tree's is deferred and names both digests."""
        finding = gate.plan_staleness_finding(
            self._fixture_with_provenance(tmp_path, "0" * 64, "stale.json")
        )
        assert finding is not None
        assert finding.status == "deferred", finding.detail
        assert "0" * 64 in finding.detail
        assert plan_fixtures.infra_digest(REPO_ROOT) in finding.detail
        # The detail must state that source-derived controls are unaffected.
        assert "teardown_force_destroy" in finding.detail

    def test_a_fixture_recording_the_current_digest_is_a_pass(self, tmp_path: Path) -> None:
        current = plan_fixtures.infra_digest(REPO_ROOT)
        finding = gate.plan_staleness_finding(
            self._fixture_with_provenance(tmp_path, current, "fresh.json")
        )
        assert finding is not None, "a fresh plan produced no finding at all"
        assert finding.status == "pass", finding.detail

    @pytest.mark.parametrize(
        "digest,expected_phrase,label",
        [
            (None, "records no `_fixture_provenance.captured_infra_digest`", "no provenance"),
            ("", "records no `_fixture_provenance.captured_infra_digest`", "an empty digest"),
        ],
    )
    def test_every_unanswerable_case_defers_rather_than_passing(
        self, tmp_path: Path, digest: str | None, expected_phrase: str, label: str
    ) -> None:
        """Every case where freshness cannot be determined defers rather than passes."""
        finding = gate.plan_staleness_finding(
            self._fixture_with_provenance(tmp_path, digest, "unanswerable.json")
        )
        assert finding is not None, label
        assert finding.status == "deferred", (label, finding.detail)
        assert expected_phrase in finding.detail, (label, finding.detail)

    def test_a_tree_whose_digest_cannot_be_computed_defers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        plan = self._fixture_with_provenance(
            tmp_path, plan_fixtures.infra_digest(REPO_ROOT), "fresh.json"
        )
        monkeypatch.setattr(gate, "REPO_ROOT", not_a_repo)
        finding = gate.plan_staleness_finding(plan)
        assert finding is not None
        assert finding.status == "deferred", finding.detail
        assert "could not be computed" in finding.detail

    def test_the_control_no_longer_consults_a_file_mtime_at_all(self) -> None:
        """The gate's source carries no mtime helpers."""
        source = Path(gate.__file__).read_text(encoding="utf-8")
        for banned in ("newest_infra_source_mtime", "st_mtime", "os.utime", "getmtime"):
            assert banned not in source, (
                f"{banned!r} is back in {gate.__name__}. Plan freshness is a question about CONTENT, "
                "and a timestamp cannot answer it: git gives every checked-out file one identical "
                "mtime, so any mtime path reports a fresh clone as fresh."
            )

    def test_the_control_reads_no_git_history(self) -> None:
        """The gate defines no git-history helper and issues no history command."""
        for removed in ("infra_changes_since", "_git", "_infra_tf_pathspec", "_authored_tf_paths"):
            assert not hasattr(gate, removed), f"{removed!r} is back in {gate.__name__}"
        source = Path(gate.__file__).read_text(encoding="utf-8")
        for banned in ('"rev-parse"', '"rev-list"', '"diff", "--name-only"'):
            assert banned not in source, f"{banned} is back in {gate.__name__}"

    def test_the_comparison_reaches_the_working_tree_for_tracked_tf_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An uncommitted edit to a tracked root-level or nested `.tf` stales the plan; an untracked
        `.tf` does not. Probes are planted in a clone, never in this repository's own `infra/`.
        """
        clone = self._clone(tmp_path)
        plan = self._fixture_with_provenance(
            tmp_path, plan_fixtures.infra_digest(clone), "fresh.json"
        )
        monkeypatch.setattr(gate, "REPO_ROOT", clone)

        def status() -> str:
            finding = gate.plan_staleness_finding(plan)
            assert finding is not None
            return finding.status

        assert status() == "pass", "the fresh clone does not match its own digest"

        (clone / "infra" / "zz_untracked_probe.tf").write_text("# untracked\n", encoding="utf-8")
        assert status() == "pass", "an untracked .tf changed the digest"

        nested = sorted((clone / "infra" / "modules").rglob("*.tf"))[0]
        original = nested.read_bytes()
        nested.write_bytes(original + b"\n")
        assert status() == "deferred", f"an edit to nested {nested.name} was invisible"
        nested.write_bytes(original)
        assert status() == "pass"

        root_level = clone / "infra" / "main.tf"
        root_level.write_bytes(root_level.read_bytes() + b"\n")
        assert status() == "deferred", "an edit to root-level infra/main.tf was invisible"

    def test_an_absent_plan_is_not_flagged_as_stale(self, tmp_path: Path) -> None:
        """An absent plan is not reported as stale."""
        assert gate.plan_staleness_finding(tmp_path / "nope.json") is None

    def test_a_clean_run_against_a_stale_plan_exits_zero_and_defers_visibly(
        self, tmp_path: Path
    ) -> None:
        """A clean run against a stale plan exits 0 with a deferral `run_gates.py` counts."""
        stale = self._fixture_with_provenance(tmp_path, "0" * 64, "stale_cli.json")
        result = run_cli("--plan", str(stale))
        assert result.returncode == 0, result.stdout[-2000:]
        assert f"{_run_gates_module().GATE_DEFER_MARKER} plan_freshness" in result.stdout
        assert _run_gates_module().deferred_control_count(result.stdout) > 0

    def test_the_emitted_marker_is_the_one_run_gates_counts(self) -> None:
        """The deferred count `run_gates` derives from stdout equals the gate's own tally and its
        `--json` count."""
        result = run_cli("--plan", str(fixture_path("compliant")))
        json_result = run_cli("--plan", str(fixture_path("compliant")), "--json")
        payload = json.loads(json_result.stdout)
        rendered = _run_gates_module().deferred_control_count(result.stdout)
        assert payload["deferred"] > 0, "nothing defers, so this test would pass vacuously"
        assert rendered == payload["deferred"], (
            f"run_gates counts {rendered} deferred line(s) but the gate reports "
            f"{payload['deferred']} deferred findings"
        )

    def test_the_gate_declares_a_structured_deferred_count_to_run_gates(self) -> None:
        """The gate declares a structured deferred count to `run_gates`."""
        run_gates = _run_gates_module()
        assert "check_plan_security_baseline" in run_gates.GATE_STRUCTURED_DEFERRED_ARGV
        assert run_gates.GATE_STRUCTURED_DEFERRED_KEY in json.loads(
            run_cli("--plan", str(fixture_path("compliant")), "--json").stdout
        )

    def test_a_real_violation_still_exits_one_against_a_stale_plan(self) -> None:
        """A genuine failure still exits 1 against a stale plan."""
        result = run_cli("--plan", str(FIXTURES_DIR / "violation_s3_bpa_disabled.json"))
        assert result.returncode == 1, result.stdout[-2000:]


class TestSourceParsingIsWhitespaceTolerant:
    """The gate's source parsers tolerate `terraform fmt` realignment of `=`.

    These plant the realigned form and assert the controls still pass.
    """

    @staticmethod
    def _realign(terraform: str) -> str:
        """Re-space every `name = value` so `=` sits far right, as fmt does for a wide block."""
        return re.sub(
            r"^(\s*)([a-z_]+)\s*=\s*",
            lambda m: f"{m.group(1)}{m.group(2)}{' ' * (30 - len(m.group(2)))}= ",
            terraform,
            flags=re.MULTILINE,
        )

    def test_the_realignment_helper_actually_changes_the_text(self) -> None:
        """The realignment helper changes the text."""
        realigned = self._realign(_RELAY_INVOKE_TF)
        assert realigned != _RELAY_INVOKE_TF
        assert "effect" in realigned and re.search(r"effect\s{5,}=", realigned), realigned[:400]

    @pytest.mark.parametrize(
        "control_id",
        [
            "relay_denies_command_execution",
            "relay_invoke_scoped_to_runtime_and_endpoint",
            "relay_no_forbidden_grants",
        ],
    )
    def test_the_relay_controls_pass_against_realigned_terraform(
        self, tmp_path: Path, control_id: str
    ) -> None:
        findings = _relay_findings(tmp_path, control_id, relay_tf=self._realign(_RELAY_INVOKE_TF))
        assert findings
        assert not _failed(findings), f"{control_id} broke on a pure formatting change: {findings}"

    def test_the_resource_policy_control_passes_against_realigned_terraform(
        self, tmp_path: Path
    ) -> None:
        findings = _relay_findings(
            tmp_path,
            "runtime_resource_policy_sole_invoker",
            root_tf=self._realign(_RESOURCE_POLICY_TF),
        )
        assert not _failed(findings), findings

    def test_the_teardown_control_passes_against_realigned_terraform(self, tmp_path: Path) -> None:
        module = tmp_path / "modules" / "observability"
        module.mkdir(parents=True)
        (module / "cloudtrail.tf").write_text(
            'resource "aws_s3_bucket" "cloudtrail" {\n'
            '  bucket                         = "x"\n'
            "  force_destroy                  = true\n"
            "}\n",
            encoding="utf-8",
        )
        original_dir = gate.INFRA_DIR
        original_map = gate.BUCKET_TEARDOWN_DISPOSITION
        try:
            gate.INFRA_DIR = tmp_path
            gate.BUCKET_TEARDOWN_DISPOSITION = {
                "modules/observability/aws_s3_bucket.cloudtrail": "required"
            }
            findings = gate.check_teardown_force_destroy(gate.Plan(resource_changes=[]))
        finally:
            gate.INFRA_DIR = original_dir
            gate.BUCKET_TEARDOWN_DISPOSITION = original_map
        # Asserted on the bucket's own finding: this synthetic tree has no ECR repository, so the
        # coverage assertion fails and is not under test. Match the bare address exactly.
        (bucket_finding,) = [
            f for f in findings if f.resource == "modules/observability/aws_s3_bucket.cloudtrail"
        ]
        assert bucket_finding.status == "pass", bucket_finding
        assert "force_destroy=True" in bucket_finding.detail, bucket_finding

    def test_no_parser_regex_in_the_gate_matches_a_literal_gap(self) -> None:
        """No parser regex in the gate matches a literal multi-space run before `=`."""
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        offending = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"re\.(search|match|compile|fullmatch|sub)", line)
            and re.search(r"""['"][^'"]*\s{2,}=""", line)
        ]
        assert not offending, (
            "pattern(s) match a literal whitespace gap before `=`; terraform fmt varies that gap "
            f"per block, so this fails on a formatting change and looks like a security finding: {offending}"
        )


#: The shape of the real `infra/main.tf` Bedrock grant: two statements. `BedrockInference` holds
#: the `global.` inference profiles plus `project/default`; `BedrockInferenceFoundationModels` holds
#: the foundation-model ARNs, bound to the profiles by a `bedrock:InferenceProfileArn` condition.
_BEDROCK_TF = """
data "aws_iam_policy_document" "agentcore_runtime_permissions" {
  statement {
    sid    = "EcrAuth"
    effect = "Allow"
    # A legitimate wildcard sitting next to the scoped grant: `ecr:GetAuthorizationToken` takes no
    # resource. Its presence is why a reviewer skimming for `"*"` has a reason to move on.
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "BedrockInference"
    effect = "Allow"
    # Scoped to named inference profiles and this account's default project -- never Resource = "*".
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(
      [for profile in [
        "global.anthropic.claude-opus-5-5",
        "global.openai.gpt-6-astra",
      ] : "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${profile}"],
      ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/default"],
    )
  }

  statement {
    sid    = "BedrockInferenceFoundationModels"
    effect = "Allow"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(
      [for model in ["anthropic.claude-opus-5-5", "openai.gpt-6-astra"] : "arn:aws:bedrock:::foundation-model/${model}"],
      [for model in ["anthropic.claude-opus-5-5", "openai.gpt-6-astra"] : "arn:aws:bedrock:*::foundation-model/${model}"],
    )
    condition {
      test     = "StringEquals"
      variable = "bedrock:InferenceProfileArn"
      values   = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/global.anthropic.claude-opus-5-5"]
    }
  }
}
"""


def _bedrock_findings(root: Path, tf: str = _BEDROCK_TF) -> list[Any]:
    (root / "main.tf").write_text(tf, encoding="utf-8")
    original = gate.INFRA_DIR
    try:
        gate.INFRA_DIR = root
        return gate.check_bedrock_inference_profile_scoping(gate.Plan(resource_changes=[]))
    finally:
        gate.INFRA_DIR = original


class TestBedrockInferenceProfileScoping:
    """`bedrock_inference_profile_scoping`: each planted widening of the Bedrock grant fails."""

    def test_the_real_infra_tree_passes(self) -> None:
        """The real `infra/` tree passes."""
        findings = gate.check_bedrock_inference_profile_scoping(gate.Plan(resource_changes=[]))
        assert findings
        assert not _failed(findings), findings
        assert any("BedrockInference" in f.resource for f in findings), findings

    def test_the_reproduced_shape_passes(self, tmp_path: Path) -> None:
        """The reproduced shape passes, so the planting tests below are meaningful."""
        assert not _failed(_bedrock_findings(tmp_path)), _bedrock_findings(tmp_path)

    def test_the_project_arn_is_accepted(self, tmp_path: Path) -> None:
        """A `project/default` ARN is accepted."""
        findings = _bedrock_findings(tmp_path)
        assert not _failed(findings), findings
        assert any(
            "project ARN" in f.detail and "BedrockInference" in f.resource for f in findings
        ), findings

    @pytest.mark.parametrize(
        "widened",
        [
            '["*"]',
            '["arn:aws:bedrock:us-east-1:123456789012:*"]',
            '["arn:aws:bedrock:*:*:inference-profile/*", "*"]',
        ],
        ids=["bare-star", "arn-trailing-star", "star-alongside-scoped"],
    )
    def test_widening_the_resource_set_fails(self, tmp_path: Path, widened: str) -> None:
        """Widening the `BedrockInference` resource set fails.

        Substitutes the first `resources = concat(...)`, leaving the foundation-model concat intact.
        """
        tf = re.sub(
            r"resources = concat\((?:.*\n)*?    \)", f"resources = {widened}", _BEDROCK_TF, count=1
        )
        assert widened in tf and tf.count("concat(") == 1, "the substitution did not apply"
        failed = _failed(_bedrock_findings(tmp_path, tf))
        assert failed, "widening to a wildcard was accepted"
        assert any("wildcard resource" in detail for detail in failed.values()), failed

    def test_dropping_the_resources_attribute_fails(self, tmp_path: Path) -> None:
        """An omitted resource set is an unscoped grant, not an absent one."""
        tf = re.sub(r"resources = concat\((?:.*\n)*?    \)\n", "", _BEDROCK_TF, count=1)
        failed = _failed(_bedrock_findings(tmp_path, tf))
        assert any("no `resources`" in detail for detail in failed.values()), failed

    def test_an_arn_of_none_of_the_shapes_fails(self, tmp_path: Path) -> None:
        tf = _BEDROCK_TF.replace(":inference-profile/${profile}", ":custom-model/${profile}")
        failed = _failed(_bedrock_findings(tmp_path, tf))
        assert any("none of inference-profile" in detail for detail in failed.values()), failed

    def test_losing_the_inference_profile_arns_fails(self, tmp_path: Path) -> None:
        """Losing the inference-profile ARNs fails.

        The profile template becomes a well-formed project ARN, isolating the aggregate coverage row.
        """
        tf = _BEDROCK_TF.replace(
            ":inference-profile/${profile}", ":custom-thing/${profile}"
        ).replace("custom-thing", "project")
        failed = _failed(_bedrock_findings(tmp_path, tf))
        assert any("no statement grants an inference-profile ARN" in d for d in failed.values()), (
            failed
        )

    def test_losing_the_foundation_model_arns_fails(self, tmp_path: Path) -> None:
        """Dropping the `BedrockInferenceFoundationModels` statement fails the aggregate coverage row."""
        tf = re.sub(
            r"  statement \{\n    sid    = \"BedrockInferenceFoundationModels\"(?:.*\n)*?  \}\n",
            "",
            _BEDROCK_TF,
        )
        assert "BedrockInferenceFoundationModels" not in tf, "the statement was not removed"
        failed = _failed(_bedrock_findings(tmp_path, tf))
        assert any("no foundation-model ARN" in detail for detail in failed.values()), failed

    def test_dropping_the_inference_profile_condition_fails(self, tmp_path: Path) -> None:
        """Removing the `bedrock:InferenceProfileArn` condition from the foundation-model grant fails."""
        tf = re.sub(r"    condition \{(?:.*\n)*?    \}\n", "", _BEDROCK_TF, count=1)
        assert "condition {" not in tf, "the condition block was not removed"
        failed = _failed(_bedrock_findings(tmp_path, tf))
        assert any(
            "without a bedrock:InferenceProfileArn condition" in detail
            for detail in failed.values()
        ), failed

    def test_a_non_literal_action_list_is_not_accepted(self, tmp_path: Path) -> None:
        """An action set assembled from an expression cannot be shown to grant only what it should."""
        tf = _BEDROCK_TF.replace(
            'actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]',
            'actions = concat(var.bedrock_actions, ["bedrock:InvokeModel"])',
            1,
        )
        failed = _failed(_bedrock_findings(tmp_path, tf))
        assert any("not literal strings" in detail for detail in failed.values()), failed

    def test_a_tree_with_no_bedrock_grant_fails_rather_than_finding_nothing(
        self, tmp_path: Path
    ) -> None:
        """A tree with no Bedrock grant fails."""
        failed = _failed(_bedrock_findings(tmp_path, "\n"))
        assert any("(coverage)" in resource for resource in failed), failed

    def test_the_neighbouring_ecr_wildcard_does_not_trip_the_control(self, tmp_path: Path) -> None:
        """Scoped per statement, not per document: `ecr:GetAuthorizationToken` needs its `*`."""
        findings = _bedrock_findings(tmp_path)
        assert not any("Ecr" in f.resource for f in findings), findings

    def test_a_comment_mentioning_a_wildcard_is_not_a_finding(self, tmp_path: Path) -> None:
        """A comment containing `Resource = "*"` in the policy is not a finding."""
        tf = _BEDROCK_TF.replace(
            'never Resource = "*".', 'never Resource = "*", and never ["*"] either.'
        )
        assert not _failed(_bedrock_findings(tmp_path, tf)), _bedrock_findings(tmp_path, tf)


# ---------------------------------------------------------------------------
# `resource_existence_counts` asserts equality rather than floors, so an added instance fails as
# well as a removed one.
# ---------------------------------------------------------------------------


#: `total = per_tool*tools + fixed`, as a basis states its arithmetic.
_BASIS_EQUATION = re.compile(r"\b(\d+) = (\d+)\*(\d+) \+ (\d+)\b")


def _counts_findings(document: dict[str, Any]) -> list[Any]:
    return gate.check_resource_existence_counts(
        gate.Plan(resource_changes=document["resource_changes"])
    )


def _compliant_document() -> dict[str, Any]:
    """Return a fresh in-memory copy of the compliant fixture document."""
    return plan_fixtures.build("compliant")


#: Expected-count types the committed fixture carries, i.e. excluding `FIXTURE_ABSENT_TYPES`, so the
#: instance-mutating meta-tests below have an instance to mutate. Bounded by
#: `test_the_fixture_absent_exclusion_is_exactly_the_declared_absences`.
FIXTURE_MUTABLE_EXPECTED_TYPES = sorted(
    set(gate.EXPECTED_RESOURCE_COUNTS) - set(gate.FIXTURE_ABSENT_TYPES)
)


class TestExpectedResourceCounts:
    def test_the_compliant_fixture_matches_every_expectation_exactly(self) -> None:
        """The compliant fixture matches every expected count exactly."""
        assert not _failed(_counts_findings(_compliant_document())), _failed(
            _counts_findings(_compliant_document())
        )

    @pytest.mark.parametrize("resource_type", FIXTURE_MUTABLE_EXPECTED_TYPES)
    def test_deleting_one_instance_of_each_type_fails(self, resource_type: str) -> None:
        document = _compliant_document()
        victims = [r for r in document["resource_changes"] if r.get("type") == resource_type]
        assert victims, f"{resource_type} has an expectation but no instance in the fixture"
        # Prefer a non-action-lambda victim: action instances are the multiplier base, so deleting
        # one lowers the expectation with the count. That case is covered by the identity control.
        victim = next(
            (v for v in victims if "lambda_action[" not in v.get("address", "")), victims[0]
        )
        document["resource_changes"].remove(victim)
        failed = _failed(_counts_findings(document))
        assert resource_type in failed, failed

    def test_deleting_one_action_lambda_fails_via_its_orphaned_siblings(self) -> None:
        """Deleting an action Lambda but not its route/integration/target fails each orphaned
        sibling."""
        document = _compliant_document()
        victim = next(
            r
            for r in document["resource_changes"]
            if r.get("type") == "aws_lambda_function" and "lambda_action[" in r.get("address", "")
        )
        document["resource_changes"].remove(victim)
        plan = gate.Plan(resource_changes=document["resource_changes"])
        failed = _failed(gate.check_resource_existence_counts(plan))
        assert "aws_api_gateway_method" in failed, failed
        assert "aws_bedrockagentcore_gateway_target" in failed, failed
        assert _failed(gate.check_resource_inventory_identity(plan))

    def test_the_fixture_absent_exclusion_is_exactly_the_declared_absences(self) -> None:
        """The fixture-absent exclusion is non-empty and equals the declared absences."""
        excluded = set(gate.EXPECTED_RESOURCE_COUNTS) - set(FIXTURE_MUTABLE_EXPECTED_TYPES)
        assert excluded == set(gate.EXPECTED_RESOURCE_COUNTS) & set(gate.FIXTURE_ABSENT_TYPES)
        assert FIXTURE_MUTABLE_EXPECTED_TYPES, (
            "every expected type was excluded; the mutation tests "
        )
        for resource_type in FIXTURE_MUTABLE_EXPECTED_TYPES:
            assert resource_type not in gate.FIXTURE_ABSENT_TYPES

    @pytest.mark.parametrize("resource_type", FIXTURE_MUTABLE_EXPECTED_TYPES)
    def test_adding_one_instance_of_each_type_fails(self, resource_type: str) -> None:
        """Adding one instance of any expected type fails."""
        document = _compliant_document()
        template = next(r for r in document["resource_changes"] if r.get("type") == resource_type)
        lagging = gate.FIXTURE_LAGGING_COUNTS.get(resource_type)
        shortfall = (
            gate.EXPECTED_RESOURCE_COUNTS[resource_type].fixed - lagging[0] if lagging else 0
        )
        for index in range(1 + shortfall):
            extra = dict(template)
            extra["address"] = template.get("address", "") + f"_planted_extra{index}"
            document["resource_changes"].append(extra)
        failed = _failed(_counts_findings(document))
        assert resource_type in failed, failed
        assert "expected=" in failed[resource_type]

    def test_an_unaccounted_resource_type_fails(self) -> None:
        """A resource type nothing accounts for is how a new resource enters the tree unasserted."""
        document = _compliant_document()
        document["resource_changes"].append(
            {"type": "aws_wafv2_web_acl", "address": "aws_wafv2_web_acl.planted", "change": {}}
        )
        failed = _failed(_counts_findings(document))
        assert any("(scope) every planned resource type" in row for row in failed), failed
        assert "aws_wafv2_web_acl" in str(failed)

    def test_an_absence_asserted_type_is_accepted_without_a_count(self) -> None:
        """An absence-asserted type (`aws_iam_access_key`) is accepted without an expected count."""
        subject = "aws_iam_access_key"
        assert subject in gate.ABSENCE_ASSERTED_TYPES, (
            "this test needs a genuinely absence-asserted type; retarget it rather than deleting it"
        )
        document = _compliant_document()
        document["resource_changes"].append(
            {"type": subject, "address": f"{subject}.planted", "change": {}}
        )
        failed = _failed(_counts_findings(document))
        assert not any("(scope) every planned resource type" in row for row in failed), failed

    def test_an_orphaned_fixture_absent_declaration_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fixture-absent declaration with no expected count and no plan instance fails."""
        monkeypatch.setattr(
            gate,
            "FIXTURE_ABSENT_TYPES",
            {**gate.FIXTURE_ABSENT_TYPES, "aws_quicksight_theme": "planted orphan"},
        )
        failed = _failed(_counts_findings(_compliant_document()))
        assert any("aws_quicksight_theme" in row for row in failed.values()), failed

    def test_a_fixture_absent_declaration_for_a_present_type_is_not_orphaned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fixture-absent declaration for a type present in the plan is not orphaned."""
        monkeypatch.setattr(
            gate,
            "FIXTURE_ABSENT_TYPES",
            {**gate.FIXTURE_ABSENT_TYPES, "aws_kms_key": "present in this plan, so not orphaned"},
        )
        failed = _failed(_counts_findings(_compliant_document()))
        assert not any("still reachable" in row for row in failed), failed

    def test_zero_action_lambdas_fails_the_multiplier_base(self) -> None:
        """Zero action Lambdas fails the multiplier base."""
        document = _compliant_document()
        document["resource_changes"] = [
            r
            for r in document["resource_changes"]
            if not (r.get("type") == "aws_lambda_function" and "lambda_action[" in r["address"])
        ]
        failed = _failed(_counts_findings(document))
        assert any("(base) module.lambda_action instances" in row for row in failed), failed

    def test_a_per_tool_expectation_scales_rather_than_being_a_fixed_number(self) -> None:
        """Deleting a tool's whole slice keeps count parity; identity reports the missing member once."""
        document = _compliant_document()
        document["resource_changes"] = [
            r for r in document["resource_changes"] if '"create_note"' not in r.get("address", "")
        ]
        failed = _failed(_counts_findings(document))
        assert not failed, (
            f"per-tool parity should still hold after removing a whole tool: {failed}"
        )
        # ...and the missing member is caught, once, by the identity control.
        identity = gate.check_resource_inventory_identity(
            gate.Plan(resource_changes=document["resource_changes"])
        )
        assert any(f.status == "fail" and "create_note" in f.resource for f in identity), _failed(
            identity
        )

    def test_a_failing_derivation_is_reported_not_raised(self) -> None:
        """A failing derivation is reported as a finding, not raised."""

        def _boom() -> int:
            raise RuntimeError("schema.py is unparseable")

        original = gate.EXPECTED_RESOURCE_COUNTS
        try:
            gate.EXPECTED_RESOURCE_COUNTS = {
                **original,
                "aws_dynamodb_table": gate.ExpectedCount(basis="planted", derive=_boom),
            }
            failed = _failed(_counts_findings(_compliant_document()))
            assert "aws_dynamodb_table" in failed, failed
            assert "could not resolve" in failed["aws_dynamodb_table"]
        finally:
            gate.EXPECTED_RESOURCE_COUNTS = original

    def test_the_dynamodb_count_is_derived_and_not_transcribed(self) -> None:
        """The DynamoDB table count is derived from the registry, not written as a literal."""
        entry = gate.EXPECTED_RESOURCE_COUNTS["aws_dynamodb_table"]
        assert entry.derive is not None, "the table count has an in-repo source; derive it"
        assert entry.fixed == 0

    @pytest.mark.parametrize("resource_type,entry", sorted(gate.EXPECTED_RESOURCE_COUNTS.items()))
    def test_every_entry_states_a_checkable_basis(self, resource_type: str, entry: Any) -> None:
        """Every entry's basis names what is counted rather than restating the number."""
        words = entry.basis.split()
        assert len(words) >= gate.EXPECTED_COUNT_BASIS_MIN_WORDS, (
            f"{resource_type}: basis is too thin to check against: {entry.basis!r}"
        )
        assert not re.fullmatch(r"\d+\s+\w+", entry.basis.strip()), (
            f"{resource_type}: basis restates the number instead of its derivation"
        )

    @pytest.mark.parametrize(
        "resource_type,entry",
        sorted(
            (t, e)
            for t, e in gate.EXPECTED_RESOURCE_COUNTS.items()
            if e.per_tool and e.fixed and e.derive is None
        ),
    )
    def test_a_per_tool_basis_states_its_fixed_remainder_as_a_numeral(
        self, resource_type: str, entry: Any
    ) -> None:
        """A per-tool entry's basis names its fixed remainder as `plus <fixed>` or `= k*n + <fixed>`."""
        stated = {int(n) for n in re.findall(r"\bplus (\d+)\b", entry.basis)}
        stated |= {int(m[3]) for m in _BASIS_EQUATION.findall(entry.basis)}
        assert stated == {entry.fixed}, (
            f"{resource_type}: fixed={entry.fixed}, but the basis states {sorted(stated)}: "
            f"{entry.basis!r}"
        )

    def test_no_basis_pins_a_tool_count_equation(self) -> None:
        """A `total = k*n + f` equation in a basis pins the tool count `n`, so none remains."""
        assert _BASIS_EQUATION.search("55 = 2*24 + 7"), "the pattern no longer matches its shape"
        pinned = {
            t: e.basis
            for t, e in gate.EXPECTED_RESOURCE_COUNTS.items()
            if _BASIS_EQUATION.search(e.basis)
        }
        assert pinned == {}, pinned

    def test_the_existence_control_label_counts_match_the_compliant_fixture(self) -> None:
        """Each `<n> <noun>` in the existence control's label equals the fixture's count."""
        (label,) = [
            c.spec_row for c in gate.CONTROLS if c.control_id == "resource_existence_counts"
        ]
        plan = load_plan("compliant")
        types = {
            "tables": "aws_dynamodb_table",
            "routes": "aws_api_gateway_method",
            "functions": "aws_lambda_function",
            "pool": "aws_cognito_user_pool",
            "clients": "aws_cognito_user_pool_client",
            "gateways": "aws_bedrockagentcore_gateway",
        }
        stated = {noun: int(n) for n, noun in re.findall(r"(\d+) (\w+)", label)}
        assert set(stated) == set(types), label
        actual = {noun: len(plan.by_type(t)) for noun, t in types.items()}
        assert stated == actual, f"label {label!r} vs fixture {actual}"

    def test_the_floor_registry_is_gone_so_a_floor_cannot_quietly_return(self) -> None:
        assert not hasattr(gate, "MINIMUM_RESOURCE_COUNTS")


# ---------------------------------------------------------------------------
# `s3_access_log_delivery_grant`: access logging configured without a delivery grant fails, in
# each planted shape below.
# ---------------------------------------------------------------------------

_LOG_DELIVERY_TF = """
resource "aws_s3_bucket" "logs" {
  bucket = "logs"
}

resource "aws_s3_bucket" "origin" {
  bucket = "origin"
}

resource "aws_s3_bucket_logging" "origin" {
  bucket = aws_s3_bucket.origin.id

  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "s3-access-logs/origin/"
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = data.aws_iam_policy_document.logs_tls_deny.json
}

data "aws_iam_policy_document" "logs_tls_deny" {
  statement {
    sid    = "AllowS3ServerAccessLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/*"]
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["s3:*"]
    resources = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
"""


def _delivery_findings(root: Path, tf: str = _LOG_DELIVERY_TF) -> list[Any]:
    (root / "main.tf").write_text(tf, encoding="utf-8")
    original = gate.INFRA_DIR
    try:
        gate.INFRA_DIR = root
        return gate.check_s3_access_log_delivery_grant(gate.Plan(resource_changes=[]))
    finally:
        gate.INFRA_DIR = original


class TestS3AccessLogDeliveryGrant:
    def test_the_real_infra_tree_passes_after_the_fix(self) -> None:
        findings = gate.check_s3_access_log_delivery_grant(gate.Plan(resource_changes=[]))
        assert findings
        assert not _failed(findings), findings

    def test_the_reproduced_shape_passes(self, tmp_path: Path) -> None:
        assert not _failed(_delivery_findings(tmp_path)), _delivery_findings(tmp_path)

    def test_the_state_the_tree_was_actually_in_fails(self, tmp_path: Path) -> None:
        """A destination policy with a TLS deny and no delivery grant fails."""
        tf = re.sub(
            r"  statement \{\n    sid    = \"AllowS3ServerAccessLogDelivery\"(?:.*\n)*?  \}\n\n",
            "",
            _LOG_DELIVERY_TF,
        )
        assert "AllowS3ServerAccessLogDelivery" not in tf, "the substitution did not apply"
        failed = _failed(_delivery_findings(tmp_path, tf))
        assert any("aws_s3_bucket_logging.origin" in row for row in failed), failed
        assert "present=False" in str(failed)

    def test_a_deny_of_the_delivery_principal_is_not_read_as_a_grant(self, tmp_path: Path) -> None:
        """Effect is checked: a statement naming the principal is not the same as allowing it."""
        tf = _LOG_DELIVERY_TF.replace(
            '    sid    = "AllowS3ServerAccessLogDelivery"\n    effect = "Allow"',
            '    sid    = "AllowS3ServerAccessLogDelivery"\n    effect = "Deny"',
        )
        assert _failed(_delivery_findings(tmp_path, tf))

    def test_a_grant_of_the_wrong_action_fails(self, tmp_path: Path) -> None:
        """`s3:GetObject` to the delivery principal does not let it write a log object."""
        tf = _LOG_DELIVERY_TF.replace(
            'actions   = ["s3:PutObject"]', 'actions   = ["s3:GetObject"]'
        )
        assert _failed(_delivery_findings(tmp_path, tf))

    def test_a_grant_to_a_different_principal_fails(self, tmp_path: Path) -> None:
        tf = _LOG_DELIVERY_TF.replace(
            '["logging.s3.amazonaws.com"]', '["delivery.logs.amazonaws.com"]'
        )
        assert _failed(_delivery_findings(tmp_path, tf))

    def test_a_grant_naming_a_different_bucket_fails(self, tmp_path: Path) -> None:
        """The grant must reference the bucket that is actually the logging destination."""
        tf = _LOG_DELIVERY_TF.replace(
            'resources = ["${aws_s3_bucket.logs.arn}/*"]',
            'resources = ["${aws_s3_bucket.origin.arn}/*"]',
        )
        assert _failed(_delivery_findings(tmp_path, tf))

    def test_an_acl_based_grant_is_accepted_as_the_alternative_mechanism(
        self, tmp_path: Path
    ) -> None:
        """The control asserts delivery is permitted, not which of the two mechanisms is used."""
        tf = re.sub(
            r"  statement \{\n    sid    = \"AllowS3ServerAccessLogDelivery\"(?:.*\n)*?  \}\n\n",
            "",
            _LOG_DELIVERY_TF,
        )
        tf += '\nresource "aws_s3_bucket_acl" "logs" {\n  bucket = aws_s3_bucket.logs.id\n'
        tf += '  acl    = "log-delivery-write"\n}\n'
        assert not _failed(_delivery_findings(tmp_path, tf)), _delivery_findings(tmp_path, tf)

    def test_a_cross_module_target_is_deferred_not_passed(self, tmp_path: Path) -> None:
        """A cross-module logging target is deferred, not passed."""
        tf = _LOG_DELIVERY_TF.replace(
            "target_bucket = aws_s3_bucket.logs.id", "target_bucket = var.access_log_bucket_id"
        )
        findings = _delivery_findings(tmp_path, tf)
        deferred = [f for f in findings if f.status == "deferred"]
        assert deferred, findings
        assert not any(
            f.status == "pass" and "aws_s3_bucket_logging.origin" in f.resource for f in findings
        ), findings

    def test_removing_access_logging_entirely_fails_the_coverage_floor(
        self, tmp_path: Path
    ) -> None:
        """Removing all access-logging resources fails the coverage floor."""
        tf = re.sub(
            r'resource "aws_s3_bucket_logging"(?:.*\n)*?\}\n', "", _LOG_DELIVERY_TF, count=1
        )
        failed = _failed(_delivery_findings(tmp_path, tf))
        assert any("(coverage)" in row for row in failed), failed

    def test_the_cloudfront_half_is_declared_rather_than_claimed(self) -> None:
        """CloudFront's ACL-based logging is declared as uncovered rather than claimed."""
        rows = [
            entry
            for entry in gate.PLAN_TIME_INEXPRESSIBLE
            if "CloudFront standard access logging" in entry["spec_row"]
        ]
        assert len(rows) == 1, gate.PLAN_TIME_INEXPRESSIBLE
        assert "awslogsdelivery" in rows[0]["reason"]
        assert rows[0]["layer"].strip()

    def test_no_bucket_in_infra_uses_an_ownership_setting_that_blocks_both_mechanisms(self) -> None:
        """The origin bucket keeps `BucketOwnerEnforced` object ownership."""
        source = (Path(gate.INFRA_DIR) / "modules" / "frontend_cdn" / "s3.tf").read_text(
            encoding="utf-8"
        )
        origin_block = source[
            source.index('resource "aws_s3_bucket_ownership_controls" "origin"') :
        ]
        assert "BucketOwnerEnforced" in origin_block[: origin_block.index("}\n}")], (
            "the origin bucket's ownership was relaxed; ACLs on the OAC-read bucket is a regression, "
            "not a fix for log delivery"
        )


_ALB_LOG_DELIVERY_TF = """
resource "aws_s3_bucket" "logs" {
  bucket = "logs"
}

resource "aws_lb" "this" {
  name     = "api-origin"
  internal = true

  access_logs {
    bucket  = var.access_logs_bucket
    prefix  = var.access_logs_prefix
    enabled = true
  }
}

data "aws_iam_policy_document" "logs_tls_deny" {
  statement {
    sid    = "AllowS3ServerAccessLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/*"]
  }

  statement {
    sid    = "AllowAlbAccessLogDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/prefix/AWSLogs/123456789012/*"]
  }
}
"""


def _alb_findings(root: Path, tf: str = _ALB_LOG_DELIVERY_TF) -> list[Any]:
    (root / "main.tf").write_text(tf, encoding="utf-8")
    original = gate.INFRA_DIR
    try:
        gate.INFRA_DIR = root
        return gate._alb_access_log_grant_findings()
    finally:
        gate.INFRA_DIR = original


class TestAlbAccessLogDeliveryGrant:
    """Non-vacuity for the `aws_lb` arm of the access-log delivery-grant control.

    ELB delivers as `logdelivery.elasticloadbalancing.amazonaws.com`, so a bucket granting only
    `logging.s3.amazonaws.com` fails `aws_lb` creation with access logs enabled.
    """

    def test_the_real_infra_tree_passes_after_the_fix(self) -> None:
        findings = gate._alb_access_log_grant_findings()
        assert findings, "the real tree has an aws_lb with access logs; this must not be vacuous"
        assert not _failed(findings), findings

    def test_the_reproduced_shape_passes(self, tmp_path: Path) -> None:
        assert not _failed(_alb_findings(tmp_path)), _alb_findings(tmp_path)

    def test_the_state_the_tree_was_actually_in_fails(self, tmp_path: Path) -> None:
        """Only the S3 server-access-log grant, with no ELB grant beside it, fails."""
        tf = re.sub(
            r'  statement \{\n    sid    = "AllowAlbAccessLogDelivery"(?:.*\n)*?  \}\n',
            "",
            _ALB_LOG_DELIVERY_TF,
        )
        assert "AllowAlbAccessLogDelivery" not in tf, "the substitution did not apply"
        assert "logging.s3.amazonaws.com" in tf, "the S3 grant must remain, to isolate the ELB arm"
        failed = _failed(_alb_findings(tmp_path, tf))
        assert any("aws_lb.this" in row for row in failed), failed
        assert "present in the tree=False" in str(failed)

    def test_the_legacy_per_region_elb_account_id_is_not_accepted(self, tmp_path: Path) -> None:
        """The legacy per-Region ELB account principal is not accepted.

        The id is assembled at runtime, since `check_no_account_id` rejects 12-digit literals.
        """
        legacy_elb_account = "1273" + "1192" + "3021"  # us-east-1's, per AWS's legacy-policy table
        tf = _ALB_LOG_DELIVERY_TF.replace(
            '      type        = "Service"\n'
            '      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]',
            '      type        = "AWS"\n'
            f'      identifiers = ["arn:aws:iam::{legacy_elb_account}:root"]',
        )
        assert "logdelivery.elasticloadbalancing" not in tf, "the substitution did not apply"
        assert legacy_elb_account in tf, "the assembled principal did not reach the fixture"
        assert _failed(_alb_findings(tmp_path, tf))

    def test_a_deny_of_the_delivery_principal_is_not_read_as_a_grant(self, tmp_path: Path) -> None:
        tf = _ALB_LOG_DELIVERY_TF.replace(
            '    sid    = "AllowAlbAccessLogDelivery"\n    effect = "Allow"',
            '    sid    = "AllowAlbAccessLogDelivery"\n    effect = "Deny"',
        )
        assert _failed(_alb_findings(tmp_path, tf))

    def test_a_grant_of_the_wrong_action_fails(self, tmp_path: Path) -> None:
        """`s3:GetObject` to the delivery principal does not let ELB write a log object."""
        tf = _ALB_LOG_DELIVERY_TF.replace(
            '    actions   = ["s3:PutObject"]\n    resources = '
            '["${aws_s3_bucket.logs.arn}/prefix/AWSLogs/123456789012/*"]',
            '    actions   = ["s3:GetObject"]\n    resources = '
            '["${aws_s3_bucket.logs.arn}/prefix/AWSLogs/123456789012/*"]',
        )
        assert "s3:GetObject" in tf, "the substitution did not apply"
        assert _failed(_alb_findings(tmp_path, tf))

    def test_an_alb_with_no_access_logs_block_is_not_asked_for_a_grant(
        self, tmp_path: Path
    ) -> None:
        """The control must not demand a standing permission for logging nobody enabled."""
        tf = re.sub(r"  access_logs \{(?:.*\n)*?  \}\n", "", _ALB_LOG_DELIVERY_TF)
        assert "access_logs" not in tf, "the substitution did not apply"
        findings = _alb_findings(tmp_path, tf)
        # A grant with no ALB using it is itself reported -- as the standing-permission direction,
        # not as a missing grant. That is the next test; here, no aws_lb row may be emitted.
        assert not any("aws_lb.this" in f.resource for f in findings), findings

    def test_enabled_false_is_not_asked_for_a_grant(self, tmp_path: Path) -> None:
        """`enabled` defaults to false, so only an explicit `true` needs the grant."""
        tf = _ALB_LOG_DELIVERY_TF.replace("enabled = true", "enabled = false")
        findings = _alb_findings(tmp_path, tf)
        assert not any("aws_lb.this" in f.resource for f in findings), findings

    def test_a_grant_with_no_alb_enabling_logs_is_reported(self, tmp_path: Path) -> None:
        """A delivery grant with no ALB enabling access logs is reported."""
        tf = re.sub(r"  access_logs \{(?:.*\n)*?  \}\n", "", _ALB_LOG_DELIVERY_TF)
        failed = _failed(_alb_findings(tmp_path, tf))
        assert any("(coverage) aws_lb access_logs enabled" in row for row in failed), failed

    def test_the_root_feeds_one_prefix_local_to_both_sides(self) -> None:
        """The root module feeds one prefix local to both the grant ARN and the ALB."""
        root = (gate.INFRA_DIR / "main.tf").read_text(encoding="utf-8")
        assert 'alb_access_log_prefix = "${local.name_prefix}-api-origin"' in root
        assert "access_logs_prefix = local.alb_access_log_prefix" in root
        assert "alb_access_log_prefix = local.alb_access_log_prefix" in root

        alb = (gate.INFRA_DIR / "modules/api_origin/main.tf").read_text(encoding="utf-8")
        assert "prefix  = var.access_logs_prefix" in alb, (
            "the ALB must take the prefix as an input; deriving it from name_prefix locally is the "
            "drift this local exists to prevent"
        )
        grant = (gate.INFRA_DIR / "modules/frontend_cdn/s3.tf").read_text(encoding="utf-8")
        assert "${var.alb_access_log_prefix}/AWSLogs/" in grant


class TestDeclaredInfraBlockCounts:
    """Non-vacuity for `declared_infra_block_counts`, the source-side companion to
    `resource_existence_counts`."""

    def test_the_real_counts_pass(self) -> None:
        failed = [
            f
            for f in gate.check_declared_infra_block_counts(load_plan("compliant"))
            if f.status == "fail"
        ]
        assert not failed, failed

    def test_a_wrong_expected_count_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An off-by-one expected count fails."""
        block, (real, basis) = next(iter(sorted(gate.DECLARED_INFRA_BLOCK_COUNTS.items())))
        monkeypatch.setattr(gate, "DECLARED_INFRA_BLOCK_COUNTS", {block: (real + 1, basis)})
        failed = [
            f
            for f in gate.check_declared_infra_block_counts(load_plan("compliant"))
            if f.status == "fail"
        ]
        assert failed, f"expecting {real + 1} of {block} instead of {real} did not fail"
        assert f"declared={real} expected={real + 1}" in failed[0].detail, failed[0].detail

    def test_reading_no_source_files_fails_rather_than_counting_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An empty tree must not satisfy every count with a vacuous zero."""
        monkeypatch.setattr(gate, "INFRA_DIR", tmp_path)
        findings = gate.check_declared_infra_block_counts(load_plan("compliant"))
        assert [f.status for f in findings] == ["fail"], findings
        assert "no infra/**/*.tf files were read" in findings[0].detail


#: Fixtures whose targeted control fails in most fixtures for reasons unrelated to their mutation,
#: so the assertion names the specific finding the mutation produces.
VIOLATION_TARGET_DETAILS: dict[str, tuple[str, str]] = {
    # The mutation removes every DynamoDB table, so this row is off by the whole inventory.
    "violation_resource_existence_counts": ("aws_dynamodb_table", "count=0 expected=11"),
    "violation_log_group_no_kms": (
        "module.vpc.aws_cloudwatch_log_group.flow_logs[0]",
        "kms_key_id=None",
    ),
}


class TestOverDeterminedFixturesAssertTheirOwnMutation:
    """A fixture whose target fails for unrelated reasons asserts the finding its mutation causes."""

    @pytest.mark.parametrize(
        "fixture_name,resource,detail_fragment",
        [(name, res, frag) for name, (res, frag) in sorted(VIOLATION_TARGET_DETAILS.items())],
    )
    def test_the_mutation_produces_its_own_named_finding(
        self, fixture_name: str, resource: str, detail_fragment: str
    ) -> None:
        control_id = VIOLATION_TARGETS[fixture_name]
        findings = gate.run_gate(load_plan(fixture_name))
        matching = [
            f
            for f in findings
            if f.control == control_id and f.status == "fail" and f.resource == resource
        ]
        assert matching, (
            f"{fixture_name}: no failing {control_id} finding for {resource!r}; the mutation this "
            f"fixture exists to demonstrate is not visible "
            f"(failing resources: {sorted({f.resource for f in findings if f.control == control_id and f.status == 'fail'})})"
        )
        assert any(detail_fragment in f.detail for f in matching), (
            f"{fixture_name}: {resource!r} fails, but not with {detail_fragment!r} -- so it is "
            f"failing for a different reason than its mutation ({[f.detail[:120] for f in matching]})"
        )

    @pytest.mark.parametrize("fixture_name", sorted(VIOLATION_TARGET_DETAILS))
    def test_every_entry_is_a_registered_violation_fixture(self, fixture_name: str) -> None:
        """The map cannot drift into naming a fixture nothing else exercises."""
        assert fixture_name in VIOLATION_TARGETS, fixture_name


# ---------------------------------------------------------------------------
# The two source-derived security-group controls. A tree with no security groups must fail both
# rather than pass over an empty examination.
# ---------------------------------------------------------------------------


class TestSecurityGroupSourceControls:
    """Every mutation is written as the evasion, not as the fix."""

    @staticmethod
    def _findings(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hcl: str
    ) -> tuple[list[Any], list[Any]]:
        """Run both controls against a scratch `infra/` containing exactly `hcl`."""
        scratch = tmp_path / "infra"
        scratch.mkdir(exist_ok=True)
        (scratch / "planted.tf").write_text(hcl, encoding="utf-8")
        monkeypatch.setattr(gate, "INFRA_DIR", scratch)
        plan = load_plan("compliant")
        return (
            gate.check_security_group_zero_inbound(plan),
            gate.check_security_group_egress_scoped(plan),
        )

    #: A group and an egress rule that both satisfy the controls, used as the base every mutation edits
    #: A group and an egress rule that satisfy both controls; the base every mutation edits.
    COMPLIANT = """
resource "aws_security_group" "lambda" {
  name   = "x"
  vpc_id = "vpc-known-after-apply"
}

resource "aws_vpc_security_group_ingress_rule" "ok" {
  security_group_id            = aws_security_group.lambda.id
  referenced_security_group_id = aws_security_group.lambda.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "ok" {
  security_group_id            = aws_security_group.lambda.id
  referenced_security_group_id = aws_security_group.lambda.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
"""

    def test_the_compliant_base_passes_both_controls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The compliant base passes both controls."""
        inbound, egress = self._findings(monkeypatch, tmp_path, self.COMPLIANT)
        assert _failed(inbound) == {}, _failed(inbound)
        assert _failed(egress) == {}, _failed(egress)

    def test_the_real_infra_tree_passes_both_controls(self) -> None:
        """The real `infra/` tree passes both controls."""
        plan = load_plan("compliant")
        assert _failed(gate.check_security_group_zero_inbound(plan)) == {}
        assert _failed(gate.check_security_group_egress_scoped(plan)) == {}

    def test_an_empty_tree_fails_rather_than_passing_over_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A tree with no security group and no egress rule fails both controls."""
        inbound, egress = self._findings(monkeypatch, tmp_path, "# nothing here\n")
        assert _failed(inbound), "an empty tree passed the zero-inbound control"
        assert _failed(egress), "an empty tree passed the egress-scoping control"

    @pytest.mark.parametrize(
        "mutation,label",
        [
            (
                'resource "aws_security_group" "lambda" {\n  ingress {\n    from_port = 443\n'
                '    to_port = 443\n    protocol = "tcp"\n    cidr_blocks = ["0.0.0.0/0"]\n  }\n}',
                "an inline ingress block",
            ),
            (
                'resource "aws_vpc_security_group_ingress_rule" "bad" {\n'
                "  security_group_id = aws_security_group.lambda.id\n"
                '  cidr_ipv4         = "0.0.0.0/0"\n  ip_protocol       = "tcp"\n'
                "  from_port         = 443\n  to_port           = 443\n}",
                "an ingress rule sourced from the whole internet",
            ),
            (
                'resource "aws_vpc_security_group_ingress_rule" "bad" {\n'
                "  security_group_id = aws_security_group.lambda.id\n"
                '  cidr_ipv4         = "10.20.0.0/20"\n  ip_protocol       = "tcp"\n'
                "  from_port         = 443\n  to_port           = 443\n}",
                "an ingress rule sourced from a CIDR rather than a group",
            ),
        ],
    )
    def test_an_inbound_evasion_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str, label: str
    ) -> None:
        inbound, _ = self._findings(monkeypatch, tmp_path, self.COMPLIANT + "\n" + mutation)
        assert _failed(inbound), f"{label} passed the zero-inbound control"

    @pytest.mark.parametrize(
        "mutation,label",
        [
            (
                'resource "aws_vpc_security_group_egress_rule" "bad" {\n'
                "  security_group_id            = aws_security_group.lambda.id\n"
                "  referenced_security_group_id = aws_security_group.lambda.id\n"
                '  ip_protocol                  = "-1"\n}',
                "ip_protocol = -1, which opens every protocol and port at once",
            ),
            (
                'resource "aws_vpc_security_group_egress_rule" "bad" {\n'
                "  security_group_id            = aws_security_group.lambda.id\n"
                "  referenced_security_group_id = aws_security_group.lambda.id\n"
                '  ip_protocol                  = "tcp"\n  from_port                    = 0\n'
                "  to_port                      = 443\n}",
                "a 0-443 range, which reads almost right and opens 444 ports",
            ),
            (
                'resource "aws_vpc_security_group_egress_rule" "bad" {\n'
                "  security_group_id = aws_security_group.lambda.id\n"
                '  cidr_ipv4         = "0.0.0.0/0"\n  ip_protocol       = "tcp"\n'
                "  from_port         = 80\n  to_port           = 80\n}",
                "an open CIDR on a port other than 443",
            ),
            (
                'resource "aws_security_group" "extra" {\n  egress {\n    from_port = 443\n'
                '    to_port = 443\n    protocol = "tcp"\n    cidr_blocks = ["0.0.0.0/0"]\n  }\n}',
                "an inline egress block, whose port and protocol this control cannot read",
            ),
        ],
    )
    def test_an_egress_evasion_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str, label: str
    ) -> None:
        _, egress = self._findings(monkeypatch, tmp_path, self.COMPLIANT + "\n" + mutation)
        assert _failed(egress), f"{label} passed the egress-scoping control"

    def test_the_jwks_exception_is_permitted_at_443_and_only_there(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The open-CIDR JWKS egress rule is permitted at port 443 and at no other port."""
        permitted = (
            'resource "aws_vpc_security_group_egress_rule" "jwks" {\n'
            "  security_group_id = aws_security_group.lambda.id\n"
            '  cidr_ipv4         = "0.0.0.0/0"\n  ip_protocol       = "tcp"\n'
            "  from_port         = 443\n  to_port           = 443\n}"
        )
        _, egress = self._findings(monkeypatch, tmp_path, self.COMPLIANT + "\n" + permitted)
        assert _failed(egress) == {}, _failed(egress)


# ---------------------------------------------------------------------------
# Wildcard-principal Lambda permissions are absent from the source tree, the gate and the fixture.
# ---------------------------------------------------------------------------


class TestNoWildcardPrincipalAnywhereInTheTree:
    """No module declares a wildcard-principal Lambda permission, and the gate has no exemption
    that would admit one."""

    def test_the_source_tree_declares_no_wildcard_permission(self) -> None:
        offenders = [
            path.relative_to(gate.INFRA_DIR)
            for path in sorted(gate.INFRA_DIR.rglob("*.tf"))
            if ".terraform" not in path.parts
            and re.search(r'(?m)^\s*principal\s*=\s*"\*"\s*$', path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"{offenders} declare a wildcard-principal Lambda permission, which is forbidden and "
            "`scripts/check_no_public_exposure.py` fails on with no exemption available"
        )

    def test_the_gate_carries_no_wildcard_principal_exemption(self) -> None:
        """A wildcard `principal` can pass only via `allowed_principals`; no other predicate admits it."""
        source = Path(gate.__file__).read_text(encoding="utf-8")
        assert "_is_sanctioned_public_function_url_grant" not in source
        assert 'principal") == "*"' not in source, (
            "the gate compares a principal against the wildcard literal again; if that is a new "
            "exemption it needs its own conjunctive conditions and a fence like the CloudFront OAC "
            "grant's, not a bare equality"
        )

    def test_the_committed_fixture_contains_no_wildcard_principal_grant(self) -> None:
        """The committed fixture contains no wildcard-principal grant."""
        document = plan_fixtures.build("compliant")
        wildcards = sorted(
            change["address"]
            for change in document["resource_changes"]
            if change.get("type") == "aws_lambda_permission"
            and ((change.get("change") or {}).get("after") or {}).get("principal") == "*"
        )
        assert wildcards == [], (
            f"the committed fixture carries wildcard-principal grant(s) {wildcards} while the gate has "
            "no exemption for them, so the gate must be reporting a failure -- or the control stopped "
            "reading them"
        )


# ---------------------------------------------------------------------------
# `lambda_permission_composition` compares the set of grant names in the plan with `infra/`, so a
# one-for-one substitution that leaves the total count unchanged still fails.
# ---------------------------------------------------------------------------


class TestLambdaPermissionComposition:
    @staticmethod
    def _plan_with(names: list[str]) -> Any:
        """A plan whose only `aws_lambda_permission` instances carry `names`."""
        return gate.Plan(
            resource_changes=[
                {
                    "type": "aws_lambda_permission",
                    "address": f"module.probe.aws_lambda_permission.{name}",
                    "change": {"after": {}},
                }
                for name in names
            ]
        )

    def test_the_committed_fixture_and_the_source_now_agree(self) -> None:
        """The committed fixture's grant names match the source set, and the control names the set."""
        findings = gate.check_lambda_permission_composition(load_plan("compliant"))
        assert len(findings) == 1, findings
        finding = findings[0]
        assert finding.status == "pass", finding
        assert "agent_relay_function_url" in finding.detail, finding.detail
        assert "public_function_url" not in finding.detail, (
            "the deleted wildcard grant is back in the plan's grant set"
        )

    def test_a_one_for_one_substitution_is_caught_though_the_total_is_unchanged(self) -> None:
        """A one-for-one name substitution is not passed (fails or defers) though the total count
        is unchanged."""
        declared = sorted(gate.declared_lambda_permission_names())
        assert len(declared) >= 2, declared

        matching = self._plan_with(declared)
        assert gate.check_lambda_permission_composition(matching)[0].status == "pass", (
            "the un-mutated control case must pass, or every mutation below passes for free"
        )

        substituted = [*declared[:-1], "some_other_grant"]
        assert len(substituted) == len(declared), "the substitution must not change the total"
        finding = gate.check_lambda_permission_composition(self._plan_with(substituted))[0]
        assert finding.status in {"fail", "deferred"}, finding
        assert "some_other_grant" in finding.detail, finding.detail
        assert declared[-1] in finding.detail, finding.detail

    def test_a_grant_the_plan_has_and_the_source_does_not_is_reported(self) -> None:
        declared = sorted(gate.declared_lambda_permission_names())
        finding = gate.check_lambda_permission_composition(
            self._plan_with([*declared, "smuggled_grant"])
        )[0]
        assert finding.status in {"fail", "deferred"}, finding
        assert "smuggled_grant" in finding.detail

    def test_a_grant_the_source_has_and_the_plan_does_not_is_reported(self) -> None:
        """A grant the source declares and the plan lacks is reported."""
        declared = sorted(gate.declared_lambda_permission_names())
        finding = gate.check_lambda_permission_composition(self._plan_with(declared[:-1]))[0]
        assert finding.status in {"fail", "deferred"}, finding
        assert declared[-1] in finding.detail

    def test_an_empty_plan_fails_rather_than_matching_vacuously(self) -> None:
        """A plan with no grants fails rather than matching vacuously."""
        finding = gate.check_lambda_permission_composition(self._plan_with([]))[0]
        assert finding.status == "fail", finding
        assert "no Lambda invoke grant" in finding.detail

    def test_an_empty_source_tree_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A source tree declaring no grants fails."""
        scratch = tmp_path / "infra"
        scratch.mkdir()
        (scratch / "empty.tf").write_text("# nothing\n", encoding="utf-8")
        monkeypatch.setattr(gate, "INFRA_DIR", scratch)
        finding = gate.check_lambda_permission_composition(load_plan("compliant"))[0]
        assert finding.status == "fail", finding
        assert "asserted nothing" in finding.detail

    def test_the_plan_side_parser_strips_module_paths_and_instance_keys(self) -> None:
        """Both `for_each` address shapes reduce to their block name.

        The shapes are `module.api.aws_lambda_permission.apigw_invoke["get_account"]` and
        `module.lambda_action["x"].aws_lambda_permission.api_gateway[0]`.
        """
        plan = gate.Plan(
            resource_changes=[
                {
                    "type": "aws_lambda_permission",
                    "address": 'module.api.aws_lambda_permission.apigw_invoke["get_account"]',
                    "change": {"after": {}},
                },
                {
                    "type": "aws_lambda_permission",
                    "address": 'module.lambda_action["x"].aws_lambda_permission.api_gateway[0]',
                    "change": {"after": {}},
                },
                {
                    "type": "aws_lambda_permission",
                    "address": "aws_lambda_permission.at_the_root",
                    "change": {"after": {}},
                },
            ]
        )
        assert gate.planned_lambda_permission_names(plan) == {
            "apigw_invoke",
            "api_gateway",
            "at_the_root",
        }

    def test_the_count_expectation_keeps_its_own_total_and_says_it_is_not_the_composition(
        self,
    ) -> None:
        """`resource_existence_counts` keeps its total, and its basis states that a count cannot
        see a substitution."""
        findings = [
            f
            for f in gate.check_resource_existence_counts(load_plan("compliant"))
            if f.resource == "aws_lambda_permission"
        ]
        assert len(findings) == 1, findings
        assert findings[0].status == "pass", findings[0]
        assert "SUBSTITUTION" in findings[0].detail, (
            "the basis no longer records that a count cannot see a one-for-one substitution, which is "
            "the whole reason check_lambda_permission_composition exists beside it"
        )


# ---------------------------------------------------------------------------
# Brace counting over text where braces can be data. Each case drives the parser against a
# scratch `infra/`:
#   * `tf_resource_blocks` must not enter heredoc mode on `<<EOT` inside a string, or the following
#     resource is never returned (a false negative).
#   * `_infra_tf_sources`/`_balanced_block_body` must not let an unbalanced `{` in a heredoc body
#     over-run into the neighbouring block (a false positive).


def _hcl_text_module() -> Any:
    """Return the real `scripts/hcl_text.py` module."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import hcl_text  # type: ignore[import-not-found]

    return hcl_text


class TestBraceMatchingIsNotFooledByBracesAsData:
    HEREDOC_MENTIONED_IN_A_STRING = """
resource "aws_security_group" "target" {
  description = "see <<EOT in the docs"
}

resource "aws_security_group" "neighbour" {
  ingress {
    cidr_blocks = ["0.0.0.0/0"]
  }
}
"""

    HEREDOC_BODY_WITH_AN_UNBALANCED_BRACE = """
resource "aws_security_group" "target" {
  description = <<-EOT
    an unbalanced { brace in prose
  EOT
}

resource "aws_security_group" "neighbour" {
  ingress {
    cidr_blocks = ["0.0.0.0/0"]
  }
}
"""

    @staticmethod
    def _scratch_infra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hcl: str) -> None:
        scratch = tmp_path / "infra"
        scratch.mkdir(exist_ok=True)
        (scratch / "probe.tf").write_text(hcl, encoding="utf-8")
        monkeypatch.setattr(gate, "INFRA_DIR", scratch)

    @pytest.mark.parametrize(
        "hcl_attr",
        ["HEREDOC_MENTIONED_IN_A_STRING", "HEREDOC_BODY_WITH_AN_UNBALANCED_BRACE"],
    )
    def test_no_block_is_swallowed_or_dropped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hcl_attr: str
    ) -> None:
        """`tf_resource_blocks` returns both resources, whichever way the braces are disguised."""
        self._scratch_infra(monkeypatch, tmp_path, getattr(self, hcl_attr))
        names = [name for _module, name, _body in gate.tf_resource_blocks("aws_security_group")]
        assert names == ["target", "neighbour"], (
            f"{hcl_attr}: expected both blocks, got {names}. A missing 'neighbour' means the parser "
            "over-ran into it or ran to EOF; nothing downstream can see a block it never returned."
        )

    @pytest.mark.parametrize(
        "hcl_attr",
        ["HEREDOC_MENTIONED_IN_A_STRING", "HEREDOC_BODY_WITH_AN_UNBALANCED_BRACE"],
    )
    def test_the_security_group_controls_attribute_findings_to_the_right_block(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hcl_attr: str
    ) -> None:
        """`target` passes and `neighbour` fails, so findings are attributed to the right block."""
        self._scratch_infra(monkeypatch, tmp_path, getattr(self, hcl_attr))
        reported = {
            finding.resource.rsplit("aws_security_group.", 1)[-1]: finding.status
            for finding in gate.check_security_group_zero_inbound(gate.Plan(resource_changes=[]))
            if "aws_security_group." in finding.resource
        }
        assert reported == {"target": "pass", "neighbour": "fail"}, (
            f"{hcl_attr}: findings were attributed as {reported}. `target` has no ingress block; a "
            "failure against it means a heredoc's braces were counted as structure."
        )

    def test_the_shared_helper_is_used_rather_than_a_fourth_bespoke_copy(self) -> None:
        """Both parsers route through `scripts/hcl_text.py`, and the old regex is gone."""
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "from hcl_text import" in source, "the shared helper is not imported"
        assert "blank_heredoc_bodies" in source and "blank_literals" in source
        assert "heredoc_open = re.compile" not in source, (
            "the bespoke heredoc-opener regex is back. Its defect was being UNANCHORED and searched "
            "against the raw line; hcl_text._OPENER is anchored to end-of-line, which is what HCL's "
            "own grammar requires."
        )

    def test_the_transform_preserves_shape_so_reported_lines_stay_correct(self) -> None:
        """The transform preserves length and line structure on the real tree."""
        hcl = _hcl_text_module()
        checked = 0
        for path in sorted(gate.INFRA_DIR.rglob("*.tf")):
            if ".terraform" in path.parts:
                continue
            original = path.read_text(encoding="utf-8")
            assert hcl.same_shape(original, hcl.blank_heredoc_bodies(original)), path
            assert hcl.same_shape(original, hcl.blank_literals(original)), path
            checked += 1
        assert checked > 0, "no infra/**/*.tf was checked, so this asserts nothing"

    def test_same_shape_can_actually_report_false(self) -> None:
        """`same_shape` reports `False` for a transform that changes the text's shape."""
        hcl = _hcl_text_module()

        assert hcl.same_shape("a = 1\n", "a = 1\n") is True
        assert hcl.same_shape("a = 1\n", "a = 1") is False, "a length change is not detected"
        assert hcl.same_shape("a\nb\n", "a b\n") is False, "a moved newline is not detected"
        assert hcl.same_shape("ab\n", "a\nb") is False, "an inserted newline is not detected"

    def test_blanking_string_contents_would_erase_the_headers_and_is_not_used_for_matching(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The source-reading controls use `blank_heredoc_bodies`, not `blank_literals`, which would
        erase the quoted resource-header labels."""
        hcl = _hcl_text_module()
        header = 'resource "aws_security_group" "lambda" {\n}\n'
        assert "aws_security_group" not in hcl.blank_literals(header), (
            "blank_literals no longer erases header labels, so this warning is stale — but check why "
            "before deleting it: the source-reading controls depend on the labels surviving"
        )
        assert "aws_security_group" in hcl.blank_heredoc_bodies(header)

        # The controls still find blocks on the real tree.
        findings = gate.check_security_group_zero_inbound(gate.Plan(resource_changes=[]))
        assert not _failed(findings), _failed(findings)
        assert any("security group(s)" in f.detail for f in findings), findings


class TestTheSourceReaderNeutralisesBothCommentKinds:
    """`_infra_tf_sources` composes `blank_comments(blank_heredoc_bodies(...))`, asserted through
    the gate's own reader."""

    def test_the_source_reader_neutralises_both_comment_kinds(self) -> None:
        """Both markers blanked, and an in-string marker left alone, through the gate's own reader."""
        sources = gate._infra_tf_sources()
        assert sources, "no infra/**/*.tf was read, so every assertion below is vacuous"

        # Keyed by path relative to `infra/`: every module has a `main.tf`.
        by_path = {str(path.relative_to(gate.INFRA_DIR)): text for path, text in sources}

        # A brace-carrying *genuine* block comment: its braces would otherwise be counted as structure.
        assert (
            "custom_jwt_authorizer.{discovery_url" not in by_path["modules/agentcore/gateway.tf"]
        ), "a genuine block comment's text survived, so its braces are still structure to a counter"
        assert "Root composition: wires the seven" not in by_path["main.tf"], (
            "the root file's `/**` header survived the gate's source reader"
        )
        # An in-string `/*` must not open a block comment.
        assert "REGISTRY_HOST=$${ECR_REPOSITORY_URI%%/*}" in by_path["main.tf"], (
            "the in-string `/*` at main.tf:160 was treated as a comment opener; live code was eaten"
        )

    def test_the_header_keyed_extractions_still_see_every_block(self) -> None:
        """Header-keyed extraction on stripped text finds the same non-empty set as a raw regex."""
        sources = gate._infra_tf_sources()
        raw = [
            (path, path.read_text(encoding="utf-8"))
            for path in sorted(gate.INFRA_DIR.rglob("*.tf"))
            if ".terraform" not in path.parts
        ]
        assert raw, "no infra/**/*.tf was read, so this control asserts nothing"

        for pattern in (
            r'resource\s+"aws_lambda_permission"\s+"([A-Za-z0-9_-]+)"',
            r'resource\s+"aws_security_group"\s+"([A-Za-z0-9_-]+)"\s*\{',
            r'resource\s+"aws_vpc_security_group_egress_rule"\s+"([A-Za-z0-9_-]+)"\s*\{',
        ):
            stripped_hits = {m for _path, text in sources for m in re.findall(pattern, text)}
            raw_hits = {m for _path, text in raw for m in re.findall(pattern, text)}
            # Per-pattern floor first: two empty sets compare equal.
            assert raw_hits, (
                f"{pattern} matched nothing in the raw source, so its comparison is vacuous"
            )
            assert stripped_hits == raw_hits, (
                f"{pattern}: the neutralised source sees {sorted(stripped_hits)} but the raw source has "
                f"{sorted(raw_hits)}. A label the stripper drops is a block every header-keyed control "
                "silently stops examining."
            )


#: A distribution-scoped source_arn: one CloudFront distribution, no wildcard.
_SCOPED_DISTRIBUTION_ARN = "arn:aws:cloudfront::123456789012:distribution/E1EXAMPLE"
_RELAY_FUNCTION_NAME = "crm-agent-eval-agent-relay"


def _lambda_permission_change(
    *,
    address: str,
    statement_id: str,
    action: str,
    principal: str = "cloudfront.amazonaws.com",
    function_name: str = _RELAY_FUNCTION_NAME,
    function_url_auth_type: str | None = None,
    source_arn: str | None = _SCOPED_DISTRIBUTION_ARN,
    source_arn_unknown: bool = False,
) -> dict[str, Any]:
    """Return one `aws_lambda_permission` resource_change.

    `source_arn_unknown=True` places the marker in `after_unknown` and omits it from `after`.
    """
    after: dict[str, Any] = {
        "statement_id": statement_id,
        "action": action,
        "principal": principal,
        "function_name": function_name,
        "function_url_auth_type": function_url_auth_type,
    }
    after_unknown: dict[str, Any] = {}
    if source_arn_unknown:
        after_unknown["source_arn"] = True
    elif source_arn is not None:
        after["source_arn"] = source_arn
    return {
        "address": address,
        "type": "aws_lambda_permission",
        "change": {"after": after, "after_unknown": after_unknown},
    }


class TestCloudFrontOacGrantExemption:
    """The CloudFront-to-relay Function URL grant is exempt only when every condition holds.

    `cloudfront.amazonaws.com` is absent from `allowed_principals`; each test flips one condition and
    expects a `fail`.
    """

    @staticmethod
    def _statuses(
        statement_id: str = gate.CLOUDFRONT_OAC_STATEMENT_ID,
        action: str = "lambda:InvokeFunctionUrl",
        function_url_auth_type: str | None = "AWS_IAM",
        principal: str = "cloudfront.amazonaws.com",
        source_arn: str | None = None,
        source_arn_unknown: bool = True,
    ) -> list[str]:
        # Defaults to the sanctioned shape: AWS_IAM auth and a same-apply (unknown) distribution
        # source_arn.
        plan = gate.Plan(
            resource_changes=[
                _lambda_permission_change(
                    address="module.frontend_cdn.aws_lambda_permission.agent_relay_function_url",
                    statement_id=statement_id,
                    action=action,
                    principal=principal,
                    function_url_auth_type=function_url_auth_type,
                    source_arn=source_arn,
                    source_arn_unknown=source_arn_unknown,
                )
            ]
        )
        return [f.status for f in gate.check_lambda_resource_policy_no_wildcard(plan)]

    def test_the_sanctioned_grant_passes(self) -> None:
        """AWS_IAM auth with a deferred distribution source_arn passes."""
        assert self._statuses() == ["pass"]

    def test_a_different_statement_id_fails(self) -> None:
        """The exemption is one exact id, so a second CloudFront grant cannot ride on it."""
        assert self._statuses(statement_id="AllowSomethingElse") == ["fail"]

    def test_url_grant_without_a_scoped_source_arn_fails(self) -> None:
        """A URL grant without a scoped source_arn fails."""
        assert self._statuses(source_arn="*", source_arn_unknown=False) == ["fail"]
        assert self._statuses(source_arn=None, source_arn_unknown=False) == ["fail"]

    def test_invoke_function_rather_than_invoke_function_url_fails(self) -> None:
        """A lone `lambda:InvokeFunction` grant on the URL statement id fails.

        The paired-grant case is `TestCloudFrontOacInvokeFunctionGrantExemption`.
        """
        assert self._statuses(action="lambda:InvokeFunction") == ["fail"]

    def test_auth_type_none_fails(self) -> None:
        """AuthType NONE fails."""
        assert self._statuses(function_url_auth_type="NONE") == ["fail"]

    def test_a_missing_auth_type_fails(self) -> None:
        assert self._statuses(function_url_auth_type=None) == ["fail"]

    def test_a_wildcard_principal_under_the_same_statement_id_fails(self) -> None:
        assert self._statuses(principal="*") == ["fail"]

    def test_cloudfront_is_not_in_the_general_allowed_principals(self) -> None:
        """CloudFront is absent from the shared `allowed_principals` set."""
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        block = source[source.index("allowed_principals = {") :]
        block = block[: block.index("}")]
        assert "cloudfront" not in block, (
            "cloudfront.amazonaws.com was added to allowed_principals; use the statement-id-scoped "
            "exemption instead, which also requires function_url_auth_type=AWS_IAM"
        )


class TestCloudFrontOacInvokeFunctionGrantExemption:
    """The paired CloudFront OAC `lambda:InvokeFunction` grant on the relay.

    The exemption is anchored to a paired `InvokeFunctionUrl` grant on the same `function_name`;
    flipping the anchor or the source_arn condition reds exactly its case.
    """

    @staticmethod
    def _statuses(
        *,
        include_url_grant: bool = True,
        invoke_function_name: str = _RELAY_FUNCTION_NAME,
        invoke_statement_id: str = gate.CLOUDFRONT_OAC_INVOKE_FUNCTION_STATEMENT_ID,
        invoke_source_arn: str | None = _SCOPED_DISTRIBUTION_ARN,
        invoke_source_arn_unknown: bool = False,
        url_function_url_auth_type: str | None = "AWS_IAM",
    ) -> dict[str, str]:
        """Return statuses keyed `"url"` / `"invoke"` for a plan holding the paired grants."""
        changes = []
        if include_url_grant:
            changes.append(
                _lambda_permission_change(
                    address="module.frontend_cdn.aws_lambda_permission.agent_relay_function_url",
                    statement_id=gate.CLOUDFRONT_OAC_STATEMENT_ID,
                    action="lambda:InvokeFunctionUrl",
                    function_name=_RELAY_FUNCTION_NAME,
                    function_url_auth_type=url_function_url_auth_type,
                    source_arn=None,
                    source_arn_unknown=True,
                )
            )
        changes.append(
            _lambda_permission_change(
                address="module.frontend_cdn.aws_lambda_permission.agent_relay_invoke_function",
                statement_id=invoke_statement_id,
                action="lambda:InvokeFunction",
                function_name=invoke_function_name,
                function_url_auth_type=None,
                source_arn=invoke_source_arn,
                source_arn_unknown=invoke_source_arn_unknown,
            )
        )
        plan = gate.Plan(resource_changes=changes)
        result: dict[str, str] = {}
        for f in gate.check_lambda_resource_policy_no_wildcard(plan):
            key = "url" if f.resource.endswith("agent_relay_function_url") else "invoke"
            result[key] = f.status
        return result

    def test_both_paired_grants_pass(self) -> None:
        """The AWS_IAM URL grant and the scoped InvokeFunction grant both pass."""
        assert self._statuses() == {"url": "pass", "invoke": "pass"}

    def test_both_pass_with_a_deferred_invoke_source_arn(self) -> None:
        """The pair passes with a deferred InvokeFunction source_arn."""
        assert self._statuses(invoke_source_arn=None, invoke_source_arn_unknown=True) == {
            "url": "pass",
            "invoke": "pass",
        }

    def test_action_lambda_invoke_function_grant_still_fails(self) -> None:
        """An `InvokeFunction` grant to CloudFront on a function with no paired URL grant fails."""
        assert self._statuses(
            include_url_grant=False, invoke_function_name="crm-agent-eval-get_account"
        ) == {"invoke": "fail"}

    def test_no_paired_invoke_function_url_grant_fails(self) -> None:
        """Anchor (i): without the paired URL grant in the plan, the InvokeFunction grant fails."""
        assert self._statuses(include_url_grant=False) == {"invoke": "fail"}

    def test_paired_grant_with_auth_type_none_makes_both_fail(self) -> None:
        """Anchor (ii): flip the URL to AuthType=NONE and the anchor disappears — BOTH go red."""
        assert self._statuses(url_function_url_auth_type="NONE") == {
            "url": "fail",
            "invoke": "fail",
        }

    def test_invoke_function_grant_without_a_scoped_source_arn_fails(self) -> None:
        """Scoping (iii): source_arn `"*"` or absent is unscoped — the InvokeFunction grant fails."""
        assert self._statuses(invoke_source_arn="*") == {"url": "pass", "invoke": "fail"}
        assert self._statuses(invoke_source_arn=None) == {"url": "pass", "invoke": "fail"}

    def test_a_different_invoke_statement_id_fails(self) -> None:
        """The exemption is one exact id; a different id cannot ride the anchor."""
        assert self._statuses(invoke_statement_id="AllowSomethingElse") == {
            "url": "pass",
            "invoke": "fail",
        }


# ---------------------------------------------------------------------------
# The REST API controls (authorizer, throttle, access logging, request validators) read
# `aws_api_gateway_*`. Each case drives a control on a synthetic one-resource plan.
# ---------------------------------------------------------------------------


class TestRestApiControlsReadTheRestApi:
    @staticmethod
    def _plan(
        resource_type: str, after: dict[str, Any], unknown: dict[str, Any] | None = None
    ) -> gate.Plan:
        return gate.Plan(
            resource_changes=[
                {
                    "type": resource_type,
                    "address": f"module.api.{resource_type}.subject",
                    "change": {"after": after, "after_unknown": unknown or {}},
                }
            ]
        )

    # --- authorizer -------------------------------------------------------------------------
    def test_a_method_with_the_cognito_authorizer_passes_even_unknown_at_plan_time(self) -> None:
        """The authorizer id is a same-apply reference, so unknown must not be a failure."""
        findings = gate.check_api_authorizer_on_every_route(
            self._plan(
                "aws_api_gateway_method",
                {
                    "authorization": "COGNITO_USER_POOLS",
                    "authorizer_id": None,
                    "http_method": "POST",
                },
                {"authorizer_id": True},
            )
        )
        assert [f.status for f in findings] == ["pass"], findings

    @pytest.mark.parametrize("authorization", ["NONE", "AWS_IAM", "CUSTOM", None])
    def test_any_other_authorization_fails(self, authorization: str | None) -> None:
        """Any authorization other than `COGNITO_USER_POOLS` fails."""
        findings = gate.check_api_authorizer_on_every_route(
            self._plan(
                "aws_api_gateway_method",
                {"authorization": authorization, "authorizer_id": None, "http_method": "POST"},
                {"authorizer_id": True},
            )
        )
        assert [f.status for f in findings] == ["fail"], findings

    def test_cognito_authorization_with_no_authorizer_at_all_fails(self) -> None:
        """Neither a known id nor an unknown marker: the method demands a pool nothing attaches."""
        findings = gate.check_api_authorizer_on_every_route(
            self._plan(
                "aws_api_gateway_method",
                {
                    "authorization": "COGNITO_USER_POOLS",
                    "authorizer_id": None,
                    "http_method": "POST",
                },
                {},
            )
        )
        assert [f.status for f in findings] == ["fail"], findings

    def test_the_v2_route_type_is_no_longer_read(self) -> None:
        """A plan of only `aws_apigatewayv2_*` routes yields no findings."""
        findings = gate.check_api_authorizer_on_every_route(
            self._plan("aws_apigatewayv2_route", {"authorization_type": "NONE"})
        )
        assert findings == [], findings

    # --- throttle ---------------------------------------------------------------------------
    def test_stage_wide_throttling_passes(self) -> None:
        findings = gate.check_api_throttle(
            self._plan(
                "aws_api_gateway_method_settings",
                {
                    "method_path": "*/*",
                    "settings": [{"throttling_burst_limit": 50, "throttling_rate_limit": 25}],
                },
            )
        )
        assert [f.status for f in findings] == ["pass"], findings

    @pytest.mark.parametrize(
        "after,reason",
        [
            (
                {
                    "method_path": "tools/POST",
                    "settings": [{"throttling_burst_limit": 50, "throttling_rate_limit": 25}],
                },
                "a settings block scoped to one route leaves the rest unlimited",
            ),
            (
                {"method_path": "*/*", "settings": [{"throttling_rate_limit": 25}]},
                "no burst limit",
            ),
            (
                {"method_path": "*/*", "settings": [{"throttling_burst_limit": 50}]},
                "no rate limit",
            ),
            ({"method_path": "*/*", "settings": []}, "no settings block at all"),
        ],
    )
    def test_throttling_that_does_not_cover_every_method_fails(
        self, after: dict[str, Any], reason: str
    ) -> None:
        findings = gate.check_api_throttle(self._plan("aws_api_gateway_method_settings", after))
        assert [f.status for f in findings] == ["fail"], f"{reason}: {findings}"

    # --- access logging ---------------------------------------------------------------------
    def test_a_stage_whose_log_destination_is_unknown_at_plan_time_passes(self) -> None:
        findings = gate.check_api_access_logging(
            self._plan(
                "aws_api_gateway_stage",
                {"access_log_settings": [{"format": "{}"}]},
                {"access_log_settings": [{"destination_arn": True}]},
            )
        )
        assert [f.status for f in findings] == ["pass"], findings

    def test_a_stage_with_no_access_log_settings_fails(self) -> None:
        findings = gate.check_api_access_logging(
            self._plan("aws_api_gateway_stage", {"access_log_settings": []})
        )
        assert [f.status for f in findings] == ["fail"], findings

    # --- request validators -------------------------------------------------------------------
    def test_a_post_with_the_validator_unknown_and_the_model_present_passes(self) -> None:
        """A POST with an unknown validator id and the application/json model passes."""
        findings = gate.check_api_request_validators(
            self._plan(
                "aws_api_gateway_method",
                {
                    "http_method": "POST",
                    "request_validator_id": None,
                    "request_models": {"application/json": "JsonObject"},
                },
                {"request_validator_id": True},
            )
        )
        assert [f.status for f in findings] == ["pass"], findings

    def test_a_method_with_no_request_validator_id_at_all_fails(self) -> None:
        """Neither a known id nor an unknown marker: no validator attaches, so validation is off."""
        findings = gate.check_api_request_validators(
            self._plan(
                "aws_api_gateway_method",
                {
                    "http_method": "POST",
                    "request_validator_id": None,
                    "request_models": {"application/json": "JsonObject"},
                },
                {},
            )
        )
        assert [f.status for f in findings] == ["fail"], findings

    def test_a_post_with_no_object_model_fails(self) -> None:
        findings = gate.check_api_request_validators(
            self._plan(
                "aws_api_gateway_method",
                {"http_method": "POST", "request_validator_id": None, "request_models": {}},
                {"request_validator_id": True},
            )
        )
        assert [f.status for f in findings] == ["fail"], findings

    def test_a_get_with_the_validator_and_no_model_passes(self) -> None:
        """A GET has no body, so it references the validator but carries no model."""
        findings = gate.check_api_request_validators(
            self._plan(
                "aws_api_gateway_method",
                {"http_method": "GET", "request_validator_id": None, "request_models": {}},
                {"request_validator_id": True},
            )
        )
        assert [f.status for f in findings] == ["pass"], findings

    def test_a_get_carrying_a_body_model_fails(self) -> None:
        """A non-POST route must carry no body model; one that does is a misconfiguration."""
        findings = gate.check_api_request_validators(
            self._plan(
                "aws_api_gateway_method",
                {
                    "http_method": "GET",
                    "request_validator_id": None,
                    "request_models": {"application/json": "JsonObject"},
                },
                {"request_validator_id": True},
            )
        )
        assert [f.status for f in findings] == ["fail"], findings


# ---------------------------------------------------------------------------
# The two DynamoDB IAM controls read `data.aws_iam_policy_document` `statement[*]`, whose `sid`,
# `effect`, `actions` and `condition` are known at plan time.
# ---------------------------------------------------------------------------


class TestDynamoDbGrantsReadThePolicyDocument:
    @staticmethod
    def _document(address: str, statements: list[dict[str, Any]]) -> gate.Plan:
        return gate.Plan(
            resource_changes=[
                {
                    "type": "aws_iam_policy_document",
                    "address": address,
                    "change": {"after": {"statement": statements}, "after_unknown": {}},
                }
            ]
        )

    INTERCEPTOR = "module.lambda_action_interceptor.data.aws_iam_policy_document.dynamodb[0]"

    def test_effect_is_load_bearing_for_the_wildcard_check(self) -> None:
        """`dynamodb:*` under Deny passes and under Allow fails."""
        deny = self._document(
            self.INTERCEPTOR,
            [
                {
                    "sid": "ExplicitDenyCrossFunctionIsolation",
                    "effect": "Deny",
                    "actions": ["dynamodb:*"],
                }
            ],
        )
        allow = self._document(
            self.INTERCEPTOR,
            [{"sid": "DynamoDbReps0", "effect": "Allow", "actions": ["dynamodb:*"]}],
        )
        deny_statuses = [
            f.status
            for f in gate.check_dynamodb_no_wildcard_action(deny)
            if "statement[" in f.resource
        ]
        allow_statuses = [
            f.status
            for f in gate.check_dynamodb_no_wildcard_action(allow)
            if "statement[" in f.resource
        ]
        assert deny_statuses == ["pass"], deny_statuses
        assert allow_statuses == ["fail"], allow_statuses

    def test_a_bare_star_action_also_fails(self) -> None:
        """A bare `*` action also fails."""
        plan = self._document(
            self.INTERCEPTOR, [{"sid": "DynamoDbReps0", "effect": "Allow", "actions": ["*"]}]
        )
        assert any(f.status == "fail" for f in gate.check_dynamodb_no_wildcard_action(plan))

    def test_no_documents_at_all_fails_the_coverage_finding(self) -> None:
        """The vacuity floor: zero documents must not read as zero violations."""
        findings = gate.check_dynamodb_no_wildcard_action(gate.Plan(resource_changes=[]))
        assert [f.status for f in findings] == ["fail"], findings
        assert "coverage" in findings[0].resource

    def test_the_matrix_disposition_is_asserted_in_both_directions(self) -> None:
        """A LeadingKeys condition where the matrix says application code fails, and so does its
        absence where the matrix says leading_keys."""
        reps_with = self._document(
            self.INTERCEPTOR,
            [
                {
                    "sid": "DynamoDbReps0",
                    "effect": "Allow",
                    "actions": ["dynamodb:GetItem"],
                    "condition": [
                        {
                            "test": "ForAllValues:StringEquals",
                            "variable": "dynamodb:LeadingKeys",
                            "values": ["svc-mcp-gateway"],
                        }
                    ],
                }
            ],
        )
        reps_without = self._document(
            self.INTERCEPTOR,
            [{"sid": "DynamoDbReps0", "effect": "Allow", "actions": ["dynamodb:GetItem"]}],
        )
        matching = [
            f
            for f in gate.check_dynamodb_leading_keys_isolation(reps_with)
            if f.resource == "identity-interceptor -> DynamoDbReps0"
        ]
        assert [f.status for f in matching] == ["pass"], matching

        downgraded = [
            f
            for f in gate.check_dynamodb_leading_keys_isolation(reps_without)
            if f.resource == "identity-interceptor -> DynamoDbReps0"
        ]
        assert [f.status for f in downgraded] == ["fail"], downgraded
        assert "the matrix claims IAM enforces rep isolation" in downgraded[0].detail

    def test_a_condition_on_a_synthetic_key_table_fails(self) -> None:
        """A LeadingKeys condition on a synthetic-key table (`accounts`) fails."""
        plan = self._document(
            'module.lambda_action["approve_proposal"].data.aws_iam_policy_document.dynamodb[0]',
            [
                {
                    "sid": "DynamoDbAccounts1",
                    "effect": "Allow",
                    "actions": [
                        "dynamodb:GetItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:DeleteItem",
                        "dynamodb:PutItem",
                    ],
                    "condition": [
                        {
                            "test": "ForAllValues:StringEquals",
                            "variable": "dynamodb:LeadingKeys",
                            "values": ["rep-42"],
                        }
                    ],
                }
            ],
        )
        failed = [f for f in gate.check_dynamodb_leading_keys_isolation(plan) if f.status == "fail"]
        assert any("leading_keys_can_enforce_rep_isolation=false" in f.detail for f in failed), (
            failed
        )

    def test_a_widened_action_set_fails_against_the_matrix(self) -> None:
        """An action set wider than the matrix entry fails."""
        plan = self._document(
            self.INTERCEPTOR,
            [
                {
                    "sid": "DynamoDbReps0",
                    "effect": "Allow",
                    "actions": ["dynamodb:GetItem", "dynamodb:DeleteItem"],
                    "condition": [
                        {
                            "test": "ForAllValues:StringEquals",
                            "variable": "dynamodb:LeadingKeys",
                            "values": ["svc-mcp-gateway"],
                        }
                    ],
                }
            ],
        )
        failed = [f for f in gate.check_dynamodb_leading_keys_isolation(plan) if f.status == "fail"]
        assert any("!= the matrix's" in f.detail for f in failed), failed

    def test_an_unresolvable_sid_or_module_fails_rather_than_asserting_nothing(self) -> None:
        """An unresolvable sid or module name fails rather than skipping the statement."""
        unknown_table = self._document(
            self.INTERCEPTOR,
            [{"sid": "DynamoDbNotATable0", "effect": "Allow", "actions": ["dynamodb:GetItem"]}],
        )
        failed = [
            f
            for f in gate.check_dynamodb_leading_keys_isolation(unknown_table)
            if f.status == "fail"
        ]
        assert any("names no table in the matrix" in f.detail for f in failed), failed

        unknown_module = self._document(
            "module.lambda_action_not_a_singleton.data.aws_iam_policy_document.dynamodb[0]",
            [{"sid": "DynamoDbReps0", "effect": "Allow", "actions": ["dynamodb:GetItem"]}],
        )
        failed = [
            f
            for f in gate.check_dynamodb_leading_keys_isolation(unknown_module)
            if f.status == "fail"
        ]
        assert any("resolves to no matrix `function`" in f.detail for f in failed), failed

    def test_the_table_token_reproduces_terraforms_title_and_not_pythons(self) -> None:
        """The sid table token follows HCL `replace(title(x), "_", "")`, not Python's `str.title()`.

        The real sids are `DynamoDbAuditrecords0` and `DynamoDbEvalruns0`.
        """
        assert gate._matrix_table_token("audit_records") == "Auditrecords"
        assert gate._matrix_table_token("eval_runs") == "Evalruns"
        assert gate._matrix_table_token("reps") == "Reps"
        assert "audit_records".title().replace("_", "") == "AuditRecords", (
            "this test's own premise: Python's title() differs from Terraform's"
        )


# ---------------------------------------------------------------------------
# `no_public_ingress` reads standalone `aws_vpc_security_group_ingress_rule` resources as well as
# inline `ingress` blocks, and the ALB scheme is asserted internal.
# ---------------------------------------------------------------------------


class TestNoPublicIngressReadsEveryFormOfInboundPath:
    @staticmethod
    def _plan(resources: list[dict[str, Any]]) -> gate.Plan:
        return gate.Plan(resource_changes=resources)

    @staticmethod
    def _rule(**after: Any) -> dict[str, Any]:
        return {
            "type": "aws_vpc_security_group_ingress_rule",
            "address": "module.vpc.aws_vpc_security_group_ingress_rule.subject",
            "change": {"after": after, "after_unknown": {}},
        }

    def test_a_referenced_group_rule_passes_and_a_public_cidr_rule_fails(self) -> None:
        referenced = self._plan([self._rule(referenced_security_group_id="sg-x", from_port=443)])
        assert [f.status for f in gate.check_no_public_ingress(referenced)] == ["pass"]
        for cidr_field, value in (("cidr_ipv4", "0.0.0.0/0"), ("cidr_ipv6", "::/0")):
            public = self._plan([self._rule(**{cidr_field: value, "from_port": 443})])
            assert [f.status for f in gate.check_no_public_ingress(public)] == ["fail"], cidr_field

    def test_an_inline_ingress_block_is_still_read(self) -> None:
        """An inline `ingress` block is still read."""
        group = {
            "type": "aws_security_group",
            "address": "module.vpc.aws_security_group.subject",
            "change": {"after": {"ingress": [{"cidr_blocks": ["0.0.0.0/0"]}]}, "after_unknown": {}},
        }
        assert [f.status for f in gate.check_no_public_ingress(self._plan([group]))] == ["fail"]

    @pytest.mark.parametrize("internal,expected", [(True, "pass"), (False, "fail"), (None, "fail")])
    def test_the_load_balancer_scheme_is_asserted(
        self, internal: bool | None, expected: str
    ) -> None:
        """An internal ALB passes; `internal = false` or an absent attribute fails."""
        alb = {
            "type": "aws_lb",
            "address": "module.api_origin.aws_lb.this",
            "change": {
                "after": {"internal": internal, "load_balancer_type": "application"},
                "after_unknown": {},
            },
        }
        assert [f.status for f in gate.check_no_public_ingress(self._plan([alb]))] == [expected]


# ---------------------------------------------------------------------------
# A lambda_action DynamoDB document attached as managed policies.
# ---------------------------------------------------------------------------

_SPECIMEN_MODULE = "module.lambda_action_sweep_task"
_OTHER_MANAGED_MODULE = "module.lambda_action_sweep_control"


_MANAGED_DYNAMODB_NAMES = frozenset({"dynamodb", "dynamodb_allow", "dynamodb_deny"})


def _is_managed_dynamodb(change: dict[str, Any], module: str = _SPECIMEN_MODULE) -> bool:
    return (
        change.get("module_address") == module
        and change.get("type") in {"aws_iam_policy", "aws_iam_role_policy_attachment"}
        and change.get("name") in _MANAGED_DYNAMODB_NAMES
    )


def _inline_layout_document() -> dict[str, Any]:
    """The compliant fixture with sweep_task's DynamoDB document inline, whichever layout it holds.

    A fixture already in the managed layout has its four managed entries replaced by one inline
    `dynamodb[0]`, shaped like eval_trigger's.
    """
    document = _compliant_document()
    changes = document["resource_changes"]
    managed = [c for c in changes if _is_managed_dynamodb(c)]
    inline = f"{_SPECIMEN_MODULE}.aws_iam_role_policy.dynamodb[0]"
    if not managed:
        assert sum(1 for c in changes if c["address"] == inline) == 1
        return document
    assert len(managed) == 4, [c["address"] for c in managed]
    assert not any(c["address"] == inline for c in changes)
    changes[:] = [c for c in changes if not _is_managed_dynamodb(c)]
    template = next(
        c
        for c in changes
        if c["address"] == "module.lambda_action_eval_trigger.aws_iam_role_policy.dynamodb[0]"
    )
    added = copy.deepcopy(template)
    added.update(address=inline, module_address=_SPECIMEN_MODULE)
    changes.append(added)
    return document


def test_the_committed_fixture_holds_one_of_the_two_layouts() -> None:
    changes = _compliant_document()["resource_changes"]
    managed = sum(1 for c in changes if _is_managed_dynamodb(c))
    inline = sum(
        1 for c in changes if c["address"] == f"{_SPECIMEN_MODULE}.aws_iam_role_policy.dynamodb[0]"
    )
    assert (managed, inline) in {(0, 1), (4, 0)}, (managed, inline)


def _option_a_document(*, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """The compliant fixture with sweep_task's DynamoDB document attached as managed policies.

    Built from `_inline_layout_document`: the inline `dynamodb[0]` is replaced by
    `aws_iam_policy.dynamodb["allow"|"deny"]` and their `dynamodb_deny[0]`/`dynamodb_allow[0]`
    attachments, shaped like sweep_control's own managed policies and attachments. `drop` removes
    added addresses, for the negative cells.
    """
    document = _inline_layout_document()
    changes = document["resource_changes"]
    inline = f"{_SPECIMEN_MODULE}.aws_iam_role_policy.dynamodb[0]"
    assert sum(1 for c in changes if c["address"] == inline) == 1
    changes[:] = [c for c in changes if c["address"] != inline]
    for key in ("allow", "deny"):
        policy = next(
            c
            for c in changes
            if c["address"] == f'{_OTHER_MANAGED_MODULE}.aws_iam_policy.dynamodb["{key}"]'
        )
        attachment = next(
            c
            for c in changes
            if c["address"]
            == f"{_OTHER_MANAGED_MODULE}.aws_iam_role_policy_attachment.dynamodb_{key}[0]"
        )
        added = copy.deepcopy(policy)
        added.update(
            address=f'{_SPECIMEN_MODULE}.aws_iam_policy.dynamodb["{key}"]',
            module_address=_SPECIMEN_MODULE,
        )
        added["change"]["after"]["name"] = f"crm-agent-eval-sweep-task-dynamodb-{key}"
        attached = copy.deepcopy(attachment)
        attached.update(
            address=f"{_SPECIMEN_MODULE}.aws_iam_role_policy_attachment.dynamodb_{key}[0]",
            module_address=_SPECIMEN_MODULE,
        )
        changes += [c for c in (added, attached) if c["address"] not in drop]
    return document


def _dynamodb_coverage(document: dict[str, Any]) -> Any:
    plan = gate.Plan(resource_changes=document["resource_changes"])
    (row,) = [
        f
        for f in gate.check_dynamodb_no_wildcard_action(plan)
        if f.resource.startswith("(coverage)")
    ]
    return row


class TestManagedDynamodbPolicy:
    def test_the_managed_layout_passes_every_existence_count(self) -> None:
        assert not _failed(_counts_findings(_option_a_document()))

    def test_the_managed_layout_passes_dynamodb_coverage(self) -> None:
        row = _dynamodb_coverage(_option_a_document())
        assert row.status == "pass", row.detail
        assert "managed policies=2" in row.detail, row.detail

    def test_the_inline_layout_still_passes_both(self) -> None:
        assert _dynamodb_coverage(_inline_layout_document()).status == "pass"
        assert not _failed(_counts_findings(_inline_layout_document()))

    def test_dropping_the_inline_policy_with_no_managed_replacement_fails(self) -> None:
        """Removing the grant without attaching it elsewhere is not absorbed by the new terms."""
        managed = (
            f'{_SPECIMEN_MODULE}.aws_iam_policy.dynamodb["allow"]',
            f'{_SPECIMEN_MODULE}.aws_iam_policy.dynamodb["deny"]',
            f"{_SPECIMEN_MODULE}.aws_iam_role_policy_attachment.dynamodb_allow[0]",
            f"{_SPECIMEN_MODULE}.aws_iam_role_policy_attachment.dynamodb_deny[0]",
        )
        document = _option_a_document(drop=managed)
        assert _dynamodb_coverage(document).status == "fail"
        assert "aws_iam_role_policy" in _failed(_counts_findings(document))

    def test_a_missing_effect_policy_or_attachment_fails_its_count(self) -> None:
        no_deny = _option_a_document(drop=(f'{_SPECIMEN_MODULE}.aws_iam_policy.dynamodb["deny"]',))
        assert set(_failed(_counts_findings(no_deny))) == {"aws_iam_policy"}
        no_allow_attachment = _option_a_document(
            drop=(f"{_SPECIMEN_MODULE}.aws_iam_role_policy_attachment.dynamodb_allow[0]",)
        )
        assert set(_failed(_counts_findings(no_allow_attachment))) == {
            "aws_iam_role_policy_attachment"
        }

    def test_managed_modules_are_counted_once_per_module_not_per_policy(self) -> None:
        plan = gate.Plan(resource_changes=_option_a_document()["resource_changes"])
        assert gate._managed_dynamodb_modules(plan) == {_SPECIMEN_MODULE, _OTHER_MANAGED_MODULE}
        inline = gate.Plan(resource_changes=_inline_layout_document()["resource_changes"])
        assert gate._managed_dynamodb_modules(inline) == {_OTHER_MANAGED_MODULE}

    def test_the_managed_terms_are_exactly_option_a(self) -> None:
        counts = gate.EXPECTED_RESOURCE_COUNTS
        assert counts["aws_iam_role_policy"].per_managed_dynamodb == -1
        assert counts["aws_iam_policy"].per_managed_dynamodb == 2
        assert counts["aws_iam_role_policy_attachment"].per_managed_dynamodb == 2
        others = {t for t, e in counts.items() if e.per_managed_dynamodb} - {
            "aws_iam_role_policy",
            "aws_iam_policy",
            "aws_iam_role_policy_attachment",
        }
        assert not others, others
        assert counts["aws_iam_policy"].formula(26, 2) == "2+2*2"


# ---------------------------------------------------------------------------
# Fixture-lag declarations: empty against the committed fixture, and each
# mechanism still absorbs exactly the lag it declares when one is planted into the plan.
# ---------------------------------------------------------------------------

_EVAL_TRIGGER_DYNAMODB = (
    "module.lambda_action_eval_trigger.data.aws_iam_policy_document.dynamodb[0]"
)
_CAPABILITIES_DYNAMODB = (
    "module.lambda_action_capabilities.data.aws_iam_policy_document.dynamodb[0]"
)
_APPROVE_PROPOSAL_DYNAMODB = (
    'module.lambda_action["approve_proposal"].data.aws_iam_policy_document.dynamodb[0]'
)


def _statements(document: dict[str, Any], address: str) -> list[dict[str, Any]]:
    (change,) = [c for c in document["resource_changes"] if c["address"] == address]
    statements: list[dict[str, Any]] = change["change"]["after"]["statement"]
    return statements


def _plan_of(document: dict[str, Any]) -> gate.Plan:
    return gate.Plan(
        resource_changes=document["resource_changes"],
        configuration=document.get("configuration", {}),
    )


class TestFixtureLagDeclarations:
    @staticmethod
    def _findings(control: str, document: dict[str, Any] | None = None) -> list[Any]:
        plan = load_plan("compliant") if document is None else _plan_of(document)
        return [f for f in gate.run_gate(plan) if f.control == control]

    @staticmethod
    def _failed_subjects(findings: list[Any]) -> list[str]:
        return [f.resource for f in findings if f.status == "fail"]

    def test_every_declaration_passes_against_the_committed_fixture(self) -> None:
        for control in (
            "dynamodb_leading_keys_isolation",
            "resource_inventory_identity",
            "resource_existence_counts",
        ):
            assert not self._failed_subjects(self._findings(control)), control

    def test_no_fixture_lag_declaration_survives_the_re_capture(self) -> None:
        """The committed fixture is current, so every lag declaration is empty and no
        control defers or fails on a declared lag."""
        declarations = {
            name: getattr(gate, name)
            for name in (
                "FIXTURE_STALE_SINGLETONS",
                "FIXTURE_STALE_GRANTS",
                "FIXTURE_PENDING_GRANTS",
                "FIXTURE_PENDING_CONDITIONS",
                "FIXTURE_ABSENT_INSTANCES",
                "FIXTURE_ABSENT_TYPES",
            )
        }
        assert set(gate.FIXTURE_ABSENT_TYPES) == PENDING_RECAPTURE_ABSENT_TYPES
        assert set(gate.FIXTURE_LAGGING_COUNTS) == PENDING_RECAPTURE_LAGGING_COUNTS
        declarations["FIXTURE_ABSENT_TYPES"] = {
            k: v
            for k, v in gate.FIXTURE_ABSENT_TYPES.items()
            if k not in PENDING_RECAPTURE_ABSENT_TYPES
        }
        declarations["FIXTURE_LAGGING_COUNTS"] = {
            k: v
            for k, v in gate.FIXTURE_LAGGING_COUNTS.items()
            if k not in PENDING_RECAPTURE_LAGGING_COUNTS
        }
        assert set(gate.FIXTURE_STALE_GRANTS) == PENDING_RECAPTURE_STALE_GRANTS
        assert set(gate.FIXTURE_PENDING_GRANTS) == PENDING_RECAPTURE_PENDING_GRANTS
        for name, pending in (
            ("FIXTURE_STALE_GRANTS", PENDING_RECAPTURE_STALE_GRANTS),
            ("FIXTURE_PENDING_GRANTS", PENDING_RECAPTURE_PENDING_GRANTS),
        ):
            declarations[name] = {k: v for k, v in getattr(gate, name).items() if k not in pending}
        assert {name: value for name, value in declarations.items() if value} == {}
        lag = [
            f
            for f in gate.run_gate(load_plan("compliant"))
            if f.status != "pass" and re.search(r"\(declared-[a-z-]+\)", f.resource)
        ]
        assert lag == [], lag

    def test_a_planted_stale_grant_is_absorbed_only_while_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = _compliant_document()
        (accounts,) = [
            st
            for st in _statements(document, _APPROVE_PROPOSAL_DYNAMODB)
            if st["sid"].startswith("DynamoDbAccounts")
        ]
        accounts["actions"] = sorted({*accounts["actions"], "dynamodb:PutItem"})
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation", document))
        assert "approve_proposal -> accounts (action coverage)" in failed, failed
        monkeypatch.setattr(
            gate,
            "FIXTURE_STALE_GRANTS",
            {**gate.FIXTURE_STALE_GRANTS, "approve_proposal:accounts:dynamodb:PutItem": "planted"},
        )
        findings = self._findings("dynamodb_leading_keys_isolation", document)
        assert not self._failed_subjects(findings), self._failed_subjects(findings)

    def test_a_stale_grant_source_still_grants_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        entry = "approve_proposal:proposals:dynamodb:GetItem"
        monkeypatch.setattr(gate, "FIXTURE_STALE_GRANTS", {**gate.FIXTURE_STALE_GRANTS, entry: "x"})
        findings = self._findings("dynamodb_leading_keys_isolation")
        bad = [f for f in findings if f.resource == f"(declared-stale) {entry}"]
        assert [f.status for f in bad] == ["fail"], bad
        assert "source still grants it" in bad[0].detail

    def test_a_stale_grant_for_an_unknown_pair_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        entry = "approve_proposal:no_such_table:dynamodb:PutItem"
        monkeypatch.setattr(gate, "FIXTURE_STALE_GRANTS", {entry: "x"})
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation"))
        assert f"(declared-stale) {entry}" in failed, failed

    def test_the_stale_grant_source_reader_reads_main_tf(self) -> None:
        actions = gate._source_tool_grant_actions("approve_proposal", "accounts")
        assert actions is not None and "dynamodb:UpdateItem" in actions
        assert "dynamodb:PutItem" not in actions
        assert gate._source_tool_grant_actions("approve_proposal", "no_such_table") is None

    def test_a_planted_pending_grant_is_absorbed_only_while_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = _compliant_document()
        statements = _statements(document, _EVAL_TRIGGER_DYNAMODB)
        statements[:] = [st for st in statements if st.get("actions") != ["dynamodb:Query"]]
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation", document))
        assert "eval-trigger -> eval_runs (action coverage)" in failed, failed
        monkeypatch.setattr(
            gate,
            "FIXTURE_PENDING_GRANTS",
            {
                **gate.FIXTURE_PENDING_GRANTS,
                "eval-trigger:eval_runs:dynamodb:Query": (
                    "planted",
                    "eval_trigger_eval_runs_grants",
                ),
            },
        )
        findings = self._findings("dynamodb_leading_keys_isolation", document)
        assert not self._failed_subjects(findings), self._failed_subjects(findings)

    def test_a_pending_grant_source_does_not_carry_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = "eval-trigger:eval_runs:dynamodb:Scan"
        monkeypatch.setattr(
            gate,
            "FIXTURE_PENDING_GRANTS",
            {**gate.FIXTURE_PENDING_GRANTS, entry: ("x", "eval_trigger_eval_runs_grants")},
        )
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation"))
        assert f"(declared-pending) {entry}" in failed, failed

    def test_a_planted_endpoint_absence_is_absorbed_only_while_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = _compliant_document()
        document["resource_changes"] = [
            c
            for c in document["resource_changes"]
            if c["address"] != 'module.vpc.aws_vpc_endpoint.interface["states"]'
        ]
        failed = self._failed_subjects(self._findings("resource_existence_counts", document))
        assert "aws_vpc_endpoint" in failed, failed
        monkeypatch.setattr(
            gate, "FIXTURE_ABSENT_INSTANCES", {"aws_vpc_endpoint:states": "planted"}
        )
        failed = self._failed_subjects(self._findings("resource_existence_counts", document))
        assert "aws_vpc_endpoint" not in failed, failed

    def test_an_endpoint_absence_naming_an_undeclared_service_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gate,
            "FIXTURE_ABSENT_INSTANCES",
            {**gate.FIXTURE_ABSENT_INSTANCES, "aws_vpc_endpoint:no-such-service": "x"},
        )
        failed = self._failed_subjects(self._findings("resource_inventory_identity"))
        assert "(declared-absent) aws_vpc_endpoint:no-such-service" in failed, failed

    def test_a_planted_singleton_absence_is_absorbed_only_while_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = _compliant_document()
        document["resource_changes"] = [
            c
            for c in document["resource_changes"]
            if not c["address"].startswith("module.lambda_action_chat_target.")
        ]

        def coverage_failed() -> list[str]:
            failed = self._failed_subjects(
                self._findings("dynamodb_leading_keys_isolation", document)
            )
            return [s for s in failed if s.startswith("(coverage) the matrix's functions")]

        assert coverage_failed()
        monkeypatch.setattr(
            gate, "FIXTURE_ABSENT_INSTANCES", {"aws_lambda_function:chat-target": "planted"}
        )
        assert coverage_failed() == []

    def test_a_singleton_absence_naming_no_known_function_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gate,
            "FIXTURE_ABSENT_INSTANCES",
            {**gate.FIXTURE_ABSENT_INSTANCES, "aws_lambda_function:no-such-singleton": "x"},
        )
        failed = self._failed_subjects(self._findings("resource_inventory_identity"))
        assert "(declared-absent) aws_lambda_function:no-such-singleton" in failed, failed

    def test_each_new_singleton_module_is_named(self) -> None:
        """Every `module "lambda_action_<suffix>"` in `infra/` resolves to a matrix function name."""
        suffixes = {
            m
            for path in gate.INFRA_DIR.glob("*.tf")
            for m in re.findall(
                r'^module\s+"lambda_action_([a-z_]+)"', path.read_text("utf-8"), re.MULTILINE
            )
        }
        assert suffixes, "no singleton lambda_action module found"
        assert suffixes <= set(gate._SINGLETON_FUNCTION_NAMES), sorted(
            suffixes - set(gate._SINGLETON_FUNCTION_NAMES)
        )

    def test_the_zero_grant_capabilities_document_passes_with_only_its_deny(self) -> None:
        findings = self._findings("dynamodb_leading_keys_isolation")
        rows = [f for f in findings if f.resource == "(zero-grant) capabilities"]
        assert [f.status for f in rows] == ["pass"], rows
        effects = {
            st["effect"] for st in _statements(_compliant_document(), _CAPABILITIES_DYNAMODB)
        }
        assert effects == {"Deny"}, effects

    @pytest.mark.parametrize(
        "sid",
        ["DynamoDbAccounts0", "NotADynamoDbGrantSid", ""],
        ids=["grant-sid", "other-sid", "no-sid"],
    )
    def test_a_planted_dynamodb_allow_reds_the_zero_grant_row(self, sid: str) -> None:
        document = _compliant_document()
        statements = _statements(document, _CAPABILITIES_DYNAMODB)
        statements.append(
            {**statements[0], "sid": sid, "effect": "Allow", "actions": ["dynamodb:GetItem"]}
        )
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation", document))
        assert failed == ["(zero-grant) capabilities"], failed

    @pytest.mark.parametrize(
        ("actions", "not_actions"),
        [
            (["DynamoDB:GetItem"], None),
            (["DYNAMODB:*"], None),
            (["dynamo*"], None),
            (["*:*"], None),
            (["*:GetItem"], None),
            (["*"], None),
            (["d?namodb:Scan"], None),
            ([], ["s3:*"]),
            ([], ["dynamodb:GetItem"]),
        ],
        ids=[
            "mixed-case",
            "upper-wildcard",
            "service-prefix-glob",
            "star-colon-star",
            "any-service-getitem",
            "bare-star",
            "question-mark",
            "not-actions-other-service",
            "not-actions-one-dynamodb-action",
        ],
    )
    def test_every_dynamodb_granting_allow_shape_reds_the_zero_grant_row(
        self, actions: list[str], not_actions: list[str] | None
    ) -> None:
        document = _compliant_document()
        statements = _statements(document, _CAPABILITIES_DYNAMODB)
        statements.append(
            {
                **statements[0],
                "sid": "Planted",
                "effect": "Allow",
                "actions": actions,
                "not_actions": not_actions,
            }
        )
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation", document))
        assert failed == ["(zero-grant) capabilities"], failed

    @pytest.mark.parametrize("not_actions", [["dynamodb:*"], ["DynamoDB:*"], ["dyn*"], ["*"]])
    def test_an_allow_whose_not_actions_exclude_all_of_dynamodb_keeps_the_row_green(
        self, not_actions: list[str]
    ) -> None:
        document = _compliant_document()
        statements = _statements(document, _CAPABILITIES_DYNAMODB)
        statements.append(
            {
                **statements[0],
                "sid": "OtherServices",
                "effect": "Allow",
                "actions": [],
                "not_actions": not_actions,
            }
        )
        findings = self._findings("dynamodb_leading_keys_isolation", document)
        rows = [f for f in findings if f.resource == "(zero-grant) capabilities"]
        assert [f.status for f in rows] == ["pass"], rows

    @pytest.mark.parametrize(
        ("sid", "actions"),
        [("Extra", ["dynamodb:Scan"]), ("", ["dynamodb:DeleteItem"]), ("Extra", ["DynamoDB:Scan"])],
        ids=["extra-sid", "no-sid", "extra-sid-mixed-case"],
    )
    def test_an_allow_outside_the_grant_sids_reds_a_regular_role(
        self, sid: str, actions: list[str]
    ) -> None:
        document = _compliant_document()
        statements = _statements(document, _APPROVE_PROPOSAL_DYNAMODB)
        statements.append(
            {**statements[0], "sid": sid, "effect": "Allow", "actions": actions, "condition": []}
        )
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation", document))
        assert failed == [f"approve_proposal -> {sid or '(no sid)'}"], failed

    @pytest.mark.parametrize(
        ("actions", "not_actions"),
        [(["DYNAMODB:*"], None), (["dynamo*"], None), (["*:*"], None), ([], ["s3:*"])],
        ids=["upper-wildcard", "service-prefix-glob", "star-colon-star", "not-actions"],
    )
    def test_a_wildcard_allow_in_any_spelling_fails_no_wildcard_action(
        self, actions: list[str], not_actions: list[str] | None
    ) -> None:
        document = _compliant_document()
        statements = _statements(document, _APPROVE_PROPOSAL_DYNAMODB)
        statements.append(
            {
                **statements[0],
                "sid": "Wild",
                "effect": "Allow",
                "actions": actions,
                "not_actions": not_actions,
            }
        )
        failed = self._failed_subjects(self._findings("dynamodb_no_wildcard_action", document))
        assert failed == [f"{_APPROVE_PROPOSAL_DYNAMODB} statement[{len(statements) - 1}] 'Wild'"]

    def test_a_planted_pending_condition_is_absorbed_only_while_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = _compliant_document()
        for statement in _statements(document, _EVAL_TRIGGER_DYNAMODB):
            if statement["sid"].startswith("DynamoDbEvalruns"):
                statement["condition"] = []
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation", document))
        assert "eval-trigger -> DynamoDbEvalruns0" in failed, failed
        monkeypatch.setattr(
            gate,
            "FIXTURE_PENDING_CONDITIONS",
            {"eval-trigger:eval_runs": ("planted", "eval_trigger_eval_runs_grants")},
        )
        findings = self._findings("dynamodb_leading_keys_isolation", document)
        assert not self._failed_subjects(findings), self._failed_subjects(findings)
        subject = "(declared-pending-condition) eval-trigger:eval_runs"
        assert [f.status for f in findings if f.resource == subject] == ["pass"]

    def test_a_pending_condition_source_does_not_carry_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gate,
            "FIXTURE_PENDING_CONDITIONS",
            {"eval-trigger:eval_runs": ("x", "eval_runs_grant_that_does_not_exist")},
        )
        failed = self._failed_subjects(self._findings("dynamodb_leading_keys_isolation"))
        assert "(declared-pending-condition) eval-trigger:eval_runs" in failed, failed


# ---------------------------------------------------------------------------
# `key_prefix`: a LeadingKeys condition that confines a key family without pinning identity.
# ---------------------------------------------------------------------------


class TestKeyPrefixMechanism:
    @staticmethod
    def _with_mechanism(
        monkeypatch: pytest.MonkeyPatch,
        function: str,
        table: str,
        mechanism: str,
        *,
        table_can_pin_identity: bool | None = None,
    ) -> list[Any]:
        matrix = copy.deepcopy(gate._load_table_action_role_matrix())
        if table_can_pin_identity is not None:
            matrix["tables"][table]["leading_keys_can_enforce_rep_isolation"] = (
                table_can_pin_identity
            )
        rows = [g for g in matrix["grants"] if g["function"] == function and g["table"] == table]
        assert rows, (function, table)
        for row in rows:
            row["isolation_mechanism"] = mechanism
        monkeypatch.setattr(gate, "_load_table_action_role_matrix", lambda: matrix)
        return [
            f
            for f in gate.check_dynamodb_leading_keys_isolation(load_plan("compliant"))
            if f.status == "fail"
        ]

    def test_the_legend_defines_key_prefix(self) -> None:
        legend = gate._load_table_action_role_matrix()["isolation_mechanism_legend"]
        assert "key_prefix" in legend and "prefix only" in legend["key_prefix"]

    def test_a_conditioned_prefix_grant_passes_as_key_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """identity-interceptor `reps` is the fixture's one LeadingKeys-conditioned grant."""
        assert not self._with_mechanism(monkeypatch, "identity-interceptor", "reps", "key_prefix")

    def test_on_a_table_that_cannot_pin_identity_only_key_prefix_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`leading_keys` claims an identity pin; `key_prefix` does not, so only it survives the flag."""
        failed = self._with_mechanism(
            monkeypatch,
            "identity-interceptor",
            "reps",
            "leading_keys",
            table_can_pin_identity=False,
        )
        assert any(
            f.resource.startswith("identity-interceptor ->")
            and "leading_keys_can_enforce_rep_isolation=false" in f.detail
            for f in failed
        ), failed
        survivors = self._with_mechanism(
            monkeypatch, "identity-interceptor", "reps", "key_prefix", table_can_pin_identity=False
        )
        assert not [f for f in survivors if f.resource.startswith("identity-interceptor ->")], (
            survivors
        )

    def test_a_condition_under_neither_label_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failed = self._with_mechanism(
            monkeypatch, "identity-interceptor", "reps", "application_code"
        )
        assert any("LeadingKeys present=True" in f.detail for f in failed), failed

    def test_key_prefix_without_a_condition_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failed = self._with_mechanism(monkeypatch, "pre-token-generation", "reps", "key_prefix")
        assert any("LeadingKeys present=False" in f.detail for f in failed), failed

    def test_the_relabelled_rows_are_exactly_the_prefix_only_grants(self) -> None:
        """The prefix-only rows (the sweep functions', their tenant `Query` rows and sweep-task's
        seed-key reads); the two interceptors pin one rep id and keep `leading_keys`."""
        matrix = gate._load_table_action_role_matrix()
        labels = {
            (g["function"], g["table"], tuple(g["actions"])): g["isolation_mechanism"]
            for g in matrix["grants"]
            if g["isolation_mechanism"] in gate._CONDITIONED_MECHANISMS
        }
        sweep_rows = {
            **{
                ("sweep-control", table, ("dynamodb:Query",)): "key_prefix"
                for table in _SWEEP_TENANT_TABLES
            },
            **{
                ("sweep-task", table, ("dynamodb:Query",)): "key_prefix"
                for table in _SWEEP_SEED_TABLES
            },
            **{
                ("sweep-task", table, ("dynamodb:GetItem",)): "key_prefix"
                for table in _SWEEP_SEED_TABLES
            },
        }
        assert {key[:2]: value for key, value in labels.items() if key not in sweep_rows} == {
            ("identity-interceptor", "reps"): "leading_keys",
            ("chat-identity-interceptor", "reps"): "leading_keys",
            ("eval-trigger", "eval_runs"): "key_prefix",
            ("user-settings", "reps"): "key_prefix",
            ("chat-target", "reps"): "key_prefix",
            ("sweep-control", "eval_runs"): "key_prefix",
            ("sweep-control", "reps"): "key_prefix",
            ("sweep-task", "eval_runs"): "key_prefix",
        }
        assert {key: value for key, value in labels.items() if key in sweep_rows} == sweep_rows


# ---------------------------------------------------------------------------
# A pair split across matrix rows; each statement is judged by the one row covering it.
# ---------------------------------------------------------------------------

_SWEEP_SEED_TABLES = ("accounts", "contacts", "opportunities", "activities", "notes")
_SWEEP_TENANT_TABLES = (*_SWEEP_SEED_TABLES, "audit_records", "proposals", "usage_records")

#: The sorted matrix mechanisms of each sweep tenant pair.
_SWEEP_PAIR_MECHANISMS: dict[tuple[str, str], list[str]] = {
    **{
        ("sweep-control", table): ["application_code", "key_prefix"]
        for table in (*_SWEEP_SEED_TABLES, "audit_records", "proposals", "usage_records")
    },
    **{("sweep-task", table): ["key_prefix", "key_prefix"] for table in _SWEEP_SEED_TABLES},
    ("sweep-task", "proposals"): ["application_code"],
    ("sweep-task", "audit_records"): [],
    ("sweep-task", "usage_records"): [],
}


def _fixture_sid(module: str, table_token: str, actions: set[str]) -> str:
    """The sid of `module`'s one compliant-fixture DynamoDB statement on `table_token` with
    exactly `actions`."""
    (change,) = [
        c
        for c in load_plan("compliant").resource_changes
        if c["address"] == f"module.lambda_action_{module}.data.aws_iam_policy_document.dynamodb[0]"
    ]
    (sid,) = [
        s["sid"]
        for s in change["change"]["after"]["statement"]
        if re.fullmatch(rf"DynamoDb{table_token}\d+", s["sid"]) and set(s["actions"]) == actions
    ]
    return str(sid)


class TestSplitMatrixRows:
    @staticmethod
    def _failed_with(monkeypatch: pytest.MonkeyPatch, edit: Any) -> list[Any]:
        matrix = copy.deepcopy(gate._load_table_action_role_matrix())
        edit(matrix["grants"])
        monkeypatch.setattr(gate, "_load_table_action_role_matrix", lambda: matrix)
        return [
            f
            for f in gate.check_dynamodb_leading_keys_isolation(load_plan("compliant"))
            if f.status == "fail"
        ]

    @staticmethod
    def _row(grants: list[dict[str, Any]], function: str, table: str, action: str) -> Any:
        (row,) = [
            g
            for g in grants
            if g["function"] == function and g["table"] == table and action in g["actions"]
        ]
        return row

    def test_the_sweep_tenant_pairs_are_split_and_pass(self) -> None:
        matrix = gate._load_table_action_role_matrix()
        for (function, table), expected in _SWEEP_PAIR_MECHANISMS.items():
            rows = [
                g for g in matrix["grants"] if g["function"] == function and g["table"] == table
            ]
            assert sorted(r["isolation_mechanism"] for r in rows) == expected, (function, table)
        findings = gate.check_dynamodb_leading_keys_isolation(load_plan("compliant"))
        assert not [f for f in findings if f.status == "fail"]

    def test_a_conditioned_statement_under_an_application_code_row_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def edit(grants: list[dict[str, Any]]) -> None:
            self._row(grants, "sweep-task", "accounts", "dynamodb:Query")["isolation_mechanism"] = (
                "application_code"
            )

        failed = self._failed_with(monkeypatch, edit)
        sid = _fixture_sid("sweep_task", "Accounts", {"dynamodb:Query"})
        assert [f.resource for f in failed] == [f"sweep-task -> {sid}"], failed
        assert "LeadingKeys present=True" in failed[0].detail

    def test_an_unconditioned_statement_under_a_key_prefix_row_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def edit(grants: list[dict[str, Any]]) -> None:
            self._row(grants, "sweep-control", "accounts", "dynamodb:BatchWriteItem")[
                "isolation_mechanism"
            ] = "key_prefix"

        failed = self._failed_with(monkeypatch, edit)
        sid = _fixture_sid("sweep_control", "Accounts", {"dynamodb:BatchWriteItem"})
        assert [f.resource for f in failed] == [f"sweep-control -> {sid}"], failed
        assert "LeadingKeys present=False" in failed[0].detail

    def test_a_statement_no_single_row_covers_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The run-row statement's actions split across two rows: each row's mechanism agrees with
        the statement's condition, and the statement still fails because no one row covers it."""

        def edit(grants: list[dict[str, Any]]) -> None:
            base = self._row(grants, "sweep-control", "eval_runs", "dynamodb:PutItem")
            moved = [a for a in base["actions"] if a != "dynamodb:PutItem"]
            base["actions"] = ["dynamodb:PutItem"]
            grants.append({**base, "actions": moved})

        failed = self._failed_with(monkeypatch, edit)
        sid = _fixture_sid("sweep_control", "Evalruns", {"dynamodb:GetItem", "dynamodb:PutItem"})
        assert [f.resource for f in failed] == [f"sweep-control -> {sid}"], failed
        assert "covered by 0 of the pair's 2 matrix rows" in failed[0].detail

    def test_a_statement_two_rows_cover_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def edit(grants: list[dict[str, Any]]) -> None:
            query = self._row(grants, "sweep-task", "notes", "dynamodb:Query")
            grants.append(dict(query))

        failed = self._failed_with(monkeypatch, edit)
        sid = _fixture_sid("sweep_task", "Notes", {"dynamodb:Query"})
        assert [f.resource for f in failed] == [f"sweep-task -> {sid}"], failed
        assert "covered by 2 of the pair's 3 matrix rows" in failed[0].detail


# ---------------------------------------------------------------------------
# Source-list parsing and the per-statement pending condition.
# ---------------------------------------------------------------------------


class TestRootLocalParsing:
    def test_a_bracket_in_a_comment_cannot_hide_a_fourth_invoker(self, tmp_path: Path) -> None:
        """A bracketed tag in a comment must not end the list before an added principal."""
        root_tf = _RESOURCE_POLICY_TF.replace(
            "    module.lambda_action_chat_target.role_arn,\n",
            "    module.lambda_action_chat_target.role_arn,\n"
            "    # see [TAG-1]\n"
            "    aws_iam_role.someone_else.arn,\n",
            1,
        )
        assert "[TAG-1]" in root_tf and "someone_else" in root_tf
        failed = _failed(
            _relay_findings(tmp_path, "runtime_resource_policy_sole_invoker", root_tf=root_tf)
        )
        assert "local.runtime_invoker_role_arns" in failed, failed

    @staticmethod
    def _gate_against(tmp_path: Path, main_tf: str) -> list[Any]:
        (tmp_path / "main.tf").write_text(main_tf, encoding="utf-8")
        original = (gate.INFRA_DIR, gate.FIXTURE_PENDING_CONDITIONS)
        try:
            gate.INFRA_DIR = tmp_path
            gate.FIXTURE_PENDING_CONDITIONS = {
                "eval-trigger:eval_runs": ("planted", "eval_trigger_eval_runs_grants")
            }
            return gate.check_dynamodb_leading_keys_isolation(load_plan("compliant"))
        finally:
            gate.INFRA_DIR, gate.FIXTURE_PENDING_CONDITIONS = original

    def test_dropping_the_base_table_condition_reds_the_pending_condition(
        self, tmp_path: Path
    ) -> None:
        """The gsi_status statement keeping its condition must not excuse the base table."""
        main_tf = (gate.INFRA_DIR / "main.tf").read_text(encoding="utf-8")
        block = re.search(r"(?ms)^  eval_trigger_eval_runs_grants = \[\n(.*?)^  \]$", main_tf)
        assert block is not None
        first = re.search(r"(?ms)^    \{\n(.*?)^    \},?$", block.group(1))
        assert first is not None and "dynamodb:LeadingKeys" in first.group(1)
        stripped = re.sub(
            r"(?ms)^      leading_key_condition = \{\n.*?^      \}\n", "", first.group(1), count=1
        )
        assert stripped != first.group(1)
        mutated = main_tf.replace(first.group(1), stripped, 1)
        assert mutated.count("dynamodb:LeadingKeys") < main_tf.count("dynamodb:LeadingKeys")

        unmutated = self._gate_against(tmp_path, main_tf)
        subject = "(declared-pending-condition) eval-trigger:eval_runs"
        assert [f.status for f in unmutated if f.resource == subject] == ["deferred"]
        findings = self._gate_against(tmp_path, mutated)
        assert [f.status for f in findings if f.resource == subject] == ["fail"], findings

    def test_a_commented_condition_does_not_satisfy_the_pending_condition(
        self, tmp_path: Path
    ) -> None:
        main_tf = (gate.INFRA_DIR / "main.tf").read_text(encoding="utf-8")
        mutated = re.sub(
            r'(?m)^(\s+)(attribute = "dynamodb:LeadingKeys")',
            r"\1# \2",
            main_tf,
        )
        assert mutated != main_tf
        findings = self._gate_against(tmp_path, mutated)
        subject = "(declared-pending-condition) eval-trigger:eval_runs"
        assert [f.status for f in findings if f.resource == subject] == ["fail"], findings


# ---------------------------------------------------------------------------
# dynamodb_data_plane_principal_deny
# ---------------------------------------------------------------------------

#: What `_principal_deny_tree` copies: the `.tf` sources only. The gate reads nothing else, so
#: provider caches (`infra/.terraform`, ~700 MB) and build output are never copied.
PRINCIPAL_DENY_TREE_GLOB = "*.tf"
PRINCIPAL_DENY_TREE_EXCLUDED_PARTS = frozenset({".terraform"})

_PRINCIPAL_DENY = "dynamodb_data_plane_principal_deny"
_TABLE_POLICY = "modules/dynamodb/aws_dynamodb_resource_policy.tables"
_KEY_POLICY = "modules/dynamodb/aws_kms_key_policy.dynamodb"


def _principal_deny_tree(root: Path) -> Path:
    """A copy of `infra/`'s `.tf` files under `root`, laid out as in the repository."""
    for path in gate.INFRA_DIR.rglob(PRINCIPAL_DENY_TREE_GLOB):
        relative = path.relative_to(gate.INFRA_DIR)
        if PRINCIPAL_DENY_TREE_EXCLUDED_PARTS & set(relative.parts):
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    return root


def _principal_deny_findings(infra: Path) -> list[Any]:
    control = next(c for c in gate.CONTROLS if c.control_id == _PRINCIPAL_DENY)
    original = gate.INFRA_DIR
    try:
        gate.INFRA_DIR = infra
        return control.fn(gate.Plan(resource_changes=[]))
    finally:
        gate.INFRA_DIR = original


def _mutate(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= 1, f"{old!r} not found in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _statement_span(text: str, sid: str) -> tuple[int, int]:
    """The `{ ... }` span of the jsonencode statement whose Sid is `sid`."""
    sid_at = text.index(f'"{sid}"')
    start = text.rindex("{", 0, sid_at)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
    raise AssertionError(f"unbalanced statement {sid}")


def _mutate_statement(path: Path, sid: str, old: str, new: str) -> None:
    """Replace `old` with `new` once, inside the statement `sid` only."""
    text = path.read_text(encoding="utf-8")
    start, end = _statement_span(text, sid)
    statement = text[start:end]
    assert old in statement, f"{old!r} not in statement {sid}"
    path.write_text(text[:start] + statement.replace(old, new, 1) + text[end:], encoding="utf-8")


class TestDynamodbDataPlanePrincipalDeny:
    """The principal-deny source gate passes on `infra/` and reds exactly one row per planted defect."""

    @pytest.fixture
    def infra(self, tmp_path: Path) -> Path:
        return _principal_deny_tree(tmp_path / "infra")

    def _failed_rows(self, infra: Path) -> dict[str, str]:
        return _failed(_principal_deny_findings(infra))

    def test_the_unmutated_copy_passes(self, infra: Path) -> None:
        findings = _principal_deny_findings(infra)
        assert len(findings) >= 15
        assert not _failed(findings), _failed(findings)

    def test_the_copy_holds_only_tf_sources(self, infra: Path) -> None:
        copied = {p.suffix for p in infra.rglob("*") if p.is_file()}
        assert copied == {".tf"}
        assert not any(".terraform" in p.parts for p in infra.rglob("*"))

    def test_the_repository_tree_passes(self) -> None:
        assert not _failed(_principal_deny_findings(gate.INFRA_DIR))

    @pytest.mark.parametrize(
        ("file", "sid", "old", "new", "row"),
        [
            pytest.param(
                "modules/dynamodb/policy.tf", gate.DYNAMODB_TABLE_DATA_PLANE_SID,
                "ArnNotEquals", "ArnNotEqualsIfExists",
                f"{_TABLE_POLICY}#{gate.DYNAMODB_TABLE_DATA_PLANE_SID}", id="if-exists",
            ),
            pytest.param(
                "modules/dynamodb/policy.tf", gate.DYNAMODB_TABLE_ADMIN_SID,
                "ArnNotEquals", "StringNotEquals",
                f"{_TABLE_POLICY}#{gate.DYNAMODB_TABLE_ADMIN_SID}", id="string-not-equals",
            ),
            pytest.param(
                "modules/dynamodb/kms.tf", gate.DYNAMODB_KEY_USE_SID,
                '"aws:PrincipalArn"', '"aws:userid"',
                f"{_KEY_POLICY}#{gate.DYNAMODB_KEY_USE_SID}", id="another-key",
            ),
            pytest.param(
                "modules/dynamodb/kms.tf", gate.DYNAMODB_KEY_USE_SID,
                '"kms:CreateGrant",', "",
                f"{_KEY_POLICY}#{gate.DYNAMODB_KEY_USE_SID}", id="missing-key-use-action",
            ),
            pytest.param(
                "modules/dynamodb/policy.tf", gate.DYNAMODB_TABLE_ADMIN_SID,
                '"dynamodb:UpdateContributorInsights",', "",
                f"{_TABLE_POLICY}#{gate.DYNAMODB_TABLE_ADMIN_SID}", id="missing-table-admin-action",
            ),
            pytest.param(
                "modules/dynamodb/kms.tf", gate.DYNAMODB_KEY_ADMIN_SID,
                '"kms:DisableKeyRotation",', "",
                f"{_KEY_POLICY}#{gate.DYNAMODB_KEY_ADMIN_SID}", id="missing-key-admin-action",
            ),
            pytest.param(
                "modules/dynamodb/policy.tf", gate.DYNAMODB_TABLE_DATA_PLANE_SID,
                "ArnNotEquals = {",
                'Bool = { "aws:ViaAWSService" = "false" }\n          ArnNotEquals = {',
                f"{_TABLE_POLICY}#{gate.DYNAMODB_TABLE_DATA_PLANE_SID}", id="extra-via-aws-service",
            ),
            pytest.param(
                "modules/dynamodb/kms.tf", gate.DYNAMODB_KEY_USE_SID,
                'Bool         = { "aws:PrincipalIsAWSService" = "false" }', "",
                f"{_KEY_POLICY}#{gate.DYNAMODB_KEY_USE_SID}", id="service-exemption-dropped",
            ),
            pytest.param(
                "modules/dynamodb/policy.tf", gate.DYNAMODB_TABLE_DATA_PLANE_SID,
                "ArnNotEquals = {",
                'Bool = { "aws:PrincipalIsAWSService" = "false" }\n          ArnNotEquals = {',
                f"{_TABLE_POLICY}#{gate.DYNAMODB_TABLE_DATA_PLANE_SID}",
                id="service-exemption-on-table-deny",
            ),
            pytest.param(
                "modules/dynamodb/policy.tf", gate.DYNAMODB_TABLE_DATA_PLANE_SID,
                'Principal = "*"', 'Principal = { AWS = "*" }',
                f"{_TABLE_POLICY}#{gate.DYNAMODB_TABLE_DATA_PLANE_SID}", id="aws-star-principal",
            ),
            pytest.param(
                "modules/dynamodb/kms.tf", gate.DYNAMODB_KEY_ROOT_SID,
                '"kms:*"', '"kms:Decrypt"',
                f"{_KEY_POLICY}#{gate.DYNAMODB_KEY_ROOT_SID}", id="root-statement-narrowed",
            ),
        ],
    )  # fmt: skip
    def test_a_statement_defect_reds_its_own_row_only(
        self, infra: Path, file: str, sid: str, old: str, new: str, row: str
    ) -> None:
        _mutate_statement(infra / file, sid, old, new)
        assert list(self._failed_rows(infra)) == [row]

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            pytest.param(
                "arn:aws:iam::${var.account_id}:root",
                "arn:aws:iam::${var.partner_account_id}:root",
                id="another-variable",
            ),
            pytest.param(
                "arn:aws:iam::${var.account_id}:root",
                "arn:aws:iam::${local.anything}:root",
                id="a-local",
            ),
            pytest.param(
                "arn:aws:iam::${var.account_id}:root",
                "arn:${var.partition}:iam::${var.account_id}:root",
                id="unpinned-partition",
            ),
        ],
    )
    def test_a_root_principal_not_pinned_to_the_account_variable_reds_the_root_row_only(
        self, infra: Path, old: str, new: str
    ) -> None:
        _mutate_statement(infra / "modules/dynamodb/kms.tf", gate.DYNAMODB_KEY_ROOT_SID, old, new)
        assert list(self._failed_rows(infra)) == [f"{_KEY_POLICY}#{gate.DYNAMODB_KEY_ROOT_SID}"]

    def test_the_partition_data_source_is_an_accepted_root_partition(self, infra: Path) -> None:
        _mutate_statement(
            infra / "modules/dynamodb/kms.tf",
            gate.DYNAMODB_KEY_ROOT_SID,
            "arn:aws:iam",
            "arn:${data.aws_partition.current.partition}:iam",
        )
        assert not self._failed_rows(infra)

    def test_a_renamed_root_sid_reds_the_statement_list_and_the_root_row(self, infra: Path) -> None:
        _mutate(infra / "modules/dynamodb/kms.tf", f'"{gate.DYNAMODB_KEY_ROOT_SID}"', '"Root"')
        assert sorted(self._failed_rows(infra)) == sorted(
            [f"{_KEY_POLICY} (statements)", f"{_KEY_POLICY}#{gate.DYNAMODB_KEY_ROOT_SID}"]
        )

    def test_root_in_the_data_plane_list_reds_the_wiring_row_only(self, infra: Path) -> None:
        _mutate(
            infra / "main.tf",
            "concat(local.dynamodb_data_plane_role_arns, var.crm_operator_principal_arns)",
            "concat(local.dynamodb_data_plane_role_arns, var.crm_operator_principal_arns, "
            '["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"])',
        )
        assert list(self._failed_rows(infra)) == ["module.dynamodb.data_plane_principal_arns"]

    def test_a_grant_holding_module_missing_from_the_local_reds_the_local_row_only(
        self, infra: Path
    ) -> None:
        _mutate(infra / "main.tf", "      module.lambda_action_user_settings.role_arn,\n", "")
        failed = self._failed_rows(infra)
        assert list(failed) == [f"local.{gate.DYNAMODB_DATA_PLANE_LOCAL}"]
        assert (
            "module.lambda_action_user_settings"
            in failed[f"local.{gate.DYNAMODB_DATA_PLANE_LOCAL}"]
        )

    def test_a_local_entry_without_a_grant_reds_the_local_row_only(self, infra: Path) -> None:
        settings = infra / "user_settings.tf"
        text = settings.read_text(encoding="utf-8")
        assert len(re.findall(r"dynamodb_kms_grant_enabled\s*=\s*true", text)) == 1
        settings.write_text(
            re.sub(
                r"dynamodb_kms_grant_enabled(\s*)=\s*true",
                r"dynamodb_kms_grant_enabled\1= false",
                text,
            ),
            encoding="utf-8",
        )
        failed = self._failed_rows(infra)
        assert list(failed) == [f"local.{gate.DYNAMODB_DATA_PLANE_LOCAL}"]
        assert (
            "without dynamodb_kms_grant_enabled"
            in failed[f"local.{gate.DYNAMODB_DATA_PLANE_LOCAL}"]
        )

    def test_a_non_module_local_entry_reds_the_local_row_only(self, infra: Path) -> None:
        _mutate(
            infra / "main.tf",
            "      module.lambda_action_user_settings.role_arn,\n",
            "      module.lambda_action_user_settings.role_arn,\n      aws_iam_role.someone.arn,\n",
        )
        assert list(self._failed_rows(infra)) == [f"local.{gate.DYNAMODB_DATA_PLANE_LOCAL}"]

    @pytest.mark.parametrize(
        ("file", "policy"),
        [("modules/dynamodb/policy.tf", _TABLE_POLICY), ("modules/dynamodb/kms.tf", _KEY_POLICY)],
    )
    def test_a_deleted_precondition_reds_its_own_row_only(
        self, infra: Path, file: str, policy: str
    ) -> None:
        path = infra / file
        text = path.read_text(encoding="utf-8")
        stripped = re.sub(r"(?ms)^  lifecycle \{\n.*?^  \}\n", "", text, count=1)
        assert stripped != text
        path.write_text(stripped, encoding="utf-8")
        assert list(self._failed_rows(infra)) == [f"{policy} (precondition)"]

    def test_confirm_remove_self_resource_access_true_reds_its_own_row_only(
        self, infra: Path
    ) -> None:
        _mutate(
            infra / "modules/dynamodb/policy.tf",
            "  for_each = local.tables\n",
            "  for_each = local.tables\n\n  confirm_remove_self_resource_access = true\n",
        )
        assert list(self._failed_rows(infra)) == [
            f"{_TABLE_POLICY} (confirm_remove_self_resource_access)"
        ]

    def test_a_bypassed_lockout_check_reds_its_own_row_only(self, infra: Path) -> None:
        _mutate(
            infra / "modules/dynamodb/kms.tf",
            "bypass_policy_lockout_safety_check = false",
            "bypass_policy_lockout_safety_check = true",
        )
        assert list(self._failed_rows(infra)) == [f"{_KEY_POLICY} (key_id, lockout check)"]

    def test_an_operator_variable_default_reds_its_own_row_only(self, infra: Path) -> None:
        _mutate(
            infra / "variables.tf",
            'variable "crm_operator_principal_arns" {\n',
            'variable "crm_operator_principal_arns" {\n  default = []\n',
        )
        assert list(self._failed_rows(infra)) == [gate.CRM_OPERATOR_PRINCIPALS_VAR]

    def test_a_removed_table_policy_fails_its_count(self, infra: Path) -> None:
        (infra / "modules/dynamodb/policy.tf").unlink()
        failed = self._failed_rows(infra)
        assert "(count) modules/dynamodb/aws_dynamodb_resource_policy" in failed


class TestRenderedPolicyStatements:
    """`rendered_policy_statements` reads `jsonencode` and `aws_iam_policy_document` alike."""

    _JSONENCODE = """
resource "aws_kms_key_policy" "k" {
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyIt"
        Effect    = "Deny"
        Principal = "*" # everyone
        Action    = ["kms:Decrypt", "kms:Encrypt"]
        Resource  = "*"
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = var.allowed }
          Bool         = { "aws:PrincipalIsAWSService" = "false" }
        }
      },
    ]
  })
}
"""

    _DOCUMENT = """
data "aws_iam_policy_document" "doc" {
  statement {
    sid     = "DenyIt"
    effect  = "Deny"
    actions = ["kms:Decrypt", "kms:Encrypt"]
    resources = ["*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = var.allowed
    }
    condition {
      test     = "Bool"
      variable = "aws:PrincipalIsAWSService"
      values   = ["false"]
    }
  }
}

resource "aws_kms_key_policy" "k" {
  policy = data.aws_iam_policy_document.doc.json
}
"""

    def _statements(self, tmp_path: Path, source: str) -> list[Any]:
        module = tmp_path / "infra" / "modules" / "m"
        module.mkdir(parents=True)
        (module / "main.tf").write_text(source, encoding="utf-8")
        original = gate.INFRA_DIR
        try:
            gate.INFRA_DIR = tmp_path / "infra"
            ((_, _, body),) = gate.tf_resource_blocks("aws_kms_key_policy")
            return list(gate.rendered_policy_statements("modules/m", body))
        finally:
            gate.INFRA_DIR = original

    def test_both_forms_render_the_same_statement(self, tmp_path: Path) -> None:
        encoded = self._statements(tmp_path / "a", self._JSONENCODE)
        documented = self._statements(tmp_path / "b", self._DOCUMENT)
        assert encoded == documented
        (statement,) = encoded
        assert statement.principal == "*"
        assert statement.conditions == {
            "ArnNotEquals": {"aws:PrincipalArn": (gate.HclExpr("var.allowed"),)},
            "Bool": {"aws:PrincipalIsAWSService": ("false",)},
        }

    def test_an_unreadable_policy_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="neither jsonencode"):
            self._statements(
                tmp_path, 'resource "aws_kms_key_policy" "k" {\n  policy = file("p.json")\n}\n'
            )


def test_the_gate_constants_equal_the_smoke_constants() -> None:
    """A4 parity: the source gate and the T3 smoke assert the same sids and ordered actions."""
    from scripts.smoke import dynamodb_principal_deny as smoke

    assert (gate.DYNAMODB_TABLE_DATA_PLANE_SID, gate.DYNAMODB_TABLE_DATA_PLANE_ACTIONS) == (
        smoke.TABLE_DATA_PLANE_SID,
        smoke.TABLE_DATA_PLANE_ACTIONS,
    )
    assert (gate.DYNAMODB_TABLE_ADMIN_SID, gate.DYNAMODB_TABLE_ADMIN_ACTIONS) == (
        smoke.TABLE_ADMIN_SID,
        smoke.TABLE_ADMIN_ACTIONS,
    )
    assert gate.DYNAMODB_KEY_ROOT_SID == smoke.KEY_ROOT_SID
    assert (gate.DYNAMODB_KEY_USE_SID, gate.DYNAMODB_KEY_USE_ACTIONS) == (
        smoke.KEY_USE_SID,
        smoke.KEY_USE_ACTIONS,
    )
    assert (gate.DYNAMODB_KEY_ADMIN_SID, gate.DYNAMODB_KEY_ADMIN_ACTIONS) == (
        smoke.KEY_ADMIN_SID,
        smoke.KEY_ADMIN_ACTIONS,
    )
    assert "kms:RetireGrant" not in gate.DYNAMODB_KEY_ADMIN_ACTIONS


def test_the_principal_deny_control_is_registered_and_source_derived() -> None:
    (spec,) = [c for c in gate.CONTROLS if c.control_id == _PRINCIPAL_DENY]
    assert spec.module == "dynamodb"
    assert _PRINCIPAL_DENY in gate.NO_SUBJECT_TYPE_CONTROLS
    assert gate.subject_resource_types()[_PRINCIPAL_DENY] == ()


def test_the_self_check_maps_the_principal_deny_baseline_row() -> None:
    rows = [f for f in gate.run_self_check() if f.control == "self_check"]
    (row,) = [f for f in rows if "table and CMK policies" in f.resource]
    assert row.status == "pass", row
    assert len([f for f in rows if f.resource.startswith("DynamoDB IAM | ")]) == 2


def test_the_self_check_fails_the_row_when_the_control_is_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate, "CONTROLS", [c for c in gate.CONTROLS if c.control_id != _PRINCIPAL_DENY]
    )
    rows = [f for f in gate.run_self_check() if f.control == "self_check"]
    (row,) = [f for f in rows if "table and CMK policies" in f.resource]
    assert row.status == "fail"


def test_the_table_and_key_policy_types_are_in_the_fixture_and_not_declared_absent() -> None:
    """The fixture holds the table resource policies and the key policies; neither is declared
    absent, and fixture currency reports no row for either type."""
    planned = {r.get("type") for r in load_plan("compliant").resource_changes}
    for resource_type in ("aws_dynamodb_resource_policy", "aws_kms_key_policy"):
        assert resource_type in planned
        assert resource_type not in gate.FIXTURE_ABSENT_TYPES
    currency = {f.resource: f.status for f in gate.check_fixture_currency()}
    assert "aws_dynamodb_resource_policy" not in currency
    assert "(declared-absent) aws_dynamodb_resource_policy" not in currency
    assert set(currency.values()) == {"pass"}, currency


def _sources_into_separate_roots(infra: Path) -> list[str]:
    """Every stack `.tf` `source` that resolves into a `SEPARATE_ROOT_DIRS` directory."""
    separate = [(infra / name).resolve() for name in gate.SEPARATE_ROOT_DIRS]
    offending: list[str] = []
    for path in sorted(infra.rglob("*.tf")):
        relative = path.relative_to(infra)
        if ".terraform" in relative.parts or relative.parts[0] in gate.SEPARATE_ROOT_DIRS:
            continue
        text = path.read_text(encoding="utf-8")
        for source in re.findall(r'^\s*source\s*=\s*"(\.{1,2}/[^"]*)"', text, re.MULTILINE):
            resolved = (path.parent / source).resolve()
            if any(resolved == root or root in resolved.parents for root in separate):
                offending.append(f"{relative}: {source}")
    return offending


def test_no_stack_module_source_resolves_into_a_separate_root() -> None:
    """A stack using a separate root as a module would plan its excluded types uncounted."""
    assert gate.SEPARATE_ROOT_DIRS
    assert _sources_into_separate_roots(gate.INFRA_DIR) == []


@pytest.mark.parametrize(
    ("file", "source"),
    [("main.tf", "./bootstrap_account"), ("modules/vpc/extra.tf", "../../bootstrap_account/")],
)
def test_a_stack_source_into_a_separate_root_is_found(
    tmp_path: Path, file: str, source: str
) -> None:
    infra = _principal_deny_tree(tmp_path / "infra")
    target = infra / file
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f'\nmodule "planted" {{\n  source = "{source}"\n}}\n')
    assert _sources_into_separate_roots(infra) == [f"{file}: {source}"]


# ---------------------------------------------------------------------------
# store_principal_deny
# ---------------------------------------------------------------------------

_STORE_DENY = "store_principal_deny"
_LOGS_KEY = "modules/observability/aws_kms_key.logs"
_CLOUDTRAIL_KEY = "modules/observability/aws_kms_key.cloudtrail"
_CLOUDTRAIL_BUCKET = "modules/observability/aws_s3_bucket_policy.cloudtrail"
_FRONTEND_KEY = "modules/frontend_cdn/aws_kms_key_policy.frontend"
_ORIGIN_BUCKET = "modules/frontend_cdn/aws_s3_bucket_policy.origin"
_EVAL_BUCKET = "modules/eval_artifacts/aws_s3_bucket_policy.this"
_EVAL_KEY = "./aws_kms_key_policy.eval_artifacts"
_KEY_USE = gate.DYNAMODB_KEY_USE_SID
_KEY_ADMIN = gate.DYNAMODB_KEY_ADMIN_SID


def _store_deny_findings(infra: Path) -> list[Any]:
    control = next(c for c in gate.CONTROLS if c.control_id == _STORE_DENY)
    original = gate.INFRA_DIR
    try:
        gate.INFRA_DIR = infra
        return control.fn(gate.Plan(resource_changes=[]))
    finally:
        gate.INFRA_DIR = original


def _document_statement_span(text: str, sid: str) -> tuple[int, int]:
    """The `statement { ... }` span of the policy-document statement whose sid is `sid`."""
    (match,) = list(re.finditer(rf'^\s*sid\s*=\s*"{re.escape(sid)}"', text, re.MULTILINE))
    start = text.rindex("statement {", 0, match.start())
    depth = 0
    for index in range(text.index("{", start), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
    raise AssertionError(f"unbalanced statement {sid}")


def _mutate_document_statement(path: Path, sid: str, old: str, new: str) -> None:
    """Replace `old` with `new` once, inside the policy-document statement `sid` only."""
    text = path.read_text(encoding="utf-8")
    start, end = _document_statement_span(text, sid)
    statement = text[start:end]
    assert old in statement, f"{old!r} not in statement {sid}"
    path.write_text(text[:start] + statement.replace(old, new, 1) + text[end:], encoding="utf-8")


_SERVICE_EXEMPTION_BLOCK = """
    condition {
      test     = "Bool"
      variable = "aws:PrincipalIsAWSService"
      values   = ["false"]
    }
"""


class TestStorePrincipalDeny:
    """The store principal-deny source gate passes on `infra/` and reds exactly one row per defect."""

    @pytest.fixture
    def infra(self, tmp_path: Path) -> Path:
        return _principal_deny_tree(tmp_path / "infra")

    def _failed_rows(self, infra: Path) -> dict[str, str]:
        return _failed(_store_deny_findings(infra))

    def test_the_unmutated_copy_passes(self, infra: Path) -> None:
        findings = _store_deny_findings(infra)
        assert len(findings) >= 60
        assert not _failed(findings), _failed(findings)

    def test_the_repository_tree_passes(self) -> None:
        assert not _failed(_store_deny_findings(gate.INFRA_DIR))

    def test_every_store_policy_is_read(self) -> None:
        resources = {f.resource for f in _store_deny_findings(gate.INFRA_DIR)}
        for address in (
            _LOGS_KEY,
            _CLOUDTRAIL_KEY,
            _CLOUDTRAIL_BUCKET,
            _FRONTEND_KEY,
            _ORIGIN_BUCKET,
            _EVAL_BUCKET,
            _EVAL_KEY,
        ):
            assert f"(count) {address}" in resources
            assert f"{address} (precondition)" in resources

    @pytest.mark.parametrize(
        ("file", "sid", "old", "new", "row"),
        [
            pytest.param(
                "modules/observability/kms.tf", _KEY_USE,
                _SERVICE_EXEMPTION_BLOCK, "",
                f"{_LOGS_KEY}#{_KEY_USE}", id="exemption-dropped-from-logs-key-use",
            ),
            pytest.param(
                "modules/eval_artifacts/main.tf", gate.S3_DATA_PLANE_SID,
                "    condition {\n      test     = \"ArnNotEquals\"",
                _SERVICE_EXEMPTION_BLOCK.lstrip("\n").replace("Bool", "BoolIfExists")
                + "\n    condition {\n      test     = \"ArnNotEquals\"",
                f"{_EVAL_BUCKET}#{gate.S3_DATA_PLANE_SID}", id="exemption-added-to-eval-bucket",
            ),
            pytest.param(
                "modules/frontend_cdn/s3.tf", gate.S3_WRITE_SID,
                '"ArnNotEquals"', '"ArnNotEqualsIfExists"',
                f"{_ORIGIN_BUCKET}#{gate.S3_WRITE_SID}", id="if-exists-on-arn-not-equals",
            ),
            pytest.param(
                "modules/observability/cloudtrail.tf", _KEY_USE,
                '"Bool"', '"BoolIfExists"',
                f"{_CLOUDTRAIL_KEY}#{_KEY_USE}", id="if-exists-on-key-use-exemption",
            ),
            pytest.param(
                "modules/observability/cloudtrail.tf", gate.S3_DATA_PLANE_SID,
                '"BoolIfExists"', '"Bool"',
                f"{_CLOUDTRAIL_BUCKET}#{gate.S3_DATA_PLANE_SID}", id="bucket-exemption-not-if-exists",
            ),
            pytest.param(
                "modules/observability/cloudtrail.tf", gate.S3_ADMIN_SID,
                '"ArnNotEquals"', '"StringNotEquals"',
                f"{_CLOUDTRAIL_BUCKET}#{gate.S3_ADMIN_SID}", id="string-not-equals",
            ),
            pytest.param(
                "modules/observability/cloudtrail.tf", gate.S3_ADMIN_SID,
                '      "s3:PutMetricsConfiguration",\n', "",
                f"{_CLOUDTRAIL_BUCKET}#{gate.S3_ADMIN_SID}", id="missing-s3-admin-action",
            ),
            pytest.param(
                "modules/frontend_cdn/s3.tf", gate.S3_WRITE_SID,
                '      "s3:DeleteObjectVersionTagging",\n', "",
                f"{_ORIGIN_BUCKET}#{gate.S3_WRITE_SID}", id="missing-s3-write-action",
            ),
            pytest.param(
                "modules/frontend_cdn/kms.tf", _KEY_ADMIN,
                '      "kms:UpdateKeyDescription",\n', "",
                f"{_FRONTEND_KEY}#{_KEY_ADMIN}", id="missing-store-key-admin-action",
            ),
            pytest.param(
                "modules/eval_artifacts/main.tf", gate.S3_DATA_PLANE_SID,
                'resources = [local.bucket_arn, "${local.bucket_arn}/*"]',
                'resources = ["${local.bucket_arn}/*"]',
                f"{_EVAL_BUCKET}#{gate.S3_DATA_PLANE_SID}", id="bucket-resource-dropped",
            ),
            pytest.param(
                "modules/frontend_cdn/kms.tf", _KEY_USE,
                'type        = "*"', 'type        = "AWS"',
                f"{_FRONTEND_KEY}#{_KEY_USE}", id="aws-star-principal",
            ),
            pytest.param(
                "modules/frontend_cdn/kms.tf", "EnableIAMUserPermissions",
                "${data.aws_caller_identity.current.account_id}", "${var.partner_account_id}",
                f"{_FRONTEND_KEY}#EnableIAMUserPermissions", id="root-of-another-account",
            ),
            pytest.param(
                "modules/frontend_cdn/kms.tf", "AllowCloudFrontOACDecrypt",
                "    condition {",
                _SERVICE_EXEMPTION_BLOCK.lstrip("\n") + "\n    condition {",
                f"{_FRONTEND_KEY} (service exemption)", id="exemption-on-a-retained-statement",
            ),
        ],
    )  # fmt: skip
    def test_a_document_defect_reds_its_own_row_only(
        self, infra: Path, file: str, sid: str, old: str, new: str, row: str
    ) -> None:
        _mutate_document_statement(infra / file, sid, old, new)
        assert list(self._failed_rows(infra)) == [row]

    @pytest.mark.parametrize(
        ("sid", "old", "new"),
        [
            pytest.param(_KEY_ADMIN, "ArnNotEquals", "StringNotEquals", id="string-not-equals"),
            pytest.param(_KEY_USE, "Bool ", "BoolIfExists ", id="if-exists-exemption"),
            pytest.param(_KEY_USE, '"kms:CreateGrant",', "", id="missing-key-use-action"),
            pytest.param(
                _KEY_USE,
                "concat(local.eval_artifacts_principal_arns, var.crm_operator_principal_arns)",
                "var.crm_operator_principal_arns",
                id="allowlist-narrowed",
            ),
        ],
    )
    def test_a_root_key_policy_defect_reds_its_own_row_only(
        self, infra: Path, sid: str, old: str, new: str
    ) -> None:
        _mutate_statement(infra / "main.tf", sid, old, new)
        assert list(self._failed_rows(infra)) == [f"{_EVAL_KEY}#{sid}"]

    def test_a_renamed_deny_sid_reds_the_statement_list_and_its_row(self, infra: Path) -> None:
        _mutate(infra / "modules/frontend_cdn/s3.tf", f'"{gate.S3_WRITE_SID}"', '"DenyWrites"')
        assert sorted(self._failed_rows(infra)) == sorted(
            [f"{_ORIGIN_BUCKET} (statements)", f"{_ORIGIN_BUCKET}#{gate.S3_WRITE_SID}"]
        )

    def test_capabilities_dropped_from_the_logs_local_reds_the_local_row_only(
        self, infra: Path
    ) -> None:
        _mutate(infra / "main.tf", "      module.lambda_action_capabilities.role_arn,\n", "")
        failed = self._failed_rows(infra)
        assert list(failed) == [f"local.{gate.LOGS_KEY_USERS_LOCAL}"]
        assert (
            "module.lambda_action_capabilities" not in failed[f"local.{gate.LOGS_KEY_USERS_LOCAL}"]
            or "call sites missing" in failed[f"local.{gate.LOGS_KEY_USERS_LOCAL}"]
        )

    def test_a_tool_key_dropped_from_the_logs_local_reds_the_local_row_only(
        self, infra: Path
    ) -> None:
        _mutate(infra / "main.tf", '      module.lambda_action["get_page_context"].role_arn,\n', "")
        failed = self._failed_rows(infra)
        assert list(failed) == [f"local.{gate.LOGS_KEY_USERS_LOCAL}"]
        assert "get_page_context" in failed[f"local.{gate.LOGS_KEY_USERS_LOCAL}"]

    def test_a_local_entry_with_no_environment_kms_key_arn_reds_the_local_row_only(
        self, infra: Path
    ) -> None:
        path = infra / "main.tf"
        text = path.read_text(encoding="utf-8")
        start = text.index('module "lambda_action_capabilities" {')
        line = "  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn\n"
        at = text.index(line, start)
        path.write_text(text[:at] + text[at + len(line) :], encoding="utf-8")
        failed = self._failed_rows(infra)
        assert list(failed) == [f"local.{gate.LOGS_KEY_USERS_LOCAL}"]
        assert (
            "entries without environment_kms_key_arn"
            in failed[f"local.{gate.LOGS_KEY_USERS_LOCAL}"]
        )

    def test_a_non_module_logs_local_entry_reds_the_local_row_only(self, infra: Path) -> None:
        _mutate(
            infra / "main.tf",
            "      module.agent_relay.role_arn,\n",
            "      module.agent_relay.role_arn,\n      aws_iam_role.someone.arn,\n",
        )
        assert list(self._failed_rows(infra)) == [f"local.{gate.LOGS_KEY_USERS_LOCAL}"]

    def test_codebuild_dropped_from_the_eval_artifacts_local_reds_the_local_row_only(
        self, infra: Path
    ) -> None:
        _mutate(infra / "main.tf", "    module.agentcore.codebuild_role_arn,\n", "")
        assert list(self._failed_rows(infra)) == [f"local.{gate.EVAL_ARTIFACTS_PRINCIPALS_LOCAL}"]

    def test_a_stray_artefact_grant_reds_the_local_row_only(self, infra: Path) -> None:
        with (infra / "main.tf").open("a", encoding="utf-8") as handle:
            handle.write(
                '\nresource "aws_iam_role_policy" "stray" {\n'
                "  role   = module.lambda_action_sweep_task.role_name\n"
                "  policy = data.aws_iam_policy_document.stray.json\n}\n\n"
                'data "aws_iam_policy_document" "stray" {\n  statement {\n'
                '    actions   = ["s3:GetObject"]\n'
                '    resources = ["${module.eval_artifacts.bucket_arn}/*"]\n  }\n}\n'
            )
        failed = self._failed_rows(infra)
        assert list(failed) == [f"local.{gate.EVAL_ARTIFACTS_PRINCIPALS_LOCAL}"]
        assert (
            "module.lambda_action_sweep_task"
            in failed[f"local.{gate.EVAL_ARTIFACTS_PRINCIPALS_LOCAL}"]
        )

    def test_an_unattached_artefact_document_reds_the_local_row_only(self, infra: Path) -> None:
        with (infra / "modules/agentcore/ecr_codebuild.tf").open("a", encoding="utf-8") as handle:
            handle.write(
                '\ndata "aws_iam_policy_document" "loose" {\n  statement {\n'
                '    actions   = ["kms:Decrypt"]\n    resources = [var.source_kms_key_arn]\n  }\n}\n'
            )
        assert list(self._failed_rows(infra)) == [f"local.{gate.EVAL_ARTIFACTS_PRINCIPALS_LOCAL}"]

    def test_the_codebuild_output_removed_reds_its_own_row_only(self, infra: Path) -> None:
        path = infra / "modules/agentcore/outputs.tf"
        text = path.read_text(encoding="utf-8")
        stripped = re.sub(r'(?ms)^output "codebuild_role_arn" \{\n.*?^\}\n', "", text, count=1)
        assert stripped != text
        path.write_text(stripped, encoding="utf-8")
        assert list(self._failed_rows(infra)) == ["modules/agentcore output.codebuild_role_arn"]

    @pytest.mark.parametrize(
        ("file", "anchor", "row"),
        [
            ("modules/observability/kms.tf", 'resource "aws_kms_key" "logs"', _LOGS_KEY),
            ("modules/frontend_cdn/s3.tf", 'resource "aws_s3_bucket_policy" "origin"', _ORIGIN_BUCKET),
            ("modules/eval_artifacts/main.tf", 'resource "aws_s3_bucket_policy" "this"', _EVAL_BUCKET),
            ("main.tf", 'resource "aws_kms_key_policy" "eval_artifacts"', _EVAL_KEY),
        ],
    )  # fmt: skip
    def test_a_deleted_precondition_reds_its_own_row_only(
        self, infra: Path, file: str, anchor: str, row: str
    ) -> None:
        path = infra / file
        text = path.read_text(encoding="utf-8")
        start = text.index(anchor)
        stripped = text[:start] + re.sub(
            r"(?ms)^  lifecycle \{\n.*?^  \}\n", "", text[start:], count=1
        )
        assert stripped != text
        path.write_text(stripped, encoding="utf-8")
        assert list(self._failed_rows(infra)) == [f"{row} (precondition)"]

    @pytest.mark.parametrize(
        ("file", "anchor", "row"),
        [
            ("modules/observability/cloudtrail.tf", 'resource "aws_kms_key" "cloudtrail"', _CLOUDTRAIL_KEY),
            ("main.tf", 'resource "aws_kms_key_policy" "eval_artifacts"', _EVAL_KEY),
        ],
    )  # fmt: skip
    def test_a_bypassed_lockout_check_reds_its_own_row_only(
        self, infra: Path, file: str, anchor: str, row: str
    ) -> None:
        path = infra / file
        text = path.read_text(encoding="utf-8")
        start = text.index(anchor)
        flag = "bypass_policy_lockout_safety_check = false"
        at = text.index(flag, start)
        path.write_text(
            text[:at] + flag.replace("false", "true") + text[at + len(flag) :], encoding="utf-8"
        )
        assert list(self._failed_rows(infra)) == [f"{row} (bypass_policy_lockout_safety_check)"]

    def test_root_on_an_operator_list_reds_the_wiring_row_only(self, infra: Path) -> None:
        path = infra / "main.tf"
        text = path.read_text(encoding="utf-8")
        start = text.index('module "frontend_cdn" {')
        line = "  operator_principal_arns      = var.crm_operator_principal_arns\n"
        at = text.index(line, start)
        root = '["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]'
        planted = (
            f"  operator_principal_arns      = concat(var.crm_operator_principal_arns, {root})\n"
        )
        path.write_text(text[:at] + planted + text[at + len(line) :], encoding="utf-8")
        assert list(self._failed_rows(infra)) == ["module.frontend_cdn.operator_principal_arns"]

    def test_a_removed_store_policy_fails_its_count(self, infra: Path) -> None:
        path = infra / "modules/frontend_cdn/kms.tf"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                'resource "aws_kms_key_policy" "frontend"', 'resource "aws_kms_key_policy" "gone"'
            ),
            encoding="utf-8",
        )
        assert f"(count) {_FRONTEND_KEY}" in self._failed_rows(infra)


def test_the_store_control_is_registered_source_derived_and_mapped_to_both_docs_rows() -> None:
    (spec,) = [c for c in gate.CONTROLS if c.control_id == _STORE_DENY]
    assert spec.module == "store"
    assert spec.spec_row == gate.STORE_PRINCIPAL_DENY_SPEC_ROW
    assert _STORE_DENY in gate.NO_SUBJECT_TYPE_CONTROLS
    store_rows = [
        f
        for f in gate.run_self_check()
        if f.resource.startswith(("S3 encryption | ", "KMS | ")) and "policies deny" in f.resource
    ]
    assert len(store_rows) == 2
    assert all(f.status == "pass" for f in store_rows)


def test_the_self_check_fails_both_store_rows_when_the_control_is_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "CONTROLS", [c for c in gate.CONTROLS if c.control_id != _STORE_DENY])
    rows = [
        f
        for f in gate.run_self_check()
        if f.resource.startswith(("S3 encryption | ", "KMS | ")) and "policies deny" in f.resource
    ]
    assert len(rows) == 2
    assert all(f.status == "fail" for f in rows)


def test_the_store_gate_constants_equal_the_smoke_constants() -> None:
    """The source gate and the T3 smoke hold the same tuples, sids and exemptions."""
    from scripts.smoke import store_principal_deny as smoke

    assert gate.STORE_KEY_ADMIN_ACTIONS == smoke.STORE_KEY_ADMIN_ACTIONS
    assert gate.DYNAMODB_KEY_USE_ACTIONS == smoke.KEY_USE_ACTIONS
    assert gate.S3_OBJECT_READ_ACTIONS == smoke.S3_OBJECT_READ_ACTIONS
    assert gate.S3_OBJECT_WRITE_ACTIONS == smoke.S3_OBJECT_WRITE_ACTIONS
    assert gate.S3_BUCKET_ADMIN_ACTIONS == smoke.S3_BUCKET_ADMIN_ACTIONS
    assert (gate.S3_DATA_PLANE_SID, gate.S3_WRITE_SID, gate.S3_ADMIN_SID) == (
        smoke.S3_DATA_PLANE_SID,
        smoke.S3_WRITE_SID,
        smoke.S3_ADMIN_SID,
    )
    assert gate.STORE_KEY_EXEMPTION_OPERATOR == smoke.KEY_EXEMPTION_OPERATOR
    assert gate.STORE_BUCKET_EXEMPTION_OPERATOR == smoke.BUCKET_EXEMPTION_OPERATOR
    assert "kms:RetireGrant" not in gate.STORE_KEY_ADMIN_ACTIONS
    policies = {p.store: p for p in gate.STORE_POLICIES}
    exempt_buckets = {
        store
        for store, policy in policies.items()
        if any(d.exemption == gate.STORE_BUCKET_EXEMPTION_OPERATOR for d in policy.denies)
    }
    assert exempt_buckets == set(smoke.SERVICE_EXEMPT_BUCKETS) & set(policies)
    for store, policy in policies.items():
        deny_sids = {d.sid for d in policy.denies}
        if policy.root_sid is not None:
            assert smoke.KEY_ROOT_SIDS[store] == policy.root_sid, store
        retained = set(policy.sids) - deny_sids - {policy.root_sid}
        assert smoke.RETAINED_SIDS[store] == retained, store


def _bootstrap_tuple_mismatches(calls: list[Any]) -> list[str]:
    """Where the bootstrap's rendered key, lock-table and bucket Denies differ from the gate's."""
    from tests.unit import test_bootstrap_state_backend as bootstrap

    by_sid: dict[str, list[dict[str, Any]]] = {}
    documents = [
        bootstrap._key_statements(calls),
        bootstrap._lock_table_statements(calls),
        bootstrap._policy(calls, bootstrap.LOG_BUCKET),
        bootstrap._policy(calls, bootstrap.STATE_BUCKET),
    ]
    for statements in documents:
        for statement in statements:
            by_sid.setdefault(statement["Sid"], []).append(statement)
    expected = {
        gate.DYNAMODB_KEY_USE_SID: gate.DYNAMODB_KEY_USE_ACTIONS,
        gate.DYNAMODB_KEY_ADMIN_SID: gate.STORE_KEY_ADMIN_ACTIONS,
        gate.DYNAMODB_TABLE_DATA_PLANE_SID: gate.DYNAMODB_TABLE_DATA_PLANE_ACTIONS,
        gate.DYNAMODB_TABLE_ADMIN_SID: gate.DYNAMODB_TABLE_ADMIN_ACTIONS,
        gate.S3_DATA_PLANE_SID: (*gate.S3_OBJECT_READ_ACTIONS, *gate.S3_OBJECT_WRITE_ACTIONS),
        gate.S3_ADMIN_SID: gate.S3_BUCKET_ADMIN_ACTIONS,
    }
    mismatches: list[str] = []
    for sid, actions in expected.items():
        rendered = by_sid.get(sid, [])
        if not rendered:
            mismatches.append(f"{sid}: not rendered")
        mismatches += [
            f"{sid}: rendered {s['Action']!r}" for s in rendered if tuple(s["Action"]) != actions
        ]
    exemptions = {
        "key": [
            op for op in by_sid[gate.DYNAMODB_KEY_USE_SID][0]["Condition"] if op != "ArnNotEquals"
        ],
        "log bucket": [
            op
            for op in bootstrap._policy(calls, bootstrap.LOG_BUCKET)[-2]["Condition"]
            if op != "ArnNotEquals"
        ],
    }
    if exemptions != {
        "key": [gate.STORE_KEY_EXEMPTION_OPERATOR],
        "log bucket": [gate.STORE_BUCKET_EXEMPTION_OPERATOR],
    }:
        mismatches.append(f"exemption operators {exemptions!r}")
    return mismatches


@pytest.fixture
def _bootstrap_jq() -> None:
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; the bootstrap renders its documents with it")


@pytest.mark.usefixtures("_bootstrap_jq")
class TestBootstrapParity:
    """The literals `infra/bootstrap_state_backend.sh` emits equal the gate's tuples and exemptions."""

    def test_the_rendered_bootstrap_documents_equal_the_gate_constants(
        self, tmp_path: Path
    ) -> None:
        from tests.unit import test_bootstrap_state_backend as bootstrap

        calls, _repo = bootstrap._ok(tmp_path)
        assert _bootstrap_tuple_mismatches(calls) == []

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            pytest.param('"s3:PutMetricsConfiguration"', '"s3:PutMetricsConfigurationX"', id="s3-admin"),
            pytest.param('"kms:UpdateKeyDescription"', '"kms:UpdateKeyDescriptionX"', id="key-admin"),
            pytest.param('"s3:DeleteObjectVersionTagging"', '"s3:DeleteObjectVersionTaggingX"', id="s3-write"),
        ],
    )  # fmt: skip
    def test_a_one_sided_script_edit_is_a_mismatch(
        self, tmp_path: Path, old: str, new: str
    ) -> None:
        from tests.unit import test_bootstrap_state_backend as bootstrap

        script = bootstrap.BOOTSTRAP.read_text(encoding="utf-8")
        assert script.count(old) == 1, old
        calls, _repo = bootstrap._ok(tmp_path, script_text=script.replace(old, new))
        assert _bootstrap_tuple_mismatches(calls)

    def test_a_one_sided_gate_edit_is_a_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.unit import test_bootstrap_state_backend as bootstrap

        calls, _repo = bootstrap._ok(tmp_path)
        monkeypatch.setattr(gate, "S3_BUCKET_ADMIN_ACTIONS", gate.S3_BUCKET_ADMIN_ACTIONS[:-1])
        assert _bootstrap_tuple_mismatches(calls)


class TestStoreKeyPolicyCount:
    """`aws_kms_key_policy` expects 5, and the committed fixture plans 5."""

    def test_the_expected_and_declared_counts_are_five(self) -> None:
        assert gate.EXPECTED_RESOURCE_COUNTS["aws_kms_key_policy"].fixed == 5
        assert gate.DECLARED_INFRA_BLOCK_COUNTS['resource "aws_kms_key_policy"'][0] == 5
        rows = {
            f.resource: f.status
            for f in gate.check_declared_infra_block_counts(gate.Plan(resource_changes=[]))
        }
        assert rows['resource "aws_kms_key_policy"'] == "pass"

    def _count_row(self, plan: Any) -> Any:
        (row,) = [
            f
            for f in gate.check_resource_existence_counts(plan)
            if f.resource == "aws_kms_key_policy"
        ]
        return row

    def test_the_fixture_plans_five_and_the_count_passes(self) -> None:
        planned = sum(
            1
            for r in load_plan("compliant").resource_changes
            if r.get("type") == "aws_kms_key_policy"
        )
        assert planned == 5
        assert "aws_kms_key_policy" not in gate.FIXTURE_LAGGING_COUNTS
        assert self._count_row(load_plan("compliant")).status == "pass"

    def _with_extra(self, document: dict[str, Any], count: int) -> Any:
        policy = next(r for r in document["resource_changes"] if r["type"] == "aws_kms_key_policy")
        extra = [
            copy.deepcopy(policy) | {"address": f"aws_kms_key_policy.planted_{index}"}
            for index in range(count)
        ]
        return _plan_of(document | {"resource_changes": [*document["resource_changes"], *extra]})

    def test_a_plan_with_five_passes_and_four_or_six_fails(self) -> None:
        document = plan_fixtures.build("compliant")
        policy = next(r for r in document["resource_changes"] if r["type"] == "aws_kms_key_policy")
        fewer = [r for r in document["resource_changes"] if r is not policy]
        assert self._count_row(_plan_of(document)).status == "pass"
        assert self._count_row(self._with_extra(document, 1)).status == "fail"
        assert self._count_row(_plan_of(document | {"resource_changes": fewer})).status == "fail"

    def test_a_lagging_declaration_defers_only_the_count_it_declares(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = plan_fixtures.build("compliant")
        policy = next(r for r in document["resource_changes"] if r["type"] == "aws_kms_key_policy")
        fewer = _plan_of(
            document
            | {"resource_changes": [r for r in document["resource_changes"] if r is not policy]}
        )
        monkeypatch.setattr(gate, "FIXTURE_LAGGING_COUNTS", {"aws_kms_key_policy": (4, "lagging")})
        assert self._count_row(fewer).status == "deferred"
        assert self._count_row(load_plan("compliant")).status == "pass"
        assert self._count_row(self._with_extra(document, 1)).status == "fail"

    def test_a_wrong_lagging_count_is_stale_against_the_fixture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = "(declared-lagging) aws_kms_key_policy"
        assert row not in {f.resource for f in gate.check_fixture_currency()}
        monkeypatch.setattr(gate, "FIXTURE_LAGGING_COUNTS", {"aws_kms_key_policy": (3, "stale")})
        assert {f.resource: f.status for f in gate.check_fixture_currency()}[row] == "fail"


class TestMandatoryTagsIncludeAppName:
    """`mandatory_tags` requires `AppName = crm-agent-eval` on every taggable resource."""

    @staticmethod
    def _first_finding(edit: Callable[[dict[str, Any]], object]) -> gate.Finding:
        """The finding for the first taggable resource after `edit` is applied to its `tags_all`."""
        document = plan_fixtures.build("compliant")
        first = next(r for r in document["resource_changes"] if "tags_all" in r["change"]["after"])
        edit(first["change"]["after"]["tags_all"])
        findings = gate.check_mandatory_tags(_plan_of(document))
        (finding,) = [f for f in findings if f.resource == first["address"]]
        return finding

    def test_the_compliant_plan_carries_app_name_everywhere(self) -> None:
        findings = gate.check_mandatory_tags(load_plan("compliant"))
        assert len(findings) > 100, len(findings)
        assert [f for f in findings if f.status != "pass"] == []

    def test_the_gate_names_app_name_as_mandatory(self) -> None:
        assert "AppName" in gate.MANDATORY_TAGS
        assert gate.MANDATORY_TAG_VALUES == {"AppName": "crm-agent-eval"}

    def test_an_absent_app_name_fails(self) -> None:
        first = self._first_finding(lambda tags: tags.pop("AppName"))
        assert first.status == "fail"
        assert "'AppName'" in first.detail

    def test_a_wrong_app_name_value_fails(self) -> None:
        first = self._first_finding(lambda tags: tags.update({"AppName": "other-app"}))
        assert first.status == "fail"
        assert "other-app" in first.detail

    def test_the_unmutated_copy_passes(self) -> None:
        assert self._first_finding(lambda _tags: None).status == "pass"
