# SPDX-License-Identifier: MIT
"""Tests for `scripts/migrate_supervisor_effort.py` and `Store.scan_key_prefix`, offline.

The CLI runs under moto with an isolated AWS config naming one profile; the store method's
pagination and error translation run against a scripted client.
"""

from __future__ import annotations

import dataclasses
import io
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from scripts import migrate_supervisor_effort as mse

from crm.errors import Conflict, CrmError, TableNotFound
from crm.store.keys import user_settings_key
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.user_settings import SETTINGS_KIND, SETTINGS_SOURCE
from providers.capabilities import load_capability_matrix

MATRIX = load_capability_matrix()
PREFIX = "crm-agent-eval"
ACCOUNT = "123456789012"
OTHER_ACCOUNT = "111122223333"
REGION = "us-east-1"
PROFILE = "crm-mse-test"
#: A rep id and tuning value that must never reach the printed summary.
IDENTIFYING_REP_ID = "rep-identifying-sample"

SUMMARY_KEYS = (
    "dry_run",
    "partial",
    "scanned",
    "eligible",
    "migrated",
    "conflict",
    "already_medium",
    "unchanged",
    "skipped_unresolvable",
    "skipped_not_settings",
)


def _settings_item(rep_id: str, effort: str | None) -> dict[str, object]:
    return {
        "rep_id": user_settings_key("demo", rep_id),
        "kind": SETTINGS_KIND,
        "settings_tenant": "demo",
        "owner_rep_id": rep_id,
        "tuning": {
            "schema": 3,
            "source": SETTINGS_SOURCE,
            "tiers": {
                "supervisor": {
                    "family": "anthropic",
                    "model": "anthropic.claude-opus-5-5",
                    "effort": effort,
                }
            },
        },
        "schema_version": 2,
        "updated_at": "2026-09-01T10:00:00+00:00",
    }


def _outputs(*, account: str = ACCOUNT, region: str = REGION) -> dict[str, Any]:
    return {
        "dynamodb_table_names": {
            "value": {"eval_runs": f"{PREFIX}eval_runs", "reps": f"{PREFIX}reps"}
        },
        "reps_table_arn": {"value": f"arn:aws:dynamodb:{region}:{account}:table/{PREFIX}reps"},
    }


@pytest.fixture
def aws_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An isolated AWS config naming only `PROFILE`, under moto, with one settings item at low."""
    config = tmp_path / "config"
    config.write_text(f"[profile {PROFILE}]\nregion = {REGION}\n", encoding="utf-8")
    credentials = tmp_path / "credentials"
    credentials.write_text(
        f"[{PROFILE}]\naws_access_key_id = testing\naws_secret_access_key = testing\n",
        encoding="utf-8",
    )
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        create_all_tables(client, table_prefix=PREFIX)
        Store(client=client, table_prefix=PREFIX).put_item(
            "reps", _settings_item(IDENTIFYING_REP_ID, "low")
        )
        yield


def _supervisor_effort() -> object:
    item = Store(client=boto3.client("dynamodb", region_name=REGION), table_prefix=PREFIX).get_item(
        "reps", {"rep_id": user_settings_key("demo", IDENTIFYING_REP_ID)}
    )
    assert item is not None
    return mse._supervisor_effort(item["tuning"])


def _outputs_file(tmp_path: Path, outputs: object) -> str:
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(outputs), encoding="utf-8")
    return str(path)


def _cli(outputs_path: str, *extra: str) -> list[str]:
    return ["--outputs-json", outputs_path, "--profile", PROFILE, "--region", REGION, *extra]


class TestArguments:
    def test_the_default_is_a_dry_run(self) -> None:
        args = mse.build_arg_parser().parse_args(_cli("o.json"))
        assert args.apply is False
        assert args.table_prefix is None

    def test_apply_and_table_prefix_are_parsed(self) -> None:
        args = mse.build_arg_parser().parse_args(_cli("o.json", "--apply", "--table-prefix", "p-"))
        assert (args.apply, args.table_prefix) == (True, "p-")

    @pytest.mark.parametrize("missing", ["--profile", "--region", "--outputs-json"])
    def test_every_required_flag_is_required(self, missing: str) -> None:
        argv = _cli("o.json")
        index = argv.index(missing)
        del argv[index : index + 2]
        with pytest.raises(SystemExit) as raised:
            mse.main(argv)
        assert raised.value.code == 2

    def test_an_unknown_flag_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as raised:
            mse.main(_cli("o.json", "--force"))
        assert raised.value.code == 2


class TestCli:
    @pytest.mark.usefixtures("aws_env")
    def test_a_dry_run_exits_zero_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert mse.main(_cli(_outputs_file(tmp_path, _outputs()))) == 0
        assert json.loads(capsys.readouterr().out)["eligible"] == 1
        assert _supervisor_effort() == "low"

    @pytest.mark.usefixtures("aws_env")
    def test_apply_exits_zero_and_migrates(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert mse.main(_cli(_outputs_file(tmp_path, _outputs()), "--apply")) == 0
        assert json.loads(capsys.readouterr().out)["migrated"] == 1
        assert _supervisor_effort() == "medium"

    @pytest.mark.usefixtures("aws_env")
    def test_outputs_are_read_from_stdin_with_a_dash(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_outputs())))
        assert mse.main(_cli("-")) == 0
        assert json.loads(capsys.readouterr().out)["scanned"] == 1

    @pytest.mark.usefixtures("aws_env")
    def test_the_summary_is_counts_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert mse.main(_cli(_outputs_file(tmp_path, _outputs()), "--apply")) == 0
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert tuple(summary) == SUMMARY_KEYS
        assert isinstance(summary["dry_run"], bool) and summary["partial"] is False
        assert all(
            isinstance(v, int) and not isinstance(v, bool)
            for k, v in summary.items()
            if k not in ("dry_run", "partial")
        )
        for leaked in (IDENTIFYING_REP_ID, "usersettings", "anthropic", "opus", "low"):
            assert leaked not in captured.out + captured.err

    @pytest.mark.usefixtures("aws_env")
    @pytest.mark.parametrize(
        "outputs_kwargs", [{"account": OTHER_ACCOUNT}, {"region": "us-west-2"}], ids=str
    )
    def test_a_target_mismatch_exits_one_before_any_write(
        self, tmp_path: Path, outputs_kwargs: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert mse.main(_cli(_outputs_file(tmp_path, _outputs(**outputs_kwargs)), "--apply")) == 1
        assert capsys.readouterr().out == ""
        assert _supervisor_effort() == "low"

    @pytest.mark.usefixtures("aws_env")
    def test_outputs_naming_no_target_exit_one(self, tmp_path: Path) -> None:
        outputs = _outputs()
        del outputs["reps_table_arn"]
        assert mse.main(_cli(_outputs_file(tmp_path, outputs), "--apply")) == 1
        assert _supervisor_effort() == "low"

    @pytest.mark.usefixtures("aws_env")
    def test_a_disagreeing_table_prefix_exits_one(self, tmp_path: Path) -> None:
        path = _outputs_file(tmp_path, _outputs())
        assert mse.main(_cli(path, "--apply", "--table-prefix", "other-")) == 1
        assert _supervisor_effort() == "low"

    @pytest.mark.parametrize("body", ["not json", "[]"])
    def test_unusable_outputs_exit_one_before_any_aws_session(
        self, tmp_path: Path, body: str
    ) -> None:
        path = tmp_path / "outputs.json"
        path.write_text(body, encoding="utf-8")

        def explode(_profile: str, _region: str) -> Any:
            raise AssertionError("constructed an AWS session before validating inputs")

        assert mse.main(_cli(str(path)), session_factory=explode) == 1

    def test_an_unknown_profile_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty_config = tmp_path / "empty-config"
        empty_config.write_text("", encoding="utf-8")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(empty_config))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty_config))
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        assert mse.main(_cli(_outputs_file(tmp_path, _outputs()))) == 1

    @pytest.mark.usefixtures("aws_env")
    def test_a_failure_part_way_prints_the_partial_counts_and_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def failing_scan(self: Store, *_args: Any) -> Iterator[dict[str, object]]:
            yield _settings_item(IDENTIFYING_REP_ID, "medium")
            raise CrmError("The service failed.", detail=f"rep_id={IDENTIFYING_REP_ID}")

        monkeypatch.setattr(Store, "scan_key_prefix", failing_scan)
        assert mse.main(_cli(_outputs_file(tmp_path, _outputs()), "--apply")) == 1
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert (summary["partial"], summary["scanned"], summary["already_medium"]) == (True, 1, 1)
        assert "re-running is safe" in captured.err
        assert "CrmError" in captured.err
        assert IDENTIFYING_REP_ID not in captured.out + captured.err

    @pytest.mark.usefixtures("aws_env")
    def test_an_unresolvable_skip_is_warned_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        item = _settings_item("rep-gone", "low")
        tuning = item["tuning"]
        assert isinstance(tuning, dict)
        tuning["tiers"]["supervisor"]["model"] = "anthropic.claude-opus-0-0"
        Store(client=boto3.client("dynamodb", region_name=REGION), table_prefix=PREFIX).put_item(
            "reps", item
        )
        assert mse.main(_cli(_outputs_file(tmp_path, _outputs()))) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["skipped_unresolvable"] == 1
        assert "warning: 1 item(s)" in captured.err

    @pytest.mark.usefixtures("aws_env")
    def test_no_warning_without_an_unresolvable_skip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert mse.main(_cli(_outputs_file(tmp_path, _outputs()))) == 0
        assert capsys.readouterr().err == ""

    def test_a_supervisor_rejecting_the_new_effort_is_refused_before_any_session(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        supervisor = MATRIX.model_for("openai", "supervisor")
        without_medium = dataclasses.replace(
            supervisor,
            effort=dataclasses.replace(
                supervisor.effort,
                values=tuple(v for v in supervisor.effort.values if v != mse.NEW_EFFORT),
            ),
        )
        matrix = dataclasses.replace(
            MATRIX, models={**MATRIX.models, supervisor.model_id: without_medium}
        )

        def explode(_profile: str, _region: str) -> Any:
            raise AssertionError("constructed an AWS session before the precondition")

        path = _outputs_file(tmp_path, _outputs())
        assert mse.main(_cli(path, "--apply"), session_factory=explode, matrix=matrix) == 1
        assert "openai supervisor does not accept effort 'medium'" in capsys.readouterr().err

    def test_the_configured_supervisors_accept_the_new_effort(self) -> None:
        mse.assert_new_effort_resolves(MATRIX)


def _client_error(code: str, operation: str = "UpdateItem") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "scripted"}}, operation)


class _ScriptedStore:
    """A store yielding fixed settings items whose updates raise a scripted error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def scan_key_prefix(self, *_args: Any) -> Iterator[dict[str, object]]:
        yield _settings_item("rep-1", "low")

    def update_item(self, *_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise self._error


class TestConflictClassification:
    def test_a_failed_condition_counts_as_a_conflict(self) -> None:
        failed = Conflict("changed")
        failed.__cause__ = _client_error("ConditionalCheckFailedException")
        store: Any = _ScriptedStore(failed)
        summary = mse.migrate(store, matrix=MATRIX, apply=True)
        assert (summary.eligible, summary.migrated, summary.conflict) == (1, 0, 1)

    def test_a_throttled_write_is_raised_not_counted(self) -> None:
        throttled = Conflict("busy")
        throttled.__cause__ = _client_error("ProvisionedThroughputExceededException")
        store: Any = _ScriptedStore(throttled)
        with pytest.raises(mse.MigrationInterruptedError) as raised:
            mse.migrate(store, matrix=MATRIX, apply=True)
        assert raised.value.__cause__ is throttled
        summary = raised.value.summary
        assert (summary.partial, summary.eligible, summary.conflict) == (True, 1, 0)


class TestPlainTuning:
    @pytest.mark.parametrize(
        "extra", [frozenset({"x"}), b"raw", 0.5], ids=["set", "bytes", "float"]
    )
    def test_a_tuning_holding_a_non_settings_type_is_not_a_settings_item(
        self, extra: object
    ) -> None:
        item = _settings_item("rep-1", "low")
        tuning = item["tuning"]
        assert isinstance(tuning, dict)
        tuning["tiers"]["supervisor"]["extra"] = extra

        class _OneItem:
            def scan_key_prefix(self, *_args: Any) -> Iterator[dict[str, object]]:
                yield item

        store: Any = _OneItem()
        summary = mse.migrate(store, matrix=MATRIX, apply=True)
        assert (summary.skipped_not_settings, summary.eligible) == (1, 0)


class TestMigratedTuning:
    def test_only_the_supervisor_effort_changes_and_the_input_is_not_mutated(self) -> None:
        original = _settings_item("rep-1", "low")["tuning"]
        assert isinstance(original, dict)
        snapshot = json.dumps(original, sort_keys=True)
        updated = mse.migrated_tuning(original)
        assert json.dumps(original, sort_keys=True) == snapshot
        assert updated["tiers"]["supervisor"]["effort"] == "medium"
        updated["tiers"]["supervisor"]["effort"] = "low"
        assert updated == original

    def test_the_key_prefix_is_the_settings_key_builders(self) -> None:
        key = user_settings_key("demo", "rep-1")
        assert key.startswith(mse.SETTINGS_KEY_PREFIX)
        assert not "conversation#c-1".startswith(mse.SETTINGS_KEY_PREFIX)


class _PagingClient:
    """A scripted DynamoDB client: ``scan`` returns the given pages, or raises ``error``."""

    def __init__(self, pages: list[dict[str, Any]], error: ClientError | None = None) -> None:
        self.pages = pages
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.pages[len(self.calls) - 1]


class TestScanKeyPrefix:
    def test_it_follows_every_page_with_a_begins_with_filter(self) -> None:
        cursor = {"rep_id": {"S": "usersettings#demo#a"}}
        client = _PagingClient(
            [
                {"Items": [{"rep_id": {"S": "usersettings#demo#a"}}], "LastEvaluatedKey": cursor},
                {"Items": [{"rep_id": {"S": "usersettings#demo#b"}, "n": {"N": "3"}}]},
            ]
        )
        items = list(
            Store(client=client, table_prefix="p-").scan_key_prefix(
                "reps", "rep_id", "usersettings#"
            )
        )
        assert items == [
            {"rep_id": "usersettings#demo#a"},
            {"rep_id": "usersettings#demo#b", "n": 3},
        ]
        assert [call.get("ExclusiveStartKey") for call in client.calls] == [None, cursor]
        assert {call["TableName"] for call in client.calls} == {"p-reps"}
        assert client.calls[0]["FilterExpression"] == "begins_with(#k, :p)"
        assert client.calls[0]["ExpressionAttributeNames"] == {"#k": "rep_id"}
        assert client.calls[0]["ExpressionAttributeValues"] == {":p": {"S": "usersettings#"}}
        assert all(call["ConsistentRead"] is True for call in client.calls)

    def test_items_are_decoded_exactly(self) -> None:
        client = _PagingClient(
            [
                {
                    "Items": [
                        {
                            "rep_id": {"S": "usersettings#demo#a"},
                            "t": {"N": "0.123456789012345678901234567"},
                            "i": {"N": "3"},
                            "ss": {"SS": ["x", "y"]},
                            "ns": {"NS": ["1", "1.5"]},
                            "b": {"B": b"raw"},
                            "bs": {"BS": [b"a"]},
                        }
                    ]
                }
            ]
        )
        [item] = Store(client=client).scan_key_prefix("reps", "rep_id", "usersettings#")
        assert item["t"] == Decimal("0.123456789012345678901234567")
        assert item["i"] == 3 and isinstance(item["i"], int)
        assert item["ss"] == frozenset({"x", "y"})
        assert item["ns"] == frozenset({1, Decimal("1.5")})
        assert (item["b"], item["bs"]) == (b"raw", frozenset({b"a"}))

    def test_the_default_decode_is_unchanged(self) -> None:
        from crm.store.store import _decode_item

        assert _decode_item({"t": {"N": "0.5"}}) == {"t": 0.5}
        assert isinstance(_decode_item({"t": {"N": "0.5"}})["t"], float)
        with pytest.raises(TypeError):
            _decode_item({"ss": {"SS": ["x"]}})

    def test_a_client_error_becomes_a_taxonomy_member(self) -> None:
        client = _PagingClient([], error=_client_error("ResourceNotFoundException", "Scan"))
        with pytest.raises(TableNotFound) as raised:
            list(Store(client=client).scan_key_prefix("reps", "rep_id", "usersettings#"))
        assert not isinstance(raised.value, ClientError)
        assert raised.value.detail is not None
        assert "operation=scan_key_prefix:reps" in raised.value.detail

    def test_a_non_key_attribute_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a key attribute"):
            list(Store(client=_PagingClient([])).scan_key_prefix("reps", "kind", "x"))

    def test_an_empty_prefix_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty prefix"):
            list(Store(client=_PagingClient([])).scan_key_prefix("reps", "rep_id", ""))

    def test_an_unknown_table_is_refused(self) -> None:
        with pytest.raises(ValueError):
            list(Store(client=_PagingClient([])).scan_key_prefix("nope", "rep_id", "x"))
