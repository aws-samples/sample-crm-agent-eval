# SPDX-License-Identifier: MIT
"""Handler and domain error messages carry no caller-supplied value.

A planted caller value never reaches the response; it lives only in ``detail``, bounded by
:data:`crm.errors.DETAIL_VALUE_LIMIT`. An AST scan holds every interpolation in a handler or domain
error message to a declared set of schema-derived expressions.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.domain import allowlists, stages
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import DETAIL_VALUE_LIMIT, CrmError, NotFound, ValidationFailed
from crm.handlers import (
    capabilities,
    create_contact,
    create_note,
    create_opportunity,
    draft_message,
    eval_trigger,
    get_account,
    get_contact,
    get_opportunity,
    get_page_context,
    list_activities,
    log_activity,
    submit_proposal,
    update_account,
    update_activity,
    update_contact,
    update_opportunity,
    user_settings,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import ACTIVITY_KINDS
from crm.toolspec.page_context import EntityPageContext

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OPPORTUNITY = "opp-1"
_PLANTED = "planted-7f3a9c1d"
_LONG_PLANTED = "planted-" + "x" * 200

_SRC: Final[Path] = Path(__file__).resolve().parents[2] / "src" / "crm"


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _event(args: dict[str, Any]) -> dict[str, Any]:
    return identity_event(
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
        args,
    )


def _seed(store: Store) -> None:
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
            "account_id": _ACCOUNT,
            "name": "Acme Renewal",
            "stage": "Discovery",
            "owner_rep_id": _REP,
            "value": 1000,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    )


Handler = Callable[..., dict[str, Any]]

#: (handler module, arguments naming a planted missing id, the fixed message).
_NOT_FOUND_CASES: Final[list[tuple[str, Handler, dict[str, Any], str]]] = [
    ("get_account", get_account.handler, {"account_id": _PLANTED}, "The account does not exist."),
    ("get_contact", get_contact.handler, {"contact_id": _PLANTED}, "The contact does not exist."),
    (
        "get_opportunity",
        get_opportunity.handler,
        {"opportunity_id": _PLANTED},
        "The opportunity does not exist.",
    ),
    (
        "list_activities",
        list_activities.handler,
        {"opportunity_id": _PLANTED},
        "The opportunity does not exist.",
    ),
    (
        "update_account",
        update_account.handler,
        {"account_id": _PLANTED, "fields": {"name": "n"}, "transaction_key": "tx-ua"},
        "The account does not exist.",
    ),
    (
        "update_contact",
        update_contact.handler,
        {"contact_id": _PLANTED, "fields": {"title": "t"}, "transaction_key": "tx-uc"},
        "The contact does not exist.",
    ),
    (
        "update_opportunity",
        update_opportunity.handler,
        {"opportunity_id": _PLANTED, "fields": {"name": "n"}, "transaction_key": "tx-uo"},
        "The opportunity does not exist.",
    ),
    (
        "update_activity",
        update_activity.handler,
        {"activity_id": _PLANTED, "fields": {"subject": "s"}, "transaction_key": "tx-uact"},
        "The activity does not exist.",
    ),
    (
        "create_contact",
        create_contact.handler,
        {"account_id": _PLANTED, "name": "N", "email": "n@example.com", "transaction_key": "tx-cc"},
        "The account does not exist.",
    ),
    (
        "create_opportunity",
        create_opportunity.handler,
        {"account_id": _PLANTED, "name": "N", "value": 5, "transaction_key": "tx-co"},
        "The account does not exist.",
    ),
    (
        "create_note",
        create_note.handler,
        {"opportunity_id": _PLANTED, "body": "b", "transaction_key": "tx-cn"},
        "The opportunity does not exist.",
    ),
    (
        "log_activity",
        log_activity.handler,
        {
            "opportunity_id": _PLANTED,
            "kind": ACTIVITY_KINDS[0],
            "subject": "s",
            "transaction_key": "tx-la",
        },
        "The opportunity does not exist.",
    ),
    (
        "draft_message",
        draft_message.handler,
        {"contact_id": _PLANTED, "intent": "follow up"},
        "The contact does not exist.",
    ),
    (
        "submit_proposal",
        submit_proposal.handler,
        {
            "target_table": "opportunities",
            "target_id": _PLANTED,
            "operation": "update",
            "after_payload": {"value": 5},
            "reason": "r",
            "transaction_key": "tx-sp",
        },
        "The target record does not exist.",
    ),
]


@pytest.mark.parametrize(
    ("name", "handler", "arguments", "message"),
    _NOT_FOUND_CASES,
    ids=[case[0] for case in _NOT_FOUND_CASES],
)
def test_a_missing_record_never_echoes_the_caller_id(
    store: Store, name: str, handler: Handler, arguments: dict[str, Any], message: str
) -> None:
    """Each handler's 404 is its fixed message, and the planted id appears nowhere in the reply."""
    _seed(store)
    result = handler(_event(arguments), store=store)
    assert result["statusCode"] == 404, (name, result)
    assert result["error"]["code"] == "not_found", (name, result)
    assert result["error"]["message"] == message, (name, result)
    assert _PLANTED not in json.dumps(result), (name, result)


def test_draft_message_missing_opportunity_never_echoes_the_caller_id(store: Store) -> None:
    """`draft_message`'s second lookup, the optional opportunity, is fixed text too."""
    _seed(store)
    store.put_item(
        "contacts",
        {"contact_id": "con-1", "tenant": _TENANT, "account_id": _ACCOUNT, "name": "Pat"},
    )
    result = draft_message.handler(
        _event({"contact_id": "con-1", "opportunity_id": _PLANTED, "intent": "follow up"}),
        store=store,
    )
    assert result["statusCode"] == 404, result
    assert result["error"]["message"] == "The opportunity does not exist."
    assert _PLANTED not in json.dumps(result)


def _identity() -> Identity:
    return Identity(
        rep_id=_REP,
        book=frozenset({_ACCOUNT}),
        full_visibility=False,
        principal_type="user",
        session_id="sess-1",
        source="gateway",
        tenant=_TENANT,
    )


def test_a_missing_page_record_never_echoes_the_entity_id(store: Store) -> None:
    context = EntityPageContext(route="/accounts/x", entity_type="accounts", entity_id=_PLANTED)
    with pytest.raises(NotFound) as raised:
        get_page_context._resolve_entity(store, _identity(), context)
    assert raised.value.message == "The page's record does not exist."
    assert _PLANTED not in json.dumps(raised.value.to_api_response())


def test_an_unknown_stage_is_named_by_the_permitted_set_not_echoed(store: Store) -> None:
    """The submit_proposal path through `stages`, and create_opportunity's own stage check."""
    _seed(store)
    proposal = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "after_payload": {"stage": _PLANTED},
                "reason": "r",
                "transaction_key": "tx-stage",
            }
        ),
        store=store,
    )
    created = create_opportunity.handler(
        _event(
            {
                "account_id": _ACCOUNT,
                "name": "N",
                "value": 5,
                "stage": _PLANTED,
                "transaction_key": "tx-co-stage",
            }
        ),
        store=store,
    )
    assert proposal["error"]["message"].startswith("Unrecognised stage; expected one of "), proposal
    for result, source in ((proposal, "after_payload.stage"), (created, "stage")):
        assert result["statusCode"] == 400, result
        assert result["error"]["source"] == source, result
        assert _PLANTED not in json.dumps(result), result


def test_the_stage_rejection_keeps_the_value_in_a_bounded_detail() -> None:
    with pytest.raises(ValidationFailed) as raised:
        stages.classify_requested_transition("Discovery", _LONG_PLANTED, source="stage")
    detail = raised.value.detail or ""
    assert repr(_LONG_PLANTED[:DETAIL_VALUE_LIMIT]) in detail
    assert _LONG_PLANTED not in detail


@pytest.mark.parametrize(
    "check",
    [
        lambda keys: capabilities._require_no_arguments(dict.fromkeys(keys, 1)),
        lambda keys: eval_trigger._require_action(dict.fromkeys(keys, 1)),
        lambda keys: eval_trigger._require_action({"action": "status", **dict.fromkeys(keys, 1)}),
        lambda keys: user_settings._require_action(dict.fromkeys(keys, 1)),
    ],
    ids=["capabilities", "eval_trigger_unrecognised", "eval_trigger_extra", "user_settings"],
)
def test_an_unrecognised_argument_key_is_never_echoed(check: Callable[[list[str]], None]) -> None:
    """The message names the recognised set; the caller's key lives only in a bounded detail."""
    with pytest.raises(ValidationFailed) as raised:
        check([_LONG_PLANTED])
    error = raised.value
    assert _LONG_PLANTED[:16] not in json.dumps(error.to_api_response())
    assert error.source == "arguments"
    assert _LONG_PLANTED not in (error.detail or "")


@pytest.mark.parametrize(
    "check",
    [
        lambda: allowlists.validate_update_fields("accounts", {_LONG_PLANTED: 1}),
        lambda: allowlists.validate_proposal_payload("opportunities", {_LONG_PLANTED: 1}),
    ],
    ids=["update", "proposal"],
)
def test_the_allowlist_detail_bounds_the_rejected_keys(check: Callable[[], None]) -> None:
    with pytest.raises(ValidationFailed) as raised:
        check()
    detail = raised.value.detail or ""
    assert "rejected_count=1" in detail
    assert repr(_LONG_PLANTED[:DETAIL_VALUE_LIMIT]) in detail
    assert _LONG_PLANTED not in detail


# -- the AST scan -----------------------------------------------------------------------------

#: Error classes whose first argument is the user-facing message.
_ERROR_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "CrmError",
        "AuthenticationFailed",
        "ScopeDenied",
        "NotFound",
        "ValidationFailed",
        "Conflict",
        "ServiceBusy",
    }
)

#: Every expression a handler or domain error message may interpolate: schema constants, or a
#: caller value only after it has been checked against a schema-declared closed set.
_ALLOWED_INTERPOLATIONS: Final[frozenset[str]] = frozenset(
    {
        "list(STAGES)",
        "list(ACTIONS)",
        "sorted(_RECOGNISED_ARGUMENTS)",
        "sorted(ARGUMENTS_SCHEMA['properties'])",
        "sorted({'action'} | _ACTION_ARGUMENTS[action])",
        # `action` is a member of ACTIONS by this point.
        "action",
        # Both stages are members of STAGES by this point (classify_requested_transition).
        "from_stage",
        "to_stage",
        # A schema table name, a schema field, its declared type and its permitted set.
        "table",
        "key",
        "_type_name(rule)",
        "permitted",
        "', '.join(sorted(allowed))",
        # `named` quotes the field only when it is one of the table's schema fields.
        "named",
        "proposal_route",
    }
)

_SCANNED_PACKAGES: Final[tuple[str, ...]] = ("handlers", "domain")


def _message_interpolations(source: str, filename: str) -> list[tuple[str, int, str]]:
    """Every ``(file, line, expression)`` an error-class message f-string interpolates."""
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name not in _ERROR_CLASSES:
            continue
        message = node.args[0] if node.args else None
        if message is None:
            message = next((k.value for k in node.keywords if k.arg == "message"), None)
        if not isinstance(message, ast.JoinedStr):
            continue
        for part in message.values:
            if isinstance(part, ast.FormattedValue):
                found.append((filename, node.lineno, ast.unparse(part.value)))
    return found


def _scan_tree() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for package in _SCANNED_PACKAGES:
        for path in sorted((_SRC / package).rglob("*.py")):
            found.extend(_message_interpolations(path.read_text(encoding="utf-8"), str(path)))
    return found


def test_no_handler_or_domain_message_interpolates_an_undeclared_expression() -> None:
    found = _scan_tree()
    assert found, "the scan found no interpolation at all, so it read nothing"
    undeclared = [entry for entry in found if entry[2] not in _ALLOWED_INTERPOLATIONS]
    assert undeclared == [], undeclared


def test_the_scan_reports_a_planted_caller_id_interpolation() -> None:
    """Control: a NotFound message interpolating a caller-supplied id is reported."""
    planted = 'raise NotFound(f"No such account: {account_id!r}.")\n'
    found = _message_interpolations(planted, "planted.py")
    assert found == [("planted.py", 1, "account_id")]
    assert found[0][2] not in _ALLOWED_INTERPOLATIONS


def test_the_scanned_error_classes_exist() -> None:
    import crm.errors as errors

    for name in _ERROR_CLASSES:
        assert issubclass(getattr(errors, name), CrmError), name
