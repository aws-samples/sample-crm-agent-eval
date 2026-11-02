#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Drift gate: a gate that lists its phases in its module docstring must name every phase in its row
of the `| Gate | Enforces |` table in `docs/development.md`.

Subjects are discovered, not listed: any `scripts/*.py` whose module docstring holds a numbered list
whose items begin with an RST-quoted (double-backtick) short name is a subject. A module that
describes its phases in prose is not one. Because the parse reads text, a module that quotes an
example of the format is discovered as a subject; describe the format in prose instead.

Bounds: the row is compared against the docstring list, not the phase functions, so a phase
function with no docstring entry is invisible; and each name is checked for presence in the row,
not for an accurate description. `SENTINEL_SUBJECT` must also yield at least `SENTINEL_MIN_PHASES`
phases, so a phase dropped from its docstring is caught.

Usage: `python3 scripts/check_readme_gate_phases.py [--repo-root DIR] [--sentinel-min-phases N]`

Exit codes: 0 pass; 1 drift detected; 2 usage/environment error; 3 verified nothing (never a pass).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The development-doc table this gate reads, matched on its header.
_GATE_TABLE_HEADER = "| Gate | Enforces |"

#: One numbered docstring item whose content starts with an RST-quoted short name.
_PHASE_ITEM_RE = re.compile(r"^\s*\d+\.\s+``([a-z][a-z0-9_]*)``", re.MULTILINE)

#: A gate row's first cell, e.g. ``| `check_pin_drift` | …``.
_GATE_ROW_RE = re.compile(r"^\| `([a-z_][a-z0-9_]*)` \|")

#: A subject discovery must always find; its absence means the parse no longer discriminates.
SENTINEL_SUBJECT = "check_lambda_zip_imports"

#: The minimum phase count `SENTINEL_SUBJECT` must yield. Lower it in the same change that removes
#: a phase from that module.
SENTINEL_MIN_PHASES = 7


def module_docstring(text: str) -> str:
    """The leading triple-quoted docstring of a module's source, or "" if it has none.

    Textual, so no candidate module is imported or executed.
    """
    match = re.search(r'^"""(.*?)"""', text, re.DOTALL | re.MULTILINE)
    return match.group(1) if match else ""


def discover_phase_lists(scripts_dir: Path) -> dict[str, list[str]]:
    """Map each `scripts/*.py` module carrying a machine-readable phase list to that list."""
    discovered: dict[str, list[str]] = {}
    for path in sorted(scripts_dir.glob("*.py")):
        names = _PHASE_ITEM_RE.findall(module_docstring(path.read_text(encoding="utf-8")))
        if names:
            discovered[path.stem] = names
    return discovered


def parse_gate_rows(doc_text: str) -> dict[str, str]:
    """Map each gate name in the `| Gate | Enforces |` table to its whole row.

    Returns `{}` when the table is absent, which the caller treats as verified-nothing rather than
    as "no gate is documented".
    """
    lines = doc_text.split("\n")
    try:
        header = next(i for i, line in enumerate(lines) if line.strip() == _GATE_TABLE_HEADER)
    except StopIteration:
        return {}
    rows: dict[str, str] = {}
    # Skip the header and its `|---|---|` separator; a blank line ends the table.
    for line in lines[header + 2 :]:
        if not line.startswith("|"):
            break
        match = _GATE_ROW_RE.match(line)
        if match:
            rows[match.group(1)] = line
    return rows


def check_sentinel_completeness(phase_lists: dict[str, list[str]], minimum: int) -> list[str]:
    """`SENTINEL_SUBJECT` must still yield at least `minimum` phases.

    Reported as a violation, not a skip: the remaining phases are still compared.
    """
    found = phase_lists.get(SENTINEL_SUBJECT, [])
    if len(found) >= minimum:
        return []
    return [
        f"{SENTINEL_SUBJECT}: discovery found {len(found)} phase(s) {found} where at least {minimum} "
        "are expected, so a phase declaration was lost from the module docstring or the parse stopped "
        "seeing it — either way this gate silently stopped comparing that phase against the "
        "development-doc row. If the phase was deliberately removed from the module, lower SENTINEL_MIN_PHASES in the "
        "same change"
    ]


def check_phase_coverage(phase_lists: dict[str, list[str]], gate_rows: dict[str, str]) -> list[str]:
    """Every discovered phase name must appear backticked in its gate's development-doc row."""
    violations: list[str] = []
    for gate, phases in sorted(phase_lists.items()):
        row = gate_rows.get(gate)
        if row is None:
            violations.append(
                f"{gate}: documents phases {phases} but has no row in docs/development.md's "
                f"'{_GATE_TABLE_HEADER}' table, so none of them is described anywhere"
            )
            continue
        missing = [name for name in phases if f"`{name}`" not in row]
        if missing:
            violations.append(
                f"{gate}: its docs/development.md row does not name phase(s) {missing} — the row describes less "
                f"than the gate checks, so a reader concludes those phases do not exist. Either name "
                f"them in the row or remove them from the module docstring's numbered list"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Tree to check (default: this script's repository).",
    )
    parser.add_argument(
        "--sentinel-min-phases",
        type=int,
        default=SENTINEL_MIN_PHASES,
        help="Minimum phase count the sentinel module must yield (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root if args.repo_root is not None else REPO_ROOT
    dev_doc = repo_root / "docs" / "development.md"
    scripts_dir = repo_root / "scripts"
    for required in (dev_doc, scripts_dir):
        if not required.exists():
            print(f"error: {required} does not exist", file=sys.stderr)
            return 2

    phase_lists = discover_phase_lists(scripts_dir)
    if not phase_lists:
        print(
            "SKIP: no module under scripts/ documents its phases as a numbered list of RST-quoted "
            "short names, so this gate compared nothing. That is either a parser regression or a "
            "convention nobody uses — both are reasons to look, not to pass.",
            file=sys.stderr,
        )
        return 3

    gate_rows = parse_gate_rows(dev_doc.read_text(encoding="utf-8"))
    if not gate_rows:
        print(
            f"SKIP: could not find the '{_GATE_TABLE_HEADER}' table in {dev_doc}, or it holds no gate "
            "row, so every phase would have been reported missing for the wrong reason.",
            file=sys.stderr,
        )
        return 3

    if SENTINEL_SUBJECT not in phase_lists:
        print(
            f"SKIP: discovery did not find {SENTINEL_SUBJECT!r}, which documents its phases in the "
            "form this gate parses. Whatever it did find, the parse no longer sees the case this "
            "gate was written for, so a pass would not mean the scan discriminates.",
            file=sys.stderr,
        )
        return 3

    violations = check_sentinel_completeness(
        phase_lists, args.sentinel_min_phases
    ) + check_phase_coverage(phase_lists, gate_rows)

    covered = sorted(phase_lists)
    uncovered = sorted(set(gate_rows) - set(phase_lists))
    print(
        f"scope: {len(covered)} of {len(gate_rows)} documented gate(s) carry a parseable phase list"
    )
    for gate in covered:
        print(f"  covered   {gate}: {', '.join(phase_lists[gate])}")
    print(
        "  NOT covered (no numbered RST-quoted phase list in the module docstring, so this gate "
        f"says nothing about what their rows claim): {', '.join(uncovered)}"
    )
    print(
        "  bound: each covered row is compared against its module's DOCSTRING list, not against its "
        "phase functions — a phase added without a docstring entry is invisible here."
    )
    print(
        f"  floor: {SENTINEL_SUBJECT} must yield >= {args.sentinel_min_phases} phase(s) and yields "
        f"{len(phase_lists[SENTINEL_SUBJECT])}, so a phase silently dropped from its docstring reds "
        "here rather than shrinking this scope unnoticed."
    )

    if violations:
        print(
            f"\nFAIL: {len(violations)} development-doc phase-coverage violation(s):\n",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    print("\nPASS: every documented phase is named in its gate's docs/development.md row.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
