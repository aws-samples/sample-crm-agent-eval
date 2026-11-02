# SPDX-License-Identifier: MIT
"""Assert identity decoding has no permissive default on any of its three channels.

A missing or malformed identity on any channel raises rather than yielding an empty-scope identity.
"""

from __future__ import annotations

from typing import Any

import pytest

from crm.authz import DEMO_TENANT
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    IDENTITY_CHANNEL_KEYS,
    IDENTITY_KEY,
    decode_identity,
    identity_from_claims,
    split_book,
    strip_identity,
)
from crm.errors import AuthenticationFailed


def _apigw_event(**claims: Any) -> dict[str, Any]:
    """Build an API Gateway event carrying authorizer claims."""
    return {
        "requestContext": {
            "requestId": "req-1",
            "authorizer": {"jwt": {"claims": {CLAIM_REP_ID: "rep-1", **claims}}},
        }
    }


def _gateway_event(**overrides: Any) -> dict[str, Any]:
    """Build a Gateway Lambda-target event with an injected identity."""
    identity: dict[str, Any] = {
        "rep_id": "rep-1",
        "book": ["A-1", "A-2"],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
    }
    identity.update(overrides)
    return {IDENTITY_KEY: identity, "opportunity_id": "opp-1"}


# --------------------------------------------------------------------------------------
# No-channel and malformed-event handling
# --------------------------------------------------------------------------------------


def test_event_with_no_identity_channel_is_rejected() -> None:
    """A bare tool payload carries no identity and must not proceed."""
    with pytest.raises(AuthenticationFailed):
        decode_identity({"opportunity_id": "opp-1"})


def test_empty_event_is_rejected() -> None:
    """The degenerate case must not yield a default identity."""
    with pytest.raises(AuthenticationFailed):
        decode_identity({})


def test_non_dict_event_is_rejected_without_crashing() -> None:
    """A malformed invocation must surface as 401, not as a TypeError 500."""
    with pytest.raises(AuthenticationFailed):
        decode_identity([])  # type: ignore[arg-type]


def test_rejection_detail_does_not_leak_underscore_prefixed_keys() -> None:
    """Rejection detail does not echo underscore-prefixed payload keys."""
    with pytest.raises(AuthenticationFailed) as caught:
        decode_identity({"_secret_internal": "x", "opportunity_id": "opp-1"})
    assert "_secret_internal" not in (caught.value.detail or "")


# --------------------------------------------------------------------------------------
# Gateway channel
# --------------------------------------------------------------------------------------


def test_gateway_identity_is_decoded() -> None:
    """The happy path for an external MCP caller."""
    identity = decode_identity(_gateway_event())
    assert identity.rep_id == "rep-1"
    assert identity.book == frozenset({"A-1", "A-2"})
    assert identity.source == "gateway"
    assert identity.session_id == "sess-1"


@pytest.mark.parametrize("bad", [None, "", [], 42, "not-an-object"])
def test_malformed_gateway_identity_raises_rather_than_defaulting(bad: object) -> None:
    """A malformed `_identity` raises rather than defaulting."""
    with pytest.raises(AuthenticationFailed):
        decode_identity({IDENTITY_KEY: bad, "opportunity_id": "opp-1"})


def test_gateway_identity_without_a_rep_id_is_rejected() -> None:
    """Scope originates at the IdP; a repless identity cannot be scoped."""
    with pytest.raises(AuthenticationFailed):
        decode_identity(_gateway_event(rep_id=""))


def test_gateway_identity_must_declare_the_gateway_source() -> None:
    """A payload claiming another source did not come from the interceptor."""
    with pytest.raises(AuthenticationFailed) as caught:
        decode_identity(_gateway_event(source="apigw"))
    assert "source" in (caught.value.detail or "")


def test_gateway_identity_with_an_unknown_principal_type_is_rejected() -> None:
    """An unrecognised principal type must not silently become `user`."""
    with pytest.raises(AuthenticationFailed):
        decode_identity(_gateway_event(principal_type="superuser"))


def test_service_principal_claiming_full_visibility_is_rejected() -> None:
    """A service principal claiming full visibility is rejected."""
    with pytest.raises(AuthenticationFailed):
        decode_identity(_gateway_event(principal_type="service", full_visibility=True, book=[]))


def test_gateway_identity_full_visibility_requires_an_explicit_true() -> None:
    """A garbage value for a privilege flag must resolve False, never True."""
    assert decode_identity(_gateway_event(full_visibility="maybe")).full_visibility is False
    assert decode_identity(_gateway_event(full_visibility="true")).full_visibility is True


# --------------------------------------------------------------------------------------
# API Gateway channel
# --------------------------------------------------------------------------------------


def test_apigw_claims_are_decoded() -> None:
    """The happy path for the browser."""
    identity = decode_identity(_apigw_event(**{CLAIM_BOOK: "A-1,A-2"}))
    assert identity.source == "apigw"
    assert identity.book == frozenset({"A-1", "A-2"})
    assert identity.session_id == "req-1"


def test_apigw_rest_authorizer_claims_without_the_jwt_nesting_are_honoured() -> None:
    """REST ``COGNITO_USER_POOLS`` claims at ``authorizer.claims`` (no ``jwt`` nesting) decode."""
    identity = decode_identity(
        {
            "requestContext": {
                "requestId": "req-rest-1",
                "authorizer": {"claims": {CLAIM_REP_ID: "rep-1"}},
            }
        }
    )

    assert identity.rep_id == "rep-1"
    assert identity.source == "apigw"
    assert identity.session_id == "req-rest-1"


def test_apigw_event_without_an_authorizer_is_rejected() -> None:
    """An unauthenticated route should not exist; if one appears, this fails closed."""
    with pytest.raises(AuthenticationFailed):
        decode_identity({"requestContext": {"requestId": "req-1"}})


def test_apigw_event_without_claims_is_rejected() -> None:
    """An authorizer block with no claims cannot establish a rep."""
    with pytest.raises(AuthenticationFailed):
        decode_identity({"requestContext": {"authorizer": {"jwt": {}}}})


def test_apigw_event_missing_the_rep_claim_is_rejected() -> None:
    """An authorizer missing the rep claim is an authentication failure."""
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "abc"}}}}}
    with pytest.raises(AuthenticationFailed):
        decode_identity(event)


# --------------------------------------------------------------------------------------
# Agent channel
# --------------------------------------------------------------------------------------


def test_agent_forwarded_claims_are_decoded() -> None:
    """The agent channel decodes the invoking user's forwarded identity and scope."""
    event = {
        "_agent_identity": {
            "claims": {CLAIM_REP_ID: "rep-7", CLAIM_BOOK: "A-3"},
            "session_id": "chat-1",
        },
        "opportunity_id": "opp-1",
    }
    identity = decode_identity(event)
    assert identity.rep_id == "rep-7"
    assert identity.source == "agent"
    assert identity.book == frozenset({"A-3"})


def test_agent_identity_without_claims_is_rejected() -> None:
    """The agent cannot invent an identity of its own for authorization purposes."""
    with pytest.raises(AuthenticationFailed):
        decode_identity({"_agent_identity": {"session_id": "chat-1"}})


# --------------------------------------------------------------------------------------
# Channel precedence
# --------------------------------------------------------------------------------------


def test_gateway_channel_takes_precedence_over_api_gateway() -> None:
    """The gateway channel wins when both gateway and API Gateway keys are present."""
    event = {**_gateway_event(), **_apigw_event()}
    assert decode_identity(event).source == "gateway"


# --------------------------------------------------------------------------------------
# Book parsing and claim coercion
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset()),
        ("", frozenset()),
        ("A-1", frozenset({"A-1"})),
        ("A-1,A-2", frozenset({"A-1", "A-2"})),
        (" A-1 , A-2 ,, ", frozenset({"A-1", "A-2"})),
        (["A-1", "A-2"], frozenset({"A-1", "A-2"})),
        (("A-1",), frozenset({"A-1"})),
    ],
)
def test_split_book_parses_both_claim_forms(raw: object, expected: frozenset[str]) -> None:
    """Cognito emits comma-joined strings; programmatic callers pass lists."""
    assert split_book(raw) == expected


def test_split_book_rejects_an_unsupported_type() -> None:
    """A dict book claim is malformed and must fail rather than silently empty out."""
    with pytest.raises(AuthenticationFailed):
        split_book({"A-1": True})


def test_tenant_claim_is_honoured_and_defaults_to_demo() -> None:
    """A tenant claim is honoured, and its absence defaults to demo."""
    assert identity_from_claims({CLAIM_REP_ID: "r"}, source="apigw").tenant == DEMO_TENANT
    scoped = identity_from_claims({CLAIM_REP_ID: "r", CLAIM_TENANT: "eval-run-1"}, source="apigw")
    assert scoped.tenant == "eval-run-1"


def test_blank_tenant_claim_falls_back_to_demo_rather_than_empty() -> None:
    """A blank tenant claim falls back to demo rather than an empty tenant."""
    identity = identity_from_claims({CLAIM_REP_ID: "r", CLAIM_TENANT: "   "}, source="apigw")
    assert identity.tenant == DEMO_TENANT


def test_full_visibility_claim_coercion_is_conservative() -> None:
    """Only recognised affirmatives grant visibility across all accounts."""
    for value, expected in [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("perhaps", False),
        (None, False),
    ]:
        claims = {CLAIM_REP_ID: "r", CLAIM_FULL_VISIBILITY: value}
        assert identity_from_claims(claims, source="apigw").full_visibility is expected


# --------------------------------------------------------------------------------------
# strip_identity — the anti-spoofing primitive
# --------------------------------------------------------------------------------------


def test_strip_identity_removes_a_caller_supplied_value() -> None:
    """The interceptor calls this before injecting the verified identity."""
    arguments = {"opportunity_id": "opp-1", IDENTITY_KEY: {"rep_id": "attacker", "book": ["A-9"]}}
    cleaned = strip_identity(arguments)
    assert IDENTITY_KEY not in cleaned
    assert cleaned["opportunity_id"] == "opp-1"


def test_strip_identity_does_not_mutate_its_input() -> None:
    """Mutating the caller's dict in place would make the strip order-dependent."""
    arguments = {IDENTITY_KEY: {"rep_id": "attacker"}}
    strip_identity(arguments)
    assert IDENTITY_KEY in arguments


def test_strip_identity_is_a_no_op_when_absent() -> None:
    """The common case: a well-behaved caller supplied no identity."""
    assert strip_identity({"opportunity_id": "opp-1"}) == {"opportunity_id": "opp-1"}


def test_strip_then_decode_yields_the_injected_not_the_supplied_identity() -> None:
    """After stripping, decoding yields the injected identity, not the caller-supplied one."""
    supplied = {
        "opportunity_id": "opp-1",
        IDENTITY_KEY: {"rep_id": "attacker", "full_visibility": True, "source": "gateway"},
    }
    cleaned = strip_identity(supplied)
    cleaned[IDENTITY_KEY] = {
        "rep_id": "rep-1",
        "book": ["A-1"],
        "full_visibility": False,
        "principal_type": "user",
        "source": "gateway",
    }
    identity = decode_identity(cleaned)
    assert identity.rep_id == "rep-1"
    assert identity.full_visibility is False
    assert identity.book == frozenset({"A-1"})


def test_strip_identity_removes_every_key_decode_identity_would_honour() -> None:
    """`strip_identity` removes every key in `IDENTITY_CHANNEL_KEYS`."""
    for key in IDENTITY_CHANNEL_KEYS:
        stripped = strip_identity({key: {"anything": "goes"}, "opportunity_id": "opp-1"})
        assert key not in stripped, f"strip_identity left {key!r} intact"


def test_decode_identity_channel_keys_are_exactly_identity_channel_keys() -> None:
    """`IDENTITY_CHANNEL_KEYS` equals the literal keys `decode_identity` reads from `event`.

    The collector recognises `<key> in event` and `event.get(<key>, ...)` anywhere in the function
    body. Other ways of reading a key (arbitrary subscription, dynamic lookups, runtime-built keys)
    are not recognised.
    """
    import ast
    import inspect

    import crm.envelope as envelope_module

    source = inspect.getsource(decode_identity)
    tree = ast.parse(source)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)

    def _resolve_key(node: ast.expr) -> str | None:
        """Resolve a module-constant `Name` or a string literal to its key value."""
        if isinstance(node, ast.Name):
            resolved = getattr(envelope_module, node.id, None)
            return resolved if isinstance(resolved, str) else None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    # Collect every expression reading a caller-supplied key to select an identity channel,
    # anywhere in the function body, via two idioms:
    #   1. `<key> in event`                         (ast.Compare with ast.In)
    #   2. `event.get(<key>)` / `event.get(<key>, ...)`  (ast.Call on the `.get` attribute)
    # A loop variable such as `k` in `k in event` resolves to None and is excluded.
    branch_keys: set[str] = set()
    for node in ast.walk(func_def):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == "event"
        ):
            key = _resolve_key(node.left)
            if key is not None:
                branch_keys.add(key)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "event"
            and node.args
        ):
            key = _resolve_key(node.args[0])
            if key is not None:
                branch_keys.add(key)

    assert branch_keys == IDENTITY_CHANNEL_KEYS, (
        f"decode_identity reads {sorted(branch_keys)} from event but "
        f"IDENTITY_CHANNEL_KEYS is {sorted(IDENTITY_CHANNEL_KEYS)} — strip_identity would not "
        "remove a channel key decode_identity honours"
    )


def test_ast_channel_collector_catches_a_hypothetical_event_dot_get_idiom_channel() -> None:
    """The channel collector catches a channel read as ``event.get(key)``."""
    import ast

    hypothetical_source = (
        "def _decode(event):\n"
        "    if event.get('_v2_identity') is not None:\n"
        "        return event.get('_v2_identity')\n"
        "    raise ValueError('no channel')\n"
    )
    tree = ast.parse(hypothetical_source)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)

    collected: set[str] = set()
    for node in ast.walk(func_def):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "event"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            collected.add(node.args[0].value)

    assert collected == {"_v2_identity"}, (
        "the event.get(...) idiom collector must find a hypothetical fourth channel expressed "
        "this way, matching cycle-3 mutation 4's previously-uncaught scenario"
    )


def test_strip_identity_removes_the_second_caller_controllable_channel() -> None:
    """A hostile `_agent_identity` does not survive the strip on the external MCP path."""
    hostile = {
        "opportunity_id": "opp-1",
        AGENT_IDENTITY_KEY: {
            "claims": {
                CLAIM_REP_ID: "attacker",
                CLAIM_FULL_VISIBILITY: "true",
                CLAIM_BOOK: "",
            }
        },
    }
    cleaned = strip_identity(hostile)
    assert AGENT_IDENTITY_KEY not in cleaned
    with pytest.raises(AuthenticationFailed):
        decode_identity(cleaned)


def test_strip_identity_removes_the_request_context_channel() -> None:
    """A forged `requestContext` does not survive the strip on the MCP path."""
    hostile = {
        "opportunity_id": "opp-1",
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        CLAIM_REP_ID: "attacker",
                        CLAIM_FULL_VISIBILITY: "true",
                        CLAIM_TENANT: "demo",
                        CLAIM_BOOK: "",
                    }
                }
            }
        },
    }
    cleaned = strip_identity(hostile)
    assert "requestContext" not in cleaned
    with pytest.raises(AuthenticationFailed):
        decode_identity(cleaned)


def test_module_all_excludes_the_bypass_constructor() -> None:
    """`identity_from_claims` is not in `crm.envelope.__all__`."""
    import crm.envelope as envelope_module

    assert "identity_from_claims" not in envelope_module.__all__
    assert "decode_identity" in envelope_module.__all__


def test_no_module_outside_crm_envelope_imports_the_bypass_constructor() -> None:
    """No module under `src/` other than `crm.envelope` imports `identity_from_claims` by name."""
    import ast
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if path.name == "envelope.py" and path.parent.name == "crm":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom | ast.Import) and any(
                alias.name == "identity_from_claims" for alias in node.names
            ):
                offenders.append(str(path.relative_to(src_root)))

    assert not offenders, f"identity_from_claims imported outside crm.envelope by: {offenders}"


def test_channel_key_collector_catches_the_event_get_idiom_not_only_in_event() -> None:
    """The collector reads an `event.get("_v2_identity")` key from a synthetic function body."""
    import ast
    import textwrap

    synthetic_source = textwrap.dedent(
        """
        def _synthetic(event):
            if "_identity" in event:
                return "gateway"
            if event.get("_v2_identity") is not None:
                return "v2"
            return "none"
        """
    )
    tree = ast.parse(synthetic_source)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)

    def _resolve_key(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    found_keys: set[str] = set()
    for node in ast.walk(func_def):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == "event"
        ):
            key = _resolve_key(node.left)
            if key is not None:
                found_keys.add(key)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "event"
            and node.args
        ):
            key = _resolve_key(node.args[0])
            if key is not None:
                found_keys.add(key)

    assert found_keys == {"_identity", "_v2_identity"}, (
        f"collector found {sorted(found_keys)}, expected both the `in event` and `event.get(...)` "
        "channels to be detected"
    )
