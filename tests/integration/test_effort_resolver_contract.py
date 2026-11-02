# SPDX-License-Identifier: MIT
"""The SPA's effort resolver agrees with `providers.tier_tuning.resolve_tier`.

This test computes `resolve_tier` for every family and tier, for the tier's configured model (named
and unnamed) and for every other model of the family, with effort unset and with one explicit
value. It writes the results, beside the capability payload `GET /api/capabilities` serves, to
`tests/integration/fixtures/resolved_efforts.json`.
`frontend/src/components/tuning/capabilityMatrix.test.ts` reads that fixture and asserts that the
TypeScript resolver gives the same effort for every cell. A committed fixture that differs from
what the server computes is rewritten and fails here, so drift on either side fails a test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import proc_run

from crm.handlers.capabilities import _capability_matrix_payload
from providers.capabilities import CapabilityMatrix, load_capability_matrix
from providers.tier_tuning import TIERS, TierRequest, resolve_tier

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO_ROOT / "tests" / "integration" / "fixtures" / "resolved_efforts.json"
_CONSUMER = _REPO_ROOT / "frontend" / "src" / "components" / "tuning" / "capabilityMatrix.test.ts"

_DESCRIPTION = (
    "Written by tests/integration/test_effort_resolver_contract.py from "
    "providers.tier_tuning.resolve_tier; read by "
    "frontend/src/components/tuning/capabilityMatrix.test.ts. Never hand-edited."
)


def _cells(matrix: CapabilityMatrix) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for family in sorted(matrix.tiers):
        for tier in TIERS:
            configured = matrix.tiers[family][tier].model_id
            candidates: list[str | None] = [None, configured]
            candidates += sorted(
                model_id
                for model_id, model in matrix.models.items()
                if model.family == family and model_id != configured
            )
            for model_id in candidates:
                requests: list[str | None] = [None]
                model = matrix.models[model_id or configured]
                if model.effort.supported:
                    requests.append(model.effort.values[0])
                for effort in requests:
                    request = TierRequest(family=family, model=model_id, effort=effort)
                    resolved = resolve_tier(matrix, tier, request, source="contract")
                    cells.append(
                        {
                            "family": family,
                            "tier": tier,
                            "model": model_id,
                            "requested": effort,
                            "resolved_model": resolved.model.model_id,
                            "resolved_effort": resolved.effort,
                        }
                    )
    return cells


def _document() -> str:
    matrix = load_capability_matrix()
    document = {
        "description": _DESCRIPTION,
        "capabilities": _capability_matrix_payload(),
        "cells": _cells(matrix),
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def test_the_committed_fixture_is_what_the_server_resolver_computes() -> None:
    """A stale fixture is rewritten and fails, so the vitest never checks against old results."""
    expected = _document()
    committed = _FIXTURE.read_text(encoding="utf-8") if _FIXTURE.exists() else None
    if committed != expected:
        _FIXTURE.write_text(expected, encoding="utf-8")
    assert committed == expected, f"{_FIXTURE} was stale and has been rewritten; commit it"


def test_the_fixture_is_tracked_and_read_by_the_typescript_contract_cell() -> None:
    tracked = proc_run.run(
        ["git", "ls-files", "--error-unmatch", str(_FIXTURE.relative_to(_REPO_ROOT))],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, f"{_FIXTURE} is not tracked: {tracked.stderr}"
    assert '"tests", "integration", "fixtures", "resolved_efforts.json"' in _CONSUMER.read_text(
        encoding="utf-8"
    )


def test_the_cells_exercise_both_branches_of_the_unset_effort_rule() -> None:
    """Non-vacuity: some configured tier's effort differs from its model's default, and some cell
    resolves an unset effort on a non-configured model to that model's default."""
    matrix = load_capability_matrix()
    cells = _cells(matrix)
    unset = [cell for cell in cells if cell["requested"] is None]
    configured_differs = [
        cell
        for cell in unset
        if cell["resolved_model"] == matrix.tiers[cell["family"]][cell["tier"]].model_id
        and cell["resolved_effort"] != matrix.models[cell["resolved_model"]].effort.default
    ]
    non_configured = [
        cell
        for cell in unset
        if cell["resolved_model"] != matrix.tiers[cell["family"]][cell["tier"]].model_id
    ]
    assert configured_differs
    assert non_configured
    cross_tier = [
        cell
        for cell in non_configured
        if any(
            assignment.model_id == cell["resolved_model"]
            for assignment in matrix.tiers[cell["family"]].values()
        )
    ]
    assert cross_tier, "no cell runs another tier's configured model, e.g. Sonnet at the tool tier"
    for cell in non_configured:
        assert cell["resolved_effort"] == matrix.models[cell["resolved_model"]].effort.default
    for cell in unset:
        configured = matrix.tiers[cell["family"]][cell["tier"]]
        if cell["resolved_model"] == configured.model_id:
            assert cell["resolved_effort"] == configured.tuning.get(
                "effort", matrix.models[cell["resolved_model"]].effort.default
            )
