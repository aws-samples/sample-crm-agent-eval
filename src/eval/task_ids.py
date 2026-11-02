# SPDX-License-Identifier: MIT
"""Compose the golden tasks' tenant-relative record ids against the tenant a sweep actually runs in.

``src/eval/golden_tasks.yaml`` names records by tenant-relative local id (``acct-0000``,
``opp-9999``, ``act-defect-pd01``). :func:`localize_tasks` composes every id in a task's prompt and
assertions against the sweep's ``eval-<run_id>`` tenant with :func:`fixtures.generate.scoped_id`,
and :func:`assert_tenant_relative` refuses a task that already carries a tenant-scoped id.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from fixtures.generate import scoped_id

#: Entity-id kind prefixes :mod:`fixtures.generate` emits. A token outside this set (``rep-…``,
#: ``svc-…``) is never a tenant-scoped record id and is left untouched by composition.
LOCAL_ID_KINDS: Final[tuple[str, ...]] = ("acct", "cont", "opp", "act", "note")

#: A tenant-relative local id as it appears in ``golden_tasks.yaml``: a kind prefix plus either a
#: four-digit index (``acct-0000``) or a planted-defect suffix (``act-defect-pd01``).
_LOCAL_ID_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"\b(" + "|".join(LOCAL_ID_KINDS) + r")-(defect-pd\d{2}|\d{4})\b"
)

#: An id that already carries a tenant: a kind prefix, any intervening segment(s), then the
#: terminal index or planted-defect suffix. ``acct-0000`` and ``act-defect-pd01`` do not match.
_ALREADY_SCOPED_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:"
    + "|".join(LOCAL_ID_KINDS)
    + r")-(?:[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*?)-(?:defect-pd\d{2}|\d{4})\b"
)


class TenantScopedIdInTaskFileError(ValueError):
    """Raised when a golden task carries an already-tenant-scoped record id (``acct-demo-0000``)."""


def compose_ids_in_text(text: str, *, tenant: str) -> str:
    """Return ``text`` with every tenant-relative local id token composed against ``tenant``.

    Args:
        text: Any string from a golden task — a prompt, a note, an assertion's id or value.
        tenant: The tenant to compose against.

    Returns:
        ``text`` with ``acct-0000``-shaped tokens rewritten to ``acct-<tenant>-0000``. Text with no
        such token is returned unchanged.
    """
    return _LOCAL_ID_TOKEN.sub(
        lambda match: scoped_id(tenant, f"{match.group(1)}-{match.group(2)}"), text
    )


def _localize_value(value: object, tenant: str) -> Any:
    """Recursively compose every string inside ``value``; non-string leaves pass through unchanged."""
    if isinstance(value, str):
        return compose_ids_in_text(value, tenant=tenant)
    if isinstance(value, Mapping):
        return {key: _localize_value(item, tenant) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_localize_value(item, tenant) for item in value]
    return value


def localize_task(task: Mapping[str, Any], *, tenant: str) -> dict[str, Any]:
    """Return a copy of one golden task with every record id composed against ``tenant``.

    Args:
        task: One task object as parsed from ``golden_tasks.yaml``.
        tenant: The tenant the sweep seeded and will score against.

    Returns:
        A deep copy of ``task``; the input mapping is not mutated.

    Raises:
        TenantScopedIdInTaskFileError: ``task`` carries an already-tenant-scoped record id.
    """
    assert_tenant_relative([task])
    localized = _localize_value(task, tenant)
    if not isinstance(localized, dict):  # pragma: no cover - a task is always a mapping
        raise TypeError("a golden task must be a mapping")
    return localized


def localize_tasks(tasks: Sequence[Mapping[str, Any]], *, tenant: str) -> list[dict[str, Any]]:
    """Localize every task in ``tasks`` against ``tenant`` (see :func:`localize_task`)."""
    return [localize_task(task, tenant=tenant) for task in tasks]


def _iter_strings(value: object) -> Iterable[str]:
    """Yield every string leaf reachable from ``value``."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _iter_strings(item)


def assert_tenant_relative(tasks: Sequence[Mapping[str, Any]]) -> None:
    """Refuse any task carrying an already-tenant-scoped record id.

    Raises:
        TenantScopedIdInTaskFileError: Naming the offending task and token.
    """
    for task in tasks:
        for text in _iter_strings(task):
            match = _ALREADY_SCOPED_TOKEN.search(text)
            if match is not None:
                raise TenantScopedIdInTaskFileError(
                    f"task {task.get('id')!r} carries the tenant-scoped id {match.group(0)!r}: "
                    "golden tasks name records by tenant-relative local id only "
                    "(for example 'acct-0000'), composed against the run's tenant at load time"
                )


__all__ = [
    "LOCAL_ID_KINDS",
    "TenantScopedIdInTaskFileError",
    "assert_tenant_relative",
    "compose_ids_in_text",
    "localize_task",
    "localize_tasks",
]
