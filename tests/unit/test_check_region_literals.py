# SPDX-License-Identifier: MIT
"""`scripts/check_region_literals.py`: every cell fails without the rule it names.

Scratch git repositories stand in for the tree; one real-tree control runs `--check literals` on
this repository.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import check_region_literals as gate
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
LITERAL = gate.REGION_LITERAL


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    proc_run.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    if files:
        proc_run.run(["git", "-C", str(root), "add", "--", *files], check=True, capture_output=True)
    return root


def _pin(count: int) -> gate.Exemption:
    return gate.Exemption(count, "default", "test pin")


def _literals(
    tmp_path: Path, files: dict[str, str], exempt: dict[str, gate.Exemption]
) -> gate.CheckResult:
    root = _git_repo(tmp_path / "repo", files)
    return gate.check_literals(root, gate.tracked_files(root), exempt)


def _main(
    root: Path,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exempt: dict[str, gate.Exemption] | None = None,
) -> tuple[int, str]:
    monkeypatch.setattr(gate, "EXEMPT_FILES", exempt or {})
    rc = gate.main(["--root", str(root), *argv])
    return rc, capsys.readouterr().out


# --- literals --------------------------------------------------------------------------------


def test_a_new_literal_in_a_scanned_file_fails_and_names_its_line(tmp_path: Path) -> None:
    result = _literals(tmp_path, {"src/a.py": f'x = 1\nREGION = "{LITERAL}"\n'}, {})
    assert result.failed
    assert "src/a.py: 1 region literal(s) or AZ id(s) at line(s) [2]" in result.lines[0]


def test_the_same_file_without_the_literal_passes(tmp_path: Path) -> None:
    result = _literals(tmp_path, {"src/a.py": 'REGION = os.environ["AWS_REGION"]\n'}, {})
    assert not result.failed, result.lines
    assert result.lines[0].startswith("[PASS] literals: 1 tracked file(s) scanned")


def test_a_pin_equal_to_the_hit_count_passes(tmp_path: Path) -> None:
    result = _literals(tmp_path, {"src/a.py": f"{LITERAL} {LITERAL}\n"}, {"src/a.py": _pin(2)})
    assert not result.failed, result.lines


@pytest.mark.parametrize(("hits", "pin"), [(3, 2), (1, 2)], ids=["plus_one", "minus_one"])
def test_a_count_off_its_pin_either_way_fails(tmp_path: Path, hits: int, pin: int) -> None:
    body = "\n".join([LITERAL] * hits) + "\n"
    result = _literals(tmp_path, {"src/a.py": body}, {"src/a.py": _pin(pin)})
    assert result.failed
    assert f"src/a.py: {hits} hit(s)" in result.lines[0]
    assert f"pinned {pin} (default)" in result.lines[0]


def test_two_literals_on_one_line_count_twice(tmp_path: Path) -> None:
    """The pin counts occurrences, so a second literal on a pinned line is still caught."""
    result = _literals(tmp_path, {"src/a.py": f"{LITERAL}/{LITERAL}\n"}, {"src/a.py": _pin(1)})
    assert result.failed


def test_a_literal_under_tests_is_ignored(tmp_path: Path) -> None:
    files = {"tests/unit/test_a.py": f"{LITERAL}\n", "src/ok.py": "x = 1\n"}
    result = _literals(tmp_path, files, {})
    assert not result.failed, result.lines


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/runbook.md",
        "src/relay/tests/a.test.ts",
        "frontend/src/x/y.test.tsx",
        "frontend/a.test.ts",
        "frontend/src/mocks/data.ts",
        "frontend/src/test/setup.ts",
        "docs/figures/a.drawio",
        "results/out.json",
        "infra/regions.json",
    ],
)
def test_every_exempt_class_is_exempt(path: str) -> None:
    assert gate.is_class_exempt(path)


@pytest.mark.parametrize(
    "path", ["src/a.py", "scripts/x.sh", "infra/main.tf", "frontend/src/a.ts", "testsx/a.py"]
)
def test_code_iac_and_config_paths_are_scanned(path: str) -> None:
    assert not gate.is_class_exempt(path)


def test_an_az_id_outside_the_map_fails(tmp_path: Path) -> None:
    result = _literals(tmp_path, {"infra/vpc.tf": 'zones = ["usw2-az1"]\n'}, {})
    assert result.failed
    assert "infra/vpc.tf" in result.lines[0]


def test_the_same_az_id_inside_the_map_passes(tmp_path: Path) -> None:
    result = _literals(
        tmp_path, {"infra/regions.json": '["usw2-az1"]\n', "infra/vpc.tf": "x = 1\n"}, {}
    )
    assert not result.failed, result.lines


def test_a_pin_for_a_file_that_is_not_scanned_fails(tmp_path: Path) -> None:
    result = _literals(tmp_path, {"src/a.py": "x = 1\n"}, {"src/gone.py": _pin(1)})
    assert result.failed
    assert "src/gone.py: pinned in EXEMPT_FILES but not a scanned file" in result.lines[0]


def test_a_pin_with_an_unknown_category_fails(tmp_path: Path) -> None:
    exempt = {"src/a.py": gate.Exemption(1, "because", "x")}
    result = _literals(tmp_path, {"src/a.py": f"{LITERAL}\n"}, exempt)
    assert result.failed
    assert "unknown category 'because'" in result.lines[0]


def test_every_real_pin_has_a_known_category_and_a_reason() -> None:
    for path, pin in gate.EXEMPT_FILES.items():
        assert pin.category in gate.CATEGORIES, path
        assert pin.reason.strip(), path
        assert pin.count > 0, path


# --- phrases ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase", [f"{LITERAL} only", f"{LITERAL.upper()} ONLY", "the only supported region"]
)
def test_a_banned_phrase_in_markdown_fails(
    tmp_path: Path, phrase: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _git_repo(tmp_path / "repo", {"docs/a.md": f"# Title\n\nDeploys to {phrase}.\n"})
    rc, out = _main(root, ["--check", "phrases"], monkeypatch, capsys)
    assert rc == 1
    assert "[FAIL] phrases: docs/a.md:3: banned phrase" in out


def test_the_supported_wording_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = f"Default and tested region: {LITERAL}; supported: the regions in infra/regions.json.\n"
    root = _git_repo(tmp_path / "repo", {"docs/a.md": body})
    rc, out = _main(root, ["--check", "phrases"], monkeypatch, capsys)
    assert rc == 0, out


# --- plan ------------------------------------------------------------------------------------


def _plan(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _variable_default(value: str) -> dict[str, object]:
    return {"variables": {"aws_region": {"default": value}}}


@pytest.mark.parametrize(
    "configuration",
    [
        {"root_module": _variable_default(LITERAL)},
        {"root_module": {"module_calls": {"agentcore": {"module": _variable_default(LITERAL)}}}},
    ],
    ids=["root_module", "module_call"],
)
def test_plan_a_variable_default_on_an_allowed_path_passes_and_is_printed(
    tmp_path: Path, configuration: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan(tmp_path, {"format_version": "1.2", "configuration": configuration})
    rc = gate.main(["--check", "plan", "--plan", str(plan)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[ALLOWED] plan:" in out
    assert f"variables.aws_region.default = '{LITERAL}'" in out


def test_plan_a_resource_attribute_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = {
        "configuration": {"root_module": _variable_default(LITERAL)},
        "planned_values": {
            "root_module": {
                "resources": [
                    {"values": {"arn": f"arn:aws:dynamodb:{LITERAL}:123456789012:table/x"}}
                ]
            }
        },
    }
    rc = gate.main(["--check", "plan", "--plan", str(_plan(tmp_path, document))])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] plan:" in out
    assert "planned_values.root_module.resources.0.values.arn" in out


def test_plan_a_value_outside_the_allowed_variable_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configuration = {"root_module": {"variables": {"aws_region": {"description": LITERAL}}}}
    rc = gate.main(
        ["--plan", str(_plan(tmp_path, {"configuration": configuration})), "--check", "plan"]
    )
    assert rc == 1, capsys.readouterr().out


def test_plan_without_a_plan_file_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["--check", "plan"]) == 2
    assert "--check plan needs" in capsys.readouterr().err


def test_an_unreadable_plan_is_an_environment_error(tmp_path: Path) -> None:
    assert gate.main(["--check", "plan", "--plan", str(tmp_path / "absent.json")]) == 2


# --- verified nothing ------------------------------------------------------------------------


def test_an_empty_literal_scope_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _git_repo(tmp_path / "repo", {"tests/a.py": f"{LITERAL}\n"})
    rc, out = _main(root, ["--check", "literals"], monkeypatch, capsys, {"src/a.py": _pin(1)})
    assert rc == gate.SKIP_EXIT_STATUS
    assert "[SKIP]" in out


def test_no_tracked_markdown_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _git_repo(tmp_path / "repo", {"src/a.py": "x = 1\n"})
    rc, _ = _main(root, ["--check", "phrases"], monkeypatch, capsys)
    assert rc == gate.SKIP_EXIT_STATUS


def test_a_plan_with_no_strings_exits_3(tmp_path: Path) -> None:
    assert gate.main(["--check", "plan", "--plan", str(_plan(tmp_path, {"a": [1, 2]}))]) == 3


def test_a_directory_that_is_not_a_git_work_tree_is_an_error(tmp_path: Path) -> None:
    assert gate.main(["--root", str(tmp_path), "--check", "literals"]) == 2


# --- registration and real tree ---------------------------------------------------------------


def _run_gates() -> ModuleType:
    path = REPO_ROOT / "scripts" / "run_gates.py"
    spec = importlib.util.spec_from_file_location("run_gates_for_region_literals", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_both_gates_are_registered_with_their_checks() -> None:
    registered = dict(_run_gates().GATES)
    literals = registered["check_region_literals"]
    assert literals[1:] == [
        "scripts/check_region_literals.py",
        "--check",
        "literals",
        "--check",
        "phrases",
    ]
    assert registered["check_region_map"][1:] == ["scripts/region_map.py", "check"]


def test_the_gate_declares_the_shared_skip_status() -> None:
    assert gate.SKIP_EXIT_STATUS == _run_gates().GATE_SKIP_EXIT_STATUS


def test_literals_pass_on_this_repository(capsys: pytest.CaptureFixture[str]) -> None:
    """Real-tree control: every pin in `EXEMPT_FILES` matches the tracked tree exactly."""
    rc = gate.main(["--check", "literals"])
    assert rc == 0, capsys.readouterr().out
