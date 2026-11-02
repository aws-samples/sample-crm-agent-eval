# SPDX-License-Identifier: MIT
"""Unit tests for the `frontend` job in `.github/workflows/verify.yml`.

The job exists, runs the suite in shuffled order, and cannot pass without doing the work.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is required to parse the workflow")

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_YML = REPO_ROOT / ".github" / "workflows" / "verify.yml"
JOB = "frontend"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    """`verify.yml` parsed once."""
    parsed = yaml.safe_load(VERIFY_YML.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "verify.yml did not parse as a mapping"
    return parsed


@pytest.fixture(scope="module")
def steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the frontend job's steps, asserted to number at least five."""
    jobs = workflow["jobs"]
    assert JOB in jobs, f"verify.yml declares no `{JOB}` job; the frontend suite has no CI coverage"
    job_steps = jobs[JOB]["steps"]
    assert len(job_steps) >= 5, f"the {JOB} job has only {len(job_steps)} step(s)"
    return list(job_steps)


def _runs(steps: list[dict[str, Any]]) -> list[str]:
    return [str(step["run"]) for step in steps if "run" in step]


def test_the_frontend_job_exists_and_is_independent_of_the_python_job(
    workflow: dict[str, Any],
) -> None:
    """The frontend job declares no `needs:` and so runs independently of the Python job."""
    job = workflow["jobs"][JOB]

    assert "needs" not in job, "the frontend job must not be gated behind the Python job"


def test_the_unit_suite_runs_with_shuffled_order(steps: list[dict[str, Any]]) -> None:
    """A step runs the frontend unit suite with `--sequence.shuffle=true`."""
    shuffled = [
        run for run in _runs(steps) if "npm run test" in run and "--sequence.shuffle=true" in run
    ]

    assert shuffled, f"no step runs the suite with --sequence.shuffle=true: {_runs(steps)}"


def test_the_shuffle_caveat_is_recorded_where_the_next_reader_will_be(
    steps: list[dict[str, Any]],
) -> None:
    """`verify.yml` records that a green shuffled run is evidence, not proof."""
    comments = VERIFY_YML.read_text(encoding="utf-8")

    assert "DETECTOR, NOT A CERTIFICATE" in comments, (
        "the shuffle step must record that a green shuffled run is evidence, not proof"
    )


def test_types_are_generated_before_anything_consumes_them(steps: list[dict[str, Any]]) -> None:
    """The `gen:types` step precedes the typecheck step."""
    runs = _runs(steps)
    generate = next(index for index, run in enumerate(runs) if "gen:types" in run)
    typecheck = next(index for index, run in enumerate(runs) if "typecheck" in run)

    assert generate < typecheck, f"gen:types must precede the typecheck: {runs}"


def test_the_typecheck_does_not_regenerate_while_compiling(steps: list[dict[str, Any]]) -> None:
    """The single typecheck step invokes `typecheck:no-gen`, never a bare `npm run typecheck`."""
    typecheck_steps = [run for run in _runs(steps) if "typecheck" in run]

    assert len(typecheck_steps) == 1, f"expected exactly one typecheck step: {typecheck_steps}"
    assert typecheck_steps[0].split(" -- ")[0] == "npm run typecheck:no-gen", typecheck_steps
    assert not any(run.strip() == "npm run typecheck" for run in _runs(steps)), _runs(steps)


def test_the_install_is_lockfile_only(steps: list[dict[str, Any]]) -> None:
    """The job installs with `npm ci` and never with `npm install`."""
    runs = _runs(steps)

    assert any(run.strip() == "npm ci" for run in runs), runs
    assert not any("npm install" in run for run in runs), runs


def test_the_node_version_comes_from_the_repositorys_single_pin(
    steps: list[dict[str, Any]],
) -> None:
    """The Node version is read from `frontend/.nvmrc`, not written inline."""
    assert (REPO_ROOT / "frontend" / ".nvmrc").is_file()

    reads_pin = [run for run in _runs(steps) if ".nvmrc" in run]
    inline = [
        step
        for step in steps
        if step.get("uses", "").startswith("actions/setup-node")
        and not str(step.get("with", {}).get("node-version", "")).startswith("${{")
    ]

    assert reads_pin, "no step reads frontend/.nvmrc"
    assert inline == [], f"setup-node must consume the pin, not a literal: {inline}"


def test_no_frontend_step_swallows_a_failure(steps: list[dict[str, Any]]) -> None:
    """No frontend step suppresses its own failure."""
    offenders = [
        run
        for run in _runs(steps)
        if "|| true" in run or "continue-on-error" in run or re.search(r"\|\|\s*exit\s+0", run)
    ]

    assert offenders == [], offenders
    assert not any(step.get("continue-on-error") for step in steps)


def test_every_action_in_the_frontend_job_is_pinned_to_a_sha(steps: list[dict[str, Any]]) -> None:
    """Every action in the frontend job is pinned to a 40-hex commit SHA."""
    unpinned = [
        step["uses"]
        for step in steps
        if "uses" in step and not re.fullmatch(r"[^@]+@[0-9a-f]{40}", str(step["uses"]))
    ]

    assert unpinned == [], unpinned


def test_the_frontend_job_needs_no_aws_credential(workflow: dict[str, Any]) -> None:
    """The frontend job references no secret and no AWS credential."""
    rendered = yaml.safe_dump(workflow["jobs"][JOB])

    assert "secrets." not in rendered
    assert "AWS_ACCESS_KEY_ID" not in rendered
    assert "aws-actions" not in rendered
