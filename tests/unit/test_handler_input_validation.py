# SPDX-License-Identifier: MIT
"""Tests for the toolspec-derived per-handler input validator.

* every schema violation leaves the boundary as `ValidationFailed` (HTTP 400, `validation_failed`);
* `additionalProperties: false` and the per-property keywords AgentCore's `SchemaDefinition` cannot
  advertise are enforced here;
* a caller-supplied identity channel key cannot reach a handler, on either channel;
* the user-safe `message`/`source` and the internal `detail` never echo a caller-supplied value;
* each message is corrective: built from the schema or allowlist alone, it names what to fix.

Runs against a `moto`-mocked DynamoDB client.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.domain.allowlists import validate_proposal_payload, validate_update_fields
from crm.envelope import AGENT_IDENTITY_KEY, IDENTITY_KEY, identity_event
from crm.errors import ValidationFailed
from crm.handlers import get_account, log_activity, submit_proposal, update_opportunity
from crm.handlers._common import build_handler, tool_for_action
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS, BY_NAME, allowlist_for, high_risk_fields_for
from crm.toolspec.validation import CORRECTION_HINT, validate_arguments

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OPPORTUNITY = "opp-1"

_CAPTURED_PROXY_EVENT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "events"
    / "apigw_v2_proxy_post_get_account.json"
)


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _identity() -> dict[str, Any]:
    return {
        "rep_id": _REP,
        "book": [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }


def _event(arguments: dict[str, Any]) -> dict[str, Any]:
    return identity_event(IDENTITY_KEY, _identity(), arguments)


def _seed_opportunity(store: Store) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": _ACCOUNT,
            "tenant": _TENANT,
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "owner_rep_id": _REP,
            "name": "Acme",
            "name_lower": "acme",
        },
    )
    store.put_item(
        "opportunities",
        {
            "opportunity_id": _OPPORTUNITY,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{_ACCOUNT}",
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "tenant_stage": f"{_TENANT}#Discovery",
            "account_id": _ACCOUNT,
            "owner_rep_id": _REP,
            "name": "Big Deal",
            "name_lower": "big deal",
            "stage": "Discovery",
            "value": 1000,
            "primary_contact_id": None,
            "last_touch": None,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


class _ExplodingStore:
    """Store double that fails the test if any attribute of it is touched at all."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"store.{name} was reached despite invalid arguments")


def _assert_validation_failed(result: dict[str, Any]) -> dict[str, Any]:
    assert result["statusCode"] == 400, result
    error: dict[str, Any] = result["error"]
    assert error["code"] == "validation_failed", error
    return error


# ---------------------------------------------------------------------------------------------
# The two handlers that subscript a required argument directly: not an unhandled exception.
# ---------------------------------------------------------------------------------------------


def test_submit_proposal_missing_reason_is_a_taxonomy_error_not_an_unhandled_keyerror() -> None:
    """Omitting the required `reason` argument is a 400, not an unhandled `KeyError`."""
    result = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "transaction_key": "txn-1",
            }
        ),
        store=_ExplodingStore(),
    )
    _assert_validation_failed(result)


def test_update_opportunity_missing_fields_is_a_taxonomy_error_not_an_unhandled_keyerror() -> None:
    """Omitting the required `fields` argument is a 400, not an unhandled `KeyError`."""
    result = update_opportunity.handler(
        _event({"opportunity_id": _OPPORTUNITY, "transaction_key": "txn-1"}),
        store=_ExplodingStore(),
    )
    _assert_validation_failed(result)


def test_validation_precedes_every_store_call() -> None:
    """No store attribute is reached when arguments fail the schema."""
    for handler, arguments in (
        (update_opportunity.handler, {"opportunity_id": 17}),
        (log_activity.handler, {"opportunity_id": _OPPORTUNITY, "kind": "call"}),
        (get_account.handler, {}),
    ):
        result = handler(_event(arguments), store=_ExplodingStore())
        _assert_validation_failed(result)


# ---------------------------------------------------------------------------------------------
# The keywords the Gateway's SchemaDefinition cannot advertise.
# ---------------------------------------------------------------------------------------------


def test_additional_properties_false_is_enforced() -> None:
    """An undeclared argument alongside an otherwise valid payload is refused, not ignored."""
    result = update_opportunity.handler(
        _event(
            {
                "opportunity_id": _OPPORTUNITY,
                "fields": {"name": "Renamed"},
                "transaction_key": "txn-1",
                "owner_rep_id": "rep-attacker",
            }
        ),
        store=_ExplodingStore(),
    )
    error = _assert_validation_failed(result)
    assert error["source"] == "arguments"


def test_enum_min_length_and_bounds_are_enforced() -> None:
    """One executed rejection per keyword class, on the tools that declare them."""
    cases: list[tuple[Any, dict[str, Any]]] = [
        # enum: `kind` is restricted to ACTIVITY_KINDS.
        (
            log_activity.handler,
            {
                "opportunity_id": _OPPORTUNITY,
                "kind": "not_a_kind",
                "subject": "s",
                "transaction_key": "txn-1",
            },
        ),
        # minLength: `account_id` is `{"type": "string", "minLength": 1}`.
        (get_account.handler, {"account_id": ""}),
    ]
    for handler, arguments in cases:
        _assert_validation_failed(handler(_event(arguments), store=_ExplodingStore()))

    # minimum / maximum: `search_opportunities.limit` is an integer in [1, 200].
    search_tool = BY_NAME["search_opportunities"]
    for out_of_bounds in (0, 201):
        with pytest.raises(ValidationFailed):
            validate_arguments(search_tool, {"limit": out_of_bounds})
    validate_arguments(search_tool, {"limit": 200})


# ---------------------------------------------------------------------------------------------
# Identity channels are unaffected by the validation step.
# ---------------------------------------------------------------------------------------------


def test_caller_supplied_identity_key_is_stripped_and_never_reaches_the_handler(
    store: Store,
) -> None:
    """A planted `_identity` inside a proxy body is stripped, and the request succeeds on the
    identity API Gateway supplied."""
    event: dict[str, Any] = copy.deepcopy(
        json.loads(_CAPTURED_PROXY_EVENT.read_text(encoding="utf-8"))
    )
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    in_book_account = claims["custom:book"].split(",")[0]
    store.put_item(
        "accounts",
        {
            "account_id": in_book_account,
            "tenant": claims["custom:tenant"],
            "tenant_owner_rep_id": f"{claims['custom:tenant']}#{claims['custom:rep_id']}",
            "owner_rep_id": claims["custom:rep_id"],
            "name": "Acme",
            "name_lower": "acme",
        },
    )
    event["body"] = json.dumps(
        {
            "account_id": in_book_account,
            IDENTITY_KEY: {
                "rep_id": "rep-attacker",
                "book": ["acct-not-mine"],
                "principal_type": "user",
                "source": "gateway",
            },
        }
    )
    event["isBase64Encoded"] = False

    result = get_account.handler(event, store=store)

    assert result["statusCode"] == 200, result
    body = json.loads(result["body"])
    assert body["account"]["account_id"] == in_book_account


def test_agent_identity_channel_still_carries_a_valid_call(store: Store) -> None:
    """A valid call on the direct-invoke agent channel still succeeds."""
    _seed_opportunity(store)
    event = identity_event(
        AGENT_IDENTITY_KEY,
        {
            "claims": {
                "custom:rep_id": _REP,
                "custom:book": _ACCOUNT,
                "custom:tenant": _TENANT,
            },
            "principal_type": "user",
            "session_id": "sess-1",
        },
        {
            "opportunity_id": _OPPORTUNITY,
            "fields": {"name": "Renamed"},
            "transaction_key": "txn-agent-1",
        },
    )
    result = update_opportunity.handler(event, store=store)
    assert result["statusCode"] == 200, result


# ---------------------------------------------------------------------------------------------
# A valid payload still succeeds unchanged.
# ---------------------------------------------------------------------------------------------


def test_valid_payload_still_commits_unchanged(store: Store) -> None:
    _seed_opportunity(store)
    result = update_opportunity.handler(
        _event(
            {
                "opportunity_id": _OPPORTUNITY,
                "fields": {"name": "Renamed"},
                "transaction_key": "txn-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200, result
    assert result["body"]["opportunity"]["name"] == "Renamed"
    persisted = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert persisted is not None
    assert persisted["name"] == "Renamed"


# ---------------------------------------------------------------------------------------------
# Nothing derived from the instance reaches the client or the logs.
# ---------------------------------------------------------------------------------------------


def test_no_caller_value_reaches_the_user_safe_response_or_the_detail() -> None:
    planted_value = "s3cr3t-token-value"
    planted_key = "s3cr3t-key-name"
    tool = BY_NAME["update_opportunity"]

    with pytest.raises(ValidationFailed) as raised:
        validate_arguments(
            tool,
            {
                "opportunity_id": planted_value,
                "fields": {},
                "transaction_key": "t",
                planted_key: planted_value,
            },
        )
    exc = raised.value
    assert exc.detail is not None
    rendered = json.dumps(exc.to_api_response()) + json.dumps(exc.to_mcp_error())
    for leaked in (planted_value, planted_key):
        assert leaked not in rendered
        assert leaked not in exc.detail
    assert "undeclared_property_count=1" in exc.detail


def test_reported_source_is_only_ever_a_schema_declared_property_name() -> None:
    tool = BY_NAME["log_activity"]
    declared = set(tool.input_schema["properties"])
    with pytest.raises(ValidationFailed) as raised:
        validate_arguments(
            tool,
            {
                "opportunity_id": "opp-1",
                "kind": "definitely-not-a-kind",
                "subject": "s",
                "transaction_key": "t",
            },
        )
    assert raised.value.source in declared | {"arguments"}
    assert raised.value.source == "kind"


# ---------------------------------------------------------------------------------------------
# Corrective messages, built from the schema or the allowlist only.
# ---------------------------------------------------------------------------------------------


def _rejection(tool_name: str, arguments: dict[str, Any]) -> ValidationFailed:
    with pytest.raises(ValidationFailed) as raised:
        validate_arguments(BY_NAME[tool_name], arguments)
    return raised.value


_LOG_ACTIVITY = {"opportunity_id": "o", "kind": "call", "subject": "s", "transaction_key": "t"}


@pytest.mark.parametrize(
    ("tool_name", "arguments", "message", "source"),
    [
        (
            "log_activity",
            {**_LOG_ACTIVITY, "kind": "PLANTED-kind"},
            "`kind` must be one of: call, email, meeting, note, stage_change, finding.",
            "kind",
        ),
        (
            "submit_proposal",
            {
                "target_table": "opportunities",
                "target_id": "o",
                "operation": "update",
                "transaction_key": "t",
            },
            "Missing required argument(s): reason.",
            "reason",
        ),
        (
            "log_activity",
            {"opportunity_id": "o", "subject": "s"},
            "Missing required argument(s): kind, transaction_key.",
            "kind",
        ),
        (
            "log_activity",
            {**_LOG_ACTIVITY, "PLANTED-key": 1},
            "Unknown argument name(s) supplied. Accepted: body, kind, occurred_at, opportunity_id, "
            "subject, transaction_key.",
            "arguments",
        ),
        ("get_account", {"account_id": ""}, "`account_id` violates minLength=1.", "account_id"),
        (
            "log_activity",
            {**_LOG_ACTIVITY, "transaction_key": "k" * 129},
            "`transaction_key` violates maxLength=128.",
            "transaction_key",
        ),
        ("search_opportunities", {"limit": 0}, "`limit` violates minimum=1.", "limit"),
        ("search_opportunities", {"limit": 201}, "`limit` violates maximum=200.", "limit"),
        (
            "search_opportunities",
            {"limit": "PLANTED-limit"},
            "`limit` must be of type integer.",
            "limit",
        ),
    ],
    ids=[
        "enum",
        "required-one",
        "required-several",
        "additionalProperties",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "type",
    ],
)
def test_each_keyword_has_a_corrective_schema_derived_message(
    tool_name: str, arguments: dict[str, Any], message: str, source: str
) -> None:
    error = _rejection(tool_name, arguments)
    assert error.message == f"{message} {CORRECTION_HINT}"
    assert error.source == source
    rendered = json.dumps(error.to_api_response()) + json.dumps(error.to_mcp_error())
    assert "PLANTED" not in rendered
    assert "k" * 129 not in rendered


def test_a_missing_required_property_names_itself_in_message_and_source() -> None:
    tool = BY_NAME["update_opportunity"]
    for name in tool.input_schema["required"]:
        arguments = {"opportunity_id": "o", "fields": {}, "transaction_key": "t"}
        arguments.pop(name)
        error = _rejection("update_opportunity", arguments)
        assert f"Missing required argument(s): {name}." in error.message
        assert error.source == name


def test_a_high_risk_update_field_is_named_and_routed_to_submit_proposal() -> None:
    risky = sorted(high_risk_fields_for("opportunities"))[0]
    with pytest.raises(ValidationFailed) as raised:
        validate_update_fields("opportunities", {risky: "x"})
    permitted = ", ".join(sorted(allowlist_for("opportunities")))
    assert raised.value.message == (
        f"Field {risky!r} is not permitted on opportunities updates. Permitted: {permitted}. "
        "Change it with submit_proposal."
    )
    assert raised.value.source == risky


def test_an_unknown_update_field_is_not_echoed_and_not_routed_to_a_proposal() -> None:
    planted = "PLANTED-field-name"
    with pytest.raises(ValidationFailed) as raised:
        validate_update_fields("accounts", {planted: "PLANTED-value"})
    permitted = ", ".join(sorted(allowlist_for("accounts")))
    assert raised.value.message == (
        f"A supplied field is not permitted on accounts updates. Permitted: {permitted}."
    )
    assert "PLANTED" not in raised.value.message
    assert "submit_proposal" not in raised.value.message


def test_a_proposal_payload_rejection_lists_the_permitted_fields_without_echo() -> None:
    planted = "PLANTED-field-name"
    with pytest.raises(ValidationFailed) as raised:
        validate_proposal_payload("opportunities", {planted: "PLANTED-value"})
    permitted = sorted(allowlist_for("opportunities") | high_risk_fields_for("opportunities"))
    assert raised.value.message == (
        "A supplied field is not permitted in a opportunities proposal's after_payload. "
        f"Permitted: {', '.join(permitted)}."
    )
    assert "PLANTED" not in raised.value.message


def test_the_high_risk_route_reaches_the_handler_response(store: Store) -> None:
    _seed_opportunity(store)
    result = update_opportunity.handler(
        _event(
            {
                "opportunity_id": _OPPORTUNITY,
                "fields": {"stage": "Closed Won"},
                "transaction_key": "txn-hr",
            }
        ),
        store=store,
    )
    error = _assert_validation_failed(result)
    assert error["source"] == "stage"
    assert "Change it with submit_proposal." in error["message"]
    assert "Permitted: " in error["message"]


# ---------------------------------------------------------------------------------------------
# The seam itself: no action Lambda can exist without a validator.
# ---------------------------------------------------------------------------------------------


def test_every_tool_resolves_to_its_handler_module() -> None:
    for tool in ALL_TOOLS:
        module_path, _, _ = tool.handler.rpartition(".")
        assert tool_for_action(type("A", (), {"__module__": module_path})).name == tool.name


def test_build_handler_refuses_an_action_with_no_toolspec_entry() -> None:
    """A handler module absent from `ALL_TOOLS` fails at import rather than skipping validation."""

    def _orphan(store: Any, identity: Any, arguments: Any) -> Any:  # pragma: no cover - never run
        raise AssertionError("unreachable")

    _orphan.__module__ = "crm.handlers.not_a_declared_tool"
    with pytest.raises(RuntimeError, match="not declared by any"):
        build_handler(_orphan)
