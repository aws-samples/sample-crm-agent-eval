# SPDX-License-Identifier: MIT
"""Every handler ``CrmError`` is logged as one structured ``crm_error`` line, never with the request.

Drives real handler entry points (format-1.0 proxy events built from the captured fixture, and the
flat Gateway channel) against a `moto`-mocked DynamoDB, and reads the emitted records with
``caplog``. Asserted: one WARNING ``crm_error`` line per error, ``reason`` is the error code plus its
bounded ``detail``, ``rep_id`` appears only when the identity was decoded, the response is the
error's own API response, and a bearer token carried in the event reaches no log record.
"""

from __future__ import annotations

import copy
import json
import logging
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import jwt
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import AuthenticationFailed, CrmError, ValidationFailed
from crm.handlers import _common, get_account
from crm.handlers._common import ERROR_LOG_DETAIL_MAX_CHARS, error_response, run_handler
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_V1_EVENT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "events"
    / "apigw_v1_proxy_post_get_account.json"
)


def _mint_fake_token() -> str:
    """Return a freshly signed HS256 JWT under a random per-process key."""
    return jwt.PyJWS().encode(
        b'{"sub":"fake-claims-for-log-test"}', secrets.token_bytes(32), algorithm="HS256"
    )


_FAKE_TOKEN = _mint_fake_token()
_OUT_OF_BOOK_ACCOUNT = "acct-eval-smoke-9999"


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


@pytest.fixture
def caplog_all(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def _v1_event(*, account_id: str, with_claims: bool) -> dict[str, Any]:
    event: dict[str, Any] = copy.deepcopy(json.loads(_V1_EVENT.read_text(encoding="utf-8")))
    bearer = f"Bearer {_FAKE_TOKEN}"
    event["headers"]["Authorization"] = bearer
    event["multiValueHeaders"]["Authorization"] = [bearer]
    event["body"] = json.dumps({"account_id": account_id})
    if not with_claims:
        event["requestContext"].pop("authorizer", None)
    return event


def _seed_out_of_book_account(store: Store, tenant: str) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": _OUT_OF_BOOK_ACCOUNT,
            "tenant": tenant,
            "owner_rep_id": "rep-elsewhere",
            "name": "Elsewhere",
        },
    )


def _claims() -> dict[str, Any]:
    claims: dict[str, Any] = json.loads(_V1_EVENT.read_text(encoding="utf-8"))["requestContext"][
        "authorizer"
    ]["claims"]
    return claims


def _crm_error_lines(
    caplog: pytest.LogCaptureFixture,
) -> list[tuple[logging.LogRecord, dict[str, Any]]]:
    lines = []
    for record in caplog.records:
        try:
            parsed = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") == "crm_error":
            lines.append((record, parsed))
    return lines


def _assert_token_absent(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        rendered = record.getMessage()
        assert _FAKE_TOKEN not in rendered
        assert _FAKE_TOKEN not in repr(record.args)
    assert _FAKE_TOKEN not in caplog.text


def test_scope_denial_logs_one_line_naming_the_operation_and_rep(
    store: Store, caplog_all: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRM_API_ALLOWED_CLIENT_IDS", str(_claims()["client_id"]))
    _seed_out_of_book_account(store, str(_claims()["custom:tenant"]))
    event = _v1_event(account_id=_OUT_OF_BOOK_ACCOUNT, with_claims=True)

    response = get_account.handler(event, store=store)

    assert response["statusCode"] == 403
    lines = _crm_error_lines(caplog_all)
    assert len(lines) == 1
    record, line = lines[0]
    assert record.levelno == logging.WARNING
    assert record.name == _common.__name__
    assert set(line) == {"event", "reason", "rep_id"}
    assert line["reason"].startswith("scope_denied: ")
    assert "operation=" in line["reason"]
    assert line["rep_id"] == _claims()["custom:rep_id"]
    _assert_token_absent(caplog_all)


def test_authentication_failure_logs_one_line_without_rep_id(
    store: Store, caplog_all: pytest.LogCaptureFixture
) -> None:
    event = _v1_event(account_id=_OUT_OF_BOOK_ACCOUNT, with_claims=False)

    response = get_account.handler(event, store=store)

    assert response["statusCode"] == 401
    lines = _crm_error_lines(caplog_all)
    assert len(lines) == 1
    record, line = lines[0]
    assert record.levelno == logging.WARNING
    assert set(line) == {"event", "reason"}
    assert line["reason"].startswith("authentication_failed: ")
    _assert_token_absent(caplog_all)


def test_gateway_channel_denial_is_logged_with_rep_id(
    store: Store, caplog_all: pytest.LogCaptureFixture
) -> None:
    identity = {
        "rep_id": "rep-1",
        "book": ["acct-1"],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": "demo",
    }
    _seed_out_of_book_account(store, "demo")
    event = identity_event(IDENTITY_KEY, identity, {"account_id": _OUT_OF_BOOK_ACCOUNT})

    response = get_account.handler(event, store=store)

    assert response["statusCode"] == 403
    lines = _crm_error_lines(caplog_all)
    assert [line for _, line in lines] == [
        {"event": "crm_error", "reason": lines[0][1]["reason"], "rep_id": "rep-1"}
    ]
    assert "operation=" in lines[0][1]["reason"]


def test_successful_call_logs_no_crm_error(
    store: Store, caplog_all: pytest.LogCaptureFixture
) -> None:
    store.put_item(
        "accounts",
        {"account_id": "acct-1", "tenant": "demo", "owner_rep_id": "rep-1", "name": "Acme"},
    )
    identity = {
        "rep_id": "rep-1",
        "book": ["acct-1"],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": "demo",
    }

    response = get_account.handler(
        identity_event(IDENTITY_KEY, identity, {"account_id": "acct-1"}), store=store
    )

    assert response["statusCode"] == 200
    assert _crm_error_lines(caplog_all) == []


_GET_ACCOUNT_TOOL = next(tool for tool in ALL_TOOLS if tool.name == "get_account")


def _gateway_event() -> dict[str, Any]:
    identity = {
        "rep_id": "rep-1",
        "book": ["acct-1"],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": "demo",
    }
    return identity_event(IDENTITY_KEY, identity, {"account_id": "acct-1"})


@pytest.mark.parametrize(
    "error",
    [
        ValidationFailed("bad", source="x", detail="d" * (ERROR_LOG_DETAIL_MAX_CHARS + 50)),
        AuthenticationFailed("no", detail=None),
        CrmError("boom", detail="internal"),
    ],
    ids=["long-detail", "no-detail", "base"],
)
def test_reason_is_code_and_bounded_detail_and_response_is_unchanged(
    caplog_all: pytest.LogCaptureFixture, error: CrmError
) -> None:
    def _raise(identity: Any, arguments: Any) -> Any:
        raise error

    response = run_handler(_gateway_event(), _raise, tool=_GET_ACCOUNT_TOOL)

    assert response == error_response(error)
    lines = _crm_error_lines(caplog_all)
    assert len(lines) == 1
    expected_detail = (error.detail or "")[:ERROR_LOG_DETAIL_MAX_CHARS]
    assert lines[0][1]["reason"] == f"{error.code}: {expected_detail}"
    assert len(lines[0][1]["reason"]) <= len(error.code) + 2 + ERROR_LOG_DETAIL_MAX_CHARS
