# SPDX-License-Identifier: MIT
"""Tenant-prefixed composite key construction and derived index attributes.

On the tenant-scoped tables every GSI partition key in :mod:`crm.store.schema` is either ``tenant``
or a composite ``"<tenant>#<dimension value>"`` built here, so a query against one tenant's
partition cannot return another tenant's items.

Every index key attribute computed from other attributes of the same item is declared in
:data:`DERIVED_ATTRIBUTES` and computed by :func:`derive`; the :class:`crm.store.store.Store` write
methods re-derive them on every write. This module issues no writes itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

_SEPARATOR = "#"

#: Attribute naming the isolation tenant on every item, and the first part of every composite.
TENANT_ATTRIBUTE: Final[str] = "tenant"


def tenant_prefixed(tenant: str, *parts: str) -> str:
    """Build a tenant-prefixed composite GSI partition key value.

    Args:
        tenant: The isolation tenant — ``"demo"`` or ``"eval-<run_id>"``.
        parts: One or more dimension values to join after the tenant, in order.

    Returns:
        ``"<tenant>#<part1>#<part2>..."``.

    Raises:
        ValueError: If ``tenant`` is empty, no part is given, or any part is empty.
    """
    if not tenant:
        raise ValueError("tenant must not be empty")
    if not parts:
        raise ValueError("at least one dimension part is required")
    for part in parts:
        if not part:
            raise ValueError(f"dimension part must not be empty: parts={parts!r}")
    return _SEPARATOR.join((tenant, *parts))


def stage_close_date(stage: str, close_date: str) -> str:
    """Build the ``opportunities.gsi_owner_stage`` sort-key value ``"<stage>#<close_date>"``."""
    if not stage or not close_date:
        raise ValueError("stage and close_date must both be non-empty")
    return f"{stage}{_SEPARATOR}{close_date}"


def run_id_task_id(run_id: str, task_id: str) -> str:
    """Build the ``eval_results.gsi_task`` partition-key value ``"<run_id>#<task_id>"``."""
    if not run_id or not task_id:
        raise ValueError("run_id and task_id must both be non-empty")
    return f"{run_id}{_SEPARATOR}{task_id}"


#: ``eval_runs`` primary key of the single-flight sweep lock row.
SWEEP_LOCK_ROW_ID: Final[str] = "lock#sweep"

#: Exclusive upper bound on an ``eval_results`` arm index, so ``result_id`` sorts by arm.
_MAX_ARM_INDEX: Final[int] = 100


def user_settings_key(tenant: str, rep_id: str) -> str:
    """Build the ``reps`` primary key of a user's settings item, ``"usersettings#<tenant>#<rep_id>"``.

    Raises:
        ValueError: If ``tenant`` or ``rep_id`` is empty.
    """
    if not tenant or not rep_id:
        raise ValueError("tenant and rep_id must both be non-empty")
    return _SEPARATOR.join(("usersettings", tenant, rep_id))


def conversation_key(conversation_id: str) -> str:
    """Build the ``reps`` primary key of a conversation-ownership item, ``"conversation#<id>"``.

    Raises:
        ValueError: If ``conversation_id`` is empty.
    """
    if not conversation_id:
        raise ValueError("conversation_id must be non-empty")
    return f"conversation{_SEPARATOR}{conversation_id}"


def eval_result_id(arm_index: int, task_id: str) -> str:
    """Build the ``eval_results`` sort-key value ``"<arm_index:02d>#<task_id>"``.

    Raises:
        ValueError: If ``arm_index`` is not an ``int`` in ``[0, 100)`` or ``task_id`` is empty.
    """
    if isinstance(arm_index, bool) or not isinstance(arm_index, int):
        raise ValueError(f"arm_index must be an int, got {type(arm_index).__name__}")
    if not 0 <= arm_index < _MAX_ARM_INDEX:
        raise ValueError(f"arm_index must be in [0, {_MAX_ARM_INDEX}), got {arm_index}")
    if not task_id:
        raise ValueError("task_id must be non-empty")
    return f"{arm_index:02d}{_SEPARATOR}{task_id}"


def split_tenant_prefixed(value: str) -> tuple[str, str]:
    """Split a tenant-prefixed composite value back into ``(tenant, remainder)``.

    Raises:
        ValueError: If ``value`` carries no ``#`` separator.
    """
    if _SEPARATOR not in value:
        raise ValueError(f"not a tenant-prefixed composite key: {value!r}")
    tenant, remainder = value.split(_SEPARATOR, 1)
    return tenant, remainder


def name_lower(name: str) -> str:
    """Build the ``name_lower`` search key every named entity's ``gsi_tenant`` sorts on."""
    return name.lower()


@dataclass(frozen=True, slots=True)
class DerivedAttribute:
    """One index key attribute computed from other attributes of the same item.

    Attributes:
        name: The attribute this declaration computes.
        sources: Attribute names its value is computed from, in the order ``build`` takes them.
            Every source must be a non-empty string for the derived attribute to be written.
        build: Pure function from the source values to the derived value.
    """

    name: str
    sources: tuple[str, ...]
    build: Callable[..., str]


def _composite(name: str, *sources: str) -> DerivedAttribute:
    """Declare a tenant-prefixed composite attribute built by :func:`tenant_prefixed`."""
    return DerivedAttribute(name=name, sources=(TENANT_ATTRIBUTE, *sources), build=tenant_prefixed)


_NAME_LOWER = DerivedAttribute(name="name_lower", sources=("name",), build=name_lower)

#: Every derived index key attribute, per table. A table absent from this mapping has none.
DERIVED_ATTRIBUTES: Final[dict[str, tuple[DerivedAttribute, ...]]] = {
    "accounts": (_composite("tenant_owner_rep_id", "owner_rep_id"), _NAME_LOWER),
    "contacts": (_composite("tenant_account_id", "account_id"), _NAME_LOWER),
    "opportunities": (
        _composite("tenant_account_id", "account_id"),
        _composite("tenant_owner_rep_id", "owner_rep_id"),
        _composite("tenant_stage", "stage"),
        DerivedAttribute(
            name="stage_close_date", sources=("stage", "close_date"), build=stage_close_date
        ),
        _NAME_LOWER,
    ),
    "activities": (
        _composite("tenant_opportunity_id", "opportunity_id"),
        _composite("tenant_account_id", "account_id"),
    ),
    "notes": (_composite("tenant_opportunity_id", "opportunity_id"),),
    "proposals": (
        _composite("tenant_status", "status"),
        _composite("tenant_actor_rep_id", "actor_rep_id"),
        _composite("tenant_target_id", "target_id"),
    ),
    "audit_records": (
        _composite("tenant_target_id", "target_id"),
        _composite("tenant_actor_rep_id", "actor_rep_id"),
    ),
    "usage_records": (
        _composite("tenant_correlation_id", "correlation_id"),
        _composite("tenant_run_id", "run_id"),
    ),
    "eval_results": (
        DerivedAttribute(
            name="run_id_task_id", sources=("run_id", "task_id"), build=run_id_task_id
        ),
    ),
}

#: Index key attributes stored as supplied rather than computed from siblings.
VERBATIM_INDEX_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        TENANT_ATTRIBUTE,
        "arm",
        "close_date",
        "created_at",
        "occurred_at",
        "rep_id",
        "result_id",
        "run_id",
        "started_at",
        "status",
    }
)


def derived_attributes(table: str) -> tuple[DerivedAttribute, ...]:
    """Return every derived attribute declared for ``table``, empty for a table with none."""
    return DERIVED_ATTRIBUTES.get(table, ())


def derived_attribute_names(table: str) -> frozenset[str]:
    """Return the names of ``table``'s derived attributes."""
    return frozenset(spec.name for spec in derived_attributes(table))


def derived_source_attributes(table: str) -> frozenset[str]:
    """Return every attribute of ``table`` some derived attribute is computed from."""
    return frozenset(source for spec in derived_attributes(table) for source in spec.sources)


def derive(table: str, item: Mapping[str, object]) -> dict[str, object]:
    """Return ``item`` with every derived attribute of ``table`` recomputed from its sources.

    A derived attribute whose sources are not all non-empty strings is removed, so the item is
    absent from that index.

    Args:
        table: One of :data:`crm.store.schema.TABLE_NAMES`. A table with no declared derived
            attribute is returned copied but unchanged.
        item: The item as its writer built it, carrying the source attributes.

    Returns:
        A new dict — the caller's mapping is never mutated.

    Example:
        >>> derive("opportunities", {"tenant": "demo", "stage": "Won", "close_date": "2026-01-01"})[
        ...     "tenant_stage"
        ... ]
        'demo#Won'
    """
    derived = dict(item)
    for spec in derived_attributes(table):
        sources = [derived.get(source) for source in spec.sources]
        if all(isinstance(value, str) and value for value in sources):
            derived[spec.name] = spec.build(*sources)
        else:
            derived.pop(spec.name, None)
    return derived


def derive_encoded(table: str, item: Mapping[str, object]) -> dict[str, object]:
    """Return a DynamoDB-encoded ``item`` with ``table``'s derived attributes recomputed.

    The :func:`derive` equivalent for an item in DynamoDB's typed attribute-value format. Every
    non-derived attribute value is passed through unchanged.

    Args:
        table: One of :data:`crm.store.schema.TABLE_NAMES`.
        item: The encoded item — a mapping of attribute name to a single-key typed value dict.

    Returns:
        A new dict in the same encoded form.
    """
    derived = dict(item)
    for spec in derived_attributes(table):
        sources = [_encoded_string(derived.get(source)) for source in spec.sources]
        if all(value for value in sources):
            derived[spec.name] = {"S": spec.build(*sources)}
        else:
            derived.pop(spec.name, None)
    return derived


def _encoded_string(value: object) -> str | None:
    """Return the string an encoded attribute value holds, or ``None`` if it holds anything else."""
    if isinstance(value, Mapping):
        raw = value.get("S")
        if isinstance(raw, str):
            return raw
    return None
