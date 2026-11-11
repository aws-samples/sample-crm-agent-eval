# SPDX-License-Identifier: MIT
"""The per-request prompt limit at each of the three model call sites.

Each tier's request is checked against the capability matrix's limit for that tier before the
provider is called. A refused request reaches no provider, records no attribution, and ends the
turn with ``budget_exhausted`` naming the tier, the estimate and the limit.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agents.budget import EVAL_WALL_CLOCK_SECONDS, RequestRefusedError, TurnBudget, TurnBudgetSpec
from agents.events import AgentEvent
from agents.prompt_estimate import estimate_prompt_tokens
from agents.registry import RegisteredTool, tool_registry_for
from agents.tool_actor import formulate_and_invoke
from agents.turn_driver import TurnRecords, drive_turn, session_turn_tuning
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import CapabilityMatrix, Tier, load_capability_matrix
from providers.tier_tuning import resolve_turn

MATRIX = load_capability_matrix()
TUNING = Tuning(source="test.request_prompt_limit", family="anthropic", effort=None)
LARGE = 10_000_000


def _matrix(
    *, supervisor: int = LARGE, subagent: int = LARGE, tool: int = LARGE
) -> CapabilityMatrix:
    """The real matrix with its per-tier limits replaced, bypassing the loader's window check."""
    return dataclasses.replace(
        MATRIX, request_prompt_limits={"supervisor": supervisor, "subagent": subagent, "tool": tool}
    )


@dataclass
class _Call:
    messages: list[dict[str, object]]
    tools: list[dict[str, object]]


@dataclass
class _ScriptedProvider(ModelProvider):
    """Plays one scripted round per call; a call beyond the script fails the test."""

    rounds: list[list[ProviderEvent]]
    calls: list[_Call] = field(default_factory=list)

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        assert self.rounds, "the provider was called for a request that should have been refused"
        self.calls.append(_Call(messages=[dict(m) for m in messages], tools=list(tools)))
        return self._emit(self.rounds.pop(0))

    async def _emit(self, events: list[ProviderEvent]) -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event


@dataclass
class _Dispatch:
    calls: list[str] = field(default_factory=list)

    def __call__(self, tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        self.calls.append(tool.name)
        return {"statusCode": 200, "body": {"account_id": "acc-1"}}


_SUPERVISOR_DELEGATES = [
    ProviderEvent(type="usage", input_tokens=100, output_tokens=20),
    ProviderEvent(
        type="tool_call", tool_name="research", tool_input={"intent": "acme"}, tool_id="t1"
    ),
    ProviderEvent(type="completed", awaiting_tools=True),
]
_SUBAGENT_CALLS_LEAF = [
    ProviderEvent(type="usage", input_tokens=40, output_tokens=10),
    ProviderEvent(
        type="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"}, tool_id="s1"
    ),
    ProviderEvent(type="completed", awaiting_tools=False),
]
_TOOL_FORMULATES = [
    ProviderEvent(type="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"}),
    ProviderEvent(type="usage", input_tokens=8, output_tokens=4),
    ProviderEvent(type="completed", awaiting_tools=False),
]
_SUPERVISOR_ANSWERS = [
    ProviderEvent(type="text_delta", text="Here is Acme."),
    ProviderEvent(type="usage", input_tokens=50, output_tokens=15),
    ProviderEvent(type="completed", awaiting_tools=False),
]


def _drive(
    matrix: CapabilityMatrix, rounds: list[list[ProviderEvent]]
) -> tuple[list[AgentEvent], TurnRecords, _ScriptedProvider, _Dispatch]:
    provider = _ScriptedProvider(rounds=[list(r) for r in rounds])
    dispatch = _Dispatch()
    records = TurnRecords()

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt="find acme",
                session_id="sess-limit",
                providers=lambda _family: provider,
                matrix=matrix,
                tuning=session_turn_tuning(TUNING),
                budget=TurnBudgetSpec(
                    max_tool_calls=8, max_wall_clock_seconds=EVAL_WALL_CLOCK_SECONDS
                ),
                dispatch=dispatch,
                records=records,
            )
        ]

    return asyncio.run(_run()), records, provider, dispatch


def _single_terminal(events: list[AgentEvent]) -> AgentEvent:
    terminal = [e for e in events if e.kind in ("completed", "error")]
    assert len(terminal) == 1, terminal
    return terminal[0]


def _refusal_message(events: list[AgentEvent], tier: str) -> str:
    terminal = _single_terminal(events)
    assert terminal.kind == "error" and terminal.error_kind == "budget_exhausted"
    message = terminal.error_message or ""
    assert "request_prompt_limit: estimated " in message
    assert f"prompt tokens for one request on the {tier} tier" in message
    return message


def _tiers(records: TurnRecords) -> list[str]:
    """The tiers attributed so far, sorted: a leaf's tool call is recorded before its round."""
    return sorted(inv.tier for inv in records.invocations)


def test_the_shipped_limits_are_the_values_each_site_reads() -> None:
    assert MATRIX.request_prompt_limit("supervisor") == 270_000
    assert MATRIX.request_prompt_limit("subagent") == 270_000
    assert MATRIX.request_prompt_limit("tool") == 95_000


def test_a_turn_under_every_limit_runs_all_three_tiers() -> None:
    """Control: the same script completes when no site refuses."""
    rounds = [_SUPERVISOR_DELEGATES, _SUBAGENT_CALLS_LEAF, _TOOL_FORMULATES, _SUPERVISOR_ANSWERS]
    events, records, provider, dispatch = _drive(MATRIX, rounds)
    assert _single_terminal(events).kind == "completed"
    assert len(provider.calls) == 4
    assert dispatch.calls == ["get_account"]
    assert _tiers(records) == ["subagent", "supervisor", "supervisor", "tool"]


class TestSupervisorSite:
    def test_a_refused_supervisor_request_calls_no_provider(self) -> None:
        events, records, provider, dispatch = _drive(_matrix(supervisor=1), rounds=[])
        message = _refusal_message(events, "supervisor")
        assert "> 1 prompt tokens" in message
        assert provider.calls == [] and dispatch.calls == []
        assert records.invocations == []

    def test_the_supervisor_limit_is_inclusive_at_the_real_request_estimate(self) -> None:
        _, _, probe, _ = _drive(MATRIX, rounds=[_SUPERVISOR_ANSWERS])
        first = probe.calls[0]
        estimate = estimate_prompt_tokens(first.messages, first.tools)

        events, _, provider, _ = _drive(_matrix(supervisor=estimate), rounds=[_SUPERVISOR_ANSWERS])
        assert _single_terminal(events).kind == "completed"
        assert len(provider.calls) == 1

        events, _, provider, _ = _drive(_matrix(supervisor=estimate - 1), rounds=[])
        assert f"estimated {estimate} > {estimate - 1} " in _refusal_message(events, "supervisor")
        assert provider.calls == []

    def test_the_supervisor_site_reads_the_supervisor_limit_not_the_subagent_limit(self) -> None:
        """A tiny subagent limit leaves a supervisor-only turn untouched."""
        events, _, provider, _ = _drive(
            _matrix(supervisor=LARGE, subagent=1), rounds=[_SUPERVISOR_ANSWERS]
        )
        assert _single_terminal(events).kind == "completed"
        assert len(provider.calls) == 1


class TestSubagentSite:
    def test_a_refused_subagent_request_calls_no_subagent_provider(self) -> None:
        events, records, provider, dispatch = _drive(
            _matrix(supervisor=LARGE, subagent=1), rounds=[_SUPERVISOR_DELEGATES]
        )
        assert "> 1 prompt tokens" in _refusal_message(events, "subagent")
        assert len(provider.calls) == 1
        assert dispatch.calls == []
        assert _tiers(records) == ["supervisor"]

    def test_the_subagent_site_reads_the_subagent_limit_not_the_supervisor_limit(self) -> None:
        """The supervisor's request is larger than the subagent's, so swapped reads would invert."""
        _, _, probe, _ = _drive(
            MATRIX,
            rounds=[
                _SUPERVISOR_DELEGATES,
                _SUBAGENT_CALLS_LEAF,
                _TOOL_FORMULATES,
                _SUPERVISOR_ANSWERS,
            ],
        )
        supervisor_estimate = estimate_prompt_tokens(probe.calls[0].messages, probe.calls[0].tools)
        subagent_estimate = estimate_prompt_tokens(probe.calls[1].messages, probe.calls[1].tools)
        assert subagent_estimate < supervisor_estimate

        events, records, provider, _ = _drive(
            _matrix(supervisor=supervisor_estimate, subagent=subagent_estimate - 1),
            rounds=[_SUPERVISOR_DELEGATES],
        )
        assert f"estimated {subagent_estimate} > {subagent_estimate - 1} " in _refusal_message(
            events, "subagent"
        )
        assert len(provider.calls) == 1
        assert _tiers(records) == ["supervisor"]


class TestToolSite:
    def test_a_refused_tool_request_calls_no_tool_provider_and_dispatches_nothing(self) -> None:
        events, records, provider, dispatch = _drive(
            _matrix(tool=1), rounds=[_SUPERVISOR_DELEGATES, _SUBAGENT_CALLS_LEAF]
        )
        assert "> 1 prompt tokens" in _refusal_message(events, "tool")
        assert len(provider.calls) == 2
        assert dispatch.calls == []
        assert _tiers(records) == ["subagent", "supervisor"]

    def test_the_tool_site_reads_the_tool_limit(self) -> None:
        """Supervisor and subagent limits at 1 would refuse earlier; only the tool limit is low."""
        _, _, probe, _ = _drive(
            MATRIX,
            rounds=[
                _SUPERVISOR_DELEGATES,
                _SUBAGENT_CALLS_LEAF,
                _TOOL_FORMULATES,
                _SUPERVISOR_ANSWERS,
            ],
        )
        tool_estimate = estimate_prompt_tokens(probe.calls[2].messages, probe.calls[2].tools)
        events, _, provider, _ = _drive(
            _matrix(tool=tool_estimate),
            rounds=[
                _SUPERVISOR_DELEGATES,
                _SUBAGENT_CALLS_LEAF,
                _TOOL_FORMULATES,
                _SUPERVISOR_ANSWERS,
            ],
        )
        assert _single_terminal(events).kind == "completed"
        assert len(provider.calls) == 4


def _get_account_tool() -> RegisteredTool:
    return next(tool for tool in tool_registry_for("research") if tool.name == "get_account")


def _intent_with_estimate(tool: RegisteredTool, tokens: int) -> str:
    """An intent padded so the tool-tier request estimates to exactly ``tokens``."""
    probe = _ScriptedProvider(rounds=[list(_TOOL_FORMULATES)])
    asyncio.run(_formulate(tool, "", probe, limit=None))
    base = probe.calls[0]
    base_bytes = 3 * estimate_prompt_tokens(base.messages, base.tools)
    # Pad to the exact byte count, then confirm against the real request below.
    for pad in range(max(0, tokens * 3 - base_bytes - 3), tokens * 3 - base_bytes + 4):
        candidate = "x" * pad
        check = _ScriptedProvider(rounds=[list(_TOOL_FORMULATES)])
        asyncio.run(_formulate(tool, candidate, check, limit=None))
        call = check.calls[0]
        if estimate_prompt_tokens(call.messages, call.tools) == tokens:
            return candidate
    raise AssertionError(f"no padding gives an estimate of {tokens}")


async def _formulate(
    tool: RegisteredTool,
    intent: str,
    provider: _ScriptedProvider,
    *,
    limit: int | None,
    budget: TurnBudget | None = None,
    dispatch: _Dispatch | None = None,
) -> Any:
    resolved = resolve_turn(MATRIX, session_turn_tuning(TUNING))["tool"]
    return await formulate_and_invoke(
        tool,
        intent,
        "research",
        provider=provider,
        resolved=resolved,
        dispatch=dispatch or _Dispatch(),
        request_prompt_limit=limit,
        budget=budget,
    )


@pytest.mark.parametrize("estimate", [95_000, 95_001])
def test_the_tool_tier_calls_at_95000_and_refuses_at_95001(estimate: int) -> None:
    tool = _get_account_tool()
    limit = MATRIX.request_prompt_limit("tool")
    assert limit == 95_000
    intent = _intent_with_estimate(tool, estimate)
    provider = _ScriptedProvider(rounds=[list(_TOOL_FORMULATES)] if estimate <= limit else [])
    dispatch = _Dispatch()
    budget = TurnBudget()
    if estimate <= limit:
        result = asyncio.run(
            _formulate(tool, intent, provider, limit=limit, budget=budget, dispatch=dispatch)
        )
        assert len(provider.calls) == 1
        assert dispatch.calls == ["get_account"]
        assert result.invocation.tier == "tool"
        assert not budget.is_exhausted()
        return
    with pytest.raises(RequestRefusedError):
        asyncio.run(
            _formulate(tool, intent, provider, limit=limit, budget=budget, dispatch=dispatch)
        )
    assert provider.calls == []
    assert dispatch.calls == []
    refusal = budget.refused_request
    assert refusal is not None
    assert (refusal.estimated_prompt_tokens, refusal.limit, refusal.tier) == (
        95_001,
        95_000,
        "tool",
    )


@pytest.mark.parametrize("tier", ["supervisor", "subagent", "tool"])
def test_no_site_refuses_under_the_shipped_limits_for_an_ordinary_turn(tier: Tier) -> None:
    """The shipped limits sit far above an ordinary turn's requests at every tier."""
    _, _, probe, _ = _drive(
        MATRIX,
        rounds=[_SUPERVISOR_DELEGATES, _SUBAGENT_CALLS_LEAF, _TOOL_FORMULATES, _SUPERVISOR_ANSWERS],
    )
    index = {"supervisor": 0, "subagent": 1, "tool": 2}[tier]
    call = probe.calls[index]
    assert estimate_prompt_tokens(call.messages, call.tools) < MATRIX.request_prompt_limit(tier)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENTS_DIR = _REPO_ROOT / "src" / "agents"
_TURN_DRIVER = _AGENTS_DIR / "turn_driver.py"

#: Callables whose ``request_prompt_limit`` keyword reaches a model call site; ``None`` disables it.
_LIMITED_CALLEES = frozenset(
    {"run_subagent", "run_subagent_async", "formulate_and_invoke", "_bind_tool_actor"}
)


def _limited_aliases(tree: ast.Module) -> dict[str, str]:
    """Map every name a limited callee is imported under to that callee's own name."""
    aliases = {name: name for name in _LIMITED_CALLEES}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _LIMITED_CALLEES and alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


def _calls_missing_the_limit(source: str, filename: str) -> tuple[int, list[str]]:
    """Count calls to a limited callee in ``source``; list those not passing a real limit.

    A call is reported when it omits ``request_prompt_limit``, passes it as the constant ``None``,
    or passes keywords only through a ``**`` splat. Callees imported under another name are
    resolved to their own name; a bare-name or attribute call is recognised either way.
    """
    tree = ast.parse(source, filename=filename)
    aliases = _limited_aliases(tree)
    calls = 0
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = aliases.get(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in _LIMITED_CALLEES:
            name = func.attr
        else:
            name = None
        if name is None:
            continue
        calls += 1
        limit = next((kw.value for kw in node.keywords if kw.arg == "request_prompt_limit"), None)
        if limit is None or (isinstance(limit, ast.Constant) and limit.value is None):
            missing.append(f"{filename}:{node.lineno} {name}")
    return calls, missing


def _unguarded_stream_calls(source: str, filename: str) -> tuple[int, list[str]]:
    """Count ``.stream(...)`` calls in ``source``; list those not preceded by the limit check.

    A ``.stream(`` call is guarded when its innermost enclosing function calls
    ``refuse_if_over_limit`` on an earlier line. A call outside any function is unguarded.
    """
    tree = ast.parse(source, filename=filename)
    functions = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]

    def innermost(line: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        enclosing = [fn for fn in functions if fn.lineno <= line <= (fn.end_lineno or fn.lineno)]
        return max(enclosing, key=lambda fn: fn.lineno, default=None)

    def check_lines(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "refuse_if_over_limit")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "refuse_if_over_limit"
                )
            )
            and innermost(node.lineno) is fn
        ]

    calls = 0
    unguarded: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "stream"
        ):
            continue
        calls += 1
        fn = innermost(node.lineno)
        if fn is None or not any(line < node.lineno for line in check_lines(fn)):
            unguarded.append(f"{filename}:{node.lineno}")
    return calls, unguarded


def test_every_production_call_of_a_model_call_site_passes_its_limit() -> None:
    """No caller under src/ or scripts/ omits the limit or passes ``None``, which disables it."""
    total = 0
    missing: list[str] = []
    for root in ("src", "scripts"):
        for path in sorted((_REPO_ROOT / root).rglob("*.py")):
            calls, gaps = _calls_missing_the_limit(
                path.read_text(encoding="utf-8"), str(path.relative_to(_REPO_ROOT))
            )
            total += calls
            missing.extend(gaps)
    assert total >= 4, f"expected the driver's, binder's and runner's calls, found {total}"
    assert missing == []


def test_every_model_stream_call_in_the_agents_follows_the_limit_check() -> None:
    """Every ``.stream(`` call under src/agents/ is preceded by ``refuse_if_over_limit``."""
    total = 0
    unguarded: list[str] = []
    for path in sorted(_AGENTS_DIR.rglob("*.py")):
        calls, gaps = _unguarded_stream_calls(
            path.read_text(encoding="utf-8"), str(path.relative_to(_REPO_ROOT))
        )
        total += calls
        unguarded.extend(gaps)
    assert total >= 3, f"expected the supervisor, subagent and tool streams, found {total}"
    assert unguarded == []


@pytest.mark.parametrize(
    "source",
    [
        "async def f():\n    await run_subagent_async(spec=s, intent=i, budget=b)\n",
        "def f():\n    run_subagent(spec=s, intent=i)\n",
        "async def f():\n    await tool_actor.formulate_and_invoke(t, i, r, provider=p)\n",
        "async def f():\n    await formulate_and_invoke(t, i, r, **kwargs)\n",
        "def f():\n    _bind_tool_actor(role, provider=p, resolved=t, dispatch=d, budget=b)\n",
        "async def f():\n    await run_subagent_async(spec=s, request_prompt_limit=None)\n",
        (
            "from agents.tool_actor import formulate_and_invoke as fai\n"
            "async def f():\n    await fai(t, i, r, provider=p)\n"
        ),
        (
            "import agents.tool_actor as ta\n"
            "async def f():\n    await ta.formulate_and_invoke(t, i, r, provider=p)\n"
        ),
    ],
    ids=[
        "async-runner",
        "sync-runner",
        "attribute-call",
        "kwargs-splat",
        "binder-forwarder",
        "explicit-none",
        "import-alias",
        "module-alias",
    ],
)
def test_the_scan_reds_on_a_call_that_omits_the_limit(source: str) -> None:
    calls, missing = _calls_missing_the_limit(source, "fixture.py")
    assert calls == 1
    assert len(missing) == 1


def test_the_scan_accepts_a_call_that_passes_the_limit() -> None:
    """Control: the same calls with a real limit are not reported."""
    source = (
        "from agents.tool_actor import formulate_and_invoke as fai\n"
        "async def f():\n"
        "    await run_subagent_async(spec=s, request_prompt_limit=n)\n"
        "    await fai(t, i, r, request_prompt_limit=matrix.request_prompt_limit(TOOL_TIER))\n"
    )
    assert _calls_missing_the_limit(source, "fixture.py") == (2, [])


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ("        request_prompt_limit=matrix.request_prompt_limit(TOOL_TIER),\n", ""),
        (
            "request_prompt_limit=matrix.request_prompt_limit(SUBAGENT_TIER),",
            "request_prompt_limit=None,",
        ),
    ],
    ids=["binder-limit-deleted", "subagent-limit-none"],
)
def test_the_scan_reds_on_a_bypass_applied_to_the_shipped_driver(
    needle: str, replacement: str
) -> None:
    source = _TURN_DRIVER.read_text(encoding="utf-8")
    assert _calls_missing_the_limit(source, "turn_driver.py")[1] == []
    assert source.count(needle) == 1
    _, missing = _calls_missing_the_limit(source.replace(needle, replacement), "turn_driver.py")
    assert len(missing) == 1, missing


@pytest.mark.parametrize(
    "source",
    [
        "async def f(p):\n    return p.stream(r, m, t)\n",
        (
            "async def f(p):\n"
            "    s = p.stream(r, m, t)\n"
            "    refuse_if_over_limit(m, t, limit=1, tier='tool', role='x', budget=None)\n"
        ),
        (
            "async def f(p):\n"
            "    refuse_if_over_limit(m, t, limit=1, tier='tool', role='x', budget=None)\n"
            "    async def g():\n"
            "        return p.stream(r, m, t)\n"
        ),
        "s = provider.stream(r, m, t)\n",
    ],
    ids=["no-check", "check-after-stream", "check-in-outer-function-only", "module-level"],
)
def test_the_stream_scan_reds_on_an_unguarded_model_call(source: str) -> None:
    calls, unguarded = _unguarded_stream_calls(source, "fixture.py")
    assert calls == 1
    assert len(unguarded) == 1


def test_the_stream_scan_accepts_a_call_after_the_check() -> None:
    source = (
        "async def f(p):\n"
        "    refuse_if_over_limit(m, t, limit=1, tier='tool', role='x', budget=None)\n"
        "    return p.stream(r, m, t)\n"
    )
    assert _unguarded_stream_calls(source, "fixture.py") == (1, [])


def test_the_stream_scan_reds_when_the_check_is_removed_from_a_shipped_site() -> None:
    path = _AGENTS_DIR / "tool_actor.py"
    source = path.read_text(encoding="utf-8")
    assert _unguarded_stream_calls(source, "tool_actor.py")[1] == []
    mutated = source.replace("refuse_if_over_limit(\n", "_not_the_check(\n", 1)
    assert mutated != source
    _, unguarded = _unguarded_stream_calls(mutated, "tool_actor.py")
    assert len(unguarded) == 1, unguarded
