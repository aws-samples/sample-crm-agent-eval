#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assert the ``canary`` tenant is empty, independently of the eval harness.

Usage:
    python3 -m scripts.check_canary_tenant_empty [--table-prefix PREFIX] [--profile P] [--region R]
                                                 [--outputs PATH] [--endpoint-url URL]
                                                 [--require-store] [--json]

A remote run against a deployment::

    .venv/bin/python3 scripts/check_canary_tenant_empty.py \
      --outputs results/tf-outputs.json --table-prefix crm-agent-eval --region us-east-1

``--outputs`` is required for a remote run: without it the gate exits ``3`` having read nothing.
``--endpoint-url`` targets a local engine instead and needs no outputs document. The table prefix
is ``--table-prefix``, else ``$CRM_TABLE_PREFIX``, else empty. A resolved account or region that
disagrees with the outputs document is refused.

This is the second of two independent ``canary`` checks; the first is inside
:func:`eval.tenancy.teardown_tenant`, which the harness calls from its ``finally``. The two share no
enumeration code. Both go through :class:`crm.store.store.Store`, and this module's
:data:`CANARY_TENANT` is asserted equal to :data:`eval.tenancy.CANARY_TENANT` by a unit test.

Every offending row is reported as ``table:key=value``.

Exit status:

* ``0`` — the ``canary`` tenant was read and holds no row.
* ``1`` — the ``canary`` tenant holds at least one row, each named in the output, or the store
  answered with a refusal (access denied, validation error, throttle), or the resolved target does
  not match the outputs document.
* ``2`` — usage error (argparse).
* ``3`` — the tenant could not be reached (:func:`is_unreachable_failure`): no credential, no
  configured profile, no reachable endpoint, or the tables do not exist. Nothing was verified;
  ``scripts/run_gates.py`` renders it ``[SKIP]``.

``--require-store`` turns exit ``3`` into ``1``, for environments where a store exists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
# `src` for `crm.*`; the repository root for the deferred `scripts.smoke.harness` import.
for _root in (REPO_ROOT / "src", REPO_ROOT):  # pragma: no cover - import bootstrap
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from botocore.exceptions import (  # noqa: E402
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    NoCredentialsError,
    ProfileNotFound,
)

from crm.errors import CrmError, NotFound, ServiceBusy, TableNotFound  # noqa: E402
from crm.store.schema import TENANT_SCOPED_TABLES, table_schema  # noqa: E402
from crm.store.store import Store  # noqa: E402

#: This gate's machine-readable name, carried in the ``--json`` report and printed in every line.
GATE_NAME: Final[str] = "canary_tenant_empty"

#: The tenant this gate asserts empty. A unit test asserts it equals ``eval.tenancy.CANARY_TENANT``.
CANARY_TENANT: Final[str] = "canary"

#: Exit status meaning "ran, verified nothing". Must equal ``scripts/run_gates.py``'s
#: ``GATE_SKIP_EXIT_STATUS``; a unit test pins the two together.
SKIP_EXIT_STATUS: Final[int] = 3

#: Environment variable naming the DynamoDB table prefix.
TABLE_PREFIX_ENV_VAR: Final[str] = "CRM_TABLE_PREFIX"

#: How many offending records the human-readable output names inline. The JSON report carries every
#: one, and both forms state the total.
_RECORDS_NAMED_IN_TEXT: Final[int] = 20

#: Every store-layer failure this gate recognises. The disposition is decided by
#: :func:`is_unreachable_failure`.
_STORE_FAILURES: Final[tuple[type[Exception], ...]] = (BotoCoreError, ClientError, CrmError)

#: The only failures that prove the tenant was never reached, or was never provisioned. Exit
#: :data:`SKIP_EXIT_STATUS` turns on exactly this set. ``CrmError`` subclasses other than
#: ``NotFound`` and ``TableNotFound`` (access denied, validation, throttling) are store answers and
#: are failures. A unit test asserts this classifier agrees with ``scripts/seed_demo_tenant.py``'s.
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
    current: BaseException | None = exc.__cause__
    while current is not None and id(current) not in seen:
        if isinstance(current, _CONNECTION_CAUSES):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def is_unreachable_failure(exc: BaseException) -> bool:
    """Whether ``exc`` proves the tenant was never read, as opposed to read and refused.

    ``True`` earns the exit-3 "verified nothing" disposition; anything else is a store that answered,
    which is a **failure**. See :data:`_UNREACHABLE_FAILURES`.

    A :class:`crm.errors.ServiceBusy` that :class:`crm.store.store.Store` raised from a connection
    failure (:data:`_CONNECTION_CAUSES` anywhere in its cause chain) counts as unreachable too; one
    with any other cause, or none, is an answer.
    """
    if isinstance(exc, _UNREACHABLE_FAILURES):
        return True
    return isinstance(exc, ServiceBusy) and _raised_from_connection_failure(exc)


@dataclass(frozen=True, slots=True)
class OffendingRecord:
    """One row found beneath the ``canary`` tenant.

    Attributes:
        table: Logical table the row was found in.
        key_attribute: The table's primary partition-key attribute name.
        key_value: That attribute's value, or ``"<unknown>"`` when the row carries no readable key.
    """

    table: str
    key_attribute: str
    key_value: str

    def describe(self) -> str:
        """Render as ``table:key_attribute=key_value`` for output."""
        return f"{self.table}:{self.key_attribute}={self.key_value}"

    def to_json_dict(self) -> dict[str, str]:
        """The pinned JSON shape for one offending record."""
        return {"table": self.table, "key": self.key_attribute, "value": self.key_value}


@dataclass(frozen=True, slots=True)
class CanaryGuardReport:
    """The outcome of one gate run.

    Attributes:
        status: ``"pass"``, ``"fail"``, or ``"skip"``. ``"skip"`` means the tenant was never read.
        tables_read: Every table successfully enumerated, in order. Empty for a skip.
        records: Every offending row found. Empty for a pass.
        reason: Why a skip happened, or what a fail found. Empty for a pass.
    """

    status: str
    tables_read: tuple[str, ...]
    records: tuple[OffendingRecord, ...]
    reason: str = ""

    def exit_code(self) -> int:
        """Return ``0`` for a pass, ``1`` for a fail, :data:`SKIP_EXIT_STATUS` for a skip."""
        if self.status == "pass":
            return 0
        if self.status == "fail":
            return 1
        return SKIP_EXIT_STATUS

    def to_json_dict(self) -> dict[str, Any]:
        """The pinned ``--json`` shape. Always carries every record, never a truncated sample."""
        return {
            "gate": GATE_NAME,
            "tenant": CANARY_TENANT,
            "status": self.status,
            "tables_read": list(self.tables_read),
            "record_count": len(self.records),
            "records": [record.to_json_dict() for record in self.records],
            "reason": self.reason,
        }

    def render_text(self) -> str:
        """One human-readable line per outcome, naming the offending records on a failure."""
        if self.status == "pass":
            return (
                f"[ pass ] {GATE_NAME}: tenant {CANARY_TENANT!r} holds no row across "
                f"{len(self.tables_read)} table(s): {list(self.tables_read)}"
            )
        if self.status == "skip":
            return f"[SKIP] {GATE_NAME}: {self.reason} Nothing was verified."
        if not self.records:
            # A fail with no records is a failed read; its cause is in `reason`.
            return (
                f"[FAIL] {GATE_NAME}: tenant {CANARY_TENANT!r} could not be checked, and the cause is "
                f"not an absent credential. {self.reason} No row was found beneath "
                f"{CANARY_TENANT!r} -- this failure is about the read, not about a leaked tenant."
            )
        named = [record.describe() for record in self.records[:_RECORDS_NAMED_IN_TEXT]]
        remainder = len(self.records) - len(named)
        return (
            f"[FAIL] {GATE_NAME}: tenant {CANARY_TENANT!r} must be empty but holds "
            f"{len(self.records)} record(s): {named}"
            + (f" (+{remainder} more)" if remainder > 0 else "")
            + f". {CANARY_TENANT!r} is seeded by nothing, so a row beneath it means a run wrote "
            "outside its own tenant."
        )


def resolve_table_prefix(flag: str | None = None, env: dict[str, str] | None = None) -> str:
    """Resolve the DynamoDB table prefix: flag, then :data:`TABLE_PREFIX_ENV_VAR`, then empty.

    An empty prefix is not an error; whether the resulting tables exist is answered by reading them.
    """
    if flag is not None:
        return flag
    environment = env if env is not None else dict(os.environ)
    return environment.get(TABLE_PREFIX_ENV_VAR, "")


def _assert_target_matches_outputs(
    *, profile: str | None, region: str | None, outputs: str
) -> None:
    """Raise unless the resolved account and region are the ones `outputs` describes.

    Delegates to `scripts.smoke.harness`, imported lazily so this module stays importable without
    `boto3`.
    """
    from scripts.smoke.harness import assert_account_matches_outputs, smoke_context

    assert_account_matches_outputs(smoke_context(profile=profile, region=region, outputs=outputs))


class UnverifiedTargetError(RuntimeError):
    """A remote run was asked for with no outputs document, so its target cannot be established."""


def _is_wrong_target(exc: Exception) -> bool:
    """Whether `exc` is the harness's named resolved-vs-expected mismatch.

    Matched on the class name so `scripts.smoke.harness`, which imports `boto3`, is not imported here.
    """
    return type(exc).__name__ == "WrongTargetError"


def build_store(
    *,
    table_prefix: str,
    profile: str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
    outputs: str | None = None,
) -> Store:
    """Build a :class:`Store` for this gate, resolving profile and region the repository's way.

    Constructs only a session and a DynamoDB client, handed to ``Store``.

    A remote build (no ``endpoint_url``) raises :class:`UnverifiedTargetError` without an ``outputs``
    document, and otherwise asserts the resolved account and region match it.
    """
    if endpoint_url is None:
        if outputs is None:
            raise UnverifiedTargetError(
                "no --outputs document was given, so the account and region this gate would read "
                "cannot be checked against the deployment under test. This gate PASSES when it finds "
                "nothing, so an unverified target could report a clean canary about an account it "
                "never contacted. Pass --outputs results/tf-outputs.json, or --endpoint-url for a "
                "local engine. Nothing was read."
            )
        _assert_target_matches_outputs(profile=profile, region=region, outputs=outputs)

    import boto3

    from crm.config import resolve_aws_config

    resolved = resolve_aws_config(profile=profile, region=region, validate=False)
    session = boto3.session.Session(profile_name=resolved.profile, region_name=resolved.region)
    client = session.client("dynamodb", region_name=resolved.region, endpoint_url=endpoint_url)
    return Store(table_prefix=table_prefix, client=client)


def check_canary_tenant(store: Store) -> CanaryGuardReport:
    """Enumerate the ``canary`` tenant and report what is there.

    Walks every table in :data:`crm.store.schema.TENANT_SCOPED_TABLES` — the same set
    :meth:`crm.store.store.Store.delete_tenant` walks — through the tenant-wide index, paginating fully.

    The first table that cannot be read ends the run:

    * Records already found — **fail**, carrying those records; the reason names the read failure.
    * Nothing found, and the failure proves unreachability (:func:`is_unreachable_failure`) —
      **skip**.
    * Nothing found, and the store answered (access denied, validation error, throttle) — **fail**.
    """
    records: list[OffendingRecord] = []
    tables_read: list[str] = []
    for table in sorted(TENANT_SCOPED_TABLES):
        key_attribute = table_schema(table).partition_key
        cursor: dict[str, object] | None = None
        while True:
            try:
                items, cursor = store.query_tenant_scan(
                    table, tenant=CANARY_TENANT, exclusive_start_key=cursor
                )
            except _STORE_FAILURES as exc:
                unreachable = is_unreachable_failure(exc)
                unreadable = (
                    f"could not read tenant {CANARY_TENANT!r} in table {table!r} "
                    f"({type(exc).__name__}: {exc}); "
                    + (
                        "no credential, no configured profile, no reachable endpoint, or the "
                        "tables do not exist."
                        if unreachable
                        else "the store answered rather than being unreachable, so this is a "
                        "failure and not a skip — a refused or malformed read means a missing "
                        "permission, a wrong index, or a throttle."
                    )
                )
                if records:
                    return CanaryGuardReport(
                        status="fail",
                        tables_read=tuple(tables_read),
                        records=tuple(records),
                        reason=(
                            f"{len(records)} record(s) found beneath tenant {CANARY_TENANT!r} "
                            f"before enumeration was cut short: {unreadable} The violation stands; "
                            "the record list may be incomplete."
                        ),
                    )
                return CanaryGuardReport(
                    status="skip" if unreachable else "fail",
                    tables_read=tuple(tables_read),
                    records=(),
                    reason=unreadable,
                )
            for item in items:
                raw = item.get(key_attribute)
                records.append(
                    OffendingRecord(
                        table=table,
                        key_attribute=key_attribute,
                        key_value=str(raw) if raw is not None else "<unknown>",
                    )
                )
            if cursor is None:
                break
        tables_read.append(table)

    if records:
        return CanaryGuardReport(
            status="fail",
            tables_read=tuple(tables_read),
            records=tuple(records),
            reason=f"{len(records)} record(s) found beneath tenant {CANARY_TENANT!r}",
        )
    return CanaryGuardReport(status="pass", tables_read=tuple(tables_read), records=())


def run(
    *,
    table_prefix: str,
    profile: str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
    outputs: str | None = None,
    store: Store | None = None,
) -> CanaryGuardReport:
    """Build a store (unless one is injected) and check the tenant, never raising.

    ``store`` is injectable for offline tests. A remote run with no ``outputs`` document is reported
    ``skip``; a target mismatching the outputs is ``fail``; ``endpoint_url`` and an injected ``store``
    need no outputs document.
    """
    if store is None:
        try:
            store = build_store(
                table_prefix=table_prefix,
                profile=profile,
                region=region,
                endpoint_url=endpoint_url,
                outputs=outputs,
            )
        except UnverifiedTargetError as exc:
            return CanaryGuardReport(status="skip", tables_read=(), records=(), reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - classified below; nothing was read either way
            if _is_wrong_target(exc):
                return CanaryGuardReport(
                    status="fail",
                    tables_read=(),
                    records=(),
                    reason=f"refusing to read the wrong target: {exc}",
                )
            if is_unreachable_failure(exc):
                return CanaryGuardReport(
                    status="skip",
                    tables_read=(),
                    records=(),
                    reason=(
                        f"could not construct a DynamoDB store ({type(exc).__name__}: {exc}); no "
                        "credential, no configured profile, or no reachable endpoint."
                    ),
                )
            return CanaryGuardReport(
                status="fail",
                tables_read=(),
                records=(),
                reason=(
                    f"could not construct a DynamoDB store ({type(exc).__name__}: {exc}). That is "
                    "not an absent credential or an unreachable endpoint, so it is a failure rather "
                    "than a skip: a defect in this gate would otherwise report [SKIP] forever."
                ),
            )
    return check_canary_tenant(store)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the gate, emit the report, and return its exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table-prefix",
        default=None,
        help=f"DynamoDB table prefix (default: ${TABLE_PREFIX_ENV_VAR}, else empty).",
    )
    parser.add_argument("--profile", default=None, help="AWS profile (default: resolved order).")
    parser.add_argument("--region", default=None, help="AWS region (default: resolved order).")
    parser.add_argument(
        "--endpoint-url",
        default=None,
        help="DynamoDB endpoint override, for a local engine.",
    )
    parser.add_argument(
        "--outputs",
        default=None,
        help=(
            "Terraform outputs snapshot describing the deployment to read. REQUIRED for a remote run: "
            "this gate passes when it finds nothing, so without it the resolved account and region "
            "cannot be checked and the run is reported as a skip rather than a pass."
        ),
    )
    parser.add_argument(
        "--require-store",
        action="store_true",
        help=(
            f"Fail (exit 1) instead of skipping (exit {SKIP_EXIT_STATUS}) when the tenant cannot be "
            "read. Pass this wherever a store genuinely exists, notably deploy verification, so the "
            "gate cannot be silently unverified in the one environment that can verify it."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable report.")
    args = parser.parse_args(argv)

    report = run(
        table_prefix=resolve_table_prefix(args.table_prefix),
        outputs=args.outputs,
        profile=args.profile,
        region=args.region,
        endpoint_url=args.endpoint_url,
    )

    if args.json:
        print(json.dumps(report.to_json_dict(), indent=2, sort_keys=True))
    else:
        print(report.render_text())

    if report.status == "skip" and args.require_store:
        return 1
    return report.exit_code()


__all__ = [
    "CANARY_TENANT",
    "GATE_NAME",
    "SKIP_EXIT_STATUS",
    "TABLE_PREFIX_ENV_VAR",
    "CanaryGuardReport",
    "OffendingRecord",
    "build_store",
    "is_unreachable_failure",
    "check_canary_tenant",
    "main",
    "resolve_table_prefix",
    "run",
]


if __name__ == "__main__":
    sys.exit(main())
