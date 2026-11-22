# SPDX-License-Identifier: MIT
"""Delete the conversation-history items smoke turns write, and find any a run left behind.

The Runtime keeps one `reps` item per session at `history#<tenant>#<session_id>`
(:func:`crm.store.keys.history_key`). The items carry no `tenant` attribute, so smoke-tenant
teardown never sees them: every module that sends a chat turn records the sessions it used and
calls :func:`delete_session_history` in a `finally`. Deleting an absent item is not an error.

Run as a module, it is `scripts/verify_deployment.sh`'s whole-run check, issued after every layer:
a strongly consistent scan of `reps` for keys beginning :data:`SMOKE_HISTORY_KEY_PREFIX`. Every item
found is deleted and re-read before the row is reported, and any item found fails the row.

Usage:
    python3 -m scripts.smoke.history_cleanup [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Final, Protocol

from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    exception_detail,
    run_main,
)

from crm.store.keys import history_key

#: The table holding the Runtime's conversation-history items.
HISTORY_TABLE: Final[str] = "reps"

#: The key prefix of every history item written under a disposable smoke tenant.
SMOKE_HISTORY_KEY_PREFIX: Final[str] = "history#eval-smoke-"

HISTORY_ITEMS_DELETED_CHECK: Final[str] = "history_items_deleted"

#: The whole-run row's resource class and check name.
RESIDUE_RESOURCE_CLASS: Final[str] = "smoke_history_residue"
RESIDUE_CHECK: Final[str] = "no_smoke_history_left_after_run"

#: The inventory layer of the whole-run row; layer 0 is the run-level residue position.
_WHOLE_RUN_LAYER: Final[int] = 0


class HistoryItems(Protocol):
    """Operator access to the `reps` history items, by their `rep_id` key."""

    def get(self, rep_id: str) -> Any:
        """The item at `rep_id`, strongly consistent, or `None`."""
        ...

    def delete(self, rep_id: str) -> None:
        """Delete the item at `rep_id`; absent is not an error."""
        ...


class ScannableHistoryItems(HistoryItems, Protocol):
    """`HistoryItems` that can also list keys by prefix."""

    def keys_with_prefix(self, prefix: str) -> list[str]:
        """Every `rep_id` beginning `prefix`, from a strongly consistent scan."""
        ...


#: `(ctx) -> HistoryItems`. Live default addresses the deployed `reps` table.
HistoryItemsFactory = Callable[[SmokeContext], HistoryItems]
#: `(ctx) -> ScannableHistoryItems`, for the whole-run check.
ScannableHistoryItemsFactory = Callable[[SmokeContext], ScannableHistoryItems]


class LiveHistoryItems:
    """`ScannableHistoryItems` over the deployed `reps` table, as the operator credential."""

    def __init__(self, ctx: SmokeContext) -> None:
        from crm.store.store import Store

        names = ctx.outputs.require_mapping("dynamodb_table_names")
        accounts = str(names["accounts"])
        self._store = Store(
            client=ctx.client("dynamodb"), table_prefix=accounts.removesuffix("accounts")
        )

    def get(self, rep_id: str) -> Any:
        return self._store.get_item(HISTORY_TABLE, {"rep_id": rep_id}, consistent=True)

    def delete(self, rep_id: str) -> None:
        self._store.delete_item(HISTORY_TABLE, {"rep_id": rep_id})

    def keys_with_prefix(self, prefix: str) -> list[str]:
        return sorted(
            str(item["rep_id"])
            for item in self._store.scan_key_prefix(HISTORY_TABLE, "rep_id", prefix)
        )


def _delete_and_reread(items: HistoryItems, keys: Iterable[str]) -> list[str]:
    """Delete each key, then return those a consistent re-read still finds."""
    remaining: list[str] = []
    for key in keys:
        items.delete(key)
        if items.get(key) is not None:
            remaining.append(key)
    return remaining


def delete_history_items(items: HistoryItems, keys: Sequence[str], *, resource: str) -> CheckResult:
    """Delete every history item in `keys` and prove each absent by a consistent re-read."""
    unique = list(dict.fromkeys(keys))
    try:
        remaining = _delete_and_reread(items, unique)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
        return CheckResult(
            resource=resource,
            check=HISTORY_ITEMS_DELETED_CHECK,
            status="fail",
            detail=f"could not delete the history items: {exception_detail(exc)}",
        )
    return CheckResult(
        resource=resource,
        check=HISTORY_ITEMS_DELETED_CHECK,
        status="fail" if remaining else "pass",
        detail=(
            f"{len(unique)} history key(s) deleted, {len(remaining)} still present on re-read"
            + (f": {remaining}" if remaining else "")
        ),
    )


def delete_session_history(
    ctx: SmokeContext,
    items_factory: HistoryItemsFactory,
    sessions_by_tenant: Iterable[tuple[str, Sequence[str]]],
    *,
    resource: str,
) -> list[CheckResult]:
    """Delete the history item of every `(tenant, session)` a turn was sent with.

    Returns no row when no session was used, one `history_items_deleted` row otherwise. Every
    addressable item is deleted even when some session's tenant is empty (never allocated); such a
    session cannot be addressed, so it fails the row, with its count in the detail.
    """
    keys: list[str] = []
    unaddressable = 0
    for tenant, sessions in sessions_by_tenant:
        for session in sessions:
            if tenant and session:
                keys.append(history_key(tenant, session))
            else:
                unaddressable += 1
    if not keys and not unaddressable:
        return []
    unaddressable_note = (
        f"; {unaddressable} session(s) with no known tenant could not be addressed, so the "
        "whole-run residue check will find any item they left"
        if unaddressable
        else ""
    )
    if not keys:
        return [
            CheckResult(
                resource=resource,
                check=HISTORY_ITEMS_DELETED_CHECK,
                status="fail",
                detail=f"0 history key(s) deleted{unaddressable_note}",
            )
        ]
    try:
        items = items_factory(ctx)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
        return [
            CheckResult(
                resource=resource,
                check=HISTORY_ITEMS_DELETED_CHECK,
                status="fail",
                detail=f"could not address the history items: {exception_detail(exc)}"
                + unaddressable_note,
            )
        ]
    row = delete_history_items(items, keys, resource=resource)
    if not unaddressable:
        return [row]
    return [dataclasses.replace(row, status="fail", detail=row.detail + unaddressable_note)]


def leftover_history_check(items: ScannableHistoryItems) -> CheckResult:
    """Fail when any smoke-tenant history item exists, deleting each one before reporting."""
    try:
        found = items.keys_with_prefix(SMOKE_HISTORY_KEY_PREFIX)
        remaining = _delete_and_reread(items, found)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
        return CheckResult(
            resource=HISTORY_TABLE,
            check=RESIDUE_CHECK,
            status="fail",
            detail=f"could not scan or clean the smoke history items: {exception_detail(exc)}",
        )
    if not found:
        return CheckResult(
            resource=HISTORY_TABLE,
            check=RESIDUE_CHECK,
            status="pass",
            detail=f"consistent scan of {HISTORY_TABLE} found no {SMOKE_HISTORY_KEY_PREFIX}* item",
        )
    return CheckResult(
        resource=HISTORY_TABLE,
        check=RESIDUE_CHECK,
        status="fail",
        detail=(
            f"{len(found)} {SMOKE_HISTORY_KEY_PREFIX}* item(s) outlived their module and were "
            f"deleted now; {len(remaining)} still present on re-read: {found}"
        ),
    )


def checks(
    ctx: SmokeContext, *, history_items: ScannableHistoryItemsFactory = LiveHistoryItems
) -> Iterable[CheckResult]:
    """Yield the single whole-run residue row."""
    try:
        items = history_items(ctx)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
        yield CheckResult(
            resource=HISTORY_TABLE,
            check=RESIDUE_CHECK,
            status="fail",
            detail=f"could not address the {HISTORY_TABLE} table: {exception_detail(exc)}",
        )
        return
    yield leftover_history_check(items)


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """Exactly the one residue row, on every path."""
    return ExpectedCheckCount(minimum=1, basis="one residue row, emitted on every path")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the whole-run residue check and return 0 pass / 1 fail."""
    return run_main(
        argv,
        layer=_WHOLE_RUN_LAYER,
        resource_class=RESIDUE_RESOURCE_CLASS,
        description=__doc__ or RESIDUE_RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


__all__ = [
    "HISTORY_ITEMS_DELETED_CHECK",
    "HISTORY_TABLE",
    "RESIDUE_CHECK",
    "RESIDUE_RESOURCE_CLASS",
    "SMOKE_HISTORY_KEY_PREFIX",
    "HistoryItems",
    "HistoryItemsFactory",
    "LiveHistoryItems",
    "ScannableHistoryItems",
    "checks",
    "delete_history_items",
    "delete_session_history",
    "leftover_history_check",
    "main",
]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
