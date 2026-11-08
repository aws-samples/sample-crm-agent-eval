# SPDX-License-Identifier: MIT
"""`eval_trigger`'s `artefact_url` action through the real handler, against moto DynamoDB and S3.

Requests are the recorded REST v1 proxy event with claims edited, so identities go through the
production channel decoder. The run is submitted through the handler and given the
`artifact_links` the aggregate step records; its artefact objects are put in a moto bucket, and
each presigned URL is fetched over HTTP to prove it names exactly that object.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import boto3
import pytest
import requests
from moto import mock_aws

from crm.envelope import CLAIM_FULL_VISIBILITY, CLAIM_GROUPS, CLAIM_REP_ID
from crm.handlers import eval_trigger as handler_module
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import runs, sfn_handlers
from eval.artefacts import (
    ARTEFACT_URL_EXPIRES_SECONDS,
    BUCKET_ENV_VAR,
    EVAL_ARTEFACTS_SCOPE,
    PREFIX_ENV_VAR,
    ArtefactsConfig,
    download_disposition,
    validate_artefact_key,
)
from eval.runs import job_row_id
from eval.trigger import EVAL_STATE_MACHINE_ARN_ENV_VAR

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
_STATE_MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:eval_sweep"
_BUCKET = "crm-agent-eval-eval-artifacts-123456789012"
_PREFIX = "eval-runs"
_RAW_KEY = "raw/anthropic.claude-opus-5-5_2026-10-12T18-20-12.008916+00-00.json"
_LINKS = {
    "matrix_csv": "matrix.csv",
    "matrix_json": "matrix.json",
    "matrix_md": "matrix.md",
    "raw": [_RAW_KEY],
}


class _Sfn:
    """Answers `start_execution` with an ARN and every `describe_execution` as RUNNING."""

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {"executionArn": f"{_STATE_MACHINE}:{kwargs['name']}"}

    def describe_execution(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "RUNNING"}


class _NeverPresign:
    """An S3 client that fails the test if anything asks it to presign."""

    def generate_presigned_url(self, **kwargs: Any) -> str:
        raise AssertionError(f"presigned a URL for a request that must be refused: {kwargs}")


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Store, Any]]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv(EVAL_STATE_MACHINE_ARN_ENV_VAR, _STATE_MACHINE)
    monkeypatch.setenv(BUCKET_ENV_VAR, _BUCKET)
    monkeypatch.setenv(PREFIX_ENV_VAR, _PREFIX)
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(dynamodb)
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        yield Store(client=dynamodb), s3


def _event(body: dict[str, Any], *, admin: bool = False, full: bool = False) -> dict[str, Any]:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    claims = event["requestContext"]["authorizer"]["claims"]
    claims[CLAIM_REP_ID] = "rep-1"
    claims[CLAIM_FULL_VISIBILITY] = "true" if full else "false"
    if admin:
        claims[CLAIM_GROUPS] = "admin"
    event["path"] = "/eval_trigger"
    event["body"] = json.dumps(body)
    return event


def _call(store: Store, event: dict[str, Any], *, s3: Any = None) -> tuple[int, Any]:
    response = handler_module.handler(event, None, store=store, sfn=_Sfn(), s3=s3)
    assert isinstance(response["body"], str), "proxy bodies must be JSON strings"
    return response["statusCode"], json.loads(response["body"])


def _completed_run(store: Store, s3: Any, *, links: dict[str, Any] | None = _LINKS) -> str:
    """Submit a run through the handler, record its artefact links, upload each artefact, finish it."""
    status, run = _call(store, _event({"action": "submit"}, full=True))
    assert status == 200, run
    run_id = str(run["run_id"])
    if links is not None:
        store.update_item(
            "eval_runs",
            {"run_id": job_row_id(run_id)},
            set_values={"artifact_links": json.dumps(links)},
            condition=None,
        )
        for key in [links["matrix_json"], links["matrix_md"], links["matrix_csv"], *links["raw"]]:
            s3.put_object(Bucket=_BUCKET, Key=f"{_PREFIX}/{run_id}/{key}", Body=f"body of {key}")
    runs.finalize(store, run_id=run_id, teardown_ok=True)
    return run_id


def _artefact_url(run_id: str, key: object, **claims: bool) -> dict[str, Any]:
    return _event({"action": "artefact_url", "run_id": run_id, "key": key}, **claims)


class TestGranted:
    @pytest.mark.parametrize("key", ["matrix.json", "matrix.md", "matrix.csv", _RAW_KEY])
    def test_a_full_visibility_caller_gets_a_url_for_exactly_that_object(
        self, aws: tuple[Store, Any], key: str
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)

        status, body = _call(store, _artefact_url(run_id, key, full=True), s3=s3)

        assert status == 200, body
        assert body["run_id"] == run_id and body["key"] == key
        parts = urlsplit(body["url"])
        assert parts.scheme == "https"
        assert parts.netloc.startswith(f"{_BUCKET}.s3.")
        assert unquote(parts.path) == f"/{_PREFIX}/{run_id}/{key}"
        fetched = requests.get(body["url"], timeout=10)
        assert fetched.status_code == 200
        assert fetched.text == f"body of {key}"

    def test_an_admin_without_full_visibility_gets_a_url(self, aws: tuple[Store, Any]) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        status, body = _call(store, _artefact_url(run_id, "matrix.json", admin=True), s3=s3)
        assert status == 200, body

    def test_the_handlers_own_client_signs_sigv4_virtual_host_and_expires_within_300_seconds(
        self, aws: tuple[Store, Any]
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)

        status, body = _call(store, _artefact_url(run_id, "matrix.json", full=True))

        assert status == 200, body
        parts = urlsplit(body["url"])
        assert parts.netloc.startswith(f"{_BUCKET}.s3.")
        query = parse_qs(parts.query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert 0 < int(query["X-Amz-Expires"][0]) <= 300
        assert body["expires_in"] == ARTEFACT_URL_EXPIRES_SECONDS <= 300
        assert requests.get(body["url"], timeout=10).text == "body of matrix.json"


class TestDenied:
    def test_an_unauthenticated_call_is_401(self, aws: tuple[Store, Any]) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        event = _artefact_url(run_id, "matrix.json", full=True)
        del event["requestContext"]["authorizer"]
        status, _body = _call(store, event, s3=_NeverPresign())
        assert status == 401

    def test_a_caller_without_full_visibility_is_403(self, aws: tuple[Store, Any]) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        status, body = _call(store, _artefact_url(run_id, "matrix.json"), s3=_NeverPresign())
        assert status == 403
        assert body["error"]["missing_scope"] == EVAL_ARTEFACTS_SCOPE

    @pytest.mark.parametrize("tenant", ["demo", "canary", "eval-another-run"])
    def test_a_run_recording_another_tenant_is_403(
        self, aws: tuple[Store, Any], tenant: str
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        store.update_item(
            "eval_runs",
            {"run_id": job_row_id(run_id)},
            set_values={"tenant": tenant},
            condition=None,
        )
        status, body = _call(
            store, _artefact_url(run_id, "matrix.json", full=True), s3=_NeverPresign()
        )
        assert status == 403
        assert body["error"]["missing_scope"] == EVAL_ARTEFACTS_SCOPE

    def test_an_unknown_run_is_404(self, aws: tuple[Store, Any]) -> None:
        store, _s3 = aws
        status, _body = _call(
            store, _artefact_url("no-such-run", "matrix.json", full=True), s3=_NeverPresign()
        )
        assert status == 404

    @pytest.mark.parametrize("key", ["matrix.parquet", "raw/other.json", "results.csv", "raw"])
    def test_a_key_the_run_did_not_record_is_404(self, aws: tuple[Store, Any], key: str) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        status, _body = _call(store, _artefact_url(run_id, key, full=True), s3=_NeverPresign())
        assert status == 404

    def test_a_key_recorded_only_by_another_run_is_404_for_this_run(
        self, aws: tuple[Store, Any]
    ) -> None:
        store, s3 = aws
        this_run = _completed_run(store, s3)
        other_only = "raw/other-run-only.json"
        other_run = _completed_run(store, s3, links={**_LINKS, "raw": [_RAW_KEY, other_only]})
        assert other_run != this_run
        status, _body = _call(
            store, _artefact_url(this_run, other_only, full=True), s3=_NeverPresign()
        )
        assert status == 404
        # The other run's own object path, asked for under this run, is not this run's artefact.
        status, _body = _call(
            store,
            _artefact_url(this_run, f"{_PREFIX}/{other_run}/matrix.json", full=True),
            s3=_NeverPresign(),
        )
        assert status == 404
        # Control: the same key is granted for the run that recorded it.
        status, body = _call(store, _artefact_url(other_run, other_only, full=True), s3=s3)
        assert status == 200, body
        assert unquote(urlsplit(body["url"]).path) == f"/{_PREFIX}/{other_run}/{other_only}"

    def test_a_run_with_no_recorded_artefacts_is_404(self, aws: tuple[Store, Any]) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3, links=None)
        status, _body = _call(
            store, _artefact_url(run_id, "matrix.json", full=True), s3=_NeverPresign()
        )
        assert status == 404

    @pytest.mark.parametrize(
        "key",
        [
            "../matrix.json",
            "raw/../matrix.json",
            "./matrix.json",
            "/matrix.json",
            "raw//x.json",
            "%2e%2e/matrix.json",
            "raw\\..\\matrix.json",
            "matrix.json\x00",
            "",
            "a" * 513,
            None,
            ["matrix.json"],
        ],
    )
    def test_a_traversal_or_malformed_key_is_400_before_any_lookup(
        self, aws: tuple[Store, Any], key: object
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        status, body = _call(store, _artefact_url(run_id, key, full=True), s3=_NeverPresign())
        assert status == 400, body

    def test_an_absent_key_is_400(self, aws: tuple[Store, Any]) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        event = _event({"action": "artefact_url", "run_id": run_id}, full=True)
        status, _body = _call(store, event, s3=_NeverPresign())
        assert status == 400

    def test_key_is_refused_by_every_other_action(self, aws: tuple[Store, Any]) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        event = _event({"action": "status", "run_id": run_id, "key": "matrix.json"}, full=True)
        status, _body = _call(store, event, s3=_NeverPresign())
        assert status == 400

    @pytest.mark.parametrize("unset", [BUCKET_ENV_VAR, PREFIX_ENV_VAR])
    def test_a_deployment_without_the_artefacts_env_fails_loudly(
        self, aws: tuple[Store, Any], monkeypatch: pytest.MonkeyPatch, unset: str
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        monkeypatch.delenv(unset)
        status, body = _call(
            store, _artefact_url(run_id, "matrix.json", full=True), s3=_NeverPresign()
        )
        assert status == 500
        assert "artefacts are not available" in body["error"]["message"]


@pytest.mark.parametrize("key", ["matrix.json", _RAW_KEY, "raw/a-b_c.d+e.json"])
def test_validate_artefact_key_accepts_recorded_path_shapes(key: str) -> None:
    assert validate_artefact_key(key) == key


#: (prefix, run_id, relative) triples the writer and reader must place identically.
_LAYOUT_CASES = [
    ("eval-runs", "d4f704fcc38841eb99fee8628a4f9d0b", "matrix.json"),
    ("eval-runs", "run-1", _RAW_KEY),
    ("a/b", "run-2", "raw/x.json"),
]


@pytest.mark.parametrize(("prefix", "run_id", "relative"), _LAYOUT_CASES)
def test_the_writer_and_the_reader_place_an_artefact_at_the_same_byte_identical_key(
    prefix: str, run_id: str, relative: str
) -> None:
    env = {BUCKET_ENV_VAR: _BUCKET, PREFIX_ENV_VAR: prefix}
    written = sfn_handlers.SweepControlConfig.from_env(env).artifact_key(run_id, relative)
    read = ArtefactsConfig.from_env(env).object_key(run_id, relative)
    assert written == read == f"{prefix}/{run_id}/{relative}"


def test_an_artefact_the_sweep_uploads_is_the_object_artefact_url_signs(
    aws: tuple[Store, Any], tmp_path: Path
) -> None:
    """Round trip: `_upload_tree` writes the run's files; `artefact_url`'s URL fetches each back."""
    store, s3 = aws
    run_id = _completed_run(store, s3, links=None)
    store.update_item(
        "eval_runs",
        {"run_id": job_row_id(run_id)},
        set_values={"artifact_links": json.dumps(_LINKS)},
        condition=None,
    )
    for relative in ["matrix.json", _RAW_KEY]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"uploaded {relative}", encoding="utf-8")
    config = sfn_handlers.SweepControlConfig.from_env()
    uploaded = sfn_handlers._upload_tree(s3, config, run_id, tmp_path)
    assert sorted(uploaded) == sorted(["matrix.json", _RAW_KEY])

    for relative in uploaded:
        status, body = _call(store, _artefact_url(run_id, relative, full=True), s3=s3)
        assert status == 200, body
        assert requests.get(body["url"], timeout=10).text == f"uploaded {relative}"


# -- download disposition -------------------------------------------------------------------------


def _download_url(run_id: str, key: str, download: object, **claims: bool) -> dict[str, Any]:
    return _event(
        {"action": "artefact_url", "run_id": run_id, "key": key, "download": download}, **claims
    )


class TestDownload:
    @pytest.mark.parametrize(
        ("key", "filename"),
        [
            ("matrix.md", "matrix-{run}.md"),
            ("matrix.csv", "matrix-{run}.csv"),
            (_RAW_KEY, "matrix-{run}.json"),
        ],
    )
    def test_a_download_url_serves_the_object_as_a_named_attachment(
        self, aws: tuple[Store, Any], key: str, filename: str
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)

        status, body = _call(store, _download_url(run_id, key, True, full=True), s3=s3)

        assert status == 200, body
        expected = f'attachment; filename="{filename.format(run=run_id)}"'
        query = parse_qs(urlsplit(body["url"]).query)
        assert query["response-content-disposition"] == [expected]
        fetched = requests.get(body["url"], timeout=10)
        assert fetched.status_code == 200
        assert fetched.headers["Content-Disposition"] == expected
        assert fetched.text == f"body of {key}"

    @pytest.mark.parametrize("download", [False, None])
    def test_without_download_the_url_names_no_disposition(
        self, aws: tuple[Store, Any], download: object
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        body_args: dict[str, Any] = {"action": "artefact_url", "run_id": run_id, "key": "matrix.md"}
        if download is not None:
            body_args["download"] = download
        status, body = _call(store, _event(body_args, full=True), s3=s3)
        assert status == 200, body
        assert "response-content-disposition" not in parse_qs(urlsplit(body["url"]).query)

    @pytest.mark.parametrize("download", ["true", 1, None, ["x"]])
    def test_a_non_boolean_download_is_400_before_any_presign(
        self, aws: tuple[Store, Any], download: object
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        status, body = _call(
            store, _download_url(run_id, "matrix.md", download, full=True), s3=_NeverPresign()
        )
        assert status == 400, body
        assert body["error"]["source"] == "download"

    def test_a_caller_without_full_visibility_is_403_with_download(
        self, aws: tuple[Store, Any]
    ) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        status, body = _call(store, _download_url(run_id, "matrix.md", True), s3=_NeverPresign())
        assert status == 403
        assert body["error"]["missing_scope"] == EVAL_ARTEFACTS_SCOPE

    def test_an_unrecorded_key_is_404_with_download(self, aws: tuple[Store, Any]) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        status, _body = _call(
            store, _download_url(run_id, "other.md", True, full=True), s3=_NeverPresign()
        )
        assert status == 404

    def test_download_is_refused_by_every_other_action(self, aws: tuple[Store, Any]) -> None:
        store, s3 = aws
        run_id = _completed_run(store, s3)
        for body_args in (
            {"action": "status", "run_id": run_id, "download": True},
            {"action": "list", "download": True},
            {"action": "submit", "download": True},
        ):
            status, body = _call(store, _event(body_args, full=True), s3=_NeverPresign())
            assert status == 400, (body_args, body)
            assert body["error"]["source"] == "arguments"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("matrix.md", 'attachment; filename="matrix-r1.md"'),
        ("raw/a.b.json", 'attachment; filename="matrix-r1.json"'),
        ("raw/README", 'attachment; filename="matrix-r1"'),
    ],
)
def test_download_disposition_names_matrix_run_and_the_key_suffix(key: str, expected: str) -> None:
    assert download_disposition("r1", key) == expected


# -- the run payload's tier table guard -----------------------------------------------------------


def _tier_record(summary: object) -> runs.RunRecord:
    return runs.RunRecord(
        run_id="r-guard",
        tenant="eval-r-guard",
        mode="stub",
        status="completed",
        started_at="2026-11-06T00:00:00+00:00",
        matrix_report={"tier_comparison": summary},
    )


def _unavailable_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    lines = []
    for record in caplog.records:
        try:
            parsed = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") == "eval_tier_table_unavailable":
            lines.append(parsed)
    return lines


def test_a_summary_the_formatter_refuses_is_omitted_and_logged_with_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    payload = _tier_record({"tiers": {}, "notes": []}).to_payload()
    assert "tier_comparison_table" not in payload
    assert _unavailable_lines(caplog) == [
        {
            "event": "eval_tier_table_unavailable",
            "correlation_id": "r-guard",
            "reason": "tier comparison summary field 'columns' is not a list",
        }
    ]


@pytest.mark.parametrize("bug", [TypeError, KeyError, AttributeError])
def test_a_formatter_bug_is_not_swallowed(
    bug: type[Exception], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken(_summary: object) -> dict[str, object]:
        raise bug("formatter bug")

    monkeypatch.setattr(runs, "tier_comparison_table", _broken)
    caplog.set_level("INFO")
    with pytest.raises(bug):
        _tier_record({"columns": [], "tiers": {}, "notes": []}).to_payload()
    assert _unavailable_lines(caplog) == []


def _stored_summary() -> dict[str, Any]:
    from eval.matrix import build_matrix_result, tier_comparison

    stored: dict[str, Any] = json.loads(json.dumps(tier_comparison(build_matrix_result([]))))
    return stored


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("model_ids", [1, 2], "has a non-string model id"),
        ("model_ids", "not-a-list", "has a non-string model id"),
        ("efforts", [{"x": 1}], "has a non-string effort"),
    ],
)
def test_a_stored_entry_with_unjoinable_lists_is_refused_as_malformed(
    field: str, value: object, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    from eval.matrix import tier_comparison_table

    summary = _stored_summary()
    summary["columns"] = [{"label": "anthropic", "mode": "stub"}]
    entry: dict[str, Any] = {"model_ids": ["m"], "efforts": ["medium"], field: value}
    summary["tiers"] = {tier: [dict(entry)] for tier in ("supervisor", "subagent", "tool")}
    with pytest.raises(ValueError, match=reason):
        tier_comparison_table(summary)
    caplog.set_level("INFO")
    assert "tier_comparison_table" not in _tier_record(summary).to_payload()
    (line,) = _unavailable_lines(caplog)
    assert reason in line["reason"]
