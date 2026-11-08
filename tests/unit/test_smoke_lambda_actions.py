# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.lambda_actions`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from io import BytesIO
from typing import Any, NoReturn

import boto3
import pytest
from botocore.stub import ANY, Stubber
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.lambda_actions import (
    _EMPTY_BOOK_DENIAL_TOOLS,
    _EXPLICIT_FILTER_TOOL,
    _LAYER5_CHAIN_TOOLS,
    _NO_OUT_OF_SCOPE_DENIAL_TOOLS,
    _NO_STATIC_SEED_TOOLS,
    _PRIMARY_CONTACT_CHECKS,
    _PRIMARY_CONTACT_TOOL,
    _T3_PENDING_TOOLS,
    CONTACT_DEPARTED_MISSING_SCOPE,
    CURSOR_ENV_CHECK,
    CURSOR_KEY_ARN_CHECK,
    CURSOR_KEY_RESOURCE,
    CURSOR_KEY_ROTATION_CHECK,
    CURSOR_KEY_SHAPE_CHECK,
    CURSOR_KEY_TAG_CHECK,
    CURSOR_OTHER_OPERATION_CHECK,
    CURSOR_OTHER_REP_CHECK,
    CURSOR_PAGE_TWO_CHECK,
    CURSOR_TAMPERED_CHECK,
    CURSOR_TOOLS,
    DEPARTED_CONTACT_CHECKS,
    DEPARTED_REACTIVATED_CHECK,
    DEPARTED_REFUSED_CHECK,
    DEPARTED_RESTORED_CHECK,
    EMPTY_BOOK_MISSING_SCOPE,
    EXPLICIT_FILTER_CHECK,
    FUNCTION_NAME_PREFIX,
    KMS_ADMIN_DENIED_CHECK,
    KMS_NO_CONTEXT_CHECK,
    KMS_OTHER_ACTIONS_CHECK,
    KMS_OTHER_PURPOSE_CHECK,
    KMS_OUTSIDE_IDENTITY_CHECK,
    KMS_OUTSIDE_KEY_POLICY_CHECK,
    KMS_USE_ALLOWED_CHECK,
    LONG_TEXT_CAP_CHECK,
    MALFORMED_PAGE_CONTEXT_CHECK,
    NO_SECOND_PAGE_DEFER_REASON,
    NON_CURSOR_PROBE_TOOL,
    SMOKE_PHONE,
    _check_explicit_filter_denied,
    _check_foreign_primary_contact_refused,
    _check_malformed_page_context_is_rejected,
    _check_out_of_scope,
    _check_primary_contact_accepted,
    _departed_contact_checks,
    checks,
    cursor_event,
    empty_book_listing_event,
    explicit_filter_event,
    function_name_for,
    guarded_extra_checks,
    in_scope_event,
    long_text_event,
    out_of_scope_event,
    page_context_tools,
    primary_contact_event,
    seeded_active_contact_id,
    seeded_departed_contact_id,
    seeded_in_account_contact_id,
    seeded_in_scope_account_id,
    seeded_other_account_contact_id,
    seeded_out_of_scope_account_id,
    tampered_cursor,
)
from scripts.smoke.tenant import SmokeTenant, TenantTeardownError

from crm.cursor import CURSOR_KMS_KEY_ENV, CURSOR_PURPOSE
from crm.toolspec.definitions import ALL_TOOLS, LONG_TEXT_MAX_LENGTH, ToolDefinition

_TEST_TENANT = "eval-smoke-testfixedrunid"
#: Placeholder credential for a client whose calls never leave the process; not a secret.
_STUB_CRED = "test"
_TEST_SMOKE_TENANT = SmokeTenant(run_id="smoke-testfixedrunid", tenant=_TEST_TENANT)
#: Minimal `dynamodb_table_names` output `checks()` needs to build a `Store` when `ctx.tenant`
#: is None.
_TEST_OUTPUTS = {"dynamodb_table_names": {"eval_runs": "crm-agent-eval-testeval_runs"}}


@pytest.fixture(autouse=True)
def _no_extra_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the per-tool orchestration tests to the per-tool rows.

    The cursor, cursor-key and long-text rows are tested through `guarded_extra_checks` directly,
    and `test_the_tool_checks_end_with_the_extra_rows` asserts they are wired in.
    """

    def none(ctx: Any, *, tenant: str) -> Any:
        return iter(())

    monkeypatch.setattr("scripts.smoke.lambda_actions.guarded_extra_checks", none)


def _fake_lifecycle(
    *, teardown_raises: Exception | None = None
) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    """Return `(allocate_calls, seed_calls, teardown_calls, kwargs)` for injecting into `checks()`.

    `kwargs` holds fake `tenant_allocator`/`seeder`/`teardown` callables that record their calls.
    """
    allocate_calls: list[str] = []
    seed_calls: list[str] = []
    teardown_calls: list[str] = []

    def fake_allocate(store: Any) -> SmokeTenant:
        allocate_calls.append("allocate")
        return _TEST_SMOKE_TENANT

    def fake_seed(store: Any, smoke_tenant: SmokeTenant) -> None:
        seed_calls.append(smoke_tenant.tenant)

    def fake_teardown(store: Any, smoke_tenant: SmokeTenant) -> dict[str, int]:
        teardown_calls.append(smoke_tenant.tenant)
        if teardown_raises is not None:
            raise teardown_raises
        return {"accounts": 20}

    kwargs = {
        "tenant_allocator": fake_allocate,
        "seeder": fake_seed,
        "teardown": fake_teardown,
    }
    return allocate_calls, seed_calls, teardown_calls, kwargs


def _lambda_client() -> Any:
    """A real boto3 `lambda` client, never used for network I/O: `Stubber` intercepts first."""
    return boto3.client(
        "lambda",
        region_name="us-east-1",
        aws_access_key_id=_STUB_CRED,
        aws_secret_access_key=_STUB_CRED,
    )


class _NoDynamoDbCallsExpected:
    """A DynamoDB client that raises `AssertionError` naming any method called on it."""

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(
            f"no DynamoDB call is expected on this path, but client.{name} was accessed. When the "
            "tenant lifecycle is faked, `checks()` only constructs a `Store` and never uses it; if "
            "that changed, inject a real-shaped fake (or moto) rather than widening this one."
        )


def _offline_clients(lambda_client: Any) -> dict[str, Any]:
    """Return the clients for the self-contained-tenant path: the stubbed Lambda client and a
    DynamoDB client that fails on any use."""
    return {"lambda": lambda_client, "dynamodb": _NoDynamoDbCallsExpected()}


def _payload_response(body: dict[str, Any], *, function_error: str | None = None) -> dict[str, Any]:
    """Build a `lambda:Invoke` response with a real streaming `Payload` body, as the API returns."""
    response: dict[str, Any] = {
        "StatusCode": 200,
        "Payload": BytesIO(json.dumps(body).encode("utf-8")),
    }
    if function_error is not None:
        response["FunctionError"] = function_error
    return response


def _success_body() -> dict[str, Any]:
    return {"statusCode": 200, "body": {"ok": True}}


def _scope_denied_body(missing_scope: str = "account:smoke-account-out-of-scope") -> dict[str, Any]:
    return {
        "statusCode": 403,
        "error": {
            "code": "scope_denied",
            "message": "You do not have access to that account.",
            "missing_scope": missing_scope,
        },
    }


def _explicit_filter_scope() -> str:
    return f"account:{seeded_out_of_scope_account_id(_TEST_TENANT)}"


_SEEDED_PHONE = "+1-555-321-4321"


def _contact_body(*, status: str, phone: str = _SEEDED_PHONE) -> dict[str, Any]:
    """A `get_contact`/`update_contact` success body for the departed contact."""
    return {
        "statusCode": 200,
        "body": {
            "contact": {
                "contact_id": seeded_departed_contact_id(_TEST_TENANT),
                "status": status,
                "phone": phone,
            }
        },
    }


def _contact_departed_body(missing_scope: str = CONTACT_DEPARTED_MISSING_SCOPE) -> dict[str, Any]:
    return {
        "statusCode": 409,
        "error": {
            "code": "contact_departed",
            "message": "That contact has left the company.",
            "next_step": "tell_user",
            "missing_scope": missing_scope,
        },
    }


def _departed_pass_bodies() -> list[dict[str, Any]]:
    """The five responses the departed-contact checks consume, in order, all passing:
    pre-read, refused update, re-read, reactivation, restore."""
    return [
        _contact_body(status="departed"),
        _contact_departed_body(),
        _contact_body(status="departed"),
        _contact_body(status="active"),
        _contact_body(status="departed"),
    ]


def _created_opportunity_body(contact_id: str) -> dict[str, Any]:
    """A `create_opportunity` success response carrying the `primary_contact_id` it was given.

    Uses the direct-invoke envelope of `crm.handlers._common.ok`, with the payload under ``body``.
    """
    return {
        "statusCode": 200,
        "body": {
            "opportunity": {
                "opportunity_id": "opp-smoke-0001",
                "primary_contact_id": contact_id,
            }
        },
    }


def _validation_failed_body() -> dict[str, Any]:
    return {
        "statusCode": 400,
        "error": {
            "code": "validation_failed",
            "message": "primary_contact_id must reference a contact under the same account.",
        },
    }


def _authentication_failed_body() -> dict[str, Any]:
    """The response a missing `dynamodb:GetItem` grant on `contacts` produces.

    `crm.errors._AWS_CODE_MAP` maps DynamoDB's `AccessDeniedException` onto `AuthenticationFailed`.
    """
    return {
        "statusCode": 401,
        "error": {"code": "authentication_failed", "message": "The request was not authorised."},
    }


def _internal_error_body() -> dict[str, Any]:
    return {"statusCode": 500, "error": {"code": "internal_error", "message": "x"}}


def _not_found_body() -> dict[str, Any]:
    return {"statusCode": 404, "error": {"code": "not_found", "message": "Not found."}}


def _leaky_scope_denied_body() -> dict[str, Any]:
    body = _scope_denied_body()
    body["error"] = {**body["error"], "detail": "internal trace leaked to caller"}
    return body


# ---------------------------------------------------------------------------
# function_name_for / event builders
# ---------------------------------------------------------------------------


def test_function_name_for_uses_pinned_prefix() -> None:
    tool = ALL_TOOLS[0]
    assert function_name_for(tool) == f"{FUNCTION_NAME_PREFIX}{tool.name}"


def test_in_scope_event_carries_full_visibility_identity() -> None:
    tool = ALL_TOOLS[0]
    event = in_scope_event(tool, tenant=_TEST_TENANT)
    assert event["_identity"]["full_visibility"] is True
    assert event["_identity"]["source"] == "gateway"
    assert event["_identity"]["tenant"] == _TEST_TENANT


def test_in_scope_event_never_hardcodes_the_demo_tenant() -> None:
    """The tenant field always reflects the caller-supplied tenant, never a hardcoded literal."""
    tool = ALL_TOOLS[0]
    other_tenant = "eval-smoke-someotherrunid"
    assert in_scope_event(tool, tenant=other_tenant)["_identity"]["tenant"] == other_tenant
    assert in_scope_event(tool, tenant=_TEST_TENANT)["_identity"]["tenant"] == _TEST_TENANT


def test_out_of_scope_event_book_excludes_target_account() -> None:
    tool = ALL_TOOLS[0]
    event = out_of_scope_event(tool, tenant=_TEST_TENANT)
    assert event["_identity"]["full_visibility"] is False
    assert "smoke-account-out-of-scope" not in event["_identity"]["book"]


def test_in_scope_event_targets_the_real_seeded_account() -> None:
    """The in-scope target is the deterministic seeded account id."""
    tool = ALL_TOOLS[0]
    expected_account_id = seeded_in_scope_account_id(_TEST_TENANT)
    event = in_scope_event(tool, tenant=_TEST_TENANT)
    assert expected_account_id in event["_identity"]["book"]


def test_event_builders_satisfy_required_schema_fields() -> None:
    """Every tool's required properties are present in both synthetic events (minus identity)."""
    for tool in ALL_TOOLS:
        required = tool.input_schema.get("required", [])
        in_scope = in_scope_event(tool, tenant=_TEST_TENANT)
        out_scope = out_of_scope_event(tool, tenant=_TEST_TENANT)
        for field_name in required:
            assert field_name in in_scope, f"{tool.name}: missing {field_name!r} in in-scope event"
            assert field_name in out_scope, (
                f"{tool.name}: missing {field_name!r} in out-of-scope event"
            )


# ---------------------------------------------------------------------------
# checks() — derivation from the toolspec
# ---------------------------------------------------------------------------


def _add_invoke(stubber: Stubber, body: dict[str, Any]) -> None:
    stubber.add_response(
        "invoke",
        _payload_response(body),
        {"FunctionName": ANY, "InvocationType": ANY, "Payload": ANY},
    )


def _stub_all_pass(
    stubber: Stubber,
    tools: Sequence[ToolDefinition],
    *,
    first_in_scope_body: dict[str, Any] | None = None,
) -> None:
    """Queue exactly the invocations `_run_all_tool_checks` makes, in that order, all passing.

    Tools in `_NO_STATIC_SEED_TOOLS` are not invoked, tools in `_NO_OUT_OF_SCOPE_DENIAL_TOOLS` are
    invoked once, `_PRIMARY_CONTACT_TOOL` is invoked twice more for its optional arguments, and
    `_EXPLICIT_FILTER_TOOL` once more for its explicit filter. The departed-contact sequence
    follows the last tool. `first_in_scope_body` replaces the first tool's in-scope response.
    """
    for index, tool in enumerate(tools):
        if tool.name in _NO_STATIC_SEED_TOOLS:
            continue
        in_scope_body = first_in_scope_body if index == 0 and first_in_scope_body else None
        _add_invoke(stubber, in_scope_body or _success_body())
        if tool.name in page_context_tools():
            # The malformed-page_context cell invokes once more, immediately after in_scope_invoke,
            # and expects `validation_failed`.
            _add_invoke(stubber, _validation_failed_body())
        if tool.name == _PRIMARY_CONTACT_TOOL:
            _add_invoke(
                stubber, _created_opportunity_body(seeded_in_account_contact_id(_TEST_TENANT))
            )
            _add_invoke(stubber, _validation_failed_body())
        if tool.name in _EMPTY_BOOK_DENIAL_TOOLS:
            _add_invoke(stubber, _scope_denied_body(EMPTY_BOOK_MISSING_SCOPE))
        elif tool.name not in _NO_OUT_OF_SCOPE_DENIAL_TOOLS:
            _add_invoke(stubber, _scope_denied_body())
        if tool.name == _EXPLICIT_FILTER_TOOL:
            _add_invoke(stubber, _scope_denied_body(_explicit_filter_scope()))
    for body in _departed_pass_bodies():
        _add_invoke(stubber, body)


def _expected_check_count(tools: Sequence[ToolDefinition]) -> int:
    """Two checks per tool, plus the per-tool extras and the departed-contact checks."""
    return (
        2 * len(tools)
        + len(_PRIMARY_CONTACT_CHECKS)
        + len(page_context_tools())
        + 1  # EXPLICIT_FILTER_CHECK
        + len(DEPARTED_CONTACT_CHECKS)
    )


def test_checks_derives_from_toolspec_not_a_hand_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injecting an extra synthetic ToolDefinition produces a corresponding pair of checks."""
    extra = replace(ALL_TOOLS[0], name="synthetic_extra_tool")
    extended_tools = (*ALL_TOOLS, extra)
    monkeypatch.setattr("scripts.smoke.lambda_actions.ALL_TOOLS", extended_tools)

    client = _lambda_client()
    stubber = Stubber(client)
    _stub_all_pass(stubber, extended_tools)
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    results = list(checks(ctx))
    stubber.deactivate()

    resources = {result.resource for result in results}
    assert "synthetic_extra_tool" in resources
    assert len(results) == _expected_check_count(extended_tools)


def test_checks_covers_every_declared_tool_exactly_twice() -> None:
    """Every tool gets the in/out-of-scope pair; `_PRIMARY_CONTACT_TOOL` gets two checks more."""
    client = _lambda_client()
    stubber = Stubber(client)
    _stub_all_pass(stubber, ALL_TOOLS)
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    results = list(checks(ctx))
    stubber.deactivate()

    by_resource: dict[str, list[str]] = {}
    for result in results:
        by_resource.setdefault(result.resource, []).append(result.check)
    assert set(by_resource) == {tool.name for tool in ALL_TOOLS}
    for name, tool_checks in by_resource.items():
        expected = ["in_scope_invoke", "out_of_scope_invoke"]
        if name in page_context_tools():
            expected = sorted([*expected, MALFORMED_PAGE_CONTEXT_CHECK])
        if name == _PRIMARY_CONTACT_TOOL:
            expected = sorted([*expected, *_PRIMARY_CONTACT_CHECKS])
        if name == _EXPLICIT_FILTER_TOOL:
            expected = sorted([*expected, EXPLICIT_FILTER_CHECK])
        if name == "update_contact":
            expected = sorted([*expected, *DEPARTED_CONTACT_CHECKS])
        assert sorted(tool_checks) == expected, name


def test_every_check_passes_when_every_invocation_returns_its_expected_shape() -> None:
    """With `_stub_all_pass` queued in the real invocation order, no check reports `fail`."""
    client = _lambda_client()
    stubber = Stubber(client)
    _stub_all_pass(stubber, ALL_TOOLS)
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    results = list(checks(ctx))
    stubber.deactivate()

    assert [r for r in results if r.status == "fail"] == []


# ---------------------------------------------------------------------------
# create_opportunity's optional primary_contact_id path
# ---------------------------------------------------------------------------


def _primary_contact_tool() -> ToolDefinition:
    return next(tool for tool in ALL_TOOLS if tool.name == _PRIMARY_CONTACT_TOOL)


def _primary_contact_results(bodies: list[dict[str, Any]]) -> list[Any]:
    """Run only the two primary-contact checks against `bodies`, in order."""
    tool = _primary_contact_tool()
    client = _lambda_client()
    stubber = Stubber(client)
    for body in bodies:
        _add_invoke(stubber, body)
    stubber.activate()
    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    results = [
        _check_primary_contact_accepted(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT),
        _check_foreign_primary_contact_refused(
            ctx, tool, function_name_for(tool), tenant=_TEST_TENANT
        ),
    ]
    stubber.deactivate()
    return results


def test_the_two_contact_ids_are_distinct_and_the_valid_one_is_the_first_accounts() -> None:
    """The invalid id must be a different, genuinely seeded contact -- not the valid one, not None."""
    valid = seeded_in_account_contact_id(_TEST_TENANT)
    foreign = seeded_other_account_contact_id(_TEST_TENANT)
    assert valid != foreign
    assert valid == f"cont-{_TEST_TENANT}-0000"
    assert foreign == f"cont-{_TEST_TENANT}-0004"


def test_primary_contact_event_carries_the_optional_argument_and_a_distinct_transaction_key() -> (
    None
):
    """The optional argument is sent under a `transaction_key` the base in-scope call never used."""
    tool = _primary_contact_tool()
    contact_id = seeded_in_account_contact_id(_TEST_TENANT)
    event = primary_contact_event(
        tool,
        tenant=_TEST_TENANT,
        contact_id=contact_id,
        transaction_key_scope=f"{_TEST_TENANT}-in-pc",
    )
    base = in_scope_event(tool, tenant=_TEST_TENANT)
    assert event["primary_contact_id"] == contact_id
    assert "primary_contact_id" not in base
    assert event["transaction_key"] != base["transaction_key"]
    assert event["account_id"] == seeded_in_scope_account_id(_TEST_TENANT)


def test_valid_primary_contact_passes_and_foreign_one_refused_with_validation_failed() -> None:
    accepted, refused = _primary_contact_results(
        [
            _created_opportunity_body(seeded_in_account_contact_id(_TEST_TENANT)),
            _validation_failed_body(),
        ]
    )
    assert accepted.status == "pass", accepted.detail
    assert refused.status == "pass", refused.detail


def test_a_401_authentication_failed_is_never_accepted_as_a_pass_by_either_check() -> None:
    """A missing-`GetItem`-grant response fails both checks and is named as a grant gap."""
    accepted, refused = _primary_contact_results(
        [_authentication_failed_body(), _authentication_failed_body()]
    )
    assert accepted.status == "fail"
    assert refused.status == "fail"
    for result in (accepted, refused):
        assert "authentication_failed" in result.detail
        assert "GetItem" in result.detail and "contacts" in result.detail


def test_a_response_with_no_body_envelope_fails_rather_than_being_read_at_the_wrong_level() -> None:
    """A success response without a `body` fails the accepted check."""
    accepted, _refused = _primary_contact_results([{"statusCode": 200}, _validation_failed_body()])
    assert accepted.status == "fail"
    assert "expected an 'opportunity' mapping" in accepted.detail


def test_a_dropped_primary_contact_id_fails_rather_than_passing_on_a_bare_200() -> None:
    """A success body that omits the contact id fails the accepted check."""
    accepted, _refused = _primary_contact_results(
        [_created_opportunity_body("cont-someone-else-9999"), _validation_failed_body()]
    )
    assert accepted.status == "fail"
    assert "dropped rather than honoured" in accepted.detail


def test_a_foreign_primary_contact_accepted_with_200_fails_the_refusal_check() -> None:
    """If the cross-account rule silently permitted it, the refusal check must fail, not pass."""
    _accepted, refused = _primary_contact_results(
        [
            _created_opportunity_body(seeded_in_account_contact_id(_TEST_TENANT)),
            _created_opportunity_body(seeded_other_account_contact_id(_TEST_TENANT)),
        ]
    )
    assert refused.status == "fail"
    assert "non-error response" in refused.detail


def test_a_scope_denied_does_not_satisfy_the_validation_failed_refusal_check() -> None:
    """Only `validation_failed` counts -- another typed refusal is a different defect, not a pass."""
    _accepted, refused = _primary_contact_results(
        [_created_opportunity_body(seeded_in_account_contact_id(_TEST_TENANT)), _not_found_body()]
    )
    assert refused.status == "fail"
    assert "not_found" in refused.detail


# ---------------------------------------------------------------------------
# Tenant lifecycle: self-contained allocate/seed/teardown when no --tenant is supplied
# ---------------------------------------------------------------------------


def test_checks_with_no_ctx_tenant_allocates_seeds_and_tears_down_its_own_smoke_tenant() -> None:
    """A standalone invocation (no `--tenant`) allocates, seeds, and tears down its own tenant."""
    client = _lambda_client()
    stubber = Stubber(client)
    _stub_all_pass(stubber, ALL_TOOLS)
    stubber.activate()

    allocate_calls, seed_calls, teardown_calls, lifecycle_kwargs = _fake_lifecycle()
    ctx = context_for_test(clients=_offline_clients(client), tenant=None, outputs=_TEST_OUTPUTS)
    results = list(checks(ctx, **lifecycle_kwargs))
    stubber.deactivate()

    assert allocate_calls == ["allocate"]
    assert seed_calls == [_TEST_TENANT]
    assert teardown_calls == [_TEST_TENANT]
    teardown_results = [r for r in results if r.check == "tenant_left_clean"]
    assert len(teardown_results) == 1
    assert teardown_results[0].status == "pass"


def test_the_injected_dynamodb_client_is_a_tripwire_not_a_permissive_fake() -> None:
    """`_NoDynamoDbCallsExpected` raises on any method call."""
    with pytest.raises(AssertionError, match="no DynamoDB call is expected"):
        _NoDynamoDbCallsExpected().put_item(TableName="x")


def test_the_offline_context_accepts_the_injected_dynamodb_client() -> None:
    """`ctx.client("dynamodb")` returns the injected fake rather than raising."""
    ctx = context_for_test(
        clients=_offline_clients(_lambda_client()), tenant=None, outputs=_TEST_OUTPUTS
    )
    assert isinstance(ctx.client("dynamodb"), _NoDynamoDbCallsExpected)


def test_checks_with_ctx_tenant_supplied_does_not_allocate_or_tear_down() -> None:
    """With `ctx.tenant` supplied, the module neither allocates a tenant nor tears one down."""
    client = _lambda_client()
    stubber = Stubber(client)
    _stub_all_pass(stubber, ALL_TOOLS)
    stubber.activate()

    allocate_calls, seed_calls, teardown_calls, lifecycle_kwargs = _fake_lifecycle()
    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    results = list(checks(ctx, **lifecycle_kwargs))
    stubber.deactivate()

    assert allocate_calls == []
    assert seed_calls == []
    assert teardown_calls == []
    assert all(r.check != "tenant_left_clean" for r in results)
    # Every invoked event still carries the orchestrator-supplied tenant.
    resources = {result.resource for result in results}
    assert resources == {tool.name for tool in ALL_TOOLS}


def test_checks_reports_a_teardown_failure_as_a_failing_check_never_swallowed() -> None:
    client = _lambda_client()
    stubber = Stubber(client)
    _stub_all_pass(stubber, ALL_TOOLS)
    stubber.activate()

    _allocate_calls, _seed_calls, teardown_calls, lifecycle_kwargs = _fake_lifecycle(
        teardown_raises=TenantTeardownError(_TEST_TENANT, deleted=None, cause=RuntimeError("boom"))
    )
    ctx = context_for_test(clients=_offline_clients(client), tenant=None, outputs=_TEST_OUTPUTS)
    results = list(checks(ctx, **lifecycle_kwargs))
    stubber.deactivate()

    assert teardown_calls == [_TEST_TENANT]
    teardown_results = [r for r in results if r.check == "tenant_left_clean"]
    assert len(teardown_results) == 1
    assert teardown_results[0].status == "fail"
    assert _TEST_TENANT in teardown_results[0].detail


def test_checks_tears_down_even_when_a_tool_check_fails() -> None:
    """A failing tool check must not skip teardown -- the `finally` in `checks()` must run."""
    client = _lambda_client()
    stubber = Stubber(client)
    _stub_all_pass(stubber, ALL_TOOLS, first_in_scope_body=_internal_error_body())
    stubber.activate()

    _allocate_calls, _seed_calls, teardown_calls, lifecycle_kwargs = _fake_lifecycle()
    ctx = context_for_test(clients=_offline_clients(client), tenant=None, outputs=_TEST_OUTPUTS)
    results = list(checks(ctx, **lifecycle_kwargs))
    stubber.deactivate()

    assert teardown_calls == [_TEST_TENANT]
    teardown_results = [r for r in results if r.check == "tenant_left_clean"]
    assert len(teardown_results) == 1 and teardown_results[0].status == "pass"
    assert any(r.status == "fail" for r in results if r.check != "tenant_left_clean")


# ---------------------------------------------------------------------------
# Seeded entity ids and skip-classified tools (proposal/audit-record chain, no-scope-check tools)
# ---------------------------------------------------------------------------


def test_seeded_entity_id_returns_deterministic_id_for_recognised_properties() -> None:
    from scripts.smoke.lambda_actions import seeded_entity_id

    assert seeded_entity_id(_TEST_TENANT, "opportunity_id") == f"opp-{_TEST_TENANT}-0000"
    assert seeded_entity_id(_TEST_TENANT, "contact_id") == f"cont-{_TEST_TENANT}-0000"
    assert seeded_entity_id(_TEST_TENANT, "activity_id") == f"act-{_TEST_TENANT}-0000"


def test_seeded_entity_id_returns_none_for_unseeded_properties() -> None:
    from scripts.smoke.lambda_actions import seeded_entity_id

    assert seeded_entity_id(_TEST_TENANT, "proposal_id") is None
    assert seeded_entity_id(_TEST_TENANT, "audit_id") is None


def test_in_scope_event_uses_real_seeded_id_for_get_opportunity() -> None:
    from crm.toolspec.definitions import BY_NAME

    tool = BY_NAME["get_opportunity"]
    event = in_scope_event(tool, tenant=_TEST_TENANT)
    assert event["opportunity_id"] == f"opp-{_TEST_TENANT}-0000"


def test_no_static_seed_tools_are_reported_skip_not_fail() -> None:
    """A proposal/audit-record tool never gets probed against an id that can never resolve."""
    from scripts.smoke.lambda_actions import checks as checks_fn

    # No stub responses are queued at all -- these tools must never reach `ctx.client("lambda")`.
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.activate()

    _allocate_calls, _seed_calls, teardown_calls, lifecycle_kwargs = _fake_lifecycle()
    ctx = context_for_test(clients=_offline_clients(client), tenant=None, outputs=_TEST_OUTPUTS)
    # checks() invokes every non-skipped tool too; stub those with the standard all-pass shape,
    # queued before the skip-classified tools are reached (skip-classified tools consume none).
    for _ in range(len(ALL_TOOLS)):
        stubber.add_response(
            "invoke",
            _payload_response(_success_body()),
            {"FunctionName": ANY, "InvocationType": ANY, "Payload": ANY},
        )
        stubber.add_response(
            "invoke",
            _payload_response(_scope_denied_body()),
            {"FunctionName": ANY, "InvocationType": ANY, "Payload": ANY},
        )
    results = list(checks_fn(ctx, **lifecycle_kwargs))
    stubber.deactivate()

    for tool_name in (
        "submit_proposal",
        "approve_proposal",
        "reject_proposal",
        "revert_audit_record",
    ):
        tool_results = [r for r in results if r.resource == tool_name]
        assert len(tool_results) == 2
        assert all(r.status == "skip" for r in tool_results), tool_results


def test_a_skip_names_only_the_coverage_that_exists() -> None:
    """Layer-5 chain tools cite the chain; the two tools no smoke module covers say T3 is pending."""
    from scripts.smoke.lambda_actions import checks as checks_fn

    client = _lambda_client()
    stubber = Stubber(client)
    stubber.activate()
    _allocate_calls, _seed_calls, _teardown_calls, lifecycle_kwargs = _fake_lifecycle()
    ctx = context_for_test(clients=_offline_clients(client), tenant=None, outputs=_TEST_OUTPUTS)
    for _ in range(len(ALL_TOOLS)):
        stubber.add_response(
            "invoke",
            _payload_response(_success_body()),
            {"FunctionName": ANY, "InvocationType": ANY, "Payload": ANY},
        )
        stubber.add_response(
            "invoke",
            _payload_response(_scope_denied_body()),
            {"FunctionName": ANY, "InvocationType": ANY, "Payload": ANY},
        )
    results = list(checks_fn(ctx, **lifecycle_kwargs))
    stubber.deactivate()

    assert {"get_audit_record", "rebase_proposal"} == _T3_PENDING_TOOLS
    assert _NO_STATIC_SEED_TOOLS == _LAYER5_CHAIN_TOOLS | _T3_PENDING_TOOLS
    for tool_name in sorted(_NO_STATIC_SEED_TOOLS):
        tool_results = [r for r in results if r.resource == tool_name]
        assert len(tool_results) == 2 and all(r.status == "skip" for r in tool_results)
        for result in tool_results:
            if tool_name in _T3_PENDING_TOOLS:
                assert "T3 pending" in result.detail, result
                assert "Layer 5" not in result.detail, result
            else:
                assert "Layer 5 (integration_proposal_chain.py)" in result.detail, result


def test_no_out_of_scope_denial_tools_skip_only_the_out_of_scope_check() -> None:
    """A create/list tool with no coherent scope-denial target still runs in-scope."""
    from crm.toolspec.definitions import BY_NAME

    tool_names = ("create_account", "list_proposals", "list_audit_records")
    client = _lambda_client()
    stubber = Stubber(client)
    for name in tool_names:
        stubber.add_response(
            "invoke",
            _payload_response(_success_body()),
            {
                "FunctionName": function_name_for(BY_NAME[name]),
                "InvocationType": ANY,
                "Payload": ANY,
            },
        )
    stubber.activate()

    from scripts.smoke.lambda_actions import _check_in_scope

    results: list[Any] = []
    for name in tool_names:
        tool = BY_NAME[name]
        results.append(
            _check_in_scope(
                context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT),
                tool,
                function_name_for(tool),
                tenant=_TEST_TENANT,
            )
        )
    stubber.deactivate()

    assert all(r.status == "pass" for r in results), results

    # The out-of-scope skip itself is asserted at the `checks()` orchestration level, not by
    # invoking the Lambda at all -- confirmed directly against the module's own skip set.
    from scripts.smoke.lambda_actions import _NO_OUT_OF_SCOPE_DENIAL_TOOLS

    assert set(tool_names) == _NO_OUT_OF_SCOPE_DENIAL_TOOLS


# ---------------------------------------------------------------------------
# In-scope invocation
# ---------------------------------------------------------------------------


def test_in_scope_success_passes() -> None:
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response("invoke", _payload_response(_success_body()))
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_in_scope, function_name_for

    result = _check_in_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "pass"


def test_in_scope_call_that_errors_fails() -> None:
    """An in-scope call must succeed; an error response of any code is a failure."""
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response("invoke", _payload_response(_not_found_body()))
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_in_scope, function_name_for

    result = _check_in_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "fail"
    assert "not_found" in result.detail


def test_in_scope_response_leaking_detail_fails() -> None:
    tool = ALL_TOOLS[0]
    body = _success_body()
    body["detail"] = "internal trace"
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response("invoke", _payload_response(body))
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_in_scope, function_name_for

    result = _check_in_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "fail"
    assert "detail" in result.detail


def test_in_scope_function_error_fails_never_silently_passes() -> None:
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response(
        "invoke", _payload_response({"errorMessage": "boom"}, function_error="Unhandled")
    )
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_in_scope, function_name_for

    result = _check_in_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "fail"
    assert "FunctionError" in result.detail


# ---------------------------------------------------------------------------
# Out-of-scope invocation — the core authorization-vs-leak distinction
# ---------------------------------------------------------------------------


def test_out_of_scope_scope_denied_passes() -> None:
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response("invoke", _payload_response(_scope_denied_body()))
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_out_of_scope, function_name_for

    result = _check_out_of_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "pass"


def test_out_of_scope_not_found_fails_distinctly_from_scope_denied() -> None:
    """A `not_found` response is a failure — it looks superficially "denied" but leaks a bug."""
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response("invoke", _payload_response(_not_found_body()))
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_out_of_scope, function_name_for

    result = _check_out_of_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "fail"
    assert "not_found" in result.detail
    assert "scope_denied" in result.detail


def test_out_of_scope_unhandled_error_fails() -> None:
    """A 500/unhandled error on the out-of-scope path is a failure, not a pass."""
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response(
        "invoke",
        _payload_response(
            {"statusCode": 500, "error": {"code": "internal_error", "message": "boom"}}
        ),
    )
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_out_of_scope, function_name_for

    result = _check_out_of_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "fail"


def test_out_of_scope_success_response_fails() -> None:
    """An out-of-scope call that actually succeeds (no denial at all) is a failure."""
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response("invoke", _payload_response(_success_body()))
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_out_of_scope, function_name_for

    result = _check_out_of_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "fail"


def test_out_of_scope_scope_denied_leaking_detail_fails() -> None:
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response("invoke", _payload_response(_leaky_scope_denied_body()))
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _check_out_of_scope, function_name_for

    result = _check_out_of_scope(ctx, tool, function_name_for(tool), tenant=_TEST_TENANT)
    stubber.deactivate()

    assert result.status == "fail"
    assert "detail" in result.detail


# ---------------------------------------------------------------------------
# Streaming Payload fidelity
# ---------------------------------------------------------------------------


def test_invoke_reads_payload_as_a_stream_not_a_plain_dict() -> None:
    """A `Payload` returned as a real `BytesIO` stream is read correctly."""
    tool = ALL_TOOLS[0]
    client = _lambda_client()
    stubber = Stubber(client)
    stubber.add_response("invoke", _payload_response(_success_body()))
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    from scripts.smoke.lambda_actions import _invoke, function_name_for

    body = _invoke(ctx, function_name_for(tool), in_scope_event(tool, tenant=_TEST_TENANT))
    stubber.deactivate()

    assert body == _success_body()


# ---------------------------------------------------------------------------
# Complete inventory even when an earlier resource fails
# ---------------------------------------------------------------------------


def test_one_failing_lambda_does_not_truncate_the_inventory() -> None:
    """With N tools and the first tool's in-scope call failing, all N tools still get checks."""
    client = _lambda_client()
    stubber = Stubber(client)
    # `ALL_TOOLS[0]`'s in-scope call fails (a 500); every other invocation passes.
    _stub_all_pass(stubber, ALL_TOOLS, first_in_scope_body=_internal_error_body())
    stubber.activate()

    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    results = list(checks(ctx))
    stubber.deactivate()

    resources = {result.resource for result in results}
    assert resources == {tool.name for tool in ALL_TOOLS}
    assert len(results) == _expected_check_count(ALL_TOOLS)
    first_tool_results = [r for r in results if r.resource == ALL_TOOLS[0].name]
    assert any(r.status == "fail" for r in first_tool_results)


def test_toolspec_input_schema_placeholder_covers_every_required_type() -> None:
    """Sanity check: every declared tool's required-argument placeholders are JSON-serializable."""
    for tool in ALL_TOOLS:
        json.dumps(in_scope_event(tool, tenant=_TEST_TENANT))
        json.dumps(out_of_scope_event(tool, tenant=_TEST_TENANT))


def test_all_tools_is_non_empty_and_matches_toolspec() -> None:
    assert len(ALL_TOOLS) > 0
    assert all(isinstance(tool, ToolDefinition) for tool in ALL_TOOLS)


# ---------------------------------------------------------------------------
# get_page_context's two cells: a well-formed payload exercises scope; a malformed one is refused.
# ---------------------------------------------------------------------------


def _page_context_tool() -> ToolDefinition:
    return next(tool for tool in ALL_TOOLS if tool.name == page_context_tools()[0])


def test_both_scope_arms_send_a_page_context_the_real_parser_accepts() -> None:
    """The `get_page_context` scope payloads parse with the real `parse_page_context`."""
    from crm.toolspec.page_context import parse_page_context

    tool = _page_context_tool()
    for build in (in_scope_event, out_of_scope_event):
        payload = build(tool, tenant=_TEST_TENANT)["page_context"]
        context = parse_page_context(payload)  # raises ValidationFailed if malformed
        assert context.kind == "entity", (
            "only the `entity` kind is scope-checked by get_page_context; `list`/`board` name no "
            "single record and `opaque` returns early, so another kind would make the out-of-scope "
            "arm return 200 and assert nothing"
        )


def test_the_two_scope_arms_target_different_real_seeded_accounts() -> None:
    """Both scope arms name distinct seeded accounts; only the in-scope one is in the book."""
    tool = _page_context_tool()
    in_event = in_scope_event(tool, tenant=_TEST_TENANT)
    out_event = out_of_scope_event(tool, tenant=_TEST_TENANT)
    in_id = in_event["page_context"]["entity_id"]
    out_id = out_event["page_context"]["entity_id"]

    assert in_id != out_id, "both arms name the same record, so they cannot differ in scope"
    assert in_id == seeded_in_scope_account_id(_TEST_TENANT)
    assert in_id in in_event["_identity"]["book"]
    assert out_id not in out_event["_identity"]["book"]
    assert out_event["_identity"]["book"] == [], (
        "the out-of-scope identity must hold an empty book, or the denial could come from "
        "full_visibility rather than from the book comparison"
    )
    # Both ids are the deterministic fixture form, so both records genuinely exist once seeded.
    for entity_id in (in_id, out_id):
        assert entity_id.startswith(f"acct-{_TEST_TENANT}-")


def test_a_malformed_page_context_is_reported_as_validation_failed() -> None:
    """A payload naming no `kind`, refused with `validation_failed`, passes the malformed cell."""
    tool = _page_context_tool()
    client = _lambda_client()
    stubber = Stubber(client)
    _add_invoke(stubber, _validation_failed_body())
    stubber.activate()
    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    result = _check_malformed_page_context_is_rejected(
        ctx, tool, function_name_for(tool), tenant=_TEST_TENANT
    )
    stubber.deactivate()

    assert result.check == MALFORMED_PAGE_CONTEXT_CHECK
    assert result.status == "pass"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"statusCode": 200, "result": {}}, "a success"),
        ({"statusCode": 403, "error": {"code": "scope_denied"}}, "scope_denied"),
        ({"statusCode": 404, "error": {"code": "not_found"}}, "not_found"),
    ],
)
def test_the_malformed_cell_fails_on_anything_other_than_validation_failed(
    body: dict[str, Any], why: str
) -> None:
    """Any response but `validation_failed`, `scope_denied` included, fails the malformed cell."""
    tool = _page_context_tool()
    client = _lambda_client()
    stubber = Stubber(client)
    _add_invoke(stubber, body)
    stubber.activate()
    ctx = context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)
    result = _check_malformed_page_context_is_rejected(
        ctx, tool, function_name_for(tool), tenant=_TEST_TENANT
    )
    stubber.deactivate()

    assert result.status == "fail", why


def test_the_malformed_cell_actually_sends_an_unparseable_payload() -> None:
    """The malformed cell's payload is rejected by the real `parse_page_context`."""
    from crm.errors import ValidationFailed
    from crm.toolspec.page_context import parse_page_context

    tool = _page_context_tool()
    sent = {**in_scope_event(tool, tenant=_TEST_TENANT), "page_context": {}}
    with pytest.raises(ValidationFailed) as caught:
        parse_page_context(sent["page_context"])
    assert caught.value.source == "page_context.kind"


# ---------------------------------------------------------------------------
# Empty-book listing denial and the explicit out-of-book filter
# ---------------------------------------------------------------------------


class _RecordingLambda:
    """A `lambda` client double that returns queued bodies and records each invoked event."""

    def __init__(self, bodies: Sequence[dict[str, Any]]) -> None:
        self._bodies = list(bodies)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        """Record `(FunctionName, decoded Payload)` and return the next queued body."""
        assert kwargs["InvocationType"] == "RequestResponse"
        function_name = str(kwargs["FunctionName"])
        self.calls.append((function_name, json.loads(kwargs["Payload"])))
        if not self._bodies:
            raise AssertionError(f"unexpected extra invoke of {function_name}")
        return _payload_response(self._bodies.pop(0))


def _tool(name: str) -> ToolDefinition:
    return next(tool for tool in ALL_TOOLS if tool.name == name)


def _listing_result(name: str, body: dict[str, Any]) -> tuple[Any, _RecordingLambda]:
    tool = _tool(name)
    fake = _RecordingLambda([body])
    ctx = context_for_test(clients={"lambda": fake}, tenant=_TEST_TENANT)
    result = _check_out_of_scope(
        ctx,
        tool,
        function_name_for(tool),
        tenant=_TEST_TENANT,
        event=empty_book_listing_event(tool, tenant=_TEST_TENANT),
        expected_missing_scope=EMPTY_BOOK_MISSING_SCOPE,
    )
    return result, fake


def test_the_listing_tools_no_longer_skip_their_out_of_scope_check() -> None:
    assert {"search_opportunities", "search_accounts", "pipeline_aggregate"} == (
        _EMPTY_BOOK_DENIAL_TOOLS
    )
    assert not _EMPTY_BOOK_DENIAL_TOOLS & _NO_OUT_OF_SCOPE_DENIAL_TOOLS


@pytest.mark.parametrize("name", sorted(_EMPTY_BOOK_DENIAL_TOOLS))
def test_empty_book_event_has_an_empty_book_and_no_explicit_filter(name: str) -> None:
    event = empty_book_listing_event(_tool(name), tenant=_TEST_TENANT)
    assert event["_identity"]["book"] == []
    assert event["_identity"]["full_visibility"] is False
    assert event["_identity"]["tenant"] == _TEST_TENANT
    assert "account_id" not in event


@pytest.mark.parametrize("name", sorted(_EMPTY_BOOK_DENIAL_TOOLS))
def test_empty_book_denial_naming_account_star_passes(name: str) -> None:
    result, fake = _listing_result(name, _scope_denied_body(EMPTY_BOOK_MISSING_SCOPE))
    assert result.status == "pass", result.detail
    assert result.check == "out_of_scope_invoke"
    assert fake.calls[0][0] == f"{FUNCTION_NAME_PREFIX}{name}"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (_success_body(), "non-error response"),
        (_scope_denied_body("account:acct-x"), "missing_scope='account:acct-x'"),
        (_not_found_body(), "not_found"),
        (
            {
                "statusCode": 403,
                "error": {"code": "scope_denied", "missing_scope": "account:*", "detail": "x"},
            },
            "detail",
        ),
    ],
)
def test_empty_book_check_fails_on_anything_but_a_clean_account_star_denial(
    body: dict[str, Any], fragment: str
) -> None:
    """A filtered success, a wrong scope, a miss or a leak each fail."""
    result, _fake = _listing_result("search_accounts", body)
    assert result.status == "fail"
    assert fragment in result.detail


def test_explicit_filter_event_names_an_account_outside_a_non_empty_book() -> None:
    event = explicit_filter_event(tenant=_TEST_TENANT)
    assert event["account_id"] == seeded_out_of_scope_account_id(_TEST_TENANT)
    assert event["_identity"]["book"] == [seeded_in_scope_account_id(_TEST_TENANT)]
    assert event["_identity"]["full_visibility"] is False
    assert event["account_id"] not in event["_identity"]["book"]


def _explicit_filter_result(body: dict[str, Any]) -> tuple[Any, _RecordingLambda]:
    fake = _RecordingLambda([body])
    ctx = context_for_test(clients={"lambda": fake}, tenant=_TEST_TENANT)
    return _check_explicit_filter_denied(
        ctx, _tool(_EXPLICIT_FILTER_TOOL), tenant=_TEST_TENANT
    ), fake


def test_explicit_filter_denial_naming_the_account_passes() -> None:
    result, fake = _explicit_filter_result(_scope_denied_body(_explicit_filter_scope()))
    assert result.status == "pass", result.detail
    assert result.check == EXPLICIT_FILTER_CHECK
    function_name, event = fake.calls[0]
    assert function_name == f"{FUNCTION_NAME_PREFIX}search_opportunities"
    assert event == explicit_filter_event(tenant=_TEST_TENANT)


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (_success_body(), "non-error response"),
        (_scope_denied_body(EMPTY_BOOK_MISSING_SCOPE), "missing_scope='account:*'"),
        (_not_found_body(), "not_found"),
        (_leaky_scope_denied_body(), "detail"),
    ],
)
def test_explicit_filter_check_fails_unless_the_named_account_is_denied(
    body: dict[str, Any], fragment: str
) -> None:
    result, _fake = _explicit_filter_result(body)
    assert result.status == "fail"
    assert fragment in result.detail


# ---------------------------------------------------------------------------
# Departed-contact refusal, reactivation and restore
# ---------------------------------------------------------------------------


def _departed_results(bodies: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], _RecordingLambda]:
    fake = _RecordingLambda(bodies)
    ctx = context_for_test(clients={"lambda": fake}, tenant=_TEST_TENANT)
    results = {r.check: r for r in _departed_contact_checks(ctx, tenant=_TEST_TENANT)}
    return results, fake


def test_update_contacts_generic_in_scope_call_targets_an_active_contact() -> None:
    """The departed contact would refuse the generic call's empty `fields`."""
    event = in_scope_event(_tool("update_contact"), tenant=_TEST_TENANT)
    assert event["contact_id"] == seeded_active_contact_id(_TEST_TENANT)
    assert seeded_active_contact_id(_TEST_TENANT) != seeded_departed_contact_id(_TEST_TENANT)
    assert in_scope_event(_tool("get_contact"), tenant=_TEST_TENANT)["contact_id"] == (
        seeded_departed_contact_id(_TEST_TENANT)
    )


def test_departed_checks_pass_and_send_the_expected_calls_in_order() -> None:
    results, fake = _departed_results(_departed_pass_bodies())
    assert list(results) == list(DEPARTED_CONTACT_CHECKS)
    assert all(r.status == "pass" for r in results.values()), results
    assert all(r.resource == "update_contact" for r in results.values())

    contact_id = seeded_departed_contact_id(_TEST_TENANT)
    names = [name.removeprefix(FUNCTION_NAME_PREFIX) for name, _ in fake.calls]
    assert names == [
        "get_contact",
        "update_contact",
        "get_contact",
        "update_contact",
        "update_contact",
    ]
    assert all(event["contact_id"] == contact_id for _, event in fake.calls)
    fields = [event.get("fields") for _, event in fake.calls]
    assert fields == [
        None,
        {"phone": SMOKE_PHONE},
        None,
        {"status": "active"},
        {"status": "departed"},
    ]
    keys = [event["transaction_key"] for name, event in fake.calls if "fields" in event]
    assert len(set(keys)) == 3
    for _, event in fake.calls:
        assert event["_identity"]["book"] == [seeded_in_scope_account_id(_TEST_TENANT)]
        assert event["_identity"]["full_visibility"] is False


@pytest.mark.parametrize(
    ("refusal", "fragment"),
    [
        (_contact_body(status="departed", phone=SMOKE_PHONE), "expected code='contact_departed'"),
        (_scope_denied_body(), "got code='scope_denied'"),
        (_contact_departed_body(missing_scope="unknown"), "missing_scope='unknown'"),
        (
            {"statusCode": 409, "error": {**_contact_departed_body()["error"], "detail": "x"}},
            "detail",
        ),
    ],
)
def test_the_refusal_check_fails_unless_the_update_is_a_clean_contact_departed(
    refusal: dict[str, Any], fragment: str
) -> None:
    bodies = _departed_pass_bodies()
    bodies[1] = refusal
    results, _fake = _departed_results(bodies)
    refused = results[DEPARTED_REFUSED_CHECK]
    assert refused.status == "fail"
    assert fragment in refused.detail


def test_the_refusal_check_fails_when_the_re_read_shows_the_phone_written() -> None:
    bodies = _departed_pass_bodies()
    bodies[2] = _contact_body(status="departed", phone=SMOKE_PHONE)
    results, _fake = _departed_results(bodies)
    refused = results[DEPARTED_REFUSED_CHECK]
    assert refused.status == "fail"
    assert "refused update still wrote" in refused.detail


def test_an_active_contact_fails_the_precondition_and_nothing_is_written() -> None:
    """With the contact not departed, no update is sent and no restore overwrites it."""
    results, fake = _departed_results([_contact_body(status="active")])
    assert results[DEPARTED_REFUSED_CHECK].status == "fail"
    assert "precondition" in results[DEPARTED_REFUSED_CHECK].detail
    assert results[DEPARTED_REACTIVATED_CHECK].status == "fail"
    assert results[DEPARTED_RESTORED_CHECK].status == "skip"
    assert [name for name, _ in fake.calls] == [f"{FUNCTION_NAME_PREFIX}get_contact"]


def test_a_failed_pre_read_fails_the_refusal_check() -> None:
    results, _fake = _departed_results([_not_found_body()])
    assert results[DEPARTED_REFUSED_CHECK].status == "fail"
    assert "pre-read" in results[DEPARTED_REFUSED_CHECK].detail


@pytest.mark.parametrize(
    "reactivation",
    [_contact_departed_body(), _contact_body(status="departed")],
)
def test_the_reactivation_check_fails_on_a_refusal_or_an_unchanged_status(
    reactivation: dict[str, Any],
) -> None:
    bodies = _departed_pass_bodies()
    bodies[3] = reactivation
    results, fake = _departed_results(bodies)
    assert results[DEPARTED_REACTIVATED_CHECK].status == "fail"
    assert results[DEPARTED_RESTORED_CHECK].status == "pass"
    assert len(fake.calls) == 5


def test_the_restore_check_fails_when_the_contact_is_not_set_back_to_departed() -> None:
    bodies = _departed_pass_bodies()
    bodies[4] = _contact_body(status="active")
    results, _fake = _departed_results(bodies)
    assert results[DEPARTED_RESTORED_CHECK].status == "fail"
    assert "expected contact status='departed'" in results[DEPARTED_RESTORED_CHECK].detail


def test_a_function_error_on_the_refused_update_fails_never_reads_as_a_pass() -> None:
    """An `Unhandled` FunctionError arrives with HTTP 200; the payload decides."""

    class _Unhandled(_RecordingLambda):
        def invoke(self, **kwargs: Any) -> dict[str, Any]:
            if len(self.calls) == 1:
                self.calls.append((str(kwargs["FunctionName"]), json.loads(kwargs["Payload"])))
                return _payload_response({"errorMessage": "boom"}, function_error="Unhandled")
            return super().invoke(**kwargs)

    bodies = _departed_pass_bodies()
    del bodies[1]
    fake = _Unhandled(bodies)
    ctx = context_for_test(clients={"lambda": fake}, tenant=_TEST_TENANT)
    results = {r.check: r for r in _departed_contact_checks(ctx, tenant=_TEST_TENANT)}
    assert results[DEPARTED_REFUSED_CHECK].status == "fail"
    assert "FunctionError" in results[DEPARTED_REFUSED_CHECK].detail


# ---------------------------------------------------------------------------
# Pagination cursors, the cursor key and the long-text cap
# ---------------------------------------------------------------------------

_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-5555aaaa5555"
_OTHER_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/99999999-2222-3333-4444-5555bbbb5555"
_UNRELATED_RESOURCE = "arn:aws:kms:us-east-1:123456789012:alias/unrelated"
#: A key policy as KMS stores it, every statement on `Resource "*"`, plus a non-key list element.
_KEY_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EnableIAMUserPermissions",
                "Effect": "Allow",
                "Action": "kms:*",
                "Resource": "*",
            },
            {
                "Sid": "Mixed",
                "Effect": "Deny",
                "Action": "kms:*",
                "Resource": ["*", _UNRELATED_RESOURCE],
            },
        ],
    }
)


def _role_arn(tool_name: str) -> str:
    return f"arn:aws:iam::123456789012:role/crm-agent-eval-{tool_name}-role"


def _issued_cursor(tool_name: str) -> str:
    return f"QUJDRA{tool_name.replace('_', '')}cursorQUJDRA"


def _tool_of(function_name: str) -> str:
    return function_name.removeprefix(FUNCTION_NAME_PREFIX)


def _cursor_refusal_body() -> dict[str, Any]:
    return {
        "statusCode": 400,
        "error": {
            "code": "validation_failed",
            "message": "The cursor is not valid.",
            "next_step": "fix_arguments",
            "source": "cursor",
        },
    }


class _CursorLambda:
    """A `lambda` double modelling the deployed cursor tools and every function's configuration.

    A cursor is accepted only by the tool that issued it, unaltered, for the issuing rep. Each
    `accept_*` set names tools that wrongly accept that kind of bad cursor.
    """

    def __init__(
        self,
        *,
        issuing: Sequence[str] = CURSOR_TOOLS,
        page_two_error: Sequence[str] = (),
        accept_tampered: Sequence[str] = (),
        accept_other_rep: Sequence[str] = (),
        accept_other_operation: Sequence[str] = (),
        refusal_body: dict[str, Any] | None = None,
        env_overrides: dict[str, str | None] | None = None,
        long_text_body: dict[str, Any] | None = None,
    ) -> None:
        self.issuing = set(issuing)
        self.page_two_error = set(page_two_error)
        self.accept_tampered = set(accept_tampered)
        self.accept_other_rep = set(accept_other_rep)
        self.accept_other_operation = set(accept_other_operation)
        self.refusal_body = refusal_body or _cursor_refusal_body()
        self.env_overrides = env_overrides or {}
        self.long_text_body = long_text_body or _validation_failed_body()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        tool = _tool_of(str(kwargs["FunctionName"]))
        event = json.loads(kwargs["Payload"])
        self.events.append((tool, event))
        return _payload_response(self._answer(tool, event))

    def _answer(self, tool: str, event: dict[str, Any]) -> dict[str, Any]:
        if tool == "create_note":
            return self.long_text_body
        cursor = event.get("cursor")
        if cursor is None:
            body: dict[str, Any] = {"items": [{"id": "x"}]}
            if tool in self.issuing:
                body["next_cursor"] = _issued_cursor(tool)
            return {"statusCode": 200, "body": body}
        owner = next((name for name in CURSOR_TOOLS if _issued_cursor(name) == cursor), None)
        rep = event["_identity"]["rep_id"]
        if owner is None:
            accepted = tool in self.accept_tampered
        elif owner != tool:
            accepted = tool in self.accept_other_operation
        elif rep != "rep-ashford":
            accepted = tool in self.accept_other_rep
        elif tool in self.page_two_error:
            return _internal_error_body()
        else:
            accepted = True
        return {"statusCode": 200, "body": {"items": []}} if accepted else self.refusal_body

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        tool = _tool_of(FunctionName)
        default = _KEY_ARN if tool in CURSOR_TOOLS else None
        value = self.env_overrides.get(tool, default)
        variables = {"LOG_LEVEL": "INFO"}
        if value is not None:
            variables[CURSOR_KMS_KEY_ENV] = value
        return {"Role": _role_arn(tool), "Environment": {"Variables": variables}}


class _Kms:
    def __init__(
        self,
        *,
        rotation: bool = True,
        metadata: dict[str, Any] | None = None,
        tags: list[dict[str, str]] | None = None,
        policy_error: bool = False,
    ) -> None:
        self.rotation = rotation
        self.metadata = metadata or {
            "KeySpec": "SYMMETRIC_DEFAULT",
            "KeyUsage": "ENCRYPT_DECRYPT",
            "MultiRegion": False,
        }
        self.tags = (
            tags
            if tags is not None
            else [{"TagKey": "data-classification", "TagValue": "internal"}]
        )
        self.policy_error = policy_error

    def get_key_rotation_status(self, *, KeyId: str) -> dict[str, Any]:  # noqa: N803
        assert KeyId == _KEY_ARN
        return {"KeyRotationEnabled": self.rotation}

    def describe_key(self, *, KeyId: str) -> dict[str, Any]:  # noqa: N803
        return {"KeyMetadata": self.metadata}

    def list_resource_tags(self, *, KeyId: str) -> dict[str, Any]:  # noqa: N803
        return {"Tags": self.tags}

    def get_key_policy(self, *, KeyId: str, PolicyName: str) -> dict[str, Any]:  # noqa: N803
        if self.policy_error:
            raise RuntimeError("AccessDeniedException")
        assert PolicyName == "default"
        return {"Policy": _KEY_POLICY}


class _Iam:
    """Simulates the intended policy: the five roles may Encrypt/Decrypt under the cursor purpose,
    the key policy explicitly denies administration and every outside principal.

    `overrides[(role tool, action, purpose-or-None, with_policy)]` replaces one decision;
    `missing_context` names roles whose allowed results report missing context values.
    """

    def __init__(
        self,
        *,
        overrides: dict[tuple[str, str, str | None, bool], str] | None = None,
        missing_context: Sequence[str] = (),
    ) -> None:
        self.overrides = overrides or {}
        self.missing_context = set(missing_context)
        self.requests: list[dict[str, Any]] = []

    def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        role_tool = (
            str(request["PolicySourceArn"]).split("crm-agent-eval-", 1)[1].removesuffix("-role")
        )
        purpose = next(
            (
                entry["ContextKeyValues"][0]
                for entry in request["ContextEntries"]
                if entry["ContextKeyName"] == "kms:EncryptionContext:purpose"
            ),
            None,
        )
        with_policy = "ResourcePolicy" in request
        results = []
        for action in request["ActionNames"]:
            decision = self._decide(role_tool, action, purpose, with_policy)
            decision = self.overrides.get((role_tool, action, purpose, with_policy), decision)
            result: dict[str, Any] = {"EvalActionName": action, "EvalDecision": decision}
            if decision == "allowed" and role_tool in self.missing_context:
                result["MissingContextValues"] = ["kms:EncryptionContext:tenant"]
            results.append(result)
        return {"EvaluationResults": results, "IsTruncated": False}

    @staticmethod
    def _decide(role_tool: str, action: str, purpose: str | None, with_policy: bool) -> str:
        if with_policy and (
            action in {"kms:PutKeyPolicy", "kms:ScheduleKeyDeletion", "kms:DisableKeyRotation"}
            or role_tool not in CURSOR_TOOLS
        ):
            return "explicitDeny"
        if (
            role_tool in CURSOR_TOOLS
            and action in {"kms:Encrypt", "kms:Decrypt"}
            and purpose == CURSOR_PURPOSE
        ):
            return "allowed"
        return "implicitDeny"


def _extra_ctx(
    lam: _CursorLambda | None = None,
    kms: _Kms | None = None,
    iam: _Iam | None = None,
    outputs: dict[str, Any] | None = None,
) -> Any:
    return context_for_test(
        clients={"lambda": lam or _CursorLambda(), "kms": kms or _Kms(), "iam": iam or _Iam()},
        outputs=outputs,
        tenant=_TEST_TENANT,
    )


def _rows(ctx: Any) -> list[Any]:
    return list(guarded_extra_checks(ctx, tenant=_TEST_TENANT))


def _failing(rows: Sequence[Any]) -> set[tuple[str, str]]:
    return {(r.resource, r.check) for r in rows if r.status == "fail"}


def test_every_extra_row_passes_against_the_intended_deployment() -> None:
    rows = _rows(_extra_ctx())
    assert _failing(rows) == set()
    assert {r.status for r in rows} == {"pass"}
    checks_by_resource: dict[str, set[str]] = {}
    for row in rows:
        checks_by_resource.setdefault(row.resource, set()).add(row.check)
    for name in CURSOR_TOOLS:
        assert {
            CURSOR_PAGE_TWO_CHECK,
            CURSOR_TAMPERED_CHECK,
            CURSOR_OTHER_REP_CHECK,
            CURSOR_OTHER_OPERATION_CHECK,
            CURSOR_ENV_CHECK,
            KMS_USE_ALLOWED_CHECK,
            KMS_OTHER_PURPOSE_CHECK,
            KMS_NO_CONTEXT_CHECK,
            KMS_OTHER_ACTIONS_CHECK,
            KMS_ADMIN_DENIED_CHECK,
        } <= checks_by_resource[name]
    assert {
        CURSOR_KEY_ARN_CHECK,
        CURSOR_KEY_ROTATION_CHECK,
        CURSOR_KEY_SHAPE_CHECK,
        CURSOR_KEY_TAG_CHECK,
    } == checks_by_resource[CURSOR_KEY_RESOURCE]
    assert {
        CURSOR_ENV_CHECK,
        KMS_OUTSIDE_IDENTITY_CHECK,
        KMS_OUTSIDE_KEY_POLICY_CHECK,
    } == checks_by_resource[NON_CURSOR_PROBE_TOOL]
    assert LONG_TEXT_CAP_CHECK in checks_by_resource["create_note"]
    every_tool = {tool.name for tool in ALL_TOOLS}
    assert {r.resource for r in rows if r.check == CURSOR_ENV_CHECK} == every_tool, (
        "the env row must cover every action Lambda"
    )


@pytest.mark.parametrize(
    ("lam", "expected"),
    [
        (
            _CursorLambda(page_two_error=["search_accounts"]),
            {("search_accounts", "cursor_page_two")},
        ),
        (
            _CursorLambda(accept_tampered=["list_activities"]),
            {("list_activities", "cursor_tampered_refused")},
        ),
        (
            _CursorLambda(accept_other_rep=["search_opportunities"]),
            {("search_opportunities", "cursor_other_rep_refused")},
        ),
        (
            _CursorLambda(accept_other_operation=["list_proposals"]),
            {("list_proposals", "cursor_other_operation_refused")},
        ),
        (
            _CursorLambda(env_overrides={"list_audit_records": None}),
            {("list_audit_records", "cursor_key_env")},
        ),
        (
            _CursorLambda(env_overrides={"get_account": _KEY_ARN}),
            {("get_account", "cursor_key_env")},
        ),
        (
            _CursorLambda(long_text_body=_success_body()),
            {("create_note", "long_text_cap_enforced")},
        ),
    ],
    ids=[
        "page-two-error",
        "tampered-accepted",
        "other-rep-accepted",
        "other-operation-accepted",
        "env-missing-on-cursor-tool",
        "env-set-on-other-tool",
        "over-cap-accepted",
    ],
)
def test_a_broken_function_property_fails_exactly_its_row(
    lam: _CursorLambda, expected: set[tuple[str, str]]
) -> None:
    assert _failing(_rows(_extra_ctx(lam=lam))) == expected


def test_a_5xx_refusal_is_not_a_clean_cursor_refusal() -> None:
    lam = _CursorLambda(refusal_body=_internal_error_body())
    failing = _failing(_rows(_extra_ctx(lam=lam)))
    refusal_checks = {
        CURSOR_TAMPERED_CHECK,
        CURSOR_OTHER_REP_CHECK,
        CURSOR_OTHER_OPERATION_CHECK,
    }
    assert failing == {(name, check) for name in CURSOR_TOOLS for check in refusal_checks}


def test_a_refusal_without_source_cursor_fails() -> None:
    body = _cursor_refusal_body()
    del body["error"]["source"]
    failing = _failing(_rows(_extra_ctx(lam=_CursorLambda(refusal_body=body))))
    assert ("search_opportunities", CURSOR_TAMPERED_CHECK) in failing


@pytest.mark.parametrize(
    ("kms", "expected"),
    [
        (_Kms(rotation=False), {(CURSOR_KEY_RESOURCE, "cursor_key_rotation_enabled")}),
        (
            _Kms(
                metadata={
                    "KeySpec": "SYMMETRIC_DEFAULT",
                    "KeyUsage": "ENCRYPT_DECRYPT",
                    "MultiRegion": True,
                }
            ),
            {(CURSOR_KEY_RESOURCE, "cursor_key_shape")},
        ),
        (_Kms(tags=[]), {(CURSOR_KEY_RESOURCE, "cursor_key_classified")}),
        (
            _Kms(policy_error=True),
            {(name, KMS_ADMIN_DENIED_CHECK) for name in CURSOR_TOOLS}
            | {(NON_CURSOR_PROBE_TOOL, KMS_OUTSIDE_KEY_POLICY_CHECK)},
        ),
    ],
    ids=["rotation-off", "multi-region", "untagged", "key-policy-unreadable"],
)
def test_a_broken_key_property_fails_exactly_its_row(
    kms: _Kms, expected: set[tuple[str, str]]
) -> None:
    assert _failing(_rows(_extra_ctx(kms=kms))) == expected


@pytest.mark.parametrize(
    ("iam", "expected"),
    [
        (
            _Iam(
                overrides={
                    ("search_accounts", "kms:Decrypt", CURSOR_PURPOSE, False): "implicitDeny"
                }
            ),
            {("search_accounts", KMS_USE_ALLOWED_CHECK)},
        ),
        (_Iam(missing_context=["list_activities"]), {("list_activities", KMS_USE_ALLOWED_CHECK)}),
        (
            _Iam(overrides={("list_proposals", "kms:Encrypt", "other", False): "allowed"}),
            {("list_proposals", KMS_OTHER_PURPOSE_CHECK)},
        ),
        (
            _Iam(overrides={("list_audit_records", "kms:Encrypt", None, False): "allowed"}),
            {("list_audit_records", KMS_NO_CONTEXT_CHECK)},
        ),
        (
            _Iam(
                overrides={
                    (
                        "search_opportunities",
                        "kms:GenerateDataKey",
                        CURSOR_PURPOSE,
                        False,
                    ): "allowed"
                }
            ),
            {("search_opportunities", KMS_OTHER_ACTIONS_CHECK)},
        ),
        (
            _Iam(
                overrides={("search_accounts", "kms:PutKeyPolicy", CURSOR_PURPOSE, True): "allowed"}
            ),
            {("search_accounts", KMS_ADMIN_DENIED_CHECK)},
        ),
        (
            _Iam(overrides={("get_account", "kms:Encrypt", CURSOR_PURPOSE, False): "allowed"}),
            {("get_account", KMS_OUTSIDE_IDENTITY_CHECK)},
        ),
        (
            _Iam(overrides={("get_account", "kms:Decrypt", CURSOR_PURPOSE, True): "implicitDeny"}),
            {("get_account", KMS_OUTSIDE_KEY_POLICY_CHECK)},
        ),
    ],
    ids=[
        "use-not-allowed",
        "missing-context",
        "other-purpose-allowed",
        "no-context-allowed",
        "generate-data-key-allowed",
        "admin-not-explicitly-denied",
        "outside-role-allowed",
        "outside-role-only-implicitly-denied",
    ],
)
def test_a_broken_simulation_fails_exactly_its_row(
    iam: _Iam, expected: set[tuple[str, str]]
) -> None:
    assert _failing(_rows(_extra_ctx(iam=iam))) == expected


def test_the_simulations_use_the_deployed_roles_and_the_live_key_policy() -> None:
    iam = _Iam()
    _rows(_extra_ctx(iam=iam))
    sources = {request["PolicySourceArn"] for request in iam.requests}
    assert sources == {_role_arn(name) for name in (*CURSOR_TOOLS, NON_CURSOR_PROBE_TOOL)}
    assert all(request["ResourceArns"] == [_KEY_ARN] for request in iam.requests)
    with_policy = [request for request in iam.requests if "ResourcePolicy" in request]
    assert with_policy
    for request in with_policy:
        sent = [s["Resource"] for s in json.loads(request["ResourcePolicy"])["Statement"]]
        assert sent == [_KEY_ARN, [_KEY_ARN, _UNRELATED_RESOURCE]], sent
    for request in with_policy:
        principal = next(
            e["ContextKeyValues"]
            for e in request["ContextEntries"]
            if e["ContextKeyName"] == "aws:PrincipalArn"
        )
        assert principal == [request["PolicySourceArn"]]


def test_two_cursor_keys_fail_only_the_consistency_row_and_skip_the_dependents() -> None:
    lam = _CursorLambda(env_overrides={"search_accounts": _OTHER_KEY_ARN})
    rows = _rows(_extra_ctx(lam=lam))
    assert _failing(rows) == {(CURSOR_KEY_RESOURCE, CURSOR_KEY_ARN_CHECK)}
    skipped = {(r.resource, r.check) for r in rows if r.status == "skip"}
    assert (CURSOR_KEY_RESOURCE, CURSOR_KEY_ROTATION_CHECK) in skipped
    assert (NON_CURSOR_PROBE_TOOL, KMS_OUTSIDE_KEY_POLICY_CHECK) in skipped


def test_a_tool_with_no_second_page_defers_its_own_rows_and_still_checks_foreign_cursors() -> None:
    lam = _CursorLambda(issuing=["search_opportunities", "search_accounts", "list_activities"])
    rows = _rows(_extra_ctx(lam=lam))
    assert _failing(rows) == set()
    deferred = {(r.resource, r.check) for r in rows if r.status == "defer"}
    assert deferred == {
        (name, check)
        for name in ("list_audit_records", "list_proposals")
        for check in (CURSOR_PAGE_TWO_CHECK, CURSOR_TAMPERED_CHECK, CURSOR_OTHER_REP_CHECK)
    }
    assert all(r.defer_reason == NO_SECOND_PAGE_DEFER_REASON for r in rows if r.status == "defer")
    other_operation = {
        r.resource: r.status for r in rows if r.check == CURSOR_OTHER_OPERATION_CHECK
    }
    assert other_operation == dict.fromkeys(CURSOR_TOOLS, "pass")


def test_no_cursor_issued_anywhere_fails_every_other_operation_row() -> None:
    rows = _rows(_extra_ctx(lam=_CursorLambda(issuing=[])))
    assert _failing(rows) == {(name, CURSOR_OTHER_OPERATION_CHECK) for name in CURSOR_TOOLS}


def test_the_replays_send_the_real_cursor_altered_only_as_each_row_says() -> None:
    lam = _CursorLambda()
    _rows(_extra_ctx(lam=lam))
    resumed = [(tool, event) for tool, event in lam.events if "cursor" in event]
    own = _issued_cursor("search_opportunities")
    sent = [event for tool, event in resumed if tool == "search_opportunities"]
    cursors = [event["cursor"] for event in sent]
    reps = [event["_identity"]["rep_id"] for event in sent]
    assert cursors[0] == own and reps[0] == "rep-ashford"
    assert cursors[1] == tampered_cursor(own) and cursors[1] != own and len(cursors[1]) == len(own)
    assert cursors[2] == own and reps[2] != "rep-ashford"
    assert cursors[3] != own and cursors[3] in {_issued_cursor(n) for n in CURSOR_TOOLS}
    page_one = [
        event for tool, event in lam.events if "cursor" not in event and tool != "create_note"
    ]
    assert all(event["limit"] == 1 for event in page_one if "limit" in event)


def test_the_page_one_calls_force_a_second_page_where_the_tool_takes_a_limit() -> None:
    for name in CURSOR_TOOLS:
        event = cursor_event(name, tenant=_TEST_TENANT)
        tool = next(t for t in ALL_TOOLS if t.name == name)
        assert ("limit" in event) == ("limit" in tool.input_schema["properties"])
        assert "cursor" not in event
        assert event["_identity"]["tenant"] == _TEST_TENANT
    assert cursor_event("list_activities", tenant=_TEST_TENANT)["opportunity_id"]


def test_tampering_changes_one_character_within_the_url_safe_alphabet() -> None:
    for cursor in ("AAAA", "ABCD", "x-_y"):
        tampered = tampered_cursor(cursor)
        assert len(tampered) == len(cursor)
        assert sum(a != b for a, b in zip(cursor, tampered, strict=True)) == 1
    with pytest.raises(ValueError):
        tampered_cursor("")


def test_the_long_text_body_is_exactly_one_over_the_cap() -> None:
    event = long_text_event(tenant=_TEST_TENANT)
    assert len(event["body"]) == LONG_TEXT_MAX_LENGTH + 1
    assert event["_identity"]["tenant"] == _TEST_TENANT


def test_the_long_text_cap_row_fails_on_a_5xx() -> None:
    lam = _CursorLambda(long_text_body=_internal_error_body())
    assert _failing(_rows(_extra_ctx(lam=lam))) == {("create_note", LONG_TEXT_CAP_CHECK)}


def test_the_tool_checks_end_with_the_extra_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run_all_tool_checks` yields `guarded_extra_checks` last, under the same tenant."""
    seen: list[str] = []

    def recorder(ctx: Any, *, tenant: str) -> Any:
        seen.append(tenant)
        yield CheckResult(resource="sentinel", check="extra", status="pass")

    monkeypatch.setattr("scripts.smoke.lambda_actions.guarded_extra_checks", recorder)
    client = _lambda_client()
    stubber = Stubber(client)
    _stub_all_pass(stubber, ALL_TOOLS)
    stubber.activate()
    results = list(checks(context_for_test(clients={"lambda": client}, tenant=_TEST_TENANT)))
    stubber.deactivate()
    assert seen == [_TEST_TENANT]
    assert (results[-1].resource, results[-1].check) == ("sentinel", "extra")
