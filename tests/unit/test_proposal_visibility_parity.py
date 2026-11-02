# SPDX-License-Identifier: MIT
"""Differential tests: the proposal paths' visibility must not be looser than ``filter_to_scope``.

Two properties are asserted:

* ``list_proposals._visible`` agrees with :func:`crm.authz.filter_to_scope` on the scope source it
  decides from — a record hidden from a ``get`` cannot be listed through a ``list``.
* ``approve_proposal``/``reject_proposal`` run the scope check on *every* input shape, including a
  stored ``before_snapshot`` that is not a mapping, rather than skipping it on a type mismatch.

Runs against a `moto`-mocked DynamoDB client: zero credentials, zero network, zero cost.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity, filter_to_scope, is_in_scope
from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import approve_proposal, get_opportunity, list_proposals, reject_proposal
from crm.handlers.list_proposals import _visible
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_OTHER_TENANT = "eval-other"
_REP = "rep-1"
_IN_BOOK_ACCOUNT = "acct-1"
_OUT_OF_BOOK_ACCOUNT = "acct-2"

#: Every scope-source shape a stored proposal could carry, paired with a label for failure output.
_SCOPE_SOURCES: list[tuple[str, dict[str, object]]] = [
    ("in-book", {"tenant": _TENANT, "account_id": _IN_BOOK_ACCOUNT}),
    ("out-of-book", {"tenant": _TENANT, "account_id": _OUT_OF_BOOK_ACCOUNT}),
    ("no-account-key", {"tenant": _TENANT, "name": "Acme"}),
    ("blank-account-key", {"tenant": _TENANT, "account_id": ""}),
    ("non-string-account-key", {"tenant": _TENANT, "account_id": 17}),
    ("camel-case-account-key", {"tenant": _TENANT, "accountId": _IN_BOOK_ACCOUNT}),
    ("foreign-tenant", {"tenant": _OTHER_TENANT, "account_id": _IN_BOOK_ACCOUNT}),
    ("no-tenant-key", {"account_id": _IN_BOOK_ACCOUNT}),
    ("empty", {}),
]


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _identity_payload() -> dict[str, Any]:
    return {
        "rep_id": _REP,
        "book": [_IN_BOOK_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }


def _event(args: dict[str, Any]) -> dict[str, Any]:
    return identity_event(IDENTITY_KEY, _identity_payload(), args)


def _identity() -> Identity:
    return Identity(
        rep_id=_REP, book=frozenset({_IN_BOOK_ACCOUNT}), tenant=_TENANT, source="gateway"
    )


def _seed_opportunity(store: Store, *, opportunity_id: str, account_id: str) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{account_id}",
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "tenant_stage": f"{_TENANT}#Discovery",
            "account_id": account_id,
            "owner_rep_id": _REP,
            "name": "Big Deal",
            "name_lower": "big deal",
            "stage": "Discovery",
            "value": 1000,
            "primary_contact_id": None,
            "last_touch": None,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _seed_proposal(
    store: Store,
    *,
    proposal_id: str,
    before_snapshot: object,
    target_id: str = "opp-1",
    tenant: str = _TENANT,
) -> None:
    """Write a proposal row directly, bypassing ``submit_proposal``'s own scope check."""
    store.put_item(
        "proposals",
        {
            "proposal_id": proposal_id,
            "tenant": tenant,
            "status": "pending",
            "tenant_status": f"{tenant}#pending",
            "target_table": "opportunities",
            "target_id": target_id,
            "operation": "update",
            "before_snapshot": before_snapshot,
            "after_payload": {"stage": "Qualification"},
            "reason": "advance",
            "actor_rep_id": _REP,
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        },
    )


# --------------------------------------------------------------------------------------
# The list predicate never admits what filter_to_scope drops
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "scope_source"), _SCOPE_SOURCES, ids=[s[0] for s in _SCOPE_SOURCES]
)
def test_list_visibility_agrees_with_filter_to_scope_on_every_scope_source(
    label: str, scope_source: dict[str, object]
) -> None:
    """``_visible`` and ``filter_to_scope`` reach the same decision on every scope source."""
    identity = _identity()
    proposal: dict[str, object] = {
        "proposal_id": f"prop-{label}",
        "tenant": _TENANT,
        "before_snapshot": scope_source,
    }

    canonical = bool(filter_to_scope(identity, [scope_source]))
    assert _visible(identity, proposal) is canonical, label
    assert is_in_scope(identity, scope_source) is canonical, label


def test_only_the_in_scope_shapes_are_visible_at_all() -> None:
    """Exactly the in-book and camel-case-account-key shapes are visible."""
    identity = _identity()
    visible = {
        label
        for label, source in _SCOPE_SOURCES
        if _visible(identity, {"tenant": _TENANT, "before_snapshot": source})
    }
    assert visible == {"in-book", "camel-case-account-key"}


def test_a_proposal_whose_snapshot_has_no_account_scope_is_not_listed(store: Store) -> None:
    """A proposal whose snapshot has no determinable account scope is not visible."""
    _seed_proposal(store, proposal_id="prop-unscoped", before_snapshot={"tenant": _TENANT})

    result = list_proposals.handler(_event({}), store=store)

    assert result["statusCode"] == 200
    assert result["body"]["proposals"] == []


@pytest.mark.parametrize(
    "snapshot", [None, "not-a-mapping", 17, [{"account_id": _IN_BOOK_ACCOUNT}]]
)
def test_a_proposal_whose_snapshot_is_not_a_mapping_is_not_listed(
    store: Store, snapshot: object
) -> None:
    _seed_proposal(store, proposal_id="prop-malformed", before_snapshot=snapshot)

    result = list_proposals.handler(_event({}), store=store)

    assert result["statusCode"] == 200
    assert result["body"]["proposals"] == []


def test_a_record_hidden_from_get_is_not_listed_through_list_proposals(store: Store) -> None:
    """A proposal targeting an opportunity ``get`` denies is omitted from ``list_proposals``."""
    _seed_opportunity(store, opportunity_id="opp-out", account_id=_OUT_OF_BOOK_ACCOUNT)
    _seed_proposal(
        store,
        proposal_id="prop-out",
        target_id="opp-out",
        before_snapshot={
            "opportunity_id": "opp-out",
            "tenant": _TENANT,
            "account_id": _OUT_OF_BOOK_ACCOUNT,
            "stage": "Discovery",
        },
    )
    _seed_opportunity(store, opportunity_id="opp-in", account_id=_IN_BOOK_ACCOUNT)
    _seed_proposal(
        store,
        proposal_id="prop-in",
        target_id="opp-in",
        before_snapshot={
            "opportunity_id": "opp-in",
            "tenant": _TENANT,
            "account_id": _IN_BOOK_ACCOUNT,
            "stage": "Discovery",
        },
    )

    fetched = get_opportunity.handler(_event({"opportunity_id": "opp-out"}), store=store)
    assert fetched["statusCode"] == 403
    assert fetched["error"]["code"] == "scope_denied"

    listed = list_proposals.handler(_event({}), store=store)
    assert listed["statusCode"] == 200
    proposal_ids = {str(item["proposal_id"]) for item in listed["body"]["proposals"]}
    assert proposal_ids == {"prop-in"}


# --------------------------------------------------------------------------------------
# The decision paths run the scope check on every input shape
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("snapshot", [None, "not-a-mapping", {"tenant": _TENANT}, {}])
def test_approve_denies_when_the_scope_source_cannot_be_checked(
    store: Store, snapshot: object
) -> None:
    """With no target record and an uncheckable snapshot, approve denies."""
    _seed_proposal(store, proposal_id="prop-x", target_id="opp-missing", before_snapshot=snapshot)

    result = approve_proposal.handler(
        _event({"proposal_id": "prop-x", "transaction_key": "tx-a1"}), store=store
    )

    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"


@pytest.mark.parametrize("snapshot", [None, "not-a-mapping", {"tenant": _TENANT}, {}])
def test_reject_denies_when_the_scope_source_cannot_be_checked(
    store: Store, snapshot: object
) -> None:
    _seed_proposal(store, proposal_id="prop-y", target_id="opp-missing", before_snapshot=snapshot)

    result = reject_proposal.handler(
        _event({"proposal_id": "prop-y", "transaction_key": "tx-r1"}), store=store
    )

    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"


def test_approve_denies_a_target_record_in_a_foreign_tenant(store: Store) -> None:
    """The target's own tenant is checked, not only the proposal's."""
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "opp-foreign",
            "tenant": _OTHER_TENANT,
            "tenant_account_id": f"{_OTHER_TENANT}#{_IN_BOOK_ACCOUNT}",
            "account_id": _IN_BOOK_ACCOUNT,
            "owner_rep_id": _REP,
            "name": "Big Deal",
            "stage": "Discovery",
            "value": 1000,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )
    _seed_proposal(
        store,
        proposal_id="prop-foreign",
        target_id="opp-foreign",
        before_snapshot={"tenant": _OTHER_TENANT, "account_id": _IN_BOOK_ACCOUNT},
    )

    result = approve_proposal.handler(
        _event({"proposal_id": "prop-foreign", "transaction_key": "tx-a2"}), store=store
    )

    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"


def test_approve_still_succeeds_on_a_well_formed_in_scope_proposal(store: Store) -> None:
    """A well-formed in-scope proposal is still approved."""
    _seed_opportunity(store, opportunity_id="opp-in", account_id=_IN_BOOK_ACCOUNT)
    stored = store.get_item("opportunities", {"opportunity_id": "opp-in"})
    assert stored is not None
    _seed_proposal(store, proposal_id="prop-ok", target_id="opp-in", before_snapshot=dict(stored))

    result = approve_proposal.handler(
        _event({"proposal_id": "prop-ok", "transaction_key": "tx-a3"}), store=store
    )

    assert result["statusCode"] == 200
    assert result["body"]["proposal"]["status"] == "approved"
