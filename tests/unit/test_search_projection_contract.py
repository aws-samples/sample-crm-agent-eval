# SPDX-License-Identifier: MIT
"""Contract test: every index ``crm.search`` may query projects everything its readers dereference.

Expectations are derived from source: the ``item.get`` reads in :mod:`crm.search` and the
``opportunity.<field>`` reads on the pipeline board.
"""

from __future__ import annotations

import re
from itertools import product
from pathlib import Path
from typing import Final

import pytest

from crm.search import (
    _ACCOUNT_GSI_OWNER,
    _ACCOUNT_GSI_TENANT,
    _ACCOUNTS_TABLE,
    _GSI_ACCOUNT,
    _GSI_OWNER_STAGE,
    _GSI_STAGE,
    _GSI_TENANT,
    _TABLE,
    _account_index_for,
    _index_for,
)
from crm.store.schema import OPPORTUNITY_SEARCH_PROJECTION, TABLES

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SEARCH_SOURCE: Final[Path] = REPO_ROOT / "src" / "crm" / "search.py"

#: The pipeline-board sources whose ``opportunity.<field>`` dereferences the projection must cover.
BOARD_SOURCES: Final[tuple[Path, ...]] = (
    REPO_ROOT / "frontend" / "src" / "pages" / "pipeline" / "OpportunityCard.tsx",
    REPO_ROOT / "frontend" / "src" / "pages" / "pipeline" / "PipelineBoard.tsx",
    REPO_ROOT / "frontend" / "src" / "pages" / "pipeline" / "usePipelineBoard.ts",
)

#: ``item.get("<attr>")`` in :mod:`crm.search` — every per-item attribute read its own filters make.
_ITEM_GET_RE: Final[re.Pattern[str]] = re.compile(r'item\.get\(\s*"([A-Za-z_]+)"')

#: ``opportunity.<field>`` in a board source. Requires the literal dot, so the plural
#: ``opportunities.map`` / ``opportunities.length`` collection reads are not mistaken for fields.
_BOARD_FIELD_RE: Final[re.Pattern[str]] = re.compile(r"\bopportunity\.([A-Za-z_]+)")


def _effective_projection(index_name: str) -> frozenset[str]:
    """Return every attribute a query on ``index_name`` actually returns.

    That is the ``INCLUDE`` list plus the index's and the base table's key attributes.
    """
    table = TABLES[_TABLE]
    gsi = table.gsi(index_name)
    keys = {table.partition_key, table.sort_key, gsi.partition_key, gsi.sort_key}
    return frozenset(gsi.projected_attributes | {key for key in keys if key is not None})


def _selectable_indexes() -> frozenset[str]:
    """Return every index name ``_index_for`` can return, by exercising its whole input domain."""
    stages: tuple[str | None, ...] = (None, "Discovery")
    owners: tuple[str | None, ...] = (None, "rep-delgado")
    accounts: tuple[str | None, ...] = (None, "acct-1")
    return frozenset(
        _index_for(stage=stage, owner_rep_id=owner, account_id=account)
        for stage, owner, account in product(stages, owners, accounts)
    )


def test_index_for_selects_exactly_the_four_indexes_this_contract_covers() -> None:
    """``_index_for`` selects exactly the four opportunity indexes the tests below cover."""
    assert _selectable_indexes() == {_GSI_TENANT, _GSI_STAGE, _GSI_OWNER_STAGE, _GSI_ACCOUNT}


@pytest.mark.parametrize("index_name", sorted(_selectable_indexes()))
def test_every_selectable_index_projects_the_full_search_projection(index_name: str) -> None:
    missing = OPPORTUNITY_SEARCH_PROJECTION - _effective_projection(index_name)
    assert not missing, (
        f"GSI {index_name!r} is selectable by crm.search._index_for but does not project "
        f"{sorted(missing)}. A filter reading an unprojected attribute silently matches nothing, "
        "and a consumer field reaching the caller as absent crashes it."
    )


def test_the_projection_contract_covers_every_attribute_search_itself_reads() -> None:
    """Every ``item.get("x")`` in :mod:`crm.search` names an attribute the contract guarantees."""
    read = frozenset(_ITEM_GET_RE.findall(SEARCH_SOURCE.read_text(encoding="utf-8")))
    assert read, "found no item.get(...) reads in crm.search — the derivation stopped working"
    unguaranteed = read - OPPORTUNITY_SEARCH_PROJECTION
    assert not unguaranteed, (
        f"crm.search filters on {sorted(unguaranteed)}, which OPPORTUNITY_SEARCH_PROJECTION does "
        "not guarantee. On an index that omits it the filter returns no match instead of failing."
    )


@pytest.mark.parametrize("source", BOARD_SOURCES, ids=lambda path: path.name)
def test_the_projection_contract_covers_every_field_the_board_dereferences(source: Path) -> None:
    """Every ``opportunity.<field>`` on the pipeline board names a guaranteed attribute."""
    fields = frozenset(_BOARD_FIELD_RE.findall(source.read_text(encoding="utf-8")))
    assert fields, f"found no opportunity.<field> reads in {source.name} — the derivation broke"
    unguaranteed = fields - OPPORTUNITY_SEARCH_PROJECTION
    assert not unguaranteed, (
        f"{source.name} dereferences {sorted(unguaranteed)} on an opportunity returned by "
        "search_opportunities, which OPPORTUNITY_SEARCH_PROJECTION does not guarantee. TypeScript "
        "cannot catch this: the response is typed as the full Opportunity record."
    )


# --------------------------------------------------------------------------------------
# The account-index projection contract. The two account indexes project different attributes,
# so the guarantee is stated per index.
# --------------------------------------------------------------------------------------

#: What ``search_accounts`` reads from each index it can select: the ``crm.authz.is_in_scope`` axes
#: (``tenant``, ``account_id``), the free-text haystack ``name``, and on the owner-filtered path
#: ``owner_rep_id``.
_ACCOUNT_SEARCH_READS_BY_INDEX: Final[dict[str, frozenset[str]]] = {
    _ACCOUNT_GSI_TENANT: frozenset({"tenant", "account_id", "name"}),
    _ACCOUNT_GSI_OWNER: frozenset({"tenant", "account_id", "name", "owner_rep_id"}),
}


def _account_effective_projection(index_name: str) -> frozenset[str]:
    """Every attribute a query on the accounts ``index_name`` actually returns (keys + INCLUDE)."""
    table = TABLES[_ACCOUNTS_TABLE]
    gsi = table.gsi(index_name)
    keys = {table.partition_key, table.sort_key, gsi.partition_key, gsi.sort_key}
    return frozenset(gsi.projected_attributes | {key for key in keys if key is not None})


def test_account_index_for_selects_exactly_the_two_indexes_this_contract_covers() -> None:
    """``_account_index_for`` selects exactly the two account indexes the test below covers."""
    selectable = {_account_index_for(owner_rep_id=owner) for owner in (None, "rep-x")}
    assert (
        selectable
        == set(_ACCOUNT_SEARCH_READS_BY_INDEX)
        == {
            _ACCOUNT_GSI_TENANT,
            _ACCOUNT_GSI_OWNER,
        }
    )


@pytest.mark.parametrize("index_name", sorted(_ACCOUNT_SEARCH_READS_BY_INDEX))
def test_every_selectable_account_index_projects_what_search_reads_on_it(index_name: str) -> None:
    missing = _ACCOUNT_SEARCH_READS_BY_INDEX[index_name] - _account_effective_projection(index_name)
    assert not missing, (
        f"account GSI {index_name!r} is selectable by search_accounts but does not project "
        f"{sorted(missing)}. A filter reading an unprojected attribute silently matches nothing; "
        "this is why search_accounts matches free text on the projected `name` rather than "
        "`name_lower`, which gsi_owner omits."
    )


# --------------------------------------------------------------------------------------
# schema.py's opportunity indexes against the IaC that deploys them.
# --------------------------------------------------------------------------------------

_TABLES_TF: Final[Path] = REPO_ROOT / "infra" / "modules" / "dynamodb" / "tables.tf"


def _tf_opportunity_projections() -> dict[str, frozenset[str]]:
    """`{gsi name: non_key_attributes}` of the opportunities table's GSIs in tables.tf."""
    text = _TABLES_TF.read_text(encoding="utf-8")
    start = text.find('resource "aws_dynamodb_table" "opportunities" {')
    assert start != -1, "the opportunities table block was not found in tables.tf"
    following = re.search(r"^resource\s", text[start + 1 :], re.MULTILINE)
    block = text[start : start + 1 + following.start()] if following else text[start:]
    projections = {
        match.group("name"): frozenset(re.findall(r'"(\w+)"', match.group("attrs")))
        for match in re.finditer(
            r'global_secondary_index\s*\{[^}]*?name\s*=\s*"(?P<name>\w+)"[^}]*?'
            r"non_key_attributes\s*=\s*\[(?P<attrs>[^\]]*)\]",
            block,
        )
    }
    assert projections, "no opportunities GSI projections were read from tables.tf"
    return projections


def test_schema_py_opportunity_projections_match_the_iac() -> None:
    """Every opportunities GSI projects in tables.tf exactly what schema.py declares."""
    declared = {name: gsi.projected_attributes for name, gsi in TABLES[_TABLE].gsis.items()}
    assert _tf_opportunity_projections() == declared
