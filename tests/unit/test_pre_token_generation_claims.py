# SPDX-License-Identifier: MIT
"""Pins the wire shape of the four scope claims the pre-token-generation trigger stamps.

The producer half of the contract whose consumer halves are
`src/gateway/identity_interceptor.py::_build_verified_identity` and
`frontend/src/auth/identity.ts::parseIdentityClaims`; `frontend/src/auth/identity.test.ts` asserts
the same names and encodings. The naming contract is in `infra/modules/cognito/README.md`.
"""

from __future__ import annotations

from typing import Any

import pytest

from crm.auth.pre_token_generation import lambda_handler
from gateway.identity_interceptor import _split_book, _truthy

#: The exact claim names every consumer of a human sign-in token reads. Cognito custom attributes
#: are string-typed, so every value is a string regardless of the underlying Python type.
EXPECTED_CLAIM_NAMES = frozenset(
    {"custom:rep_id", "custom:book", "custom:full_visibility", "custom:tenant"}
)


class _FakeStore:
    """Returns one canned `reps` item for any `get_item`, recording the key it was asked for."""

    def __init__(self, item: dict[str, Any] | None) -> None:
        self._item = item
        self.requested: list[tuple[str, dict[str, Any]]] = []

    def get_item(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
        self.requested.append((table, key))
        return self._item


def _claims(event: dict[str, Any], *, token: str = "accessTokenGeneration") -> dict[str, str]:
    """Extract one token's `claimsToAddOrOverride` from a handler response."""
    details = event["response"]["claimsAndScopeOverrideDetails"]
    claims: dict[str, str] = details[token]["claimsToAddOrOverride"]
    return claims


def _invoke(item: dict[str, Any] | None, *, username: str = "rep-ashford") -> dict[str, str]:
    store = _FakeStore(item)
    return _claims(lambda_handler({"userName": username}, store=store))


def test_claim_names_are_exactly_the_four_custom_prefixed_names() -> None:
    claims = _invoke({"rep_id": "rep-ashford", "book": ["a-1"], "full_visibility": False})
    assert set(claims) == EXPECTED_CLAIM_NAMES


def test_every_claim_value_is_a_string() -> None:
    claims = _invoke({"rep_id": "rep-ashford", "book": ["a-1"], "full_visibility": True})
    assert all(isinstance(value, str) for value in claims.values())


def test_book_is_comma_joined_and_round_trips_through_the_gateway_parser() -> None:
    book = ["demo-acct-0001", "demo-acct-0009"]
    claims = _invoke({"rep_id": "rep-ashford", "book": book, "full_visibility": False})
    assert claims["custom:book"] == "demo-acct-0001,demo-acct-0009"
    assert _split_book(claims["custom:book"]) == book


@pytest.mark.parametrize(
    ("stored", "rendered"), [(True, "true"), (False, "false"), (None, "false"), ("true", "false")]
)
def test_full_visibility_renders_lowercase_and_fails_closed(stored: object, rendered: str) -> None:
    """Only a real `True` yields `"true"`; a truthy string does not widen visibility."""
    claims = _invoke({"rep_id": "rep-ashford", "book": [], "full_visibility": stored})
    assert claims["custom:full_visibility"] == rendered
    assert _truthy(claims["custom:full_visibility"]) is (rendered == "true")


def test_id_and_access_tokens_carry_identical_claims() -> None:
    """The ID and access tokens carry identical scope claims."""
    store = _FakeStore({"rep_id": "rep-ashford", "book": ["a-1"], "full_visibility": False})
    event = lambda_handler({"userName": "rep-ashford"}, store=store)
    assert _claims(event, token="idTokenGeneration") == _claims(event)  # nosec B106  # Cognito trigger source name, not a credential


def test_missing_reps_item_yields_least_privilege_claims() -> None:
    claims = _invoke(None)
    assert claims == {
        "custom:rep_id": "rep-ashford",
        "custom:book": "",
        "custom:full_visibility": "false",
        "custom:tenant": "demo",
    }


def test_username_is_the_reps_partition_key() -> None:
    store = _FakeStore(None)
    lambda_handler({"userName": "rep-brixton"}, store=store)
    assert store.requested == [("reps", {"rep_id": "rep-brixton"})]


def test_store_failure_falls_back_rather_than_failing_sign_in() -> None:
    class _Exploding:
        def get_item(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
            raise RuntimeError("table unreachable")

    claims = _claims(lambda_handler({"userName": "rep-ashford"}, store=_Exploding()))
    assert claims["custom:full_visibility"] == "false"
    assert claims["custom:book"] == ""
