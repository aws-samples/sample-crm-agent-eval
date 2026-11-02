# SPDX-License-Identifier: MIT
"""`providers.tier_tuning`: the schema-3 wire block, every rejection, and per-tier resolution.

Every rejection asserts `ValidationFailed.source`, the path the UI uses to place the message.
Runs against the real `config/` matrix; no model or AWS call.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable, Mapping
from typing import Any, get_args

import pytest

from agents.runtime_entrypoint import resolve_tuning
from agents.turn_driver import session_turn_tuning
from crm.errors import ValidationFailed
from eval.harness import DEFAULT_ARM_SPECS
from eval.sweep_units import ArmSpec, arm_turn_tuning
from providers.base import ResolvedInvocation, Tuning
from providers.capabilities import (
    CapabilityMatrix,
    Family,
    Tier,
    TierAssignment,
    load_capability_matrix,
)
from providers.tier_tuning import (
    TIER_FIELDS,
    TIERS,
    WIRE_SCHEMA_VERSION,
    TierRequest,
    TurnTuning,
    defaults,
    parse_stored_tuning,
    parse_wire_tuning,
    resolve_tier,
    resolve_turn,
    to_wire,
)

MATRIX = load_capability_matrix()
SONNET = MATRIX.model_for("anthropic", "subagent").model_id
HAIKU = MATRIX.model_for("anthropic", "tool").model_id
#: The anthropic model in the matrix that no tier assigns.
OPUS = next(
    model_id
    for model_id, model in MATRIX.models.items()
    if model.family == "anthropic"
    and model_id not in {assignment.model_id for assignment in MATRIX.tiers["anthropic"].values()}
)
SOL = MATRIX.model_for("openai", "supervisor").model_id
TERRA = MATRIX.model_for("openai", "subagent").model_id
LUNA = MATRIX.model_for("openai", "tool").model_id

#: A full schema-3 block naming every tier.
EXAMPLE: dict[str, Any] = {
    "schema": 3,
    "source": "settings.tuning",
    "tiers": {
        "supervisor": {"family": "anthropic", "model": OPUS, "effort": "low"},
        "subagent": {"family": "openai", "model": TERRA, "effort": None},
        "tool": {"family": "anthropic", "model": None, "effort": "low"},
    },
}

#: A stored legacy schema-2 block: every tier carries a temperature key.
LEGACY_STORED: dict[str, Any] = {
    "schema": 2,
    "source": "settings.tuning",
    "tiers": {
        "supervisor": {"family": "anthropic", "model": OPUS, "effort": "low", "temperature": None},
        "subagent": {"family": "openai", "model": TERRA, "effort": None, "temperature": None},
        "tool": {"family": "anthropic", "model": None, "effort": None, "temperature": 0.2},
    },
}


def _block(**tiers: dict[str, object]) -> dict[str, object]:
    return {"schema": 3, "source": "test", "tiers": tiers}


def _parse_rejects(raw: object) -> ValidationFailed:
    with pytest.raises(ValidationFailed) as exc_info:
        parse_wire_tuning(raw, default_source="test")
    return exc_info.value


def _resolve_rejects(**tiers: dict[str, object]) -> ValidationFailed:
    tuning = parse_wire_tuning(_block(**tiers), default_source="test")
    with pytest.raises(ValidationFailed) as exc_info:
        resolve_turn(MATRIX, tuning)
    return exc_info.value


class TestTheBlockItself:
    def test_the_wire_schema_is_3_and_tiers_carry_no_temperature(self) -> None:
        assert WIRE_SCHEMA_VERSION == 3
        assert TIER_FIELDS == ("family", "model", "effort")
        assert "temperature" not in TierRequest.__slots__

    def test_the_example_parses_and_resolves(self) -> None:
        resolved = resolve_turn(MATRIX, parse_wire_tuning(EXAMPLE, default_source="x"))
        assert resolved["supervisor"].model.model_id == OPUS
        assert resolved["supervisor"].effort == "low"
        assert resolved["subagent"].model.model_id == TERRA
        assert resolved["tool"].model.model_id == HAIKU
        assert resolved["tool"].effort == "low"
        assert "temperature" not in type(resolved["tool"]).__slots__

    @pytest.mark.parametrize("raw", [None, [], "tuning", 2])
    def test_a_non_object_block_is_rejected(self, raw: object) -> None:
        assert _parse_rejects(raw).source == "tuning"

    @pytest.mark.parametrize(
        "raw",
        [
            {"family": "openai", "effort": "low", "temperature": 0.3},
            {"schema": 1, "family": "openai"},
            {"tiers": {}},
            {"schema": "3", "tiers": {}},
            {"schema": True, "tiers": {}},
            {"schema": 2, "tiers": {}},
            {"schema": 2, "tiers": {"tool": {"effort": "low"}}},
            {"schema": 4, "tiers": {}},
            {"schema": 3.0, "tiers": {}},
        ],
        ids=[
            "flat-schema-1",
            "schema-1",
            "no-schema",
            "string-schema",
            "bool-schema",
            "schema-2-empty",
            "schema-2-without-temperature",
            "schema-4",
            "float-schema",
        ],
    )
    def test_anything_but_schema_3_is_rejected_never_reinterpreted(self, raw: object) -> None:
        error = _parse_rejects(raw)
        assert error.source == "tuning.schema"

    def test_a_flat_key_beside_schema_3_is_rejected(self) -> None:
        assert _parse_rejects({"schema": 3, "effort": "low"}).source == "tuning.effort"

    def test_the_source_defaults_and_must_be_a_non_empty_string(self) -> None:
        assert parse_wire_tuning({"schema": 3}, default_source="chat").source == "chat"
        assert parse_wire_tuning({"schema": 3}, default_source="chat").tiers == {}
        for bad in (3, "", None):
            assert _parse_rejects({"schema": 3, "source": bad}).source == "tuning.source"

    def test_tiers_must_be_an_object(self) -> None:
        assert _parse_rejects({"schema": 3, "tiers": []}).source == "tuning.tiers"


class TestTemperatureIsRefused:
    """A temperature key is refused by name, before the schema check."""

    @pytest.mark.parametrize("value", [0.2, None, 0, "hot"], ids=["float", "null", "int", "str"])
    @pytest.mark.parametrize("schema", [3, 2], ids=["schema-3", "schema-2"])
    @pytest.mark.parametrize("tier", TIERS)
    def test_a_temperature_key_names_its_tier(self, tier: str, schema: int, value: object) -> None:
        raw = {"schema": schema, "tiers": {tier: {"effort": "low", "temperature": value}}}
        error = _parse_rejects(raw)
        assert error.source == f"tuning.tiers.{tier}.temperature"
        assert f"tuning.tiers.{tier}.temperature" in error.message
        assert f"tuning.tiers.{tier}.temperature" in str(error)

    def test_the_temperature_rule_precedes_the_schema_rule(self) -> None:
        """A stale schema-2 client is told about temperature, not about the schema."""
        assert _parse_rejects(copy.deepcopy(LEGACY_STORED)).source == (
            "tuning.tiers.supervisor.temperature"
        )

    def test_the_temperature_rule_also_precedes_an_unknown_tier(self) -> None:
        error = _parse_rejects({"schema": 2, "tiers": {"planner": {"temperature": 0.1}}})
        assert error.source == "tuning.tiers.planner.temperature"

    def test_without_the_key_the_same_block_parses(self) -> None:
        """Control: dropping only the temperature key leaves a block that parses."""
        raw = {"schema": 3, "tiers": {"tool": {"effort": "low"}}}
        assert parse_wire_tuning(raw, default_source="t").tiers["tool"].effort == "low"


class TestStoredTuning:
    """The stored-row reader also accepts schema 2, dropping temperature."""

    def test_a_legacy_stored_block_parses_without_its_temperatures(self) -> None:
        tuning = parse_stored_tuning(copy.deepcopy(LEGACY_STORED), default_source="x")
        assert tuning.source == "settings.tuning"
        assert tuning.tiers["supervisor"] == TierRequest(
            family="anthropic", model=OPUS, effort="low"
        )
        assert tuning.tiers["subagent"] == TierRequest(family="openai", model=TERRA, effort=None)
        assert tuning.tiers["tool"] == TierRequest(family="anthropic", model=None, effort=None)
        assert "temperature" not in to_wire(tuning)["tiers"]["tool"]  # type: ignore[index]

    def test_a_legacy_stored_block_is_not_mutated(self) -> None:
        raw = copy.deepcopy(LEGACY_STORED)
        parse_stored_tuning(raw, default_source="x")
        assert raw == LEGACY_STORED

    def test_a_schema_2_block_without_temperature_is_read(self) -> None:
        raw = {"schema": 2, "tiers": {"tool": {"effort": "low"}}}
        assert parse_stored_tuning(raw, default_source="x").tiers["tool"].effort == "low"

    def test_a_schema_3_block_is_read_as_the_wire_parser_reads_it(self) -> None:
        raw = copy.deepcopy(EXAMPLE)
        assert parse_stored_tuning(raw, default_source="x") == parse_wire_tuning(
            raw, default_source="x"
        )

    def test_a_schema_3_block_with_temperature_is_still_refused(self) -> None:
        with pytest.raises(ValidationFailed) as exc_info:
            parse_stored_tuning(
                {"schema": 3, "tiers": {"tool": {"temperature": 0.2}}}, default_source="x"
            )
        assert exc_info.value.source == "tuning.tiers.tool.temperature"

    @pytest.mark.parametrize(
        ("raw", "source"),
        [
            (None, "tuning"),
            ({"schema": 1}, "tuning.schema"),
            ({"schema": 2, "tiers": {"tool": {"top_p": 0.9}}}, "tuning.tiers.tool.top_p"),
            ({"schema": 2, "tiers": []}, "tuning.tiers"),
            ({"schema": 2, "tiers": {"tool": "fast"}}, "tuning.tiers.tool"),
            ({"schema": 2, "extra": 1}, "tuning.extra"),
        ],
        ids=["non-object", "schema-1", "unknown-field", "tiers-list", "tier-string", "extra-key"],
    )
    def test_every_other_rule_still_applies(self, raw: object, source: str) -> None:
        with pytest.raises(ValidationFailed) as exc_info:
            parse_stored_tuning(raw, default_source="x")
        assert exc_info.value.source == source


class TestStructuralRejections:
    def test_an_unknown_tier_is_rejected(self) -> None:
        assert _parse_rejects(_block(planner={})).source == "tuning.tiers.planner"

    def test_a_non_object_tier_is_rejected(self) -> None:
        assert _parse_rejects({"schema": 3, "tiers": {"tool": "fast"}}).source == (
            "tuning.tiers.tool"
        )

    def test_an_unknown_field_is_rejected(self) -> None:
        assert _parse_rejects(_block(tool={"top_p": 0.9})).source == "tuning.tiers.tool.top_p"

    def test_an_unknown_family_is_rejected(self) -> None:
        error = _parse_rejects(_block(subagent={"family": "gemini"}))
        assert error.source == "tuning.tiers.subagent.family"
        assert "gemini" in error.message

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("model", 5),
            ("effort", 1),
            ("family", 7),
        ],
    )
    def test_a_field_of_the_wrong_type_is_rejected(self, field: str, value: object) -> None:
        assert _parse_rejects(_block(supervisor={field: value})).source == (
            f"tuning.tiers.supervisor.{field}"
        )


class TestMatrixRejections:
    def test_a_model_absent_from_the_matrix_is_rejected(self) -> None:
        error = _resolve_rejects(supervisor={"model": "anthropic.claude-imaginary"})
        assert error.source == "tuning.tiers.supervisor.model"

    def test_a_model_of_another_family_is_rejected(self) -> None:
        error = _resolve_rejects(supervisor={"family": "anthropic", "model": SOL})
        assert error.source == "tuning.tiers.supervisor.model"

    def test_a_model_of_another_family_than_the_default_is_rejected(self) -> None:
        assert MATRIX.default_family == "openai"
        assert _resolve_rejects(subagent={"model": SONNET}).source == "tuning.tiers.subagent.model"

    def test_effort_outside_the_models_values_is_rejected(self) -> None:
        error = _resolve_rejects(subagent={"family": "anthropic", "model": SONNET, "effort": "max"})
        assert error.source == "tuning.tiers.subagent.effort"

    def test_a_rung_only_the_other_family_has_is_rejected(self) -> None:
        """`none` is an OpenAI rung; the Anthropic tool tier refuses it, the OpenAI one accepts it."""
        error = _resolve_rejects(tool={"family": "anthropic", "effort": "none"})
        assert error.source == "tuning.tiers.tool.effort"
        tuning = parse_wire_tuning(
            _block(tool={"family": "openai", "effort": "none"}), default_source="t"
        )
        assert resolve_turn(MATRIX, tuning)["tool"].effort == "none"

    def test_an_unconfigured_family_on_a_constructed_request_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed) as exc_info:
            resolve_tier(
                MATRIX,
                "tool",
                TierRequest(family="gemini"),  # type: ignore[arg-type]
                source="t",
            )
        assert exc_info.value.source == "tuning.tiers.tool.family"

    def test_an_unknown_tier_on_a_constructed_tuning_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed) as exc_info:
            TurnTuning(source="t", tiers={"planner": TierRequest()})  # type: ignore[dict-item]
        assert exc_info.value.source == "tuning.tiers.planner"

    @pytest.mark.parametrize("source", ["", None, 3])
    def test_a_constructed_tuning_needs_a_non_empty_source(self, source: object) -> None:
        with pytest.raises(ValidationFailed) as exc_info:
            TurnTuning(source=source, tiers={})  # type: ignore[arg-type]
        assert exc_info.value.source == "tuning.source"

    def test_a_constructed_tuning_holds_a_read_only_copy_of_its_tiers(self) -> None:
        given = {"tool": TierRequest(effort="low")}
        tuning = TurnTuning(source="t", tiers=given)  # type: ignore[arg-type]
        given["supervisor"] = TierRequest(effort="max")
        assert set(tuning.tiers) == {"tool"}
        with pytest.raises(TypeError):
            tuning.tiers["supervisor"] = TierRequest()  # type: ignore[index]

    @pytest.mark.parametrize("value", [10**400, -(10**400)], ids=["huge", "huge-negative"])
    def test_a_temperature_too_large_for_a_float_is_refused_by_name(self, value: int) -> None:
        error = _parse_rejects({"schema": 3, "tiers": {"tool": {"temperature": value}}})
        assert error.source == "tuning.tiers.tool.temperature"

    def test_a_rejection_records_where_the_request_came_from(self) -> None:
        tuning = parse_wire_tuning(
            {"schema": 3, "source": "settings.tuning", "tiers": {"tool": {"effort": "ultra"}}},
            default_source="t",
        )
        with pytest.raises(ValidationFailed) as exc_info:
            resolve_turn(MATRIX, tuning)
        assert exc_info.value.detail is not None
        assert "settings.tuning" in exc_info.value.detail


def _configured_effort(family: Family, tier: Tier) -> str:
    return MATRIX.tiers[family][tier].tuning["effort"]


def _efforts(resolved: Mapping[Tier, ResolvedInvocation]) -> dict[str, tuple[str, str | None]]:
    return {
        tier: (invocation.model.model_id, invocation.effort)
        for tier, invocation in resolved.items()
    }


def _configured(family: Family) -> dict[str, tuple[str, str | None]]:
    return {
        tier: (MATRIX.tiers[family][tier].model_id, _configured_effort(family, tier))
        for tier in TIERS
    }


class TestUnsetEffortHonoursModelsYaml:
    """An unset effort on a tier running its configured model takes models.yaml's effort."""

    @pytest.mark.parametrize("family", get_args(Family))
    def test_the_configured_efforts_differ_from_the_model_defaults(self, family: Family) -> None:
        """Control: for each family some tier's models.yaml effort is not its model's default."""
        assert any(
            _configured_effort(family, tier) != MATRIX.model_for(family, tier).effort.default
            for tier in TIERS
        )

    def test_the_d27_outcome_for_both_families(self) -> None:
        assert _configured("anthropic") == {
            "supervisor": (SONNET, "medium"),
            "subagent": (SONNET, "low"),
            "tool": (HAIKU, "low"),
        }
        assert _configured("openai") == {
            "supervisor": (SOL, "medium"),
            "subagent": (TERRA, "low"),
            "tool": (LUNA, "low"),
        }

    @pytest.mark.parametrize("family", get_args(Family))
    def test_a_turn_with_no_tuning_block_runs_the_configured_efforts(self, family: Family) -> None:
        """X3-1 (a-none): the Runtime's no-block path, and a family-only block."""
        if family == MATRIX.default_family:
            _, resolved = resolve_tuning(MATRIX, raw_tuning=None)
            assert _efforts(resolved) == _configured(family)
        block = {"schema": 3, "tiers": {tier: {"family": family} for tier in TIERS}}
        resolved = resolve_turn(MATRIX, parse_wire_tuning(block, default_source="t"))
        assert _efforts(resolved) == _configured(family)

    @pytest.mark.parametrize("parse", [parse_wire_tuning, parse_stored_tuning])
    def test_a_saved_tier_with_a_null_effort_runs_the_configured_effort(
        self, parse: Callable[..., TurnTuning]
    ) -> None:
        """X3-2 (b): a saved block naming the tier's configured model with effort null."""
        block = _block(tool={"family": "anthropic", "model": HAIKU, "effort": None})
        resolved = resolve_turn(MATRIX, parse(block, default_source="settings.tuning"))
        assert resolved["tool"].effort == _configured_effort("anthropic", "tool")

    @pytest.mark.parametrize("family", get_args(Family))
    def test_a_session_tuning_pins_the_supervisor_and_configures_the_rest(
        self, family: Family
    ) -> None:
        """X3-3 (c): the request's effort reaches the supervisor; the others run models.yaml."""
        tuning = session_turn_tuning(Tuning(source="t", family=family, effort="high"))
        expected = _configured(family)
        expected["supervisor"] = (expected["supervisor"][0], "high")
        assert _efforts(resolve_turn(MATRIX, tuning)) == expected

    @pytest.mark.parametrize("arm", DEFAULT_ARM_SPECS, ids=lambda arm: arm.family)
    def test_an_eval_arm_runs_the_configured_efforts(self, arm: ArmSpec) -> None:
        """X3-4 (d): `arm_turn_tuning`, the tuning both sweep paths send."""
        expected = _configured(arm.family)
        if arm.effort is not None:
            expected["supervisor"] = (expected["supervisor"][0], arm.effort)
        assert _efforts(resolve_turn(MATRIX, arm_turn_tuning(arm))) == expected

    @pytest.mark.parametrize(
        ("tier", "model"), [("tool", SONNET), ("subagent", HAIKU), ("tool", OPUS)]
    )
    def test_another_tiers_model_with_effort_unset_runs_its_own_default(
        self, tier: Tier, model: str
    ) -> None:
        resolved = resolve_tier(
            MATRIX, tier, TierRequest(family="anthropic", model=model), source="t"
        )
        assert resolved.effort == MATRIX.models[model].effort.default
        assert resolved.effort != _configured_effort("anthropic", tier)

    def test_an_explicit_effort_wins_over_the_configured_one(self) -> None:
        resolved = resolve_tier(MATRIX, "tool", TierRequest(effort="high"), source="t")
        assert resolved.effort == "high"


def _matrix_with_sonnet_on_the_tool_tier_at_medium() -> CapabilityMatrix:
    tiers = {family: dict(assignments) for family, assignments in MATRIX.tiers.items()}
    tiers["anthropic"]["tool"] = TierAssignment(model_id=SONNET, tuning={"effort": "medium"})
    return dataclasses.replace(MATRIX, tiers=tiers)


class TestOneModelOnTwoTiers:
    """A model configured on two tiers at different efforts resolves each tier's own effort."""

    def test_each_tier_naming_the_shared_model_resolves_its_own_configured_effort(self) -> None:
        matrix = _matrix_with_sonnet_on_the_tool_tier_at_medium()
        subagent = matrix.tiers["anthropic"]["subagent"]
        tool = matrix.tiers["anthropic"]["tool"]
        assert subagent.model_id == tool.model_id == SONNET
        assert (subagent.tuning["effort"], tool.tuning["effort"]) == ("low", "medium")
        assert matrix.models[SONNET].effort.default not in {"medium", "low"}

        request = TierRequest(family="anthropic", model=SONNET)
        shared: tuple[Tier, ...] = ("subagent", "tool")
        resolved = {tier: resolve_tier(matrix, tier, request, source="t") for tier in shared}
        assert {tier: (r.model.model_id, r.effort) for tier, r in resolved.items()} == {
            "subagent": (SONNET, "low"),
            "tool": (SONNET, "medium"),
        }

    def test_the_shipped_supervisor_and_subagent_share_sonnet_at_their_own_efforts(self) -> None:
        request = TierRequest(family="anthropic", model=SONNET)
        shared: tuple[Tier, ...] = ("supervisor", "subagent")
        resolved = {tier: resolve_tier(MATRIX, tier, request, source="t") for tier in shared}
        assert {tier: (r.model.model_id, r.effort) for tier, r in resolved.items()} == {
            "supervisor": (SONNET, "medium"),
            "subagent": (SONNET, "low"),
        }


#: Each family's selectable non-default model.
NON_DEFAULT_MODELS: dict[Family, str] = {
    "anthropic": "anthropic.claude-opus-5-5",
    "openai": "openai.gpt-6-astra",
}


class TestNonDefaultModelsStaySelectable:
    """A turn may name either family's non-default model at any of that family's tiers."""

    @pytest.mark.parametrize("tier", TIERS)
    @pytest.mark.parametrize("family", get_args(Family))
    def test_an_explicit_non_default_model_resolves_at_every_tier(
        self, family: Family, tier: Tier
    ) -> None:
        model_id = NON_DEFAULT_MODELS[family]
        assert model_id not in {a.model_id for a in MATRIX.tiers[family].values()}
        request = TierRequest(family=family, model=model_id)
        resolved = resolve_tier(MATRIX, tier, request, source="t")
        assert resolved.model.model_id == model_id
        assert resolved.effort == MATRIX.models[model_id].effort.default


class TestResolution:
    def test_an_empty_request_resolves_every_tier_to_the_default_family(self) -> None:
        resolved = resolve_turn(MATRIX, TurnTuning(source="t", tiers={}))
        assert tuple(resolved) == TIERS
        for tier in TIERS:
            assert resolved[tier].model == MATRIX.model_for(MATRIX.default_family, tier)
            assert resolved[tier].effort == _configured_effort(MATRIX.default_family, tier)

    def test_mixed_families_resolve_each_tier_in_its_own_family(self) -> None:
        tuning = parse_wire_tuning(
            _block(supervisor={"family": "openai"}, subagent={"family": "anthropic"}),
            default_source="t",
        )
        resolved = resolve_turn(MATRIX, tuning)
        assert resolved["supervisor"].model.model_id == SOL
        assert resolved["subagent"].model.model_id == SONNET
        assert resolved["tool"].model == MATRIX.model_for(MATRIX.default_family, "tool")

    def test_a_tier_may_run_another_tiers_model_of_its_family(self) -> None:
        tuning = parse_wire_tuning(
            _block(tool={"family": "anthropic", "model": SONNET}), default_source="t"
        )
        assert resolve_turn(MATRIX, tuning)["tool"].model.model_id == SONNET

    def test_an_explicit_family_selects_that_familys_tier_model(self) -> None:
        resolved = resolve_tier(MATRIX, "subagent", TierRequest(family="openai"), source="t")
        assert resolved.model.model_id == TERRA


class TestDefaultsAndWire:
    def test_defaults_are_the_models_yaml_assignment_and_tuning(self) -> None:
        tuning = defaults(MATRIX)
        for tier in TIERS:
            assignment = MATRIX.tiers[MATRIX.default_family][tier]
            request = tuning.tiers[tier]
            assert request.family == MATRIX.default_family
            assert request.model == assignment.model_id
            assert request.effort == assignment.tuning.get("effort")

    def test_the_defaults_resolve(self) -> None:
        resolved = resolve_turn(MATRIX, defaults(MATRIX))
        for tier in TIERS:
            expected = MATRIX.tiers[MATRIX.default_family][tier].tuning["effort"]
            assert resolved[tier].effort == expected

    @pytest.mark.parametrize(
        "tuning",
        [
            defaults(MATRIX),
            TurnTuning(source="t", tiers={}),
            TurnTuning(
                source="settings.tuning",
                tiers={"supervisor": TierRequest(family="openai", effort="none")},
            ),
        ],
        ids=["defaults", "empty", "partial"],
    )
    def test_to_wire_is_the_inverse_of_parse(self, tuning: TurnTuning) -> None:
        wire = to_wire(tuning)
        assert wire["schema"] == WIRE_SCHEMA_VERSION
        assert parse_wire_tuning(wire, default_source="other") == tuning

    def test_the_example_round_trips_byte_for_byte(self) -> None:
        raw = copy.deepcopy(EXAMPLE)
        assert to_wire(parse_wire_tuning(raw, default_source="x")) == EXAMPLE

    def test_to_wire_emits_no_temperature_key(self) -> None:
        wire = to_wire(defaults(MATRIX))
        tiers = wire["tiers"]
        assert isinstance(tiers, dict)
        for tier in TIERS:
            assert set(tiers[tier]) == set(TIER_FIELDS)
