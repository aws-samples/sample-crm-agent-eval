# SPDX-License-Identifier: MIT
"""The root's reserved-concurrency headroom guard covers every function in the deployment.

The claim side is parsed from `infra/main.tf`'s `local.reserved_concurrency`, with the tool tier
resolved through `crm.toolspec.definitions.ALL_TOOLS`; the truth side is the committed plan fixture
`plan_fixtures.build("compliant")`, summed per `aws_lambda_function`. No terraform binary is needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts import check_plan_security_baseline as _baseline
from scripts import plan_fixtures

from crm.toolspec.definitions import ALL_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_MAIN = REPO_ROOT / "infra" / "main.tf"

#: The non-tool functions the root claims pool capacity for.
EXPECTED_NON_TOOL_KEYS = frozenset(
    {
        "identity_interceptor",
        "pre_token_generation",
        "eval_trigger",
        "agent_relay",
        "capabilities",
        "sweep_control",
        "sweep_task",
        "chat_target",
        "chat_interceptor",
        "user_settings",
    }
)


@pytest.fixture(scope="module")
def root_text() -> str:
    return ROOT_MAIN.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The claim side, parsed from `infra/main.tf`.
# ---------------------------------------------------------------------------


def _tool_tier(root_text: str) -> dict[str, int]:
    """`local.tool_reserved_concurrency` resolved over the real `ALL_TOOLS`, read from the HCL ternary."""
    match = re.search(
        r"tool_reserved_concurrency\s*=\s*\{.*?contains\(\[(?P<raised>[^\]]*)\],\s*name\)\s*"
        r"\?\s*(?P<high>\d+)\s*:\s*(?P<low>\d+)",
        root_text,
        re.DOTALL,
    )
    assert match is not None, "could not parse local.tool_reserved_concurrency"
    raised = set(re.findall(r'"([a-z0-9_]+)"', match.group("raised")))
    assert raised, (
        "the raised-concurrency list parsed empty, so every tool would read as the default"
    )

    tool_names = {tool.name for tool in ALL_TOOLS}
    assert raised <= tool_names, f"raised list names a non-tool: {sorted(raised - tool_names)}"
    high, low = int(match.group("high")), int(match.group("low"))
    assert high > low, (high, low)
    return {name: (high if name in raised else low) for name in tool_names}


def _non_tool_tier(root_text: str) -> dict[str, int]:
    """The literal `key = <int>` entries of `local.non_tool_reserved_concurrency`.

    Requires `local.reserved_concurrency` to be the merge of exactly the tool tier and that map.
    """
    assert re.search(
        r"reserved_concurrency\s*=\s*merge\(\s*local\.tool_reserved_concurrency,\s*"
        r"local\.non_tool_reserved_concurrency,\s*\)",
        root_text,
    ), "local.reserved_concurrency is not the merge of the tool tier and the non-tool map"
    match = re.search(
        r"^  non_tool_reserved_concurrency = \{(?P<body>.*?)\n  \}$",
        root_text,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "could not parse local.non_tool_reserved_concurrency"
    entries = {
        name: int(value)
        for name, value in re.findall(
            r"^\s+([a-z0-9_]+)\s*=\s*(\d+)\s*$", match.group("body"), re.MULTILINE
        )
    }
    assert entries, "the merged object parsed empty"
    return entries


@pytest.fixture(scope="module")
def claimed(root_text: str) -> dict[str, int]:
    tools = _tool_tier(root_text)
    others = _non_tool_tier(root_text)
    collisions = sorted(set(tools) & set(others))
    assert collisions == [], (
        f"`merge` would silently override {collisions}, shrinking the map and lowering the sum"
    )
    return {**tools, **others}


def test_the_parse_is_not_vacuous(claimed: dict[str, int]) -> None:
    """The parsed map has one positive entry per tool and per expected non-tool key."""
    assert len(claimed) == len(ALL_TOOLS) + len(EXPECTED_NON_TOOL_KEYS)
    assert all(value > 0 for value in claimed.values()), claimed


def test_every_non_tool_function_that_claims_pool_capacity_is_accounted_for(
    claimed: dict[str, int],
) -> None:
    """The parsed non-tool entries are exactly `EXPECTED_NON_TOOL_KEYS`."""
    parsed_non_tool = set(claimed) - {tool.name for tool in ALL_TOOLS}
    assert parsed_non_tool == set(EXPECTED_NON_TOOL_KEYS), sorted(
        parsed_non_tool.symmetric_difference(EXPECTED_NON_TOOL_KEYS)
    )


# ---------------------------------------------------------------------------
# The truth side, and the comparison.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def planned() -> dict[str, float]:
    """Every `aws_lambda_function` in the committed plan fixture and its reserved concurrency."""
    document = plan_fixtures.build("compliant")
    functions = {
        resource["address"]: (resource.get("change") or {})
        .get("after", {})
        .get("reserved_concurrent_executions")
        for resource in document.get("resource_changes", [])
        if resource.get("type") == "aws_lambda_function"
    }
    assert functions, "the plan fixture contains no aws_lambda_function, so it asserts nothing"
    return functions


def test_the_fixture_reserves_concurrency_on_every_function(planned: dict[str, float]) -> None:
    """Every function in the plan fixture reserves a positive numeric concurrency."""
    unreserved = sorted(a for a, v in planned.items() if not isinstance(v, (int, float)) or v <= 0)
    assert unreserved == [], unreserved


def _require_current_fixture() -> None:
    """Skip, naming the remedy, when the committed plan fixture predates current `infra/`.

    Staleness is decided by `check_plan_security_baseline.plan_is_stale_against_source`.
    """
    if _baseline.plan_is_stale_against_source():
        pytest.skip(
            "the committed plan fixture predates current infra/, so this plan-vs-source comparison "
            "cannot hold and is not asserting anything about the deployment. REMEDY: regenerate the "
            "fixture per `check_plan_security_baseline._RECAPTURE_HINT` -- `terraform show -json` "
            "against the dev account, scrub the account id to 123456789012 and the IAM unique_id / "
            "caller user_id to AROA-/AIDA- examples, then set "
            "`_fixture_provenance.captured_infra_digest` to `plan_fixtures.infra_digest` of the "
            "tree it was captured from. This assertion re-arms "
            "AUTOMATICALLY once the fixture is current -- no edit here is needed to restore it. The "
            "loud form of this same fact is plan_freshness's [DEFER] row in run_gates output."
        )


def test_the_deployment_has_one_function_per_tool_plus_the_non_tool_set(
    planned: dict[str, float],
) -> None:
    """The plan has one function per tool plus one per expected non-tool key."""
    _require_current_fixture()
    assert len(planned) == len(ALL_TOOLS) + len(EXPECTED_NON_TOOL_KEYS), sorted(planned)


def test_the_guards_claim_equals_the_plans_actual_reservation(
    claimed: dict[str, int], planned: dict[str, float]
) -> None:
    """The map's claimed total equals the plan's total reservation."""
    _require_current_fixture()
    assert sum(claimed.values()) == sum(planned.values()), (
        f"local.reserved_concurrency sums to {sum(claimed.values())} but the plan reserves "
        f"{sum(planned.values())} across {len(planned)} functions — the guard on the apply gate is "
        f"asserting a smaller number than the deployment claims from the account pool"
    )


def test_the_headroom_ceiling_still_holds_with_the_complete_sum(planned: dict[str, float]) -> None:
    """The ceiling applies to the plan's real total, not to the map's claim about it."""
    assert sum(planned.values()) <= 900, sum(planned.values())


# ---------------------------------------------------------------------------
# No call site may restate a literal.
# ---------------------------------------------------------------------------


def test_no_call_site_carries_a_literal_reserved_concurrency(root_text: str) -> None:
    """Every `reserved_concurrent_executions` argument reads from `local.reserved_concurrency[...]`."""
    arguments = re.findall(
        r"^\s+reserved_concurrent_executions\s*=\s*(.+?)\s*$", root_text, re.MULTILINE
    )
    assert len(arguments) >= 6, f"expected every Lambda call site, found {arguments}"
    offenders = [
        value for value in arguments if not value.startswith("local.reserved_concurrency[")
    ]
    assert offenders == [], offenders


def test_the_staleness_skip_is_conditional_in_both_directions(
    monkeypatch: pytest.MonkeyPatch, planned: dict[str, float]
) -> None:
    """The staleness guard skips when the fixture is stale and reaches its assertion when current."""
    # Arm 1 — reported stale: the cell must skip rather than fail.
    monkeypatch.setattr(_baseline, "plan_is_stale_against_source", lambda: True)
    with pytest.raises(pytest.skip.Exception):
        test_the_deployment_has_one_function_per_tool_plus_the_non_tool_set(planned)

    # Arm 2 — reported current: the cell must reach its assertion, whatever that assertion then says.
    monkeypatch.setattr(_baseline, "plan_is_stale_against_source", lambda: False)
    try:
        test_the_deployment_has_one_function_per_tool_plus_the_non_tool_set(planned)
    except pytest.skip.Exception:  # pragma: no cover - the failure this arm exists to catch
        pytest.fail(
            "the cell skipped although the fixture was reported CURRENT, so the skip is "
            "unconditional and the plan-vs-source comparison is permanently unarmed"
        )
    except AssertionError:
        pass  # reached its assertion and disagreed, which is the arm passing


def test_the_guarded_comparison_can_still_fail_on_a_current_fixture(
    monkeypatch: pytest.MonkeyPatch, planned: dict[str, float]
) -> None:
    """With the fixture reported current, a plan missing one function fails the comparison."""
    monkeypatch.setattr(_baseline, "plan_is_stale_against_source", lambda: False)
    doctored = dict(planned)
    removed = sorted(doctored)[0]
    del doctored[removed]

    with pytest.raises(AssertionError):
        test_the_deployment_has_one_function_per_tool_plus_the_non_tool_set(doctored)


def test_the_skip_reason_names_the_remedy_and_the_re_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The staleness skip reason names the remedy, the recapture hint, the re-arm and `plan_freshness`."""
    monkeypatch.setattr(_baseline, "plan_is_stale_against_source", lambda: True)
    with pytest.raises(pytest.skip.Exception) as caught:
        _require_current_fixture()
    reason = str(caught.value)

    for token in ("REMEDY", "regenerate", "_RECAPTURE_HINT", "re-arms", "plan_freshness"):
        assert token in reason, (
            f"the skip reason does not mention {token!r}, so a reader learns that something was not "
            f"checked without learning what to do about it: {reason!r}"
        )
