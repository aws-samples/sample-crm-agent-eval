# SPDX-License-Identifier: MIT
"""Layer 4 smoke for the principal Deny on the CRM data plane.

Reads the deployed stack, never source:

* tables from the `dynamodb_table_names` output, and the DynamoDB CMK from each table's
  `SSEDescription.KMSMasterKeyArn`;
* the operator ARNs from the `crm_operator_principal_arns` output (the shape rows defer while it is
  absent, that is, before the apply);
* the data-plane roles from the account: the execution role of every `crm-agent-eval-*` Lambda that
  holds an inline policy named `<function>-dynamodb-kms`.

Rows:

* `table_policy_shape:<table>` and `key_policy_shape`: each statement's sid, effect, principal,
  action set, resources and exact conditions; the allowlists equal the discovered roles plus the
  operators (data plane) and the operators plus the account root (administration).
* `table_sse_enabled:<table>`: `TableStatus == ACTIVE`, no `InaccessibleEncryptionDateTime`, and
  `SSEDescription.Status == ENABLED`.
* `data_plane_roles_discovered`: at least one role was found.
* `operator_data_plane_admitted`: the operator's `GetItem` on a non-existent key succeeds.
* `probe_denied:<call>`: a run-scoped role `crm-agent-eval-probe-<run_id>`, trusted by the operator
  only and holding an identity Allow for exactly the probed actions, is refused with "explicit deny
  in a resource-based policy". A success, `ConditionalCheckFailedException`,
  `PolicyNotFoundException` or an implicit-deny message fails; any other error defers.
* `probe_role_setup` (only on failure): creating, granting or assuming the probe role raised.
* `probe_role_deleted`: the probe role is deleted in a `finally`.

No probe call can mutate data even if the boundary were absent: `PutItem` carries
`attribute_exists(<pk>)` on a non-existent key, `PutResourcePolicy` carries an `ExpectedRevisionId`
no policy can have, and `GenerateDataKey`'s key material is discarded.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import boto3
from scripts.smoke.harness import (
    DEFER,
    CheckResult,
    Deadline,
    ExpectedCheckCount,
    SmokeContext,
    error_code,
    run_main,
    smoke_client_config,
)

from crm.store.schema import table_schema

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "dynamodb_principal_deny"

TABLE_NAMES_OUTPUT: Final[str] = "dynamodb_table_names"
OPERATOR_ARNS_OUTPUT: Final[str] = "crm_operator_principal_arns"

FUNCTION_PREFIX: Final[str] = "crm-agent-eval-"
DYNAMODB_KMS_POLICY_SUFFIX: Final[str] = "-dynamodb-kms"
PROBE_ROLE_PREFIX: Final[str] = "crm-agent-eval-probe-"
PROBE_INLINE_POLICY_NAME: Final[str] = "probe-allow"
PROBE_ROLE_TAGS: Final[tuple[tuple[str, str], ...]] = (
    ("service", "crm-agent-eval"),
    ("data-classification", "internal"),
    ("purpose", "smoke-probe"),
)
PROBE_SESSION_SECONDS: Final[int] = 900
ASSUME_TIMEOUT_SECONDS: Final[float] = 60.0
ASSUME_BACKOFF_SECONDS: Final[float] = 3.0

#: The table and GSI the probe calls address. Policies are checked on every table.
PROBE_TABLE: Final[str] = "accounts"
PROBE_INDEX: Final[str] = "gsi_tenant"
PROBE_EXPECTED_REVISION_ID: Final[str] = "probe-nonexistent-revision"

TABLE_DATA_PLANE_SID: Final[str] = "DenyDataPlaneToNonProjectPrincipals"
TABLE_DATA_PLANE_ACTIONS: Final[tuple[str, ...]] = (
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:ConditionCheckItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:PartiQLSelect",
    "dynamodb:PartiQLInsert",
    "dynamodb:PartiQLUpdate",
    "dynamodb:PartiQLDelete",
    "dynamodb:CreateBackup",
    "dynamodb:ExportTableToPointInTime",
    "dynamodb:RestoreTableToPointInTime",
    "dynamodb:EnableKinesisStreamingDestination",
)
TABLE_ADMIN_SID: Final[str] = "DenyTableAdministrationToNonOperators"
TABLE_ADMIN_ACTIONS: Final[tuple[str, ...]] = (
    "dynamodb:DeleteTable",
    "dynamodb:UpdateTable",
    "dynamodb:UpdateContinuousBackups",
    "dynamodb:UpdateTimeToLive",
    "dynamodb:PutResourcePolicy",
    "dynamodb:DeleteResourcePolicy",
    "dynamodb:TagResource",
    "dynamodb:UntagResource",
    "dynamodb:UpdateContributorInsights",
)
KEY_ROOT_SID: Final[str] = "Enable IAM User Permissions"
KEY_USE_SID: Final[str] = "DenyKeyUseToNonProjectPrincipals"
KEY_USE_ACTIONS: Final[tuple[str, ...]] = (
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
    "kms:CreateGrant",
)
KEY_ADMIN_SID: Final[str] = "DenyKeyAdministrationToNonOperators"
KEY_ADMIN_ACTIONS: Final[tuple[str, ...]] = (
    "kms:PutKeyPolicy",
    "kms:ScheduleKeyDeletion",
    "kms:DisableKey",
    "kms:RevokeGrant",
    "kms:DisableKeyRotation",
)

PRINCIPAL_ARN_KEY: Final[str] = "aws:PrincipalArn"
PRINCIPAL_IS_SERVICE_KEY: Final[str] = "aws:PrincipalIsAWSService"
EXPLICIT_RESOURCE_DENY: Final[str] = "explicit deny in a resource-based policy"

PROBE_GET_ITEM: Final[str] = "get_item"
PROBE_QUERY_GSI: Final[str] = "query_gsi"
PROBE_PUT_ITEM: Final[str] = "put_item_conditional"
PROBE_PUT_RESOURCE_POLICY: Final[str] = "put_resource_policy_conditional"
PROBE_GENERATE_DATA_KEY: Final[str] = "kms_generate_data_key"
PROBE_CALLS: Final[tuple[str, ...]] = (
    PROBE_GET_ITEM,
    PROBE_QUERY_GSI,
    PROBE_PUT_ITEM,
    PROBE_PUT_RESOURCE_POLICY,
    PROBE_GENERATE_DATA_KEY,
)
#: The identity actions the probe role is granted, exactly the ones its calls need.
PROBE_DYNAMODB_ACTIONS: Final[tuple[str, ...]] = (
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:PutItem",
    "dynamodb:PutResourcePolicy",
)
PROBE_KMS_ACTIONS: Final[tuple[str, ...]] = ("kms:GenerateDataKey",)

#: Error codes that mean the call reached the data or policy, so no boundary refused it.
_REACHED_RESOURCE_CODES: Final[frozenset[str]] = frozenset(
    {"ConditionalCheckFailedException", "PolicyNotFoundException"}
)

DEFER_OPERATOR_OUTPUT_ABSENT: Final[str] = "operator_principal_output_absent"
DEFER_PROBE_UNCLASSIFIED: Final[str] = "probe_call_unclassified_error"

#: Builds a client for `service` from the probe role's temporary credentials.
ProbeClientFactory = Callable[[SmokeContext, str, Mapping[str, str]], Any]


def session_probe_client(ctx: SmokeContext, service: str, credentials: Mapping[str, str]) -> Any:
    """Build a `service` client from the probe's `AssumeRole` credentials, never ambient ones."""
    session = boto3.session.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=ctx.region,
    )
    return session.client(service, region_name=ctx.region, config=smoke_client_config(service))


@dataclass(frozen=True)
class TableTarget:
    """One deployed table: its logical and physical names, ARN and encryption description."""

    logical: str
    physical: str
    arn: str
    sse: Mapping[str, Any]
    table_status: str = ""


@dataclass(frozen=True)
class CallerIdentity:
    """The operator's account, partition and trust-policy principal ARN."""

    account: str
    partition: str
    principal_arn: str


@dataclass(frozen=True)
class ExpectedStatement:
    """One required policy statement: effect, principal, actions, resources and exact conditions."""

    sid: str
    effect: str
    principal: str
    actions: tuple[str, ...]
    resources: frozenset[str]
    conditions: Mapping[str, Mapping[str, frozenset[str]]]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _principal_values(principal: Any) -> set[str]:
    """The principal identifiers of a statement; `"*"` and `{"AWS": "*"}` both yield `{"*"}`."""
    if principal == "*":
        return {"*"}
    if isinstance(principal, Mapping):
        values: set[str] = set()
        for key, entry in principal.items():
            if key != "AWS":
                values.add(f"{key}:{entry}")
                continue
            values.update(str(v) for v in _as_list(entry))
        return values
    return {f"unrecognised:{principal!r}"}


def _normalise_conditions(raw: Any) -> dict[str, dict[str, frozenset[str]]] | None:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        return None
    conditions: dict[str, dict[str, frozenset[str]]] = {}
    for operator, keys in raw.items():
        if not isinstance(keys, Mapping):
            return None
        values: dict[str, frozenset[str]] = {}
        for key, value in keys.items():
            items = [str(v) for v in _as_list(value)]
            if operator == "Bool":
                items = [item.lower() for item in items]
            values[str(key)] = frozenset(items)
        conditions[str(operator)] = values
    return conditions


def statement_problems(statement: Mapping[str, Any], expected: ExpectedStatement) -> list[str]:
    """Every way `statement` differs from `expected`; empty when it matches exactly."""
    problems: list[str] = []
    if statement.get("Effect") != expected.effect:
        problems.append(f"Effect {statement.get('Effect')!r} != {expected.effect!r}")
    principals = _principal_values(statement.get("Principal"))
    if principals != {expected.principal}:
        problems.append(f"Principal {sorted(principals)} != [{expected.principal!r}]")
    if "NotAction" in statement or "NotResource" in statement or "NotPrincipal" in statement:
        problems.append("carries a Not* element")
    actions = {str(a) for a in _as_list(statement.get("Action"))}
    if actions != set(expected.actions):
        problems.append(
            f"Action missing {sorted(set(expected.actions) - actions)}, "
            f"extra {sorted(actions - set(expected.actions))}"
        )
    resources = {str(r) for r in _as_list(statement.get("Resource"))}
    if resources != set(expected.resources):
        problems.append(f"Resource {sorted(resources)} != {sorted(expected.resources)}")
    conditions = _normalise_conditions(statement.get("Condition"))
    if conditions is None:
        problems.append(f"Condition is malformed: {statement.get('Condition')!r}")
    elif conditions != {op: dict(keys) for op, keys in expected.conditions.items()}:
        problems.append(f"Condition {_render(conditions)} != {_render(expected.conditions)}")
    return problems


def _render(conditions: Mapping[str, Mapping[str, frozenset[str]]]) -> str:
    return json.dumps(
        {op: {k: sorted(v) for k, v in keys.items()} for op, keys in conditions.items()},
        sort_keys=True,
    )


def document_problems(document_text: str, expected: Sequence[ExpectedStatement]) -> list[str]:
    """Every way a policy document differs from exactly the `expected` statements, by sid."""
    try:
        document = json.loads(document_text)
    except json.JSONDecodeError as exc:
        return [f"policy is not JSON: {exc}"]
    if not isinstance(document, Mapping):
        return ["policy is not a JSON object"]
    statements = [s for s in _as_list(document.get("Statement")) if isinstance(s, Mapping)]
    by_sid: dict[str, list[Mapping[str, Any]]] = {}
    for statement in statements:
        by_sid.setdefault(str(statement.get("Sid", "")), []).append(statement)
    expected_sids = {e.sid for e in expected}
    problems: list[str] = []
    extra = sorted(set(by_sid) - expected_sids)
    if extra or len(statements) != len(by_sid):
        problems.append(f"unexpected or duplicate statement sid(s): {extra or sorted(by_sid)}")
    for wanted in expected:
        found = by_sid.get(wanted.sid)
        if not found:
            problems.append(f"{wanted.sid}: absent")
            continue
        problems.extend(f"{wanted.sid}: {p}" for p in statement_problems(found[0], wanted))
    return problems


def expected_table_statements(
    table_arn: str, data_plane: frozenset[str], administrators: frozenset[str]
) -> tuple[ExpectedStatement, ...]:
    """The two statements every table's resource policy must hold."""
    resources = frozenset({table_arn, f"{table_arn}/index/*"})
    return (
        ExpectedStatement(
            sid=TABLE_DATA_PLANE_SID,
            effect="Deny",
            principal="*",
            actions=TABLE_DATA_PLANE_ACTIONS,
            resources=resources,
            conditions={"ArnNotEquals": {PRINCIPAL_ARN_KEY: data_plane}},
        ),
        ExpectedStatement(
            sid=TABLE_ADMIN_SID,
            effect="Deny",
            principal="*",
            actions=TABLE_ADMIN_ACTIONS,
            resources=resources,
            conditions={"ArnNotEquals": {PRINCIPAL_ARN_KEY: administrators}},
        ),
    )


def expected_key_statements(
    root_arn: str, data_plane: frozenset[str], administrators: frozenset[str]
) -> tuple[ExpectedStatement, ...]:
    """The three statements the DynamoDB CMK's key policy must hold."""
    everything = frozenset({"*"})
    return (
        ExpectedStatement(
            sid=KEY_ROOT_SID,
            effect="Allow",
            principal=root_arn,
            actions=("kms:*",),
            resources=everything,
            conditions={},
        ),
        ExpectedStatement(
            sid=KEY_USE_SID,
            effect="Deny",
            principal="*",
            actions=KEY_USE_ACTIONS,
            resources=everything,
            conditions={
                "ArnNotEquals": {PRINCIPAL_ARN_KEY: data_plane},
                "Bool": {PRINCIPAL_IS_SERVICE_KEY: frozenset({"false"})},
            },
        ),
        ExpectedStatement(
            sid=KEY_ADMIN_SID,
            effect="Deny",
            principal="*",
            actions=KEY_ADMIN_ACTIONS,
            resources=everything,
            conditions={"ArnNotEquals": {PRINCIPAL_ARN_KEY: administrators}},
        ),
    )


def caller_identity(ctx: SmokeContext) -> CallerIdentity:
    """Resolve the operator's account, partition and the ARN a trust policy can name.

    An assumed-role session resolves to its role's ARN through `iam get-role` (so a role path is
    kept); an IAM user is used as is. Any other caller raises `ValueError`.
    """
    arn = str(ctx.client("sts").get_caller_identity()["Arn"])
    parts = arn.split(":", 5)
    if len(parts) != 6:
        raise ValueError(f"caller ARN {arn!r} is not an ARN")
    partition, service, account, resource = parts[1], parts[2], parts[4], parts[5]
    if service == "sts" and resource.startswith("assumed-role/"):
        role_name = resource.split("/")[1]
        role_arn = str(ctx.client("iam").get_role(RoleName=role_name)["Role"]["Arn"])
        return CallerIdentity(account=account, partition=partition, principal_arn=role_arn)
    if service == "iam" and resource.startswith("user/"):
        return CallerIdentity(account=account, partition=partition, principal_arn=arn)
    raise ValueError(f"caller {arn!r} is neither an assumed role nor an IAM user")


def discover_data_plane_roles(ctx: SmokeContext) -> frozenset[str]:
    """The execution-role ARNs of every project Lambda holding `<function>-dynamodb-kms`."""
    lambda_client = ctx.client("lambda")
    iam = ctx.client("iam")
    roles: set[str] = set()
    for page in lambda_client.get_paginator("list_functions").paginate():
        for function in page.get("Functions", []):
            name = str(function.get("FunctionName", ""))
            role_arn = str(function.get("Role", ""))
            if not name.startswith(FUNCTION_PREFIX) or ":role/" not in role_arn:
                continue
            role_name = role_arn.rsplit("/", 1)[-1]
            policy_names: set[str] = set()
            for policy_page in iam.get_paginator("list_role_policies").paginate(RoleName=role_name):
                policy_names.update(policy_page.get("PolicyNames", []))
            if f"{name}{DYNAMODB_KMS_POLICY_SUFFIX}" in policy_names:
                roles.add(role_arn)
    return frozenset(roles)


def _operator_arns(ctx: SmokeContext) -> tuple[frozenset[str] | None, str]:
    """The operator ARNs from the outputs, or `None` with a reason (absent or malformed)."""
    raw = ctx.outputs.get(OPERATOR_ARNS_OUTPUT)
    if raw is None:
        return None, "absent"
    if not isinstance(raw, list) or not raw or not all(isinstance(v, str) and v for v in raw):
        return None, f"malformed: {raw!r}"
    return frozenset(raw), ""


def _describe_tables(
    ctx: SmokeContext, deployed: Mapping[str, str]
) -> tuple[list[TableTarget], list[CheckResult]]:
    dynamodb = ctx.client("dynamodb")
    targets: list[TableTarget] = []
    failures: list[CheckResult] = []
    for logical in sorted(deployed):
        physical = deployed[logical]
        try:
            table = dynamodb.describe_table(TableName=physical)["Table"]
        except Exception as exc:  # noqa: BLE001 - reported as a failing row
            failures.append(
                CheckResult(
                    resource=physical,
                    check="describe_table",
                    status="fail",
                    detail=f"describe_table raised {type(exc).__name__}: {exc}",
                )
            )
            continue
        targets.append(
            TableTarget(
                logical=logical,
                physical=physical,
                arn=str(table["TableArn"]),
                sse=table.get("SSEDescription") or {},
                table_status=str(table.get("TableStatus", "")),
            )
        )
    return targets, failures


def _sse_row(target: TableTarget) -> CheckResult:
    """Pass only for an `ACTIVE` table whose CMK is reachable and whose SSE is `ENABLED`."""
    status = target.sse.get("Status")
    inaccessible = target.sse.get("InaccessibleEncryptionDateTime")
    healthy = target.table_status == "ACTIVE" and inaccessible is None and status == "ENABLED"
    return CheckResult(
        resource=target.physical,
        check=f"table_sse_enabled:{target.physical}",
        status="pass" if healthy else "fail",
        detail=(
            f"TableStatus={target.table_status!r}, SSEDescription.Status={status!r}, "
            f"InaccessibleEncryptionDateTime={inaccessible!r}"
        ),
    )


def _shape_row(
    resource: str,
    check: str,
    fetch: Callable[[], str],
    expected: Sequence[ExpectedStatement] | None,
    operator_reason: str,
) -> tuple[CheckResult, str | None]:
    """Fetch a policy and compare it with `expected`; returns the row and the policy text."""
    if expected is None and operator_reason == "absent":
        row = CheckResult(
            resource=resource,
            check=check,
            status=DEFER,
            defer_reason=DEFER_OPERATOR_OUTPUT_ABSENT,
            detail=f"the {OPERATOR_ARNS_OUTPUT} output is absent, so the allowlists are unknown",
        )
        text: str | None = None
        try:
            text = fetch()
        except Exception:  # noqa: BLE001, S110 - the policy text only seeds the probe's no-op body
            text = None
        return row, text
    if expected is None:
        return (
            CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=f"the {OPERATOR_ARNS_OUTPUT} output is {operator_reason}",
            ),
            None,
        )
    try:
        text = fetch()
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return (
            CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=f"reading the policy raised {error_code(exc) or type(exc).__name__}: {exc}",
            ),
            None,
        )
    problems = document_problems(text, expected)
    return (
        CheckResult(
            resource=resource,
            check=check,
            status="fail" if problems else "pass",
            detail="; ".join(problems) if problems else f"{len(expected)} statements as designed",
        ),
        text,
    )


def _probe_key(target: TableTarget, run_id: str) -> dict[str, dict[str, str]]:
    schema = table_schema(target.logical)
    key = {schema.partition_key: {"S": f"probe-{run_id}"}}
    if schema.sort_key is not None:
        key[schema.sort_key] = {"S": f"probe-{run_id}"}
    return key


def _operator_admitted_row(ctx: SmokeContext, target: TableTarget, run_id: str) -> CheckResult:
    check = "operator_data_plane_admitted"
    try:
        ctx.client("dynamodb").get_item(TableName=target.physical, Key=_probe_key(target, run_id))
    except Exception as exc:  # noqa: BLE001 - reported as a failing row
        return CheckResult(
            resource=target.physical,
            check=check,
            status="fail",
            detail=f"the operator's GetItem raised {error_code(exc) or type(exc).__name__}: {exc}",
        )
    return CheckResult(
        resource=target.physical, check=check, status="pass", detail="GetItem returned 200"
    )


def probe_role_name(run_id: str) -> str:
    """The run-scoped probe role's name."""
    return f"{PROBE_ROLE_PREFIX}{run_id}"


def probe_trust_policy(operator_arn: str) -> dict[str, Any]:
    """Trust exactly the operator principal."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": operator_arn},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def probe_identity_policy(target: TableTarget, cmk_arn: str | None) -> dict[str, Any]:
    """Allow exactly the probed actions on the probed table, its indexes, and the CMK."""
    statements: list[dict[str, Any]] = [
        {
            "Sid": "ProbeDynamoDb",
            "Effect": "Allow",
            "Action": list(PROBE_DYNAMODB_ACTIONS),
            "Resource": [target.arn, f"{target.arn}/index/*"],
        }
    ]
    if cmk_arn is not None:
        statements.append(
            {
                "Sid": "ProbeKms",
                "Effect": "Allow",
                "Action": list(PROBE_KMS_ACTIONS),
                "Resource": [cmk_arn],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _placeholder_policy(target: TableTarget, role_arn: str) -> str:
    """A body for the conditional `PutResourcePolicy` when the table has no policy to echo."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ProbePlaceholder",
                    "Effect": "Deny",
                    "Principal": {"AWS": role_arn},
                    "Action": "dynamodb:GetItem",
                    "Resource": target.arn,
                }
            ],
        }
    )


def assume_probe_role(
    ctx: SmokeContext,
    role_arn: str,
    session_name: str,
    *,
    timeout_seconds: float,
    sleeper: Callable[[float], None],
) -> Mapping[str, str]:
    """`AssumeRole` with bounded retry on `AccessDenied` while IAM propagates the new role."""
    deadline = Deadline(seconds=timeout_seconds)
    sts = ctx.client("sts")
    while True:
        try:
            response = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=session_name,
                DurationSeconds=PROBE_SESSION_SECONDS,
            )
            return {str(k): str(v) for k, v in response["Credentials"].items()}
        except Exception as exc:
            if error_code(exc) != "AccessDenied" or deadline.expired:
                raise
            sleeper(min(ASSUME_BACKOFF_SECONDS, deadline.remaining))


def classify_probe(
    call: str, resource: str, outcome: BaseException | None, probe_role: str
) -> CheckResult:
    """Classify one probe call's outcome into its `probe_denied:<call>` row."""
    check = f"probe_denied:{call}"
    if outcome is None:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="the call succeeded: no boundary refused the probe role",
        )
    code = error_code(outcome)
    message = str((getattr(outcome, "response", {}) or {}).get("Error", {}).get("Message", ""))
    quoted = f"{code}: {message}" if code else f"{type(outcome).__name__}: {outcome}"
    if code in _REACHED_RESOURCE_CODES:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"the call reached the resource, no boundary refused it ({quoted})",
        )
    if code in {"AccessDeniedException", "AccessDenied"}:
        lowered = message.lower()
        if EXPLICIT_RESOURCE_DENY in lowered:
            if probe_role.lower() not in lowered:
                return CheckResult(
                    resource=resource,
                    check=check,
                    status="fail",
                    detail=f"the denial does not name the probe role {probe_role} ({quoted})",
                )
            return CheckResult(resource=resource, check=check, status="pass", detail=quoted)
        if "because no " in lowered and "policy allows" in lowered:
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=f"an implicit deny, not the resource-based Deny ({quoted})",
            )
    return CheckResult(
        resource=resource,
        check=check,
        status=DEFER,
        defer_reason=DEFER_PROBE_UNCLASSIFIED,
        detail=quoted,
    )


def probe_requests(
    target: TableTarget, run_id: str, policy_text: str | None, role_arn: str, cmk_arn: str | None
) -> dict[str, tuple[str, str, dict[str, Any]]]:
    """Each probe call as `(service, operation, params)`; every one is non-mutating by construction."""
    schema = table_schema(target.logical)
    gsi = schema.gsi(PROBE_INDEX)
    key = _probe_key(target, run_id)
    requests: dict[str, tuple[str, str, dict[str, Any]]] = {
        PROBE_GET_ITEM: ("dynamodb", "get_item", {"TableName": target.physical, "Key": key}),
        PROBE_QUERY_GSI: (
            "dynamodb",
            "query",
            {
                "TableName": target.physical,
                "IndexName": gsi.name,
                "KeyConditionExpression": "#pk = :pk",
                "ExpressionAttributeNames": {"#pk": gsi.partition_key},
                "ExpressionAttributeValues": {":pk": {"S": f"probe-{run_id}"}},
                "Limit": 1,
            },
        ),
        PROBE_PUT_ITEM: (
            "dynamodb",
            "put_item",
            {
                "TableName": target.physical,
                "Item": key,
                "ConditionExpression": "attribute_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": schema.partition_key},
            },
        ),
        PROBE_PUT_RESOURCE_POLICY: (
            "dynamodb",
            "put_resource_policy",
            {
                "ResourceArn": target.arn,
                "Policy": policy_text or _placeholder_policy(target, role_arn),
                "ExpectedRevisionId": PROBE_EXPECTED_REVISION_ID,
            },
        ),
    }
    if cmk_arn is not None:
        requests[PROBE_GENERATE_DATA_KEY] = (
            "kms",
            "generate_data_key",
            {"KeyId": cmk_arn, "KeySpec": "AES_256"},
        )
    return requests


def _run_probe_calls(
    clients: Mapping[str, Any],
    requests: Mapping[str, tuple[str, str, dict[str, Any]]],
    target: TableTarget,
    cmk_arn: str | None,
    role_name: str,
) -> Iterator[CheckResult]:
    for call in PROBE_CALLS:
        if call not in requests:
            yield CheckResult(
                resource=target.physical,
                check=f"probe_denied:{call}",
                status="fail",
                detail="no DynamoDB CMK was discovered, so the key-policy Deny was not probed",
            )
            continue
        service, operation, params = requests[call]
        resource = cmk_arn if service == "kms" and cmk_arn else target.physical
        outcome: BaseException | None = None
        try:
            getattr(clients[service], operation)(**params)
        except Exception as exc:  # noqa: BLE001 - every outcome is classified
            outcome = exc
        yield classify_probe(call, resource, outcome, role_name)


def _delete_probe_role(ctx: SmokeContext, role_name: str) -> CheckResult:
    iam = ctx.client("iam")
    check = "probe_role_deleted"
    try:
        try:
            iam.delete_role_policy(RoleName=role_name, PolicyName=PROBE_INLINE_POLICY_NAME)
        except Exception as exc:
            if error_code(exc) != "NoSuchEntity":
                raise
        try:
            iam.delete_role(RoleName=role_name)
        except Exception as exc:
            if error_code(exc) != "NoSuchEntity":
                raise
    except Exception as exc:  # noqa: BLE001 - reported as a failing row naming the role
        return CheckResult(
            resource=role_name,
            check=check,
            status="fail",
            detail=(
                f"deleting probe role {role_name} raised {error_code(exc) or type(exc).__name__}: "
                f"{exc}. Delete it by hand."
            ),
        )
    return CheckResult(
        resource=role_name, check=check, status="pass", detail=f"{role_name} deleted"
    )


def _probe(
    ctx: SmokeContext,
    *,
    target: TableTarget,
    cmk_arn: str | None,
    policy_text: str | None,
    identity: CallerIdentity,
    run_id: str,
    probe_client_factory: ProbeClientFactory,
    sleeper: Callable[[float], None],
    assume_timeout_seconds: float,
) -> Iterator[CheckResult]:
    iam = ctx.client("iam")
    role_name = probe_role_name(run_id)
    rows: list[CheckResult] = []
    try:
        role_arn = str(
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(probe_trust_policy(identity.principal_arn)),
                Description="dynamodb_principal_deny smoke probe; deleted by the smoke",
                MaxSessionDuration=3600,
                Tags=[{"Key": k, "Value": v} for k, v in PROBE_ROLE_TAGS],
            )["Role"]["Arn"]
        )
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName=PROBE_INLINE_POLICY_NAME,
            PolicyDocument=json.dumps(probe_identity_policy(target, cmk_arn)),
        )
        credentials = assume_probe_role(
            ctx,
            role_arn,
            role_name,
            timeout_seconds=assume_timeout_seconds,
            sleeper=sleeper,
        )
        clients = {
            "dynamodb": probe_client_factory(ctx, "dynamodb", credentials),
            "kms": probe_client_factory(ctx, "kms", credentials),
        }
        requests = probe_requests(target, run_id, policy_text, role_arn, cmk_arn)
        rows.extend(_run_probe_calls(clients, requests, target, cmk_arn, role_name))
    except Exception as exc:  # noqa: BLE001 - reported as a failing row; teardown still runs
        rows.append(
            CheckResult(
                resource=role_name,
                check="probe_role_setup",
                status="fail",
                detail=(
                    f"setting up the probe raised {error_code(exc) or type(exc).__name__}: {exc}; "
                    "no probe call was classified"
                ),
            )
        )
    finally:
        rows.append(_delete_probe_role(ctx, role_name))
    yield from rows


def run_checks(
    ctx: SmokeContext,
    *,
    probe_client_factory: ProbeClientFactory = session_probe_client,
    sleeper: Callable[[float], None] = time.sleep,
    run_id: str | None = None,
    assume_timeout_seconds: float = ASSUME_TIMEOUT_SECONDS,
) -> Iterator[CheckResult]:
    """Yield every row of this resource class; the seams exist for offline tests."""
    probe_run_id = run_id or uuid.uuid4().hex[:12]
    deployed = {str(k): str(v) for k, v in ctx.outputs.require_mapping(TABLE_NAMES_OUTPUT).items()}
    identity = caller_identity(ctx)
    root_arn = f"arn:{identity.partition}:iam::{identity.account}:root"
    targets, failures = _describe_tables(ctx, deployed)
    yield from failures
    for target in targets:
        yield _sse_row(target)

    discovered = discover_data_plane_roles(ctx)
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="data_plane_roles_discovered",
        status="pass" if discovered else "fail",
        detail=f"{len(discovered)} execution role(s) hold a <function>{DYNAMODB_KMS_POLICY_SUFFIX} policy",
    )

    operators, operator_reason = _operator_arns(ctx)
    data_plane = discovered | operators if operators is not None else None
    administrators = operators | {root_arn} if operators is not None else None

    dynamodb = ctx.client("dynamodb")
    policy_texts: dict[str, str | None] = {}
    for target in targets:
        expected = (
            expected_table_statements(target.arn, data_plane, administrators)
            if data_plane is not None and administrators is not None
            else None
        )

        def fetch(arn: str = target.arn) -> str:
            return str(dynamodb.get_resource_policy(ResourceArn=arn)["Policy"])

        row, text = _shape_row(
            target.physical,
            f"table_policy_shape:{target.physical}",
            fetch,
            expected,
            operator_reason,
        )
        policy_texts[target.logical] = text
        yield row

    cmk_arns = sorted(
        {str(t.sse["KMSMasterKeyArn"]) for t in targets if t.sse.get("KMSMasterKeyArn")}
    )
    cmk_arn = cmk_arns[0] if len(cmk_arns) == 1 else None
    if cmk_arn is None:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="key_policy_shape",
            status="fail",
            detail=f"expected exactly one DynamoDB CMK across the tables, found {cmk_arns}",
        )
    else:
        key_expected = (
            expected_key_statements(root_arn, data_plane, administrators)
            if data_plane is not None and administrators is not None
            else None
        )
        kms = ctx.client("kms")

        def fetch_key_policy() -> str:
            return str(kms.get_key_policy(KeyId=cmk_arn, PolicyName="default")["Policy"])

        key_row, _ = _shape_row(
            cmk_arn, "key_policy_shape", fetch_key_policy, key_expected, operator_reason
        )
        yield key_row

    probe_target = next((t for t in targets if t.logical == PROBE_TABLE), None)
    if probe_target is None:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="probe_role_setup",
            status="fail",
            detail=f"the probe table {PROBE_TABLE!r} is not among the deployed tables",
        )
        return
    yield _operator_admitted_row(ctx, probe_target, probe_run_id)
    yield from _probe(
        ctx,
        target=probe_target,
        cmk_arn=cmk_arn,
        policy_text=policy_texts.get(probe_target.logical),
        identity=identity,
        run_id=probe_run_id,
        probe_client_factory=probe_client_factory,
        sleeper=sleeper,
        assume_timeout_seconds=assume_timeout_seconds,
    )


def checks(ctx: SmokeContext) -> Iterable[CheckResult]:
    """The rows `main` emits, against the real probe-client factory."""
    return run_checks(ctx)


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """Two rows per table, plus the key, discovery, operator, five probe and deletion rows."""
    tables = len(ctx.outputs.require_mapping(TABLE_NAMES_OUTPUT))
    return ExpectedCheckCount(
        minimum=2 * tables + 4 + len(PROBE_CALLS),
        basis=(
            f"2 x {tables} tables in {TABLE_NAMES_OUTPUT} + key_policy_shape + "
            f"data_plane_roles_discovered + operator_data_plane_admitted + {len(PROBE_CALLS)} "
            "probe calls + probe_role_deleted"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the principal-Deny smoke and return the process exit code."""
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
