# SPDX-License-Identifier: MIT
"""Tests for `scripts.outputs_provenance`, the freshness predicate and shape contract for outputs.

Each test that needs a repository gets its own throwaway git repo, with `REPO_ROOT` and `INFRA_DIR`
monkeypatched, so none of them reads or writes the real tree.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
import scripts.outputs_provenance as provenance
from scripts import proc_run
from scripts.outputs_provenance import (
    OUTPUT_SHAPE_CONTRACT,
    PROVENANCE_KEY,
    Freshness,
    assess_freshness,
    check_output_shapes,
    classify_shape,
    infra_changes_since,
    main,
    read_provenance,
    stamp_document,
)

_REAL_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEPLOY_SH: Final[Path] = _REAL_REPO_ROOT / "scripts" / "deploy.sh"
_PLAYWRIGHT_CONFIG: Final[Path] = (
    _REAL_REPO_ROOT / "tests" / "e2e" / "browser" / "playwright.config.ts"
)


def _run(repo: Path, *args: str) -> None:
    proc_run.run(
        ["git", "--no-pager", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throwaway git repository with one `infra/main.tf`, wired in as the module's REPO_ROOT."""
    root = tmp_path / "clone"
    (root / "infra").mkdir(parents=True)
    (root / "infra" / "main.tf").write_text('resource "null_resource" "a" {}\n', encoding="utf-8")
    _run(root, "init", "--quiet")
    _run(root, "add", "-A")
    _run(root, "commit", "--quiet", "-m", "initial")
    monkeypatch.setattr(provenance, "REPO_ROOT", root)
    monkeypatch.setattr(provenance, "INFRA_DIR", root / "infra")
    yield root


def _head(repo: Path) -> str:
    return str(
        proc_run.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout
    ).strip()


def _write_snapshot(repo: Path, *, commit: str | None, dirty: bool = False) -> Path:
    snapshot = repo / "results" / "tf-outputs.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    document = stamp_document({"api_endpoint": "https://x"}, commit=commit, dirty=dirty)
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    return snapshot


def _flatten_mtimes(repo: Path, stamp: float = 1_700_000_000.0) -> None:
    """Give every file one identical mtime, reproducing a fresh checkout."""
    for path in repo.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))


# ---------------------------------------------------------------------------
# THE CONTROL: the staleness the mtime form structurally cannot see
# ---------------------------------------------------------------------------


def test_the_fresh_clone_case_the_mtime_form_cannot_see(repo: Path) -> None:
    """A stale snapshot with fresh-clone mtimes passes the mtime form but not the commit form."""
    captured_at_commit = _head(repo)
    snapshot = _write_snapshot(repo, commit=captured_at_commit)

    # infra/ moves on after the snapshot was captured -- real staleness.
    (repo / "infra" / "main.tf").write_text('resource "null_resource" "b" {}\n', encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "--quiet", "-m", "change infra")

    # ...and then a fresh clone flattens every mtime to one value.
    _flatten_mtimes(repo)

    mtime_verdict = provenance._mtime_freshness(snapshot)
    assert mtime_verdict.is_fresh, (
        "precondition of this control: the mtime comparison must report FRESH here, otherwise it is "
        "not reproducing the blind spot"
    )

    commit_verdict = assess_freshness(snapshot)
    assert commit_verdict.form == "commit"
    assert not commit_verdict.is_fresh
    assert commit_verdict.refusal is not None
    assert "STALE" in commit_verdict.refusal
    assert "infra/main.tf" in commit_verdict.refusal


def test_the_cp_case_the_mtime_form_also_cannot_see(repo: Path) -> None:
    """A stale snapshot given a new mtime passes the mtime form but not the commit form."""
    captured_at_commit = _head(repo)
    snapshot = _write_snapshot(repo, commit=captured_at_commit)
    (repo / "infra" / "main.tf").write_text('resource "null_resource" "b" {}\n', encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "--quiet", "-m", "change infra")

    os.utime(snapshot, None)  # what `cp` does to the mtime

    assert provenance._mtime_freshness(snapshot).is_fresh
    assert not assess_freshness(snapshot).is_fresh


def test_a_current_snapshot_is_accepted_by_the_commit_form(repo: Path) -> None:
    """A snapshot stamped with the current HEAD is fresh under the commit form."""
    snapshot = _write_snapshot(repo, commit=_head(repo))
    verdict = assess_freshness(snapshot)
    assert verdict.is_fresh, verdict.refusal
    assert verdict.form == "commit"
    assert not verdict.used_weaker_form


def test_an_uncommitted_infra_edit_makes_the_snapshot_stale(repo: Path) -> None:
    """An uncommitted `.tf` edit in the working tree makes the snapshot stale."""
    snapshot = _write_snapshot(repo, commit=_head(repo))
    (repo / "infra" / "main.tf").write_text("changed\n", encoding="utf-8")
    assert not assess_freshness(snapshot).is_fresh


def test_an_untracked_tf_file_makes_the_snapshot_stale(repo: Path) -> None:
    """An untracked `.tf` file under `infra/` makes the snapshot stale."""
    snapshot = _write_snapshot(repo, commit=_head(repo))
    (repo / "infra" / "new_module.tf").write_text('resource "null_resource" "c" {}\n', "utf-8")
    assert not assess_freshness(snapshot).is_fresh


def test_a_root_level_tf_edit_is_detected(repo: Path) -> None:
    """An edit to a `.tf` file directly under `infra/` is reported as a change."""
    snapshot = _write_snapshot(repo, commit=_head(repo))
    (repo / "infra" / "main.tf").write_text("root level edit\n", encoding="utf-8")
    resolvable, changed, _ = infra_changes_since(_head(repo))
    assert resolvable
    assert changed == ["infra/main.tf"]
    assert not assess_freshness(snapshot).is_fresh


def test_a_non_tf_change_under_infra_does_not_make_it_stale(repo: Path) -> None:
    """A non-`.tf` change under `infra/` leaves the snapshot fresh."""
    snapshot = _write_snapshot(repo, commit=_head(repo))
    (repo / "infra" / "README.md").write_text("notes\n", encoding="utf-8")
    assert assess_freshness(snapshot).is_fresh


def test_the_provider_cache_is_excluded(repo: Path) -> None:
    """`.tf` files under `infra/.terraform/` are ignored."""
    snapshot = _write_snapshot(repo, commit=_head(repo))
    cache = repo / "infra" / ".terraform" / "providers" / "vendored.tf"
    cache.parent.mkdir(parents=True)
    cache.write_text('resource "x" "y" {}\n', encoding="utf-8")
    assert assess_freshness(snapshot).is_fresh


# ---------------------------------------------------------------------------
# Fail-safe: an unanswerable comparison is STALE, never fresh
# ---------------------------------------------------------------------------


def test_an_unresolvable_commit_is_treated_as_stale(repo: Path) -> None:
    """A captured commit that does not resolve makes the snapshot stale."""
    snapshot = _write_snapshot(repo, commit="0" * 40)
    verdict = assess_freshness(snapshot)
    assert not verdict.is_fresh
    assert verdict.refusal is not None
    assert "does not resolve" in verdict.refusal


def test_a_snapshot_captured_from_a_dirty_tree_is_stale(repo: Path) -> None:
    """A snapshot stamped as captured from a dirty tree is stale."""
    snapshot = _write_snapshot(repo, commit=_head(repo), dirty=True)
    verdict = assess_freshness(snapshot)
    assert not verdict.is_fresh
    assert verdict.refusal is not None
    assert "DIRTY" in verdict.refusal


def test_an_absent_snapshot_is_refused_by_name(repo: Path) -> None:
    verdict = assess_freshness(repo / "results" / "nope.json")
    assert verdict.refusal is not None
    assert "does not exist" in verdict.refusal


def test_a_malformed_snapshot_falls_back_rather_than_crashing(repo: Path) -> None:
    """Unparseable JSON falls back to the mtime form rather than raising."""
    snapshot = repo / "results" / "tf-outputs.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("{not json", encoding="utf-8")
    verdict = assess_freshness(snapshot)
    assert verdict.form == "mtime"


# ---------------------------------------------------------------------------
# The fallback is declared, never silent
# ---------------------------------------------------------------------------


def test_a_snapshot_with_no_provenance_uses_the_weaker_form_and_declares_it(repo: Path) -> None:
    """A snapshot with no provenance uses the mtime form and labels the basis as weaker."""
    snapshot = repo / "results" / "tf-outputs.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps({"api_endpoint": "https://x"}), encoding="utf-8")
    verdict = assess_freshness(snapshot)
    assert verdict.form == "mtime"
    assert verdict.used_weaker_form
    assert "MTIME" in verdict.basis
    assert "fresh clone" in verdict.basis


def test_a_null_commit_is_recorded_and_falls_back(repo: Path) -> None:
    """A snapshot that could not name its commit records that fact rather than omitting the field."""
    snapshot = _write_snapshot(repo, commit=None)
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    assert read_provenance(document) is not None
    assert read_provenance(document)["captured_from_commit"] is None  # type: ignore[index]
    assert assess_freshness(snapshot).form == "mtime"


def test_the_commit_form_pass_is_not_labelled_weak(repo: Path) -> None:
    assert not assess_freshness(_write_snapshot(repo, commit=_head(repo))).used_weaker_form


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_stamp_document_does_not_mutate_its_input() -> None:
    original = {"api_endpoint": "https://x"}
    stamped = stamp_document(original, commit="abc", dirty=False)
    assert PROVENANCE_KEY not in original
    assert stamped[PROVENANCE_KEY]["captured_from_commit"] == "abc"


def test_the_provenance_key_cannot_collide_with_a_terraform_output() -> None:
    """`PROVENANCE_KEY` starts with an underscore, which no Terraform output name can."""
    assert PROVENANCE_KEY.startswith("_")


def test_stamping_preserves_every_existing_output(repo: Path) -> None:
    snapshot = repo / "results" / "tf-outputs.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Any] = {
        "api_endpoint": {"value": "https://x", "type": "string", "sensitive": False},
        "agent_relay_function_name": {"value": "fn", "type": "string", "sensitive": False},
    }
    snapshot.write_text(json.dumps(outputs), encoding="utf-8")
    assert main(["--stamp", str(snapshot)]) == 0
    reloaded = json.loads(snapshot.read_text(encoding="utf-8"))
    assert reloaded["api_endpoint"] == outputs["api_endpoint"]
    assert reloaded["agent_relay_function_name"] == outputs["agent_relay_function_name"]
    assert read_provenance(reloaded) is not None


def test_stamping_refuses_a_document_that_is_not_a_json_object(repo: Path) -> None:
    """`--stamp` exits 1 on a document that is not a JSON object."""
    snapshot = repo / "results" / "tf-outputs.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("[]", encoding="utf-8")
    assert main(["--stamp", str(snapshot)]) == 1


def test_the_stamped_snapshot_survives_the_smoke_harness_loader(repo: Path) -> None:
    """The smoke harness's `load_outputs` reads a stamped snapshot with its outputs intact."""
    from scripts.smoke.harness import load_outputs

    snapshot = repo / "results" / "tf-outputs.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps({"api_endpoint": {"value": "https://x", "type": "string"}}), encoding="utf-8"
    )
    assert main(["--stamp", str(snapshot)]) == 0
    loaded = load_outputs(snapshot)
    assert loaded.require_str("api_endpoint") == "https://x"
    assert PROVENANCE_KEY in loaded.values


# ---------------------------------------------------------------------------
# The CLI contract the TypeScript reader depends on
# ---------------------------------------------------------------------------


def test_the_check_cli_exits_three_on_a_stale_snapshot(repo: Path) -> None:
    """`--check` exits 3 (verified nothing) on a stale snapshot."""
    snapshot = _write_snapshot(repo, commit="0" * 40)
    assert main(["--check", str(snapshot)]) == 3


def test_the_check_cli_exits_zero_on_a_current_snapshot(repo: Path) -> None:
    assert main(["--check", str(_write_snapshot(repo, commit=_head(repo)))]) == 0


def test_the_check_cli_json_shape_is_what_the_config_reads(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--check --json` emits exactly the keys `playwright.config.ts` parses."""
    snapshot = _write_snapshot(repo, commit=_head(repo))
    main(["--check", str(snapshot), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"fresh", "form", "basis", "refusal", "used_weaker_form"}
    assert payload["fresh"] is True
    assert payload["form"] == "commit"


# ---------------------------------------------------------------------------
# Both consumers go through outputs_provenance
# ---------------------------------------------------------------------------


def test_deploy_sh_stamps_the_snapshot_provenance() -> None:
    text = _DEPLOY_SH.read_text(encoding="utf-8")
    assert "outputs_provenance.py --stamp" in text, (
        "scripts/deploy.sh must stamp the snapshot's provenance, or every reader falls back to the "
        "mtime comparison this change exists to retire"
    )


def test_the_playwright_config_consults_outputs_provenance() -> None:
    text = _PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
    assert "outputs_provenance" in text, (
        "playwright.config.ts must consult scripts/outputs_provenance.py rather than carrying a "
        "second implementation of the freshness comparison"
    )


def test_freshness_is_a_value_not_a_bare_boolean() -> None:
    """`Freshness` reports which form answered as well as whether the snapshot is fresh."""
    verdict = Freshness(refusal=None, form="mtime", basis="x")
    assert verdict.is_fresh
    assert verdict.used_weaker_form


# ---------------------------------------------------------------------------
# Output shape contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("https://z813hfzx92.execute-api.us-east-1.amazonaws.com", "url"),
        ("z813hfzx92.execute-api.us-east-1.amazonaws.com", "hostname"),
        ("arn:aws:iam::123456789012:role/r", "arn"),
        ("s3://bucket/key", "s3_uri"),
        ("/aws/lambda/crm-agent-eval-agent-relay", "log_group_path"),
        ("crm-agent-eval-agent-relay", "identifier"),
        ("E2QD1ZBMJWIWFP", "identifier"),
        ({"accounts": "crm-agent-evalaccounts"}, "mapping"),
        ("", "empty"),
    ],
)
def test_shape_classification(value: object, shape: str) -> None:
    assert classify_shape(value) == shape


def test_a_url_where_a_hostname_is_contracted_is_a_violation() -> None:
    """A URL value for the hostname-contracted `api_endpoint` is a violation."""
    report = check_output_shapes(
        {"api_endpoint": "https://z813hfzx92.execute-api.us-east-1.amazonaws.com"}
    )
    assert not report.ok
    assert "api_endpoint" in report.violations[0]
    assert "expected a 'hostname' value, found 'url'" in report.violations[0]


def test_the_repointed_hostname_satisfies_the_contract() -> None:
    """A hostname value for `api_endpoint` satisfies the contract."""
    report = check_output_shapes({"api_endpoint": "z813hfzx92.execute-api.us-east-1.amazonaws.com"})
    assert report.ok, report.violations


def test_a_violation_never_echoes_the_value() -> None:
    """A violation message withholds the offending value.

    The planted account id is assembled at runtime so no tracked line carries the 12-digit shape
    `check_no_account_id` refuses.
    """
    planted_account = "9" * 6 + "8" * 3 + "7" * 3
    secret = f"arn:aws:iam::{planted_account}:role/should-never-appear"
    report = check_output_shapes({"api_endpoint": secret})
    assert not report.ok
    assert secret not in report.violations[0]
    assert planted_account not in report.violations[0]
    assert "withheld" in report.violations[0]


def test_an_absent_contracted_output_is_reported_separately_from_a_mismatch() -> None:
    """A contracted output that is absent is listed in `absent`, not as a violation."""
    report = check_output_shapes({"api_endpoint": "host.example.com"})
    assert report.ok
    assert "api_id" in report.absent


def test_an_unlisted_output_is_unconstrained_but_counted() -> None:
    """An output not in the contract passes and is counted as unconstrained."""
    report = check_output_shapes({"some_brand_new_output": "whatever"})
    assert report.ok
    assert report.unconstrained == 1
    assert report.checked == 0


def test_the_shape_cli_exits_three_when_it_checked_nothing(tmp_path: Path) -> None:
    """`--check-shapes` exits 3 when no contracted output was present."""
    snapshot = tmp_path / "tf-outputs.json"
    snapshot.write_text(json.dumps({"unrelated": "x"}), encoding="utf-8")
    assert main(["--check-shapes", str(snapshot)]) == 3


def test_the_shape_cli_exits_one_on_a_semantics_change(tmp_path: Path) -> None:
    snapshot = tmp_path / "tf-outputs.json"
    snapshot.write_text(
        json.dumps(
            {"api_endpoint": {"value": "https://x.example.com"}, "api_id": {"value": "abc"}}
        ),
        encoding="utf-8",
    )
    assert main(["--check-shapes", str(snapshot)]) == 1


def test_the_shape_cli_reads_both_output_document_shapes(tmp_path: Path) -> None:
    """`terraform output -json` wrapping and an already-flattened mapping must both work."""
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"api_endpoint": {"value": "h.example.com"}}), encoding="utf-8")
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"api_endpoint": "h.example.com"}), encoding="utf-8")
    assert main(["--check-shapes", str(wrapped)]) == 0
    assert main(["--check-shapes", str(flat)]) == 0


def test_every_contracted_name_is_a_real_output_name() -> None:
    """Every name in `OUTPUT_SHAPE_CONTRACT` is declared in `infra/outputs.tf`."""
    declared = set(
        re.findall(
            r'^output\s+"([^"]+)"',
            (_REAL_REPO_ROOT / "infra" / "outputs.tf").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    assert declared, (
        "no outputs parsed from infra/outputs.tf; the discriminator has stopped selecting"
    )
    unknown = sorted(set(OUTPUT_SHAPE_CONTRACT) - declared)
    assert unknown == [], f"contracted output(s) not declared in infra/outputs.tf: {unknown}"


def test_the_how_string_names_the_recorder(repo: Path) -> None:
    """`--recorded-by refresh` names scripts/refresh_outputs.sh; the default stays deploy.sh's."""
    snapshot = repo / "results" / "tf-outputs.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps({"api_endpoint": "https://x"}), encoding="utf-8")
    assert main(["--stamp", str(snapshot), "--recorded-by", "refresh"]) == 0
    how = read_provenance(json.loads(snapshot.read_text(encoding="utf-8")))["how"]  # type: ignore[index]
    assert "scripts/refresh_outputs.sh" in how and "no apply" in how
    assert "deploy.sh" not in how

    snapshot.write_text(json.dumps({"api_endpoint": "https://x"}), encoding="utf-8")
    assert main(["--stamp", str(snapshot)]) == 0
    how = read_provenance(json.loads(snapshot.read_text(encoding="utf-8")))["how"]  # type: ignore[index]
    assert how == provenance.HOW_BY_RECORDER["deploy"] and "scripts/deploy.sh" in how


def test_each_recorder_script_stamps_with_its_own_how() -> None:
    deploy_text = _DEPLOY_SH.read_text(encoding="utf-8")
    refresh_text = (_REAL_REPO_ROOT / "scripts" / "refresh_outputs.sh").read_text(encoding="utf-8")
    assert re.search(r"outputs_provenance\.py\" --stamp [^\n]*\n", deploy_text)
    assert "--recorded-by" not in deploy_text
    assert "--recorded-by refresh" in refresh_text
