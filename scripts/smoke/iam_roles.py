# SPDX-License-Identifier: MIT
"""Layer 4 per-role IAM least-privilege smoke check.

For every execution role the composition creates — the action Lambdas, the Gateway identity
interceptor, the Cognito pre-token-generation trigger, and the eval functions — runs
`iam:SimulatePrincipalPolicy` across every function's actions on its tables: each role must be
allowed exactly its own grants and denied every action belonging to every other function.

Each non-own pair gets two rows: `simulate:<function>:<action>@<table>` (the action is refused)
and `explicit_deny:<function>:<action>@<table>` (a matching `Deny` statement refuses it; an
`implicitDeny` fails). Both are `pass`/`fail` only. `OWN_TABLE_DENIAL_PROBES` pairs get
`own_table_denied:<function>:<action>@<table>`, which requires `explicitDeny`. A grant `infra/main.tf` declares index-only
(`include_table_arn = false`) is simulated against its index ARN, and its own role gets one more row,
`index_only_base_table_denied:<function>:<action>@<table>`, asserting the base table is refused;
every other role must be refused that pair on both the index ARN and the base-table ARN. The own
index row sends the fixed `dynamodb:LeadingKeys` value in `INDEX_IN_SCOPE_LEADING_KEY`, and an own
`gsi_tenant` grant gets `index_scope_denied:<function>:<action>@<table>`, asserting
`OUT_OF_SCOPE_TENANT` is refused there.

An own table-level grant carrying a `leading_key_condition` is simulated with an in-scope
`dynamodb:LeadingKeys` value (`conditioned_own_keys`), never context-less; one the matrix declares
`leading_keys`/`key_prefix` with no such value fails its own row. Every conditioned own grant, sweep
roles included, also gets `conditioned_scope_denied:<function>:<action>@<table>`, asserting
`PROBE_FOREIGN_KEY` and each `scope_denied_keys` value is refused. A grant object, list item or
condition the readers cannot resolve raises `UnreadableTerraformGrantsError` and becomes a failing
`*_readable` (or `conditioned_scope_keys_derivable`) row. The conditioned tool and sweep grants read
from `infra/*.tf` must equal the matrix's `leading_keys`/`key_prefix` grants, or a
`conditioned_grants_parity` row fails.

A sweep grant carrying a `select_condition` (a required `dynamodb:Select` value) gets three rows per
resource and key: `extra_condition_allowed:<function>:<action>@<where>[<key>]` with each condition
satisfied (allowed), `extra_condition_denied:...[<key>,<variable>=<value>]` for each other value of
the variable (denied), and `extra_condition_absent_denied:...[<key>]` with the variable unset
(denied, the variable reported missing). Each sibling grant on the same resource with no extra
condition gets `extra_condition_sibling_allowed:...[<key>,<variable>=<value>]`, allowed with an
unsatisfying value, so the restriction binds only its own keys.

A role's own grants are `local.tool_dynamodb_grants` plus every named `lambda_action`
instantiation's `dynamodb_grants` across the root composition's `infra/*.tf` files (a literal
`function_name`, or `"${local.name_prefix}-<name>"`), and `table_action_role_matrix.json` is
asserted equal to them per role. Also asserts no role's policy carries a `dynamodb:*` (or bare `*`,
or `NotAction`) wildcard, and that a `dynamodb:LeadingKeys` condition is present on every statement
whose table supports it per `infra/modules/lambda_action/table_action_role_matrix.json`.

Statement resource ARNs are resolved through the deployed logical->physical table-name mapping, so
`table/crm-agent-evalreps` resolves to the matrix's `reps`; a DynamoDB Allow resolving to no table
this deployment owns is a `fail`, as is a `ForAllValues` LeadingKeys Allow that grants any action
outside `KEYED_DYNAMODB_ACTIONS`. An absent role, a missing or unrecognised evaluation result, a role
whose policies cannot be loaded (`leading_keys:policies_unreadable`), or an over-grant is a `fail`,
never `skip`. A matrix that `matrix_shape_problems` rejects is one failing `matrix_readable` row.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import re
import sys
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, NamedTuple

from scripts.hcl_text import blank_comments, blank_heredoc_bodies, blank_literals, same_shape
from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    run_main,
)

from crm.authz import DEMO_TENANT
from crm.store.keys import tenant_prefixed
from eval.runs import JOB_ROW_STATUS
from eval.tenancy import tenant_for_run
from eval.verification import Status

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "iam_roles"

#: The project's function -> role naming convention (`infra/main.tf`), pinned here since the
#: matrix file names functions, not yet-generated role names.
ROLE_NAME_TEMPLATE: Final[str] = "crm-agent-eval-{function}-role"

#: The deployed name prefix (`local.name_prefix` in `infra/main.tf`), stripped from a literal
#: `function_name` to recover its function key.
_NAME_PREFIX: Final[str] = "crm-agent-eval"

#: The only (role, table) grants on a LeadingKeys-capable table that may lack a
#: `dynamodb:LeadingKeys` condition, each with its reason. Any other such grant is a `fail`.
#:
#: The Cognito pre-token-generation trigger reads the `reps` row of whichever user is signing in, so
#: there is no static value a condition could be pinned to.
_LEADING_KEYS_ADMIN_EXCEPTIONS: Final[Mapping[tuple[str, str], str]] = {
    (
        "crm-agent-eval-pre-token-generation-role",
        "reps",
    ): (
        "administrative cross-rep read: the Cognito pre-token-generation trigger reads the "
        "signing-in user's own reps row, keyed on event['userName'], so no static LeadingKeys "
        "value exists and no IAM context key carries the authenticating user's identity into "
        "this role - RULED not_applicable: an accepted administrative cross-rep read, not a verified "
        "control"
    ),
}

#: The per-function DynamoDB grant matrix, read once at import time from a local file.
_MATRIX_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "modules"
    / "lambda_action"
    / "table_action_role_matrix.json"
)

#: The root composition's file, whose `local.tool_dynamodb_grants` is the intended grant set the
#: allow/deny expectations are derived from. `intended_grants_parity` asserts the matrix file equals
#: it per role.
_MAIN_TF_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "main.tf"

#: Functions the composition instantiates whose roles are neither read nor simulated here; must equal
#: `scripts/check_handler_grant_coverage.py`'s `PENDING_FUNCTIONS`.
PENDING_FUNCTIONS: Final[frozenset[str]] = frozenset()

#: The fixed `dynamodb:LeadingKeys` value an own index-only grant is simulated with, by index, in
#: the shape the code's own queries use.
#: Actions a role must be explicitly denied on a table it holds a narrower grant on, by
#: `(function, table)`: revert_audit_record may delete and index-query notes and nothing else, and
#: sweep_control seeds the five seed tables through `BatchWriteItem` only.
OWN_TABLE_DENIAL_PROBES: Final[Mapping[tuple[str, str], tuple[str, ...]]] = {
    ("revert_audit_record", "notes"): ("dynamodb:PutItem", "dynamodb:UpdateItem"),
    **{
        ("sweep-control", table): ("dynamodb:PutItem",)
        for table in ("accounts", "contacts", "opportunities", "activities", "notes")
    },
}

INDEX_IN_SCOPE_LEADING_KEY: Final[Mapping[str, str]] = {
    "gsi_tenant": tenant_for_run("smoke-scope-probe"),
    "gsi_status": JOB_ROW_STATUS,
    "gsi_account": tenant_prefixed(tenant_for_run("smoke-scope-probe"), "acct-smoke-probe"),
    "gsi_opportunity": tenant_prefixed(tenant_for_run("smoke-scope-probe"), "opp-smoke-probe"),
}

#: The index whose own grants also get an `index_scope_denied` row, and the tenant that row sends.
TENANT_INDEX: Final[str] = "gsi_tenant"
OUT_OF_SCOPE_TENANT: Final[str] = "smoke-x"


def _root_composition_text(path: Path = _MAIN_TF_PATH) -> str:
    """Every root `*.tf` file beside `path`, concatenated, with `${local.name_prefix}` rendered.

    The root composition spreads `lambda_action` instantiations and their grant locals over several
    files, and the newer ones name their function `"${local.name_prefix}-<name>"`.
    """
    texts = [p.read_text(encoding="utf-8") for p in sorted(path.parent.glob("*.tf"))]
    text = "\n".join(texts).replace("${local.name_prefix}", _NAME_PREFIX)
    return _expand_string_list_comprehensions(text)


#: A `locals` value that is a one-line list of quoted strings: `  tables = ["a", "b"]`.
_STRING_LIST_LOCAL_RE: Final[re.Pattern[str]] = re.compile(
    r'^  (?P<name>\w+)\s*=\s*\[(?P<items>(?:\s*"[^"]*"\s*,?)*)\s*\]\s*$', re.MULTILINE
)
#: `[for <var> in local.<list> : { ... }]`, on one line or several, whose object may nest one level.
_OBJECT_COMPREHENSION_RE: Final[re.Pattern[str]] = re.compile(
    r"\[\s*for\s+(?P<var>\w+)\s+in\s+local\.(?P<local>\w+)\s*:\s*"
    r"(?P<body>\{(?:[^{}]|\{[^{}]*\})*\})\s*\]"
)


def _expand_string_list_comprehensions(text: str) -> str:
    """Rewrite each grant comprehension over a string-list local as its literal objects.

    `[for table in local.sweep_task_tenant_tables : { table = table, ... }]` becomes one object per
    listed table with `table = "<name>"`, so the grant readers see quoted table names. A
    comprehension over any other local is left as written.
    """
    lists = {
        match.group("name"): re.findall(r'"([^"]*)"', match.group("items"))
        for match in _STRING_LIST_LOCAL_RE.finditer(text)
    }

    def expand(match: re.Match[str]) -> str:
        values = lists.get(match.group("local"))
        if values is None:
            return match.group(0)
        key = re.compile(rf"\b(table(?:_name)?\s*=\s*){re.escape(match.group('var'))}\b")
        objects = [
            key.sub(lambda m, v=value: f'{m.group(1)}"{v}"', match.group("body"))
            for value in values
        ]
        return "[" + ", ".join(objects) + "]"

    return _OBJECT_COMPREHENSION_RE.sub(expand, text)


_TOOL_GRANTS_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"^\s*tool_dynamodb_grants\s*=\s*\{\s*$")
_TOOL_GRANTS_FUNCTION_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s{4}(?P<function>\w+)\s*=\s*\[\s*$"
)
_TOOL_GRANTS_ENTRY_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\{\s*table\s*=\s*\"(?P<table>\w+)\"\s*,\s*actions\s*=\s*\[(?P<actions>[^\]]*)\]\s*\}\s*,?\s*$"
)
_QUOTED_ITEM_RE: Final[re.Pattern[str]] = re.compile(r'^"(?P<value>[^"]*)"$')


def _literal_strings(items: str, where: str) -> list[str]:
    """The quoted values of a list body, every item of which must be a string literal.

    Raises:
        UnreadableTerraformGrantsError: An item is a reference or expression (`local.x`), which a
            quoted-string scan would drop silently.
    """
    values: list[str] = []
    for item in (part.strip() for part in _split_top_level(items)):
        if not item:
            continue
        literal = _QUOTED_ITEM_RE.match(item)
        if literal is None:
            raise UnreadableTerraformGrantsError(
                f"{where}: list item {item!r} is not a string literal this reader can resolve"
            )
        values.append(literal.group("value"))
    return values


def _terraform_tool_grants(path: Path = _MAIN_TF_PATH) -> dict[str, set[tuple[str, str]]]:
    """Return `{function: {(table, action)}}` as the root composition itself computes it.

    Reads `local.tool_dynamodb_grants` out of `infra/main.tf`, not from the live IAM policies.

    Raises:
        UnreadableTerraformGrantsError: A non-blank, non-comment line inside a function's list is
            neither its closing `]` nor a one-line `{table, actions}` grant.
    """
    grants: dict[str, set[tuple[str, str]]] = {}
    in_block = False
    function: str | None = None
    # Comment lines are skipped, not blanked. Heredoc bodies are blanked (preserving line structure)
    # so a heredoc line equal to `  }` cannot end the block early.
    source = blank_heredoc_bodies(path.read_text(encoding="utf-8"))
    for raw_line in source.splitlines():
        if not in_block:
            if _TOOL_GRANTS_BLOCK_RE.match(raw_line):
                in_block = True
            continue
        stripped = raw_line.rstrip()
        if not stripped.strip() or stripped.lstrip().startswith(("#", "//")):
            continue
        if function is None:
            if stripped == "  }":
                break
            match = _TOOL_GRANTS_FUNCTION_RE.match(raw_line)
            if match is not None:
                function = match.group("function")
                grants.setdefault(function, set())
            continue
        if stripped.strip() == "]":
            function = None
            continue
        entry = _TOOL_GRANTS_ENTRY_RE.match(raw_line)
        where = f"local.tool_dynamodb_grants.{function}"
        if entry is None:
            raise UnreadableTerraformGrantsError(
                f"{where}: line {stripped.strip()!r} is not a one-line {{table, actions}} grant"
            )
        for action in _literal_strings(entry.group("actions"), where):
            grants[function].add((entry.group("table"), action))
    return grants


def _terraform_intended_grants(path: Path = _MAIN_TF_PATH) -> dict[str, set[tuple[str, str]]]:
    """Return `{function: {(table, action)}}` for **every** `lambda_action` instantiation.

    The union of two sources, both read from `infra/main.tf`: the `for_each` tool functions'
    `local.tool_dynamodb_grants`, and the `dynamodb_grants` of every literal-named `lambda_action`
    module block.
    """
    grants = _terraform_tool_grants(path)
    grants.update(_terraform_literal_module_grants(path))
    return grants


_MODULE_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"^module\s+\"[\w-]+\"\s*\{\s*$")
_LITERAL_FUNCTION_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*function_name\s*=\s*\"(?P<name>[\w-]+)\"\s*$"
)
_DYNAMODB_GRANTS_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"^\s*dynamodb_grants\s*=\s*\[\s*$")
_TABLE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^\s*table_name\s*=\s*\"(?P<table>\w+)\"\s*$")
_ACTIONS_RE: Final[re.Pattern[str]] = re.compile(r"^\s*actions\s*=\s*\[(?P<actions>[^\]]*)\]\s*$")

#: A `dynamodb_grants` supplied wholly by reference: `dynamodb_grants = [local.<name>]`.
_GRANTS_LOCAL_LIST_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*dynamodb_grants\s*=\s*\[(?P<locals>\s*local\.\w+\s*(?:,\s*local\.\w+\s*)*,?\s*)\]\s*$"
)
#: One `local.<name>` item on its own line inside a multi-line `dynamodb_grants = [` list.
_GRANTS_LOCAL_ITEM_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*local\.(?P<local>\w+)\s*,?\s*$"
)
#: A `dynamodb_grants` supplied as a bare reference: `dynamodb_grants = local.eval_trigger_eval_runs_grants`.
_GRANTS_LOCAL_BARE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*dynamodb_grants\s*=\s*local\.(?P<local>\w+)\s*$"
)
#: A `dynamodb_grants` declared empty on one line: `dynamodb_grants = []`. Registers the function
#: with no pairs, so its role is still simulated (and denied everything). Distinct from an unreadable
#: reference, which raises :class:`UnreadableTerraformGrantsError`.
_GRANTS_EMPTY_LIST_RE: Final[re.Pattern[str]] = re.compile(r"^\s*dynamodb_grants\s*=\s*\[\s*\]\s*$")
#: A `dynamodb_grants` built by comprehension over a local list:
#: `dynamodb_grants = [` / `for grant in local.sweep_control_dynamodb_grants : {`.
_GRANTS_FOR_LOCAL_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*for\s+\w+\s+in\s+local\.(?P<local>\w+)\s*:"
)
#: A `table`, `table_name` or `actions` key anywhere on a line inside a `dynamodb_grants = [` list.
_WALKER_GRANT_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r'"?\b(?:table(?:_name)?|actions)"?\s*[=:](?!=)'
)
#: A comprehension body's pass-through line: `table_name = grant.table`, `actions = grant.actions`.
_WALKER_PASSTHROUGH_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:table(?:_name)?|actions)\s*=\s*\w+\.(?:table(?:_name)?|actions)\s*,?\s*$"
)
#: One `{table|table_name = "x", ... actions = [...]}` grant object inside a `locals` value. Both key
#: spellings occur: `local.eval_trigger_eval_runs_grants` uses `table_name`,
#: `local.sweep_control_dynamodb_grants` uses `table`.
_LOCAL_GRANT_OBJECT_RE: Final[re.Pattern[str]] = re.compile(
    r"\{[^{}]*?\btable(?:_name)?\s*=\s*\"(?P<table>\w+)\"[^{}]*?"
    r"\bactions\s*=\s*\[(?P<actions>[^\]]*)\]",
    re.DOTALL,
)


def _terraform_local_value_spans(text: str) -> dict[str, tuple[int, int]]:
    """Return `{name: (start, end)}` offsets of every two-space-indented list, object or `concat(...)`.

    Module arguments match too; a repeated name keeps its last occurrence.

    Brackets are counted on a literal- and comment-blanked copy, since a brace inside a quoted string,
    heredoc or comment is not structure; the offsets index `text` and its blanked copy alike.
    """
    scanned = blank_comments(blank_literals(text))
    if not same_shape(text, scanned):
        raise RuntimeError("hcl_text must preserve offsets for the slice below to be valid")
    spans: dict[str, tuple[int, int]] = {}
    for assignment in re.finditer(
        r"^  (?P<name>\w+)\s*=\s*(?:concat)?(?P<open>[\[{(])", scanned, re.MULTILINE
    ):
        opener = assignment.group("open")
        closer = {"[": "]", "{": "}", "(": ")"}[opener]
        depth = 0
        end = -1
        for index in range(assignment.start("open"), len(scanned)):
            if scanned[index] in "[{(":
                depth += 1
            elif scanned[index] in "]})":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1 or scanned[end] != closer:
            continue
        spans[assignment.group("name")] = (assignment.start("open"), end + 1)
    return spans


def _terraform_local_grant_definitions(text: str) -> dict[str, set[tuple[str, str]]]:
    """Return `{local name: {(table, action)}}` for every `locals` value holding grant objects.

    The root composition expresses non-tool grants through `locals`: a bare reference
    (`local.eval_trigger_eval_runs_grants`) or a comprehension over one
    (`local.sweep_control_dynamodb_grants`).

    Resolution is shallow: a named value whose text contains grant objects, matched where it is
    defined. It resolves no interpolation and evaluates no expression. A value with an unreadable
    grant object is omitted (:func:`_local_grant_definitions_and_defects` gives the reason).
    """
    return _local_grant_definitions_and_defects(text)[0]


def _local_grant_definitions_and_defects(
    text: str,
) -> tuple[dict[str, set[tuple[str, str]]], dict[str, str]]:
    """`(definitions, defects)`: each readable value's pairs, and why each other one was omitted.

    A value is omitted when an actions list holds a non-literal item, or when it holds an object
    naming a `table`/`table_name` that the grant pattern does not read.
    """
    decommented = blank_comments(text)
    scanned = blank_comments(blank_literals(text))
    definitions: dict[str, set[tuple[str, str]]] = {}
    defects: dict[str, str] = {}
    for name, (start, end) in _terraform_local_value_spans(text).items():
        # Grant pairs are read from the comment-blanked text, not the literal-blanked copy, so
        # quoted table names survive.
        matches = list(_LOCAL_GRANT_OBJECT_RE.finditer(decommented[start:end]))
        grant_like = sum(
            1
            for span in _GRANT_OBJECT_SPAN_RE.finditer(scanned, start, end)
            if _GRANT_KEY_RE.search(span.group(0))
        )
        try:
            pairs = {
                (match.group("table"), action)
                for match in matches
                for action in _literal_strings(match.group("actions"), f"local.{name}")
            }
        except UnreadableTerraformGrantsError as exc:
            defects[name] = str(exc)
            continue
        if len(matches) < grant_like:
            defects[name] = (
                f"local.{name}: {grant_like - len(matches)} of its {grant_like} grant object(s) "
                "carry no literal table and actions list"
            )
            continue
        if pairs:
            definitions[name] = pairs
    return definitions, defects


#: One grant object, tolerating one level of nested object (`leading_key_condition = { ... }`).
_GRANT_OBJECT_SPAN_RE: Final[re.Pattern[str]] = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}")
_GRANT_KEY_RE: Final[re.Pattern[str]] = re.compile(r"\btable(?:_name)?\s*=")
_INDEX_ONLY_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"\binclude_table_arn\s*=\s*false\b")
_OBJECT_TABLE_RE: Final[re.Pattern[str]] = re.compile(r"\btable(?:_name)?\s*=\s*\"(?P<table>\w+)\"")
_OBJECT_ACTIONS_RE: Final[re.Pattern[str]] = re.compile(r"\bactions\s*=\s*\[(?P<list>[^\]]*)\]")
_OBJECT_INDEX_NAMES_RE: Final[re.Pattern[str]] = re.compile(
    r"\bindex_names\s*=\s*\[(?P<list>[^\]]*)\]"
)
#: An index named directly in `index_arns`: `module.dynamodb.gsi_arns["eval_runs"]["gsi_status"]`.
_OBJECT_GSI_ARN_RE: Final[re.Pattern[str]] = re.compile(
    r"gsi_arns\[\"\w+\"\]\[\"(?P<index>\w+)\"\]"
)


class _LocalIndexGrants(NamedTuple):
    """One `locals` value's grant objects, split by whether their Allow covers the table ARN."""

    index_only: dict[tuple[str, str], list[str]]
    table_inclusive: set[tuple[str, str]]
    index_only_objects: int


def _grant_object_shape(raw: str, where: str) -> tuple[str, list[str], list[str]]:
    """`(table, actions, index_names)` of one grant object, every value a literal.

    Raises:
        UnreadableTerraformGrantsError: The object has no literal table or no actions list, or a
            list item is not a string literal; an unread object is never skipped.
    """
    table = _OBJECT_TABLE_RE.search(raw)
    actions = _OBJECT_ACTIONS_RE.search(raw)
    if table is None or actions is None:
        raise UnreadableTerraformGrantsError(
            f"{where}: a grant object has no literal table or actions list: {' '.join(raw.split())!r}"
        )
    index_names = _OBJECT_INDEX_NAMES_RE.search(raw)
    return (
        table.group("table"),
        _literal_strings(actions.group("list"), f"{where} actions"),
        _literal_strings(index_names.group("list"), f"{where} index_names") if index_names else [],
    )


def _terraform_local_index_grants(
    text: str, strict: Collection[str] = ()
) -> dict[str, _LocalIndexGrants]:
    """Return each `locals` value's grant objects split into index-only and table-inclusive pairs.

    `index_only` maps `(table, action)` to its `index_names`, `table_inclusive` holds the pairs whose
    Allow also covers the table ARN, and `index_only_objects` counts the index-only objects read.

    A grant object carrying `include_table_arn = false` renders an Allow whose only resources are
    `table/<t>/index/<name>` for its `index_names` (`infra/modules/lambda_action/main.tf`).

    Raises:
        UnreadableTerraformGrantsError: A value named in `strict` holds an object
            :func:`_grant_object_shape` cannot read. Any other value's such object is not a grant.
    """
    scanned = blank_comments(blank_literals(text))
    decommented = blank_comments(text)
    result: dict[str, _LocalIndexGrants] = {}
    for name, (start, end) in _terraform_local_value_spans(text).items():
        index_only: dict[tuple[str, str], list[str]] = {}
        table_inclusive: set[tuple[str, str]] = set()
        objects = 0
        for span in _GRANT_OBJECT_SPAN_RE.finditer(scanned, start, end):
            raw = decommented[span.start() : span.end()]
            try:
                table, actions, names = _grant_object_shape(raw, f"local.{name}")
            except UnreadableTerraformGrantsError:
                if name in strict:
                    raise
                continue
            pairs = {(table, a) for a in actions}
            if _INDEX_ONLY_MARKER_RE.search(span.group(0)) is None:
                table_inclusive |= pairs
                continue
            objects += 1
            names += _OBJECT_GSI_ARN_RE.findall(raw)
            for pair in pairs:
                index_only.setdefault(pair, []).extend(names)
        if index_only or table_inclusive:
            result[name] = _LocalIndexGrants(index_only, table_inclusive, objects)
    return result


class ExtraCondition(NamedTuple):
    """One further condition of a grant object's statement: `test`, `variable`, `values`."""

    test: str
    variable: str
    values: tuple[str, ...]


class LocalGrant(NamedTuple):
    """One grant object of a `locals` grant list, with its `leading_key_condition` resolved.

    `key_values` holds the condition's values with every `local.*` reference resolved, and is `None`
    with `operator` when the object sets no condition. `extra_conditions` are the further
    conditions its statement ANDs with that one, read from the object's `select_condition`.
    """

    table: str
    actions: tuple[str, ...]
    index_names: tuple[str, ...]
    include_table_arn: bool
    operator: str | None
    key_values: tuple[str, ...] | None
    extra_conditions: tuple[ExtraCondition, ...] = ()


#: The `select_condition` key of a grant object: the `dynamodb:Select` values its statement requires.
_SELECT_CONDITION_KEY_RE: Final[re.Pattern[str]] = re.compile(r"\bselect_condition\s*=\s*")
#: Every value a supported extra-condition variable can take, from the DynamoDB API.
EXTRA_CONDITION_DOMAINS: Final[Mapping[str, tuple[str, ...]]] = {
    "dynamodb:Select": (
        "ALL_ATTRIBUTES",
        "ALL_PROJECTED_ATTRIBUTES",
        "SPECIFIC_ATTRIBUTES",
        "COUNT",
    ),
}


def _select_condition(
    raw: str,
    where: str,
    source: str,
    spans: Mapping[str, tuple[int, int]],
    strings: Mapping[str, str],
) -> tuple[ExtraCondition, ...]:
    """A grant object's `select_condition` (a list of string literals or a `local.*` list) as the
    `StringEquals dynamodb:Select` condition `modules/lambda_action` renders; `()` when absent or empty.

    Raises:
        UnreadableTerraformGrantsError: The value is not a list of string literals or a resolvable
            `local.*` list, or names a value outside `EXTRA_CONDITION_DOMAINS`.
    """
    key = _SELECT_CONDITION_KEY_RE.search(raw)
    if key is None:
        return ()
    rest = raw[key.end() :]
    if rest.startswith("["):
        close = _closing_bracket(rest, 0)
        if close == -1:
            raise UnreadableTerraformGrantsError(f"{where}: select_condition never closes")
        values = tuple(_literal_strings(rest[1:close], f"{where} select_condition"))
    else:
        reference = re.match(r"local\.\w+", rest)
        if reference is None:
            shown = rest.split("\n", 1)[0].strip()
            raise UnreadableTerraformGrantsError(
                f"{where}: select_condition = {shown!r} is not a list this reader can read"
            )
        values = tuple(_condition_values(reference.group(0), source, spans, strings))
    unknown = [v for v in values if v not in EXTRA_CONDITION_DOMAINS["dynamodb:Select"]]
    if unknown:
        raise UnreadableTerraformGrantsError(
            f"{where}: select_condition value(s) {unknown} are not dynamodb:Select values"
        )
    return (ExtraCondition("StringEquals", "dynamodb:Select", values),) if values else ()


#: `leading_key_condition` as an object key, bare or quoted, assigned with `=` or `:`. Searched on
#: comment-blanked text with literals intact, since literal blanking erases a quoted key.
_CONDITION_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r'(?P<key>\bleading_key_condition\b|"leading_key_condition")\s*[=:](?!=)\s*'
)
#: The whole value is `null`: followed by a `,`, a `}` or the end of the line.
_NULL_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"null[ \t]*(?:[,}\n]|$)")
_CONDITION_OPERATOR_RE: Final[re.Pattern[str]] = re.compile(r'\boperator\s*=\s*"(?P<op>\w+)"')
_CONDITION_VALUES_RE: Final[re.Pattern[str]] = re.compile(r"\bvalues\s*=\s*")
_OPERATOR_KEY_RE: Final[re.Pattern[str]] = re.compile(r'\boperator"?\s*[=:]')
#: `var.dynamodb_grants[*].leading_key_condition.operator`'s default in `modules/lambda_action`.
_DEFAULT_CONDITION_OPERATOR: Final[str] = "StringEquals"
_MAP_ENTRY_RE: Final[re.Pattern[str]] = re.compile(r'^\s*(?P<key>\w+)\s*=\s*"(?P<value>[^"]*)"\s*$')
#: `  name = join("", [for i in range(N) : "c"])`: a string of `N` copies of `c`.
_REPEAT_STRING_LOCAL_RE: Final[re.Pattern[str]] = re.compile(
    r'^  (?P<name>\w+)\s*=\s*join\(""\s*,\s*\[for\s+\w+\s+in\s+range\((?P<count>\d+)\)\s*:\s*'
    r'"(?P<char>[^"]*)"\s*\]\)\s*$',
    re.MULTILINE,
)
_STRING_LOCAL_RE: Final[re.Pattern[str]] = re.compile(
    r'^  (?P<name>\w+)\s*=\s*"(?P<value>[^"$]*)"\s*$', re.MULTILINE
)
_REFERENCE_RE: Final[re.Pattern[str]] = re.compile(r'^local\.(?P<name>\w+)(?:\["(?P<key>\w+)"\])?$')


def _string_locals(text: str) -> dict[str, str]:
    """Every one-line string `locals` value, plus each `join("", [for … range(N) : "c"])` value."""
    values = {m.group("name"): m.group("value") for m in _STRING_LOCAL_RE.finditer(text)}
    for match in _REPEAT_STRING_LOCAL_RE.finditer(text):
        values[match.group("name")] = match.group("char") * int(match.group("count"))
    return values


def _resolve_reference(
    reference: str, text: str, spans: Mapping[str, tuple[int, int]], strings: Mapping[str, str]
) -> list[str]:
    """`local.<list>`, `local.<string>` or `local.<map>["<key>"]` resolved to its string values."""
    match = _REFERENCE_RE.match(reference)
    if match is None:
        raise UnreadableTerraformGrantsError(
            f"unsupported condition value expression {reference!r}"
        )
    name, key = match.group("name"), match.group("key")
    if key is None and name in strings:
        return [strings[name]]
    if name not in spans:
        raise UnreadableTerraformGrantsError(f"local.{name} is not a readable string, list or map")
    body = _local_definition_text(text, name, spans)
    if key is None:
        if not body.startswith("["):
            raise UnreadableTerraformGrantsError(f"local.{name} is not a string list")
        return _literal_strings(body.strip()[1:-1], f"local.{name}")
    entries = {
        m.group("key"): m.group("value")
        for line in body.strip("{}").splitlines()
        if (m := _MAP_ENTRY_RE.match(line))
    }
    if key not in entries:
        raise UnreadableTerraformGrantsError(f'local.{name}["{key}"] is not a string entry')
    return [entries[key]]


def _values_expression(body: str) -> str | None:
    """The expression after `values =`: a bracketed list (nesting counted) or a bare reference."""
    match = _CONDITION_VALUES_RE.search(body)
    if match is None:
        return None
    rest = body[match.end() :]
    if not rest.startswith("["):
        reference = re.match(r'local\.\w+(?:\["\w+"\])?', rest)
        return reference.group(0) if reference else None
    depth = 0
    for index, char in enumerate(rest):
        depth += {"[": 1, "]": -1}.get(char, 0)
        if depth == 0:
            return rest[: index + 1]
    return None


def _split_top_level(items: str) -> list[str]:
    """`items` split on commas outside brackets."""
    parts, depth, current = [], 0, ""
    for char in items:
        depth += {"[": 1, "]": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    return [*parts, current]


def _condition_values(
    expr: str, text: str, spans: Mapping[str, tuple[int, int]], strings: Mapping[str, str]
) -> list[str]:
    values: list[str] = []
    items = [expr] if not expr.startswith("[") else _split_top_level(expr[1:-1])
    for item in (part.strip() for part in items):
        if not item:
            continue
        if item.startswith('"') and item.endswith('"'):
            values.append(item[1:-1])
        else:
            values.extend(_resolve_reference(item, text, spans, strings))
    interpolated = [value for value in values if "${" in value]
    if interpolated:
        raise UnreadableTerraformGrantsError(
            f"condition value(s) {interpolated} interpolate an expression this reader does not resolve"
        )
    return values


def terraform_local_grants(name: str, text: str | None = None) -> list[LocalGrant]:
    """Every grant object of `local.<name>` across the root composition, conditions resolved.

    `text` replaces the root composition's source; the composition's own result is read once per
    process.

    Raises:
        UnreadableTerraformGrantsError: The local is absent, holds no grant object, or a condition
            value cannot be resolved; an unread list is never treated as empty.
    """
    if text is None:
        return list(_composition_local_grants(name))
    return _local_grants(name, _expand_string_list_comprehensions(text))


@functools.cache
def _composition_local_grants(name: str) -> tuple[LocalGrant, ...]:
    return tuple(_local_grants(name, _root_composition_text()))


def _local_grants(name: str, source: str) -> list[LocalGrant]:
    spans = _terraform_local_value_spans(source)
    if name not in spans:
        raise UnreadableTerraformGrantsError(f"local.{name} is not defined as a list or concat")
    start, end = spans[name]
    grants = _grants_in_span(source, start, end, f"local.{name}")
    if not grants:
        raise UnreadableTerraformGrantsError(f"local.{name} holds no grant object")
    return grants


def _condition_key(scanned: str, decommented: str, start: int, end: int) -> re.Match[str] | None:
    """The first `leading_key_condition` key in `[start, end)` that is code, not string content."""
    for key in _CONDITION_KEY_RE.finditer(decommented, start, end):
        written = key.group("key")
        blanked = scanned[key.start() : key.start() + len(written)]
        if blanked == written or blanked == f'"{" " * (len(written) - 2)}"':
            return key
    return None


def _condition_body(scanned: str, decommented: str, start: int, end: int, where: str) -> str | None:
    """The body of the object's `leading_key_condition = { ... }`, or `None` if absent or `null`.

    The key may be quoted and assigned with `:`. `null` counts only as the whole value.

    Raises:
        UnreadableTerraformGrantsError: The condition is set to anything else (a reference, a call,
            an expression starting with `null`), or its object never closes; such a grant is never
            read as unconditioned.
    """
    key = _condition_key(scanned, decommented, start, end)
    if key is None:
        return None
    if _NULL_VALUE_RE.match(scanned, key.end()):
        return None
    if scanned[key.end()] != "{":
        shown = decommented[key.end() : decommented.find("\n", key.end())].strip()
        raise UnreadableTerraformGrantsError(
            f"{where}: leading_key_condition = {shown!r} is not an object this reader can read"
        )
    close = _closing_bracket(scanned, key.end())
    if close == -1 or close >= end:
        raise UnreadableTerraformGrantsError(f"{where}: the leading_key_condition never closes")
    return decommented[key.end() + 1 : close]


def _grants_in_span(source: str, start: int, end: int, where: str) -> list[LocalGrant]:
    """Every grant object between offsets `start` and `end` of `source`, conditions resolved.

    An absent `operator` reads as `StringEquals`, the `lambda_action` variable's default.

    Raises:
        UnreadableTerraformGrantsError: An object, list item, operator or condition value is not
            readable as a literal or a resolvable `local.*` reference.
    """
    spans = _terraform_local_value_spans(source)
    strings = _string_locals(source)
    scanned = blank_comments(blank_literals(source))
    decommented = blank_comments(source)
    grants: list[LocalGrant] = []
    for span in _GRANT_OBJECT_SPAN_RE.finditer(scanned, start, end):
        raw = decommented[span.start() : span.end()]
        table, actions, index_names = _grant_object_shape(raw, where)
        body = _condition_body(
            scanned, decommented, span.start(), span.end(), f"{where} on {table}"
        )
        operator: str | None = None
        key_values: tuple[str, ...] | None = None
        if body is not None:
            op = _CONDITION_OPERATOR_RE.search(body)
            expr = _values_expression(body)
            if expr is None or (op is None and _OPERATOR_KEY_RE.search(body) is not None):
                raise UnreadableTerraformGrantsError(
                    f"{where}: a leading_key_condition on {table} has no readable operator or values"
                )
            operator = op.group("op") if op is not None else _DEFAULT_CONDITION_OPERATOR
            key_values = tuple(_condition_values(expr, source, spans, strings))
        grants.append(
            LocalGrant(
                table=table,
                actions=tuple(actions),
                index_names=tuple(index_names),
                include_table_arn=_INDEX_ONLY_MARKER_RE.search(span.group(0)) is None,
                operator=operator,
                key_values=key_values,
                extra_conditions=_select_condition(
                    raw, f"{where} on {table}", source, spans, strings
                ),
            )
        )
    return grants


#: The `locals` grant list behind each sweep role, whose conditions are read by
#: :func:`terraform_local_grants`.
SWEEP_GRANT_LOCALS: Final[Mapping[str, str]] = {
    "sweep-control": "sweep_control_dynamodb_grants",
    "sweep-task": "sweep_task_dynamodb_grants",
}

#: The fixed 32-hex run id an in-scope probe key is built from (`eval.tenancy.allocate_run_id`).
PROBE_RUN_ID: Final[str] = "0123456789abcdef0123456789abcdef"
#: A well-formed key no `leading_key_condition` value matches.
PROBE_FOREIGN_KEY: Final[str] = "00000000-0000-4000-8000-000000000000"


def _require_plain_pattern(value: str) -> None:
    if "[" in value or "]" in value:
        raise UnreadableTerraformGrantsError(
            f"condition value {value!r} uses a character class, which StringLike does not have"
        )


def condition_matches(value: str, key: str, operator: str) -> bool:
    """Whether `key` satisfies one condition `value` under `StringEquals` or `StringLike`."""
    if operator == "StringEquals":
        return key == value
    _require_plain_pattern(value)
    return fnmatch.fnmatchcase(key, value)


def in_scope_key(value: str) -> str:
    """A concrete key `value` admits: each `?` a run-id character, `*` → `<run>-0001`."""
    _require_plain_pattern(value)
    characters = iter(PROBE_RUN_ID * (1 + value.count("?") // len(PROBE_RUN_ID)))
    filled = "".join(next(characters) if char == "?" else char for char in value)
    return filled.replace("*", f"{PROBE_RUN_ID}-0001")


def out_of_scope_keys(values: Sequence[str], operator: str) -> tuple[str, ...]:
    """Keys none of `values` admits: a foreign uuid, a `demo` twin of each `eval` value, and a
    minted-id twin (`act-<uuid>` for `act-eval-*`) of each value with a prefix before `eval`.

    Raises:
        UnreadableTerraformGrantsError: A candidate key is admitted after all, so a deny row built
            from it would assert nothing.
    """
    candidates = [PROBE_FOREIGN_KEY]
    candidates += [
        value.replace("eval", DEMO_TENANT, 1).replace("*", "0000")
        for value in values
        if "eval" in value
    ]
    candidates += [
        f"{value.partition('eval')[0]}{PROBE_FOREIGN_KEY}"
        for value in values
        if "eval" in value and value.partition("eval")[0]
    ]
    keys = tuple(dict.fromkeys(candidates))
    admitted = [k for k in keys if any(condition_matches(v, k, operator) for v in values)]
    if admitted:
        raise UnreadableTerraformGrantsError(
            f"out-of-scope probe key(s) {admitted} are admitted by {list(values)}"
        )
    return keys


def asserted_dynamodb_actions() -> frozenset[str]:
    """`local.dynamodb_asserted_actions`: every tool grant's and sweep_control grant's actions."""
    tool = {action for pairs in _terraform_tool_grants().values() for _table, action in pairs}
    control = {
        action
        for grant in terraform_local_grants(SWEEP_GRANT_LOCALS["sweep-control"])
        for action in grant.actions
    }
    return frozenset(tool | control)


def conditioned_own_keys() -> dict[tuple[str, str, str], str]:
    """`{(function, table, action): in-scope key}` for every table-level conditioned own grant.

    Covers the sweep grants and every :func:`conditioned_tool_scopes` grant.
    """
    keys: dict[tuple[str, str, str], str] = {}
    for function, local in SWEEP_GRANT_LOCALS.items():
        for grant in terraform_local_grants(local):
            if grant.key_values is None or not grant.include_table_arn:
                continue
            for action in grant.actions:
                keys.setdefault((function, grant.table, action), in_scope_key(grant.key_values[0]))
    for triple, scopes in conditioned_tool_scopes().items():
        operator, values = scopes[0]
        keys.setdefault(
            triple, values[0] if operator == "StringEquals" else in_scope_key(values[0])
        )
    return keys


#: One `leading_key_condition` as `(operator, values)`.
ConditionScope = tuple[str, tuple[str, ...]]

_MODULE_HEADER_RE: Final[re.Pattern[str]] = re.compile(r'^module\s+"[\w-]+"\s*\{\s*$', re.MULTILINE)
_MODULE_GRANTS_RE: Final[re.Pattern[str]] = re.compile(r"^\s*dynamodb_grants\s*=\s*", re.MULTILINE)
_MODULE_SOURCE_RE: Final[re.Pattern[str]] = re.compile(
    r'^\s*source\s*=\s*"(?P<source>[^"]*)"\s*$', re.MULTILINE
)
_LAMBDA_ACTION_SOURCE: Final[str] = "./modules/lambda_action"
_MODULE_FUNCTION_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*function_name\s*=\s*(?P<value>.*?)\s*$", re.MULTILINE
)
_LITERAL_NAME_VALUE_RE: Final[re.Pattern[str]] = re.compile(r'^"(?P<name>[\w-]+)"$')
#: The one `lambda_action` block whose `function_name` is not literal: the `for_each` tool functions,
#: whose grants `_terraform_tool_grants` reads from `local.tool_dynamodb_grants`.
_TOOLS_FOR_EACH_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*for_each\s*=\s*local\.tools\s*$", re.M
)
_LOCAL_ITEM_RE: Final[re.Pattern[str]] = re.compile(r"^local\.(?P<local>\w+)$")
_COMPREHENSION_HEAD_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*for\s+(?P<var>\w+)\s+in\s+local\.(?P<local>\w+)\s*:\s*"
)
#: An object key opening a line, bare or quoted, assigned with `=` or `:`. Matched on literal-blanked
#: text, where a quoted key keeps its quotes and is blanked inside them.
_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r'[ \t]*(?P<key>\w+|"[^"\n]*")\s*[=:](?!=)\s*')
_BRACKET_DEPTH: Final[Mapping[str, int]] = {"[": 1, "{": 1, "(": 1, "]": -1, "}": -1, ")": -1}


def _closing_bracket(scanned: str, opener: int) -> int:
    """The offset of the bracket closing the one at `opener` in a literal-blanked text, else -1."""
    depth = 0
    for index in range(opener, len(scanned)):
        depth += _BRACKET_DEPTH.get(scanned[index], 0)
        if depth == 0:
            return index
    return -1


def _top_level_items(scanned: str, start: int, end: int) -> list[tuple[int, int]]:
    """`(start, end)` offsets of each comma-separated item in `[start, end)`, outside brackets."""
    items: list[tuple[int, int]] = []
    depth, item_start = 0, start
    for index in range(start, end):
        char = scanned[index]
        depth += _BRACKET_DEPTH.get(char, 0)
        if char == "," and depth == 0:
            items.append((item_start, index))
            item_start = index + 1
    return [*items, (item_start, end)]


def _object_assignments(scanned: str, decommented: str, start: int, end: int) -> dict[str, str]:
    """`{key: value}` of each assignment opening a line at the top level of `[start, end)`.

    Quoted keys are returned unquoted.

    Raises:
        UnreadableTerraformGrantsError: Non-blank text precedes the first assignment, or the
            region holds text but no assignment, so a key would be dropped unread.
    """
    assignments: dict[str, str] = {}
    current: tuple[str, int] | None = None
    depth, index, line_start = 0, start, True
    while index < end:
        assignment = (
            _ASSIGNMENT_RE.match(scanned, index, end) if depth == 0 and line_start else None
        )
        if assignment is not None:
            if current is not None:
                assignments[current[0]] = decommented[current[1] : index].strip().rstrip(",")
            elif decommented[start:index].strip():
                raise UnreadableTerraformGrantsError(
                    f"object text {decommented[start:index].strip()!r} is not a key assignment"
                )
            key = decommented[assignment.start("key") : assignment.end("key")].strip('"')
            current = (key, assignment.end())
            index, line_start = assignment.end(), False
            continue
        depth += _BRACKET_DEPTH.get(scanned[index], 0)
        line_start = scanned[index] == "\n"
        index += 1
    if current is not None:
        assignments[current[0]] = decommented[current[1] : end].strip().rstrip(",")
    elif decommented[start:end].strip():
        raise UnreadableTerraformGrantsError(
            f"object text {decommented[start:end].strip()!r} is not a key assignment"
        )
    return assignments


def _comprehension_grants(
    text: str, scanned: str, decommented: str, opener: int, closer: int, where: str
) -> list[LocalGrant] | None:
    """The grants of `[for <v> in local.<x> : { ... }]` as its body maps them, else `None`.

    The body must pass `table_name` and `actions` through unchanged. `include_table_arn`,
    `leading_key_condition` and `select_condition` either pass through (bare or under
    `try(..., default)`) or are absent, in which case the module default applies. `table_arn` and
    `index_arns` are not read.

    Raises:
        UnreadableTerraformGrantsError: The body sets any other key or transforms one of these, or
            an `if` filter or other text follows it.
    """
    head = _COMPREHENSION_HEAD_RE.match(decommented, opener + 1, closer)
    if head is None:
        return None
    var = head.group("var")
    body_open = head.end()
    body_close = _closing_bracket(scanned, body_open) if scanned[body_open] == "{" else -1
    if body_close == -1 or body_close > closer:
        raise UnreadableTerraformGrantsError(f"{where}: the comprehension body is not an object")
    trailing = decommented[body_close + 1 : closer].strip()
    if trailing:
        raise UnreadableTerraformGrantsError(
            f"{where}: text after the comprehension body ({trailing!r}) may filter its grants"
        )
    body = {
        key: re.sub(r"\s+", "", value)
        for key, value in _object_assignments(
            scanned, decommented, body_open + 1, body_close
        ).items()
    }
    passthrough = {
        "table_name": {f"{var}.table", f"{var}.table_name"},
        "actions": {f"{var}.actions"},
        "include_table_arn": {f"{var}.include_table_arn", f"try({var}.include_table_arn,true)"},
        "leading_key_condition": {
            f"{var}.leading_key_condition",
            f"try({var}.leading_key_condition,null)",
        },
        "select_condition": {f"{var}.select_condition", f"try({var}.select_condition,[])"},
    }
    for key, value in body.items():
        if key in ("table_arn", "index_arns"):
            continue
        if value not in passthrough.get(key, set()):
            raise UnreadableTerraformGrantsError(
                f"{where}: the comprehension maps {key} = {value!r}, which this reader does not "
                f"model; only a pass-through of {sorted(passthrough)} is read"
            )
    for required in ("table_name", "actions"):
        if required not in body:
            raise UnreadableTerraformGrantsError(f"{where}: the comprehension sets no {required}")
    return [
        grant._replace(
            include_table_arn=grant.include_table_arn if "include_table_arn" in body else True,
            operator=grant.operator if "leading_key_condition" in body else None,
            key_values=grant.key_values if "leading_key_condition" in body else None,
            extra_conditions=grant.extra_conditions if "select_condition" in body else (),
        )
        for grant in _local_grants(head.group("local"), text)
    ]


def _list_grants(
    text: str, scanned: str, decommented: str, opener: int, closer: int, where: str
) -> list[LocalGrant]:
    """The grants of a `[ ... ]` value: inline objects, `local.<x>` items, or one comprehension.

    Raises:
        UnreadableTerraformGrantsError: An item is neither an object nor a `local.<x>` reference, or
            is unreadable.
    """
    comprehension = _comprehension_grants(text, scanned, decommented, opener, closer, where)
    if comprehension is not None:
        return comprehension
    grants: list[LocalGrant] = []
    for item_start, item_end in _top_level_items(scanned, opener + 1, closer):
        item = decommented[item_start:item_end].strip()
        if not item:
            continue
        reference = _LOCAL_ITEM_RE.match(item)
        if reference is not None:
            grants.extend(_local_grants(reference.group("local"), text))
        elif item.startswith("{"):
            objects = _grants_in_span(text, item_start, item_end, where)
            if len(objects) != 1:
                raise UnreadableTerraformGrantsError(f"{where}: unreadable list item {item!r}")
            grants.extend(objects)
        else:
            raise UnreadableTerraformGrantsError(
                f"{where}: list item {item!r} is neither a grant object nor a local.<name>"
            )
    return grants


_TOOLS_SCOPE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r'"?\b(?:leading_key_condition|include_table_arn)"?\s*[=:](?!=)'
)
#: The one comprehension head the tools block's `dynamodb_grants` may open with.
_TOOLS_COMPREHENSION_HEAD_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*for\s+\w+\s+in\s+local\.tool_dynamodb_grants\[each\.key\]\s*:\s*"
)


def _require_unconditioned_tools_block(
    scanned: str, decommented: str, start: int, end: int
) -> None:
    """Assert the `for_each` tools block's `dynamodb_grants` set no condition or index-only flag.

    Its grants are read from `local.tool_dynamodb_grants` as unconditioned table-level pairs, so the
    value must be exactly `[for <v> in local.tool_dynamodb_grants[each.key] : { ... }]`. The one
    exception is an `include_table_arn` derived from `local.tool_query_indexes`.

    Raises:
        UnreadableTerraformGrantsError: The value is not that comprehension, or its body sets either
            key.
    """
    assignment = _MODULE_GRANTS_RE.search(scanned, start, end)
    if assignment is None:
        raise UnreadableTerraformGrantsError("the for_each tools block sets no dynamodb_grants")
    opener = assignment.end()
    closer = _closing_bracket(scanned, opener) if scanned.startswith("[", opener) else -1
    if closer == -1 or closer >= end:
        shown = decommented[opener : decommented.find("\n", opener)].strip()
        raise UnreadableTerraformGrantsError(
            f"the for_each tools block's dynamodb_grants = {shown!r} is not a closed list"
        )
    head = _TOOLS_COMPREHENSION_HEAD_RE.match(scanned, opener + 1, closer)
    body_open = head.end() if head is not None else -1
    body_close = _closing_bracket(scanned, body_open) if head and scanned[body_open] == "{" else -1
    if body_close == -1 or body_close > closer or scanned[body_close + 1 : closer].strip():
        raise UnreadableTerraformGrantsError(
            "the for_each tools block's dynamodb_grants is not exactly "
            "[for <v> in local.tool_dynamodb_grants[each.key] : { ... }], so its grants are not "
            "the local.tool_dynamodb_grants pairs this reader reads"
        )
    for found in _TOOLS_SCOPE_KEY_RE.finditer(decommented, opener, closer):
        line_end = decommented.find("\n", found.end())
        value = decommented[found.end() : closer if line_end == -1 else min(line_end, closer)]
        # The one permitted scoping: `include_table_arn` derived from `local.tool_query_indexes`,
        # which `terraform_tool_query_indexes` reads.
        if "include_table_arn" in found.group(0) and "local.tool_query_indexes" in value:
            continue
        raise UnreadableTerraformGrantsError(
            f"the for_each tools block's dynamodb_grants sets {found.group(0).rstrip(' =:')!r}, but "
            "its grants are read from local.tool_dynamodb_grants as unconditioned table-level pairs"
        )


def module_grants(text: str) -> dict[str, list[LocalGrant]]:
    """Every literal-named `lambda_action` block's `dynamodb_grants`, by function key.

    The value is read as `local.<name>`, as a list of grant objects and `local.<name>` items, or as
    a comprehension over `local.<name>` (:func:`_comprehension_grants`). A block whose `source` is
    another module is not read; a block with no `source` line is read as a `lambda_action`.

    Raises:
        UnreadableTerraformGrantsError: The `for_each` tools block sets a condition, or a
            `lambda_action` block other than it has no literal `<prefix>-<name>` `function_name` or no `dynamodb_grants`, its value
            has none of those forms, or one of its objects is unreadable (:func:`_grants_in_span`).
    """
    scanned = blank_comments(blank_literals(text))
    decommented = blank_comments(text)
    headers = list(_MODULE_HEADER_RE.finditer(decommented))
    result: dict[str, list[LocalGrant]] = {}
    for position, header in enumerate(headers):
        block_end = headers[position + 1].start() if position + 1 < len(headers) else len(text)
        block = decommented[header.end() : block_end]
        source = _MODULE_SOURCE_RE.search(block)
        if source is not None and source.group("source") != _LAMBDA_ACTION_SOURCE:
            continue
        name_value = _MODULE_FUNCTION_NAME_RE.search(block)
        if source is None and name_value is None:
            continue
        literal = _LITERAL_NAME_VALUE_RE.match(name_value.group("value")) if name_value else None
        if literal is None or not literal.group("name").startswith(f"{_NAME_PREFIX}-"):
            if _TOOLS_FOR_EACH_RE.search(block) is not None:
                _require_unconditioned_tools_block(scanned, decommented, header.end(), block_end)
                continue
            shown = name_value.group("value") if name_value else None
            raise UnreadableTerraformGrantsError(
                f"{text[header.start() : header.end()].strip()!r}: function_name {shown!r} is not a "
                f'literal "{_NAME_PREFIX}-<name>", so its grants would be read for no role'
            )
        function = literal.group("name")[len(_NAME_PREFIX) + 1 :]
        if function in PENDING_FUNCTIONS:
            continue
        where = f"{function}'s dynamodb_grants"
        assignment = _MODULE_GRANTS_RE.search(scanned, header.end(), block_end)
        if assignment is None:
            raise UnreadableTerraformGrantsError(
                f"{where}: the block sets no dynamodb_grants, which lambda_action requires"
            )
        line = decommented[assignment.end() : decommented.find("\n", assignment.end())].strip()
        reference = _LOCAL_ITEM_RE.match(line)
        if reference is not None:
            result[function] = _local_grants(reference.group("local"), text)
            continue
        if not line.startswith("["):
            raise UnreadableTerraformGrantsError(f"{where}: unsupported value {line!r}")
        opener = scanned.index("[", assignment.end())
        closer = _closing_bracket(scanned, opener)
        if closer == -1:
            raise UnreadableTerraformGrantsError(f"{where}: the list never closes")
        result[function] = _list_grants(text, scanned, decommented, opener, closer, where)
    return result


def conditioned_tool_scopes(
    text: str | None = None,
) -> dict[tuple[str, str, str], tuple[ConditionScope, ...]]:
    """`{(function, table, action): scopes}` for every non-sweep table-level conditioned grant.

    A pair is included only when every table-level object of the function covering it carries a
    `leading_key_condition`; `scopes` holds each such object's condition. `text` replaces the root
    composition's source, whose own result is read once per process.

    Raises:
        UnreadableTerraformGrantsError: As :func:`module_grants`.
    """
    if text is None:
        return dict(_composition_tool_scopes())
    rendered = text.replace("${local.name_prefix}", _NAME_PREFIX)
    return _tool_scopes(_checked_module_grants(_expand_string_list_comprehensions(rendered)))


@functools.cache
def _composition_tool_scopes() -> tuple[
    tuple[tuple[str, str, str], tuple[ConditionScope, ...]], ...
]:
    return tuple(_tool_scopes(_checked_module_grants(_root_composition_text())).items())


def _checked_module_grants(text: str) -> dict[str, list[LocalGrant]]:
    """:func:`module_grants`, asserted to read the same `(table, action)` pairs per function as the
    line walker.

    Raises:
        UnreadableTerraformGrantsError: The two readers disagree on a function or on its pairs, so
            one of them skipped a block or a grant.
    """
    grants = module_grants(text)
    walked, _ = _walk_literal_module_grants(text)
    if set(grants) != set(walked):
        raise UnreadableTerraformGrantsError(
            "the block reader and the line walker disagree on the literal-named lambda_action "
            f"functions: block reader only {sorted(set(grants) - set(walked))}, line walker only "
            f"{sorted(set(walked) - set(grants))}"
        )
    for function, objects in sorted(grants.items()):
        pairs = {(grant.table, action) for grant in objects for action in grant.actions}
        if pairs != walked[function]:
            raise UnreadableTerraformGrantsError(
                f"the block reader and the line walker disagree on {function}'s grants: block "
                f"reader only {sorted(pairs - walked[function])}, line walker only "
                f"{sorted(walked[function] - pairs)}"
            )
    return grants


def conditioned_scopes(
    text: str | None = None,
) -> dict[tuple[str, str, str], tuple[ConditionScope, ...]]:
    """:func:`conditioned_tool_scopes` plus the sweep roles' table-level conditioned grants.

    The sweep grants are read from :data:`SWEEP_GRANT_LOCALS` by :func:`terraform_local_grants`,
    with the same rule: a pair is included only when every table-level object covering it is
    conditioned.

    Raises:
        UnreadableTerraformGrantsError: As :func:`conditioned_tool_scopes` and
            :func:`terraform_local_grants`.
    """
    rendered = None if text is None else text.replace("${local.name_prefix}", _NAME_PREFIX)
    sweeps = {
        function: terraform_local_grants(local, rendered)
        for function, local in SWEEP_GRANT_LOCALS.items()
    }
    tools = conditioned_tool_scopes() if text is None else conditioned_tool_scopes(text)
    return {**tools, **_grant_scopes(sweeps)}


def _tool_scopes(
    grants: Mapping[str, Sequence[LocalGrant]],
) -> dict[tuple[str, str, str], tuple[ConditionScope, ...]]:
    return _grant_scopes(
        {
            function: objects
            for function, objects in grants.items()
            if function not in SWEEP_GRANT_LOCALS
        }
    )


def _grant_scopes(
    grants: Mapping[str, Sequence[LocalGrant]],
) -> dict[tuple[str, str, str], tuple[ConditionScope, ...]]:
    scopes: dict[tuple[str, str, str], list[ConditionScope]] = {}
    unconditioned: set[tuple[str, str, str]] = set()
    for function, objects in grants.items():
        for grant in objects:
            if not grant.include_table_arn:
                continue
            for action in grant.actions:
                triple = (function, grant.table, action)
                if grant.operator is None or grant.key_values is None:
                    unconditioned.add(triple)
                else:
                    scopes.setdefault(triple, []).append((grant.operator, grant.key_values))
    return {
        triple: tuple(found)
        for triple, found in sorted(scopes.items())
        if triple not in unconditioned
    }


def scope_denied_keys(
    scopes: Sequence[ConditionScope], siblings: Sequence[str] = ()
) -> tuple[str, ...]:
    """Out-of-scope keys every scope refuses: :func:`out_of_scope_keys` per scope, then each of
    `siblings` (other grants' in-scope keys on the same table) that no scope admits.

    Raises:
        UnreadableTerraformGrantsError: A candidate is admitted by some scope.
    """
    keys = tuple(
        dict.fromkeys(
            key for operator, values in scopes for key in out_of_scope_keys(values, operator)
        )
    )
    admitted = [
        key
        for key in keys
        for operator, values in scopes
        if any(condition_matches(value, key, operator) for value in values)
    ]
    if admitted:
        raise UnreadableTerraformGrantsError(
            f"out-of-scope probe key(s) {admitted} are admitted by one of {list(scopes)}"
        )
    refused_siblings = [
        key
        for key in siblings
        if not any(
            condition_matches(value, key, operator)
            for operator, values in scopes
            for value in values
        )
    ]
    return tuple(dict.fromkeys((*keys, *refused_siblings)))


#: Matrix `isolation_mechanism` values a `dynamodb:LeadingKeys` condition enforces.
_CONDITIONED_ISOLATION: Final[frozenset[str]] = frozenset({"leading_keys", "key_prefix"})


def matrix_conditioned_grants(
    matrix: Mapping[str, Any], index_only: Mapping[tuple[str, str, str], str]
) -> set[tuple[str, str, str]]:
    """Table-level `(function, table, action)` triples the matrix declares LeadingKeys-isolated.

    Sweep functions are included and `index_only` triples excluded, so this is the set
    :func:`conditioned_scopes` must read from `infra/*.tf`, derived from a second source.
    """
    return {
        (grant["function"], grant["table"], action)
        for grant in matrix.get("grants", [])
        if grant.get("isolation_mechanism") in _CONDITIONED_ISOLATION
        for action in grant["actions"]
        if (grant["function"], grant["table"], action) not in index_only
    }


class UnreadableTerraformGrantsError(RuntimeError):
    """Raised when a `dynamodb_grants` reference cannot be resolved to any grant.

    An unread grant list is reported, never treated as empty.
    """


def _require_local_grants(
    definitions: dict[str, set[tuple[str, str]]],
    name: str,
    function: str,
    defects: Mapping[str, str] | None = None,
) -> set[tuple[str, str]]:
    """Return `local.<name>`'s grants, raising rather than yielding an empty set.

    `defects` maps an omitted local to the reason it was omitted, which the error then names.
    """
    pairs = definitions.get(name)
    if not pairs:
        reason = (defects or {}).get(name)
        raise UnreadableTerraformGrantsError(
            f"{function}'s dynamodb_grants reference local.{name}, which no locals block defines as "
            f"grant objects this reader understands"
            + (f" ({reason})" if reason else "")
            + ". Its grants would otherwise be reported as empty and the role would be simulated "
            f"against nothing. Known locals: {sorted(definitions)}"
        )
    return pairs


def _terraform_literal_module_grants(
    path: Path = _MAIN_TF_PATH,
) -> dict[str, set[tuple[str, str]]]:
    """Return the `dynamodb_grants` of each `lambda_action` block naming a literal `function_name`.

    The tool functions are instantiated through one `for_each` block whose `function_name` is
    an interpolation, so it never matches `_LITERAL_FUNCTION_NAME_RE` and is not double-counted
    here; its grants come from `_terraform_tool_grants`. The function key is the `function_name`
    with the project name prefix stripped, which `ROLE_NAME_TEMPLATE` renders into the role name.
    """
    grants, _ = _walk_literal_module_grants(_root_composition_text(path))
    return grants


def _walk_literal_module_grants(
    text: str,
) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[str]]]:
    """Return `(grants, local_refs)` for every literal-named `lambda_action` block in `text`.

    `local_refs` maps each function to the `locals` names its `dynamodb_grants` reference.
    """
    locals_grants, defects = _local_grant_definitions_and_defects(text)
    grants: dict[str, set[tuple[str, str]]] = {}
    local_refs: dict[str, set[str]] = {}

    def reference(function: str, local: str) -> None:
        local_refs.setdefault(function, set()).add(local)
        grants.setdefault(function, set()).update(
            _require_local_grants(locals_grants, local, function, defects)
        )

    function: str | None = None
    in_grants = in_comprehension = False
    current_table: str | None = None
    for raw_line, code_line in zip(
        text.splitlines(), blank_comments(text).splitlines(), strict=True
    ):
        if _MODULE_BLOCK_RE.match(code_line):
            function, in_grants, current_table = None, False, None
            continue
        name_match = _LITERAL_FUNCTION_NAME_RE.match(raw_line)
        if name_match is not None:
            name = name_match.group("name")
            function = (
                name[len(_NAME_PREFIX) + 1 :] if name.startswith(f"{_NAME_PREFIX}-") else None
            )
            if function in PENDING_FUNCTIONS:
                function = None
            continue
        if function is None:
            continue
        # A grants list supplied wholly by reference never opens a parseable block.
        local_list = _GRANTS_LOCAL_LIST_RE.match(raw_line)
        if local_list is not None:
            for local in re.findall(r"local\.(\w+)", local_list.group("locals")):
                reference(function, local)
            continue
        local_bare = _GRANTS_LOCAL_BARE_RE.match(raw_line)
        if local_bare is not None:
            reference(function, local_bare.group("local"))
            continue
        # A declared-empty list registers the function with no pairs, so its role is still simulated.
        if _GRANTS_EMPTY_LIST_RE.match(raw_line) is not None:
            grants.setdefault(function, set())
            continue
        if not in_grants:
            in_grants = _DYNAMODB_GRANTS_OPEN_RE.match(raw_line) is not None
            in_comprehension = False
            continue
        if _GRANTS_FOR_LOCAL_RE.match(code_line) is not None:
            in_comprehension = True
        for_local = _GRANTS_FOR_LOCAL_RE.match(code_line) or _GRANTS_LOCAL_ITEM_LINE_RE.match(
            code_line
        )
        if for_local is not None:
            reference(function, for_local.group("local"))
            continue
        if code_line.rstrip() == "  ]":
            in_grants, current_table = False, None
            continue
        table_match = _TABLE_NAME_RE.match(code_line)
        if table_match is not None:
            current_table = table_match.group("table")
            continue
        actions_match = _ACTIONS_RE.match(code_line)
        if actions_match is not None and current_table is not None:
            where = f"{function}'s dynamodb_grants on {current_table}"
            for action in _literal_strings(actions_match.group("actions"), where):
                grants.setdefault(function, set()).add((current_table, action))
            continue
        if _WALKER_GRANT_KEY_RE.search(code_line) is not None and not (
            in_comprehension and _WALKER_PASSTHROUGH_RE.match(code_line) is not None
        ):
            raise UnreadableTerraformGrantsError(
                f"{function}'s dynamodb_grants: line {code_line.strip()!r} names a table or actions "
                "in a shape this line walker does not read (one key per line, a literal table and a "
                "one-line actions list)"
            )
    return grants, local_refs


def _local_definition_text(text: str, name: str, spans: Mapping[str, tuple[int, int]]) -> str:
    """The source of `local.<name>`: its bracketed span, else every line up to the next assignment.

    A `concat(...)` span keeps its `concat`. The fallback covers any other call, which has no
    bracketed span.
    """
    if name in spans:
        start, end = spans[name]
        call = "concat" if text[max(0, start - len("concat")) : start] == "concat" else ""
        return call + text[start:end]
    match = re.search(
        rf"^  {re.escape(name)}\s*=\s*(?P<body>.*?)(?=^  \w+\s*=|^\}})",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group("body") if match else ""


def _pending_function_locals(text: str) -> set[str]:
    """`locals` names referenced from a :data:`PENDING_FUNCTIONS` `lambda_action` block, transitively.

    `local.sweep_task_dynamodb_grants` is a `concat` over other locals, so references are followed
    through the named values they appear in.
    """
    spans = _terraform_local_value_spans(text)
    pending: set[str] = set()
    for block in re.split(r"^module\s+\"[\w-]+\"\s*\{", text, flags=re.MULTILINE)[1:]:
        body = block.split("\n}\n", 1)[0]
        literal = re.search(r'^\s*function_name\s*=\s*"([\w-]+)"\s*$', body, re.MULTILINE)
        if literal is None:
            continue
        if literal.group(1).removeprefix(f"{_NAME_PREFIX}-") not in PENDING_FUNCTIONS:
            continue
        pending.update(re.findall(r"\blocal\.(\w+)", body))
    frontier = set(pending)
    while frontier:
        name = frontier.pop()
        definition = _local_definition_text(text, name, spans)
        for ref in re.findall(r"\blocal\.(\w+)", definition):
            if ref not in pending:
                pending.add(ref)
                frontier.add(ref)
    return pending


#: `local.tool_query_indexes`: `{ <tool> = { <table> = ["<index>", ...] } }`, literal names only.
_TOOL_QUERY_INDEXES_RE: Final[re.Pattern[str]] = re.compile(
    r"^  tool_query_indexes = \{\n(?P<body>.*?)^  \}\n", re.DOTALL | re.MULTILINE
)
_TOOL_QUERY_INDEXES_TOOL_RE: Final[re.Pattern[str]] = re.compile(
    r"^    (?P<tool>\w+) = \{\n(?P<body>.*?)^    \}\n", re.DOTALL | re.MULTILINE
)
_TOOL_QUERY_INDEXES_TABLE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s+(?P<table>\w+)\s*=\s*\[(?P<list>[^\]]*)\]\s*$", re.MULTILINE
)


def terraform_tool_query_indexes(
    path: Path = _MAIN_TF_PATH,
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Return `{(tool, table): indexes}` from `local.tool_query_indexes` in `infra/main.tf`.

    A tool's Query-only grant on a table listed here is scoped to exactly those GSIs, with no base
    table ARN. An absent block is no scoping; a tool or table entry that does not parse raises.

    Raises:
        UnreadableTerraformGrantsError: An entry names no index, or a tool block holds no table.
    """
    block = _TOOL_QUERY_INDEXES_RE.search(path.read_text(encoding="utf-8"))
    if block is None:
        return {}
    result: dict[tuple[str, str], tuple[str, ...]] = {}
    for tool in _TOOL_QUERY_INDEXES_TOOL_RE.finditer(block.group("body")):
        tables = list(_TOOL_QUERY_INDEXES_TABLE_RE.finditer(tool.group("body")))
        if not tables:
            raise UnreadableTerraformGrantsError(
                f"local.tool_query_indexes.{tool.group('tool')} names no table"
            )
        for entry in tables:
            indexes = tuple(_literal_strings(entry.group("list"), "local.tool_query_indexes"))
            if not indexes:
                raise UnreadableTerraformGrantsError(
                    f"local.tool_query_indexes.{tool.group('tool')}.{entry.group('table')} names no index"
                )
            result[(tool.group("tool"), entry.group("table"))] = indexes
    return result


def _terraform_index_only_grants(path: Path = _MAIN_TF_PATH) -> dict[tuple[str, str, str], str]:
    """Return `{(function, table, action): index name}` for every index-only DynamoDB grant.

    An index-only grant's Allow covers `table/<t>/index/<name>` and not the base table, so it is
    simulated against that index ARN. A pair some other
    grant object of the same function also covers on the base table is not index-only.

    Raises:
        UnreadableTerraformGrantsError: An index-only grant names no index or more than one, or
            `infra/main.tf` holds an `include_table_arn = false` no literal-named function's grants
            account for, or a referenced grant local holds an unreadable object.
    """
    text = _root_composition_text(path)
    _, local_refs = _walk_literal_module_grants(text)
    referenced = {name for names in local_refs.values() for name in names}
    local_grants = _terraform_local_index_grants(text, strict=referenced)
    result: dict[tuple[str, str, str], str] = {}
    attributed_locals: set[str] = set()
    for function, names in sorted(local_refs.items()):
        index_only: dict[tuple[str, str], list[str]] = {}
        table_inclusive: set[tuple[str, str]] = set()
        for name in names:
            grants = local_grants.get(name)
            if grants is None:
                continue
            attributed_locals.add(name)
            table_inclusive |= grants.table_inclusive
            for pair, indexes in grants.index_only.items():
                index_only.setdefault(pair, []).extend(indexes)
        for (table, action), indexes in sorted(index_only.items()):
            if (table, action) in table_inclusive:
                continue
            if len(set(indexes)) != 1:
                raise UnreadableTerraformGrantsError(
                    f"{function}'s index-only {action} grant on {table} names indexes {indexes!r}; "
                    "this reader simulates exactly one index per index-only grant"
                )
            result[(function, table, action)] = indexes[0]
    # A tool's scoped Query grant is index-only too; its first index drives the matrix row and
    # `scoped_index_allowed` covers the rest.
    for (tool, table), scoped in terraform_tool_query_indexes(path).items():
        result[(tool, table, "dynamodb:Query")] = scoped[0]
    spans = _terraform_local_value_spans(text)
    pending_objects = sum(
        len(
            _INDEX_ONLY_MARKER_RE.findall(
                blank_comments(blank_literals(_local_definition_text(text, name, spans)))
            )
        )
        for name in _pending_function_locals(text) - attributed_locals
    )
    declared = (
        len(_INDEX_ONLY_MARKER_RE.findall(blank_comments(blank_literals(text)))) - pending_objects
    )
    attributed = sum(local_grants[name].index_only_objects for name in attributed_locals)
    if declared != attributed:
        raise UnreadableTerraformGrantsError(
            f"infra/*.tf declares {declared} include_table_arn = false grant(s) outside pending "
            f"functions but only "
            f"{attributed} belong to a literal-named function's locals grants; an unattributed "
            "index-only grant would be simulated against its base-table ARN"
        )
    return result


def _load_matrix(path: Path = _MATRIX_PATH) -> dict[str, Any]:
    """Load the project's function x table x action grant matrix from disk."""
    return dict(json.loads(path.read_text(encoding="utf-8")))


def matrix_shape_problems(matrix: Mapping[str, Any]) -> list[str]:
    """Every way `matrix` departs from the shape `checks` reads; empty when it is readable.

    `tables` must be an object of objects, `functions_without_dynamodb_grants` (when present) a
    list of strings, and `grants` a list of objects, each with a string `function`, a `table` that
    `tables` names, and a non-empty list of string `actions`. When the matrix carries an
    `isolation_mechanism_legend`, it must be an object and every grant's `isolation_mechanism` one
    of its keys.
    """
    tables = matrix.get("tables")
    grants = matrix.get("grants")
    problems: list[str] = []
    if not isinstance(tables, Mapping):
        problems.append(f"`tables` is {type(tables).__name__}, not an object")
        tables = {}
    problems += [
        f"tables[{name!r}] is {type(entry).__name__}, not an object"
        for name, entry in tables.items()
        if not isinstance(entry, Mapping)
    ]
    declared_empty = matrix.get("functions_without_dynamodb_grants", [])
    if not isinstance(declared_empty, list) or not all(isinstance(f, str) for f in declared_empty):
        problems.append("`functions_without_dynamodb_grants` is not a list of strings")
    if not isinstance(grants, list):
        return [*problems, f"`grants` is {type(grants).__name__}, not a list"]
    legend = matrix.get("isolation_mechanism_legend")
    if legend is not None and not isinstance(legend, Mapping):
        problems.append(f"`isolation_mechanism_legend` is {type(legend).__name__}, not an object")
    for index, grant in enumerate(grants):
        where = f"grants[{index}]"
        if not isinstance(grant, Mapping):
            problems.append(f"{where} is {type(grant).__name__}, not an object")
            continue
        for key in ("function", "table"):
            if not isinstance(grant.get(key), str):
                problems.append(f"{where}.{key} is missing or not a string")
        if isinstance(grant.get("table"), str) and grant["table"] not in tables:
            problems.append(f"{where}.table {grant['table']!r} is not in `tables`")
        actions = grant.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
            or not all(isinstance(a, str) for a in actions)
        ):
            problems.append(f"{where}.actions is not a non-empty list of strings")
        if isinstance(legend, Mapping) and grant.get("isolation_mechanism") not in legend:
            problems.append(
                f"{where}.isolation_mechanism {grant.get('isolation_mechanism')!r} is not one of "
                f"the legend's {sorted(legend)}"
            )
    return problems


def _function_grants(matrix: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group the matrix's flat `grants` list by function name.

    A function named in `functions_without_dynamodb_grants` is included with an empty list.
    """
    by_function: dict[str, list[dict[str, Any]]] = {}
    declared_empty = matrix.get("functions_without_dynamodb_grants", [])
    if isinstance(declared_empty, list):
        for function in declared_empty:
            by_function.setdefault(str(function), [])
    for grant in matrix["grants"]:
        by_function.setdefault(grant["function"], []).append(grant)
    return by_function


def _all_actions_for(grants: Sequence[dict[str, Any]]) -> set[tuple[str, str]]:
    """Return the `(table, action)` pairs a function's own grants cover."""
    return {(grant["table"], action) for grant in grants for action in grant["actions"]}


def _role_name(function: str) -> str:
    """Render the execution-role name the project's naming convention generates for `function`."""
    return ROLE_NAME_TEMPLATE.format(function=function)


#: `SimulatePrincipalPolicy` is rate-limited per account. A throttle is retried with linear backoff;
#: once the attempts are exhausted it surfaces as a `fail`.
_SIMULATE_MAX_ATTEMPTS: Final[int] = 6
_SIMULATE_BACKOFF_SECONDS: Final[float] = 2.0
_THROTTLE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"Throttling", "ThrottlingException", "TooManyRequestsException"}
)


def _explicit_deny_detail(role_name: str, action: str, table: str, denial_check: str) -> str:
    """The `detail` of an explicit-deny row that failed because the refusal is only implicit."""
    return (
        f"role {role_name!r} is refused {action!r} on {table!r} by the absence of any grant "
        "(implicitDeny), not by a matching Deny statement: the role's explicit Deny covering this "
        f"action on this table is missing. The {denial_check!r} row asserts the refusal itself."
    )


def _is_throttle(exc: BaseException) -> bool:
    """Whether `exc` is an IAM request-rate error rather than a genuine evaluation failure."""
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return False
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, Mapping) else None
    return str(code) in _THROTTLE_ERROR_CODES


def _is_no_such_entity(exc: BaseException) -> bool:
    """Whether `exc` is IAM's "that principal does not exist"."""
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return False
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, Mapping) else None
    return str(code) == "NoSuchEntity"


def _role_exists(ctx: SmokeContext, role_name: str) -> bool | str:
    """Whether `role_name` exists, or an error string when existence could not be established.

    `SimulatePrincipalPolicy` raises `NoSuchEntity` for a non-existent principal, so existence is
    established first and reported as its own check.
    """
    try:
        ctx.client("iam").get_role(RoleName=role_name)
    except Exception as exc:  # noqa: BLE001 - classified below; never silently treated as absent
        if _is_no_such_entity(exc):
            return False
        return f"{type(exc).__name__}: {exc}"
    return True


def _simulate_evaluation(
    ctx: SmokeContext,
    role_arn: str,
    action: str,
    resource_arn: str,
    leading_keys: Sequence[str] | None = None,
    extra_context: Mapping[str, str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Return `(decision, MissingContextValues)` for one simulated action.

    `decision` is one of `allowed`, `explicitDeny`, `implicitDeny`, or `no_result` (the simulate
    call returned no evaluation result for the requested action). `leading_keys` adds a
    `dynamodb:LeadingKeys` context entry and `extra_context` one string entry per key. A throttled
    call is retried up to `_SIMULATE_MAX_ATTEMPTS` times and then raised.
    """
    request: dict[str, Any] = {
        "PolicySourceArn": role_arn,
        "ActionNames": [action],
        "ResourceArns": [resource_arn],
    }
    entries: list[dict[str, Any]] = []
    if leading_keys is not None:
        entries.append(
            {
                "ContextKeyName": "dynamodb:LeadingKeys",
                "ContextKeyType": "stringList",
                "ContextKeyValues": list(leading_keys),
            }
        )
    for name, value in (extra_context or {}).items():
        entries.append(
            {"ContextKeyName": name, "ContextKeyType": "string", "ContextKeyValues": [value]}
        )
    if entries:
        request["ContextEntries"] = entries
    iam = ctx.client("iam")
    for attempt in range(1, _SIMULATE_MAX_ATTEMPTS + 1):
        try:
            response = iam.simulate_principal_policy(**request)
            break
        except Exception as exc:  # noqa: BLE001 - re-raised below unless it is a retryable throttle
            if attempt == _SIMULATE_MAX_ATTEMPTS or not _is_throttle(exc):
                raise
            time.sleep(_SIMULATE_BACKOFF_SECONDS * attempt)
    results = response.get("EvaluationResults", [])
    if not results:
        return "no_result", ()
    missing = tuple(str(key) for key in results[0].get("MissingContextValues") or ())
    return str(results[0]["EvalDecision"]), missing


def _simulate(
    ctx: SmokeContext, role_arn: str, action: str, resource_arn: str
) -> tuple[str, str | None]:
    """Return `(decision, None)` for one context-free simulation (:func:`_simulate_evaluation`)."""
    return _simulate_evaluation(ctx, role_arn, action, resource_arn)[0], None


#: SimulatePrincipalPolicy decisions ordered from most to least restrictive.
_DECISION_PERMISSIVENESS: Final[Mapping[str, int]] = {
    "explicitDeny": 0,
    "implicitDeny": 1,
    "allowed": 2,
}


def _most_permissive(decisions: Iterable[str]) -> str:
    """Return the most permissive of ``decisions``; an unrecognised decision ranks as ``allowed``."""
    return max(decisions, key=lambda decision: _DECISION_PERMISSIVENESS.get(decision, 2))


def _own_table_denied_check(
    ctx: SmokeContext,
    role_name: str,
    role_arn: str,
    function: str,
    table: str,
    action: str,
    base_table_arn: str,
) -> CheckResult:
    """Assert ``function``'s role is explicitly denied ``action`` on a table it holds other grants on."""
    check = f"own_table_denied:{function}:{action}@{table}"
    try:
        decision, _ = _simulate_evaluation(ctx, role_arn, action, base_table_arn)
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        return CheckResult(
            resource=role_name, check=check, status="fail", detail=f"simulate raised: {exc}"
        )
    return CheckResult(
        resource=role_name,
        check=check,
        status="pass" if decision == "explicitDeny" else "fail",
        detail=(
            f"{action} on {base_table_arn} evaluated {decision}; expected explicitDeny from the "
            "module's ExplicitDenyNonGranted statement (OWN_TABLE_DENIAL_PROBES)"
        ),
    )


def _scoped_index_allowed_check(
    ctx: SmokeContext,
    role_name: str,
    role_arn: str,
    function: str,
    table: str,
    action: str,
    index_arn: str,
) -> CheckResult:
    """Assert a further index of ``function``'s scoped ``action`` grant is allowed."""
    index = index_arn.rpartition("/")[2]
    check = f"scoped_index_allowed:{function}:{action}@{table}/{index}"
    probe = INDEX_IN_SCOPE_LEADING_KEY.get(index)
    try:
        decision, _ = _simulate_evaluation(
            ctx, role_arn, action, index_arn, [probe] if probe else None
        )
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        return CheckResult(
            resource=role_name, check=check, status="fail", detail=f"simulate raised: {exc}"
        )
    return CheckResult(
        resource=role_name,
        check=check,
        status="pass" if decision == "allowed" else "fail",
        detail=f"{action} on {index_arn} evaluated {decision}; local.tool_query_indexes grants it",
    )


def _index_only_base_table_check(
    ctx: SmokeContext,
    role_name: str,
    role_arn: str,
    function: str,
    table: str,
    action: str,
    base_table_arn: str,
) -> CheckResult:
    """Assert ``function``'s index-only ``action`` grant does not also reach the base table."""
    check = f"index_only_base_table_denied:{function}:{action}@{table}"
    try:
        decision, _ = _simulate(ctx, role_arn, action, base_table_arn)
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        return CheckResult(
            resource=role_name,
            check=check,
            status="fail",
            detail=f"simulate_principal_policy raised: {exc}",
        )
    if decision == "no_result":
        return CheckResult(
            resource=role_name,
            check=check,
            status="fail",
            detail=f"no EvaluationResults returned for role {role_name!r}, action {action!r}",
        )
    if decision == "allowed":
        return CheckResult(
            resource=role_name,
            check=check,
            status="fail",
            detail=(
                f"{action} on the base table {base_table_arn} is allowed, but infra/main.tf declares "
                "this grant index-only (include_table_arn = false)"
            ),
        )
    return CheckResult(
        resource=role_name,
        check=check,
        status="pass",
        detail=f"denied ({decision}) on {base_table_arn}",
    )


def _simulate_with_leading_keys(
    ctx: SmokeContext,
    role_arn: str,
    action: str,
    resource_arn: str,
    leading_keys: Sequence[str],
) -> str:
    """Simulate `action` on `resource_arn` with an explicit `dynamodb:LeadingKeys` context.

    Proves the condition's value scope, not what DynamoDB puts in LeadingKeys for a request. Returns
    the EvalDecision or `no_result`; throttles are retried then raised.
    """
    return _simulate_evaluation(ctx, role_arn, action, resource_arn, leading_keys)[0]


def _extra_condition_key(grant: LocalGrant, value: str) -> str:
    """The concrete `dynamodb:LeadingKeys` value one condition value admits."""
    return value if grant.operator == "StringEquals" else in_scope_key(value)


def _grant_resource_arns(grant: LocalGrant, table_arn: str) -> list[tuple[str, str]]:
    """`(where, arn)` for each resource a grant object's Allow names: its table, then each index."""
    resources = [(grant.table, table_arn)] if grant.include_table_arn else []
    resources += [
        (f"{grant.table}/{index}", f"{table_arn}/index/{index}") for index in grant.index_names
    ]
    return resources


def _extra_condition_row(
    ctx: SmokeContext,
    role_name: str,
    role_arn: str,
    check: str,
    action: str,
    resource_arn: str,
    key: str,
    context: Mapping[str, str],
    *,
    allowed: bool,
    missing: str | None = None,
) -> CheckResult:
    """One extra-condition simulation: `allowed` expects `allowed` with no missing key, a denial
    expects either deny kind, and `missing` must then be reported in `MissingContextValues`."""
    subject = f"{action} on {resource_arn} with LeadingKeys={[key]} context={dict(context)}"
    try:
        decision, unset = _simulate_evaluation(ctx, role_arn, action, resource_arn, [key], context)
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        return CheckResult(
            resource=role_name,
            check=check,
            status="fail",
            detail=f"simulate raised: {exc} ({subject})",
        )
    if allowed:
        ok = decision == "allowed" and not unset
    else:
        ok = decision in ("explicitDeny", "implicitDeny") and (missing is None or missing in unset)
    expected = "allowed" if allowed else "denied" + (f" with {missing} missing" if missing else "")
    return CheckResult(
        resource=role_name,
        check=check,
        status="pass" if ok else "fail",
        detail=f"{subject}: {decision}, MissingContextValues={list(unset)} (expected {expected})",
    )


def extra_condition_rows(
    ctx: SmokeContext,
    role_name: str,
    role_arn: str,
    function: str,
    grants: Sequence[LocalGrant],
    table_arn: Callable[[str], str],
) -> Iterator[CheckResult]:
    """The rows of every grant in `grants` carrying a `select_condition` (module docstring)."""
    for grant in grants:
        if not grant.extra_conditions or grant.key_values is None:
            continue
        satisfied = {c.variable: c.values[0] for c in grant.extra_conditions}
        for action in grant.actions:
            for where, arn in _grant_resource_arns(grant, table_arn(grant.table)):
                suffix = f"{function}:{action}@{where}"
                for value in grant.key_values:
                    key = _extra_condition_key(grant, value)
                    yield _extra_condition_row(
                        ctx,
                        role_name,
                        role_arn,
                        f"extra_condition_allowed:{suffix}[{key}]",
                        action,
                        arn,
                        key,
                        satisfied,
                        allowed=True,
                    )
                    for condition in grant.extra_conditions:
                        for other in EXTRA_CONDITION_DOMAINS[condition.variable]:
                            if other in condition.values:
                                continue
                            yield _extra_condition_row(
                                ctx,
                                role_name,
                                role_arn,
                                f"extra_condition_denied:{suffix}[{key},{condition.variable}={other}]",
                                action,
                                arn,
                                key,
                                {**satisfied, condition.variable: other},
                                allowed=False,
                            )
                        absent = {k: v for k, v in satisfied.items() if k != condition.variable}
                        yield _extra_condition_row(
                            ctx,
                            role_name,
                            role_arn,
                            f"extra_condition_absent_denied:{suffix}[{key},{condition.variable}]",
                            action,
                            arn,
                            key,
                            absent,
                            allowed=False,
                            missing=condition.variable,
                        )
                siblings = [
                    g
                    for g in grants
                    if g.table == grant.table
                    and action in g.actions
                    and not g.extra_conditions
                    and g.key_values
                    and any(w == where for w, _ in _grant_resource_arns(g, table_arn(g.table)))
                ]
                for sibling in siblings:
                    for value in sibling.key_values or ():
                        key = _extra_condition_key(sibling, value)
                        unsatisfying = {
                            c.variable: next(
                                v for v in EXTRA_CONDITION_DOMAINS[c.variable] if v not in c.values
                            )
                            for c in grant.extra_conditions
                        }
                        label = ",".join(f"{k}={v}" for k, v in unsatisfying.items())
                        yield _extra_condition_row(
                            ctx,
                            role_name,
                            role_arn,
                            f"extra_condition_sibling_allowed:{suffix}[{key},{label}]",
                            action,
                            arn,
                            key,
                            unsatisfying,
                            allowed=True,
                        )


def _get_role_policy_documents(ctx: SmokeContext, role_name: str) -> list[dict[str, Any]]:
    """Return every inline and attached-managed policy document for `role_name`.

    Missing/absent policies are treated as an empty list rather than raised.
    """
    iam = ctx.client("iam")
    documents: list[dict[str, Any]] = []

    inline_names = iam.list_role_policies(RoleName=role_name).get("PolicyNames", [])
    for policy_name in inline_names:
        policy = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
        documents.append(dict(policy["PolicyDocument"]))

    attached = iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
    for attached_policy in attached:
        version_id = iam.get_policy(PolicyArn=attached_policy["PolicyArn"])["Policy"][
            "DefaultVersionId"
        ]
        version = iam.get_policy_version(
            PolicyArn=attached_policy["PolicyArn"], VersionId=version_id
        )
        documents.append(dict(version["PolicyVersion"]["Document"]))

    return documents


def _statements(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a policy document's `Statement` list, normalising the single-statement dict form."""
    statement = document.get("Statement", [])
    if isinstance(statement, dict):
        return [statement]
    return list(statement)


def _action_list(statement: Mapping[str, Any]) -> list[str]:
    """Return a statement's `Action` (or `NotAction`) values, always as a list."""
    for key in ("Action", "NotAction"):
        if key in statement:
            value = statement[key]
            return [value] if isinstance(value, str) else list(value)
    return []


def _has_dynamodb_wildcard(statement: Mapping[str, Any]) -> str | None:
    """Return a human-readable reason if `statement` grants a DynamoDB action via any wildcard form.

    Catches `"dynamodb:*"`, a bare `"*"`, and `NotAction` (which grants everything except the listed
    actions). Returns `None` when the statement carries no such wildcard.
    """
    if statement.get("Effect") != "Allow":
        return None
    if "NotAction" in statement:
        return f"NotAction {_action_list(statement)!r} implicitly grants all unlisted actions"
    for action in _action_list(statement):
        if action == "*":
            return "Action '*' grants every DynamoDB action"
        if action == "dynamodb:*":
            return "Action 'dynamodb:*' is an explicit wildcard grant"
    return None


def _has_leading_keys_condition(statement: Mapping[str, Any]) -> bool:
    """Whether `statement` carries a `dynamodb:LeadingKeys` condition, in either condition form."""
    condition = statement.get("Condition", {})
    for operator_block in condition.values():
        if not isinstance(operator_block, Mapping):
            continue
        if "dynamodb:LeadingKeys" in operator_block:
            return True
    return False


def _leading_keys_values(statement: Mapping[str, Any]) -> list[str]:
    """Return the values a statement's `dynamodb:LeadingKeys` condition is pinned to, if any."""
    condition = statement.get("Condition", {})
    values: list[str] = []
    for operator_block in condition.values():
        if not isinstance(operator_block, Mapping):
            continue
        raw = operator_block.get("dynamodb:LeadingKeys")
        if raw is None:
            continue
        values.extend([raw] if isinstance(raw, str) else [str(v) for v in raw])
    return values


#: The item-level DynamoDB actions whose request supplies `dynamodb:LeadingKeys` context. Under a
#: `ForAllValues` LeadingKeys Allow, every other action satisfies the condition vacuously.
KEYED_DYNAMODB_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "dynamodb:GetItem",
        "dynamodb:BatchGetItem",
        "dynamodb:Query",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:ConditionCheckItem",
    }
)


def _vacuous_for_all_values_actions(statement: Mapping[str, Any]) -> list[str]:
    """The actions an Allow with a `ForAllValues:*` LeadingKeys condition grants that are not in
    :data:`KEYED_DYNAMODB_ACTIONS` (case-insensitive, literal only); empty otherwise.

    A wildcard pattern and any `NotAction` list are returned as vacuous, since either can grant an
    action outside the set.
    """
    condition = statement.get("Condition", {})
    if statement.get("Effect") != "Allow" or not isinstance(condition, Mapping):
        return []
    if not any(
        str(operator).casefold().startswith("forallvalues:")
        and isinstance(block, Mapping)
        and "dynamodb:LeadingKeys" in block
        for operator, block in condition.items()
    ):
        return []
    if "NotAction" in statement:
        return [f"NotAction {_action_list(statement)!r}"]
    keyed = {action.casefold() for action in KEYED_DYNAMODB_ACTIONS}
    return sorted(action for action in _action_list(statement) if action.casefold() not in keyed)


def _probe_decision(
    ctx: SmokeContext, role_arn: str, action: str, resource_arn: str, leading_keys: Sequence[str]
) -> str:
    """:func:`_simulate_with_leading_keys`, with a raised error returned as its description."""
    try:
        return _simulate_with_leading_keys(ctx, role_arn, action, resource_arn, leading_keys)
    except Exception as exc:  # noqa: BLE001 - the caller's row fails on any non-decision
        return f"an error ({type(exc).__name__}: {exc})"


def _role_documents(
    ctx: SmokeContext,
    role_name: str,
    cache: dict[str, tuple[list[dict[str, Any]], Exception | None]],
) -> tuple[list[dict[str, Any]], Exception | None]:
    """`role_name`'s policy documents, loaded once per run, with the load error if one was raised."""
    if role_name not in cache:
        try:
            cache[role_name] = (_get_role_policy_documents(ctx, role_name), None)
        except Exception as exc:  # noqa: BLE001 - reported on the no_dynamodb_wildcard row
            cache[role_name] = ([], exc)
    return cache[role_name]


def _index_scope_denied_check(
    ctx: SmokeContext,
    role_name: str,
    role_arn: str,
    function: str,
    table: str,
    action: str,
    index_arn: str,
) -> CheckResult:
    """Assert ``function``'s own index grant refuses ``OUT_OF_SCOPE_TENANT`` on ``index_arn``."""
    check = f"index_scope_denied:{function}:{action}@{table}"
    subject = f"{action} on {index_arn} with LeadingKeys={[OUT_OF_SCOPE_TENANT]}"
    try:
        decision = _simulate_with_leading_keys(
            ctx, role_arn, action, index_arn, [OUT_OF_SCOPE_TENANT]
        )
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        return CheckResult(
            resource=role_name,
            check=check,
            status="fail",
            detail=f"simulate_principal_policy raised: {exc} ({subject})",
        )
    if decision in ("explicitDeny", "implicitDeny"):
        return CheckResult(
            resource=role_name, check=check, status="pass", detail=f"{subject}: {decision}"
        )
    return CheckResult(
        resource=role_name,
        check=check,
        status="fail",
        detail=(
            f"{subject}: {decision}, expected denied. The grant's dynamodb:LeadingKeys condition "
            "is absent or wider than the tenants this role's code queries"
        ),
    )


def _conditioned_scope_denied_check(
    ctx: SmokeContext,
    role_name: str,
    role_arn: str,
    function: str,
    table: str,
    action: str,
    table_arn: str,
    keys: Sequence[str],
) -> CheckResult:
    """Assert ``function``'s own conditioned grant refuses each out-of-scope key on ``table_arn``."""
    check = f"conditioned_scope_denied:{function}:{action}@{table}"
    if not keys:
        return CheckResult(
            resource=role_name,
            check=check,
            status="fail",
            detail="no out-of-scope dynamodb:LeadingKeys value to probe with",
        )
    outcomes: list[str] = []
    for key in keys:
        subject = f"{action} on {table_arn} with LeadingKeys={[key]}"
        try:
            decision = _simulate_with_leading_keys(ctx, role_arn, action, table_arn, [key])
        except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
            return CheckResult(
                resource=role_name,
                check=check,
                status="fail",
                detail=f"simulate_principal_policy raised: {exc} ({subject})",
            )
        if decision not in ("explicitDeny", "implicitDeny"):
            return CheckResult(
                resource=role_name,
                check=check,
                status="fail",
                detail=(
                    f"{subject}: {decision}, expected denied. The grant's dynamodb:LeadingKeys "
                    "condition is absent or wider than infra/*.tf declares"
                ),
            )
        outcomes.append(f"{key}: {decision}")
    return CheckResult(resource=role_name, check=check, status="pass", detail="; ".join(outcomes))


def _conditioned_scope_rows(
    ctx: SmokeContext,
    role_name: str,
    role_arn: str,
    function: str,
    table: str,
    action: str,
    table_arn: str,
    scopes: Sequence[ConditionScope],
    siblings: Sequence[str],
) -> Iterator[CheckResult]:
    """The `conditioned_scope_denied` row of one own conditioned grant, emitted whatever was parsed.

    It probes `PROBE_FOREIGN_KEY` plus :func:`scope_denied_keys` of `scopes`. When that derivation
    raises, a failing `conditioned_scope_keys_derivable` row names the error and the foreign key is
    still probed.
    """
    keys: tuple[str, ...] = (PROBE_FOREIGN_KEY,)
    try:
        if scopes:
            keys += scope_denied_keys(scopes, siblings)
    except Exception as exc:  # noqa: BLE001 - report as a failing check
        yield CheckResult(
            resource=role_name,
            check=f"conditioned_scope_keys_derivable:{function}:{action}@{table}",
            status="fail",
            detail=(
                f"no parser-derived out-of-scope key could be derived: {exc}. The "
                f"conditioned_scope_denied row probes only {PROBE_FOREIGN_KEY!r}"
            ),
        )
    yield _conditioned_scope_denied_check(
        ctx, role_name, role_arn, function, table, action, table_arn, tuple(dict.fromkeys(keys))
    )


def _resource_arns(statement: Mapping[str, Any]) -> list[str]:
    """Return a statement's `Resource` values, always as a list."""
    resource = statement.get("Resource", [])
    if isinstance(resource, str):
        return [resource]
    return list(resource)


_ARN_TABLE_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r":table/(?P<name>[^/]+)")


def _physical_to_logical(dynamodb_table_names: object) -> dict[str, str]:
    """Invert the `dynamodb_table_names` output into `{physical name: logical name}`.

    `dynamodb_table_names` maps a logical table name to its *deployed physical* name (e.g.
    `reps -> crm-agent-evalreps`), which is what every deployed policy statement's resource ARN
    carries. A non-mapping value yields an empty mapping, so the caller falls back to logical-name
    matching.
    """
    if not isinstance(dynamodb_table_names, Mapping):
        return {}
    return {str(physical): str(logical) for logical, physical in dynamodb_table_names.items()}


def _matrix_table_for_resource_arn(
    resource_arn: str,
    table_names: Iterable[str],
    physical_to_logical: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve a statement's resource ARN back to its logical (matrix) table name, or `None`.

    Parses the ARN's `table/<name>` segment exactly — dropping any `/index/<gsi>` sub-resource —
    then maps that name through `physical_to_logical` before falling back to treating it as a
    logical name.
    """
    match = _ARN_TABLE_SEGMENT_RE.search(resource_arn)
    if match is None:
        return None
    segment = match.group("name")
    mapped = (physical_to_logical or {}).get(segment)
    if mapped is not None:
        return mapped
    return segment if segment in set(table_names) else None


class _IntendedGrants(NamedTuple):
    """What every role is judged against, and how well it could be read.

    `intended` is what `checks` iterates and what `expected_check_count` sizes its floor from.
    """

    intended: dict[str, set[tuple[str, str]]]
    documented: dict[str, set[tuple[str, str]]]
    computed: dict[str, set[tuple[str, str]]]
    error: str


def _resolve_intended_grants(matrix: Mapping[str, Any]) -> _IntendedGrants:
    """Resolve the per-function intended DynamoDB grants from the matrix and `infra/main.tf`.

    Intended grants come from `_terraform_intended_grants`, never from the live policy. A function
    present only in the documented matrix keeps that matrix as its intent; `intended_grants_parity`
    reports any divergence per role as a failure.
    """
    documented: dict[str, set[tuple[str, str]]] = {
        function: _all_actions_for(grants) for function, grants in _function_grants(matrix).items()
    }
    try:
        computed = _terraform_intended_grants()
        error = ""
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        computed = {}
        error = f"could not read infra/main.tf's local.tool_dynamodb_grants: {exc}"
    intended: dict[str, set[tuple[str, str]]] = dict(documented)
    intended.update(computed)
    return _IntendedGrants(intended=intended, documented=documented, computed=computed, error=error)


#: Rows every *existing* role contributes besides its slice of the simulate matrix, on every path
#: through the loop body: `intended_grants_parity`, `role_exists`, `no_dynamodb_wildcard`, and at
#: least one `leading_keys:*` row (a per-table verdict, an `unresolved_table_resource` fail, or the
#: `no_dynamodb_grant` skip — the branches are exhaustive, so the floor of one holds).
_ROWS_PER_ROLE_OUTSIDE_THE_SIMULATE_MATRIX: Final[int] = 4


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """Derive the fewest checks a correct run of this module can emit.

    Derived from the two files `checks` iterates. Each role is simulated against every function's
    actions, and each non-own pair emits a second `explicit_deny` row, so the matrix term is
    `roles x triples + non-own pairs`; `own_rows` is counted by pair membership.

    A minimum, because the `leading_keys:*` row count per role depends on the deployed policy
    documents. A role that does not exist short-circuits after two rows and so also falls under the
    floor. A vacuous derivation raises.
    """
    matrix = _load_matrix()
    problems = matrix_shape_problems(matrix)
    if problems:
        raise UnreadableTerraformGrantsError(
            f"table_action_role_matrix.json is not readable: {'; '.join(problems)}"
        )
    intended = _resolve_intended_grants(matrix).intended
    roles = len(intended)
    triples = sum(len(pairs) for pairs in intended.values())
    if roles == 0 or triples == 0:
        raise UnreadableTerraformGrantsError(
            f"the intended-grant derivation read {roles} role(s) and {triples} (table, action) "
            "pair(s), so it cannot bound anything. A floor computed from an empty read would pass "
            "on a run that verified nothing, which is the hole this floor exists to close"
        )
    # A non-own pair emits a denial row and an explicit-deny row. Own iterations are counted by pair
    # membership, since two functions can share a (table, action) pair.
    own_rows = sum(len(intended[other] & intended[role]) for role in intended for other in intended)
    non_own_rows = roles * triples - own_rows
    simulate_rows = roles * triples + non_own_rows
    index_only = _terraform_index_only_grants()
    scope_rows = sum(
        1
        for (function, table, action), index in index_only.items()
        if index == TENANT_INDEX and (table, action) in intended.get(function, set())
    )
    conditioned_rows = sum(
        1
        for function, table, action in matrix_conditioned_grants(matrix, index_only)
        if (table, action) in intended.get(function, set())
    )
    return ExpectedCheckCount(
        minimum=(
            simulate_rows
            + scope_rows
            + conditioned_rows
            + roles * _ROWS_PER_ROLE_OUTSIDE_THE_SIMULATE_MATRIX
        ),
        basis=(
            f"{simulate_rows} matrix row(s) over {roles} execution role(s) x {triples} "
            f"(table, action) pair(s) - one denial row per pair plus an explicit_deny row for each of "
            f"the {non_own_rows} non-own pairs - plus {scope_rows} index_scope_denied row(s), one "
            f"per own index-only {TENANT_INDEX} grant, plus {conditioned_rows} "
            "conditioned_scope_denied row(s), one per own table-level grant the matrix declares "
            "leading_keys or key_prefix, the sweep roles' included, plus "
            f"{roles} x {_ROWS_PER_ROLE_OUTSIDE_THE_SIMULATE_MATRIX} per-role rows "
            "(intended_grants_parity, role_exists, no_dynamodb_wildcard, and at least one "
            "leading_keys row), derived from table_action_role_matrix.json and infra/main.tf"
        ),
    )


def checks(ctx: SmokeContext) -> Iterator[CheckResult]:  # noqa: C901
    """Yield one `CheckResult` per (role, assertion) pair across the full allow/deny matrix.

    Never raises for a check failure: an absent role, a missing simulation result, or an
    over-grant is a `fail` with an actionable detail. A generator, so an exception from a later role
    keeps every row already yielded.
    """
    matrix = _load_matrix()
    problems = matrix_shape_problems(matrix)
    if problems:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="matrix_readable",
            status="fail",
            detail=(
                f"table_action_role_matrix.json is not readable: {'; '.join(problems)}. No role "
                "was examined"
            ),
        )
        return
    resolved = _resolve_intended_grants(matrix)
    documented, computed, computed_error = resolved.documented, resolved.computed, resolved.error
    intended = resolved.intended
    functions = sorted(intended)
    table_partition_keys = matrix["tables"]
    dynamodb_table_names = ctx.outputs.get("dynamodb_table_names", {})
    account_id = ctx.client("sts").get_caller_identity()["Account"]
    try:
        index_only = _terraform_index_only_grants()
        scoped_indexes = terraform_tool_query_indexes()
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        index_only = {}
        scoped_indexes = {}
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="index_only_grants_readable",
            status="fail",
            detail=(
                f"could not read infra/main.tf's index-only grants: {exc}. Every index-only grant "
                "is simulated against its base-table ARN instead, so its own-grant row fails"
            ),
        )

    try:
        own_keys = conditioned_own_keys()
        scopes = conditioned_scopes()
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        own_keys, scopes = {}, {}
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="conditioned_grants_readable",
            status="fail",
            detail=(
                f"could not read the sweep and tool roles' leading_key_condition values: {exc}. "
                "Their own conditioned grants the matrix declares are refused rather than simulated, "
                f"and each conditioned_scope_denied row probes only {PROBE_FOREIGN_KEY!r}"
            ),
        )

    matrix_conditioned = matrix_conditioned_grants(matrix, index_only)
    conditioned = matrix_conditioned | scopes.keys()
    parsed = {triple for triple in scopes if triple[0] in intended}
    declared = {triple for triple in matrix_conditioned if triple[0] in intended}
    if parsed != declared:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="conditioned_grants_parity",
            status="fail",
            detail=(
                "infra/*.tf's leading_key_condition grants and table_action_role_matrix.json's "
                f"leading_keys/key_prefix grants disagree: terraform-only {sorted(parsed - declared)}, "
                f"matrix-only {sorted(declared - parsed)}"
            ),
        )

    def table_arn(table_name: str) -> str:
        # `dynamodb_table_names` maps logical table name -> deployed physical table name
        # (e.g. "crm-agent-evalaccounts"), not an ARN, so it is rendered into one here.
        physical_name = (
            dynamodb_table_names.get(table_name) if isinstance(dynamodb_table_names, dict) else None
        )
        resolved_name = str(physical_name) if physical_name else table_name
        return f"arn:aws:dynamodb:{ctx.region}:{account_id}:table/{resolved_name}"

    document_cache: dict[str, tuple[list[dict[str, Any]], Exception | None]] = {}
    for role_function in functions:
        role_name = _role_name(role_function)
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        own_actions = intended[role_function]

        # --- The documented matrix must equal the IaC's computed grants for this role. ---
        documented_pairs = documented.get(role_function, set())
        computed_pairs = computed.get(role_function, set())
        if computed_error:
            yield (
                CheckResult(
                    resource=role_name,
                    check="intended_grants_parity",
                    status="fail",
                    detail=computed_error,
                )
            )
        elif documented_pairs == computed_pairs:
            yield (CheckResult(resource=role_name, check="intended_grants_parity", status="pass"))
        else:
            yield (
                CheckResult(
                    resource=role_name,
                    check="intended_grants_parity",
                    status="fail",
                    detail=(
                        "infra/main.tf's local.tool_dynamodb_grants and "
                        "table_action_role_matrix.json disagree on this function's intended "
                        f"grants: terraform-only {sorted(computed_pairs - documented_pairs)!r}, "
                        f"matrix-only {sorted(documented_pairs - computed_pairs)!r}"
                    ),
                )
            )

        # --- The role must exist before any decision about it means anything. ---
        existence = _role_exists(ctx, role_name)
        if existence is not True:
            detail = (
                f"role {role_name} does not exist, so it was NOT examined. Every simulate below is "
                "skipped deliberately: SimulatePrincipalPolicy raises NoSuchEntity for a "
                "non-existent principal, so no decision "
                "about this role could be read. Check the matrix key renders the deployed function "
                "name."
                if existence is False
                else f"could not establish whether role {role_name} exists ({existence}); an "
                "unestablished principal is not simulated against, because no decision read for "
                "it could be attributed to a scoped policy"
            )
            yield (
                CheckResult(resource=role_name, check="role_exists", status="fail", detail=detail)
            )
            continue
        yield (CheckResult(resource=role_name, check="role_exists", status="pass"))

        if role_function in SWEEP_GRANT_LOCALS:
            try:
                sweep_grants = terraform_local_grants(SWEEP_GRANT_LOCALS[role_function])
            except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
                yield CheckResult(
                    resource=role_name,
                    check="extra_conditions_readable",
                    status="fail",
                    detail=f"could not read {SWEEP_GRANT_LOCALS[role_function]}: {exc}",
                )
            else:
                yield from extra_condition_rows(
                    ctx, role_name, role_arn, role_function, sweep_grants, table_arn
                )

        for (probe_function, probe_table), probe_actions in OWN_TABLE_DENIAL_PROBES.items():
            if probe_function != role_function:
                continue
            for probe_action in probe_actions:
                yield _own_table_denied_check(
                    ctx,
                    role_name,
                    role_arn,
                    role_function,
                    probe_table,
                    probe_action,
                    table_arn(probe_table),
                )

        # --- Full allow/deny matrix: this role against every function's actions. ---
        #
        # A non-own pair emits two rows (denied; denied explicitly). An own pair emits one.
        for other_function in functions:
            for table, action in sorted(intended[other_function]):
                is_own_grant = (table, action) in own_actions
                # An own pair is judged by this role's own grant shape, not the other function's.
                index_name = index_only.get(
                    (role_function if is_own_grant else other_function, table, action)
                )
                resource_arn = (
                    table_arn(table)
                    if index_name is None
                    else f"{table_arn(table)}/index/{index_name}"
                )
                check_name = f"simulate:{other_function}:{action}@{table}"
                explicit_check = f"explicit_deny:{other_function}:{action}@{table}"
                probe_keys: list[str] | None = None
                scope_pair_due = (
                    is_own_grant
                    and index_name is None
                    and other_function == role_function
                    and (role_function, table, action) in conditioned
                )
                if is_own_grant and index_name is not None:
                    in_scope_key = INDEX_IN_SCOPE_LEADING_KEY.get(index_name)
                    if in_scope_key is None:
                        yield CheckResult(
                            resource=role_name,
                            check=check_name,
                            status="fail",
                            detail=(
                                f"no fixed in-scope dynamodb:LeadingKeys value for index "
                                f"{index_name!r} in INDEX_IN_SCOPE_LEADING_KEY, so this own "
                                f"index-only grant on {resource_arn} cannot be simulated"
                            ),
                        )
                        continue
                    probe_keys = [in_scope_key]
                elif is_own_grant:
                    own_key = own_keys.get((role_function, table, action))
                    if own_key is not None:
                        probe_keys = [own_key]
                    elif (role_function, table, action) in conditioned:
                        yield CheckResult(
                            resource=role_name,
                            check=check_name,
                            status="fail",
                            detail=(
                                "conditioned grant with no in-scope dynamodb:LeadingKeys value; "
                                "a context-less simulate passes ForAllValues vacuously, so it is "
                                "refused"
                            ),
                        )
                        if scope_pair_due:
                            yield from _conditioned_scope_rows(
                                ctx,
                                role_name,
                                role_arn,
                                role_function,
                                table,
                                action,
                                resource_arn,
                                scopes.get((role_function, table, action), ()),
                                [k for (_f, t, _a), k in own_keys.items() if t == table],
                            )
                        continue

                # A non-own index-only pair must be refused on the index and on the base table.
                simulated_arns = [resource_arn]
                if index_name is not None and not is_own_grant:
                    simulated_arns.append(table_arn(table))
                decisions: dict[str, str] = {}
                inconclusive = ""
                for simulated_arn in simulated_arns:
                    try:
                        arn_decision, _ = _simulate_evaluation(
                            ctx, role_arn, action, simulated_arn, probe_keys or None
                        )
                    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
                        inconclusive = f"simulate_principal_policy raised: {exc}"
                        break
                    if arn_decision == "no_result":
                        inconclusive = (
                            f"no EvaluationResults returned for role {role_name!r}, action "
                            f"{action!r} on {simulated_arn}"
                        )
                        break
                    if arn_decision not in _DECISION_PERMISSIVENESS:
                        inconclusive = (
                            f"unrecognised EvalDecision {arn_decision!r} for role {role_name!r}, "
                            f"action {action!r} on {simulated_arn}"
                        )
                        break
                    decisions[simulated_arn] = arn_decision
                decision = _most_permissive(decisions.values()) if decisions else ""
                allowed_arns = [arn for arn, value in decisions.items() if value == "allowed"]

                if inconclusive:
                    # An unanswered simulate fails both rows.
                    yield (
                        CheckResult(
                            resource=role_name,
                            check=check_name,
                            status="fail",
                            detail=inconclusive,
                        )
                    )
                    if not is_own_grant:
                        yield (
                            CheckResult(
                                resource=role_name,
                                check=explicit_check,
                                status="fail",
                                detail=inconclusive,
                            )
                        )
                    if scope_pair_due:
                        yield from _conditioned_scope_rows(
                            ctx,
                            role_name,
                            role_arn,
                            role_function,
                            table,
                            action,
                            resource_arn,
                            scopes.get((role_function, table, action), ()),
                            [k for (_f, t, _a), k in own_keys.items() if t == table],
                        )
                    continue

                if is_own_grant:
                    status: Status = "pass" if decision == "allowed" else "fail"
                    detail = "" if status == "pass" else f"expected allowed, got {decision}"
                    keys = f" with LeadingKeys={probe_keys}" if probe_keys else ""
                    if index_name is not None:
                        detail = f"{detail} (index-only grant, simulated on {resource_arn}{keys})".lstrip()
                    elif probe_keys:
                        detail = f"{detail} (conditioned grant, simulated{keys})".lstrip()
                    yield (
                        CheckResult(
                            resource=role_name, check=check_name, status=status, detail=detail
                        )
                    )
                    if index_name is not None and other_function == role_function:
                        yield _index_only_base_table_check(
                            ctx, role_name, role_arn, role_function, table, action, table_arn(table)
                        )
                        for extra_index in scoped_indexes.get((role_function, table), ())[1:]:
                            yield _scoped_index_allowed_check(
                                ctx,
                                role_name,
                                role_arn,
                                role_function,
                                table,
                                action,
                                f"{table_arn(table)}/index/{extra_index}",
                            )
                        if index_name == TENANT_INDEX:
                            yield _index_scope_denied_check(
                                ctx, role_name, role_arn, role_function, table, action, resource_arn
                            )
                    if scope_pair_due:
                        yield from _conditioned_scope_rows(
                            ctx,
                            role_name,
                            role_arn,
                            role_function,
                            table,
                            action,
                            resource_arn,
                            scopes.get((role_function, table, action), ()),
                            [k for (_f, t, _a), k in own_keys.items() if t == table],
                        )
                    continue

                # --- Claim 1: the action is denied (pass/fail only). ---
                if decision == "allowed":
                    yield (
                        CheckResult(
                            resource=role_name,
                            check=check_name,
                            status="fail",
                            detail=(
                                f"role {role_name!r} is allowed {action!r} on {table!r}, which "
                                f"belongs to function {other_function!r}, not {role_function!r} "
                                f"(over-grant; allowed on {allowed_arns})"
                            ),
                        )
                    )
                else:
                    yield (
                        CheckResult(
                            resource=role_name,
                            check=check_name,
                            status="pass",
                            detail=f"denied ({decision}) on {sorted(decisions)}",
                        )
                    )

                # --- Claim 2: that denial is explicit rather than implicit. ---
                if decision == "explicitDeny":
                    yield (
                        CheckResult(
                            resource=role_name,
                            check=explicit_check,
                            status="pass",
                            detail="explicitDeny",
                        )
                    )
                elif decision == "implicitDeny":
                    yield (
                        CheckResult(
                            resource=role_name,
                            check=explicit_check,
                            status="fail",
                            detail=_explicit_deny_detail(role_name, action, table, check_name),
                        )
                    )
                else:
                    # Allowed: an over-grant, so this row fails rather than defers.
                    yield (
                        CheckResult(
                            resource=role_name,
                            check=explicit_check,
                            status="fail",
                            detail=(
                                f"no denial to be explicit about: {action!r} on {table!r} is "
                                f"ALLOWED - see the over-grant on the {check_name!r} row"
                            ),
                        )
                    )

        # --- No dynamodb:* (or equivalent wildcard) on this role's policies. ---
        documents, documents_error = _role_documents(ctx, role_name, document_cache)
        if documents_error is not None:
            yield (
                CheckResult(
                    resource=role_name,
                    check="no_dynamodb_wildcard",
                    status="fail",
                    detail=f"could not load role policies: {documents_error}",
                )
            )
            yield CheckResult(
                resource=role_name,
                check="leading_keys:policies_unreadable",
                status="fail",
                detail=(
                    f"could not load role policies ({documents_error}), so no statement's "
                    "dynamodb:LeadingKeys condition was examined"
                ),
            )
            continue

        wildcard_reasons = [
            reason
            for document in documents
            for statement in _statements(document)
            if (reason := _has_dynamodb_wildcard(statement)) is not None
        ]
        if wildcard_reasons:
            yield (
                CheckResult(
                    resource=role_name,
                    check="no_dynamodb_wildcard",
                    status="fail",
                    detail="; ".join(wildcard_reasons),
                )
            )
        else:
            yield (CheckResult(resource=role_name, check="no_dynamodb_wildcard", status="pass"))

        # --- LeadingKeys present where the access pattern allows. ---
        #
        # Statement resources are resolved through the logical->physical mapping. A DynamoDB Allow
        # whose resource resolves to no table this deployment owns is a `fail`.
        physical_map = _physical_to_logical(dynamodb_table_names)
        unresolved_arns: list[str] = []
        emitted_leading_keys = False
        for document in documents:
            for statement in _statements(document):
                if statement.get("Effect") != "Allow":
                    continue
                if not any(
                    action == "*" or action.startswith("dynamodb:")
                    for action in _action_list(statement)
                ):
                    continue
                for resource_arn in _resource_arns(statement):
                    matched_table = _matrix_table_for_resource_arn(
                        resource_arn, table_partition_keys, physical_map
                    )
                    if matched_table is None:
                        unresolved_arns.append(resource_arn)
                        continue
                    table_info = table_partition_keys[matched_table]
                    check_name = f"leading_keys:{matched_table}"
                    emitted_leading_keys = True
                    vacuous = _vacuous_for_all_values_actions(statement)
                    if vacuous:
                        yield CheckResult(
                            resource=role_name,
                            check=check_name,
                            status="fail",
                            detail=(
                                f"a ForAllValues dynamodb:LeadingKeys condition also allows "
                                f"{vacuous}, outside KEYED_DYNAMODB_ACTIONS, so the condition may "
                                "hold vacuously and scope nothing for them"
                            ),
                        )
                    elif not table_info.get("leading_keys_can_enforce_rep_isolation", False):
                        yield (
                            CheckResult(
                                resource=role_name,
                                check=check_name,
                                status="skip",
                                detail=(
                                    f"table {matched_table!r} partition key is a synthetic "
                                    "record id, not the rep id - LeadingKeys cannot correlate "
                                    "caller identity here; isolation is enforced in "
                                    f"application code ({table_info.get('reason', '')})"
                                ),
                            )
                        )
                    elif _has_leading_keys_condition(statement):
                        yield (
                            CheckResult(
                                resource=role_name,
                                check=check_name,
                                status="pass",
                                detail=(
                                    "dynamodb:LeadingKeys pinned to "
                                    f"{sorted(_leading_keys_values(statement))!r} on "
                                    f"{sorted(_action_list(statement))!r}"
                                ),
                            )
                        )
                        # For a reps statement with LeadingKeys pinned to exactly {"eval-*"}, a
                        # context-carrying simulate must deny a demo rep id and allow an eval-tenant
                        # rep id. Gated to that exact statement shape.
                        if (
                            matched_table == "reps"
                            and "dynamodb:GetItem" in _action_list(statement)
                            and set(_leading_keys_values(statement)) == {"eval-*"}
                        ):
                            denied = _probe_decision(
                                ctx, role_arn, "dynamodb:GetItem", resource_arn, ["rep-ashford"]
                            )
                            yield CheckResult(
                                resource=role_name,
                                check="leading_keys:reps_demo_rep_denied",
                                status="pass"
                                if denied in ("implicitDeny", "explicitDeny")
                                else "fail",
                                detail=(
                                    "GetItem on reps with dynamodb:LeadingKeys=['rep-ashford'] (a demo "
                                    f"rep) evaluated {denied}; the eval-* scope must DENY an existing "
                                    "rep row"
                                ),
                            )
                            allowed = _probe_decision(
                                ctx, role_arn, "dynamodb:GetItem", resource_arn, ["eval-smoke-run"]
                            )
                            yield CheckResult(
                                resource=role_name,
                                check="leading_keys:reps_eval_tenant_allowed",
                                status="pass" if allowed == "allowed" else "fail",
                                detail=(
                                    "GetItem on reps with dynamodb:LeadingKeys=['eval-smoke-run'] "
                                    f"evaluated {allowed}; an eval-tenant rep id must be allowed "
                                    "(positive control, so the deny above is scope, not a blanket refusal)"
                                ),
                            )
                    elif (role_name, matched_table) in _LEADING_KEYS_ADMIN_EXCEPTIONS:
                        yield (
                            CheckResult(
                                resource=role_name,
                                check=check_name,
                                status="skip",
                                detail=_LEADING_KEYS_ADMIN_EXCEPTIONS[(role_name, matched_table)],
                            )
                        )
                    else:
                        yield (
                            CheckResult(
                                resource=role_name,
                                check=check_name,
                                status="fail",
                                detail=(
                                    f"table {matched_table!r} can enforce rep isolation via "
                                    "LeadingKeys but no such condition is present on this grant"
                                ),
                            )
                        )
        if unresolved_arns:
            yield (
                CheckResult(
                    resource=role_name,
                    check="leading_keys:unresolved_table_resource",
                    status="fail",
                    detail=(
                        "DynamoDB Allow statement(s) name resource ARN(s) that resolve to no "
                        f"table this deployment owns: {sorted(set(unresolved_arns))!r}"
                    ),
                )
            )
        elif not emitted_leading_keys:
            yield (
                CheckResult(
                    resource=role_name,
                    check="leading_keys:no_dynamodb_grant",
                    status="skip",
                    detail="this role's policies carry no DynamoDB Allow statement to assert on",
                )
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the IAM roles smoke checks and return the process exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
