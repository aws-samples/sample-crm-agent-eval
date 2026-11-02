# SPDX-License-Identifier: MIT
"""Each planned DynamoDB GSI projects exactly what `crm.store.schema.TABLES` declares for it.

A GSI's effective INCLUDE projection is its `non_key_attributes` plus the table's and the index's
key attributes. The schema side is `projected_attributes` plus the same keys. The two must be equal
for every index the schema declares, every index must project `tenant`, and the planned module must
declare no index the schema lacks. The source is the real `dynamodb` module plan; a scratch copy
with `close_date` dropped from `opportunities.gsi_tenant` reds the equality.
Terraform-backed cells skip when terraform is absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Final

import pytest

from crm.store.schema import TABLES, TableSchema
from tests.unit._terraform_fixture import (
    _PROVIDER_BLOCK,
    MODULES_DIR,
    Plan,
    plan_directory,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

_PREFIX: Final[str] = "t-"
_MODULE_ARGUMENTS: Final[str] = (
    f'  name_prefix = "{_PREFIX}"\n'
    '  account_id = "123456789012"\n'
    '  caller_principal_arn = "arn:aws:iam::123456789012:role/fixture-operator"\n'
    '  data_plane_principal_arns = ["arn:aws:iam::123456789012:role/fixture-data-plane"]\n'
    '  administrator_principal_arns = ["arn:aws:iam::123456789012:role/fixture-operator"]'
)


def _skip_without_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None and not require_terraform():
        pytest.skip(reason)


@pytest.fixture(scope="module")
def module_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_without_terraform()
    return plan_module(tmp_path_factory.mktemp("dynamodb"), "dynamodb", _MODULE_ARGUMENTS)


def _planned_table(plan: Plan, table: str) -> dict[str, Any]:
    matches = [
        plan.after(address)
        for address in plan.addresses_of_type("aws_dynamodb_table")
        if plan.after(address).get("name") == f"{_PREFIX}{table}"
    ]
    assert len(matches) == 1, (
        f"expected one planned table named {_PREFIX}{table}, got {len(matches)}"
    )
    return matches[0]


def _planned_projections(plan: Plan, table: TableSchema) -> dict[str, frozenset[str]]:
    planned = _planned_table(plan, table.name)
    keys = {table.partition_key, *([table.sort_key] if table.sort_key else [])}
    result: dict[str, frozenset[str]] = {}
    for index in planned.get("global_secondary_index") or []:
        assert index.get("projection_type") == "INCLUDE", (
            f"{table.name}.{index['name']} is not INCLUDE"
        )
        index_keys = {index["hash_key"], *([index["range_key"]] if index.get("range_key") else [])}
        result[index["name"]] = frozenset(
            set(index.get("non_key_attributes") or []) | keys | index_keys
        )
    return result


def _schema_projections(table: TableSchema) -> dict[str, frozenset[str]]:
    keys = {table.partition_key, *([table.sort_key] if table.sort_key else [])}
    return {
        name: frozenset(
            set(gsi.projected_attributes)
            | keys
            | {gsi.partition_key, *([gsi.sort_key] if gsi.sort_key else [])}
        )
        for name, gsi in table.gsis.items()
    }


@pytest.mark.parametrize("table_name", sorted(name for name, t in TABLES.items() if t.gsis))
def test_each_planned_gsi_projects_exactly_the_schema_projection(
    module_plan: Plan, table_name: str
) -> None:
    table = TABLES[table_name]
    planned = _planned_projections(module_plan, table)
    schema = _schema_projections(table)
    assert sorted(planned) == sorted(schema), f"{table_name}: planned vs schema index names differ"
    for name, attributes in schema.items():
        assert planned[name] == attributes, (
            f"{table_name}.{name}: planned-only {sorted(planned[name] - attributes)}, "
            f"schema-only {sorted(attributes - planned[name])}"
        )
        assert "tenant" in planned[name], f"{table_name}.{name} does not project tenant"


@pytest.mark.parametrize(
    ("index", "added"),
    [
        ("gsi_tenant", {"close_date"}),
        ("gsi_account", {"name_lower", "owner_rep_id", "close_date"}),
    ],
)
def test_opportunity_indexes_project_the_g6_search_attributes(
    module_plan: Plan, index: str, added: set[str]
) -> None:
    planned = _planned_projections(module_plan, TABLES["opportunities"])[index]
    assert added <= planned, f"opportunities.{index} lacks {sorted(added - planned)}"


def test_a_dropped_projection_attribute_reds_the_equality(tmp_path: Path) -> None:
    _skip_without_terraform()
    modules = tmp_path / "modules"
    shutil.copytree(MODULES_DIR / "dynamodb", modules / "dynamodb")
    tables_tf = modules / "dynamodb" / "tables.tf"
    text = tables_tf.read_text(encoding="utf-8")
    original = '"opportunity_id", "account_id", "name", "owner_rep_id", "stage", "close_date", "value", "tenant",'
    assert text.count(original) == 1, "the gsi_tenant projection line moved; update this control"
    tables_tf.write_text(
        text.replace(original, original.replace(' "close_date",', "")), encoding="utf-8"
    )

    work = tmp_path / "root"
    work.mkdir()
    (work / "main.tf").write_text(
        f'{_PROVIDER_BLOCK}\nmodule "subject" {{\n  source = "{modules / "dynamodb"}"\n\n'
        f"{_MODULE_ARGUMENTS}\n}}\n",
        encoding="utf-8",
    )
    planned_copy = plan_directory(work)
    planned = _planned_projections(planned_copy, TABLES["opportunities"])["gsi_tenant"]
    assert "close_date" not in planned
    assert planned != _schema_projections(TABLES["opportunities"])["gsi_tenant"]
