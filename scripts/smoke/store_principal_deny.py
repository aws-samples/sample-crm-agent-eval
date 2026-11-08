# SPDX-License-Identifier: MIT
"""Layer 4 smoke for the principal Deny on the KMS keys, buckets and lock table outside the CRM tables.

Reads the deployed account, never source:

* buckets from the `eval_artifacts_bucket_name`, `cloudtrail_s3_bucket_name` and
  `frontend_origin_bucket_name` outputs, and each bucket's CMK from `get-bucket-encryption`;
* the logs CMK and the probed log group from the first `/aws/lambda/crm-agent-eval-*` log group
  carrying a `kmsKeyId`;
* the state bucket, lock table and state CMK from the gitignored `infra/backend.hcl`, and the
  state-access-log bucket from the state bucket's `get-bucket-logging` (rows `defer` when the file
  is absent);
* the operator ARNs from the `crm_operator_principal_arns` output (shape rows `defer` while it is
  absent);
* allowlist ground truth: every `crm-agent-eval-*` Lambda whose `KMSKeyArn` is the logs CMK (its
  `Role`), and the eval-artefact roles by name through `iam get-role`;
* the DynamoDB CMK from the first `dynamodb_table_names` table, probed for its admin Deny only;
* the Runtime image from `agentcore_runtime_arn`, and the VPC from `vpc_id`.

Rows:

* `<store>_policy_shape` for the five keys, five buckets and the lock table: the Deny statements'
  sids, effects, `"*"` principal, action sets, resources and exact conditions, the allowlists equal
  to the discovered sets, and no statement beyond the designed and the retained ones.
* `state_operator_list_matches_stack`: the bootstrap stores' operator values equal the
  `crm_operator_principal_arns` output.
* `operator_admitted:<store>`: the operator's `GetObject` of an absent key is `NoSuchKey`, and its
  lock-table `GetItem` returns 200.
* `ecr_layer_fetch`: the probe role, outside every allowlist, fetches the Runtime image's config
  blob from ECR over HTTPS, so ECR's own decrypt under the logs CMK is admitted.
* `probe_denied:<store>:<call>`: a run-scoped role `crm-agent-eval-probe-<run_id>`, trusted by the
  operator only and holding an identity Allow for exactly the probed actions and resources, is
  refused with "explicit deny in a resource-based policy". A success, `NoSuchKey`, a 412, a
  `ConditionalCheckFailedException`, `PolicyNotFoundException`, a grant-not-found error or an
  implicit-deny message fails; any other error defers with its message.
* `admin_simulated_denied:<bucket>`: a pair of `iam simulate-principal-policy` calls against the
  live bucket document, each naming `aws:PrincipalArn`: every bucket-administration action is
  `explicitDeny` for the probe role and none is for the operator. This is a simulation of the live
  document, not a call.
* `logs_probe_denied`: the probe's `FilterLogEvents` on the probed log group is refused with an
  explicit resource-based deny (pass); an implicit deny fails; a successful read defers as
  `logs_read_not_key_gated`; any other refusal defers with its message.
* `probe_denied:cursor_key:kms_encrypt_with_context`: the pagination-cursor CMK, read from the
  `CRM_CURSOR_KMS_KEY_ARN` of the deployed `search_opportunities` function. The probe role holds an
  identity Allow for `kms:Encrypt` on it, and its `Encrypt` under the valid `crm-cursor` context is
  refused with an explicit resource-based deny, classified like every other probe row.
* `cursor_key_simulated:<subject>`: simulations of the live cursor key policy, each naming
  `aws:PrincipalArn` and the valid encryption context: `Encrypt`/`Decrypt` are `explicitDeny` for
  the probe role (`probe_denied`) and not for the `search_opportunities` role
  (`allowlisted_role_admitted`) or the operator (`operator_admitted`); `PutKeyPolicy`,
  `ScheduleKeyDeletion` and `DisableKeyRotation` are `explicitDeny` for that role
  (`allowlisted_role_admin_denied`).
* `probe_role_setup` (only on failure) and `probe_role_deleted`: the probe role is deleted in a
  `finally`.
* `cloudtrail_delivery_current` and `cloudtrail_logs_delivery_current`: `get-trail-status` shows an
  S3 and a CloudWatch Logs delivery after the run start minus 15 minutes with no delivery error,
  polled for at most 20 minutes before deferring.
* `flow_log_delivery`: every flow log on the VPC reports `DeliverLogsStatus=SUCCESS`.
* `logs_key_access_denied_events` and `logs_key_decrypt_per_function`: CloudTrail events on the
  logs CMK (the `ResourceName` lookup plus the `EventName=Decrypt` events naming the key,
  deduplicated by `EventId`; either lookup cut defers) since its latest `PutKeyPolicy` hold no `AccessDenied*` for an admitted principal (a
  logs-key function role, an operator or an AWS service), and a successful `Decrypt` by every
  logs-key function's role with none errored; a function not yet seen defers by name. Probe-role
  events and `DryRunOperationException` results are excluded, and denials of other principals are
  counted in the detail, never failed.
* `log_ingestion_current:<writer>`: the API access, Runtime and CodeBuild log groups each hold an
  event newer than that `PutKeyPolicy`, polled for at most 5 minutes before deferring.

No probe call can mutate data even if the boundary were absent: `PutObject` carries an `IfMatch`
no object has on an absent key, `PutItem` carries `attribute_exists(LockID)` on an absent key,
`PutResourcePolicy` carries an `ExpectedRevisionId` no policy can have, `RevokeGrant` names a grant
that does not exist, and KMS plaintext is discarded. Twelve-digit account ids are redacted from
every row.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from scripts.smoke.dynamodb_principal_deny import (
    DEFER_OPERATOR_OUTPUT_ABSENT,
    FUNCTION_PREFIX,
    KEY_ADMIN_SID,
    KEY_USE_SID,
    OPERATOR_ARNS_OUTPUT,
    PRINCIPAL_ARN_KEY,
    PRINCIPAL_IS_SERVICE_KEY,
    PROBE_EXPECTED_REVISION_ID,
    PROBE_INLINE_POLICY_NAME,
    PROBE_ROLE_PREFIX,
    PROBE_ROLE_TAGS,
    TABLE_ADMIN_ACTIONS,
    TABLE_ADMIN_SID,
    TABLE_DATA_PLANE_ACTIONS,
    TABLE_DATA_PLANE_SID,
    TABLE_NAMES_OUTPUT,
    CallerIdentity,
    ExpectedStatement,
    ProbeClientFactory,
    assume_probe_role,
    caller_identity,
    classify_probe,
    probe_role_name,
    probe_trust_policy,
    session_probe_client,
    statement_problems,
)
from scripts.smoke.dynamodb_principal_deny import (
    KEY_ADMIN_ACTIONS as KEY_ADMIN_ACTIONS,
)
from scripts.smoke.dynamodb_principal_deny import (
    KEY_USE_ACTIONS as KEY_USE_ACTIONS,
)
from scripts.smoke.harness import (
    DEFER,
    CheckResult,
    Deadline,
    ExpectedCheckCount,
    SmokeContext,
    Status,
    error_code,
    key_policy_for_simulation,
    open_https,
    run_main,
)
from scripts.smoke.resource_protection import (
    API_ACCESS_LOG_GROUP,
    CODEBUILD_PROJECT_NAME,
    resolve_trail,
)

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "store_principal_deny"

EVAL_ARTIFACTS_BUCKET_OUTPUT: Final[str] = "eval_artifacts_bucket_name"
CLOUDTRAIL_BUCKET_OUTPUT: Final[str] = "cloudtrail_s3_bucket_name"
FRONTEND_ORIGIN_BUCKET_OUTPUT: Final[str] = "frontend_origin_bucket_name"
RUNTIME_ARN_OUTPUT: Final[str] = "agentcore_runtime_arn"
RUNTIME_LOG_GROUP_OUTPUT: Final[str] = "agentcore_runtime_log_group_name"
VPC_ID_OUTPUT: Final[str] = "vpc_id"

BACKEND_CONFIG_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "backend.hcl"
LOG_GROUP_PREFIX: Final[str] = f"/aws/lambda/{FUNCTION_PREFIX}"
EVAL_ARTIFACTS_ROLE_NAMES: Final[tuple[str, ...]] = (
    f"{FUNCTION_PREFIX}eval-trigger-role",
    f"{FUNCTION_PREFIX}sweep-control-role",
    f"{FUNCTION_PREFIX}codebuild-role",
)

TFSTATE_KEY: Final[str] = "tfstate_key"
EVAL_ARTIFACTS_KEY: Final[str] = "eval_artifacts_key"
LOGS_KEY: Final[str] = "logs_key"
CLOUDTRAIL_KEY: Final[str] = "cloudtrail_key"
FRONTEND_KEY: Final[str] = "frontend_key"
STATE_BUCKET: Final[str] = "state_bucket"
STATE_LOG_BUCKET: Final[str] = "state_log_bucket"
EVAL_ARTIFACTS_BUCKET: Final[str] = "eval_artifacts_bucket"
CLOUDTRAIL_BUCKET: Final[str] = "cloudtrail_bucket"
FRONTEND_ORIGIN_BUCKET: Final[str] = "frontend_origin_bucket"
LOCK_TABLE: Final[str] = "lock_table"

KEY_STORES: Final[tuple[str, ...]] = (
    TFSTATE_KEY,
    EVAL_ARTIFACTS_KEY,
    LOGS_KEY,
    CLOUDTRAIL_KEY,
    FRONTEND_KEY,
)
DATA_PLANE_BUCKETS: Final[tuple[str, ...]] = (
    STATE_BUCKET,
    STATE_LOG_BUCKET,
    EVAL_ARTIFACTS_BUCKET,
    CLOUDTRAIL_BUCKET,
)
WRITE_ONLY_BUCKETS: Final[tuple[str, ...]] = (FRONTEND_ORIGIN_BUCKET,)
BUCKET_STORES: Final[tuple[str, ...]] = DATA_PLANE_BUCKETS + WRITE_ONLY_BUCKETS
STORES: Final[tuple[str, ...]] = KEY_STORES + BUCKET_STORES + (LOCK_TABLE,)
STATE_STORES: Final[frozenset[str]] = frozenset(
    {TFSTATE_KEY, STATE_BUCKET, STATE_LOG_BUCKET, LOCK_TABLE}
)
#: The bucket data-plane Denies that carry `BoolIfExists aws:PrincipalIsAWSService = "false"`.
SERVICE_EXEMPT_BUCKETS: Final[frozenset[str]] = frozenset({STATE_LOG_BUCKET, CLOUDTRAIL_BUCKET})
KEY_EXEMPTION_OPERATOR: Final[str] = "Bool"
BUCKET_EXEMPTION_OPERATOR: Final[str] = "BoolIfExists"
#: The Group G key-administration tuple: the DynamoDB CMK's plus alias, tag and description changes.
STORE_KEY_ADMIN_ACTIONS: Final[tuple[str, ...]] = (
    *KEY_ADMIN_ACTIONS,
    "kms:CreateAlias",
    "kms:UpdateAlias",
    "kms:DeleteAlias",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:UpdateKeyDescription",
)
#: The DynamoDB CMK, probed only for its admin Deny; its shape is `dynamodb_principal_deny`'s.
DYNAMODB_KEY: Final[str] = "dynamodb_key"
PROBED_STORES: Final[tuple[str, ...]] = (*STORES, DYNAMODB_KEY)

S3_OBJECT_READ_ACTIONS: Final[tuple[str, ...]] = (
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:GetObjectAttributes",
    "s3:GetObjectVersionAttributes",
    "s3:GetObjectTagging",
    "s3:GetObjectVersionTagging",
    "s3:RestoreObject",
    "s3:ListBucket",
    "s3:ListBucketVersions",
    "s3:ListBucketMultipartUploads",
    "s3:ListMultipartUploadParts",
)
S3_OBJECT_WRITE_ACTIONS: Final[tuple[str, ...]] = (
    "s3:PutObject",
    "s3:PutObjectTagging",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:DeleteObjectTagging",
    "s3:AbortMultipartUpload",
    "s3:PutObjectVersionTagging",
    "s3:DeleteObjectVersionTagging",
)
S3_BUCKET_ADMIN_ACTIONS: Final[tuple[str, ...]] = (
    "s3:DeleteBucket",
    "s3:PutBucketPolicy",
    "s3:DeleteBucketPolicy",
    "s3:PutBucketVersioning",
    "s3:PutEncryptionConfiguration",
    "s3:PutLifecycleConfiguration",
    "s3:PutBucketLogging",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketOwnershipControls",
    "s3:PutBucketAcl",
    "s3:PutBucketTagging",
    "s3:PutReplicationConfiguration",
    "s3:PutInventoryConfiguration",
    "s3:PutBucketNotification",
    "s3:PutBucketObjectLockConfiguration",
    "s3:PutBucketRequestPayment",
    "s3:PutBucketWebsite",
    "s3:PutBucketCORS",
    "s3:PutAccelerateConfiguration",
    "s3:PutIntelligentTieringConfiguration",
    "s3:PutAnalyticsConfiguration",
    "s3:PutMetricsConfiguration",
)
S3_DATA_PLANE_SID: Final[str] = "DenyObjectDataPlaneToNonProjectPrincipals"
S3_WRITE_SID: Final[str] = "DenyObjectWritesToNonOperators"
S3_ADMIN_SID: Final[str] = "DenyBucketAdministrationToNonOperators"

#: Each key's account-root `kms:*` Allow sid, kept from before the Deny statements.
KEY_ROOT_SIDS: Final[Mapping[str, str]] = {
    TFSTATE_KEY: "EnableRootAccountPermissions",
    EVAL_ARTIFACTS_KEY: "Enable IAM User Permissions",
    LOGS_KEY: "EnableIAMUserPermissions",
    CLOUDTRAIL_KEY: "EnableIAMUserPermissions",
    FRONTEND_KEY: "EnableIAMUserPermissions",
}
#: Statements that pre-date the Deny statements and stay in each document unchanged.
RETAINED_SIDS: Final[Mapping[str, frozenset[str]]] = {
    TFSTATE_KEY: frozenset(),
    EVAL_ARTIFACTS_KEY: frozenset(),
    LOGS_KEY: frozenset({"AllowCloudWatchLogsUseOfTheKey"}),
    CLOUDTRAIL_KEY: frozenset(
        {
            "AllowCloudTrailToEncryptLogs",
            "AllowCloudTrailToDescribeKey",
            "AllowPrincipalsInAccountToDecryptLogFiles",
        }
    ),
    FRONTEND_KEY: frozenset({"AllowCloudFrontOACDecrypt"}),
    STATE_BUCKET: frozenset({"DenyInsecureTransport"}),
    STATE_LOG_BUCKET: frozenset({"DenyInsecureTransport", "AllowStateBucketAccessLogDelivery"}),
    EVAL_ARTIFACTS_BUCKET: frozenset({"DenyInsecureTransport", "DenyUnencryptedObjectUploads"}),
    CLOUDTRAIL_BUCKET: frozenset(
        {"DenyInsecureTransport", "AWSCloudTrailAclCheck", "AWSCloudTrailWrite"}
    ),
    FRONTEND_ORIGIN_BUCKET: frozenset({"DenyInsecureTransport", "AllowCloudFrontOACRead"}),
    LOCK_TABLE: frozenset(),
}

PROBE_GENERATE_DATA_KEY: Final[str] = "kms_generate_data_key"
PROBE_DECRYPT: Final[str] = "kms_decrypt"
PROBE_REVOKE_GRANT: Final[str] = "kms_revoke_grant_absent"
PROBE_GET_OBJECT: Final[str] = "s3_get_object_absent"
PROBE_LIST_OBJECTS: Final[str] = "s3_list_objects_absent_prefix"
PROBE_PUT_OBJECT: Final[str] = "s3_put_object_if_match"
PROBE_GET_ITEM: Final[str] = "get_item"
PROBE_PUT_ITEM: Final[str] = "put_item_conditional"
PROBE_PUT_RESOURCE_POLICY: Final[str] = "put_resource_policy_conditional"
KEY_PROBE_CALLS: Final[tuple[str, ...]] = (
    PROBE_GENERATE_DATA_KEY,
    PROBE_DECRYPT,
    PROBE_REVOKE_GRANT,
)
DATA_PLANE_BUCKET_PROBE_CALLS: Final[tuple[str, ...]] = (
    PROBE_GET_OBJECT,
    PROBE_LIST_OBJECTS,
    PROBE_PUT_OBJECT,
)
WRITE_ONLY_BUCKET_PROBE_CALLS: Final[tuple[str, ...]] = (PROBE_PUT_OBJECT,)
LOCK_TABLE_PROBE_CALLS: Final[tuple[str, ...]] = (
    PROBE_GET_ITEM,
    PROBE_PUT_ITEM,
    PROBE_PUT_RESOURCE_POLICY,
)
PROBE_IF_MATCH: Final[str] = '"probe-nonexistent-etag"'
PROBE_GRANT_ID: Final[str] = "0123456789abcdef" * 4
LOCK_TABLE_PARTITION_KEY: Final[str] = "LockID"

#: Error codes that mean the call reached the data, object or grant, so no boundary refused it.
REACHED_RESOURCE_CODES: Final[frozenset[str]] = frozenset(
    {
        "NoSuchKey",
        "PreconditionFailed",
        "ConditionalCheckFailedException",
        "PolicyNotFoundException",
        "NotFoundException",
        "InvalidGrantIdException",
    }
)

CHECK_STATE_OPERATORS: Final[str] = "state_operator_list_matches_stack"
CHECK_LOGS_PROBE: Final[str] = "logs_probe_denied"
CHECK_ECR_LAYER_FETCH: Final[str] = "ecr_layer_fetch"
CHECK_CLOUDTRAIL_DELIVERY: Final[str] = "cloudtrail_delivery_current"
CHECK_PROBE_ROLE_DELETED: Final[str] = "probe_role_deleted"
CHECK_PROBE_ROLE_SETUP: Final[str] = "probe_role_setup"
CHECK_CLOUDTRAIL_LOGS_DELIVERY: Final[str] = "cloudtrail_logs_delivery_current"
CHECK_FLOW_LOG_DELIVERY: Final[str] = "flow_log_delivery"
CHECK_LOG_INGESTION: Final[str] = "log_ingestion_current"
CHECK_LOGS_KEY_ERRORS: Final[str] = "logs_key_access_denied_events"
CHECK_LOGS_KEY_DECRYPT: Final[str] = "logs_key_decrypt_per_function"

#: Non-Lambda log writers on the logs CMK, checked for a new event since the logs key changed.
API_ACCESS_LOG: Final[str] = "api_access_log"
RUNTIME_LOG: Final[str] = "runtime_log"
CODEBUILD_LOG: Final[str] = "codebuild_log"
INGESTION_LOGS: Final[tuple[str, ...]] = (API_ACCESS_LOG, RUNTIME_LOG, CODEBUILD_LOG)

DELIVERY_FRESHNESS: Final[timedelta] = timedelta(minutes=15)
DELIVERY_POLL_SECONDS: Final[float] = 20 * 60.0
DELIVERY_POLL_INTERVAL_SECONDS: Final[float] = 60.0
INGESTION_POLL_SECONDS: Final[float] = 5 * 60.0
INGESTION_POLL_INTERVAL_SECONDS: Final[float] = 30.0
KEY_CHANGE_LOOKBACK: Final[timedelta] = timedelta(days=90)
#: Upper bound on CloudTrail `lookup-events` pages read for the logs CMK (50 events a page).
LOOKUP_MAX_PAGES: Final[int] = 100
LOOKUP_PAGE_PAUSE_SECONDS: Final[float] = 0.5
LAYER_FETCH_TIMEOUT_SECONDS: Final[float] = 20.0
ASSUME_TIMEOUT_SECONDS: Final[float] = 60.0

CURSOR_KEY: Final[str] = "cursor_key"
CURSOR_KEY_FUNCTION: Final[str] = f"{FUNCTION_PREFIX}search_opportunities"
CURSOR_KEY_ENV_VAR: Final[str] = "CRM_CURSOR_KMS_KEY_ARN"
CURSOR_KEY_PURPOSE: Final[str] = "crm-cursor"
CURSOR_KEY_CONTEXT_KEYS: Final[tuple[str, ...]] = ("purpose", "tenant", "rep_id", "operation")
CURSOR_KEY_USE_ACTIONS: Final[tuple[str, ...]] = ("kms:Encrypt", "kms:Decrypt")
CURSOR_KEY_ADMIN_ACTIONS: Final[tuple[str, ...]] = (
    "kms:PutKeyPolicy",
    "kms:ScheduleKeyDeletion",
    "kms:DisableKeyRotation",
)
PROBE_CURSOR_ENCRYPT: Final[str] = "kms_encrypt_with_context"
CHECK_CURSOR_PROBE: Final[str] = f"probe_denied:{CURSOR_KEY}:{PROBE_CURSOR_ENCRYPT}"
CHECK_CURSOR_SIM_PROBE: Final[str] = "cursor_key_simulated:probe_denied"
CHECK_CURSOR_SIM_ROLE: Final[str] = "cursor_key_simulated:allowlisted_role_admitted"
CHECK_CURSOR_SIM_OPERATOR: Final[str] = "cursor_key_simulated:operator_admitted"
CHECK_CURSOR_SIM_ADMIN: Final[str] = "cursor_key_simulated:allowlisted_role_admin_denied"
CURSOR_KEY_CHECKS: Final[tuple[str, ...]] = (
    CHECK_CURSOR_PROBE,
    CHECK_CURSOR_SIM_PROBE,
    CHECK_CURSOR_SIM_ROLE,
    CHECK_CURSOR_SIM_OPERATOR,
    CHECK_CURSOR_SIM_ADMIN,
)

DEFER_BACKEND_CONFIG_ABSENT: Final[str] = "backend_config_absent"
DEFER_STACK_OUTPUT_ABSENT: Final[str] = "stack_output_absent"
DEFER_DELIVERY_NOT_CURRENT: Final[str] = "cloudtrail_delivery_not_current"
DEFER_NO_PROBE_TARGET: Final[str] = "no_probe_target"
DEFER_TRAIL_NO_CLOUDWATCH: Final[str] = "trail_has_no_cloudwatch_logs_delivery"
DEFER_NO_FLOW_LOG: Final[str] = "vpc_has_no_flow_log"
DEFER_NO_INGESTION: Final[str] = "no_log_event_since_logs_key_change"
DEFER_KEY_CHANGE_UNOBSERVED: Final[str] = "logs_key_policy_change_unobserved"
DEFER_FUNCTIONS_NOT_INVOKED: Final[str] = "functions_not_invoked_since_logs_key_change"
DEFER_LOOKUP_TRUNCATED: Final[str] = "cloudtrail_lookup_truncated"
DEFER_LOGS_READ_NOT_KEY_GATED: Final[str] = "logs_read_not_key_gated"
DRY_RUN_ERROR_CODE: Final[str] = "DryRunOperationException"

_ACCOUNT_ID: Final[re.Pattern[str]] = re.compile(r"(?<!\d)\d{12}(?!\d)")
_HCL_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(r'^\s*([A-Za-z_]+)\s*=\s*"([^"]*)"\s*$')

#: GETs an HTTPS URL, discards the body, and returns the HTTP status.
LayerFetcher = Callable[[str], int]


@dataclass(frozen=True)
class Store:
    """One discovered store: its kind, ARN, the name its API takes, and a bucket's encryption."""

    name: str
    kind: str
    arn: str
    physical: str
    sse_algorithm: str = ""
    sse_key_arn: str = ""


@dataclass(frozen=True)
class Unavailable:
    """Why a store or allowlist could not be discovered: a `fail` or a `defer` with its reason."""

    status: Status
    detail: str
    defer_reason: str = ""

    def row(self, resource: str, check: str) -> CheckResult:
        """This reason as the row for `check`."""
        return CheckResult(
            resource=resource,
            check=check,
            status=self.status,
            detail=self.detail,
            defer_reason=self.defer_reason if self.status == DEFER else "",
        )


Discovered = Store | Unavailable


@dataclass(frozen=True)
class Allowlists:
    """The principal sets the Deny statements must name, discovered from the account."""

    operators: frozenset[str] | Unavailable
    root: str
    logs_key_roles: frozenset[str] | Unavailable
    eval_artifacts_roles: frozenset[str] | Unavailable


@dataclass(frozen=True)
class CursorKey:
    """The pagination-cursor CMK, one role its policy admits, and its live key policy."""

    store: Store
    allowlisted_role_arn: str
    policy_text: str | None


def discover_cursor_key(ctx: SmokeContext) -> CursorKey | Unavailable:
    """The cursor CMK named by `search_opportunities`' environment, with that function's role."""
    try:
        config = ctx.client("lambda").get_function_configuration(FunctionName=CURSOR_KEY_FUNCTION)
    except Exception as exc:  # noqa: BLE001 - every cursor-key row fails naming the cause
        return Unavailable(
            "fail", _raised(f"get-function-configuration {CURSOR_KEY_FUNCTION}", exc)
        )
    variables = (config.get("Environment") or {}).get("Variables") or {}
    key_arn = str(variables.get(CURSOR_KEY_ENV_VAR) or "")
    role_arn = str(config.get("Role") or "")
    if not key_arn.startswith("arn:") or not role_arn:
        return Unavailable(
            "fail",
            f"{CURSOR_KEY_FUNCTION} carries no {CURSOR_KEY_ENV_VAR} key ARN or no role "
            f"({CURSOR_KEY_ENV_VAR}={key_arn!r})",
        )
    try:
        policy_text: str | None = str(
            ctx.client("kms").get_key_policy(KeyId=key_arn, PolicyName="default")["Policy"]
        )
    except Exception:  # noqa: BLE001 - the simulation rows report the unreadable policy
        policy_text = None
    return CursorKey(
        store=Store(name=CURSOR_KEY, kind="key", arn=key_arn, physical=key_arn),
        allowlisted_role_arn=role_arn,
        policy_text=policy_text,
    )


def cursor_key_context(principal_arn: str) -> list[dict[str, Any]]:
    """Simulation context: the principal, a non-service caller, and the valid cursor context."""
    return [
        {
            "ContextKeyName": PRINCIPAL_ARN_KEY,
            "ContextKeyValues": [principal_arn],
            "ContextKeyType": "string",
        },
        {
            "ContextKeyName": PRINCIPAL_IS_SERVICE_KEY,
            "ContextKeyValues": ["false"],
            "ContextKeyType": "boolean",
        },
        {
            "ContextKeyName": "kms:EncryptionContext:purpose",
            "ContextKeyValues": [CURSOR_KEY_PURPOSE],
            "ContextKeyType": "string",
        },
        {
            "ContextKeyName": "kms:EncryptionContextKeys",
            "ContextKeyValues": list(CURSOR_KEY_CONTEXT_KEYS),
            "ContextKeyType": "stringList",
        },
    ]


def _simulate_cursor_key(
    ctx: SmokeContext,
    principal_arn: str,
    cursor: CursorKey,
    actions: Sequence[str],
    root_arn: str,
) -> dict[str, str]:
    if cursor.policy_text is None:
        raise ValueError("the cursor key policy could not be read")
    response = ctx.client("iam").simulate_principal_policy(
        PolicySourceArn=principal_arn,
        ActionNames=list(actions),
        ResourceArns=[cursor.store.arn],
        ResourcePolicy=key_policy_for_simulation(cursor.policy_text, cursor.store.arn),
        ResourceOwner=root_arn,
        ContextEntries=cursor_key_context(principal_arn),
    )
    if response.get("IsTruncated"):
        raise ValueError("the simulation result is truncated")
    return {
        str(r.get("EvalActionName")): str(r.get("EvalDecision"))
        for r in response.get("EvaluationResults", [])
    }


def cursor_key_simulation_rows(
    ctx: SmokeContext, cursor: CursorKey, probe_arn: str, operator_arn: str, root_arn: str
) -> Iterator[CheckResult]:
    """Four simulations of the live cursor key policy under the valid encryption context."""
    cases: tuple[tuple[str, str, Sequence[str], bool], ...] = (
        (CHECK_CURSOR_SIM_PROBE, probe_arn, CURSOR_KEY_USE_ACTIONS, True),
        (CHECK_CURSOR_SIM_ROLE, cursor.allowlisted_role_arn, CURSOR_KEY_USE_ACTIONS, False),
        (CHECK_CURSOR_SIM_OPERATOR, operator_arn, CURSOR_KEY_USE_ACTIONS, False),
        (CHECK_CURSOR_SIM_ADMIN, cursor.allowlisted_role_arn, CURSOR_KEY_ADMIN_ACTIONS, True),
    )
    for check, principal, actions, expect_deny in cases:
        try:
            decisions = _simulate_cursor_key(ctx, principal, cursor, actions, root_arn)
        except Exception as exc:  # noqa: BLE001 - reported as a failing row
            yield _fail(cursor.store.physical, check, _raised("simulate-principal-policy", exc))
            continue
        problems = [
            f"{action}={decisions.get(action, 'not evaluated')}"
            for action in actions
            if (decisions.get(action) == "explicitDeny") != expect_deny
        ]
        wanted = "explicitDeny" if expect_deny else "not explicitDeny"
        yield CheckResult(
            resource=cursor.store.physical,
            check=check,
            status="fail" if problems else "pass",
            detail=(
                "simulation of the live key policy, not a call: "
                + (
                    f"expected {wanted}: " + "; ".join(problems)
                    if problems
                    else f"{', '.join(actions)} {wanted}"
                )
            ),
        )


def cursor_probe_request(cursor: CursorKey, run_id: str) -> tuple[str, str, dict[str, Any]]:
    """The probe's `Encrypt` under the valid cursor context; the ciphertext is discarded."""
    return (
        "kms",
        "encrypt",
        {
            "KeyId": cursor.store.arn,
            "Plaintext": b"probe",
            "EncryptionContext": {
                "purpose": CURSOR_KEY_PURPOSE,
                "tenant": f"probe-{run_id}",
                "rep_id": f"probe-{run_id}",
                "operation": "search_opportunities",
            },
        },
    )


def redact(text: str) -> str:
    """Replace every twelve-digit account id in `text`."""
    return _ACCOUNT_ID.sub("<account>", text)


def _redacted(row: CheckResult) -> CheckResult:
    return dataclasses.replace(row, resource=redact(row.resource), detail=redact(row.detail))


def _fail(resource: str, check: str, detail: str) -> CheckResult:
    return CheckResult(resource=resource, check=check, status="fail", detail=detail)


def _raised(action: str, exc: BaseException) -> str:
    return f"{action} raised {error_code(exc) or type(exc).__name__}: {exc}"


def read_backend_config(path: Path) -> dict[str, str] | None:
    """The quoted assignments of a `backend.hcl`, or `None` when the file is absent."""
    if not path.is_file():
        return None
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _HCL_ASSIGNMENT.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _deny(
    sid: str,
    actions: tuple[str, ...],
    resources: frozenset[str],
    allowed: frozenset[str],
    *,
    exemption: str | None = None,
) -> ExpectedStatement:
    conditions: dict[str, Mapping[str, frozenset[str]]] = {
        "ArnNotEquals": {PRINCIPAL_ARN_KEY: allowed}
    }
    if exemption is not None:
        conditions[exemption] = {PRINCIPAL_IS_SERVICE_KEY: frozenset({"false"})}
    return ExpectedStatement(
        sid=sid,
        effect="Deny",
        principal="*",
        actions=actions,
        resources=resources,
        conditions=conditions,
    )


def expected_key_statements(
    store: str, root_arn: str, users: frozenset[str], administrators: frozenset[str]
) -> tuple[ExpectedStatement, ...]:
    """The root Allow, the key-use Deny (with the service exemption) and the admin Deny."""
    everything = frozenset({"*"})
    return (
        ExpectedStatement(
            sid=KEY_ROOT_SIDS[store],
            effect="Allow",
            principal=root_arn,
            actions=("kms:*",),
            resources=everything,
            conditions={},
        ),
        _deny(
            KEY_USE_SID,
            KEY_USE_ACTIONS,
            everything,
            users,
            exemption=KEY_EXEMPTION_OPERATOR,
        ),
        _deny(KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS, everything, administrators),
    )


def expected_bucket_statements(
    store: str, bucket_arn: str, data_plane: frozenset[str], administrators: frozenset[str]
) -> tuple[ExpectedStatement, ...]:
    """A bucket's object Deny (data plane, or writes only for the origin) and its admin Deny."""
    objects = frozenset({bucket_arn, f"{bucket_arn}/*"})
    admin = _deny(S3_ADMIN_SID, S3_BUCKET_ADMIN_ACTIONS, frozenset({bucket_arn}), administrators)
    if store in WRITE_ONLY_BUCKETS:
        return (_deny(S3_WRITE_SID, S3_OBJECT_WRITE_ACTIONS, objects, data_plane), admin)
    return (
        _deny(
            S3_DATA_PLANE_SID,
            S3_OBJECT_READ_ACTIONS + S3_OBJECT_WRITE_ACTIONS,
            objects,
            data_plane,
            exemption=BUCKET_EXEMPTION_OPERATOR if store in SERVICE_EXEMPT_BUCKETS else None,
        ),
        admin,
    )


def expected_lock_table_statements(
    table_arn: str, data_plane: frozenset[str], administrators: frozenset[str]
) -> tuple[ExpectedStatement, ...]:
    """The lock table's data-plane and admin Denies, with the DynamoDB table tuples."""
    resources = frozenset({table_arn, f"{table_arn}/index/*"})
    return (
        _deny(TABLE_DATA_PLANE_SID, TABLE_DATA_PLANE_ACTIONS, resources, data_plane),
        _deny(TABLE_ADMIN_SID, TABLE_ADMIN_ACTIONS, resources, administrators),
    )


def _statements(document_text: str) -> list[Mapping[str, Any]] | str:
    try:
        document = json.loads(document_text)
    except json.JSONDecodeError as exc:
        return f"policy is not JSON: {exc}"
    if not isinstance(document, Mapping):
        return "policy is not a JSON object"
    return [s for s in _as_list(document.get("Statement")) if isinstance(s, Mapping)]


def store_document_problems(
    document_text: str, expected: Sequence[ExpectedStatement], retained: frozenset[str]
) -> list[str]:
    """Every way a document differs from `expected` plus, at most, the `retained` statements."""
    statements = _statements(document_text)
    if isinstance(statements, str):
        return [statements]
    by_sid: dict[str, list[Mapping[str, Any]]] = {}
    for statement in statements:
        by_sid.setdefault(str(statement.get("Sid", "")), []).append(statement)
    expected_sids = {e.sid for e in expected}
    problems: list[str] = []
    duplicates = sorted(sid for sid, found in by_sid.items() if len(found) > 1)
    if duplicates:
        problems.append(f"duplicate statement sid(s): {duplicates}")
    unexpected = sorted(set(by_sid) - expected_sids - retained)
    if unexpected:
        problems.append(f"unexpected statement sid(s): {unexpected}")
    for wanted in expected:
        found = by_sid.get(wanted.sid)
        if not found:
            problems.append(f"{wanted.sid}: absent")
            continue
        problems.extend(f"{wanted.sid}: {p}" for p in statement_problems(found[0], wanted))
    return problems


def denied_principal_values(document_text: str, sid: str) -> frozenset[str] | None:
    """The `ArnNotEquals aws:PrincipalArn` values of statement `sid`, or `None` if it is absent."""
    statements = _statements(document_text)
    if isinstance(statements, str):
        return None
    for statement in statements:
        if statement.get("Sid") != sid:
            continue
        condition = statement.get("Condition")
        arn_not_equals = condition.get("ArnNotEquals") if isinstance(condition, Mapping) else None
        if not isinstance(arn_not_equals, Mapping) or PRINCIPAL_ARN_KEY not in arn_not_equals:
            return None
        return frozenset(str(v) for v in _as_list(arn_not_equals[PRINCIPAL_ARN_KEY]))
    return None


def _principal_sid(store: str) -> str:
    """The sid of the store's statement whose allowlist is the data-plane (or key-use) set."""
    if store in KEY_STORES:
        return KEY_USE_SID
    if store == LOCK_TABLE:
        return TABLE_DATA_PLANE_SID
    return S3_WRITE_SID if store in WRITE_ONLY_BUCKETS else S3_DATA_PLANE_SID


# ---------------------------------------------------------------------------
# Discovery.
# ---------------------------------------------------------------------------


def _output_absent(name: str) -> Unavailable:
    return Unavailable(DEFER, f"the {name} output is absent", DEFER_STACK_OUTPUT_ABSENT)


def _key_store(ctx: SmokeContext, store: str, key_id: str) -> Discovered:
    try:
        metadata = ctx.client("kms").describe_key(KeyId=key_id)["KeyMetadata"]
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return Unavailable("fail", _raised(f"kms describe-key of {key_id}", exc))
    arn = str(metadata["Arn"])
    return Store(name=store, kind="key", arn=arn, physical=arn)


def _bucket_store(ctx: SmokeContext, store: str, bucket: str) -> Discovered:
    try:
        rules = ctx.client("s3").get_bucket_encryption(Bucket=bucket)[
            "ServerSideEncryptionConfiguration"
        ]["Rules"]
        default = rules[0]["ApplyServerSideEncryptionByDefault"]
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return Unavailable("fail", _raised(f"get-bucket-encryption of {bucket}", exc))
    return Store(
        name=store,
        kind="bucket",
        arn=f"arn:aws:s3:::{bucket}",
        physical=bucket,
        sse_algorithm=str(default.get("SSEAlgorithm", "")),
        sse_key_arn=str(default.get("KMSMasterKeyID", "")),
    )


def _key_of_bucket(ctx: SmokeContext, store: str, bucket: Discovered) -> Discovered:
    if isinstance(bucket, Unavailable):
        return bucket
    if bucket.sse_algorithm != "aws:kms" or not bucket.sse_key_arn:
        return Unavailable(
            "fail",
            f"{bucket.physical}'s default encryption is {bucket.sse_algorithm!r}, not an aws:kms key",
        )
    return _key_store(ctx, store, bucket.sse_key_arn)


def _output_bucket(ctx: SmokeContext, store: str, output: str) -> Discovered:
    name = ctx.outputs.get(output)
    if not isinstance(name, str) or not name:
        return _output_absent(output)
    return _bucket_store(ctx, store, name)


def _state_stores(ctx: SmokeContext, backend: Mapping[str, str] | None) -> dict[str, Discovered]:
    if backend is None:
        absent = Unavailable(
            DEFER,
            "infra/backend.hcl is absent, so the state stores are unknown",
            DEFER_BACKEND_CONFIG_ABSENT,
        )
        return dict.fromkeys(sorted(STATE_STORES), absent)
    stores: dict[str, Discovered] = {}
    missing = sorted({"bucket", "dynamodb_table", "kms_key_id"} - set(backend))
    if missing:
        broken = Unavailable("fail", f"infra/backend.hcl does not set {missing}")
        return dict.fromkeys(sorted(STATE_STORES), broken)
    stores[TFSTATE_KEY] = _key_store(ctx, TFSTATE_KEY, backend["kms_key_id"])
    state_bucket = _bucket_store(ctx, STATE_BUCKET, backend["bucket"])
    stores[STATE_BUCKET] = state_bucket
    stores[STATE_LOG_BUCKET] = _state_log_bucket(ctx, state_bucket)
    table = backend["dynamodb_table"]
    try:
        arn = str(ctx.client("dynamodb").describe_table(TableName=table)["Table"]["TableArn"])
        stores[LOCK_TABLE] = Store(name=LOCK_TABLE, kind="table", arn=arn, physical=table)
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        stores[LOCK_TABLE] = Unavailable("fail", _raised(f"describe-table of {table}", exc))
    return stores


def _state_log_bucket(ctx: SmokeContext, state_bucket: Discovered) -> Discovered:
    if isinstance(state_bucket, Unavailable):
        return state_bucket
    try:
        logging = ctx.client("s3").get_bucket_logging(Bucket=state_bucket.physical)
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return Unavailable("fail", _raised(f"get-bucket-logging of {state_bucket.physical}", exc))
    target = (logging.get("LoggingEnabled") or {}).get("TargetBucket")
    if not target:
        return Unavailable("fail", f"{state_bucket.physical} has no access-log target bucket")
    return _bucket_store(ctx, STATE_LOG_BUCKET, str(target))


def discover_logs_key(ctx: SmokeContext) -> tuple[Discovered, Mapping[str, Any] | None]:
    """The logs CMK and the first project log group encrypted with it."""
    try:
        for page in (
            ctx.client("logs")
            .get_paginator("describe_log_groups")
            .paginate(logGroupNamePrefix=LOG_GROUP_PREFIX)
        ):
            for group in page.get("logGroups", []):
                key = group.get("kmsKeyId")
                if key:
                    return Store(name=LOGS_KEY, kind="key", arn=str(key), physical=str(key)), group
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return Unavailable("fail", _raised("describe-log-groups", exc)), None
    return Unavailable("fail", f"no {LOG_GROUP_PREFIX}* log group carries a kmsKeyId"), None


def discover_stores(
    ctx: SmokeContext, backend: Mapping[str, str] | None
) -> tuple[dict[str, Discovered], Mapping[str, Any] | None]:
    """Every store, discovered from the outputs, `backend.hcl` and the account."""
    stores = _state_stores(ctx, backend)
    for store, output in (
        (EVAL_ARTIFACTS_BUCKET, EVAL_ARTIFACTS_BUCKET_OUTPUT),
        (CLOUDTRAIL_BUCKET, CLOUDTRAIL_BUCKET_OUTPUT),
        (FRONTEND_ORIGIN_BUCKET, FRONTEND_ORIGIN_BUCKET_OUTPUT),
    ):
        stores[store] = _output_bucket(ctx, store, output)
    stores[EVAL_ARTIFACTS_KEY] = _key_of_bucket(
        ctx, EVAL_ARTIFACTS_KEY, stores[EVAL_ARTIFACTS_BUCKET]
    )
    stores[CLOUDTRAIL_KEY] = _key_of_bucket(ctx, CLOUDTRAIL_KEY, stores[CLOUDTRAIL_BUCKET])
    stores[FRONTEND_KEY] = _key_of_bucket(ctx, FRONTEND_KEY, stores[FRONTEND_ORIGIN_BUCKET])
    stores[LOGS_KEY], log_group = discover_logs_key(ctx)
    stores[DYNAMODB_KEY] = discover_dynamodb_key(ctx)
    return stores, log_group


def discover_logs_key_functions(
    ctx: SmokeContext, logs_key: Discovered
) -> dict[str, str] | Unavailable:
    """`{function name: execution role ARN}` for every project Lambda whose environment uses the logs CMK."""
    if isinstance(logs_key, Unavailable):
        return logs_key
    functions: dict[str, str] = {}
    try:
        for page in ctx.client("lambda").get_paginator("list_functions").paginate():
            for function in page.get("Functions", []):
                name = str(function.get("FunctionName", ""))
                if name.startswith(FUNCTION_PREFIX) and function.get("KMSKeyArn") == logs_key.arn:
                    functions[name] = str(function["Role"])
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return Unavailable("fail", _raised("list-functions", exc))
    if not functions:
        return Unavailable("fail", "no project Lambda's KMSKeyArn is the logs CMK")
    return functions


def discover_dynamodb_key(ctx: SmokeContext) -> Discovered:
    """The DynamoDB CMK, from the first deployed CRM table's `SSEDescription`."""
    tables = ctx.outputs.get(TABLE_NAMES_OUTPUT)
    if not isinstance(tables, Mapping) or not tables:
        return _output_absent(TABLE_NAMES_OUTPUT)
    table = str(tables[sorted(tables)[0]])
    try:
        sse = ctx.client("dynamodb").describe_table(TableName=table)["Table"].get("SSEDescription")
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return Unavailable("fail", _raised(f"describe-table of {table}", exc))
    arn = (sse or {}).get("KMSMasterKeyArn")
    if not arn:
        return Unavailable("fail", f"{table} names no KMSMasterKeyArn")
    return Store(name=DYNAMODB_KEY, kind="key", arn=str(arn), physical=str(arn))


def discover_eval_artifacts_roles(ctx: SmokeContext) -> frozenset[str] | Unavailable:
    """The eval-artefact principals' role ARNs, by name through `iam get-role`."""
    roles: set[str] = set()
    for name in EVAL_ARTIFACTS_ROLE_NAMES:
        try:
            roles.add(str(ctx.client("iam").get_role(RoleName=name)["Role"]["Arn"]))
        except Exception as exc:  # noqa: BLE001 - reported as a failing row
            return Unavailable("fail", _raised(f"iam get-role {name}", exc))
    return frozenset(roles)


def _operators(ctx: SmokeContext) -> frozenset[str] | Unavailable:
    raw = ctx.outputs.get(OPERATOR_ARNS_OUTPUT)
    if raw is None:
        return Unavailable(
            DEFER,
            f"the {OPERATOR_ARNS_OUTPUT} output is absent, so the allowlists are unknown",
            DEFER_OPERATOR_OUTPUT_ABSENT,
        )
    if not isinstance(raw, list) or not raw or not all(isinstance(v, str) and v for v in raw):
        return Unavailable("fail", f"the {OPERATOR_ARNS_OUTPUT} output is malformed: {raw!r}")
    return frozenset(raw)


def expected_statements(
    store: Store, allow: Allowlists
) -> tuple[ExpectedStatement, ...] | Unavailable:
    """The statements `store`'s document must carry, or why its allowlists are unknown."""
    if isinstance(allow.operators, Unavailable):
        return allow.operators
    operators = allow.operators
    administrators = operators | {allow.root}
    users: frozenset[str] | Unavailable = operators
    if store.name in (EVAL_ARTIFACTS_KEY, EVAL_ARTIFACTS_BUCKET):
        users = (
            allow.eval_artifacts_roles
            if isinstance(allow.eval_artifacts_roles, Unavailable)
            else allow.eval_artifacts_roles | operators
        )
    elif store.name == LOGS_KEY:
        users = (
            allow.logs_key_roles
            if isinstance(allow.logs_key_roles, Unavailable)
            else allow.logs_key_roles | operators
        )
    if isinstance(users, Unavailable):
        return users
    if store.kind == "key":
        return expected_key_statements(store.name, allow.root, users, administrators)
    if store.kind == "table":
        return expected_lock_table_statements(store.arn, users, administrators)
    return expected_bucket_statements(store.name, store.arn, users, administrators)


# ---------------------------------------------------------------------------
# Operator rows.
# ---------------------------------------------------------------------------


def fetch_policy(ctx: SmokeContext, store: Store) -> str:
    """The live policy document of `store`."""
    if store.kind == "key":
        return str(
            ctx.client("kms").get_key_policy(KeyId=store.arn, PolicyName="default")["Policy"]
        )
    if store.kind == "table":
        return str(ctx.client("dynamodb").get_resource_policy(ResourceArn=store.arn)["Policy"])
    return str(ctx.client("s3").get_bucket_policy(Bucket=store.physical)["Policy"])


def _shape_row(
    ctx: SmokeContext, name: str, store: Discovered, allow: Allowlists
) -> tuple[CheckResult, str | None]:
    check = f"{name}_policy_shape"
    if isinstance(store, Unavailable):
        return store.row(name, check), None
    text: str | None = None
    fetch_error = ""
    try:
        text = fetch_policy(ctx, store)
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        fetch_error = _raised("reading the policy", exc)
    expected = expected_statements(store, allow)
    if isinstance(expected, Unavailable):
        return expected.row(store.physical, check), text
    if text is None:
        return _fail(store.physical, check, fetch_error), None
    problems = store_document_problems(text, expected, RETAINED_SIDS[name])
    return (
        CheckResult(
            resource=store.physical,
            check=check,
            status="fail" if problems else "pass",
            detail="; ".join(problems)
            if problems
            else f"{len(expected)} Deny/root statements as designed",
        ),
        text,
    )


def state_operator_row(
    stores: Mapping[str, Discovered],
    policies: Mapping[str, str | None],
    operators: frozenset[str] | Unavailable,
) -> CheckResult:
    """The bootstrap stores' operator allowlists against the `crm_operator_principal_arns` output."""
    resource = "state_backend"
    if isinstance(operators, Unavailable):
        return operators.row(resource, CHECK_STATE_OPERATORS)
    problems: list[str] = []
    for name in sorted(STATE_STORES):
        store = stores[name]
        if isinstance(store, Unavailable):
            return store.row(resource, CHECK_STATE_OPERATORS)
        text = policies.get(name)
        values = denied_principal_values(text, _principal_sid(name)) if text else None
        if values is None:
            problems.append(f"{name}: no {_principal_sid(name)} allowlist to compare")
        elif values != operators:
            problems.append(
                f"{name}: missing {sorted(operators - values)}, extra {sorted(values - operators)}"
            )
    return CheckResult(
        resource=resource,
        check=CHECK_STATE_OPERATORS,
        status="fail" if problems else "pass",
        detail="; ".join(problems)
        if problems
        else f"all four state stores name exactly the {len(operators)} stack operator(s)",
    )


def operator_admitted_row(
    ctx: SmokeContext, name: str, store: Discovered, run_id: str
) -> CheckResult:
    """The operator still reaches the store: `NoSuchKey` for a bucket, 200 for the lock table."""
    check = f"operator_admitted:{name}"
    if isinstance(store, Unavailable):
        return store.row(name, check)
    if store.kind == "table":
        try:
            ctx.client("dynamodb").get_item(TableName=store.physical, Key=_lock_key(run_id))
        except Exception as exc:  # noqa: BLE001 - reported as a failing row
            return _fail(store.physical, check, _raised("the operator's GetItem", exc))
        return CheckResult(
            resource=store.physical, check=check, status="pass", detail="GetItem returned 200"
        )
    try:
        ctx.client("s3").get_object(Bucket=store.physical, Key=_probe_object_key(run_id))
    except Exception as exc:  # noqa: BLE001 - classified below
        if error_code(exc) == "NoSuchKey":
            return CheckResult(
                resource=store.physical,
                check=check,
                status="pass",
                detail="GetObject of an absent key returned NoSuchKey",
            )
        return _fail(store.physical, check, _raised("the operator's GetObject", exc))
    return _fail(store.physical, check, "GetObject of the absent probe key returned an object")


def http_get_status(url: str) -> int:
    """GET `url` over HTTPS, discard the body, and return the status."""
    request = urllib.request.Request(url)  # noqa: S310 - open_https refuses any non-https scheme
    with open_https(request, timeout=LAYER_FETCH_TIMEOUT_SECONDS) as response:
        while response.read(65536):
            pass
        return int(response.status)


@dataclass(frozen=True)
class RuntimeImage:
    """The Runtime's deployed image: its ECR repository, tag and repository ARN."""

    repository: str
    tag: str
    repository_arn: str


def resolve_runtime_image(
    ctx: SmokeContext, identity: CallerIdentity
) -> RuntimeImage | Unavailable:
    """The Runtime's `containerUri`, read by the operator from `get-agent-runtime`."""
    runtime_arn = ctx.outputs.get(RUNTIME_ARN_OUTPUT)
    if not isinstance(runtime_arn, str) or not runtime_arn:
        return _output_absent(RUNTIME_ARN_OUTPUT)
    try:
        runtime = ctx.client("bedrock-agentcore-control").get_agent_runtime(
            agentRuntimeId=runtime_arn.rpartition("/")[2]
        )
        uri = str(runtime["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"])
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return Unavailable("fail", _raised("get-agent-runtime", exc))
    repository_uri, _, tag = uri.rpartition(":")
    repository = repository_uri.partition("/")[2]
    if not repository or not tag:
        return Unavailable("fail", f"the containerUri names no repository and tag: {uri!r}")
    return RuntimeImage(
        repository=repository,
        tag=tag,
        repository_arn=(
            f"arn:{identity.partition}:ecr:{ctx.region}:{identity.account}:repository/{repository}"
        ),
    )


def _image_config_digest(ecr: Any, repository: str, image_id: Mapping[str, str]) -> str:
    for _ in range(2):
        images = ecr.batch_get_image(repositoryName=repository, imageIds=[dict(image_id)])["images"]
        if len(images) != 1:
            raise ValueError(f"batch-get-image returned {len(images)} image(s) for {image_id}")
        manifest = json.loads(images[0]["imageManifest"])
        config = manifest.get("config")
        if isinstance(config, Mapping) and config.get("digest"):
            return str(config["digest"])
        children = manifest.get("manifests") or []
        if not children:
            break
        image_id = {"imageDigest": str(children[0]["digest"])}
    raise ValueError("the image manifest names no config blob")


def ecr_layer_fetch_row(
    ecr: Any, image: RuntimeImage | Unavailable, fetch: LayerFetcher
) -> CheckResult:
    """The probe role, outside every allowlist, downloads the Runtime image's config blob."""
    if isinstance(image, Unavailable):
        return image.row("ecr", CHECK_ECR_LAYER_FETCH)
    try:
        digest = _image_config_digest(ecr, image.repository, {"imageTag": image.tag})
        url = str(
            ecr.get_download_url_for_layer(repositoryName=image.repository, layerDigest=digest)[
                "downloadUrl"
            ]
        )
        status = fetch(url)
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return _fail(
            image.repository,
            CHECK_ECR_LAYER_FETCH,
            _raised("the probe role fetching the image config blob", exc),
        )
    return CheckResult(
        resource=image.repository,
        check=CHECK_ECR_LAYER_FETCH,
        status="pass" if status == 200 else "fail",
        detail=f"the probe role's config blob GET returned HTTP {status}; bytes discarded",
    )


@dataclass
class _Delivery:
    check: str
    time_field: str
    error_field: str
    row: CheckResult | None = None
    observed: str = ""
    error: str = ""


def trail_delivery_rows(
    ctx: SmokeContext,
    run_started: datetime,
    *,
    sleeper: Callable[[float], None],
    poll_seconds: float,
) -> list[CheckResult]:
    """S3 and CloudWatch Logs delivery after the run start minus 15 minutes, with no error."""
    checks = (CHECK_CLOUDTRAIL_DELIVERY, CHECK_CLOUDTRAIL_LOGS_DELIVERY)
    resource, trail = resolve_trail(ctx)
    if isinstance(trail, str):
        return [_fail(resource, check, trail) for check in checks]
    deliveries = [
        _Delivery(CHECK_CLOUDTRAIL_DELIVERY, "LatestDeliveryTime", "LatestDeliveryError"),
        _Delivery(
            CHECK_CLOUDTRAIL_LOGS_DELIVERY,
            "LatestCloudWatchLogsDeliveryTime",
            "LatestCloudWatchLogsDeliveryError",
        ),
    ]
    if not trail.get("CloudWatchLogsLogGroupArn"):
        deliveries[1].row = CheckResult(
            resource=resource,
            check=CHECK_CLOUDTRAIL_LOGS_DELIVERY,
            status=DEFER,
            defer_reason=DEFER_TRAIL_NO_CLOUDWATCH,
            detail="the trail names no CloudWatchLogsLogGroupArn",
        )
    name = str(trail.get("TrailARN") or trail.get("Name"))
    threshold = run_started - DELIVERY_FRESHNESS
    deadline = Deadline(seconds=poll_seconds)
    while any(d.row is None for d in deliveries):
        try:
            status = ctx.client("cloudtrail").get_trail_status(Name=name)
        except Exception as exc:  # noqa: BLE001 - reported as failing rows
            for d in deliveries:
                d.row = d.row or _fail(resource, d.check, _raised("get-trail-status", exc))
            break
        for d in (d for d in deliveries if d.row is None):
            delivered = status.get(d.time_field)
            d.error = str(status.get(d.error_field) or "")
            d.observed = f"{d.time_field}={delivered!s}, {d.error_field}={d.error!r}"
            if isinstance(delivered, datetime) and delivered >= threshold and not d.error:
                d.row = CheckResult(
                    resource=resource,
                    check=d.check,
                    status="pass",
                    detail=f"{d.observed}; threshold {threshold.isoformat()}",
                )
        if deadline.expired or all(d.row is not None for d in deliveries):
            break
        sleeper(min(DELIVERY_POLL_INTERVAL_SECONDS, deadline.remaining))
    rows: list[CheckResult] = []
    for d in deliveries:
        if d.row is not None:
            rows.append(d.row)
        elif d.error:
            rows.append(_fail(resource, d.check, f"{d.observed} after {poll_seconds:.0f}s"))
        else:
            rows.append(
                CheckResult(
                    resource=resource,
                    check=d.check,
                    status=DEFER,
                    defer_reason=DEFER_DELIVERY_NOT_CURRENT,
                    detail=(
                        f"{d.observed}; no delivery after {threshold.isoformat()} "
                        f"within {poll_seconds:.0f}s"
                    ),
                )
            )
    return rows


def flow_log_row(ctx: SmokeContext) -> CheckResult:
    """Every flow log on the stack's VPC reports `DeliverLogsStatus=SUCCESS` with no error."""
    vpc_id = ctx.outputs.get(VPC_ID_OUTPUT)
    if not isinstance(vpc_id, str) or not vpc_id:
        return _output_absent(VPC_ID_OUTPUT).row("vpc", CHECK_FLOW_LOG_DELIVERY)
    try:
        flow_logs = ctx.client("ec2").describe_flow_logs(
            Filters=[{"Name": "resource-id", "Values": [vpc_id]}]
        )["FlowLogs"]
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return _fail(vpc_id, CHECK_FLOW_LOG_DELIVERY, _raised("describe-flow-logs", exc))
    if not flow_logs:
        return CheckResult(
            resource=vpc_id,
            check=CHECK_FLOW_LOG_DELIVERY,
            status=DEFER,
            defer_reason=DEFER_NO_FLOW_LOG,
            detail="the VPC has no flow log",
        )
    problems = [
        f"{f.get('FlowLogId')}: DeliverLogsStatus={f.get('DeliverLogsStatus')!r}, "
        f"DeliverLogsErrorMessage={f.get('DeliverLogsErrorMessage')!r}"
        for f in flow_logs
        if f.get("DeliverLogsStatus") != "SUCCESS" or f.get("DeliverLogsErrorMessage")
    ]
    return CheckResult(
        resource=vpc_id,
        check=CHECK_FLOW_LOG_DELIVERY,
        status="fail" if problems else "pass",
        detail="; ".join(problems) or f"{len(flow_logs)} flow log(s) deliver with SUCCESS",
    )


def _lookup_pages(
    ctx: SmokeContext,
    attribute: tuple[str, str],
    start: datetime,
    sleeper: Callable[[float], None],
) -> tuple[list[Mapping[str, Any]], bool]:
    """CloudTrail events matching `attribute` since `start`, and whether `LOOKUP_MAX_PAGES` cut it."""
    events: list[Mapping[str, Any]] = []
    pages = (
        ctx.client("cloudtrail")
        .get_paginator("lookup_events")
        .paginate(
            LookupAttributes=[{"AttributeKey": attribute[0], "AttributeValue": attribute[1]}],
            StartTime=start,
        )
    )
    for index, page in enumerate(pages):
        if index >= LOOKUP_MAX_PAGES:
            return events, True
        if index:
            sleeper(LOOKUP_PAGE_PAUSE_SECONDS)
        events.extend(page.get("Events", []))
    return events, False


def logs_key_changed_at(
    ctx: SmokeContext,
    logs_key: Discovered,
    now: datetime,
    sleeper: Callable[[float], None],
) -> datetime | Unavailable:
    """The latest `PutKeyPolicy` on the logs CMK in the last 90 days, from CloudTrail."""
    if isinstance(logs_key, Unavailable):
        return logs_key
    key_id = logs_key.arn.rpartition("/")[2]
    try:
        events, _ = _lookup_pages(
            ctx, ("EventName", "PutKeyPolicy"), now - KEY_CHANGE_LOOKBACK, sleeper
        )
    except Exception as exc:  # noqa: BLE001 - reported as failing rows
        return Unavailable("fail", _raised("cloudtrail lookup-events PutKeyPolicy", exc))
    times: list[datetime] = [
        event["EventTime"]
        for event in events
        if isinstance(event.get("EventTime"), datetime)
        and any(
            str(r.get("ResourceName", "")) in (logs_key.arn, key_id)
            for r in event.get("Resources", [])
        )
    ]
    if not times:
        return Unavailable(
            DEFER,
            "CloudTrail shows no PutKeyPolicy on the logs CMK in the last 90 days",
            DEFER_KEY_CHANGE_UNOBSERVED,
        )
    return max(times)


def _event_detail(event: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        detail = json.loads(str(event.get("CloudTrailEvent") or "{}"))
    except json.JSONDecodeError:
        return {}
    return detail if isinstance(detail, Mapping) else {}


def _names_key(event: Mapping[str, Any], key_arn: str) -> bool:
    key_id = key_arn.rpartition("/")[2]
    if any(
        str(r.get("ResourceName", "")) in (key_arn, key_id) for r in event.get("Resources") or []
    ):
        return True
    return any(
        isinstance(r, Mapping) and str(r.get("ARN", "")) == key_arn
        for r in _event_detail(event).get("resources") or []
    )


def logs_key_events(
    ctx: SmokeContext, key_arn: str, since: datetime, sleeper: Callable[[float], None]
) -> tuple[list[Mapping[str, Any]], bool]:
    """CloudTrail events on `key_arn` since `since`, and whether either lookup was cut.

    The union of the `ResourceName` lookup and the `EventName=Decrypt` lookup restricted to events
    naming the key, deduplicated by `EventId`.
    """
    by_resource, resource_cut = _lookup_pages(ctx, ("ResourceName", key_arn), since, sleeper)
    decrypts, decrypt_cut = _lookup_pages(ctx, ("EventName", "Decrypt"), since, sleeper)
    events: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for event in [*by_resource, *(e for e in decrypts if _names_key(e, key_arn))]:
        event_id = str(event.get("EventId") or "")
        if event_id and event_id in seen:
            continue
        seen.add(event_id)
        events.append(event)
    return events, resource_cut or decrypt_cut


def _issuer_arn(detail: Mapping[str, Any]) -> str:
    identity = detail.get("userIdentity") or {}
    issuer = (identity.get("sessionContext") or {}).get("sessionIssuer") or {}
    return str(issuer.get("arn") or identity.get("arn") or "")


@dataclass
class LogsKeyTally:
    """CloudTrail events on the logs CMK, sorted by whose they are and whether they count."""

    admitted_denials: list[str] = dataclasses.field(default_factory=list)
    failed_decrypts: list[str] = dataclasses.field(default_factory=list)
    decrypted: set[str] = dataclasses.field(default_factory=set)
    probe_events: int = 0
    dry_runs: int = 0
    foreign_denials: int = 0

    def excluded(self) -> str:
        """The events read but not counted, for a row's detail."""
        return (
            f"excluded: {self.probe_events} probe-role event(s), {self.dry_runs} "
            f"{DRY_RUN_ERROR_CODE}, {self.foreign_denials} expected denial(s) of "
            "non-allowlisted principals"
        )


def tally_logs_key_events(
    events: Iterable[Mapping[str, Any]], admitted_roles: frozenset[str]
) -> LogsKeyTally:
    """Count only what the logs CMK must admit: `admitted_roles` and AWS service principals.

    Events issued by a `crm-agent-eval-probe-*` role and `DryRunOperationException` results are
    excluded; a denial of any other principal is the boundary working and is only counted.
    """
    tally = LogsKeyTally()
    for event in events:
        detail = _event_detail(event)
        code = str(detail.get("errorCode") or "")
        issuer = _issuer_arn(detail)
        name = str(event.get("EventName") or detail.get("eventName") or "")
        identity = detail.get("userIdentity") or {}
        is_service = identity.get("type") == "AWSService"
        if issuer.rpartition("/")[2].startswith(PROBE_ROLE_PREFIX):
            tally.probe_events += 1
            continue
        if code == DRY_RUN_ERROR_CODE:
            tally.dry_runs += 1
            continue
        admitted = is_service or issuer in admitted_roles
        if code.startswith("AccessDenied"):
            if admitted:
                principal = issuer or f"service {identity.get('invokedBy', 'unknown')}"
                tally.admitted_denials.append(f"{name} by {principal}")
            else:
                tally.foreign_denials += 1
        if name == "Decrypt" and issuer in admitted_roles:
            if code:
                tally.failed_decrypts.append(f"{issuer}: {code}")
            else:
                tally.decrypted.add(issuer)
    return tally


def logs_key_event_rows(
    ctx: SmokeContext,
    logs_key: Discovered,
    since: datetime | Unavailable,
    functions: Mapping[str, str] | Unavailable,
    operators: frozenset[str] | Unavailable,
    sleeper: Callable[[float], None],
) -> list[CheckResult]:
    """No admitted principal denied on the logs CMK since it changed, and a Decrypt by every function."""
    checks = (CHECK_LOGS_KEY_ERRORS, CHECK_LOGS_KEY_DECRYPT)
    if isinstance(logs_key, Unavailable):
        return [logs_key.row(LOGS_KEY, check) for check in checks]
    if isinstance(since, Unavailable):
        return [since.row(logs_key.arn, check) for check in checks]
    if isinstance(functions, Unavailable):
        return [functions.row(logs_key.arn, check) for check in checks]
    if isinstance(operators, Unavailable):
        return [operators.row(logs_key.arn, check) for check in checks]
    try:
        events, truncated = logs_key_events(ctx, logs_key.arn, since, sleeper)
    except Exception as exc:  # noqa: BLE001 - reported as failing rows
        return [
            _fail(logs_key.arn, check, _raised("cloudtrail lookup-events", exc)) for check in checks
        ]
    tally = tally_logs_key_events(events, frozenset(functions.values()) | operators)
    window = f"since {since.isoformat()} ({len(events)} event(s) read; {tally.excluded()})"
    rows: list[CheckResult] = []
    if tally.admitted_denials:
        rows.append(
            _fail(
                logs_key.arn,
                CHECK_LOGS_KEY_ERRORS,
                f"{window}: admitted principals denied {sorted(tally.admitted_denials)[:10]}",
            )
        )
    elif truncated:
        rows.append(
            CheckResult(
                resource=logs_key.arn,
                check=CHECK_LOGS_KEY_ERRORS,
                status=DEFER,
                defer_reason=DEFER_LOOKUP_TRUNCATED,
                detail=f"{window}: none denied in the pages read, cut at {LOOKUP_MAX_PAGES} pages",
            )
        )
    else:
        rows.append(
            CheckResult(
                resource=logs_key.arn,
                check=CHECK_LOGS_KEY_ERRORS,
                status="pass",
                detail=f"{window}: no admitted principal denied",
            )
        )
    missing = sorted(name for name, role in functions.items() if role not in tally.decrypted)
    if tally.failed_decrypts:
        rows.append(
            _fail(
                logs_key.arn,
                CHECK_LOGS_KEY_DECRYPT,
                f"{window}: errored Decrypt {sorted(tally.failed_decrypts)[:10]}",
            )
        )
    elif missing:
        rows.append(
            CheckResult(
                resource=logs_key.arn,
                check=CHECK_LOGS_KEY_DECRYPT,
                status=DEFER,
                defer_reason=DEFER_FUNCTIONS_NOT_INVOKED,
                detail=(
                    f"{window}: {len(functions) - len(missing)}/{len(functions)} functions decrypted; "
                    f"no Decrypt yet by {missing}"
                    + (f" (cut at {LOOKUP_MAX_PAGES} pages)" if truncated else "")
                ),
            )
        )
    else:
        rows.append(
            CheckResult(
                resource=logs_key.arn,
                check=CHECK_LOGS_KEY_DECRYPT,
                status="pass",
                detail=f"{window}: all {len(functions)} functions decrypted, none errored",
            )
        )
    return rows


def ingestion_log_groups(ctx: SmokeContext) -> dict[str, str | Unavailable]:
    """The non-Lambda log groups whose writers the logs CMK must still admit."""
    runtime = ctx.outputs.get(RUNTIME_LOG_GROUP_OUTPUT)
    return {
        API_ACCESS_LOG: API_ACCESS_LOG_GROUP,
        RUNTIME_LOG: (
            runtime
            if isinstance(runtime, str) and runtime
            else _output_absent(RUNTIME_LOG_GROUP_OUTPUT)
        ),
        CODEBUILD_LOG: f"/aws/codebuild/{CODEBUILD_PROJECT_NAME}",
    }


def _has_event_since(ctx: SmokeContext, group: str, since: datetime) -> bool:
    pages = (
        ctx.client("logs")
        .get_paginator("filter_log_events")
        .paginate(
            logGroupName=group,
            startTime=int(since.timestamp() * 1000),
            PaginationConfig={"PageSize": 1, "MaxItems": 1},
        )
    )
    return any(page.get("events") for page in pages)


def ingestion_rows(
    ctx: SmokeContext,
    since: datetime | Unavailable,
    *,
    sleeper: Callable[[float], None],
    poll_seconds: float,
) -> list[CheckResult]:
    """Each non-Lambda writer's log group holds an event newer than the logs CMK's change."""
    groups = ingestion_log_groups(ctx)
    rows: dict[str, CheckResult] = {}
    for name, group in groups.items():
        check = f"{CHECK_LOG_INGESTION}:{name}"
        if isinstance(group, Unavailable):
            rows[name] = group.row(name, check)
        elif isinstance(since, Unavailable):
            rows[name] = since.row(group, check)
    deadline = Deadline(seconds=poll_seconds)
    while True:
        for name, group in groups.items():
            if name in rows or isinstance(group, Unavailable) or isinstance(since, Unavailable):
                continue
            check = f"{CHECK_LOG_INGESTION}:{name}"
            try:
                found = _has_event_since(ctx, group, since)
            except Exception as exc:  # noqa: BLE001 - reported as a failing row
                rows[name] = _fail(group, check, _raised("filter-log-events", exc))
                continue
            if found:
                rows[name] = CheckResult(
                    resource=group,
                    check=check,
                    status="pass",
                    detail=f"an event after the logs CMK's change at {since.isoformat()}",
                )
        if len(rows) == len(groups) or deadline.expired:
            break
        sleeper(min(INGESTION_POLL_INTERVAL_SECONDS, deadline.remaining))
    for name, group in groups.items():
        if name not in rows and isinstance(group, str) and isinstance(since, datetime):
            rows[name] = CheckResult(
                resource=group,
                check=f"{CHECK_LOG_INGESTION}:{name}",
                status=DEFER,
                defer_reason=DEFER_NO_INGESTION,
                detail=(
                    f"no event after the logs CMK's change at {since.isoformat()} "
                    f"within {poll_seconds:.0f}s"
                ),
            )
    return [rows[name] for name in groups]


# ---------------------------------------------------------------------------
# Probe.
# ---------------------------------------------------------------------------


def _probe_object_key(run_id: str) -> str:
    return f"probe-{run_id}/absent"


def _lock_key(run_id: str) -> dict[str, dict[str, str]]:
    return {LOCK_TABLE_PARTITION_KEY: {"S": f"probe-{run_id}"}}


def probe_calls(store: str) -> tuple[str, ...]:
    """The probe calls made against `store`."""
    if store in KEY_STORES:
        return KEY_PROBE_CALLS
    if store == DYNAMODB_KEY:
        return (PROBE_REVOKE_GRANT,)
    if store == LOCK_TABLE:
        return LOCK_TABLE_PROBE_CALLS
    return (
        WRITE_ONLY_BUCKET_PROBE_CALLS
        if store in WRITE_ONLY_BUCKETS
        else DATA_PLANE_BUCKET_PROBE_CALLS
    )


def probe_identity_policy(
    stores: Mapping[str, Discovered],
    log_group: Mapping[str, Any] | None,
    image: RuntimeImage | Unavailable,
    cursor: CursorKey | Unavailable | None = None,
) -> dict[str, Any]:
    """Allow exactly the probed actions on the discovered stores, log group, image repository and
    cursor key."""
    found = {name: s for name, s in stores.items() if isinstance(s, Store)}
    keys = sorted(found[n].arn for n in KEY_STORES if n in found)
    admin_keys = sorted(s.arn for s in found.values() if s.kind == "key")
    data_buckets = sorted(found[n].arn for n in DATA_PLANE_BUCKETS if n in found)
    write_buckets = sorted(found[n].arn for n in WRITE_ONLY_BUCKETS if n in found)
    statements: list[dict[str, Any]] = []
    if keys:
        statements.append(
            {
                "Sid": "ProbeKmsUse",
                "Effect": "Allow",
                "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
                "Resource": keys,
            }
        )
    if admin_keys:
        statements.append(
            {
                "Sid": "ProbeKmsAdmin",
                "Effect": "Allow",
                "Action": ["kms:RevokeGrant"],
                "Resource": admin_keys,
            }
        )
    if data_buckets:
        statements.append(
            {
                "Sid": "ProbeS3Objects",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": [f"{arn}/probe-*" for arn in data_buckets],
            }
        )
        statements.append(
            {
                "Sid": "ProbeS3List",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": data_buckets,
            }
        )
    if write_buckets:
        statements.append(
            {
                "Sid": "ProbeS3Writes",
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [f"{arn}/probe-*" for arn in write_buckets],
            }
        )
    if LOCK_TABLE in found:
        statements.append(
            {
                "Sid": "ProbeLockTable",
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:PutResourcePolicy"],
                "Resource": [found[LOCK_TABLE].arn],
            }
        )
    if log_group is not None and log_group.get("arn"):
        statements.append(
            {
                "Sid": "ProbeLogs",
                "Effect": "Allow",
                "Action": ["logs:FilterLogEvents"],
                "Resource": [str(log_group["arn"])],
            }
        )
    if isinstance(cursor, CursorKey):
        statements.append(
            {
                "Sid": "ProbeCursorKmsEncrypt",
                "Effect": "Allow",
                "Action": ["kms:Encrypt"],
                "Resource": [cursor.store.arn],
            }
        )
    if isinstance(image, RuntimeImage):
        statements.append(
            {
                "Sid": "ProbeEcrPull",
                "Effect": "Allow",
                "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                "Resource": [image.repository_arn],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _placeholder_table_policy(table_arn: str, role_arn: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ProbePlaceholder",
                    "Effect": "Deny",
                    "Principal": {"AWS": role_arn},
                    "Action": "dynamodb:GetItem",
                    "Resource": table_arn,
                }
            ],
        }
    )


def probe_request(
    store: Store,
    call: str,
    *,
    run_id: str,
    role_arn: str,
    policy_text: str | None,
    ciphertext: bytes | None,
) -> tuple[str, str, dict[str, Any]] | str:
    """One probe call as `(service, operation, params)`, or why it cannot be made."""
    if call == PROBE_GENERATE_DATA_KEY:
        return "kms", "generate_data_key", {"KeyId": store.arn, "KeySpec": "AES_256"}
    if call == PROBE_DECRYPT:
        if ciphertext is None:
            return "the operator could not mint a ciphertext under this key"
        return "kms", "decrypt", {"KeyId": store.arn, "CiphertextBlob": ciphertext}
    if call == PROBE_REVOKE_GRANT:
        return "kms", "revoke_grant", {"KeyId": store.arn, "GrantId": PROBE_GRANT_ID}
    if call == PROBE_GET_OBJECT:
        return "s3", "get_object", {"Bucket": store.physical, "Key": _probe_object_key(run_id)}
    if call == PROBE_LIST_OBJECTS:
        return (
            "s3",
            "list_objects_v2",
            {"Bucket": store.physical, "Prefix": f"probe-{run_id}/", "MaxKeys": 1},
        )
    if call == PROBE_PUT_OBJECT:
        params: dict[str, Any] = {
            "Bucket": store.physical,
            "Key": _probe_object_key(run_id),
            "Body": b"",
            "IfMatch": PROBE_IF_MATCH,
            "ServerSideEncryption": store.sse_algorithm or "AES256",
        }
        if store.sse_algorithm == "aws:kms" and store.sse_key_arn:
            params["SSEKMSKeyId"] = store.sse_key_arn
        return "s3", "put_object", params
    if call == PROBE_GET_ITEM:
        return "dynamodb", "get_item", {"TableName": store.physical, "Key": _lock_key(run_id)}
    if call == PROBE_PUT_ITEM:
        return (
            "dynamodb",
            "put_item",
            {
                "TableName": store.physical,
                "Item": _lock_key(run_id),
                "ConditionExpression": "attribute_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": LOCK_TABLE_PARTITION_KEY},
            },
        )
    if call == PROBE_PUT_RESOURCE_POLICY:
        return (
            "dynamodb",
            "put_resource_policy",
            {
                "ResourceArn": store.arn,
                "Policy": policy_text or _placeholder_table_policy(store.arn, role_arn),
                "ExpectedRevisionId": PROBE_EXPECTED_REVISION_ID,
            },
        )
    raise ValueError(f"unknown probe call {call!r}")


def classify_store_probe(
    check_suffix: str, resource: str, outcome: BaseException | None, probe_role: str
) -> CheckResult:
    """Classify one probe outcome into its `probe_denied:<check_suffix>` row."""
    code = error_code(outcome) if outcome is not None else ""
    status_code = (
        (getattr(outcome, "response", {}) or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if outcome is not None
        else None
    )
    if code in REACHED_RESOURCE_CODES or status_code in (404, 412):
        message = str((getattr(outcome, "response", {}) or {}).get("Error", {}).get("Message", ""))
        return _fail(
            resource,
            f"probe_denied:{check_suffix}",
            f"the call reached the resource, no boundary refused it ({code or status_code}: {message})",
        )
    return classify_probe(check_suffix, resource, outcome, probe_role)


def _simulate(
    ctx: SmokeContext, principal_arn: str, store: Store, policy_text: str, root_arn: str
) -> dict[str, str]:
    response = ctx.client("iam").simulate_principal_policy(
        PolicySourceArn=principal_arn,
        ActionNames=list(S3_BUCKET_ADMIN_ACTIONS),
        ResourceArns=[store.arn],
        ResourcePolicy=policy_text,
        ResourceOwner=root_arn,
        ContextEntries=[
            {
                "ContextKeyName": PRINCIPAL_ARN_KEY,
                "ContextKeyValues": [principal_arn],
                "ContextKeyType": "string",
            },
            {
                "ContextKeyName": PRINCIPAL_IS_SERVICE_KEY,
                "ContextKeyValues": ["false"],
                "ContextKeyType": "boolean",
            },
            {
                "ContextKeyName": "aws:SecureTransport",
                "ContextKeyValues": ["true"],
                "ContextKeyType": "boolean",
            },
        ],
    )
    if response.get("IsTruncated"):
        raise ValueError("the simulation result is truncated")
    return {
        str(r.get("EvalActionName")): str(r.get("EvalDecision"))
        for r in response.get("EvaluationResults", [])
    }


def admin_simulation_row(
    ctx: SmokeContext,
    store: Store,
    policy_text: str | None,
    probe_arn: str,
    operator_arn: str,
    root_arn: str,
) -> CheckResult:
    """A simulation pair against the live bucket document: the probe denied, the operator not.

    Both halves name `aws:PrincipalArn` explicitly, so an `ArnNotEquals` that matched every
    principal would deny the operator too and fail the row.
    """
    check = f"admin_simulated_denied:{store.name}"
    if policy_text is None:
        return _fail(store.physical, check, "the bucket has no readable policy to simulate")
    try:
        probe = _simulate(ctx, probe_arn, store, policy_text, root_arn)
        operator = _simulate(ctx, operator_arn, store, policy_text, root_arn)
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return _fail(store.physical, check, _raised("simulate-principal-policy", exc))
    problems = [
        f"probe {action}={probe.get(action, 'not evaluated')}"
        for action in S3_BUCKET_ADMIN_ACTIONS
        if probe.get(action) != "explicitDeny"
    ]
    problems.extend(
        f"operator {action}={operator.get(action, 'not evaluated')}"
        for action in S3_BUCKET_ADMIN_ACTIONS
        if operator.get(action, "explicitDeny") == "explicitDeny"
    )
    return CheckResult(
        resource=store.physical,
        check=check,
        status="fail" if problems else "pass",
        detail=(
            "simulation pair of the live document, not a call: "
            + (
                "; ".join(problems)
                if problems
                else f"{len(S3_BUCKET_ADMIN_ACTIONS)} admin actions explicitDeny for the probe "
                "and not for the operator"
            )
        ),
    )


def logs_probe_row(
    client: Any, log_group: Mapping[str, Any] | None, logs_key: Discovered, probe_role: str
) -> CheckResult:
    """The probe's `FilterLogEvents` on a log group encrypted with the logs CMK is refused.

    A successful read is a `defer` with `DEFER_LOGS_READ_NOT_KEY_GATED`, never a `pass`.
    """
    if isinstance(logs_key, Unavailable) or log_group is None:
        reason = (
            logs_key if isinstance(logs_key, Unavailable) else Unavailable("fail", "no log group")
        )
        return reason.row(LOGS_KEY, CHECK_LOGS_PROBE)
    name = str(log_group["logGroupName"])
    outcome: BaseException | None = None
    try:
        client.filter_log_events(logGroupName=name, limit=1)
    except Exception as exc:  # noqa: BLE001 - every outcome is classified
        outcome = exc
    if outcome is None:
        return CheckResult(
            resource=name,
            check=CHECK_LOGS_PROBE,
            status=DEFER,
            defer_reason=DEFER_LOGS_READ_NOT_KEY_GATED,
            detail=(
                "the read succeeded: CloudWatch Logs decrypts log data under its own service "
                "identity, so the logs-key principal Deny does not gate log reads"
            ),
        )
    row = classify_probe("logs", name, outcome, probe_role)
    return dataclasses.replace(row, check=CHECK_LOGS_PROBE)


def _not_probed(name: str, store: Discovered, check: str, detail: str) -> CheckResult:
    if isinstance(store, Unavailable):
        return store.row(name, check)
    return _fail(store.physical, check, detail)


def _pending_probe_checks(
    stores: Mapping[str, Discovered],
    image: RuntimeImage | Unavailable,
    cursor: CursorKey | Unavailable,
) -> list[tuple[str, str, Discovered]]:
    pending = [
        (name, f"probe_denied:{name}:{call}", stores[name])
        for name in PROBED_STORES
        for call in probe_calls(name)
    ]
    pending.extend((name, f"admin_simulated_denied:{name}", stores[name]) for name in BUCKET_STORES)
    pending.append((LOGS_KEY, CHECK_LOGS_PROBE, stores[LOGS_KEY]))
    ecr_target: Discovered = (
        image
        if isinstance(image, Unavailable)
        else Store(
            name="ecr", kind="repository", arn=image.repository_arn, physical=image.repository
        )
    )
    pending.append(("ecr", CHECK_ECR_LAYER_FETCH, ecr_target))
    cursor_target: Discovered = cursor if isinstance(cursor, Unavailable) else cursor.store
    pending.extend((CURSOR_KEY, check, cursor_target) for check in CURSOR_KEY_CHECKS)
    return pending


def _delete_probe_role(ctx: SmokeContext, role_name: str) -> CheckResult:
    iam = ctx.client("iam")
    try:
        for operation, params in (
            ("delete_role_policy", {"RoleName": role_name, "PolicyName": PROBE_INLINE_POLICY_NAME}),
            ("delete_role", {"RoleName": role_name}),
        ):
            try:
                getattr(iam, operation)(**params)
            except Exception as exc:
                if error_code(exc) != "NoSuchEntity":
                    raise
    except Exception as exc:  # noqa: BLE001 - reported as a failing row naming the role
        return _fail(
            role_name,
            CHECK_PROBE_ROLE_DELETED,
            f"deleting probe role {role_name} {_raised('', exc).strip()}. Delete it by hand.",
        )
    return CheckResult(
        resource=role_name,
        check=CHECK_PROBE_ROLE_DELETED,
        status="pass",
        detail=f"{role_name} deleted",
    )


def _mint_ciphertexts(ctx: SmokeContext, stores: Mapping[str, Discovered]) -> dict[str, bytes]:
    ciphertexts: dict[str, bytes] = {}
    for name in KEY_STORES:
        store = stores[name]
        if not isinstance(store, Store):
            continue
        try:
            ciphertexts[name] = bytes(
                ctx.client("kms").generate_data_key_without_plaintext(
                    KeyId=store.arn, KeySpec="AES_256"
                )["CiphertextBlob"]
            )
        except Exception:  # noqa: BLE001, S112 - the decrypt row reports the missing ciphertext
            continue
    return ciphertexts


def _store_probe_rows(
    clients: Mapping[str, Any],
    stores: Mapping[str, Discovered],
    policies: Mapping[str, str | None],
    ciphertexts: Mapping[str, bytes],
    *,
    run_id: str,
    role_arn: str,
    role_name: str,
) -> Iterator[CheckResult]:
    for name in PROBED_STORES:
        store = stores[name]
        for call in probe_calls(name):
            check = f"probe_denied:{name}:{call}"
            if not isinstance(store, Store):
                yield _not_probed(name, store, check, "")
                continue
            request = probe_request(
                store,
                call,
                run_id=run_id,
                role_arn=role_arn,
                policy_text=policies.get(name),
                ciphertext=ciphertexts.get(name),
            )
            if isinstance(request, str):
                yield _fail(store.physical, check, request)
                continue
            service, operation, params = request
            outcome: BaseException | None = None
            try:
                getattr(clients[service], operation)(**params)
            except Exception as exc:  # noqa: BLE001 - every outcome is classified
                outcome = exc
            yield classify_store_probe(f"{name}:{call}", store.physical, outcome, role_name)


def _probe_rows(
    ctx: SmokeContext,
    *,
    stores: Mapping[str, Discovered],
    policies: Mapping[str, str | None],
    log_group: Mapping[str, Any] | None,
    image: RuntimeImage | Unavailable,
    cursor: CursorKey | Unavailable,
    identity: CallerIdentity,
    root_arn: str,
    run_id: str,
    probe_client_factory: ProbeClientFactory,
    layer_fetcher: LayerFetcher,
    sleeper: Callable[[float], None],
    assume_timeout_seconds: float,
) -> Iterator[CheckResult]:
    pending = _pending_probe_checks(stores, image, cursor)
    identity_policy = probe_identity_policy(stores, log_group, image, cursor)
    role_name = probe_role_name(run_id)
    if not identity_policy["Statement"]:
        for name, check, store in pending:
            yield _not_probed(name, store, check, "no store was discovered")
        yield CheckResult(
            resource=role_name,
            check=CHECK_PROBE_ROLE_DELETED,
            status=DEFER,
            defer_reason=DEFER_NO_PROBE_TARGET,
            detail="no store was discovered, so no probe role was created",
        )
        return
    ciphertexts = _mint_ciphertexts(ctx, stores)
    iam = ctx.client("iam")
    rows: list[CheckResult] = []
    try:
        role_arn = str(
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(probe_trust_policy(identity.principal_arn)),
                Description="store_principal_deny smoke probe; deleted by the smoke",
                MaxSessionDuration=3600,
                Tags=[{"Key": k, "Value": v} for k, v in PROBE_ROLE_TAGS],
            )["Role"]["Arn"]
        )
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName=PROBE_INLINE_POLICY_NAME,
            PolicyDocument=json.dumps(identity_policy),
        )
        credentials = assume_probe_role(
            ctx, role_arn, role_name, timeout_seconds=assume_timeout_seconds, sleeper=sleeper
        )
        clients = {
            service: probe_client_factory(ctx, service, credentials)
            for service in ("kms", "s3", "dynamodb", "logs", "ecr")
        }
        rows.extend(
            _store_probe_rows(
                clients,
                stores,
                policies,
                ciphertexts,
                run_id=run_id,
                role_arn=role_arn,
                role_name=role_name,
            )
        )
        for name in BUCKET_STORES:
            store = stores[name]
            check = f"admin_simulated_denied:{name}"
            if not isinstance(store, Store):
                rows.append(_not_probed(name, store, check, ""))
                continue
            rows.append(
                admin_simulation_row(
                    ctx, store, policies.get(name), role_arn, identity.principal_arn, root_arn
                )
            )
        rows.append(logs_probe_row(clients["logs"], log_group, stores[LOGS_KEY], role_name))
        rows.append(ecr_layer_fetch_row(clients["ecr"], image, layer_fetcher))
        if isinstance(cursor, Unavailable):
            rows.extend(cursor.row(CURSOR_KEY, check) for check in CURSOR_KEY_CHECKS)
        else:
            _service, operation, params = cursor_probe_request(cursor, run_id)
            outcome: BaseException | None = None
            try:
                getattr(clients["kms"], operation)(**params)
            except Exception as exc:  # noqa: BLE001 - every outcome is classified
                outcome = exc
            rows.append(
                classify_store_probe(
                    f"{CURSOR_KEY}:{PROBE_CURSOR_ENCRYPT}",
                    cursor.store.physical,
                    outcome,
                    role_name,
                )
            )
            rows.extend(
                cursor_key_simulation_rows(ctx, cursor, role_arn, identity.principal_arn, root_arn)
            )
    except Exception as exc:  # noqa: BLE001 - reported as failing rows; teardown still runs
        rows.append(
            _fail(
                role_name,
                CHECK_PROBE_ROLE_SETUP,
                f"{_raised('setting up or running the probe', exc)}; the rows below were not classified",
            )
        )
        done = {row.check for row in rows}
        rows.extend(
            _not_probed(name, store, check, "the probe did not run")
            for name, check, store in pending
            if check not in done
        )
    finally:
        rows.append(_delete_probe_role(ctx, role_name))
    yield from rows


# ---------------------------------------------------------------------------
# Entry points.
# ---------------------------------------------------------------------------


def run_checks(
    ctx: SmokeContext,
    *,
    probe_client_factory: ProbeClientFactory = session_probe_client,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    layer_fetcher: LayerFetcher = http_get_status,
    backend_config_path: Path = BACKEND_CONFIG_PATH,
    run_id: str | None = None,
    assume_timeout_seconds: float = ASSUME_TIMEOUT_SECONDS,
    delivery_poll_seconds: float = DELIVERY_POLL_SECONDS,
    ingestion_poll_seconds: float = INGESTION_POLL_SECONDS,
) -> Iterator[CheckResult]:
    """Yield every row of this resource class, account ids redacted; the seams exist for tests."""
    for row in _rows(
        ctx,
        probe_client_factory=probe_client_factory,
        sleeper=sleeper,
        clock=clock,
        layer_fetcher=layer_fetcher,
        backend_config_path=backend_config_path,
        run_id=run_id or uuid.uuid4().hex[:12],
        assume_timeout_seconds=assume_timeout_seconds,
        delivery_poll_seconds=delivery_poll_seconds,
        ingestion_poll_seconds=ingestion_poll_seconds,
    ):
        yield _redacted(row)


def _rows(
    ctx: SmokeContext,
    *,
    probe_client_factory: ProbeClientFactory,
    sleeper: Callable[[float], None],
    clock: Callable[[], datetime],
    layer_fetcher: LayerFetcher,
    backend_config_path: Path,
    run_id: str,
    assume_timeout_seconds: float,
    delivery_poll_seconds: float,
    ingestion_poll_seconds: float,
) -> Iterator[CheckResult]:
    run_started = clock()
    identity = caller_identity(ctx)
    root_arn = f"arn:{identity.partition}:iam::{identity.account}:root"
    stores, log_group = discover_stores(ctx, read_backend_config(backend_config_path))
    operators = _operators(ctx)
    functions = discover_logs_key_functions(ctx, stores[LOGS_KEY])
    allow = Allowlists(
        operators=operators,
        root=root_arn,
        logs_key_roles=(
            functions if isinstance(functions, Unavailable) else frozenset(functions.values())
        ),
        eval_artifacts_roles=discover_eval_artifacts_roles(ctx),
    )
    policies: dict[str, str | None] = {}
    for name in STORES:
        row, policies[name] = _shape_row(ctx, name, stores[name], allow)
        yield row
    yield state_operator_row(stores, policies, operators)
    for name in BUCKET_STORES + (LOCK_TABLE,):
        yield operator_admitted_row(ctx, name, stores[name], run_id)
    yield from _probe_rows(
        ctx,
        stores=stores,
        policies=policies,
        log_group=log_group,
        image=resolve_runtime_image(ctx, identity),
        cursor=discover_cursor_key(ctx),
        identity=identity,
        root_arn=root_arn,
        run_id=run_id,
        probe_client_factory=probe_client_factory,
        layer_fetcher=layer_fetcher,
        sleeper=sleeper,
        assume_timeout_seconds=assume_timeout_seconds,
    )
    yield from trail_delivery_rows(
        ctx, run_started, sleeper=sleeper, poll_seconds=delivery_poll_seconds
    )
    yield flow_log_row(ctx)
    since = logs_key_changed_at(ctx, stores[LOGS_KEY], run_started, sleeper)
    yield from logs_key_event_rows(ctx, stores[LOGS_KEY], since, functions, operators, sleeper)
    yield from ingestion_rows(ctx, since, sleeper=sleeper, poll_seconds=ingestion_poll_seconds)


def checks(ctx: SmokeContext) -> Iterable[CheckResult]:
    """The rows `main` emits, against the real probe-client factory and HTTPS fetcher."""
    return run_checks(ctx)


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """Every row `run_checks` emits on a clean run; each is emitted whatever its outcome."""
    probes = sum(len(probe_calls(name)) for name in PROBED_STORES)
    admitted = len(BUCKET_STORES) + 1
    singletons = (
        *CURSOR_KEY_CHECKS,
        CHECK_STATE_OPERATORS,
        CHECK_ECR_LAYER_FETCH,
        CHECK_LOGS_PROBE,
        CHECK_PROBE_ROLE_DELETED,
        CHECK_CLOUDTRAIL_DELIVERY,
        CHECK_CLOUDTRAIL_LOGS_DELIVERY,
        CHECK_FLOW_LOG_DELIVERY,
        CHECK_LOGS_KEY_ERRORS,
        CHECK_LOGS_KEY_DECRYPT,
    )
    return ExpectedCheckCount(
        minimum=(
            len(STORES)
            + admitted
            + probes
            + len(BUCKET_STORES)
            + len(INGESTION_LOGS)
            + len(singletons)
        ),
        basis=(
            f"{len(STORES)} policy shapes + {admitted} operator_admitted + {probes} probe calls + "
            f"{len(BUCKET_STORES)} admin simulation pairs + {len(INGESTION_LOGS)} "
            f"{CHECK_LOG_INGESTION} + {', '.join(singletons)}"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the store principal-Deny smoke and return the process exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
