# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts/check_plan_security_baseline.py`'s DynamoDB action matcher and
its IAM policy-source reader (`_policy_source_findings`)."""

from __future__ import annotations

import functools
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from scripts import plan_fixtures

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_plan_security_baseline.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "plans"

if TYPE_CHECKING:
    import scripts.check_plan_security_baseline as gate
elif "check_plan_security_baseline" in sys.modules:
    gate = sys.modules["check_plan_security_baseline"]
else:
    _SPEC = importlib.util.spec_from_file_location("check_plan_security_baseline", SCRIPT_PATH)
    assert _SPEC is not None and _SPEC.loader is not None
    gate = importlib.util.module_from_spec(_SPEC)
    sys.modules["check_plan_security_baseline"] = gate
    _SPEC.loader.exec_module(gate)


@functools.cache
def fixture_path(name: str) -> Path:
    """The plan file for `name`: built by `scripts.plan_fixtures` for its names, else tracked."""
    if name in plan_fixtures.NAMES:
        return plan_fixtures.materialize(name)
    return FIXTURES_DIR / f"{name}.json"


def load_plan(name: str) -> gate.Plan:
    return gate.load_plan(fixture_path(name))


class TestDynamodbActionMatcherReadsTheServicePrefix:
    @pytest.mark.parametrize(
        "action",
        ["dynamodb:GetItem", "dynamodb:NotARealAction", "DynamoDB:*", "*", "*:*", "*:GetItem",
         "dynamo*:Scan", "*:partiqlselect", "dynamodb*"],
    )  # fmt: skip
    def test_a_pattern_naming_a_dynamodb_action_matches(self, action: str) -> None:
        assert gate._action_matches_dynamodb(action)

    @pytest.mark.parametrize("action", ["*:PutObject", "s3:*", "*:GetObject*", "dyn*:Publish"])
    def test_a_wildcarded_service_matches_only_a_real_dynamodb_action_name(
        self, action: str
    ) -> None:
        assert not gate._action_matches_dynamodb(action)

    def test_an_allow_of_star_putobject_grants_no_dynamodb(self) -> None:
        put_object = {"effect": "Allow", "actions": ["*:PutObject"]}
        get_item = {"effect": "Allow", "actions": ["*:GetItem"]}
        assert not gate._statement_grants_dynamodb(put_object)
        assert gate._statement_grants_dynamodb(get_item)


def _source_plan(
    resources: list[dict[str, Any]], config_resources: list[dict[str, Any]]
) -> gate.Plan:
    """A root-module plan of `resources`, with `config_resources` as its configuration entries."""
    return gate.Plan(
        resource_changes=resources,
        configuration={"root_module": {"resources": config_resources}},
    )


def _iam(resource_type: str, name: str, after: dict[str, Any]) -> dict[str, Any]:
    return {
        "address": f"{resource_type}.{name}",
        "type": resource_type,
        "change": {"after": after, "after_unknown": {}},
    }


def _config(resource_type: str, name: str, **expressions: dict[str, Any]) -> dict[str, Any]:
    return {"address": f"{resource_type}.{name}", "type": resource_type, "expressions": expressions}


_DOC_POLICY: dict[str, Any] = {"references": ["data.aws_iam_policy_document.x.json"]}
_JSONENCODE: dict[str, Any] = {"references": ["aws_dynamodb_table.t.arn", "aws_dynamodb_table.t"]}


def _json_policy(*actions: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Sid": "Inline", "Effect": "Allow", "Action": list(actions)}],
        }
    )


def _source_rows(plan: gate.Plan) -> tuple[list[gate.Finding], gate.Finding]:
    findings = gate._policy_source_findings(plan)
    *unread, coverage = findings
    assert coverage.resource == "(policy sources) every IAM policy source is read"
    return unread, coverage


class TestEveryIamPolicySourceIsRead:
    def test_the_compliant_fixture_reads_every_source(self) -> None:
        unread, coverage = _source_rows(load_plan("compliant"))
        assert unread == [] and coverage.status == "pass", coverage.detail
        assert "0 unread row(s) over " in coverage.detail

    def test_the_compliant_fixture_row_is_emitted_by_the_control(self) -> None:
        rows = [
            f
            for f in gate.check_dynamodb_no_wildcard_action(load_plan("compliant"))
            if f.resource.startswith("(policy sources)")
        ]
        assert [f.status for f in rows] == ["pass"]

    def test_a_document_referencing_policy_is_read(self) -> None:
        plan = _source_plan(
            [_iam("aws_iam_role_policy", "p", {})],
            [_config("aws_iam_role_policy", "p", policy=_DOC_POLICY)],
        )
        unread, coverage = _source_rows(plan)
        assert unread == [] and coverage.status == "pass"

    def test_an_inline_jsonencode_unknown_at_plan_time_fails(self) -> None:
        plan = _source_plan(
            [_iam("aws_iam_role_policy", "p", {})],
            [_config("aws_iam_role_policy", "p", policy=_JSONENCODE)],
        )
        unread, coverage = _source_rows(plan)
        assert [f.resource for f in unread] == ["aws_iam_role_policy.p"]
        assert "cannot be read" in unread[0].detail and coverage.status == "fail"

    @pytest.mark.parametrize("resource_type", ["aws_iam_role_policy", "aws_iam_policy"])
    def test_a_known_json_policy_granting_dynamodb_fails(self, resource_type: str) -> None:
        plan = _source_plan(
            [_iam(resource_type, "p", {"policy": _json_policy("dynamodb:GetItem")})],
            [_config(resource_type, "p", policy=_JSONENCODE)],
        )
        unread, _ = _source_rows(plan)
        assert len(unread) == 1 and "['Inline']" in unread[0].detail, unread

    def test_a_known_json_policy_without_dynamodb_is_read(self) -> None:
        plan = _source_plan(
            [_iam("aws_iam_role_policy", "p", {"policy": _json_policy("s3:GetObject")})],
            [_config("aws_iam_role_policy", "p", policy=_JSONENCODE)],
        )
        unread, coverage = _source_rows(plan)
        assert unread == [] and coverage.status == "pass"

    def test_a_statement_that_is_not_an_object_fails_as_a_row(self) -> None:
        policy = json.dumps({"Version": "2012-10-17", "Statement": ["x"]})
        plan = _source_plan(
            [_iam("aws_iam_role_policy", "p", {"policy": policy})],
            [_config("aws_iam_role_policy", "p", policy=_JSONENCODE)],
        )
        unread, _ = _source_rows(plan)
        assert len(unread) == 1 and "not an object or a list of objects" in unread[0].detail

    def test_a_role_only_source_is_counted_in_the_coverage_detail(self) -> None:
        plan = _source_plan(
            [_iam("aws_iam_role", "r", {})],
            [_config("aws_iam_role", "r", inline_policy={"references": ["local.p"]})],
        )
        _, coverage = _source_rows(plan)
        assert "1 unread row(s) over 1 IAM resource(s)" in coverage.detail, coverage.detail

    def test_an_unparseable_json_policy_fails(self) -> None:
        plan = _source_plan(
            [_iam("aws_iam_role_policy", "p", {"policy": "{not json"})],
            [_config("aws_iam_role_policy", "p", policy=_JSONENCODE)],
        )
        unread, _ = _source_rows(plan)
        assert len(unread) == 1 and "not a readable policy document" in unread[0].detail

    def test_an_attachment_to_a_planned_policy_is_read(self) -> None:
        plan = _source_plan(
            [_iam("aws_iam_policy", "m", {}), _iam("aws_iam_role_policy_attachment", "a", {})],
            [
                _config("aws_iam_policy", "m", policy=_DOC_POLICY),
                _config(
                    "aws_iam_role_policy_attachment",
                    "a",
                    policy_arn={"references": ["aws_iam_policy.m.arn", "aws_iam_policy.m"]},
                ),
            ],
        )
        unread, coverage = _source_rows(plan)
        assert unread == [] and coverage.status == "pass"

    def test_a_literal_managed_policy_arn_fails(self) -> None:
        arn = "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess"
        plan = _source_plan(
            [_iam("aws_iam_role_policy_attachment", "a", {"policy_arn": arn})],
            [_config("aws_iam_role_policy_attachment", "a", policy_arn={"constant_value": arn})],
        )
        unread, _ = _source_rows(plan)
        assert len(unread) == 1 and f"constant:{arn}" in unread[0].detail

    def test_an_attachment_resolved_through_a_variable_and_module_output_is_read(self) -> None:
        """`var.x` in a child module -> the parent call's `module.p.arn` -> `aws_iam_policy.this`."""
        child = {
            "resources": [
                _config("aws_iam_role_policy_attachment", "a", policy_arn={"references": ["var.x"]})
            ]
        }
        producer = {
            "resources": [_config("aws_iam_policy", "this", policy=_DOC_POLICY)],
            "outputs": {"arn": {"expression": {"references": ["aws_iam_policy.this.arn"]}}},
        }
        configuration: dict[str, Any] = {
            "root_module": {
                "module_calls": {
                    "c": {
                        "module": child,
                        "expressions": {"x": {"references": ["module.p.arn", "module.p"]}},
                    },
                    "p": {"module": producer, "expressions": {}},
                }
            }
        }
        resources = [
            {**_iam("aws_iam_role_policy_attachment", "a", {}), "address": "module.c.aws_iam_role_policy_attachment.a"},
            {**_iam("aws_iam_policy", "this", {}), "address": "module.p.aws_iam_policy.this"},
        ]  # fmt: skip
        plan = gate.Plan(resource_changes=resources, configuration=configuration)
        unread, coverage = _source_rows(plan)
        assert unread == [] and coverage.status == "pass", unread
        configuration["root_module"]["module_calls"]["p"]["module"]["outputs"] = {}
        unread, coverage = _source_rows(plan)
        assert [f.resource for f in unread] == ["module.c.aws_iam_role_policy_attachment.a"]
        assert coverage.status == "fail"

    @pytest.mark.parametrize(
        "resource_type",
        [
            "aws_iam_policy_attachment",
            "aws_iam_role_policies_exclusive",
            "aws_iam_role_policy_attachments_exclusive",
        ],
    )
    def test_an_attachment_type_with_no_reader_fails(self, resource_type: str) -> None:
        plan = _source_plan([_iam(resource_type, "a", {})], [])
        unread, coverage = _source_rows(plan)
        assert [f.resource for f in unread] == [f"{resource_type}.a"] and coverage.status == "fail"

    @pytest.mark.parametrize("key", ["inline_policy", "managed_policy_arns"])
    def test_a_role_carrying_its_own_policies_fails(self, key: str) -> None:
        plan = _source_plan(
            [_iam("aws_iam_role", "r", {})],
            [_config("aws_iam_role", "r", **{key: {"references": ["local.p"]}})],
        )
        unread, _ = _source_rows(plan)
        assert [f.resource for f in unread] == ["aws_iam_role.r"] and key in unread[0].detail


def _document(statements: list[dict[str, Any]], **after: Any) -> dict[str, Any]:
    return {
        "address": "data.aws_iam_policy_document.d",
        "mode": "data",
        "type": "aws_iam_policy_document",
        "change": {"actions": ["read"], "after": {"statement": statements, **after}},
    }


_GET_ITEM: dict[str, Any] = {
    "sid": "DynamoDbReps0",
    "effect": "Allow",
    "actions": ["dynamodb:GetItem"],
}


class TestMergedPolicyDocumentsAreUnread:
    """A document merging other documents in is an unread policy source."""

    def test_a_plain_document_is_read(self) -> None:
        unread, coverage = _source_rows(gate.Plan(resource_changes=[_document([_GET_ITEM])]))
        assert unread == [] and coverage.status == "pass"

    @pytest.mark.parametrize("key", gate._MERGED_DOCUMENT_ARGUMENTS)
    def test_a_set_merge_argument_fails(self, key: str) -> None:
        merged = json.dumps({"Statement": [{"Effect": "Allow", "Action": "dynamodb:*"}]})
        value: Any = merged if key.endswith("_json") else [merged]
        plan = gate.Plan(resource_changes=[_document([_GET_ITEM], **{key: value})])
        unread, coverage = _source_rows(plan)
        assert [f.resource for f in unread] == ["data.aws_iam_policy_document.d"]
        assert key in unread[0].detail and coverage.status == "fail"

    def test_a_merge_argument_unknown_at_plan_time_fails(self) -> None:
        document = _document([_GET_ITEM])
        document["change"]["after_unknown"] = {"source_policy_documents": True}
        unread, _ = _source_rows(gate.Plan(resource_changes=[document]))
        assert len(unread) == 1 and "source_policy_documents" in unread[0].detail

    def test_a_merge_argument_written_in_configuration_fails(self) -> None:
        plan = _source_plan(
            [_document([_GET_ITEM])],
            [
                {
                    "address": "data.aws_iam_policy_document.d",
                    "mode": "data",
                    "type": "aws_iam_policy_document",
                    "expressions": {"override_policy_documents": {"references": ["local.p"]}},
                }
            ],
        )
        unread, _ = _source_rows(plan)
        assert len(unread) == 1 and "override_policy_documents" in unread[0].detail


def _action_name_rows(statements: list[dict[str, Any]]) -> list[gate.Finding]:
    plan = gate.Plan(resource_changes=[_document(statements)])
    return [
        f
        for f in gate.check_dynamodb_no_wildcard_action(plan)
        if f.resource.endswith(" action names")
    ]


class TestNonExistentDynamodbActionNamesFail:
    """A granted `dynamodb:<Name>` that DynamoDB does not define fails, naming it."""

    def test_an_undefined_action_name_fails_naming_it(self) -> None:
        statement = {**_GET_ITEM, "actions": ["dynamodb:GetItem", "dynamodb:TransactWriteItems"]}
        (row,) = _action_name_rows([statement])
        assert row.status == "fail" and "['dynamodb:TransactWriteItems']" in row.detail

    @pytest.mark.parametrize(
        "statement",
        [
            _GET_ITEM,
            {**_GET_ITEM, "actions": ["dynamodb:getitem", "DynamoDB:Query"]},
            {**_GET_ITEM, "actions": ["dynamodb:Transact*"]},
            {**_GET_ITEM, "effect": "Deny", "actions": ["dynamodb:TransactWriteItems"]},
        ],
        ids=["defined", "case-folded", "wildcard", "deny"],
    )
    def test_defined_names_wildcards_and_denies_yield_no_row(
        self, statement: dict[str, Any]
    ) -> None:
        assert _action_name_rows([statement]) == []

    def test_the_compliant_fixture_grants_only_defined_action_names(self) -> None:
        rows = [
            f
            for f in gate.check_dynamodb_no_wildcard_action(load_plan("compliant"))
            if f.resource.endswith(" action names")
        ]
        assert rows == [], [f.detail for f in rows][:3]
