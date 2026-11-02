# SPDX-License-Identifier: MIT
"""`scripts/migrate_supervisor_effort.py` against DynamoDB Local.

Items are compared as the raw typed attribute maps the engine returns, so "untouched" means
byte-equal and the conditional write is the engine's own map-equality check.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

import pytest
from scripts import migrate_supervisor_effort as mse

from crm.store.keys import user_settings_key
from crm.store.store import Store
from crm.user_settings import SETTINGS_KIND, SETTINGS_SCHEMA_VERSION, SETTINGS_SOURCE
from providers.capabilities import load_capability_matrix

MATRIX = load_capability_matrix()
ACCOUNT = "123456789012"
REGION = "us-east-1"
UPDATED_AT = "2026-09-01T10:00:00.000001+00:00"


def _tuning(**supervisor: object) -> dict[str, object]:
    tiers: dict[str, object] = {
        "tool": {"family": "openai", "model": "openai.gpt-6-luna", "effort": "low"},
    }
    if supervisor:
        tiers["supervisor"] = dict(supervisor)
    return {"schema": 3, "source": SETTINGS_SOURCE, "tiers": tiers}


def _settings_item(rep_id: str, tuning: Mapping[str, object]) -> dict[str, object]:
    return {
        "rep_id": user_settings_key("demo", rep_id),
        "kind": SETTINGS_KIND,
        "settings_tenant": "demo",
        "owner_rep_id": rep_id,
        "tuning": dict(tuning),
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "updated_at": UPDATED_AT,
    }


ANTHROPIC_LOW = _settings_item(
    "rep-a-low", _tuning(family="anthropic", model="anthropic.claude-opus-5-5", effort="low")
)
OPENAI_LOW = _settings_item(
    "rep-o-low", _tuning(family="openai", model="openai.gpt-6-astra", effort="low")
)
ALREADY_MEDIUM = _settings_item(
    "rep-medium", _tuning(family="anthropic", model="anthropic.claude-opus-5-5", effort="medium")
)
NULL_EFFORT = _settings_item(
    "rep-null", _tuning(family="anthropic", model="anthropic.claude-opus-5-5", effort=None)
)
NO_SUPERVISOR = _settings_item("rep-tool-only", _tuning())
OTHER_EFFORT = _settings_item(
    "rep-high", _tuning(family="anthropic", model="anthropic.claude-opus-5-5", effort="high")
)
UNRESOLVABLE = _settings_item(
    "rep-gone", _tuning(family="anthropic", model="anthropic.claude-opus-0-0", effort="low")
)
NOT_SETTINGS_UNDER_PREFIX = {
    **_settings_item(
        "rep-kindless", _tuning(family="anthropic", model="anthropic.claude-opus-5-5", effort="low")
    ),
    "kind": "something_else",
}
#: A plain reps row shaped like a settings item but outside the key prefix.
LOOKALIKE_REP = {
    **ANTHROPIC_LOW,
    "rep_id": "rep-lookalike",
    "tenant": "demo",
    "name": "Lookalike",
    "status": "active",
}

UNTOUCHED = (
    ALREADY_MEDIUM,
    NULL_EFFORT,
    NO_SUPERVISOR,
    OTHER_EFFORT,
    UNRESOLVABLE,
    NOT_SETTINGS_UNDER_PREFIX,
    LOOKALIKE_REP,
)
MIGRATING = (ANTHROPIC_LOW, OPENAI_LOW)


def _raw(client: Any, prefix: str, rep_id: object) -> dict[str, Any]:
    response = client.get_item(TableName=f"{prefix}reps", Key={"rep_id": {"S": str(rep_id)}})
    return dict(response["Item"])


def _snapshot(client: Any, prefix: str) -> dict[str, dict[str, Any]]:
    items = client.scan(TableName=f"{prefix}reps")["Items"]
    return {item["rep_id"]["S"]: item for item in items}


@pytest.fixture
def seeded(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> tuple[Store, str]:
    store, prefix = dynamodb_local_store
    for item in (*MIGRATING, *UNTOUCHED):
        store.put_item("reps", item)
    return store, prefix


def test_both_families_at_low_move_to_medium_and_nothing_else_changes(
    seeded: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = seeded
    before = _snapshot(dynamodb_local_client, prefix)
    summary = mse.migrate(store, matrix=MATRIX, apply=True)

    assert summary.migrated == 2 and summary.eligible == 2 and summary.conflict == 0
    after = _snapshot(dynamodb_local_client, prefix)
    for item in MIGRATING:
        key = str(item["rep_id"])
        expected_tuning = copy.deepcopy(before[key]["tuning"])
        expected_tuning["M"]["tiers"]["M"]["supervisor"]["M"]["effort"] = {"S": "medium"}
        assert after[key]["tuning"] == expected_tuning
        assert {k: v for k, v in after[key].items() if k != "tuning"} == {
            k: v for k, v in before[key].items() if k != "tuning"
        }
        assert after[key]["updated_at"] == {"S": UPDATED_AT}
    for item in UNTOUCHED:
        key = str(item["rep_id"])
        assert after[key] == before[key], key


def test_every_item_under_the_prefix_is_counted_once_and_no_other_is_read(
    seeded: tuple[Store, str],
) -> None:
    store, _prefix = seeded
    summary = mse.migrate(store, matrix=MATRIX, apply=False)
    assert summary.to_report_fields() == {
        "dry_run": True,
        "partial": False,
        "scanned": 8,
        "eligible": 2,
        "migrated": 0,
        "conflict": 0,
        "already_medium": 1,
        "unchanged": 3,
        "skipped_unresolvable": 1,
        "skipped_not_settings": 1,
    }


def test_a_dry_run_writes_nothing(seeded: tuple[Store, str], dynamodb_local_client: Any) -> None:
    store, prefix = seeded
    before = _snapshot(dynamodb_local_client, prefix)
    assert mse.migrate(store, matrix=MATRIX, apply=False).eligible == 2
    assert _snapshot(dynamodb_local_client, prefix) == before


def test_a_second_run_migrates_nothing(
    seeded: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = seeded
    assert mse.migrate(store, matrix=MATRIX, apply=True).migrated == 2
    after_first = _snapshot(dynamodb_local_client, prefix)
    second = mse.migrate(store, matrix=MATRIX, apply=True)
    assert (second.eligible, second.migrated, second.already_medium) == (0, 0, 3)
    assert _snapshot(dynamodb_local_client, prefix) == after_first


class _SaveBetweenReadAndWrite(Store):
    """A store that writes the user's own save at the item just before the migration's update."""

    def __init__(self, *, users_tuning: Mapping[str, object], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._users_tuning = users_tuning

    def update_item(self, table: str, key: Mapping[str, str], **kwargs: Any) -> dict[str, object]:
        if key["rep_id"] == ANTHROPIC_LOW["rep_id"]:
            self.put_item(table, {**ANTHROPIC_LOW, "tuning": dict(self._users_tuning)})
        return super().update_item(table, key, **kwargs)


def test_a_save_between_read_and_write_is_a_conflict_and_the_users_value_is_kept(
    seeded: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    _store, prefix = seeded
    users_tuning = _tuning(family="anthropic", model="anthropic.claude-opus-5-5", effort="high")
    racing = _SaveBetweenReadAndWrite(
        users_tuning=users_tuning, table_prefix=prefix, client=dynamodb_local_client
    )
    summary = mse.migrate(racing, matrix=MATRIX, apply=True)
    assert (summary.eligible, summary.migrated, summary.conflict) == (2, 1, 1)
    raw = _raw(dynamodb_local_client, prefix, ANTHROPIC_LOW["rep_id"])
    assert raw["tuning"]["M"]["tiers"]["M"]["supervisor"]["M"]["effort"] == {"S": "high"}


def test_an_unresolvable_item_is_skipped_and_left_as_saved(
    seeded: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = seeded
    before = _raw(dynamodb_local_client, prefix, UNRESOLVABLE["rep_id"])
    assert mse.migrate(store, matrix=MATRIX, apply=True).skipped_unresolvable == 1
    assert _raw(dynamodb_local_client, prefix, UNRESOLVABLE["rep_id"]) == before


def test_scan_key_prefix_returns_exactly_the_prefixed_items(seeded: tuple[Store, str]) -> None:
    store, _prefix = seeded
    found = {
        str(item["rep_id"]) for item in store.scan_key_prefix("reps", "rep_id", "usersettings#")
    }
    assert found == {str(item["rep_id"]) for item in (*MIGRATING, *UNTOUCHED[:-1])}
    assert str(LOOKALIKE_REP["rep_id"]) not in found


# -- CLI against the engine ---------------------------------------------------------------------


class _LocalSession:
    """A session whose DynamoDB client is the local engine and whose STS reports ``account``."""

    def __init__(self, dynamodb: Any, account: str) -> None:
        self._dynamodb = dynamodb
        self._account = account

    def client(self, service: str) -> Any:
        if service == "dynamodb":
            return self._dynamodb
        if service == "sts":
            account = self._account

            class _Sts:
                def get_caller_identity(self) -> dict[str, str]:
                    return {"Account": account}

            return _Sts()
        raise AssertionError(f"unexpected client {service!r}")


def _outputs(prefix: str, *, account: str = ACCOUNT, region: str = REGION) -> dict[str, object]:
    return {
        "dynamodb_table_names": {"value": {"eval_runs": f"{prefix}eval_runs"}},
        "reps_table_arn": {"value": f"arn:aws:dynamodb:{region}:{account}:table/{prefix}reps"},
    }


def _run_cli(
    tmp_path: Any, client: Any, prefix: str, outputs: object, *extra: str, account: str = ACCOUNT
) -> int:
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(outputs), encoding="utf-8")
    argv = ["--outputs-json", str(path), "--profile", "p", "--region", REGION, *extra]
    return mse.main(
        argv, session_factory=lambda _p, _r: _LocalSession(client, account), matrix=MATRIX
    )


def test_the_cli_applies_against_a_matching_target(
    seeded: tuple[Store, str],
    dynamodb_local_client: Any,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store, prefix = seeded
    assert _run_cli(tmp_path, dynamodb_local_client, prefix, _outputs(prefix), "--apply") == 0
    assert json.loads(capsys.readouterr().out)["migrated"] == 2


@pytest.mark.parametrize(
    ("outputs_kwargs", "session_account"),
    [({"region": "us-west-2"}, ACCOUNT), ({}, "111122223333")],
    ids=["region", "account"],
)
def test_a_target_mismatch_is_refused_with_nothing_read_or_written(
    seeded: tuple[Store, str],
    dynamodb_local_client: Any,
    tmp_path: Any,
    outputs_kwargs: dict[str, str],
    session_account: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store, prefix = seeded
    before = _snapshot(dynamodb_local_client, prefix)
    code = _run_cli(
        tmp_path,
        dynamodb_local_client,
        prefix,
        _outputs(prefix, **outputs_kwargs),
        "--apply",
        account=session_account,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Nothing has been written" in captured.err
    assert _snapshot(dynamodb_local_client, prefix) == before


# -- stored shapes the plain encoder never writes ------------------------------------------------


def _raw_settings_item(rep_id: str, supervisor: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "rep_id": {"S": user_settings_key("demo", rep_id)},
        "kind": {"S": SETTINGS_KIND},
        "updated_at": {"S": UPDATED_AT},
        "tuning": {
            "M": {
                "schema": {"N": "2"},
                "source": {"S": SETTINGS_SOURCE},
                "tiers": {"M": {"supervisor": {"M": supervisor}}},
            }
        },
        **extra,
    }


_LOW_OPUS = {
    "family": {"S": "anthropic"},
    "model": {"S": "anthropic.claude-opus-5-5"},
    "effort": {"S": "low"},
}


def test_a_high_precision_number_survives_the_conditional_write(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = dynamodb_local_store
    precise = "0.123456789012345678901234567"
    item = _raw_settings_item("rep-precise", {**_LOW_OPUS, "temperature": {"N": precise}})
    dynamodb_local_client.put_item(TableName=f"{prefix}reps", Item=item)

    summary = mse.migrate(store, matrix=MATRIX, apply=True)
    assert (summary.migrated, summary.conflict) == (1, 0)
    supervisor = _raw(dynamodb_local_client, prefix, item["rep_id"]["S"])["tuning"]["M"]["tiers"][
        "M"
    ]["supervisor"]["M"]
    assert supervisor["effort"] == {"S": "medium"}
    assert supervisor["temperature"] == {"N": precise}


def test_set_and_binary_attributes_are_counted_and_left_as_stored(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = dynamodb_local_store
    set_in_tuning = _raw_settings_item("rep-set-tuning", {**_LOW_OPUS, "tags": {"SS": ["a", "b"]}})
    set_beside_tuning = _raw_settings_item(
        "rep-set-beside", _LOW_OPUS, labels={"SS": ["x"]}, blob={"B": b"raw"}
    )
    for item in (set_in_tuning, set_beside_tuning):
        dynamodb_local_client.put_item(TableName=f"{prefix}reps", Item=item)

    before = {
        item["rep_id"]["S"]: _raw(dynamodb_local_client, prefix, item["rep_id"]["S"])
        for item in (set_in_tuning, set_beside_tuning)
    }

    summary = mse.migrate(store, matrix=MATRIX, apply=True)
    assert (summary.scanned, summary.skipped_not_settings, summary.migrated) == (2, 2, 0)
    assert summary.partial is False
    for key, raw in before.items():
        assert _raw(dynamodb_local_client, prefix, key) == raw


# -- every tenant's saved settings move; nothing crosses a tenant -------------------------

#: The same rep id under two tenants, with different non-supervisor values per tenant.
_OTHER_TENANT = "acme"
_SHARED_REP = "rep-shared"


def _tenant_settings_item(tenant: str, tool_effort: str) -> dict[str, object]:
    tuning = _tuning(family="openai", model="openai.gpt-6-astra", effort="low")
    tiers = tuning["tiers"]
    assert isinstance(tiers, dict)
    tiers["tool"] = {**tiers["tool"], "effort": tool_effort}
    return {
        **_settings_item(_SHARED_REP, tuning),
        "rep_id": user_settings_key(tenant, _SHARED_REP),
        "settings_tenant": tenant,
    }


def test_every_tenants_settings_are_migrated_in_place_and_no_other_item_is_written(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = dynamodb_local_store
    settings = {
        "demo": _tenant_settings_item("demo", "low"),
        _OTHER_TENANT: _tenant_settings_item(_OTHER_TENANT, "medium"),
    }
    for item in settings.values():
        store.put_item("reps", item)
    for tenant in settings:
        store.put_item(
            "reps",
            {
                "rep_id": f"{tenant}-{_SHARED_REP}",
                "tenant": tenant,
                "name": tenant,
                "status": "active",
            },
        )
    before = _snapshot(dynamodb_local_client, prefix)
    settings_keys = {str(item["rep_id"]) for item in settings.values()}

    summary = mse.migrate(store, matrix=MATRIX, apply=True)

    assert (summary.scanned, summary.migrated, summary.conflict) == (2, 2, 0)
    after = _snapshot(dynamodb_local_client, prefix)
    assert set(after) == set(before)
    for tenant, item in settings.items():
        key = str(item["rep_id"])
        expected = copy.deepcopy(before[key])
        expected["tuning"]["M"]["tiers"]["M"]["supervisor"]["M"]["effort"] = {"S": "medium"}
        assert after[key] == expected, tenant
        assert after[key]["settings_tenant"] == {"S": tenant}
    assert after[user_settings_key(_OTHER_TENANT, _SHARED_REP)]["tuning"]["M"]["tiers"]["M"][
        "tool"
    ]["M"]["effort"] == {"S": "medium"}
    for key in set(before) - settings_keys:
        assert after[key] == before[key], key
