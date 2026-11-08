# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/check_handler_grant_coverage.py`.

Covers transitive grant derivation against the real source, the gate failing on reconstructed
missing grants, and fail-closed handling of unresolved call sites and unparsable Terraform.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_handler_grant_coverage.py"

_SPEC = importlib.util.spec_from_file_location("check_handler_grant_coverage", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["check_handler_grant_coverage"] = gate
_SPEC.loader.exec_module(gate)

MAIN_TF = REPO_ROOT / "infra" / "main.tf"
MATRIX = REPO_ROOT / "infra" / "modules" / "lambda_action" / "table_action_role_matrix.json"


@pytest.fixture(scope="module")
def analysis() -> Any:
    """Return the real repository's handler-derived grants."""
    return gate.derive_grants()


@pytest.fixture(scope="module")
def matrix_grants() -> Any:
    """Return the declared grant map from `table_action_role_matrix.json`."""
    return gate.load_matrix_grants(MATRIX)


def test_every_action_lambda_is_discovered(analysis: Any) -> None:
    """The action Lambdas and the non-tool Lambdas are all analysed."""
    functions = {function for function, _table in analysis.derived}
    assert "submit_proposal" in functions
    assert "identity-interceptor" in functions
    assert "pre-token-generation" in functions
    assert len(functions) >= 22


def test_put_idempotent_implies_getitem_and_putitem(analysis: Any) -> None:
    """A write through `put_idempotent` implies `GetItem` and `PutItem`."""
    derived = analysis.derived
    assert derived[("submit_proposal", "proposals")] >= {"dynamodb:GetItem", "dynamodb:PutItem"}


def test_cascade_rules_query_is_attributed_to_submit_proposal(analysis: Any) -> None:
    """`cascade_rules`'s `query_gsi` on `opportunities` is attributed to `submit_proposal`."""
    derived = analysis.derived
    assert "dynamodb:Query" in derived[("submit_proposal", "opportunities")]


def test_reject_proposal_reads_every_target_table(analysis: Any) -> None:
    """`reject_proposal` derives `GetItem` on every proposal target table."""
    derived = analysis.derived
    for table in ("accounts", "contacts", "opportunities", "activities"):
        assert derived[("reject_proposal", table)] == {"dynamodb:GetItem"}


def test_transact_write_items_is_attributed_at_the_operation(analysis: Any) -> None:
    """A transact-item `Put` implies `PutItem` on its own table and no `TransactWriteItems`."""
    derived = analysis.derived
    assert derived[("update_account", "audit_records")] == {"dynamodb:PutItem"}


def test_no_transact_item_implies_a_non_iam_action() -> None:
    assert set().union(*gate.TRANSACT_ITEM_ACTIONS.values()) == {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:ConditionCheckItem",
    }


def test_no_unresolved_table_expression_and_no_unreached_call_site(analysis: Any) -> None:
    """Every table expression is resolved or declared and every store call site is reached."""
    assert analysis.unresolved == set()


def test_removing_a_declared_domain_makes_the_gate_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable table expression with no declaration is reported as unresolved."""
    monkeypatch.setattr(gate, "DECLARED_TABLE_DOMAINS", {})
    result = gate.derive_grants()
    assert result.unresolved, "clearing the declared domains must produce unresolved expressions"


def _without(
    grants: dict[tuple[str, str], set[str]], function: str, table: str, action: str | None = None
) -> dict[tuple[str, str], set[str]]:
    """Return a copy of `grants` with one grant, or one action of it, removed."""
    copy = {key: set(value) for key, value in grants.items()}
    if action is None:
        copy.pop((function, table), None)
    else:
        copy[(function, table)].discard(action)
    return copy


def test_gate_fails_against_d112_finding_1_missing_getitem_on_proposals(
    analysis: Any, matrix_grants: Any
) -> None:
    """A `submit_proposal` grant with `PutItem` but no `GetItem` is reported MISSING."""
    broken = _without(matrix_grants, "submit_proposal", "proposals", "dynamodb:GetItem")
    findings = gate.compare(analysis.derived, broken, "fixture")
    assert any(
        "MISSING GRANT" in f and "submit_proposal" in f and "dynamodb:GetItem on proposals" in f
        for f in findings
    ), findings


def test_gate_fails_against_d112_finding_2_missing_target_reads_on_reject(
    analysis: Any, matrix_grants: Any
) -> None:
    """`reject_proposal` holding no grant on its target tables is reported MISSING."""
    broken = matrix_grants
    for table in ("accounts", "contacts", "opportunities", "activities"):
        broken = _without(broken, "reject_proposal", table)
    findings = gate.compare(analysis.derived, broken, "fixture")
    missing = {
        f.split("dynamodb:GetItem on ")[1].split(" ")[0]
        for f in findings
        if "MISSING GRANT" in f and "reject_proposal" in f
    }
    assert missing == {"accounts", "contacts", "opportunities", "activities"}, findings


def test_gate_fails_against_the_delete_proposal_query_gap(
    analysis: Any, matrix_grants: Any
) -> None:
    """A missing `Query` on `opportunities` for `submit_proposal` is reported MISSING."""
    broken = _without(matrix_grants, "submit_proposal", "opportunities", "dynamodb:Query")
    findings = gate.compare(analysis.derived, broken, "fixture")
    assert any(
        "MISSING GRANT" in f and "dynamodb:Query on opportunities" in f and "submit_proposal" in f
        for f in findings
    ), findings


def test_an_unreachable_grant_is_reported_as_surplus(analysis: Any, matrix_grants: Any) -> None:
    """A grant no source path reaches is reported SURPLUS."""
    widened = {key: set(value) for key, value in matrix_grants.items()}
    widened[("get_contact", "accounts")] = {"dynamodb:DeleteItem"}
    findings = gate.compare(analysis.derived, widened, "fixture")
    assert any(
        "SURPLUS GRANT" in f and "get_contact" in f and "dynamodb:DeleteItem" in f for f in findings
    ), findings


def test_every_declared_surplus_grant_corresponds_to_a_real_grant(
    matrix_grants: Any,
) -> None:
    """Every declared surplus grant matches a real grant and carries a reason."""
    assert gate.check_surplus_declarations(matrix_grants) == []
    assert gate.DECLARED_SURPLUS_GRANTS, "the surplus exception list must be an enumeration"
    for surplus in gate.DECLARED_SURPLUS_GRANTS:
        assert surplus.reason.strip(), f"{surplus} carries no reason"


def test_terraform_parser_refuses_to_parse_too_little(tmp_path: Path) -> None:
    """The Terraform grant parser refuses a block with too few tools."""
    truncated = tmp_path / "main.tf"
    truncated.write_text(
        "  tool_dynamodb_grants = {\n"
        "    get_contact = [\n"
        '      { table = "contacts", actions = ["dynamodb:GetItem"] },\n'
        "    ]\n"
        "  }\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="parsed only 1 tools"):
        gate.load_terraform_grants(truncated)


def test_terraform_parser_refuses_a_file_without_the_grant_block(tmp_path: Path) -> None:
    """The Terraform grant parser refuses a file without the grant block."""
    empty = tmp_path / "empty.tf"
    empty.write_text("locals {\n  something_else = 1\n}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="could not locate"):
        gate.load_terraform_grants(empty)


def test_terraform_and_matrix_agree_on_every_tool_grant(
    matrix_grants: Any,
) -> None:
    """`main.tf` and the role matrix declare the same tool grants."""
    terraform = gate.load_terraform_grants(MAIN_TF)
    assert gate.compare_sources(terraform, matrix_grants) == []


def test_store_method_action_mapping_is_declared_in_one_place() -> None:
    """Every recognised Store method declares its implied actions in `STORE_METHOD_ACTIONS`."""
    for method in ("get_item", "put_item", "put_idempotent", "query_gsi", "query_tenant_scan"):
        assert method in gate.STORE_METHOD_ACTIONS
    assert gate.STORE_METHOD_ACTIONS["put_idempotent"].actions == frozenset(
        {"dynamodb:GetItem", "dynamodb:PutItem"}
    )
    assert gate.STORE_METHOD_ACTIONS["query_tenant_scan"].actions == frozenset({"dynamodb:Query"})


# ---------------------------------------------------------------------------------------------
# An action-bearing store method must be attributable to a table.
# ---------------------------------------------------------------------------------------------


def test_every_action_bearing_store_method_names_exactly_one_target() -> None:
    """Each action-bearing Store method names its target by argument or by fan-out, not both."""
    for name, method in gate.STORE_METHOD_ACTIONS.items():
        if not method.actions:
            continue
        named = (method.table_arg is not None, method.tables is not None)
        assert sum(named) == 1, f"{name} must name its target by exactly one of the two mechanisms"


def test_delete_tenant_fans_out_over_every_tenant_scoped_table() -> None:
    """`delete_tenant` implies `Query` and `BatchWriteItem`, not `DeleteItem`, per tenant table."""
    from crm.store.schema import TENANT_SCOPED_TABLES

    method = gate.STORE_METHOD_ACTIONS["delete_tenant"]
    assert method.actions == frozenset({"dynamodb:Query", "dynamodb:BatchWriteItem"})
    assert "dynamodb:DeleteItem" not in method.actions
    assert method.tables == TENANT_SCOPED_TABLES


def test_a_store_method_may_not_name_its_target_twice() -> None:
    """A Store method naming both a table argument and a fan-out set is refused."""
    with pytest.raises(ValueError, match="never both"):
        gate.StoreMethod(frozenset({"dynamodb:Query"}), table_arg=0, tables=frozenset({"accounts"}))


def _synthetic_handler_tree(tmp_path: Path, body: str) -> Path:
    """Write a minimal `src` tree carrying one discoverable handler whose body is `body`."""
    src = tmp_path / "src"
    handlers = src / "crm" / "handlers"
    handlers.mkdir(parents=True)
    (src / "crm" / "__init__.py").write_text("", encoding="utf-8")
    (handlers / "__init__.py").write_text("", encoding="utf-8")
    (handlers / "probe.py").write_text(
        "def _probe(store, identity, arguments):\n"
        f"{body}\n"
        "    return {}\n"
        "\n"
        "handler = build_handler(_probe)\n",
        encoding="utf-8",
    )
    return src


def test_a_delete_tenant_reachable_from_a_handler_emits_its_declared_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reachable `delete_tenant` call requires its declared actions on every tenant table."""
    from crm.store.schema import TENANT_SCOPED_TABLES

    monkeypatch.setattr(gate, "EXTRA_ENTRY_POINTS", {})
    src = _synthetic_handler_tree(tmp_path, '    store.delete_tenant("probe")')

    analysis = gate.derive_grants(src)

    assert analysis.unresolved == set()
    for table in TENANT_SCOPED_TABLES:
        assert analysis.derived[("probe", table)] == {
            "dynamodb:Query",
            "dynamodb:BatchWriteItem",
        }, table
    findings = gate.compare(analysis.derived, {}, "fixture")
    assert any("MISSING GRANT" in f and "dynamodb:BatchWriteItem" in f for f in findings), findings


def test_an_action_bearing_method_with_no_attributable_table_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reached action-bearing call with no attributable table is reported unresolved."""
    monkeypatch.setattr(gate, "EXTRA_ENTRY_POINTS", {})
    monkeypatch.setitem(
        gate.STORE_METHOD_ACTIONS,
        "delete_tenant",
        gate.StoreMethod(frozenset({"dynamodb:Query", "dynamodb:DeleteItem"}), table_arg=None),
    )
    src = _synthetic_handler_tree(tmp_path, '    store.delete_tenant("probe")')

    analysis = gate.derive_grants(src)

    assert analysis.derived == {}, "an unattributable call must emit no grant"
    assert any(
        "names no table and declares no fan-out table set" in key for key in analysis.unresolved
    ), analysis.unresolved


# ---------------------------------------------------------------------------------------------
# Both exception lists are pinned.
# ---------------------------------------------------------------------------------------------


def test_declared_table_domains_holds_exactly_the_dynamic_target_tables() -> None:
    """`DECLARED_TABLE_DOMAINS` holds exactly the expected keys and domains, each with a reason."""
    from crm.store.schema import TENANT_SCOPED_TABLES

    entity_tables = frozenset({"accounts", "contacts", "opportunities", "activities"})
    tenant_scoped_keys = {
        "eval.tenancy:find_canary_records:table",
        "eval.tenant_guard:store_row_reader:table",
        "eval.tenant_guard:store_count_reader:table",
    }
    from crm.audit import _CHILD_INDEXES

    child_check_key = "crm.audit:_refuse_while_children_exist:child_table"
    golden_task_keys = {
        "eval.scoring:capture_pre_turn_values:table",
        "eval.scoring:_check_must_not_change:str(entry['table'])",
        "eval.scoring:_resolve_expected_field_change:str(change['table'])",
    }

    assert set(gate.DECLARED_TABLE_DOMAINS) == {
        "crm.handlers.submit_proposal:_submit_proposal:str(arguments['target_table'])",
        "crm.handlers.approve_proposal:_approve_proposal:str(proposal['target_table'])",
        "crm.handlers.reject_proposal:_reject_proposal:str(proposal['target_table'])",
        "crm.proposals:approve_proposal:str(proposal['target_table'])",
        "crm.handlers.rebase_proposal:_rebase_proposal:str(proposal['target_table'])",
        "crm.proposals:rebase_proposal:str(proposal['target_table'])",
        "crm.audit:revert:str(item['target_table'])",
        "crm.handlers.get_page_context:_resolve_entity:context.entity_type",
    } | tenant_scoped_keys | golden_task_keys | {child_check_key}
    for key, declared in gate.DECLARED_TABLE_DOMAINS.items():
        if key == child_check_key:
            expected = frozenset(table for pairs in _CHILD_INDEXES.values() for table, _ in pairs)
        elif key == "crm.audit:revert:str(item['target_table'])":
            # A note create is revertable too.
            expected = entity_tables | {"notes"}
        elif key in golden_task_keys:
            expected = gate.GOLDEN_TASK_ASSERTION_TABLES
        elif key in tenant_scoped_keys:
            expected = TENANT_SCOPED_TABLES
        else:
            expected = entity_tables
        assert declared.tables == expected, key
        assert declared.reason.strip(), key


_SWEEP_SEED_TABLES = frozenset({"accounts", "contacts", "opportunities", "activities", "notes"})

#: The sweep grants no store call reaches, removed from `infra/eval_sfn.tf` and the matrix.
_SWEEP_REMOVED_GRANTS = frozenset(
    {
        *(
            ("sweep-control", table, "dynamodb:GetItem")
            for table in (
                *_SWEEP_SEED_TABLES,
                "audit_records",
                "proposals",
                "usage_records",
                "reps",
            )
        ),
        *(("sweep-control", table, "dynamodb:PutItem") for table in _SWEEP_SEED_TABLES),
        ("sweep-control", "eval_results", "dynamodb:Query"),
        ("sweep-control", "eval_results", "dynamodb:BatchWriteItem"),
        ("sweep-control", "eval_runs", "dynamodb:Query"),
        ("sweep-task", "eval_results", "dynamodb:UpdateItem"),
        ("sweep-task", "audit_records", "dynamodb:GetItem"),
        ("sweep-task", "usage_records", "dynamodb:GetItem"),
        ("sweep-task", "audit_records", "dynamodb:Query"),
        ("sweep-task", "proposals", "dynamodb:Query"),
        ("sweep-task", "usage_records", "dynamodb:Query"),
        ("sweep-task", "reps", "dynamodb:Query"),
        ("sweep-task", "reps", "dynamodb:GetItem"),
    }
)


def test_declared_surplus_grants_holds_exactly_the_enumerated_rows() -> None:
    """`DECLARED_SURPLUS_GRANTS` holds exactly the enumerated rows, each with a reason."""
    update_item_rows = {
        ("update_account", "accounts"),
        ("update_contact", "contacts"),
        ("update_opportunity", "opportunities"),
        ("update_activity", "activities"),
        ("log_activity", "opportunities"),
        ("approve_proposal", "accounts"),
        ("approve_proposal", "contacts"),
        ("approve_proposal", "opportunities"),
        ("approve_proposal", "activities"),
        ("revert_audit_record", "accounts"),
        ("revert_audit_record", "contacts"),
        ("revert_audit_record", "opportunities"),
        ("revert_audit_record", "activities"),
    }
    get_item_rows = {
        ("revert_audit_record", "accounts"),
        ("revert_audit_record", "contacts"),
        ("revert_audit_record", "opportunities"),
        ("revert_audit_record", "activities"),
    }
    expected = {(f, t, "dynamodb:UpdateItem") for f, t in update_item_rows} | {
        (f, t, "dynamodb:GetItem") for f, t in get_item_rows
    }

    actual = {(s.function, s.table, s.action) for s in gate.DECLARED_SURPLUS_GRANTS}
    assert actual == expected
    assert len(gate.DECLARED_SURPLUS_GRANTS) == len(expected)
    for surplus in gate.DECLARED_SURPLUS_GRANTS:
        assert surplus.reason.strip(), surplus


def test_the_removed_seed_put_item_covers_exactly_the_tables_the_corpus_writes() -> None:
    """The removed sweep-control PutItem rows are the tables `fixtures.generate.seed_tenant` writes."""
    from fixtures.generate import generate_dataset

    written = frozenset(generate_dataset("eval-0123456789abcdef0123456789abcdef").as_mapping())
    removed_puts = {
        table
        for function, table, action in _SWEEP_REMOVED_GRANTS
        if function == "sweep-control" and action == "dynamodb:PutItem"
    }
    assert removed_puts == written == _SWEEP_SEED_TABLES


def test_no_removed_sweep_grant_is_declared_or_granted(matrix_grants: Any) -> None:
    """Each removed sweep grant is neither a declared surplus nor a matrix grant."""
    declared = {(s.function, s.table, s.action) for s in gate.DECLARED_SURPLUS_GRANTS}
    assert not _SWEEP_REMOVED_GRANTS & declared
    for function, table, action in sorted(_SWEEP_REMOVED_GRANTS):
        assert action not in matrix_grants.get((function, table), set()), (function, table, action)


@pytest.mark.parametrize("removed", sorted(_SWEEP_REMOVED_GRANTS))
def test_re_adding_a_removed_sweep_grant_reds_the_gate(
    analysis: Any, matrix_grants: Any, removed: tuple[str, str, str]
) -> None:
    """Control: a removed sweep grant returning to the matrix is exactly one SURPLUS finding."""
    function, table, action = removed
    widened = {key: set(value) for key, value in matrix_grants.items()}
    widened.setdefault((function, table), set()).add(action)
    surplus = [f for f in gate.compare(analysis.derived, widened, "fixture") if "SURPLUS" in f]
    assert len(surplus) == 1, surplus
    assert f"{function} is granted {action} on {table}" in surplus[0], surplus


def test_golden_tasks_name_only_the_declared_assertion_tables() -> None:
    """The scoring table domain is bounded by what `golden_tasks.yaml` names, so a task naming
    another table reds here before its read is denied at run time."""
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks

    named: set[str] = set()
    for task in _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH):
        assertions = cast("dict[str, Any]", task.get("assertions") or {})
        for key in ("must_not_change", "expected_field_changes"):
            named.update(str(entry["table"]) for entry in assertions.get(key) or [])
    assert named, "no golden task names a table, so the bound is untested"
    assert named <= gate.GOLDEN_TASK_ASSERTION_TABLES, named - gate.GOLDEN_TASK_ASSERTION_TABLES


def test_the_assertion_tables_are_the_seeded_corpus_tables() -> None:
    from fixtures.generate import generate_dataset

    seeded = frozenset(generate_dataset("eval-0123456789abcdef0123456789abcdef").as_mapping())
    assert seeded == gate.GOLDEN_TASK_ASSERTION_TABLES


def test_widening_either_exception_list_is_visible_rather_than_silent(
    analysis: Any, matrix_grants: Any
) -> None:
    """Adding a row to `DECLARED_SURPLUS_GRANTS` silences a real SURPLUS finding."""
    widened = {key: set(value) for key, value in matrix_grants.items()}
    widened[("get_contact", "accounts")] = {"dynamodb:DeleteItem"}
    assert any("SURPLUS GRANT" in f for f in gate.compare(analysis.derived, widened, "fixture"))

    silenced = (
        *gate.DECLARED_SURPLUS_GRANTS,
        gate.SurplusGrant("get_contact", "accounts", "dynamodb:DeleteItem", "quiet edit"),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gate, "DECLARED_SURPLUS_GRANTS", silenced)
        assert not any(
            "SURPLUS GRANT" in f for f in gate.compare(analysis.derived, widened, "fixture")
        )


# ---------------------------------------------------------------------------------------------
# Scope: the analyser follows `src/eval`, and the two exclusion lists are pinned.
# ---------------------------------------------------------------------------------------------


def test_source_roots_include_eval_so_its_store_calls_are_visible() -> None:
    """`SOURCE_ROOTS` is exactly `crm`, `gateway` and `eval`."""
    assert gate.SOURCE_ROOTS == ("crm", "gateway", "eval")


def test_the_sweep_trigger_is_a_registered_entry_point() -> None:
    """`eval-trigger` is a registered extra entry point and a non-tool function."""
    # The key is a hyphenated matrix function name; the module path keeps its underscores.
    assert "eval-trigger" in gate.EXTRA_ENTRY_POINTS
    module, entries = gate.EXTRA_ENTRY_POINTS["eval-trigger"]
    assert module == "crm.handlers.eval_trigger"
    assert "handler" in entries
    assert "eval-trigger" in gate.NON_TOOL_FUNCTIONS


_TENANT_TABLES = frozenset(
    {
        "accounts",
        "contacts",
        "opportunities",
        "activities",
        "notes",
        "audit_records",
        "proposals",
        "reps",
        "usage_records",
    }
)

#: What `eval.sfn_handlers` reaches, per function and table.
_SWEEP_DERIVED: dict[tuple[str, str], set[str]] = {
    ("sweep-control", "eval_runs"): {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    },
    ("sweep-control", "eval_results"): {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
    },
    **{
        ("sweep-control", table): {"dynamodb:Query", "dynamodb:BatchWriteItem"}
        for table in _TENANT_TABLES
    },
    ("sweep-task", "eval_runs"): {
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
    },
    ("sweep-task", "eval_results"): {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
    },
    **{
        ("sweep-task", table): {"dynamodb:GetItem", "dynamodb:Query"}
        for table in _SWEEP_SEED_TABLES
    },
    ("sweep-task", "proposals"): {"dynamodb:GetItem"},
}


def test_the_sweep_handlers_are_registered_non_tool_entry_points() -> None:
    """Both sweep functions are entry points into `eval.sfn_handlers` and non-tool."""
    assert gate.EXTRA_ENTRY_POINTS["sweep-control"] == (
        "eval.sfn_handlers",
        ("sweep_control_handler",),
    )
    assert gate.EXTRA_ENTRY_POINTS["sweep-task"] == ("eval.sfn_handlers", ("sweep_task_handler",))
    assert {"sweep-control", "sweep-task"} <= gate.NON_TOOL_FUNCTIONS


def test_the_sweep_handlers_derive_exactly_the_pinned_grants(analysis: Any) -> None:
    """Every DynamoDB action the sweep handlers reach, per table: a new store call is visible here.

    Includes the `lock#sweep` UpdateItem `set_execution_arn` issues and the
    TransactWriteItems of `record_item` and `release_lock`.
    """
    derived = {
        key: set(actions)
        for key, actions in analysis.derived.items()
        if key[0] in {"sweep-control", "sweep-task"}
    }
    assert derived == _SWEEP_DERIVED


def test_the_sweep_matrix_rows_cover_the_derived_grants_plus_declared_surplus(
    analysis: Any, matrix_grants: Any
) -> None:
    """The matrix's sweep rows are exactly the derived grants and the declared surplus rows."""
    declared: dict[tuple[str, str], set[str]] = {
        (s.function, s.table): set() for s in gate.DECLARED_SURPLUS_GRANTS
    }
    for surplus in gate.DECLARED_SURPLUS_GRANTS:
        declared[(surplus.function, surplus.table)].add(surplus.action)
    for key in {k for k in matrix_grants if k[0] in {"sweep-control", "sweep-task"}} | set(
        _SWEEP_DERIVED
    ):
        assert matrix_grants.get(key, set()) == _SWEEP_DERIVED.get(key, set()) | declared.get(
            key, set()
        ), key


def test_harness_exemption_is_pinned_exactly() -> None:
    """The harness exemption constants hold exactly their expected values."""
    expected_reachers: dict[str, frozenset[str]] = {"eval.tenancy": frozenset({"sweep-control"})}

    assert gate._STORE_MODULE == "crm.store.store"
    assert gate._HARNESS_ONLY_UNREACHED_SITES == {"eval.tenancy": 2}
    assert expected_reachers == gate._EXEMPTION_REACHED_BY


def test_a_new_lambda_reaching_an_exempted_module_invalidates_the_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new Lambda reaching an exempted module reports the exemption stale."""
    monkeypatch.setitem(
        gate.EXTRA_ENTRY_POINTS,
        "eval_reaper",
        ("eval.tenancy", ("seed_tenant", "teardown_tenant")),
    )

    analysis = gate.derive_grants()

    reachers = [f for f in analysis.stale_exemptions if "eval_reaper now does" in f]
    assert reachers, analysis.stale_exemptions


def _shared_module_tree(tmp_path: Path, caller_body: str) -> Path:
    """A `src` tree with `crm.shared.read` (one `reps` GetItem) and a `crm.caller.entry`."""
    src = tmp_path / "src"
    (src / "crm").mkdir(parents=True)
    (src / "crm" / "__init__.py").write_text("", encoding="utf-8")
    (src / "crm" / "shared.py").write_text(
        'def read(store):\n    return store.get_item("reps", {"rep_id": "x"})\n', encoding="utf-8"
    )
    (src / "crm" / "caller.py").write_text(caller_body, encoding="utf-8")
    return src


def test_two_functions_sharing_a_module_each_derive_its_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk memo is per function: a second entry point into a walked module still derives."""
    src = _shared_module_tree(tmp_path, "")
    monkeypatch.setattr(
        gate,
        "EXTRA_ENTRY_POINTS",
        {"fn-a": ("crm.shared", ("read",)), "fn-b": ("crm.shared", ("read",))},
    )
    derived = gate.derive_grants(src).derived
    assert derived.get(("fn-a", "reps")) == {"dynamodb:GetItem"}
    assert derived.get(("fn-b", "reps")) == {"dynamodb:GetItem"}


def test_a_shared_memo_would_mask_the_second_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: with the per-function reset removed, the second function derives nothing."""
    src = _shared_module_tree(tmp_path, "")
    monkeypatch.setattr(
        gate,
        "EXTRA_ENTRY_POINTS",
        {"fn-a": ("crm.shared", ("read",)), "fn-b": ("crm.shared", ("read",))},
    )

    def analyse_without_reset(
        self: Any, function: str, module: str, entry_points: tuple[str, ...]
    ) -> None:
        self._function = function
        for entry in entry_points:
            index = self._index[module]
            self._walk(index, index.functions[entry], {})

    monkeypatch.setattr(gate.Analyser, "analyse", analyse_without_reset)
    derived = gate.derive_grants(src).derived
    assert ("fn-b", "reps") not in derived


def test_a_submodule_attribute_call_is_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`from crm import shared` then `shared.read(store)` reaches `shared.read`'s store call."""
    src = _shared_module_tree(
        tmp_path, "from crm import shared\n\n\ndef entry(store):\n    return shared.read(store)\n"
    )
    monkeypatch.setattr(gate, "EXTRA_ENTRY_POINTS", {"fn-c": ("crm.caller", ("entry",))})
    assert gate.derive_grants(src).derived.get(("fn-c", "reps")) == {"dynamodb:GetItem"}

    # Control: without the submodule resolution the same call derives nothing.
    monkeypatch.setattr(gate.Analyser, "_lookup_module_attribute_call", lambda *_a: None)
    assert ("fn-c", "reps") not in gate.derive_grants(src).derived


_PENDING_RUNS_FUNCTIONS = frozenset({"mark_running", "record_item", "finalize"})


def test_no_pending_entry_point_site_or_function_remains() -> None:
    """With `eval.sfn_handlers` present every sweep store call site is reached, so no pending
    site and no pending function is declared, and the real tree reports none unreached."""
    assert gate._PENDING_ENTRY_POINT_SITES == {}
    assert frozenset() == gate.PENDING_FUNCTIONS
    analysis = gate.derive_grants()
    assert not [u for u in analysis.unresolved if u.startswith("eval.")], analysis.unresolved
    assert analysis.stale_exemptions == []


def test_a_named_function_without_an_unreached_site_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(gate._PENDING_ENTRY_POINT_SITES, "eval.runs", frozenset({"read_run"}))
    stale = [f for f in gate.derive_grants().stale_exemptions if "eval.runs" in f]
    assert stale and "read_run is now reached" in stale[0], stale


def _pending_runs_tree(tmp_path: Path) -> Path:
    """A `src` tree whose `eval.runs` has the three pending functions plus an `orphan` site, and an
    entry point that reaches `mark_running`."""
    src = tmp_path / "src"
    for package in ("crm", "eval"):
        (src / package).mkdir(parents=True)
        (src / package / "__init__.py").write_text("", encoding="utf-8")
    site = '    return store.get_item("eval_runs", {"run_id": "x"})\n'
    (src / "eval" / "runs.py").write_text(
        "".join(
            f"def {name}(store):\n{site}\n\n"
            for name in ("mark_running", "record_item", "finalize", "orphan")
        ),
        encoding="utf-8",
    )
    (src / "crm" / "caller.py").write_text(
        "from eval.runs import mark_running\n\n\ndef entry(store):\n    return mark_running(store)\n",
        encoding="utf-8",
    )
    return src


def test_an_orphan_site_and_a_newly_reached_function_both_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One site becoming reachable while an unrelated one appears keeps a count at 3 but
    changes the identities; both changes are reported."""
    src = _pending_runs_tree(tmp_path)
    monkeypatch.setattr(gate, "EXTRA_ENTRY_POINTS", {"fn-c": ("crm.caller", ("entry",))})
    monkeypatch.setattr(gate, "_PENDING_ENTRY_POINT_SITES", {"eval.runs": _PENDING_RUNS_FUNCTIONS})
    analysis = gate.derive_grants(src)
    orphan_line = 1 + 4 * 3 + 1
    assert f"eval.runs:{orphan_line}:store call site never reached by any entry point" in (
        analysis.unresolved
    )
    stale = [f for f in analysis.stale_exemptions if "eval.runs" in f]
    assert stale and "mark_running is now reached" in stale[0], stale


def test_every_additional_root_resolves_on_the_real_tree() -> None:
    """Every `_ADDITIONAL_ROOTS` module and entry resolves on the real tree."""
    index = gate.build_index(REPO_ROOT / "src")

    for function, roots in gate._ADDITIONAL_ROOTS.items():
        for module, entries in roots:
            assert module in index, f"{function}: additional root module {module} not in the index"
            for entry in entries:
                assert entry in index[module].functions, f"{function}: {module}.{entry} not found"


def test_an_added_harness_call_site_is_not_absorbed_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An understated harness unreached-site count is reported stale on its own."""
    monkeypatch.setattr(
        gate, "_HARNESS_ONLY_UNREACHED_SITES", {"eval.scoring": 3, "eval.tenancy": 3}
    )

    analysis = gate.derive_grants()

    counts = [f for f in analysis.stale_exemptions if "exactly 3 unreached" in f]
    assert counts, analysis.stale_exemptions
    assert not [f for f in analysis.stale_exemptions if "now does" in f], "reacher check misfired"


def test_the_real_tree_reports_no_stale_exemption() -> None:
    """The real tree reports no stale exemption."""
    assert gate.derive_grants().stale_exemptions == []


def test_a_stale_exemption_is_not_reported_as_an_undeclared_table_domain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale exemption is reported as STALE EXEMPTION, not as a table-domain finding."""
    monkeypatch.setattr(
        gate, "_HARNESS_ONLY_UNREACHED_SITES", {"eval.scoring": 3, "eval.tenancy": 3}
    )

    exit_code = gate.main([])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "STALE EXEMPTION eval.tenancy" in out
    stale_lines = [line for line in out.splitlines() if "STALE EXEMPTION" in line]
    assert stale_lines and not any("DECLARED_TABLE_DOMAINS" in line for line in stale_lines)


def test_excluding_a_module_from_the_scan_does_not_hide_a_reached_grant(analysis: Any) -> None:
    """Store calls in a scan-excluded module are still attributed when reached."""
    reached = {
        (function, table) for function, table in analysis.derived if function == "eval-trigger"
    }

    assert ("eval-trigger", "eval_runs") in reached, sorted(reached)


def _grant_objects(main_tf: str) -> list[str]:
    """Return every innermost brace-balanced object literal in `main.tf` carrying a `table_arn`."""
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for index, char in enumerate(main_tf):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            spans.append((stack.pop(), index + 1))

    carrying = [
        (start, end) for start, end in spans if re.search(r"\btable_arn\s*=", main_tf[start:end])
    ]
    # Keep only the innermost span: an enclosing `module` body also matches `table_arn`.
    return [
        main_tf[start:end]
        for start, end in carrying
        if not any(
            other != (start, end) and start <= other[0] and other[1] <= end for other in carrying
        )
    ]


def _field(block: str, name: str) -> str | None:
    """Return the raw right-hand side of `name = ...` in `block`, or `None` if absent."""
    match = re.search(rf"^\s*{name}\s*=\s*(?P<value>.+)$", block, re.MULTILINE)
    return match.group("value").strip() if match else None


def _may_include_query(actions: str) -> bool:
    """Return whether an `actions` expression can yield `dynamodb:Query`.

    A non-literal expression is treated as possibly including `Query`.
    """
    literal = re.fullmatch(r"\[(?P<items>[^\]]*)\]", actions)
    if literal is None:
        return True
    quoted = re.findall(r'"([^"]+)"', literal.group("items"))
    return "dynamodb:Query" in quoted


def _index_arns_is_satisfied(index_arns: str | None) -> bool:
    """Return whether an `index_arns` value grants index sub-resources for a Query.

    Accepts a non-empty list, any expression, and a conditional keyed on `dynamodb:Query`;
    rejects an absent key and a bare `[]`.
    """
    if index_arns is None:
        return False
    if index_arns.startswith("?") or "dynamodb:Query" in index_arns:
        return True
    return index_arns != "[]"


def test_every_query_grant_in_main_tf_carries_index_arns() -> None:
    """Every grant in `main.tf` that may include `dynamodb:Query` carries index ARNs."""
    blocks = _grant_objects(MAIN_TF.read_text(encoding="utf-8"))

    assert len(blocks) >= 6, f"the grant-object scan found only {len(blocks)}; it has gone blind"

    offenders = [
        (_field(block, "table_name"), _field(block, "actions"))
        for block in blocks
        if (actions := _field(block, "actions")) is not None
        and _may_include_query(actions)
        and not _index_arns_is_satisfied(_field(block, "index_arns"))
    ]

    assert offenders == [], (
        "grant(s) may include dynamodb:Query but supply no index ARNs; every Query here targets a "
        f"named GSI, so the statement cannot run as written: {offenders}"
    )


def test_the_index_arn_check_fails_on_a_planted_bespoke_grant() -> None:
    """A planted bespoke `Query` grant without index ARNs is reported."""
    planted = (
        MAIN_TF.read_text(encoding="utf-8")
        + """
module "planted_bespoke" {
  dynamodb_grants = [
    {
      table_name = "accounts"
      table_arn  = module.dynamodb.table_arns["accounts"]
      actions    = ["dynamodb:Query", "dynamodb:BatchWriteItem"]
    },
  ]
}
"""
    )

    blocks = _grant_objects(planted)
    offenders = [
        (_field(block, "table_name") or "").strip(chr(34))
        for block in blocks
        if (actions := _field(block, "actions")) is not None
        and _may_include_query(actions)
        and not _index_arns_is_satisfied(_field(block, "index_arns"))
    ]

    assert offenders == ["accounts"], offenders


def test_a_getitem_only_grant_does_not_require_index_arns() -> None:
    """Only an `actions` expression that may yield `Query` requires index ARNs."""
    assert not _may_include_query('["dynamodb:GetItem"]')
    assert _may_include_query('["dynamodb:GetItem", "dynamodb:Query"]')
    assert _may_include_query("grant.actions")
    assert _may_include_query('setsubtract(local.dynamodb_asserted_actions, ["dynamodb:GetItem"])')


def test_index_arns_satisfaction_rejects_the_module_default() -> None:
    """An absent or empty `index_arns` is rejected; a list or Query conditional is accepted."""
    assert not _index_arns_is_satisfied(None)
    assert not _index_arns_is_satisfied("[]")
    assert _index_arns_is_satisfied('["${module.dynamodb.table_arns["reps"]}/index/*"]')
    assert _index_arns_is_satisfied(
        'contains(grant.actions, "dynamodb:Query") ? values(module.dynamodb.gsi_arns[grant.table]) : []'
    )


# ---------------------------------------------------------------------------
# The non-DynamoDB half of each role's policy set: `(function, resource, action)`.
# ---------------------------------------------------------------------------


class TestResourceGrantCoverage:
    """Coverage of each role's non-DynamoDB grants: KMS actions and log writes."""

    MODULE_TF = REPO_ROOT / "infra" / "modules" / "lambda_action" / "main.tf"

    @pytest.fixture(scope="class")
    @staticmethod
    def kms_enabled(matrix_grants: Any) -> Any:
        return gate.load_kms_enabled_functions(MAIN_TF, frozenset({f for f, _t in matrix_grants}))

    def test_the_kms_enabled_set_is_exactly_the_functions_that_must_have_it(
        self, kms_enabled: Any, matrix_grants: Any
    ) -> None:
        """The parsed KMS-enabled set equals the expected set exactly."""
        known = frozenset({f for f, _t in matrix_grants})
        assert kms_enabled == gate.expected_kms_enabled_functions(known)

    def test_singleton_call_functions_names_exactly_the_non_tool_functions(self) -> None:
        """`SINGLETON_CALL_FUNCTIONS` maps to exactly the non-tool and pending functions."""
        assert frozenset(gate.SINGLETON_CALL_FUNCTIONS.values()) == (
            gate.NON_TOOL_FUNCTIONS | gate.PENDING_FUNCTIONS
        )

    def test_no_singleton_function_name_is_reachable_by_a_mechanical_transform(self) -> None:
        """The singleton function names are not derivable by replacing `_` with `-`."""
        mechanical = {suffix.replace("_", "-") for suffix in gate.SINGLETON_CALL_FUNCTIONS}
        assert mechanical != gate.NON_TOOL_FUNCTIONS | gate.PENDING_FUNCTIONS
        assert "interceptor" not in gate.NON_TOOL_FUNCTIONS

    def test_the_fan_out_branch_contributes_exactly_the_tool_functions(
        self, tmp_path: Path, matrix_grants: Any
    ) -> None:
        """The fan-out block alone contributes exactly the tool functions to the KMS-enabled set."""
        known = frozenset({f for f, _t in matrix_grants})
        fan_out_only = tmp_path / "main.tf"
        fan_out_only.write_text(
            'module "lambda_action" {\n'
            "  for_each                   = local.tools\n"
            "  dynamodb_kms_grant_enabled = true\n"
            "}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError) as excinfo:
            gate.load_kms_enabled_functions(fan_out_only, known)

        message = str(excinfo.value)
        assert "Missing (reaches an SSE-KMS table" in message
        for function in sorted(gate.NON_TOOL_FUNCTIONS | gate.PENDING_FUNCTIONS):
            assert function in message, (
                f"{function} was not reported missing, so the fan-out branch is contributing it -- "
                "the blanket `update(known_functions)` is back and the singleton half is masked again"
            )
        assert "Surplus (granted kms:Decrypt without being a known function): none" in message

    # --- mutation tests: the KMS arm fails in both directions -------------------------------------
    #
    # Each mutates a copy of `infra/main.tf` in `tmp_path`, never the real tree.

    def _mutated_main_tf(self, tmp_path: Path, mutate: Any, only: str | None = None) -> Path:
        """A copy of every root `*.tf` file, with `mutate` applied to the one declaring the call.

        `only` names the single file to mutate when `mutate` would change any file (an append).
        """
        for source in sorted(MAIN_TF.parent.glob("*.tf")):
            (tmp_path / source.name).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
        changed = 0
        for target in sorted(tmp_path.glob("*.tf")):
            if only is not None and target.name != only:
                continue
            text = target.read_text(encoding="utf-8")
            try:
                mutated = mutate(text)
            except AssertionError:
                continue
            if mutated != text:
                target.write_text(mutated, encoding="utf-8")
                changed += 1
        assert changed == 1, f"the mutation changed {changed} files, so this test proves nothing"
        return tmp_path / "main.tf"

    def test_an_unmutated_copy_of_the_root_files_is_accepted(
        self, tmp_path: Path, matrix_grants: Any
    ) -> None:
        """Control: the copied root, unchanged, parses to the expected set, so a refusal is the mutation's."""
        known = frozenset({f for f, _t in matrix_grants})
        for source in sorted(MAIN_TF.parent.glob("*.tf")):
            (tmp_path / source.name).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
        assert gate.load_kms_enabled_functions(
            tmp_path / "main.tf", known
        ) == gate.expected_kms_enabled_functions(known)

    @staticmethod
    def _drop_flag_from(text: str, module_name: str) -> str:
        match = re.search(rf'^module "{module_name}"\s*\{{.*?^\}}', text, re.S | re.M)
        assert match is not None, f'no `module "{module_name}"` block in main.tf'
        body = match.group(0)
        stripped = re.sub(r"^\s*dynamodb_kms_grant_enabled\s*=\s*true.*\n", "", body, flags=re.M)
        assert stripped != body, (
            f"{module_name} does not enable the KMS grant, so dropping it is a no-op"
        )
        return text.replace(body, stripped)

    @pytest.mark.parametrize(
        "module_name",
        [
            "lambda_action",  # the for_each fan-out carrying every tool Lambda
            "lambda_action_interceptor",
            "lambda_action_eval_trigger",
            "lambda_action_pre_token_generation",
            "lambda_action_user_settings",
            "lambda_action_chat_target",
            "lambda_action_chat_interceptor",
            "lambda_action_sweep_control",
            "lambda_action_sweep_task",
        ],
    )
    def test_dropping_the_kms_flag_from_any_call_site_is_refused(
        self, tmp_path: Path, matrix_grants: Any, module_name: str
    ) -> None:
        """Dropping the KMS flag from any call site is refused."""
        known = frozenset({f for f, _t in matrix_grants})
        mutated = self._mutated_main_tf(
            tmp_path, lambda text: self._drop_flag_from(text, module_name)
        )
        with pytest.raises(ValueError, match="not the set that must have it"):
            gate.load_kms_enabled_functions(mutated, known)

    def test_the_replaced_floor_would_have_passed_every_one_of_those_mutants(
        self, tmp_path: Path, matrix_grants: Any
    ) -> None:
        """A count-floor parse passes a mutant that the set-equality check refuses."""
        known = frozenset({f for f, _t in matrix_grants})

        def old_parse(main_tf: Path) -> set[str]:
            text = main_tf.read_text(encoding="utf-8")
            enabled: set[str] = set()
            for call in gate._LAMBDA_ACTION_CALL_RE.finditer(text):
                body = call.group("body")
                if not re.search(r"^\s*dynamodb_kms_grant_enabled\s*=\s*true", body, re.MULTILINE):
                    continue
                if re.search(r"^\s*for_each\s*=", body, re.MULTILINE):
                    enabled.update(known)
                else:
                    enabled.add(call.group("name").removeprefix("lambda_action_"))
            return enabled

        def strip_every_singleton(text: str) -> str:
            for name in (
                "lambda_action_interceptor",
                "lambda_action_eval_trigger",
                "lambda_action_pre_token_generation",
            ):
                text = self._drop_flag_from(text, name)
            return text

        mutated = self._mutated_main_tf(tmp_path, strip_every_singleton)

        assert len(old_parse(mutated)) >= 20, (
            "the reconstructed old parse no longer clears its own floor, so this test has stopped "
            "discriminating and the comparison below proves nothing"
        )
        with pytest.raises(ValueError, match="not the set that must have it"):
            gate.load_kms_enabled_functions(mutated, known)

    def test_a_singleton_call_naming_an_unknown_function_is_refused(
        self, tmp_path: Path, matrix_grants: Any
    ) -> None:
        """A singleton call enabling KMS for an unmapped function is refused."""
        known = frozenset({f for f, _t in matrix_grants})
        block = (
            '\nmodule "lambda_action_bogus" {\n'
            '  source                     = "./modules/lambda_action"\n'
            "  dynamodb_kms_grant_enabled = true\n"
            "}\n"
        )
        mutated = self._mutated_main_tf(tmp_path, lambda text: text + block, only="main.tf")
        with pytest.raises(ValueError, match="not in SINGLETON_CALL_FUNCTIONS"):
            gate.load_kms_enabled_functions(mutated, known)

    def test_a_surplus_kms_enabled_function_is_refused(self, matrix_grants: Any) -> None:
        """A KMS-enabled function outside the known set is refused as surplus."""
        known = frozenset({f for f, _t in matrix_grants})
        with pytest.raises(ValueError, match="Surplus"):
            gate.load_kms_enabled_functions(MAIN_TF, known - gate.NON_TOOL_FUNCTIONS)

    def test_every_analysed_function_implies_the_own_log_group_writes(
        self, analysis: Any, kms_enabled: Any
    ) -> None:
        """Every analysed function implies exactly the log-write actions on its own log group."""
        implied = gate.derive_resource_grants(analysis.derived, kms_enabled)
        functions = {f for f, _t in analysis.derived}
        assert functions, "no functions derived, so this asserts nothing"
        for function in sorted(functions):
            actions = implied.get((function, gate.LOGS_RESOURCE_KEY), set())
            assert actions == set(gate.LOGS_IMPLIED_ACTIONS), f"{function}: {actions}"

    def test_logs_create_log_group_is_never_implied_or_granted(self) -> None:
        """`logs:CreateLogGroup` is neither implied nor granted."""
        assert "logs:CreateLogGroup" not in gate.LOGS_IMPLIED_ACTIONS
        granted = gate.load_module_resource_grants(
            self.MODULE_TF, frozenset({"probe"}), frozenset({"probe"})
        )
        for (_function, _resource), actions in granted.items():
            assert "logs:CreateLogGroup" not in actions, (
                "the module grants logs:CreateLogGroup, which is account-wide rather than scoped to "
                "this function's own log group"
            )

    def test_a_function_without_the_kms_flag_implies_no_kms_action(self, analysis: Any) -> None:
        """A function without the KMS flag implies no KMS action."""
        implied = gate.derive_resource_grants(analysis.derived, frozenset())
        assert implied, "no implied grants at all, so this asserts nothing"
        assert not any(resource == gate.KMS_RESOURCE_KEY for _f, resource in implied)

    def test_the_real_repository_has_no_missing_resource_grant(
        self, analysis: Any, matrix_grants: Any, kms_enabled: Any
    ) -> None:
        """Every implied KMS and logs action is granted by the `lambda_action` module."""
        implied = gate.derive_resource_grants(analysis.derived, kms_enabled)
        granted = gate.load_module_resource_grants(
            self.MODULE_TF, frozenset({f for f, _t in analysis.derived}), kms_enabled
        )
        findings = gate.compare(implied, granted, "lambda_action module")
        missing = [f for f in findings if f.startswith("MISSING GRANT")]
        assert missing == [], "\n".join(missing)

    @pytest.mark.parametrize(
        ("dropped", "expected_action"),
        [
            pytest.param("kms:Decrypt", "kms:Decrypt", id="kms_decrypt"),
            pytest.param("kms:GenerateDataKey", "kms:GenerateDataKey", id="kms_generatedatakey"),
            pytest.param("logs:PutLogEvents", "logs:PutLogEvents", id="logs_putlogevents"),
            pytest.param("logs:CreateLogStream", "logs:CreateLogStream", id="logs_createlogstream"),
        ],
    )
    def test_removing_a_granted_action_is_reported_as_missing(
        self, analysis: Any, kms_enabled: Any, dropped: str, expected_action: str
    ) -> None:
        """Removing a granted KMS or logs action in memory is reported MISSING."""
        granted = gate.load_module_resource_grants(
            self.MODULE_TF, frozenset({f for f, _t in analysis.derived}), kms_enabled
        )
        assert any(dropped in actions for actions in granted.values()), (
            f"{dropped} is not granted anywhere, so removing it proves nothing"
        )
        for actions in granted.values():
            actions.discard(dropped)

        implied = gate.derive_resource_grants(analysis.derived, kms_enabled)
        findings = gate.compare(implied, granted, "lambda_action module")
        assert any(f.startswith("MISSING GRANT") and expected_action in f for f in findings), (
            f"dropping {dropped} produced no MISSING finding naming it: {findings[:3]}"
        )

    def test_an_undeclared_surplus_action_still_fails(
        self, analysis: Any, kms_enabled: Any
    ) -> None:
        """An undeclared action added to the module KMS policy is reported SURPLUS."""
        granted = gate.load_module_resource_grants(
            self.MODULE_TF, frozenset({f for f, _t in analysis.derived}), kms_enabled
        )
        for (_function, resource), actions in granted.items():
            if resource == gate.KMS_RESOURCE_KEY:
                actions.add("kms:ReEncryptFrom")

        implied = gate.derive_resource_grants(analysis.derived, kms_enabled)
        findings = gate.compare(implied, granted, "lambda_action module")
        assert any("kms:ReEncryptFrom" in f and f.startswith("SURPLUS") for f in findings), (
            "an unaudited action added to the module policy was silently exempted"
        )

    def test_the_declared_surplus_exemption_is_scoped_to_resource_keys(self) -> None:
        """The surplus resource-action exemption applies only to namespaced resource keys."""
        source = gate.__file__
        assert source is not None
        text = Path(source).read_text(encoding="utf-8")
        assert 'table.startswith("@")' in text, (
            "the resource-key surplus exemption is no longer scoped to namespaced keys, so it can "
            "excuse a DynamoDB table action"
        )

    def test_every_declared_surplus_resource_action_carries_a_reason(self) -> None:
        assert gate.DECLARED_SURPLUS_RESOURCE_ACTIONS, (
            "the declaration is empty, so this asserts nothing"
        )
        for action, reason in gate.DECLARED_SURPLUS_RESOURCE_ACTIONS.items():
            assert len(reason) > 80, f"{action} has no substantive reason recorded"

    def test_the_kms_describekey_exemption_does_not_claim_it_is_required(self) -> None:
        """The `kms:DescribeKey` exemption reason states its need is not established."""
        reason = gate.DECLARED_SURPLUS_RESOURCE_ACTIONS["kms:DescribeKey"]
        assert "NOT established" in reason
        assert "T3" in reason


_CAPABILITIES_HANDLER = Path("crm") / "handlers" / "capabilities.py"
_INJECTED_STORE_CALL = '    store.get_item("reps", {"rep_id": "x"})\n'


def _scratch_source_tree(tmp_path: Path, *, inject: bool) -> Path:
    """Copy exactly the gate's `SOURCE_ROOTS` into `tmp_path`, optionally planting a store call.

    The planted call is the first statement of the capabilities `handler`, after its docstring.
    """
    src = tmp_path / "src"
    for root in gate.SOURCE_ROOTS:
        shutil.copytree(
            REPO_ROOT / "src" / root, src / root, ignore=shutil.ignore_patterns("__pycache__")
        )
    if inject:
        handler = src / _CAPABILITIES_HANDLER
        text = handler.read_text(encoding="utf-8")
        anchor = "    del context\n"
        assert text.count(anchor) == 1
        handler.write_text(text.replace(anchor, anchor + _INJECTED_STORE_CALL), encoding="utf-8")
    return src


def test_capabilities_is_an_analysed_zero_grant_entry_point() -> None:
    """The matrix's zero-grant function is analysed from its real handler."""
    assert gate.load_zero_grant_functions(MATRIX) == frozenset({"capabilities"})
    assert gate.EXTRA_ENTRY_POINTS["capabilities"] == ("crm.handlers.capabilities", ("handler",))


def test_the_capabilities_handler_derives_no_grant(analysis: Any) -> None:
    """Its implied set is the matrix's zero: no `(capabilities, table)` pair is derived."""
    assert not [key for key in analysis.derived if key[0] == "capabilities"]


@pytest.mark.parametrize("inject", [False, True], ids=["unmutated-copy", "planted-store-call"])
def test_a_store_call_planted_in_the_capabilities_handler_reds_the_gate_naming_it(
    tmp_path: Path, matrix_grants: Any, inject: bool
) -> None:
    """A store call added to the capabilities handler is a MISSING GRANT naming capabilities."""
    analysis = gate.derive_grants(_scratch_source_tree(tmp_path, inject=inject))
    findings = gate.compare(analysis.derived, matrix_grants, "matrix.json") + sorted(
        analysis.unresolved
    )
    if not inject:
        assert findings == []
        return
    assert analysis.derived.get(("capabilities", "reps")) == {"dynamodb:GetItem"}
    assert findings == [
        "MISSING GRANT [matrix.json] capabilities reaches dynamodb:GetItem on reps but the "
        "declared grant map does not permit it"
    ]


def test_a_zero_grant_function_without_an_entry_point_is_reported(
    matrix_grants: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping capabilities from `EXTRA_ENTRY_POINTS` reds the namespace check, naming it."""
    terraform = gate.load_terraform_grants(MAIN_TF)
    zero = gate.load_zero_grant_functions(MATRIX)
    assert (
        gate.check_function_key_namespace(matrix_grants, terraform, zero_grant_functions=zero) == []
    )
    monkeypatch.delitem(gate.EXTRA_ENTRY_POINTS, "capabilities")
    findings = gate.check_function_key_namespace(
        matrix_grants, terraform, zero_grant_functions=zero
    )
    assert len(findings) == 1 and "'capabilities'" in findings[0], findings
    assert "EXTRA_ENTRY_POINTS does not name it" in findings[0]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"functions_without_dynamodb_grants": "capabilities"},
        {"functions_without_dynamodb_grants": [1]},
    ],
    ids=["absent", "string", "non-string-item"],
)
def test_a_malformed_zero_grant_list_is_refused(tmp_path: Path, payload: dict[str, Any]) -> None:
    """The zero-grant list must be present and a list of strings, or the gate errors."""
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="functions_without_dynamodb_grants"):
        gate.load_zero_grant_functions(path)


def test_the_only_declared_unreachable_grant_is_revert_putitem_on_notes() -> None:
    """The one reach the analyser attributes that no request can take."""
    assert [(u.function, u.table, u.action) for u in gate.DECLARED_UNREACHABLE_GRANTS] == [
        ("revert_audit_record", "notes", "dynamodb:PutItem")
    ]
    assert all(u.reason.strip() for u in gate.DECLARED_UNREACHABLE_GRANTS)


def test_a_declared_unreachable_reach_is_not_a_missing_grant() -> None:
    derived = {("revert_audit_record", "notes"): {"dynamodb:PutItem", "dynamodb:DeleteItem"}}
    declared = {("revert_audit_record", "notes"): {"dynamodb:DeleteItem"}}
    assert gate.compare(derived, declared, "main.tf") == []


def test_an_undeclared_missing_reach_still_fails() -> None:
    derived = {("revert_audit_record", "notes"): {"dynamodb:UpdateItem", "dynamodb:DeleteItem"}}
    declared = {("revert_audit_record", "notes"): {"dynamodb:DeleteItem"}}
    findings = gate.compare(derived, declared, "main.tf")
    assert len(findings) == 1 and "MISSING GRANT" in findings[0] and "UpdateItem" in findings[0]


def test_granting_a_declared_unreachable_action_fails() -> None:
    derived = {("revert_audit_record", "notes"): {"dynamodb:PutItem", "dynamodb:DeleteItem"}}
    declared = {("revert_audit_record", "notes"): {"dynamodb:PutItem", "dynamodb:DeleteItem"}}
    findings = gate.compare(derived, declared, "main.tf")
    assert len(findings) == 1 and "UNREACHABLE GRANTED" in findings[0]


def test_every_public_store_method_has_a_store_method_actions_entry() -> None:
    """A Store method absent from the mapping would be invisible to the gate at every call site."""
    from crm.store.store import Store

    public = {
        name for name in vars(Store) if not name.startswith("_") and callable(vars(Store)[name])
    }
    assert public - set(gate.STORE_METHOD_ACTIONS) == set()


def test_batch_put_items_is_attributed_batch_write_item_only() -> None:
    """Batched puts are PutRequest entries inside BatchWriteItem; no PutItem is issued."""
    method = gate.STORE_METHOD_ACTIONS["batch_put_items"]
    assert method.actions == frozenset({"dynamodb:BatchWriteItem"})


# ---------------------------------------------------------------------------
# The pagination-cursor key, compared as its own resource.
# ---------------------------------------------------------------------------

MODULE_TF = REPO_ROOT / "infra" / "modules" / "lambda_action" / "main.tf"
CURSOR_TOOLS = frozenset(
    {
        "search_opportunities",
        "search_accounts",
        "list_activities",
        "list_audit_records",
        "list_proposals",
    }
)
_CURSOR_TOOLS_LINE = re.compile(r"^(\s*cursor_tools\s*=\s*)\[[^\]]*\]", re.MULTILINE)


def _known_functions(matrix_grants: Any) -> frozenset[str]:
    return frozenset(function for function, _table in matrix_grants)


def _cursor_findings(analysis: Any, main_tf: Path, known: frozenset[str]) -> list[str]:
    kms_enabled = gate.load_kms_enabled_functions(main_tf, known)
    cursor_enabled = gate.load_cursor_enabled_functions(main_tf, known)
    functions = frozenset(function for function, _table in analysis.derived)
    granted = gate.load_module_resource_grants(MODULE_TF, functions, kms_enabled, cursor_enabled)
    implied = gate.derive_resource_grants(analysis.derived, kms_enabled, analysis.reached)
    return [f for f in gate.compare(implied, granted, "lambda_action module") if "cursor" in f]


def _with_cursor_tools(tmp_path: Path, tools: list[str]) -> Path:
    infra = tmp_path / "infra"
    infra.mkdir()
    for path in MAIN_TF.parent.glob("*.tf"):
        shutil.copy(path, infra / path.name)
    main = infra / "main.tf"
    text = main.read_text(encoding="utf-8")
    listed = "[" + ", ".join(f'"{tool}"' for tool in tools) + "]"
    planted, count = _CURSOR_TOOLS_LINE.subn(lambda m: m.group(1) + listed, text)
    assert count == 1
    main.write_text(planted, encoding="utf-8")
    return main


def test_exactly_the_cursor_tools_hold_the_cursor_key_grant(matrix_grants: Any) -> None:
    assert gate.load_cursor_enabled_functions(MAIN_TF, _known_functions(matrix_grants)) == (
        CURSOR_TOOLS
    )


def test_exactly_the_cursor_tools_reach_the_codec(analysis: Any) -> None:
    implied = gate.derive_resource_grants(analysis.derived, frozenset(), analysis.reached)
    users = {f for f, resource in implied if resource == gate.CURSOR_KMS_RESOURCE_KEY}
    assert users == CURSOR_TOOLS
    for tool in CURSOR_TOOLS:
        assert implied[(tool, gate.CURSOR_KMS_RESOURCE_KEY)] == {"kms:Encrypt", "kms:Decrypt"}


def test_the_repository_cursor_grant_matches_the_codec_users(
    analysis: Any, matrix_grants: Any
) -> None:
    assert _cursor_findings(analysis, MAIN_TF, _known_functions(matrix_grants)) == []


def test_a_cursor_grant_on_a_non_cursor_function_is_surplus(
    analysis: Any, matrix_grants: Any, tmp_path: Path
) -> None:
    main = _with_cursor_tools(tmp_path, [*sorted(CURSOR_TOOLS), "get_account"])
    findings = _cursor_findings(analysis, main, _known_functions(matrix_grants))
    assert findings, "a cursor grant on get_account went unreported"
    assert all(f.startswith("SURPLUS GRANT") and " get_account " in f for f in findings), findings


def test_a_codec_user_without_the_cursor_grant_is_missing(
    analysis: Any, matrix_grants: Any, tmp_path: Path
) -> None:
    main = _with_cursor_tools(tmp_path, sorted(CURSOR_TOOLS - {"list_proposals"}))
    findings = _cursor_findings(analysis, main, _known_functions(matrix_grants))
    assert findings and all(
        f.startswith("MISSING GRANT") and " list_proposals " in f for f in findings
    ), findings


def test_an_unreadable_cursor_flag_expression_is_refused(
    matrix_grants: Any, tmp_path: Path
) -> None:
    infra = tmp_path / "infra"
    infra.mkdir()
    for path in MAIN_TF.parent.glob("*.tf"):
        shutil.copy(path, infra / path.name)
    main = infra / "main.tf"
    text = main.read_text(encoding="utf-8")
    old = "cursor_kms_grant_enabled = contains(local.cursor_tools, each.key)"
    assert text.count(old) == 1
    main.write_text(text.replace(old, "cursor_kms_grant_enabled = true"), encoding="utf-8")
    with pytest.raises(ValueError, match="cursor_kms_grant_enabled"):
        gate.load_cursor_enabled_functions(main, _known_functions(matrix_grants))
