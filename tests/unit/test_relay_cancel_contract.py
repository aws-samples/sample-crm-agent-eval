# SPDX-License-Identifier: MIT
"""The cancellation wire contract, read from all three sides.

The browser hook composes the cancel body, the relay puts its `mode` field in the
`InvokeAgentRuntime` payload, and `runtime_server.py` dispatches on it. Each side's spelling is
asserted against the Runtime's Python constants.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agents import runtime_server

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELAY_RUNTIME_TS = _REPO_ROOT / "src" / "relay" / "src" / "runtime.ts"
_RELAY_REQUESTS_TS = _REPO_ROOT / "src" / "relay" / "src" / "requests.ts"
_FRONTEND_CANCEL_TS = (
    _REPO_ROOT / "frontend" / "src" / "components" / "AssistantPanel" / "useCancelTurn.ts"
)

#: `export const MODE_KEY = "…"` / `export const CANCEL_MODE = "…"` in the relay.
_RELAY_CONST = r'^export const {name} = "(?P<value>[^"]+)";\s*$'


def _relay_const(name: str, source: Path = _RELAY_RUNTIME_TS) -> str:
    """Return the string a relay `export const` is declared with, failing loudly on absence."""
    matches = re.findall(
        _RELAY_CONST.format(name=name), source.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not matches:
        pytest.fail(f"{source.name} declares no {name}; this contract test verified nothing")
    assert len(matches) == 1, f"{name} declared {len(matches)} times in {source.name}"
    return str(matches[0])


def test_the_relay_and_the_runtime_agree_on_the_mode_field_name() -> None:
    """A rename on either side fails here rather than routing every cancel to the turn path."""
    assert _relay_const("MODE_KEY") == runtime_server._MODE_KEY


def test_the_relay_and_the_runtime_agree_on_the_cancel_mode_value() -> None:
    assert _relay_const("CANCEL_MODE") == runtime_server._CANCEL_MODE


def test_the_relay_and_the_runtime_agree_on_the_session_id_field() -> None:
    """The relay's cancel payload carries the session id under the Runtime's field name."""
    payload_builder = _RELAY_RUNTIME_TS.read_text(encoding="utf-8")
    body = payload_builder[payload_builder.index("export function buildCancelPayload") :]
    body = body[: body.index("\n}\n")]
    assert f"{runtime_server._SESSION_ID_KEY}: request.sessionId" in body


def test_the_relay_sends_the_identity_channel_the_runtime_honours() -> None:
    """The relay's cancel payload carries identity under the agent identity channel key."""
    from crm.envelope import AGENT_IDENTITY_KEY

    source = _RELAY_RUNTIME_TS.read_text(encoding="utf-8")
    body = source[source.index("export function buildCancelPayload") :]
    body = body[: body.index("\n}\n")]
    assert f"{AGENT_IDENTITY_KEY}:" in body


def test_the_cancel_payload_carries_no_prompt() -> None:
    """The relay's cancel payload has no `prompt` field."""
    source = _RELAY_RUNTIME_TS.read_text(encoding="utf-8")
    body = source[source.index("export function buildCancelPayload") :]
    body = body[: body.index("\n}\n")]
    assert "prompt" not in body


def test_the_cancel_command_reuses_the_turns_session_id_as_the_runtime_session_id() -> None:
    """The relay passes the turn's own session id as `runtimeSessionId` on a cancel."""
    source = _RELAY_RUNTIME_TS.read_text(encoding="utf-8")
    body = source[source.index("export function buildCancelCommand") :]
    body = body[: body.index("\n}\n")]
    assert "runtimeSessionId: request.sessionId" in body


def test_the_relay_decoder_reads_the_same_mode_constants_it_sends() -> None:
    """The relay's request decoder imports and compares against `runtime.ts`'s mode constants."""
    requests_source = _RELAY_REQUESTS_TS.read_text(encoding="utf-8")
    assert 'import { CANCEL_MODE, MODE_KEY } from "./runtime.js";' in requests_source
    assert "body[MODE_KEY] !== CANCEL_MODE" in requests_source


def test_the_runtime_dispatches_the_cancel_mode_before_decoding_a_turn() -> None:
    """In `runtime_server.py`, cancel dispatch precedes turn decoding and provider selection."""
    source = (_REPO_ROOT / "src" / "agents" / "runtime_server.py").read_text(encoding="utf-8")
    cancel_dispatch = source.index("_cancel_response(payload)")
    decode = source.index("invocation = decode_invocation(payload, matrix)")
    provider = source.index("providers = _turn_provider_resolver()")
    assert cancel_dispatch < decode < provider


@pytest.mark.parametrize(
    ("name", "source"),
    [("MODE_KEY", _RELAY_RUNTIME_TS), ("CANCEL_MODE", _RELAY_RUNTIME_TS)],
)
def test_each_relay_constant_is_parsed_from_its_real_source(name: str, source: Path) -> None:
    """Each relay constant is parsed from the source file it is declared in."""
    assert _relay_const(name, source)


# ------------------------------------------------------------------------------------------------
# The third side: the browser hook that composes the body
# ------------------------------------------------------------------------------------------------


def _frontend_const(name: str) -> str:
    """Return the string the frontend hook declares for `name`."""
    matches = re.findall(
        _RELAY_CONST.format(name=name),
        _FRONTEND_CANCEL_TS.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not matches:
        pytest.fail(f"useCancelTurn.ts declares no {name}; this contract test verified nothing")
    assert len(matches) == 1, f"{name} declared {len(matches)} times in useCancelTurn.ts"
    return str(matches[0])


def test_the_browser_hook_sends_the_mode_the_runtime_dispatches_on() -> None:
    """The browser hook's cancel mode equals the Runtime's cancel mode."""
    assert _frontend_const("CANCEL_MODE") == runtime_server._CANCEL_MODE
    assert _frontend_const("CANCEL_MODE_KEY") == runtime_server._MODE_KEY


def test_all_three_sides_agree_on_the_mode_value() -> None:
    """The hook, the relay, and the Runtime declare one identical cancel mode value."""
    assert (
        _frontend_const("CANCEL_MODE")
        == _relay_const("CANCEL_MODE")
        == runtime_server._CANCEL_MODE
        == "cancel"
    )


def test_the_browser_hook_sends_the_session_id_field_the_runtime_authorizes_against() -> None:
    source = _FRONTEND_CANCEL_TS.read_text(encoding="utf-8")
    assert f"{runtime_server._SESSION_ID_KEY}: sessionId" in source


def test_the_browser_hook_accepts_exactly_the_runtimes_dispositions() -> None:
    """The browser hook's dispositions equal the members of `CancelDisposition`."""
    from typing import get_args

    from agents.cancellation import CancelDisposition

    # Read from the type alias rather than a text scan of `runtime_server.py`.
    runtime_dispositions = {str(value) for value in get_args(CancelDisposition)}
    assert runtime_dispositions, "CancelDisposition has no members, so this verified nothing"
    hook_source = _FRONTEND_CANCEL_TS.read_text(encoding="utf-8")

    declared = re.search(r"DISPOSITIONS: readonly string\[\] = \[(?P<body>[^\]]*)\]", hook_source)
    assert declared is not None, "useCancelTurn.ts declares no DISPOSITIONS list"
    hook_dispositions = {
        part.strip().strip('"').strip("'")
        for part in declared.group("body").split(",")
        if part.strip()
    }
    assert hook_dispositions, "the DISPOSITIONS parse found nothing, so this verified nothing"

    # Both directions: no disposition is known to only one side.
    assert hook_dispositions == runtime_dispositions
