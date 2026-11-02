# SPDX-License-Identifier: MIT
"""The page-context contract across the browser -> relay -> Runtime boundary.

The payload key is read from each side's own source (the frontend hook,
``src/relay/src/runtime.ts``, :data:`agents.runtime_entrypoint.PAGE_CONTEXT_KEY`), the descriptor
is followed to the model's message list, and row-carrying descriptors are refused at the parse
site.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
from agents.answer_style import answer_style_system_message
from agents.runtime_entrypoint import (
    PAGE_CONTEXT_KEY,
    InMemorySessionStore,
    RuntimeInvocation,
    build_supervisor_turn,
    decode_invocation,
    decode_page_context,
)
from agents.supervisor import page_context_briefing
from agents.turn_driver import supervisor_system_message
from crm.envelope import CLAIM_BOOK, CLAIM_REP_ID, CLAIM_TENANT
from crm.errors import ValidationFailed
from crm.toolspec import page_context as pc_module
from crm.toolspec.page_context import PageContext, parse_page_context, render_json_schema
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import CapabilityMatrix, load_capability_matrix
from providers.prompt_caching import CACHE_SCOPE_KEY, VOLATILE
from providers.stub import StubProvider

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELAY_RUNTIME_TS = _REPO_ROOT / "src" / "relay" / "src" / "runtime.ts"
_FRONTEND_TURN_TS = (
    _REPO_ROOT / "frontend" / "src" / "components" / "AssistantPanel" / "useAssistantTurn.ts"
)

#: Matches the payload assignment in the relay's `buildRuntimePayload`, e.g.
#: `payload["page_context"] = request.pageContext;`.
_RELAY_PAYLOAD_ASSIGNMENT = re.compile(
    r"""payload\[["']([a-z_]+)["']\]\s*=\s*request\.pageContext"""
)


def _relay_page_context_key() -> str:
    """Return the payload key the relay actually writes the descriptor under."""
    source = _RELAY_RUNTIME_TS.read_text(encoding="utf-8")
    matches = _RELAY_PAYLOAD_ASSIGNMENT.findall(source)
    assert matches, (
        f"no `payload[...] = request.pageContext` assignment found in {_RELAY_RUNTIME_TS}; "
        "the relay stopped sending page context, or the assignment was reshaped"
    )
    assert len(matches) == 1, f"expected exactly one page-context assignment, found {matches}"
    return str(matches[0])


#: The browser's wire-key declaration, e.g. `export const PAGE_CONTEXT_WIRE_KEY = "page_context";`.
_FRONTEND_WIRE_KEY = re.compile(r"""PAGE_CONTEXT_WIRE_KEY\s*=\s*["']([a-z_]+)["']""")


def _frontend_page_context_key() -> str:
    """Return the wire key the browser hook sends the descriptor under."""
    source = _FRONTEND_TURN_TS.read_text(encoding="utf-8")
    matches = _FRONTEND_WIRE_KEY.findall(source)
    assert matches, (
        f"no PAGE_CONTEXT_WIRE_KEY declaration found in {_FRONTEND_TURN_TS}; the browser stopped "
        "naming the wire key, or the constant was renamed — either way this test verified nothing"
    )
    assert len(matches) == 1, f"expected exactly one wire-key declaration, found {matches}"
    return str(matches[0])


def _claims() -> dict[str, Any]:
    return {
        CLAIM_REP_ID: "rep-1",
        CLAIM_BOOK: "acc-1,acc-2",
        CLAIM_TENANT: "eval-context-unit",
        "custom:full_visibility": "false",
    }


def _payload(**extra: Any) -> dict[str, Any]:
    """Build an authenticated invocation payload with `extra` merged in.

    The identity block follows the splat so no key in `extra` can shadow it; the ordering is
    enforced by `test_identity_channel_shadowing.py`.
    """
    # A unique session id per call keeps each turn free of another test's session history.
    return {
        "prompt": "what changed on this one?",
        "session_id": f"session-context-{uuid.uuid4()}",
        **extra,
        "_agent_identity": {"claims": _claims()},
    }


def _matrix() -> CapabilityMatrix:
    return load_capability_matrix()


_ENTITY_DESCRIPTOR: dict[str, Any] = {
    "kind": "entity",
    "route": "/opportunities/opp-1",
    "entity_type": "opportunities",
    "entity_id": "opp-1",
}


_USE_PAGE_CONTEXT_TS = _REPO_ROOT / "frontend" / "src" / "hooks" / "usePageContext.ts"


def _spa_source() -> str:
    return _USE_PAGE_CONTEXT_TS.read_text(encoding="utf-8")


def _spa_opaque_reasons() -> list[str]:
    """Every reason in the SPA's `OPAQUE_REASONS`, with its `+`-joined literals concatenated."""
    block = re.search(r"OPAQUE_REASONS = \{(.*?)\} as const", _spa_source(), re.S)
    assert block is not None, "OPAQUE_REASONS not found in usePageContext.ts"
    reasons: list[str] = []
    for entry in re.split(r",\s*\n", block.group(1)):
        literals = re.findall(r'"((?:[^"\\]|\\.)*)"', entry)
        if literals:
            reasons.append("".join(literals))
    return reasons


def _spa_route_paths() -> list[str]:
    """Every path in the SPA's `ROUTES` table, with each `:param` filled by a store-issued id."""
    paths: list[str] = []
    for segments in re.findall(r"segments: \[([^\]]*)\]", _spa_source()):
        parts = re.findall(r'"([^"]*)"', segments)
        paths.append("/" + "/".join("acct-demo-0000" if p.startswith(":") else p for p in parts))
    return paths


def test_the_spa_source_yields_its_reasons_and_routes() -> None:
    """Positive control: the extraction below reads the real tables, not an empty match."""
    assert len(_spa_opaque_reasons()) >= 5
    assert "/pipeline" in _spa_route_paths()
    assert len(_spa_route_paths()) >= 15


def _spa_constant(name: str) -> str:
    """The right-hand side of `export const <name> = ...;` in usePageContext.ts."""
    match = re.search(rf"export const {name} = (.+);", _spa_source())
    assert match is not None, f"{name} not found in usePageContext.ts"
    return match.group(1)


def _spa_regex(name: str) -> re.Pattern[str]:
    """A `/^...$/` literal from usePageContext.ts as an equivalent Python pattern for fullmatch."""
    literal = _spa_constant(name)
    assert literal.startswith("/^") and literal.endswith("$/"), literal
    return re.compile(literal[2:-2].replace("\\/", "/"))


def test_the_spa_bounds_are_the_runtime_bounds() -> None:
    """The SPA's pre-send bounds equal the runtime's, so it never sends what the runtime refuses."""
    for name in (
        "ROUTE_MAX_LENGTH",
        "ENTITY_ID_MAX_LENGTH",
        "FILTER_MAX_COUNT",
        "FIELD_NAME_MAX_LENGTH",
        "FILTER_VALUE_MAX_LENGTH",
        "CURSOR_MAX_LENGTH",
    ):
        assert int(_spa_constant(name)) == getattr(pc_module, name), name


@pytest.mark.parametrize(
    ("spa_name", "runtime_pattern", "prefix"),
    [
        ("ROUTE_PATTERN", pc_module._ROUTE_PATTERN, "/"),
        ("ENTITY_ID_PATTERN", pc_module._ENTITY_ID_PATTERN, "a"),
        ("FIELD_NAME_PATTERN", pc_module._FIELD_NAME_PATTERN, "a"),
        ("CURSOR_PATTERN", pc_module._CURSOR_PATTERN, "A"),
    ],
)
def test_the_spa_charset_is_the_runtime_charset(
    spa_name: str, runtime_pattern: re.Pattern[str], prefix: str
) -> None:
    """Both patterns agree on every character of the first 0x300 code points, leading and inside."""
    spa_pattern = _spa_regex(spa_name)
    for code in range(0x300):
        for candidate in (chr(code), prefix + chr(code), prefix + chr(code) + "a"):
            assert (spa_pattern.fullmatch(candidate) is None) == (
                runtime_pattern.fullmatch(candidate) is None
            ), (spa_name, candidate)


def _spa_forbidden_ranges() -> list[tuple[int, int]]:
    """The SPA's `FORBIDDEN_VALUE_RANGES`, as inclusive code-point pairs."""
    block = re.search(r"FORBIDDEN_VALUE_RANGES = \[(.*?)\] as const", _spa_source(), re.S)
    assert block is not None, "FORBIDDEN_VALUE_RANGES not found in usePageContext.ts"
    pairs = re.findall(r"\[(0x[0-9a-fA-F]+), (0x[0-9a-fA-F]+)\]", block.group(1))
    assert pairs, "FORBIDDEN_VALUE_RANGES is empty"
    return [(int(low, 16), int(high, 16)) for low, high in pairs]


def test_the_spa_forbids_exactly_the_value_characters_the_runtime_forbids() -> None:
    """For every code point below 0x3000, both sides agree on whether a filter value may carry it."""
    ranges = _spa_forbidden_ranges()
    for code in range(0x3000):
        spa_forbids = any(low <= code <= high for low, high in ranges)
        runtime_forbids = pc_module._FORBIDDEN_VALUE_CHARACTERS.search(chr(code)) is not None
        assert spa_forbids == runtime_forbids, hex(code)


def test_every_reason_the_spa_emits_is_within_the_bound() -> None:
    """Each `OPAQUE_REASONS` entry parses as an opaque context's `reason`."""
    for reason in _spa_opaque_reasons():
        context = parse_page_context({"kind": "opaque", "route": "/pipeline", "reason": reason})
        assert context.kind == "opaque"


def test_every_route_the_spa_describes_passes_the_route_check() -> None:
    """Each `ROUTES` path, with a store-issued id in its parameter, parses as a route."""
    for path in _spa_route_paths():
        assert parse_page_context({"kind": "board", "route": path}).route == path


# --------------------------------------------------------------------------------------
# The key name, read from both sides
# --------------------------------------------------------------------------------------


def test_the_relay_and_the_runtime_name_the_same_payload_key() -> None:
    """Both sides read from their own sources, so a rename on either one turns this red."""
    assert _relay_page_context_key() == PAGE_CONTEXT_KEY


def test_all_three_sides_name_the_same_payload_key() -> None:
    """The browser, the relay and the Runtime all name the same payload key."""
    assert _frontend_page_context_key() == _relay_page_context_key() == PAGE_CONTEXT_KEY


def test_the_browser_and_the_runtime_name_the_same_payload_key() -> None:
    """The browser's wire key equals the key the Runtime reads."""
    assert _frontend_page_context_key() == PAGE_CONTEXT_KEY


def test_the_key_the_relay_writes_is_the_key_decode_invocation_reads() -> None:
    """End to end through the real decoder, keyed by the name taken out of the relay's source."""
    payload = _payload(**{_relay_page_context_key(): dict(_ENTITY_DESCRIPTOR)})

    invocation = decode_invocation(payload, _matrix())

    assert invocation.page_context is not None
    assert invocation.page_context.kind == "entity"


def test_a_descriptor_under_any_other_key_is_not_read() -> None:
    """A descriptor under any key other than `PAGE_CONTEXT_KEY` is not read."""
    payload = _payload(pageContext=dict(_ENTITY_DESCRIPTOR), page_ctx=dict(_ENTITY_DESCRIPTOR))

    invocation = decode_invocation(payload, _matrix())

    assert invocation.page_context is None


# --------------------------------------------------------------------------------------
# Reachability: the parsed descriptor arrives at the supervisor turn
# --------------------------------------------------------------------------------------


def test_the_parsed_descriptor_reaches_the_supervisor_turn() -> None:
    """`SupervisorTurn.page_context` is populated from the payload, not left at its default."""
    invocation = decode_invocation(
        _payload(**{PAGE_CONTEXT_KEY: dict(_ENTITY_DESCRIPTOR)}), _matrix()
    )

    turn = build_supervisor_turn(invocation, StubProvider(), _matrix())

    assert turn.page_context is invocation.page_context
    briefing = turn.page_context_briefing()
    assert briefing is not None
    assert "opp-1" in briefing


def test_the_briefing_is_reachable_and_names_the_record() -> None:
    """`page_context_briefing` is reached from a payload, not only from a hand-built descriptor."""
    context = decode_page_context(_payload(**{PAGE_CONTEXT_KEY: dict(_ENTITY_DESCRIPTOR)}))

    assert context is not None
    assert "opp-1" in page_context_briefing(context)


def test_a_turn_without_page_context_still_builds_and_briefs_nothing() -> None:
    """A payload with no descriptor builds a turn whose briefing is `None`."""
    invocation = decode_invocation(_payload(), _matrix())

    assert invocation.page_context is None
    turn = build_supervisor_turn(invocation, StubProvider(), _matrix())
    assert turn.page_context is None
    assert turn.page_context_briefing() is None


def test_an_explicit_null_descriptor_is_treated_as_absent() -> None:
    """A client with no current view may say so explicitly rather than omitting the key."""
    invocation = decode_invocation(_payload(**{PAGE_CONTEXT_KEY: None}), _matrix())
    assert invocation.page_context is None


def test_page_context_is_optional_on_the_dataclass_itself() -> None:
    """`RuntimeInvocation.page_context` defaults to `None`."""
    field = RuntimeInvocation.__dataclass_fields__[PAGE_CONTEXT_KEY]
    assert field.default is None


# --------------------------------------------------------------------------------------
# Malformed input fails at the boundary, before a turn exists
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("descriptor", "label"),
    [
        ({"kind": "not-a-kind"}, "unrecognised kind"),
        ({"route": "/opportunities/opp-1"}, "no kind"),
        ({"kind": "entity"}, "missing required fields"),
        (
            {"kind": "entity", "route": "/x", "entity_type": "nope", "entity_id": "x"},
            "unknown entity type",
        ),
        ({"kind": "opaque", "route": "/somewhere"}, "opaque with no reason"),
        ({"kind": "board", "route": "/pipeline?x=1"}, "route outside the charset"),
        ({"kind": "board", "route": "/" + "a" * 256}, "route over the bound"),
        ({"kind": "opaque", "route": "/pipeline", "reason": "r" * 301}, "reason over the bound"),
        ({**_ENTITY_DESCRIPTOR, "entity_id": "opp#1"}, "entity id outside the key shape"),
        ("a string", "not an object"),
        ([{"kind": "entity"}], "a list"),
        (42, "a number"),
    ],
)
def test_a_malformed_descriptor_is_rejected_at_the_boundary(descriptor: object, label: str) -> None:
    """A malformed descriptor raises `ValidationFailed` from `decode_invocation`."""
    with pytest.raises(ValidationFailed):
        decode_invocation(_payload(**{PAGE_CONTEXT_KEY: descriptor}), _matrix())


def test_a_malformed_descriptor_is_never_silently_dropped() -> None:
    """A descriptor missing required fields raises rather than decoding to `None`."""
    with pytest.raises(ValidationFailed):
        decode_invocation(_payload(**{PAGE_CONTEXT_KEY: {"kind": "entity"}}), _matrix())


def test_the_parse_site_uses_the_one_supported_parser() -> None:
    """`decode_page_context` delegates to `parse_page_context`, the sole path to a `PageContext`."""
    descriptor = dict(_ENTITY_DESCRIPTOR)

    assert decode_page_context({PAGE_CONTEXT_KEY: descriptor}) == parse_page_context(descriptor)


# --------------------------------------------------------------------------------------
# No rows cross this boundary
# --------------------------------------------------------------------------------------


def test_an_undeclared_rows_field_is_rejected_rather_than_ignored() -> None:
    """A descriptor carrying a `rows` field is rejected, even alongside valid fields."""
    with pytest.raises(ValidationFailed):
        decode_invocation(
            _payload(
                **{
                    PAGE_CONTEXT_KEY: {
                        **_ENTITY_DESCRIPTOR,
                        "rows": [{"id": "opp-1", "amount": 50000}],
                    }
                }
            ),
            _matrix(),
        )


@pytest.mark.parametrize(
    "row_field",
    ["rows", "items", "records", "results", "data"],
)
def test_no_row_shaped_field_name_is_accepted(row_field: str) -> None:
    """Every plausible name for a row collection is refused, not only `rows`."""
    with pytest.raises(ValidationFailed):
        decode_page_context(
            {PAGE_CONTEXT_KEY: {**_ENTITY_DESCRIPTOR, row_field: [{"id": "opp-1"}]}}
        )


def test_the_schema_the_boundary_accepts_declares_no_array_of_records() -> None:
    """The generated page-context schema declares no array and no row-collection field."""
    schema_text = json.dumps(render_json_schema())

    assert '"array"' not in schema_text
    for row_field in ("rows", "items", "records", "results"):
        assert f'"{row_field}"' not in schema_text


def test_a_briefing_never_emits_a_row_because_the_descriptor_cannot_carry_one() -> None:
    """The briefing describes the view and contains no record values."""
    context = decode_page_context({PAGE_CONTEXT_KEY: dict(_ENTITY_DESCRIPTOR)})
    assert context is not None

    briefing = page_context_briefing(context)

    assert isinstance(briefing, str)
    assert "50000" not in briefing
    assert isinstance(context, PageContext)


# --------------------------------------------------------------------------------------
# Reachable from *production*: the HTTP turn path, which does not use `SupervisorTurn`
# --------------------------------------------------------------------------------------


class _MessageRecordingProvider(ModelProvider):
    """Records the messages one turn sends, so what the model actually receives is assertable."""

    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.messages = list(messages)
        yield ProviderEvent(type="text_delta", text="ok")
        yield ProviderEvent(type="completed", input_tokens=1, output_tokens=1)


def _drive_turn(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> _MessageRecordingProvider:
    """POST one turn through the real `/invocations` route and return the provider it drove."""
    provider = _MessageRecordingProvider()
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: provider)
    monkeypatch.setattr(runtime_server, "_session_store", InMemorySessionStore())
    with TestClient(runtime_server.app).stream("POST", "/invocations", json=payload) as response:
        assert response.status_code == 200, response.read()
        "".join(response.iter_text())
    return provider


def test_the_http_turn_path_puts_the_briefing_in_front_of_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over `/invocations`, the briefing reaches the model after the system messages, before the prompt."""
    descriptor = dict(_ENTITY_DESCRIPTOR)
    provider = _drive_turn(monkeypatch, _payload(**{PAGE_CONTEXT_KEY: descriptor}))
    briefing = page_context_briefing(parse_page_context(descriptor))

    # Full-list equality: supervisor and answer-style system messages, briefing, prompt, nothing else.
    assert provider.messages == [
        supervisor_system_message(),
        answer_style_system_message(),
        {"role": "system", "content": briefing, CACHE_SCOPE_KEY: VOLATILE},
        {"role": "user", "content": "what changed on this one?"},
    ]


def test_the_http_turn_path_sends_only_the_prompt_when_there_is_no_page_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No view, no briefing — and no empty system message standing in for one."""
    provider = _drive_turn(monkeypatch, _payload())

    assert provider.messages == [
        supervisor_system_message(),
        answer_style_system_message(),
        {"role": "user", "content": "what changed on this one?"},
    ]


def test_the_briefing_the_http_path_sends_is_the_one_the_supervisor_would_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP path sends exactly one `page_context_briefing` rendering, at message index 2."""
    descriptor = dict(_ENTITY_DESCRIPTOR)
    provider = _drive_turn(monkeypatch, _payload(**{PAGE_CONTEXT_KEY: descriptor}))
    context = parse_page_context(descriptor)

    briefing = page_context_briefing(context)
    briefing_msgs = [m for m in provider.messages if m["content"] == briefing]
    assert len(briefing_msgs) == 1
    assert (
        provider.messages.index({"role": "system", "content": briefing, CACHE_SCOPE_KEY: VOLATILE})
        == 2
    )


def test_a_malformed_descriptor_is_a_400_before_the_stream_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed descriptor over HTTP is a `validation_failed` 400, not an event stream."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a malformed page context must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    response = TestClient(runtime_server.app).post(
        "/invocations", json=_payload(**{PAGE_CONTEXT_KEY: {"kind": "entity"}})
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"
    assert not response.headers["content-type"].startswith("text/event-stream")


def test_an_over_long_reason_is_a_400_that_never_reaches_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-bound `reason` over HTTP is a `validation_failed` 400 whose body carries none of it."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("an over-long page context must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)
    reason = "zqxmarker" * 40

    response = TestClient(runtime_server.app).post(
        "/invocations",
        json=_payload(
            **{PAGE_CONTEXT_KEY: {"kind": "opaque", "route": "/pipeline", "reason": reason}}
        ),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"
    assert "zqxmarker" not in response.text


def test_a_row_carrying_descriptor_is_refused_over_http_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row-carrying descriptor over HTTP is a 400 and never reaches a provider."""

    def _never(*_a: object) -> ModelProvider:
        raise AssertionError("a row-carrying page context must never reach a model provider")

    monkeypatch.setattr(runtime_server, "_model_provider", _never)

    response = TestClient(runtime_server.app).post(
        "/invocations",
        json=_payload(**{PAGE_CONTEXT_KEY: {**_ENTITY_DESCRIPTOR, "rows": [{"id": "opp-1"}]}}),
    )

    assert response.status_code == 400
