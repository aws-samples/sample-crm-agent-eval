# SPDX-License-Identifier: MIT
"""No former pinned-count site pins the tool count again.

Covers the sites that pinned 23/24/`+ 6`/`EXPECTED_TOOL_COUNT`, the parity gate's replacement
check (name uniqueness), the derived keyword-instance count, and the plan-security-baseline gate's
declared fixture absences: none against the committed fixture, and a planted one is load-bearing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest
from scripts import check_plan_security_baseline as baseline
from scripts import check_toolspec_parity as parity
from scripts import plan_fixtures

from crm.toolspec.definitions import ALL_TOOLS, BY_NAME

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Each former pinned-count site, with a pattern any reintroduced literal would match.
PINNED_COUNT_PATTERNS: Final[dict[str, tuple[str, ...]]] = {
    "infra/modules/agentcore/variables.tf": (r"length\(var\.tool_\w+\)\s*==\s*\d+",),
    "infra/modules/api/variables.tf": (r"length\(var\.tool_\w+\)\s*==\s*\d+",),
    "infra/main.tf": (
        r"length\(local\.tool_names\)\s*\+\s*\d+",
        r"ALL_TOOLS` \(\d+ tools\)",
    ),
    "scripts/check_toolspec_parity.py": (r"EXPECTED_TOOL_COUNT", r'"additionalProperties":\s*\d+'),
    "scripts/package_lambdas.sh": (r'TOOL_NAMES\[@\]\}"\s*-ne\s*\d+',),
}


@pytest.mark.parametrize(
    ("path", "pattern"),
    [(path, pattern) for path, patterns in PINNED_COUNT_PATTERNS.items() for pattern in patterns],
)
def test_no_former_site_pins_a_tool_count(path: str, pattern: str) -> None:
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert re.search(pattern, text) is None, f"{path} pins a tool count again ({pattern!r})"


def test_the_pinned_count_patterns_match_the_shape_they_guard() -> None:
    """Each guard pattern matches the literal it replaced, so a green scan is not vacuous."""
    replaced = {
        r"length\(var\.tool_\w+\)\s*==\s*\d+": "length(var.tool_lambda_arns) == 24",
        r"length\(local\.tool_names\)\s*\+\s*\d+": "length(local.tool_names) + 6",
        r"ALL_TOOLS` \(\d+ tools\)": "ALL_TOOLS` (24 tools)",
        r"EXPECTED_TOOL_COUNT": "EXPECTED_TOOL_COUNT = 24",
        r'"additionalProperties":\s*\d+': '"additionalProperties": 24,',
        r'TOOL_NAMES\[@\]\}"\s*-ne\s*\d+': 'if [[ "${#TOOL_NAMES[@]}" -ne 24 ]]; then',
    }
    for patterns in PINNED_COUNT_PATTERNS.values():
        for pattern in patterns:
            assert re.search(pattern, replaced[pattern]), pattern


def test_the_two_new_tools_are_declared() -> None:
    assert BY_NAME["get_audit_record"].risk == "read"
    assert BY_NAME["get_audit_record"].mutates is False
    assert BY_NAME["rebase_proposal"].risk == "approval"
    assert BY_NAME["rebase_proposal"].agents == BY_NAME["submit_proposal"].agents
    assert BY_NAME["get_audit_record"].agents == BY_NAME["list_audit_records"].agents


def test_the_restrictive_additional_properties_count_is_one_per_tool() -> None:
    assert parity.EXPECTED_KEYWORD_INSTANCES["additionalProperties"] == len(ALL_TOOLS)


def test_the_parity_gate_passes_unique_names(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parity.check_tool_names_unique() == []


def test_the_parity_gate_names_a_duplicated_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parity, "ALL_TOOLS", (*ALL_TOOLS, BY_NAME["get_audit_record"]))
    (violation,) = parity.check_tool_names_unique()
    assert "'get_audit_record' more than once" in violation


# -- declared fixture absences ------------------------------------------------------------------


_TOOL_MODULE: Final[str] = 'module.lambda_action["{}"].'


def _coverage(findings: list[baseline.Finding]) -> baseline.Finding:
    (finding,) = [
        f
        for f in findings
        if f.resource
        == "(coverage) the matrix's functions are the plan's functions, less declared fixture absences"
    ]
    return finding


@pytest.fixture(scope="module")
def compliant() -> baseline.Plan:
    return baseline.load_plan(plan_fixtures.materialize("compliant"))


def _plan_without(*tools: str) -> baseline.Plan:
    """The compliant fixture with every resource of each tool's `lambda_action` instance removed."""
    document = plan_fixtures.build("compliant")
    prefixes = tuple(_TOOL_MODULE.format(tool) for tool in tools)
    document["resource_changes"] = [
        c for c in document["resource_changes"] if not c["address"].startswith(prefixes)
    ]
    return baseline.Plan(
        resource_changes=document["resource_changes"],
        configuration=document.get("configuration", {}),
    )


def test_the_committed_fixture_needs_no_declared_absence(compliant: baseline.Plan) -> None:
    findings = baseline.check_resource_inventory_identity(compliant)
    assert not [f for f in findings if f.resource.startswith("(declared-absent)")], findings
    assert baseline._fixture_absent_functions() == frozenset()
    assert not [f for f in findings if f.status == "fail"], findings
    assert _coverage(baseline.check_dynamodb_leading_keys_isolation(compliant)).status == "pass"


def test_a_declared_absence_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan lacking a tool fails the inventory and the matrix coverage until it is declared."""
    plan = _plan_without("rebase_proposal")
    inventory_failures = {
        f.resource for f in baseline.check_resource_inventory_identity(plan) if f.status == "fail"
    }
    assert inventory_failures == {"aws_lambda_function:rebase_proposal"}
    coverage = _coverage(baseline.check_dynamodb_leading_keys_isolation(plan))
    assert coverage.status == "fail" and "rebase_proposal" in coverage.detail

    monkeypatch.setattr(
        baseline, "FIXTURE_ABSENT_INSTANCES", {"aws_lambda_function:rebase_proposal": "planted"}
    )
    assert not [f for f in baseline.check_resource_inventory_identity(plan) if f.status == "fail"]
    assert _coverage(baseline.check_dynamodb_leading_keys_isolation(plan)).status == "pass"


def test_a_declared_absence_that_is_planned_does_not_fail_the_coverage(
    compliant: baseline.Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh plan containing a declared member still passes, as a re-captured fixture would."""
    monkeypatch.setattr(
        baseline,
        "FIXTURE_ABSENT_INSTANCES",
        {**baseline.FIXTURE_ABSENT_INSTANCES, "aws_lambda_function:get_account": "planted"},
    )
    assert _coverage(baseline.check_dynamodb_leading_keys_isolation(compliant)).status == "pass"


def test_an_undeclared_missing_tool_still_fails_the_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The excusal is exactly the declared set: of two missing tools, the undeclared one reds."""
    plan = _plan_without("rebase_proposal", "get_audit_record")
    monkeypatch.setattr(
        baseline, "FIXTURE_ABSENT_INSTANCES", {"aws_lambda_function:get_audit_record": "planted"}
    )
    coverage = _coverage(baseline.check_dynamodb_leading_keys_isolation(plan))
    assert coverage.status == "fail"
    declared = re.search(r"\(FIXTURE_ABSENT_INSTANCES\): (\[[^\]]*\])", coverage.detail)
    missing = re.search(r"in the matrix and not the plan: (\[[^\]]*\])", coverage.detail)
    assert declared is not None and missing is not None, coverage.detail
    assert set(ast.literal_eval(missing.group(1))) == {"rebase_proposal", "get_audit_record"}
    assert set(ast.literal_eval(declared.group(1))) == {"get_audit_record"}
