# SPDX-License-Identifier: MIT
"""Golden-task actor helpers.

:func:`actor_principal_type` reads the ``principal_type`` every golden-task ``actor`` block in
``src/eval/golden_tasks.yaml`` declares, so eval identity producers send the actor's declared
type rather than a constant.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast, get_args

from crm.authz import PrincipalType

PRINCIPAL_TYPES: Final[frozenset[str]] = frozenset(get_args(PrincipalType))


def actor_principal_type(actor: Mapping[str, object]) -> PrincipalType:
    """Return the actor's declared principal type.

    Args:
        actor: One golden task's ``actor`` mapping.

    Returns:
        ``"user"`` or ``"service"``.

    Raises:
        ValueError: ``principal_type`` is absent or not one of :data:`PRINCIPAL_TYPES`, or the
            actor is a ``service`` principal whose ``full_visibility`` is not exactly ``False``.
    """
    rep_id = actor.get("rep_id")
    if "principal_type" not in actor:
        raise ValueError(f"actor {rep_id!r} declares no principal_type")
    declared = actor["principal_type"]
    if not isinstance(declared, str) or declared not in PRINCIPAL_TYPES:
        raise ValueError(
            f"actor {rep_id!r} declares principal_type {declared!r}; "
            f"expected one of {sorted(PRINCIPAL_TYPES)}"
        )
    if declared == "service" and actor.get("full_visibility") is not False:
        raise ValueError(
            f"actor {rep_id!r} is a service principal and must declare full_visibility: false"
        )
    return cast(PrincipalType, declared)
