# SPDX-License-Identifier: MIT
"""Per-user tuning settings: own-item keying, save validation, service principals, stale saves.

Runs against a `moto`-mocked DynamoDB client; handler cases decode
identities from real channel events (the recorded REST v1 fixture and a Gateway `_identity`).
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from scripts.smoke.runtime_bedrock_iam import RETIRED_INFERENCE_PROFILES

from crm.authz import Identity
from crm.envelope import CLAIM_REP_ID, CLAIM_TENANT, IDENTITY_KEY, decode_identity
from crm.errors import ScopeDenied, ValidationFailed
from crm.handlers import user_settings as handler_module
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.user_settings import (
    NOTICE_SAVED_MODEL_UNAVAILABLE,
    NOTICE_SAVED_SETTINGS_UNREADABLE,
    SETTINGS_KIND,
    SETTINGS_SCHEMA_VERSION,
    SETTINGS_SOURCE,
    SETTINGS_TABLE,
    get_settings,
    reset_settings,
    save_settings,
)
from providers.capabilities import CapabilityMatrix, Family, load_capability_matrix
from providers.tier_tuning import WIRE_SCHEMA_VERSION, TierRequest, TurnTuning, defaults, to_wire

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

MATRIX = load_capability_matrix()

_REST_V1_EVENT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "events"
    / "apigw_v1_proxy_post_get_account.json"
)


def _unsupported_tool_effort(matrix: CapabilityMatrix) -> tuple[Family, str]:
    """A family and an effort rung some model accepts that the family's tool-tier model does not.

    The default family is tried first.

    Raises:
        AssertionError: Every family's tool-tier model accepts every rung in the matrix.
    """
    scale = {value for model in matrix.models.values() for value in model.effort.values}
    others = sorted(family for family in matrix.tiers if family != matrix.default_family)
    for family in (matrix.default_family, *others):
        missing = scale - set(matrix.model_for(family, "tool").effort.values)
        if missing:
            return family, sorted(missing)[0]
    raise AssertionError("every tool-tier model accepts every effort rung in the matrix")


_UNSUPPORTED_TOOL_FAMILY, _UNSUPPORTED_TOOL_EFFORT = _unsupported_tool_effort(MATRIX)

_OPENAI_TOOL_TUNING = TurnTuning(
    source="ui",
    tiers={"tool": TierRequest(family="openai", model="openai.gpt-6-luna", effort="low")},
)


@pytest.fixture
def store() -> Iterator[Store]:
    """A Store backed by a moto-mocked client, with every declared table created."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _api_event(
    body: dict[str, Any] | str, *, rep_id: str = "rep-a", tenant: str = "demo"
) -> dict[str, Any]:
    """The recorded REST v1 proxy event, retargeted to ``/user_settings`` for one caller."""
    event: dict[str, Any] = json.loads(_REST_V1_EVENT.read_text(encoding="utf-8"))
    event["resource"] = event["path"] = "/user_settings"
    claims = event["requestContext"]["authorizer"]["claims"]
    claims[CLAIM_REP_ID] = rep_id
    claims[CLAIM_TENANT] = tenant
    event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def _service_event(body: dict[str, Any]) -> dict[str, Any]:
    """A Gateway-channel event for a service principal, with the action arguments flat."""
    return {
        **body,
        IDENTITY_KEY: {
            "rep_id": "svc-1",
            "book": ["A-1"],
            "principal_type": "service",
            "source": "gateway",
            "tenant": "demo",
        },
    }


def _call(store: Store, event: dict[str, Any], matrix: CapabilityMatrix = MATRIX) -> Any:
    return handler_module.handler(event, store=store, matrix=matrix)


def _proxy_body(response: dict[str, Any]) -> Any:
    assert isinstance(response["body"], str)
    return json.loads(response["body"])


def _user(rep_id: str = "rep-a", tenant: str = "demo") -> Identity:
    return decode_identity(_api_event({"action": "get"}, rep_id=rep_id, tenant=tenant))


def _without_model(matrix: CapabilityMatrix, model_id: str) -> CapabilityMatrix:
    return dataclasses.replace(
        matrix, models={k: v for k, v in matrix.models.items() if k != model_id}
    )


# --------------------------------------------------------------------------------------
# Key and item shape
# --------------------------------------------------------------------------------------


def test_save_writes_the_item_at_the_identity_derived_key_with_no_tenant_attribute(
    store: Store,
) -> None:
    """The key comes from the identity's tenant and rep id; the item omits `tenant`."""
    identity = _user("rep-a", "demo")
    save_settings(store, identity, _OPENAI_TOOL_TUNING, matrix=MATRIX)
    item = store.get_item(SETTINGS_TABLE, {"rep_id": "usersettings#demo#rep-a"})
    assert item is not None
    assert keys.user_settings_key("demo", "rep-a") == "usersettings#demo#rep-a"
    assert "tenant" not in item
    assert item["kind"] == SETTINGS_KIND
    assert item["owner_rep_id"] == "rep-a"
    assert item["settings_tenant"] == "demo"
    assert item["schema_version"] == SETTINGS_SCHEMA_VERSION == 2
    assert item["tuning"]["schema"] == WIRE_SCHEMA_VERSION == 3  # type: ignore[index]
    assert item["tuning"]["source"] == SETTINGS_SOURCE  # type: ignore[index]


def test_saved_tuning_round_trips_through_get(store: Store) -> None:
    """A saved tuning is returned as the wire block, not the defaults."""
    identity = _user()
    saved = save_settings(store, identity, _OPENAI_TOOL_TUNING, matrix=MATRIX)
    view = get_settings(store, identity, matrix=MATRIX)
    assert view.is_default is False
    assert view.notice is None
    assert view.tuning == saved.tuning
    assert view.tuning["tiers"] == {
        "tool": {
            "family": "openai",
            "model": "openai.gpt-6-luna",
            "effort": "low",
        }
    }
    assert view.updated_at == saved.updated_at and view.updated_at is not None


def test_get_with_no_saved_item_returns_the_defaults(store: Store) -> None:
    """No item → the `config/models.yaml` defaults, flagged as such."""
    view = get_settings(store, _user(), matrix=MATRIX)
    assert view.is_default is True
    assert view.tuning == to_wire(defaults(MATRIX))
    assert view.notice is None and view.updated_at is None


def test_user_a_cannot_read_user_b_settings(store: Store) -> None:
    """B's save is invisible to A, in the same tenant and with A's rep id in another tenant."""
    save_settings(store, _user("rep-b"), _OPENAI_TOOL_TUNING, matrix=MATRIX)
    assert get_settings(store, _user("rep-a"), matrix=MATRIX).is_default is True
    save_settings(store, _user("rep-a", "eval-x"), _OPENAI_TOOL_TUNING, matrix=MATRIX)
    assert get_settings(store, _user("rep-a", "demo"), matrix=MATRIX).is_default is True


def test_reset_deletes_only_the_callers_item(store: Store) -> None:
    """Reset restores the defaults for the caller and leaves another user's save in place."""
    save_settings(store, _user("rep-a"), _OPENAI_TOOL_TUNING, matrix=MATRIX)
    save_settings(store, _user("rep-b"), _OPENAI_TOOL_TUNING, matrix=MATRIX)
    view = reset_settings(store, _user("rep-a"), matrix=MATRIX)
    assert view.is_default is True and view.tuning == to_wire(defaults(MATRIX))
    assert store.get_item(SETTINGS_TABLE, {"rep_id": "usersettings#demo#rep-a"}) is None
    assert get_settings(store, _user("rep-b"), matrix=MATRIX).is_default is False


# --------------------------------------------------------------------------------------
# Save validation through resolve_turn
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_", "source"),
    [
        (TierRequest(family="anthropic", model="openai.gpt-6-luna"), "tuning.tiers.tool.model"),
        (TierRequest(model="no-such-model"), "tuning.tiers.tool.model"),
        (
            TierRequest(family=_UNSUPPORTED_TOOL_FAMILY, effort=_UNSUPPORTED_TOOL_EFFORT),
            "tuning.tiers.tool.effort",
        ),
    ],
)
def test_save_rejects_tuning_that_does_not_resolve_and_writes_nothing(
    store: Store, request_: TierRequest, source: str
) -> None:
    """Every `resolve_turn` rejection refuses the save with its source path; no item is written."""
    tuning = TurnTuning(source="ui", tiers={"tool": request_})
    with pytest.raises(ValidationFailed) as excinfo:
        save_settings(store, _user(), tuning, matrix=MATRIX)
    assert excinfo.value.source == source
    assert store.get_item(SETTINGS_TABLE, {"rep_id": "usersettings#demo#rep-a"}) is None


# --------------------------------------------------------------------------------------
# Service principals
# --------------------------------------------------------------------------------------


def test_service_principal_save_and_reset_are_denied(store: Store) -> None:
    """A service principal has no settings item to write or delete."""
    service = decode_identity(_service_event({}))
    assert service.principal_type == "service"
    with pytest.raises(ScopeDenied) as denied:
        save_settings(store, service, _OPENAI_TOOL_TUNING, matrix=MATRIX)
    assert denied.value.missing_scope == "user_settings"
    with pytest.raises(ScopeDenied):
        reset_settings(store, service, matrix=MATRIX)
    assert store.get_item(SETTINGS_TABLE, {"rep_id": "usersettings#demo#svc-1"}) is None


def test_service_principal_get_returns_defaults_even_if_an_item_exists(store: Store) -> None:
    """A service read never consults the store."""
    store.put_item(
        SETTINGS_TABLE,
        {
            "rep_id": "usersettings#demo#svc-1",
            "kind": SETTINGS_KIND,
            "owner_rep_id": "svc-1",
            "settings_tenant": "demo",
            "tuning": to_wire(_OPENAI_TOOL_TUNING),
        },
    )
    view = get_settings(store, decode_identity(_service_event({})), matrix=MATRIX)
    assert view.is_default is True and view.tuning == to_wire(defaults(MATRIX))


# --------------------------------------------------------------------------------------
# A saved model later removed from the matrix
# --------------------------------------------------------------------------------------


def test_removed_model_returns_defaults_with_a_notice(store: Store) -> None:
    """A save whose model left the matrix reads back as the defaults plus the notice."""
    identity = _user()
    save_settings(store, identity, _OPENAI_TOOL_TUNING, matrix=MATRIX)
    shrunk = _without_model(MATRIX, "openai.gpt-6-luna")
    view = get_settings(store, identity, matrix=shrunk)
    assert view.is_default is True
    assert view.notice == NOTICE_SAVED_MODEL_UNAVAILABLE
    assert view.tuning == to_wire(defaults(shrunk))
    assert get_settings(store, identity, matrix=MATRIX).notice is None


def test_removed_model_is_logged(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    identity = _user()
    save_settings(store, identity, _OPENAI_TOOL_TUNING, matrix=MATRIX)
    with caplog.at_level("WARNING", logger="crm.user_settings"):
        get_settings(store, identity, matrix=_without_model(MATRIX, "openai.gpt-6-luna"))
    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert records == [
        {
            "event": "user_settings_replaced_by_defaults",
            "reason": NOTICE_SAVED_MODEL_UNAVAILABLE,
            "rep_id": "rep-a",
        }
    ]


@pytest.mark.parametrize(
    "stored_tuning",
    ["not-an-object", {"schema": 1, "family": "openai"}, {"schema": 2, "tiers": {"bogus": {}}}],
    ids=["not-object", "flat-schema-1", "unknown-tier"],
)
def test_unparseable_saved_tuning_gets_its_own_notice_and_is_logged(
    store: Store, caplog: pytest.LogCaptureFixture, stored_tuning: object
) -> None:
    """A corrupt block is distinguished from a removed model."""
    store.put_item(
        SETTINGS_TABLE,
        {
            "rep_id": "usersettings#demo#rep-a",
            "kind": SETTINGS_KIND,
            "owner_rep_id": "rep-a",
            "settings_tenant": "demo",
            "tuning": stored_tuning,
        },
    )
    with caplog.at_level("WARNING", logger="crm.user_settings"):
        view = get_settings(store, _user(), matrix=MATRIX)
    assert view.is_default is True
    assert view.notice == NOTICE_SAVED_SETTINGS_UNREADABLE
    assert NOTICE_SAVED_SETTINGS_UNREADABLE != NOTICE_SAVED_MODEL_UNAVAILABLE
    assert [json.loads(r.getMessage())["reason"] for r in caplog.records] == [
        NOTICE_SAVED_SETTINGS_UNREADABLE
    ]


def test_an_item_at_the_callers_key_owned_by_someone_else_is_refused(store: Store) -> None:
    """An item whose owner fields disagree with the key is never returned."""
    store.put_item(
        SETTINGS_TABLE,
        {
            "rep_id": "usersettings#demo#rep-a",
            "kind": SETTINGS_KIND,
            "owner_rep_id": "rep-b",
            "settings_tenant": "demo",
            "tuning": to_wire(_OPENAI_TOOL_TUNING),
        },
    )
    response = _call(store, _api_event({"action": "get"}))
    assert response["statusCode"] == 500
    assert "rep-b" not in response["body"]


# --------------------------------------------------------------------------------------
# Handler: arguments schema, actions, proxy contract
# --------------------------------------------------------------------------------------


def test_arguments_schema_is_closed_and_has_no_rep_id() -> None:
    """The item is always the caller's; the schema cannot name another rep."""
    schema = handler_module.ARGUMENTS_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"action", "tuning"}
    assert "rep_id" not in schema["properties"]


def test_put_then_get_over_the_api_returns_the_saved_tuning(store: Store) -> None:
    """The proxy response is 200 JSON with a string body, and get reads back the put."""
    put = _call(store, _api_event({"action": "put", "tuning": to_wire(_OPENAI_TOOL_TUNING)}))
    assert put["statusCode"] == 200
    assert put["headers"]["content-type"] == "application/json"
    assert put["isBase64Encoded"] is False
    saved = _proxy_body(put)
    assert saved["is_default"] is False and saved["notice"] is None
    got = _proxy_body(_call(store, _api_event({"action": "get"})))
    assert got == saved
    assert set(got) == {"tuning", "is_default", "notice", "updated_at"}


def test_reset_over_the_api_returns_defaults(store: Store) -> None:
    _call(store, _api_event({"action": "put", "tuning": to_wire(_OPENAI_TOOL_TUNING)}))
    reset = _proxy_body(_call(store, _api_event({"action": "reset"})))
    assert reset["is_default"] is True
    assert reset["tuning"] == json.loads(json.dumps(to_wire(defaults(MATRIX))))


@pytest.mark.parametrize(
    "body",
    [
        {"action": "get", "rep_id": "rep-b"},
        {"action": "put", "rep_id": "rep-b", "tuning": to_wire(_OPENAI_TOOL_TUNING)},
        {"action": "get", "extra": 1},
    ],
)
def test_a_request_naming_another_argument_is_400_and_reads_nothing(
    store: Store, body: dict[str, Any]
) -> None:
    """`rep_id` (or any unlisted key) is refused before the store is touched."""
    save_settings(store, _user("rep-b"), _OPENAI_TOOL_TUNING, matrix=MATRIX)
    response = _call(store, _api_event(body))
    assert response["statusCode"] == 400
    payload = _proxy_body(response)
    assert payload["error"]["code"] == "validation_failed"
    assert "openai.gpt-6-luna" not in response["body"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"action": "delete"},
        {"action": 1},
        {"action": "put"},
        {"action": "get", "tuning": {"schema": WIRE_SCHEMA_VERSION}},
        {"action": "reset", "tuning": {"schema": WIRE_SCHEMA_VERSION}},
        {"action": "put", "tuning": "not-an-object"},
        {"action": "put", "tuning": {"family": "openai", "effort": "low"}},
    ],
)
def test_malformed_requests_are_400(store: Store, body: dict[str, Any]) -> None:
    """Unknown actions, a missing or misplaced tuning block, and a flat schema-1 block are 400."""
    response = _call(store, _api_event(body))
    assert response["statusCode"] == 400
    assert _proxy_body(response)["error"]["code"] == "validation_failed"
    assert store.get_item(SETTINGS_TABLE, {"rep_id": "usersettings#demo#rep-a"}) is None


def test_put_with_unsupported_effort_is_400_naming_the_field(store: Store) -> None:
    """A tuning value the model does not accept is rejected at save, not at turn time."""
    tuning = {
        "schema": WIRE_SCHEMA_VERSION,
        "tiers": {"tool": {"family": _UNSUPPORTED_TOOL_FAMILY, "effort": _UNSUPPORTED_TOOL_EFFORT}},
    }
    response = _call(store, _api_event({"action": "put", "tuning": tuning}))
    assert response["statusCode"] == 400
    assert _proxy_body(response)["error"]["source"] == "tuning.tiers.tool.effort"


def test_service_principal_put_over_the_handler_is_403(store: Store) -> None:
    response = _call(
        store, _service_event({"action": "put", "tuning": to_wire(_OPENAI_TOOL_TUNING)})
    )
    assert response["statusCode"] == 403
    assert response["error"]["missing_scope"] == "user_settings"


def test_unauthenticated_request_is_401(store: Store) -> None:
    """A proxy event with no authorizer claims is refused."""
    event = _api_event({"action": "get"})
    unauthenticated = copy.deepcopy(event)
    del unauthenticated["requestContext"]["authorizer"]
    response = _call(store, unauthenticated)
    assert response["statusCode"] == 401
    assert isinstance(response["body"], str)


def test_unreadable_matrix_is_internal_error(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """A capability-matrix load failure is a 500 `internal_error`, not an unhandled exception."""

    def _broken() -> CapabilityMatrix:
        raise ValueError("models.yaml unreadable")

    monkeypatch.setattr(handler_module, "load_capability_matrix", _broken)
    response = handler_module.handler(_api_event({"action": "get"}), store=store)
    assert response["statusCode"] == 500
    assert "models.yaml" not in response["body"]


# --------------------------------------------------------------------------------------
# Rows written at settings schema 1 (wire tuning schema 2, with temperatures)
# --------------------------------------------------------------------------------------

_V1_KEY = {"rep_id": "usersettings#demo#rep-a"}


def _put_v1_row(store: Store, tiers: dict[str, dict[str, object]]) -> dict[str, object]:
    """Write a settings item as settings schema 1 stored it, and return it as read back."""
    store.put_item(
        SETTINGS_TABLE,
        {
            **_V1_KEY,
            "kind": SETTINGS_KIND,
            "owner_rep_id": "rep-a",
            "settings_tenant": "demo",
            "tuning": {"schema": 2, "source": SETTINGS_SOURCE, "tiers": tiers},
            "schema_version": 1,
            "updated_at": "2026-10-01T00:00:00+00:00",
        },
    )
    item = store.get_item(SETTINGS_TABLE, _V1_KEY)
    assert item is not None
    return item


def _keys_anywhere(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | {k for v in value.values() for k in _keys_anywhere(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys_anywhere(v)}
    return set()


def test_a_v1_row_reads_without_its_temperatures_and_is_not_rewritten(store: Store) -> None:
    """The lenient reader drops `temperature`; the read leaves the stored item as it was."""
    before = _put_v1_row(
        store,
        {
            "tool": {
                "family": "openai",
                "model": "openai.gpt-6-luna",
                "effort": "low",
                "temperature": 0.4,
            },
            "supervisor": {"temperature": None},
        },
    )
    view = get_settings(store, _user(), matrix=MATRIX)
    assert view.is_default is False and view.notice is None
    assert view.tuning["schema"] == WIRE_SCHEMA_VERSION
    assert view.tuning["tiers"] == {
        "tool": {"family": "openai", "model": "openai.gpt-6-luna", "effort": "low"},
        "supervisor": {"family": None, "model": None, "effort": None},
    }
    assert "temperature" not in _keys_anywhere(view.to_payload())
    assert store.get_item(SETTINGS_TABLE, _V1_KEY) == before


def test_the_next_save_over_a_v1_row_stores_no_temperature_anywhere(store: Store) -> None:
    _put_v1_row(store, {"tool": {"effort": "low", "temperature": 0.4}})
    save_settings(store, _user(), _OPENAI_TOOL_TUNING, matrix=MATRIX)
    item = store.get_item(SETTINGS_TABLE, _V1_KEY)
    assert item is not None
    assert item["schema_version"] == SETTINGS_SCHEMA_VERSION
    assert item["tuning"]["schema"] == WIRE_SCHEMA_VERSION  # type: ignore[index]
    assert "temperature" not in _keys_anywhere(item)


def test_a_v1_row_naming_a_retired_model_returns_defaults_and_stays_untouched(
    store: Store,
) -> None:
    """A saved model no longer in the matrix → defaults plus the notice, row kept."""
    retired = next(
        profile.removeprefix("global.")
        for profile in RETIRED_INFERENCE_PROFILES
        if profile.startswith("global.anthropic.")
    )
    assert retired not in MATRIX.models
    before = _put_v1_row(
        store, {"tool": {"family": "anthropic", "model": retired, "temperature": 0.2}}
    )
    view = get_settings(store, _user(), matrix=MATRIX)
    assert view.is_default is True
    assert view.notice == NOTICE_SAVED_MODEL_UNAVAILABLE
    assert view.tuning == to_wire(defaults(MATRIX))
    assert store.get_item(SETTINGS_TABLE, _V1_KEY) == before


@pytest.mark.parametrize("temperature", [0.7, None], ids=["number", "null"])
def test_put_with_a_temperature_is_400_naming_it_and_writes_nothing(
    store: Store, temperature: float | None
) -> None:
    """The save path is strict: a temperature key, even `null`, is refused at its tier."""
    for schema in (2, WIRE_SCHEMA_VERSION):
        tuning = {
            "schema": schema,
            "tiers": {"tool": {"effort": "low", "temperature": temperature}},
        }
        response = _call(store, _api_event({"action": "put", "tuning": tuning}))
        assert response["statusCode"] == 400
        error = _proxy_body(response)["error"]
        assert error["code"] == "validation_failed"
        assert error["source"] == "tuning.tiers.tool.temperature"
    assert store.get_item(SETTINGS_TABLE, _V1_KEY) is None


def test_put_of_a_schema_2_block_without_temperature_is_400_on_the_schema(store: Store) -> None:
    tuning = {"schema": 2, "tiers": {"tool": {"effort": "low"}}}
    response = _call(store, _api_event({"action": "put", "tuning": tuning}))
    assert response["statusCode"] == 400
    assert _proxy_body(response)["error"]["source"] == "tuning.schema"
    assert store.get_item(SETTINGS_TABLE, _V1_KEY) is None
