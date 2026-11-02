# SPDX-License-Identifier: MIT
"""`config/eval_live_estimate.yaml` agrees with `config/models.yaml` and parses under `eval.runs`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from crm.metering import TokenUsage, cost_for_usage
from eval import runs
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import TIERS

MATRIX = load_capability_matrix()
_MODELS_PATH = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


def test_every_family_and_tier_names_that_familys_tier_model() -> None:
    per_task, _effort = runs.load_static_estimate()
    assert set(per_task) == set(MATRIX.tiers)
    for family, tiers in per_task.items():
        assert tuple(tiers) == TIERS
        for tier, entry in tiers.items():
            assert entry["model_id"] == MATRIX.tiers[family][tier].model_id  # type: ignore[index]


def test_every_model_is_priced() -> None:
    for tiers in runs.load_static_estimate()[0].values():
        for entry in tiers.values():
            assert cost_for_usage(TokenUsage(1, 1), str(entry["model_id"])).total > 0


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.update(schema_version=2), "schema_version 1"),
        (lambda d: d.pop("per_task"), "per_task"),
        (lambda d: d["per_task"]["openai"]["tool"].pop("model_id"), "model_id"),
        (lambda d: d["per_task"]["openai"]["tool"].update(input_tokens=-1), "input_tokens"),
        (lambda d: d["per_task"]["openai"]["tool"].update(output_tokens=1.5), "output_tokens"),
        (lambda d: d.pop("measured_at_supervisor_effort"), "measured_at_supervisor_effort"),
        (lambda d: d.update(measured_at_supervisor_effort=""), "measured_at_supervisor_effort"),
        (lambda d: d.update(measured_at_supervisor_effort=3), "measured_at_supervisor_effort"),
    ],
    ids=[
        "schema",
        "no-per-task",
        "no-model",
        "negative",
        "fractional",
        "no-measured-effort",
        "empty-measured-effort",
        "non-string-measured-effort",
    ],
)
def test_the_loader_refuses_a_malformed_file(tmp_path: Path, mutate: object, match: str) -> None:
    data = yaml.safe_load(runs.DEFAULT_ESTIMATE_PATH.read_text(encoding="utf-8"))
    mutate(data)  # type: ignore[operator]
    path = tmp_path / "estimate.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        runs.load_static_estimate(path)


def test_an_unmutated_copy_loads(tmp_path: Path) -> None:
    """Control for the refusals above."""
    path = tmp_path / "estimate.yaml"
    path.write_text(runs.DEFAULT_ESTIMATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    assert runs.load_static_estimate(path) == runs.load_static_estimate()


def test_the_figures_were_measured_at_every_familys_configured_supervisor_effort() -> None:
    """`measured_at_supervisor_effort` equals each family's supervisor `tuning.effort` in
    `config/models.yaml`, so the static estimate carries no effort caveat."""
    models = yaml.safe_load(_MODELS_PATH.read_text(encoding="utf-8"))
    configured = {
        family: spec["tiers"]["supervisor"]["tuning"]["effort"]
        for family, spec in models["families"].items()
    }
    assert set(configured) == {"anthropic", "openai"}
    _per_task, measured = runs.load_static_estimate()
    assert all(effort == measured for effort in configured.values()), (measured, configured)
