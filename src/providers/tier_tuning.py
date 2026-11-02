# SPDX-License-Identifier: MIT
"""Per-tier tuning: the schema-3 wire block, its parsers, and per-tier resolution.

A :class:`TurnTuning` carries one optional :class:`TierRequest` per tier. :func:`parse_wire_tuning`
decodes the wire block strictly and is used by every write and every turn;
:func:`parse_stored_tuning` additionally reads a stored schema-2 block and is used only by the
stored-settings reader. :func:`resolve_tier` / :func:`resolve_turn` validate requests against the
:class:`~providers.capabilities.CapabilityMatrix` and return the invocation each tier sends,
:func:`defaults` reads the ``config/models.yaml`` tier defaults, and :func:`to_wire` is the inverse
of :func:`parse_wire_tuning`. Every rejection is a :class:`crm.errors.ValidationFailed` whose
``source`` is ``tuning.tiers.<tier>.<field>`` (or ``tuning.<key>`` for the block itself).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, get_args

from crm.errors import ValidationFailed
from providers.base import ResolvedInvocation, Tuning, validate_tuning
from providers.capabilities import CapabilityMatrix, Family, Tier

TIERS: Final[tuple[Tier, ...]] = ("supervisor", "subagent", "tool")
WIRE_SCHEMA_VERSION: Final[int] = 3

#: The stored-row schema :func:`parse_stored_tuning` also reads, whose tiers may carry temperature.
_LEGACY_STORED_SCHEMA_VERSION: Final[int] = 2

#: The fields a tier request may carry on the wire.
TIER_FIELDS: Final[tuple[str, ...]] = ("family", "model", "effort")

_RETIRED_TIER_FIELD: Final[str] = "temperature"

#: The keys a block may carry at its top level.
_BLOCK_KEYS: Final[frozenset[str]] = frozenset({"schema", "source", "tiers"})

_FAMILIES: Final[tuple[Family, ...]] = get_args(Family)


@dataclass(frozen=True, slots=True)
class TierRequest:
    """One tier as requested; every field optional.

    Attributes:
        family: The tier's provider family, or ``None`` for the matrix's default family.
        model: A model id of that family, or ``None`` for the family's model for the tier.
        effort: An effort value, or ``None`` for the model's ``effort.default``.
    """

    family: Family | None = None
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True, slots=True)
class TurnTuning:
    """A turn's tuning: where it came from and a request per tier.

    Attributes:
        source: Where the request originated (for example ``settings.tuning`` or ``--effort``).
            Must be a non-empty string.
        tiers: Per-tier requests keyed by a member of :data:`TIERS`; an absent tier is treated as
            ``TierRequest()``. Stored as a read-only copy.

    Raises:
        ValidationFailed: ``source`` is empty or not a string (``source="tuning.source"``), or
            ``tiers`` names a tier outside :data:`TIERS` (``source="tuning.tiers.<key>"``).
    """

    source: str
    tiers: Mapping[Tier, TierRequest]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValidationFailed(
                "tuning.source must be a non-empty string.", source="tuning.source"
            )
        for key in self.tiers:
            _known_tier(key)
        object.__setattr__(self, "tiers", MappingProxyType(dict(self.tiers)))


def _field_source(tier: str, field: str) -> str:
    return f"tuning.tiers.{tier}.{field}"


def _optional_str(value: object, *, source: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValidationFailed(f"{source} must be a string or null.", source=source)


def _optional_family(value: object, *, source: str) -> Family | None:
    if value is None:
        return None
    for family in _FAMILIES:
        if value == family:
            return family
    raise ValidationFailed(
        f"Unknown model family {value!r}; known families are {list(_FAMILIES)}.", source=source
    )


def _parse_tier(tier: str, raw: object) -> TierRequest:
    if not isinstance(raw, dict):
        raise ValidationFailed(
            f"tuning.tiers.{tier} must be an object.", source=f"tuning.tiers.{tier}"
        )
    for field in raw:
        if field not in TIER_FIELDS:
            raise ValidationFailed(
                f"Unknown tuning field {field!r} for the {tier} tier; accepted fields are "
                f"{list(TIER_FIELDS)}.",
                source=_field_source(tier, str(field)),
            )
    return TierRequest(
        family=_optional_family(raw.get("family"), source=_field_source(tier, "family")),
        model=_optional_str(raw.get("model"), source=_field_source(tier, "model")),
        effort=_optional_str(raw.get("effort"), source=_field_source(tier, "effort")),
    )


def _known_tier(key: object) -> Tier:
    for tier in TIERS:
        if key == tier:
            return tier
    raise ValidationFailed(
        f"Unknown tuning tier {key!r}; tiers are {list(TIERS)}.", source=f"tuning.tiers.{key}"
    )


def _reject_retired_fields(raw: dict[object, object]) -> None:
    raw_tiers = raw.get("tiers")
    if not isinstance(raw_tiers, dict):
        return
    for tier, request in raw_tiers.items():
        if isinstance(request, dict) and _RETIRED_TIER_FIELD in request:
            source = _field_source(str(tier), _RETIRED_TIER_FIELD)
            raise ValidationFailed(
                f"{source}: temperature is no longer a tuning parameter; remove it.",
                source=source,
            )


def _decode_block(raw: dict[object, object], *, default_source: str) -> TurnTuning:
    for key in raw:
        if key not in _BLOCK_KEYS:
            raise ValidationFailed(
                f"Unknown tuning key {key!r}; accepted keys are {sorted(_BLOCK_KEYS)}.",
                source=f"tuning.{key}",
            )
    source = raw.get("source", default_source)
    if not isinstance(source, str) or not source:
        raise ValidationFailed("tuning.source must be a non-empty string.", source="tuning.source")
    raw_tiers = raw.get("tiers", {})
    if not isinstance(raw_tiers, dict):
        raise ValidationFailed("tuning.tiers must be an object.", source="tuning.tiers")
    tiers = {_known_tier(key): _parse_tier(str(key), value) for key, value in raw_tiers.items()}
    return TurnTuning(source=source, tiers=tiers)


def _is_schema(value: object, version: int) -> bool:
    return type(value) is int and value == version


def parse_wire_tuning(raw: object, *, default_source: str) -> TurnTuning:
    """Decode a schema-3 wire ``tuning`` block into a :class:`TurnTuning`, strictly.

    Structural only: families are checked against the known families, and models and effort are
    checked against the matrix by :func:`resolve_turn`. Rules apply in order: the block must be an
    object; no tier may carry a ``temperature`` key (``null`` included); ``schema`` must be 3; no
    unknown key, tier or field; every field of the right type.

    Args:
        raw: The decoded JSON value of the request's ``tuning`` field.
        default_source: The ``source`` to record when the block carries none.

    Raises:
        ValidationFailed: ``source="tuning"`` for a non-object,
            ``source="tuning.tiers.<tier>.temperature"`` for a temperature key,
            ``source="tuning.schema"`` for any schema but 3, else the offending key or field.
    """
    if not isinstance(raw, dict):
        raise ValidationFailed("tuning must be an object.", source="tuning")
    _reject_retired_fields(raw)
    schema = raw.get("schema")
    if not _is_schema(schema, WIRE_SCHEMA_VERSION):
        raise ValidationFailed(
            f"tuning must declare schema {WIRE_SCHEMA_VERSION} with per-tier settings under "
            f"'tiers'; got schema {schema!r}. A flat family/effort block is not accepted.",
            source="tuning.schema",
        )
    return _decode_block(raw, default_source=default_source)


def parse_stored_tuning(raw: object, *, default_source: str) -> TurnTuning:
    """Decode a stored ``tuning`` block, accepting schema 2 as well as schema 3.

    A schema-2 block has each tier's ``temperature`` key dropped and is then decoded as schema 3;
    any other block is decoded by :func:`parse_wire_tuning` unchanged. For the stored-settings
    reader only: write paths and turns use :func:`parse_wire_tuning`.

    Raises:
        ValidationFailed: As :func:`parse_wire_tuning`, after the schema-2 conversion.
    """
    if isinstance(raw, dict) and _is_schema(raw.get("schema"), _LEGACY_STORED_SCHEMA_VERSION):
        raw_tiers = raw.get("tiers")
        converted: dict[object, object] = {**raw, "schema": WIRE_SCHEMA_VERSION}
        if isinstance(raw_tiers, dict):
            converted["tiers"] = {
                tier: (
                    {k: v for k, v in request.items() if k != _RETIRED_TIER_FIELD}
                    if isinstance(request, dict)
                    else request
                )
                for tier, request in raw_tiers.items()
            }
        raw = converted
    return parse_wire_tuning(raw, default_source=default_source)


def resolve_tier(
    matrix: CapabilityMatrix, tier: Tier, request: TierRequest, *, source: str
) -> ResolvedInvocation:
    """Validate one tier's request and return the invocation that tier sends.

    An unset effort resolves to the family's ``config/models.yaml`` tier effort when the tier runs
    its configured model, and to the model's ``effort.default`` otherwise.

    Args:
        matrix: The loaded capability matrix.
        tier: The tier being resolved.
        request: That tier's request.
        source: Where the request originated, recorded in each rejection's ``detail``.

    Raises:
        ValidationFailed: An unknown tier or family, a model absent from the matrix or not of the
            tier's family, or an effort the model does not accept (unsupported, or outside
            ``effort.values``).
    """
    tier = _known_tier(tier)
    detail = f"requested by {source}"
    family = request.family if request.family is not None else matrix.default_family
    if family not in matrix.tiers:
        raise ValidationFailed(
            f"Unknown model family {family!r}; configured families are {sorted(matrix.tiers)}.",
            source=_field_source(tier, "family"),
            detail=detail,
        )
    if tier not in matrix.tiers[family]:
        raise ValidationFailed(
            f"The {family} family configures no {tier} tier.",
            source=_field_source(tier, "family"),
            detail=detail,
        )
    if request.model is None:
        model = matrix.model_for(family, tier)
    else:
        candidate = matrix.models.get(request.model)
        if candidate is None:
            raise ValidationFailed(
                f"Unknown model {request.model!r} for the {tier} tier.",
                source=_field_source(tier, "model"),
                detail=detail,
            )
        if candidate.family != family:
            raise ValidationFailed(
                f"{request.model} is a {candidate.family} model; the {tier} tier runs the "
                f"{family} family.",
                source=_field_source(tier, "model"),
                detail=detail,
            )
        model = candidate
    effort = request.effort
    configured = matrix.tiers[family][tier]
    if effort is None and model.model_id == configured.model_id:
        effort = configured.tuning.get("effort")
    try:
        return validate_tuning(
            model,
            Tuning(source=source, effort=effort),
            effort_source=_field_source(tier, "effort"),
        )
    except ValidationFailed as exc:
        raise ValidationFailed(str(exc), source=exc.source, detail=detail) from exc


def resolve_turn(matrix: CapabilityMatrix, tuning: TurnTuning) -> Mapping[Tier, ResolvedInvocation]:
    """Resolve every tier of a turn, in :data:`TIERS` order.

    Raises:
        ValidationFailed: ``tuning.tiers`` names an unknown tier, or any tier fails
            :func:`resolve_tier`.
    """
    for key in tuning.tiers:
        _known_tier(key)
    return {
        tier: resolve_tier(
            matrix, tier, tuning.tiers.get(tier, TierRequest()), source=tuning.source
        )
        for tier in TIERS
    }


def defaults(matrix: CapabilityMatrix) -> TurnTuning:
    """The ``config/models.yaml`` default for every tier: default family, its model and tuning."""
    tiers: dict[Tier, TierRequest] = {}
    for tier in TIERS:
        assignment = matrix.tiers[matrix.default_family][tier]
        tiers[tier] = TierRequest(
            family=matrix.default_family,
            model=assignment.model_id,
            effort=assignment.tuning.get("effort"),
        )
    return TurnTuning(source="config/models.yaml", tiers=tiers)


def to_wire(tuning: TurnTuning) -> dict[str, object]:
    """Render ``tuning`` as a schema-3 wire block that :func:`parse_wire_tuning` decodes back."""
    return {
        "schema": WIRE_SCHEMA_VERSION,
        "source": tuning.source,
        "tiers": {
            tier: {
                "family": request.family,
                "model": request.model,
                "effort": request.effort,
            }
            for tier, request in tuning.tiers.items()
        },
    }


__all__ = [
    "TIERS",
    "TIER_FIELDS",
    "WIRE_SCHEMA_VERSION",
    "TierRequest",
    "TurnTuning",
    "defaults",
    "parse_stored_tuning",
    "parse_wire_tuning",
    "resolve_tier",
    "resolve_turn",
    "to_wire",
]
