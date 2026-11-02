# SPDX-License-Identifier: MIT
"""Field-level validation of update and proposal payloads against the toolspec allowlists.

:func:`validate_update_fields` checks a direct ``update_*`` payload against ``allowlist_for``;
:func:`validate_proposal_payload` checks a proposal's ``after_payload`` against
:func:`proposal_payload_allowlist`. Both check keys, then values via :func:`validate_field_values`,
and raise :class:`crm.errors.ValidationFailed` naming the first rejected field.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Union, get_args, get_origin

from crm.domain.entities import Account, Activity, Contact, Opportunity
from crm.errors import ValidationFailed, bounded_detail_value
from crm.store.keys import derived_source_attributes
from crm.toolspec.definitions import TABLES, allowlist_for, high_risk_fields_for

#: The entity dataclass whose annotations declare each table's field value types.
_ENTITY_FOR_TABLE: Final[dict[str, type]] = {
    "accounts": Account,
    "contacts": Contact,
    "opportunities": Opportunity,
    "activities": Activity,
}


@dataclass(frozen=True, slots=True)
class FieldValueRule:
    """What one allowlisted field's own entity annotation admits as a value.

    Attributes:
        accepted: The concrete value types, never containing ``NoneType``.
        optional: Whether the annotation admitted ``None``.
        permitted_values: The closed set of values a :data:`typing.Literal` annotation declares, or
            ``None`` when the annotation constrains the type only.
    """

    accepted: frozenset[type]
    optional: bool
    permitted_values: frozenset[object] | None


def _field_value_rule(annotation: object) -> FieldValueRule:
    """Return what ``annotation`` admits: its concrete types, optionality, and enum members.

    Args:
        annotation: A resolved type annotation from an entity dataclass — a class, an optional
            union, or a :data:`typing.Literal` of string values.

    Returns:
        The :class:`FieldValueRule` for that annotation. ``permitted_values`` is populated only when
        every non-``None`` member of the annotation is a ``Literal``.

    Raises:
        TypeError: When the annotation is a form this function does not model.
    """
    origin = get_origin(annotation)
    if origin is Literal:
        members = get_args(annotation)
        return FieldValueRule(
            accepted=frozenset(type(value) for value in members),
            optional=False,
            permitted_values=frozenset(members),
        )
    if origin is Union or origin is types.UnionType:
        accepted: set[type] = set()
        optional = False
        members_by_arm: list[frozenset[object] | None] = []
        for argument in get_args(annotation):
            if argument is type(None):
                optional = True
                continue
            member_rule = _field_value_rule(argument)
            accepted |= member_rule.accepted
            optional = optional or member_rule.optional
            members_by_arm.append(member_rule.permitted_values)
        combined: frozenset[object] | None = None
        if members_by_arm and all(arm is not None for arm in members_by_arm):
            combined = frozenset().union(*(arm for arm in members_by_arm if arm is not None))
        return FieldValueRule(
            accepted=frozenset(accepted), optional=optional, permitted_values=combined
        )
    if isinstance(annotation, type):
        return FieldValueRule(
            accepted=frozenset({annotation}), optional=False, permitted_values=None
        )
    raise TypeError(f"unsupported field annotation: {annotation!r}")


def _build_field_types() -> dict[str, dict[str, FieldValueRule]]:
    """Resolve every allowlisted field's declared value rule, per table.

    Raises:
        ValueError: When a field permitted by ``proposal_payload_allowlist`` is not declared on the
            table's entity dataclass.
    """
    resolved: dict[str, dict[str, FieldValueRule]] = {}
    for table, entity in _ENTITY_FOR_TABLE.items():
        hints = typing.get_type_hints(entity)
        fields = allowlist_for(table) | high_risk_fields_for(table)
        undeclared = sorted(field for field in fields if field not in hints)
        if undeclared:
            raise ValueError(
                f"{table}: allowlisted field(s) {undeclared} are not declared on "
                f"{entity.__name__}, so their value types cannot be validated"
            )
        resolved[table] = {field: _field_value_rule(hints[field]) for field in sorted(fields)}
    return resolved


#: Declared value rules for every allowlisted field, keyed by table then field name; built at import.
FIELD_VALUE_TYPES: Final[dict[str, dict[str, FieldValueRule]]] = _build_field_types()


def _type_name(rule: FieldValueRule) -> str:
    names = sorted(t.__name__ for t in rule.accepted)
    return " | ".join([*names, *(["None"] if rule.optional else [])])


def _value_matches(value: object, rule: FieldValueRule) -> bool:
    """Whether ``value`` is one of the declared types, with JSON number widening.

    ``bool`` never satisfies an ``int`` or ``float`` field; an ``int`` satisfies a ``float`` field.
    """
    if value is None:
        return rule.optional
    if isinstance(value, bool):
        return bool in rule.accepted
    if isinstance(value, int) and float in rule.accepted:
        return True
    return any(
        type(value) is candidate or isinstance(value, candidate) for candidate in rule.accepted
    )


def validate_field_values(table: str, fields: Mapping[str, object]) -> None:
    """Reject a field value whose type, enum membership or shape the declared schema does not admit.

    Rejects a value whose type the entity annotation does not admit, a value outside a
    ``Literal`` field's closed set, and a blank string for a field :func:`crm.store.keys.derive`
    derives an index key attribute from. ``None`` is accepted for an optional field.

    Args:
        table: A plural table name from :data:`crm.toolspec.definitions.TABLES`.
        fields: Field values to check. Keys outside the table's allowlist are ignored.

    Raises:
        ValidationFailed: When a value's type is not one the entity declares for that field, when it
            falls outside the closed set an enum-typed field declares, or when a derived-source
            field carries a blank string.
        ValueError: When ``table`` is not a recognised table name.
    """
    if table not in FIELD_VALUE_TYPES:
        raise ValueError(f"unknown table: {table!r}")
    declared = FIELD_VALUE_TYPES[table]
    index_sources = derived_source_attributes(table)

    for key, value in fields.items():
        rule = declared.get(key)
        if rule is None:
            continue
        if not _value_matches(value, rule):
            raise ValidationFailed(
                f"Field {key!r} on {table} requires a value of type {_type_name(rule)}.",
                source=key,
                detail=(
                    f"table={table} field={key} expected={_type_name(rule)} "
                    f"got={type(value).__name__}"
                ),
            )
        if (
            rule.permitted_values is not None
            and value is not None
            and value not in rule.permitted_values
        ):
            permitted = sorted(str(member) for member in rule.permitted_values)
            raise ValidationFailed(
                f"Field {key!r} on {table} must be one of {permitted}.",
                source=key,
                detail=f"table={table} field={key} permitted={permitted}",
            )
        if key in index_sources and isinstance(value, str) and not value.strip():
            raise ValidationFailed(
                f"Field {key!r} on {table} must not be blank: it is a source of a derived index "
                f"key attribute, and a blank value would remove the record from that index.",
                source=key,
                detail=f"table={table} field={key} derived_index_source=true",
            )


#: Every field name the repo defines for each table: its entity's fields and its stored index sources.
_SCHEMA_FIELDS: Final[dict[str, frozenset[str]]] = {
    table: frozenset(typing.get_type_hints(entity)) | derived_source_attributes(table)
    for table, entity in _ENTITY_FOR_TABLE.items()
}


#: The ``source`` reported for a rejected update key that no schema names.
UNKNOWN_FIELD_SOURCE: Final[str] = "unknown_field"


def validate_update_fields(table: str, fields: dict[str, object]) -> None:
    """Reject any key in ``fields`` absent from ``table``'s update allowlist.

    Also validates the values via :func:`validate_field_values`.

    Args:
        table: A plural table name from :data:`crm.toolspec.definitions.TABLES`.
        fields: The caller-supplied field payload from an ``update_*`` request.

    Raises:
        ValidationFailed: When ``fields`` contains a key absent from ``allowlist_for(table)``. The
            error concerns the first such key, in the order ``fields`` iterates: when it is a field
            the repo defines for the table (``_SCHEMA_FIELDS``) it is the ``source`` and is named
            in the message, which routes one of ``high_risk_fields_for(table)`` to
            ``submit_proposal``; any other key is reported as :data:`UNKNOWN_FIELD_SOURCE` and
            never echoed. The message lists the permitted fields.
        ValueError: When ``table`` is not a member of :data:`crm.toolspec.definitions.TABLES`.
    """
    allowed = allowlist_for(table)
    rejected = [key for key in fields if key not in allowed]
    if rejected:
        first = rejected[0]
        known = first in _SCHEMA_FIELDS.get(table, frozenset())
        named = f"Field {first!r} is" if known else "A supplied field is"
        high_risk = first in high_risk_fields_for(table)
        proposal_route = " Change it with submit_proposal." if high_risk else ""
        raise ValidationFailed(
            f"{named} not permitted on {table} updates. Permitted: {', '.join(sorted(allowed))}."
            f"{proposal_route}",
            source=first if known else UNKNOWN_FIELD_SOURCE,
            detail=(
                f"table={table} rejected_count={len(rejected)} "
                f"rejected_fields={[bounded_detail_value(key) for key in rejected[:8]]}"
            ),
        )
    validate_field_values(table, fields)


def proposal_payload_allowlist(table: str) -> frozenset[str]:
    """Return the fields a proposal's ``after_payload`` may carry for ``table``.

    The set is ``allowlist_for(table) | high_risk_fields_for(table)``.

    Args:
        table: A plural table name from :data:`crm.toolspec.definitions.TABLES`.

    Returns:
        The permitted field names.

    Raises:
        ValueError: When ``table`` is not a member of :data:`crm.toolspec.definitions.TABLES`.
    """
    return allowlist_for(table) | high_risk_fields_for(table)


def validate_proposal_payload(table: str, after_payload: Mapping[str, object] | None) -> None:
    """Reject any key in ``after_payload`` absent from :func:`proposal_payload_allowlist`.

    Called when a proposal is submitted and again before its approval commits. Also validates the
    values via :func:`validate_field_values`.

    Args:
        table: A plural table name from :data:`crm.toolspec.definitions.TABLES`.
        after_payload: The proposal's intended field values, or ``None``/empty for an operation
            that carries none (a ``delete``), which is accepted.

    Raises:
        ValidationFailed: When ``after_payload`` contains a key outside the permitted set. The
            error's ``source`` is ``after_payload.`` + :data:`UNKNOWN_FIELD_SOURCE`, never the key;
            its message lists the permitted fields.
        ValueError: When ``table`` is not a recognised table name.
    """
    allowed = proposal_payload_allowlist(table)
    if not after_payload:
        return
    rejected = [key for key in after_payload if key not in allowed]
    if rejected:
        raise ValidationFailed(
            f"A supplied field is not permitted in a {table} proposal's after_payload. "
            f"Permitted: {', '.join(sorted(allowed))}.",
            source=f"after_payload.{UNKNOWN_FIELD_SOURCE}",
            detail=(
                f"table={table} rejected_count={len(rejected)} "
                f"rejected_fields={[bounded_detail_value(key) for key in rejected[:8]]}"
            ),
        )
    validate_field_values(table, after_payload)


def is_known_table(table: str) -> bool:
    """Return whether ``table`` is a recognised entity table name.

    Args:
        table: A candidate table name to check against :data:`crm.toolspec.definitions.TABLES`.

    Returns:
        ``True`` if ``table`` is a member of :data:`crm.toolspec.definitions.TABLES`.
    """
    return table in TABLES
