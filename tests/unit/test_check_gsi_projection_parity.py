# SPDX-License-Identifier: MIT
"""Unit tests for ``scripts/check_gsi_projection_parity.py``: the tracked ``tables.tf`` passes, and
each kind of drift against ``crm.store.schema.TABLES`` exits 1 naming its subject.

Mutations run on a copy of ``tables.tf`` under ``tmp_path``, never on the tracked file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import scripts.check_gsi_projection_parity as gate

from crm.store.schema import TABLES

_OWNER_GSI_ATTRS = '      "account_id", "owner_rep_id", "name", "tenant",\n'


def _mutated_copy(tmp_path: Path, old: str, new: str) -> Path:
    """Write ``tables.tf`` with exactly one occurrence of ``old`` replaced by ``new``."""
    text = gate.DEFAULT_TABLES_TF.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"fixture anchor {old!r} is not unique in tables.tf"
    target = tmp_path / "tables.tf"
    target.write_text(text.replace(old, new), encoding="utf-8")
    return target


def test_the_tracked_tables_tf_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main([]) == 0
    assert "PASS" in capsys.readouterr().out


def test_an_unmodified_copy_passes(tmp_path: Path) -> None:
    copy = tmp_path / "tables.tf"
    copy.write_text(gate.DEFAULT_TABLES_TF.read_text(encoding="utf-8"), encoding="utf-8")
    assert gate.main(["--tables-tf", str(copy)]) == 0


def test_a_projection_omitting_tenant_fails_naming_the_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mutated = _mutated_copy(
        tmp_path, _OWNER_GSI_ATTRS, '      "account_id", "owner_rep_id", "name",\n'
    )
    assert gate.main(["--tables-tf", str(mutated)]) == 1
    out = capsys.readouterr().out
    assert "accounts.gsi_owner: projection drift" in out
    assert "missing from tables.tf: ['tenant']" in out


def test_an_extra_projected_attribute_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mutated = _mutated_copy(
        tmp_path, _OWNER_GSI_ATTRS, '      "account_id", "owner_rep_id", "name", "tenant", "x",\n'
    )
    assert gate.main(["--tables-tf", str(mutated)]) == 1
    assert "extra in tables.tf (not in schema.py): ['x']" in capsys.readouterr().out


def test_a_renamed_gsi_fails_as_missing_and_extra(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mutated = _mutated_copy(tmp_path, 'name            = "gsi_owner"', 'name = "gsi_owner_x"')
    assert gate.main(["--tables-tf", str(mutated)]) == 1
    out = capsys.readouterr().out
    assert "accounts: GSI(s) declared in schema.py but absent from tables.tf: ['gsi_owner']" in out
    assert "accounts: GSI(s) present in tables.tf but absent from schema.py: ['gsi_owner_x']" in out


def test_a_missing_table_fails_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mutated = _mutated_copy(
        tmp_path,
        'resource "aws_dynamodb_table" "accounts" {',
        'resource "aws_dynamodb_table" "accounts_renamed" {',
    )
    assert gate.main(["--tables-tf", str(mutated)]) == 1
    out = capsys.readouterr().out
    assert "not found in tables.tf: ['accounts']" in out
    assert "not declared in crm.store.schema.TABLES: ['accounts_renamed']" in out


def test_input_with_no_table_blocks_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    malformed = tmp_path / "tables.tf"
    malformed.write_text("this is not terraform\n", encoding="utf-8")
    assert gate.main(["--tables-tf", str(malformed)]) == 1
    out = capsys.readouterr().out
    for table in sorted(TABLES):
        assert repr(table) in out


def test_a_missing_tables_tf_is_an_environment_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main(["--tables-tf", str(tmp_path / "absent.tf")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_parser_reads_every_projection_of_the_tracked_file() -> None:
    projections = gate.parse_terraform_gsi_projections(
        gate.DEFAULT_TABLES_TF.read_text(encoding="utf-8")
    )
    assert set(projections) == set(TABLES)
    assert projections["accounts"]["gsi_owner"] == frozenset(
        {"account_id", "owner_rep_id", "name", "tenant"}
    )
