# SPDX-License-Identifier: MIT
"""The toolspec-derived input validator every action Lambda applies to its arguments.

:func:`validate_arguments` reads ``ToolDefinition.input_schema`` directly and raises
:class:`crm.errors.ValidationFailed` on failure. The error's ``message``, ``source`` and ``detail``
are derived from the schema only and never echo caller input: the message names the violated
keyword's schema terms (the property, the allowed values or bound, the missing or accepted names) and
ends with :data:`CORRECTION_HINT`. :func:`arguments_conform` is the
non-raising predicate over the same compiled schema, for callers that only need the verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from crm.errors import ValidationFailed
from crm.toolspec.definitions import BY_NAME, ToolDefinition

__all__ = [
    "CORRECTION_HINT",
    "SCHEMA_KEYWORDS_ENFORCED_HERE",
    "arguments_conform",
    "validate_arguments",
    "validator_for",
]

#: Schema keywords enforced only here; AgentCore's ``SchemaDefinition`` cannot carry them.
SCHEMA_KEYWORDS_ENFORCED_HERE: Final[frozenset[str]] = frozenset(
    {"additionalProperties", "enum", "maxLength", "minLength", "minimum", "maximum"}
)

#: Appended to every validation message.
CORRECTION_HINT: Final[str] = "Correct the arguments and call again."

#: Keyword family whose message reports the schema bound the argument violated.
_BOUND_KEYWORDS: Final[frozenset[str]] = frozenset({"minLength", "maxLength", "minimum", "maximum"})

_VALIDATORS: Final[dict[str, Draft202012Validator]] = {}


def validator_for(tool: ToolDefinition) -> Draft202012Validator:
    """Return the JSON Schema validator for ``tool``'s ``input_schema``, cached by tool name."""
    cached = _VALIDATORS.get(tool.name)
    if cached is None:
        cached = Draft202012Validator(tool.input_schema)
        _VALIDATORS[tool.name] = cached
    return cached


def _declared_properties(tool: ToolDefinition) -> set[str]:
    """Return the top-level property names ``tool``'s input schema declares."""
    properties = tool.input_schema.get("properties")
    return set(properties) if isinstance(properties, dict) else set()


def _error_order(error: ValidationError) -> tuple[int, str, str]:
    """Sort key making the reported error deterministic when several keywords fail at once."""
    path = [str(part) for part in error.absolute_path]
    return (len(path), "/".join(path), str(error.validator))


def _missing_required(error: ValidationError) -> list[str]:
    """For a ``required`` error, the schema-listed names absent from the instance, in schema order."""
    instance = error.instance if isinstance(error.instance, Mapping) else {}
    required = error.validator_value if isinstance(error.validator_value, list) else []
    return [str(name) for name in required if name not in instance]


def _safe_source(tool: ToolDefinition, error: ValidationError) -> str:
    """Return the offending schema-declared property name, else ``"arguments"``.

    A missing top-level required property is reported by its own (schema) name. An undeclared key
    (the ``additionalProperties`` case) is reported as ``"arguments"``.
    """
    path = list(error.absolute_path)
    if path and isinstance(path[0], str) and path[0] in _declared_properties(tool):
        return path[0]
    if not path and error.validator == "required":
        missing = _missing_required(error)
        if missing:
            return missing[0]
    return "arguments"


def _schema_location(tool: ToolDefinition, error: ValidationError) -> str | None:
    """The dotted path of schema-declared property names leading to the error, or ``None``.

    Walks the schema alongside the error path and stops at the first part the schema does not
    declare, so every name in the result is a schema property name.
    """
    schema: Mapping[str, Any] = tool.input_schema
    names: list[str] = []
    for part in error.absolute_path:
        properties = schema.get("properties")
        if not (isinstance(part, str) and isinstance(properties, Mapping) and part in properties):
            break
        names.append(part)
        child = properties[part]
        schema = child if isinstance(child, Mapping) else {}
    return ".".join(names) or None


def _joined(values: object) -> str:
    items = values if isinstance(values, list | tuple) else [values]
    return ", ".join(str(item) for item in items)


def _safe_message(tool: ToolDefinition, error: ValidationError) -> str:
    """Build the corrective message from the schema alone; no instance value is read into it."""
    where = _schema_location(tool, error)
    subject = f"`{where}`" if where else "The arguments"
    keyword = str(error.validator)
    if keyword == "required":
        scope = f" in `{where}`" if where else ""
        text = f"Missing required argument(s){scope}: {', '.join(_missing_required(error))}."
    elif keyword == "additionalProperties":
        accepted = error.schema.get("properties") if isinstance(error.schema, Mapping) else None
        names = sorted(accepted) if isinstance(accepted, Mapping) else []
        scope = f" in `{where}`" if where else ""
        text = f"Unknown argument name(s) supplied{scope}. Accepted: {', '.join(names)}."
    elif keyword == "enum":
        text = f"{subject} must be one of: {_joined(error.validator_value)}."
    elif keyword == "type":
        text = f"{subject} must be of type {_joined(error.validator_value).replace(', ', ' or ')}."
    elif keyword in _BOUND_KEYWORDS:
        text = f"{subject} violates {keyword}={error.validator_value}."
    else:
        text = f"{subject} does not match this tool's input schema."
    return f"{text} {CORRECTION_HINT}"


def _safe_detail(tool: ToolDefinition, error: ValidationError, arguments: Mapping[str, Any]) -> str:
    """Build the internal diagnostic string, derived from the schema and never from the instance."""
    parts = [
        f"tool={tool.name}",
        f"keyword={error.validator}",
        f"property={_safe_source(tool, error)}",
    ]
    if error.validator == "additionalProperties":
        undeclared = [key for key in arguments if key not in _declared_properties(tool)]
        parts.append(f"undeclared_property_count={len(undeclared)}")
    else:
        parts.append(f"schema_requires={error.validator_value!r}")
    return " ".join(parts)


def _first_error(tool: ToolDefinition, arguments: Mapping[str, Any]) -> ValidationError | None:
    """Return the deterministic first schema violation of ``arguments``, or ``None``."""
    errors = sorted(validator_for(tool).iter_errors(dict(arguments)), key=_error_order)
    return errors[0] if errors else None


def arguments_conform(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    """Return whether ``arguments`` satisfy the input schema of the tool named ``tool_name``.

    Accepts and rejects exactly what :func:`validate_arguments` does. An unknown tool name does not
    conform.
    """
    tool = BY_NAME.get(tool_name)
    return tool is not None and _first_error(tool, arguments) is None


def validate_arguments(tool: ToolDefinition, arguments: Mapping[str, Any]) -> None:
    """Validate ``arguments`` against ``tool``'s input schema, raising on the first violation.

    Args:
        tool: The invoked action's definition, from :data:`crm.toolspec.definitions.ALL_TOOLS`.
        arguments: The tool arguments, already stripped of identity channel keys.

    Raises:
        ValidationFailed: ``arguments`` violates the schema. The reported error is deterministic
            when several keywords fail; its ``message`` and ``source`` carry no caller input.
    """
    error = _first_error(tool, arguments)
    if error is None:
        return
    raise ValidationFailed(
        _safe_message(tool, error),
        source=_safe_source(tool, error),
        detail=_safe_detail(tool, error, arguments),
    )
