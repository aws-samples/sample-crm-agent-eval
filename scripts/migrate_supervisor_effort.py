#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Move every saved per-user supervisor effort of ``low`` to ``medium``.

Saved settings are ``reps`` items keyed ``usersettings#<tenant>#<rep_id>`` (:mod:`crm.user_settings`).
Each one whose ``tuning.tiers.supervisor.effort`` is ``low`` gets that one value set to ``medium``;
every other tier, value and attribute, ``updated_at`` included, is left as it is. The write is
conditional on the stored ``tuning`` still equalling the block that was read, so a save made in
between is kept and counted as a ``conflict``, and a re-run migrates nothing. An item whose migrated
block would not resolve against the capability matrix is skipped and counted.

Usage:
    terraform -chdir=infra output -json | scripts/migrate_supervisor_effort.py \\
        --outputs-json - --profile NAME --region <region> [--table-prefix PREFIX] [--apply]

Dry run by default; ``--apply`` writes. The resolved account and region are checked against the
outputs document before anything is read. Prints one JSON object of counts to stdout.

Exit status: ``0`` done; ``1`` refused or failed; ``2`` usage error.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "src"):  # pragma: no cover - import bootstrap
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402
from scripts.provision_demo_user import (  # noqa: E402
    TargetMismatchError,
    assert_target_matches_outputs,
)
from scripts.provision_gateway_principal import (  # noqa: E402
    assert_outputs_name_target,
    resolve_table_prefix,
)

from crm.errors import Conflict, CrmError, ValidationFailed  # noqa: E402
from crm.store.store import Store  # noqa: E402
from crm.user_settings import SETTINGS_KIND, SETTINGS_SOURCE, SETTINGS_TABLE  # noqa: E402
from providers.capabilities import CapabilityMatrix, load_capability_matrix  # noqa: E402
from providers.tier_tuning import (  # noqa: E402
    TierRequest,
    TurnTuning,
    parse_stored_tuning,
    resolve_turn,
)

#: Key prefix shared by every saved-settings item (:func:`crm.store.keys.user_settings_key`).
SETTINGS_KEY_PREFIX: Final[str] = "usersettings#"

MIGRATED_TIER: Final[str] = "supervisor"
OLD_EFFORT: Final[str] = "low"
NEW_EFFORT: Final[str] = "medium"


@dataclass(slots=True)
class MigrationSummary:
    """Counts of one migration run.

    ``scanned == eligible + already_medium + unchanged + skipped_unresolvable +
    skipped_not_settings``; with ``--apply``, ``eligible == migrated + conflict``.

    Attributes:
        dry_run: Whether the run wrote nothing.
        partial: Whether the run stopped on an error before every item was read.
        scanned: Items under :data:`SETTINGS_KEY_PREFIX`.
        eligible: Settings items at supervisor ``low`` whose migrated block resolves.
        migrated: Eligible items written (0 in a dry run).
        conflict: Eligible items whose ``tuning`` changed before the write; left as saved.
        already_medium: Settings items whose supervisor effort is already ``medium``.
        unchanged: Settings items with no supervisor effort or another value.
        skipped_unresolvable: Supervisor-``low`` items whose migrated block would not resolve.
        skipped_not_settings: Items under the prefix that are not settings items, including any
            holding a value the settings writer never stores (a set, binary or float).
    """

    dry_run: bool
    partial: bool = False
    scanned: int = 0
    eligible: int = 0
    migrated: int = 0
    conflict: int = 0
    already_medium: int = 0
    unchanged: int = 0
    skipped_unresolvable: int = 0
    skipped_not_settings: int = 0

    def to_report_fields(self) -> dict[str, object]:
        """Render as a flat JSON-safe mapping in field order."""
        return asdict(self)


class MigrationInterruptedError(Exception):
    """A run stopped part-way; :attr:`summary` holds the counts so far, with ``partial`` set."""

    def __init__(self, summary: MigrationSummary) -> None:
        super().__init__("the migration stopped before every item was read")
        self.summary = summary


def _is_plain(value: object) -> bool:
    if value is None or isinstance(value, str | bool | int | Decimal):
        return True
    if isinstance(value, Mapping):
        return all(isinstance(k, str) and _is_plain(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_is_plain(v) for v in value)
    return False


def assert_new_effort_resolves(matrix: CapabilityMatrix) -> None:
    """Raise ``ValueError`` unless every family's configured supervisor accepts :data:`NEW_EFFORT`."""
    for family in matrix.tiers:
        tuning = TurnTuning(
            source=SETTINGS_SOURCE,
            tiers={"supervisor": TierRequest(family=family, effort=NEW_EFFORT)},
        )
        try:
            resolve_turn(matrix, tuning)
        except ValidationFailed as exc:
            raise ValueError(
                f"the configured {family} supervisor does not accept effort {NEW_EFFORT!r}, so "
                "no item could migrate. Nothing has been read."
            ) from exc


def _supervisor_effort(tuning: object) -> object:
    if not isinstance(tuning, Mapping):
        return None
    tiers = tuning.get("tiers")
    request = tiers.get(MIGRATED_TIER) if isinstance(tiers, Mapping) else None
    return request.get("effort") if isinstance(request, Mapping) else None


def migrated_tuning(tuning: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``tuning`` with only the supervisor effort set to :data:`NEW_EFFORT`."""
    updated = copy.deepcopy(dict(tuning))
    updated["tiers"][MIGRATED_TIER]["effort"] = NEW_EFFORT
    return updated


def _resolves(matrix: CapabilityMatrix, tuning: Mapping[str, Any]) -> bool:
    try:
        resolve_turn(matrix, parse_stored_tuning(dict(tuning), default_source=SETTINGS_SOURCE))
    except ValidationFailed:
        return False
    return True


def _is_condition_failure(exc: Conflict) -> bool:
    """Whether ``exc`` is a failed write condition, not another error the taxonomy calls Conflict."""
    cause = exc.__cause__
    return (
        isinstance(cause, ClientError)
        and cause.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    )


def migrate(store: Store, *, matrix: CapabilityMatrix, apply: bool) -> MigrationSummary:
    """Migrate every saved supervisor effort of :data:`OLD_EFFORT` in ``store``.

    Args:
        store: The persistence layer, prefixed for the target deployment.
        matrix: The capability matrix the migrated blocks must resolve against.
        apply: Write the migrated blocks; when ``False`` nothing is written.

    Returns:
        The run's counts.

    Raises:
        MigrationInterruptedError: A store read or write failed other than by a changed ``tuning`` (a
            throttled write is not counted as a conflict); the original error is its cause.
    """
    summary = MigrationSummary(dry_run=not apply)
    try:
        _migrate_items(store, matrix=matrix, apply=apply, summary=summary)
    except (CrmError, BotoCoreError, ClientError, TypeError) as exc:
        summary.partial = True
        raise MigrationInterruptedError(summary) from exc
    return summary


def _migrate_items(
    store: Store, *, matrix: CapabilityMatrix, apply: bool, summary: MigrationSummary
) -> None:
    for item in store.scan_key_prefix(SETTINGS_TABLE, "rep_id", SETTINGS_KEY_PREFIX):
        summary.scanned += 1
        tuning = item.get("tuning")
        if (
            item.get("kind") != SETTINGS_KIND
            or not isinstance(tuning, Mapping)
            or not _is_plain(item)
        ):
            summary.skipped_not_settings += 1
            continue
        effort = _supervisor_effort(tuning)
        if effort == NEW_EFFORT:
            summary.already_medium += 1
            continue
        if effort != OLD_EFFORT:
            summary.unchanged += 1
            continue
        updated = migrated_tuning(tuning)
        if not _resolves(matrix, updated):
            summary.skipped_unresolvable += 1
            continue
        summary.eligible += 1
        if not apply:
            continue
        try:
            store.update_item(
                SETTINGS_TABLE,
                {"rep_id": str(item["rep_id"])},
                set_values={"tuning": updated},
                condition="#t = :read",
                names={"#t": "tuning"},
                values={":read": tuning},
            )
        except Conflict as exc:
            if not _is_condition_failure(exc):
                raise
            summary.conflict += 1
            continue
        summary.migrated += 1


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outputs-json",
        required=True,
        help="File holding `terraform output -json`, or '-' for stdin",
    )
    parser.add_argument("--profile", required=True, help="AWS profile for the DynamoDB calls")
    parser.add_argument("--region", required=True, help="AWS region for the DynamoDB calls")
    parser.add_argument(
        "--table-prefix",
        help="DynamoDB table prefix (default: derived from the outputs; must agree with them)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write the migration (default: dry run)"
    )
    return parser


def _read_outputs(source: str) -> dict[str, Any]:
    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    outputs = json.loads(raw)
    if not isinstance(outputs, dict):
        raise ValueError(
            f"expected a JSON object from `terraform output -json`, got {type(outputs).__name__}"
        )
    return outputs


#: Appended to a mid-run failure message.
_PARTIAL_RUN_NOTE: Final[str] = (
    "The counts on stdout cover the items read before the failure; items written stay migrated "
    "and re-running is safe."
)


def _default_session(profile: str, region: str) -> Any:
    import boto3

    return boto3.session.Session(profile_name=profile, region_name=region)


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[str, str], Any] = _default_session,
    matrix: CapabilityMatrix | None = None,
) -> int:
    """Run the CLI; ``session_factory`` and ``matrix`` are injectable for tests."""
    args = build_arg_parser().parse_args(argv)

    try:
        outputs = _read_outputs(args.outputs_json)
        prefix = resolve_table_prefix(outputs, args.table_prefix)
        assert_outputs_name_target(outputs)
        resolved_matrix = matrix if matrix is not None else load_capability_matrix()
        assert_new_effort_resolves(resolved_matrix)
    except (OSError, ValueError, TargetMismatchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        session = session_factory(args.profile, args.region)
        assert_target_matches_outputs(
            session, outputs=outputs, region=args.region, profile=args.profile
        )
    except (BotoCoreError, ClientError, TargetMismatchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        summary = migrate(
            Store(client=session.client("dynamodb"), table_prefix=prefix),
            matrix=resolved_matrix,
            apply=args.apply,
        )
    except MigrationInterruptedError as interrupted:
        print(json.dumps(interrupted.summary.to_report_fields(), sort_keys=False))
        cause = interrupted.__cause__
        reason = cause.message if isinstance(cause, CrmError) else "see the error type"
        print(
            f"error: the migration failed ({type(cause).__name__}): {reason} {_PARTIAL_RUN_NOTE}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summary.to_report_fields(), sort_keys=False))
    if summary.skipped_unresolvable:
        print(
            f"warning: {summary.skipped_unresolvable} item(s) at supervisor effort {OLD_EFFORT!r} "
            "were skipped because the migrated settings would not resolve against the capability "
            "matrix; they are left as saved.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
