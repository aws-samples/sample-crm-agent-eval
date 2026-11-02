# SPDX-License-Identifier: MIT
"""Per-user tuning settings: one ``reps`` item per user principal.

:func:`get_settings`, :func:`save_settings` and :func:`reset_settings` always address the caller's
own item, keyed by :func:`crm.store.keys.user_settings_key` from the identity's tenant and rep id;
no function accepts a rep id. The item carries no ``tenant`` attribute, so it never appears in
``reps.gsi_tenant``. A service principal has no settings: a read returns the defaults and a write
is refused with :class:`~crm.errors.ScopeDenied`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from crm.authz import Identity
from crm.errors import CrmError, ScopeDenied, ValidationFailed
from crm.store import keys
from crm.store.store import Store
from providers.capabilities import CapabilityMatrix, load_capability_matrix
from providers.tier_tuning import TurnTuning, defaults, parse_stored_tuning, resolve_turn, to_wire

SETTINGS_TABLE: Final[str] = "reps"
SETTINGS_KIND: Final[str] = "user_settings"
SETTINGS_SCHEMA_VERSION: Final[int] = 2

#: The ``source`` recorded on every saved tuning block.
SETTINGS_SOURCE: Final[str] = "settings.tuning"

#: :attr:`SettingsView.notice` when a saved tuning parses but no longer resolves against the matrix.
NOTICE_SAVED_MODEL_UNAVAILABLE: Final[str] = "saved_model_unavailable"

#: :attr:`SettingsView.notice` when a saved item's tuning block cannot be parsed at all.
NOTICE_SAVED_SETTINGS_UNREADABLE: Final[str] = "saved_settings_unreadable"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SettingsView:
    """The caller's effective tuning settings.

    Attributes:
        tuning: A wire tuning block at :data:`providers.tier_tuning.WIRE_SCHEMA_VERSION`
            (:func:`providers.tier_tuning.to_wire`).
        is_default: True when ``tuning`` is the ``config/models.yaml`` default, not a saved item.
        notice: :data:`NOTICE_SAVED_MODEL_UNAVAILABLE` or :data:`NOTICE_SAVED_SETTINGS_UNREADABLE`
            when a saved item was replaced by the defaults, otherwise ``None``.
        updated_at: When the saved item was written, or ``None`` for the defaults.
    """

    tuning: dict[str, object]
    is_default: bool
    notice: str | None = None
    updated_at: str | None = None

    def to_payload(self) -> dict[str, object]:
        """Render the view as the ``/api/user_settings`` response body."""
        return {
            "tuning": self.tuning,
            "is_default": self.is_default,
            "notice": self.notice,
            "updated_at": self.updated_at,
        }


def _settings_key(identity: Identity) -> dict[str, str]:
    return {"rep_id": keys.user_settings_key(identity.tenant, identity.rep_id)}


def _default_view(matrix: CapabilityMatrix, *, notice: str | None = None) -> SettingsView:
    return SettingsView(tuning=to_wire(defaults(matrix)), is_default=True, notice=notice)


def _replaced_by_defaults(
    matrix: CapabilityMatrix, identity: Identity, notice: str
) -> SettingsView:
    logger.warning(
        json.dumps(
            {
                "event": "user_settings_replaced_by_defaults",
                "reason": notice,
                "rep_id": identity.rep_id,
            }
        )
    )
    return _default_view(matrix, notice=notice)


def _require_user_principal(identity: Identity, *, operation: str) -> None:
    if identity.principal_type != "user":
        raise ScopeDenied(
            "Only a signed-in user can change tuning settings.",
            missing_scope="user_settings",
            detail=f"operation={operation} principal_type={identity.principal_type}",
        )


def _require_own_item(item: dict[str, object], identity: Identity) -> None:
    if (
        item.get("kind") != SETTINGS_KIND
        or item.get("owner_rep_id") != identity.rep_id
        or item.get("settings_tenant") != identity.tenant
    ):
        raise CrmError(
            "Saved settings could not be read.",
            detail=f"settings item at {_settings_key(identity)['rep_id']} has a mismatched owner",
        )


def get_settings(
    store: Store, identity: Identity, *, matrix: CapabilityMatrix | None = None
) -> SettingsView:
    """Return the caller's saved tuning, or the defaults when there is none.

    The saved block is read with :func:`providers.tier_tuning.parse_stored_tuning`, so an item
    written at an earlier settings schema still reads, without its retired fields; the read never
    rewrites the item. A saved tuning that no longer resolves against ``matrix`` (for example its model was removed)
    yields the defaults with ``notice=NOTICE_SAVED_MODEL_UNAVAILABLE``; one whose block does not
    parse yields ``notice=NOTICE_SAVED_SETTINGS_UNREADABLE``. Both are logged. A service principal
    always gets the defaults, without a store read.

    Args:
        store: The persistence layer.
        identity: The authenticated caller; its item is the only one read.
        matrix: The capability matrix; loaded from ``config/`` when omitted.

    Raises:
        CrmError: The item at the caller's key is not the caller's settings item.
    """
    resolved_matrix = matrix if matrix is not None else load_capability_matrix()
    if identity.principal_type != "user":
        return _default_view(resolved_matrix)
    item = store.get_item(SETTINGS_TABLE, _settings_key(identity))
    if item is None:
        return _default_view(resolved_matrix)
    _require_own_item(item, identity)
    try:
        tuning = parse_stored_tuning(item.get("tuning"), default_source=SETTINGS_SOURCE)
    except ValidationFailed:
        return _replaced_by_defaults(resolved_matrix, identity, NOTICE_SAVED_SETTINGS_UNREADABLE)
    try:
        resolve_turn(resolved_matrix, tuning)
    except ValidationFailed:
        return _replaced_by_defaults(resolved_matrix, identity, NOTICE_SAVED_MODEL_UNAVAILABLE)
    updated_at = item.get("updated_at")
    return SettingsView(
        tuning=to_wire(tuning),
        is_default=False,
        updated_at=updated_at if isinstance(updated_at, str) else None,
    )


def save_settings(
    store: Store,
    identity: Identity,
    tuning: TurnTuning,
    *,
    matrix: CapabilityMatrix | None = None,
) -> SettingsView:
    """Validate ``tuning`` and save it as the caller's settings, replacing any earlier save.

    Args:
        store: The persistence layer.
        identity: The authenticated caller; its item is the only one written.
        tuning: The requested per-tier tuning.
        matrix: The capability matrix; loaded from ``config/`` when omitted.

    Returns:
        The saved view.

    Raises:
        ScopeDenied: The caller is not a user principal.
        ValidationFailed: ``tuning`` does not resolve against ``matrix``
            (:func:`providers.tier_tuning.resolve_turn`); nothing is written.
    """
    _require_user_principal(identity, operation="user_settings:put")
    resolved_matrix = matrix if matrix is not None else load_capability_matrix()
    stored = TurnTuning(source=SETTINGS_SOURCE, tiers=tuning.tiers)
    resolve_turn(resolved_matrix, stored)
    wire = to_wire(stored)
    updated_at = datetime.now(UTC).isoformat()
    store.put_item(
        SETTINGS_TABLE,
        {
            **_settings_key(identity),
            "kind": SETTINGS_KIND,
            "settings_tenant": identity.tenant,
            "owner_rep_id": identity.rep_id,
            "tuning": wire,
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "updated_at": updated_at,
        },
    )
    return SettingsView(tuning=wire, is_default=False, updated_at=updated_at)


def reset_settings(
    store: Store, identity: Identity, *, matrix: CapabilityMatrix | None = None
) -> SettingsView:
    """Delete the caller's saved settings and return the defaults.

    Raises:
        ScopeDenied: The caller is not a user principal.
    """
    _require_user_principal(identity, operation="user_settings:reset")
    resolved_matrix = matrix if matrix is not None else load_capability_matrix()
    store.delete_item(SETTINGS_TABLE, _settings_key(identity))
    return _default_view(resolved_matrix)


__all__ = [
    "NOTICE_SAVED_MODEL_UNAVAILABLE",
    "NOTICE_SAVED_SETTINGS_UNREADABLE",
    "SETTINGS_KIND",
    "SETTINGS_SCHEMA_VERSION",
    "SETTINGS_SOURCE",
    "SETTINGS_TABLE",
    "SettingsView",
    "get_settings",
    "reset_settings",
    "save_settings",
]
