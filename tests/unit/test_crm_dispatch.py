# SPDX-License-Identifier: MIT
"""Tests for `crm.dispatch`, the direct action-Lambda invoke transport.

Every test injects a `LambdaInvoker` double, so no AWS client is constructed, no credential is
read, and no network call is made.
"""

from __future__ import annotations

import io
import json
from typing import Any

from crm.dispatch import invoke_action


class _RecordingInvoker:
    """A `LambdaInvoker` double returning a fixed response and recording the request."""

    def __init__(self, response: dict[str, object]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return self._response


def _payload(body: object) -> dict[str, object]:
    return {"Payload": io.BytesIO(json.dumps(body).encode("utf-8"))}


def test_invoke_action_sends_a_synchronous_request_and_returns_the_handler_response() -> None:
    """The handler's own wire response is returned verbatim, over a `RequestResponse` invoke."""
    invoker = _RecordingInvoker(_payload({"statusCode": 200, "body": {"items": []}}))

    result = invoke_action("crm-get_account", {"account_id": "acc-1"}, client=invoker)

    assert result == {"statusCode": 200, "body": {"items": []}}
    call = invoker.calls[0]
    assert call["FunctionName"] == "crm-get_account"
    assert call["InvocationType"] == "RequestResponse"
    assert json.loads(call["Payload"]) == {"account_id": "acc-1"}


def test_invoke_action_returns_a_taxonomy_error_response_verbatim() -> None:
    """A handler's own taxonomy response is passed through, never reshaped into a generic error."""
    denial = {"statusCode": 403, "error": {"code": "scope_denied", "message": "Outside your book."}}
    result = invoke_action("crm-get_account", {}, client=_RecordingInvoker(_payload(denial)))
    assert result == denial


def test_invoke_action_maps_a_function_error_to_internal_error_without_leaking_detail() -> None:
    """An unhandled exception in the invoked function never crosses the boundary as a traceback."""

    class _Erroring:
        def invoke(self, **_kwargs: Any) -> dict[str, object]:
            return {
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(b'{"errorMessage": "KeyError: secret_internal_detail"}'),
            }

    result = invoke_action("crm-get_account", {}, client=_Erroring())

    assert result["statusCode"] == 500
    assert result["error"]["code"] == "internal_error"
    assert "secret_internal_detail" not in json.dumps(result)


def test_invoke_action_maps_a_non_object_body_to_internal_error() -> None:
    """A response body that is not a JSON object is an internal error, never returned as-is."""
    for body in ("not an object", [1, 2, 3], 7, None):
        result = invoke_action("crm-get_account", {}, client=_RecordingInvoker(_payload(body)))
        assert result["error"]["code"] == "internal_error", body


def test_invoke_action_maps_an_empty_body_to_internal_error() -> None:
    """An empty payload is an internal error rather than a JSON decode crash."""

    class _Empty:
        def invoke(self, **_kwargs: Any) -> dict[str, object]:
            return {"Payload": io.BytesIO(b"")}

    assert (
        invoke_action("crm-get_account", {}, client=_Empty())["error"]["code"] == "internal_error"
    )
