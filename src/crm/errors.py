# SPDX-License-Identifier: MIT
"""Closed error taxonomy for the CRM API surface.

Every failure crossing the API boundary is one of six named members or the generic
``internal_error`` base. Each carries a user-safe ``message``, a ``detail`` that is only logged, and a
class-level :data:`NextStep` rendered as ``error.next_step``.
:func:`wrap_aws_error` converts an AWS SDK exception into one of them, or into :class:`ServiceBusy`
or :class:`TableNotFound`, which render as ``internal_error``. :class:`TurnTimeout` and
:class:`ProviderUnavailable` cross only the Runtime's ``eval_turn`` boundary to the sweep-task
Lambda.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal, TypedDict

#: What a caller should do next after an error: correct its arguments, report to the user,
#: refresh and confirm with the user, retry after a pause, or stop.
NextStep = Literal["fix_arguments", "tell_user", "refresh_then_ask_user", "retry_later", "stop"]


class CrmError(Exception):
    """Base class for every error the API surface may return.

    Args:
        message: Text safe to display to an end user or return to an MCP client.
        detail: Internal diagnostic context. Logged, never returned across the boundary.
    """

    http_status: int = 500
    code: str = "internal_error"
    next_step: NextStep = "stop"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_api_response(self) -> dict[str, Any]:
        """Render the wire form returned through API Gateway. Excludes ``detail``."""
        payload: dict[str, Any] = {
            "statusCode": self.http_status,
            "error": {"code": self.code, "message": self.message},
        }
        payload["error"]["next_step"] = self.next_step
        return payload

    def to_mcp_error(self) -> dict[str, Any]:
        """Render the MCP tool error form. Preserves the user-safe message, discards detail."""
        return {"isError": True, "content": [{"type": "text", "text": self.message}]}


class AuthenticationFailed(CrmError):
    """No usable caller identity: absent, expired, or malformed credential or identity envelope."""

    http_status = 401
    code = "authentication_failed"


class ScopeDenied(CrmError):
    """The caller is authenticated but the target lies outside its book of accounts."""

    http_status = 403
    code = "scope_denied"
    next_step: NextStep = "tell_user"

    def __init__(self, message: str, *, missing_scope: str, detail: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.missing_scope = missing_scope

    def to_api_response(self) -> dict[str, Any]:
        """Include the missing scope by name so the caller can explain the denial."""
        payload = super().to_api_response()
        payload["error"]["missing_scope"] = self.missing_scope
        return payload

    def to_mcp_error(self) -> dict[str, Any]:
        """Name the missing scope in the tool error so the agent can surface it verbatim."""
        return {
            "isError": True,
            "content": [
                {"type": "text", "text": f"{self.message} (missing scope: {self.missing_scope})"}
            ],
        }


class NotFound(CrmError):
    """The target does not exist within a scope the caller is permitted to see."""

    http_status = 404
    code = "not_found"
    next_step: NextStep = "tell_user"


class ValidationFailed(CrmError):
    """Input violated a schema, a field allowlist, an enum, or a tuning constraint.

    ``source`` names where the offending value came from (a CLI flag, an environment variable, or a
    UI control).
    """

    http_status = 400
    code = "validation_failed"
    next_step: NextStep = "fix_arguments"

    def __init__(
        self, message: str, *, source: str | None = None, detail: str | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.source = source

    def to_api_response(self) -> dict[str, Any]:
        """Include the offending source when one is known."""
        payload = super().to_api_response()
        if self.source is not None:
            payload["error"]["source"] = self.source
        return payload


#: Why a :class:`Conflict` was raised, when the raiser can say.
ConflictReason = Literal["fields_changed", "target_missing", "already_decided"]


class ConflictingField(TypedDict):
    """One field whose value moved between a proposal's snapshot and the current record."""

    field: str
    snapshot: object
    current: object
    proposed: object


class Conflict(CrmError):
    """The operation cannot apply against current state.

    Covers a stale proposal before-snapshot, a second approval of an already-decided proposal, a
    rebase of a decided proposal (``already_decided``) or of one whose target is gone
    (``target_missing``), a repeated revert, a revert of a ``proposal_rebased`` record, and an
    idempotency-key collision carrying a different payload.

    Args:
        message: User-safe text.
        reason: Optional machine-readable cause, rendered as ``error.reason``.
        conflicting_fields: Optional per-field values, rendered as ``error.conflicting_fields``.
        detail: Internal diagnostic context, logged only.
    """

    http_status = 409
    code = "conflict"
    next_step: NextStep = "refresh_then_ask_user"

    def __init__(
        self,
        message: str,
        *,
        reason: ConflictReason | None = None,
        conflicting_fields: Sequence[Mapping[str, object]] | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.reason = reason
        self.conflicting_fields: list[ConflictingField] | None = (
            None
            if conflicting_fields is None
            else [
                ConflictingField(
                    field=str(entry["field"]),
                    snapshot=entry.get("snapshot"),
                    current=entry.get("current"),
                    proposed=entry.get("proposed"),
                )
                for entry in conflicting_fields
            ]
        )

    def to_api_response(self) -> dict[str, Any]:
        """Include ``reason`` and ``conflicting_fields`` when known."""
        payload = super().to_api_response()
        if self.reason is not None:
            payload["error"]["reason"] = self.reason
        if self.conflicting_fields is not None:
            payload["error"]["conflicting_fields"] = [dict(f) for f in self.conflicting_fields]
        return payload


#: The user-safe message :class:`ContactDeparted` carries by default.
CONTACT_DEPARTED_MESSAGE: Final[str] = (
    "That contact has left the company. Reactivate them or choose another contact."
)


class ContactDeparted(CrmError):
    """The target contact has left the company and the update does not reactivate it.

    Renders ``error.missing_scope`` as ``status:active``, the change that would make the update
    acceptable.
    """

    http_status = 409
    code = "contact_departed"
    next_step: NextStep = "tell_user"
    missing_scope: Final[str] = "status:active"

    def __init__(
        self,
        message: str = CONTACT_DEPARTED_MESSAGE,
        *,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)

    def to_api_response(self) -> dict[str, Any]:
        """Include the missing scope by name so the caller can explain the refusal."""
        payload = super().to_api_response()
        payload["error"]["missing_scope"] = self.missing_scope
        return payload

    def to_mcp_error(self) -> dict[str, Any]:
        """Name the missing scope in the tool error so the agent can surface it verbatim."""
        return {
            "isError": True,
            "content": [
                {"type": "text", "text": f"{self.message} (missing scope: {self.missing_scope})"}
            ],
        }


class TurnTimeout(CrmError):
    """An eval turn outlived its wall-clock deadline and was cancelled.

    Raised by the Runtime's ``eval_turn`` mode and classified by the sweep-task Lambda; outside
    :data:`TAXONOMY`, which no action handler raises it through.
    """

    http_status = 504
    code = "turn_timeout"


class ProviderUnavailable(CrmError):
    """A live model provider failed after its bounded retries, outside the exclusion taxonomy.

    Raised and classified like :class:`TurnTimeout`; outside :data:`TAXONOMY`.
    """

    http_status = 503
    code = "provider_unavailable"


class ServiceBusy(CrmError):
    """A transient capacity limit on a backing service; the same request may succeed later.

    Renders as ``internal_error`` with ``next_step`` ``retry_later``; outside :data:`TAXONOMY`.
    """

    next_step: NextStep = "retry_later"


class TableNotFound(CrmError):
    """A backing table does not exist (a deployment fault, never an absent record).

    Renders as ``internal_error``; outside :data:`TAXONOMY`.
    """


#: Longest prefix of a caller-supplied value that a ``detail`` string carries.
DETAIL_VALUE_LIMIT: Final[int] = 64


def bounded_detail_value(value: object) -> str:
    """``repr`` of ``str(value)`` cut to :data:`DETAIL_VALUE_LIMIT` characters, for a ``detail``."""
    return repr(str(value)[:DETAIL_VALUE_LIMIT])


#: The members an action handler may return across the API surface.
TAXONOMY: Final[tuple[type[CrmError], ...]] = (
    AuthenticationFailed,
    ScopeDenied,
    NotFound,
    ValidationFailed,
    Conflict,
    ContactDeparted,
)

#: A mapped entry's constructor: takes ``message`` and returns a :class:`CrmError`.
_ErrorFactory = Callable[[str], CrmError]

# AWS error code -> (taxonomy factory, user-facing message). Unmapped codes become a generic
# internal error.
_AWS_CODE_MAP: Final[dict[str, tuple[_ErrorFactory, str]]] = {
    "ConditionalCheckFailedException": (
        Conflict,
        "The record changed before this operation could be applied.",
    ),
    "TransactionCanceledException": (
        Conflict,
        "The operation conflicted with a concurrent change.",
    ),
    "IdempotentParameterMismatchException": (
        Conflict,
        "This transaction key was already used. If this is a retry, the earlier request may "
        "have been applied; refresh before trying again.",
    ),
    "ResourceNotFoundException": (TableNotFound, "The request could not be completed."),
    "ValidationException": (ValidationFailed, "The request was not valid."),
    "ItemCollectionSizeLimitExceededException": (
        Conflict,
        "The record set is too large to modify.",
    ),
    "ProvisionedThroughputExceededException": (ServiceBusy, "The service is busy. Retry shortly."),
    "AccessDeniedException": (
        AuthenticationFailed,
        "The service is not configured to perform this operation.",
    ),
    "ExpiredTokenException": (AuthenticationFailed, "The session has expired. Sign in again."),
}


def wrap_aws_error(exc: Exception, *, operation: str) -> CrmError:
    """Convert an AWS SDK exception into a taxonomy member.

    The returned error's ``message`` is a fixed user-safe string; the original exception text goes
    to ``detail`` for logging.

    Args:
        exc: The exception raised by boto3 or botocore.
        operation: Name of the logical operation, recorded in ``detail`` for correlation.

    Returns:
        A :class:`CrmError` subclass instance ready to render.
    """
    aws_code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error_block = response.get("Error")
        if isinstance(error_block, dict):
            aws_code = str(error_block.get("Code", ""))

    detail = (
        f"operation={operation} aws_code={aws_code or 'unknown'} exc={type(exc).__name__}: {exc}"
    )

    mapped = _AWS_CODE_MAP.get(aws_code)
    if mapped is not None:
        factory, message = mapped
        error = factory(message)
        error.detail = detail
        return error

    return CrmError("The request could not be completed.", detail=detail)
