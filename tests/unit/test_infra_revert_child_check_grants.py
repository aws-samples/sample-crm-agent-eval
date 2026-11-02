# SPDX-License-Identifier: MIT
"""revert_audit_record may Query exactly the child-check indexes and no base table, and delete notes.

* `local.tool_query_indexes.revert_audit_record` in `infra/main.tf` equals the table/index pairs of
  `crm.audit._CHILD_INDEXES`.
* The root's `dynamodb_grants` expression for the `lambda_action` module, read verbatim from
  `infra/main.tf` and planned against stand-in table and GSI ARNs from `crm.store.schema.TABLES`,
  renders revert_audit_record's Query grants over exactly those five index ARNs with
  `include_table_arn = false`, and no Query on any base table.
* Control: another tool's Query grant (`get_opportunity`) still covers every GSI of its table and
  the table ARN.
Terraform-backed cells skip when terraform is absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from crm.audit import _CHILD_INDEXES
from crm.store.schema import TABLES
from tests.unit._terraform_fixture import plan_root, require_terraform, terraform_unavailable_reason

_MAIN_TF: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "main.tf"
_ARN_PREFIX: Final[str] = "arn:aws:dynamodb:us-east-1:123456789012:table/t-"

CHILD_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    pair for pairs in _CHILD_INDEXES.values() for pair in pairs
)


def _table_arn(table: str) -> str:
    return f"{_ARN_PREFIX}{table}"


def _block(text: str, header: str, indent: str) -> str:
    start = text.index(header)
    end = text.index(f"\n{indent}}}\n", start) + len(f"\n{indent}}}")
    return text[start:end]


def _declared_query_indexes() -> dict[str, list[str]]:
    block = _block(
        _MAIN_TF.read_text(encoding="utf-8"), "    revert_audit_record = {\n      contacts", "    "
    )
    return {
        table: re.findall(r'"([^"]+)"', indexes)
        for table, indexes in re.findall(r"(\w+)\s*=\s*\[([^\]]*)\]", block)
    }


def test_the_scoped_indexes_mirror_the_child_check() -> None:
    declared = {
        (table, index) for table, indexes in _declared_query_indexes().items() for index in indexes
    }
    assert declared == set(CHILD_PAIRS)


def _grants_expression(text: str) -> str:
    module = text[text.index('module "lambda_action" {') :]
    start = module.index("  dynamodb_grants = [")
    end = module.index("\n  ]\n", start) + len("\n  ]")
    return module[start + len("  dynamodb_grants = ") : end]


def _render(tmp_path: Path, tool: str) -> list[dict[str, Any]]:
    reason = terraform_unavailable_reason()
    if reason is not None and not require_terraform():
        pytest.skip(reason)
    text = _MAIN_TF.read_text(encoding="utf-8")
    grants = _block(text, "  tool_dynamodb_grants = {", "  ")
    query_indexes = _block(text, "  tool_query_indexes = {", "  ")
    expression = (
        _grants_expression(text)
        .replace("each.key", f'"{tool}"')
        .replace("module.dynamodb.table_arns", "local.stand_in_table_arns")
        .replace("module.dynamodb.gsi_arns", "local.stand_in_gsi_arns")
    )
    table_arns = {name: _table_arn(name) for name in TABLES}
    gsi_arns = {
        name: {gsi: f"{_table_arn(name)}/index/{gsi}" for gsi in schema.gsis}
        for name, schema in TABLES.items()
    }
    hcl = (
        f"locals {{\n{grants}\n{query_indexes}\n"
        f"  stand_in_table_arns = {json.dumps(table_arns)}\n"
        f"  stand_in_gsi_arns = {json.dumps(gsi_arns)}\n}}\n"
        f'resource "terraform_data" "grants" {{\n  input = jsonencode({expression})\n}}\n'
    )
    plan = plan_root(tmp_path / "infra", hcl)
    rendered = plan.after("terraform_data.grants")["input"]
    assert isinstance(rendered, str)
    result: list[dict[str, Any]] = json.loads(rendered)
    return result


def test_revert_queries_exactly_the_child_indexes_and_no_base_table(tmp_path: Path) -> None:
    grants = _render(tmp_path, "revert_audit_record")
    query = [g for g in grants if "dynamodb:Query" in g["actions"]]
    assert all(g["actions"] == ["dynamodb:Query"] for g in query)
    assert all(g["include_table_arn"] is False for g in query)
    assert {arn for g in query for arn in g["index_arns"]} == {
        f"{_table_arn(table)}/index/{index}" for table, index in CHILD_PAIRS
    }
    for grant in grants:
        if "dynamodb:Query" not in grant["actions"]:
            assert grant["index_arns"] == [] and grant["include_table_arn"] is True
    notes = [g for g in grants if g["table_name"] == "notes"]
    # Besides the child-check Query, revert may only delete a note (a note-create revert).
    assert sorted(g["actions"][0] for g in notes) == ["dynamodb:DeleteItem", "dynamodb:Query"]
    assert all(len(g["actions"]) == 1 for g in notes), notes
    delete = next(g for g in notes if g["actions"] == ["dynamodb:DeleteItem"])
    assert delete["include_table_arn"] is True and delete["index_arns"] == []
    assert delete["table_arn"] == _table_arn("notes")


def test_an_unscoped_query_grant_still_covers_every_gsi_and_the_table(tmp_path: Path) -> None:
    grants = _render(tmp_path, "get_opportunity")
    activities = next(g for g in grants if g["table_name"] == "activities")
    assert activities["include_table_arn"] is True
    assert set(activities["index_arns"]) == {
        f"{_table_arn('activities')}/index/{gsi}" for gsi in TABLES["activities"].gsis
    }
