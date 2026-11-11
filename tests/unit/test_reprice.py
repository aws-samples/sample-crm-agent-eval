# SPDX-License-Identifier: MIT
"""Tests for `eval.reprice`: per-call re-pricing of a stored sweep and its refusals."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import ArmSpec, run_sweep
from eval.reprice import (
    ARCHIVED_PRICING_KEYS,
    INFERENCE_PROFILE_KEY,
    PRE_AGGREGATION_DIRNAME,
    REPRICE_RECORD_FILENAME,
    RepriceRefused,
    attribution_from_entry,
    main,
    reprice_run,
    with_archived_pricing,
)

_SONNET = "anthropic.claude-sonnet-5-5"
_HAIKU = "anthropic.claude-haiku-5-5"
_GEO_MULTIPLIER = 1.10


@pytest.fixture(autouse=True)
def _noop_seeder(monkeypatch: pytest.MonkeyPatch) -> None:
    def _seed(store: Store, *, tenant: str) -> None:
        del store, tenant

    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: _seed)


@pytest.fixture
def sweep_dir(tmp_path: Path) -> Iterator[Path]:
    """A one-arm stub sweep's output directory, raw archives included."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        out = tmp_path / "sweep"
        run_sweep(
            store=Store(client=client),
            arm_specs=(ArmSpec(family="anthropic"),),
            output_dir=out,
            archive_raw=True,
            run_id="run-reprice-unit",
        )
    yield out


def _pre_aggregation_path(sweep_dir: Path) -> Path:
    (path,) = (sweep_dir / "raw" / PRE_AGGREGATION_DIRNAME).glob("*.json")
    return path


def _rewrite_entries(sweep_dir: Path, edit: Any) -> None:
    path = _pre_aggregation_path(sweep_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["invocation_attributions"] = edit(payload["invocation_attributions"])
    path.write_text(json.dumps(payload), encoding="utf-8")


def _arm_archive_path(sweep_dir: Path) -> Path:
    (path,) = (sweep_dir / "raw").glob("*.json")
    return path


def _record_in_arm_archive(sweep_dir: Path, entries: list[dict[str, Any]]) -> None:
    """Add ``entries`` to the arm archive's row and tier figures, as if the sweep had made them."""
    path = _arm_archive_path(sweep_dir)
    arm = json.loads(path.read_text(encoding="utf-8"))
    for entry in entries:
        key = (entry["tier"], entry["role"], entry["model_id"])
        rows = [
            r
            for r in arm["tier_role_model_metrics"]
            if (r["tier"], r["role"], r["model_id"]) == key
        ]
        if not rows:
            arm["tier_role_model_metrics"].append(
                {
                    "tier": key[0],
                    "role": key[1],
                    "model_id": key[2],
                    "included_invocation_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
            )
            rows = arm["tier_role_model_metrics"][-1:]
        (row,) = rows
        row["included_invocation_count"] += 1
        row["input_tokens"] += entry["tokens"]["input_tokens"]
        row["output_tokens"] += entry["tokens"]["output_tokens"]
        summary = arm["tier_summary"][entry["tier"]]
        summary["input_tokens"] += entry["tokens"]["input_tokens"]
        summary["output_tokens"] += entry["tokens"]["output_tokens"]
    path.write_text(json.dumps(arm), encoding="utf-8")


def _strip_profiles(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k not in ARCHIVED_PRICING_KEYS} for e in entries]


def _arm(matrix_json: Path) -> dict[str, Any]:
    (arm,) = json.loads(matrix_json.read_text(encoding="utf-8"))["arms"]
    assert isinstance(arm, dict)
    return arm


# -- writer side --------------------------------------------------------------------------------


def _entry(model_id: str, input_tokens: int, tier: str = "supervisor") -> dict[str, Any]:
    return {
        "tier": tier,
        "role": "supervisor" if tier == "supervisor" else "research",
        "model_id": model_id,
        "tokens": {"input_tokens": input_tokens, "output_tokens": 10},
        "cache": {"read_tokens": 0, "write_tokens": 0},
        "latency_ms": None,
    }


def test_with_archived_pricing_adds_profile_tier_and_route() -> None:
    priced = with_archived_pricing(_entry(_SONNET, 1_000))
    assert priced[INFERENCE_PROFILE_KEY] == f"global.{_SONNET}"
    assert priced["pricing_tier"] == "standard"
    assert priced["route"] == "global"


def test_with_archived_pricing_marks_a_long_context_call() -> None:
    priced = with_archived_pricing(_entry(_HAIKU, 120_000, tier="tool"))
    assert priced["pricing_tier"] == "long_context"


def test_a_structural_zero_token_entry_carries_null_pricing_keys() -> None:
    priced = with_archived_pricing(
        {**_entry("", 0), "tokens": {"input_tokens": 0, "output_tokens": 0}}
    )
    assert all(priced[key] is None for key in ARCHIVED_PRICING_KEYS)


def test_a_malformed_entry_is_refused() -> None:
    with pytest.raises(RepriceRefused, match="malformed"):
        attribution_from_entry({"tier": "supervisor"})
    with pytest.raises(RepriceRefused, match="not an object"):
        with_archived_pricing(["not", "an", "entry"])


# -- round trip ----------------------------------------------------------------------------------


def test_a_stub_sweep_repriced_reproduces_its_own_matrix(sweep_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "repriced"
    assert main(["--raw-dir", str(sweep_dir / "raw"), "--output-dir", str(out)]) == 0
    for name in ("matrix.json", "matrix.md", "matrix.csv"):
        assert (out / name).read_bytes() == (sweep_dir / name).read_bytes(), name
    record = json.loads((out / REPRICE_RECORD_FILENAME).read_text(encoding="utf-8"))
    assert record["assumed_inference_profile_prefix"] is None
    (arm,) = record["arms"]
    assert arm["assumed_profile_entry_count"] == 0
    assert arm["inference_profiles"] == {_SONNET: f"global.{_SONNET}"}


def test_a_profile_less_archive_is_refused_without_a_prefix(
    sweep_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _rewrite_entries(sweep_dir, _strip_profiles)
    out = tmp_path / "repriced"
    assert main(["--raw-dir", str(sweep_dir / "raw"), "--output-dir", str(out)]) == 2
    assert "--assume-inference-profile-prefix" in capsys.readouterr().err
    assert not (out / "matrix.json").exists()


def test_an_asserted_prefix_prices_profile_less_entries_and_is_recorded(
    sweep_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry_count = len(
        json.loads(_pre_aggregation_path(sweep_dir).read_text())["invocation_attributions"]
    )
    _rewrite_entries(sweep_dir, _strip_profiles)
    out = tmp_path / "repriced"
    argv = ["--raw-dir", str(sweep_dir / "raw"), "--output-dir", str(out)]
    assert main([*argv, "--assume-inference-profile-prefix", "global."]) == 0
    assert "'global.'" in capsys.readouterr().err
    assert (out / "matrix.json").read_bytes() == (sweep_dir / "matrix.json").read_bytes()
    record = json.loads((out / REPRICE_RECORD_FILENAME).read_text(encoding="utf-8"))
    assert record["assumed_inference_profile_prefix"] == "global."
    assert record["arms"][0]["assumed_profile_entry_count"] == entry_count


def test_each_call_is_priced_from_its_archived_profile(sweep_dir: Path, tmp_path: Path) -> None:
    """A geo profile in the archive raises every cost by the geo multiplier, not the config's."""

    def _to_geo(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**e, INFERENCE_PROFILE_KEY: f"us.{e['model_id']}"} for e in entries]

    _rewrite_entries(sweep_dir, _to_geo)
    out = tmp_path / "repriced"
    reprice_run(sweep_dir / "raw", out)
    original, repriced = _arm(sweep_dir / "matrix.json"), _arm(out / "matrix.json")
    assert original["model_metrics"]["cost_total"] > 0
    assert repriced["model_metrics"]["cost_total"] == pytest.approx(
        original["model_metrics"]["cost_total"] * _GEO_MULTIPLIER
    )
    assert repriced["tier_summary"]["supervisor"]["cost_total"] == pytest.approx(
        original["tier_summary"]["supervisor"]["cost_total"] * _GEO_MULTIPLIER
    )
    (row,) = repriced["tier_role_model_metrics"]
    assert row["cost_total"] == pytest.approx(
        original["tier_role_model_metrics"][0]["cost_total"] * _GEO_MULTIPLIER
    )


def test_a_long_context_call_is_counted_and_priced_at_its_tier(
    sweep_dir: Path, tmp_path: Path
) -> None:
    long_call = with_archived_pricing(_entry(_HAIKU, 120_000, tier="tool"))
    short_call = with_archived_pricing(_entry(_HAIKU, 30_000, tier="tool"))
    _rewrite_entries(sweep_dir, lambda entries: [*entries, long_call, short_call])
    _record_in_arm_archive(sweep_dir, [long_call, short_call])
    out = tmp_path / "repriced"
    reprice_run(sweep_dir / "raw", out)
    repriced = _arm(out / "matrix.json")
    assert repriced["tier_summary"]["tool"]["long_context_invocation_count"] == 1
    assert repriced["tier_summary"]["supervisor"]["long_context_invocation_count"] == 0
    (tool_row,) = [r for r in repriced["tier_role_model_metrics"] if r["tier"] == "tool"]
    assert tool_row["long_context_invocation_count"] == 1
    assert tool_row["included_invocation_count"] == 2
    assert repriced["tier_summary"]["tool"]["cost_total"] == pytest.approx(tool_row["cost_total"])
    assert repriced["model_metrics"] == _arm(sweep_dir / "matrix.json")["model_metrics"]
    assert repriced["arm_long_context_invocation_count"] == 1
    tier_costs = [
        repriced["tier_summary"][t]["cost_total"] for t in ("supervisor", "subagent", "tool")
    ]
    assert repriced["arm_cost_total"] == pytest.approx(sum(tier_costs), abs=1e-9)
    assert repriced["arm_cost_total"] > repriced["model_metrics"]["cost_total"]
    record = json.loads((out / REPRICE_RECORD_FILENAME).read_text(encoding="utf-8"))
    assert record["arms"][0]["arm_long_context_invocation_count"] == 1
    assert record["arms"][0]["arm_cost_total"] == repriced["arm_cost_total"]


# -- refusals ------------------------------------------------------------------------------------


def test_an_arm_without_a_pre_aggregation_archive_is_refused(
    sweep_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(sweep_dir / "raw" / PRE_AGGREGATION_DIRNAME)
    out = tmp_path / "repriced"
    assert main(["--raw-dir", str(sweep_dir / "raw"), "--output-dir", str(out)]) == 2
    assert "no per-call granularity" in capsys.readouterr().err
    assert not out.exists()


def test_one_model_under_two_profiles_is_refused(sweep_dir: Path, tmp_path: Path) -> None:
    def _mixed(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**entries[0], INFERENCE_PROFILE_KEY: f"us.{_SONNET}"}, *entries[1:]]

    _rewrite_entries(sweep_dir, _mixed)
    with pytest.raises(RepriceRefused, match="more than one profile"):
        reprice_run(sweep_dir / "raw", tmp_path / "repriced")


@pytest.mark.parametrize("profile", ["", 7, f"eu-west.{_SONNET}", f"global.{_HAIKU}"])
def test_an_unusable_archived_profile_is_refused(
    profile: object, sweep_dir: Path, tmp_path: Path
) -> None:
    _rewrite_entries(
        sweep_dir, lambda entries: [{**e, INFERENCE_PROFILE_KEY: profile} for e in entries]
    )
    with pytest.raises(RepriceRefused):
        reprice_run(sweep_dir / "raw", tmp_path / "repriced")
    assert not (tmp_path / "repriced" / "matrix.json").exists()


def test_two_pre_aggregation_archives_for_one_model_are_refused(
    sweep_dir: Path, tmp_path: Path
) -> None:
    path = _pre_aggregation_path(sweep_dir)
    shutil.copy(path, path.with_name("pre-aggregation-arm-9-copy.json"))
    with pytest.raises(RepriceRefused, match="both hold pre-aggregation records"):
        reprice_run(sweep_dir / "raw", tmp_path / "repriced")


def test_an_empty_raw_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    assert main(["--raw-dir", str(tmp_path / "raw"), "--output-dir", str(tmp_path / "o")]) == 2


# -- cross-check against the arm archive ---------------------------------------------------------


def test_a_pre_aggregation_archive_missing_one_call_is_refused(
    sweep_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _rewrite_entries(sweep_dir, lambda entries: entries[1:])
    out = tmp_path / "repriced"
    assert main(["--raw-dir", str(sweep_dir / "raw"), "--output-dir", str(out)]) == 2
    err = capsys.readouterr().err
    assert "disagree with the arm archive" in err
    assert "included_invocation_count: archived" in err
    assert not out.exists()


def test_per_call_tokens_that_differ_from_the_archive_are_refused(
    sweep_dir: Path, tmp_path: Path
) -> None:
    def _inflate(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        first = {**entries[0], "tokens": {**entries[0]["tokens"]}}
        first["tokens"]["input_tokens"] += 1
        return [first, *entries[1:]]

    _rewrite_entries(sweep_dir, _inflate)
    with pytest.raises(RepriceRefused, match="input_tokens: archived"):
        reprice_run(sweep_dir / "raw", tmp_path / "repriced")


def test_a_call_absent_from_the_arm_archive_is_refused(sweep_dir: Path, tmp_path: Path) -> None:
    extra = with_archived_pricing(_entry(_HAIKU, 500, tier="tool"))
    _rewrite_entries(sweep_dir, lambda entries: [*entries, extra])
    with pytest.raises(RepriceRefused, match="tool/research/"):
        reprice_run(sweep_dir / "raw", tmp_path / "repriced")


def test_a_tier_summary_that_disagrees_with_the_calls_is_refused(
    sweep_dir: Path, tmp_path: Path
) -> None:
    path = _arm_archive_path(sweep_dir)
    arm = json.loads(path.read_text(encoding="utf-8"))
    arm["tier_summary"]["supervisor"]["output_tokens"] += 1
    path.write_text(json.dumps(arm), encoding="utf-8")
    with pytest.raises(RepriceRefused, match=r"tier_summary\['supervisor'\] output_tokens"):
        reprice_run(sweep_dir / "raw", tmp_path / "repriced")


def test_an_arm_archive_without_rows_cannot_be_cross_checked(
    sweep_dir: Path, tmp_path: Path
) -> None:
    path = _arm_archive_path(sweep_dir)
    arm = json.loads(path.read_text(encoding="utf-8"))
    arm["tier_role_model_metrics"] = []
    path.write_text(json.dumps(arm), encoding="utf-8")
    with pytest.raises(RepriceRefused, match="cannot be cross-checked"):
        reprice_run(sweep_dir / "raw", tmp_path / "repriced")
