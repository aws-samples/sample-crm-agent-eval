# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_secret_scan.py`.

Every plant goes into a scratch git repository under `tmp_path`, never the working tree: either a
`git archive HEAD` materialisation re-initialised as a repository, or a minimal `git init` repository.
The positive control runs once with gitleaks' built-in rules (the tracked tree carries no rule set)
and once with the untracked local `.gitleaks.toml` installed, which skips when that file is absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import scripts.check_secret_scan as gate
from scripts import proc_run
from scripts.check_secret_scan import (
    EXIT_VERIFIED_NOTHING,
    GIT_LOCATION_ENV,
    SCAN_FLAGS,
    NothingToScanError,
    SecretScanError,
    binary_unavailable_reason,
    findings_for,
    main,
    materialise,
    resolve_config,
    scan_set,
)

from tests.unit._gitleaks_config import install_local_rule_set, require_local_rule_set


@dataclass(frozen=True)
class _FakeCompleted[Out: (str, bytes)]:
    """Stand-in for a finished process: the attributes the code under test reads; nothing runs."""

    args: Sequence[str]
    returncode: int
    stdout: Out
    stderr: Out


REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SCRIPT = REPO_ROOT / "scripts" / "check_secret_scan.py"

#: A literal matching the local `aws-access-key-id` rule and the built-in `aws-access-token` rule,
#: assembled from fragments.
_PLANTED_SECRET = "A" + "KIA" + "ABCDEFGHIJKLMNOP"


#: A custom-rule match that no built-in rule reports, for telling the rule sets apart.
_CUSTOM_SECRET = "CRMTEST" + "SECRET" + "12345678"  # pragma: allowlist secret

_CUSTOM_RULE_SET = """\
[[rules]]
id = "crm-test-custom-rule"
description = "test-only rule"
regex = '''CRMTESTSECRET[0-9]{8}'''
"""

_NEVER_MATCHING_RULE_SET = """\
[[rules]]
id = "crm-test-never-matches"
description = "test-only rule that matches nothing planted"
regex = '''ZZZ_NEVER_PLANTED_[0-9]{40}'''
"""


def _secret_line() -> str:
    return f'aws_access_key_id = "{_PLANTED_SECRET}"\n'


def _git(root: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_ENV}
    proc_run.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, env=env, timeout=300
    )


def _git_init(root: Path) -> Path:
    """Initialise `root` as a git repository whose ignore rules come only from inside it."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    empty_excludes = root / ".git" / "empty-global-excludes"
    empty_excludes.write_text("", encoding="utf-8")
    _git(root, "config", "core.excludesFile", str(empty_excludes))
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "commit.gpgsign", "false")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository ignoring CI's cache directories, with one harmless tracked commit."""
    _require_gitleaks()
    root = _git_init(tmp_path / "repo")
    (root / ".gitignore").write_text(".mypy_cache/\n.cache/\n", encoding="utf-8")
    (root / "README.txt").write_text("nothing sensitive here\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--no-verify", "-m", "init")
    return root


def _plant(root: Path, rel: str, content: str | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_secret_line() if content is None else content, encoding="utf-8")
    return path


def _require_gitleaks() -> None:
    reason = binary_unavailable_reason()
    if reason is not None:
        pytest.skip(f"gitleaks unavailable: {reason}")


def _materialise(tmp_path: Path) -> Path:
    """Extract `git archive HEAD` into `tmp_path` as a fresh git repository and return the root.

    Nothing is committed, so every extracted file is untracked and scanned unless ignored.
    """
    archive = tmp_path / "tree.tar"
    with archive.open("wb") as handle:
        proc_run.run(
            ["git", "archive", "HEAD"], cwd=REPO_ROOT, stdout=handle, check=True, timeout=300
        )
    root = tmp_path / "tree"
    root.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(root, filter="data")
    assert not (root / ".gitleaks.toml").exists(), "the rule set is tracked again"
    return _git_init(root)


@pytest.fixture(params=["built-in", "local"])
def scan_root(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A materialised tree scanned with built-in rules, or with the local rule set installed."""
    _require_gitleaks()
    if request.param == "local":
        require_local_rule_set()
    root = _materialise(tmp_path)
    if request.param == "local":
        install_local_rule_set(root)
    return root


class TestTheAbsentBinaryIsASkipAndNeverAPass:
    """An absent gitleaks binary yields exit 3 (verified nothing), never a pass."""

    def test_the_predicate_reports_an_absent_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("scripts.check_secret_scan.shutil.which", lambda _name: None)
        reason = binary_unavailable_reason()
        assert reason is not None
        assert "not on PATH" in reason

    def test_main_returns_exit_three_not_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("scripts.check_secret_scan.shutil.which", lambda _name: None)
        assert main([]) == EXIT_VERIFIED_NOTHING
        assert EXIT_VERIFIED_NOTHING == 3

    def test_the_skip_states_that_nothing_was_scanned(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The skip message states that nothing was scanned."""
        monkeypatch.setattr("scripts.check_secret_scan.shutil.which", lambda _name: None)
        main([])
        message = capsys.readouterr().err
        assert "NOTHING was scanned" in message
        assert "not evidence" in message

    def test_the_json_form_also_reports_skip(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("scripts.check_secret_scan.shutil.which", lambda _name: None)
        assert main(["--json"]) == EXIT_VERIFIED_NOTHING
        assert json.loads(capsys.readouterr().out)["status"] == "skip"

    def test_the_real_cli_exits_three_with_an_empty_path(self, tmp_path: Path) -> None:
        """The real CLI exits 3 when `PATH` is an empty directory."""
        empty = tmp_path / "no-bin"
        empty.mkdir()
        completed = proc_run.run(
            [sys.executable, str(GATE_SCRIPT)],
            cwd=REPO_ROOT,
            env={**os.environ, "PATH": str(empty)},
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert completed.returncode == EXIT_VERIFIED_NOTHING, completed.stderr
        assert "SKIP" in completed.stderr


class TestThePositiveControl:
    """A planted secret is detected and the unplanted tree scans clean."""

    def test_a_planted_secret_is_caught(self, scan_root: Path) -> None:
        root = scan_root
        planted = root / "planted_credential.txt"
        planted.write_text(f'aws_access_key_id = "{_PLANTED_SECRET}"\n', encoding="utf-8")

        findings = findings_for(root, resolve_config(root, None))
        assert findings, "the planted credential was not detected — this gate catches nothing"
        files = {str(f.get("File", "")) for f in findings}
        assert any("planted_credential.txt" in name for name in files), files

    def test_main_returns_one_and_names_the_file_without_the_value(
        self, scan_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A finding exits 1 and names the file without printing the secret."""
        root = scan_root
        (root / "planted_credential.txt").write_text(
            f'aws_access_key_id = "{_PLANTED_SECRET}"\n', encoding="utf-8"
        )
        assert main(["--source", str(root)]) == 1
        captured = capsys.readouterr()
        assert "planted_credential.txt" in captured.err
        assert _PLANTED_SECRET not in captured.err + captured.out, (
            "the gate printed the secret it found; --redact is not being applied"
        )

    def test_the_same_tree_without_the_plant_passes(self, scan_root: Path) -> None:
        """The same materialised tree without the plant scans clean."""
        root = scan_root
        assert findings_for(root, resolve_config(root, None)) == []
        assert main(["--source", str(root)]) == 0


class TestTheRuleSetIsResolvedFromTheScannedTree:
    """No `--config` means the tree's own `.gitleaks.toml` if present, else built-in rules."""

    def test_an_explicit_config_wins(self, tmp_path: Path) -> None:
        (tmp_path / ".gitleaks.toml").write_text("", encoding="utf-8")
        explicit = tmp_path / "other.toml"
        assert resolve_config(tmp_path, explicit) == explicit

    def test_the_trees_local_rule_set_is_used_when_present(self, tmp_path: Path) -> None:
        (tmp_path / ".gitleaks.toml").write_text("", encoding="utf-8")
        assert resolve_config(tmp_path, None) == tmp_path / ".gitleaks.toml"

    def test_built_in_rules_are_used_when_the_tree_has_none(self, tmp_path: Path) -> None:
        assert resolve_config(tmp_path, None) is None

    def test_built_in_rules_pass_no_config_flag(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no rule set the scanner's argv carries no `--config`."""
        seen: list[list[str]] = []
        real_run = proc_run.run

        def _record(argv: list[str], **kwargs: Any) -> object:
            if argv[0] != gate.BINARY:
                return real_run(argv, **kwargs)
            seen.append(argv)
            return _FakeCompleted(argv, 0, "", "")

        monkeypatch.setattr("scripts.check_secret_scan.proc_run.run", _record)
        assert findings_for(repo, None) == []
        assert seen and "--config" not in seen[0], seen

    def test_the_ok_line_names_the_built_in_rules(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--source", str(repo)]) == 0
        assert "built-in rules" in capsys.readouterr().out

    def test_an_excluded_local_rule_set_is_resolved_from_the_source(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A `.gitleaks.toml` that `.git/info/exclude` hides is still the rule set applied."""
        (repo / ".gitleaks.toml").write_text(_CUSTOM_RULE_SET, encoding="utf-8")
        (repo / ".git" / "info" / "exclude").write_text(".gitleaks.toml\n", encoding="utf-8")
        assert ".gitleaks.toml" not in scan_set(repo)
        _plant(repo, "notes/custom.txt", f"token {_CUSTOM_SECRET}\n")

        assert main(["--source", str(repo), "--json"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["rule_set"] == str(repo / ".gitleaks.toml")
        assert any(
            line.startswith("crm-test-custom-rule: notes/custom.txt:")
            for line in report["findings"]
        ), report

    def test_without_the_local_rule_set_the_custom_match_is_not_reported(self, repo: Path) -> None:
        """Control: the custom match is invisible to built-in rules, so the rule set decided it."""
        _plant(repo, "notes/custom.txt", f"token {_CUSTOM_SECRET}\n")
        assert main(["--source", str(repo)]) == 0

    def test_an_explicit_config_wins_over_the_local_rule_set(self, repo: Path) -> None:
        (repo / ".gitleaks.toml").write_text(_CUSTOM_RULE_SET, encoding="utf-8")
        (repo / ".git" / "info" / "exclude").write_text(".gitleaks.toml\n", encoding="utf-8")
        _plant(repo, "notes/custom.txt", f"token {_CUSTOM_SECRET}\n")
        explicit = repo.parent / "explicit.toml"
        explicit.write_text(_NEVER_MATCHING_RULE_SET, encoding="utf-8")
        assert main(["--source", str(repo)]) == 1
        assert main(["--source", str(repo), "--config", str(explicit)]) == 0


class TestErrorsAreNotReportedAsFindings:
    """A gitleaks failure to run surfaces as an error, not as a finding."""

    def test_an_absent_config_is_an_error_not_a_leak(self, tmp_path: Path) -> None:
        _require_gitleaks()
        with pytest.raises(SecretScanError, match="does not exist"):
            findings_for(tmp_path, tmp_path / "nope.toml")

    def test_main_maps_that_to_exit_two(self, tmp_path: Path) -> None:
        """An absent config exits 2."""
        _require_gitleaks()
        assert main(["--source", str(tmp_path), "--config", str(tmp_path / "nope.toml")]) == 2

    def test_a_nonzero_status_with_no_report_raises(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero gitleaks status with no report raises `SecretScanError`."""
        monkeypatch.setattr(gate, "_run_gitleaks", lambda *_a, **_k: (127, "command not found"))
        (tmp_path / "cfg.toml").write_text("", encoding="utf-8")
        with pytest.raises(SecretScanError, match="wrote no report"):
            findings_for(repo, tmp_path / "cfg.toml")

    def test_an_unparseable_report_raises(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _write_garbage(_source: Path, _config: Path | None, report: Path) -> tuple[int, str]:
            report.write_text("{not json", encoding="utf-8")
            return 1, ""

        monkeypatch.setattr(gate, "_run_gitleaks", _write_garbage)
        (tmp_path / "cfg.toml").write_text("", encoding="utf-8")
        with pytest.raises(SecretScanError, match="unparseable report"):
            findings_for(repo, tmp_path / "cfg.toml")

    def test_a_source_that_is_not_a_git_work_tree_is_an_error(self, tmp_path: Path) -> None:
        """A non-git source raises rather than falling back to scanning the raw tree."""
        _plant(tmp_path / "plain", "planted.txt")
        with pytest.raises(SecretScanError, match="not a git work tree"):
            findings_for(tmp_path / "plain", None)

    def test_main_maps_a_non_git_source_to_exit_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _require_gitleaks()
        _plant(tmp_path / "plain", "planted.txt")
        assert main(["--source", str(tmp_path / "plain")]) == 2
        assert "not a git work tree" in capsys.readouterr().err

    def test_the_real_cli_exits_two_for_a_non_git_source(self, tmp_path: Path) -> None:
        _require_gitleaks()
        _plant(tmp_path / "plain", "planted.txt")
        completed = proc_run.run(
            [sys.executable, str(GATE_SCRIPT), "--source", str(tmp_path / "plain")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert completed.returncode == 2, completed.stderr

    def test_an_absent_git_binary_is_an_error(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = tmp_path / "no-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        with pytest.raises(SecretScanError, match="could not run git"):
            scan_set(repo)

    def test_a_failing_ls_files_is_an_error(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero `git ls-files` raises instead of yielding an empty, passing scan set."""
        real_run = proc_run.run

        def _fail_ls_files(argv: list[str], **kwargs: Any) -> object:
            if "ls-files" in argv:
                return _FakeCompleted(argv, 128, b"", b"fatal: simulated")
            return real_run(argv, **kwargs)

        monkeypatch.setattr("scripts.check_secret_scan.proc_run.run", _fail_ls_files)
        with pytest.raises(SecretScanError, match="ls-files exited 128"):
            scan_set(repo)
        assert main(["--source", str(repo)]) == 2


class TestTheScanReadsOnlyFilesGitDoesNotIgnore:
    """The scanned set is `git ls-files --cached --others --exclude-standard`, present on disk."""

    _CACHE_PLANTS = (".mypy_cache/3.13/moto/x.json", ".cache/pip/http-v2/a/b/x.body")

    def test_the_raw_tree_scan_finds_the_cache_plants(self, repo: Path) -> None:
        """Reproduction of the CI failure: a raw `--no-git --source` scan reads ignored caches."""
        for rel in self._CACHE_PLANTS:
            _plant(repo, rel)
        report = repo.parent / "raw-report.json"
        proc_run.run(
            [
                gate.BINARY,
                *SCAN_FLAGS,
                "--source",
                str(repo),
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            capture_output=True,
            check=False,
            timeout=300,
        )
        files = {str(f["File"]) for f in json.loads(report.read_text(encoding="utf-8"))}
        for rel in self._CACHE_PLANTS:
            assert any(name.endswith(rel) for name in files), (rel, files)

    def test_gitignored_cache_plants_are_not_scanned(self, repo: Path) -> None:
        for rel in self._CACHE_PLANTS:
            _plant(repo, rel)
        assert findings_for(repo, None) == []
        assert main(["--source", str(repo)]) == 0

    def test_an_untracked_unignored_plant_fails_with_a_source_relative_path(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for rel in self._CACHE_PLANTS:
            _plant(repo, rel)
        _plant(repo, "notes/planted.txt")

        assert {str(f["File"]) for f in findings_for(repo, None)} == {"notes/planted.txt"}
        assert main(["--source", str(repo)]) == 1
        err = capsys.readouterr().err
        assert ": notes/planted.txt:" in err, err
        assert "crm-secret-scan-" not in err, "a scratch path leaked into the report"
        assert _PLANTED_SECRET not in err

    def test_a_tracked_plant_is_scanned(self, repo: Path) -> None:
        _plant(repo, "src/committed.txt")
        _git(repo, "add", "src/committed.txt")
        _git(repo, "commit", "-q", "--no-verify", "-m", "plant")
        assert {str(f["File"]) for f in findings_for(repo, None)} == {"src/committed.txt"}

    def test_an_info_excluded_plant_is_not_scanned(self, repo: Path) -> None:
        (repo / ".git" / "info" / "exclude").write_text(".ash/\n", encoding="utf-8")
        _plant(repo, ".ash/report.json")
        assert main(["--source", str(repo)]) == 0

    def test_the_info_exclude_control_fails_without_the_exclusion(self, repo: Path) -> None:
        """Control: the same plant without the `.git/info/exclude` entry is reported."""
        _plant(repo, ".ash/report.json")
        assert main(["--source", str(repo)]) == 1

    def test_a_deleted_tracked_file_does_not_crash_the_scan(self, repo: Path) -> None:
        (repo / "README.txt").unlink()
        assert "README.txt" in scan_set(repo)
        assert main(["--source", str(repo)]) == 0

    def test_entries_after_a_deleted_tracked_file_are_still_scanned(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Skipping the deleted entry continues the walk; a later plant is still reported."""
        _plant(repo, "zz/planted.txt")
        _git(repo, "add", "zz/planted.txt")
        _git(repo, "commit", "-q", "--no-verify", "-m", "plant")
        (repo / "README.txt").unlink()
        listed = scan_set(repo)
        assert listed.index("README.txt") < listed.index("zz/planted.txt"), listed
        assert main(["--source", str(repo)]) == 1
        assert ": zz/planted.txt:" in capsys.readouterr().err

    def test_symlinks_are_copied_as_symlinks_and_not_followed(
        self, repo: Path, tmp_path: Path
    ) -> None:
        outside = _plant(tmp_path / "outside", "secret.txt")
        _plant(repo, ".cache/ignored-secret.txt")
        (repo / "to-outside").symlink_to(outside)
        (repo / "to-ignored").symlink_to(".cache/ignored-secret.txt")
        dest = tmp_path / "placed"
        dest.mkdir()
        materialise(repo, scan_set(repo), dest)
        assert (dest / "to-outside").is_symlink()
        assert (dest / "to-ignored").readlink() == Path(".cache/ignored-secret.txt")
        assert main(["--source", str(repo)]) == 0

    @pytest.mark.parametrize("entry", [".git/config", "../escape.txt"])
    def test_a_git_directory_or_escaping_entry_is_refused(
        self, repo: Path, tmp_path: Path, entry: str
    ) -> None:
        (repo.parent / "escape.txt").write_text("outside\n", encoding="utf-8")
        dest = tmp_path / "scratch" / "placed"
        dest.mkdir(parents=True)
        with pytest.raises(SecretScanError, match="not a placeable relative path"):
            materialise(repo, [*scan_set(repo), entry], dest)
        assert not (dest / ".git").exists()
        assert not (dest.parent / "escape.txt").exists()

    def test_an_inherited_git_dir_does_not_redirect_the_scan_set(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`GIT_DIR` naming another repository does not swap in that repository's exclusions."""
        (repo / ".git" / "info" / "exclude").write_text(".ash/\n", encoding="utf-8")
        _plant(repo, ".ash/report.json")
        other = _git_init(tmp_path / "other")
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(repo))
        assert main(["--source", str(repo)]) == 0


class TestMaterialisationNeverWritesOutsideTheScratchTree:
    """No entry is written through a placed symlink or outside the scratch root."""

    def test_a_tracked_directory_replaced_by_a_symlink_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A relative link resolving to different victims from the source and the scratch tree."""
        _require_gitleaks()
        root = _git_init(tmp_path / "s1" / "s2" / "repo")
        _plant(root, "d/secret.txt", "repo content\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--no-verify", "-m", "init")
        (root / "d" / "secret.txt").unlink()
        (root / "d").rmdir()
        (root / "d").symlink_to("../../../victim")
        source_victim = _plant(tmp_path, "victim/secret.txt", "source victim\n")
        scratch_parent = tmp_path / "x" / "t"
        scratch_parent.mkdir(parents=True)
        scratch_victim = _plant(tmp_path / "x", "victim/secret.txt", "scratch victim\n")
        monkeypatch.setattr(tempfile, "tempdir", str(scratch_parent))
        listed = scan_set(root)
        assert "d" in listed and "d/secret.txt" in listed, listed

        assert main(["--source", str(root)]) == 2
        assert "sits below the symlinked directory 'd'" in capsys.readouterr().err
        assert source_victim.read_text(encoding="utf-8") == "source victim\n"
        assert scratch_victim.read_text(encoding="utf-8") == "scratch victim\n"
        assert list(scratch_parent.iterdir()) == []

    def test_a_target_below_a_placed_symlink_is_refused(self, tmp_path: Path) -> None:
        """Even with a clean source, a destination parent that is a symlink is never traversed."""
        source = tmp_path / "src"
        _plant(source, "a/b.txt", "content\n")
        dest = tmp_path / "placed"
        dest.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (dest / "a").symlink_to(elsewhere)
        with pytest.raises(SecretScanError, match="already placed"):
            materialise(source, ["a/b.txt"], dest)
        assert list(elsewhere.iterdir()) == []

    def test_an_existing_target_is_never_overwritten(self, tmp_path: Path) -> None:
        source = tmp_path / "src"
        _plant(source, "a.txt", "source\n")
        dest = tmp_path / "placed"
        dest.mkdir()
        (dest / "a.txt").write_text("placed\n", encoding="utf-8")
        with pytest.raises(FileExistsError):
            materialise(source, ["a.txt"], dest)
        assert (dest / "a.txt").read_text(encoding="utf-8") == "placed\n"

    def test_the_copy_fallback_places_identical_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_link(*_a: object, **_k: object) -> None:
            raise OSError(18, "Invalid cross-device link")

        source = tmp_path / "src"
        _plant(source, "a/b.txt", "content\n")
        dest = tmp_path / "placed"
        dest.mkdir()
        monkeypatch.setattr("scripts.check_secret_scan.os.link", _no_link)
        assert materialise(source, ["a/b.txt"], dest) == 1
        assert (dest / "a" / "b.txt").read_text(encoding="utf-8") == "content\n"
        assert (source / "a" / "b.txt").stat().st_nlink == 1


class TestTheSourceTreeIsUnchangedByAScan:
    """Every source file keeps its bytes and link count across a scan and its cleanup."""

    @staticmethod
    def _fingerprint(root: Path) -> dict[str, tuple[str, int]]:
        prints: dict[str, tuple[str, int]] = {}
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if rel.parts[0] == ".git" or path.is_symlink() or not path.is_file():
                continue
            prints[str(rel)] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_nlink)
        return prints

    def test_bytes_and_link_counts_are_unchanged(self, repo: Path) -> None:
        _plant(repo, "notes/planted.txt")
        _plant(repo, ".cache/ignored.txt")
        before = self._fingerprint(repo)
        assert set(before) >= {"README.txt", "notes/planted.txt", ".cache/ignored.txt"}
        assert findings_for(repo, None)
        assert self._fingerprint(repo) == before


class TestUnscannableEntriesAreErrorsNotSkips:
    """Only absent entries may be skipped; anything unreadable fails the gate."""

    def test_an_untracked_nested_repository_is_an_error(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        nested = _git_init(repo / "n")
        _plant(nested, "planted.txt")
        assert "n/" in scan_set(repo)
        assert main(["--source", str(repo)]) == 2
        assert "'n/'" in capsys.readouterr().err

    def test_a_tracked_path_replaced_by_a_special_file_is_an_error(self, repo: Path) -> None:
        (repo / "README.txt").unlink()
        os.mkfifo(repo / "README.txt")
        assert "README.txt" in scan_set(repo)
        assert main(["--source", str(repo)]) == 2

    def test_an_unreadable_file_is_an_error(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        locked = _plant(repo, "notes/locked.txt")
        locked.chmod(0)
        try:
            assert main(["--source", str(repo)]) == 2
            assert "not readable" in capsys.readouterr().err
        finally:
            locked.chmod(0o600)

    def test_a_gitleaks_skipping_file_line_is_an_error(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scanner's own skip log, observed for a mode-000 file, is not a pass."""

        def _skipped(_source: Path, _config: Path | None, report: Path) -> tuple[int, str]:
            report.write_text("[]", encoding="utf-8")
            return 0, "WRN skipping file: permission denied path=x\nINF no leaks found"

        monkeypatch.setattr(gate, "_run_gitleaks", _skipped)
        with pytest.raises(SecretScanError, match="did not read every file"):
            findings_for(repo, None)

    def test_the_real_scanner_logs_the_marker_for_an_unreadable_file(self, tmp_path: Path) -> None:
        """Contract: real gitleaks prints the marker the gate reads, and still exits 0."""
        _require_gitleaks()
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        locked = _plant(tmp_path / "tree", "locked.txt")
        locked.chmod(0)
        try:
            status, output = gate._run_gitleaks(tmp_path / "tree", None, tmp_path / "r.json")
        finally:
            locked.chmod(0o600)
        assert status == 0
        assert gate.SKIPPED_FILE_MARKER in output.lower(), output

    @pytest.mark.parametrize("setup", ["empty-repo", "all-ignored"])
    def test_a_scan_set_that_places_nothing_verified_nothing(
        self, tmp_path: Path, setup: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _require_gitleaks()
        root = _git_init(tmp_path / "repo")
        if setup == "all-ignored":
            (root / ".git" / "info" / "exclude").write_text("*\n", encoding="utf-8")
            _plant(root, "ignored.txt")
        with pytest.raises(NothingToScanError):
            findings_for(root, None)
        assert main(["--source", str(root)]) == EXIT_VERIFIED_NOTHING
        assert "NOTHING was scanned" in capsys.readouterr().err


class TestOnlyTheRepositorysOwnIgnoreFilesDecideTheSet:
    """User-global excludes and environment-injected git config do not narrow the scan."""

    @staticmethod
    def _lists(root: Path, rel: str) -> bool:
        listed = proc_run.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        return rel in listed.stdout.splitlines()

    @pytest.fixture
    def excludes(self, repo: Path, tmp_path: Path) -> Path:
        _git(repo, "config", "--unset", "core.excludesFile")
        excludes = tmp_path / "global-excludes"
        excludes.write_text(".env\n", encoding="utf-8")
        _plant(repo, ".env")
        return excludes

    def test_a_file_ignored_only_by_global_excludes_is_scanned(
        self, repo: Path, excludes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {excludes}\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        assert not self._lists(repo, ".env"), "control: the global excludes file hides .env"
        assert main(["--source", str(repo)]) == 1

    def test_a_file_ignored_by_injected_config_count_is_scanned(
        self, repo: Path, excludes: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.excludesFile")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(excludes))
        assert not self._lists(repo, ".env"), "control: injected config hides .env"
        assert main(["--source", str(repo)]) == 1

    def test_a_file_ignored_by_injected_config_parameters_is_scanned(
        self, repo: Path, excludes: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.excludesFile={excludes}'")
        assert not self._lists(repo, ".env"), "control: injected config hides .env"
        assert main(["--source", str(repo)]) == 1


class TestTheHookRunsThisGate:
    """The `.pre-commit-config.yaml` working-tree hook invokes this gate, not a raw tree scan."""

    def _hook_entry(self) -> str:
        import yaml

        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
        for repo in config.get("repos", []):
            for hook in repo.get("hooks", []):
                if hook.get("id") == "gitleaks":
                    return str(hook.get("entry", ""))
        raise AssertionError("no gitleaks hook is declared in .pre-commit-config.yaml")

    def test_the_working_tree_hook_runs_this_gate(self) -> None:
        entry = self._hook_entry()
        assert "scripts/check_secret_scan.py" in entry, entry
        assert "detect --no-git" not in entry, entry

    def test_the_gate_scans_the_filesystem_not_history(self) -> None:
        """The gate scans the filesystem (`--no-git`), not commit history."""
        assert "--no-git" in SCAN_FLAGS

    def test_redaction_is_present(self) -> None:
        assert "--redact" in SCAN_FLAGS


class TestTheGateWritesNothingIntoTheRepository:
    def test_no_report_file_is_left_behind(self) -> None:
        """Running the gate leaves no new file in the repository root."""
        _require_gitleaks()
        before = {p.name for p in REPO_ROOT.iterdir()}
        main([])
        after = {p.name for p in REPO_ROOT.iterdir()}
        assert after == before, f"the gate created {after - before} in the repository root"

    def test_the_scratch_tree_is_removed(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch_parent = tmp_path / "tmpdir"
        scratch_parent.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch_parent))
        _plant(repo, "notes/planted.txt")
        assert findings_for(repo, None)
        assert list(scratch_parent.iterdir()) == []

    def test_the_report_path_is_a_temporary_directory(self) -> None:
        """The gate source writes its report into a temporary directory."""
        source = GATE_SCRIPT.read_text(encoding="utf-8")
        assert "TemporaryDirectory" in source
        assert "--report-path" in source


def test_the_gate_is_registered_in_the_aggregator() -> None:
    """`check_secret_scan` is registered in `run_gates.py`'s `GATES`."""
    run_gates = (REPO_ROOT / "scripts" / "run_gates.py").read_text(encoding="utf-8")
    registrations = re.findall(r'\(\s*"([a-z0-9_]+)"\s*,\s*\[', run_gates)
    assert "check_secret_scan" in registrations, (
        "check_secret_scan is not registered in run_gates.py's GATES, so `make verify-gates` never "
        "runs it and the secret scan is a control nothing exercises"
    )
