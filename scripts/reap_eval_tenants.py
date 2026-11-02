#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reap orphaned eval tenants older than a threshold, non-interactively.

An eval run that crashes mid-execution leaves its ``eval_runs`` row recorded as ``running`` with
no teardown. This script finds every such run older than ``--older-than`` and tears its tenant down,
with any unit tenants recorded on its row, via :func:`src.eval.tenancy.reap_tenant`.

``--dry-run`` (the default) only reports what would be reaped; ``--yes`` performs the deletions and
requires ``--outputs``, against which the resolved account and region are checked first. The
``demo`` tenant is refused by :mod:`src.eval.tenancy`. Against a real AWS account this is billable.

Usage:
    python3 scripts/reap_eval_tenants.py --older-than 24h [--dry-run | --yes]
        [--profile NAME] [--region REGION] [--table-prefix PREFIX] [--outputs PATH]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _import_root in (REPO_ROOT / "src", REPO_ROOT):  # pragma: no cover - import bootstrap
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

import boto3  # noqa: E402

from crm.config import DEFAULT_REGION  # noqa: E402
from crm.errors import CrmError  # noqa: E402
from crm.store.store import Store  # noqa: E402
from eval.tenancy import (  # noqa: E402
    CanaryTenantNotEmptyError,
    OrphanedRun,
    TenantTeardownError,
    find_orphaned_runs,
    reap_tenant,
)

_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[hdm])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}

#: Environment variable naming the deployment's DynamoDB table prefix, read when ``--table-prefix``
#: is absent.
_TABLE_PREFIX_ENV_VAR = "CRM_TABLE_PREFIX"


def parse_duration(raw: str) -> timedelta:
    """Parse a duration like ``24h``, ``30m``, or ``2d`` into a :class:`datetime.timedelta`.

    Raises:
        ValueError: ``raw`` does not match ``<integer><unit>`` with unit one of ``m``, ``h``, ``d``.
    """
    match = _DURATION_RE.fullmatch(raw.strip())
    if match is None:
        raise ValueError(f"invalid duration {raw!r}; expected e.g. '24h', '30m', or '2d'")
    seconds = int(match.group("value")) * _UNIT_SECONDS[match.group("unit")]
    return timedelta(seconds=seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Reap orphaned eval tenants older than a threshold, non-interactively."
    )
    parser.add_argument(
        "--older-than",
        required=True,
        help="Age threshold, e.g. '24h', '30m', or '2d'. Runs started longer ago are reaped.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report orphaned runs without deleting anything (default).",
    )
    mode.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Actually tear down every orphaned tenant found. Overrides --dry-run.",
    )
    parser.add_argument(
        "--profile", default=None, help="AWS profile name. Defaults to AWS_PROFILE."
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(f"AWS region. Resolved as this flag, then AWS_REGION, then {DEFAULT_REGION!r}."),
    )
    parser.add_argument(
        "--outputs",
        default=None,
        help=(
            "Terraform outputs snapshot describing the deployment to act on. REQUIRED with --yes: the "
            "resolved account and region are checked against it before anything is deleted."
        ),
    )
    parser.add_argument(
        "--table-prefix",
        default="",
        help=(
            "DynamoDB table name prefix, matching the deployment's table_prefix. Resolved as this "
            f"flag, then the {_TABLE_PREFIX_ENV_VAR} environment variable (the same one the "
            "deployed Lambdas and Runtime read), then no prefix — the flag-then-environment-then-"
            "default order every entry point in this project uses."
        ),
    )
    return parser


def resolve_table_prefix(flag: str, env: Mapping[str, str] | None = None) -> str:
    """Resolve the table prefix as flag, then :data:`_TABLE_PREFIX_ENV_VAR`, then empty."""
    if flag:
        return flag
    environment = env if env is not None else os.environ
    return environment.get(_TABLE_PREFIX_ENV_VAR, "").strip()


def resolve_region(flag: str | None, env: Mapping[str, str] | None = None) -> str:
    """Resolve the region as ``flag``, then ``AWS_REGION``, then :data:`crm.config.DEFAULT_REGION`."""
    if flag:
        return flag
    environment = env if env is not None else os.environ
    return environment.get("AWS_REGION", "").strip() or DEFAULT_REGION


class UnverifiedTargetError(RuntimeError):
    """A destructive run was asked for without an outputs document naming its target."""


def _assert_target(
    *, profile: str | None, region: str, outputs: str | None, destructive: bool
) -> None:
    """Refuse a destructive run whose target has not been established.

    Only the destructive path is gated: it requires an outputs document and the resolved account and
    region are checked against it.
    """
    if not destructive:
        return
    if outputs is None:
        raise UnverifiedTargetError(
            f"--yes deletes tenant rows and would act on profile {profile!r} in region {region!r}, "
            "resolved from the flags and the environment. Pass --outputs (e.g. "
            "results/tf-outputs.json) so the account and region can be checked against the "
            "deployment first. Nothing has been deleted."
        )
    from scripts.smoke.harness import assert_account_matches_outputs, smoke_context

    assert_account_matches_outputs(smoke_context(profile=profile, region=region, outputs=outputs))


def _build_store(
    *,
    profile: str | None,
    region: str,
    table_prefix: str,
    outputs: str | None = None,
    destructive: bool = False,
) -> Store:
    """Build the reaper's store, refusing a destructive run whose target is not established."""
    _assert_target(profile=profile, region=region, outputs=outputs, destructive=destructive)
    session = boto3.Session(profile_name=profile, region_name=region)
    client = session.client("dynamodb")
    return Store(table_prefix=table_prefix, client=client)


def _report_found(runs: list[OrphanedRun]) -> None:
    if not runs:
        print("No orphaned eval tenants found.")
        return
    print(f"Found {len(runs)} orphaned eval tenant(s):")
    for run in runs:
        print(
            f"  run_id={run.run_id} tenant={run.tenant} status={run.status} started_at={run.started_at}"
            + (f" unit_tenants={','.join(run.unit_tenants)}" if run.unit_tenants else "")
        )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, find orphaned runs, and reap them unless in dry-run mode.

    Returns:
        ``0`` on success (including a dry run, or no orphans found). ``1`` on a bad duration, an
        unverified target, an unreadable catalog, a malformed ``unit_tenants`` on a catalog row,
        any tenant teardown failure, or a ``canary`` violation.

    Reaping continues past both failure kinds. A teardown failure is reported per tenant. A
    ``canary`` violation (:class:`eval.tenancy.CanaryTenantNotEmptyError`) is reported once; the
    tenant being reaped when it surfaced was still torn down.
    """
    args = build_arg_parser().parse_args(argv)

    try:
        threshold = parse_duration(args.older_than)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    table_prefix = resolve_table_prefix(args.table_prefix)
    try:
        store = _build_store(
            profile=args.profile,
            region=resolve_region(args.region),
            table_prefix=table_prefix,
            outputs=args.outputs,
            destructive=bool(args.yes),
        )
    except UnverifiedTargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        orphans = find_orphaned_runs(store, older_than=threshold)
    except ValueError as exc:
        print(f"error: {exc}. No tenant was reaped.", file=sys.stderr)
        return 1
    except CrmError as exc:
        # A missing catalog table surfaces here as TableNotFound.
        print(
            f"error: could not read the eval_runs catalog under table prefix {table_prefix!r} "
            f"({exc.code}: {exc}). Pass --table-prefix, or set "
            f"{_TABLE_PREFIX_ENV_VAR}, to the deployment's table_prefix. No tenant was reaped.",
            file=sys.stderr,
        )
        return 1
    _report_found(orphans)

    perform_deletion = args.yes
    if not orphans:
        return 0
    if not perform_deletion:
        print("Dry run: no tenant was torn down. Pass --yes to reap the tenants listed above.")
        return 0

    failures: list[str] = []
    canary_violation: CanaryTenantNotEmptyError | None = None
    for run in orphans:
        try:
            deleted = reap_tenant(store, run=run)
        except CanaryTenantNotEmptyError as exc:
            # The tenant was torn down before this propagated; the violation is reported once.
            if canary_violation is None:
                canary_violation = exc
                print(f"FAIL canary tenant not empty: {exc}", file=sys.stderr)
            print(f"Reaped {run.tenant}: {exc.deleted}")
            continue
        except TenantTeardownError as exc:
            failures.append(str(exc))
            print(f"FAIL reaping {run.tenant}: {exc}", file=sys.stderr)
            continue
        print(f"Reaped {run.tenant}: {deleted}")

    if failures:
        print(f"\n{len(failures)} tenant(s) failed to reap; see errors above.", file=sys.stderr)
    if canary_violation is not None:
        print(
            "\nThe canary tenant is not empty, so some run wrote outside its own tenant. Every "
            "orphan above was still reaped; the offending records are named in the error above.",
            file=sys.stderr,
        )
    if failures or canary_violation is not None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
