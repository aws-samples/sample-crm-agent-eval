# SPDX-License-Identifier: MIT
"""Record shape 4: every writer leads with the version and every reader refuses any other record.

The refusals are checked against a record that is otherwise readable, so each case fails for the
one reason it names; a positive control shows the same record loads once the defect is undone.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from scripts import proc_run

from eval import matrix
from eval.matrix import (
    RECORD_SHAPE_VERSION,
    RECORD_SHAPE_VERSION_KEY,
    TIER_SUMMARY_TOKEN_KEYS,
    TIER_TASK_COUNT_KEYS,
    ArmResult,
    RecordShapeRefused,
    archive_raw_run,
    arm_from_report_fields,
    build_matrix_result,
    render_json,
    require_record_shape,
    tier_comparison,
    tier_comparison_table,
)
from tests.unit.test_matrix_tier_comparison import _anthropic_arm

_LEGACY_ARTIFACT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "legacy_matrix_artifact.json"
)

_TIERS: tuple[str, ...] = ("supervisor", "subagent", "tool")

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _arm() -> ArmResult:
    return _anthropic_arm()


def _fields() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(_arm().to_report_fields())))


def _legacy_payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_LEGACY_ARTIFACT.read_text(encoding="utf-8")))


# ------------------------------------------------------------------------------------ constants


def test_the_version_is_the_integer_four_under_its_named_key() -> None:
    assert RECORD_SHAPE_VERSION == 4
    assert type(RECORD_SHAPE_VERSION) is int
    assert RECORD_SHAPE_VERSION_KEY == "record_shape_version"
    assert issubclass(RecordShapeRefused, ValueError)


# -------------------------------------------------------------------------------------- writers


def test_the_arm_report_leads_with_the_version() -> None:
    fields = _arm().to_report_fields()
    assert next(iter(fields)) == RECORD_SHAPE_VERSION_KEY
    assert fields[RECORD_SHAPE_VERSION_KEY] == RECORD_SHAPE_VERSION


def test_matrix_json_and_its_tier_comparison_block_lead_with_the_version() -> None:
    payload = json.loads(render_json(build_matrix_result([_arm()])))
    assert next(iter(payload)) == RECORD_SHAPE_VERSION_KEY
    assert payload[RECORD_SHAPE_VERSION_KEY] == RECORD_SHAPE_VERSION
    block = payload["tier_comparison"]
    assert next(iter(block)) == RECORD_SHAPE_VERSION_KEY
    assert block[RECORD_SHAPE_VERSION_KEY] == RECORD_SHAPE_VERSION
    assert next(iter(payload["arms"][0])) == RECORD_SHAPE_VERSION_KEY


def test_the_raw_archive_file_carries_the_version_first(tmp_path: Path) -> None:
    path = archive_raw_run(_arm(), tmp_path)
    assert next(iter(json.loads(path.read_text(encoding="utf-8")))) == RECORD_SHAPE_VERSION_KEY


# ------------------------------------------------------------------------- require_record_shape


def test_require_record_shape_accepts_version_four() -> None:
    require_record_shape({RECORD_SHAPE_VERSION_KEY: 4}, subject="x")


@pytest.mark.parametrize(
    ("record", "found"),
    [
        ({}, "absent"),
        ({RECORD_SHAPE_VERSION_KEY: 1}, "1"),
        ({RECORD_SHAPE_VERSION_KEY: 2}, "2"),
        ({RECORD_SHAPE_VERSION_KEY: 3}, "3"),
        ({RECORD_SHAPE_VERSION_KEY: None}, "None"),
        ({RECORD_SHAPE_VERSION_KEY: "4"}, "'4'"),
        ({RECORD_SHAPE_VERSION_KEY: 4.0}, "4.0"),
        ({RECORD_SHAPE_VERSION_KEY: True}, "True"),
    ],
    ids=["absent", "one", "two", "three", "null", "string", "float", "bool"],
)
def test_require_record_shape_names_the_found_and_required_version(
    record: dict[str, object], found: str
) -> None:
    with pytest.raises(RecordShapeRefused) as excinfo:
        require_record_shape(record, subject="the subject")
    assert str(excinfo.value) == (
        f"the subject: record shape {found}, required 4 — recorded in an earlier format; "
        "not rendered"
    )


# ------------------------------------------------------------------------ arm_from_report_fields


def test_a_shape_four_arm_loads_as_the_positive_control() -> None:
    assert arm_from_report_fields(_fields()) == _arm()


@pytest.mark.parametrize("version", [None, 1, 2, 3], ids=["absent", "one", "two", "three"])
def test_an_arm_of_another_record_shape_is_refused(version: int | None) -> None:
    fields = _fields()
    if version is None:
        del fields[RECORD_SHAPE_VERSION_KEY]
    else:
        fields[RECORD_SHAPE_VERSION_KEY] = version
    found = "absent" if version is None else str(version)
    with pytest.raises(RecordShapeRefused, match=f"raw archive: record shape {found}, required 4"):
        arm_from_report_fields(fields)


def _missing_field_paths() -> list[tuple[str, ...]]:
    """Every required field of an arm report, as the key path that reaches it."""
    paths: list[tuple[str, ...]] = [("provenance", name) for name in matrix._PROVENANCE_FIELDS]
    metrics = matrix.TIER_TASK_METRICS_KEY
    paths.extend([("tier_summary",), (metrics,), (metrics, "by_tier")])
    for tier in _TIERS:
        paths.append(("tier_summary", tier))
        paths.extend(("tier_summary", tier, key) for key in TIER_SUMMARY_TOKEN_KEYS)
        paths.append((metrics, "by_tier", tier))
        paths.extend((metrics, "by_tier", tier, key) for key in TIER_TASK_COUNT_KEYS)
    return paths


@pytest.mark.parametrize("path", _missing_field_paths(), ids=".".join)
def test_an_arm_missing_a_required_field_is_refused_naming_it(path: tuple[str, ...]) -> None:
    fields = _fields()
    parent: dict[str, Any] = fields
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    with pytest.raises(RecordShapeRefused) as excinfo:
        arm_from_report_fields(fields)
    assert str(excinfo.value) == f"raw archive: missing required field {'.'.join(path)}"


@pytest.mark.parametrize("value", [None, "7", 7.0, True], ids=["null", "string", "float", "bool"])
@pytest.mark.parametrize("key", TIER_SUMMARY_TOKEN_KEYS)
def test_a_non_integer_token_total_is_refused(key: str, value: object) -> None:
    fields = _fields()
    fields["tier_summary"]["subagent"][key] = value
    with pytest.raises(RecordShapeRefused, match=rf"tier_summary\.subagent\.{key}$"):
        arm_from_report_fields(fields)


def test_the_prompt_caching_text_round_trips_after_the_bundle_digest() -> None:
    fields = _fields()
    names = list(fields["provenance"])
    assert names.index("prompt_caching") == names.index("prompt_bundle_sha256") + 1
    assert arm_from_report_fields(fields).provenance.prompt_caching == "explicit cache points"


@pytest.mark.parametrize("value", [None, 1, ["x"]], ids=["null", "int", "list"])
def test_a_non_string_prompt_caching_is_refused(value: object) -> None:
    fields = _fields()
    fields["provenance"]["prompt_caching"] = value
    with pytest.raises(RecordShapeRefused, match=r"provenance\.prompt_caching$"):
        arm_from_report_fields(fields)


def test_the_output_cap_and_dirty_tree_follow_prompt_caching_and_round_trip() -> None:
    fields = _fields()
    names = list(fields["provenance"])
    assert names[-3:] == ["prompt_caching", "output_token_cap", "dirty_tree"]
    provenance = arm_from_report_fields(fields).provenance
    assert (provenance.output_token_cap, provenance.dirty_tree) == (16_384, False)


@pytest.mark.parametrize(
    "value", [None, "16384", 16384.0, True], ids=["null", "str", "float", "bool"]
)
def test_a_non_integer_output_token_cap_is_refused(value: object) -> None:
    fields = _fields()
    fields["provenance"]["output_token_cap"] = value
    with pytest.raises(RecordShapeRefused, match=r"provenance\.output_token_cap$"):
        arm_from_report_fields(fields)


@pytest.mark.parametrize("value", [0, 1, "false"], ids=["zero", "one", "string"])
def test_a_non_boolean_dirty_tree_is_refused(value: object) -> None:
    fields = _fields()
    fields["provenance"]["dirty_tree"] = value
    with pytest.raises(RecordShapeRefused, match=r"provenance\.dirty_tree$"):
        arm_from_report_fields(fields)


def test_a_null_dirty_tree_with_a_known_revision_is_refused() -> None:
    fields = _fields()
    fields["provenance"]["dirty_tree"] = None
    with pytest.raises(ValueError, match="dirty_tree None is inconsistent"):
        arm_from_report_fields(fields)


def test_a_null_dirty_tree_with_an_unknown_revision_loads() -> None:
    fields = _fields()
    fields["provenance"].update(git_revision=matrix.UNKNOWN_GIT_REVISION, dirty_tree=None)
    assert arm_from_report_fields(fields).provenance.dirty_tree is None


@pytest.mark.parametrize("dirty_tree", [True, False])
def test_a_measured_dirty_tree_with_an_unknown_revision_is_refused(dirty_tree: bool) -> None:
    provenance = _arm().provenance
    with pytest.raises(ValueError, match="is inconsistent with git_revision 'unknown'"):
        dataclasses.replace(
            provenance, git_revision=matrix.UNKNOWN_GIT_REVISION, dirty_tree=dirty_tree
        )


@pytest.mark.parametrize("cap", [0, -1])
def test_a_non_positive_output_token_cap_is_refused_at_construction(cap: int) -> None:
    with pytest.raises(ValueError, match="output_token_cap must be a positive int"):
        dataclasses.replace(_arm().provenance, output_token_cap=cap)


@pytest.mark.parametrize(
    ("tier", "key", "value"),
    [
        ("supervisor", "budget_exhausted_task_count", None),
        ("supervisor", "truncated_tool_call_count", None),
        ("tool", "truncated_task_count", "3"),
        ("tool", "budget_exhausted_task_count", True),
        ("subagent", "budget_exhausted_task_count", None),
        ("subagent", "truncated_tool_call_count", 0),
        ("subagent", "truncated_task_count", 0),
        ("subagent", "output_truncated_call_count", None),
        ("subagent", "output_truncated_task_count", None),
        ("supervisor", "output_truncated_call_count", "1"),
        ("tool", "output_truncated_task_count", True),
    ],
)
def test_a_tier_task_count_of_the_wrong_type_is_refused(tier: str, key: str, value: object) -> None:
    """Counts are integers, except the subagent tool-result truncation pair, which must be null."""
    fields = _fields()
    fields[matrix.TIER_TASK_METRICS_KEY]["by_tier"][tier][key] = value
    with pytest.raises(RecordShapeRefused) as excinfo:
        arm_from_report_fields(fields)
    assert str(excinfo.value) == (
        f"raw archive: missing required field {matrix.TIER_TASK_METRICS_KEY}.by_tier.{tier}.{key}"
    )


def test_only_the_subagent_tool_truncation_pair_is_null_in_the_positive_control() -> None:
    by_tier = _fields()[matrix.TIER_TASK_METRICS_KEY]["by_tier"]
    assert [by_tier["subagent"][key] for key in TIER_TASK_COUNT_KEYS] == [1, None, None, 5, 4]
    assert all(
        type(by_tier[tier][key]) is int
        for tier in ("supervisor", "tool")
        for key in TIER_TASK_COUNT_KEYS
    )


def test_a_legacy_temperature_key_is_not_carried_into_a_reloaded_arm() -> None:
    fields = _fields()
    fields["provenance"]["temperature"] = 0.2
    reloaded = arm_from_report_fields(fields).to_report_fields()
    assert "temperature" not in cast(dict[str, object], reloaded["provenance"])


# ------------------------------------------------------------------------ tier_comparison_table


def _summary() -> dict[str, Any]:
    return cast(
        dict[str, Any], json.loads(json.dumps(tier_comparison(build_matrix_result([_arm()]))))
    )


def test_a_shape_four_summary_formats_as_the_positive_control() -> None:
    summary = _summary()
    assert tier_comparison_table(summary)["headers"] == [
        "Metric",
        *(column["label"] for column in summary["columns"]),
    ]


@pytest.mark.parametrize("version", [None, 1, 2, 3], ids=["absent", "one", "two", "three"])
def test_a_summary_of_another_record_shape_is_refused(version: int | None) -> None:
    summary = _summary()
    if version is None:
        del summary[RECORD_SHAPE_VERSION_KEY]
    else:
        summary[RECORD_SHAPE_VERSION_KEY] = version
    with pytest.raises(RecordShapeRefused, match="tier comparison summary: record shape"):
        tier_comparison_table(summary)


@pytest.mark.parametrize(
    "key", ["cost_per_applicable_task", *TIER_SUMMARY_TOKEN_KEYS, *TIER_TASK_COUNT_KEYS]
)
@pytest.mark.parametrize("tier", _TIERS)
def test_a_summary_entry_missing_a_required_key_is_refused(tier: str, key: str) -> None:
    summary = _summary()
    del summary["tiers"][tier][0][key]
    with pytest.raises(RecordShapeRefused) as excinfo:
        tier_comparison_table(summary)
    assert str(excinfo.value) == (
        f"tier comparison summary: missing required field tiers.{tier}[0].{key}"
    )


# ----------------------------------------------------------------------- the tracked legacy file


def test_every_arm_of_the_legacy_artifact_is_refused() -> None:
    arms = _legacy_payload()["arms"]
    assert arms
    for fields in arms:
        assert RECORD_SHAPE_VERSION_KEY not in fields
        with pytest.raises(RecordShapeRefused, match="record shape absent, required 4"):
            arm_from_report_fields(fields)


def test_the_legacy_artifact_matrix_is_refused_at_its_top_level() -> None:
    with pytest.raises(RecordShapeRefused, match="record shape absent"):
        require_record_shape(_legacy_payload(), subject="matrix.json")


def test_the_legacy_artifact_refusal_is_the_version_not_a_field() -> None:
    """Control: stamping the version on a legacy arm moves the refusal to a missing field."""
    fields = {RECORD_SHAPE_VERSION_KEY: RECORD_SHAPE_VERSION, **_legacy_payload()["arms"][0]}
    with pytest.raises(RecordShapeRefused, match="missing required field"):
        arm_from_report_fields(fields)


# ------------------------------------------------------------------------------ python -m eval.matrix


def _archive_legacy_arms(raw_dir: Path) -> None:
    raw_dir.mkdir(parents=True)
    for index, fields in enumerate(_legacy_payload()["arms"]):
        (raw_dir / f"legacy-{index}.json").write_text(json.dumps(fields), encoding="utf-8")


def test_the_re_render_cli_exits_2_and_writes_nothing_for_a_legacy_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_dir, output_dir = tmp_path / "raw", tmp_path / "out"
    _archive_legacy_arms(raw_dir)
    assert matrix.main(["--raw-dir", str(raw_dir), "--output-dir", str(output_dir)]) == 2
    assert "record shape absent, required 4" in capsys.readouterr().err
    assert not output_dir.exists()


def test_one_legacy_archive_beside_a_shape_four_one_still_writes_nothing(tmp_path: Path) -> None:
    raw_dir, output_dir = tmp_path / "raw", tmp_path / "out"
    _archive_legacy_arms(raw_dir)
    archive_raw_run(_arm(), raw_dir)
    assert matrix.main(["--raw-dir", str(raw_dir), "--output-dir", str(output_dir)]) == 2
    assert not output_dir.exists()


def test_the_re_render_cli_exits_2_and_writes_nothing_for_mismatched_prompt_bundles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_dir, output_dir = tmp_path / "raw", tmp_path / "out"
    first = _arm()
    second = dataclasses.replace(
        first,
        provenance=dataclasses.replace(
            first.provenance, model_id="openai.gpt-6.2-sol", prompt_bundle_sha256="2" * 64
        ),
    )
    archive_raw_run(first, raw_dir)
    archive_raw_run(second, raw_dir)
    assert matrix.main(["--raw-dir", str(raw_dir), "--output-dir", str(output_dir)]) == 2
    assert "different prompt bundles" in capsys.readouterr().err
    assert not output_dir.exists()


def test_the_module_entry_point_exits_2_with_the_refusal(tmp_path: Path) -> None:
    raw_dir, output_dir = tmp_path / "raw", tmp_path / "out"
    _archive_legacy_arms(raw_dir)
    result = proc_run.run(
        [
            sys.executable,
            "-m",
            "eval.matrix",
            "--raw-dir",
            str(raw_dir),
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 2, result.stderr
    assert "record shape absent, required 4" in result.stderr
    assert not output_dir.exists()


def test_the_module_entry_point_renders_a_shape_four_archive(tmp_path: Path) -> None:
    """Control: the same invocation succeeds once the archive is record shape 4."""
    raw_dir, output_dir = tmp_path / "raw", tmp_path / "out"
    archive_raw_run(_arm(), raw_dir)
    result = proc_run.run(
        [
            sys.executable,
            "-m",
            "eval.matrix",
            "--raw-dir",
            str(raw_dir),
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "matrix.csv",
        "matrix.json",
        "matrix.md",
    ]
