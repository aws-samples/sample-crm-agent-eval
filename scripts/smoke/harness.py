# SPDX-License-Identifier: MIT
"""Shared harness for Layer 4-6 post-deploy smoke scripts.

Provides Terraform output loading, an injectable AWS client seam for offline tests, the
`--json` emitter, the https-only `open_https` every smoke HTTP call goes through, and a fail-fast
layer runner: every step within a layer runs even if an earlier step in that layer failed, but no
step of a later layer runs once any step in an earlier layer has failed.

The inventory *contract* it emits — `CheckResult`, `Provenance`, `ResourceClassResult`, the
four-status vocabulary and the `fail`-only exit-code rule — is defined in
`src/eval/verification.py` and re-exported below. Read that module for what each status means.

Every inventory emitted through `run_main` carries a `provenance` block naming the UTC
instant of the run, the git commit it was produced from and whether that tree was dirty,
the AWS region, and the resource class. It records no AWS account id.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final, cast

import boto3

from eval.verification import (
    COVERAGE_CHECK as COVERAGE_CHECK,
)

# The verification-inventory contract is re-exported from `src/`; nothing is re-defined here. The
# `X as X` form is the explicit re-export `mypy --strict` requires.
from eval.verification import (
    DEFAULT_PROFILE as DEFAULT_PROFILE,
)
from eval.verification import (
    DEFAULT_REGION as DEFAULT_REGION,
)
from eval.verification import (
    DEFER as DEFER,
)
from eval.verification import (
    UNKNOWN as UNKNOWN,
)
from eval.verification import (
    CheckResult as CheckResult,
)
from eval.verification import (
    ExpectedCheckCount as ExpectedCheckCount,
)
from eval.verification import (
    Provenance as Provenance,
)
from eval.verification import (
    ResourceClassResult as ResourceClassResult,
)
from eval.verification import (
    Status as Status,
)
from eval.verification import (
    UnderivableCheckCount as UnderivableCheckCount,
)
from eval.verification import (
    coverage_check as coverage_check,
)
from eval.verification import (
    git_commit as git_commit,
)
from eval.verification import (
    git_tree_dirty as git_tree_dirty,
)
from eval.verification import (
    resolve_profile as resolve_profile,
)
from eval.verification import (
    resolve_region as resolve_region,
)
from eval.verification import (
    utc_now_iso as utc_now_iso,
)

#: Path default for `--outputs`.
DEFAULT_OUTPUTS_PATH: Final[str] = "results/tf-outputs.json"

# ---------------------------------------------------------------------------
# Bounded time and an asserted target.
#
# Inside the VPC (no NAT, no internet gateway) an unreachable endpoint produces a timeout rather
# than a refusal, so every client and check is time-bounded. `WrongTargetError` refuses a run whose
# region or account is not the one the outputs describe.
# ---------------------------------------------------------------------------

#: Per-connection and per-response ceilings, in seconds, for every AWS client this harness builds.
SMOKE_CONNECT_TIMEOUT_SECONDS: Final[int] = 5
SMOKE_READ_TIMEOUT_SECONDS: Final[int] = 20

#: Attempts, not retries: botocore counts the first call. Used with `standard` retry mode.
SMOKE_MAX_ATTEMPTS: Final[int] = 3

#: Services whose clients use `adaptive` retry mode with `SMOKE_THROTTLED_MAX_ATTEMPTS`: IAM's
#: SimulatePrincipalPolicy throttles under the iam_roles matrix.
SMOKE_THROTTLED_SERVICES: Final[frozenset[str]] = frozenset({"iam"})
SMOKE_THROTTLED_MAX_ATTEMPTS: Final[int] = 10

#: Default wall-clock ceiling for one check, including its retries.
SMOKE_CHECK_TIMEOUT_SECONDS: Final[float] = 60.0

#: Machine-readable marker opening the `detail` of a check that ran out of time. A timeout is a
#: `fail`; the marker distinguishes it from an assertion failure.
TIMEOUT_DETAIL_PREFIX: Final[str] = "TIMEOUT after "

#: The one AWS error code a smoke check may retry on its own, matched exactly.
#:
#: `ENINotReadyException` means the Lambda function's VPC network interface is still being
#: attached, a transient condition on the first invocation after an apply.
RETRYABLE_ERROR_CODES: Final[frozenset[str]] = frozenset({"ENINotReadyException"})

#: Botocore exception type names that mean "no answer came back in time".
#:
#: Matched by type name rather than by importing them from botocore.
_TIMEOUT_EXCEPTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "EndpointConnectionError",
    }
)


class UninjectedAwsClientError(RuntimeError):
    """A test-built context asked for an AWS client no fake was supplied for.

    An offline context fails closed here rather than falling through to a real `boto3` client for an
    un-injected service.
    """


class MissingTerraformOutputError(RuntimeError):
    """Raised when a required Terraform output is absent.

    The message names the missing output and lists the outputs that are available.
    """

    def __init__(self, name: str, available: Iterable[str]) -> None:
        available_list = sorted(available)
        super().__init__(
            f"missing required Terraform output {name!r}; available outputs: {available_list}"
        )
        self.name = name
        self.available = tuple(available_list)


@dataclass(frozen=True)
class TerraformOutputs:
    """Read-only access to loaded Terraform outputs."""

    values: Mapping[str, Any]

    def require(self, name: str) -> Any:
        """Return the value for `name`, raising `MissingTerraformOutputError` if absent."""
        if name not in self.values:
            raise MissingTerraformOutputError(name, self.values.keys())
        return self.values[name]

    def require_str(self, name: str) -> str:
        """Return the value for `name` as a `str`."""
        value = self.require(name)
        return str(value)

    def require_mapping(self, name: str) -> Mapping[str, Any]:
        """Return the value for `name` as a `Mapping`."""
        value = self.require(name)
        if not isinstance(value, Mapping):
            raise MissingTerraformOutputError(name, self.values.keys())
        return value

    def get(self, name: str, default: Any = None) -> Any:
        """Return the value for `name`, or `default` if absent."""
        return self.values.get(name, default)


def load_outputs(path: str | Path) -> TerraformOutputs:
    """Load Terraform outputs from a JSON file at `path`.

    Accepts both the `terraform output -json` shape (`{"name": {"value": v, "type": t}}`)
    and an already-flattened `{"name": v}` mapping. Never shells out and never calls AWS.
    Raises `RuntimeError` with a clear message on a missing file or malformed JSON.
    """
    output_path = Path(path)
    if not output_path.exists():
        raise RuntimeError(f"Terraform outputs file not found: {output_path}")

    try:
        raw = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Terraform outputs file is not valid JSON: {output_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"Terraform outputs file must contain a JSON object: {output_path}")

    flattened: dict[str, Any] = {}
    for name, entry in raw.items():
        if isinstance(entry, Mapping) and "value" in entry:
            flattened[name] = entry["value"]
        else:
            flattened[name] = entry
    return TerraformOutputs(values=flattened)


def outputs_path(flag: str | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Resolve the Terraform outputs path: flag -> `CRM_TF_OUTPUTS` -> default path."""
    if flag:
        return Path(flag)
    environment = env if env is not None else os.environ
    return Path(environment.get("CRM_TF_OUTPUTS") or DEFAULT_OUTPUTS_PATH)


@dataclass
class SmokeContext:
    """Runtime context threaded through a smoke script's checks."""

    profile: str
    region: str
    tenant: str | None
    as_json: bool
    outputs: TerraformOutputs
    _injected_clients: Mapping[str, Any] = field(default_factory=dict)
    #: True only for contexts built by `context_for_test`.
    _offline_only: bool = False
    _client_cache: dict[str, Any] = field(default_factory=dict)
    _session: Any = None

    def client(self, service_name: str) -> Any:
        """Return the AWS client for `service_name`.

        Returns an injected client when one was supplied via `context_for_test`,
        otherwise lazily builds one from a `boto3.session.Session` and caches it.
        """
        if service_name in self._injected_clients:
            return self._injected_clients[service_name]
        # Fires only where a real session would be constructed; an injected `_session` is allowed.
        if self._offline_only and self._session is None:
            raise UninjectedAwsClientError(
                f"no fake was injected for the {service_name!r} client, and this context was built by "
                "`context_for_test`, so it must not reach AWS. Pass "
                f'`clients={{..., "{service_name}": <fake>}}` to `context_for_test`. Falling through '
                "to a real client would make an offline test read live state with "
                "AdministratorAccess credentials."
            )
        if service_name not in self._client_cache:
            if self._session is None:
                self._session = boto3.session.Session(
                    profile_name=self.profile, region_name=self.region
                )
            # Every smoke module's clients are built here, so the bounded config applies to all.
            self._client_cache[service_name] = self._session.client(
                service_name, region_name=self.region, config=smoke_client_config(service_name)
            )
        return self._client_cache[service_name]


def smoke_client_config(service_name: str | None = None) -> Any:
    """The bounded `botocore.config.Config` every smoke client is built with.

    A `service_name` in `SMOKE_THROTTLED_SERVICES` gets `adaptive` retries with
    `SMOKE_THROTTLED_MAX_ATTEMPTS`; every other client gets `standard` with `SMOKE_MAX_ATTEMPTS`.
    A function rather than a module constant so importing this module does not import botocore.
    """
    from botocore.config import Config

    retries = (
        {"max_attempts": SMOKE_THROTTLED_MAX_ATTEMPTS, "mode": "adaptive"}
        if service_name in SMOKE_THROTTLED_SERVICES
        else {"max_attempts": SMOKE_MAX_ATTEMPTS, "mode": "standard"}
    )
    return Config(
        connect_timeout=SMOKE_CONNECT_TIMEOUT_SECONDS,
        read_timeout=SMOKE_READ_TIMEOUT_SECONDS,
        retries=retries,
        tcp_keepalive=True,
    )


def error_code(exc: BaseException) -> str:
    """The AWS error code on a `ClientError`, or `""` for anything else.

    A stubbed or malformed error resolves to `""` rather than raising.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return ""
    error = response.get("Error")
    if not isinstance(error, Mapping):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


def is_timeout(exc: BaseException) -> bool:
    """Whether `exc` means no answer came back in time, rather than an answer we did not like."""
    return type(exc).__name__ in _TIMEOUT_EXCEPTION_NAMES


# ---------------------------------------------------------------------------
# HTTPS-only transport for smoke HTTP calls (bandit rule B310).
# ---------------------------------------------------------------------------


class InsecureSchemeError(ValueError):
    """A smoke HTTP request named a scheme other than `https`; raised before any network I/O."""


def https_only_opener() -> urllib.request.OpenerDirector:
    """Build an opener whose only transport is HTTPS.

    It has no `http`, `file`, `ftp` or `data` handler, so a redirect to any of those schemes fails
    with `urllib.error.URLError` ("unknown url type") instead of being followed. Proxy, redirect
    and HTTP-error handling match `urllib.request.urlopen`'s defaults.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPSHandler(),
    ):
        opener.add_handler(handler)
    return opener


_HTTPS_OPENER: Final[urllib.request.OpenerDirector] = https_only_opener()


def open_https(request: urllib.request.Request, *, timeout: float) -> http.client.HTTPResponse:
    """Send `request` over HTTPS and return the response, as `urllib.request.urlopen` would.

    Raises:
        InsecureSchemeError: `request`'s scheme is not `https`; nothing was sent.
        urllib.error.HTTPError: The server answered with an error status.
        urllib.error.URLError: No response was received, or a redirect left HTTPS.
    """
    if request.type != "https":
        raise InsecureSchemeError(
            f"refusing to open {request.type!r} URL {request.full_url!r}: smoke HTTP calls are "
            "https-only"
        )
    return cast(http.client.HTTPResponse, _HTTPS_OPENER.open(request, timeout=timeout))


class SmokeTimeoutError(RuntimeError):
    """A check exceeded its wall-clock ceiling. Carries the elapsed seconds for the report."""

    def __init__(self, elapsed_seconds: float, detail: str = "") -> None:
        self.elapsed_seconds = elapsed_seconds
        self.detail = detail
        super().__init__(f"{TIMEOUT_DETAIL_PREFIX}{elapsed_seconds:.1f}s: {detail}".rstrip(": "))


@dataclass
class Deadline:
    """A monotonic wall-clock ceiling for one check, including its retries.

    Measured with `time.monotonic`, so a wall-clock adjustment cannot move it.
    """

    seconds: float
    _started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    def check(self, detail: str = "") -> None:
        """Raise `SmokeTimeoutError` when the ceiling has passed. Call at the top of every poll iteration."""
        if self.expired:
            raise SmokeTimeoutError(self.elapsed, detail)


def call_with_eni_retry(
    operation: Callable[[], Any],
    *,
    deadline: Deadline,
    sleeper: Callable[[float], None] | None = None,
    backoff_seconds: float = 2.0,
) -> Any:
    """Call `operation`, retrying **only** on `ENINotReadyException`, within `deadline`.

    Every other exception propagates on the first attempt, unretried. Bounded by the deadline and by
    `SMOKE_MAX_ATTEMPTS`.
    """
    sleep = sleeper if sleeper is not None else time.sleep
    for attempt in range(1, SMOKE_MAX_ATTEMPTS + 1):
        deadline.check(f"before attempt {attempt}")
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is the one retryable code
            if error_code(exc) not in RETRYABLE_ERROR_CODES or attempt == SMOKE_MAX_ATTEMPTS:
                raise
            if deadline.remaining <= 0.0:
                raise SmokeTimeoutError(
                    deadline.elapsed, f"{error_code(exc)} after {attempt} attempt(s)"
                ) from exc
            sleep(min(backoff_seconds * attempt, deadline.remaining))
    raise AssertionError("unreachable: the loop either returns or raises")


def timed_check(
    resource: str,
    check: str,
    operation: Callable[[Deadline], CheckResult],
    *,
    timeout_seconds: float = SMOKE_CHECK_TIMEOUT_SECONDS,
) -> CheckResult:
    """Run one check under a wall-clock ceiling, reporting a timeout as a distinguishable failure.

    A timeout is `fail`, marked with `TIMEOUT_DETAIL_PREFIX` and its elapsed time.

    `operation` receives the `Deadline` so a poll loop inside it can call `deadline.check()` per
    iteration; a botocore timeout raised beneath it is classified the same way.
    """
    deadline = Deadline(seconds=timeout_seconds)
    try:
        return operation(deadline)
    except SmokeTimeoutError as exc:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"{TIMEOUT_DETAIL_PREFIX}{exc.elapsed_seconds:.1f}s (ceiling {timeout_seconds:.0f}s)"
                f"{': ' + exc.detail if exc.detail else ''}. Nothing was verified. Inside the VPC an "
                "unreachable endpoint returns nothing rather than an error, so check for a missing "
                "interface endpoint, a security-group egress rule, or a subnet route before "
                "suspecting the resource itself."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - every failure becomes a reported result, not a crash
        if not is_timeout(exc):
            raise
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"{TIMEOUT_DETAIL_PREFIX}{deadline.elapsed:.1f}s: the AWS client timed out "
                f"({type(exc).__name__}). Nothing was verified — this is reachability, not "
                "configuration."
            ),
        )


def build_parser(description: str) -> argparse.ArgumentParser:
    """Return an `ArgumentParser` with the standard smoke-script flags."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--profile", default=None, help="AWS profile (default: resolved order)")
    parser.add_argument("--region", default=None, help="AWS region (default: resolved order)")
    parser.add_argument("--tenant", default=None, help="Smoke-run tenant id for writing checks")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON to stdout")
    parser.add_argument(
        "--outputs", default=None, help="Path to terraform output -json (default: resolved order)"
    )
    return parser


class WrongTargetError(RuntimeError):
    """The resolved region is not the region the Terraform outputs describe.

    Raised before any check runs. Also raised for an account mismatch.
    """


#: ARN region and account fields. The region group may be empty: `iam` and `cloudfront` ARNs carry
#: no region.
_ARN_FIELDS = re.compile(r"arn:aws[a-z0-9-]*:[a-z0-9-]+:([a-z0-9-]*):(\d{12}):")


def outputs_regions(outputs: TerraformOutputs) -> frozenset[str]:
    """Every non-empty region named by an ARN anywhere in the outputs.

    The expected region is derived from the outputs file rather than configured.
    """
    found: set[str] = set()
    for region, _account in _ARN_FIELDS.findall(json.dumps(outputs.values, default=str)):
        if region:
            found.add(region)
    return frozenset(found)


def outputs_accounts(outputs: TerraformOutputs) -> frozenset[str]:
    """Every account id named by an ARN in the outputs.

    Returned for comparison; `assert_account_matches_outputs` names both ids on a mismatch.
    """
    return frozenset(
        account
        for _region, account in _ARN_FIELDS.findall(json.dumps(outputs.values, default=str))
        if account
    )


def assert_region_matches_outputs(region: str, outputs: TerraformOutputs) -> None:
    """Refuse to proceed when `region` is not a region the outputs describe.

    Silent on an outputs file carrying no regional ARN. The account half is
    :func:`assert_account_matches_outputs`.
    """
    regions = outputs_regions(outputs)
    if not regions or region in regions:
        return
    raise WrongTargetError(
        f"resolved region {region!r}, but this deployment's outputs describe "
        f"{', '.join(repr(name) for name in sorted(regions))}. Nothing has been checked. Region "
        f"resolves flag -> AWS_REGION -> default, so an unset `--region` takes AWS_REGION from your "
        f"environment: pass `--region {sorted(regions)[0]}` explicitly, or unset AWS_REGION. "
        f"Proceeding would exercise a different deployment and report the results as if they were "
        f"this one's."
    )


def assert_account_matches_outputs(ctx: SmokeContext) -> None:
    """Refuse to proceed when the caller's account is not the outputs' account.

    Makes a live `sts:GetCallerIdentity` call, so it is called from a live entry point rather than
    during context construction. The message names both account ids.
    """
    expected = outputs_accounts(ctx.outputs)
    if not expected:
        return
    identity = ctx.client("sts").get_caller_identity()
    actual = str(identity.get("Account", ""))
    if actual and actual not in expected:
        raise WrongTargetError(
            f"resolved account {actual} in region {ctx.region!r} via profile {ctx.profile!r}, but "
            f"this deployment's outputs describe account "
            f"{', '.join(sorted(expected))}. Nothing has been checked. Profile resolves flag -> "
            f"AWS_PROFILE -> default, so an unset `--profile` takes AWS_PROFILE from your "
            f"environment: pass `--profile` explicitly, or unset AWS_PROFILE."
        )


#: Prefix on every stderr line reporting a target half that was not asserted.
TARGET_UNASSERTED_WARNING_PREFIX: Final[str] = "warning: NO TARGET ASSERTION WAS MADE - "


def assert_target_matches_outputs(ctx: SmokeContext) -> tuple[str, ...]:
    """Assert both halves of the target -- region *and* account -- before any check runs.

    Raises `WrongTargetError` on a mismatch of either half. Returns one warning line per half that could
    not be asserted at all; an empty tuple means both were checked and both matched. The caller is
    responsible for emitting the warnings, which `run_main` does on stderr.

    Called from `run_main`. A failure of the `sts:GetCallerIdentity` call itself is a warning rather
    than a refusal.
    """
    unasserted: list[str] = []

    if outputs_regions(ctx.outputs):
        assert_region_matches_outputs(ctx.region, ctx.outputs)
    else:
        unasserted.append(
            f"{TARGET_UNASSERTED_WARNING_PREFIX}the outputs name no region, so nothing verified that "
            f"region {ctx.region!r} belongs to this deployment. Proceeding."
        )

    if not outputs_accounts(ctx.outputs):
        unasserted.append(
            f"{TARGET_UNASSERTED_WARNING_PREFIX}the outputs name no account, so nothing verified that "
            f"profile {ctx.profile!r} resolves to the account you intend to read. Proceeding."
        )
        return tuple(unasserted)

    try:
        assert_account_matches_outputs(ctx)
    except WrongTargetError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unusable credential is a warning, not a wrong target
        unasserted.append(
            f"{TARGET_UNASSERTED_WARNING_PREFIX}the account half could not be checked "
            f"({type(exc).__name__}: {exc}), so nothing verified which account this run reads. "
            "Proceeding: a credential failure is not a wrong target."
        )
    return tuple(unasserted)


def context_from_args(args: argparse.Namespace) -> SmokeContext:
    """Build a `SmokeContext` from parsed CLI args, resolving profile/region/outputs.

    Asserts the resolved region against the outputs before returning, so a run redirected by an
    ambient `AWS_REGION` fails here. `context_for_test` does not assert.
    """
    profile = resolve_profile(args.profile)
    region = resolve_region(args.region)
    path = outputs_path(args.outputs)
    outputs = load_outputs(path)
    assert_region_matches_outputs(region, outputs)
    return SmokeContext(
        profile=profile,
        region=region,
        tenant=args.tenant,
        as_json=args.as_json,
        outputs=outputs,
    )


def smoke_context(
    *,
    profile: str | None,
    region: str | None,
    outputs: str | Path | None = None,
    tenant: str | None = None,
) -> SmokeContext:
    """Build an asserted `SmokeContext` from plain values rather than an `argparse.Namespace`.

    The seam for callers that are not a smoke module's `main` — notably the inline `python -c`
    snippets in `scripts/verify_deployment.sh`, which provision and tear down the smoke tenant.
    Resolution order and the assertion are `context_from_args`'s.
    """
    resolved_region = resolve_region(region)
    loaded = load_outputs(outputs_path(str(outputs) if outputs is not None else None))
    assert_region_matches_outputs(resolved_region, loaded)
    return SmokeContext(
        profile=resolve_profile(profile),
        region=resolved_region,
        tenant=tenant,
        as_json=False,
        outputs=loaded,
    )


def context_for_test(
    *,
    outputs: Mapping[str, Any] | None = None,
    clients: Mapping[str, Any] | None = None,
    tenant: str | None = None,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
    as_json: bool = False,
) -> SmokeContext:
    """Build a `SmokeContext` for offline tests, injecting outputs and/or AWS clients."""
    return SmokeContext(
        profile=profile,
        region=region,
        tenant=tenant,
        as_json=as_json,
        outputs=TerraformOutputs(values=dict(outputs or {})),
        _injected_clients=dict(clients or {}),
        _offline_only=True,
    )


def emit(result: ResourceClassResult, *, as_json: bool, stream: IO[str] | None = None) -> None:
    """Write `result` to `stream` (default `sys.stdout`).

    `as_json=True` writes the pinned JSON object; otherwise a human-readable line per check.
    """
    out = stream if stream is not None else sys.stdout
    if as_json:
        json.dump(result.to_json_dict(), out)
        out.write("\n")
        return
    for check in result.checks:
        out.write(f"[{result.resource_class}] {check.resource}: {check.check} = {check.status}")
        if check.status == DEFER:
            out.write(f" [reason={check.defer_reason}]")
        if check.detail:
            out.write(f" ({check.detail})")
        out.write("\n")


CheckFn = Callable[[SmokeContext], Iterable[CheckResult]]


#: Derives the minimum check count for a resource class from the same context its checks read.
ExpectationFn = Callable[[SmokeContext], ExpectedCheckCount]


def _resolve_expectation(
    ctx: SmokeContext, expected: ExpectationFn | None
) -> ExpectedCheckCount | UnderivableCheckCount | str:
    """Return the derived expectation, or a stated reason none is available.

    An absent deriver means the count is compared against 1. A deriver that raises leaves the bound
    unknown, which `coverage_check` reports as a `fail`.
    """
    if expected is None:
        return "this resource class declares no expected_check_count deriver"
    try:
        return expected(ctx)
    except Exception as exc:  # noqa: BLE001 - reported in the check's detail, never swallowed
        return UnderivableCheckCount(
            reason=f"its expected_check_count deriver raised {type(exc).__name__}: {exc}"
        )


def run_main(
    argv: Sequence[str] | None,
    *,
    layer: int,
    resource_class: str,
    description: str,
    checks: CheckFn,
    extra_arguments: Sequence[str] | None = None,
    expected: ExpectationFn | None = None,
) -> int:
    """Parse `argv`, build the context, run `checks`, emit the result, and return the exit code.

    `extra_arguments` names every additional long-flag string (e.g. `["--token"]`) a script
    accepts and pre-scans out of `argv` itself before calling this function. Any `--flag` in
    `argv` that is neither a pinned flag (`--profile`, `--region`, `--tenant`, `--json`,
    `--outputs`) nor named in `extra_arguments` is unconditionally rejected as a usage error
    (exit 2) - a script must declare every flag it accepts; there is no tolerance heuristic. A
    near-miss of a known flag (edit distance <= 2) is called out with a "did you mean" hint.

    Catches `MissingTerraformOutputError`, malformed/missing Terraform outputs (`RuntimeError`),
    and any exception `checks` raises - `botocore.exceptions.ClientError`, `BotoCoreError`
    (covers `NoCredentialsError`, `ProfileNotFound`, `EndpointConnectionError`, etc.), and any
    other unexpected exception - and converts it into an actionable failing `CheckResult` plus
    exit 1 (emitted in the pinned `--json` shape when requested, else one actionable line on
    stderr) rather than letting a traceback reach the operator. `checks` is consumed lazily: every
    `CheckResult` already yielded before the exception is kept, so a partial inventory survives
    rather than being discarded and replaced by a single synthesised failure.

    `expected` derives, from the same context the checks read, the fewest checks this resource class
    must emit. Every run appends a `coverage_is_non_vacuous` check built from it, so an empty run is
    not reported as a clean one. Omitting `expected` yields a floor of 1 check, stated in the check's
    detail; a derivation that raises is reported as a failure.

    Every result returned from here carries a `Provenance` block (timestamp, git commit and
    dirty flag, region, resource class - never an account id), so the emitted inventory is
    attributable to the revision that produced it.
    """
    parser = build_parser(description)
    args, unknown = parser.parse_known_args(argv)
    _reject_unrecognized_arguments(parser, unknown, extra_arguments)

    # Captured before the context is built so the outputs-loading failure path carries provenance.
    provenance = Provenance.capture(
        region=resolve_region(args.region), resource_class=resource_class
    )

    target_warnings: tuple[str, ...] = ()
    try:
        ctx = context_from_args(args)
        # Before `checks(ctx)`, so the refusal lands ahead of the first AWS call a module makes.
        target_warnings = assert_target_matches_outputs(ctx)
    except (MissingTerraformOutputError, RuntimeError) as exc:
        # A wrong target gets its own check name, distinct from an outputs-loading failure.
        failed_check = "target" if isinstance(exc, WrongTargetError) else "load_outputs"
        result = ResourceClassResult(
            layer=layer,
            resource_class=resource_class,
            checks=(
                CheckResult(
                    resource=resource_class, check=failed_check, status="fail", detail=str(exc)
                ),
            ),
            provenance=provenance,
        )
        _emit_failure(result, as_json=args.as_json)
        return result.exit_code()

    # stderr, so the pinned `--json` shape on stdout is unaffected.
    for warning in target_warnings:
        sys.stderr.write(f"{warning}\n")

    collected: list[CheckResult] = []
    try:
        for check_result in checks(ctx):
            collected.append(check_result)
    except Exception as exc:  # noqa: BLE001 - every failure becomes a reported result; see docstring
        collected.append(
            CheckResult(
                resource=resource_class,
                check="checks",
                status="fail",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    collected.append(coverage_check(resource_class, collected, _resolve_expectation(ctx, expected)))
    result = ResourceClassResult(
        layer=layer, resource_class=resource_class, checks=tuple(collected), provenance=provenance
    )
    emit(result, as_json=args.as_json)
    return result.exit_code()


_PINNED_FLAGS: Final[tuple[str, ...]] = ("--profile", "--region", "--tenant", "--json", "--outputs")


def _levenshtein(a: str, b: str) -> int:
    """Return the edit distance between `a` and `b` (used only for the typo heuristic below)."""
    if a == b:
        return 0
    previous_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current_row = [i]
        for j, char_b in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            substitute_cost = previous_row[j - 1] + (char_a != char_b)
            current_row.append(min(insert_cost, delete_cost, substitute_cost))
        previous_row = current_row
    return previous_row[-1]


def _reject_unrecognized_arguments(
    parser: argparse.ArgumentParser,
    unknown: Sequence[str],
    extra_arguments: Sequence[str] | None,
) -> None:
    """Reject every unrecognised `--flag` unconditionally (`parser.error`, exit 2).

    A flag name explicitly declared via `extra_arguments`, or any token that is not itself a
    long-flag name (e.g. a flag's value), is tolerated silently. Every other `--flag` fails. A
    near-miss of a known flag (edit distance <= 2) gets a "did you mean" hint in the error message.
    """
    allowed = set(extra_arguments or ())
    known = (*_PINNED_FLAGS, *allowed)
    for token in unknown:
        if not token.startswith("--"):
            continue
        flag_name = token.split("=", 1)[0]
        if flag_name in allowed:
            continue
        hint = next(
            (candidate for candidate in known if _levenshtein(flag_name, candidate) <= 2), None
        )
        if hint is not None:
            parser.error(f"unrecognized arguments: {token} (did you mean {hint!r}?)")
        parser.error(f"unrecognized arguments: {token}")


def _emit_failure(result: ResourceClassResult, *, as_json: bool) -> None:
    """Emit an outputs-loading failure: the pinned JSON shape on stdout, else one line on stderr."""
    if as_json:
        emit(result, as_json=True)
        return
    for check in result.failed:
        sys.stderr.write(f"error: {check.detail}\n")


@dataclass(frozen=True)
class LayerStep:
    """One resource-class check bound to the layer it belongs to."""

    layer: int
    resource_class: str
    run: Callable[[], ResourceClassResult]


def run_layers(steps: Sequence[LayerStep]) -> tuple[int, list[ResourceClassResult]]:
    """Run `steps` grouped by layer, ascending, stopping at the first failing layer.

    Every step within a layer runs even if an earlier step in that layer failed. Once a
    layer completes, if any of its steps failed, no step of any later layer runs. Returns
    `(exit_code, results)`; `exit_code` is 1 if anything failed, else 0.
    """
    layers: dict[int, list[LayerStep]] = {}
    for step in steps:
        layers.setdefault(step.layer, []).append(step)

    results: list[ResourceClassResult] = []
    any_failed = False
    for layer_number in sorted(layers):
        layer_failed = False
        for step in layers[layer_number]:
            result = step.run()
            results.append(result)
            if not result.ok:
                layer_failed = True
        if layer_failed:
            any_failed = True
            break

    return (1 if any_failed else 0), results
