# SPDX-License-Identifier: MIT
"""Layer 4 deployed-artifact parity check: every deployed Lambda's code == the local zip.

`scripts/check_lambda_zip_imports.py`'s `source_parity` phase binds zip <-> `src/`; this binds
deployed <-> zip. The comparison is Lambda's own `CodeSha256`: the base64-encoded SHA-256 of the
deployment package bytes, i.e.

    openssl dgst -sha256 -binary infra/build/lambda/<name>.zip | base64

Coverage:

* The action functions are derived from `crm.toolspec.definitions.ALL_TOOLS` (never a
  hand-written list), through `scripts.smoke.lambda_actions.function_name_for`.
* `crm-agent-eval-identity-interceptor` and `crm-agent-eval-pre-token-generation`, the two non-tool
  Lambda functions `infra/main.tf` declares.

Every target gets a pass/fail verdict; nothing here defers. A missing build directory, a missing or
unreadable zip, a function that does not exist, or a response carrying no `CodeSha256` is a `fail`.
Two coverage checks compare the resource names that reached a verdict against independently obtained
expectations, so a dropped, duplicated, extra or renamed target fails.

Two entry points, one comparison implementation:

* `python3 -m scripts.smoke.deployed_parity --json` -- the Layer 4 inventory path
  (`scripts/verify_deployment.sh`), emitting the pinned per-check record shape with provenance.
* `python3 -m scripts.smoke.deployed_parity --deploy-gate` -- the post-apply gate
  `scripts/deploy.sh` runs. Same comparison over the same targets, but it needs no Terraform
  outputs file (it reads nothing but the zips and the Lambda API).

No AWS account id is written to the output: function names, digests, sizes and timestamps only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from scripts.smoke.harness import (
    CheckResult,
    SmokeContext,
    resolve_profile,
    resolve_region,
    run_main,
    smoke_client_config,
)
from scripts.smoke.lambda_actions import function_name_for

from crm.toolspec.definitions import ALL_TOOLS

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "deployed_parity"

#: Repository root, three levels up from this file (`scripts/smoke/deployed_parity.py`).
REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent

#: Where `scripts/package_lambdas.sh` writes the zips `infra/main.tf` consumes through
#: `filebase64sha256`. `LAMBDA_BUILD_DIR` overrides it, the same env var
#: `scripts/check_lambda_zip_imports.py` honours.
DEFAULT_BUILD_DIR: Final[Path] = REPO_ROOT / "infra" / "build" / "lambda"

#: The check name every parity verdict publishes.
PARITY_CHECK: Final[str] = "deployed_code_sha256_matches_local_zip"

#: The flag that selects the post-apply gate mode `scripts/deploy.sh` invokes.
DEPLOY_GATE_FLAG: Final[str] = "--deploy-gate"

#: The two non-tool Lambda functions, as `infra/main.tf` names them, paired with the zip
#: `scripts/package_lambdas.sh` builds for each.
NON_TOOL_FUNCTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("crm-agent-eval-identity-interceptor", "identity_interceptor.zip"),
    ("crm-agent-eval-pre-token-generation", "pre_token_generation.zip"),
)


@dataclass(frozen=True)
class ParityTarget:
    """One (deployed function, local zip) pair to compare."""

    resource: str
    function_name: str
    zip_path: Path


def build_dir(override: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Resolve the Lambda build directory: argument -> `LAMBDA_BUILD_DIR` -> the default path."""
    if override:
        return Path(override)
    environment = env if env is not None else os.environ
    from_env = environment.get("LAMBDA_BUILD_DIR")
    return Path(from_env) if from_env else DEFAULT_BUILD_DIR


def action_targets(directory: Path) -> tuple[ParityTarget, ...]:
    """Return one target per action tool, derived from `ALL_TOOLS` (never a hand-written list)."""
    return tuple(
        ParityTarget(
            resource=tool.name,
            function_name=function_name_for(tool),
            zip_path=directory / f"{tool.name}.zip",
        )
        for tool in ALL_TOOLS
    )


def non_tool_targets(directory: Path) -> tuple[ParityTarget, ...]:
    """Return one target per non-tool Lambda function declared in `NON_TOOL_FUNCTIONS`."""
    return tuple(
        ParityTarget(resource=name, function_name=name, zip_path=directory / zip_name)
        for name, zip_name in NON_TOOL_FUNCTIONS
    )


def parity_targets(directory: Path) -> tuple[ParityTarget, ...]:
    """Return every parity target: one per `ALL_TOOLS` entry plus one per `NON_TOOL_FUNCTIONS` entry."""
    return (*action_targets(directory), *non_tool_targets(directory))


def local_zip_digest(path: Path) -> str:
    """Return the base64 SHA-256 of `path`, the encoding Lambda reports as `CodeSha256`.

    Equivalent to `openssl dgst -sha256 -binary <path> | base64`. Raises `OSError` when the file is
    absent or unreadable.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


@dataclass(frozen=True)
class DeployedCode:
    """The deployed code identity of one function, as Lambda reports it."""

    code_sha256: str
    code_size: int
    last_modified: str


def deployed_code(client: Any, function_name: str) -> DeployedCode:
    """Return `function_name`'s deployed code identity.

    Raises `LookupError` when the function does not exist or the response carries no usable
    `CodeSha256`, and lets `ClientError`/`BotoCoreError` propagate for the caller to report.
    """
    try:
        configuration = client.get_function_configuration(FunctionName=function_name)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "ResourceNotFoundException":
            raise LookupError(
                f"function {function_name!r} does not exist in this account/region — parity cannot "
                "be established (never read as a pass)"
            ) from exc
        raise
    sha = str(configuration.get("CodeSha256") or "")
    if not sha:
        raise LookupError(
            f"Lambda reported no CodeSha256 for {function_name!r}: {configuration!r} — parity "
            "cannot be established (never read as a pass)"
        )
    return DeployedCode(
        code_sha256=sha,
        code_size=int(configuration.get("CodeSize") or 0),
        last_modified=str(configuration.get("LastModified") or "unknown"),
    )


def _local_size(path: Path) -> int:
    """Return `path`'s size in bytes, or -1 when it cannot be determined."""
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _compare(client: Any, target: ParityTarget) -> tuple[bool | None, str]:
    """Compare one target, returning `(matched, detail)`.

    `matched` is `None` when the comparison could not be made at all (missing zip, missing function,
    absent digest, AWS error); the call sites report it as a failure.
    """
    try:
        local_sha = local_zip_digest(target.zip_path)
    except OSError as exc:
        return None, (
            f"local zip {target.zip_path} could not be read ({type(exc).__name__}: {exc}) — run "
            "scripts/package_lambdas.sh; an unbuilt or unreadable zip is never parity"
        )
    try:
        deployed = deployed_code(client, target.function_name)
    except LookupError as exc:
        return None, str(exc)
    except (ClientError, BotoCoreError) as exc:
        return None, (
            f"lambda:GetFunctionConfiguration for {target.function_name!r} failed "
            f"({type(exc).__name__}: {exc}) — parity cannot be established"
        )

    local_size = _local_size(target.zip_path)
    if deployed.code_sha256 == local_sha:
        return True, (
            f"{target.function_name}: deployed CodeSha256 {deployed.code_sha256} == local "
            f"{target.zip_path.name} digest, {deployed.code_size} bytes deployed vs {local_size} "
            f"local, LastModified {deployed.last_modified}"
        )
    return False, (
        f"{target.function_name}: deployed CodeSha256 {deployed.code_sha256} "
        f"({deployed.code_size} bytes, LastModified {deployed.last_modified}) != local "
        f"{target.zip_path.name} digest {local_sha} ({local_size} bytes) — the deployed artifact "
        "is not the one this source tree builds; apply the current zips"
    )


def check_parity_target(client: Any, target: ParityTarget) -> CheckResult:
    """Return the pass/fail parity verdict for one Lambda function."""
    matched, detail = _compare(client, target)
    if matched is True:
        return CheckResult(
            resource=target.resource, check=PARITY_CHECK, status="pass", detail=detail
        )
    return CheckResult(resource=target.resource, check=PARITY_CHECK, status="fail", detail=detail)


def check_build_dir(directory: Path) -> CheckResult:
    """Assert the build directory exists and holds at least one zip."""
    if not directory.is_dir():
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="local_build_present",
            status="fail",
            detail=(
                f"Lambda build directory {directory} does not exist — nothing to compare the "
                "deployment against; run scripts/package_lambdas.sh"
            ),
        )
    zips = sorted(p.name for p in directory.glob("*.zip"))
    if not zips:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="local_build_present",
            status="fail",
            detail=f"Lambda build directory {directory} holds no *.zip — no zips built is not parity",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="local_build_present",
        status="pass",
        detail=f"{len(zips)} zip(s) present in {directory}",
    )


def expected_action_resources() -> tuple[str, ...]:
    """Return the resource name every action tool must produce a parity verdict for.

    Derived from `ALL_TOOLS` directly, not from the target list the verdicts were produced by.
    """
    return tuple(tool.name for tool in ALL_TOOLS)


def check_coverage(verdict_resources: Iterable[str]) -> CheckResult:
    """Assert exactly one parity verdict was reached per declared action tool, by resource name.

    `verdict_resources` are the resource names that actually carry a `PARITY_CHECK` verdict, read
    back off the emitted results; the expectation comes from `expected_action_resources()`. Compared
    as multisets, so a missing, duplicated, extra or renamed target fails.
    """
    reached = sorted(verdict_resources)
    expected = sorted(expected_action_resources())
    if reached == expected and expected:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="coverage_is_non_vacuous",
            status="pass",
            detail=(
                f"{len(reached)} parity verdict(s), one per tool declared in ALL_TOOLS "
                f"({len(expected)}), matched by resource name"
            ),
        )
    missing = sorted(set(expected) - set(reached))
    unexpected = sorted(set(reached) - set(expected))
    duplicated = sorted({name for name in reached if reached.count(name) > 1})
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="coverage_is_non_vacuous",
        status="fail",
        detail=(
            f"reached {len(reached)} parity verdict(s) for {len(expected)} tool(s) declared in "
            f"ALL_TOOLS — an incomplete sweep is never parity (missing={missing}, "
            f"unexpected={unexpected}, duplicated={duplicated})"
        ),
    )


def expected_non_tool_resources() -> tuple[str, ...]:
    """Return the resource name every non-tool Lambda must produce a parity verdict for."""
    return tuple(name for name, _ in NON_TOOL_FUNCTIONS)


def check_non_tool_coverage(verdict_resources: Iterable[str]) -> CheckResult:
    """Assert exactly one parity verdict was reached per non-tool Lambda function."""
    reached = sorted(verdict_resources)
    expected = sorted(expected_non_tool_resources())
    if reached == expected and expected:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="non_tool_coverage_is_non_vacuous",
            status="pass",
            detail=(
                f"{len(reached)} parity verdict(s), one per non-tool Lambda function declared in "
                f"NON_TOOL_FUNCTIONS ({len(expected)}), matched by resource name"
            ),
        )
    missing = sorted(set(expected) - set(reached))
    unexpected = sorted(set(reached) - set(expected))
    duplicated = sorted({name for name in reached if reached.count(name) > 1})
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="non_tool_coverage_is_non_vacuous",
        status="fail",
        detail=(
            f"reached {len(reached)} parity verdict(s) for {len(expected)} non-tool function(s) "
            f"declared in NON_TOOL_FUNCTIONS — an incomplete sweep is never parity "
            f"(missing={missing}, unexpected={unexpected}, duplicated={duplicated})"
        ),
    )


def _action_verdict_resources(results: Iterable[CheckResult]) -> tuple[str, ...]:
    """Return the resource names carrying a `PARITY_CHECK` verdict for an action tool."""
    non_tool = set(expected_non_tool_resources())
    return tuple(
        r.resource for r in results if r.check == PARITY_CHECK and r.resource not in non_tool
    )


def _non_tool_verdict_resources(results: Iterable[CheckResult]) -> tuple[str, ...]:
    """Return the resource names carrying a `PARITY_CHECK` verdict for a non-tool function."""
    non_tool = set(expected_non_tool_resources())
    return tuple(r.resource for r in results if r.check == PARITY_CHECK and r.resource in non_tool)


def checks(ctx: SmokeContext, *, directory: Path | None = None) -> Iterable[CheckResult]:
    """Yield the build-directory check, one verdict per function, and both coverage checks.

    Every target is evaluated even when an earlier one failed, so the inventory stays complete.
    """
    resolved = build_dir(directory)
    yield check_build_dir(resolved)
    client = ctx.client("lambda")
    emitted: list[CheckResult] = []
    for target in parity_targets(resolved):
        result = check_parity_target(client, target)
        emitted.append(result)
        yield result
    yield check_coverage(_action_verdict_resources(emitted))
    yield check_non_tool_coverage(_non_tool_verdict_resources(emitted))


def deploy_gate(
    client: Any, *, directory: Path, stream: Any = None
) -> tuple[int, tuple[CheckResult, ...]]:
    """Compare every Lambda function's deployed code against its local zip; return (exit, results).

    Used by `scripts/deploy.sh` immediately after a successful apply. Needs no Terraform outputs.
    Exits non-zero when any verdict is not `pass`, and also when the set of verdicts reached is not
    exactly the declared set.
    """
    out = stream if stream is not None else sys.stdout
    results = [check_build_dir(directory)]
    for target in parity_targets(directory):
        results.append(check_parity_target(client, target))
    results.append(check_coverage(_action_verdict_resources(results)))
    results.append(check_non_tool_coverage(_non_tool_verdict_resources(results)))
    failures = [r for r in results if r.status == "fail"]
    for result in results:
        out.write(f"[{RESOURCE_CLASS}] {result.resource}: {result.check} = {result.status}")
        if result.detail:
            out.write(f" ({result.detail})")
        out.write("\n")
    if failures:
        out.write(
            f"FAIL: {len(failures)} of {len(results)} deployed-parity check(s) failed — the "
            "deployed artifacts are not the ones this source tree built\n"
        )
        return 1, tuple(results)
    out.write(
        f"OK: all {len(ALL_TOOLS) + len(NON_TOOL_FUNCTIONS)} Lambda function(s) report a CodeSha256 "
        "byte-equal to their own local zip digest\n"
    )
    return 0, tuple(results)


def _deploy_gate_main(argv: Sequence[str]) -> int:
    """Entry point for `--deploy-gate`: parse flags, build a client, run `deploy_gate`."""
    parser = argparse.ArgumentParser(
        description="Post-apply deployed<->zip parity gate for the action Lambdas."
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--build-dir", default=None)
    # Accepted and ignored so deploy.sh may pass the same vector this module's inventory mode takes.
    parser.add_argument("--outputs", default=None)
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args(list(argv))

    profile = resolve_profile(args.profile)
    region = resolve_region(args.region)
    session = boto3.session.Session(profile_name=profile, region_name=region)
    # Not `smoke_context`: this gate needs no Terraform outputs. A wrong-region client finds none of
    # the functions, so every parity target fails.
    client = session.client("lambda", region_name=region, config=smoke_client_config())
    exit_code, _ = deploy_gate(client, directory=build_dir(args.build_dir))
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: the Layer 4 inventory path, or the `--deploy-gate` post-apply gate."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if DEPLOY_GATE_FLAG in arguments:
        return _deploy_gate_main([a for a in arguments if a != DEPLOY_GATE_FLAG])
    return run_main(
        arguments,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
