# SPDX-License-Identifier: MIT
"""Layer 5 smoke script: UI -> CloudFront `/api/*` -> API Gateway -> Lambda -> DynamoDB round trip.

Needs a live deployment. Drives a create-then-read-then-update sequence for one opportunity
through the CloudFront `/api/*` behaviour (never directly against `api_endpoint`), with
a real Cognito token, and reads the resulting DynamoDB row back directly to assert the API's
claim and the persisted state agree - including the tenant prefix and the audit attributes a
committed write leaves behind. Also asserts an out-of-book target is refused with `403`
specifically through the same full path, and that every write this script issues carries the
smoke tenant, never `demo`.

The CloudFront hop is asserted behaviourally: the same authenticated read that answers `200`
answers `401` with the header removed, and two identical authenticated reads both report an
`X-Cache` miss.

Tenant safety: every call writes under the supplied token's `custom:tenant` claim. When
`--tenant` is not supplied the claim is used, with the same refusal `scripts/smoke/api_routes.py`
applies: a token naming `demo`, naming no tenant, or naming a tenant that is not a disposable
`eval-` one means no call is made, reported as a failure.

Account resolution: `--account-id`/`CRM_SMOKE_ACCOUNT_ID`
wins; otherwise the in-book account is the lowest entry of the token's own `custom:book` claim, and a
non-JWT token or an absent, empty or malformed claim is a failure naming which. `--out-of-book-account-id`/`CRM_SMOKE_OUT_OF_BOOK_ACCOUNT_ID` wins; otherwise the target is
the lowest-numbered seeded `acct-<tenant>-NNNN` account outside that book, read back from DynamoDB so
the 403 refuses a real record of the same tenant rather than a not-found.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.api_routes import (
    TENANT_CLAIM,
    _tenant_refusal,
    verified_token_claims,
)
from scripts.smoke.harness import CheckResult, SmokeContext, open_https, run_main

from crm.authz import DEMO_TENANT
from crm.envelope import CLAIM_BOOK, split_book
from crm.errors import AuthenticationFailed, CrmError
from crm.toolspec.definitions import BY_NAME

LAYER = 5
RESOURCE_CLASS = "integration_ui_api_ddb"

_OPPORTUNITY_TABLE = "opportunities"
_ACCOUNT_TABLE = "accounts"

#: Response header CloudFront reports its cache disposition in.
_CACHE_HEADER: Final[str] = "x-cache"

# (status_code, response_body, response_headers) for a completed HTTP call, or a `str`
# transport-failure message when no HTTP response was received at all.
HttpResult = tuple[int, str, Mapping[str, str]] | str

HttpCaller = Callable[[str, str, Mapping[str, str], str], HttpResult]


def default_http_caller(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
    """Perform one HTTP request using only `urllib.request` (no `requests` dependency).

    Returns `(status_code, response_body, response_headers)` for any received response, including
    4xx/5xx (an `HTTPError`'s `.code`/`.read()`/`.headers` are captured as a normal result, not a
    transport failure). Response header names are lower-cased. Returns a `str` describing the
    failure only when no HTTP response was received at all.
    """
    request = urllib.request.Request(  # noqa: S310 - internal seam, no untrusted URL
        url, data=body.encode("utf-8") if body else None, headers=dict(headers), method=method
    )
    try:
        with open_https(request, timeout=30) as response:
            return (
                response.status,
                response.read().decode("utf-8", errors="replace"),
                _lowercased(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.read().decode("utf-8", errors="replace"),
            _lowercased(exc.headers.items() if exc.headers else ()),
        )
    except urllib.error.URLError as exc:
        return f"transport error calling {url}: {exc}"


def _lowercased(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Return `items` as a mapping with lower-cased keys."""
    return {name.lower(): value for name, value in items}


def _token_from_env_or_flag(argv: Sequence[str] | None) -> str | None:
    """Resolve the Cognito access token: `--token` flag -> `CRM_SMOKE_TOKEN` env -> `None`.

    Never mints, signs, or fetches a token; the caller supplies an already-issued one. Parsed
    out of `argv` ahead of `run_main`; `--token` is declared to `run_main` via `extra_arguments`.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--token" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--token="):
            return arg.split("=", 1)[1]
    return os.environ.get("CRM_SMOKE_TOKEN")


def _tool_invoke_url(cloudfront_domain_name: str, tool_name: str) -> str:
    """Build the invoke URL for one tool call through the CloudFront `/api/*` behaviour.

    Every tool is reachable at `POST {apiBaseUrl}/{tool_name}`, fronted by the distribution's
    `/api/*` behaviour, so the URL is `https://<cloudfront_domain_name>/api/<tool_name>`.
    """
    return f"https://{cloudfront_domain_name}/api/{tool_name}"


def _live_table_prefix(ctx: SmokeContext) -> str:
    """Derive the live DynamoDB table-name prefix from the `dynamodb_table_names` output.

    Mirrors `scripts/smoke/api_routes.py` and `scripts/smoke/lambda_actions.py`: the physical name for
    the `eval_runs` logical table is `<table_prefix>eval_runs`, so stripping that suffix recovers the
    prefix.
    """
    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    return str(table_names["eval_runs"]).removesuffix("eval_runs")


def _spying_caller(caller: HttpCaller, calls: list[tuple[str, str, str]]) -> HttpCaller:
    """Wrap `caller`, recording every `(method, url, body)` triple issued through it.

    The recorded calls are what `writes_only_under_smoke_tenant` inspects.
    """

    def spy(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        calls.append((method, url, body))
        return caller(method, url, headers, body)

    return spy


def _call_tool(
    caller: HttpCaller,
    cloudfront_domain_name: str,
    tool_name: str,
    token: str | None,
    arguments: dict[str, Any],
) -> HttpResult:
    """Call one tool through the CloudFront `/api/*` behaviour.

    A `token` of `None` sends no `Authorization` header at all, which is how the
    header-forwarding assertion obtains its negative half.
    """
    url = _tool_invoke_url(cloudfront_domain_name, tool_name)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return caller("POST", url, headers, json.dumps(arguments))


def _parse_json_body(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_opportunity_row(ctx: SmokeContext, opportunity_id: str) -> dict[str, Any] | None:
    """Read the persisted opportunity row directly from DynamoDB, never trusting the API response.

    The store is built with the live table prefix, so it addresses the same physical tables the
    Lambdas wrote to.
    """
    from crm.store.store import Store

    store = Store(client=ctx.client("dynamodb"), table_prefix=_live_table_prefix(ctx))
    return store.get_item(_OPPORTUNITY_TABLE, {"opportunity_id": opportunity_id})


def in_book_accounts(claims: Mapping[str, Any]) -> list[str]:
    """Return the sorted account ids in a verified token's `custom:book` claim.

    ``claims`` are the token's claims as :func:`scripts.smoke.api_routes.token_claims` returns them.

    Raises:
        ValueError: when ``claims`` is empty, or the claim is absent, empty, or not a book-shaped
            value; the message names which.
    """
    if not claims:
        raise ValueError(f"the token carries no verified claim set, so no {CLAIM_BOOK} claim")
    try:
        book = split_book(claims.get(CLAIM_BOOK))
    except AuthenticationFailed as exc:
        raise ValueError(f"the token's {CLAIM_BOOK} claim is malformed: {exc.detail}") from exc
    if not book:
        raise ValueError(f"the token's {CLAIM_BOOK} claim is absent or empty")
    return sorted(book)


def _seeded_account_outside_book(ctx: SmokeContext, tenant: str, book: Iterable[str]) -> str:
    """Return the lowest-numbered seeded `acct-<tenant>-NNNN` account of ``tenant`` not in ``book``.

    The id form is the fixture generator's (see `scripts.smoke.cognito_claims._seeded_book`). Each
    candidate is read back from DynamoDB and must carry ``tenant``; the first `len(book) + 1`
    indices are tried, so at least one candidate lies outside the book.

    Raises:
        LookupError: when no candidate exists as a row of ``tenant``.
        CrmError: when the DynamoDB read fails.
    """
    from crm.store.store import Store

    excluded = set(book)
    store = Store(client=ctx.client("dynamodb"), table_prefix=_live_table_prefix(ctx))
    for index in range(len(excluded) + 1):
        candidate = f"acct-{tenant}-{index:04d}"
        if candidate in excluded:
            continue
        row = store.get_item(_ACCOUNT_TABLE, {"account_id": candidate})
        if row is not None and row.get("tenant") == tenant:
            return candidate
    raise LookupError(
        f"no seeded account of tenant {tenant!r} outside the token's book {sorted(excluded)!r} "
        "was found in DynamoDB"
    )


def _tenant_marker(tenant: str) -> str:
    """Return the write marker identifying ``tenant``, as it appears in every `transaction_key`.

    A digest of the whole tenant name, so the marker is specific to one tenant.
    """
    return f"smoke-{hashlib.sha256(tenant.encode()).hexdigest()[:8]}"


def _make_transaction_key(smoke_tenant: str, step: str) -> str:
    """Build a `transaction_key` unique per (tenant, step) and within DynamoDB's 36-character cap.

    `Store.transact_write` forwards this value as `ClientRequestToken`, which AWS rejects above 36
    characters, so the value is hashed.
    """
    digest = hashlib.sha256(f"{smoke_tenant}:{step}".encode()).hexdigest()[:20]
    return f"{_tenant_marker(smoke_tenant)}-{digest}"


def checks(  # noqa: C901, PLR0912, PLR0915 - one cohesive round trip; splitting hides shared state
    ctx: SmokeContext,
    *,
    http_caller: HttpCaller | None = None,
    token: str | None = None,
    account_id: str | None = None,
    out_of_book_account_id: str | None = None,
) -> Iterable[CheckResult]:
    """Yield one `CheckResult` per (step, check) pair for the create/read/update round trip.

    Never raises for an individual check failure; after a failed prerequisite step every later
    step reports a `skip` naming what failed.
    """
    if token is None:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="token_configured",
            status="fail",
            detail="no --token/CRM_SMOKE_TOKEN supplied",
        )
        return

    claims, unverified = verified_token_claims(ctx, token)
    claimed_tenant = str(claims.get(TENANT_CLAIM) or "")
    smoke_tenant = ctx.tenant or claimed_tenant
    refusal = unverified or _tenant_refusal(
        tenant=smoke_tenant, claimed_tenant=claimed_tenant, ctx_tenant=ctx.tenant
    )
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="token_tenant_is_disposable_smoke_tenant",
        status="fail" if refusal else "pass",
        detail=refusal or f"token {TENANT_CLAIM}={smoke_tenant!r}: a disposable eval smoke tenant",
    )
    if refusal:
        return
    if account_id is None:
        try:
            account_id = in_book_accounts(claims)[0]
        except ValueError as exc:
            yield CheckResult(
                resource=RESOURCE_CLASS,
                check="account_id_configured",
                status="fail",
                detail=(
                    "no --account-id/CRM_SMOKE_ACCOUNT_ID supplied and no in-book account could be "
                    f"derived to create the opportunity under: {exc}"
                ),
            )
            return
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="account_id_configured",
            status="pass",
            detail=f"in-book account {account_id!r} derived from the token's {CLAIM_BOOK} claim",
        )

    caller = http_caller if http_caller is not None else default_http_caller
    recorded_calls: list[tuple[str, str, str]] = []
    spying_caller = _spying_caller(caller, recorded_calls)
    cloudfront_domain_name = ctx.outputs.require_str("cloudfront_domain_name")

    create_tool = BY_NAME["create_opportunity"]
    read_tool = BY_NAME["get_opportunity"]
    update_tool = BY_NAME["update_opportunity"]

    # -- create -----------------------------------------------------------------------------
    create_transaction_key = _make_transaction_key(smoke_tenant, "create")
    create_result = _call_tool(
        spying_caller,
        cloudfront_domain_name,
        create_tool.name,
        token,
        {
            "account_id": account_id,
            "name": "Layer 5 smoke opportunity",
            "transaction_key": create_transaction_key,
        },
    )
    opportunity_id: str | None = None
    if isinstance(create_result, str):
        yield CheckResult(resource="create", check="http_call", status="fail", detail=create_result)
    else:
        status_code, response_body, _headers = create_result
        if status_code >= 400:
            yield CheckResult(
                resource="create",
                check="http_call",
                status="fail",
                detail=f"status={status_code} body={response_body}",
            )
        else:
            yield CheckResult(
                resource="create", check="http_call", status="pass", detail=f"status={status_code}"
            )
            parsed = _parse_json_body(response_body)
            opportunity = (parsed or {}).get("opportunity")
            if isinstance(opportunity, dict) and isinstance(opportunity.get("opportunity_id"), str):
                opportunity_id = opportunity["opportunity_id"]
            else:
                yield CheckResult(
                    resource="create",
                    check="response_has_opportunity_id",
                    status="fail",
                    detail=f"response body did not carry an opportunity id: {response_body}",
                )

    if opportunity_id is None:
        yield CheckResult(
            resource="create",
            check="ddb_row_matches",
            status="skip",
            detail="create did not yield an opportunity_id; see create:http_call",
        )
        yield CheckResult(
            resource="read",
            check="http_call",
            status="skip",
            detail="prerequisite failed: create did not yield an opportunity_id",
        )
        yield CheckResult(
            resource="update",
            check="http_call",
            status="skip",
            detail="prerequisite failed: create did not yield an opportunity_id",
        )
        yield from _writes_only_under_smoke_tenant_check(recorded_calls, smoke_tenant)
        return

    created_row = _read_opportunity_row(ctx, opportunity_id)
    yield _row_matches_check(
        resource="create",
        row=created_row,
        smoke_tenant=smoke_tenant,
        expected_fields={"account_id": account_id, "name": "Layer 5 smoke opportunity"},
        require_audit=False,
    )

    # -- read -------------------------------------------------------------------------------
    read_arguments = {"opportunity_id": opportunity_id}
    read_result = _call_tool(
        spying_caller, cloudfront_domain_name, read_tool.name, token, read_arguments
    )
    if isinstance(read_result, str):
        yield CheckResult(resource="read", check="http_call", status="fail", detail=read_result)
        read_ok = False
        read_headers: Mapping[str, str] = {}
    else:
        status_code, response_body, read_headers = read_result
        read_ok = status_code < 400
        yield CheckResult(
            resource="read",
            check="http_call",
            status="pass" if read_ok else "fail",
            detail=f"status={status_code}",
        )

    if not read_ok:
        yield CheckResult(
            resource="read",
            check="ddb_row_matches",
            status="skip",
            detail="prerequisite failed: read:http_call did not succeed",
        )
        yield CheckResult(
            resource="update",
            check="http_call",
            status="skip",
            detail="prerequisite failed: read:http_call did not succeed",
        )
        yield from _writes_only_under_smoke_tenant_check(recorded_calls, smoke_tenant)
        return

    reread_row = _read_opportunity_row(ctx, opportunity_id)
    yield _row_matches_check(
        resource="read",
        row=reread_row,
        smoke_tenant=smoke_tenant,
        expected_fields={"account_id": account_id, "name": "Layer 5 smoke opportunity"},
        require_audit=False,
    )

    # -- the CloudFront hop, asserted behaviourally ------------------------------------------
    yield from _cloudfront_behaviour_checks(
        spying_caller,
        cloudfront_domain_name,
        token=token,
        tool_name=read_tool.name,
        arguments=read_arguments,
        first_response_headers=read_headers,
    )

    # -- update -----------------------------------------------------------------------------
    update_transaction_key = _make_transaction_key(smoke_tenant, "update")
    updated_name = "Layer 5 smoke opportunity (updated)"
    update_result = _call_tool(
        spying_caller,
        cloudfront_domain_name,
        update_tool.name,
        token,
        {
            "opportunity_id": opportunity_id,
            "fields": {"name": updated_name},
            "transaction_key": update_transaction_key,
        },
    )
    if isinstance(update_result, str):
        yield CheckResult(resource="update", check="http_call", status="fail", detail=update_result)
        update_ok = False
    else:
        status_code, response_body, _headers = update_result
        update_ok = status_code < 400
        yield CheckResult(
            resource="update",
            check="http_call",
            status="pass" if update_ok else "fail",
            detail=f"status={status_code}"
            + ("" if update_ok else f" body={response_body[:300]!r}"),
        )

    if not update_ok:
        yield CheckResult(
            resource="update",
            check="ddb_row_matches",
            status="skip",
            detail="prerequisite failed: update:http_call did not succeed",
        )
    else:
        updated_row = _read_opportunity_row(ctx, opportunity_id)
        yield _row_matches_check(
            resource="update",
            row=updated_row,
            smoke_tenant=smoke_tenant,
            expected_fields={"account_id": account_id, "name": updated_name},
            require_audit=True,
        )

    # -- out-of-book denial -------------------------------------------------------------------
    derivation_failure: str | None = None
    if out_of_book_account_id is None:
        try:
            out_of_book_account_id = _seeded_account_outside_book(
                ctx, smoke_tenant, [*in_book_accounts(claims), account_id]
            )
        except (ValueError, LookupError, CrmError) as exc:
            derivation_failure = f"{type(exc).__name__}: {exc}"
    if out_of_book_account_id is None:
        yield CheckResult(
            resource="denial",
            check="out_of_book_refused_403",
            status="fail",
            detail=(
                "no out-of-book account_id supplied or derived "
                f"({derivation_failure}); the 403 refusal is this check's security "
                "assertion and an unobserved authorization boundary is not a passing one"
            ),
        )
    else:
        yield from _out_of_book_denial_check(
            spying_caller, cloudfront_domain_name, token, out_of_book_account_id
        )

    yield from _writes_only_under_smoke_tenant_check(recorded_calls, smoke_tenant)


def _row_matches_check(
    *,
    resource: str,
    row: dict[str, Any] | None,
    smoke_tenant: str,
    expected_fields: Mapping[str, Any],
    require_audit: bool,
) -> CheckResult:
    """Assert the DynamoDB row read back matches what the API step claims, including the tenant
    prefix (`tenant`, and every `tenant_*` composite attribute) and, for a mutated row, the audit
    attributes a committed write leaves behind (`updated_at`, `created_by`).
    """
    if row is None:
        return CheckResult(
            resource=resource,
            check="ddb_row_matches",
            status="fail",
            detail="no row found in DynamoDB at the claimed opportunity_id",
        )

    mismatches: list[str] = []
    if row.get("tenant") != smoke_tenant:
        mismatches.append(f"tenant={row.get('tenant')!r} expected {smoke_tenant!r}")
    for tenant_prefixed_attr in ("tenant_account_id", "tenant_owner_rep_id", "tenant_stage"):
        value = row.get(tenant_prefixed_attr)
        if not (isinstance(value, str) and value.startswith(f"{smoke_tenant}#")):
            mismatches.append(f"{tenant_prefixed_attr}={value!r} does not carry tenant prefix")
    for field, expected_value in expected_fields.items():
        if row.get(field) != expected_value:
            mismatches.append(f"{field}={row.get(field)!r} expected {expected_value!r}")
    if require_audit:
        if not row.get("updated_at"):
            mismatches.append("missing audit attribute updated_at")
        if not row.get("created_by"):
            mismatches.append("missing audit attribute created_by")

    if mismatches:
        return CheckResult(
            resource=resource, check="ddb_row_matches", status="fail", detail="; ".join(mismatches)
        )
    return CheckResult(
        resource=resource,
        check="ddb_row_matches",
        status="pass",
        detail="persisted row matches the API's claim, including tenant prefix",
    )


def _cloudfront_behaviour_checks(
    caller: HttpCaller,
    cloudfront_domain_name: str,
    *,
    token: str,
    tool_name: str,
    arguments: dict[str, Any],
    first_response_headers: Mapping[str, str],
) -> Iterable[CheckResult]:
    """Assert the `/api/*` behaviour forwards `Authorization` and caches no authenticated response.

    * `authorization_forwarded` - the identical read that answered `200` with the header must
      answer `401` without it.
    * `authenticated_response_not_cached` - both identical authenticated reads must report an
      `X-Cache` miss.
    """
    unauthenticated = _call_tool(caller, cloudfront_domain_name, tool_name, None, arguments)
    if isinstance(unauthenticated, str):
        yield CheckResult(
            resource="cloudfront",
            check="authorization_forwarded",
            status="fail",
            detail=unauthenticated,
        )
    else:
        status_code, body, _headers = unauthenticated
        if status_code == 401:
            yield CheckResult(
                resource="cloudfront",
                check="authorization_forwarded",
                status="pass",
                detail=(
                    "the same read answers 200 with the Authorization header and 401 without it, "
                    "so CloudFront forwarded it to the origin authorizer"
                ),
            )
        else:
            yield CheckResult(
                resource="cloudfront",
                check="authorization_forwarded",
                status="fail",
                detail=(
                    f"expected 401 for the same read without the Authorization header, got "
                    f"{status_code}: body={body[:200]!r}"
                ),
            )

    second = _call_tool(caller, cloudfront_domain_name, tool_name, token, arguments)
    if isinstance(second, str):
        yield CheckResult(
            resource="cloudfront",
            check="authenticated_response_not_cached",
            status="fail",
            detail=second,
        )
        return
    _status, _body, second_headers = second
    dispositions = [
        first_response_headers.get(_CACHE_HEADER, ""),
        second_headers.get(_CACHE_HEADER, ""),
    ]
    if any("hit" in disposition.lower() for disposition in dispositions):
        yield CheckResult(
            resource="cloudfront",
            check="authenticated_response_not_cached",
            status="fail",
            detail=(
                f"an authenticated response was served from cache: {_CACHE_HEADER} "
                f"dispositions={dispositions!r}"
            ),
        )
        return
    if not all(dispositions):
        yield CheckResult(
            resource="cloudfront",
            check="authenticated_response_not_cached",
            status="fail",
            detail=(
                f"no {_CACHE_HEADER} header on one or both authenticated reads "
                f"({dispositions!r}); caching cannot be observed, so it is not proven disabled"
            ),
        )
        return
    yield CheckResult(
        resource="cloudfront",
        check="authenticated_response_not_cached",
        status="pass",
        detail=f"two identical authenticated reads, {_CACHE_HEADER} dispositions={dispositions!r}",
    )


def _out_of_book_denial_check(
    caller: HttpCaller, cloudfront_domain_name: str, token: str, out_of_book_account_id: str
) -> Iterable[CheckResult]:
    """Assert an out-of-book target is refused with `403` specifically through the full path.

    A `200` is a failure. A `404` is reported as a distinct failure from a `403` denial.
    """
    result = _call_tool(
        caller,
        cloudfront_domain_name,
        BY_NAME["get_account"].name,
        token,
        {"account_id": out_of_book_account_id},
    )
    if isinstance(result, str):
        yield CheckResult(
            resource="denial", check="out_of_book_refused_403", status="fail", detail=result
        )
        return

    status_code, response_body, _headers = result
    if status_code == 403:
        yield CheckResult(
            resource="denial",
            check="out_of_book_refused_403",
            status="pass",
            detail=(
                f"403 as expected for out-of-book account {out_of_book_account_id!r}: "
                f"body={response_body[:200]!r}"
            ),
        )
    elif status_code == 404:
        yield CheckResult(
            resource="denial",
            check="out_of_book_refused_403",
            status="fail",
            detail=(
                "got 404, not 403 - an authorization denial must not surface as a not-found "
                f"response: body={response_body}"
            ),
        )
    elif status_code == 200:
        yield CheckResult(
            resource="denial",
            check="out_of_book_refused_403",
            status="fail",
            detail=f"got 200 - out-of-book target was NOT refused: body={response_body}",
        )
    else:
        yield CheckResult(
            resource="denial",
            check="out_of_book_refused_403",
            status="fail",
            detail=f"expected 403, got {status_code}: body={response_body}",
        )


def _writes_only_under_smoke_tenant_check(
    recorded_calls: list[tuple[str, str, str]], smoke_tenant: str
) -> Iterable[CheckResult]:
    """Assert every recorded write carries the smoke tenant marker and never `demo`.

    Over the spy's recorded calls: every call body carrying a `transaction_key` (the write marker,
    since every tool call is a `POST`) must carry :func:`_tenant_marker`'s digest of the smoke
    tenant, and never `demo`'s.
    """
    calls_with_transaction_keys = [
        (method, url, body)
        for method, url, body in recorded_calls
        if (_parse_json_body(body) or {}).get("transaction_key")
    ]
    if not calls_with_transaction_keys:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="writes_only_under_smoke_tenant",
            status="skip",
            detail="no write calls were issued",
        )
        return

    violations: list[str] = []
    demo_marker = f"{_tenant_marker(DEMO_TENANT)}-"
    smoke_marker = f"{_tenant_marker(smoke_tenant)}-"
    for _method, url, body in calls_with_transaction_keys:
        parsed = _parse_json_body(body) or {}
        transaction_key = str(parsed.get("transaction_key") or "")
        if demo_marker in transaction_key:
            violations.append(f"url={url} transaction_key={transaction_key!r} carries demo marker")
        elif not transaction_key.startswith(smoke_marker):
            violations.append(
                f"url={url} transaction_key={transaction_key!r} does not carry the smoke tenant"
            )

    if violations:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="writes_only_under_smoke_tenant",
            status="fail",
            detail="; ".join(violations),
        )
    else:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="writes_only_under_smoke_tenant",
            status="pass",
            detail=f"{len(calls_with_transaction_keys)} write call(s) all carried the smoke tenant",
        )


def _account_id_from_env_or_flag(argv: Sequence[str] | None, flag: str, env_var: str) -> str | None:
    """Resolve an account id: ``flag`` -> ``env_var`` -> ``None``.

    Same `argv`-scanning convention as :func:`_token_from_env_or_flag`. `--account-id` and
    `--out-of-book-account-id` are declared to `run_main` via `extra_arguments`.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return os.environ.get(env_var)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: resolve the token and account ids from flags/env, then run the checks."""
    token = _token_from_env_or_flag(argv)
    account_id = _account_id_from_env_or_flag(argv, "--account-id", "CRM_SMOKE_ACCOUNT_ID")
    out_of_book_account_id = _account_id_from_env_or_flag(
        argv, "--out-of-book-account-id", "CRM_SMOKE_OUT_OF_BOOK_ACCOUNT_ID"
    )
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=lambda ctx: checks(
            ctx,
            token=token,
            account_id=account_id,
            out_of_book_account_id=out_of_book_account_id,
        ),
        extra_arguments=["--token", "--account-id", "--out-of-book-account-id"],
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
