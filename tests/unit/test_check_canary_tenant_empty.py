# SPDX-License-Identifier: MIT
"""Unit tests for ``scripts/check_canary_tenant_empty.py``, the gate asserting the ``canary`` tenant
is empty: dispositions, record naming, classification, registration, and target verification.

Every run here is against ``moto`` or an injected store, never a deployed table.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
import scripts.check_canary_tenant_empty as gate
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    NoCredentialsError,
    ProfileNotFound,
)
from moto import mock_aws
from scripts.run_gates import GATE_SKIP_EXIT_STATUS

from crm.errors import CrmError, ServiceBusy, wrap_aws_error
from crm.store.provisioning import create_all_tables
from crm.store.schema import TENANT_SCOPED_TABLES
from crm.store.store import Store
from eval.tenancy import CANARY_TENANT as TENANCY_CANARY_TENANT

#: A planted-row count exceeding the gate's inline naming bound.
_MANY_PLANTED = 25


#: Every environment variable through which the host's AWS identity, profile or region could reach a
#: session built in these tests.
_AMBIENT_AWS_ENV: tuple[str, ...] = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CREDENTIAL_EXPIRATION",
    "AWS_VAULT",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_MAX_ATTEMPTS",
    "AWS_RETRY_MODE",
)


def _hermetic_aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Replace the host's AWS configuration with a `default` profile holding fake static credentials
    and a single attempt per call, so an unreachable endpoint fails at once rather than after retries.

    No session built afterwards reads the host's ``~/.aws`` or its ambient ``AWS_*`` variables.
    """
    for name in _AMBIENT_AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "aws-config"
    credentials = tmp_path / "aws-credentials"
    config.write_text("[default]\nregion = us-east-1\nmax_attempts = 1\n", encoding="utf-8")
    credentials.write_text(
        "[default]\naws_access_key_id = testing\naws_secret_access_key = testing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))


@pytest.fixture
def dynamodb_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """A moto-mocked DynamoDB client built from its own session with fake static credentials."""
    _hermetic_aws_env(monkeypatch, tmp_path)
    with mock_aws():
        yield boto3.session.Session(region_name="us-east-1").client("dynamodb")


@pytest.fixture
def store(dynamodb_client: Any) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _plant_canary_account(store: Store, *, account_id: str) -> str:
    """Write one ``canary`` account directly and return its id."""
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": gate.CANARY_TENANT,
            "name": "Leaked Account",
            "owner_rep_id": "rep-ashford",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    return account_id


# -- shared values agree with the modules that own them ------------------------------------------


def test_the_gate_watches_the_same_tenant_the_harness_guard_does() -> None:
    """The gate's canary tenant name equals the harness guard's."""
    assert gate.CANARY_TENANT == TENANCY_CANARY_TENANT


def test_the_gates_skip_status_is_the_one_run_gates_renders_as_skip() -> None:
    """The gate's skip status equals `run_gates.py`'s skip status."""
    assert gate.SKIP_EXIT_STATUS == GATE_SKIP_EXIT_STATUS


def test_the_gate_is_not_a_layered_smoke_module() -> None:
    """The gate lives under ``scripts/`` as a ``check_*`` module, not under ``scripts/smoke/``."""
    source = gate.__file__
    assert source is not None
    path = Path(source).resolve()
    repo_root = Path(__file__).resolve().parents[2]

    assert path.parent == repo_root / "scripts"
    assert not (repo_root / "scripts" / "smoke" / "canary_guard.py").exists()
    assert path.name.startswith("check_"), (
        "an offline gate is named check_*, which is how run_gates.py's registry reads"
    )


def test_the_gate_declares_no_layer_constant() -> None:
    """The gate declares no ``LAYER`` constant."""
    source = gate.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")

    assert not hasattr(gate, "LAYER")
    assert "\nLAYER" not in text


def test_the_gate_names_itself_in_both_output_forms(store: Store) -> None:
    """The gate name appears in both the JSON and the text report."""
    report = gate.check_canary_tenant(store)

    assert gate.GATE_NAME == "canary_tenant_empty"
    assert report.to_json_dict()["gate"] == gate.GATE_NAME
    assert gate.GATE_NAME in report.render_text()


def test_the_gate_does_not_import_the_harness_side_guard() -> None:
    """The gate source does not import the harness-side canary guard."""
    source = gate.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "from eval.tenancy import" not in text
    assert "assert_canary_empty" not in text
    assert "find_canary_records" not in text


# -- pass: the tenant was really read, and every table was covered --------------------------------


def test_a_clean_canary_tenant_passes_and_names_every_table_it_read(store: Store) -> None:
    report = gate.check_canary_tenant(store)

    assert report.status == "pass"
    assert report.exit_code() == 0
    assert set(report.tables_read) == set(TENANT_SCOPED_TABLES)
    assert report.records == ()


def test_a_pass_reads_every_table_delete_tenant_would_walk(store: Store) -> None:
    """A pass reads every tenant-scoped table."""
    report = gate.check_canary_tenant(store)

    assert sorted(report.tables_read) == sorted(TENANT_SCOPED_TABLES)


# -- fail: a violation exits 1 and is named -------------------------------------------------------


def test_a_planted_record_fails_and_is_named(store: Store) -> None:
    account_id = _plant_canary_account(store, account_id="canary-leak-0001")

    report = gate.check_canary_tenant(store)

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert [record.key_value for record in report.records] == [account_id]
    rendered = report.render_text()
    assert account_id in rendered, "only counting the rows costs an hour of bisection"
    assert "accounts" in rendered


def test_a_mass_violation_names_a_bounded_sample_in_text_but_all_of_them_in_json(
    store: Store,
) -> None:
    for index in range(_MANY_PLANTED):
        _plant_canary_account(store, account_id=f"canary-leak-{index:04d}")

    report = gate.check_canary_tenant(store)
    rendered = report.render_text()
    payload = report.to_json_dict()

    assert len(report.records) == _MANY_PLANTED
    assert f"{_MANY_PLANTED} record(s)" in rendered
    assert "more" in rendered, "a truncated list must disclose that it is truncated"
    assert payload["record_count"] == _MANY_PLANTED
    assert len(payload["records"]) == _MANY_PLANTED


def test_a_row_with_no_readable_key_is_still_reported(store: Store, dynamodb_client: Any) -> None:
    """An offending row with no readable key is still reported."""
    dynamodb_client.put_item(
        TableName="accounts",
        Item={
            "account_id": {"S": "canary-keyless"},
            "tenant": {"S": gate.CANARY_TENANT},
            "name_lower": {"S": "keyless"},
        },
    )

    report = gate.check_canary_tenant(store)

    assert report.status == "fail"
    assert len(report.records) == 1


# -- skip: verified nothing is exit 3, never 0 ----------------------------------------------------


def test_an_unreadable_tenant_skips_rather_than_passing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable tenant reports skip, not pass."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise NoCredentialsError

    monkeypatch.setattr(store, "query_tenant_scan", _raise)

    report = gate.check_canary_tenant(store)

    assert report.status == "skip"
    assert report.exit_code() == gate.SKIP_EXIT_STATUS
    assert report.exit_code() != 0
    assert "could not read" in report.reason


def test_a_partial_read_never_reports_a_pass(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial read reports skip, not pass."""
    real = store.query_tenant_scan
    calls: list[str] = []

    def _fail_after_first(table: str, **kwargs: Any) -> Any:
        calls.append(table)
        if len(calls) > 1:
            raise NoCredentialsError
        return real(table, **kwargs)

    monkeypatch.setattr(store, "query_tenant_scan", _fail_after_first)

    report = gate.check_canary_tenant(store)

    assert report.status == "skip"
    assert set(report.tables_read) != set(TENANT_SCOPED_TABLES)


def test_a_violation_already_found_outranks_a_later_read_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A violation found before a later read failure still reports fail with its records."""
    _plant_canary_account(store, account_id="canary-leak-before-outage")
    real = store.query_tenant_scan
    calls: list[str] = []

    def _fail_after_the_first_table(table: str, **kwargs: Any) -> Any:
        calls.append(table)
        if len(calls) > 1:
            raise NoCredentialsError
        return real(table, **kwargs)

    monkeypatch.setattr(store, "query_tenant_scan", _fail_after_the_first_table)

    report = gate.check_canary_tenant(store)

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert [record.key_value for record in report.records] == ["canary-leak-before-outage"]
    assert "may be incomplete" in report.reason, "the truncated enumeration must be disclosed"
    assert "could not read" in report.reason


def test_a_store_that_cannot_be_built_skips_rather_than_passing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(**_kwargs: object) -> Store:
        raise NoCredentialsError

    monkeypatch.setattr(gate, "build_store", _raise)

    report = gate.run(table_prefix="")

    assert report.status == "skip"
    assert report.exit_code() == gate.SKIP_EXIT_STATUS


def test_main_exits_three_not_zero_without_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main` exits 3 and prints `[SKIP]` when no store can be built."""

    def _raise(**_kwargs: object) -> Store:
        raise NoCredentialsError

    monkeypatch.setattr(gate, "build_store", _raise)

    status = gate.main([])

    assert status == gate.SKIP_EXIT_STATUS
    assert status != 0
    assert "[SKIP]" in capsys.readouterr().out


def test_require_store_turns_a_skip_into_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--require-store` turns a skip into exit 1."""

    def _raise(**_kwargs: object) -> Store:
        raise NoCredentialsError

    monkeypatch.setattr(gate, "build_store", _raise)

    assert gate.main(["--require-store"]) == 1


# -- CLI surface ----------------------------------------------------------------------------------


def test_main_exits_one_and_names_the_record_on_a_violation(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant_canary_account(store, account_id="canary-leak-cli")
    monkeypatch.setattr(gate, "build_store", lambda **_kwargs: store)

    status = gate.main([])

    assert status == 1
    assert "canary-leak-cli" in capsys.readouterr().out


def test_main_emits_the_pinned_json_shape(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "build_store", lambda **_kwargs: store)

    status = gate.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["gate"] == "canary_tenant_empty"
    assert payload["tenant"] == gate.CANARY_TENANT
    assert payload["status"] == "pass"
    assert payload["record_count"] == 0


def test_the_table_prefix_resolves_from_the_flag_then_the_environment() -> None:
    assert gate.resolve_table_prefix("flag-", {"CRM_TABLE_PREFIX": "env-"}) == "flag-"
    assert gate.resolve_table_prefix(None, {"CRM_TABLE_PREFIX": "env-"}) == "env-"
    assert gate.resolve_table_prefix(None, {}) == ""


def test_an_unrecognised_flag_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        gate.main(["--not-a-flag"])

    assert excinfo.value.code == 2


# -- the narrowed exit-3 classification: a refusal is a failure, not an absence -------------------


def _instantiate_for_classification(exception_type: type[Exception]) -> Exception:
    """Return an instance of ``exception_type``, bypassing ``__init__`` if construction fails."""
    try:
        return exception_type()
    except (TypeError, KeyError):
        return exception_type.__new__(exception_type)


def _taxonomy_error(code: str) -> CrmError:
    """Return the taxonomy member `crm.errors.wrap_aws_error` produces for the AWS error `code`."""
    return wrap_aws_error(
        ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "Query"),
        operation="Query",
    )


@pytest.mark.parametrize(
    ("aws_code", "meaning"),
    [
        ("AccessDeniedException", "the role lacks dynamodb:Query on gsi_tenant"),
        ("ValidationException", "the gsi_tenant key schema does not match the query"),
        ("ProvisionedThroughputExceededException", "a live table under load"),
    ],
)
def test_a_store_that_answered_with_a_refusal_fails_rather_than_skipping(
    store: Store, monkeypatch: pytest.MonkeyPatch, aws_code: str, meaning: str
) -> None:
    """A store that answered with a refusal is a failure, not an absence."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise _taxonomy_error(aws_code)

    monkeypatch.setattr(store, "query_tenant_scan", _raise)

    report = gate.check_canary_tenant(store)

    assert report.status == "fail", meaning
    assert report.exit_code() == 1
    assert report.exit_code() != gate.SKIP_EXIT_STATUS
    assert "not a skip" in report.reason


@pytest.mark.parametrize("aws_code", ["ResourceNotFoundException"])
def test_absent_tables_still_skip(
    store: Store, monkeypatch: pytest.MonkeyPatch, aws_code: str
) -> None:
    """Absent tables report skip."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise _taxonomy_error(aws_code)

    monkeypatch.setattr(store, "query_tenant_scan", _raise)

    report = gate.check_canary_tenant(store)

    assert report.status == "skip"
    assert report.exit_code() == gate.SKIP_EXIT_STATUS


def test_a_found_violation_still_outranks_a_refusal(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A violation found before a later refusal still reports fail with its records."""
    _plant_canary_account(store, account_id="acct-leaked")
    real = store.query_tenant_scan
    calls: list[str] = []

    def _fail_after_first(table: str, **kwargs: Any) -> Any:
        calls.append(table)
        if len(calls) > 1:
            raise _taxonomy_error("AccessDeniedException")
        return real(table, **kwargs)

    monkeypatch.setattr(store, "query_tenant_scan", _fail_after_first)

    report = gate.check_canary_tenant(store)

    assert report.status == "fail"
    assert any(r.key_value == "acct-leaked" for r in report.records)
    assert "may be incomplete" in report.reason


def test_the_two_classifiers_agree() -> None:
    """The gate and `seed_demo_tenant` classify every unreachable-failure type identically."""
    import scripts.seed_demo_tenant as cli

    for code in ("AccessDeniedException", "ValidationException", "ResourceNotFoundException"):
        exc = _taxonomy_error(code)
        assert gate.is_unreachable_failure(exc) is cli.is_unreachable_failure(exc), code

    unreachable = set(gate._UNREACHABLE_FAILURES) | set(cli._UNREACHABLE_FAILURES)
    assert unreachable, "the union is empty; the tuples this test derives from have gone missing"
    for exception_type in sorted(unreachable, key=lambda cls: cls.__name__):
        instance = _instantiate_for_classification(exception_type)
        assert gate.is_unreachable_failure(instance) is cli.is_unreachable_failure(instance), (
            f"{exception_type.__name__} is classified differently by the two entry points: "
            f"gate={gate.is_unreachable_failure(instance)}, "
            f"seed_demo_tenant={cli.is_unreachable_failure(instance)}. The duplication is deliberate; "
            "the divergence in meaning is not"
        )
        assert gate.is_unreachable_failure(instance) is True, (
            f"{exception_type.__name__} is a member of an _UNREACHABLE_FAILURES tuple but is not "
            "classified as unreachable, so the tuple and the predicate disagree"
        )


def test_a_gate_defect_is_a_failure_not_a_permanent_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `AttributeError` inside `build_store` reports fail, not skip."""

    def _broken(**_kwargs: object) -> Store:
        raise AttributeError("Store has no attribute 'tabel_prefix'")

    monkeypatch.setattr(gate, "build_store", _broken)

    report = gate.run(table_prefix="")

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert "not an absent credential" in report.reason


def test_the_registration_supplies_a_prefix_that_can_reach_the_deployed_tables() -> None:
    """The gate's registration passes the table prefix that `infra/main.tf` declares."""
    from scripts.run_gates import GATES, deployed_table_prefix

    argv = dict(GATES)["check_canary_tenant_empty"]
    assert "--table-prefix" in argv, (
        "check_canary_tenant_empty is registered without --table-prefix, so it resolves an empty "
        "prefix and can only ever [SKIP] — a gate that never reaches its subject"
    )
    supplied = argv[argv.index("--table-prefix") + 1]
    assert supplied, (
        "the registration passes --table-prefix with an empty value, which is the same hole"
    )
    assert gate.resolve_table_prefix(supplied) == supplied

    # The expected prefix is `infra/main.tf`'s `name_prefix` local.
    repo_root = Path(__file__).resolve().parents[2]
    declared = (repo_root / "infra" / "main.tf").read_text(encoding="utf-8")
    assert f'name_prefix = "{deployed_table_prefix()}"' in declared, (
        "the prefix the aggregator would pass is not the one infra/main.tf declares — the two have "
        "drifted, which is the hand-maintained-copy failure reading the declaration exists to prevent"
    )


def test_an_operators_explicit_prefix_wins_over_the_declared_one() -> None:
    """An explicit `CRM_TABLE_PREFIX` wins over the declared prefix."""
    from scripts.run_gates import deployed_table_prefix

    assert (
        deployed_table_prefix({"CRM_TABLE_PREFIX": "operator-chose-this"}) == "operator-chose-this"
    )
    assert deployed_table_prefix({}) == deployed_table_prefix({"CRM_TABLE_PREFIX": ""})


def test_the_registration_does_not_pin_profile_or_region() -> None:
    """The gate's registration passes neither `--profile` nor `--region`."""
    from scripts.run_gates import GATES

    argv = dict(GATES)["check_canary_tenant_empty"]
    assert "--profile" not in argv and "--region" not in argv


def test_the_skip_path_survives_the_pin() -> None:
    """With a prefix supplied and a closed local port as endpoint, the gate reports `[SKIP]`."""
    report = gate.run(table_prefix="crm-agent-eval", endpoint_url="http://127.0.0.1:1")

    assert report.status == "skip", report.render_text()
    assert report.exit_code() == GATE_SKIP_EXIT_STATUS
    assert "Nothing was verified." in report.render_text()


def test_a_read_failure_with_no_records_does_not_render_as_a_leaked_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-record fail renders its real cause, not the tenant-leak sentence."""

    def _broken(**_kwargs: object) -> Store:
        raise AttributeError("Store has no attribute 'tabel_prefix'")

    monkeypatch.setattr(gate, "build_store", _broken)
    rendered = gate.run(table_prefix="crm-agent-eval").render_text()

    assert "[FAIL]" in rendered
    assert "holds 0 record(s)" not in rendered
    assert "not an absent credential" in rendered
    assert "about the read, not about a leaked tenant" in rendered


class TestAnAbsentProfileIsAnAbsence:
    """A named-but-absent AWS profile is a configuration absence: exit 3, never exit 1.

    Each cell points botocore's config-file environment variables at absent paths.
    """

    @staticmethod
    def _with_no_aws_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Make every profile name unresolvable, independently of the host's real configuration."""
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-credentials"))

    def test_the_classifier_treats_an_absent_profile_as_an_absence(self) -> None:
        assert gate.is_unreachable_failure(ProfileNotFound(profile="no-such-profile")) is True

    def test_an_absent_profile_skips_rather_than_failing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._with_no_aws_config(monkeypatch, tmp_path)

        report = gate.run(
            table_prefix="crm-agent-eval",
            profile="no-such-profile",
            endpoint_url="http://127.0.0.1:1",
        )

        assert report.status == "skip", report.render_text()
        assert report.exit_code() == GATE_SKIP_EXIT_STATUS
        assert "ProfileNotFound" in report.reason, (
            "the skip must be attributable to the absent profile; a loopback endpoint skips too, so "
            "this is what makes the cell discriminate rather than agree with itself"
        )
        assert report.tables_read == ()

    def test_main_exits_three_rather_than_one_with_no_profile_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``main`` exits 3 and renders ``[SKIP]`` naming ``ProfileNotFound``."""
        self._with_no_aws_config(monkeypatch, tmp_path)

        status = gate.main(
            [
                "--table-prefix",
                "crm-agent-eval",
                "--profile",
                "no-such-profile",
                "--endpoint-url",
                "http://127.0.0.1:1",
            ]
        )
        out = capsys.readouterr().out

        assert status == gate.SKIP_EXIT_STATUS
        assert status != 1
        # Prefix-anchored: the FAIL rendering's prose also contains the literal "[SKIP]".
        assert out.startswith("[SKIP]"), out
        assert "ProfileNotFound" in out

    def test_require_store_still_converts_that_skip_into_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--require-store`` escalates the absent-profile ``[SKIP]`` to exit 1."""
        self._with_no_aws_config(monkeypatch, tmp_path)

        status = gate.main(
            [
                "--table-prefix",
                "crm-agent-eval",
                "--profile",
                "no-such-profile",
                "--endpoint-url",
                "http://127.0.0.1:1",
                "--require-store",
            ]
        )
        out = capsys.readouterr().out

        assert status == 1
        assert status != gate.SKIP_EXIT_STATUS
        assert out.startswith("[SKIP]"), (
            "the escalation must come from --require-store, not from the gate having classified an "
            f"absent profile as a failure of the read: {out}"
        )
        assert "[FAIL]" not in out


class TestARemoteRunCannotPassWithoutProvingItsTarget:
    """A remote run must verify its target against `--outputs` before it can report a pass."""

    def test_a_remote_build_with_no_outputs_refuses(self) -> None:
        with pytest.raises(gate.UnverifiedTargetError) as caught:
            gate.build_store(table_prefix="p", profile="whatever", region="us-west-2")
        message = str(caught.value)
        assert "--outputs" in message, "the refusal must name the flag that fixes it"
        assert "Nothing was read" in message

    def test_a_local_endpoint_is_exempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A loopback endpoint (`scripts/run_local_dynamodb.sh`) is exempt from the guard."""
        built: list[str | None] = []

        class _Session:
            def client(self, _service: str, **kwargs: object) -> object:
                built.append(str(kwargs.get("endpoint_url")))
                return object()

        monkeypatch.setattr(gate, "Store", lambda **_kwargs: "store")
        import boto3

        monkeypatch.setattr(boto3.session, "Session", lambda **_kwargs: _Session())
        gate.build_store(table_prefix="p", endpoint_url="http://localhost:8000")
        assert built == ["http://localhost:8000"]

    def test_run_reports_the_refusal_as_skip_and_never_as_a_pass(self) -> None:
        """`run` reports the unverified-target refusal as skip with no tables read."""
        report = gate.run(table_prefix="p", profile="whatever", region="us-west-2")
        assert report.status == "skip"
        assert report.tables_read == ()
        assert "--outputs" in (report.reason or "")

    def test_a_named_target_mismatch_is_a_failure_not_a_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolved-vs-expected target mismatch reports fail."""

        class WrongTargetError(RuntimeError):
            pass

        # Both ids are placeholders in `check_no_account_id`'s `_PLACEHOLDER_ACCOUNT_IDS`.
        def _refuse(**_kwargs: object) -> None:
            raise WrongTargetError("resolved account 999999999999, expected 123456789012")

        monkeypatch.setattr(gate, "_assert_target_matches_outputs", _refuse)
        report = gate.run(table_prefix="p", region="us-west-2", outputs="whatever.json")
        assert report.status == "fail"
        assert "wrong target" in (report.reason or "")

    def test_the_exit_status_of_the_refusal_is_the_skip_status(self) -> None:
        """`main` exits with the skip status on the unverified-target refusal."""
        assert gate.main(["--table-prefix", "p"]) == gate.SKIP_EXIT_STATUS


def test_the_import_bootstrap_covers_both_roots_not_only_src() -> None:
    """The gate's `sys.path` bootstrap adds both the repository root and `src`."""
    source = Path(gate.__file__).read_text(encoding="utf-8")
    assert "from scripts.smoke.harness import" in source, (
        "the deferred import this guards has moved; retarget the test rather than deleting it"
    )
    bootstrap = source[source.index("REPO_ROOT = ") : source.index("from botocore")]
    assert 'REPO_ROOT / "src"' in bootstrap and "REPO_ROOT)" in bootstrap, (
        f"the bootstrap no longer adds BOTH roots, so the deferred scripts.* import needs "
        f"PYTHONPATH=. again: {bootstrap!r}"
    )


def _busy_raised_from(cause: BaseException | None) -> ServiceBusy:
    """A `ServiceBusy` raised the way `Store` raises it, `from` ``cause``."""
    try:
        try:
            if cause is not None:
                raise cause
            raise ServiceBusy("The service is busy. Retry shortly.")
        except ServiceBusy:
            raise
        except Exception as exc:
            raise ServiceBusy("The service is busy. Retry shortly.") from exc
    except ServiceBusy as busy:
        return busy


@pytest.mark.parametrize(
    "cause",
    [
        EndpointConnectionError(endpoint_url="http://127.0.0.1:1"),
        ConnectTimeoutError(endpoint_url="http://127.0.0.1:1"),
        HTTPClientError(error="connection reset"),
    ],
    ids=["endpoint_connection", "connect_timeout", "http_client"],
)
def test_a_service_busy_raised_from_a_connection_failure_is_unreachable(cause: Exception) -> None:
    import scripts.seed_demo_tenant as cli

    busy = _busy_raised_from(cause)
    assert gate.is_unreachable_failure(busy) is True
    assert cli.is_unreachable_failure(busy) is True


@pytest.mark.parametrize(
    "cause",
    [None, ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "Query")],
    ids=["no_cause", "throttle_answer"],
)
def test_a_service_busy_without_a_connection_cause_is_an_answer(cause: Exception | None) -> None:
    import scripts.seed_demo_tenant as cli

    busy = _busy_raised_from(cause)
    assert gate.is_unreachable_failure(busy) is False
    assert cli.is_unreachable_failure(busy) is False


def test_a_real_store_against_a_closed_port_raises_service_busy_and_the_gate_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real `Store` maps the connection error to `ServiceBusy`; the gate still exits 3."""
    _hermetic_aws_env(monkeypatch, tmp_path)
    store = gate.build_store(table_prefix="crm-agent-eval", endpoint_url="http://127.0.0.1:1")
    with pytest.raises(ServiceBusy) as raised:
        store.query_tenant_scan(
            sorted(TENANT_SCOPED_TABLES)[0], tenant=gate.CANARY_TENANT, exclusive_start_key=None
        )
    assert isinstance(raised.value.__cause__, EndpointConnectionError)
    assert gate.main(
        ["--table-prefix", "crm-agent-eval", "--endpoint-url", "http://127.0.0.1:1"]
    ) == (GATE_SKIP_EXIT_STATUS)


def _busy_with_context_only(cause: BaseException) -> ServiceBusy:
    """A `ServiceBusy` raised while handling ``cause`` without `from`: ``__context__`` only."""
    try:
        try:
            raise cause
        except Exception:
            raise ServiceBusy("The service is busy. Retry shortly.")  # noqa: B904
    except ServiceBusy as busy:
        return busy


def test_a_connection_failure_reachable_only_through_context_is_not_unreachable() -> None:
    """The cause walk follows ``__cause__`` alone; an implicit ``__context__`` is not a cause."""
    import scripts.seed_demo_tenant as cli

    busy = _busy_with_context_only(EndpointConnectionError(endpoint_url="http://127.0.0.1:1"))
    assert busy.__cause__ is None
    assert isinstance(busy.__context__, EndpointConnectionError)
    assert gate.is_unreachable_failure(busy) is False
    assert cli.is_unreachable_failure(busy) is False
