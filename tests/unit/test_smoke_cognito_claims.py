# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.cognito_claims`, using `Stubber` on a `cognito-idp` client.

Test credentials are assembled at runtime by `_password()`. Issued tokens are RS256-signed by
`tests.unit._jwks_test_key` and verified against its loopback JWKS document.
"""

from __future__ import annotations

import functools
import secrets
from collections.abc import Mapping
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from scripts.smoke import cognito_claims
from scripts.smoke.cognito_claims import (
    RESOURCE_CLASS,
    LiveSmokeUserProvisioner,
    ProvisionedSmokeUser,
    UnsignedSignUpClient,
    _check_account_recovery,
    _check_admin_create_user_only,
    _check_full_visibility_is_boolean_like,
    _check_mfa_configuration,
    _check_password_policy,
    _check_pre_token_generation_v2,
    _check_tenant_claim_is_disposable,
    _decode_id_token,
    _live_decode_for,
    _require_disposable_tenant,
    auth_factor_detail,
    checks,
    totp_code,
)
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.verified_claims import ClaimsVerificationError

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.unit import _jwks_test_key as test_key

_USER_POOL_ID = test_key.USER_POOL_ID
_SPA_CLIENT_ID = test_key.CLIENT_ID


def _password() -> str:
    """Assemble a throwaway test password at runtime rather than a literal."""
    return "Tt-" + secrets.token_urlsafe(12)


def _idp_client() -> tuple[Any, Stubber]:
    client = boto3.client("cognito-idp", region_name="us-east-1")
    return client, Stubber(client)


def _compliant_pool(**overrides: Any) -> dict[str, Any]:
    pool: dict[str, Any] = {
        "Id": _USER_POOL_ID,
        "MfaConfiguration": "ON",
        "Policies": {
            "PasswordPolicy": {
                "MinimumLength": 14,
                "RequireUppercase": True,
                "RequireLowercase": True,
                "RequireNumbers": True,
                "RequireSymbols": True,
            }
        },
        "AccountRecoverySetting": {
            "RecoveryMechanisms": [{"Name": "verified_email", "Priority": 1}]
        },
        "LambdaConfig": {
            "PreTokenGenerationConfig": {
                "LambdaArn": "arn:aws:lambda:us-east-1:123456789012:function:pre-token-gen",
                "LambdaVersion": "V2_0",
            }
        },
        "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
    }
    pool.update(overrides)
    return pool


def _spa_client(*, enable_token_revocation: object = True) -> dict[str, Any]:
    client: dict[str, Any] = {"UserPoolId": _USER_POOL_ID, "ClientId": _SPA_CLIENT_ID}
    if enable_token_revocation is not _OMIT:
        client["EnableTokenRevocation"] = enable_token_revocation
    return {"UserPoolClient": client}


_OMIT = object()


def _add_pool(
    stubber: Stubber, pool: Mapping[str, Any], *, spa_client: Mapping[str, Any] | None = None
) -> None:
    """Queue `describe_user_pool` then the SPA client's `describe_user_pool_client`, in call order."""
    stubber.add_response("describe_user_pool", {"UserPool": dict(pool)}, {"UserPoolId": ANY})
    stubber.add_response(
        "describe_user_pool_client",
        dict(spa_client) if spa_client is not None else _spa_client(),
        {"UserPoolId": _USER_POOL_ID, "ClientId": _SPA_CLIENT_ID},
    )


def _outputs() -> dict[str, str]:
    return {"cognito_user_pool_id": _USER_POOL_ID, "cognito_spa_client_id": _SPA_CLIENT_ID}


def _run_checks_with_describe(
    pool: Mapping[str, Any], *, username: str = "", password: str = ""
) -> list[Any]:
    client, stubber = _idp_client()
    _add_pool(stubber, dict(pool))
    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        return list(checks(ctx, username=username, password=password))


# ---------------------------------------------------------------------------
# Pool-configuration checks: non-vacuous per field.
# ---------------------------------------------------------------------------


def test_compliant_pool_passes_all_config_checks() -> None:
    results = _run_checks_with_describe(_compliant_pool())
    by_check = {r.check: r for r in results}
    assert by_check["mfa_configuration"].status == "pass"
    assert by_check["password_policy"].status == "pass"
    assert by_check["account_recovery"].status == "pass"
    assert by_check["pre_token_generation_v2"].status == "pass"
    assert by_check["admin_create_user_only"].status == "pass"


def test_mfa_off_fails() -> None:
    result = _check_mfa_configuration(_compliant_pool(MfaConfiguration="OFF"))
    assert result.status == "fail"
    assert "OFF" in result.detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MinimumLength", 6),
        ("RequireUppercase", False),
        ("RequireLowercase", False),
        ("RequireNumbers", False),
        ("RequireSymbols", False),
    ],
)
def test_weak_password_policy_fails_per_field(field: str, value: Any) -> None:
    pool = _compliant_pool()
    pool["Policies"]["PasswordPolicy"][field] = value
    result = _check_password_policy(pool)
    assert result.status == "fail"


def test_password_policy_missing_entirely_fails() -> None:
    pool = _compliant_pool()
    pool["Policies"] = {}
    result = _check_password_policy(pool)
    assert result.status == "fail"


def test_account_recovery_missing_email_mechanism_fails() -> None:
    pool = _compliant_pool()
    pool["AccountRecoverySetting"] = {"RecoveryMechanisms": [{"Name": "admin_only", "Priority": 1}]}
    result = _check_account_recovery(pool)
    assert result.status == "fail"


def test_account_recovery_absent_fails() -> None:
    pool = _compliant_pool()
    del pool["AccountRecoverySetting"]
    result = _check_account_recovery(pool)
    assert result.status == "fail"


def test_pre_token_generation_v1_legacy_fails_naming_what_was_found() -> None:
    pool = _compliant_pool()
    pool["LambdaConfig"] = {
        "PreTokenGeneration": "arn:aws:lambda:us-east-1:123456789012:function:legacy-v1"
    }
    result = _check_pre_token_generation_v2(pool)
    assert result.status == "fail"
    assert "legacy-v1" in result.detail
    assert "V1" in result.detail or "PreTokenGeneration" in result.detail


def test_pre_token_generation_v2_with_wrong_lambda_version_fails() -> None:
    pool = _compliant_pool()
    pool["LambdaConfig"] = {
        "PreTokenGenerationConfig": {
            "LambdaArn": "arn:aws:lambda:us-east-1:123456789012:function:pre-token-gen",
            "LambdaVersion": "V1_0",
        }
    }
    result = _check_pre_token_generation_v2(pool)
    assert result.status == "fail"
    assert "V1_0" in result.detail


def test_pre_token_generation_missing_entirely_fails() -> None:
    pool = _compliant_pool()
    pool["LambdaConfig"] = {}
    result = _check_pre_token_generation_v2(pool)
    assert result.status == "fail"


def test_admin_create_user_only_true_passes() -> None:
    assert _check_admin_create_user_only(_compliant_pool()).status == "pass"


@pytest.mark.parametrize("value", [False, "true", 1])
def test_admin_create_user_only_anything_but_true_fails(value: Any) -> None:
    pool = _compliant_pool(AdminCreateUserConfig={"AllowAdminCreateUserOnly": value})
    result = _check_admin_create_user_only(pool)
    assert result.status == "fail"
    assert repr(value) in result.detail


@pytest.mark.parametrize("config", [None, {}])
def test_admin_create_user_only_missing_is_a_fail_not_a_skip(config: Any) -> None:
    pool = _compliant_pool()
    if config is None:
        del pool["AdminCreateUserConfig"]
    else:
        pool["AdminCreateUserConfig"] = config
    result = _check_admin_create_user_only(pool)
    assert result.status == "fail"
    assert "absent" in result.detail


# ---------------------------------------------------------------------------
# Self sign-up probe: a real unauthenticated SignUp must be refused by error code, and an accepted
# one must be cleaned up and confirmed gone.
# ---------------------------------------------------------------------------


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "fake"}}, operation)


class _FakeSignUpClient:
    """Records `sign_up` calls; raises the configured error code, or accepts."""

    def __init__(self, *, error_code: str | None = None) -> None:
        self._error_code = error_code
        self.calls: list[dict[str, Any]] = []

    def sign_up(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error_code is not None:
            raise _client_error(self._error_code, "SignUp")
        return {"UserConfirmed": False, "UserSub": "fake-self-signup-sub"}


def _refusing_signup_client() -> _FakeSignUpClient:
    return _FakeSignUpClient(error_code="NotAuthorizedException")


def _run_signup_probe(
    signup_client: _FakeSignUpClient,
    *,
    expect_cleanup: bool,
    user_still_present: bool = False,
    readback_error_code: str = "UserNotFoundException",
) -> list[Any]:
    client, stubber = _idp_client()
    _add_pool(stubber, _compliant_pool())
    if expect_cleanup:
        stubber.add_response(
            "admin_delete_user", {}, {"UserPoolId": _USER_POOL_ID, "Username": ANY}
        )
        if user_still_present:
            stubber.add_response(
                "admin_get_user",
                {"Username": "still-here", "UserAttributes": []},
                {"UserPoolId": _USER_POOL_ID, "Username": ANY},
            )
        else:
            stubber.add_client_error(
                "admin_get_user",
                service_error_code=readback_error_code,
                expected_params={"UserPoolId": _USER_POOL_ID, "Username": ANY},
            )
    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        results = list(checks(ctx, signup_client=signup_client))
        stubber.assert_no_pending_responses()
    return results


def test_self_signup_refused_with_not_authorized_passes() -> None:
    signup = _refusing_signup_client()
    results = _run_signup_probe(signup, expect_cleanup=False)
    by_check = {r.check: r for r in results}
    assert by_check["self_signup_refused"].status == "pass"
    assert "NotAuthorizedException" in by_check["self_signup_refused"].detail
    assert "self_signup_user_deleted" not in by_check
    (call,) = signup.calls
    assert call["ClientId"] == _SPA_CLIENT_ID
    assert call["Username"].startswith("selfsignup-smoke-")
    assert call["Username"].endswith("@example.invalid")


def test_self_signup_uses_a_fresh_username_and_a_policy_satisfying_password() -> None:
    first, second = _refusing_signup_client(), _refusing_signup_client()
    _run_signup_probe(first, expect_cleanup=False)
    _run_signup_probe(second, expect_cleanup=False)
    assert first.calls[0]["Username"] != second.calls[0]["Username"]
    password = first.calls[0]["Password"]
    assert len(password) >= 14
    assert any(c.isupper() for c in password)
    assert any(c.islower() for c in password)
    assert any(c.isdigit() for c in password)
    assert any(not c.isalnum() for c in password)


def test_self_signup_accepted_fails_and_deletes_and_confirms_the_user_gone() -> None:
    signup = _FakeSignUpClient()
    results = _run_signup_probe(signup, expect_cleanup=True)
    by_check = {r.check: r for r in results}
    assert by_check["self_signup_refused"].status == "fail"
    assert "ACCEPTED" in by_check["self_signup_refused"].detail
    assert by_check["self_signup_user_deleted"].status == "pass"
    assert "UserNotFoundException" in by_check["self_signup_user_deleted"].detail


def test_self_signup_accepted_and_user_still_resolvable_fails_cleanup() -> None:
    results = _run_signup_probe(_FakeSignUpClient(), expect_cleanup=True, user_still_present=True)
    by_check = {r.check: r for r in results}
    assert by_check["self_signup_refused"].status == "fail"
    assert by_check["self_signup_user_deleted"].status == "fail"
    assert "still resolves" in by_check["self_signup_user_deleted"].detail


@pytest.mark.parametrize(
    "code", ["AccessDeniedException", "ResourceNotFoundException", "TooManyRequestsException"]
)
def test_self_signup_readback_error_other_than_user_not_found_fails_naming_the_code(
    code: str,
) -> None:
    """A read-back that errors for any other reason has not shown the user is gone."""
    results = _run_signup_probe(_FakeSignUpClient(), expect_cleanup=True, readback_error_code=code)
    by_check = {r.check: r for r in results}
    assert by_check["self_signup_refused"].status == "fail"
    assert by_check["self_signup_user_deleted"].status == "fail"
    assert code in by_check["self_signup_user_deleted"].detail


@pytest.mark.parametrize(
    "code", ["InvalidParameterException", "InvalidPasswordException", "ResourceNotFoundException"]
)
def test_self_signup_other_error_code_is_a_fail_naming_the_code(code: str) -> None:
    """An unrelated error must never read as a refusal: that would pass for the wrong reason."""
    client, stubber = _idp_client()
    _add_pool(stubber, _compliant_pool())
    stubber.add_client_error(
        "admin_delete_user",
        service_error_code="UserNotFoundException",
        expected_params={"UserPoolId": _USER_POOL_ID, "Username": ANY},
    )
    stubber.add_client_error(
        "admin_get_user",
        service_error_code="UserNotFoundException",
        expected_params={"UserPoolId": _USER_POOL_ID, "Username": ANY},
    )
    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        results = list(checks(ctx, signup_client=_FakeSignUpClient(error_code=code)))
        stubber.assert_no_pending_responses()
    by_check = {r.check: r for r in results}
    assert by_check["self_signup_refused"].status == "fail"
    assert code in by_check["self_signup_refused"].detail
    assert by_check["self_signup_user_deleted"].status == "pass"
    assert "nothing to delete" in by_check["self_signup_user_deleted"].detail


def test_self_signup_non_client_error_is_a_fail_naming_the_exception() -> None:
    class _Exploding:
        def sign_up(self, **_kwargs: Any) -> Any:
            raise TimeoutError("read timed out")

    client, stubber = _idp_client()
    _add_pool(stubber, _compliant_pool())
    stubber.add_response("admin_delete_user", {}, {"UserPoolId": _USER_POOL_ID, "Username": ANY})
    stubber.add_client_error(
        "admin_get_user",
        service_error_code="UserNotFoundException",
        expected_params={"UserPoolId": _USER_POOL_ID, "Username": ANY},
    )
    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        results = list(checks(ctx, signup_client=_Exploding()))
    by_check = {r.check: r for r in results}
    assert by_check["self_signup_refused"].status == "fail"
    assert "TimeoutError" in by_check["self_signup_refused"].detail
    assert by_check["self_signup_user_deleted"].status == "pass"


def test_no_signup_client_reports_skip_never_pass() -> None:
    by_check = {r.check: r for r in _run_checks_with_describe(_compliant_pool())}
    assert by_check["self_signup_refused"].status == "skip"


def test_unsigned_signup_client_builds_no_client_until_called() -> None:
    probe = UnsignedSignUpClient(context_for_test(outputs=_outputs()))
    assert probe._client is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Token-decode assertions, per claim.
# ---------------------------------------------------------------------------

_COMPLETE_CLAIMS: dict[str, Any] = {
    "custom:rep_id": "rep-ashford",
    "custom:book": "acct-001,acct-002",
    "custom:full_visibility": "false",
}


def _checks_with_token(
    claims: Mapping[str, Any],
    *,
    expected_rep_id: str = "",
    expected_book: frozenset[str] = frozenset(),
) -> list[Any]:
    client, stubber = _idp_client()
    _add_pool(stubber, _compliant_pool())
    password = _password()
    stubber.add_response(
        "admin_initiate_auth",
        {"AuthenticationResult": {"IdToken": "opaque-test-token"}},
        {
            "UserPoolId": _USER_POOL_ID,
            "ClientId": _SPA_CLIENT_ID,
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": {"USERNAME": "smoke-user@example.test", "PASSWORD": password},
        },
    )
    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        return list(
            checks(
                ctx,
                username="smoke-user@example.test",
                password=password,
                expected_rep_id=expected_rep_id,
                expected_book=expected_book,
                decode_id_token=lambda _token: dict(claims),
            )
        )


def test_complete_token_passes_every_claim_check() -> None:
    results = _checks_with_token(
        _COMPLETE_CLAIMS,
        expected_rep_id="rep-ashford",
        expected_book=frozenset({"acct-001", "acct-002"}),
    )
    by_check = {r.check: r for r in results}
    assert by_check["claim_present:custom:rep_id"].status == "pass"
    assert by_check["claim_present:custom:book"].status == "pass"
    assert by_check["claim_present:custom:full_visibility"].status == "pass"
    assert by_check["full_visibility_boolean_like"].status == "pass"
    assert by_check["book_matches_seeded_rep"].status == "pass"


@pytest.mark.parametrize(
    "missing_claim", ["custom:rep_id", "custom:book", "custom:full_visibility"]
)
def test_token_missing_each_claim_individually_is_detected(missing_claim: str) -> None:
    """A token missing one required claim fails that claim's check while the others pass."""
    claims = dict(_COMPLETE_CLAIMS)
    del claims[missing_claim]
    results = _checks_with_token(claims)
    by_check = {r.check: r for r in results}
    target = by_check[f"claim_present:{missing_claim}"]
    assert target.status == "fail"
    assert missing_claim in target.detail

    # The other claim-presence checks still run and pass.
    other_claims = {"custom:rep_id", "custom:book", "custom:full_visibility"} - {missing_claim}
    for other in other_claims:
        assert by_check[f"claim_present:{other}"].status == "pass"


@pytest.mark.parametrize("empty_claim", ["custom:rep_id", "custom:book", "custom:full_visibility"])
def test_token_with_present_but_empty_claim_is_detected(empty_claim: str) -> None:
    claims = dict(_COMPLETE_CLAIMS)
    claims[empty_claim] = ""
    results = _checks_with_token(claims)
    by_check = {r.check: r for r in results}
    target = by_check[f"claim_present:{empty_claim}"]
    assert target.status == "fail"
    assert "empty" in target.detail


def test_full_visibility_non_boolean_string_is_flagged_not_silently_falsy() -> None:
    """A `custom:full_visibility` value that is not a recognised boolean string fails."""
    claims = dict(_COMPLETE_CLAIMS)
    claims["custom:full_visibility"] = "maybe"
    result = _check_full_visibility_is_boolean_like(claims)
    assert result.status == "fail"
    assert "maybe" in result.detail


def test_full_visibility_recognised_falsy_string_passes() -> None:
    claims = dict(_COMPLETE_CLAIMS)
    claims["custom:full_visibility"] = "false"
    result = _check_full_visibility_is_boolean_like(claims)
    assert result.status == "pass"


def test_book_mismatch_against_seeded_rep_fails() -> None:
    results = _checks_with_token(
        _COMPLETE_CLAIMS,
        expected_rep_id="rep-ashford",
        expected_book=frozenset({"acct-999"}),
    )
    by_check = {r.check: r for r in results}
    target = by_check["book_matches_seeded_rep"]
    assert target.status == "fail"
    assert "acct-999" in target.detail


def test_book_matches_when_rep_id_itself_mismatches_fails() -> None:
    results = _checks_with_token(
        _COMPLETE_CLAIMS,
        expected_rep_id="rep-brixton",
        expected_book=frozenset({"acct-001", "acct-002"}),
    )
    by_check = {r.check: r for r in results}
    target = by_check["book_matches_seeded_rep"]
    assert target.status == "fail"
    assert "rep-brixton" in target.detail


def test_no_expected_rep_supplied_skips_book_check() -> None:
    results = _checks_with_token(_COMPLETE_CLAIMS)
    by_check = {r.check: r for r in results}
    assert by_check["book_matches_seeded_rep"].status == "skip"


# ---------------------------------------------------------------------------
# No credentials supplied: token checks skip, pool checks still run.
# ---------------------------------------------------------------------------


def test_no_username_password_skips_token_checks_but_runs_pool_checks() -> None:
    results = _run_checks_with_describe(_compliant_pool())
    by_check = {r.check: r for r in results}
    assert by_check["mfa_configuration"].status == "pass"
    assert by_check["issue_and_decode"].status == "skip"


# ---------------------------------------------------------------------------
# Authentication failure surfaces as a single actionable failing check, not a traceback.
# ---------------------------------------------------------------------------


def test_admin_initiate_auth_no_id_token_fails_with_actionable_detail() -> None:
    client, stubber = _idp_client()
    _add_pool(stubber, _compliant_pool())
    password = _password()
    stubber.add_response(
        "admin_initiate_auth",
        {"ChallengeName": "NEW_PASSWORD_REQUIRED", "AuthenticationResult": {}},
        {
            "UserPoolId": _USER_POOL_ID,
            "ClientId": _SPA_CLIENT_ID,
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": {"USERNAME": "smoke-user@example.test", "PASSWORD": password},
        },
    )
    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        results = list(checks(ctx, username="smoke-user@example.test", password=password))
    by_check = {r.check: r for r in results}
    assert by_check["authenticate"].status == "fail"
    assert "NEW_PASSWORD_REQUIRED" in by_check["authenticate"].detail


# ---------------------------------------------------------------------------
# `_decode_id_token`: claims are returned only from a token the pool's JWKS verifies.
# ---------------------------------------------------------------------------


def _decode(token: str) -> dict[str, Any]:
    return _decode_id_token(
        token,
        region=test_key.REGION,
        user_pool_id=_USER_POOL_ID,
        client_id=_SPA_CLIENT_ID,
        jwks_url=test_key.jwks_url(),
    )


def test_decode_id_token_returns_the_claims_of_a_verified_token() -> None:
    claims = _decode(test_key.id_token(**_COMPLETE_CLAIMS))
    assert claims["custom:rep_id"] == "rep-ashford"


def test_decode_id_token_refuses_a_token_signed_by_another_key() -> None:
    forged = test_key.TestKey().sign(test_key.id_token_claims(**_COMPLETE_CLAIMS))
    with pytest.raises(ClaimsVerificationError, match="signature"):
        _decode(forged)


def test_decode_id_token_refuses_an_access_token() -> None:
    with pytest.raises(ClaimsVerificationError, match="token_use"):
        _decode(test_key.access_token(aud=_SPA_CLIENT_ID, **_COMPLETE_CLAIMS))


def test_decode_id_token_refuses_another_clients_token() -> None:
    with pytest.raises(ClaimsVerificationError, match="aud"):
        _decode(test_key.id_token(aud="another-client", **_COMPLETE_CLAIMS))


def test_the_live_decoder_verifies_against_the_contexts_pool() -> None:
    ctx = context_for_test(outputs=_outputs())
    decode = _live_decode_for(ctx, jwks_url=test_key.jwks_url())
    assert decode(test_key.id_token(**_COMPLETE_CLAIMS))["custom:rep_id"] == "rep-ashford"
    with pytest.raises(ClaimsVerificationError, match="iss"):
        decode(test_key.id_token(iss="https://cognito-idp.us-east-1.amazonaws.com/other"))


def test_resource_class_constant() -> None:
    assert RESOURCE_CLASS == "cognito_claims"


# ---------------------------------------------------------------------------
# RFC 6238 TOTP, against the RFC's published SHA-1 test vectors.
# ---------------------------------------------------------------------------

#: RFC 6238 Appendix B seed: ASCII "12345678901234567890", base32-encoded.
_RFC6238_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ],
)
def test_totp_code_matches_rfc6238_test_vectors(timestamp: int, expected: str) -> None:
    assert totp_code(_RFC6238_SEED, timestamp=timestamp) == expected


def test_totp_code_changes_across_the_step_boundary() -> None:
    """The TOTP code differs across a 30-second step boundary."""
    inside = totp_code(_RFC6238_SEED, timestamp=30)
    next_step = totp_code(_RFC6238_SEED, timestamp=60)
    assert inside != next_step


def test_totp_code_accepts_an_unpadded_cognito_secret() -> None:
    """An unpadded base32 secret, as `associate_software_token` returns, yields a 6-digit code."""
    unpadded = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZA"
    assert len(totp_code(unpadded.rstrip("="), timestamp=59)) == 6


# ---------------------------------------------------------------------------
# The disposable-tenant guard. `demo` is the trigger's fail-closed default and is refused.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tenant", ["demo", "prod", "", "smoke", "evaluation"])
def test_require_disposable_tenant_refuses_anything_not_smoke_or_eval(tenant: str) -> None:
    with pytest.raises(RuntimeError, match="disposable tenant"):
        _require_disposable_tenant(tenant)


@pytest.mark.parametrize("tenant", ["smoke-abc", "eval-smoke-abc"])
def test_require_disposable_tenant_accepts_a_disposable_tenant(tenant: str) -> None:
    _require_disposable_tenant(tenant)


def test_tenant_claim_of_demo_fails_naming_the_fail_closed_default() -> None:
    result = _check_tenant_claim_is_disposable({"custom:tenant": "demo"}, expected="eval-smoke-x")
    assert result.status == "fail"
    assert "demo" in result.detail


def test_tenant_claim_mismatch_fails() -> None:
    result = _check_tenant_claim_is_disposable(
        {"custom:tenant": "eval-smoke-other"}, expected="eval-smoke-x"
    )
    assert result.status == "fail"
    assert "eval-smoke-x" in result.detail


def test_tenant_claim_matching_the_provisioned_tenant_passes() -> None:
    result = _check_tenant_claim_is_disposable(
        {"custom:tenant": "eval-smoke-x"}, expected="eval-smoke-x"
    )
    assert result.status == "pass"


# ---------------------------------------------------------------------------
# The provisioned path: `issue_and_decode` passes or fails, never skips, and cleanup always runs.
# ---------------------------------------------------------------------------

_PROVISIONED_TENANT = "eval-smoke-deadbeef"
_PROVISIONED_REP = "1111aaaa-2222-bbbb-3333-cccc4444dddd"
_PROVISIONED_BOOK = frozenset(
    {f"acct-{_PROVISIONED_TENANT}-0000", f"acct-{_PROVISIONED_TENANT}-0001"}
)


def _provisioned_claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "custom:rep_id": _PROVISIONED_REP,
        "custom:book": ",".join(sorted(_PROVISIONED_BOOK)),
        "custom:full_visibility": "false",
        "custom:tenant": _PROVISIONED_TENANT,
    }
    claims.update(overrides)
    return claims


#: A stand-in refresh token; the stubbed client never inspects it.
_REFRESH_TOKEN = "refresh-" + "x" * 24


class _FakeProvisioner:
    """A `SmokeUserProvisioner` double: configurable failure, and records that cleanup ran."""

    def __init__(
        self,
        *,
        provision_error: Exception | None = None,
        claims: Mapping[str, Any] | None = None,
        cleanup_status: str = "pass",
        refresh_token: str = _REFRESH_TOKEN,
    ) -> None:
        self._provision_error = provision_error
        self._claims = dict(claims) if claims is not None else _provisioned_claims()
        self._cleanup_status = cleanup_status
        self._refresh_token = refresh_token
        self.cleaned_up = False

    def provision(self) -> ProvisionedSmokeUser:
        if self._provision_error is not None:
            raise self._provision_error
        return ProvisionedSmokeUser(
            username=_PROVISIONED_REP,
            rep_id=_PROVISIONED_REP,
            tenant=_PROVISIONED_TENANT,
            book=_PROVISIONED_BOOK,
            id_token=test_key.id_token(**self._claims),
            detail="MFA-completed ID token issued on attempt 1",
            refresh_token=self._refresh_token,
        )

    def cleanup(self) -> Any:
        self.cleaned_up = True
        yield CheckResult(
            resource="smoke_user",
            check="cognito_user_deleted",
            status=self._cleanup_status,  # type: ignore[arg-type]
            detail="fake cleanup",
        )


def _queue_revocation(
    stubber: Stubber,
    *,
    revoke_error: str | None = None,
    refresh_outcome: str | None = "NotAuthorizedException",
) -> None:
    """Queue `revoke_token` then `initiate_auth REFRESH_TOKEN_AUTH`, as the provisioned path calls
    them. `refresh_outcome=None` answers the refresh with fresh tokens (revocation did not hold)."""
    revoke_params = {"Token": _REFRESH_TOKEN, "ClientId": _SPA_CLIENT_ID}
    if revoke_error is not None:
        stubber.add_client_error(
            "revoke_token", service_error_code=revoke_error, expected_params=revoke_params
        )
        return
    stubber.add_response("revoke_token", {}, revoke_params)
    refresh_params = {
        "AuthFlow": "REFRESH_TOKEN_AUTH",
        "ClientId": _SPA_CLIENT_ID,
        "AuthParameters": {"REFRESH_TOKEN": _REFRESH_TOKEN},
    }
    if refresh_outcome is None:
        stubber.add_response(
            "initiate_auth",
            {"AuthenticationResult": {"IdToken": "still-valid", "AccessToken": "still-valid"}},
            refresh_params,
        )
    else:
        stubber.add_client_error(
            "initiate_auth", service_error_code=refresh_outcome, expected_params=refresh_params
        )


def _checks_with_provisioner(
    provisioner: Any,
    *,
    spa_client: Mapping[str, Any] | None = None,
    revoke_error: str | None = None,
    refresh_outcome: str | None = "NotAuthorizedException",
    queue_revocation: bool = True,
) -> list[Any]:
    client, stubber = _idp_client()
    _add_pool(stubber, _compliant_pool(Domain=_DOMAIN), spa_client=spa_client)
    stubber.add_response(
        "describe_user_pool_client",
        {
            "UserPoolClient": {
                "UserPoolId": _USER_POOL_ID,
                "ClientId": _SPA_CLIENT_ID,
                "LogoutURLs": [_LOGOUT_URL],
            }
        },
        {"UserPoolId": _USER_POOL_ID, "ClientId": _SPA_CLIENT_ID},
    )
    if queue_revocation:
        _queue_revocation(stubber, revoke_error=revoke_error, refresh_outcome=refresh_outcome)
    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        return list(
            checks(
                ctx,
                provisioner=provisioner,
                decode_id_token=_live_decode_for(ctx, jwks_url=test_key.jwks_url()),
                signup_client=_refusing_signup_client(),
                logout_probe=_Probe(),
            )
        )


def test_provisioned_run_reports_issue_and_decode_as_a_real_pass_never_a_skip() -> None:
    provisioner = _FakeProvisioner()
    results = _checks_with_provisioner(provisioner)
    by_check = {r.check: r for r in results}
    assert by_check["issue_and_decode"].status == "pass"
    assert "skip" not in {r.status for r in results}
    assert by_check["tenant_claim_is_disposable"].status == "pass"
    assert by_check["claim_present:custom:rep_id"].status == "pass"
    assert by_check["claim_present:custom:book"].status == "pass"
    assert by_check["claim_present:custom:full_visibility"].status == "pass"
    assert by_check["book_matches_seeded_rep"].status == "pass"
    assert provisioner.cleaned_up


def test_provisioning_failure_is_a_fail_not_a_skip_and_still_cleans_up() -> None:
    """A provisioning failure fails `issue_and_decode` and cleanup still runs."""
    provisioner = _FakeProvisioner(provision_error=RuntimeError("MFA_SETUP never completed"))
    results = _checks_with_provisioner(provisioner, queue_revocation=False)
    by_check = {r.check: r for r in results}
    assert by_check["issue_and_decode"].status == "fail"
    assert "MFA_SETUP never completed" in by_check["issue_and_decode"].detail
    assert provisioner.cleaned_up
    assert by_check["cognito_user_deleted"].status == "pass"


def test_provisioned_token_missing_a_claim_fails_and_still_cleans_up() -> None:
    claims = _provisioned_claims()
    del claims["custom:book"]
    provisioner = _FakeProvisioner(claims=claims)
    results = _checks_with_provisioner(provisioner)
    by_check = {r.check: r for r in results}
    assert by_check["claim_present:custom:book"].status == "fail"
    assert provisioner.cleaned_up


def test_provisioned_token_that_fell_back_to_demo_fails_loudly() -> None:
    provisioner = _FakeProvisioner(claims=_provisioned_claims(**{"custom:tenant": "demo"}))
    results = _checks_with_provisioner(provisioner)
    by_check = {r.check: r for r in results}
    assert by_check["tenant_claim_is_disposable"].status == "fail"
    assert "demo" in by_check["tenant_claim_is_disposable"].detail


def test_a_failing_cleanup_is_reported_as_a_failure_not_swallowed() -> None:
    provisioner = _FakeProvisioner(cleanup_status="fail")
    results = _checks_with_provisioner(provisioner)
    by_check = {r.check: r for r in results}
    assert by_check["cognito_user_deleted"].status == "fail"


def test_undecodable_token_fails_and_still_cleans_up() -> None:
    provisioner = _FakeProvisioner()
    client, stubber = _idp_client()
    _add_pool(stubber, _compliant_pool())

    def explode(_token: str) -> dict[str, Any]:
        raise ValueError("signature verification failed")

    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        results = list(checks(ctx, provisioner=provisioner, decode_id_token=explode))
    by_check = {r.check: r for r in results}
    assert by_check["issue_and_decode"].status == "fail"
    assert "signature verification failed" in by_check["issue_and_decode"].detail
    assert provisioner.cleaned_up


# ---------------------------------------------------------------------------
# The live entry point always supplies a provisioner.
# ---------------------------------------------------------------------------


def test_main_wires_a_live_provisioner_so_a_live_run_cannot_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_main(_argv: Any, **kwargs: Any) -> int:
        ctx = context_for_test(outputs=_outputs())
        monkeypatch.setattr(
            cognito_claims, "checks", lambda _ctx, **inner: captured.update(inner) or []
        )
        kwargs["checks"](ctx)
        return 0

    monkeypatch.delenv("CRM_SMOKE_COGNITO_USERNAME", raising=False)
    monkeypatch.delenv("CRM_SMOKE_COGNITO_PASSWORD", raising=False)
    monkeypatch.setattr(cognito_claims, "run_main", fake_run_main)
    assert cognito_claims.main([]) == 0
    assert isinstance(captured["provisioner"], LiveSmokeUserProvisioner)
    assert isinstance(captured["signup_client"], UnsignedSignUpClient)
    assert not captured["username"]
    # With no injected tenant the provisioner owns the tenant's allocation, seeding and teardown.
    assert captured["provisioner"]._owns_tenant is True  # noqa: SLF001


# ---------------------------------------------------------------------------
# The live provisioner's MFA_SETUP sequence against a stubbed Cognito and a `moto` DynamoDB:
# the call order, and cleanup of the user, its `reps` row and the tenant, each read back.
# ---------------------------------------------------------------------------

_TOTP_SECRET = _RFC6238_SEED


def _stub_mfa_setup_sequence(stubber: Stubber, *, username: str, tenant: str) -> None:
    """Queue the full `admin_create_user` -> ... -> `admin_respond_to_auth_challenge` sequence."""
    stubber.add_response(
        "admin_create_user",
        {"User": {"Username": username}},
        {
            "UserPoolId": ANY,
            "Username": ANY,
            "MessageAction": "SUPPRESS",
            "UserAttributes": ANY,
        },
    )
    stubber.add_response(
        "admin_set_user_password",
        {},
        {"UserPoolId": ANY, "Username": ANY, "Password": ANY, "Permanent": True},
    )
    stubber.add_response(
        "admin_initiate_auth",
        {"ChallengeName": "MFA_SETUP", "Session": "smoke-session-token-1-padding"},
        {"UserPoolId": ANY, "ClientId": ANY, "AuthFlow": ANY, "AuthParameters": ANY},
    )
    stubber.add_response(
        "associate_software_token",
        {"SecretCode": _TOTP_SECRET, "Session": "smoke-session-token-2-padding"},
        {"Session": "smoke-session-token-1-padding"},
    )
    stubber.add_response(
        "verify_software_token",
        {"Status": "SUCCESS", "Session": "smoke-session-token-3-padding"},
        {"Session": "smoke-session-token-2-padding", "UserCode": ANY, "FriendlyDeviceName": ANY},
    )
    token = test_key.id_token(
        **{
            "custom:rep_id": username,
            "custom:book": f"acct-{tenant}-0000,acct-{tenant}-0001",
            "custom:full_visibility": "false",
            "custom:tenant": tenant,
        }
    )
    stubber.add_response(
        "admin_respond_to_auth_challenge",
        {"AuthenticationResult": {"IdToken": token}},
        {
            "UserPoolId": ANY,
            "ClientId": ANY,
            "ChallengeName": "MFA_SETUP",
            "Session": "smoke-session-token-3-padding",
            "ChallengeResponses": ANY,
        },
    )


def test_live_provisioner_completes_mfa_setup_and_cleans_up_every_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3 as real_boto3
    from moto import mock_aws
    from scripts.smoke.tenant import allocate_smoke_tenant as real_allocate

    with mock_aws():
        dynamodb = real_boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(dynamodb)
        idp, stubber = _idp_client()
        ctx = context_for_test(
            outputs={**_outputs(), "dynamodb_table_names": {"eval_runs": "eval_runs"}},
            clients={"cognito-idp": idp, "dynamodb": dynamodb},
        )
        provisioner = LiveSmokeUserProvisioner(
            ctx, clock=lambda: 59.0, sleeper=lambda _s: None, jwks_url=test_key.jwks_url()
        )

        # `provision` allocates the tenant before any Cognito call, so the Cognito stubs are
        # queued from inside the allocation, once the tenant is known.
        smoke_tenant_holder: dict[str, str] = {}

        def recording_allocate(store: Any) -> Any:
            allocated = real_allocate(store)
            smoke_tenant_holder["tenant"] = allocated.tenant
            _stub_mfa_setup_sequence(stubber, username=_PROVISIONED_REP, tenant=allocated.tenant)
            stubber.activate()
            return allocated

        monkeypatch.setattr(cognito_claims, "allocate_smoke_tenant", recording_allocate)
        provisioned = provisioner.provision()

        tenant = smoke_tenant_holder["tenant"]
        assert provisioned.tenant == tenant
        assert tenant.startswith("eval-smoke-")
        assert provisioned.rep_id == _PROVISIONED_REP
        assert provisioned.book == {f"acct-{tenant}-0000", f"acct-{tenant}-0001"}

        store = Store(client=dynamodb)
        rep_row = store.get_item("reps", {"rep_id": _PROVISIONED_REP})
        assert rep_row is not None
        assert rep_row["tenant"] == tenant
        assert rep_row["full_visibility"] is False

        stubber.deactivate()
        stubber.add_response("admin_delete_user", {}, {"UserPoolId": ANY, "Username": ANY})
        stubber.add_client_error(
            "admin_get_user", service_error_code="UserNotFoundException", http_status_code=400
        )
        with stubber:
            cleanup_results = list(provisioner.cleanup())

    by_check = {r.check: r for r in cleanup_results}
    assert by_check["cognito_user_deleted"].status == "pass"
    assert by_check["reps_row_deleted"].status == "pass"
    assert by_check["tenant_left_clean"].status == "pass"


@pytest.mark.parametrize("forged", [False, True])
def test_live_provisioner_reads_the_tenant_only_from_a_verified_token(
    monkeypatch: pytest.MonkeyPatch, forged: bool
) -> None:
    """The tenant claim gating the auth retry is read only after signature verification."""
    claims = test_key.id_token_claims(**{"custom:tenant": _PROVISIONED_TENANT})
    signer = test_key.TestKey() if forged else test_key.default_key()
    token = signer.sign(claims)
    provisioner = LiveSmokeUserProvisioner(
        context_for_test(outputs=_outputs()), jwks_url=test_key.jwks_url()
    )
    monkeypatch.setattr(provisioner, "_one_auth_attempt", lambda **_kwargs: (token, ""))
    authenticate = functools.partial(
        provisioner._authenticate_with_mfa,  # noqa: SLF001
        username=_PROVISIONED_REP,
        password=_password(),
        expected_tenant=_PROVISIONED_TENANT,
    )
    if forged:
        with pytest.raises(ClaimsVerificationError, match="signature"):
            authenticate()
    else:
        assert authenticate()[0] == token


def test_live_provisioner_reports_a_still_resolvable_user_as_a_cleanup_failure() -> None:
    """Cleanup fails when `admin_get_user` still resolves the user after `admin_delete_user`."""
    idp, stubber = _idp_client()
    ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": idp})
    provisioner = LiveSmokeUserProvisioner(ctx)
    provisioner._username = _PROVISIONED_REP  # noqa: SLF001 - exercising cleanup in isolation
    stubber.add_response("admin_delete_user", {}, {"UserPoolId": ANY, "Username": ANY})
    stubber.add_response(
        "admin_get_user",
        {"Username": _PROVISIONED_REP, "UserAttributes": []},
        {"UserPoolId": ANY, "Username": ANY},
    )
    with stubber:
        results = list(provisioner._cleanup_cognito_user())  # noqa: SLF001
    assert results[0].status == "fail"
    assert "still resolves" in results[0].detail


def test_live_provisioner_refuses_a_book_naming_an_unseeded_account() -> None:
    """`_seeded_book` raises when the tenant's book accounts were never seeded."""
    import boto3 as real_boto3
    from moto import mock_aws

    with mock_aws():
        dynamodb = real_boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(dynamodb)
        ctx = context_for_test(
            outputs={**_outputs(), "dynamodb_table_names": {"eval_runs": "eval_runs"}},
            clients={"dynamodb": dynamodb},
        )
        provisioner = LiveSmokeUserProvisioner(ctx)
        with pytest.raises(RuntimeError, match="not found after seeding"):
            provisioner._seeded_book(Store(client=dynamodb), "eval-smoke-never-seeded")  # noqa: SLF001


class TestAuthFactorDetail:
    """`auth_factor_detail` claims MFA only when a challenge was answered."""

    def test_an_answered_challenge_is_reported_as_mfa_completed(self) -> None:
        assert auth_factor_detail(mfa_completed=True) == "MFA-completed"

    def test_no_challenge_is_reported_as_password_only_and_never_as_mfa(self) -> None:
        detail = auth_factor_detail(mfa_completed=False)
        assert "password-only" in detail
        assert "MFA-completed" not in detail


class _StopAfterCreateError(Exception):
    """Raised by the fake IdP once it has recorded the create, so `provision` goes no further."""


class _CollidingIdp:
    """Raises `UsernameExistsException` for a repeated Username, as Cognito does."""

    def __init__(self) -> None:
        self.usernames: list[str] = []

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        username = kwargs["Username"]
        if username in self.usernames:
            raise RuntimeError("UsernameExistsException: User account already exists")
        self.usernames.append(username)
        raise _StopAfterCreateError


def test_two_provisioners_on_one_injected_tenant_create_distinct_users() -> None:
    """`user_settings_api` builds two users on one shared tenant in the same run."""
    from scripts.smoke.tenant import SmokeTenant

    idp = _CollidingIdp()
    shared = SmokeTenant(run_id="0123456789abcdef", tenant="eval-smoke-0123456789abcdef")

    class _Offline(LiveSmokeUserProvisioner):
        def _store(self) -> Any:
            return None

        def _seeded_book(self, store: Any, tenant: str) -> frozenset[str]:
            return frozenset({f"acct-{tenant}-0000"})

        def _idp(self) -> Any:
            return idp

        def _user_pool_id(self) -> str:
            return "us-east-1_pool"

    ctx = context_for_test(outputs=_outputs(), clients={})
    for _ in range(2):
        with pytest.raises(_StopAfterCreateError):
            _Offline(ctx, smoke_tenant=shared).provision()
    first, second = idp.usernames
    assert first != second
    for username in (first, second):
        assert username.startswith(f"smoke-{shared.run_id}-") and username.endswith("@example.test")


# ---------------------------------------------------------------------------
# SPA client token revocation: enabled on the client, and a revoked refresh token is refused.
# ---------------------------------------------------------------------------

_REVOCATION_ROWS = ("token_revocation_enabled", "revoked_refresh_token_refused")


def _failing_revocation_rows(results: list[Any]) -> set[str]:
    by_check = {r.check: r for r in results}
    return {check for check in _REVOCATION_ROWS if by_check[check].status != "pass"}


def test_a_revocation_enabled_client_and_a_refused_revoked_token_pass_both_rows() -> None:
    results = _checks_with_provisioner(_FakeProvisioner())
    assert _failing_revocation_rows(results) == set(), [
        (r.check, r.detail) for r in results if r.check in _REVOCATION_ROWS
    ]
    by_check = {r.check: r for r in results}
    assert by_check["issue_and_decode"].status == "pass"
    assert by_check["cognito_user_deleted"].status == "pass"


@pytest.mark.parametrize("value", [False, _OMIT], ids=["false", "absent"])
def test_token_revocation_not_enabled_reds_exactly_its_row(value: object) -> None:
    results = _checks_with_provisioner(
        _FakeProvisioner(), spa_client=_spa_client(enable_token_revocation=value)
    )
    assert _failing_revocation_rows(results) == {"token_revocation_enabled"}


@pytest.mark.parametrize(
    ("kwargs", "provisioner_kwargs"),
    [
        ({"refresh_outcome": None}, {}),
        ({"refresh_outcome": "InvalidParameterException"}, {}),
        ({"refresh_outcome": "TooManyRequestsException"}, {}),
        ({"revoke_error": "UnsupportedTokenTypeException"}, {}),
        ({"queue_revocation": False}, {"refresh_token": ""}),  # nosec B105  # empty-token fixture
    ],
    ids=["refresh-accepted", "other-error", "throttled", "revoke-failed", "no-refresh-token"],
)
def test_a_revocation_that_did_not_hold_reds_exactly_its_row(
    kwargs: dict[str, Any], provisioner_kwargs: dict[str, Any]
) -> None:
    provisioner = _FakeProvisioner(**provisioner_kwargs)
    results = _checks_with_provisioner(provisioner, **kwargs)
    assert _failing_revocation_rows(results) == {"revoked_refresh_token_refused"}
    assert provisioner.cleaned_up


def test_the_revocation_probe_runs_before_cleanup() -> None:
    results = _checks_with_provisioner(_FakeProvisioner())
    order = [r.check for r in results]
    assert order.index("revoked_refresh_token_refused") < order.index("cognito_user_deleted")


def test_a_pre_provisioned_user_skips_the_revocation_probe_with_a_reason() -> None:
    results = _checks_with_token(_COMPLETE_CLAIMS)
    by_check = {r.check: r for r in results}
    assert by_check["revoked_refresh_token_refused"].status == "skip"
    assert "live entry point" in by_check["revoked_refresh_token_refused"].detail
    assert by_check["token_revocation_enabled"].status == "pass"


def test_the_live_provisioner_carries_the_issued_refresh_token() -> None:
    provisioner = LiveSmokeUserProvisioner(context_for_test(outputs=_outputs()))
    client, stubber = _idp_client()
    stubber.add_response(
        "admin_initiate_auth",
        {"AuthenticationResult": {"IdToken": "id", "RefreshToken": _REFRESH_TOKEN}},
        {"UserPoolId": ANY, "ClientId": ANY, "AuthFlow": ANY, "AuthParameters": ANY},
    )
    with stubber:
        provisioner._ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        id_token, _secret = provisioner._one_auth_attempt(
            username="u", password=_password(), secret=None
        )
    assert id_token == "id"  # nosec B105  # a stub token value, not a credential
    assert provisioner._refresh_token == _REFRESH_TOKEN


# ---------------------------------------------------------------------------
# Hosted UI sign-out: registered logout_uri -> 302 to it; unregistered -> no redirect to it.
# ---------------------------------------------------------------------------

_LOGOUT_URL = "https://d111111abcdef8.cloudfront.net/logout"
_DOMAIN = "crm-fixture"
_LOGOUT_BASE = f"https://{_DOMAIN}.auth.us-east-1.amazoncognito.com/logout"


class _Probe:
    """Answers like Cognito: a registered logout_uri redirects there, anything else to /error."""

    def __init__(self, registered: str = _LOGOUT_URL, *, echo: bool = False) -> None:
        self.registered = registered
        self.echo = echo
        self.urls: list[str] = []

    def __call__(self, url: str) -> tuple[int, str]:
        from urllib.parse import parse_qs, urlsplit

        self.urls.append(url)
        query = parse_qs(urlsplit(url).query)
        uri = query["logout_uri"][0]
        if uri == self.registered or self.echo:
            return 302, uri
        return (
            302,
            f"https://{_DOMAIN}.auth.us-east-1.amazoncognito.com/error?error=redirect_mismatch",
        )


def _logout_rows(
    *,
    probe: Any,
    domain: str | None = _DOMAIN,
    logout_urls: list[str] | None = None,
) -> dict[str, Any]:
    client, stubber = _idp_client()
    pool = _compliant_pool(**({"Domain": domain} if domain is not None else {}))
    _add_pool(stubber, pool)
    if domain:
        stubber.add_response(
            "describe_user_pool_client",
            {
                "UserPoolClient": {
                    "UserPoolId": _USER_POOL_ID,
                    "ClientId": _SPA_CLIENT_ID,
                    "LogoutURLs": [_LOGOUT_URL] if logout_urls is None else logout_urls,
                }
            },
            {"UserPoolId": _USER_POOL_ID, "ClientId": _SPA_CLIENT_ID},
        )
    with stubber:
        ctx = context_for_test(outputs=_outputs(), clients={"cognito-idp": client})
        results = list(checks(ctx, logout_probe=probe))
    return {r.check: r for r in results if r.check.startswith("hosted_ui_logout_")}


def test_the_registered_logout_redirects_and_the_unregistered_one_does_not() -> None:
    probe = _Probe()
    rows = _logout_rows(probe=probe)
    assert {c: r.status for c, r in rows.items()} == {
        "hosted_ui_logout_registered_redirects": "pass",
        "hosted_ui_logout_unregistered_refused": "pass",
    }
    assert all(url.startswith(f"{_LOGOUT_BASE}?") for url in probe.urls)
    assert f"client_id={_SPA_CLIENT_ID}" in probe.urls[0]


def test_a_registered_logout_that_does_not_redirect_there_fails() -> None:
    rows = _logout_rows(probe=_Probe(registered="https://elsewhere.example/logout"))
    assert rows["hosted_ui_logout_registered_redirects"].status == "fail"
    assert rows["hosted_ui_logout_unregistered_refused"].status == "pass"


def test_an_endpoint_that_redirects_to_any_logout_uri_fails_the_unregistered_row() -> None:
    rows = _logout_rows(probe=_Probe(echo=True))
    assert rows["hosted_ui_logout_registered_redirects"].status == "pass"
    assert rows["hosted_ui_logout_unregistered_refused"].status == "fail"


def test_an_unreachable_hosted_ui_defers_both_rows_with_a_reason() -> None:
    def _down(url: str) -> tuple[int, str]:
        raise OSError("connection refused")

    rows = _logout_rows(probe=_down)
    assert {r.status for r in rows.values()} == {"defer"}
    assert {r.defer_reason for r in rows.values()} == {"hosted_ui_unreachable"}


def test_a_pool_without_a_domain_defers_both_rows_with_a_reason() -> None:
    rows = _logout_rows(probe=_Probe(), domain=None)
    assert {r.status for r in rows.values()} == {"defer"}
    assert {r.defer_reason for r in rows.values()} == {"hosted_ui_domain_absent"}


def test_a_client_with_no_logout_url_fails_both_rows() -> None:
    rows = _logout_rows(probe=_Probe(), logout_urls=[])
    assert {r.status for r in rows.values()} == {"fail"}


def test_no_probe_skips_both_rows_never_passes() -> None:
    results = _run_checks_with_describe(_compliant_pool())
    rows = {r.check: r.status for r in results if r.check.startswith("hosted_ui_logout_")}
    assert rows == {
        "hosted_ui_logout_registered_redirects": "skip",
        "hosted_ui_logout_unregistered_refused": "skip",
    }


def test_the_live_probe_refuses_a_non_https_url() -> None:
    with pytest.raises(ValueError, match="non-HTTPS"):
        cognito_claims._live_logout_probe(
            "http://crm-fixture.auth.us-east-1.amazoncognito.com/logout"
        )


def test_the_live_entry_point_supplies_the_logout_probe() -> None:
    import inspect

    source = inspect.getsource(cognito_claims.main)
    assert "logout_probe=_live_logout_probe" in source
