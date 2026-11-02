# SPDX-License-Identifier: MIT
"""Cascade checks for delete operations.

* Deleting an account with an open opportunity is refused with :class:`crm.errors.ValidationFailed`.
  Closed opportunities do not block the delete.
* Deleting a contact still named as any opportunity's ``primary_contact_id`` is refused with
  :class:`crm.errors.ValidationFailed`. Marking a contact ``departed`` is an ordinary update and
  leaves the reference in place.

Both checks run in the ``submit_proposal`` handler, before a proposal is created.
"""

from __future__ import annotations

from crm.errors import ValidationFailed
from crm.store.keys import tenant_prefixed
from crm.store.store import Store

_OPEN_OPPORTUNITY_LIMIT_PAGE = 25


def reject_account_delete_with_open_opportunities(
    store: Store, *, tenant: str, account_id: str
) -> None:
    """Raise :class:`ValidationFailed` if ``account_id`` has any open opportunity.

    Pages ``opportunities.gsi_account`` and stops at the first stage in
    :data:`crm.domain.stages.OPEN_STAGES`.
    """
    from crm.domain.stages import OPEN_STAGES

    partition_value = tenant_prefixed(tenant, account_id)
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_gsi(
            "opportunities",
            "gsi_account",
            partition_value=partition_value,
            limit=_OPEN_OPPORTUNITY_LIMIT_PAGE,
            exclusive_start_key=cursor,
        )
        for item in items:
            if item.get("stage") in OPEN_STAGES:
                raise ValidationFailed(
                    "This account has open opportunities and cannot be deleted.",
                    source="account_id",
                    detail=(
                        f"account_id={account_id} open_opportunity_id={item.get('opportunity_id')}"
                    ),
                )
        if cursor is None:
            break


def reject_contact_delete_if_referenced(store: Store, *, tenant: str, contact_id: str) -> None:
    """Raise :class:`ValidationFailed` if any opportunity names ``contact_id`` as its primary contact.

    Scans the account's opportunities through ``gsi_account`` and re-fetches each by primary key,
    since that index does not project ``primary_contact_id``.
    """
    account_id = _account_id_of_contact(store, tenant=tenant, contact_id=contact_id)
    if account_id is None:
        return

    partition_value = tenant_prefixed(tenant, account_id)
    cursor: dict[str, object] | None = None
    while True:
        stubs, cursor = store.query_gsi(
            "opportunities",
            "gsi_account",
            partition_value=partition_value,
            limit=_OPEN_OPPORTUNITY_LIMIT_PAGE,
            exclusive_start_key=cursor,
        )
        for stub in stubs:
            full = store.get_item("opportunities", {"opportunity_id": str(stub["opportunity_id"])})
            if full is not None and full.get("primary_contact_id") == contact_id:
                raise ValidationFailed(
                    "This contact is a primary contact on an opportunity and cannot be deleted.",
                    source="contact_id",
                    detail=(
                        f"contact_id={contact_id} referencing_opportunity_id="
                        f"{full.get('opportunity_id')}"
                    ),
                )
        if cursor is None:
            break


def _account_id_of_contact(store: Store, *, tenant: str, contact_id: str) -> str | None:
    contact = store.get_item("contacts", {"contact_id": contact_id})
    if contact is None or contact.get("tenant") != tenant:
        return None
    account_id = contact.get("account_id")
    return account_id if isinstance(account_id, str) and account_id else None
