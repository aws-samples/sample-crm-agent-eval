# SPDX-License-Identifier: MIT
"""Identity-bound tool dispatch to the deployed action Lambdas — the one leaf both paths share.

Holds the function-name composition, the ``agent``-channel event assembly, and the
``lambda:Invoke`` itself. The runtime path builds an identity-bound
:data:`~agents.tool_actor.ToolDispatch` here and injects it into ``turn_driver.drive_turn``; the
driver holds no identity of its own.

The event is built by :func:`crm.envelope.identity_event`, which strips every identity channel from
the untrusted ``tool_input`` before writing the trusted ``agent``-channel block. The Lambda client
belongs to ``crm.dispatch.invoke_action``.
"""

from __future__ import annotations

import os
from typing import Any, Final

from agents.registry import RegisteredTool
from agents.tool_actor import ToolDispatch
from crm.dispatch import invoke_action
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    Identity,
    identity_event,
)

#: Environment variable naming the deployed action-Lambda prefix.
_ACTION_LAMBDA_PREFIX_ENV_VAR: Final[str] = "ACTION_LAMBDA_PREFIX"


def action_function_name(tool_name: str) -> str:
    """Return the deployed action Lambda's function name for ``tool_name``.

    Composed from ``ACTION_LAMBDA_PREFIX``; also used by ``eval.harness``'s live-AWS substrate.

    Args:
        tool_name: The tool whose deployed function name is wanted.

    Returns:
        ``"<prefix>-<tool_name>"``, or the bare ``tool_name`` when no prefix is configured.
    """
    prefix = os.environ.get(_ACTION_LAMBDA_PREFIX_ENV_VAR, "").strip()
    return f"{prefix}-{tool_name}" if prefix else tool_name


def agent_identity_event(identity: Identity, tool_input: dict[str, object]) -> dict[str, Any]:
    """Build the action-Lambda event an in-app agent sends, on the ``agent`` channel.

    :data:`crm.envelope.AGENT_IDENTITY_KEY` carries the invoking user's claims, with the tool's
    arguments as sibling keys. ``tool_input`` is untrusted and is stripped of every identity channel
    by :func:`crm.envelope.identity_event` before the trusted block is written.

    Args:
        identity: The invoking user's decoded identity.
        tool_input: The tool's own arguments, from a caller or a model.

    Returns:
        The raw event :func:`crm.envelope.decode_identity` reads on the ``agent`` channel.
    """
    return identity_event(
        AGENT_IDENTITY_KEY,
        {
            "claims": {
                CLAIM_REP_ID: identity.rep_id,
                CLAIM_BOOK: sorted(identity.book),
                CLAIM_FULL_VISIBILITY: identity.full_visibility,
                CLAIM_TENANT: identity.tenant,
            },
            "session_id": identity.session_id,
        },
        tool_input,
    )


def dispatch_tool_call(
    tool: RegisteredTool,
    tool_input: dict[str, object],
    *,
    identity: Identity,
) -> dict[str, Any]:
    """Invoke ``tool``'s deployed action Lambda and return its wire response.

    Identity travels on the ``agent`` channel — see :func:`agent_identity_event`.

    Returns:
        The handler's own wire response. An unhandled exception inside the function, or a response
        body that is not a JSON object, is returned by :func:`crm.dispatch.invoke_action` as a
        taxonomy-shaped payload rather than raised.
    """
    return invoke_action(
        action_function_name(tool.name), agent_identity_event(identity, tool_input)
    )


def production_dispatch(identity: Identity) -> ToolDispatch:
    """Return an identity-bound :data:`~agents.tool_actor.ToolDispatch` for the production path.

    The returned callable closes over the authenticated ``identity`` and dispatches each tool call
    to its deployed action Lambda on the ``agent`` channel. Injected into
    ``turn_driver.drive_turn``.

    Args:
        identity: The authenticated caller's decoded identity, bound into every dispatch this turn.
    """

    def dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        return dispatch_tool_call(tool, tool_input, identity=identity)

    return dispatch


__all__ = [
    "action_function_name",
    "agent_identity_event",
    "dispatch_tool_call",
    "production_dispatch",
]
