# SPDX-License-Identifier: MIT
"""``user_settings`` Lambda handler: read, save or reset the caller's own tuning settings.

``POST /api/user_settings`` with ``{"action": "get" | "put" | "reset", "tuning"?: <schema-3 block>}``.
``tuning`` is required for ``put`` and refused otherwise. :data:`ARGUMENTS_SCHEMA` is closed
(``additionalProperties: false``) and has no ``rep_id``: the item is always the caller's own.
Not an action tool: it composes :mod:`crm.handlers._common`'s plumbing and validates its
own arguments. Responses are :meth:`crm.user_settings.SettingsView.to_payload`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from crm.authz import Identity
from crm.errors import CrmError, ValidationFailed, bounded_detail_value
from crm.handlers._common import decode, default_store, error_response, is_proxy_event, ok
from crm.store.store import Store
from crm.user_settings import (
    SETTINGS_SOURCE,
    SettingsView,
    get_settings,
    reset_settings,
    save_settings,
)
from providers.capabilities import CapabilityMatrix, load_capability_matrix
from providers.tier_tuning import parse_wire_tuning

_logger = logging.getLogger(__name__)

ACTION_GET: Final[str] = "get"
ACTION_PUT: Final[str] = "put"
ACTION_RESET: Final[str] = "reset"
ACTIONS: Final[tuple[str, ...]] = (ACTION_GET, ACTION_PUT, ACTION_RESET)

#: The request-argument schema. Closed, and with no ``rep_id``.
ARGUMENTS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "tuning": {"type": "object"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _require_action(arguments: dict[str, Any]) -> str:
    """Return the validated ``action``.

    Raises:
        ValidationFailed: An argument outside :data:`ARGUMENTS_SCHEMA`, an absent or unknown
            ``action``, or ``tuning`` present or absent against the action's rule.
    """
    unexpected = sorted(set(arguments) - set(ARGUMENTS_SCHEMA["properties"]))
    if unexpected:
        raise ValidationFailed(
            f"Unrecognised argument; recognised: {sorted(ARGUMENTS_SCHEMA['properties'])}.",
            source="arguments",
            detail=f"unexpected={[bounded_detail_value(key) for key in unexpected[:8]]}",
        )
    action = arguments.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValidationFailed(
            f"An action is required, one of {list(ACTIONS)}.",
            source="action",
            detail=f"received action={bounded_detail_value(action)}",
        )
    if action == ACTION_PUT and "tuning" not in arguments:
        raise ValidationFailed("A tuning block is required to save settings.", source="tuning")
    if action != ACTION_PUT and "tuning" in arguments:
        raise ValidationFailed(
            f"The {action} action takes no tuning block.",
            source="tuning",
        )
    return action


def _load_matrix() -> CapabilityMatrix:
    """Load the capability matrix, reporting any failure as ``internal_error`` after logging it."""
    try:
        return load_capability_matrix()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _logger.exception(json.dumps({"event": "capability_matrix_unavailable", "reason": reason}))
        raise CrmError("The model capability matrix could not be read.", detail=reason) from exc


def _user_settings(
    store: Store,
    identity: Identity,
    action: str,
    arguments: dict[str, Any],
    *,
    matrix: CapabilityMatrix,
) -> SettingsView:
    if action == ACTION_GET:
        return get_settings(store, identity, matrix=matrix)
    if action == ACTION_RESET:
        return reset_settings(store, identity, matrix=matrix)
    tuning = parse_wire_tuning(arguments["tuning"], default_source=SETTINGS_SOURCE)
    return save_settings(store, identity, tuning, matrix=matrix)


def handler(
    event: dict[str, Any],
    context: object = None,
    *,
    store: Store | None = None,
    matrix: CapabilityMatrix | None = None,
) -> dict[str, Any]:
    """AWS Lambda entry point for ``POST /api/user_settings``.

    A raised :class:`crm.errors.CrmError` is rendered as its own status code and user-safe message.

    Args:
        event: The raw Lambda event, from API Gateway's proxy envelope or a direct invoke.
        context: The Lambda context, unused.
        store: Injectable persistence layer; constructed from the ambient configuration when absent.
        matrix: Injectable capability matrix; loaded from ``config/`` when absent.
    """
    del context
    proxy = is_proxy_event(event)
    try:
        identity, arguments = decode(event)
        action = _require_action(arguments)
        resolved_store = store if store is not None else default_store()
        resolved_matrix = matrix if matrix is not None else _load_matrix()
        view = _user_settings(resolved_store, identity, action, arguments, matrix=resolved_matrix)
    except CrmError as exc:
        return error_response(exc, proxy=proxy)
    return ok(view.to_payload(), proxy=proxy)


__all__ = ["ACTIONS", "ACTION_GET", "ACTION_PUT", "ACTION_RESET", "ARGUMENTS_SCHEMA", "handler"]
