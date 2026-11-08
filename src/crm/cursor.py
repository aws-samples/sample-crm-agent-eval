# SPDX-License-Identifier: MIT
"""Opaque, caller-bound pagination cursors over raw DynamoDB page keys.

:func:`encode_cursor` encrypts a ``LastEvaluatedKey`` under an encryption context naming the
caller's tenant and rep and the issuing operation; :func:`decode_cursor` reverses it only for that
same caller and operation. With ``CRM_CURSOR_KMS_KEY_ARN`` set, encryption is AWS KMS
``Encrypt``/``Decrypt`` under that key; unset, outside a deployed Lambda, it is AES-GCM under a key
drawn once per process, so a cursor is valid only within the process that issued it. A deployed
Lambda (``AWS_LAMBDA_FUNCTION_NAME`` set) without the key variable refuses every cursor operation
as a configuration error. A cursor that is malformed, tampered with, or presented by another caller
or to another operation raises :class:`crm.errors.ValidationFailed` with ``source="cursor"``.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from collections.abc import Mapping
from typing import Any, Final

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    HTTPClientError,
    ParamValidationError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from crm.authz import Identity
from crm.errors import CrmError, ServiceBusy, ValidationFailed, wrap_aws_error

#: Environment variable naming the KMS key cursors are encrypted under.
CURSOR_KMS_KEY_ENV: Final[str] = "CRM_CURSOR_KMS_KEY_ARN"

#: Set by the Lambda runtime; its presence marks a deployed function.
_LAMBDA_FUNCTION_ENV: Final[str] = "AWS_LAMBDA_FUNCTION_NAME"

#: The ``purpose`` member of every cursor's encryption context.
CURSOR_PURPOSE: Final[str] = "crm-cursor"

#: Longest cursor string :func:`encode_cursor` issues and :func:`decode_cursor` will decrypt.
CURSOR_MAX_LENGTH: Final[int] = 4000

#: KMS error codes meaning the ciphertext or its context is not one this key issued.
_KMS_REJECTED_CIPHERTEXT_CODES: Final[frozenset[str]] = frozenset(
    {"InvalidCiphertextException", "IncorrectKeyException"}
)

#: DynamoDB key attribute types; a page key's values are single-member maps of one of these.
_KEY_ATTRIBUTE_TYPES: Final[frozenset[str]] = frozenset({"S", "N", "B"})

_NONCE_BYTES: Final[int] = 12

_INVALID_MESSAGE: Final[str] = "The cursor is not valid."
_UNAVAILABLE_MESSAGE: Final[str] = "The request could not be completed."
_BUSY_MESSAGE: Final[str] = "The service is busy. Retry shortly."

_lock = threading.Lock()
_kms_client: Any = None
_process_key: bytes | None = None


def encryption_context(identity: Identity, operation: str) -> dict[str, str]:
    """Return the encryption context a cursor for ``identity`` and ``operation`` is bound to."""
    return {
        "purpose": CURSOR_PURPOSE,
        "tenant": identity.tenant,
        "rep_id": identity.rep_id,
        "operation": operation,
    }


def bound_operation(operation: str, index_name: str, partition_value: str) -> str:
    """Return the ``operation`` binding for a cursor over one index partition.

    A cursor issued for one index or partition value is refused when presented for another.
    """
    return f"{operation}|{index_name}|{partition_value}"


def encode_cursor(
    page_key: Mapping[str, Any] | None, *, identity: Identity, operation: str
) -> str | None:
    """Encrypt ``page_key`` into an opaque URL-safe cursor bound to ``identity`` and ``operation``.

    Returns ``None`` when ``page_key`` is ``None``.

    Raises:
        CrmError: A taxonomy member when KMS refuses or cannot be reached, when no cipher is
            configured (see :func:`kms_key_arn`), or when the cursor would exceed
            :data:`CURSOR_MAX_LENGTH`.
    """
    if page_key is None:
        return None
    plaintext = json.dumps(
        dict(page_key), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    context = encryption_context(identity, operation)
    key_arn = kms_key_arn()
    if key_arn:
        ciphertext = _kms_encrypt(key_arn, plaintext, context)
    else:
        ciphertext = _local_encrypt(plaintext, context)
    token = base64.urlsafe_b64encode(ciphertext).decode("ascii")
    if len(token) > CURSOR_MAX_LENGTH:
        raise CrmError(_UNAVAILABLE_MESSAGE, detail=f"cursor: encoded length {len(token)}")
    return token


def decode_cursor(
    cursor: str | None, *, identity: Identity, operation: str
) -> dict[str, Any] | None:
    """Decrypt a cursor from :func:`encode_cursor` back into its raw DynamoDB page key.

    Returns ``None`` when ``cursor`` is ``None`` or the empty string.

    Raises:
        ValidationFailed: ``source="cursor"`` when the cursor is not a string, is longer than
            :data:`CURSOR_MAX_LENGTH`, is not URL-safe base64, is tampered with, was issued to
            another caller or operation, or is not a DynamoDB key-attribute map.
        CrmError: A taxonomy member when KMS cannot be reached or refuses the caller's role, when
            no cipher is configured (see :func:`kms_key_arn`), or when the local cipher library
            is unavailable.
    """
    if cursor is None:
        return None
    if not isinstance(cursor, str):
        raise _invalid("type")
    if not cursor:
        return None
    key_arn = kms_key_arn()
    if len(cursor) > CURSOR_MAX_LENGTH:
        raise _invalid("length")
    try:
        ciphertext = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise _invalid("encoding") from exc
    context = encryption_context(identity, operation)
    if key_arn:
        plaintext = _kms_decrypt(key_arn, ciphertext, context)
    else:
        plaintext = _local_decrypt(ciphertext, context)
    try:
        decoded = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _invalid("payload") from exc
    if not _is_page_key(decoded):
        raise _invalid("shape")
    return dict(decoded)


def kms_key_arn() -> str | None:
    """Return the configured cursor KMS key, or ``None`` to use the local AES-GCM cipher.

    Raises:
        CrmError: In a deployed Lambda with ``CRM_CURSOR_KMS_KEY_ARN`` unset or empty.
    """
    key_arn = os.environ.get(CURSOR_KMS_KEY_ENV)
    if key_arn:
        return key_arn
    if os.environ.get(_LAMBDA_FUNCTION_ENV):
        raise CrmError(
            _UNAVAILABLE_MESSAGE,
            detail=f"cursor: {CURSOR_KMS_KEY_ENV} unset in a deployed function",
        )
    return None


def _unavailable_cipher(exc: ImportError) -> CrmError:
    return CrmError(_UNAVAILABLE_MESSAGE, detail=f"cursor: local cipher unavailable ({exc.name})")


def _invalid(reason: str) -> ValidationFailed:
    return ValidationFailed(_INVALID_MESSAGE, source="cursor", detail=f"reason={reason}")


def _is_page_key(value: object) -> bool:
    """Return whether ``value`` is a non-empty map of names to single-member key attributes."""
    if not isinstance(value, dict) or not value:
        return False
    for name, attribute in value.items():
        if not isinstance(name, str) or not name:
            return False
        if not isinstance(attribute, dict) or len(attribute) != 1:
            return False
        ((type_tag, member),) = attribute.items()
        if type_tag not in _KEY_ATTRIBUTE_TYPES or not isinstance(member, str):
            return False
    return True


def _kms() -> Any:
    """Return the process-wide KMS client, built once from the ambient AWS configuration."""
    global _kms_client
    with _lock:
        if _kms_client is None:
            import boto3

            try:
                _kms_client = boto3.client("kms")
            except Exception as exc:  # noqa: BLE001 - any setup failure is a configuration fault
                raise CrmError(
                    _UNAVAILABLE_MESSAGE, detail=f"cursor: kms client setup {type(exc).__name__}"
                ) from exc
        return _kms_client


def _wrap_kms_error(exc: ClientError | BotoCoreError, *, operation: str) -> CrmError:
    """Map a KMS failure to the taxonomy: :class:`ServiceBusy` when transient, else a 5xx member."""
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        busy = ServiceBusy(_BUSY_MESSAGE)
        busy.detail = f"operation={operation} exc={type(exc).__name__}"
        return busy
    wrapped = wrap_aws_error(exc, operation=operation)
    if wrapped.http_status >= 500:
        return wrapped
    return CrmError(_UNAVAILABLE_MESSAGE, detail=wrapped.detail)


def _kms_encrypt(key_arn: str, plaintext: bytes, context: dict[str, str]) -> bytes:
    try:
        response = _kms().encrypt(KeyId=key_arn, Plaintext=plaintext, EncryptionContext=context)
    except (ClientError, BotoCoreError) as exc:
        raise _wrap_kms_error(exc, operation="cursor:encrypt") from exc
    blob = response.get("CiphertextBlob")
    if not isinstance(blob, bytes):
        raise CrmError(_UNAVAILABLE_MESSAGE, detail="cursor:encrypt no blob")
    return blob


def _kms_decrypt(key_arn: str, ciphertext: bytes, context: dict[str, str]) -> bytes:
    try:
        response = _kms().decrypt(
            KeyId=key_arn, CiphertextBlob=ciphertext, EncryptionContext=context
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _KMS_REJECTED_CIPHERTEXT_CODES:
            raise _invalid(f"kms:{code}") from exc
        raise _wrap_kms_error(exc, operation="cursor:decrypt") from exc
    except BotoCoreError as exc:
        if isinstance(exc, ParamValidationError):
            raise _invalid("kms:param") from exc
        raise _wrap_kms_error(exc, operation="cursor:decrypt") from exc
    plaintext = response.get("Plaintext")
    if not isinstance(plaintext, bytes):
        raise _invalid("kms:no-plaintext")
    return plaintext


def _associated_data(context: dict[str, str]) -> bytes:
    return json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _local_key() -> bytes:
    global _process_key
    with _lock:
        if _process_key is None:
            _process_key = os.urandom(32)
        return _process_key


def _local_encrypt(plaintext: bytes, context: dict[str, str]) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise _unavailable_cipher(exc) from exc
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(_local_key()).encrypt(nonce, plaintext, _associated_data(context))


def _local_decrypt(ciphertext: bytes, context: dict[str, str]) -> bytes:
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise _unavailable_cipher(exc) from exc
    if len(ciphertext) <= _NONCE_BYTES:
        raise _invalid("length")
    nonce, sealed = ciphertext[:_NONCE_BYTES], ciphertext[_NONCE_BYTES:]
    try:
        return AESGCM(_local_key()).decrypt(nonce, sealed, _associated_data(context))
    except InvalidTag as exc:
        raise _invalid("tag") from exc
