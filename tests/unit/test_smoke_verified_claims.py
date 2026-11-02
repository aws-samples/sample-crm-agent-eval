# SPDX-License-Identifier: MIT
"""Unit tests for `scripts.smoke.verified_claims`, against a real loopback JWKS document."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Literal

import jwt
import pytest
from scripts.smoke import verified_claims as verified_claims_module
from scripts.smoke.verified_claims import (
    CLOCK_SKEW_SECONDS,
    USE_ACCESS,
    USE_ID,
    ClaimsVerificationError,
    cognito_jwks_url,
    verified_claims,
)

from tests.unit import _jwks_test_key as keys


def _verify(
    token: str, token_use: Literal["id", "access"] = USE_ID, **kwargs: Any
) -> dict[str, Any]:
    return verified_claims(
        token,
        region=keys.REGION,
        user_pool_id=keys.USER_POOL_ID,
        client_id=keys.CLIENT_ID,
        token_use=token_use,
        jwks_url=kwargs.pop("jwks_url", keys.jwks_url()),
        **kwargs,
    )


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_a_valid_id_token_returns_its_claims() -> None:
    claims = _verify(keys.id_token(**{"custom:tenant": "eval-smoke-x"}))
    assert claims["custom:tenant"] == "eval-smoke-x"
    assert claims["aud"] == keys.CLIENT_ID


def test_a_valid_access_token_returns_its_claims() -> None:
    claims = _verify(keys.access_token(scope="crm/read"), token_use=USE_ACCESS)
    assert claims["scope"] == "crm/read"


def test_a_tampered_payload_fails_the_signature_check() -> None:
    header, _payload, signature = keys.id_token().split(".")
    forged = _b64(keys.id_token_claims(**{"cognito:groups": ["admin"]}))
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(f"{header}.{forged}.{signature}")
    assert info.value.check == "signature"


def test_a_token_signed_by_an_unpublished_key_with_the_published_kid_fails() -> None:
    impostor = keys.TestKey(kid=keys.DEFAULT_KID)
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(impostor.sign(keys.id_token_claims()))
    assert info.value.check == "signature"


def test_an_unknown_kid_fails_the_signing_key_check() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.sign(keys.id_token_claims(), kid="not-published"))
    assert info.value.check == "signing_key"


def test_alg_none_is_refused() -> None:
    token = f"{_b64({'alg': 'none', 'typ': 'JWT', 'kid': keys.DEFAULT_KID})}."
    token += f"{_b64(keys.id_token_claims())}."
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(token)
    assert info.value.check == "alg"


def test_hs256_is_refused_even_keyed_with_the_public_jwk() -> None:
    public_n = keys.default_key().jwk()["n"]
    token = jwt.encode(
        keys.id_token_claims(),
        public_n.ljust(32, "x"),
        algorithm="HS256",
        headers={"kid": keys.DEFAULT_KID},
    )
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(token)
    assert info.value.check == "alg"


def test_a_wrong_issuer_fails() -> None:
    token = keys.id_token(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other")
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(token)
    assert info.value.check == "iss"


def test_a_missing_issuer_fails() -> None:
    claims = keys.id_token_claims()
    del claims["iss"]
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.sign(claims))
    assert info.value.check == "iss"


def test_an_access_token_presented_as_an_id_token_fails_token_use() -> None:
    token = keys.access_token(aud=keys.CLIENT_ID)
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(token, token_use=USE_ID)
    assert info.value.check == "token_use"


def test_an_id_token_presented_as_an_access_token_fails_token_use() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(), token_use=USE_ACCESS)
    assert info.value.check == "token_use"


def test_an_id_token_for_another_client_fails_aud() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(aud="other-client"))
    assert info.value.check == "aud"


def test_an_id_token_without_aud_fails_aud() -> None:
    claims = keys.id_token_claims()
    del claims["aud"]
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.sign(claims))
    assert info.value.check == "aud"


def test_an_access_token_for_another_client_fails_client_id() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.access_token(client_id="other-client"), token_use=USE_ACCESS)
    assert info.value.check == "client_id"


def test_an_expired_token_fails_exp() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(exp=int(time.time()) - 3600))
    assert info.value.check == "exp"


def test_leeway_admits_a_token_just_past_expiry() -> None:
    claims = _verify(keys.id_token(exp=int(time.time()) - 5), leeway_seconds=60)
    assert claims["token_use"] == "id"


def test_the_default_clock_skew_is_sixty_seconds() -> None:
    assert CLOCK_SKEW_SECONDS == 60


@pytest.mark.parametrize("ahead", [1, 59])
def test_an_iat_slightly_ahead_of_the_host_clock_verifies_by_default(ahead: int) -> None:
    claims = _verify(keys.id_token(iat=int(time.time()) + ahead))
    assert claims["token_use"] == "id"


def test_an_iat_beyond_the_default_skew_fails_iat() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(iat=int(time.time()) + 120))
    assert info.value.check == "iat"


def test_an_nbf_beyond_the_default_skew_fails_nbf() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(nbf=int(time.time()) + 120))
    assert info.value.check == "nbf"


def test_an_exp_within_the_default_skew_verifies() -> None:
    claims = _verify(keys.id_token(exp=int(time.time()) - 30))
    assert claims["token_use"] == "id"


def test_an_exp_beyond_the_default_skew_fails_exp() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(exp=int(time.time()) - 120))
    assert info.value.check == "exp"


def test_an_explicit_zero_leeway_still_refuses_a_future_iat() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(iat=int(time.time()) + 5), leeway_seconds=0)
    assert info.value.check == "iat"


def test_a_token_without_exp_fails_exp() -> None:
    claims = keys.id_token_claims()
    del claims["exp"]
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.sign(claims))
    assert info.value.check == "exp"


def test_a_malformed_token_fails_the_header_check() -> None:
    with pytest.raises(ClaimsVerificationError) as info:
        _verify("not-a-jwt")
    assert info.value.check == "header"


def test_an_unreachable_jwks_fails_the_signing_key_check() -> None:
    with keys.local_jwks([keys.default_key()]) as url:
        pass
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(), jwks_url=url)
    assert info.value.check == "signing_key"


def test_a_key_served_by_a_per_test_jwks_verifies() -> None:
    rotated = keys.TestKey(kid="rotated")
    with keys.local_jwks([rotated]) as url:
        claims = _verify(rotated.sign(keys.id_token_claims()), jwks_url=url)
    assert claims["token_use"] == "id"


class _FetchReachedError(Exception):
    """Raised by a stand-in key client: proves the URL check let the fetch stage begin."""


def _refuse_fetch(url: str) -> Any:
    raise _FetchReachedError(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool/.well-known/jwks.json",
        "http://10.0.0.1/jwks.json",
        "ftp://127.0.0.1/jwks.json",
        "file:///etc/jwks.json",
        "https:///no-host",
        "jwks.json",
    ],
)
def test_an_untrusted_jwks_url_is_refused_before_any_fetch(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(verified_claims_module, "_jwk_client", _refuse_fetch)
    with pytest.raises(ClaimsVerificationError) as info:
        _verify(keys.id_token(), jwks_url=url)
    assert info.value.check == "jwks_url"


def test_the_https_default_url_passes_the_check_and_reaches_the_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verified_claims_module, "_jwk_client", _refuse_fetch)
    with pytest.raises(_FetchReachedError) as info:
        verified_claims(
            keys.id_token(),
            region=keys.REGION,
            user_pool_id=keys.USER_POOL_ID,
            client_id=keys.CLIENT_ID,
            token_use=USE_ID,
        )
    assert str(info.value) == cognito_jwks_url(keys.REGION, keys.USER_POOL_ID)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_loopback_http_jwks_url_verifies(host: str) -> None:
    url = keys.jwks_url().replace("127.0.0.1", host)
    assert _verify(keys.id_token(), jwks_url=url)["token_use"] == USE_ID


def test_the_default_jwks_url_is_the_pools_well_known_endpoint() -> None:
    assert cognito_jwks_url("eu-west-1", "eu-west-1_Abc") == (
        "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_Abc/.well-known/jwks.json"
    )
