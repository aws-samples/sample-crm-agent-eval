# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.mfa_user`.

Drives `botocore.stub.Stubber` against a real `cognito-idp` client; no AWS call is made. The tests
pin the auth factor the provisioner reports (`BareMfaUser.auth_factor`) on both the password-only
and the MFA-challenge branch. Test passwords and TOTP secrets are assembled at runtime.
"""

from __future__ import annotations

import base64
import secrets

import boto3
import pytest
from botocore.stub import ANY, Stubber
from scripts.smoke.cognito_claims import totp_code
from scripts.smoke.harness import context_for_test
from scripts.smoke.mfa_user import LiveBareMfaUserProvisioner

_USER_POOL_ID = "us-east-1_abc123"
_SPA_CLIENT_ID = "spa-client-id"
_USERNAME = "11111111-2222-3333-4444-5555dddd6666"
_ACCESS_TOKEN = "opaque-access-token"  # nosec B105  # fake access-token fixture, not a credential
_CLOCK = 1_000_000.0


def _totp_secret() -> str:
    """A base32 TOTP secret, assembled at runtime — the shape `associate_software_token` returns."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _outputs() -> dict[str, str]:
    return {"cognito_user_pool_id": _USER_POOL_ID, "cognito_spa_client_id": _SPA_CLIENT_ID}


def _provisioner_with(stubber: Stubber, client: object) -> LiveBareMfaUserProvisioner:
    ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
    return LiveBareMfaUserProvisioner(ctx, clock=lambda: _CLOCK)


def _stub_user_creation(stubber: Stubber) -> None:
    stubber.add_response(
        "admin_create_user",
        {"User": {"Username": _USERNAME}},
        {
            "UserPoolId": _USER_POOL_ID,
            "Username": ANY,
            "MessageAction": "SUPPRESS",
            "UserAttributes": ANY,
        },
    )
    stubber.add_response(
        "admin_set_user_password",
        {},
        {
            "UserPoolId": _USER_POOL_ID,
            "Username": _USERNAME,
            "Password": ANY,
            "Permanent": True,
        },
    )


def test_a_pool_that_issues_no_challenge_reports_password_only_not_mfa() -> None:
    """The normal path under `MfaConfiguration = "OPTIONAL"`: no `MFA_SETUP` challenge arrives."""
    client = boto3.client("cognito-idp", region_name="us-east-1")
    stubber = Stubber(client)
    _stub_user_creation(stubber)
    stubber.add_response(
        "admin_initiate_auth",
        {"AuthenticationResult": {"AccessToken": _ACCESS_TOKEN}},
        {
            "UserPoolId": _USER_POOL_ID,
            "ClientId": _SPA_CLIENT_ID,
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": ANY,
        },
    )

    with stubber:
        user = _provisioner_with(stubber, client).provision()

    assert user.username == _USERNAME
    assert user.access_token == _ACCESS_TOKEN
    assert "password-only" in user.auth_factor
    assert "MFA-completed" not in user.auth_factor


def test_a_pool_that_does_challenge_reports_mfa_completed_with_a_real_totp_code() -> None:
    """The `MfaConfiguration = "ON"` path: the challenge is answered, so the factor is established."""
    secret = _totp_secret()
    client = boto3.client("cognito-idp", region_name="us-east-1")
    stubber = Stubber(client)
    _stub_user_creation(stubber)
    stubber.add_response(
        "admin_initiate_auth",
        {"ChallengeName": "MFA_SETUP", "Session": "cognito-session-value-10000000000"},
        {
            "UserPoolId": _USER_POOL_ID,
            "ClientId": _SPA_CLIENT_ID,
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": ANY,
        },
    )
    stubber.add_response(
        "associate_software_token",
        {"SecretCode": secret, "Session": "cognito-session-value-20000000000"},
        {"Session": "cognito-session-value-10000000000"},
    )
    stubber.add_response(
        "verify_software_token",
        {"Status": "SUCCESS", "Session": "cognito-session-value-30000000000"},
        {
            "Session": "cognito-session-value-20000000000",
            # A code derived from the secret the pool just handed out, never a fixed literal.
            "UserCode": totp_code(secret, timestamp=_CLOCK),
            "FriendlyDeviceName": "crm-eval-smoke",
        },
    )
    stubber.add_response(
        "admin_respond_to_auth_challenge",
        {"AuthenticationResult": {"AccessToken": _ACCESS_TOKEN}},
        {
            "UserPoolId": _USER_POOL_ID,
            "ClientId": _SPA_CLIENT_ID,
            "ChallengeName": "MFA_SETUP",
            "Session": "cognito-session-value-30000000000",
            "ChallengeResponses": {"USERNAME": _USERNAME},
        },
    )

    with stubber:
        user = _provisioner_with(stubber, client).provision()

    assert user.access_token == _ACCESS_TOKEN
    assert user.auth_factor == "MFA-completed"


def test_an_unexpected_challenge_is_refused_rather_than_the_pool_weakened() -> None:
    client = boto3.client("cognito-idp", region_name="us-east-1")
    stubber = Stubber(client)
    _stub_user_creation(stubber)
    stubber.add_response(
        "admin_initiate_auth",
        {"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "cognito-session-value-10000000000"},
        {
            "UserPoolId": _USER_POOL_ID,
            "ClientId": _SPA_CLIENT_ID,
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": ANY,
        },
    )

    with stubber, pytest.raises(RuntimeError, match="NEW_PASSWORD_REQUIRED"):
        _provisioner_with(stubber, client).provision()


def test_a_pool_that_issues_no_access_token_fails_with_an_actionable_message() -> None:
    client = boto3.client("cognito-idp", region_name="us-east-1")
    stubber = Stubber(client)
    _stub_user_creation(stubber)
    stubber.add_response(
        "admin_initiate_auth",
        {"AuthenticationResult": {}},
        {
            "UserPoolId": _USER_POOL_ID,
            "ClientId": _SPA_CLIENT_ID,
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": ANY,
        },
    )

    with stubber, pytest.raises(RuntimeError, match="no AccessToken issued"):
        _provisioner_with(stubber, client).provision()
