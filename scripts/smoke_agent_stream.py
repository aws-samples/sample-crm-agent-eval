# SPDX-License-Identifier: MIT
"""Layer 6 smoke check that the `/agent/*` chat-turn stream is genuinely incremental.

`--assert-incremental` measures the wall-clock arrival time of each SSE event and fails if the
whole response arrived as one buffered flush. Two discriminators, both required (see
`_incremental_check`):

* **Spread.** The time between the first and last event's arrival must clear
  `_MIN_SPREAD_SECONDS`.
* **Lead-in fraction.** The time from issuing the request to the first event, as a fraction of the
  time to the last event, must be at or below `_MAX_LEAD_IN_FRACTION`. A fully buffered response
  has a fraction of 1.0.

The live path selects a tool-calling prompt and measures it through the CloudFront `/agent/*`
behaviour, the production identity path. On that measured turn,
`arrival_gap_aligns_with_tool_dispatch` places the largest arrival gap at the tool boundary, and
`text_delta_frames_arrive_before_completed` requires every `text_delta` frame to arrive before the
turn's single `completed` frame with a measurable spread. `lambda_dispatch_observed` corroborates
the arrival gap against the dispatched action Lambda's `START RequestId` records in CloudWatch Logs,
reporting `defer` (`cloudwatch_log_indexing_lag`) when CloudWatch has not yet indexed the window.
`stub_fingerprint_checks` fails when the Runtime answers from the stub provider.

The transport is an injected `SseSource` seam, so the offline self-test uses a fake clock and canned
bytes. A live turn makes a real, billable model call; the Runtime's provider is `AGENT_PROVIDER` in
`infra/main.tf`.

A disposable `eval-smoke-<run_id>` tenant and one Cognito user are created and removed, and the
`demo` tenant is proved untouched. Any MFA challenge the pool issues is completed. The issued
access token is signature-verified against the pool's JWKS (`scripts.smoke.verified_claims`) before
its claims are read.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

# Supports both `python3 scripts/smoke_agent_stream.py` and `python3 -m scripts.smoke_agent_stream`;
# the path form needs the repository root on `sys.path` to import `scripts.smoke.*`.
if __package__ in (None, ""):  # pragma: no cover - only taken by the path-form invocation
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.smoke.cognito_claims import totp_code  # noqa: E402
from scripts.smoke.demo_tenant_guard import (  # noqa: E402
    TenantSnapshot,
    demo_tenant_untouched_check,
    read_tenant_snapshot,
    store_row_reader,
    writes_only_under_smoke_tenant_check,
)
from scripts.smoke.harness import CheckResult, SmokeContext, open_https, run_main  # noqa: E402
from scripts.smoke.lambda_actions import seeded_in_scope_account_id  # noqa: E402
from scripts.smoke.runtime_invoke import verified_access_claims  # noqa: E402
from scripts.smoke.tenant import (  # noqa: E402
    SmokeTenant,
    allocate_smoke_tenant,
    teardown_smoke_tenant,
)

from crm.envelope import CLAIM_TENANT  # noqa: E402
from crm.store.store import Store  # noqa: E402
from providers.stub import _STUB_INPUT_TOKENS, _STUB_OUTPUT_TOKENS  # noqa: E402

LAYER = 6
RESOURCE_CLASS = "agent_stream"

__all__ = [
    "LAYER",
    "RESOURCE_CLASS",
    "ArrivalMeasurement",
    "LiveStreamPrincipalProvisioner",
    "SseEvent",
    "SseSource",
    "checks",
    "checks_with_source",
    "cloudfront_source",
    "live_source",
    "main",
    "measure_arrivals",
    "measured_turn_book",
    "parse_sse_bytes",
    "smoke_rep_item",
    "stub_fingerprint_checks",
    "token_claims",
]

#: Minimum number of events required to compute an arrival-time spread.
_MIN_EVENTS_FOR_SPREAD = 2

#: The minimum spread, in seconds, between the first and last event arrival that counts as
#: incremental rather than one buffered flush. Combined with `_MAX_LEAD_IN_FRACTION`.
_MIN_SPREAD_SECONDS = 0.05

#: The maximum fraction of the whole request's wall-clock duration (from issuing the request to
#: the last event's arrival) that may elapse before the first event arrives. A fully buffered
#: response has a fraction of 1.0; the lead-in also includes TLS, connection setup, container
#: dispatch and, on the CloudFront arm, the relay's JWT verification.
_MAX_LEAD_IN_FRACTION = 0.9

#: The minimum wall-clock spread between a turn's first `text_delta` frame and its `completed` frame.
_MIN_DELTA_TO_COMPLETED_SPREAD_SECONDS: Final[float] = 0.05

#: How many candidate prompts to try before giving up on eliciting a tool-calling turn. The stub
#: provider chooses a tool call or a text answer from a digest of its inputs, and only a
#: tool-calling turn does real work mid-stream.
_MAX_PROMPT_CANDIDATES: Final[int] = 6

#: Per-request timeout for a live turn, in seconds.
_STREAM_TIMEOUT_SECONDS: Final[float] = 90.0

#: How long to wait for CloudWatch Logs to index the dispatched Lambda's `START RequestId` record,
#: and how often to re-poll while waiting.
_LOG_WAIT_SECONDS: Final[float] = 45.0
_LOG_POLL_SECONDS: Final[float] = 5.0

#: Read size for each socket read on a live stream.
_READ_CHUNK_BYTES: Final[int] = 512

#: TOTP step and sign-in attempt budget; a first sign-in can return a token whose `custom:tenant`
#: is still the pre-token-generation trigger's fail-closed default.
_TOTP_STEP_SECONDS: Final[float] = 30.0
_AUTH_ATTEMPTS: Final[int] = 3


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One parsed SSE record.

    Attributes:
        event: The `event:` field value, or `""` when the record carried none (a bare `data:`
            record, which is a valid SSE message with an implicit `"message"` event type).
        data: The `data:` field's value, with multi-line `data:` fields joined by `\\n` per the
            SSE spec.
    """

    event: str
    data: str


class SseSource(Protocol):
    """The typed seam an SSE stream is read through.

    A live implementation yields raw bytes as they arrive on the socket; an offline test substitutes
    pre-recorded chunks paired with a fake clock.
    """

    def __call__(self) -> Iterator[bytes]:
        """Yield the response body in arrival order, one chunk per network read."""
        ...


def _generated_password() -> str:
    """Return a throwaway password satisfying the pool's 14-character all-classes policy."""
    return "Aa1!" + secrets.token_urlsafe(16)


def _live_table_prefix(ctx: SmokeContext) -> str:
    """Return the deployed physical table-name prefix, derived from the Terraform outputs."""
    names = ctx.outputs.require_mapping("dynamodb_table_names")
    accounts = str(names["accounts"])
    return accounts[: -len("accounts")]


def smoke_rep_item(*, username: str, tenant: str, book: Sequence[str] = ()) -> dict[str, Any]:
    """Return the `reps` row for the smoke user: `book` (empty by default), no full visibility."""
    return {
        "rep_id": username,
        "tenant": tenant,
        "book": list(book),
        "full_visibility": False,
        "name": "Layer 6 agent stream smoke",
        "status": "active",
    }


def measured_turn_book(tenant: str) -> tuple[str, ...]:
    """The book this module's own smoke rep holds: one account of `tenant`.

    A non-empty book lets `crm.authz.require_listing_scope` admit the measured turn's
    `pipeline_aggregate` call, so the turn is a data turn rather than a `scope_denied` one.
    """
    return (seeded_in_scope_account_id(tenant),)


class LiveStreamPrincipalProvisioner:
    """Creates a disposable tenant and one Cognito user, and removes both.

    The pool's MFA configuration is left as deployed and any MFA challenge is completed. The tenant
    comes from `scripts.smoke.tenant.allocate_smoke_tenant`, which refuses `demo`. Mints an access
    token for the relay to verify; the Runtime itself authorizes inbound calls with IAM. The token
    is verified against `jwks_url` (the pool's JWKS by default) before its tenant claim is read.
    Does not seed the fixture corpus. The rep's book is empty unless `book_for_tenant` is given,
    which maps the allocated tenant to the book.
    """

    def __init__(
        self,
        ctx: SmokeContext,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        jwks_url: str | None = None,
        book_for_tenant: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._ctx = ctx
        self._jwks_url = jwks_url
        self._book_for_tenant = book_for_tenant
        self._clock = clock if clock is not None else time.time
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self._smoke_tenant: SmokeTenant | None = None
        self._username: str | None = None

    def store(self) -> Store:
        """Return a `Store` addressing the live tables, using the deployed physical prefix."""
        return Store(
            client=self._ctx.client("dynamodb"), table_prefix=_live_table_prefix(self._ctx)
        )

    def tenant(self) -> str:
        """Return the disposable tenant allocated so far, or `""` before allocation."""
        return self._smoke_tenant.tenant if self._smoke_tenant is not None else ""

    def _idp(self) -> Any:
        return self._ctx.client("cognito-idp")

    def _user_pool_id(self) -> str:
        return self._ctx.outputs.require_str("cognito_user_pool_id")

    def _client_id(self) -> str:
        return self._ctx.outputs.require_str("cognito_spa_client_id")

    def provision(self) -> str:
        """Allocate a tenant, create the user, and return its access token.

        Raises:
            RuntimeError: Any step could not be completed, or the issued token names a tenant
                other than the disposable one this provisioner allocated.
        """
        store = self.store()
        smoke_tenant = allocate_smoke_tenant(store)
        self._smoke_tenant = smoke_tenant

        idp = self._idp()
        email = f"smoke-agent-stream-{smoke_tenant.run_id}@example.test"
        password = _generated_password()
        created = idp.admin_create_user(
            UserPoolId=self._user_pool_id(),
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        username = str((created.get("User") or {}).get("Username") or email)
        self._username = username
        idp.admin_set_user_password(
            UserPoolId=self._user_pool_id(), Username=username, Password=password, Permanent=True
        )
        book = self._book_for_tenant(smoke_tenant.tenant) if self._book_for_tenant else ()
        store.put_item(
            "reps", smoke_rep_item(username=username, tenant=smoke_tenant.tenant, book=book)
        )
        return self._authenticate_with_mfa(
            username=username, password=password, expected_tenant=smoke_tenant.tenant
        )

    def _authenticate_with_mfa(self, *, username: str, password: str, expected_tenant: str) -> str:
        """Sign in, completing MFA enrollment, and return an access token naming `expected_tenant`."""
        secret: str | None = None
        last_tenant = ""
        for attempt in range(1, _AUTH_ATTEMPTS + 1):
            if attempt > 1:
                self._wait_for_next_totp_step()
            access_token, secret = self._one_auth_attempt(
                username=username, password=password, secret=secret
            )
            claims = token_claims(self._ctx, access_token, jwks_url=self._jwks_url)
            last_tenant = str(claims.get(CLAIM_TENANT) or "")
            if last_tenant == expected_tenant:
                return access_token
        raise RuntimeError(
            f"issued access token names tenant {last_tenant!r}, expected the disposable tenant "
            f"{expected_tenant!r} after {_AUTH_ATTEMPTS} attempt(s); the pre-token-generation "
            "trigger's reps lookup did not resolve, so the token must not be used"
        )

    def _one_auth_attempt(
        self, *, username: str, password: str, secret: str | None
    ) -> tuple[str, str]:
        """Run one `ADMIN_USER_PASSWORD_AUTH` sign-in, answering whichever MFA challenge arrives."""
        idp = self._idp()
        response = idp.admin_initiate_auth(
            UserPoolId=self._user_pool_id(),
            ClientId=self._client_id(),
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        challenge = str(response.get("ChallengeName") or "")

        if challenge == "MFA_SETUP":
            associated = idp.associate_software_token(Session=response["Session"])
            secret = str(associated["SecretCode"])
            verified = idp.verify_software_token(
                Session=associated["Session"],
                UserCode=totp_code(secret, timestamp=self._clock()),
                FriendlyDeviceName="crm-eval-smoke",
            )
            if str(verified.get("Status")) != "SUCCESS":
                raise RuntimeError(
                    f"verify_software_token returned Status={verified.get('Status')!r}, "
                    "expected 'SUCCESS'"
                )
            response = idp.admin_respond_to_auth_challenge(
                UserPoolId=self._user_pool_id(),
                ClientId=self._client_id(),
                ChallengeName="MFA_SETUP",
                Session=verified["Session"],
                ChallengeResponses={"USERNAME": username},
            )
        elif challenge == "SOFTWARE_TOKEN_MFA":
            if secret is None:
                raise RuntimeError(
                    "pool challenged SOFTWARE_TOKEN_MFA before any software token was associated"
                )
            response = idp.admin_respond_to_auth_challenge(
                UserPoolId=self._user_pool_id(),
                ClientId=self._client_id(),
                ChallengeName="SOFTWARE_TOKEN_MFA",
                Session=response["Session"],
                ChallengeResponses={
                    "USERNAME": username,
                    "SOFTWARE_TOKEN_MFA_CODE": totp_code(secret, timestamp=self._clock()),
                },
            )
        elif challenge:
            raise RuntimeError(
                f"unexpected challenge {challenge!r} from admin_initiate_auth; this script answers "
                "MFA_SETUP and SOFTWARE_TOKEN_MFA only and never weakens the pool to avoid one"
            )

        access_token = (response.get("AuthenticationResult") or {}).get("AccessToken")
        if not access_token:
            raise RuntimeError(
                f"no AccessToken issued for {username!r} "
                f"(ChallengeName={response.get('ChallengeName')!r})"
            )
        return str(access_token), secret or ""

    def _wait_for_next_totp_step(self) -> None:
        """Sleep into the next TOTP window, so a retry never replays an already-consumed code."""
        now = self._clock()
        self._sleeper(_TOTP_STEP_SECONDS - (now % _TOTP_STEP_SECONDS) + 1.0)

    def cleanup(self) -> Iterable[CheckResult]:
        """Remove the Cognito user, its `reps` row, and the tenant, proving each removal by re-read."""
        yield from self._cleanup_cognito_user()
        yield from self._cleanup_reps_row()
        yield from self._cleanup_tenant()

    def _cleanup_cognito_user(self) -> Iterable[CheckResult]:
        username = self._username
        if username is None:
            return
        try:
            idp = self._idp()
            idp.admin_delete_user(UserPoolId=self._user_pool_id(), Username=username)
            try:
                idp.admin_get_user(UserPoolId=self._user_pool_id(), Username=username)
            except Exception as exc:  # noqa: BLE001 - the expected outcome is an exception
                if "UserNotFoundException" not in f"{type(exc).__name__}: {exc}":
                    raise
                yield CheckResult(
                    resource="smoke_principal",
                    check="cognito_user_deleted",
                    status="pass",
                    detail="admin_get_user after deletion raised UserNotFoundException",
                )
                return
            yield CheckResult(
                resource="smoke_principal",
                check="cognito_user_deleted",
                status="fail",
                detail="admin_get_user still resolves the smoke user after admin_delete_user",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource="smoke_principal",
                check="cognito_user_deleted",
                status="fail",
                detail=f"could not confirm smoke user deletion: {type(exc).__name__}: {exc}",
            )

    def _cleanup_reps_row(self) -> Iterable[CheckResult]:
        username = self._username
        if username is None:
            return
        try:
            store = self.store()
            store.delete_item("reps", {"rep_id": username})
            remaining = store.get_item("reps", {"rep_id": username})
            yield CheckResult(
                resource="smoke_principal",
                check="reps_row_deleted",
                status="pass" if remaining is None else "fail",
                detail=(
                    "reps row absent on read-back"
                    if remaining is None
                    else "reps row still present after delete_item"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource="smoke_principal",
                check="reps_row_deleted",
                status="fail",
                detail=f"could not delete the smoke reps row: {type(exc).__name__}: {exc}",
            )

    def _cleanup_tenant(self) -> Iterable[CheckResult]:
        smoke_tenant = self._smoke_tenant
        if smoke_tenant is None:
            return
        try:
            deleted = teardown_smoke_tenant(self.store(), smoke_tenant)
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="pass",
                detail=f"tenant {smoke_tenant.tenant} torn down; rows deleted: {sum(deleted.values())}",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="fail",
                detail=f"could not tear down {smoke_tenant.tenant}: {type(exc).__name__}: {exc}",
            )


def live_source(
    url: str,
    *,
    token: str,
    body: Mapping[str, Any],
    session_id: str,
    timeout: float,
    headers_sink: dict[str, str] | None = None,
) -> SseSource:
    """Return an `SseSource` performing one authenticated `POST` through CloudFront to the relay.

    Reads with `read1` where supported, so one yielded chunk corresponds to one socket read.

    The JWT is sent in `X-Crm-Authorization`, since `Authorization` carries CloudFront's OAC SigV4
    signature. `x-amz-content-sha256` carries the digest of the exact bytes sent, which OAC verifies
    at the edge, so the body is serialised once and hashed from that buffer.

    Args:
        url: The full invocation URL through the CloudFront `/agent/*` behaviour.
        token: A Cognito access or id token for the relay to verify.
        body: The JSON invocation payload.
        session_id: Value for `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`.
        timeout: Socket timeout in seconds.
        headers_sink: When given, the response's headers are recorded into it (lowercased keys)
            before the first chunk is yielded, including on an error response.
    """
    payload = json.dumps(dict(body)).encode("utf-8")
    body_digest = hashlib.sha256(payload).hexdigest()

    def _read() -> Iterator[bytes]:
        request = urllib.request.Request(  # noqa: S310 - internal seam, fixed https AWS/CloudFront host
            url,
            data=payload,
            method="POST",
            headers={
                "X-Crm-Authorization": f"Bearer {token}",
                "x-amz-content-sha256": body_digest,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
            },
        )
        try:
            with open_https(request, timeout=timeout) as response:
                if headers_sink is not None:
                    headers_sink.update(
                        {str(k).lower(): str(v) for k, v in response.headers.items()}
                    )
                reader = getattr(response, "read1", None)
                while True:
                    chunk = (
                        reader(_READ_CHUNK_BYTES)
                        if callable(reader)
                        else response.read(_READ_CHUNK_BYTES)
                    )
                    if not chunk:
                        return
                    yield bytes(chunk)
        except urllib.error.HTTPError as exc:
            # The sink is filled on the error path too: an error served through CloudFront still
            # carries `x-amz-cf-id`.
            if headers_sink is not None:
                headers_sink.update({str(k).lower(): str(v) for k, v in exc.headers.items()})
            detail = exc.read()[:400]
            raise ValueError(f"invocation failed: status={exc.code} body={detail!r}") from exc

    return _read


def cloudfront_source(
    ctx: SmokeContext,
    *,
    token: str,
    body: Mapping[str, Any],
    session_id: str,
    headers_sink: dict[str, str] | None = None,
) -> SseSource:
    """Build the `SseSource` that streams through the CloudFront `/agent/*` behaviour.

    The browser posts to `/agent/<url-encoded runtime ARN>` same-origin, and the `/agent/*`
    behaviour forwards it unchanged to the relay's Function URL.
    """
    domain = ctx.outputs.require_str("cloudfront_domain_name")
    runtime_arn = ctx.outputs.require_str("agentcore_runtime_arn")
    encoded_arn = urllib.parse.quote(runtime_arn, safe="")
    url = f"https://{domain}/agent/{encoded_arn}"
    return live_source(
        url,
        token=token,
        body=body,
        session_id=session_id,
        timeout=_STREAM_TIMEOUT_SECONDS,
        headers_sink=headers_sink,
    )


class _SseRecordAccumulator:
    """Accumulates SSE field lines into complete `SseEvent`s.

    Handles `event:`/`data:` field lines, a multi-line `data:` field joined by newlines, ignored
    comment lines (starting with `:`), and the blank-line record separator. A dangling unterminated
    record is flushed at end-of-stream via `flush()`. Each logical record gets one arrival time.
    """

    def __init__(self) -> None:
        self._event = ""
        self._data_lines: list[str] = []
        self._has_data = False

    def feed_line(self, line: str) -> Iterator[SseEvent]:
        """Feed one already-newline-split line, yielding a completed event if the line ends one."""
        if line == "":
            yield from self.flush()
            return
        if line.startswith(":"):
            return  # comment / heartbeat line, per the SSE spec
        if line.startswith("event:"):
            self._event = line[len("event:") :].lstrip(" ")
            return
        if line.startswith("data:"):
            self._data_lines.append(line[len("data:") :].lstrip(" "))
            self._has_data = True
            return
        # Any other field (id:, retry:) is accepted by the grammar but irrelevant to this check.

    def flush(self) -> Iterator[SseEvent]:
        """Emit the in-progress record if it carried a `data:` field, then reset."""
        if self._has_data:
            yield SseEvent(event=self._event, data="\n".join(self._data_lines))
        self._event = ""
        self._data_lines = []
        self._has_data = False


def parse_sse_bytes(chunks: Iterable[bytes]) -> Iterator[SseEvent]:
    """Parse a byte stream into `SseEvent`s honouring the real SSE record grammar.

    See `_SseRecordAccumulator` for the record-assembly rules. Malformed or truncated input — a
    stream that ends mid-record with no trailing blank line — still emits whatever complete
    `data:` content was accumulated.
    """
    accumulator = _SseRecordAccumulator()
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
            yield from accumulator.feed_line(line)
    # Flush a final, unterminated line (no trailing newline) and then the record buffer itself.
    if buffer:
        line = buffer.decode("utf-8", errors="replace").rstrip("\r")
        yield from accumulator.feed_line(line)
    yield from accumulator.flush()


@dataclass(frozen=True, slots=True)
class ArrivalMeasurement:
    """The result of timing an SSE stream's event arrivals.

    Attributes:
        event_count: Number of complete SSE records observed.
        spread_seconds: Wall-clock time between the first and last event's arrival, as measured
            by the injected clock. `0.0` when fewer than two events arrived.
        first_gap_seconds: Wall-clock time between the first and second event's arrival. `0.0`
            when fewer than two events arrived. Reported as a diagnostic; the verdict uses
            `lead_in_fraction`.
        lead_in_seconds: Wall-clock time from issuing the request to the first event's arrival.
            `0.0` when no event arrived.
        total_seconds: Wall-clock time from issuing the request to the last event's arrival.
            `0.0` when no event arrived.
        events: The parsed records, in arrival order, so a caller can tell a tool-calling turn
            from a text-only one without re-reading the stream.
        arrival_offsets: Each record's arrival time, in seconds after the request was issued, in
            arrival order. Lets a caller check *where* in the stream the delay fell rather than
            only how large it was.
    """

    event_count: int
    spread_seconds: float
    first_gap_seconds: float = 0.0
    lead_in_seconds: float = 0.0
    total_seconds: float = 0.0
    events: tuple[SseEvent, ...] = ()
    arrival_offsets: tuple[float, ...] = ()

    @property
    def lead_in_fraction(self) -> float:
        """The share of the request's total duration that elapsed before the first event arrived.

        `1.0` when the total duration is zero or unmeasurable, which is the conservative reading:
        a stream that produced no measurable post-first-event duration has not demonstrated
        incremental delivery.
        """
        if self.total_seconds <= 0.0:
            return 1.0
        return self.lead_in_seconds / self.total_seconds


def measure_arrivals(source: SseSource, *, clock: Callable[[], float]) -> ArrivalMeasurement:
    """Read `source` to completion, timestamping each parsed SSE record's arrival with `clock`.

    `clock()` is sampled once before the source is opened, so `lead_in_seconds` and
    `total_seconds` are measured from the moment the request was issued. A record's arrival time is
    the time of the chunk that completed it.

    Args:
        source: Yields raw response body chunks in arrival order.
        clock: Returns the current time in seconds, e.g. `time.monotonic`.

    Returns:
        The measured event count, arrival spread, first-to-second gap, lead-in, total duration,
        and the parsed records.
    """
    accumulator = _SseRecordAccumulator()
    timestamps: list[float] = []
    events: list[SseEvent] = []
    buffer = b""
    started_at = clock()
    for chunk in source():
        now = clock()
        buffer += chunk
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
            for event in accumulator.feed_line(line):
                timestamps.append(now)
                events.append(event)
    if buffer:
        now = clock()
        line = buffer.decode("utf-8", errors="replace").rstrip("\r")
        for event in accumulator.feed_line(line):
            timestamps.append(now)
            events.append(event)
    now = clock()
    for event in accumulator.flush():
        timestamps.append(now)
        events.append(event)

    if not timestamps:
        return ArrivalMeasurement(event_count=0, spread_seconds=0.0)
    spread = timestamps[-1] - timestamps[0]
    first_gap = timestamps[1] - timestamps[0] if len(timestamps) >= _MIN_EVENTS_FOR_SPREAD else 0.0
    return ArrivalMeasurement(
        event_count=len(timestamps),
        spread_seconds=spread,
        first_gap_seconds=first_gap,
        lead_in_seconds=timestamps[0] - started_at,
        total_seconds=timestamps[-1] - started_at,
        events=tuple(events),
        arrival_offsets=tuple(stamp - started_at for stamp in timestamps),
    )


def _incremental_check(
    measurement: ArrivalMeasurement, *, resource: str, check: str
) -> CheckResult:
    """Assert the measured arrival pattern proves incremental delivery, not merely many events.

    Both are required:

    * `spread_seconds` at or above `_MIN_SPREAD_SECONDS`;
    * `lead_in_fraction` at or below `_MAX_LEAD_IN_FRACTION`.
    """
    if measurement.event_count < _MIN_EVENTS_FOR_SPREAD:
        return CheckResult(
            resource=resource,
            check=check,
            status="skip",
            detail=(
                f"only {measurement.event_count} event(s) arrived; at least "
                f"{_MIN_EVENTS_FOR_SPREAD} are needed to measure an arrival-time spread"
            ),
        )
    if measurement.spread_seconds < _MIN_SPREAD_SECONDS:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"spread={measurement.spread_seconds:.4f}s over {measurement.event_count} events is "
                f"below the {_MIN_SPREAD_SECONDS}s floor: the response arrived as one buffered block "
                "rather than incrementally"
            ),
        )
    if measurement.lead_in_fraction > _MAX_LEAD_IN_FRACTION:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"lead_in={measurement.lead_in_seconds:.4f}s is "
                f"{measurement.lead_in_fraction:.3f} of the {measurement.total_seconds:.4f}s total "
                f"request duration over {measurement.event_count} events - exceeds the "
                f"{_MAX_LEAD_IN_FRACTION} ceiling: the first event did not arrive meaningfully "
                "before the last, consistent with the whole body being buffered until the turn "
                "completed rather than delivered incrementally"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=(
            f"spread={measurement.spread_seconds:.4f}s, "
            f"lead_in={measurement.lead_in_seconds:.4f}s "
            f"({measurement.lead_in_fraction:.3f} of {measurement.total_seconds:.4f}s total), "
            f"first_gap={measurement.first_gap_seconds:.4f}s "
            f"over {measurement.event_count} events"
        ),
    )


def _event_kind(event: SseEvent) -> str:
    """Return the `kind` field of one SSE record's JSON payload, or `""` if it has none."""
    try:
        decoded = json.loads(event.data)
    except (ValueError, TypeError):
        return ""
    return str(decoded.get("kind", "")) if isinstance(decoded, dict) else ""


#: The events after which the origin awaits real work: the action Lambda after `tool_call`, the
#: post-tool model round after `tool_result`.
_TOOL_BOUNDARY_KINDS: Final[frozenset[str]] = frozenset({"tool_call", "tool_result"})


def _gap_alignment_check(measurement: ArrivalMeasurement, *, resource: str) -> CheckResult:
    """Assert the stream's arrival gaps sit where the origin did its work around the tool call.

    Three conditions, all required:

    1. **boundary** — the largest gap follows a `tool_call` or `tool_result` event;
    2. **substantial** — that gap is at least `_MIN_SPREAD_SECONDS`;
    3. **dispatch visible** — the first `tool_result` after the first `tool_call` arrives at least
       `_MIN_SPREAD_SECONDS` later, so the Lambda round trip was not coalesced into one flush.

    `_MAX_LEAD_IN_FRACTION` is not consulted. The detail names every condition that failed.
    """
    check = "arrival_gap_aligns_with_tool_dispatch"
    kinds = [_event_kind(event) for event in measurement.events]
    offsets = measurement.arrival_offsets
    if "tool_call" not in kinds or len(offsets) < _MIN_EVENTS_FOR_SPREAD:
        return CheckResult(
            resource=resource,
            check=check,
            status="skip",
            detail=(
                f"no tool_call event in this turn (kinds={kinds!r}); there is no dispatch boundary "
                "for an arrival gap to align with"
            ),
        )
    gaps = [offsets[index + 1] - offsets[index] for index in range(len(offsets) - 1)]
    largest_index = max(range(len(gaps)), key=lambda index: gaps[index])
    tool_call_index = kinds.index("tool_call")
    tool_result_index = next(
        (
            index
            for index in range(tool_call_index + 1, len(kinds))
            if kinds[index] == "tool_result"
        ),
        None,
    )
    dispatch_seconds = (
        offsets[tool_result_index] - offsets[tool_call_index]
        if tool_result_index is not None
        else None
    )

    reasons: list[str] = []
    if gaps[largest_index] < _MIN_SPREAD_SECONDS:
        reasons.append(
            f"the largest gap is below the {_MIN_SPREAD_SECONDS}s floor, so every arrival is "
            "indistinguishable noise and no gap identifies a real delay at all"
        )
    if kinds[largest_index] not in _TOOL_BOUNDARY_KINDS:
        reasons.append(
            f"the largest gap follows {kinds[largest_index]!r}, not a tool_call or tool_result, so "
            "the delay does not sit at the tool boundary where the origin does its work"
        )
    if dispatch_seconds is None:
        reasons.append(
            "no tool_result follows the tool_call, so the Lambda round trip is not visible as a "
            "separate arrival"
        )
    elif dispatch_seconds < _MIN_SPREAD_SECONDS:
        reasons.append(
            f"tool_result arrived {dispatch_seconds:.4f}s after tool_call, below the "
            f"{_MIN_SPREAD_SECONDS}s floor: the dispatch was coalesced into one flush rather than "
            "arriving as a separate event"
        )
    dispatch_text = "absent" if dispatch_seconds is None else f"{dispatch_seconds:.4f}s"
    return CheckResult(
        resource=resource,
        check=check,
        status="fail" if reasons else "pass",
        detail=(
            f"largest arrival gap {gaps[largest_index]:.4f}s follows event {largest_index} "
            f"({kinds[largest_index]!r}); tool_call -> tool_result {dispatch_text}; "
            f"kinds={kinds!r}; gaps={[round(gap, 4) for gap in gaps]!r}"
            + "".join(f" - {reason}" for reason in reasons)
        ),
    )


def _dispatched_tool_name(measurement: ArrivalMeasurement) -> str:
    """Return the `tool_name` of the turn's first `tool_call` event, or `""` if there is none."""
    for event in measurement.events:
        if _event_kind(event) != "tool_call":
            continue
        try:
            decoded = json.loads(event.data)
        except (ValueError, TypeError):
            return ""
        if isinstance(decoded, dict):
            return str(decoded.get("tool_name") or "")
    return ""


def _latest_indexed_timestamp_ms(logs_client: Any, log_group: str) -> int | None:
    """Return the newest event timestamp CloudWatch has indexed for `log_group`, or None."""
    response = logs_client.describe_log_streams(
        logGroupName=log_group, orderBy="LastEventTime", descending=True, limit=1
    )
    streams = response.get("logStreams") or []
    if not streams:
        return None
    timestamp = streams[0].get("lastEventTimestamp")
    return int(timestamp) if isinstance(timestamp, int | float) else None


def _lambda_dispatch_check(
    *,
    logs_client: Any,
    function_name: str,
    window_start_ms: int,
    window_end_ms: int,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> CheckResult:
    """Corroborate the measured arrival gap against the dispatched Lambda's own START records.

    Reads `START RequestId` entries in `/aws/lambda/<function>` inside the measured turns' window:

    * `pass` — at least one START record inside the window.
    * `fail` — the log group has indexed events at or after the window's start yet has no START
      record inside it, or the CloudWatch read raised.
    * `defer` (`cloudwatch_log_indexing_lag`) — the group's newest indexed event predates the
      window, or the group has no indexed stream.
    """
    check = "lambda_dispatch_observed"
    log_group = f"/aws/lambda/{function_name}"
    deadline = now() + _LOG_WAIT_SECONDS
    try:
        while True:
            response = logs_client.filter_log_events(
                logGroupName=log_group,
                startTime=window_start_ms,
                endTime=window_end_ms,
                filterPattern='"START RequestId"',
                limit=50,
            )
            events = response.get("events") or []
            if events:
                return CheckResult(
                    resource="agent_stream",
                    check=check,
                    status="pass",
                    detail=(
                        f"{len(events)} 'START RequestId' record(s) in {log_group} between "
                        f"{window_start_ms} and {window_end_ms} (epoch ms), so the measured "
                        "arrival gap is a real Lambda round trip observed by the function itself, "
                        "not a transport artifact"
                    ),
                )
            if now() >= deadline:
                break
            sleeper(_LOG_POLL_SECONDS)
        latest = _latest_indexed_timestamp_ms(logs_client, log_group)
    except Exception as exc:  # noqa: BLE001 - an unobservable boundary is not a passing one
        return CheckResult(
            resource="agent_stream",
            check=check,
            status="fail",
            detail=(
                f"could not read {log_group} to corroborate the dispatch: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
    if latest is not None and latest >= window_start_ms:
        return CheckResult(
            resource="agent_stream",
            check=check,
            status="fail",
            detail=(
                f"{log_group} carries no 'START RequestId' record between {window_start_ms} and "
                f"{window_end_ms} (epoch ms) after waiting {_LOG_WAIT_SECONDS}s, yet its newest "
                f"indexed event is at {latest}, at or after the window's start - so ingestion has "
                "reached this window and the dispatch genuinely was not recorded"
            ),
        )
    return CheckResult(
        resource="agent_stream",
        check=check,
        status="defer",
        defer_reason="cloudwatch_log_indexing_lag",
        detail=(
            f"{log_group} carries no 'START RequestId' record in the measured window "
            f"({window_start_ms}-{window_end_ms} epoch ms) after waiting {_LOG_WAIT_SECONDS}s, and "
            f"its newest indexed event ({latest!r}) predates that window, so CloudWatch has not "
            "caught up and the corroboration is unanswerable rather than absent"
        ),
    )


def _text_delta_order_check(measurement: ArrivalMeasurement, *, resource: str) -> CheckResult:
    """Assert the turn's answer streamed as `text_delta` frames ahead of its `completed` frame.

    All required: exactly one `completed` frame; at least one `text_delta`; every `text_delta`
    strictly before `completed`; and at least `_MIN_DELTA_TO_COMPLETED_SPREAD_SECONDS` from the
    first `text_delta` to `completed`. The detail names every condition that failed.
    """
    kinds = [_event_kind(event) for event in measurement.events]
    offsets = measurement.arrival_offsets
    completed = [index for index, kind in enumerate(kinds) if kind == "completed"]
    deltas = [index for index, kind in enumerate(kinds) if kind == "text_delta"]
    reasons: list[str] = []
    if len(completed) != 1:
        reasons.append(f"expected exactly one completed frame, saw {len(completed)}")
    if not deltas:
        reasons.append("no text_delta frame arrived, so the answer did not stream incrementally")
    spread: float | None = None
    if len(completed) == 1 and deltas:
        completed_at = offsets[completed[0]]
        late = [index for index in deltas if offsets[index] >= completed_at]
        if late:
            reasons.append(f"text_delta frame(s) {late} arrived at or after completed")
        spread = completed_at - min(offsets[index] for index in deltas)
        if spread < _MIN_DELTA_TO_COMPLETED_SPREAD_SECONDS:
            reasons.append(
                f"only {spread:.4f}s from the first text_delta to completed, under the "
                f"{_MIN_DELTA_TO_COMPLETED_SPREAD_SECONDS}s floor: consistent with one buffered flush"
            )
    spread_text = "n/a" if spread is None else f"{spread:.4f}s"
    return CheckResult(
        resource=resource,
        check="text_delta_frames_arrive_before_completed",
        status="fail" if reasons else "pass",
        detail=(
            f"{len(deltas)} text_delta frame(s), {len(completed)} completed frame(s); first "
            f"text_delta -> completed {spread_text}; kinds={kinds!r}"
            + "".join(f" - {reason}" for reason in reasons)
        ),
    )


def checks_with_source(source: SseSource, *, clock: Callable[[], float]) -> Iterable[CheckResult]:
    """Measure one turn from `source` and publish its arrival, tool-gap and text-delta verdicts.

    A transport failure is one failing `assert_incremental` row.
    """
    try:
        measurement = measure_arrivals(source, clock=clock)
    except Exception as exc:  # noqa: BLE001 - a transport failure is a check failure, not a crash
        return [
            CheckResult(
                resource="agent_stream",
                check="assert_incremental",
                status="fail",
                detail=str(exc),
            )
        ]
    return [
        _incremental_check(measurement, resource="agent_stream", check="assert_incremental"),
        _gap_alignment_check(measurement, resource="agent_stream"),
        _text_delta_order_check(measurement, resource="agent_stream"),
    ]


def _measure_or_failure(
    source: SseSource, *, clock: Callable[[], float], resource: str, check: str
) -> tuple[ArrivalMeasurement | None, CheckResult | None]:
    try:
        return measure_arrivals(source, clock=clock), None
    except Exception as exc:  # noqa: BLE001 - a transport failure is a check failure, not a crash
        return None, CheckResult(resource=resource, check=check, status="fail", detail=str(exc))


#: Marker the stub provider prefixes its synthetic reply with (`src/providers/stub.py`).
#: `test_smoke_agent_stream.py` asserts it still appears in the producer's source.
_STUB_TEXT_MARKER: Final[str] = "[stub:"

#: Key the stub sets in `tool_output`.
_STUB_TOOL_OUTPUT_FLAG: Final[str] = "stub"

#: The three stub fingerprints, in the order they are published.
_STUB_FINGERPRINT_CHECKS: Final[tuple[str, ...]] = (
    "runtime_text_is_not_stub_shaped",
    "runtime_tool_output_carries_no_stub_flag",
    "runtime_usage_is_not_the_stub_constants",
)


def _event_payloads(events: Sequence[SseEvent]) -> list[dict[str, Any]]:
    """Return each SSE record's decoded JSON object, skipping any record that does not parse."""
    payloads: list[dict[str, Any]] = []
    for event in events:
        try:
            decoded = json.loads(event.data)
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, dict):
            payloads.append(decoded)
    return payloads


def stub_fingerprint_checks(events: Sequence[SseEvent]) -> list[CheckResult]:
    """Assert the deployed Runtime answered from a real provider and not from `providers.stub`.

    Three fingerprints, each a value only the stub produces:

    * the reply text carries the stub's `[stub:<model_id>]` prefix;
    * a `tool_output` carries the stub's `stub` flag;
    * usage reports the stub's two exact token constants.

    The stub takes either the tool or the text branch, so the first two are mutually exclusive on
    one turn; usage is emitted on both. A row that did not observe its fingerprint reports `pass`.

    Args:
        events: The turn's parsed SSE records. An empty sequence yields `skip` for all three.
    """
    if not events:
        return [
            CheckResult(
                resource="agent_stream",
                check=check,
                status="skip",
                detail=(
                    "no SSE events were parsed, so nothing was examined for stub fingerprints; a "
                    "pass here would assert a property over an empty transcript"
                ),
            )
            for check in _STUB_FINGERPRINT_CHECKS
        ]

    payloads = _event_payloads(events)
    stub_texts = [
        text
        for payload in payloads
        if isinstance(text := payload.get("text"), str) and _STUB_TEXT_MARKER in text
    ]
    stub_tool_outputs = [
        output
        for payload in payloads
        if isinstance(output := payload.get("tool_output"), dict)
        and bool(output.get(_STUB_TOOL_OUTPUT_FLAG))
    ]
    # The stub emits usage on both branches, so a transcript with no token counts examined nothing
    # (for example a turn ending in an `error`) and is not a pass.
    usage_bearing = [
        payload for payload in payloads if "input_tokens" in payload and "output_tokens" in payload
    ]
    stub_usages = [
        payload
        for payload in usage_bearing
        if payload.get("input_tokens") == _STUB_INPUT_TOKENS
        and payload.get("output_tokens") == _STUB_OUTPUT_TOKENS
    ]

    kinds = [str(payload.get("kind", "")) for payload in payloads]
    seen = f"{len(payloads)} event(s), kinds={kinds!r}"
    return [
        CheckResult(
            resource="agent_stream",
            check="runtime_text_is_not_stub_shaped",
            status="fail" if stub_texts else "pass",
            detail=(
                f"a reply text carries the stub provider's {_STUB_TEXT_MARKER!r} prefix, so the "
                f"deployed Runtime answered from providers.stub rather than a model: "
                f"{stub_texts[0][:120]!r} ({seen})"
                if stub_texts
                else f"no reply text carries the {_STUB_TEXT_MARKER!r} prefix ({seen})"
            ),
        ),
        CheckResult(
            resource="agent_stream",
            check="runtime_tool_output_carries_no_stub_flag",
            status="fail" if stub_tool_outputs else "pass",
            detail=(
                f"a tool_output carries the stub provider's {_STUB_TOOL_OUTPUT_FLAG!r} flag, so the "
                f"tool result was synthesised rather than dispatched: {stub_tool_outputs[0]!r} "
                f"({seen})"
                if stub_tool_outputs
                else f"no tool_output carries a {_STUB_TOOL_OUTPUT_FLAG!r} flag ({seen})"
            ),
        ),
        CheckResult(
            resource="agent_stream",
            check="runtime_usage_is_not_the_stub_constants",
            status="fail" if stub_usages else ("pass" if usage_bearing else "skip"),
            detail=(
                f"usage reports the stub provider's exact constants "
                f"({_STUB_INPUT_TOKENS} in / {_STUB_OUTPUT_TOKENS} out), which a real model call "
                f"does not produce ({seen})"
                if stub_usages
                else (
                    f"usage does not report the stub's {_STUB_INPUT_TOKENS}/{_STUB_OUTPUT_TOKENS} "
                    f"constants ({seen})"
                )
                if usage_bearing
                else (
                    "no event carried token counts, so the stub's constants could not be examined; "
                    "the stub emits usage on both of its branches, so a transcript without any is a "
                    f"turn that did not complete normally rather than a clean one ({seen})"
                )
            ),
        ),
    ]


def _has_tool_call(measurement: ArrivalMeasurement) -> bool:
    """Return whether the measured turn actually dispatched a tool."""
    return any(
        '"kind": "tool_call"' in event.data or '"kind":"tool_call"' in event.data
        for event in measurement.events
    )


def _turn_body(prompt: str, session_id: str) -> dict[str, Any]:
    """Return the invocation payload for one turn, matching `useAgentStream`'s own body shape."""
    return {"prompt": prompt, "session_id": session_id, "resume_from": 0}


def token_claims(ctx: SmokeContext, token: str, *, jwks_url: str | None = None) -> dict[str, Any]:
    """Return an access token's claims once it verifies against the stack's user pool.

    Args:
        ctx: Supplies the region and the Cognito pool and client outputs.
        token: The Cognito access token.
        jwks_url: JWKS endpoint to verify against; the pool's by default.

    Raises:
        ClaimsVerificationError: The token failed verification.
    """
    return verified_access_claims(ctx, token, jwks_url=jwks_url)


#: Every `(resource, check)` row a complete live run publishes. `_deferrals_for_unemitted` emits
#: a `defer` for every row missing at the end of a run, and `test_smoke_agent_stream.py` holds the
#: table to the module's real emissions.
_EXPECTED_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("smoke_principal", "mfa_access_token_issued"),
    ("agent_stream", "tool_calling_turn_elicited"),
    ("agent_stream", "assert_incremental"),
    ("agent_stream", "arrival_gap_aligns_with_tool_dispatch"),
    ("agent_stream", "text_delta_frames_arrive_before_completed"),
    ("agent_stream", "lambda_dispatch_observed"),
    ("agent_stream", "runtime_text_is_not_stub_shaped"),
    ("agent_stream", "runtime_tool_output_carries_no_stub_flag"),
    ("agent_stream", "runtime_usage_is_not_the_stub_constants"),
    ("agent_stream", "demo_tenant_untouched"),
    ("agent_stream", "writes_only_under_smoke_tenant"),
    ("smoke_principal", "cognito_user_deleted"),
    ("smoke_principal", "reps_row_deleted"),
    ("smoke_tenant", "tenant_left_clean"),
)

#: Reason attached to a row that never ran because something upstream of it failed.
_UNEMITTED_DEFER_REASON: Final[str] = "upstream_check_did_not_run"


def _deferrals_for_unemitted(results: Iterable[CheckResult]) -> list[CheckResult]:
    """Return an explicit `defer` for every expected check the run did not emit.

    The detail names the failures that preceded it.
    """
    emitted = {result.check for result in results}
    blockers = sorted({result.check for result in results if result.status == "fail"})
    cause = ", ".join(blockers) if blockers else "an upstream step that published no failure"
    return [
        CheckResult(
            resource=resource,
            check=check,
            status="defer",
            defer_reason=_UNEMITTED_DEFER_REASON,
            detail=(
                f"did not run, so this property is unverified rather than absent; blocked by: "
                f"{cause}"
            ),
        )
        for resource, check in _EXPECTED_CHECKS
        if check not in emitted
    ]


def checks(ctx: SmokeContext, *, jwks_url: str | None = None) -> Iterable[CheckResult]:
    """Provision a disposable principal, measure the live stream, and clean up.

    The `demo` snapshot is taken before anything is provisioned. Every row in `_EXPECTED_CHECKS`
    is published; a check that could not run emits an explicit `defer`. Tokens are verified
    against `jwks_url`, the pool's JWKS by default.
    """
    results: list[CheckResult] = []
    provisioner = LiveStreamPrincipalProvisioner(
        ctx, jwks_url=jwks_url, book_for_tenant=measured_turn_book
    )
    reader = store_row_reader(provisioner.store())
    demo_before: TenantSnapshot = read_tenant_snapshot(reader, "demo")

    try:
        token = provisioner.provision()
        results.append(
            CheckResult(
                resource="smoke_principal",
                check="mfa_access_token_issued",
                status="pass",
                detail=(
                    f"access token names the disposable tenant {provisioner.tenant()!r}; "
                    "MFA completed, never weakened. Establishes only that a token was issued "
                    "for that tenant — it says nothing about whether the stream works, which is "
                    "what the agent_stream rows below are for"
                ),
            )
        )
        results.extend(_stream_checks(ctx, token=token))
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, never a crash
        results.append(
            CheckResult(
                resource="agent_stream",
                check="assert_incremental",
                status="fail",
                detail=f"could not reach a measurable live turn: {type(exc).__name__}: {exc}",
            )
        )
    finally:
        # Both tenant-delta assertions run before teardown, which deletes the `reps` row they read.
        results.append(
            demo_tenant_untouched_check(resource="agent_stream", before=demo_before, reader=reader)
        )
        if provisioner.tenant():
            results.append(
                writes_only_under_smoke_tenant_check(
                    resource="agent_stream", tenant=provisioner.tenant(), reader=reader
                )
            )
        results.extend(provisioner.cleanup())
        # Last, so it sees every row any path above produced.
        results.extend(_deferrals_for_unemitted(results))
    return results


def _stream_checks(ctx: SmokeContext, *, token: str) -> list[CheckResult]:
    """Find a tool-calling prompt, then measure that same prompt's turn through CloudFront.

    Both turns run on the CloudFront `/agent/*` path, the production identity path. Checks skipped
    by an early return are reported by `_deferrals_for_unemitted` in `checks`.
    """
    clock = time.monotonic
    chosen_prompt: str | None = None
    chosen_measurement: ArrivalMeasurement | None = None
    attempts: list[str] = []
    for attempt in range(_MAX_PROMPT_CANDIDATES):
        prompt = f"Summarise my pipeline (probe {attempt})."
        session_id = f"smoke-agent-stream-{uuid.uuid4().hex}"
        measurement, failure = _measure_or_failure(
            cloudfront_source(
                ctx, token=token, body=_turn_body(prompt, session_id), session_id=session_id
            ),
            clock=clock,
            resource="agent_stream",
            check="tool_calling_turn_elicited",
        )
        if failure is not None:
            return [failure]
        if measurement is None:
            raise RuntimeError("_measure_or_failure returned neither a measurement nor a failure")
        attempts.append(f"{prompt!r}->{measurement.event_count} events")
        if _has_tool_call(measurement):
            chosen_prompt = prompt
            chosen_measurement = measurement
            break

    if chosen_prompt is None or chosen_measurement is None:
        return [
            CheckResult(
                resource="agent_stream",
                check="tool_calling_turn_elicited",
                status="fail",
                detail=(
                    f"no candidate prompt produced a tool-calling turn in "
                    f"{_MAX_PROMPT_CANDIDATES} attempts ({'; '.join(attempts)}); a text-only stub "
                    "turn completes instantly and carries no timing evidence either way"
                ),
            )
        ]

    # Over the transcript the probe already produced, so the stub fingerprints cost no extra turn.
    results = list(stub_fingerprint_checks(chosen_measurement.events))
    results.append(
        CheckResult(
            resource="agent_stream",
            check="tool_calling_turn_elicited",
            status="pass",
            detail=(
                f"prompt {chosen_prompt!r} dispatches a real action Lambda mid-stream, so the turn "
                "carries wall-clock work after its first event"
            ),
        )
    )
    cf_session = f"smoke-agent-stream-cf-{uuid.uuid4().hex}"
    # Epoch bounds around the measured turn only, scoping the CloudWatch corroboration.
    window_start_ms = int(time.time() * 1000)
    results.extend(
        checks_with_source(
            cloudfront_source(
                ctx, token=token, body=_turn_body(chosen_prompt, cf_session), session_id=cf_session
            ),
            clock=clock,
        )
    )
    window_end_ms = int(time.time() * 1000)
    tool_name = _dispatched_tool_name(chosen_measurement)
    if not tool_name:
        results.append(
            CheckResult(
                resource="agent_stream",
                check="lambda_dispatch_observed",
                status="fail",
                detail=(
                    "the elicited turn's tool_call event named no tool, so the dispatched function "
                    "could not be identified and the arrival gap cannot be corroborated against "
                    "the Lambda's own record"
                ),
            )
        )
    else:
        results.append(
            _lambda_dispatch_check(
                logs_client=ctx.client("logs"),
                function_name=f"{_live_table_prefix(ctx)}-{tool_name}",
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            )
        )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the `--assert-incremental` smoke check and return 0 pass / 1 fail.

    `--assert-incremental` is accepted and inert; this script only performs that assertion.
    """
    filtered = [arg for arg in (argv or []) if arg != "--assert-incremental"]
    return run_main(
        filtered,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
