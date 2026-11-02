# SPDX-License-Identifier: MIT
"""Tests for ``scripts/remove_state_backend.py`` against ``moto``'s S3, DynamoDB, KMS and STS.

Every AWS call here is intercepted by ``mock_aws``; the session factory builds sessions with fake
explicit credentials, and no test reaches a real endpoint.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
import scripts.remove_state_backend as rsb
from botocore.exceptions import ClientError
from moto import mock_aws

REGION = "us-east-1"
ACCOUNT = "123456789012"
NAMES = rsb.backend_names(ACCOUNT, REGION)
LOCK_DIGEST_ID = f"{NAMES.state_bucket}/{rsb.STATE_KEY}{rsb.LOCK_DIGEST_SUFFIX}"
LIVE_LOCK_ID = f"{NAMES.state_bucket}/{rsb.STATE_KEY}"
_PLANTED_BY_MOTO = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
EMPTY_STATE = {"version": 4, "resources": []}
WORKSPACE_KEY = f"env:/staging/{rsb.STATE_KEY}"
PROJECT_TAG = [{"TagKey": "service", "TagValue": rsb.PROJECT}]
BASE_ARGV = ("--profile", "test-profile", "--region", REGION, "--yes")


@dataclass
class Backend:
    """A moto-backed copy of the bootstrap's resources plus a client per service."""

    s3: Any
    dynamodb: Any
    kms: Any
    key_id: str


def _session(profile: str = "unused", region: str = REGION) -> boto3.Session:
    del profile
    return boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # nosec B106  # fake moto/stub credentials, not a credential
        region_name=region,
    )


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``mock_aws`` with the static-credential env vars it plants removed again."""
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    with mock_aws():
        planted = {name: os.environ.pop(name, None) for name in _PLANTED_BY_MOTO}
        try:
            yield
        finally:
            for name, value in planted.items():
                if value is not None:
                    os.environ[name] = value


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(rsb, "_sleep", delays.append)
    return delays


def _make_bucket(s3: Any, name: str) -> None:
    s3.create_bucket(Bucket=name)
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})


def _make_backend(
    *,
    key_tags: list[dict[str, str]] | None = None,
    state: dict[str, Any] | None = None,
    lock_ids: tuple[str, ...] = (LOCK_DIGEST_ID,),
) -> Backend:
    session = _session()
    s3, dynamodb, kms = (session.client(name) for name in ("s3", "dynamodb", "kms"))
    key_id = _make_key(kms, key_tags if key_tags is not None else PROJECT_TAG)
    kms.create_alias(AliasName=NAMES.kms_alias, TargetKeyId=key_id)
    _make_bucket(s3, NAMES.log_bucket)
    _make_bucket(s3, NAMES.state_bucket)
    _put_state(s3, rsb.STATE_KEY, state if state is not None else EMPTY_STATE)
    s3.put_object(Bucket=NAMES.log_bucket, Key="log-1", Body=b"x")
    dynamodb.create_table(
        TableName=NAMES.lock_table,
        AttributeDefinitions=[{"AttributeName": "LockID", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    for lock_id in lock_ids:
        dynamodb.put_item(TableName=NAMES.lock_table, Item={"LockID": {"S": lock_id}})
    return Backend(s3=s3, dynamodb=dynamodb, kms=kms, key_id=key_id)


def _make_key(kms: Any, tags: list[dict[str, str]], description: str = rsb.KEY_DESCRIPTION) -> str:
    key_id: str = kms.create_key(Description=description, Tags=tags)["KeyMetadata"]["KeyId"]
    return key_id


def _put_state(s3: Any, key: str, state: dict[str, Any]) -> None:
    s3.put_object(Bucket=NAMES.state_bucket, Key=key, Body=json.dumps(state).encode())


def _key_state(kms: Any, key_id: str) -> str:
    state: str = kms.describe_key(KeyId=key_id)["KeyMetadata"]["KeyState"]
    return state


def _bucket_exists(s3: Any, name: str) -> bool:
    try:
        s3.head_bucket(Bucket=name)
    except ClientError:
        return False
    return True


def _table_exists(dynamodb: Any) -> bool:
    try:
        dynamodb.describe_table(TableName=NAMES.lock_table)
    except ClientError:
        return False
    return True


def _alias_exists(kms: Any) -> bool:
    aliases = kms.list_aliases()["Aliases"]
    return any(alias["AliasName"] == NAMES.kms_alias for alias in aliases)


def _assert_untouched(backend: Backend) -> None:
    assert _bucket_exists(backend.s3, NAMES.state_bucket)
    assert _bucket_exists(backend.s3, NAMES.log_bucket)
    assert _table_exists(backend.dynamodb)
    assert _alias_exists(backend.kms)
    assert _key_state(backend.kms, backend.key_id) == "Enabled"


def _forbidden_factory(profile: str, region: str) -> boto3.Session:
    raise AssertionError(f"a refusal must not build a session ({profile}, {region})")


def _read_report(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return report


def _register(session: boto3.Session, event: str, handler: Callable[..., Any]) -> None:
    session.events.register(event, handler)


def test_happy_path_removes_every_resource_including_over_1000_versions(
    aws: None, no_sleep: list[float], tmp_path: Path
) -> None:
    backend = _make_backend()
    for serial in range(1200):
        _put_state(backend.s3, rsb.STATE_KEY, {**EMPTY_STATE, "serial": serial})
    for index in range(50):
        workspace_key = f"env:/ws{index}/{rsb.STATE_KEY}"
        _put_state(backend.s3, workspace_key, EMPTY_STATE)
        backend.s3.delete_object(Bucket=NAMES.state_bucket, Key=workspace_key)
    batch_sizes: list[int] = []
    session = _session()

    def record(params: dict[str, Any], **_: Any) -> None:
        batch_sizes.append(len(params["Delete"]["Objects"]))
        assert params["Delete"]["Quiet"] is True

    _register(session, "provide-client-params.s3.DeleteObjects", record)
    report_path = tmp_path / "report.json"

    rc = rsb.main([*BASE_ARGV, "--json", str(report_path)], lambda _p, _r: session)

    assert rc == rsb.EXIT_OK
    assert not _bucket_exists(backend.s3, NAMES.state_bucket)
    assert not _bucket_exists(backend.s3, NAMES.log_bucket)
    assert not _table_exists(backend.dynamodb)
    assert not _alias_exists(backend.kms)
    assert _key_state(backend.kms, backend.key_id) == "PendingDeletion"
    assert max(batch_sizes) <= rsb.DELETE_BATCH_SIZE
    assert sum(batch_sizes) == 1 + 1200 + 50 + 50 + 1
    assert len(batch_sizes) >= 3
    report = _read_report(report_path)
    assert report["result"] == "ok"
    outcomes = {
        item["resource"] + ":" + item["name"]: item["outcome"] for item in report["resources"]
    }
    assert outcomes[f"kms-key:{backend.key_id}"] == "pending-deletion"
    assert outcomes[f"dynamodb-table:{NAMES.lock_table}"] == "deleted"
    assert ACCOUNT not in report_path.read_text(encoding="utf-8")
    assert no_sleep == []


def test_key_is_scheduled_with_a_seven_day_window(aws: None, no_sleep: list[float]) -> None:
    _make_backend()
    windows: list[int] = []
    session = _session()

    def record(params: dict[str, Any], **_: Any) -> None:
        windows.append(params["PendingWindowInDays"])

    _register(session, "provide-client-params.kms.ScheduleKeyDeletion", record)
    assert rsb.main(list(BASE_ARGV), lambda _p, _r: session) == rsb.EXIT_OK
    assert windows == [rsb.KEY_PENDING_WINDOW_DAYS] == [7]


def test_second_run_is_idempotent(aws: None, no_sleep: list[float], tmp_path: Path) -> None:
    _make_backend()
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_OK
    report_path = tmp_path / "second.json"

    assert rsb.main([*BASE_ARGV, "--json", str(report_path)], _session) == rsb.EXIT_OK

    outcomes = [item["outcome"] for item in _read_report(report_path)["resources"]]
    assert outcomes == ["absent"] * 5


def test_md5_digest_lock_items_are_allowed(aws: None, no_sleep: list[float]) -> None:
    backend = _make_backend(lock_ids=(LOCK_DIGEST_ID, f"other/{rsb.STATE_KEY}-md5"))
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_OK
    assert not _table_exists(backend.dynamodb)


def test_state_listing_resources_refuses_and_changes_nothing(
    aws: None, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _make_backend(state={"version": 4, "resources": [{"type": "a"}, {"type": "b"}]})
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_FAILED
    _assert_untouched(backend)
    assert "still lists 2 resource(s)" in capsys.readouterr().err


def test_malformed_state_object_refuses_and_changes_nothing(aws: None) -> None:
    backend = _make_backend()
    backend.s3.put_object(Bucket=NAMES.state_bucket, Key=rsb.STATE_KEY, Body=b"{not json")
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_FAILED
    _assert_untouched(backend)


def test_live_lock_refuses_and_points_to_force_unlock(
    aws: None, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _make_backend(lock_ids=(LOCK_DIGEST_ID, LIVE_LOCK_ID))
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_FAILED
    _assert_untouched(backend)
    assert "terraform force-unlock" in capsys.readouterr().err


@pytest.mark.parametrize(
    "tags",
    [
        [],
        [{"TagKey": "service", "TagValue": "something-else"}],
        [{"TagKey": "owner", "TagValue": rsb.PROJECT}],
    ],
    ids=["untagged", "wrong-value", "wrong-key"],
)
def test_key_with_wrong_tags_is_refused(aws: None, tags: list[dict[str, str]]) -> None:
    backend = _make_backend(key_tags=tags)
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_FAILED
    _assert_untouched(backend)


def test_key_identity_problem_rejects_aws_managed_keys() -> None:
    tags = [{"TagKey": "service", "TagValue": rsb.PROJECT}]
    assert rsb.key_identity_problem({"KeyManager": "CUSTOMER"}, tags) is None
    problem = rsb.key_identity_problem({"KeyManager": "AWS"}, tags)
    assert problem is not None and "CUSTOMER" in problem


@pytest.mark.parametrize(
    "argv",
    [
        ("--profile", "p", "--region", REGION),
        ("--region", REGION, "--yes"),
        ("--profile", "p", "--yes"),
        (),
    ],
    ids=["no-yes", "no-profile", "no-region", "nothing"],
)
def test_argument_refusals_exit_2_before_any_session(
    argv: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in rsb.STATIC_CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    report_path = tmp_path / "refused.json"
    assert rsb.main([*argv, "--json", str(report_path)], _forbidden_factory) == rsb.EXIT_REFUSED
    report = _read_report(report_path)
    assert report["result"] == "refused"
    assert "Nothing changed" in report["message"]


@pytest.mark.parametrize("variable", rsb.STATIC_CREDENTIAL_VARS)
def test_env_static_credentials_refuse(
    aws: None, variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _make_backend()
    monkeypatch.setenv(variable, "planted")
    assert rsb.main(list(BASE_ARGV), _forbidden_factory) == rsb.EXIT_REFUSED
    _assert_untouched(backend)


def test_sts_failure_refuses_and_changes_nothing(aws: None) -> None:
    backend = _make_backend()
    session = _session()

    def deny(**_: Any) -> None:
        raise ClientError(
            {"Error": {"Code": "ExpiredToken", "Message": "expired"}}, "GetCallerIdentity"
        )

    _register(session, "before-call.sts.GetCallerIdentity", deny)
    assert rsb.main(list(BASE_ARGV), lambda _p, _r: session) == rsb.EXIT_REFUSED
    _assert_untouched(backend)


def _refill_log_bucket(times: int | None) -> Callable[..., None]:
    """A ``provide-client-params`` hook writing a late log object before log-bucket deletes.

    ``times`` bounds how many deletes are preceded by a write; ``None`` writes before every one.
    """
    writer = _session().client("s3")
    calls = {"n": 0}

    def refill(params: dict[str, Any], **_: Any) -> None:
        if params["Bucket"] != NAMES.log_bucket:
            return
        calls["n"] += 1
        if times is None or calls["n"] <= times:
            writer.put_object(Bucket=NAMES.log_bucket, Key=f"late-{calls['n']}", Body=b"x")

    return refill


def test_bucket_not_empty_is_retried_until_the_bucket_goes(
    aws: None, no_sleep: list[float], tmp_path: Path
) -> None:
    backend = _make_backend()
    session = _session()
    _register(session, "provide-client-params.s3.DeleteBucket", _refill_log_bucket(times=2))
    report_path = tmp_path / "retry.json"

    rc = rsb.main([*BASE_ARGV, "--json", str(report_path)], lambda _p, _r: session)

    assert rc == rsb.EXIT_OK
    assert not _bucket_exists(backend.s3, NAMES.log_bucket)
    assert no_sleep == [rsb.BUCKET_NOT_EMPTY_DELAY_SECONDS] * 2
    details = [item["detail"] for item in _read_report(report_path)["resources"]]
    assert "attempts=3" in details


def test_bucket_not_empty_exhaustion_fails_with_rerun_later(
    aws: None, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _make_backend()
    session = _session()
    _register(session, "provide-client-params.s3.DeleteBucket", _refill_log_bucket(times=None))

    assert rsb.main(list(BASE_ARGV), lambda _p, _r: session) == rsb.EXIT_FAILED

    assert len(no_sleep) == rsb.BUCKET_NOT_EMPTY_ATTEMPTS - 1
    assert "re-run later" in capsys.readouterr().err
    assert not _bucket_exists(backend.s3, NAMES.state_bucket)
    assert _bucket_exists(backend.s3, NAMES.log_bucket)
    assert _table_exists(backend.dynamodb)
    assert _alias_exists(backend.kms)


def test_delete_objects_errors_fail_the_run(aws: None, no_sleep: list[float]) -> None:
    backend = _make_backend()
    session = _session()

    def partial_failure(http_response: Any, parsed: dict[str, Any], **_: Any) -> None:
        parsed["Errors"] = [{"Key": "k", "Code": "AccessDenied", "Message": "denied"}]

    _register(session, "after-call.s3.DeleteObjects", partial_failure)
    assert rsb.main(list(BASE_ARGV), lambda _p, _r: session) == rsb.EXIT_FAILED
    assert _bucket_exists(backend.s3, NAMES.state_bucket)
    assert _table_exists(backend.dynamodb)


def test_backend_names_match_the_documented_shape() -> None:
    expected = rsb.BackendNames(
        kms_alias="alias/crm-agent-eval-tfstate",
        state_bucket=f"crm-agent-eval-tfstate-{ACCOUNT}-{REGION}",
        log_bucket=f"crm-agent-eval-tfstate-logs-{ACCOUNT}-{REGION}",
        lock_table="crm-agent-eval-tflock",
    )
    assert expected == NAMES


def test_lock_ids_refusing_keeps_only_non_digest_items() -> None:
    assert rsb.lock_ids_refusing([LOCK_DIGEST_ID, LIVE_LOCK_ID]) == [LIVE_LOCK_ID]


def test_non_state_key_in_the_state_bucket_refuses(
    aws: None, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _make_backend()
    _put_state(backend.s3, "exports/copy.json", EMPTY_STATE)
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_FAILED
    _assert_untouched(backend)
    assert "non-state object" in capsys.readouterr().err


def test_workspace_state_listing_resources_refuses(
    aws: None, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _make_backend()
    _put_state(backend.s3, WORKSPACE_KEY, {"version": 4, "resources": [{"type": "a"}]})
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_FAILED
    _assert_untouched(backend)
    assert WORKSPACE_KEY in capsys.readouterr().err


def test_empty_workspace_state_is_allowed(aws: None, no_sleep: list[float]) -> None:
    backend = _make_backend()
    _put_state(backend.s3, WORKSPACE_KEY, EMPTY_STATE)
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_OK
    assert not _bucket_exists(backend.s3, NAMES.state_bucket)


def test_state_key_absent_but_bucket_non_empty_refuses(aws: None) -> None:
    backend = _make_backend()
    backend.s3.delete_object(Bucket=NAMES.state_bucket, Key=rsb.STATE_KEY)
    _put_state(backend.s3, WORKSPACE_KEY, EMPTY_STATE)
    assert rsb.main(list(BASE_ARGV), _session) == rsb.EXIT_FAILED
    _assert_untouched(backend)


def test_every_bucket_scoped_s3_call_pins_the_expected_owner(
    aws: None, no_sleep: list[float]
) -> None:
    _make_backend()
    session = _session()
    calls: list[tuple[str, str | None]] = []

    def record(params: dict[str, Any], event_name: str, **_: Any) -> None:
        calls.append((event_name.rsplit(".", 1)[-1], params.get("ExpectedBucketOwner")))

    _register(session, "provide-client-params.s3", record)
    assert rsb.main(list(BASE_ARGV), lambda _p, _r: session) == rsb.EXIT_OK
    operations = {name for name, _ in calls}
    assert {"ListObjectsV2", "GetObject", "ListObjectVersions", "DeleteObjects"} <= operations
    assert "DeleteBucket" in operations
    assert all(owner == ACCOUNT for _, owner in calls), calls


def test_crash_state_key_without_alias_is_found_by_tag_and_scheduled(
    aws: None, no_sleep: list[float], tmp_path: Path
) -> None:
    session = _session()
    kms = session.client("kms")
    key_id = _make_key(kms, PROJECT_TAG)
    decoy = _make_key(kms, PROJECT_TAG, description="some other key")
    report_path = tmp_path / "crash.json"

    assert rsb.main([*BASE_ARGV, "--json", str(report_path)], _session) == rsb.EXIT_OK

    assert _key_state(kms, key_id) == "PendingDeletion"
    assert _key_state(kms, decoy) == "Enabled"
    outcomes = {item["resource"]: item for item in _read_report(report_path)["resources"]}
    assert outcomes["kms-key"]["name"] == key_id
    assert outcomes["kms-key"]["outcome"] == "pending-deletion"
    assert outcomes["kms-alias"]["outcome"] == "absent"


def test_ambiguous_key_lookup_refuses_before_any_destructive_call(
    aws: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _make_backend()
    backend.kms.delete_alias(AliasName=NAMES.kms_alias)
    second = _make_key(backend.kms, PROJECT_TAG)
    report_path = tmp_path / "ambiguous.json"

    assert rsb.main([*BASE_ARGV, "--json", str(report_path)], _session) == rsb.EXIT_FAILED

    assert _bucket_exists(backend.s3, NAMES.state_bucket)
    assert _bucket_exists(backend.s3, NAMES.log_bucket)
    assert _table_exists(backend.dynamodb)
    assert _key_state(backend.kms, backend.key_id) == _key_state(backend.kms, second) == "Enabled"
    report = _read_report(report_path)
    assert report["result"] == "refused"
    assert report["resources"] == []
    assert "Nothing changed" in report["message"]
    assert "2 keys match" in capsys.readouterr().err


def test_unwritable_json_path_refuses_before_any_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in rsb.STATIC_CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    missing_dir = tmp_path / "absent" / "report.json"
    argv = [*BASE_ARGV, "--json", str(missing_dir)]
    assert rsb.main(argv, _forbidden_factory) == rsb.EXIT_REFUSED
    assert not missing_dir.parent.exists()


def test_malformed_lock_item_refuses_and_changes_nothing(aws: None) -> None:
    backend = _make_backend()
    session = _session()

    def corrupt(parsed: dict[str, Any], **_: Any) -> None:
        parsed["Items"] = [{"LockID": {"N": "1"}}]

    _register(session, "after-call.dynamodb.Scan", corrupt)
    assert rsb.main(list(BASE_ARGV), lambda _p, _r: session) == rsb.EXIT_FAILED
    _assert_untouched(backend)


def test_unexpected_action_exception_writes_a_partial_failed_report(
    aws: None, no_sleep: list[float], tmp_path: Path
) -> None:
    backend = _make_backend()
    session = _session()

    def explode(**_: Any) -> None:
        raise RuntimeError("injected")

    _register(session, "provide-client-params.dynamodb.DeleteTable", explode)
    report_path = tmp_path / "partial.json"

    rc = rsb.main([*BASE_ARGV, "--json", str(report_path)], lambda _p, _r: session)

    assert rc == rsb.EXIT_FAILED
    report = _read_report(report_path)
    assert report["result"] == "failed"
    assert "RuntimeError" in report["message"]
    assert [item["resource"] for item in report["resources"]] == ["s3-bucket", "s3-bucket"]
    assert _table_exists(backend.dynamodb)
    assert _alias_exists(backend.kms)


def test_bucket_gone_during_retry_is_absent_not_deleted(
    aws: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_backend()
    session = _session()
    _register(session, "provide-client-params.s3.DeleteBucket", _refill_log_bucket(times=1))
    outsider = _session().client("s3")

    def remove_out_of_band(_delay: float) -> None:
        versions = outsider.list_object_versions(Bucket=NAMES.log_bucket)
        for entry in [*versions.get("Versions", []), *versions.get("DeleteMarkers", [])]:
            outsider.delete_object(
                Bucket=NAMES.log_bucket, Key=entry["Key"], VersionId=entry["VersionId"]
            )
        outsider.delete_bucket(Bucket=NAMES.log_bucket)

    monkeypatch.setattr(rsb, "_sleep", remove_out_of_band)
    report_path = tmp_path / "gone.json"

    assert rsb.main([*BASE_ARGV, "--json", str(report_path)], lambda _p, _r: session) == 0

    log_outcome = _read_report(report_path)["resources"][1]
    assert (log_outcome["outcome"], log_outcome["detail"]) == ("absent", "attempts=2")


def test_client_error_escaping_the_preconditions_gives_a_nothing_changed_report(
    aws: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _make_backend()

    def escape(self: rsb.BackendRemover) -> None:
        raise ClientError({"Error": {"Code": "Throttling", "Message": "slow"}}, "ListKeys")

    monkeypatch.setattr(rsb.BackendRemover, "check_preconditions", escape)
    report_path = tmp_path / "escaped.json"

    assert rsb.main([*BASE_ARGV, "--json", str(report_path)], _session) == rsb.EXIT_FAILED

    _assert_untouched(backend)
    report = _read_report(report_path)
    assert report["result"] == "refused"
    assert report["resources"] == []
    assert "Nothing changed" in report["message"]
