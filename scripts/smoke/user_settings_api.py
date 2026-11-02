# SPDX-License-Identifier: MIT
"""Layer 5 T3 smoke: `POST /api/user_settings` keeps each user's tuning their own.

Allocates and seeds one disposable tenant, then provisions two MFA-enrolled users A and B under it.
Each call uses that user's own ID token through CloudFront:

1. No token -> 401/403.
2. A `get` before any save -> `is_default: true`, every tier on `config/models.yaml`'s
   `default_family`.
3. A `put` of a valid current-schema block (the `config/models.yaml` defaults) ->
   `is_default: false`, and A's `get` returns those tiers.
4. B's `get` -> `is_default: true` (A's save is invisible to B in the same tenant).
5. B's `put` carrying `rep_id: <A>` -> 400 `validation_failed`, and neither item changes.
6. A's `put` with an unsupported supervisor effort -> 400 `validation_failed` with a
   `tuning.tiers.supervisor.*` source, and A's saved item is unchanged.
6a. A's `put` whose supervisor tier carries a `temperature` -> 400 `validation_failed` with source
   `tuning.tiers.supervisor.temperature`, and A's saved item is unchanged.
6b. A's `put` naming a retired model on its tool tier -> 400 `validation_failed` with a
   `tuning.tiers.tool.*` source, and A's saved item is unchanged.
6c. A's `GET /api/capabilities` -> 200 naming every `config/models.yaml` model, with no `temperature`
   key and no retired model id anywhere in the payload.
6d. The same payload's `tiers.<family>.supervisor.tuning.effort` equals `config/models.yaml`'s, one
   `capabilities_supervisor_effort:<family>` row per configured family.
6e. The served defaults match the config: `capabilities_default_family`, one
   `capabilities_tier_model:<family>:<tier>` and `capabilities_tier_effort:<family>:<tier>` row per
   configured cell, and per `config/model_capabilities.yaml` model one
   `capabilities_assignable_tiers:<model>` and one `capabilities_selectable:<model>` row.
7. The saved item sits at `keys.user_settings_key(tenant, A)` and names A as its owner.
8. A's `reset` -> defaults on the default family, and the item is gone.
9. Role simulations for the `user-settings` function's role: `reps`
   Get/Put/Delete allowed under `usersettings#…`; denied under `conversation#…`, on a rep item, on
   `reps` Query and on another table.

Cleanup (both users, their `reps` rows, A's settings item, the tenant) runs in a `finally`.
"""

from __future__ import annotations

import copy
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.admin_authz import (
    ApiPost,
    Provisioner,
    RoleSmokeUserProvisioner,
    SimulationRow,
    error_fields,
    function_name_prefix,
    live_api_post,
    live_store,
    load_json_object,
    provision_or_fail,
    role_arn_of_function,
    simulation_result,
    summarise,
    unauthenticated_check,
)
from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    open_https,
    run_main,
)
from scripts.smoke.runtime_bedrock_iam import RETIRED_INFERENCE_PROFILES
from scripts.smoke.tenant import (
    SmokeTenant,
    allocate_smoke_tenant,
    seed_smoke_tenant,
    teardown_smoke_tenant,
)

from crm.store.keys import user_settings_key
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import TIERS, defaults, to_wire

LAYER: Final[int] = 5
RESOURCE_CLASS: Final[str] = "user_settings_api"

ROUTE: Final[str] = "user_settings"
CAPABILITIES_ROUTE: Final[str] = "capabilities"
SETTINGS_TABLE: Final[str] = "reps"
UNSUPPORTED_EFFORT: Final[str] = "smoke-unsupported-effort"
REFUSED_FIELD: Final[str] = "temperature"
RETIRED_MODEL_IDS: Final[tuple[str, ...]] = tuple(
    profile.removeprefix("global.") for profile in RETIRED_INFERENCE_PROFILES
)
_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0

#: `(route, token) -> (status, body)`: an authenticated, bodyless `GET /api/<route>`.
ApiGet = Callable[[str, str | None], tuple[int, dict[str, Any]]]


def live_api_get(ctx: SmokeContext) -> ApiGet:
    """Build the live :data:`ApiGet`: `GET https://<cloudfront_domain_name>/api/<route>`."""
    domain = ctx.outputs.require_str("cloudfront_domain_name")

    def get(route: str, token: str | None) -> tuple[int, dict[str, Any]]:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        request = urllib.request.Request(  # noqa: S310 - fixed https CloudFront host
            f"https://{domain}/api/{route}", method="GET", headers=headers
        )
        try:
            with open_https(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                return int(response.status), load_json_object(response.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), load_json_object(exc.read())

    return get


def valid_tuning() -> dict[str, Any]:
    """A current-schema block every deployed matrix accepts: the `config/models.yaml` defaults."""
    return to_wire(defaults(load_capability_matrix()))


def unsupported_tuning(base: Mapping[str, Any]) -> dict[str, Any]:
    """`base` with the supervisor's effort replaced by a value no model supports."""
    tuning = copy.deepcopy(dict(base))
    tuning["tiers"]["supervisor"]["effort"] = UNSUPPORTED_EFFORT
    return tuning


def temperature_tuning(base: Mapping[str, Any]) -> dict[str, Any]:
    """`base` with a `temperature` added to the supervisor's request."""
    tuning = copy.deepcopy(dict(base))
    tuning["tiers"]["supervisor"][REFUSED_FIELD] = 0.5
    return tuning


def retired_model_tuning(base: Mapping[str, Any]) -> dict[str, Any]:
    """`base` with the tool tier naming the first retired model."""
    tuning = copy.deepcopy(dict(base))
    tuning["tiers"]["tool"]["model"] = RETIRED_MODEL_IDS[0]
    return tuning


def _keys_and_strings(value: object) -> Iterator[str]:
    """Every mapping key and string value in a decoded JSON document."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _keys_and_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys_and_strings(item)
    elif isinstance(value, str):
        yield value


def _tiers(payload: Mapping[str, Any]) -> object:
    tuning = payload.get("tuning")
    return tuning.get("tiers") if isinstance(tuning, Mapping) else None


def _at(value: object, *path: str) -> object:
    """The value at `path` in nested mappings, or ``None`` where any step is absent."""
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _off_default_family(payload: Mapping[str, Any]) -> list[str]:
    """The tiers whose wire `family` is not `config/models.yaml`'s `default_family`."""
    expected = load_capability_matrix().default_family
    return [tier for tier in TIERS if _at(_tiers(payload), tier, "family") != expected]


def _get(post: ApiPost, token: str) -> tuple[int, dict[str, Any]]:
    return post(ROUTE, token, {"action": "get"})


def _row(check: str, ok: bool, detail_ok: str, detail_fail: str) -> CheckResult:
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if ok else "fail",
        detail=detail_ok if ok else detail_fail,
    )


def check_default_view(post: ApiPost, token: str, *, check: str, who: str) -> CheckResult:
    """`get` answers 200 with `is_default: true` and every tier on the default family."""
    status, payload = _get(post, token)
    off_family = _off_default_family(payload)
    ok = status == 200 and payload.get("is_default") is True and not off_family
    family = load_capability_matrix().default_family
    return _row(
        check,
        ok,
        f"{who} get -> 200 is_default=true; every tier family={family!r}",
        f"{who} get -> HTTP {status} {summarise(payload)}; expected 200 is_default=true with "
        f"every tier family={family!r}, off-family tiers={off_family}",
    )


def check_put_and_read_back(post: ApiPost, token: str, tuning: Mapping[str, Any]) -> CheckResult:
    """A's `put` is saved and a fresh `get` returns the same tiers, not the defaults view."""
    put_status, put_payload = post(ROUTE, token, {"action": "put", "tuning": dict(tuning)})
    get_status, get_payload = _get(post, token)
    ok = (
        put_status == 200
        and put_payload.get("is_default") is False
        and get_status == 200
        and get_payload.get("is_default") is False
        and _tiers(get_payload) == tuning["tiers"]
    )
    return _row(
        "own_settings_saved_and_read_back",
        ok,
        "A put -> 200 is_default=false; A get returns the saved tiers",
        f"A put -> HTTP {put_status} {summarise(put_payload, 160)}; A get -> HTTP {get_status} "
        f"{summarise(get_payload, 160)}",
    )


def check_rep_id_rejected(
    post: ApiPost, token: str, *, other_rep_id: str, tuning: Mapping[str, Any]
) -> CheckResult:
    """A `put` naming another rep is refused 400 `validation_failed` before anything is written."""
    status, payload = post(
        ROUTE, token, {"action": "put", "tuning": dict(tuning), "rep_id": other_rep_id}
    )
    error = error_fields(payload)
    ok = status == 400 and error.get("code") == "validation_failed"
    return _row(
        "rep_id_argument_rejected",
        ok,
        "B put carrying rep_id=<A> -> 400 validation_failed",
        f"B put carrying rep_id=<A> -> HTTP {status} {summarise(payload)}",
    )


def check_unsupported_value_rejected(
    post: ApiPost, token: str, *, saved: Mapping[str, Any]
) -> CheckResult:
    """An unsupported effort is refused 400 at its tier's source path and the saved item stays."""
    status, payload = post(ROUTE, token, {"action": "put", "tuning": unsupported_tuning(saved)})
    error = error_fields(payload)
    source = str(error.get("source") or "")
    get_status, get_payload = _get(post, token)
    unchanged = get_status == 200 and _tiers(get_payload) == saved["tiers"]
    ok = (
        status == 400
        and error.get("code") == "validation_failed"
        and source.startswith("tuning.tiers.supervisor")
        and unchanged
    )
    return _row(
        "unsupported_value_rejected",
        ok,
        f"A put with effort={UNSUPPORTED_EFFORT!r} -> 400 validation_failed source={source!r}; "
        "saved item unchanged",
        f"A put with an unsupported effort -> HTTP {status} {summarise(payload, 160)}; saved item "
        f"unchanged={unchanged}",
    )


def _refused_unchanged(
    post: ApiPost,
    token: str,
    *,
    saved: Mapping[str, Any],
    tuning: Mapping[str, Any],
    check: str,
    what: str,
    source_ok: Callable[[str], bool],
) -> CheckResult:
    """A `put` of `tuning` is refused 400 `validation_failed` at a matching source; item unchanged."""
    status, payload = post(ROUTE, token, {"action": "put", "tuning": dict(tuning)})
    error = error_fields(payload)
    source = str(error.get("source") or "")
    get_status, get_payload = _get(post, token)
    unchanged = get_status == 200 and _tiers(get_payload) == saved["tiers"]
    ok = status == 400 and error.get("code") == "validation_failed" and source_ok(source)
    return _row(
        check,
        ok and unchanged,
        f"A put {what} -> 400 validation_failed source={source!r}; saved item unchanged",
        f"A put {what} -> HTTP {status} {summarise(payload, 160)}; saved item unchanged={unchanged}",
    )


def check_temperature_rejected(
    post: ApiPost, token: str, *, saved: Mapping[str, Any]
) -> CheckResult:
    """A tuning carrying a `temperature` is refused at that field's source."""
    return _refused_unchanged(
        post,
        token,
        saved=saved,
        tuning=temperature_tuning(saved),
        check="temperature_rejected",
        what=f"with a supervisor {REFUSED_FIELD}",
        source_ok=lambda source: source == f"tuning.tiers.supervisor.{REFUSED_FIELD}",
    )


def check_retired_model_rejected(
    post: ApiPost, token: str, *, saved: Mapping[str, Any]
) -> CheckResult:
    """A tuning naming a retired model is refused at the tool tier's source."""
    return _refused_unchanged(
        post,
        token,
        saved=saved,
        tuning=retired_model_tuning(saved),
        check="retired_model_rejected",
        what=f"naming retired model {RETIRED_MODEL_IDS[0]!r} on the tool tier",
        source_ok=lambda source: source.startswith("tuning.tiers.tool"),
    )


def check_capabilities_current(get: ApiGet, token: str) -> CheckResult:
    """`GET /api/capabilities` names the current models and no retired id or temperature."""
    status, payload = get(CAPABILITIES_ROUTE, token)
    tokens = set(_keys_and_strings(payload))
    current = {
        assignment.model_id
        for tiers in load_capability_matrix().tiers.values()
        for assignment in tiers.values()
    }
    missing = sorted(current - tokens)
    retired = sorted(set(RETIRED_MODEL_IDS) & tokens)
    temperature = REFUSED_FIELD in {token.lower() for token in tokens}
    ok = status == 200 and not missing and not retired and not temperature
    return _row(
        "capabilities_payload_current",
        ok,
        f"GET /api/{CAPABILITIES_ROUTE} -> 200 naming all {len(current)} configured models; no "
        f"retired id, no {REFUSED_FIELD!r} key",
        f"GET /api/{CAPABILITIES_ROUTE} -> HTTP {status}; missing={missing} retired={retired} "
        f"{REFUSED_FIELD}={temperature} {summarise(payload, 120)}",
    )


def check_capabilities_supervisor_effort(get: ApiGet, token: str) -> Iterator[CheckResult]:
    """Per family, the served supervisor `tuning.effort` equals `config/models.yaml`'s."""
    status, payload = get(CAPABILITIES_ROUTE, token)
    served_tiers = payload.get("tiers")
    for family, tiers in sorted(load_capability_matrix().tiers.items()):
        expected = tiers["supervisor"].tuning.get("effort")
        served: object = None
        family_tiers = served_tiers.get(family) if isinstance(served_tiers, Mapping) else None
        if isinstance(family_tiers, Mapping):
            supervisor = family_tiers.get("supervisor")
            tuning = supervisor.get("tuning") if isinstance(supervisor, Mapping) else None
            served = tuning.get("effort") if isinstance(tuning, Mapping) else None
        recorded = "absent" if served is None else repr(served)
        yield _row(
            f"capabilities_supervisor_effort:{family}",
            status == 200 and served is not None and served == expected,
            f"GET /api/{CAPABILITIES_ROUTE} tiers.{family}.supervisor.tuning.effort={recorded}, "
            f"config/models.yaml {expected!r}",
            f"GET /api/{CAPABILITIES_ROUTE} -> HTTP {status}; tiers.{family}.supervisor.tuning.effort"
            f"={recorded}, expected {expected!r} (config/models.yaml)",
        )


def _served_row(
    check: str, ok: bool, *, status: int, where: str, served: object, expected: object
) -> CheckResult:
    recorded = "absent" if served is None else repr(served)
    return _row(
        check,
        status == 200 and ok,
        f"GET /api/{CAPABILITIES_ROUTE} {where}={recorded}, config {expected!r}",
        f"GET /api/{CAPABILITIES_ROUTE} -> HTTP {status}; {where}={recorded}, expected "
        f"{expected!r} (config)",
    )


def check_capabilities_served_defaults(get: ApiGet, token: str) -> Iterator[CheckResult]:
    """The served `default_family`, every tier cell, and every model's tiers and family match config.

    Yields `capabilities_default_family`, then `capabilities_tier_model:<family>:<tier>` and
    `capabilities_tier_effort:<family>:<tier>` per configured cell, then
    `capabilities_assignable_tiers:<model>` and `capabilities_selectable:<model>` per configured
    model. A model is selectable when the payload serves it under its configured family.
    """
    status, payload = get(CAPABILITIES_ROUTE, token)
    matrix = load_capability_matrix()
    served_family = payload.get("default_family")
    yield _served_row(
        "capabilities_default_family",
        served_family == matrix.default_family,
        status=status,
        where="default_family",
        served=served_family,
        expected=matrix.default_family,
    )
    cells = [(family, tier) for family in sorted(matrix.tiers) for tier in TIERS]
    for family, tier in cells:
        expected_model = matrix.tiers[family][tier].model_id
        served_model = _at(payload, "tiers", family, tier, "model_id")
        yield _served_row(
            f"capabilities_tier_model:{family}:{tier}",
            served_model == expected_model,
            status=status,
            where=f"tiers.{family}.{tier}.model_id",
            served=served_model,
            expected=expected_model,
        )
    for family, tier in cells:
        expected_effort = matrix.tiers[family][tier].tuning.get("effort")
        served_effort = _at(payload, "tiers", family, tier, "tuning", "effort")
        yield _served_row(
            f"capabilities_tier_effort:{family}:{tier}",
            served_effort is not None and served_effort == expected_effort,
            status=status,
            where=f"tiers.{family}.{tier}.tuning.effort",
            served=served_effort,
            expected=expected_effort,
        )
    for model_id, model in sorted(matrix.models.items()):
        expected_tiers = list(model.assignable_tiers)
        served_tiers = _at(payload, "models", model_id, "assignable_tiers")
        yield _served_row(
            f"capabilities_assignable_tiers:{model_id}",
            served_tiers == expected_tiers,
            status=status,
            where=f"models.{model_id}.assignable_tiers",
            served=served_tiers,
            expected=expected_tiers,
        )
    for model_id, model in sorted(matrix.models.items()):
        served_model_family = _at(payload, "models", model_id, "family")
        served_id = _at(payload, "models", model_id, "model_id")
        yield _served_row(
            f"capabilities_selectable:{model_id}",
            served_id == model_id and served_model_family == model.family,
            status=status,
            where=f"models.{model_id}.family",
            served=served_model_family,
            expected=model.family,
        )


def check_item_key(store: Any, *, tenant: str, rep_id: str) -> CheckResult:
    """The saved item is keyed by the caller's tenant and rep id and names its owner."""
    key = user_settings_key(tenant, rep_id)
    item = store.get_item(SETTINGS_TABLE, {"rep_id": key})
    ok = item is not None and item.get("owner_rep_id") == rep_id
    return _row(
        "item_keyed_by_caller",
        ok,
        "reps item usersettings#<tenant>#<A> present with owner_rep_id=A",
        f"reps item {key!r}: {'absent' if item is None else 'owner ' + repr(item.get('owner_rep_id'))}",
    )


def check_reset(post: ApiPost, token: str, store: Any, *, tenant: str, rep_id: str) -> CheckResult:
    """`reset` answers with the defaults on the default family, a `get` agrees, the item is gone."""
    status, payload = post(ROUTE, token, {"action": "reset"})
    get_status, get_payload = _get(post, token)
    remaining = store.get_item(SETTINGS_TABLE, {"rep_id": user_settings_key(tenant, rep_id)})
    off_family = sorted(set(_off_default_family(payload)) | set(_off_default_family(get_payload)))
    ok = (
        status == 200
        and payload.get("is_default") is True
        and get_status == 200
        and get_payload.get("is_default") is True
        and not off_family
        and remaining is None
    )
    family = load_capability_matrix().default_family
    return _row(
        "reset_restores_defaults",
        ok,
        f"A reset -> 200 is_default=true, every tier family={family!r}; get agrees; item deleted",
        f"A reset -> HTTP {status} {summarise(payload, 120)}; get -> HTTP {get_status} "
        f"is_default={get_payload.get('is_default')!r}; off-family tiers={off_family} "
        f"(expected {family!r}); item present={remaining is not None}",
    )


def simulation_rows(
    *, role_arn: str, reps_arn: str, other_table_arn: str
) -> tuple[SimulationRow, ...]:
    """`user_settings` role row: `reps` under `usersettings#*` only, nothing else."""
    settings_key = ("usersettings#smoke-tenant#smoke-rep",)
    rows = [
        SimulationRow(
            check=f"role_allows_{action.split(':')[1]}_usersettings",
            role_arn=role_arn,
            action=action,
            resource_arn=reps_arn,
            allowed=True,
            leading_keys=settings_key,
        )
        for action in ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem")
    ]
    rows += [
        SimulationRow(
            check="role_denies_conversation_prefix",
            role_arn=role_arn,
            action="dynamodb:GetItem",
            resource_arn=reps_arn,
            allowed=False,
            leading_keys=("conversation#smoke",),
        ),
        SimulationRow(
            check="role_denies_rep_item",
            role_arn=role_arn,
            action="dynamodb:GetItem",
            resource_arn=reps_arn,
            allowed=False,
            leading_keys=("smoke-rep",),
        ),
        SimulationRow(
            check="role_denies_reps_query",
            role_arn=role_arn,
            action="dynamodb:Query",
            resource_arn=reps_arn,
            allowed=False,
            leading_keys=settings_key,
        ),
        SimulationRow(
            check="role_denies_other_table",
            role_arn=role_arn,
            action="dynamodb:GetItem",
            resource_arn=other_table_arn,
            allowed=False,
            leading_keys=settings_key,
        ),
    ]
    return tuple(rows)


def _table_arn(ctx: SmokeContext, account_id: str, logical: str) -> str:
    names = ctx.outputs.require_mapping("dynamodb_table_names")
    return f"arn:aws:dynamodb:{ctx.region}:{account_id}:table/{names[logical]}"


def role_simulations(ctx: SmokeContext) -> Iterator[CheckResult]:
    """Simulate every `simulation_rows` entry against the deployed function's own role."""
    function_name = f"{function_name_prefix(ctx)}-user-settings"
    role_arn = role_arn_of_function(ctx.client("lambda"), function_name)
    account_id = str(ctx.client("sts").get_caller_identity()["Account"])
    iam = ctx.client("iam")
    for row in simulation_rows(
        role_arn=role_arn,
        reps_arn=_table_arn(ctx, account_id, "reps"),
        other_table_arn=_table_arn(ctx, account_id, "accounts"),
    ):
        yield simulation_result(iam, row, resource=function_name)


def _delete_settings_item(store: Any, *, tenant: str, rep_id: str, who: str) -> CheckResult:
    """Delete one user's settings key and prove it absent; the item carries no `tenant` attribute."""
    check = f"settings_item_cleaned_{who.lower()}"
    key = user_settings_key(tenant, rep_id)
    try:
        store.delete_item(SETTINGS_TABLE, {"rep_id": key})
        remaining = store.get_item(SETTINGS_TABLE, {"rep_id": key})
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check
        return _row(check, False, "", f"{type(exc).__name__}: {exc}")
    return _row(
        check,
        remaining is None,
        f"{who}'s settings key absent on read-back",
        f"{who}'s settings item still present after delete",
    )


def _teardown(store: Any, smoke_tenant: SmokeTenant) -> CheckResult:
    try:
        deleted = teardown_smoke_tenant(store, smoke_tenant)
    except Exception as exc:  # noqa: BLE001 - a failed teardown must never read as clean
        return _row("tenant_torn_down", False, "", f"{type(exc).__name__}: {exc}")
    return _row("tenant_torn_down", True, f"{sum(deleted.values())} item(s) deleted", "")


def checks(
    ctx: SmokeContext,
    *,
    store: Any | None = None,
    post: ApiPost | None = None,
    get: ApiGet | None = None,
    provisioner_factory: Callable[[SmokeTenant], Provisioner] | None = None,
    simulations: Callable[[SmokeContext], Iterator[CheckResult]] | None = None,
) -> Iterator[CheckResult]:
    """Yield every user-settings check; users, settings item and tenant are removed in a `finally`.

    Args:
        ctx: The smoke context.
        store: The operator's store over the deployed tables; :func:`live_store` by default.
        post: The authenticated POST; :func:`live_api_post` by default.
        get: The authenticated GET; :func:`live_api_get` by default.
        provisioner_factory: Builds one user's provisioner bound to the shared tenant.
        simulations: The role-simulation rows; :func:`role_simulations` by default.
    """
    resolved_store = store if store is not None else live_store(ctx)
    api = post if post is not None else live_api_post(ctx)
    api_get = get if get is not None else live_api_get(ctx)
    factory = (
        provisioner_factory
        if provisioner_factory is not None
        else lambda tenant: RoleSmokeUserProvisioner(
            ctx, full_visibility=False, smoke_tenant=tenant
        )
    )
    simulate = simulations if simulations is not None else role_simulations

    yield unauthenticated_check(api, route=ROUTE, body={"action": "get"}, resource=RESOURCE_CLASS)
    try:
        yield from simulate(ctx)
    except Exception as exc:  # noqa: BLE001 - the API rows still run
        yield _row("role_simulations", False, "", f"{type(exc).__name__}: {exc}")

    smoke_tenant = allocate_smoke_tenant(resolved_store)
    provisioners: list[Provisioner] = []
    rep_ids: dict[str, str] = {}
    try:
        seed_smoke_tenant(resolved_store, smoke_tenant)
        provisioners = [factory(smoke_tenant), factory(smoke_tenant)]
        user_a, row = provision_or_fail(
            provisioners[0], resource=RESOURCE_CLASS, check="provision_a"
        )
        yield row
        if user_a is not None:
            rep_ids["A"] = user_a.rep_id
        user_b, row = provision_or_fail(
            provisioners[1], resource=RESOURCE_CLASS, check="provision_b"
        )
        yield row
        if user_b is not None:
            rep_ids["B"] = user_b.rep_id
        if user_a is None or user_b is None:
            return
        tuning = valid_tuning()
        yield check_default_view(api, user_a.id_token, check="defaults_before_save", who="A")
        yield check_put_and_read_back(api, user_a.id_token, tuning)
        yield check_default_view(api, user_b.id_token, check="other_user_sees_defaults", who="B")
        yield check_rep_id_rejected(api, user_b.id_token, other_rep_id=user_a.rep_id, tuning=tuning)
        yield check_default_view(
            api, user_b.id_token, check="rejected_put_wrote_nothing_for_b", who="B"
        )
        yield check_unsupported_value_rejected(api, user_a.id_token, saved=tuning)
        yield check_temperature_rejected(api, user_a.id_token, saved=tuning)
        yield check_retired_model_rejected(api, user_a.id_token, saved=tuning)
        yield check_capabilities_current(api_get, user_a.id_token)
        yield from check_capabilities_supervisor_effort(api_get, user_a.id_token)
        yield from check_capabilities_served_defaults(api_get, user_a.id_token)
        yield check_item_key(resolved_store, tenant=smoke_tenant.tenant, rep_id=user_a.rep_id)
        yield check_reset(
            api, user_a.id_token, resolved_store, tenant=smoke_tenant.tenant, rep_id=user_a.rep_id
        )
    finally:
        for who, rep_id in rep_ids.items():
            yield _delete_settings_item(
                resolved_store, tenant=smoke_tenant.tenant, rep_id=rep_id, who=who
            )
        for provisioner in provisioners:
            yield from provisioner.cleanup()
        yield _teardown(resolved_store, smoke_tenant)


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """The unauthenticated row, every simulation row and the teardown row always run."""
    simulated = len(simulation_rows(role_arn="r", reps_arn="t", other_table_arn="o"))
    return ExpectedCheckCount(
        minimum=1 + simulated + 1,
        basis=(
            f"unauthenticated_request_denied, {simulated} simulation rows and tenant_torn_down run "
            "on every path; the API rows follow only once both users exist"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exercise the deployed user-settings route as two real users."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
