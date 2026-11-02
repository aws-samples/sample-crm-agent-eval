# SPDX-License-Identifier: MIT
"""The eval harness: stub-by-default CLI and library entry point for one model-comparison sweep.

Sequences :mod:`eval.tenancy` (per-run tenant), :mod:`eval.sweep_units` (one scored task unit per
arm x task, and one assembly per arm), :mod:`eval.turn_observation` (the observed turn) and
:mod:`eval.matrix` (the output artifacts); it holds no scoring, metering or rendering logic itself.

:func:`run_sweep` is the single entry point for the CLI (:func:`main`) and the UI. Stub mode is the
default; live mode needs ``mode="live"`` (``--live``) and the ``live-aws`` substrate. A sweep
allocates one run id, records and seeds one fresh unit tenant per (arm, repeat), runs each unit's
golden tasks in that unit's tenant, publishes the ``demo`` tenant delta to
``<output_dir>/verification-inventory/eval_sweep.json``, and tears every unit tenant down in a
``finally``. This module never imports ``anthropic`` or ``openai``; live adapters are built
lazily through :mod:`eval.live`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv as csv_module
import importlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import boto3
import yaml

from agents.registry import RegisteredTool
from agents.turn_driver import TurnRecords, session_turn_tuning
from crm.config import resolve_aws_config
from crm.dispatch import invoke_action
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    IDENTITY_KEY,
    Identity,
    decode_identity,
    identity_event,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.matrix import (
    REPEAT_COMPLETION_KEY,
    MatrixResult,
    StubMatrixOverwriteRefused,
    archive_raw_run,
    build_matrix_result,
    write_matrix_artifacts,
)
from eval.pricing import is_cost_provisional, load_price_table_status
from eval.scoring import (
    TurnOutcome,
)
from eval.sweep_units import (
    DEFAULT_ARM_EFFORT,
    ArmRunResult,
    ArmSpec,
    EvalMode,
    RawRecordsWriter,
    arm_tuning,
    assemble_arm,
    run_task_unit,
    task_actor,
)
from eval.task_ids import localize_tasks
from eval.tenancy import (
    TenantTeardownError,
    allocate_run_id,
    record_run_started,
    record_run_torn_down,
    seed_unit_tenants,
    teardown_unit_tenants,
    tenant_for_run,
    unit_tenants_for_run,
)
from eval.turn_observation import (
    ObservedTurn,
    drain_provider_exclusions,
    observe_turn,
    tool_output_reports_truncation,
)
from providers.base import ModelProvider, ResolvedInvocation, Tuning
from providers.capabilities import CapabilityMatrix, Family, load_capability_matrix
from providers.stub import StubProvider

if TYPE_CHECKING:  # pragma: no cover - import cycle-free typing only
    from eval.tenant_guard import TenantSnapshot
    from eval.verification import CheckResult


_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEFAULT_GOLDEN_TASKS_PATH: Final[Path] = Path(__file__).resolve().parent / "golden_tasks.yaml"
_DEFAULT_RESULTS_DIR: Final[Path] = _REPO_ROOT / "results"
#: The CLI's default output directory for a stub sweep (gitignored); live keeps
#: :data:`_DEFAULT_RESULTS_DIR`.
_DEFAULT_STUB_RESULTS_DIR: Final[Path] = _DEFAULT_RESULTS_DIR / "stub"

#: DynamoDB Local's default loopback endpoint; the CLI's default target.
_DEFAULT_LOCAL_ENDPOINT_URL: Final[str] = "http://127.0.0.1:8000"

#: Placeholder credential for DynamoDB Local, which performs no IAM check. Passed explicitly to
#: `boto3.client`, so botocore's ambient credential chain is not consulted on the default path.
_LOCAL_PLACEHOLDER_ACCESS_KEY_ID: Final[str] = "local"
_LOCAL_PLACEHOLDER_SECRET_ACCESS_KEY: Final[str] = "local"  # noqa: S105
_LOCAL_PLACEHOLDER_REGION: Final[str] = "us-east-1"

#: The CRM substrate a sweep runs against, independent of ``--live`` (model spend).
#:
#: ``local`` (default): DynamoDB Local with the placeholder credential, and tool calls dispatched
#: in-process to the handler module on the Gateway ``_identity`` channel.
#:
#: ``live-aws``: a DynamoDB client from the caller's ambient credentials, and tool calls dispatched
#: by ``lambda:InvokeFunction`` of the deployed action Lambda on the ``_agent_identity`` channel.
#: The supervisor loop still runs in this process, not in the deployed Runtime.
Substrate = Literal["local", "live-aws"]
_SUBSTRATE_LOCAL: Final[str] = "local"
_SUBSTRATE_LIVE_AWS: Final[str] = "live-aws"

#: Environment variables the CLI reads when the corresponding flag is absent.
_SUBSTRATE_ENV_VAR: Final[str] = "EVAL_SUBSTRATE"
_TABLE_PREFIX_ENV_VAR: Final[str] = "CRM_TABLE_PREFIX"

#: Runtime identifier recorded in provenance for a ``live-aws`` sweep, which never invokes the
#: deployed Runtime.
_LIVE_AWS_RUNTIME_IDENTIFIER: Final[str] = "live-aws-lambda-dispatch"

#: Where a sweep publishes its ``demo``-untouched delta, relative to ``--output-dir``, in the smoke
#: scripts' inventory shape.
FR93_INVENTORY_RELATIVE_PATH: Final[Path] = Path("verification-inventory") / "eval_sweep.json"

#: The resource class and layer the delta inventory records itself under.
_FR93_RESOURCE_CLASS: Final[str] = "eval_sweep"
_FR93_LAYER: Final[int] = 6

#: The checks the delta inventory must carry at `pass` for :func:`assert_artifacts` to accept it.
_FR93_REQUIRED_CHECKS: Final[tuple[str, ...]] = (
    "demo_tenant_untouched",
    "writes_only_under_smoke_tenant",
)

#: Whole-word tokenizer for the verdict-word scan; underscore is a separator.
_WORD_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z]+")


#: A copy of ``fixtures.generate.SEED``, recorded in every result's provenance.
DEFAULT_DATASET_SEED: Final[int] = 20260101


#: The default sweep matrix: {Anthropic, OpenAI} x {effort medium}, two arms.
DEFAULT_ARM_SPECS: Final[tuple[ArmSpec, ...]] = (
    ArmSpec(family="anthropic", effort=DEFAULT_ARM_EFFORT),
    ArmSpec(family="openai", effort=DEFAULT_ARM_EFFORT),
)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """The full outcome of one :func:`run_sweep` invocation.

    Attributes:
        run_id: The eval run identifier allocated for this sweep.
        tenant: The run's base tenant ``eval-<run_id>``, recorded on its ``eval_runs`` row.
        mode: Which provider backend produced this sweep's results.
        matrix: The rendered :class:`eval.matrix.MatrixResult` covering every arm.
        arm_runs: Every arm's detailed result, in sweep order.
        artifact_paths: Paths written for ``matrix.json``, ``matrix.md``, and ``matrix.csv``, or
            an empty mapping when ``output_dir`` was not supplied to :func:`run_sweep`.
        raw_archive_paths: Paths written for each arm's raw JSON archive under ``results/raw/``,
            or an empty tuple when archiving was not requested.
        teardown_error: The :class:`eval.tenancy.TenantTeardownError` raised during teardown, if
            any. :func:`run_sweep` re-raises it after populating this field.
        fr93_checks: The ``demo_tenant_untouched`` and ``writes_only_under_smoke_tenant`` checks,
            computed before teardown by ``eval.tenant_guard``. Empty only when the sweep raised
            before reaching teardown.
        fr93_inventory_path: Where those checks were published, or ``None`` when ``output_dir`` was
            not supplied.
        unit_tenants: The per-(arm, repeat) tenants the tasks ran in, arm-major
            (:func:`eval.tenancy.unit_tenants_for_run`).
    """

    run_id: str
    tenant: str
    mode: EvalMode
    matrix: MatrixResult
    arm_runs: tuple[ArmRunResult, ...] = field(default_factory=tuple)
    artifact_paths: dict[str, Path] = field(default_factory=dict)
    raw_archive_paths: tuple[Path, ...] = field(default_factory=tuple)
    teardown_error: TenantTeardownError | None = None
    fr93_checks: tuple[CheckResult, ...] = field(default_factory=tuple)
    fr93_inventory_path: Path | None = None
    unit_tenants: tuple[str, ...] = field(default_factory=tuple)


def _git_revision() -> str:
    """Return the repository's current git commit hash, or ``"unknown"`` when unavailable.

    Never raises: a ``git`` binary absent from ``PATH`` or a non-git checkout yields the
    placeholder. ``git`` is invoked by the absolute path ``shutil.which`` resolves.
    """
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        return "unknown"
    launch = subprocess.run
    try:
        completed = launch(  # nosec B603 - absolute git path, fixed argv, no shell
            [git, "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and revision else "unknown"


def _load_golden_tasks(path: Path) -> list[dict[str, object]]:
    """Load and parse ``golden_tasks.yaml`` into a list of plain task mappings.

    Validates only that a top-level ``tasks`` list is present.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "tasks" not in data:
        raise ValueError(f"{path} did not parse to a mapping with a top-level 'tasks' key")
    tasks = data["tasks"]
    if not isinstance(tasks, list):
        raise ValueError(f"{path}'s 'tasks' key must be a list")
    return tasks


def _build_provider(mode: EvalMode, *, family: Family = "anthropic") -> ModelProvider:
    """Return the provider for ``mode`` and ``family``: the sole provider-construction seam.

    Stub mode returns :class:`providers.stub.StubProvider` and ignores ``family``. Live mode
    returns the family's adapter from :func:`eval.live.build_live_provider`, for the region
    :func:`crm.config.resolve_aws_config` resolves.

    Args:
        mode: ``"stub"`` or ``"live"``.
        family: Which family's adapter to build for live mode. Ignored for stub mode.

    Returns:
        A zero-cost :class:`providers.stub.StubProvider` for stub mode, or the family's live
        adapter for live mode.
    """
    if mode == "live":
        from eval.live import build_live_provider

        return build_live_provider(family, region_name=resolve_aws_config(validate=False).region)
    return StubProvider()


def _resolve_handler(tool: RegisteredTool) -> Any:
    """Import and return the handler callable named by ``tool.handler``'s dotted path."""
    module_path, _, attr = tool.handler.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _identity_payload_for_actor(actor: dict[str, object], *, tenant: str) -> dict[str, object]:
    """Build the gateway ``_identity`` block for a golden task's trusted ``actor`` fixture.

    ``actor`` carries ``rep_id``, ``book`` and ``full_visibility``.
    """
    book_raw = actor.get("book") or []
    if not isinstance(book_raw, list):
        raise ValueError("actor.book must be a list of account ids")
    return {
        "rep_id": str(actor["rep_id"]),
        "book": [str(account_id) for account_id in book_raw],
        "full_visibility": bool(actor.get("full_visibility", False)),
        "principal_type": "user",
        "session_id": "",
        "source": "gateway",
        "tenant": tenant,
    }


def _agent_identity_for_actor(actor: dict[str, object], *, tenant: str) -> Identity:
    """Build the :class:`crm.envelope.Identity` the deployed ``_agent_identity`` channel carries.

    The actor is rendered into Cognito access-token claim keys and decoded by
    :func:`crm.envelope.decode_identity`, so claims a real token could not carry are refused.

    Args:
        actor: The task's ``actor`` block (``rep_id``, ``book``, ``full_visibility``).
        tenant: The eval tenant the turn executes within.

    Returns:
        The identity the ``live-aws`` substrate's dispatch presents to the deployed action Lambda.
    """
    book_raw = actor.get("book") or []
    if not isinstance(book_raw, list):
        raise ValueError("actor.book must be a list of account ids")
    claims_event = identity_event(
        AGENT_IDENTITY_KEY,
        {
            "claims": {
                CLAIM_REP_ID: str(actor["rep_id"]),
                CLAIM_BOOK: [str(account_id) for account_id in book_raw],
                CLAIM_FULL_VISIBILITY: bool(actor.get("full_visibility", False)),
                CLAIM_TENANT: tenant,
            },
            "session_id": "",
        },
        {},
    )
    return decode_identity(claims_event)


def _dispatch_tool_call(
    tool: RegisteredTool,
    tool_input: dict[str, object],
    *,
    store: Store,
    identity_payload: dict[str, object],
) -> dict[str, Any]:
    """Invoke ``tool``'s Lambda handler entry point in-process with a Gateway-channel event.

    The event is built by :func:`crm.envelope.identity_event`, so ``tool_input`` cannot shadow the
    identity channel.
    """
    handler = _resolve_handler(tool)
    event = identity_event(IDENTITY_KEY, identity_payload, tool_input)
    return dict(handler(event, store=store))


#: Dispatches one tool call: the tool and the model's arguments in, the handler's wire response out.
TurnDispatch = Callable[[RegisteredTool, dict[str, object]], dict[str, Any]]


class DispatchFactory(Protocol):
    """Builds the :data:`TurnDispatch` one task's turn dispatches every tool call through."""

    def __call__(self, *, store: Store, actor: dict[str, object], tenant: str) -> TurnDispatch: ...


def in_process_dispatch_factory(
    *, store: Store, actor: dict[str, object], tenant: str
) -> TurnDispatch:
    """Dispatch in-process to the imported handler module, on the Gateway ``_identity`` channel.

    The ``local`` substrate's dispatcher; it makes no AWS call.

    Args:
        store: The persistence layer the handler reads and writes directly.
        actor: The golden task's own ``actor`` block.
        tenant: The eval tenant the turn executes within.

    Returns:
        A :data:`TurnDispatch` closing over ``store`` and the actor's identity payload.
    """
    identity_payload = _identity_payload_for_actor(actor, tenant=tenant)

    def _dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        return _dispatch_tool_call(tool, tool_input, store=store, identity_payload=identity_payload)

    return _dispatch


def live_lambda_dispatch_factory(
    *, store: Store, actor: dict[str, object], tenant: str
) -> TurnDispatch:
    """Dispatch by ``lambda:InvokeFunction`` of the deployed action Lambda.

    The ``live-aws`` substrate's dispatcher. Uses the deployed Runtime's own function-name
    resolution and ``_agent_identity`` event constructor, and :func:`crm.dispatch.invoke_action`,
    which returns a function-level failure as a taxonomy-shaped response.

    Args:
        store: Unused; accepted so both factories satisfy :class:`DispatchFactory`.
        actor: The golden task's ``actor`` block, decoded by :func:`_agent_identity_for_actor`.
        tenant: The eval tenant the turn executes within.

    Returns:
        A :data:`TurnDispatch` invoking the deployed action Lambda for the tool it is handed.
    """
    # `agents.runtime_server` builds a Starlette app at import time, so it is imported lazily.
    from agents.runtime_server import action_function_name, agent_identity_event

    del store
    identity = _agent_identity_for_actor(actor, tenant=tenant)

    def _dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        return invoke_action(
            action_function_name(tool.name), agent_identity_event(identity, tool_input)
        )

    return _dispatch


def _observe_provider_turn(
    provider: ModelProvider,
    *,
    matrix: CapabilityMatrix,
    tuning: Tuning,
    prompt: str,
    store: Store,
    tenant: str,
    identity_payload: dict[str, object],
    task_id: str,
    dispatch: TurnDispatch | None = None,
    max_rounds: int | None = None,
    dispatch_clock: Callable[[], float] | None = None,
    turn_clock: Callable[[], float] | None = None,
) -> ObservedTurn:
    """Drive one in-process turn through :func:`eval.turn_observation.observe_turn`.

    The turn is single-turn, with no history and no page context.

    Args:
        provider: The arm's provider, serving every tier of its family.
        matrix: The capability matrix each tier's model is resolved against.
        tuning: The arm's tuning, mapped onto the tiers by
            :func:`agents.turn_driver.session_turn_tuning`; the turn runs under
            :data:`agents.budget.EVAL_BUDGET`.
        prompt: The golden task's prompt.
        store: The persistence layer the dispatched tool call reads and writes.
        tenant: The eval tenant the turn executes within, carried into the ``TurnOutcome``.
        identity_payload: The ``_identity`` block passed to any in-process dispatched handler.
        task_id: The golden task's id; also the turn's ``session_id`` (the cancel marker's key).
        dispatch: How each tool call is dispatched. ``None`` (default) dispatches in-process against
            ``store`` on the Gateway ``_identity`` channel.
        max_rounds: Backstop on supervisor rounds, or ``None`` for the driver's default.
        dispatch_clock: A monotonic-seconds clock used to time each leaf tool dispatch, or
            ``None`` to leave dispatch latency unrecorded.
        turn_clock: A monotonic-seconds clock timing the whole turn, or ``None``.
    """

    def _in_process(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        return _dispatch_tool_call(tool, tool_input, store=store, identity_payload=identity_payload)

    return asyncio.run(
        observe_turn(
            prompt=prompt,
            task_id=task_id,
            tenant=tenant,
            providers=lambda _family: provider,
            matrix=matrix,
            tuning=session_turn_tuning(tuning),
            dispatch=dispatch if dispatch is not None else _in_process,
            max_rounds=max_rounds,
            dispatch_clock=dispatch_clock,
            turn_clock=turn_clock,
        )
    )


def _run_provider_turn(
    provider: ModelProvider,
    *,
    matrix: CapabilityMatrix,
    tuning: Tuning,
    prompt: str,
    store: Store,
    tenant: str,
    identity_payload: dict[str, object],
    task_id: str,
    dispatch: TurnDispatch | None = None,
    max_rounds: int | None = None,
    dispatch_clock: Callable[[], float] | None = None,
) -> tuple[TurnOutcome, TurnRecords, int]:
    """Drive one in-process turn and return its outcome, attribution and stream-call count.

    Arguments as :func:`_observe_provider_turn`.

    Returns:
        The :class:`eval.scoring.TurnOutcome`, the :class:`TurnRecords` attribution the driver
        appended, and the number of ``provider.stream`` calls the driver made.
    """
    turn = _observe_provider_turn(
        provider,
        matrix=matrix,
        tuning=tuning,
        prompt=prompt,
        store=store,
        tenant=tenant,
        identity_payload=identity_payload,
        task_id=task_id,
        dispatch=dispatch,
        max_rounds=max_rounds,
        dispatch_clock=dispatch_clock,
    )
    return turn.outcome, turn.records, turn.stream_calls


#: The truncation predicate, kept under its historical name.
_tool_output_reports_truncation = tool_output_reports_truncation

#: The exclusion drain, kept under its historical name.
_drain_provider_exclusions = drain_provider_exclusions


class _StreamCallCounter(ModelProvider):
    """Wraps a provider, delegating unchanged and counting :meth:`stream` calls in :attr:`call_count`."""

    def __init__(self, wrapped: ModelProvider) -> None:
        self._wrapped = wrapped
        self.call_count = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> Any:
        """Delegate to the wrapped provider's ``stream``, incrementing :attr:`call_count` first."""
        self.call_count += 1
        return self._wrapped.stream(invocation, messages, tools)


def _raw_records_writer(raw_records_dir: Path) -> RawRecordsWriter:
    """Return a :data:`RawRecordsWriter` that writes into ``raw_records_dir``.

    Each call writes ``pre-aggregation-arm-<index>-<model id>.json``, with ``/`` and ``:`` in the
    model id replaced by ``_``.
    """

    def _write_raw_records(arm_index: int, model_id: str, payload: dict[str, object]) -> None:
        safe_model_id = model_id.replace("/", "_").replace(":", "_")
        path = raw_records_dir / f"pre-aggregation-arm-{arm_index}-{safe_model_id}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return _write_raw_records


def _run_arm(
    *,
    provider: ModelProvider,
    matrix: CapabilityMatrix,
    arm_spec: ArmSpec,
    tasks: list[dict[str, object]],
    store: Store,
    mode: EvalMode,
    dataset_seed: int,
    git_revision: str,
    runtime_identifier: str,
    price_table_version: str,
    cost_withheld: bool,
    dispatch_factory: DispatchFactory = in_process_dispatch_factory,
    repeats: int = 1,
    arm_index: int = 0,
    raw_records_writer: RawRecordsWriter | None = None,
    tenant: str | None = None,
    unit_tenants: Sequence[str] | None = None,
) -> ArmRunResult:
    """Run every golden task once per repeat for one arm and assemble its scored, metered result.

    With ``unit_tenants``, ``tasks`` are tenant-relative and repeat ``r`` localizes them to
    ``unit_tenants[r]`` and runs them there, in file order; ``repeats`` and ``tenant`` are ignored.
    Without it, ``tasks`` are already localized to ``tenant`` and every repeat runs there. Each
    task is one :func:`eval.sweep_units.run_task_unit` over an in-process
    :func:`_observe_provider_turn`; :func:`eval.sweep_units.assemble_arm` folds them.

    Raises ``ValueError`` when neither ``tenant`` nor ``unit_tenants`` is given.

    Raises ``AssertionError`` when the arm's ``stream`` call count differs from the calls its turn
    drivers made, or is below ``included_invocation_count``.
    """
    tuning = arm_tuning(arm_spec)
    counting_provider = _StreamCallCounter(provider)
    measured_latency = mode == "live"

    def _turn_runner(task: Mapping[str, object], unit_tenant: str) -> ObservedTurn:
        actor = task_actor(task)
        return _observe_provider_turn(
            counting_provider,
            matrix=matrix,
            tuning=tuning,
            prompt=str(task.get("prompt") or ""),
            store=store,
            tenant=unit_tenant,
            identity_payload=_identity_payload_for_actor(actor, tenant=unit_tenant),
            task_id=str(task.get("id") or "unknown-task"),
            dispatch=dispatch_factory(store=store, actor=actor, tenant=unit_tenant),
            dispatch_clock=time.monotonic if measured_latency else None,
            turn_clock=time.monotonic if measured_latency else None,
        )

    if unit_tenants is not None:
        unit_tasks = [
            (unit_tenant, localize_tasks(tasks, tenant=unit_tenant)) for unit_tenant in unit_tenants
        ]
    elif tenant is not None:
        unit_tasks = [(tenant, tasks) for _rep_index in range(repeats)]
    else:
        raise ValueError("_run_arm needs a tenant or unit_tenants")
    units = [
        run_task_unit(
            store=store,
            task=task,
            tenant=unit_tenant,
            family=arm_spec.family,
            turn_runner=_turn_runner,
        )
        for unit_tenant, localized in unit_tasks
        for task in localized
    ]
    return assemble_arm(
        units,
        matrix=matrix,
        arm_spec=arm_spec,
        mode=mode,
        dataset_seed=dataset_seed,
        git_revision=git_revision,
        runtime_identifier=runtime_identifier,
        price_table_version=price_table_version,
        cost_withheld=cost_withheld,
        arm_index=arm_index,
        provider_stream_calls=counting_provider.call_count,
        residual_exclusions=drain_provider_exclusions(provider),
        raw_records_writer=raw_records_writer,
    )


class UndisclosedZeroInvocationRenderError(RuntimeError):
    """Raised when a sweep's rendered artifacts would report figures from zero invocations.

    Raised by :func:`run_sweep` when an arm has scored tasks but an
    :attr:`eval.metrics.ModelMetrics.included_invocation_count` of zero.
    """


def _assert_no_undisclosed_zero_invocation_render(arm_runs: list[ArmRunResult]) -> None:
    """Refuse to proceed to rendering when any arm's provider was never genuinely invoked.

    Args:
        arm_runs: Every arm run assembled so far this sweep.

    Raises:
        UndisclosedZeroInvocationRenderError: When any arm carries
            ``model_metrics.included_invocation_count == 0`` while also carrying at least one task
            score.
    """
    for arm_run in arm_runs:
        if arm_run.model_metrics.included_invocation_count == 0 and arm_run.task_scores:
            raise UndisclosedZeroInvocationRenderError(
                f"model {arm_run.model_metrics.model_id!r} recorded zero included invocations "
                f"across {len(arm_run.task_scores)} scored task(s); refusing to render an "
                "artifact that would report figures from a provider that was never genuinely "
                "invoked."
            )


def _read_demo_baseline(store: Store) -> TenantSnapshot:
    """Sample the ``demo`` tenant's per-table row counts with count-only ``gsi_tenant`` queries.

    Never raises for an unreadable ``demo`` tenant: the failure is recorded on the snapshot and
    becomes a failing ``demo_tenant_untouched`` check in :func:`_fr93_delta_checks`.
    """
    from eval.tenant_guard import DEMO_TENANT, read_tenant_counts, store_count_reader

    return read_tenant_counts(store_count_reader(store), DEMO_TENANT)


def _fr93_delta_checks(
    store: Store,
    *,
    tenant: str,
    before: TenantSnapshot,
    unit_tenants: Sequence[str] = (),
) -> tuple[CheckResult, ...]:
    """Return this sweep's ``demo_tenant_untouched`` and ``writes_only_under_smoke_tenant`` checks.

    ``writes_only_under_smoke_tenant`` is evaluated for each of ``unit_tenants`` (or ``tenant``
    alone when none is given) and folded into one check by :func:`_fold_writes_checks`. Must be
    called before teardown, which deletes the rows that check reads.
    """
    tenants = tuple(unit_tenants) or (tenant,)
    from eval.tenant_guard import (
        demo_tenant_untouched_check,
        store_count_reader,
        store_row_reader,
        writes_only_under_smoke_tenant_check,
    )

    reader = store_row_reader(store)
    return (
        demo_tenant_untouched_check(
            resource=_FR93_RESOURCE_CLASS, before=before, counter=store_count_reader(store)
        ),
        _fold_writes_checks(
            [
                writes_only_under_smoke_tenant_check(
                    resource=_FR93_RESOURCE_CLASS, tenant=tenant, reader=reader
                )
                for tenant in tenants
            ],
            tenants=tenants,
        ),
    )


def _fold_writes_checks(checks: Sequence[CheckResult], *, tenants: Sequence[str]) -> CheckResult:
    """Fold per-tenant ``writes_only_under_smoke_tenant`` checks into one.

    The folded check passes only when every per-tenant check passes; its detail names each tenant's
    detail. With no tenant at all it fails.
    """
    from eval.verification import CheckResult as _CheckResult

    if not checks:
        return _CheckResult(
            resource=_FR93_RESOURCE_CLASS,
            check="writes_only_under_smoke_tenant",
            status="fail",
            detail="the sweep allocated no unit tenant, so no write location can be proved",
        )
    if len(checks) == 1:
        return checks[0]
    failing = [check for check in checks if check.status != "pass"]
    status = failing[0].status if failing else "pass"
    detail = "; ".join(
        f"{tenant}: {check.status}: {check.detail}"
        for tenant, check in zip(tenants, checks, strict=True)
    )
    return _CheckResult(
        resource=_FR93_RESOURCE_CLASS,
        check="writes_only_under_smoke_tenant",
        status=status,
        detail=detail,
    )


def _write_fr93_inventory(checks: tuple[CheckResult, ...], *, output_dir: Path) -> Path:
    """Write ``checks`` to ``<output_dir>/verification-inventory/eval_sweep.json`` and return it.

    Uses the smoke inventory's ``ResourceClassResult``/``Provenance`` shape.
    """
    from eval.verification import Provenance, ResourceClassResult, resolve_region

    result = ResourceClassResult(
        layer=_FR93_LAYER,
        resource_class=_FR93_RESOURCE_CLASS,
        checks=checks,
        provenance=Provenance.capture(region=resolve_region(), resource_class=_FR93_RESOURCE_CLASS),
    )
    path = output_dir / FR93_INVENTORY_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_json_dict(), indent=2) + "\n", encoding="utf-8")
    return path


class LiveSubstratePairingError(RuntimeError):
    """Raised when a live-model sweep was requested against a non-live CRM substrate.

    ``--live`` selects live *models*; ``--substrate live-aws`` selects live *AWS*.
    """


def _assert_live_mode_is_paired_with_the_live_substrate(
    mode: EvalMode, dispatch_factory: DispatchFactory
) -> None:
    """Refuse a live-model sweep whose tool dispatch is not the deployed one.

    Checked against the resolved ``dispatch_factory``, not a substrate string.

    Args:
        mode: The sweep's provider mode.
        dispatch_factory: How the sweep dispatches every tool call.

    Raises:
        LiveSubstratePairingError: When ``mode == "live"`` and ``dispatch_factory`` is not
            :func:`live_lambda_dispatch_factory`.
    """
    if mode != "live":
        return
    if dispatch_factory is not live_lambda_dispatch_factory:
        raise LiveSubstratePairingError(
            "live model mode requires the live-AWS substrate: pass --substrate live-aws (or set "
            f"{_SUBSTRATE_ENV_VAR}=live-aws) so every tool call is a real invocation of the "
            "deployed action Lambda. Spending model money against an offline substrate would "
            "publish figures that misrepresent what was measured."
        )


def run_sweep(
    *,
    store: Store,
    mode: EvalMode = "stub",
    arm_specs: tuple[ArmSpec, ...] = DEFAULT_ARM_SPECS,
    golden_tasks_path: Path = _DEFAULT_GOLDEN_TASKS_PATH,
    capability_matrix: CapabilityMatrix | None = None,
    dataset_seed: int = DEFAULT_DATASET_SEED,
    runtime_identifier: str = "local-stub",
    output_dir: Path | None = None,
    archive_raw: bool = False,
    run_id: str | None = None,
    dispatch_factory: DispatchFactory = in_process_dispatch_factory,
    repeats: int = 1,
) -> SweepResult:
    """Run one full eval sweep: allocate, seed one unit tenant per (arm, repeat), score, tear down.

    Every (arm, repeat) unit runs the golden tasks, in file order, in its own freshly seeded tenant
    :func:`eval.tenancy.unit_tenant`, so no unit observes another's writes. The unit tenants are
    recorded on the run's ``eval_runs`` row before seeding and all torn down in a ``finally``.

    The single code path for the CLI (:func:`main`) and a UI-triggered handler. Stub mode is the
    default.

    Args:
        store: The persistence layer to seed, score against, and tear down.
        mode: ``"stub"`` (default) or ``"live"``. Live must be paired with
            :func:`live_lambda_dispatch_factory`; an unpaired live sweep is refused before anything
            is seeded.
        arm_specs: The sweep's arms, in run order. Defaults to :data:`DEFAULT_ARM_SPECS`.
        golden_tasks_path: Path to ``golden_tasks.yaml``.
        capability_matrix: A pre-loaded :class:`providers.capabilities.CapabilityMatrix`, or
            ``None`` to load ``config/models.yaml`` and ``config/model_capabilities.yaml``.
        dataset_seed: The fixture generator seed recorded in every arm's provenance.
        runtime_identifier: The runtime identifier recorded in provenance.
        output_dir: Directory to write ``matrix.json``/``matrix.md``/``matrix.csv`` into, or
            ``None`` to compute the artifacts without writing them.
        archive_raw: Whether to also archive each arm's raw JSON beneath ``<output_dir>/raw/``.
            Ignored when ``output_dir`` is ``None``.
        run_id: Override the allocated run id.
        dispatch_factory: How every tool call is dispatched. Defaults to
            :func:`in_process_dispatch_factory` (the ``local`` substrate).
        repeats: How many times each arm runs the golden tasks (>= 1), each in its own unit tenant.

    Returns:
        The full :class:`SweepResult`. Its ``demo``-tenant delta checks are also published to
        ``<output_dir>/verification-inventory/eval_sweep.json`` when ``output_dir`` is supplied; a
        failing check is recorded, not raised.

    Raises:
        LiveSubstratePairingError: When ``mode == "live"`` but ``dispatch_factory`` is not the
            live-AWS one.
        eval.live.LiveInvocationError: When a live turn failed for a reason the exclusion taxonomy
            cannot categorise.
        eval.tenancy.TenantTeardownError: When scoped teardown of any unit tenant fails.
        eval.matrix.StubMatrixOverwriteRefused: When an all-stub sweep's ``output_dir`` is the
            tracked ``results/`` directory; the sweep has run and been torn down, nothing is
            written.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    resolved_run_id = run_id if run_id is not None else allocate_run_id()
    tenant = tenant_for_run(resolved_run_id)
    unit_tenants = unit_tenants_for_run(resolved_run_id, arm_count=len(arm_specs), repeats=repeats)

    _assert_live_mode_is_paired_with_the_live_substrate(mode, dispatch_factory)
    # Built before anything is seeded.
    providers_by_arm = tuple(_build_provider(mode, family=spec.family) for spec in arm_specs)
    matrix = capability_matrix if capability_matrix is not None else load_capability_matrix()
    # Tenant-relative; each unit localizes them to its own tenant in `_run_arm`.
    tasks = _load_golden_tasks(golden_tasks_path)

    price_status = load_price_table_status()
    cost_withheld = is_cost_provisional()
    git_revision = _git_revision()

    started_at = datetime.now(UTC)
    demo_before = _read_demo_baseline(store)
    record_run_started(
        store,
        run_id=resolved_run_id,
        tenant=tenant,
        started_at=started_at,
        unit_tenants=unit_tenants,
    )
    arm_runs: list[ArmRunResult] = []
    teardown_error: TenantTeardownError | None = None
    body_exc: BaseException | None = None
    fr93_checks: tuple[CheckResult, ...] = ()
    try:
        # Inside the try so a failed seed of a later unit still tears the earlier ones down.
        seed_unit_tenants(store, run_id=resolved_run_id, arm_count=len(arm_specs), repeats=repeats)
        # Inside the try so a failing `mkdir` still reaches teardown. Pre-aggregation snapshots go
        # to `raw/pre-aggregation/`, which `assert_artifacts`'s non-recursive `raw/*.json` scan
        # does not read.
        raw_records_dir = (
            output_dir / "raw" / "pre-aggregation"
            if (output_dir is not None and archive_raw)
            else None
        )
        raw_records_writer: RawRecordsWriter | None = None
        if raw_records_dir is not None:
            raw_records_dir.mkdir(parents=True, exist_ok=True)
            raw_records_writer = _raw_records_writer(raw_records_dir)
        for arm_index, (arm_spec, provider) in enumerate(
            zip(arm_specs, providers_by_arm, strict=True)
        ):
            arm_runs.append(
                _run_arm(
                    provider=provider,
                    matrix=matrix,
                    arm_spec=arm_spec,
                    tasks=tasks,
                    store=store,
                    unit_tenants=unit_tenants[arm_index * repeats : (arm_index + 1) * repeats],
                    mode=mode,
                    dataset_seed=dataset_seed,
                    git_revision=git_revision,
                    runtime_identifier=runtime_identifier,
                    price_table_version=price_status.price_table_version,
                    cost_withheld=cost_withheld,
                    dispatch_factory=dispatch_factory,
                    arm_index=arm_index,
                    raw_records_writer=raw_records_writer,
                )
            )
    except BaseException as exc:  # noqa: BLE001 - re-raised verbatim below, never swallowed
        body_exc = exc
        raise
    finally:
        # Before teardown, which deletes the rows `writes_only_under_smoke_tenant` reads.
        fr93_checks = _fr93_delta_checks(
            store, tenant=tenant, before=demo_before, unit_tenants=unit_tenants
        )
        try:
            teardown_unit_tenants(store, tenants=unit_tenants)
        except TenantTeardownError as exc:
            teardown_error = exc
            if body_exc is not None:
                # The body's exception propagates; the teardown failure is logged.
                print(
                    f"tenant teardown also failed while handling {body_exc!r}: {exc}",
                    file=sys.stderr,
                )
        else:
            # A failed teardown leaves the catalog row ``running``, where
            # ``scripts/reap_eval_tenants.py`` finds it.
            record_run_torn_down(
                store,
                run_id=resolved_run_id,
                tenant=tenant,
                started_at=started_at,
                unit_tenants=unit_tenants,
            )

    _assert_no_undisclosed_zero_invocation_render(arm_runs)

    matrix_result = build_matrix_result([arm_run.arm_result for arm_run in arm_runs])

    artifact_paths: dict[str, Path] = {}
    raw_archive_paths: list[Path] = []
    fr93_inventory_path: Path | None = None
    if output_dir is not None:
        artifact_paths = write_matrix_artifacts(matrix_result, output_dir)
        if archive_raw:
            raw_dir = output_dir / "raw"
            for arm_run in arm_runs:
                raw_archive_paths.append(archive_raw_run(arm_run.arm_result, raw_dir))
        fr93_inventory_path = _write_fr93_inventory(fr93_checks, output_dir=output_dir)

    result = SweepResult(
        run_id=resolved_run_id,
        tenant=tenant,
        mode=mode,
        matrix=matrix_result,
        arm_runs=tuple(arm_runs),
        artifact_paths=artifact_paths,
        raw_archive_paths=tuple(raw_archive_paths),
        teardown_error=teardown_error,
        fr93_checks=fr93_checks,
        fr93_inventory_path=fr93_inventory_path,
        unit_tenants=unit_tenants,
    )

    if teardown_error is not None:
        raise teardown_error

    return result


class ArtifactAssertionError(RuntimeError):
    """Raised when ``--assert-artifacts`` finds a sweep's published artifacts absent or malformed.

    Carries every failure found, never only the first, so one run names every defect.
    """

    def __init__(self, failures: list[str]) -> None:
        self.failures = list(failures)
        joined = "\n".join(f"  - {failure}" for failure in self.failures)
        super().__init__(f"artifact assertions failed ({len(self.failures)}):\n{joined}")


#: Verdict words ``matrix.md`` may not carry, matched case-insensitively as whole words.
_FORBIDDEN_VERDICT_WORDS: Final[tuple[str, ...]] = (
    "pass",
    "fail",
    "passed",
    "failed",
    "verdict",
    "winner",
    "wins",
    "won",
    "better",
    "best",
    "worse",
    "recommend",
    "recommended",
)


def assert_artifacts(
    *,
    output_dir: Path,
    expected_arm_count: int,
    expected_task_count: int,
    raw_archive_paths: tuple[Path, ...],
) -> None:
    """Assert every published sweep artifact exists on disk and is well-formed.

    Reads the files back from ``output_dir`` and collects every failure.

    What each artifact is checked for:

    * ``matrix.json`` — parses as JSON, carries an ``arms`` list of exactly ``expected_arm_count``
      entries, and every arm names a model id, records ``mode``, and accounts for exactly
      ``expected_task_count`` tasks across its scored and unscored counts.
    * ``matrix.md`` — non-empty, names every arm's model id, carries no verdict word, and carries
      every figure-semantics note ``matrix.json`` publishes, verbatim.
    * tier comparison — ``matrix.json`` carries a ``tier_comparison`` object with one column per
      arm (model ids in arm order) and one entry per column for each of the three tiers, every one
      of its notes appears verbatim in ``matrix.md``, and ``matrix.md``'s
      ``## Tier comparison summary`` heading precedes ``## Reading these figures``.
    * ``matrix.csv`` — parses as CSV with the ``model_id``/``mode`` columns present and at least one
      data row per arm.
    * completion over repeats — every tier comparison entry's ``completion_over_repeats`` in
      ``matrix.json`` equals its ``Task completion over repeats`` cell in ``matrix.md`` and its
      ``summary_<tier>_completion_over_repeats_*`` cells in ``matrix.csv``.
    * ``raw/`` — exists as a directory holding one parseable JSON file per arm, each naming the same
      model id as its ``matrix.json`` arm.
    * ``verification-inventory/eval_sweep.json`` — present, parseable, and carrying both
      ``demo_tenant_untouched`` and ``writes_only_under_smoke_tenant`` at ``pass``.

    Args:
        output_dir: The directory the sweep wrote its artifacts into.
        expected_arm_count: How many arms the sweep ran.
        expected_task_count: How many golden tasks each arm ran.
        raw_archive_paths: The raw archive paths the sweep reported writing, checked against what
            is actually present beneath ``output_dir / "raw"``.

    Raises:
        ArtifactAssertionError: When any check failed. The exception carries every failure.
    """
    failures: list[str] = []
    model_ids: list[str] = []
    json_payload: dict[str, object] | None = None
    markdown: str | None = None

    json_path = output_dir / "matrix.json"
    if not json_path.is_file():
        failures.append(f"{json_path} was not written")
    else:
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{json_path} is not parseable JSON: {exc}")
        else:
            json_payload = payload if isinstance(payload, dict) else None
            arms = payload.get("arms") if isinstance(payload, dict) else None
            if not isinstance(arms, list):
                failures.append(f"{json_path} carries no 'arms' list")
            elif len(arms) != expected_arm_count:
                failures.append(
                    f"{json_path} carries {len(arms)} arm(s), expected {expected_arm_count}"
                )
            else:
                for index, arm in enumerate(arms):
                    if not isinstance(arm, dict):
                        failures.append(f"{json_path} arm {index} is not an object")
                        continue
                    provenance = arm.get("provenance")
                    provenance = provenance if isinstance(provenance, dict) else {}
                    aggregate = arm.get("aggregate_score")
                    aggregate = aggregate if isinstance(aggregate, dict) else {}
                    model_id = str(provenance.get("model_id") or "")
                    if not model_id:
                        failures.append(f"{json_path} arm {index} names no model_id")
                    else:
                        model_ids.append(model_id)
                    if not str(provenance.get("mode") or ""):
                        failures.append(f"{json_path} arm {index} records no run mode")
                    scored = aggregate.get("scored_task_count")
                    unscored = aggregate.get("unscored_task_count")
                    if not isinstance(scored, int) or not isinstance(unscored, int):
                        failures.append(
                            f"{json_path} arm {index} reports no integer scored/unscored counts"
                        )
                    elif scored + unscored != expected_task_count:
                        failures.append(
                            f"{json_path} arm {index} accounts for {scored + unscored} task(s) "
                            f"({scored} scored + {unscored} unscored), expected "
                            f"{expected_task_count}"
                        )
                    if not arm.get("task_scores"):
                        failures.append(f"{json_path} arm {index} carries no per-task scores")

    md_path = output_dir / "matrix.md"
    if not md_path.is_file():
        failures.append(f"{md_path} was not written")
    else:
        markdown = md_path.read_text(encoding="utf-8")
        if not markdown.strip():
            failures.append(f"{md_path} is empty")
        for model_id in model_ids:
            if model_id not in markdown:
                failures.append(f"{md_path} does not name arm model {model_id!r}")
        for word in _rendered_verdict_words(markdown):
            failures.append(f"{md_path} renders the forbidden verdict word {word!r}")
        _assert_figure_semantics_agree(
            json_path=json_path,
            md_path=md_path,
            payload=json_payload,
            markdown=markdown,
            failures=failures,
        )
    _assert_tier_comparison(
        json_path=json_path,
        md_path=md_path,
        payload=json_payload,
        markdown=markdown,
        arm_model_ids=model_ids if len(model_ids) == expected_arm_count else None,
        expected_arm_count=expected_arm_count,
        failures=failures,
    )

    csv_path = output_dir / "matrix.csv"
    rows: list[dict[str, str]] | None = None
    if not csv_path.is_file():
        failures.append(f"{csv_path} was not written")
    else:
        with csv_path.open(encoding="utf-8", newline="") as csv_file:
            rows = list(csv_module.DictReader(csv_file))
        if not rows:
            failures.append(f"{csv_path} carries a header but no data row")
        else:
            missing_columns = {"model_id", "mode"} - set(rows[0])
            if missing_columns:
                failures.append(f"{csv_path} is missing column(s) {sorted(missing_columns)}")
            csv_models = {str(row.get("model_id") or "") for row in rows}
            for model_id in model_ids:
                if model_id not in csv_models:
                    failures.append(f"{csv_path} carries no row for arm model {model_id!r}")
    _assert_repeat_completion_agrees(
        json_path=json_path,
        md_path=md_path,
        csv_path=csv_path,
        payload=json_payload,
        markdown=markdown,
        csv_rows=rows,
        failures=failures,
    )

    raw_dir = output_dir / "raw"
    if not raw_dir.is_dir():
        failures.append(f"{raw_dir} was not created")
    else:
        present = sorted(path for path in raw_dir.glob("*.json") if path.is_file())
        for reported in raw_archive_paths:
            if not reported.is_file():
                failures.append(f"{reported} was reported archived but is not on disk")
        if len(present) < expected_arm_count:
            failures.append(
                f"{raw_dir} holds {len(present)} raw JSON file(s), expected at least "
                f"{expected_arm_count} (one per arm)"
            )
        archived_models: set[str] = set()
        for path in present:
            try:
                raw_payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                failures.append(f"{path} is not parseable JSON: {exc}")
                continue
            if not isinstance(raw_payload, dict) or not raw_payload.get("task_scores"):
                failures.append(f"{path} carries no per-task scores, so it cannot be re-rendered")
            if isinstance(raw_payload, dict):
                raw_provenance = raw_payload.get("provenance")
                if isinstance(raw_provenance, dict):
                    archived_models.add(str(raw_provenance.get("model_id") or ""))
        for model_id in model_ids:
            if model_id not in archived_models:
                failures.append(f"{raw_dir} holds no raw archive for arm model {model_id!r}")

    _assert_fr93_inventory(output_dir, failures)

    if failures:
        raise ArtifactAssertionError(failures)


#: Minimum length, in characters, at which a string inside ``matrix.json``'s ``figure_semantics``
#: block is treated as published prose rather than an identifier.
_SEMANTICS_PROSE_MIN_LENGTH: Final[int] = 60


def _figure_semantics_prose(
    payload: object, path: str = "figure_semantics"
) -> list[tuple[str, str]]:
    """Return every published prose string inside a ``figure_semantics`` block, with its JSON path."""
    found: list[tuple[str, str]] = []
    if isinstance(payload, str):
        if len(payload) >= _SEMANTICS_PROSE_MIN_LENGTH and " " in payload:
            found.append((path, payload))
    elif isinstance(payload, Mapping):
        for key, value in payload.items():
            found.extend(_figure_semantics_prose(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(_figure_semantics_prose(value, f"{path}[{index}]"))
    return found


def _assert_figure_semantics_agree(
    *,
    json_path: Path,
    md_path: Path,
    payload: dict[str, object] | None,
    markdown: str,
    failures: list[str],
) -> None:
    """Append a failure for every figure-semantics note ``matrix.json`` carries and ``matrix.md`` does not.

    Every prose string in the JSON block must appear verbatim in the Markdown.
    """
    if payload is None:
        return
    semantics = payload.get("figure_semantics")
    if not isinstance(semantics, Mapping):
        failures.append(f"{json_path} carries no 'figure_semantics' object")
        return
    prose = _figure_semantics_prose(semantics)
    if not prose:
        failures.append(f"{json_path}'s 'figure_semantics' block carries no published note")
        return
    for field_path, text in prose:
        if text not in markdown:
            failures.append(
                f"{md_path} does not carry {json_path.name}'s {field_path} text verbatim, so the "
                f"two artifacts publish different figure semantics — re-render both from "
                f"results/raw/ rather than one of them. Absent text: "
                f"{text[:120]!r}"
            )


def _assert_tier_comparison(
    *,
    json_path: Path,
    md_path: Path,
    payload: dict[str, object] | None,
    markdown: str | None,
    arm_model_ids: list[str] | None,
    expected_arm_count: int,
    failures: list[str],
) -> None:
    """Append one failure for every way the published tier comparison summary falls short.

    JSON checks are skipped when ``payload`` is ``None`` and Markdown checks when ``markdown`` is
    ``None``; the model-id order check runs only when ``arm_model_ids`` is given.
    """
    summary_heading = "## Tier comparison summary"
    reading_heading = "## Reading these figures"
    if markdown is not None:
        summary_at = markdown.find(summary_heading)
        reading_at = markdown.find(reading_heading)
        if summary_at < 0:
            failures.append(f"{md_path} carries no {summary_heading!r} section")
        elif reading_at >= 0 and summary_at > reading_at:
            failures.append(f"{md_path} renders {summary_heading!r} after {reading_heading!r}")
    if payload is None:
        return
    summary = payload.get("tier_comparison")
    if not isinstance(summary, Mapping):
        failures.append(f"{json_path} carries no 'tier_comparison' object")
        return
    columns = summary.get("columns")
    if not isinstance(columns, list):
        failures.append(f"{json_path}'s tier_comparison carries no 'columns' list")
    elif len(columns) != expected_arm_count:
        failures.append(
            f"{json_path}'s tier_comparison carries {len(columns)} column(s), expected "
            f"{expected_arm_count} (one per arm)"
        )
    elif arm_model_ids is not None:
        column_model_ids = [
            column.get("model_id") if isinstance(column, Mapping) else None for column in columns
        ]
        if column_model_ids != arm_model_ids:
            failures.append(
                f"{json_path}'s tier_comparison columns name models {column_model_ids}, expected "
                f"the arms' {arm_model_ids} in arm order"
            )
    tiers = summary.get("tiers")
    expected_tiers = ("supervisor", "subagent", "tool")
    if not isinstance(tiers, Mapping) or sorted(tiers) != sorted(expected_tiers):
        found = sorted(tiers) if isinstance(tiers, Mapping) else None
        failures.append(
            f"{json_path}'s tier_comparison tiers are {found}, expected {list(expected_tiers)}"
        )
    else:
        for tier in expected_tiers:
            entries = tiers[tier]
            if not isinstance(entries, list) or len(entries) != expected_arm_count:
                count = len(entries) if isinstance(entries, list) else None
                failures.append(
                    f"{json_path}'s tier_comparison {tier!r} carries {count} entr(ies), expected "
                    f"{expected_arm_count} (one per column)"
                )
    notes = summary.get("notes")
    if not isinstance(notes, list) or not notes:
        failures.append(f"{json_path}'s tier_comparison carries no notes")
    elif markdown is not None:
        for index, note in enumerate(notes):
            if not isinstance(note, str) or note not in markdown:
                failures.append(
                    f"{md_path} does not carry {json_path.name}'s tier_comparison note {index} "
                    f"verbatim: {str(note)[:120]!r}"
                )


#: A rendered ``Task completion over repeats`` cell: ``rate (completed/applicable, majority of N)``.
_REPEAT_COMPLETION_CELL: Final[re.Pattern[str]] = re.compile(
    r"(?P<rate>\S+) \((?P<completed>\d+)/(?P<applicable>\d+), majority of (?P<repeats>\d+)\)"
)
_REPEAT_COMPLETION_ROW_LABEL: Final[str] = "Task completion over repeats"
_TIER_SECTION_HEADINGS: Final[tuple[tuple[str, str], ...]] = (
    ("supervisor", "### Supervisor"),
    ("subagent", "### Subagent"),
    ("tool", "### ToolCall"),
)
_REPEAT_COMPLETION_FIELDS: Final[tuple[str, ...]] = ("completed", "applicable", "repeats", "rate")


def _repeat_completion_values(value: object) -> tuple[object, ...] | None:
    """Normalise a ``completion_over_repeats`` block to ``(completed, applicable, repeats, rate)``.

    ``None`` stands for an absent block. ``rate`` is rounded to 6 places so that renderings of
    one float compare equal.
    """
    if not isinstance(value, Mapping) or value.get("applicable") is None:
        return None
    rate = value.get("rate")
    return (
        _as_int(value.get("completed")),
        _as_int(value.get("applicable")),
        _as_int(value.get("repeats")),
        None if rate in (None, "") else round(float(str(rate)), 6),
    )


def _as_int(value: object) -> int | None:
    """Return ``value`` as an ``int`` when it is an int or an integral numeric string, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _md_repeat_completion_cells(markdown: str, tier_heading: str) -> list[str] | None:
    """Return the ``Task completion over repeats`` cells under ``tier_heading``, or ``None``."""
    start = markdown.find(f"\n{tier_heading}\n")
    if start < 0:
        return None
    for line in markdown[start:].splitlines()[2:]:
        if line.startswith("### ") or line.startswith("## "):
            return None
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == _REPEAT_COMPLETION_ROW_LABEL:
            return cells[1:]
    return None


def _md_cell_values(cell: str) -> tuple[object, ...] | None | str:
    """Parse a rendered repeat-completion cell; return the raw text when it does not parse."""
    if cell == "n/a":
        return None
    if cell == "not applicable":
        return ("not applicable",)
    match = _REPEAT_COMPLETION_CELL.fullmatch(cell)
    if match is None:
        return cell
    rate = match.group("rate")
    return (
        int(match.group("completed")),
        int(match.group("applicable")),
        int(match.group("repeats")),
        None if rate in ("—", "n/a") else round(float(rate), 6),
    )


def _assert_repeat_completion_agrees(
    *,
    json_path: Path,
    md_path: Path,
    csv_path: Path,
    payload: dict[str, object] | None,
    markdown: str | None,
    csv_rows: list[dict[str, str]] | None,
    failures: list[str],
) -> None:
    """Append a failure wherever ``completion_over_repeats`` differs between JSON, MD and CSV.

    ``matrix.json``'s tier comparison is the reference. Each entry is compared with its column's
    cell in the tier's ``Task completion over repeats`` Markdown row, and with the
    ``summary_<tier>_completion_over_repeats_*`` cells of the CSV row whose ``summary_column`` is
    that column's label. Checks for an artifact that is absent are skipped.
    """
    summary = payload.get("tier_comparison") if payload is not None else None
    if not isinstance(summary, Mapping):
        return
    columns = summary.get("columns")
    tiers = summary.get("tiers")
    if not isinstance(columns, list) or not isinstance(tiers, Mapping):
        return
    labels = [str(c.get("label")) if isinstance(c, Mapping) else "" for c in columns]
    csv_by_label: dict[str, dict[str, str]] = {}
    for row in csv_rows or []:
        csv_by_label.setdefault(str(row.get("summary_column") or ""), row)

    for tier, heading in _TIER_SECTION_HEADINGS:
        entries = tiers.get(tier)
        if not isinstance(entries, list) or len(entries) != len(labels):
            continue
        expected = [
            _repeat_completion_values(
                e.get(REPEAT_COMPLETION_KEY) if isinstance(e, Mapping) else None
            )
            for e in entries
        ]
        if markdown is not None:
            cells = _md_repeat_completion_cells(markdown, heading)
            if cells is None or len(cells) != len(labels):
                failures.append(
                    f"{md_path} carries no {_REPEAT_COMPLETION_ROW_LABEL!r} row with one cell per "
                    f"arm under {heading!r}"
                )
            else:
                for label, cell, want in zip(labels, cells, expected, strict=True):
                    got = _md_cell_values(cell)
                    want_md = ("not applicable",) if want is not None and want[1] == 0 else want
                    if got != want_md:
                        failures.append(
                            f"{md_path} renders {tier} completion over repeats for {label!r} as "
                            f"{cell!r}, but {json_path.name} records {want}"
                        )
        if csv_rows is not None:
            for label, want in zip(labels, expected, strict=True):
                csv_row = csv_by_label.get(label)
                if csv_row is None:
                    failures.append(f"{csv_path} carries no summary row for column {label!r}")
                    continue
                cells_by_field = {
                    name: csv_row.get(f"summary_{tier}_{REPEAT_COMPLETION_KEY}_{name}", "")
                    for name in _REPEAT_COMPLETION_FIELDS
                }
                got_csv = _repeat_completion_values(
                    {k: (v if v != "" else None) for k, v in cells_by_field.items()}
                )
                if got_csv != want:
                    failures.append(
                        f"{csv_path} records {tier} completion over repeats for {label!r} as "
                        f"{got_csv}, but {json_path.name} records {want}"
                    )


def _assert_fr93_inventory(output_dir: Path, failures: list[str]) -> None:
    """Append a failure for every way the sweep's published ``demo``-tenant delta falls short.

    A missing file, a required check absent from it, or a check not at ``pass`` each fail.
    """
    path = output_dir / FR93_INVENTORY_RELATIVE_PATH
    if not path.is_file():
        failures.append(f"{path} was not written, so this sweep published no demo-untouched delta")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        failures.append(f"{path} is not parseable JSON: {exc}")
        return
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list):
        failures.append(f"{path} carries no 'checks' list")
        return
    by_name = {str(entry.get("check") or ""): entry for entry in checks if isinstance(entry, dict)}
    for name in _FR93_REQUIRED_CHECKS:
        entry = by_name.get(name)
        if entry is None:
            failures.append(f"{path} carries no {name!r} check")
            continue
        status = str(entry.get("status") or "")
        if status != "pass":
            failures.append(
                f"{path}'s {name!r} check is {status!r}, not 'pass': "
                f"{entry.get('detail') or 'no detail recorded'}"
            )


def _rendered_verdict_words(text: str) -> list[str]:
    """Return every :data:`_FORBIDDEN_VERDICT_WORDS` entry ``text`` uses as a whole word."""
    words = {token for token in _WORD_PATTERN.findall(text.lower()) if token}
    return sorted(words & set(_FORBIDDEN_VERDICT_WORDS))


def _resolve_substrate(flag: str | None, env: Mapping[str, str] | None = None) -> Substrate:
    """Resolve the substrate as flag, then environment, then the ``local`` default.

    Args:
        flag: The ``--substrate`` value, or ``None`` when the flag was not passed.
        env: The environment to read :data:`_SUBSTRATE_ENV_VAR` from. Defaults to
            :data:`os.environ`.

    Returns:
        The resolved substrate.

    Raises:
        ValueError: When either source names a substrate this module does not implement.
    """
    environment = env if env is not None else os.environ
    raw = flag if flag is not None else environment.get(_SUBSTRATE_ENV_VAR, "").strip()
    resolved = raw or _SUBSTRATE_LOCAL
    if resolved not in (_SUBSTRATE_LOCAL, _SUBSTRATE_LIVE_AWS):
        raise ValueError(
            f"unrecognised substrate {resolved!r}: expected "
            f"{_SUBSTRATE_LOCAL!r} or {_SUBSTRATE_LIVE_AWS!r}"
        )
    return "live-aws" if resolved == _SUBSTRATE_LIVE_AWS else "local"


def _resolve_table_prefix(flag: str, env: Mapping[str, str] | None = None) -> str:
    """Resolve the DynamoDB table prefix as flag, then ``CRM_TABLE_PREFIX``, then empty."""
    if flag:
        return flag
    environment = env if env is not None else os.environ
    return environment.get(_TABLE_PREFIX_ENV_VAR, "").strip()


def _build_default_store(
    *, table_prefix: str, endpoint_url: str, substrate: Substrate = "local"
) -> Store:
    """Build the CLI's :class:`Store` for ``substrate``.

    Under ``local`` the client uses ``endpoint_url``, a fixed region and the placeholder key pair,
    so no ambient credential is consulted; when ``endpoint_url`` is the DynamoDB Local default,
    every table is also provisioned, tolerating existing ones. Under ``live-aws`` the client uses
    the resolved region and the caller's ambient credentials, and nothing is provisioned.

    Args:
        table_prefix: Prefix applied to physical DynamoDB table names.
        endpoint_url: The DynamoDB endpoint to target under the ``local`` substrate. An empty
            string means no endpoint override, still with the placeholder credential. Ignored
            under ``live-aws``.
        substrate: Which substrate to build for. Defaults to ``local``.

    Returns:
        A :class:`Store` wrapping the explicitly-constructed client.
    """
    if substrate == "live-aws":
        aws = resolve_aws_config()
        return Store(
            table_prefix=table_prefix,
            client=boto3.client("dynamodb", region_name=aws.region),
        )

    client = boto3.client(
        "dynamodb",
        endpoint_url=endpoint_url or None,
        region_name=_LOCAL_PLACEHOLDER_REGION,
        aws_access_key_id=_LOCAL_PLACEHOLDER_ACCESS_KEY_ID,
        aws_secret_access_key=_LOCAL_PLACEHOLDER_SECRET_ACCESS_KEY,
    )
    if endpoint_url == _DEFAULT_LOCAL_ENDPOINT_URL:
        with contextlib.suppress(client.exceptions.ResourceInUseException):
            create_all_tables(client, table_prefix=table_prefix)
    return Store(table_prefix=table_prefix, client=client)


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser. Stub is the default; ``--live`` opts in explicitly."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the model-comparison eval sweep. Stub mode (the default) makes no model call, "
            "costs nothing, and requires no AWS credentials: it targets a local DynamoDB engine "
            "(see --endpoint-url) with a fixed placeholder key pair, never your ambient AWS "
            "profile. Live mode requires --live."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help=(
            "Run against live models instead of the zero-cost stub. Spends real money and "
            "requires AWS credentials with Bedrock access — models are reached through Bedrock "
            "with IAM, never a vendor API key. Must be paired with the live-AWS substrate "
            f"(--substrate {_SUBSTRATE_LIVE_AWS}, or {_SUBSTRATE_ENV_VAR}={_SUBSTRATE_LIVE_AWS}); "
            "an unpaired --live is refused rather than run."
        ),
    )
    parser.add_argument(
        "--substrate",
        choices=(_SUBSTRATE_LOCAL, _SUBSTRATE_LIVE_AWS),
        default=None,
        help=(
            "Which CRM substrate to run against, independently of model spend. "
            f"{_SUBSTRATE_LOCAL!r} (the default) uses --endpoint-url with a fixed placeholder "
            "credential and dispatches every tool call in-process to the imported handler: no AWS "
            f"credential, no AWS endpoint, no cost. {_SUBSTRATE_LIVE_AWS!r} uses your own ambient "
            "AWS credentials for DynamoDB and dispatches every tool call as a real "
            "lambda:InvokeFunction of the deployed action Lambda, so it mutates a real account and "
            "incurs AWS (not model) charges. Resolved as this flag, then the "
            f"{_SUBSTRATE_ENV_VAR} environment variable, then {_SUBSTRATE_LOCAL!r}."
        ),
    )
    parser.add_argument(
        "--endpoint-url",
        default=_DEFAULT_LOCAL_ENDPOINT_URL,
        help=(
            "DynamoDB endpoint to target under the 'local' substrate. Defaults to a local "
            f"DynamoDB Local loopback address ({_DEFAULT_LOCAL_ENDPOINT_URL}) reached with a "
            "fixed placeholder credential, never your ambient AWS profile. Ignored under "
            f"--substrate {_SUBSTRATE_LIVE_AWS}, which is the supported way to target a real "
            "account: pointing this flag at a real AWS endpoint would still send the placeholder "
            "credential and be refused."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Directory to write matrix.json/matrix.md/matrix.csv into. Defaults to results/stub/ "
            "(gitignored) for a stub sweep and results/ for a live one. An all-stub matrix is "
            "refused in results/, which holds the tracked live sample."
        ),
    )
    parser.add_argument(
        "--archive-raw",
        action="store_true",
        default=False,
        help="Also archive each arm's raw JSON beneath <output-dir>/raw/.",
    )
    parser.add_argument(
        "--assert-artifacts",
        action="store_true",
        default=False,
        help=(
            "After the sweep, read every published artifact back from --output-dir and assert it "
            "was produced and is well-formed: matrix.json, matrix.md, matrix.csv, raw/, and "
            "verification-inventory/eval_sweep.json (the sweep's own demo-untouched delta, "
            "which must carry both its checks at pass). Implies --archive-raw, since raw/ is one "
            "of them. Exits non-zero listing every failure if any artifact is absent, "
            "unparseable, inconsistent with the arms actually run, or renders a verdict."
        ),
    )
    parser.add_argument(
        "--table-prefix",
        default="",
        help=(
            "Prefix applied to physical DynamoDB table names. Resolved as this flag, then the "
            f"{_TABLE_PREFIX_ENV_VAR} environment variable (the same one the deployed Lambdas and "
            "Runtime read), then no prefix."
        ),
    )
    parser.add_argument(
        "--dataset-seed",
        type=int,
        default=DEFAULT_DATASET_SEED,
        help="Fixture generator seed to record in provenance.",
    )
    parser.add_argument(
        "--runtime-identifier",
        default="local-stub",
        help="Runtime identifier to record in provenance (never a real account ARN for stub).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help=(
            "How many times each golden task is run per arm (>= 1). Repeats feed the per-task "
            "correctness spread; the default of 1 runs each task once, unchanged."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m eval.harness``. Stub by default; ``--live`` opts in.

    ``--live`` selects live models and ``--substrate`` selects live AWS; ``--live`` requires
    ``--substrate live-aws``.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults to :data:`sys.argv`.

    Returns:
        Process exit code: ``0`` on success, ``1`` if teardown failed after a completed sweep or if
        ``--assert-artifacts`` found an artifact absent or malformed, or if a stub sweep was
        pointed at the tracked ``results/`` directory.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    mode: EvalMode = "live" if args.live else "stub"
    if args.output_dir is None:
        args.output_dir = _DEFAULT_RESULTS_DIR if mode == "live" else _DEFAULT_STUB_RESULTS_DIR

    try:
        substrate = _resolve_substrate(args.substrate)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    table_prefix = _resolve_table_prefix(args.table_prefix)
    live_aws = substrate == "live-aws"

    if mode == "live" and not live_aws:
        print(
            "--live selects live models and requires the live-AWS substrate: pass "
            f"--substrate {_SUBSTRATE_LIVE_AWS} or set {_SUBSTRATE_ENV_VAR}={_SUBSTRATE_LIVE_AWS}. "
            "Refusing to spend model money against an offline substrate, whose figures would "
            "misrepresent what was measured.",
            file=sys.stderr,
        )
        return 2

    store = _build_default_store(
        table_prefix=table_prefix, endpoint_url=args.endpoint_url, substrate=substrate
    )
    dispatch_factory: DispatchFactory = (
        live_lambda_dispatch_factory if live_aws else in_process_dispatch_factory
    )
    runtime_identifier = _LIVE_AWS_RUNTIME_IDENTIFIER if live_aws else str(args.runtime_identifier)

    try:
        result = run_sweep(
            store=store,
            mode=mode,
            dataset_seed=args.dataset_seed,
            runtime_identifier=runtime_identifier,
            output_dir=args.output_dir,
            archive_raw=args.archive_raw or args.assert_artifacts,
            dispatch_factory=dispatch_factory,
            repeats=args.repeats,
        )
    except TenantTeardownError as exc:
        print(f"eval sweep completed but tenant teardown failed: {exc}", file=sys.stderr)
        return 1
    except StubMatrixOverwriteRefused as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    print(
        f"run_id={result.run_id} tenant={result.tenant} mode={result.mode} "
        f"substrate={substrate} table_prefix={table_prefix!r}"
    )
    for name, path in sorted(result.artifact_paths.items()):
        print(f"wrote {name}: {path}")
    for raw_path in result.raw_archive_paths:
        print(f"wrote raw: {raw_path}")
    for check in result.fr93_checks:
        print(f"demo-untouched {check.check} = {check.status} ({check.detail})")
    if result.fr93_inventory_path is not None:
        print(f"wrote fr93 inventory: {result.fr93_inventory_path}")

    failed_fr93 = [check for check in result.fr93_checks if check.status != "pass"]
    if live_aws and failed_fr93:
        for check in failed_fr93:
            print(
                f"demo-untouched delta not proved for this live sweep: {check.check} = "
                f"{check.status}: {check.detail}",
                file=sys.stderr,
            )
        return 1

    if args.assert_artifacts:
        expected_task_count = len(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)) * args.repeats
        try:
            assert_artifacts(
                output_dir=args.output_dir,
                expected_arm_count=len(result.arm_runs),
                expected_task_count=expected_task_count,
                raw_archive_paths=result.raw_archive_paths,
            )
        except ArtifactAssertionError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        print(
            f"asserted 5 artifacts for {len(result.arm_runs)} arm(s) x "
            f"{expected_task_count} task(s): matrix.json, matrix.md, matrix.csv, raw/, "
            f"{FR93_INVENTORY_RELATIVE_PATH}"
        )
    return 0


def run_sweep_for_ui(
    *,
    store: Store,
    live: bool = False,
    output_dir: Path | None = None,
    archive_raw: bool = False,
) -> SweepResult:
    """UI-triggered entry point over :func:`run_sweep`, the same code path as :func:`main`.

    Args:
        store: The persistence layer to seed, score against, and tear down.
        live: Whether to request live mode. Defaults to ``False`` (stub). ``True`` raises
            :class:`LiveSubstratePairingError`, because this entry point dispatches in-process.
        output_dir: Directory to write artifacts into, or ``None`` to skip writing.
        archive_raw: Whether to also archive raw per-arm JSON.

    Returns:
        The full :class:`SweepResult`, identical in shape to what :func:`main` produces for the
        same arguments.
    """
    mode: EvalMode = "live" if live else "stub"
    return run_sweep(store=store, mode=mode, output_dir=output_dir, archive_raw=archive_raw)


__all__ = [
    "DEFAULT_ARM_SPECS",
    "DEFAULT_DATASET_SEED",
    "FR93_INVENTORY_RELATIVE_PATH",
    "ArmRunResult",
    "ArmSpec",
    "ArtifactAssertionError",
    "DispatchFactory",
    "EvalMode",
    "LiveSubstratePairingError",
    "Substrate",
    "SweepResult",
    "TurnDispatch",
    "UndisclosedZeroInvocationRenderError",
    "assert_artifacts",
    "build_arg_parser",
    "in_process_dispatch_factory",
    "live_lambda_dispatch_factory",
    "main",
    "run_sweep",
    "run_sweep_for_ui",
]


if __name__ == "__main__":
    raise SystemExit(main())
