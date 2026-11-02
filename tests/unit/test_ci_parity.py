# SPDX-License-Identifier: MIT
"""Prove CI and local offline verification stay in parity.

Parses the real `.github/workflows/*.yml`, `.gitlab-ci.yml`, `Makefile` and `scripts/run_gates.py`
and derives each expected set from those files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_PATH = WORKFLOWS_DIR / "verify.yml"
BROWSER_WORKFLOW_PATH = WORKFLOWS_DIR / "browser-journeys.yml"
MAKEFILE_PATH = REPO_ROOT / "Makefile"
RUN_GATES_PATH = REPO_ROOT / "scripts" / "run_gates.py"
PYTHON_VERSION_PATH = REPO_ROOT / ".python-version"
LOCKFILE_PATH = REPO_ROOT / "requirements.txt"

#: Pytest suite directories a Makefile recipe's pytest invocation may name.
_KNOWN_PYTEST_DIRS = ("tests/unit", "tests/stub", "tests/integration")


def _load_workflow(text: str) -> dict[str, Any]:
    """Parse workflow YAML. `on:` parses as the boolean key `True` in PyYAML; ignore it."""
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def _all_workflow_paths() -> list[Path]:
    """Return every workflow file under `.github/workflows`."""
    return sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))


def _browser_workflow() -> dict[str, Any]:
    return _load_workflow(BROWSER_WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the workflow's `on:` block, which PyYAML parses under the key `True`."""
    # The key is the boolean `True`, which a `dict[str, Any]` annotation cannot express.
    keyed: dict[Any, Any] = workflow
    raw: Any = keyed[True] if True in keyed else keyed.get("on")
    assert isinstance(raw, dict), f"unexpected trigger shape: {raw!r}"
    return raw


def _browser_job() -> dict[str, Any]:
    jobs = _browser_workflow().get("jobs", {})
    assert len(jobs) == 1, f"expected exactly one job in the browser workflow, got {sorted(jobs)}"
    job = next(iter(jobs.values()))
    assert isinstance(job, dict)
    return job


def _browser_steps() -> list[dict[str, Any]]:
    steps = _browser_job().get("steps", [])
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _makefile_recipes() -> dict[str, list[str]]:
    """Parse the real Makefile into {target: [recipe lines]}, skipping comments/.PHONY."""
    text = MAKEFILE_PATH.read_text(encoding="utf-8")
    recipes: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        if raw_line.startswith("#") or not raw_line.strip():
            continue
        if raw_line.startswith("\t"):
            if current is not None:
                recipes[current].append(raw_line.strip())
            continue
        # A target header line, e.g. "verify: export FOO = 1" or "test:" or
        # "verify-gates:". Ignore .PHONY and variable-only lines with no colon.
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:", raw_line)
        if match is None:
            continue
        name = match.group(1)
        if name == ".PHONY":
            continue
        current = name
        recipes.setdefault(current, [])
    return recipes


def _derive_local_gate_set() -> set[str]:
    """Derive the set of offline gates `make verify` runs, by parsing the Makefile.

    Each sub-target of `verify`'s `$(MAKE) -k` line becomes `"pytest:<dir>"` when its recipe runs
    pytest on a known directory, else the bare target name; `verify-gates` expands to the leaf
    gates in `run_gates.py`.
    """
    recipes = _makefile_recipes()
    verify_recipe = recipes.get("verify")
    assert verify_recipe, "Makefile has no 'verify' target — cannot derive local gate set"

    make_line = next((line for line in verify_recipe if line.startswith("$(MAKE)")), None)
    assert make_line is not None, f"'verify' recipe has no $(MAKE) sub-target line: {verify_recipe}"

    # "$(MAKE) -k lint format-check typecheck test test-stub test-integration ..."
    tokens = make_line.split()
    sub_targets = [tok for tok in tokens[1:] if not tok.startswith("-")]
    assert sub_targets, f"could not parse sub-targets from: {make_line!r}"

    gates: set[str] = set()
    for sub in sub_targets:
        sub_recipe = recipes.get(sub, [])
        pytest_line = next((line for line in sub_recipe if "pytest" in line), None)
        if pytest_line is not None:
            matched_dir = next(
                (d for d in _KNOWN_PYTEST_DIRS if d in pytest_line),
                None,
            )
            assert matched_dir is not None, (
                f"target {sub!r} runs pytest but names none of {_KNOWN_PYTEST_DIRS}: "
                f"{pytest_line!r}"
            )
            gates.add(f"pytest:{matched_dir}")
        else:
            gates.add(sub)

    # Expand verify-gates into run_gates.py's leaf gates.
    if "verify-gates" in gates:
        gates.discard("verify-gates")
        gates.update(f"run_gates:{name}" for name in _run_gates_leaf_names())

    return gates


def _run_gates_leaf_names() -> list[str]:
    """Return the `name` of each `(name, argv)` tuple in run_gates.py's GATES list."""
    text = RUN_GATES_PATH.read_text(encoding="utf-8")
    return re.findall(r'\(\s*"([a-z_]+)"\s*,\s*\[', text)


def _derive_ci_gate_set(workflow: dict[str, Any]) -> set[str]:
    """Return the local gate set if some workflow step runs `make verify`, else raise."""
    jobs = workflow.get("jobs", {})
    assert jobs, "workflow has no jobs"
    for job in jobs.values():
        steps = job.get("steps", [])
        for step in steps:
            run_text = step.get("run", "")
            if re.search(r"\bmake\s+verify\b", run_text):
                return _derive_local_gate_set()
    raise AssertionError("no step in the workflow runs `make verify`")


def test_ci_gate_set_matches_local_gate_set_derived_from_the_makefile() -> None:
    """CI's effective offline gate set equals the gate set `make verify` runs locally."""
    local_gates = _derive_local_gate_set()
    workflow = _load_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))
    ci_gates = _derive_ci_gate_set(workflow)

    assert local_gates, "derived local gate set is empty — parsing regressed"
    assert local_gates == ci_gates


def test_non_vacuity_a_gate_missing_from_ci_is_detected() -> None:
    """A workflow that drops `make verify` for a partial step list fails the parity check."""
    local_gates = _derive_local_gate_set()
    assert local_gates, "derived local gate set is empty — parsing regressed"

    # A partial CI mirror that runs only the unit tests.
    mutated_workflow_yaml = """
jobs:
  verify:
    runs-on: ubuntu-24.04
    steps:
      - name: Checkout
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
      - name: Run only unit tests
        run: .venv/bin/python -m pytest tests/unit -q
"""
    mutated_workflow = _load_workflow(mutated_workflow_yaml)

    try:
        mutated_ci_gates = _derive_ci_gate_set(mutated_workflow)
    except AssertionError as exc:
        # `_derive_ci_gate_set` raises when no step runs `make verify`.
        assert "make verify" in str(exc)
        return

    assert mutated_ci_gates != local_gates, (
        "expected the mutated (gate-dropping) workflow to disagree with the real "
        "local gate set, but the comparison passed — the parity check would be vacuous"
    )


#: Make targets whose pytest run is distributed over ``$(PYTEST_WORKERS)`` xdist workers, with the
#: ``--dist`` mode each must use.
_DISTRIBUTED_PYTEST_TARGETS = {"test-stub": "loadfile", "test-integration": "loadfile"}


def _pytest_recipe_tokens(target: str) -> list[str]:
    """Return the whitespace-split tokens of ``target``'s single pytest recipe line."""
    lines = [line for line in _makefile_recipes().get(target, []) if "pytest" in line]
    assert len(lines) == 1, f"{target!r} should have exactly one pytest recipe line, got {lines}"
    return lines[0].split()


def _distribution_problems(tokens: list[str], dist_mode: str) -> list[str]:
    """Return why a pytest token list is not distributed over ``$(PYTEST_WORKERS)`` in ``dist_mode``."""
    problems: list[str] = []
    pairs = list(zip(tokens, tokens[1:], strict=False))
    if ("-n", "$(PYTEST_WORKERS)") not in pairs:
        problems.append("no `-n $(PYTEST_WORKERS)`")
    if ("--dist", dist_mode) not in pairs:
        problems.append(f"no `--dist {dist_mode}`")
    return problems


@pytest.mark.parametrize(("target", "dist_mode"), sorted(_DISTRIBUTED_PYTEST_TARGETS.items()))
def test_the_dynamodb_local_suites_run_distributed_over_the_worker_count(
    target: str, dist_mode: str
) -> None:
    """The stub and integration recipes take ``-n $(PYTEST_WORKERS)`` and their ``--dist`` mode."""
    assert _distribution_problems(_pytest_recipe_tokens(target), dist_mode) == []


def test_non_vacuity_a_serial_recipe_is_reported_as_undistributed() -> None:
    """The bare pytest recipe each target had before distribution is reported on both counts."""
    serial = [".venv/bin/python", "-m", "pytest", "tests/integration"]
    assert _distribution_problems(serial, "loadfile") == [
        "no `-n $(PYTEST_WORKERS)`",
        "no `--dist loadfile`",
    ]


def test_ci_interpreter_pin_matches_the_local_pin_file() -> None:
    """CI's `actions/setup-python` version input equals the real `.python-version`."""
    pinned_version = PYTHON_VERSION_PATH.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned_version), (
        f".python-version does not contain a full X.Y.Z pin: {pinned_version!r}"
    )

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = _load_workflow(workflow_text)

    # The workflow reads .python-version into a step output that setup-python consumes.
    assert ".python-version" in workflow_text, (
        "workflow does not appear to read .python-version anywhere"
    )

    jobs = workflow.get("jobs", {})
    setup_python_step = None
    for job in jobs.values():
        for step in job.get("steps", []):
            if "setup-python" in step.get("uses", ""):
                setup_python_step = step
                break
    assert setup_python_step is not None, "no actions/setup-python step found"

    version_input = setup_python_step.get("with", {}).get("python-version", "")
    assert "steps.pin.outputs.version" in version_input, (
        f"setup-python's python-version input does not reference the pin-reading "
        f"step's output: {version_input!r}"
    )

    pin_step = None
    for job in jobs.values():
        for step in job.get("steps", []):
            if step.get("id") == "pin":
                pin_step = step
                break
    assert pin_step is not None, "no step with id 'pin' found"
    assert ".python-version" in pin_step.get("run", ""), (
        "the 'pin' step does not read .python-version"
    )


def test_lock_file_is_installed_as_the_sole_authority_with_no_deps() -> None:
    """CI installs requirements.txt with `--no-deps` and makes no unpinned install."""
    assert LOCKFILE_PATH.exists(), "requirements.txt (the lock file) is missing"

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = _load_workflow(workflow_text)

    install_lines = [
        line
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        for line in step.get("run", "").splitlines()
        if "requirements.txt" in line and "pip install" in line
    ]
    assert install_lines, "no step installs from requirements.txt"

    for line in install_lines:
        assert "--no-deps" in line, (
            f"install line does not pin --no-deps (sole-install-authority violation): {line!r}"
        )
        # Reject a direct pip install with no -r/-e/--no-deps flag.
        assert not re.search(r"pip install(?!.*(-r |--no-deps|-e ))", line), (
            f"install line looks like an unpinned direct install: {line!r}"
        )


def test_workflow_requires_no_aws_credential() -> None:
    """The verify workflow references no AWS credential, OIDC role or secret."""
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "configure-aws-credentials" not in workflow_text
    assert "aws-actions" not in workflow_text
    assert "role-to-assume" not in workflow_text
    assert "id-token" not in workflow_text  # OIDC token permission scope

    # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY may appear only as DynamoDB Local placeholders.
    assert "AWS_ROLE_ARN" not in workflow_text
    assert "secrets.AWS" not in workflow_text
    assert "secrets." not in workflow_text, (
        "workflow references a GitHub secret; this pipeline must stay credential-free"
    )

    workflow = _load_workflow(workflow_text)
    for job in workflow.get("jobs", {}).values():
        env = job.get("env", {})
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            if key in env:
                assert env[key] in ("local", ""), (
                    f"{key} is not a fixed local placeholder: {env[key]!r}"
                )


def test_permissions_are_read_only() -> None:
    """Every workflow's top-level `permissions` block is exactly `contents: read`."""
    paths = _all_workflow_paths()
    assert paths, f"no workflow files found under {WORKFLOWS_DIR}"
    for path in paths:
        workflow = _load_workflow(path.read_text(encoding="utf-8"))
        permissions = workflow.get("permissions")
        assert permissions == {"contents": "read"}, (
            f"{path.name}: expected permissions: {{contents: read}} only, got {permissions!r}"
        )


def test_every_github_action_is_pinned_to_an_exact_commit_sha_or_tag() -> None:
    """Every `uses:` reference is pinned to a full commit SHA or an exact version tag."""
    paths = _all_workflow_paths()
    assert paths, f"no workflow files found under {WORKFLOWS_DIR}"
    uses_refs = [
        step["uses"]
        for path in paths
        for job in _load_workflow(path.read_text(encoding="utf-8")).get("jobs", {}).values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    assert uses_refs, "no 'uses:' action references found"

    floating_ref_pattern = re.compile(r"@(main|master|latest|v\d+)$")
    for ref in uses_refs:
        assert "@" in ref, f"action reference has no pin at all: {ref!r}"
        _, _, pin = ref.rpartition("@")
        assert not floating_ref_pattern.search(ref), (
            f"action reference uses a floating tag: {ref!r}"
        )
        is_full_sha = re.fullmatch(r"[0-9a-f]{40}", pin) is not None
        is_exact_tag = re.fullmatch(r"v?\d+\.\d+\.\d+", pin) is not None
        assert is_full_sha or is_exact_tag, (
            f"action reference is neither a full commit SHA nor an exact version tag: {ref!r}"
        )


def test_dynamodb_local_service_container_is_configured_for_layer_2() -> None:
    """CI runs a DynamoDB Local service on port 8000 and sets `CRM_REQUIRE_DYNAMODB_LOCAL`."""
    workflow = _load_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))
    services_found = [
        job.get("services", {}) for job in workflow.get("jobs", {}).values() if job.get("services")
    ]
    assert services_found, "no job configures a 'services' container"

    all_services = {name: cfg for services in services_found for name, cfg in services.items()}
    dynamodb_services = {
        name: cfg
        for name, cfg in all_services.items()
        if "dynamodb" in cfg.get("image", "").lower()
    }
    assert dynamodb_services, f"no DynamoDB Local service container found among: {all_services!r}"

    for name, cfg in dynamodb_services.items():
        ports = cfg.get("ports", [])
        assert any("8000" in str(p) for p in ports), (
            f"service {name!r} does not expose port 8000: {ports!r}"
        )

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "CRM_REQUIRE_DYNAMODB_LOCAL" in workflow_text, (
        "workflow does not set CRM_REQUIRE_DYNAMODB_LOCAL, so Layer 2 could silently skip"
    )


# ---------------------------------------------------------------------------
# The T4 browser tier: registered, gated, and unable to report success when it did not run.
# ---------------------------------------------------------------------------

#: The token that acknowledges an action is billable, shared with `scripts/verify_deployment.sh`.
BILLABLE_ACKNOWLEDGMENT_TOKEN = "--i-understand-this-is-billable"  # nosec B105  # CLI flag literal, not a credential

#: The browser journey directory.
JOURNEYS_DIR = "tests/e2e/browser"


def test_non_vacuity_the_browser_workflow_parses_with_a_job_and_steps() -> None:
    """The browser workflow parses with a job, its steps, and a `playwright test` step."""
    assert BROWSER_WORKFLOW_PATH.is_file(), f"{BROWSER_WORKFLOW_PATH} does not exist"
    steps = _browser_steps()
    assert len(steps) >= 5, f"expected the full browser pipeline, got {len(steps)} step(s)"
    assert any("playwright test" in str(step.get("run", "")) for step in steps), (
        "no step runs `playwright test`, so this workflow does not run the browser tier at all"
    )


def test_the_browser_tier_is_registered_in_ci() -> None:
    """The browser workflow runs `playwright test` from the journeys directory."""
    steps = _browser_steps()
    runners = [
        str(step.get("run", "")) for step in steps if "playwright test" in str(step.get("run", ""))
    ]
    assert runners, "no step runs the journeys"
    working_dirs = {
        step.get("working-directory")
        for step in steps
        if "playwright test" in str(step.get("run", ""))
    }
    assert working_dirs == {JOURNEYS_DIR}, (
        f"the journeys must run from {JOURNEYS_DIR!r}, got {working_dirs!r}"
    )


def test_without_the_opt_in_the_browser_tier_cannot_report_success() -> None:
    """Without the opt-in the browser job exits 3 before any install, with no job-level `if:`."""
    job = _browser_job()
    assert "if" not in job, (
        "the browser job carries a job-level `if:`, so absent the opt-in it would SKIP and leave a "
        "green workflow run — the exact 'a skip is not a pass' violation this tier must not have"
    )

    steps = _browser_steps()
    gate_candidates = [step for step in steps if "exit 3" in str(step.get("run", ""))]
    assert len(gate_candidates) == 1, (
        f"expected exactly one step that exits 3, found {len(gate_candidates)}: "
        f"{[step.get('name') for step in gate_candidates]}"
    )
    gate = gate_candidates[0]
    gate_script = str(gate["run"])

    assert "CRM_RUN_BROWSER_JOURNEYS" in gate_script, (
        "the gate does not check CRM_RUN_BROWSER_JOURNEYS"
    )
    assert BILLABLE_ACKNOWLEDGMENT_TOKEN in gate_script, (
        "the gate does not check the billable acknowledgment"
    )

    # The gate precedes every install and every step that touches the stack.
    gate_index = steps.index(gate)
    for index, step in enumerate(steps):
        run = str(step.get("run", ""))
        touches = "npm ci" in run or "playwright" in run
        if touches:
            assert index > gate_index, f"step {step.get('name')!r} runs before the opt-in gate"


def test_the_browser_tier_never_runs_automatically() -> None:
    """The browser workflow is triggered by `workflow_dispatch` only."""
    triggers = _triggers(_browser_workflow())
    assert set(triggers) == {"workflow_dispatch"}, (
        f"the browser tier must be workflow_dispatch only, got triggers: {sorted(triggers)}"
    )


def test_the_browser_tier_is_gated_behind_two_explicit_opt_ins() -> None:
    """Both dispatch opt-ins default to off and the billable token is named exactly."""
    triggers = _triggers(_browser_workflow())
    inputs = triggers["workflow_dispatch"]["inputs"]

    run_input = inputs["run_browser_journeys"]
    assert run_input.get("default") is False, (
        f"run_browser_journeys must default to false, got {run_input.get('default')!r}"
    )

    ack_input = inputs["billable_acknowledgment"]
    assert ack_input.get("default") == "", (
        f"billable_acknowledgment must default to empty, got {ack_input.get('default')!r}"
    )
    assert BILLABLE_ACKNOWLEDGMENT_TOKEN in str(ack_input.get("description", "")), (
        "the acknowledgment input does not name the exact token an operator must type"
    )

    job_env = _browser_job().get("env", {})
    assert "CRM_RUN_BROWSER_JOURNEYS" in job_env, (
        "the in-job predicate CRM_RUN_BROWSER_JOURNEYS is not set from the dispatch input"
    )


def test_the_billable_acknowledgment_token_is_the_repository_wide_one() -> None:
    """The CI acknowledgment token is the one `scripts/verify_deployment.sh` requires."""
    script = (REPO_ROOT / "scripts" / "verify_deployment.sh").read_text(encoding="utf-8")
    assert BILLABLE_ACKNOWLEDGMENT_TOKEN in script, (
        f"{BILLABLE_ACKNOWLEDGMENT_TOKEN!r} is no longer the token verify_deployment.sh requires; "
        "CI and the script have drifted apart"
    )
    assert BILLABLE_ACKNOWLEDGMENT_TOKEN in BROWSER_WORKFLOW_PATH.read_text(encoding="utf-8")


def test_no_browser_step_swallows_a_failure() -> None:
    """No browser job or step uses `continue-on-error`, `|| true` or `|| exit 0`."""
    job = _browser_job()
    assert job.get("continue-on-error") is not True, "the browser job is continue-on-error"
    for step in _browser_steps():
        assert step.get("continue-on-error") is not True, (
            f"step {step.get('name')!r} is continue-on-error"
        )
        run = str(step.get("run", ""))
        assert "|| true" not in run, f"step {step.get('name')!r} swallows failure with `|| true`"
        assert "|| exit 0" not in run, (
            f"step {step.get('name')!r} swallows failure with `|| exit 0`"
        )


def test_the_browser_workflow_does_not_enumerate_the_journey_spec_files() -> None:
    """The browser workflow names no individual journey spec file."""
    text = BROWSER_WORKFLOW_PATH.read_text(encoding="utf-8")
    enumerated = re.findall(r"\b\d{2}-[a-z-]+\.spec\.ts\b", text)
    assert not enumerated, (
        f"the workflow names individual journey spec files: {enumerated}. Target the directory and "
        "let Playwright discover them, so the set cannot drift from the directory"
    )


def test_the_browser_tier_installs_from_the_lockfile_with_a_pinned_playwright() -> None:
    """The browser tier installs with `npm ci` and pins `@playwright/test` exactly."""
    runs = [str(step.get("run", "")) for step in _browser_steps()]
    assert any(run.strip().startswith("npm ci") for run in runs), (
        "the browser tier does not install from the lockfile with `npm ci`"
    )
    assert not any("npm install" in run for run in runs), (
        "the browser tier uses `npm install`, which lets the resolver pick versions"
    )

    manifest = json.loads((REPO_ROOT / JOURNEYS_DIR / "package.json").read_text(encoding="utf-8"))
    pin = manifest["devDependencies"]["@playwright/test"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin), (
        f"@playwright/test must be exact-pinned, got {pin!r}"
    )


def test_the_browser_tier_target_is_discovered_and_never_hardcoded() -> None:
    """The browser workflow hardcodes no target URL and uses the discovery variables."""
    text = BROWSER_WORKFLOW_PATH.read_text(encoding="utf-8")
    # Comment lines are excluded from the URL scan.
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    hardcoded = [
        url
        for url in re.findall(r"https?://[^\s\"'<>]+", body)
        if "<dev-" not in url and "github" not in url
    ]
    assert not hardcoded, f"the workflow hardcodes a target URL: {hardcoded}"
    assert "CRM_E2E_BASE_URL" in text and "CRM_TF_OUTPUTS_JSON" in text, (
        "the workflow does not resolve its target from the documented discovery variables"
    )


def test_only_the_browser_workflow_references_repository_secrets() -> None:
    """Only the browser workflow references repository secrets."""
    with_secrets = {
        path.name
        for path in _all_workflow_paths()
        if "secrets." in path.read_text(encoding="utf-8")
    }
    assert with_secrets == {BROWSER_WORKFLOW_PATH.name}, (
        f"expected only {BROWSER_WORKFLOW_PATH.name} to reference secrets, got {sorted(with_secrets)}"
    )


# ---------------------------------------------------------------------------
# CI provisions what `CRM_REQUIRE_TERRAFORM=1` requires: the binary and the provider plugin cache.
# ---------------------------------------------------------------------------

#: The setup action and init command that provide the terraform binary and plugin cache.
_TERRAFORM_SETUP_ACTION = "hashicorp/setup-terraform"
_TERRAFORM_PLUGIN_CACHE_COMMAND = "terraform -chdir=infra init -backend=false"


def _verify_target_exported_requirements(makefile_text: str) -> set[str]:
    """Return every `CRM_REQUIRE_*` variable the `verify` target exports."""
    return set(
        re.findall(r"^verify:\s*export\s+(CRM_REQUIRE_\w+)\s*=", makefile_text, re.MULTILINE)
    )


def _ci_satisfies_terraform_requirement(workflow_text: str) -> bool:
    """Return whether the workflow provisions both clauses of the terraform harness predicate."""
    return (
        _TERRAFORM_SETUP_ACTION in workflow_text
        and _TERRAFORM_PLUGIN_CACHE_COMMAND in workflow_text
    )


def test_the_terraform_requirement_and_its_ci_provision_agree_in_both_directions() -> None:
    """`make verify` exports `CRM_REQUIRE_TERRAFORM` exactly when CI provisions terraform."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    exported = "CRM_REQUIRE_TERRAFORM" in _verify_target_exported_requirements(makefile_text)
    provisioned = _ci_satisfies_terraform_requirement(workflow_text)
    assert exported == provisioned, (
        "the Makefile and CI disagree about terraform: "
        f"`make verify` exports CRM_REQUIRE_TERRAFORM={exported}, "
        f"verify.yml provisions both the binary and {_TERRAFORM_PLUGIN_CACHE_COMMAND!r}={provisioned}. "
        "Exported-but-unprovisioned is 49 unexplained CI errors; provisioned-but-unexported is a "
        "silent skip of the same 49."
    )


def test_ci_provisions_both_clauses_of_the_terraform_availability_predicate() -> None:
    """CI installs the terraform binary and populates the provider plugin cache."""
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert _TERRAFORM_SETUP_ACTION in workflow_text, "CI installs no terraform binary"
    assert _TERRAFORM_PLUGIN_CACHE_COMMAND in workflow_text, (
        "CI never populates infra/.terraform/providers, so the plan-time fixture stays unavailable "
        "even with a terraform binary present (infra/.terraform/ is gitignored and cannot be "
        "inherited from the checkout)"
    )


def test_non_vacuity_dropping_either_terraform_provision_is_detected() -> None:
    """Removing either terraform provision from a copy of the workflow reads as unprovisioned."""
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert _ci_satisfies_terraform_requirement(workflow_text), "fixture assumption broken"
    without_binary = workflow_text.replace(_TERRAFORM_SETUP_ACTION, "some/other-action")
    without_cache = workflow_text.replace(_TERRAFORM_PLUGIN_CACHE_COMMAND, "terraform version")
    assert not _ci_satisfies_terraform_requirement(without_binary)
    assert not _ci_satisfies_terraform_requirement(without_cache)


def test_the_ci_terraform_version_matches_the_repositorys_own_required_version_floor() -> None:
    """CI pins an exact terraform version at or above `infra/backend.tf`'s `required_version`."""
    backend_text = (REPO_ROOT / "infra" / "backend.tf").read_text(encoding="utf-8")
    floor_match = re.search(r'required_version\s*=\s*">=\s*([0-9.]+)"', backend_text)
    assert floor_match is not None, 'no `required_version = ">= X.Y.Z"` found in infra/backend.tf'
    floor = floor_match.group(1)

    workflow = _load_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))
    pins = [
        str(step.get("with", {}).get("terraform_version"))
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if _TERRAFORM_SETUP_ACTION in str(step.get("uses", ""))
    ]
    assert pins, f"no {_TERRAFORM_SETUP_ACTION} step declares a terraform_version"
    for pin in pins:
        assert re.fullmatch(r"\d+\.\d+\.\d+", pin), (
            f"terraform_version must be an exact X.Y.Z pin, got {pin!r}"
        )
        assert tuple(int(part) for part in pin.split(".")) >= tuple(
            int(part) for part in floor.split(".")
        ), f"CI pins terraform {pin} but infra/ requires >= {floor}"


def test_the_terraform_wrapper_is_disabled_so_the_fixture_reads_real_stdout() -> None:
    """Every setup-terraform step sets `terraform_wrapper: false`."""
    workflow = _load_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if _TERRAFORM_SETUP_ACTION in str(step.get("uses", "")):
                assert step.get("with", {}).get("terraform_wrapper") is False, (
                    "terraform_wrapper must be explicitly false: the default wraps the binary and "
                    "the plan-time fixture parses its raw stdout"
                )


# ---------------------------------------------------------------------------
# CI installs gitleaks, so check_secret_scan returns PASS/FAIL there rather than exit 3 ([SKIP]).
# ---------------------------------------------------------------------------

GITLEAKS_RELEASE_HOST = "github.com/gitleaks/gitleaks/releases/download"

#: Matches an invocation of the gitleaks binary, however its path is quoted.
_INVOKES_GITLEAKS_RE = re.compile(r"gitleaks[\"']?\s+version")


def _gitleaks_install_step() -> dict[str, Any]:
    """Return the workflow step that downloads gitleaks from its release host."""
    workflow = _load_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if GITLEAKS_RELEASE_HOST in str(step.get("run", "")):
                return dict(step)
    raise AssertionError(
        f"no step in {WORKFLOW_PATH.name} downloads gitleaks from {GITLEAKS_RELEASE_HOST}; "
        "check_secret_scan would exit 3 ([SKIP]) in CI, which is not a scan"
    )


def test_ci_installs_gitleaks_so_the_secret_scan_is_not_a_permanent_skip() -> None:
    """CI installs gitleaks and invokes the binary in the install step."""
    step = _gitleaks_install_step()
    # The binary is invoked by quoted full path, since GITHUB_PATH applies only to later steps.
    assert _INVOKES_GITLEAKS_RE.search(str(step.get("run", ""))), (
        "the install step never invokes the binary, so a broken install would pass silently"
    )


def test_the_gitleaks_pin_is_a_tag_and_a_hardcoded_digest() -> None:
    """The gitleaks install pins an exact version and a literal sha256 digest."""
    step = _gitleaks_install_step()
    env = step.get("env", {})
    version = str(env.get("GITLEAKS_VERSION", ""))
    digest = str(env.get("GITLEAKS_SHA256", ""))
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"GITLEAKS_VERSION must be an exact X.Y.Z pin, got {version!r}"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", digest), (
        f"GITLEAKS_SHA256 must be a literal 64-hex-character sha256, got {digest!r}"
    )


def test_the_digest_is_verified_before_the_binary_is_executed() -> None:
    """The gitleaks digest is verified before the archive is extracted or the binary run."""
    script = str(_gitleaks_install_step().get("run", ""))
    verify_at = script.find("sha256sum")
    extract_at = script.find("tar -xzf")
    invoked = _INVOKES_GITLEAKS_RE.search(script)
    assert verify_at != -1, "the step never verifies a checksum"
    assert extract_at != -1 and invoked is not None
    execute_at = invoked.start()
    assert verify_at < extract_at, "the archive is extracted before its digest is verified"
    assert verify_at < execute_at, "the binary is executed before its digest is verified"


def test_the_checksum_is_not_fetched_from_the_release() -> None:
    """The gitleaks install does not fetch the release's `checksums.txt`."""
    script = str(_gitleaks_install_step().get("run", ""))
    assert "checksums.txt" not in script, (
        "the step fetches the release's own checksum file; the expected digest must be a literal that "
        "an attacker able to swap the asset cannot also swap"
    )


def test_the_secret_scan_gate_exists_and_declares_the_skip_exit_status() -> None:
    """`scripts/check_secret_scan.py` exists, declares exit status 3, and redacts findings."""
    gate = REPO_ROOT / "scripts" / "check_secret_scan.py"
    assert gate.is_file(), "scripts/check_secret_scan.py is missing"
    source = gate.read_text(encoding="utf-8")
    assert "EXIT_VERIFIED_NOTHING: Final[int] = 3" in source
    assert "--redact" in source, "the gate must never print the secret it found"


# ---------------------------------------------------------------------------
# The `tsc` gates, and whether an incremental cache can serve a green result over a broken tree.
#
# Only `frontend` uses build mode (`tsc -b`) with a `tsBuildInfoFile`. The controls are `--force` and
# a buildinfo path under the directory `npm ci` recreates.
# ---------------------------------------------------------------------------

FRONTEND_DIR = "frontend"

#: The projects the frontend's `tsc -b` builds, each of which declares its own buildinfo path.
_FRONTEND_TSCONFIGS = ("tsconfig.app.json", "tsconfig.node.json")

#: The directory `npm ci` deletes and recreates on every install.
_NPM_CI_MANAGED_DIR = "node_modules"


def _frontend_typecheck_steps() -> list[dict[str, Any]]:
    """Return the workflow steps that run the frontend's typecheck script."""
    workflow = _load_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return [
        step
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if re.search(r"npm run typecheck", str(step.get("run", "")))
    ]


def _tsbuildinfo_paths() -> dict[str, str]:
    """Return the `tsBuildInfoFile` each frontend project declares, read by regex."""
    paths: dict[str, str] = {}
    for name in _FRONTEND_TSCONFIGS:
        text = (REPO_ROOT / FRONTEND_DIR / name).read_text(encoding="utf-8")
        match = re.search(r'"tsBuildInfoFile"\s*:\s*"([^"]+)"', text)
        assert match is not None, (
            f"{FRONTEND_DIR}/{name} declares no tsBuildInfoFile; if build mode's cache location is now "
            f"implicit, this test can no longer tell whether it survives between CI runs"
        )
        paths[name] = match.group(1)
    return paths


def test_the_frontend_typecheck_disables_the_incremental_up_to_date_check() -> None:
    """Every CI frontend typecheck step passes `--force`."""
    steps = _frontend_typecheck_steps()
    assert steps, "no step runs the frontend typecheck"
    for step in steps:
        run = str(step.get("run", ""))
        assert "--force" in run, (
            f"the frontend typecheck runs without --force, so build mode may skip projects it "
            f"believes are up to date and report a cached result: {run!r}"
        )


def test_no_frontend_buildinfo_can_survive_between_ci_runs() -> None:
    """Every frontend `tsBuildInfoFile` sits under `node_modules/`."""
    for name, path in _tsbuildinfo_paths().items():
        normalised = path.lstrip("./")
        assert normalised.startswith(f"{_NPM_CI_MANAGED_DIR}/"), (
            f"{FRONTEND_DIR}/{name} puts its buildinfo at {path!r}, outside {_NPM_CI_MANAGED_DIR}/. "
            f"`npm ci` would no longer clear it, so a cached type-check result could outlive the run "
            f"that produced it"
        )


def test_ci_does_not_cache_the_frontend_workspace_across_runs() -> None:
    """No CI step uses `actions/cache` or enables setup-node's dependency cache.

    Restoring `node_modules/` restores its `.tsbuildinfo` files, so a cache is safe only if every
    frontend typecheck step passes `--force`.
    """
    workflow = _load_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))
    for job_name, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []):
            uses = str(step.get("uses", ""))
            assert "actions/cache" not in uses, (
                f"job {job_name!r} restores a cache; see this test's docstring before allowing it"
            )
            if _NODE_SETUP_ACTION in uses:
                assert "cache" not in step.get("with", {}), (
                    f"job {job_name!r} enables setup-node's dependency cache, which restores "
                    f"node_modules/ and with it any .tsbuildinfo inside it"
                )


def test_every_setup_node_step_disables_the_package_manager_cache() -> None:
    """Each setup-node step sets `package-manager-cache: false`, in every workflow."""
    found = 0
    for path in (WORKFLOW_PATH, BROWSER_WORKFLOW_PATH):
        workflow = _load_workflow(path.read_text(encoding="utf-8"))
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                if _NODE_SETUP_ACTION in str(step.get("uses", "")):
                    found += 1
                    assert step.get("with", {}).get("package-manager-cache") is False, (
                        f"{path.name} job {job_name!r}: setup-node caches automatically when a "
                        "package.json declares `packageManager` unless this input is false"
                    )
    assert found, "no setup-node step found"


def test_non_vacuity_a_cached_typecheck_invocation_is_detected() -> None:
    """A typecheck step without `--force` is detected by the step filter."""
    cached_workflow = _load_workflow(
        """
jobs:
  frontend:
    runs-on: ubuntu-24.04
    steps:
      - name: Typecheck
        run: npm run typecheck:no-gen
"""
    )
    steps = [
        step
        for job in cached_workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if re.search(r"npm run typecheck", str(step.get("run", "")))
    ]
    assert steps, "the mutant fixture itself did not parse"
    assert not any("--force" in str(step.get("run", "")) for step in steps)


# ---------------------------------------------------------------------------
# The relay's TypeScript suite is wired into `make verify` and CI.
# ---------------------------------------------------------------------------

RELAY_DIR = "src/relay"
RELAY_MAKE_TARGET = "test-relay"
RELAY_REQUIRE_ENV_VAR = "CRM_REQUIRE_RELAY_TESTS"

#: The GitHub action that installs Node.
_NODE_SETUP_ACTION = "actions/setup-node"


def _verify_sub_targets() -> list[str]:
    """Return the sub-targets `make verify` runs, parsed from the Makefile."""
    recipe = _makefile_recipes().get("verify", [])
    make_line = next((line for line in recipe if line.startswith("$(MAKE)")), None)
    assert make_line is not None, f"'verify' recipe has no $(MAKE) line: {recipe}"
    return [token for token in make_line.split()[1:] if not token.startswith("-")]


def _verify_job() -> dict[str, Any]:
    """Return the workflow job that runs `make verify`."""
    workflow = _load_workflow(WORKFLOW_PATH.read_text(encoding="utf-8"))
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if re.search(r"\bmake\s+verify\b", str(step.get("run", ""))):
                assert isinstance(job, dict)
                return job
    raise AssertionError("no job runs `make verify`")


def _relay_install_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in job.get("steps", [])
        if step.get("working-directory") == RELAY_DIR and "npm ci" in str(step.get("run", ""))
    ]


def _ci_satisfies_relay_test_requirement(job: dict[str, Any]) -> bool:
    """Return whether the `make verify` job installs Node and the relay's `node_modules`."""
    has_node = any(_NODE_SETUP_ACTION in str(step.get("uses", "")) for step in job.get("steps", []))
    return has_node and bool(_relay_install_steps(job))


def test_the_relay_suite_is_actually_wired_into_make_verify() -> None:
    """The relay test target exists, has a recipe, and is run by `make verify`."""
    recipes = _makefile_recipes()
    assert RELAY_MAKE_TARGET in recipes, f"Makefile has no {RELAY_MAKE_TARGET!r} target"
    assert recipes[RELAY_MAKE_TARGET], f"{RELAY_MAKE_TARGET!r} has an empty recipe"
    assert RELAY_MAKE_TARGET in _verify_sub_targets(), (
        f"{RELAY_MAKE_TARGET!r} is not among the sub-targets `make verify` runs, so the relay suite "
        f"is reachable by hand and run by nothing — the state this target exists to end"
    )
    recipe_text = "\n".join(recipes[RELAY_MAKE_TARGET])
    assert "npm run" in recipe_text and "test" in recipe_text, (
        f"{RELAY_MAKE_TARGET!r} does not invoke the relay's npm test script: {recipe_text!r}"
    )


def test_the_relay_requirement_and_its_ci_provision_agree_in_both_directions() -> None:
    """`make verify` exports the relay require flag exactly when CI provisions Node and npm ci."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    exported = RELAY_REQUIRE_ENV_VAR in _verify_target_exported_requirements(makefile_text)
    provisioned = _ci_satisfies_relay_test_requirement(_verify_job())
    assert exported == provisioned, (
        f"the Makefile and CI disagree about the relay suite: `make verify` exports "
        f"{RELAY_REQUIRE_ENV_VAR}={exported}, the CI job provisioning Node and "
        f"`npm ci` in {RELAY_DIR}={provisioned}. Exported-but-unprovisioned is an unexplained CI "
        f"failure; provisioned-but-unexported is a silent skip of the entire relay suite."
    )


def test_ci_provisions_both_clauses_of_the_relay_test_availability_predicate() -> None:
    """The `make verify` job sets up Node and installs the relay's dependencies."""
    job = _verify_job()
    assert any(_NODE_SETUP_ACTION in str(step.get("uses", "")) for step in job.get("steps", [])), (
        "the `make verify` job sets up no pinned Node, so the relay suite would run on whatever "
        "version the runner image ships — a non-reproducible build"
    )
    assert _relay_install_steps(job), (
        f"no step installs {RELAY_DIR}'s dependencies, so `test-relay` would hard-fail in CI under "
        f"{RELAY_REQUIRE_ENV_VAR}=1 with a reason no test explains"
    )


def test_non_vacuity_dropping_either_relay_provision_is_detected() -> None:
    """Removing either relay provision from a copy of the job reads as unprovisioned."""
    job = _verify_job()
    assert _ci_satisfies_relay_test_requirement(job), "fixture assumption broken"

    without_node = {
        **job,
        "steps": [
            step
            for step in job.get("steps", [])
            if _NODE_SETUP_ACTION not in str(step.get("uses", ""))
        ],
    }
    without_install = {
        **job,
        "steps": [step for step in job.get("steps", []) if step not in _relay_install_steps(job)],
    }
    assert not _ci_satisfies_relay_test_requirement(without_node)
    assert not _ci_satisfies_relay_test_requirement(without_install)


def test_the_relay_installs_from_the_lockfile_and_never_resolves_versions() -> None:
    """The relay has a lockfile and installs with `npm ci`, never `npm install`."""
    assert (REPO_ROOT / RELAY_DIR / "package-lock.json").is_file(), (
        f"{RELAY_DIR}/package-lock.json is missing, so there is no install authority to be sole"
    )
    for step in _relay_install_steps(_verify_job()):
        run = str(step.get("run", ""))
        assert not re.search(r"npm install\b", run), (
            f"the relay install step uses `npm install`, not `npm ci`: {run!r}"
        )


#: The Terraform file whose `runtime` default is the single source for the relay's Node major.
_RELAY_RUNTIME_TF = REPO_ROOT / "infra" / "modules" / "agent_relay" / "variables.tf"


def _terraform_runtime_major() -> int:
    """Return the relay Lambda's `nodejs<major>.x` runtime major from the Terraform default."""
    text = _RELAY_RUNTIME_TF.read_text(encoding="utf-8")
    match = re.search(r'(?ms)^variable\s+"runtime"\s*\{.*?default\s*=\s*"nodejs(\d+)\.x"', text)
    assert match is not None, (
        f'no `variable "runtime"` with a `default = "nodejs<major>.x"` in {_RELAY_RUNTIME_TF}'
    )
    return int(match.group(1))


def _node_derivation_step() -> dict[str, Any]:
    """Return the `make verify` job step that derives the Node major from the Terraform file."""
    for step in _verify_job().get("steps", []):
        if _RELAY_RUNTIME_TF.name in str(step.get("run", "")) and "nodejs" in str(
            step.get("run", "")
        ):
            return dict(step)
    raise AssertionError(
        f"no step in the `make verify` job derives a Node version from {_RELAY_RUNTIME_TF.name}; "
        "a literal or a second pin file would drift from the runtime the relay actually deploys on"
    )


def test_ci_derives_the_relay_node_major_from_the_deployed_lambda_runtime() -> None:
    """Every setup-node step in the `make verify` job consumes the derivation step's output."""
    derivation = _node_derivation_step()
    step_id = derivation.get("id")
    assert step_id, "the derivation step has no `id`, so no later step can consume its output"

    setup_steps = [
        step
        for step in _verify_job().get("steps", [])
        if _NODE_SETUP_ACTION in str(step.get("uses", ""))
    ]
    assert setup_steps, "the `make verify` job sets up no Node at all"
    for step in setup_steps:
        version_input = str(step.get("with", {}).get("node-version", ""))
        assert f"steps.{step_id}.outputs" in version_input, (
            f"setup-node reads {version_input!r} rather than the derivation step's output, so the "
            f"derived major is computed and then ignored"
        )


def test_the_node_derivation_fails_loudly_on_an_unparseable_runtime() -> None:
    """The Node derivation step fails non-zero on an empty parse and supplies no default."""
    script = str(_node_derivation_step().get("run", ""))
    assert "set -euo pipefail" in script, "the derivation step does not fail on an error"
    assert re.search(r'if\s+\[\s+-z\s+"\$\{?runtime', script), (
        "the derivation does not explicitly test for an empty parse, so an unparseable runtime would "
        "flow onward as an empty version rather than failing"
    )
    assert "exit 1" in script, "the derivation has no non-zero exit path"
    assert not re.search(r"version=\$\{[a-z_]+:-", script), (
        "the derivation supplies a shell default for the version — a fallback is what makes this class "
        "of failure silent"
    )


def test_the_derived_node_major_and_the_terraform_runtime_agree_in_both_directions() -> None:
    """The derivation step's own grep window yields the Terraform runtime major."""
    runtime_major = _terraform_runtime_major()
    script = str(_node_derivation_step().get("run", ""))

    # Re-derive with the step's own grep window against the real file.
    window = re.search(r"grep -A(\d+) '\^variable \"runtime\"'", script)
    assert window is not None, "cannot find the derivation's `grep -A<n>` window in the step"
    lines = _RELAY_RUNTIME_TF.read_text(encoding="utf-8").splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith('variable "runtime"')), None
    )
    assert start is not None, 'no `variable "runtime"` block at the start of a line'
    scanned = "\n".join(lines[start : start + int(window.group(1)) + 1])
    found = re.findall(r"nodejs(\d+)\.x", scanned)
    assert found, (
        f"the derivation's `grep -A{window.group(1)}` window does not reach the `runtime` default in "
        f"{_RELAY_RUNTIME_TF.name}; CI would fail the parse it is supposed to succeed at"
    )
    assert int(found[0]) == runtime_major


def test_the_derived_node_major_satisfies_the_relays_own_engine_floor() -> None:
    """The relay's Terraform runtime major satisfies `src/relay/package.json`'s `engines.node`."""
    manifest = json.loads((REPO_ROOT / RELAY_DIR / "package.json").read_text(encoding="utf-8"))
    floor_spec = str(manifest["engines"]["node"])
    floor_match = re.fullmatch(r">=\s*(\d+)(?:\.\d+)*", floor_spec)
    assert floor_match is not None, (
        f"cannot read a major-version floor out of engines.node={floor_spec!r}"
    )
    assert _terraform_runtime_major() >= int(floor_match.group(1)), (
        f"the relay deploys on nodejs{_terraform_runtime_major()}.x but {RELAY_DIR} declares "
        f"engines.node={floor_spec!r}"
    )


def test_the_frontend_job_still_pins_from_the_repositorys_nvmrc() -> None:
    """The frontend job reads `frontend/.nvmrc`, which holds an exact X.Y.Z pin."""
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "frontend/.nvmrc" in workflow_text, "the frontend job no longer reads frontend/.nvmrc"
    pinned = (REPO_ROOT / "frontend" / ".nvmrc").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned), (
        f"frontend/.nvmrc is not a full X.Y.Z pin: {pinned!r}"
    )


def test_the_relay_target_hard_fails_rather_than_skipping_under_the_require_flag() -> None:
    """The relay target skips by default and exits 1 only after consulting the require flag."""
    recipe = "\n".join(_makefile_recipes()[RELAY_MAKE_TARGET])
    assert RELAY_REQUIRE_ENV_VAR in recipe, (
        f"{RELAY_MAKE_TARGET!r} never consults {RELAY_REQUIRE_ENV_VAR}, so `make verify` cannot "
        f"distinguish a missing toolchain from a passing suite"
    )
    assert "exit 1" in recipe, f"{RELAY_MAKE_TARGET!r} has no hard-fail branch"
    assert "[SKIP]" in recipe, (
        f"{RELAY_MAKE_TARGET!r} has no skip branch, so it is unusable without a relay install"
    )
    # The fail branch is guarded by the flag.
    assert recipe.index(RELAY_REQUIRE_ENV_VAR) < recipe.index("exit 1"), (
        f"{RELAY_MAKE_TARGET!r} reaches `exit 1` before consulting {RELAY_REQUIRE_ENV_VAR}"
    )


def test_the_relay_packaging_requirement_is_not_mistaken_for_the_suite() -> None:
    """`make verify` exports both the relay packaging and relay test requirements."""
    exported = _verify_target_exported_requirements(MAKEFILE_PATH.read_text(encoding="utf-8"))
    assert {"CRM_REQUIRE_RELAY_PACKAGING", RELAY_REQUIRE_ENV_VAR} <= exported, (
        f"`make verify` must export both relay requirements; it exports {sorted(exported)}"
    )


def test_the_workflow_still_references_no_repository_secret() -> None:
    """The verify workflow references no secret and does not use `gitleaks-action`."""
    assert "secrets." not in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "gitleaks-action" not in WORKFLOW_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Environment properties asserted of both CI definitions: the system tool set, a UTF-8 locale, a
# pinned shellcheck, and the emitted-css coverage. Each expected set is derived from the files.
# ---------------------------------------------------------------------------

GITLAB_CI_PATH = REPO_ROOT / ".gitlab-ci.yml"

#: The package that identifies the base system-tool `apt-get install` among a job's installs.
_APT_BASE_INSTALL_SENTINEL = "make"

#: The tool installed from a pinned release rather than the distribution.
_PINNED_SHELLCHECK_TOOL = "shellcheck"

_APT_INSTALL_RE = re.compile(r"apt-get install\s+([^\n&|;]*)")

#: A `command -v <tool>` preflight at column 0, which every invocation of the script reaches.
_TOP_LEVEL_TOOL_PREFLIGHT_RE = re.compile(r"^command -v ([a-z][a-z0-9_.-]*)\b", re.MULTILINE)

_SHELLCHECK_RELEASE_HOST = "github.com/koalaman/shellcheck/releases/download"


def _yaml_scalars(node: Any) -> Iterator[str]:
    """Yield every string scalar in a parsed YAML document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _yaml_scalars(value)
    elif isinstance(node, list):
        for value in node:
            yield from _yaml_scalars(value)


def _ci_definition_paths() -> list[Path]:
    """Return the GitLab and GitHub CI definition paths."""
    return [GITLAB_CI_PATH, WORKFLOW_PATH]


def _load_ci(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} did not parse as a mapping"
    return data


#: GitLab top-level keys that configure the pipeline rather than declare a job.
_GITLAB_RESERVED_KEYS = frozenset(
    {
        "image",
        "stages",
        "variables",
        "default",
        "include",
        "workflow",
        "before_script",
        "after_script",
        "cache",
        "services",
    }
)


def _ci_jobs(path: Path) -> dict[str, Any]:
    """`{job name: job node}` for either definition, normalising the two schemas."""
    return _ci_jobs_from_doc(_load_ci(path))


def _ci_jobs_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Return `{job name: job node}` from an already-parsed CI document."""
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):  # GitHub Actions
        return {str(name): node for name, node in jobs.items() if isinstance(node, dict)}
    return {  # GitLab
        str(name): node
        for name, node in doc.items()
        if isinstance(node, dict)
        and not str(name).startswith(".")
        and str(name) not in _GITLAB_RESERVED_KEYS
    }


def _pipeline_level_env(path: Path) -> dict[str, Any]:
    """Variables the definition sets for every job: GitLab `variables:`, GHA `env:`."""
    doc = _load_ci(path)
    block = doc.get("env") if "jobs" in doc else doc.get("variables")
    return dict(block) if isinstance(block, dict) else {}


def _job_env(path: Path, job: dict[str, Any]) -> dict[str, Any]:
    """Return a job's declared variables: pipeline-level, overridden by job-level."""
    own = job.get("env") if "jobs" in _load_ci(path) else job.get("variables")
    merged = dict(_pipeline_level_env(path))
    if isinstance(own, dict):
        merged.update(own)
    return merged


def _make_verify_job_name(path: Path) -> str:
    """Return the name of the one job that runs `make verify`."""
    matches = [
        name
        for name, job in _ci_jobs(path).items()
        if any(re.search(r"\bmake\s+verify\b", scalar) for scalar in _yaml_scalars(job))
    ]
    assert len(matches) == 1, f"{path.name}: expected exactly one `make verify` job, got {matches}"
    return matches[0]


def test_every_gitlab_script_entry_parses_as_a_command_and_not_as_a_mapping() -> None:
    """Every GitLab script, before_script and after_script entry parses as a string command."""
    doc = _load_ci(GITLAB_CI_PATH)
    checked = 0
    for holder_name, holder in list(doc.items()) + list(_ci_jobs(GITLAB_CI_PATH).items()):
        if not isinstance(holder, dict):
            continue
        for key in ("before_script", "script", "after_script"):
            entries = holder.get(key)
            if entries is None:
                continue
            assert isinstance(entries, list), f"{holder_name}.{key} is not a list: {entries!r}"
            for index, entry in enumerate(_flattened(entries)):
                checked += 1
                assert isinstance(entry, str), (
                    f"{holder_name}.{key}[{index}] parsed as {type(entry).__name__}, not a shell "
                    f"command: {entry!r}. An unquoted colon-space turns the line into a mapping and "
                    f"the command is silently lost."
                )
    assert checked >= 10, f"only {checked} script entries inspected — the walk regressed"


def _flattened(entries: list[Any]) -> Iterator[Any]:
    """Yield script entries, flattening nesting introduced by a YAML anchor alias."""
    for entry in entries:
        if isinstance(entry, list):
            yield from _flattened(entry)
        else:
            yield entry


def _apt_install_sets(node: Any) -> list[set[str]]:
    """Return one package set per `apt-get install` command found under `node`."""
    sets: list[set[str]] = []
    for scalar in _yaml_scalars(node):
        for match in _APT_INSTALL_RE.finditer(scalar):
            sets.append({token for token in match.group(1).split() if not token.startswith("-")})
    return sets


def _base_apt_tool_set(path: Path) -> set[str]:
    """Return the base system-tool set the `make verify` job installs."""
    job = _ci_jobs(path)[_make_verify_job_name(path)]
    candidates = [pkgs for pkgs in _apt_install_sets(job) if _APT_BASE_INSTALL_SENTINEL in pkgs]
    assert len(candidates) == 1, (
        f"{path.name}: expected exactly one apt install naming "
        f"{_APT_BASE_INSTALL_SENTINEL!r}, found {len(candidates)}: {candidates}. Splitting the "
        f"base install would make this comparison read only part of it."
    )
    return candidates[0]


def _shell_scripts_named_by_unit_tests() -> set[Path]:
    """Tracked `scripts/*.sh` files some `tests/unit` test shells out to, by name."""
    named: set[str] = set()
    for test in sorted((REPO_ROOT / "tests" / "unit").rglob("*.py")):
        named |= set(re.findall(r"scripts/[A-Za-z0-9_]+\.sh", test.read_text(encoding="utf-8")))
    return {REPO_ROOT / rel for rel in named if (REPO_ROOT / rel).is_file()}


def _tools_required_by_scripts_under_unit_test() -> dict[str, set[str]]:
    """Return `{tool: {script, ...}}` for each top-level `command -v` preflight in those scripts.

    e.g. `jq` in `scripts/verify_security_baseline.sh`.
    """
    required: dict[str, set[str]] = {}
    for script in _shell_scripts_named_by_unit_tests():
        for tool in _TOP_LEVEL_TOOL_PREFLIGHT_RE.findall(script.read_text(encoding="utf-8")):
            required.setdefault(tool, set()).add(script.name)
    return required


def _shell_files_with_non_ascii() -> dict[str, list[str]]:
    """Return `{filename: [non-ASCII characters]}` over `scripts/*.sh`."""
    found: dict[str, list[str]] = {}
    for script in sorted((REPO_ROOT / "scripts").glob("*.sh")):
        chars = sorted({ch for ch in script.read_text(encoding="utf-8") if ord(ch) > 127})
        if chars:
            found[script.name] = chars
    return found


def test_non_vacuity_both_ci_definitions_parse_with_the_same_named_jobs() -> None:
    """Both CI definitions parse, declare the same jobs, and each has one `make verify` job."""
    for path in _ci_definition_paths():
        assert path.is_file(), f"{path} does not exist"
        jobs = _ci_jobs(path)
        assert len(jobs) >= 2, f"{path.name}: parsed {len(jobs)} job(s): {sorted(jobs)}"
        _make_verify_job_name(path)  # raises unless exactly one job runs the aggregate gate

    gitlab_jobs = set(_ci_jobs(GITLAB_CI_PATH))
    github_jobs = set(_ci_jobs(WORKFLOW_PATH))
    assert gitlab_jobs == github_jobs, (
        f"the two definitions declare different jobs — {sorted(gitlab_jobs)} vs "
        f"{sorted(github_jobs)} — so the per-job comparisons below would compare a job "
        f"against nothing"
    )


def test_the_base_apt_tool_set_is_identical_in_both_ci_definitions() -> None:
    """Both CI definitions install the same base system-tool set."""
    sets = {path.name: _base_apt_tool_set(path) for path in _ci_definition_paths()}
    for name, tools in sets.items():
        assert len(tools) >= 5, f"{name}: parsed an implausibly small tool set: {sorted(tools)}"
    (first_name, first), (second_name, second) = sets.items()
    assert first == second, (
        f"the two CI definitions install different system tools: "
        f"{first_name} has {sorted(first - second)} that {second_name} lacks, and "
        f"{second_name} has {sorted(second - first)} that {first_name} lacks"
    )


def test_every_tool_a_unit_tested_script_refuses_to_run_without_names_jq() -> None:
    """A script named by a unit test carries a top-level `command -v jq` preflight."""
    required = _tools_required_by_scripts_under_unit_test()
    assert "jq" in required, (
        "no script named by a tests/unit test carries a top-level `command -v jq` "
        f"preflight any more; the derived requirement set is {sorted(required)}. If that is "
        "intentional, the jq assertion below is now vacuous and should be removed with it."
    )


def test_jq_is_in_both_tool_sets_because_a_unit_tested_script_demands_it() -> None:
    """Both CI definitions install jq."""
    demanders = _tools_required_by_scripts_under_unit_test()["jq"]
    for path in _ci_definition_paths():
        assert "jq" in _base_apt_tool_set(path), (
            f"{path.name} does not install jq, but {sorted(demanders)} exit 2 without it and "
            f"tests/unit shells out to them"
        )


def test_both_ci_definitions_set_a_utf8_locale_for_the_gate_job() -> None:
    """Both `make verify` jobs set UTF-8 `LANG` and `LC_ALL`."""
    offenders = _shell_files_with_non_ascii()
    assert offenders, (
        "no shell script under scripts/ contains a non-ASCII character any more, so this "
        "assertion no longer has a cause; re-derive it or delete it rather than leaving a "
        "requirement nothing needs"
    )

    for path in _ci_definition_paths():
        env = _job_env(path, _ci_jobs(path)[_make_verify_job_name(path)])
        for variable in ("LANG", "LC_ALL"):
            value = str(env.get(variable, ""))
            assert "UTF-8" in value.upper(), (
                f"{path.name}: the `make verify` job resolves {variable}={value!r}, which is not "
                f"a UTF-8 locale. shellcheck aborts with `commitBuffer: invalid argument` while "
                f"printing a finding on a line containing any of the characters in "
                f"{sorted(offenders)}"
            )


def _shellcheck_install_scalar(path: Path) -> str:
    """Return the one script block that downloads a pinned shellcheck release."""
    blocks = [
        scalar for scalar in _yaml_scalars(_load_ci(path)) if _SHELLCHECK_RELEASE_HOST in scalar
    ]
    assert len(blocks) == 1, (
        f"{path.name}: expected exactly one block downloading shellcheck from "
        f"{_SHELLCHECK_RELEASE_HOST}, found {len(blocks)}"
    )
    return blocks[0]


def test_shellcheck_is_never_installed_from_the_distribution_in_either_definition() -> None:
    """No apt install in either CI definition installs shellcheck."""
    for path in _ci_definition_paths():
        for packages in _apt_install_sets(_load_ci(path)):
            assert _PINNED_SHELLCHECK_TOOL not in packages, (
                f"{path.name} apt-installs {_PINNED_SHELLCHECK_TOOL}, whose distribution version "
                f"is not the one `verify-shellcheck` is green against locally; install the pinned "
                f"release instead"
            )


def test_both_definitions_pin_the_same_shellcheck_version_and_digests() -> None:
    """Both CI definitions pin the same shellcheck version and per-architecture digests."""
    pins: dict[str, tuple[set[str], set[str]]] = {}
    for path in _ci_definition_paths():
        text = path.read_text(encoding="utf-8")
        versions = set(re.findall(r'SHELLCHECK_VERSION:\s*"(\d+\.\d+\.\d+)"', text))
        digests = set(re.findall(r'SHELLCHECK_SHA256_[A-Z0-9_]+:\s*"([0-9a-f]{64})"', text))
        assert len(versions) == 1, f"{path.name}: expected one SHELLCHECK_VERSION, got {versions}"
        assert len(digests) >= 2, (
            f"{path.name}: expected a literal sha256 per architecture, got {len(digests)}"
        )
        pins[path.name] = (versions, digests)

    (first_name, first), (second_name, second) = pins.items()
    assert first == second, (
        f"{first_name} and {second_name} pin different shellcheck artefacts: {first} vs {second}"
    )


def test_the_shellcheck_digest_is_verified_before_the_binary_is_unpacked_or_run() -> None:
    """The shellcheck digest is verified before unpack and run, and PATH resolution is checked."""
    for path in _ci_definition_paths():
        script = _shellcheck_install_scalar(path)
        verify_at = script.find("sha256sum")
        unpack_at = script.find("tar -xJf")
        run_at = script.find("shellcheck --version")
        assert verify_at != -1, f"{path.name}: the shellcheck install verifies no checksum"
        assert unpack_at != -1 and run_at != -1, f"{path.name}: shellcheck is never unpacked or run"
        assert verify_at < unpack_at, f"{path.name}: unpacked before its digest is verified"
        assert verify_at < run_at, f"{path.name}: executed before its digest is verified"
        assert "checksums.txt" not in script, (
            f"{path.name}: fetches the release's own checksum file; whoever can swap the asset "
            f"can swap a checksum living beside it"
        )
        # The pinned binary is the one PATH resolves.
        assert "command -v shellcheck" in script, (
            f"{path.name}: the install never asserts which shellcheck PATH resolves, so an "
            f"earlier copy on PATH would run instead with every step reporting success"
        )


def _crm_require_map(path: Path) -> dict[str, dict[str, str]]:
    """Return `{job name: {CRM_REQUIRE_*: value}}` from a definition's declared variables."""
    result: dict[str, dict[str, str]] = {}
    for name, job in _ci_jobs(path).items():
        env = _job_env(path, job)
        result[name] = {
            str(key): str(value)
            for key, value in env.items()
            if str(key).startswith("CRM_REQUIRE_")
        }
    return result


def test_the_crm_require_surface_is_the_same_in_both_ci_definitions() -> None:
    """Both CI definitions declare the same `CRM_REQUIRE_*` variables per job."""
    maps = {path.name: _crm_require_map(path) for path in _ci_definition_paths()}
    (first_name, first), (second_name, second) = maps.items()
    assert first == second, (
        f"{first_name} and {second_name} declare different CRM_REQUIRE_* variables: "
        f"{first} vs {second}"
    )
    assert any(flags for flags in first.values()), (
        "neither definition declares any CRM_REQUIRE_* variable, so this comparison is "
        "vacuous — the four `make verify` exports are a different subject"
    )


def _emitted_css_require_variable() -> str:
    """Return the require-flag name declared in `test_check_emitted_css.py`."""
    text = (REPO_ROOT / "tests" / "unit" / "test_check_emitted_css.py").read_text(encoding="utf-8")
    match = re.search(r'REQUIRE_TOOLCHAIN_ENV_VAR\s*=\s*"(CRM_REQUIRE_\w+)"', text)
    assert match is not None, (
        "tests/unit/test_check_emitted_css.py no longer declares REQUIRE_TOOLCHAIN_ENV_VAR, so "
        "the variable CI must set can no longer be derived from the contract that consumes it"
    )
    return match.group(1)


def test_the_emitted_css_coverage_is_forced_in_the_only_job_that_can_run_it() -> None:
    """One non-verify job installs the frontend, sets the emitted-css flag, and runs those tests."""
    variable = _emitted_css_require_variable()
    test_path = "tests/unit/test_check_emitted_css.py"

    for path in _ci_definition_paths():
        require_map = _crm_require_map(path)
        setters = {name for name, flags in require_map.items() if flags.get(variable) == "1"}
        assert len(setters) == 1, (
            f"{path.name}: expected exactly one job setting {variable}=1, got {setters}"
        )
        job_name = next(iter(setters))
        assert job_name != _make_verify_job_name(path), (
            f"{path.name}: {variable}=1 is set on the `make verify` job, which cannot obtain the "
            f"Tailwind toolchain — that turns six legitimate skips into six hard failures"
        )

        job = _ci_jobs(path)[job_name]
        scalars = list(_yaml_scalars(job))
        assert any("npm ci" in scalar for scalar in scalars), (
            f"{path.name}: job {job_name!r} forces {variable} but never runs `npm ci`, so the "
            f"Tailwind PostCSS plugin the gate compiles with is absent and all six hard-fail"
        )
        assert any(test_path in scalar for scalar in scalars), (
            f"{path.name}: job {job_name!r} sets {variable} but never runs {test_path}; the flag "
            f"only changes skip into failure for tests that are collected somewhere"
        )


def test_the_pytest_worker_count_is_derived_from_the_runner_cores_in_both_definitions() -> None:
    """Both CI definitions run `nproc` and set `PYTEST_WORKERS` from a non-literal value."""
    variable_match = re.search(
        r"^(PYTEST_WORKERS)\s*\?=", MAKEFILE_PATH.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert variable_match is not None, (
        "the Makefile no longer declares an overridable PYTEST_WORKERS"
    )
    variable = variable_match.group(1)

    for path in _ci_definition_paths():
        text = path.read_text(encoding="utf-8")
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "nproc" in body, (
            f"{path.name}: never runs `nproc`, so the next run still will not record how many "
            f"cores it had"
        )
        assignments = re.findall(rf"{variable}=(\S+)", body)
        assert assignments, f"{path.name}: never sets {variable} from the derivation"
        for assigned in assignments:
            assert not re.fullmatch(r'"?\d+"?', assigned), (
                f"{path.name}: sets {variable}={assigned}, a literal — that is a second copy of "
                f"the Makefile's default, free to drift from both it and the hardware"
            )


#: The variable each CI definition declares to carry the worker-count lower bound.
_WORKER_FLOOR_VARIABLE = "PYTEST_WORKERS_FLOOR"


def _makefile_pytest_worker_default() -> str:
    """Return the Makefile's `PYTEST_WORKERS ?= N` default."""
    match = re.search(
        r"^PYTEST_WORKERS\s*\?=\s*(\d+)\s*$",
        MAKEFILE_PATH.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None, (
        "the Makefile no longer declares a numeric PYTEST_WORKERS default, so the CI "
        "floor has no authority to be compared against"
    )
    return match.group(1)


def _declared_worker_floors(node: Any) -> list[str]:
    """Return every `PYTEST_WORKERS_FLOOR` value declared anywhere in a parsed CI document."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == _WORKER_FLOOR_VARIABLE:
                found.append(str(value))
            else:
                found.extend(_declared_worker_floors(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_declared_worker_floors(item))
    return found


def _worker_derivation_script(path: Path) -> str:
    """Return the one shell block in the `make verify` job that names `nproc`."""
    job = _ci_jobs(path)[_make_verify_job_name(path)]
    blocks = [scalar for scalar in _yaml_scalars(job) if "nproc" in scalar]
    assert len(blocks) == 1, (
        f"{path.name}: expected exactly one shell block naming `nproc`, found {len(blocks)}"
    )
    return blocks[0]


def test_the_worker_count_is_floored_at_the_makefile_default_in_both_definitions() -> None:
    """Each definition sets the floor to the Makefile default and its derivation floors by it."""
    expected = _makefile_pytest_worker_default()

    for path in _ci_definition_paths():
        floors = _declared_worker_floors(_load_ci(path))
        assert floors == [expected], (
            f"{path.name}: declares {_WORKER_FLOOR_VARIABLE}={floors} but the Makefile's "
            f"PYTEST_WORKERS default is {expected}. The floor is the value the one completed "
            f"run measured; keeping the two equal is what makes the fallback path (an export "
            f"that never reaches the gate, leaving the Makefile default in force) land on the "
            f"same number. Change both deliberately or neither."
        )

        job = _ci_jobs(path)[_make_verify_job_name(path)]
        declared = _job_env(path, job).get(_WORKER_FLOOR_VARIABLE)
        assert declared is not None and str(declared) == expected, (
            f"{path.name}: {_WORKER_FLOOR_VARIABLE} is not in the effective variables of the "
            f"`make verify` job, so the derivation cannot read it — a floor declared where it "
            f"does not apply is a floor that does nothing"
        )

        derivation = _worker_derivation_script(path)
        assert _WORKER_FLOOR_VARIABLE in derivation, (
            f"{path.name}: the worker derivation never reads {_WORKER_FLOOR_VARIABLE}, so the "
            f"declared value is inert and the count is clamped rather than floored"
        )
        assert re.search(rf'-lt\s+"\$\{{{_WORKER_FLOOR_VARIABLE}\}}"', derivation), (
            f"{path.name}: the derivation does not compare the count with `-lt "
            f'"${{{_WORKER_FLOOR_VARIABLE}}}"`. A `-gt` there would make the value a CAP, '
            f"which is the shape this change exists to replace, and the two read almost "
            f"identically"
        )


def test_both_derivations_floor_the_count_and_never_override_a_supplied_value(
    tmp_path: Path,
) -> None:
    """Each derivation floors a 1-CPU count and leaves a supplied `PYTEST_WORKERS` unchanged."""
    bash = shutil.which("bash")
    assert bash is not None, "bash is absent, so neither CI definition's shell block can run"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    nproc = fake_bin / "nproc"
    nproc.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
    nproc.chmod(0o755)

    for path in _ci_definition_paths():
        floor = _makefile_pytest_worker_default()
        derivation = _worker_derivation_script(path)
        # The GitLab block exports the value; the GitHub block writes it to $GITHUB_ENV.
        script = tmp_path / f"{path.name}.derivation.sh"
        script.write_text(
            f'{derivation}\necho "RESULT=${{PYTEST_WORKERS:-<unset>}}"\n', encoding="utf-8"
        )

        for preset in (None, "2"):
            github_env = tmp_path / f"{path.name}.{preset}.github_env"
            github_env.write_text("", encoding="utf-8")
            env = {
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "GITHUB_ENV": str(github_env),
                _WORKER_FLOOR_VARIABLE: floor,
            }
            if preset is not None:
                env["PYTEST_WORKERS"] = preset
            completed = proc_run.run(
                [bash, str(script)],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
            assert completed.returncode == 0, (
                f"{path.name}: the derivation exited {completed.returncode}\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
            written = github_env.read_text(encoding="utf-8")
            exported = f"PYTEST_WORKERS={floor}"

            if preset is None:
                assert f"derived PYTEST_WORKERS={floor}" in completed.stdout, (
                    f"{path.name}: with 1 available CPU the derivation did not floor the count "
                    f"at {floor}:\n{completed.stdout}"
                )
                if "GITHUB_ENV" in derivation:
                    assert written.strip() == exported, (
                        f"{path.name}: derived the count but wrote {written!r} to $GITHUB_ENV"
                    )
                else:
                    assert f"RESULT={floor}" in completed.stdout, (
                        f"{path.name}: derived the count but did not export it:\n{completed.stdout}"
                    )
            else:
                assert "derived PYTEST_WORKERS" not in completed.stdout, (
                    f"{path.name}: overrode an explicitly supplied PYTEST_WORKERS={preset}. A "
                    f"pre-set value is the more specific, more recent intent and must win:\n"
                    f"{completed.stdout}"
                )
                assert f"RESULT={preset}" in completed.stdout, (
                    f"{path.name}: PYTEST_WORKERS={preset} did not survive the derivation:\n"
                    f"{completed.stdout}"
                )
                assert written == "", (
                    f"{path.name}: honoured PYTEST_WORKERS={preset} but still wrote {written!r} "
                    f"to $GITHUB_ENV, where precedence against an env:-declared value is not a "
                    f"property this repository controls"
                )


def test_non_vacuity_the_worker_floor_assertions_bite() -> None:
    """A drifted, removed, or capped worker floor in a copy of each definition is detected."""
    expected = _makefile_pytest_worker_default()
    bogus = str(int(expected) + 1)

    for path in _ci_definition_paths():
        text = path.read_text(encoding="utf-8")
        assert _declared_worker_floors(_load_ci(path)) == [expected], (
            f"{path.name}: fixture assumption broken before mutating"
        )

        drifted = text.replace(
            f'{_WORKER_FLOOR_VARIABLE}: "{expected}"', f'{_WORKER_FLOOR_VARIABLE}: "{bogus}"'
        )
        assert drifted != text, f"{path.name}: the floor is not declared in the form assumed"
        assert _declared_worker_floors(yaml.safe_load(drifted)) == [bogus], (
            f"{path.name}: a drifted floor is invisible to the parser, so the equality "
            f"assertion could not detect one"
        )

        removed = text.replace(f'    {_WORKER_FLOOR_VARIABLE}: "{expected}"\n', "")
        assert removed != text
        assert _declared_worker_floors(yaml.safe_load(removed)) == [], (
            f"{path.name}: removing the declaration left the parser still reporting it"
        )

        capped = text.replace(
            f'-lt "${{{_WORKER_FLOOR_VARIABLE}}}"', f'-gt "${{{_WORKER_FLOOR_VARIABLE}}}"'
        )
        assert capped != text, f"{path.name}: the floor comparison is not in the form assumed"
        mutant_job = _ci_jobs_from_doc(yaml.safe_load(capped))[_make_verify_job_name(path)]
        mutant_blocks = [scalar for scalar in _yaml_scalars(mutant_job) if "nproc" in scalar]
        assert len(mutant_blocks) == 1
        assert not re.search(rf'-lt\s+"\$\{{{_WORKER_FLOOR_VARIABLE}\}}"', mutant_blocks[0]), (
            f"{path.name}: turning the floor into a cap left the `-lt` assertion satisfied"
        )


#: The GitLab runner tags the `make verify` job requests.
_GITLAB_REQUIRED_TAGS = ("arch:arm64", "size:large")


def test_neither_ci_definition_inherits_its_runner() -> None:
    """The `make verify` job pins its runner: GitLab `tags:` exactly, GitHub `runs-on:`."""
    gitlab_job = _ci_jobs(GITLAB_CI_PATH)[_make_verify_job_name(GITLAB_CI_PATH)]
    tags = gitlab_job.get("tags")
    assert isinstance(tags, list) and tags, (
        f"{GITLAB_CI_PATH.name}: the `make verify` job declares no `tags:`, so it inherits the "
        f"project's default runner and its core count is whatever that box happens to have"
    )
    assert [str(tag) for tag in tags] == list(_GITLAB_REQUIRED_TAGS), (
        f"{GITLAB_CI_PATH.name}: `make verify` requests {tags}, expected "
        f"{list(_GITLAB_REQUIRED_TAGS)}"
    )

    github_job = _ci_jobs(WORKFLOW_PATH)[_make_verify_job_name(WORKFLOW_PATH)]
    assert github_job.get("runs-on"), (
        f"{WORKFLOW_PATH.name}: the `make verify` job declares no `runs-on:`, so its runner is "
        f"not pinned either"
    )


def test_non_vacuity_a_dropped_or_downgraded_runner_tag_is_detected() -> None:
    """A removed or downgraded GitLab runner tag in a copy of the file is detected."""
    text = GITLAB_CI_PATH.read_text(encoding="utf-8")
    job_name = _make_verify_job_name(GITLAB_CI_PATH)

    block = "  tags:\n" + "".join(f"    - {tag}\n" for tag in _GITLAB_REQUIRED_TAGS)
    assert block in text, (
        f"{GITLAB_CI_PATH.name}: the tags block is not in the form these mutants assume"
    )

    removed = _ci_jobs_from_doc(yaml.safe_load(text.replace(block, "")))[job_name]
    assert removed.get("tags") is None, (
        f"{GITLAB_CI_PATH.name}: removing the tags block left the parser still reporting tags"
    )

    size_tag = next(tag for tag in _GITLAB_REQUIRED_TAGS if tag.startswith("size:"))
    assert size_tag != "size:small", (
        "this mutant downgrades to `size:small`, so it cannot discriminate when that IS the required "
        "size — pick a different mutant before requiring the smallest box"
    )
    downgraded_text = text.replace(block, block.replace(size_tag, "size:small"))
    assert downgraded_text != text, f"{GITLAB_CI_PATH.name}: the size mutant changed nothing"
    downgraded = _ci_jobs_from_doc(yaml.safe_load(downgraded_text))[job_name]
    assert [str(tag) for tag in downgraded["tags"]] != list(_GITLAB_REQUIRED_TAGS), (
        f"{GITLAB_CI_PATH.name}: a downgraded runner size is invisible to the value comparison"
    )

    arch_tag = next(tag for tag in _GITLAB_REQUIRED_TAGS if tag.startswith("arch:"))
    other_arch = "arch:amd64" if arch_tag != "arch:amd64" else "arch:arm64"
    swapped_text = text.replace(block, block.replace(arch_tag, other_arch))
    assert swapped_text != text, f"{GITLAB_CI_PATH.name}: the arch mutant changed nothing"
    swapped = _ci_jobs_from_doc(yaml.safe_load(swapped_text))[job_name]
    assert [str(tag) for tag in swapped["tags"]] != list(_GITLAB_REQUIRED_TAGS), (
        f"{GITLAB_CI_PATH.name}: a swapped runner architecture is invisible to the value comparison"
    )


def test_non_vacuity_each_environment_property_is_detected_when_removed() -> None:
    """Removing each environment property from a copy of each definition is detected."""
    for path in _ci_definition_paths():
        text = path.read_text(encoding="utf-8")

        without_jq = yaml.safe_load(text.replace("make git jq zip", "make git zip"))
        job = _ci_jobs(path)[_make_verify_job_name(path)]
        mutated_job_sets = [
            pkgs
            for pkgs in _apt_install_sets(without_jq)
            if _APT_BASE_INSTALL_SENTINEL in pkgs and "socat" in pkgs
        ]
        assert mutated_job_sets, f"{path.name}: the jq mutant did not parse"
        assert all("jq" not in pkgs for pkgs in mutated_job_sets), (
            f"{path.name}: removing jq from the install line left the parser still reporting it"
        )
        assert "jq" in _base_apt_tool_set(path), f"{path.name}: fixture assumption broken"

        without_locale = yaml.safe_load(text.replace('LC_ALL: "C.UTF-8"', 'LC_ALL: "C"'))
        mutated_env = dict(
            without_locale.get("env") or without_locale.get("variables") or {},
        )
        assert "UTF-8" not in str(mutated_env.get("LC_ALL", "")).upper(), (
            f"{path.name}: the locale mutant did not take effect, so the locale assertion "
            f"cannot distinguish a UTF-8 locale from an ASCII one"
        )

        without_pin = text.replace(_SHELLCHECK_RELEASE_HOST, "example.invalid/not-shellcheck")
        assert _SHELLCHECK_RELEASE_HOST not in without_pin
        blocks = [
            scalar
            for scalar in _yaml_scalars(yaml.safe_load(without_pin))
            if _SHELLCHECK_RELEASE_HOST in scalar
        ]
        assert not blocks, f"{path.name}: the shellcheck-pin mutant left the block findable"

        apt_shellcheck = yaml.safe_load(
            text.replace("make git jq zip", "make git jq shellcheck zip")
        )
        assert any(_PINNED_SHELLCHECK_TOOL in pkgs for pkgs in _apt_install_sets(apt_shellcheck)), (
            f"{path.name}: the apt-shellcheck mutant is invisible to the parser"
        )
        assert all(
            _PINNED_SHELLCHECK_TOOL not in pkgs for pkgs in _apt_install_sets(_load_ci(path))
        ), f"{path.name}: fixture assumption broken — the real file apt-installs shellcheck"
        assert job, f"{path.name}: fixture assumption broken — no `make verify` job parsed"


# ---------------------------------------------------------------------------
# Install hygiene in both definitions: a non-interactive debconf, a pinned pip in every venv, an
# exact Node release on the relay runtime major, the full toolspec gate in the frontend job, and
# built zips before `make verify`.
# ---------------------------------------------------------------------------

_PIP_PIN_COMMAND = '.venv/bin/python -m pip install --no-input "pip==${PIP_VERSION}"'
_LOCK_INSTALL_FRAGMENT = "-r requirements.txt"
_NODE_RELEASE_HOST = "https://nodejs.org/dist/"
_TOOLSPEC_GATE = "scripts/check_toolspec_parity.py"
_ZIP_GATES = ("check_lambda_zip_imports", "check_relay_zip")

#: Tools the packaging scripts call that the GitLab image lacks: rsync (package_lambdas.sh) and
#: perl's shasum (package_relay.sh).
_PACKAGING_APT_TOOLS = frozenset({"rsync", "perl"})


def _gitlab_doc() -> dict[str, Any]:
    return _load_ci(GITLAB_CI_PATH)


def _job_steps(job: dict[str, Any]) -> list[str]:
    """Return a job's commands: GitLab before_script then script entries, or GitHub `run:` blocks."""
    if "steps" in job:
        return [str(step["run"]) for step in job.get("steps") or [] if "run" in step]
    steps: list[str] = []
    for key in ("before_script", "script"):
        steps.extend(str(entry) for entry in _flattened(list(job.get(key) or [])))
    return steps


def _job_lines(job: dict[str, Any]) -> list[str]:
    """Every shell line a job runs, in order.

    A GitHub step's `working-directory` (or the job's `defaults.run` one) becomes a `cd <dir> && `
    prefix, so `src/relay`'s `npm ci` reads the same in both definitions.
    """
    lines: list[str] = []
    if "steps" not in job:
        for step in _job_steps(job):
            lines.extend(line.strip() for line in step.splitlines() if line.strip())
        return lines
    default_dir = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
    for step in job.get("steps") or []:
        if "run" not in step:
            continue
        directory = step.get("working-directory", default_dir)
        prefix = f"cd {directory} && " if directory and directory != "." else ""
        lines.extend(
            prefix + line.strip() for line in str(step["run"]).splitlines() if line.strip()
        )
    return lines


def _first_index(steps: list[str], fragment: str) -> int:
    return next((i for i, step in enumerate(steps) if fragment in step), -1)


def _env_of(doc: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Return a job's effective variables from a parsed document of either definition."""
    key = "env" if "jobs" in doc else "variables"
    merged = dict(doc.get(key) or {})
    merged.update(job.get(key) or {})
    return merged


def _verify_job_of(path: Path, doc: dict[str, Any]) -> dict[str, Any]:
    job = _ci_jobs_from_doc(doc)[_make_verify_job_name(path)]
    assert isinstance(job, dict)
    return job


def _without_first_line(text: str, fragment: str) -> str:
    """Return `text` with the first line containing `fragment` removed."""
    lines = text.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if fragment in line)
    return "".join(lines[:index] + lines[index + 1 :])


def _debian_frontend_violations(doc: dict[str, Any]) -> list[str]:
    """Jobs that run apt-get without `DEBIAN_FRONTEND=noninteractive` among their variables."""
    return [
        name
        for name, job in _ci_jobs_from_doc(doc).items()
        if any("apt-get" in line for line in _job_lines(job))
        and str(_env_of(doc, job).get("DEBIAN_FRONTEND")) != "noninteractive"
    ]


def test_every_job_that_runs_apt_sets_a_noninteractive_debian_frontend() -> None:
    """Every apt-running job in either definition resolves `DEBIAN_FRONTEND=noninteractive`."""
    for path in _ci_definition_paths():
        doc = _load_ci(path)
        apt_jobs = [
            n
            for n, j in _ci_jobs_from_doc(doc).items()
            if any("apt-get" in s for s in _job_lines(j))
        ]
        assert apt_jobs, f"{path.name}: no job runs apt-get, so this check is vacuous"
        assert _debian_frontend_violations(doc) == [], path.name

        text = path.read_text(encoding="utf-8")
        mutant = yaml.safe_load(_without_first_line(text, 'DEBIAN_FRONTEND: "noninteractive"'))
        assert sorted(_debian_frontend_violations(mutant)) == sorted(apt_jobs), (
            f"{path.name}: removing DEBIAN_FRONTEND left a job still reported as non-interactive"
        )


def test_every_sudo_apt_get_carries_the_frontend_inline() -> None:
    """`sudo` resets the environment, so a sudo'd apt-get must set DEBIAN_FRONTEND itself."""
    for path in _ci_definition_paths():
        for scalar in _yaml_scalars(_load_ci(path)):
            for line in scalar.splitlines():
                if re.search(r"\bsudo\b.*\bapt-get\s+install\b", line):
                    assert "DEBIAN_FRONTEND=noninteractive" in line, (
                        f"{path.name}: {line.strip()!r}"
                    )


def _pip_pin_violations(doc: dict[str, Any]) -> list[str]:
    """Return a message for every job that installs the lock without first pinning pip."""
    problems: list[str] = []
    for name, job in _ci_jobs_from_doc(doc).items():
        lines = _job_lines(job)
        lock_at = _first_index(lines, _LOCK_INSTALL_FRAGMENT)
        if lock_at == -1:
            continue
        venv_at = _first_index(lines, "python3 -m venv .venv")
        pin_at = _first_index(lines, _PIP_PIN_COMMAND)
        if not (-1 < venv_at < pin_at < lock_at):
            problems.append(
                f"{name}: venv at {venv_at}, pip pin at {pin_at}, lock install at {lock_at}"
            )
        if not re.fullmatch(r"\d+\.\d+\.\d+", str(_env_of(doc, job).get("PIP_VERSION", ""))):
            problems.append(f"{name}: PIP_VERSION is not an exact X.Y.Z pin")
    return problems


def test_every_venv_installs_the_pinned_pip_before_the_lock() -> None:
    """In both definitions, each job that installs the lock pins pip right after the venv."""
    for path in _ci_definition_paths():
        doc = _load_ci(path)
        lock_jobs = [
            n
            for n, j in _ci_jobs_from_doc(doc).items()
            if _first_index(_job_lines(j), _LOCK_INSTALL_FRAGMENT) != -1
        ]
        assert len(lock_jobs) >= 2, f"{path.name}: expected both jobs to install the lock"
        assert _pip_pin_violations(doc) == [], path.name

        text = path.read_text(encoding="utf-8")
        dropped = _without_first_line(text, _PIP_PIN_COMMAND)
        assert len(_pip_pin_violations(yaml.safe_load(dropped))) == 1, (
            f"{path.name}: a dropped pip pin went unseen"
        )
        floating = text.replace('PIP_VERSION: "', 'PIP_VERSION: ">=', 1)
        assert _pip_pin_violations(yaml.safe_load(floating)), (
            f"{path.name}: a range PIP_VERSION went unseen"
        )


def _declared_pins(path: Path, name: str) -> set[str]:
    """Every value `name` is declared with anywhere in a definition."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == name:
                    found.add(str(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(_load_ci(path))
    return found


def test_both_definitions_pin_one_and_the_same_pip_and_node_version() -> None:
    """`PIP_VERSION` and `NODE_VERSION` each have one value, equal across the two definitions."""
    for name in ("PIP_VERSION", "NODE_VERSION"):
        pins = {path.name: _declared_pins(path, name) for path in _ci_definition_paths()}
        for file_name, values in pins.items():
            assert len(values) == 1, f"{file_name}: expected one {name}, got {sorted(values)}"
        assert len(set.union(*pins.values())) == 1, f"{name} differs between definitions: {pins}"


def test_no_ci_step_pipes_a_remote_script_into_a_shell() -> None:
    """No step downloads and executes a script (the nodesource setup pattern)."""
    for path in _ci_definition_paths():
        for scalar in _yaml_scalars(_load_ci(path)):
            assert "deb.nodesource.com" not in scalar, (
                f"{path.name}: nodesource is back: {scalar!r}"
            )
            assert not re.search(r"\|\s*(sudo\s+)?(ba)?sh\b", scalar), (
                f"{path.name}: pipes into a shell: {scalar!r}"
            )
            assert not re.search(r"\bbash\s+/tmp/", scalar), (
                f"{path.name}: executes a downloaded script: {scalar!r}"
            )


def _node_install_block(doc: dict[str, Any]) -> str:
    blocks = [
        step
        for step in _job_steps(_verify_job_of(GITLAB_CI_PATH, doc))
        if _NODE_RELEASE_HOST in step
    ]
    assert len(blocks) == 1, (
        f"expected one step downloading from {_NODE_RELEASE_HOST}, got {len(blocks)}"
    )
    return blocks[0]


def test_gitlab_node_is_a_digest_verified_release_on_the_relay_runtime_major() -> None:
    """The GitLab verify job installs an exact Node release, verified per arch before unpacking."""
    doc = _gitlab_doc()
    env = _env_of(doc, _verify_job_of(GITLAB_CI_PATH, doc))
    version = str(env.get("NODE_VERSION", ""))
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"NODE_VERSION={version!r} is not exact"
    assert int(version.split(".")[0]) == _terraform_runtime_major(), (
        f"NODE_VERSION {version} is not on the relay runtime major "
        f"nodejs{_terraform_runtime_major()}.x"
    )
    for arch in ("X64", "ARM64"):
        digest = str(env.get(f"NODE_SHA256_{arch}", ""))
        assert re.fullmatch(r"[0-9a-f]{64}", digest), f"NODE_SHA256_{arch} is not a literal sha256"

    block = _node_install_block(doc)
    verify_at = block.find("sha256sum --check --strict")
    unpack_at = block.find("tar -xJf")
    run_at = block.find("node --version")
    assert -1 < verify_at < unpack_at < run_at, (
        "Node is unpacked or run before its digest is verified"
    )
    assert "SHASUMS256" not in block, "the digest is fetched beside the asset rather than pinned"
    assert "command -v node" in block, "the install never asserts which node PATH resolves"
    assert '"v${NODE_VERSION}"' in block, "the install never compares node --version with the pin"
    assert "apt-get" not in block, "Node comes from the distribution, not the pinned release"


def test_github_node_is_the_exact_pinned_release_and_is_asserted_on_path() -> None:
    """setup-node gets the exact NODE_VERSION via the derivation, and a later step asserts it."""
    steps = list(_verify_job().get("steps", []))
    derivation = _node_derivation_step()
    assert "version=${NODE_VERSION}" in str(derivation.get("run", "")), (
        "the derivation step outputs something other than the exact NODE_VERSION, so setup-node "
        "resolves a floating major"
    )
    setup_at = next(
        i for i, step in enumerate(steps) if _NODE_SETUP_ACTION in str(step.get("uses", ""))
    )
    asserts = [
        i
        for i, step in enumerate(steps)
        if '"v${NODE_VERSION}"' in str(step.get("run", ""))
        and "node --version" in str(step.get("run", ""))
    ]
    assert asserts and asserts[0] > setup_at, "no step after setup-node asserts the pinned Node"


def _node_major_check_script(path: Path) -> str:
    """The shell that derives the Node major and asserts NODE_VERSION is on it, per definition."""
    if path == GITLAB_CI_PATH:
        block = _node_install_block(_gitlab_doc())
        return block[: block.index('case "$(uname -m)"')]
    return str(_node_derivation_step().get("run", ""))


def test_a_node_pin_off_the_relay_runtime_major_is_refused(tmp_path: Path) -> None:
    """Each definition's own derivation accepts the pinned version and exits 1 on another major."""
    bash = shutil.which("bash")
    assert bash is not None
    for path in _ci_definition_paths():
        (pinned,) = _declared_pins(path, "NODE_VERSION")
        script = tmp_path / f"{path.name}.node-major.sh"
        script.write_text(
            f"set -euo pipefail\n{_node_major_check_script(path)}\necho MAJOR-OK\n",
            encoding="utf-8",
        )
        for version, expect_ok in (
            (pinned, True),
            (f"{_terraform_runtime_major() + 2}.0.0", False),
        ):
            github_output = tmp_path / f"{path.name}.{version}.out"
            github_output.write_text("", encoding="utf-8")
            completed = proc_run.run(
                [bash, str(script)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "NODE_VERSION": version,
                    "GITHUB_OUTPUT": str(github_output),
                },
                timeout=60,
                check=False,
            )
            if expect_ok:
                assert completed.returncode == 0 and "MAJOR-OK" in completed.stdout, (
                    f"{path.name}: the pinned {version} was refused:\n{completed.stderr}"
                )
                if path == WORKFLOW_PATH:
                    assert github_output.read_text(encoding="utf-8").strip() == f"version={version}"
            else:
                assert completed.returncode == 1 and "MAJOR-OK" not in completed.stdout, (
                    f"{path.name}: NODE_VERSION={version} passed the major assertion:\n"
                    f"{completed.stdout}\n{completed.stderr}"
                )
                assert "is not on the derived major" in completed.stderr


def _frontend_toolspec_violations(doc: dict[str, Any]) -> list[str]:
    """The emitted-css job must run the full toolspec gate after regenerating the types."""
    variable = _emitted_css_require_variable()
    jobs = [j for j in _ci_jobs_from_doc(doc).values() if str(_env_of(doc, j).get(variable)) == "1"]
    if len(jobs) != 1:
        return [f"expected one frontend job, found {len(jobs)}"]
    lines = _job_lines(jobs[0])
    gen_at = _first_index(lines, "npm run gen:types")
    gate_at = _first_index(lines, _TOOLSPEC_GATE)
    problems: list[str] = []
    if not -1 < gen_at < gate_at:
        problems.append(f"gen:types at {gen_at}, toolspec gate at {gate_at}")
    if gate_at != -1 and "--skip-frontend" in lines[gate_at]:
        problems.append("the frontend job defers the very consumer it alone can check")
    if _first_index(lines, "npm ci") == -1 or _first_index(lines, _PIP_PIN_COMMAND) == -1:
        problems.append("the frontend job lacks the npm or Python install the gate needs")
    return problems


def test_the_frontend_job_runs_the_full_toolspec_gate() -> None:
    """Some CI job byte-diffs the frontend toolspec consumer: the frontend job, without the skip."""
    run_gates = RUN_GATES_PATH.read_text(encoding="utf-8")
    assert '"--skip-frontend"' in run_gates, (
        "run_gates no longer defers the frontend half, so this job is no longer its only checker"
    )
    for path in _ci_definition_paths():
        assert _frontend_toolspec_violations(_load_ci(path)) == [], path.name

        text = path.read_text(encoding="utf-8")
        removed = yaml.safe_load(_without_first_line(text, _TOOLSPEC_GATE))
        assert _frontend_toolspec_violations(removed), f"{path.name}: a removed gate went unseen"
        skipped = yaml.safe_load(text.replace(_TOOLSPEC_GATE, f"{_TOOLSPEC_GATE} --skip-frontend"))
        assert _frontend_toolspec_violations(skipped), f"{path.name}: --skip-frontend went unseen"


def _zip_build_violations(path: Path, doc: dict[str, Any]) -> list[str]:
    """The verify job builds both zip sets, and asserts the relay zip, before `make verify`."""
    lines = _job_lines(_verify_job_of(path, doc))
    tools_at = next(
        (
            i
            for i, line in enumerate(lines)
            if any(pkgs >= _PACKAGING_APT_TOOLS for pkgs in _apt_install_sets(line))
        ),
        -1,
    )
    prerequisites = [
        _first_index(lines, "python3 -m venv .venv"),
        _first_index(lines, "cd src/relay && npm ci"),
        tools_at,
    ]
    builds = [
        _first_index(lines, "scripts/package_lambdas.sh"),
        _first_index(lines, "scripts/package_relay.sh"),
    ]
    relay_check_at = _first_index(lines, "scripts/check_relay_zip.py --require-zip")
    verify_at = next((i for i, line in enumerate(lines) if re.fullmatch(r"make verify", line)), -1)
    everything = [*prerequisites, *builds, relay_check_at, verify_at]
    if -1 in everything or not (
        max(prerequisites) < min(builds) and max(builds) < relay_check_at < verify_at
    ):
        return [
            f"venv, relay npm ci, packaging tools {prerequisites}; builds {builds}; "
            f"relay check {relay_check_at}; make verify {verify_at}"
        ]
    return []


def test_the_verify_job_builds_the_zips_its_gates_verify() -> None:
    """In both definitions both zip gates have a subject, so they PASS rather than SKIP."""
    for gate in _ZIP_GATES:
        assert f'"{gate}"' in RUN_GATES_PATH.read_text(encoding="utf-8"), (
            f"{gate} is not registered"
        )
    for path in _ci_definition_paths():
        assert _zip_build_violations(path, _load_ci(path)) == [], path.name

        text = path.read_text(encoding="utf-8")
        for fragment in (
            "scripts/package_lambdas.sh\n",
            "scripts/package_relay.sh\n",
            "scripts/check_relay_zip.py --require-zip\n",
        ):
            mutant = yaml.safe_load(_without_first_line(text, fragment))
            assert _zip_build_violations(path, mutant), f"{path.name}: dropping {fragment!r} unseen"
        for tool in sorted(_PACKAGING_APT_TOOLS):
            rest = " ".join(sorted(_PACKAGING_APT_TOOLS - {tool}))
            without = text.replace(" rsync perl\n", f" {rest}\n")
            assert without != text
            assert _zip_build_violations(path, yaml.safe_load(without)), (
                f"{path.name}: dropping {tool} went unseen"
            )


# ---------------------------------------------------------------------------
# Every digest-pinned release install verifies the very file it then unpacks, and a wrong digest
# stops the block before `tar` runs. The block is executed against a stand-in artefact, with `curl`,
# `tar` and `uname` replaced, so a swallowed or misdirected check is observed rather than read.
# ---------------------------------------------------------------------------

#: `(definition, release host)` for each install verified against a pinned digest.
_PINNED_RELEASE_INSTALLS = (
    (GITLAB_CI_PATH, GITLEAKS_RELEASE_HOST),
    (GITLAB_CI_PATH, _SHELLCHECK_RELEASE_HOST),
    (GITLAB_CI_PATH, _NODE_RELEASE_HOST),
    (WORKFLOW_PATH, GITLEAKS_RELEASE_HOST),
    (WORKFLOW_PATH, _SHELLCHECK_RELEASE_HOST),
)

_VERIFIED_PATH_RE = re.compile(r'echo "\$\{?\w+\}?  ([^"]+)" \| sha256sum\b')
_EXTRACTED_PATH_RE = re.compile(r'\btar -x\w*f "([^"]+)"')
_DOWNLOADED_PATH_RE = re.compile(r'\bcurl\b[^\n]*?-o "([^"]+)"')
_STAND_IN_ARTEFACT = "stand-in release artefact\n"


def _pinned_install(
    path: Path, host: str, doc: dict[str, Any] | None = None
) -> tuple[str, dict[str, str]]:
    """Return the shell block installing from `host` and the variables it runs with."""
    doc = _load_ci(path) if doc is None else doc
    found: list[tuple[str, dict[str, str]]] = []
    for job in _ci_jobs_from_doc(doc).values():
        job_env = {str(k): str(v) for k, v in _env_of(doc, job).items()}
        if "steps" in job:
            for step in job.get("steps") or []:
                if host in str(step.get("run", "")):
                    env = dict(job_env)
                    env.update({str(k): str(v) for k, v in (step.get("env") or {}).items()})
                    found.append((str(step["run"]), env))
        else:
            found.extend((step, job_env) for step in _job_steps(job) if host in step)
    assert len(found) == 1, (
        f"{path.name}: expected one block installing from {host}, got {len(found)}"
    )
    return found[0]


def _logical_lines(script: str) -> list[str]:
    """Join backslash-continued lines, so one command is one entry."""
    joined: list[str] = []
    pending = ""
    for line in script.splitlines():
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        joined.append(pending + line)
        pending = ""
    if pending:
        joined.append(pending)
    return joined


def _digest_check_problems(script: str) -> list[str]:
    """Static shape of the digest check: one strict check, unswallowed, on the unpacked file."""
    lines = _logical_lines(script)
    checks = [i for i, line in enumerate(lines) if "sha256sum" in line]
    if len(checks) != 1:
        return [f"expected one sha256sum line, found {len(checks)}"]
    line = lines[checks[0]]
    problems: list[str] = []
    if not re.search(r"\s(--check|-c)\s", line) or "--strict" not in line:
        problems.append(f"the check is not `sha256sum --check --strict`: {line.strip()!r}")
    if re.search(r"\|\||;\s*(true|:)\b|\bset \+e\b", line):
        problems.append(f"the check's failure is swallowed: {line.strip()!r}")
    verified = _VERIFIED_PATH_RE.search(line)
    extract_at = next((i for i, text in enumerate(lines) if _EXTRACTED_PATH_RE.search(text)), -1)
    if verified is None or extract_at == -1:
        return [*problems, "cannot read the verified or the extracted path"]
    extracted = _EXTRACTED_PATH_RE.search(lines[extract_at])
    downloaded = next(
        (m.group(1) for text in lines if (m := _DOWNLOADED_PATH_RE.search(text))), None
    )
    assert extracted is not None
    if not checks[0] < extract_at:
        problems.append("the archive is extracted before its digest is checked")
    if len({verified.group(1), extracted.group(1), downloaded}) != 1:
        problems.append(
            f"downloaded {downloaded!r}, verified {verified.group(1)!r}, "
            f"extracted {extracted.group(1)!r}: not one file"
        )
    return problems


def _run_install_prefix(
    path: Path,
    script: str,
    env: dict[str, str],
    tmp_path: Path,
    digest: str,
    *,
    machine: str = "x86_64",
    digest_overrides: dict[str, str] | None = None,
) -> tuple[int, str, str | None, str | None]:
    """Run `script` up to and including its `tar`; return rc, stderr, curl's -o path, tar's args.

    Every `*_SHA256*` variable is set to `digest`, then `digest_overrides` apply; `uname -m`
    reports `machine`. The URL curl was asked for is written to `tmp_path / "curl.url"`.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    curl_log, curl_url, tar_log = tmp_path / "curl.log", tmp_path / "curl.url", tmp_path / "tar.log"
    for log in (curl_log, curl_url, tar_log):
        log.unlink(missing_ok=True)
    stubs = {
        "curl": (
            '#!/bin/sh\nout=""\nurl=""\nwhile [ $# -gt 0 ]; do\n'
            '  case "$1" in -o) out="$2"; shift ;; https://*) url="$1" ;; esac\n  shift\ndone\n'
            f'printf "%s" "$out" > "{curl_log}"\nprintf "%s" "$url" > "{curl_url}"\n'
            f'printf "{_STAND_IN_ARTEFACT.strip()}\\n" > "$out"\n'
        ),
        "tar": f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tar_log}"\n',
        "uname": f"#!/bin/sh\necho {machine}\n",
    }
    for name, body in stubs.items():
        stub = fake_bin / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)

    lines = _logical_lines(script.replace("/tmp/", f"{tmp_path}/"))  # nosec B108  # test rewrites /tmp/ to tmp_path
    end = next(i for i, line in enumerate(lines) if _EXTRACTED_PATH_RE.search(line))
    prefix = "\n".join(lines[: end + 1])
    runner = tmp_path / "install-prefix.sh"
    # GitLab runs every entry under the job's leading `set -euo pipefail`; GitHub uses `bash -e`.
    preamble = "set -euo pipefail\n" if path == GITLAB_CI_PATH else ""
    runner.write_text(f"{preamble}{prefix}\n", encoding="utf-8")

    run_env = {k: (digest if "_SHA256" in k else v) for k, v in env.items()}
    run_env.update(digest_overrides or {})
    run_env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "RUNNER_TEMP": str(tmp_path),
        }
    )
    bash = shutil.which("bash")
    assert bash is not None
    completed = proc_run.run(
        [bash, "-e", str(runner)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=run_env,
        timeout=60,
        check=False,
    )
    downloaded = curl_log.read_text(encoding="utf-8") if curl_log.exists() else None
    tar_args = tar_log.read_text(encoding="utf-8") if tar_log.exists() else None
    return completed.returncode, completed.stderr, downloaded, tar_args


def _effective_digest_problems(
    path: Path, script: str, env: dict[str, str], tmp_path: Path
) -> list[str]:
    """A wrong digest stops the block before `tar`; the right one unpacks the downloaded file."""
    problems: list[str] = []
    genuine = hashlib.sha256(_STAND_IN_ARTEFACT.encode()).hexdigest()
    wrong = hashlib.sha256(b"tampered").hexdigest()

    rc, stderr, _, tar_args = _run_install_prefix(path, script, env, tmp_path, wrong)
    if rc == 0 or tar_args is not None:
        problems.append(f"a wrong digest reached tar (rc={rc}): {stderr.strip()[-200:]}")
    rc, stderr, downloaded, tar_args = _run_install_prefix(path, script, env, tmp_path, genuine)
    if rc != 0 or tar_args is None or downloaded is None:
        problems.append(f"the matching digest did not reach tar (rc={rc}): {stderr.strip()[-200:]}")
    elif downloaded not in tar_args.splitlines():
        problems.append(
            f"tar unpacked {tar_args.splitlines()} rather than the download {downloaded}"
        )
    return problems


@pytest.mark.parametrize(
    ("path", "host"),
    _PINNED_RELEASE_INSTALLS,
    ids=[
        f"{p.name}:{h.split('/')[-3] if 'github' in h else 'node'}"
        for p, h in _PINNED_RELEASE_INSTALLS
    ],
)
def test_each_pinned_release_install_verifies_the_file_it_unpacks(
    path: Path, host: str, tmp_path: Path
) -> None:
    """Static shape plus an executed run: a wrong digest never reaches tar, the right one does."""
    script, env = _pinned_install(path, host)
    assert _digest_check_problems(script) == [], path.name
    assert _effective_digest_problems(path, script, env, tmp_path) == [], path.name


#: Mutants of the digest-check line, each disabling the control; every one must be caught.
_DIGEST_CHECK_MUTANTS: dict[str, Callable[[str], str]] = {
    "swallowed with || true": lambda line: line.rstrip() + " || true",
    "swallowed with || :": lambda line: line.rstrip() + " || :",
    "a different file verified": lambda line: re.sub(
        r'  [^"]+"', '  /tmp/other.tar.xz"', line, count=1
    ),
    "no --strict": lambda line: line.replace(" --strict", ""),
    "no --check": lambda line: line.replace("--check ", "").replace("-c ", ""),
}


@pytest.mark.parametrize("mutant", sorted(_DIGEST_CHECK_MUTANTS))
@pytest.mark.parametrize(
    ("path", "host"),
    _PINNED_RELEASE_INSTALLS,
    ids=[
        f"{p.name}:{h.split('/')[-3] if 'github' in h else 'node'}"
        for p, h in _PINNED_RELEASE_INSTALLS
    ],
)
def test_non_vacuity_a_disabled_digest_check_is_caught(
    path: Path, host: str, mutant: str, tmp_path: Path
) -> None:
    """Each mutant of each install's digest line is reported, statically or by the executed run."""
    text = path.read_text(encoding="utf-8")
    original, _ = _pinned_install(path, host)
    line = next(line for line in original.splitlines() if "sha256sum" in line)
    mutated_line = _DIGEST_CHECK_MUTANTS[mutant](line)
    assert mutated_line != line, f"{mutant}: the mutant changed nothing"
    assert text.count(line) == 1, f"{path.name}: the digest line is not unique in the file"
    script, env = _pinned_install(path, host, yaml.safe_load(text.replace(line, mutated_line)))
    caught = _digest_check_problems(script) + _effective_digest_problems(
        path, script, env, tmp_path
    )
    assert caught, f"{path.name} {host}: the {mutant!r} mutant went unseen"


#: GitLab installs that select their asset by `uname -m`: host -> {machine: (digest var, asset)}.
_GITLAB_PER_ARCH_INSTALLS: dict[str, dict[str, tuple[str, str]]] = {
    GITLEAKS_RELEASE_HOST: {
        "x86_64": ("GITLEAKS_SHA256_X64", "_linux_x64.tar.gz"),
        "aarch64": ("GITLEAKS_SHA256_ARM64", "_linux_arm64.tar.gz"),
    },
    _SHELLCHECK_RELEASE_HOST: {
        "x86_64": ("SHELLCHECK_SHA256_X86_64", ".linux.x86_64.tar.xz"),
        "aarch64": ("SHELLCHECK_SHA256_AARCH64", ".linux.aarch64.tar.xz"),
    },
    _NODE_RELEASE_HOST: {
        "x86_64": ("NODE_SHA256_X64", "-linux-x64.tar.xz"),
        "aarch64": ("NODE_SHA256_ARM64", "-linux-arm64.tar.xz"),
    },
}


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
@pytest.mark.parametrize(
    "host",
    sorted(_GITLAB_PER_ARCH_INSTALLS),
    ids=lambda h: h.split("/")[-3] if "github" in h else "node",
)
def test_each_gitlab_install_fetches_and_verifies_the_asset_of_the_runner_arch(
    host: str, machine: str, tmp_path: Path
) -> None:
    """On each runner arch the install fetches that arch's asset and accepts only that arch's digest."""
    script, env = _pinned_install(GITLAB_CI_PATH, host)
    if host == _NODE_RELEASE_HOST:
        script = script[script.index('case "$(uname -m)"') :]
    own_var, asset_suffix = _GITLAB_PER_ARCH_INSTALLS[host][machine]
    other_var = next(v for m, (v, _) in _GITLAB_PER_ARCH_INSTALLS[host].items() if m != machine)
    assert {own_var, other_var} <= set(env), (
        f"{host}: the job does not define {own_var}/{other_var}"
    )
    genuine = hashlib.sha256(_STAND_IN_ARTEFACT.encode()).hexdigest()
    wrong = hashlib.sha256(b"tampered").hexdigest()

    rc, stderr, downloaded, tar_args = _run_install_prefix(
        GITLAB_CI_PATH,
        script,
        env,
        tmp_path,
        wrong,
        machine=machine,
        digest_overrides={own_var: genuine},
    )
    url = (tmp_path / "curl.url").read_text(encoding="utf-8")
    assert rc == 0 and tar_args is not None, f"{machine}: own digest rejected: {stderr[-200:]}"
    assert url.endswith(asset_suffix), f"{machine}: fetched {url!r}, not a {asset_suffix} asset"
    assert downloaded is not None and downloaded in tar_args.splitlines()

    rc, stderr, _, tar_args = _run_install_prefix(
        GITLAB_CI_PATH,
        script,
        env,
        tmp_path,
        wrong,
        machine=machine,
        digest_overrides={other_var: genuine},
    )
    assert rc != 0 and tar_args is None, (
        f"{machine}: the other architecture's digest ({other_var}) was accepted"
    )


def test_an_unpinned_runner_arch_stops_each_gitlab_install(tmp_path: Path) -> None:
    """A runner arch with no pinned digest fails the install before anything is fetched."""
    for host in sorted(_GITLAB_PER_ARCH_INSTALLS):
        script, env = _pinned_install(GITLAB_CI_PATH, host)
        if host == _NODE_RELEASE_HOST:
            script = script[script.index('case "$(uname -m)"') :]
        genuine = hashlib.sha256(_STAND_IN_ARTEFACT.encode()).hexdigest()
        rc, stderr, downloaded, tar_args = _run_install_prefix(
            GITLAB_CI_PATH, script, env, tmp_path, genuine, machine="riscv64"
        )
        assert rc != 0 and downloaded is None and tar_args is None, f"{host}: rc={rc}"
        assert "no pinned" in stderr, f"{host}: {stderr[-200:]}"


def test_gitlab_gitleaks_pins_the_github_release_and_a_digest_per_arch() -> None:
    """GitLab's gitleaks pin is GitHub's version and x64 digest, plus a distinct arm64 digest."""
    gitlab_env = _env_of(_gitlab_doc(), _verify_job_of(GITLAB_CI_PATH, _gitlab_doc()))
    github_env = _gitleaks_install_step().get("env", {})
    assert str(gitlab_env.get("GITLEAKS_VERSION")) == str(github_env.get("GITLEAKS_VERSION"))
    assert "GITLEAKS_SHA256" not in gitlab_env, "an arch-blind gitleaks digest is still defined"
    x64 = str(gitlab_env.get("GITLEAKS_SHA256_X64", ""))
    arm64 = str(gitlab_env.get("GITLEAKS_SHA256_ARM64", ""))
    assert x64 == str(github_env.get("GITLEAKS_SHA256")), (
        "the two definitions pin different x64 assets"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", arm64), f"GITLEAKS_SHA256_ARM64={arm64!r} is not a sha256"
    assert arm64 != x64, "the arm64 digest is the x64 digest"


def test_non_vacuity_extracting_before_verifying_is_caught(tmp_path: Path) -> None:
    """Moving the Node tar ahead of its digest check is reported."""
    script, env = _pinned_install(GITLAB_CI_PATH, _NODE_RELEASE_HOST)
    lines = _logical_lines(script)
    check_at = next(i for i, line in enumerate(lines) if "sha256sum" in line)
    tar_at = next(i for i, line in enumerate(lines) if _EXTRACTED_PATH_RE.search(line))
    lines.insert(check_at, lines.pop(tar_at))
    reordered = "\n".join(lines)
    assert _digest_check_problems(reordered) + _effective_digest_problems(
        GITLAB_CI_PATH, reordered, env, tmp_path
    )


# ---------------------------------------------------------------------------
# The browser journeys' static gates are wired into `make verify` and both CI definitions.
# ---------------------------------------------------------------------------

JOURNEYS_DIR = "tests/e2e/browser"
JOURNEYS_MAKE_TARGET = "test-journeys-static"
JOURNEYS_REQUIRE_ENV_VAR = "CRM_REQUIRE_JOURNEY_STATIC"
_JOURNEYS_GITLAB_INSTALL = f"(cd {JOURNEYS_DIR} && npm ci"


def _journeys_install_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in job.get("steps", [])
        if step.get("working-directory") == JOURNEYS_DIR and "npm ci" in str(step.get("run", ""))
    ]


def _gitlab_installs_journeys(doc: dict[str, Any]) -> bool:
    """Whether GitLab's `make verify` job (or the pipeline `before_script` it inherits) runs `npm ci`
    in the journeys project."""
    job = _ci_jobs_from_doc(doc)[_make_verify_job_name(GITLAB_CI_PATH)]
    scalars = list(_yaml_scalars(job)) + list(_yaml_scalars(doc.get("before_script", [])))
    return any(_JOURNEYS_GITLAB_INSTALL in scalar for scalar in scalars)


def test_the_journey_static_gates_are_wired_into_make_verify() -> None:
    """The target exists, runs both static gates with the pinned local binaries, and `verify` runs it."""
    recipes = _makefile_recipes()
    assert JOURNEYS_MAKE_TARGET in recipes and recipes[JOURNEYS_MAKE_TARGET]
    assert JOURNEYS_MAKE_TARGET in _verify_sub_targets()
    recipe_text = "\n".join(recipes[JOURNEYS_MAKE_TARGET])
    assert "./node_modules/.bin/tsc --noEmit" in recipe_text
    assert "./node_modules/.bin/playwright test --list" in recipe_text
    assert "npx" not in recipe_text, "npx resolves an unrelated public `tsc` package"


def test_the_journey_requirement_and_its_ci_provision_agree_in_both_definitions() -> None:
    """`make verify` exports the journeys require flag exactly when both CI files run `npm ci` there."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    exported = JOURNEYS_REQUIRE_ENV_VAR in _verify_target_exported_requirements(makefile_text)
    github = bool(_journeys_install_steps(_verify_job())) and _ci_satisfies_relay_test_requirement(
        _verify_job()
    )
    gitlab = _gitlab_installs_journeys(_load_ci(GITLAB_CI_PATH))
    assert exported == github == gitlab, (
        f"{JOURNEYS_REQUIRE_ENV_VAR} exported={exported}, GitHub provisions={github}, "
        f"GitLab provisions={gitlab}: exported-but-unprovisioned is an unexplained CI failure, "
        "provisioned-but-unexported a silent skip"
    )
    assert exported, "the journeys' static gates are not required by `make verify`"


def test_non_vacuity_dropping_the_journey_install_is_detected() -> None:
    """Removing the install from a copy of either definition reads as unprovisioned."""
    job = _verify_job()
    assert _journeys_install_steps(job), "fixture assumption broken"
    stripped_job = {
        **job,
        "steps": [s for s in job["steps"] if s not in _journeys_install_steps(job)],
    }
    assert not _journeys_install_steps(stripped_job)

    text = GITLAB_CI_PATH.read_text(encoding="utf-8")
    assert _gitlab_installs_journeys(yaml.safe_load(text))
    stripped = "\n".join(line for line in text.splitlines() if _JOURNEYS_GITLAB_INSTALL not in line)
    assert not _gitlab_installs_journeys(yaml.safe_load(stripped))


def test_the_journeys_install_from_the_lockfile_and_never_resolve_versions() -> None:
    """The journeys project has a lockfile, and every CI install of it is `npm ci`."""
    assert (REPO_ROOT / JOURNEYS_DIR / "package-lock.json").is_file()
    for step in _journeys_install_steps(_verify_job()):
        assert not re.search(r"npm install\b", str(step.get("run", "")))
    gitlab_text = GITLAB_CI_PATH.read_text(encoding="utf-8")
    assert not re.search(rf"cd {re.escape(JOURNEYS_DIR)} && npm install\b", gitlab_text)


# ---------------------------------------------------------------------------
# The TypeScript-executing contracts run, and fail rather than skip, in the frontend CI job only,
# on the frontend's pinned Node. `make verify` does not export the variable.
# ---------------------------------------------------------------------------

TS_CONTRACTS_REQUIRE_ENV_VAR = "CRM_REQUIRE_TS_CONTRACTS"
TS_CONTRACT_FILES = (
    "tests/integration/test_envelope_groups_contract.py",
    "tests/integration/test_eval_envelopes_contract.py",
)
_FRONTEND_JOB = "frontend"


def _job_scalars(job: dict[str, Any]) -> list[str]:
    return list(_yaml_scalars(job))


def _sets_ts_contracts(path: Path, job: dict[str, Any]) -> bool:
    return str(_job_env(path, job).get(TS_CONTRACTS_REQUIRE_ENV_VAR)) == "1"


def _runs_every_ts_contract(job: dict[str, Any]) -> bool:
    return any(
        "pytest" in scalar and all(name in scalar for name in TS_CONTRACT_FILES)
        for scalar in _job_scalars(job)
    )


def _ts_contract_violations(path: Path, jobs: dict[str, Any]) -> list[str]:
    """Why `jobs` (one CI definition) does not run the contracts in the frontend job only."""
    problems: list[str] = []
    frontend = jobs.get(_FRONTEND_JOB)
    if not isinstance(frontend, dict):
        return [f"{path.name}: no `{_FRONTEND_JOB}` job"]
    if not _sets_ts_contracts(path, frontend):
        problems.append(
            f"{path.name}: the frontend job does not set {TS_CONTRACTS_REQUIRE_ENV_VAR}=1"
        )
    if not _runs_every_ts_contract(frontend):
        problems.append(f"{path.name}: the frontend job does not run both TS contract files")
    for name, job in jobs.items():
        if name != _FRONTEND_JOB and _sets_ts_contracts(path, job):
            problems.append(
                f"{path.name}: job {name!r} (not on the frontend's Node) sets the variable"
            )
    return problems


def test_the_ts_contracts_variable_has_a_reader_under_tests() -> None:
    """Every contract file the frontend job runs exists and reads the variable."""
    for name in TS_CONTRACT_FILES:
        assert TS_CONTRACTS_REQUIRE_ENV_VAR in (REPO_ROOT / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("path", [WORKFLOW_PATH, GITLAB_CI_PATH], ids=lambda p: p.name)
def test_only_the_frontend_job_runs_the_ts_contracts_and_requires_them(path: Path) -> None:
    """The frontend job sets the variable and runs both files; no Node-22 job sets it."""
    assert _ts_contract_violations(path, _ci_jobs(path)) == []


def test_make_verify_does_not_export_the_ts_contracts_variable() -> None:
    """The verify jobs run Node 22, so `make verify` must not turn the skip into a failure."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    assert TS_CONTRACTS_REQUIRE_ENV_VAR not in _verify_target_exported_requirements(makefile_text)


@pytest.mark.parametrize("path", [WORKFLOW_PATH, GITLAB_CI_PATH], ids=lambda p: p.name)
def test_non_vacuity_each_ts_contract_rule_is_detected(path: Path) -> None:
    """Dropping the variable, dropping a file, or setting it in the verify job each reads as a violation."""
    jobs = _ci_jobs(path)
    assert _ts_contract_violations(path, jobs) == [], "fixture assumption broken"
    env_key = "env" if path == WORKFLOW_PATH else "variables"

    no_var = {**jobs, _FRONTEND_JOB: {**jobs[_FRONTEND_JOB], env_key: {}}}
    assert _ts_contract_violations(path, no_var)

    text = json.dumps(jobs[_FRONTEND_JOB]).replace(
        TS_CONTRACT_FILES[1], "tests/integration/other.py"
    )
    one_file = {**jobs, _FRONTEND_JOB: json.loads(text)}
    assert _ts_contract_violations(path, one_file)

    verify_name = _make_verify_job_name(path)
    verify_job = jobs[verify_name]
    leaked = {
        **jobs,
        verify_name: {
            **verify_job,
            env_key: {**(verify_job.get(env_key) or {}), TS_CONTRACTS_REQUIRE_ENV_VAR: "1"},
        },
    }
    assert _ts_contract_violations(path, leaked)


# ---------------------------------------------------------------------------
# The wire-tuning fixture vitest writes is diffed against the committed copy the Python
# wire-contract tests read, after the frontend unit run, in both CI frontend jobs.
# ---------------------------------------------------------------------------

WIRE_TUNING_FIXTURE = "tests/integration/fixtures/wire_tuning.json"
_WIRE_TUNING_DIFF = f"git diff --exit-code {WIRE_TUNING_FIXTURE}"
_FRONTEND_UNIT_RUN = "npm run test"


def _job_commands(job: dict[str, Any]) -> list[str]:
    """The shell commands of one job in order: GitHub step `run`s, or the GitLab `script`."""
    steps = job.get("steps")
    if isinstance(steps, list):
        return [str(s["run"]) for s in steps if isinstance(s, dict) and "run" in s]
    return [str(line) for line in job.get("script") or []]


def _wire_tuning_violations(path: Path, jobs: dict[str, Any]) -> list[str]:
    """Why the frontend job does not diff the fixture after its unit run."""
    frontend = jobs.get(_FRONTEND_JOB)
    if not isinstance(frontend, dict):
        return [f"{path.name}: no `{_FRONTEND_JOB}` job"]
    commands = _job_commands(frontend)
    unit = [i for i, c in enumerate(commands) if _FRONTEND_UNIT_RUN in c]
    diff = [i for i, c in enumerate(commands) if _WIRE_TUNING_DIFF in c]
    if not unit:
        return [f"{path.name}: the frontend job has no `{_FRONTEND_UNIT_RUN}`"]
    if not diff:
        return [f"{path.name}: the frontend job never runs `{_WIRE_TUNING_DIFF}`"]
    if max(diff) < min(unit):
        return [f"{path.name}: `{_WIRE_TUNING_DIFF}` runs before `{_FRONTEND_UNIT_RUN}`"]
    return []


def test_the_wire_tuning_fixture_is_tracked_and_written_by_the_frontend_suite() -> None:
    """The diffed path is tracked, and the vitest case that rewrites it still names it."""
    tracked = proc_run.run(
        ["git", "ls-files", "--error-unmatch", WIRE_TUNING_FIXTURE],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert tracked.returncode == 0, WIRE_TUNING_FIXTURE
    writer = REPO_ROOT / "frontend/src/components/AssistantPanel/wireTuning.test.ts"
    assert '"wire_tuning.json"' in writer.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [WORKFLOW_PATH, GITLAB_CI_PATH], ids=lambda p: p.name)
def test_the_frontend_job_diffs_the_wire_tuning_fixture_after_its_unit_run(path: Path) -> None:
    """A vitest rewrite that differs from the committed fixture fails the frontend job."""
    assert _wire_tuning_violations(path, _ci_jobs(path)) == []


@pytest.mark.parametrize("path", [WORKFLOW_PATH, GITLAB_CI_PATH], ids=lambda p: p.name)
def test_non_vacuity_a_missing_or_early_wire_tuning_diff_is_detected(path: Path) -> None:
    """Dropping the diff, or moving it before the unit run, each reads as a violation."""
    jobs = _ci_jobs(path)
    assert _wire_tuning_violations(path, jobs) == [], "fixture assumption broken"
    frontend = jobs[_FRONTEND_JOB]
    key = "steps" if isinstance(frontend.get("steps"), list) else "script"
    entries = list(frontend[key])

    def is_diff(entry: Any) -> bool:
        return _WIRE_TUNING_DIFF in json.dumps(entry)

    dropped = [e for e in entries if not is_diff(e)]
    assert len(dropped) == len(entries) - 1
    assert _wire_tuning_violations(path, {**jobs, _FRONTEND_JOB: {**frontend, key: dropped}})

    early = [e for e in entries if is_diff(e)] + dropped
    assert _wire_tuning_violations(path, {**jobs, _FRONTEND_JOB: {**frontend, key: early}})
