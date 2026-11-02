# SPDX-License-Identifier: MIT
"""Per-user settings against DynamoDB Local: the item is sparse and strictly per user.

The real engine's `reps.gsi_tenant` must never list a settings item, and
`delete_tenant` must leave it untouched. A row written at settings schema 1 reads without its
temperatures and is replaced, temperature-free, by the next save.
"""

from __future__ import annotations

from crm.authz import Identity
from crm.envelope import decode_identity
from crm.store.store import Store
from crm.user_settings import (
    SETTINGS_KIND,
    SETTINGS_SCHEMA_VERSION,
    SETTINGS_SOURCE,
    SETTINGS_TABLE,
    get_settings,
    reset_settings,
    save_settings,
)
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import WIRE_SCHEMA_VERSION, TierRequest, TurnTuning

MATRIX = load_capability_matrix()

_TUNING = TurnTuning(
    source="ui",
    tiers={"tool": TierRequest(family="openai", model="openai.gpt-6-luna", effort="low")},
)


def _user(rep_id: str, tenant: str = "demo") -> Identity:
    return decode_identity(
        {
            "requestContext": {
                "requestId": "req-1",
                "authorizer": {
                    "claims": {"custom:rep_id": rep_id, "custom:tenant": tenant},
                },
            }
        }
    )


def _gsi_tenant_rep_ids(store: Store, tenant: str) -> list[object]:
    items, _ = store.query_gsi("reps", "gsi_tenant", partition_value=tenant)
    return [item["rep_id"] for item in items]


def test_settings_item_is_absent_from_gsi_tenant(dynamodb_local_store: tuple[Store, str]) -> None:
    """The engine's sparse index lists the rep, never the rep's settings item."""
    store, _ = dynamodb_local_store
    store.put_item("reps", {"rep_id": "rep-a", "tenant": "demo", "created_at": "2026-10-07"})
    save_settings(store, _user("rep-a"), _TUNING, matrix=MATRIX)
    assert store.get_item(SETTINGS_TABLE, {"rep_id": "usersettings#demo#rep-a"}) is not None
    assert _gsi_tenant_rep_ids(store, "demo") == ["rep-a"]


def test_user_a_cannot_read_user_b_settings(dynamodb_local_store: tuple[Store, str]) -> None:
    """B's saved tuning never reaches A; A's reset leaves B's item in place."""
    store, _ = dynamodb_local_store
    save_settings(store, _user("rep-b"), _TUNING, matrix=MATRIX)
    save_settings(store, _user("rep-a"), TurnTuning(source="ui", tiers={}), matrix=MATRIX)
    a_view = get_settings(store, _user("rep-a"), matrix=MATRIX)
    assert a_view.tuning["tiers"] == {}
    reset_settings(store, _user("rep-a"), matrix=MATRIX)
    assert get_settings(store, _user("rep-a"), matrix=MATRIX).is_default is True
    b_view = get_settings(store, _user("rep-b"), matrix=MATRIX)
    assert b_view.is_default is False
    assert b_view.tuning["tiers"]["tool"]["model"] == "openai.gpt-6-luna"  # type: ignore[index]


def test_same_rep_id_in_another_tenant_has_its_own_item(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """The tenant is part of the key, so a rep id reused across tenants never collides."""
    store, _ = dynamodb_local_store
    save_settings(store, _user("rep-a", "eval-run1"), _TUNING, matrix=MATRIX)
    assert get_settings(store, _user("rep-a", "demo"), matrix=MATRIX).is_default is True
    assert get_settings(store, _user("rep-a", "eval-run1"), matrix=MATRIX).is_default is False


def test_delete_tenant_does_not_see_settings_items(dynamodb_local_store: tuple[Store, str]) -> None:
    """Tenant teardown enumerates through `gsi_tenant`, so it leaves the settings item alone."""
    store, _ = dynamodb_local_store
    store.put_item("reps", {"rep_id": "rep-e", "tenant": "eval-run2", "created_at": "2026-10-07"})
    save_settings(store, _user("rep-e", "eval-run2"), _TUNING, matrix=MATRIX)
    store.delete_tenant("eval-run2")
    assert store.get_item("reps", {"rep_id": "rep-e"}) is None
    assert store.get_item(SETTINGS_TABLE, {"rep_id": "usersettings#eval-run2#rep-e"}) is not None


def _keys_anywhere(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | {k for v in value.values() for k in _keys_anywhere(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys_anywhere(v)}
    return set()


def test_a_v1_row_reads_leniently_and_the_next_save_drops_temperature(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """Against the real engine: no write on read; the next save is v2, schema 3, no temperature."""
    store, _ = dynamodb_local_store
    key = {"rep_id": "usersettings#demo#rep-v1"}
    store.put_item(
        SETTINGS_TABLE,
        {
            **key,
            "kind": SETTINGS_KIND,
            "owner_rep_id": "rep-v1",
            "settings_tenant": "demo",
            "tuning": {
                "schema": 2,
                "source": SETTINGS_SOURCE,
                "tiers": {
                    "tool": {
                        "family": "openai",
                        "model": "openai.gpt-6-luna",
                        "effort": "low",
                        "temperature": 0.4,
                    }
                },
            },
            "schema_version": 1,
            "updated_at": "2026-10-01T00:00:00+00:00",
        },
    )
    before = store.get_item(SETTINGS_TABLE, key, consistent=True)

    view = get_settings(store, _user("rep-v1"), matrix=MATRIX)

    assert view.is_default is False and view.notice is None
    assert "temperature" not in _keys_anywhere(view.to_payload())
    assert store.get_item(SETTINGS_TABLE, key, consistent=True) == before

    save_settings(store, _user("rep-v1"), _TUNING, matrix=MATRIX)

    after = store.get_item(SETTINGS_TABLE, key, consistent=True)
    assert after is not None
    assert after["schema_version"] == SETTINGS_SCHEMA_VERSION
    assert after["tuning"]["schema"] == WIRE_SCHEMA_VERSION  # type: ignore[index]
    assert "temperature" not in _keys_anywhere(after)
