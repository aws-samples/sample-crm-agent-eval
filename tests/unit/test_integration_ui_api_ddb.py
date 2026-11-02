# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.integration_ui_api_ddb`: the Layer 5 UI->API->Lambda->DynamoDB round
trip through the CloudFront `/api/*` behaviour.

Exercises `checks()` against an injected HTTP-call seam and a `botocore.stub.Stubber`-backed
DynamoDB client, with no credentials or network. Covers the create/read/update round trip with
persisted rows compared, the out-of-book 403 denial, `Authorization` forwarding and no cached
authenticated response, the skip cascade when create fails, the tenant refusal, and that every
write carries the smoke tenant and never `demo`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from scripts.smoke import verified_claims
from scripts.smoke.harness import context_for_test
from scripts.smoke.integration_ui_api_ddb import (
    HttpResult,
    _make_transaction_key,
    _tenant_marker,
    checks,
)

from crm.authz import DEMO_TENANT
from tests.unit import _jwks_test_key as test_key

CLOUDFRONT_DOMAIN = "d123456abcdef.cloudfront.net"
SMOKE_TENANT = "eval-smoke-deadbeef"
ACCOUNT_ID = "acct-1"
#: Placeholder credential for a client whose calls never leave the process; not a secret.
_STUB_CRED = "test"
OUT_OF_BOOK_ACCOUNT_ID = "acct-out-of-book"
OPPORTUNITY_ID = "opp-smoke-1"
REP_ID = "rep-1"
TABLE_PREFIX = "crm-test"
OPPORTUNITY_TABLE = f"{TABLE_PREFIX}opportunities"
TOKEN = test_key.id_token(**{"custom:tenant": SMOKE_TENANT})

#: The `X-Cache` disposition CloudFront reports for a response served from the origin.
MISS = "Miss from cloudfront"


@pytest.fixture(autouse=True)
def _loopback_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens verify against the loopback test JWKS instead of the pool's public endpoint."""
    monkeypatch.setattr(verified_claims, "cognito_jwks_url", lambda _r, _p: test_key.jwks_url())


def _token_claiming_tenant(tenant: str) -> str:
    """A signed ID token for the test pool whose `custom:tenant` is `tenant`."""
    return test_key.id_token(**{"custom:tenant": tenant})


def _dynamodb_client() -> Any:
    """A real boto3 `dynamodb` client, never used for network I/O: `Stubber` intercepts first."""
    return boto3.client(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id=_STUB_CRED,
        aws_secret_access_key=_STUB_CRED,
    )


def _ddb_string(value: str) -> dict[str, str]:
    return {"S": value}


def _opportunity_item(*, tenant: str, name: str, updated: bool) -> dict[str, Any]:
    item = {
        "opportunity_id": _ddb_string(OPPORTUNITY_ID),
        "tenant": _ddb_string(tenant),
        "tenant_account_id": _ddb_string(f"{tenant}#{ACCOUNT_ID}"),
        "tenant_owner_rep_id": _ddb_string(f"{tenant}#{REP_ID}"),
        "tenant_stage": _ddb_string(f"{tenant}#Discovery"),
        "account_id": _ddb_string(ACCOUNT_ID),
        "owner_rep_id": _ddb_string(REP_ID),
        "name": _ddb_string(name),
        "name_lower": _ddb_string(name.lower()),
        "stage": _ddb_string("Discovery"),
        "created_at": _ddb_string("2026-01-01T00:00:00+00:00"),
        "created_by": _ddb_string(REP_ID),
    }
    if updated:
        item["updated_at"] = _ddb_string("2026-01-01T00:05:00+00:00")
    return item


def _stub_get_opportunity(stubber: Stubber, *, tenant: str, name: str, updated: bool) -> None:
    """Queue one `get_item`, asserting the store addressed the *prefixed* physical table."""
    stubber.add_response(
        "get_item",
        {"Item": _opportunity_item(tenant=tenant, name=name, updated=updated)},
        {
            "TableName": OPPORTUNITY_TABLE,
            "Key": {"opportunity_id": _ddb_string(OPPORTUNITY_ID)},
            "ConsistentRead": False,
        },
    )


def _token_with_claims(claims: Mapping[str, Any]) -> str:
    """A signed ID token for the test pool carrying ``claims``."""
    return test_key.id_token(**claims)


def _stub_get_account(stubber: Stubber, account_id: str, *, tenant: str | None) -> None:
    """Queue one `accounts` `get_item`: a row of ``tenant``, or no item when ``tenant`` is None."""
    response: dict[str, Any] = {}
    if tenant is not None:
        response["Item"] = {"account_id": _ddb_string(account_id), "tenant": _ddb_string(tenant)}
    stubber.add_response(
        "get_item",
        response,
        {
            "TableName": f"{TABLE_PREFIX}accounts",
            "Key": {"account_id": _ddb_string(account_id)},
            "ConsistentRead": False,
        },
    )


def _recording_caller(caller: Any, calls: list[tuple[str, dict[str, Any]]]) -> Any:
    """Wrap ``caller`` so every tool call's (tool name, parsed body) is appended to ``calls``."""

    def recorder(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        calls.append((url.split("/api/", 1)[1], json.loads(body) if body else {}))
        result: HttpResult = caller(method, url, headers, body)
        return result

    return recorder


def _make_ctx(client: Any, *, tenant: str | None = SMOKE_TENANT) -> Any:
    return context_for_test(
        outputs={
            "cloudfront_domain_name": CLOUDFRONT_DOMAIN,
            "dynamodb_table_names": {"eval_runs": f"{TABLE_PREFIX}eval_runs"},
            "cognito_user_pool_id": test_key.USER_POOL_ID,
            "cognito_spa_client_id": test_key.CLIENT_ID,
        },
        clients={"dynamodb": client},
        tenant=tenant,
    )


def _happy_http_caller(
    *, denial_status: int = 403, cache_disposition: str = MISS, unauthenticated_status: int = 401
) -> Any:
    """An HTTP caller that succeeds create/read/update and denies the out-of-book target.

    Mirrors the live contract the script depends on: an authenticated call answers `200`, the same
    call with no `Authorization` header answers `unauthenticated_status`, and every response
    carries an `X-Cache` header reporting `cache_disposition`.
    """

    def caller(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        assert method == "POST"
        assert url.startswith(f"https://{CLOUDFRONT_DOMAIN}/api/")
        path = url.split("/api/", 1)[1]
        parsed_body = json.loads(body) if body else {}
        response_headers = {"x-cache": cache_disposition}

        if "Authorization" not in headers:
            return unauthenticated_status, json.dumps({"message": "Unauthorized"}), response_headers
        assert headers["Authorization"].startswith("Bearer ")

        if path == "create_opportunity":
            return (
                200,
                json.dumps({"opportunity": {"opportunity_id": OPPORTUNITY_ID}}),
                response_headers,
            )
        if path == "get_opportunity":
            assert parsed_body.get("opportunity_id") == OPPORTUNITY_ID
            return (
                200,
                json.dumps({"opportunity": {"opportunity_id": OPPORTUNITY_ID}}),
                response_headers,
            )
        if path == "update_opportunity":
            return (
                200,
                json.dumps({"opportunity": {"opportunity_id": OPPORTUNITY_ID}}),
                response_headers,
            )
        if path == "get_account":
            return denial_status, json.dumps({"error": {"code": "scope_denied"}}), response_headers
        raise AssertionError(f"unexpected tool call: {path}")  # pragma: no cover

    return caller


def _stub_full_round_trip(stubber: Stubber, *, tenant: str = SMOKE_TENANT) -> None:
    """Queue the three `get_item` reads the create/read/update sequence performs."""
    _stub_get_opportunity(stubber, tenant=tenant, name="Layer 5 smoke opportunity", updated=False)
    _stub_get_opportunity(stubber, tenant=tenant, name="Layer 5 smoke opportunity", updated=False)
    _stub_get_opportunity(
        stubber, tenant=tenant, name="Layer 5 smoke opportunity (updated)", updated=True
    )


def _run(
    ctx: Any,
    caller: Any,
    *,
    token: str | None = TOKEN,
    out_of_book_account_id: str | None = OUT_OF_BOOK_ACCOUNT_ID,
) -> list[Any]:
    return list(
        checks(
            ctx,
            http_caller=caller,
            token=token,
            account_id=ACCOUNT_ID,
            out_of_book_account_id=out_of_book_account_id,
        )
    )


def _check(results: list[Any], check_name: str, resource: str | None = None) -> Any:
    return next(
        r for r in results if r.check == check_name and (resource is None or r.resource == resource)
    )


def test_happy_path_create_read_update_all_pass_and_ddb_rows_match() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(
        _make_ctx(client), _happy_http_caller(), out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID
    )
    stubber.deactivate()

    failed = [r for r in results if r.status == "fail"]
    assert not failed, f"unexpected failures: {failed}"

    ddb_checks = {r.resource: r for r in results if r.check == "ddb_row_matches"}
    assert ddb_checks["create"].status == "pass"
    assert ddb_checks["read"].status == "pass"
    assert ddb_checks["update"].status == "pass"

    assert _check(results, "out_of_book_refused_403").status == "pass"
    assert _check(results, "writes_only_under_smoke_tenant").status == "pass"
    assert _check(results, "authorization_forwarded").status == "pass"
    assert _check(results, "authenticated_response_not_cached").status == "pass"


def test_ddb_row_mismatch_on_tenant_is_detected() -> None:
    """The row-match assertion is non-vacuous: a row persisted under the wrong tenant must fail,
    not be silently accepted as agreeing with the API's claim."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    # The persisted row is under `demo`, not the smoke tenant - a real cross-tenant bug.
    _stub_full_round_trip(stubber, tenant=DEMO_TENANT)
    stubber.activate()

    results = _run(_make_ctx(client), _happy_http_caller())
    stubber.deactivate()

    create_ddb_check = _check(results, "ddb_row_matches", "create")
    assert create_ddb_check.status == "fail"
    assert "tenant" in create_ddb_check.detail


def test_ddb_row_missing_audit_attributes_on_update_is_detected() -> None:
    """The update step's row-match assertion requires audit attributes; a row missing
    `updated_at` must fail rather than pass."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_get_opportunity(
        stubber, tenant=SMOKE_TENANT, name="Layer 5 smoke opportunity", updated=False
    )
    _stub_get_opportunity(
        stubber, tenant=SMOKE_TENANT, name="Layer 5 smoke opportunity", updated=False
    )
    # Updated row is missing `updated_at` - the audit attribute this check requires.
    _stub_get_opportunity(
        stubber, tenant=SMOKE_TENANT, name="Layer 5 smoke opportunity (updated)", updated=False
    )
    stubber.activate()

    results = _run(_make_ctx(client), _happy_http_caller())
    stubber.deactivate()

    update_ddb_check = _check(results, "ddb_row_matches", "update")
    assert update_ddb_check.status == "fail"
    assert "updated_at" in update_ddb_check.detail


def test_out_of_book_denied_with_403_passes() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(
        _make_ctx(client),
        _happy_http_caller(denial_status=403),
        out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID,
    )
    stubber.deactivate()

    denial_check = _check(results, "out_of_book_refused_403")
    assert denial_check.status == "pass"
    assert "403" in denial_check.detail


def test_out_of_book_returning_200_is_a_distinct_failure() -> None:
    """A 200 for an out-of-book target fails the denial check."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(
        _make_ctx(client),
        _happy_http_caller(denial_status=200),
        out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID,
    )
    stubber.deactivate()

    denial_check = _check(results, "out_of_book_refused_403")
    assert denial_check.status == "fail"
    assert "200" in denial_check.detail


def test_out_of_book_returning_404_is_a_distinct_failure_from_403() -> None:
    """404 must not be silently accepted as an acceptable stand-in for 403 - an authorization
    bug that surfaces as not-found is a different, distinctly-reported bug."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(
        _make_ctx(client),
        _happy_http_caller(denial_status=404),
        out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID,
    )
    stubber.deactivate()

    denial_check = _check(results, "out_of_book_refused_403")
    assert denial_check.status == "fail"
    assert "404" in denial_check.detail
    assert "403" in denial_check.detail


def test_out_of_book_account_not_supplied_is_a_failure_never_a_skip() -> None:
    """The 403 refusal is this script's security assertion: an unobserved boundary must be
    reported as a failure, not as a skip a reader could mistake for not-applicable.

    With no flag and no seeded account outside the book in DynamoDB, nothing is derived either.
    """
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    for index in (1, 2):
        _stub_get_account(stubber, f"acct-{SMOKE_TENANT}-{index:04d}", tenant=None)
    stubber.activate()
    token = _token_with_claims(
        {"custom:tenant": SMOKE_TENANT, "custom:book": f"acct-{SMOKE_TENANT}-0000"}
    )

    results = _run(
        _make_ctx(client), _happy_http_caller(), token=token, out_of_book_account_id=None
    )
    stubber.assert_no_pending_responses()
    stubber.deactivate()

    denial_check = _check(results, "out_of_book_refused_403")
    assert denial_check.status == "fail"
    assert "LookupError" in denial_check.detail


def test_authorization_not_forwarded_would_be_detected() -> None:
    """Non-vacuous: if the unauthenticated repeat of the same read answered 200, the header never
    reached the origin's authorizer and the check must fail rather than pass."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(_make_ctx(client), _happy_http_caller(unauthenticated_status=200))
    stubber.deactivate()

    forwarded = _check(results, "authorization_forwarded")
    assert forwarded.status == "fail"
    assert "200" in forwarded.detail


def test_cached_authenticated_response_is_detected_as_a_failure() -> None:
    """Non-vacuous: an `X-Cache` hit on an authenticated response is the cross-caller leak the
    behaviour's disabled cache policy exists to prevent, and must fail."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(_make_ctx(client), _happy_http_caller(cache_disposition="Hit from cloudfront"))
    stubber.deactivate()

    not_cached = _check(results, "authenticated_response_not_cached")
    assert not_cached.status == "fail"
    assert "cache" in not_cached.detail


def test_absent_cache_header_is_a_failure_not_a_pass() -> None:
    """A missing `X-Cache` header means caching was not observed at all; an unobservable control
    is not a verified one."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(_make_ctx(client), _happy_http_caller(cache_disposition=""))
    stubber.deactivate()

    not_cached = _check(results, "authenticated_response_not_cached")
    assert not_cached.status == "fail"


def test_create_failure_reports_read_and_update_as_skip_not_omitted() -> None:
    """A failed create must not silently omit the read/update steps from the inventory - they
    must be reported as `skip` naming the failed prerequisite."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    stubber.activate()  # no calls expected: create fails before any DynamoDB read

    def failing_caller(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        assert method == "POST"
        path = url.split("/api/", 1)[1]
        if path == "create_opportunity":
            return 500, json.dumps({"error": {"code": "internal_error"}}), {"x-cache": MISS}
        raise AssertionError(f"unexpected tool call: {path}")  # pragma: no cover

    results = _run(_make_ctx(client), failing_caller)
    stubber.deactivate()

    assert _check(results, "http_call", "create").status == "fail"

    read_check = _check(results, "http_call", "read")
    assert read_check.status == "skip"
    assert "create" in read_check.detail

    update_check = _check(results, "http_call", "update")
    assert update_check.status == "skip"
    assert "create" in update_check.detail

    # No DynamoDB call was ever issued - the stubber above had zero queued responses and
    # `stubber.deactivate()` above did not raise, proving `checks()` never attempted a read.


def test_read_failure_reports_update_as_skip_and_create_is_still_checked() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_get_opportunity(
        stubber, tenant=SMOKE_TENANT, name="Layer 5 smoke opportunity", updated=False
    )
    stubber.activate()

    def caller(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        path = url.split("/api/", 1)[1]
        if path == "create_opportunity":
            return (
                200,
                json.dumps({"opportunity": {"opportunity_id": OPPORTUNITY_ID}}),
                {"x-cache": MISS},
            )
        if path == "get_opportunity":
            return 500, json.dumps({"error": {"code": "internal_error"}}), {"x-cache": MISS}
        raise AssertionError(f"unexpected tool call: {path}")  # pragma: no cover

    results = _run(_make_ctx(client), caller)
    stubber.deactivate()

    assert _check(results, "ddb_row_matches", "create").status == "pass"
    assert _check(results, "http_call", "read").status == "fail"

    update_check = _check(results, "http_call", "update")
    assert update_check.status == "skip"
    assert "read" in update_check.detail


def test_writes_only_under_smoke_tenant_is_proven_over_a_spy_not_source_inspection() -> None:
    """The strong proof: every recorded write call's transaction_key carries the smoke tenant.
    Non-vacuous - a script that (hypothetically) wrote under `demo` would be caught here."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(_make_ctx(client), _happy_http_caller())
    stubber.deactivate()

    writes_check = _check(results, "writes_only_under_smoke_tenant")
    assert writes_check.status == "pass"
    assert "2 write" in writes_check.detail


def test_writes_carrying_demo_marker_are_detected_as_a_violation() -> None:
    """Non-vacuous in the failing direction: if a write's transaction_key carried the `demo`
    marker instead of the smoke tenant, the check must fail, not silently pass."""
    from scripts.smoke.integration_ui_api_ddb import _writes_only_under_smoke_tenant_check

    recorded_calls = [
        (
            "POST",
            f"https://{CLOUDFRONT_DOMAIN}/api/create_opportunity",
            json.dumps({"transaction_key": _make_transaction_key(DEMO_TENANT, "create")}),
        )
    ]
    violation_results = list(_writes_only_under_smoke_tenant_check(recorded_calls, SMOKE_TENANT))
    assert len(violation_results) == 1
    assert violation_results[0].status == "fail"
    assert "demo" in violation_results[0].detail


def test_transaction_key_fits_dynamodb_client_request_token_cap() -> None:
    """The smoke's keys stay within 36 characters even for a long smoke tenant name, well inside
    the toolspec's 128-character `transaction_key` bound; `Store` derives the token itself."""
    key = _make_transaction_key("eval-smoke-" + "a" * 32, "update")
    assert len(key) <= 36
    assert key.startswith(_tenant_marker("eval-smoke-" + "a" * 32))


def test_transaction_key_marker_distinguishes_two_similar_tenants() -> None:
    """The marker is a digest of the whole tenant name, so two tenants sharing a long prefix do
    not share a marker - otherwise the write-safety assertion would be satisfiable by the
    wrong tenant."""
    first = "eval-smoke-" + "a" * 32
    second = "eval-smoke-" + "a" * 31 + "b"
    assert _tenant_marker(first) != _tenant_marker(second)


def test_token_claiming_demo_tenant_is_refused_before_any_call() -> None:
    """A token scoped to `demo` stops the run outright, reported as a failure."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    stubber.activate()

    results = _run(
        _make_ctx(client, tenant=None),
        _happy_http_caller(),
        token=_token_claiming_tenant(DEMO_TENANT),
    )
    stubber.deactivate()

    assert len(results) == 1
    assert results[0].check == "token_tenant_is_disposable_smoke_tenant"
    assert results[0].status == "fail"
    assert DEMO_TENANT in results[0].detail


def test_tenant_is_taken_from_the_token_claim_when_no_flag_is_given() -> None:
    """With no `--tenant` flag the tenant is the token's own claim."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()

    results = _run(
        _make_ctx(client, tenant=None),
        _happy_http_caller(),
        token=_token_claiming_tenant(SMOKE_TENANT),
    )
    stubber.deactivate()

    tenant_check = _check(results, "token_tenant_is_disposable_smoke_tenant")
    assert tenant_check.status == "pass"
    assert SMOKE_TENANT in tenant_check.detail
    assert [r for r in results if r.check == "ddb_row_matches" and r.status == "pass"]


def test_missing_token_fails_before_any_http_or_ddb_call() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    stubber.activate()

    results = _run(_make_ctx(client), _happy_http_caller(), token=None)
    stubber.deactivate()

    assert len(results) == 1
    assert results[0].check == "token_configured"
    assert results[0].status == "fail"


def test_transport_failure_on_create_is_reported_as_fail_not_traceback() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    stubber.activate()

    def transport_failing_caller(
        method: str, url: str, headers: Mapping[str, str], body: str
    ) -> HttpResult:
        return f"transport error calling {url}: connection refused"

    results = _run(_make_ctx(client), transport_failing_caller)
    stubber.deactivate()

    create_check = _check(results, "http_call", "create")
    assert create_check.status == "fail"
    assert "transport error" in create_check.detail


def test_main_missing_outputs_fails_cleanly(tmp_path: Any, capsys: Any) -> None:
    from scripts.smoke.integration_ui_api_ddb import main

    outputs_file = tmp_path / "tf-outputs.json"
    outputs_file.write_text("{}", encoding="utf-8")

    exit_code = main(["--outputs", str(outputs_file), "--json"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert '"layer": 5' in captured.out
    assert '"resource_class": "integration_ui_api_ddb"' in captured.out


# -- account resolution from the token's own `custom:book` claim -------------------------------


def test_supplied_token_without_account_id_uses_first_book_entry_from_claim() -> None:
    """With no `--account-id`, the create targets the lowest `custom:book` entry, not the first."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()
    calls: list[tuple[str, dict[str, Any]]] = []
    token = _token_with_claims(
        {"custom:tenant": SMOKE_TENANT, "custom:book": f"acct-zzz-last,{ACCOUNT_ID}"}
    )

    results = list(
        checks(
            _make_ctx(client),
            http_caller=_recording_caller(_happy_http_caller(), calls),
            token=token,
            account_id=None,
            out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID,
        )
    )
    stubber.deactivate()

    configured = _check(results, "account_id_configured")
    assert configured.status == "pass"
    assert "custom:book" in configured.detail
    assert not [r for r in results if r.status == "fail"], results
    creates = [body for tool, body in calls if tool == "create_opportunity"]
    assert creates
    assert all(body["account_id"] == ACCOUNT_ID for body in creates)


def test_supplied_token_with_empty_book_claim_fails_naming_the_claim_before_any_call() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    stubber.activate()
    calls: list[tuple[str, dict[str, Any]]] = []
    token = _token_with_claims({"custom:tenant": SMOKE_TENANT, "custom:book": " , "})

    results = list(
        checks(
            _make_ctx(client),
            http_caller=_recording_caller(_happy_http_caller(), calls),
            token=token,
            account_id=None,
            out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID,
        )
    )
    stubber.deactivate()

    configured = _check(results, "account_id_configured")
    assert configured.status == "fail"
    assert "custom:book" in configured.detail
    assert calls == []


def test_explicit_account_id_wins_over_the_book_claim() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()
    calls: list[tuple[str, dict[str, Any]]] = []
    token = _token_with_claims({"custom:tenant": SMOKE_TENANT, "custom:book": "acct-0-first"})

    results = list(
        checks(
            _make_ctx(client),
            http_caller=_recording_caller(_happy_http_caller(), calls),
            token=token,
            account_id=ACCOUNT_ID,
            out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID,
        )
    )
    stubber.deactivate()

    assert not [r for r in results if r.check == "account_id_configured"]
    assert [body["account_id"] for tool, body in calls if tool == "create_opportunity"] == [
        ACCOUNT_ID
    ]


def test_out_of_book_target_is_derived_as_a_seeded_account_read_back_outside_the_book() -> None:
    """The derived 403 target is the first seeded `acct-<tenant>-NNNN` not in the book, read back."""
    book = [f"acct-{SMOKE_TENANT}-0000", f"acct-{SMOKE_TENANT}-0001"]
    derived = f"acct-{SMOKE_TENANT}-0002"
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    _stub_get_account(stubber, derived, tenant=SMOKE_TENANT)
    stubber.activate()
    calls: list[tuple[str, dict[str, Any]]] = []
    token = _token_with_claims({"custom:tenant": SMOKE_TENANT, "custom:book": ",".join(book)})

    results = list(
        checks(
            _make_ctx(client),
            http_caller=_recording_caller(_happy_http_caller(denial_status=403), calls),
            token=token,
            account_id=ACCOUNT_ID,
            out_of_book_account_id=None,
        )
    )
    stubber.assert_no_pending_responses()
    stubber.deactivate()

    denial = _check(results, "out_of_book_refused_403")
    assert denial.status == "pass"
    assert derived in denial.detail
    assert [body["account_id"] for tool, body in calls if tool == "get_account"] == [derived]


def test_derived_out_of_book_candidate_of_another_tenant_is_never_used() -> None:
    """A candidate row carrying a different tenant would test tenancy, not the book: refused."""
    book = [f"acct-{SMOKE_TENANT}-0000", f"acct-{SMOKE_TENANT}-0001"]
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    _stub_get_account(stubber, f"acct-{SMOKE_TENANT}-0002", tenant="eval-someone-else")
    _stub_get_account(stubber, f"acct-{SMOKE_TENANT}-0003", tenant=None)
    stubber.activate()
    calls: list[tuple[str, dict[str, Any]]] = []
    token = _token_with_claims({"custom:tenant": SMOKE_TENANT, "custom:book": ",".join(book)})

    results = list(
        checks(
            _make_ctx(client),
            http_caller=_recording_caller(_happy_http_caller(), calls),
            token=token,
            account_id=ACCOUNT_ID,
            out_of_book_account_id=None,
        )
    )
    stubber.deactivate()

    denial = _check(results, "out_of_book_refused_403")
    assert denial.status == "fail"
    assert "LookupError" in denial.detail
    assert not [tool for tool, _ in calls if tool == "get_account"]


@pytest.mark.parametrize("kind", ["not_a_jwt", "forged"])
def test_an_unverifiable_token_is_refused_before_any_call_even_with_a_tenant_flag(
    kind: str,
) -> None:
    """With `--tenant` set the token's tenant claim is not needed, yet an unverifiable token is
    still refused at the tenant guard, before any HTTP call or DynamoDB read."""
    token = (
        "not-a-jwt"
        if kind == "not_a_jwt"
        else test_key.TestKey().sign(
            test_key.id_token_claims(**{"custom:tenant": SMOKE_TENANT, "custom:book": ACCOUNT_ID})
        )
    )
    client = _dynamodb_client()
    stubber = Stubber(client)
    stubber.activate()
    calls: list[tuple[str, dict[str, Any]]] = []

    results = list(
        checks(
            _make_ctx(client),
            http_caller=_recording_caller(_happy_http_caller(), calls),
            token=token,
            account_id=None,
            out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID,
        )
    )
    stubber.deactivate()

    refused = _check(results, "token_tenant_is_disposable_smoke_tenant")
    assert refused.status == "fail"
    assert "failed verification" in refused.detail
    assert calls == []
    assert [r.check for r in results] == ["token_tenant_is_disposable_smoke_tenant"]


def test_malformed_book_claim_without_account_id_fails_naming_the_malformed_claim() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    stubber.activate()
    token = _token_with_claims({"custom:tenant": SMOKE_TENANT, "custom:book": 7})

    results = list(
        checks(
            _make_ctx(client),
            http_caller=_happy_http_caller(),
            token=token,
            account_id=None,
            out_of_book_account_id=OUT_OF_BOOK_ACCOUNT_ID,
        )
    )
    stubber.deactivate()

    configured = _check(results, "account_id_configured")
    assert configured.status == "fail"
    assert "custom:book claim is malformed" in configured.detail


def test_malformed_book_claim_is_named_in_the_out_of_book_fail_and_no_target_is_guessed() -> None:
    """With an explicit in-book account, an unparseable book still blocks out-of-book derivation:
    a guessed candidate might be in the real book."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_full_round_trip(stubber)
    stubber.activate()
    calls: list[tuple[str, dict[str, Any]]] = []
    token = _token_with_claims({"custom:tenant": SMOKE_TENANT, "custom:book": 7})

    results = list(
        checks(
            _make_ctx(client),
            http_caller=_recording_caller(_happy_http_caller(), calls),
            token=token,
            account_id=ACCOUNT_ID,
            out_of_book_account_id=None,
        )
    )
    stubber.assert_no_pending_responses()
    stubber.deactivate()

    denial = _check(results, "out_of_book_refused_403")
    assert denial.status == "fail"
    assert "custom:book claim is malformed" in denial.detail
    assert not [tool for tool, _ in calls if tool == "get_account"]
