# SPDX-License-Identifier: MIT
"""The timeout chain: every outer hop outlasts the hop it waits on.

Each value comes from its source (`agents.budget`, `gateway.chat_target`, the committed Terraform and
ASL template), never from a literal in this file, so a change on either side of an inequality reds it.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies

import agents.runtime_server as runtime_server
from agents.budget import CHAT_BUDGET, EVAL_BUDGET

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INFRA: Final = REPO_ROOT / "infra"
CHAT_TARGET_SOURCE: Final = REPO_ROOT / "src" / "gateway" / "chat_target.py"

#: The idle heartbeat interval; `agents.runtime_server.HEARTBEAT_INTERVAL_SECONDS` wins
#: when exported.
DESIGN_HEARTBEAT_INTERVAL_SECONDS: Final = 20.0


def _module_float(path: Path, name: str) -> float:
    """A module-level numeric constant, read from source without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, int | float), f"{name} is not numeric: {value!r}"
            return float(value)
    raise AssertionError(f"no module-level `{name}: ... = <number>` in {path}")


def _chat_deadline() -> float:
    return _module_float(CHAT_TARGET_SOURCE, "CHAT_DEADLINE_SECONDS")


def _chat_reserve() -> float:
    """What `effective_deadline_seconds` keeps back from the Lambda's remaining time."""
    return _module_float(CHAT_TARGET_SOURCE, "READ_TIMEOUT_SECONDS") + _module_float(
        CHAT_TARGET_SOURCE, "_RESULT_MARGIN_SECONDS"
    )


def _effective_chat_deadline() -> float:
    """`min(CHAT_DEADLINE_SECONDS, Lambda timeout - reserve)` at a full invocation."""
    return min(_chat_deadline(), _chat_target_timeout() - _chat_reserve())


def _code(path: Path) -> str:
    return blank_comments(blank_heredoc_bodies(path.read_text(encoding="utf-8")))


def _block(text: str, header: str) -> str:
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(header)]
    assert len(starts) == 1, f"expected one {header!r} block, got {len(starts)}"
    depth = 0
    for offset, line in enumerate(lines[starts[0] :], start=starts[0]):
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(lines[starts[0] : offset + 1])
    raise AssertionError(f"unterminated {header!r}")


def _number(block: str, attribute: str) -> float:
    matches = re.findall(rf"^\s*{attribute}\s*=\s*([0-9.]+)\s*$", block, re.MULTILINE)
    assert len(matches) == 1, f"expected one numeric `{attribute}`, got {matches}"
    return float(matches[0])


def _relay_timeout() -> float:
    return _number(_block(_code(INFRA / "main.tf"), 'module "agent_relay"'), "timeout_seconds")


def _relay_validation_floor() -> float:
    variable = _block(
        _code(INFRA / "modules" / "agent_relay" / "variables.tf"), 'variable "timeout_seconds"'
    )
    floor = re.search(r"condition\s*=\s*var\.timeout_seconds\s*>\s*([0-9.]+)", variable)
    assert floor is not None, "the relay timeout lost its `> N` validation"
    return float(floor.group(1))


def _chat_target_timeout() -> float:
    return _number(
        _block(_code(INFRA / "mcp_chat_gateway.tf"), 'module "lambda_action_chat_target"'),
        "timeout_seconds",
    )


def _sweep_task_timeout() -> float:
    return _number(
        _block(_code(INFRA / "eval_sfn.tf"), 'module "lambda_action_sweep_task"'), "timeout_seconds"
    )


def _origin_read_timeout() -> float:
    cdn = _code(INFRA / "modules" / "frontend_cdn" / "cloudfront.tf")
    origin = re.search(
        r'origin\s*\{[^{}]*origin_id\s*=\s*"agent-relay-origin".*?custom_origin_config\s*\{([^}]*)\}',
        cdn,
        re.DOTALL,
    )
    assert origin is not None, "no custom_origin_config on the agent-relay origin"
    return _number(origin.group(1), "origin_read_timeout")


def _states(node: Any) -> dict[str, Any]:
    """Every state in the ASL document, nested Map processors included, by name."""
    found: dict[str, Any] = {}
    if isinstance(node, dict):
        for name, state in (node.get("States") or {}).items():
            found[name] = state
        for value in node.values():
            found.update(_states(value))
    elif isinstance(node, list):
        for value in node:
            found.update(_states(value))
    return found


def _asl() -> dict[str, Any]:
    document = json.loads((INFRA / "templates" / "eval_sweep.asl.json").read_text("utf-8"))
    assert isinstance(document, dict)
    return document


def _sweep_task_state_timeout() -> float:
    calls = [
        state
        for state in _states(_asl()).values()
        if isinstance(state, dict)
        and "sweep_task" in json.dumps(state.get("Parameters", state))
        and state.get("Type") == "Task"
    ]
    assert len(calls) == 1, f"expected one Task state invoking sweep_task, got {len(calls)}"
    return float(calls[0]["TimeoutSeconds"])


def _heartbeat_interval() -> float:
    value = getattr(runtime_server, "HEARTBEAT_INTERVAL_SECONDS", DESIGN_HEARTBEAT_INTERVAL_SECONDS)
    return float(value)


def test_the_relay_outlasts_the_chat_turn() -> None:
    assert _relay_timeout() > CHAT_BUDGET.max_wall_clock_seconds


def test_the_relay_validation_floor_is_the_chat_turn() -> None:
    """`timeout_seconds > N` with N the chat wall clock: a relay at or below it is refused at plan."""
    assert _relay_validation_floor() == CHAT_BUDGET.max_wall_clock_seconds
    assert _relay_timeout() > _relay_validation_floor()


def test_the_chat_target_outlasts_its_deadline_which_outlasts_the_chat_turn() -> None:
    """Chat wall clock < effective deadline <= Lambda timeout - (read timeout + margin)."""
    effective = _effective_chat_deadline()
    assert CHAT_BUDGET.max_wall_clock_seconds < effective
    assert effective <= _chat_target_timeout() - _chat_reserve()
    assert _chat_target_timeout() > _chat_deadline()


def test_the_sfn_task_outlasts_the_sweep_task_lambda_which_outlasts_an_eval_turn() -> None:
    assert _sweep_task_state_timeout() > _sweep_task_timeout() > EVAL_BUDGET.max_wall_clock_seconds


def test_the_state_machine_outlasts_a_sequential_sweep_of_its_items() -> None:
    """The machine timeout exceeds one sweep_task state timeout (the Map runs items one at a time)."""
    assert float(_asl()["TimeoutSeconds"]) > _sweep_task_state_timeout()


def test_three_heartbeat_intervals_fit_inside_the_relay_origin_read_timeout() -> None:
    assert _heartbeat_interval() * 3 <= _origin_read_timeout()


@pytest.mark.parametrize(
    ("reader", "expected_minimum"),
    [
        (_relay_timeout, 1),
        (_chat_target_timeout, 1),
        (_chat_deadline, 1),
        (_sweep_task_timeout, 1),
        (_origin_read_timeout, 1),
    ],
)
def test_every_reader_finds_a_positive_value(reader: Any, expected_minimum: float) -> None:
    """Each source value is read, not defaulted: a missing attribute raises rather than reads 0."""
    assert reader() >= expected_minimum


def test_the_block_reader_refuses_an_absent_block() -> None:
    with pytest.raises(AssertionError):
        _block(_code(INFRA / "main.tf"), 'module "no_such_module_anywhere"')
