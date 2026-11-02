# SPDX-License-Identifier: MIT
"""Two identical stub sweeps write byte-identical artifacts except for ``timestamp``.

Covers every field the split harness now publishes, ``tier_summary`` included, at zero cost and
with no credential.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import run_sweep

_RUN_ID = "reproducible01"

#: An ISO-8601 UTC timestamp as `datetime.now(UTC).isoformat()` renders it.
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No seeding, moto's dummy credentials, and no ambient profile."""

    def _noop_seeder(store: Store, *, tenant: str) -> None:
        del store, tenant

    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _noop_seeder)
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _sweep(store: Store, output_dir: Path) -> dict[str, bytes]:
    run_sweep(store=store, output_dir=output_dir, run_id=_RUN_ID)
    return {
        name: (output_dir / name).read_bytes()
        for name in ("matrix.json", "matrix.md", "matrix.csv")
    }


def _without_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("<ts>" if k == "timestamp" else _without_timestamps(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


def test_two_stub_sweeps_differ_only_in_timestamp(store: Store, tmp_path: Path) -> None:
    first = _sweep(store, tmp_path / "a")
    second = _sweep(store, tmp_path / "b")

    first_json = json.loads(first["matrix.json"])
    second_json = json.loads(second["matrix.json"])
    assert _without_timestamps(first_json) == _without_timestamps(second_json)
    for name in ("matrix.md", "matrix.csv"):
        assert _ISO_TIMESTAMP.sub("<ts>", first[name].decode()) == _ISO_TIMESTAMP.sub(
            "<ts>", second[name].decode()
        ), name


def test_the_reproducible_artifacts_carry_the_tier_summary(store: Store, tmp_path: Path) -> None:
    arms = json.loads(_sweep(store, tmp_path)["matrix.json"])["arms"]
    assert len(arms) == 2
    for arm in arms:
        assert list(arm["tier_summary"]) == ["supervisor", "subagent", "tool"]
        assert arm["tier_summary"]["supervisor"]["input_tokens"] > 0
