# SPDX-License-Identifier: MIT
"""Free-text length caps: declared in every tool schema and enforced on open field objects.

Long text (``description``, ``notes``, ``body``, ``reason``) is capped at 4000 characters, cursors
at 4000, the revert reason at the limit revert enforces, every other free-text string at 256. Over-cap values are refused with ``validation_failed``
naming the field as ``source``; at-cap values are accepted.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm import audit
from crm.domain.allowlists import validate_field_values
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import ValidationFailed
from crm.handlers import (
    create_account,
    create_note,
    create_opportunity,
    log_activity,
    submit_proposal,
    update_account,
    update_opportunity,
)
from crm.store.keys import derive
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import (
    ALL_TOOLS,
    BY_NAME,
    CURSOR_MAX_LENGTH,
    LONG_TEXT_FIELDS,
    LONG_TEXT_MAX_LENGTH,
    REVERT_REASON_MAX_LENGTH,
    TEXT_MAX_LENGTH,
    max_length_for,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OPPORTUNITY = "opp-1"
_LONG = LONG_TEXT_MAX_LENGTH
_SHORT = TEXT_MAX_LENGTH


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        engine = Store(client=client)
        engine.put_item(
            "accounts",
            derive(
                "accounts",
                {"account_id": _ACCOUNT, "tenant": _TENANT, "owner_rep_id": _REP, "name": "Acme"},
            ),
        )
        engine.put_item(
            "opportunities",
            derive(
                "opportunities",
                {
                    "opportunity_id": _OPPORTUNITY,
                    "tenant": _TENANT,
                    "account_id": _ACCOUNT,
                    "owner_rep_id": _REP,
                    "name": "Acme Renewal",
                    "stage": "Discovery",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
            ),
        )
        yield engine


def _call(module: Any, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    event = identity_event(
        IDENTITY_KEY,
        {
            "rep_id": _REP,
            "book": [_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-1",
            "source": "gateway",
            "tenant": _TENANT,
        },
        {**args, "transaction_key": uuid.uuid4().hex},
    )
    result: dict[str, Any] = module.handler(event, store=store)
    return result


def _assert_refused(result: dict[str, Any], source: str) -> None:
    assert result["statusCode"] == 400, result
    assert result["error"]["code"] == "validation_failed", result
    assert result["error"]["source"] == source, result


def _assert_accepted(result: dict[str, Any]) -> None:
    assert result["statusCode"] == 200, result


# --- the declared schema -----------------------------------------------------------------------


def _free_text_properties() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (tool.name, name, prop)
        for tool in ALL_TOOLS
        for name, prop in tool.input_schema["properties"].items()
        if isinstance(prop, dict) and prop.get("type") == "string" and "enum" not in prop
    ]


def _expected_cap(tool_name: str, name: str) -> int:
    if name == "transaction_key":
        return 128
    if (tool_name, name) == ("revert_audit_record", "reason"):
        return REVERT_REASON_MAX_LENGTH
    return max_length_for(name)


def test_every_free_text_tool_argument_declares_its_class_cap() -> None:
    properties = _free_text_properties()
    assert properties
    for tool_name, name, prop in properties:
        assert prop.get("maxLength") == _expected_cap(tool_name, name), (tool_name, name, prop)


def test_the_revert_reason_cap_is_the_limit_revert_enforces() -> None:
    assert REVERT_REASON_MAX_LENGTH == audit._MAX_REASON_LENGTH
    schema = BY_NAME["revert_audit_record"].input_schema["properties"]["reason"]
    assert schema["maxLength"] == audit._MAX_REASON_LENGTH


def test_the_cap_classes_are_the_decided_values() -> None:
    assert (LONG_TEXT_MAX_LENGTH, TEXT_MAX_LENGTH, CURSOR_MAX_LENGTH) == (4000, 256, 4000)
    assert {"description", "notes", "body", "reason"} == LONG_TEXT_FIELDS
    assert max_length_for("description") == 4000
    assert max_length_for("name") == 256
    assert max_length_for("query") == 256
    assert max_length_for("cursor") == 4000


# --- top-level tool arguments ----------------------------------------------------------------


def test_create_opportunity_refuses_an_over_cap_description(store: Store) -> None:
    result = _call(
        create_opportunity,
        store,
        {"account_id": _ACCOUNT, "name": "Deal", "description": "x" * (_LONG + 1)},
    )
    _assert_refused(result, "description")


def test_create_opportunity_accepts_an_at_cap_description(store: Store) -> None:
    result = _call(
        create_opportunity,
        store,
        {"account_id": _ACCOUNT, "name": "Deal", "description": "x" * _LONG},
    )
    _assert_accepted(result)


def test_log_activity_refuses_an_over_cap_body_and_accepts_one_at_cap(store: Store) -> None:
    args: dict[str, Any] = {
        "opportunity_id": _OPPORTUNITY,
        "kind": "call",
        "subject": "Check-in",
        "occurred_at": "2026-03-01T09:00:00+00:00",
    }
    _assert_refused(_call(log_activity, store, {**args, "body": "x" * (_LONG + 1)}), "body")
    _assert_accepted(_call(log_activity, store, {**args, "body": "x" * _LONG}))


def test_create_note_refuses_an_over_cap_body(store: Store) -> None:
    result = _call(create_note, store, {"opportunity_id": _OPPORTUNITY, "body": "x" * (_LONG + 1)})
    _assert_refused(result, "body")


def test_create_account_refuses_an_over_cap_name_and_accepts_one_at_cap(store: Store) -> None:
    _assert_refused(_call(create_account, store, {"name": "x" * (_SHORT + 1)}), "name")
    _assert_accepted(_call(create_account, store, {"name": "x" * _SHORT}))


def test_submit_proposal_refuses_an_over_cap_reason(store: Store) -> None:
    result = _call(
        submit_proposal,
        store,
        {
            "target_table": "opportunities",
            "target_id": _OPPORTUNITY,
            "operation": "update",
            "after_payload": {"stage": "Qualification"},
            "reason": "x" * (_LONG + 1),
        },
    )
    _assert_refused(result, "reason")


# --- open field objects ----------------------------------------------------------------------


def test_update_opportunity_refuses_an_over_cap_description_and_accepts_one_at_cap(
    store: Store,
) -> None:
    def update(description: str) -> dict[str, Any]:
        return _call(
            update_opportunity,
            store,
            {"opportunity_id": _OPPORTUNITY, "fields": {"description": description}},
        )

    _assert_refused(update("x" * (_LONG + 1)), "description")
    _assert_accepted(update("x" * _LONG))


def test_update_account_refuses_an_over_cap_short_field(store: Store) -> None:
    result = _call(
        update_account, store, {"account_id": _ACCOUNT, "fields": {"website": "x" * (_SHORT + 1)}}
    )
    _assert_refused(result, "website")


def test_submit_proposal_refuses_an_over_cap_after_payload_field(store: Store) -> None:
    result = _call(
        submit_proposal,
        store,
        {
            "target_table": "opportunities",
            "target_id": _OPPORTUNITY,
            "operation": "update",
            "after_payload": {"name": "x" * (_SHORT + 1)},
            "reason": "rename",
        },
    )
    _assert_refused(result, "name")


@pytest.mark.parametrize(
    ("table", "field", "cap"),
    [
        ("accounts", "notes", 4000),
        ("accounts", "name", 256),
        ("contacts", "email", 256),
        ("opportunities", "description", 4000),
        ("activities", "body", 4000),
        ("activities", "subject", 256),
    ],
)
def test_validate_field_values_caps_each_string_field_by_class(
    table: str, field: str, cap: int
) -> None:
    validate_field_values(table, {field: "x" * cap})
    with pytest.raises(ValidationFailed) as raised:
        validate_field_values(table, {field: "x" * (cap + 1)})
    assert raised.value.source == field
