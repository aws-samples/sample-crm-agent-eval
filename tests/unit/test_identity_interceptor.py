# SPDX-License-Identifier: MIT
"""Assert the Gateway REQUEST interceptor strips every caller-controllable identity channel first,
then unconditionally injects a verified identity, and never leaks internal detail on a JWT failure.

No network call is made: `PyJWKClient.get_signing_key_from_jwt` is monkeypatched to resolve a
locally generated RSA key by `kid`, so JWT verification runs the real `PyJWT` signature-checking
code path against a real signed token without reaching Cognito.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from crm.envelope import IDENTITY_CHANNEL_KEYS, IDENTITY_KEY, decode_identity
from crm.errors import AuthenticationFailed
from gateway.identity_interceptor import (
    GatewayInterceptorConfig,
    InterceptorConfigError,
    _build_verified_identity,
    lambda_handler,
)
from tests.unit import _jwks_test_key as jwks_test_key

_KID = "test-key-1"
_ALLOWED_CLIENT_ID = "spa-client-abc123"
_ALLOWED_SCOPE = "crm/invoke"
_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/pool123"
_DISCOVERY_URL = f"{_ISSUER}/.well-known/openid-configuration"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _sign(claims: dict[str, Any], *, kid: str = _KID, key: rsa.RSAPrivateKey = _private_key) -> str:
    """Sign an RS256 JWT over `claims` with `key` (default: the module-level test key)."""
    payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
    return pyjwt.PyJWS().encode(payload, key, algorithm="RS256", headers={"kid": kid})


def _valid_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "cognito-sub-1",
        "iss": _ISSUER,
        "client_id": _ALLOWED_CLIENT_ID,
        "token_use": "access",  # nosec B105  # a token_use claim value, not a credential
        "scope": _ALLOWED_SCOPE,
        "iat": now,
        "exp": now + 3600,
        "custom:rep_id": "rep-1",
        "custom:book": "A-1,A-2",
        "custom:full_visibility": "false",
    }
    claims.update(overrides)
    return claims


@pytest.mark.parametrize("sign", ["module", "test_key"])
def test_the_signing_helpers_mint_a_verifiable_rs256_token(sign: str) -> None:
    """Each test signing helper yields an RS256 JWT that `jwt.decode` verifies to the same claims."""
    claims = _valid_claims()
    if sign == "module":
        token, public_key, kid = _sign(claims), _private_key.public_key(), _KID
    else:
        key = jwks_test_key.TestKey("helper-kid")
        token, public_key, kid = key.sign(claims), key.private_key.public_key(), "helper-kid"
    assert pyjwt.get_unverified_header(token) == {"alg": "RS256", "kid": kid, "typ": "JWT"}
    decoded = pyjwt.decode(
        token, public_key, algorithms=["RS256"], options={"verify_aud": False}, issuer=_ISSUER
    )
    assert decoded == claims


def _config() -> GatewayInterceptorConfig:
    return GatewayInterceptorConfig(
        discovery_url=_DISCOVERY_URL,
        jwks_url="https://cognito-idp.us-east-1.amazonaws.com/pool123/.well-known/jwks.json",
        allowed_client_ids=frozenset({_ALLOWED_CLIENT_ID}),
        allowed_scopes=frozenset({_ALLOWED_SCOPE}),
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


def _invoke(event: dict[str, Any], config: GatewayInterceptorConfig) -> dict[str, Any]:
    with (
        patch("gateway.identity_interceptor._load_config", return_value=config),
        patch.object(
            GatewayInterceptorConfig,
            "signing_key_for",
            return_value=_private_key.public_key(),
        ),
    ):
        return lambda_handler(event, object())


# --------------------------------------------------------------------------------------
# tools/list passthrough
# --------------------------------------------------------------------------------------


def test_tools_list_passes_through_unmodified() -> None:
    """A `tools/list` call carries no `arguments` to strip and needs no identity."""
    body_in = {"jsonrpc": "2.0", "id": "l-1", "method": "tools/list"}
    event = {"mcp": {"gatewayRequest": {"headers": {}, "body": body_in}}}

    result = _invoke(event, _config())

    assert result["mcp"]["transformedGatewayRequest"]["body"] == body_in


def test_tools_list_passes_through_even_with_no_bearer_token() -> None:
    """`tools/list` must not be gated behind a token that a listing call has no reason to carry."""
    event = _gateway_request(method="tools/list", token=None)

    result = _invoke(event, _config())

    assert "transformedGatewayRequest" in result["mcp"]
    assert "error" not in result["mcp"]["transformedGatewayRequest"]["body"]


# --------------------------------------------------------------------------------------
# Happy path — verified identity injected
# --------------------------------------------------------------------------------------


def test_valid_token_injects_verified_identity_matching_the_envelope_shape() -> None:
    token = _sign(_valid_claims())
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    rewritten = result["mcp"]["transformedGatewayRequest"]["body"]
    injected = rewritten["params"]["arguments"][IDENTITY_KEY]

    assert injected["rep_id"] == "rep-1"
    assert injected["book"] == ["A-1", "A-2"]
    assert injected["full_visibility"] is False
    assert injected["principal_type"] == "user"
    assert injected["session_id"] == "sess-abc"
    assert injected["source"] == "gateway"

    # The injected shape decodes cleanly through the real shared decoder.
    identity = decode_identity(rewritten["params"]["arguments"])
    assert identity.rep_id == "rep-1"
    assert identity.book == frozenset({"A-1", "A-2"})
    assert identity.source == "gateway"


def test_valid_token_with_full_visibility_claim_is_honoured() -> None:
    token = _sign(_valid_claims(**{"custom:full_visibility": "true", "custom:book": ""}))
    event = _gateway_request(token=token, arguments={})

    result = _invoke(event, _config())

    injected = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"][
        IDENTITY_KEY
    ]
    assert injected["full_visibility"] is True


def test_valid_token_with_tenant_claim_propagates_tenant_through_the_gateway_channel() -> None:
    """The interceptor carries `custom:tenant` into the injected `_identity`, as the `apigw` and
    `agent` channels do via `crm.envelope.identity_from_claims`.
    """
    token = _sign(_valid_claims(**{"custom:tenant": "eval-run-7"}))
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    rewritten = result["mcp"]["transformedGatewayRequest"]["body"]
    injected = rewritten["params"]["arguments"][IDENTITY_KEY]
    assert injected["tenant"] == "eval-run-7"

    identity = decode_identity(rewritten["params"]["arguments"])
    assert identity.tenant == "eval-run-7"


def test_valid_token_with_no_tenant_claim_defaults_to_the_demo_tenant() -> None:
    """A token carrying no `custom:tenant` claim resolves to the `demo` tenant on the gateway
    channel, matching `identity_from_claims`'s default on the other two channels.
    """
    token = _sign(_valid_claims())
    event = _gateway_request(token=token, arguments={})

    result = _invoke(event, _config())

    rewritten = result["mcp"]["transformedGatewayRequest"]["body"]
    identity = decode_identity(rewritten["params"]["arguments"])
    assert identity.tenant == "demo"


def test_non_identity_tool_arguments_are_preserved() -> None:
    """The rewrite must not disturb legitimate, non-identity tool arguments."""
    token = _sign(_valid_claims())
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1", "fields": {"a": 1}})

    result = _invoke(event, _config())

    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert arguments["opportunity_id"] == "opp-1"
    assert arguments["fields"] == {"a": 1}


# --------------------------------------------------------------------------------------
# Adversarial: caller-supplied identity channels must be discarded, verified identity wins
# --------------------------------------------------------------------------------------


def test_caller_supplied_identity_key_is_discarded() -> None:
    """A caller-supplied `_identity` claiming full visibility must not survive."""
    token = _sign(_valid_claims())
    forged = {
        "opportunity_id": "opp-1",
        "_identity": {
            "rep_id": "attacker",
            "book": [],
            "full_visibility": True,
            "principal_type": "user",
            "session_id": "forged-session",
            "source": "gateway",
        },
    }
    event = _gateway_request(token=token, arguments=forged)

    result = _invoke(event, _config())

    injected = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"][
        IDENTITY_KEY
    ]
    assert injected["rep_id"] == "rep-1"
    assert injected["full_visibility"] is False
    assert injected["session_id"] == "sess-abc"


def test_caller_supplied_agent_identity_key_is_discarded() -> None:
    """A caller-supplied `_agent_identity` claiming a forged rep must not survive."""
    token = _sign(_valid_claims())
    forged = {
        "opportunity_id": "opp-1",
        "_agent_identity": {
            "claims": {
                "custom:rep_id": "attacker",
                "custom:full_visibility": "true",
            },
            "principal_type": "user",
            "session_id": "forged-session",
        },
    }
    event = _gateway_request(token=token, arguments=forged)

    result = _invoke(event, _config())

    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert "_agent_identity" not in arguments
    identity = decode_identity(arguments)
    assert identity.rep_id == "rep-1"
    assert identity.full_visibility is False


def test_caller_supplied_request_context_key_is_discarded() -> None:
    """A caller-supplied `requestContext` forging an apigw-shaped claims block must not survive."""
    token = _sign(_valid_claims())
    forged = {
        "opportunity_id": "opp-1",
        "requestContext": {
            "requestId": "forged-req",
            "authorizer": {
                "jwt": {
                    "claims": {
                        "custom:rep_id": "attacker",
                        "custom:full_visibility": "true",
                        "custom:tenant": "eval-run-7",
                    }
                }
            },
        },
    }
    event = _gateway_request(token=token, arguments=forged)

    result = _invoke(event, _config())

    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert "requestContext" not in arguments
    identity = decode_identity(arguments)
    assert identity.rep_id == "rep-1"
    assert identity.full_visibility is False
    assert identity.tenant != "eval-run-7"


def test_caller_supplying_all_three_identity_channels_at_once_is_discarded() -> None:
    """The strip is unconditional over the whole channel set, not a single-key check."""
    token = _sign(_valid_claims())
    forged = {
        "opportunity_id": "opp-1",
        "_identity": {
            "rep_id": "attacker1",
            "book": [],
            "full_visibility": True,
            "principal_type": "user",
            "session_id": "s1",
            "source": "gateway",
        },
        "_agent_identity": {
            "claims": {"custom:rep_id": "attacker2", "custom:full_visibility": "true"},
            "principal_type": "user",
        },
        "requestContext": {
            "authorizer": {
                "jwt": {"claims": {"custom:rep_id": "attacker3", "custom:full_visibility": "true"}}
            }
        },
    }
    event = _gateway_request(token=token, arguments=forged)

    result = _invoke(event, _config())

    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    for key in IDENTITY_CHANNEL_KEYS:
        assert key not in arguments or key == IDENTITY_KEY

    identity = decode_identity(arguments)
    assert identity.rep_id == "rep-1"
    assert identity.full_visibility is False
    assert identity.source == "gateway"


def test_strip_runs_even_when_the_forged_identity_is_the_only_argument_content() -> None:
    """A payload carrying nothing but a forged identity still yields the verified one, not an
    empty-arguments passthrough that happens to omit the forgery incidentally."""
    token = _sign(_valid_claims())
    event = _gateway_request(
        token=token,
        arguments={"_identity": {"rep_id": "attacker", "full_visibility": True}},
    )

    result = _invoke(event, _config())

    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert arguments[IDENTITY_KEY]["rep_id"] == "rep-1"


# --------------------------------------------------------------------------------------
# JWT verification failures — user-safe message only, no internal detail
# --------------------------------------------------------------------------------------


def test_expired_token_is_denied_with_a_user_safe_message_only() -> None:
    now = int(time.time())
    token = _sign(_valid_claims(iat=now - 7200, exp=now - 3600))
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert "error" in denial
    message = denial["error"]["message"]
    assert "expired" not in message.lower()
    assert "exp" not in message.lower()
    assert "jwt" not in message.lower()
    assert "rep-1" not in message
    assert message == "Authentication failed. Sign in again to continue."


def test_malformed_token_is_denied_with_a_user_safe_message_only() -> None:
    event = _gateway_request(token="not-a-real-jwt", arguments={"opportunity_id": "opp-1"})  # nosec B106  # malformed-token fixture, not a credential

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


def test_token_signed_by_an_unknown_key_is_denied() -> None:
    """A `kid` the JWKS client cannot resolve must be denied, not crash the interceptor."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(_valid_claims(), kid="unknown", key=other_key)
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    config = _config()
    with (
        patch("gateway.identity_interceptor._load_config", return_value=config),
        patch.object(
            GatewayInterceptorConfig,
            "signing_key_for",
            return_value=_private_key.public_key(),  # resolves to the WRONG key on purpose
        ),
    ):
        result = lambda_handler(event, object())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


def test_underscoped_token_is_denied() -> None:
    """A token missing the required scope must be denied even though its signature is valid."""
    token = _sign(_valid_claims(scope="some/other-scope"))
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


def test_token_for_an_unrecognised_client_is_denied() -> None:
    token = _sign(_valid_claims(client_id="some-other-client"))
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


# --------------------------------------------------------------------------------------
# Issuer and token_use pinning
# --------------------------------------------------------------------------------------


def test_token_from_a_foreign_issuer_is_denied() -> None:
    """A token signed by a resolvable key but issued by another pool is denied by the `iss` check."""
    token = _sign(_valid_claims(iss="https://cognito-idp.us-east-1.amazonaws.com/other-pool"))
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."
    assert "transformedGatewayRequest" not in result["mcp"]


def test_token_with_no_issuer_claim_is_denied() -> None:
    """An absent `iss` fails closed rather than skipping the issuer comparison."""
    claims = _valid_claims()
    del claims["iss"]
    event = _gateway_request(token=_sign(claims), arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


def test_token_with_no_expiry_claim_is_denied() -> None:
    """`exp` is required, so an unexpiring token cannot be presented indefinitely."""
    claims = _valid_claims()
    del claims["exp"]
    event = _gateway_request(token=_sign(claims), arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


@pytest.mark.parametrize("token_use", ["", "refresh", "ACCESS", "unknown"])
def test_token_with_an_unrecognised_token_use_is_denied(token_use: str) -> None:
    """Only `access` and `id` are accepted; anything else is refused, not treated as an access token."""
    event = _gateway_request(
        token=_sign(_valid_claims(token_use=token_use)), arguments={"opportunity_id": "opp-1"}
    )

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


def test_access_token_cannot_present_its_client_identifier_as_aud_only() -> None:
    """`token_use` selects the claim: an access token's client id must be in `client_id`, not `aud`."""
    claims = _valid_claims(aud=_ALLOWED_CLIENT_ID)
    del claims["client_id"]
    event = _gateway_request(token=_sign(claims), arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


def test_id_token_is_accepted_on_its_own_aud_claim() -> None:
    """An ID token remains acceptable, decided on `aud` because its `token_use` says so."""
    claims = _valid_claims(token_use="id", aud=_ALLOWED_CLIENT_ID)  # nosec B106  # Cognito token_use claim value, not a credential
    del claims["client_id"]
    event = _gateway_request(token=_sign(claims), arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert arguments[IDENTITY_KEY]["rep_id"] == "rep-1"


def test_id_token_with_multiple_audiences_is_denied() -> None:
    """A multi-valued `aud` names no single client, so it resolves to none rather than the first."""
    claims = _valid_claims(token_use="id", aud=[_ALLOWED_CLIENT_ID, "another-client"])  # nosec B106  # Cognito token_use claim value, not a credential
    del claims["client_id"]
    event = _gateway_request(token=_sign(claims), arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


def test_config_exposes_the_issuer_the_discovery_url_names() -> None:
    assert _config().issuer == _ISSUER


def test_config_rejects_a_discovery_url_that_pins_no_issuer() -> None:
    """A discovery URL of an unexpected shape is a configuration error, not an unpinned issuer."""
    with pytest.raises(InterceptorConfigError):
        GatewayInterceptorConfig(
            discovery_url="https://cognito-idp.us-east-1.amazonaws.com/pool123",
            jwks_url="https://example.com/jwks.json",
            allowed_client_ids=frozenset({"c1"}),
        )


def test_missing_bearer_token_is_denied() -> None:
    event = _gateway_request(token=None, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."


def test_denial_response_echoes_the_jsonrpc_id_for_correlation() -> None:
    event = _gateway_request(token=None, arguments={"opportunity_id": "opp-1"}, tool_id="corr-42")

    result = _invoke(event, _config())

    denial = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["id"] == "corr-42"


def test_denial_never_contains_the_raw_token_text() -> None:
    token = _sign(_valid_claims(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600))
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    denial_text = str(result["mcp"]["transformedGatewayResponse"]["body"])
    assert token not in denial_text


def _assert_denied(result: dict[str, Any]) -> dict[str, Any]:
    assert "transformedGatewayRequest" not in result["mcp"]
    denial: dict[str, Any] = result["mcp"]["transformedGatewayResponse"]["body"]
    assert denial["error"]["code"] == -32001
    assert denial["error"]["message"] == "Authentication failed. Sign in again to continue."
    return denial


def _event_with_body(body: object, *, token: str | None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return {"mcp": {"gatewayRequest": {"headers": headers, "body": body}}}


@pytest.mark.parametrize("body", [[{"method": "tools/list"}], ["tools/call"], "tools/call", 7])
def test_a_non_object_body_is_denied_even_with_a_valid_token(body: object) -> None:
    """A JSON-RPC batch (array) or scalar body is refused before any field of it is read."""
    result = _invoke(_event_with_body(body, token=_sign(_valid_claims())), _config())

    denial = _assert_denied(result)
    assert denial["id"] is None


@pytest.mark.parametrize("params", ["x", ["ab"], ["ab", "cd"], 3])
def test_a_non_object_params_is_denied_even_with_a_valid_token(params: object) -> None:
    """`params` that is not an object can carry no `arguments._identity`, so it is refused."""
    body = {"jsonrpc": "2.0", "id": "p-1", "method": "tools/call", "params": params}

    result = _invoke(_event_with_body(body, token=_sign(_valid_claims())), _config())

    denial = _assert_denied(result)
    assert denial["id"] == "p-1"


def test_an_absent_params_still_reaches_token_verification() -> None:
    """Only a present, non-object `params` is malformed; an omitted one is not denied for shape."""
    body = {"jsonrpc": "2.0", "id": "p-2", "method": "initialize"}

    result = _invoke(_event_with_body(body, token=_sign(_valid_claims())), _config())

    rewritten = result["mcp"]["transformedGatewayRequest"]["body"]
    assert rewritten["params"]["arguments"][IDENTITY_KEY]["rep_id"] == "rep-1"


def test_an_unexpected_exception_is_denied_with_one_structured_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any exception inside the handler becomes a denial, logged without a traceback or token."""
    token = _sign(_valid_claims())
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"}, tool_id="u-1")

    with (
        caplog.at_level(logging.DEBUG, logger="gateway.identity_interceptor"),
        patch(
            "gateway.identity_interceptor._build_verified_identity",
            side_effect=RuntimeError("boom"),
        ),
    ):
        result = _invoke(event, _config())

    denial = _assert_denied(result)
    assert denial["id"] == "u-1"
    denied = [
        record
        for record in caplog.records
        if json.loads(record.getMessage()).get("event") == "gateway_auth_denied"
    ]
    assert len(denied) == 1
    assert json.loads(denied[0].getMessage())["reason"] == "unexpected_error:RuntimeError"
    assert denied[0].exc_info is None
    assert all(token not in record.getMessage() for record in caplog.records)
    assert "boom" not in caplog.text


def test_a_configuration_error_is_denied_and_logs_which_setting_is_wrong(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Lambda with missing configuration fails closed, and the denial names the bad setting."""
    token = _sign(_valid_claims())
    event = _gateway_request(token=token, arguments={}, tool_id="c-1")

    with (
        caplog.at_level(logging.DEBUG, logger="gateway.identity_interceptor"),
        patch(
            "gateway.identity_interceptor._load_config",
            side_effect=InterceptorConfigError("discovery_url must be non-empty"),
        ),
    ):
        result = lambda_handler(event, object())

    assert _assert_denied(result)["id"] == "c-1"
    reasons = [json.loads(record.getMessage())["reason"] for record in caplog.records]
    assert reasons == ["config_error:discovery_url must be non-empty"]
    assert token not in caplog.text


@pytest.mark.parametrize("event", [{}, {"mcp": []}, {"mcp": {"gatewayRequest": "x"}}, []])
def test_a_malformed_event_envelope_is_denied(event: Any) -> None:
    result = _invoke(event, _config())

    assert _assert_denied(result)["id"] is None


def test_denial_never_contains_an_account_identifier() -> None:
    """No account id appears in a user-facing denial, even one taken from the book."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad_token = _sign(_valid_claims(**{"custom:book": "A-SECRET-ACCOUNT-999"}), key=other_key)
    event = _gateway_request(token=bad_token, arguments={"opportunity_id": "opp-1"})

    config = _config()
    with (
        patch("gateway.identity_interceptor._load_config", return_value=config),
        patch.object(
            GatewayInterceptorConfig,
            "signing_key_for",
            return_value=_private_key.public_key(),
        ),
    ):
        result = lambda_handler(event, object())

    denial_text = str(result["mcp"]["transformedGatewayResponse"]["body"])
    assert "A-SECRET-ACCOUNT-999" not in denial_text


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_config_rejects_empty_discovery_url() -> None:
    with pytest.raises(InterceptorConfigError):
        GatewayInterceptorConfig(
            discovery_url="",
            jwks_url="https://example.com/jwks.json",
            allowed_client_ids=frozenset({"c1"}),
        )


def test_config_rejects_empty_jwks_url() -> None:
    with pytest.raises(InterceptorConfigError):
        GatewayInterceptorConfig(
            discovery_url="https://example.com/.well-known/openid-configuration",
            jwks_url="",
            allowed_client_ids=frozenset({"c1"}),
        )


def test_config_rejects_empty_allowed_client_ids() -> None:
    with pytest.raises(InterceptorConfigError):
        GatewayInterceptorConfig(
            discovery_url="https://example.com/.well-known/openid-configuration",
            jwks_url="https://example.com/jwks.json",
            allowed_client_ids=frozenset(),
        )


def test_load_config_raises_when_environment_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    import gateway.identity_interceptor as module

    monkeypatch.delenv("COGNITO_DISCOVERY_URL", raising=False)
    monkeypatch.delenv("COGNITO_JWKS_URL", raising=False)
    monkeypatch.delenv("COGNITO_ALLOWED_CLIENT_IDS", raising=False)
    monkeypatch.setattr(module, "_config_cache", None)

    with pytest.raises(InterceptorConfigError):
        module._load_config()


def test_load_config_is_cached_across_invocations(monkeypatch: pytest.MonkeyPatch) -> None:
    import gateway.identity_interceptor as module

    monkeypatch.setenv(
        "COGNITO_DISCOVERY_URL",
        "https://cognito-idp.us-east-1.amazonaws.com/pool1/.well-known/openid-configuration",
    )
    monkeypatch.setenv(
        "COGNITO_JWKS_URL",
        "https://cognito-idp.us-east-1.amazonaws.com/pool1/.well-known/jwks.json",
    )
    monkeypatch.setenv("COGNITO_ALLOWED_CLIENT_IDS", "client-a,client-b")
    monkeypatch.setattr(module, "_config_cache", None)

    first = module._load_config()
    second = module._load_config()

    assert first is second
    assert first.allowed_client_ids == frozenset({"client-a", "client-b"})


# --------------------------------------------------------------------------------------
# AuthenticationFailed never escapes decode_identity's own contract via this handler
# --------------------------------------------------------------------------------------


def test_injected_identity_is_never_rejected_by_the_shared_decoder() -> None:
    """The shape the interceptor injects must satisfy `decode_identity` without raising."""
    token = _sign(_valid_claims())
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    result = _invoke(event, _config())

    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    try:
        decode_identity(arguments)
    except AuthenticationFailed as exc:  # pragma: no cover - failure path documented by raising
        pytest.fail(f"injected identity was rejected by decode_identity: {exc.detail}")


# --------------------------------------------------------------------------------------
# Service principal: a machine token carries no custom:* claim, so identity comes from a
# configured rep_id whose book is read from the reps table. Every missing precondition fails
# closed to authentication_failed.
# --------------------------------------------------------------------------------------

_SERVICE_REP_ID = "svc-mcp-gateway"


def _service_config(rep_id: str = _SERVICE_REP_ID) -> GatewayInterceptorConfig:
    return GatewayInterceptorConfig(
        discovery_url=_DISCOVERY_URL,
        jwks_url="https://cognito-idp.us-east-1.amazonaws.com/pool123/.well-known/jwks.json",
        allowed_client_ids=frozenset({_ALLOWED_CLIENT_ID}),
        allowed_scopes=frozenset({_ALLOWED_SCOPE}),
        service_principal_rep_id=rep_id,
        table_prefix="crm-agent-eval",
    )


def _machine_claims(**overrides: Any) -> dict[str, Any]:
    """Claims of a real `client_credentials` access token: no `custom:*` claim at all."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "machine-sub-1",
        "iss": _ISSUER,
        "client_id": _ALLOWED_CLIENT_ID,
        "token_use": "access",  # nosec B105  # a token_use claim value, not a credential
        "scope": _ALLOWED_SCOPE,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return claims


def _rep_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "rep_id": _SERVICE_REP_ID,
        "tenant": "eval-smoke-1",
        "book": ["acct-1", "acct-2"],
        "full_visibility": False,
    }
    row.update(overrides)
    return row


def _build(
    claims: dict[str, Any],
    config: GatewayInterceptorConfig | None,
    lookup: Any = None,
) -> dict[str, Any]:
    return _build_verified_identity(claims, session_id="sess-1", config=config, rep_lookup=lookup)


def test_machine_token_maps_to_the_configured_scoped_service_principal() -> None:
    """The book and tenant come from the configured rep's `reps` item, not from the token."""
    identity = _build(_machine_claims(), _service_config(), lambda _rep_id: _rep_row())

    assert identity["rep_id"] == _SERVICE_REP_ID
    assert identity["principal_type"] == "service"
    assert sorted(identity["book"]) == ["acct-1", "acct-2"]
    assert identity["full_visibility"] is False
    assert identity["tenant"] == "eval-smoke-1"


def test_service_principal_identity_is_accepted_by_the_shared_decoder() -> None:
    """`crm.envelope._from_gateway_identity` must accept the injected service identity unchanged."""
    identity = _build(_machine_claims(), _service_config(), lambda _rep_id: _rep_row())

    decoded = decode_identity({IDENTITY_KEY: identity})

    assert decoded.principal_type == "service"
    assert decoded.book == frozenset({"acct-1", "acct-2"})
    assert decoded.full_visibility is False


def test_machine_token_is_unauthenticable_when_no_service_principal_is_configured() -> None:
    """Fails closed: absent the configuration this must never become a permissive default."""
    identity = _build(_machine_claims(), _service_config(rep_id=""), lambda _rep_id: _rep_row())

    assert identity["rep_id"] == ""
    with pytest.raises(AuthenticationFailed):
        decode_identity({IDENTITY_KEY: identity})


def test_machine_token_is_unauthenticable_when_no_config_is_supplied() -> None:
    """With no interceptor configuration there is no service-principal path at all."""
    identity = _build(_machine_claims(), None)

    assert identity["rep_id"] == ""
    with pytest.raises(AuthenticationFailed):
        decode_identity({IDENTITY_KEY: identity})


def test_service_principal_is_refused_when_its_reps_item_is_absent() -> None:
    """A configured rep_id with no `reps` row grants nothing."""
    identity = _build(_machine_claims(), _service_config(), lambda _rep_id: None)

    assert identity["rep_id"] == ""


def test_service_principal_is_refused_when_the_book_is_not_an_explicit_list() -> None:
    """An absent or non-list `book` is refused rather than defaulted — the book must be explicit."""
    for row in (_rep_row(book=None), _rep_row(book="acct-1,acct-2"), {"rep_id": _SERVICE_REP_ID}):
        identity = _build(_machine_claims(), _service_config(), lambda _rep_id, r=row: r)
        assert identity["rep_id"] == ""


def test_service_principal_with_an_explicitly_empty_book_authenticates_with_no_accounts() -> None:
    """An explicit `[]` is configured data, so the principal exists but every scope check denies."""
    identity = _build(_machine_claims(), _service_config(), lambda _rep_id: _rep_row(book=[]))

    assert identity["rep_id"] == _SERVICE_REP_ID
    assert identity["book"] == []
    assert decode_identity({IDENTITY_KEY: identity}).may_access("acct-1") is False


def test_service_principal_is_refused_when_the_reps_item_names_no_tenant() -> None:
    """Never defaulted to `demo`: a service principal may not implicitly reach the demo tenant."""
    for row in (_rep_row(tenant=""), _rep_row(tenant=None)):
        identity = _build(_machine_claims(), _service_config(), lambda _rep_id, r=row: r)
        assert identity["rep_id"] == ""


def test_service_principal_is_refused_when_its_reps_item_asks_for_full_visibility() -> None:
    """A reps row asking for full visibility is refused, not silently downgraded."""
    for value in (True, "true"):
        identity = _build(
            _machine_claims(),
            _service_config(),
            lambda _rep_id, v=value: _rep_row(full_visibility=v),
        )
        assert identity["rep_id"] == ""


def test_service_principal_lookup_failure_fails_closed_without_raising() -> None:
    """A DynamoDB failure must not surface as a Lambda 500; it denies instead."""

    def exploding_lookup(_rep_id: str) -> dict[str, Any] | None:
        raise RuntimeError("dynamodb unavailable")

    identity = _build(_machine_claims(), _service_config(), exploding_lookup)

    assert identity["rep_id"] == ""


def test_a_user_token_never_takes_the_service_principal_path() -> None:
    """`custom:rep_id` wins: a human sign-in stays a `user` principal with the token's own book."""

    def never_called(_rep_id: str) -> dict[str, Any] | None:  # pragma: no cover - must not run
        raise AssertionError("the reps lookup must not run for a token carrying custom:rep_id")

    identity = _build(_valid_claims(), _service_config(), never_called)

    assert identity["principal_type"] == "user"
    assert identity["rep_id"] == "rep-1"


def test_handler_injects_the_service_identity_for_a_real_machine_token() -> None:
    """End to end through `lambda_handler`, with only the `reps` read stubbed."""
    token = _sign(_machine_claims())
    event = _gateway_request(token=token, arguments={"opportunity_id": "opp-1"})

    with patch(
        "gateway.identity_interceptor._default_rep_lookup",
        return_value=lambda _rep_id: _rep_row(),
    ):
        result = _invoke(event, _service_config())

    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert arguments[IDENTITY_KEY]["principal_type"] == "service"
    assert arguments[IDENTITY_KEY]["rep_id"] == _SERVICE_REP_ID


def test_handler_replaces_rather_than_merges_a_forged_identity_on_the_service_path() -> None:
    """A caller-supplied `_identity` is discarded on the service path exactly as on the user path."""
    token = _sign(_machine_claims())
    event = _gateway_request(
        token=token,
        arguments={
            "opportunity_id": "opp-1",
            IDENTITY_KEY: {
                "rep_id": "attacker",
                "book": ["acct-out-of-book"],
                "full_visibility": True,
                "principal_type": "user",
                "source": "gateway",
            },
        },
    )

    with patch(
        "gateway.identity_interceptor._default_rep_lookup",
        return_value=lambda _rep_id: _rep_row(),
    ):
        result = _invoke(event, _service_config())

    injected = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"][
        IDENTITY_KEY
    ]
    assert injected["rep_id"] == _SERVICE_REP_ID
    assert injected["full_visibility"] is False
    assert sorted(injected["book"]) == ["acct-1", "acct-2"]


def test_load_config_reads_the_service_principal_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed Lambda receives its service principal through these two variables."""
    import gateway.identity_interceptor as module

    monkeypatch.setattr(module, "_config_cache", None)
    monkeypatch.setenv(
        "COGNITO_DISCOVERY_URL", "https://example.test/.well-known/openid-configuration"
    )
    monkeypatch.setenv("COGNITO_JWKS_URL", "https://example.test/.well-known/jwks.json")
    monkeypatch.setenv("COGNITO_ALLOWED_CLIENT_IDS", "client-a")
    monkeypatch.setenv(module.SERVICE_PRINCIPAL_REP_ID_ENV, _SERVICE_REP_ID)
    monkeypatch.setenv(module.TABLE_PREFIX_ENV, "crm-agent-eval")

    config = module._load_config()

    assert config.service_principal_rep_id == _SERVICE_REP_ID
    assert config.table_prefix == "crm-agent-eval"


def test_load_config_leaves_the_service_principal_unconfigured_when_the_variable_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset variable is not an error — it means no service principal exists (fail closed)."""
    import gateway.identity_interceptor as module

    monkeypatch.setattr(module, "_config_cache", None)
    monkeypatch.setenv(
        "COGNITO_DISCOVERY_URL", "https://example.test/.well-known/openid-configuration"
    )
    monkeypatch.setenv("COGNITO_JWKS_URL", "https://example.test/.well-known/jwks.json")
    monkeypatch.setenv("COGNITO_ALLOWED_CLIENT_IDS", "client-a")
    monkeypatch.delenv(module.SERVICE_PRINCIPAL_REP_ID_ENV, raising=False)

    config = module._load_config()

    assert config.service_principal_rep_id == ""


# --------------------------------------------------------------------------------------
# Per-instance scope: the same code, configured per gateway
# --------------------------------------------------------------------------------------

_CHAT_SCOPE = "supervisor/chat"
_GATEWAY_INVOKE = "gateway/invoke"
_M2M_CLIENT_ID = "m2m-client-xyz"


def _env_configured_invoke(
    monkeypatch: pytest.MonkeyPatch, *, allowed_scopes: str, token: str
) -> dict[str, Any]:
    """Invoke the handler with config loaded from env, as a deployed instance loads it."""
    import gateway.identity_interceptor as module

    monkeypatch.setattr(module, "_config_cache", None)
    monkeypatch.setenv("COGNITO_DISCOVERY_URL", _DISCOVERY_URL)
    monkeypatch.setenv("COGNITO_JWKS_URL", f"{_ISSUER}/.well-known/jwks.json")
    monkeypatch.setenv("COGNITO_ALLOWED_CLIENT_IDS", f"{_ALLOWED_CLIENT_ID},{_M2M_CLIENT_ID}")
    monkeypatch.setenv("COGNITO_ALLOWED_SCOPES", allowed_scopes)
    with patch.object(
        GatewayInterceptorConfig, "signing_key_for", return_value=_private_key.public_key()
    ):
        return lambda_handler(_gateway_request(token=token), object())


def _user_token(scope: str) -> str:
    return _sign(_valid_claims(scope=scope, client_id=_ALLOWED_CLIENT_ID))


def _machine_token(scope: str) -> str:
    return _sign(_valid_claims(scope=scope, client_id=_M2M_CLIENT_ID))


def _denied(result: dict[str, Any]) -> bool:
    return "transformedGatewayResponse" in result["mcp"]


def test_the_chat_instance_admits_a_user_token_carrying_the_chat_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _env_configured_invoke(
        monkeypatch, allowed_scopes=_CHAT_SCOPE, token=_user_token(f"openid {_CHAT_SCOPE}")
    )

    assert not _denied(result)
    arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert arguments[IDENTITY_KEY]["rep_id"] == "rep-1"


def test_the_chat_instance_refuses_a_gateway_invoke_only_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _env_configured_invoke(
        monkeypatch, allowed_scopes=_CHAT_SCOPE, token=_machine_token(_GATEWAY_INVOKE)
    )

    assert _denied(result)


def test_the_tools_instance_refuses_a_chat_scope_only_user_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user token minted with the chat scope cannot reach Gateway #1's CRM tools."""
    result = _env_configured_invoke(
        monkeypatch, allowed_scopes=_GATEWAY_INVOKE, token=_user_token(f"openid {_CHAT_SCOPE}")
    )

    assert _denied(result)


def test_the_tools_instance_still_admits_its_machine_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _env_configured_invoke(
        monkeypatch, allowed_scopes=_GATEWAY_INVOKE, token=_machine_token(_GATEWAY_INVOKE)
    )

    assert not _denied(result)


# --------------------------------------------------------------------------------------
# Log levels: every denial or refusal decision is a WARNING; a success stays INFO.
# --------------------------------------------------------------------------------------

_LOGGER_NAME = "gateway.identity_interceptor"


def _records_for(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == _LOGGER_NAME and json.loads(record.getMessage()).get("event") == event
    ]


def _exploding_lookup(_rep_id: str) -> dict[str, Any] | None:
    raise RuntimeError("dynamodb unavailable")


@pytest.mark.parametrize(
    ("lookup", "event"),
    [
        (_exploding_lookup, "gateway_service_identity_lookup_failed"),
        (lambda _rep_id: None, "gateway_service_identity_absent"),
        (lambda _rep_id: _rep_row(book=None), "gateway_service_identity_book_not_explicit"),
        (lambda _rep_id: _rep_row(tenant=""), "gateway_service_identity_tenant_absent"),
        (
            lambda _rep_id: _rep_row(full_visibility=True),
            "gateway_service_identity_full_visibility_refused",
        ),
    ],
    ids=["lookup_failed", "absent", "book_not_explicit", "tenant_absent", "full_visibility"],
)
def test_each_service_identity_refusal_logs_at_warning(
    caplog: pytest.LogCaptureFixture, lookup: Any, event: str
) -> None:
    """A refused service principal is a denial decision, so it is logged at WARNING."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        identity = _build(_machine_claims(), _service_config(), lookup)

    assert identity["rep_id"] == ""
    records = _records_for(caplog, event)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


def _expired_token() -> str:
    return _sign(_valid_claims(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600))


@pytest.mark.parametrize(
    ("make_event", "reason"),
    [
        (
            lambda: _event_with_body(["tools/call"], token=_sign(_valid_claims())),
            "body_not_an_object",
        ),
        (
            lambda: _event_with_body(
                {"jsonrpc": "2.0", "id": "p", "method": "tools/call", "params": "x"},
                token=_sign(_valid_claims()),
            ),
            "params_not_an_object",
        ),
        (lambda: _gateway_request(token=None, arguments={}), "no_bearer_token"),
        (lambda: _gateway_request(token=_expired_token(), arguments={}), "ExpiredSignatureError"),
        (lambda: _gateway_request(token="not-a-jwt", arguments={}), "DecodeError"),  # nosec B106  # malformed-token fixture
    ],
    ids=["body_not_an_object", "params_not_an_object", "no_bearer", "expired", "malformed"],
)
def test_each_handler_denial_logs_at_warning(
    caplog: pytest.LogCaptureFixture, make_event: Any, reason: str
) -> None:
    """Every `gateway_auth_denied` decision inside the handler is logged at WARNING."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _assert_denied(_invoke(make_event(), _config()))

    records = _records_for(caplog, "gateway_auth_denied")
    assert [json.loads(record.getMessage())["reason"] for record in records] == [reason]
    assert records[0].levelno == logging.WARNING


@pytest.mark.parametrize(
    ("patch_target", "error", "reason"),
    [
        (
            "gateway.identity_interceptor._build_verified_identity",
            RuntimeError("boom"),
            "unexpected_error:RuntimeError",
        ),
        (
            "gateway.identity_interceptor._load_config",
            InterceptorConfigError("discovery_url must be non-empty"),
            "config_error:discovery_url must be non-empty",
        ),
    ],
    ids=["unexpected_error", "config_error"],
)
def test_a_handler_failure_denial_logs_at_warning(
    caplog: pytest.LogCaptureFixture, patch_target: str, error: Exception, reason: str
) -> None:
    """The fail-closed wrapper's denial is logged at WARNING."""
    event = _gateway_request(token=_sign(_valid_claims()), arguments={})
    with (
        caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME),
        patch.object(
            GatewayInterceptorConfig, "signing_key_for", return_value=_private_key.public_key()
        ),
        patch("gateway.identity_interceptor._load_config", return_value=_config()),
        patch(patch_target, side_effect=error),
    ):
        _assert_denied(lambda_handler(event, object()))

    records = _records_for(caplog, "gateway_auth_denied")
    assert [json.loads(record.getMessage())["reason"] for record in records] == [reason]
    assert records[0].levelno == logging.WARNING


def test_a_verified_request_logs_at_info_and_emits_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A success is not a denial: it stays at INFO and produces no WARNING record."""
    event = _gateway_request(token=_sign(_valid_claims()), arguments={"opportunity_id": "opp-1"})
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        result = _invoke(event, _config())

    assert "transformedGatewayRequest" in result["mcp"]
    verified = _records_for(caplog, "gateway_auth_verified")
    assert len(verified) == 1
    assert verified[0].levelno == logging.INFO
    assert not [
        r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno >= logging.WARNING
    ]


def test_a_successful_service_identity_lookup_logs_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A service principal that is accepted produces no refusal record."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        identity = _build(_machine_claims(), _service_config(), lambda _rep_id: _rep_row())

    assert identity["rep_id"] == _SERVICE_REP_ID
    assert not [
        r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno >= logging.WARNING
    ]
