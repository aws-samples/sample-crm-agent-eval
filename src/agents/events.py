# SPDX-License-Identifier: MIT
"""The closed agent event stream.

Every event an agent turn can produce is one of the kinds in :data:`EVENT_KINDS`. This extends
:data:`providers.base.EventType` with ``proposal`` (a ``submit_proposal`` result), ``text_delta``
(one chunk of streamed answer text, grouped by ``event_id``, always ``partial``; the following
``completed`` or ``error`` frame settles it), ``heartbeat`` (an idle keep-alive carrying no
fields) and ``replay_unavailable`` (sent first on a resume the session store cannot replay, before
the turn runs afresh; it carries no fields). ``usage`` and ``completed`` may carry the turn's cost and resolved tiers.
:meth:`AgentEvent.__post_init__` rejects any field combination that does not match the declared
``kind``, and strips control characters from every text field via
:func:`strip_control_chars`.

An exhausted turn budget ends the stream with a terminal ``error`` event whose ``error_kind`` is
``"budget_exhausted"``. An oversized tool result is truncated at a record boundary and marked with
:data:`TRUNCATION_MARKER`; :func:`bound_tool_output` is the one bound every dispatch path applies.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, TypedDict, get_args

from providers.capabilities import Family, Tier

#: The closed, exhaustive set of agent event kinds.
EVENT_KINDS: Final[tuple[str, ...]] = (
    "tool_call",
    "tool_result",
    "proposal",
    "usage",
    "completed",
    "error",
    "text_delta",
    "heartbeat",
    "replay_unavailable",
)

EventKind = Literal[
    "tool_call",
    "tool_result",
    "proposal",
    "usage",
    "completed",
    "error",
    "text_delta",
    "heartbeat",
    "replay_unavailable",
]

#: Kinds that may carry token counts, cost lines and resolved tiers.
_ACCOUNTED_KINDS: Final[frozenset[str]] = frozenset({"usage", "completed"})

#: Reported terminal error kinds. :mod:`agents.budget` attaches ``budget_exhausted``; callers
#: report the others, with ``invalid_request`` marking a turn-level validation failure sent as a
#: 200 SSE ``error`` frame. :mod:`agents.event_schema` derives the SSE frame enum from this set.
ErrorKind = Literal["budget_exhausted", "tool_failure", "model_failure", "invalid_request"]

#: Marker appended to a truncated tool result payload's ``items`` list in place of the records it
#: could not include.
TRUNCATION_MARKER: Final[str] = "__truncated__"

#: The tool-result payload byte ceiling before truncation applies.
TOOL_RESULT_BYTE_CEILING: Final[int] = 32_768

#: The payload key a bounded tool result sets to ``True`` to report it was truncated.
TOOL_OUTPUT_TRUNCATED_KEY: Final[str] = "truncated"

#: C0 control characters and DEL removed from event text; tab and newline are kept.
_CONTROL_CHARS: Final[dict[int, None]] = {
    code: None for code in (*range(0x00, 0x20), 0x7F) if code not in (0x09, 0x0A)
}

#: The event fields carrying free text, each passed through :func:`strip_control_chars`.
_TEXT_FIELDS: Final[tuple[str, ...]] = ("text", "error_message", "partial_work")


def strip_control_chars(text: str) -> str:
    """Return ``text`` without U+0000–U+001F (except tab and newline) and U+007F."""
    return text.translate(_CONTROL_CHARS)


class CostLine(TypedDict):
    """One priced ``(tier, role, model_id)`` group of a turn's model usage.

    ``cost_usd`` is ``None`` when pricing is withheld. ``long_context_calls`` counts the group's
    invocations priced at the model's long-context rates, and is reported even when withheld.
    """

    tier: Tier
    role: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    long_context_calls: int
    cost_usd: float | None


class ResolvedTierInfo(TypedDict):
    """The model and tuning one tier actually ran with for a turn."""

    tier: Tier
    family: Family
    model_id: str
    effort: str | None


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One event in the closed agent event stream.

    Exactly one of the kind-specific field groups below is populated, matching ``kind``; every
    other field is ``None`` or empty, as enforced by :meth:`__post_init__`.

    Attributes:
        kind: Which closed event kind this is; always a value in :data:`EVENT_KINDS`.
        tool_name: The tool invoked, for ``tool_call``; absent otherwise.
        tool_input: The tool call arguments, for ``tool_call``; absent otherwise.
        tool_output: The tool result payload, for ``tool_result``; absent otherwise. May carry
            :data:`TRUNCATION_MARKER` per :attr:`truncated`.
        truncated: Whether ``tool_output`` was cut at a record boundary before delivery;
            ``False`` when nothing was cut.
        proposal_id: The created proposal's id, for ``proposal``; absent otherwise.
        proposal_summary: A user-facing before/after summary, for ``proposal``; absent otherwise.
        input_tokens: Input tokens consumed, for ``usage`` and ``completed``; absent otherwise.
        output_tokens: Output tokens produced, for ``usage`` and ``completed``; absent otherwise.
        cache_read_tokens: Tokens served from a prompt cache, for ``usage`` and ``completed``,
            when reported.
        cache_write_tokens: Tokens written to a prompt cache, for ``usage`` and ``completed``,
            when reported.
        text: The reply text — the whole synthesized reply for ``completed``, or one incremental
            chunk for ``text_delta``; absent on every other kind.
        event_id: Groups the ``text_delta`` frames of one streamed answer so a consumer updates a
            single node in place, for ``text_delta``; absent otherwise.
        partial: ``True`` on a produced ``text_delta``; always ``False`` on every other kind.
        error_kind: Which closed error category this is, for ``error``; absent otherwise.
        error_message: A user-safe error description, for ``error``; absent otherwise.
        partial_work: A description of work already committed or proposed before the turn ended,
            for an ``error`` event whose ``error_kind`` is ``"budget_exhausted"``; absent
            otherwise.
        cost_lines: The turn's priced usage grouped by tier, role and model, for ``usage`` and
            ``completed``, when reported.
        cost_usd: The turn's total cost, for ``usage`` and ``completed``; ``None`` when pricing is
            withheld or not reported.
        cost_withheld: Whether pricing is provisional and figures are withheld, for ``usage`` and
            ``completed``, when reported.
        resolved_tiers: The model and tuning each tier ran with, for ``usage`` and ``completed``,
            when reported.
    """

    kind: EventKind
    tool_name: str | None = None
    tool_input: dict[str, object] | None = None
    tool_output: dict[str, object] | None = None
    truncated: bool = False
    proposal_id: str | None = None
    proposal_summary: dict[str, object] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    text: str | None = None
    event_id: str | None = None
    partial: bool = False
    error_kind: ErrorKind | None = None
    error_message: str | None = None
    partial_work: str | None = None
    cost_lines: tuple[CostLine, ...] | None = None
    cost_usd: float | None = None
    cost_withheld: bool | None = None
    resolved_tiers: tuple[ResolvedTierInfo, ...] | None = None

    def __post_init__(self) -> None:
        """Reject a field combination that does not match ``kind``; strip control characters.

        ``text``, ``error_message`` and ``partial_work`` are replaced by their
        :func:`strip_control_chars` form when they are strings.
        """
        if self.kind not in EVENT_KINDS:
            raise ValueError(
                f"unrecognised event kind {self.kind!r}; expected one of {EVENT_KINDS}"
            )
        for name in _TEXT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, strip_control_chars(value))

        _require(self.kind == "tool_call", self.tool_name is not None, "tool_call", "tool_name")
        _require(self.kind == "tool_call", self.tool_input is not None, "tool_call", "tool_input")
        _forbid_unless(self.kind == "tool_call", self.tool_name, "tool_name")
        _forbid_unless(self.kind == "tool_call", self.tool_input, "tool_input")

        _require(
            self.kind == "tool_result", self.tool_output is not None, "tool_result", "tool_output"
        )
        _forbid_unless(self.kind == "tool_result", self.tool_output, "tool_output")

        _require(self.kind == "proposal", self.proposal_id is not None, "proposal", "proposal_id")
        _require(
            self.kind == "proposal",
            self.proposal_summary is not None,
            "proposal",
            "proposal_summary",
        )
        _forbid_unless(self.kind == "proposal", self.proposal_id, "proposal_id")
        _forbid_unless(self.kind == "proposal", self.proposal_summary, "proposal_summary")

        _require(self.kind == "usage", self.input_tokens is not None, "usage", "input_tokens")
        _require(self.kind == "usage", self.output_tokens is not None, "usage", "output_tokens")

        _require(self.kind == "completed", self.text is not None, "completed", "text")
        _require(self.kind == "text_delta", self.text is not None, "text_delta", "text")
        _forbid_unless(self.kind in ("completed", "text_delta"), self.text, "text")

        _require(self.kind == "text_delta", self.event_id is not None, "text_delta", "event_id")
        _forbid_unless(self.kind == "text_delta", self.event_id, "event_id")
        if self.partial and self.kind != "text_delta":
            raise ValueError("partial may only be set on a text_delta event")

        if self.kind not in _ACCOUNTED_KINDS:
            _forbid_unless(False, self.input_tokens, "input_tokens")
            _forbid_unless(False, self.output_tokens, "output_tokens")
            _forbid_unless(False, self.cache_read_tokens, "cache_read_tokens")
            _forbid_unless(False, self.cache_write_tokens, "cache_write_tokens")
            _forbid_present(self.cost_lines, "cost_lines")
            _forbid_present(self.cost_usd, "cost_usd")
            _forbid_present(self.cost_withheld, "cost_withheld")
            _forbid_present(self.resolved_tiers, "resolved_tiers")
        else:
            _check_accounting(self)

        _require(self.kind == "error", self.error_kind is not None, "error", "error_kind")
        _require(self.kind == "error", self.error_message is not None, "error", "error_message")
        _forbid_unless(self.kind == "error", self.error_kind, "error_kind")
        _forbid_unless(self.kind == "error", self.error_message, "error_message")
        if self.error_kind != "budget_exhausted":
            _forbid_unless(False, self.partial_work, "partial_work")
        if self.kind != "error":
            _forbid_unless(False, self.partial_work, "partial_work")

        if self.truncated and self.kind != "tool_result":
            raise ValueError("truncated may only be set on a tool_result event")


def _require(applies: bool, satisfied: bool, kind: str, field_name: str) -> None:
    """Raise if a ``kind``-specific field is missing when ``kind`` requires it."""
    if applies and not satisfied:
        raise ValueError(f"{kind} event requires {field_name}")


def _forbid_unless(allowed: bool, value: object, field_name: str) -> None:
    """Raise if a field is populated on an event kind that must not carry it."""
    if not allowed and value:
        raise ValueError(f"{field_name} must not be set on this event kind")


def _forbid_present(value: object, field_name: str) -> None:
    """Raise if a field whose falsy values are meaningful is set on a kind that must not carry it."""
    if value is not None:
        raise ValueError(f"{field_name} must not be set on this event kind")


_TIERS: Final[tuple[str, ...]] = get_args(Tier)
_FAMILIES: Final[tuple[str, ...]] = get_args(Family)
_COST_LINE_KEYS: Final[frozenset[str]] = frozenset(CostLine.__annotations__)
_RESOLVED_TIER_KEYS: Final[frozenset[str]] = frozenset(ResolvedTierInfo.__annotations__)
_COUNT_KEYS: Final[tuple[str, ...]] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "long_context_calls",
)


def _check_accounting(event: AgentEvent) -> None:
    """Validate the cost and resolved-tier fields' values on a ``usage``/``completed`` event."""
    _check_cost(event.cost_usd, "cost_usd")
    if event.cost_withheld is not None and not isinstance(event.cost_withheld, bool):
        raise ValueError("cost_withheld must be a boolean or None")
    withheld = event.cost_withheld is True
    if withheld and event.cost_usd is not None:
        raise ValueError("cost_usd must be None when cost_withheld is true")
    for line in event.cost_lines or ():
        entry: dict[str, object] = _check_keys(line, _COST_LINE_KEYS, "cost_lines")
        _check_member(entry["tier"], _TIERS, "cost_lines.tier")
        for name in ("role", "model_id"):
            _check_non_empty(entry[name], f"cost_lines.{name}")
        for name in _COUNT_KEYS:
            count = entry[name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"cost_lines.{name} must be a non-negative integer")
        _check_cost(entry["cost_usd"], "cost_lines.cost_usd")
        if withheld and entry["cost_usd"] is not None:
            raise ValueError("cost_lines.cost_usd must be None when cost_withheld is true")
    for resolved in event.resolved_tiers or ():
        entry = _check_keys(resolved, _RESOLVED_TIER_KEYS, "resolved_tiers")
        _check_member(entry["tier"], _TIERS, "resolved_tiers.tier")
        _check_member(entry["family"], _FAMILIES, "resolved_tiers.family")
        _check_non_empty(entry["model_id"], "resolved_tiers.model_id")
        if entry["effort"] is not None and not isinstance(entry["effort"], str):
            raise ValueError("resolved_tiers.effort must be a string or None")


def _check_cost(value: object, field_name: str) -> None:
    """Raise unless ``value`` is ``None`` or a finite, non-negative number."""
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a finite, non-negative number or None")


def _check_keys(entry: object, expected: frozenset[str], field_name: str) -> dict[str, object]:
    """Return ``entry`` if it is a mapping with exactly the ``expected`` keys, else raise."""
    if not isinstance(entry, dict) or frozenset(entry) != expected:
        raise ValueError(f"each {field_name} entry must carry exactly {sorted(expected)}")
    return entry


def _check_non_empty(value: object, field_name: str) -> None:
    """Raise unless ``value`` is a non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _check_member(value: object, allowed: tuple[str, ...], field_name: str) -> None:
    """Raise unless ``value`` is one of ``allowed``."""
    if value not in allowed:
        raise ValueError(f"{field_name} must be one of {list(allowed)}, got {value!r}")


def truncate_tool_output(
    items: list[dict[str, object]], *, byte_ceiling: int = TOOL_RESULT_BYTE_CEILING
) -> tuple[list[dict[str, object]], bool]:
    """Truncate ``items`` at a record boundary so the serialised payload stays under a ceiling.

    Items are kept whole — never sliced mid-record — up to the point where adding the next item
    would exceed ``byte_ceiling``, measured as the UTF-8 length of each item's ``repr``. When any
    item is dropped, :data:`TRUNCATION_MARKER` is appended as the final list entry.

    Args:
        items: Whole records to include, in order. Each is kept or dropped entire.
        byte_ceiling: Maximum cumulative size, in bytes, of the included items before truncation
            applies.

    Returns:
        A tuple of the possibly-truncated item list (with :data:`TRUNCATION_MARKER` appended when
        truncated) and whether truncation occurred.
    """
    kept: list[dict[str, object]] = []
    running_size = 0
    truncated = False
    for item in items:
        item_size = len(repr(item).encode("utf-8"))
        if running_size + item_size > byte_ceiling:
            truncated = True
            break
        kept.append(item)
        running_size += item_size
    if truncated:
        kept.append({TRUNCATION_MARKER: True})
    return kept, truncated


def _repr_size(value: object) -> int:
    """The UTF-8 length of ``value``'s ``repr``, the measure :func:`truncate_tool_output` uses."""
    return len(repr(value).encode("utf-8"))


def record_list_field(container: Mapping[str, object]) -> tuple[str, list[object]] | None:
    """Return the list to trim at a record boundary: ``items`` if a list, else the largest list.

    The largest is the non-empty list value with the greatest ``repr`` size. Returns ``None`` when
    ``container`` holds no such list.
    """
    items = container.get("items")
    if isinstance(items, list):
        return "items", items
    lists = [(key, value) for key, value in container.items() if isinstance(value, list) and value]
    if not lists:
        return None
    return max(lists, key=lambda pair: _repr_size(pair[1]))


def bound_tool_output(
    raw: Mapping[str, object], *, byte_ceiling: int = TOOL_RESULT_BYTE_CEILING
) -> tuple[dict[str, object], bool]:
    """Bound one tool result to ``byte_ceiling`` bytes by trimming its record list.

    The container is ``raw["body"]`` when that is a dict, else ``raw``; its record list is chosen
    by :func:`record_list_field`. When the container's ``repr`` exceeds ``byte_ceiling``, the list
    is cut at a record boundary, :data:`TRUNCATION_MARKER` is appended to it and
    :data:`TOOL_OUTPUT_TRUNCATED_KEY` is set to ``True`` in the container, so the bounded container
    stays within ``byte_ceiling`` (when its other fields alone fit). Non-dict list entries are not
    kept in a trimmed list.

    Returns:
        ``(bounded, True)`` when trimmed; otherwise ``(raw, False)``, with ``raw`` itself returned
        when it is a dict. A container over the ceiling with no list to trim is returned
        unchanged with ``False``.
    """
    unchanged = raw if isinstance(raw, dict) else dict(raw)
    body = raw.get("body")
    container: Mapping[str, object] = body if isinstance(body, dict) else raw
    if _repr_size(container) <= byte_ceiling:
        return unchanged, False
    record_list = record_list_field(container)
    if record_list is None:
        return unchanged, False
    field_name, records = record_list
    marker: dict[str, object] = {TRUNCATION_MARKER: True}

    def _with(kept: list[dict[str, object]]) -> dict[str, object]:
        return {**container, field_name: [*kept, marker], TOOL_OUTPUT_TRUNCATED_KEY: True}

    room = max(byte_ceiling - _repr_size(_with([])), 0)
    kept, _ = truncate_tool_output(
        [record for record in records if isinstance(record, dict)], byte_ceiling=room
    )
    if kept and kept[-1] == marker:
        kept.pop()
    bounded = _with(kept)
    while kept and _repr_size(bounded) > byte_ceiling:
        kept.pop()
        bounded = _with(kept)
    if isinstance(body, dict):
        return {**raw, "body": bounded}, True
    return bounded, True


def budget_exhausted_event(*, partial_work: str, error_message: str) -> AgentEvent:
    """Build the terminal event a turn emits when its budget is exhausted.

    Args:
        partial_work: A description of work already committed or proposed before the turn ended.
            Never empty; with nothing to report, say so (for example ``"no tool calls completed"``).
        error_message: A user-safe description of the exhaustion, shown to the caller.

    Returns:
        A terminal ``error`` event with ``error_kind="budget_exhausted"``.
    """
    return AgentEvent(
        kind="error",
        error_kind="budget_exhausted",
        error_message=error_message,
        partial_work=partial_work,
    )


__all__ = [
    "EVENT_KINDS",
    "TOOL_OUTPUT_TRUNCATED_KEY",
    "TOOL_RESULT_BYTE_CEILING",
    "TRUNCATION_MARKER",
    "AgentEvent",
    "CostLine",
    "ErrorKind",
    "EventKind",
    "ResolvedTierInfo",
    "bound_tool_output",
    "budget_exhausted_event",
    "record_list_field",
    "strip_control_chars",
    "truncate_tool_output",
]
