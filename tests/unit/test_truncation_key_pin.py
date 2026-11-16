# SPDX-License-Identifier: MIT
"""The eval truncation reader and the agent truncation writer share one key constant."""

from __future__ import annotations

import ast
from pathlib import Path

import agents.events as agent_events
import agents.supervisor as supervisor
import eval.turn_observation as turn_observation

_REPO = Path(__file__).resolve().parents[2]
_KEY = "TOOL_OUTPUT_TRUNCATED_KEY"


def _module_tree(relative: str) -> ast.Module:
    return ast.parse((_REPO / relative).read_text(encoding="utf-8"))


def _assigns(tree: ast.Module, name: str) -> bool:
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
    return False


def _imports_from(tree: ast.Module, module: str, name: str) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == module
        and any(alias.name == name and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )


def test_the_key_is_defined_once_in_agents_events() -> None:
    assert _assigns(_module_tree("src/agents/events.py"), _KEY)
    assert agent_events.TOOL_OUTPUT_TRUNCATED_KEY == "truncated"


def test_the_eval_reader_imports_the_key_rather_than_defining_its_own() -> None:
    tree = _module_tree("src/eval/turn_observation.py")
    assert not _assigns(tree, _KEY)
    assert _imports_from(tree, "agents.events", _KEY)
    assert turn_observation.TOOL_OUTPUT_TRUNCATED_KEY is agent_events.TOOL_OUTPUT_TRUNCATED_KEY


def test_the_supervisor_notice_reads_the_same_key() -> None:
    tree = _module_tree("src/agents/supervisor.py")
    assert _imports_from(tree, "agents.events", _KEY)
    assert supervisor._SEARCH_TRUNCATED_KEY is agent_events.TOOL_OUTPUT_TRUNCATED_KEY


def test_what_the_bound_writes_the_eval_reader_reports() -> None:
    raw: dict[str, object] = {"body": {"items": [{"id": n, "pad": "x" * 700} for n in range(60)]}}
    bounded, truncated = agent_events.bound_tool_output(raw)
    assert truncated is True
    assert turn_observation.tool_output_reports_truncation(bounded) is True
    assert turn_observation.tool_output_reports_truncation(raw) is False
