# SPDX-License-Identifier: MIT
"""Layer 4 T3 check: no globally accessible resources, asserted against the live account.

Every finding is an API response; nothing here reads Terraform text. Per stack Lambda function:

1. **`function_url_auth_type`** — a Function URL, if one exists, must report `AuthType: AWS_IAM`.
2. **`no_public_invoke_grant`** — the resource policy must carry no statement granting a wildcard
   principal (with or without a `lambda:FunctionUrlAuthType: NONE` condition).
3. **`vpc_attached`** — `VpcConfig` must name at least one subnet and one security group.

Once per stack:

4. **`rest_api_private_vpce_only`** — the REST API (id from the `api_id` output) is `PRIVATE`,
   attached to a VPC endpoint, and its resource policy denies `execute-api:Invoke` for any caller whose
   `aws:SourceVpce` is not one of exactly those endpoints.

CloudFront's TLS floor is checked by `scripts/smoke/cloudfront.py`; this module emits one `defer`
citing it.

The function set is enumerated from the account (paginated), filtered to this stack's name prefix,
and compared against the source-derived set in both directions. Discovery must also find a
known-present function, the relay.

Exits 3 (never 0) when the account could not be verified at all: no credentials, no outputs, a stale
outputs snapshot, or zero functions discovered.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from scripts.outputs_provenance import assess_freshness
from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    outputs_path,
    run_main,
)
from scripts.smoke.lambda_actions import FUNCTION_NAME_PREFIX

from crm.toolspec.definitions import ALL_TOOLS

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "public_exposure"

#: Exit status for "ran, verified nothing", matching `scripts/run_gates.py`'s skip status.
EXIT_VERIFIED_NOTHING: Final[int] = 3

#: Lambda functions this stack declares that are not action tools:
#: `scripts.smoke.deployed_parity.NON_TOOL_FUNCTIONS` plus the rest the Terraform composition
#: declares. A unit test reads every root `infra/*.tf` to keep this list complete.
NON_TOOL_FUNCTION_SUFFIXES: Final[tuple[str, ...]] = (
    "identity-interceptor",
    "pre-token-generation",
    "eval-trigger",
    "agent-relay",
    "capabilities",
    "chat-identity-interceptor",
    "chat-target",
    "sweep-control",
    "sweep-task",
    "user-settings",
)

#: The function whose presence shows discovery reached the right account, named by a stack output.
KNOWN_PRESENT_OUTPUT: Final[str] = "agent_relay_function_name"

#: The condition key a public Function URL grant is written against.
FUNCTION_URL_AUTH_TYPE_CONDITION_KEY: Final[str] = "lambda:FunctionUrlAuthType"

#: Principal forms that are a wildcard however they are spelled.
_WILDCARD_PRINCIPALS: Final[frozenset[str]] = frozenset({"*"})

#: The only acceptable Function URL auth type.
REQUIRED_FUNCTION_URL_AUTH_TYPE: Final[str] = "AWS_IAM"

#: Checks whose `skip` means the run verified nothing about the account, and so must exit
#: :data:`EXIT_VERIFIED_NOTHING` rather than 0. Read by :func:`vacuity_reason`.
VACUITY_CHECK_NAMES: Final[frozenset[str]] = frozenset(
    {"credentials", "outputs_freshness", "discovery"}
)

#: `defer` token for the control this module cites rather than re-implements.
TLS_DEFER_REASON: Final[str] = "owned_by_smoke_cloudfront_module"

#: The module that owns the CloudFront TLS floor, and the check name it emits.
TLS_OWNER_MODULE: Final[str] = "scripts/smoke/cloudfront.py"
TLS_OWNER_CHECK: Final[str] = "configured_tls_minimum"


def source_declared_function_names() -> tuple[str, ...]:
    """Every Lambda function name this stack declares, derived, never hand-listed.

    The action tools come from `crm.toolspec.definitions.ALL_TOOLS`, as in `lambda_actions` and
    `deployed_parity`.
    """
    tools = tuple(f"{FUNCTION_NAME_PREFIX}{tool.name}" for tool in ALL_TOOLS)
    others = tuple(f"{FUNCTION_NAME_PREFIX}{suffix}" for suffix in NON_TOOL_FUNCTION_SUFFIXES)
    return tuple(sorted(set(tools + others)))


def discover_stack_functions(ctx: SmokeContext) -> tuple[str, ...]:
    """Return every deployed function name carrying this stack's prefix, paginated.

    Reads every `list_functions` page.
    """
    client = ctx.client("lambda")
    names: list[str] = []
    paginator = client.get_paginator("list_functions")
    for page in paginator.paginate():
        for function in page.get("Functions", ()):
            name = str(function.get("FunctionName", ""))
            if name.startswith(FUNCTION_NAME_PREFIX):
                names.append(name)
    return tuple(sorted(set(names)))


def public_url_grant_statements(
    policy_document: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Return every statement granting a wildcard principal under `FunctionUrlAuthType: NONE`.

    Matches `Effect: Allow` with `Principal: "*"` (or `{"AWS": "*"}`, or a list containing `"*"`),
    whether or not it carries a condition equating :data:`FUNCTION_URL_AUTH_TYPE_CONDITION_KEY` to
    `NONE`. Returns the statements so the finding can name each `Sid`.
    """
    offenders: list[Mapping[str, Any]] = []
    for statement in _as_sequence(policy_document.get("Statement")):
        if not isinstance(statement, Mapping):
            continue
        if str(statement.get("Effect", "")) != "Allow":
            continue
        if not _principal_is_wildcard(statement.get("Principal")):
            continue
        offenders.append(statement)
    return tuple(offenders)


def _as_sequence(value: Any) -> Sequence[Any]:
    """Normalise IAM's one-or-many spelling to a sequence."""
    if value is None:
        return ()
    if isinstance(value, str | bytes | Mapping):
        return (value,)
    if isinstance(value, Sequence):
        return value
    return (value,)


def _principal_is_wildcard(principal: Any) -> bool:
    """Whether `principal` grants everyone, in any of IAM's spellings.

    `"*"`, `{"AWS": "*"}`, and `{"AWS": ["*", ...]}` are the same grant.
    """
    if isinstance(principal, str):
        return principal in _WILDCARD_PRINCIPALS
    if isinstance(principal, Mapping):
        return any(
            isinstance(entry, str) and entry in _WILDCARD_PRINCIPALS
            for values in principal.values()
            for entry in _as_sequence(values)
        )
    return any(
        isinstance(entry, str) and entry in _WILDCARD_PRINCIPALS
        for entry in _as_sequence(principal)
    )


def conditioned_on_auth_type_none(statement: Mapping[str, Any]) -> bool:
    """Whether `statement`'s condition pins `lambda:FunctionUrlAuthType` to `NONE`.

    Diagnostic only: distinguishes the conditioned and unconditioned forms in the finding's detail.
    """
    condition = statement.get("Condition")
    if not isinstance(condition, Mapping):
        return False
    for operands in condition.values():
        if not isinstance(operands, Mapping):
            continue
        for key, expected in operands.items():
            if str(key) != FUNCTION_URL_AUTH_TYPE_CONDITION_KEY:
                continue
            if any(str(value) == "NONE" for value in _as_sequence(expected)):
                return True
    return False


def vpc_config_attached(configuration: Mapping[str, Any]) -> bool:
    """Whether `configuration` reports a real VPC attachment.

    `VpcConfig` is present-but-empty for an unattached function, so both lists must be non-empty.
    """
    vpc_config = configuration.get("VpcConfig")
    if not isinstance(vpc_config, Mapping):
        return False
    subnets = _as_sequence(vpc_config.get("SubnetIds"))
    groups = _as_sequence(vpc_config.get("SecurityGroupIds"))
    return bool(subnets) and bool(groups)


def _get_function_url_auth_type(ctx: SmokeContext, name: str) -> str | None:
    """Return the function's URL auth type, or `None` when it has no URL configured.

    `ResourceNotFoundException` here means no Function URL exists, which is not a violation.
    """
    client = ctx.client("lambda")
    try:
        response = client.get_function_url_config(FunctionName=name)
    except Exception as exc:  # noqa: BLE001 - narrowed by name below; anything else re-raised
        if type(exc).__name__ == "ResourceNotFoundException":
            return None
        raise
    return str(response.get("AuthType", ""))


def _get_policy_document(ctx: SmokeContext, name: str) -> Mapping[str, Any] | None:
    """Return the function's resource policy, or `None` when it has none.

    `ResourceNotFoundException` means the function has no resource policy at all, which is not a
    violation.
    """
    client = ctx.client("lambda")
    try:
        response = client.get_policy(FunctionName=name)
    except Exception as exc:  # noqa: BLE001 - narrowed by name below; anything else re-raised
        if type(exc).__name__ == "ResourceNotFoundException":
            return None
        raise
    raw = response.get("Policy")
    if not isinstance(raw, str):
        return None
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, Mapping) else None


def check_function_url_auth(ctx: SmokeContext, name: str) -> CheckResult:
    """Assert this function's Function URL, if any, requires IAM auth."""
    auth_type = _get_function_url_auth_type(ctx, name)
    if auth_type is None:
        return CheckResult(
            resource=name,
            check="function_url_auth_type",
            status="pass",
            detail="no Function URL is configured, so none can be reached unauthenticated",
        )
    if auth_type == REQUIRED_FUNCTION_URL_AUTH_TYPE:
        return CheckResult(
            resource=name,
            check="function_url_auth_type",
            status="pass",
            detail=f"AuthType={auth_type}",
        )
    return CheckResult(
        resource=name,
        check="function_url_auth_type",
        status="fail",
        detail=(
            f"Function URL reports AuthType={auth_type!r}, which permits an unauthenticated "
            f"invoke; the required auth type is {REQUIRED_FUNCTION_URL_AUTH_TYPE}"
        ),
    )


def check_no_public_invoke_grant(ctx: SmokeContext, name: str) -> CheckResult:
    """Assert this function's resource policy grants no wildcard principal."""
    document = _get_policy_document(ctx, name)
    if document is None:
        return CheckResult(
            resource=name,
            check="no_public_invoke_grant",
            status="pass",
            detail="no resource policy exists, so invoke is refused by absence of permission",
        )
    offenders = public_url_grant_statements(document)
    if not offenders:
        return CheckResult(
            resource=name,
            check="no_public_invoke_grant",
            status="pass",
            detail=f"{len(_as_sequence(document.get('Statement')))} statement(s), none wildcard",
        )
    described = ", ".join(
        f"{statement.get('Sid', '<no Sid>')}"
        + (" [FunctionUrlAuthType=NONE]" if conditioned_on_auth_type_none(statement) else "")
        for statement in offenders
    )
    return CheckResult(
        resource=name,
        check="no_public_invoke_grant",
        status="fail",
        detail=(
            f"resource policy grants a wildcard principal in {len(offenders)} statement(s): "
            f"{described}. Remove with `aws lambda remove-permission --statement-id <Sid>`"
        ),
    )


def check_vpc_attached(ctx: SmokeContext, name: str) -> CheckResult:
    """Assert this function runs inside the VPC."""
    configuration = ctx.client("lambda").get_function_configuration(FunctionName=name)
    if vpc_config_attached(configuration):
        vpc_config = configuration.get("VpcConfig", {})
        return CheckResult(
            resource=name,
            check="vpc_attached",
            status="pass",
            detail=(
                f"{len(_as_sequence(vpc_config.get('SubnetIds')))} subnet(s), "
                f"{len(_as_sequence(vpc_config.get('SecurityGroupIds')))} security group(s)"
            ),
        )
    return CheckResult(
        resource=name,
        check="vpc_attached",
        status="fail",
        detail="VpcConfig names no subnet or no security group, so this function is not VPC-attached",
    )


#: The check name for the private-REST-API assertion.
REST_API_CHECK: Final[str] = "rest_api_private_vpce_only"

#: The REST API's endpoint type that means "not reachable over the public internet". The
#: `endpointConfiguration.types` list must be exactly this.
_PRIVATE_ENDPOINT_TYPES: Final[tuple[str, ...]] = ("PRIVATE",)

#: The condition key a private REST API's resource policy pins to bound invoke to its VPC endpoint.
#: Compared case-insensitively: live it is `aws:sourceVpce`, docs often write `aws:SourceVpce`.
_SOURCE_VPCE_CONDITION_KEY: Final[str] = "aws:sourcevpce"


def rest_api_private_vpce_only(ctx: SmokeContext) -> Iterator[CheckResult]:
    """Assert the stack's REST (v1) API is private and reachable only via its named VPC endpoint.

    The API id comes from the ``api_id`` stack output. Every AWS error here becomes a `fail` row.

    Three properties, reported as one row:

    1. ``endpointConfiguration.types`` is exactly ``["PRIVATE"]`` — not ``REGIONAL``/``EDGE``.
    2. ``vpcEndpointIds`` is non-empty.
    3. the resource ``policy`` carries a ``Deny`` on ``execute-api:Invoke`` conditioned on
       ``StringNotEquals aws:SourceVpce`` (case-insensitive key) naming exactly that endpoint set —
       so a caller arriving through any other route is denied.
    """
    api_id = str(ctx.outputs.get("api_id") or "")
    if not api_id:
        yield CheckResult(
            resource="rest_api",
            check=REST_API_CHECK,
            status="fail",
            detail=(
                "the `api_id` stack output is absent or empty, so the REST API's disposition is "
                "unknown — reported rather than skipped, and never derived from the private "
                "`<id>-vpce-…` invoke hostname"
            ),
        )
        return

    try:
        api = ctx.client("apigateway").get_rest_api(restApiId=api_id)
    except Exception as exc:  # noqa: BLE001 - any lookup failure is a fail row, never a generator abort
        yield CheckResult(
            resource=f"rest_api/{api_id}",
            check=REST_API_CHECK,
            status="fail",
            detail=(
                f"apigateway.get_rest_api({api_id!r}) failed ({type(exc).__name__}: {exc}); the "
                "REST API's disposition could not be read. Reported as a fail so the TLS row below "
                "is still emitted"
            ),
        )
        return

    failures = _rest_api_privacy_failures(api)
    endpoint = api.get("endpointConfiguration")
    endpoint = endpoint if isinstance(endpoint, Mapping) else {}
    vpce_ids = sorted(str(v) for v in _as_sequence(endpoint.get("vpcEndpointIds")))
    if failures:
        yield CheckResult(
            resource=f"rest_api/{api_id}",
            check=REST_API_CHECK,
            status="fail",
            detail="; ".join(failures),
        )
        return
    yield CheckResult(
        resource=f"rest_api/{api_id}",
        check=REST_API_CHECK,
        status="pass",
        detail=(
            f"endpointConfiguration.types=['PRIVATE'], vpcEndpointIds={vpce_ids}, and the resource "
            "policy denies execute-api:Invoke for any aws:SourceVpce outside that set"
        ),
    )


def _rest_api_privacy_failures(api: Mapping[str, Any]) -> list[str]:
    """Return one message per unmet private-REST-API property, empty when all three hold."""
    failures: list[str] = []
    endpoint = api.get("endpointConfiguration")
    endpoint = endpoint if isinstance(endpoint, Mapping) else {}
    types = tuple(str(t) for t in _as_sequence(endpoint.get("types")))
    if types != _PRIVATE_ENDPOINT_TYPES:
        failures.append(
            f"endpointConfiguration.types is {list(types)}, not {list(_PRIVATE_ENDPOINT_TYPES)}: "
            "the API is reachable outside a VPC endpoint"
        )
    vpce_ids = frozenset(str(v) for v in _as_sequence(endpoint.get("vpcEndpointIds")))
    if not vpce_ids:
        failures.append(
            "endpointConfiguration.vpcEndpointIds is empty: no VPC endpoint is attached"
        )
    denied = _source_vpce_deny_endpoint_set(api.get("policy"))
    if denied is None:
        failures.append(
            "the resource policy carries no Deny on execute-api:Invoke conditioned on "
            "StringNotEquals aws:SourceVpce, so a caller from another route is not denied"
        )
    elif vpce_ids and denied != vpce_ids:
        failures.append(
            f"the aws:SourceVpce Deny names {sorted(denied)} but the API is attached to "
            f"{sorted(vpce_ids)}; the deny must name exactly the attached endpoint set"
        )
    return failures


def _decode_rest_policy_document(policy_text: str) -> Any:
    """Decode the `policy` string `get_rest_api` returns, which AWS backslash-escapes.

    `apigateway get-rest-api` serialises the resource policy with its quotes backslash-escaped and
    its slashes as ``\\/`` (`{\\"Version\\":...}`), so the raw value is wrapped as a JSON string
    literal and decoded twice. Plain JSON parses on the first try; a value that decodes to a string is
    decoded once more. Returns `None` when nothing parses, which the caller treats as "no Deny".
    """
    try:
        decoded: Any = json.loads(policy_text)
    except json.JSONDecodeError:
        try:
            decoded = json.loads(json.loads(f'"{policy_text}"'))
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            return None
    return decoded


def _source_vpce_deny_endpoint_set(policy: Any) -> frozenset[str] | None:
    """Return the endpoint set a `Deny … StringNotEquals aws:SourceVpce` names, or `None` if absent.

    `get_rest_api` returns `policy` as a JSON string (often double-escaped), or a caller/test may pass
    a mapping directly; both are handled. The condition key is matched case-insensitively.
    """
    document = policy
    if isinstance(document, str):
        document = _decode_rest_policy_document(document)
    if not isinstance(document, Mapping):
        return None
    for statement in _as_sequence(document.get("Statement")):
        if not isinstance(statement, Mapping):
            continue
        if str(statement.get("Effect", "")) != "Deny":
            continue
        actions = {str(a).lower() for a in _as_sequence(statement.get("Action"))}
        if "execute-api:invoke" not in actions:
            continue
        condition = statement.get("Condition")
        if not isinstance(condition, Mapping):
            continue
        operands = condition.get("StringNotEquals")
        if not isinstance(operands, Mapping):
            continue
        for key, expected in operands.items():
            if str(key).lower() != _SOURCE_VPCE_CONDITION_KEY:
                continue
            return frozenset(str(v) for v in _as_sequence(expected))
    return None


def check_tls_floor_is_owned_elsewhere() -> CheckResult:
    """Record CloudFront's TLS floor as owned by `scripts/smoke/cloudfront.py`, not re-checked here.

    A `defer`, never a `pass`: this module verifies nothing about TLS.
    """
    return CheckResult(
        resource="cloudfront",
        check="tls_minimum_protocol_version",
        status="defer",
        defer_reason=TLS_DEFER_REASON,
        detail=(
            f"not re-checked here; {TLS_OWNER_MODULE} owns it via its {TLS_OWNER_CHECK!r} check and "
            "its standing disposition is unchanged. Duplicating it would create a second reader that "
            "can drift"
        ),
    )


def checks(ctx: SmokeContext, *, snapshot: Path | None = None) -> Iterator[CheckResult]:
    """Yield every public-exposure check against the live account.

    `snapshot` names the outputs document whose freshness is asserted first. Optional for offline
    tests; `main` always passes one.
    """
    if snapshot is not None:
        stale = outputs_staleness_reason(snapshot)
        if stale is not None:
            yield CheckResult(
                resource=RESOURCE_CLASS,
                check="outputs_freshness",
                status="skip",
                detail=f"{stale}. Refused rather than reported: exit {EXIT_VERIFIED_NOTHING}",
            )
            return
        disclosure = freshness_disclosure(snapshot)
        if disclosure is not None:
            yield disclosure

    try:
        discovered = discover_stack_functions(ctx)
    except Exception as exc:  # noqa: BLE001 - re-raised below unless it is a credential failure
        reason = credential_failure_reason(exc)
        if reason is None:
            raise
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="credentials",
            status="skip",
            detail=(
                f"this run never reached the account, so it verified nothing about it: {reason}. "
                f"Exit {EXIT_VERIFIED_NOTHING}, never 0"
            ),
        )
        return

    yield _discovery_check(ctx, discovered)
    if not discovered:
        # Nothing further is assertable, and the caller turns this into exit 3 rather than 0.
        yield check_tls_floor_is_owned_elsewhere()
        return

    yield _parity_check(discovered)

    for name in discovered:
        yield check_function_url_auth(ctx, name)
        yield check_no_public_invoke_grant(ctx, name)
        yield check_vpc_attached(ctx, name)

    yield from rest_api_private_vpce_only(ctx)
    yield check_tls_floor_is_owned_elsewhere()


def _discovery_check(ctx: SmokeContext, discovered: Sequence[str]) -> CheckResult:
    """Assert discovery found functions **and** found a known-present one.

    A non-empty result alone could come from the wrong account, so a known function must be present.
    """
    if not discovered:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="discovery",
            status="skip",
            detail=(
                f"no deployed function carries the {FUNCTION_NAME_PREFIX!r} prefix. This is a broken "
                "discovery step, not a clean account: every assertion below would be vacuous, so "
                f"this run exits {EXIT_VERIFIED_NOTHING} rather than 0"
            ),
        )
    expected_present = str(ctx.outputs.get(KNOWN_PRESENT_OUTPUT) or "")
    if expected_present and expected_present not in discovered:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="discovery",
            status="fail",
            detail=(
                f"discovery found {len(discovered)} function(s) but not {expected_present!r}, which "
                f"the {KNOWN_PRESENT_OUTPUT!r} output names — so this is not the account, region or "
                "stack the outputs describe"
            ),
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="discovery",
        status="pass",
        detail=f"{len(discovered)} function(s) carry the {FUNCTION_NAME_PREFIX!r} prefix, "
        f"including {expected_present or 'the expected set'}",
    )


def _parity_check(discovered: Sequence[str]) -> CheckResult:
    """Compare the live function set against the source-derived one, in both directions.

    Reported as one check listing both live-but-undeclared and declared-but-absent functions.
    """
    declared = set(source_declared_function_names())
    live = set(discovered)
    undeclared = sorted(live - declared)
    absent = sorted(declared - live)
    if not undeclared and not absent:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="source_parity",
            status="pass",
            detail=f"{len(live)} live function(s) match the source-derived set exactly",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="source_parity",
        status="fail",
        detail=(
            f"live-but-undeclared: {undeclared}; declared-but-absent: {absent}. The first is "
            "out-of-band state; the second means these checks covered less than "
            "the stack"
        ),
    )


def expected_check_count(ctx: SmokeContext, *, snapshot: Path | None = None) -> ExpectedCheckCount:
    """Derive the minimum check count from the source-declared function set.

    Three per function plus discovery, parity, the API endpoint and the TLS defer. A refused run
    (for example a stale snapshot) gets a floor of 1, so its `skip` is not compounded by a coverage
    failure.
    """
    del ctx
    if snapshot is not None:
        refusal = outputs_staleness_reason(snapshot)
        if refusal is not None:
            return ExpectedCheckCount(
                minimum=1,
                basis=(
                    "this run refused its inputs and asserts nothing about the account, so the only "
                    f"check it must emit is the refusal itself: {refusal}"
                ),
            )
    functions = len(source_declared_function_names())
    return ExpectedCheckCount(
        minimum=functions * 3 + 4,
        basis=(
            f"{functions} source-declared function(s) x 3 checks (function_url_auth_type, "
            "no_public_invoke_grant, vpc_attached) + discovery + source_parity + "
            "rest_api_private_vpce_only + the CloudFront TLS defer"
        ),
    )


#: Exception class names that mean "this run never reached the account", matched by name.
_CREDENTIAL_FAILURE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "NoCredentialsError",
        "PartialCredentialsError",
        "CredentialRetrievalError",
        "ProfileNotFound",
        "TokenRetrievalError",
        "UnauthorizedSSOTokenError",
        "SSOTokenLoadError",
        "NoRegionError",
    }
)

#: `ClientError` codes that mean the same thing one layer up.
_CREDENTIAL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "AuthFailure",
    }
)


def credential_failure_reason(exc: BaseException) -> str | None:
    """Return why `exc` means the account was never reached, or `None` if it is a real finding.

    Distinguishes "could not authenticate" (verified nothing) from "authenticated and was denied"
    (a finding).
    """
    name = type(exc).__name__
    if name in _CREDENTIAL_FAILURE_NAMES:
        return f"{name}: {exc}"
    code = (
        getattr(exc, "response", {}).get("Error", {}).get("Code")
        if hasattr(exc, "response")
        else None
    )
    if isinstance(code, str) and code in _CREDENTIAL_ERROR_CODES:
        return f"{code}: {exc}"
    return None


def outputs_staleness_reason(path: Path) -> str | None:
    """Return why the outputs snapshot at `path` cannot be trusted, or `None` if it is usable.

    Delegates to `scripts.outputs_provenance.assess_freshness`. Which comparison answered is
    reported separately by :func:`freshness_disclosure`.
    """
    return assess_freshness(path).refusal


def freshness_disclosure(path: Path) -> CheckResult | None:
    """A `defer` row when freshness was established by the weaker mtime comparison, else `None`.

    The mtime fallback applies to snapshots carrying no `_outputs_provenance` commit stamp.
    """
    verdict = assess_freshness(path)
    if not verdict.is_fresh or not verdict.used_weaker_form:
        return None
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="outputs_freshness_form",
        status="defer",
        defer_reason="snapshot_predates_provenance_stamp",
        detail=(
            f"the snapshot is accepted but {verdict.basis}. Re-run scripts/deploy.sh to record "
            "provenance and get a commit-form comparison"
        ),
    )


def vacuity_reason(results: Sequence[CheckResult]) -> str | None:
    """Return why this run verified nothing, or `None` if it verified something.

    Pure, so the exit-3 decision is testable without AWS. A run is vacuous when any check named in
    :data:`VACUITY_CHECK_NAMES` skipped — absent credentials, an absent or stale outputs snapshot, or
    a discovery step that enumerated nothing. A run with real `fail`s is not vacuous.
    """
    for result in results:
        if result.check in VACUITY_CHECK_NAMES and result.status == "skip":
            return result.detail
    return None


def _outputs_flag(argv: Sequence[str] | None) -> str | None:
    """Return the `--outputs` value in `argv`, in both the `--outputs X` and `--outputs=X` spellings.

    A pre-scan only: `run_main` remains the authority on argument validity.
    """
    arguments = list(argv if argv is not None else sys.argv[1:])
    for index, argument in enumerate(arguments):
        if argument == "--outputs" and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith("--outputs="):
            return argument.split("=", 1)[1]
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checks, then downgrade a vacuous 0 to :data:`EXIT_VERIFIED_NOTHING`.

    The decision is made from the rows `run_main` emitted. The outputs snapshot is resolved with the
    same flag/env precedence `context_from_args` uses; an absent snapshot is reported by `run_main`.
    """
    collected: list[CheckResult] = []
    snapshot = outputs_path(_outputs_flag(argv))

    def _recording(ctx: SmokeContext) -> Iterator[CheckResult]:
        for result in checks(ctx, snapshot=snapshot):
            collected.append(result)
            yield result

    def _expected(ctx: SmokeContext) -> ExpectedCheckCount:
        return expected_check_count(ctx, snapshot=snapshot)

    code = run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or "",
        checks=_recording,
        expected=_expected,
    )
    if not snapshot.exists():
        # `run_main` reports this as a failing `load_outputs` check and exits 1; nothing was examined.
        return EXIT_VERIFIED_NOTHING if code != 0 else code
    if vacuity_reason(collected) is not None:
        # A refused run reports 3 whatever else the harness appended.
        return EXIT_VERIFIED_NOTHING
    return code


__all__ = [
    "EXIT_VERIFIED_NOTHING",
    "VACUITY_CHECK_NAMES",
    "credential_failure_reason",
    "outputs_staleness_reason",
    "LAYER",
    "RESOURCE_CLASS",
    "REST_API_CHECK",
    "rest_api_private_vpce_only",
    "check_function_url_auth",
    "check_no_public_invoke_grant",
    "check_tls_floor_is_owned_elsewhere",
    "check_vpc_attached",
    "freshness_disclosure",
    "checks",
    "conditioned_on_auth_type_none",
    "discover_stack_functions",
    "expected_check_count",
    "main",
    "public_url_grant_statements",
    "source_declared_function_names",
    "vacuity_reason",
    "vpc_config_attached",
]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
