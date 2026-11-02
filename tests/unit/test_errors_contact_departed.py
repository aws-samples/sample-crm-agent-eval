# SPDX-License-Identifier: MIT
"""Pin the `ContactDeparted` taxonomy member's wire forms."""

from __future__ import annotations

from crm.errors import TAXONOMY, ContactDeparted, CrmError

MESSAGE = "That contact has left the company. Reactivate them or choose another contact."


def test_contact_departed_is_a_taxonomy_member_with_its_documented_class_fields() -> None:
    """409, `contact_departed`, `tell_user`, and `status:active` as the missing scope."""
    assert ContactDeparted in TAXONOMY
    assert issubclass(ContactDeparted, CrmError)
    assert ContactDeparted.http_status == 409
    assert ContactDeparted.code == "contact_departed"
    assert ContactDeparted.next_step == "tell_user"
    assert ContactDeparted.missing_scope == "status:active"


def test_default_message_is_the_user_safe_text() -> None:
    """Constructed with no message, the error carries the documented user-safe text."""
    assert ContactDeparted().message == MESSAGE


def test_api_response_is_exactly_the_documented_payload() -> None:
    """The API body is the base payload plus `missing_scope`, and nothing else."""
    error = ContactDeparted(detail="contact_id='cont-0000'")
    assert error.to_api_response() == {
        "statusCode": 409,
        "error": {
            "code": "contact_departed",
            "message": MESSAGE,
            "next_step": "tell_user",
            "missing_scope": "status:active",
        },
    }


def test_mcp_error_is_exactly_the_documented_payload() -> None:
    """The MCP text names the missing scope after the user-safe message."""
    error = ContactDeparted(detail="contact_id='cont-0000'")
    assert error.to_mcp_error() == {
        "isError": True,
        "content": [{"type": "text", "text": f"{MESSAGE} (missing scope: status:active)"}],
    }


def test_detail_never_crosses_either_rendering() -> None:
    """The internal `detail` is kept on the instance and absent from both wire forms."""
    error = ContactDeparted(detail="SECRET_INTERNAL")
    assert error.detail == "SECRET_INTERNAL"
    api = error.to_api_response()
    assert "detail" not in api["error"]
    assert "SECRET_INTERNAL" not in repr(api)
    assert "SECRET_INTERNAL" not in repr(error.to_mcp_error())


def test_contact_departed_code_is_distinct_from_conflict() -> None:
    """Shares 409 with `Conflict` but keeps its own code, so clients can tell them apart."""
    codes = [member.code for member in TAXONOMY]
    assert codes.count("contact_departed") == 1
    assert codes.count("conflict") == 1
