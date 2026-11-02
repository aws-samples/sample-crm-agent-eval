# SPDX-License-Identifier: MIT
"""``capabilities`` Lambda handler: serve the per-model capability matrix.

``GET /api/capabilities`` returns ``dataclasses.asdict(load_capability_matrix())``, the payload
``providers.capabilities`` serialises from ``config/model_capabilities.yaml``. It is not an action
tool: it composes :mod:`crm.handlers._common`'s public plumbing rather than
:func:`~crm.handlers._common.build_handler`.

Any authenticated caller may read the matrix; an unauthenticated one gets
:class:`~crm.errors.AuthenticationFailed` (401). Arguments in the request body or the query string
are refused.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from crm.errors import CrmError, ValidationFailed, bounded_detail_value
from crm.handlers._common import decode, error_response, is_proxy_event, ok

_logger = logging.getLogger(__name__)

#: Message returned when the matrix cannot be read, as ``internal_error`` (500).
_UNAVAILABLE_MESSAGE = "The model capability matrix could not be read."

#: Envelope keys carrying a GET's query string; both are refused.
_QUERY_KEYS = ("queryStringParameters", "multiValueQueryStringParameters")


def _require_no_arguments(arguments: dict[str, Any]) -> None:
    """Refuse any request-body argument, since this operation recognises none.

    Raises:
        ValidationFailed: Any key survived identity stripping.
    """
    unexpected = sorted(arguments)
    if unexpected:
        raise ValidationFailed(
            "This operation accepts no arguments.",
            source="arguments",
            detail=f"unexpected={[bounded_detail_value(key) for key in unexpected[:8]]}",
        )


def _require_no_query_arguments(event: dict[str, Any]) -> None:
    """Refuse a query string, the channel a GET client puts arguments on.

    Called only after :func:`~crm.handlers._common.decode` has returned, so ``event`` is known to be
    a mapping.

    Raises:
        ValidationFailed: The envelope carries a non-empty query string.
    """
    for key in _QUERY_KEYS:
        supplied = event.get(key)
        if isinstance(supplied, dict) and supplied:
            raise ValidationFailed(
                "This operation accepts no query parameters.",
                source="query",
                detail="the capability matrix read accepts no query parameters",
            )


def _capability_matrix_payload() -> dict[str, Any]:
    """Return the capability matrix as the wire mapping.

    Raises:
        CrmError: The matrix could not be imported, read, parsed, or serialised, reported as
            ``internal_error`` after being logged. A :class:`~crm.errors.CrmError` raised by the reader
            is re-raised unchanged.
    """
    try:
        # Deferred so importing this module reads no file.
        from providers.capabilities import load_capability_matrix

        return dataclasses.asdict(load_capability_matrix())
    except CrmError:
        raise
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _logger.exception(json.dumps({"event": "capability_matrix_unavailable", "reason": reason}))
        raise CrmError(_UNAVAILABLE_MESSAGE, detail=reason) from exc


def handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    """AWS Lambda entry point for ``GET /api/capabilities``.

    Args:
        event: The raw Lambda event, from API Gateway's proxy envelope or a direct invoke.
        context: The Lambda context, unused.
    """
    del context
    # Read before `decode`, so a failure to authenticate is still rendered in the envelope's shape.
    proxy = is_proxy_event(event)
    try:
        _identity, arguments = decode(event)
        _require_no_arguments(arguments)
        _require_no_query_arguments(event)
        payload = _capability_matrix_payload()
    except CrmError as exc:
        return error_response(exc, proxy=proxy)
    return ok(payload, proxy=proxy)


__all__ = ["handler"]
