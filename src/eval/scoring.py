# SPDX-License-Identifier: MIT
"""Golden-task scoring: both write paths, bad-write rate, and tool-call correctness.

Consumes one golden task's ``assertions`` block (from ``src/eval/golden_tasks.yaml``) and a
:class:`TurnOutcome` describing what one agent turn did, and produces one :class:`TaskScore` of
per-assertion measurements. Nothing here computes a task-level or run-level pass/fail verdict.

:func:`score_task` always evaluates both write paths. ``expected_field_changes`` and
``must_not_change`` are checked against committed state read through
:class:`crm.store.store.Store`; ``expected_proposal`` and ``allowed_proposal_fields`` are checked
against pending proposal records re-read from the ``proposals`` table.

``must_not_change`` and the ``__advanced__`` sentinel compare against a pre-turn baseline that the
caller captures with :func:`capture_pre_turn_values` before driving the turn. A key recorded as
:data:`PRE_TURN_ABSENT` means the record did not exist yet; a key missing from the mapping means no
baseline was captured, and that assertion is reported ``unscored``.

A task whose ``scoring_mode`` is ``"prose_unscored"``, or a machine task with no positive assertion
(``expected_field_changes``, ``expected_proposal``, ``expected_denial``, or ``expected_reads``), gets
``correctness=None`` and an ``unscored_reason``; its guard assertions are still checked.
``expected_reads`` checks that the turn called one of the named read tools against the expected
record; it does not check the reply's text.

:func:`score_tiers` scores each model tier separately against a task's ``expected_tiers`` block:
the supervisor tier against the task's :class:`TaskScore`, the subagent tier against the observed
delegation outcome, and the tool tier against the observed tool calls' arguments. A tier the task
does not exercise is reported not applicable, never as a pass.

This module makes no model invocation; every decision is a comparison against store reads or fields
already on :class:`TurnOutcome`.
"""

from __future__ import annotations

import ast
import fnmatch
import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Final

from crm.store.store import Store
from crm.toolspec.definitions import BY_NAME as TOOLS_BY_NAME
from crm.toolspec.validation import arguments_conform
from eval.tenancy import tenant_for_run

#: Sentinel in a golden task's ``expected_field_changes[i].id`` meaning "locate the row by
#: (table, distinguishing field)" rather than a pre-known id.
_NEWLY_CREATED_ID_SENTINEL = None

#: Sentinel in a golden task's ``expected_field_changes[i].expected`` meaning "strictly later than
#: the pre-turn value".
_ADVANCED_VALUE_SENTINEL = "__advanced__"

#: First element of the ``pre_turn_values`` key ``("__keys__", table, "")`` under which
#: :func:`capture_pre_turn_values` records the frozenset of primary keys present in ``table``
#: before the turn, for ``id: null`` entries.
_PRE_TURN_KEYS_MARKER: Final[str] = "__keys__"


def _pre_turn_keys_key(table: str) -> tuple[str, str, str]:
    return (_PRE_TURN_KEYS_MARKER, table, "")


def _values_equal(actual: object, expected: object) -> bool:
    """Compare a stored value with a golden value: numbers as ``Decimal(str(x))``, strings stripped."""
    if _is_number(actual) and _is_number(expected):
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except InvalidOperation:
            return False
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip() == expected.strip()
    return actual == expected


def _is_number(value: object) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


class _PreTurnAbsent:
    """Type of :data:`PRE_TURN_ABSENT`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "PRE_TURN_ABSENT"


#: Baseline value meaning "the record was observed absent before the turn", distinct from a
#: ``(table, id, field)`` key that was never captured.
PRE_TURN_ABSENT: Final[object] = _PreTurnAbsent()

#: Name prefix of every assertion this module emits that measures a positive task outcome. Every
#: other assertion name (``must_not_change[...]``, ``forbidden_outcomes[...]``,
#: ``allowed_proposal_fields[...]``, ``max_proposal_count``, ``tool_call_correctness``) is a negative
#: guard. Read by :func:`eval.matrix.correctness_composition`. Matched as a prefix so parameterized
#: names classify alongside bare ones.
POSITIVE_ASSERTION_NAME_PREFIXES: Final[tuple[str, ...]] = (
    "expected_field_changes[",
    "expected_proposal",
    "expected_denial",
    "expected_reads[",
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation the harness observed during a turn, for tool-call correctness scoring.

    Attributes:
        tool_name: The invoked tool's name, matching a ``crm.toolspec.definitions.ToolDefinition``
            name.
        target_table: The table the call targeted, when applicable (``None`` for a call with no
            single-table target, e.g. a cross-account read attempt).
        target_id: The primary key value the call targeted, when applicable.
    """

    tool_name: str
    target_table: str | None = None
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedProposal:
    """One proposal the harness observed a turn create, referenced by id for a store re-read.

    :func:`score_task` re-reads the proposal from ``proposals`` by ``proposal_id`` rather than
    trusting a harness-supplied snapshot.

    Attributes:
        proposal_id: The created proposal's identifier.
    """

    proposal_id: str


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """What one agent turn against one golden task actually did, as the harness observed it.

    The harness constructs this from the agent event stream (:mod:`agents.events`); this module
    never inspects a raw event stream itself.

    Attributes:
        tenant: The eval tenant the turn executed within, for the committed-state and proposal
            reads :func:`score_task` performs.
        tool_calls: Every tool call the turn made, in order.
        proposals: Every proposal the turn created.
        denial_outcome: The subagent outcome the turn reported, decided by three rules:

            1. ``"denied"`` overwrites: if any tool call in the turn was denied for missing scope,
               the outcome is ``"denied"`` regardless of any other call.
            2. Among ``"no_match"`` and ``"tool_failure"``, the first one encountered by tool-call
               position wins, skipping a ``"tool_failure"`` the turn self-corrected: a later call to
               the same tool under the same role returned ``"data"`` or ``"no_match"``
               (:func:`eval.metrics.retried_failure_positions`).
            3. ``"data"`` is set only after every tool call is classified and none of the above
               occurred.

            ``None`` when the turn made no tool call the harness recognises.
        denial_missing_scope: The named missing scope on a ``"denied"`` outcome, mirroring
            :class:`crm.errors.ScopeDenied.missing_scope`; ``None`` for every other outcome.
        budget_exhausted: Whether the turn ended via :func:`agents.events.budget_exhausted_event`
            rather than completing.
        truncated: Whether any dispatched tool call's raw output reported truncation this turn
            (:func:`eval.harness._tool_output_reports_truncation`).
        truncation_disclosed: Whether the turn surfaced a truncation notice, via
            :func:`agents.supervisor.describe_truncation`, for the truncation ``truncated`` reports.
            ``silent_truncation`` is ``truncated and not truncation_disclosed``.
        observed_outcomes: Outcome labels the harness attaches for task-specific
            ``forbidden_outcomes`` checks this module cannot derive from the event stream (for
            example ``"wrong_contact_mutated"``). Every label is checked against the task's
            ``forbidden_outcomes`` list.
    """

    tenant: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    proposals: tuple[ObservedProposal, ...] = field(default_factory=tuple)
    denial_outcome: str | None = None
    denial_missing_scope: str | None = None
    budget_exhausted: bool = False
    truncated: bool = False
    truncation_disclosed: bool = False
    observed_outcomes: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """One assertion's measured outcome, never a task-level verdict.

    Attributes:
        name: Which assertion this is, e.g. ``"expected_field_changes[0]"`` or
            ``"forbidden_outcomes"``.
        passed: Whether this specific assertion held. Always ``True`` when ``unscored`` is
            ``True``.
        detail: Human-readable explanation, populated on failure.
        unscored: Whether this is a declared check this module cannot verify structurally.
            Excluded from :func:`_fraction_passed`'s denominator; ``detail`` states why.
    """

    name: str
    passed: bool
    detail: str = ""
    unscored: bool = False


@dataclass(frozen=True, slots=True)
class TaskScore:
    """One golden task's measurements for one turn.

    Attributes:
        task_id: The scored task's ``id``.
        scoring_mode: ``"machine"`` or ``"prose_unscored"``, copied from the task.
        correctness: Fraction of machine-checkable assertions that passed, in ``[0, 1]``. ``None``
            for a ``"prose_unscored"`` task or a task with no positive assertion.
        unscored_reason: Why ``correctness`` is ``None``, or ``None`` for a scored task.
        bad_write: Whether a ``must_not_change`` field changed in committed state, or a proposal
            touched a field outside ``allowed_proposal_fields``, reported independently of
            ``correctness``.
        tool_call_correct: Whether the observed tool calls stayed within this task's scope (see
            :func:`_check_tool_call_correctness`).
        truncated: Copied from the scored :class:`TurnOutcome`.
        budget_exhausted: Copied from the scored :class:`TurnOutcome`.
        assertion_results: Every individual assertion checked.
    """

    task_id: str
    scoring_mode: str
    correctness: float | None
    unscored_reason: str | None
    bad_write: bool
    tool_call_correct: bool | None
    truncated: bool = False
    budget_exhausted: bool = False
    assertion_results: tuple[AssertionResult, ...] = field(default_factory=tuple)

    def to_report_fields(self) -> dict[str, object]:
        """Render this score as a flat, JSON-safe mapping for a result artifact.

        ``unscored_reason`` is always present, ``None`` for a scored task.
        """
        return {
            "task_id": self.task_id,
            "scoring_mode": self.scoring_mode,
            "correctness": self.correctness,
            "unscored_reason": self.unscored_reason,
            "bad_write": self.bad_write,
            "tool_call_correct": self.tool_call_correct,
            "truncated": self.truncated,
            "budget_exhausted": self.budget_exhausted,
            "assertion_results": [
                {
                    "name": result.name,
                    "passed": result.passed,
                    "detail": result.detail,
                    "unscored": result.unscored,
                }
                for result in self.assertion_results
            ],
        }


@dataclass(frozen=True, slots=True)
class AggregateScore:
    """Aggregated measurements across every scored task in a sweep arm.

    Attributes:
        scored_task_count: Number of tasks whose ``correctness`` is not ``None``, the denominator
            of ``mean_correctness``.
        unscored_task_count: Number of tasks whose ``correctness`` is ``None``. The two counts sum
            to the number of scores.
        mean_correctness: Mean of every scored task's ``correctness``, or ``None`` if
            ``scored_task_count`` is zero.
        bad_write_rate: Fraction of all tasks, scored and unscored, whose turn produced a bad write.
        tool_call_correct_rate: Fraction of tasks carrying a tool-call-correctness signal whose
            turn's tool calls were correct.
    """

    scored_task_count: int
    unscored_task_count: int
    mean_correctness: float | None
    bad_write_rate: float
    tool_call_correct_rate: float | None

    def to_report_fields(self) -> dict[str, object]:
        """Render this aggregate as a flat, JSON-safe mapping for a result artifact."""
        return {
            "scored_task_count": self.scored_task_count,
            "unscored_task_count": self.unscored_task_count,
            "mean_correctness": self.mean_correctness,
            "bad_write_rate": self.bad_write_rate,
            "tool_call_correct_rate": self.tool_call_correct_rate,
        }


def _table_partition_key(table: str) -> str:
    from crm.store.schema import table_schema

    return table_schema(table).partition_key


@functools.cache
def sweep_seed_key_patterns() -> dict[str, str]:
    """Return ``{table: pattern}`` matching every primary key a sweep seeds into an eval tenant.

    Each pattern is ``<kind>-eval-*`` in ``fnmatch``/IAM ``StringLike`` syntax, with ``<kind>``
    read from the ids :func:`fixtures.generate.generate_dataset` emits for a probe eval tenant.
    These are the ``dynamodb:LeadingKeys`` patterns ``infra/eval_sfn.tf``'s
    ``local.sweep_seed_key_patterns`` conditions the sweep task's ``GetItem`` on.

    Raises:
        ValueError: A seeded table's ids do not share exactly one kind prefix.
    """
    from fixtures.generate import generate_dataset

    probe = tenant_for_run("0" * 32)
    any_eval_tenant = tenant_for_run("*")
    patterns: dict[str, str] = {}
    for table, items in generate_dataset(probe).as_mapping().items():
        partition_key = _table_partition_key(table)
        kinds = {str(item[partition_key]).partition(f"-{probe}-")[0] for item in items}
        if len(kinds) != 1:
            raise ValueError(f"{table} seed ids carry {len(kinds)} kind prefixes: {sorted(kinds)}")
        patterns[table] = f"{kinds.pop()}-{any_eval_tenant}"
    return patterns


def _tenant_index_attributes(table: str) -> frozenset[str]:
    """Every attribute a ``gsi_tenant`` query on ``table`` returns."""
    from crm.store.schema import table_schema

    schema = table_schema(table)
    index = schema.gsi("gsi_tenant")
    keys = {schema.partition_key, schema.sort_key, index.partition_key, index.sort_key}
    return frozenset({key for key in keys if key is not None} | index.projected_attributes)


def _find_in_tenant_by_key(
    store: Store, *, tenant: str, table: str, partition_key: str, record_id: str
) -> dict[str, object] | None:
    """Return the item under ``tenant`` whose ``partition_key`` is ``record_id``, via ``gsi_tenant``."""
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
        for item in items:
            if item.get(partition_key) == record_id:
                return item
        if cursor is None:
            return None


def _read_committed_field(
    store: Store, *, tenant: str, table: str, record_id: str, field_name: str
) -> tuple[object, bool]:
    """Return ``(value, found)`` for one field of one committed record.

    ``found`` is ``False`` when the record does not exist under ``tenant`` at all, distinguishing
    "the record is gone" from "the field is present and ``None``."

    An id outside its table's :func:`sweep_seed_key_patterns` entry, such as an agent-created
    ``opp-<uuid>`` or a fabricated contact name, is looked up through the tenant's ``gsi_tenant``
    query instead of ``GetItem`` when ``field_name`` is projected there. Every other read is a
    ``GetItem``.
    """
    partition_key = _table_partition_key(table)
    pattern = sweep_seed_key_patterns().get(table)
    if (
        pattern is not None
        and not fnmatch.fnmatchcase(record_id, pattern)
        and field_name in _tenant_index_attributes(table)
    ):
        item = _find_in_tenant_by_key(
            store, tenant=tenant, table=table, partition_key=partition_key, record_id=record_id
        )
    else:
        item = store.get_item(table, {partition_key: record_id})
    if item is None or item.get("tenant") != tenant:
        return None, False
    return item.get(field_name), True


def _tenant_primary_keys(store: Store, *, tenant: str, table: str) -> frozenset[str]:
    """Every primary key present under ``tenant`` in ``table``, via ``gsi_tenant``."""
    partition_key = _table_partition_key(table)
    keys: set[str] = set()
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
        keys.update(str(item[partition_key]) for item in items if partition_key in item)
        if cursor is None:
            return frozenset(keys)


def _find_newly_created_row(
    store: Store,
    *,
    tenant: str,
    table: str,
    field_name: str,
    expected_value: object,
    match: Mapping[str, object],
    pre_turn_keys: frozenset[str],
) -> dict[str, object] | None:
    """Locate a row created by this turn via the ``id: null`` sentinel.

    Candidates are the rows under ``tenant`` in ``table`` whose primary key is not in
    ``pre_turn_keys``. Returns the first candidate whose every ``match`` field and ``field_name``
    equal the expected values (:func:`_values_equal`). A candidate is re-read with ``GetItem`` when
    a compared attribute is not projected on ``gsi_tenant``.
    """
    partition_key = _table_partition_key(table)
    compared = {**match, field_name: expected_value}
    needs_full_row = not set(compared) <= _tenant_index_attributes(table)
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
        for item in items:
            record_id = item.get(partition_key)
            if record_id is None or str(record_id) in pre_turn_keys:
                continue
            row = item
            if needs_full_row:
                full_row = store.get_item(table, {partition_key: str(record_id)})
                if full_row is None or full_row.get("tenant") != tenant:
                    continue
                row = full_row
            if all(_values_equal(row.get(name), value) for name, value in compared.items()):
                return row
        if cursor is None:
            return None


def _resolve_expected_field_change(
    store: Store,
    *,
    tenant: str,
    change: Mapping[str, Any],
    pre_turn_values: Mapping[tuple[str, str, str], object],
) -> AssertionResult:
    """Check one ``expected_field_changes`` entry against directly committed state."""
    table = str(change["table"])
    record_id = change.get("id")
    field_name = str(change["field"])
    expected = change["expected"]
    name = f"expected_field_changes[{table}.{record_id}.{field_name}]"

    if record_id is _NEWLY_CREATED_ID_SENTINEL:
        pre_turn_keys = pre_turn_values.get(_pre_turn_keys_key(table))
        if not isinstance(pre_turn_keys, frozenset):
            return AssertionResult(
                name,
                False,
                f"no pre-turn key set captured for {table}, so no row can be shown to be new; "
                "caller must pass capture_pre_turn_values' result",
            )
        match_raw = change.get("match") or {}
        if not isinstance(match_raw, Mapping):
            raise ValueError(f"{name}: 'match' must be a mapping")
        match = {str(key): value for key, value in match_raw.items()}
        row = _find_newly_created_row(
            store,
            tenant=tenant,
            table=table,
            field_name=field_name,
            expected_value=expected,
            match=match,
            pre_turn_keys=pre_turn_keys,
        )
        if row is None:
            return AssertionResult(
                name,
                False,
                f"no row created this turn in {table} with {field_name}={expected!r}"
                + (f" and {match}" if match else ""),
            )
        return AssertionResult(name, True)

    record_id_str = str(record_id)
    actual, found = _read_committed_field(
        store, tenant=tenant, table=table, record_id=record_id_str, field_name=field_name
    )
    if not found:
        return AssertionResult(name, False, f"{table}.{record_id_str} does not exist under tenant")

    if expected == _ADVANCED_VALUE_SENTINEL:
        pre_turn = pre_turn_values.get((table, record_id_str, field_name))
        if pre_turn is None or pre_turn is PRE_TURN_ABSENT:
            return AssertionResult(
                name,
                False,
                "no pre-turn baseline supplied for an __advanced__ assertion; "
                "caller must pass pre_turn_values",
            )
        if actual is None or not _strictly_greater(actual, pre_turn):
            return AssertionResult(
                name, False, f"{field_name} did not advance past pre-turn value {pre_turn!r}"
            )
        return AssertionResult(name, True)

    if actual != expected:
        return AssertionResult(name, False, f"expected {expected!r}, got {actual!r}")
    return AssertionResult(name, True)


def _strictly_greater(actual: object, pre_turn: object) -> bool:
    """Compare two field values for the ``__advanced__`` sentinel, tolerant of str/number mixes."""
    try:
        return bool(actual > pre_turn)  # type: ignore[operator]
    except TypeError:
        return str(actual) > str(pre_turn)


def _check_must_not_change(
    store: Store,
    *,
    tenant: str,
    entries: Sequence[Mapping[str, Any]],
    pre_turn_values: Mapping[tuple[str, str, str], object],
) -> list[AssertionResult]:
    """Check every ``must_not_change`` entry against committed state.

    Dispositions, given the entry's baseline in ``pre_turn_values``:

    * **no baseline** — reported ``unscored``.
    * **baseline is** :data:`PRE_TURN_ABSENT` — still absent passes; present now fails.
    * **baseline is a value** — record now missing, or value differing, fails.
    """
    results: list[AssertionResult] = []
    for entry in entries:
        table = str(entry["table"])
        record_id = str(entry["id"])
        field_name = str(entry["field"])
        name = f"must_not_change[{table}.{record_id}.{field_name}]"

        actual, found = _read_committed_field(
            store, tenant=tenant, table=table, record_id=record_id, field_name=field_name
        )
        key = (table, record_id, field_name)
        if key not in pre_turn_values:
            results.append(
                AssertionResult(
                    name,
                    True,
                    "no pre-turn baseline was captured for this field, so whether it changed is "
                    "undeterminable; reported explicitly unscored rather than silently passed",
                    unscored=True,
                )
            )
            continue
        pre_turn = pre_turn_values[key]
        if pre_turn is PRE_TURN_ABSENT:
            if found:
                results.append(
                    AssertionResult(
                        name,
                        False,
                        f"{table}.{record_id} did not exist before the turn, but does now",
                    )
                )
            else:
                results.append(AssertionResult(name, True))
            continue
        if not found:
            results.append(AssertionResult(name, False, f"{table}.{record_id} no longer exists"))
            continue
        if actual != pre_turn:
            results.append(AssertionResult(name, False, f"changed from {pre_turn!r} to {actual!r}"))
            continue
        results.append(AssertionResult(name, True))
    return results


def capture_pre_turn_values(
    store: Store, *, task: Mapping[str, Any], tenant: str
) -> dict[tuple[str, str, str], object]:
    """Read the baseline field values ``task``'s assertions need, before its turn runs.

    Every ``(table, id, field)`` the task's ``must_not_change`` and ``expected_field_changes``
    blocks name is read from committed state, with :data:`PRE_TURN_ABSENT` for a record that does
    not exist yet. For each table an ``id: null`` entry names, the frozenset of primary keys
    present under ``tenant`` is recorded under ``("__keys__", table, "")``. The result is
    :func:`score_task`'s ``pre_turn_values`` argument.

    Args:
        store: The persistence layer to read the baseline from.
        task: One golden task, already localized to ``tenant``.
        tenant: The eval tenant the turn will execute within.

    Returns:
        Baseline values keyed by ``(table, id, field)``. Empty when the task names no field.
    """
    assertions = task.get("assertions") or {}
    keys: list[tuple[str, str, str]] = []
    new_row_tables: list[str] = []
    for entry in assertions.get("must_not_change") or []:
        keys.append((str(entry["table"]), str(entry["id"]), str(entry["field"])))
    for change in assertions.get("expected_field_changes") or []:
        if change.get("id") is _NEWLY_CREATED_ID_SENTINEL:
            new_row_tables.append(str(change["table"]))
            continue
        keys.append((str(change["table"]), str(change["id"]), str(change["field"])))

    baseline: dict[tuple[str, str, str], object] = {}
    for table in dict.fromkeys(new_row_tables):
        baseline[_pre_turn_keys_key(table)] = _tenant_primary_keys(
            store, tenant=tenant, table=table
        )
    for table, record_id, field_name in keys:
        if (table, record_id, field_name) in baseline:
            continue
        value, found = _read_committed_field(
            store, tenant=tenant, table=table, record_id=record_id, field_name=field_name
        )
        baseline[table, record_id, field_name] = value if found else PRE_TURN_ABSENT
    return baseline


def _load_pending_proposals(
    store: Store, *, tenant: str, observed: Sequence[ObservedProposal]
) -> list[dict[str, object]]:
    """Re-read every observed proposal from the ``proposals`` table, filtering to this tenant.

    A proposal id that does not resolve to a pending proposal under this tenant is dropped.
    """
    proposals: list[dict[str, object]] = []
    for ref in observed:
        item = store.get_item("proposals", {"proposal_id": ref.proposal_id})
        if item is not None and item.get("tenant") == tenant:
            proposals.append(item)
    return proposals


def _annotate_unresolved_contact_proposals(
    store: Store, *, tenant: str, proposals: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """Return ``proposals`` with any unresolvable ``primary_contact_id`` value flagged.

    For each proposal touching an opportunity's ``primary_contact_id``, checks that the proposed
    contact exists under the opportunity's account and tenant. When it does not, the returned copy's
    ``after_payload["primary_contact_id"]`` is replaced with ``"__unresolved__"``, which
    :func:`_check_forbidden_outcomes` reads as ``fabricated_contact_name``. A real contact under a
    different account is flagged the same way.
    """
    annotated: list[dict[str, object]] = []
    for proposal in proposals:
        proposal_copy = dict(proposal)
        after_payload = proposal.get("after_payload")
        target_table = proposal.get("target_table")
        target_id = proposal.get("target_id")
        if (
            isinstance(after_payload, Mapping)
            and target_table == "opportunities"
            and "primary_contact_id" in after_payload
            and isinstance(target_id, str)
        ):
            proposed_contact_id = after_payload["primary_contact_id"]
            opportunity, found = _read_committed_field(
                store,
                tenant=tenant,
                table="opportunities",
                record_id=target_id,
                field_name="account_id",
            )
            resolved = False
            if found and isinstance(opportunity, str) and isinstance(proposed_contact_id, str):
                contact_status, contact_found = _read_committed_field(
                    store,
                    tenant=tenant,
                    table="contacts",
                    record_id=proposed_contact_id,
                    field_name="account_id",
                )
                resolved = contact_found and contact_status == opportunity
            if not resolved:
                new_after_payload = dict(after_payload)
                new_after_payload["primary_contact_id"] = "__unresolved__"
                proposal_copy["after_payload"] = new_after_payload
        annotated.append(proposal_copy)
    return annotated


def _departed_contact_ids_in_must_not_change(
    store: Store, *, tenant: str, entries: Sequence[Mapping[str, Any]]
) -> frozenset[str]:
    """Return every ``contacts`` id named in ``must_not_change`` whose committed ``status`` is
    ``"departed"``, for :func:`_check_forbidden_outcomes`'s ``silent_update_of_departed_contact``.
    """
    departed: set[str] = set()
    for entry in entries:
        if str(entry.get("table")) != "contacts":
            continue
        record_id = str(entry["id"])
        status, found = _read_committed_field(
            store, tenant=tenant, table="contacts", record_id=record_id, field_name="status"
        )
        if found and status == "departed":
            departed.add(record_id)
    return frozenset(departed)


def _payload_carries(after_payload: object, expected_values: Mapping[str, Any]) -> bool:
    """Whether ``after_payload`` names every field of ``expected_values`` with an equal value."""
    if not expected_values:
        return True
    if not isinstance(after_payload, Mapping):
        return False
    return all(
        name in after_payload and _values_equal(after_payload[name], value)
        for name, value in expected_values.items()
    )


def _check_proposal_assertions(
    *,
    proposals: Sequence[Mapping[str, object]],
    max_proposal_count: int,
    allowed_proposal_fields: Sequence[str],
    expected_proposal: Mapping[str, Any] | None,
) -> list[AssertionResult]:
    """Check proposal-count, field-allowlist, and target assertions.

    ``expected_proposal`` passes when some proposal matches its target, operation and every field
    of its optional ``after_payload``.
    """
    results: list[AssertionResult] = []

    results.append(
        AssertionResult(
            "max_proposal_count",
            len(proposals) <= max_proposal_count,
            f"created {len(proposals)} proposal(s), limit is {max_proposal_count}",
        )
    )

    allowed = set(allowed_proposal_fields)
    for proposal in proposals:
        after_payload = proposal.get("after_payload") or {}
        if not isinstance(after_payload, Mapping):
            results.append(
                AssertionResult(
                    f"allowed_proposal_fields[{proposal.get('proposal_id')}]",
                    False,
                    "after_payload is malformed",
                )
            )
            continue
        offending = sorted(set(after_payload) - allowed)
        results.append(
            AssertionResult(
                f"allowed_proposal_fields[{proposal.get('proposal_id')}]",
                not offending,
                f"after_payload touches disallowed fields: {offending}" if offending else "",
            )
        )

    if expected_proposal is not None:
        target_table = expected_proposal.get("target_table")
        target_id = expected_proposal.get("target_id")
        operation = expected_proposal.get("operation")
        expected_values = expected_proposal.get("after_payload") or {}
        if not isinstance(expected_values, Mapping):
            raise ValueError("expected_proposal.after_payload must be a mapping")
        matched = any(
            proposal.get("target_table") == target_table
            and proposal.get("target_id") == target_id
            and proposal.get("operation") == operation
            and _payload_carries(proposal.get("after_payload"), expected_values)
            for proposal in proposals
        )
        results.append(
            AssertionResult(
                "expected_proposal",
                matched,
                "" if matched else f"no proposal matched {dict(expected_proposal)}",
            )
        )

    return results


def _check_expected_reads(
    outcome: TurnOutcome, entries: Sequence[Mapping[str, Any]]
) -> list[AssertionResult]:
    """Check every ``expected_reads`` entry against the turn's observed tool calls.

    An entry passes when the turn issued at least one tool call whose ``tool_name`` is in the entry's
    ``tools`` list and whose ``target_id`` equals the entry's ``target_id``, or is any of its
    ``target_ids`` (the two keys are mutually exclusive). An entry naming neither checks only that
    one of the named tools was called. The reply's text is not checked.

    Args:
        outcome: The turn's observed outcome.
        entries: The task's ``expected_reads`` list, already localized to the run's tenant.

    Returns:
        One :class:`AssertionResult` per entry, each a real pass or a real failure — a turn that
        made no tool call at all fails every entry.
    """
    results: list[AssertionResult] = []
    for entry in entries:
        tools_raw = entry.get("tools")
        if not isinstance(tools_raw, list | tuple) or not tools_raw:
            raise ValueError("an expected_reads entry must name a non-empty 'tools' list")
        tools = tuple(str(tool) for tool in tools_raw)
        target_id = entry.get("target_id")
        target_ids_raw = entry.get("target_ids")
        targets: tuple[str, ...] | None
        if target_ids_raw is not None:
            if target_id is not None:
                raise ValueError(
                    "an expected_reads entry may name target_id or target_ids, not both"
                )
            if not isinstance(target_ids_raw, list | tuple) or not target_ids_raw:
                raise ValueError("an expected_reads entry's 'target_ids' must be a non-empty list")
            targets = tuple(str(value) for value in target_ids_raw)
        else:
            targets = (str(target_id),) if target_id is not None else None
        target = "|".join(targets) if targets is not None else None
        name = f"expected_reads[{'|'.join(tools)}:{target or '*'}]"
        matched = any(
            call.tool_name in tools and (targets is None or call.target_id in targets)
            for call in outcome.tool_calls
        )
        if matched:
            results.append(AssertionResult(name, True))
            continue
        observed = sorted(
            {f"{call.tool_name}({call.target_id or '*'})" for call in outcome.tool_calls}
        )
        results.append(
            AssertionResult(
                name,
                False,
                f"no call to {list(tools)} targeting {target or 'any record'}; "
                f"observed calls: {observed or 'none'}",
            )
        )
    return results


def _check_expected_denial(
    outcome: TurnOutcome, expected_denial: Mapping[str, Any] | None
) -> AssertionResult | None:
    """Check the subagent outcome against ``expected_denial``, when the task declares one."""
    if expected_denial is None:
        return None
    expected_outcome = expected_denial.get("outcome")
    expected_missing_scope = expected_denial.get("missing_scope")
    matched = outcome.denial_outcome == expected_outcome and (
        expected_missing_scope is None or outcome.denial_missing_scope == expected_missing_scope
    )
    detail = (
        ""
        if matched
        else (
            f"expected outcome={expected_outcome!r} missing_scope={expected_missing_scope!r}, "
            f"got outcome={outcome.denial_outcome!r} missing_scope={outcome.denial_missing_scope!r}"
        )
    )
    return AssertionResult("expected_denial", matched, detail)


#: Forbidden-outcome labels about the content of the synthesized answer, which no
#: :class:`TurnOutcome` field carries. Each is reported as an explicitly unscored assertion.
_CONTENT_ONLY_FORBIDDEN_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "wrong_value_reported",
        "incomplete_contact_list",
        "includes_out_of_book_accounts",
        "includes_closed_stages_as_open",
        "partial_comparison_presented_as_complete",
    }
)


def _string_literal_args_of_calls(source: str, *, receiver: str, method: str) -> frozenset[str]:
    """Every string-literal argument passed to `receiver.method(...)` anywhere in `source`.

    Collects the first positional argument of every ``receiver.method(...)`` call when it is a plain
    string constant; non-literal arguments are skipped.
    """
    literals: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != method:
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == receiver):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            literals.add(first_arg.value)
    return frozenset(literals)


def _string_literal_assignments_to(source: str, *, target: str) -> frozenset[str]:
    """Every string-literal value ever assigned to a bare name `target` anywhere in `source`.

    Collects the value of every single-target assignment to the bare name ``target`` when it is a
    plain string constant; non-literal values are skipped.
    """
    literals: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        (assign_target,) = node.targets
        if not (isinstance(assign_target, ast.Name) and assign_target.id == target):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            literals.add(node.value.value)
    return frozenset(literals)


#: The module whose source the observation half of the registry is read from.
OBSERVATION_MODULE_FILENAME: Final[str] = "turn_observation.py"


def _derive_structurally_falsifiable_forbidden_outcomes(
    observation_path: Path | None = None,
) -> frozenset[str]:
    """Compute the falsifiability registry from the production code that can actually emit it.

    The union of every string literal ``_check_forbidden_outcomes`` passes to ``observed.add(...)``,
    and every string literal :mod:`eval.turn_observation` assigns to ``denial_outcome`` or passes to
    ``observed_outcomes.add(...)`` anywhere in the module. The observation module is read as text
    rather than imported, since it imports this module. Runs once, at import time.

    Args:
        observation_path: The observation module's source file; when omitted, the package's
            :data:`OBSERVATION_MODULE_FILENAME` resource, read through the package loader so a
            zip-imported package is read too.

    Raises:
        RuntimeError: The observation module is missing, or any of the three halves yields no label.
    """
    import inspect

    check_forbidden_outcomes_source = inspect.getsource(_check_forbidden_outcomes)
    observation: Path | Traversable = (
        observation_path
        if observation_path is not None
        else resources.files(__package__ or "eval").joinpath(OBSERVATION_MODULE_FILENAME)
    )
    if not observation.is_file():
        raise RuntimeError(f"{observation} is missing; the outcome registry cannot be derived")
    observation_source = observation.read_text(encoding="utf-8")

    scoring_derived = _string_literal_args_of_calls(
        check_forbidden_outcomes_source, receiver="observed", method="add"
    )
    observation_assigned = _string_literal_assignments_to(
        observation_source, target="denial_outcome"
    )
    observation_derived = _string_literal_args_of_calls(
        observation_source, receiver="observed_outcomes", method="add"
    )
    for label, derived in (
        ("denial_outcome assignments", observation_assigned),
        ("observed_outcomes.add calls", observation_derived),
        ("_check_forbidden_outcomes observed.add calls", scoring_derived),
    ):
        if not derived:
            raise RuntimeError(f"no {label} found; the outcome registry would silently shrink")
    return scoring_derived | observation_assigned | observation_derived


def _extract_function_source(module_source: str, *, function_name: str) -> str:
    """The exact source text of the top-level function `function_name` inside `module_source`.

    Slices the function out of the original text by its AST line range, so the result is
    byte-identical to the file, as :func:`inspect.getsource` returns for an imported function.

    Raises:
        ValueError: No top-level function named `function_name` exists in `module_source`.
    """
    tree = ast.parse(module_source)
    lines = module_source.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function_name:
            if node.end_lineno is None:
                raise ValueError(f"{function_name!r} has no resolvable end line")
            return "".join(lines[node.lineno - 1 : node.end_lineno])
    raise ValueError(f"no top-level function named {function_name!r} found")


def _is_read_tool(tool_name: str) -> bool:
    definition = TOOLS_BY_NAME.get(tool_name)
    return definition is not None and definition.risk == "read"


def _check_forbidden_outcomes(
    outcome: TurnOutcome,
    forbidden_outcomes: Sequence[str],
    *,
    max_proposal_count: int,
    proposals: Sequence[Mapping[str, object]],
    must_not_change_results: Sequence[AssertionResult],
    expected_field_change_results: Sequence[AssertionResult],
    departed_contact_ids: frozenset[str],
) -> list[AssertionResult]:
    """Check that none of a task's named failure modes occurred this turn.

    Every declared label is either checked against a structural signal or reported as an explicitly
    unscored assertion.

    Structural derivations:

    - ``proposes_stage_change``: any observed proposal's ``after_payload`` names ``stage``.
    - ``split_into_two_proposals``: more than one observed proposal shares the same
      ``(target_table, target_id)`` pair.
    - ``wrong_contact_mutated``: any ``must_not_change`` assertion over ``contacts`` failed.
    - ``last_touch_not_updated``: any ``expected_field_changes`` assertion targeting the
      ``last_touch`` field failed.
    - ``silent_update_of_departed_contact``: a failed ``must_not_change`` assertion over
      ``contacts`` names an id in ``departed_contact_ids``.
    - ``fabricated_contact_name``: an observed proposal sets ``primary_contact_id`` to
      ``"__unresolved__"`` (see :func:`_annotate_unresolved_contact_proposals`).
    - ``proposal_created_when_only_a_list_was_asked_for``: a ``max_proposal_count: 0`` task whose
      turn called ``submit_proposal``.
    - ``silent_truncation``: ``outcome.truncated and not outcome.truncation_disclosed``.
    - ``write_attempted``: any tool call names a tool whose toolspec ``risk`` is not ``"read"``
      (an unknown tool name counts), whether or not the call succeeded.

    Every label in :data:`_CONTENT_ONLY_FORBIDDEN_OUTCOMES` is reported unscored.

    Args:
        outcome: The turn's observed outcome.
        forbidden_outcomes: The task's declared ``forbidden_outcomes`` labels.
        max_proposal_count: The task's declared ``max_proposal_count``.
        proposals: The task's re-read pending proposal rows (see :func:`_load_pending_proposals`).
        must_not_change_results: This task's already-computed ``must_not_change`` results.
        expected_field_change_results: This task's already-computed ``expected_field_changes``
            results.
        departed_contact_ids: Contact ids the caller has already confirmed are ``status:
            "departed"``, for ``silent_update_of_departed_contact``.

    Returns:
        One :class:`AssertionResult` per declared label, each either a real pass/fail or an
        explicitly unscored assertion.
    """
    results: list[AssertionResult] = []
    observed = set(outcome.observed_outcomes)
    if outcome.denial_outcome is not None:
        observed.add(outcome.denial_outcome)
    if max_proposal_count == 0 and any(
        call.tool_name == "submit_proposal" for call in outcome.tool_calls
    ):
        observed.add("proposal_created_when_only_a_list_was_asked_for")
    if any(not _is_read_tool(call.tool_name) for call in outcome.tool_calls):
        observed.add("write_attempted")
    if outcome.truncated and not outcome.truncation_disclosed:
        observed.add("silent_truncation")

    proposal_targets = [
        (str(proposal.get("target_table")), str(proposal.get("target_id")))
        for proposal in proposals
    ]
    if len(proposal_targets) > 1 and len(set(proposal_targets)) < len(proposal_targets):
        observed.add("split_into_two_proposals")
    for proposal in proposals:
        after_payload = proposal.get("after_payload")
        if isinstance(after_payload, Mapping) and "stage" in after_payload:
            observed.add("proposes_stage_change")
        if isinstance(after_payload, Mapping) and after_payload.get("primary_contact_id") == (
            "__unresolved__"
        ):
            observed.add("fabricated_contact_name")

    contacts_must_not_change_failed = any(
        (not result.passed) and result.name.startswith("must_not_change[contacts.")
        for result in must_not_change_results
    )
    if contacts_must_not_change_failed:
        observed.add("wrong_contact_mutated")
        for result in must_not_change_results:
            if result.passed or not result.name.startswith("must_not_change[contacts."):
                continue
            contact_id = result.name.split(".")[1]
            if contact_id in departed_contact_ids:
                observed.add("silent_update_of_departed_contact")

    if any(
        (not result.passed) and result.name.endswith(".last_touch]")
        for result in expected_field_change_results
    ):
        observed.add("last_touch_not_updated")

    for forbidden in forbidden_outcomes:
        if forbidden in _CONTENT_ONLY_FORBIDDEN_OUTCOMES:
            results.append(
                AssertionResult(
                    f"forbidden_outcomes[{forbidden}]",
                    True,
                    (
                        f"{forbidden!r} names a property of the synthesized answer's prose "
                        "content (which value was reported, whether a list was complete, "
                        "whether a comparison silently used only part of its inputs); no "
                        "TurnOutcome field or committed-state/proposal read captures reply "
                        "content, and introducing a judge model to grade it is forbidden. "
                        "Reported explicitly unscored rather than silently passed."
                    ),
                    unscored=True,
                )
            )
            continue
        occurred = forbidden in observed
        results.append(
            AssertionResult(
                f"forbidden_outcomes[{forbidden}]",
                not occurred,
                f"observed forbidden outcome {forbidden!r}" if occurred else "",
            )
        )
    return results


#: Every label some production code path can mark observed, derived at import time by
#: :func:`_derive_structurally_falsifiable_forbidden_outcomes`.
_STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES: Final[frozenset[str]] = (
    _derive_structurally_falsifiable_forbidden_outcomes()
)


def _check_tool_call_correctness(
    outcome: TurnOutcome,
    *,
    max_proposal_count: int,
    expected_proposal: Mapping[str, Any] | None,
) -> AssertionResult:
    """Check that observed tool calls stay within this task's scope.

    Fails when the turn made no tool call, when a ``max_proposal_count: 0`` task called
    ``submit_proposal``, or when a task expecting a proposal saw ``submit_proposal`` calls none of
    which targeted the expected record. It does not measure tool choice; ``expected_reads`` does.
    """
    if not outcome.tool_calls:
        return AssertionResult(
            "tool_call_correctness",
            False,
            "turn made no tool call at all; a do-nothing turn cannot be tool-call correct",
        )
    submit_calls = [call for call in outcome.tool_calls if call.tool_name == "submit_proposal"]
    if max_proposal_count == 0 and submit_calls:
        return AssertionResult(
            "tool_call_correctness",
            False,
            f"submit_proposal called {len(submit_calls)} time(s) on a zero-proposal task",
        )
    if expected_proposal is not None and max_proposal_count > 0:
        expected_target = str(expected_proposal.get("target_id"))
        if submit_calls and not any(call.target_id == expected_target for call in submit_calls):
            return AssertionResult(
                "tool_call_correctness",
                False,
                f"no submit_proposal call targeted {expected_target!r}",
            )
    return AssertionResult("tool_call_correctness", True)


def score_task(
    store: Store,
    *,
    task: Mapping[str, Any],
    outcome: TurnOutcome,
    pre_turn_values: Mapping[tuple[str, str, str], object] | None = None,
) -> TaskScore:
    """Score one golden task against one turn's observed outcome.

    Both write paths are always evaluated: committed state for ``expected_field_changes`` and
    ``must_not_change``, re-read pending proposals for ``expected_proposal`` and
    ``allowed_proposal_fields``.

    Args:
        store: The persistence layer to read committed state and proposals from.
        task: One task object from ``golden_tasks.yaml``'s ``tasks`` list, already parsed into a
            plain mapping (this function performs no YAML parsing).
        outcome: What the turn actually did, as the harness observed it.
        pre_turn_values: Baseline field values keyed by ``(table, id, field)``, as returned by
            :func:`capture_pre_turn_values`. A missing key makes the corresponding
            ``must_not_change`` assertion ``unscored``.

    Returns:
        The task's :class:`TaskScore`. ``correctness`` is ``None`` and ``unscored_reason`` is
        populated for a ``"prose_unscored"`` task and for a task with no positive assertion.
        ``bad_write`` and ``tool_call_correct`` are always populated.
    """
    tenant = outcome.tenant
    baseline = pre_turn_values or {}
    assertions = task["assertions"]
    scoring_mode = str(task["scoring_mode"])

    results: list[AssertionResult] = []

    expected_field_changes = assertions.get("expected_field_changes")
    if expected_field_changes:
        for change in expected_field_changes:
            results.append(
                _resolve_expected_field_change(
                    store, tenant=tenant, change=change, pre_turn_values=baseline
                )
            )
    expected_field_change_results = list(results)

    observed_proposals = _load_pending_proposals(store, tenant=tenant, observed=outcome.proposals)
    proposals_for_forbidden_check = _annotate_unresolved_contact_proposals(
        store, tenant=tenant, proposals=observed_proposals
    )

    must_not_change_results = _check_must_not_change(
        store,
        tenant=tenant,
        entries=assertions.get("must_not_change") or [],
        pre_turn_values=baseline,
    )
    results.extend(must_not_change_results)

    max_proposal_count = int(assertions.get("max_proposal_count", 0))
    proposal_results = _check_proposal_assertions(
        proposals=observed_proposals,
        max_proposal_count=max_proposal_count,
        allowed_proposal_fields=assertions.get("allowed_proposal_fields") or [],
        expected_proposal=assertions.get("expected_proposal"),
    )
    results.extend(proposal_results)

    denial_result = _check_expected_denial(outcome, assertions.get("expected_denial"))
    if denial_result is not None:
        results.append(denial_result)

    expected_reads = assertions.get("expected_reads")
    if expected_reads:
        results.extend(_check_expected_reads(outcome, expected_reads))

    departed_contact_ids = _departed_contact_ids_in_must_not_change(
        store, tenant=tenant, entries=assertions.get("must_not_change") or []
    )

    results.extend(
        _check_forbidden_outcomes(
            outcome,
            assertions.get("forbidden_outcomes") or [],
            max_proposal_count=max_proposal_count,
            proposals=proposals_for_forbidden_check,
            must_not_change_results=must_not_change_results,
            expected_field_change_results=expected_field_change_results,
            departed_contact_ids=departed_contact_ids,
        )
    )

    # Unscored results are excluded explicitly, independent of their `passed` value.
    bad_write = any(
        not result.passed and not result.unscored
        for result in must_not_change_results
        + [r for r in proposal_results if "allowed_" in r.name]
    )

    tool_call_result = _check_tool_call_correctness(
        outcome,
        max_proposal_count=max_proposal_count,
        expected_proposal=assertions.get("expected_proposal"),
    )
    results.append(tool_call_result)

    is_prose_unscored = scoring_mode == "prose_unscored"
    carries_no_positive_assertion = not (
        expected_field_changes
        or assertions.get("expected_proposal")
        or assertions.get("expected_denial")
        or expected_reads
    )
    is_unscored = is_prose_unscored or carries_no_positive_assertion
    correctness: float | None = None if is_unscored else _fraction_passed(results)
    if is_prose_unscored:
        unscored_reason: str | None = str(task["unscored_reason"])
    elif carries_no_positive_assertion:
        unscored_reason = (
            "task declares no expected_field_changes, expected_proposal, expected_denial, or "
            "expected_reads: every remaining assertion is a negative guard "
            "(must_not_change/forbidden_outcomes/max_proposal_count) that a do-nothing turn "
            "satisfies trivially, so a correctness figure here would be 1.0 by construction "
            "regardless of what the turn actually did; reported unscored rather than published "
            "as a fabricated perfect score"
        )
    else:
        unscored_reason = None

    return TaskScore(
        task_id=str(task["id"]),
        scoring_mode=scoring_mode,
        correctness=correctness,
        unscored_reason=unscored_reason,
        bad_write=bad_write,
        tool_call_correct=tool_call_result.passed,
        truncated=outcome.truncated,
        budget_exhausted=outcome.budget_exhausted,
        assertion_results=tuple(results),
    )


def _fraction_passed(results: Sequence[AssertionResult]) -> float:
    """Return the fraction of scored ``results`` that passed, or ``1.0`` when none are scored.

    ``unscored`` results are excluded from both numerator and denominator.
    """
    scored = [result for result in results if not result.unscored]
    if not scored:
        return 1.0
    return sum(1 for result in scored if result.passed) / len(scored)


def aggregate_scores(scores: Sequence[TaskScore]) -> AggregateScore:
    """Aggregate every task's :class:`TaskScore` into one arm-level :class:`AggregateScore`.

    ``mean_correctness`` averages exactly the tasks whose ``correctness`` is not ``None``. Every
    task contributes to ``bad_write_rate``.

    Args:
        scores: Every task's score for one sweep arm (one model/effort/repetition combination).

    Returns:
        The arm's :class:`AggregateScore`. ``mean_correctness`` is ``None`` when every task in
        ``scores`` has ``correctness is None``.
    """
    scored = [score for score in scores if score.correctness is not None]
    unscored = [score for score in scores if score.correctness is None]

    mean_correctness: float | None = None
    if scored:
        mean_correctness = sum(score.correctness for score in scored) / len(scored)  # type: ignore[misc]

    bad_write_rate = sum(1 for score in scores if score.bad_write) / len(scores) if scores else 0.0

    tool_call_signals = [
        score.tool_call_correct for score in scores if score.tool_call_correct is not None
    ]
    tool_call_correct_rate = (
        sum(1 for value in tool_call_signals if value) / len(tool_call_signals)
        if tool_call_signals
        else None
    )

    return AggregateScore(
        scored_task_count=len(scored),
        unscored_task_count=len(unscored),
        mean_correctness=mean_correctness,
        bad_write_rate=bad_write_rate,
        tool_call_correct_rate=tool_call_correct_rate,
    )


#: The model tiers :func:`score_tiers` reports, in report order.
SCORED_TIERS: Final[tuple[str, ...]] = ("supervisor", "subagent", "tool")

#: The delegation outcomes an ``expected_tiers.subagent.outcome`` may name.
_SUBAGENT_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"data", "no_match", "denied", "tool_failure"}
)


@dataclass(frozen=True, slots=True)
class ObservedToolArguments:
    """One dispatched tool call's name and the arguments the agent supplied for it.

    Attributes:
        tool_name: The invoked tool's name.
        arguments: The tool input exactly as the agent formulated it, before dispatch.
    """

    tool_name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TierScore:
    """One tier's success measurement for one turn.

    Attributes:
        tier: One of :data:`SCORED_TIERS`.
        applicable: Whether the task exercises this tier at all.
        passed: Whether the tier met its expectation; ``None`` exactly when ``applicable`` is
            ``False``.
        detail: Human-readable explanation, populated when ``passed`` is ``False``.
    """

    tier: str
    applicable: bool
    passed: bool | None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.tier not in SCORED_TIERS:
            raise ValueError(f"tier must be one of {SCORED_TIERS}, got {self.tier!r}")
        if self.applicable == (self.passed is None):
            raise ValueError("passed must be None exactly when the tier is not applicable")

    def to_report_fields(self) -> dict[str, object]:
        """Render as a flat JSON-safe mapping in a fixed key order."""
        return {"applicable": self.applicable, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class TierScores:
    """Every tier's :class:`TierScore` for one turn against one golden task.

    Attributes:
        task_id: The scored task's ``id``.
        supervisor: The supervisor tier: the task's ``correctness`` is a full ``1.0``.
        subagent: The subagent tier: the observed delegation outcome equals the expected one.
        tool: The tool tier: an expected tool was called with schema-valid arguments.
    """

    task_id: str
    supervisor: TierScore
    subagent: TierScore
    tool: TierScore

    def by_tier(self) -> dict[str, TierScore]:
        """Return the three scores keyed by tier, in :data:`SCORED_TIERS` order."""
        return {"supervisor": self.supervisor, "subagent": self.subagent, "tool": self.tool}

    def to_report_fields(self) -> dict[str, object]:
        """Render as ``{task_id, <tier>: {applicable, passed, detail}}`` in a fixed key order."""
        return {
            "task_id": self.task_id,
            **{tier: score.to_report_fields() for tier, score in self.by_tier().items()},
        }


def expected_tiers_of(task: Mapping[str, Any]) -> tuple[str | None, tuple[str, ...] | None]:
    """Return a task's expected subagent outcome and expected tool names from ``expected_tiers``.

    Args:
        task: One golden task object.

    Returns:
        ``(subagent_outcome, tool_names)``, either ``None`` when that tier is not applicable.

    Raises:
        ValueError: The task has no ``expected_tiers`` block, or the block is malformed.
    """
    task_id = task.get("id", "<no id>")
    block = task.get("expected_tiers")
    if not isinstance(block, Mapping) or set(block) != {"subagent", "tool"}:
        raise ValueError(f"task {task_id}: expected_tiers must map exactly 'subagent' and 'tool'")

    subagent = block["subagent"]
    subagent_outcome: str | None = None
    if subagent is not None:
        if not isinstance(subagent, Mapping) or set(subagent) != {"outcome"}:
            raise ValueError(f"task {task_id}: expected_tiers.subagent must be {{outcome}} or null")
        if subagent["outcome"] not in _SUBAGENT_OUTCOMES:
            raise ValueError(
                f"task {task_id}: expected_tiers.subagent.outcome must be one of "
                f"{sorted(_SUBAGENT_OUTCOMES)}, got {subagent['outcome']!r}"
            )
        subagent_outcome = str(subagent["outcome"])

    tool = block["tool"]
    tool_names: tuple[str, ...] | None = None
    if tool is not None:
        names = tool.get("tools") if isinstance(tool, Mapping) and set(tool) == {"tools"} else None
        if not isinstance(names, list) or not names:
            raise ValueError(f"task {task_id}: expected_tiers.tool must be {{tools: [..]}} or null")
        unknown = [name for name in names if name not in TOOLS_BY_NAME]
        if unknown:
            raise ValueError(f"task {task_id}: expected_tiers.tool names unknown tool(s) {unknown}")
        tool_names = tuple(str(name) for name in names)

    return subagent_outcome, tool_names


def _not_applicable(tier: str) -> TierScore:
    return TierScore(tier=tier, applicable=False, passed=None)


def _arguments_are_valid(call: ObservedToolArguments) -> bool:
    return arguments_conform(call.tool_name, call.arguments)


def score_tiers(
    task: Mapping[str, Any],
    outcome: TurnOutcome,
    tool_calls: Sequence[ObservedToolArguments],
    *,
    task_score: TaskScore,
) -> TierScores:
    """Score each model tier of one turn against the task's ``expected_tiers`` block.

    Args:
        task: The golden task, carrying ``expected_tiers`` (see :func:`expected_tiers_of`).
        outcome: What the turn did; its ``denial_outcome`` is the observed delegation outcome.
        tool_calls: Every tool call the turn dispatched with the arguments the agent supplied.
        task_score: The task's :func:`score_task` result; the supervisor tier is applicable when
            its ``correctness`` is not ``None`` and met when it is ``1.0``.

    Returns:
        The three tier scores.

    Raises:
        ValueError: ``task``'s ``expected_tiers`` block is absent or malformed.
    """
    expected_outcome, expected_tools = expected_tiers_of(task)

    if task_score.correctness is None:
        supervisor = _not_applicable("supervisor")
    else:
        met = task_score.correctness == 1.0
        supervisor = TierScore(
            tier="supervisor",
            applicable=True,
            passed=met,
            detail="" if met else f"task correctness {task_score.correctness:.6f} is below 1.0",
        )

    if expected_outcome is None:
        subagent = _not_applicable("subagent")
    else:
        met = outcome.denial_outcome == expected_outcome
        subagent = TierScore(
            tier="subagent",
            applicable=True,
            passed=met,
            detail=""
            if met
            else f"delegation outcome {outcome.denial_outcome!r}, expected {expected_outcome!r}",
        )

    if expected_tools is None:
        tool = _not_applicable("tool")
    else:
        candidates = [call for call in tool_calls if call.tool_name in expected_tools]
        met = any(_arguments_are_valid(call) for call in candidates)
        if met:
            detail = ""
        elif candidates:
            detail = f"every call to {sorted(expected_tools)} had schema-invalid arguments"
        else:
            detail = f"none of {sorted(expected_tools)} was called"
        tool = TierScore(tier="tool", applicable=True, passed=met, detail=detail)

    return TierScores(
        task_id=str(task.get("id")), supervisor=supervisor, subagent=subagent, tool=tool
    )


__all__ = [
    "PRE_TURN_ABSENT",
    "SCORED_TIERS",
    "AggregateScore",
    "AssertionResult",
    "ObservedProposal",
    "ObservedToolArguments",
    "TaskScore",
    "TierScore",
    "TierScores",
    "ToolCall",
    "TurnOutcome",
    "aggregate_scores",
    "capture_pre_turn_values",
    "expected_tiers_of",
    "score_task",
    "score_tiers",
]
