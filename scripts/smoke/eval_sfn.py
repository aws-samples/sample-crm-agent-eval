# SPDX-License-Identifier: MIT
"""Layer 5 T3 smoke: the `eval_sweep` state machine, driven through the run API.

Default (stub, no model call). Provisions one disposable admin user with `full_visibility` and
calls `POST /api/eval_trigger` with its ID token.

- `estimate` returns a basis and a number.
- **Stub rehearsal** at `REHEARSAL_REPEATS` repeats, checked as it runs and once it is terminal:
  - a second `submit` while it runs is refused 409 (single-flight);
  - `unit_tenants_recorded`: the run's `eval_runs` catalog row records every (arm, repeat) unit
    tenant, arm-major;
  - `unit_tenants_seeded`: once a task is done and the run is still running, every unit tenant
    holds rows;
  - the record reaches `completed` with `tasks_done == tasks_total` = arms x repeats x tasks,
    `repeats` recorded, both arms in the matrix and `tier_summary` for every tier;
  - each arm's `provenance.effort` is `DEFAULT_ARM_EFFORT`, one `arm_effort_is_default:<family>`
    row per default-sweep family (arms in sweep order);
  - each arm's models are `config/models.yaml`'s, one `arm_models_are_configured:<family>` row per
    default-sweep family: `provenance.model_id` is the family's supervisor model and every tier's
    `tier_task_metrics.by_tier.<tier>.configured_model_id` is that tier's model; an absent
    provenance or tier entry fails;
  - `prompt_bundle_matches_repo`: every arm's `provenance.prompt_bundle_sha256` equals
    `agents.prompt_bundle.prompt_bundle_sha256()` of this checkout; an absent digest fails;
  - the execution is `SUCCEEDED`;
  - every `eval_results` row is `done` with `stream_calls > 0`, so `sweep_task` reached the Runtime,
    with one distinct `result_id` per item and arms x tasks rows at each `repeat_index`;
  - the S3 artifacts pass `eval.harness.assert_artifacts`;
  - `artefact_url` presigns a recorded artefact: an https GET returns 200; the same URL over plain
    http returns 403 `AccessDenied`, and with a tampered key 403 `SignatureDoesNotMatch`;
  - the sweep tenant is empty, and the `demo` row counts are unchanged;
  - `tenant_emptied_by_the_workers_own_role`: the run's `eval_runs` catalog row is `torn_down`, so
    the `Teardown` step ran under `sweep_control`'s `{Query, BatchWriteItem}` role, and the run
    tenant and every unit tenant are empty when read with the runner's own credentials. Rows left
    while the catalog row is not `torn_down` fail without being attributed to the role's grants;
  - `canary` is empty and the lock row is gone;
  - `list` shows the run with its starter.
- **Cancel proof**, on a second stub sweep at one repeat:
  - once `tasks_done >= 2`, `cancel` returns `accepted`;
  - the run ends `cancelled` with fewer than all tasks done, a partial matrix, `teardown_status`
    `ok`, an empty tenant and no lock;
  - a second `cancel` returns `already_terminal`;
  - the execution history has no `ExecutionAborted`.
- **Role simulations** for the IAM rows of `sweep_control`, `sweep_task`, the state machine
  role and `eval_trigger`. Targets come from `eval_state_machine_arn` and the definition it serves.
  They run first, with no tenant write, and include the state machine's `tracingConfiguration` and
  its role's X-Ray grant: the four tracing actions allowed on `*`, and
  `xray:BatchGetTraces` and `xray:PutEncryptionConfig` denied.

`--live-once --i-understand-this-is-billable` runs these steps instead:
- read-only pre-checks: the data-retention mode, lock absent, estimate recorded;
- the operator's `--retention-verdict=<mode>` must equal the mode read; absent or different, the run
  stops after the pre-checks, submits nothing and exits non-zero. Run once without it to read the
  mode, judge it, then re-run with it;
- exactly one `submit {mode: live, confirmed: true}`, never retried;
- poll to terminal, then assert `completed` 40/40, both arms, `tier_summary` per tier, cost spent,
  tenant torn down, `canary` empty, and `assert_artifacts` on the downloaded S3 artifacts.
A `failed` run is reported with its failure summary.

Cleanup runs in a `finally`, each step's error reported as a failing row:
- any run still non-terminal is cancelled cooperatively and reported as a failure;
- a sweep tenant or unit tenant left with rows is reaped and reported;
- the admin user is removed, even when a step above failed.
"""

from __future__ import annotations

import dataclasses
import functools
import http.client
import json
import re
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from scripts import proc_run
from scripts.smoke.admin_authz import (
    EVAL_TRIGGER_ROUTE,
    ApiPost,
    Provisioner,
    RoleSmokeUserProvisioner,
    SimulationRow,
    decisive_missing_keys,
    error_fields,
    live_api_post,
    live_store,
    role_arn_of_function,
    simulate_evaluation,
    simulation_result,
    summarise,
)
from scripts.smoke.harness import CheckResult, ExpectedCheckCount, SmokeContext, run_main
from scripts.smoke.iam_roles import (
    INDEX_IN_SCOPE_LEADING_KEY,
    SWEEP_GRANT_LOCALS,
    TENANT_INDEX,
    LocalGrant,
    asserted_dynamodb_actions,
    condition_matches,
    in_scope_key,
    out_of_scope_keys,
    terraform_local_grants,
)

from agents.prompt_bundle import prompt_bundle_sha256
from crm.authz import ADMIN_GROUP, DEMO_TENANT
from crm.store.keys import SWEEP_LOCK_ROW_ID
from crm.store.schema import TABLES, TENANT_SCOPED_TABLES
from eval.harness import ArtifactAssertionError, assert_artifacts
from eval.matrix import TIER_TASK_METRICS_KEY
from eval.runs import EVAL_RESULTS_TABLE, EVAL_RUNS_TABLE, MAX_SWEEP_REPEATS, execution_arn_for
from eval.sweep_units import DEFAULT_ARM_EFFORT
from eval.tenancy import (
    CANARY_TENANT,
    RUN_STATUS_TORN_DOWN,
    find_canary_records,
    teardown_tenant,
    unit_tenants_for_run,
)
from eval.trigger import sweep_shape
from providers.capabilities import Family, load_capability_matrix
from providers.tier_tuning import TIERS

LAYER: Final[int] = 5
RESOURCE_CLASS: Final[str] = "eval_sfn"

LIVE_ONCE_FLAG: Final[str] = "--live-once"
BILLABLE_FLAG: Final[str] = "--i-understand-this-is-billable"

STUB_POLL_SECONDS: Final[float] = 2400.0
LIVE_POLL_SECONDS: Final[float] = 7800.0
POLL_INTERVAL_SECONDS: Final[float] = 10.0
CANCEL_AFTER_TASKS: Final[int] = 2
#: Repeats the stub rehearsal submits; more than one, so units of the same arm are distinct.
REHEARSAL_REPEATS: Final[int] = 2
POLL_MAX_CONSECUTIVE_ERRORS: Final[int] = 5
RETENTION_VERDICT_FLAG: Final[str] = "--retention-verdict"

#: The state machine role's `SfnXRayTracing` actions (SFN-XRAY-1), each allowed on `*`.
SFN_XRAY_TRACING_ACTIONS: Final[tuple[str, ...]] = (
    "xray:GetSamplingRules",
    "xray:GetSamplingTargets",
    "xray:PutTelemetryRecords",
    "xray:PutTraceSegments",
)
#: X-Ray actions outside SFN-XRAY-1, each denied to the state machine role.
SFN_XRAY_DENIED_ACTIONS: Final[tuple[str, ...]] = (
    "xray:BatchGetTraces",
    "xray:PutEncryptionConfig",
)

#: `(argv) -> (returncode, stdout, stderr)` for the read-only AWS CLI pre-check.
CliRunner = Callable[[Sequence[str]], "tuple[int, str, str]"]


class RunApi:
    """The admin's `/api/eval_trigger` actions over one :data:`ApiPost`."""

    def __init__(self, post: ApiPost, token: str) -> None:
        self._post = post
        self._token = token

    def call(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST `body` with the admin token."""
        return self._post(EVAL_TRIGGER_ROUTE, self._token, body)

    def submit(
        self, mode: str, *, confirmed: bool = False, repeats: int | None = None
    ) -> tuple[int, dict[str, Any]]:
        body: dict[str, Any] = {"action": "submit", "mode": mode}
        if confirmed:
            body["confirmed"] = True
        if repeats is not None:
            body["repeats"] = repeats
        return self.call(body)

    def status(self, run_id: str) -> tuple[int, dict[str, Any]]:
        return self.call({"action": "status", "run_id": run_id})

    def cancel(self, run_id: str) -> tuple[int, dict[str, Any]]:
        return self.call({"action": "cancel", "run_id": run_id})

    def artefact_url(self, run_id: str, key: str) -> tuple[int, dict[str, Any]]:
        return self.call({"action": "artefact_url", "run_id": run_id, "key": key})


def _row(check: str, ok: bool, detail: str, *, resource: str = RESOURCE_CLASS) -> CheckResult:
    return CheckResult(
        resource=resource, check=check, status="pass" if ok else "fail", detail=detail
    )


class RunStatusError(RuntimeError):
    """`status` refused the admin (401/403) or kept answering non-200."""


def poll_run(
    api: RunApi,
    run_id: str,
    *,
    until: Callable[[Mapping[str, Any]], bool],
    seconds: float,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    """Poll `status` until `until(run)` holds or `seconds` pass; return the last run payload seen.

    A transport error (`OSError`, which covers `URLError` and timeouts, or `HTTPException`) or a
    non-200 answer is retried; :data:`POLL_MAX_CONSECUTIVE_ERRORS` in a row are raised. A 401 or
    403 raises :class:`RunStatusError` at once.
    """
    deadline = clock() + seconds
    last: dict[str, Any] = {}
    errors = 0
    while True:
        try:
            status, payload = api.status(run_id)
        except (OSError, http.client.HTTPException):
            errors += 1
            if errors >= POLL_MAX_CONSECUTIVE_ERRORS:
                raise
        else:
            if status in (401, 403):
                raise RunStatusError(
                    f"{run_id}: status refused with HTTP {status} {error_fields(payload)}"
                )
            if status == 200:
                errors = 0
                last = payload
                if until(payload):
                    return last
            else:
                errors += 1
                if errors >= POLL_MAX_CONSECUTIVE_ERRORS:
                    raise RunStatusError(
                        f"{run_id}: status answered HTTP {status} {errors} times in a row "
                        f"{error_fields(payload)}"
                    )
        if clock() >= deadline:
            return last
        sleeper(POLL_INTERVAL_SECONDS)


def is_terminal(run: Mapping[str, Any]) -> bool:
    return run.get("is_terminal") is True


# -- individual checks ---------------------------------------------------------------------------


def check_estimate(api: RunApi) -> tuple[CheckResult, dict[str, Any]]:
    """`estimate` returns a basis and a non-negative number."""
    status, payload = api.call({"action": "estimate"})
    amount = payload.get("estimate_usd")
    ok = (
        status == 200
        and payload.get("basis") in ("baseline", "static")
        and isinstance(amount, int | float)
        and not isinstance(amount, bool)
        and amount >= 0
    )
    return (
        _row(
            "estimate_returns_basis_and_number",
            ok,
            f"estimate -> HTTP {status} basis={payload.get('basis')!r} estimate_usd={amount!r} "
            f"per_arm_usd={payload.get('per_arm_usd')!r}",
        ),
        payload,
    )


def check_submit(
    status: int, payload: Mapping[str, Any], *, mode: str, check: str
) -> tuple[CheckResult, str]:
    """`submit` answered 200 with a queued record of the requested mode; returns the run id."""
    run_id = str(payload.get("run_id") or "")
    ok = status == 200 and bool(run_id) and payload.get("mode") == mode
    return (
        _row(check, ok, f"submit {mode} -> HTTP {status} {summarise(payload, 200)}"),
        run_id if status == 200 else "",
    )


def check_single_flight(api: RunApi) -> tuple[CheckResult, str]:
    """A second stub `submit` while a sweep holds the lock is refused 409 `conflict`.

    Returns the run id of a sweep the refusal failed to prevent, or `""`.
    """
    status, payload = api.submit("stub")
    error = error_fields(payload)
    ok = status == 409 and error.get("code") == "conflict"
    stray = str(payload.get("run_id") or "") if status == 200 else ""
    return (
        _row(
            "second_submit_refused_409",
            ok,
            f"second submit while running -> HTTP {status} {summarise(payload, 200)}",
        ),
        stray,
    )


def check_completed_record(
    run: Mapping[str, Any], *, expected_total: int, arm_count: int
) -> CheckResult:
    """A rehearsal record is `completed`, every task done, teardown ok, matrix complete."""
    matrix = run.get("matrix")
    arms = matrix.get("arms") if isinstance(matrix, Mapping) else None
    arms_ok = isinstance(arms, list) and len(arms) == arm_count
    tiers_ok = arms_ok and all(
        isinstance(arm, Mapping)
        and isinstance(arm.get("tier_summary"), Mapping)
        and set(arm["tier_summary"]) == set(TIERS)
        for arm in arms or []
    )
    ok = (
        run.get("status") == "completed"
        and run.get("tasks_total") == expected_total
        and run.get("tasks_done") == expected_total
        and run.get("tasks_failed") == 0
        and run.get("teardown_status") == "ok"
        and arms_ok
        and tiers_ok
    )
    return _row(
        "run_completed_all_tasks",
        ok,
        f"status={run.get('status')!r} done={run.get('tasks_done')}/{run.get('tasks_total')} "
        f"(expected {expected_total}) failed={run.get('tasks_failed')} "
        f"teardown={run.get('teardown_status')!r} arms={len(arms) if isinstance(arms, list) else None}"
        f"/{arm_count} tier_summary_per_tier={tiers_ok} error={run.get('error')!r}",
    )


def check_arm_efforts(run: Mapping[str, Any], families: Sequence[str]) -> list[CheckResult]:
    """One row per family: the arm at its sweep position records `effort == DEFAULT_ARM_EFFORT`."""
    matrix = run.get("matrix")
    arms = matrix.get("arms") if isinstance(matrix, Mapping) else None
    arm_list = arms if isinstance(arms, list) else []
    rows: list[CheckResult] = []
    for index, family in enumerate(families):
        arm = arm_list[index] if index < len(arm_list) else None
        provenance = arm.get("provenance") if isinstance(arm, Mapping) else None
        effort = provenance.get("effort") if isinstance(provenance, Mapping) else None
        model_id = provenance.get("model_id") if isinstance(provenance, Mapping) else None
        recorded = "absent" if effort is None else repr(effort)
        rows.append(
            _row(
                f"arm_effort_is_default:{family}",
                effort == DEFAULT_ARM_EFFORT,
                f"arm {index} ({family}, model_id={model_id!r}) provenance.effort={recorded}, "
                f"expected {DEFAULT_ARM_EFFORT!r}",
            )
        )
    return rows


def _mapping(value: object, key: str) -> Mapping[str, Any] | None:
    """`value[key]` when `value` is a mapping and that entry is one, else `None`."""
    entry = value.get(key) if isinstance(value, Mapping) else None
    return entry if isinstance(entry, Mapping) else None


def check_arm_models(run: Mapping[str, Any], families: Sequence[str]) -> list[CheckResult]:
    """One row per family: the arm's supervisor and per-tier configured models are `models.yaml`'s.

    The expected ids come from :func:`providers.capabilities.load_capability_matrix`. An absent
    `provenance.model_id` or `tier_task_metrics.by_tier.<tier>.configured_model_id` fails the row.
    """
    capabilities = load_capability_matrix()
    matrix = run.get("matrix")
    arms = matrix.get("arms") if isinstance(matrix, Mapping) else None
    arm_list = arms if isinstance(arms, list) else []
    rows: list[CheckResult] = []
    for index, family in enumerate(families):
        expected = {tier: capabilities.tiers[cast(Family, family)][tier].model_id for tier in TIERS}
        arm = arm_list[index] if index < len(arm_list) else None
        provenance = _mapping(arm, "provenance")
        by_tier = _mapping(_mapping(arm, TIER_TASK_METRICS_KEY), "by_tier")
        recorded: dict[str, object] = {
            "provenance.model_id": provenance.get("model_id") if provenance else None,
            **{
                f"{tier}.configured_model_id": (
                    entry.get("configured_model_id")
                    if (entry := _mapping(by_tier, tier)) is not None
                    else None
                )
                for tier in TIERS
            },
        }
        wanted: dict[str, str] = {
            "provenance.model_id": expected["supervisor"],
            **{f"{tier}.configured_model_id": expected[tier] for tier in TIERS},
        }
        mismatches = [
            f"{field}={'absent' if recorded[field] is None else repr(recorded[field])}, "
            f"expected {wanted[field]!r}"
            for field in wanted
            if recorded[field] != wanted[field]
        ]
        rows.append(
            _row(
                f"arm_models_are_configured:{family}",
                not mismatches,
                f"arm {index} ({family}): "
                + ("; ".join(mismatches) if mismatches else f"every model matches {wanted}"),
            )
        )
    return rows


def check_prompt_bundle(run: Mapping[str, Any], families: Sequence[str]) -> CheckResult:
    """Every arm records the prompt bundle digest this checkout computes; absent or short fails."""
    expected = prompt_bundle_sha256()
    matrix = run.get("matrix")
    arms = matrix.get("arms") if isinstance(matrix, Mapping) else None
    arm_list = arms if isinstance(arms, list) else []
    recorded: list[str] = []
    ok = len(arm_list) >= len(families)
    for index, family in enumerate(families):
        arm = arm_list[index] if index < len(arm_list) else None
        provenance = arm.get("provenance") if isinstance(arm, Mapping) else None
        digest = provenance.get("prompt_bundle_sha256") if isinstance(provenance, Mapping) else None
        ok = ok and digest == expected
        recorded.append(f"{family}={'absent' if digest is None else repr(digest)}")
    return _row(
        "prompt_bundle_matches_repo",
        ok,
        f"provenance.prompt_bundle_sha256 {', '.join(recorded)}; repo {expected!r}",
    )


def check_execution(sfn: Any, execution_arn: str, *, check: str) -> CheckResult:
    """The execution is `SUCCEEDED` and its history carries no `ExecutionAborted` event."""
    described = sfn.describe_execution(executionArn=execution_arn)
    status = str(described.get("status"))
    aborted = 0
    token: str | None = None
    while True:
        request: dict[str, Any] = {"executionArn": execution_arn, "maxResults": 1000}
        if token:
            request["nextToken"] = token
        page = sfn.get_execution_history(**request)
        aborted += sum(1 for e in page.get("events", []) if e.get("type") == "ExecutionAborted")
        token = page.get("nextToken")
        if not token:
            break
    return _row(
        check,
        status == "SUCCEEDED" and aborted == 0,
        f"execution status={status} ExecutionAborted events={aborted}",
    )


def _deserialise(item: Mapping[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def result_rows(dynamodb: Any, table_name: str, run_id: str) -> list[dict[str, Any]]:
    """Every `eval_results` row of `run_id`, paginated."""
    rows: list[dict[str, Any]] = []
    start: dict[str, Any] | None = None
    while True:
        request: dict[str, Any] = {
            "TableName": table_name,
            "KeyConditionExpression": "run_id = :r",
            "ExpressionAttributeValues": {":r": {"S": run_id}},
        }
        if start:
            request["ExclusiveStartKey"] = start
        page = dynamodb.query(**request)
        rows.extend(_deserialise(item) for item in page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return rows


def check_result_rows(
    dynamodb: Any, table_name: str, run_id: str, *, expected_done: int, repeats: int = 1
) -> CheckResult:
    """`expected_done` rows are `done`, each recorded `stream_calls > 0` (Runtime reached), each
    `result_id` is distinct, and every `repeat_index` in `range(repeats)` holds an equal share."""
    rows = result_rows(dynamodb, table_name, run_id)
    done = [row for row in rows if row.get("item_status") == "done"]
    streamed = [row for row in done if int(row.get("stream_calls") or 0) > 0]
    distinct = len({str(row.get("result_id")) for row in rows})
    per_repeat: dict[object, int] = {}
    for row in rows:
        index = row.get("repeat_index")
        key = (
            int(index) if isinstance(index, int | float) and not isinstance(index, bool) else index
        )
        per_repeat[key] = per_repeat.get(key, 0) + 1
    share = expected_done // repeats if repeats else 0
    wanted = dict.fromkeys(range(repeats), share)
    ok = (
        len(done) == expected_done
        and len(streamed) == len(done)
        and distinct == len(rows)
        and per_repeat == wanted
    )
    return _row(
        "results_done_with_runtime_streams",
        ok,
        f"{len(rows)} eval_results row(s): {len(done)} done (expected {expected_done}), "
        f"{len(streamed)} with stream_calls>0, {distinct} distinct result_id(s), rows per "
        f"repeat_index {dict(sorted(per_repeat.items(), key=str))} (expected {wanted})",
    )


def tenant_row_counts(store: Any, tenant: str) -> dict[str, int]:
    """Rows `tenant` holds in each tenant-scoped table, every page counted."""
    counts: dict[str, int] = {}
    for table in sorted(TENANT_SCOPED_TABLES):
        total = 0
        cursor: dict[str, object] | None = None
        while True:
            rows, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
            total += len(rows)
            if cursor is None:
                break
        counts[table] = total
    return counts


def check_tenant_empty(store: Any, tenant: str, *, check: str) -> CheckResult:
    counts = tenant_row_counts(store, tenant)
    residual = {table: n for table, n in counts.items() if n}
    return _row(check, not residual, f"tenant residual rows: {residual or 0}")


def check_tenant_emptied_by_the_workers_own_role(
    store: Any, run_id: str, tenant: str, unit_tenants: Sequence[str] = ()
) -> CheckResult:
    """`tenant` and every unit tenant are empty after a teardown the run's `eval_runs` catalog row
    records as run.

    The catalog row is read first: `torn_down` is written only after `delete_tenant` returned, so
    residual rows then are a finding about the teardown role's grants. Any other status means
    teardown did not run, and residual rows are reported without that attribution. Residual rows
    are keyed by table for the run tenant alone, else by tenant then table.
    """
    check = "tenant_emptied_by_the_workers_own_role"
    try:
        catalog = store.get_item(EVAL_RUNS_TABLE, {"run_id": run_id})
    except Exception as exc:  # noqa: BLE001 - an unreadable discriminator is a failing row
        return _row(check, False, f"eval_runs catalog row unreadable: {type(exc).__name__}: {exc}")
    lifecycle = str(catalog.get("status") or "absent") if isinstance(catalog, Mapping) else "absent"
    by_tenant = {
        name: {table: n for table, n in tenant_row_counts(store, name).items() if n}
        for name in (tenant, *unit_tenants)
    }
    residual: Mapping[str, object] = (
        by_tenant[tenant]
        if not unit_tenants
        else {name: rows for name, rows in by_tenant.items() if rows}
    )
    if lifecycle != RUN_STATUS_TORN_DOWN:
        return _row(
            check,
            False,
            f"eval_runs catalog row is {lifecycle!r}, not {RUN_STATUS_TORN_DOWN!r}: teardown did "
            f"not run to completion, so the residual rows {residual or 0} are NOT attributed to "
            "the teardown role's grants",
        )
    return _row(
        check,
        not residual,
        f"catalog row {RUN_STATUS_TORN_DOWN!r}; residual rows read with the runner's "
        f"credentials: {residual or 0}"
        + (
            ""
            if not residual
            else ". Rows survived a teardown that ran under sweep_control's {Query, "
            "BatchWriteItem} role: a grant the role needs is missing"
        ),
    )


def check_unit_tenants_recorded(
    store: Any, run_id: str, expected: Sequence[str]
) -> tuple[CheckResult, tuple[str, ...]]:
    """The run's `eval_runs` catalog row records exactly `expected` as `unit_tenants`, in order.

    Returns the row and the recorded unit tenants (empty when unreadable or absent).
    """
    check = "unit_tenants_recorded"
    try:
        catalog = store.get_item(EVAL_RUNS_TABLE, {"run_id": run_id})
    except Exception as exc:  # noqa: BLE001 - an unreadable catalog row is a failing row
        return _row(
            check, False, f"eval_runs catalog row unreadable: {type(exc).__name__}: {exc}"
        ), ()
    raw = catalog.get("unit_tenants") if isinstance(catalog, Mapping) else None
    recorded = tuple(str(t) for t in raw) if isinstance(raw, list | tuple) else ()
    return (
        _row(
            check,
            recorded == tuple(expected),
            f"catalog unit_tenants={list(recorded) if raw is not None else 'absent'}, expected "
            f"{list(expected)}",
        ),
        recorded,
    )


def check_unit_tenants_seeded(
    store: Any, run: Mapping[str, Any], unit_tenants: Sequence[str]
) -> CheckResult:
    """While the run is still running with a task done, every unit tenant holds rows.

    A run already terminal when observed cannot show its seed, since teardown has emptied it.
    """
    check = "unit_tenants_seeded"
    if not unit_tenants:
        return _row(check, False, "no unit tenant to observe")
    if is_terminal(run) or int(run.get("tasks_done") or 0) < 1:
        return _row(
            check,
            False,
            f"not observable: run status={run.get('status')!r} tasks_done={run.get('tasks_done')}; "
            "the seed is visible only while the run is running with a task done",
        )
    totals = {tenant: sum(tenant_row_counts(store, tenant).values()) for tenant in unit_tenants}
    empty = sorted(tenant for tenant, total in totals.items() if not total)
    return _row(
        check,
        not empty,
        f"rows per unit tenant while running: {totals}" + (f"; unseeded: {empty}" if empty else ""),
    )


def check_run_repeats(run: Mapping[str, Any], *, expected: int) -> CheckResult:
    """The run payload records the `repeats` the rehearsal submitted."""
    return _row(
        "run_records_repeats",
        run.get("repeats") == expected,
        f"run repeats={run.get('repeats')!r}, expected {expected}",
    )


def check_demo_untouched(before: Mapping[str, int], after: Mapping[str, int]) -> CheckResult:
    changed = {t: (before.get(t), after.get(t)) for t in after if before.get(t) != after.get(t)}
    return _row(
        "demo_tenant_untouched",
        not changed and sum(before.values()) > 0,
        f"demo row counts changed (before, after): {changed}"
        if changed
        else f"demo rows per table unchanged across the sweep ({sum(after.values())} rows)"
        if sum(before.values()) > 0
        else "the demo baseline counted 0 rows, so the tenant scan is blind and proves nothing",
    )


def check_canary_empty(store: Any) -> CheckResult:
    found = find_canary_records(store)
    return _row(
        "canary_tenant_empty",
        not found,
        f"{len(found)} row(s) under {CANARY_TENANT!r}"
        + ("" if not found else f": {[(r.table, r.key_value) for r in found[:5]]}"),
    )


def check_lock_absent(store: Any, *, check: str) -> CheckResult:
    lock = store.get_item(EVAL_RUNS_TABLE, {"run_id": SWEEP_LOCK_ROW_ID})
    return _row(
        check,
        lock is None,
        "lock#sweep absent"
        if lock is None
        else f"lock#sweep still held by {lock.get('holder_run_id')!r}",
    )


def check_listed(api: RunApi, run_id: str, *, rep_id: str) -> CheckResult:
    """`list` page 1 carries the run with its starter."""
    status, payload = api.call({"action": "list"})
    raw_runs = payload.get("runs")
    runs: list[Any] = raw_runs if isinstance(raw_runs, list) else []
    match = next((r for r in runs if isinstance(r, dict) and r.get("run_id") == run_id), None)
    ok = status == 200 and match is not None and match.get("started_by_rep_id") == rep_id
    return _row(
        "list_shows_run_with_starter",
        ok,
        f"list -> HTTP {status}, {len(runs)} run(s); rehearsal present={match is not None}, "
        f"starter matches={match is not None and match.get('started_by_rep_id') == rep_id}",
    )


def artifact_root(prefix: str, run_id: str) -> str:
    """The S3 key prefix a run's artefacts are written beneath: `<prefix>/<run_id>/`."""
    return f"{prefix.strip('/')}/{run_id}/"


#: The S3 error code each refused probe must carry.
PLAIN_HTTP_ERROR_CODE: Final[str] = "AccessDenied"
TAMPERED_KEY_ERROR_CODE: Final[str] = "SignatureDoesNotMatch"

#: `(url) -> (HTTP status, S3 error <Code> or None)` for a GET of a presigned artefact URL.
UrlFetch = Callable[[str], tuple[int, str | None]]

#: The `<Code>` element of an S3 XML error body.
_S3_ERROR_CODE: Final[re.Pattern[bytes]] = re.compile(rb"<Code>([^<]{1,64})</Code>")

#: Bytes of an error body read for its `<Code>`; S3 error documents are well under this.
_ERROR_BODY_LIMIT: Final[int] = 4096


def s3_error_code(body: bytes) -> str | None:
    """The `<Code>` of an S3 XML error body, or `None` when the body carries none."""
    match = _S3_ERROR_CODE.search(body)
    return match.group(1).decode("ascii", "replace") if match else None


def fetch_status(url: str) -> tuple[int, str | None]:
    """GET `url` over https or plain http, following no redirect: its status and S3 error code.

    The error code is read from the body of a non-2xx response only.
    """
    parsed = urllib.parse.urlsplit(url)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection: http.client.HTTPConnection = (
        http.client.HTTPSConnection(parsed.netloc, timeout=30)
        if parsed.scheme == "https"
        else http.client.HTTPConnection(parsed.netloc, timeout=30)  # nosec B309  # deliberate non-TLS denial probe
    )
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        if 200 <= response.status < 300:
            return response.status, None
        return response.status, s3_error_code(response.read(_ERROR_BODY_LIMIT))
    finally:
        connection.close()


def check_artefact_url(
    api: RunApi, run: Mapping[str, Any], *, fetch: UrlFetch = fetch_status
) -> Iterator[CheckResult]:
    """`artefact_url` presigns one recorded artefact: https GET 200; plain http refused 403
    `AccessDenied` (the TLS-only bucket policy); a tampered key refused 403
    `SignatureDoesNotMatch`."""
    run_id = str(run.get("run_id") or "")
    artifacts = run.get("artifacts")
    raw = artifacts.get("raw") if isinstance(artifacts, Mapping) else None
    key = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], str) else None
    if not run_id or key is None:
        yield _row(
            "artefact_url_issued", False, f"no recorded artefact to sign ({summarise(run, 160)})"
        )
        return
    status, payload = api.artefact_url(run_id, key)
    url = payload.get("url")
    issued = status == 200 and isinstance(url, str) and url.startswith("https://")
    yield _row(
        "artefact_url_issued",
        issued,
        f"artefact_url {key!r} -> HTTP {status}, https url={issued}, "
        f"expires_in={payload.get('expires_in')!r}",
    )
    if not issued or not isinstance(url, str):
        return
    parts = urllib.parse.urlsplit(url)
    probes: tuple[tuple[str, str, int, str | None], ...] = (
        ("artefact_url_https_get_succeeds", url, 200, None),
        (
            "artefact_url_plain_http_denied",
            "http://" + url.removeprefix("https://"),
            403,
            PLAIN_HTTP_ERROR_CODE,
        ),
        (
            "artefact_url_tampered_key_denied",
            urllib.parse.urlunsplit(parts._replace(path=parts.path + ".tampered")),
            403,
            TAMPERED_KEY_ERROR_CODE,
        ),
    )
    for check, probe, expected, expected_code in probes:
        try:
            observed, code = fetch(probe)
        except (OSError, http.client.HTTPException) as exc:
            yield _row(check, False, f"GET failed: {type(exc).__name__}: {exc}")
            continue
        yield _row(
            check,
            observed == expected and code == expected_code,
            f"GET -> HTTP {observed} code={code!r}, expected {expected} code={expected_code!r}",
        )


def download_artifacts(s3: Any, *, bucket: str, root: str, into: Path) -> int:
    """Download every object under `root` into `into`, keeping relative paths; returns the count."""
    count = 0
    token: str | None = None
    while True:
        request: dict[str, Any] = {"Bucket": bucket, "Prefix": root}
        if token:
            request["ContinuationToken"] = token
        page = s3.list_objects_v2(**request)
        for entry in page.get("Contents", []):
            key = str(entry["Key"])
            relative = key[len(root) :]
            if not relative or relative.endswith("/") or ".." in Path(relative).parts:
                continue
            target = into / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            target.write_bytes(body)
            count += 1
        token = page.get("NextContinuationToken")
        if not token:
            return count


def check_artifacts(
    s3: Any,
    run: Mapping[str, Any],
    *,
    bucket: str,
    prefix: str,
    arm_count: int,
    task_count: int,
) -> CheckResult:
    """The run's S3 artefacts, downloaded, pass `eval.harness.assert_artifacts`.

    Raw archive paths are the record's `artifacts.raw` list, relative to the run's artefact root.
    """
    run_id = str(run["run_id"])
    root = artifact_root(prefix, run_id)
    artifacts = run.get("artifacts")
    raw = artifacts.get("raw") if isinstance(artifacts, Mapping) else None
    if not isinstance(raw, list) or not raw:
        return _row(
            "artifacts_pass_assert_artifacts",
            False,
            f"the record carries no artifacts.raw list ({summarise(artifacts or {}, 160)})",
        )
    with tempfile.TemporaryDirectory(prefix="eval-sfn-artifacts-") as scratch:
        directory = Path(scratch)
        downloaded = download_artifacts(s3, bucket=bucket, root=root, into=directory)
        try:
            assert_artifacts(
                output_dir=directory,
                expected_arm_count=arm_count,
                expected_task_count=task_count,
                raw_archive_paths=tuple(directory / str(path) for path in raw),
            )
        except ArtifactAssertionError as exc:
            return _row(
                "artifacts_pass_assert_artifacts",
                False,
                f"{downloaded} object(s) under s3://{bucket}/{root}: {exc}",
            )
    return _row(
        "artifacts_pass_assert_artifacts",
        True,
        f"{downloaded} object(s) under s3://{bucket}/{root} pass assert_artifacts",
    )


def check_cancelled_record(run: Mapping[str, Any], *, expected_total: int) -> CheckResult:
    """A cancelled run: `cancelled`, fewer than all tasks done, partial matrix, teardown ok."""
    matrix = run.get("matrix")
    arms = matrix.get("arms") if isinstance(matrix, Mapping) else None
    done = run.get("tasks_done")
    ok = (
        run.get("status") == "cancelled"
        and isinstance(done, int)
        and CANCEL_AFTER_TASKS <= done < expected_total
        and isinstance(arms, list)
        and len(arms) >= 1
        and run.get("teardown_status") == "ok"
    )
    return _row(
        "cancelled_run_partial_and_torn_down",
        ok,
        f"status={run.get('status')!r} done={done}/{expected_total} "
        f"skipped={run.get('tasks_skipped')} arms={len(arms) if isinstance(arms, list) else None} "
        f"teardown={run.get('teardown_status')!r}",
    )


# -- role simulations ----------------------------------------------------------------------------


def _function_name(reference: str) -> str:
    """The function name in a Lambda ARN (qualifier dropped), or `reference` itself."""
    parts = reference.split(":")
    return parts[6] if reference.startswith("arn:") and len(parts) >= 7 else reference


def sweep_function_arns(definition: Mapping[str, Any]) -> dict[str, str]:
    """`{"sweep-control": ref, "sweep-task": ref}` from every Task `FunctionName` in a definition."""
    found: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            reference = node.get("FunctionName")
            if isinstance(reference, str):
                for suffix in ("sweep-control", "sweep-task"):
                    if _function_name(reference).endswith(f"-{suffix}"):
                        found[suffix] = reference
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(definition)
    return found


def simulation_rows(
    *,
    control_role: str,
    task_role: str,
    machine_role: str,
    trigger_role: str,
    control_arn: str,
    task_arn: str,
    trigger_arn: str,
    state_machine_arn: str,
    eval_runs_arn: str,
    accounts_arn: str,
    runtime_arn: str,
    artifacts_bucket_arn: str,
    artifacts_prefix: str,
) -> tuple[SimulationRow, ...]:
    """IAM rows for `sweep_control`, `sweep_task`, the state machine role and `eval_trigger`."""
    execution_arn = execution_arn_for(state_machine_arn, "smoke-simulation")
    inside = f"{artifacts_bucket_arn}/{artifacts_prefix.strip('/')}/smoke/matrix.json"
    outside = f"{artifacts_bucket_arn}/outside-prefix/matrix.json"
    job, lock = ("job#smoke",), (SWEEP_LOCK_ROW_ID,)

    def row(
        check: str, role: str, action: str, resource: str, allowed: bool, keys: Any = None
    ) -> SimulationRow:
        return SimulationRow(check, role, action, resource, allowed, keys)

    return (
        row("control_allows_job_get", control_role, "dynamodb:GetItem", eval_runs_arn, True, job),
        row(
            "control_allows_lock_delete",
            control_role,
            "dynamodb:DeleteItem",
            eval_runs_arn,
            True,
            lock,
        ),
        row(
            "control_denies_job_delete",
            control_role,
            "dynamodb:DeleteItem",
            eval_runs_arn,
            False,
            job,
        ),
        row("control_allows_artifact_put", control_role, "s3:PutObject", inside, True),
        row("control_denies_put_outside_prefix", control_role, "s3:PutObject", outside, False),
        row(
            "control_denies_invoke_runtime",
            control_role,
            "bedrock-agentcore:InvokeAgentRuntime",
            runtime_arn,
            False,
        ),
        row("control_denies_invoke_lambda", control_role, "lambda:InvokeFunction", task_arn, False),
        row(
            "control_denies_stop_execution",
            control_role,
            "states:StopExecution",
            execution_arn,
            False,
        ),
        row(
            "task_allows_invoke_runtime",
            task_role,
            "bedrock-agentcore:InvokeAgentRuntime",
            runtime_arn,
            True,
        ),
        row("task_allows_job_update", task_role, "dynamodb:UpdateItem", eval_runs_arn, True, job),
        row(
            "task_denies_tenant_write",
            task_role,
            "dynamodb:PutItem",
            accounts_arn,
            False,
            ("eval-smoke",),
        ),
        row("task_denies_artifact_put", task_role, "s3:PutObject", inside, False),
        row("task_denies_stop_execution", task_role, "states:StopExecution", execution_arn, False),
        row(
            "machine_allows_invoke_control",
            machine_role,
            "lambda:InvokeFunction",
            control_arn,
            True,
        ),
        row("machine_allows_invoke_task", machine_role, "lambda:InvokeFunction", task_arn, True),
        row(
            "machine_denies_invoke_trigger",
            machine_role,
            "lambda:InvokeFunction",
            trigger_arn,
            False,
        ),
        row(
            "machine_denies_stop_execution",
            machine_role,
            "states:StopExecution",
            execution_arn,
            False,
        ),
        *(
            row(f"machine_allows_{_short(action)}", machine_role, action, "*", True)
            for action in SFN_XRAY_TRACING_ACTIONS
        ),
        *(
            row(f"machine_denies_{_short(action)}", machine_role, action, "*", False)
            for action in SFN_XRAY_DENIED_ACTIONS
        ),
        row(
            "trigger_allows_start_execution",
            trigger_role,
            "states:StartExecution",
            state_machine_arn,
            True,
        ),
        row(
            "trigger_allows_describe_execution",
            trigger_role,
            "states:DescribeExecution",
            execution_arn,
            True,
        ),
        row(
            "trigger_denies_stop_execution",
            trigger_role,
            "states:StopExecution",
            execution_arn,
            False,
        ),
        row("trigger_allows_artifact_get", trigger_role, "s3:GetObject", inside, True),
        row("trigger_denies_get_outside_prefix", trigger_role, "s3:GetObject", outside, False),
        row("trigger_denies_artifact_put", trigger_role, "s3:PutObject", inside, False),
        row(
            "trigger_denies_list_bucket", trigger_role, "s3:ListBucket", artifacts_bucket_arn, False
        ),
    )


#: `(table, index name or None) -> ARN`: a deployed table ARN, or its index ARN.
TableArn = Callable[[str, "str | None"], str]


def _short(action: str) -> str:
    return action.partition(":")[2]


def _where(table: str, index: str | None) -> str:
    return table if index is None else f"{table}/{index}"


def _grant_resources(grant: LocalGrant) -> list[str | None]:
    """The resources a grant object's Allow names: the table, and each listed index."""
    return [*([None] if grant.include_table_arn else []), *grant.index_names]


def _conditioned_rows(
    who: str, role: str, grants: Sequence[LocalGrant], table_arn: TableArn
) -> list[SimulationRow]:
    """An allow per (conditioned grant, action, resource, value) and its out-of-scope denies.

    A `gsi_tenant` grant's denies also send each protected tenant (`demo`, `canary`) it does not
    admit.

    A deny key is out of scope for every grant this role holds on the same (table, action,
    resource), so a row cannot be satisfied by another statement.
    """
    rows: list[SimulationRow] = []
    for grant in grants:
        if grant.key_values is None or grant.operator is None:
            continue
        for action in grant.actions:
            for index in _grant_resources(grant):
                covering = [
                    g
                    for g in grants
                    if g.table == grant.table
                    and action in g.actions
                    and index in _grant_resources(g)
                ]
                if any(g.key_values is None for g in covering):
                    raise ValueError(
                        f"{who}: {action} on {_where(grant.table, index)} is also granted "
                        "unconditioned, so no key is out of scope"
                    )
                values = [v for g in covering for v in g.key_values or ()]
                operators = {g.operator for g in covering}
                arn = table_arn(grant.table, index)
                where = _where(grant.table, index)
                context = tuple((c.variable, c.values[0]) for c in grant.extra_conditions)
                for value in grant.key_values:
                    key = in_scope_key(value)
                    check = f"{who}_allows_{_short(action)}@{where}[{value}]"
                    rows.append(
                        ContextRow(check, role, action, arn, True, (key,), context)
                        if context
                        else SimulationRow(check, role, action, arn, True, (key,))
                    )
                for operator in sorted(o for o in operators if o is not None):
                    protected = [
                        tenant
                        for tenant in (DEMO_TENANT, CANARY_TENANT)
                        if index == TENANT_INDEX
                        and not any(condition_matches(v, tenant, operator) for v in values)
                    ]
                    for key in (*out_of_scope_keys(values, operator), *protected):
                        check = f"{who}_denies_{_short(action)}@{where}[{key}]"
                        if any(r.check == check for r in rows):
                            continue
                        rows.append(SimulationRow(check, role, action, arn, False, (key,)))
    return rows


@dataclass(frozen=True, slots=True)
class ExplicitDenyRow(SimulationRow):
    """A denied row whose decision must be `explicitDeny`: the role's deny statement covers it."""

    context: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ContextRow(SimulationRow):
    """A row of a grant with `extra_conditions`, simulated with each condition satisfied."""

    context: tuple[tuple[str, str], ...] = ()


def role_condition_context(grants: Sequence[LocalGrant]) -> tuple[tuple[str, str], ...]:
    """One `(variable, value)` per `extra_conditions` variable in `grants`, at the value every
    conditioned grant admits.

    IAM reports a variable some statement of the role conditions on as missing from any
    simulation that leaves it unset, so every DynamoDB row of the role sends it. The admitted value
    keeps a denial from being the extra condition's doing.

    Raises:
        ValueError: The role's grants admit no common value for a variable.
    """
    admitted: dict[str, list[str]] = {}
    for grant in grants:
        for condition in grant.extra_conditions:
            values = admitted.get(condition.variable)
            admitted[condition.variable] = (
                list(condition.values)
                if values is None
                else [v for v in values if v in condition.values]
            )
    empty = sorted(variable for variable, values in admitted.items() if not values)
    if empty:
        raise ValueError(f"no value every conditioned grant admits for {empty}")
    return tuple((variable, values[0]) for variable, values in sorted(admitted.items()))


def with_role_context(
    rows: Sequence[SimulationRow], contexts: Mapping[str, Sequence[tuple[str, str]]]
) -> tuple[SimulationRow, ...]:
    """`rows`, each DynamoDB row of a role in `contexts` carrying that role's context entries.

    A row's own context wins over the role's for a shared variable.
    """
    out: list[SimulationRow] = []
    for row in rows:
        role_context = contexts.get(row.role_arn, ())
        if not role_context or not row.action.startswith("dynamodb:"):
            out.append(row)
            continue
        own = row.context if isinstance(row, ExplicitDenyRow | ContextRow) else ()
        merged = tuple({**dict(role_context), **dict(own)}.items())
        if isinstance(row, ExplicitDenyRow | ContextRow):
            out.append(dataclasses.replace(row, context=merged))
        else:
            fields = {f.name: getattr(row, f.name) for f in dataclasses.fields(row)}
            out.append(ContextRow(**fields, context=merged))
    return tuple(out)


class _ContextIam:
    """`iam` whose simulate requests also carry one string context entry per `context` pair."""

    def __init__(self, iam: Any, context: Sequence[tuple[str, str]]) -> None:
        self._iam = iam
        self._entries = [
            {"ContextKeyName": name, "ContextKeyType": "string", "ContextKeyValues": [value]}
            for name, value in context
        ]
        self.meta = getattr(iam, "meta", None)

    def simulate_principal_policy(self, **request: Any) -> Any:
        entries = [*request.get("ContextEntries", []), *self._entries]
        return self._iam.simulate_principal_policy(**{**request, "ContextEntries": entries})


def context_result(iam: Any, row: ContextRow, *, resource: str) -> CheckResult:
    """:func:`simulation_result` of `row` with its context entries added to the request."""
    result = simulation_result(_ContextIam(iam, row.context), row, resource=resource)
    return dataclasses.replace(result, detail=f"{result.detail}; context={dict(row.context)}")


def explicit_deny_result(iam: Any, row: SimulationRow, *, resource: str) -> CheckResult:
    """Simulate `row` and pass only on `explicitDeny` with no decisive missing context key."""
    subject = f"{row.action} on {row.resource_arn} LeadingKeys={list(row.leading_keys or ())}"
    if isinstance(row, ExplicitDenyRow) and row.context:
        iam = _ContextIam(iam, row.context)
        subject += f" context={dict(row.context)}"
    try:
        decision, missing = simulate_evaluation(
            iam, row.role_arn, row.action, row.resource_arn, leading_keys=row.leading_keys
        )
    except Exception as exc:  # noqa: BLE001 - reported as a failing row, never swallowed
        return CheckResult(
            resource=resource,
            check=row.check,
            status="fail",
            detail=f"simulate_principal_policy raised {type(exc).__name__}: {exc} ({subject})",
        )
    decisive = decisive_missing_keys(row.action, missing)
    unset = f"; MissingContextValues={list(decisive)}" if decisive else ""
    return CheckResult(
        resource=resource,
        check=row.check,
        status="pass" if decision == "explicitDeny" and not decisive else "fail",
        detail=f"{subject}: {decision} (expected explicitDeny) for {row.role_arn}{unset}",
    )


def _probe_key(grants: Sequence[LocalGrant], table: str, index: str | None) -> str:
    """A key the role's own grants on `table` would admit, so a deny is the action's, not the key's."""
    if index is not None and index in INDEX_IN_SCOPE_LEADING_KEY:
        return INDEX_IN_SCOPE_LEADING_KEY[index]
    for grant in grants:
        if grant.table == table and grant.key_values:
            return in_scope_key(grant.key_values[0])
    return in_scope_key("eval-*")


def _removed_rows(
    who: str, role: str, grants: Sequence[LocalGrant], table_arn: TableArn
) -> list[SimulationRow]:
    """An `explicitDeny` per asserted action the role holds on no resource of a table, on every table.

    Covers removed grants without listing them: a removed grant is an
    asserted action the role's grant list no longer names for that table.
    """
    granted: dict[str, set[str]] = {}
    for grant in grants:
        granted.setdefault(grant.table, set()).update(grant.actions)
    asserted = asserted_dynamodb_actions()
    rows: list[SimulationRow] = []
    for table in sorted(TABLES):
        for action in sorted(asserted - granted.get(table, set())):
            indexes: list[str | None] = (
                [None, *TABLES[table].gsis] if action == "dynamodb:Query" else [None]
            )
            for index in indexes:
                rows.append(
                    ExplicitDenyRow(
                        f"{who}_denies_removed_{_short(action)}@{_where(table, index)}",
                        role,
                        action,
                        table_arn(table, index),
                        False,
                        (_probe_key(grants, table, index),),
                    )
                )
    return rows


def require_leading_keys(rows: Sequence[SimulationRow]) -> tuple[SimulationRow, ...]:
    """`rows`, refusing any DynamoDB row with no `dynamodb:LeadingKeys` context.

    Raises:
        ValueError: A DynamoDB row carries no key; its decision would not exercise the condition.
    """
    bare = [r.check for r in rows if r.action.startswith("dynamodb:") and not r.leading_keys]
    if bare:
        raise ValueError(f"DynamoDB simulation rows without dynamodb:LeadingKeys context: {bare}")
    return tuple(rows)


def sweep_grant_rows(
    *, control_role: str, task_role: str, table_arn: TableArn
) -> tuple[SimulationRow, ...]:
    """The narrowed sweep grants' rows, derived from `infra/eval_sfn.tf`'s grant locals."""
    roles = {"sweep-control": ("control", control_role), "sweep-task": ("task", task_role)}
    rows: list[SimulationRow] = []
    for function, local in SWEEP_GRANT_LOCALS.items():
        who, role = roles[function]
        grants = terraform_local_grants(local)
        rows += _conditioned_rows(who, role, grants, table_arn)
        rows += _removed_rows(who, role, grants, table_arn)
    return require_leading_keys(rows)


def _placeholder_table_arn(table: str, index: str | None) -> str:
    return f"arn:aws:dynamodb:us-east-1:123456789012:table/{table}" + (
        f"/index/{index}" if index else ""
    )


def check_tracing(described: Mapping[str, Any]) -> CheckResult:
    """The deployed state machine traces with X-Ray: `tracingConfiguration.enabled is True`."""
    tracing = described.get("tracingConfiguration")
    enabled = tracing.get("enabled") if isinstance(tracing, Mapping) else None
    return _row(
        "machine_tracing_enabled",
        enabled is True,
        f"describe-state-machine tracingConfiguration={tracing!r} (expected enabled=True)",
    )


def simulation_row_count() -> int:
    """How many rows :func:`role_simulations` yields: the tracing row plus every simulation row,
    derived by building them over placeholders."""
    placeholder = "arn:aws:states:us-east-1:123456789012:stateMachine:placeholder"
    sweep = sweep_grant_rows(control_role="r", task_role="r", table_arn=_placeholder_table_arn)
    design = simulation_rows(
        control_role="r",
        task_role="r",
        machine_role="r",
        trigger_role="r",
        control_arn="f",
        task_arn="f",
        trigger_arn="f",
        state_machine_arn=placeholder,
        eval_runs_arn="t",
        accounts_arn="t",
        runtime_arn="a",
        artifacts_bucket_arn="b",
        artifacts_prefix="p",
    )
    return 1 + len(sweep) + len(design)


def role_simulations(ctx: SmokeContext) -> Iterator[CheckResult]:
    """Resolve every role from the deployed state machine and functions, then simulate each row."""
    state_machine_arn = ctx.outputs.require_str("eval_state_machine_arn")
    sfn, lambda_client = ctx.client("stepfunctions"), ctx.client("lambda")
    described = sfn.describe_state_machine(stateMachineArn=state_machine_arn)
    yield check_tracing(described)
    functions = sweep_function_arns(json.loads(str(described["definition"])))
    missing = {"sweep-control", "sweep-task"} - set(functions)
    if missing:
        yield _row(
            "sweep_functions_resolved",
            False,
            f"the deployed definition names no FunctionName for {sorted(missing)}",
        )
        return
    trigger_config = lambda_client.get_function_configuration(
        FunctionName=ctx.outputs.require_str("eval_trigger_function_name")
    )
    account_id = str(ctx.client("sts").get_caller_identity()["Account"])
    tables = ctx.outputs.require_mapping("dynamodb_table_names")

    def table_arn(logical: str) -> str:
        return f"arn:aws:dynamodb:{ctx.region}:{account_id}:table/{tables[logical]}"

    def resource_arn(logical: str, index: str | None) -> str:
        return table_arn(logical) + (f"/index/{index}" if index else "")

    control_role = role_arn_of_function(lambda_client, functions["sweep-control"])
    task_role = role_arn_of_function(lambda_client, functions["sweep-task"])
    rows = simulation_rows(
        control_role=control_role,
        task_role=task_role,
        machine_role=str(described["roleArn"]),
        trigger_role=str(trigger_config["Role"]),
        control_arn=functions["sweep-control"],
        task_arn=functions["sweep-task"],
        trigger_arn=str(trigger_config["FunctionArn"]),
        state_machine_arn=state_machine_arn,
        eval_runs_arn=table_arn(EVAL_RUNS_TABLE),
        accounts_arn=table_arn("accounts"),
        runtime_arn=ctx.outputs.require_str("agentcore_runtime_arn"),
        artifacts_bucket_arn=ctx.outputs.require_str("eval_artifacts_bucket_arn"),
        artifacts_prefix=ctx.outputs.require_str("eval_artifacts_prefix"),
    )
    rows += sweep_grant_rows(control_role=control_role, task_role=task_role, table_arn=resource_arn)
    contexts = {
        role: role_condition_context(terraform_local_grants(SWEEP_GRANT_LOCALS[function]))
        for function, role in (("sweep-control", control_role), ("sweep-task", task_role))
    }
    iam = ctx.client("iam")
    for simulation in with_role_context(require_leading_keys(rows), contexts):
        if isinstance(simulation, ExplicitDenyRow):
            yield explicit_deny_result(iam, simulation, resource="eval_sweep_roles")
        elif isinstance(simulation, ContextRow):
            yield context_result(iam, simulation, resource="eval_sweep_roles")
        else:
            yield simulation_result(iam, simulation, resource="eval_sweep_roles")


# -- cleanup -------------------------------------------------------------------------------------


def _reap_tenant(store: Any, tenant: str) -> Iterator[CheckResult]:
    """Tear down a sweep tenant left with rows; never `demo` or `canary`."""
    if not tenant.startswith("eval-") or tenant in (DEMO_TENANT, CANARY_TENANT):
        return
    try:
        residual = sum(tenant_row_counts(store, tenant).values())
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check
        yield _row("leftover_tenant_counted", False, f"{tenant}: {type(exc).__name__}: {exc}")
        return
    if not residual:
        return
    try:
        teardown_tenant(store, tenant=tenant)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check
        yield _row("leftover_tenant_reaped", False, f"{tenant}: {type(exc).__name__}: {exc}")
        return
    yield _row(
        "leftover_tenant_reaped",
        False,
        f"{tenant} still held {residual} row(s) after its run; reaped by the smoke (the state "
        "machine's teardown did not empty it)",
    )


def _settle_runs(
    api: RunApi | None,
    store: Any,
    runs: Mapping[str, str],
    units: Mapping[str, Sequence[str]] | None = None,
) -> Iterator[CheckResult]:
    """For each started run: cancel it if still running, then reap its tenant and each of its
    `units` tenants left with rows.

    Every step's error, and a non-200 `status`, becomes a failing row (that run's tenant is then
    not reaped), so the caller's user cleanup always runs.
    """
    for run_id, tenant in runs.items():
        if api is not None:
            try:
                status, payload = api.status(run_id)
                if status != 200:
                    yield _row(
                        "run_settled",
                        False,
                        f"{run_id}: status answered HTTP {status} {error_fields(payload)}; "
                        f"run state unknown, {tenant} not reaped",
                    )
                    continue
                if not is_terminal(payload):
                    cancel_status, cancel_payload = api.cancel(run_id)
                else:
                    cancel_status = 0
            except Exception as exc:  # noqa: BLE001 - surfaced as a failing check
                yield _row("run_settled", False, f"{run_id}: {type(exc).__name__}: {exc}")
                continue
            if cancel_status:
                yield _row(
                    "no_run_left_running",
                    False,
                    f"run was still {payload.get('status')!r} at cleanup; cooperative cancel -> "
                    f"HTTP {cancel_status} {cancel_payload.get('disposition')!r}",
                )
                continue
        yield from _reap_tenant(store, tenant)
        for unit in (units or {}).get(run_id, ()):
            yield from _reap_tenant(store, unit)


# -- flows ---------------------------------------------------------------------------------------


def _rehearsal(
    ctx: SmokeContext,
    api: RunApi,
    store: Any,
    started: dict[str, str],
    *,
    rep_id: str,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    fetch: UrlFetch = fetch_status,
    units: dict[str, tuple[str, ...]] | None = None,
) -> Iterator[CheckResult]:
    families, task_count = sweep_shape()
    repeats = REHEARSAL_REPEATS
    if not 1 < repeats <= MAX_SWEEP_REPEATS:
        raise ValueError(f"REHEARSAL_REPEATS {repeats} must be in (1, {MAX_SWEEP_REPEATS}]")
    expected_total = len(families) * repeats * task_count
    poll_seconds = STUB_POLL_SECONDS * repeats
    demo_before = tenant_row_counts(store, DEMO_TENANT)

    status, payload = api.submit("stub", repeats=repeats)
    row, run_id = check_submit(status, payload, mode="stub", check="rehearsal_submitted")
    yield row
    if not run_id:
        return
    tenant = str(payload.get("tenant") or f"eval-{run_id}")
    started[run_id] = tenant
    expected_units = unit_tenants_for_run(run_id, arm_count=len(families), repeats=repeats)
    if units is not None:
        units[run_id] = expected_units

    row, stray = check_single_flight(api)
    yield row
    if stray:
        started[stray] = f"eval-{stray}"

    progressed = poll_run(
        api,
        run_id,
        until=lambda r: is_terminal(r) or int(r.get("tasks_done") or 0) >= 1,
        seconds=poll_seconds,
        clock=clock,
        sleeper=sleeper,
    )
    row, recorded_units = check_unit_tenants_recorded(store, run_id, expected_units)
    yield row
    yield check_unit_tenants_seeded(store, progressed, recorded_units or expected_units)

    run = poll_run(
        api, run_id, until=is_terminal, seconds=poll_seconds, clock=clock, sleeper=sleeper
    )
    if not is_terminal(run):
        yield _row(
            "rehearsal_reached_terminal",
            False,
            f"not terminal after {poll_seconds:.0f}s: {summarise(run, 200)}",
        )
        return
    yield check_completed_record(run, expected_total=expected_total, arm_count=len(families))
    yield check_run_repeats(run, expected=repeats)
    yield from check_arm_efforts(run, families)
    yield from check_arm_models(run, families)
    yield check_prompt_bundle(run, families)
    state_machine_arn = ctx.outputs.require_str("eval_state_machine_arn")
    yield check_execution(
        ctx.client("stepfunctions"),
        execution_arn_for(state_machine_arn, run_id),
        check="rehearsal_execution_succeeded",
    )
    yield check_result_rows(
        ctx.client("dynamodb"),
        store.physical_name(EVAL_RESULTS_TABLE),
        run_id,
        expected_done=expected_total,
        repeats=repeats,
    )
    yield check_artifacts(
        ctx.client("s3"),
        run,
        bucket=ctx.outputs.require_str("eval_artifacts_bucket_name"),
        prefix=ctx.outputs.require_str("eval_artifacts_prefix"),
        arm_count=len(families),
        task_count=task_count * repeats,
    )
    yield from check_artefact_url(api, run, fetch=fetch)
    yield check_tenant_empty(store, tenant, check="rehearsal_tenant_empty")
    yield check_tenant_emptied_by_the_workers_own_role(store, run_id, tenant, expected_units)
    yield check_demo_untouched(demo_before, tenant_row_counts(store, DEMO_TENANT))
    yield check_canary_empty(store)
    yield check_lock_absent(store, check="lock_released_after_rehearsal")
    yield check_listed(api, run_id, rep_id=rep_id)


def _cancel_proof(
    ctx: SmokeContext,
    api: RunApi,
    store: Any,
    started: dict[str, str],
    *,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> Iterator[CheckResult]:
    families, task_count = sweep_shape()
    expected_total = len(families) * task_count
    status, payload = api.submit("stub")
    row, run_id = check_submit(status, payload, mode="stub", check="cancel_proof_submitted")
    yield row
    if not run_id:
        return
    tenant = str(payload.get("tenant") or f"eval-{run_id}")
    started[run_id] = tenant

    progressed = poll_run(
        api,
        run_id,
        until=lambda r: is_terminal(r) or int(r.get("tasks_done") or 0) >= CANCEL_AFTER_TASKS,
        seconds=STUB_POLL_SECONDS,
        clock=clock,
        sleeper=sleeper,
    )
    if is_terminal(progressed) or int(progressed.get("tasks_done") or 0) < CANCEL_AFTER_TASKS:
        yield _row(
            "cancel_proof_reached_progress",
            False,
            f"run did not reach tasks_done>={CANCEL_AFTER_TASKS} while running: "
            f"{summarise(progressed, 200)}",
        )
        return
    cancel_status, cancel_payload = api.cancel(run_id)
    yield _row(
        "cancel_accepted",
        cancel_status == 200 and cancel_payload.get("disposition") == "accepted",
        f"cancel at tasks_done={progressed.get('tasks_done')} -> HTTP {cancel_status} "
        f"disposition={cancel_payload.get('disposition')!r}",
    )
    run = poll_run(
        api, run_id, until=is_terminal, seconds=STUB_POLL_SECONDS, clock=clock, sleeper=sleeper
    )
    if not is_terminal(run):
        yield _row(
            "cancel_proof_reached_terminal",
            False,
            f"not terminal after {STUB_POLL_SECONDS:.0f}s: {summarise(run, 200)}",
        )
        return
    yield check_cancelled_record(run, expected_total=expected_total)
    yield check_tenant_empty(store, tenant, check="cancel_proof_tenant_empty")
    yield check_lock_absent(store, check="lock_released_after_cancel")
    again_status, again_payload = api.cancel(run_id)
    yield _row(
        "second_cancel_already_terminal",
        again_status == 200 and again_payload.get("disposition") == "already_terminal",
        f"second cancel -> HTTP {again_status} disposition={again_payload.get('disposition')!r}",
    )
    yield check_execution(
        ctx.client("stepfunctions"),
        execution_arn_for(ctx.outputs.require_str("eval_state_machine_arn"), run_id),
        check="cancelled_execution_not_aborted",
    )


def checks(
    ctx: SmokeContext,
    *,
    provisioner: Provisioner | None = None,
    post: ApiPost | None = None,
    store: Any | None = None,
    simulations: Callable[[SmokeContext], Iterator[CheckResult]] | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
    fetch: UrlFetch | None = None,
) -> Iterator[CheckResult]:
    """The stub flow: estimate, rehearsal, cancel proof and role simulations; cleanup in `finally`.

    Args:
        ctx: The smoke context (`stepfunctions`, `dynamodb`, `s3`, `lambda`, `iam`, `sts` clients).
        provisioner: The admin user's provisioner; a live `RoleSmokeUserProvisioner` by default.
        post: The authenticated POST; :func:`live_api_post` by default.
        store: The operator's store over the deployed tables; :func:`live_store` by default.
        simulations: The role-simulation rows; :func:`role_simulations` by default.
        clock, sleeper: Time seams for the poll loops.
        fetch: GETs a presigned artefact URL and returns its status and S3 error code;
            :func:`fetch_status` by default.
    """
    admin = (
        provisioner
        if provisioner is not None
        else RoleSmokeUserProvisioner(ctx, full_visibility=True, admin_group=ADMIN_GROUP)
    )
    api_post = post if post is not None else live_api_post(ctx)
    resolved_store = store if store is not None else live_store(ctx)
    now = clock if clock is not None else time.monotonic
    wait = sleeper if sleeper is not None else time.sleep
    simulate = simulations if simulations is not None else role_simulations

    try:
        yield from simulate(ctx)
    except Exception as exc:  # noqa: BLE001 - the sweep checks still run
        yield _row("role_simulations", False, f"{type(exc).__name__}: {exc}")
    started: dict[str, str] = {}
    units: dict[str, tuple[str, ...]] = {}
    api: RunApi | None = None
    try:
        try:
            user = admin.provision()
        except Exception as exc:  # noqa: BLE001 - one actionable failing check
            yield _row("provision_admin_user", False, f"{type(exc).__name__}: {exc}")
            return
        yield _row("provision_admin_user", True, user.detail)
        api = RunApi(api_post, user.id_token)
        yield check_estimate(api)[0]
        yield from _rehearsal(
            ctx,
            api,
            resolved_store,
            started,
            rep_id=user.rep_id,
            clock=now,
            sleeper=wait,
            fetch=fetch if fetch is not None else fetch_status,
            units=units,
        )
        yield from _cancel_proof(ctx, api, resolved_store, started, clock=now, sleeper=wait)
    finally:
        try:
            yield from _settle_runs(api, resolved_store, started, units)
        finally:
            yield from admin.cleanup()


# -- live once -----------------------------------------------------------------------------------


def _run_cli(argv: Sequence[str]) -> tuple[int, str, str]:
    completed = proc_run.run(list(argv), capture_output=True, text=True, check=False, timeout=60)
    return completed.returncode, completed.stdout, completed.stderr


def retention_mode(cli_stdout: str) -> str | None:
    """The data-retention mode in `get-account-data-retention`'s JSON, or `None` if not exactly one.

    Every string value under a key whose name contains `mode` (any case, any depth) is a
    candidate; anything but exactly one distinct candidate is unreadable.
    """
    try:
        parsed: object = json.loads(cli_stdout)
    except json.JSONDecodeError:
        return None
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if "mode" in str(key).lower() and isinstance(value, str) and value:
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(parsed)
    return next(iter(found)) if len(found) == 1 else None


def check_data_retention(ctx: SmokeContext, runner: CliRunner) -> tuple[CheckResult, str | None]:
    """Read the account's Bedrock data-retention mode (read-only) and return it with its row.

    A failed read or a response naming no single mode fails, and the sweep is not submitted.
    """
    code, out, err = runner(
        [
            "aws",
            "bedrock",
            "get-account-data-retention",
            "--profile",
            ctx.profile,
            "--region",
            ctx.region,
            "--output",
            "json",
            "--no-cli-pager",
        ]
    )
    if code != 0:
        return _row("data_retention_read", False, f"exit {code}: {err.strip()[:240]}"), None
    mode = retention_mode(out)
    if mode is None:
        return (
            _row(
                "data_retention_read",
                False,
                f"no single retention mode in the response {out.strip()[:400]}",
            ),
            None,
        )
    return _row("data_retention_read", True, f"mode={mode!r}; response {out.strip()[:300]}"), mode


def check_retention_verdict(mode: str | None, verdict: str | None) -> CheckResult:
    """The operator's `--retention-verdict` names the mode just read.

    The verdict is the operator's record that this mode permits both model families; without it,
    or with any other value, nothing is submitted.
    """
    if verdict is None:
        detail = (
            f"no {RETENTION_VERDICT_FLAG} given. The mode read is {mode!r}: confirm it permits the "
            f"OpenAI models, then re-run with {RETENTION_VERDICT_FLAG}={mode}"
        )
        return _row("retention_verdict_matches", False, detail)
    ok = mode is not None and verdict == mode
    return _row(
        "retention_verdict_matches",
        ok,
        f"{RETENTION_VERDICT_FLAG}={verdict!r} matches the mode read"
        if ok
        else f"{RETENTION_VERDICT_FLAG}={verdict!r} does not match the mode read ({mode!r})",
    )


def live_once_checks(
    ctx: SmokeContext,
    *,
    provisioner: Provisioner | None = None,
    post: ApiPost | None = None,
    store: Any | None = None,
    runner: CliRunner | None = None,
    retention_verdict: str | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> Iterator[CheckResult]:
    """One live sweep: pre-checks, one submission, poll to terminal, verify; cleanup in `finally`.

    Nothing is submitted unless every pre-check passes, including `retention_verdict` equal to
    the mode this run reads.
    """
    admin = (
        provisioner
        if provisioner is not None
        else RoleSmokeUserProvisioner(ctx, full_visibility=True, admin_group=ADMIN_GROUP)
    )
    api_post = post if post is not None else live_api_post(ctx)
    resolved_store = store if store is not None else live_store(ctx)
    now = clock if clock is not None else time.monotonic
    wait = sleeper if sleeper is not None else time.sleep
    families, task_count = sweep_shape()
    expected_total = len(families) * task_count

    retention, mode = check_data_retention(ctx, runner if runner is not None else _run_cli)
    yield retention
    verdict = check_retention_verdict(mode, retention_verdict)
    yield verdict
    lock = check_lock_absent(resolved_store, check="lock_absent_before_submit")
    yield lock
    if "fail" in (retention.status, verdict.status, lock.status):
        yield _row("live_sweep_submitted", False, "not submitted: a pre-check failed")
        return

    started: dict[str, str] = {}
    api: RunApi | None = None
    try:
        try:
            user = admin.provision()
        except Exception as exc:  # noqa: BLE001 - one actionable failing check
            yield _row("provision_admin_user", False, f"{type(exc).__name__}: {exc}")
            return
        yield _row("provision_admin_user", True, user.detail)
        api = RunApi(api_post, user.id_token)
        estimate_row, _ = check_estimate(api)
        yield estimate_row
        if estimate_row.status == "fail":
            yield _row("live_sweep_submitted", False, "not submitted: no estimate was recorded")
            return

        status, payload = api.submit("live", confirmed=True)
        row, run_id = check_submit(status, payload, mode="live", check="live_sweep_submitted")
        yield row
        if not run_id:
            return
        tenant = str(payload.get("tenant") or f"eval-{run_id}")
        started[run_id] = tenant
        run = poll_run(
            api, run_id, until=is_terminal, seconds=LIVE_POLL_SECONDS, clock=now, sleeper=wait
        )
        if not is_terminal(run):
            yield _row(
                "live_sweep_reached_terminal",
                False,
                f"not terminal after {LIVE_POLL_SECONDS:.0f}s: {summarise(run, 200)}",
            )
            return
        yield check_completed_record(run, expected_total=expected_total, arm_count=len(families))
        cost = run.get("cost_spent_usd")
        yield _row(
            "live_cost_recorded",
            isinstance(cost, int | float) and cost > 0,
            f"cost_spent_usd={cost!r} estimate_usd={run.get('estimate_usd')!r}",
        )
        yield check_artifacts(
            ctx.client("s3"),
            run,
            bucket=ctx.outputs.require_str("eval_artifacts_bucket_name"),
            prefix=ctx.outputs.require_str("eval_artifacts_prefix"),
            arm_count=len(families),
            task_count=task_count,
        )
        yield check_tenant_empty(resolved_store, tenant, check="live_tenant_torn_down")
        yield check_canary_empty(resolved_store)
    finally:
        try:
            yield from _settle_runs(api, resolved_store, started)
        finally:
            yield from admin.cleanup()


# -- entry point ---------------------------------------------------------------------------------


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """The simulation rows and the provisioning row always run on the stub path."""
    simulated = simulation_row_count()
    return ExpectedCheckCount(
        minimum=simulated + 1,
        basis=(
            f"{simulated} simulation rows plus provision_admin_user run on every stub "
            "path; the sweep rows follow once the admin user exists"
        ),
    )


def live_expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """The three pre-checks always run on the live path."""
    return ExpectedCheckCount(
        minimum=3,
        basis="data_retention_read, retention_verdict_matches and lock_absent_before_submit always run",
    )


def _flag_value(args: Sequence[str], flag: str) -> str | None:
    """The value of `flag` given as `flag=value` or `flag value`, or `None` when absent."""
    for index, token in enumerate(args):
        if token.startswith(f"{flag}="):
            return token.split("=", 1)[1]
        if token == flag and index + 1 < len(args) and not args[index + 1].startswith("--"):
            return args[index + 1]
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: the stub flow, or with `--live-once` and the billable flag, one live sweep."""
    args = list(sys.argv[1:] if argv is None else argv)
    verdict = _flag_value(args, RETENTION_VERDICT_FLAG)
    if LIVE_ONCE_FLAG in args:
        if BILLABLE_FLAG not in args:
            sys.stderr.write(
                f"error: {LIVE_ONCE_FLAG} starts one paid live sweep; pass {BILLABLE_FLAG} to "
                "confirm. Nothing was submitted.\n"
            )
            return 2
        return run_main(
            args,
            layer=LAYER,
            resource_class=RESOURCE_CLASS,
            description=__doc__ or RESOURCE_CLASS,
            checks=functools.partial(live_once_checks, retention_verdict=verdict),
            extra_arguments=[LIVE_ONCE_FLAG, BILLABLE_FLAG, RETENTION_VERDICT_FLAG],
            expected=live_expected_check_count,
        )
    for flag in (BILLABLE_FLAG, RETENTION_VERDICT_FLAG):
        if any(token == flag or token.startswith(f"{flag}=") for token in args):
            sys.stderr.write(f"error: {flag} is only meaningful with {LIVE_ONCE_FLAG}.\n")
            return 2
    return run_main(
        args,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
