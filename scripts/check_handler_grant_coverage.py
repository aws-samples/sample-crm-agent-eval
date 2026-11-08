#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Coverage gate: every action Lambda's declared DynamoDB grants must match the store calls its own
source actually reaches — no grant missing, no grant unreachable.

Compares three sources: `infra/main.tf`'s `local.tool_dynamodb_grants`,
`infra/modules/lambda_action/table_action_role_matrix.json`, and the handler code itself.

For every entry point the analyser walks the transitive call chain through the first-party modules
in `SOURCE_ROOTS`, resolves each store call's logical table name, and maps the store method to the
DynamoDB actions it implies via `STORE_METHOD_ACTIONS`.

Coverage is asserted in both directions, against both declared sources:

* every implied `(function, table, action)` must be granted;
* every granted `(function, table, action)` must be reachable, or be listed in
  `DECLARED_SURPLUS_GRANTS` with a reason.

A table expression the analyser cannot resolve must be declared in `DECLARED_TABLE_DOMAINS`, and a
store call site in scope that the analysis never visited fails the gate (the anti-vacuity check).
The same comparison also covers each role's KMS and log-group actions.

Usage: `python3 scripts/check_handler_grant_coverage.py [--matrix PATH] [--main-tf PATH]
[--module-tf PATH]`

Exit codes: 0 pass; 1 coverage findings; 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crm.store.schema import TABLE_NAMES, TENANT_SCOPED_TABLES  # noqa: E402

DEFAULT_MATRIX = REPO_ROOT / "infra" / "modules" / "lambda_action" / "table_action_role_matrix.json"
DEFAULT_MAIN_TF = REPO_ROOT / "infra" / "main.tf"
DEFAULT_MODULE_TF = REPO_ROOT / "infra" / "modules" / "lambda_action" / "main.tf"

#: Top-level packages under `src/` the analyser treats as first-party and follows calls into.
SOURCE_ROOTS: Final[tuple[str, ...]] = ("crm", "gateway", "eval")

#: The store module itself, excluded from the anti-vacuity call-site scan.
_STORE_MODULE: Final[str] = "crm.store.store"

#: Harness-only modules mapped to the exact number of their store call sites no entry point reaches.
_HARNESS_ONLY_UNREACHED_SITES: Final[dict[str, int]] = {"eval.tenancy": 2}

#: Matrix function names expected to reach each exempted module (key: dotted module path; values:
#: hyphenated matrix function names).
_EXEMPTION_REACHED_BY: Final[dict[str, frozenset[str]]] = {
    "eval.tenancy": frozenset({"sweep-control"}),
}

#: Store call sites whose Lambda entry point is declared by a later task, mapped to the exact set of
#: functions enclosing them. Empty: every sweep call site is reached from `eval.sfn_handlers`.
_PENDING_ENTRY_POINT_SITES: Final[dict[str, frozenset[str]]] = {}

#: Deployed functions whose handler module a later task adds, so they have no entry point or matrix
#: row yet; only their KMS call site is checked. Empty since `eval.sfn_handlers` exists.
PENDING_FUNCTIONS: Final[frozenset[str]] = frozenset()


@dataclass(frozen=True)
class StoreMethod:
    """One `crm.store.store.Store` method and the DynamoDB actions reaching it implies.

    Exactly one of `table_arg` and `tables` names where an action-bearing method's actions land; the
    analyser reports a method that declares actions but neither as a finding.

    Attributes:
        actions: The `dynamodb:*` actions an IAM policy must grant on the method's target table.
        table_arg: Position of the logical table name in the call's arguments, or `None` when the
            method's target is not an argument.
        tables: The fixed set of logical tables the method fans out over, for a method whose target
            is not an argument at all. `None` when the target comes from `table_arg`.
    """

    actions: frozenset[str]
    table_arg: int | None = 0
    tables: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.actions and self.table_arg is not None and self.tables is not None:
            raise ValueError(
                "a StoreMethod must name its target either by argument position or by fan-out "
                "table set, never both"
            )


#: The Store-method -> DynamoDB-action mapping, stating each method's transitive requirement.
STORE_METHOD_ACTIONS: Final[dict[str, StoreMethod]] = {
    "get_item": StoreMethod(frozenset({"dynamodb:GetItem"})),
    "put_item": StoreMethod(frozenset({"dynamodb:PutItem"})),
    "put_if_absent": StoreMethod(frozenset({"dynamodb:PutItem"})),
    "update_item": StoreMethod(frozenset({"dynamodb:UpdateItem"})),
    "delete_item": StoreMethod(frozenset({"dynamodb:DeleteItem"})),
    # Re-reads the item at its own primary key before writing.
    "put_idempotent": StoreMethod(frozenset({"dynamodb:GetItem", "dynamodb:PutItem"})),
    # PutRequest entries inside BatchWriteItem; no PutItem is issued.
    "batch_put_items": StoreMethod(frozenset({"dynamodb:BatchWriteItem"})),
    # BatchGetItem is a distinct IAM action; GetItem does not grant it.
    "batch_get_items": StoreMethod(frozenset({"dynamodb:BatchGetItem"})),
    "query_gsi": StoreMethod(frozenset({"dynamodb:Query"})),
    # Delegates to query_gsi("gsi_tenant").
    "query_tenant_scan": StoreMethod(frozenset({"dynamodb:Query"})),
    "count_tenant": StoreMethod(frozenset({"dynamodb:Query"})),
    # Operator scripts only; no handler role is granted Scan.
    "scan_key_prefix": StoreMethod(frozenset({"dynamodb:Scan"})),
    # Fans out over every tenant-scoped table; deletes are DeleteRequest entries inside
    # BatchWriteItem, so no DeleteItem is issued.
    "delete_tenant": StoreMethod(
        frozenset({"dynamodb:Query", "dynamodb:BatchWriteItem"}),
        table_arg=None,
        tables=TENANT_SCOPED_TABLES,
    ),
    # Each operation's action is attributed at its construction site (TRANSACT_ITEM_ACTIONS).
    "transact_write": StoreMethod(frozenset(), table_arg=None),
    # Resolves a logical name to the physical one; no API call.
    "physical_name": StoreMethod(frozenset(), table_arg=None),
}

#: The IAM action authorizing one `TransactWriteItems` operation on its own table, keyed by its
#: action key. `TransactWriteItems` is an API operation, not an IAM action.
TRANSACT_ITEM_ACTIONS: Final[dict[str, frozenset[str]]] = {
    "Put": frozenset({"dynamodb:PutItem"}),
    "Update": frozenset({"dynamodb:UpdateItem"}),
    "Delete": frozenset({"dynamodb:DeleteItem"}),
    "ConditionCheck": frozenset({"dynamodb:ConditionCheckItem"}),
}

#: First-party helpers that build one transact-item operation, mapped to the action key they build.
#: Their first positional argument is the logical table name.
TRANSACT_ITEM_BUILDERS: Final[dict[str, str]] = {
    # crm.handlers.wire.put_operation -> {"Put": {"TableName": table, ...}}
    "put_operation": "Put",
}

#: Non-tool entry points: matrix function name -> (module path, entry function names). Action
#: Lambdas are discovered separately by :func:`discover_action_entry_points`.
EXTRA_ENTRY_POINTS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "identity-interceptor": (
        "gateway.identity_interceptor",
        # `_default_rep_lookup` is the production `RepLookup`, injected rather than called by name.
        ("lambda_handler", "_default_rep_lookup"),
    ),
    "pre-token-generation": ("crm.auth.pre_token_generation", ("lambda_handler",)),
    "eval-trigger": ("crm.handlers.eval_trigger", ("handler", "_eval_trigger")),
    "user-settings": ("crm.handlers.user_settings", ("handler",)),
    "chat-target": ("gateway.chat_target", ("lambda_handler", "handle")),
    # Gateway #2's own instance of the identity interceptor.
    "chat-identity-interceptor": (
        "gateway.identity_interceptor",
        ("lambda_handler", "_default_rep_lookup"),
    ),
    "sweep-control": ("eval.sfn_handlers", ("sweep_control_handler",)),
    "sweep-task": ("eval.sfn_handlers", ("sweep_task_handler",)),
    "capabilities": ("crm.handlers.capabilities", ("handler",)),
}

#: Additional reachability roots, consumed alongside :data:`EXTRA_ENTRY_POINTS`, for a function whose
#: work is invoked through a parameter the call-graph walk cannot follow.
_ADDITIONAL_ROOTS: Final[dict[str, tuple[tuple[str, tuple[str, ...]], ...]]] = {}


#: Matrix functions whose grants are not in `local.tool_dynamodb_grants`; they are compared against
#: the matrix only.
NON_TOOL_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "identity-interceptor",
        "pre-token-generation",
        "eval-trigger",
        "user-settings",
        "chat-target",
        "chat-identity-interceptor",
        "sweep-control",
        "sweep-task",
    }
)


@dataclass(frozen=True)
class DeclaredDomain:
    """A table expression the analyser cannot resolve statically, and its declared value domain."""

    tables: frozenset[str]
    reason: str


#: The tables a golden task's `must_not_change` / `expected_field_changes` entries may name: the
#: seeded corpus tables.
GOLDEN_TASK_ASSERTION_TABLES: Final[frozenset[str]] = frozenset(
    {"accounts", "contacts", "opportunities", "activities", "notes"}
)

#: Table expressions that do not resolve to a literal, keyed by
#: `<module>:<function>:<expression source>`, each with its declared domain and reason.
DECLARED_TABLE_DOMAINS: Final[dict[str, DeclaredDomain]] = {
    "crm.handlers.submit_proposal:_submit_proposal:str(arguments['target_table'])": DeclaredDomain(
        frozenset({"accounts", "contacts", "opportunities", "activities"}),
        "Request-supplied target table; crm.proposals._require_table refuses any other value.",
    ),
    "crm.handlers.approve_proposal:_approve_proposal:str(proposal['target_table'])": DeclaredDomain(
        frozenset({"accounts", "contacts", "opportunities", "activities"}),
        "Recorded on the proposal by submit_proposal, which validated it against TABLES.",
    ),
    "crm.handlers.reject_proposal:_reject_proposal:str(proposal['target_table'])": DeclaredDomain(
        frozenset({"accounts", "contacts", "opportunities", "activities"}),
        "Recorded on the proposal by submit_proposal, which validated it against TABLES.",
    ),
    "crm.proposals:approve_proposal:str(proposal['target_table'])": DeclaredDomain(
        frozenset({"accounts", "contacts", "opportunities", "activities"}),
        "Recorded on the proposal by submit_proposal, which validated it against TABLES.",
    ),
    "crm.handlers.rebase_proposal:_rebase_proposal:str(proposal['target_table'])": DeclaredDomain(
        frozenset({"accounts", "contacts", "opportunities", "activities"}),
        "Recorded on the proposal by submit_proposal; the handler reads it only when in TABLES.",
    ),
    "crm.proposals:rebase_proposal:str(proposal['target_table'])": DeclaredDomain(
        frozenset({"accounts", "contacts", "opportunities", "activities"}),
        "Recorded on the proposal by submit_proposal; rebase_proposal refuses any value outside "
        "TABLES before reading.",
    ),
    "crm.audit:_refuse_while_children_exist:child_table": DeclaredDomain(
        frozenset({"contacts", "opportunities", "activities", "notes"}),
        "Iterates crm.audit._CHILD_INDEXES, whose child tables are exactly these four.",
    ),
    "crm.audit:revert:str(item['target_table'])": DeclaredDomain(
        frozenset({"accounts", "contacts", "opportunities", "activities", "notes"}),
        "Recorded on the audit record by the write path that produced it.",
    ),
    "crm.handlers.get_page_context:_resolve_entity:context.entity_type": DeclaredDomain(
        frozenset({"accounts", "contacts", "opportunities", "activities"}),
        "Request-supplied entity_type; crm.toolspec.page_context.EntityType admits only these four "
        "and crm.handlers.get_page_context._KEY_ATTRIBUTE is keyed by the same set.",
    ),
    "eval.tenancy:find_canary_records:table": DeclaredDomain(
        TENANT_SCOPED_TABLES,
        "Iterates sorted(TENANT_SCOPED_TABLES) directly — the same set Store.delete_tenant walks, so "
        "no table a sweep could have written is left unwatched.",
    ),
    "eval.scoring:capture_pre_turn_values:table": DeclaredDomain(
        GOLDEN_TASK_ASSERTION_TABLES,
        "Reads the (table, id, field) triples a task's must_not_change / expected_field_changes "
        "blocks name; tests/unit/test_check_handler_grant_coverage.py pins every table "
        "src/eval/golden_tasks.yaml names to this set.",
    ),
    "eval.scoring:_check_must_not_change:str(entry['table'])": DeclaredDomain(
        GOLDEN_TASK_ASSERTION_TABLES,
        "Table named by a golden task's must_not_change entry; bounded as above.",
    ),
    "eval.scoring:_resolve_expected_field_change:str(change['table'])": DeclaredDomain(
        GOLDEN_TASK_ASSERTION_TABLES,
        "Table named by a golden task's expected_field_changes entry; bounded as above.",
    ),
    "eval.tenant_guard:store_row_reader:table": DeclaredDomain(
        TENANT_SCOPED_TABLES,
        "The reader is called only with `tables=WATCHED_TABLES`, which is "
        "`tuple(sorted(TENANT_SCOPED_TABLES))` — the same set Store.delete_tenant walks, so no table "
        "a sweep could have written is left unwatched.",
    ),
    "eval.tenant_guard:store_count_reader:table": DeclaredDomain(
        TENANT_SCOPED_TABLES,
        "The counter is called only with `tables=WATCHED_TABLES`, which is "
        "`tuple(sorted(TENANT_SCOPED_TABLES))`, and Store.count_tenant refuses any other table.",
    ),
}


@dataclass(frozen=True)
class SurplusGrant:
    """A granted action no handler reaches, declared with the reason it is nonetheless kept."""

    function: str
    table: str
    action: str
    reason: str


_UPDATE_ITEM_WHOLE_RECORD_PUT = (
    "Declared over-privilege, kept pending a least-privilege narrowing: this write path replaces "
    "the whole record with a transact-item Put, so UpdateItem is never issued. Removing it is an "
    "IAM change with its own live re-verification."
)

_REVERT_TARGET_READ = (
    "Declared over-privilege, kept pending a least-privilege narrowing: crm.audit.revert restores "
    "the target from the audit record's own before-snapshot and never re-reads the target row, so "
    "GetItem is never issued."
)

#: Reached actions no grant needs to cover, one row per (function, table, action), each with the
#: reason the reach is not real. The analyser attributes revert's transact Put and Delete to every
#: table in its target-table domain; a notes revert is delete-only.
DECLARED_UNREACHABLE_GRANTS: Final[tuple[SurplusGrant, ...]] = (
    SurplusGrant(
        "revert_audit_record",
        "notes",
        "dynamodb:PutItem",
        "Notes are written only by create_note, whose audit carries no before-snapshot, so a notes "
        "revert always takes the Delete branch; crm.audit refuses a notes revert that would Put, and "
        "tests/unit/test_notes_revert_delete_only.py fails if any writer records a notes audit with a "
        "before-snapshot.",
    ),
)

#: Every granted action no entry point reaches, one row per (function, table, action).
DECLARED_SURPLUS_GRANTS: Final[tuple[SurplusGrant, ...]] = (
    SurplusGrant(
        "update_account", "accounts", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "update_contact", "contacts", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "update_opportunity", "opportunities", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "update_activity", "activities", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "log_activity", "opportunities", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "approve_proposal", "accounts", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "approve_proposal", "contacts", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "approve_proposal", "opportunities", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "approve_proposal", "activities", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "revert_audit_record", "accounts", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "revert_audit_record", "contacts", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "revert_audit_record", "opportunities", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant(
        "revert_audit_record", "activities", "dynamodb:UpdateItem", _UPDATE_ITEM_WHOLE_RECORD_PUT
    ),
    SurplusGrant("revert_audit_record", "accounts", "dynamodb:GetItem", _REVERT_TARGET_READ),
    SurplusGrant("revert_audit_record", "contacts", "dynamodb:GetItem", _REVERT_TARGET_READ),
    SurplusGrant("revert_audit_record", "opportunities", "dynamodb:GetItem", _REVERT_TARGET_READ),
    SurplusGrant("revert_audit_record", "activities", "dynamodb:GetItem", _REVERT_TARGET_READ),
)


@dataclass(frozen=True)
class Unresolved:
    """A table expression that did not reduce to a literal, carrying its declaration key."""

    key: str


Resolution = frozenset[str] | Unresolved

#: (function, table) -> granted actions.
GrantMap = dict[tuple[str, str], set[str]]


@dataclass
class ModuleIndex:
    """Parsed first-party module: its functions, string constants, and import bindings."""

    name: str
    path: Path
    tree: ast.Module
    functions: dict[str, ast.FunctionDef] = field(default_factory=dict)
    constants: dict[str, str] = field(default_factory=dict)
    #: local name -> (module, original name) for `from <module> import <name> [as <local>]`.
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)


def _module_name(path: Path, src_root: Path) -> str:
    relative = path.relative_to(src_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def build_index(src_root: Path) -> dict[str, ModuleIndex]:
    """Parse every first-party module under `src_root` into a `ModuleIndex`."""
    index: dict[str, ModuleIndex] = {}
    for root in SOURCE_ROOTS:
        for path in sorted((src_root / root).rglob("*.py")):
            name = _module_name(path, src_root)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module = ModuleIndex(name=name, path=path, tree=tree)
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    module.functions[node.name] = node
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    _record_constant(module, node)
            # Imports are collected from the whole tree, including function-local imports.
            for imported in ast.walk(tree):
                if isinstance(imported, ast.ImportFrom) and imported.module:
                    for alias in imported.names:
                        module.imports[alias.asname or alias.name] = (
                            imported.module,
                            alias.name,
                        )
            index[name] = module
    return index


def _record_constant(module: ModuleIndex, node: ast.Assign | ast.AnnAssign) -> None:
    value = node.value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        if isinstance(target, ast.Name):
            module.constants[target.id] = value.value


@dataclass
class Analysis:
    """Result of analysing every entry point."""

    #: (function, table) -> implied actions.
    derived: GrantMap
    #: Undeclared unresolvable table expressions, by declaration key.
    unresolved: set[str]
    #: Visited store call sites as `<module>:<lineno>`.
    visited_sites: set[str]
    #: Findings that the harness-only exemption no longer matches the analysis.
    stale_exemptions: list[str] = field(default_factory=list)
    #: Every first-party function (`<module>.<function>`) each entry point's walk reached.
    reached: dict[str, set[str]] = field(default_factory=dict)


class Analyser:
    """Walks the transitive call chain from each entry point, deriving implied grants."""

    def __init__(self, index: dict[str, ModuleIndex]) -> None:
        self._index = index
        self.derived: GrantMap = {}
        self.unresolved: set[str] = set()
        self.visited_sites: set[str] = set()
        self.reached: dict[str, set[str]] = {}
        self._function: str = ""
        self._seen: set[tuple[str, str, frozenset[tuple[str, Resolution]]]] = set()

    def analyse(self, function: str, module: str, entry_points: tuple[str, ...]) -> None:
        """Derive `function`'s implied grants from `entry_points` in `module`.

        The walk memo is per function: a module two functions share is walked for each of them.
        """
        self._function = function
        self._seen = set()
        for entry in entry_points:
            index = self._index.get(module)
            if index is None or entry not in index.functions:
                raise KeyError(f"entry point {module}.{entry} not found")
            self._walk(index, index.functions[entry], {})

    def _emit(self, tables: frozenset[str], actions: frozenset[str]) -> None:
        for table in tables:
            self.derived.setdefault((self._function, table), set()).update(actions)

    def _walk(self, module: ModuleIndex, func: ast.FunctionDef, env: dict[str, Resolution]) -> None:
        memo_key = (module.name, func.name, frozenset(env.items()))
        if memo_key in self._seen:
            return
        self._seen.add(memo_key)
        self.reached.setdefault(self._function, set()).add(f"{module.name}.{func.name}")

        locals_env = dict(env)
        locals_env.update(self._local_bindings(module, func, env))

        for node in ast.walk(func):
            if isinstance(node, ast.Dict):
                self._visit_transact_dict(module, func, node, locals_env)
            if not isinstance(node, ast.Call):
                continue
            self._visit_store_call(module, func, node, locals_env)
            self._visit_builder_call(module, func, node, locals_env)
            self._visit_first_party_call(module, func, node, locals_env)

    def _local_bindings(
        self, module: ModuleIndex, func: ast.FunctionDef, env: dict[str, Resolution]
    ) -> dict[str, Resolution]:
        """Resolve simple local assignments to table names or declared domains.

        Multiple bindings of one name are unioned."""
        bindings: dict[str, Resolution] = {}
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign) or node.value is None:
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                resolved = self._resolve(module, func, node.value, {**env, **bindings})
                previous = bindings.get(target.id)
                if isinstance(resolved, frozenset) and isinstance(previous, frozenset):
                    bindings[target.id] = previous | resolved
                else:
                    bindings[target.id] = resolved
        return bindings

    def _resolve(
        self,
        module: ModuleIndex,
        func: ast.FunctionDef,
        expr: ast.expr,
        env: dict[str, Resolution],
    ) -> Resolution:
        """Reduce `expr` to the set of logical table names it can name."""
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            if expr.value in TABLE_NAMES:
                return frozenset({expr.value})
            return Unresolved(f"{module.name}:{func.name}:{expr.value!r} (not a known table)")
        if isinstance(expr, ast.Name):
            if expr.id in env:
                return env[expr.id]
            constant = module.constants.get(expr.id)
            if constant is not None and constant in TABLE_NAMES:
                return frozenset({constant})
        # Resolve through a `str(x)` coercion.
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "str"
            and len(expr.args) == 1
        ):
            inner = self._resolve(module, func, expr.args[0], env)
            if isinstance(inner, frozenset):
                return inner
        key = f"{module.name}:{func.name}:{ast.unparse(expr)}"
        declared = DECLARED_TABLE_DOMAINS.get(key)
        if declared is not None:
            return declared.tables
        return Unresolved(key)

    def _table_resolution(self, resolution: Resolution, actions: frozenset[str]) -> None:
        if isinstance(resolution, Unresolved):
            self.unresolved.add(resolution.key)
            return
        self._emit(resolution, actions)

    def _visit_store_call(
        self,
        module: ModuleIndex,
        owner: ast.FunctionDef,
        node: ast.Call,
        env: dict[str, Resolution],
    ) -> None:
        if not isinstance(node.func, ast.Attribute):
            return
        method = STORE_METHOD_ACTIONS.get(node.func.attr)
        if method is None:
            return
        self.visited_sites.add(f"{module.name}:{node.lineno}")
        if not method.actions:
            return
        if method.tables is not None:
            self._emit(method.tables, method.actions)
            return
        if method.table_arg is None:
            self.unresolved.add(
                f"{module.name}:{node.lineno}:{node.func.attr} declares actions "
                f"{sorted(method.actions)} but names no table and declares no fan-out table set, "
                f"so they can never be required of any role"
            )
            return
        table_expr = self._argument(node, method.table_arg, "table")
        if table_expr is None:
            self.unresolved.add(f"{module.name}:{node.lineno}:missing table argument")
            return
        resolution = self._resolve(module, owner, table_expr, env)
        self._table_resolution(resolution, method.actions)

    def _visit_builder_call(
        self,
        module: ModuleIndex,
        owner: ast.FunctionDef,
        node: ast.Call,
        env: dict[str, Resolution],
    ) -> None:
        name = self._callee_name(node)
        if name is None:
            return
        action_key = TRANSACT_ITEM_BUILDERS.get(name)
        if action_key is None:
            return
        table_expr = self._argument(node, 0, "table")
        if table_expr is None:
            self.unresolved.add(f"{module.name}:{node.lineno}:missing table argument")
            return
        resolution = self._resolve(module, owner, table_expr, env)
        self._table_resolution(resolution, TRANSACT_ITEM_ACTIONS[action_key])

    def _visit_transact_dict(
        self,
        module: ModuleIndex,
        owner: ast.FunctionDef,
        node: ast.Dict,
        env: dict[str, Resolution],
    ) -> None:
        """Attribute a raw `{"Put": {"TableName": ..., ...}}` transact-item literal."""
        for key_node, value in zip(node.keys, node.values, strict=True):
            if (
                not isinstance(key_node, ast.Constant)
                or key_node.value not in TRANSACT_ITEM_ACTIONS
            ):
                continue
            if not isinstance(value, ast.Dict):
                continue
            for inner_key, inner_value in zip(value.keys, value.values, strict=True):
                if not isinstance(inner_key, ast.Constant) or inner_key.value != "TableName":
                    continue
                resolution = self._resolve(module, owner, inner_value, env)
                self._table_resolution(resolution, TRANSACT_ITEM_ACTIONS[str(key_node.value)])

    def _visit_first_party_call(
        self,
        module: ModuleIndex,
        owner: ast.FunctionDef,
        node: ast.Call,
        env: dict[str, Resolution],
    ) -> None:
        target = self._lookup_module_attribute_call(module, node)
        if target is None:
            name = self._callee_name(node)
            if name is None:
                return
            target = self._lookup_function(module, name)
        if target is None:
            return
        target_module, target_func = target
        bound = self._bind_arguments(module, owner, target_func, node, env)
        self._walk(target_module, target_func, bound)

    def _bind_arguments(
        self,
        module: ModuleIndex,
        owner: ast.FunctionDef,
        target: ast.FunctionDef,
        node: ast.Call,
        env: dict[str, Resolution],
    ) -> dict[str, Resolution]:
        """Bind the callee's parameters to the resolved value of each argument at this call site."""
        bound: dict[str, Resolution] = {}
        positional = [*target.args.posonlyargs, *target.args.args]
        for position, arg in enumerate(node.args):
            if position >= len(positional):
                break
            bound[positional[position].arg] = self._resolve(module, owner, arg, env)
        keyword_names = {a.arg for a in (*positional, *target.args.kwonlyargs)}
        for keyword in node.keywords:
            if keyword.arg in keyword_names:
                bound[str(keyword.arg)] = self._resolve(module, owner, keyword.value, env)
        return bound

    @staticmethod
    def _argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
        if len(node.args) > position:
            return node.args[position]
        for entry in node.keywords:
            if entry.arg == keyword:
                return entry.value
        return None

    @staticmethod
    def _callee_name(node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    def _lookup_module_attribute_call(
        self, module: ModuleIndex, node: ast.Call
    ) -> tuple[ModuleIndex, ast.FunctionDef] | None:
        """Resolve `<submodule>.<func>(...)` where `from <package> import <submodule>` bound the name."""
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            return None
        imported = module.imports.get(func.value.id)
        if imported is None:
            return None
        target_module = self._index.get(f"{imported[0]}.{imported[1]}")
        if target_module is None:
            return None
        target_func = target_module.functions.get(func.attr)
        return None if target_func is None else (target_module, target_func)

    def _lookup_function(
        self, module: ModuleIndex, name: str
    ) -> tuple[ModuleIndex, ast.FunctionDef] | None:
        """Resolve `name` to a first-party function, following this module's import bindings."""
        imported = module.imports.get(name)
        if imported is not None:
            target_module = self._index.get(imported[0])
            if target_module is not None:
                func = target_module.functions.get(imported[1])
                if func is not None:
                    return target_module, func
        func = module.functions.get(name)
        if func is not None:
            return module, func
        return None


def discover_action_entry_points(
    index: dict[str, ModuleIndex],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Return `tool -> (module, entry functions)` for every `src/crm/handlers/<tool>.py`.

    A handler module is recognised by its module-level `handler = build_handler(<action>)`.
    """
    entry_points: dict[str, tuple[str, tuple[str, ...]]] = {}
    for name, module in index.items():
        if not name.startswith("crm.handlers.") or name.rsplit(".", 1)[1].startswith("_"):
            continue
        for node in module.tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "handler" for t in node.targets):
                continue
            call = node.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "build_handler"
                and call.args
                and isinstance(call.args[0], ast.Name)
            ):
                entry_points[name.rsplit(".", 1)[1]] = (name, (call.args[0].id,))
    return entry_points


def derive_grants(src_root: Path | None = None) -> Analysis:
    """Derive every entry point's implied `(function, table) -> actions` from source."""
    index = build_index(src_root if src_root is not None else REPO_ROOT / "src")
    entry_points = {**discover_action_entry_points(index), **EXTRA_ENTRY_POINTS}
    analyser = Analyser(index)
    reached_by: dict[str, set[str]] = {module: set() for module in _EXEMPTION_REACHED_BY}
    roots = [
        (function, module, entries) for function, (module, entries) in sorted(entry_points.items())
    ]
    # An additional root whose module is absent from the index is skipped.
    roots += [
        (function, module, entries)
        for function, extra in sorted(_ADDITIONAL_ROOTS.items())
        for module, entries in extra
        if module in index
    ]
    for function, module, entries in roots:
        before = set(analyser.visited_sites)
        analyser.analyse(function, module, entries)
        for site in analyser.visited_sites - before:
            site_module = site.rsplit(":", 1)[0]
            if site_module in reached_by:
                reached_by[site_module].add(function)
    scan_sites = _scan_call_sites(index)
    unvisited = scan_sites - analyser.visited_sites
    site_functions = {site: _enclosing_function(index, site) for site in unvisited}
    for site in sorted(unvisited):
        module = site.rsplit(":", 1)[0]
        if site_functions[site] in _PENDING_ENTRY_POINT_SITES.get(module, frozenset()):
            continue
        if module in _HARNESS_ONLY_UNREACHED_SITES:
            continue
        analyser.unresolved.add(f"{site}:store call site never reached by any entry point")
    return Analysis(
        derived=analyser.derived,
        unresolved=analyser.unresolved,
        visited_sites=analyser.visited_sites,
        stale_exemptions=_check_harness_exemption(reached_by, site_functions),
        reached=analyser.reached,
    )


def _enclosing_function(index: dict[str, ModuleIndex], site: str) -> str:
    """Return the name of the innermost function enclosing `site` (`module:lineno`), or `""`."""
    module, _, line = site.rpartition(":")
    lineno = int(line)
    enclosing = [
        node
        for node in ast.walk(index[module].tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.lineno <= lineno <= (node.end_lineno or node.lineno)
    ]
    return max(enclosing, key=lambda node: node.lineno).name if enclosing else ""


def _check_harness_exemption(
    reached_by: dict[str, set[str]], site_functions: dict[str, str]
) -> list[str]:
    """Report where the harness-only exemption no longer matches the analysis.

    Checks each exempted module's set of reaching entry points against `_EXEMPTION_REACHED_BY`, its
    unreached call-site count against `_HARNESS_ONLY_UNREACHED_SITES`, and the functions enclosing
    each pending module's unreached sites against `_PENDING_ENTRY_POINT_SITES`.
    """
    findings: list[str] = []

    for module, expected_reachers in sorted(_EXEMPTION_REACHED_BY.items()):
        found_reachers = frozenset(reached_by.get(module, set()))
        if found_reachers != expected_reachers:
            added = sorted(found_reachers - expected_reachers)
            removed = sorted(expected_reachers - found_reachers)
            findings.append(
                f"STALE EXEMPTION {module} is exempted on the claim that only "
                f"{sorted(expected_reachers) or 'no entry point'} reaches it"
                + (f", but {', '.join(added)} now does" if added else "")
                + (f", and {', '.join(removed)} no longer does" if removed else "")
                + ". The exemption asserts no Lambda execution role backs these store calls, which "
                "is only true relative to the Lambdas that exist. Re-derive what the new reacher "
                "needs, grant it, then update `_EXEMPTION_REACHED_BY` — inheriting the exemption "
                "would hide every grant that role needs"
            )

    for module, expected_functions in sorted(_PENDING_ENTRY_POINT_SITES.items()):
        found_functions = frozenset(
            function
            for site, function in site_functions.items()
            if site.rsplit(":", 1)[0] == module and function in expected_functions
        )
        reached = sorted(expected_functions - found_functions)
        if reached:
            findings.append(
                f"STALE EXEMPTION {module} is exempted for unreached store call sites in "
                f"{sorted(expected_functions)}, but {', '.join(reached)} is now reached by an entry "
                "point, so it needs a grant rather than an exemption"
            )

    actual = Counter(
        site.rsplit(":", 1)[0]
        for site, function in site_functions.items()
        if function not in _PENDING_ENTRY_POINT_SITES.get(site.rsplit(":", 1)[0], frozenset())
    )
    for module, expected in sorted(_HARNESS_ONLY_UNREACHED_SITES.items()):
        found = actual[module]
        if found != expected:
            findings.append(
                f"STALE EXEMPTION {module} is exempted for exactly {expected} unreached store call "
                f"site(s) but {found} are unreached now. More means a new harness call site the "
                "exemption would silently absorb; fewer means the analyser now reaches one, so it "
                "needs a grant rather than an exemption"
            )
    return findings


def _scan_call_sites(index: dict[str, ModuleIndex]) -> set[str]:
    """Return every action-bearing store call site in first-party scope, independent of the walk."""
    sites: set[str] = set()
    for name, module in index.items():
        if name == _STORE_MODULE:
            continue
        for node in ast.walk(module.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in STORE_METHOD_ACTIONS
                and STORE_METHOD_ACTIONS[node.func.attr].actions
            ):
                sites.add(f"{name}:{node.lineno}")
    return sites


def load_zero_grant_functions(path: Path) -> frozenset[str]:
    """Load `table_action_role_matrix.json`'s `functions_without_dynamodb_grants` list.

    Raises:
        ValueError: The key is absent or is not a list of strings.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    names = payload.get("functions_without_dynamodb_grants")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError(f"{path}: functions_without_dynamodb_grants must be a list of strings")
    return frozenset(names)


def load_matrix_grants(path: Path) -> GrantMap:
    """Load `table_action_role_matrix.json`'s `grants` array as a grant map."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    grants: GrantMap = {}
    for entry in payload["grants"]:
        key = (str(entry["function"]), str(entry["table"]))
        grants.setdefault(key, set()).update(str(action) for action in entry["actions"])
    if not grants:
        raise ValueError(f"{path} declared no grants")
    return grants


_GRANTS_BLOCK_RE = re.compile(
    r"^  tool_dynamodb_grants = \{\n(?P<body>.*?)^  \}\n", re.DOTALL | re.MULTILINE
)
_TOOL_BLOCK_RE = re.compile(
    r"^    (?P<tool>[a-z_]+) = \[\n(?P<body>.*?)^    \]\n", re.DOTALL | re.MULTILINE
)
_GRANT_RE = re.compile(
    r'\{\s*table\s*=\s*"(?P<table>\w+)",\s*actions\s*=\s*\[(?P<actions>[^\]]*)\]\s*,?\s*\}'
)
_ACTION_RE = re.compile(r'"(dynamodb:\w+)"')

#: Minimum tool count `infra/main.tf` must yield; parsing fewer is an error.
MIN_TERRAFORM_TOOLS: Final[int] = 20


def load_terraform_grants(path: Path) -> GrantMap:
    """Parse `local.tool_dynamodb_grants` out of `infra/main.tf` as a grant map."""
    text = path.read_text(encoding="utf-8")
    block = _GRANTS_BLOCK_RE.search(text)
    if block is None:
        raise ValueError(f"{path}: could not locate local.tool_dynamodb_grants")
    grants: GrantMap = {}
    tools: set[str] = set()
    for tool_match in _TOOL_BLOCK_RE.finditer(block.group("body")):
        tool = tool_match.group("tool")
        tools.add(tool)
        for grant in _GRANT_RE.finditer(tool_match.group("body")):
            actions = set(_ACTION_RE.findall(grant.group("actions")))
            if not actions:
                raise ValueError(f"{path}: {tool}/{grant.group('table')} parsed with no actions")
            grants.setdefault((tool, grant.group("table")), set()).update(actions)
    if len(tools) < MIN_TERRAFORM_TOOLS:
        raise ValueError(
            f"{path}: parsed only {len(tools)} tools from local.tool_dynamodb_grants, "
            f"expected at least {MIN_TERRAFORM_TOOLS} -- the parser is not seeing the real block"
        )
    return grants


# ---------------------------------------------------------------------------
# Non-DynamoDB grants: `(function, resource, action)`. Resource keys carry a leading `@` so they
# cannot collide with a logical table name.
# ---------------------------------------------------------------------------
KMS_RESOURCE_KEY: Final[str] = "@kms:dynamodb-cmk"
CURSOR_KMS_RESOURCE_KEY: Final[str] = "@kms:cursor-cmk"

#: The cursor codec functions and the KMS action each calls on the pagination-cursor key.
CURSOR_CODEC_ACTIONS: Final[dict[str, str]] = {
    "crm.cursor.encode_cursor": "kms:Encrypt",
    "crm.cursor.decode_cursor": "kms:Decrypt",
}
#: The `dynamodb_kms` document's statement that grants the cursor key, by sid.
CURSOR_GRANT_SID: Final[str] = "SealPaginationCursors"
LOGS_RESOURCE_KEY: Final[str] = "@logs:own-log-group"

#: KMS actions the caller needs on the customer-managed key of an SSE-KMS table, implied for every
#: KMS-enabled function.
KMS_IMPLIED_ACTIONS: Final[frozenset[str]] = frozenset({"kms:Decrypt", "kms:GenerateDataKey"})

#: Log-group write actions implied for every function.
LOGS_IMPLIED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"logs:CreateLogStream", "logs:PutLogEvents"}
)


#: Actions the `lambda_action` module grants on a resource key that no derivation implies, keyed by
#: action (applies to every function) with the reason each is kept.
DECLARED_SURPLUS_RESOURCE_ACTIONS: Final[dict[str, str]] = {
    "kms:DescribeKey": (
        "Granted by data.aws_iam_policy_document.dynamodb_kms alongside Decrypt/GenerateDataKey, and not "
        "derivable from any handler call: DynamoDB calls the key on the caller's behalf, so nothing in "
        "`src/` names DescribeKey. Whether the SSE-KMS table path requires it OF THE CALLER is NOT "
        "established here, and this entry deliberately does not claim either way -- removing it is a "
        "plausible least-privilege narrowing whose only honest test is a deployed read with it revoked, "
        "which is T3 and unavailable offline. Kept rather than removed because the failure direction is "
        "asymmetric: a surplus read-only KMS action on one scoped key is a small standing privilege, "
        "while removing a genuinely required one breaks every DynamoDB read at once with "
        "AccessDeniedException. Revisit with the T3 evidence, not by reasoning."
    ),
}


def derive_resource_grants(
    derived: GrantMap,
    kms_enabled: frozenset[str],
    reached: dict[str, set[str]] | None = None,
) -> GrantMap:
    """Implied non-DynamoDB grants per function, keyed by namespaced resource rather than by table.

    `kms_enabled` is the set of functions whose call site sets `dynamodb_kms_grant_enabled = true`; a
    function outside it implies no DynamoDB-key action. `reached` maps each function to the
    first-party functions its walk reached; reaching a cursor codec function implies its KMS action
    on the cursor key.
    """
    implied: GrantMap = {}
    functions = {function for function, _resource in derived}
    for function in sorted(functions):
        implied[(function, LOGS_RESOURCE_KEY)] = set(LOGS_IMPLIED_ACTIONS)
        if function in kms_enabled:
            implied[(function, KMS_RESOURCE_KEY)] = set(KMS_IMPLIED_ACTIONS)
        codec = {
            action
            for name, action in CURSOR_CODEC_ACTIONS.items()
            if name in (reached or {}).get(function, set())
        }
        if codec:
            implied[(function, CURSOR_KMS_RESOURCE_KEY)] = codec
    return implied


_LAMBDA_ACTION_CALL_RE = re.compile(
    r'^module\s+"(?P<name>lambda_action[a-z_0-9]*)"\s*\{(?P<body>.*?)^\}',
    re.DOTALL | re.MULTILINE,
)
_MODULE_ACTIONS_RE = re.compile(
    r'data\s+"aws_iam_policy_document"\s+"(?P<name>logs|dynamodb_kms)"\s*\{(?P<body>.*?)\n\}',
    re.DOTALL,
)
_QUOTED_ACTION_RE = re.compile(r'"((?:kms|logs):\w+)"')

#: Matrix function name for each singleton `module "lambda_action_<suffix>"` call, keyed by suffix.
SINGLETON_CALL_FUNCTIONS: Final[dict[str, str]] = {
    "interceptor": "identity-interceptor",
    "pre_token_generation": "pre-token-generation",
    "eval_trigger": "eval-trigger",
    "user_settings": "user-settings",
    "chat_target": "chat-target",
    "chat_interceptor": "chat-identity-interceptor",
    "sweep_control": "sweep-control",
    "sweep_task": "sweep-task",
}


def expected_kms_enabled_functions(known_functions: frozenset[str]) -> frozenset[str]:
    """Return every function that must carry the KMS grant: `known_functions` and :data:`PENDING_FUNCTIONS`.

    Independent of any call site, so the equality in :func:`load_kms_enabled_functions` can fail.
    """
    return known_functions | PENDING_FUNCTIONS


def load_kms_enabled_functions(main_tf: Path, known_functions: frozenset[str]) -> frozenset[str]:
    """Functions whose module call sets `dynamodb_kms_grant_enabled = true`, as an exact set.

    Handles two call shapes:

    * `module "lambda_action"` with `for_each`, instantiating every tool Lambda; its functions are
      `known_functions` less :data:`NON_TOOL_FUNCTIONS`.
    * singleton `module "lambda_action_<suffix>"` blocks, named by :data:`SINGLETON_CALL_FUNCTIONS`.

    Every root `*.tf` file beside `main_tf` is read, since singleton calls live in several of them.

    Raises `ValueError` unless the parsed set equals :func:`expected_kms_enabled_functions`, or a
    KMS-enabled singleton's suffix is unknown.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(main_tf.parent.glob("*.tf"))
    )
    enabled: set[str] = set()
    for call in _LAMBDA_ACTION_CALL_RE.finditer(text):
        body = call.group("body")
        if not re.search(r"^\s*dynamodb_kms_grant_enabled\s*=\s*true", body, re.MULTILINE):
            continue
        name = call.group("name")
        if re.search(r"^\s*for_each\s*=", body, re.MULTILINE):
            enabled.update(known_functions - NON_TOOL_FUNCTIONS - PENDING_FUNCTIONS)
            continue
        suffix = name.removeprefix("lambda_action_")
        if suffix not in SINGLETON_CALL_FUNCTIONS:
            raise ValueError(
                f'{main_tf}: singleton call `module "{name}"` enables the KMS grant but its suffix '
                f"{suffix!r} is not in SINGLETON_CALL_FUNCTIONS, so this gate cannot say which "
                "function it names. Add it there rather than letting the call go uncompared"
            )
        enabled.add(SINGLETON_CALL_FUNCTIONS[suffix])

    expected = expected_kms_enabled_functions(known_functions)
    if frozenset(enabled) != expected:
        missing = sorted(expected - enabled)
        surplus = sorted(enabled - expected)
        raise ValueError(
            f"{main_tf}: the set of functions with dynamodb_kms_grant_enabled = true is not the set "
            f"that must have it. Missing (reaches an SSE-KMS table but the call site does not enable "
            f"the grant, so its first read gets AccessDenied on the CMK): {missing or 'none'}. "
            f"Surplus (granted kms:Decrypt without being a known function): {surplus or 'none'}"
        )
    return frozenset(enabled)


def load_cursor_enabled_functions(main_tf: Path, known_functions: frozenset[str]) -> frozenset[str]:
    """Functions whose module call sets `cursor_kms_grant_enabled`, as an exact set.

    The `for_each` call must write `contains(local.cursor_tools, each.key)`, whose list is read from
    the root locals; a singleton call must write `true`. Any other expression raises `ValueError`,
    as does a listed tool that is not a known function.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(main_tf.parent.glob("*.tf"))
    )
    enabled: set[str] = set()
    for call in _LAMBDA_ACTION_CALL_RE.finditer(text):
        body = call.group("body")
        flag = re.search(r"^\s*cursor_kms_grant_enabled\s*=\s*(.+?)\s*$", body, re.MULTILINE)
        if flag is None:
            continue
        expression = flag.group(1)
        if re.search(r"^\s*for_each\s*=", body, re.MULTILINE):
            if expression != "contains(local.cursor_tools, each.key)":
                raise ValueError(
                    f"{main_tf}: the for_each call sets cursor_kms_grant_enabled = {expression!r}; "
                    "this gate reads only `contains(local.cursor_tools, each.key)`"
                )
            listed = re.search(r"^\s*cursor_tools\s*=\s*\[([^\]]*)\]", text, re.MULTILINE)
            if listed is None:
                raise ValueError(f"{main_tf}: no `cursor_tools = [...]` local")
            tools = set(re.findall(r'"([a-z_]+)"', listed.group(1)))
            unknown = sorted(tools - known_functions)
            if not tools or unknown:
                raise ValueError(
                    f"{main_tf}: local.cursor_tools is empty or names unknown functions: {unknown}"
                )
            enabled |= tools
            continue
        suffix = call.group("name").removeprefix("lambda_action_")
        if expression != "true" or suffix not in SINGLETON_CALL_FUNCTIONS:
            raise ValueError(
                f'{main_tf}: `module "{call.group("name")}"` sets cursor_kms_grant_enabled = '
                f"{expression!r}; a singleton must write `true` and be in SINGLETON_CALL_FUNCTIONS"
            )
        enabled.add(SINGLETON_CALL_FUNCTIONS[suffix])
    return frozenset(enabled)


def _split_cursor_statement(body: str, module_tf: Path) -> tuple[str, str]:
    """`(body without the cursor statement, the cursor statement)` of the `dynamodb_kms` document."""
    sid = body.find(f'"{CURSOR_GRANT_SID}"')
    if sid < 0:
        raise ValueError(
            f"{module_tf}: data.aws_iam_policy_document.dynamodb_kms has no {CURSOR_GRANT_SID} "
            "statement, so the cursor-key grant cannot be compared"
        )
    start = body.rfind('dynamic "statement"', 0, sid)
    if start < 0:
        raise ValueError(f"{module_tf}: the {CURSOR_GRANT_SID} statement is not a dynamic block")
    depth = 0
    for index in range(body.index("{", start), len(body)):
        if body[index] == "{":
            depth += 1
        elif body[index] == "}":
            depth -= 1
            if depth == 0:
                return body[:start] + body[index + 1 :], body[start : index + 1]
    raise ValueError(f"{module_tf}: unbalanced {CURSOR_GRANT_SID} statement")


def load_module_resource_grants(
    module_tf: Path,
    functions: frozenset[str],
    kms_enabled: frozenset[str],
    cursor_enabled: frozenset[str] = frozenset(),
) -> GrantMap:
    """Granted non-DynamoDB actions, read from the `lambda_action` module's policy documents.

    The module declares one `logs` policy for every instance and one `dynamodb_kms` policy gated by
    `dynamodb_kms_grant_enabled`, so the granted set is the module's action list projected across the
    instances that receive it. The `dynamodb_kms` document's cursor statement is a separate resource
    key, granted to the `cursor_enabled` functions only.
    """
    text = module_tf.read_text(encoding="utf-8")
    per_document: dict[str, set[str]] = {}
    cursor_actions: set[str] = set()
    for document in _MODULE_ACTIONS_RE.finditer(text):
        body = document.group("body")
        if document.group("name") == "dynamodb_kms":
            body, cursor_statement = _split_cursor_statement(body, module_tf)
            listed = re.search(r"\bactions\s*=\s*\[([^\]]*)\]", cursor_statement)
            cursor_actions = set(_QUOTED_ACTION_RE.findall(listed.group(1) if listed else ""))
            if not cursor_actions:
                raise ValueError(f"{module_tf}: parsed no actions from {CURSOR_GRANT_SID}")
        per_document[document.group("name")] = set(_QUOTED_ACTION_RE.findall(body))
    for required in ("logs", "dynamodb_kms"):
        if not per_document.get(required):
            raise ValueError(
                f"{module_tf}: parsed no actions from data.aws_iam_policy_document.{required} -- the "
                "parser is not seeing the real policy and the comparison would pass vacuously"
            )

    granted: GrantMap = {}
    for function in sorted(functions):
        granted[(function, LOGS_RESOURCE_KEY)] = set(per_document["logs"])
        if function in kms_enabled:
            granted[(function, KMS_RESOURCE_KEY)] = set(per_document["dynamodb_kms"])
        if function in cursor_enabled:
            granted[(function, CURSOR_KMS_RESOURCE_KEY)] = set(cursor_actions)
    return granted


def compare(
    derived: GrantMap, declared: GrantMap, source: str, *, functions: frozenset[str] | None = None
) -> list[str]:
    """Return every coverage finding between handler-derived and declared grants.

    Args:
        derived: Handler-derived implied grants.
        declared: The declared grant map to compare against.
        source: Name of the declared source, for the finding text.
        functions: Restrict the comparison to these functions; `None` compares all of them.
    """
    findings: list[str] = []
    surplus_allowed = {(s.function, s.table, s.action): s for s in DECLARED_SURPLUS_GRANTS}
    unreachable = {(u.function, u.table, u.action) for u in DECLARED_UNREACHABLE_GRANTS}
    if functions is not None:
        derived = {k: v for k, v in derived.items() if k[0] in functions}
        declared = {k: v for k, v in declared.items() if k[0] in functions}

    for (function, table), actions in sorted(derived.items()):
        granted = declared.get((function, table), set())
        for action in sorted(actions - granted):
            if (function, table, action) in unreachable:
                continue
            findings.append(
                f"MISSING GRANT [{source}] {function} reaches {action} on {table} but the "
                f"declared grant map does not permit it"
            )
        for action in sorted(actions & granted):
            if (function, table, action) in unreachable:
                findings.append(
                    f"UNREACHABLE GRANTED [{source}] {function} is granted {action} on {table}, "
                    "which DECLARED_UNREACHABLE_GRANTS says no request can reach; remove the grant "
                    "or the declaration"
                )

    reachable_functions = {function for function, _table in derived}
    for (function, table), actions in sorted(declared.items()):
        if function not in reachable_functions:
            findings.append(
                f"UNKNOWN FUNCTION [{source}] {function} holds grants but no entry point was "
                f"analysed for it"
            )
            continue
        implied = derived.get((function, table), set())
        for action in sorted(actions - implied):
            if (function, table, action) in surplus_allowed:
                continue
            # Per-action resource surplus applies only to `@`-namespaced keys.
            if table.startswith("@") and action in DECLARED_SURPLUS_RESOURCE_ACTIONS:
                continue
            findings.append(
                f"SURPLUS GRANT [{source}] {function} is granted {action} on {table} but no "
                f"store call reaches it; remove it or declare it in DECLARED_SURPLUS_GRANTS"
            )
    return findings


def check_surplus_declarations(declared: GrantMap) -> list[str]:
    """Fail a `DECLARED_SURPLUS_GRANTS` entry that no longer corresponds to a real grant."""
    findings: list[str] = []
    for surplus in DECLARED_SURPLUS_GRANTS:
        actions = declared.get((surplus.function, surplus.table), set())
        if surplus.action not in actions:
            findings.append(
                f"STALE SURPLUS DECLARATION {surplus.function}/{surplus.table}/{surplus.action} "
                f"is declared surplus but is not granted anywhere; delete the declaration"
            )
    return findings


def compare_sources(terraform: GrantMap, matrix: GrantMap) -> list[str]:
    """Report any disagreement between the two declared sources, offline."""
    findings: list[str] = []
    for key in sorted(set(terraform) | set(matrix)):
        if key[0] in NON_TOOL_FUNCTIONS:
            continue
        left = terraform.get(key, set())
        right = matrix.get(key, set())
        if left != right:
            findings.append(
                f"SOURCE DISAGREEMENT {key[0]}/{key[1]}: main.tf {sorted(left)} != "
                f"table_action_role_matrix.json {sorted(right)}"
            )
    return findings


def check_function_key_namespace(
    matrix: GrantMap, terraform: GrantMap, *, zero_grant_functions: frozenset[str]
) -> list[str]:
    """Report any hardcoded function key that no longer corresponds to a matrix function.

    Every function-keyed literal here must name a `table_action_role_matrix.json` function (a grant
    row's, or one of `zero_grant_functions`), and every matrix function that is not a `main.tf` tool
    must be in `NON_TOOL_FUNCTIONS`. Every zero-grant function must be in `EXTRA_ENTRY_POINTS`, so
    its zero is derived from source. An empty parse of either source is itself a finding.
    """
    findings: list[str] = []
    granted_functions = {function for function, _table in matrix}
    terraform_functions = {function for function, _table in terraform}
    if not granted_functions or not terraform_functions:
        return [
            "FUNCTION KEY NAMESPACE: parsed "
            f"{len(granted_functions)} matrix function(s) and {len(terraform_functions)} terraform "
            "function(s); an empty parse makes every check below vacuously true, so it is reported "
            "as a failure rather than a pass"
        ]
    matrix_functions = granted_functions | zero_grant_functions
    for function in sorted(zero_grant_functions - set(EXTRA_ENTRY_POINTS)):
        findings.append(
            f"FUNCTION KEY NAMESPACE: the matrix declares {function!r} in "
            "functions_without_dynamodb_grants, but EXTRA_ENTRY_POINTS does not name it, so no "
            "handler is analysed and its zero grant set holds by authorship only."
        )

    # Literal name -> the function names it is keyed on.
    declared: dict[str, set[str]] = {
        "NON_TOOL_FUNCTIONS": set(NON_TOOL_FUNCTIONS),
        "EXTRA_ENTRY_POINTS": set(EXTRA_ENTRY_POINTS),
        "_ADDITIONAL_ROOTS": set(_ADDITIONAL_ROOTS),
        "_EXEMPTION_REACHED_BY": {
            function for functions in _EXEMPTION_REACHED_BY.values() for function in functions
        },
        "DECLARED_SURPLUS_GRANTS": {grant.function for grant in DECLARED_SURPLUS_GRANTS},
    }
    for literal, names in sorted(declared.items()):
        for unknown in sorted(names - matrix_functions):
            close = sorted(
                candidate
                for candidate in matrix_functions
                if candidate.replace("-", "_") == unknown.replace("-", "_")
            )
            hint = f" Did you mean {close[0]!r}?" if close else ""
            findings.append(
                f"FUNCTION KEY NAMESPACE: {literal} names {unknown!r}, which is not a function in "
                f"table_action_role_matrix.json.{hint} The matrix key is the physical Lambda function "
                "name minus the prefix; a Python module path is a different namespace and is not "
                "renamed to match."
            )

    unregistered = sorted(granted_functions - terraform_functions - set(NON_TOOL_FUNCTIONS))
    for function in unregistered:
        findings.append(
            f"FUNCTION KEY NAMESPACE: the matrix declares {function!r}, which is neither a tool in "
            "local.tool_dynamodb_grants nor listed in NON_TOOL_FUNCTIONS. It would be compared "
            "against the tool-only main.tf map and report a phantom disagreement."
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--main-tf", type=Path, default=DEFAULT_MAIN_TF)
    parser.add_argument("--module-tf", type=Path, default=DEFAULT_MODULE_TF)
    args = parser.parse_args(argv)

    try:
        analysis = derive_grants()
        matrix = load_matrix_grants(args.matrix)
        zero_grant_functions = load_zero_grant_functions(args.matrix)
        terraform = load_terraform_grants(args.main_tf)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    findings: list[str] = list(analysis.stale_exemptions)
    for key in sorted(analysis.unresolved):
        findings.append(
            f"UNRESOLVED {key}: declare its table domain in DECLARED_TABLE_DOMAINS "
            f"(the gate refuses to guess)"
        )
    tool_functions = frozenset({function for function, _table in matrix} - NON_TOOL_FUNCTIONS)
    findings.extend(
        check_function_key_namespace(matrix, terraform, zero_grant_functions=zero_grant_functions)
    )
    findings.extend(compare_sources(terraform, matrix))
    findings.extend(compare(analysis.derived, terraform, "main.tf", functions=tool_functions))
    findings.extend(compare(analysis.derived, matrix, "matrix.json"))
    findings.extend(check_surplus_declarations(matrix))

    # Non-DynamoDB grants: KMS and log-group actions, compared per `@`-namespaced resource key.
    try:
        kms_enabled = load_kms_enabled_functions(args.main_tf, frozenset({f for f, _t in matrix}))
        cursor_enabled = load_cursor_enabled_functions(
            args.main_tf, frozenset({f for f, _t in matrix})
        )
        module_granted = load_module_resource_grants(
            args.module_tf,
            frozenset({f for f, _t in analysis.derived}),
            kms_enabled,
            cursor_enabled,
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    resource_implied = derive_resource_grants(analysis.derived, kms_enabled, analysis.reached)
    findings.extend(compare(resource_implied, module_granted, "lambda_action module"))

    if findings:
        for finding in findings:
            print(finding)
        print(f"\nFAILED: {len(findings)} handler/grant coverage finding(s)")
        return 1

    print(
        f"OK: {len(analysis.derived)} (function, table) grant pairs derived from "
        f"{len(analysis.visited_sites)} store call sites; both declared sources cover them exactly. "
        f"Plus {len(resource_implied)} (function, resource) pairs over the roles' non-DynamoDB "
        f"policies -- kms:Decrypt on the DynamoDB CMK, the cursor CMK's Encrypt/Decrypt, and the "
        "own-log-group writes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
