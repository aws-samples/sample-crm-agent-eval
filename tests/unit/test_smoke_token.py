# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.smoke_token`: the run-tenant-bound `CRM_SMOKE_TOKEN` seam.

The demo-token cells drive the real `crm.auth.pre_token_generation.lambda_handler`, whose defaults
are fail-closed on visibility but fall back to the `demo` tenant.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber
from scripts.smoke import verified_claims
from scripts.smoke.cognito_claims import LiveSmokeUserProvisioner
from scripts.smoke.harness import context_for_test
from scripts.smoke.smoke_token import (
    TENANT_CLAIM,
    assert_token_tenant,
    mint_run_token,
)
from scripts.smoke.tenant import SmokeTenant, allocate_smoke_tenant, seed_smoke_tenant
from scripts.smoke.verified_claims import cognito_issuer

from crm.auth.pre_token_generation import lambda_handler
from crm.authz import DEMO_TENANT
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.unit import _jwks_test_key as test_key

_RUN_TENANT = "eval-smoke-0123456789abcdef0123456789abcdef"
_RUN_ID = "smoke-0123456789abcdef0123456789abcdef"

#: The `Username` the stubbed pool returns from `admin_create_user`.
_PROVISIONED_REP = "1111aaaa-2222-bbbb-3333-cccc4444dddd"

#: RFC 6238's base32 test seed.
_RFC6238_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _outputs() -> dict[str, Any]:
    return {
        "cognito_user_pool_id": "us-east-1_TESTPOOL",
        "cognito_spa_client_id": "test-spa-client",
        "dynamodb_table_names": {"eval_runs": "eval_runs"},
    }


# -- the guard ------------------------------------------------------------------------------------


def test_a_matching_tenant_claim_is_returned() -> None:
    assert assert_token_tenant({TENANT_CLAIM: _RUN_TENANT}, expected=_RUN_TENANT) == _RUN_TENANT


def test_an_absent_tenant_claim_is_refused() -> None:
    """A token with no tenant claim is refused."""
    with pytest.raises(RuntimeError, match="carries no 'custom:tenant' claim"):
        assert_token_tenant({}, expected=_RUN_TENANT)


def test_an_empty_tenant_claim_is_refused_like_an_absent_one() -> None:
    with pytest.raises(RuntimeError, match="carries no 'custom:tenant' claim"):
        assert_token_tenant({TENANT_CLAIM: "   "}, expected=_RUN_TENANT)


def test_a_different_disposable_tenant_is_refused() -> None:
    """A disposable `eval-` tenant other than the run's own is refused."""
    stray = "eval-smoke-ffffffffffffffffffffffffffffffff"
    with pytest.raises(RuntimeError, match="this run owns"):
        assert_token_tenant({TENANT_CLAIM: stray}, expected=_RUN_TENANT)


def test_the_demo_tenant_is_refused_with_its_own_diagnosis() -> None:
    """A `demo` tenant claim is refused with a message naming the absent `reps` row."""
    with pytest.raises(RuntimeError, match="absent `reps` row"):
        assert_token_tenant({TENANT_CLAIM: DEMO_TENANT}, expected=_RUN_TENANT)


# -- the real demo-token path ---------------------------------------------------------------------


def test_a_user_with_no_reps_row_mints_a_demo_token_and_is_refused() -> None:
    """The real trigger stamps `demo` for a user with no `reps` row, and the guard refuses it."""
    event: dict[str, Any] = {
        "userName": "24180448-3001-708d-4ead-5d9261dea96c",
        "request": {"userAttributes": {"email": "smoke@example.test"}},
        "response": {},
    }

    # A store whose `reps` lookup misses.
    class _EmptyStore:
        def get_item(self, table: str, key: dict[str, Any]) -> None:
            return None

    result = lambda_handler(event, None, store=_EmptyStore())
    claims = _claims_from_trigger_result(result)

    assert claims[TENANT_CLAIM] == DEMO_TENANT, (
        "the trigger no longer falls back to `demo`; if that changed, this cell's premise is stale "
        "and the guard's demo-specific message should be revisited"
    )
    with pytest.raises(RuntimeError, match="absent `reps` row"):
        assert_token_tenant(dict(claims), expected=_RUN_TENANT)


def test_the_triggers_defaults_are_fail_closed_on_visibility_but_not_on_tenant() -> None:
    """With no `reps` row, visibility claims are empty/false while the tenant claim is `demo`."""

    class _EmptyStore:
        def get_item(self, table: str, key: dict[str, Any]) -> None:
            return None

    event: dict[str, Any] = {
        "userName": "some-uuid",
        "request": {"userAttributes": {}},
        "response": {},
    }
    claims = _claims_from_trigger_result(lambda_handler(event, None, store=_EmptyStore()))

    assert claims["custom:full_visibility"] in ("false", "False", False)
    assert claims["custom:book"] in ("", None)
    # ...and yet the tenant is the seeded UI one, not an empty or disposable value.
    assert claims[TENANT_CLAIM] == DEMO_TENANT


def _claims_from_trigger_result(result: dict[str, Any]) -> dict[str, Any]:
    """Pull the stamped claims out of the V2 trigger's response envelope."""
    response = result.get("response") or {}
    override = response.get("claimsAndScopeOverrideDetails") or {}
    id_override = override.get("idTokenGeneration") or {}
    claims = id_override.get("claimsToAddOrOverride") or {}
    assert claims, f"no claims found in the trigger response: {result!r}"
    return dict(claims)


# -- mint_run_token's own preconditions -----------------------------------------------------------


def test_a_non_smoke_run_id_is_refused_before_any_aws_call() -> None:
    """A run id not allocated by `scripts.smoke.tenant` is refused before any client is built."""
    ctx = context_for_test(outputs=_outputs())
    with pytest.raises(RuntimeError, match="is not a smoke run id"):
        mint_run_token(ctx, SmokeTenant(run_id="not-a-smoke-run", tenant=_RUN_TENANT))


# -- the injection contract -----------------------------------------------------------------------


def test_an_injected_tenant_is_not_owned_and_its_teardown_is_skipped() -> None:
    """A caller-injected tenant is not torn down: its `tenant_left_clean` row is a `skip`."""
    ctx = context_for_test(outputs=_outputs())
    provisioner = LiveSmokeUserProvisioner(
        ctx, smoke_tenant=SmokeTenant(run_id=_RUN_ID, tenant=_RUN_TENANT)
    )

    rows = {result.check: result for result in provisioner._cleanup_tenant()}

    assert "tenant_left_clean" in rows
    assert rows["tenant_left_clean"].status == "skip"
    assert "injected by the caller" in rows["tenant_left_clean"].detail


def test_an_owned_tenant_is_still_torn_down_so_the_skip_is_conditional() -> None:
    """A provisioner holding its own allocated tenant still tears it down.

    The tenant must be real: `_cleanup_tenant` returns early on a `None` tenant.
    """
    with _moto_dynamodb() as dynamodb:
        ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": dynamodb})
        provisioner = LiveSmokeUserProvisioner(ctx)
        provisioner._smoke_tenant = allocate_smoke_tenant(Store(client=dynamodb))

        rows = {result.check: result for result in provisioner._cleanup_tenant()}

    assert rows["tenant_left_clean"].status == "pass"
    assert "torn down" in rows["tenant_left_clean"].detail


def test_for_existing_user_binds_the_username_and_owns_no_tenant() -> None:
    """`for_existing_user` binds a username and owns no tenant, so tenant cleanup is skipped."""
    ctx = context_for_test(outputs=_outputs())
    provisioner = LiveSmokeUserProvisioner.for_existing_user(ctx, username="some-uuid")

    rows = {result.check: result for result in provisioner._cleanup_tenant()}
    assert rows["tenant_left_clean"].status == "skip"


# -- mint_run_token end to end, against a stubbed Cognito and a moto DynamoDB ----------------------


def test_a_mint_under_the_runs_own_tenant_returns_the_token_and_leaves_the_tenant_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mint under the run's own tenant returns the token and deletes neither user nor tenant."""
    _patch_offline_decode(monkeypatch)
    with _moto_dynamodb() as dynamodb:
        idp, stubber = _idp_client()
        ctx = _live_ctx(idp, dynamodb)
        smoke_tenant = _allocated_and_seeded(dynamodb)
        _stub_one_auth_attempt(stubber, tenant=smoke_tenant.tenant)

        with stubber:
            minted = mint_run_token(ctx, smoke_tenant)
            stubber.assert_no_pending_responses()

        assert minted.tenant == smoke_tenant.tenant
        assert minted.username == _PROVISIONED_REP
        assert minted.id_token
        # The provisioner's own per-user address, nonce included, not a run-only reconstruction.
        assert re.fullmatch(
            rf"smoke-{re.escape(smoke_tenant.run_id)}-[0-9a-f]{{8}}@example\.test", minted.email
        ), minted.email

        # No cleanup ran: the user is the caller's to delete from its trap, and the tenant is intact.
        assert _reps_row(dynamodb, _PROVISIONED_REP) is not None
        assert _run_status(dynamodb, smoke_tenant.run_id) == "running"


def test_a_mint_that_fails_after_creating_the_user_still_removes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after user creation propagates and removes the Cognito user and `reps` row."""
    _patch_offline_decode(monkeypatch)
    with _moto_dynamodb() as dynamodb:
        idp, stubber = _idp_client()
        ctx = _live_ctx(idp, dynamodb)
        smoke_tenant = _allocated_and_seeded(dynamodb)

        stubber.add_response(
            "admin_create_user",
            {"User": {"Username": _PROVISIONED_REP}},
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
        # The sign-in fails outright, standing for any post-creation failure.
        stubber.add_client_error(
            "admin_initiate_auth",
            service_error_code="TooManyRequestsException",
            http_status_code=400,
        )
        # ...and the cleanup that must follow it.
        stubber.add_response("admin_delete_user", {}, {"UserPoolId": ANY, "Username": ANY})
        stubber.add_client_error(
            "admin_get_user", service_error_code="UserNotFoundException", http_status_code=400
        )

        with stubber:
            with pytest.raises(Exception, match="TooManyRequests"):
                mint_run_token(ctx, smoke_tenant)
            # Every queued response consumed, so the delete and its read-back both really happened.
            stubber.assert_no_pending_responses()

        assert _reps_row(dynamodb, _PROVISIONED_REP) is None, (
            "the smoke user's reps row survived a failed mint, so a rep id with no Cognito user is "
            "left in a tenant-scoped table"
        )
        # The caller's tenant survives: its rows are the corpus every other module is still reading.
        assert _run_status(dynamodb, smoke_tenant.run_id) == "running", (
            "the refusal tore down the caller's tenant, which teardown_smoke_tenant marks torn_down"
        )


# -- offline fixtures ------------------------------------------------------------------------------


@contextlib.contextmanager
def _moto_dynamodb() -> Iterator[Any]:
    """A moto-backed DynamoDB client with every CRM table created."""
    from moto import mock_aws

    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(dynamodb)
        yield dynamodb


def _idp_client() -> tuple[Any, Stubber]:
    client = boto3.client("cognito-idp", region_name="us-east-1")
    return client, Stubber(client)


def _live_ctx(idp: Any, dynamodb: Any) -> Any:
    return context_for_test(outputs=_outputs(), clients={"cognito-idp": idp, "dynamodb": dynamodb})


def _allocated_and_seeded(dynamodb: Any) -> SmokeTenant:
    """Allocate and seed a smoke tenant the way `verify_deployment.sh`'s `provision_tenant` does."""
    store = Store(client=dynamodb)
    smoke_tenant = allocate_smoke_tenant(store)
    seed_smoke_tenant(store, smoke_tenant)
    return smoke_tenant


def _reps_row(dynamodb: Any, rep_id: str) -> dict[str, Any] | None:
    return Store(client=dynamodb).get_item("reps", {"rep_id": rep_id})


def _run_status(dynamodb: Any, run_id: str) -> str:
    """The `eval_runs` catalog status, which `teardown_smoke_tenant` flips to `torn_down`."""
    row = Store(client=dynamodb).get_item("eval_runs", {"run_id": run_id})
    assert row is not None, f"no eval_runs catalog row for {run_id!r}"
    return str(row.get("status") or "")


def _stub_one_auth_attempt(stubber: Stubber, *, tenant: str) -> None:
    """Queue `admin_create_user` -> ... -> a token claiming ``tenant`` on the first attempt."""
    stubber.add_response(
        "admin_create_user",
        {"User": {"Username": _PROVISIONED_REP}},
        {"UserPoolId": ANY, "Username": ANY, "MessageAction": "SUPPRESS", "UserAttributes": ANY},
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
        {"SecretCode": _RFC6238_SEED, "Session": "smoke-session-token-2-padding"},
        {"Session": "smoke-session-token-1-padding"},
    )
    stubber.add_response(
        "verify_software_token",
        {"Status": "SUCCESS", "Session": "smoke-session-token-3-padding"},
        {"Session": "smoke-session-token-2-padding", "UserCode": ANY, "FriendlyDeviceName": ANY},
    )
    outputs = _outputs()
    token = test_key.id_token(
        iss=cognito_issuer(test_key.REGION, outputs["cognito_user_pool_id"]),
        aud=outputs["cognito_spa_client_id"],
        **{
            "custom:rep_id": _PROVISIONED_REP,
            "custom:book": f"acct-{tenant}-0000,acct-{tenant}-0001",
            "custom:full_visibility": "false",
            TENANT_CLAIM: tenant,
        },
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


def _patch_offline_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the stubbed pool's JWKS endpoint at the loopback test JWKS, so tokens still verify."""
    monkeypatch.setattr(
        verified_claims, "cognito_jwks_url", lambda _region, _pool: test_key.jwks_url()
    )
