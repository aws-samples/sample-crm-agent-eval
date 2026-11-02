# SPDX-License-Identifier: MIT
"""Assert the error taxonomy is closed and that no internal detail crosses the API boundary."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from crm.errors import (
    TAXONOMY,
    AuthenticationFailed,
    Conflict,
    ContactDeparted,
    CrmError,
    NotFound,
    ProviderUnavailable,
    ScopeDenied,
    ServiceBusy,
    TableNotFound,
    TurnTimeout,
    ValidationFailed,
    wrap_aws_error,
)

EXPECTED_STATUSES = {
    AuthenticationFailed: 401,
    ScopeDenied: 403,
    NotFound: 404,
    ValidationFailed: 400,
    Conflict: 409,
    ContactDeparted: 409,
}


def test_taxonomy_is_exactly_the_documented_set() -> None:
    """The taxonomy is exactly the six members named in `EXPECTED_STATUSES`, with no duplicates."""
    assert set(TAXONOMY) == set(EXPECTED_STATUSES)
    assert len(TAXONOMY) == len(EXPECTED_STATUSES), (
        "a member exists twice or EXPECTED_STATUSES lost one"
    )


@pytest.mark.parametrize(("error_class", "status"), EXPECTED_STATUSES.items())
def test_each_member_maps_to_its_documented_status(
    error_class: type[CrmError], status: int
) -> None:
    """The HTTP mapping is part of the published contract."""
    assert error_class.http_status == status


@pytest.mark.parametrize("error_class", TAXONOMY)
def test_each_member_declares_a_distinct_machine_code(error_class: type[CrmError]) -> None:
    """Clients branch on `code`, so it must be present and specific."""
    assert error_class.code
    assert error_class.code != CrmError.code


def test_codes_are_unique_across_the_taxonomy() -> None:
    """A duplicated code would make two different outcomes indistinguishable."""
    codes = [member.code for member in TAXONOMY]
    assert len(codes) == len(set(codes))


def _build(error_class: type[CrmError]) -> CrmError:
    """Construct a member with an internal detail attached."""
    if error_class is ScopeDenied:
        return ScopeDenied("nope", missing_scope="account:A-1", detail="SECRET_INTERNAL")
    return error_class("nope", detail="SECRET_INTERNAL")


@pytest.mark.parametrize("error_class", TAXONOMY)
def test_api_response_never_leaks_internal_detail(error_class: type[CrmError]) -> None:
    """An API response never carries the internal `detail`."""
    payload = _build(error_class).to_api_response()
    assert "SECRET_INTERNAL" not in repr(payload)
    assert "detail" not in payload["error"]


@pytest.mark.parametrize("error_class", TAXONOMY)
def test_mcp_error_never_leaks_internal_detail(error_class: type[CrmError]) -> None:
    """MCP tool errors preserve the user-safe message and discard internal detail."""
    payload = _build(error_class).to_mcp_error()
    assert payload["isError"] is True
    assert "SECRET_INTERNAL" not in repr(payload)


def test_scope_denied_names_the_missing_scope_in_both_renderings() -> None:
    """A denial names the missing scope in both the API and MCP renderings."""
    error = ScopeDenied("You do not have access.", missing_scope="account:A-9")
    assert error.to_api_response()["error"]["missing_scope"] == "account:A-9"
    assert "account:A-9" in error.to_mcp_error()["content"][0]["text"]


def test_scope_denied_is_not_a_subclass_of_not_found() -> None:
    """`ScopeDenied` and `NotFound` are unrelated classes with different statuses."""
    assert not issubclass(ScopeDenied, NotFound)
    assert not issubclass(NotFound, ScopeDenied)
    assert ScopeDenied.http_status != NotFound.http_status


def test_validation_failed_names_the_offending_source() -> None:
    """A rejected tuning value must say where it came from."""
    error = ValidationFailed("effort 'max' is not accepted", source="--effort CLI flag")
    assert error.to_api_response()["error"]["source"] == "--effort CLI flag"


def test_validation_failed_omits_source_when_unknown() -> None:
    """An absent source must not render as a null or empty string."""
    assert "source" not in ValidationFailed("bad input").to_api_response()["error"]


def _client_error(code: str) -> ClientError:
    """Build a realistic botocore ClientError for the given service error code."""
    return ClientError({"Error": {"Code": code, "Message": "raw AWS text"}}, "PutItem")


@pytest.mark.parametrize(
    ("aws_code", "expected"),
    [
        ("ConditionalCheckFailedException", Conflict),
        ("TransactionCanceledException", Conflict),
        ("ResourceNotFoundException", TableNotFound),
        ("ValidationException", ValidationFailed),
        ("AccessDeniedException", AuthenticationFailed),
        ("ExpiredTokenException", AuthenticationFailed),
        ("ProvisionedThroughputExceededException", ServiceBusy),
    ],
)
def test_known_aws_codes_map_onto_the_taxonomy(aws_code: str, expected: type[CrmError]) -> None:
    """Each known AWS error code maps onto its taxonomy member."""
    assert isinstance(wrap_aws_error(_client_error(aws_code), operation="approve"), expected)


def test_unknown_aws_code_becomes_a_generic_internal_error() -> None:
    """An unmapped service error must not be guessed into a client-fault status."""
    wrapped = wrap_aws_error(_client_error("SomeBrandNewException"), operation="write")
    assert type(wrapped) is CrmError
    assert wrapped.http_status == 500


def test_wrapped_aws_error_never_exposes_raw_sdk_text() -> None:
    """No raw AWS SDK exception crosses the API boundary."""
    wrapped = wrap_aws_error(_client_error("ConditionalCheckFailedException"), operation="approve")
    assert "raw AWS text" not in wrapped.message
    assert "raw AWS text" not in repr(wrapped.to_api_response())
    assert "raw AWS text" not in repr(wrapped.to_mcp_error())


def test_wrapped_aws_error_retains_diagnostics_internally() -> None:
    """The detail must survive for logs, or debugging becomes impossible."""
    wrapped = wrap_aws_error(_client_error("ConditionalCheckFailedException"), operation="approve")
    assert wrapped.detail is not None
    assert "ConditionalCheckFailedException" in wrapped.detail
    assert "operation=approve" in wrapped.detail


def test_non_aws_exception_is_still_wrapped_safely() -> None:
    """A plain exception with no `response` attribute must not crash the wrapper."""
    wrapped = wrap_aws_error(RuntimeError("boom internal"), operation="search")
    assert type(wrapped) is CrmError
    assert "boom internal" not in wrapped.message
    assert wrapped.detail is not None and "boom internal" in wrapped.detail


# --------------------------------------------------------------------------------------
# _AWS_CODE_MAP factory shape
# --------------------------------------------------------------------------------------


def test_a_scope_denied_factory_constructs_without_the_required_keyword_missing() -> None:
    """A one-argument factory wrapping `ScopeDenied` with a `missing_scope` constructs cleanly."""

    def _scope_denied_factory(message: str) -> ScopeDenied:
        return ScopeDenied(message, missing_scope="account")

    error = _scope_denied_factory("You do not have access to that account.")
    assert isinstance(error, ScopeDenied)
    assert error.missing_scope == "account"


def test_bare_scope_denied_class_is_rejected_as_an_aws_code_map_factory_by_mypy() -> None:
    """`ScopeDenied` cannot be called with only a message."""
    with pytest.raises(TypeError):
        ScopeDenied("nope")  # type: ignore[call-arg]


def test_every_currently_mapped_factory_is_callable_with_only_a_message() -> None:
    """Every live `_AWS_CODE_MAP` entry already satisfies the one-argument factory shape."""
    from crm.errors import _AWS_CODE_MAP

    for aws_code, (factory, message) in _AWS_CODE_MAP.items():
        error = factory(message)
        assert isinstance(error, CrmError), f"{aws_code}: factory did not return a CrmError"


@pytest.mark.parametrize("error_class", TAXONOMY)
def test_every_member_is_raisable_and_catchable_as_crm_error(
    error_class: type[CrmError],
) -> None:
    """Handlers catch CrmError once; every member must be reachable that way."""
    with pytest.raises(CrmError):
        raise _build(error_class)


EXPECTED_NEXT_STEPS = {
    CrmError: "stop",
    AuthenticationFailed: "stop",
    ScopeDenied: "tell_user",
    NotFound: "tell_user",
    ValidationFailed: "fix_arguments",
    Conflict: "refresh_then_ask_user",
    ContactDeparted: "tell_user",
    ServiceBusy: "retry_later",
    TableNotFound: "stop",
    TurnTimeout: "stop",
    ProviderUnavailable: "stop",
}


@pytest.mark.parametrize(("error_class", "next_step"), EXPECTED_NEXT_STEPS.items())
def test_every_error_renders_its_class_level_next_step(
    error_class: type[CrmError], next_step: str
) -> None:
    """`error.next_step` is the class's documented action, beside `code` and `message`."""
    error = _build(error_class)
    assert error_class.next_step == next_step
    block = error.to_api_response()["error"]
    assert block["next_step"] == next_step
    assert (block["code"], block["message"]) == (error_class.code, "nope")


def test_every_taxonomy_member_has_an_expected_next_step() -> None:
    """A new member cannot slip in without a declared next step."""
    assert set(TAXONOMY) <= set(EXPECTED_NEXT_STEPS)


def test_throughput_is_a_transient_internal_error_to_retry_later() -> None:
    """Throttled capacity is not a state conflict: it renders as `internal_error`, `retry_later`."""
    wrapped = wrap_aws_error(
        _client_error("ProvisionedThroughputExceededException"), operation="query"
    )
    assert not isinstance(wrapped, Conflict)
    assert type(wrapped) not in TAXONOMY
    block = wrapped.to_api_response()
    assert block["statusCode"] == 500
    assert block["error"]["code"] == "internal_error"
    assert block["error"]["next_step"] == "retry_later"


def test_an_iam_access_denial_does_not_tell_the_user_they_lack_rights() -> None:
    """`AccessDeniedException` is the service's own configuration, so its message names the service."""
    wrapped = wrap_aws_error(_client_error("AccessDeniedException"), operation="query")
    message = wrapped.to_api_response()["error"]["message"]
    assert "service" in message.lower()
    assert "not authorised" not in message.lower()
    assert "you" not in message.lower().split()
    assert wrapped.to_api_response()["error"]["next_step"] == "stop"


def test_the_generic_internal_error_says_stop() -> None:
    """An unmapped service error asks for no retry."""
    wrapped = wrap_aws_error(_client_error("SomeBrandNewException"), operation="write")
    assert wrapped.to_api_response()["error"]["next_step"] == "stop"


def test_a_missing_table_is_an_internal_error_and_never_an_absent_record() -> None:
    """A DynamoDB ``ResourceNotFoundException`` is a deployment fault, not ``not_found``."""
    wrapped = wrap_aws_error(_client_error("ResourceNotFoundException"), operation="query")
    assert type(wrapped) is TableNotFound
    assert not isinstance(wrapped, NotFound)
    assert type(wrapped) not in TAXONOMY
    block = wrapped.to_api_response()
    assert block["statusCode"] == 500
    assert block["error"]["code"] == "internal_error"
    assert block["error"]["next_step"] == "stop"
    assert "record" not in block["error"]["message"].lower()
