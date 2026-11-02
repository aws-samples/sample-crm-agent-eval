# SPDX-License-Identifier: MIT
"""Layer 4 agent smoke checks over one real chat turn through CloudFront -> relay -> Runtime.

A disposable `eval-smoke-<run_id>` tenant and Cognito user are provisioned with
`scripts.smoke_agent_stream.LiveStreamPrincipalProvisioner` (empty book, no full visibility) and
removed afterwards; the `demo` tenant is proved untouched. One turn is posted through
`cloudfront_source`, naming a real `demo` account the caller cannot see, and counted against this
module's :data:`scripts.smoke.three_tier_chat.MODEL_TURN_ALLOTMENTS` share. Billable: the turn makes
real model calls.

:func:`relay_turn_checks` turns the parsed stream into rows: the turn completed, no `tool_result`
carried the out-of-book account, every event kind is in the closed vocabulary, and the per-agent
grid (`responds`, `no_surplus_tool`, `no_missing_tool`, `no_direct_high_risk_write` per agent and
the mutation subagent's `outcome_discipline_*`). A grid row the stream cannot establish is a `defer`
naming its reason (:data:`GRID_DEFER_REASONS`), never a `pass`. Structural properties only; the
assistant's prose is never read.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final, Protocol

from scripts.smoke.demo_tenant_guard import (
    DEMO_TENANT,
    demo_tenant_untouched_check,
    read_tenant_snapshot,
    store_row_reader,
    writes_only_under_smoke_tenant_check,
)
from scripts.smoke.harness import DEFER, CheckResult, SmokeContext, run_main
from scripts.smoke.three_tier_chat import ModelTurnBudget, budget_refused
from scripts.smoke_agent_stream import (
    LiveStreamPrincipalProvisioner,
    cloudfront_source,
    parse_sse_bytes,
)

from agents.events import EVENT_KINDS
from agents.registry import tool_registry_for
from agents.subagents import ALL_SUBAGENT_SPECS
from agents.supervisor import DIRECT_TOOL_NAMES, SUPERVISOR_ROLE
from crm.toolspec.definitions import (
    BY_NAME,
    TABLES,
    UPDATE_ALLOWLISTS,
    ToolDefinition,
    high_risk_fields_for,
)

LAYER = 4
RESOURCE_CLASS = "agents"

__all__ = [
    "GRID_DEFER_REASONS",
    "LAYER",
    "RESOURCE_CLASS",
    "BY_NAME",
    "TurnRunner",
    "checks",
    "main",
    "out_of_book_account",
    "relay_turn_checks",
    "turn_prompt",
]

_SUBAGENT_ROLES: tuple[str, ...] = tuple(spec.role for spec in ALL_SUBAGENT_SPECS)

#: The per-agent grid's check names, in report order.
_PER_AGENT_CHECKS: Final[tuple[str, ...]] = (
    "responds",
    "no_surplus_tool",
    "no_missing_tool",
    "no_direct_high_risk_write",
)

#: The mutation subagent's outcome-discipline rows.
_OUTCOME_RESOURCE: Final[str] = "mutation"
_OUTCOME_CHECKS: Final[tuple[str, ...]] = (
    "outcome_discipline_scope_denial",
    "outcome_discipline_empty_result",
    "outcome_discipline_handler_error",
    "outcome_discipline_distinct",
)

#: Every grid row that a single relay turn cannot establish, mapped to its `defer_reason`.
GRID_DEFER_REASONS: Final[Mapping[tuple[str, str], str]] = {
    **{
        (role, "no_missing_tool"): "stream_carries_no_advertised_tool_surface"
        for role in (SUPERVISOR_ROLE, *_SUBAGENT_ROLES)
    },
    **{(role, "no_surplus_tool"): "tool_call_events_carry_no_role" for role in _SUBAGENT_ROLES},
    **{
        (role, "no_direct_high_risk_write"): "tool_call_events_carry_no_role"
        for role in _SUBAGENT_ROLES
    },
    **{
        (_OUTCOME_RESOURCE, check): "relay_turn_cannot_select_an_outcome_trigger"
        for check in _OUTCOME_CHECKS
    },
}

_GRID_DEFER_DETAIL: Final[Mapping[str, str]] = {
    "stream_carries_no_advertised_tool_surface": (
        "the SSE stream reports the tools a turn called, never the tools an agent holds, so a "
        "missing tool cannot be observed from a turn"
    ),
    "tool_call_events_carry_no_role": (
        "tool_call events name the tool but not the agent that called it, so a call cannot be "
        "attributed to this subagent; every observed call is judged under the supervisor's rows"
    ),
    "relay_turn_cannot_select_an_outcome_trigger": (
        "the relay's payload is closed, so a turn cannot request the scope-denial, empty-result or "
        "handler-error trigger each of these rows needs"
    ),
}

_RELAY_RESOURCE: Final[str] = "relay_turn"

#: The turn-level checks, in report order.
_RELAY_TURN_CHECKS: Final[tuple[str, ...]] = (
    "relay_turn_reaches_completion",
    "no_out_of_book_row_in_any_tool_result",
    "every_event_kind_is_in_the_closed_vocabulary",
)

_TURN_EVENT_KINDS: Final[frozenset[str]] = frozenset(EVENT_KINDS)


def _turn_rows() -> tuple[tuple[str, str], ...]:
    """Every `(resource, check)` row :func:`relay_turn_checks` emits, in report order."""
    grid = [
        (role, check) for role in (SUPERVISOR_ROLE, *_SUBAGENT_ROLES) for check in _PER_AGENT_CHECKS
    ]
    grid += [(_OUTCOME_RESOURCE, check) for check in _OUTCOME_CHECKS]
    return (*((_RELAY_RESOURCE, check) for check in _RELAY_TURN_CHECKS), *grid)


class TurnRunner(Protocol):
    """The seam one live chat turn is driven through.

    A live implementation posts through CloudFront `/agent/*` and parses the SSE body; an offline
    test substitutes a callable returning canned event payloads.
    """

    def __call__(self, prompt: str) -> list[dict[str, Any]]:
        """Run one turn and return its parsed event payloads in arrival order."""
        ...


# -- the high-risk judgement over a tool name ---------------------------------------------------


def _could_commit_high_risk_change(tool: ToolDefinition) -> bool:
    """Return whether `tool` could commit a high-risk change directly.

    True iff `tool` commits without a proposal gate (`risk in {"low_risk_write", "approval"}`) and
    its update allowlist for its target table meets that table's high-risk field set. The allowlist
    is `properties.fields.enum` (or `properties.after_payload.enum`) when the schema enumerates it,
    else the shared `UPDATE_ALLOWLISTS` entry.
    """
    if tool.risk not in {"low_risk_write", "approval"}:
        return False
    target_table = _target_table_for(tool)
    if target_table is None:
        return False
    high_risk_fields = high_risk_fields_for(target_table)
    if not high_risk_fields:
        return False
    properties = tool.input_schema.get("properties", {})
    fields_property = properties.get("fields") or properties.get("after_payload") or {}
    enumerated = fields_property.get("enum")
    allowlist = (
        frozenset(enumerated)
        if enumerated is not None
        else UPDATE_ALLOWLISTS.get(target_table, frozenset())
    )
    return not allowlist.isdisjoint(high_risk_fields)


def _target_table_for(tool: ToolDefinition) -> str | None:
    """The entity table `tool`'s `fields`/`after_payload` argument applies to, or `None`."""
    properties = tool.input_schema.get("properties", {})
    if "opportunity_id" in properties and "opportunities" in TABLES:
        return "opportunities"
    return None


def _high_risk_check(
    resource: str,
    tool_names: Iterable[str],
    *,
    definitions: Mapping[str, ToolDefinition] | None = None,
) -> CheckResult:
    """Fail when any of `tool_names` could commit a high-risk change directly.

    Args:
        resource: The resource the row is reported against.
        tool_names: The tool names the turn called.
        definitions: Name-to-definition lookup; the real `BY_NAME` by default.
    """
    lookup = definitions if definitions is not None else BY_NAME
    names = sorted(set(tool_names))
    for name in names:
        definition = lookup.get(name)
        if definition is not None and _could_commit_high_risk_change(definition):
            return CheckResult(
                resource=resource,
                check="no_direct_high_risk_write",
                status="fail",
                detail=f"the turn called {name!r}, which could commit a high-risk change directly",
            )
    unknown = [name for name in names if name not in lookup]
    return CheckResult(
        resource=resource,
        check="no_direct_high_risk_write",
        status="pass",
        detail=(
            f"{len(names)} called tool(s) swept against the toolspec risk classes: {names!r}"
            + (f"; not toolspec tools (dispatch targets): {unknown!r}" if unknown else "")
        ),
    )


def _registered_tool_names() -> frozenset[str]:
    """Every tool any agent holds: the supervisor's direct tools, the roles, each registry."""
    names = set(DIRECT_TOOL_NAMES) | set(_SUBAGENT_ROLES)
    for role in _SUBAGENT_ROLES:
        names |= {tool.name for tool in tool_registry_for(role)}
    return frozenset(names)


# -- the rows one turn establishes -------------------------------------------------------------


def _mentions(payload: object, needle: str) -> bool:
    """Whether `needle` appears anywhere in the serialised `payload`."""
    return needle in json.dumps(payload, default=str)


def _completed(events: list[dict[str, Any]]) -> bool:
    kinds = {e.get("kind") for e in events}
    return "completed" in kinds and "error" not in kinds


def _roles_that_spent_tokens(events: list[dict[str, Any]]) -> frozenset[str] | None:
    """Roles named by a token-bearing `cost_lines` entry, or `None` when no event carried any."""
    lines = [
        line
        for event in events
        if event.get("kind") in ("usage", "completed") and isinstance(event.get("cost_lines"), list)
        for line in event["cost_lines"]
        if isinstance(line, Mapping)
    ]
    if not lines:
        return None
    return frozenset(
        str(line.get("role"))
        for line in lines
        if int(line.get("input_tokens") or 0) + int(line.get("output_tokens") or 0) > 0
    )


def _relay_completion_check(events: list[dict[str, Any]], kinds: list[str]) -> CheckResult:
    """The turn must terminate with `completed`; an `error` event is a reported failure."""
    check = "relay_turn_reaches_completion"
    errors = [e for e in events if e.get("kind") == "error"]
    if errors:
        first = errors[0]
        return CheckResult(
            resource=_RELAY_RESOURCE,
            check=check,
            status="fail",
            detail=(
                f"turn ended in an error event: error_kind={first.get('error_kind')!r} "
                f"message={first.get('error_message')!r}; kinds={kinds!r}"
            ),
        )
    if "completed" not in kinds:
        return CheckResult(
            resource=_RELAY_RESOURCE,
            check=check,
            status="fail",
            detail=f"no 'completed' event arrived, so the turn did not terminate: kinds={kinds!r}",
        )
    tool_calls = [e.get("tool_name") for e in events if e.get("kind") == "tool_call"]
    return CheckResult(
        resource=_RELAY_RESOURCE,
        check=check,
        status="pass",
        detail=(
            f"turn completed over CloudFront -> relay -> SigV4 -> Runtime; {len(events)} event(s), "
            f"tool_call(s)={tool_calls!r}"
        ),
    )


def _relay_isolation_check(events: list[dict[str, Any]], out_of_book_account: str) -> CheckResult:
    """No `tool_result` may carry the out-of-book account; zero results is a `defer`."""
    check = "no_out_of_book_row_in_any_tool_result"
    results = [e for e in events if e.get("kind") == "tool_result"]
    if not results:
        return CheckResult(
            resource=_RELAY_RESOURCE,
            check=check,
            status=DEFER,
            detail=(
                "the turn emitted no tool_result event, so no row crossed the boundary and nothing "
                "was observed; a pass here would assert isolation over zero observations"
            ),
            defer_reason="no_tool_result_observed",
        )
    leaked = [e for e in results if _mentions(e.get("tool_output"), out_of_book_account)]
    if leaked:
        return CheckResult(
            resource=_RELAY_RESOURCE,
            check=check,
            status="fail",
            detail=(
                f"{len(leaked)} of {len(results)} tool_result event(s) carried the out-of-book "
                f"account {out_of_book_account!r}, which the caller's book excludes"
            ),
        )
    return CheckResult(
        resource=_RELAY_RESOURCE,
        check=check,
        status="pass",
        detail=(
            f"{len(results)} tool_result event(s) observed, none carrying the out-of-book account "
            f"{out_of_book_account!r}"
        ),
    )


def _relay_vocabulary_check(kinds: list[str]) -> CheckResult:
    """Every event kind must be in the closed `EVENT_KINDS` vocabulary."""
    check = "every_event_kind_is_in_the_closed_vocabulary"
    unknown = sorted({k for k in kinds if k not in _TURN_EVENT_KINDS})
    if unknown:
        return CheckResult(
            resource=_RELAY_RESOURCE,
            check=check,
            status="fail",
            detail=f"event kind(s) outside the closed EVENT_KINDS set: {unknown!r}",
        )
    if not kinds:
        return CheckResult(
            resource=_RELAY_RESOURCE,
            check=check,
            status=DEFER,
            detail="no event arrived, so the vocabulary was not exercised",
            defer_reason="no_event_observed",
        )
    return CheckResult(
        resource=_RELAY_RESOURCE,
        check=check,
        status="pass",
        detail=f"{len(kinds)} event(s), every kind within the closed vocabulary: {sorted(set(kinds))!r}",
    )


def _responds_check(role: str, events: list[dict[str, Any]]) -> CheckResult:
    """An agent responds when the completed turn carries token-bearing cost lines for its role.

    The supervisor must have spent tokens on a completed turn; a subagent the turn did not delegate
    to is a `defer`.
    """
    spent = _roles_that_spent_tokens(events)
    completed = _completed(events)
    if role == SUPERVISOR_ROLE:
        if not completed:
            return CheckResult(
                resource=role,
                check="responds",
                status="fail",
                detail="the turn did not complete, so the supervisor did not answer it",
            )
        if spent is None:
            return CheckResult(
                resource=role,
                check="responds",
                status=DEFER,
                detail="the completed turn carried no cost_lines, so no role's spend was observed",
                defer_reason="no_cost_lines_observed",
            )
        return CheckResult(
            resource=role,
            check="responds",
            status="pass" if role in spent else "fail",
            detail=f"completed turn; roles with token-bearing cost_lines: {sorted(spent)!r}",
        )
    if completed and spent is not None and role in spent:
        return CheckResult(
            resource=role,
            check="responds",
            status="pass",
            detail=f"the turn delegated to {role!r}, which spent tokens: {sorted(spent)!r}",
        )
    return CheckResult(
        resource=role,
        check="responds",
        status=DEFER,
        detail=(
            f"{role!r} spent no tokens on this turn (roles that did: "
            f"{sorted(spent) if spent is not None else 'none reported'}), so it was not exercised"
        ),
        defer_reason="role_not_exercised_by_this_turn",
    )


def _called_tools(events: list[dict[str, Any]]) -> list[str]:
    return [str(e.get("tool_name")) for e in events if e.get("kind") == "tool_call"]


def _no_call_observed(check: str) -> CheckResult:
    return CheckResult(
        resource=SUPERVISOR_ROLE,
        check=check,
        status=DEFER,
        detail="the turn called no tool, so no call was observed to judge",
        defer_reason="no_tool_call_observed",
    )


def _surplus_check(called: list[str]) -> CheckResult:
    """Every tool the turn called must be one some agent holds."""
    if not called:
        return _no_call_observed("no_surplus_tool")
    surplus = sorted(set(called) - _registered_tool_names())
    return CheckResult(
        resource=SUPERVISOR_ROLE,
        check="no_surplus_tool",
        status="fail" if surplus else "pass",
        detail=(
            f"the turn called tool(s) no agent holds: {surplus!r}"
            if surplus
            else f"every one of the {len(called)} call(s) names a registered tool: {sorted(set(called))!r}"
        ),
    )


def _grid_rows(events: list[dict[str, Any]]) -> list[CheckResult]:
    called = _called_tools(events)
    rows: list[CheckResult] = []
    for role in (SUPERVISOR_ROLE, *_SUBAGENT_ROLES):
        for check in _PER_AGENT_CHECKS:
            reason = GRID_DEFER_REASONS.get((role, check))
            if reason is not None:
                rows.append(_declared_defer(role, check, reason))
            elif check == "responds":
                rows.append(_responds_check(role, events))
            elif check == "no_surplus_tool":
                rows.append(_surplus_check(called))
            elif not called:
                rows.append(_no_call_observed(check))
            else:
                rows.append(_high_risk_check(role, called))
    rows.extend(
        _declared_defer(_OUTCOME_RESOURCE, check, GRID_DEFER_REASONS[(_OUTCOME_RESOURCE, check)])
        for check in _OUTCOME_CHECKS
    )
    return rows


def _declared_defer(resource: str, check: str, reason: str) -> CheckResult:
    return CheckResult(
        resource=resource,
        check=check,
        status=DEFER,
        detail=_GRID_DEFER_DETAIL[reason],
        defer_reason=reason,
    )


def relay_turn_checks(
    run_turn: TurnRunner,
    *,
    prompt: str,
    out_of_book_account: str,
) -> list[CheckResult]:
    """Run one turn and return every row in :func:`_turn_rows` order.

    Args:
        run_turn: The turn seam.
        prompt: The turn's prompt; it names `out_of_book_account`.
        out_of_book_account: A real account outside the caller's book.

    Returns:
        One row per `(resource, check)`; a transport failure fails every row.
    """
    try:
        events = list(run_turn(prompt))
    except Exception as exc:  # noqa: BLE001 - a transport failure is a check failure, not a crash
        detail = f"turn failed: {type(exc).__name__}: {exc}"
        return [
            CheckResult(resource=resource, check=check, status="fail", detail=detail)
            for resource, check in _turn_rows()
        ]
    kinds = [str(e.get("kind", "")) for e in events]
    return [
        _relay_completion_check(events, kinds),
        _relay_isolation_check(events, out_of_book_account),
        _relay_vocabulary_check(kinds),
        *_grid_rows(events),
    ]


def _unrun_turn_rows(reason: str, detail: str) -> list[CheckResult]:
    return [
        CheckResult(
            resource=resource, check=check, status=DEFER, detail=detail, defer_reason=reason
        )
        for resource, check in _turn_rows()
    ]


# -- the live turn ------------------------------------------------------------------------------


def _event_payloads(chunks: Iterable[bytes]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in parse_sse_bytes(chunks):
        try:
            decoded = json.loads(event.data)
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, dict):
            payloads.append(decoded)
    return payloads


def _cloudfront_turn_runner(ctx: SmokeContext, token: str) -> TurnRunner:
    def run(prompt: str) -> list[dict[str, Any]]:
        session_id = f"smoke-agents-{uuid.uuid4().hex}"
        body = {"prompt": prompt, "session_id": session_id, "resume_from": 0}
        source = cloudfront_source(ctx, token=token, body=body, session_id=session_id)
        return _event_payloads(source())

    return run


def out_of_book_account() -> str:
    """A real `demo` account id; the smoke caller's tenant and empty book exclude it."""
    from fixtures.demo_seed import demo_account_ids

    return demo_account_ids(DEMO_TENANT)[0]


def turn_prompt(account_id: str) -> str:
    """The turn's prompt: a read of `account_id`, which the caller is not entitled to."""
    return f"Look up account {account_id} and summarise its open opportunities."


def checks(
    ctx: SmokeContext,
    *,
    provisioner_factory: Callable[[SmokeContext], LiveStreamPrincipalProvisioner] = (
        LiveStreamPrincipalProvisioner
    ),
    runner_factory: Callable[[SmokeContext, str], TurnRunner] = _cloudfront_turn_runner,
) -> Iterable[CheckResult]:
    """Provision a disposable principal, run one counted turn, prove `demo` untouched, clean up.

    Every row in :func:`_turn_rows` is emitted on every path: a turn that did not run is a `defer`
    naming why. The factories are injectable for the offline test.
    """
    budget = ModelTurnBudget(RESOURCE_CLASS)
    provisioner = provisioner_factory(ctx)
    reader = store_row_reader(provisioner.store())
    demo_before = read_tenant_snapshot(reader, DEMO_TENANT)
    results: list[CheckResult] = []
    try:
        try:
            token = provisioner.provision()
        except Exception as exc:  # noqa: BLE001 - reported as a failing check, never a crash
            results.append(
                CheckResult(
                    resource="smoke_principal",
                    check="mfa_access_token_issued",
                    status="fail",
                    detail=f"could not provision the smoke principal: {type(exc).__name__}: {exc}",
                )
            )
            results.extend(
                _unrun_turn_rows(
                    "upstream_check_did_not_run",
                    "the turn did not run because the smoke principal could not be provisioned",
                )
            )
        else:
            results.append(
                CheckResult(
                    resource="smoke_principal",
                    check="mfa_access_token_issued",
                    status="pass",
                    detail=f"access token names the disposable tenant {provisioner.tenant()!r}",
                )
            )
            label = "relay turn"
            if budget.spend(label):
                account = out_of_book_account()
                results.extend(
                    relay_turn_checks(
                        runner_factory(ctx, token),
                        prompt=turn_prompt(account),
                        out_of_book_account=account,
                    )
                )
            else:
                results.append(budget_refused(budget, label))
                results.extend(
                    _unrun_turn_rows(
                        "model_turn_share_spent",
                        "the turn was not issued because this module's model-turn share is spent",
                    )
                )
    finally:
        results.append(budget.result())
        results.append(
            demo_tenant_untouched_check(resource=RESOURCE_CLASS, before=demo_before, reader=reader)
        )
        if provisioner.tenant():
            results.append(
                writes_only_under_smoke_tenant_check(
                    resource=RESOURCE_CLASS, tenant=provisioner.tenant(), reader=reader
                )
            )
        results.extend(provisioner.cleanup())
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the relay-backed agent checks and return 0 pass / 1 fail.

    Declares no `--token`, so `scripts/verify_deployment.sh` does not pass it one.
    """
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
