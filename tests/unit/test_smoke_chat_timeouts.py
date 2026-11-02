# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.chat_timeouts`: timeout rows, the image rows and the IaC agreement.

Fake `lambda`, `cloudfront`, `bedrock-agentcore-control` and `ecr` clients; no network.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from scripts.smoke import chat_timeouts as module
from scripts.smoke.harness import CheckResult, context_for_test

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA = REPO_ROOT / "infra"

_RELAY = "crm-agent-eval-agent-relay"
_CHAT_TARGET = "crm-agent-eval-chat-target"
_RELAY_HOST = "abc123.lambda-url.us-east-1.on.aws"
_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm_agent_eval-AbC123"
_REPO_URI = "123456789012.dkr.ecr.us-east-1.amazonaws.com/crm-agent-eval-agent"
_OUTPUTS: dict[str, Any] = {
    "agent_relay_function_name": _RELAY,
    "agent_relay_function_url": f"https://{_RELAY_HOST}/",
    "cloudfront_distribution_id": "E123EXAMPLE",
    "agentcore_runtime_arn": _RUNTIME_ARN,
    "dynamodb_table_names": {"eval_runs": "crm-agent-evaleval_runs"},
}


class _FakeLambda:
    def __init__(self, timeouts: dict[str, int]) -> None:
        self.timeouts = timeouts

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        return {"FunctionName": FunctionName, "Timeout": self.timeouts[FunctionName]}


class _FakeCloudFront:
    def __init__(self, read_timeout: int, *, hosts: tuple[str, ...] = (_RELAY_HOST,)) -> None:
        self.read_timeout = read_timeout
        self.hosts = hosts

    def get_distribution_config(self, *, Id: str) -> dict[str, Any]:  # noqa: N803
        origins: list[dict[str, Any]] = [
            {"Id": "s3-frontend-origin", "DomainName": "bucket.s3.amazonaws.com"}
        ]
        origins += [
            {
                "Id": "agent-relay-origin",
                "DomainName": host,
                "CustomOriginConfig": {"OriginReadTimeout": self.read_timeout},
            }
            for host in self.hosts
        ]
        return {"DistributionConfig": {"Origins": {"Quantity": len(origins), "Items": origins}}}


class _FakeControl:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    def get_agent_runtime(self, *, agentRuntimeId: str) -> dict[str, Any]:  # noqa: N803
        assert agentRuntimeId == "crm_agent_eval-AbC123"
        uri = f"{_REPO_URI}:{self.tag}"
        return {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": uri}}}


class _FakeEcr:
    def __init__(self, tags: frozenset[str]) -> None:
        self.tags = tags

    def describe_images(self, *, repositoryName: str, imageIds: list[dict[str, str]]) -> Any:  # noqa: N803
        assert repositoryName == "crm-agent-eval-agent"
        tag = imageIds[0]["imageTag"]
        if tag not in self.tags:
            raise RuntimeError("ImageNotFoundException")
        return {"imageDetails": [{"imageDigest": "sha256:feed", "imageTags": [tag]}]}


@pytest.fixture
def zip_path(tmp_path: Path) -> Path:
    path = tmp_path / "agent-source.zip"
    path.write_bytes(b"agent source bytes")
    return path


def _run(
    zip_path: Path,
    *,
    relay: int = module.RELAY_TIMEOUT_SECONDS,
    target: int = module.CHAT_TARGET_TIMEOUT_SECONDS,
    read_timeout: int = module.RELAY_ORIGIN_READ_TIMEOUT_SECONDS,
    tag: str | None = None,
    ecr_tags: frozenset[str] | None = None,
    outputs: dict[str, Any] | None = None,
    hosts: tuple[str, ...] = (_RELAY_HOST,),
) -> dict[str, CheckResult]:
    built = module.expected_image_tag(zip_path)
    ctx = context_for_test(
        outputs=_OUTPUTS if outputs is None else outputs,
        clients={
            "lambda": _FakeLambda({_RELAY: relay, _CHAT_TARGET: target}),
            "cloudfront": _FakeCloudFront(read_timeout, hosts=hosts),
            "bedrock-agentcore-control": _FakeControl(built if tag is None else tag),
            "ecr": _FakeEcr(frozenset({built}) if ecr_tags is None else ecr_tags),
        },
    )
    return {r.check: r for r in module.checks(ctx, agent_source_zip=zip_path)}


def test_a_compliant_deployment_passes_every_row_and_declares_the_600s_skip(zip_path: Path) -> None:
    rows = _run(zip_path)
    assert {c: r.status for c, r in rows.items()} == {
        "relay_timeout": "pass",
        "chat_target_timeout": "pass",
        "relay_origin_read_timeout": "pass",
        "runtime_image_is_the_built_one": "pass",
        "runtime_image_tag_in_ecr": "pass",
        "chat_600s_turn_end_to_end": "skip",
    }
    assert "not exercised" in rows["chat_600s_turn_end_to_end"].detail


@pytest.mark.parametrize(
    ("override", "red"),
    [
        ({"relay": 180}, "relay_timeout"),
        ({"target": 30}, "chat_target_timeout"),
        ({"read_timeout": 30}, "relay_origin_read_timeout"),
        (
            {"tag": "sha-000000000000", "ecr_tags": frozenset({"sha-000000000000"})},
            "runtime_image_is_the_built_one",
        ),
        ({"ecr_tags": frozenset()}, "runtime_image_tag_in_ecr"),
        ({"hosts": ()}, "relay_origin_read_timeout"),
        ({"hosts": (_RELAY_HOST, _RELAY_HOST)}, "relay_origin_read_timeout"),
    ],
    ids=["relay", "target", "origin", "image", "ecr", "no-origin", "two-origins"],
)
def test_each_deviation_reds_exactly_its_row(
    zip_path: Path, override: dict[str, Any], red: str
) -> None:
    rows = _run(zip_path, **override)
    assert [c for c, r in rows.items() if r.status == "fail"] == [red]


def test_an_absent_local_build_fails_rather_than_skipping(tmp_path: Path) -> None:
    missing = tmp_path / "absent.zip"
    ctx = context_for_test(
        outputs=_OUTPUTS,
        clients={
            "lambda": _FakeLambda({_RELAY: 660, _CHAT_TARGET: 660}),
            "cloudfront": _FakeCloudFront(60),
        },
    )
    rows = {r.check: r for r in module.checks(ctx, agent_source_zip=missing)}
    assert rows["runtime_image_is_the_built_one"].status == "fail"
    assert "package_lambdas.sh" in rows["runtime_image_is_the_built_one"].detail


def test_an_absent_output_fails_once(zip_path: Path) -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "agent_relay_function_name"}
    rows = _run(zip_path, outputs=outputs)
    assert list(rows) == ["terraform_output_present"]
    assert rows["terraform_output_present"].status == "fail"


def test_the_floor_matches_a_compliant_emission(zip_path: Path) -> None:
    ctx = context_for_test(outputs=_OUTPUTS, clients={})
    assert module.expected_check_count(ctx).minimum <= len(_run(zip_path))
    assert module.expected_check_count(context_for_test(outputs={}, clients={})).minimum == 1


def test_the_expected_tag_is_the_terraform_local_formula() -> None:
    text = (INFRA / "main.tf").read_text(encoding="utf-8")
    assert re.search(
        r'agent_image_tag\s*=\s*"sha-\$\{substr\(filesha256\("\$\{var\.lambda_build_dir\}'
        r'/agent-source\.zip"\), 0, 12\)\}"',
        text,
    )
    assert re.search(r'variable "lambda_build_dir"[^}]*default\s*=\s*"build/lambda"', text, re.S)


def _module_block(text: str, name: str) -> str:
    start = text.index(f'module "{name}" {{')
    end = text.index("\n}\n", start)
    return text[start:end]


def test_the_configured_values_equal_the_iac() -> None:
    """Each constant is the value the IaC sets, read from the file that sets it."""
    main_tf = (INFRA / "main.tf").read_text(encoding="utf-8")
    relay = re.search(
        r"^\s*timeout_seconds\s*=\s*(\d+)", _module_block(main_tf, "agent_relay"), re.M
    )
    assert relay is not None and int(relay.group(1)) == module.RELAY_TIMEOUT_SECONDS

    chat_tf = (INFRA / "mcp_chat_gateway.tf").read_text(encoding="utf-8")
    block = _module_block(chat_tf, "lambda_action_chat_target")
    target = re.search(r"^\s*timeout_seconds\s*=\s*(\d+)", block, re.M)
    assert target is not None and int(target.group(1)) == module.CHAT_TARGET_TIMEOUT_SECONDS
    assert re.search(
        rf'function_name\s*=\s*"\$\{{local\.name_prefix\}}{module.CHAT_TARGET_SUFFIX}"', block
    )

    cdn = (INFRA / "modules" / "frontend_cdn" / "cloudfront.tf").read_text(encoding="utf-8")
    relay_origin = cdn[cdn.index('origin_id                = "agent-relay-origin"') :]
    read = re.search(r"origin_read_timeout\s*=\s*(\d+)", relay_origin)
    assert read is not None and int(read.group(1)) == module.RELAY_ORIGIN_READ_TIMEOUT_SECONDS
