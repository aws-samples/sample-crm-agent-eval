# SPDX-License-Identifier: MIT
"""Layer 5 check: `dynamodb:DeleteItem` is denied to the role that tears eval tenants down.

`Store.delete_tenant` issues a `Query` and then `batch_write_item` with `DeleteRequest` entries, so
`STORE_METHOD_ACTIONS["delete_tenant"]` is `{Query, BatchWriteItem}` without `dynamodb:DeleteItem`.
This module is the necessity half: `dynamodb:DeleteItem` on each tenant-scoped table, simulated
against the `sweep_control` role (the state machine's `Teardown` step), must be denied. Each deny
row rests on three controls: the action string is
expressible (a matrix holder is `allowed` it on a table it is granted), the table ARN matches the
teardown grant (the role is `allowed` teardown's own `Query` on that table's `gsi_tenant` index,
under an `eval-*` `dynamodb:LeadingKeys` context), and, where a matrix role holds `DeleteItem` on
that table, that holder is `allowed` it there. The role must also hold `BatchWriteItem`.

`teardown_was_not_vacuous` checks that the committed fixture corpus the seeder writes into every
eval tenant is non-empty, so an empty tenant after teardown is evidence. The sufficiency half, a
tenant emptied by a teardown that ran, is `eval_sfn`'s `tenant_emptied_by_the_workers_own_role`
on its stub rehearsal. No tenant is written and no sweep is started here.

Usage:
    python3 -m scripts.smoke.integration_teardown_privilege [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import json
import sys
from collections.abc import Generator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final

from scripts.smoke.harness import CheckResult, ExpectedCheckCount, SmokeContext, run_main
from scripts.smoke.iam_roles import (
    _function_grants,
    _load_matrix,
    _role_name,
    _simulate,
    _simulate_with_leading_keys,
)

from eval.tenancy import tenant_for_run

LAYER: Final[int] = 5
RESOURCE_CLASS: Final[str] = "integration_teardown_privilege"

#: The action asserted denied. One spelling, used by the deny row and its positive control alike.
ACTION_DELETE_ITEM: Final[str] = "dynamodb:DeleteItem"

#: The two actions teardown uses, asserted `allowed` for the teardown role.
ACTION_QUERY: Final[str] = "dynamodb:Query"
ACTION_BATCH_WRITE: Final[str] = "dynamodb:BatchWriteItem"

#: The index `Store.delete_tenant` enumerates every tenant-scoped table through.
TENANT_INDEX: Final[str] = "gsi_tenant"

#: The `dynamodb:LeadingKeys` value teardown's `Query` carries: an eval tenant's partition key.
_PROBE_TENANT: Final[str] = tenant_for_run("smoke-teardown-privilege")

#: Decisions that satisfy a deny row. `no_result` is not one of them.
DENIED: Final[frozenset[str]] = frozenset({"explicitDeny", "implicitDeny"})

#: The function whose role runs the state machine's `Teardown` step.
TEARDOWN_FUNCTION: Final[str] = "sweep-control"

__all__ = [
    "ACTION_BATCH_WRITE",
    "ACTION_DELETE_ITEM",
    "ACTION_QUERY",
    "DENIED",
    "LAYER",
    "RESOURCE_CLASS",
    "checks",
    "delete_item_holders",
    "main",
]


def delete_item_holders(account_id: str) -> dict[str, str]:
    """`{logical_table: role_arn}` of an action-Lambda role holding `dynamodb:DeleteItem` there.

    Resolved from the grant matrix, first function in sorted order per table. A table no role holds
    `DeleteItem` on is absent: a holder is only a positive control on a table it is granted.
    """
    grants = _function_grants(_load_matrix())
    holders: dict[str, str] = {}
    for function, entries in sorted(grants.items()):
        for entry in entries:
            table = entry.get("table")
            actions = entry.get("actions") or []
            # The matrix spells actions fully qualified (`dynamodb:DeleteItem`).
            if (
                isinstance(table, str)
                and isinstance(actions, list)
                and ACTION_DELETE_ITEM in actions
            ):
                holders.setdefault(table, f"arn:aws:iam::{account_id}:role/{_role_name(function)}")
    return holders


def _teardown_role_arn(ctx: SmokeContext, account_id: str) -> str:
    """The `TEARDOWN_FUNCTION` role ARN, rendered through `ROLE_NAME_TEMPLATE`.

    The function's existence is confirmed from the `eval_state_machine_arn` output, which raises
    when absent; the role has no output of its own.
    """
    ctx.outputs.require_str("eval_state_machine_arn")
    return f"arn:aws:iam::{account_id}:role/{_role_name(TEARDOWN_FUNCTION)}"


def _table_arns(ctx: SmokeContext, account_id: str) -> dict[str, str]:
    """`{logical_table: arn}` for every tenant-scoped table, from the deployed name mapping."""
    from crm.store.schema import TENANT_SCOPED_TABLES

    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    arns: dict[str, str] = {}
    for logical in sorted(TENANT_SCOPED_TABLES):
        physical = table_names.get(logical)
        if isinstance(physical, str) and physical:
            arns[logical] = f"arn:aws:dynamodb:{ctx.region}:{account_id}:table/{physical}"
    return arns


#: The committed fixture corpus the seeder writes into every eval tenant.
_FIXTURE_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "fixtures"


def _fixture_record_count() -> int:
    """Total records in the committed fixture corpus the seeder writes into every eval tenant."""
    total = 0
    for path in sorted(_FIXTURE_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            total += len(payload)
        elif isinstance(payload, Mapping):
            total += sum(len(v) for v in payload.values() if isinstance(v, list))
    return total


def _simulated_results(
    ctx: SmokeContext, *, worker_role_arn: str, account_id: str
) -> Iterator[CheckResult]:
    """The necessity half: `DeleteItem` denied per table, each resting on its controls."""
    holders = delete_item_holders(account_id)
    table_arns = _table_arns(ctx, account_id)

    first_arn = next(iter(table_arns.values()), "")
    if first_arn:
        decision, _ = _simulate(ctx, worker_role_arn, ACTION_BATCH_WRITE, first_arn)
        yield CheckResult(
            resource=first_arn,
            check="worker_holds_batchwriteitem",
            status="pass" if decision == "allowed" else "fail",
            detail=(
                f"{ACTION_BATCH_WRITE} evaluated {decision} for the teardown role -- the narrowed "
                "mapping keeps {Query, BatchWriteItem}, so a deny here means teardown could not run "
                "on the path eval_sfn's tenant_emptied_by_the_workers_own_role exercises"
            ),
        )

    action_expressible = yield from _delete_item_expressibility(ctx, holders, table_arns)

    for logical, table_arn in table_arns.items():
        index_arn = f"{table_arn}/index/{TENANT_INDEX}"
        query_decision = _simulate_with_leading_keys(
            ctx, worker_role_arn, ACTION_QUERY, index_arn, [_PROBE_TENANT]
        )
        arn_is_real = query_decision == "allowed"
        yield CheckResult(
            resource=index_arn,
            check=f"worker_holds_query[{logical}]",
            status="pass" if arn_is_real else "fail",
            detail=(
                f"{ACTION_QUERY} on {TENANT_INDEX} with dynamodb:LeadingKeys={_PROBE_TENANT!r} "
                f"evaluated {query_decision} for the teardown role"
                + (
                    " -- teardown's own Query is held here, so this table's name matches the "
                    "teardown grant and the DeleteItem deny below is about the grant, not a "
                    "mistyped ARN"
                    if arn_is_real
                    else ", expected 'allowed'. Teardown cannot enumerate this table, and a "
                    "mistyped table name would also read implicitDeny, so the deny row below is "
                    "failed"
                )
            ),
        )

        holder_ok = True
        holder = holders.get(logical)
        if holder is not None:
            positive_decision, _ = _simulate(ctx, holder, ACTION_DELETE_ITEM, table_arn)
            holder_ok = positive_decision == "allowed"
            yield CheckResult(
                resource=table_arn,
                check=f"delete_item_denied[{logical}]_positive_control",
                status="pass" if holder_ok else "fail",
                detail=(
                    f"{holder.rpartition('/')[2]}, which the matrix grants {ACTION_DELETE_ITEM} on "
                    f"this table, evaluated {positive_decision}"
                    + (
                        " -- the pair is expressible on this exact ARN"
                        if holder_ok
                        else ", expected 'allowed'. Without an expressible pair a deny cannot be "
                        "distinguished from a typo, so the deny row is failed alongside this one"
                    )
                ),
            )

        controls_hold = action_expressible and arn_is_real and holder_ok
        deny_decision, _ = _simulate(ctx, worker_role_arn, ACTION_DELETE_ITEM, table_arn)
        satisfied = deny_decision in DENIED and controls_hold
        if satisfied:
            outcome = (
                "; with eval_sfn's tenant_emptied_by_the_workers_own_role, this settles that "
                "teardown does not require DeleteItem -- the deny establishes necessity, the "
                "emptied tenant sufficiency, and neither does so alone"
            )
        elif deny_decision in DENIED:
            outcome = (
                f", but a control it rests on failed (action expressible: {action_expressible}, "
                f"table ARN matches grant: {arn_is_real}, per-table holder allowed: {holder_ok}), so the "
                "deny cannot be distinguished from a malformed pair"
            )
        else:
            outcome = (
                f", expected one of {sorted(DENIED)} on an expressible pair. If this "
                "is 'allowed', the grant the mapping edit removed is still deployed, and a "
                "successful teardown proves nothing about the narrowed set"
            )
        yield CheckResult(
            resource=table_arn,
            check=f"delete_item_denied[{logical}]",
            status="pass" if satisfied else "fail",
            detail=f"{ACTION_DELETE_ITEM} evaluated {deny_decision} for the teardown role{outcome}",
        )


def _delete_item_expressibility(
    ctx: SmokeContext, holders: Mapping[str, str], table_arns: Mapping[str, str]
) -> Generator[CheckResult, None, bool]:
    """Yield the one `delete_item_action_expressible` row; return whether it passed.

    Simulates a matrix holder on a deployed table it is granted `DeleteItem` on, never on a table
    it is not, where cross-function isolation denies it by design.
    """
    granted = next(((t, holders[t]) for t in table_arns if t in holders), None)
    if granted is None:
        yield CheckResult(
            resource="dynamodb:DeleteItem",
            check="delete_item_action_expressible",
            status="fail",
            detail=(
                "no role in the grant matrix holds dynamodb:DeleteItem on any deployed tenant-scoped "
                "table, so nothing can establish the action string is expressible and every deny "
                "below would be self-certifying. Failing rather than skipping: the matrix is "
                "supposed to carry DeleteItem for approve_proposal and revert_audit_record"
            ),
        )
        return False
    table, holder = granted
    decision, _ = _simulate(ctx, holder, ACTION_DELETE_ITEM, table_arns[table])
    expressible = decision == "allowed"
    yield CheckResult(
        resource=table_arns[table],
        check="delete_item_action_expressible",
        status="pass" if expressible else "fail",
        detail=(
            f"{holder.rpartition('/')[2]} evaluated {decision} for {ACTION_DELETE_ITEM} on "
            f"{table!r}, a table the matrix grants it"
            + (
                " -- the action string is expressible, for every table's deny row below"
                if expressible
                else ", expected 'allowed'. The action string is unproven, so every DeleteItem "
                "deny row below is failed"
            )
        ),
    )
    return expressible


def checks(ctx: SmokeContext) -> Iterator[CheckResult]:
    """Yield the simulated `DeleteItem` deny rows with their controls, then the non-vacuity row."""
    account_id = str(ctx.client("sts").get_caller_identity()["Account"])
    try:
        worker_role_arn = _teardown_role_arn(ctx, account_id)
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, not a crash
        yield CheckResult(
            resource="eval_state_machine_arn",
            check="terraform_output_present",
            status="fail",
            detail=(
                f"required Terraform output 'eval_state_machine_arn' is absent ({exc}); without the "
                f"sweep state machine there is no {TEARDOWN_FUNCTION} role to simulate every "
                "DeleteItem deny row against. A missing output is a failure, not a skipped check"
            ),
        )
        return

    yield from _simulated_results(ctx, worker_role_arn=worker_role_arn, account_id=account_id)
    yield _non_vacuity_result()


def _non_vacuity_result() -> CheckResult:
    """The fixture corpus every eval tenant is seeded with is non-empty."""
    seeded_record_count = _fixture_record_count()
    return CheckResult(
        resource=str(_FIXTURE_DIR.name),
        check="teardown_was_not_vacuous",
        status="pass" if seeded_record_count > 0 else "fail",
        detail=(
            f"the fixture corpus the seeder writes into every eval tenant holds "
            f"{seeded_record_count} record(s), so eval_sfn's emptied rehearsal tenant is a "
            "teardown of real rows. A teardown of a tenant that never held anything succeeds "
            "while proving nothing"
            if seeded_record_count > 0
            else "the fixture corpus is empty, so every eval tenant holds nothing from the start "
            "and 'zero rows remain' asserts nothing at all"
        ),
    )


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """The floor, derived from the tables the deny rows iterate."""
    try:
        table_arns = _table_arns(ctx, "expectation-only")
        holders = delete_item_holders("expectation-only")
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the basis, not in a traceback
        return ExpectedCheckCount(
            minimum=1,
            basis=(
                f"the tenant-scoped table mapping could not be read ({type(exc).__name__}), so no "
                "per-table expectation is derivable and this falls back to the weakest floor"
            ),
        )
    tables = len(table_arns)
    holder_tables = len(set(table_arns) & set(holders))
    batch_write_rows = 1 if tables else 0
    return ExpectedCheckCount(
        minimum=2 * tables + holder_tables + 1 + batch_write_rows + 1,
        basis=(
            f"2 rows x {tables} tenant-scoped table(s) resolvable from dynamodb_table_names (each "
            f"DeleteItem deny plus the teardown role's Query on that ARN) + {holder_tables} per-table "
            "holder control(s) for the tables a matrix role holds DeleteItem on + 1 action-"
            f"expressibility row + {batch_write_rows} teardown-role-holds-BatchWriteItem row + 1 "
            "teardown_was_not_vacuous row"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the teardown least-privilege check and return the exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
