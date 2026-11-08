# SPDX-License-Identifier: MIT
"""Scope-constrained opportunity and account search: a GSI query plus in-process token filtering.

A tenant-scoped GSI query retrieves candidates in bounded pages and free-text matching runs in
this process. At most :data:`EXAMINATION_BUDGET` index items are read per call, out-of-book items
included; a cut-off result is marked ``truncated=True`` and carries a ``next_cursor`` to resume
from. Items outside the caller's book (:func:`crm.authz.filter_to_scope`) are dropped before they
are counted or matched. The cursor is opaque and bound to the caller, the operation, the index and
its partition value (:mod:`crm.cursor`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from crm.authz import Identity, filter_to_scope, require_listing_scope, require_scope
from crm.cursor import bound_operation, decode_cursor, encode_cursor
from crm.errors import CrmError, ValidationFailed, bounded_detail_value
from crm.store.keys import tenant_prefixed
from crm.store.schema import table_schema
from crm.store.store import Store

#: Maximum number of index items examined per call before search stops and reports truncation.
EXAMINATION_BUDGET: Final[int] = 2000

#: Page size requested from the underlying GSI query per round trip.
_PAGE_SIZE: Final[int] = 200

_TABLE: Final[str] = "opportunities"
_GSI_TENANT: Final[str] = "gsi_tenant"
_GSI_ACCOUNT: Final[str] = "gsi_account"
_GSI_OWNER_STAGE: Final[str] = "gsi_owner_stage"
_GSI_STAGE: Final[str] = "gsi_stage"

#: Longest explicit ``account_id`` filter accepted; longer values are refused, never echoed.
ACCOUNT_ID_MAX_LENGTH: Final[int] = 128

_ACCOUNT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")

_ACCOUNTS_TABLE: Final[str] = "accounts"
_ACCOUNT_GSI_OWNER: Final[str] = "gsi_owner"
_ACCOUNT_GSI_TENANT: Final[str] = "gsi_tenant"


@dataclass(frozen=True, slots=True)
class SearchResult:
    """The outcome of one :func:`search_opportunities` or :func:`search_accounts` call.

    Attributes:
        items: In-scope records matching the query, up to the point examination stopped.
        truncated: ``True`` when :data:`EXAMINATION_BUDGET` index items were read, or more than
            ``limit`` in-scope matches were found, so ``items`` may not be the complete match set.
        examined_count: In-scope items examined across every page this call issued; items outside
            the caller's book are never counted.
        next_cursor: An opaque token resuming examination where this call stopped, valid only for
            the same caller and the same index-selecting filters; ``None`` when not truncated or
            nothing remains.
    """

    items: list[dict[str, object]]
    truncated: bool
    examined_count: int
    next_cursor: str | None


def _index_for(
    *, stage: str | None, owner_rep_id: str | None, account_id: str | None = None
) -> str:
    """Choose the narrowest GSI the given filters can be served from.

    An account filter selects ``gsi_account``; otherwise an owner filter selects
    ``gsi_owner_stage``, a stage-only filter ``gsi_stage``, and no filter ``gsi_tenant``. Any
    filter the chosen index does not key on is applied in process.
    """
    if account_id is not None:
        return _GSI_ACCOUNT
    if owner_rep_id is not None:
        return _GSI_OWNER_STAGE
    if stage is not None:
        return _GSI_STAGE
    return _GSI_TENANT


def _partition_value(
    identity: Identity,
    index_name: str,
    *,
    stage: str | None,
    owner_rep_id: str | None,
    account_id: str | None = None,
) -> str:
    """Build the chosen index's partition-key value: ``tenant`` or a tenant-prefixed composite."""
    if index_name == _GSI_ACCOUNT:
        if account_id is None:  # pragma: no cover - guarded by _index_for's own selection
            raise ValueError("gsi_account requires account_id")
        return tenant_prefixed(identity.tenant, account_id)
    if index_name == _GSI_OWNER_STAGE:
        if owner_rep_id is None:  # pragma: no cover - guarded by _index_for's own selection
            raise ValueError("gsi_owner_stage requires owner_rep_id")
        return tenant_prefixed(identity.tenant, owner_rep_id)
    if index_name == _GSI_STAGE:
        if stage is None:  # pragma: no cover - guarded by _index_for's own selection
            raise ValueError("gsi_stage requires stage")
        return tenant_prefixed(identity.tenant, stage)
    return identity.tenant


def _matches_query(item: dict[str, object], tokens: tuple[str, ...]) -> bool:
    """Return whether every free-text token appears in the item's normalised search field."""
    if not tokens:
        return True
    name_lower = item.get("name_lower")
    haystack = name_lower if isinstance(name_lower, str) else ""
    return all(token in haystack for token in tokens)


def _matches_owner(item: dict[str, object], owner_rep_id: str | None) -> bool:
    """Apply the owner filter when the chosen index could not already fully narrow to it."""
    return owner_rep_id is None or item.get("owner_rep_id") == owner_rep_id


def _matches_stage(item: dict[str, object], stage: str | None) -> bool:
    """Apply the stage filter when the chosen index could not already fully narrow to it."""
    return stage is None or item.get("stage") == stage


def _examine(
    store: Store,
    identity: Identity,
    table: str,
    index_name: str,
    *,
    operation: str,
    partition_value: str,
    cursor: str | None,
    limit: int | None,
    matches: Callable[[dict[str, object]], bool],
) -> SearchResult:
    """Run the bounded, cursor-resumable GSI examination loop shared by both search entry points.

    Pages the chosen index, scope-filters each page to ``identity``'s book, then applies
    ``matches``, until the index is exhausted, :data:`EXAMINATION_BUDGET` index items have been
    read, or more in-scope matches than ``limit`` have been collected. ``next_cursor`` is present
    only when examination truncated with something left to resume from; after a ``limit`` cut it
    resumes immediately after the last returned item, so no match is skipped.
    """
    binding = bound_operation(operation, index_name, partition_value)
    page_key = decode_cursor(cursor, identity=identity, operation=binding)
    matched: list[dict[str, object]] = []
    read_count = 0
    examined_count = 0
    truncated = False

    while True:
        remaining_budget = EXAMINATION_BUDGET - read_count
        if remaining_budget <= 0:
            truncated = True
            break

        page_limit = min(_PAGE_SIZE, remaining_budget)
        raw_items, page_key = store.query_gsi(
            table,
            index_name,
            partition_value=partition_value,
            limit=page_limit,
            exclusive_start_key=page_key,
        )
        read_count += len(raw_items)
        in_scope = filter_to_scope(identity, raw_items)
        examined_count += len(in_scope)
        matched.extend(item for item in in_scope if matches(item))

        index_exhausted = page_key is None
        budget_exhausted = read_count >= EXAMINATION_BUDGET
        limit_exceeded = limit is not None and len(matched) > limit

        if limit_exceeded or budget_exhausted:
            truncated = True
            break
        if index_exhausted:
            break

    items = matched
    if limit is not None and len(matched) > limit:
        items = matched[:limit]
        page_key = _resume_key(table, index_name, items[-1])
    return SearchResult(
        items=items,
        truncated=truncated,
        examined_count=examined_count,
        next_cursor=(
            encode_cursor(page_key, identity=identity, operation=binding) if truncated else None
        ),
    )


def _resume_key(table: str, index_name: str, item: dict[str, object]) -> dict[str, object]:
    """Build the ``ExclusiveStartKey`` that resumes ``index_name`` immediately after ``item``.

    Raises:
        CrmError: If ``item`` lacks one of the table or index key attributes.
    """
    schema = table_schema(table)
    gsi = schema.gsi(index_name)
    names = {schema.partition_key, gsi.partition_key}
    names.update(name for name in (schema.sort_key, gsi.sort_key) if name is not None)
    key: dict[str, object] = {}
    for name in sorted(names):
        value = item.get(name)
        if isinstance(value, str):
            key[name] = {"S": value}
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            key[name] = {"N": str(value)}
        else:
            raise CrmError(
                "The request could not be completed.",
                detail=f"search resume key: {table}.{index_name} item lacks {name}",
            )
    return key


def _require_well_formed_account_id(account_id: str) -> None:
    """Raise :class:`ValidationFailed` (``source="account_id"``) for an id no account can have.

    Accepts at most :data:`ACCOUNT_ID_MAX_LENGTH` characters of ``[A-Za-z0-9][A-Za-z0-9_-]*``. The
    refusal never carries the value in its message; ``detail`` holds a bounded prefix.
    """
    if len(account_id) <= ACCOUNT_ID_MAX_LENGTH and _ACCOUNT_ID_PATTERN.fullmatch(account_id):
        return
    raise ValidationFailed(
        "account_id is not a valid account id.",
        source="account_id",
        detail=f"length={len(account_id)} value={bounded_detail_value(account_id)}",
    )


def search_opportunities(
    store: Store,
    identity: Identity,
    *,
    query: str | None = None,
    stage: str | None = None,
    owner_rep_id: str | None = None,
    account_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> SearchResult:
    """Search opportunities within ``identity``'s tenant and book, with a bounded examination cost.

    Queries the GSI chosen by :func:`_index_for`, applies the remaining free-text, stage, and owner
    filters in process, and scope-filters every match.

    Args:
        store: The persistence layer.
        identity: The authenticated caller; results are constrained to its tenant and book.
        query: Free-text tokens matched case-insensitively against the opportunity's precomputed
            ``name_lower`` attribute. ``None`` or empty applies no text filter.
        stage: Restrict to one lifecycle stage.
        owner_rep_id: Restrict to one owning rep.
        account_id: Restrict to one account, queried on that account's tenant-prefixed partition.
            An account outside the caller's book is denied before any read, whether or not it
            exists.
        cursor: A ``next_cursor`` from a prior truncated call with the same
            ``stage``/``owner_rep_id``/``account_id`` filters.
        limit: Maximum number of matching items to return. Examination may stop before ``limit``
            matches are found if the budget is exhausted first.

    Returns:
        A :class:`SearchResult`. Each item carries the index's projected attributes, including
        ``close_date``, ``value`` and ``stage`` where the opportunity has them.

    Raises:
        ScopeDenied: If the caller has an empty book and no full visibility (``account:*``), or
            ``account_id`` is outside its book (``account:<id>``).
        ValidationFailed: If ``cursor`` is not one this operation issued to ``identity``,
            ``account_id`` or ``owner_rep_id`` is the empty string, or ``account_id`` is malformed
            or longer than :data:`ACCOUNT_ID_MAX_LENGTH`.
    """
    require_listing_scope(identity, operation="search_opportunities")
    for source, value in (("account_id", account_id), ("owner_rep_id", owner_rep_id)):
        if value == "":
            raise ValidationFailed(f"{source} must not be empty.", source=source)
    if account_id is not None:
        _require_well_formed_account_id(account_id)
        require_scope(identity, account_id, operation="search_opportunities")
    index_name = _index_for(stage=stage, owner_rep_id=owner_rep_id, account_id=account_id)
    partition_value = _partition_value(
        identity, index_name, stage=stage, owner_rep_id=owner_rep_id, account_id=account_id
    )
    tokens = tuple(token.lower() for token in (query or "").split() if token)

    def matches(item: dict[str, object]) -> bool:
        return (
            _matches_stage(item, stage)
            and _matches_owner(item, owner_rep_id)
            and _matches_query(item, tokens)
        )

    return _examine(
        store,
        identity,
        _TABLE,
        index_name,
        operation="search_opportunities",
        partition_value=partition_value,
        cursor=cursor,
        limit=limit,
        matches=matches,
    )


def _account_index_for(*, owner_rep_id: str | None) -> str:
    """Choose ``gsi_owner`` for an owner filter, otherwise the tenant-wide ``gsi_tenant``."""
    if owner_rep_id is not None:
        return _ACCOUNT_GSI_OWNER
    return _ACCOUNT_GSI_TENANT


def _account_partition_value(
    identity: Identity, index_name: str, *, owner_rep_id: str | None
) -> str:
    """Build the account index partition-key value: ``tenant`` or a tenant-prefixed composite."""
    if index_name == _ACCOUNT_GSI_OWNER:
        if owner_rep_id is None:  # pragma: no cover - guarded by _account_index_for's selection
            raise ValueError("gsi_owner requires owner_rep_id")
        return tenant_prefixed(identity.tenant, owner_rep_id)
    return identity.tenant


def _matches_account_query(item: dict[str, object], tokens: tuple[str, ...]) -> bool:
    """Return whether every free-text token appears in the account's name, case-insensitively.

    Matches on ``name`` lowercased in process: ``name`` is projected on both account indexes,
    whereas ``name_lower`` is not projected on ``gsi_owner``.
    """
    if not tokens:
        return True
    name = item.get("name")
    haystack = name.lower() if isinstance(name, str) else ""
    return all(token in haystack for token in tokens)


def search_accounts(
    store: Store,
    identity: Identity,
    *,
    query: str | None = None,
    owner_rep_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> SearchResult:
    """Search accounts within ``identity``'s tenant and book, with a bounded examination cost.

    Budget, cursor, and truncation semantics are those of :func:`search_opportunities`.

    Args:
        store: The persistence layer.
        identity: The authenticated caller; results are constrained to its tenant and book.
        query: Free-text tokens matched case-insensitively against the account ``name``. ``None`` or
            empty applies no text filter.
        owner_rep_id: Restrict to accounts owned by one rep. Result rows carry ``owner_rep_id``
            only when this is given (``gsi_tenant`` does not project it).
        cursor: A ``next_cursor`` from a prior truncated call with the same ``owner_rep_id``.
        limit: Maximum number of matching accounts to return.

    Returns:
        A :class:`SearchResult`.

    Raises:
        ScopeDenied: If the caller has an empty book and no full visibility (``account:*``).
        ValidationFailed: If ``cursor`` is not one this operation issued to ``identity``, or
            ``owner_rep_id`` is the empty string.
    """
    require_listing_scope(identity, operation="search_accounts")
    if owner_rep_id == "":
        raise ValidationFailed("owner_rep_id must not be empty.", source="owner_rep_id")
    index_name = _account_index_for(owner_rep_id=owner_rep_id)
    partition_value = _account_partition_value(identity, index_name, owner_rep_id=owner_rep_id)
    tokens = tuple(token.lower() for token in (query or "").split() if token)

    def matches(item: dict[str, object]) -> bool:
        return _matches_owner(item, owner_rep_id) and _matches_account_query(item, tokens)

    return _examine(
        store,
        identity,
        _ACCOUNTS_TABLE,
        index_name,
        operation="search_accounts",
        partition_value=partition_value,
        cursor=cursor,
        limit=limit,
        matches=matches,
    )
