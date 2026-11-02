# SPDX-License-Identifier: MIT
"""Plan freshness by content: `plan_fixtures.infra_digest` and the gate's digest comparison.

Every mutation runs in a scratch `git init` repository under `tmp_path`; the real tree is only read.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from scripts import plan_fixtures, proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_plan_security_baseline.py"

if TYPE_CHECKING:
    import scripts.check_plan_security_baseline as gate
else:
    _SPEC = importlib.util.spec_from_file_location("check_plan_security_baseline", SCRIPT_PATH)
    assert _SPEC is not None and _SPEC.loader is not None
    gate = importlib.util.module_from_spec(_SPEC)
    sys.modules.setdefault("check_plan_security_baseline", gate)
    _SPEC.loader.exec_module(gate)

#: `git -c` identity for scratch commits, so no global git config is needed or read.
_GIT_IDENTITY = ("-c", "user.name=scratch", "-c", "user.email=scratch@example.invalid")


def _git(repo: Path, *args: str) -> str:
    completed = proc_run.run(
        ["git", "--no-pager", *_GIT_IDENTITY, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return str(completed.stdout)


def _tracked_tf(root: Path) -> list[str]:
    listed = _git(root, "ls-files", "-z", "--", *plan_fixtures.INFRA_TF_PATHSPECS)
    return sorted(path for path in listed.split("\0") if path)


def _scratch_repo(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """A fresh repository with exactly `files` committed in one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "one")
    return repo


def _copy_of_real_infra(tmp_path: Path) -> Path:
    """A one-commit repository holding only the real tree's tracked `infra/**/*.tf`."""
    return _scratch_repo(
        tmp_path, {path: (REPO_ROOT / path).read_bytes() for path in _tracked_tf(REPO_ROOT)}
    )


def _reference_digest(root: Path, paths: list[str]) -> str:
    lines = "".join(
        f"{path}\0{hashlib.sha256((root / path).read_bytes()).hexdigest()}\n"
        for path in sorted(paths)
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def _plan_with_digest(tmp_path: Path, digest: object, *, drop: bool = False) -> Path:
    document = plan_fixtures.build("compliant")
    if drop:
        document["_fixture_provenance"].pop(gate.INFRA_DIGEST_KEY, None)
    else:
        document["_fixture_provenance"][gate.INFRA_DIGEST_KEY] = digest
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_digest_is_the_specified_hash_over_sorted_path_and_content_lines(
    tmp_path: Path,
) -> None:
    repo = _scratch_repo(
        tmp_path,
        {"infra/main.tf": b"a = 1\n", "infra/modules/m/x.tf": b"b = 2\n", "infra/README.md": b"x"},
    )
    expected = _reference_digest(repo, ["infra/main.tf", "infra/modules/m/x.tf"])
    assert plan_fixtures.infra_digest(repo) == expected


def test_the_real_tree_digest_matches_an_independent_recomputation() -> None:
    paths = _tracked_tf(REPO_ROOT)
    assert any("/" in path.removeprefix("infra/") for path in paths), "no nested module .tf"
    assert any("/" not in path.removeprefix("infra/") for path in paths), "no root-level .tf"
    assert plan_fixtures.infra_digest(REPO_ROOT) == _reference_digest(REPO_ROOT, paths)


def test_the_digest_reads_no_history(tmp_path: Path) -> None:
    """A one-commit repository with the same tracked .tf content has the same digest."""
    assert plan_fixtures.infra_digest(_copy_of_real_infra(tmp_path)) == plan_fixtures.infra_digest(
        REPO_ROOT
    )


def test_a_one_byte_tf_change_changes_the_digest(tmp_path: Path) -> None:
    repo = _copy_of_real_infra(tmp_path)
    before = plan_fixtures.infra_digest(repo)
    target = repo / "infra" / "main.tf"
    content = target.read_bytes()
    target.write_bytes(content[:-1] + (b" " if content[-1:] != b" " else b"\n"))
    assert plan_fixtures.infra_digest(repo) != before


def test_an_untracked_tf_file_is_excluded(tmp_path: Path) -> None:
    repo = _copy_of_real_infra(tmp_path)
    before = plan_fixtures.infra_digest(repo)
    (repo / "infra" / "scratch_untracked.tf").write_text('variable "x" {}\n', encoding="utf-8")
    assert plan_fixtures.infra_digest(repo) == before


def test_a_deleted_tracked_tf_file_changes_the_digest(tmp_path: Path) -> None:
    repo = _copy_of_real_infra(tmp_path)
    before = plan_fixtures.infra_digest(repo)
    (repo / "infra" / "main.tf").unlink()
    assert plan_fixtures.infra_digest(repo) != before


def test_no_tracked_tf_or_no_repository_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no tracked infra"):
        plan_fixtures.infra_digest(_scratch_repo(tmp_path, {"README.md": b"x"}))
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    with pytest.raises(ValueError, match="git ls-files"):
        plan_fixtures.infra_digest(bare)


def test_the_committed_base_records_the_current_tree_digest() -> None:
    provenance = plan_fixtures.build("compliant")["_fixture_provenance"]
    assert provenance[gate.INFRA_DIGEST_KEY] == plan_fixtures.infra_digest(REPO_ROOT)
    assert "captured_from_commit" not in provenance


@pytest.mark.parametrize("name", plan_fixtures.NAMES)
def test_every_built_fixture_carries_the_digest_and_no_commit_key(name: str) -> None:
    provenance = plan_fixtures.build(name)["_fixture_provenance"]
    base = plan_fixtures.build("compliant")["_fixture_provenance"]
    assert provenance.get(gate.INFRA_DIGEST_KEY) == base[gate.INFRA_DIGEST_KEY]
    assert "captured_from_commit" not in provenance


def test_plan_freshness_passes_at_head(tmp_path: Path) -> None:
    finding = gate.plan_staleness_finding(plan_fixtures.materialize("compliant", tmp_path))
    assert finding is not None
    assert finding.status == "pass", finding.detail
    assert gate.plan_is_stale_against_source() is False


def test_a_one_byte_tf_mutation_reds_plan_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _copy_of_real_infra(tmp_path)
    plan = _plan_with_digest(tmp_path, plan_fixtures.infra_digest(repo))
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    monkeypatch.setattr(
        gate, "compliant_fixture", lambda: json.loads(plan.read_text(encoding="utf-8"))
    )
    control = gate.plan_staleness_finding(plan)
    assert control is not None and control.status == "pass", "unmutated copy must pass"
    assert gate.plan_is_stale_against_source() is False

    target = repo / "infra" / "main.tf"
    target.write_bytes(target.read_bytes() + b"\n")
    finding = gate.plan_staleness_finding(plan)
    assert finding is not None
    assert finding.status == "deferred"
    assert "differs from the working tree" in finding.detail
    assert "captured_infra_digest" in finding.detail
    assert gate.plan_is_stale_against_source() is True


def test_an_untracked_tf_file_does_not_stale_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _copy_of_real_infra(tmp_path)
    plan = _plan_with_digest(tmp_path, plan_fixtures.infra_digest(repo))
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    (repo / "infra" / "scratch_untracked.tf").write_text('variable "x" {}\n', encoding="utf-8")
    finding = gate.plan_staleness_finding(plan)
    assert finding is not None and finding.status == "pass"


@pytest.mark.parametrize(
    ("digest", "drop"),
    [(None, True), ("", False), ("   ", False), (123, False)],
    ids=["absent", "empty", "blank", "not-a-string"],
)
def test_a_missing_digest_is_deferred_never_pass(
    tmp_path: Path, digest: object, drop: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan_with_digest(tmp_path, digest, drop=drop)
    finding = gate.plan_staleness_finding(plan)
    assert finding is not None
    assert finding.status == "deferred"
    assert "records no `_fixture_provenance.captured_infra_digest`" in finding.detail
    monkeypatch.setattr(
        gate, "compliant_fixture", lambda: json.loads(plan.read_text(encoding="utf-8"))
    )
    assert gate.plan_is_stale_against_source() is True


def test_a_different_recorded_digest_is_deferred(tmp_path: Path) -> None:
    finding = gate.plan_staleness_finding(_plan_with_digest(tmp_path, "0" * 64))
    assert finding is not None and finding.status == "deferred"


def test_an_uncomputable_tree_digest_is_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    plan = plan_fixtures.materialize("compliant", tmp_path / "plans")
    monkeypatch.setattr(gate, "REPO_ROOT", bare)
    finding = gate.plan_staleness_finding(plan)
    assert finding is not None and finding.status == "deferred"
    assert "could not be computed" in finding.detail
    assert gate.plan_is_stale_against_source() is True


def test_the_remediation_names_the_digest_and_no_commit() -> None:
    assert "captured_infra_digest" in gate._RECAPTURE_HINT
    assert "plan_fixtures.infra_digest" in gate._RECAPTURE_HINT
    assert "commit" not in gate._RECAPTURE_HINT
    assert "captured_infra_digest" in gate._ASSERTED_PROVENANCE_FIELDS
    assert "captured_from_commit" not in gate._ASSERTED_PROVENANCE_FIELDS


def test_the_copy_helper_copies_only_tracked_tf(tmp_path: Path) -> None:
    """Control on the fixture's resource subject: the scratch copy holds exactly the tracked .tf."""
    repo = _copy_of_real_infra(tmp_path)
    copied = sorted(
        str(path.relative_to(repo))
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    assert copied == _tracked_tf(REPO_ROOT)
    shutil.rmtree(repo)
