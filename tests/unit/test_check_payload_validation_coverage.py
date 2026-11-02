# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/check_payload_validation_coverage.py`.

Mutation cases edit a copy of `src/` in `tmp_path`, never the repository tree.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_payload_validation_coverage.py"

_SPEC = importlib.util.spec_from_file_location("check_payload_validation_coverage", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["check_payload_validation_coverage"] = gate
_SPEC.loader.exec_module(gate)

_SUBMIT_CALL = "    validate_proposal_payload(target_table, after_payload)\n"
_APPROVE_CALL = "    validate_proposal_payload(target_table, stored_payload)\n"


#: Names excluded from the `src/` copy; the gate parses only first-party Python modules.
#: `src/relay/build` is written and removed concurrently by `scripts/package_relay.sh`.
_EXCLUDED_FROM_COPY = (
    "node_modules",  # the gate parses no JavaScript
    "build",  # compiled TypeScript, written and removed by the relay packaging tests
    "__pycache__",
    "*.egg-info",  # packaging metadata, never a first-party module
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
)


@pytest.fixture
def scratch_src(tmp_path: Path) -> Path:
    """A throwaway copy of `src/` the mutation tests edit instead of the repository tree."""
    destination = tmp_path / "src"
    shutil.copytree(
        REPO_ROOT / "src",
        destination,
        ignore=shutil.ignore_patterns(*_EXCLUDED_FROM_COPY),
    )
    return destination


def _mutate(scratch_src: Path, old: str, new: str) -> None:
    path = scratch_src / "crm" / "proposals.py"
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"fixture assumption broken: {old!r} appears {text.count(old)}x"
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_the_real_repository_source_passes_the_gate() -> None:
    assert gate.findings_for(gate.analyse()) == []


def test_both_payload_persisting_functions_carry_an_obligation() -> None:
    coverage = gate.analyse()
    obligated = {(o.module, o.function) for o in coverage.obligations}
    assert obligated >= gate.EXPECTED_OBLIGATIONS
    triggers = {(o.function, o.trigger) for o in coverage.obligations}
    assert ("submit_proposal", "TAINTED_PERSIST") in triggers
    assert ("submit_proposal", "PROPOSAL_ITEM") in triggers
    assert ("approve_proposal", "APPLY_CALL") in triggers


def test_an_unmutated_copy_passes_the_gate(scratch_src: Path) -> None:
    """An unmutated scratch copy of `src/` produces no findings."""
    assert gate.findings_for(gate.analyse(scratch_src)) == []


def test_deleting_the_submit_time_call_fails_the_gate(scratch_src: Path) -> None:
    _mutate(scratch_src, _SUBMIT_CALL, "")
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("submit_proposal" in finding for finding in findings), findings
    assert all("approve_proposal" not in finding for finding in findings), findings


def test_deleting_the_approve_time_call_fails_the_gate(scratch_src: Path) -> None:
    _mutate(scratch_src, _APPROVE_CALL, "")
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("approve_proposal" in finding and "APPLY_CALL" in finding for finding in findings), (
        findings
    )


def test_deleting_both_calls_fails_the_gate_for_both_functions(scratch_src: Path) -> None:
    _mutate(scratch_src, _SUBMIT_CALL, "")
    _mutate(scratch_src, _APPROVE_CALL, "")
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("submit_proposal" in finding for finding in findings), findings
    assert any("approve_proposal" in finding for finding in findings), findings


def test_a_conditional_call_does_not_satisfy_the_gate(scratch_src: Path) -> None:
    """A validation call nested under an `if` is reported."""
    _mutate(
        scratch_src,
        _SUBMIT_CALL,
        '    if operation == "update":\n        validate_proposal_payload'
        "(target_table, after_payload)\n",
    )
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("submit_proposal" in finding for finding in findings), findings


def test_a_call_placed_after_the_sink_does_not_satisfy_the_gate(scratch_src: Path) -> None:
    """A validation call placed after the persist is reported."""
    _mutate(scratch_src, _SUBMIT_CALL, "")
    path = scratch_src / "crm" / "proposals.py"
    text = path.read_text(encoding="utf-8")
    anchor = "    return proposal_item\n"
    assert text.count(anchor) == 1
    path.write_text(text.replace(anchor, _SUBMIT_CALL + anchor), encoding="utf-8")
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("submit_proposal" in finding for finding in findings), findings


def test_a_pinned_obligation_the_pass_cannot_see_is_reported_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned obligation the analysis cannot find is reported as `ANALYSIS BLIND`."""
    monkeypatch.setattr(
        gate, "EXPECTED_OBLIGATIONS", frozenset({("crm.proposals", "no_such_function")})
    )
    findings = gate.findings_for(gate.analyse())
    assert any("ANALYSIS BLIND" in finding for finding in findings), findings


def test_the_unguarded_exception_enumeration_is_empty_today() -> None:
    assert gate.DECLARED_UNGUARDED_SINKS == ()


def test_a_declared_exception_suppresses_only_its_own_obligation(
    monkeypatch: pytest.MonkeyPatch, scratch_src: Path
) -> None:
    """A declared exception suppresses only its own (module, function, trigger) obligation."""
    declaration: Any = gate.UnguardedSink(
        "crm.proposals", "submit_proposal", "PROPOSAL_ITEM", "test-only"
    )
    monkeypatch.setattr(gate, "DECLARED_UNGUARDED_SINKS", (declaration,))
    _mutate(scratch_src, _SUBMIT_CALL, "")
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert all("PROPOSAL_ITEM" not in finding for finding in findings), findings
    assert any("TAINTED_PERSIST" in finding for finding in findings), findings


def test_a_stale_declared_exception_is_a_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    declaration: Any = gate.UnguardedSink("crm.nowhere", "gone", "APPLY_CALL", "test-only")
    monkeypatch.setattr(gate, "DECLARED_UNGUARDED_SINKS", (declaration,))
    findings = gate.findings_for(gate.analyse())
    assert any("STALE UNGUARDED DECLARATION" in finding for finding in findings), findings


def test_a_locally_shadowed_validator_name_is_reported(scratch_src: Path) -> None:
    """A same-named local stub in place of the imported validator is reported."""
    path = scratch_src / "crm" / "proposals.py"
    text = path.read_text(encoding="utf-8")
    old = "from crm.domain.allowlists import validate_proposal_payload\n"
    assert text.count(old) == 1
    stub = (
        "def validate_proposal_payload(table: str, after_payload: object) -> None:\n"
        "    del table, after_payload\n"
    )
    path.write_text(text.replace(old, stub), encoding="utf-8")
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("is not imported from crm.domain.allowlists" in finding for finding in findings), (
        findings
    )


def test_main_exits_zero_on_real_source_and_one_on_a_mutated_copy(scratch_src: Path) -> None:
    assert gate.main([]) == 0
    _mutate(scratch_src, _APPROVE_CALL, "")
    assert gate.main(["--src", str(scratch_src)]) == 1


# ---------------------------------------------------------------------------
# Payload writers added as new modules in the scratch copy.
# ---------------------------------------------------------------------------

_EVASION_COMPUTED_KEY = '''# SPDX-License-Identifier: MIT
"""A payload writer that fetches the payload under a computed key and merges it inline."""

from __future__ import annotations

from typing import Any

from crm.store.keys import derive
from crm.store.store import Store


def apply_intended(
    store: Store, table: str, record: dict[str, Any], proposal: dict[str, Any], now: str
) -> None:
    intended = proposal.get("after" + "_payload") or {}
    merged = derive(table, {**record, **intended, "updated_at": now})
    store.put_item(table, merged)
'''

_EVASION_RENAMED_PARAMETER = '''# SPDX-License-Identifier: MIT
"""A payload writer whose payload arrives as a parameter not named `after_payload`."""

from __future__ import annotations

from typing import Any

from crm.store.keys import derive
from crm.store.store import Store


def apply_intended_values(
    store: Store, table: str, before: dict[str, Any], intended: dict[str, Any], now: str
) -> None:
    store.put_item(table, derive(table, {**dict(before), **dict(intended), "updated_at": now}))
'''


def _add_module(scratch_src: Path, name: str, source: str) -> None:
    (scratch_src / "crm" / name).write_text(source, encoding="utf-8")


def test_a_writer_fetching_the_payload_under_a_computed_key_is_a_finding(
    scratch_src: Path,
) -> None:
    """A payload fetched under a constant-folded computed key is a finding."""
    _add_module(scratch_src, "evader_computed_key.py", _EVASION_COMPUTED_KEY)
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("crm.evader_computed_key.apply_intended" in f for f in findings), findings
    assert any("TAINTED_PERSIST" in f and "evader_computed_key" in f for f in findings), findings


def test_a_writer_taking_the_payload_as_a_renamed_parameter_is_a_finding(
    scratch_src: Path,
) -> None:
    """A payload passed as a parameter not named `after_payload` is a `MERGE_PERSIST` finding."""
    _add_module(scratch_src, "evader_renamed.py", _EVASION_RENAMED_PARAMETER)
    findings = gate.findings_for(gate.analyse(scratch_src))
    matching = [f for f in findings if "crm.evader_renamed.apply_intended_values" in f]
    assert matching, findings
    assert all("MERGE_PERSIST" in f for f in matching), matching


def test_both_evasions_together_are_findings_and_the_gate_exits_one(scratch_src: Path) -> None:
    _add_module(scratch_src, "evader_computed_key.py", _EVASION_COMPUTED_KEY)
    _add_module(scratch_src, "evader_renamed.py", _EVASION_RENAMED_PARAMETER)
    assert gate.main(["--src", str(scratch_src)]) == 1


def test_the_direct_update_path_is_covered_and_guarded_by_its_own_validator() -> None:
    """The `update_*` handlers' `MERGE_PERSIST` obligations are met by `validate_update_fields`."""
    coverage = gate.analyse()
    triggers = {(o.module, o.function, o.trigger) for o in coverage.obligations}
    for module in (
        "crm.handlers.update_account",
        "crm.handlers.update_contact",
        "crm.handlers.update_opportunity",
        "crm.handlers.update_activity",
    ):
        function = module.replace("crm.handlers.", "_")
        assert (module, function, "MERGE_PERSIST") in triggers, sorted(triggers)
        guards = coverage.guards[(module, function)]
        assert [g.name for g in guards] == [gate.UPDATE_VALIDATOR_NAME], guards
    assert gate.findings_for(coverage) == []


def test_deleting_an_update_handlers_validator_call_fails_the_gate(scratch_src: Path) -> None:
    """Deleting an update handler's `validate_update_fields` call is reported."""
    path = scratch_src / "crm" / "handlers" / "update_contact.py"
    text = path.read_text(encoding="utf-8")
    call = "    validate_update_fields(_TABLE, fields)\n"
    assert text.count(call) == 1
    path.write_text(text.replace(call, ""), encoding="utf-8")
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("update_contact" in f and "MERGE_PERSIST" in f for f in findings), findings


def test_a_proposal_obligation_is_not_satisfied_by_the_update_validator(
    scratch_src: Path,
) -> None:
    """`validate_update_fields` does not discharge a proposal-payload obligation."""
    _mutate(
        scratch_src,
        _SUBMIT_CALL,
        "    validate_update_fields(target_table, after_payload)\n",
    )
    path = scratch_src / "crm" / "proposals.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "from crm.domain.allowlists import validate_proposal_payload",
            "from crm.domain.allowlists import validate_proposal_payload, validate_update_fields",
        ),
        encoding="utf-8",
    )
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("submit_proposal" in f and "TAINTED_PERSIST" in f for f in findings), findings


def test_a_single_unpack_record_stamp_is_not_an_obligation() -> None:
    """A single-unpack record stamp such as `{**record, "last_touch": ...}` is not an obligation."""
    coverage = gate.analyse()
    assert not [
        o
        for o in coverage.obligations
        if o.trigger == "MERGE_PERSIST" and o.module == "crm.handlers.log_activity"
    ], coverage.obligations


def test_a_reexported_validator_binding_is_resolved_through_the_index(scratch_src: Path) -> None:
    """Repointing `crm.domain`'s validator re-export at another function is reported."""
    path = scratch_src / "crm" / "domain" / "__init__.py"
    text = path.read_text(encoding="utf-8")
    assert "validate_update_fields," in text
    path.write_text(
        text.replace(
            "    validate_update_fields,\n",
            "    is_known_table as validate_update_fields,\n",
        ),
        encoding="utf-8",
    )
    findings = gate.findings_for(gate.analyse(scratch_src))
    assert any("does not re-export it from crm.domain.allowlists" in f for f in findings), findings


# ---------------------------------------------------------------------------
# Generalised merge forms. Each case adds a writer module to a scratch `src/` tree.
# ---------------------------------------------------------------------------

_WRITER_PREAMBLE = '''# SPDX-License-Identifier: MIT
"""A payload writer added by a test: merges caller input into a record and persists it."""

from __future__ import annotations

from typing import Any

from crm.store.store import Store

'''

#: `name -> function body`. Every one merges a caller-supplied mapping into a fetched record and
#: reaches `put_item` with no validator call, so every one must be a `MERGE_PERSIST` finding.
_UNGUARDED_WRITER_FORMS: dict[str, str] = {
    "dict_copy_then_update": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = dict(record)
    merged.update(intended)
    store.put_item("opportunities", merged)
""",
    "pep_584_or_operator": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = record | intended
    store.put_item("opportunities", merged)
""",
    "one_unpack_plus_subscripts": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = {**record, "name": intended["name"], "stage": intended["stage"]}
    store.put_item("opportunities", merged)
""",
    "merge_in_helper_persist_in_caller": """

def _combine(record: dict[str, Any], intended: dict[str, Any]) -> dict[str, Any]:
    return {**record, **intended}


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    store.put_item("opportunities", _combine(record, intended))
""",
    "augmented_or_assignment": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    record |= intended
    store.put_item("opportunities", record)
""",
    "dict_constructor_merge": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    store.put_item("opportunities", dict(record, **intended))
""",
    "one_unpack_plus_get": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = {**record, "name": intended.get("name")}
    store.put_item("opportunities", merged)
""",
    "two_hop_helper_chain": """

def _inner(record: dict[str, Any], intended: dict[str, Any]) -> dict[str, Any]:
    return {**record, **intended}


def _outer(record: dict[str, Any], intended: dict[str, Any]) -> dict[str, Any]:
    return _inner(record, intended)


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    store.put_item("opportunities", _outer(record, intended))
""",
    "key_by_key_loop": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = dict(record)
    for key, value in intended.items():
        merged[key] = value
    store.put_item("opportunities", merged)
""",
    "dict_comprehension_merge": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    store.put_item(
        "opportunities",
        {k: intended.get(k, v) for k, v in record.items()},
    )
""",
    "transact_write_sink": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    store.transact_write([{"Put": {**record, **intended}}], transaction_key="k")
""",
    # Per-key and iterator-based merge forms.
    "copy_then_per_key_subscript": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = dict(record)
    merged["stage"] = intended["stage"]
    merged["name"] = intended["name"]
    store.put_item("opportunities", merged)
""",
    "for_key_in_mapping_loop": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = dict(record)
    for key in intended:
        merged[key] = intended[key]
    store.put_item("opportunities", merged)
""",
    "dict_of_chained_items": """

import itertools


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = dict(itertools.chain(record.items(), intended.items()))
    store.put_item("opportunities", merged)
""",
    # Further constructed forms.
    "dict_of_concatenated_item_lists": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = dict(list(record.items()) + list(intended.items()))
    store.put_item("opportunities", merged)
""",
    "dict_of_flattened_chainmap": """

import collections


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = dict(collections.ChainMap(intended, record))
    store.put_item("opportunities", merged)
""",
    "deepcopy_then_per_key_get": """

import copy


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = copy.deepcopy(record)
    merged["stage"] = intended.get("stage")
    store.put_item("opportunities", merged)
""",
    "zip_keys_and_values_loop": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = dict(record)
    for key, value in zip(intended.keys(), intended.values(), strict=True):
        merged[key] = value
    store.put_item("opportunities", merged)
""",
    "reduce_over_two_mappings": """

import functools


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = functools.reduce(lambda a, b: {**a, **b}, [record, intended])
    store.put_item("opportunities", merged)
""",
    "copy_method_then_update_unpacked": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = record.copy()
    merged.update(**intended)
    store.put_item("opportunities", merged)
""",
    "merge_into_a_mapping_held_on_an_attribute": """

class _Holder:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def write_intended(store: Store, holder: _Holder, intended: dict[str, Any]) -> None:
    holder.data["stage"] = intended["stage"]
    store.put_item("opportunities", holder.data)
""",
    "merge_bound_through_a_tuple_target": """

def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged, _spare = {**record, **intended}, None
    store.put_item("opportunities", merged)
""",
}

#: Forms that must NOT be obligations: no second caller-supplied mapping is involved.
_INNOCENT_WRITER_FORMS: dict[str, str] = {
    "single_unpack_computed_stamp": """

def write_intended(store: Store, record: dict[str, Any], now: str) -> None:
    store.put_item("opportunities", {**record, "last_touch": now, "updated_at": now})
""",
    "bare_dict_copy": """

def write_intended(store: Store, record: dict[str, Any]) -> None:
    store.put_item("opportunities", dict(record))
""",
    "integer_bitwise_or": """

def write_intended(store: Store, record: dict[str, Any], flags: int) -> None:
    record["flags"] = flags | 4
    store.put_item("opportunities", record)
""",
    "fixed_key_write_in_a_loop": """

def write_intended(store: Store, record: dict[str, Any], stamps: dict[str, Any]) -> None:
    merged = dict(record)
    for _name, _spec in stamps.items():
        merged["updated_at"] = "now"
    store.put_item("opportunities", merged)
""",
    "computed_value_written_per_key": """

def write_intended(store: Store, record: dict[str, Any], now: str) -> None:
    merged = dict(record)
    merged["updated_at"] = now
    merged["etag"] = str(len(record))
    store.put_item("opportunities", merged)
""",
    "attribute_mapping_stamped_with_computed_values": """

class _Holder:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def write_intended(store: Store, holder: _Holder, now: str) -> None:
    holder.data["updated_at"] = now
    store.put_item("opportunities", holder.data)
""",
    "single_mapping_through_a_helper": """

def write_intended(store: Store, record: dict[str, Any]) -> None:
    store.put_item("opportunities", dict(sorted(record.items())))
""",
}


def _findings_for_writer(scratch_src: Path, name: str, body: str) -> list[str]:
    _add_module(scratch_src, f"writer_{name}.py", _WRITER_PREAMBLE + body)
    findings = gate.findings_for(gate.analyse(scratch_src))
    return [f for f in findings if f"writer_{name}" in f]


@pytest.mark.parametrize("name,body", sorted(_UNGUARDED_WRITER_FORMS.items()))
def test_an_unguarded_merge_form_is_a_finding(scratch_src: Path, name: str, body: str) -> None:
    matching = _findings_for_writer(scratch_src, name, body)
    assert matching, f"{name}: a merge reaching put_item unvalidated produced no finding"
    assert all("MERGE_PERSIST" in f for f in matching), matching


@pytest.mark.parametrize("name,body", sorted(_INNOCENT_WRITER_FORMS.items()))
def test_a_form_with_no_second_mapping_is_not_an_obligation(
    scratch_src: Path, name: str, body: str
) -> None:
    assert _findings_for_writer(scratch_src, name, body) == [], name


def test_a_guarded_merge_form_satisfies_the_gate(scratch_src: Path) -> None:
    """A merge writer that validates the caller mapping unconditionally first is not a finding."""
    body = """

from crm.domain.allowlists import validate_update_fields


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    validate_update_fields("opportunities", intended)
    merged = dict(record)
    merged.update(intended)
    store.put_item("opportunities", merged)
"""
    assert _findings_for_writer(scratch_src, "guarded_update_form", body) == []


def test_a_conditional_guard_does_not_satisfy_a_generalised_merge_form(
    scratch_src: Path,
) -> None:
    """A guard nested in an `if` does not satisfy a generalised merge obligation."""
    body = """

from crm.domain.allowlists import validate_update_fields


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    if intended:
        validate_update_fields("opportunities", intended)
    merged = record | intended
    store.put_item("opportunities", merged)
"""
    matching = _findings_for_writer(scratch_src, "conditional_guard_form", body)
    assert matching, "a conditional guard satisfied a generalised merge obligation"


def test_the_generalised_trigger_needs_no_new_exception() -> None:
    """`DECLARED_UNGUARDED_SINKS` is empty and the real source produces no findings."""
    assert gate.DECLARED_UNGUARDED_SINKS == ()
    assert gate.findings_for(gate.analyse()) == []


def test_merge_producer_discovery_reaches_a_fixpoint() -> None:
    """Merge-producer discovery returns a frozenset that excludes `derive`."""
    build_index = gate._load_index_builder()
    producers = gate.find_merge_producers(build_index(REPO_ROOT / "src"))
    assert isinstance(producers, frozenset)
    assert "derive" not in producers, (
        "a bare dict() copy plus computed keys must not register as a mapping merge, or every "
        "normaliser in the codebase becomes an obligation"
    )


# ---------------------------------------------------------------------------
# Stated boundary: a third-party callable that merges two mappings and is neither `dict(...)`, `|`,
# `.update()`, a comprehension, a subscript write nor a first-party merge helper is NOT recognised.
# ---------------------------------------------------------------------------


def test_a_third_party_merge_callable_is_a_stated_boundary(scratch_src: Path) -> None:
    body = """

import operator


def write_intended(store: Store, record: dict[str, Any], intended: dict[str, Any]) -> None:
    merged = operator.or_(record, intended)
    store.put_item("opportunities", merged)
"""
    assert _findings_for_writer(scratch_src, "operator_or_call", body) == [], (
        "this form is a declared boundary; if it is now caught, delete this test and update the "
        "MERGE_PERSIST docstring's stated boundary rather than leaving the claim understated"
    )
