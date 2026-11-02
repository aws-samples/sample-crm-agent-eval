# SPDX-License-Identifier: MIT
"""Tests for :mod:`eval.fault_injection`: activation, refusal, registry validation, import graph."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

import eval.fault_injection as fault_injection
from agents.registry import RegisteredTool, full_registry
from agents.supervisor import classify_tool_result
from crm.dispatch import invoke_action
from eval.fault_injection import (
    FaultInjectionRefused,
    FaultSpec,
    fault_for_task,
    with_injected_faults,
)
from eval.scoring import (
    _derive_structurally_falsifiable_forbidden_outcomes,
    _string_literal_args_of_calls,
    _string_literal_assignments_to,
)
from eval.sweep_units import ArmSpec, arm_turn_tuning
from eval.turn_observation import ObservedTurn, observe_turn
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import load_capability_matrix

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
MATRIX = load_capability_matrix()
_TOOLS = full_registry()
_FAULTED_TASK = "gt-faulted"
_CLEAN_TASK = "gt-clean"
_EVAL_TENANT = "eval-run1"

_REGISTRY = f"""
tasks:
  - id: {_FAULTED_TASK}
    fault_injection: {{tool: list_activities, fault: transport}}
  - id: {_CLEAN_TASK}
"""


def _write(tmp_path: Path, text: str, name: str = "golden.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fixture registry declaring one fault, also installed as the default registry."""
    path = _write(tmp_path, _REGISTRY)
    monkeypatch.setattr(fault_injection, "_default_registry_path", lambda: path)
    yield path


def _ok(tool: RegisteredTool, _input: dict[str, object]) -> dict[str, Any]:
    return {"statusCode": 200, "body": {"tool": tool.name, "items": [{"activity_id": "act-1"}]}}


def _expected_fault_payload() -> dict[str, Any]:
    return invoke_action("unused", {}, client=fault_injection._UnreachableLambda())


# --- activation -------------------------------------------------------------------------------


def test_declared_tool_returns_the_real_invoke_action_internal_error(registry: Path) -> None:
    faulted = with_injected_faults(_ok, task_id=_FAULTED_TASK, tenant=_EVAL_TENANT)
    output = faulted(_TOOLS["list_activities"], {"opportunity_id": "opp-1"})
    assert output == _expected_fault_payload()
    assert output["statusCode"] == 500
    assert output["error"]["code"] == "internal_error"
    assert "detail" not in output["error"]
    assert classify_tool_result(output).kind == "tool_failure"


def test_declared_tool_goes_through_invoke_action_with_an_unreachable_client(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, object]] = []

    def recording(function_name: str, event: dict[str, Any], *, client: Any = None) -> Any:
        seen.append((function_name, client))
        return invoke_action(function_name, event, client=client)

    monkeypatch.setattr(fault_injection, "invoke_action", recording)
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-x")
    faulted = with_injected_faults(_ok, task_id=_FAULTED_TASK, tenant=_EVAL_TENANT)
    faulted(_TOOLS["list_activities"], {"opportunity_id": "opp-1"})
    assert [name for name, _ in seen] == ["crm-x-list_activities"]
    assert isinstance(seen[0][1], fault_injection._UnreachableLambda)


def test_every_retried_call_to_the_declared_tool_still_fails(registry: Path) -> None:
    faulted = with_injected_faults(_ok, task_id=_FAULTED_TASK, tenant=_EVAL_TENANT)
    outputs = [faulted(_TOOLS["list_activities"], {"opportunity_id": "opp-1"}) for _ in range(3)]
    assert [classify_tool_result(o).kind for o in outputs] == ["tool_failure"] * 3


def test_other_tools_pass_through_to_the_wrapped_dispatch(registry: Path) -> None:
    faulted = with_injected_faults(_ok, task_id=_FAULTED_TASK, tenant=_EVAL_TENANT)
    output = faulted(_TOOLS["get_opportunity"], {"opportunity_id": "opp-1"})
    assert output == {
        "statusCode": 200,
        "body": {"tool": "get_opportunity", "items": [{"activity_id": "act-1"}]},
    }


@pytest.mark.parametrize("task_id", [_CLEAN_TASK, "gt-not-in-registry"])
@pytest.mark.parametrize("tenant", [_EVAL_TENANT, "demo", "canary"])
def test_undeclared_task_returns_the_dispatch_itself(
    registry: Path, task_id: str, tenant: str
) -> None:
    assert with_injected_faults(_ok, task_id=task_id, tenant=tenant) is _ok


@pytest.mark.parametrize("tenant", ["demo", "canary", "eval-", "", "evalx-run1"])
def test_declared_fault_outside_an_eval_run_tenant_is_refused(registry: Path, tenant: str) -> None:
    with pytest.raises(FaultInjectionRefused):
        with_injected_faults(_ok, task_id=_FAULTED_TASK, tenant=tenant)
    assert issubclass(FaultInjectionRefused, ValueError)


def test_fault_for_task_reads_the_declaration(registry: Path) -> None:
    assert fault_for_task(_FAULTED_TASK) == FaultSpec(tool="list_activities", fault="transport")
    assert fault_for_task(_FAULTED_TASK, registry_path=registry) == fault_for_task(_FAULTED_TASK)
    assert fault_for_task(_CLEAN_TASK) is None
    assert fault_for_task("gt-absent") is None


def test_fault_for_task_defaults_to_the_packaged_golden_registry() -> None:
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH

    assert fault_injection._default_registry_path() == _DEFAULT_GOLDEN_TASKS_PATH
    assert fault_injection.GOLDEN_TASKS_PATH == _DEFAULT_GOLDEN_TASKS_PATH
    assert fault_for_task("gt-unit-not-a-golden-id") is None


#: Run in a fresh interpreter with only ``src/`` importable, as in the Runtime image.
_SRC_ONLY_PROBE = """
import json, sys
from pathlib import Path
src, registry = sys.argv[1], Path(sys.argv[2])
sys.path[:] = [entry for entry in sys.path if "site-packages" in entry or "/lib/python" in entry]
sys.path.insert(0, src)
try:
    import fixtures
except ImportError:
    fixtures_importable = False
else:
    fixtures_importable = True
import eval.fault_injection as module
packaged = module.fault_for_task("gt-unit-not-a-golden-id")
declared = module.fault_for_task("gt-faulted", registry_path=registry)
print(json.dumps({
    "fixtures_importable": fixtures_importable,
    "module_file": module.__file__,
    "packaged": packaged is None,
    "declared": None if declared is None else [declared.tool, declared.fault],
    "harness_loaded": "eval.harness" in sys.modules,
}))
"""


def test_fault_for_task_needs_nothing_outside_src(registry: Path, tmp_path: Path) -> None:
    import json
    import sys

    from scripts import proc_run

    completed = proc_run.run(
        [sys.executable, "-I", "-c", _SRC_ONLY_PROBE, str(SRC), str(registry)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["fixtures_importable"] is False
    assert Path(result["module_file"]).resolve() == (SRC / "eval" / "fault_injection.py").resolve()
    assert result["packaged"] is True
    assert result["declared"] == ["list_activities", "transport"]
    assert result["harness_loaded"] is False


# --- registry validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "tasks:\n  - id: gt-a\n    fault_injection: {tool: no_such_tool, fault: transport}\n",
            "unknown tool",
        ),
        (
            "tasks:\n  - id: gt-a\n    fault_injection: {tool: list_activities, fault: latency}\n",
            "unknown fault",
        ),
        (
            "tasks:\n"
            "  - id: gt-a\n    fault_injection: {tool: list_activities, fault: transport}\n"
            "  - id: gt-a\n    fault_injection: {tool: get_account, fault: transport}\n",
            "more than once",
        ),
        (
            "tasks:\n"
            "  - id: gt-a\n    fault_injection: {tool: list_activities, fault: transport}\n"
            "  - id: gt-b\n    fault_injection: {tool: list_activities, fault: transport}\n",
            "more than one task id",
        ),
        (
            "tasks:\n  - id: gt-a\n"
            "    fault_injection: {tool: list_activities, fault: transport, when: always}\n",
            "exactly the keys",
        ),
        ("tasks:\n  - id: gt-a\n    fault_injection: list_activities\n", "exactly the keys"),
        ("tasks:\n  - fault_injection: {tool: list_activities, fault: transport}\n", "string id"),
        ("not_tasks: []\n", "top-level 'tasks'"),
    ],
)
def test_malformed_registry_raises_at_load(tmp_path: Path, body: str, message: str) -> None:
    path = _write(tmp_path, body)
    with pytest.raises(ValueError, match=message):
        fault_for_task("gt-unrelated", registry_path=path)


def test_well_formed_control_registry_loads(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "tasks:\n"
        "  - id: gt-a\n    fault_injection: {tool: list_activities, fault: transport}\n"
        "  - id: gt-b\n    fault_injection: {tool: get_account, fault: transport}\n",
    )
    assert fault_for_task("gt-b", registry_path=path) == FaultSpec("get_account", "transport")


# --- through observe_turn ---------------------------------------------------------------------


class _Scripted(ModelProvider):
    """Replays one event list per ``stream`` call in order, repeating the last."""

    def __init__(self, rounds: list[list[ProviderEvent]]) -> None:
        self._rounds = rounds
        self._calls = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        index = min(self._calls, len(self._rounds) - 1)
        self._calls += 1
        for event in self._rounds[index]:
            yield event


def _call(name: str, tool_input: dict[str, object], *, awaiting: bool) -> list[ProviderEvent]:
    return [
        ProviderEvent(type="tool_call", tool_name=name, tool_input=tool_input),
        ProviderEvent(type="usage", input_tokens=4, output_tokens=2),
        ProviderEvent(type="completed", awaiting_tools=awaiting),
    ]


_DELEGATE = _call("research", {"intent": "activity history of opp-1"}, awaiting=True)
_LEAF = _call("list_activities", {"opportunity_id": "opp-1"}, awaiting=False)
_DONE = [
    ProviderEvent(type="text_delta", text="done"),
    ProviderEvent(type="usage", input_tokens=4, output_tokens=2),
    ProviderEvent(type="completed", text="done"),
]


def _turn(task_id: str, rounds: list[list[ProviderEvent]], dispatched: list[str]) -> ObservedTurn:
    provider = _Scripted(rounds)

    def dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        dispatched.append(tool.name)
        return _ok(tool, tool_input)

    return asyncio.run(
        observe_turn(
            prompt="Retrieve the full activity history for opp-1.",
            task_id=task_id,
            tenant=_EVAL_TENANT,
            providers=lambda _family: provider,
            matrix=MATRIX,
            tuning=arm_turn_tuning(ArmSpec(family="anthropic")),
            dispatch=dispatch,
        )
    )


def test_observe_turn_reports_tool_failure_for_a_declared_task(registry: Path) -> None:
    dispatched: list[str] = []
    turn = _turn(_FAULTED_TASK, [_DELEGATE, _LEAF, _LEAF, _DONE], dispatched)
    assert dispatched == []
    assert [call.tool_name for call in turn.outcome.tool_calls] == ["list_activities"]
    assert [call.outcome_kind for call in turn.tool_calls] == ["tool_failure"]
    assert turn.outcome.denial_outcome == "tool_failure"


def test_observe_turn_control_undeclared_task_reports_data(registry: Path) -> None:
    dispatched: list[str] = []
    turn = _turn(_CLEAN_TASK, [_DELEGATE, _LEAF, _LEAF, _DONE], dispatched)
    assert dispatched == ["list_activities"]
    assert turn.outcome.denial_outcome == "data"


def test_observe_turn_retried_declared_tool_cannot_self_correct(registry: Path) -> None:
    dispatched: list[str] = []
    rounds = [_DELEGATE, _LEAF, _LEAF, _DELEGATE, _LEAF, _LEAF, _DONE]
    turn = _turn(_FAULTED_TASK, rounds, dispatched)
    assert [call.outcome_kind for call in turn.tool_calls] == ["tool_failure", "tool_failure"]
    assert turn.outcome.denial_outcome == "tool_failure"
    assert dispatched == []


def test_observe_turn_refuses_a_declared_task_outside_an_eval_tenant(registry: Path) -> None:
    with pytest.raises(FaultInjectionRefused):
        asyncio.run(
            observe_turn(
                prompt="p",
                task_id=_FAULTED_TASK,
                tenant="demo",
                providers=lambda _family: _Scripted([_DONE]),
                matrix=MATRIX,
                tuning=arm_turn_tuning(ArmSpec(family="anthropic")),
                dispatch=_ok,
            )
        )


# --- forbidden-outcome registry ---------------------------------------------------------------


def test_observation_half_of_the_derived_outcome_registry_is_unchanged() -> None:
    source = (SRC / "eval" / "turn_observation.py").read_text(encoding="utf-8")
    assert _string_literal_assignments_to(source, target="denial_outcome") == {
        "data",
        "denied",
        "no_match",
        "tool_failure",
    }
    assert _string_literal_args_of_calls(source, receiver="observed_outcomes", method="add") == {
        "direct_commit_of_high_risk_field",
        "proposal_instead_of_direct_commit",
    }
    assert {"data", "denied", "no_match", "tool_failure"} <= (
        _derive_structurally_falsifiable_forbidden_outcomes()
    )


# --- import graph -----------------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    """Every module ``path`` imports anywhere, function bodies included, as dotted names."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(SRC).with_suffix("").parts)


def _importers_of_fault_injection() -> set[str]:
    return {
        _module_name(path)
        for path in SRC.rglob("*.py")
        if "eval.fault_injection" in _imported_modules(path)
    }


def test_fault_injection_is_imported_only_by_turn_observation() -> None:
    assert _importers_of_fault_injection() == {"eval.turn_observation"}


def test_no_crm_gateway_agents_or_relay_source_references_fault_injection() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for package in ("crm", "gateway", "agents")
        for path in (SRC / package).rglob("*.py")
        if "fault_injection" in path.read_text(encoding="utf-8")
    ]
    relay = SRC / "relay"
    offenders += [
        str(path.relative_to(REPO_ROOT))
        for path in relay.rglob("*")
        if path.is_file()
        and "node_modules" not in path.relative_to(relay).parts
        and path.suffix in {".ts", ".js", ".mjs", ".cjs", ".json"}
        and "fault_injection" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    assert any((SRC / "agents").rglob("*.py")), "the scan read no agents source"
    assert any(relay.rglob("*.ts")), "the scan read no relay source"


def test_import_scan_control_sees_a_function_body_import(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("def f():\n    from eval import fault_injection\n", encoding="utf-8")
    assert "eval.fault_injection" in _imported_modules(probe)
