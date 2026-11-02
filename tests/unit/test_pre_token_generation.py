# SPDX-License-Identifier: MIT
"""The pre-token-generation trigger adds the Gateway #2 chat scope to user access tokens.

The scope comes from env `CHAT_SCOPE`. The trigger never adds `gateway/invoke`, never touches
`cognito:groups`, and adds nothing to the ID token.
"""

from __future__ import annotations

from typing import Any

import pytest

from crm.auth.pre_token_generation import CHAT_SCOPE_ENV, lambda_handler

_CHAT_SCOPE = "supervisor/chat"


class _FakeStore:
    """Returns one canned `reps` item for any `get_item`."""

    def __init__(self, item: dict[str, Any] | None) -> None:
        self._item = item

    def get_item(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
        del table, key
        return self._item


class _FailingStore:
    def get_item(self, table: str, key: dict[str, Any]) -> dict[str, Any] | None:
        raise RuntimeError("lookup down")


def _details(store: Any = None, **event: Any) -> dict[str, Any]:
    response = lambda_handler(
        {"userName": "rep-1", **event},
        store=store if store is not None else _FakeStore({"book": ["a-1"], "tenant": "demo"}),
    )
    details: dict[str, Any] = response["response"]["claimsAndScopeOverrideDetails"]
    return details


def test_the_access_token_gains_exactly_the_chat_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAT_SCOPE_ENV, _CHAT_SCOPE)

    assert _details()["accessTokenGeneration"]["scopesToAdd"] == [_CHAT_SCOPE]


def test_the_id_token_gains_no_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAT_SCOPE_ENV, _CHAT_SCOPE)

    assert "scopesToAdd" not in _details()["idTokenGeneration"]


def test_the_chat_scope_is_added_even_when_the_rep_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scope only opens Gateway #2's door; the empty-book identity still denies every record."""
    monkeypatch.setenv(CHAT_SCOPE_ENV, _CHAT_SCOPE)

    details = _details(store=_FailingStore())

    assert details["accessTokenGeneration"]["scopesToAdd"] == [_CHAT_SCOPE]
    assert details["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:book"] == ""


@pytest.mark.parametrize(
    "configured",
    [None, "", "   ", "gateway/invoke", " gateway/invoke ", "a b", "x\tgateway/invoke"],
)
def test_an_unset_malformed_or_machine_scope_adds_nothing(
    monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    """`gateway/invoke` is never issued to a user token, whatever `CHAT_SCOPE` says."""
    if configured is None:
        monkeypatch.delenv(CHAT_SCOPE_ENV, raising=False)
    else:
        monkeypatch.setenv(CHAT_SCOPE_ENV, configured)

    scopes = _details()["accessTokenGeneration"]["scopesToAdd"]

    assert scopes == []
    assert "gateway/invoke" not in scopes


def test_groups_are_never_added_overridden_or_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cognito:groups` stays exactly what Cognito computed, including for an admin."""
    monkeypatch.setenv(CHAT_SCOPE_ENV, _CHAT_SCOPE)
    details = _details(
        request={"groupConfiguration": {"groupsToOverride": ["admin"]}},
    )

    assert "groupOverrideDetails" not in details
    for token in ("idTokenGeneration", "accessTokenGeneration"):
        generation = details[token]
        assert "claimsToSuppress" not in generation
        assert "cognito:groups" not in generation["claimsToAddOrOverride"]
        assert not any("group" in key for key in generation["claimsToAddOrOverride"])
