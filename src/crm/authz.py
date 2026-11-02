# SPDX-License-Identifier: MIT
"""Caller identity and per-rep book-of-accounts scope enforcement.

Every action handler calls into this module to check scope at the Lambda tool boundary. The same
predicate serves single-record checks, list filtering, search filtering, and aggregates.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal, TypeVar

from crm.errors import ScopeDenied

IdentitySource = Literal["apigw", "gateway", "agent"]
PrincipalType = Literal["user", "service"]

DEMO_TENANT: Final[str] = "demo"

#: Cognito group whose members may perform admin-only actions.
ADMIN_GROUP: Final[str] = "admin"

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class Identity:
    """An authenticated caller and the scope it may act within.

    Attributes:
        rep_id: The CRM rep this caller acts as.
        book: Account identifiers the rep owns. Ignored by :meth:`may_access` when
            ``full_visibility`` is set.
        full_visibility: True when the rep may see every account in the tenant.
        principal_type: ``user`` for a human via Cognito, ``service`` for a SigV4 principal.
        session_id: Correlates a chat turn with its subagent invocations and tool calls.
        source: Which of the three identity channels produced this value.
        tenant: Isolation boundary. ``demo`` for demonstration data, ``eval-<run_id>`` for a run.
        groups: Cognito groups the caller belongs to. Non-empty only on the ``apigw`` channel.
    """

    rep_id: str
    book: frozenset[str] = field(default_factory=frozenset)
    full_visibility: bool = False
    principal_type: PrincipalType = "user"
    session_id: str = ""
    source: IdentitySource = "apigw"
    tenant: str = DEMO_TENANT
    groups: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Reject an identity that could not have come from a real authenticated caller."""
        if not self.rep_id:
            raise ValueError("Identity requires a non-empty rep_id")
        if not self.tenant:
            raise ValueError("Identity requires a non-empty tenant")
        # A service principal must carry an explicit book.
        if self.principal_type == "service" and self.full_visibility:
            raise ValueError("a service principal may not hold implicit full visibility")
        if self.groups and self.source != "apigw":
            raise ValueError(
                f"group membership is decoded on the apigw channel only, not {self.source}"
            )

    @property
    def is_admin(self) -> bool:
        """Return whether this caller is a user principal in the :data:`ADMIN_GROUP` group."""
        return self.principal_type == "user" and ADMIN_GROUP in self.groups

    def may_access(self, account_id: str) -> bool:
        """Return whether this caller may read or write records under ``account_id``."""
        if self.full_visibility:
            return True
        return account_id in self.book

    def scope_description(self) -> str:
        """Render the caller's scope for logs and denial messages. Never includes a token."""
        if self.full_visibility:
            return f"rep={self.rep_id} scope=all-accounts tenant={self.tenant}"
        return f"rep={self.rep_id} scope={len(self.book)}-accounts tenant={self.tenant}"


def require_scope(identity: Identity, account_id: str, *, operation: str = "access") -> None:
    """Raise :class:`ScopeDenied` unless ``identity`` may act on ``account_id``.

    The denial names the missing scope.

    Args:
        identity: The authenticated caller.
        account_id: The account the target record belongs to.
        operation: Logical operation name, used in the internal detail only.

    Raises:
        ScopeDenied: When the account is outside the caller's book.
    """
    if identity.may_access(account_id):
        return
    raise ScopeDenied(
        "You do not have access to that account.",
        missing_scope=f"account:{account_id}",
        detail=f"operation={operation} {identity.scope_description()}",
    )


def require_listing_scope(identity: Identity, *, operation: str) -> None:
    """Raise ScopeDenied(missing_scope="account:*") unless identity.full_visibility or identity.book.

    Guards the tenant-wide, book-filtered listings, so an empty book is a denial rather than an
    empty result.

    Args:
        identity: The authenticated caller.
        operation: Logical operation name, used in the internal detail only.

    Raises:
        ScopeDenied: When the caller has neither full visibility nor any account in its book.
    """
    if identity.full_visibility or identity.book:
        return
    raise ScopeDenied(
        "You do not have access to any accounts.",
        missing_scope="account:*",
        detail=f"operation={operation} reason=empty_book {identity.scope_description()}",
    )


def require_record_scope(identity: Identity, record: object, *, operation: str = "access") -> None:
    """Raise :class:`ScopeDenied` unless ``identity`` may act on ``record``.

    The single-record counterpart to :func:`is_in_scope`; fails closed on every axis.

    Args:
        identity: The authenticated caller.
        record: The record the operation targets; a non-mapping is denied.
        operation: Logical operation name, used in the internal detail only.

    Raises:
        ScopeDenied: The record is not a mapping, its scope is undeterminable on either axis, its
            tenant differs from the caller's, or its account lies outside the caller's book.
    """
    if not isinstance(record, dict):
        raise ScopeDenied(
            "That record is not available in this context.",
            missing_scope="record",
            detail=(
                f"operation={operation} reason=scope_source_not_a_mapping "
                f"type={type(record).__name__} {identity.scope_description()}"
            ),
        )
    try:
        record_tenant = tenant_of(record)
    except ValueError:
        raise ScopeDenied(
            "That record is not available in this context.",
            missing_scope="tenant",
            detail=(
                f"operation={operation} reason=record_tenant_undeterminable "
                f"{identity.scope_description()}"
            ),
        ) from None
    require_tenant(identity, record_tenant, operation=operation)
    try:
        account_id = account_id_of(record)
    except ValueError:
        raise ScopeDenied(
            "You do not have access to that account.",
            missing_scope="account",
            detail=(
                f"operation={operation} reason=record_account_undeterminable "
                f"{identity.scope_description()}"
            ),
        ) from None
    require_scope(identity, account_id, operation=operation)


#: The identity channels whose caller may decide a proposal or revert an audit record; not ``agent``.
PROPOSAL_DECIDING_SOURCES: Final[frozenset[IdentitySource]] = frozenset({"apigw", "gateway"})

#: The only principal type that may decide a proposal or revert an audit record.
PROPOSAL_DECIDING_PRINCIPAL: Final[PrincipalType] = "user"


def require_proposal_decider(identity: Identity, *, operation: str) -> None:
    """Raise :class:`ScopeDenied` unless ``identity`` is a person on a deciding channel.

    Used by approve, reject and revert. Fails closed: the channel must be in
    :data:`PROPOSAL_DECIDING_SOURCES` and the principal type must be
    :data:`PROPOSAL_DECIDING_PRINCIPAL`.

    Args:
        identity: The authenticated caller.
        operation: Logical operation name, used in the internal detail only.

    Raises:
        ScopeDenied: The identity arrived through any other channel (``missing_scope``
            ``channel:<source>``) or is not a person (``principal:<type>``).
    """
    if identity.source not in PROPOSAL_DECIDING_SOURCES:
        missing_scope = f"channel:{identity.source}"
    elif identity.principal_type != PROPOSAL_DECIDING_PRINCIPAL:
        missing_scope = f"principal:{identity.principal_type}"
    else:
        return
    raise ScopeDenied(
        "Only a person can approve, reject or revert a change. Do it in the app or an MCP client.",
        missing_scope=missing_scope,
        detail=(
            f"operation={operation} source={identity.source} "
            f"principal_type={identity.principal_type} {identity.scope_description()}"
        ),
    )


def require_tenant(identity: Identity, record_tenant: str, *, operation: str = "access") -> None:
    """Raise :class:`ScopeDenied` when a record belongs to a different tenant.

    Used for records fetched by primary key. ``missing_scope`` names only ``"tenant"``; the
    record's tenant appears only in the log-only ``detail``.
    """
    if record_tenant == identity.tenant:
        return
    raise ScopeDenied(
        "That record is not available in this context.",
        missing_scope="tenant",
        detail=f"operation={operation} identity_tenant={identity.tenant} record_tenant={record_tenant}",
    )


def account_id_of(record: dict[str, object]) -> str:
    """Extract the account a record is scoped by.

    Raises:
        ValueError: When the record has no non-empty ``account_id`` or ``accountId``.
    """
    for key in ("account_id", "accountId"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"record has no account scope key: keys={sorted(record)}")


def tenant_of(record: dict[str, object]) -> str:
    """Extract the tenant a record belongs to.

    Raises:
        ValueError: When ``tenant`` is absent, blank, or not a string.
    """
    value = record.get("tenant")
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError(f"record has no tenant scope key: keys={sorted(record)}")


def is_in_scope(identity: Identity, record: object) -> bool:
    """Return whether ``identity`` may see ``record``, failing closed on any undeterminable axis.

    A record is visible only when it is a ``dict`` whose tenant equals the caller's (regardless of
    ``full_visibility``) and whose account the caller may access.
    """
    if not isinstance(record, dict):
        return False
    try:
        record_tenant = tenant_of(record)
    except ValueError:
        return False
    if record_tenant != identity.tenant:
        return False
    try:
        account_id = account_id_of(record)
    except ValueError:
        return False
    return identity.may_access(account_id)


def filter_to_scope(
    identity: Identity, records: Iterable[dict[str, object]]
) -> list[dict[str, object]]:
    """Return only the records ``identity`` may see, as a concrete list.

    Used by list, search, and aggregate paths; the decision is :func:`is_in_scope`'s.
    """
    return [record for record in records if is_in_scope(identity, record)]
