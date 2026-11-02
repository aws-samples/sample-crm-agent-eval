# SPDX-License-Identifier: MIT
"""The image-build script restarts an early-failed CodeBuild run at most once.

The `local-exec` heredoc of `null_resource.image_build` in `infra/modules/agentcore/ecr_codebuild.tf`
is extracted, its Terraform interpolations are replaced with literals, and it is run under `bash`
with stub `aws` and `sleep` executables on `PATH`. No AWS call is made.

The stub `aws` answers `batch-get-builds` with the shape a terminal build has: `currentPhase` is
`COMPLETED` and the failed phase is identified only by `phases[].phaseStatus`. It applies the
script's own `--query` to that document with `jmespath` and renders `--output text` the way the
CLI does, so the script's query is exercised against the real response shape.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import textwrap
from pathlib import Path

from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
ECR_CODEBUILD_TF = REPO_ROOT / "infra" / "modules" / "agentcore" / "ecr_codebuild.tf"

STUB_AWS = """#!{python}
import json, os, sys
import jmespath

PHASE_ORDER = [
    "SUBMITTED", "QUEUED", "PROVISIONING", "DOWNLOAD_SOURCE", "INSTALL", "PRE_BUILD",
    "BUILD", "POST_BUILD", "UPLOAD_ARTIFACTS", "FINALIZING",
]
FAILED_PHASE = {{"early": "PROVISIONING", "late": "BUILD"}}


def build_document(build_id, outcome, poll):
    outcome, _, pending = outcome.partition("@")
    if poll <= int(pending or 0):
        return {{"builds": [{{
            "id": build_id,
            "buildStatus": "IN_PROGRESS",
            "currentPhase": "PROVISIONING",
            "phases": [
                {{"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED"}},
                {{"phaseType": "QUEUED", "phaseStatus": "SUCCEEDED"}},
                {{"phaseType": "PROVISIONING", "phaseStatus": "IN_PROGRESS"}},
            ],
        }}]}}
    phases = []
    for phase in PHASE_ORDER:
        if outcome in FAILED_PHASE and phase == FAILED_PHASE[outcome]:
            phases.append({{"phaseType": phase, "phaseStatus": "FAILED"}})
            phases.append({{"phaseType": "FINALIZING", "phaseStatus": "SUCCEEDED"}})
            break
        phases.append({{"phaseType": phase, "phaseStatus": "SUCCEEDED"}})
    phases.append({{"phaseType": "COMPLETED"}})
    return {{"builds": [{{
        "id": build_id,
        "buildStatus": "SUCCEEDED" if outcome == "ok" else "FAILED",
        "currentPhase": "COMPLETED",
        "phases": phases,
    }}]}}


def text(value):
    if isinstance(value, list):
        return "\\t".join(text(v) for v in value)
    return "None" if value is None else str(value)


def option(name):
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args else None


state = os.environ["STUB_STATE_DIR"]
outcomes = os.environ["STUB_OUTCOMES"].split(",")
with open(os.path.join(state, "calls"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
op = " ".join(sys.argv[1:3])
starts_path = os.path.join(state, "starts")
starts = int(open(starts_path).read()) if os.path.exists(starts_path) else 0
if op == "ecr describe-images":
    sys.stderr.write("An error occurred (ImageNotFoundException)\\n")
    sys.exit(254)
elif op == "codebuild start-build":
    starts += 1
    open(starts_path, "w").write(str(starts))
    if starts > len(outcomes):
        sys.stderr.write("stub: start-build limit reached\\n")
        sys.exit(1)
    print(f"build-{{starts}}")
elif op == "codebuild batch-get-builds":
    build_id = option("--ids")
    polls_path = os.path.join(state, f"polls-{{build_id}}")
    poll = (int(open(polls_path).read()) if os.path.exists(polls_path) else 0) + 1
    open(polls_path, "w").write(str(poll))
    document = build_document(build_id, outcomes[int(build_id.split("-")[1]) - 1], poll)
    query = option("--query")
    print(text(jmespath.search(query, document) if query else document))
else:
    sys.stderr.write(f"stub: unexpected aws call: {{sys.argv[1:]}}\\n")
    sys.exit(99)
"""


def _image_build_script() -> str:
    text = ECR_CODEBUILD_TF.read_text()
    match = re.search(
        r'resource "null_resource" "image_build".*?command = <<-EOT\n(.*?)\n\s*EOT\n', text, re.S
    )
    assert match is not None, "image_build local-exec heredoc not found"
    body = textwrap.dedent(match.group(1))
    body = re.sub(r"(?<!\$)\$\{(?:var|aws_[a-z_]+)\.[a-z_.]+\}", "stub-value", body)
    assert "${" not in body.replace("$${", ""), "unreplaced Terraform interpolation"
    return body.replace("$${", "${")


def _run(tmp_path: Path, *outcomes: str) -> tuple[int, int, list[list[str]], str]:
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()
    for name, content in (
        ("aws", STUB_AWS.format(python=sys.executable)),
        ("sleep", "#!/bin/sh\nexit 0\n"),
    ):
        path = bin_dir / name
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "image_build.sh"
    script.write_text(_image_build_script())
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_STATE_DIR": str(state_dir),
        "STUB_OUTCOMES": ",".join(outcomes),
    }
    proc = proc_run.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    starts = int((state_dir / "starts").read_text())
    calls = [json.loads(line) for line in (state_dir / "calls").read_text().splitlines()]
    polls = [call for call in calls if call[:2] == ["codebuild", "batch-get-builds"]]
    return proc.returncode, starts, polls, proc.stderr


def test_a_persistent_early_failure_restarts_once_then_fails_the_apply(tmp_path: Path) -> None:
    rc, starts, _, stderr = _run(tmp_path, "early", "early", "early")
    assert starts == 2, stderr
    assert rc != 0
    assert "stub: start-build limit reached" not in stderr
    assert "already restarted once" in stderr


def test_an_early_failure_followed_by_success_passes_after_one_restart(tmp_path: Path) -> None:
    rc, starts, _, stderr = _run(tmp_path, "early", "ok")
    assert (rc, starts) == (0, 2), stderr


def test_a_first_build_success_starts_exactly_one_build(tmp_path: Path) -> None:
    rc, starts, _, stderr = _run(tmp_path, "ok")
    assert (rc, starts) == (0, 1), stderr


def test_an_early_failure_observed_after_several_polls_still_restarts(tmp_path: Path) -> None:
    rc, starts, polls, stderr = _run(tmp_path, "early@3", "ok")
    assert (rc, starts) == (0, 2), stderr
    assert len(polls) == 5, polls


def test_a_failure_in_the_build_phase_fails_the_apply_without_a_restart(tmp_path: Path) -> None:
    rc, starts, _, stderr = _run(tmp_path, "late", "ok")
    assert starts == 1, stderr
    assert rc != 0
    assert "phase BUILD" in stderr


def test_each_poll_reads_status_and_phase_in_one_call(tmp_path: Path) -> None:
    rc, starts, polls, stderr = _run(tmp_path, "early", "ok")
    assert (rc, starts) == (0, 2), stderr
    assert len(polls) == 2, polls
    for call in polls:
        query = call[call.index("--query") + 1]
        assert "buildStatus" in query and "phases" in query, query
