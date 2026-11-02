# SPDX-License-Identifier: MIT
"""Offline tests for `scripts/scan_residue.py`: arguments, guards, exit codes, stdout and JSON.

The real scanner registry runs against an injected fake client factory whose operations return
configured pages (empty by default), so every family executes its real filtering code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from scripts import scan_residue
from scripts.residue import compute_network, global_services, regional_data

ACCOUNT = "123456789012"
CALLER = {"Account": ACCOUNT, "Arn": "arn:aws:iam::123456789012:user/smoke-test"}
BASE_ARGS = ["--profile", "test-profile", "--region", "us-east-1"]

Pages = list[dict[str, Any]] | Exception


def _denied(operation: str) -> ClientError:
    message = f"User: arn:aws:sts::{ACCOUNT}:federated-user/negctl is not authorized"
    return ClientError({"Error": {"Code": "AccessDenied", "Message": message}}, operation)


class FakePaginator:
    def __init__(self, pages: Pages, calls: list[dict[str, Any]]) -> None:
        self._pages = pages
        self._calls = calls

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self._calls.append(kwargs)
        if isinstance(self._pages, Exception):
            raise self._pages
        yield from self._pages


class FakeClient:
    """Answers any operation from `responses[(service, op)]`, else with one empty page."""

    def __init__(self, service: str, responses: Mapping[tuple[str, str], Pages]) -> None:
        self._service = service
        self._responses = responses
        self.kwargs: dict[str, list[dict[str, Any]]] = {}

    def _pages(self, operation: str) -> Pages:
        return self._responses.get((self._service, operation), [{}])

    def get_paginator(self, operation: str) -> FakePaginator:
        return FakePaginator(self._pages(operation), self.kwargs.setdefault(operation, []))

    def __getattr__(self, operation: str) -> Any:
        def invoke(**kwargs: Any) -> dict[str, Any]:
            self.kwargs.setdefault(operation, []).append(kwargs)
            pages = self._pages(operation)
            if isinstance(pages, Exception):
                raise pages
            return pages[0]

        return invoke


class FakeFactory:
    """A `ClientFactory` that records each `(service, region)` request."""

    def __init__(self, responses: Mapping[tuple[str, str], Pages] | None = None) -> None:
        self.responses = dict(responses or {})
        self.requests: list[tuple[str, str]] = []
        self.clients: dict[str, FakeClient] = {}

    def __call__(self, service: str, region: str) -> FakeClient:
        self.requests.append((service, region))
        return self.clients.setdefault(service, FakeClient(service, self.responses))


@pytest.fixture(autouse=True)
def _no_static_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def _run(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    factory: FakeFactory | None = None,
    caller: Mapping[str, str] | None = CALLER,
) -> tuple[int, str, str]:
    code = scan_residue.main(argv, client_factory=factory or FakeFactory(), caller=caller)
    out, err = capsys.readouterr()
    assert ACCOUNT not in out
    return code, out, err


def _lambda_fn(name: str) -> dict[str, Any]:
    return {
        "FunctionName": name,
        "FunctionArn": f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{name}",
    }


def _tag_hits(*arns: str) -> list[dict[str, Any]]:
    return [{"ResourceTagMappingList": [{"ResourceARN": arn} for arn in arns]}]


@pytest.mark.parametrize(
    "argv", [["--region", "us-east-1"], ["--profile", "test-profile"]], ids=["profile", "region"]
)
def test_missing_profile_or_region_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    factory = FakeFactory()

    code, _, _ = _run(capsys, argv, factory)

    assert code == 2
    assert factory.requests == []


@pytest.mark.parametrize("var", ["AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"])
def test_static_env_credentials_are_refused_before_any_call(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    monkeypatch.setenv(var, "set-by-test")
    factory = FakeFactory()

    code, _, err = _run(capsys, BASE_ARGS, factory)

    assert code == 2
    assert var in err and "refusing" in err
    assert factory.requests == []


def test_unknown_family_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _run(capsys, [*BASE_ARGS, "--families", "lambda,nosuch"])

    assert code == 2
    assert "nosuch" in err


def test_empty_family_selection_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    factory = FakeFactory()

    code, _, _ = _run(capsys, [*BASE_ARGS, "--families", ","], factory)

    assert code == 3
    assert [s for s, _ in factory.requests if s != "sts"] == []


@pytest.mark.parametrize("tag", ["service", "=x", "service="])
def test_malformed_tag_is_a_usage_error(capsys: pytest.CaptureFixture[str], tag: str) -> None:
    code, _, _ = _run(capsys, [*BASE_ARGS, "--tag", tag])

    assert code == 2


def test_clean_account_exits_0_with_every_family_measured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(capsys, BASE_ARGS)

    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "caller: user (account resolved)"
    assert not [line for line in lines if line.startswith(("RESIDUE", "ACCEPTED", "STALE"))]
    measured = next(line for line in lines if line.startswith("measured: "))
    assert set(measured.removeprefix("measured: ").split(",")) == set(scan_residue.build_registry())
    assert lines[-1] == "exit 0"


def test_project_resource_is_residue_and_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    factory = FakeFactory(
        {("lambda", "list_functions"): [{"Functions": [_lambda_fn("crm-agent-eval-eniprobe-x")]}]}
    )

    code, out, _ = _run(capsys, BASE_ARGS, factory)

    assert code == 1
    assert "RESIDUE lambda function crm-agent-eval-eniprobe-x" in out.splitlines()


def test_accepted_residue_line_carries_its_reason_and_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sg = {"GroupName": "CloudFront-VPCOrigins-Service-SG", "GroupId": "sg-1", "OwnerId": ACCOUNT}
    factory = FakeFactory({("ec2", "describe_security_groups"): [{"SecurityGroups": [sg]}]})

    code, out, _ = _run(capsys, BASE_ARGS, factory)

    assert code == 0
    accepted = [line for line in out.splitlines() if line.startswith("ACCEPTED ")]
    assert len(accepted) == 1
    assert accepted[0].startswith(
        "ACCEPTED ec2 security-group CloudFront-VPCOrigins-Service-SG vpc-origins-service-sg:"
    )


def test_denied_family_exits_2_over_residue_and_redacts_the_account(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = FakeFactory(
        {
            ("iam", "list_roles"): _denied("ListRoles"),
            ("lambda", "list_functions"): [{"Functions": [_lambda_fn("crm-agent-eval-x")]}],
        }
    )

    code, out, _ = _run(capsys, BASE_ARGS, factory)

    assert code == 2
    lines = out.splitlines()
    assert "RESIDUE lambda function crm-agent-eval-x" in lines
    unmeasured = [line for line in lines if line.startswith("unmeasured: ")]
    assert len(unmeasured) == 1
    assert unmeasured[0].startswith("unmeasured: iam AccessDenied list_roles: ")
    assert scan_residue.ACCOUNT_PLACEHOLDER in unmeasured[0]


def test_failed_identity_lookup_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    factory = FakeFactory({("sts", "get_caller_identity"): _denied("GetCallerIdentity")})

    code, _, err = _run(capsys, BASE_ARGS, factory, caller=None)

    assert code == 2
    assert "caller identity" in err
    assert [s for s, _ in factory.requests] == ["sts"]


def test_identity_resolved_through_sts_when_not_injected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = FakeFactory({("sts", "get_caller_identity"): [dict(CALLER)]})

    code, out, _ = _run(capsys, BASE_ARGS, factory, caller=None)

    assert code == 0
    assert out.splitlines()[0] == "caller: user (account resolved)"


def test_a_scanner_crash_exits_2_not_1(capsys: pytest.CaptureFixture[str]) -> None:
    factory = FakeFactory({("lambda", "list_functions"): [{"Functions": [{"no": "name"}]}]})

    code, out, err = _run(capsys, BASE_ARGS, factory)

    assert code == 2
    assert "scan aborted" in err and "KeyError" in err
    assert "exit" not in out


def test_families_restricts_scanners_and_drops_tag_hits_of_unselected_families(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = FakeFactory(
        {
            ("resourcegroupstaggingapi", "get_resources"): _tag_hits(
                f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/crm-agent-evalaccounts",
                f"arn:aws:sqs:us-east-1:{ACCOUNT}:crm-agent-eval-queue",
            )
        }
    )

    code, out, _ = _run(capsys, [*BASE_ARGS, "--families", "lambda"], factory)

    assert code == 1
    assert {s for s, _ in factory.requests} == {"lambda", "resourcegroupstaggingapi"}
    residue = [line for line in out.splitlines() if line.startswith("RESIDUE ")]
    assert residue == ["RESIDUE sqs tagged-unscanned crm-agent-eval-queue"]


def test_custom_tag_reaches_the_tag_index_filter(capsys: pytest.CaptureFixture[str]) -> None:
    factory = FakeFactory()

    _run(capsys, [*BASE_ARGS, "--families", "lambda", "--tag", "project=other"], factory)

    calls = factory.clients["resourcegroupstaggingapi"].kwargs["get_resources"]
    assert calls[0]["TagFilters"] == [{"Key": "project", "Values": ["other"]}]


def test_json_report_schema(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    report_path = tmp_path / "scan.json"
    factory = FakeFactory(
        {
            ("lambda", "list_functions"): [{"Functions": [_lambda_fn("crm-agent-eval-x")]}],
            ("iam", "list_roles"): _denied("ListRoles"),
        }
    )

    code, _, _ = _run(capsys, [*BASE_ARGS, "--json", str(report_path)], factory)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert set(report) == {
        "schema_version",
        "region",
        "caller",
        "families",
        "residue",
        "accepted",
        "stale_tag_index",
        "exit_code",
    }
    assert report["schema_version"] == scan_residue.SCHEMA_VERSION
    assert report["region"] == "us-east-1"
    assert report["caller"] == {"account": ACCOUNT, "arn_type": "user"}
    assert report["exit_code"] == code == 2
    assert "iam" not in report["families"]["measured"]
    assert report["families"]["unmeasured"]["iam"]["code"] == "AccessDenied"
    assert report["families"]["unmeasured"]["iam"]["operation"] == "list_roles"
    assert report["residue"] == [
        {
            "family": "lambda",
            "kind": "function",
            "name": "crm-agent-eval-x",
            "arn": _lambda_fn("crm-agent-eval-x")["FunctionArn"],
            "region": "us-east-1",
            "detail": {},
        }
    ]


def test_unwritable_json_path_exits_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _, err = _run(capsys, [*BASE_ARGS, "--json", str(tmp_path / "missing" / "scan.json")])

    assert code == 2
    assert "cannot write" in err


@pytest.mark.parametrize(
    ("arn", "expected"),
    [
        ("arn:aws:iam::123456789012:user/smoke-test", "user"),
        (f"arn:aws:sts::{ACCOUNT}:assumed-role/r/s", "assumed-role"),
        (f"arn:aws:sts::{ACCOUNT}:federated-user/negctl", "federated-user"),
        (f"arn:aws:iam::{ACCOUNT}:root", "root"),
        ("not-an-arn", "other"),
    ],
)
def test_arn_type(arn: str, expected: str) -> None:
    assert scan_residue.arn_type(arn) == expected


def test_registry_is_the_union_of_the_three_modules() -> None:
    registry = scan_residue.build_registry()

    assert set(registry) == (
        set(regional_data.SCANNERS) | set(compute_network.SCANNERS) | set(global_services.SCANNERS)
    )
    assert len(registry) == (
        len(regional_data.SCANNERS) + len(compute_network.SCANNERS) + len(global_services.SCANNERS)
    )


def test_duplicate_family_key_is_an_error() -> None:
    with pytest.raises(ValueError, match="duplicate residue family 'lambda'"):
        scan_residue.merge_registries(
            compute_network.SCANNERS, {"lambda": compute_network.scan_lambda}
        )
    with pytest.raises(ValueError, match="duplicate ENUMERATED_ARN_TYPES family 'iam'"):
        scan_residue.merge_arn_types({"iam": frozenset()}, {"iam": frozenset({"role"})})
