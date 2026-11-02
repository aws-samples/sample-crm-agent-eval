# SPDX-License-Identifier: MIT
"""Tests for `scripts/provision_gateway_principal.py`, against a moto-backed `crm.store.Store`.

The written item is read back through the store and fed to the Gateway interceptor's
service-principal mapping, so the shape is asserted against its real consumer.
"""

from __future__ import annotations

import ast
import io
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from fixtures.demo_seed import empty_account_id
from fixtures.generate import generate_dataset
from moto import mock_aws
from scripts import proc_run
from scripts import provision_gateway_principal as gp
from scripts.provision_demo_user import TargetMismatchError

from crm.authz import DEMO_TENANT
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.tenancy import CANARY_TENANT
from gateway.identity_interceptor import GatewayInterceptorConfig, _service_identity

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TF = REPO_ROOT / "infra" / "main.tf"
PRODUCTION_PY = sorted(
    REPO_ROOT / name
    for name in proc_run.run(
        ["git", "ls-files", "--", "scripts/*.py", "src/*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
)

PREFIX = "crm-agent-eval"
ACCOUNT = "123456789012"
OTHER_ACCOUNT = "111122223333"
REGION = "us-east-1"
PROFILE = "crm-gp-test"

_REP_ID_LOCAL = re.compile(r'^\s*gateway_service_principal_rep_id\s*=\s*"([^"]*)"', re.MULTILINE)


def _rep_id_from_main_tf(text: str) -> str:
    found = _REP_ID_LOCAL.findall(text)
    assert len(found) == 1, f"expected one gateway_service_principal_rep_id local, found {found}"
    return str(found[0])


def _outputs(*, account: str = ACCOUNT, region: str = REGION) -> dict[str, Any]:
    return {
        "dynamodb_table_names": {
            "value": {"eval_runs": f"{PREFIX}eval_runs", "reps": f"{PREFIX}reps"}
        },
        "reps_table_arn": {"value": f"arn:aws:dynamodb:{region}:{account}:table/{PREFIX}reps"},
    }


@pytest.fixture
def aws_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An isolated AWS config naming only `PROFILE`, under moto, with the reps table created."""
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
        create_all_tables(boto3.client("dynamodb", region_name=REGION), table_prefix=PREFIX)
        yield


def _store() -> Store:
    return Store(client=boto3.client("dynamodb", region_name=REGION), table_prefix=PREFIX)


def _reps_items() -> list[dict[str, Any]]:
    client = boto3.client("dynamodb", region_name=REGION)
    return list(client.scan(TableName=f"{PREFIX}reps")["Items"])


def _outputs_file(tmp_path: Path, outputs: object) -> str:
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(outputs), encoding="utf-8")
    return str(path)


def _cli(outputs_path: str, *extra: str) -> list[str]:
    return ["--outputs-json", outputs_path, "--profile", PROFILE, "--region", REGION, *extra]


class TestTheBook:
    def test_it_is_every_generated_demo_account_plus_the_empty_account(self) -> None:
        generated = {str(a["account_id"]) for a in generate_dataset(DEMO_TENANT).accounts}
        book = gp.demo_book()
        assert set(book) > generated
        assert len(book) == len(generated) + 1
        (extra,) = set(book) - generated
        assert extra == empty_account_id(DEMO_TENANT)
        assert extra.startswith(f"acct-{DEMO_TENANT}-")

    def test_it_is_sorted_and_deterministic(self) -> None:
        assert gp.demo_book() == sorted(gp.demo_book())
        assert gp.demo_book() == gp.demo_book()

    def test_every_id_is_scoped_to_the_demo_tenant(self) -> None:
        assert all(account.startswith(f"acct-{DEMO_TENANT}-") for account in gp.demo_book())


class TestTheItem:
    @pytest.mark.usefixtures("aws_env")
    def test_the_item_read_back_has_the_service_principal_shape(self) -> None:
        gp.provision_gateway_principal(_store())
        item = _store().get_item("reps", {"rep_id": gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID})
        assert item is not None
        assert item["rep_id"] == "svc-mcp-gateway"
        assert item["tenant"] == DEMO_TENANT
        assert item["book"] == gp.demo_book()
        assert item["full_visibility"] is False
        assert item["status"] == "active"
        assert item["name"] == "MCP gateway service principal"

    @pytest.mark.usefixtures("aws_env")
    def test_a_rerun_converges_on_identical_content(self) -> None:
        first = gp.provision_gateway_principal(_store())
        before = _reps_items()
        second = gp.provision_gateway_principal(_store())
        assert first == second
        assert _reps_items() == before
        assert len(before) == 1

    @pytest.mark.usefixtures("aws_env")
    @pytest.mark.parametrize("tenant", [CANARY_TENANT, "eval-run1", ""])
    def test_any_tenant_but_demo_is_refused_with_nothing_written(self, tenant: str) -> None:
        with pytest.raises(ValueError, match="nothing was written"):
            gp.provision_gateway_principal(_store(), tenant=tenant)
        assert _reps_items() == []

    @pytest.mark.usefixtures("aws_env")
    def test_the_interceptor_maps_the_item_to_a_service_identity_with_the_book(self) -> None:
        gp.provision_gateway_principal(_store())
        config = GatewayInterceptorConfig(
            discovery_url="https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/openid-configuration",
            jwks_url="https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/jwks.json",
            allowed_client_ids=frozenset({"client"}),
            service_principal_rep_id=gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID,
            table_prefix=PREFIX,
        )
        identity = _service_identity(
            config,
            session_id="s-1",
            rep_lookup=lambda rep_id: _store().get_item("reps", {"rep_id": rep_id}),
        )
        assert identity is not None
        assert identity["principal_type"] == "service"
        assert identity["tenant"] == DEMO_TENANT
        assert identity["book"] == gp.demo_book()
        assert identity["full_visibility"] is False


def _private_names_imported_from_demo_seed(source: str) -> list[str]:
    tree = ast.parse(source)
    return [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "fixtures.demo_seed"
        for alias in node.names
        if alias.name.startswith("_")
    ]


class TestSeedAccessorIsPublic:
    def test_no_production_module_imports_a_private_demo_seed_name(self) -> None:
        offenders = {
            str(path.relative_to(REPO_ROOT)): names
            for path in PRODUCTION_PY
            if (names := _private_names_imported_from_demo_seed(path.read_text(encoding="utf-8")))
        }
        assert offenders == {}

    def test_non_vacuity_a_private_import_is_detected(self) -> None:
        mutated = "from fixtures.demo_seed import _EMPTY_ACCOUNT_LOCAL_ID, demo_account_ids\n"
        assert _private_names_imported_from_demo_seed(mutated) == ["_EMPTY_ACCOUNT_LOCAL_ID"]

    def test_the_scanned_set_includes_the_gateway_script(self) -> None:
        assert REPO_ROOT / "scripts" / "provision_gateway_principal.py" in PRODUCTION_PY


class TestTheRepIdContract:
    def test_it_equals_the_terraform_local(self) -> None:
        main_tf = MAIN_TF.read_text(encoding="utf-8")
        assert _rep_id_from_main_tf(main_tf) == gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID

    def test_non_vacuity_a_renamed_local_is_detected(self) -> None:
        mutated = MAIN_TF.read_text(encoding="utf-8").replace(
            'gateway_service_principal_rep_id = "svc-mcp-gateway"',
            'gateway_service_principal_rep_id = "svc-renamed"',
        )
        assert _rep_id_from_main_tf(mutated) != gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID


class TestTablePrefix:
    def test_it_is_derived_from_the_outputs(self) -> None:
        assert gp.resolve_table_prefix(_outputs(), None) == PREFIX

    def test_an_agreeing_flag_is_accepted(self) -> None:
        assert gp.resolve_table_prefix(_outputs(), PREFIX) == PREFIX

    def test_a_disagreeing_flag_is_refused(self) -> None:
        with pytest.raises(ValueError, match="differs"):
            gp.resolve_table_prefix(_outputs(), "other-")

    def test_the_flag_is_used_when_the_outputs_carry_no_table_names(self) -> None:
        assert gp.resolve_table_prefix({}, PREFIX) == PREFIX

    def test_no_flag_and_no_table_names_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no 'dynamodb_table_names'"):
            gp.resolve_table_prefix({}, None)

    @pytest.mark.parametrize("flag", [None, PREFIX])
    def test_malformed_table_names_are_refused_even_with_a_flag(self, flag: str | None) -> None:
        outputs = {"dynamodb_table_names": {"value": {"reps": f"{PREFIX}reps"}}}
        with pytest.raises(ValueError, match="eval_runs"):
            gp.resolve_table_prefix(outputs, flag)


class TestOutputsNameTheTarget:
    def test_outputs_with_an_arn_pass(self) -> None:
        gp.assert_outputs_name_target(_outputs())

    def test_outputs_naming_no_account_are_refused(self) -> None:
        outputs = _outputs()
        del outputs["reps_table_arn"]
        with pytest.raises(TargetMismatchError, match="cannot be verified"):
            gp.assert_outputs_name_target(outputs)


class TestCli:
    @pytest.mark.usefixtures("aws_env")
    def test_a_matching_target_writes_the_item(self, tmp_path: Path) -> None:
        assert gp.main(_cli(_outputs_file(tmp_path, _outputs()))) == 0
        item = _store().get_item("reps", {"rep_id": gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID})
        assert item is not None
        assert item["book"] == gp.demo_book()

    @pytest.mark.usefixtures("aws_env")
    def test_outputs_are_read_from_stdin_with_a_dash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_outputs())))
        assert gp.main(_cli("-")) == 0
        assert len(_reps_items()) == 1

    @pytest.mark.usefixtures("aws_env")
    def test_a_wrong_account_is_refused_before_any_write(self, tmp_path: Path) -> None:
        assert gp.main(_cli(_outputs_file(tmp_path, _outputs(account=OTHER_ACCOUNT)))) == 1
        assert _reps_items() == []

    @pytest.mark.usefixtures("aws_env")
    def test_a_wrong_region_is_refused_before_any_write(self, tmp_path: Path) -> None:
        assert gp.main(_cli(_outputs_file(tmp_path, _outputs(region="us-west-2")))) == 1
        assert _reps_items() == []

    @pytest.mark.usefixtures("aws_env")
    def test_outputs_naming_no_target_are_refused_before_any_write(self, tmp_path: Path) -> None:
        outputs = _outputs()
        del outputs["reps_table_arn"]
        assert gp.main(_cli(_outputs_file(tmp_path, outputs))) == 1
        assert _reps_items() == []

    @pytest.mark.usefixtures("aws_env")
    def test_malformed_table_names_are_refused_before_any_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        outputs = _outputs()
        outputs["dynamodb_table_names"] = {"value": {"reps": f"{PREFIX}reps"}}
        path = _outputs_file(tmp_path, outputs)
        assert gp.main(_cli(path, "--table-prefix", PREFIX)) == 1
        assert "dynamodb_table_names" in capsys.readouterr().err
        assert _reps_items() == []

    @pytest.mark.usefixtures("aws_env")
    def test_a_disagreeing_table_prefix_is_refused_before_any_write(self, tmp_path: Path) -> None:
        path = _outputs_file(tmp_path, _outputs())
        assert gp.main(_cli(path, "--table-prefix", "other-")) == 1
        assert _reps_items() == []

    @pytest.mark.parametrize("body", ["not json", "[]"])
    def test_unusable_outputs_exit_one_before_any_aws_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
    ) -> None:
        path = tmp_path / "outputs.json"
        path.write_text(body, encoding="utf-8")

        def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("constructed an AWS session before validating inputs")

        monkeypatch.setattr(boto3.session, "Session", explode)
        assert gp.main(_cli(str(path))) == 1

    def test_an_unknown_profile_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty_config = tmp_path / "empty-config"
        empty_config.write_text("", encoding="utf-8")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(empty_config))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty_config))
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        assert gp.main(_cli(_outputs_file(tmp_path, _outputs()))) == 1

    @pytest.mark.parametrize("missing", ["--profile", "--region", "--outputs-json"])
    def test_every_required_flag_is_required(self, tmp_path: Path, missing: str) -> None:
        argv = _cli(_outputs_file(tmp_path, _outputs()))
        index = argv.index(missing)
        del argv[index : index + 2]
        with pytest.raises(SystemExit) as raised:
            gp.main(argv)
        assert raised.value.code == 2
