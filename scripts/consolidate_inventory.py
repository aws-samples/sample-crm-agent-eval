#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Consolidate every per-resource-class verification inventory into one published document.

Reads every ``results/verification-inventory/*.json`` artifact each Layer 4/5/6 smoke script
wrote, plus a live residue query, and renders one document listing every resource verified
across Layers 4-6 with its outcome.

Rendering rules:

* A ``defer`` renders as ``DEFER`` with its ``defer_reason`` verbatim, and a ``skip`` as ``SKIP``;
  the headline states pass/skip/defer/fail counts separately.
  :func:`_assert_no_disposition_laundered` re-checks the rendered document against the parsed
  artifacts and refuses to write on a mismatch.
* Every 12-digit run in a rendered cell is rewritten to a placeholder account id, and a finished
  document containing a currency shape or any other account id is refused.
* Each class's evidence age is compared against a reference instant -- ``--deployment-changed-at``
  when supplied, the newest evidence in the document otherwise -- and the unqualified opening
  claim is emitted only when no class is older than it.
  :func:`_assert_no_unqualified_verification_claim` re-checks the rendered bytes.

The residue query is a live, billable DynamoDB read: it scans every tenant-scoped table for
any surviving ``smoke-``/``eval-`` prefixed tenant row, scans the ``eval_runs`` catalogue for
any non-terminal row, and reports the ``demo`` tenant's row count. It never writes.

Usage:
    python3 scripts/consolidate_inventory.py --profile default --region us-east-1 \\
        --table-prefix crm-agent-eval [--inventory-dir DIR] [--out MD] [--json-out JSON] \\
        [--older-than 0h] [--deployment-changed-at ISO8601] [--outputs JSON] [--no-residue]

Exit code: 0 when no check failed and no residue was found; 1 otherwise. A ``defer`` or a
``skip`` never drives the exit code -- only a ``fail``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(1, str(REPO_ROOT / "src"))

from scripts.smoke.harness import (  # noqa: E402
    DEFER,
    CheckResult,
    Provenance,
    ResourceClassResult,
    Status,
)

from crm.config import DEFAULT_REGION  # noqa: E402

#: The statuses `scripts/smoke/harness.py` does not export as named constants.
PASS: Final[Status] = "pass"  # noqa: S105 - a status name, not a credential
FAIL: Final[Status] = "fail"
SKIP: Final[Status] = "skip"

#: The closed status vocabulary an artifact may record; anything else is an error.
_VALID_STATUSES: Final[frozenset[str]] = frozenset({PASS, FAIL, SKIP, DEFER})

#: The logical name of the eval-run catalogue table.
_CATALOG_TABLE = "eval_runs"

#: Tenant-name prefixes that are eval/smoke residue. The ``demo`` tenant is not residue.
_RESIDUE_PREFIXES = ("smoke-", "eval-")

#: A 12-digit run of digits, the shape of an AWS account id, not part of a longer digit run.
_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")

#: The documentation placeholder every account id in a detail string is rewritten to.
_PLACEHOLDER_ACCOUNT_ID = "123456789012"

#: Currency shapes forbidden in a published artifact.
_CURRENCY_RE = re.compile(r"[$\u00a3\u20ac]|\bUSD\b")

#: Per-layer caveats about how a layer was run, carried into the document as prose. Ordered by layer.
#: A unit test pins a phrase from each entry, so none can be dropped from the document.
CARRIED_CAVEATS: tuple[tuple[str, str, str], ...] = (
    (
        "4",
        "TLS 1.2 is NOT enforced at CloudFront",
        "`minimum_protocol_version` is inert without a custom ACM certificate, so the live "
        "distribution reports `MinimumProtocolVersion: TLSv1`. Recorded as `defer` with reason "
        "`missing_custom_acm_certificate`, never a pass. The security baseline's Transport row "
        "is DEFER for the same reason.",
    ),
    (
        "4",
        "The `iam_roles` `leading_keys` skip for the pre-token-generation role",
        "That role's `reps` grant carries no `dynamodb:LeadingKeys` condition and is recorded "
        "`skip` with a stated reason, not `pass`. It is an accepted residual risk, held "
        "fail-closed by an exact-set tripwire.",
    ),
    (
        "4",
        "The `lambda_actions` and `iam_roles` skips",
        "The `lambda_actions` skips are authorization boundaries this invocation does not "
        "observe. The `iam_roles` skips are predominantly LeadingKeys conditions on grants where "
        "the condition is not applicable. A skip is not a pass.",
    ),
    (
        "4",
        "`iam_roles`' greens are scoped to what its script enumerates",
        "Its simulate matrix proves only the actions it enumerates.",
    ),
    (
        "4",
        "The `agents` 24/24 is a read-only isolation probe, not an agent turn",
        "The probe builds no `Store`, resolves no provider and dispatches no tool, so it can be "
        "green while the turn path is broken. A full turn is exercised only at Layers 5-6.",
    ),
    (
        "5",
        "Every Layer 5 inventory was produced against a dirty tree",
        "Each records `git_tree_dirty: true`, i.e. it was produced against work in progress "
        "rather than a clean commit (see the provenance column below).",
    ),
    (
        "5",
        "The proposal-chain evidence depends on the `after_payload` field allowlist",
        "Layer 5's proposal chain is verified with `after_payload` restricted to an allowlist of "
        "fields; without it the chain is not a passing control.",
    ),
    (
        "6",
        "The eval sweep does NOT exercise the live AgentCore Runtime",
        "Its provenance records `live-aws-lambda-dispatch`, not a Runtime ARN: the sweep "
        "dispatches to the action Lambdas directly. Live DynamoDB and live Lambdas are "
        "exercised; the deployed Runtime is not.",
    ),
    (
        "6",
        "OPEN product gap: no model has chosen `submit_proposal` in natural language",
        "The proposal card is proved to reach a caller as a structured `proposal` event by a "
        "directed invocation. The model-driven half is deferred, not claimed.",
    ),
    (
        "6",
        "OPEN product gap: no browser has rendered the proposal card",
        "The `proposal_card` inventory carries this as a `defer` in the artifact itself. No "
        "browser drives any verified path anywhere in Layers 4-6.",
    ),
    (
        "-",
        "17 declared surplus IAM grants remain",
        "`check_handler_grant_coverage` enumerates each: actions no handler source transitively "
        "reaches, and grants broader than the store calls behind them. Declared and reasoned, "
        "not eliminated.",
    ),
    (
        "-",
        "`scripts/package_lambdas.sh` is not byte-deterministic",
        "Re-running it produces a different zip hash for unchanged source, so roughly 27 "
        "resources show pending Lambda churn attributable to packaging rather than to any "
        "change under test. Bounded and disclosed, not eliminated.",
    ),
    (
        "-",
        "Offline dispatch diverges from the deployed Runtime's dispatch path",
        "`eval.harness`'s offline path dispatches in-process on the Gateway `_identity` channel; "
        "the deployed Runtime invokes the action Lambdas over `lambda:InvokeFunction` on the "
        "`_agent_identity` channel.",
    ),
    (
        "-",
        "There is no WAF anywhere",
        "On this architecture a WAF can only attach at CloudFront; the live distribution's "
        "`WebACLId` is empty. The security baseline does not list it as a control, so this is "
        "an open item rather than a documented violation.",
    ),
)


#: The unqualified claim the document makes only when every class's evidence is current. Shared
#: by :func:`render` and :func:`_assert_no_unqualified_verification_claim`.
_BLANKET_VERIFICATION_CLAIM: Final[str] = (
    "Every resource verified against the live deployment, with its outcome"
)

#: The heading under which per-class evidence age is published. Required whenever any class's
#: evidence is older than the reference or carries no provenance.
_EVIDENCE_AGE_HEADING: Final[str] = "## Evidence age — which classes predate the reference"


@dataclass(frozen=True)
class EvidenceAge:
    """One resource class's evidence age relative to the freshness reference."""

    resource_class: str
    generated_at: str | None
    older_by: timedelta | None


@dataclass(frozen=True)
class Freshness:
    """How current each class's evidence is relative to one reference instant.

    The reference is the deployment's last change when the caller supplies it, and otherwise the
    newest evidence in the document, labelled as a proxy.
    """

    reference: datetime | None
    reference_source: str
    current: tuple[EvidenceAge, ...]
    stale: tuple[EvidenceAge, ...]
    unknown: tuple[str, ...]

    @property
    def fully_current(self) -> bool:
        """True only when every class carries provenance at or after the reference."""
        return not self.stale and not self.unknown and self.reference is not None


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an artifact ``generated_at`` string, returning ``None`` when it is not a timestamp.

    An unparseable value is reported with the classes carrying no provenance, not as current.
    """
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def evidence_freshness(
    results: list[ResourceClassResult], *, deployment_changed_at: str | None = None
) -> Freshness:
    """Partition ``results`` by how their evidence ages against one reference instant.

    Args:
        results: Every parsed per-class artifact.
        deployment_changed_at: When the deployment this document describes last changed, in ISO
            8601. Without it the newest evidence in the document is used as a labelled proxy.

    Returns:
        The partition. ``reference`` is ``None`` only when no class carries a parseable timestamp
        and none was supplied, in which case every class is reported ``unknown``.
    """
    stamps = {
        result.resource_class: _parse_timestamp(result.provenance.generated_at)
        for result in results
        if result.provenance is not None
    }
    supplied = _parse_timestamp(deployment_changed_at) if deployment_changed_at else None
    known = [value for value in stamps.values() if value is not None]
    if supplied is not None:
        reference = supplied
        source = f"the deployment change time supplied by the caller ({supplied.isoformat()})"
    elif known:
        reference = max(known)
        source = (
            "the newest evidence in this document, used as a proxy because no deployment change "
            "time was supplied -- so a class shown below as older than the reference is older than "
            "the freshest evidence here, which is weaker than a statement that it predates the "
            "deployment"
        )
    else:
        reference = None
        source = "no reference could be established: no artifact carries a parseable timestamp"

    current: list[EvidenceAge] = []
    stale: list[EvidenceAge] = []
    unknown: list[str] = []
    for result in results:
        stamp = stamps.get(result.resource_class)
        if stamp is None or reference is None:
            unknown.append(result.resource_class)
            continue
        age = EvidenceAge(
            resource_class=result.resource_class,
            generated_at=stamp.isoformat(),
            older_by=reference - stamp,
        )
        (current if stamp >= reference else stale).append(age)
    return Freshness(
        reference=reference,
        reference_source=source,
        current=tuple(current),
        stale=tuple(stale),
        unknown=tuple(unknown),
    )


def _format_age(delta: timedelta) -> str:
    """Render an evidence age as whole days and hours, the resolution the claim turns on."""
    total_minutes = int(delta.total_seconds() // 60)
    days, remainder = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(remainder, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{minutes}m"]
    return " ".join(part for part in parts if part)


def _tenant_scoped_tables() -> tuple[str, ...]:
    """Return every tenant-scoped logical table name, read from the schema.

    Read from ``crm.store.schema``.
    """
    from crm.store.schema import TABLES

    return tuple(name for name in TABLES if not name.startswith("eval_"))


def load_inventories(inventory_dir: Path) -> list[ResourceClassResult]:
    """Parse every ``*.json`` inventory artifact in ``inventory_dir``, layer then class ordered.

    Raises:
        RuntimeError: ``inventory_dir`` holds no artifact, or one is unparseable.
    """
    if not inventory_dir.is_dir():
        raise RuntimeError(f"inventory directory not found: {inventory_dir}")
    results: list[ResourceClassResult] = []
    paths = sorted(inventory_dir.glob("*.json"))
    if not paths:
        raise RuntimeError(f"no inventory artifacts found in {inventory_dir}")
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeError(f"inventory artifact is not valid JSON: {path}: {exc}") from exc
        results.append(_result_from_json(raw, source=path))
    results.sort(key=lambda r: (r.layer, r.resource_class))
    return results


def _result_from_json(raw: Any, *, source: Path) -> ResourceClassResult:
    """Rebuild a :class:`ResourceClassResult` from one artifact's pinned JSON shape."""
    if not isinstance(raw, dict):
        raise RuntimeError(f"inventory artifact must be a JSON object: {source}")
    checks: list[CheckResult] = []
    for entry in raw.get("checks", []):
        status = str(entry.get("status", ""))
        if status not in _VALID_STATUSES:
            raise RuntimeError(
                f"inventory artifact {source} records unrecognised status {status!r} for check "
                f"{entry.get('check')!r}; expected one of {sorted(_VALID_STATUSES)}"
            )
        checks.append(
            CheckResult(
                resource=str(entry.get("resource", "")),
                check=str(entry.get("check", "")),
                status=cast("Status", status),
                detail=str(entry.get("detail", "")),
                defer_reason=str(entry.get("defer_reason", "")),
            )
        )
    provenance_raw = raw.get("provenance")
    provenance = None
    if isinstance(provenance_raw, dict):
        dirty = provenance_raw.get("git_tree_dirty")
        provenance = Provenance(
            generated_at=str(provenance_raw.get("generated_at", "")),
            git_commit=str(provenance_raw.get("git_commit", "")),
            git_tree_dirty=dirty if isinstance(dirty, bool) else None,
            region=str(provenance_raw.get("region", "")),
            resource_class=str(provenance_raw.get("resource_class", "")),
        )
    return ResourceClassResult(
        layer=int(raw.get("layer", 0)),
        resource_class=str(raw.get("resource_class", source.stem)),
        checks=tuple(checks),
        provenance=provenance,
    )


def residue_checks(
    *,
    profile: str | None,
    region: str,
    table_prefix: str,
    older_than: timedelta,
    outputs: str | None = None,
) -> ResourceClassResult:
    """Query live DynamoDB for surviving eval/smoke residue. Read-only; never writes.

    Three independent checks, each of which fails on residue:

    * ``no_residue_tenant_rows`` -- every tenant-scoped table swept for any surviving row whose
      ``tenant`` carries a ``smoke-``/``eval-`` prefix.
    * ``no_orphaned_run_older_than_threshold`` -- ``find_orphaned_runs`` at the threshold, i.e.
      the same primitive ``scripts/reap_eval_tenants.py`` uses.
    * ``eval_runs_catalogue_all_terminal`` -- every catalogue row reads ``torn_down``.

    ``demo_tenant_row_count`` reports the ``demo`` tenant's row count.

    ``outputs`` names the Terraform outputs snapshot describing the deployment the query is meant to
    read; when given, the resolved region and account are asserted against it before the first scan.
    """
    import boto3
    from scripts.smoke.harness import (
        assert_account_matches_outputs,
        smoke_client_config,
        smoke_context,
    )

    from crm.authz import DEMO_TENANT
    from crm.store.store import Store
    from eval.tenancy import find_orphaned_runs

    if outputs is not None:
        assert_account_matches_outputs(
            smoke_context(profile=profile, region=region, outputs=outputs)
        )

    session = boto3.session.Session(profile_name=profile, region_name=region)
    client = session.client("dynamodb", region_name=region, config=smoke_client_config())

    tenant_counts: Counter[str] = Counter()
    for logical in _tenant_scoped_tables():
        kwargs: dict[str, Any] = {
            "TableName": f"{table_prefix}{logical}",
            "ProjectionExpression": "#t",
            "ExpressionAttributeNames": {"#t": "tenant"},
        }
        while True:
            page = client.scan(**kwargs)
            for item in page.get("Items", []):
                tenant_counts[item.get("tenant", {}).get("S", "<none>")] += 1
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    residue = {t: n for t, n in tenant_counts.items() if t.startswith(_RESIDUE_PREFIXES)}
    checks = [
        CheckResult(
            resource=f"dynamodb:{table_prefix}* ({len(_tenant_scoped_tables())} tenant-scoped tables)",
            check="no_residue_tenant_rows",
            status=PASS if not residue else FAIL,
            detail=(
                f"swept {len(_tenant_scoped_tables())} tables; 0 rows under any "
                f"{'/'.join(_RESIDUE_PREFIXES)} tenant"
                if not residue
                else f"residue remains: {residue}"
            ),
        ),
        CheckResult(
            resource=f"dynamodb:{table_prefix}{_CATALOG_TABLE}",
            check="demo_tenant_row_count",
            status=PASS,
            detail=(
                f"{DEMO_TENANT!r} holds {tenant_counts.get(DEMO_TENANT, 0)} rows across all "
                f"{len(_tenant_scoped_tables())} tenant-scoped tables (read-only witness)"
            ),
        ),
    ]

    catalogue: list[dict[str, Any]] = []
    kwargs = {"TableName": f"{table_prefix}{_CATALOG_TABLE}"}
    while True:
        page = client.scan(**kwargs)
        catalogue.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    checks.append(catalogue_terminal_check(f"dynamodb:{table_prefix}{_CATALOG_TABLE}", catalogue))

    store = Store(client=client, table_prefix=table_prefix)
    orphans = find_orphaned_runs(store, older_than=older_than)
    checks.append(
        CheckResult(
            resource=f"dynamodb:{table_prefix}{_CATALOG_TABLE}",
            check="no_orphaned_run_older_than_threshold",
            status=PASS if not orphans else FAIL,
            detail=(
                f"find_orphaned_runs(older_than={older_than}) returned 0 candidates"
                if not orphans
                else f"orphans: {[o.run_id for o in orphans]}"
            ),
        )
    )

    return ResourceClassResult(
        layer=0,
        resource_class="residue",
        checks=tuple(checks),
        provenance=Provenance.capture(region=region, resource_class="residue"),
    )


def catalogue_terminal_check(resource: str, catalogue: list[dict[str, Any]]) -> CheckResult:
    """``eval_runs_catalogue_all_terminal`` over raw ``eval_runs`` items: every run row reads
    ``torn_down``. A ``job#<run_id>`` row (status :data:`eval.runs.JOB_ROW_STATUS`) is not a
    catalogue row and is not counted."""
    from eval.runs import JOB_ROW_STATUS

    runs = [item for item in catalogue if item.get("status", {}).get("S") != JOB_ROW_STATUS]
    statuses = Counter(item.get("status", {}).get("S", "<none>") for item in runs)
    non_terminal = {s: n for s, n in statuses.items() if s != "torn_down"}
    excluded = len(catalogue) - len(runs)
    return CheckResult(
        resource=resource,
        check="eval_runs_catalogue_all_terminal",
        status=PASS if not non_terminal else FAIL,
        detail=(
            f"{len(runs)} run rows, all 'torn_down' ({excluded} job rows not counted)"
            if not non_terminal
            else f"{len(runs)} run rows; non-terminal: {non_terminal} ({excluded} job rows not "
            "counted)"
        ),
    )


def _escape(text: str) -> str:
    """Make ``text`` safe inside a Markdown table cell, redacting any AWS account id.

    Every 12-digit run is rewritten to :data:`_PLACEHOLDER_ACCOUNT_ID`.
    """
    redacted = _ACCOUNT_ID_RE.sub(_PLACEHOLDER_ACCOUNT_ID, text)
    return redacted.replace("|", "\\|").replace("\n", " ")


def _status_marker(status: str) -> str:
    """Render one status as its published marker.

    ``defer`` renders ``DEFER`` and ``skip`` renders ``SKIP``.
    """
    markers: dict[str, str] = {PASS: "pass", FAIL: "**FAIL**", SKIP: "SKIP", DEFER: "**DEFER**"}
    return markers.get(status, status)


def _render_header(freshness: Freshness) -> list[str]:
    """Render the document's opening claim, qualified by what the evidence actually supports.

    The unqualified claim is emitted only when every class's evidence is at or after the reference;
    otherwise it is replaced by a claim that states the age spread.
    """
    provenance_line = (
        "Consolidated from the per-resource-class artifacts each smoke script wrote under "
        "`results/verification-inventory/`. Generated by `scripts/consolidate_inventory.py`; "
        "gitignored, like every artifact under `results/`."
    )
    if freshness.fully_current:
        return [
            f"{_BLANKET_VERIFICATION_CLAIM}. {provenance_line}",
            "",
            f"Every class's evidence is at or after the reference instant: "
            f"{freshness.reference_source}.",
            "",
        ]
    counts: list[str] = []
    if freshness.stale:
        counts.append(f"{len(freshness.stale)} carry evidence older than the reference")
    if freshness.unknown:
        counts.append(
            f"{len(freshness.unknown)} carry no usable provenance timestamp, so their age cannot "
            "be established at all"
        )
    total = len(freshness.current) + len(freshness.stale) + len(freshness.unknown)
    return [
        "Every resource each smoke script verified, with its outcome, **each as of the moment that "
        "class was last run and no later**. This document does NOT claim that every class was "
        f"verified against the deployment as it stands now: of {total} resource classes, "
        f"{' and '.join(counts)}. " + provenance_line,
        "",
        f"Freshness reference: {freshness.reference_source}. Per-class ages are in the "
        f'"{_EVIDENCE_AGE_HEADING.removeprefix("## ")}" section below, and every row of the summary '
        "table carries its own `Generated at`. A class whose evidence predates a change to the "
        "resources it describes has not been re-verified against them; only re-running that class "
        "produces evidence that it has.",
        "",
    ]


def _render_evidence_age(freshness: Freshness) -> list[str]:
    """Render the per-class evidence-age section, or nothing when every class is current."""
    if freshness.fully_current:
        return []
    lines = [
        _EVIDENCE_AGE_HEADING,
        "",
        f"Reference: {freshness.reference_source}.",
        "",
        "| Resource class | Evidence generated at | Older than reference by |",
        "|---|---|---|",
    ]
    for entry in sorted(freshness.stale, key=lambda e: e.older_by or timedelta(0), reverse=True):
        lines.append(
            f"| `{entry.resource_class}` | {entry.generated_at} | "
            f"{_format_age(entry.older_by) if entry.older_by else '—'} |"
        )
    for name in freshness.unknown:
        lines.append(f"| `{name}` | — (no usable provenance timestamp) | unknown |")
    for entry in freshness.current:
        lines.append(f"| `{entry.resource_class}` | {entry.generated_at} | at the reference |")
    lines.append("")
    return lines


def render(results: list[ResourceClassResult], *, deployment_changed_at: str | None = None) -> str:
    """Render the consolidated document.

    Args:
        results: Every parsed per-class artifact, plus the residue class when one was queried.
        deployment_changed_at: When the deployment last changed, in ISO 8601. Threaded to
            :func:`evidence_freshness`.
    """
    tally: Counter[str] = Counter()
    for result in results:
        for check in result.checks:
            tally[check.status] += 1
    total = sum(tally.values())
    freshness = evidence_freshness(results, deployment_changed_at=deployment_changed_at)

    lines: list[str] = [
        "# Consolidated verification inventory — Layers 4, 5 and 6",
        "",
        *_render_header(freshness),
        f"**{total} checks total — {tally[PASS]} pass, {tally[SKIP]} skip, "
        f"{tally[DEFER]} DEFER, {tally[FAIL]} fail.**",
        "",
        "A `DEFER` is a control that is genuinely **not verified and not in force**, held open on "
        "a named unmet prerequisite. It is never a pass. A `SKIP` is a boundary this invocation "
        "did not observe. Neither drives the exit code; only a `fail` does. Read "
        '"Caveats carried forward" below before citing any figure here — the count above is not '
        "a claim that the system is verified as a product.",
        "",
        *_render_evidence_age(freshness),
        "## Summary by resource class",
        "",
        "| Layer | Resource class | Checks | pass | SKIP | DEFER | fail | Generated at | Commit | Tree |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        counts: Counter[str] = Counter(c.status for c in result.checks)
        prov = result.provenance
        dirty = "—"
        if prov is not None and prov.git_tree_dirty is not None:
            dirty = "dirty" if prov.git_tree_dirty else "clean"
        elif prov is not None:
            dirty = "unknown"
        layer = "residue" if result.layer == 0 else str(result.layer)
        lines.append(
            f"| {layer} | `{result.resource_class}` | {len(result.checks)} | "
            f"{counts[PASS]} | {counts[SKIP]} | {counts[DEFER]} | {counts[FAIL]} | "
            f"{prov.generated_at if prov else '—'} | "
            f"{(prov.git_commit[:8] if prov and prov.git_commit else '—')} | {dirty} |"
        )
    lines.append("")
    missing_provenance = [r.resource_class for r in results if r.provenance is None]
    if missing_provenance:
        lines += [
            f"{len(missing_provenance)} artifact(s) carry **no provenance block** at all — "
            f"{', '.join(f'`{name}`' for name in missing_provenance)} — because they were produced "
            "before provenance was added. For those rows there "
            "is no record of which commit or which run produced the outcome; re-running the class "
            "is the only way to obtain one.",
            "",
        ]
    dirty_classes = [
        r.resource_class for r in results if r.provenance and r.provenance.git_tree_dirty
    ]
    if dirty_classes:
        lines += [
            f"{len(dirty_classes)} artifact(s) were produced against a **dirty tree**, i.e. against "
            "work in progress rather than a clean commit, so the recorded commit does not fully "
            f"describe the code that produced the outcome: "
            f"{', '.join(f'`{name}`' for name in dirty_classes)}.",
            "",
        ]

    if tally[DEFER]:
        lines += [
            "## Deferred controls — NOT verified, NOT enforced",
            "",
            "| Layer | Resource | Check | Reason | Detail |",
            "|---|---|---|---|---|",
        ]
        for result in results:
            for check in result.checks:
                if check.status != DEFER:
                    continue
                lines.append(
                    f"| {result.layer} | `{_escape(check.resource)}` | "
                    f"`{_escape(check.check)}` | `{_escape(check.defer_reason)}` | "
                    f"{_escape(check.detail)} |"
                )
        lines.append("")

    if tally[SKIP]:
        lines += [
            "## Skipped checks — boundary not observed by this invocation",
            "",
            "| Layer | Resource class | Resource | Check | Detail |",
            "|---|---|---|---|---|",
        ]
        for result in results:
            for check in result.checks:
                if check.status != SKIP:
                    continue
                lines.append(
                    f"| {result.layer} | `{result.resource_class}` | "
                    f"`{_escape(check.resource)}` | `{_escape(check.check)}` | "
                    f"{_escape(check.detail)} |"
                )
        lines.append("")

    lines += [
        "## Caveats carried forward",
        "",
        "| Layer | Caveat | What it means |",
        "|---|---|---|",
    ]
    for layer, title, body in CARRIED_CAVEATS:
        lines.append(f"| {layer} | {_escape(title)} | {_escape(body)} |")
    lines.append("")

    lines += ["## Every check, by layer and resource class", ""]
    for result in results:
        layer = "Residue query" if result.layer == 0 else f"Layer {result.layer}"
        lines += [
            f"### {layer} — `{result.resource_class}` ({len(result.checks)} checks)",
            "",
            "| Resource | Check | Outcome | Detail |",
            "|---|---|---|---|",
        ]
        for check in result.checks:
            reason = f" [reason={check.defer_reason}]" if check.defer_reason else ""
            lines.append(
                f"| `{_escape(check.resource)}` | `{_escape(check.check)}` | "
                f"{_status_marker(check.status)}{reason} | {_escape(check.detail)} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _assert_no_disposition_laundered(document: str, results: list[ResourceClassResult]) -> None:
    """Re-check the rendered document against the parsed artifacts before it is written.

    Refuses to publish a document that shows fewer deferred or failed checks than the artifacts
    recorded, that omits a ``defer_reason``, or that contains an account id other than the
    placeholder or a currency shape.
    """
    expected_defer = sum(1 for r in results for c in r.checks if c.status == DEFER)
    expected_fail = sum(1 for r in results for c in r.checks if c.status == FAIL)
    if document.count("**DEFER**") < expected_defer:
        raise RuntimeError(
            f"rendered document shows {document.count('**DEFER**')} DEFER markers but the "
            f"artifacts record {expected_defer} deferred checks — refusing to publish"
        )
    if document.count("**FAIL**") < expected_fail:
        raise RuntimeError(
            f"rendered document shows {document.count('**FAIL**')} FAIL markers but the "
            f"artifacts record {expected_fail} failed checks — refusing to publish"
        )
    for result in results:
        for check in result.checks:
            if check.status == DEFER and check.defer_reason not in document:
                raise RuntimeError(
                    f"defer_reason {check.defer_reason!r} for {check.check!r} is missing from the "
                    "rendered document — refusing to publish a deferred control without its reason"
                )
    account_ids = [
        found for found in _ACCOUNT_ID_RE.findall(document) if found != _PLACEHOLDER_ACCOUNT_ID
    ]
    if account_ids:
        raise RuntimeError(
            f"rendered document contains {len(account_ids)} 12-digit account-id-shaped value(s) "
            "— refusing to publish"
        )
    currency = _CURRENCY_RE.findall(document)
    if currency:
        raise RuntimeError(
            f"rendered document contains currency symbol(s) {sorted(set(currency))} — refusing to "
            "publish an absolute currency figure while pricing is provisional"
        )


def _assert_no_unqualified_verification_claim(document: str, freshness: Freshness) -> None:
    """Refuse to publish a present-tense verification claim the evidence does not support.

    The blanket claim must be absent, and the per-class age section present, when any class is
    stale or undateable.
    """
    if freshness.fully_current:
        return
    if _BLANKET_VERIFICATION_CLAIM in document:
        raise RuntimeError(
            f"rendered document makes the unqualified claim {_BLANKET_VERIFICATION_CLAIM!r} while "
            f"{len(freshness.stale)} class(es) carry evidence older than the reference and "
            f"{len(freshness.unknown)} carry none — refusing to publish"
        )
    if _EVIDENCE_AGE_HEADING not in document:
        raise RuntimeError(
            "rendered document omits the per-class evidence-age section while "
            f"{len(freshness.stale) + len(freshness.unknown)} class(es) are older than the "
            "reference or undateable — refusing to publish"
        )
    for entry in freshness.stale:
        if f"`{entry.resource_class}`" not in document:
            raise RuntimeError(
                f"stale class {entry.resource_class!r} is missing from the rendered document — "
                "refusing to publish an age disclosure that omits a class it covers"
            )


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default=None, help="AWS CLI profile for the residue query.")
    parser.add_argument(
        "--region",
        default=None,
        help=f"AWS region. Resolved as this flag, then AWS_REGION, then {DEFAULT_REGION!r}.",
    )
    parser.add_argument(
        "--table-prefix", default="", help="DynamoDB table prefix of the deployment."
    )
    parser.add_argument(
        "--outputs",
        default=None,
        help=(
            "Terraform outputs snapshot the residue query must match. When given, the resolved "
            "region and account are asserted against it and the query refuses a wrong target."
        ),
    )
    parser.add_argument(
        "--inventory-dir",
        default=str(REPO_ROOT / "results" / "verification-inventory"),
        help="Directory of per-resource-class inventory artifacts.",
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "results" / "verification-inventory.md"),
        help="Consolidated Markdown document to write.",
    )
    parser.add_argument(
        "--json-out",
        default=str(REPO_ROOT / "results" / "verification-inventory.json"),
        help="Consolidated JSON document to write.",
    )
    parser.add_argument(
        "--older-than", default="0h", help="Orphan age threshold for the residue query."
    )
    parser.add_argument(
        "--deployment-changed-at",
        default=None,
        help=(
            "ISO 8601 instant at which the deployment this document describes last changed "
            "(for example the newest LastModified across the deployed Lambda functions). Evidence "
            "older than this is reported as predating the deployment. Without it, staleness is "
            "measured against the newest evidence in the document and labelled as that proxy."
        ),
    )
    parser.add_argument(
        "--no-residue",
        action="store_true",
        help="Skip the live residue query (makes no AWS call). Offline consolidation only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 when nothing failed and no residue was found, 1 otherwise."""
    args = build_arg_parser().parse_args(argv)
    try:
        results = load_inventories(Path(args.inventory_dir))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.no_residue:
        from scripts.reap_eval_tenants import parse_duration, resolve_region

        results.append(
            residue_checks(
                profile=args.profile,
                region=resolve_region(args.region),
                table_prefix=args.table_prefix,
                older_than=parse_duration(args.older_than),
                outputs=args.outputs,
            )
        )
        results.sort(key=lambda r: (r.layer, r.resource_class))

    document = render(results, deployment_changed_at=args.deployment_changed_at)
    freshness = evidence_freshness(results, deployment_changed_at=args.deployment_changed_at)
    try:
        _assert_no_disposition_laundered(document, results)
        _assert_no_unqualified_verification_claim(document, freshness)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")
    Path(args.json_out).write_text(
        json.dumps([r.to_json_dict() for r in results], indent=2) + "\n", encoding="utf-8"
    )

    tally: Counter[str] = Counter(c.status for r in results for c in r.checks)
    print(
        f"consolidate_inventory: wrote {out_path} and {args.json_out} — "
        f"{sum(tally.values())} checks: {tally[PASS]} pass, {tally[SKIP]} skip, "
        f"{tally[DEFER]} DEFER, {tally[FAIL]} fail"
    )
    if tally[DEFER]:
        print(
            f"consolidate_inventory: {tally[DEFER]} control(s) DEFERRED — not verified and not "
            "enforced; see the 'Deferred controls' section",
            file=sys.stderr,
        )
    return 1 if tally[FAIL] else 0


__all__ = [
    "CARRIED_CAVEATS",
    "DEFER",
    "FAIL",
    "PASS",
    "SKIP",
    "CheckResult",
    "EvidenceAge",
    "Freshness",
    "Provenance",
    "ResourceClassResult",
    "build_arg_parser",
    "evidence_freshness",
    "load_inventories",
    "main",
    "render",
    "residue_checks",
]


if __name__ == "__main__":
    raise SystemExit(main())
