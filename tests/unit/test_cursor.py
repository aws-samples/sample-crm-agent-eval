# SPDX-License-Identifier: MIT
"""Tests for crm.cursor: opaque, caller-bound cursors on the AES-GCM and KMS paths, and their
refusal as a ``cursor`` validation failure at every handler that accepts one.
"""

from __future__ import annotations

import base64
import json
import pathlib
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from moto import mock_aws

from crm import cursor as cursor_module
from crm.authz import Identity
from crm.cursor import (
    CURSOR_KMS_KEY_ENV,
    CURSOR_MAX_LENGTH,
    bound_operation,
    decode_cursor,
    encode_cursor,
    encryption_context,
)
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import CrmError, ServiceBusy, ValidationFailed
from crm.handlers import (
    list_activities,
    list_audit_records,
    list_proposals,
    search_accounts,
    search_opportunities,
)
from crm.search import search_opportunities as search_opportunities_fn

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_OPERATION = "search_opportunities"
_PAGE_KEY: dict[str, Any] = {
    "opportunity_id": {"S": "opp-1"},
    "tenant": {"S": _TENANT},
    "value": {"N": "1000"},
}


def _identity(*, rep_id: str = _REP, tenant: str = _TENANT) -> Identity:
    return Identity(rep_id=rep_id, full_visibility=True, tenant=tenant)


@pytest.fixture(autouse=True)
def _local_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts on the AES-GCM path, outside a Lambda, unless it opts into KMS."""
    monkeypatch.delenv(CURSOR_KMS_KEY_ENV, raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)


def _seal_locally(plaintext: bytes, *, identity: Identity, operation: str) -> str:
    """Encrypt arbitrary plaintext exactly as the AES-GCM path would, bypassing the shape check."""
    sealed = cursor_module._local_encrypt(plaintext, encryption_context(identity, operation))
    return base64.urlsafe_b64encode(sealed).decode("ascii")


def _flip_one_byte(token: str) -> str:
    raw = bytearray(base64.urlsafe_b64decode(token.encode("ascii")))
    raw[len(raw) // 2] ^= 0x01
    return base64.urlsafe_b64encode(bytes(raw)).decode("ascii")


def _assert_cursor_refusal(exc_info: pytest.ExceptionInfo[ValidationFailed]) -> None:
    assert exc_info.value.source == "cursor"
    assert exc_info.value.http_status == 400
    assert exc_info.value.message == "The cursor is not valid."


# -- AES-GCM path ---------------------------------------------------------------------------------


def test_a_cursor_round_trips_for_the_same_caller_and_operation() -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)

    assert token is not None
    assert decode_cursor(token, identity=_identity(), operation=_OPERATION) == _PAGE_KEY


def test_no_page_key_encodes_to_no_cursor_and_no_cursor_decodes_to_no_page_key() -> None:
    assert encode_cursor(None, identity=_identity(), operation=_OPERATION) is None
    assert decode_cursor(None, identity=_identity(), operation=_OPERATION) is None
    assert decode_cursor("", identity=_identity(), operation=_OPERATION) is None


def test_a_cursor_is_url_safe_base64_and_does_not_carry_the_key_in_the_clear() -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)

    assert token is not None
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    assert b"opp-1" not in raw
    assert b"opportunity_id" not in raw


def test_two_cursors_for_the_same_key_differ() -> None:
    first = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    second = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)

    assert first != second


@pytest.mark.parametrize(
    ("identity", "operation"),
    [
        (_identity(rep_id="rep-2"), _OPERATION),
        (_identity(tenant="eval-other"), _OPERATION),
        (_identity(), "search_accounts"),
    ],
    ids=["other-rep", "other-tenant", "other-operation"],
)
def test_a_cursor_presented_by_another_caller_or_to_another_operation_is_refused(
    identity: Identity, operation: str
) -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    assert token is not None

    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(token, identity=identity, operation=operation)

    _assert_cursor_refusal(refused)


def test_a_tampered_cursor_is_refused() -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    assert token is not None

    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(_flip_one_byte(token), identity=_identity(), operation=_OPERATION)

    _assert_cursor_refusal(refused)


@pytest.mark.parametrize(
    "token",
    [
        "not-a-valid-cursor!!!",
        "abc",
        base64.urlsafe_b64encode(b"short").decode("ascii"),
        base64.urlsafe_b64encode(json.dumps(_PAGE_KEY).encode("utf-8")).decode("ascii"),
        "é" * 8,
        "A" * (CURSOR_MAX_LENGTH + 4),
    ],
    ids=["bad-alphabet", "bad-padding", "too-short", "plaintext-json", "non-ascii", "too-long"],
)
def test_a_malformed_cursor_is_refused(token: str) -> None:
    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(token, identity=_identity(), operation=_OPERATION)

    _assert_cursor_refusal(refused)


@pytest.mark.parametrize(
    "plaintext",
    [
        b'{"x":"y"}',
        b'"just a string"',
        b"[]",
        b"{}",
        b'{"x":{"S":"a","N":"1"}}',
        b'{"x":{"M":{}}}',
        b'{"x":{"N":1}}',
        b"not json",
        b"\xff\xfe",
    ],
    ids=[
        "string-value",
        "non-mapping",
        "list",
        "empty",
        "two-type-tags",
        "map-type",
        "non-string-member",
        "not-json",
        "not-utf8",
    ],
)
def test_a_correctly_sealed_cursor_that_is_not_a_key_attribute_map_is_refused(
    plaintext: bytes,
) -> None:
    token = _seal_locally(plaintext, identity=_identity(), operation=_OPERATION)

    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(token, identity=_identity(), operation=_OPERATION)

    _assert_cursor_refusal(refused)


def test_the_local_cipher_being_unavailable_is_a_configuration_error_not_a_cursor_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    real_import = builtins.__import__

    def refuse_cryptography(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("cryptography"):
            raise ModuleNotFoundError(name, name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_cryptography)

    for call in (
        lambda: encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION),
        lambda: decode_cursor(token, identity=_identity(), operation=_OPERATION),
    ):
        with pytest.raises(CrmError) as failed:
            call()
        assert type(failed.value) is CrmError


@pytest.mark.parametrize("key_value", [None, ""], ids=["unset", "empty"])
def test_a_deployed_lambda_without_the_kms_key_refuses_cursors_as_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, key_value: str | None
) -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "crm-search_opportunities")
    if key_value is not None:
        monkeypatch.setenv(CURSOR_KMS_KEY_ENV, key_value)

    for call in (
        lambda: encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION),
        lambda: decode_cursor(token, identity=_identity(), operation=_OPERATION),
    ):
        with pytest.raises(CrmError) as failed:
            call()
        assert type(failed.value) is CrmError
        assert failed.value.http_status >= 500
        assert CURSOR_KMS_KEY_ENV in str(failed.value.detail)
        assert CURSOR_KMS_KEY_ENV not in failed.value.message


def test_a_deployed_lambda_with_no_cursor_and_no_page_key_needs_no_cipher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "crm-search_opportunities")

    assert encode_cursor(None, identity=_identity(), operation=_OPERATION) is None
    assert decode_cursor(None, identity=_identity(), operation=_OPERATION) is None


def test_a_deployed_lambda_without_the_kms_key_fails_the_handler_as_internal_not_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "crm-search_opportunities")

    result = search_opportunities.handler(_event({"cursor": token}), store=_SpyStore())

    assert result["statusCode"] >= 500, result
    assert result["error"]["code"] != "validation_failed"


# -- a malformed key never reaches DynamoDB -------------------------------------------------------


class _SpyStore:
    """A store stand-in that records every attribute access and refuses to serve any of them."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"store.{name} was reached")


def test_a_sealed_string_valued_key_is_refused_before_any_store_call() -> None:
    binding = bound_operation(_OPERATION, "gsi_tenant", _TENANT)
    token = _seal_locally(b'{"x":"y"}', identity=_identity(), operation=binding)
    assert (
        cursor_module._local_decrypt(
            base64.urlsafe_b64decode(token), encryption_context(_identity(), binding)
        )
        == b'{"x":"y"}'
    )

    spy = _SpyStore()

    with pytest.raises(ValidationFailed) as refused:
        search_opportunities_fn(spy, _identity(), cursor=token)  # type: ignore[arg-type]

    _assert_cursor_refusal(refused)
    assert spy.calls == []


# -- every cursor-accepting handler answers 400, never 502 ----------------------------------------


def _event(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "rep_id": _REP,
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    return identity_event(IDENTITY_KEY, payload, arguments)


@pytest.fixture
def store() -> Iterator[Any]:
    from crm.store.keys import tenant_prefixed
    from crm.store.provisioning import create_all_tables
    from crm.store.store import Store

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        seeded = Store(client=client)
        seeded.put_item(
            "opportunities",
            {
                "opportunity_id": "opp-1",
                "tenant": _TENANT,
                "account_id": "acct-1",
                "owner_rep_id": _REP,
                "stage": "Discovery",
                "name": "Renewal",
                "name_lower": "renewal",
                "created_at": "2026-01-01T00:00:00Z",
                "tenant_account_id": tenant_prefixed(_TENANT, "acct-1"),
            },
        )
        yield seeded


_HANDLER_CASES = [
    ("search_opportunities", search_opportunities.handler, {}, "gsi_tenant", _TENANT),
    ("search_accounts", search_accounts.handler, {}, "gsi_tenant", _TENANT),
    (
        "list_activities",
        list_activities.handler,
        {"opportunity_id": "opp-1"},
        "gsi_opportunity",
        f"{_TENANT}#opp-1",
    ),
    ("list_audit_records", list_audit_records.handler, {}, "gsi_tenant", _TENANT),
    ("list_proposals", list_proposals.handler, {}, "gsi_tenant", _TENANT),
]


@pytest.mark.parametrize(
    ("operation", "handler", "arguments", "index_name", "partition_value"),
    _HANDLER_CASES,
    ids=[case[0] for case in _HANDLER_CASES],
)
@pytest.mark.parametrize(
    "kind", ["tampered", "foreign-operation", "unbound-operation", "string-valued-key"]
)
def test_every_cursor_accepting_handler_returns_a_cursor_validation_failure(
    store: Any,
    operation: str,
    handler: Any,
    arguments: dict[str, Any],
    index_name: str,
    partition_value: str,
    kind: str,
) -> None:
    binding = bound_operation(operation, index_name, partition_value)
    if kind == "tampered":
        issued = encode_cursor(_PAGE_KEY, identity=_identity(), operation=binding)
        assert issued is not None
        token = _flip_one_byte(issued)
    elif kind == "foreign-operation":
        foreign = "list_activities" if operation != "list_activities" else "list_proposals"
        foreign_binding = bound_operation(foreign, index_name, partition_value)
        token = str(encode_cursor(_PAGE_KEY, identity=_identity(), operation=foreign_binding))
    elif kind == "unbound-operation":
        token = str(encode_cursor(_PAGE_KEY, identity=_identity(), operation=operation))
    else:
        token = _seal_locally(b'{"x":"y"}', identity=_identity(), operation=binding)

    result = handler(_event({**arguments, "cursor": token}), store=store)

    assert result["statusCode"] == 400, result
    assert result["error"]["code"] == "validation_failed"
    assert result["error"]["source"] == "cursor"


# -- KMS path -------------------------------------------------------------------------------------


@pytest.fixture
def kms_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A moto KMS key named by the cursor key variable, with the module client pointed at moto."""
    with mock_aws():
        client = boto3.client("kms", region_name="us-east-1")
        key_arn = client.create_key(Description="crm-cursor")["KeyMetadata"]["Arn"]
        monkeypatch.setenv(CURSOR_KMS_KEY_ENV, key_arn)
        monkeypatch.setattr(cursor_module, "_kms_client", client)
        yield key_arn


def test_a_kms_cursor_round_trips_for_the_same_caller_and_operation(kms_key: str) -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)

    assert token is not None
    assert decode_cursor(token, identity=_identity(), operation=_OPERATION) == _PAGE_KEY


def test_a_kms_cursor_is_encrypted_under_the_named_key_with_the_caller_context(
    kms_key: str,
) -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    assert token is not None
    blob = base64.urlsafe_b64decode(token.encode("ascii"))

    decrypted = cursor_module._kms_client.decrypt(
        CiphertextBlob=blob, EncryptionContext=encryption_context(_identity(), _OPERATION)
    )

    assert decrypted["KeyId"] == kms_key
    assert json.loads(decrypted["Plaintext"]) == _PAGE_KEY


@pytest.mark.parametrize(
    ("identity", "operation"),
    [
        (_identity(rep_id="rep-2"), _OPERATION),
        (_identity(tenant="eval-other"), _OPERATION),
        (_identity(), "list_proposals"),
    ],
    ids=["other-rep", "other-tenant", "other-operation"],
)
def test_a_kms_cursor_presented_under_another_context_is_refused(
    kms_key: str, identity: Identity, operation: str
) -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    assert token is not None

    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(token, identity=identity, operation=operation)

    _assert_cursor_refusal(refused)


def test_a_tampered_kms_cursor_is_refused(kms_key: str) -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    assert token is not None

    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(_flip_one_byte(token), identity=_identity(), operation=_OPERATION)

    _assert_cursor_refusal(refused)


def test_an_aes_cursor_is_refused_once_the_kms_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)
    assert token is not None
    with mock_aws():
        client = boto3.client("kms", region_name="us-east-1")
        key_arn = client.create_key(Description="crm-cursor")["KeyMetadata"]["Arn"]
        monkeypatch.setenv(CURSOR_KMS_KEY_ENV, key_arn)
        monkeypatch.setattr(cursor_module, "_kms_client", client)

        with pytest.raises(ValidationFailed) as refused:
            decode_cursor(token, identity=_identity(), operation=_OPERATION)

    _assert_cursor_refusal(refused)


class _FailingKms:
    """A KMS client whose every call raises the configured exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def encrypt(self, **_: Any) -> Any:
        raise self._exc

    def decrypt(self, **_: Any) -> Any:
        raise self._exc


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Decrypt")


_RETRYABLE_KMS_FAILURES = [
    _client_error("ThrottlingException"),
    _client_error("KMSInternalException"),
    _client_error("DependencyTimeoutException"),
    EndpointConnectionError(endpoint_url="https://kms"),
    ConnectTimeoutError(endpoint_url="https://kms"),
    ReadTimeoutError(endpoint_url="https://kms"),
]
_DEPLOYMENT_KMS_FAILURES = [
    _client_error("DisabledException"),
    _client_error("KMSInvalidStateException"),
    _client_error("NotFoundException"),
    _client_error("AccessDeniedException"),
    _client_error("ExpiredTokenException"),
]


def _both_directions(token: str) -> list[Any]:
    return [
        lambda: encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION),
        lambda: decode_cursor(token, identity=_identity(), operation=_OPERATION),
    ]


@pytest.mark.parametrize(
    "exc",
    _RETRYABLE_KMS_FAILURES,
    ids=["throttled", "internal", "dependency-timeout", "unreachable", "connect-timeout", "read"],
)
def test_a_transient_kms_failure_is_a_retryable_service_busy(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    token = _seal_locally(b"{}", identity=_identity(), operation=_OPERATION)
    monkeypatch.setenv(CURSOR_KMS_KEY_ENV, "arn:aws:kms:us-east-1:123456789012:key/k")
    monkeypatch.setattr(cursor_module, "_kms_client", _FailingKms(exc))

    for call in _both_directions(token):
        with pytest.raises(ServiceBusy) as failed:
            call()
        assert failed.value.next_step == "retry_later"
        assert failed.value.http_status >= 500


@pytest.mark.parametrize(
    "exc",
    _DEPLOYMENT_KMS_FAILURES,
    ids=["disabled", "invalid-state", "not-found", "access-denied", "expired-token"],
)
def test_a_kms_deployment_fault_is_a_taxonomy_error_neither_400_nor_retryable(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    token = _seal_locally(b"{}", identity=_identity(), operation=_OPERATION)
    monkeypatch.setenv(CURSOR_KMS_KEY_ENV, "arn:aws:kms:us-east-1:123456789012:key/k")
    monkeypatch.setattr(cursor_module, "_kms_client", _FailingKms(exc))

    for call in _both_directions(token):
        with pytest.raises(CrmError) as failed:
            call()
        assert not isinstance(failed.value, (ValidationFailed, ServiceBusy))
        assert failed.value.http_status >= 500


@pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
def test_a_kms_encrypt_refusal_is_a_5xx_never_a_client_error(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    monkeypatch.setenv(CURSOR_KMS_KEY_ENV, "arn:aws:kms:us-east-1:123456789012:key/k")
    monkeypatch.setattr(cursor_module, "_kms_client", _FailingKms(_client_error(code)))

    with pytest.raises(CrmError) as failed:
        encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)

    assert failed.value.http_status >= 500


def test_a_kms_decrypt_validation_exception_is_a_5xx_not_a_cursor_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _seal_locally(b"{}", identity=_identity(), operation=_OPERATION)
    monkeypatch.setenv(CURSOR_KMS_KEY_ENV, "arn:aws:kms:us-east-1:123456789012:key/k")
    monkeypatch.setattr(
        cursor_module, "_kms_client", _FailingKms(_client_error("ValidationException"))
    )

    with pytest.raises(CrmError) as failed:
        decode_cursor(token, identity=_identity(), operation=_OPERATION)

    assert not isinstance(failed.value, ValidationFailed)
    assert failed.value.http_status >= 500


@pytest.mark.parametrize("value", [0, [], {}, False, 7, ["c"]], ids=repr)
def test_a_non_string_cursor_is_refused_even_when_falsy(value: object) -> None:
    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(value, identity=_identity(), operation=_OPERATION)  # type: ignore[arg-type]

    _assert_cursor_refusal(refused)


def test_a_kms_encrypt_failure_fails_the_search_rather_than_dropping_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crm.search import EXAMINATION_BUDGET

    class _FullPageStore:
        def query_gsi(self, *_: Any, **kwargs: Any) -> Any:
            rows = [
                {"opportunity_id": f"opp-{n}", "tenant": _TENANT, "account_id": "acct-1"}
                for n in range(int(kwargs["limit"]))
            ]
            return rows, {"opportunity_id": {"S": "opp-last"}, "tenant": {"S": _TENANT}}

    monkeypatch.setenv(CURSOR_KMS_KEY_ENV, "arn:aws:kms:us-east-1:123456789012:key/k")
    monkeypatch.setattr(
        cursor_module, "_kms_client", _FailingKms(_client_error("ThrottlingException"))
    )

    with pytest.raises(ServiceBusy):
        search_opportunities_fn(_FullPageStore(), _identity())  # type: ignore[arg-type]
    assert EXAMINATION_BUDGET > 0


def test_a_kms_client_that_cannot_be_built_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from botocore.exceptions import NoRegionError

    def no_region(*_: Any, **__: Any) -> Any:
        raise NoRegionError()

    monkeypatch.setenv(CURSOR_KMS_KEY_ENV, "arn:aws:kms:us-east-1:123456789012:key/k")
    monkeypatch.setattr(cursor_module, "_kms_client", None)
    monkeypatch.setattr(boto3, "client", no_region)

    with pytest.raises(CrmError) as failed:
        encode_cursor(_PAGE_KEY, identity=_identity(), operation=_OPERATION)

    assert type(failed.value) is CrmError
    assert "region" not in failed.value.message.lower()


def test_the_cursor_module_imports_only_what_every_action_zip_carries() -> None:
    import ast

    source = pathlib.Path(cursor_module.__file__).read_text(encoding="utf-8")
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
            if node.module.startswith("crm."):
                roots.add(node.module)
    first_party = {root for root in roots if root.startswith("crm.")}

    assert first_party <= {"crm.authz", "crm.errors"}, first_party
    assert "yaml" not in roots
    assert roots - {"crm", "__future__"} - first_party <= {
        "base64",
        "binascii",
        "json",
        "os",
        "threading",
        "collections",
        "typing",
        "botocore",
        "boto3",
        "cryptography",
    }, roots


def test_kms_rejecting_the_ciphertext_is_a_cursor_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _seal_locally(b"{}", identity=_identity(), operation=_OPERATION)
    monkeypatch.setenv(CURSOR_KMS_KEY_ENV, "arn:aws:kms:us-east-1:123456789012:key/k")
    monkeypatch.setattr(
        cursor_module, "_kms_client", _FailingKms(_client_error("InvalidCiphertextException"))
    )

    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(token, identity=_identity(), operation=_OPERATION)

    _assert_cursor_refusal(refused)


# -- length bound ---------------------------------------------------------------------------------

#: A page key at the application's field caps: 128-character ids, a long eval tenant, a composite
#: tenant-prefixed key, and a 256-character sort key of four-byte UTF-8 characters.
_LONG_TENANT = "eval-" + "f" * 32 + "-a1r10"
_WORST_CASE_PAGE_KEY: dict[str, Any] = {
    "account_id": {"S": "a" * 128},
    "opportunity_id": {"S": "o" * 128},
    "tenant": {"S": _LONG_TENANT},
    "tenant_account_id": {"S": f"{_LONG_TENANT}#{'a' * 128}"},
    "name_lower": {"S": "\U0001f600" * 256},
}
_WORST_CASE_BINDING = bound_operation(
    "search_opportunities", "gsi_account", f"{_LONG_TENANT}#{'a' * 128}"
)

#: Ciphertext-blob bytes real KMS adds to a symmetric-key plaintext, rounded well above observed.
_KMS_BLOB_OVERHEAD_BOUND = 256


def _worst_identity() -> Identity:
    return Identity(rep_id="r" * 128, full_visibility=True, tenant=_LONG_TENANT)


def _encoded_length(plaintext_bytes: int) -> int:
    return 4 * -(-plaintext_bytes // 3)


def _worst_plaintext_bytes() -> int:
    return len(
        json.dumps(
            _WORST_CASE_PAGE_KEY, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )


def test_a_worst_case_aes_cursor_fits_the_tool_schema_cap() -> None:
    token = encode_cursor(
        _WORST_CASE_PAGE_KEY, identity=_worst_identity(), operation=_WORST_CASE_BINDING
    )

    assert token is not None
    assert len(token) < CURSOR_MAX_LENGTH == 4000
    assert (
        decode_cursor(token, identity=_worst_identity(), operation=_WORST_CASE_BINDING)
        == _WORST_CASE_PAGE_KEY
    )


def test_a_worst_case_kms_cursor_fits_the_tool_schema_cap(kms_key: str) -> None:
    token = encode_cursor(
        _WORST_CASE_PAGE_KEY, identity=_worst_identity(), operation=_WORST_CASE_BINDING
    )

    assert token is not None
    assert len(token) < CURSOR_MAX_LENGTH
    assert _encoded_length(_worst_plaintext_bytes() + _KMS_BLOB_OVERHEAD_BOUND) < CURSOR_MAX_LENGTH
    assert (
        decode_cursor(token, identity=_worst_identity(), operation=_WORST_CASE_BINDING)
        == _WORST_CASE_PAGE_KEY
    )


def test_a_cursor_that_would_exceed_the_cap_is_never_issued() -> None:
    oversized = {"name_lower": {"S": "x" * CURSOR_MAX_LENGTH}}

    with pytest.raises(CrmError) as failed:
        encode_cursor(oversized, identity=_identity(), operation=_OPERATION)

    assert not isinstance(failed.value, ValidationFailed)


def test_every_cursor_length_cap_is_the_same_value() -> None:
    from crm.toolspec import definitions, page_context

    assert CURSOR_MAX_LENGTH == definitions.CURSOR_MAX_LENGTH == page_context.CURSOR_MAX_LENGTH


def test_a_real_kms_worst_case_length_cursor_is_accepted_as_page_context() -> None:
    from crm.toolspec.page_context import PageDescriptor

    worst = _encoded_length(_worst_plaintext_bytes() + _KMS_BLOB_OVERHEAD_BOUND)
    assert worst == 2492

    descriptor = PageDescriptor(cursor="A" * (worst - 2) + "==", limit=25)

    assert descriptor.cursor is not None
    assert len(descriptor.cursor) == worst
