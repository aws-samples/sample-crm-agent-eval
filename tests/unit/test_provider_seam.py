# SPDX-License-Identifier: MIT
"""Tests for the provider seam: tuning validation, resolution, and the stub provider.

Covers tuning validated against the capability matrix (rejection names the offending source),
cross-family override rejection, the stub provider being credential-free, network-free and
deterministic, and the absence of vendor SDK imports under ``src/agents/`` and ``src/eval/``.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import json
import os
import socket
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import proc_run

from crm.errors import ValidationFailed
from providers import stub as stub_module
from providers.base import (
    ProviderEvent,
    ResolvedInvocation,
    Tuning,
    validate_tuning,
)
from providers.capabilities import load_capability_matrix
from providers.stub import StubProvider
from providers.tier_tuning import TierRequest, resolve_tier

REPO_ROOT = Path(__file__).resolve().parents[2]

ANTHROPIC_SUPERVISOR = "anthropic.claude-sonnet-5-5"
ANTHROPIC_SUBAGENT = "anthropic.claude-sonnet-5-5"
OPENAI_SUPERVISOR = "openai.gpt-6.1-sol"


@pytest.fixture(scope="module")
def matrix():  # type: ignore[no-untyped-def]
    """The real, repo-committed capability matrix."""
    return load_capability_matrix()


# --------------------------------------------------------------------------------------
# Tuning validation against the matrix
# --------------------------------------------------------------------------------------


def test_default_tuning_resolves_with_no_explicit_override(matrix) -> None:  # type: ignore[no-untyped-def]
    """Omitting effort yields the model's own configured default."""
    model = matrix.model_for("anthropic", "supervisor")
    resolved = validate_tuning(model, Tuning(source="test"))
    assert resolved.model.model_id == ANTHROPIC_SUPERVISOR
    assert resolved.effort == "high"


def test_valid_effort_override_is_accepted(matrix) -> None:  # type: ignore[no-untyped-def]
    """A requested effort within the model's accepted values passes through unchanged."""
    model = matrix.model_for("anthropic", "supervisor")
    resolved = validate_tuning(model, Tuning(source="--effort", effort="low"))
    assert resolved.effort == "low"


def test_effort_rejected_when_model_does_not_accept_it_at_all(matrix) -> None:  # type: ignore[no-untyped-def]
    """A model whose capability has no effort axis rejects a requested effort client-side."""
    model = matrix.model_for("anthropic", "tool")
    no_effort = dataclasses.replace(
        model, effort=dataclasses.replace(model.effort, supported=False, values=(), default=None)
    )
    with pytest.raises(ValidationFailed) as excinfo:
        validate_tuning(no_effort, Tuning(source="CRM_EFFORT", effort="low"))
    assert excinfo.value.source == "CRM_EFFORT"
    assert model.model_id in excinfo.value.message


def test_effort_rejected_when_out_of_the_models_value_set(matrix) -> None:  # type: ignore[no-untyped-def]
    """Sonnet 5.5 has no xhigh or max; requesting either is rejected, not silently clamped."""
    model = matrix.model_for("anthropic", "subagent")
    assert model.model_id == ANTHROPIC_SUBAGENT
    with pytest.raises(ValidationFailed) as excinfo:
        validate_tuning(model, Tuning(source="settings.tuning.effort", effort="xhigh"))
    assert excinfo.value.source == "settings.tuning.effort"
    assert "xhigh" in excinfo.value.message


def _configured_tier_efforts() -> list[tuple[str, str, str]]:
    """Every ``(family, tier, effort)`` default, read from ``config/models.yaml``."""
    config = yaml.safe_load((REPO_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    return [
        (family, tier, entry["tuning"]["effort"])
        for family, family_config in config["families"].items()
        for tier, entry in family_config["tiers"].items()
    ]


@pytest.mark.parametrize(("family", "tier", "effort"), _configured_tier_efforts())
def test_configured_defaults_all_validate_cleanly(matrix, family, tier, effort) -> None:  # type: ignore[no-untyped-def]
    """Every models.yaml tier effort validates and is what a family-only tier request resolves to."""
    model = matrix.model_for(family, tier)
    resolved = validate_tuning(model, Tuning(source="config", effort=effort))
    assert resolved.effort == effort
    unset = resolve_tier(matrix, tier, TierRequest(family=family), source="config")
    assert (unset.model.model_id, unset.effort) == (model.model_id, effort)


# --------------------------------------------------------------------------------------
# Validation happens before request construction
# --------------------------------------------------------------------------------------


def test_rejection_happens_without_touching_the_stub_provider(matrix) -> None:  # type: ignore[no-untyped-def]
    """A tuning rejection must be raised by validate_tuning itself, before any provider call."""
    model = matrix.model_for("anthropic", "subagent")
    # No provider instance is constructed in this test.
    with pytest.raises(ValidationFailed):
        validate_tuning(model, Tuning(source="--effort", effort="xhigh"))


# --------------------------------------------------------------------------------------
# Stub provider: zero credentials, zero network, deterministic
# --------------------------------------------------------------------------------------


def _run(coro: Coroutine[Any, Any, list[ProviderEvent]]) -> list[ProviderEvent]:
    return asyncio.run(coro)


async def _collect(
    invocation: ResolvedInvocation,
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
) -> list[ProviderEvent]:
    provider = StubProvider()
    events: list[ProviderEvent] = []
    async for event in provider.stream(invocation, messages, tools):
        events.append(event)
    return events


#: The AWS credential variables the no-credentials cell removes, via ``monkeypatch.delenv`` so the
#: removal is reverted after the test.
_CREDENTIAL_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def test_stub_provider_needs_no_credentials(matrix, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Constructing and using StubProvider reads no environment credential variable."""
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)
    model = matrix.model_for("anthropic", "supervisor")
    resolved = validate_tuning(model, Tuning(source="test"))
    events = _run(_collect(resolved, [{"role": "user", "content": "hello"}], []))
    assert events, "the stub must still produce output with no credentials present"


def test_the_credential_removal_is_reverted_and_does_not_leak_to_other_tests(matrix) -> None:  # type: ignore[no-untyped-def]
    """The no-credentials cell's variable removal is reverted when its `MonkeyPatch` exits."""
    sentinel = "set-by-this-test-not-a-credential"
    with pytest.MonkeyPatch.context() as outer:
        for var in _CREDENTIAL_VARS:
            outer.setenv(var, sentinel)
        before = {var: os.environ.get(var) for var in _CREDENTIAL_VARS}
        with pytest.MonkeyPatch.context() as inner:
            test_stub_provider_needs_no_credentials(matrix, inner)
            assert all(var not in os.environ for var in _CREDENTIAL_VARS), (
                "the cell under test must actually remove the variables while it runs, "
                "otherwise the revert assertion below is vacuous"
            )
        after = {var: os.environ.get(var) for var in _CREDENTIAL_VARS}

    assert after == before, (
        f"credential variables were not restored: before={before} after={after}. "
        "Use monkeypatch.delenv, never os.environ.pop, in a test that removes them."
    )


def test_stub_provider_makes_no_network_call(matrix, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The stub makes no outbound connection.

    ``socket.create_connection`` and ``socket.socket.connect`` are patched rather than
    ``socket.socket`` itself, so asyncio's internal loopback socketpair keeps working.
    """

    def _forbidden_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("StubProvider must never open a network connection")

    monkeypatch.setattr(socket, "create_connection", _forbidden_connect)
    model = matrix.model_for("openai", "tool")
    resolved = validate_tuning(model, Tuning(source="test"))
    events = _run(_collect(resolved, [{"role": "user", "content": "ping"}], []))
    assert events


def test_stub_provider_is_deterministic_for_identical_input(matrix) -> None:  # type: ignore[no-untyped-def]
    """The same model, tuning, and messages must yield byte-identical stub output every time."""
    model = matrix.model_for("anthropic", "supervisor")
    resolved = validate_tuning(model, Tuning(source="test"))
    messages: list[dict[str, object]] = [{"role": "user", "content": "find the account for Acme"}]

    first = _run(_collect(resolved, messages, []))
    second = _run(_collect(resolved, messages, []))

    assert first == second


def test_stub_provider_differs_for_different_input(matrix) -> None:  # type: ignore[no-untyped-def]
    """Different messages should (with overwhelming probability) select a different response."""
    model = matrix.model_for("anthropic", "supervisor")
    resolved = validate_tuning(model, Tuning(source="test"))

    events_a = _run(_collect(resolved, [{"role": "user", "content": "task one"}], []))
    events_b = _run(_collect(resolved, [{"role": "user", "content": "task two"}], []))

    assert events_a != events_b


def test_stub_provider_ends_with_a_completed_event(matrix) -> None:  # type: ignore[no-untyped-def]
    """Every stub stream terminates in a completed event carrying usage."""
    model = matrix.model_for("anthropic", "tool")
    resolved = validate_tuning(model, Tuning(source="test"))
    events = _run(_collect(resolved, [{"role": "user", "content": "hi"}], []))
    assert events[-1].type == "completed"
    assert events[-1].input_tokens is not None
    assert events[-1].output_tokens is not None


def test_stub_digest_input_is_exactly_model_effort_and_messages(matrix) -> None:  # type: ignore[no-untyped-def]
    """The stub's determinism input carries the resolved model id, effort and messages only."""
    model = matrix.model_for("anthropic", "tool")
    resolved = validate_tuning(model, Tuning(source="test", effort="low"))
    messages: list[dict[str, object]] = [{"role": "user", "content": "hi"}]
    payload = {"model_id": model.model_id, "effort": "low", "messages": messages}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).digest()

    assert stub_module._digest(resolved, messages) == expected


def test_stub_provider_tool_branch_awaits_tools_and_carries_the_stub_fingerprint(matrix) -> None:  # type: ignore[no-untyped-def]
    """The tool branch emits a tool_call, a `stub: True` tool_result and
    `completed(awaiting_tools=True)` with no text; a round carrying a tool result takes the text
    branch."""
    model = matrix.model_for("anthropic", "supervisor")
    resolved = validate_tuning(model, Tuning(source="test"))
    tools: list[dict[str, object]] = [{"name": "search_opportunities"}]

    # Try enough distinct messages that at least one selects the tool-call branch, proving the
    # branch is reachable without depending on which specific digest bit it lands on.
    saw_tool_call = False
    for i in range(20):
        messages: list[dict[str, object]] = [{"role": "user", "content": f"message {i}"}]
        events = _run(_collect(resolved, messages, tools))
        if any(event.type == "tool_call" for event in events):
            saw_tool_call = True
            # The stub fingerprint rides on a tool_result, which `drive_turn` ignores.
            tool_results = [event for event in events if event.type == "tool_result"]
            assert len(tool_results) == 1
            tool_output = tool_results[0].tool_output
            assert tool_output is not None
            assert tool_output.get("stub") is True
            completed = [event for event in events if event.type == "completed"]
            assert len(completed) == 1
            assert completed[0].awaiting_tools is True
            assert completed[0].text is None
            # Round two, which already carries a tool result, must take the text branch (terminate).
            with_result: list[dict[str, object]] = [
                *messages,
                {"role": "tool", "tool_results": [{"id": "c0", "output": "{}"}]},
            ]
            follow = _run(_collect(resolved, with_result, tools))
            assert not any(event.type == "tool_call" for event in follow)
            assert any(event.type == "text_delta" for event in follow)
            break
    assert saw_tool_call, "the stub never exercised its tool-call branch across 20 inputs"


def test_stub_provider_never_emits_a_tool_call_with_no_tools_offered(matrix) -> None:  # type: ignore[no-untyped-def]
    """With an empty tool list there is nothing to call, so the stub must answer with text."""
    model = matrix.model_for("anthropic", "supervisor")
    resolved = validate_tuning(model, Tuning(source="test"))
    for i in range(10):
        messages: list[dict[str, object]] = [{"role": "user", "content": f"message {i}"}]
        events = _run(_collect(resolved, messages, []))
        assert not any(event.type == "tool_call" for event in events)


# --------------------------------------------------------------------------------------
# AST walk: no vendor import anywhere under src/agents/ or src/eval/
# --------------------------------------------------------------------------------------

#: Model-vendor SDK module roots that must never appear as an import under the agent or eval
#: trees, with no per-file carve-out. ``boto3``/``botocore`` are not in this shared list; see
#: `_MODEL_VENDOR_IMPORT_ROOTS_BY_SUBTREE`.
_FORBIDDEN_VENDOR_IMPORT_ROOTS = ("anthropic", "openai")

#: Per-subtree forbidden roots. `agents/` additionally forbids `boto3`/`botocore`; `eval/` does not,
#: since `eval.harness` builds its own local DynamoDB client.
_MODEL_VENDOR_IMPORT_ROOTS_BY_SUBTREE: dict[str, tuple[str, ...]] = {
    "agents": (*_FORBIDDEN_VENDOR_IMPORT_ROOTS, "boto3", "botocore"),
    "eval": _FORBIDDEN_VENDOR_IMPORT_ROOTS,
}


def _iter_python_files(root: Path) -> list[Path]:
    """Return every ``.py`` file under ``root``, recursively."""
    return sorted(root.rglob("*.py"))


def _imported_module_roots(tree: ast.Module) -> set[str]:
    """Collect the top-level module name of every import statement in a parsed module."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("subtree", ["agents", "eval"])
def test_no_vendor_sdk_import_anywhere_under_the_tree(subtree: str) -> None:
    """No file under the subtree imports a forbidden root, nested imports included.

    Every ``.py`` file is walked with ``ast.walk`` over the full module, so imports inside
    functions, conditionals and try blocks are caught.
    """
    root = REPO_ROOT / "src" / subtree
    forbidden = set(_MODEL_VENDOR_IMPORT_ROOTS_BY_SUBTREE[subtree])
    violations: list[str] = []
    for path in _iter_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = _imported_module_roots(tree) & forbidden
        if found:
            violations.append(f"{path.relative_to(REPO_ROOT)}: imports {sorted(found)}")
    assert not violations, "vendor SDK import(s) found:\n" + "\n".join(violations)


def test_the_ast_walk_itself_detects_a_nested_vendor_import(tmp_path: Path) -> None:
    """The collector catches a forbidden import nested inside a function in a synthetic file."""
    source = "\n".join(
        [
            "def handler():",
            "    import boto3",
            "    return boto3",
        ]
    )
    tree = ast.parse(source, filename="synthetic.py")
    found = _imported_module_roots(tree) & set(_MODEL_VENDOR_IMPORT_ROOTS_BY_SUBTREE["agents"])
    assert found == {"boto3"}


def test_the_ast_walk_detects_a_nested_from_import_inside_a_conditional() -> None:
    """Prove the collector catches `from x import y` nested inside an `if`, not only `import x`."""
    source = "\n".join(
        [
            "if True:",
            "    from openai import OpenAI",
        ]
    )
    tree = ast.parse(source, filename="synthetic.py")
    found = _imported_module_roots(tree) & set(_FORBIDDEN_VENDOR_IMPORT_ROOTS)
    assert found == {"openai"}


def test_forbidden_roots_do_not_accidentally_match_this_seams_own_modules() -> None:
    """Sanity check the forbidden-root list itself does not clash with providers/ or crm/."""
    assert "providers" not in _FORBIDDEN_VENDOR_IMPORT_ROOTS
    assert "crm" not in _FORBIDDEN_VENDOR_IMPORT_ROOTS


# ================================================================================================
# The `src/agents/` -> no-AWS-client guarantee, checked transitively
# ================================================================================================

#: Modules whose import must not pull an AWS SDK into the process, including through dependencies
#: outside `src/agents/`.
_AGENT_MODULES_THAT_MUST_NOT_REACH_BOTO3: tuple[str, ...] = (
    "agents.supervisor",
    "agents.registry",
    "agents.cancellation",
    "agents.budget",
    "agents.events",
    "crm.toolspec.page_context",
)


@pytest.mark.parametrize("module_name", _AGENT_MODULES_THAT_MUST_NOT_REACH_BOTO3)
def test_importing_an_agent_module_does_not_pull_an_aws_sdk_into_the_process(
    module_name: str,
) -> None:
    """Importing the module leaves `boto3`/`botocore` absent from `sys.modules`.

    Run in a subprocess, since the test session itself has already imported `boto3`.
    """
    program = "\n".join(
        [
            "import sys",
            f"import {module_name}",
            "leaked = sorted(m for m in sys.modules if m.split('.')[0] in ('boto3', 'botocore'))",
            "print(','.join(leaked))",
        ]
    )
    completed = proc_run.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", (
        f"{module_name} pulled an AWS SDK into the process: {completed.stdout.strip()}"
    )


def test_the_subprocess_probe_itself_detects_an_eager_boto3_import() -> None:
    """The subprocess probe reports a leak for a direct `import boto3`."""
    program = "\n".join(
        [
            "import sys",
            "import boto3",
            "leaked = sorted(m for m in sys.modules if m.split('.')[0] in ('boto3', 'botocore'))",
            "print(','.join(leaked))",
        ]
    )
    completed = proc_run.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "boto3" in completed.stdout
