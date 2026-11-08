# SPDX-License-Identifier: MIT
"""Cross-record reference checks shared by direct writes and the proposal path.

:func:`require_primary_contact_in_account` checks that an opportunity's ``primary_contact_id``
names a contact in the caller's tenant under the opportunity's own account.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crm.errors import ValidationFailed, bounded_detail_value

if TYPE_CHECKING:
    from crm.authz import Identity
    from crm.store.store import Store


def require_primary_contact_in_account(
    store: Store,
    identity: Identity,
    account_id: str,
    contact_id: object,
    *,
    source: str = "primary_contact_id",
) -> None:
    """Raise unless ``contact_id`` is a contact in ``identity.tenant`` under ``account_id``.

    ``None`` and ``""`` (a cleared reference) are accepted without a read.

    Args:
        store: The persistence layer.
        identity: The caller; its ``tenant`` must own the contact.
        account_id: The account the referencing opportunity belongs to.
        contact_id: The proposed ``primary_contact_id``.
        source: The ``source`` reported on the raised error.

    Raises:
        ValidationFailed: The contact does not exist, belongs to another tenant, or sits under a
            different account.
    """
    if contact_id is None or contact_id == "":
        return
    contact = store.get_item("contacts", {"contact_id": str(contact_id)})
    if (
        contact is None
        or contact.get("tenant") != identity.tenant
        or contact.get("account_id") != account_id
    ):
        raise ValidationFailed(
            "primary_contact_id must reference a contact under the same account.",
            source=source,
            detail=(
                f"account_id={bounded_detail_value(account_id)} "
                f"contact_id={bounded_detail_value(str(contact_id))}"
            ),
        )
