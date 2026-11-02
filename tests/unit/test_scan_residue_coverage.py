# SPDX-License-Identifier: MIT
"""Coverage contract: every managed resource type the compliant plan deploys is scanned or exempt.

Each managed `type` in `tests/fixtures/plans/compliant.base.ndjson` must map to a registered
residue family (`TYPE_TO_FAMILY`) or carry a reason in `NOT_SEPARATELY_LISTABLE`. A mapped type's
ARN resource type, derived by `core.arn_resource_type` from a sample ARN in the documented AWS
format, must be in that family's `ENUMERATED_ARN_TYPES`, or carry a reason in
`ARN_TYPE_NOT_ENUMERATED`. Adding an IaC resource type without classifying it reds this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from scripts import scan_residue
from scripts.residue.core import arn_resource_type

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PLAN_BASE: Final = REPO_ROOT / "tests" / "fixtures" / "plans" / "compliant.base.ndjson"
_ACCT: Final = "123456789012"
_R: Final = "us-east-1"

#: Managed plan type -> (residue family, sample ARN in the AWS-documented format for that type).
TYPE_TO_FAMILY: Final[dict[str, tuple[str, str]]] = {
    "aws_api_gateway_rest_api": ("apigateway", f"arn:aws:apigateway:{_R}::/restapis/a1b2c3"),
    "aws_bedrockagentcore_agent_runtime": (
        "agentcore",
        f"arn:aws:bedrock-agentcore:{_R}:{_ACCT}:runtime/crm_agent_eval_runtime-x",
    ),
    "aws_bedrockagentcore_gateway": (
        "agentcore",
        f"arn:aws:bedrock-agentcore:{_R}:{_ACCT}:gateway/crm-agent-eval-gw-x",
    ),
    "aws_cloudfront_distribution": ("cloudfront", f"arn:aws:cloudfront::{_ACCT}:distribution/E1"),
    "aws_cloudfront_function": (
        "cloudfront",
        f"arn:aws:cloudfront::{_ACCT}:function/crm-agent-eval-fn",
    ),
    "aws_cloudfront_origin_access_control": (
        "cloudfront",
        f"arn:aws:cloudfront::{_ACCT}:origin-access-control/E2",
    ),
    "aws_cloudfront_origin_request_policy": (
        "cloudfront",
        f"arn:aws:cloudfront::{_ACCT}:origin-request-policy/p-1",
    ),
    "aws_cloudfront_vpc_origin": ("cloudfront", f"arn:aws:cloudfront::{_ACCT}:vpcorigin/vo-1"),
    "aws_cloudtrail": ("cloudtrail", f"arn:aws:cloudtrail:{_R}:{_ACCT}:trail/crm-agent-eval"),
    "aws_cloudwatch_log_group": (
        "logs",
        f"arn:aws:logs:{_R}:{_ACCT}:log-group:/aws/lambda/crm-agent-eval-x",
    ),
    "aws_codebuild_project": (
        "codebuild",
        f"arn:aws:codebuild:{_R}:{_ACCT}:project/crm-agent-eval-agent-image",
    ),
    "aws_cognito_user_pool": ("cognito", f"arn:aws:cognito-idp:{_R}:{_ACCT}:userpool/{_R}_abc"),
    "aws_dynamodb_table": ("dynamodb", f"arn:aws:dynamodb:{_R}:{_ACCT}:table/crm-agent-evalx"),
    "aws_ecr_repository": ("ecr", f"arn:aws:ecr:{_R}:{_ACCT}:repository/crm-agent-eval-agent"),
    "aws_eip": ("ec2", f"arn:aws:ec2:{_R}:{_ACCT}:elastic-ip/eipalloc-1"),
    "aws_flow_log": ("ec2", f"arn:aws:ec2:{_R}:{_ACCT}:vpc-flow-log/fl-1"),
    "aws_iam_policy": ("iam", f"arn:aws:iam::{_ACCT}:policy/crm-agent-eval-p"),
    "aws_iam_role": ("iam", f"arn:aws:iam::{_ACCT}:role/crm-agent-eval-r"),
    "aws_kms_alias": ("kms", f"arn:aws:kms:{_R}:{_ACCT}:alias/crm-agent-eval-k"),
    "aws_kms_key": ("kms", f"arn:aws:kms:{_R}:{_ACCT}:key/1234abcd-12ab-34cd-56ef-1234567890ab"),
    "aws_lambda_function": ("lambda", f"arn:aws:lambda:{_R}:{_ACCT}:function:crm-agent-eval-f"),
    "aws_lb": (
        "elbv2",
        f"arn:aws:elasticloadbalancing:{_R}:{_ACCT}:loadbalancer/app/crm-agent-eval-a/1",
    ),
    "aws_lb_target_group": (
        "elbv2",
        f"arn:aws:elasticloadbalancing:{_R}:{_ACCT}:targetgroup/crm-agent-eval-t/1",
    ),
    "aws_nat_gateway": ("ec2", f"arn:aws:ec2:{_R}:{_ACCT}:natgateway/nat-1"),
    "aws_s3_bucket": ("s3", "arn:aws:s3:::crm-agent-eval-bucket"),
    "aws_security_group": ("ec2", f"arn:aws:ec2:{_R}:{_ACCT}:security-group/sg-1"),
    "aws_sfn_state_machine": (
        "stepfunctions",
        f"arn:aws:states:{_R}:{_ACCT}:stateMachine:crm-agent-eval-sweep",
    ),
    "aws_ssm_parameter": ("ssm", f"arn:aws:ssm:{_R}:{_ACCT}:parameter/crm-agent-eval/x"),
    "aws_vpc_endpoint": ("ec2", f"arn:aws:ec2:{_R}:{_ACCT}:vpc-endpoint/vpce-1"),
}

_CHILD: Final = "child of a listed resource; deleted with it"

#: Managed plan types with no family of their own, each with the reason.
NOT_SEPARATELY_LISTABLE: Final[dict[str, str]] = {
    "aws_api_gateway_authorizer": f"{_CHILD} (REST API)",
    "aws_api_gateway_deployment": f"{_CHILD} (REST API)",
    "aws_api_gateway_integration": f"{_CHILD} (REST API)",
    "aws_api_gateway_method": f"{_CHILD} (REST API)",
    "aws_api_gateway_method_settings": f"{_CHILD} (REST API stage)",
    "aws_api_gateway_model": f"{_CHILD} (REST API)",
    "aws_api_gateway_request_validator": f"{_CHILD} (REST API)",
    "aws_api_gateway_resource": f"{_CHILD} (REST API)",
    "aws_api_gateway_stage": f"{_CHILD} (REST API)",
    "aws_bedrockagentcore_gateway_target": f"{_CHILD} (AgentCore gateway)",
    "aws_bedrockagentcore_resource_policy": f"{_CHILD} (AgentCore runtime/gateway policy)",
    "aws_cognito_resource_server": f"{_CHILD} (user pool)",
    "aws_cognito_user_group": f"{_CHILD} (user pool)",
    "aws_cognito_user_pool_client": f"{_CHILD} (user pool)",
    "aws_cognito_user_pool_domain": f"{_CHILD} (user pool)",
    "aws_dynamodb_resource_policy": f"{_CHILD} (DynamoDB table)",
    "aws_ecr_lifecycle_policy": f"{_CHILD} (ECR repository)",
    "aws_iam_role_policy": f"{_CHILD} (IAM role, inline policy)",
    "aws_iam_role_policy_attachment": f"{_CHILD} (IAM role attachment)",
    "aws_kms_key_policy": f"{_CHILD} (KMS key)",
    "aws_lambda_function_url": f"{_CHILD} (Lambda function)",
    "aws_lambda_permission": f"{_CHILD} (Lambda function resource policy)",
    "aws_lb_listener": f"{_CHILD} (load balancer)",
    "aws_lb_target_group_attachment": f"{_CHILD} (target group)",
    "aws_route": (
        "entry in an adopted route table, never enumerated; its NAT gateway or endpoint "
        "target is scanned by the ec2 family"
    ),
    "aws_s3_bucket_lifecycle_configuration": f"{_CHILD} (S3 bucket)",
    "aws_s3_bucket_logging": f"{_CHILD} (S3 bucket)",
    "aws_s3_bucket_ownership_controls": f"{_CHILD} (S3 bucket)",
    "aws_s3_bucket_policy": f"{_CHILD} (S3 bucket)",
    "aws_s3_bucket_public_access_block": f"{_CHILD} (S3 bucket)",
    "aws_s3_bucket_server_side_encryption_configuration": f"{_CHILD} (S3 bucket)",
    "aws_s3_bucket_versioning": f"{_CHILD} (S3 bucket)",
    "aws_s3_object": f"{_CHILD} (S3 bucket; force_destroy)",
    "aws_vpc_endpoint_policy": f"{_CHILD} (VPC endpoint)",
    "aws_vpc_security_group_egress_rule": f"{_CHILD} (security group)",
    "aws_vpc_security_group_ingress_rule": f"{_CHILD} (security group)",
    "null_resource": "Terraform-internal; creates no AWS resource",
    "terraform_data": "Terraform-internal; creates no AWS resource",
}

#: Mapped types whose ARN type is not in their family's ENUMERATED_ARN_TYPES, with the reason.
ARN_TYPE_NOT_ENUMERATED: Final[dict[str, str]] = {
    "aws_cloudfront_origin_access_control": (
        "listed by name with arn None, so not matchable by ARN; any tag hit stays "
        "tagged-unscanned residue (fail-safe, never stale)"
    ),
    "aws_cloudfront_origin_request_policy": (
        "listed by name with arn None, so not matchable by ARN; any tag hit stays "
        "tagged-unscanned residue (fail-safe, never stale)"
    ),
}


def managed_plan_types(path: Path = PLAN_BASE) -> set[str]:
    """Every `type` of a `mode == "managed"` resource change in the plan base."""
    types: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        change = json.loads(line)
        if change.get("mode") == "managed":
            types.add(change["type"])
    return types


def unclassified(types: set[str]) -> set[str]:
    """The types in neither `TYPE_TO_FAMILY` nor `NOT_SEPARATELY_LISTABLE`."""
    return types - set(TYPE_TO_FAMILY) - set(NOT_SEPARATELY_LISTABLE)


def test_plan_base_has_managed_types() -> None:
    assert len(managed_plan_types()) > 30


def test_every_managed_plan_type_is_classified() -> None:
    assert unclassified(managed_plan_types()) == set()


def test_an_unclassified_type_is_detected() -> None:
    assert unclassified(managed_plan_types() | {"aws_sqs_queue"}) == {"aws_sqs_queue"}


def test_classification_has_no_stale_or_overlapping_entries() -> None:
    plan = managed_plan_types()
    assert set(TYPE_TO_FAMILY) & set(NOT_SEPARATELY_LISTABLE) == set()
    assert set(TYPE_TO_FAMILY) | set(NOT_SEPARATELY_LISTABLE) <= plan
    assert set(ARN_TYPE_NOT_ENUMERATED) <= set(TYPE_TO_FAMILY)
    assert all(reason.strip() for reason in NOT_SEPARATELY_LISTABLE.values())
    assert all(reason.strip() for reason in ARN_TYPE_NOT_ENUMERATED.values())


def test_every_mapped_family_is_registered() -> None:
    registry = scan_residue.build_registry()
    assert {family for family, _ in TYPE_TO_FAMILY.values()} <= set(registry)


def test_every_mapped_type_has_its_arn_type_enumerated_or_a_reason() -> None:
    enumerated = scan_residue.build_enumerated_arn_types()
    missing = {
        plan_type: (family, arn_resource_type(sample))
        for plan_type, (family, sample) in TYPE_TO_FAMILY.items()
        if plan_type not in ARN_TYPE_NOT_ENUMERATED
        and arn_resource_type(sample) not in enumerated.get(family, frozenset())
    }
    assert missing == {}


def test_arn_type_exemptions_are_still_needed() -> None:
    enumerated = scan_residue.build_enumerated_arn_types()
    unneeded = {
        plan_type
        for plan_type in ARN_TYPE_NOT_ENUMERATED
        if arn_resource_type(TYPE_TO_FAMILY[plan_type][1])
        in enumerated.get(TYPE_TO_FAMILY[plan_type][0], frozenset())
    }
    assert unneeded == set()


def test_every_registered_family_has_enumerated_arn_types() -> None:
    assert set(scan_residue.build_enumerated_arn_types()) == set(scan_residue.build_registry())
