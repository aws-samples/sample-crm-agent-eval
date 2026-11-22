# SPDX-License-Identifier: MIT
"""Tests for `scripts.plan_fixtures`: `build`, `materialize` and the CLI; each plan is sha256-pinned.

Also covers `tests/unit/_terraform_fixture.terraform_unavailable_reason`, the skip declaration the
plan-time harness gives when the `terraform` on PATH cannot plan its fixture root.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from scripts import plan_fixtures, proc_run

from tests.unit import _terraform_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Plan keys no reader in the repository reads; absent from every built document.
UNREAD_KEYS = frozenset(
    {"planned_values", "prior_state", "output_changes", "relevant_attributes", "checks"}
)

#: sha256 of `json.dumps(build(name), sort_keys=True)` per name.
EXPECTED_SHA256 = {
    "compliant": "40f3d457e0935ac05656f13c1ec5a133d9e95397e24ee83b22040825acf429df",
    "compliant_gateway_role_known_arn": (
        "cc00ee686e5b28c72eb66597dfdc70c7afe4be545bbc28cacf2ddc391ef9f7ff"
    ),
    "violation_log_group_no_kms": (
        "ad04c61a58b5bcd642d2251e7ed5eee462ede7a4c59fa4edbf5414509c55b7d8"
    ),
    "violation_relay_function_url_buffered": (
        "83c6f753e53ec9a2324b2f103137830022db9a40c827f08cdd918c270d0b7698"
    ),
}


def _canonical_sha256(document: Any) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


def test_names_are_the_four_former_fixture_files() -> None:
    assert set(plan_fixtures.NAMES) == set(EXPECTED_SHA256)


@pytest.mark.parametrize("name", sorted(EXPECTED_SHA256))
def test_built_plan_matches_its_pinned_sha256(name: str) -> None:
    assert _canonical_sha256(plan_fixtures.build(name)) == EXPECTED_SHA256[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_SHA256))
def test_built_plan_carries_no_unread_key(name: str) -> None:
    assert not UNREAD_KEYS & set(plan_fixtures.build(name))


def test_build_returns_a_fresh_document_each_call() -> None:
    first = plan_fixtures.build("compliant")
    first["resource_changes"][0]["change"]["after"] = "mutated"
    first["_fixture_provenance"]["how"] = "mutated"
    second = plan_fixtures.build("compliant")
    assert second["resource_changes"][0]["change"]["after"] != "mutated"
    assert second["_fixture_provenance"]["how"] != "mutated"


def test_every_variant_differs_from_compliant() -> None:
    compliant = _canonical_sha256(plan_fixtures.build("compliant"))
    for name in plan_fixtures.NAMES:
        if name != "compliant":
            assert _canonical_sha256(plan_fixtures.build(name)) != compliant, name


def test_an_unknown_name_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown plan fixture"):
        plan_fixtures.build("violation_nonexistent")


def _walk_keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_the_base_carries_no_plan_marker_key_at_any_level() -> None:
    """Without `terraform_version` or `planned_values`, plan scanners do not read the base."""
    lines = plan_fixtures.BASE_PATH.read_text(encoding="utf-8").splitlines()
    keys = {key for line in lines for key in _walk_keys(json.loads(line))}
    assert "resource_changes" not in keys
    assert not {"terraform_version", "planned_values"} & keys
    assert set(json.loads(lines[0])) == plan_fixtures.HEADER_KEYS


def test_the_base_is_one_sorted_object_per_line() -> None:
    lines = plan_fixtures.BASE_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + len(plan_fixtures.build("compliant")["resource_changes"])
    for line in lines:
        assert line == json.dumps(json.loads(line), sort_keys=True)


def test_the_base_is_not_a_json_file() -> None:
    """Checkov's plan runner selects `*.json`; the base is `.ndjson` so it is never selected."""
    assert plan_fixtures.BASE_PATH.suffix == ".ndjson"


def _mutated_base(tmp_path: Path, edit: Any) -> Path:
    lines = plan_fixtures.BASE_PATH.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    edit(parsed)
    path = tmp_path / "compliant.base.ndjson"
    path.write_text("\n".join(json.dumps(p, sort_keys=True) for p in parsed) + "\n")
    return path


def _change(parsed: list[dict[str, Any]], address: str) -> dict[str, Any]:
    change: dict[str, Any] = next(p["change"] for p in parsed[1:] if p["address"] == address)
    return change


@pytest.mark.parametrize(
    ("name", "edit", "message"),
    [
        (
            "compliant_gateway_role_known_arn",
            lambda p: _change(p, "aws_iam_role.agentcore_gateway")["after_unknown"].pop("arn"),
            "arn is not plan-time unknown",
        ),
        (
            "violation_log_group_no_kms",
            lambda p: _change(p, "module.vpc.aws_cloudwatch_log_group.flow_logs[0]")[
                "after"
            ].update(kms_key_id="arn:aws:kms:us-east-1:123456789012:key/example"),
            "kms_key_id is not plan-time unknown",
        ),
        (
            "violation_relay_function_url_buffered",
            lambda p: _change(p, "module.agent_relay.aws_lambda_function_url.this")["after"].update(
                invoke_mode="BUFFERED"
            ),
            "invoke_mode is not RESPONSE_STREAM",
        ),
        (
            "violation_relay_function_url_buffered",
            lambda p: p.pop(
                next(
                    i
                    for i, rc in enumerate(p)
                    if rc.get("address") == "module.agent_relay.aws_lambda_function_url.this"
                )
            ),
            "expected exactly one resource_change",
        ),
        ("compliant", lambda p: p[0].pop("variables"), "header keys"),
    ],
    ids=["known-arn-base-drift", "no-kms-base-drift", "buffered-base-drift", "missing", "header"],
)
def test_a_base_that_no_longer_carries_the_mutated_field_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, edit: Any, message: str
) -> None:
    monkeypatch.setattr(plan_fixtures, "BASE_PATH", _mutated_base(tmp_path, edit))
    plan_fixtures._base_lines.cache_clear()
    try:
        with pytest.raises(ValueError, match=message):
            plan_fixtures.build(name)
    finally:
        plan_fixtures._base_lines.cache_clear()


def test_the_known_arn_variant_leaves_the_chat_gateway_grants_unknown() -> None:
    plan = plan_fixtures.build("compliant_gateway_role_known_arn")
    grants = {
        rc["address"]: rc["change"]
        for rc in plan["resource_changes"]
        if rc["address"].endswith("aws_lambda_permission.gateway_target_role[0]")
    }
    known = {
        a
        for a, c in grants.items()
        if c["after"].get("principal") == plan_fixtures.GATEWAY_ROLE_ARN
    }
    unknown = {a for a, c in grants.items() if c["after_unknown"].get("principal") is True}
    assert len(known) == 28
    assert unknown and all("chat" in address for address in unknown)
    assert known.isdisjoint(unknown)


def test_materialize_writes_the_built_document_and_skips_an_identical_rewrite(
    tmp_path: Path,
) -> None:
    path = plan_fixtures.materialize("violation_log_group_no_kms", tmp_path)
    assert path == tmp_path / "violation_log_group_no_kms.json"
    assert json.loads(path.read_text()) == plan_fixtures.build("violation_log_group_no_kms")
    before = path.stat().st_mtime_ns
    assert plan_fixtures.materialize("violation_log_group_no_kms", tmp_path) == path
    assert path.stat().st_mtime_ns == before
    path.write_text("{}")
    plan_fixtures.materialize("violation_log_group_no_kms", tmp_path)
    assert json.loads(path.read_text()) == plan_fixtures.build("violation_log_group_no_kms")


def test_materialize_defaults_to_the_gitignored_results_directory() -> None:
    assert plan_fixtures.DEFAULT_OUT_DIR == REPO_ROOT / "results" / "plan-fixtures"
    probe = "results/plan-fixtures/compliant.json"
    result = proc_run.run(
        ["git", "check-ignore", "-q", probe],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"{probe} is not gitignored"


def _materialize_in_child(out_dir: str) -> str:
    return str(plan_fixtures.materialize("compliant", Path(out_dir)))


def test_concurrent_writers_never_expose_a_partial_or_temporary_file(tmp_path: Path) -> None:
    """Writers race to replace a stale file while the parent reads it: every read is the stale
    content or the complete document, and no temporary file is left behind."""
    expected = plan_fixtures.build("compliant")
    stale = '{"stale": true}'
    for round_number in range(3):
        out = tmp_path / f"round-{round_number}"
        out.mkdir()
        target = out / "compliant.json"
        target.write_text(stale)
        reads = 0
        with ProcessPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(_materialize_in_child, str(out)) for _ in range(12)]
            while not all(f.done() for f in futures):
                text = target.read_text()
                assert text == stale or json.loads(text) == expected
                reads += 1
            paths = {f.result() for f in futures}
        assert reads > 0
        assert paths == {str(target)}
        assert json.loads(target.read_text()) == expected
        assert sorted(p.name for p in out.iterdir()) == ["compliant.json"]


def test_the_cli_materializes_the_named_fixtures(tmp_path: Path) -> None:
    result = proc_run.run(
        [sys.executable, "scripts/plan_fixtures.py", "--out", str(tmp_path), "compliant"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [str(tmp_path / "compliant.json")]
    refused = proc_run.run(
        [sys.executable, "scripts/plan_fixtures.py", "--out", str(tmp_path), "nope"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 2
    assert "unknown fixture name" in refused.stderr


def _checkov_failed(checkov: str, path: Path, cwd: Path) -> int:
    """Failed terraform_plan checks Checkov reports for `path`, run outside the repository so no
    local Checkov config applies."""
    result = proc_run.run(
        [checkov, "-f", str(path), "--framework", "terraform_plan", "-o", "json", "--quiet"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    reports = payload if isinstance(payload, list) else [payload]
    return sum(int((r.get("summary") or r).get("failed", 0)) for r in reports)


def _with_planned_values(plan: dict[str, Any]) -> dict[str, Any]:
    """`plan` plus a flat `planned_values` view synthesized from each change's `after`."""
    resources = [
        {key: rc[key] for key in ("address", "mode", "type", "name", "provider_name")}
        | {"values": rc["change"]["after"]}
        for rc in plan["resource_changes"]
        if rc["mode"] == "managed" and isinstance(rc["change"]["after"], dict)
    ]
    return plan | {"planned_values": {"root_module": {"resources": resources}}}


def test_neither_the_base_nor_a_built_plan_has_a_failed_checkov_check(tmp_path: Path) -> None:
    """The base (even renamed `.json`) and a materialized plan yield no failed check; the control,
    the same plan with a `planned_values` view added back, does, so the scanner reads plans here."""
    checkov = shutil.which("checkov") or "/opt/homebrew/bin/checkov"
    if not Path(checkov).exists():
        pytest.skip("checkov is not installed on this host")
    renamed = tmp_path / "base.json"
    shutil.copyfile(plan_fixtures.BASE_PATH, renamed)
    built = plan_fixtures.materialize("compliant", tmp_path / "built")
    control = tmp_path / "control.json"
    control.write_text(json.dumps(_with_planned_values(plan_fixtures.build("compliant"))))
    assert _checkov_failed(checkov, control, tmp_path) > 0
    assert _checkov_failed(checkov, built, tmp_path) == 0
    assert _checkov_failed(checkov, renamed, tmp_path) == 0


# ---------------------------------------------------------------------------
# The plan-time harness declares an unusable terraform as a skip reason.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version_json", "expected"),
    [
        ('{"terraform_version": "1.17.0"}', (1, 17, 0)),
        ('{"terraform_version": "1.5.7", "terraform_outdated": true}', (1, 5, 7)),
        ('{"terraform_version": "1.18.0-beta1"}', (1, 18, 0)),
        ('{"terraform_version": "1.17"}', None),
        ('{"version": "1.17.0"}', None),
        ("Terraform v1.17.0", None),
        ("[]", None),
    ],
)
def test_the_version_parser_reads_only_a_full_semver(
    version_json: str, expected: tuple[int, int, int] | None
) -> None:
    assert _terraform_fixture.parse_terraform_version(version_json) == expected


def _fake_terraform(tmp_path: Path, stdout: str, exit_code: int = 0) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "terraform"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{stdout}'\nexit {exit_code}\n", encoding="utf-8")
    script.chmod(0o755)
    return bin_dir


@pytest.fixture
def _fresh_version_cache() -> Iterator[None]:
    _terraform_fixture._path_terraform_version_reason.cache_clear()
    yield
    _terraform_fixture._path_terraform_version_reason.cache_clear()


@pytest.mark.usefixtures("_fresh_version_cache")
@pytest.mark.parametrize(
    ("stdout", "exit_code", "fragment"),
    [
        ('{"terraform_version": "1.5.7"}', 0, "is 1.5.7; the fixture root requires >= 1.17.0"),
        ("not json", 0, "reported no readable version (exit 0)"),
        ('{"terraform_version": "1.17.0"}', 1, "reported no readable version (exit 1)"),
    ],
    ids=["too_old", "unreadable", "nonzero_exit"],
)
def test_an_unusable_path_terraform_is_a_declared_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, exit_code: int, fragment: str
) -> None:
    monkeypatch.setenv("PATH", str(_fake_terraform(tmp_path, stdout, exit_code)))
    reason = _terraform_fixture.terraform_unavailable_reason()
    assert reason is not None
    assert fragment in reason


@pytest.mark.usefixtures("_fresh_version_cache")
def test_a_new_enough_path_terraform_falls_through_to_the_plugin_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(_fake_terraform(tmp_path, '{"terraform_version": "1.17.0"}')))
    monkeypatch.setattr(_terraform_fixture, "PLUGIN_DIR", tmp_path / "no-providers")
    reason = _terraform_fixture.terraform_unavailable_reason()
    assert reason is not None
    assert "no cached provider plugins" in reason


def test_the_fixture_root_pins_the_minimum_the_reason_names() -> None:
    minimum = ".".join(map(str, _terraform_fixture.MINIMUM_TERRAFORM_VERSION))
    assert f'required_version = ">= {minimum}"' in _terraform_fixture._PROVIDER_BLOCK
