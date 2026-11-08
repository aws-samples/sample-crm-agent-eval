#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regenerate every toolspec consumer from `crm.toolspec.definitions.ALL_TOOLS` and fail on drift.

It checks every consumer against the single source of truth:

1. The frontend's generated TypeScript types (`frontend/src/types/generated/toolSchemas.ts`),
   regenerated in place via `npm run gen:types` and diffed byte-for-byte against its prior content.
   When the file is absent (a fresh clone before any `gen:types` run), the first regeneration is
   the baseline and a second one is diffed against it. A compared regeneration that exits 0 without
   replacing the file (same inode and mtime) fails rather than self-compares. Either way, the regenerated `ToolName`
   union must name exactly the tools in `ALL_TOOLS`.
2. The Terraform root composition's hand-transcribed tool inventory (`infra/main.tf`'s
   `locals.tools` and `locals.tool_schema_properties`), diffed structurally: every tool name,
   handler path, and description in `local.tools` must match `ALL_TOOLS` exactly, and
   `local.tool_schema_properties` must match per property — same tool name set, same property name
   set per tool, and the same `type`, `required` flag and `description` for every property
   (a property ALL_TOOLS gives no description must carry none). Keywords AgentCore's
   `SchemaDefinition` cannot carry (`minLength`, `maxLength`, `enum`, `minimum`, `maximum`,
   `additionalProperties`) are checked against the handler validator by consumer 3 instead.
3. The per-handler input validator (`crm.toolspec.validation.validate_arguments`, applied by
   `crm.handlers._common.build_handler`). `check_handler_validator_coverage` verifies that every
   tool resolves to an importable handler, that every declared instance of the five keywords above
   rejects one violating payload attributable to that keyword, and that every handler, driven end
   to end through its Lambda entry point, rejects an argument mapping its schema rejects.
   `check_validator_call_site_unconditional` requires `src/` to hold exactly one
   `validate_arguments` call site, reached unconditionally.
4. Tool-name uniqueness: no two `ToolDefinition`s in `ALL_TOOLS` share a `name`.

Usage: `python3 scripts/check_toolspec_parity.py [--skip-frontend]`
`--skip-frontend` skips the Node/npm regeneration step (still checks Terraform + validator + names)
and reports it as deferred.

Exit codes: 0 pass; 1 drift detected; 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# `scripts/` is not a package; its directory is put on the path to import `run_gates`' marker.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import proc_run  # noqa: E402

from crm.envelope import IDENTITY_KEY, identity_event  # noqa: E402
from crm.errors import ValidationFailed  # noqa: E402
from crm.toolspec.definitions import ALL_TOOLS, ToolDefinition  # noqa: E402
from crm.toolspec.validation import (  # noqa: E402
    SCHEMA_KEYWORDS_ENFORCED_HERE,
    validate_arguments,
    validator_for,
)
from run_gates import GATE_DEFER_MARKER  # type: ignore[import-not-found] # noqa: E402

FRONTEND_DIR = REPO_ROOT / "frontend"
GENERATED_TS = FRONTEND_DIR / "src" / "types" / "generated" / "toolSchemas.ts"
MAIN_TF = REPO_ROOT / "infra" / "main.tf"

#: Declared restrictive instances of each keyword the handler validator alone enforces. Every one is
#: executed against a violating payload by `check_handler_validator_coverage`. Every root schema is
#: closed, so the restrictive `additionalProperties` count is one per tool.
EXPECTED_KEYWORD_INSTANCES = {
    "additionalProperties": len(ALL_TOOLS),
    "enum": 9,
    "maxLength": 68,
    "minLength": 45,
    "minimum": 5,
    "maximum": 3,
}

#: Nested `additionalProperties: true` instances — the open `fields`/`after_payload` objects, plus
#: `get_page_context.page_context`. They permit rather than restrict, so they are counted separately
#: from `EXPECTED_KEYWORD_INSTANCES["additionalProperties"]`.
EXPECTED_PERMISSIVE_ADDITIONAL_PROPERTIES = 6

#: Argument key no tool declares, used as the violating property in the end-to-end handler probe.
#: Every tool's root schema carries `additionalProperties: false`, so this one key violates them all.
_UNDECLARED_PROBE_KEY = "__undeclared_property__"

#: Identity the handler probe supplies on the `_identity` channel. The probe injects a store double
#: that raises on any attribute access, so it never reaches AWS.
_PROBE_IDENTITY: dict[str, Any] = {
    "rep_id": "rep-probe",
    "book": ["acct-probe"],
    "full_visibility": False,
    "principal_type": "user",
    "session_id": "sess-probe",
    "source": "gateway",
    "tenant": "demo",
}


def check_tool_names_unique() -> list[str]:
    """Assert no two `ALL_TOOLS` entries share a `name`."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for tool in ALL_TOOLS:
        (duplicates if tool.name in seen else seen).add(tool.name)
    return [
        f"ALL_TOOLS declares {name!r} more than once; every name-keyed consumer keeps only one"
        for name in sorted(duplicates)
    ]


def check_display_label_coverage() -> list[str]:
    """Assert every tool carries a `display_label` and that it never enters the MCP projection.

    `display_label` is UI-only: it flows to the frontend `TOOL_METADATA`, but the AgentCore Gateway
    MCP tool (``to_mcp_tool``) carries only name/description/inputSchema.
    """
    violations: list[str] = []
    for tool in ALL_TOOLS:
        if not getattr(tool, "display_label", "").strip():
            violations.append(f"{tool.name!r}: display_label is missing or empty")
        if "display_label" in tool.to_mcp_tool():
            violations.append(
                f"{tool.name!r}: display_label leaked into the MCP projection (to_mcp_tool); it is "
                "UI-only and must not be surfaced to MCP clients"
            )
    return violations


#: The one module in `src/` permitted to call `crm.toolspec.validation.validate_arguments`, and the
#: name of the function the call must sit directly in.
VALIDATOR_CALL_SITE_MODULE = Path("crm") / "handlers" / "_common.py"
VALIDATOR_CALL_SITE_FUNCTION = "run_handler"

#: AST node types the validator call may be nested inside, each with the child field it may be
#: reached through. Any other enclosing node fails the gate.
_UNCONDITIONAL_ANCESTORS: dict[type[ast.AST], frozenset[str]] = {
    ast.Module: frozenset({"body"}),
    ast.FunctionDef: frozenset({"body"}),
    ast.AsyncFunctionDef: frozenset({"body"}),
    # Only the `try` body, not `except`/`else`/`finally`.
    ast.Try: frozenset({"body"}),
    ast.Expr: frozenset({"value"}),
}


def _validator_call_sites(tree: ast.Module) -> list[tuple[ast.Call, list[tuple[ast.AST, str]]]]:
    """Return every ``validate_arguments(...)`` call in `tree` with its ancestor chain.

    Each ancestor is ``(node, field)`` — the enclosing node and the field of that node the call was
    reached through — outermost first, so a caller can decide whether every step was unconditional.
    """
    found: list[tuple[ast.Call, list[tuple[ast.AST, str]]]] = []

    def walk(node: ast.AST, chain: list[tuple[ast.AST, str]]) -> None:
        for field, value in ast.iter_fields(node):
            children = value if isinstance(value, list) else [value]
            for child in children:
                if not isinstance(child, ast.AST):
                    continue
                if isinstance(child, ast.Call) and _called_name(child) == "validate_arguments":
                    found.append((child, [*chain, (node, field)]))
                walk(child, [*chain, (node, field)])

    walk(tree, [])
    return found


def _called_name(call: ast.Call) -> str | None:
    """The bare name being called: ``f(...)`` -> ``f``, ``mod.f(...)`` -> ``f``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def check_validator_call_site_unconditional() -> list[str]:
    """Verify `src/` holds exactly one validator call site and that nothing can skip it.

    Reads every `*.py` under `src/` and delegates to :func:`validator_call_site_violations`.
    """
    sources: dict[Path, str] = {}
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        sources[path.relative_to(REPO_ROOT / "src")] = path.read_text(encoding="utf-8")
    return validator_call_site_violations(sources)


def validator_call_site_violations(sources: dict[Path, str]) -> list[str]:
    """Return every way `sources` fails the single-unconditional-call-site rule.

    The call must be the only one in `sources`, must live in :data:`VALIDATOR_CALL_SITE_MODULE`'s
    :data:`VALIDATOR_CALL_SITE_FUNCTION`, and every AST node enclosing it must be one of
    :data:`_UNCONDITIONAL_ANCESTORS` reached through a permitted field.

    Args:
        sources: Source text keyed by path relative to the tree's root (``crm/handlers/_common.py``).

    Returns:
        A list of violation messages; empty when exactly one unconditional call site exists.
    """
    violations: list[str] = []
    sites: list[tuple[Path, ast.Call, list[tuple[ast.AST, str]]]] = []
    for relative, text in sorted(sources.items()):
        try:
            tree = ast.parse(text, filename=str(relative))
        except SyntaxError as exc:  # pragma: no cover - a source file that does not parse
            violations.append(f"{relative} does not parse: {exc}")
            continue
        for call, chain in _validator_call_sites(tree):
            sites.append((relative, call, chain))

    if len(sites) != 1:
        located = sorted(f"{path}:{call.lineno}" for path, call, _ in sites)
        violations.append(
            f"expected exactly one `validate_arguments` call site under src/, found "
            f"{len(sites)}: {located}. The request-path trust boundary is one seam every action "
            "Lambda delegates to; a second call site means at least one path validates somewhere "
            "this gate does not check, and zero means no path validates at all."
        )
        return violations

    relative, call, chain = sites[0]
    if relative != VALIDATOR_CALL_SITE_MODULE:
        violations.append(
            f"the `validate_arguments` call site is in {relative} at line {call.lineno}, not in "
            f"{VALIDATOR_CALL_SITE_MODULE}. Every handler reaches the validator through "
            f"{VALIDATOR_CALL_SITE_MODULE}; a call elsewhere is a second, unverified seam."
        )

    enclosing_functions = [
        node.name for node, _ in chain if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    if enclosing_functions != [VALIDATOR_CALL_SITE_FUNCTION]:
        violations.append(
            f"the `validate_arguments` call at {relative}:{call.lineno} sits in "
            f"{enclosing_functions or ['<module level>']}, expected exactly "
            f"[{VALIDATOR_CALL_SITE_FUNCTION!r}] — a nested or relocated call is not the seam "
            "every handler's entry point delegates to."
        )

    for node, field in chain:
        permitted = _UNCONDITIONAL_ANCESTORS.get(type(node))
        if permitted is None:
            violations.append(
                f"the `validate_arguments` call at {relative}:{call.lineno} is nested inside "
                f"{type(node).__name__}, which is not one of the unconditional enclosing nodes "
                f"{sorted(n.__name__ for n in _UNCONDITIONAL_ANCESTORS)}. The validator must run on "
                "every request; a guard, loop, comprehension or conditional expression around it "
                "leaves a payload shape that reaches the handler body unvalidated."
            )
            continue
        if field not in permitted:
            violations.append(
                f"the `validate_arguments` call at {relative}:{call.lineno} is reached through "
                f"{type(node).__name__}.{field}, not {sorted(permitted)}. Reached from anywhere "
                "but the primary body, the call runs on some request paths and not others."
            )
    return violations


def regenerate_frontend_types(*, skip: bool) -> list[str]:
    """Regenerate `toolSchemas.ts` via `npm run gen:types` and diff against its prior content.

    When `skip` is set this half prints a :data:`GATE_DEFER_MARKER` line instead, which
    `run_gates.py` renders as `[PARTIAL]`.

    Returns:
        A list of violation messages; empty when the regenerated file is byte-identical to what
        was on disk before regeneration.
    """
    if skip:
        print(
            f"{GATE_DEFER_MARKER} check_toolspec_parity: the frontend consumer "
            f"({GENERATED_TS.relative_to(REPO_ROOT)}) was NOT checked — `--skip-frontend` was "
            "passed, so "
            "`npm run gen:types` never ran and its byte-diff asserted nothing. The Terraform, "
            "handler-validator and tool-name halves did run. Reason: the CI `verify` job installs "
            "npm dependencies only in `src/relay`, so `frontend/node_modules` does not exist there."
        )
        return []
    first_generation = not GENERATED_TS.exists()
    if first_generation:
        failure = _run_gen_types()
        if failure:
            return [failure]
    before = GENERATED_TS.read_text(encoding="utf-8")
    before_identity = _file_identity(GENERATED_TS)

    failure = _run_gen_types()
    if failure:
        return [failure]
    if _file_identity(GENERATED_TS) == before_identity:
        return [
            f"frontend `npm run gen:types` exited 0 but did not replace {GENERATED_TS}; its "
            "prior content would be compared with itself"
        ]

    after = GENERATED_TS.read_text(encoding="utf-8")
    violations = generated_tool_name_violations(after)
    if before != after:
        if first_generation:
            violations.append(
                f"{GENERATED_TS} is not reproducible: two consecutive `npm run gen:types` runs "
                "from the same toolspec produced different content."
            )
        else:
            violations.append(
                f"{GENERATED_TS} diverged from `crm.toolspec.definitions.ALL_TOOLS`: regenerating "
                "it via `npm run gen:types` produced different content than what was on disk. The "
                "frontend's generated types are stale relative to the toolspec."
            )
    return violations


def _file_identity(path: Path) -> tuple[int, int]:
    """Return ``path``'s (inode, mtime ns); the generator's rename-over write changes both."""
    stat = path.stat()
    return stat.st_ino, stat.st_mtime_ns


def _run_gen_types() -> str | None:
    """Run `npm run gen:types` in the frontend; return a violation message on failure, else None."""
    result = proc_run.run(
        ["npm", "run", "--silent", "gen:types"],
        cwd=FRONTEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return (
            "frontend `npm run gen:types` failed to run — cannot verify parity:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    if not GENERATED_TS.exists():
        return f"frontend `npm run gen:types` exited 0 but did not write {GENERATED_TS}"
    return None


def generated_tool_name_violations(generated: str) -> list[str]:
    """Compare the `ToolName` union in generated `toolSchemas.ts` text against `ALL_TOOLS` names."""
    match = re.search(r"^export type ToolName =\n((?:  \| \"[^\"]+\"\n?)+);", generated, re.M)
    if match is None:
        return [f"{GENERATED_TS} carries no `export type ToolName =` union of string literals"]
    generated_names = set(re.findall(r'"([^"]+)"', match.group(1)))
    expected_names = {tool.name for tool in ALL_TOOLS}
    if generated_names == expected_names:
        return []
    return [
        f"{GENERATED_TS} `ToolName` union differs from `ALL_TOOLS`: missing "
        f"{sorted(expected_names - generated_names)}, unexpected "
        f"{sorted(generated_names - expected_names)}"
    ]


def _parse_tf_locals_tools(text: str) -> dict[str, dict[str, str]]:
    """Extract `name = { handler = "...", description = "..." }` entries from `locals.tools`.

    Args:
        text: Full contents of `infra/main.tf`.

    Returns:
        Mapping of tool name to its transcribed `handler` and `description`.
    """
    match = re.search(r"tools\s*=\s*\{(.*?)\n  \}\n", text, re.DOTALL)
    if match is None:
        return {}
    block = match.group(1)
    entries: dict[str, dict[str, str]] = {}
    entry_pattern = re.compile(
        r'^\s*(\w+)\s*=\s*\{\s*handler\s*=\s*"([^"]*)"\s*,\s*description\s*=\s*"((?:[^"\\]|\\.)*)"',
        re.MULTILINE,
    )
    for entry_match in entry_pattern.finditer(block):
        name, handler, description = entry_match.groups()
        entries[name] = {
            "handler": handler,
            "description": description.replace('\\"', '"'),
        }
    return entries


def _parse_tf_tool_schema_properties(text: str) -> dict[str, dict[str, dict[str, object]]]:
    """Extract `locals.tool_schema_properties` as tool -> property -> `{type, required, description}`.

    The block's entries are `{ name = "x", type = "string", required = true, description = "..." }`,
    with `required` omitted when false and `description` omitted when the property has none
    (`None`). A description's HCL escapes `\"` and `\\` are decoded.
    """
    match = re.search(r"tool_schema_properties\s*=\s*\{(.*?)\n  \}\n\}", text, re.DOTALL)
    if match is None:
        return {}
    block = match.group(1)

    tools: dict[str, dict[str, dict[str, object]]] = {}
    current: dict[str, dict[str, object]] | None = None
    entry_pattern = re.compile(
        r'name\s*=\s*"([^"]*)"\s*,\s*type\s*=\s*"([^"]*)"(\s*,\s*required\s*=\s*(true|false))?'
        r'(\s*,\s*description\s*=\s*"((?:[^"\\]|\\.)*)")?'
    )
    for line in block.splitlines():
        tool_match = re.match(r"^\s*(\w+)\s*=\s*\[", line)
        if tool_match is not None:
            current = {}
            tools[tool_match.group(1)] = current
            continue
        entry_match = entry_pattern.search(line)
        if entry_match is not None and current is not None:
            name, property_type, _, required, _, description = entry_match.groups()
            current[name] = {
                "type": property_type,
                "required": required == "true",
                "description": (
                    None if description is None else re.sub(r"\\(.)", r"\1", description)
                ),
            }
    return tools


def _toolspec_schema_properties(tool: object) -> dict[str, dict[str, object]]:
    """Return `{property name: {type, required, description}}` for one `ToolDefinition`'s schema."""
    schema = tool.input_schema  # type: ignore[attr-defined]
    required = set(schema.get("required") or [])
    properties: dict[str, dict[str, object]] = {}
    for name, declared in (schema.get("properties") or {}).items():
        declared_type = declared.get("type") if isinstance(declared, dict) else None
        description = declared.get("description") if isinstance(declared, dict) else None
        properties[name] = {
            "type": declared_type,
            "required": name in required,
            "description": description,
        }
    return properties


def _check_tool_schema_property_parity(text: str) -> list[str]:
    """Compare `locals.tool_schema_properties` against `ALL_TOOLS` property by property.

    Only what AgentCore's `SchemaDefinition` structure can express is compared — the property name
    set, each property's `type`, whether it is `required`, and its `description`, byte for byte.
    Keywords the structure has no field for (`minLength`, `maxLength`, `enum`, `minimum`,
    `maximum`, `additionalProperties`) are checked by `check_handler_validator_coverage` instead.
    """
    transcribed = _parse_tf_tool_schema_properties(text)
    if not transcribed:
        return [
            f"{MAIN_TF}: could not parse `locals.tool_schema_properties` at all — either the "
            "block is empty or this gate's parser needs updating to match a structural change."
        ]

    violations: list[str] = []
    expected_names = {tool.name for tool in ALL_TOOLS}
    transcribed_names = set(transcribed)

    missing = expected_names - transcribed_names
    extra = transcribed_names - expected_names
    if missing:
        violations.append(
            f"{MAIN_TF}: `locals.tool_schema_properties` is missing tool(s): {sorted(missing)}"
        )
    if extra:
        violations.append(
            f"{MAIN_TF}: `locals.tool_schema_properties` declares tool(s) absent from ALL_TOOLS: "
            f"{sorted(extra)}"
        )

    for tool in ALL_TOOLS:
        declared = transcribed.get(tool.name)
        if declared is None:
            continue  # already reported above
        expected = _toolspec_schema_properties(tool)

        missing_props = sorted(set(expected) - set(declared))
        extra_props = sorted(set(declared) - set(expected))
        if missing_props:
            violations.append(
                f"{MAIN_TF}: tool_schema_properties[{tool.name!r}] is missing property(ies) "
                f"declared by ALL_TOOLS: {missing_props}"
            )
        if extra_props:
            violations.append(
                f"{MAIN_TF}: tool_schema_properties[{tool.name!r}] declares property(ies) absent "
                f"from ALL_TOOLS: {extra_props}"
            )

        for name in sorted(set(expected) & set(declared)):
            if declared[name]["type"] != expected[name]["type"]:
                violations.append(
                    f"{MAIN_TF}: tool_schema_properties[{tool.name!r}][{name!r}].type = "
                    f"{declared[name]['type']!r} but ALL_TOOLS declares "
                    f"{expected[name]['type']!r}"
                )
            if declared[name]["required"] != expected[name]["required"]:
                violations.append(
                    f"{MAIN_TF}: tool_schema_properties[{tool.name!r}][{name!r}].required = "
                    f"{declared[name]['required']!r} but ALL_TOOLS declares "
                    f"{expected[name]['required']!r}"
                )
            if declared[name]["description"] != expected[name]["description"]:
                violations.append(
                    f"{MAIN_TF}: tool_schema_properties[{tool.name!r}][{name!r}].description "
                    "diverges from ALL_TOOLS"
                )

    return violations


def check_infra_transcription() -> list[str]:
    """Diff `infra/main.tf`'s hand-transcribed tool inventory against `ALL_TOOLS`.

    Compares `locals.tools`' name set, handler paths and descriptions, then
    `locals.tool_schema_properties` per property, including `type` and `required`.
    """
    if not MAIN_TF.exists():
        return [f"{MAIN_TF} not found — cannot verify the infra toolspec transcription."]

    text = MAIN_TF.read_text(encoding="utf-8")
    transcribed = _parse_tf_locals_tools(text)

    violations: list[str] = []

    if not transcribed:
        violations.append(
            f"{MAIN_TF}: could not parse `locals.tools` at all — either the block is empty or "
            "the parity gate's parser needs updating to match a structural change."
        )
        return violations

    expected_names = {tool.name for tool in ALL_TOOLS}
    transcribed_names = set(transcribed)

    missing_from_tf = expected_names - transcribed_names
    extra_in_tf = transcribed_names - expected_names
    if missing_from_tf:
        violations.append(
            f"{MAIN_TF}: `locals.tools` is missing tool(s) present in ALL_TOOLS: "
            f"{sorted(missing_from_tf)}"
        )
    if extra_in_tf:
        violations.append(
            f"{MAIN_TF}: `locals.tools` declares tool(s) absent from ALL_TOOLS: "
            f"{sorted(extra_in_tf)}"
        )

    for tool in ALL_TOOLS:
        entry = transcribed.get(tool.name)
        if entry is None:
            continue  # already reported above
        if entry["handler"] != tool.handler:
            violations.append(
                f"{MAIN_TF}: locals.tools[{tool.name!r}].handler = {entry['handler']!r} "
                f"but ALL_TOOLS declares handler = {tool.handler!r}"
            )
        if entry["description"] != tool.description:
            violations.append(
                f"{MAIN_TF}: locals.tools[{tool.name!r}].description diverges from "
                f"ALL_TOOLS[{tool.name!r}].description"
            )

    violations += _check_tool_schema_property_parity(text)

    return violations


def _sample_value(subschema: dict[str, Any]) -> Any:
    """Return a value satisfying `subschema`, for building a schema-valid baseline payload."""
    enum = subschema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    declared = subschema.get("type")
    if declared == "object":
        return {}
    if declared == "boolean":
        return True
    if declared in {"integer", "number"}:
        lower = subschema.get("minimum", 1)
        upper = subschema.get("maximum")
        value = lower if isinstance(lower, int | float) else 1
        if isinstance(upper, int | float) and value > upper:
            value = upper
        return value
    return "x" * max(1, int(subschema.get("minLength", 1) or 1))


#: Per-tool baselines for tools whose handler validates a nested closed union in its own body, where
#: the generated `{}` baseline is schema-valid but rejected by the handler. Each baseline must still
#: pass `validate_arguments` and be accepted by the handler.
SEMANTIC_BASELINE_OVERRIDES: dict[str, dict[str, Any]] = {
    # `page_context` is a closed discriminated union enforced by
    # `crm.toolspec.page_context.parse_page_context`; `board` is the minimal well-formed descriptor.
    "get_page_context": {"page_context": {"kind": "board", "route": "/pipeline"}},
}


def _baseline_payload(tool: ToolDefinition) -> dict[str, Any]:
    """Return the minimal schema-valid argument mapping for `tool` — every required property only.

    A tool listed in :data:`SEMANTIC_BASELINE_OVERRIDES` gets its declared baseline instead, deep
    copied so a probe mutating its payload cannot affect a later iteration.
    """
    override = SEMANTIC_BASELINE_OVERRIDES.get(tool.name)
    if override is not None:
        return copy.deepcopy(override)
    properties = tool.input_schema.get("properties") or {}
    required = tool.input_schema.get("required") or []
    return {
        name: _sample_value(properties[name])
        for name in required
        if isinstance(properties.get(name), dict)
    }


def _schema_without(schema: dict[str, Any], path: tuple[str, ...], keyword: str) -> dict[str, Any]:
    """Return a deep copy of `schema` with the `keyword` at `path` removed.

    `path` is the empty tuple for a keyword on the root object schema, or `("properties", name)`
    for one on a declared property.
    """
    pruned = copy.deepcopy(schema)
    node: Any = pruned
    for part in path:
        node = node[part]
    node.pop(keyword, None)
    return pruned


def _keyword_instances(tool: ToolDefinition) -> list[tuple[tuple[str, ...], str, Any]]:
    """Enumerate every restrictive instance of the validator-only keywords in `tool`'s schema.

    Returns:
        `(path, keyword, keyword_value)` triples, where `path` locates the schema node carrying the
        keyword. A permissive `additionalProperties: true` is excluded — it restricts nothing, so
        no violating payload exists for it.
    """
    instances: list[tuple[tuple[str, ...], str, Any]] = []
    root = tool.input_schema
    for keyword in sorted(SCHEMA_KEYWORDS_ENFORCED_HERE):
        if keyword in root and not (
            keyword == "additionalProperties" and root[keyword] is not False
        ):
            instances.append(((), keyword, root[keyword]))
    for name, subschema in sorted((root.get("properties") or {}).items()):
        if not isinstance(subschema, dict):
            continue
        for keyword in sorted(SCHEMA_KEYWORDS_ENFORCED_HERE):
            if keyword not in subschema:
                continue
            if keyword == "additionalProperties" and subschema[keyword] is not False:
                continue
            instances.append((("properties", name), keyword, subschema[keyword]))
    return instances


def _violating_payload(
    baseline: dict[str, Any], path: tuple[str, ...], keyword: str, keyword_value: Any
) -> dict[str, Any] | None:
    """Return a payload violating exactly the `keyword` instance at `path`, or None if unsupported."""
    payload = dict(baseline)
    if not path:
        if keyword == "additionalProperties":
            payload["__undeclared_property__"] = "x"
            return payload
        return None
    name = path[1]
    if keyword == "additionalProperties":
        payload[name] = {"__undeclared_property__": "x"}
        return payload
    if keyword == "enum":
        payload[name] = "__not_a_permitted_enum_member__"
        return payload
    if keyword == "minLength":
        payload[name] = "y" * max(0, int(keyword_value) - 1)
        return payload
    if keyword == "maxLength":
        payload[name] = "y" * (int(keyword_value) + 1)
        return payload
    if keyword == "minimum":
        payload[name] = keyword_value - 1
        return payload
    if keyword == "maximum":
        payload[name] = keyword_value + 1
        return payload
    return None


class _StoreTouchedError(Exception):
    """Raised by :class:`_ExplodingStore` when a handler body touches the store."""


class _ExplodingStore:
    """Store double raising :class:`_StoreTouchedError` on any attribute access.

    Injected into a handler probe so a handler body that runs despite invalid arguments announces
    itself instead of reaching a real AWS client.
    """

    def __getattr__(self, name: str) -> Any:
        raise _StoreTouchedError(f"store.{name}")


def _error_code(response: Any) -> str | None:
    """Return the `error.code` of a flat handler response, or None if it is not an error shape."""
    if not isinstance(response, dict):
        return None
    error = response.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def _probe_handler_enforcement(
    tool: ToolDefinition, handler: Any, baseline: dict[str, Any]
) -> str | None:
    """Drive `handler` end to end and return a violation message, or None when it enforces.

    Two invocations of the real Lambda entry point, both with a store double that raises on any
    attribute access so no AWS client is constructed:

    1. `baseline` plus one undeclared property — must come back as `validation_failed`; a raise or
       any other response is reported.
    2. `baseline` alone — must **not** come back as `validation_failed`.

    Args:
        tool: The tool whose handler is being probed, for the violation message only.
        handler: The module-level `handler(event, context, *, store=...)` entry point.
        baseline: A schema-valid argument mapping for `tool`.

    Returns:
        A violation message, or None when both properties hold.
    """
    violating = dict(baseline)
    violating[_UNDECLARED_PROBE_KEY] = "x"
    try:
        response = handler(
            identity_event(IDENTITY_KEY, _PROBE_IDENTITY, violating), None, store=_ExplodingStore()
        )
    except BaseException as exc:  # noqa: BLE001 - the escape itself is the finding
        return (
            f"{tool.name!r}: invoking {tool.handler} with an argument mapping its own "
            f"`input_schema` rejects raised {type(exc).__name__}: {exc} instead of returning a "
            "`validation_failed` response. `crm.toolspec.validation.validate_arguments` is not "
            "reached on this handler's path — the call is missing, neutered, or bypassed"
        )
    code = _error_code(response)
    if code != "validation_failed":
        return (
            f"{tool.name!r}: invoking {tool.handler} with an argument mapping its own "
            f"`input_schema` rejects returned error code {code!r}, not 'validation_failed'. "
            "`crm.toolspec.validation.validate_arguments` is not reached on this handler's path"
        )

    try:
        accepted = handler(
            identity_event(IDENTITY_KEY, _PROBE_IDENTITY, dict(baseline)),
            None,
            store=_ExplodingStore(),
        )
    except BaseException:  # noqa: BLE001 - reaching the body is the attribution this probe wants
        return None
    if _error_code(accepted) == "validation_failed":
        return (
            f"{tool.name!r}: {tool.handler} rejects even the schema-valid baseline payload with "
            "'validation_failed', so the rejection of the violating payload is not attributable "
            "to the undeclared property — this probe is not evidence the validator is reached"
        )
    return None


def check_handler_validator_coverage() -> list[str]:
    """Verify consumer 3 — the per-handler input validator — is wired and actually enforcing.

    Four properties, each executed:

    1. Every `ToolDefinition.handler` imports and exposes a callable `handler` attribute.
    2. The validator's compiled schema is the `ToolDefinition.input_schema` object, not a copy.
    3. Every restrictive instance of `additionalProperties`/`enum`/`minLength`/`maxLength`/`minimum`/`maximum`
       rejects a violating payload, and the same payload passes once that one keyword is removed
       from a copy of the schema.
    4. Every handler, driven end to end through its own Lambda entry point, answers an argument
       mapping its `input_schema` rejects with `validation_failed`, and does not answer the
       schema-valid baseline the same way (:func:`_probe_handler_enforcement`).

    Returns:
        A list of violation messages; empty when all four hold, the executed instance counts match
        `EXPECTED_KEYWORD_INSTANCES`, and every tool's handler path was proved.
    """
    violations: list[str] = []
    executed = dict.fromkeys(EXPECTED_KEYWORD_INSTANCES, 0)
    permissive_additional_properties = 0
    enforcing_handler_paths = 0

    for tool in ALL_TOOLS:
        module_path, _, attribute = tool.handler.rpartition(".")
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 - the failure itself is the finding
            violations.append(
                f"ALL_TOOLS[{tool.name!r}].handler = {tool.handler!r} but importing "
                f"{module_path!r} failed ({type(exc).__name__}: {exc}), so no input validator is "
                "wired for this tool"
            )
            continue
        entry_point = getattr(module, attribute, None)
        if not callable(entry_point):
            violations.append(
                f"ALL_TOOLS[{tool.name!r}].handler = {tool.handler!r} but {module_path!r} exposes "
                f"no callable {attribute!r}"
            )
            continue

        if validator_for(tool).schema is not tool.input_schema:
            violations.append(
                f"the validator for {tool.name!r} does not hold ALL_TOOLS' own input_schema object; "
                "a copy can drift from the source of truth"
            )

        baseline = _baseline_payload(tool)
        try:
            validate_arguments(tool, baseline)
        except ValidationFailed as exc:
            violations.append(
                f"{tool.name!r}: the minimal required-properties-only payload this gate builds is "
                f"itself rejected by the validator ({exc.detail}); the gate cannot prove keyword "
                "enforcement until its sample-value builder and the schema agree"
            )
            continue

        unenforced = _probe_handler_enforcement(tool, entry_point, baseline)
        if unenforced is not None:
            violations.append(unenforced)
        else:
            enforcing_handler_paths += 1

        subschemas = tool.input_schema.get("properties") or {}
        permissive_additional_properties += sum(
            1
            for subschema in subschemas.values()
            if isinstance(subschema, dict) and subschema.get("additionalProperties") is True
        )

        for path, keyword, keyword_value in _keyword_instances(tool):
            location = f"{tool.name}.{path[1]}" if path else tool.name
            payload = _violating_payload(baseline, path, keyword, keyword_value)
            if payload is None:
                violations.append(
                    f"{location}: this gate cannot construct a payload violating {keyword!r}, so "
                    "that instance is unproven — extend `_violating_payload`"
                )
                continue
            try:
                validate_arguments(tool, payload)
            except ValidationFailed:
                pass
            else:
                violations.append(
                    f"{location}: the validator ACCEPTED a payload violating {keyword} = "
                    f"{keyword_value!r}; that keyword is enforced nowhere"
                )
                continue
            relaxed = _schema_without(tool.input_schema, path, keyword)
            if list(validator_for(tool).evolve(schema=relaxed).iter_errors(payload)):
                violations.append(
                    f"{location}: the probe payload for {keyword!r} is rejected even with that "
                    "keyword removed, so the rejection is not attributable to it — the probe is "
                    "not evidence that keyword is enforced"
                )
                continue
            executed[keyword] += 1

    for keyword, expected in sorted(EXPECTED_KEYWORD_INSTANCES.items()):
        if executed[keyword] != expected:
            violations.append(
                f"proved {executed[keyword]} enforced instance(s) of {keyword!r}, expected "
                f"{expected}. Update EXPECTED_KEYWORD_INSTANCES only alongside a deliberate schema "
                "change — a drop here means a constraint is declared and enforced nowhere."
            )
    if permissive_additional_properties != EXPECTED_PERMISSIVE_ADDITIONAL_PROPERTIES:
        violations.append(
            f"counted {permissive_additional_properties} permissive `additionalProperties: true` "
            f"instance(s), expected {EXPECTED_PERMISSIVE_ADDITIONAL_PROPERTIES}. A restrictive "
            "instance turning permissive would otherwise keep the executed count green."
        )
    if enforcing_handler_paths != len(ALL_TOOLS):
        violations.append(
            f"proved end-to-end validator enforcement on {enforcing_handler_paths} of "
            f"{len(ALL_TOOLS)} handler paths. Every tool's own Lambda entry point must reject an "
            "argument mapping its `input_schema` rejects; a shortfall means at least one path "
            "reaches its handler body with unvalidated arguments."
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Skip the npm/node regeneration step (Terraform + validator + name checks still run).",
    )
    args = parser.parse_args(argv)

    violations: list[str] = []
    violations += check_tool_names_unique()
    violations += check_display_label_coverage()
    violations += regenerate_frontend_types(skip=args.skip_frontend)
    violations += check_infra_transcription()
    violations += check_handler_validator_coverage()
    violations += check_validator_call_site_unconditional()

    if violations:
        print("check_toolspec_parity: VIOLATION(S) FOUND:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print("check_toolspec_parity: FAILED", file=sys.stderr)
        return 1

    print("check_toolspec_parity: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
