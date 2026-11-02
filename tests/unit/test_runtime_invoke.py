# SPDX-License-Identifier: MIT
"""Tests for `scripts/smoke/runtime_invoke.py`: the inbound-deny reader and token-claims check."""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError
from jwt.utils import base64url_encode
from scripts.smoke.harness import context_for_test
from scripts.smoke.runtime_invoke import is_explicit_inbound_deny, verified_access_claims
from scripts.smoke.verified_claims import ClaimsVerificationError

from tests.unit import _jwks_test_key as test_key

_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r"

_EXPLICIT_DENY_MESSAGE = (
    "User: operator is not authorized to perform: "
    "bedrock-agentcore:InvokeAgentRuntime on resource: arn:aws:bedrock-agentcore:us-east-1:"
    "123456789012:runtime/r with an explicit deny in a resource-based policy"
)


def _access_denied(message: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": message}}, "InvokeAgentRuntime"
    )


# -- is_explicit_inbound_deny --------------------------------------------------------------------


def test_is_explicit_inbound_deny_reads_the_code_and_the_message() -> None:
    assert is_explicit_inbound_deny(_access_denied(_EXPLICIT_DENY_MESSAGE))
    assert not is_explicit_inbound_deny(_access_denied("not authorized"))
    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "with an explicit deny"}},
        "InvokeAgentRuntime",
    )
    assert not is_explicit_inbound_deny(throttled)
    assert not is_explicit_inbound_deny(ValueError(_EXPLICIT_DENY_MESSAGE))


@pytest.mark.parametrize(
    "policy_type",
    [
        "an identity-based policy",
        "a service control policy",
        "a permissions boundary",
        "a session policy",
    ],
)
def test_an_explicit_deny_from_another_policy_type_is_not_the_runtimes(policy_type: str) -> None:
    message = _EXPLICIT_DENY_MESSAGE.replace("a resource-based policy", policy_type)
    assert not is_explicit_inbound_deny(_access_denied(message))


def test_a_resource_policy_deny_naming_another_resource_is_not_the_runtimes_deny() -> None:
    other = _EXPLICIT_DENY_MESSAGE.replace(_RUNTIME_ARN, _RUNTIME_ARN.replace("runtime/r", "x/y"))
    assert is_explicit_inbound_deny(_access_denied(other))
    assert not is_explicit_inbound_deny(_access_denied(other), runtime_arn=_RUNTIME_ARN)
    assert is_explicit_inbound_deny(
        _access_denied(_EXPLICIT_DENY_MESSAGE), runtime_arn=_RUNTIME_ARN
    )


def test_the_runtime_arn_match_is_case_insensitive_and_accepts_an_endpoint_arn() -> None:
    endpoint = _EXPLICIT_DENY_MESSAGE.replace("runtime/r", "runtime/r/runtime-endpoint/DEFAULT")
    assert is_explicit_inbound_deny(_access_denied(endpoint), runtime_arn=_RUNTIME_ARN.upper())


# -- verified_access_claims ----------------------------------------------------------------------

_CLAIMS: dict[str, Any] = test_key.access_token_claims(
    **{"sub": "u-1", "custom:rep_id": "rep-x", "custom:tenant": "eval-smoke-x"}
)


def _ctx(**outputs: str) -> Any:
    return context_for_test(
        outputs={
            "cognito_user_pool_id": test_key.USER_POOL_ID,
            "cognito_spa_client_id": test_key.CLIENT_ID,
            **outputs,
        }
    )


def test_a_verified_access_token_yields_its_claims() -> None:
    claims = verified_access_claims(_ctx(), test_key.sign(_CLAIMS), jwks_url=test_key.jwks_url())
    assert claims["custom:tenant"] == "eval-smoke-x"
    assert claims["custom:rep_id"] == "rep-x"


def _alg_none(claims: dict[str, Any]) -> str:
    segments = ({"alg": "none", "typ": "JWT"}, claims)
    return ".".join(base64url_encode(json.dumps(s).encode()).decode() for s in segments) + "."


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(test_key.TestKey().sign(_CLAIMS), id="foreign-key"),
        pytest.param(_alg_none(_CLAIMS), id="alg-none"),
        pytest.param(test_key.id_token(), id="id-token"),
    ],
)
def test_an_unverifiable_token_is_refused(token: str) -> None:
    with pytest.raises(ClaimsVerificationError):
        verified_access_claims(_ctx(), token, jwks_url=test_key.jwks_url())


def test_a_token_for_another_app_client_is_refused() -> None:
    with pytest.raises(ClaimsVerificationError):
        verified_access_claims(
            _ctx(cognito_spa_client_id="another-client"),
            test_key.sign(_CLAIMS),
            jwks_url=test_key.jwks_url(),
        )
