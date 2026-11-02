# SPDX-License-Identifier: MIT
"""Offline tests for `scripts.smoke.dynamodb_principal_deny` (botocore `Stubber` and `moto`)."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
import scripts.smoke.dynamodb_principal_deny as smoke
from botocore.stub import ANY, Stubber
from moto import mock_aws
from scripts.smoke.harness import CheckResult, context_for_test

from crm.store.schema import TABLE_NAMES, table_schema

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_STUB_CRED = "test"
_RUN_ID = "testrun1"
_PROBE_ROLE = f"crm-agent-eval-probe-{_RUN_ID}"
_PROBE_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/{_PROBE_ROLE}"
_OPERATOR_ROLE = "OperatorRole"
_OPERATOR_ARN = f"arn:aws:iam::{_ACCOUNT}:role/{_OPERATOR_ROLE}"
_ROOT_ARN = f"arn:aws:iam::{_ACCOUNT}:root"
_CMK_ARN = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/11111111-1111-1111-1111-111111111111"
_DATA_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-accounts-get-role"
_RELAY_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-agent-relay-role"
_TABLES = {name: f"crm-agent-eval-{name}" for name in sorted(TABLE_NAMES)}
_OUTPUTS: dict[str, Any] = {
    "dynamodb_table_names": _TABLES,
    "crm_operator_principal_arns": [_OPERATOR_ARN],
}

#: The action tables, literal here so a drift in the module constants reds.
_DESIGN_TABLE_DATA_PLANE = [
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
]
_DESIGN_TABLE_ADMIN = [
    "dynamodb:DeleteTable",
    "dynamodb:UpdateTable",
    "dynamodb:UpdateContinuousBackups",
    "dynamodb:UpdateTimeToLive",
    "dynamodb:PutResourcePolicy",
    "dynamodb:DeleteResourcePolicy",
    "dynamodb:TagResource",
    "dynamodb:UntagResource",
    "dynamodb:UpdateContributorInsights",
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


def _table_arn(physical: str) -> str:
    return f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/{physical}"


def _table_policy(physical: str, **overrides: Any) -> dict[str, Any]:
    arn = _table_arn(physical)
    data_plane = overrides.pop("data_plane", [_DATA_ROLE_ARN, _OPERATOR_ARN])
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyDataPlaneToNonProjectPrincipals",
                "Effect": "Deny",
                "Principal": "*",
                "Action": list(_DESIGN_TABLE_DATA_PLANE),
                "Resource": [arn, f"{arn}/index/*"],
                "Condition": {"ArnNotEquals": {"aws:PrincipalArn": data_plane}},
            },
            {
                "Sid": "DenyTableAdministrationToNonOperators",
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": list(_DESIGN_TABLE_ADMIN),
                "Resource": [arn, f"{arn}/index/*"],
                "Condition": {"ArnNotEquals": {"aws:PrincipalArn": [_OPERATOR_ARN, _ROOT_ARN]}},
            },
        ],
    }


def _key_policy() -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Id": "key-default-1",
        "Statement": [
            {
                "Sid": "Enable IAM User Permissions",
                "Effect": "Allow",
                "Principal": {"AWS": _ROOT_ARN},
                "Action": "kms:*",
                "Resource": "*",
            },
            {
                "Sid": "DenyKeyUseToNonProjectPrincipals",
                "Effect": "Deny",
                "Principal": "*",
                "Action": list(_DESIGN_KEY_USE),
                "Resource": "*",
                "Condition": {
                    "ArnNotEquals": {"aws:PrincipalArn": [_DATA_ROLE_ARN, _OPERATOR_ARN]},
                    "Bool": {"aws:PrincipalIsAWSService": "false"},
                },
            },
            {
                "Sid": "DenyKeyAdministrationToNonOperators",
                "Effect": "Deny",
                "Principal": "*",
                "Action": list(_DESIGN_KEY_ADMIN),
                "Resource": "*",
                "Condition": {"ArnNotEquals": {"aws:PrincipalArn": [_OPERATOR_ARN, _ROOT_ARN]}},
            },
        ],
    }


def _explicit_deny(action: str, resource: str, principal: str = _PROBE_ROLE) -> str:
    return (
        f"User: arn:aws:sts::{_ACCOUNT}:assumed-role/{principal}/{principal} is not authorized to "
        f"perform: {action} on resource: {resource} with an explicit deny in a resource-based policy"
    )


def _implicit_deny(action: str, resource: str) -> str:
    return (
        f"User: arn:aws:sts::{_ACCOUNT}:assumed-role/{_PROBE_ROLE}/{_PROBE_ROLE} is not authorized "
        f"to perform: {action} on resource: {resource} because no identity-based policy allows the "
        f"{action} action"
    )


def _client(service: str) -> Any:
    """A real boto3 client whose calls `Stubber` intercepts before any network I/O."""
    return boto3.client(
        service,
        region_name=_REGION,
        aws_access_key_id=_STUB_CRED,
        aws_secret_access_key=_STUB_CRED,
    )


#: One probe-call outcome: `None` is a 200, a `(code, message)` pair is a client error.
Outcome = tuple[str, str] | None


def _deny_outcomes() -> dict[str, Outcome]:
    accounts = _table_arn(_TABLES["accounts"])
    return {
        smoke.PROBE_GET_ITEM: (
            "AccessDeniedException",
            _explicit_deny("dynamodb:GetItem", accounts),
        ),
        smoke.PROBE_QUERY_GSI: (
            "AccessDeniedException",
            _explicit_deny("dynamodb:Query", f"{accounts}/index/gsi_tenant"),
        ),
        smoke.PROBE_PUT_ITEM: (
            "AccessDeniedException",
            _explicit_deny("dynamodb:PutItem", accounts),
        ),
        smoke.PROBE_PUT_RESOURCE_POLICY: (
            "AccessDeniedException",
            _explicit_deny("dynamodb:PutResourcePolicy", accounts),
        ),
        smoke.PROBE_GENERATE_DATA_KEY: (
            "AccessDeniedException",
            _explicit_deny("kms:GenerateDataKey", _CMK_ARN),
        ),
    }


class Scenario:
    """Queues every operator and probe response `run_checks` consumes, in its call order."""

    def __init__(self) -> None:
        self.clients = {s: _client(s) for s in ("sts", "iam", "lambda", "dynamodb", "kms")}
        self.stubs = {s: Stubber(c) for s, c in self.clients.items()}
        self.probe_clients = {s: _client(s) for s in ("dynamodb", "kms")}
        self.probe_stubs = {s: Stubber(c) for s, c in self.probe_clients.items()}
        self.table_policies: dict[str, Any] = {
            logical: _table_policy(physical) for logical, physical in _TABLES.items()
        }
        self.key_policy: Any = _key_policy()
        self.sse_status: dict[str, str] = dict.fromkeys(_TABLES, "ENABLED")
        self.table_status: dict[str, str] = dict.fromkeys(_TABLES, "ACTIVE")
        self.inaccessible: set[str] = set()
        self.outcomes: dict[str, Outcome] = _deny_outcomes()
        self.operator_get_item_error: tuple[str, str] | None = None
        self.assume_denials = 0
        self.create_role_error: str | None = None
        self.delete_role_error: str | None = None
        self.fetch_policies = True
        self.probe_params: dict[str, dict[str, Any]] = {}

    def _queue_identity(self) -> None:
        self.stubs["sts"].add_response(
            "get_caller_identity",
            {
                "UserId": "operator-user-id:sess",
                "Account": _ACCOUNT,
                "Arn": f"arn:aws:sts::{_ACCOUNT}:assumed-role/{_OPERATOR_ROLE}/sess",
            },
        )
        self.stubs["iam"].add_response(
            "get_role", _role_response(_OPERATOR_ROLE, _OPERATOR_ARN), {"RoleName": _OPERATOR_ROLE}
        )

    def _queue_tables(self) -> None:
        for logical, physical in _TABLES.items():
            self.stubs["dynamodb"].add_response(
                "describe_table",
                {
                    "Table": {
                        "TableName": physical,
                        "TableArn": _table_arn(physical),
                        "TableStatus": self.table_status[logical],
                        "SSEDescription": {
                            "Status": self.sse_status[logical],
                            "SSEType": "KMS",
                            "KMSMasterKeyArn": _CMK_ARN,
                            **(
                                {
                                    "InaccessibleEncryptionDateTime": datetime(
                                        2026, 10, 20, tzinfo=UTC
                                    )
                                }
                                if logical in self.inaccessible
                                else {}
                            ),
                        },
                    }
                },
                {"TableName": physical},
            )

    def _queue_discovery(self) -> None:
        self.stubs["lambda"].add_response(
            "list_functions",
            {
                "Functions": [
                    {"FunctionName": "crm-agent-eval-accounts-get", "Role": _DATA_ROLE_ARN},
                    {"FunctionName": "crm-agent-eval-agent-relay", "Role": _RELAY_ROLE_ARN},
                    {"FunctionName": "co-tenant-fn", "Role": f"arn:aws:iam::{_ACCOUNT}:role/x"},
                ]
            },
        )
        self.stubs["iam"].add_response(
            "list_role_policies",
            {
                "PolicyNames": ["crm-agent-eval-accounts-get-dynamodb-kms", "logs"],
                "IsTruncated": False,
            },
            {"RoleName": "crm-agent-eval-accounts-get-role"},
        )
        self.stubs["iam"].add_response(
            "list_role_policies",
            {"PolicyNames": ["logs"], "IsTruncated": False},
            {"RoleName": "crm-agent-eval-agent-relay-role"},
        )

    def _queue_policies(self) -> None:
        if not self.fetch_policies:
            return
        for logical, physical in _TABLES.items():
            policy = self.table_policies[logical]
            if policy is None:
                self.stubs["dynamodb"].add_client_error(
                    "get_resource_policy",
                    service_error_code="PolicyNotFoundException",
                    service_message="Resource-based policy not found",
                    http_status_code=400,
                )
                continue
            self.stubs["dynamodb"].add_response(
                "get_resource_policy",
                {"Policy": json.dumps(policy), "RevisionId": "1792550371863"},
                {"ResourceArn": _table_arn(physical)},
            )
        self.stubs["kms"].add_response(
            "get_key_policy",
            {"Policy": json.dumps(self.key_policy), "PolicyName": "default"},
            {"KeyId": _CMK_ARN, "PolicyName": "default"},
        )

    def _queue_operator_get_item(self) -> None:
        accounts = _TABLES["accounts"]
        params = {"TableName": accounts, "Key": {"account_id": {"S": f"probe-{_RUN_ID}"}}}
        if self.operator_get_item_error:
            code, message = self.operator_get_item_error
            self.stubs["dynamodb"].add_client_error(
                "get_item", service_error_code=code, service_message=message, http_status_code=400
            )
        else:
            self.stubs["dynamodb"].add_response("get_item", {}, params)

    def _queue_probe_setup(self) -> None:
        iam = self.stubs["iam"]
        if self.create_role_error:
            iam.add_client_error(
                "create_role",
                service_error_code=self.create_role_error,
                service_message="refused",
                http_status_code=400,
            )
            return
        iam.add_response("create_role", _role_response(_PROBE_ROLE, _PROBE_ROLE_ARN), ANY_PARAMS)
        iam.add_response("put_role_policy", {}, ANY_PARAMS)
        for _ in range(self.assume_denials):
            self.stubs["sts"].add_client_error(
                "assume_role",
                service_error_code="AccessDenied",
                service_message="not authorized to perform: sts:AssumeRole",
                http_status_code=403,
            )
        self.stubs["sts"].add_response(
            "assume_role",
            {
                "Credentials": {
                    "AccessKeyId": "test-access-key-id",
                    "SecretAccessKey": _STUB_CRED,
                    "SessionToken": _STUB_CRED,
                    "Expiration": datetime(2026, 10, 20, tzinfo=UTC),
                },
                "AssumedRoleUser": {
                    "AssumedRoleId": "probe-role-id:probe",
                    "Arn": f"arn:aws:sts::{_ACCOUNT}:assumed-role/{_PROBE_ROLE}/{_PROBE_ROLE}",
                },
            },
            {"RoleArn": _PROBE_ROLE_ARN, "RoleSessionName": _PROBE_ROLE, "DurationSeconds": 900},
        )
        self._queue_probe_calls()

    def _queue_probe_calls(self) -> None:
        accounts = _TABLES["accounts"]
        arn = _table_arn(accounts)
        key = {"account_id": {"S": f"probe-{_RUN_ID}"}}
        expected: dict[str, tuple[str, str, dict[str, Any]]] = {
            smoke.PROBE_GET_ITEM: ("dynamodb", "get_item", {"TableName": accounts, "Key": key}),
            smoke.PROBE_QUERY_GSI: (
                "dynamodb",
                "query",
                {
                    "TableName": accounts,
                    "IndexName": "gsi_tenant",
                    "KeyConditionExpression": "#pk = :pk",
                    "ExpressionAttributeNames": {"#pk": "tenant"},
                    "ExpressionAttributeValues": {":pk": {"S": f"probe-{_RUN_ID}"}},
                    "Limit": 1,
                },
            ),
            smoke.PROBE_PUT_ITEM: (
                "dynamodb",
                "put_item",
                {
                    "TableName": accounts,
                    "Item": key,
                    "ConditionExpression": "attribute_exists(#pk)",
                    "ExpressionAttributeNames": {"#pk": "account_id"},
                },
            ),
            smoke.PROBE_PUT_RESOURCE_POLICY: (
                "dynamodb",
                "put_resource_policy",
                {
                    "ResourceArn": arn,
                    "Policy": ANY,
                    "ExpectedRevisionId": "probe-nonexistent-revision",
                },
            ),
            smoke.PROBE_GENERATE_DATA_KEY: (
                "kms",
                "generate_data_key",
                {"KeyId": _CMK_ARN, "KeySpec": "AES_256"},
            ),
        }
        for call, (service, operation, params) in expected.items():
            self.probe_params[call] = params
            outcome = self.outcomes[call]
            stub = self.probe_stubs[service]
            if outcome is None:
                stub.add_response(operation, _success(operation), params)
            else:
                code, message = outcome
                stub.add_client_error(
                    operation,
                    service_error_code=code,
                    service_message=message,
                    http_status_code=400,
                    expected_params=params,
                )

    def _queue_teardown(self) -> None:
        iam = self.stubs["iam"]
        if self.delete_role_error:
            iam.add_response("delete_role_policy", {}, ANY_PARAMS)
            iam.add_client_error(
                "delete_role",
                service_error_code=self.delete_role_error,
                service_message="Cannot delete entity, must remove roles from instance profile",
                http_status_code=409,
            )
            return
        if self.create_role_error:
            for operation in ("delete_role_policy", "delete_role"):
                iam.add_client_error(
                    operation,
                    service_error_code="NoSuchEntity",
                    service_message="The role cannot be found",
                    http_status_code=404,
                )
            return
        iam.add_response(
            "delete_role_policy", {}, {"RoleName": _PROBE_ROLE, "PolicyName": "probe-allow"}
        )
        iam.add_response("delete_role", {}, {"RoleName": _PROBE_ROLE})

    def run(self, outputs: dict[str, Any] | None = None) -> list[CheckResult]:
        """Queue every response, run the smoke, and assert every queued response was consumed."""
        self._queue_identity()
        self._queue_tables()
        self._queue_discovery()
        self._queue_policies()
        self._queue_operator_get_item()
        self._queue_probe_setup()
        self._queue_teardown()
        for stub in [*self.stubs.values(), *self.probe_stubs.values()]:
            stub.activate()
        ctx = context_for_test(outputs=outputs or _OUTPUTS, clients=self.clients)
        rows = list(
            smoke.run_checks(
                ctx,
                probe_client_factory=lambda _ctx, service, _creds: self.probe_clients[service],
                sleeper=lambda _s: None,
                run_id=_RUN_ID,
            )
        )
        for stub in [*self.stubs.values(), *self.probe_stubs.values()]:
            stub.assert_no_pending_responses()
        return rows


ANY_PARAMS: Any = None


def _role_response(name: str, arn: str) -> dict[str, Any]:
    return {
        "Role": {
            "Path": "/",
            "RoleName": name,
            "RoleId": "test-role-id-00001",
            "Arn": arn,
            "CreateDate": datetime(2026, 10, 20, tzinfo=UTC),
        }
    }


def _success(operation: str) -> dict[str, Any]:
    if operation == "generate_data_key":
        return {"CiphertextBlob": b"cipher", "Plaintext": b"PLAINTEXT-SECRET", "KeyId": _CMK_ARN}
    if operation == "put_resource_policy":
        return {"RevisionId": "1"}
    if operation == "query":
        return {"Items": [], "Count": 0}
    return {}


def _by_check(rows: list[CheckResult]) -> dict[str, CheckResult]:
    by: dict[str, CheckResult] = {}
    for row in rows:
        assert row.check not in by, f"duplicate row {row.check}"
        by[row.check] = row
    return by


# ---------------------------------------------------------------------------
# Constants match the action tables above.
# ---------------------------------------------------------------------------


def test_module_constants_are_the_design_action_tuples_in_order() -> None:
    assert list(smoke.TABLE_DATA_PLANE_ACTIONS) == _DESIGN_TABLE_DATA_PLANE
    assert list(smoke.TABLE_ADMIN_ACTIONS) == _DESIGN_TABLE_ADMIN
    assert list(smoke.KEY_USE_ACTIONS) == _DESIGN_KEY_USE
    assert list(smoke.KEY_ADMIN_ACTIONS) == _DESIGN_KEY_ADMIN
    assert (
        smoke.TABLE_DATA_PLANE_SID,
        smoke.TABLE_ADMIN_SID,
        smoke.KEY_ROOT_SID,
        smoke.KEY_USE_SID,
        smoke.KEY_ADMIN_SID,
    ) == (
        "DenyDataPlaneToNonProjectPrincipals",
        "DenyTableAdministrationToNonOperators",
        "Enable IAM User Permissions",
        "DenyKeyUseToNonProjectPrincipals",
        "DenyKeyAdministrationToNonOperators",
    )


def test_probe_table_and_index_exist_in_the_schema() -> None:
    assert table_schema(smoke.PROBE_TABLE).gsi(smoke.PROBE_INDEX).name == smoke.PROBE_INDEX


# ---------------------------------------------------------------------------
# The post-apply happy path.
# ---------------------------------------------------------------------------


def test_a_deployed_boundary_passes_every_row_and_deletes_the_probe() -> None:
    rows = Scenario().run()
    by = _by_check(rows)
    assert all(r.status == "pass" for r in rows), [r for r in rows if r.status != "pass"]
    for physical in _TABLES.values():
        assert f"table_policy_shape:{physical}" in by
        assert f"table_sse_enabled:{physical}" in by
    for call in smoke.PROBE_CALLS:
        assert by[f"probe_denied:{call}"].status == "pass"
        assert "explicit deny in a resource-based policy" in by[f"probe_denied:{call}"].detail
    assert by["probe_role_deleted"].resource == _PROBE_ROLE
    assert len(rows) >= smoke.expected_check_count(context_for_test(outputs=_OUTPUTS)).minimum


def test_the_probe_role_trusts_only_the_operator_and_allows_only_the_probed_actions() -> None:
    target = smoke.TableTarget("accounts", "t", _table_arn("t"), {})
    trust = smoke.probe_trust_policy(_OPERATOR_ARN)
    assert trust["Statement"] == [
        {"Effect": "Allow", "Principal": {"AWS": _OPERATOR_ARN}, "Action": "sts:AssumeRole"}
    ]
    identity = smoke.probe_identity_policy(target, _CMK_ARN)["Statement"]
    assert {a for s in identity for a in s["Action"]} == {
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:PutItem",
        "dynamodb:PutResourcePolicy",
        "kms:GenerateDataKey",
    }
    assert {r for s in identity for r in s["Resource"]} == {
        target.arn,
        f"{target.arn}/index/*",
        _CMK_ARN,
    }
    assert all(s["Effect"] == "Allow" for s in identity)
    assert smoke.probe_role_name(_RUN_ID) == _PROBE_ROLE


def test_the_probe_role_is_created_tagged_with_the_operator_trust() -> None:
    scenario = Scenario()
    captured: dict[str, Any] = {}
    original = scenario.clients["iam"].create_role

    def capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return original(**kwargs)

    scenario.clients["iam"].create_role = capture
    scenario.run()
    assert captured["RoleName"] == _PROBE_ROLE
    assert json.loads(captured["AssumeRolePolicyDocument"]) == smoke.probe_trust_policy(
        _OPERATOR_ARN
    )
    assert {t["Key"]: t["Value"] for t in captured["Tags"]} == {
        "service": "crm-agent-eval",
        "data-classification": "internal",
        "purpose": "smoke-probe",
    }


# ---------------------------------------------------------------------------
# Probe-call classification: pass only on an explicit resource-based deny naming the probe.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call", smoke.PROBE_CALLS)
def test_a_successful_probe_call_fails(call: str) -> None:
    scenario = Scenario()
    scenario.outcomes[call] = None
    row = _by_check(scenario.run())[f"probe_denied:{call}"]
    assert row.status == "fail"
    assert "succeeded" in row.detail


def test_generate_data_key_plaintext_never_reaches_a_row() -> None:
    scenario = Scenario()
    scenario.outcomes[smoke.PROBE_GENERATE_DATA_KEY] = None
    rows = scenario.run()
    assert not any("PLAINTEXT-SECRET" in r.detail for r in rows)


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (smoke.PROBE_PUT_ITEM, "ConditionalCheckFailedException"),
        (smoke.PROBE_PUT_RESOURCE_POLICY, "PolicyNotFoundException"),
    ],
)
def test_reaching_the_resource_fails(call: str, code: str) -> None:
    scenario = Scenario()
    scenario.outcomes[call] = (code, "The conditional request failed")
    row = _by_check(scenario.run())[f"probe_denied:{call}"]
    assert row.status == "fail"
    assert code in row.detail


@pytest.mark.parametrize("call", smoke.PROBE_CALLS)
def test_an_implicit_deny_fails(call: str) -> None:
    scenario = Scenario()
    scenario.outcomes[call] = ("AccessDeniedException", _implicit_deny("dynamodb:GetItem", "x"))
    row = _by_check(scenario.run())[f"probe_denied:{call}"]
    assert row.status == "fail"
    assert "implicit deny" in row.detail


def test_an_explicit_deny_naming_another_principal_fails() -> None:
    scenario = Scenario()
    scenario.outcomes[smoke.PROBE_GET_ITEM] = (
        "AccessDeniedException",
        _explicit_deny("dynamodb:GetItem", "x", principal="SomeoneElse"),
    )
    row = _by_check(scenario.run())[f"probe_denied:{smoke.PROBE_GET_ITEM}"]
    assert row.status == "fail"
    assert _PROBE_ROLE in row.detail


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (
            "AccessDeniedException",
            "User: x is not authorized to perform: dynamodb:GetItem with an explicit deny in a "
            "service control policy",
        ),
        ("ProvisionedThroughputExceededException", "Rate exceeded"),
        ("ValidationException", "1 validation error detected"),
    ],
)
def test_any_other_error_defers_with_its_message(code: str, message: str) -> None:
    scenario = Scenario()
    scenario.outcomes[smoke.PROBE_QUERY_GSI] = (code, message)
    row = _by_check(scenario.run())[f"probe_denied:{smoke.PROBE_QUERY_GSI}"]
    assert row.status == "defer"
    assert row.defer_reason == "probe_call_unclassified_error"
    assert message in row.detail


def test_the_pass_detail_quotes_the_denial() -> None:
    row = _by_check(Scenario().run())[f"probe_denied:{smoke.PROBE_GET_ITEM}"]
    assert row.detail.startswith("AccessDeniedException: User: ")


# ---------------------------------------------------------------------------
# Every probe call is non-mutating by construction.
# ---------------------------------------------------------------------------


def test_put_item_is_conditional_on_the_partition_key_of_a_probe_key() -> None:
    params = _probe_requests()[smoke.PROBE_PUT_ITEM][2]
    assert params["ConditionExpression"] == "attribute_exists(#pk)"
    assert params["ExpressionAttributeNames"] == {"#pk": "account_id"}
    assert params["Item"] == {"account_id": {"S": f"probe-{_RUN_ID}"}}


def test_put_resource_policy_carries_an_impossible_revision_and_echoes_the_current_policy() -> None:
    current = json.dumps(_table_policy("t"))
    params = _probe_requests(policy_text=current)[smoke.PROBE_PUT_RESOURCE_POLICY][2]
    assert params["ExpectedRevisionId"] == "probe-nonexistent-revision"
    assert params["Policy"] == current
    assert "ConfirmRemoveSelfResourceAccess" not in params


@pytest.mark.parametrize("policy_text", [None, '{"Version": "2012-10-17", "Statement": []}'])
def test_the_probe_revision_is_never_no_policy(policy_text: str | None) -> None:
    """`NO_POLICY` would attach the probe's document to a table that has no policy yet."""
    params = _probe_requests(policy_text=policy_text)[smoke.PROBE_PUT_RESOURCE_POLICY][2]
    assert params["ExpectedRevisionId"] != "NO_POLICY"
    assert smoke.PROBE_EXPECTED_REVISION_ID != "NO_POLICY"


def test_put_resource_policy_without_a_current_policy_still_carries_the_revision() -> None:
    params = _probe_requests(policy_text=None)[smoke.PROBE_PUT_RESOURCE_POLICY][2]
    assert params["ExpectedRevisionId"] == "probe-nonexistent-revision"
    assert json.loads(params["Policy"])["Statement"][0]["Effect"] == "Deny"


def test_get_item_query_and_generate_data_key_are_reads() -> None:
    requests = _probe_requests()
    assert requests[smoke.PROBE_GET_ITEM][:2] == ("dynamodb", "get_item")
    assert requests[smoke.PROBE_QUERY_GSI][:2] == ("dynamodb", "query")
    assert requests[smoke.PROBE_GENERATE_DATA_KEY][:2] == ("kms", "generate_data_key")


def test_probe_requests_without_a_cmk_omit_the_kms_call_and_fail_its_row() -> None:
    assert smoke.PROBE_GENERATE_DATA_KEY not in _probe_requests(cmk_arn=None)
    target = smoke.TableTarget("accounts", "t", _table_arn("t"), {})
    rows = list(smoke._run_probe_calls({}, {}, target, None, _PROBE_ROLE))
    assert {r.status for r in rows} == {"fail"}


def _probe_requests(
    *, policy_text: str | None = None, cmk_arn: str | None = _CMK_ARN
) -> dict[str, tuple[str, str, dict[str, Any]]]:
    target = smoke.TableTarget("accounts", "t", _table_arn("t"), {})
    return smoke.probe_requests(target, _RUN_ID, policy_text, _PROBE_ROLE_ARN, cmk_arn)


# ---------------------------------------------------------------------------
# Policy shape.
# ---------------------------------------------------------------------------


def _accounts_policy_mutated(mutate: Callable[[dict[str, Any]], None]) -> Scenario:
    scenario = Scenario()
    policy = scenario.table_policies["accounts"]
    mutate(policy)
    return scenario


def _first(policy: dict[str, Any]) -> dict[str, Any]:
    statement: dict[str, Any] = policy["Statement"][0]
    return statement


def _second(policy: dict[str, Any]) -> dict[str, Any]:
    statement: dict[str, Any] = policy["Statement"][1]
    return statement


_TABLE_MUTANTS: dict[str, Callable[[dict[str, Any]], None]] = {
    "missing_action": lambda p: _first(p)["Action"].remove("dynamodb:Scan"),
    "extra_action": lambda p: _second(p)["Action"].append("dynamodb:DescribeTable"),
    "if_exists": lambda p: _first(p).update(
        Condition={"ArnNotEqualsIfExists": {"aws:PrincipalArn": [_DATA_ROLE_ARN, _OPERATOR_ARN]}}
    ),
    "string_not_equals": lambda p: _first(p).update(
        Condition={"StringNotEquals": {"aws:PrincipalArn": [_DATA_ROLE_ARN, _OPERATOR_ARN]}}
    ),
    "extra_via_aws_service": lambda p: _first(p)["Condition"].update(
        Bool={"aws:ViaAWSService": "false"}
    ),
    "principal_is_aws_service_on_table": lambda p: _first(p)["Condition"].update(
        Bool={"aws:PrincipalIsAWSService": "false"}
    ),
    "missing_discovered_role": lambda p: _first(p)["Condition"]["ArnNotEquals"].update(
        {"aws:PrincipalArn": [_OPERATOR_ARN]}
    ),
    "root_in_data_plane": lambda p: _first(p)["Condition"]["ArnNotEquals"][
        "aws:PrincipalArn"
    ].append(_ROOT_ARN),
    "root_missing_from_admin": lambda p: _second(p)["Condition"]["ArnNotEquals"].update(
        {"aws:PrincipalArn": [_OPERATOR_ARN]}
    ),
    "named_principal": lambda p: _first(p).update(Principal={"AWS": _OPERATOR_ARN}),
    "allow_effect": lambda p: _first(p).update(Effect="Allow"),
    "index_resource_missing": lambda p: _first(p)["Resource"].pop(),
    "not_action": lambda p: _first(p).update(NotAction=["dynamodb:DescribeTable"]),
    "statement_absent": lambda p: p["Statement"].pop(),
    "extra_statement": lambda p: p["Statement"].append(
        {"Sid": "AllowAll", "Effect": "Allow", "Principal": "*", "Action": "dynamodb:*"}
    ),
}


@pytest.mark.parametrize("mutant", sorted(_TABLE_MUTANTS))
def test_each_table_policy_weakening_reds_only_that_tables_shape_row(mutant: str) -> None:
    rows = _accounts_policy_mutated(_TABLE_MUTANTS[mutant]).run()
    failing = [r.check for r in rows if r.status == "fail"]
    assert failing == [f"table_policy_shape:{_TABLES['accounts']}"], failing


def test_a_missing_table_policy_fails_its_shape_row() -> None:
    scenario = Scenario()
    scenario.table_policies["contacts"] = None
    row = _by_check(scenario.run())[f"table_policy_shape:{_TABLES['contacts']}"]
    assert row.status == "fail"
    assert "PolicyNotFoundException" in row.detail


_KEY_MUTANTS: dict[str, Callable[[dict[str, Any]], None]] = {
    "principal_is_aws_service_dropped": lambda p: p["Statement"][1]["Condition"].pop("Bool"),
    "principal_is_aws_service_true": lambda p: p["Statement"][1]["Condition"].update(
        Bool={"aws:PrincipalIsAWSService": "true"}
    ),
    "principal_is_aws_service_on_admin": lambda p: p["Statement"][2]["Condition"].update(
        Bool={"aws:PrincipalIsAWSService": "false"}
    ),
    "root_statement_absent": lambda p: p["Statement"].pop(0),
    "root_statement_conditioned": lambda p: p["Statement"][0].update(
        Condition={"Bool": {"aws:SecureTransport": "true"}}
    ),
    "missing_key_use_action": lambda p: p["Statement"][1]["Action"].remove("kms:CreateGrant"),
    "missing_key_admin_action": lambda p: p["Statement"][2]["Action"].remove("kms:PutKeyPolicy"),
    "root_in_key_use": lambda p: p["Statement"][1]["Condition"]["ArnNotEquals"][
        "aws:PrincipalArn"
    ].append(_ROOT_ARN),
}


@pytest.mark.parametrize("mutant", sorted(_KEY_MUTANTS))
def test_each_key_policy_weakening_reds_only_the_key_shape_row(mutant: str) -> None:
    scenario = Scenario()
    _KEY_MUTANTS[mutant](scenario.key_policy)
    failing = [r.check for r in scenario.run() if r.status == "fail"]
    assert failing == ["key_policy_shape"], failing


def test_the_shape_rows_defer_when_the_operator_output_is_absent() -> None:
    outputs = {"dynamodb_table_names": _TABLES}
    rows = Scenario().run(outputs=outputs)
    shape = [r for r in rows if "policy_shape" in r.check]
    assert len(shape) == len(_TABLES) + 1
    assert {r.status for r in shape} == {"defer"}
    assert {r.defer_reason for r in shape} == {"operator_principal_output_absent"}


def test_a_malformed_operator_output_fails_the_shape_rows() -> None:
    scenario = Scenario()
    scenario.fetch_policies = False
    rows = scenario.run(outputs={**_OUTPUTS, "crm_operator_principal_arns": []})
    shape = [r for r in rows if "policy_shape" in r.check]
    assert {r.status for r in shape} == {"fail"}


def test_a_table_without_enabled_sse_fails_its_row() -> None:
    scenario = Scenario()
    scenario.sse_status["contacts"] = "UPDATING"
    failing = [r.check for r in scenario.run() if r.status == "fail"]
    assert failing == [f"table_sse_enabled:{_TABLES['contacts']}"]


def test_key_loss_with_sse_still_enabled_fails_its_row() -> None:
    scenario = Scenario()
    scenario.inaccessible.add("contacts")
    scenario.table_status["contacts"] = "INACCESSIBLE_ENCRYPTION_CREDENTIALS"
    by = _by_check(scenario.run())
    row = by[f"table_sse_enabled:{_TABLES['contacts']}"]
    assert row.status == "fail"
    assert "InaccessibleEncryptionDateTime" in row.detail


def test_an_inaccessible_key_date_alone_fails_even_when_active_and_enabled() -> None:
    scenario = Scenario()
    scenario.inaccessible.add("contacts")
    failing = [r.check for r in scenario.run() if r.status == "fail"]
    assert failing == [f"table_sse_enabled:{_TABLES['contacts']}"]


def test_a_non_active_table_fails_its_sse_row() -> None:
    scenario = Scenario()
    scenario.table_status["contacts"] = "UPDATING"
    failing = [r.check for r in scenario.run() if r.status == "fail"]
    assert failing == [f"table_sse_enabled:{_TABLES['contacts']}"]


def test_discovery_keeps_only_project_roles_holding_the_dynamodb_kms_policy() -> None:
    row = _by_check(Scenario().run())["data_plane_roles_discovered"]
    assert row.status == "pass"
    assert row.detail.startswith("1 execution role")


# ---------------------------------------------------------------------------
# Operator path, probe setup and teardown.
# ---------------------------------------------------------------------------


def test_an_operator_refused_on_the_data_plane_fails() -> None:
    scenario = Scenario()
    scenario.operator_get_item_error = (
        "AccessDeniedException",
        _explicit_deny("dynamodb:GetItem", "x", principal=_OPERATOR_ROLE),
    )
    row = _by_check(scenario.run())["operator_data_plane_admitted"]
    assert row.status == "fail"


def test_assume_role_retries_through_iam_propagation() -> None:
    scenario = Scenario()
    scenario.assume_denials = 2
    rows = scenario.run()
    assert all(r.status == "pass" for r in rows)


def test_assume_role_exhaustion_is_a_setup_failure_and_the_role_is_still_deleted() -> None:
    ctx_sts = _client("sts")
    stub = Stubber(ctx_sts)
    stub.add_client_error(
        "assume_role", service_error_code="AccessDenied", service_message="no", http_status_code=403
    )
    stub.activate()
    ctx = context_for_test(clients={"sts": ctx_sts})
    with pytest.raises(Exception, match="AccessDenied"):
        smoke.assume_probe_role(
            ctx, _PROBE_ROLE_ARN, _PROBE_ROLE, timeout_seconds=0.0, sleeper=lambda _s: None
        )


def test_a_create_role_failure_is_a_setup_fail_row_and_teardown_still_runs() -> None:
    scenario = Scenario()
    scenario.create_role_error = "LimitExceeded"
    by = _by_check(scenario.run())
    assert by["probe_role_setup"].status == "fail"
    assert by["probe_role_deleted"].status == "pass"
    assert not any(c.startswith("probe_denied:") for c in by)


def test_a_role_deletion_failure_is_a_fail_row_naming_the_role() -> None:
    scenario = Scenario()
    scenario.delete_role_error = "DeleteConflict"
    row = _by_check(scenario.run())["probe_role_deleted"]
    assert row.status == "fail"
    assert _PROBE_ROLE in row.detail


def test_a_non_role_non_user_caller_is_refused() -> None:
    sts = _client("sts")
    stub = Stubber(sts)
    stub.add_response(
        "get_caller_identity",
        {"UserId": "x", "Account": _ACCOUNT, "Arn": f"arn:aws:iam::{_ACCOUNT}:root"},
    )
    stub.activate()
    with pytest.raises(ValueError, match="neither an assumed role nor an IAM user"):
        smoke.caller_identity(context_for_test(clients={"sts": sts}))


def test_the_expected_check_count_is_derived_from_the_table_output() -> None:
    expected = smoke.expected_check_count(context_for_test(outputs=_OUTPUTS))
    assert expected.minimum == 2 * len(_TABLES) + 4 + 5


# ---------------------------------------------------------------------------
# T2 against moto: with no boundary at all, nothing is written and every probe row fails.
# ---------------------------------------------------------------------------


@pytest.fixture
def moto_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", _STUB_CRED)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _STUB_CRED)
    with mock_aws():
        yield


def _lambda_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("h.py", "def h(event, context):\n    return None\n")
    return buffer.getvalue()


def test_pre_apply_against_moto_writes_nothing_and_fails_every_probe_row(moto_env: None) -> None:
    trust_any = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{_ACCOUNT}:root"},
                    "Action": "sts:AssumeRole",
                },
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                },
            ],
        }
    )
    iam = boto3.client("iam", region_name=_REGION)
    operator_arn = iam.create_role(RoleName=_OPERATOR_ROLE, AssumeRolePolicyDocument=trust_any)[
        "Role"
    ]["Arn"]
    data_role = iam.create_role(
        RoleName="crm-agent-eval-accounts-get-role", AssumeRolePolicyDocument=trust_any
    )["Role"]["Arn"]
    iam.put_role_policy(
        RoleName="crm-agent-eval-accounts-get-role",
        PolicyName="crm-agent-eval-accounts-get-dynamodb-kms",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": "kms:Decrypt", "Resource": "*"}],
            }
        ),
    )
    boto3.client("lambda", region_name=_REGION).create_function(
        FunctionName="crm-agent-eval-accounts-get",
        Runtime="python3.13",
        Role=data_role,
        Handler="h.h",
        Code={"ZipFile": _lambda_zip()},
    )
    cmk = boto3.client("kms", region_name=_REGION).create_key()["KeyMetadata"]["Arn"]
    ddb = boto3.client("dynamodb", region_name=_REGION)
    schema = table_schema("accounts")
    ddb.create_table(
        TableName=_TABLES["accounts"],
        KeySchema=[{"AttributeName": schema.partition_key, "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": schema.partition_key, "AttributeType": "S"},
            {"AttributeName": "tenant", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi_tenant",
                "KeySchema": [{"AttributeName": "tenant", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
        SSESpecification={"Enabled": True, "SSEType": "KMS", "KMSMasterKeyId": cmk},
    )
    credentials = boto3.client("sts", region_name=_REGION).assume_role(
        RoleArn=operator_arn, RoleSessionName="sess"
    )["Credentials"]
    session = boto3.session.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=_REGION,
    )
    clients = {s: session.client(s) for s in ("sts", "iam", "lambda", "dynamodb", "kms")}
    ctx = context_for_test(
        outputs={"dynamodb_table_names": {"accounts": _TABLES["accounts"]}}, clients=clients
    )

    def probe_factory(_ctx: Any, service: str, creds: Any) -> Any:
        return boto3.session.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=_REGION,
        ).client(service)

    rows = list(smoke.run_checks(ctx, probe_client_factory=probe_factory, run_id=_RUN_ID))
    by = _by_check(rows)

    for call in smoke.PROBE_CALLS:
        assert by[f"probe_denied:{call}"].status == "fail", by[f"probe_denied:{call}"]
    assert by["operator_data_plane_admitted"].status == "pass"
    assert by["data_plane_roles_discovered"].status == "pass"
    assert by["probe_role_deleted"].status == "pass"
    assert ddb.scan(TableName=_TABLES["accounts"])["Count"] == 0
    roles = iam.list_roles()["Roles"]
    assert not [r for r in roles if r["RoleName"].startswith("crm-agent-eval-probe-")]
