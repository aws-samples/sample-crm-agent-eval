# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_untracked_artifact_secrets.py`.

Fixtures live in `tmp_path` with their own git repository.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_untracked_artifact_secrets.py"
ACCOUNT_GATE = REPO_ROOT / "scripts" / "check_no_account_id.sh"
VENDOR_GATE = REPO_ROOT / "scripts" / "check_no_vendor_keys.sh"


def _load_module(name: str, path: Path) -> ModuleType:
    """Import a `scripts/` module without requiring it to be importable as a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_untracked_artifact_secrets", GATE)
run_gates = _load_module(
    "run_gates_for_artifact_secrets_test", REPO_ROOT / "scripts" / "run_gates.py"
)

#: A syntactically valid, non-placeholder account id, assembled at runtime.
SYNTHETIC_ACCOUNT_ID = "48210" + "7365194"

#: An absolute home-directory path, assembled at runtime.
SYNTHETIC_HOME_PATH = "/Us" + "ers/someone"


def _repo(tmp_path: Path, *, gitignore: str = "artifacts/\n") -> Path:
    """Create a git repository at `tmp_path` with an `artifacts/` directory."""
    proc_run.run(["git", "init", "-q", str(tmp_path)], check=True)
    proc_run.run(["git", "-C", str(tmp_path), "config", "user.email", "t@e"], check=True)
    proc_run.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    (tmp_path / "artifacts").mkdir()
    return tmp_path


def _run_in(repo: Path, artifacts: Path) -> tuple[list[str], int]:
    """Run the gate against a fixture repository and return `(violations, scanned)`."""
    result: tuple[list[str], int] = gate.run_gate((artifacts,), repo)
    return result


# ---------------------------------------------------------------------------
# Patterns are read from the real shell gates, not copied.
# ---------------------------------------------------------------------------


def test_pattern_extraction_matches_the_shell_arrays() -> None:
    """The gate reads exactly as many patterns as each shell gate's `PATTERNS` array declares."""
    for path in (ACCOUNT_GATE, VENDOR_GATE):
        source = path.read_text(encoding="utf-8")
        body = source.split("PATTERNS=(\n", 1)[1].split("\n)", 1)[0]
        declared = [
            line
            for line in (raw.strip() for raw in body.splitlines())
            if line and not line.startswith("#")
        ]
        assert len(gate.shell_patterns(path)) == len(declared), (
            f"{path.name}: this gate reads a different number of patterns than the array declares"
        )


def test_extraction_unquotes_shell_quoting() -> None:
    """Shell quoting in the pattern arrays is unquoted with POSIX classes kept verbatim."""
    patterns = gate.shell_patterns(ACCOUNT_GATE)
    account_id_pattern = next(p for p in patterns if "AccountId" in p)
    assert "[[:space:]]" in account_id_pattern
    assert '"' in account_id_pattern and "'" in account_id_pattern
    assert "\\s" not in account_id_pattern


def test_a_malformed_pattern_array_is_an_error_not_an_empty_scan(tmp_path: Path) -> None:
    """A script with no `PATTERNS` array raises `GateError`."""
    broken = tmp_path / "broken.sh"
    broken.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    with pytest.raises(gate.GateError, match="no PATTERNS"):
        gate.shell_patterns(broken)


def test_placeholder_ids_are_parsed_from_the_account_gate() -> None:
    ids = gate.placeholder_account_ids()
    assert "123456789012" in ids, "the documented placeholder is not in the parsed allowlist"


# ---------------------------------------------------------------------------
# The three checks.
# ---------------------------------------------------------------------------


class TestVendorKeys:
    def test_a_vendor_key_in_an_ignored_artifact_still_fails(self, tmp_path: Path) -> None:
        """A vendor key in a gitignored artifact is a finding."""
        repo = _repo(tmp_path)
        (repo / "artifacts" / "log.txt").write_text(
            "ANTHROPIC_API_KEY=sk-ant-" + "A" * 40 + "\n", encoding="utf-8"
        )
        violations, scanned = _run_in(repo, repo / "artifacts")
        assert scanned == 1
        assert any("vendor-api-key" in v for v in violations)

    def test_the_finding_does_not_print_the_key(self, tmp_path: Path) -> None:
        """A finding does not contain the key itself."""
        repo = _repo(tmp_path)
        planted = "sk-ant-" + "B" * 40
        (repo / "artifacts" / "log.txt").write_text(f"key={planted}\n", encoding="utf-8")
        violations, _ = _run_in(repo, repo / "artifacts")
        assert violations
        assert all(planted not in v for v in violations)

    def test_a_clean_artifact_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        (repo / "artifacts" / "log.txt").write_text("mean_correctness: 0.75\n", encoding="utf-8")
        violations, scanned = _run_in(repo, repo / "artifacts")
        assert (violations, scanned) == ([], 1)


class TestContainment:
    def _write_account_arn(self, repo: Path, name: str = "out.json") -> Path:
        path = repo / "artifacts" / name
        path.write_text(
            f'{{"arn": "arn:aws:iam::{SYNTHETIC_ACCOUNT_ID}:role/app"}}\n', encoding="utf-8"
        )
        return path

    def test_an_ignored_artifact_with_a_real_account_id_passes(self, tmp_path: Path) -> None:
        """A real account id in a gitignored artifact is not a finding."""
        repo = _repo(tmp_path, gitignore="artifacts/\n")
        self._write_account_arn(repo)
        violations, scanned = _run_in(repo, repo / "artifacts")
        assert (violations, scanned) == ([], 1)

    def test_an_unignored_artifact_with_a_real_account_id_fails(self, tmp_path: Path) -> None:
        """A real account id in a non-ignored artifact is a finding."""
        repo = _repo(tmp_path, gitignore="# nothing ignored\n")
        self._write_account_arn(repo)
        violations, _ = _run_in(repo, repo / "artifacts")
        assert any("NOT gitignored" in v for v in violations)

    def test_containment_is_decided_per_file_not_per_directory(self, tmp_path: Path) -> None:
        """A `dir/*` ignore rule contains the files under it."""
        repo = _repo(tmp_path, gitignore="artifacts/*\n")
        self._write_account_arn(repo)
        violations, _ = _run_in(repo, repo / "artifacts")
        assert violations == [], (
            "a `dir/*` ignore rule was read as leaving the files uncontained; containment must be "
            "asked per file"
        )

    def test_a_tracked_artifact_with_a_real_account_id_fails(self, tmp_path: Path) -> None:
        """A real account id in a tracked or staged artifact is a finding."""
        repo = _repo(tmp_path, gitignore="# nothing ignored\n")
        path = self._write_account_arn(repo)
        proc_run.run(["git", "-C", str(repo), "add", "-f", str(path)], check=True)
        violations, _ = _run_in(repo, repo / "artifacts")
        assert any("tracked or staged" in v for v in violations)

    def test_a_placeholder_account_id_is_not_a_finding(self, tmp_path: Path) -> None:
        """The documented placeholder account id is not a finding."""
        repo = _repo(tmp_path, gitignore="# nothing ignored\n")
        (repo / "artifacts" / "doc.md").write_text(
            "arn:aws:iam::123456789012:role/example\n", encoding="utf-8"
        )
        violations, _ = _run_in(repo, repo / "artifacts")
        assert violations == [], f"the placeholder id was reported: {violations}"

    def test_a_private_key_block_is_a_finding(self, tmp_path: Path) -> None:
        """A private-key block in an artifact is a finding."""
        repo = _repo(tmp_path, gitignore="# nothing ignored\n")
        header = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
        (repo / "artifacts" / "id.pem").write_text(f"{header}\nx\n", encoding="utf-8")
        violations, _ = _run_in(repo, repo / "artifacts")
        assert violations, (
            "the runtime-assembled header no longer matches the gate's pattern; the fixture must "
            "produce the real bytes, not merely avoid the literal"
        )


class TestValueLevelFiltering:
    """Whether a grep hit is a finding is decided by the matched value."""

    def test_a_home_path_alone_is_not_a_finding(self, tmp_path: Path) -> None:
        """A home-directory path alone in an artifact is not a finding."""
        repo = _repo(tmp_path, gitignore="# nothing ignored\n")
        (repo / "artifacts" / "paths.txt").write_text(
            f"{SYNTHETIC_HOME_PATH}/workspace/thing\n", encoding="utf-8"
        )
        assert _run_in(repo, repo / "artifacts")[0] == []

    def test_a_home_path_beside_a_real_account_id_is_a_finding(self, tmp_path: Path) -> None:
        """A real account id beside a home path in the same file is still a finding."""
        repo = _repo(tmp_path, gitignore="# nothing ignored\n")
        (repo / "artifacts" / "paths.txt").write_text(
            f"{SYNTHETIC_HOME_PATH}/x\narn:aws:iam::{SYNTHETIC_ACCOUNT_ID}:role/app\n",
            encoding="utf-8",
        )
        assert _run_in(repo, repo / "artifacts")[0]

    def test_the_synthetic_home_path_still_matches_the_shell_gate(self) -> None:
        """The synthetic home path matches the home-path pattern in `check_no_account_id.sh`."""
        patterns = [p for p in gate.shell_patterns(ACCOUNT_GATE) if "(Users|home)" in p]
        assert len(patterns) == 1, f"expected exactly one home-path pattern, found {patterns}"
        assert re.search(patterns[0], f"{SYNTHETIC_HOME_PATH}/x"), (
            "the runtime-assembled home path no longer matches check_no_account_id.sh's pattern"
        )

    def test_a_twelve_digit_run_inside_a_longer_number_is_not_a_finding(
        self, tmp_path: Path
    ) -> None:
        """A twelve-digit run inside a longer decimal number is not a finding."""
        repo = _repo(tmp_path, gitignore="# nothing ignored\n")
        (repo / "artifacts" / "m.json").write_text(
            '{"mean_correctness": 0.7203907203907204}\n', encoding="utf-8"
        )
        assert _run_in(repo, repo / "artifacts")[0] == []


# ---------------------------------------------------------------------------
# Skip disposition and registration.
# ---------------------------------------------------------------------------


def test_an_empty_artifact_tree_verifies_nothing_and_skips(tmp_path: Path) -> None:
    """An empty artifact tree scans nothing and the gate exits with the skip status."""
    repo = _repo(tmp_path)
    assert _run_in(repo, repo / "artifacts") == ([], 0)
    completed = proc_run.run(
        [sys.executable, str(GATE), "--artifact-dir", str(repo / "artifacts")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == gate.SKIP_EXIT_STATUS == 3
    assert "[SKIP]" in completed.stdout
    assert "Nothing was verified" in completed.stdout


def test_skip_status_matches_the_run_gates_contract() -> None:
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS


def test_the_gate_is_registered_in_run_gates() -> None:
    names = [name for name, _ in run_gates.GATES]
    assert "check_untracked_artifact_secrets" in names


def test_the_real_tree_passes() -> None:
    """The gate passes or skips on the real repository."""
    completed = proc_run.run(
        [sys.executable, str(GATE)], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert completed.returncode in (0, gate.SKIP_EXIT_STATUS), completed.stdout + completed.stderr


class TestTheLocaleDoesNotDecideWhatThisGateCanSee:
    """The gate's locale pin finds secrets on lines carrying bytes invalid in UTF-8.

    Whether an unpinned `grep` misses such a line depends on the host's `grep` implementation, so no
    test here requires a particular unpinned outcome.
    """

    #: A UTF-8 locale available on macOS and in the CI image.
    _UTF8_LOCALE = "C.UTF-8"

    @staticmethod
    def _plant(repo: Path, key: str) -> None:
        """Write the artifact as bytes, with an ill-formed UTF-8 sequence before the key."""
        (repo / "artifacts" / "log.txt").write_bytes(
            b"bad \xe9\xe8 seq then " + key.encode("ascii") + b" trailing\n"
        )

    def test_a_vendor_key_on_an_invalid_utf8_line_is_found(self, tmp_path: Path) -> None:
        """The gate finds a vendor key on a line carrying an invalid UTF-8 sequence."""
        repo = _repo(tmp_path)
        # Assembled at runtime so this test file never itself carries a key-shaped literal.
        key = "sk-" + "ant-" + "C" * 40
        self._plant(repo, key)
        assert b"\xe9\xe8" in (repo / "artifacts" / "log.txt").read_bytes(), (
            "the fixture does not contain the invalid sequence, so this cell would measure nothing"
        )

        violations, scanned = _run_in(repo, repo / "artifacts")
        assert scanned == 1
        assert any("vendor-api-key" in v for v in violations), (
            f"the gate must FIND a vendor key on an invalid-byte line; it reported {violations}"
        )
        assert key not in " ".join(violations), "the finding must not echo the key"

    #: Labels for what the pin-removed matcher does on this host, derived at runtime.
    _UNPINNED_MISSED = "MISSED (this host is blind without the pin; the pin is load-bearing here)"
    _UNPINNED_FOUND = "FOUND (this host is not blind; the pin is redundant here, not wrong)"

    @classmethod
    def _unpinned_outcome(cls, violations: list[str]) -> str:
        return (
            cls._UNPINNED_FOUND
            if any("vendor-api-key" in v for v in violations)
            else cls._UNPINNED_MISSED
        )

    def test_the_pin_never_loses_a_finding_and_what_it_gains_is_derived_per_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pinned matcher finds the key and reports a superset of the unpinned findings."""
        key = "sk-" + "ant-" + "D" * 40

        pinned_repo = _repo(tmp_path / "pinned")
        self._plant(pinned_repo, key)
        pinned_violations, pinned_scanned = _run_in(pinned_repo, pinned_repo / "artifacts")

        # The unpinned matcher runs on a byte-identical fixture in its own repository.
        unpinned_repo = _repo(tmp_path / "unpinned")
        self._plant(unpinned_repo, key)
        monkeypatch.setattr(
            gate, "_MATCHER_ENV", {**os.environ, "LC_ALL": self._UTF8_LOCALE}, raising=True
        )
        unpinned_violations, unpinned_scanned = _run_in(unpinned_repo, unpinned_repo / "artifacts")

        assert pinned_scanned == 1 and unpinned_scanned == 1, (
            "both fixtures must present exactly one artifact to the gate, or the comparison below is "
            f"between two different subjects: pinned scanned {pinned_scanned}, unpinned scanned "
            f"{unpinned_scanned}"
        )

        pinned_found = {v for v in pinned_violations if "vendor-api-key" in v}
        unpinned_found = {v for v in unpinned_violations if "vendor-api-key" in v}
        outcome = self._unpinned_outcome(unpinned_violations)

        assert pinned_found, (
            "the shipped gate must find a vendor key on an invalid-byte line and did not. The "
            f"pin-removed matcher on this host {outcome}. Reported: {pinned_violations}"
        )

        assert unpinned_found <= pinned_found, (
            f"the pin LOST a finding: under LC_ALL=C the gate reported {sorted(pinned_found)}, but "
            f"under LC_ALL={self._UTF8_LOCALE} it reported {sorted(unpinned_found)}, which is not a "
            "subset. The pin exists to widen what the gate can see and has narrowed it instead — "
            "re-derive from measurement; do not relax this into a skip and do not delete it."
        )

        assert key not in " ".join([*pinned_violations, *unpinned_violations]), (
            "no finding may echo the key itself"
        )
