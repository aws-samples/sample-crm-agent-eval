#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Drift gate: every Terraform GSI projection must match `crm.store.schema.TABLES` exactly.

`src/crm/store/schema.py` is the source of truth for every table's global secondary indexes.
`infra/modules/dynamodb/tables.tf` transcribes each `global_secondary_index` block's
`non_key_attributes` by hand; this gate holds that transcription to the Python schema.

It parses `tables.tf` with a narrow regex-based parser and asserts, for every
`(table, index_name)` pair in `crm.store.schema.TABLES`, that Terraform's `non_key_attributes` set
equals Python's `projected_attributes` set exactly, and that every GSI in Terraform is accounted
for in Python and vice versa.

Usage: `python3 scripts/check_gsi_projection_parity.py [--tables-tf PATH]`

Exit codes: 0 pass; 1 drift detected; 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crm.store.schema import TABLES  # noqa: E402

DEFAULT_TABLES_TF = REPO_ROOT / "infra" / "modules" / "dynamodb" / "tables.tf"

#: Matches one `resource "aws_dynamodb_table" "<name>" { ... }` block, non-greedily up to the first
#: closing brace at column 0.
_TABLE_BLOCK_RE = re.compile(
    r'resource\s+"aws_dynamodb_table"\s+"(?P<logical_name>\w+)"\s*\{(?P<body>.*?)\n\}\n',
    re.DOTALL,
)

#: Matches one `global_secondary_index { ... }` block within a table block body.
_GSI_BLOCK_RE = re.compile(r"global_secondary_index\s*\{(?P<body>.*?)\n  \}", re.DOTALL)

_GSI_NAME_RE = re.compile(r'name\s*=\s*"([^"]+)"')

#: Matches the `non_key_attributes = [ ... ]` list, across one or more lines, up to its closing
#: bracket. Only quoted-string items are extracted.
_NON_KEY_ATTRS_RE = re.compile(r"non_key_attributes\s*=\s*\[(?P<items>.*?)\]", re.DOTALL)

_QUOTED_ITEM_RE = re.compile(r'"([^"]+)"')


def parse_terraform_gsi_projections(tables_tf_text: str) -> dict[str, dict[str, frozenset[str]]]:
    """Parse every table's GSI `non_key_attributes` set out of `tables.tf`'s raw text.

    Returns:
        `{logical_table_name: {gsi_name: frozenset(non_key_attributes)}}`. A GSI with no
        `non_key_attributes` block (e.g. `projection_type = "ALL"`) is recorded with an empty
        frozenset, which fails parity against any Python GSI.
    """
    result: dict[str, dict[str, frozenset[str]]] = {}
    for table_match in _TABLE_BLOCK_RE.finditer(tables_tf_text):
        logical_name = table_match.group("logical_name")
        body = table_match.group("body")
        gsis: dict[str, frozenset[str]] = {}
        for gsi_match in _GSI_BLOCK_RE.finditer(body):
            gsi_body = gsi_match.group("body")
            name_match = _GSI_NAME_RE.search(gsi_body)
            if name_match is None:
                continue
            gsi_name = name_match.group(1)
            attrs_match = _NON_KEY_ATTRS_RE.search(gsi_body)
            attrs = (
                frozenset(_QUOTED_ITEM_RE.findall(attrs_match.group("items")))
                if attrs_match is not None
                else frozenset()
            )
            gsis[gsi_name] = attrs
        result[logical_name] = gsis
    return result


def check_parity(
    terraform_projections: dict[str, dict[str, frozenset[str]]],
) -> list[str]:
    """Compare `terraform_projections` against `crm.store.schema.TABLES` and return violations."""
    violations: list[str] = []

    python_table_names = set(TABLES)
    terraform_table_names = set(terraform_projections)

    missing_tables = sorted(python_table_names - terraform_table_names)
    if missing_tables:
        violations.append(
            f"tables declared in crm.store.schema.TABLES but not found in tables.tf: {missing_tables}"
        )
    extra_tables = sorted(terraform_table_names - python_table_names)
    if extra_tables:
        violations.append(
            f"tables found in tables.tf but not declared in crm.store.schema.TABLES: {extra_tables}"
        )

    for table_name in sorted(python_table_names & terraform_table_names):
        python_gsis = TABLES[table_name].gsis
        terraform_gsis = terraform_projections[table_name]

        python_gsi_names = set(python_gsis)
        terraform_gsi_names = set(terraform_gsis)

        missing_gsis = sorted(python_gsi_names - terraform_gsi_names)
        if missing_gsis:
            violations.append(
                f"{table_name}: GSI(s) declared in schema.py but absent from tables.tf: {missing_gsis}"
            )
        extra_gsis = sorted(terraform_gsi_names - python_gsi_names)
        if extra_gsis:
            violations.append(
                f"{table_name}: GSI(s) present in tables.tf but absent from schema.py: {extra_gsis}"
            )

        for gsi_name in sorted(python_gsi_names & terraform_gsi_names):
            expected = python_gsis[gsi_name].projected_attributes
            actual = terraform_gsis[gsi_name]
            if expected != actual:
                missing_in_tf = sorted(expected - actual)
                extra_in_tf = sorted(actual - expected)
                detail_parts = []
                if missing_in_tf:
                    detail_parts.append(f"missing from tables.tf: {missing_in_tf}")
                if extra_in_tf:
                    detail_parts.append(f"extra in tables.tf (not in schema.py): {extra_in_tf}")
                violations.append(
                    f"{table_name}.{gsi_name}: projection drift — {'; '.join(detail_parts)}"
                )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables-tf", type=Path, default=DEFAULT_TABLES_TF)
    args = parser.parse_args(argv)

    if not args.tables_tf.exists():
        print(f"error: {args.tables_tf} does not exist", file=sys.stderr)
        return 2

    terraform_projections = parse_terraform_gsi_projections(
        args.tables_tf.read_text(encoding="utf-8")
    )
    violations = check_parity(terraform_projections)

    if violations:
        print(f"FAIL: {len(violations)} GSI projection drift violation(s) found:\n")
        for violation in violations:
            print(f"  - {violation}")
        return 1

    print("PASS: every Terraform GSI projection matches crm.store.schema.TABLES exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
