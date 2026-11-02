# SPDX-License-Identifier: MIT
"""`docs/security-baseline.md` records every Holmes checkov disposition H-4..H-10.

Each finding id has exactly one `###` entry under the dispositions section, carrying its rule ids, a
FP/ACCEPT disposition, a compensating control and, for the two ACCEPTs that need a domain and
certificate, an owner decision. Every repository path the entries name must exist.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SECURITY_BASELINE: Final[Path] = REPO_ROOT / "docs" / "security-baseline.md"
SECTION_HEADING: Final[str] = "## Holmes scan dispositions"

#: Finding id -> (rule ids, disposition, owner decision required).
EXPECTED: Final[dict[str, tuple[frozenset[str], str, bool]]] = {
    "H-4": (frozenset({"CKV_AWS_355"}), "FP", False),
    "H-5": (frozenset({"CKV_AWS_174"}), "ACCEPT", True),
    "H-6": (frozenset({"CKV_AWS_109", "CKV_AWS_111", "CKV_AWS_356"}), "FP", False),
    "H-7": (frozenset({"CKV_AWS_111", "CKV_AWS_356"}), "FP", False),
    "H-8": (frozenset({"CKV_AWS_2"}), "ACCEPT", True),
    "H-9": (frozenset({"CKV_AWS_260"}), "FP", False),
    "H-10": (frozenset({"CKV_AWS_316"}), "ACCEPT", False),
}


def entries(text: str) -> dict[str, str]:
    """`### <id>` heading -> body, for every entry under the dispositions section."""
    parts = text.split(f"\n{SECTION_HEADING}\n", 1)
    assert len(parts) == 2, f"no {SECTION_HEADING!r} section"
    section = re.split(r"\n## ", parts[1], maxsplit=1)[0]
    found: dict[str, str] = {}
    for match in re.finditer(r"(?ms)^### (\S+)\n(.*?)(?=^### |\Z)", section):
        assert match.group(1) not in found, f"duplicate entry {match.group(1)}"
        found[match.group(1)] = match.group(2)
    return found


def _field(body: str, label: str) -> str:
    match = re.search(rf"(?ms)^- \*\*{re.escape(label)}\*\*(.*?)(?=^- \*\*|\Z)", body)
    assert match, f"no {label!r} field"
    return match.group(1).strip()


def entry_failures(entry_id: str, body: str) -> list[str]:
    rules, disposition, owner_decision = EXPECTED[entry_id]
    failures = []
    recorded_rules = set(re.findall(r"`(CKV_AWS_\d+)`", _field(body, "Rule:")))
    if recorded_rules != rules:
        failures.append(f"{entry_id}: rules {sorted(recorded_rules)} != {sorted(rules)}")
    recorded = _field(body, "Disposition:").rstrip(".")
    if recorded != disposition:
        failures.append(f"{entry_id}: disposition {recorded!r} != {disposition!r}")
    for label in ("Subject:", "Reason.", "Compensating control."):
        if not _field(body, label):
            failures.append(f"{entry_id}: empty {label!r}")
    has_owner = re.search(r"(?m)^- \*\*Owner decision\.\*\*", body) is not None
    if has_owner != owner_decision:
        failures.append(
            f"{entry_id}: owner decision present={has_owner}, expected {owner_decision}"
        )
    if owner_decision and has_owner and "custom domain" not in _field(body, "Owner decision."):
        failures.append(f"{entry_id}: owner decision does not name the custom domain it needs")
    return failures


@pytest.fixture(scope="module")
def recorded() -> dict[str, str]:
    return entries(SECURITY_BASELINE.read_text(encoding="utf-8"))


def test_every_finding_id_has_exactly_one_entry(recorded: dict[str, str]) -> None:
    assert set(recorded) == set(EXPECTED)


@pytest.mark.parametrize("entry_id", sorted(EXPECTED))
def test_each_entry_records_its_rules_disposition_and_controls(
    recorded: dict[str, str], entry_id: str
) -> None:
    assert entry_failures(entry_id, recorded[entry_id]) == []


@pytest.mark.parametrize("entry_id", sorted(EXPECTED))
def test_every_path_an_entry_names_exists(recorded: dict[str, str], entry_id: str) -> None:
    paths = re.findall(r"`((?:infra|tests|scripts)/[^`\s]+)`", recorded[entry_id])
    missing = [p for p in paths if not (REPO_ROOT / p).exists()]
    assert missing == []


def test_every_rule_id_appears_in_some_entry(recorded: dict[str, str]) -> None:
    recorded_rules = {
        rule
        for body in recorded.values()
        for rule in re.findall(r"`(CKV_AWS_\d+)`", _field(body, "Rule:"))
    }
    assert recorded_rules == set().union(*(rules for rules, _, _ in EXPECTED.values()))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("### H-9\n", "### H-90\n"),
        ("- **Rule:** `CKV_AWS_316`.", "- **Rule:** `CKV_AWS_31`."),
        (
            "- **Disposition:** ACCEPT.\n- **Reason.** Privileged",
            "- **Disposition:** FP.\n- **Reason.** Privileged",
        ),
        (
            "- **Owner decision.** Required: a custom domain and ACM certificate.\n",
            "",
        ),
    ],
)
def test_a_mutated_section_reds_the_checks(old: str, new: str) -> None:
    text = SECURITY_BASELINE.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    mutated = entries(text.replace(old, new))
    failures = [] if set(mutated) == set(EXPECTED) else ["entry set changed"]
    for entry_id in set(mutated) & set(EXPECTED):
        failures.extend(entry_failures(entry_id, mutated[entry_id]))
    assert failures
