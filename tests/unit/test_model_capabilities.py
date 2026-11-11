# SPDX-License-Identifier: MIT
"""Assert config/model_capabilities.yaml and config/models.yaml encode the capability matrix."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import providers.capabilities as capabilities_module
from crm.errors import ValidationFailed
from providers.base import Tuning, validate_tuning
from providers.capabilities import API_PATHS, ModelCapability, load_capability_matrix

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_PATH = REPO_ROOT / "config" / "model_capabilities.yaml"
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"

ANTHROPIC_SUPERVISOR = "anthropic.claude-sonnet-5-5"
ANTHROPIC_SUBAGENT = "anthropic.claude-sonnet-5-5"
ANTHROPIC_TOOL = "anthropic.claude-haiku-5-5"
ANTHROPIC_UNASSIGNED = "anthropic.claude-opus-5-5"
OPENAI_SUPERVISOR = "openai.gpt-6.1-sol"
OPENAI_SUBAGENT = "openai.gpt-6.1-terra"
OPENAI_TOOL = "openai.gpt-6-luna"
OPENAI_UNASSIGNED = "openai.gpt-6-astra"

#: Every model the capability matrix declares with ``family: openai``, read from the file.
OPENAI_MODEL_IDS: tuple[str, ...] = tuple(
    model_id
    for model_id, entry in yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))[
        "models"
    ].items()
    if entry.get("family") == "openai"
)

ALL_MODELS = (
    ANTHROPIC_UNASSIGNED,
    ANTHROPIC_SUBAGENT,
    ANTHROPIC_TOOL,
    OPENAI_SUPERVISOR,
    OPENAI_SUBAGENT,
    OPENAI_TOOL,
    OPENAI_UNASSIGNED,
)


@pytest.fixture(scope="module")
def capabilities() -> dict[str, Any]:
    """The parsed capability matrix."""
    data = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def models(capabilities: dict[str, Any]) -> dict[str, Any]:
    """The per-model section of the capability matrix."""
    entries = capabilities["models"]
    assert isinstance(entries, dict)
    return entries


@pytest.fixture(scope="module")
def tier_config() -> dict[str, Any]:
    """The parsed tier-to-model assignment."""
    data = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# --------------------------------------------------------------------------------------
# Structural completeness
# --------------------------------------------------------------------------------------


def test_every_required_model_is_present(models: dict[str, Any]) -> None:
    """The matrix declares exactly both families' tier defaults plus the unassigned Opus and Astra."""
    assert set(models) == set(ALL_MODELS)


def test_opus_stays_a_selectable_unassigned_anthropic_model(
    models: dict[str, Any], tier_config: dict[str, Any]
) -> None:
    """Claude Opus 5.5 keeps its matrix entry, home tier supervisor, and no tier assigns it."""
    entry = models[ANTHROPIC_UNASSIGNED]
    assert entry["family"] == "anthropic"
    assert entry["tier"] == "supervisor"
    assert entry["inference_profile"] == "global.anthropic.claude-opus-5-5"
    assigned = {tier["model"] for tier in tier_config["families"]["anthropic"]["tiers"].values()}
    assert ANTHROPIC_UNASSIGNED not in assigned
    matrix = load_capability_matrix()
    assert ANTHROPIC_UNASSIGNED in matrix.models
    assert ANTHROPIC_UNASSIGNED not in {
        assignment.model_id for assignment in matrix.tiers["anthropic"].values()
    }


def test_astra_stays_a_selectable_unassigned_openai_model(
    models: dict[str, Any], tier_config: dict[str, Any]
) -> None:
    """GPT-6 Astra keeps its matrix entry, home tier supervisor, and no tier assigns it."""
    entry = models[OPENAI_UNASSIGNED]
    assert entry["family"] == "openai"
    assert entry["tier"] == "supervisor"
    assert entry["inference_profile"] == "global.openai.gpt-6-astra"
    assigned = {tier["model"] for tier in tier_config["families"]["openai"]["tiers"].values()}
    assert OPENAI_UNASSIGNED not in assigned


@pytest.mark.parametrize("model_id", ALL_MODELS)
def test_every_model_declares_the_required_fields(models: dict[str, Any], model_id: str) -> None:
    """Every model declares every required capability field."""
    entry = models[model_id]
    for field in ("family", "tier", "inference_profile", "api_path", "effort"):
        assert field in entry, f"{model_id} is missing {field!r}"
    for field in ("supported", "values", "default"):
        assert field in entry["effort"], f"{model_id}.effort is missing {field!r}"


@pytest.mark.parametrize("model_id", ALL_MODELS)
def test_inference_profile_uses_the_global_prefix(models: dict[str, Any], model_id: str) -> None:
    """Every model uses a `global.` inference profile and none uses `us.`."""
    profile = models[model_id]["inference_profile"]
    assert profile == f"global.{model_id}", f"{model_id} must use the global. inference profile"
    assert not profile.startswith("us."), "the `us.` US-residency prefix is not used"


# --------------------------------------------------------------------------------------
# Anthropic effort rulings
# --------------------------------------------------------------------------------------


def test_anthropic_effort_parameter_is_a_sibling_of_thinking(capabilities: dict[str, Any]) -> None:
    """Placing effort inside `thinking` returns a ValidationException."""
    anthropic = capabilities["effort_parameter"]["anthropic"]
    assert anthropic["path"] == "output_config.effort"
    assert anthropic["sibling_of"] == "thinking"


def test_opus_55_supports_xhigh_and_max_with_a_medium_default(models: dict[str, Any]) -> None:
    """Opus 5.5 offers the full low-to-max effort scale with a medium default."""
    values = models[ANTHROPIC_UNASSIGNED]["effort"]["values"]
    assert values == ["low", "medium", "high", "xhigh", "max"]
    assert models[ANTHROPIC_UNASSIGNED]["effort"]["default"] == "medium"


def test_opus_55_has_no_disabled_thinking_effort_cap(models: dict[str, Any]) -> None:
    """Opus 5.5 supports only adaptive thinking and declares no disabled-thinking effort cap."""
    effort = models[ANTHROPIC_UNASSIGNED]["effort"]
    assert effort.get("capped_at_when_thinking_disabled") is None
    thinking = models[ANTHROPIC_UNASSIGNED]["thinking"]
    assert thinking["supported_types"] == ["adaptive"]
    assert "disabled" not in thinking["supported_types"]


def test_sonnet_55_excludes_xhigh_and_max_as_disputed(models: dict[str, Any]) -> None:
    """Sonnet 5.5 excludes xhigh and max, recording both as disputed values."""
    effort = models[ANTHROPIC_SUBAGENT]["effort"]
    assert effort["values"] == ["low", "medium", "high"]
    assert "xhigh" not in effort["values"]
    assert "max" not in effort["values"]
    disputed = {item["value"] for item in effort["disputed_values"] if item["excluded"]}
    assert disputed == {"xhigh", "max"}


def test_sonnet_55_rejects_manual_extended_thinking(models: dict[str, Any]) -> None:
    """thinking.type "enabled" with budget_tokens returns a ValidationException on Sonnet 5.5."""
    supported = models[ANTHROPIC_SUBAGENT]["thinking"]["supported_types"]
    assert supported == ["adaptive", "disabled"]
    assert "enabled" not in supported


@pytest.mark.parametrize("model_id", [ANTHROPIC_UNASSIGNED, ANTHROPIC_SUBAGENT])
def test_adaptive_thinking_must_be_set_explicitly(models: dict[str, Any], model_id: str) -> None:
    """Omitting `thinking` selects adaptive and bills thinking tokens, distorting the comparison."""
    thinking = models[model_id]["thinking"]
    assert thinking["adaptive_default"] is True
    assert thinking["must_set_explicitly"] is True


# --------------------------------------------------------------------------------------
# Haiku 5.5
# --------------------------------------------------------------------------------------


def test_haiku_55_entry_records_every_d01_fact(models: dict[str, Any]) -> None:
    """Every Haiku 5.5 field equals its pinned value."""
    entry = models[ANTHROPIC_TOOL]
    assert entry["family"] == "anthropic"
    assert entry["tier"] == "tool"
    assert entry["inference_profile"] == "global.anthropic.claude-haiku-5-5"
    assert entry["api_path"] == "messages"
    assert entry["context_window_tokens"] == 1000000
    assert entry["max_output_tokens"] == 128000
    assert entry["prompt_cache_min_checkpoint_tokens"] == 512


def test_haiku_55_accepts_five_effort_rungs_with_a_medium_default(models: dict[str, Any]) -> None:
    """Haiku 5.5 accepts low through max, defaults to medium, and caps at high without thinking."""
    effort = models[ANTHROPIC_TOOL]["effort"]
    assert effort["supported"] is True
    assert effort["values"] == ["low", "medium", "high", "xhigh", "max"]
    assert effort["default"] == "medium"
    assert effort["capped_at_when_thinking_disabled"] == "high"
    assert "disputed_values" not in effort


def test_haiku_55_thinking_is_adaptive_by_default(models: dict[str, Any]) -> None:
    """Haiku 5.5 accepts adaptive and disabled thinking, adaptive by default, never `enabled`."""
    thinking = models[ANTHROPIC_TOOL]["thinking"]
    assert thinking["supported_types"] == ["adaptive", "disabled"]
    assert thinking["adaptive_default"] is True
    assert thinking["must_set_explicitly"] is False


def test_haiku_55_lifecycle_is_one_year_from_launch(models: dict[str, Any]) -> None:
    """Haiku 5.5 launched 2026-10-07 with an EOL no sooner than a year later, and no successor."""
    lifecycle = models[ANTHROPIC_TOOL]["lifecycle"]
    assert lifecycle == {"launched": "2026-10-07", "eol_no_sooner_than": "2027-10-07"}


def test_the_loaded_haiku_55_cap_equals_the_yaml() -> None:
    """The loader carries Haiku 5.5's disabled-thinking cap through to the capability."""
    effort = load_capability_matrix().models[ANTHROPIC_TOOL].effort
    assert effort.capped_at_when_thinking_disabled == "high"
    assert effort.supported is True


# --------------------------------------------------------------------------------------
# OpenAI effort rulings
# --------------------------------------------------------------------------------------


def test_gpt_61_sol_entry_records_every_d02_fact(models: dict[str, Any]) -> None:
    """Every GPT-6.1 Sol field equals its pinned value, its effort scale marked inferred."""
    entry = models[OPENAI_SUPERVISOR]
    assert entry["family"] == "openai"
    assert entry["tier"] == "supervisor"
    assert entry["inference_profile"] == "global.openai.gpt-6.1-sol"
    assert entry["api_path"] == "responses"
    assert entry["context_window_tokens"] == 1000000
    assert entry["max_output_tokens"] == 131072
    assert entry["prompt_cache_min_checkpoint_tokens"] == 1024
    effort = entry["effort"]
    assert effort["supported"] is True
    assert effort["values"] == ["none", "low", "medium", "high", "xhigh", "max"]
    assert effort["default"] == "medium"
    assert effort["confidence"] == "inferred"


def test_gpt_61_terra_entry_records_every_pinned_fact(models: dict[str, Any]) -> None:
    """Every GPT-6.1 Terra field equals its pinned value, effort scale and cache minimum inferred."""
    entry = models[OPENAI_SUBAGENT]
    assert entry["family"] == "openai"
    assert entry["tier"] == "subagent"
    assert entry["inference_profile"] == "global.openai.gpt-6.1-terra"
    assert entry["api_path"] == "responses"
    assert entry["context_window_tokens"] == 1050000
    assert entry["max_output_tokens"] == 128000
    assert entry["prompt_cache_min_checkpoint_tokens"] == 1024
    effort = entry["effort"]
    assert effort["supported"] is True
    assert effort["values"] == ["none", "low", "medium", "high", "xhigh", "max"]
    assert effort["default"] == "medium"
    assert effort["confidence"] == "inferred"


def test_the_terra_model_card_is_a_cited_source(capabilities: dict[str, Any]) -> None:
    """The provenance sources cite GPT-6.1 Terra's AWS model card."""
    sources = capabilities["provenance"]["sources"]
    assert any(source.endswith("/model-card-openai-gpt-6-1-terra.html") for source in sources)


def test_the_openai_model_set_covers_every_tier_and_the_unassigned_model() -> None:
    """The effort-scale tests below iterate the three tier models and Astra, which is unassigned."""
    assert set(OPENAI_MODEL_IDS) >= {
        OPENAI_SUPERVISOR,
        OPENAI_SUBAGENT,
        OPENAI_TOOL,
        OPENAI_UNASSIGNED,
    }


@pytest.mark.parametrize("model_id", OPENAI_MODEL_IDS)
def test_every_openai_model_offers_max(models: dict[str, Any], model_id: str) -> None:
    """Every OpenAI model lists max among its effort values."""
    assert "max" in models[model_id]["effort"]["values"], f"{model_id} must offer max"


@pytest.mark.parametrize("model_id", OPENAI_MODEL_IDS)
def test_no_openai_model_carries_a_disputed_values_block(
    models: dict[str, Any], model_id: str
) -> None:
    """No OpenAI model carries a disputed effort value."""
    assert "disputed_values" not in models[model_id]["effort"]


@pytest.mark.parametrize("model_id", OPENAI_MODEL_IDS)
def test_openai_default_effort_is_medium(models: dict[str, Any], model_id: str) -> None:
    """Every OpenAI model's default effort is medium (documented for Luna, inferred for the rest)."""
    assert models[model_id]["effort"]["default"] == "medium"


# --------------------------------------------------------------------------------------
# Prompt-cache thresholds
# --------------------------------------------------------------------------------------


def test_anthropic_cache_thresholds_per_tier(models: dict[str, Any]) -> None:
    """Every Anthropic tier checkpoints at 512 tokens."""
    for model_id in (ANTHROPIC_UNASSIGNED, ANTHROPIC_SUBAGENT, ANTHROPIC_TOOL):
        assert models[model_id]["prompt_cache_min_checkpoint_tokens"] == 512


# --------------------------------------------------------------------------------------
# Tier assignment agreement
# --------------------------------------------------------------------------------------


def test_tier_assignment_matches_fr_7_3(tier_config: dict[str, Any]) -> None:
    """Each family's tier slots name the expected model ids."""
    families = tier_config["families"]
    assert families["anthropic"]["tiers"]["supervisor"]["model"] == ANTHROPIC_SUPERVISOR
    assert families["anthropic"]["tiers"]["subagent"]["model"] == ANTHROPIC_SUBAGENT
    assert families["anthropic"]["tiers"]["tool"]["model"] == ANTHROPIC_TOOL
    assert families["openai"]["tiers"]["supervisor"]["model"] == OPENAI_SUPERVISOR
    assert families["openai"]["tiers"]["subagent"]["model"] == OPENAI_SUBAGENT
    assert families["openai"]["tiers"]["tool"]["model"] == OPENAI_TOOL


def test_every_tier_carries_its_default_effort(tier_config: dict[str, Any]) -> None:
    """Both families run supervisor at effort medium and subagent and tool at effort low."""
    expected = {"supervisor": "medium", "subagent": "low", "tool": "low"}
    for family in ("anthropic", "openai"):
        tiers = tier_config["families"][family]["tiers"]
        for tier, effort in expected.items():
            assert tiers[tier]["tuning"] == {"effort": effort}, f"{family}/{tier}"


def test_openai_is_the_default_family(tier_config: dict[str, Any]) -> None:
    """OpenAI is the default family."""
    assert tier_config["default_family"] == "openai"


def test_endpoint_is_bedrock_runtime_not_mantle(tier_config: dict[str, Any]) -> None:
    """The configured endpoint is bedrock-runtime in us-east-1."""
    assert tier_config["endpoint"] == "bedrock-runtime"
    assert tier_config["region"] == "us-east-1"


def test_every_configured_default_tuning_is_valid_for_its_model(
    tier_config: dict[str, Any], models: dict[str, Any]
) -> None:
    """Every configured default tuning names only effort, at a value its model accepts."""
    for family_name, family in tier_config["families"].items():
        for tier_name, tier in family["tiers"].items():
            model_id = tier["model"]
            entry = models[model_id]
            assert entry["family"] == family_name
            assignable = entry.get("assignable_tiers", [entry["tier"]])
            assert tier_name in assignable, f"{model_id} is not assignable to {tier_name}"
            tuning = tier.get("tuning", {})
            assert set(tuning) <= {"effort"}, f"{family_name}/{tier_name} tuning {tuning!r}"
            if "effort" in tuning:
                assert entry["effort"]["supported"] is True, (
                    f"{family_name}/{tier_name} sets effort but {model_id} does not accept it"
                )
                assert tuning["effort"] in entry["effort"]["values"], (
                    f"{family_name}/{tier_name} effort {tuning['effort']!r} "
                    f"is not valid for {model_id}"
                )


def test_no_tier_sends_effort_to_a_model_that_forbids_it(
    tier_config: dict[str, Any], models: dict[str, Any]
) -> None:
    """No tier configures effort for a model that does not support it."""
    for family in tier_config["families"].values():
        for tier in family["tiers"].values():
            entry = models[tier["model"]]
            if entry["effort"]["supported"] is False:
                assert "effort" not in tier.get("tuning", {}), (
                    f"{tier['model']} forbids effort but the tier configures it"
                )


# --------------------------------------------------------------------------------------
# Cross-family effort-label comparability
# --------------------------------------------------------------------------------------

TIER_NAMES = ("supervisor", "subagent", "tool")


def test_effort_scale_comparability_block_is_present_and_denies_equivalence(
    capabilities: dict[str, Any], tier_config: dict[str, Any]
) -> None:
    """The block denies label equivalence and compares, per tier, each family's model for it."""
    block = capabilities["effort_scale_comparability"]
    assert block["labels_are_equivalent_across_families"] is False
    assert "compared_supervisor_models" not in block
    statement = block["statement"]
    assert "not a claim of equivalent reasoning budget" in statement
    assert "reproducibility" in statement
    compared = block["compared_models"]
    assert set(compared) == set(TIER_NAMES)
    for tier in TIER_NAMES:
        expected = {family["tiers"][tier]["model"] for family in tier_config["families"].values()}
        assert set(compared[tier]) == expected, f"{tier} compares the wrong models"
        for model_id in expected:
            assert model_id in statement, f"the statement does not name {model_id}"


@pytest.mark.parametrize("tier", TIER_NAMES)
def test_effort_scale_comparability_rungs_match_the_models_own_declared_scales(
    capabilities: dict[str, Any], models: dict[str, Any], tier: str
) -> None:
    """Each compared model's rungs and default in the block equal its own declared effort scale."""
    block = capabilities["effort_scale_comparability"]["compared_models"][tier]
    for model_id, declared in block.items():
        entry = models[model_id]["effort"]
        assert declared["rungs"] == entry["values"], f"{model_id} rungs drifted from effort.values"
        assert declared["default"] == entry["default"], f"{model_id} default drifted"


@pytest.mark.parametrize("tier", TIER_NAMES)
def test_the_two_compared_effort_scales_of_each_tier_are_genuinely_different(
    tier_config: dict[str, Any], models: dict[str, Any], tier: str
) -> None:
    """Per tier, the two families' scales differ in rung count and in where the pinned label sits."""
    families = tier_config["families"]
    anthropic_tier = families["anthropic"]["tiers"][tier]
    openai_tier = families["openai"]["tiers"][tier]
    anthropic = models[anthropic_tier["model"]]["effort"]["values"]
    openai = models[openai_tier["model"]]["effort"]["values"]
    assert len(anthropic) != len(openai)
    label = anthropic_tier["tuning"]["effort"]
    assert label == openai_tier["tuning"]["effort"]
    assert anthropic.index(label) != openai.index(label)


# --------------------------------------------------------------------------------------
# API path: Messages for every Anthropic model, Responses for every OpenAI model
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", ALL_MODELS)
def test_each_family_uses_its_one_vendor_api(models: dict[str, Any], model_id: str) -> None:
    """Anthropic models use the Messages API and OpenAI models the Responses API."""
    expected = {"anthropic": "messages", "openai": "responses"}[models[model_id]["family"]]
    assert models[model_id]["api_path"] == expected


def test_the_accepted_api_paths_are_the_two_vendor_apis() -> None:
    """The loader's accepted set is exactly the two vendor APIs."""
    assert sorted(API_PATHS) == ["messages", "responses"]


def _write_capabilities_with_haiku_api_path(tmp_path: Path, api_path: object) -> Path:
    """Copy the real capability matrix with Haiku's ``api_path`` replaced (or removed for None)."""
    data = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    entry = data["models"][ANTHROPIC_TOOL]
    if api_path is None:
        del entry["api_path"]
    else:
        entry["api_path"] = api_path
    path = tmp_path / "model_capabilities.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_loader_accepts_an_unmutated_copy_of_the_matrix(tmp_path: Path) -> None:
    """Control for the rejection tests below: the round-tripped copy itself loads cleanly."""
    path = _write_capabilities_with_haiku_api_path(tmp_path, "messages")
    matrix = load_capability_matrix(capabilities_path=path)
    assert ANTHROPIC_TOOL in matrix.models


@pytest.mark.parametrize("api_path", ["converse_stream", None, "invoke"])
def test_loader_rejects_an_api_path_outside_the_accepted_set(
    tmp_path: Path, api_path: object
) -> None:
    """A missing or unknown ``api_path`` fails at load time, naming the model and the value."""
    path = _write_capabilities_with_haiku_api_path(tmp_path, api_path)
    with pytest.raises(ValueError, match="api_path") as exc_info:
        load_capability_matrix(capabilities_path=path)
    assert ANTHROPIC_TOOL in str(exc_info.value)
    assert repr(api_path) in str(exc_info.value)


def test_validate_tuning_accepts_effort_low_for_haiku_55() -> None:
    """The tool tier's pinned effort resolves on Haiku 5.5; a rung it lacks is refused."""
    haiku = load_capability_matrix().models[ANTHROPIC_TOOL]
    assert validate_tuning(haiku, Tuning(source="--effort", effort="low")).effort == "low"
    with pytest.raises(ValidationFailed, match="does not accept effort='none'") as exc_info:
        validate_tuning(haiku, Tuning(source="--effort", effort="none"))
    assert exc_info.value.source == "--effort"


# --------------------------------------------------------------------------------------
# Temperature is not a capability
# --------------------------------------------------------------------------------------


def test_the_temperature_capability_type_is_deleted() -> None:
    """Neither the capability type nor a `temperature` field on a model entry exists."""
    assert not hasattr(capabilities_module, "TemperatureCapability")
    assert "temperature" not in ModelCapability.__slots__


def _write_capabilities_with_model_temperature(tmp_path: Path, model_id: str) -> Path:
    data = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    data["models"][model_id]["temperature"] = {"supported": False}
    path = tmp_path / "model_capabilities.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("model_id", ALL_MODELS)
def test_loader_rejects_a_temperature_key_in_any_model_entry(tmp_path: Path, model_id: str) -> None:
    """A `temperature` block in a model entry fails at load time, naming the model."""
    path = _write_capabilities_with_model_temperature(tmp_path, model_id)
    with pytest.raises(ValueError, match="temperature") as exc_info:
        load_capability_matrix(capabilities_path=path)
    assert model_id in str(exc_info.value)


def test_loader_rejects_a_temperature_in_a_tiers_default_tuning(tmp_path: Path) -> None:
    """A `temperature` in a models.yaml tier tuning fails at load time, naming the tier."""
    data = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
    data["families"]["anthropic"]["tiers"]["tool"]["tuning"]["temperature"] = 0.2
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="temperature") as exc_info:
        load_capability_matrix(models_path=path)
    assert "anthropic/tool" in str(exc_info.value)


def test_loader_accepts_an_unmutated_copy_of_models_yaml(tmp_path: Path) -> None:
    """Control for the tier-tuning rejection: the round-tripped copy itself loads cleanly."""
    path = tmp_path / "models.yaml"
    path.write_text(MODELS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    matrix = load_capability_matrix(models_path=path)
    assert matrix.tiers["anthropic"]["tool"].tuning == {"effort": "low"}


def _write_models_with_openai_tier(tmp_path: Path, tier: str, model_id: str) -> Path:
    data = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
    data["families"]["openai"]["tiers"][tier]["model"] = model_id
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_loader_rejects_a_tier_model_absent_from_the_matrix(tmp_path: Path) -> None:
    """A tier naming a model the matrix lacks fails at load time, naming family, tier and model."""
    path = _write_models_with_openai_tier(tmp_path, "subagent", "openai.gpt-6-nova")
    with pytest.raises(ValueError, match="not in the capability matrix") as exc_info:
        load_capability_matrix(models_path=path)
    assert "openai/subagent" in str(exc_info.value)
    assert "openai.gpt-6-nova" in str(exc_info.value)


def test_loader_rejects_a_tier_model_of_another_family(tmp_path: Path) -> None:
    """An openai tier naming an anthropic model fails at load time, naming both families."""
    path = _write_models_with_openai_tier(tmp_path, "tool", ANTHROPIC_TOOL)
    with pytest.raises(ValueError, match="is of family 'anthropic', not 'openai'") as exc_info:
        load_capability_matrix(models_path=path)
    assert "openai/tool" in str(exc_info.value)
    assert ANTHROPIC_TOOL in str(exc_info.value)


def test_loader_rejects_a_tier_model_whose_capability_tier_differs(tmp_path: Path) -> None:
    """Assigning Astra (home tier supervisor) to the subagent tier fails, naming both tiers."""
    path = _write_models_with_openai_tier(tmp_path, "subagent", OPENAI_UNASSIGNED)
    with pytest.raises(
        ValueError, match=r"is assignable only to \['supervisor'\], not 'subagent'"
    ) as exc_info:
        load_capability_matrix(models_path=path)
    assert "openai/subagent" in str(exc_info.value)
    assert OPENAI_UNASSIGNED in str(exc_info.value)


def test_loader_accepts_an_unassigned_model_and_its_home_tier_assignment(tmp_path: Path) -> None:
    """Astra loads while no tier assigns it, and also when assigned to its home tier."""
    matrix = load_capability_matrix()
    assert matrix.models[OPENAI_UNASSIGNED].tier == "supervisor"
    assert OPENAI_UNASSIGNED not in {
        assignment.model_id for assignment in matrix.tiers["openai"].values()
    }
    path = _write_models_with_openai_tier(tmp_path, "supervisor", OPENAI_UNASSIGNED)
    reassigned = load_capability_matrix(models_path=path)
    assert reassigned.model_for("openai", "supervisor").model_id == OPENAI_UNASSIGNED


def test_the_loaded_openai_tiers_resolve_to_sol_terra_luna() -> None:
    """The openai tier defaults resolve to the interface every consumer reads."""
    matrix = load_capability_matrix()
    resolved = {
        tier: (assignment.model_id, assignment.tuning)
        for tier, assignment in matrix.tiers["openai"].items()
    }
    assert resolved == {
        "supervisor": (OPENAI_SUPERVISOR, {"effort": "medium"}),
        "subagent": (OPENAI_SUBAGENT, {"effort": "low"}),
        "tool": (OPENAI_TOOL, {"effort": "low"}),
    }


def test_the_loaded_anthropic_tiers_resolve_to_sonnet_sonnet_haiku() -> None:
    """The anthropic tier defaults resolve to the interface every consumer reads."""
    matrix = load_capability_matrix()
    resolved = {
        tier: (assignment.model_id, assignment.tuning)
        for tier, assignment in matrix.tiers["anthropic"].items()
    }
    assert resolved == {
        "supervisor": (ANTHROPIC_SUPERVISOR, {"effort": "medium"}),
        "subagent": (ANTHROPIC_SUBAGENT, {"effort": "low"}),
        "tool": (ANTHROPIC_TOOL, {"effort": "low"}),
    }


# --------------------------------------------------------------------------------------
# Assignable tiers
# --------------------------------------------------------------------------------------


def test_only_sonnet_declares_more_than_its_home_tier() -> None:
    """Sonnet 5.5 is assignable to supervisor and subagent; every other model only to its home."""
    for model_id, model in load_capability_matrix().models.items():
        if model_id == ANTHROPIC_SUBAGENT:
            assert model.tier == "subagent"
            assert model.assignable_tiers == ("supervisor", "subagent")
        else:
            assert model.assignable_tiers == (model.tier,), model_id


def test_every_assigned_model_is_assignable_to_its_tier() -> None:
    """Every default assignment names a model whose assignable tiers include that tier."""
    matrix = load_capability_matrix()
    for family, tiers in matrix.tiers.items():
        for tier, assignment in tiers.items():
            model = matrix.models[assignment.model_id]
            assert tier in model.assignable_tiers, f"{family}/{tier}: {assignment.model_id}"


def _write_capabilities_with_assignable(tmp_path: Path, model_id: str, value: object) -> Path:
    """Copy the real matrix with ``model_id``'s ``assignable_tiers`` set (or removed for None)."""
    data = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    entry = data["models"][model_id]
    if value is None:
        entry.pop("assignable_tiers", None)
    else:
        entry["assignable_tiers"] = value
    path = tmp_path / "model_capabilities.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _write_models_with_anthropic_tier(tmp_path: Path, tier: str, model_id: str) -> Path:
    data = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
    data["families"]["anthropic"]["tiers"][tier]["model"] = model_id
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_loader_accepts_sonnet_at_the_supervisor_tier() -> None:
    """The shipped config assigns Sonnet 5.5, home tier subagent, to the supervisor tier."""
    matrix = load_capability_matrix()
    assert matrix.model_for("anthropic", "supervisor").model_id == ANTHROPIC_SUBAGENT
    assert matrix.models[ANTHROPIC_SUBAGENT].tier == "subagent"


def test_loader_rejects_sonnet_at_supervisor_without_its_assignable_tiers(tmp_path: Path) -> None:
    """With the key removed, Sonnet defaults to its home tier and the supervisor assignment fails."""
    path = _write_capabilities_with_assignable(tmp_path, ANTHROPIC_SUBAGENT, None)
    with pytest.raises(ValueError, match=r"assignable only to \['subagent'\], not 'supervisor'"):
        load_capability_matrix(capabilities_path=path)


def test_loader_rejects_haiku_at_the_anthropic_supervisor_tier(tmp_path: Path) -> None:
    """Assigning Haiku 5.5 (home tier tool) to the anthropic supervisor tier fails."""
    path = _write_models_with_anthropic_tier(tmp_path, "supervisor", ANTHROPIC_TOOL)
    with pytest.raises(
        ValueError, match=r"is assignable only to \['tool'\], not 'supervisor'"
    ) as exc_info:
        load_capability_matrix(models_path=path)
    assert "anthropic/supervisor" in str(exc_info.value)
    assert ANTHROPIC_TOOL in str(exc_info.value)


def test_an_absent_assignable_tiers_equals_the_home_tier(tmp_path: Path) -> None:
    """A model entry without the key loads with ``assignable_tiers == (tier,)``."""
    data = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    assert "assignable_tiers" not in data["models"][ANTHROPIC_TOOL]
    path = _write_capabilities_with_assignable(tmp_path, ANTHROPIC_TOOL, ["tool"])
    explicit = load_capability_matrix(capabilities_path=path).models[ANTHROPIC_TOOL]
    implicit = load_capability_matrix().models[ANTHROPIC_TOOL]
    assert implicit.assignable_tiers == explicit.assignable_tiers == ("tool",)


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (["supervisor"], "omits the home tier 'subagent'"),
        (["supervisor", "subagent", "planner"], r"unknown tiers \['planner'\]"),
        (["supervisor", "subagent", "subagent"], "contains a duplicate"),
        ([], "must be a non-empty list"),
        ("subagent", "must be a non-empty list"),
    ],
    ids=["missing-home", "unknown-tier", "duplicate", "empty", "non-list"],
)
def test_loader_rejects_a_malformed_assignable_tiers(
    tmp_path: Path, value: object, match: str
) -> None:
    """A malformed ``assignable_tiers`` fails at load time, naming the model."""
    path = _write_capabilities_with_assignable(tmp_path, ANTHROPIC_SUBAGENT, value)
    with pytest.raises(ValueError, match=match) as exc_info:
        load_capability_matrix(capabilities_path=path)
    assert ANTHROPIC_SUBAGENT in str(exc_info.value)


def test_a_config_only_edit_makes_opus_and_astra_the_supervisor_defaults(tmp_path: Path) -> None:
    """Making Opus 5.5 and GPT-6 Astra the supervisor defaults is three YAML edits, no code change."""
    data = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
    data["families"]["anthropic"]["tiers"]["supervisor"]["model"] = ANTHROPIC_UNASSIGNED
    data["families"]["openai"]["tiers"]["supervisor"]["model"] = OPENAI_UNASSIGNED
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    matrix = load_capability_matrix(models_path=path)
    assert matrix.model_for("anthropic", "supervisor").model_id == ANTHROPIC_UNASSIGNED
    assert matrix.model_for("openai", "supervisor").model_id == OPENAI_UNASSIGNED


# --------------------------------------------------------------------------------------
# Inference profiles, context windows and per-tier request prompt limits
# --------------------------------------------------------------------------------------

TIERS: tuple[str, ...] = ("supervisor", "subagent", "tool")
EXPECTED_REQUEST_PROMPT_LIMITS: dict[str, int] = {
    "supervisor": 270_000,
    "subagent": 270_000,
    "tool": 95_000,
}


def _write_models_with_limits(tmp_path: Path, limits: object) -> Path:
    """Copy the real ``models.yaml`` with ``request_prompt_limits`` replaced (removed for None)."""
    data = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
    if limits is None:
        del data["request_prompt_limits"]
    else:
        data["request_prompt_limits"] = limits
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _write_capabilities_with_model_field(
    tmp_path: Path, model_id: str, field: str, value: object
) -> Path:
    """Copy the real matrix with one model's ``field`` set (removed for None)."""
    data = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    entry = data["models"][model_id]
    if value is None:
        del entry[field]
    else:
        entry[field] = value
    path = tmp_path / "model_capabilities.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_the_loaded_request_prompt_limits_match_the_configured_per_tier_values() -> None:
    """Supervisor 270,000, subagent 270,000 and tool 95,000, each read through its accessor."""
    matrix = load_capability_matrix()
    assert dict(matrix.request_prompt_limits) == EXPECTED_REQUEST_PROMPT_LIMITS
    for tier, limit in EXPECTED_REQUEST_PROMPT_LIMITS.items():
        assert matrix.request_prompt_limit(tier) == limit  # type: ignore[arg-type]


def test_the_limits_are_read_from_models_yaml(tier_config: dict[str, Any]) -> None:
    assert tier_config["request_prompt_limits"] == EXPECTED_REQUEST_PROMPT_LIMITS


def test_model_capabilities_yaml_carries_no_prompt_limit_field() -> None:
    """The limit is a tier setting; no model entry or top-level key in the matrix carries one."""
    assert "prompt_limit" not in CAPABILITIES_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("tier", TIERS)
def test_changing_one_tier_limit_moves_only_that_tier(tmp_path: Path, tier: str) -> None:
    """Each tier key is independent: editing one leaves the other two at their configured values."""
    limits = dict(EXPECTED_REQUEST_PROMPT_LIMITS)
    limits[tier] = 12_345
    matrix = load_capability_matrix(models_path=_write_models_with_limits(tmp_path, limits))
    for other in TIERS:
        expected = 12_345 if other == tier else EXPECTED_REQUEST_PROMPT_LIMITS[other]
        assert matrix.request_prompt_limit(other) == expected  # type: ignore[arg-type]


def test_an_unmutated_copy_of_the_limits_loads(tmp_path: Path) -> None:
    """Control for the rejections below."""
    path = _write_models_with_limits(tmp_path, dict(EXPECTED_REQUEST_PROMPT_LIMITS))
    assert dict(load_capability_matrix(models_path=path).request_prompt_limits) == (
        EXPECTED_REQUEST_PROMPT_LIMITS
    )


@pytest.mark.parametrize(
    ("limits", "match"),
    [
        (None, "must be a mapping"),
        ([270_000, 270_000, 95_000], "must be a mapping"),
        ({"supervisor": 270_000, "subagent": 270_000}, r"missing \['tool'\]"),
        ({"supervisor": 270_000, "tool": 95_000}, r"missing \['subagent'\]"),
        ({"subagent": 270_000, "tool": 95_000}, r"missing \['supervisor'\]"),
        (
            {"supervisor": 270_000, "subagent": 270_000, "tool": 95_000, "planner": 1},
            r"unknown \['planner'\]",
        ),
        ({"supervisor": 0, "subagent": 270_000, "tool": 95_000}, "supervisor 0 is not a positive"),
        ({"supervisor": 270_000, "subagent": -1, "tool": 95_000}, "subagent -1 is not a positive"),
        ({"supervisor": 270_000, "subagent": 270_000, "tool": True}, "tool True is not a positive"),
        (
            {"supervisor": "270000", "subagent": 270_000, "tool": 95_000},
            "supervisor '270000' is not a positive",
        ),
        ({"supervisor": 270_000.0, "subagent": 270_000, "tool": 95_000}, "is not a positive"),
    ],
    ids=[
        "absent",
        "non-mapping",
        "missing-tool",
        "missing-subagent",
        "missing-supervisor",
        "extra-key",
        "zero",
        "negative",
        "bool",
        "string",
        "float",
    ],
)
def test_loader_rejects_a_malformed_request_prompt_limits(
    tmp_path: Path, limits: object, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        load_capability_matrix(models_path=_write_models_with_limits(tmp_path, limits))


def test_a_tier_limit_above_a_models_window_is_refused(tmp_path: Path) -> None:
    limits = {**EXPECTED_REQUEST_PROMPT_LIMITS, "supervisor": 1_000_001}
    with pytest.raises(ValueError, match=r"supervisor 1000001 exceeds the 1000000-token"):
        load_capability_matrix(models_path=_write_models_with_limits(tmp_path, limits))


def test_a_limit_equal_to_the_smallest_window_loads(tmp_path: Path) -> None:
    """The window bound is inclusive."""
    smallest = min(m.context_window_tokens or 0 for m in load_capability_matrix().models.values())
    limits = {**EXPECTED_REQUEST_PROMPT_LIMITS, "subagent": smallest}
    matrix = load_capability_matrix(models_path=_write_models_with_limits(tmp_path, limits))
    assert matrix.request_prompt_limit("subagent") == smallest


@pytest.mark.parametrize(
    ("model_id", "tier"),
    [
        (ANTHROPIC_UNASSIGNED, "supervisor"),
        (OPENAI_UNASSIGNED, "supervisor"),
        (ANTHROPIC_TOOL, "supervisor"),
        (OPENAI_TOOL, "subagent"),
    ],
    ids=["opus-selectable", "astra-selectable", "haiku-user-selected", "luna-user-selected"],
)
def test_a_selectable_model_with_a_window_below_a_tier_limit_is_refused(
    tmp_path: Path, model_id: str, tier: str
) -> None:
    """A model a turn may select for the tier, default or not, must fit that tier's limit.

    Only ``tier`` carries a limit above the shrunken window, so the refusal names that tier.
    """
    path = _write_capabilities_with_model_field(
        tmp_path, model_id, "context_window_tokens", 200_000
    )
    limits = {name: 270_000 if name == tier else 95_000 for name in TIERS}
    with pytest.raises(ValueError, match=rf"{tier} 270000 exceeds the 200000-token") as exc_info:
        load_capability_matrix(
            models_path=_write_models_with_limits(tmp_path, limits), capabilities_path=path
        )
    assert model_id in str(exc_info.value)


def test_a_window_between_the_tool_and_supervisor_limits_is_still_refused(tmp_path: Path) -> None:
    """Haiku's window at 100,000 fits the tool limit but not the supervisor limit a user can pick."""
    path = _write_capabilities_with_model_field(
        tmp_path, ANTHROPIC_TOOL, "context_window_tokens", 100_000
    )
    with pytest.raises(ValueError, match="270000 exceeds the 100000-token"):
        load_capability_matrix(capabilities_path=path)


@pytest.mark.parametrize("model_id", ALL_MODELS)
def test_every_model_loads_its_context_window_from_the_yaml(
    models: dict[str, Any], model_id: str
) -> None:
    loaded = load_capability_matrix().models[model_id].context_window_tokens
    assert loaded == models[model_id]["context_window_tokens"]
    assert isinstance(loaded, int) and loaded > 0


@pytest.mark.parametrize(
    "window", [None, 0, -5, True, "1000000"], ids=["missing", "zero", "negative", "bool", "string"]
)
def test_loader_rejects_a_missing_or_non_positive_context_window(
    tmp_path: Path, window: object
) -> None:
    path = _write_capabilities_with_model_field(
        tmp_path, OPENAI_TOOL, "context_window_tokens", window
    )
    with pytest.raises(ValueError, match="context_window_tokens") as exc_info:
        load_capability_matrix(capabilities_path=path)
    assert OPENAI_TOOL in str(exc_info.value)


@pytest.mark.parametrize(
    ("profile", "match"),
    [
        ("anthropic.claude-haiku-5-5", "no recognised route prefix"),
        ("xx.anthropic.claude-haiku-5-5", "no recognised route prefix"),
        ("", "empty"),
        ("global.anthropic.claude-sonnet-5-5", "does not name model"),
        ("us.anthropic.claude-sonnet-5-5", "does not name model"),
        (42, "is not a string"),
    ],
    ids=["bare-id", "unknown-prefix", "empty", "global-other-model", "geo-other-model", "non-str"],
)
def test_loader_rejects_an_inference_profile_without_a_route_to_its_model(
    tmp_path: Path, profile: object, match: str
) -> None:
    path = _write_capabilities_with_model_field(
        tmp_path, ANTHROPIC_TOOL, "inference_profile", profile
    )
    with pytest.raises(ValueError, match=match) as exc_info:
        load_capability_matrix(capabilities_path=path)
    assert ANTHROPIC_TOOL in str(exc_info.value)


@pytest.mark.parametrize("prefix", ["us.", "eu.", "apac.", "ca.", "jp.", "au."])
def test_loader_accepts_a_geo_inference_profile(tmp_path: Path, prefix: str) -> None:
    profile = f"{prefix}{ANTHROPIC_TOOL}"
    path = _write_capabilities_with_model_field(
        tmp_path, ANTHROPIC_TOOL, "inference_profile", profile
    )
    assert load_capability_matrix(capabilities_path=path).models[
        ANTHROPIC_TOOL
    ].inference_profile == (profile)
