# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_page_context_no_rows.py`.

Violations are planted in an in-memory schema dict or in a scratch copy of the module.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_page_context_no_rows.py"
MODULE_PATH = REPO_ROOT / "src" / "crm" / "toolspec" / "page_context.py"


def _load_module(name: str, path: Path) -> ModuleType:
    """Import a script from `scripts/` by file path and return the module."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_page_context_no_rows", GATE)
run_gates = _load_module("run_gates_for_test", REPO_ROOT / "scripts" / "run_gates.py")


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(GATE), *args],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# The clean tree, and the skip disposition.
# ---------------------------------------------------------------------------


def test_passes_on_the_real_page_context_module() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
    assert "EntityPageContext" in result.stdout
    assert "OpaquePageContext" in result.stdout


def test_skips_with_exit_three_when_the_module_is_absent(tmp_path: Path) -> None:
    """A missing page-context module makes the gate exit 3 with a SKIP message."""
    result = _run("--module-path", str(tmp_path / "nonexistent.py"))
    assert result.returncode == gate.SKIP_EXIT_STATUS
    assert result.returncode == 3
    assert "SKIP" in result.stdout
    assert "Nothing was verified" in result.stdout


def test_skip_status_matches_run_gates_contract() -> None:
    """The gate's skip status equals `run_gates.py`'s skip status."""
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS


# ---------------------------------------------------------------------------
# Schema walk.
# ---------------------------------------------------------------------------


class TestSchemaWalk:
    def test_rejects_an_added_items_array_of_records(self) -> None:
        """An `items` array of records on a variant is rejected."""
        schema = {
            "oneOf": [
                {
                    "type": "object",
                    "title": "ListPageContext",
                    "properties": {
                        "route": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "title": "Record",
                                "properties": {"id": {"type": "string"}},
                            },
                        },
                    },
                }
            ]
        }
        violations = gate.find_schema_violations(schema)
        assert len(violations) == 1
        assert "items" in violations[0]
        assert "never carry its rows" in violations[0]

    def test_rejects_an_array_of_records_nested_deep_in_a_variant(self) -> None:
        """An array of records nested inside a variant property is rejected."""
        schema = {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "object",
                            "properties": {
                                "results": {
                                    "type": "array",
                                    "items": {"properties": {"id": {"type": "string"}}},
                                }
                            },
                        }
                    },
                }
            ]
        }
        violations = gate.find_schema_violations(schema)
        assert len(violations) == 1
        assert "results" in violations[0]

    def test_rejects_an_unconstrained_array(self) -> None:
        """An array with no `items` constraint is rejected."""
        violations = gate.find_schema_violations({"properties": {"rows": {"type": "array"}}})
        assert len(violations) == 1

    def test_rejects_an_array_whose_items_are_a_ref(self) -> None:
        violations = gate.find_schema_violations(
            {"properties": {"rows": {"type": "array", "items": {"$ref": "#/defs/Record"}}}}
        )
        assert len(violations) == 1

    def test_rejects_a_nullable_array_of_records(self) -> None:
        """A nullable array of records is rejected."""
        violations = gate.find_schema_violations(
            {
                "properties": {
                    "rows": {
                        "type": ["array", "null"],
                        "items": {"type": "object", "properties": {}},
                    }
                }
            }
        )
        assert len(violations) == 1

    def test_allows_an_array_of_scalars(self) -> None:
        """An array of scalars is allowed."""
        violations = gate.find_schema_violations(
            {"properties": {"visible_fields": {"type": "array", "items": {"type": "string"}}}}
        )
        assert violations == []

    def test_allows_the_scalar_valued_filters_map(self) -> None:
        """A mapping whose values are scalars is allowed."""
        violations = gate.find_schema_violations(
            {
                "properties": {
                    "filters": {
                        "type": "object",
                        "additionalProperties": {
                            "type": ["string", "integer", "number", "boolean"]
                        },
                    }
                }
            }
        )
        assert violations == []

    def test_a_word_named_items_in_an_enum_is_not_a_violation(self) -> None:
        """An enum value named `items` is not a violation."""
        violations = gate.find_schema_violations(
            {"properties": {"kind": {"type": "string", "enum": ["items", "array"]}}}
        )
        assert violations == []


# ---------------------------------------------------------------------------
# Annotation walk.
# ---------------------------------------------------------------------------


class TestAnnotationWalk:
    def test_rejects_a_list_of_a_record_dataclass(self) -> None:
        violations = gate.find_annotation_violations(
            "class ListPageContext:\n    route: str\n    items: list[Record]\n"
        )
        assert len(violations) == 1
        assert "ListPageContext.items" in violations[0]

    def test_rejects_a_tuple_of_records(self) -> None:
        violations = gate.find_annotation_violations("class C:\n    rows: tuple[Record, ...]\n")
        assert len(violations) == 1

    def test_rejects_a_sequence_of_records(self) -> None:
        violations = gate.find_annotation_violations("class C:\n    rows: Sequence[Record]\n")
        assert len(violations) == 1

    def test_rejects_an_optional_list_of_records(self) -> None:
        violations = gate.find_annotation_violations(
            "class C:\n    rows: list[Record] | None = None\n"
        )
        assert len(violations) == 1

    def test_rejects_a_mapping_whose_values_are_a_list_of_records(self) -> None:
        """A mapping whose values are lists of records is rejected."""
        violations = gate.find_annotation_violations(
            "class C:\n    grouped: Mapping[str, list[Record]]\n"
        )
        assert len(violations) == 1

    def test_allows_a_list_of_strings(self) -> None:
        violations = gate.find_annotation_violations("class C:\n    fields: list[str]\n")
        assert violations == []

    def test_allows_the_real_variant_annotations(self) -> None:
        violations = gate.find_annotation_violations(MODULE_PATH.read_text(encoding="utf-8"))
        assert violations == []


# ---------------------------------------------------------------------------
# End to end over a scratch copy of the module.
# ---------------------------------------------------------------------------


def test_gate_fails_end_to_end_on_a_module_with_an_items_field(tmp_path: Path) -> None:
    """The gate script exits 1 on a module copy with an added `items` list field."""
    scratch = tmp_path / "page_context.py"
    shutil.copyfile(MODULE_PATH, scratch)
    text = scratch.read_text(encoding="utf-8")
    needle = "    route: str\n    entity_type: EntityType\n    entity_id: str\n"
    assert needle in text, "fixture setup did not find the EntityPageContext field block"
    mutated = text.replace(needle, needle + "    items: list[SortDescriptor]\n", 1)
    assert mutated != text
    scratch.write_text(mutated, encoding="utf-8")

    result = _run("--module-path", str(scratch))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "VIOLATION" in result.stderr
    assert "items" in result.stderr


@pytest.mark.parametrize("annotation", ["list[Record]", "tuple[Record, ...]", "Sequence[Record]"])
def test_every_sequence_form_is_caught(annotation: str) -> None:
    violations = gate.find_annotation_violations(f"class C:\n    rows: {annotation}\n")
    assert len(violations) == 1, f"{annotation} was not caught"


# ---------------------------------------------------------------------------
# Cross-check against the schema module's `array_of_record_paths` predicate.
# ---------------------------------------------------------------------------


class TestOwnerPredicateCrossCheck:
    def test_the_owner_predicate_is_available_and_used(self) -> None:
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert "array_of_record_paths" in result.stdout, (
            "the gate should name the owner predicate when it cross-checks against it, so a reader "
            "can tell whether two implementations or only one backed the pass"
        )

    def test_the_two_implementations_agree_on_the_real_schema(self) -> None:
        """Both the gate walk and the owner predicate find no violation in the real schema."""
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from crm.toolspec.page_context import array_of_record_paths, render_json_schema

        schema = render_json_schema()
        assert gate.find_schema_violations(schema) == []
        assert array_of_record_paths(schema) == []

    def test_the_two_implementations_agree_on_a_planted_violation(self) -> None:
        """Both the gate walk and the owner predicate flag the same planted violation."""
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from crm.toolspec.page_context import array_of_record_paths

        offending = {
            "oneOf": [
                {
                    "type": "object",
                    "title": "ListPageContext",
                    "properties": {
                        "route": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"id": {"type": "string"}}},
                        },
                    },
                }
            ]
        }
        assert gate.find_schema_violations(offending), "this gate's walk missed it"
        assert array_of_record_paths(offending), "the owner predicate missed it"

    def test_the_gate_still_works_without_the_owner_predicate(self, tmp_path: Path) -> None:
        """The gate reads the owner predicate via `getattr` with a `None` default."""
        source = MODULE_PATH.read_text(encoding="utf-8")
        assert "def array_of_record_paths" in source
        assert 'getattr(page_context, "array_of_record_paths", None)' in (
            (REPO_ROOT / "scripts" / "check_page_context_no_rows.py").read_text(encoding="utf-8")
        )
