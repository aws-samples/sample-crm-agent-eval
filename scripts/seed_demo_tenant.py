#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Seed the ``demo`` tenant non-interactively, and prove the seeding was idempotent.

Usage:
    python3 scripts/seed_demo_tenant.py [--table-prefix PREFIX] [--profile P] [--region R]
                                        [--endpoint-url URL] [--outputs PATH] [--json]

A wrapper around :func:`eval.tenancy.seed_demo_tenant`, the sanctioned ``demo``-writing entry point:
it resolves a store, calls that function, and verifies the result. It performs no write of its own.
There is no ``--tenant`` flag; the target is always :data:`crm.authz.DEMO_TENANT`, so this script
cannot seed ``canary`` or an ``eval-<run_id>`` tenant.

What it verifies, in order, before reporting success:

1. **Idempotency.** The corpus is seeded twice and the tenant's per-table row census, read back from
   the store, must be unchanged across the two passes.
2. **Determinism of the record identity set.** The two :class:`fixtures.demo_seed.DemoSeedSummary`
   values must be equal.
3. **A non-empty tenant.** An empty tenant satisfies checks 1 and 2 vacuously, and a corpus written
   without the ``gsi_tenant`` sort key reads back as empty.
4. **The bystander is still untouched**, via :func:`eval.tenancy.assert_canary_empty`.

Exit status:

* ``0`` — ``demo`` was seeded, holds rows, re-seeding changed nothing, and ``canary`` is still empty.
* ``1`` — seeding failed, the tenant read back empty, re-seeding changed the census, the two summaries
  differed, ``canary`` holds a row, the store could not be constructed for a reason that is not an
  absent credential or an unreachable endpoint, or the seeding succeeded but its verification could
  not be completed. In every case the reason states whether anything was written.
* ``2`` — usage error (argparse).
* ``3`` — the store was never reached or is not provisioned: no credential, no configured profile, no
  reachable endpoint, absent tables, or no ``--outputs`` for a remote run. Nothing was seeded and
  nothing verified. A store that answered with ``AccessDenied``, a ``ValidationException``, or a
  throttle exits ``1`` (:func:`is_unreachable_failure`).

Seeding runs twice (about 1,500 rows per pass), so against a real AWS account this script is billable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
for _import_root in (REPO_ROOT / "src", REPO_ROOT):  # pragma: no cover - import bootstrap
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from botocore.exceptions import (  # noqa: E402
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    NoCredentialsError,
    ProfileNotFound,
)

from crm.authz import DEMO_TENANT  # noqa: E402
from crm.errors import CrmError, NotFound, ServiceBusy, TableNotFound  # noqa: E402
from crm.store.schema import TENANT_SCOPED_TABLES  # noqa: E402
from crm.store.store import Store  # noqa: E402
from eval.tenancy import (  # noqa: E402
    CanaryTenantNotEmptyError,
    DemoSeeder,
    assert_canary_empty,
)
from eval.tenancy import seed_demo_tenant as seed_demo_tenant_via_sanctioned_door  # noqa: E402

#: This script's machine-readable name, carried in the ``--json`` report.
SCRIPT_NAME: Final[str] = "seed_demo_tenant"

#: The one tenant this script writes.
TARGET_TENANT: Final[str] = DEMO_TENANT

#: Exit status meaning "ran, verified nothing", rendered ``[SKIP]`` by ``scripts/run_gates.py``.
SKIP_EXIT_STATUS: Final[int] = 3

#: Environment variable naming the DynamoDB table prefix.
TABLE_PREFIX_ENV_VAR: Final[str] = "CRM_TABLE_PREFIX"

#: Every store-layer failure this script recognises.
_STORE_FAILURES: Final[tuple[type[Exception], ...]] = (BotoCoreError, ClientError, CrmError)

#: The failures that prove the store was never reached or never provisioned; exit ``3`` turns on
#: exactly this set. Must stay equal in meaning to ``scripts/check_canary_tenant_empty.py``'s tuple.
_UNREACHABLE_FAILURES: Final[tuple[type[Exception], ...]] = (
    NoCredentialsError,
    EndpointConnectionError,
    ConnectTimeoutError,
    ProfileNotFound,
    NotFound,
    TableNotFound,
)


#: SDK transport failures that `Store` re-raises as `ServiceBusy`; found through the cause chain.
_CONNECTION_CAUSES: Final[tuple[type[Exception], ...]] = (
    EndpointConnectionError,
    ConnectTimeoutError,
    HTTPClientError,
)


def _raised_from_connection_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen:
        if isinstance(current, _CONNECTION_CAUSES):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def is_unreachable_failure(exc: BaseException) -> bool:
    """Whether ``exc`` proves the store was never reached or never provisioned.

    ``True`` earns the exit-``3`` "verified nothing" disposition; anything else is a failure of a
    store that answered.

    A :class:`crm.errors.ServiceBusy` that :class:`crm.store.store.Store` raised from a connection
    failure (:data:`_CONNECTION_CAUSES` anywhere in its cause chain) counts as unreachable too; one
    with any other cause, or none, is an answer.
    """
    if isinstance(exc, _UNREACHABLE_FAILURES):
        return True
    return isinstance(exc, ServiceBusy) and _raised_from_connection_failure(exc)


class CensusUnreadableError(RuntimeError):
    """Raised when a tenant's row census could not be completed.

    Attributes:
        cause: The underlying failure, also set as ``__cause__``.
    """

    def __init__(self, message: str, *, cause: BaseException) -> None:
        super().__init__(message)
        self.cause = cause

    @property
    def unreachable(self) -> bool:
        """Whether the underlying failure means the store was never reached or never provisioned."""
        return is_unreachable_failure(self.cause)


def census_tenant(store: Store, *, tenant: str) -> dict[str, int]:
    """Return the number of rows beneath ``tenant`` per tenant-scoped table.

    Walks every table in :data:`crm.store.schema.TENANT_SCOPED_TABLES` — the same set
    :meth:`crm.store.store.Store.delete_tenant` walks — through the tenant-wide index, paginating
    fully. Tables holding no row are present in the result with a count of ``0``.

    Raises:
        CensusUnreadableError: Any table could not be read. Never returns a partial count.
    """
    counts: dict[str, int] = {}
    for table in sorted(TENANT_SCOPED_TABLES):
        total = 0
        cursor: dict[str, object] | None = None
        while True:
            try:
                items, next_cursor = store.query_tenant_scan(
                    table, tenant=tenant, exclusive_start_key=cursor
                )
            except _STORE_FAILURES as exc:
                raise CensusUnreadableError(
                    f"could not count rows beneath tenant {tenant!r} in table {table!r} "
                    f"({type(exc).__name__}: {exc})",
                    cause=exc,
                ) from exc
            total += len(items)
            if next_cursor is None:
                break
            if next_cursor == cursor:
                # DynamoDB always advances the last evaluated key; a repeated one is an error.
                raise CensusUnreadableError(
                    f"the tenant scan of table {table!r} returned a pagination cursor that did not "
                    f"advance after {total} row(s); refusing to loop indefinitely",
                    cause=RuntimeError("non-advancing pagination cursor"),
                )
            cursor = next_cursor
        counts[table] = total
    return counts


def describe_census_delta(before: dict[str, int], after: dict[str, int]) -> str:
    """Render the per-table differences between two censuses, or ``""`` when they are equal.

    Names every table whose count changed, with both values.
    """
    changed = [
        f"{table}:{before.get(table, 0)}->{after.get(table, 0)}"
        for table in sorted(set(before) | set(after))
        if before.get(table, 0) != after.get(table, 0)
    ]
    return ", ".join(changed)


#: The closed set of outcomes.
SeedStatus = Literal["pass", "fail", "unverified", "skip"]


@dataclass(frozen=True, slots=True)
class SeedReport:
    """The outcome of one run of this script.

    Attributes:
        status: ``"pass"``, ``"fail"``, ``"unverified"``, or ``"skip"``.
        tenant: The tenant targeted. Always :data:`TARGET_TENANT`.
        passes_completed: How many seeding passes finished: ``0`` for a skip, ``2`` for a pass, and
            ``0``-``2`` on a failure or an unverified run.
        census_after_first: Per-table row counts after the first pass, when it was readable.
        census_after_second: Per-table row counts after the second pass, when it was readable.
        reason: Why a non-pass happened. Always states whether anything was written.
    """

    status: SeedStatus
    tenant: str = TARGET_TENANT
    passes_completed: int = 0
    census_after_first: dict[str, int] = field(default_factory=dict)
    census_after_second: dict[str, int] = field(default_factory=dict)
    reason: str = ""

    def exit_code(self) -> int:
        """Return ``0`` for a pass, :data:`SKIP_EXIT_STATUS` for a skip, ``1`` for anything else."""
        if self.status == "pass":
            return 0
        if self.status == "skip":
            return SKIP_EXIT_STATUS
        return 1

    def to_json_dict(self) -> dict[str, Any]:
        """The pinned ``--json`` shape."""
        return {
            "script": SCRIPT_NAME,
            "tenant": self.tenant,
            "status": self.status,
            "passes_completed": self.passes_completed,
            "census_after_first": dict(sorted(self.census_after_first.items())),
            "census_after_second": dict(sorted(self.census_after_second.items())),
            "total_rows": sum(self.census_after_second.values()),
            "reason": self.reason,
        }

    def render_text(self) -> str:
        """One human-readable line for this outcome."""
        if self.status == "pass":
            counts = dict(sorted(self.census_after_second.items()))
            return (
                f"[ pass ] {SCRIPT_NAME}: tenant {self.tenant!r} seeded and re-seeded with no "
                f"change; {sum(counts.values())} row(s) across {len(counts)} table(s): {counts}"
            )
        if self.status == "skip":
            return f"[SKIP] {SCRIPT_NAME}: {self.reason} Nothing was seeded and nothing verified."
        label = "FAIL" if self.status == "fail" else "UNVERIFIED"
        return f"[{label}] {SCRIPT_NAME}: {self.reason}"


def resolve_table_prefix(flag: str | None = None, env: dict[str, str] | None = None) -> str:
    """Resolve the DynamoDB table prefix: flag, then :data:`TABLE_PREFIX_ENV_VAR`, then empty.

    An empty prefix is not an error.
    """
    if flag is not None:
        return flag
    environment = env if env is not None else dict(os.environ)
    return environment.get(TABLE_PREFIX_ENV_VAR, "")


class UnverifiedTargetError(RuntimeError):
    """A remote write was asked for with no outputs document naming its target."""


def build_store(
    *,
    table_prefix: str,
    profile: str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
    outputs: str | None = None,
) -> Store:
    """Build a :class:`Store`, resolving profile and region the repository's way (flag, env, config).

    A remote build (no ``endpoint_url``) requires an ``outputs`` document and checks the resolved
    account and region against it before returning.

    Raises:
        UnverifiedTargetError: A remote build was asked for without ``outputs``.
    """
    if endpoint_url is None:
        if outputs is None:
            raise UnverifiedTargetError(
                "no --outputs document was given, so the account and region this would WRITE to "
                "cannot be checked against the deployment. Pass --outputs (e.g. "
                "results/tf-outputs.json), or --endpoint-url for a local engine. Nothing was written."
            )
        from scripts.smoke.harness import assert_account_matches_outputs, smoke_context

        assert_account_matches_outputs(
            smoke_context(profile=profile, region=region, outputs=outputs)
        )

    import boto3

    from crm.config import resolve_aws_config

    resolved = resolve_aws_config(profile=profile, region=region, validate=False)
    session = boto3.session.Session(profile_name=resolved.profile, region_name=resolved.region)
    client = session.client("dynamodb", region_name=resolved.region, endpoint_url=endpoint_url)
    return Store(table_prefix=table_prefix, client=client)


def _seed_once(store: Store, seeder: DemoSeeder | None) -> object:
    """Run one seeding pass through the sanctioned door, or through an injected seeder in tests."""
    return seed_demo_tenant_via_sanctioned_door(store, seeder=seeder)


def seed_and_verify(store: Store, *, seeder: DemoSeeder | None = None) -> SeedReport:
    """Seed ``demo`` twice, compare what the store holds after each pass, and report.

    A census is taken before anything is written; an unreachable store there is a skip. Every later
    failure is a ``fail`` or an ``unverified``, and its reason says how many passes landed.

    Args:
        store: The store to seed into and read back through.
        seeder: Override the demo-seed entry point, forwarded to
            :func:`eval.tenancy.seed_demo_tenant`.
    """
    try:
        census_tenant(store, tenant=TARGET_TENANT)
    except CensusUnreadableError as exc:
        if exc.unreachable:
            return SeedReport(
                status="skip",
                reason=f"{exc}. The tenant was not written.",
            )
        return SeedReport(
            status="fail",
            reason=(
                f"{exc}. The store answered rather than being unreachable, so this is a failure and "
                "not a skip — a refused or malformed read means a missing permission, a wrong index, "
                f"or a throttle. The tenant was not written. Cause: {type(exc.cause).__name__}."
            ),
        )

    try:
        first_summary = _seed_once(store, seeder)
    except _STORE_FAILURES as exc:
        return SeedReport(
            status="fail",
            reason=(
                f"the first seeding pass failed against a readable store "
                f"({type(exc).__name__}: {exc}); tenant {TARGET_TENANT!r} may hold a partial corpus."
            ),
        )

    try:
        first_census = census_tenant(store, tenant=TARGET_TENANT)
    except CensusUnreadableError as exc:
        return SeedReport(
            status="unverified",
            passes_completed=1,
            reason=(
                f"tenant {TARGET_TENANT!r} was seeded once, but idempotency could not be verified: "
                f"{exc}. The corpus is written; the property is unproven."
            ),
        )

    try:
        second_summary = _seed_once(store, seeder)
    except _STORE_FAILURES as exc:
        return SeedReport(
            status="fail",
            passes_completed=1,
            census_after_first=first_census,
            reason=(
                f"the second seeding pass failed ({type(exc).__name__}: {exc}); tenant "
                f"{TARGET_TENANT!r} holds the corpus from one pass and idempotency is unproven."
            ),
        )

    try:
        second_census = census_tenant(store, tenant=TARGET_TENANT)
    except CensusUnreadableError as exc:
        return SeedReport(
            status="unverified",
            passes_completed=2,
            census_after_first=first_census,
            reason=(
                f"tenant {TARGET_TENANT!r} was seeded twice, but the second census failed: {exc}. "
                "The corpus is written; idempotency is unproven."
            ),
        )

    if sum(second_census.values()) == 0:
        return SeedReport(
            status="fail",
            passes_completed=2,
            census_after_first=first_census,
            census_after_second=second_census,
            reason=(
                f"both seeding passes reported success but tenant {TARGET_TENANT!r} reads back empty. "
                "Every other check here is satisfied vacuously by an empty tenant — two empty "
                "censuses are equal and an untouched bystander says nothing about demo — so this is "
                "the lower bound that stops a no-op seeder, or a corpus written without the "
                "'gsi_tenant' sort key and therefore absent from every tenant scan, from reading as "
                "a pass."
            ),
        )

    delta = describe_census_delta(first_census, second_census)
    if delta:
        return SeedReport(
            status="fail",
            passes_completed=2,
            census_after_first=first_census,
            census_after_second=second_census,
            reason=(
                f"seeding tenant {TARGET_TENANT!r} is not idempotent: re-seeding changed the row "
                f"census ({delta}). Every write is meant to land at a deterministic key, so a "
                "growing census means the corpus duplicates rather than overwrites."
            ),
        )

    if first_summary != second_summary:
        return SeedReport(
            status="fail",
            passes_completed=2,
            census_after_first=first_census,
            census_after_second=second_census,
            reason=(
                f"seeding tenant {TARGET_TENANT!r} is not deterministic: the row census was "
                "unchanged but the two passes reported different records, so the same number of "
                "rows was written under different identities."
            ),
        )

    try:
        assert_canary_empty(store, tenant=TARGET_TENANT)
    except CanaryTenantNotEmptyError as exc:
        return SeedReport(
            status="fail",
            passes_completed=2,
            census_after_first=first_census,
            census_after_second=second_census,
            reason=(
                f"tenant {TARGET_TENANT!r} was seeded, but the bystander is no longer empty: {exc}"
            ),
        )
    except _STORE_FAILURES as exc:
        return SeedReport(
            status="unverified",
            passes_completed=2,
            census_after_first=first_census,
            census_after_second=second_census,
            reason=(
                f"tenant {TARGET_TENANT!r} was seeded idempotently, but the bystander check could "
                f"not be completed ({type(exc).__name__}: {exc}). An unreadable bystander is not an "
                "empty one."
            ),
        )

    return SeedReport(
        status="pass",
        passes_completed=2,
        census_after_first=first_census,
        census_after_second=second_census,
    )


def run(
    *,
    table_prefix: str,
    profile: str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
    store: Store | None = None,
    outputs: str | None = None,
    seeder: DemoSeeder | None = None,
) -> SeedReport:
    """Build a store unless one is injected, then seed and verify. Never raises.

    A store construction failure is a skip when it is a missing ``--outputs`` or one of
    :data:`_UNREACHABLE_FAILURES`, and a failure otherwise.
    """
    if store is None:
        try:
            store = build_store(
                outputs=outputs,
                table_prefix=table_prefix,
                profile=profile,
                region=region,
                endpoint_url=endpoint_url,
            )
        except UnverifiedTargetError as exc:
            return SeedReport(status="skip", reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - classified below; an unbuildable store wrote nothing
            if is_unreachable_failure(exc):
                return SeedReport(
                    status="skip",
                    reason=(
                        f"could not construct a DynamoDB store ({type(exc).__name__}: {exc}); no "
                        "credential, no configured profile, or no reachable endpoint."
                    ),
                )
            return SeedReport(
                status="fail",
                reason=(
                    f"could not construct a DynamoDB store ({type(exc).__name__}: {exc}). This is "
                    "not an absent credential or an unreachable endpoint, so it is reported as a "
                    "failure rather than a skip; nothing was written."
                ),
            )
    return seed_and_verify(store, seeder=seeder)


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser.

    Exposes no ``--tenant`` option. Every input is a flag or an environment variable.
    """
    parser = argparse.ArgumentParser(
        description="Seed the demo tenant non-interactively and verify the seeding was idempotent."
    )
    parser.add_argument(
        "--table-prefix",
        default=None,
        help=f"DynamoDB table prefix (default: ${TABLE_PREFIX_ENV_VAR}, else empty).",
    )
    parser.add_argument(
        "--outputs",
        default=None,
        help=(
            "Terraform outputs snapshot describing the deployment to seed. REQUIRED for a remote run: "
            "the resolved account and region are checked against it before anything is written."
        ),
    )
    parser.add_argument("--profile", default=None, help="AWS profile (default: resolved order).")
    parser.add_argument("--region", default=None, help="AWS region (default: resolved order).")
    parser.add_argument(
        "--endpoint-url",
        default=None,
        help="DynamoDB endpoint override, for a local engine.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable report.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, seed and verify, emit the report, and return its exit status."""
    args = build_arg_parser().parse_args(argv)

    report = run(
        table_prefix=resolve_table_prefix(args.table_prefix),
        profile=args.profile,
        region=args.region,
        endpoint_url=args.endpoint_url,
        outputs=args.outputs,
    )

    if args.json:
        print(json.dumps(report.to_json_dict(), indent=2, sort_keys=True))
    else:
        print(report.render_text())
    return report.exit_code()


__all__ = [
    "SKIP_EXIT_STATUS",
    "SeedStatus",
    "SCRIPT_NAME",
    "TABLE_PREFIX_ENV_VAR",
    "TARGET_TENANT",
    "CensusUnreadableError",
    "SeedReport",
    "is_unreachable_failure",
    "build_arg_parser",
    "build_store",
    "census_tenant",
    "describe_census_delta",
    "main",
    "resolve_table_prefix",
    "run",
    "seed_and_verify",
]


if __name__ == "__main__":
    sys.exit(main())
