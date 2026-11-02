# SPDX-License-Identifier: MIT
"""Load and query the per-model capability matrix.

Reads ``config/model_capabilities.yaml`` and ``config/models.yaml`` and answers which model a tier
resolves to and which tuning values that model accepts. Every per-model fact comes from the YAML.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, get_args

import yaml

Family = Literal["anthropic", "openai"]
Tier = Literal["supervisor", "subagent", "tool"]
_TIER_NAMES: Final[tuple[str, ...]] = get_args(Tier)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEFAULT_MODELS_PATH: Final[Path] = _REPO_ROOT / "config" / "models.yaml"
_DEFAULT_CAPABILITIES_PATH: Final[Path] = _REPO_ROOT / "config" / "model_capabilities.yaml"

#: The ``api_path`` values a model entry may declare: the Anthropic Messages API and the OpenAI
#: Responses API.
API_PATHS: Final[frozenset[str]] = frozenset({"messages", "responses"})


@dataclass(frozen=True, slots=True)
class EffortCapability:
    """What a model accepts for the effort tuning axis.

    Attributes:
        supported: Whether the model accepts an effort parameter at all.
        values: Effort values the model accepts, excluding any disputed-and-excluded value.
        default: The model's default effort value, or ``None`` when effort is unsupported.
        capped_at_when_thinking_disabled: The highest effort value still valid once thinking is
            explicitly disabled, or ``None`` when no such cap applies.
    """

    supported: bool
    values: tuple[str, ...]
    default: str | None
    capped_at_when_thinking_disabled: str | None


@dataclass(frozen=True, slots=True)
class ModelCapability:
    """The full capability entry for one model, as read from the matrix.

    Attributes:
        model_id: The Bedrock model identifier, recorded in provenance and priced against
            ``config/pricing.yaml``.
        inference_profile: The cross-region inference profile identifier a request addresses.
            Adapters send this; artifacts report ``model_id``.
        family: Provider family this model belongs to.
        tier: The model's home tier, which orders models in the tuning UI. It need not equal a
            tier ``config/models.yaml`` assigns the model to.
        effort: Effort-axis capability, the only tuning axis.
        assignable_tiers: The tiers ``config/models.yaml`` may assign this model to as a default;
            always contains ``tier``. Defaults to ``(tier,)`` when the YAML entry omits the key.
    """

    model_id: str
    inference_profile: str
    family: Family
    tier: Tier
    effort: EffortCapability
    assignable_tiers: tuple[Tier, ...]


@dataclass(frozen=True, slots=True)
class TierAssignment:
    """A single family's model and default tuning for one tier, as read from ``models.yaml``.

    Attributes:
        model_id: The model this family assigns to the tier.
        tuning: The default tuning values configured for this tier.
    """

    model_id: str
    tuning: dict[str, str]


@dataclass(frozen=True, slots=True)
class CapabilityMatrix:
    """The loaded, queryable capability matrix and tier assignment.

    Attributes:
        default_family: The provider family a tier uses when its request names none.
        models: Every model's capability entry, keyed by model id.
        tiers: Per-family, per-tier model assignment and default tuning.
    """

    default_family: Family
    models: dict[str, ModelCapability]
    tiers: dict[Family, dict[Tier, TierAssignment]]

    def model_for(self, family: Family, tier: Tier) -> ModelCapability:
        """Return the capability entry for the model assigned to ``family``'s ``tier``."""
        model_id = self.tiers[family][tier].model_id
        return self.models[model_id]


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML file into a mapping, raising if the top level is not a mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def _check_api_path(model_id: str, entry: dict[str, Any]) -> None:
    """Raise ``ValueError`` unless one model's raw ``api_path`` is a member of :data:`API_PATHS`."""
    api_path = entry.get("api_path")
    if api_path not in API_PATHS:
        raise ValueError(f"{model_id}: api_path {api_path!r} is not one of {sorted(API_PATHS)}")


def _parse_effort(entry: dict[str, Any]) -> EffortCapability:
    """Build an :class:`EffortCapability` from one model's raw ``effort`` block."""
    effort = entry["effort"]
    values = tuple(effort["values"])
    return EffortCapability(
        supported=bool(effort["supported"]),
        values=values,
        default=effort["default"],
        capped_at_when_thinking_disabled=effort.get("capped_at_when_thinking_disabled"),
    )


def _parse_assignable_tiers(model_id: str, entry: dict[str, Any]) -> tuple[Tier, ...]:
    """Return one model's ``assignable_tiers``, defaulting to its home ``tier`` when absent.

    Raises:
        ValueError: When the key is present but is not a non-empty, duplicate-free list of known
            tiers that contains the home tier.
    """
    home = entry["tier"]
    if "assignable_tiers" not in entry:
        return (home,)
    raw = entry["assignable_tiers"]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{model_id}: assignable_tiers must be a non-empty list, got {raw!r}")
    unknown = [tier for tier in raw if tier not in _TIER_NAMES]
    if unknown:
        raise ValueError(f"{model_id}: assignable_tiers names unknown tiers {unknown!r}")
    if len(set(raw)) != len(raw):
        raise ValueError(f"{model_id}: assignable_tiers {raw!r} contains a duplicate")
    if home not in raw:
        raise ValueError(f"{model_id}: assignable_tiers {raw!r} omits the home tier {home!r}")
    return tuple(raw)


def _reject_temperature(owner: str, block: dict[str, Any]) -> None:
    """Raise ``ValueError`` when a configuration block carries a ``temperature`` key."""
    if "temperature" in block:
        raise ValueError(f"{owner}: temperature is not a tuning parameter; remove the key")


def _check_assignment(
    family: str, tier: str, model_id: str, models: dict[str, ModelCapability]
) -> None:
    """Raise ``ValueError`` unless ``model_id`` is a ``family`` matrix model assignable to ``tier``."""
    model = models.get(model_id)
    if model is None:
        raise ValueError(f"{family}/{tier}: model {model_id!r} is not in the capability matrix")
    if model.family != family:
        raise ValueError(
            f"{family}/{tier}: model {model_id!r} is of family {model.family!r}, not {family!r}"
        )
    if tier not in model.assignable_tiers:
        raise ValueError(
            f"{family}/{tier}: model {model_id!r} is assignable only to "
            f"{list(model.assignable_tiers)!r}, not {tier!r}"
        )


def load_capability_matrix(
    *,
    models_path: Path = _DEFAULT_MODELS_PATH,
    capabilities_path: Path = _DEFAULT_CAPABILITIES_PATH,
) -> CapabilityMatrix:
    """Load and parse the tier assignment and capability matrix from their YAML sources.

    Args:
        models_path: Path to ``config/models.yaml``. Overridable for tests.
        capabilities_path: Path to ``config/model_capabilities.yaml``. Overridable for tests.

    Returns:
        A fully parsed, immutable :class:`CapabilityMatrix`.

    Raises:
        ValueError: When a model entry's ``api_path`` is absent or not in :data:`API_PATHS`, or a
            model entry or a tier's ``tuning`` carries a ``temperature`` key, or a model's
            ``assignable_tiers`` is malformed, or a tier's model is absent from the matrix, of
            another family, or not assignable to that tier.
    """
    models_config = _load_yaml(models_path)
    capabilities_config = _load_yaml(capabilities_path)

    raw_models = capabilities_config["models"]
    models: dict[str, ModelCapability] = {}
    for model_id, entry in raw_models.items():
        _check_api_path(model_id, entry)
        _reject_temperature(model_id, entry)
        models[model_id] = ModelCapability(
            model_id=model_id,
            inference_profile=entry["inference_profile"],
            family=entry["family"],
            tier=entry["tier"],
            effort=_parse_effort(entry),
            assignable_tiers=_parse_assignable_tiers(model_id, entry),
        )

    tiers: dict[Family, dict[Tier, TierAssignment]] = {}
    for family_name, family_block in models_config["families"].items():
        family_tiers: dict[Tier, TierAssignment] = {}
        for tier_name, tier_block in family_block["tiers"].items():
            tuning = dict(tier_block.get("tuning", {}))
            _reject_temperature(f"{family_name}/{tier_name} tuning", tuning)
            _check_assignment(family_name, tier_name, tier_block["model"], models)
            family_tiers[tier_name] = TierAssignment(
                model_id=tier_block["model"],
                tuning=tuning,
            )
        tiers[family_name] = family_tiers

    return CapabilityMatrix(
        default_family=models_config["default_family"],
        models=models,
        tiers=tiers,
    )
