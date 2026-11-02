# SPDX-License-Identifier: MIT
"""Layer 4 check: the deployed chat-path timeouts and the Runtime image.

Read-only describes of the deployed resources:

- the relay Lambda's `Timeout` equals :data:`RELAY_TIMEOUT_SECONDS`;
- the Gateway #2 chat target Lambda's `Timeout` equals :data:`CHAT_TARGET_TIMEOUT_SECONDS`;
- the CloudFront relay origin's `OriginReadTimeout` equals :data:`RELAY_ORIGIN_READ_TIMEOUT_SECONDS`;
- the Runtime's container image tag is the one the local build (`agent-source.zip`) mints, and ECR
  holds that tag.

The 600 s chat turn itself is not exercised end to end; that row is a declared `skip`.

Usage:
    python3 -m scripts.smoke.chat_timeouts [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import hashlib
import sys
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from scripts.smoke.harness import CheckResult, ExpectedCheckCount, SmokeContext, run_main

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "chat_timeouts"

#: Configured values, held equal to the IaC by `tests/unit/test_smoke_chat_timeouts.py`.
RELAY_TIMEOUT_SECONDS: Final[int] = 660
CHAT_TARGET_TIMEOUT_SECONDS: Final[int] = 660
RELAY_ORIGIN_READ_TIMEOUT_SECONDS: Final[int] = 60

#: The chat target's function-name suffix under the deployment prefix (`infra/mcp_chat_gateway.tf`).
CHAT_TARGET_SUFFIX: Final[str] = "-chat-target"

#: The archive whose digest names the Runtime image (`local.agent_image_tag` in `infra/main.tf`).
AGENT_SOURCE_ZIP: Final[Path] = (
    Path(__file__).resolve().parents[2] / "infra" / "build" / "lambda" / "agent-source.zip"
)

__all__ = [
    "AGENT_SOURCE_ZIP",
    "CHAT_TARGET_TIMEOUT_SECONDS",
    "LAYER",
    "RELAY_ORIGIN_READ_TIMEOUT_SECONDS",
    "RELAY_TIMEOUT_SECONDS",
    "RESOURCE_CLASS",
    "checks",
    "expected_image_tag",
    "main",
]


def expected_image_tag(zip_path: Path = AGENT_SOURCE_ZIP) -> str:
    """`sha-<first 12 hex of sha256(agent-source.zip)>`, as `local.agent_image_tag` mints it."""
    return "sha-" + hashlib.sha256(zip_path.read_bytes()).hexdigest()[:12]


def _live_table_prefix(ctx: SmokeContext) -> str:
    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    return str(table_names["eval_runs"]).removesuffix("eval_runs")


def _timeout_row(
    ctx: SmokeContext, *, check: str, function_name: str, expected: int
) -> CheckResult:
    try:
        configuration = ctx.client("lambda").get_function_configuration(FunctionName=function_name)
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        return CheckResult(
            resource=function_name,
            check=check,
            status="fail",
            detail=f"get_function_configuration raised {type(exc).__name__}: {exc}",
        )
    observed = configuration.get("Timeout")
    return CheckResult(
        resource=function_name,
        check=check,
        status="pass" if observed == expected else "fail",
        detail=f"deployed Timeout={observed!r}, configured {expected}",
    )


def _origin_read_timeout_row(ctx: SmokeContext) -> CheckResult:
    check = "relay_origin_read_timeout"
    try:
        distribution_id = ctx.outputs.require_str("cloudfront_distribution_id")
        relay_host = urllib.parse.urlparse(ctx.outputs.require_str("agent_relay_function_url"))
        config = ctx.client("cloudfront").get_distribution_config(Id=distribution_id)
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        return CheckResult(
            resource="cloudfront",
            check=check,
            status="fail",
            detail=f"could not read the distribution: {type(exc).__name__}: {exc}",
        )
    origins = ((config.get("DistributionConfig") or {}).get("Origins") or {}).get("Items") or []
    matching = [o for o in origins if o.get("DomainName") == relay_host.hostname]
    if len(matching) != 1:
        return CheckResult(
            resource=distribution_id,
            check=check,
            status="fail",
            detail=f"{len(matching)} origin(s) carry the relay host {relay_host.hostname!r}; "
            "expected exactly one",
        )
    observed = (matching[0].get("CustomOriginConfig") or {}).get("OriginReadTimeout")
    return CheckResult(
        resource=f"{distribution_id}:{matching[0].get('Id')}",
        check=check,
        status="pass" if observed == RELAY_ORIGIN_READ_TIMEOUT_SECONDS else "fail",
        detail=f"deployed OriginReadTimeout={observed!r}, configured "
        f"{RELAY_ORIGIN_READ_TIMEOUT_SECONDS}",
    )


def _image_rows(ctx: SmokeContext, zip_path: Path) -> Iterator[CheckResult]:
    resource = "agentcore_runtime"
    if not zip_path.is_file():
        yield CheckResult(
            resource=str(zip_path),
            check="runtime_image_is_the_built_one",
            status="fail",
            detail=f"no local build at {zip_path}; run scripts/package_lambdas.sh before the smoke "
            "so the expected image tag can be computed",
        )
        return
    expected = expected_image_tag(zip_path)
    try:
        runtime_arn = ctx.outputs.require_str("agentcore_runtime_arn")
        runtime_id = runtime_arn.rpartition("/")[2]
        runtime = ctx.client("bedrock-agentcore-control").get_agent_runtime(
            agentRuntimeId=runtime_id
        )
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        yield CheckResult(
            resource=resource,
            check="runtime_image_is_the_built_one",
            status="fail",
            detail=f"could not describe the Runtime: {type(exc).__name__}: {exc}",
        )
        return
    uri = str(
        ((runtime.get("agentRuntimeArtifact") or {}).get("containerConfiguration") or {}).get(
            "containerUri"
        )
        or ""
    )
    repository_uri, _, tag = uri.rpartition(":")
    yield CheckResult(
        resource=resource,
        check="runtime_image_is_the_built_one",
        status="pass" if tag == expected else "fail",
        detail=f"deployed containerUri tag {tag!r}, local build {expected!r}"
        + ("" if tag == expected else " (a rebuilt agent-source.zip after deploy also differs)"),
    )
    repository = repository_uri.partition("/")[2]
    yield _ecr_row(ctx, repository=repository, tag=tag)


def _ecr_row(ctx: SmokeContext, *, repository: str, tag: str) -> CheckResult:
    check = "runtime_image_tag_in_ecr"
    if not repository or not tag:
        return CheckResult(
            resource="ecr",
            check=check,
            status="fail",
            detail=f"the containerUri names no repository/tag ({repository!r}, {tag!r})",
        )
    try:
        images = ctx.client("ecr").describe_images(
            repositoryName=repository, imageIds=[{"imageTag": tag}]
        )
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        return CheckResult(
            resource=repository,
            check=check,
            status="fail",
            detail=f"describe_images raised {type(exc).__name__}: {exc}",
        )
    details: Sequence[Mapping[str, Any]] = images.get("imageDetails") or []
    digests = [str(d.get("imageDigest")) for d in details]
    return CheckResult(
        resource=repository,
        check=check,
        status="pass" if len(digests) == 1 else "fail",
        detail=f"tag {tag!r} -> digest(s) {digests}",
    )


def checks(
    ctx: SmokeContext, *, agent_source_zip: Path = AGENT_SOURCE_ZIP
) -> Iterator[CheckResult]:
    """Yield the three timeout rows, the two image rows, and the declared end-to-end skip."""
    try:
        relay = ctx.outputs.require_str("agent_relay_function_name")
        chat_target = f"{_live_table_prefix(ctx)}{CHAT_TARGET_SUFFIX}"
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        yield CheckResult(
            resource="agent_relay_function_name|dynamodb_table_names",
            check="terraform_output_present",
            status="fail",
            detail=f"a required Terraform output is absent ({exc}); a missing output is a failure",
        )
        return
    yield _timeout_row(
        ctx, check="relay_timeout", function_name=relay, expected=RELAY_TIMEOUT_SECONDS
    )
    yield _timeout_row(
        ctx,
        check="chat_target_timeout",
        function_name=chat_target,
        expected=CHAT_TARGET_TIMEOUT_SECONDS,
    )
    yield _origin_read_timeout_row(ctx)
    yield from _image_rows(ctx, agent_source_zip)
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="chat_600s_turn_end_to_end",
        status="skip",
        detail="declared not exercised: a 600 s turn would hold a model call for ten minutes; the "
        "timeout chain is asserted from configuration (test_timeout_chain.py) and the rows above",
    )


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """Five rows once the outputs resolve (three timeouts, at least one image row, the skip)."""
    try:
        ctx.outputs.require_str("agent_relay_function_name")
        _live_table_prefix(ctx)
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the basis
        return ExpectedCheckCount(
            minimum=1,
            basis=f"a required output is unreadable ({type(exc).__name__}), so `checks` emits "
            "its single terraform_output_present failure",
        )
    return ExpectedCheckCount(
        minimum=5,
        basis="3 timeout rows + at least 1 image row (2 when the containerUri resolves) + the "
        "declared end-to-end skip",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the chat-timeout checks and return the process exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
