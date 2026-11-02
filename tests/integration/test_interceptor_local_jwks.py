# SPDX-License-Identifier: MIT
"""Integration tests: the Gateway REQUEST interceptor against a signed JWT and a local JWKS server.

`GatewayInterceptorConfig.signing_key_for` is never mocked; verification runs through `PyJWT` and
`PyJWKClient` against a loopback `http.server` on `127.0.0.1`. No credential is used.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from crm.envelope import IDENTITY_CHANNEL_KEYS, IDENTITY_KEY, decode_identity
from gateway.identity_interceptor import GatewayInterceptorConfig, lambda_handler

from .jwks_fixture import JwksServer, SigningKey, local_jwks

_ALLOWED_CLIENT_ID = "spa-client-abc123"
_ALLOWED_SCOPE = "crm/invoke"
_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_realpool"


def _valid_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "cognito-sub-1",
        "iss": _ISSUER,
        "client_id": _ALLOWED_CLIENT_ID,
        "aud": _ALLOWED_CLIENT_ID,
        "token_use": "access",  # nosec B105  # token type, not a credential
        "scope": _ALLOWED_SCOPE,
        "iat": now,
        "exp": now + 3600,
        "custom:rep_id": "rep-1",
        "custom:book": "A-1,A-2",
        "custom:full_visibility": "false",
    }
    claims.update(overrides)
    return claims


def _config(jwks_url: str, *, cache_seconds: int = 3600) -> GatewayInterceptorConfig:
    return GatewayInterceptorConfig(
        discovery_url=f"{_ISSUER}/.well-known/openid-configuration",
        jwks_url=jwks_url,
        allowed_client_ids=frozenset({_ALLOWED_CLIENT_ID}),
        allowed_scopes=frozenset({_ALLOWED_SCOPE}),
        jwks_cache_seconds=cache_seconds,
    )


def _gateway_request(
    *,
    method: str = "tools/call",
    arguments: dict[str, Any] | None = None,
    token: str | None,
    session_id: str = "sess-abc",
    tool_id: str | None = "req-1",
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    body: dict[str, Any] = {"jsonrpc": "2.0", "id": tool_id, "method": method}
    if method != "tools/list":
        body["params"] = {"name": "get_opportunity", "arguments": arguments or {}}

    return {"mcp": {"gatewayRequest": {"headers": headers, "body": body}}}


def _invoke(
    event: dict[str, Any], config: GatewayInterceptorConfig, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Invoke the handler with only `_load_config` patched; `signing_key_for` stays real."""
    import gateway.identity_interceptor as module

    monkeypatch.setattr(module, "_load_config", lambda: config)
    return lambda_handler(event, object())


def _denial_message(result: dict[str, Any]) -> str:
    return str(result["mcp"]["transformedGatewayResponse"]["body"]["error"]["message"])


def _is_denied(result: dict[str, Any]) -> bool:
    return "transformedGatewayResponse" in result["mcp"]


# --------------------------------------------------------------------------------------
# Fixture: one real key, one real JWKS server
# --------------------------------------------------------------------------------------


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey(kid="real-kid-1")


@pytest.fixture
def jwks(signing_key: SigningKey):  # type: ignore[no-untyped-def]
    with local_jwks([signing_key]) as server:
        yield server


# --------------------------------------------------------------------------------------
# 1. Signature verification is real, not stubbed
# --------------------------------------------------------------------------------------


def test_a_correctly_signed_token_is_accepted_through_the_real_jwks_endpoint(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token signed by the key genuinely published at the JWKS endpoint verifies successfully."""
    token = signing_key.sign(_valid_claims())
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert not _is_denied(result)
    injected = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"][
        IDENTITY_KEY
    ]
    assert injected["rep_id"] == "rep-1"
    assert jwks.request_count >= 1, "the JWKS endpoint was never actually queried over HTTP"


def test_a_token_whose_signature_does_not_match_its_claimed_kid_is_rejected(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token signed by a different key than the one published under its `kid` is denied."""
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged_token = pyjwt.encode(
        _valid_claims(),
        attacker_key,
        algorithm="RS256",
        headers={"kid": signing_key.kid},  # claims the REAL, published kid
    )
    event = _gateway_request(token=forged_token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert _is_denied(result)
    assert _denial_message(result) == "Authentication failed. Sign in again to continue."


# --------------------------------------------------------------------------------------
# 2. kid selection is real
# --------------------------------------------------------------------------------------


def test_kid_selection_picks_the_correct_key_among_several_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With multiple keys published, the token verifies against the one matching its own `kid`."""
    key_a = SigningKey(kid="kid-a")
    key_b = SigningKey(kid="kid-b")
    with local_jwks([key_a, key_b]) as server:
        token = key_b.sign(_valid_claims())
        event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

        result = _invoke(event, _config(server.jwks_url), monkeypatch)

    assert not _is_denied(result)


def test_a_token_signed_by_an_entirely_unknown_key_is_refused(
    jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `kid` absent from the served JWKS yields the fixed denial, not a crash or a leak."""
    unknown_key = SigningKey(kid="never-published")
    token = unknown_key.sign(_valid_claims())
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert _is_denied(result)
    message = _denial_message(result)
    assert message == "Authentication failed. Sign in again to continue."
    assert "never-published" not in message


def test_a_token_missing_a_kid_header_entirely_is_refused(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token with no `kid` in its header cannot be matched to any published key."""
    token = pyjwt.encode(_valid_claims(), signing_key.private_key, algorithm="RS256")  # no kid
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert _is_denied(result)


# --------------------------------------------------------------------------------------
# 3. JWKS caching and refresh are real
# --------------------------------------------------------------------------------------


def test_a_second_verification_reuses_the_cached_jwks_rather_than_refetching(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within the cache lifetime, a second call for a `kid` already resolved does not refetch."""
    config = _config(jwks.jwks_url, cache_seconds=3600)
    token = signing_key.sign(_valid_claims())
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    _invoke(event, config, monkeypatch)
    first_count = jwks.request_count
    assert first_count >= 1

    _invoke(event, config, monkeypatch)
    second_count = jwks.request_count

    assert second_count == first_count, (
        "a second verification within the cache lifetime re-fetched the JWKS document; "
        f"requests went from {first_count} to {second_count}"
    )


class _FakeMonotonic:
    """A settable stand-in for `time.monotonic`, installed only inside `jwt.jwks_client`."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


def _install_refresh_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeMonotonic:
    clock = _FakeMonotonic()
    monkeypatch.setattr(pyjwt.jwks_client, "time", clock)
    return clock


def _prime_then_rotate(
    server: JwksServer, config: GatewayInterceptorConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], int]:
    """Resolve the original key, then publish a second key; return its request and the fetch count."""
    original_key = SigningKey(kid="kid-original")
    server.replace_keys([original_key])
    priming_event = _gateway_request(
        token=original_key.sign(_valid_claims()), arguments={"opportunity_id": "O-1"}
    )
    assert not _is_denied(_invoke(priming_event, config, monkeypatch))

    rotated_key = SigningKey(kid="kid-rotated")
    server.replace_keys([original_key, rotated_key])
    rotated_event = _gateway_request(
        token=rotated_key.sign(_valid_claims()), arguments={"opportunity_id": "O-1"}
    )
    return rotated_event, server.request_count


def test_a_key_rotation_is_picked_up_by_a_kid_miss_refresh_once_the_refresh_cooldown_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key added after the first fetch resolves via a kid-miss refetch after the cooldown."""
    clock = _install_refresh_clock(monkeypatch)
    with local_jwks([SigningKey(kid="placeholder")]) as server:
        config = _config(server.jwks_url, cache_seconds=3600)
        rotated_event, requests_after_priming = _prime_then_rotate(server, config, monkeypatch)

        clock.now += config._jwk_client.cooldown_duration + 1
        result = _invoke(rotated_event, config, monkeypatch)

        assert not _is_denied(result), "a token signed by the newly rotated-in key was rejected"
        assert server.request_count > requests_after_priming, (
            "the rotated key was resolved without the client ever refetching the JWKS document"
        )


def test_an_unknown_kid_within_the_refresh_cooldown_is_refused_without_refetching_the_jwks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Within the cooldown, a kid miss is refused and never forces a JWKS fetch."""
    clock = _install_refresh_clock(monkeypatch)
    with local_jwks([SigningKey(kid="placeholder")]) as server:
        config = _config(server.jwks_url, cache_seconds=3600)
        assert config._jwk_client.cooldown_duration > 0
        rotated_event, requests_after_priming = _prime_then_rotate(server, config, monkeypatch)

        clock.now += config._jwk_client.cooldown_duration / 2
        results = [_invoke(rotated_event, config, monkeypatch) for _ in range(5)]

        assert all(_is_denied(result) for result in results)
        assert server.request_count == requests_after_priming, (
            "an unknown kid inside the refresh cooldown forced a JWKS refetch; requests went from "
            f"{requests_after_priming} to {server.request_count}"
        )


def test_a_key_evicted_from_the_bounded_per_kid_cache_is_resolved_again_via_the_jwk_set_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key evicted from the per-`kid` LRU cache still verifies from the cached JWK set."""
    outgoing_key = SigningKey(kid="kid-outgoing")
    filler_keys = [SigningKey(kid=f"filler-{i}") for i in range(20)]
    with local_jwks([outgoing_key, *filler_keys]) as server:
        config = _config(server.jwks_url, cache_seconds=3600)
        token = outgoing_key.sign(_valid_claims())
        event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

        primed = _invoke(event, config, monkeypatch)
        assert not _is_denied(primed)

        # Resolve 20 distinct kids to overflow the 16-entry per-kid LRU cache and evict the
        # original entry from THAT layer.
        for filler in filler_keys:
            filler_token = filler.sign(_valid_claims())
            filler_event = _gateway_request(token=filler_token, arguments={"opportunity_id": "O-1"})
            filler_result = _invoke(filler_event, config, monkeypatch)
            assert not _is_denied(filler_result)

        result = _invoke(event, config, monkeypatch)

    assert not _is_denied(result), (
        "the outgoing key stopped verifying after per-kid cache eviction, even though it was "
        "never rotated out of the JWKS document"
    )
    # All 22 resolutions (1 prime + 20 fillers + 1 re-resolution) are served from one HTTP fetch.
    assert server.request_count == 1


def test_a_key_removed_by_rotation_is_refused_for_a_kid_never_previously_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `kid` rotated out of the served JWKS before it is ever resolved is refused."""
    surviving_key = SigningKey(kid="kid-surviving")
    removed_key = SigningKey(kid="kid-removed")
    with local_jwks([surviving_key]) as server:  # removed_key never published at all
        config = _config(server.jwks_url, cache_seconds=3600)
        token = removed_key.sign(_valid_claims())
        event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

        result = _invoke(event, config, monkeypatch)

    assert _is_denied(result)


# --------------------------------------------------------------------------------------
# 4. exp, aud/client_id, and token_use / scope are each real checks, not stubbed
# --------------------------------------------------------------------------------------


def test_exp_expired_token_is_rejected_through_real_verification(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    token = signing_key.sign(_valid_claims(iat=now - 7200, exp=now - 3600))
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert _is_denied(result)
    assert _denial_message(result) == "Authentication failed. Sign in again to continue."


def test_exp_not_yet_expired_token_is_accepted(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token with a future `exp` is accepted."""
    now = int(time.time())
    token = signing_key.sign(_valid_claims(iat=now, exp=now + 3600))
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert not _is_denied(result)


def test_aud_and_client_id_mismatch_is_rejected(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correctly signed, unexpired, correctly scoped token for a DIFFERENT client is refused."""
    token = signing_key.sign(_valid_claims(client_id="some-other-client", aud="some-other-client"))
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert _is_denied(result)


def test_iss_is_carried_on_the_verified_claims_and_a_wrong_client_is_still_rejected_regardless(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token from an unexpected issuer and an unrecognised client is refused."""
    foreign_issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_wrongpool"
    token = signing_key.sign(
        _valid_claims(iss=foreign_issuer, client_id="not-allowed", aud="not-allowed")
    )
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert _is_denied(result)

    payload_segment = token.split(".")[1]
    decoded_unverified = json.loads(
        base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
    )
    assert decoded_unverified["iss"] == foreign_issuer


def test_token_use_id_token_style_payload_missing_client_id_falls_back_to_aud_and_can_still_pass(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ID-token-shaped claims set (no `client_id`, `token_use=id`) is judged on `aud` alone."""
    token = signing_key.sign(
        {
            k: v
            for k, v in _valid_claims(token_use="id").items()  # nosec B106  # token type, not a credential
            if k != "client_id"  # ID tokens carry aud, not client_id
        }
    )
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert not _is_denied(result)


def test_token_use_access_token_with_an_underscoped_scope_is_rejected(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An access-token-shaped payload missing the required `scope` value is refused."""
    token = signing_key.sign(_valid_claims(scope="crm/read-only"))
    event = _gateway_request(token=token, arguments={"opportunity_id": "O-1"})

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert _is_denied(result)


# --------------------------------------------------------------------------------------
# 5. The `_identity` strip invariant, asserted through the real verification path
# --------------------------------------------------------------------------------------


def _forged_channel_payload(key: str) -> dict[str, Any]:
    """Build a hostile value for one identity-channel key, shaped to look legitimate on its face."""
    if key == "requestContext":
        return {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "custom:rep_id": "attacker",
                        "custom:full_visibility": "true",
                        "custom:tenant": "eval-run-7",
                    }
                }
            }
        }
    if key == "_agent_identity":
        return {"claims": {"custom:rep_id": "attacker", "custom:full_visibility": "true"}}
    # "_identity"
    return {
        "rep_id": "attacker",
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "forged-session",
        "source": "gateway",
    }


@pytest.mark.parametrize("channel_key", sorted(IDENTITY_CHANNEL_KEYS))
def test_each_identity_channel_key_is_discarded_under_real_verification(
    channel_key: str,
    signing_key: SigningKey,
    jwks: JwksServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied value for any one channel key never survives real JWT verification."""
    token = signing_key.sign(_valid_claims())
    forged_arguments = {
        "opportunity_id": "O-1",
        channel_key: _forged_channel_payload(channel_key),
    }
    event = _gateway_request(token=token, arguments=forged_arguments)

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert not _is_denied(result)
    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]

    # No channel key survives except IDENTITY_KEY, which the interceptor itself legitimately sets.
    for key in IDENTITY_CHANNEL_KEYS:
        if key == IDENTITY_KEY:
            continue
        assert key not in arguments, f"{key!r} survived the strip"

    identity = decode_identity(arguments)
    assert identity.rep_id == "rep-1", "the verified identity did not win over the forged one"
    assert identity.full_visibility is False
    assert identity.tenant != "eval-run-7"


def test_all_identity_channel_keys_forged_simultaneously_are_all_discarded(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strip is unconditional over the whole channel set at once, not a single-key check."""
    token = signing_key.sign(_valid_claims())
    forged_arguments: dict[str, Any] = {"opportunity_id": "O-1"}
    for key in IDENTITY_CHANNEL_KEYS:
        forged_arguments[key] = _forged_channel_payload(key)
    event = _gateway_request(token=token, arguments=forged_arguments)

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert not _is_denied(result)
    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]

    for key in IDENTITY_CHANNEL_KEYS:
        if key == IDENTITY_KEY:
            continue
        assert key not in arguments

    identity = decode_identity(arguments)
    assert identity.rep_id == "rep-1"
    assert identity.full_visibility is False
    assert identity.source == "gateway"


def test_a_forged_identity_that_would_pass_verification_if_the_strip_were_conditional_still_fails(
    signing_key: SigningKey, jwks: JwksServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token carrying only a forged `requestContext` still has every identity channel stripped."""
    token = signing_key.sign(_valid_claims())
    event = _gateway_request(
        token=token,
        arguments={"requestContext": _forged_channel_payload("requestContext")},
    )

    result = _invoke(event, _config(jwks.jwks_url), monkeypatch)

    assert not _is_denied(result)
    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert "requestContext" not in arguments
    identity = decode_identity(arguments)
    assert identity.rep_id == "rep-1"
    assert identity.tenant != "eval-run-7"
