# SPDX-License-Identifier: MIT
"""Invoke a deployed action Lambda directly; the transport an in-app agent reaches the CRM through.

The caller supplies an event that already carries its identity channel. A handler's taxonomy
response is returned verbatim; an unhandled function error or a non-object response body becomes a
taxonomy-shaped ``internal_error`` payload, as does a failed invoke (a ``botocore`` timeout,
connection or service error); no ``botocore`` exception is raised. The Lambda client is bounded by
:data:`CONNECT_TIMEOUT_SECONDS`, :data:`READ_TIMEOUT_SECONDS` and :data:`TOTAL_MAX_ATTEMPTS`
(:func:`client_config`).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from crm.errors import CrmError, wrap_aws_error

if TYPE_CHECKING:
    from botocore.config import Config


class LambdaInvoker(Protocol):
    """The single ``boto3`` Lambda-client method :func:`invoke_action` uses."""

    def invoke(self, **kwargs: Any) -> Any:
        """Invoke a Lambda function. See ``boto3``'s ``Lambda.Client.invoke``."""
        ...


logger = logging.getLogger(__name__)

_lambda_client: LambdaInvoker | None = None

#: Message returned when the invoked function failed in a way that carries no user-safe detail.
_INTERNAL_MESSAGE: Final[str] = "The request could not be completed."

#: Seconds to establish the connection to the Lambda endpoint.
CONNECT_TIMEOUT_SECONDS: Final[float] = 2.0

#: Seconds to wait for a synchronous invoke's response; above the action Lambdas' 30 s timeout.
READ_TIMEOUT_SECONDS: Final[float] = 35.0

#: Total attempts per invoke, the first included. A retried ``RequestResponse`` invoke would re-run
#: the handler, so no attempt is ever repeated.
TOTAL_MAX_ATTEMPTS: Final[int] = 1


def client_config() -> Config:
    """Return the ``botocore`` client configuration every action-Lambda invoke uses."""
    from botocore.config import Config

    return Config(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"mode": "standard", "total_max_attempts": TOTAL_MAX_ATTEMPTS},
    )


def lambda_client() -> LambdaInvoker:
    """Return the process-wide AWS Lambda client, built once per process with :func:`client_config`."""
    global _lambda_client
    if _lambda_client is None:
        import boto3

        _lambda_client = boto3.client("lambda", config=client_config())
    return _lambda_client


def invoke_action(
    function_name: str, event: dict[str, Any], *, client: LambdaInvoker | None = None
) -> dict[str, Any]:
    """Invoke ``function_name`` synchronously with ``event`` and return its wire response.

    Args:
        function_name: The deployed action Lambda's function name.
        event: The raw event to send, already carrying its identity channel.
        client: An override for the process-wide client, for tests.

    Returns:
        The handler's own wire response, or a taxonomy-shaped ``internal_error`` payload when the
        invoke itself failed (timeout, connection or AWS service error), the function reported an
        unhandled error, or it returned a body that is not a JSON object. Never carries the
        invoked function's or the SDK's internal error detail.
    """
    try:
        resolved = client if client is not None else lambda_client()
        response = resolved.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode("utf-8"),
        )
        raw = response["Payload"].read()
    except (BotoCoreError, ClientError) as exc:
        failure = CrmError(
            _INTERNAL_MESSAGE, detail=wrap_aws_error(exc, operation="lambda:Invoke").detail
        )
        # Log keys are limited to `scripts/check_log_redaction.py`'s allowlist.
        logger.warning(
            json.dumps({"event": "action_invoke_failed", "reason": (failure.detail or "")[:300]})
        )
        return failure.to_api_response()
    if response.get("FunctionError"):
        return CrmError(_INTERNAL_MESSAGE).to_api_response()
    decoded = json.loads(raw) if raw else None
    if not isinstance(decoded, dict):
        return CrmError(_INTERNAL_MESSAGE).to_api_response()
    return decoded


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "READ_TIMEOUT_SECONDS",
    "TOTAL_MAX_ATTEMPTS",
    "LambdaInvoker",
    "client_config",
    "invoke_action",
    "lambda_client",
]
