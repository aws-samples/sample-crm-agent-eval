# SPDX-License-Identifier: MIT
"""Layer 5 T3 smoke: the eval run API's admin-only operations.

Provisions two disposable MFA-enrolled users through :class:`RoleSmokeUserProvisioner`: an admin
(Cognito group ``crm.authz.ADMIN_GROUP``) and a non-admin holding ``full_visibility``. Every call goes
through ``POST /api/eval_trigger`` on the CloudFront domain with the user's own ID token.

Checks:

1. The route refuses a request with no token (401/403 from the authorizer).
2. The admin group exists in the pool, and the admin token's ``cognito:groups`` claim carries it
   (the rendering is recorded in the detail).
3. Admin ``list`` names a terminal run to aim the remaining calls at, so none can change anything
   even if its gate were missing.
4. Non-admin ``submit {mode: live, confirmed: false}``, ``cancel`` of that terminal run and
   ``unlock`` each return 403 ``eval:admin``. ``confirmed: false`` means a missing gate surfaces as
   400 at the confirmation check and never starts a paid sweep.
5. Admin ``cancel`` of that terminal run returns 200 ``already_terminal`` and leaves the run unchanged.

Layer 5 runs ``eval_sfn`` first, so a terminal run normally exists. Both users are removed in a
``finally``. Never writes ``demo`` or ``canary``.

Also hosts the helpers ``eval_sfn`` and ``user_settings_api`` share: the role-aware provisioner, the
authenticated JSON POST, the live store, and the IAM simulation call.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from scripts.smoke.cognito_claims import LiveSmokeUserProvisioner, ProvisionedSmokeUser
from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    error_code,
    open_https,
    run_main,
)
from scripts.smoke.tenant import SmokeTenant
from scripts.smoke.verified_claims import USE_ID, ClaimsVerificationError, verified_claims

from crm.authz import ADMIN_GROUP
from crm.envelope import CLAIM_GROUPS, split_groups
from crm.store.store import Store
from eval.trigger import EVAL_ADMIN_SCOPE

LAYER: Final[int] = 5
RESOURCE_CLASS: Final[str] = "admin_authz"

EVAL_TRIGGER_ROUTE: Final[str] = "eval_trigger"

#: `(route, id_token_or_None, body) -> (http_status, json_object_body)`.
ApiPost = Callable[[str, "str | None", Mapping[str, Any]], "tuple[int, dict[str, Any]]"]

_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0
_SIMULATE_MAX_ATTEMPTS: Final[int] = 6
_SIMULATE_BACKOFF_SECONDS: Final[float] = 2.0
_THROTTLE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"Throttling", "ThrottlingException", "TooManyRequestsException"}
)

#: Statuses the REST authorizer answers an unauthenticated request with.
UNAUTHENTICATED_STATUSES: Final[frozenset[int]] = frozenset({401, 403})


# -- shared helpers ------------------------------------------------------------------------------


def load_json_object(raw: bytes) -> dict[str, Any]:
    """Decode a JSON object body, or `{}` when the body is empty, not JSON, or not an object."""
    try:
        parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def live_api_post(ctx: SmokeContext) -> ApiPost:
    """Build the live :data:`ApiPost`: `POST https://<cloudfront_domain_name>/api/<route>`.

    A token rides in `Authorization: Bearer`; `None` sends no `Authorization` header at all.
    """
    domain = ctx.outputs.require_str("cloudfront_domain_name")

    def post(route: str, token: str | None, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(  # noqa: S310 - fixed https CloudFront host
            f"https://{domain}/api/{route}",
            data=json.dumps(dict(body)).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with open_https(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                return int(response.status), load_json_object(response.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), load_json_object(exc.read())

    return post


def error_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The taxonomy `error` object of a failure body, or `{}`."""
    error = payload.get("error")
    return dict(error) if isinstance(error, Mapping) else {}


def summarise(payload: Mapping[str, Any], limit: int = 240) -> str:
    """A bounded JSON rendering of a response body for a check's detail."""
    return json.dumps(payload, sort_keys=True, default=str)[:limit]


def live_table_prefix(ctx: SmokeContext) -> str:
    """The deployed table-name prefix, from the `dynamodb_table_names` output's `eval_runs` entry."""
    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    return str(table_names["eval_runs"]).removesuffix("eval_runs")


def live_store(ctx: SmokeContext) -> Store:
    """A :class:`Store` over the deployed tables, with the operator's credentials."""
    return Store(client=ctx.client("dynamodb"), table_prefix=live_table_prefix(ctx))


def function_name_prefix(ctx: SmokeContext) -> str:
    """The Lambda name prefix, from the `eval_trigger_function_name` output.

    Raises:
        RuntimeError: The output does not end in `-eval-trigger`.
    """
    trigger = ctx.outputs.require_str("eval_trigger_function_name")
    prefix = trigger.removesuffix("-eval-trigger")
    if prefix == trigger:
        raise RuntimeError(
            f"eval_trigger_function_name {trigger!r} does not end in '-eval-trigger'; cannot derive "
            "the sibling function names"
        )
    return prefix


def role_arn_of_function(lambda_client: Any, function_name: str) -> str:
    """The execution role ARN the deployed function runs as."""
    return str(lambda_client.get_function_configuration(FunctionName=function_name)["Role"])


def source_function_arn(role_arn: str, region: str | None) -> str | None:
    """The Lambda function ARN `<prefix>-<name>-role` implies: `<prefix>-<name>` in `region`.

    None for a role ARN of any other shape, or without a region.
    """
    parts = role_arn.split(":", 5)
    if len(parts) != 6 or parts[2] != "iam" or not parts[5].startswith("role/") or not region:
        return None
    role_name = parts[5].removeprefix("role/").rsplit("/", 1)[-1]
    if not role_name.endswith(_ROLE_SUFFIX) or len(role_name) == len(_ROLE_SUFFIX):
        return None
    return f"arn:aws:lambda:{region}:{parts[4]}:function:{role_name.removesuffix(_ROLE_SUFFIX)}"


_ROLE_SUFFIX: Final[str] = "-role"


def simulate_evaluation(
    iam: Any,
    role_arn: str,
    action: str,
    resource_arn: str,
    *,
    leading_keys: Sequence[str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[str, tuple[str, ...]]:
    """One `SimulatePrincipalPolicy` decision (or `no_result`) and its `MissingContextValues`.

    `leading_keys` adds a `dynamodb:LeadingKeys` context entry, so a key-prefix condition is
    evaluated against a concrete key. `lambda:SourceFunctionArn` is set to the function ARN the
    role's name implies (:func:`source_function_arn`), as every call from that function's code
    carries it; it is omitted when the role name or the client's region does not imply one.
    Throttles are retried, then raised.
    """
    request: dict[str, Any] = {
        "PolicySourceArn": role_arn,
        "ActionNames": [action],
        "ResourceArns": [resource_arn],
    }
    context: list[dict[str, Any]] = []
    if leading_keys is not None:
        context.append(
            {
                "ContextKeyName": "dynamodb:LeadingKeys",
                "ContextKeyType": "stringList",
                "ContextKeyValues": list(leading_keys),
            }
        )
    function_arn = source_function_arn(
        role_arn, getattr(getattr(iam, "meta", None), "region_name", None)
    )
    if function_arn is not None:
        context.append(
            {
                "ContextKeyName": "lambda:SourceFunctionArn",
                "ContextKeyType": "string",
                "ContextKeyValues": [function_arn],
            }
        )
    if context:
        request["ContextEntries"] = context
    for attempt in range(1, _SIMULATE_MAX_ATTEMPTS + 1):
        try:
            response = iam.simulate_principal_policy(**request)
            break
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is a retryable throttle
            if attempt == _SIMULATE_MAX_ATTEMPTS or error_code(exc) not in _THROTTLE_ERROR_CODES:
                raise
            sleeper(_SIMULATE_BACKOFF_SECONDS * attempt)
    results = response.get("EvaluationResults", [])
    if not results:
        return "no_result", ()
    missing = tuple(str(key) for key in results[0].get("MissingContextValues") or ())
    return str(results[0]["EvalDecision"]), missing


def simulate_decision(
    iam: Any,
    role_arn: str,
    action: str,
    resource_arn: str,
    *,
    leading_keys: Sequence[str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """The decision half of :func:`simulate_evaluation`."""
    return simulate_evaluation(
        iam, role_arn, action, resource_arn, leading_keys=leading_keys, sleeper=sleeper
    )[0]


#: Service-namespaced keys AWS is known to set on requests to other services, so never ignorable by
#: namespace. A hand-kept list: nothing derives it from the condition keys the deployed policies use.
CROSS_SERVICE_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {"lambda:sourcefunctionarn", "ec2:sourceinstancearn"}
)


def decisive_missing_keys(action: str, missing: Sequence[str]) -> tuple[str, ...]:
    """The `MissingContextValues` entries that can change `action`'s decision.

    Another service's namespaced key is treated as not decisive: it is assumed absent from this
    request's context, as `dynamodb:LeadingKeys` is from non-DynamoDB actions. That holds for the
    keys this module has seen ignored, not for every key IAM defines; a service key that AWS does
    set on other services' requests belongs in :data:`CROSS_SERVICE_CONTEXT_KEYS`. Global `aws:`
    keys, the action's own service's keys, that set, and anything without a namespace stay decisive.
    """
    service = action.partition(":")[0].lower()
    decisive = []
    for key in missing:
        namespace, separator, _name = key.partition(":")
        namespace = namespace.lower()
        if (
            not separator
            or namespace in ("aws", service)
            or key.lower() in CROSS_SERVICE_CONTEXT_KEYS
        ):
            decisive.append(key)
    return tuple(decisive)


@dataclass(frozen=True, slots=True)
class SimulationRow:
    """One expected simulation outcome: who, what, on which resource, and whether it is allowed."""

    check: str
    role_arn: str
    action: str
    resource_arn: str
    allowed: bool
    leading_keys: tuple[str, ...] | None = None


def simulation_result(
    iam: Any, row: SimulationRow, *, resource: str, sleeper: Callable[[float], None] = time.sleep
) -> CheckResult:
    """Simulate `row` and pass only when the decision matches its expectation.

    `no_result`, a raised call and any decisive `MissingContextValues` entry
    (:func:`decisive_missing_keys`) are failures: a decision reached with such a key unset says
    nothing about the condition. Another service's keys are listed in the detail as not decisive.
    An expected denial accepts either deny kind.
    """
    keys = f" LeadingKeys={list(row.leading_keys)}" if row.leading_keys is not None else ""
    subject = f"{row.action} on {row.resource_arn}{keys}"
    try:
        decision, missing = simulate_evaluation(
            iam,
            row.role_arn,
            row.action,
            row.resource_arn,
            leading_keys=row.leading_keys,
            sleeper=sleeper,
        )
    except Exception as exc:  # noqa: BLE001 - reported as a failing row, never swallowed
        return CheckResult(
            resource=resource,
            check=row.check,
            status="fail",
            detail=f"simulate_principal_policy raised {type(exc).__name__}: {exc} ({subject})",
        )
    if decision == "no_result":
        ok = False
    elif row.allowed:
        ok = decision == "allowed"
    else:
        ok = decision in ("explicitDeny", "implicitDeny")
    expected = "allowed" if row.allowed else "denied"
    decisive = decisive_missing_keys(row.action, missing)
    ignored = [key for key in missing if key not in decisive]
    unset = f"; MissingContextValues={list(decisive)}" if decisive else ""
    not_decisive = (
        f"; not decisive (another service's keys, never in a {row.action} request): {ignored}"
        if ignored
        else ""
    )
    return CheckResult(
        resource=resource,
        check=row.check,
        status="pass" if ok and not decisive else "fail",
        detail=f"{subject}: {decision} (expected {expected}) for {row.role_arn}{unset}{not_decisive}",
    )


class RoleSmokeUserProvisioner(LiveSmokeUserProvisioner):
    """`LiveSmokeUserProvisioner` with a chosen `full_visibility` and optional admin membership.

    The `reps` put carries `full_visibility`, which the pre-token-generation trigger stamps into
    the token. With `admin_group` set, the user joins that group right after its password is set,
    before the first sign-in, so the issued ID token carries `cognito:groups`. Deleting the user in
    `cleanup` removes the membership with it.
    """

    def __init__(
        self,
        ctx: SmokeContext,
        *,
        full_visibility: bool,
        admin_group: str | None = None,
        smoke_tenant: SmokeTenant | None = None,
        jwks_url: str | None = None,
    ) -> None:
        super().__init__(ctx, smoke_tenant=smoke_tenant, jwks_url=jwks_url)
        self._full_visibility = full_visibility
        self._admin_group = admin_group

    def _store(self) -> Any:
        return _RepsVisibilityStore(super()._store(), full_visibility=self._full_visibility)

    def _idp(self) -> Any:
        if self._admin_group is None:
            return super()._idp()
        return _GroupJoiningIdp(super()._idp(), group=self._admin_group)

    def store(self) -> Any:
        """The store `provision` writes through."""
        return self._store()


class _RepsVisibilityStore:
    """Delegates to a real `Store`, forcing `full_visibility` on any `reps` put."""

    def __init__(self, base: Any, *, full_visibility: bool) -> None:
        self._base = base
        self._full_visibility = full_visibility

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def put_item(self, table: str, item: Mapping[str, Any]) -> Any:
        if table == "reps":
            item = {**item, "full_visibility": self._full_visibility}
        return self._base.put_item(table, item)


class _GroupJoiningIdp:
    """Delegates to a real `cognito-idp` client, adding the user to `group` once its password is set."""

    def __init__(self, base: Any, *, group: str) -> None:
        self._base = base
        self._group = group

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def admin_set_user_password(self, **kwargs: Any) -> Any:
        result = self._base.admin_set_user_password(**kwargs)
        self._base.admin_add_user_to_group(
            UserPoolId=kwargs["UserPoolId"], Username=kwargs["Username"], GroupName=self._group
        )
        return result


class Provisioner(Protocol):
    """The provisioner seam the checks drive; the offline tests inject a fake."""

    def provision(self) -> ProvisionedSmokeUser: ...

    def cleanup(self) -> Iterable[CheckResult]: ...


def provision_or_fail(
    provisioner: Provisioner, *, resource: str, check: str
) -> tuple[ProvisionedSmokeUser | None, CheckResult]:
    """Provision one user, returning it with a pass row, or `None` with a fail row."""
    try:
        user = provisioner.provision()
    except Exception as exc:  # noqa: BLE001 - one actionable failing check
        return None, CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"could not provision the smoke user: {type(exc).__name__}: {exc}",
        )
    return user, CheckResult(resource=resource, check=check, status="pass", detail=user.detail)


def unauthenticated_check(
    post: ApiPost, *, route: str, body: Mapping[str, Any], resource: str
) -> CheckResult:
    """The route answers a request carrying no token with 401/403 and no success body."""
    status, payload = post(route, None, body)
    denied = status in UNAUTHENTICATED_STATUSES
    return CheckResult(
        resource=resource,
        check="unauthenticated_request_denied",
        status="pass" if denied else "fail",
        detail=(
            f"POST /api/{route} with no Authorization header -> HTTP {status} (the authorizer "
            "answered; this proves authorization, not unreachability)"
            if denied
            else f"POST /api/{route} with no token -> HTTP {status} {summarise(payload)}: an "
            "unauthenticated caller was not refused"
        ),
    )


# -- this module's checks ------------------------------------------------------------------------


def check_admin_group_exists(idp: Any, *, user_pool_id: str) -> CheckResult:
    """The group the server's `is_admin` reads (`crm.authz.ADMIN_GROUP`) exists in the pool."""
    try:
        idp.get_group(GroupName=ADMIN_GROUP, UserPoolId=user_pool_id)
    except Exception as exc:  # noqa: BLE001 - reported as a failing check
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="admin_group_exists",
            status="fail",
            detail=f"get_group({ADMIN_GROUP!r}) raised {type(exc).__name__}: {exc}",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="admin_group_exists",
        status="pass",
        detail=f"Cognito group {ADMIN_GROUP!r} exists in the user pool",
    )


def check_groups_claim(
    id_token: str,
    *,
    region: str,
    user_pool_id: str,
    client_id: str,
    jwks_url: str | None = None,
) -> CheckResult:
    """The admin ID token verifies and its `cognito:groups` claim names the admin group.

    The claim's rendering is recorded. A token failing `verified_claims` fails the row.
    """
    try:
        claims = verified_claims(
            id_token,
            region=region,
            user_pool_id=user_pool_id,
            client_id=client_id,
            token_use=USE_ID,
            jwks_url=jwks_url,
        )
    except ClaimsVerificationError as exc:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="admin_token_groups_claim",
            status="fail",
            detail=f"admin ID token failed verification: {exc}",
        )
    raw = claims.get(CLAIM_GROUPS)
    rendering = f"{type(raw).__name__} {json.dumps(raw, default=str)}"
    try:
        groups = split_groups(raw)
    except Exception as exc:  # noqa: BLE001 - an unparseable claim is the failure under test
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="admin_token_groups_claim",
            status="fail",
            detail=f"{CLAIM_GROUPS} rendered as {rendering} does not parse: {exc}",
        )
    present = ADMIN_GROUP in groups
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="admin_token_groups_claim",
        status="pass" if present else "fail",
        detail=(
            f"ID token {CLAIM_GROUPS} rendered as {rendering}; parses to {sorted(groups)}"
            + ("" if present else f", which lacks {ADMIN_GROUP!r}")
        ),
    )


def find_terminal_run(post: ApiPost, token: str) -> tuple[dict[str, Any] | None, CheckResult]:
    """The newest terminal run on the admin's first `list` page, with the check that found it."""
    status, payload = post(EVAL_TRIGGER_ROUTE, token, {"action": "list"})
    runs = payload.get("runs")
    if status != 200 or not isinstance(runs, list):
        return None, CheckResult(
            resource=RESOURCE_CLASS,
            check="terminal_run_available",
            status="fail",
            detail=f"admin list -> HTTP {status} {summarise(payload)}",
        )
    for run in runs:
        if isinstance(run, dict) and run.get("is_terminal") is True and run.get("run_id"):
            return run, CheckResult(
                resource=RESOURCE_CLASS,
                check="terminal_run_available",
                status="pass",
                detail=f"newest terminal run on page 1: status={run.get('status')!r}",
            )
    return None, CheckResult(
        resource=RESOURCE_CLASS,
        check="terminal_run_available",
        status="fail",
        detail=(
            f"no terminal run among {len(runs)} on page 1, so the cancel checks have no harmless "
            "target; eval_sfn runs earlier in Layer 5 and leaves two"
        ),
    )


def check_non_admin_denied(
    post: ApiPost, token: str, *, check: str, body: Mapping[str, Any]
) -> CheckResult:
    """A non-admin call returns 403 `scope_denied` with `missing_scope == eval:admin`."""
    status, payload = post(EVAL_TRIGGER_ROUTE, token, body)
    error = error_fields(payload)
    denied = (
        status == 403
        and error.get("code") == "scope_denied"
        and error.get("missing_scope") == EVAL_ADMIN_SCOPE
    )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if denied else "fail",
        detail=(
            f"non-admin {summarise(body, 120)} -> 403 missing_scope={EVAL_ADMIN_SCOPE}"
            if denied
            else f"non-admin {summarise(body, 120)} -> HTTP {status} {summarise(payload)}; "
            f"expected 403 scope_denied {EVAL_ADMIN_SCOPE}"
        ),
    )


#: Run fields an `already_terminal` cancel must leave as they were.
_UNCHANGED_FIELDS: Final[tuple[str, ...]] = (
    "status",
    "tasks_done",
    "tasks_failed",
    "tasks_skipped",
    "cost_spent_usd",
    "finished_at",
)


def check_admin_terminal_cancel_noop(
    post: ApiPost, token: str, run: Mapping[str, Any]
) -> CheckResult:
    """Admin `cancel` of a terminal run returns 200 `already_terminal` and changes no field."""
    run_id = str(run["run_id"])
    status, payload = post(EVAL_TRIGGER_ROUTE, token, {"action": "cancel", "run_id": run_id})
    raw_after = payload.get("run")
    after: dict[str, Any] = raw_after if isinstance(raw_after, dict) else {}
    disposition = payload.get("disposition")
    changed = [name for name in _UNCHANGED_FIELDS if run.get(name) != after.get(name)]
    ok = status == 200 and disposition == "already_terminal" and not changed
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="admin_cancel_of_terminal_run_is_noop",
        status="pass" if ok else "fail",
        detail=(
            f"admin cancel of a terminal run -> 200 already_terminal; {list(_UNCHANGED_FIELDS)} "
            "unchanged"
            if ok
            else f"admin cancel -> HTTP {status} disposition={disposition!r} changed={changed} "
            f"{summarise(payload)}"
        ),
    )


def checks(
    ctx: SmokeContext,
    *,
    admin: Provisioner | None = None,
    non_admin: Provisioner | None = None,
    post: ApiPost | None = None,
    jwks_url: str | None = None,
) -> Iterator[CheckResult]:
    """Yield every admin-authz check; both users are removed in a `finally`.

    Args:
        ctx: The smoke context (`cognito-idp` client, stack outputs).
        admin, non_admin: Provisioners for the two users; live ones by default.
        post: The authenticated POST; :func:`live_api_post` by default.
        jwks_url: JWKS endpoint tokens are verified against; the pool's by default.
    """
    admin_provisioner: Provisioner = (
        admin
        if admin is not None
        else RoleSmokeUserProvisioner(
            ctx, full_visibility=True, admin_group=ADMIN_GROUP, jwks_url=jwks_url
        )
    )
    non_admin_provisioner: Provisioner = (
        non_admin
        if non_admin is not None
        else RoleSmokeUserProvisioner(ctx, full_visibility=True, jwks_url=jwks_url)
    )
    api = post if post is not None else live_api_post(ctx)
    try:
        yield unauthenticated_check(
            api, route=EVAL_TRIGGER_ROUTE, body={"action": "list"}, resource=RESOURCE_CLASS
        )
        yield check_admin_group_exists(
            ctx.client("cognito-idp"), user_pool_id=ctx.outputs.require_str("cognito_user_pool_id")
        )
        admin_user, row = provision_or_fail(
            admin_provisioner, resource=RESOURCE_CLASS, check="provision_admin_user"
        )
        yield row
        non_admin_user, row = provision_or_fail(
            non_admin_provisioner, resource=RESOURCE_CLASS, check="provision_non_admin_user"
        )
        yield row
        if admin_user is None or non_admin_user is None:
            return
        yield check_groups_claim(
            admin_user.id_token,
            region=ctx.region,
            user_pool_id=ctx.outputs.require_str("cognito_user_pool_id"),
            client_id=ctx.outputs.require_str("cognito_spa_client_id"),
            jwks_url=jwks_url,
        )
        run, row = find_terminal_run(api, admin_user.id_token)
        yield row
        yield check_non_admin_denied(
            api,
            non_admin_user.id_token,
            check="non_admin_submit_live_denied",
            body={"action": "submit", "mode": "live", "confirmed": False},
        )
        yield check_non_admin_denied(
            api, non_admin_user.id_token, check="non_admin_unlock_denied", body={"action": "unlock"}
        )
        if run is None:
            return
        yield check_non_admin_denied(
            api,
            non_admin_user.id_token,
            check="non_admin_cancel_denied",
            body={"action": "cancel", "run_id": str(run["run_id"])},
        )
        yield check_admin_terminal_cancel_noop(api, admin_user.id_token, run)
    finally:
        yield from admin_provisioner.cleanup()
        yield from non_admin_provisioner.cleanup()


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """The unauthenticated, group-exists and two provisioning rows always run."""
    return ExpectedCheckCount(
        minimum=4,
        basis=(
            "unauthenticated_request_denied, admin_group_exists, and one provisioning row per user "
            "run on every path; the authz rows follow only once both users exist"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: provision an admin and a non-admin user and exercise the admin-only actions."""
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
