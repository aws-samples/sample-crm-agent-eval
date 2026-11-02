#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run every offline (Layers 1-3) shell/Python gate and report all failures together.

Usage: `python3 scripts/run_gates.py [--json | --census]`

Runs each gate in `GATES` to completion regardless of earlier failures, then prints a summary
naming every gate that failed with its captured stdout/stderr, and exits `0` only if no gate
failed. Does not run pytest suites or `ruff`/`mypy`/`shellcheck`; those are separate `make`
targets. Requires no AWS credential and makes no network call.

Each gate gets one of four dispositions. A gate that exits `GATE_SKIP_EXIT_STATUS` (3) ran but
verified nothing and is rendered `[SKIP]`. A gate that exits 0 while printing
:data:`GATE_DEFER_MARKER` lines is rendered `[PARTIAL]` with its deferred count; it is not a
failure and never `[PASS]`. Any other non-zero exit is `[FAIL]`. The headline is `INCOMPLETE` when
any gate skipped and `PARTIAL` when any deferred, and names every kind present. A gate listed in
`GATE_STRUCTURED_DEFERRED_ARGV` has its deferred count read from its own `--json` document and
cross-checked against its marker lines; a disagreement is a `[FAIL]`. A skipping gate's reason is
echoed and it is excluded from the "gates passed" count.

Set `CRM_RUN_GATES_INJECT` to override `GATES` with a test-owned list (one `name<TAB>command` per
line); used only by `tests/unit/test_verify_gate_aggregation.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import plan_fixtures, proc_run  # noqa: E402

#: The plan fixture `check_plan_security_baseline` reads, written by `run_all_gates` before any gate
#: runs (`scripts/plan_fixtures.py`).
COMPLIANT_PLAN_FIXTURE = plan_fixtures.DEFAULT_OUT_DIR / "compliant.json"

#: Test-only injection seam. When set, its value is split on newlines into `name<TAB>argv` pairs
#: (argv shell-split), and used in place of `GATES`. Never set outside of tests.
_INJECT_ENV_VAR = "CRM_RUN_GATES_INJECT"

#: Exit status by which a gate declares "I ran, and I verified nothing". A gate using it prints its
#: own reason on stdout; this script echoes that reason and never counts the gate as passed.
GATE_SKIP_EXIT_STATUS = 3

#: Marker a gate prints on stdout for each control it could not assert, having verified the rest.
#: A gate reporting deferred controls exits 0 and is rendered :data:`DISPOSITION_PARTIAL`. Gates
#: import this constant rather than spelling the marker themselves.
GATE_DEFER_MARKER = "[DEFER]"

#: The four dispositions a gate run can have.
DISPOSITION_PASS = "PASS"  # noqa: S105 - a status name, not a credential  # nosec B105
DISPOSITION_PARTIAL = "PARTIAL"
DISPOSITION_SKIP = "SKIP"
DISPOSITION_FAIL = "FAIL"

#: Key carrying the deferred-control count in a gate's own `--json` document.
GATE_STRUCTURED_DEFERRED_KEY = "deferred"

__all__ = [
    "DISPOSITION_FAIL",
    "DISPOSITION_PARTIAL",
    "DISPOSITION_PASS",
    "DISPOSITION_SKIP",
    "GATES",
    "GATE_DEFER_MARKER",
    "GATE_SKIP_EXIT_STATUS",
    "GATE_STRUCTURED_DEFERRED_ARGV",
    "GATE_STRUCTURED_DEFERRED_KEY",
    "TABLE_PREFIX_ENV_VAR",
    "deferred_control_count",
    "deployed_table_prefix",
    "final_summary_line",
    "main",
    "run_all_gates",
]


#: Where the deployment's name prefix is declared, and the pattern that reads it.
TABLE_PREFIX_ENV_VAR = "CRM_TABLE_PREFIX"
_INFRA_MAIN = REPO_ROOT / "infra" / "main.tf"
_NAME_PREFIX_RE = re.compile(r'^\s*name_prefix\s*=\s*"([^"]+)"', re.MULTILINE)


def deployed_table_prefix(env: Mapping[str, str] | None = None) -> str:
    """The DynamoDB table prefix a gate needs in order to address the deployed tables.

    An explicit :data:`TABLE_PREFIX_ENV_VAR` wins; otherwise the ``name_prefix`` local declared in
    ``infra/main.tf`` is read. Returns ``""`` when neither is available.
    """
    environment = env if env is not None else dict(os.environ)
    from_env = environment.get(TABLE_PREFIX_ENV_VAR, "").strip()
    if from_env:
        return from_env
    try:
        declared = _NAME_PREFIX_RE.search(_INFRA_MAIN.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return declared.group(1) if declared else ""


def deferred_control_count(stdout: str) -> int:
    """Count the controls a gate reported as deferred, by counting :data:`GATE_DEFER_MARKER` lines.

    Counts lines carrying the marker, not occurrences of the substring. Used directly for a gate with
    no structured report (:data:`GATE_STRUCTURED_DEFERRED_ARGV`), and as the cross-check for one with.
    """
    return sum(1 for line in stdout.splitlines() if GATE_DEFER_MARKER in line)


def final_summary_line(stdout: str) -> str | None:
    """A gate's own last non-empty, non-marker stdout line -- its denominator for the defers above.

    Echoed alongside the deferred lines in the `[PARTIAL]` block.
    """
    for line in reversed(stdout.splitlines()):
        if line.strip() and GATE_DEFER_MARKER not in line:
            return line.rstrip()
    return None


#: Each gate is (name, argv). All paths are relative to REPO_ROOT; argv[0] is run via the
#: current interpreter's isolated venv where the gate is a Python script (`sys.executable`),
#: or bash where it is a shell script.
GATES: list[tuple[str, list[str]]] = [
    # Both content-hygiene gates run twice: once over the working tree (`--worktree`) and once over
    # committed history (`--ref HEAD`, the `_head` names).
    ("check_no_account_id", ["bash", "scripts/check_no_account_id.sh", "--worktree"]),
    ("check_no_account_id_head", ["bash", "scripts/check_no_account_id.sh", "--ref", "HEAD"]),
    ("check_no_vendor_keys", ["bash", "scripts/check_no_vendor_keys.sh", "--worktree"]),
    ("check_no_vendor_keys_head", ["bash", "scripts/check_no_vendor_keys.sh", "--ref", "HEAD"]),
    # No git commit id in any tracked text file; reads contents only, never history.
    ("check_no_commit_ids", [sys.executable, "scripts/check_no_commit_ids.py"]),
    # No comment, docstring or tracked document references content that is not tracked.
    ("check_no_untracked_refs", [sys.executable, "scripts/check_no_untracked_refs.py"]),
    ("check_log_redaction", [sys.executable, "scripts/check_log_redaction.py"]),
    ("check_license_headers", ["bash", "scripts/check_license_headers.sh", "--worktree"]),
    (
        "check_toolspec_parity",
        [sys.executable, "scripts/check_toolspec_parity.py", "--skip-frontend"],
    ),
    ("check_pin_drift", [sys.executable, "scripts/check_pin_drift.py"]),
    ("check_cost_withheld", ["bash", "scripts/check_cost_withheld.sh"]),
    # Every first-party import must resolve in the tracked tree (materialised with `git archive`).
    (
        "check_tracked_tree_self_contained",
        [sys.executable, "scripts/check_tracked_tree_self_contained.py"],
    ),
    # Every process started from scripts/ or tests/ goes through scripts/proc_run.py.
    ("check_subprocess_funnel", [sys.executable, "scripts/check_subprocess_funnel.py"]),
    # Documentation figures: linked, plain-text draw.io sources, SVG exports with no external URL.
    ("check_doc_figures", [sys.executable, "scripts/check_doc_figures.py"]),
    # Tracked Markdown links resolve (targets and anchors), and every `.md` is reachable from README.
    ("check_doc_links", [sys.executable, "scripts/check_doc_links.py"]),
    # No `grep -q` / `head` / `sed Nq` pipe consumer in shell, Makefile or CI code under pipefail.
    ("check_pipefail_sigpipe", [sys.executable, "scripts/check_pipefail_sigpipe.py"]),
    # Secret hygiene for untracked generated artifacts such as `results/`.
    (
        "check_untracked_artifact_secrets",
        [sys.executable, "scripts/check_untracked_artifact_secrets.py"],
    ),
    # gitleaks over the filesystem: the local `.gitleaks.toml` if present, else built-in rules.
    # Exits 3 ([SKIP]) when gitleaks is absent.
    ("check_secret_scan", [sys.executable, "scripts/check_secret_scan.py"]),
    # Every static class token in first-party source must resolve to a rule in the built CSS. Needs
    # `npm ci` in `frontend/`; exits 3 ([SKIP]) without `node_modules`.
    ("check_emitted_css", [sys.executable, "scripts/check_emitted_css.py"]),
    ("verify_fixtures", ["bash", "scripts/verify_fixtures.sh"]),
    (
        "check_plan_security_baseline",
        [
            sys.executable,
            "scripts/check_plan_security_baseline.py",
            "--plan",
            str(COMPLIANT_PLAN_FIXTURE.relative_to(REPO_ROOT)),
        ],
    ),
    # No NONE-authorized Function URL, no wildcard-principal invoke permission, no live
    # `execute-api` endpoint, no VPC-less Lambda, no public S3 surface, asserted on source.
    ("check_no_public_exposure", [sys.executable, "scripts/check_no_public_exposure.py"]),
    (
        # `local.vpc_interface_endpoint_services` must cover the AWS services `src/` builds clients for.
        "check_vpc_endpoint_coverage",
        [sys.executable, "scripts/check_vpc_endpoint_coverage.py"],
    ),
    ("check_gsi_projection_parity", [sys.executable, "scripts/check_gsi_projection_parity.py"]),
    (
        "check_handler_grant_coverage",
        [sys.executable, "scripts/check_handler_grant_coverage.py"],
    ),
    (
        "check_payload_validation_coverage",
        [sys.executable, "scripts/check_payload_validation_coverage.py"],
    ),
    (
        "check_lambda_zip_imports",
        [sys.executable, "scripts/check_lambda_zip_imports.py"],
    ),
    (
        "check_lambda_adapter_isolation",
        [sys.executable, "scripts/check_lambda_adapter_isolation.py"],
    ),
    (
        # Three `terraform init`+`plan` runs in temp dirs against a mock provider; the slowest gate.
        "verify_terraform_plan_with_kms_absent",
        ["bash", "scripts/verify_terraform_plan_with_kms_absent.sh"],
    ),
    ("check_sse_schema_parity", [sys.executable, "scripts/check_sse_schema_parity.py"]),
    (
        # Each gate's documented row must name every phase listed in that gate's module docstring.
        "check_readme_gate_phases",
        [sys.executable, "scripts/check_readme_gate_phases.py"],
    ),
    ("check_relay_thinness", [sys.executable, "scripts/check_relay_thinness.py"]),
    (
        # Without `--require-zip`: a clean clone has no `infra/build/`, so this exits 3 there.
        # `scripts/deploy.sh` passes `--require-zip`.
        "check_relay_zip",
        [sys.executable, "scripts/check_relay_zip.py"],
    ),
    (
        # The relay's own `IDENTITY_CHANNEL_KEYS` must cover the Python constant.
        "check_identity_channel_parity",
        [sys.executable, "scripts/check_identity_channel_parity.py"],
    ),
    ("check_page_context_no_rows", [sys.executable, "scripts/check_page_context_no_rows.py"]),
    ("check_design_tokens", [sys.executable, "scripts/check_design_tokens.py"]),
    ("check_testid_preservation", [sys.executable, "scripts/check_testid_preservation.py"]),
    (
        # No default-region literal or AZ id outside `infra/regions.json` and the pinned exemptions;
        # no "only supported region" phrasing in tracked Markdown.
        "check_region_literals",
        [
            sys.executable,
            "scripts/check_region_literals.py",
            "--check",
            "literals",
            "--check",
            "phrases",
        ],
    ),
    ("check_region_map", [sys.executable, "scripts/region_map.py", "check"]),
    (
        # Passes no `--outputs`, so the gate always exits 3 here; run it by hand with `--outputs`.
        # Profile and region come from the inherited environment via `crm.config.resolve_aws_config`.
        "check_canary_tenant_empty",
        [
            sys.executable,
            "scripts/check_canary_tenant_empty.py",
            "--table-prefix",
            deployed_table_prefix(),
        ],
    ),
]

#: Gates that report their deferred-control count as structured data, mapped to the extra argv that
#: asks for it. For these the deferred count is the parsed integer field
#: :data:`GATE_STRUCTURED_DEFERRED_KEY`, at the cost of one extra run of the gate.
GATE_STRUCTURED_DEFERRED_ARGV: dict[str, tuple[str, ...]] = {
    "check_plan_security_baseline": ("--json",),
}


def _structured_deferred_count(name: str, argv: list[str]) -> tuple[int | None, str | None]:
    """`(count, problem)` from a gate's own structured report.

    `(None, None)` means the gate declares no structured report and the marker count stands. A
    non-`None` `problem` means the gate declares one and it could not be read; that is reported as a
    failure, with no fallback to the marker count.
    """
    extra = GATE_STRUCTURED_DEFERRED_ARGV.get(name)
    if extra is None:
        return None, None
    result = proc_run.run(
        [*argv, *extra],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        # Gate output may echo non-UTF-8 bytes from tracked content.
        errors="replace",
        check=False,
    )
    joined = " ".join(extra)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, (
            f"declares a structured deferred count but `{joined}` emitted no parseable JSON "
            f"({exc}); stderr: {result.stderr.strip()[:400]}"
        )
    value = payload.get(GATE_STRUCTURED_DEFERRED_KEY) if isinstance(payload, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        return None, (
            f"declares a structured deferred count but `{joined}` produced no integer "
            f"{GATE_STRUCTURED_DEFERRED_KEY!r} field (got {value!r})"
        )
    return value, None


def _run_gate(name: str, argv: list[str]) -> dict[str, object]:
    """Run one gate to completion and return its result; never raises on a non-zero exit."""
    result = proc_run.run(
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    skipped = result.returncode == GATE_SKIP_EXIT_STATUS
    rendered = deferred_control_count(result.stdout)
    structured, problem = _structured_deferred_count(name, argv)
    if structured is None:
        deferred = rendered
    else:
        deferred = structured
        if structured != rendered:
            problem = (
                f"reports {structured} deferred control(s) in its structured output but rendered "
                f"{rendered} {GATE_DEFER_MARKER} line(s) on stdout. The disposition cannot be "
                "trusted while the two disagree -- one of them has drifted"
            )
    # `passed` is exactly "exited 0"; `disposition` carries the finer distinction.
    passed = result.returncode == 0
    if not passed and not skipped:
        # A real finding outranks every softer disposition, including an unreadable defer count.
        disposition = DISPOSITION_FAIL
    elif problem is not None:
        disposition = DISPOSITION_FAIL
    elif skipped:
        disposition = DISPOSITION_SKIP
    elif deferred:
        disposition = DISPOSITION_PARTIAL
    else:
        disposition = DISPOSITION_PASS
    return {
        "name": name,
        "argv": argv,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "skipped": skipped,
        "passed": passed,
        "deferred_controls": deferred,
        "rendered_deferred_controls": rendered,
        "deferred_count_problem": problem,
        "disposition": disposition,
    }


def _gates_from_env() -> list[tuple[str, list[str]]] | None:
    """Parse `CRM_RUN_GATES_INJECT` into a gate list, or return `None` if unset.

    Format: one gate per line, `name<TAB>shell-command`. Test-only seam for proving
    collect-all-failures without mutating any real gate script or fixture.
    """
    raw = os.environ.get(_INJECT_ENV_VAR)
    if raw is None:
        return None
    gates: list[tuple[str, list[str]]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        name, _, command = line.partition("\t")
        gates.append((name, shlex.split(command)))
    return gates


def _materialize_plan_fixture() -> None:
    """Write :data:`COMPLIANT_PLAN_FIXTURE`; on failure remove any earlier copy so the gate fails."""
    try:
        plan_fixtures.materialize(COMPLIANT_PLAN_FIXTURE.stem, COMPLIANT_PLAN_FIXTURE.parent)
    except (OSError, ValueError) as exc:
        COMPLIANT_PLAN_FIXTURE.unlink(missing_ok=True)
        print(f"run_gates: could not materialize {COMPLIANT_PLAN_FIXTURE}: {exc}", file=sys.stderr)


def run_all_gates() -> list[dict[str, object]]:
    """Run every gate in `GATES` (or the injected set), continuing past failures, and return all
    results in order."""
    gates = _gates_from_env()
    if gates is None:
        _materialize_plan_fixture()
        gates = GATES
    return [_run_gate(name, argv) for name, argv in gates]


def _print_human_summary(results: list[dict[str, object]]) -> None:
    # Grouped by `disposition`: a gate whose deferred count is untrustworthy exits 0 yet is a failure.
    skipped = [r for r in results if r["disposition"] == DISPOSITION_SKIP]
    failures = [r for r in results if r["disposition"] == DISPOSITION_FAIL]
    partial = [r for r in results if r["disposition"] == DISPOSITION_PARTIAL]
    deferred_total = sum(c for r in results if isinstance(c := r["deferred_controls"], int))
    for result in results:
        status = str(result["disposition"])
        suffix = (
            f" ({result['deferred_controls']} control(s) deferred)"
            if status == DISPOSITION_PARTIAL
            else ""
        )
        print(f"[{status}] {result['name']}{suffix}")
    print()
    executed = len(results) - len(skipped)
    if failures:
        print(f"FAILED: {len(failures)}/{len(results)} gate(s) failed\n")
        for result in failures:
            print(f"--- {result['name']} (exit {result['returncode']}) ---")
            if result["deferred_count_problem"]:
                print(f"deferred-count defect: {result['name']} {result['deferred_count_problem']}")
            if result["stdout"]:
                print(str(result["stdout"]).rstrip())
            if result["stderr"]:
                print(str(result["stderr"]).rstrip())
            print()
    elif skipped or partial:
        # One clause per kind of unverified work present.
        clauses = []
        if skipped:
            clauses.append(f"{len(skipped)}/{len(results)} gate(s) SKIPPED having verified nothing")
        if partial:
            clauses.append(
                f"{deferred_total} control(s) across {len(partial)} gate(s) were DEFERRED and are "
                "NOT asserted"
            )
        # Any skip makes the board INCOMPLETE; skipped gates are excluded from the passed count.
        label = "INCOMPLETE" if skipped else DISPOSITION_PARTIAL
        counted = (
            f"{executed}/{executed} executed gates"
            if skipped
            else f"{len(results)}/{len(results)} gates"
        )
        print(f"{label}: {counted} passed, but {', and '.join(clauses)} (see the detail below)\n")
    else:
        print(f"OK: {len(results)}/{len(results)} gates passed")

    for result in partial:
        print(
            f"--- {result['name']} (PARTIAL: {result['deferred_controls']} control(s) deferred, "
            "not asserted) ---"
        )
        if result["stdout"]:
            stdout = str(result["stdout"])
            deferred_lines = [line for line in stdout.splitlines() if GATE_DEFER_MARKER in line]
            print("\n".join(deferred_lines).rstrip())
            summary = final_summary_line(stdout)
            if summary is not None:
                print(summary)
        print()

    # A skipping gate's reason is echoed on every run, passing runs included.
    for result in skipped:
        print(f"--- {result['name']} (SKIPPED, exit {result['returncode']}: verified nothing) ---")
        if result["stdout"]:
            print(str(result["stdout"]).rstrip())
        if result["stderr"]:
            print(str(result["stderr"]).rstrip())
        print()


def disposition_census(results: list[dict[str, object]]) -> dict[str, int]:
    """`{disposition: count}` over every disposition, including the ones that are zero.

    Every disposition is a key even at zero.
    """
    counts = dict.fromkeys(
        (DISPOSITION_PASS, DISPOSITION_PARTIAL, DISPOSITION_SKIP, DISPOSITION_FAIL), 0
    )
    for result in results:
        counts[str(result["disposition"])] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of text."
    )
    parser.add_argument(
        "--census",
        action="store_true",
        help="Emit only the {disposition: count} census as JSON, for pasting into a report verbatim.",
    )
    args = parser.parse_args(argv)

    results = run_all_gates()

    if args.census:
        print(json.dumps(disposition_census(results), indent=2))
    elif args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_human_summary(results)

    # Keyed on `disposition`, so an untrustworthy deferred count fails the run despite exit 0.
    return 0 if all(r["disposition"] != DISPOSITION_FAIL for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
