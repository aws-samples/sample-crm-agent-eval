# SPDX-License-Identifier: MIT
"""Opportunity stage machine.

Stages are totally ordered as declared in :data:`STAGES`; the last two are closed and the first four
open. :func:`classify_transition` classifies every transition into one :class:`TransitionKind`.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from crm.errors import Conflict, ValidationFailed, bounded_detail_value
from crm.toolspec.definitions import STAGES as _TOOLSPEC_STAGES

#: Opportunity lifecycle stages, ordered; re-exported from :mod:`crm.toolspec.definitions`.
STAGES: Final[tuple[str, ...]] = _TOOLSPEC_STAGES

#: The first four stages in :data:`STAGES`; an opportunity in one of these has not yet closed.
OPEN_STAGES: Final[frozenset[str]] = frozenset(STAGES[:4])

#: The last two stages in :data:`STAGES`; an opportunity in one of these is closed.
CLOSED_STAGES: Final[frozenset[str]] = frozenset(STAGES[4:])

_STAGE_INDEX: Final[dict[str, int]] = {stage: index for index, stage in enumerate(STAGES)}


class TransitionKind(Enum):
    """Classification of a transition between two members of :data:`STAGES`."""

    SAME_STAGE = "same_stage"
    ADVANCE = "advance"
    REGRESSION = "regression"
    CLOSE = "close"
    REOPEN = "reopen"
    CLOSED_LATERAL = "closed_lateral"


def classify_transition(from_stage: str, to_stage: str) -> TransitionKind:
    """Classify a stage transition between two members of :data:`STAGES`.

    Args:
        from_stage: The opportunity's current stage.
        to_stage: The proposed next stage.

    Returns:
        SAME_STAGE if ``from_stage == to_stage``.
        ADVANCE if both stages are open and ``to_stage`` has a higher index than ``from_stage``.
        REGRESSION if both stages are open and ``to_stage`` has a lower index than ``from_stage``.
        CLOSE if ``from_stage`` is open and ``to_stage`` is closed.
        REOPEN if ``from_stage`` is closed and ``to_stage`` is open.
        CLOSED_LATERAL if ``from_stage`` and ``to_stage`` are both closed but differ.

    Raises:
        ValueError: When either stage is not a member of :data:`STAGES`.
    """
    if from_stage not in _STAGE_INDEX:
        raise ValueError(f"unrecognised stage {from_stage!r}; expected one of {STAGES}")
    if to_stage not in _STAGE_INDEX:
        raise ValueError(f"unrecognised stage {to_stage!r}; expected one of {STAGES}")

    if from_stage == to_stage:
        return TransitionKind.SAME_STAGE

    from_open = from_stage in OPEN_STAGES
    to_open = to_stage in OPEN_STAGES

    if from_open and to_open:
        return (
            TransitionKind.ADVANCE
            if _STAGE_INDEX[to_stage] > _STAGE_INDEX[from_stage]
            else TransitionKind.REGRESSION
        )
    if from_open and not to_open:
        return TransitionKind.CLOSE
    if not from_open and to_open:
        return TransitionKind.REOPEN
    return TransitionKind.CLOSED_LATERAL


def classify_requested_transition(from_stage: str, to_stage: str, *, source: str) -> TransitionKind:
    """Classify a transition whose ``to_stage`` arrived as untrusted caller input.

    Unlike :func:`classify_transition`, failures are raised as :mod:`crm.errors` taxonomy members.

    Args:
        from_stage: The record's stored current stage.
        to_stage: The proposed next stage, as supplied by the caller.
        source: Argument path the caller supplied ``to_stage`` through (for example
            ``"after_payload.stage"``), reported in a rejection.

    Returns:
        The :class:`TransitionKind` :func:`classify_transition` yields for the pair.

    Raises:
        ValidationFailed: ``to_stage`` is not a member of :data:`STAGES`.
        Conflict: ``from_stage`` is not a member of :data:`STAGES`.
    """
    if to_stage not in _STAGE_INDEX:
        raise ValidationFailed(
            f"Unrecognised stage; expected one of {list(STAGES)}.",
            source=source,
            detail=f"reason=unrecognised_target_stage to_stage={bounded_detail_value(to_stage)}",
        )
    if from_stage not in _STAGE_INDEX:
        raise Conflict(
            "That record's current stage is not one this operation can act on.",
            detail="reason=unrecognised_current_stage",
        )
    return classify_transition(from_stage, to_stage)


#: Transition kinds that are permitted: every kind except an exit from a closed stage.
PERMITTED_TRANSITION_KINDS: Final[frozenset[TransitionKind]] = frozenset(
    {
        TransitionKind.SAME_STAGE,
        TransitionKind.ADVANCE,
        TransitionKind.REGRESSION,
        TransitionKind.CLOSE,
    }
)

#: Transition kinds refused outright: reopening a closed opportunity, or moving laterally between
#: two closed stages.
REFUSED_TRANSITION_KINDS: Final[frozenset[TransitionKind]] = frozenset(
    {TransitionKind.REOPEN, TransitionKind.CLOSED_LATERAL}
)


def is_permitted_transition(from_stage: str, to_stage: str) -> bool:
    """Return whether the transition from ``from_stage`` to ``to_stage`` is permitted.

    Args:
        from_stage: The opportunity's current stage.
        to_stage: The proposed next stage.

    Returns:
        ``True`` if :func:`classify_transition` yields a member of
        :data:`PERMITTED_TRANSITION_KINDS`, ``False`` if it yields a member of
        :data:`REFUSED_TRANSITION_KINDS`.
    """
    return classify_transition(from_stage, to_stage) in PERMITTED_TRANSITION_KINDS
