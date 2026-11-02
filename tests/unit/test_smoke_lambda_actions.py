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
from scripts.smoke.harness import context_for_test
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
    DEPARTED_CONTACT_CHECKS,
    DEPARTED_REACTIVATED_CHECK,
    DEPARTED_REFUSED_CHECK,
    DEPARTED_RESTORED_CHECK,
    EMPTY_BOOK_MISSING_SCOPE,
    EXPLICIT_FILTER_CHECK,
    FUNCTION_NAME_PREFIX,
    MALFORMED_PAGE_CONTEXT_CHECK,
    SMOKE_PHONE,
    _check_explicit_filter_denied,
    _check_foreign_primary_contact_refused,
    _check_malformed_page_context_is_rejected,
    _check_out_of_scope,
    _check_primary_contact_accepted,
    _departed_contact_checks,
    checks,
    empty_book_listing_event,
    explicit_filter_event,
    function_name_for,
    in_scope_event,
    out_of_scope_event,
    page_context_tools,
    primary_contact_event,
    seeded_active_contact_id,
    seeded_departed_contact_id,
    seeded_in_account_contact_id,
    seeded_in_scope_account_id,
    seeded_other_account_contact_id,
    seeded_out_of_scope_account_id,
)
from scripts.smoke.tenant import SmokeTenant, TenantTeardownError

from crm.toolspec.definitions import ALL_TOOLS, ToolDefinition

_TEST_TENANT = "eval-smoke-testfixedrunid"
#: Placeholder credential for a client whose calls never leave the process; not a secret.
_STUB_CRED = "test"
_TEST_SMOKE_TENANT = SmokeTenant(run_id="smoke-testfixedrunid", tenant=_TEST_TENANT)
#: Minimal `dynamodb_table_names` output `checks()` needs to build a `Store` when `ctx.tenant`
#: is None.
_TEST_OUTPUTS = {"dynamodb_table_names": {"eval_runs": "crm-agent-eval-testeval_runs"}}


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
