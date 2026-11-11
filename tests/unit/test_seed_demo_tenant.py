# SPDX-License-Identifier: MIT
"""Layer 1 tests for ``scripts/seed_demo_tenant.py``, the demo-tenant seeding CLI.

Covers the single expressible target tenant, exit-status dispositions, idempotency and determinism
checks, the ``canary`` bystander guard, the census, and reporting. Runs against moto or an injected
store.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
import scripts.seed_demo_tenant as cli
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
from moto import mock_aws
from scripts.run_gates import GATE_SKIP_EXIT_STATUS

from crm.authz import DEMO_TENANT
from crm.errors import CrmError, wrap_aws_error
from crm.store.provisioning import create_all_tables
from crm.store.schema import TENANT_SCOPED_TABLES
from crm.store.store import Store
from eval.tenancy import CANARY_TENANT, _reject_protected_tenant, seed_demo_tenant


@pytest.fixture
def dynamodb_client() -> Iterator[Any]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: Any) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _store_error(code: str = "ResourceNotFoundException", *, operation: str = "Query") -> CrmError:
    """The taxonomy member a real ``Store`` raises for the AWS error ``code``.

    Converts ``code`` through ``crm.errors.wrap_aws_error``, as the store does. The default is the
    absent-table code, which maps to ``TableNotFound``.
    """
    client_error = ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, operation)
    return wrap_aws_error(client_error, operation=operation)


class ReadFailingStore(Store):
    """A store whose tenant scans fail after ``allowed_reads`` successful calls.

    ``error`` selects which failure is raised.
    """

    def __init__(
        self, inner: Store, *, allowed_reads: int, error: BaseException | None = None
    ) -> None:
        self._inner = inner
        self._allowed_reads = allowed_reads
        self._error = error if error is not None else _store_error()
        self.reads_attempted = 0

    def query_tenant_scan(
        self,
        table: str,
        *,
        tenant: str,
        limit: int | None = None,
        exclusive_start_key: Mapping[str, object] | None = None,
        scan_forward: bool = True,
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        self.reads_attempted += 1
        if self.reads_attempted > self._allowed_reads:
            raise self._error
        return self._inner.query_tenant_scan(
            table,
            tenant=tenant,
            limit=limit,
            exclusive_start_key=exclusive_start_key,
            scan_forward=scan_forward,
        )

    def put_item(self, table: str, item: Mapping[str, object]) -> None:
        self._inner.put_item(table, item)


class TenantSelectiveFailingStore(Store):
    """A store that reads every tenant except ``failing_tenant``, which always fails.

    With ``failing_tenant="canary"`` only the bystander check fails.
    """

    def __init__(self, inner: Store, *, failing_tenant: str) -> None:
        self._inner = inner
        self._failing_tenant = failing_tenant

    def query_tenant_scan(
        self,
        table: str,
        *,
        tenant: str,
        limit: int | None = None,
        exclusive_start_key: Mapping[str, object] | None = None,
        scan_forward: bool = True,
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        if tenant == self._failing_tenant:
            raise _store_error("AccessDeniedException")
        return self._inner.query_tenant_scan(
            table,
            tenant=tenant,
            limit=limit,
            exclusive_start_key=exclusive_start_key,
            scan_forward=scan_forward,
        )

    def put_item(self, table: str, item: Mapping[str, object]) -> None:
        self._inner.put_item(table, item)


class NonAdvancingCursorStore(Store):
    """A store whose tenant scan always returns the same non-``None`` cursor.

    It raises after ``call_ceiling`` calls, so a missing non-advancement guard fails instead of
    hanging.
    """

    def __init__(self, inner: Store, *, call_ceiling: int = 50) -> None:
        self._inner = inner
        self._call_ceiling = call_ceiling
        self.reads_attempted = 0

    def query_tenant_scan(
        self,
        table: str,
        *,
        tenant: str,
        limit: int | None = None,
        exclusive_start_key: Mapping[str, object] | None = None,
        scan_forward: bool = True,
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        self.reads_attempted += 1
        if self.reads_attempted > self._call_ceiling:
            raise AssertionError(
                f"the census re-read table {table!r} {self.reads_attempted} times against a cursor "
                "that never advanced; the non-advancement guard is missing and the caller would "
                "otherwise loop forever"
            )
        return [], {"offset": 0}


class PagingStore(Store):
    """A store that returns tenant scans ``page_size`` items at a time, with a real cursor."""

    def __init__(self, inner: Store, *, page_size: int) -> None:
        self._inner = inner
        self._page_size = page_size
        self.reads_attempted = 0

    def query_tenant_scan(
        self,
        table: str,
        *,
        tenant: str,
        limit: int | None = None,
        exclusive_start_key: Mapping[str, object] | None = None,
        scan_forward: bool = True,
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        self.reads_attempted += 1
        offset = 0 if exclusive_start_key is None else int(str(exclusive_start_key["offset"]))
        items, _ = self._inner.query_tenant_scan(table, tenant=tenant, scan_forward=scan_forward)
        page = items[offset : offset + self._page_size]
        next_offset = offset + len(page)
        cursor: dict[str, object] | None = (
            {"offset": next_offset} if next_offset < len(items) else None
        )
        return page, cursor


@dataclass
class RecordingSeeder:
    """A minimal stand-in for ``fixtures.demo_seed.seed_demo``.

    Writes ``rows_per_pass`` accounts at deterministic keys and records every tenant it was asked to
    seed.
    """

    rows_per_pass: int = 3
    calls: list[str] = field(default_factory=list)

    def __call__(self, store: Store, *, tenant: str) -> object:
        self.calls.append(tenant)
        for index in range(self.rows_per_pass):
            _write_account(store, tenant=tenant, account_id=f"acct-{index}")
        return ("summary", self.rows_per_pass)


@dataclass
class DuplicatingSeeder:
    """A seeder that adds one new row every pass."""

    calls: int = 0

    def __call__(self, store: Store, *, tenant: str) -> object:
        self.calls += 1
        _write_account(store, tenant=tenant, account_id=f"acct-pass-{self.calls}")
        return ("summary", "stable")


@dataclass
class NonDeterministicSeeder:
    """A seeder whose census is stable but whose reported record identities change every pass."""

    calls: int = 0

    def __call__(self, store: Store, *, tenant: str) -> object:
        self.calls += 1
        _write_account(store, tenant=tenant, account_id="acct-stable")
        return ("summary", self.calls)


class ExplodingSeeder:
    """A seeder that raises a store-layer failure on the ``fail_on_call``-th pass."""

    def __init__(self, *, fail_on_call: int) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0

    def __call__(self, store: Store, *, tenant: str) -> object:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise _store_error(operation="PutItem")
        _write_account(store, tenant=tenant, account_id="acct-0")
        return ("summary", "stable")


def _write_account(store: Store, *, tenant: str, account_id: str) -> None:
    """Write one account row directly, carrying the fields the tenant index requires.

    ``name`` is required: without it ``name_lower`` is not derived and the row is absent from
    ``gsi_tenant``.
    """
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": tenant,
            "name": f"Account {account_id}",
            "owner_rep_id": "rep-ashford",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )


# -- the target tenant is the only expressible one ------------------------------------------------


def test_the_target_tenant_is_the_sanctioned_writers_own_tenant() -> None:
    """The script's target tenant is the one ``eval.tenancy.seed_demo_tenant`` writes."""
    assert cli.TARGET_TENANT == DEMO_TENANT
    assert cli.TARGET_TENANT == "demo"


def test_the_sanctioned_writer_still_accepts_no_tenant_parameter() -> None:
    """``eval.tenancy.seed_demo_tenant`` accepts no tenant parameter."""
    parameters = inspect.signature(seed_demo_tenant).parameters
    assert "tenant" not in parameters
    assert set(parameters) == {"store", "seeder"}


@pytest.mark.parametrize("tenant", [CANARY_TENANT, "demo", "eval-abc123", "anything"])
def test_no_tenant_can_be_named_on_the_command_line(tenant: str) -> None:
    """A ``--tenant`` option is a usage error (exit ``2``) before any store is built."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--tenant", tenant])
    assert excinfo.value.code == 2


def test_the_shared_guard_still_refuses_the_bystander_and_the_demo_tenant() -> None:
    """``_reject_protected_tenant`` refuses both ``canary`` and ``demo``."""
    with pytest.raises(ValueError, match=CANARY_TENANT):
        _reject_protected_tenant(CANARY_TENANT)
    with pytest.raises(ValueError, match=DEMO_TENANT):
        _reject_protected_tenant(DEMO_TENANT)


# -- exit-status conventions ----------------------------------------------------------------------


def test_the_skip_status_is_the_one_run_gates_renders_as_skip() -> None:
    """The script's skip exit status is the one ``run_gates.py`` renders as ``[SKIP]``."""
    assert cli.SKIP_EXIT_STATUS == GATE_SKIP_EXIT_STATUS


def test_an_unbuildable_store_skips_and_says_nothing_was_seeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbuildable store exits ``3`` and reports that nothing was seeded."""

    def refuse(**_kwargs: object) -> Store:
        raise NoCredentialsError()

    monkeypatch.setattr(cli, "build_store", refuse)
    report = cli.run(table_prefix="")

    assert report.status == "skip"
    assert report.exit_code() == cli.SKIP_EXIT_STATUS
    assert report.passes_completed == 0
    assert "could not construct" in report.reason


def test_a_missing_aws_profile_skips_rather_than_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ProfileNotFound`` while building the store exits ``3``, not a failure."""

    def refuse(**_kwargs: object) -> Store:
        raise ProfileNotFound(profile="not-a-profile")

    monkeypatch.setattr(cli, "build_store", refuse)
    report = cli.run(table_prefix="")

    assert report.status == "skip"
    assert report.exit_code() == cli.SKIP_EXIT_STATUS
    assert report.passes_completed == 0


def test_the_unreachable_classifier_admits_a_missing_profile_and_still_refuses_a_defect() -> None:
    """The unreachable classifiers admit ``ProfileNotFound`` and do not admit an ordinary defect."""
    import scripts.check_canary_tenant_empty as canary_gate

    assert cli.is_unreachable_failure(ProfileNotFound(profile="x")) is True
    assert canary_gate.is_unreachable_failure(ProfileNotFound(profile="x")) is True
    assert cli.is_unreachable_failure(AttributeError("typo")) is False
    assert cli.is_unreachable_failure(ValueError("answered")) is False


def test_a_defect_inside_build_store_is_a_failure_not_a_credential_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``AttributeError`` inside ``build_store`` is a failure, not the exit-``3`` skip."""

    def broken(**_kwargs: object) -> Store:
        raise AttributeError("Store has no attribute 'tabel_prefix'")

    monkeypatch.setattr(cli, "build_store", broken)
    report = cli.run(table_prefix="")

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert report.exit_code() != cli.SKIP_EXIT_STATUS
    assert "AttributeError" in report.reason
    assert "not an absent credential" in report.reason


def test_an_unreadable_store_skips_before_the_seeder_is_ever_called(store: Store) -> None:
    """An unreadable store skips at the pre-census, before the seeder is called."""
    seeder = RecordingSeeder()
    failing = ReadFailingStore(store, allowed_reads=0)

    report = cli.run(table_prefix="", store=failing, seeder=seeder)

    assert report.status == "skip"
    assert report.exit_code() == cli.SKIP_EXIT_STATUS
    assert seeder.calls == []
    assert "was not written" in report.reason


def test_a_readable_store_whose_write_fails_is_a_failure_not_a_skip(store: Store) -> None:
    """A store that reads but cannot be written to is a real failure — the corpus may be partial."""
    report = cli.run(table_prefix="", store=store, seeder=ExplodingSeeder(fail_on_call=1))

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert report.passes_completed == 0
    assert "first seeding pass failed" in report.reason
    assert "partial corpus" in report.reason


def test_a_second_pass_failure_reports_that_one_pass_landed(store: Store) -> None:
    """A second-pass failure's reason states that one pass landed."""
    report = cli.run(table_prefix="", store=store, seeder=ExplodingSeeder(fail_on_call=2))

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert report.passes_completed == 1
    assert report.census_after_first["accounts"] == 1
    assert "idempotency is unproven" in report.reason


@pytest.mark.parametrize(
    "aws_code",
    ["AccessDeniedException", "ValidationException", "ProvisionedThroughputExceededException"],
)
def test_a_store_that_answered_with_a_refusal_fails_rather_than_skipping(
    store: Store, aws_code: str
) -> None:
    """An access-denied, validation or throttle refusal is a failure, not a skip."""
    seeder = RecordingSeeder()
    failing = ReadFailingStore(store, allowed_reads=0, error=_store_error(aws_code))

    report = cli.run(table_prefix="", store=failing, seeder=seeder)

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert report.exit_code() != cli.SKIP_EXIT_STATUS
    assert seeder.calls == [], "nothing may be written on this path"
    assert "not a skip" in report.reason


def test_an_empty_tenant_after_two_successful_passes_is_a_failure(store: Store) -> None:
    """An empty tenant after two successful passes is a failure."""
    report = cli.run(table_prefix="", store=store, seeder=RecordingSeeder(rows_per_pass=0))

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert sum(report.census_after_second.values()) == 0
    assert "reads back empty" in report.reason


def test_a_second_census_failure_is_unverified_and_never_claims_nothing_was_seeded(
    store: Store,
) -> None:
    """A second-census failure is ``unverified`` (exit ``1``), never the nothing-seeded skip."""
    # Pre-census plus the first post-seed census, then failure on the second post-seed census.
    failing = ReadFailingStore(store, allowed_reads=2 * len(TENANT_SCOPED_TABLES))

    report = cli.run(table_prefix="", store=failing, seeder=RecordingSeeder())

    assert report.status == "unverified"
    assert report.exit_code() == 1
    assert report.exit_code() != cli.SKIP_EXIT_STATUS
    assert report.passes_completed == 2
    assert "seeded twice" in report.reason
    assert "idempotency is unproven" in report.reason


def test_an_unreadable_bystander_is_unverified_and_never_a_pass(store: Store) -> None:
    """An unreadable bystander is ``unverified`` and never exits ``0``."""
    failing = TenantSelectiveFailingStore(store, failing_tenant=CANARY_TENANT)

    report = cli.run(table_prefix="", store=failing, seeder=RecordingSeeder())

    assert report.status == "unverified"
    assert report.exit_code() == 1
    assert report.exit_code() != 0
    assert report.passes_completed == 2
    assert "bystander check could not be completed" in report.reason


def test_a_census_failure_after_seeding_is_unverified_and_never_a_pass(store: Store) -> None:
    """A census failure after seeding is ``unverified`` (exit ``1``), and the reason says so."""
    # One successful read for the pre-census sweep of every tenant-scoped table, then failure.
    failing = ReadFailingStore(store, allowed_reads=len(TENANT_SCOPED_TABLES))

    report = cli.run(table_prefix="", store=failing, seeder=RecordingSeeder())

    assert report.status == "unverified"
    assert report.exit_code() == 1
    assert report.passes_completed == 1
    assert "was seeded once" in report.reason
    assert "the property is unproven" in report.reason


# -- the idempotency and determinism checks actually catch their defects ---------------------------


def test_an_idempotent_seeder_passes_and_reports_a_stable_census(store: Store) -> None:
    seeder = RecordingSeeder(rows_per_pass=4)

    report = cli.run(table_prefix="", store=store, seeder=seeder)

    assert report.status == "pass"
    assert report.exit_code() == 0
    assert report.passes_completed == 2
    assert seeder.calls == [DEMO_TENANT, DEMO_TENANT]
    assert report.census_after_first == report.census_after_second
    assert report.census_after_second["accounts"] == 4


def test_a_duplicating_seeder_fails_and_the_delta_is_named(store: Store) -> None:
    """A seeder that grows the tenant fails, and the reason names the table and both counts."""
    report = cli.run(table_prefix="", store=store, seeder=DuplicatingSeeder())

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert report.passes_completed == 2
    assert "not idempotent" in report.reason
    assert "accounts:1->2" in report.reason


def test_a_nondeterministic_seeder_with_a_stable_census_still_fails(store: Store) -> None:
    """A seeder with a stable census but changing record identities fails."""
    report = cli.run(table_prefix="", store=store, seeder=NonDeterministicSeeder())

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert report.census_after_first == report.census_after_second
    assert "not deterministic" in report.reason


# -- the bystander guard this script runs for itself -----------------------------------------------


def test_a_planted_canary_row_fails_the_run_and_is_named(store: Store) -> None:
    """A planted ``canary`` row fails the run and is named in the reason."""
    _write_account(store, tenant=CANARY_TENANT, account_id="acct-leaked")

    report = cli.run(table_prefix="", store=store, seeder=RecordingSeeder())

    assert report.status == "fail"
    assert report.exit_code() == 1
    assert report.passes_completed == 2
    assert "accounts:account_id=acct-leaked" in report.reason


def test_the_bystander_check_is_reached_only_after_a_clean_idempotent_seed(store: Store) -> None:
    """A planted row alongside a duplicating seeder reports the idempotency defect first."""
    _write_account(store, tenant=CANARY_TENANT, account_id="acct-leaked")

    report = cli.run(table_prefix="", store=store, seeder=DuplicatingSeeder())

    assert report.status == "fail"
    assert "not idempotent" in report.reason


# -- the real seeder, end to end ------------------------------------------------------------------


def test_seeding_the_real_demo_corpus_twice_leaves_the_census_unchanged(store: Store) -> None:
    """Seeding the real ``fixtures.demo_seed`` corpus twice leaves the census unchanged."""
    report = cli.run(table_prefix="", store=store)

    assert report.status == "pass"
    assert report.exit_code() == 0
    assert report.passes_completed == 2
    assert report.census_after_first == report.census_after_second
    assert set(report.census_after_second) == set(TENANT_SCOPED_TABLES)
    assert report.census_after_second["opportunities"] > 0
    assert report.census_after_second["activities"] > 0
    assert sum(report.census_after_second.values()) > 1000


# -- census and delta helpers ---------------------------------------------------------------------


def test_the_census_names_every_tenant_scoped_table_including_empty_ones(store: Store) -> None:
    """The census names every tenant-scoped table, including empty ones."""
    counts = cli.census_tenant(store, tenant=DEMO_TENANT)

    assert set(counts) == set(TENANT_SCOPED_TABLES)
    assert set(counts.values()) == {0}


def test_the_census_paginates_past_a_single_page(store: Store) -> None:
    """The census follows the cursor past a forced one-item page boundary."""
    for index in range(12):
        _write_account(store, tenant=DEMO_TENANT, account_id=f"acct-{index:04d}")
    paged = PagingStore(store, page_size=1)

    counts = cli.census_tenant(paged, tenant=DEMO_TENANT)

    assert counts["accounts"] == 12
    assert paged.reads_attempted > 12, "each page plus a terminating read per table"


def test_a_partial_census_is_never_returned(store: Store) -> None:
    """A census read failure raises rather than returning a partial count."""
    failing = ReadFailingStore(store, allowed_reads=1)

    with pytest.raises(cli.CensusUnreadableError, match="could not count rows"):
        cli.census_tenant(failing, tenant=DEMO_TENANT)


def test_a_non_advancing_pagination_cursor_raises_instead_of_hanging(store: Store) -> None:
    """A non-advancing pagination cursor raises a named error instead of looping."""
    with pytest.raises(cli.CensusUnreadableError, match="did not advance"):
        cli.census_tenant(NonAdvancingCursorStore(store), tenant=DEMO_TENANT)


def test_describe_census_delta_is_empty_for_equal_censuses() -> None:
    assert cli.describe_census_delta({"accounts": 3}, {"accounts": 3}) == ""


def test_describe_census_delta_names_changes_in_both_directions() -> None:
    delta = cli.describe_census_delta(
        {"accounts": 3, "notes": 5}, {"accounts": 4, "notes": 5, "contacts": 2}
    )

    assert "accounts:3->4" in delta
    assert "contacts:0->2" in delta
    assert "notes" not in delta


# -- reporting -------------------------------------------------------------------------------------


def test_table_prefix_resolves_flag_then_environment_then_empty() -> None:
    assert cli.resolve_table_prefix("flag-", {cli.TABLE_PREFIX_ENV_VAR: "env-"}) == "flag-"
    assert cli.resolve_table_prefix(None, {cli.TABLE_PREFIX_ENV_VAR: "env-"}) == "env-"
    assert cli.resolve_table_prefix(None, {}) == ""


def test_build_store_forwards_the_table_prefix_and_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_store`` forwards ``--table-prefix``, ``--endpoint-url`` and the region, and names a
    profile.

    The session class is intercepted to record the resolved profile/region and then build a
    profile-less client, so the endpoint and region are read off botocore.
    """
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    real_session_cls = boto3.session.Session
    resolved: list[dict[str, object]] = []

    class _ProfilelessSession:
        """Records the resolved profile/region, then delegates to a session naming no profile."""

        def __init__(self, **kwargs: object) -> None:
            resolved.append(kwargs)

        def client(self, service_name: str, **kwargs: object) -> Any:
            return real_session_cls().client(service_name, **kwargs)

    monkeypatch.setattr(boto3.session, "Session", _ProfilelessSession)

    built = cli.build_store(
        table_prefix="pfx-", region="us-east-1", endpoint_url="http://127.0.0.1:8000"
    )

    # Proves the interception was used.
    assert [kwargs.get("region_name") for kwargs in resolved] == ["us-east-1"]
    assert isinstance(resolved[0].get("profile_name"), str)
    assert resolved[0]["profile_name"] != ""
    assert built.physical_name("accounts") == "pfx-accounts"
    client_meta = built._client.meta  # noqa: SLF001
    assert client_meta.endpoint_url == "http://127.0.0.1:8000"
    assert client_meta.region_name == "us-east-1"


def test_an_unknown_status_never_reports_success_or_a_skip() -> None:
    """``exit_code`` fails closed: only the exact ``"pass"`` and ``"skip"`` strings get 0 and 3."""
    unknown = cli.SeedReport(status="something-new")  # type: ignore[arg-type]

    assert unknown.exit_code() == 1


def test_the_pass_line_states_the_row_count_an_operator_should_see(store: Store) -> None:
    """The pass line states the seeded row count."""
    report = cli.run(table_prefix="", store=store, seeder=RecordingSeeder(rows_per_pass=5))
    text = report.render_text()

    assert "[ pass ]" in text
    assert "5 row(s)" in text
    assert DEMO_TENANT in text


def test_the_failure_line_is_labelled_distinctly_from_the_unverified_line() -> None:
    """The failure line is labelled distinctly from the unverified line."""
    failed = cli.SeedReport(status="fail", reason="census grew.").render_text()
    unverified = cli.SeedReport(status="unverified", reason="could not read back.").render_text()

    assert failed.startswith("[FAIL]")
    assert unverified.startswith("[UNVERIFIED]")


def test_the_skip_line_states_that_nothing_was_seeded() -> None:
    """The skip line states that nothing was seeded."""
    text = cli.SeedReport(status="skip", reason="no credential.").render_text()

    assert "[SKIP]" in text
    assert "Nothing was seeded" in text


def test_the_json_report_shape_is_pinned(store: Store) -> None:
    report = cli.run(table_prefix="", store=store, seeder=RecordingSeeder(rows_per_pass=2))
    payload = json.loads(json.dumps(report.to_json_dict()))

    assert payload["script"] == cli.SCRIPT_NAME
    assert payload["tenant"] == DEMO_TENANT
    assert payload["status"] == "pass"
    assert payload["passes_completed"] == 2
    assert payload["total_rows"] == 2
    assert payload["census_after_second"]["accounts"] == 2
    assert payload["reason"] == ""


def test_main_emits_json_and_returns_the_reports_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main`` emits JSON and returns the exit code of the report ``run`` produced."""

    def refuse(**_kwargs: object) -> Store:
        raise NoCredentialsError()

    monkeypatch.setattr(cli, "build_store", refuse)

    exit_code = cli.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == cli.SKIP_EXIT_STATUS
    assert payload["status"] == "skip"
    assert payload["passes_completed"] == 0


class TestARemoteSeedRequiresANamedTarget:
    """A remote seed without ``--outputs`` naming its target is refused before any write."""

    def test_a_remote_build_without_outputs_refuses(self) -> None:
        with pytest.raises(cli.UnverifiedTargetError) as caught:
            cli.build_store(table_prefix="p", profile="whatever", region="us-west-2")
        message = str(caught.value)
        assert "--outputs" in message
        assert "Nothing was written" in message

    def test_a_local_endpoint_is_exempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A loopback endpoint (run_local_dynamodb.sh) is exempt from the target check."""
        seen: list[str | None] = []

        class _Session:
            def client(self, _service: str, **kwargs: object) -> object:
                seen.append(str(kwargs.get("endpoint_url")))
                return object()

        import boto3 as _boto3

        monkeypatch.setattr(_boto3.session, "Session", lambda **_kwargs: _Session())
        monkeypatch.setattr(cli, "Store", lambda **_kwargs: "store")
        cli.build_store(table_prefix="p", endpoint_url="http://localhost:8000")
        assert seen == ["http://localhost:8000"]

    def test_run_reports_the_refusal_as_skip_never_as_a_pass(self) -> None:
        """`run` reports the refusal as `skip` (exit 3), never `pass`."""
        report = cli.run(table_prefix="p", profile="whatever", region="us-west-2")
        assert report.status == "skip"
        assert "--outputs" in (report.reason or "")

    def test_the_cli_exposes_outputs(self) -> None:
        """The CLI's option surface includes `--outputs`."""
        options = {
            option for action in cli.build_arg_parser()._actions for option in action.option_strings
        }
        assert "--outputs" in options, sorted(options)


def test_the_seed_cause_walk_ignores_a_context_only_connection_failure() -> None:
    """A connection error that is only ``__context__`` of a `ServiceBusy` is not its cause."""
    from botocore.exceptions import EndpointConnectionError

    from crm.errors import ServiceBusy

    def _raise_without_from() -> None:
        try:
            raise EndpointConnectionError(endpoint_url="http://127.0.0.1:1")
        except EndpointConnectionError:
            raise ServiceBusy("The service is busy. Retry shortly.")  # noqa: B904

    with pytest.raises(ServiceBusy) as raised:
        _raise_without_from()

    assert raised.value.__cause__ is None
    assert isinstance(raised.value.__context__, EndpointConnectionError)
    assert cli.is_unreachable_failure(raised.value) is False
