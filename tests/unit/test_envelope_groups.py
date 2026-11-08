# SPDX-License-Identifier: MIT
"""Assert `cognito:groups` is decoded on the API channel only and agent `principal_type` narrows only.

The REST v1 cases start from the recorded authorizer event in
``tests/fixtures/events/apigw_v1_proxy_post_get_account.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_FULL_VISIBILITY,
    CLAIM_GROUPS,
    CLAIM_REP_ID,
    IDENTITY_KEY,
    decode_identity,
    split_groups,
)
from crm.errors import AuthenticationFailed
from tests.conftest import TEST_API_CLIENT_ID

_REST_V1_EVENT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "events"
    / "apigw_v1_proxy_post_get_account.json"
)


def _rest_v1_event(groups: object = None, *, include_groups: bool = True) -> dict[str, Any]:
    """Return the recorded REST v1 event, with `cognito:groups` set to ``groups`` when included."""
    event: dict[str, Any] = json.loads(_REST_V1_EVENT.read_text(encoding="utf-8"))
    claims = event["requestContext"]["authorizer"]["claims"]
    if include_groups:
        claims[CLAIM_GROUPS] = groups
    return event


def _http_v2_event(groups: object) -> dict[str, Any]:
    """Build an HTTP API payload-v2 event carrying `cognito:groups` as given."""
    return {
        "requestContext": {
            "requestId": "req-1",
            "authorizer": {
                "jwt": {
                    "claims": {
                        CLAIM_REP_ID: "rep-1",
                        CLAIM_GROUPS: groups,
                        "token_use": "id",
                        "aud": TEST_API_CLIENT_ID,
                    }
                }
            },
        }
    }


def _agent_event(**block: Any) -> dict[str, Any]:
    """Build an agent-channel event whose claims include an admin `cognito:groups`."""
    identity_block: dict[str, Any] = {
        "claims": {CLAIM_REP_ID: "rep-1", CLAIM_GROUPS: ["admin"]},
        "session_id": "sess-1",
    }
    identity_block.update(block)
    return {AGENT_IDENTITY_KEY: identity_block}


# --------------------------------------------------------------------------------------
# split_groups renderings
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset()),
        ([], frozenset()),
        (["admin"], frozenset({"admin"})),
        (["admin", "reps"], frozenset({"admin", "reps"})),
        ('["admin","reps"]', frozenset({"admin", "reps"})),
        ('["admin"]', frozenset({"admin"})),
        ("[]", frozenset()),
        ("[admin, reps]", frozenset({"admin", "reps"})),
        ("[admin]", frozenset({"admin"})),
        ("admin,reps", frozenset({"admin", "reps"})),
        (" admin , reps ", frozenset({"admin", "reps"})),
        ("admin", frozenset({"admin"})),
        ("", frozenset()),
        ("a,,b, ", frozenset({"a", "b"})),
    ],
)
def test_split_groups_accepts_every_documented_rendering(
    raw: object, expected: frozenset[str]
) -> None:
    """List, JSON-array string, bracketed string, comma string and absent all decode."""
    assert split_groups(raw) == expected


@pytest.mark.parametrize(
    "raw", [42, True, {"admin": True}, ["admin", 1], '["admin", 1]', '[{"admin": 1}]']
)
def test_split_groups_rejects_unsupported_shapes(raw: object) -> None:
    """A non-string, non-list claim or a non-string member fails authentication."""
    with pytest.raises(AuthenticationFailed):
        split_groups(raw)


@pytest.mark.parametrize("raw", ['{"admin": 1}', '"admin"', "[admin", "admin]", "[[admin]]"])
def test_split_groups_never_reads_admin_from_an_unrecognised_string(raw: str) -> None:
    """A string in no documented rendering cannot yield the exact name `admin`."""
    assert "admin" not in split_groups(raw)


def test_split_groups_does_not_case_fold() -> None:
    """`Admin` is a different group from `admin`."""
    assert split_groups("Admin") == frozenset({"Admin"})


# --------------------------------------------------------------------------------------
# apigw channel — recorded REST v1 fixture and HTTP v2
# --------------------------------------------------------------------------------------


def test_recorded_rest_v1_event_without_groups_decodes_no_admin() -> None:
    """The recorded authorizer event carries no `cognito:groups`; the caller is not an admin."""
    identity = decode_identity(_rest_v1_event(include_groups=False))
    assert identity.source == "apigw"
    assert identity.groups == frozenset()
    assert identity.is_admin is False


@pytest.mark.parametrize(
    "rendering", ["[admin]", '["admin"]', "admin", "[reps, admin]", "reps,admin"]
)
def test_rest_v1_admin_group_in_every_string_rendering_decodes_admin(rendering: str) -> None:
    """REST v1 flattens claim values to strings; each rendering yields an admin."""
    identity = decode_identity(_rest_v1_event(rendering))
    assert "admin" in identity.groups
    assert identity.is_admin is True


def test_rest_v1_non_admin_groups_decode_without_admin() -> None:
    """A member of other groups only is not an admin."""
    identity = decode_identity(_rest_v1_event("[reps, managers]"))
    assert identity.groups == frozenset({"reps", "managers"})
    assert identity.is_admin is False


def test_http_v2_list_groups_decode_admin() -> None:
    """The payload-v2 JWT authorizer may deliver a real list."""
    identity = decode_identity(_http_v2_event(["admin"]))
    assert identity.is_admin is True


def test_apigw_malformed_groups_claim_fails_authentication() -> None:
    """An unsupported groups type is rejected rather than read as no groups."""
    with pytest.raises(AuthenticationFailed):
        decode_identity(_http_v2_event({"admin": True}))


# --------------------------------------------------------------------------------------
# gateway and agent channels never decode groups
# --------------------------------------------------------------------------------------


def test_gateway_channel_ignores_a_groups_field() -> None:
    """A `groups`/`cognito:groups` value inside `_identity` is never honoured."""
    event = {
        IDENTITY_KEY: {
            "rep_id": "rep-1",
            "source": "gateway",
            "groups": ["admin"],
            CLAIM_GROUPS: ["admin"],
        }
    }
    identity = decode_identity(event)
    assert identity.groups == frozenset()
    assert identity.is_admin is False


def test_agent_channel_ignores_forwarded_groups_claim() -> None:
    """Forwarded user claims carrying `cognito:groups` do not make an agent caller admin."""
    identity = decode_identity(_agent_event())
    assert identity.source == "agent"
    assert identity.groups == frozenset()
    assert identity.is_admin is False


# --------------------------------------------------------------------------------------
# agent principal_type
# --------------------------------------------------------------------------------------


def test_agent_principal_type_defaults_to_user_when_absent() -> None:
    """Today's payloads carry no `principal_type` and still decode as a user."""
    assert decode_identity(_agent_event()).principal_type == "user"


@pytest.mark.parametrize("value", ["user", "service"])
def test_agent_principal_type_accepts_user_and_service(value: str) -> None:
    """Both documented values decode to that principal type."""
    assert decode_identity(_agent_event(principal_type=value)).principal_type == value


@pytest.mark.parametrize("value", ["admin", "USER", "Service", "", None, 1, ["service"]])
def test_agent_principal_type_rejects_any_other_value(value: object) -> None:
    """Any value other than `user`/`service` fails authentication."""
    with pytest.raises(AuthenticationFailed, match="Authentication is required"):
        decode_identity(_agent_event(principal_type=value))


def test_agent_service_principal_with_full_visibility_claim_is_rejected() -> None:
    """`service` may only narrow: a full-visibility claim with it fails authentication."""
    event = _agent_event(principal_type="service")
    event[AGENT_IDENTITY_KEY]["claims"][CLAIM_FULL_VISIBILITY] = "true"
    with pytest.raises(AuthenticationFailed):
        decode_identity(event)


def test_agent_principal_type_inside_claims_is_not_honoured() -> None:
    """`principal_type` is read from the block's top level, never from the forwarded claims."""
    event = _agent_event()
    event[AGENT_IDENTITY_KEY]["claims"]["principal_type"] = "service"
    assert decode_identity(event).principal_type == "user"
