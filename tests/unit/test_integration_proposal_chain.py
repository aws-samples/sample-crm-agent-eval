# SPDX-License-Identifier: MIT
"""Offline self-test for `scripts/smoke/integration_proposal_chain.py`.

No AWS credential or network call: DynamoDB is `moto`-mocked, and every HTTP call the script would
issue is dispatched in-process to the real action-Lambda handlers (`crm.handlers.*.handler`) via a
fake `HttpCaller`. Each refusal assertion is shown to fail both on the response a violating
implementation would produce and end to end with one guarantee patched out of the production code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import boto3
import pytest
from moto import mock_aws
from scripts.smoke import verified_claims
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.integration_proposal_chain import (
    _CREATED_STAGE,
    _PROPOSED_STAGE,
    RESOURCE_CLASS,
    HttpResult,
    SmokeIdentity,
    SuppliedTokenLease,
    _atomicity_check,
    _audit_actor_and_timestamp_check,
    _audit_snapshots_check,
    _created_row_keys,
    _leg_from,
    _make_transaction_key,
    _refused_with_conflict,
    _refused_with_validation_failed,
    _unchanged_check,
    _writes_only_under_smoke_tenant_check,
    canonical_row_bytes,
    checks,
    row_fingerprint,
)

from crm.envelope import IDENTITY_KEY
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import BY_NAME
from eval.tenancy import teardown_tenant
from tests.unit import _jwks_test_key as test_key

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "eval-smoke-test-run"
_ACCOUNT_ID = "acct-smoke-1"
_REP_ID = "rep-smoke"
_TOKEN = "fake-token"  # noqa: S105  # nosec B105 - not a credential, just a non-None sentinel this script checks
_DOMAIN = "smoke.example.test"


@pytest.fixture
def dynamodb_client() -> Iterator[Any]:
    """A moto-mocked DynamoDB client - no credentials, no network, no cost."""
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: Any) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _identity_claims() -> dict[str, Any]:
    """The `_identity` envelope shape `crm.envelope.decode_identity` expects for source=gateway."""
    return {
        "rep_id": _REP_ID,
        "book": [_ACCOUNT_ID],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "smoke-session",
        "source": "gateway",
        "tenant": _TENANT,
    }


def _seed_account(store: Store) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": _ACCOUNT_ID,
            "tenant": _TENANT,
            "tenant_owner_rep_id": f"{_TENANT}#{_REP_ID}",
            "name_lower": "smoke account",
            "name": "Smoke Account",
            "owner_rep_id": _REP_ID,
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
            "created_by": _REP_ID,
            "schema_version": 1,
        },
    )


def _make_handler_http_caller(store: Store) -> Any:
    """Build an `HttpCaller` dispatching `/api/<tool_name>` calls to the real handler in-process.

    Mirrors `POST {apiBaseUrl}/{tool_name}`: the final path segment names the tool, the body is the
    tool's JSON arguments, and the identity is injected as `_identity`. Returns `(status, body)`,
    where an error body is `CrmError.to_api_response()`.
    """

    def caller(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        del method, headers
        tool_name = url.rsplit("/", 1)[-1]
        tool = BY_NAME[tool_name]
        arguments = json.loads(body) if body else {}
        event = {**arguments, IDENTITY_KEY: _identity_claims()}
        module = __import__(tool.handler.rsplit(".handler", 1)[0], fromlist=["handler"])
        response = module.handler(event, None, store=store)
        status_code = int(response["statusCode"])
        response_body = response.get("body") if status_code < 400 else response
        return status_code, json.dumps(response_body, default=str)

    return caller


class _FakeLease:
    """An offline `IdentityLease`: a fixed identity, released by a real scoped teardown.

    `release` calls the real `eval.tenancy.teardown_tenant`.
    """

    def __init__(self, store: Store, *, tenant: str = _TENANT, teardown: bool = True) -> None:
        self._store = store
        self._tenant = tenant
        self._teardown = teardown

    def acquire(self) -> SmokeIdentity:
        return SmokeIdentity(
            token=_TOKEN,
            tenant=self._tenant,
            rep_id=_REP_ID,
            account_id=_ACCOUNT_ID,
            detail="offline fake lease",
            book=frozenset({_ACCOUNT_ID}),
        )

    def release(self) -> Iterable[CheckResult]:
        if not self._teardown:
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="fail",
                detail="teardown of 'eval-smoke-test-run' failed: RuntimeError: boom",
            )
            return
        if self._tenant == "demo":
            # `eval.tenancy.teardown_tenant` refuses `demo` structurally; a lease that acquired
            # nothing under it has nothing to release either.
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="pass",
                detail="nothing was provisioned, so nothing was released",
            )
            return
        deleted = teardown_tenant(self._store, tenant=self._tenant)
        yield CheckResult(
            resource="smoke_tenant",
            check="tenant_left_clean",
            status="pass",
            detail=f"tenant {self._tenant!r} torn down ({sum(deleted.values())} items)",
        )


def _test_context(store: Store) -> Any:
    return context_for_test(
        clients={"dynamodb": store._client},  # noqa: SLF001
        tenant=_TENANT,
        outputs={
            "cloudfront_domain_name": _DOMAIN,
            # `_live_table_prefix` strips the known `eval_runs` suffix to recover the prefix; an
            # unprefixed physical name therefore yields the empty prefix the mocked store uses.
            "dynamodb_table_names": {"eval_runs": "eval_runs"},
        },
    )


def _run_chain(store: Store, *, lease: Any = None) -> list[CheckResult]:
    _seed_account(store)
    return list(
        checks(
            _test_context(store),
            http_caller=_make_handler_http_caller(store),
            lease=lease if lease is not None else _FakeLease(store),
        )
    )


def _find(results: list[CheckResult], resource: str, check: str) -> CheckResult:
    for result in results:
        if result.resource == resource and result.check == check:
            return result
    raise AssertionError(
        f"no check {resource}:{check} in {[(r.resource, r.check) for r in results]}"
    )


# -- full chain happy path ---------------------------------------------------------------------


def test_full_chain_passes_every_check_against_real_handler_behaviour(store: Store) -> None:
    """The whole chain passes against genuine `crm.proposals`/`crm.audit`/teardown behaviour."""
    results = _run_chain(store)
    failed = [(r.resource, r.check, r.detail) for r in results if r.status == "fail"]
    assert not failed, f"unexpected failures: {failed}"
    skipped = [(r.resource, r.check, r.detail) for r in results if r.status == "skip"]
    assert not skipped, f"no step should be skipped on the happy path: {skipped}"


@pytest.mark.parametrize(
    ("resource", "check"),
    [
        ("submit", "proposal_mutates_nothing"),
        ("submit", "proposal_writes_no_audit_record"),
        ("submit", "proposal_row_is_pending"),
        ("approve", "commits_the_change"),
        ("approve", "derived_index_attributes_recomputed"),
        ("approve", "proposal_reachable_by_approved_status_index"),
        ("approve", "writes_exactly_one_audit_record"),
        ("approve", "commit_and_audit_are_atomic"),
        ("approve", "audit_carries_before_and_after_snapshots"),
        ("approve", "audit_carries_actor_and_timestamp"),
        ("approve", "proposal_row_is_approved"),
        ("approve_twice", "refused_with_conflict"),
        ("approve_twice", "left_neither_half_behind"),
        ("revert", "restores_the_pre_approval_row"),
        ("revert", "audit_record_marked_reversed"),
        ("revert_twice", "refused_with_conflict"),
        ("revert_twice", "restored_nothing_a_second_time"),
        ("stale_approval", "refused_with_conflict"),
        ("stale_approval", "conflict_reason_is_fields_changed"),
        ("stale_approval", "target_unchanged_by_the_refusal"),
        ("stale_approval", "proposal_still_pending"),
        ("stale_approval", "refusal_wrote_no_audit_record"),
        ("reject", "proposal_row_is_rejected"),
        ("reject", "target_mutates_nothing"),
        ("reject", "approval_after_rejection_refused_with_conflict"),
        ("reject_twice", "refused_with_conflict"),
        (RESOURCE_CLASS, "demo_tenant_untouched"),
        (RESOURCE_CLASS, "writes_only_under_smoke_tenant"),
        ("smoke_tenant", "tenant_left_clean"),
        ("smoke_tenant", "opportunities_rows_removed"),
        ("smoke_tenant", "proposals_rows_removed"),
        ("smoke_tenant", "contacts_rows_removed"),
        ("smoke_tenant", "opportunities_empty_for_tenant"),
        ("smoke_tenant", "proposals_empty_for_tenant"),
        ("smoke_tenant", "accounts_empty_for_tenant"),
        ("smoke_tenant", "contacts_empty_for_tenant"),
        ("smoke_tenant", "audit_records_empty_for_tenant"),
        ("delete_proposal", "accepted_on_an_unreferenced_contact"),
        ("delete_proposal", "proposal_row_records_the_delete"),
        ("delete_proposal", "target_mutates_nothing"),
        ("delete_proposal", "wrote_no_audit_record"),
        ("delete_proposal", "refused_on_an_account_with_an_open_opportunity"),
        ("payload_allowlist", "key_redirection_refused"),
        ("payload_allowlist", "re_tenanting_refused"),
        ("payload_allowlist", "derived_attribute_supply_refused"),
        ("payload_allowlist", "redirect_victim_row_unchanged"),
        ("payload_allowlist", "declared_target_unchanged"),
        ("payload_allowlist", "no_proposal_stored_for_a_refused_payload"),
        ("payload_allowlist", "permitted_union_payload_accepted"),
        ("out_of_book_revert", "fixture_seeded_outside_the_book"),
        ("out_of_book_revert", "note_revert_refused_with_scope_denied"),
        ("out_of_book_revert", "note_revert_changed_nothing"),
        ("out_of_book_revert", "delete_revert_refused_with_scope_denied"),
        ("out_of_book_revert", "delete_revert_changed_nothing"),
    ],
)
def test_every_named_assertion_is_present_and_passing(
    store: Store, resource: str, check: str
) -> None:
    """Each acceptance clause is a separately named, separately passing check in the inventory."""
    assert _find(_run_chain(store), resource, check).status == "pass"


def test_the_chain_uses_one_independent_target_record_per_leg(store: Store) -> None:
    """No two legs share a target record: seven opportunities and one account for the delete leg."""
    _seed_account(store)
    recorded: list[tuple[str, str, str]] = []
    inner = _make_handler_http_caller(store)

    def spy(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        recorded.append((method, url, body))
        result: HttpResult = inner(method, url, headers, body)
        return result

    list(checks(_test_context(store), http_caller=spy, lease=_FakeLease(store)))
    created = _created_row_keys(recorded)
    # Six legs' targets plus the note-revert leg's.
    assert len(created["opportunities"]) == 7, created
    # The delete leg's contact and the delete-revert leg's.
    assert len(created["contacts"]) == 2, created
    # Ten submit_proposal calls: three stage-change legs, the delete leg's accepted proposal and
    # its refused one, the payload-allowlist leg's three refused payloads plus its accepted
    # one, and the delete-revert leg's delete (recorded from the call body, so the teardown proof
    # covers proposals that were refused and therefore never written, and asserts their absence).
    assert len(created["proposals"]) == 10, created


# -- the demo tenant is refused before any call is made ----------------------------------------


def test_a_demo_tenant_identity_is_refused_before_any_write(store: Store) -> None:
    """An identity naming `demo` produces one failing check and zero HTTP calls."""
    _seed_account(store)
    calls: list[str] = []
    inner = _make_handler_http_caller(store)

    def spy(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        calls.append(url)
        result: HttpResult = inner(method, url, headers, body)
        return result

    results = list(
        checks(_test_context(store), http_caller=spy, lease=_FakeLease(store, tenant="demo"))
    )
    assert calls == [], f"a demo-tenant run issued live calls: {calls}"
    identity_check = _find(results, RESOURCE_CLASS, "identity_acquired")
    assert identity_check.status == "fail"
    assert "demo" in identity_check.detail


def _no_tenant_flag_context(store: Store) -> Any:
    """A context with no `--tenant`, the pinned CLI's own shape: the token's claim is the tenant."""
    return context_for_test(
        clients={"dynamodb": store._client},  # noqa: SLF001
        outputs={
            "cloudfront_domain_name": _DOMAIN,
            "dynamodb_table_names": {"eval_runs": "eval_runs"},
            "cognito_user_pool_id": test_key.USER_POOL_ID,
            "cognito_spa_client_id": test_key.CLIENT_ID,
        },
    )


@pytest.fixture(autouse=True)
def _loopback_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens verify against the loopback test JWKS instead of the pool's public endpoint."""
    monkeypatch.setattr(verified_claims, "cognito_jwks_url", lambda _r, _p: test_key.jwks_url())


def test_supplied_token_lease_refuses_a_forged_token(store: Store) -> None:
    """A smoke-tenant token signed by an unpublished key is refused before its claims are used."""
    forged = test_key.TestKey().sign(test_key.id_token_claims(**{"custom:tenant": _TENANT}))
    lease = SuppliedTokenLease(_no_tenant_flag_context(store), token=forged, account_id=None)
    with pytest.raises(RuntimeError, match="failed verification"):
        lease.acquire()


def test_supplied_token_lease_refuses_a_demo_tenant_claim(store: Store) -> None:
    lease = SuppliedTokenLease(
        _no_tenant_flag_context(store),
        token=_jwt({"custom:tenant": "demo"}),
        account_id=_ACCOUNT_ID,
    )
    with pytest.raises(RuntimeError, match="demo"):
        lease.acquire()


def test_supplied_token_lease_refuses_a_token_with_no_tenant_claim(store: Store) -> None:
    lease = SuppliedTokenLease(
        _no_tenant_flag_context(store), token=_jwt({}), account_id=_ACCOUNT_ID
    )
    with pytest.raises(RuntimeError, match="custom:tenant"):
        lease.acquire()


def test_supplied_token_lease_refuses_a_non_disposable_tenant_claim(store: Store) -> None:
    """Only a throwaway `eval-` tenant may be written by a smoke run."""
    lease = SuppliedTokenLease(
        _no_tenant_flag_context(store),
        token=_jwt({"custom:tenant": "acme-production"}),
        account_id=_ACCOUNT_ID,
    )
    with pytest.raises(RuntimeError):
        lease.acquire()


def test_supplied_token_lease_refuses_a_token_with_no_account_id(store: Store) -> None:
    lease = SuppliedTokenLease(
        _no_tenant_flag_context(store), token=_jwt({"custom:tenant": _TENANT}), account_id=None
    )
    with pytest.raises(RuntimeError, match="account-id"):
        lease.acquire()


def test_supplied_token_lease_derives_account_from_book_claim(store: Store) -> None:
    """With no account id, the lease takes the lowest `custom:book` entry, not the first listed."""
    lease = SuppliedTokenLease(
        _no_tenant_flag_context(store),
        token=_jwt({"custom:tenant": _TENANT, "custom:book": "acct-x-0001,acct-x-0000"}),
        account_id=None,
    )
    identity = lease.acquire()
    assert identity.account_id == "acct-x-0000"
    assert "custom:book" in identity.detail
    assert identity.book == frozenset({"acct-x-0000", "acct-x-0001"})


def test_supplied_token_lease_empty_book_claim_raises(store: Store) -> None:
    lease = SuppliedTokenLease(
        _no_tenant_flag_context(store),
        token=_jwt({"custom:tenant": _TENANT, "custom:book": ""}),
        account_id=None,
    )
    with pytest.raises(RuntimeError, match="custom:book"):
        lease.acquire()


def test_supplied_token_lease_explicit_account_id_wins_over_book_claim(store: Store) -> None:
    lease = SuppliedTokenLease(
        _no_tenant_flag_context(store),
        token=_jwt({"custom:tenant": _TENANT, "custom:book": "acct-x-0000"}),
        account_id=_ACCOUNT_ID,
    )
    assert lease.acquire().account_id == _ACCOUNT_ID


def _jwt(claims: Mapping[str, Any]) -> str:
    """A signed ID token for the test pool carrying `claims`."""
    return test_key.id_token(**claims)


def test_identity_acquisition_failure_is_a_failing_check_and_still_releases(store: Store) -> None:
    class _Failing:
        def __init__(self) -> None:
            self.released = False

        def acquire(self) -> SmokeIdentity:
            raise RuntimeError("could not mint a token")

        def release(self) -> Iterable[CheckResult]:
            self.released = True
            return ()

    lease = _Failing()
    results = list(
        checks(_test_context(store), http_caller=_make_handler_http_caller(store), lease=lease)
    )
    assert [r.status for r in results] == ["fail"]
    assert "could not mint a token" in results[0].detail
    assert lease.released is True


# -- assertion 1: "a proposal mutates nothing", proven byte-wise -------------------------------


def test_unchanged_check_passes_on_an_identical_row() -> None:
    row = {"opportunity_id": "opp-1", "stage": "Discovery", "name": "Acme"}
    assert (
        _unchanged_check(resource="submit", check="c", before=row, after=dict(row), what="x").status
        == "pass"
    )


def test_unchanged_check_fails_when_a_field_changed() -> None:
    """Non-vacuity: a proposal implementation that DID mutate the target must fail this check."""
    before = {"opportunity_id": "opp-1", "stage": "Discovery"}
    after = {"opportunity_id": "opp-1", "stage": "Negotiation"}
    outcome = _unchanged_check(resource="submit", check="c", before=before, after=after, what="x")
    assert outcome.status == "fail"
    assert "CHANGED" in outcome.detail


def test_unchanged_check_fails_when_an_attribute_was_merely_added() -> None:
    """A new attribute stamped onto the target at proposal time fails the byte-wise comparison."""
    before = {"opportunity_id": "opp-1", "stage": "Discovery"}
    after = {"opportunity_id": "opp-1", "stage": "Discovery", "pending_proposal_id": "prop-1"}
    assert (
        _unchanged_check(resource="submit", check="c", before=before, after=after, what="x").status
        == "fail"
    )


def test_unchanged_check_distinguishes_a_deleted_row_from_an_unchanged_one() -> None:
    row = {"opportunity_id": "opp-1"}
    assert (
        _unchanged_check(resource="submit", check="c", before=row, after=None, what="x").status
        == "fail"
    )
    assert canonical_row_bytes(None) == b""
    assert row_fingerprint(None) == "absent"


def test_canonical_bytes_ignore_key_order_only() -> None:
    assert canonical_row_bytes({"a": 1, "b": 2}) == canonical_row_bytes({"b": 2, "a": 1})
    assert canonical_row_bytes({"a": 1}) != canonical_row_bytes({"a": "1"})


# -- assertion 2: approval commits and audits atomically ---------------------------------------


def test_atomicity_check_passes_only_when_both_halves_are_present() -> None:
    assert _atomicity_check(change_committed=True, audit_written=True).status == "pass"


@pytest.mark.parametrize(
    ("change_committed", "audit_written", "expected_marker"),
    [
        (True, False, "HALF-COMMITTED"),
        (False, True, "HALF-COMMITTED"),
        (False, False, "neither"),
    ],
)
def test_atomicity_check_fails_on_every_half_state(
    change_committed: bool, audit_written: bool, expected_marker: str
) -> None:
    outcome = _atomicity_check(change_committed=change_committed, audit_written=audit_written)
    assert outcome.status == "fail"
    assert expected_marker in outcome.detail


def test_audit_snapshots_check_passes_when_both_snapshots_byte_match_the_row() -> None:
    before = {"opportunity_id": "opp-1", "stage": "Discovery"}
    after = {"opportunity_id": "opp-1", "stage": "Negotiation"}
    outcome = _audit_snapshots_check(
        audit_row={"before_snapshot": dict(before), "after_snapshot": dict(after)},
        before_approve=before,
        after_approve=after,
    )
    assert outcome.status == "pass"


@pytest.mark.parametrize(
    "audit_row",
    [
        {"before_snapshot": {"stage": "wrong"}, "after_snapshot": {"stage": "Negotiation"}},
        {"before_snapshot": {"stage": "Discovery"}, "after_snapshot": {"stage": "wrong"}},
        {"after_snapshot": {"stage": "Negotiation"}},
        {"before_snapshot": "not-a-mapping", "after_snapshot": {"stage": "Negotiation"}},
    ],
)
def test_audit_snapshots_check_fails_on_any_snapshot_defect(audit_row: dict[str, Any]) -> None:
    """An audit record that records the wrong state, or no state, is not an audit record."""
    outcome = _audit_snapshots_check(
        audit_row=audit_row,
        before_approve={"stage": "Discovery"},
        after_approve={"stage": "Negotiation"},
    )
    assert outcome.status == "fail"


def test_audit_actor_and_timestamp_check_passes_on_the_acting_rep_and_a_utc_timestamp() -> None:
    identity = SmokeIdentity(
        token=_TOKEN, tenant=_TENANT, rep_id=_REP_ID, account_id=_ACCOUNT_ID, detail=""
    )
    outcome = _audit_actor_and_timestamp_check(
        audit_row={"actor_rep_id": _REP_ID, "created_at": "2026-09-04T03:00:00+00:00"},
        identity=identity,
    )
    assert outcome.status == "pass"


@pytest.mark.parametrize(
    "audit_row",
    [
        {"actor_rep_id": "someone-else", "created_at": "2026-09-04T03:00:00+00:00"},
        {"actor_rep_id": _REP_ID, "created_at": "not-a-timestamp"},
        {"actor_rep_id": _REP_ID, "created_at": ""},
        {"created_at": "2026-09-04T03:00:00+00:00"},
        {"actor_rep_id": _REP_ID, "created_at": "2026-09-04T03:00:00"},
    ],
)
def test_audit_actor_and_timestamp_check_fails_without_both(audit_row: dict[str, Any]) -> None:
    identity = SmokeIdentity(
        token=_TOKEN, tenant=_TENANT, rep_id=_REP_ID, account_id=_ACCOUNT_ID, detail=""
    )
    assert _audit_actor_and_timestamp_check(audit_row=audit_row, identity=identity).status == "fail"


def test_a_non_atomic_approval_that_writes_no_audit_record_fails_the_chain(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval patched to commit without the audit write in its transaction is reported as
    `HALF-COMMITTED`.
    """
    import crm.handlers.approve_proposal as approve_module
    from crm.proposals import approve_proposal as real_approve

    def approve_without_audit(store_arg: Any, **kwargs: Any) -> Any:
        kwargs["record_audit"] = None
        return real_approve(store_arg, **kwargs)

    monkeypatch.setattr(approve_module, "approve_proposal", approve_without_audit)
    results = _run_chain(store)
    atomicity = _find(results, "approve", "commit_and_audit_are_atomic")
    assert atomicity.status == "fail"
    assert "HALF-COMMITTED" in atomicity.detail
    assert _find(results, "approve", "writes_exactly_one_audit_record").status == "fail"
    # The change itself still committed, so this must NOT be reported as "committed neither".
    assert _find(results, "approve", "commits_the_change").status == "pass"


# -- assertion 3: a stale snapshot is refused with Conflict ------------------------------------


def test_refused_with_conflict_passes_on_the_taxonomy_member_it_names() -> None:
    result: HttpResult = (409, json.dumps({"error": {"code": "conflict", "message": "stale"}}))
    assert _refused_with_conflict(result, resource="r", check="c", what="x").status == "pass"


# -- the delete-proposal leg -------------------------------------------------------------------


def test_refused_with_validation_failed_passes_only_on_that_member() -> None:
    """Only `ValidationFailed` passes; a `401 authentication_failed` (a missing `dynamodb:Query`
    grant on this path) does not.
    """
    passing: HttpResult = (
        400,
        json.dumps({"error": {"code": "validation_failed", "message": "open opportunities"}}),
    )
    assert (
        _refused_with_validation_failed(passing, resource="r", check="c", what="x").status == "pass"
    )
    for result in (
        (200, json.dumps({"proposal": {"status": "pending"}})),
        (401, json.dumps({"error": {"code": "authentication_failed", "message": "x"}})),
        (409, json.dumps({"error": {"code": "conflict", "message": "x"}})),
        (400, json.dumps({"error": {"code": "scope_denied", "message": "x"}})),
        "transport failure",
    ):
        outcome = _refused_with_validation_failed(result, resource="r", check="c", what="x")
        assert outcome.status == "fail", result


def test_the_delete_leg_reports_the_missing_query_grant_shape_as_a_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cascade query raising a wrapped `AccessDeniedException` fails the delete leg."""
    import crm.handlers.submit_proposal as submit_module
    from crm.errors import AuthenticationFailed

    def denied(*_args: Any, **_kwargs: Any) -> None:
        raise AuthenticationFailed("The request was not authorised.")

    monkeypatch.setattr(submit_module, "reject_contact_delete_if_referenced", denied, raising=True)
    results = _run_chain(store)
    accepted = _find(results, "delete_proposal", "accepted_on_an_unreferenced_contact")
    assert accepted.status == "fail"
    assert "401" in accepted.detail or "authentication_failed" in accepted.detail
    # And the assertions that depended on that proposal are skipped, never silently passed.
    assert _find(results, "delete_proposal", "proposal_row_records_the_delete").status == "skip"


def test_the_delete_leg_fails_when_the_cascade_stops_refusing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cascade that never refuses deleting an account with open pipeline fails the delete leg."""
    import crm.handlers.submit_proposal as submit_module

    monkeypatch.setattr(
        submit_module,
        "reject_account_delete_with_open_opportunities",
        lambda *_args, **_kwargs: None,
        raising=True,
    )
    results = _run_chain(store)
    refused = _find(results, "delete_proposal", "refused_on_an_account_with_an_open_opportunity")
    assert refused.status == "fail"
    assert "SUCCEEDED" in refused.detail


def test_the_delete_proposal_leg_writes_no_audit_record_and_mutates_no_account(
    store: Store,
) -> None:
    """A delete proposal is still only a proposal: it commits nothing and audits nothing."""
    results = _run_chain(store)
    assert _find(results, "delete_proposal", "target_mutates_nothing").status == "pass"
    assert _find(results, "delete_proposal", "wrote_no_audit_record").status == "pass"


def test_leg_from_names_the_tail_it_skips_and_refuses_an_unknown_check() -> None:
    """A skip must cover exactly the assertions from a named one onward, not a counted offset."""
    leg = (("a", "one"), ("b", "two"), ("c", "three"))
    assert _leg_from(leg, "b", "two") == (("b", "two"), ("c", "three"))
    with pytest.raises(KeyError):
        _leg_from(leg, "b", "nope")


@pytest.mark.parametrize(
    "result",
    [
        (200, json.dumps({"proposal": {"status": "approved"}})),
        (400, json.dumps({"error": {"code": "validation_failed", "message": "x"}})),
        (403, json.dumps({"error": {"code": "scope_denied", "message": "x"}})),
        (404, json.dumps({"error": {"code": "not_found", "message": "x"}})),
        (500, json.dumps({"error": {"code": "internal_error", "message": "x"}})),
        (409, json.dumps({"error": {"code": "not_found", "message": "x"}})),
        (409, "not json at all"),
        "transport error calling https://smoke.example.test/api/approve_proposal: boom",
    ],
)
def test_refused_with_conflict_fails_on_anything_that_is_not_conflict(result: Any) -> None:
    """Only a `conflict` passes; a success, any other member, or a 409 with another code fails."""
    outcome = _refused_with_conflict(result, resource="r", check="c", what="x")
    assert outcome.status == "fail"


def test_refused_with_conflict_names_a_success_as_the_dangerous_outcome() -> None:
    outcome = _refused_with_conflict(
        (200, "{}"), resource="r", check="c", what="approving a stale proposal"
    )
    assert "SUCCEEDED" in outcome.detail


def test_a_snapshot_refreshing_approval_fails_the_stale_conflict_assertion(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval patched to refresh a stale before-snapshot instead of refusing it fails."""
    import crm.handlers.approve_proposal as approve_module
    from crm.proposals import approve_proposal as real_approve

    def approve_after_refreshing_snapshot(store_arg: Any, **kwargs: Any) -> Any:
        proposal = store_arg.get_item("proposals", {"proposal_id": kwargs["proposal_id"]})
        if proposal is not None and proposal["target_table"] == "opportunities":
            current = store_arg.get_item(
                str(proposal["target_table"]), {"opportunity_id": str(proposal["target_id"])}
            )
            if current is not None:
                store_arg.put_item("proposals", {**proposal, "before_snapshot": current})
        return real_approve(store_arg, **kwargs)

    monkeypatch.setattr(approve_module, "approve_proposal", approve_after_refreshing_snapshot)
    results = _run_chain(store)
    stale = _find(results, "stale_approval", "refused_with_conflict")
    assert stale.status == "fail"
    assert "SUCCEEDED" in stale.detail
    assert _find(results, "stale_approval", "target_unchanged_by_the_refusal").status == "fail"


def test_a_stale_refusal_without_the_fields_changed_reason_fails_the_reason_check(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 409 that does not say `fields_changed` fails the stale leg's reason check."""
    import crm.handlers.approve_proposal as approve_module
    from crm.errors import Conflict
    from crm.proposals import approve_proposal as real_approve

    def approve_with_a_bare_conflict_for_the_stale_leg(store_arg: Any, **kwargs: Any) -> Any:
        proposal = store_arg.get_item("proposals", {"proposal_id": kwargs["proposal_id"]})
        payload = proposal.get("after_payload") if proposal is not None else None
        if isinstance(payload, dict) and "name" in payload:
            raise Conflict("The record changed.")
        return real_approve(store_arg, **kwargs)

    monkeypatch.setattr(
        approve_module, "approve_proposal", approve_with_a_bare_conflict_for_the_stale_leg
    )
    results = _run_chain(store)
    assert _find(results, "stale_approval", "refused_with_conflict").status == "pass"
    reason = _find(results, "stale_approval", "conflict_reason_is_fields_changed")
    assert reason.status == "fail"
    assert "None" in reason.detail


# -- assertion 4: revert restores once and refuses twice ---------------------------------------


def test_a_repeatable_revert_fails_the_refuse_twice_assertion(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revert path patched so a second revert succeeds fails the chain.

    The patch resets the audit record to `active` and restores the target's recorded after-state.
    """
    import crm.handlers.revert_audit_record as revert_module
    from crm.audit import revert as real_revert

    def revert_after_defeating_both_guards(identity: Any, store_arg: Any, **kwargs: Any) -> Any:
        item = store_arg.get_item("audit_records", {"audit_id": kwargs["audit_id"]})
        if item is not None and item.get("status") != "active":
            store_arg.put_item("audit_records", {**item, "status": "active", "reversed_at": None})
            after_snapshot = item.get("after_snapshot")
            if isinstance(after_snapshot, dict):
                store_arg.put_item(str(item["target_table"]), after_snapshot)
        return real_revert(identity, store_arg, **kwargs)

    monkeypatch.setattr(revert_module, "revert", revert_after_defeating_both_guards)
    results = _run_chain(store)
    second = _find(results, "revert_twice", "refused_with_conflict")
    assert second.status == "fail"
    assert "SUCCEEDED" in second.detail
    # The first revert still restored correctly, so that assertion must remain a pass - the
    # failure is specific to idempotency, not smeared across the leg.
    assert _find(results, "revert", "restores_the_pre_approval_row").status == "pass"


def test_reactivating_the_audit_record_alone_is_still_refused(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resetting the audit record's `status` to `active` alone does not make a second revert succeed:
    the after-snapshot condition check cancels the transaction on its own.
    """
    import crm.handlers.revert_audit_record as revert_module
    from crm.audit import revert as real_revert

    def revert_after_reactivating(identity: Any, store_arg: Any, **kwargs: Any) -> Any:
        item = store_arg.get_item("audit_records", {"audit_id": kwargs["audit_id"]})
        if item is not None and item.get("status") != "active":
            store_arg.put_item("audit_records", {**item, "status": "active", "reversed_at": None})
        return real_revert(identity, store_arg, **kwargs)

    monkeypatch.setattr(revert_module, "revert", revert_after_reactivating)
    results = _run_chain(store)
    assert _find(results, "revert_twice", "refused_with_conflict").status == "pass"


# -- tenant scoping, teardown, and cleanliness -------------------------------------------------


def test_teardown_failure_is_reported_as_a_failing_check_not_swallowed(store: Store) -> None:
    """A teardown failure is reported as a failing check."""
    results = _run_chain(store, lease=_FakeLease(store, teardown=False))
    teardown_result = _find(results, "smoke_tenant", "tenant_left_clean")
    assert teardown_result.status == "fail"
    assert "boom" in teardown_result.detail


def test_teardown_proof_fails_when_proposal_and_audit_rows_survive(store: Store) -> None:
    """A lease whose release does not tear the tenant down fails the per-table emptiness checks."""

    class _NoOpLease(_FakeLease):
        def release(self) -> Iterable[CheckResult]:
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="pass",
                detail="claimed clean without doing anything",
            )

    results = _run_chain(store, lease=_NoOpLease(store))
    assert _find(results, "smoke_tenant", "proposals_empty_for_tenant").status == "fail"
    assert _find(results, "smoke_tenant", "audit_records_empty_for_tenant").status == "fail"
    assert _find(results, "smoke_tenant", "opportunities_empty_for_tenant").status == "fail"
    assert _find(results, "smoke_tenant", "proposals_rows_removed").status == "fail"


def test_writes_only_under_smoke_tenant_fails_on_a_demo_marker() -> None:
    body = json.dumps({"transaction_key": _make_transaction_key("demo", "step")})
    outcome = next(iter(_writes_only_under_smoke_tenant_check([("POST", "u", body)], _TENANT)))
    assert outcome.status == "fail"
    assert "demo marker" in outcome.detail


def test_writes_only_under_smoke_tenant_fails_on_another_tenants_marker() -> None:
    body = json.dumps({"transaction_key": _make_transaction_key("eval-other-run", "step")})
    outcome = next(iter(_writes_only_under_smoke_tenant_check([("POST", "u", body)], _TENANT)))
    assert outcome.status == "fail"


def test_writes_only_under_smoke_tenant_fails_when_no_write_was_observed() -> None:
    """An unobserved boundary is not a passing one: zero writes must not read as compliance."""
    outcome = next(iter(_writes_only_under_smoke_tenant_check([], _TENANT)))
    assert outcome.status == "fail"
    assert "unobserved" in outcome.detail


def test_demo_tenant_untouched_fails_when_a_demo_row_appears(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity: the delta assertion must catch a `demo` row that appears mid-run."""
    import scripts.smoke.integration_proposal_chain as module

    real_count = module._tenant_row_count  # noqa: SLF001
    seen: list[int] = []

    def counting(ctx: Any, table: str, tenant: str) -> int:
        if tenant == "demo":
            seen.append(1)
            return len(seen)  # a different count on every demo read
        return real_count(ctx, table, tenant)

    monkeypatch.setattr(module, "_tenant_row_count", counting)
    results = _run_chain(store)
    outcome = _find(results, RESOURCE_CLASS, "demo_tenant_untouched")
    assert outcome.status == "fail"
    assert "changed" in outcome.detail


def test_transaction_keys_stay_within_dynamodbs_client_request_token_cap() -> None:
    """The smoke's keys stay within 36 characters, inside the 128-character `transaction_key` bound."""
    tenant = "eval-smoke-" + "0" * 32
    for step in ("approval-leg-submit", "stale-leg-approve", "reject-leg-reject-2"):
        key = _make_transaction_key(tenant, step)
        assert len(key) <= 36, f"{key!r} is {len(key)} characters"
    assert _make_transaction_key(tenant, "a") != _make_transaction_key(tenant, "b")
    assert _make_transaction_key("eval-a", "a") != _make_transaction_key("eval-b", "a")


def test_created_row_keys_are_derived_from_the_recorded_calls_not_hand_tracked() -> None:
    from crm.handlers._common import idempotent_id

    key = _make_transaction_key(_TENANT, "x")
    calls = [
        ("POST", f"https://{_DOMAIN}/api/create_opportunity", json.dumps({"transaction_key": key})),
        ("POST", f"https://{_DOMAIN}/api/submit_proposal", json.dumps({"transaction_key": key})),
        ("POST", f"https://{_DOMAIN}/api/get_opportunity", json.dumps({"opportunity_id": "o"})),
    ]
    created = _created_row_keys(calls)
    assert created["opportunities"] == {idempotent_id("opp", key)}
    assert created["proposals"] == {idempotent_id("prop", key)}


def test_identity_check_reports_the_rep_and_tenant_it_will_act_as(store: Store) -> None:
    outcome = _find(_run_chain(store), RESOURCE_CLASS, "identity_acquired")
    assert outcome.status == "pass"
    assert _TENANT in outcome.detail
    assert _REP_ID in outcome.detail


def test_identity_is_not_the_module_level_check_name_of_any_other_resource(store: Store) -> None:
    """Every emitted check is unique on `(resource, check)`, so no assertion can silently
    overwrite or shadow another in the inventory."""
    results = _run_chain(store)
    pairs = [(r.resource, r.check) for r in results]
    assert len(pairs) == len(set(pairs)), f"duplicate (resource, check) pairs: {pairs}"


def test_the_approved_row_carries_recomputed_derived_index_attributes(store: Store) -> None:
    """An approved stage change passes the check that its derived index composites name the new
    stage rather than the pre-approval one.
    """
    results = _run_chain(store)
    commits = _find(results, "approve", "commits_the_change")
    assert commits.status == "pass"
    assert "tenant_stage=" in commits.detail
    assert "stage_close_date=" in commits.detail
    assert "asserted separately by derived_index_attributes_recomputed" in commits.detail

    derived = _find(results, "approve", "derived_index_attributes_recomputed")
    assert derived.status == "pass"
    assert f"{_TENANT}#{_PROPOSED_STAGE}" in derived.detail
    assert f"{_TENANT}#{_CREATED_STAGE}" not in derived.detail, (
        "a detail still quoting the pre-approval composite would mean the row is indexed at the "
        "stage it no longer holds"
    )


def test_the_delete_revert_leg_restores_the_record_through_the_real_handlers(store: Store) -> None:
    """An approved delete's audit reverts in scope; the record is back and the audit reversed."""
    results = _run_chain(store)
    for check in (
        "approved_delete_removes_the_record",
        "delete_wrote_one_before_only_audit",
        "revert_succeeds_in_scope",
        "record_restored_from_the_before_snapshot",
        "audit_record_marked_reversed",
    ):
        assert _find(results, "delete_revert", check).status == "pass", check


def test_a_delete_revert_that_refuses_fails_the_leg_and_skips_the_rest(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revert that answers conflict for a delete's record fails its row and skips what follows."""
    import crm.handlers.revert_audit_record as revert_module
    from crm.audit import revert as real_revert
    from crm.errors import Conflict

    def refuse_delete_reverts(identity: Any, store_arg: Any, **kwargs: Any) -> Any:
        item = store_arg.get_item("audit_records", {"audit_id": kwargs["audit_id"]})
        if item is not None and not item.get("after_snapshot"):
            raise Conflict("The operation conflicted with a concurrent change.")
        return real_revert(identity, store_arg, **kwargs)

    monkeypatch.setattr(revert_module, "revert", refuse_delete_reverts)
    results = _run_chain(store)
    assert _find(results, "delete_revert", "revert_succeeds_in_scope").status == "fail"
    for check in ("record_restored_from_the_before_snapshot", "audit_record_marked_reversed"):
        assert _find(results, "delete_revert", check).status == "skip", check


def test_the_note_revert_leg_deletes_the_note_through_the_real_handlers(store: Store) -> None:
    """A note create's audit reverts in scope; the note is gone and the audit is reversed."""
    results = _run_chain(store)
    for check in (
        "create_note_wrote_one_after_only_audit",
        "revert_succeeds_in_scope",
        "note_is_deleted",
        "audit_record_marked_reversed",
    ):
        assert _find(results, "note_revert", check).status == "pass", check


# -- out-of-book reverts ---------------------------------------------------------------------


_OUT_OF_BOOK_ROWS = (
    "note_revert_refused_with_scope_denied",
    "note_revert_changed_nothing",
    "delete_revert_refused_with_scope_denied",
    "delete_revert_changed_nothing",
)


def test_an_out_of_book_revert_that_is_not_scope_checked_fails_every_row(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the revert's scope check patched out, the note is deleted and the contact restored."""
    import crm.audit as audit_module

    def _no_scope_check(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(audit_module, "require_scope", _no_scope_check)
    results = _run_chain(store)
    for check in _OUT_OF_BOOK_ROWS:
        assert _find(results, "out_of_book_revert", check).status == "fail", check
    assert (
        "SUCCEEDED"
        in _find(results, "out_of_book_revert", "note_revert_refused_with_scope_denied").detail
    )


def test_an_out_of_book_revert_refused_with_another_code_fails_the_refusal_row(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import crm.handlers.revert_audit_record as revert_module
    from crm.errors import Conflict

    def _conflict(*_args: Any, **_kwargs: Any) -> Any:
        raise Conflict("refused for another reason")

    monkeypatch.setattr(revert_module, "revert", _conflict)
    results = _run_chain(store)
    row = _find(results, "out_of_book_revert", "delete_revert_refused_with_scope_denied")
    assert row.status == "fail"
    assert "not with ScopeDenied" in row.detail
    assert _find(results, "out_of_book_revert", "delete_revert_changed_nothing").status == "pass"


def test_an_unknown_book_fails_the_fixture_and_skips_the_rest(store: Store) -> None:
    class _BooklessLease(_FakeLease):
        def acquire(self) -> SmokeIdentity:
            identity = super().acquire()
            return SmokeIdentity(
                token=identity.token,
                tenant=identity.tenant,
                rep_id=identity.rep_id,
                account_id=identity.account_id,
                detail=identity.detail,
            )

    results = _run_chain(store, lease=_BooklessLease(store))
    assert _find(results, "out_of_book_revert", "fixture_seeded_outside_the_book").status == "fail"
    assert {_find(results, "out_of_book_revert", c).status for c in _OUT_OF_BOOK_ROWS} == {"skip"}


def test_an_existing_out_of_book_account_is_used_rather_than_a_fixture_one(store: Store) -> None:
    other = "acct-smoke-2"
    store.put_item(
        "accounts",
        {
            "account_id": other,
            "tenant": _TENANT,
            "owner_rep_id": "rep-other",
            "name": "Other",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
            "created_by": "rep-other",
            "schema_version": 1,
        },
    )
    results = _run_chain(store)
    seeded = _find(results, "out_of_book_revert", "fixture_seeded_outside_the_book")
    assert seeded.status == "pass"
    assert repr(other) in seeded.detail
    assert {_find(results, "out_of_book_revert", c).status for c in _OUT_OF_BOOK_ROWS} == {"pass"}


def test_the_out_of_book_read_backs_are_strongly_consistent(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every target and audit read-back around an out-of-book revert sends `ConsistentRead=True`."""
    from scripts.smoke import integration_proposal_chain as module

    reads: list[tuple[str, str, bool]] = []

    class _RecordingStore(Store):
        def get_item(
            self, table: str, key: Mapping[str, str], *, consistent: bool = False
        ) -> dict[str, object] | None:
            reads.append((table, str(next(iter(key.values()))), consistent))
            return super().get_item(table, key, consistent=consistent)

    monkeypatch.setattr(module, "_store", lambda _ctx: _RecordingStore(client=store._client))  # noqa: SLF001
    sent: list[dict[str, Any]] = []
    real_client_get_item = store._client.get_item  # noqa: SLF001

    def recording_client_get_item(**kwargs: Any) -> Any:
        sent.append(kwargs)
        return real_client_get_item(**kwargs)

    monkeypatch.setattr(store._client, "get_item", recording_client_get_item)  # noqa: SLF001
    results = _run_chain(store)
    assert {_find(results, "out_of_book_revert", c).status for c in _OUT_OF_BOOK_ROWS} == {"pass"}

    consistent = [(table, key) for table, key, flag in reads if flag]
    assert sorted(table for table, _ in consistent) == sorted(
        ["audit_records"] * 4 + ["notes"] * 2 + ["contacts"] * 2
    )
    oob_targets = [r for r in reads if r[0] in {"notes", "contacts"} and "-oob-" in r[1]]
    assert len(oob_targets) == 4
    assert all(flag for _, _, flag in oob_targets)
    wire_oob = [
        call for call in sent if any("-oob-" in str(v.get("S")) for v in call["Key"].values())
    ]
    assert wire_oob and all(call.get("ConsistentRead") is True for call in wire_oob)
