# SPDX-License-Identifier: MIT
"""Gateway #2 Lambda target ``chat_with_supervisor``: one supervisor turn per MCP tool call.

The event is the tool's arguments plus the ``_identity`` the shared REQUEST interceptor injected::

    in:  {"message": str, "conversation_id"?: str, "_identity": {...}}
    out: {"answer": str, "conversation_id": str}
     or  {"isError": true, "content": [{"type": "text", "text": <user-safe>}],
          "error_kind": "denied" | "budget_exhausted" | "timeout" | "model_failure"
                        | "invalid_request"}

Only the ``gateway`` identity channel is accepted. The conversation is claimed or verified through
:func:`crm.conversations.claim_or_verify`, tuning comes from the caller's saved settings (defaults for
a service principal), and the turn runs on the AgentCore Runtime with ``runtimeSessionId`` equal to
the conversation id and an ``_agent_identity`` built here from the verified identity. The Runtime's
SSE stream is read to its ``completed`` or ``error`` frame within :data:`CHAT_DEADLINE_SECONDS`.

Environment: ``CRM_TABLE_PREFIX``, ``AGENT_RUNTIME_ARN``, ``AGENT_RUNTIME_QUALIFIER``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

from crm import conversations, user_settings
from crm.authz import Identity
from crm.envelope import (
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    decode_identity,
    strip_identity,
)
from crm.errors import AuthenticationFailed, CrmError, NotFound, ScopeDenied, ValidationFailed
from crm.store.store import Store
from providers.capabilities import CapabilityMatrix, load_capability_matrix

logger = logging.getLogger(__name__)

TOOL_NAME: Final[str] = "chat_with_supervisor"

#: Wall-clock budget for one call, from handler entry to the Runtime's terminal frame.
CHAT_DEADLINE_SECONDS: Final[float] = 640.0

#: Longest single socket read; above the Runtime's 20 s heartbeat interval, so a stalled stream
#: fails as ``timeout`` rather than outliving the Lambda.
READ_TIMEOUT_SECONDS: Final[int] = 30

#: Time reserved after the last possible read for rendering and logging the result.
_RESULT_MARGIN_SECONDS: Final[float] = 5.0


def effective_deadline_seconds(remaining_seconds: float | None) -> float:
    """Return the turn deadline, shortened so its last read ends before the Lambda does.

    Args:
        remaining_seconds: The invocation's remaining time, or ``None`` when unknown.

    Returns:
        :data:`CHAT_DEADLINE_SECONDS`, or less when ``remaining_seconds`` cannot also fit one
        :data:`READ_TIMEOUT_SECONDS` read and the result margin after it.
    """
    if remaining_seconds is None:
        return CHAT_DEADLINE_SECONDS
    return max(
        0.0,
        min(
            CHAT_DEADLINE_SECONDS, remaining_seconds - READ_TIMEOUT_SECONDS - _RESULT_MARGIN_SECONDS
        ),
    )


MESSAGE_MAX_CHARS: Final[int] = 8000

ChatErrorKind = Literal["denied", "budget_exhausted", "timeout", "model_failure", "invalid_request"]

#: Runtime ``error`` frame ``error_kind`` → this target's ``error_kind``.
_RUNTIME_ERROR_KINDS: Final[Mapping[str, ChatErrorKind]] = {
    "budget_exhausted": "budget_exhausted",
    "invalid_request": "invalid_request",
    "model_failure": "model_failure",
    "tool_failure": "model_failure",
}

#: Frame kinds read past without effect while waiting for the terminal frame.
_IGNORED_FRAME_KINDS: Final[frozenset[str]] = frozenset(
    {"heartbeat", "text_delta", "tool_call", "tool_result", "proposal", "usage"}
)

_ARGUMENT_KEYS: Final[frozenset[str]] = frozenset({"message", "conversation_id"})
_READ_CHUNK_BYTES: Final[int] = 4096

_DENIED_MESSAGE: Final[str] = "You are not allowed to use this conversation."
_UNAVAILABLE_MESSAGE: Final[str] = "The assistant is unavailable right now. Try again later."
_TIMEOUT_MESSAGE: Final[str] = "The assistant did not finish in time. Try a shorter request."


class RuntimeClient(Protocol):
    """The ``bedrock-agentcore`` client surface this target calls."""

    def invoke_agent_runtime(self, **kwargs: Any) -> Mapping[str, Any]:
        """Start one Runtime invocation; ``response`` is a readable byte stream."""
        ...


@dataclass(frozen=True, slots=True)
class ChatTargetConfig:
    """Deployment configuration, read from the Lambda environment.

    Attributes:
        table_prefix: ``CRM_TABLE_PREFIX``; may be empty.
        runtime_arn: ``AGENT_RUNTIME_ARN``.
        runtime_qualifier: ``AGENT_RUNTIME_QUALIFIER``.
    """

    table_prefix: str
    runtime_arn: str
    runtime_qualifier: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ChatTargetConfig:
        """Read the configuration from ``env`` (default ``os.environ``).

        Raises:
            ValueError: ``AGENT_RUNTIME_ARN`` or ``AGENT_RUNTIME_QUALIFIER`` is unset or empty.
        """
        source = os.environ if env is None else env
        runtime_arn = source.get("AGENT_RUNTIME_ARN", "").strip()
        qualifier = source.get("AGENT_RUNTIME_QUALIFIER", "").strip()
        if not runtime_arn or not qualifier:
            raise ValueError("AGENT_RUNTIME_ARN and AGENT_RUNTIME_QUALIFIER must both be set")
        return cls(
            table_prefix=source.get("CRM_TABLE_PREFIX", ""),
            runtime_arn=runtime_arn,
            runtime_qualifier=qualifier,
        )


class ChatTargetError(Exception):
    """A failure rendered as an MCP ``isError`` result with a closed ``error_kind``."""

    def __init__(self, kind: ChatErrorKind, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.kind: ChatErrorKind = kind
        self.message = message
        self.reason = reason

    def to_result(self) -> dict[str, Any]:
        """Render as the tool's error result."""
        return {
            "isError": True,
            "content": [{"type": "text", "text": self.message}],
            "error_kind": self.kind,
        }


def _error_for(exc: CrmError) -> ChatTargetError:
    kind: ChatErrorKind
    if isinstance(exc, AuthenticationFailed | ScopeDenied):
        kind = "denied"
    elif isinstance(exc, ValidationFailed | NotFound):
        kind = "invalid_request"
    else:
        kind = "model_failure"
    return ChatTargetError(kind, exc.message, reason=exc.code)


def _gateway_identity(event: dict[str, Any]) -> Identity:
    try:
        identity = decode_identity(event)
    except AuthenticationFailed as exc:
        raise ChatTargetError("denied", exc.message, reason="authentication_failed") from exc
    if identity.source != "gateway":
        raise ChatTargetError("denied", _DENIED_MESSAGE, reason=f"channel_{identity.source}")
    return identity


def _arguments(event: dict[str, Any]) -> tuple[str, str | None]:
    arguments = strip_identity(event)
    unexpected = sorted(set(arguments) - _ARGUMENT_KEYS)
    if unexpected:
        raise ChatTargetError(
            "invalid_request",
            f"Unrecognised argument(s): {unexpected}.",
            reason="unexpected_argument",
        )
    message = arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ChatTargetError("invalid_request", "A message is required.", reason="no_message")
    if len(message) > MESSAGE_MAX_CHARS:
        raise ChatTargetError(
            "invalid_request",
            f"The message is longer than {MESSAGE_MAX_CHARS} characters.",
            reason="message_too_long",
        )
    conversation_id = arguments.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise ChatTargetError(
            "invalid_request", "conversation_id must be a string.", reason="bad_conversation_id"
        )
    return message, conversation_id


def agent_identity(identity: Identity, *, session_id: str) -> dict[str, Any]:
    """Build the ``_agent_identity`` block the Runtime decodes for ``identity``.

    ``claims`` carries exactly the four custom claims :func:`crm.envelope.identity_from_claims`
    reads, and ``principal_type`` sits beside them, outside ``claims``. Group membership is never
    forwarded.
    """
    return {
        "claims": {
            CLAIM_REP_ID: identity.rep_id,
            CLAIM_BOOK: sorted(identity.book),
            CLAIM_FULL_VISIBILITY: "true" if identity.full_visibility else "false",
            CLAIM_TENANT: identity.tenant,
        },
        "session_id": session_id,
        "principal_type": identity.principal_type,
    }


def runtime_payload(
    *, message: str, conversation_id: str, tuning: Mapping[str, object], identity: Identity
) -> dict[str, Any]:
    """Build the Runtime invocation body; its only identity is :func:`agent_identity`'s block."""
    return {
        "prompt": message,
        "session_id": conversation_id,
        "tuning": dict(tuning),
        "_agent_identity": agent_identity(identity, session_id=conversation_id),
    }


def _iter_lines(stream: Any) -> Iterator[str]:
    buffer = b""
    while True:
        chunk = stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        buffer += bytes(chunk)
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8").rstrip("\r")
    if buffer:
        yield buffer.decode("utf-8").rstrip("\r")


def _frame(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    try:
        frame = json.loads(line[len("data:") :].strip())
    except ValueError as exc:
        raise ChatTargetError(
            "model_failure", _UNAVAILABLE_MESSAGE, reason="malformed_frame"
        ) from exc
    if not isinstance(frame, dict):
        raise ChatTargetError("model_failure", _UNAVAILABLE_MESSAGE, reason="malformed_frame")
    return frame


def read_answer(stream: Any, *, deadline: float, clock: Callable[[], float]) -> str:
    """Read Runtime SSE frames until ``completed`` and return its ``text``.

    Args:
        stream: The response byte stream (``read(n)`` returning ``b""`` at end).
        deadline: The ``clock()`` value after which the call times out.
        clock: Monotonic clock.

    Raises:
        ChatTargetError: An ``error`` frame (mapped kind), a malformed or unknown frame, the stream
            ending without a terminal frame (``model_failure``), or the deadline (``timeout``).
    """
    for line in _iter_lines(stream):
        if clock() > deadline:
            raise ChatTargetError("timeout", _TIMEOUT_MESSAGE, reason="deadline")
        frame = _frame(line)
        if frame is None:
            continue
        kind = frame.get("kind")
        if kind == "completed":
            text = frame.get("text")
            if not isinstance(text, str):
                raise ChatTargetError("model_failure", _UNAVAILABLE_MESSAGE, reason="no_text")
            return text
        if kind == "error":
            mapped = _RUNTIME_ERROR_KINDS.get(str(frame.get("error_kind")), "model_failure")
            message = frame.get("error_message")
            raise ChatTargetError(
                mapped,
                message if isinstance(message, str) and message else _UNAVAILABLE_MESSAGE,
                reason=f"runtime_{frame.get('error_kind')}",
            )
        if kind not in _IGNORED_FRAME_KINDS:
            raise ChatTargetError("model_failure", _UNAVAILABLE_MESSAGE, reason="unknown_frame")
    raise ChatTargetError("model_failure", _UNAVAILABLE_MESSAGE, reason="stream_ended")


def _run_turn(
    runtime: RuntimeClient,
    config: ChatTargetConfig,
    *,
    payload: Mapping[str, Any],
    conversation_id: str,
    deadline: float,
    clock: Callable[[], float],
) -> str:
    """Invoke the Runtime and read its answer, closing the response stream on every path."""
    try:
        response = runtime.invoke_agent_runtime(
            agentRuntimeArn=config.runtime_arn,
            qualifier=config.runtime_qualifier,
            runtimeSessionId=conversation_id,
            contentType="application/json",
            accept="text/event-stream",
            payload=json.dumps(payload).encode("utf-8"),
        )
    except ReadTimeoutError as exc:
        raise ChatTargetError("timeout", _TIMEOUT_MESSAGE, reason="read_timeout") from exc
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "ClientError")
        raise ChatTargetError("model_failure", _UNAVAILABLE_MESSAGE, reason=code) from exc
    except BotoCoreError as exc:
        raise ChatTargetError(
            "model_failure", _UNAVAILABLE_MESSAGE, reason=type(exc).__name__
        ) from exc
    stream = response.get("response")
    if stream is None:
        raise ChatTargetError("model_failure", _UNAVAILABLE_MESSAGE, reason="no_response_stream")
    try:
        return read_answer(stream, deadline=deadline, clock=clock)
    except ReadTimeoutError as exc:
        raise ChatTargetError("timeout", _TIMEOUT_MESSAGE, reason="read_timeout") from exc
    except (BotoCoreError, UnicodeDecodeError) as exc:
        raise ChatTargetError(
            "model_failure", _UNAVAILABLE_MESSAGE, reason=type(exc).__name__
        ) from exc
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


def _log_failure(
    exc: ChatTargetError, *, session_id: str, rep_id: str, principal_type: str
) -> None:
    if exc.kind in ("model_failure", "timeout"):
        logger.error(
            json.dumps(
                {
                    "event": "chat_target_failed",
                    "reason": exc.reason,
                    "session_id": session_id,
                    "rep_id": rep_id,
                    "principal_type": principal_type,
                }
            )
        )
    else:
        logger.warning(
            json.dumps(
                {
                    "event": "chat_target_refused",
                    "reason": exc.reason,
                    "session_id": session_id,
                    "rep_id": rep_id,
                    "principal_type": principal_type,
                }
            )
        )


def handle(
    event: object,
    *,
    store: Store,
    runtime: RuntimeClient,
    config: ChatTargetConfig,
    matrix: CapabilityMatrix,
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: float = CHAT_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Run one ``chat_with_supervisor`` call with injected dependencies.

    Args:
        deadline_seconds: Seconds from entry after which no further stream line is accepted
            (:func:`effective_deadline_seconds`).

    Returns:
        ``{"answer", "conversation_id"}`` on success, or a :meth:`ChatTargetError.to_result`.
    """
    deadline = clock() + deadline_seconds
    session_id = ""
    rep_id = ""
    principal_type = ""
    try:
        if not isinstance(event, dict):
            raise ChatTargetError(
                "invalid_request", "The request is malformed.", reason="not_object"
            )
        identity = _gateway_identity(event)
        rep_id, principal_type = identity.rep_id, identity.principal_type
        message, requested_id = _arguments(event)
        session_id = requested_id or ""
        try:
            conversation_id = conversations.claim_or_verify(store, requested_id, identity)
            session_id = conversation_id
            settings = user_settings.get_settings(store, identity, matrix=matrix)
        except CrmError as exc:
            raise _error_for(exc) from exc
        if settings.notice is not None:
            logger.warning(
                json.dumps(
                    {
                        "event": "chat_target_settings_replaced_by_defaults",
                        "reason": settings.notice,
                        "session_id": session_id,
                        "rep_id": rep_id,
                    }
                )
            )
        answer = _run_turn(
            runtime,
            config,
            payload=runtime_payload(
                message=message,
                conversation_id=conversation_id,
                tuning=settings.tuning,
                identity=identity,
            ),
            conversation_id=conversation_id,
            deadline=deadline,
            clock=clock,
        )
    except ChatTargetError as exc:
        _log_failure(exc, session_id=session_id, rep_id=rep_id, principal_type=principal_type)
        return exc.to_result()
    except Exception as exc:  # noqa: BLE001 - every failure must reach the caller as isError.
        logger.error(
            json.dumps(
                {
                    "event": "chat_target_failed",
                    "reason": f"unhandled_{type(exc).__name__}",
                    "session_id": session_id,
                    "rep_id": rep_id,
                    "principal_type": principal_type,
                }
            ),
            exc_info=True,
        )
        return ChatTargetError(
            "model_failure", _UNAVAILABLE_MESSAGE, reason="unhandled"
        ).to_result()
    return {"answer": answer, "conversation_id": conversation_id}


@functools.cache
def _default_config() -> ChatTargetConfig:
    return ChatTargetConfig.from_env()


@functools.cache
def _default_runtime_client() -> RuntimeClient:
    import boto3
    from botocore.config import Config

    client: RuntimeClient = boto3.client(
        "bedrock-agentcore",
        config=Config(
            connect_timeout=10,
            read_timeout=READ_TIMEOUT_SECONDS,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    return client


@functools.cache
def _default_matrix() -> CapabilityMatrix:
    return load_capability_matrix()


def _remaining_seconds(context: object) -> float | None:
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    return remaining() / 1000.0 if callable(remaining) else None


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """AWS Lambda entry point; builds the store, Runtime client and matrix from the environment."""
    config = _default_config()
    return handle(
        event,
        store=Store(table_prefix=config.table_prefix),
        runtime=_default_runtime_client(),
        config=config,
        matrix=_default_matrix(),
        deadline_seconds=effective_deadline_seconds(_remaining_seconds(context)),
    )


__all__ = [
    "CHAT_DEADLINE_SECONDS",
    "MESSAGE_MAX_CHARS",
    "READ_TIMEOUT_SECONDS",
    "TOOL_NAME",
    "ChatTargetConfig",
    "ChatTargetError",
    "RuntimeClient",
    "agent_identity",
    "effective_deadline_seconds",
    "handle",
    "lambda_handler",
    "read_answer",
    "runtime_payload",
]
