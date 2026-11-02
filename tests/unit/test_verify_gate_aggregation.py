# SPDX-License-Identifier: MIT
"""Tests that `make verify` collects every offline gate failure, needs no AWS credential, and does
not depend on `make verify-deploy` (Layers 4-6).

A module-scoped fixture runs `scripts/run_gates.py --json` once and every whole-repository assertion
reads it; `test_the_module_runs_each_expensive_subprocess_exactly_once` checks that under
`--dist loadgroup`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from scripts import plan_fixtures, proc_run

from tests.unit._deferred_tally import EXPECTED_DEFERRED_BY_CONTROL

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE_PATH = REPO_ROOT / "Makefile"
RUN_GATES = REPO_ROOT / "scripts" / "run_gates.py"

#: Pins every test in this module to one pytest-xdist worker (requires `--dist loadgroup`).
pytestmark = pytest.mark.xdist_group("run_gates")

#: Expected count per label of the expensive subprocesses recorded in `_invocation_log`.
_EXPECTED_INVOCATIONS = {"run_gates --json": 1, "check_plan_security_baseline": 1}

#: Wall-clock upper bound on a whole-repository `run_gates.py` run, to fail a hung subprocess.
_RUN_GATES_TIMEOUT_SECONDS = 1800


@pytest.fixture(scope="session")
def _invocation_log(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Path to the invocation log, in the run-scoped temp root shared by every xdist worker.

    Under xdist each worker's base temp is a `popen-gw*` subdirectory of that root; serially the
    root is the base temp.
    """
    base = tmp_path_factory.getbasetemp()
    root = base.parent if base.name.startswith("popen-gw") else base
    return root / "run_gates_invocations.log"


def _record_invocation(log: Path, label: str) -> None:
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"{label}\t{os.environ.get('PYTEST_XDIST_WORKER', 'serial')}\t{os.getpid()}\n")


class RunGatesRun(NamedTuple):
    """One `scripts/run_gates.py --json` run: its exit status, output, and parsed gate results."""

    returncode: int
    stdout: str
    stderr: str
    report: list[dict[str, object]]


@pytest.fixture(scope="module")
def run_gates_json_run(_invocation_log: Path) -> RunGatesRun:
    """The module's single `scripts/run_gates.py --json` run."""
    _record_invocation(_invocation_log, "run_gates --json")
    result = proc_run.run(
        [sys.executable, str(RUN_GATES), "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_RUN_GATES_TIMEOUT_SECONDS,
    )
    report = json.loads(result.stdout)
    assert isinstance(report, list) and report, "run_gates --json produced no gate results"
    return RunGatesRun(result.returncode, result.stdout, result.stderr, report)


@pytest.fixture(scope="module")
def run_gates_report(run_gates_json_run: RunGatesRun) -> list[dict[str, object]]:
    """Every gate's machine-readable result, from :func:`run_gates_json_run`."""
    return run_gates_json_run.report


def _run_gates_module() -> Any:
    """Import and return the real `scripts/run_gates.py`."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import run_gates  # type: ignore[import-not-found]

    return run_gates


def _baseline_gate_module() -> Any:
    """Import and return the real `scripts/check_plan_security_baseline.py`."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_plan_security_baseline  # type: ignore[import-not-found]

    return check_plan_security_baseline


def _parse_make_targets(makefile_text: str) -> dict[str, list[str]]:
    """Return target name -> recipe lines, parsed from a GNU Makefile."""
    targets: dict[str, list[str]] = {}
    current: str | None = None
    header_pattern = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:(?!=)")
    for line in makefile_text.splitlines():
        if line.startswith("\t") and current is not None:
            targets[current].append(line[1:])
            continue
        header_match = header_pattern.match(line)
        if header_match:
            current = header_match.group(1)
            targets.setdefault(current, [])
            continue
        if line.strip() == "":
            current = None
    return targets


def _recipe_text_for(targets: dict[str, list[str]], name: str) -> str:
    return "\n".join(targets.get(name, []))


def _referenced_sub_target_names(verify_recipe: str) -> list[str]:
    match = re.search(r"\$\(MAKE\)\s+(?:-\S+\s+)*(.+)$", verify_recipe.strip())
    if not match:
        return []
    return match.group(1).split()


# ---------------------------------------------------------------------------
# 1. Collect-all-failures, proven with two simultaneous failures.
# ---------------------------------------------------------------------------


def test_run_gates_collects_two_simultaneous_failures_in_one_report() -> None:
    """Two always-failing gates injected via `CRM_RUN_GATES_INJECT` are both reported in one run."""
    injected = (
        "GATE_A\tsh -c 'echo GATE_A_FAILED; exit 1'\nGATE_B\tsh -c 'echo GATE_B_FAILED; exit 1'\n"
    )
    result = proc_run.run(
        [sys.executable, str(RUN_GATES)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "CRM_RUN_GATES_INJECT": injected},
    )

    combined_output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "GATE_A_FAILED" in combined_output
    assert "GATE_B_FAILED" in combined_output
    assert "[FAIL] GATE_A" in combined_output
    assert "[FAIL] GATE_B" in combined_output


#: Gates expected to report `[SKIP]` (exit 3) offline, each with its reason and where it is covered.
#: The set is exact in both directions; an entry with a probe in `SKIP_PRECONDITION_PROBES` must
#: skip exactly when its input is absent.
EXPECTED_OFFLINE_SKIPS: dict[str, str] = {
    "check_canary_tenant_empty": (
        "no DynamoDB store is reachable offline; its coverage is the credentialed deploy-verification invocation "
        "with --require-store"
    ),
    "check_emitted_css": (
        "The gate compiles the real stylesheet, so it needs `frontend/node_modules/@tailwindcss/postcss` "
        "and a `node`. The CI `verify` job cannot have them: it installs the RELAY's Node major (22, "
        "derived from the relay's Lambda runtime) and runs `npm ci` only in src/relay, while "
        "frontend/package.json pins `engines.node` to an exact 26.13.0 -- so the toolchain cannot exist "
        "in that job and the skip there is structural, not a missing `npm ci`. "
        "Coverage is the SEPARATE `frontend` job in both .gitlab-ci.yml and "
        ".github/workflows/verify.yml, which owns the frontend Node pin, runs `npm ci` in frontend/, "
        "and runs `pytest tests/unit/test_check_emitted_css.py` with CRM_REQUIRE_EMITTED_CSS=1 -- the "
        "variable that converts this same absent-toolchain skip into a hard failure where the "
        "toolchain CAN exist, so the coverage cannot go quietly missing. "
        "This entry is CONDITIONAL (see SKIP_PRECONDITION_PROBES): where the toolchain is present -- a "
        "contributor laptop, the `frontend` job -- the gate must NOT skip, and it does not: measured "
        "[PASS] locally on 2026-09-15. Declaring it unconditionally would have exempted it everywhere "
        "and red on this laptop in the stale direction. "
        "REMOVAL TRIGGER: the `verify` job gaining the frontend toolchain (a second Node install, or "
        "the two Node pins converging), at which point the probe reports the toolchain present in CI "
        "too and the entry must go."
    ),
    "check_lambda_zip_imports": (
        "The gate audits built artefacts, and no expected zip exists under infra/build/lambda in any "
        "offline job: the directory is gitignored and nothing in the `verify` job runs "
        "scripts/package_lambdas.sh (which needs network access for pip). Its own skip reason says so, "
        "naming 28 expected and 0 present. "
        "Its coverage is the deploy path: scripts/deploy.sh runs it as "
        "`check_lambda_zip_imports.py --require-zips` after packaging and before `terraform apply`, "
        "and --require-zips makes an absent zip an exit 1 rather than this skip -- the same "
        "skip-becomes-failure-where-the-input-can-exist contract CRM_REQUIRE_EMITTED_CSS gives the "
        "gate above. The gate's LOGIC is additionally covered host-independently by "
        "tests/unit/test_lambda_zip_imports.py against synthetic zips (including the assertion that "
        "run_gates renders this gate [SKIP] and never [PASS]), but that citation carries a qualifier: "
        "the subset of those tests needing a real artefact `pytest.skip` when the build dir is empty, "
        "so in CI they assert nothing about a real zip either. "
        "This entry is CONDITIONAL (see SKIP_PRECONDITION_PROBES): where zips have been built the gate "
        "must NOT skip -- measured [PASS] over 28/28 zips locally on 2026-09-15. A built-zip host on "
        "which it FAILS is a real defect and stays a failure, owned by "
        "test_no_gate_fails_against_the_real_repository, never absorbed here. "
        "REMOVAL TRIGGER: an offline job that builds the zips before running the gates, at which point "
        "the probe reports them present in CI too and the entry must go."
    ),
    "check_relay_zip": (
        "Same shape as the entry above and the same reason: no `infra/build/lambda/relay.zip` exists in "
        "any offline job, because the directory is gitignored and nothing in the `verify` job runs "
        "scripts/package_relay.sh (which needs npm). Its skip reason says so by name. "
        "Before this gate was registered its no-zip branch returned PASS, which would have put a green "
        "verdict on the board for an artefact nobody had read; it is exit 3 now, which is what made this "
        "declaration necessary. "
        "Its coverage is the deploy path: scripts/deploy.sh runs it as `--require-zip` after packaging "
        "and treats even exit 3 as fatal, so an unknown zip cannot reach an apply. The gate's LOGIC is "
        "covered host-independently by tests/unit/test_check_relay_zip.py against synthetic zips, "
        "including both no-zip dispositions. "
        "This entry is CONDITIONAL (see SKIP_PRECONDITION_PROBES): where the zip has been built the gate "
        "must NOT skip -- measured [PASS] over 10 compile inputs locally on 2026-09-17. Note the two "
        "conditional zip entries can disagree on one host, because package_lambdas.sh `rm -rf`s the "
        "directory this zip lives in: after running the two packagers in that order the OTHER zips are "
        "present and this one is not. "
        "REMOVAL TRIGGER: an offline job that runs scripts/package_relay.sh before the gates, at which "
        "point the probe reports the zip present in CI too and the entry must go."
    ),
}


def _emitted_css_toolchain_absent() -> str | None:
    """Why `check_emitted_css` cannot compile here, or `None` if it can.

    Delegates to `tests/unit/test_check_emitted_css.py`'s own toolchain probe.
    """
    from tests.unit.test_check_emitted_css import stylesheet_toolchain_unavailable_reason

    return stylesheet_toolchain_unavailable_reason()


def _lambda_zips_absent() -> str | None:
    """Why `check_lambda_zip_imports` has nothing to audit here, or `None` if it has.

    Mirrors the gate's predicate (no expected zip present), with zip names from `expected_zips()`
    and the build dir from `LAMBDA_BUILD_DIR` or the gate's `DEFAULT_BUILD_DIR`.
    """
    gate = _lambda_zip_gate_module()
    build_dir = Path(os.environ.get("LAMBDA_BUILD_DIR", str(gate.DEFAULT_BUILD_DIR))).resolve()
    specs = gate.expected_zips()
    present = [s for s in specs if (build_dir / s.zip_name).is_file()]
    if present:
        return None
    return f"no expected Lambda zip under {build_dir} ({len(specs)} expected)"


def _relay_zip_absent() -> str | None:
    """Why `check_relay_zip` has nothing to audit here, or `None` if it has.

    Mirrors the gate's predicate (zip absent), with path and name from the gate's own constants.
    """
    gate = _relay_zip_gate_module()
    zip_path = gate.DEFAULT_BUILD_DIR / gate.RELAY_ZIP_NAME
    if zip_path.is_file():
        return None
    return f"no relay zip at {zip_path}"


def _relay_zip_gate_module() -> Any:
    """The real `scripts/check_relay_zip.py`, for its zip path and name."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_relay_zip  # type: ignore[import-not-found]

    return check_relay_zip


def _lambda_zip_gate_module() -> Any:
    """The real `scripts/check_lambda_zip_imports.py`, for its expected-zip list."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_lambda_zip_imports  # type: ignore[import-not-found]

    return check_lambda_zip_imports


#: Precondition probes for conditional entries in `EXPECTED_OFFLINE_SKIPS`. `None` means the gate's
#: input exists here, so it must not skip; a string means it is absent, so it must skip, and why.
SKIP_PRECONDITION_PROBES: dict[str, Callable[[], str | None]] = {
    "check_emitted_css": _emitted_css_toolchain_absent,
    "check_lambda_zip_imports": _lambda_zips_absent,
    "check_relay_zip": _relay_zip_absent,
}

#: Gates expected to report `[PARTIAL]` offline, each with its reason, where the deferred controls
#: are covered, and a removal trigger. The set is exact in both directions.
EXPECTED_OFFLINE_PARTIALS: dict[str, str] = {
    "check_toolspec_parity": (
        "`run_gates.py` registers this gate with `--skip-frontend`, so the generated frontend "
        "consumer (frontend/src/types/generated/toolSchemas.ts) is never regenerated and its "
        "byte-diff asserts nothing -- one deferred control, which the gate emits as its own [DEFER] "
        "line naming the consumer and the flag. The Terraform, handler-validator and tool-count "
        "halves DO run, so this is a gate that asserted most of its consumers, not one that stopped "
        "asserting. The flag is there because the CI `verify` job installs npm dependencies only in "
        "src/relay, so frontend/node_modules does not exist in it. "
        "Coverage for the deferred control is the SEPARATE `frontend` job in "
        ".github/workflows/verify.yml, and it is worth being exact about its form: that job owns the "
        "property STRUCTURALLY rather than as a check. It runs `npm run gen:types` -- regenerating "
        "from crm.toolspec.definitions -- and then lints, typechecks, tests and builds against the "
        "regenerated output, so a toolspec-vs-source disagreement fails `tsc` there. A *diff* could "
        "not own it: the generated path is gitignored, so `git diff` reports nothing about it and such "
        "a check would pass vacuously. "
        "The uncomfortable half of that answer, stated because 'covered nowhere' and 'covered by a "
        "gate' are materially different facts and this is neither: NO GATE STANDS BEHIND IT. "
        "verify.yml says so itself -- delete the generate step and the coverage leaves with it, "
        "silently, with nothing in this suite going red. The frontend half IS also exercised by "
        "test_gates.py's test_toolspec_parity_passes_against_the_real_repository_including_frontend "
        "and test_tampered_generated_frontend_types_fails -- but that coverage is CONDITIONAL and must "
        "not be cited unqualified: both `skipif` on frontend/node_modules, and the CI `verify` job's "
        "only `npm ci` is `working-directory: src/relay`, so in CI they SKIP and assert nothing. They "
        "run only on a contributor machine that has installed frontend/ dependencies. Citing them "
        "without that qualifier would point this declaration at a test that never runs where it "
        "matters -- the same shape as a structurally permanent [SKIP] being read as coverage. "
        "**REMOVAL TRIGGER: only when the gate reports ZERO deferred controls** -- which means the "
        "`verify` job installing frontend/node_modules and the registration dropping "
        "`--skip-frontend`, at which point the gate returns to [PASS] and the stale-direction check "
        "above retires this entry for us. Until then UPDATE this entry, never delete it: an "
        "undeclared partial is a persistent red with nobody to close it."
    ),
    "check_plan_security_baseline": (
        "1 control defers, and it is structural: `s3_access_log_delivery_grant` on "
        "`modules/eval_artifacts`, whose logging target is a module VARIABLE, so the link from that "
        "source to its destination bucket is not resolvable from the module's own source. The "
        "destination's delivery grant IS asserted; the link is not, and `deferred` is what says so. "
        "Everything else that used to defer here is now asserted: the 2026-09-17 recapture closed the "
        "fixture-absence deferrals (the plan carries the VPC, the private REST API and ALB, and the "
        "managed policy), and the two DynamoDB IAM controls stopped deferring at all once they "
        "read `data.aws_iam_policy_document.statement[*]` -- whose `sid`, `effect`, `actions` and "
        "`condition` stay KNOWN at plan time -- instead of the `aws_iam_role_policy`'s rendered `policy`, "
        "which goes wholly unknown because `resources` concatenates a same-apply table ARN. That was 108 "
        "deferrals; it is now 169 assertions of the action sets plus 71 of the "
        "`table_action_role_matrix.json` disposition. "
        "The gate emits a [DEFER] line per unasserted control and exits 0, which run_gates renders "
        "[PARTIAL] and never [PASS]; the aggregate headline carries the deferred total. A genuine "
        "violation still exits 1. "
        "The coverage for the one deferred link: the DESTINATION bucket's delivery grant is asserted by this "
        "same control against `infra/` source, and whether log objects actually arrive is a T3 assertion "
        "(`scripts/smoke/`), recorded as `skip` with a reason until it runs and never as `pass` -- see "
        "`infra/modules/eval_artifacts/README.md`'s Known limitations. "
        "**REMOVAL TRIGGER: when the gate reports ZERO deferred controls, which needs the cross-module "
        "logging link resolved -- the destination is known only at the root, so a control reading one "
        "module's source cannot reach it.** This entry must be UPDATED, never deleted: an undeclared "
        "partial is a persistent red with nobody to close it, whereas a briefly-stale reason self-heals "
        "at the next touch. The stale-direction check fires if the gate ever reports a clean [PASS], "
        "which is how this exception retires itself."
    ),
}


#: Defers present exactly when the committed plan fixture is stale relative to `infra/`.
DEFERS_CLOSED_BY_REGENERATION: frozenset[str] = frozenset({"plan_freshness"})

#: The control that defers once per counted type the fixture predates or lags on
#: (`FIXTURE_ABSENT_TYPES`, `FIXTURE_LAGGING_COUNTS`).
DEFERS_ON_FIXTURE_ABSENT_TYPES: frozenset[str] = frozenset({"resource_existence_counts"})


def _fixture_is_fresh() -> bool:
    """Whether the committed plan fixture describes the `infra/` the working tree currently has.

    Compares the fixture's `captured_infra_digest` with `plan_fixtures.infra_digest` of the working
    tree; an unanswerable comparison counts as stale.
    """
    document = plan_fixtures.build("compliant")
    provenance = document.get("_fixture_provenance") or {}
    recorded = provenance.get("captured_infra_digest")
    if not isinstance(recorded, str) or not recorded.strip():
        return False
    try:
        return recorded.strip() == plan_fixtures.infra_digest(REPO_ROOT)
    except ValueError:
        return False


def _fixture_absent_counted_types() -> int:
    """How many `FIXTURE_ABSENT_TYPES` entries carry an expected count and are absent from the
    committed fixture, plus the `FIXTURE_LAGGING_COUNTS` entries the fixture still lags on;
    `resource_existence_counts` defers once for each."""
    gate = _baseline_gate_module()
    changes = plan_fixtures.build("compliant")["resource_changes"]
    planned = {r.get("type") for r in changes}
    absent = sum(
        1
        for resource_type in gate.FIXTURE_ABSENT_TYPES
        if resource_type in gate.EXPECTED_RESOURCE_COUNTS and resource_type not in planned
    )
    lagging = sum(
        1
        for resource_type, (count, _reason) in gate.FIXTURE_LAGGING_COUNTS.items()
        if sum(1 for r in changes if r.get("type") == resource_type) == count
        and gate.EXPECTED_RESOURCE_COUNTS[resource_type].fixed != count
    )
    return absent + lagging


def _expected_deferred_by_control() -> dict[str, int]:
    """The structural composition, plus `plan_freshness` when the fixture is stale, plus one
    existence-count defer per counted type the fixture predates."""
    expected = dict(EXPECTED_DEFERRED_BY_CONTROL)
    absent = _fixture_absent_counted_types()
    if absent:
        expected["resource_existence_counts"] = absent
    if not _fixture_is_fresh():
        expected["plan_freshness"] = 1
    return expected


@pytest.fixture(scope="module")
def baseline_gate_report(_invocation_log: Path) -> dict[str, object]:
    """The plan-security gate's own `--json` document, against the committed fixture."""
    _record_invocation(_invocation_log, "check_plan_security_baseline")
    result = proc_run.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_plan_security_baseline.py"),
            "--plan",
            str(plan_fixtures.materialize("compliant")),
            "--json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return document


class TestTheDeferredCountIsPinnedToTheRealGate:
    """The deferred composition is pinned per control against the real gate's output."""

    def test_the_deferred_composition_is_exactly_the_declared_one(
        self, baseline_gate_report: dict[str, object]
    ) -> None:
        observed = baseline_gate_report["deferred_by_control"]
        assert observed == _expected_deferred_by_control(), (
            "the deferred composition moved. This is not a number to update: each entry is a control "
            "that asserts nothing, so a new key means a NEW class of unasserted control and a changed "
            "value means more of an existing one. Re-derive the removal trigger before touching this."
        )

    def test_the_total_agrees_with_the_composition(
        self, baseline_gate_report: dict[str, object]
    ) -> None:
        """The gate's deferred total equals the sum of the expected composition."""
        assert baseline_gate_report["deferred"] == sum(_expected_deferred_by_control().values())

    def test_run_gates_reports_the_same_count_the_gate_does(
        self, run_gates_report: list[dict[str, object]]
    ) -> None:
        """`run_gates` reports the same deferred count as the gate, with a PARTIAL disposition."""
        baseline = next(g for g in run_gates_report if g["name"] == "check_plan_security_baseline")
        assert baseline["deferred_controls"] == sum(_expected_deferred_by_control().values())
        assert baseline["disposition"] == _run_gates_module().DISPOSITION_PARTIAL

    def test_only_the_freshness_defer_tracks_the_fixture_and_the_rest_are_structural(
        self, baseline_gate_report: dict[str, object]
    ) -> None:
        """Only the freshness and fixture-absent defers track the fixture; the rest are
        structural."""
        composition = baseline_gate_report["deferred_by_control"]
        assert isinstance(composition, dict), composition
        observed = set(composition)
        absent = observed & DEFERS_ON_FIXTURE_ABSENT_TYPES
        assert absent == (
            DEFERS_ON_FIXTURE_ABSENT_TYPES if _fixture_absent_counted_types() else frozenset()
        ), ("the fixture-absent defers do not match FIXTURE_ABSENT_TYPES", sorted(absent))
        structural = observed - DEFERS_CLOSED_BY_REGENERATION - DEFERS_ON_FIXTURE_ABSENT_TYPES
        assert structural == set(EXPECTED_DEFERRED_BY_CONTROL), (
            "the structural defers are not the declared ones",
            sorted(structural ^ set(EXPECTED_DEFERRED_BY_CONTROL)),
        )
        tracking = observed & DEFERS_CLOSED_BY_REGENERATION
        if _fixture_is_fresh():
            assert not tracking, (
                f"the fixture is newer than every infra/**/*.tf, so {sorted(tracking)} must not "
                "defer -- a control claiming staleness that the mtimes do not support"
            )
        else:
            assert tracking == DEFERS_CLOSED_BY_REGENERATION, (
                f"the fixture is older than some infra/**/*.tf, so "
                f"{sorted(DEFERS_CLOSED_BY_REGENERATION)} must defer; observed {sorted(tracking)}"
            )

    def test_the_declared_partial_reason_states_the_current_total(self) -> None:
        """The declared partial reason states the pinned total as `<n> control defers`."""
        reason = EXPECTED_OFFLINE_PARTIALS["check_plan_security_baseline"]
        total = sum(EXPECTED_DEFERRED_BY_CONTROL.values())
        assert f"{total} control defers" in reason, (
            f"the declared reason does not state the pinned total ({total}) in the phrase "
            f"'{total} control defers'"
        )


def test_no_gate_fails_against_the_real_repository(
    run_gates_report: list[dict[str, object]],
) -> None:
    """No gate reports a FAIL disposition against the real repository."""
    failures = [
        f"{gate['name']} (exit {gate['returncode']}): "
        f"{gate['deferred_count_problem'] or ''}{gate['stdout']}{gate['stderr']}"
        for gate in run_gates_report
        if gate["disposition"] == _run_gates_module().DISPOSITION_FAIL
    ]
    assert failures == [], "\n".join(failures)


def test_the_set_of_skipping_gates_is_exactly_the_declared_set(
    run_gates_report: list[dict[str, object]],
) -> None:
    """The skipping gates are exactly `EXPECTED_OFFLINE_SKIPS`, with probed entries skipping iff
    their input is absent."""
    skipped = {str(gate["name"]) for gate in run_gates_report if gate["skipped"]}
    passed = {str(gate["name"]) for gate in run_gates_report if gate["passed"]}
    registered = {str(gate["name"]) for gate in run_gates_report}
    declared = set(EXPECTED_OFFLINE_SKIPS)

    undeclared = sorted(skipped - declared)
    assert not undeclared, (
        f"gate(s) {undeclared} reported [SKIP] — verified nothing — without being declared in "
        "EXPECTED_OFFLINE_SKIPS. A gate that quietly starts skipping reads as green while checking "
        "nothing. Either restore its input, or declare it here with the reason and where its real "
        "coverage lives."
    )

    unconditional = declared - set(SKIP_PRECONDITION_PROBES)
    stale = sorted(unconditional & passed)
    assert not stale, (
        f"gate(s) {stale} are declared as expected offline skips but actually passed. The "
        "environmental precondition changed; remove the entry rather than leaving a gate exempted "
        "that no longer needs it."
    )

    absent = sorted(unconditional - (skipped | passed))
    assert not absent, (
        f"gate(s) {absent} are declared as expected skips but are not registered in run_gates.py "
        "at all; the entry outlived its gate"
    )

    for name, probe in sorted(SKIP_PRECONDITION_PROBES.items()):
        assert name in registered, (
            f"{name} carries a skip precondition but is not registered in run_gates.py at all; the "
            "entry outlived its gate"
        )
        why_absent = probe()
        if why_absent is None:
            assert name not in skipped, (
                f"{name}'s input IS available here, so it must not report [SKIP]. Its declared skip is "
                "conditional on an absent input; a skip with the input present means the gate stopped "
                f"checking for some OTHER reason, which this entry does not cover and must not exempt. "
                f"Reason it printed: {_gate_stdout(run_gates_report, name)!r}"
            )
        else:
            assert name in skipped, (
                f"{name}'s input is absent here ({why_absent}), so it must report [SKIP] rather than a "
                "disposition claiming coverage. Either the gate stopped keying on that input, or this "
                "probe no longer asks the question the gate asks itself."
            )


def _gate_stdout(report: list[dict[str, object]], name: str) -> str:
    return str(next((g["stdout"] for g in report if g["name"] == name), ""))[-400:]


def test_every_skip_precondition_probe_belongs_to_a_declared_skip() -> None:
    """Every `SKIP_PRECONDITION_PROBES` key is declared in `EXPECTED_OFFLINE_SKIPS`."""
    orphans = sorted(set(SKIP_PRECONDITION_PROBES) - set(EXPECTED_OFFLINE_SKIPS))
    assert not orphans, (
        f"{orphans} carry a skip precondition probe but are not declared in EXPECTED_OFFLINE_SKIPS"
    )


def test_the_set_of_partial_gates_is_exactly_the_declared_set(
    run_gates_report: list[dict[str, object]],
) -> None:
    """The `[PARTIAL]` gates are exactly `EXPECTED_OFFLINE_PARTIALS`, in both directions."""
    run_gates = _run_gates_module()
    partial = {
        str(gate["name"])
        for gate in run_gates_report
        if gate["disposition"] == run_gates.DISPOSITION_PARTIAL
    }
    clean = {
        str(gate["name"])
        for gate in run_gates_report
        if gate["disposition"] == run_gates.DISPOSITION_PASS
    }
    declared = set(EXPECTED_OFFLINE_PARTIALS)

    undeclared = sorted(partial - declared)
    assert not undeclared, (
        f"gate(s) {undeclared} reported [PARTIAL] — some controls not asserted — without being "
        "declared in EXPECTED_OFFLINE_PARTIALS. Either restore what they could not assert, or "
        "declare it here with the reason and where those controls are covered."
    )

    stale = sorted(declared & clean)
    assert not stale, (
        f"gate(s) {stale} are declared as expected offline partials but reported a clean [PASS]. "
        "Their controls are all asserted now; remove the entry rather than leaving a gate declared "
        "as incomplete when it is not."
    )

    absent = sorted(declared - (partial | clean))
    assert not absent, (
        f"gate(s) {absent} are declared as expected partials but neither deferred nor passed "
        "(failed, skipped, or unregistered); the entry no longer describes the gate"
    )


def test_every_declared_partial_carries_a_reason_and_a_removal_trigger() -> None:
    """Every declared partial names its coverage and states a `REMOVAL TRIGGER`."""
    for name, reason in EXPECTED_OFFLINE_PARTIALS.items():
        assert len(reason) > 30, (
            f"{name}'s declared partial reason is too thin to act on: {reason!r}"
        )
        assert "coverage" in reason, (
            f"{name}'s reason should say where the deferred controls are actually covered: {reason!r}"
        )
        assert "REMOVAL TRIGGER" in reason, (
            f"{name}'s entry states no removal trigger, so nothing tells the next reader what must "
            "be true before deleting it -- and an entry with no stated retirement condition is "
            "either deleted too early or kept forever"
        )


def test_every_declared_skip_carries_a_reason() -> None:
    """Every declared skip carries a reason naming where the property is covered."""
    for name, reason in EXPECTED_OFFLINE_SKIPS.items():
        assert len(reason) > 30, f"{name}'s declared skip reason is too thin to act on: {reason!r}"
        assert "coverage" in reason, (
            f"{name}'s reason should say where the property is actually covered, since it is not "
            f"covered here: {reason!r}"
        )


def test_run_gates_exits_zero_when_every_gate_passes_or_skips(
    run_gates_json_run: RunGatesRun,
) -> None:
    """`run_gates.py` exits 0 when every gate passes, defers or skips."""
    assert run_gates_json_run.returncode == 0, run_gates_json_run.stdout + run_gates_json_run.stderr


def _synthetic_result(module: Any, name: str, disposition: str) -> dict[str, object]:
    """A `_run_gate`-shaped result with the given `disposition` and no subprocess behind it."""
    returncode = {
        module.DISPOSITION_PASS: 0,
        module.DISPOSITION_SKIP: module.GATE_SKIP_EXIT_STATUS,
        module.DISPOSITION_FAIL: 1,
    }[disposition]
    return {
        "name": name,
        "argv": ["synthetic", name],
        "returncode": returncode,
        "stdout": f"{name} synthetic output\n",
        "stderr": "",
        "skipped": disposition == module.DISPOSITION_SKIP,
        "passed": returncode == 0,
        "deferred_controls": 0,
        "rendered_deferred_controls": 0,
        "deferred_count_problem": None,
        "disposition": disposition,
    }


@pytest.mark.parametrize(
    ("dispositions", "expected_status"),
    [(("PASS", "PASS"), 0), (("PASS", "SKIP"), 0), (("PASS", "SKIP", "FAIL"), 1)],
    ids=["all_pass", "one_skip", "one_fail"],
)
def test_run_gates_returns_the_same_status_in_every_output_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dispositions: tuple[str, ...],
    expected_status: int,
) -> None:
    """`main` derives its exit status from the results alone, whichever output mode is chosen."""
    module = _run_gates_module()
    results = [
        _synthetic_result(module, f"gate_{index}", disposition)
        for index, disposition in enumerate(dispositions)
    ]
    monkeypatch.setattr(module, "run_all_gates", lambda: [dict(r) for r in results])
    statuses = {}
    for mode in ([], ["--json"], ["--census"]):
        statuses[" ".join(mode) or "summary"] = module.main(mode)
        assert capsys.readouterr().out, f"main({mode}) printed nothing"
    assert statuses == dict.fromkeys(("summary", "--json", "--census"), expected_status)


def test_a_skipping_gate_is_never_reported_as_passed(
    run_gates_report: list[dict[str, object]],
) -> None:
    """A gate exiting 3 is reported `skipped` and never `passed`."""
    for gate in run_gates_report:
        if gate["skipped"]:
            assert gate["passed"] is False, f"{gate['name']} is both skipped and passed"
            assert gate["returncode"] == 3


def test_make_verify_collects_two_simultaneous_failures(tmp_path: Path) -> None:
    """`$(MAKE) -k` in a scratch `Makefile` runs two failing sub-targets and exits non-zero."""
    scratch = tmp_path / "make_scratch"
    scratch.mkdir()
    shutil.copy(MAKEFILE_PATH, scratch / "Makefile")
    makefile_path = scratch / "Makefile"
    with makefile_path.open("a") as handle:
        handle.write("\n.PHONY: probe-a probe-b probe-verify\n")
        handle.write("probe-a:\n\techo PROBE_A_RAN && exit 1\n")
        handle.write("probe-b:\n\techo PROBE_B_RAN && exit 1\n")
        handle.write("probe-verify:\n\t$(MAKE) -k probe-a probe-b\n")

    result = proc_run.run(
        ["make", "-C", str(scratch), "probe-verify"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "PROBE_A_RAN" in result.stdout
    assert "PROBE_B_RAN" in result.stdout
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# 2. No gate in the `make verify` set requires an AWS credential.
# ---------------------------------------------------------------------------

#: Patterns that indicate a script is reaching for a real AWS credential/session rather than
#: operating on local fixtures, `moto`, or a local-endpoint-only client.
_CREDENTIAL_REACHING_PATTERNS = (
    "sts.get_caller_identity",
    "get_caller_identity(",
    "boto3.Session(",
    "botocore.session.Session(",
)


def _gate_script_paths() -> list[Path]:
    """Every gate script `scripts/run_gates.py` invokes, resolved to real paths on disk."""
    run_gates = _run_gates_module()
    paths: list[Path] = []
    for _name, argv in run_gates.GATES:
        for token in argv:
            candidate = REPO_ROOT / token
            if candidate.suffix in (".py", ".sh") and candidate.exists():
                paths.append(candidate)
    return paths


def test_no_gate_in_the_verify_set_reaches_for_an_aws_credential() -> None:
    """Every gate script `run_gates.py` runs is free of credential-resolving calls in its source."""
    offending: list[str] = []
    for path in _gate_script_paths():
        text = path.read_text(encoding="utf-8")
        for pattern in _CREDENTIAL_REACHING_PATTERNS:
            if pattern in text:
                offending.append(f"{path.relative_to(REPO_ROOT)}: contains {pattern!r}")
    assert not offending, "gate(s) reach for an AWS credential:\n" + "\n".join(offending)


#: Test module directories `make verify` runs pytest against.
_VERIFY_TEST_DIRS = ("tests/unit", "tests/stub", "tests/integration")

#: A reference to the deploy/teardown entry points or the real `aws`/`terraform` binaries —
#: either the literal script filename or the module-level path constants
#: (`DEPLOY_SH`/`TEARDOWN_SH`) this test suite conventionally names them with.
_SCRIPT_REFERENCE_PATTERN = re.compile(r"""\bDEPLOY_SH\b|\bTEARDOWN_SH\b|deploy\.sh|teardown\.sh""")

#: `proc_run.run(...)`/`proc_run.popen(...)` and `subprocess.<call>(...)` call sites, captured whole
#: (across newlines) so `cwd=`/`env=`/`PATH` arguments inside the same call can be inspected together.
_SUBPROCESS_CALL_PATTERN = re.compile(
    r"""(?:proc_run\.(?:run|popen)|subprocess\.(?:run|Popen|call|check_call|check_output))"""
    r"""\s*\((?:[^()]|\([^()]*\))*\)""",
    re.DOTALL,
)

#: Substrings that, when present inside a matched subprocess call, mark it as running against a
#: sandbox with a restricted/stub `PATH` and/or a non-repo-root `cwd` — the only sanctioned pattern
#: for exercising these scripts from a test. A call naming a script/binary reference without any of
#: these markers is treated as reaching for the real script and the real host `PATH`.
_SANDBOX_MARKERS = (
    "bin_dir",
    "sandbox",
    "_minimal_path",
    "tmp_path",
    "scratch",
)


#: A `cwd=` argument naming anything other than the repository root; such a call is exempt. A call
#: with no `cwd=` inherits pytest's cwd (the repo root) and is not exempt.
_NON_REPO_CWD_PATTERN = re.compile(r"cwd\s*=\s*(?!REPO_ROOT\b)([A-Za-z_][\w.\[\]\"'/ ]*)")


def _real_script_or_cli_invocations(text: str) -> list[str]:
    """Returns each subprocess call in `text` that references a deploy/teardown script or the
    `aws`/`terraform` names and shows none of the sandboxing markers — i.e. every call that looks
    like it runs the real script against the real environment rather than a stub-binary sandbox."""
    offending: list[str] = []
    for call in _SUBPROCESS_CALL_PATTERN.findall(text):
        names_a_script_or_cli = _SCRIPT_REFERENCE_PATTERN.search(call) or re.search(
            r"""(?<!\w)["']aws["']|(?<!\w)["']terraform["']""", call
        )
        if not names_a_script_or_cli:
            continue
        if any(marker in call for marker in _SANDBOX_MARKERS):
            continue
        if _NON_REPO_CWD_PATTERN.search(call):
            continue
        offending.append(call.splitlines()[0][:120])
    return offending


def _test_module_paths() -> list[Path]:
    """Every `test_*.py` file under the directories `make verify` runs pytest against."""
    paths: list[Path] = []
    for rel_dir in _VERIFY_TEST_DIRS:
        directory = REPO_ROOT / rel_dir
        if not directory.is_dir():
            continue
        paths.extend(sorted(directory.rglob("test_*.py")))
    return paths


def test_a_non_repo_cwd_exempts_and_a_repo_root_cwd_does_not() -> None:
    """A non-repository `cwd` exempts a call; `cwd=REPO_ROOT` or no `cwd` does not."""
    # CLI tokens are assembled at runtime so this file's own text is not a scanner match.
    tf = "terra" + "form"
    cli = "a" + "ws"
    call = "subprocess.run(["

    sandboxed = f'{call}"{tf}", "plan"], cwd=work_dir, check=False)'
    assert _real_script_or_cli_invocations(sandboxed) == [], (
        "a terraform call directed at a non-repository directory cannot be running this repo's real "
        "deploy path, whatever else it is doing"
    )

    at_repo_root = f'{call}"{tf}", "apply"], cwd=REPO_ROOT, check=False)'
    assert _real_script_or_cli_invocations(at_repo_root), (
        "the exact pattern this guard was written for is no longer detected"
    )

    no_cwd = f'{call}"{cli}", "sts", "get-caller-identity"], check=False)'
    assert _real_script_or_cli_invocations(no_cwd), (
        "a call with no cwd inherits pytest's, which IS the repo root -- omitting cwd must not exempt"
    )


def test_no_test_module_in_the_verify_set_invokes_the_real_deploy_teardown_or_cli() -> None:
    """No test module `make verify` runs invokes deploy/teardown scripts, `aws` or `terraform`
    without a sandbox marker or a non-repository `cwd`."""
    offending: list[str] = []
    for path in _test_module_paths():
        text = path.read_text(encoding="utf-8")
        hits = _real_script_or_cli_invocations(text)
        if hits:
            offending.append(f"{path.relative_to(REPO_ROOT)}: {hits}")
    assert not offending, (
        "test module(s) appear to invoke the real deploy.sh/teardown.sh/aws/terraform without a "
        "sandbox:\n" + "\n".join(offending)
    )


def test_check_reintroduces_and_detects_a_real_deploy_sh_invocation() -> None:
    """The detection helper flags an unsandboxed deploy-script call assembled at runtime."""
    script_var = "DEPLOY" + "_SH"
    reintroduced_call_site = (
        "result = subprocess.run(\n"
        f"    ['bash', str({script_var}), '--profile', 'x'],\n"
        "    cwd=REPO_ROOT,\n"
        "    capture_output=True,\n"
        "    text=True,\n"
        "    env=env,\n"
        "    timeout=30,\n"
        ")\n"
    )
    hits = _real_script_or_cli_invocations(reintroduced_call_site)
    assert hits, "detection helper failed to flag a real, unsandboxed deploy.sh invocation"


@pytest.mark.parametrize("callee", ["proc_run.run", "proc_run.popen"])
def test_check_detects_an_unsandboxed_cli_call_through_the_proc_run_funnel(callee: str) -> None:
    """A funnelled call naming the real CLI with no sandbox marker is flagged like a bare one."""
    cli = "a" + "ws"
    call_site = f'{callee}(["{cli}", "sts", "get-caller-identity"], check=False)'
    assert _real_script_or_cli_invocations(call_site)


def test_verify_gates_target_recipe_contains_no_aws_cli_or_terraform_apply() -> None:
    """The `verify-gates` and `verify-shellcheck` Makefile recipes invoke no AWS CLI call and no
    `terraform apply`/`plan` against a live account — only local scripts and fixtures."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    targets = _parse_make_targets(makefile_text)
    for target_name in (
        "verify-gates",
        "verify-shellcheck",
        "lint",
        "format-check",
        "typecheck",
        "test",
        "test-stub",
        "test-integration",
        "check-cost-withheld",
    ):
        recipe = _recipe_text_for(targets, target_name)
        assert "terraform apply" not in recipe
        assert "terraform plan" not in recipe
        assert not re.search(r"\baws\s+(?!--)", recipe), (
            f"target {target_name!r} appears to invoke the AWS CLI directly: {recipe!r}"
        )


def test_pytest_tests_integration_uses_dynamodb_local_not_a_real_aws_endpoint() -> None:
    """The `tests/integration` conftest requires DynamoDB Local and reaches for no credential."""
    conftest_text = (REPO_ROOT / "tests" / "integration" / "conftest.py").read_text(
        encoding="utf-8"
    )
    assert "CRM_REQUIRE_DYNAMODB_LOCAL" in conftest_text
    for pattern in _CREDENTIAL_REACHING_PATTERNS:
        assert pattern not in conftest_text


# ---------------------------------------------------------------------------
# 3. `make verify-deploy` exists, delegates to Layers 4-6, and is not a `verify` prerequisite.
# ---------------------------------------------------------------------------


def test_verify_deploy_target_exists_and_is_not_a_prerequisite_of_verify() -> None:
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    targets = _parse_make_targets(makefile_text)

    assert "verify-deploy" in targets, "Makefile has no `verify-deploy` target"
    verify_deploy_recipe = _recipe_text_for(targets, "verify-deploy")
    assert "scripts/verify_deployment.sh" in verify_deploy_recipe

    assert "verify" in targets, "Makefile has no `verify` target"
    verify_recipe = _recipe_text_for(targets, "verify")
    sub_target_names = _referenced_sub_target_names(verify_recipe)
    assert "verify-deploy" not in sub_target_names, (
        "`verify-deploy` (Layers 4-6, requires AWS credentials and cost) must not be a "
        "prerequisite of `verify` (Layers 1-3, offline)"
    )
    assert "verify-deploy" not in verify_recipe


def test_verify_gate_set_includes_integration_tests_and_plan_security_baseline() -> None:
    """`make verify`'s sub-targets reach `pytest tests/integration` and `run_gates.py`, which runs
    `check_plan_security_baseline.py` against the committed compliant fixture."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    targets = _parse_make_targets(makefile_text)
    verify_recipe = _recipe_text_for(targets, "verify")
    sub_target_names = _referenced_sub_target_names(verify_recipe)

    integration_recipe = _recipe_text_for(targets, "test-integration")
    assert "test-integration" in sub_target_names
    assert re.search(r"pytest\b[^\n]*\btests/integration\b", integration_recipe)

    assert "verify-gates" in sub_target_names
    verify_gates_recipe = _recipe_text_for(targets, "verify-gates")
    assert "run_gates.py" in verify_gates_recipe

    run_gates_text = RUN_GATES.read_text(encoding="utf-8")
    assert "check_plan_security_baseline.py" in run_gates_text
    run_gates = _run_gates_module()
    assert dict(run_gates.GATES)["check_plan_security_baseline"][2:] == [
        "--plan",
        str(run_gates.COMPLIANT_PLAN_FIXTURE.relative_to(REPO_ROOT)),
    ]
    assert run_gates.COMPLIANT_PLAN_FIXTURE == plan_fixtures.DEFAULT_OUT_DIR / "compliant.json"
    assert "_materialize_plan_fixture()" in run_gates_text
    assert "check_log_redaction.py" in run_gates_text


# ---------------------------------------------------------------------------
# 4. The expensive subprocesses above are spawned once per suite, not once per worker.
# ---------------------------------------------------------------------------


def _invocations_by_process(lines: list[str]) -> dict[tuple[str, str], Counter[str]]:
    """`{(worker, pid): Counter(label)}` from the invocation log's `label\tworker\tpid` lines."""
    by_process: dict[tuple[str, str], Counter[str]] = {}
    for line in lines:
        label, worker, pid = line.split("\t")
        by_process.setdefault((worker, pid), Counter())[label] += 1
    return by_process


def _sharing_violations(lines: list[str], own: tuple[str, str]) -> list[str]:
    """Why the log shows an expensive subprocess re-run inside one process, or missing from ours.

    Module-scoped fixtures run once per process: serially that is once per run, and under xdist once
    per worker that the module's tests land on. So the property is per process.
    """
    by_process = _invocations_by_process(lines)
    problems = [
        f"{process} ran {label!r} {count} times"
        for process, counts in sorted(by_process.items())
        for label, count in sorted(counts.items())
        if count > 1 or label not in _EXPECTED_INVOCATIONS
    ]
    if dict(by_process.get(own, Counter())) != _EXPECTED_INVOCATIONS:
        problems.append(
            f"this process {own} recorded {dict(by_process.get(own, Counter()))}, "
            f"expected {_EXPECTED_INVOCATIONS}"
        )
    return problems


def test_the_module_runs_each_expensive_subprocess_exactly_once(
    _invocation_log: Path,
    run_gates_report: list[dict[str, object]],
    baseline_gate_report: dict[str, object],
) -> None:
    """Each expensive subprocess runs once per process, however xdist distributes the module."""
    lines = _invocation_log.read_text(encoding="utf-8").splitlines()
    own = (os.environ.get("PYTEST_XDIST_WORKER", "serial"), str(os.getpid()))
    problems = _sharing_violations(lines, own)
    assert not problems, (
        "the expensive subprocesses were not shared across this module's tests:\n"
        + "\n".join(problems)
        + "\nlog:\n"
        + "\n".join(lines)
    )


def test_the_sharing_check_discriminates() -> None:
    """Control: a per-test re-run inside one process, or a missing label, is reported."""
    own = ("gw0", "100")
    shared = [f"{label}\tgw0\t100" for label in _EXPECTED_INVOCATIONS] + [
        f"{label}\tgw1\t101" for label in _EXPECTED_INVOCATIONS
    ]
    assert _sharing_violations(shared, own) == []
    rerun = [*shared, "run_gates --json\tgw0\t100"]
    assert any("2 times" in p for p in _sharing_violations(rerun, own))
    other_rerun = [*shared, "run_gates --json\tgw1\t101"]
    assert any("('gw1', '101')" in p for p in _sharing_violations(other_rerun, own))
    missing = [line for line in shared if not line.startswith("run_gates --json\tgw0")]
    assert any("this process" in p for p in _sharing_violations(missing, own))


def test_a_gate_report_carrying_an_invalid_utf8_byte_is_rendered_and_not_crashed(
    tmp_path: Path,
) -> None:
    """`_run_gate` renders a FAIL, with undecodable bytes replaced, for a non-UTF-8 gate report.

    The `pytest.raises` control shows a strict-decode capture of the same output raises.
    """
    module = _run_gates_module()
    # Assembled at runtime: a literal security-group id in tracked text fails `check_no_account_id`.
    synthetic_sg_id = "sg-" + "0a1b2c3d"
    emitter = tmp_path / "badbyte_gate.py"
    emitter.write_text(
        "import os, sys\n"
        f'os.write(2, b"VIOLATION: {synthetic_sg_id} "'
        ' + bytes([0xE9, 0xE8]) + b" trailing\\n")\n'
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    argv = [sys.executable, str(emitter)]

    with pytest.raises(UnicodeDecodeError):
        proc_run.run(argv, capture_output=True, text=True, check=False)

    result = module._run_gate("badbyte", argv)

    assert result["returncode"] == 1, result
    assert result["disposition"] == module.DISPOSITION_FAIL, (
        "a gate exiting non-zero must render as a failure even when its report is undecodable; "
        f"got {result['disposition']!r}"
    )
    stderr = str(result["stderr"])
    assert synthetic_sg_id in stderr, (
        "the decodable part of the report must survive, so the operator still learns which "
        f"pattern matched; got {stderr!r}"
    )
    assert "�" in stderr, (
        f"the undecodable bytes must be replaced rather than dropped or raised on; got {stderr!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_a_failed_materialize_removes_the_earlier_plan_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale `compliant.json` is never left for the baseline gate when the build fails."""
    run_gates = _run_gates_module()
    stale = tmp_path / "compliant.json"
    stale.write_text("{}", encoding="utf-8")

    def broken(name: str, out_dir: Path | None = None) -> Path:
        raise ValueError(f"cannot build {name}")

    monkeypatch.setattr(run_gates, "COMPLIANT_PLAN_FIXTURE", stale)
    monkeypatch.setattr(run_gates.plan_fixtures, "materialize", broken)
    run_gates._materialize_plan_fixture()
    assert not stale.exists()
    assert "cannot build compliant" in capsys.readouterr().err


def test_materialize_writes_the_plan_the_gate_is_pointed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_gates = _run_gates_module()
    target = tmp_path / "compliant.json"
    monkeypatch.setattr(run_gates, "COMPLIANT_PLAN_FIXTURE", target)
    run_gates._materialize_plan_fixture()
    assert json.loads(target.read_text(encoding="utf-8")) == plan_fixtures.build("compliant")
