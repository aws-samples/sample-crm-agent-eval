# SPDX-License-Identifier: MIT
"""Tests for the ``capabilities`` handler: the wire contract, identity, and the unavailable path.

Happy-path assertions run against the real ``load_capability_matrix()``. Events come from
``tests/fixtures/events/apigw_v1_proxy_post_get_account.json``, retargeted by
:func:`_captured_v1_envelope` at a ``GET`` on ``/capabilities``. :data:`_FAMILIES` and
:data:`_TIERS` are read out of ``capabilityMatrix.ts``.
"""

from __future__ import annotations

import builtins
import copy
import dataclasses
import json
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from scripts.smoke.runtime_bedrock_iam import RETIRED_INFERENCE_PROFILES

import providers.capabilities as capabilities_module
from crm.errors import ScopeDenied, ValidationFailed
from crm.handlers import capabilities as handler_module
from providers.capabilities import load_capability_matrix

_REPO_ROOT = Path(__file__).resolve().parents[2]
_V1_EVENT_PATH = (
    _REPO_ROOT / "tests" / "fixtures" / "events" / "apigw_v1_proxy_post_get_account.json"
)
_CLIENT_TS = _REPO_ROOT / "frontend" / "src" / "components" / "tuning" / "capabilityMatrix.ts"

#: The route the deployed REST API serves this handler on.
_RESOURCE = "/capabilities"

#: The client function whose declared HTTP method must match the route the root provisions.
_CLIENT_FETCH_FN = "fetchCapabilityMatrix"


def _read_const_tuple(source: str, name: str) -> tuple[str, ...]:
    """Return the string members of an ``export const <name> = [...] as const;`` declaration."""
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", source, re.DOTALL)
    assert match is not None, f"{name} is no longer declared in {_CLIENT_TS.name}"
    members = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    assert members, f"{name} parsed to an empty tuple; the assertions below would verify nothing"
    return members


_CLIENT_SOURCE = _CLIENT_TS.read_text(encoding="utf-8")
_FAMILIES = _read_const_tuple(_CLIENT_SOURCE, "FAMILIES")
_TIERS = _read_const_tuple(_CLIENT_SOURCE, "TIERS")


def _captured_v1_envelope() -> dict[str, Any]:
    """The captured format-1.0 envelope, retargeted at ``GET /capabilities`` with no body."""
    event: dict[str, Any] = copy.deepcopy(json.loads(_V1_EVENT_PATH.read_text(encoding="utf-8")))
    event["resource"] = _RESOURCE
    event["path"] = _RESOURCE
    event["httpMethod"] = "GET"
    event["body"] = None
    event["requestContext"]["httpMethod"] = "GET"
    event["requestContext"]["path"] = _RESOURCE
    event["requestContext"]["resourcePath"] = _RESOURCE
    return event


def _import_raising_for(target: str, real_import: Callable[..., Any]) -> Callable[..., Any]:
    """An ``__import__`` replacement that fails for ``target`` and delegates everything else."""

    def _guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == target:
            raise ImportError(f"No module named {target!r}")
        return real_import(name, *args, **kwargs)

    return _guarded


def _decoded_proxy_body(response: dict[str, Any]) -> Any:
    """Assert the full structured proxy-response contract and return the parsed ``body``."""
    assert set(response) == {"statusCode", "headers", "body", "isBase64Encoded"}
    assert isinstance(response["statusCode"], int)
    assert response["headers"] == {"content-type": "application/json"}
    assert response["isBase64Encoded"] is False
    body = response["body"]
    assert isinstance(body, str), (
        "an AWS_PROXY integration rejects a structured response whose body is an object with "
        '`Lambda body contains the wrong type for field "body"`, which API Gateway surfaces to the '
        f"caller as a 5xx the Lambda never logged; got {type(body).__name__}"
    )
    return json.loads(body)


# -- the payload contract -------------------------------------------------------------------------


def test_the_served_payload_is_exactly_what_providers_capabilities_serialises() -> None:
    """The served body equals the JSON round-trip of ``asdict(load_capability_matrix())``."""
    expected = json.loads(json.dumps(dataclasses.asdict(load_capability_matrix())))

    body = _decoded_proxy_body(handler_module.handler(_captured_v1_envelope(), None))

    assert body == expected


def test_the_payload_satisfies_every_field_the_browser_validator_reads() -> None:
    """Every field `parseCapabilityMatrix` dereferences is present with the type it requires."""
    body = _decoded_proxy_body(handler_module.handler(_captured_v1_envelope(), None))

    assert isinstance(body, dict)
    for key in ("default_family", "models", "tiers"):
        assert key in body, f"parseCapabilityMatrix rejects a payload missing {key!r}"
    assert "allow_cross_family_override" not in body, "the key is retired"
    assert body["default_family"] in _FAMILIES

    assert body["models"], "an empty models map would make the per-model assertions verify nothing"
    for model_id, model in body["models"].items():
        assert model["model_id"] == model_id, (
            "parseCapabilityMatrix rejects a key/model_id mismatch"
        )
        assert model["family"] in _FAMILIES
        assert model["tier"] in _TIERS
        effort = model["effort"]
        assert isinstance(effort["supported"], bool)
        assert isinstance(effort["values"], list)
        assert all(isinstance(value, str) for value in effort["values"])
        assert effort["default"] is None or isinstance(effort["default"], str)
        capped = effort["capped_at_when_thinking_disabled"]
        assert capped is None or isinstance(capped, str)
        assert "temperature" not in model, "temperature is removed from the matrix"
        assignable = model["assignable_tiers"]
        assert isinstance(assignable, list)
        assert set(assignable) <= set(_TIERS)
        assert model["tier"] in assignable, f"{model_id} is not assignable to its own home tier"

    assert set(body["tiers"]) == set(_FAMILIES), (
        "the served `tiers` map and the validator's FAMILIES tuple disagree; the browser indexes "
        "this as a total record, so an extra or missing family is a contract break in one direction "
        "or an undefined read in the other"
    )
    for family in _FAMILIES:
        assert set(body["tiers"][family]) == set(_TIERS), (
            f"tiers.{family} does not cover exactly the validator's TIERS tuple"
        )
        for tier in _TIERS:
            assignment = body["tiers"][family][tier]
            assert isinstance(assignment["model_id"], str)
            assert assignment["model_id"] in body["models"], (
                f"tiers.{family}.{tier} names {assignment['model_id']!r}, which is absent from "
                "models; the browser's modelFor() would throw on it"
            )
            for key, value in assignment["tuning"].items():
                assert isinstance(value, str | int | float) and not isinstance(value, bool), (
                    f"tiers.{family}.{tier}.tuning.{key} must be a string or a number"
                )


def test_every_served_tier_assignment_is_within_its_models_assignable_tiers() -> None:
    """Each ``tiers.<family>.<tier>`` names a model whose served ``assignable_tiers`` holds ``tier``."""
    body = _decoded_proxy_body(handler_module.handler(_captured_v1_envelope(), None))

    for family in _FAMILIES:
        for tier in _TIERS:
            model_id = body["tiers"][family][tier]["model_id"]
            assert tier in body["models"][model_id]["assignable_tiers"], (
                f"tiers.{family}.{tier} assigns {model_id!r}, which the served matrix does not "
                f"mark assignable to {tier!r}"
            )


def test_sonnet_is_served_as_assignable_to_supervisor_and_subagent() -> None:
    """The served Sonnet 5.5 entry is the default at two tiers and says so on the wire."""
    body = _decoded_proxy_body(handler_module.handler(_captured_v1_envelope(), None))

    sonnet = body["models"]["anthropic.claude-sonnet-5-5"]
    assert sonnet["tier"] == "subagent"
    assert sonnet["assignable_tiers"] == ["supervisor", "subagent"]
    assert body["tiers"]["anthropic"]["supervisor"]["model_id"] == sonnet["model_id"]
    assert body["tiers"]["anthropic"]["subagent"]["model_id"] == sonnet["model_id"]


#: Retired model ids, from the one constant that names them.
_RETIRED_MODEL_IDS = tuple(
    profile.removeprefix("global.") for profile in RETIRED_INFERENCE_PROFILES
)


def _strings_and_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | {
            item for v in value.values() for item in _strings_and_keys(v)
        }
    if isinstance(value, list):
        return {item for v in value for item in _strings_and_keys(v)}
    return {value} if isinstance(value, str) else set()


def test_the_payload_names_no_temperature_and_no_retired_model() -> None:
    """No key or value in the served matrix mentions temperature or a retired model id."""
    body = _decoded_proxy_body(handler_module.handler(_captured_v1_envelope(), None))
    served = _strings_and_keys(body)

    assert not [item for item in served if "temperature" in item.lower()]
    for retired in _RETIRED_MODEL_IDS:
        assert not [item for item in served if retired in item], retired
    assert "anthropic.claude-haiku-5-5" in body["models"]
    assert "openai.gpt-6.1-sol" in body["models"]


def test_a_successful_read_reports_200() -> None:
    response = handler_module.handler(_captured_v1_envelope(), None)

    assert response["statusCode"] == 200


def test_the_flat_channel_returns_the_flat_shape() -> None:
    """A direct invoke carrying the agent channel gets ``{"statusCode", "body"}``, body undumped."""
    claims = _captured_v1_envelope()["requestContext"]["authorizer"]["claims"]

    response = handler_module.handler(
        {"_agent_identity": {"claims": claims, "principal_type": "user"}}, None
    )

    assert set(response) == {"statusCode", "body"}
    assert response["statusCode"] == 200
    assert response["body"] == dataclasses.asdict(load_capability_matrix())


# -- identity ------------------------------------------------------------------------------------


def test_an_event_with_no_identity_channel_is_refused_as_401() -> None:
    """An event with no identity channel is refused with 401 `authentication_failed`."""
    response = handler_module.handler({}, None)

    assert response["statusCode"] == 401
    assert response["error"]["code"] == "authentication_failed"


def test_a_proxy_request_with_no_authorizer_block_is_a_401_in_the_proxy_shape() -> None:
    """An unauthenticated proxy request gets a 401 rendered in the proxy response shape."""
    event = _captured_v1_envelope()
    del event["requestContext"]["authorizer"]

    response = handler_module.handler(event, None)

    body = _decoded_proxy_body(response)
    assert response["statusCode"] == 401
    assert body == {
        "statusCode": 401,
        "error": {
            "code": "authentication_failed",
            "message": "Authentication is required.",
            "next_step": "stop",
        },
    }


def test_claims_without_a_rep_id_are_refused_rather_than_defaulted() -> None:
    """An authenticated token that cannot be scoped is refused, not given an empty-scope identity."""
    event = _captured_v1_envelope()
    del event["requestContext"]["authorizer"]["claims"]["custom:rep_id"]

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 401
    assert _decoded_proxy_body(response)["error"]["code"] == "authentication_failed"


def test_a_caller_supplied_identity_channel_in_the_body_cannot_establish_identity() -> None:
    """`decode` strips every identity channel out of a proxy body before the handler sees it."""
    event = _captured_v1_envelope()
    del event["requestContext"]["authorizer"]
    event["body"] = json.dumps(
        {"_identity": {"rep_id": "attacker", "principal_type": "user", "source": "gateway"}}
    )

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 401


# -- arguments -----------------------------------------------------------------------------------


def test_an_unrecognised_argument_is_refused_rather_than_ignored() -> None:
    """An unrecognised body argument is refused with a validation error, not ignored."""
    event = _captured_v1_envelope()
    event["body"] = json.dumps({"family": "openai"})

    response = handler_module.handler(event, None)

    body = _decoded_proxy_body(response)
    assert response["statusCode"] == 400
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["source"] == "arguments"
    assert body["error"]["message"] == "This operation accepts no arguments."
    assert "family" not in body["error"]["message"]


def test_an_absent_body_is_not_an_argument_error() -> None:
    """A request with no body is not refused by the no-arguments guard."""
    event = _captured_v1_envelope()
    event.pop("body")

    assert handler_module.handler(event, None)["statusCode"] == 200


def test_an_empty_string_body_is_not_an_argument_error() -> None:
    """API Gateway sends `""` rather than `null` for some empty-body requests."""
    event = _captured_v1_envelope()
    event["body"] = ""

    assert handler_module.handler(event, None)["statusCode"] == 200


def test_a_body_that_is_not_json_is_a_400_and_not_a_crash() -> None:
    event = _captured_v1_envelope()
    event["body"] = "not json at all"

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 400
    assert _decoded_proxy_body(response)["error"]["code"] == "validation_failed"


def test_a_query_string_argument_is_refused_on_the_channel_a_GET_client_uses() -> None:  # noqa: N802
    """A query-string argument on the proxy GET is refused rather than ignored."""
    event = _captured_v1_envelope()
    event["queryStringParameters"] = {"family": "openai"}

    response = handler_module.handler(event, None)

    body = _decoded_proxy_body(response)
    assert response["statusCode"] == 400
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["source"] == "query"
    assert body["error"]["message"] == "This operation accepts no query parameters."
    assert "family" not in body["error"]["message"]


def test_the_multi_value_query_string_is_refused_too() -> None:
    """Both query-string forms are refused, independently of which one is populated."""
    event = _captured_v1_envelope()
    event["multiValueQueryStringParameters"] = {"family": ["openai"]}

    assert handler_module.handler(event, None)["statusCode"] == 400


def test_the_captured_envelopes_own_empty_query_fields_do_not_trip_the_guard() -> None:
    """The captured envelope, with no query string, gets 200."""
    event = _captured_v1_envelope()

    assert "queryStringParameters" in event, (
        "the captured envelope no longer carries a query field, so this control proves nothing"
    )
    assert handler_module.handler(event, None)["statusCode"] == 200


# -- the unavailable path ------------------------------------------------------------------------


def _fail_the_load_with_a_real_missing_file(monkeypatch: pytest.MonkeyPatch, missing: Path) -> None:
    """Point the real loader at a path that does not exist."""
    monkeypatch.setattr(
        capabilities_module,
        "load_capability_matrix",
        lambda **_: load_capability_matrix(capabilities_path=missing),
    )


def test_an_unreadable_matrix_is_a_taxonomy_shaped_500_not_an_unhandled_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing matrix file yields a taxonomy-shaped 500 proxy response."""
    _fail_the_load_with_a_real_missing_file(monkeypatch, tmp_path / "absent.yaml")

    response = handler_module.handler(_captured_v1_envelope(), None)

    body = _decoded_proxy_body(response)
    assert response["statusCode"] == 500
    assert body == {
        "statusCode": 500,
        "error": {
            "code": "internal_error",
            "message": "The model capability matrix could not be read.",
            "next_step": "stop",
        },
    }


def test_a_malformed_matrix_yields_the_same_taxonomy_member_as_an_absent_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-mapping YAML document (``ValueError``) yields the same unavailable response."""
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("- this is a list, not a mapping\n", encoding="utf-8")
    monkeypatch.setattr(
        capabilities_module,
        "load_capability_matrix",
        lambda **_: load_capability_matrix(capabilities_path=malformed),
    )

    response = handler_module.handler(_captured_v1_envelope(), None)

    assert response["statusCode"] == 500
    assert _decoded_proxy_body(response)["error"]["code"] == "internal_error"


def test_the_unavailable_response_carries_no_internal_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unavailable response on the wire does not include the offending path."""
    absent = tmp_path / "secret-looking-path" / "absent.yaml"
    _fail_the_load_with_a_real_missing_file(monkeypatch, absent)

    message = _decoded_proxy_body(handler_module.handler(_captured_v1_envelope(), None))["error"][
        "message"
    ]

    assert "/" not in message
    assert "yaml" not in message.lower()
    assert absent.name not in message
    assert "FileNotFoundError" not in message


def test_a_typed_taxonomy_error_from_the_loader_keeps_its_own_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `CrmError` raised by the loader propagates as its own status, not a 500."""

    def _raise_typed(**_: object) -> None:
        raise ValidationFailed("A tier names an absent model.", source="model_capabilities")

    monkeypatch.setattr(capabilities_module, "load_capability_matrix", _raise_typed)

    response = handler_module.handler(_captured_v1_envelope(), None)

    body = _decoded_proxy_body(response)
    assert response["statusCode"] == 400
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["source"] == "model_capabilities"
    assert body["error"]["message"] == "A tier names an absent model."


def test_a_scope_denial_from_the_loader_keeps_its_missing_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `ScopeDenied` from the loader keeps its `missing_scope` on the wire."""

    def _raise_denied(**_: object) -> None:
        raise ScopeDenied("Not your matrix.", missing_scope="capabilities:read")

    monkeypatch.setattr(capabilities_module, "load_capability_matrix", _raise_denied)

    body = _decoded_proxy_body(handler_module.handler(_captured_v1_envelope(), None))

    assert body["statusCode"] == 403
    assert body["error"]["missing_scope"] == "capabilities:read"


def test_an_import_failure_is_a_taxonomy_shaped_500_not_a_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``ImportError`` of ``providers`` is converted to the unavailable response, not a 502."""
    monkeypatch.delitem(sys.modules, "providers.capabilities", raising=False)
    monkeypatch.setattr(
        builtins,
        "__import__",
        _import_raising_for("providers.capabilities", builtins.__import__),
    )

    response = handler_module.handler(_captured_v1_envelope(), None)

    assert response["statusCode"] == 500
    assert _decoded_proxy_body(response)["error"]["code"] == "internal_error"


def test_a_matrix_that_is_not_a_dataclass_is_a_500_not_a_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dataclasses.asdict` is inside the `try` too: it raises `TypeError` on a non-dataclass."""
    monkeypatch.setattr(capabilities_module, "load_capability_matrix", lambda **_: {"not": "a dc"})

    response = handler_module.handler(_captured_v1_envelope(), None)

    assert response["statusCode"] == 500
    assert _decoded_proxy_body(response)["error"]["code"] == "internal_error"


def test_the_failure_is_logged_before_it_is_converted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The unavailable path logs the underlying exception with its traceback."""
    _fail_the_load_with_a_real_missing_file(monkeypatch, tmp_path / "absent.yaml")

    with caplog.at_level(logging.ERROR, logger=handler_module.__name__):
        handler_module.handler(_captured_v1_envelope(), None)

    records = [r for r in caplog.records if r.name == handler_module.__name__]
    assert len(records) == 1, f"expected exactly one log record, got {len(records)}"
    logged = json.loads(records[0].getMessage())
    assert logged["event"] == "capability_matrix_unavailable"
    assert "FileNotFoundError" in logged["reason"]
    assert records[0].exc_info is not None, (
        "the record carries no traceback, so the conversion still loses what an escaping exception "
        "would have put in CloudWatch"
    )


def test_an_unreadable_matrix_still_refuses_an_unauthenticated_caller_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unauthenticated caller gets 401 even when the matrix cannot be loaded."""
    _fail_the_load_with_a_real_missing_file(monkeypatch, tmp_path / "absent.yaml")

    response = handler_module.handler({}, None)

    assert response["statusCode"] == 401


# -- the non-tool guarantee ----------------------------------------------------------------------


def test_this_route_is_not_published_as_an_mcp_tool() -> None:
    """No `ALL_TOOLS` entry names this handler."""
    from crm.toolspec.definitions import ALL_TOOLS

    assert ALL_TOOLS, "an empty tool list would make this assertion verify nothing"
    handlers = {tool.handler for tool in ALL_TOOLS}
    assert "crm.handlers.capabilities.handler" not in handlers
    assert not any(tool.name == "capabilities" for tool in ALL_TOOLS)


def test_the_handler_declares_no_envelope_predicate_of_its_own() -> None:
    """The handler module discriminates v1/v2 only through `_common.is_proxy_event`.

    The scan covers everything after the module docstring, function docstrings included.
    """
    source = Path(handler_module.__file__ or "").read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]

    assert "is_proxy_event(" in body, (
        "the handler no longer calls is_proxy_event, so the assertions below would pass over a "
        "module that discriminates some other way entirely"
    )
    denied = ("version", "routeKey", "rawPath", "resource", "httpMethod")
    for name in denied:
        for token in (f'"{name}"', f"'{name}'"):
            assert token not in body, (
                f"{token} appears in this handler's code; envelope-shape discrimination must stay "
                "in crm.handlers._common.is_proxy_event, which recognises both payload formats"
            )


# -- the route-method contract, read from both sides ----------------------------------------------


def _client_declared_method() -> str:
    """The HTTP method ``fetchCapabilityMatrix`` sends, read from that function's body."""
    body = re.search(
        rf"export async function {_CLIENT_FETCH_FN}\b(?P<body>.*?)\n\}}",
        _CLIENT_SOURCE,
        re.DOTALL,
    )
    assert body is not None, (
        f"{_CLIENT_FETCH_FN} was not found in {_CLIENT_TS.name}, so the CLIENT side of the route "
        "contract cannot be derived. Failing rather than comparing against nothing."
    )
    assert "/capabilities" in body.group("body"), (
        f"{_CLIENT_FETCH_FN} no longer fetches `/capabilities`; the method read below would be some "
        "other request's"
    )
    declared = re.search(r'method:\s*"(?P<method>\w+)"', body.group("body"))
    assert declared is not None, (
        f"{_CLIENT_FETCH_FN} declares no `method:`, so the CLIENT side of the route contract cannot "
        "be derived. Failing rather than defaulting — a wrong guess here would pass over a real "
        "mismatch."
    )
    return declared.group("method")


def test_the_client_and_the_deployment_agree_on_this_routes_method() -> None:
    """``fetchCapabilityMatrix``'s method equals the method the API root provisions for the route.

    The root derivation is imported from `test_infra_api._root_extra_routes`.
    """
    from tests.unit.test_infra_api import CAPABILITIES_ROUTE_NAME, _root_extra_routes

    served = _root_extra_routes()
    assert CAPABILITIES_ROUTE_NAME in served, (
        f"the root wires no {CAPABILITIES_ROUTE_NAME!r} route, so there is no deployment side to "
        "compare the client against"
    )

    requested = _client_declared_method()
    assert requested == served[CAPABILITIES_ROUTE_NAME], (
        f"{_CLIENT_FETCH_FN} sends {requested} but the root wires {CAPABILITIES_ROUTE_NAME} as "
        f"{served[CAPABILITIES_ROUTE_NAME]}. A method mismatch answers every settings-page fetch "
        "with 403, which the browser cannot distinguish from an authorization failure."
    )
