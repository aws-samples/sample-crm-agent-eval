# SPDX-License-Identifier: MIT
"""Tests that `.gitleaks.toml`'s single rule-scoped allowlist entry stays necessary and narrow.

The entry exempts `--i-understand-this-is-billable` as an assigned value. These tests assert that
removing it reintroduces the finding, that other credential-shaped values are still reported in the
exempted file and elsewhere, and that exactly one rule-scoped allowlist exists. They skip without
the `gitleaks` binary, and without the untracked local `.gitleaks.toml` unless
`CRM_REQUIRE_GITLEAKS_CONFIG=1`.

Scans run over a `git archive HEAD` materialisation under `/tmp` with the working tree's config;
plants are written only into that materialisation.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

from tests.unit._gitleaks_config import LOCAL_RULE_SET, require_local_rule_set

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = LOCAL_RULE_SET

#: Repo-relative paths these tests plant into; also the set the no-mutation guard watches.
EXEMPTED_RELATIVE: Final[str] = "tests/unit/test_ci_parity.py"
UNRELATED_RELATIVE: Final[str] = "tests/unit/test_infra_api.py"
PLANT_TARGETS: Final[tuple[str, ...]] = (EXEMPTED_RELATIVE, UNRELATED_RELATIVE)

#: The one exempted value, as defined in `scripts/verify_deployment.sh`.
EXEMPTED_VALUE = "--i-understand-this-is-billable"

#: The rule the exemption is scoped to.
EXEMPTED_RULE = "generic-high-entropy-secret"

#: A high-entropy placeholder the rule reports; not a real credential.
PLANTED_HIGH_ENTROPY = "aZ9kQ2mN7pR4sT6v" + "X8yB1cD3eF5gH0jK"


def _gitleaks_unavailable_reason() -> str | None:
    if shutil.which("gitleaks") is None:
        return "gitleaks is not on PATH, so the exemption's necessity cannot be measured"
    return None


def _require_gitleaks() -> None:
    reason = _gitleaks_unavailable_reason()
    if reason is not None:
        pytest.skip(reason)


@pytest.fixture(autouse=True)
def _local_rule_set() -> None:
    require_local_rule_set()


def materialise_head(destination: Path) -> Path:
    """Extract `git archive HEAD` into `destination` and return the tree root."""
    root = destination / "tree"
    root.mkdir(parents=True, exist_ok=True)
    archive = proc_run.run(
        ["git", "archive", "HEAD"], cwd=REPO_ROOT, capture_output=True, check=True
    )
    proc_run.run(["tar", "-x", "-C", str(root)], input=archive.stdout, check=True)
    return root


@pytest.fixture(scope="module")
def scan_tree() -> Iterator[Path]:
    """A module-scoped `/tmp` materialisation of HEAD; the only tree these tests write into."""
    _require_gitleaks()
    with tempfile.TemporaryDirectory(prefix="gitleaks-ratchet-") as tmp:
        yield materialise_head(Path(tmp))


def _findings(
    scan_root: Path,
    config_text: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Run gitleaks over `scan_root` and return its findings.

    `config_text` replaces `.gitleaks.toml` for this run. `extra_files` maps a repo-relative path
    to content appended to that file inside `scan_root` and restored afterwards.
    """
    planted: list[tuple[Path, str]] = []
    for relative, content in (extra_files or {}).items():
        target = scan_root / relative
        assert target.exists(), (
            f"planting into a file that does not exist in the HEAD materialisation: {relative}. "
            "If it was renamed or deleted, update PLANT_TARGETS."
        )
        original = target.read_text(encoding="utf-8")
        target.write_text(original + content, encoding="utf-8")
        planted.append((target, original))

    try:
        with tempfile.TemporaryDirectory() as tmp:
            config = CONFIG_PATH
            if config_text is not None:
                config = Path(tmp) / "gitleaks.toml"
                config.write_text(config_text, encoding="utf-8")
            report = Path(tmp) / "report.json"
            proc_run.run(
                [
                    "gitleaks",
                    "detect",
                    "--no-git",
                    "-c",
                    str(config),
                    "--source",
                    str(scan_root),
                    "--report-format",
                    "json",
                    "--report-path",
                    str(report),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            if not report.exists():
                return []
            loaded = json.loads(report.read_text(encoding="utf-8") or "[]")
            assert isinstance(loaded, list)
            return loaded
    finally:
        for target, original in planted:
            target.write_text(original, encoding="utf-8")


def _tracked_digests() -> dict[str, str]:
    """SHA-256 of each plant target as it currently exists in the working tree."""
    digests: dict[str, str] = {}
    for relative in PLANT_TARGETS:
        path = REPO_ROOT / relative
        if path.exists():
            digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


@pytest.fixture(autouse=True)
def tracked_tree_is_untouched() -> Iterator[None]:
    """Fail any test in this module that changes a plant target's working-tree content digest."""
    before = _tracked_digests()
    yield
    after = _tracked_digests()
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert not changed, (
        f"this test changed tracked file(s) {changed}. Plants belong in the `scan_tree` materialisation "
        "under /tmp, never in the working tree: a missed restore leaves a planted secret in tracked "
        "source, and two concurrent runs silently overwrite each other's edits. (If a peer edited these "
        "files during this test, re-run on a quiet tree to confirm.)"
    )


def _config_without_the_exemption() -> str:
    """`.gitleaks.toml` with the `[rules.allowlist]` table removed, bounded by table headers."""
    text = CONFIG_PATH.read_text(encoding="utf-8")
    marker = "\n[rules.allowlist]\n"
    start = text.find(marker)
    assert start != -1, (
        "no [rules.allowlist] table in .gitleaks.toml. If the exemption was removed, delete this test "
        "file too; if it was renamed (e.g. to the plural form, which gitleaks 8.30.1 SILENTLY IGNORES), "
        "fix the config — a config that parses cleanly and does nothing is the worst available shape."
    )
    nxt = text.find("\n[", start + len(marker))
    assert nxt != -1, "no table follows [rules.allowlist]; cannot bound the block to remove"
    stripped = text[:start] + text[nxt:]
    # Compared per line: retained comments may name `[rules.allowlist]` in prose.
    assert "[rules.allowlist]" not in [line.strip() for line in stripped.splitlines()]
    return stripped


def _finding_locations(findings: list[dict[str, object]], scan_root: Path) -> set[tuple[str, str]]:
    """`(rule_id, path relative to scan_root)` per finding; gitleaks reports absolute paths."""
    located: set[tuple[str, str]] = set()
    for finding in findings:
        raw = Path(str(finding.get("File")))
        path = raw.relative_to(scan_root) if raw.is_absolute() else raw
        located.add((str(finding.get("RuleID")), str(path)))
    return located


@pytest.fixture(scope="module")
def baseline(scan_tree: Path) -> set[tuple[str, str]]:
    """Findings on the unplanted materialisation, used for file-scoped membership checks."""
    return _finding_locations(_findings(scan_tree), scan_tree)


def test_the_exemption_is_still_necessary(baseline: set[tuple[str, str]], scan_tree: Path) -> None:
    """Removing the exemption reintroduces the finding it suppresses; with it, the finding is absent."""
    without = _finding_locations(
        _findings(scan_tree, config_text=_config_without_the_exemption()), scan_tree
    )
    relative = EXEMPTED_RELATIVE
    assert (EXEMPTED_RULE, relative) in without, (
        f"gitleaks no longer reports {relative} under {EXEMPTED_RULE} even without the allowlist "
        "entry, so that entry is unnecessary. Delete it from .gitleaks.toml and delete this test — "
        "do not leave an exemption in place for a finding that no longer occurs."
    )
    assert (EXEMPTED_RULE, relative) not in baseline, (
        f"the exemption is present but {relative} is still reported, so it is not working"
    )


def test_the_exemption_does_not_cover_a_real_secret_in_the_same_file(
    baseline: set[tuple[str, str]], scan_tree: Path
) -> None:
    """A credential-shaped value planted in the exempted file is still reported."""
    relative = EXEMPTED_RELATIVE
    plant = f'\nPLANTED_API_KEY = "{PLANTED_HIGH_ENTROPY}"\n'
    with_plant = _finding_locations(_findings(scan_tree, extra_files={relative: plant}), scan_tree)
    assert (EXEMPTED_RULE, relative) in with_plant, (
        f"a planted high-entropy value in {relative} was NOT reported — the allowlist entry is "
        "exempting the whole file, not one value"
    )


def test_the_exemption_does_not_cover_a_real_secret_in_an_unrelated_file(
    baseline: set[tuple[str, str]], scan_tree: Path
) -> None:
    """A credential-shaped value planted in an unrelated file is still reported."""
    relative = UNRELATED_RELATIVE
    plant = f'\nPLANTED_SECRET = "{PLANTED_HIGH_ENTROPY}"\n'
    with_plant = _finding_locations(_findings(scan_tree, extra_files={relative: plant}), scan_tree)
    assert (EXEMPTED_RULE, relative) in with_plant, (
        f"a planted high-entropy value in {relative} was NOT reported — the exemption is matching more "
        "than the one flag it is scoped to"
    )


def test_the_exemption_covers_documentation_that_quotes_the_flag(
    baseline: set[tuple[str, str]], scan_tree: Path
) -> None:
    """The exempted flag assigned in a file other than its definition site is not reported."""
    relative = UNRELATED_RELATIVE
    plant = f'\nSOME_TOKEN = "{EXEMPTED_VALUE}"\n'
    with_plant = _finding_locations(_findings(scan_tree, extra_files={relative: plant}), scan_tree)
    assert (EXEMPTED_RULE, relative) not in with_plant, (
        f"the exempted flag assigned in {relative} was reported, so the entry is still path-scoped and "
        "will re-fire on the next document that quotes it"
    )


def test_there_is_exactly_one_rule_scoped_allowlist() -> None:
    """`.gitleaks.toml` contains exactly one rule-scoped allowlist, naming the exempted value."""
    text = CONFIG_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"^\[\[?rules\.allowlists?\]\]?", text, re.M)
    assert len(blocks) == 1, (
        f"expected exactly one rule-scoped allowlist in .gitleaks.toml, found {len(blocks)}: "
        f"{blocks}. Each one needs its own stated reason and its own ratchet test in this file."
    )
    assert EXEMPTED_VALUE in text
    assert "tests/unit/test_ci_parity" in text
