# SPDX-License-Identifier: MIT
"""Eval-only, registry-declared tool faults.

A golden task may declare ``fault_injection: {tool, fault}``. :func:`with_injected_faults` wraps an
eval turn's dispatch so that every call to the declared tool fails through the real
:func:`crm.dispatch.invoke_action` transport-error path, which yields the taxonomy
``internal_error`` payload. Activation needs the task id's registry declaration and an
``eval-<run_id>`` tenant; no tool argument or request field can switch it on.
:func:`eval.turn_observation.observe_turn` is this module's only importer.

This module imports no vendor SDK and constructs no AWS client.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast, get_args

import yaml
from botocore.exceptions import EndpointConnectionError

from agents.registry import RegisteredTool
from agents.tool_actor import ToolDispatch
from agents.tool_dispatch import action_function_name
from crm.dispatch import invoke_action
from crm.toolspec.definitions import ALL_TOOLS
from eval.tenancy import run_id_for_tenant

FaultKind = Literal["transport"]

#: The golden task key declaring a fault.
FAULT_INJECTION_KEY: Final[str] = "fault_injection"

#: The endpoint the unreachable Lambda client reports; ``.invalid`` never resolves (RFC 2606).
UNREACHABLE_ENDPOINT_URL: Final[str] = "https://lambda.invalid"

#: The packaged golden task registry, beside this module.
GOLDEN_TASKS_PATH: Final[Path] = Path(__file__).resolve().parent / "golden_tasks.yaml"

_FAULT_KINDS: Final[frozenset[str]] = frozenset(get_args(FaultKind))
_SPEC_KEYS: Final[frozenset[str]] = frozenset({"tool", "fault"})


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """One declared fault: every call to ``tool`` fails with ``fault``."""

    tool: str
    fault: FaultKind


class FaultInjectionRefused(ValueError):  # noqa: N818
    """A declared fault was requested outside an ``eval-<run_id>`` tenant."""


class _UnreachableLambda:
    """A Lambda client whose every ``invoke`` fails as an unreachable endpoint."""

    def invoke(self, **_: Any) -> Any:
        raise EndpointConnectionError(endpoint_url=UNREACHABLE_ENDPOINT_URL)


def _parse_spec(task_id: str, raw: object) -> FaultSpec:
    if not isinstance(raw, Mapping) or frozenset(raw) != _SPEC_KEYS:
        raise ValueError(
            f"task {task_id!r}: {FAULT_INJECTION_KEY} must be a mapping with exactly the keys "
            f"{sorted(_SPEC_KEYS)}"
        )
    tool, fault = raw["tool"], raw["fault"]
    if tool not in {definition.name for definition in ALL_TOOLS}:
        raise ValueError(f"task {task_id!r}: {FAULT_INJECTION_KEY} names unknown tool {tool!r}")
    if fault not in _FAULT_KINDS:
        raise ValueError(f"task {task_id!r}: {FAULT_INJECTION_KEY} names unknown fault {fault!r}")
    return FaultSpec(tool=str(tool), fault=cast(FaultKind, fault))


_DECLARED_FAULTS_BY_PATH: dict[Path, Mapping[str, FaultSpec]] = {}


def _declared_faults(registry_path: Path) -> Mapping[str, FaultSpec]:
    """Every fault ``registry_path`` declares, by task id; validated in full, cached per path.

    Raises:
        ValueError: The file is not a golden task registry, or a declaration names an unknown
            tool or fault, repeats a task id, or repeats a fault another task already declares.
    """
    cached = _DECLARED_FAULTS_BY_PATH.get(registry_path)
    if cached is not None:
        return cached
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping) or not isinstance(data.get("tasks"), list):
        raise ValueError(
            f"{registry_path} did not parse to a mapping with a top-level 'tasks' list"
        )
    declared: dict[str, FaultSpec] = {}
    for task in data["tasks"]:
        if not isinstance(task, Mapping) or FAULT_INJECTION_KEY not in task:
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"a task declaring {FAULT_INJECTION_KEY} has no string id")
        spec = _parse_spec(task_id, task[FAULT_INJECTION_KEY])
        if task_id in declared:
            raise ValueError(f"task {task_id!r} declares {FAULT_INJECTION_KEY} more than once")
        if spec in declared.values():
            raise ValueError(f"fault {spec} is declared by more than one task id")
        declared[task_id] = spec
    _DECLARED_FAULTS_BY_PATH[registry_path] = declared
    return declared


def _default_registry_path() -> Path:
    return GOLDEN_TASKS_PATH


def fault_for_task(task_id: str, *, registry_path: Path | None = None) -> FaultSpec | None:
    """The golden registry's declared fault for ``task_id``, or ``None``.

    Reads :data:`GOLDEN_TASKS_PATH` (or ``registry_path``) with ``yaml.safe_load``; the parsed
    declarations are cached per path.

    Raises:
        ValueError: The registry is malformed (see :func:`_declared_faults`).
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    return _declared_faults(path).get(task_id)


def with_injected_faults(
    dispatch: ToolDispatch,
    *,
    task_id: str,
    tenant: str,
    registry_path: Path | None = None,
) -> ToolDispatch:
    """Return ``dispatch``, or a dispatch failing every call to ``task_id``'s declared tool.

    With no declared fault, ``dispatch`` itself is returned. Otherwise each call to the declared
    tool returns :func:`crm.dispatch.invoke_action`'s response for an unreachable Lambda endpoint,
    and every other tool delegates to ``dispatch``.

    Raises:
        FaultInjectionRefused: A fault is declared but ``tenant`` is not ``eval-<run_id>``.
        ValueError: The registry is malformed.
    """
    spec = fault_for_task(task_id, registry_path=registry_path)
    if spec is None:
        return dispatch
    try:
        run_id = run_id_for_tenant(tenant)
    except ValueError:
        run_id = ""
    if not run_id:
        raise FaultInjectionRefused(
            f"task {task_id!r} declares a fault; faults run only in an eval-<run_id> tenant"
        )
    unreachable = _UnreachableLambda()

    def faulted(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        if tool.name != spec.tool:
            return dispatch(tool, tool_input)
        return invoke_action(action_function_name(tool.name), dict(tool_input), client=unreachable)

    return faulted


__all__ = [
    "FAULT_INJECTION_KEY",
    "GOLDEN_TASKS_PATH",
    "UNREACHABLE_ENDPOINT_URL",
    "FaultInjectionRefused",
    "FaultKind",
    "FaultSpec",
    "fault_for_task",
    "with_injected_faults",
]
