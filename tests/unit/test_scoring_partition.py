# SPDX-License-Identifier: MIT
"""Tests for the scored/unscored partition in `eval.scoring.aggregate_scores`.

The counts must equal the tasks actually averaged into `mean_correctness` (`correctness is not
None`), not a partition on `scoring_mode`.
"""

from __future__ import annotations

from eval.scoring import AggregateScore, AssertionResult, TaskScore, aggregate_scores


def _task_score(
    task_id: str,
    *,
    correctness: float | None,
    scoring_mode: str = "machine",
    bad_write: bool = False,
) -> TaskScore:
    return TaskScore(
        task_id=task_id,
        scoring_mode=scoring_mode,
        correctness=correctness,
        unscored_reason=None if correctness is not None else "unscored for this test",
        bad_write=bad_write,
        tool_call_correct=True,
        assertion_results=(AssertionResult("x", True),),
    )


def test_scored_and_unscored_counts_equal_the_actual_correctness_partition() -> None:
    """13 tasks with a real correctness value, 7 with `None` -> scored=13, unscored=7."""
    scores = [_task_score(f"gt-{i}", correctness=0.8) for i in range(13)]
    scores += [_task_score(f"gt-unscored-{i}", correctness=None) for i in range(7)]

    aggregate = aggregate_scores(scores)

    assert aggregate.scored_task_count == 13
    assert aggregate.unscored_task_count == 7
    assert aggregate.scored_task_count + aggregate.unscored_task_count == len(scores)


def test_a_machine_task_reported_unscored_is_excluded_from_scored_count_not_just_from_the_mean() -> (
    None
):
    """A `"machine"` task with `correctness=None` is counted unscored."""
    scores = [
        _task_score("gt-scored-1", correctness=1.0, scoring_mode="machine"),
        _task_score("gt-scored-2", correctness=0.5, scoring_mode="machine"),
        # Unscored by score_task's no-positive-assertion rule while scoring_mode stays "machine".
        _task_score("gt-no-positive-assertion", correctness=None, scoring_mode="machine"),
    ]

    aggregate = aggregate_scores(scores)

    assert aggregate.scored_task_count == 2
    assert aggregate.unscored_task_count == 1
    assert aggregate.mean_correctness == 0.75


def test_prose_unscored_and_no_positive_assertion_tasks_are_both_counted_as_unscored() -> None:
    """A `"prose_unscored"` and a `"machine"` no-positive-assertion task both count unscored."""
    scores = [
        _task_score("gt-prose", correctness=None, scoring_mode="prose_unscored"),
        _task_score("gt-no-positive-assertion", correctness=None, scoring_mode="machine"),
        _task_score("gt-real", correctness=0.9, scoring_mode="machine"),
    ]

    aggregate = aggregate_scores(scores)

    assert aggregate.scored_task_count == 1
    assert aggregate.unscored_task_count == 2
    assert aggregate.mean_correctness == 0.9


def test_scored_task_count_matches_the_actual_number_of_values_averaged() -> None:
    """`mean_correctness` is the mean of exactly `scored_task_count` values."""
    scores = [
        _task_score("gt-a", correctness=1.0),
        _task_score("gt-b", correctness=0.0),
        _task_score("gt-c", correctness=None),
    ]

    aggregate = aggregate_scores(scores)

    assert aggregate.scored_task_count == 2
    assert aggregate.mean_correctness == 0.5  # (1.0 + 0.0) / 2, not / 3 and not / 1


def test_bad_write_rate_denominator_is_the_full_task_count_regardless_of_scored_partition() -> None:
    """`bad_write_rate` is computed over every task, scored or not."""
    scores = [
        _task_score("gt-a", correctness=1.0, bad_write=True),
        _task_score("gt-b", correctness=None, bad_write=False),
    ]

    aggregate = aggregate_scores(scores)

    assert aggregate.bad_write_rate == 0.5


def test_empty_scores_report_zero_counts_and_no_mean() -> None:
    """Empty input yields zero counts and a `None` mean."""
    aggregate = aggregate_scores([])

    assert aggregate.scored_task_count == 0
    assert aggregate.unscored_task_count == 0
    assert aggregate.mean_correctness is None


def test_report_fields_render_the_corrected_partition() -> None:
    """`to_report_fields` carries the scored and unscored counts verbatim."""
    scores = [_task_score("gt-a", correctness=0.5)] + [
        _task_score(f"gt-u{i}", correctness=None) for i in range(2)
    ]
    aggregate = aggregate_scores(scores)
    fields = aggregate.to_report_fields()

    assert fields["scored_task_count"] == 1
    assert fields["unscored_task_count"] == 2


def _reference_pre_fix_partition(scores: list[TaskScore]) -> AggregateScore:
    """Return an `AggregateScore` partitioned on `scoring_mode` alone, for contrast."""
    scored = [s for s in scores if s.scoring_mode != "prose_unscored"]
    unscored = [s for s in scores if s.scoring_mode == "prose_unscored"]
    correctness_values = [s.correctness for s in scored if s.correctness is not None]
    mean = sum(correctness_values) / len(correctness_values) if correctness_values else None
    return AggregateScore(
        scored_task_count=len(scored),
        unscored_task_count=len(unscored),
        mean_correctness=mean,
        bad_write_rate=0.0,
        tool_call_correct_rate=None,
    )


def test_break_the_code_pre_fix_partition_reports_dishonest_counts_on_the_same_input() -> None:
    """A `scoring_mode`-only partition reports 18/2 where `aggregate_scores` reports 13/7."""
    # 13 tasks with a real machine correctness value.
    scores = [
        _task_score(f"gt-scored-{i}", correctness=0.8, scoring_mode="machine") for i in range(13)
    ]
    # 5 "machine" tasks that score_task reported unscored (no positive assertion).
    scores += [
        _task_score(f"gt-no-assertion-{i}", correctness=None, scoring_mode="machine")
        for i in range(5)
    ]
    # 2 genuinely prose_unscored tasks.
    scores += [
        _task_score(f"gt-prose-{i}", correctness=None, scoring_mode="prose_unscored")
        for i in range(2)
    ]
    assert len(scores) == 20

    pre_fix = _reference_pre_fix_partition(scores)
    assert pre_fix.scored_task_count == 18  # 13 real + 5 miscounted
    assert pre_fix.unscored_task_count == 2  # only the 2 genuinely prose ones
    assert pre_fix.mean_correctness == 0.8

    fixed = aggregate_scores(scores)
    assert fixed.scored_task_count == 13
    assert fixed.unscored_task_count == 7  # 5 no-assertion + 2 prose
    assert fixed.mean_correctness == 0.8
