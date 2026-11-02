# SPDX-License-Identifier: MIT
"""The prompt-bundle digest travels from the observed turn into arm provenance.

Covers the turn's wire form, the SFN result row, arm assembly, the archived provenance and its
rendering, and an observed turn recording the digest of the process that ran it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Iterator
from typing import Any, cast

import boto3
import pytest
from moto import mock_aws

from agents.prompt_bundle import prompt_bundle_sha256
from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness, matrix, sfn_handlers
from eval.scoring import ObservedToolArguments, ToolCall, TurnOutcome
from eval.sweep_units import ArmSpec, TaskUnitResult, arm_turn_tuning, assemble_arm, run_task_unit
from eval.turn_observation import (
    PROMPT_BUNDLE_SHA256_KEY,
    ObservedTurn,
    deserialise_turn,
    observe_turn,
    serialise_turn,
)
from providers.capabilities import load_capability_matrix
from providers.stub import StubProvider

MATRIX = load_capability_matrix()
TENANT = "eval-bundle"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _turn(digest: str | None) -> ObservedTurn:
    return ObservedTurn(
        outcome=TurnOutcome(
            tenant=TENANT,
            tool_calls=(ToolCall(tool_name="search_accounts"),),
            denial_outcome="data",
        ),
        tool_arguments=(
            ObservedToolArguments(tool_name="search_accounts", arguments={"q": "acme"}),
        ),
        invocations=(
            InvocationAttribution(
                tier="supervisor",
                role="supervisor",
                model_id=MATRIX.model_for("anthropic", "supervisor").model_id,
                tokens=TokenCounts(input_tokens=10, output_tokens=2),
                cache=CacheCounts(),
            ),
        ),
        stream_calls=1,
        prompt_bundle_sha256=digest,
    )


def _task() -> dict[str, object]:
    from eval.task_ids import localize_tasks

    tasks = harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH)
    return localize_tasks(tasks, tenant=TENANT)[0]


def _unit(store: Store, turn: ObservedTurn) -> TaskUnitResult:
    return run_task_unit(
        store=store,
        task=_task(),
        tenant=TENANT,
        family="anthropic",
        turn_runner=lambda _t, _n: turn,
    )


def _assemble(units: list[TaskUnitResult]) -> Any:
    return assemble_arm(
        units,
        matrix=MATRIX,
        arm_spec=ArmSpec(family="anthropic"),
        mode="stub",
        dataset_seed=1,
        git_revision="rev",
        runtime_identifier="test",
        price_table_version="1.0.0",
        cost_withheld=True,
        timestamp="2026-01-01T00:00:00+00:00",
    )


# --- the observed turn and its wire form ----------------------------------------------------------


def test_an_observed_turn_records_the_digest_of_the_process_that_ran_it() -> None:
    turn = asyncio.run(
        observe_turn(
            prompt="which accounts slipped?",
            task_id="gt-unit",
            tenant=TENANT,
            providers=lambda _family: StubProvider(),
            matrix=MATRIX,
            tuning=arm_turn_tuning(ArmSpec(family="anthropic")),
            dispatch=lambda _tool, _input: {"statusCode": 200, "body": {"items": []}},
        )
    )
    assert turn.prompt_bundle_sha256 == prompt_bundle_sha256()


def test_the_wire_form_round_trips_the_digest() -> None:
    wire = serialise_turn(_turn(DIGEST_A))
    assert wire[PROMPT_BUNDLE_SHA256_KEY] == DIGEST_A
    assert deserialise_turn(wire).prompt_bundle_sha256 == DIGEST_A


def test_a_turn_without_a_digest_serialises_without_the_key_and_reads_back_as_none() -> None:
    wire = serialise_turn(_turn(None))
    assert PROMPT_BUNDLE_SHA256_KEY not in wire
    assert deserialise_turn(wire).prompt_bundle_sha256 is None


def test_a_non_string_digest_on_the_wire_is_refused() -> None:
    wire = serialise_turn(_turn(DIGEST_A))
    wire[PROMPT_BUNDLE_SHA256_KEY] = 7
    with pytest.raises(ValueError, match=PROMPT_BUNDLE_SHA256_KEY):
        deserialise_turn(wire)


# --- the SFN result row ---------------------------------------------------------------------------


def test_the_result_row_round_trips_the_digest(store: Store) -> None:
    unit = _unit(store, _turn(DIGEST_A))
    row = cast(dict[str, object], sfn_handlers._unit_fields(unit))
    assert row[PROMPT_BUNDLE_SHA256_KEY] == DIGEST_A
    assert sfn_handlers.turn_from_row(row).prompt_bundle_sha256 == DIGEST_A


def test_the_round_trip_cell_reds_when_the_row_drops_the_digest(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: a row writer that drops the key loses the digest, which the cell above detects."""

    def dropping(turn: ObservedTurn) -> dict[str, Any]:
        wire = serialise_turn(turn)
        wire.pop(PROMPT_BUNDLE_SHA256_KEY, None)
        return wire

    monkeypatch.setattr(sfn_handlers, "serialise_turn", dropping)
    row = cast(dict[str, object], sfn_handlers._unit_fields(_unit(store, _turn(DIGEST_A))))
    assert PROMPT_BUNDLE_SHA256_KEY not in row
    assert sfn_handlers.turn_from_row(row).prompt_bundle_sha256 != DIGEST_A


def test_a_row_without_the_digest_reads_back_as_none(store: Store) -> None:
    row = cast(dict[str, object], sfn_handlers._unit_fields(_unit(store, _turn(None))))
    assert PROMPT_BUNDLE_SHA256_KEY not in row
    assert sfn_handlers.turn_from_row(row).prompt_bundle_sha256 is None


# --- arm assembly ---------------------------------------------------------------------------------


def test_an_arm_whose_units_share_a_digest_records_it(store: Store) -> None:
    units = [_unit(store, _turn(DIGEST_A)), _unit(store, _turn(DIGEST_A))]
    assert _assemble(units).arm_result.provenance.prompt_bundle_sha256 == DIGEST_A


def test_an_arm_whose_units_carry_two_digests_is_refused(store: Store) -> None:
    units = [_unit(store, _turn(DIGEST_A)), _unit(store, _turn(DIGEST_B))]
    with pytest.raises(AssertionError, match="different prompt bundles"):
        _assemble(units)


def test_a_unit_without_a_digest_beside_one_with_it_is_refused(store: Store) -> None:
    units = [_unit(store, _turn(DIGEST_A)), _unit(store, _turn(None))]
    with pytest.raises(AssertionError, match="different prompt bundles"):
        _assemble(units)


def test_an_arm_of_units_without_a_digest_records_none(store: Store) -> None:
    assert _assemble([_unit(store, _turn(None))]).arm_result.provenance.prompt_bundle_sha256 is None


# --- archived provenance --------------------------------------------------------------------------


def _arm_fields(store: Store, digest: str | None) -> dict[str, object]:
    arm = _assemble([_unit(store, _turn(digest))]).arm_result
    return cast(dict[str, object], json.loads(json.dumps(arm.to_report_fields())))


def test_the_digest_is_emitted_right_after_the_answer_style_digest(store: Store) -> None:
    provenance = list(cast(dict[str, object], _arm_fields(store, DIGEST_A)["provenance"]))
    assert provenance.index("prompt_bundle_sha256") == provenance.index("answer_style_sha256") + 1
    assert list(matrix._PROVENANCE_FIELDS)[-2:] == ["answer_style_sha256", "prompt_bundle_sha256"]


def test_an_archive_with_the_digest_reloads_it(store: Store) -> None:
    arm = matrix.arm_from_report_fields(_arm_fields(store, DIGEST_A))
    assert arm.provenance.prompt_bundle_sha256 == DIGEST_A


def test_an_archive_without_the_field_loads_and_renders_a_dash(store: Store) -> None:
    fields = _arm_fields(store, DIGEST_A)
    del cast(dict[str, object], fields["provenance"])["prompt_bundle_sha256"]
    arm = matrix.arm_from_report_fields(fields)
    assert arm.provenance.prompt_bundle_sha256 is None
    rendered = matrix.render_markdown(matrix.build_matrix_result([arm]))
    assert "| prompt_bundle_sha256 | — |" in rendered


def test_an_archive_missing_a_required_field_is_still_refused(store: Store) -> None:
    fields = _arm_fields(store, DIGEST_A)
    del cast(dict[str, object], fields["provenance"])["answer_style_sha256"]
    with pytest.raises(ValueError, match="answer_style_sha256"):
        matrix.arm_from_report_fields(fields)


def test_the_provenance_dataclass_defaults_the_digest_to_none() -> None:
    field = {f.name: f for f in dataclasses.fields(matrix.RunProvenance)}["prompt_bundle_sha256"]
    assert field.default is None


# --- one matrix, one instrument -------------------------------------------------------------------


def _arm_with_digest(store: Store, digest: str | None) -> matrix.ArmResult:
    return cast(matrix.ArmResult, _assemble([_unit(store, _turn(digest))]).arm_result)


def test_a_matrix_of_arms_sharing_a_digest_builds(store: Store) -> None:
    arms = [_arm_with_digest(store, DIGEST_A), _arm_with_digest(store, DIGEST_A)]
    assert matrix.build_matrix_result(arms).arms == tuple(arms)


@pytest.mark.parametrize(("first", "second"), [(DIGEST_A, DIGEST_B), (DIGEST_A, None)])
def test_a_matrix_of_arms_with_different_digests_is_refused(
    store: Store, first: str, second: str | None
) -> None:
    arms = [_arm_with_digest(store, first), _arm_with_digest(store, second)]
    with pytest.raises(ValueError, match="2 different prompt bundles"):
        matrix.build_matrix_result(arms)


def test_a_matrix_of_archives_that_predate_the_digest_builds(store: Store) -> None:
    arms = [_arm_with_digest(store, None), _arm_with_digest(store, None)]
    assert matrix.build_matrix_result(arms).arms == tuple(arms)
