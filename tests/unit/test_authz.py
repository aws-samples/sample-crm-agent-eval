# SPDX-License-Identifier: MIT
"""Assert scope enforcement denies out-of-book access and leaks nothing through lists."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from crm.authz import (
    ADMIN_GROUP,
    DEMO_TENANT,
    Identity,
    IdentitySource,
    account_id_of,
    filter_to_scope,
    is_in_scope,
    require_listing_scope,
    require_record_scope,
    require_scope,
    require_tenant,
    tenant_of,
)
from crm.errors import ScopeDenied


def _rep(*accounts: str, full: bool = False, tenant: str = DEMO_TENANT) -> Identity:
    """Build a rep identity owning the given accounts."""
    return Identity(rep_id="rep-1", book=frozenset(accounts), full_visibility=full, tenant=tenant)


# --------------------------------------------------------------------------------------
# Identity construction invariants
# --------------------------------------------------------------------------------------


def test_identity_requires_a_rep_id() -> None:
    """An identity with an empty `rep_id` raises `ValueError`."""
    with pytest.raises(ValueError, match="rep_id"):
        Identity(rep_id="")


def test_identity_requires_a_tenant() -> None:
    """An identity with an empty tenant raises `ValueError`."""
    with pytest.raises(ValueError, match="tenant"):
        Identity(rep_id="rep-1", tenant="")


def test_service_principal_may_not_hold_implicit_full_visibility() -> None:
    """A service principal with full visibility raises `ValueError`."""
    with pytest.raises(ValueError, match="full visibility"):
        Identity(rep_id="svc-1", principal_type="service", full_visibility=True)


def test_identity_is_immutable() -> None:
    """An identity is a frozen dataclass."""
    import dataclasses

    identity = _rep("A-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.full_visibility = True  # type: ignore[misc]


def test_identity_defaults_to_the_demo_tenant_and_no_visibility() -> None:
    """A default identity is in the demo tenant with no book and no full visibility."""
    identity = Identity(rep_id="rep-1")
    assert identity.tenant == DEMO_TENANT
    assert identity.full_visibility is False
    assert identity.book == frozenset()
    assert identity.groups == frozenset()
    assert identity.is_admin is False


# --------------------------------------------------------------------------------------
# Group membership and is_admin
# --------------------------------------------------------------------------------------


def test_admin_group_name_is_admin() -> None:
    """The admin group is the Cognito group literally named `admin`."""
    assert ADMIN_GROUP == "admin"


def test_user_principal_in_the_admin_group_is_admin() -> None:
    """A user principal whose groups include `admin` is an admin."""
    identity = Identity(rep_id="rep-1", groups=frozenset({"admin", "reps"}))
    assert identity.is_admin is True


@pytest.mark.parametrize(
    "groups",
    [frozenset(), frozenset({"reps"}), frozenset({"Admin"}), frozenset({"admins"})],
)
def test_user_principal_outside_the_admin_group_is_not_admin(groups: frozenset[str]) -> None:
    """Membership is an exact, case-sensitive match on `admin`."""
    assert Identity(rep_id="rep-1", groups=groups).is_admin is False


def test_service_principal_is_never_admin_even_in_the_admin_group() -> None:
    """`is_admin` requires a user principal."""
    identity = Identity(rep_id="svc-1", principal_type="service", groups=frozenset({"admin"}))
    assert identity.is_admin is False


@pytest.mark.parametrize("source", ["gateway", "agent"])
def test_groups_are_rejected_outside_the_apigw_channel(source: IdentitySource) -> None:
    """Only the `apigw` channel may carry group membership."""
    with pytest.raises(ValueError, match="apigw channel only"):
        Identity(rep_id="rep-1", source=source, groups=frozenset({"admin"}))


@pytest.mark.parametrize("source", ["gateway", "agent"])
def test_empty_groups_are_accepted_on_every_channel(source: IdentitySource) -> None:
    """An empty group set is valid on the non-API channels."""
    identity = Identity(rep_id="rep-1", source=source)
    assert identity.groups == frozenset()
    assert identity.is_admin is False


# --------------------------------------------------------------------------------------
# require_scope
# --------------------------------------------------------------------------------------


def test_in_book_account_is_permitted() -> None:
    """An account in the rep's book is permitted."""
    require_scope(_rep("A-1", "A-2"), "A-2")


def test_out_of_book_account_is_denied_with_the_missing_scope_named() -> None:
    """An out-of-book account is denied with a 403 naming the missing account scope."""
    with pytest.raises(ScopeDenied) as caught:
        require_scope(_rep("A-1"), "A-999", operation="get_opportunity")
    assert caught.value.missing_scope == "account:A-999"
    assert caught.value.http_status == 403


def test_full_visibility_bypasses_the_book_check() -> None:
    """A full-visibility rep is permitted any account."""
    require_scope(_rep(full=True), "A-anything")


def test_empty_book_without_full_visibility_denies_everything() -> None:
    """A rep with an empty book and no full visibility is denied."""
    with pytest.raises(ScopeDenied):
        require_scope(_rep(), "A-1")


def test_denial_detail_never_contains_a_token_or_the_book_contents() -> None:
    """The denial detail reports the book size and never the book's account ids."""
    with pytest.raises(ScopeDenied) as caught:
        require_scope(_rep("A-SECRET-1", "A-SECRET-2"), "A-999")
    detail = caught.value.detail or ""
    assert "A-SECRET-1" not in detail
    assert "A-SECRET-2" not in detail
    assert "2-accounts" in detail


# --------------------------------------------------------------------------------------
# require_tenant
# --------------------------------------------------------------------------------------


def test_matching_tenant_is_permitted() -> None:
    """A record in the identity's tenant is permitted."""
    require_tenant(_rep("A-1", tenant="demo"), "demo")


def test_cross_tenant_record_is_denied() -> None:
    """A cross-tenant record is denied naming only "tenant", with the value only in `detail`."""
    with pytest.raises(ScopeDenied) as caught:
        require_tenant(_rep("A-1", tenant="demo"), "eval-abc123")
    assert caught.value.missing_scope == "tenant"
    assert "eval-abc123" not in caught.value.missing_scope
    assert "eval-abc123" in (caught.value.detail or "")


def test_eval_tenant_cannot_read_demo_data() -> None:
    """An eval-tenant identity is denied a demo-tenant record."""
    with pytest.raises(ScopeDenied):
        require_tenant(_rep("A-1", tenant="eval-r1"), "demo")


# --------------------------------------------------------------------------------------
# Scope key extraction and list filtering
# --------------------------------------------------------------------------------------


def test_account_id_is_read_from_either_casing() -> None:
    """`account_id_of` reads both `account_id` and `accountId`."""
    assert account_id_of({"account_id": "A-1"}) == "A-1"
    assert account_id_of({"accountId": "A-2"}) == "A-2"


def test_record_without_a_scope_key_raises_rather_than_defaulting() -> None:
    """A record with no account scope key raises `ValueError`."""
    with pytest.raises(ValueError, match="no account scope key"):
        account_id_of({"opportunity_id": "opp-1"})


def test_tenant_of_reads_the_tenant_key() -> None:
    """`tenant_of` returns the record's `tenant` value."""
    assert tenant_of({"tenant": "demo"}) == "demo"


@pytest.mark.parametrize("bad_tenant", [None, "", "   ", 42, [], {}])
def test_tenant_of_raises_rather_than_defaulting_on_absent_empty_or_non_string(
    bad_tenant: object,
) -> None:
    """`tenant_of` raises `ValueError` on an absent, empty, blank or non-string tenant."""
    with pytest.raises(ValueError, match="no tenant scope key"):
        tenant_of({"tenant": bad_tenant, "account_id": "A-1"} if bad_tenant != {} else {})


def test_filter_to_scope_removes_out_of_book_records() -> None:
    """`filter_to_scope` removes out-of-book records and keeps order."""
    records: list[dict[str, object]] = [
        {"account_id": "A-1", "tenant": "demo", "name": "visible"},
        {"account_id": "A-9", "tenant": "demo", "name": "hidden"},
        {"account_id": "A-2", "tenant": "demo", "name": "visible too"},
    ]
    kept = filter_to_scope(_rep("A-1", "A-2"), records)
    assert [record["name"] for record in kept] == ["visible", "visible too"]


def test_filter_to_scope_drops_records_whose_account_scope_is_undeterminable() -> None:
    """A record with no account scope key is dropped."""
    records: list[dict[str, object]] = [
        {"account_id": "A-1", "tenant": "demo", "name": "ok"},
        {"tenant": "demo", "name": "no scope key"},
    ]
    kept = filter_to_scope(_rep("A-1"), records)
    assert [record["name"] for record in kept] == ["ok"]


def test_filter_to_scope_passes_everything_for_full_visibility() -> None:
    """A rep with full visibility sees every scoped record in the tenant."""
    records: list[dict[str, object]] = [
        {"account_id": f"A-{index}", "tenant": "demo"} for index in range(5)
    ]
    assert len(filter_to_scope(_rep(full=True), records)) == 5


def test_filter_to_scope_returns_a_concrete_list_not_a_generator() -> None:
    """`filter_to_scope` returns a list, so an empty result is falsy."""
    result = filter_to_scope(_rep("A-1"), [])
    assert result == []
    assert not result


@pytest.mark.parametrize(
    "record", [None, "acct-1", 7, [{"account_id": "A-1", "tenant": "demo"}], ("a",), object()]
)
def test_is_in_scope_withholds_a_non_mapping_rather_than_raising(record: object) -> None:
    """`is_in_scope` returns False for a non-mapping that `require_record_scope` denies."""
    assert is_in_scope(_rep("A-1"), record) is False
    with pytest.raises(ScopeDenied):
        require_record_scope(_rep("A-1"), record)


def test_is_in_scope_withholds_a_non_dict_mapping_exactly_as_require_record_scope_denies() -> None:
    """A non-dict `Mapping` is withheld by `is_in_scope` and denied by `require_record_scope`."""
    from collections.abc import Mapping

    class ReadOnlyRecord(Mapping[str, object]):
        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def __getitem__(self, key: str) -> object:
            return self._data[key]

        def __iter__(self) -> Iterator[str]:
            return iter(self._data)

        def __len__(self) -> int:
            return len(self._data)

    record = ReadOnlyRecord({"account_id": "A-1", "tenant": "demo"})

    assert is_in_scope(_rep("A-1"), record) is False
    with pytest.raises(ScopeDenied):
        require_record_scope(_rep("A-1"), record)


def test_filter_to_scope_drops_a_non_mapping_record_rather_than_raising() -> None:
    """`filter_to_scope` drops non-mapping records without raising."""
    records: list[object] = [{"account_id": "A-1", "tenant": "demo", "name": "ok"}, None, "acct-1"]

    kept = filter_to_scope(_rep("A-1"), records)  # type: ignore[arg-type]

    assert [record["name"] for record in kept] == ["ok"]


def test_filter_to_scope_is_the_same_predicate_as_require_scope() -> None:
    """`filter_to_scope` agrees with `require_scope` plus `require_tenant` across account, tenant
    and full-visibility combinations."""
    identities = [
        _rep("A-1", "A-2", tenant="demo"),
        _rep("A-1", "A-2", tenant="eval-run-7"),
        _rep(full=True, tenant="eval-run-7"),
        _rep(tenant="demo"),
    ]
    records: list[dict[str, object]] = [
        {"account_id": account_id, "tenant": tenant}
        for account_id in ("A-1", "A-2", "A-3", "A-999")
        for tenant in ("demo", "eval-run-7", "eval-run-8")
    ]
    for identity in identities:
        for record in records:
            filtered = bool(filter_to_scope(identity, [record]))
            try:
                require_scope(identity, str(record["account_id"]))
                require_tenant(identity, str(record["tenant"]))
                allowed = True
            except ScopeDenied:
                allowed = False
            assert filtered == allowed, (
                f"divergent decision for identity(tenant={identity.tenant}, "
                f"full_visibility={identity.full_visibility}) vs record={record}"
            )


def test_filter_to_scope_denies_a_cross_tenant_record_even_with_full_visibility() -> None:
    """A full-visibility identity in an eval tenant receives no demo-tenant record."""
    identity = _rep(full=True, tenant="eval-run-7")
    demo_record: dict[str, object] = {"account_id": "A-1", "tenant": "demo", "name": "DEMO DATA"}
    assert filter_to_scope(identity, [demo_record]) == []


def test_filter_to_scope_denies_a_cross_tenant_record_in_book() -> None:
    """An in-book account under another tenant is dropped."""
    identity = Identity(rep_id="rep-1", book=frozenset({"A-1"}), tenant="eval-run-7")
    demo_record: dict[str, object] = {"account_id": "A-1", "tenant": "demo", "name": "DEMO DATA"}
    assert filter_to_scope(identity, [demo_record]) == []


def test_filter_to_scope_permits_same_tenant_records_as_before() -> None:
    """Same-tenant records are filtered by book only."""
    identity = _rep("A-1", "A-2", tenant="demo")
    records: list[dict[str, object]] = [
        {"account_id": "A-1", "tenant": "demo", "name": "visible"},
        {"account_id": "A-9", "tenant": "demo", "name": "hidden"},
    ]
    kept = filter_to_scope(identity, records)
    assert [record["name"] for record in kept] == ["visible"]


def test_filter_to_scope_denies_a_record_with_no_tenant_key() -> None:
    """A record with no tenant key is dropped."""
    identity = _rep("A-1", tenant="eval-run-7")
    record: dict[str, object] = {"account_id": "A-1", "name": "no tenant key"}
    assert filter_to_scope(identity, [record]) == []


def test_filter_to_scope_denies_a_record_with_an_empty_or_non_string_tenant() -> None:
    """A record with an empty, blank or non-string tenant is dropped."""
    identity = _rep("A-1", tenant="eval-run-7")
    for bad_tenant in ("", "   ", None, 42):
        record: dict[str, object] = {"account_id": "A-1", "tenant": bad_tenant}
        assert filter_to_scope(identity, [record]) == []


# --------------------------------------------------------------------------------------
# require_listing_scope
# --------------------------------------------------------------------------------------


def test_require_listing_scope_denies_an_empty_book_without_full_visibility() -> None:
    """An empty-book, non-full-visibility caller is denied with `account:*` and a safe message."""
    with pytest.raises(ScopeDenied) as denied:
        require_listing_scope(_rep(), operation="search_accounts")
    assert denied.value.missing_scope == "account:*"
    assert denied.value.message == "You do not have access to any accounts."
    assert denied.value.detail == (
        "operation=search_accounts reason=empty_book rep=rep-1 scope=0-accounts tenant=demo"
    )
    assert "detail" not in denied.value.to_api_response()["error"]


def test_require_listing_scope_allows_a_caller_with_a_book() -> None:
    """A caller owning at least one account passes."""
    require_listing_scope(_rep("A-1"), operation="search_accounts")


def test_require_listing_scope_allows_full_visibility_with_an_empty_book() -> None:
    """Full visibility passes even when the book is empty."""
    require_listing_scope(_rep(full=True), operation="search_accounts")
