# SPDX-License-Identifier: MIT
"""Scope-constrained opportunity and account search: a GSI query plus in-process token filtering.

A tenant-scoped GSI query retrieves candidates in bounded pages and free-text matching runs in
this process. At most :data:`EXAMINATION_BUDGET` items are examined per call; a cut-off result is
marked ``truncated=True`` and carries a ``next_cursor`` to resume from. Every returned record is
scope-filtered by :func:`crm.authz.filter_to_scope`.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from crm.authz import Identity, filter_to_scope, require_listing_scope, require_scope
from crm.errors import ValidationFailed, bounded_detail_value
from crm.store.keys import tenant_prefixed
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
    """The outcome of one :func:`search_opportunities` call.

    Attributes:
        items: In-scope opportunities matching the query, up to the point examination stopped.
        truncated: ``True`` when :data:`EXAMINATION_BUDGET` items were examined, or more than
            ``limit`` raw matches were found, so ``items`` may not be the complete match set.
        examined_count: Total items read from the index across every page this call issued.
        next_cursor: An opaque token resuming examination where this call stopped, passed back as
            ``cursor`` with the same filters; ``None`` when not truncated or nothing remains.
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


def encode_cursor(page_key: dict[str, object]) -> str:
    """Serialise a raw DynamoDB page key into the opaque ``next_cursor`` string."""
    raw = json.dumps(page_key, sort_keys=True, default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(token: str) -> dict[str, object]:
    """Parse an opaque cursor string back into the raw DynamoDB page key it encodes.

    Raises:
        ValidationFailed: If ``token`` is not a cursor produced by :func:`encode_cursor`; the
            error's source is ``cursor``.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        decoded = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - any decode failure means a malformed cursor
        raise ValidationFailed("The search cursor is not valid.", source="cursor") from exc
    if not isinstance(decoded, dict):
        raise ValidationFailed("The search cursor is not valid.", source="cursor")
    return decoded


def _examine(
    store: Store,
    table: str,
    index_name: str,
    *,
    partition_value: str,
    cursor: str | None,
    limit: int | None,
    matches: Callable[[dict[str, object]], bool],
) -> tuple[list[dict[str, object]], int, bool, dict[str, object] | None]:
    """Run the bounded, cursor-resumable GSI examination loop shared by both search entry points.

    Pages the chosen index, applying ``matches`` in this process, until the index is exhausted, the
    :data:`EXAMINATION_BUDGET` is exhausted, or more raw matches than ``limit`` have been collected.
    Returns the raw (pre-scope-filter) matches, the number of items examined, whether examination
    stopped early, and the page key to resume from.
    """
    page_key = decode_cursor(cursor) if cursor is not None else None
    matched: list[dict[str, object]] = []
    examined_count = 0
    truncated = False

    while True:
        remaining_budget = EXAMINATION_BUDGET - examined_count
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
        examined_count += len(raw_items)
        matched.extend(item for item in raw_items if matches(item))

        index_exhausted = page_key is None
        budget_exhausted = examined_count >= EXAMINATION_BUDGET
        # Matches beyond the limit are withheld, so this truncates even on an exhausted index.
        limit_exceeded = limit is not None and len(matched) > limit

        if limit_exceeded or budget_exhausted:
            truncated = True
            break
        if index_exhausted:
            break

    return matched, examined_count, truncated, page_key


def _build_result(
    identity: Identity,
    matched: list[dict[str, object]],
    examined_count: int,
    truncated: bool,
    page_key: dict[str, object] | None,
    limit: int | None,
) -> SearchResult:
    """Scope-filter the raw matches, apply the ``limit`` slice, and assemble the :class:`SearchResult`.

    ``next_cursor`` is present only when examination truncated with a page left to resume from.
    """
    in_scope = filter_to_scope(identity, matched)
    if limit is not None:
        in_scope = in_scope[:limit]

    return SearchResult(
        items=in_scope,
        truncated=truncated,
        examined_count=examined_count,
        next_cursor=encode_cursor(page_key) if truncated and page_key is not None else None,
    )


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
        ValidationFailed: If ``cursor`` is not a validly encoded token, ``account_id`` or
            ``owner_rep_id`` is the empty string, or ``account_id`` is malformed or longer than
            :data:`ACCOUNT_ID_MAX_LENGTH`.
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

    matched, examined_count, truncated, page_key = _examine(
        store,
        _TABLE,
        index_name,
        partition_value=partition_value,
        cursor=cursor,
        limit=limit,
        matches=matches,
    )
    return _build_result(identity, matched, examined_count, truncated, page_key, limit)


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
        ValidationFailed: If ``cursor`` is not a validly encoded token, or ``owner_rep_id`` is
            the empty string.
    """
    require_listing_scope(identity, operation="search_accounts")
    if owner_rep_id == "":
        raise ValidationFailed("owner_rep_id must not be empty.", source="owner_rep_id")
    index_name = _account_index_for(owner_rep_id=owner_rep_id)
    partition_value = _account_partition_value(identity, index_name, owner_rep_id=owner_rep_id)
    tokens = tuple(token.lower() for token in (query or "").split() if token)

    def matches(item: dict[str, object]) -> bool:
        return _matches_owner(item, owner_rep_id) and _matches_account_query(item, tokens)

    matched, examined_count, truncated, page_key = _examine(
        store,
        _ACCOUNTS_TABLE,
        index_name,
        partition_value=partition_value,
        cursor=cursor,
        limit=limit,
        matches=matches,
    )
    return _build_result(identity, matched, examined_count, truncated, page_key, limit)
