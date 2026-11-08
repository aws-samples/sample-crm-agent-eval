# SPDX-License-Identifier: MIT
"""Tests for `scripts/rotate_mcp_client_secret.py`.

Cognito and STS are real botocore clients under `botocore.stub.Stubber`, so every call's operation
and parameters are validated against the service model; the token endpoint is a scripted in-process
transport. No network and no credentials.
"""

from __future__ import annotations

import base64
import datetime
import http.client
import json
import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import botocore.session
import pytest
from botocore.stub import Stubber
from scripts import rotate_mcp_client_secret as rot

ACCOUNT = "123456789012"
OTHER_ACCOUNT = "111122223333"
REGION = "us-east-1"
PROFILE = "crm-rot-test"
POOL_ID = f"{REGION}_TestPool1"
CLIENT_ID = "testmcpclientid0123456789"
DOMAIN = "crm-test-domain"
OLD_SECRET = "old-secret-value-SENTINEL-1111"  # gitleaks:allow  # nosec B105  # planted test sentinel, not a credential
NEW_SECRET = "new-secret-value-SENTINEL-2222"  # gitleaks:allow  # nosec B105  # planted test sentinel, not a credential
ACCESS_TOKEN = "access-token-SENTINEL-3333"  # gitleaks:allow  # nosec B105  # planted test sentinel, not a credential
OLD_ID = "secret-id-old"
NEW_ID = "secret-id-new"
CREATED = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
ENDPOINT = f"https://{DOMAIN}.auth.{REGION}.amazoncognito.com/oauth2/token"
POOL = {"UserPoolId": POOL_ID, "ClientId": CLIENT_ID}

Response = tuple[int, dict[str, Any]]
GRANTED: Response = (200, {"access_token": ACCESS_TOKEN, "token_type": "Bearer"})  # nosec B105  # fake token-endpoint response, not a credential
INVALID_CLIENT: Response = (400, {"error": "invalid_client"})


def _outputs(*, account: str = ACCOUNT, region: str = REGION) -> dict[str, Any]:
    return {
        "cognito_user_pool_id": {"value": POOL_ID},
        "cognito_mcp_client_id": {"value": CLIENT_ID},
        "cognito_domain": {"value": DOMAIN},
        "cognito_mcp_client_secret": {"value": OLD_SECRET, "sensitive": True},
        "reps_table_arn": {"value": f"arn:aws:dynamodb:{region}:{account}:table/crm-reps"},
    }


@dataclass
class ScriptedTokenEndpoint:
    """Answers each secret from its own queue; the last answer repeats once the queue drains."""

    answers: dict[str, list[Response]]
    seen: list[str] = field(default_factory=list)

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        assert url == ENDPOINT
        assert body == b"grant_type=client_credentials"
        client, _, secret = (
            base64.b64decode(headers["Authorization"].removeprefix("Basic "))
            .decode()
            .partition(":")
        )
        assert client == CLIENT_ID
        self.seen.append(secret)
        queue = self.answers[secret]
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, json.dumps(payload).encode()


class StubbedSession:
    """A session whose clients are real botocore clients under Stubber."""

    def __init__(self) -> None:
        core = botocore.session.Session()
        kwargs: dict[str, Any] = {
            "region_name": REGION,
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",  # nosec B105  # fake moto/stub credentials, not a credential
        }
        self.clients = {
            "cognito-idp": core.create_client("cognito-idp", **kwargs),
            "sts": core.create_client("sts", **kwargs),
        }
        self.stubs = {name: Stubber(client) for name, client in self.clients.items()}
        for stub in self.stubs.values():
            stub.activate()

    @property
    def idp(self) -> Stubber:
        return self.stubs["cognito-idp"]

    def client(self, name: str, region_name: str | None = None) -> Any:
        assert region_name in (None, REGION)
        return self.clients[name]

    def assert_drained(self) -> None:
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()


@pytest.fixture
def aws_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An isolated AWS config naming `PROFILE` (and `other`), with no ambient profile or region."""
    config = tmp_path / "aws-config"
    config.write_text(f"[profile {PROFILE}]\n[profile other]\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))
    for name in ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def session() -> Iterator[StubbedSession]:
    stubbed = StubbedSession()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.deactivate()


@dataclass
class Run:
    rc: int
    out: str
    err: str
    secret_out: Path
    sleeps: list[float]
    sessions: list[tuple[str, str]]


def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    session: StubbedSession | None,
    endpoint: ScriptedTokenEndpoint,
    *,
    outputs: dict[str, Any] | None = None,
    secret_out: Path | None = None,
    extra: list[str] | None = None,
) -> Run:
    outputs_path = tmp_path / "tf-outputs.json"
    outputs_path.write_text(json.dumps(outputs or _outputs()), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    target = secret_out or state / "mcp-client-secret.new"
    sleeps: list[float] = []
    sessions: list[tuple[str, str]] = []

    def factory(profile: str, region: str) -> Any:
        sessions.append((profile, region))
        if session is None:
            raise AssertionError("no AWS session may be built for this refusal")
        return session

    argv = ["--outputs-json", str(outputs_path), "--secret-out", str(target)]
    argv += extra if extra is not None else ["--profile", PROFILE, "--region", REGION]
    rc = rot.main(argv, session_factory=factory, transport=endpoint, sleep=sleeps.append)
    captured = capsys.readouterr()
    return Run(rc, captured.out, captured.err, target, sleeps, sessions)


def _expect_identity(session: StubbedSession, account: str = ACCOUNT) -> None:
    session.stubs["sts"].add_response(
        "get_caller_identity",
        {"Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/op/s", "UserId": "AROA"},
        {},
    )


def _expect_list(session: StubbedSession, ids: list[str], next_token: str | None = None) -> None:
    response: dict[str, Any] = {
        "ClientSecrets": [{"ClientSecretId": i, "ClientSecretCreateDate": CREATED} for i in ids]
    }
    params: dict[str, Any] = dict(POOL)
    if next_token:
        response["NextToken"] = next_token
    session.idp.add_response("list_user_pool_client_secrets", response, params)


def _expect_add(session: StubbedSession) -> None:
    session.idp.add_response(
        "add_user_pool_client_secret",
        {
            "ClientSecretDescriptor": {
                "ClientSecretId": NEW_ID,
                "ClientSecretValue": NEW_SECRET,
                "ClientSecretCreateDate": CREATED,
            }
        },
        dict(POOL),
    )


def _expect_delete(session: StubbedSession, secret_id: str) -> None:
    session.idp.add_response(
        "delete_user_pool_client_secret", {}, {**POOL, "ClientSecretId": secret_id}
    )


def _expect_delete_error(session: StubbedSession, secret_id: str) -> None:
    session.idp.add_client_error(
        "delete_user_pool_client_secret",
        service_error_code="TooManyRequestsException",
        expected_params={**POOL, "ClientSecretId": secret_id},
    )


def _assert_no_secret_printed(run: Run) -> None:
    for value in (OLD_SECRET, NEW_SECRET, ACCESS_TOKEN):
        assert value not in run.out
        assert value not in run.err


def _happy_endpoint() -> ScriptedTokenEndpoint:
    return ScriptedTokenEndpoint({NEW_SECRET: [GRANTED], OLD_SECRET: [GRANTED, INVALID_CLIENT]})


def test_rotation_adds_verifies_and_deletes_the_old_secret_by_id(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, OLD_ID)
    endpoint = _happy_endpoint()

    run = _run(tmp_path, capsys, session, endpoint)

    assert run.rc == 0, run.err
    session.assert_drained()
    assert endpoint.seen == [NEW_SECRET, OLD_SECRET, OLD_SECRET]
    assert run.secret_out.read_text(encoding="utf-8") == NEW_SECRET
    assert stat.S_IMODE(run.secret_out.stat().st_mode) == 0o600
    assert OLD_ID in run.out and NEW_ID in run.out and CLIENT_ID in run.out
    assert "HTTP 400 (invalid_client)" in run.out
    assert run.sessions == [(PROFILE, REGION)]
    _assert_no_secret_printed(run)


def test_profile_and_region_come_from_the_environment_when_no_flag_is_given(
    aws_config: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    session: StubbedSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_PROFILE", PROFILE)
    monkeypatch.setenv("AWS_REGION", REGION)
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, OLD_ID)

    run = _run(tmp_path, capsys, session, _happy_endpoint(), extra=[])

    assert run.rc == 0, run.err
    assert run.sessions == [(PROFILE, REGION)]
    assert "(environment)" in run.out


def test_flags_override_the_environment(
    aws_config: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    session: StubbedSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "other")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, OLD_ID)

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc == 0, run.err
    assert run.sessions == [(PROFILE, REGION)]


def test_an_unknown_profile_is_refused_before_any_session(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run(
        tmp_path,
        capsys,
        None,
        _happy_endpoint(),
        extra=["--profile", "not-configured", "--region", REGION],
    )

    assert run.rc == 1
    assert run.sessions == []
    assert "not-configured" in run.err


def test_a_region_other_than_the_outputs_is_refused_before_any_cognito_call(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    run = _run(
        tmp_path,
        capsys,
        session,
        _happy_endpoint(),
        extra=["--profile", PROFILE, "--region", "us-west-2"],
    )

    assert run.rc == 1
    assert "us-west-2" in run.err and REGION in run.err
    session.assert_drained()
    assert not run.secret_out.exists()


def test_an_account_other_than_the_outputs_is_refused_before_any_cognito_call(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session, OTHER_ACCOUNT)

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc == 1
    assert OTHER_ACCOUNT in run.err
    session.assert_drained()
    assert not run.secret_out.exists()


def test_outputs_naming_no_account_are_refused(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs = _outputs()
    del outputs["reps_table_arn"]

    run = _run(tmp_path, capsys, None, _happy_endpoint(), outputs=outputs)

    assert run.rc == 1
    assert "names no account" in run.err
    assert run.sessions == []


@pytest.mark.parametrize(
    "missing",
    [
        "cognito_user_pool_id",
        "cognito_mcp_client_id",
        "cognito_domain",
    ],
)
def test_each_required_output_is_refused_when_absent(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    outputs = _outputs()
    del outputs[missing]

    run = _run(tmp_path, capsys, None, _happy_endpoint(), outputs=outputs)

    assert run.rc == 1
    assert missing in run.err
    assert run.sessions == []
    _assert_no_secret_printed(run)


@pytest.mark.parametrize("count", [0, 2])
def test_anything_but_exactly_one_active_secret_is_refused_without_adding(
    aws_config: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    session: StubbedSession,
    count: int,
) -> None:
    _expect_identity(session)
    _expect_list(session, [f"secret-id-{n}" for n in range(count)])

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc == 1
    assert f"{count} active secret(s)" in run.err and "step list" in run.err
    session.assert_drained()
    assert not run.secret_out.exists()


def test_active_secrets_are_counted_across_pages(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID], next_token="page-2")  # nosec B106  # pagination token fixture, not a credential
    session.idp.add_response(
        "list_user_pool_client_secrets",
        {
            "ClientSecrets": [
                {"ClientSecretId": "secret-id-second", "ClientSecretCreateDate": CREATED}
            ]
        },
        {**POOL, "NextToken": "page-2"},
    )

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc == 1
    assert "2 active secret(s)" in run.err
    session.assert_drained()


def test_secret_out_inside_a_git_work_tree_is_refused(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()

    run = _run(tmp_path, capsys, None, _happy_endpoint(), secret_out=repo / "sub" / "secret")

    assert run.rc == 1
    assert "git work tree" in run.err
    assert run.sessions == []
    assert not run.secret_out.exists()


def test_secret_out_inside_this_repository_is_refused(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run(
        tmp_path, capsys, None, _happy_endpoint(), secret_out=rot.REPO_ROOT / "results" / "x.new"
    )

    assert run.rc == 1
    assert "git work tree" in run.err


def test_an_existing_secret_out_is_never_overwritten(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = tmp_path / "existing"
    existing.write_text("keep", encoding="utf-8")

    run = _run(tmp_path, capsys, None, _happy_endpoint(), secret_out=existing)

    assert run.rc == 1
    assert "already exists" in run.err
    assert existing.read_text(encoding="utf-8") == "keep"
    assert run.sessions == []


def test_a_dangling_symlink_at_secret_out_is_refused(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "nowhere")

    run = _run(tmp_path, capsys, None, _happy_endpoint(), secret_out=link)

    assert run.rc == 1
    assert "already exists" in run.err
    assert not (tmp_path / "nowhere").exists()


def test_a_missing_secret_out_directory_is_refused(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run(tmp_path, capsys, None, _happy_endpoint(), secret_out=tmp_path / "absent" / "s")

    assert run.rc == 1
    assert "does not exist" in run.err
    assert run.sessions == []


def test_a_list_client_error_names_the_step(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    session.idp.add_client_error(
        "list_user_pool_client_secrets",
        service_error_code="NotAuthorizedException",
        expected_params=dict(POOL),
    )

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc == 1
    assert "step list" in run.err and "NotAuthorizedException" in run.err
    session.assert_drained()


def test_an_add_client_error_names_the_step_and_leaves_no_file(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    session.idp.add_client_error(
        "add_user_pool_client_secret",
        service_error_code="LimitExceededException",
        expected_params=dict(POOL),
    )

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc == 1
    assert "step add" in run.err and f"Old secret {OLD_ID} untouched" in run.err
    session.assert_drained()
    assert not run.secret_out.exists()


def test_a_new_secret_that_never_mints_is_deleted_and_the_old_one_kept(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, NEW_ID)
    endpoint = ScriptedTokenEndpoint({NEW_SECRET: [INVALID_CLIENT], OLD_SECRET: [GRANTED]})

    run = _run(tmp_path, capsys, session, endpoint)

    assert run.rc == 1
    assert "step mint-new" in run.err
    assert f"New secret {NEW_ID} deleted; old secret {OLD_ID} intact" in run.err
    session.assert_drained()
    assert endpoint.seen == [NEW_SECRET] * rot.PROPAGATION_ATTEMPTS
    assert run.sleeps == [rot.PROPAGATION_DELAY_SECONDS] * (rot.PROPAGATION_ATTEMPTS - 1)
    assert not run.secret_out.exists()
    _assert_no_secret_printed(run)


def test_a_new_secret_that_mints_after_propagation_completes_the_rotation(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, OLD_ID)
    endpoint = ScriptedTokenEndpoint(
        {
            NEW_SECRET: [INVALID_CLIENT, INVALID_CLIENT, GRANTED],
            OLD_SECRET: [GRANTED, GRANTED, INVALID_CLIENT],
        }
    )

    run = _run(tmp_path, capsys, session, endpoint)

    assert run.rc == 0, run.err
    session.assert_drained()
    assert endpoint.seen == [NEW_SECRET] * 3 + [OLD_SECRET] * 3
    assert len(run.sleeps) == 3


def test_a_failed_rollback_reports_both_secrets_active(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete_error(session, NEW_ID)
    endpoint = ScriptedTokenEndpoint({NEW_SECRET: [INVALID_CLIENT], OLD_SECRET: [GRANTED]})

    run = _run(tmp_path, capsys, session, endpoint)

    assert run.rc == 1
    assert "ALSO failed" in run.err and f"both {OLD_ID} and {NEW_ID} remain active" in run.err
    session.assert_drained()
    assert run.secret_out.read_text(encoding="utf-8") == NEW_SECRET
    _assert_no_secret_printed(run)


def test_an_unreachable_endpoint_on_the_new_mint_rolls_back(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, NEW_ID)

    def unreachable(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        raise TimeoutError("timed out")

    outputs_path = tmp_path / "tf-outputs.json"
    outputs_path.write_text(json.dumps(_outputs()), encoding="utf-8")
    secret_out = tmp_path / "secret.new"
    rc = rot.main(
        [
            "--outputs-json",
            str(outputs_path),
            "--secret-out",
            str(secret_out),
            "--profile",
            PROFILE,
            "--region",
            REGION,
        ],
        session_factory=lambda _profile, _region: session,
        transport=unreachable,
        sleep=lambda _seconds: None,
    )
    err = capsys.readouterr().err

    assert rc == 1
    assert "step mint-new" in err and "unreachable" in err and "intact" in err
    session.assert_drained()
    assert not secret_out.exists()


@pytest.mark.parametrize(
    "failure",
    [http.client.IncompleteRead(b"{"), http.client.BadStatusLine("garbage")],
    ids=["incomplete-read", "bad-status-line"],
)
def test_a_broken_http_answer_on_the_new_mint_rolls_back(
    tmp_path: Path, session: StubbedSession, failure: http.client.HTTPException
) -> None:
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, NEW_ID)
    secret_out = tmp_path / "secret.new"

    def broken(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        raise failure

    with pytest.raises(rot.RotationError) as raised:
        rot.rotate(
            session.clients["cognito-idp"],
            rot.RotationTarget(POOL_ID, CLIENT_ID, DOMAIN, OLD_SECRET),
            region=REGION,
            secret_out=secret_out,
            transport=broken,
            sleep=lambda _seconds: None,
            log=lambda _line: None,
        )

    message = str(raised.value)
    assert "step mint-new" in message and type(failure).__name__ in message
    assert f"New secret {NEW_ID} deleted; old secret {OLD_ID} intact" in message
    session.assert_drained()
    assert not secret_out.exists()


def _breaks_after(answers: list[Response | http.client.HTTPException]) -> rot.TokenTransport:
    """A transport that replays ``answers`` in order, raising any exception entry."""
    queue = list(answers)

    def transport(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        answer = queue.pop(0)
        if isinstance(answer, http.client.HTTPException):
            raise answer
        status, payload = answer
        return status, json.dumps(payload).encode()

    return transport


def test_a_broken_http_answer_on_the_old_mint_names_the_step_and_both_active(
    tmp_path: Path, session: StubbedSession
) -> None:
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    secret_out = tmp_path / "secret.new"

    with pytest.raises(rot.RotationError) as raised:
        rot.rotate(
            session.clients["cognito-idp"],
            rot.RotationTarget(POOL_ID, CLIENT_ID, DOMAIN, OLD_SECRET),
            region=REGION,
            secret_out=secret_out,
            transport=_breaks_after([GRANTED, http.client.IncompleteRead(b"{")]),
            sleep=lambda _seconds: None,
            log=lambda _line: None,
        )

    message = str(raised.value)
    assert message.startswith("step mint-old:")
    assert f"Both {OLD_ID} and {NEW_ID} remain active; the new value is in {secret_out}" in message
    session.assert_drained()
    assert secret_out.read_text(encoding="utf-8") == NEW_SECRET


def test_a_broken_http_answer_on_the_refusal_check_names_the_step_and_the_new_value(
    tmp_path: Path, session: StubbedSession
) -> None:
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, OLD_ID)
    secret_out = tmp_path / "secret.new"

    with pytest.raises(rot.RotationError) as raised:
        rot.rotate(
            session.clients["cognito-idp"],
            rot.RotationTarget(POOL_ID, CLIENT_ID, DOMAIN, OLD_SECRET),
            region=REGION,
            secret_out=secret_out,
            transport=_breaks_after([GRANTED, GRANTED, http.client.IncompleteRead(b"{")]),
            sleep=lambda _seconds: None,
            log=lambda _line: None,
        )

    message = str(raised.value)
    assert message.startswith("step verify-old-refused:")
    assert f"Secret {NEW_ID} is active; the new value is in {secret_out}" in message
    session.assert_drained()
    assert secret_out.read_text(encoding="utf-8") == NEW_SECRET


def test_an_old_secret_that_does_not_mint_stops_before_any_delete(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    endpoint = ScriptedTokenEndpoint({NEW_SECRET: [GRANTED], OLD_SECRET: [INVALID_CLIENT]})

    run = _run(tmp_path, capsys, session, endpoint)

    assert run.rc == 1
    assert "step mint-old" in run.err
    assert f"Both {OLD_ID} and {NEW_ID} remain active" in run.err
    session.assert_drained()
    assert run.secret_out.read_text(encoding="utf-8") == NEW_SECRET


def test_a_delete_old_client_error_names_the_step(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete_error(session, OLD_ID)

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc == 1
    assert "step delete-old" in run.err and "remain active" in run.err
    session.assert_drained()
    assert run.secret_out.read_text(encoding="utf-8") == NEW_SECRET


def test_an_old_secret_still_accepted_after_delete_fails_the_run(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, OLD_ID)
    endpoint = ScriptedTokenEndpoint({NEW_SECRET: [GRANTED], OLD_SECRET: [GRANTED]})

    run = _run(tmp_path, capsys, session, endpoint)

    assert run.rc == 1
    assert "step verify-old-refused" in run.err
    session.assert_drained()
    assert endpoint.seen.count(OLD_SECRET) == 1 + rot.OLD_SECRET_REFUSAL_ATTEMPTS
    assert run.sleeps == [rot.OLD_SECRET_REFUSAL_DELAY_SECONDS] * (
        rot.OLD_SECRET_REFUSAL_ATTEMPTS - 1
    )
    _assert_no_secret_printed(run)


def test_an_old_secret_refused_only_after_minutes_completes_the_rotation(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, OLD_ID)
    still_accepted = 19
    endpoint = ScriptedTokenEndpoint(
        {NEW_SECRET: [GRANTED], OLD_SECRET: [GRANTED] * (1 + still_accepted) + [INVALID_CLIENT]}
    )

    run = _run(tmp_path, capsys, session, endpoint)

    assert run.rc == 0, run.err
    session.assert_drained()
    assert endpoint.seen.count(OLD_SECRET) == 1 + still_accepted + 1
    assert sum(run.sleeps) == still_accepted * rot.OLD_SECRET_REFUSAL_DELAY_SECONDS
    assert sum(run.sleeps) > 180.0
    assert sum(run.sleeps) <= 300.0
    _assert_no_secret_printed(run)


def test_the_refusal_window_spans_five_minutes_and_the_mint_window_is_unchanged() -> None:
    assert (rot.OLD_SECRET_REFUSAL_ATTEMPTS - 1) * rot.OLD_SECRET_REFUSAL_DELAY_SECONDS <= 300.0
    assert rot.OLD_SECRET_REFUSAL_ATTEMPTS * rot.OLD_SECRET_REFUSAL_DELAY_SECONDS >= 300.0
    assert (rot.PROPAGATION_ATTEMPTS, rot.PROPAGATION_DELAY_SECONDS) == (6, 5.0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory write permission")
def test_a_failed_secret_write_rolls_the_new_secret_back(
    tmp_path: Path, session: StubbedSession
) -> None:
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, NEW_ID)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    target = rot.RotationTarget(POOL_ID, CLIENT_ID, DOMAIN, OLD_SECRET)
    logged: list[str] = []
    try:
        with pytest.raises(rot.RotationError) as raised:
            rot.rotate(
                session.clients["cognito-idp"],
                target,
                region=REGION,
                secret_out=locked / "secret.new",
                transport=_happy_endpoint(),
                sleep=lambda _seconds: None,
                log=logged.append,
            )
    finally:
        locked.chmod(0o700)

    assert "step write" in str(raised.value) and "intact" in str(raised.value)
    session.assert_drained()
    assert not (locked / "secret.new").exists()


def test_a_file_created_after_validation_is_neither_overwritten_nor_removed(
    tmp_path: Path, session: StubbedSession
) -> None:
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, NEW_ID)
    racer = tmp_path / "secret.new"
    racer.write_text("someone else's", encoding="utf-8")
    target = rot.RotationTarget(POOL_ID, CLIENT_ID, DOMAIN, OLD_SECRET)

    with pytest.raises(rot.RotationError) as raised:
        rot.rotate(
            session.clients["cognito-idp"],
            target,
            region=REGION,
            secret_out=racer,
            transport=_happy_endpoint(),
            sleep=lambda _seconds: None,
            log=lambda _line: None,
        )

    assert "step write" in str(raised.value) and "FileExistsError" in str(raised.value)
    session.assert_drained()
    assert racer.read_text(encoding="utf-8") == "someone else's"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, json.dumps({"access_token": ACCESS_TOKEN}).encode(), rot.MintResult(200, None)),
        (200, b"{}", rot.MintResult(200, "no_access_token")),
        (
            400,
            json.dumps({"error": "invalid_client"}).encode(),
            rot.MintResult(400, "invalid_client"),
        ),
        (502, b"<html>bad gateway</html>", rot.MintResult(502, "unparseable_response")),
        (500, b"\xff\xfe", rot.MintResult(500, "unparseable_response")),
    ],
)
def test_mint_classifies_the_token_endpoint_answer(
    status: int, body: bytes, expected: rot.MintResult
) -> None:
    def transport(url: str, data: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        return status, body

    result = rot.mint(transport, ENDPOINT, CLIENT_ID, OLD_SECRET)

    assert result == expected
    assert result.ok is (expected.error is None and status == 200)
    assert ACCESS_TOKEN not in result.describe()


def test_https_transport_refuses_a_non_https_url() -> None:
    from scripts.smoke.harness import InsecureSchemeError

    with pytest.raises(InsecureSchemeError):
        rot.https_transport("http://example.invalid/oauth2/token", b"", {})


def test_the_token_endpoint_is_derived_from_the_domain_and_region() -> None:
    target = rot.target_from_outputs(_outputs())

    assert target.token_endpoint(REGION) == ENDPOINT
    assert target.client_id == CLIENT_ID and target.user_pool_id == POOL_ID


def _with_secret(value: str) -> dict[str, Any]:
    outputs = _outputs()
    outputs["cognito_mcp_client_secret"] = {"value": value, "sensitive": True}
    return outputs


def _old_secret_args(path: Path) -> list[str]:
    return ["--profile", PROFILE, "--region", REGION, "--old-secret-file", str(path)]


def _rotated_with(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    session: StubbedSession,
    outputs: dict[str, Any],
    old_secret_file: Path,
) -> Run:
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_delete(session, OLD_ID)
    endpoint = _happy_endpoint()
    run = _run(
        tmp_path,
        capsys,
        session,
        endpoint,
        outputs=outputs,
        extra=_old_secret_args(old_secret_file),
    )
    assert run.rc == 0, run.err
    session.assert_drained()
    assert endpoint.seen == [NEW_SECRET, OLD_SECRET, OLD_SECRET]
    return run


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
def test_an_empty_output_secret_with_no_file_is_a_usage_error_naming_the_flag(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    run = _run(tmp_path, capsys, None, _happy_endpoint(), outputs=_with_secret(value))

    assert run.rc == 2
    assert "--old-secret-file" in run.err
    assert run.sessions == []
    assert not run.secret_out.exists()


def test_an_absent_output_secret_with_no_file_is_a_usage_error_naming_the_flag(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs = _outputs()
    del outputs["cognito_mcp_client_secret"]

    run = _run(tmp_path, capsys, None, _happy_endpoint(), outputs=outputs)

    assert run.rc == 2
    assert "--old-secret-file" in run.err
    assert run.sessions == []


def test_an_empty_output_secret_rotates_with_the_old_secret_file(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    old_file = tmp_path / "mcp-client-secret.prev"
    old_file.write_text(f"{OLD_SECRET}\n", encoding="utf-8")

    run = _rotated_with(tmp_path, capsys, session, _with_secret(""), old_file)

    _assert_no_secret_printed(run)


def test_an_old_secret_file_agreeing_with_the_output_is_accepted(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], session: StubbedSession
) -> None:
    old_file = tmp_path / "mcp-client-secret.prev"
    old_file.write_text(OLD_SECRET, encoding="utf-8")

    run = _rotated_with(tmp_path, capsys, session, _outputs(), old_file)

    _assert_no_secret_printed(run)


def test_an_old_secret_file_that_disagrees_with_the_output_is_a_usage_error(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old_file = tmp_path / "mcp-client-secret.prev"
    old_file.write_text(NEW_SECRET, encoding="utf-8")

    run = _run(tmp_path, capsys, None, _happy_endpoint(), extra=_old_secret_args(old_file))

    assert run.rc == 2
    assert "different secrets" in run.err and "--old-secret-file" in run.err
    assert run.sessions == []
    _assert_no_secret_printed(run)


@pytest.mark.parametrize("contents", [None, "", " \n"], ids=["missing", "empty", "blank"])
def test_an_unreadable_or_empty_old_secret_file_is_a_usage_error(
    aws_config: None, tmp_path: Path, capsys: pytest.CaptureFixture[str], contents: str | None
) -> None:
    old_file = tmp_path / "mcp-client-secret.prev"
    if contents is not None:
        old_file.write_text(contents, encoding="utf-8")

    run = _run(
        tmp_path,
        capsys,
        None,
        _happy_endpoint(),
        outputs=_with_secret(""),
        extra=_old_secret_args(old_file),
    )

    assert run.rc == 2
    assert "--old-secret-file" in run.err
    assert run.sessions == []


def _output_members(operation: str) -> dict[str, Any]:
    model = botocore.session.Session().get_service_model("cognito-idp")
    return dict(model.operation_model(operation).output_shape.members)


def test_the_fixtures_follow_the_installed_cognito_output_shapes() -> None:
    added = _output_members("AddUserPoolClientSecret")
    assert list(added) == ["ClientSecretDescriptor"]
    descriptor = set(added["ClientSecretDescriptor"].members)
    assert {"ClientSecretId", "ClientSecretValue", "ClientSecretCreateDate"} <= descriptor
    listed = _output_members("ListUserPoolClientSecrets")
    assert set(listed["ClientSecrets"].member.members) == descriptor


class _FlatteningIdp:
    """Passes every call to the stubbed client; flattens the Add response the way a reshape would."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def add_user_pool_client_secret(self, **kwargs: Any) -> dict[str, Any]:
        response = self._inner.add_user_pool_client_secret(**kwargs)
        return dict(response.pop("ClientSecretDescriptor"))


class _FlatteningSession(StubbedSession):
    def client(self, name: str, region_name: str | None = None) -> Any:
        real = super().client(name, region_name)
        return _FlatteningIdp(real) if name == "cognito-idp" else real


@pytest.fixture
def flattening_session() -> Iterator[StubbedSession]:
    stubbed = _FlatteningSession()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.deactivate()


def test_an_unreadable_add_response_deletes_the_new_secret_and_writes_no_file(
    aws_config: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flattening_session: StubbedSession,
) -> None:
    session = flattening_session
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_list(session, [OLD_ID, NEW_ID])
    _expect_delete(session, NEW_ID)
    endpoint = _happy_endpoint()

    run = _run(tmp_path, capsys, session, endpoint)

    assert run.rc != 0
    session.assert_drained()
    assert "step add" in run.err and f"deleted {NEW_ID}" in run.err
    assert f"old secret {OLD_ID} intact" in run.err
    assert not run.secret_out.exists()
    assert endpoint.seen == []
    _assert_no_secret_printed(run)


def test_an_unreadable_add_response_whose_rollback_fails_names_the_remaining_secret(
    aws_config: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flattening_session: StubbedSession,
) -> None:
    session = flattening_session
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    _expect_list(session, [OLD_ID, NEW_ID])
    _expect_delete_error(session, NEW_ID)

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc != 0
    session.assert_drained()
    assert "ALSO failed" in run.err and f"{NEW_ID} ALSO failed" in run.err
    assert not run.secret_out.exists()
    _assert_no_secret_printed(run)


def test_an_unreadable_add_response_whose_lookup_fails_warns_of_a_possible_orphan(
    aws_config: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flattening_session: StubbedSession,
) -> None:
    session = flattening_session
    _expect_identity(session)
    _expect_list(session, [OLD_ID])
    _expect_add(session)
    session.idp.add_client_error(
        "list_user_pool_client_secrets",
        service_error_code="TooManyRequestsException",
        expected_params=dict(POOL),
    )

    run = _run(tmp_path, capsys, session, _happy_endpoint())

    assert run.rc != 0
    session.assert_drained()
    assert "may remain active" in run.err
    assert not run.secret_out.exists()
    _assert_no_secret_printed(run)
