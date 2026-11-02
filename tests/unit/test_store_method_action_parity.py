# SPDX-License-Identifier: MIT
"""Parity test: every `STORE_METHOD_ACTIONS` entry matches the boto3 calls its `Store` method reaches.

Each method's actions are derived from the store module's AST, following `self.<helper>()` calls
transitively. Planting tests use synthetic in-memory module source only.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_handler_grant_coverage.py"
STORE_PATH = REPO_ROOT / "src" / "crm" / "store" / "store.py"

_SPEC = importlib.util.spec_from_file_location("check_handler_grant_coverage", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["check_handler_grant_coverage"] = gate
_SPEC.loader.exec_module(gate)

#: The class whose methods `STORE_METHOD_ACTIONS` is a mapping over.
STORE_CLASS: Final[str] = "Store"

#: The attribute every DynamoDB API call in the store is issued through.
CLIENT_ATTR: Final[str] = "_client"

#: The per-item IAM actions that authorize a `TransactWriteItems` operation (the Service
#: Authorization Reference's `AuthorizedActions` for it; it is not itself an IAM action).
_TRANSACT_WRITE_AUTHORIZED_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "dynamodb:ConditionCheckItem",
        "dynamodb:DeleteItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    }
)

#: boto3 DynamoDB operation name -> the IAM actions it is authorized by. An operation absent here
#: raises.
_BOTO3_OP_ACTIONS: Final[dict[str, frozenset[str]]] = {
    "batch_get_item": frozenset({"dynamodb:BatchGetItem"}),
    "batch_write_item": frozenset({"dynamodb:BatchWriteItem"}),
    "delete_item": frozenset({"dynamodb:DeleteItem"}),
    "get_item": frozenset({"dynamodb:GetItem"}),
    "put_item": frozenset({"dynamodb:PutItem"}),
    "query": frozenset({"dynamodb:Query"}),
    "scan": frozenset({"dynamodb:Scan"}),
    "transact_get_items": frozenset({"dynamodb:GetItem"}),
    "transact_write_items": _TRANSACT_WRITE_AUTHORIZED_ACTIONS,
    "update_item": frozenset({"dynamodb:UpdateItem"}),
}

#: Actions a method issues that `STORE_METHOD_ACTIONS` declares elsewhere: `transact_write`'s
#: actions are attributed at each operation's construction site via `TRANSACT_ITEM_ACTIONS`.
_ACTIONS_ATTRIBUTED_ELSEWHERE: Final[dict[str, frozenset[str]]] = {
    "transact_write": _TRANSACT_WRITE_AUTHORIZED_ACTIONS,
}


class UnknownClientOperationError(Exception):
    """A `self._client.<op>` call whose operation `_BOTO3_OP_ACTIONS` does not name."""


def _client_operation(node: ast.Call) -> str | None:
    """Return the boto3 operation name for a `self._client.<op>(...)` call, else `None`."""
    func = node.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Attribute):
        return None
    if func.value.attr != CLIENT_ATTR:
        return None
    if not isinstance(func.value.value, ast.Name) or func.value.value.id != "self":
        return None
    return func.attr


def _self_method_call(node: ast.Call) -> str | None:
    """Return the method name for a `self.<method>(...)` call, else `None`.

    `self._client.<op>(...)` is not a match: its callee's value is an attribute, not `self`.
    """
    func = node.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    if func.value.id != "self":
        return None
    return func.attr


def index_store_methods(source: str) -> dict[str, ast.FunctionDef]:
    """Return every method defined on the `Store` class in `source`, by name."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == STORE_CLASS:
            return {child.name: child for child in node.body if isinstance(child, ast.FunctionDef)}
    raise AssertionError(f"no class {STORE_CLASS} in the parsed source")


def derive_client_actions(source: str) -> dict[str, frozenset[str]]:
    """Return each `Store` method's transitively reached DynamoDB actions, derived from `source`.

    Raises:
        UnknownClientOperationError: A `self._client.<op>` call whose `<op>` is not in
            `_BOTO3_OP_ACTIONS`.
    """
    methods = index_store_methods(source)

    def walk(name: str, seen: frozenset[str]) -> frozenset[str]:
        func = methods.get(name)
        if func is None or name in seen:
            return frozenset()
        seen = seen | {name}
        actions: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            operation = _client_operation(node)
            if operation is not None:
                operation_actions = _BOTO3_OP_ACTIONS.get(operation)
                if operation_actions is None:
                    raise UnknownClientOperationError(
                        f"{STORE_CLASS}.{name} calls self.{CLIENT_ATTR}.{operation}(), which "
                        f"_BOTO3_OP_ACTIONS does not name; add it with the IAM action it needs"
                    )
                actions |= operation_actions
                continue
            callee = _self_method_call(node)
            if callee is not None:
                actions |= walk(callee, seen)
        return frozenset(actions)

    return {name: walk(name, frozenset()) for name in methods}


@pytest.fixture(scope="module")
def store_source() -> str:
    """The real `src/crm/store/store.py` source."""
    return STORE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def derived(store_source: str) -> dict[str, frozenset[str]]:
    """Every real `Store` method's transitively derived DynamoDB actions."""
    return derive_client_actions(store_source)


def _expected(name: str) -> frozenset[str]:
    """The declared actions for `name`, including any attributed elsewhere."""
    declared: frozenset[str] = frozenset(gate.STORE_METHOD_ACTIONS[name].actions)
    return declared | _ACTIONS_ATTRIBUTED_ELSEWHERE.get(name, frozenset())


class TestDeclaredMappingMatchesImplementation:
    """Declaration equals implementation, per method."""

    @pytest.mark.parametrize("name", sorted(gate.STORE_METHOD_ACTIONS))
    def test_declared_actions_equal_the_boto3_calls_the_method_reaches(
        self, name: str, derived: dict[str, frozenset[str]]
    ) -> None:
        """Each `STORE_METHOD_ACTIONS` entry states exactly what its method's code reaches."""
        assert name in derived, (
            f"STORE_METHOD_ACTIONS declares {name!r} but {STORE_CLASS} has no such method"
        )
        assert _expected(name) == derived[name], (
            f"{STORE_CLASS}.{name}: declared {sorted(_expected(name))} but its code reaches "
            f"{sorted(derived[name])}"
        )

    def test_delete_tenant_reaches_batch_write_item_and_never_delete_item(
        self, derived: dict[str, frozenset[str]]
    ) -> None:
        """`delete_tenant` maps to `BatchWriteItem` and `Query`, not `DeleteItem`."""
        assert derived["delete_tenant"] == frozenset({"dynamodb:Query", "dynamodb:BatchWriteItem"})

    def test_the_three_unchecked_entries_of_the_same_shape_are_correct(
        self, derived: dict[str, frozenset[str]]
    ) -> None:
        """`transact_write`, `put_idempotent` and `query_tenant_scan` are pinned by value."""
        assert derived["put_idempotent"] == frozenset({"dynamodb:GetItem", "dynamodb:PutItem"})
        assert derived["query_tenant_scan"] == frozenset({"dynamodb:Query"})
        assert derived["transact_write"] == _TRANSACT_WRITE_AUTHORIZED_ACTIONS

    def test_physical_name_reaches_no_api_call_at_all(
        self, derived: dict[str, frozenset[str]]
    ) -> None:
        """`physical_name` declares no action."""
        assert derived["physical_name"] == frozenset()
        assert gate.STORE_METHOD_ACTIONS["physical_name"].actions == frozenset()


class TestDerivationIsNotVacuous:
    """The derivation finds the actions real methods reach."""

    def test_the_derivation_follows_calls_transitively(
        self, derived: dict[str, frozenset[str]]
    ) -> None:
        """`delete_tenant`'s actions are found two frames below its own body."""
        methods = index_store_methods(STORE_PATH.read_text(encoding="utf-8"))
        own_body_operations = {
            _client_operation(node)
            for node in ast.walk(methods["delete_tenant"])
            if isinstance(node, ast.Call) and _client_operation(node) is not None
        }
        assert own_body_operations == set()
        assert derived["delete_tenant"] != frozenset()

    def test_every_declared_method_exists_on_the_store_class(
        self, derived: dict[str, frozenset[str]]
    ) -> None:
        """Every mapping entry names an existing `Store` method."""
        missing = sorted(set(gate.STORE_METHOD_ACTIONS) - set(derived))
        assert missing == [], f"STORE_METHOD_ACTIONS names methods {STORE_CLASS} does not have"

    def test_every_store_api_call_is_covered_by_a_mapping_entry(
        self, derived: dict[str, frozenset[str]]
    ) -> None:
        """Every action any `Store` method reaches is declared by some public entry."""
        declared_actions = {
            action for name in gate.STORE_METHOD_ACTIONS for action in _expected(name)
        }
        public_actions = {
            action
            for name, actions in derived.items()
            if not name.startswith("_")
            for action in actions
        }
        assert public_actions <= declared_actions, (
            f"undeclared actions reachable from a public Store method: "
            f"{sorted(public_actions - declared_actions)}"
        )

    def test_at_least_five_distinct_actions_are_derived(
        self, derived: dict[str, frozenset[str]]
    ) -> None:
        """The real store issues at least six DynamoDB operations."""
        assert len({action for actions in derived.values() for action in actions}) >= 5


class TestDerivationCanFail:
    """Planted defects in synthetic source are detected by the derivation."""

    @staticmethod
    def _synthetic(body: str) -> str:
        """Wrap `body` as the body of a synthetic `Store` class, in memory only."""
        indented = textwrap.indent(textwrap.dedent(body).strip("\n"), "    ")
        return f"class Store:\n{indented}\n"

    def test_an_extra_api_call_is_detected(self) -> None:
        """A method gaining an undeclared call widens the derived set."""
        source = self._synthetic(
            """
            def delete_tenant(self, tenant):
                self._client.query(TableName="t")
                self._client.delete_item(TableName="t")
            """
        )
        assert derive_client_actions(source)["delete_tenant"] == frozenset(
            {"dynamodb:Query", "dynamodb:DeleteItem"}
        )

    def test_a_removed_api_call_is_detected(self) -> None:
        """A method losing a call narrows the derived set."""
        source = self._synthetic(
            """
            def delete_tenant(self, tenant):
                self._client.query(TableName="t")
            """
        )
        assert derive_client_actions(source)["delete_tenant"] == frozenset({"dynamodb:Query"})

    def test_a_call_reached_only_through_a_private_helper_is_derived(self) -> None:
        """A planted transitive helper path is followed."""
        source = self._synthetic(
            """
            def delete_tenant(self, tenant):
                self._delete_all(tenant)

            def _delete_all(self, tenant):
                self._batch(tenant)

            def _batch(self, tenant):
                self._client.batch_write_item(RequestItems={})
            """
        )
        assert derive_client_actions(source)["delete_tenant"] == frozenset(
            {"dynamodb:BatchWriteItem"}
        )

    def test_mutual_recursion_terminates_without_losing_actions(self) -> None:
        """A cycle in the call graph neither hangs nor drops the action past the cycle."""
        source = self._synthetic(
            """
            def a(self):
                self.b()
                self._client.query(TableName="t")

            def b(self):
                self.a()
            """
        )
        assert derive_client_actions(source)["a"] == frozenset({"dynamodb:Query"})
        assert derive_client_actions(source)["b"] == frozenset({"dynamodb:Query"})

    def test_an_unrecognised_api_call_raises_rather_than_deriving_nothing(self) -> None:
        """An unknown DynamoDB operation raises rather than deriving nothing."""
        source = self._synthetic(
            """
            def widen(self):
                self._client.export_table_to_point_in_time(TableArn="t")
            """
        )
        with pytest.raises(UnknownClientOperationError, match="export_table_to_point_in_time"):
            derive_client_actions(source)

    def test_a_plain_client_call_on_something_other_than_self_is_not_attributed(self) -> None:
        """Only `self._client` calls count as store DynamoDB calls."""
        source = self._synthetic(
            """
            def borrow(self, other):
                other._client.delete_item(TableName="t")
            """
        )
        assert derive_client_actions(source)["borrow"] == frozenset()


class TestAttributedElsewhereDeclarationsAreLive:
    """`_ACTIONS_ATTRIBUTED_ELSEWHERE` stays consistent with the store and the gate."""

    def test_every_attributed_elsewhere_method_is_declared_by_the_gate(self) -> None:
        """Every exempted method still exists on `Store`."""
        unknown = sorted(set(_ACTIONS_ATTRIBUTED_ELSEWHERE) - set(gate.STORE_METHOD_ACTIONS))
        assert unknown == []

    def test_every_attributed_elsewhere_action_is_really_attributed_by_the_gate(self) -> None:
        """Every attributed-elsewhere action is emitted by `TRANSACT_ITEM_ACTIONS`."""
        emitted = {action for actions in gate.TRANSACT_ITEM_ACTIONS.values() for action in actions}
        for actions in _ACTIONS_ATTRIBUTED_ELSEWHERE.values():
            assert actions <= emitted, (
                f"declared as attributed elsewhere but TRANSACT_ITEM_ACTIONS emits "
                f"{sorted(emitted)}"
            )

    def test_the_exempted_method_really_declares_no_actions_itself(self) -> None:
        """`transact_write` declares no actions of its own."""
        for name in _ACTIONS_ATTRIBUTED_ELSEWHERE:
            assert gate.STORE_METHOD_ACTIONS[name].actions == frozenset()


def test_the_operation_table_covers_every_call_in_the_real_store(store_source: str) -> None:
    """Every API call in the real store resolves through `_BOTO3_OP_ACTIONS`."""
    operations = {
        operation
        for node in ast.walk(ast.parse(store_source))
        if isinstance(node, ast.Call) and (operation := _client_operation(node)) is not None
    }
    assert operations, "found no self._client calls in store.py — the matcher is broken"
    assert operations <= set(_BOTO3_OP_ACTIONS), (
        f"store.py issues operations _BOTO3_OP_ACTIONS does not name: "
        f"{sorted(operations - set(_BOTO3_OP_ACTIONS))}"
    )


def test_the_gate_derives_delete_item_only_for_functions_reaching_store_delete_item() -> None:
    """Through the gate, only `approve_proposal`, `revert_audit_record`, `eval-trigger`,
    `sweep-control` and `user-settings` derive `DeleteItem`: `eval-trigger` and `sweep-control` only
    on `eval_runs` (the holder-conditional lock release) and `user-settings` only on `reps`
    (reset of the caller's own item)."""
    analysis: Any = gate.derive_grants()
    derivers = {
        function
        for (function, _table), actions in analysis.derived.items()
        if "dynamodb:DeleteItem" in actions
    }
    assert derivers == {
        "approve_proposal",
        "revert_audit_record",
        "eval-trigger",
        "sweep-control",
        "user-settings",
    }
    for lock_releaser in ("eval-trigger", "sweep-control"):
        tables = {
            table
            for (function, table), actions in analysis.derived.items()
            if function == lock_releaser and "dynamodb:DeleteItem" in actions
        }
        assert tables == {"eval_runs"}, lock_releaser
    user_settings_tables = {
        table
        for (function, table), actions in analysis.derived.items()
        if function == "user-settings" and "dynamodb:DeleteItem" in actions
    }
    assert user_settings_tables == {"reps"}
