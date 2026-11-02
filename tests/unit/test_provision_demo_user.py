# SPDX-License-Identifier: MIT
"""Tests for `scripts/provision_demo_user.py`.

The identity contract is asserted against the real consumer: the `reps` item this script writes
is found by `crm.auth.pre_token_generation.lambda_handler` for the user it created, and the
emitted claims name the demo tenant with a non-empty scope.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import scripts.provision_demo_user as provision_demo_user
from scripts.provision_demo_user import (
    EMAIL_ENV_VAR,
    PASSWORD_ENV_VAR,
    admin_group_from_outputs,
    ensure_cognito_user,
    main,
    provision,
    table_prefix_from_outputs,
    write_rep,
)

from crm.auth.pre_token_generation import lambda_handler
from crm.authz import ADMIN_GROUP, DEMO_TENANT
from crm.store.store import Store

USER_POOL_ID = "us-east-1_ABCDEFGHI"
EMAIL = "demo@example.test"
FAKE_SIGNIN_VALUE = "Sup3r-Secret-Passw0rd!"
COGNITO_UUID = "9f1c7a1e-2b3d-4c5e-8f70-a1b2c3d4e5f6"


@pytest.fixture(autouse=True)
def _no_ambient_demo_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear `CRM_DEMO_USER_EMAIL` for every test in this file; tests that need it set it."""
    monkeypatch.delenv(EMAIL_ENV_VAR, raising=False)


class _UsernameExistsException(Exception):  # noqa: N818 - mirrors botocore's own class name
    pass


class _Exceptions:
    UsernameExistsException = _UsernameExistsException


class FakeIdp:
    """Minimal `cognito-idp` stand-in that mimics `username_attributes = ["email"]`.

    The behaviour that matters: `admin_create_user` is passed the email but reports a
    pool-assigned UUID as `User.Username`.
    """

    exceptions = _Exceptions

    def __init__(self, *, existing: bool = False, username: str = COGNITO_UUID) -> None:
        self.username = username
        self._existing = existing
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_create_user", kwargs))
        if self._existing:
            raise _UsernameExistsException("User account already exists")
        return {"User": {"Username": self.username, "Enabled": True}}

    def admin_get_user(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_get_user", kwargs))
        return {"Username": self.username}

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_set_user_password", kwargs))
        return {}


class FakeStore:
    """Captures `put_item` calls; stands in for `Store` where DynamoDB is not needed."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, table: str, item: dict[str, Any]) -> None:
        self.items[(table, str(item["rep_id"]))] = item

    def get_item(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
        return self.items.get((table, str(key["rep_id"])))


def _outputs(**overrides: object) -> dict[str, object]:
    outputs: dict[str, object] = {
        "cognito_user_pool_id": {"value": USER_POOL_ID},
        "dynamodb_table_names": {
            "value": {"eval_runs": "crm-agent-eval-eval_runs", "reps": "crm-agent-eval-reps"}
        },
    }
    outputs.update(overrides)
    return outputs


class TestCognitoUsername:
    """The reps item must be keyed on the pool-assigned username, never the email."""

    def test_returns_the_pool_assigned_username_not_the_email(self) -> None:
        idp = FakeIdp()
        assert (
            ensure_cognito_user(
                idp, user_pool_id=USER_POOL_ID, email=EMAIL, password=FAKE_SIGNIN_VALUE
            )
            == COGNITO_UUID
        )

    def test_password_is_set_permanently_on_the_returned_username(self) -> None:
        idp = FakeIdp()
        ensure_cognito_user(idp, user_pool_id=USER_POOL_ID, email=EMAIL, password=FAKE_SIGNIN_VALUE)
        (name, kwargs) = idp.calls[-1]
        assert name == "admin_set_user_password"
        # A non-permanent password leaves the user in FORCE_CHANGE_PASSWORD, which the SPA's
        # SRP sign-in surfaces as a challenge it has no UI for.
        assert kwargs == {
            "UserPoolId": USER_POOL_ID,
            "Username": COGNITO_UUID,
            "Password": FAKE_SIGNIN_VALUE,
            "Permanent": True,
        }

    def test_an_existing_user_is_reused_via_admin_get_user(self) -> None:
        idp = FakeIdp(existing=True)
        assert (
            ensure_cognito_user(
                idp, user_pool_id=USER_POOL_ID, email=EMAIL, password=FAKE_SIGNIN_VALUE
            )
            == COGNITO_UUID
        )
        assert [name for name, _ in idp.calls] == [
            "admin_create_user",
            "admin_get_user",
            "admin_set_user_password",
        ]

    def test_an_empty_username_raises_rather_than_guessing(self) -> None:
        idp = FakeIdp(username="")
        with pytest.raises(RuntimeError, match="no Username"):
            ensure_cognito_user(
                idp, user_pool_id=USER_POOL_ID, email=EMAIL, password=FAKE_SIGNIN_VALUE
            )


class TestTriggerContract:
    """The written item is what the pre-token-generation trigger actually resolves."""

    def _claims(self, store: FakeStore, username: str) -> dict[str, str]:
        event: dict[str, Any] = {"userName": username, "response": {}}
        result = lambda_handler(event, None, store=store)
        details = result["response"]["claimsAndScopeOverrideDetails"]
        claims: dict[str, str] = details["idTokenGeneration"]["claimsToAddOrOverride"]
        return claims

    def test_provisioned_user_resolves_to_demo_tenant_claims_with_full_visibility(self) -> None:
        store = FakeStore()
        result = provision(
            idp=FakeIdp(),
            store=store,  # type: ignore[arg-type]
            user_pool_id=USER_POOL_ID,
            email=EMAIL,
            password=FAKE_SIGNIN_VALUE,
            book=[],
            full_visibility=True,
            seeder=None,
        )

        claims = self._claims(store, result["username"])
        assert claims["custom:rep_id"] == COGNITO_UUID
        assert claims["custom:tenant"] == DEMO_TENANT
        assert claims["custom:full_visibility"] == "true"

    def test_a_book_scoped_user_resolves_to_the_granted_accounts(self) -> None:
        store = FakeStore()
        result = provision(
            idp=FakeIdp(),
            store=store,  # type: ignore[arg-type]
            user_pool_id=USER_POOL_ID,
            email=EMAIL,
            password=FAKE_SIGNIN_VALUE,
            book=["acct-demo-0001", "acct-demo-0000"],
            full_visibility=False,
            seeder=None,
        )

        claims = self._claims(store, result["username"])
        assert claims["custom:book"] == "acct-demo-0000,acct-demo-0001"
        assert claims["custom:full_visibility"] == "false"

    def test_keying_the_item_on_the_email_would_yield_the_fail_closed_defaults(self) -> None:
        # Control for the two tests above: keying on the email yields the fail-closed claims.
        store = FakeStore()
        write_rep(
            store,  # type: ignore[arg-type]
            rep_id=EMAIL,
            tenant=DEMO_TENANT,
            book=["acct-demo-0000"],
            full_visibility=True,
        )

        claims = self._claims(store, COGNITO_UUID)
        assert claims["custom:book"] == ""
        assert claims["custom:full_visibility"] == "false"


class TestWriteRep:
    def test_full_visibility_is_a_real_bool_and_book_is_sorted(self) -> None:
        store = FakeStore()
        item = write_rep(
            store,  # type: ignore[arg-type]
            rep_id=COGNITO_UUID,
            tenant=DEMO_TENANT,
            book=["b", "a"],
            full_visibility=True,
        )
        # `_coerce_full_visibility` treats anything that is not literally True as False, so a
        # truthy string here would silently downgrade the user.
        assert item["full_visibility"] is True
        assert item["book"] == ["a", "b"]

    def test_the_name_defaults_to_the_demo_ui_user(self) -> None:
        item = write_rep(
            FakeStore(),  # type: ignore[arg-type]
            rep_id=COGNITO_UUID,
            tenant=DEMO_TENANT,
            book=[],
            full_visibility=False,
        )
        assert item["name"] == "Demo UI user"

    def test_a_given_name_is_written_and_stored(self) -> None:
        store = FakeStore()
        item = write_rep(
            store,  # type: ignore[arg-type]
            rep_id=COGNITO_UUID,
            tenant=DEMO_TENANT,
            book=[],
            full_visibility=False,
            name="Someone else",
        )
        assert item["name"] == "Someone else"
        stored = store.get_item("reps", {"rep_id": COGNITO_UUID})
        assert stored is not None and stored["name"] == "Someone else"

    def test_the_item_satisfies_the_reps_table_key_schema(self) -> None:
        # Contract against the table this writes to: `reps` is keyed by `rep_id` alone.
        from crm.store.schema import TABLES

        schema = TABLES["reps"]
        item = write_rep(
            FakeStore(),  # type: ignore[arg-type]
            rep_id=COGNITO_UUID,
            tenant=DEMO_TENANT,
            book=[],
            full_visibility=True,
        )
        assert schema.partition_key in item
        assert schema.sort_key is None
        # The gsi_tenant index is what tenant-scoped reads use; its keys must be present.
        gsi = schema.gsis["gsi_tenant"]
        assert gsi.partition_key in item and gsi.sort_key in item


class TestSeeding:
    def test_the_demo_tenant_is_seeded_before_the_user_is_created(self) -> None:
        seen: list[str] = []
        store = FakeStore()

        def seeder(_store: Any, *, tenant: str) -> None:
            seen.append(f"seed:{tenant}")

        class RecordingIdp(FakeIdp):
            def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
                seen.append("create_user")
                return super().admin_create_user(**kwargs)

        provision(
            idp=RecordingIdp(),
            store=store,  # type: ignore[arg-type]
            user_pool_id=USER_POOL_ID,
            email=EMAIL,
            password=FAKE_SIGNIN_VALUE,
            book=[],
            full_visibility=True,
            seeder=seeder,
        )
        assert seen == [f"seed:{DEMO_TENANT}", "create_user"]

    def test_the_real_seeder_accepts_the_demo_tenant(self) -> None:
        # `eval.tenancy.seed_tenant` refuses "demo"; the script's seeder is
        # `fixtures.generate.seed_tenant`, which permits it.
        from scripts.provision_demo_user import _default_seeder

        seeded: list[tuple[str, dict[str, Any]]] = []

        class CountingStore(FakeStore):
            def batch_put_items(self, table: str, items: Sequence[Mapping[str, Any]]) -> int:
                seeded.extend((table, dict(item)) for item in items)
                return len(items)

        _default_seeder()(CountingStore(), tenant=DEMO_TENANT)
        assert seeded, "the fixture seeder wrote nothing for the demo tenant"
        assert {table for table, _ in seeded} >= {"accounts", "contacts", "opportunities"}
        assert all(item.get("tenant") == DEMO_TENANT for _, item in seeded)


class TestTablePrefix:
    def test_prefix_is_derived_from_the_eval_runs_physical_name(self) -> None:
        assert table_prefix_from_outputs(_outputs()) == "crm-agent-eval-"

    def test_a_prefixless_deployment_derives_the_empty_string(self) -> None:
        outputs = _outputs(dynamodb_table_names={"value": {"eval_runs": "eval_runs"}})
        assert table_prefix_from_outputs(outputs) == ""

    @pytest.mark.parametrize("bad", [{"value": {"reps": "x-reps"}}, {"value": "not-a-map"}])
    def test_a_mapping_without_eval_runs_raises(self, bad: object) -> None:
        with pytest.raises(ValueError, match="eval_runs"):
            table_prefix_from_outputs(_outputs(dynamodb_table_names=bad))

    def test_the_prefix_produces_the_deployed_physical_names(self) -> None:
        # A stub client: physical_name() never calls DynamoDB, and None would build a real client.
        store = Store(table_prefix=table_prefix_from_outputs(_outputs()), client=object())
        assert store.physical_name("reps") == "crm-agent-eval-reps"


class TestCli:
    @pytest.fixture(autouse=True)
    def _password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PASSWORD_ENV_VAR, FAKE_SIGNIN_VALUE)

    def _outputs_file(self, tmp_path: Path, outputs: dict[str, object] | None = None) -> list[str]:
        path = tmp_path / "outputs.json"
        path.write_text(
            json.dumps(outputs if outputs is not None else _outputs()), encoding="utf-8"
        )
        return ["--outputs-json", str(path), "--email", EMAIL]

    def test_missing_password_env_var_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(PASSWORD_ENV_VAR, raising=False)
        assert main(self._outputs_file(tmp_path)) == 1

    def test_blank_password_env_var_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PASSWORD_ENV_VAR, "   ")
        assert main(self._outputs_file(tmp_path)) == 1

    def test_unparseable_outputs_exit_one(self, tmp_path: Path) -> None:
        path = tmp_path / "outputs.json"
        path.write_text("not json", encoding="utf-8")
        assert main(["--outputs-json", str(path), "--email", EMAIL]) == 1

    def test_a_json_array_of_outputs_exits_one(self, tmp_path: Path) -> None:
        path = tmp_path / "outputs.json"
        path.write_text("[]", encoding="utf-8")
        assert main(["--outputs-json", str(path), "--email", EMAIL]) == 1

    def test_absent_user_pool_output_exits_one(self, tmp_path: Path) -> None:
        outputs = _outputs()
        del outputs["cognito_user_pool_id"]
        assert main(self._outputs_file(tmp_path, outputs)) == 1

    def test_no_full_visibility_with_an_empty_book_exits_one(self, tmp_path: Path) -> None:
        # Such a user could see nothing.
        assert main([*self._outputs_file(tmp_path), "--no-full-visibility"]) == 1

    def test_every_early_exit_happens_before_any_aws_client_is_constructed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import boto3

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("constructed an AWS session before validating inputs")

        monkeypatch.setattr(boto3.session, "Session", explode)
        monkeypatch.delenv(PASSWORD_ENV_VAR, raising=False)
        assert main(self._outputs_file(tmp_path)) == 1


class _ReachedAwsSessionError(Exception):
    """Raised in place of constructing a real AWS session, showing validation was passed."""


def _explode_session(*_args: Any, **_kwargs: Any) -> Any:
    raise _ReachedAwsSessionError("main() got past input validation and reached the AWS session")


class TestTheEmailMatchesWhatTheJourneysRead:
    """`--email` must match `CRM_DEMO_USER_EMAIL` (read by `tests/e2e/browser/fixtures/auth.ts`).

    The comparison is exact apart from surrounding whitespace; an unset or blank variable is no
    conflict.
    """

    @pytest.fixture(autouse=True)
    def _password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PASSWORD_ENV_VAR, FAKE_SIGNIN_VALUE)

    def _args(self, tmp_path: Path, email: str = EMAIL) -> list[str]:
        path = tmp_path / "outputs.json"
        path.write_text(json.dumps(_outputs()), encoding="utf-8")
        return ["--outputs-json", str(path), "--email", email, "--skip-seed"]

    def test_a_conflicting_env_var_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(EMAIL_ENV_VAR, "someone-else@example.test")
        assert main(self._args(tmp_path)) == 1

    def test_a_case_only_difference_is_also_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A case-only difference between the two addresses is refused."""
        monkeypatch.setenv(EMAIL_ENV_VAR, EMAIL.upper())
        assert main(self._args(tmp_path)) == 1

    def test_surrounding_whitespace_is_not_a_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Surrounding whitespace in the env var does not count as a conflict."""
        monkeypatch.setenv(EMAIL_ENV_VAR, f"  {EMAIL}  ")
        monkeypatch.setattr("boto3.session.Session", _explode_session)
        with pytest.raises(_ReachedAwsSessionError):
            main(self._args(tmp_path))

    def test_a_matching_env_var_is_permitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A matching env var is not refused: the run reaches the AWS session."""
        monkeypatch.setenv(EMAIL_ENV_VAR, EMAIL)
        monkeypatch.setattr("boto3.session.Session", _explode_session)
        with pytest.raises(_ReachedAwsSessionError):
            main(self._args(tmp_path))

    def test_an_unset_env_var_is_permitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset env var is not a conflict."""
        monkeypatch.delenv(EMAIL_ENV_VAR, raising=False)
        monkeypatch.setattr("boto3.session.Session", _explode_session)
        with pytest.raises(_ReachedAwsSessionError):
            main(self._args(tmp_path))

    def test_a_blank_env_var_is_permitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank env var is treated as unset."""
        monkeypatch.setenv(EMAIL_ENV_VAR, "   ")
        monkeypatch.setattr("boto3.session.Session", _explode_session)
        with pytest.raises(_ReachedAwsSessionError):
            main(self._args(tmp_path))

    def test_the_refusal_precedes_any_aws_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mismatch is refused before any AWS client is constructed."""
        monkeypatch.setenv(EMAIL_ENV_VAR, "someone-else@example.test")
        monkeypatch.setattr("boto3.session.Session", _explode_session)
        assert main(self._args(tmp_path)) == 1

    def test_the_two_compared_values_come_from_independent_sources(self) -> None:
        """The script compares `args.email` against the environment, not one source twice."""
        source = Path(provision_demo_user.__file__).read_text(encoding="utf-8")
        block = source[source.index("def main(") :]
        assert "args.email" in block, "the CLI side of the comparison is gone"
        assert "environ.get(EMAIL_ENV_VAR" in block, (
            "the environment side of the comparison is gone"
        )


class TestItRefusesToWriteToAnUnintendedAccount:
    """The script refuses a session whose account or region differs from the outputs' target."""

    #: The canonical AWS-documentation example id, the only account literal permitted in tracked
    #: content (`check_no_account_id`).
    PLACEHOLDER = "123456789012"
    #: A second, different account, derived at runtime because that gate rejects the literal.
    OTHER = PLACEHOLDER[::-1]

    def _outputs(self, account: str = PLACEHOLDER, region: str = "us-east-1") -> dict[str, Any]:
        return {
            "agent_relay_role_arn": {"value": f"arn:aws:iam::{account}:role/relay"},
            "api_endpoint": {"value": f"arn:aws:execute-api:{region}:{account}:abc123/prod"},
        }

    class _Session:
        def __init__(self, account: str) -> None:
            self._account = account

        def client(self, service: str) -> Any:
            assert service == "sts", f"the guard must not build a {service} client"
            account = self._account

            class _Sts:
                def get_caller_identity(self) -> dict[str, str]:
                    return {"Account": account}

            return _Sts()

    def test_a_matching_account_and_region_is_allowed(self) -> None:
        """A matching account and region is allowed."""
        provision_demo_user.assert_target_matches_outputs(
            self._Session(self.PLACEHOLDER),
            outputs=self._outputs(),
            region="us-east-1",
            profile="default",
        )

    def test_a_wrong_account_refuses_and_names_both(self) -> None:
        with pytest.raises(provision_demo_user.TargetMismatchError) as caught:
            provision_demo_user.assert_target_matches_outputs(
                self._Session(self.OTHER),
                outputs=self._outputs(),
                region="us-east-1",
                profile="someone-elses",
            )
        message = str(caught.value)
        assert self.OTHER in message, "the resolved account is not named"
        assert self.PLACEHOLDER in message, "the expected account is not named"
        assert "Nothing has been written" in message
        assert "AWS_PROFILE" in message, "the message must say where the wrong value came from"

    def test_a_wrong_region_refuses_before_any_sts_call(self) -> None:
        """A region mismatch refuses before any client is constructed, so before STS is called."""

        class _NoClients:
            def client(self, service: str) -> Any:
                raise AssertionError(f"built a {service} client before the region check")

        with pytest.raises(provision_demo_user.TargetMismatchError) as caught:
            provision_demo_user.assert_target_matches_outputs(
                _NoClients(), outputs=self._outputs(), region="us-west-2", profile="default"
            )
        assert "us-west-2" in str(caught.value) and "us-east-1" in str(caught.value)
        assert "AWS_REGION" in str(caught.value)

    def test_outputs_with_no_arn_are_silent(self) -> None:
        """Outputs carrying no ARN produce no refusal."""
        provision_demo_user.assert_target_matches_outputs(
            self._Session(self.OTHER),
            outputs={"cognito_user_pool_id": {"value": "us-east-1_abc123"}},
            region="us-east-1",
            profile="default",
        )

    def test_the_cli_accepts_a_profile(self) -> None:
        """The parser declares `--profile` exactly, read from its `option_strings`."""
        options = {
            option
            for action in provision_demo_user.build_arg_parser()._actions
            for option in action.option_strings
        }
        assert "--profile" in options, f"the CLI exposes no --profile: {sorted(options)}"
        assert "--region" in options, "positive control for the assertion above"


class _GroupRecordingIdp(FakeIdp):
    """`FakeIdp` that records `admin_add_user_to_group` and holds group membership like Cognito."""

    def __init__(self, *, existing: bool = False) -> None:
        super().__init__(existing=existing)
        self.members: set[tuple[str, str]] = set()

    def admin_add_user_to_group(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_add_user_to_group", kwargs))
        self.members.add((kwargs["Username"], kwargs["GroupName"]))
        return {}


class TestAdminFlag:
    """`--admin` adds the provisioned user to the admin group, idempotently."""

    def _provision(self, idp: _GroupRecordingIdp, *, admin_group: str | None) -> dict[str, Any]:
        return provision(
            idp=idp,
            store=FakeStore(),  # type: ignore[arg-type]
            user_pool_id=USER_POOL_ID,
            email=EMAIL,
            password=FAKE_SIGNIN_VALUE,
            book=[],
            full_visibility=True,
            seeder=None,
            admin_group=admin_group,
        )

    def test_the_user_joins_the_admin_group_by_its_cognito_username(self) -> None:
        idp = _GroupRecordingIdp()
        result = self._provision(idp, admin_group=ADMIN_GROUP)
        assert idp.members == {(COGNITO_UUID, ADMIN_GROUP)}
        add = [kw for name, kw in idp.calls if name == "admin_add_user_to_group"]
        assert add == [
            {"UserPoolId": USER_POOL_ID, "Username": COGNITO_UUID, "GroupName": ADMIN_GROUP}
        ]
        assert result["admin_group"] == ADMIN_GROUP

    def test_a_re_run_on_an_existing_user_leaves_one_membership(self) -> None:
        idp = _GroupRecordingIdp()
        self._provision(idp, admin_group=ADMIN_GROUP)
        idp._existing = True
        self._provision(idp, admin_group=ADMIN_GROUP)
        assert idp.members == {(COGNITO_UUID, ADMIN_GROUP)}

    def test_without_the_flag_no_group_is_touched(self) -> None:
        idp = _GroupRecordingIdp()
        self._provision(idp, admin_group=None)
        assert "admin_add_user_to_group" not in [name for name, _ in idp.calls]

    def test_the_group_is_the_one_authz_checks_and_must_match_the_output(self) -> None:
        assert admin_group_from_outputs(_outputs()) == ADMIN_GROUP
        assert admin_group_from_outputs(_outputs(admin_group_name={"value": ADMIN_GROUP})) == (
            ADMIN_GROUP
        )
        with pytest.raises(ValueError, match="would grant nothing"):
            admin_group_from_outputs(_outputs(admin_group_name={"value": "admins"}))

    def test_main_passes_the_admin_group_only_with_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PASSWORD_ENV_VAR, FAKE_SIGNIN_VALUE)
        captured: list[str | None] = []

        def _fake_provision(**kwargs: Any) -> dict[str, Any]:
            captured.append(kwargs["admin_group"])
            return {
                "username": COGNITO_UUID,
                "email": EMAIL,
                "rep": {"tenant": DEMO_TENANT, "full_visibility": True, "book": []},
                "admin_group": kwargs["admin_group"],
            }

        class _Session:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            def client(self, _name: str) -> object:
                return object()

        monkeypatch.setattr("boto3.session.Session", _Session)
        monkeypatch.setattr(
            provision_demo_user, "assert_target_matches_outputs", lambda *_a, **_k: None
        )
        monkeypatch.setattr(provision_demo_user, "provision", _fake_provision)
        path = tmp_path / "outputs.json"
        path.write_text(json.dumps(_outputs()), encoding="utf-8")
        args = ["--outputs-json", str(path), "--email", EMAIL, "--skip-seed"]
        assert main(args) == 0
        assert main([*args, "--admin"]) == 0
        assert captured == [None, ADMIN_GROUP]

    def test_main_refuses_an_admin_group_output_authz_does_not_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PASSWORD_ENV_VAR, FAKE_SIGNIN_VALUE)
        monkeypatch.setattr("boto3.session.Session", _explode_session)
        path = tmp_path / "outputs.json"
        path.write_text(
            json.dumps(_outputs(admin_group_name={"value": "admins"})), encoding="utf-8"
        )
        assert main(["--outputs-json", str(path), "--email", EMAIL, "--admin"]) == 1
