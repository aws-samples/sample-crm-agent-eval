# SPDX-License-Identifier: MIT
"""Contract tests for the API Gateway proxy channel of every action Lambda, in both payload formats.

Fixtures:

* `tests/fixtures/events/apigw_v2_proxy_post_get_account.json` -- a captured format-2.0 (HTTP API)
  envelope.
* `tests/fixtures/events/apigw_v1_proxy_post_get_account.json` -- a format-1.0 (REST `AWS_PROXY`)
  envelope, the format the deployed API sends, constructed from the documented format-1.0 shape. Its
  `requestContext.identity` VPC endpoint fields are omitted.

Both carry the route `POST /get_account`, stage `prod`, and the same Cognito claim set; the account
id is the placeholder and the bearer token is redacted.

Contracts asserted:

* **Payload format** -- `crm.handlers._common.is_proxy_event` accepts both envelopes and rejects
  every flat direct-invoke/Gateway event, including one carrying a tool argument named `body`; the
  Terraform integration type is read directly.
* **Request parse** -- the tool's arguments come out of the JSON `body` (including a base64-encoded
  body), never out of the envelope's own keys, and an identity channel key planted inside the body
  cannot widen the caller's scope.
* **Response shape** -- success and every typed `crm.errors.CrmError` render the structured proxy
  response (`statusCode`/`headers`/string `body`/`isBase64Encoded`).

The flat Gateway and direct-invoke agent channels are asserted unchanged.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.handlers import _common, get_account
from crm.handlers._common import is_proxy_event
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVENT_PATH = _REPO_ROOT / "tests" / "fixtures" / "events"
_CAPTURED_EVENT = _EVENT_PATH / "apigw_v2_proxy_post_get_account.json"
_V1_EVENT = _EVENT_PATH / "apigw_v1_proxy_post_get_account.json"
_API_MAIN_TF = _REPO_ROOT / "infra" / "modules" / "api" / "main.tf"


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


#: The captured event's own Cognito claims, read from the fixture.
_CLAIMS: dict[str, Any] = _load(_CAPTURED_EVENT)["requestContext"]["authorizer"]["jwt"]["claims"]
_TENANT: str = _CLAIMS["custom:tenant"]
_REP: str = _CLAIMS["custom:rep_id"]
_IN_BOOK_ACCOUNT: str = _CLAIMS["custom:book"].split(",")[0]
_OUT_OF_BOOK_ACCOUNT = "acct-eval-smoke-9999"


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _captured_event(body: Any = None, *, base64_encoded: bool = False) -> dict[str, Any]:
    """Return the captured envelope with ``body`` substituted, everything else untouched.

    Args:
        body: A mapping serialised to a JSON string, an already-serialised string, or ``None`` to
            keep the captured body as-is.
        base64_encoded: Base64-encode the body and set ``isBase64Encoded``, the shape API Gateway
            sends for a binary content type.
    """
    return _substituted(_CAPTURED_EVENT, body, base64_encoded=base64_encoded)


def _v1_event(body: Any = None, *, base64_encoded: bool = False) -> dict[str, Any]:
    """Return the format-1.0 REST envelope with ``body`` substituted, everything else untouched."""
    return _substituted(_V1_EVENT, body, base64_encoded=base64_encoded)


def _substituted(path: Path, body: Any, *, base64_encoded: bool = False) -> dict[str, Any]:
    event: dict[str, Any] = copy.deepcopy(_load(path))
    if body is not None:
        raw = body if isinstance(body, str) else json.dumps(body)
        if base64_encoded:
            event["body"] = base64.b64encode(raw.encode("utf-8")).decode("ascii")
            event["isBase64Encoded"] = True
        else:
            event["body"] = raw
    return event


def _seed_account(store: Store, account_id: str) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": _TENANT,
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "owner_rep_id": _REP,
            "name": "Acme",
            "name_lower": "acme",
        },
    )


def _decoded_body(response: dict[str, Any]) -> Any:
    """Assert the structured proxy response contract, returning the parsed ``body``.

    Both payload formats require the same four-key shape.
    """
    assert set(response) == {"statusCode", "headers", "body", "isBase64Encoded"}
    assert isinstance(response["statusCode"], int)
    assert response["headers"] == {"content-type": "application/json"}
    assert response["isBase64Encoded"] is False
    body = response["body"]
    assert isinstance(body, str), (
        "a format-2.0 proxy integration rejects a non-string body with "
        "'Lambda body contains the wrong type for field \"body\"'"
    )
    parsed: Any = json.loads(body)
    return parsed


def test_captured_event_is_recognised_as_a_proxy_event() -> None:
    assert is_proxy_event(_captured_event()) is True


def test_the_deployed_format_1_0_envelope_is_recognised_as_a_proxy_event() -> None:
    """The format-1.0 REST envelope is a proxy event."""
    assert is_proxy_event(_v1_event()) is True


def test_a_flat_gateway_event_carrying_request_context_is_not_a_proxy_event() -> None:
    assert is_proxy_event({"requestContext": {"authorizer": {}}, "account_id": "acct-1"}) is False


def test_a_flat_event_carrying_a_tool_argument_named_body_is_not_a_proxy_event() -> None:
    """A flat event carrying a tool argument named `body` is not a proxy event.

    The test also asserts from the toolspec that `body` is a real tool argument.
    """
    tools_with_a_body_argument = [
        tool.name for tool in ALL_TOOLS if "body" in (tool.input_schema.get("properties") or {})
    ]
    assert tools_with_a_body_argument, (
        "no tool declares a `body` argument any more, so this test no longer pins the collision it "
        "was written for -- re-derive the trap before deleting it"
    )

    flat_event = {
        "_identity": {"rep_id": _REP, "source": "gateway", "tenant": _TENANT},
        "target_table": "accounts",
        "target_id": _IN_BOOK_ACCOUNT,
        "body": "Called the customer. Not JSON, and not an envelope body.",
    }

    assert is_proxy_event(flat_event) is False


def test_no_tool_argument_can_ever_satisfy_the_format_1_0_discriminator() -> None:
    """No toolspec property is named `resource`, `path`, or `httpMethod`, the format-1.0
    discriminator keys."""
    declared: set[str] = set()
    for tool in ALL_TOOLS:
        declared |= set((tool.input_schema.get("properties") or {}).keys())

    assert declared.isdisjoint(_common.PROXY_V1_ENVELOPE_KEYS), (
        "a tool argument now shares a name with a format-1.0 envelope discriminator key; "
        f"overlap={sorted(declared & set(_common.PROXY_V1_ENVELOPE_KEYS))}"
    )


def test_the_predicate_covers_the_payload_format_the_deployed_integration_sends() -> None:
    """The API module declares a REST (`aws_api_gateway_*`) `AWS_PROXY` integration and no v2
    API."""
    declared = _API_MAIN_TF.read_text(encoding="utf-8")
    resource_declarations = re.findall(r'^resource\s+"([a-z0-9_]+)"', declared, flags=re.MULTILINE)

    assert "aws_api_gateway_integration" in resource_declarations, (
        "infra/modules/api/main.tf no longer declares a REST integration, so the format-1.0 branch "
        "of is_proxy_event may no longer describe what is deployed"
    )
    assert not [name for name in resource_declarations if name.startswith("aws_apigatewayv2_")], (
        "a v2 API resource reappeared; both formats are still recognised, but the fixture set and "
        "this file's provenance notes need re-deriving"
    )
    assert re.search(r'type\s*=\s*"AWS_PROXY"', declared), (
        "the integration is no longer AWS_PROXY, so no proxy envelope reaches the handlers at all"
    )


def test_arguments_are_parsed_from_the_body_and_the_response_is_format_2_0(store: Store) -> None:
    _seed_account(store, _IN_BOOK_ACCOUNT)

    response = get_account.handler(
        _captured_event({"account_id": _IN_BOOK_ACCOUNT}), None, store=store
    )

    assert response["statusCode"] == 200
    payload = _decoded_body(response)
    assert payload["account"]["account_id"] == _IN_BOOK_ACCOUNT
    assert payload["contacts"] == []


def test_a_base64_encoded_body_is_decoded_before_parsing(store: Store) -> None:
    _seed_account(store, _IN_BOOK_ACCOUNT)

    response = get_account.handler(
        _captured_event({"account_id": _IN_BOOK_ACCOUNT}, base64_encoded=True), None, store=store
    )

    assert response["statusCode"] == 200
    assert _decoded_body(response)["account"]["account_id"] == _IN_BOOK_ACCOUNT


def test_a_scope_denial_reaches_the_caller_as_403_with_a_parseable_body(store: Store) -> None:
    _seed_account(store, _OUT_OF_BOOK_ACCOUNT)

    response = get_account.handler(
        _captured_event({"account_id": _OUT_OF_BOOK_ACCOUNT}), None, store=store
    )

    assert response["statusCode"] == 403
    payload = _decoded_body(response)
    assert payload["error"]["code"] == "scope_denied"
    assert payload["error"]["missing_scope"] == f"account:{_OUT_OF_BOOK_ACCOUNT}"


def test_a_missing_record_reaches_the_caller_as_404_with_a_parseable_body(store: Store) -> None:
    response = get_account.handler(
        _captured_event({"account_id": _IN_BOOK_ACCOUNT}), None, store=store
    )

    assert response["statusCode"] == 404
    assert _decoded_body(response)["error"]["code"] == "not_found"


def test_a_malformed_body_is_a_400_not_an_unhandled_exception(store: Store) -> None:
    response = get_account.handler(_captured_event("{not json"), None, store=store)

    assert response["statusCode"] == 400
    payload = _decoded_body(response)
    assert payload["error"]["code"] == "validation_failed"
    assert payload["error"]["source"] == "body"


def test_a_body_that_is_not_an_object_is_a_400(store: Store) -> None:
    response = get_account.handler(_captured_event("[1, 2, 3]"), None, store=store)

    assert response["statusCode"] == 400
    assert _decoded_body(response)["error"]["code"] == "validation_failed"


def test_an_identity_key_planted_in_the_body_cannot_widen_scope(store: Store) -> None:
    _seed_account(store, _OUT_OF_BOOK_ACCOUNT)

    response = get_account.handler(
        _captured_event(
            {
                "account_id": _OUT_OF_BOOK_ACCOUNT,
                "_identity": {
                    "rep_id": "attacker",
                    "book": [_OUT_OF_BOOK_ACCOUNT],
                    "full_visibility": True,
                    "source": "gateway",
                    "tenant": _TENANT,
                },
            }
        ),
        None,
        store=store,
    )

    assert response["statusCode"] == 403
    assert _decoded_body(response)["error"]["missing_scope"] == f"account:{_OUT_OF_BOOK_ACCOUNT}"


def test_the_flat_gateway_channel_response_shape_is_unchanged(store: Store) -> None:
    _seed_account(store, _IN_BOOK_ACCOUNT)
    flat_event = {
        "_identity": {
            "rep_id": _REP,
            "book": [_IN_BOOK_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-1",
            "source": "gateway",
            "tenant": _TENANT,
        },
        "account_id": _IN_BOOK_ACCOUNT,
    }

    response = get_account.handler(flat_event, None, store=store)

    assert set(response) == {"statusCode", "body"}
    assert response["statusCode"] == 200
    assert isinstance(response["body"], dict)
    assert response["body"]["account"]["account_id"] == _IN_BOOK_ACCOUNT


def test_the_flat_agent_channel_error_shape_is_unchanged(store: Store) -> None:
    flat_event = {
        "_agent_identity": {
            "claims": {
                "custom:rep_id": _REP,
                "custom:book": _IN_BOOK_ACCOUNT,
                "custom:full_visibility": "false",
                "custom:tenant": _TENANT,
            },
            "session_id": "sess-1",
        },
        "account_id": _IN_BOOK_ACCOUNT,
    }

    response = get_account.handler(flat_event, None, store=store)

    assert set(response) == {"statusCode", "error"}
    assert response["statusCode"] == 404
    assert response["error"]["code"] == "not_found"


def test_the_captured_event_replayed_unmodified_is_served(store: Store) -> None:
    """The captured envelope, replayed exactly as recorded, is served from its recorded `body`."""
    _seed_account(store, _IN_BOOK_ACCOUNT)
    event = copy.deepcopy(_load(_CAPTURED_EVENT))
    assert json.loads(event["body"])["account_id"] == _IN_BOOK_ACCOUNT, (
        "the capture's own body no longer names the in-book account, so this asserts nothing"
    )

    response = get_account.handler(event, None, store=store)

    assert response["statusCode"] == 200
    assert _decoded_body(response)["account"]["account_id"] == _IN_BOOK_ACCOUNT


# ---------------------------------------------------------------------------
# Format 1.0 (REST `AWS_PROXY`).
# ---------------------------------------------------------------------------

#: Both envelope builders, keyed by format for the parametrisation id.
_ENVELOPE_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "format-1.0-rest": _v1_event,
    "format-2.0-http": _captured_event,
}


def test_both_fixtures_carry_the_same_claims_under_their_own_authorizer_nesting() -> None:
    """Both fixtures carry identical claims, under `authorizer.claims` (REST) and
    `authorizer.jwt.claims` (HTTP API) respectively."""
    v1_authorizer = _load(_V1_EVENT)["requestContext"]["authorizer"]
    v2_authorizer = _load(_CAPTURED_EVENT)["requestContext"]["authorizer"]

    assert "jwt" not in v1_authorizer, "the format-1.0 fixture must use the REST claims nesting"
    assert v1_authorizer["claims"] == v2_authorizer["jwt"]["claims"]


@pytest.mark.parametrize("build", _ENVELOPE_BUILDERS.values(), ids=_ENVELOPE_BUILDERS.keys())
def test_a_proxy_success_carries_the_tools_result_in_a_json_string_body(
    store: Store, build: Callable[..., dict[str, Any]]
) -> None:
    """A proxy success, in both formats, is a 2xx whose `body` is a JSON string of the tool's
    result."""
    _seed_account(store, _IN_BOOK_ACCOUNT)

    response = get_account.handler(build({"account_id": _IN_BOOK_ACCOUNT}), None, store=store)

    assert response["statusCode"] == 200
    assert isinstance(response["body"], str)
    payload = _decoded_body(response)
    assert payload["account"]["account_id"] == _IN_BOOK_ACCOUNT
    assert payload["contacts"] == []


@pytest.mark.parametrize("build", _ENVELOPE_BUILDERS.values(), ids=_ENVELOPE_BUILDERS.keys())
def test_a_proxy_error_carries_a_json_string_body_too(
    store: Store, build: Callable[..., dict[str, Any]]
) -> None:
    """A proxy error, in both formats, carries its typed status and a JSON string `body`."""
    _seed_account(store, _OUT_OF_BOOK_ACCOUNT)

    response = get_account.handler(build({"account_id": _OUT_OF_BOOK_ACCOUNT}), None, store=store)

    assert response["statusCode"] == 403
    assert isinstance(response["body"], str)
    payload = _decoded_body(response)
    assert payload["error"]["code"] == "scope_denied"
    assert payload["error"]["missing_scope"] == f"account:{_OUT_OF_BOOK_ACCOUNT}"


def test_the_format_1_0_envelope_replayed_unmodified_is_served(store: Store) -> None:
    """The format-1.0 fixture replayed as recorded, so its *own* `body` is what is parsed."""
    _seed_account(store, _IN_BOOK_ACCOUNT)
    event = copy.deepcopy(_load(_V1_EVENT))
    assert json.loads(event["body"])["account_id"] == _IN_BOOK_ACCOUNT, (
        "the fixture's own body no longer names the in-book account, so this asserts nothing"
    )

    response = get_account.handler(event, None, store=store)

    assert response["statusCode"] == 200
    assert _decoded_body(response)["account"]["account_id"] == _IN_BOOK_ACCOUNT


def test_a_format_1_0_base64_encoded_body_is_decoded_before_parsing(store: Store) -> None:
    _seed_account(store, _IN_BOOK_ACCOUNT)

    response = get_account.handler(
        _v1_event({"account_id": _IN_BOOK_ACCOUNT}, base64_encoded=True), None, store=store
    )

    assert response["statusCode"] == 200
    assert _decoded_body(response)["account"]["account_id"] == _IN_BOOK_ACCOUNT


def test_a_malformed_format_1_0_body_is_a_400_not_an_unhandled_exception(store: Store) -> None:
    response = get_account.handler(_v1_event("{not json"), None, store=store)

    assert response["statusCode"] == 400
    payload = _decoded_body(response)
    assert payload["error"]["code"] == "validation_failed"
    assert payload["error"]["source"] == "body"


def test_an_identity_key_planted_in_a_format_1_0_body_cannot_widen_scope(store: Store) -> None:
    """An identity key planted in a format-1.0 body is stripped and cannot widen scope."""
    _seed_account(store, _OUT_OF_BOOK_ACCOUNT)

    response = get_account.handler(
        _v1_event(
            {
                "account_id": _OUT_OF_BOOK_ACCOUNT,
                "_identity": {
                    "rep_id": "attacker",
                    "book": [_OUT_OF_BOOK_ACCOUNT],
                    "full_visibility": True,
                    "source": "gateway",
                    "tenant": _TENANT,
                },
            }
        ),
        None,
        store=store,
    )

    assert response["statusCode"] == 403
    assert _decoded_body(response)["error"]["missing_scope"] == f"account:{_OUT_OF_BOOK_ACCOUNT}"


def test_a_format_1_0_envelope_without_an_authorizer_is_a_401_not_an_empty_scope_identity(
    store: Store,
) -> None:
    """A format-1.0 envelope with no authorizer is a 401."""
    event = _v1_event({"account_id": _IN_BOOK_ACCOUNT})
    del event["requestContext"]["authorizer"]

    response = get_account.handler(event, None, store=store)

    assert response["statusCode"] == 401
    assert _decoded_body(response)["error"]["code"] == "authentication_failed"
