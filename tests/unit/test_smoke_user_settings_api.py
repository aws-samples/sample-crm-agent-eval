# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.user_settings_api`.

The smoke's POST is wired to the real `crm.handlers.user_settings.handler` over a `moto` DynamoDB,
with each token decoded through the recorded REST v1 event fixture. The checks therefore read the
handler's actual responses; only Cognito, CloudFront and the tenant seeding are replaced.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from scripts.smoke import user_settings_api as mod
from scripts.smoke.admin_authz import ApiPost, SimulationRow
from scripts.smoke.cognito_claims import ProvisionedSmokeUser
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.tenant import SmokeTenant

from crm.envelope import CLAIM_REP_ID, CLAIM_TENANT
from crm.handlers import capabilities as capabilities_module
from crm.handlers import user_settings as handler_module
from crm.store.keys import user_settings_key
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from providers.capabilities import Family, Tier, load_capability_matrix
from providers.tier_tuning import TIERS

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_REST_V1_EVENT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "events"
    / "apigw_v1_proxy_post_get_account.json"
)
_TENANT = SmokeTenant(run_id="smokerun", tenant="smoke-smokerun")

#: `(event) -> response`: the handler the fake API forwards to; a test may wrap it.
Handler = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _event(body: Mapping[str, Any], *, rep_id: str | None) -> dict[str, Any]:
    event: dict[str, Any] = json.loads(_REST_V1_EVENT.read_text(encoding="utf-8"))
    event["resource"] = event["path"] = "/user_settings"
    if rep_id is None:
        del event["requestContext"]["authorizer"]
    else:
        claims = event["requestContext"]["authorizer"]["claims"]
        claims[CLAIM_REP_ID] = rep_id
        claims[CLAIM_TENANT] = _TENANT.tenant
    event["body"] = json.dumps(dict(body))
    return event


def _api(store: Store, *, wrap: Callable[[Handler], Handler] | None = None) -> ApiPost:
    def real(event: dict[str, Any]) -> dict[str, Any]:
        return handler_module.handler(event, store=store)

    handler = wrap(real) if wrap is not None else real

    def post(route: str, token: str | None, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        assert route == mod.ROUTE
        response = handler(_event(body, rep_id=token))
        return int(response["statusCode"]), json.loads(response["body"])

    return post


def _capabilities_event(*, rep_id: str | None) -> dict[str, Any]:
    event = _event({}, rep_id=rep_id)
    event["resource"] = event["path"] = "/capabilities"
    event["httpMethod"] = "GET"
    event["body"] = None
    return event


def _api_get(*, wrap: Callable[[Handler], Handler] | None = None) -> mod.ApiGet:
    def real(event: dict[str, Any]) -> dict[str, Any]:
        return capabilities_module.handler(event)

    handler = wrap(real) if wrap is not None else real

    def get(route: str, token: str | None) -> tuple[int, dict[str, Any]]:
        assert route == mod.CAPABILITIES_ROUTE
        response = handler(_capabilities_event(rep_id=token))
        return int(response["statusCode"]), json.loads(response["body"])

    return get


class FakeProvisioner:
    def __init__(self, rep_id: str) -> None:
        self.rep_id = rep_id
        self.cleaned = False

    def provision(self) -> ProvisionedSmokeUser:
        return ProvisionedSmokeUser(
            username=self.rep_id,
            rep_id=self.rep_id,
            tenant=_TENANT.tenant,
            book=frozenset(),
            id_token=self.rep_id,
            detail="TOTP",
        )

    def cleanup(self) -> Iterator[CheckResult]:
        self.cleaned = True
        yield CheckResult(resource="smoke_user", check="cognito_user_deleted", status="pass")


@pytest.fixture(autouse=True)
def _tenant_lifecycle(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []

    def allocate(_store: Any) -> SmokeTenant:
        events.append("alloc")
        return _TENANT

    def seed(_store: Any, _tenant: SmokeTenant) -> None:
        events.append("seed")

    def teardown(_store: Any, _tenant: SmokeTenant) -> dict[str, int]:
        events.append("teardown")
        return {"reps": 0}

    monkeypatch.setattr(mod, "allocate_smoke_tenant", allocate)
    monkeypatch.setattr(mod, "seed_smoke_tenant", seed)
    monkeypatch.setattr(mod, "teardown_smoke_tenant", teardown)
    return events


def _run(
    store: Store, *, post: ApiPost | None = None, get: mod.ApiGet | None = None
) -> tuple[list[CheckResult], dict[str, CheckResult], list[FakeProvisioner]]:
    users = iter([FakeProvisioner("rep-a"), FakeProvisioner("rep-b")])
    made: list[FakeProvisioner] = []

    def factory(tenant: SmokeTenant) -> FakeProvisioner:
        assert tenant == _TENANT
        provisioner = next(users)
        made.append(provisioner)
        return provisioner

    results = list(
        mod.checks(
            context_for_test(),
            store=store,
            post=post or _api(store),
            get=get or _api_get(),
            provisioner_factory=factory,
            simulations=lambda _ctx: iter(()),
        )
    )
    return results, {r.check: r for r in results}, made


_MATRIX = load_capability_matrix()
_CELLS = [(family, tier) for family in sorted(_MATRIX.tiers) for tier in TIERS]
_SERVED_DEFAULTS_ROWS = [
    "capabilities_default_family",
    *(f"capabilities_tier_model:{family}:{tier}" for family, tier in _CELLS),
    *(f"capabilities_tier_effort:{family}:{tier}" for family, tier in _CELLS),
    *(f"capabilities_assignable_tiers:{model}" for model in sorted(_MATRIX.models)),
    *(f"capabilities_selectable:{model}" for model in sorted(_MATRIX.models)),
]
_SERVED_DEFAULTS_PREFIXES = (
    "capabilities_default_family",
    "capabilities_tier_model:",
    "capabilities_tier_effort:",
    "capabilities_assignable_tiers:",
    "capabilities_selectable:",
)


def _other_family(family: str) -> str:
    return next(other for other in sorted(_MATRIX.tiers) if other != family)


_FLOW = [
    "unauthenticated_request_denied",
    "provision_a",
    "provision_b",
    "defaults_before_save",
    "own_settings_saved_and_read_back",
    "other_user_sees_defaults",
    "rep_id_argument_rejected",
    "rejected_put_wrote_nothing_for_b",
    "unsupported_value_rejected",
    "temperature_rejected",
    "retired_model_rejected",
    "capabilities_payload_current",
    *(f"capabilities_supervisor_effort:{family}" for family in sorted(_MATRIX.tiers)),
    *_SERVED_DEFAULTS_ROWS,
    "item_keyed_by_caller",
    "reset_restores_defaults",
    "settings_item_cleaned_a",
    "settings_item_cleaned_b",
    "cognito_user_deleted",
    "cognito_user_deleted",
    "tenant_torn_down",
]


def test_the_real_handler_passes_every_check(store: Store, _tenant_lifecycle: list[str]) -> None:
    results, _, made = _run(store)
    assert [r.check for r in results] == _FLOW
    assert {r.check: r.detail for r in results if r.status != "pass"} == {}
    assert _tenant_lifecycle == ["alloc", "seed", "teardown"]
    assert all(p.cleaned for p in made)
    assert store.get_item("reps", {"rep_id": user_settings_key(_TENANT.tenant, "rep-a")}) is None


def test_a_handler_that_honours_rep_id_reds_the_own_only_rows(store: Store) -> None:
    def honour_rep_id(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            body = json.loads(event["body"])
            target = body.pop("rep_id", None)
            if target is not None:
                event["requestContext"]["authorizer"]["claims"][CLAIM_REP_ID] = target
                event["body"] = json.dumps(body)
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=honour_rep_id))
    assert by_check["rep_id_argument_rejected"].status == "fail"


def test_a_handler_that_shares_one_item_reds_the_other_user_row(store: Store) -> None:
    def shared(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            if "authorizer" in event["requestContext"]:
                event["requestContext"]["authorizer"]["claims"][CLAIM_REP_ID] = "rep-a"
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=shared))
    assert by_check["other_user_sees_defaults"].status == "fail"


def test_a_handler_that_saves_unvalidated_values_reds_the_unsupported_row(store: Store) -> None:
    def lax(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            body = json.loads(event["body"])
            tiers = (body.get("tuning") or {}).get("tiers") or {}
            if tiers.get("supervisor", {}).get("effort") == mod.UNSUPPORTED_EFFORT:
                return {"statusCode": 200, "body": json.dumps({"is_default": False})}
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=lax))
    assert by_check["unsupported_value_rejected"].status == "fail"


def test_a_rejection_that_still_writes_reds_the_unsupported_row(store: Store) -> None:
    def write_then_reject(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            body = json.loads(event["body"])
            tiers = (body.get("tuning") or {}).get("tiers") or {}
            if tiers.get("supervisor", {}).get("effort") == mod.UNSUPPORTED_EFFORT:
                tiers["supervisor"]["effort"] = "high"
                event["body"] = json.dumps(body)
                real(event)
                error = {"code": "validation_failed", "source": "tuning.tiers.supervisor.effort"}
                return {"statusCode": 400, "body": json.dumps({"error": error})}
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=write_then_reject))
    row = by_check["unsupported_value_rejected"]
    assert row.status == "fail" and "unchanged=False" in row.detail


def test_an_unauthenticated_success_reds_the_first_row(store: Store) -> None:
    def open_route(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            if "authorizer" not in event["requestContext"]:
                return {"statusCode": 200, "body": json.dumps({"is_default": True})}
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=open_route))
    assert by_check["unauthenticated_request_denied"].status == "fail"


def test_reset_that_leaves_the_item_reds_the_reset_row(store: Store) -> None:
    def no_delete(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            if json.loads(event["body"]).get("action") == "reset":
                return {"statusCode": 200, "body": json.dumps({"is_default": True})}
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=no_delete))
    assert by_check["reset_restores_defaults"].status == "fail"
    assert by_check["settings_item_cleaned_a"].status == "pass"


def test_a_raising_simulation_still_runs_the_api_rows(store: Store) -> None:
    def boom(_ctx: Any) -> Iterator[CheckResult]:
        yield from ()
        raise RuntimeError("no lambda")

    results = list(
        mod.checks(
            context_for_test(),
            store=store,
            post=_api(store),
            get=_api_get(),
            provisioner_factory=lambda _t: FakeProvisioner("rep-a"),
            simulations=boom,
        )
    )
    by_check = {r.check: r for r in results}
    assert by_check["role_simulations"].status == "fail"
    assert "tenant_torn_down" in by_check


def _tiers_of(event: dict[str, Any]) -> dict[str, Any]:
    tuning = json.loads(event["body"]).get("tuning") or {}
    return dict(tuning.get("tiers") or {})


def test_a_handler_that_drops_temperature_and_saves_reds_the_temperature_row(store: Store) -> None:
    def lenient(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            body = json.loads(event["body"])
            for request in ((body.get("tuning") or {}).get("tiers") or {}).values():
                request.pop(mod.REFUSED_FIELD, None)
            event["body"] = json.dumps(body)
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=lenient))
    assert by_check["temperature_rejected"].status == "fail"
    assert by_check["unsupported_value_rejected"].status == "pass"


def test_a_temperature_refusal_on_the_wrong_source_reds_the_temperature_row(store: Store) -> None:
    def schema_source(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            if mod.REFUSED_FIELD in _tiers_of(event).get("supervisor", {}):
                error = {"code": "validation_failed", "source": "tuning.schema"}
                return {"statusCode": 400, "body": json.dumps({"error": error})}
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=schema_source))
    assert by_check["temperature_rejected"].status == "fail"


def test_a_handler_that_saves_a_retired_model_reds_the_retired_row(store: Store) -> None:
    def accept_retired(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            if _tiers_of(event).get("tool", {}).get("model") in mod.RETIRED_MODEL_IDS:
                return {"statusCode": 200, "body": json.dumps({"is_default": False})}
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=accept_retired))
    assert by_check["retired_model_rejected"].status == "fail"


def test_the_retired_ids_are_the_retired_profiles_models() -> None:
    assert mod.RETIRED_MODEL_IDS
    assert not set(mod.RETIRED_MODEL_IDS) & set(load_capability_matrix().models)


@pytest.mark.parametrize(
    ("mutate", "what"),
    [
        (lambda body: {**body, "temperature": {"supported": True}}, "temperature key"),
        (lambda body: {**body, "legacy": [mod.RETIRED_MODEL_IDS[0]]}, "retired id"),
        (lambda _body: {}, "no current models"),
    ],
    ids=["temperature", "retired", "empty"],
)
def test_a_stale_capabilities_payload_reds_the_capabilities_row(
    store: Store, mutate: Callable[[dict[str, Any]], dict[str, Any]], what: str
) -> None:
    def stale(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            response = real(event)
            body = mutate(json.loads(response["body"]))
            return {**response, "body": json.dumps(body)}

        return handler

    _, by_check, _ = _run(store, get=_api_get(wrap=stale))
    assert by_check["capabilities_payload_current"].status == "fail", what


def _served(mutate: Callable[[dict[str, Any]], object]) -> Callable[[Handler], Handler]:
    def wrap(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            response = real(event)
            body = json.loads(response["body"])
            mutate(body)
            return {**response, "body": json.dumps(body)}

        return handler

    return wrap


def test_the_configured_supervisor_effort_is_medium_for_both_families() -> None:
    tiers = load_capability_matrix().tiers
    assert sorted(tiers) == ["anthropic", "openai"]
    assert {family: tiers[family]["supervisor"].tuning["effort"] for family in tiers} == {
        "anthropic": "medium",
        "openai": "medium",
    }


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_a_deployed_supervisor_effort_of_low_reds_only_that_familys_row(
    store: Store, family: str
) -> None:
    def to_low(body: dict[str, Any]) -> None:
        body["tiers"][family]["supervisor"]["tuning"]["effort"] = "low"

    _, by_check, _ = _run(store, get=_api_get(wrap=_served(to_low)))
    effort_rows = {c: r for c, r in by_check.items() if c.startswith("capabilities_supervisor")}
    failed = [c for c, r in effort_rows.items() if r.status == "fail"]
    assert failed == [f"capabilities_supervisor_effort:{family}"]
    assert "effort='low'" in effort_rows[failed[0]].detail


@pytest.mark.parametrize(
    ("mutate", "what"),
    [
        (lambda body: body["tiers"]["openai"]["supervisor"]["tuning"].pop("effort"), "effort"),
        (lambda body: body["tiers"]["openai"]["supervisor"].pop("tuning"), "tuning"),
        (lambda body: body["tiers"].pop("openai"), "family"),
        (lambda body: body.pop("tiers"), "tiers"),
    ],
    ids=["no-effort", "no-tuning", "no-family", "no-tiers"],
)
def test_an_absent_served_supervisor_effort_fails_rather_than_skips(
    store: Store, mutate: Callable[[dict[str, Any]], object], what: str
) -> None:
    _, by_check, _ = _run(store, get=_api_get(wrap=_served(mutate)))
    row = by_check["capabilities_supervisor_effort:openai"]
    assert row.status == "fail", what
    assert "effort=absent" in row.detail


def test_a_non_200_capabilities_answer_reds_the_effort_rows(store: Store) -> None:
    def unavailable(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            response = real(event)
            return {**response, "statusCode": 500}

        return handler

    _, by_check, _ = _run(store, get=_api_get(wrap=unavailable))
    for family in ("anthropic", "openai"):
        assert by_check[f"capabilities_supervisor_effort:{family}"].status == "fail"


def _failed_served_defaults_rows(by_check: Mapping[str, CheckResult]) -> list[str]:
    return [
        check
        for check, row in by_check.items()
        if check.startswith(_SERVED_DEFAULTS_PREFIXES) and row.status == "fail"
    ]


def test_served_defaults_rows_cover_every_cell_and_every_configured_model() -> None:
    tier_models = {a.model_id for tiers in _MATRIX.tiers.values() for a in tiers.values()}
    selectable = {c.split(":", 1)[1] for c in _SERVED_DEFAULTS_ROWS if "selectable:" in c}
    assert selectable == set(_MATRIX.models)
    assert selectable > tier_models


def test_a_served_default_family_other_than_config_reds_only_the_default_family_row(
    store: Store,
) -> None:
    def flip(body: dict[str, Any]) -> None:
        body["default_family"] = _other_family(body["default_family"])

    _, by_check, _ = _run(store, get=_api_get(wrap=_served(flip)))
    assert _failed_served_defaults_rows(by_check) == ["capabilities_default_family"]


@pytest.mark.parametrize(("family", "tier"), _CELLS, ids=[f"{f}-{t}" for f, t in _CELLS])
def test_a_served_tier_model_other_than_config_reds_only_that_cells_model_row(
    store: Store, family: Family, tier: Tier
) -> None:
    other = next(m for m in sorted(_MATRIX.models) if m != _MATRIX.tiers[family][tier].model_id)

    def swap(body: dict[str, Any]) -> None:
        body["tiers"][family][tier]["model_id"] = other

    _, by_check, _ = _run(store, get=_api_get(wrap=_served(swap)))
    assert _failed_served_defaults_rows(by_check) == [f"capabilities_tier_model:{family}:{tier}"]


@pytest.mark.parametrize(("family", "tier"), _CELLS, ids=[f"{f}-{t}" for f, t in _CELLS])
def test_a_served_tier_effort_other_than_config_reds_only_that_cells_effort_row(
    store: Store, family: str, tier: str
) -> None:
    def change(body: dict[str, Any]) -> None:
        body["tiers"][family][tier]["tuning"]["effort"] = mod.UNSUPPORTED_EFFORT

    _, by_check, _ = _run(store, get=_api_get(wrap=_served(change)))
    assert _failed_served_defaults_rows(by_check) == [f"capabilities_tier_effort:{family}:{tier}"]


@pytest.mark.parametrize(("family", "tier"), _CELLS[:1], ids=["first-cell"])
def test_an_absent_served_tier_effort_fails_rather_than_skips(
    store: Store, family: str, tier: str
) -> None:
    def drop(body: dict[str, Any]) -> None:
        body["tiers"][family][tier]["tuning"].pop("effort")

    _, by_check, _ = _run(store, get=_api_get(wrap=_served(drop)))
    row = by_check[f"capabilities_tier_effort:{family}:{tier}"]
    assert row.status == "fail" and "effort=absent" in row.detail


@pytest.mark.parametrize("model", sorted(_MATRIX.models))
def test_served_assignable_tiers_other_than_config_red_only_that_models_row(
    store: Store, model: str
) -> None:
    def truncate(body: dict[str, Any]) -> None:
        body["models"][model]["assignable_tiers"] = []

    _, by_check, _ = _run(store, get=_api_get(wrap=_served(truncate)))
    assert _failed_served_defaults_rows(by_check) == [f"capabilities_assignable_tiers:{model}"]


@pytest.mark.parametrize("model", sorted(_MATRIX.models))
def test_a_model_served_under_another_family_reds_only_that_models_selectable_row(
    store: Store, model: str
) -> None:
    def refamily(body: dict[str, Any]) -> None:
        body["models"][model]["family"] = _other_family(body["models"][model]["family"])

    _, by_check, _ = _run(store, get=_api_get(wrap=_served(refamily)))
    assert _failed_served_defaults_rows(by_check) == [f"capabilities_selectable:{model}"]


@pytest.mark.parametrize("model", sorted(_MATRIX.models))
def test_a_model_missing_from_the_served_payload_is_not_selectable(
    store: Store, model: str
) -> None:
    def drop(body: dict[str, Any]) -> None:
        body["models"].pop(model)

    _, by_check, _ = _run(store, get=_api_get(wrap=_served(drop)))
    assert by_check[f"capabilities_selectable:{model}"].status == "fail"


def test_a_non_200_capabilities_answer_reds_every_served_defaults_row(store: Store) -> None:
    def unavailable(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            return {**real(event), "statusCode": 500}

        return handler

    _, by_check, _ = _run(store, get=_api_get(wrap=unavailable))
    assert sorted(_failed_served_defaults_rows(by_check)) == sorted(_SERVED_DEFAULTS_ROWS)


def _defaults_on_other_family(real: Handler) -> Handler:
    def handler(event: dict[str, Any]) -> dict[str, Any]:
        response = real(event)
        body = json.loads(response["body"])
        if body.get("is_default") is True:
            for request in body["tuning"]["tiers"].values():
                request["family"] = _other_family(_MATRIX.default_family)
        return {**response, "body": json.dumps(body)}

    return handler


def test_defaults_served_on_another_family_red_the_defaults_and_reset_rows(store: Store) -> None:
    _, by_check, _ = _run(store, post=_api(store, wrap=_defaults_on_other_family))
    for check in ("defaults_before_save", "reset_restores_defaults"):
        assert by_check[check].status == "fail", check
        assert repr(_MATRIX.default_family) in by_check[check].detail
    assert by_check["own_settings_saved_and_read_back"].status == "pass"


# -- role simulations ----------------------------------------------------------------------------


def _rows() -> tuple[SimulationRow, ...]:
    return mod.simulation_rows(role_arn="role", reps_arn="arn:reps", other_table_arn="arn:accts")


def test_simulation_rows_cover_the_design_9_user_settings_row() -> None:
    rows = {r.check: r for r in _rows()}
    allowed = {c for c, r in rows.items() if r.allowed}
    assert allowed == {
        "role_allows_GetItem_usersettings",
        "role_allows_PutItem_usersettings",
        "role_allows_DeleteItem_usersettings",
    }
    for check in allowed:
        keys = rows[check].leading_keys
        assert keys is not None
        assert all(key.startswith("usersettings#") for key in keys)
    assert rows["role_denies_conversation_prefix"].leading_keys == ("conversation#smoke",)
    assert rows["role_denies_other_table"].resource_arn == "arn:accts"


class FakeIam:
    """`reps` Get/Put/Delete allowed only when every leading key is a `usersettings#` key."""

    def __init__(self, *, any_prefix: bool = False) -> None:
        self.any_prefix = any_prefix

    def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
        (action,) = request["ActionNames"]
        (resource,) = request["ResourceArns"]
        keys = request.get("ContextEntries", [{}])[0].get("ContextKeyValues", [])
        allowed = (
            resource.endswith("table/crm-agent-eval-reps")
            and action in ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem")
            and (self.any_prefix or all(k.startswith("usersettings#") for k in keys))
        )
        decision = "allowed" if allowed else "implicitDeny"
        missing = [] if "ContextEntries" in request else ["dynamodb:LeadingKeys"]
        return {"EvaluationResults": [{"EvalDecision": decision, "MissingContextValues": missing}]}


class FakeLambda:
    def __init__(self) -> None:
        self.names: list[str] = []

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:  # noqa: N803
        self.names.append(FunctionName)
        return {"Role": "arn:aws:iam::123456789012:role/crm-agent-eval-user-settings-role"}


class FakeSts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "123456789012"}


def _simulate(iam: FakeIam) -> tuple[dict[str, CheckResult], FakeLambda]:
    lambda_client = FakeLambda()
    ctx = context_for_test(
        outputs={
            "eval_trigger_function_name": "crm-agent-eval-eval-trigger",
            "dynamodb_table_names": {
                "reps": "crm-agent-eval-reps",
                "accounts": "crm-agent-eval-accounts",
            },
        },
        clients={"lambda": lambda_client, "sts": FakeSts(), "iam": iam},
    )
    return {r.check: r for r in mod.role_simulations(ctx)}, lambda_client


def test_role_simulations_pass_against_the_designed_grant() -> None:
    rows, lambda_client = _simulate(FakeIam())
    assert lambda_client.names == ["crm-agent-eval-user-settings"]
    assert len(rows) == len(_rows())
    assert {c: r.detail for c, r in rows.items() if r.status != "pass"} == {}


def test_a_grant_without_the_key_prefix_reds_exactly_the_prefix_rows() -> None:
    rows, _ = _simulate(FakeIam(any_prefix=True))
    assert {c for c, r in rows.items() if r.status != "pass"} == {
        "role_denies_conversation_prefix",
        "role_denies_rep_item",
    }


def test_expected_check_count_is_derived_from_the_rows() -> None:
    assert mod.expected_check_count(context_for_test()).minimum == len(_rows()) + 2


def test_an_item_saved_under_b_despite_the_rejection_is_removed_in_cleanup(store: Store) -> None:
    def save_under_caller(real: Handler) -> Handler:
        def handler(event: dict[str, Any]) -> dict[str, Any]:
            body = json.loads(event["body"])
            if body.pop("rep_id", None) is not None:
                event["body"] = json.dumps(body)
                real(event)
                error = {"code": "validation_failed", "source": "arguments"}
                return {"statusCode": 400, "body": json.dumps({"error": error})}
            return real(event)

        return handler

    _, by_check, _ = _run(store, post=_api(store, wrap=save_under_caller))
    assert by_check["rejected_put_wrote_nothing_for_b"].status == "fail"
    assert by_check["settings_item_cleaned_b"].status == "pass"
    assert store.get_item("reps", {"rep_id": user_settings_key(_TENANT.tenant, "rep-b")}) is None


def test_the_other_table_row_sends_leading_keys() -> None:
    row = {r.check: r for r in _rows()}["role_denies_other_table"]
    assert row.leading_keys is not None
