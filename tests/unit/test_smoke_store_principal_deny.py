# SPDX-License-Identifier: MIT
"""Offline tests for `scripts.smoke.store_principal_deny`.

Every AWS call goes through a real boto3 client: botocore validates and serialises the request,
and a `before-call` hook answers it from `FakeAccount`. Error answers use the real error shapes,
codes and messages.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import boto3
import pytest
import scripts.smoke.store_principal_deny as smoke
from botocore import xform_name
from botocore.awsrequest import AWSResponse
from botocore.validate import validate_parameters
from scripts.smoke.harness import DEFER, CheckResult, context_for_test

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_RUN_ID = "testrun1"
_PROBE_ROLE = f"crm-agent-eval-probe-{_RUN_ID}"
_PROBE_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/{_PROBE_ROLE}"
_OPERATOR_ROLE = "OperatorRole"
_OPERATOR_ARN = f"arn:aws:iam::{_ACCOUNT}:role/ops/{_OPERATOR_ROLE}"
_ROOT_ARN = f"arn:aws:iam::{_ACCOUNT}:root"
_SENSITIVE_OUTPUT = "sensitive-output-marker-never-in-a-row"
_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _key_arn(suffix: str) -> str:
    return f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeee{suffix}"


_DYNAMODB_KEY_ARN = _key_arn("00a6")
_CRM_TABLE = "crm-agent-eval-accounts"
_VPC_ID = "vpc-0abc"
_RUNTIME_LOG_GROUP = "/aws/bedrock-agentcore/runtimes/crm_agent_eval-AbCdEf-DEFAULT"
_INGESTION_GROUPS = {
    smoke.API_ACCESS_LOG: "/aws/apigateway/crm-agent-eval-api",
    smoke.RUNTIME_LOG: _RUNTIME_LOG_GROUP,
    smoke.CODEBUILD_LOG: "/aws/codebuild/crm-agent-eval-agent-image",
}
_KEY_ARNS = {
    smoke.TFSTATE_KEY: _key_arn("00a1"),
    smoke.EVAL_ARTIFACTS_KEY: _key_arn("00a2"),
    smoke.LOGS_KEY: _key_arn("00a3"),
    smoke.CLOUDTRAIL_KEY: _key_arn("00a4"),
    smoke.FRONTEND_KEY: _key_arn("00a5"),
}
_BUCKETS = {
    smoke.STATE_BUCKET: f"crm-agent-eval-tfstate-{_ACCOUNT}",
    smoke.STATE_LOG_BUCKET: f"crm-agent-eval-tfstate-logs-{_ACCOUNT}",
    smoke.EVAL_ARTIFACTS_BUCKET: f"crm-agent-eval-eval-artifacts-{_ACCOUNT}",
    smoke.CLOUDTRAIL_BUCKET: f"crm-agent-eval-cloudtrail-{_ACCOUNT}",
    smoke.FRONTEND_ORIGIN_BUCKET: f"crm-agent-eval-frontend-origin-{_ACCOUNT}",
}
#: Each bucket's default encryption: the CMK store it uses, or `None` for SSE-S3.
_BUCKET_KEYS: dict[str, str | None] = {
    smoke.STATE_BUCKET: smoke.TFSTATE_KEY,
    smoke.STATE_LOG_BUCKET: None,
    smoke.EVAL_ARTIFACTS_BUCKET: smoke.EVAL_ARTIFACTS_KEY,
    smoke.CLOUDTRAIL_BUCKET: smoke.CLOUDTRAIL_KEY,
    smoke.FRONTEND_ORIGIN_BUCKET: smoke.FRONTEND_KEY,
}
_LOCK_TABLE = "crm-agent-eval-tflock"
_LOCK_TABLE_ARN = f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/{_LOCK_TABLE}"
_LOG_GROUP = "/aws/lambda/crm-agent-eval-accounts-get"
_LOG_GROUP_ARN = f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{_LOG_GROUP}:*"
_ACCOUNTS_GET_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-accounts-get-role"
_RELAY_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-agent-relay-role"
_EVAL_ROLES = {
    "crm-agent-eval-eval-trigger-role": f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-eval-trigger-role",
    "crm-agent-eval-sweep-control-role": f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-sweep-control-role",
    "crm-agent-eval-codebuild-role": f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-codebuild-role",
}
_RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:runtime/crm_agent_eval-AbCdEf"
_REPOSITORY = "crm-agent-eval-agent"
_CONFIG_DIGEST = "sha256:" + "c" * 64
_OUTPUTS: dict[str, Any] = {
    "eval_artifacts_bucket_name": _BUCKETS[smoke.EVAL_ARTIFACTS_BUCKET],
    "cloudtrail_s3_bucket_name": _BUCKETS[smoke.CLOUDTRAIL_BUCKET],
    "frontend_origin_bucket_name": _BUCKETS[smoke.FRONTEND_ORIGIN_BUCKET],
    "crm_operator_principal_arns": [_OPERATOR_ARN],
    "agentcore_runtime_arn": _RUNTIME_ARN,
    "agentcore_runtime_log_group_name": _RUNTIME_LOG_GROUP,
    "dynamodb_table_names": {"accounts": _CRM_TABLE, "contacts": "crm-agent-eval-contacts"},
    "vpc_id": _VPC_ID,
    "cognito_mcp_client_secret": _SENSITIVE_OUTPUT,
}

#: The designed S3 tuples, literal here so a drift in the module constants reds.
_DESIGN_S3_READ = [
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
]
_DESIGN_S3_WRITE = [
    "s3:PutObject",
    "s3:PutObjectTagging",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:DeleteObjectTagging",
    "s3:AbortMultipartUpload",
    "s3:PutObjectVersionTagging",
    "s3:DeleteObjectVersionTagging",
]
_DESIGN_S3_ADMIN = [
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
]
_DESIGN_KEY_USE = [
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
    "kms:CreateGrant",
]
_DESIGN_KEY_ADMIN = [
    "kms:PutKeyPolicy",
    "kms:ScheduleKeyDeletion",
    "kms:DisableKey",
    "kms:RevokeGrant",
    "kms:DisableKeyRotation",
]
_DESIGN_STORE_KEY_ADMIN = [
    *_DESIGN_KEY_ADMIN,
    "kms:CreateAlias",
    "kms:UpdateAlias",
    "kms:DeleteAlias",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:UpdateKeyDescription",
]
_ROOT_SIDS = {
    smoke.TFSTATE_KEY: "EnableRootAccountPermissions",
    smoke.EVAL_ARTIFACTS_KEY: "Enable IAM User Permissions",
    smoke.LOGS_KEY: "EnableIAMUserPermissions",
    smoke.CLOUDTRAIL_KEY: "EnableIAMUserPermissions",
    smoke.FRONTEND_KEY: "EnableIAMUserPermissions",
}


# ---------------------------------------------------------------------------
# Designed documents, built from the literals above.
# ---------------------------------------------------------------------------


def _deny(
    sid: str, actions: list[str], resources: Any, allowed: list[str], exempt: str | None = None
) -> dict[str, Any]:
    condition: dict[str, Any] = {"ArnNotEquals": {"aws:PrincipalArn": sorted(allowed)}}
    if exempt:
        condition[exempt] = {"aws:PrincipalIsAWSService": "false"}
    return {
        "Sid": sid,
        "Effect": "Deny",
        "Principal": {"AWS": "*"},
        "Action": sorted(actions),
        "Resource": resources,
        "Condition": condition,
    }


def _retained(sid: str) -> dict[str, Any]:
    return {
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"Service": "logs.amazonaws.com"},
        "Action": "kms:Decrypt",
        "Resource": "*",
    }


_RETAINED = {
    smoke.TFSTATE_KEY: [],
    smoke.EVAL_ARTIFACTS_KEY: [],
    smoke.LOGS_KEY: ["AllowCloudWatchLogsUseOfTheKey"],
    smoke.CLOUDTRAIL_KEY: [
        "AllowCloudTrailToEncryptLogs",
        "AllowCloudTrailToDescribeKey",
        "AllowPrincipalsInAccountToDecryptLogFiles",
    ],
    smoke.FRONTEND_KEY: ["AllowCloudFrontOACDecrypt"],
    smoke.STATE_BUCKET: ["DenyInsecureTransport"],
    smoke.STATE_LOG_BUCKET: ["DenyInsecureTransport", "AllowStateBucketAccessLogDelivery"],
    smoke.EVAL_ARTIFACTS_BUCKET: ["DenyInsecureTransport", "DenyUnencryptedObjectUploads"],
    smoke.CLOUDTRAIL_BUCKET: [
        "DenyInsecureTransport",
        "AWSCloudTrailAclCheck",
        "AWSCloudTrailWrite",
    ],
    smoke.FRONTEND_ORIGIN_BUCKET: ["DenyInsecureTransport", "AllowCloudFrontOACRead"],
    smoke.LOCK_TABLE: [],
}


def _users(store: str) -> list[str]:
    if store in (smoke.EVAL_ARTIFACTS_KEY, smoke.EVAL_ARTIFACTS_BUCKET):
        return [*_EVAL_ROLES.values(), _OPERATOR_ARN]
    if store == smoke.LOGS_KEY:
        return [_ACCOUNTS_GET_ROLE, _RELAY_ROLE, _OPERATOR_ARN]
    return [_OPERATOR_ARN]


def _designed(store: str) -> list[dict[str, Any]]:
    admins = [_OPERATOR_ARN, _ROOT_ARN]
    if store in _KEY_ARNS:
        return [
            {
                "Sid": _ROOT_SIDS[store],
                "Effect": "Allow",
                "Principal": {"AWS": _ROOT_ARN},
                "Action": "kms:*",
                "Resource": "*",
            },
            _deny(
                "DenyKeyUseToNonProjectPrincipals",
                _DESIGN_KEY_USE,
                "*",
                _users(store),
                exempt="Bool",
            ),
            _deny("DenyKeyAdministrationToNonOperators", _DESIGN_STORE_KEY_ADMIN, "*", admins),
        ]
    if store == smoke.LOCK_TABLE:
        resources = [_LOCK_TABLE_ARN, f"{_LOCK_TABLE_ARN}/index/*"]
        from scripts.smoke.dynamodb_principal_deny import (
            TABLE_ADMIN_ACTIONS,
            TABLE_DATA_PLANE_ACTIONS,
        )

        return [
            _deny(
                "DenyDataPlaneToNonProjectPrincipals",
                list(TABLE_DATA_PLANE_ACTIONS),
                resources,
                [_OPERATOR_ARN],
            ),
            _deny(
                "DenyTableAdministrationToNonOperators",
                list(TABLE_ADMIN_ACTIONS),
                resources,
                admins,
            ),
        ]
    arn = f"arn:aws:s3:::{_BUCKETS[store]}"
    admin = _deny("DenyBucketAdministrationToNonOperators", _DESIGN_S3_ADMIN, arn, admins)
    if store == smoke.FRONTEND_ORIGIN_BUCKET:
        return [
            _deny(
                "DenyObjectWritesToNonOperators",
                _DESIGN_S3_WRITE,
                [arn, f"{arn}/*"],
                [_OPERATOR_ARN],
            ),
            admin,
        ]
    return [
        _deny(
            "DenyObjectDataPlaneToNonProjectPrincipals",
            _DESIGN_S3_READ + _DESIGN_S3_WRITE,
            [f"{arn}/*", arn],
            _users(store),
            exempt=(
                "BoolIfExists"
                if store in (smoke.STATE_LOG_BUCKET, smoke.CLOUDTRAIL_BUCKET)
                else None
            ),
        ),
        admin,
    ]


def _document(statements: list[dict[str, Any]]) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def _post_g_policy(store: str) -> str:
    return _document([*(_retained(s) for s in _RETAINED[store]), *_designed(store)])


def _pre_g_policy(store: str) -> str | None:
    """The documents measured before Group G: retained statements only; no lock-table policy."""
    if store == smoke.LOCK_TABLE:
        return None
    if store == smoke.EVAL_ARTIFACTS_KEY:
        return _document(
            [
                {
                    "Sid": "Enable IAM User Permissions",
                    "Effect": "Allow",
                    "Principal": {"AWS": _ROOT_ARN},
                    "Action": "kms:*",
                    "Resource": "*",
                }
            ]
        )
    statements = [_retained(s) for s in _RETAINED[store]]
    if store in _KEY_ARNS:
        statements.insert(0, _designed(store)[0])
    return _document(statements)


# ---------------------------------------------------------------------------
# The fake account behind real boto3 clients.
# ---------------------------------------------------------------------------


class Err(NamedTuple):
    """A service error: code, message and HTTP status."""

    code: str
    message: str
    status: int = 400


def _explicit_deny(action: str, resource: str, principal: str = _PROBE_ROLE) -> Err:
    return Err(
        "AccessDeniedException" if not action.startswith("s3:") else "AccessDenied",
        f"User: arn:aws:sts::{_ACCOUNT}:assumed-role/{principal}/{principal} is not authorized to "
        f"perform: {action} on resource: {resource} with an explicit deny in a resource-based policy",
        403,
    )


def _implicit_deny(action: str, resource: str) -> Err:
    return Err(
        "AccessDenied",
        f"User: arn:aws:sts::{_ACCOUNT}:assumed-role/{_PROBE_ROLE}/{_PROBE_ROLE} is not authorized "
        f"to perform: {action} on resource: {resource} because no identity-based policy allows the "
        f"{action} action",
        403,
    )


_PROBE_ACTIONS = {
    smoke.PROBE_GENERATE_DATA_KEY: "kms:GenerateDataKey",
    smoke.PROBE_DECRYPT: "kms:Decrypt",
    smoke.PROBE_REVOKE_GRANT: "kms:RevokeGrant",
    smoke.PROBE_GET_OBJECT: "s3:GetObject",
    smoke.PROBE_LIST_OBJECTS: "s3:ListBucket",
    smoke.PROBE_PUT_OBJECT: "s3:PutObject",
    smoke.PROBE_GET_ITEM: "dynamodb:GetItem",
    smoke.PROBE_PUT_ITEM: "dynamodb:PutItem",
    smoke.PROBE_PUT_RESOURCE_POLICY: "dynamodb:PutResourcePolicy",
}
_PRE_G_OUTCOMES: dict[str, Err | None] = {
    smoke.PROBE_GENERATE_DATA_KEY: None,
    smoke.PROBE_DECRYPT: None,
    smoke.PROBE_REVOKE_GRANT: Err("NotFoundException", "Grant ID not found", 400),
    smoke.PROBE_GET_OBJECT: Err("NoSuchKey", "The specified key does not exist.", 404),
    smoke.PROBE_LIST_OBJECTS: None,
    smoke.PROBE_PUT_OBJECT: Err("NoSuchKey", "The specified key does not exist.", 404),
    smoke.PROBE_GET_ITEM: None,
    smoke.PROBE_PUT_ITEM: Err(
        "ConditionalCheckFailedException", "The conditional request failed", 400
    ),
    smoke.PROBE_PUT_RESOURCE_POLICY: Err(
        "PolicyNotFoundException", "Resource-based policy not found", 400
    ),
}

_ALL_STORES = list(smoke.STORES)
_STORE_RESOURCE = {
    **_KEY_ARNS,
    smoke.DYNAMODB_KEY: _DYNAMODB_KEY_ARN,
    **_BUCKETS,
    smoke.LOCK_TABLE: _LOCK_TABLE,
}


@dataclass
class FakeAccount:
    """The deployed account's state, and every call made against it."""

    policies: dict[str, str | None] = field(
        default_factory=lambda: {s: _post_g_policy(s) for s in _ALL_STORES}
    )
    outcomes: dict[tuple[str, str], Err | None] = field(default_factory=dict)
    simulated: dict[str, dict[str, str]] = field(default_factory=dict)
    logs_outcome: Err | None = field(
        default_factory=lambda: _explicit_deny("logs:FilterLogEvents", _LOG_GROUP_ARN)
    )
    trail_statuses: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "IsLogging": True,
                "LatestDeliveryTime": _NOW - timedelta(minutes=3),
                "LatestCloudWatchLogsDeliveryTime": _NOW - timedelta(minutes=2),
            }
        ]
    )
    trail_logs_group: str | None = (
        f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:/aws/cloudtrail/t:*"
    )
    flow_logs: list[dict[str, Any]] = field(
        default_factory=lambda: [{"FlowLogId": "fl-1", "DeliverLogsStatus": "SUCCESS"}]
    )
    key_changed_at: datetime | None = _NOW - timedelta(hours=2)
    key_events: list[dict[str, Any]] | None = None
    key_events_paged: bool = False
    decrypt_events: list[dict[str, Any]] = field(default_factory=list)
    decrypt_events_paged: bool = False
    ingested: set[str] = field(default_factory=lambda: set(_INGESTION_GROUPS.values()))
    ingested_after: dict[str, int] = field(default_factory=dict)
    functions: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "FunctionName": "crm-agent-eval-accounts-get",
                "Role": _ACCOUNTS_GET_ROLE,
                "KMSKeyArn": _KEY_ARNS[smoke.LOGS_KEY],
            },
            {
                "FunctionName": "crm-agent-eval-agent-relay",
                "Role": _RELAY_ROLE,
                "KMSKeyArn": _KEY_ARNS[smoke.LOGS_KEY],
            },
            {
                "FunctionName": "crm-agent-eval-unencrypted",
                "Role": f"arn:aws:iam::{_ACCOUNT}:role/u",
            },
            {
                "FunctionName": "co-tenant-fn",
                "Role": f"arn:aws:iam::{_ACCOUNT}:role/x",
                "KMSKeyArn": _KEY_ARNS[smoke.LOGS_KEY],
            },
        ]
    )
    eval_roles: dict[str, str] = field(default_factory=lambda: dict(_EVAL_ROLES))
    manifests: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "imageTag=abc123": {
                "schemaVersion": 2,
                "config": {
                    "digest": _CONFIG_DIGEST,
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                },
                "layers": [],
            }
        }
    )
    operator_errors: dict[tuple[str, str], Err] = field(default_factory=dict)
    create_role_error: Err | None = None
    delete_role_error: Err | None = None
    calls: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)

    def calls_by(self, who: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [(s, o, p) for w, s, o, p in self.calls if w == who]

    # -- dispatch ---------------------------------------------------------

    def respond(self, who: str, service: str, operation: str, params: dict[str, Any]) -> Any:
        self.calls.append((who, service, operation, params))
        handler = getattr(self, f"{who}_{service.replace('-', '_')}_{xform_name(operation)}", None)
        if handler is None:
            raise AssertionError(f"unexpected {who} call {service}:{operation} {params}")
        return handler(params)

    def _operator_error(self, store: str, operation: str) -> Err | None:
        return self.operator_errors.get((store, operation))

    # -- operator ---------------------------------------------------------

    def operator_sts_get_caller_identity(self, _p: dict[str, Any]) -> Any:
        return {
            "UserId": "operator-user-id:sess",
            "Account": _ACCOUNT,
            "Arn": f"arn:aws:sts::{_ACCOUNT}:assumed-role/{_OPERATOR_ROLE}/sess",
        }

    def operator_iam_get_role(self, p: dict[str, Any]) -> Any:
        name = p["RoleName"]
        arns = {_OPERATOR_ROLE: _OPERATOR_ARN, **self.eval_roles}
        if name not in arns:
            return Err("NoSuchEntity", f"The role with name {name} cannot be found.", 404)
        return _role(name, arns[name])

    def operator_kms_describe_key(self, p: dict[str, Any]) -> Any:
        key = p["KeyId"]
        for arn in _KEY_ARNS.values():
            if key in (arn, arn.rpartition("/")[2]):
                return {"KeyMetadata": {"KeyId": arn.rpartition("/")[2], "Arn": arn}}
        return Err("NotFoundException", f"Key '{key}' does not exist", 400)

    def _store_of(self, p: dict[str, Any]) -> str:
        for name in ("Bucket", "KeyId", "TableName", "ResourceArn"):
            if name in p:
                value = p[name]
                for store, resource in _STORE_RESOURCE.items():
                    if value == resource or (
                        store == smoke.LOCK_TABLE and value == _LOCK_TABLE_ARN
                    ):
                        return store
        raise AssertionError(f"no store for {p}")

    def operator_s3_get_bucket_encryption(self, p: dict[str, Any]) -> Any:
        store = self._store_of(p)
        key = _BUCKET_KEYS[store]
        default = (
            {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": _KEY_ARNS[key]}
            if key
            else {"SSEAlgorithm": "AES256"}
        )
        return {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": default, "BucketKeyEnabled": True}]
            }
        }

    def operator_s3_get_bucket_logging(self, p: dict[str, Any]) -> Any:
        assert p["Bucket"] == _BUCKETS[smoke.STATE_BUCKET]
        return {
            "LoggingEnabled": {
                "TargetBucket": _BUCKETS[smoke.STATE_LOG_BUCKET],
                "TargetPrefix": "state-access/",
            }
        }

    def operator_dynamodb_describe_table(self, p: dict[str, Any]) -> Any:
        if p["TableName"] == _CRM_TABLE:
            return {
                "Table": {
                    "TableName": _CRM_TABLE,
                    "SSEDescription": {"Status": "ENABLED", "KMSMasterKeyArn": _DYNAMODB_KEY_ARN},
                }
            }
        assert p["TableName"] == _LOCK_TABLE
        return {"Table": {"TableName": _LOCK_TABLE, "TableArn": _LOCK_TABLE_ARN}}

    def operator_ec2_describe_flow_logs(self, p: dict[str, Any]) -> Any:
        assert p["Filters"] == [{"Name": "resource-id", "Values": [_VPC_ID]}]
        return {"FlowLogs": self.flow_logs}

    def _key_event_list(self) -> list[dict[str, Any]]:
        if self.key_events is not None:
            return self.key_events
        roles = [
            f["Role"] for f in self.functions if f.get("KMSKeyArn") == _KEY_ARNS[smoke.LOGS_KEY]
        ]
        return [_decrypt_event(role) for role in roles] + [_decrypt_event(_OPERATOR_ARN)]

    def operator_cloudtrail_lookup_events(self, p: dict[str, Any]) -> Any:
        (attribute,) = p["LookupAttributes"]
        if attribute == {"AttributeKey": "EventName", "AttributeValue": "PutKeyPolicy"}:
            assert p["StartTime"] == _NOW - timedelta(days=90)
            events = [
                {
                    "EventName": "PutKeyPolicy",
                    "EventTime": _NOW - timedelta(days=30),
                    "Resources": [{"ResourceName": _KEY_ARNS[smoke.FRONTEND_KEY]}],
                }
            ]
            if self.key_changed_at is not None:
                events.append(
                    {
                        "EventName": "PutKeyPolicy",
                        "EventTime": self.key_changed_at,
                        "Resources": [
                            {"ResourceName": _KEY_ARNS[smoke.LOGS_KEY].rpartition("/")[2]}
                        ],
                    }
                )
            return {"Events": events}
        if attribute == {"AttributeKey": "EventName", "AttributeValue": "Decrypt"}:
            assert p["StartTime"] == self.key_changed_at
            if self.decrypt_events_paged and "NextToken" not in p:
                return {"Events": [], "NextToken": "page-2"}
            return {"Events": self.decrypt_events}
        assert attribute == {
            "AttributeKey": "ResourceName",
            "AttributeValue": _KEY_ARNS[smoke.LOGS_KEY],
        }
        assert p["StartTime"] == self.key_changed_at
        if self.key_events_paged and "NextToken" not in p:
            return {"Events": [], "NextToken": "page-2"}
        return {"Events": self._key_event_list()}

    def operator_logs_filter_log_events(self, p: dict[str, Any]) -> Any:
        group = p["logGroupName"]
        assert group in _INGESTION_GROUPS.values()
        assert self.key_changed_at is not None
        assert p["startTime"] == int(self.key_changed_at.timestamp() * 1000)
        remaining = self.ingested_after.get(group, 0)
        if remaining:
            self.ingested_after[group] = remaining - 1
            return {"events": []}
        if group not in self.ingested:
            return {"events": []}
        return {"events": [{"logStreamName": "s", "timestamp": 1, "message": "m"}]}

    def operator_logs_describe_log_groups(self, p: dict[str, Any]) -> Any:
        assert p["logGroupNamePrefix"] == "/aws/lambda/crm-agent-eval-"
        return {
            "logGroups": [
                {"logGroupName": "/aws/lambda/crm-agent-eval-a-plain", "arn": "x"},
                {
                    "logGroupName": _LOG_GROUP,
                    "arn": _LOG_GROUP_ARN,
                    "kmsKeyId": _KEY_ARNS[smoke.LOGS_KEY],
                },
            ]
        }

    def operator_lambda_list_functions(self, _p: dict[str, Any]) -> Any:
        return {"Functions": self.functions}

    def _policy(self, p: dict[str, Any], missing: Err) -> Any:
        store = self._store_of(p)
        text = self.policies[store]
        return missing if text is None else {"Policy": text}

    def operator_kms_get_key_policy(self, p: dict[str, Any]) -> Any:
        assert p["PolicyName"] == "default"
        return {**self._policy(p, Err("NotFoundException", "no", 400)), "PolicyName": "default"}

    def operator_s3_get_bucket_policy(self, p: dict[str, Any]) -> Any:
        return self._policy(p, Err("NoSuchBucketPolicy", "The bucket policy does not exist", 404))

    def operator_dynamodb_get_resource_policy(self, p: dict[str, Any]) -> Any:
        result = self._policy(
            p, Err("PolicyNotFoundException", "Resource-based policy not found", 400)
        )
        return result if isinstance(result, Err) else {**result, "RevisionId": "1792550371863"}

    def operator_s3_get_object(self, p: dict[str, Any]) -> Any:
        error = self._operator_error(self._store_of(p), "GetObject")
        return error or Err("NoSuchKey", "The specified key does not exist.", 404)

    def operator_dynamodb_get_item(self, p: dict[str, Any]) -> Any:
        return self._operator_error(smoke.LOCK_TABLE, "GetItem") or {}

    def operator_bedrock_agentcore_control_get_agent_runtime(self, p: dict[str, Any]) -> Any:
        assert p["agentRuntimeId"] == "crm_agent_eval-AbCdEf"
        return {
            "agentRuntimeArn": _RUNTIME_ARN,
            "agentRuntimeName": "crm_agent_eval",
            "agentRuntimeId": "crm_agent_eval-AbCdEf",
            "agentRuntimeVersion": "3",
            "createdAt": _NOW,
            "lastUpdatedAt": _NOW,
            "roleArn": f"arn:aws:iam::{_ACCOUNT}:role/runtime",
            "status": "READY",
            "lifecycleConfiguration": {},
            "agentRuntimeArtifact": {
                "containerConfiguration": {
                    "containerUri": f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/{_REPOSITORY}:abc123"
                }
            },
        }

    def probe_ecr_batch_get_image(self, p: dict[str, Any]) -> Any:
        assert p["repositoryName"] == _REPOSITORY
        (image_id,) = p["imageIds"]
        key = ",".join(f"{k}={v}" for k, v in sorted(image_id.items()))
        manifest = self.manifests[key]
        return {"images": [{"imageId": image_id, "imageManifest": json.dumps(manifest)}]}

    def probe_ecr_get_download_url_for_layer(self, p: dict[str, Any]) -> Any:
        assert p == {"repositoryName": _REPOSITORY, "layerDigest": _CONFIG_DIGEST}
        return {
            "downloadUrl": f"https://prod-starport-layer-bucket.s3.amazonaws.com/{_ACCOUNT}/x?X-Amz-Signature=sig",
            "layerDigest": _CONFIG_DIGEST,
        }

    def operator_kms_generate_data_key_without_plaintext(self, p: dict[str, Any]) -> Any:
        return {"CiphertextBlob": f"cipher-of-{self._store_of(p)}".encode(), "KeyId": p["KeyId"]}

    def operator_iam_create_role(self, p: dict[str, Any]) -> Any:
        if self.create_role_error:
            return self.create_role_error
        return _role(p["RoleName"], _PROBE_ROLE_ARN)

    def operator_iam_put_role_policy(self, _p: dict[str, Any]) -> Any:
        return {}

    def operator_sts_assume_role(self, p: dict[str, Any]) -> Any:
        return {
            "Credentials": {
                "AccessKeyId": "test-probe-access-key-id",
                "SecretAccessKey": "probe-secret",
                "SessionToken": "probe-token",
                "Expiration": _NOW + timedelta(minutes=15),
            },
            "AssumedRoleUser": {
                "AssumedRoleId": "probe-role-id:probe",
                "Arn": f"arn:aws:sts::{_ACCOUNT}:assumed-role/{_PROBE_ROLE}/{_PROBE_ROLE}",
            },
        }

    def operator_iam_simulate_principal_policy(self, p: dict[str, Any]) -> Any:
        store = next(s for s, b in _BUCKETS.items() if p["ResourceArns"] == [f"arn:aws:s3:::{b}"])
        principal = p["PolicySourceArn"]
        assert p["ContextEntries"][0] == {
            "ContextKeyName": "aws:PrincipalArn",
            "ContextKeyValues": [principal],
            "ContextKeyType": "string",
        }
        default = "explicitDeny" if principal == _PROBE_ROLE_ARN else "allowed"
        decisions = self.simulated.get(f"{store}:{principal}", {})
        return {
            "EvaluationResults": [
                {
                    "EvalActionName": action,
                    "EvalResourceName": p["ResourceArns"][0],
                    "EvalDecision": decisions.get(action, default),
                }
                for action in p["ActionNames"]
            ],
            "IsTruncated": False,
        }

    def operator_iam_delete_role_policy(self, _p: dict[str, Any]) -> Any:
        return {}

    def operator_iam_delete_role(self, _p: dict[str, Any]) -> Any:
        return self.delete_role_error or {}

    def operator_cloudtrail_describe_trails(self, _p: dict[str, Any]) -> Any:
        return {
            "trailList": [
                {
                    "Name": "crm-agent-eval-trail",
                    "TrailARN": f"arn:aws:cloudtrail:{_REGION}:{_ACCOUNT}:trail/crm-agent-eval-trail",
                    "S3BucketName": _BUCKETS[smoke.CLOUDTRAIL_BUCKET],
                    **(
                        {"CloudWatchLogsLogGroupArn": self.trail_logs_group}
                        if self.trail_logs_group
                        else {}
                    ),
                }
            ]
        }

    def operator_cloudtrail_get_trail_status(self, _p: dict[str, Any]) -> Any:
        if len(self.trail_statuses) > 1:
            return self.trail_statuses.pop(0)
        return self.trail_statuses[0]

    # -- probe ------------------------------------------------------------

    def _probe(self, p: dict[str, Any], call: str, success: Any) -> Any:
        store = self._store_of(p)
        default = _explicit_deny(_PROBE_ACTIONS[call], _STORE_RESOURCE[store])
        outcome = self.outcomes.get((store, call), default)
        return success if outcome is None else outcome

    def probe_kms_generate_data_key(self, p: dict[str, Any]) -> Any:
        return self._probe(
            p,
            smoke.PROBE_GENERATE_DATA_KEY,
            {"CiphertextBlob": b"c", "Plaintext": b"PLAINTEXT-KEY-MATERIAL", "KeyId": p["KeyId"]},
        )

    def probe_kms_decrypt(self, p: dict[str, Any]) -> Any:
        assert p["CiphertextBlob"] == f"cipher-of-{self._store_of(p)}".encode()
        return self._probe(
            p, smoke.PROBE_DECRYPT, {"Plaintext": b"PLAINTEXT-KEY-MATERIAL", "KeyId": p["KeyId"]}
        )

    def probe_kms_revoke_grant(self, p: dict[str, Any]) -> Any:
        return self._probe(p, smoke.PROBE_REVOKE_GRANT, {})

    def probe_s3_get_object(self, p: dict[str, Any]) -> Any:
        return self._probe(p, smoke.PROBE_GET_OBJECT, {})

    def probe_s3_list_objects_v2(self, p: dict[str, Any]) -> Any:
        return self._probe(p, smoke.PROBE_LIST_OBJECTS, {"KeyCount": 0})

    def probe_s3_put_object(self, p: dict[str, Any]) -> Any:
        return self._probe(p, smoke.PROBE_PUT_OBJECT, {"ETag": '"e"'})

    def probe_dynamodb_get_item(self, p: dict[str, Any]) -> Any:
        return self._probe(p, smoke.PROBE_GET_ITEM, {})

    def probe_dynamodb_put_item(self, p: dict[str, Any]) -> Any:
        return self._probe(p, smoke.PROBE_PUT_ITEM, {})

    def probe_dynamodb_put_resource_policy(self, p: dict[str, Any]) -> Any:
        return self._probe(p, smoke.PROBE_PUT_RESOURCE_POLICY, {"RevisionId": "2"})

    def probe_logs_filter_log_events(self, p: dict[str, Any]) -> Any:
        assert p == {"logGroupName": _LOG_GROUP, "limit": 1}
        return self.logs_outcome or {"events": []}


def _decrypt_event(
    role_arn: str, error: str | None = None, name: str = "Decrypt"
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "eventName": name,
        "userIdentity": {
            "type": "AssumedRole",
            "arn": f"arn:aws:sts::{_ACCOUNT}:assumed-role/{role_arn.rpartition('/')[2]}/s",
            "sessionContext": {"sessionIssuer": {"type": "Role", "arn": role_arn}},
        },
    }
    if error:
        detail["errorCode"] = error
    return {
        "EventName": name,
        "EventTime": _NOW - timedelta(minutes=30),
        "CloudTrailEvent": json.dumps(detail),
    }


def _function_decrypt_event(
    role_arn: str,
    function: str,
    event_id: str,
    *,
    key_arn: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """A `LookupEvents` record of a Lambda cold-start env-var `Decrypt` under the function role."""
    key = key_arn or _KEY_ARNS[smoke.LOGS_KEY]
    function_arn = f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:{function}"
    detail: dict[str, Any] = {
        "eventVersion": "1.10",
        "userIdentity": {
            "type": "AssumedRole",
            "principalId": f"AROAEXAMPLE:{function}",
            "arn": f"arn:aws:sts::{_ACCOUNT}:assumed-role/{role_arn.rpartition('/')[2]}/{function}",
            "accountId": _ACCOUNT,
            "sessionContext": {
                "sessionIssuer": {
                    "type": "Role",
                    "principalId": "AROAEXAMPLE",
                    "arn": role_arn,
                    "accountId": _ACCOUNT,
                    "userName": role_arn.rpartition("/")[2],
                },
                "attributes": {"creationDate": "2026-10-22T05:40:00Z", "mfaAuthenticated": "false"},
            },
        },
        "eventTime": "2026-10-22T05:41:00Z",
        "eventSource": "kms.amazonaws.com",
        "eventName": "Decrypt",
        "awsRegion": _REGION,
        "sourceIPAddress": "3.0.0.1",
        "userAgent": "aws-sdk-go/1.0",
        "requestParameters": {
            "encryptionContext": {"aws:lambda:FunctionArn": function_arn},
            "encryptionAlgorithm": "SYMMETRIC_DEFAULT",
        },
        "responseElements": None,
        "requestID": f"req-{event_id}",
        "eventID": event_id,
        "readOnly": True,
        "resources": [{"accountId": _ACCOUNT, "type": "AWS::KMS::Key", "ARN": key}],
        "eventType": "AwsApiCall",
        "managementEvent": True,
        "recipientAccountId": _ACCOUNT,
        "eventCategory": "Management",
    }
    if error:
        detail["errorCode"] = error
        detail["errorMessage"] = "User is not authorized to perform: kms:Decrypt"
    return {
        "EventId": event_id,
        "EventName": "Decrypt",
        "ReadOnly": "true",
        "EventTime": _NOW - timedelta(minutes=30),
        "EventSource": "kms.amazonaws.com",
        "Username": function,
        "Resources": [],
        "CloudTrailEvent": json.dumps(detail),
    }


def _role(name: str, arn: str) -> dict[str, Any]:
    return {
        "Role": {
            "Path": "/",
            "RoleName": name,
            "RoleId": "test-role-id-00001",
            "Arn": arn,
            "CreateDate": _NOW,
        }
    }


def _client(account: FakeAccount, service: str, who: str) -> Any:
    """A real boto3 client whose requests `account` answers before any network I/O."""
    client = boto3.client(
        service,
        region_name=_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",  # nosec B106  # fake moto/stub credentials, not a credential
    )

    def capture(params: dict[str, Any], context: dict[str, Any], **_: Any) -> None:
        context["fake_params"] = copy.deepcopy(params)

    def answer(model: Any, context: dict[str, Any], **_: Any) -> Any:
        result = account.respond(who, service, model.name, context["fake_params"])
        if isinstance(result, Err):
            parsed = {
                "ResponseMetadata": {"HTTPStatusCode": result.status},
                "Error": {"Code": result.code, "Message": result.message},
            }
            return AWSResponse(None, result.status, {}, None), parsed
        if model.output_shape is not None:
            validate_parameters(result, model.output_shape)
        return AWSResponse(None, 200, {}, None), {
            **result,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def no_account_id(builtins: dict[str, Any], **_: Any) -> None:
        if "AWS::Auth::AccountId" in builtins:
            builtins["AWS::Auth::AccountId"] = None

    client.meta.events.register_first("before-parameter-build.*.*", capture)
    client.meta.events.register("before-call.*.*", answer)
    client.meta.events.register("before-endpoint-resolution.*", no_account_id)
    return client


_OPERATOR_SERVICES = (
    "sts",
    "iam",
    "kms",
    "s3",
    "dynamodb",
    "logs",
    "lambda",
    "ecr",
    "bedrock-agentcore-control",
    "cloudtrail",
    "ec2",
)


@dataclass
class Run:
    rows: list[CheckResult]
    account: FakeAccount
    fetched: list[str]
    slept: list[float]

    def by_check(self) -> dict[str, CheckResult]:
        by: dict[str, CheckResult] = {}
        for row in self.rows:
            assert row.check not in by, f"duplicate row {row.check}"
            by[row.check] = row
        return by


def _backend(tmp_path: Path) -> Path:
    path = tmp_path / "backend.hcl"
    path.write_text(
        "# Generated by infra/bootstrap_state_backend.sh\n"
        f'bucket         = "{_BUCKETS[smoke.STATE_BUCKET]}"\n'
        'key            = "crm-agent-eval/terraform.tfstate"\n'
        f'region         = "{_REGION}"\n'
        f'dynamodb_table = "{_LOCK_TABLE}"\n'
        f'kms_key_id     = "{_KEY_ARNS[smoke.TFSTATE_KEY].rpartition("/")[2]}"\n'
        "encrypt        = true\n",
        encoding="utf-8",
    )
    return path


def _run(
    tmp_path: Path,
    account: FakeAccount | None = None,
    *,
    outputs: dict[str, Any] | None = None,
    backend: Path | None = None,
    layer_status: int = 200,
    probe_factory: Callable[..., Any] | None = None,
    clock: Callable[[], datetime] = lambda: _NOW,
    delivery_poll_seconds: float = 0.0,
    ingestion_poll_seconds: float = 0.0,
) -> Run:
    account = account or FakeAccount()
    clients = {s: _client(account, s, "operator") for s in _OPERATOR_SERVICES}
    probe_clients = {
        s: _client(account, s, "probe") for s in ("kms", "s3", "dynamodb", "logs", "ecr")
    }
    fetched: list[str] = []
    slept: list[float] = []

    def fetch(url: str) -> int:
        fetched.append(url)
        return layer_status

    ctx = context_for_test(outputs=_OUTPUTS if outputs is None else outputs, clients=clients)
    rows = list(
        smoke.run_checks(
            ctx,
            probe_client_factory=probe_factory
            or (lambda _ctx, service, _creds: probe_clients[service]),
            sleeper=slept.append,
            clock=clock,
            layer_fetcher=fetch,
            backend_config_path=backend if backend is not None else _backend(tmp_path),
            run_id=_RUN_ID,
            delivery_poll_seconds=delivery_poll_seconds,
            ingestion_poll_seconds=ingestion_poll_seconds,
        )
    )
    return Run(rows=rows, account=account, fetched=fetched, slept=slept)


def _expected_checks() -> set[str]:
    checks = {f"{s}_policy_shape" for s in smoke.STORES}
    checks |= {f"operator_admitted:{s}" for s in (*smoke.BUCKET_STORES, smoke.LOCK_TABLE)}
    checks |= {f"probe_denied:{s}:{c}" for s in smoke.PROBED_STORES for c in smoke.probe_calls(s)}
    checks |= {f"log_ingestion_current:{w}" for w in smoke.INGESTION_LOGS}
    checks |= {f"admin_simulated_denied:{s}" for s in smoke.BUCKET_STORES}
    checks |= {
        "state_operator_list_matches_stack",
        "ecr_layer_fetch",
        "logs_probe_denied",
        "probe_role_deleted",
        "cloudtrail_delivery_current",
        "cloudtrail_logs_delivery_current",
        "flow_log_delivery",
        "logs_key_access_denied_events",
        "logs_key_decrypt_per_function",
    }
    return checks


def _not_passing(run: Run) -> dict[str, str]:
    return {r.check: r.status for r in run.rows if r.status != "pass"}


# ---------------------------------------------------------------------------
# Constants and shape.
# ---------------------------------------------------------------------------


def test_s3_tuples_and_sids_are_the_design_constants_in_order() -> None:
    assert list(smoke.S3_OBJECT_READ_ACTIONS) == _DESIGN_S3_READ
    assert list(smoke.S3_OBJECT_WRITE_ACTIONS) == _DESIGN_S3_WRITE
    assert list(smoke.S3_BUCKET_ADMIN_ACTIONS) == _DESIGN_S3_ADMIN
    assert list(smoke.KEY_USE_ACTIONS) == _DESIGN_KEY_USE
    assert list(smoke.KEY_ADMIN_ACTIONS) == _DESIGN_KEY_ADMIN
    assert (smoke.S3_DATA_PLANE_SID, smoke.S3_WRITE_SID, smoke.S3_ADMIN_SID) == (
        "DenyObjectDataPlaneToNonProjectPrincipals",
        "DenyObjectWritesToNonOperators",
        "DenyBucketAdministrationToNonOperators",
    )
    assert dict(smoke.KEY_ROOT_SIDS) == _ROOT_SIDS
    assert list(smoke.STORE_KEY_ADMIN_ACTIONS) == _DESIGN_STORE_KEY_ADMIN
    assert (smoke.KEY_EXEMPTION_OPERATOR, smoke.BUCKET_EXEMPTION_OPERATOR) == (
        "Bool",
        "BoolIfExists",
    )
    assert {smoke.STATE_LOG_BUCKET, smoke.CLOUDTRAIL_BUCKET} == smoke.SERVICE_EXEMPT_BUCKETS


def test_compliant_account_passes_every_row_and_meets_the_coverage_floor(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert _not_passing(run) == {}
    assert {r.check for r in run.rows} == _expected_checks()
    assert len(run.rows) == smoke.expected_check_count(context_for_test()).minimum == 66


def test_pre_group_g_account_fails_every_probe_and_shape_row(tmp_path: Path) -> None:
    account = FakeAccount(
        policies={s: _pre_g_policy(s) for s in _ALL_STORES},
        outcomes={(s, c): _PRE_G_OUTCOMES[c] for s in smoke.STORES for c in smoke.probe_calls(s)},
        simulated={
            f"{s}:{_PROBE_ROLE_ARN}": dict.fromkeys(_DESIGN_S3_ADMIN, "implicitDeny")
            for s in smoke.BUCKET_STORES
        },
        logs_outcome=None,
    )
    by = _run(tmp_path, account).by_check()
    moving = [
        c
        for c in by
        if (
            c.startswith(("probe_denied:", "admin_simulated_denied:"))
            and not c.startswith("probe_denied:dynamodb_key:")
        )
        or c.endswith("_policy_shape")
        or c == "state_operator_list_matches_stack"
    ]
    assert len(moving) == 11 + 31 + 5 + 1
    assert by["probe_denied:dynamodb_key:kms_revoke_grant_absent"].status == "pass"
    assert {c: by[c].status for c in moving} == dict.fromkeys(moving, "fail")
    logs = by["logs_probe_denied"]
    assert (logs.status, logs.defer_reason) == (DEFER, "logs_read_not_key_gated")
    assert "PolicyNotFoundException" in by["lock_table_policy_shape"].detail
    for check in ("ecr_layer_fetch", "cloudtrail_delivery_current", "probe_role_deleted"):
        assert by[check].status == "pass"
    assert by["operator_admitted:frontend_origin_bucket"].status == "pass"


@pytest.mark.parametrize(
    ("store", "mutate", "expected_text"),
    [
        (
            smoke.LOGS_KEY,
            lambda st: st[2]["Condition"].pop("Bool"),
            "Condition",
        ),
        (
            smoke.EVAL_ARTIFACTS_BUCKET,
            lambda st: st[2]["Condition"].update(Bool={"aws:PrincipalIsAWSService": "false"}),
            "Condition",
        ),
        (
            smoke.CLOUDTRAIL_KEY,
            lambda st: st[4]["Condition"].update(
                ArnNotEqualsIfExists=st[4]["Condition"].pop("ArnNotEquals")
            ),
            "Condition",
        ),
        (
            smoke.FRONTEND_KEY,
            lambda st: st[2]["Condition"].update(
                StringNotEquals=st[2]["Condition"].pop("ArnNotEquals")
            ),
            "Condition",
        ),
        (
            smoke.STATE_BUCKET,
            lambda st: st[2]["Action"].remove("s3:PutBucketPolicy"),
            "Action missing ['s3:PutBucketPolicy']",
        ),
        (
            smoke.FRONTEND_ORIGIN_BUCKET,
            lambda st: st.append({**st[2], "Sid": "AllowEverything", "Effect": "Allow"}),
            "unexpected statement sid(s): ['AllowEverything']",
        ),
        (
            smoke.EVAL_ARTIFACTS_KEY,
            lambda st: st[0].update(Sid="EnableIAMUserPermissions"),
            "Enable IAM User Permissions: absent",
        ),
        (
            smoke.LOCK_TABLE,
            lambda st: st[0].update(Principal={"AWS": _OPERATOR_ARN}),
            "Principal",
        ),
        (
            smoke.STATE_LOG_BUCKET,
            lambda st: st[2].update(Resource=f"arn:aws:s3:::{_BUCKETS[smoke.STATE_LOG_BUCKET]}/*"),
            "Resource",
        ),
        (
            smoke.CLOUDTRAIL_BUCKET,
            lambda st: st.append(dict(st[4])),
            "duplicate statement sid(s)",
        ),
    ],
)
def test_each_document_defect_reds_only_its_own_shape_row(
    tmp_path: Path, store: str, mutate: Callable[[list[dict[str, Any]]], Any], expected_text: str
) -> None:
    account = FakeAccount()
    text = account.policies[store]
    assert text is not None
    statements = json.loads(text)["Statement"]
    mutate(statements)
    account.policies[store] = _document(statements)
    run = _run(tmp_path, account)
    failing = _not_passing(run)
    assert set(failing) == {f"{store}_policy_shape"}
    assert expected_text in run.by_check()[f"{store}_policy_shape"].detail


def test_an_unmutated_copy_of_every_document_passes(tmp_path: Path) -> None:
    account = FakeAccount()
    for store in _ALL_STORES:
        text = account.policies[store]
        assert text is not None
        account.policies[store] = _document(json.loads(text)["Statement"])
    assert _not_passing(_run(tmp_path, account)) == {}


def test_logs_key_allowlist_must_equal_the_discovered_lambda_roles(tmp_path: Path) -> None:
    account = FakeAccount()
    account.functions.append(
        {
            "FunctionName": "crm-agent-eval-capabilities",
            "Role": f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-capabilities-role",
            "KMSKeyArn": _KEY_ARNS[smoke.LOGS_KEY],
        }
    )
    run = _run(tmp_path, account)
    assert set(_not_passing(run)) == {"logs_key_policy_shape"}
    assert "crm-agent-eval-capabilities-role" in run.by_check()["logs_key_policy_shape"].detail


def test_eval_artifacts_allowlists_must_name_the_codebuild_role(tmp_path: Path) -> None:
    account = FakeAccount()
    account.eval_roles["crm-agent-eval-codebuild-role"] = (
        f"arn:aws:iam::{_ACCOUNT}:role/service-role/crm-agent-eval-codebuild-role"
    )
    assert set(_not_passing(_run(tmp_path, account))) == {
        "eval_artifacts_key_policy_shape",
        "eval_artifacts_bucket_policy_shape",
    }


def test_a_missing_eval_artifacts_role_fails_its_two_shape_rows(tmp_path: Path) -> None:
    account = FakeAccount()
    del account.eval_roles["crm-agent-eval-sweep-control-role"]
    run = _run(tmp_path, account)
    assert set(_not_passing(run)) == {
        "eval_artifacts_key_policy_shape",
        "eval_artifacts_bucket_policy_shape",
    }
    assert "NoSuchEntity" in run.by_check()["eval_artifacts_bucket_policy_shape"].detail


def test_state_operator_list_must_match_the_stack_output(tmp_path: Path) -> None:
    stale = f"arn:aws:iam::{_ACCOUNT}:role/PreviousOperator"
    account = FakeAccount()
    for store in smoke.STATE_STORES:
        text = account.policies[store]
        assert text is not None
        account.policies[store] = text.replace(json.dumps(_OPERATOR_ARN), json.dumps(stale))
    run = _run(tmp_path, account)
    row = run.by_check()["state_operator_list_matches_stack"]
    assert row.status == "fail"
    assert "PreviousOperator" in row.detail and "OperatorRole" in row.detail
    assert {c for c in _not_passing(run) if c.endswith("_policy_shape")} == {
        f"{s}_policy_shape" for s in smoke.STATE_STORES
    }


# ---------------------------------------------------------------------------
# Probe classification.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("store", "call", "outcome", "status", "text"),
    [
        (smoke.STATE_BUCKET, smoke.PROBE_GET_OBJECT, None, "fail", "succeeded"),
        (
            smoke.EVAL_ARTIFACTS_BUCKET,
            smoke.PROBE_GET_OBJECT,
            Err("NoSuchKey", "The specified key does not exist.", 404),
            "fail",
            "reached the resource",
        ),
        (
            smoke.FRONTEND_ORIGIN_BUCKET,
            smoke.PROBE_PUT_OBJECT,
            Err(
                "PreconditionFailed",
                "At least one of the pre-conditions you specified did not hold",
                412,
            ),
            "fail",
            "reached the resource",
        ),
        (
            smoke.LOCK_TABLE,
            smoke.PROBE_PUT_ITEM,
            Err("ConditionalCheckFailedException", "The conditional request failed"),
            "fail",
            "reached the resource",
        ),
        (
            smoke.LOCK_TABLE,
            smoke.PROBE_PUT_RESOURCE_POLICY,
            Err("PolicyNotFoundException", "Resource-based policy not found"),
            "fail",
            "reached the resource",
        ),
        (
            smoke.TFSTATE_KEY,
            smoke.PROBE_REVOKE_GRANT,
            Err("NotFoundException", "Grant ID not found"),
            "fail",
            "reached the resource",
        ),
        (
            smoke.CLOUDTRAIL_BUCKET,
            smoke.PROBE_LIST_OBJECTS,
            _implicit_deny("s3:ListBucket", "arn:aws:s3:::b"),
            "fail",
            "implicit deny",
        ),
        (
            smoke.LOGS_KEY,
            smoke.PROBE_DECRYPT,
            _explicit_deny("kms:Decrypt", "k", principal="SomeoneElse"),
            "fail",
            "does not name the probe role",
        ),
        (
            smoke.FRONTEND_KEY,
            smoke.PROBE_GENERATE_DATA_KEY,
            Err("KMSInvalidStateException", "key is pending deletion"),
            DEFER,
            "KMSInvalidStateException: key is pending deletion",
        ),
        (
            smoke.STATE_LOG_BUCKET,
            smoke.PROBE_PUT_OBJECT,
            _explicit_deny("s3:PutObject", "arn:aws:s3:::b/k"),
            "pass",
            "explicit deny in a resource-based policy",
        ),
    ],
)
def test_probe_outcomes_are_classified(
    tmp_path: Path, store: str, call: str, outcome: Err | None, status: str, text: str
) -> None:
    account = FakeAccount(outcomes={(store, call): outcome})
    run = _run(tmp_path, account)
    check = f"probe_denied:{store}:{call}"
    row = run.by_check()[check]
    assert (row.status, text in row.detail) == (status, True), row.detail
    assert set(_not_passing(run)) <= {check}
    if status == DEFER:
        assert row.defer_reason == "probe_call_unclassified_error"


def test_every_probe_call_is_non_mutating_by_construction(tmp_path: Path) -> None:
    run = _run(tmp_path)
    probe = run.account.calls_by("probe")
    operations = {(s, o) for s, o, _ in probe}
    assert operations == {
        ("kms", "GenerateDataKey"),
        ("kms", "Decrypt"),
        ("kms", "RevokeGrant"),
        ("s3", "GetObject"),
        ("s3", "ListObjectsV2"),
        ("s3", "PutObject"),
        ("dynamodb", "GetItem"),
        ("dynamodb", "PutItem"),
        ("dynamodb", "PutResourcePolicy"),
        ("logs", "FilterLogEvents"),
        ("ecr", "BatchGetImage"),
        ("ecr", "GetDownloadUrlForLayer"),
    }
    for service, operation, params in probe:
        if operation == "PutObject":
            assert params["IfMatch"] == '"probe-nonexistent-etag"'
            assert params["Key"] == f"probe-{_RUN_ID}/absent"
            assert params["Body"] == b""
        if operation in ("GetObject", "ListObjectsV2"):
            assert (params.get("Key") or params.get("Prefix", "")).startswith(f"probe-{_RUN_ID}/")
        if operation == "PutItem":
            assert params["ConditionExpression"] == "attribute_exists(#pk)"
            assert params["ExpressionAttributeNames"] == {"#pk": "LockID"}
            assert params["Item"] == {"LockID": {"S": f"probe-{_RUN_ID}"}}
        if operation == "PutResourcePolicy":
            assert params["ExpectedRevisionId"] == "probe-nonexistent-revision"
        if operation == "RevokeGrant":
            assert params["GrantId"] == "0123456789abcdef" * 4
        if operation == "Decrypt":
            assert params["CiphertextBlob"].startswith(b"cipher-of-")
        assert service != "iam"
    assert len([1 for _, o, _ in probe if o == "PutObject"]) == 5
    revoked = {p["KeyId"] for _, o, p in probe if o == "RevokeGrant"}
    assert revoked == {*_KEY_ARNS.values(), _DYNAMODB_KEY_ARN}
    assert re.fullmatch(r"[0-9a-f]{64}", smoke.PROBE_GRANT_ID)


def test_put_object_probes_carry_the_bucket_encryption_so_no_other_deny_can_match(
    tmp_path: Path,
) -> None:
    puts = {
        p["Bucket"]: p for _, o, p in _run(tmp_path).account.calls_by("probe") if o == "PutObject"
    }
    eval_put = puts[_BUCKETS[smoke.EVAL_ARTIFACTS_BUCKET]]
    assert eval_put["ServerSideEncryption"] == "aws:kms"
    assert eval_put["SSEKMSKeyId"] == _KEY_ARNS[smoke.EVAL_ARTIFACTS_KEY]
    log_put = puts[_BUCKETS[smoke.STATE_LOG_BUCKET]]
    assert log_put["ServerSideEncryption"] == "AES256"
    assert "SSEKMSKeyId" not in log_put


def test_generated_key_material_never_reaches_a_row(tmp_path: Path) -> None:
    account = FakeAccount(
        outcomes={
            (smoke.LOGS_KEY, smoke.PROBE_GENERATE_DATA_KEY): None,
            (smoke.LOGS_KEY, smoke.PROBE_DECRYPT): None,
        }
    )
    run = _run(tmp_path, account)
    assert run.by_check()["probe_denied:logs_key:kms_generate_data_key"].status == "fail"
    assert all("PLAINTEXT-KEY-MATERIAL" not in r.detail for r in run.rows)


# ---------------------------------------------------------------------------
# Probe role lifecycle.
# ---------------------------------------------------------------------------


def test_probe_role_trusts_only_the_operator_and_allows_exactly_the_probed_resources(
    tmp_path: Path,
) -> None:
    calls = {
        o: p
        for _, o, p in _run(tmp_path).account.calls_by("operator")
        if o in ("CreateRole", "PutRolePolicy")
    }
    trust = json.loads(calls["CreateRole"]["AssumeRolePolicyDocument"])
    assert trust["Statement"] == [
        {"Effect": "Allow", "Principal": {"AWS": _OPERATOR_ARN}, "Action": "sts:AssumeRole"}
    ]
    assert calls["CreateRole"]["RoleName"] == _PROBE_ROLE
    policy = json.loads(calls["PutRolePolicy"]["PolicyDocument"])
    granted = {
        (action, resource)
        for st in policy["Statement"]
        for action in st["Action"]
        for resource in st["Resource"]
    }
    data_buckets = [_BUCKETS[s] for s in smoke.DATA_PLANE_BUCKETS]
    expected = {(a, k) for a in ("kms:GenerateDataKey", "kms:Decrypt") for k in _KEY_ARNS.values()}
    expected |= {("kms:RevokeGrant", k) for k in (*_KEY_ARNS.values(), _DYNAMODB_KEY_ARN)}
    repository = f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/{_REPOSITORY}"
    expected |= {(a, repository) for a in ("ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer")}
    expected |= {
        (a, f"arn:aws:s3:::{b}/probe-*")
        for a in ("s3:GetObject", "s3:PutObject")
        for b in data_buckets
    }
    expected |= {("s3:ListBucket", f"arn:aws:s3:::{b}") for b in data_buckets}
    expected |= {("s3:PutObject", f"arn:aws:s3:::{_BUCKETS[smoke.FRONTEND_ORIGIN_BUCKET]}/probe-*")}
    expected |= {
        (a, _LOCK_TABLE_ARN)
        for a in ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:PutResourcePolicy")
    }
    expected |= {("logs:FilterLogEvents", _LOG_GROUP_ARN)}
    assert granted == expected
    assert all(st["Effect"] == "Allow" for st in policy["Statement"])


def test_probe_role_is_deleted_after_every_probe_call(tmp_path: Path) -> None:
    calls = _run(tmp_path).account.calls
    operations = [call[2] for call in calls]
    last_probe = max(i for i, call in enumerate(calls) if call[0] == "probe")
    deleted = [i for i, call in enumerate(calls) if call[2] == "DeleteRole"]
    assert deleted and deleted[0] > last_probe
    assert operations.count("DeleteRolePolicy") == 1


def test_a_probe_setup_failure_still_deletes_the_role_and_fails_every_probe_row(
    tmp_path: Path,
) -> None:
    def broken_factory(*_: Any) -> Any:
        raise RuntimeError("cannot build a probe client")

    run = _run(tmp_path, probe_factory=broken_factory)
    by = run.by_check()
    assert by["probe_role_setup"].status == "fail"
    assert by["probe_role_deleted"].status == "pass"
    probe_rows = [c for c in by if c.startswith(("probe_denied:", "admin_simulated_denied:"))]
    assert len(probe_rows) == 37
    assert {by[c].status for c in probe_rows} == {"fail"}
    assert by["logs_probe_denied"].status == "fail"
    assert len(run.rows) == 67


def test_a_create_role_failure_is_reported_and_teardown_tolerates_no_such_entity(
    tmp_path: Path,
) -> None:
    account = FakeAccount(
        create_role_error=Err("AccessDenied", "not authorized to iam:CreateRole", 403)
    )

    def delete_absent(_p: dict[str, Any]) -> Any:
        return Err("NoSuchEntity", "The role cannot be found", 404)

    account.operator_iam_delete_role_policy = delete_absent  # type: ignore[method-assign]
    account.delete_role_error = Err("NoSuchEntity", "The role cannot be found", 404)
    by = _run(tmp_path, account).by_check()
    assert by["probe_role_setup"].status == "fail"
    assert "AccessDenied" in by["probe_role_setup"].detail
    assert by["probe_role_deleted"].status == "pass"


def test_a_role_deletion_failure_is_a_fail_row_naming_the_role(tmp_path: Path) -> None:
    account = FakeAccount(
        delete_role_error=Err("DeleteConflict", "Cannot delete entity, must detach policies", 409)
    )
    row = _run(tmp_path, account).by_check()["probe_role_deleted"]
    assert row.status == "fail"
    assert _PROBE_ROLE in row.detail and "Delete it by hand" in row.detail


# ---------------------------------------------------------------------------
# Admin simulation, logs probe, ECR, CloudTrail.
# ---------------------------------------------------------------------------


def test_admin_simulation_uses_the_live_document_and_fails_on_any_non_explicit_deny(
    tmp_path: Path,
) -> None:
    account = FakeAccount(
        simulated={
            f"{smoke.CLOUDTRAIL_BUCKET}:{_PROBE_ROLE_ARN}": {"s3:PutBucketAcl": "implicitDeny"}
        }
    )
    run = _run(tmp_path, account)
    row = run.by_check()["admin_simulated_denied:cloudtrail_bucket"]
    assert row.status == "fail"
    assert "probe s3:PutBucketAcl=implicitDeny" in row.detail
    assert "simulation pair of the live document" in row.detail
    assert set(_not_passing(run)) == {"admin_simulated_denied:cloudtrail_bucket"}
    simulations = [
        p for _, o, p in run.account.calls_by("operator") if o == "SimulatePrincipalPolicy"
    ]
    assert len(simulations) == 10
    assert sorted({p["PolicySourceArn"] for p in simulations}) == sorted(
        {_PROBE_ROLE_ARN, _OPERATOR_ARN}
    )
    for params in simulations:
        store = next(
            s for s, b in _BUCKETS.items() if params["ResourceArns"] == [f"arn:aws:s3:::{b}"]
        )
        assert params["ResourcePolicy"] == account.policies[store]
        assert params["ActionNames"] == _DESIGN_S3_ADMIN
        assert params["ResourceOwner"] == _ROOT_ARN


def test_admin_simulation_pair_fails_when_the_deny_matches_the_operator_too(
    tmp_path: Path,
) -> None:
    account = FakeAccount(
        simulated={
            f"{smoke.STATE_BUCKET}:{_OPERATOR_ARN}": dict.fromkeys(_DESIGN_S3_ADMIN, "explicitDeny")
        }
    )
    run = _run(tmp_path, account)
    row = run.by_check()["admin_simulated_denied:state_bucket"]
    assert row.status == "fail"
    assert "operator s3:DeleteBucket=explicitDeny" in row.detail
    assert set(_not_passing(run)) == {"admin_simulated_denied:state_bucket"}


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (
            Err(
                "AccessDeniedException",
                "The specified KMS key does not exist or is not allowed to be used with LogGroup",
            ),
            DEFER,
        ),
        (_explicit_deny("logs:FilterLogEvents", _LOG_GROUP_ARN), "pass"),
        (_implicit_deny("logs:FilterLogEvents", _LOG_GROUP_ARN), "fail"),
    ],
)
def test_logs_probe_is_classified_and_unclassified_wording_defers_with_the_message(
    tmp_path: Path, outcome: Err, status: str
) -> None:
    account = FakeAccount(logs_outcome=outcome)
    row = _run(tmp_path, account).by_check()["logs_probe_denied"]
    assert row.status == status
    if status == DEFER:
        assert row.defer_reason == "probe_call_unclassified_error"
        assert outcome.message in row.detail


def test_a_successful_logs_read_defers_as_not_key_gated_and_never_passes(tmp_path: Path) -> None:
    run = _run(tmp_path, FakeAccount(logs_outcome=None))
    row = run.by_check()["logs_probe_denied"]
    assert (row.status, row.defer_reason) == (DEFER, smoke.DEFER_LOGS_READ_NOT_KEY_GATED)
    assert smoke.DEFER_LOGS_READ_NOT_KEY_GATED == "logs_read_not_key_gated"
    assert "the read succeeded" in row.detail
    assert "own service identity" in row.detail
    assert row.resource == _LOG_GROUP
    assert _not_passing(run) == {"logs_probe_denied": DEFER}


def test_ecr_layer_fetch_resolves_an_image_index_and_fetches_the_config_blob(
    tmp_path: Path,
) -> None:
    child = "sha256:" + "d" * 64
    account = FakeAccount()
    account.manifests = {
        "imageTag=abc123": {"schemaVersion": 2, "manifests": [{"digest": child}]},
        f"imageDigest={child}": {"schemaVersion": 2, "config": {"digest": _CONFIG_DIGEST}},
    }
    run = _run(tmp_path, account)
    assert run.by_check()["ecr_layer_fetch"].status == "pass"
    assert len(run.fetched) == 1 and run.fetched[0].startswith("https://")
    assert "X-Amz-Signature" not in run.by_check()["ecr_layer_fetch"].detail


def test_ecr_layer_fetch_fails_on_a_non_200(tmp_path: Path) -> None:
    row = _run(tmp_path, layer_status=403).by_check()["ecr_layer_fetch"]
    assert row.status == "fail" and "HTTP 403" in row.detail


def test_cloudtrail_delivery_polls_until_a_current_delivery(tmp_path: Path) -> None:
    account = FakeAccount(
        trail_statuses=[
            {
                "LatestDeliveryTime": _NOW - timedelta(hours=2),
                "LatestCloudWatchLogsDeliveryTime": _NOW - timedelta(minutes=1),
            },
            {
                "LatestDeliveryTime": _NOW - timedelta(minutes=1),
                "LatestCloudWatchLogsDeliveryTime": _NOW - timedelta(minutes=1),
            },
        ]
    )
    run = _run(tmp_path, account, delivery_poll_seconds=1200.0)
    by = run.by_check()
    assert by["cloudtrail_delivery_current"].status == "pass"
    assert by["cloudtrail_logs_delivery_current"].status == "pass"
    assert run.slept.count(60.0) == 1


def test_cloudtrail_delivery_defers_when_not_current_within_the_poll(tmp_path: Path) -> None:
    account = FakeAccount(trail_statuses=[{"LatestDeliveryTime": _NOW - timedelta(hours=2)}])
    row = _run(tmp_path, account).by_check()["cloudtrail_delivery_current"]
    assert (row.status, row.defer_reason) == (DEFER, "cloudtrail_delivery_not_current")


def test_cloudtrail_delivery_error_fails(tmp_path: Path) -> None:
    account = FakeAccount(
        trail_statuses=[
            {
                "LatestDeliveryTime": _NOW - timedelta(hours=1),
                "LatestDeliveryError": "AccessDenied: kms:GenerateDataKey",
            }
        ]
    )
    row = _run(tmp_path, account).by_check()["cloudtrail_delivery_current"]
    assert row.status == "fail" and "AccessDenied" in row.detail


# ---------------------------------------------------------------------------
# Defers and redaction.
# ---------------------------------------------------------------------------


def test_absent_backend_config_defers_only_the_state_store_rows(tmp_path: Path) -> None:
    run = _run(tmp_path, backend=tmp_path / "absent.hcl")
    not_passing = _not_passing(run)
    assert set(not_passing.values()) == {DEFER}
    by = run.by_check()
    assert {by[c].defer_reason for c in not_passing} == {"backend_config_absent"}
    state_checks = {
        c
        for c in _expected_checks()
        if any(c.endswith(f"{s}_policy_shape") or f":{s}" in c for s in smoke.STATE_STORES)
    } | {"state_operator_list_matches_stack"}
    assert set(not_passing) == state_checks
    assert len(run.rows) == 66


def test_absent_operator_output_defers_every_shape_and_logs_key_event_row(tmp_path: Path) -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "crm_operator_principal_arns"}
    run = _run(tmp_path, outputs=outputs)
    not_passing = _not_passing(run)
    expected = {f"{s}_policy_shape" for s in smoke.STORES} | {
        "state_operator_list_matches_stack",
        "logs_key_access_denied_events",
        "logs_key_decrypt_per_function",
    }
    assert set(not_passing) == expected
    assert {run.by_check()[c].defer_reason for c in expected} == {
        "operator_principal_output_absent"
    }


def test_absent_bucket_output_defers_that_bucket_and_its_key(tmp_path: Path) -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "eval_artifacts_bucket_name"}
    run = _run(tmp_path, outputs=outputs)
    not_passing = _not_passing(run)
    assert set(not_passing.values()) == {DEFER}
    assert set(not_passing) == {c for c in _expected_checks() if "eval_artifacts" in c}
    assert len(run.rows) == 66


def test_absent_runtime_output_defers_the_ecr_row(tmp_path: Path) -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "agentcore_runtime_arn"}
    run = _run(tmp_path, outputs=outputs)
    assert _not_passing(run) == {"ecr_layer_fetch": DEFER}


def test_no_row_carries_an_account_id_or_the_client_secret(tmp_path: Path) -> None:
    account = FakeAccount(
        outcomes={(smoke.STATE_BUCKET, smoke.PROBE_GET_OBJECT): None},
        trail_statuses=[
            {
                "LatestDeliveryTime": _NOW - timedelta(hours=1),
                "LatestDeliveryError": f"bucket {_ACCOUNT}",
            }
        ],
    )
    rows = _run(tmp_path, account).rows
    text = json.dumps([[r.resource, r.check, r.detail, r.defer_reason] for r in rows])
    assert _ACCOUNT in json.dumps(_OUTPUTS)
    assert re.search(r"(?<!\d)\d{12}(?!\d)", text) is None
    assert _SENSITIVE_OUTPUT not in text
    assert "<account>" in text


def test_redaction_is_load_bearing() -> None:
    assert smoke.redact(f"arn:aws:iam::{_ACCOUNT}:root") == "arn:aws:iam::<account>:root"
    assert smoke.redact("1234567890123") == "1234567890123"


def test_backend_config_parser_reads_quoted_assignments(tmp_path: Path) -> None:
    values = smoke.read_backend_config(_backend(tmp_path))
    assert values is not None
    assert values["bucket"] == _BUCKETS[smoke.STATE_BUCKET]
    assert values["dynamodb_table"] == _LOCK_TABLE
    assert "encrypt" not in values
    assert smoke.read_backend_config(tmp_path / "missing.hcl") is None


def test_backend_config_missing_a_field_fails_the_state_rows(tmp_path: Path) -> None:
    path = tmp_path / "backend.hcl"
    path.write_text(f'bucket = "{_BUCKETS[smoke.STATE_BUCKET]}"\n', encoding="utf-8")
    run = _run(tmp_path, backend=path)
    by = run.by_check()
    assert by["tfstate_key_policy_shape"].status == "fail"
    assert "dynamodb_table" in by["tfstate_key_policy_shape"].detail
    assert len(run.rows) == 66


# ---------------------------------------------------------------------------
# Exemption operators, log-path rows and the DynamoDB CMK.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("store", "index", "operator"),
    [
        (smoke.TFSTATE_KEY, 1, "BoolIfExists"),
        (smoke.CLOUDTRAIL_BUCKET, 3, "Bool"),
        (smoke.STATE_LOG_BUCKET, 2, "Bool"),
    ],
)
def test_the_exemption_operator_is_bool_on_keys_and_bool_if_exists_on_buckets(
    tmp_path: Path, store: str, index: int, operator: str
) -> None:
    account = FakeAccount()
    text = account.policies[store]
    assert text is not None
    statements = json.loads(text)["Statement"]
    condition = statements[index]["Condition"]
    (current,) = (k for k in condition if k.startswith("Bool"))
    condition[operator] = condition.pop(current)
    account.policies[store] = _document(statements)
    run = _run(tmp_path, account)
    assert set(_not_passing(run)) == {f"{store}_policy_shape"}


def test_a_key_admin_deny_without_the_alias_actions_fails(tmp_path: Path) -> None:
    account = FakeAccount()
    text = account.policies[smoke.LOGS_KEY]
    assert text is not None
    statements = json.loads(text)["Statement"]
    statements[3]["Action"].remove("kms:UpdateAlias")
    account.policies[smoke.LOGS_KEY] = _document(statements)
    run = _run(tmp_path, account)
    assert set(_not_passing(run)) == {"logs_key_policy_shape"}
    assert "kms:UpdateAlias" in run.by_check()["logs_key_policy_shape"].detail


def test_dynamodb_cmk_admin_deny_is_probed_and_its_absence_fails(tmp_path: Path) -> None:
    account = FakeAccount(
        outcomes={
            (smoke.DYNAMODB_KEY, smoke.PROBE_REVOKE_GRANT): Err(
                "NotFoundException", "Grant ID not found"
            )
        }
    )
    row = _run(tmp_path, account).by_check()["probe_denied:dynamodb_key:kms_revoke_grant_absent"]
    assert row.status == "fail" and "reached the resource" in row.detail


def test_absent_table_names_output_defers_the_dynamodb_cmk_probe(tmp_path: Path) -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "dynamodb_table_names"}
    run = _run(tmp_path, outputs=outputs)
    assert _not_passing(run) == {"probe_denied:dynamodb_key:kms_revoke_grant_absent": DEFER}


def test_cloudtrail_logs_delivery_error_fails_and_absence_defers(tmp_path: Path) -> None:
    account = FakeAccount(
        trail_statuses=[
            {
                "LatestDeliveryTime": _NOW - timedelta(minutes=1),
                "LatestCloudWatchLogsDeliveryTime": _NOW - timedelta(hours=1),
                "LatestCloudWatchLogsDeliveryError": "AccessDeniedException: kms",
            }
        ]
    )
    run = _run(tmp_path, account)
    assert _not_passing(run) == {"cloudtrail_logs_delivery_current": "fail"}
    no_group = FakeAccount(trail_logs_group=None)
    row = _run(tmp_path, no_group).by_check()["cloudtrail_logs_delivery_current"]
    assert (row.status, row.defer_reason) == (DEFER, "trail_has_no_cloudwatch_logs_delivery")


def test_flow_log_delivery_fails_on_a_failed_flow_log_and_defers_without_one(
    tmp_path: Path,
) -> None:
    failed = FakeAccount(
        flow_logs=[
            {"FlowLogId": "fl-1", "DeliverLogsStatus": "SUCCESS"},
            {
                "FlowLogId": "fl-2",
                "DeliverLogsStatus": "FAILED",
                "DeliverLogsErrorMessage": "Access error",
            },
        ]
    )
    row = _run(tmp_path, failed).by_check()["flow_log_delivery"]
    assert row.status == "fail" and "fl-2" in row.detail and "fl-1" not in row.detail
    none = _run(tmp_path, FakeAccount(flow_logs=[])).by_check()["flow_log_delivery"]
    assert (none.status, none.defer_reason) == (DEFER, "vpc_has_no_flow_log")


_HEALTHY_DECRYPTS = (_ACCOUNTS_GET_ROLE, _RELAY_ROLE)
_OTHER_RUN_PROBE = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-probe-0123456789ab"
_CO_TENANT = f"arn:aws:iam::{_ACCOUNT}:role/PepStack-ConfigCustomRule"


def _service_event(name: str, error: str | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "eventName": name,
        "userIdentity": {"type": "AWSService", "invokedBy": "logs.amazonaws.com"},
    }
    if error:
        detail["errorCode"] = error
    return {"EventName": name, "CloudTrailEvent": json.dumps(detail)}


def _key_rows(tmp_path: Path, *extra: dict[str, Any]) -> dict[str, CheckResult]:
    account = FakeAccount()
    account.key_events = [*(_decrypt_event(r) for r in _HEALTHY_DECRYPTS), *extra]
    by = _run(tmp_path, account).by_check()
    return {c: by[c] for c in ("logs_key_access_denied_events", "logs_key_decrypt_per_function")}


def _statuses(rows: dict[str, CheckResult]) -> tuple[str, str]:
    return (
        rows["logs_key_access_denied_events"].status,
        rows["logs_key_decrypt_per_function"].status,
    )


@pytest.mark.parametrize(
    ("label", "events", "statuses", "text"),
    [
        ("healthy control", [], ("pass", "pass"), "excluded: 0 probe-role"),
        (
            "this run's own probe denials",
            [
                _decrypt_event(_PROBE_ROLE_ARN, error="AccessDenied", name=n)
                for n in ("GenerateDataKey", "Decrypt", "RevokeGrant")
            ],
            ("pass", "pass"),
            "excluded: 3 probe-role event(s)",
        ),
        (
            "an earlier run's probe denials",
            [_decrypt_event(_OTHER_RUN_PROBE, error="AccessDenied")],
            ("pass", "pass"),
            "excluded: 1 probe-role event(s)",
        ),
        (
            "an operator log read's dry-run Decrypt",
            [_decrypt_event(_OPERATOR_ARN, error="DryRunOperationException")],
            ("pass", "pass"),
            "1 DryRunOperationException",
        ),
        (
            "a function role's dry-run Decrypt",
            [_decrypt_event(_RELAY_ROLE, error="DryRunOperationException")],
            ("pass", "pass"),
            "1 DryRunOperationException",
        ),
        (
            "a co-tenant's denied read",
            [
                _decrypt_event(_CO_TENANT, error="AccessDenied"),
                _decrypt_event(_CO_TENANT, error="AccessDenied", name="GenerateDataKey"),
            ],
            ("pass", "pass"),
            "2 expected denial(s) of non-allowlisted principals",
        ),
        (
            "a co-tenant's otherwise-errored Decrypt",
            [_decrypt_event(_CO_TENANT, error="IncorrectKeyException")],
            ("pass", "pass"),
            "",
        ),
        (
            "a function role denied",
            [_decrypt_event(_ACCOUNTS_GET_ROLE, error="AccessDenied", name="GenerateDataKey")],
            ("fail", "pass"),
            "admitted principals denied ['GenerateDataKey by",
        ),
        (
            "a function role's denied Decrypt",
            [_decrypt_event(_RELAY_ROLE, error="AccessDenied")],
            ("fail", "fail"),
            "crm-agent-eval-agent-relay-role",
        ),
        (
            "an operator denied",
            [_decrypt_event(_OPERATOR_ARN, error="AccessDeniedException", name="CreateGrant")],
            ("fail", "pass"),
            "CreateGrant by",
        ),
        (
            "an AWS service denied",
            [_service_event("GenerateDataKey", error="AccessDenied")],
            ("fail", "pass"),
            "service logs.amazonaws.com",
        ),
    ],
)
def test_logs_key_rows_count_only_principals_the_key_must_admit(
    tmp_path: Path, label: str, events: list[dict[str, Any]], statuses: tuple[str, str], text: str
) -> None:
    rows = _key_rows(tmp_path, *events)
    assert _statuses(rows) == statuses, label
    assert text in rows["logs_key_access_denied_events"].detail + str(
        rows["logs_key_decrypt_per_function"].detail
    ), label


def test_a_dry_run_decrypt_is_not_taken_as_a_function_decrypt(tmp_path: Path) -> None:
    account = FakeAccount()
    account.key_events = [
        _decrypt_event(_ACCOUNTS_GET_ROLE),
        _decrypt_event(_RELAY_ROLE, error="DryRunOperationException"),
    ]
    row = _run(tmp_path, account).by_check()["logs_key_decrypt_per_function"]
    assert (row.status, row.defer_reason) == (DEFER, "functions_not_invoked_since_logs_key_change")
    assert "crm-agent-eval-agent-relay" in row.detail


def test_a_probe_role_is_excluded_only_by_its_name_prefix() -> None:
    lookalike = f"arn:aws:iam::{_ACCOUNT}:role/ops/not-crm-agent-eval-probe-x"
    tally = smoke.tally_logs_key_events(
        [
            _decrypt_event(_PROBE_ROLE_ARN, error="AccessDenied"),
            _decrypt_event(lookalike, error="AccessDenied"),
        ],
        frozenset({lookalike}),
    )
    assert (tally.probe_events, len(tally.admitted_denials)) == (1, 1)


def test_an_errored_decrypt_by_a_function_role_fails_the_per_function_row(tmp_path: Path) -> None:
    account = FakeAccount()
    account.key_events = [
        _decrypt_event(_ACCOUNTS_GET_ROLE),
        _decrypt_event(_RELAY_ROLE, error="KMSInvalidStateException"),
    ]
    run = _run(tmp_path, account)
    assert _not_passing(run) == {"logs_key_decrypt_per_function": "fail"}


def test_a_function_with_no_decrypt_since_the_change_defers_by_name(tmp_path: Path) -> None:
    account = FakeAccount()
    account.key_events = [_decrypt_event(_ACCOUNTS_GET_ROLE)]
    row = _run(tmp_path, account).by_check()["logs_key_decrypt_per_function"]
    assert (row.status, row.defer_reason) == (DEFER, "functions_not_invoked_since_logs_key_change")
    assert "crm-agent-eval-agent-relay" in row.detail
    assert "1/2 functions decrypted" in row.detail


def test_a_truncated_lookup_never_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "LOOKUP_MAX_PAGES", 1)
    run = _run(tmp_path, FakeAccount(key_events_paged=True))
    by = run.by_check()
    assert by["logs_key_access_denied_events"].defer_reason == "cloudtrail_lookup_truncated"
    assert by["logs_key_decrypt_per_function"].status == DEFER


def _event_rows(account: FakeAccount, tmp_path: Path) -> tuple[CheckResult, CheckResult]:
    by = _run(tmp_path, account).by_check()
    return by["logs_key_access_denied_events"], by["logs_key_decrypt_per_function"]


def test_a_function_decrypt_seen_only_by_the_decrypt_lookup_counts(tmp_path: Path) -> None:
    account = FakeAccount()
    account.key_events = [_decrypt_event(_ACCOUNTS_GET_ROLE)]
    account.decrypt_events = [
        _function_decrypt_event(_RELAY_ROLE, "crm-agent-eval-agent-relay", "ev-relay-1")
    ]
    errors, decrypt = _event_rows(account, tmp_path)
    assert (errors.status, decrypt.status) == ("pass", "pass"), decrypt.detail
    assert "all 2 functions decrypted" in decrypt.detail
    assert "(2 event(s) read;" in decrypt.detail


def test_an_event_returned_by_both_lookups_is_counted_once(tmp_path: Path) -> None:
    relay = _function_decrypt_event(_RELAY_ROLE, "crm-agent-eval-agent-relay", "ev-relay-1")
    relay_by_resource = {**relay, "Resources": [{"ResourceName": _KEY_ARNS[smoke.LOGS_KEY]}]}
    account = FakeAccount()
    account.key_events = [
        _function_decrypt_event(_ACCOUNTS_GET_ROLE, "crm-agent-eval-accounts-get", "ev-acct-1"),
        relay_by_resource,
    ]
    account.decrypt_events = [relay, copy.deepcopy(relay)]
    errors, decrypt = _event_rows(account, tmp_path)
    assert (errors.status, decrypt.status) == ("pass", "pass"), decrypt.detail
    assert "(2 event(s) read;" in errors.detail


def test_an_admitted_role_denied_only_in_the_decrypt_lookup_fails_the_errors_row(
    tmp_path: Path,
) -> None:
    account = FakeAccount()
    account.key_events = [_decrypt_event(r) for r in _HEALTHY_DECRYPTS]
    account.decrypt_events = [
        _function_decrypt_event(
            _ACCOUNTS_GET_ROLE, "crm-agent-eval-accounts-get", "ev-denied-1", error="AccessDenied"
        )
    ]
    errors, decrypt = _event_rows(account, tmp_path)
    assert (errors.status, decrypt.status) == ("fail", "fail")
    assert (
        "Decrypt by arn:aws:iam::<account>:role/crm-agent-eval-accounts-get-role" in errors.detail
    )


def test_decrypt_lookup_events_on_another_key_are_ignored(tmp_path: Path) -> None:
    other = _KEY_ARNS[smoke.FRONTEND_KEY]
    account = FakeAccount()
    account.key_events = [_decrypt_event(_ACCOUNTS_GET_ROLE)]
    account.decrypt_events = [
        _function_decrypt_event(
            _RELAY_ROLE, "crm-agent-eval-agent-relay", "ev-other-1", key_arn=other
        ),
        _function_decrypt_event(
            _ACCOUNTS_GET_ROLE,
            "crm-agent-eval-accounts-get",
            "ev-other-2",
            key_arn=other,
            error="AccessDenied",
        ),
    ]
    errors, decrypt = _event_rows(account, tmp_path)
    assert errors.status == "pass", errors.detail
    assert (decrypt.status, decrypt.defer_reason) == (
        DEFER,
        "functions_not_invoked_since_logs_key_change",
    )
    assert "1/2 functions decrypted" in decrypt.detail
    assert "(1 event(s) read;" in decrypt.detail


def test_a_truncated_decrypt_lookup_never_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smoke, "LOOKUP_MAX_PAGES", 1)
    account = FakeAccount(decrypt_events_paged=True)
    account.key_events = [_decrypt_event(r) for r in _HEALTHY_DECRYPTS]
    errors, decrypt = _event_rows(account, tmp_path)
    assert (errors.status, errors.defer_reason) == (DEFER, "cloudtrail_lookup_truncated")
    assert decrypt.status == "pass"


def test_an_unobserved_logs_key_change_defers_the_event_and_ingestion_rows(tmp_path: Path) -> None:
    run = _run(tmp_path, FakeAccount(key_changed_at=None))
    expected = {"logs_key_access_denied_events", "logs_key_decrypt_per_function"} | {
        f"log_ingestion_current:{w}" for w in smoke.INGESTION_LOGS
    }
    assert set(_not_passing(run)) == expected
    by = run.by_check()
    assert {by[c].defer_reason for c in expected} == {"logs_key_policy_change_unobserved"}


def test_ingestion_polls_until_an_event_arrives_and_defers_a_silent_writer(
    tmp_path: Path,
) -> None:
    account = FakeAccount(
        ingested={_INGESTION_GROUPS[smoke.API_ACCESS_LOG], _INGESTION_GROUPS[smoke.RUNTIME_LOG]},
        ingested_after={_INGESTION_GROUPS[smoke.RUNTIME_LOG]: 1},
    )
    run = _run(tmp_path, account, ingestion_poll_seconds=0.5)
    by = run.by_check()
    assert by["log_ingestion_current:api_access_log"].status == "pass"
    assert by["log_ingestion_current:runtime_log"].status == "pass"
    silent = by["log_ingestion_current:codebuild_log"]
    assert (silent.status, silent.defer_reason) == (DEFER, "no_log_event_since_logs_key_change")
    assert 0.5 >= max(run.slept) > 0


def test_absent_runtime_log_group_output_defers_only_its_ingestion_row(tmp_path: Path) -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "agentcore_runtime_log_group_name"}
    run = _run(tmp_path, outputs=outputs)
    assert _not_passing(run) == {"log_ingestion_current:runtime_log": DEFER}
