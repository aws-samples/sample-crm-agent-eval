# SPDX-License-Identifier: MIT
"""The oracle gate: a known-correct script scores 100% on every golden task.

For each golden task id and each of its scripts, a fresh ``eval-<run>`` tenant is seeded in
DynamoDB Local, the task runs through :func:`eval.sweep_units.run_task_unit` with a turn runner
calling the real :func:`eval.turn_observation.observe_turn` (so a declared fault fires), every
applicable tier must pass, and the script must be fully consumed. ``SCRIPTS`` is discovered from
every ``scripts_*.py`` in this package; a task with no script, or a script for an unknown task,
fails. The negative control, one bare supervisor ``Answer`` per task, must leave the supervisor
tier not passed wherever it is applicable.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import boto3
import pytest

from agents.registry import RegisteredTool
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS
from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks, in_process_dispatch_factory
from eval.scoring import TierScores
from eval.sweep_units import ArmSpec, TaskUnitResult, arm_turn_tuning, run_task_unit, task_actor
from eval.task_ids import localize_tasks
from eval.tenancy import seed_tenant, teardown_tenant, tenant_for_run
from eval.turn_observation import ObservedTurn, observe_turn
from providers.capabilities import load_capability_matrix
from tests.integration.oracle._contract import (
    SCRIPTS_REGISTRY_ATTR,
    Answer,
    Call,
    OracleScript,
    Step,
)
from tests.integration.oracle.scripted_provider import ScriptedProvider, ScriptExhausted

_MATRIX = load_capability_matrix()
_ARM = ArmSpec(family="anthropic")
_PACKAGE_DIR = Path(__file__).resolve().parent
_ENDPOINT_URL = "http://127.0.0.1:8000"
_LOCAL_ENGINE_CRED = "local"

ScriptRegistry = Mapping[str, tuple[OracleScript, ...]]


# ---------------------------------------------------------------------------- registry logic


class OracleRegistryError(ValueError):
    """A ``scripts_*.py`` module is malformed, or two modules script the same task."""


def discover_scripts(package_dir: Path) -> dict[str, tuple[OracleScript, ...]]:
    """Merge the ``SCRIPTS`` mapping of every ``scripts_*.py`` file in ``package_dir``.

    Raises:
        OracleRegistryError: A module lacks ``SCRIPTS``, maps a task to anything but a non-empty
            tuple of scripts, or scripts a task another module already scripts.
    """
    merged: dict[str, tuple[OracleScript, ...]] = {}
    owners: dict[str, str] = {}
    for path in sorted(package_dir.glob("scripts_*.py")):
        module_name = f"_oracle_scripts_{path.stem}_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise OracleRegistryError(f"{path.name} cannot be imported")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        registry = getattr(module, SCRIPTS_REGISTRY_ATTR, None)
        if not isinstance(registry, Mapping):
            raise OracleRegistryError(f"{path.name} exposes no {SCRIPTS_REGISTRY_ATTR} mapping")
        for task_id, scripts in registry.items():
            if not isinstance(scripts, tuple) or not scripts:
                raise OracleRegistryError(
                    f"{path.name} maps {task_id!r} to {type(scripts).__name__}, "
                    "not a non-empty tuple of scripts"
                )
            if task_id in merged:
                raise OracleRegistryError(
                    f"{task_id!r} is scripted by both {owners[task_id]} and {path.name}"
                )
            merged[str(task_id)] = scripts
            owners[str(task_id)] = path.name
    return merged


def coverage_problems(task_ids: Sequence[str], scripts: ScriptRegistry) -> list[str]:
    """Every golden task without a script and every script naming no golden task."""
    known = set(task_ids)
    return [f"no oracle script for {task_id}" for task_id in task_ids if task_id not in scripts] + [
        f"oracle script for unknown task {task_id}" for task_id in sorted(set(scripts) - known)
    ]


def gate_failures(tier_scores: TierScores, provider: ScriptedProvider) -> list[str]:
    """Why a scripted run does not pass the gate; empty when it does.

    Every applicable tier must pass, at least one tier must be applicable, and the script must
    be fully consumed.
    """
    failures = [
        f"{tier}: {score.detail or 'not passed'}"
        for tier, score in tier_scores.by_tier().items()
        if score.applicable and not score.passed
    ]
    if not any(score.applicable for score in tier_scores.by_tier().values()):
        failures.append("no tier is applicable, so the script verifies nothing")
    try:
        provider.assert_fully_consumed()
    except ScriptExhausted as exc:
        failures.append(f"script not consumed: {exc}")
    return failures


#: A tenant-relative record id in a script: kind prefix plus a four-digit index or defect suffix.
_LOCAL_ID: Final[re.Pattern[str]] = re.compile(
    r"\b(acct|cont|opp|act|note)-(defect-pd\d{2}|\d{4})\b"
)

#: Fixture file and id field per record kind; the fixtures carry the ``demo`` tenant's ids.
_FIXTURE_IDS: Final[dict[str, tuple[str, str]]] = {
    "acct": ("accounts", "account_id"),
    "cont": ("contacts", "contact_id"),
    "opp": ("opportunities", "opportunity_id"),
    "act": ("activities", "activity_id"),
    "note": ("notes", "note_id"),
}


def seeded_local_ids(fixtures_dir: Path) -> dict[str, tuple[str, ...]]:
    """Every seeded four-digit record id per kind, tenant-relative and sorted."""
    seeded: dict[str, tuple[str, ...]] = {}
    for kind, (name, field) in _FIXTURE_IDS.items():
        rows = json.loads((fixtures_dir / f"{name}.json").read_text(encoding="utf-8"))
        local = (str(row[field]).replace(f"{kind}-demo-", f"{kind}-", 1) for row in rows)
        seeded[kind] = tuple(sorted(i for i in local if re.fullmatch(rf"{kind}-\d{{4}}", i)))
    return seeded


def _tokens(value: object) -> set[str]:
    if isinstance(value, str):
        return {match.group(0) for match in _LOCAL_ID.finditer(value)}
    if isinstance(value, Mapping):
        return set().union(*(_tokens(item) for item in value.values())) if value else set()
    if isinstance(value, list | tuple):
        return set().union(*(_tokens(item) for item in value)) if value else set()
    return set()


def _substitute(value: object, mapping: Mapping[str, str]) -> object:
    if isinstance(value, str):
        return _LOCAL_ID.sub(lambda match: mapping.get(match.group(0), match.group(0)), value)
    if isinstance(value, Mapping):
        return {key: _substitute(item, mapping) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_substitute(item, mapping) for item in value]
    return value


def retarget_script(
    script: OracleScript, *, avoid: set[str], seeded: Mapping[str, Sequence[str]]
) -> tuple[OracleScript, dict[str, str]]:
    """Rewrite every record id in the script's ``Call`` args to a different seeded id of its kind.

    Each id maps to the next seeded id of the same kind, cyclically, that is not in ``avoid`` and
    not already a replacement. ``Answer`` steps are unchanged.

    Returns:
        The retargeted script and the id mapping; the mapping is empty for a target-free script.
    """
    originals = sorted(set().union(*(_tokens(s.args) for s in script if isinstance(s, Call))))
    mapping: dict[str, str] = {}
    for original in originals:
        kind = original.split("-", 1)[0]
        pool = list(seeded[kind])
        start = next((i for i, candidate in enumerate(pool) if candidate > original), 0)
        rotated = pool[start:] + pool[:start]
        taken = avoid | set(mapping.values())
        mapping[original] = next(candidate for candidate in rotated if candidate not in taken)
    steps: list[Step] = [
        Call(
            role=step.role,
            tool=step.tool,
            args={key: _substitute(item, mapping) for key, item in step.args.items()},
        )
        if isinstance(step, Call)
        else step
        for step in script
    ]
    return tuple(steps), mapping


#: Golden-task write values the prompt itself supplies, so the oracle need not observe them in a
#: tool result first: ``(task id, field)`` to the reason. Each entry's value must occur in its
#: task's prompt.
_PROMPT_SUPPLIED_VALUES: Final[dict[tuple[str, str], str]] = {
    ("gt-03-investigation-pd03-scoped-lookup", "title"): "the prompt names the new title",
    ("gt-06-enrichment-create-note", "opportunity_id"): "the prompt names the opportunity",
    ("gt-07-enrichment-log-activity-last-touch", "opportunity_id"): "the prompt names it",
    ("gt-10-enrichment-update-account-fields", "website"): "the prompt gives the website",
    ("gt-10-enrichment-update-account-fields", "industry"): "the prompt gives the industry",
    ("gt-24-enrichment-pd07-multi-step-write", "subject"): "the prompt gives the subject",
    ("gt-24-enrichment-pd07-multi-step-write", "body"): "the prompt gives the note text",
}

#: Tools that write, by the same rule as scoring's ``write_attempted``: any risk other than read.
_WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    tool.name for tool in ALL_TOOLS if tool.risk != "read"
)

#: A scoring sentinel (``__advanced__``), not a literal value a tool could return.
_SENTINEL_VALUE: Final[re.Pattern[str]] = re.compile(r"__[a-z_]+__")


def written_literal_values(task: Mapping[str, object]) -> list[tuple[str, object]]:
    """Every ``(field, value)`` a task's write assertions name literally, sentinels excluded."""
    assertions = task.get("assertions")
    if not isinstance(assertions, Mapping):
        return []
    values: list[tuple[str, object]] = []
    proposal = assertions.get("expected_proposal")
    if isinstance(proposal, Mapping) and isinstance(proposal.get("after_payload"), Mapping):
        values.extend((str(f), v) for f, v in proposal["after_payload"].items())
    for change in assertions.get("expected_field_changes") or []:
        if not isinstance(change, Mapping):
            continue
        value = change.get("expected")
        if isinstance(value, str) and _SENTINEL_VALUE.fullmatch(value):
            continue
        values.append((str(change.get("field")), value))
    return values


def write_targets(task: Mapping[str, object]) -> set[str]:
    """The record ids a task's write assertions target, including ``match`` opportunity ids."""
    assertions = task.get("assertions")
    if not isinstance(assertions, Mapping):
        return set()
    targets: set[str] = set()
    proposal = assertions.get("expected_proposal")
    if isinstance(proposal, Mapping) and proposal.get("target_id"):
        targets.add(str(proposal["target_id"]))
    for change in assertions.get("expected_field_changes") or []:
        if isinstance(change, Mapping):
            if change.get("id"):
                targets.add(str(change["id"]))
            match = change.get("match")
            if isinstance(match, Mapping) and match.get("opportunity_id"):
                targets.add(str(match["opportunity_id"]))
    return targets


def planted_evidence_notes(fixtures_dir: Path) -> dict[str, tuple[tuple[str, str], ...]]:
    """Planted-defect notes per tenant-relative opportunity id: ``(note id, body)`` pairs."""
    rows = json.loads((fixtures_dir / "notes.json").read_text(encoding="utf-8"))
    planted: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        note_id = str(row["note_id"]).replace("note-demo-", "note-", 1)
        if "-defect-" in note_id and row.get("body"):
            opportunity = str(row["opportunity_id"]).replace("opp-demo-", "opp-", 1)
            planted.setdefault(opportunity, []).append((note_id, str(row["body"])))
    return {key: tuple(value) for key, value in planted.items()}


def _leaves(value: object) -> Iterator[object]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def value_observed(value: object, outputs: Sequence[object]) -> bool:
    """Whether ``value`` occurs in some tool output: a string as a substring, a number by value."""
    leaves = [leaf for output in outputs for leaf in _leaves(output)]
    strings = [leaf for leaf in leaves if isinstance(leaf, str)]
    if isinstance(value, str):
        return any(value.strip() in leaf for leaf in strings)
    number = _as_decimal(value)
    if number is None:
        return False
    if any(_as_decimal(leaf) == number for leaf in leaves if not isinstance(leaf, str)):
        return True
    token = re.compile(rf"(?<![\d.]){re.escape(str(value))}(?:\.0+)?(?![\d])")
    return any(token.search(leaf) for leaf in strings)


def evidence_problems(
    raw_task: Mapping[str, object],
    task: Mapping[str, object],
    dispatched: Sequence[tuple[str, object]],
    planted: Mapping[str, Sequence[tuple[str, str]]],
) -> list[str]:
    """Why a script's write is not backed by evidence it observed before the write.

    Args:
        raw_task: The tenant-relative golden task.
        task: The same task localized to the run's tenant.
        dispatched: ``(tool name, output)`` per dispatched call, in order.
        planted: :func:`planted_evidence_notes`.

    Returns:
        Empty when the task writes no literal value beyond what its prompt supplies. Otherwise one
        "evidence unreachable" line per such value not seen in a tool output before the first
        write call, and per planted-defect note on a write target whose body was not seen.
    """
    task_id = str(raw_task.get("id"))
    first_write = next(
        (i for i, (tool, _) in enumerate(dispatched) if tool in _WRITE_TOOL_NAMES),
        len(dispatched),
    )
    before = [output for _, output in dispatched[:first_write]]
    inferred = [
        (field, value)
        for (field, _), (_, value) in zip(
            written_literal_values(raw_task), written_literal_values(task), strict=True
        )
        if (task_id, field) not in _PROMPT_SUPPLIED_VALUES
    ]
    if not inferred:
        return []
    problems = [
        f"evidence unreachable: {field}={value!r} was not in any tool result before the write"
        for field, value in inferred
        if not value_observed(value, before)
    ]
    for target in sorted(write_targets(raw_task)):
        for note_id, body in planted.get(target, ()):
            if not value_observed(body, before):
                problems.append(
                    f"evidence unreachable: the body of {note_id} on {target} was not in any "
                    "tool result before the write"
                )
    return problems


def negative_control_script() -> OracleScript:
    """The script that does no work: one bare supervisor answer."""
    return (Answer(role="supervisor", text="I have nothing to report."),)


# --------------------------------------------------------------------------- the real inputs


_GOLDEN_TASKS: list[dict[str, object]] = _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)
_TASK_IDS: list[str] = [str(task["id"]) for task in _GOLDEN_TASKS]
_RAW_BY_ID: dict[str, dict[str, object]] = {str(task["id"]): task for task in _GOLDEN_TASKS}
_SCRIPTS: dict[str, tuple[OracleScript, ...]] = discover_scripts(_PACKAGE_DIR)
_SEEDED: dict[str, tuple[str, ...]] = seeded_local_ids(_PACKAGE_DIR.parents[2] / "fixtures")
_PLANTED: dict[str, tuple[tuple[str, str], ...]] = planted_evidence_notes(
    _PACKAGE_DIR.parents[2] / "fixtures"
)

#: Tasks exempt from the wrong-target control, each with why it cannot be distinguished.
_WRONG_TARGET_EXEMPT: Final[dict[str, str]] = {
    "gt-15-denial-service-principal-no-implicit-visibility": (
        "target-free listing: search_opportunities with no filter, denied as account:*"
    ),
    "gt-16-denial-tool-failure-not-conflated": (
        "the injected transport fault fails every list_activities call whatever its target, and "
        "the task asserts only the tool_failure outcome, so no target is scored"
    ),
    "gt-19-lookup-pipeline-aggregate": "target-free listing: pipeline_aggregate with no filter",
    "gt-20-lookup-proposal-queue": "target-free listing: list_proposals(status=pending)",
}


@pytest.fixture(scope="module")
def _tables(_dynamodb_local_endpoint: None) -> Iterator[Store]:
    """One freshly created table set for this module; each script seeds its own tenant in it."""
    client = boto3.client(
        "dynamodb",
        endpoint_url=_ENDPOINT_URL,
        region_name="us-east-1",
        aws_access_key_id=_LOCAL_ENGINE_CRED,
        aws_secret_access_key=_LOCAL_ENGINE_CRED,
    )
    prefix = f"t{uuid.uuid4().hex[:12]}_"
    create_all_tables(client, table_prefix=prefix)
    try:
        yield Store(table_prefix=prefix, client=client)
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(client.exceptions.ResourceNotFoundException):
                client.delete_table(TableName=f"{prefix}{table}")


Dispatch = Callable[[RegisteredTool, dict[str, object]], dict[str, Any]]


def _recording(dispatch: Dispatch, log: list[tuple[str, object]]) -> Dispatch:
    """Wrap ``dispatch`` to append ``(tool name, output)`` for every call it serves."""

    def _dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        output = dispatch(tool, tool_input)
        log.append((tool.name, output))
        return output

    return _dispatch


def _run_scripted(
    store: Store,
    task_id: str,
    script: OracleScript,
    *,
    raw_task: Mapping[str, object] | None = None,
    evidence: list[str] | None = None,
) -> tuple[TaskUnitResult, ScriptedProvider]:
    """Run ``script`` for ``task_id`` (or ``raw_task``) in a freshly seeded tenant, then tear it down.

    With ``evidence``, appends :func:`evidence_problems` for the run to it.
    """
    tenant = tenant_for_run(f"oracle{uuid.uuid4().hex[:10]}")
    seed_tenant(store, tenant=tenant)
    try:
        source = raw_task if raw_task is not None else _RAW_BY_ID[task_id]
        (task,) = localize_tasks([dict(source)], tenant=tenant)
        provider = ScriptedProvider(script, tenant)
        dispatched: list[tuple[str, object]] = []

        def _turn_runner(unit_task: Mapping[str, object], unit_tenant: str) -> ObservedTurn:
            return asyncio.run(
                observe_turn(
                    prompt=str(unit_task.get("prompt") or ""),
                    task_id=str(unit_task["id"]),
                    tenant=unit_tenant,
                    providers=lambda _family: provider,
                    matrix=_MATRIX,
                    tuning=arm_turn_tuning(_ARM),
                    dispatch=_recording(
                        in_process_dispatch_factory(
                            store=store, actor=task_actor(unit_task), tenant=unit_tenant
                        ),
                        dispatched,
                    ),
                )
            )

        unit = run_task_unit(
            store=store, task=task, tenant=tenant, family=_ARM.family, turn_runner=_turn_runner
        )
        if evidence is not None:
            evidence.extend(evidence_problems(source, task, dispatched, _PLANTED))
        return unit, provider
    finally:
        teardown_tenant(store, tenant=tenant)


# ---------------------------------------------------------------------------------- the gate


@pytest.mark.parametrize("task_id", _TASK_IDS)
def test_the_oracle_script_scores_every_applicable_tier(_tables: Store, task_id: str) -> None:
    scripts = _SCRIPTS.get(task_id)
    if not scripts:
        pytest.fail(f"no oracle script for {task_id}; add one to a scripts_*.py module")
    failures: list[str] = []
    for index, script in enumerate(scripts):
        evidence: list[str] = []
        unit, provider = _run_scripted(_tables, task_id, script, evidence=evidence)
        failures.extend(
            f"script {index}: {reason}"
            for reason in [*gate_failures(unit.tier_scores, provider), *evidence]
        )
    assert failures == []


def test_no_oracle_script_names_an_unknown_task() -> None:
    assert [p for p in coverage_problems(_TASK_IDS, _SCRIPTS) if "unknown task" in p] == []


@pytest.mark.parametrize("task_id", _TASK_IDS)
def test_negative_control_fails_the_supervisor_tier(_tables: Store, task_id: str) -> None:
    unit, provider = _run_scripted(_tables, task_id, negative_control_script())
    provider.assert_fully_consumed()
    supervisor = unit.tier_scores.supervisor
    if supervisor.applicable:
        assert supervisor.passed is False, f"{task_id} passes its supervisor tier with no work"


def _script_tokens(script: OracleScript) -> set[str]:
    return set().union(
        *(_tokens(step.args) if isinstance(step, Call) else _tokens(step.text) for step in script)
    )


@pytest.mark.parametrize("task_id", _TASK_IDS)
def test_negative_control_wrong_target_fails_an_applicable_tier(
    _tables: Store, task_id: str
) -> None:
    scripts = _SCRIPTS.get(task_id)
    if not scripts:
        pytest.fail(f"no oracle script for {task_id}; add one to a scripts_*.py module")
    task_tokens = _tokens(json.dumps(_RAW_BY_ID[task_id], default=str))
    problems: list[str] = []
    still_needed = False
    for index, script in enumerate(scripts):
        retargeted, mapping = retarget_script(
            script, avoid=task_tokens | _script_tokens(script), seeded=_SEEDED
        )
        if not mapping:
            still_needed = True
            problems.append(f"script {index}: names no record id, so nothing was retargeted")
            continue
        unit, provider = _run_scripted(_tables, task_id, retargeted)
        provider.assert_fully_consumed()
        failed = [
            tier
            for tier, score in unit.tier_scores.by_tier().items()
            if score.applicable and not score.passed
        ]
        if not failed:
            still_needed = True
            problems.append(f"script {index}: retargeted {mapping} still passes every tier")
    if task_id in _WRONG_TARGET_EXEMPT:
        assert still_needed, f"{task_id}'s wrong-target exemption is stale: every control fails"
        return
    assert problems == []


def test_negative_control_wrong_target_exemptions_name_golden_tasks() -> None:
    assert set(_WRONG_TARGET_EXEMPT) <= set(_TASK_IDS)


# ------------------------------------------------------------- meta-tests (synthetic inputs)


def _write_module(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(
        "# SPDX-License-Identifier: MIT\n"
        "from tests.integration.oracle._contract import Answer, Call\n" + body,
        encoding="utf-8",
    )


def test_registry_discovery_merges_every_scripts_module(tmp_path: Path) -> None:
    _write_module(tmp_path, "scripts_a.py", 'SCRIPTS = {"t1": ((Answer("supervisor", "a"),),)}\n')
    _write_module(
        tmp_path,
        "scripts_b.py",
        'SCRIPTS = {"t2": ((Call("supervisor", "research", {"intent": "x"}),),'
        ' (Answer("supervisor", "b"),))}\n',
    )
    _write_module(tmp_path, "helpers.py", 'SCRIPTS = {"ignored": ()}\n')
    registry = discover_scripts(tmp_path)
    assert sorted(registry) == ["t1", "t2"]
    assert len(registry["t2"]) == 2


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("X = 1\n", "exposes no SCRIPTS"),
        ('SCRIPTS = {"t1": ()}\n', "non-empty tuple"),
        ('SCRIPTS = {"t1": [(Answer("supervisor", "a"),)]}\n', "non-empty tuple"),
    ],
    ids=["no-attr", "empty", "list-not-tuple"],
)
def test_registry_discovery_refuses_a_malformed_module(
    tmp_path: Path, body: str, match: str
) -> None:
    _write_module(tmp_path, "scripts_bad.py", body)
    with pytest.raises(OracleRegistryError, match=match):
        discover_scripts(tmp_path)


def test_registry_discovery_refuses_one_task_scripted_twice(tmp_path: Path) -> None:
    for name in ("scripts_a.py", "scripts_b.py"):
        _write_module(tmp_path, name, 'SCRIPTS = {"t1": ((Answer("supervisor", "a"),),)}\n')
    with pytest.raises(OracleRegistryError, match="scripted by both"):
        discover_scripts(tmp_path)


def test_registry_discovery_of_an_empty_package_is_empty(tmp_path: Path) -> None:
    assert discover_scripts(tmp_path) == {}


def test_registry_coverage_flags_a_missing_and_an_unknown_task() -> None:
    script: OracleScript = (Answer(role="supervisor", text="a"),)
    problems = coverage_problems(["t1", "t2"], {"t1": (script,), "t9": (script,)})
    assert problems == ["no oracle script for t2", "oracle script for unknown task t9"]


def test_registry_coverage_of_an_exact_registry_is_clean() -> None:
    script: OracleScript = (Answer(role="supervisor", text="a"),)
    assert coverage_problems(["t1"], {"t1": (script,)}) == []


def test_registry_of_this_package_imports_cleanly() -> None:
    assert all(isinstance(scripts, tuple) and scripts for scripts in _SCRIPTS.values())


def _tier_scores(**passed: bool | None) -> TierScores:
    from eval.scoring import SCORED_TIERS, TierScore

    return TierScores(
        task_id="t",
        **{
            tier: TierScore(
                tier=tier,
                applicable=passed.get(tier) is not None,
                passed=passed.get(tier),
                detail="" if passed.get(tier) is not False else f"{tier} detail",
            )
            for tier in SCORED_TIERS
        },
    )


def _consumed_provider() -> ScriptedProvider:
    return ScriptedProvider((), "eval-meta")


def test_registry_gate_passes_only_when_every_applicable_tier_passes() -> None:
    provider = _consumed_provider()
    assert gate_failures(_tier_scores(supervisor=True, subagent=True, tool=None), provider) == []
    assert gate_failures(_tier_scores(supervisor=True, subagent=False, tool=None), provider) == [
        "subagent: subagent detail"
    ]


def test_registry_gate_fails_when_no_tier_is_applicable() -> None:
    failures = gate_failures(_tier_scores(), _consumed_provider())
    assert failures == ["no tier is applicable, so the script verifies nothing"]


def test_registry_gate_fails_on_an_unconsumed_script() -> None:
    provider = ScriptedProvider((Answer(role="supervisor", text="left over"),), "eval-meta")
    failures = gate_failures(_tier_scores(supervisor=True), provider)
    assert len(failures) == 1
    assert failures[0].startswith("script not consumed: ")


def test_negative_control_is_one_bare_supervisor_answer() -> None:
    script = negative_control_script()
    assert len(script) == 1
    assert isinstance(script[0], Answer)
    assert script[0].role == "supervisor"


#: A synthetic read-only lookup task, independent of the golden yaml, for the gate's controls.
_SYNTHETIC_LOOKUP: dict[str, object] = {
    "id": "synthetic-oracle-lookup",
    "prompt": "What stage is opportunity opp-0000 in?",
    "actor": {"rep_id": "rep-ashford", "book": ["acct-0000"], "full_visibility": False},
    "scoring_mode": "machine",
    "expected_tiers": {"subagent": {"outcome": "data"}, "tool": {"tools": ["get_opportunity"]}},
    "assertions": {
        "must_not_change": [{"table": "opportunities", "id": "opp-0000", "field": "stage"}],
        "max_proposal_count": 0,
        "expected_reads": [{"tools": ["get_opportunity"], "target_id": "opp-0000"}],
    },
}

_SYNTHETIC_LOOKUP_SCRIPT: OracleScript = (
    Call(role="supervisor", tool="research", args={"intent": "Get opportunity opp-0000."}),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0000"}),
    Answer(role="research", text="Fetched opp-0000."),
    Answer(role="supervisor", text="Here is opp-0000."),
)


def test_negative_control_counterpart_a_correct_script_passes_the_gate(_tables: Store) -> None:
    unit, provider = _run_scripted(
        _tables, "synthetic-oracle-lookup", _SYNTHETIC_LOOKUP_SCRIPT, raw_task=_SYNTHETIC_LOOKUP
    )
    assert gate_failures(unit.tier_scores, provider) == []
    assert all(score.applicable for score in unit.tier_scores.by_tier().values())


def test_negative_control_fails_the_synthetic_task_at_every_tier(_tables: Store) -> None:
    unit, provider = _run_scripted(
        _tables, "synthetic-oracle-lookup", negative_control_script(), raw_task=_SYNTHETIC_LOOKUP
    )
    provider.assert_fully_consumed()
    assert {tier: score.passed for tier, score in unit.tier_scores.by_tier().items()} == {
        "supervisor": False,
        "subagent": False,
        "tool": False,
    }
    assert gate_failures(unit.tier_scores, provider) != []


_SYNTHETIC_SEEDED: dict[str, tuple[str, ...]] = {
    "acct": ("acct-0000", "acct-0001", "acct-0002"),
    "cont": ("cont-0000", "cont-0001"),
    "opp": ("opp-0000", "opp-0001", "opp-0004"),
    "act": ("act-0000", "act-0001"),
    "note": ("note-0000", "note-0001"),
}


def test_negative_control_retarget_moves_every_arg_id_to_another_seeded_id_of_its_kind() -> None:
    script: OracleScript = (
        Call(role="supervisor", tool="research", args={"intent": "Read opp-0000 at acct-0000."}),
        Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0000"}),
        Call(role="research", tool="x", args={"ids": ["acct-0000", {"c": "cont-0001"}]}),
        Answer(role="supervisor", text="opp-0000 stays in the answer."),
    )
    retargeted, mapping = retarget_script(
        script, avoid={"opp-0001", *_script_tokens(script)}, seeded=_SYNTHETIC_SEEDED
    )
    assert mapping == {"acct-0000": "acct-0001", "cont-0001": "cont-0000", "opp-0000": "opp-0004"}
    assert retargeted[0] == Call(
        role="supervisor", tool="research", args={"intent": "Read opp-0004 at acct-0001."}
    )
    assert retargeted[1] == Call(
        role="research", tool="get_opportunity", args={"opportunity_id": "opp-0004"}
    )
    assert retargeted[2] == Call(
        role="research", tool="x", args={"ids": ["acct-0001", {"c": "cont-0000"}]}
    )
    assert retargeted[3] == script[3]


def test_negative_control_retarget_of_a_target_free_script_is_empty() -> None:
    script: OracleScript = (
        Call(role="analytics", tool="pipeline_aggregate", args={}),
        Answer(role="supervisor", text="acct-0000"),
    )
    retargeted, mapping = retarget_script(script, avoid=set(), seeded=_SYNTHETIC_SEEDED)
    assert mapping == {}
    assert retargeted == script


def test_negative_control_seeded_ids_are_tenant_relative_and_non_empty() -> None:
    for kind, ids in _SEEDED.items():
        assert ids, kind
        assert all(re.fullmatch(rf"{kind}-\d{{4}}", value) for value in ids)


# ------------------------------------------------------------- evidence reachability (meta)


def test_registry_prompt_supplied_exemptions_name_values_their_prompts_carry() -> None:
    for (task_id, field), reason in _PROMPT_SUPPLIED_VALUES.items():
        raw = _RAW_BY_ID[task_id]
        values = [value for name, value in written_literal_values(raw) if name == field]
        assert values, f"{task_id} writes no literal {field}; drop the exemption ({reason})"
        for value in values:
            assert str(value) in str(raw.get("prompt")), f"{task_id}.{field}={value!r}"


def test_registry_evidence_value_observed_matches_strings_and_numbers() -> None:
    outputs: list[object] = [
        {"body": {"notes": [{"body": "Budget was re-approved at 180000. Sign by 2026-06-30."}]}},
        {"body": {"value": 120000.0, "stage": "Discovery"}},
    ]
    assert value_observed("2026-06-30", outputs)
    assert value_observed(180000, outputs)
    assert value_observed(120000, outputs)
    assert value_observed(Decimal("120000"), outputs)
    assert not value_observed(18000, outputs)
    assert not value_observed(1800000, outputs)
    assert not value_observed("Security Review", outputs)


_EVIDENCE_TASK: dict[str, object] = {
    "id": "synthetic-evidence",
    "prompt": "Bring opp-0001 up to date.",
    "assertions": {
        "expected_proposal": {"target_id": "opp-0001", "after_payload": {"value": 180000}},
    },
}

_EVIDENCE_PLANTED: dict[str, tuple[tuple[str, str], ...]] = {
    "opp-0001": (("note-defect-pd02", "Budget was re-approved at 180000."),)
}


def test_registry_evidence_seen_before_the_write_passes() -> None:
    dispatched: list[tuple[str, object]] = [
        ("get_opportunity", {"body": {"notes": [{"body": "Budget was re-approved at 180000."}]}}),
        ("submit_proposal", {"statusCode": 200}),
    ]
    assert evidence_problems(_EVIDENCE_TASK, _EVIDENCE_TASK, dispatched, _EVIDENCE_PLANTED) == []


def test_registry_evidence_seen_only_in_the_write_itself_is_unreachable() -> None:
    dispatched: list[tuple[str, object]] = [
        ("get_opportunity", {"body": {"notes": [{"note_id": "note-defect-pd02"}]}}),
        ("submit_proposal", {"body": {"after": {"value": 180000}, "rationale": "re-approved"}}),
        ("get_opportunity", {"body": {"notes": [{"body": "Budget was re-approved at 180000."}]}}),
    ]
    assert evidence_problems(_EVIDENCE_TASK, _EVIDENCE_TASK, dispatched, _EVIDENCE_PLANTED) == [
        "evidence unreachable: value=180000 was not in any tool result before the write",
        "evidence unreachable: the body of note-defect-pd02 on opp-0001 was not in any tool "
        "result before the write",
    ]


def test_registry_evidence_is_not_required_for_a_prompt_supplied_write() -> None:
    task = {**_EVIDENCE_TASK, "id": "gt-10-enrichment-update-account-fields"}
    task["assertions"] = {
        "expected_field_changes": [
            {"table": "accounts", "id": "opp-0001", "field": "website", "expected": "x"}
        ]
    }
    assert evidence_problems(task, task, [("update_account", {})], _EVIDENCE_PLANTED) == []


def test_registry_evidence_skips_scoring_sentinels() -> None:
    task: dict[str, object] = {
        "id": "synthetic-sentinel",
        "assertions": {
            "expected_field_changes": [
                {
                    "table": "opportunities",
                    "id": "opp-9",
                    "field": "last_touch",
                    "expected": "__advanced__",
                }
            ]
        },
    }
    assert written_literal_values(task) == []
