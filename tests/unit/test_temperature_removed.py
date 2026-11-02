# SPDX-License-Identifier: MIT
"""Absence gate for the removed tuning parameter and the retired models.

Every tracked file under :data:`SCAN_ROOTS` is searched, case-insensitively, for
:data:`REMOVED_TOKEN` and for each retired model id (bare and as its `global.` inference profile).
A hit is allowed only in a path named in :data:`TEMPERATURE_ALLOWLIST` or
:data:`RETIRED_ID_ALLOWLIST`, each entry tagged with exactly one :class:`Category`. An entry
that no longer matches anything fails too, so the allowlists cannot go stale. The controls plant
hits in a minimal scratch git repository under `tmp_path`, never in the working tree.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run
from scripts.smoke.runtime_bedrock_iam import RETIRED_INFERENCE_PROFILES

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: The tracked trees this gate scans; `README.md` and `docs/` are not among them.
SCAN_ROOTS: Final[tuple[str, ...]] = (
    "src",
    "frontend/src",
    "frontend/scripts",
    "scripts",
    "infra",
    "config",
    "tests",
    "fixtures",
)

REMOVED_TOKEN: Final[str] = "temperature"

#: Each retired model id, bare and as its `global.` profile, derived from the one retired-profile constant.
RETIRED_MODEL_TOKENS: Final[tuple[str, ...]] = tuple(
    sorted(
        set(RETIRED_INFERENCE_PROFILES)
        | {profile.removeprefix("global.") for profile in RETIRED_INFERENCE_PROFILES}
    )
)


class Category(enum.Enum):
    """Why a path may still name the removed parameter or a retired model."""

    STORED_ROW_READER = "parse_stored_tuning reads a legacy schema-2 row"
    LEGACY_EVAL_HISTORY = "past eval artifacts and run records render a legacy value"
    RECORDED_OR_LEGACY_FIXTURE = "a recorded or *legacy* fixture, or a test reading one"
    REFUSAL = "code or a test that refuses the parameter, or sends it to prove refusal"
    ABSENCE_ASSERTION = "a test asserting the parameter is absent from a payload, type or artifact"
    RETIRED_PROFILE_CONSTANT = "the one named constant of retired inference profiles"
    PLAN_FIXTURE_PENDING_RECAPTURE = "plan fixtures, re-captured from a real plan"
    THIS_GATE = "the absence gate itself"


#: An entry ending in `/` covers every tracked file under that directory.
TEMPERATURE_ALLOWLIST: Final[Mapping[str, Category]] = {
    "config/model_capabilities.yaml": Category.REFUSAL,
    "frontend/src/components/AssistantPanel/PanelTuning.test.tsx": Category.REFUSAL,
    "frontend/src/components/AssistantPanel/wireTuning.test.ts": Category.ABSENCE_ASSERTION,
    "frontend/src/components/tuning/TuningControls.test.tsx": Category.REFUSAL,
    "frontend/src/components/tuning/capabilityMatrix.test.ts": Category.ABSENCE_ASSERTION,
    "frontend/src/components/tuning/sessionTuning.test.tsx": Category.REFUSAL,
    "frontend/src/components/tuning/validateTuning.test.ts": Category.REFUSAL,
    "frontend/src/pages/evals/": Category.LEGACY_EVAL_HISTORY,
    "frontend/src/pages/settings/SettingsPage.test.tsx": Category.REFUSAL,
    "scripts/smoke/mcp_chat_gateway.py": Category.REFUSAL,
    "scripts/smoke/three_tier_chat.py": Category.REFUSAL,
    "scripts/smoke/user_settings_api.py": Category.REFUSAL,
    "src/agents/runtime_entrypoint.py": Category.REFUSAL,
    "src/eval/matrix.py": Category.LEGACY_EVAL_HISTORY,
    "src/providers/capabilities.py": Category.REFUSAL,
    "src/providers/tier_tuning.py": Category.STORED_ROW_READER,
    "tests/e2e/browser/journeys/14-tier-tuning/tier-tuning.spec.ts": Category.REFUSAL,
    "tests/e2e/browser/journeys/README.md": Category.REFUSAL,
    "tests/fixtures/eval/legacy_matrix_artifact.json": Category.RECORDED_OR_LEGACY_FIXTURE,
    "tests/integration/fixtures/bedrock/": Category.RECORDED_OR_LEGACY_FIXTURE,
    "tests/integration/test_adapters_recorded.py": Category.RECORDED_OR_LEGACY_FIXTURE,
    "tests/integration/test_eval_turn_contract.py": Category.REFUSAL,
    "tests/integration/test_migrate_supervisor_effort_ddb.py": Category.STORED_ROW_READER,
    "tests/integration/test_tier_tuning_wire_contract.py": Category.REFUSAL,
    "tests/integration/test_user_settings_store.py": Category.STORED_ROW_READER,
    "tests/stub/test_mixed_family_turn.py": Category.ABSENCE_ASSERTION,
    "tests/unit/test_adapters.py": Category.ABSENCE_ASSERTION,
    "tests/unit/test_adapters_openai.py": Category.ABSENCE_ASSERTION,
    "tests/unit/test_aws_eventstream.py": Category.RECORDED_OR_LEGACY_FIXTURE,
    "tests/unit/test_chat_target.py": Category.REFUSAL,
    "tests/unit/test_event_schema_arrays.py": Category.REFUSAL,
    "tests/unit/test_events.py": Category.REFUSAL,
    "tests/unit/test_handler_capabilities.py": Category.ABSENCE_ASSERTION,
    "tests/unit/test_harness.py": Category.ABSENCE_ASSERTION,
    "tests/unit/test_matrix.py": Category.ABSENCE_ASSERTION,
    "tests/unit/test_model_capabilities.py": Category.REFUSAL,
    "tests/unit/test_provider_base.py": Category.ABSENCE_ASSERTION,
    "tests/unit/test_provider_request_tool_schema_contract.py": Category.ABSENCE_ASSERTION,
    "tests/unit/test_runtime_entrypoint.py": Category.REFUSAL,
    "tests/unit/test_runtime_eval_turn.py": Category.REFUSAL,
    "tests/unit/test_runtime_server.py": Category.REFUSAL,
    "tests/unit/test_smoke_mcp_chat_gateway.py": Category.REFUSAL,
    "tests/unit/test_smoke_three_tier_chat.py": Category.REFUSAL,
    "tests/unit/test_smoke_user_settings_api.py": Category.REFUSAL,
    "tests/unit/test_temperature_removed.py": Category.THIS_GATE,
    "tests/unit/test_tier_tuning.py": Category.STORED_ROW_READER,
    "tests/unit/test_user_settings.py": Category.STORED_ROW_READER,
}

#: Exact occurrence count of :data:`REMOVED_TOKEN` in each production file entry, and the total
#: across each directory entry, measured with the gate's own matcher.
TEMPERATURE_OCCURRENCE_PINS: Final[Mapping[str, int]] = {
    "config/model_capabilities.yaml": 1,
    "frontend/src/pages/evals/": 39,
    "scripts/smoke/mcp_chat_gateway.py": 7,
    "scripts/smoke/three_tier_chat.py": 16,
    "scripts/smoke/user_settings_api.py": 15,
    "src/agents/runtime_entrypoint.py": 1,
    "src/eval/matrix.py": 18,
    "src/providers/capabilities.py": 7,
    "src/providers/tier_tuning.py": 7,
    "tests/integration/fixtures/bedrock/": 27,
}

#: Allowlist entries whose occurrences are pinned: production files and directory entries.
PINNED_ROOTS: Final[tuple[str, ...]] = ("src/", "scripts/", "config/")

RETIRED_ID_ALLOWLIST: Final[Mapping[str, Category]] = {
    "scripts/smoke/runtime_bedrock_iam.py": Category.RETIRED_PROFILE_CONSTANT,
    "tests/fixtures/eval/legacy_matrix_artifact.json": Category.RECORDED_OR_LEGACY_FIXTURE,
}


def files_containing(root: Path, tokens: Iterable[str], roots: Iterable[str]) -> set[str]:
    """Tracked files under `roots` in the repository at `root` naming any token, case-insensitively.

    Raises:
        RuntimeError: `git grep` failed rather than finding nothing.
    """
    patterns = [arg for token in tokens for arg in ("-e", token)]
    result = proc_run.run(
        ["git", "-C", str(root), "grep", "-I", "-i", "-l", "-F", *patterns, "--", *roots],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed ({result.returncode}): {result.stderr}")
    return {line for line in result.stdout.splitlines() if line}


def occurrence_counts(root: Path, token: str, roots: Iterable[str]) -> dict[str, int]:
    """Per tracked file under `roots`, how many times `token` occurs, case-insensitively.

    Raises:
        RuntimeError: `git grep` failed rather than finding nothing.
    """
    result = proc_run.run(
        [
            "git",
            "-C",
            str(root),
            "grep",
            "-I",
            "-i",
            "-o",
            "-F",
            "--null",
            "-e",
            token,
            "--",
            *roots,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed ({result.returncode}): {result.stderr}")
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if line:
            path = line.split("\0", 1)[0]
            counts[path] = counts.get(path, 0) + 1
    return counts


def pin_mismatches(counts: Mapping[str, int], pins: Mapping[str, int]) -> list[str]:
    """Pinned entries whose measured occurrence count differs from the pin, naming both, sorted."""
    mismatches = []
    for entry, pinned in sorted(pins.items()):
        measured = sum(n for path, n in counts.items() if _covers(entry, path))
        if measured != pinned:
            mismatches.append(f"{entry}: {measured} occurrences, pinned {pinned}")
    return mismatches


def tracked_files(root: Path, roots: Iterable[str]) -> set[str]:
    """Every tracked file under `roots` in the repository at `root`."""
    result = proc_run.run(
        ["git", "-C", str(root), "ls-files", "--", *roots],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def _covers(entry: str, path: str) -> bool:
    return path.startswith(entry) if entry.endswith("/") else path == entry


def violations(hits: Iterable[str], allowlist: Mapping[str, Category]) -> list[str]:
    """Hits that no allowlist entry covers, sorted."""
    return sorted(path for path in hits if not any(_covers(entry, path) for entry in allowlist))


def stale_entries(
    hits: Iterable[str], tracked: Iterable[str], allowlist: Mapping[str, Category]
) -> list[str]:
    """Allowlist entries that name no tracked path or cover no hit, sorted."""
    hit_set, tracked_set = set(hits), set(tracked)
    return sorted(
        entry
        for entry in allowlist
        if not any(_covers(entry, path) for path in tracked_set)
        or not any(_covers(entry, path) for path in hit_set)
    )


def test_the_removed_parameter_appears_only_in_allowlisted_files() -> None:
    hits = files_containing(REPO_ROOT, (REMOVED_TOKEN,), SCAN_ROOTS)
    assert violations(hits, TEMPERATURE_ALLOWLIST) == []


def test_retired_model_ids_appear_only_in_allowlisted_files() -> None:
    hits = files_containing(REPO_ROOT, RETIRED_MODEL_TOKENS, SCAN_ROOTS)
    assert violations(hits, RETIRED_ID_ALLOWLIST) == []


@pytest.mark.parametrize(
    ("tokens", "allowlist"),
    [
        pytest.param((REMOVED_TOKEN,), TEMPERATURE_ALLOWLIST, id="temperature"),
        pytest.param(RETIRED_MODEL_TOKENS, RETIRED_ID_ALLOWLIST, id="retired-ids"),
    ],
)
def test_no_allowlist_entry_is_stale(
    tokens: tuple[str, ...], allowlist: Mapping[str, Category]
) -> None:
    hits = files_containing(REPO_ROOT, tokens, SCAN_ROOTS)
    assert stale_entries(hits, tracked_files(REPO_ROOT, SCAN_ROOTS), allowlist) == []


def test_each_pinned_entry_holds_exactly_its_pinned_occurrence_count() -> None:
    counts = occurrence_counts(REPO_ROOT, REMOVED_TOKEN, SCAN_ROOTS)
    assert pin_mismatches(counts, TEMPERATURE_OCCURRENCE_PINS) == []


def test_every_production_and_directory_entry_is_pinned_and_only_those() -> None:
    expected = {
        entry
        for entry in TEMPERATURE_ALLOWLIST
        if entry.endswith("/") or entry.startswith(PINNED_ROOTS)
    }
    assert set(TEMPERATURE_OCCURRENCE_PINS) == expected


def test_every_allowlist_entry_lies_inside_the_scan_roots() -> None:
    for entry in (*TEMPERATURE_ALLOWLIST, *RETIRED_ID_ALLOWLIST):
        assert any(entry == root or entry.startswith(f"{root}/") for root in SCAN_ROOTS), entry


def test_the_retired_tokens_cover_each_profile_and_its_bare_model_id() -> None:
    assert RETIRED_INFERENCE_PROFILES
    for profile in RETIRED_INFERENCE_PROFILES:
        assert profile.startswith("global.")
        assert profile in RETIRED_MODEL_TOKENS
        assert profile.removeprefix("global.") in RETIRED_MODEL_TOKENS


# --- controls, against a minimal scratch repository -------------------------------------------


def _git(root: Path, *args: str) -> None:
    proc_run.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    """A three-file git repository with no hit, so each control plants exactly one."""
    _git(tmp_path, "init", "-q")
    for relative, text in {
        "src/clean.py": "EFFORT = 'low'\n",
        "tests/unit/test_clean.py": "def test_nothing() -> None: ...\n",
        "docs/notes.md": "nothing to see\n",
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    return tmp_path


def _plant(root: Path, relative: str, text: str, *, track: bool = True) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if track:
        _git(root, "add", relative)


def test_control_an_unmutated_scratch_repo_has_no_hit(scratch_repo: Path) -> None:
    assert files_containing(scratch_repo, (REMOVED_TOKEN,), SCAN_ROOTS) == set()
    assert files_containing(scratch_repo, RETIRED_MODEL_TOKENS, SCAN_ROOTS) == set()


@pytest.mark.parametrize("spelling", ["temperature", "Temperature", "TEMPERATURE"])
def test_control_a_planted_parameter_in_any_case_is_a_violation(
    scratch_repo: Path, spelling: str
) -> None:
    _plant(scratch_repo, "src/tuning.py", f"{spelling} = 0.2\n")
    hits = files_containing(scratch_repo, (REMOVED_TOKEN,), SCAN_ROOTS)
    assert violations(hits, TEMPERATURE_ALLOWLIST) == ["src/tuning.py"]


@pytest.mark.parametrize("token", RETIRED_MODEL_TOKENS)
def test_control_a_planted_retired_id_is_a_violation(scratch_repo: Path, token: str) -> None:
    _plant(scratch_repo, "config/models.yaml", f"model: {token}\n")
    hits = files_containing(scratch_repo, RETIRED_MODEL_TOKENS, SCAN_ROOTS)
    assert violations(hits, RETIRED_ID_ALLOWLIST) == ["config/models.yaml"]


def test_control_an_allowlisted_hit_is_not_a_violation(scratch_repo: Path) -> None:
    _plant(scratch_repo, "tests/unit/test_temperature_removed.py", "temperature\n")
    hits = files_containing(scratch_repo, (REMOVED_TOKEN,), SCAN_ROOTS)
    assert hits == {"tests/unit/test_temperature_removed.py"}
    assert violations(hits, TEMPERATURE_ALLOWLIST) == []


def test_control_a_directory_entry_covers_only_its_own_subtree(scratch_repo: Path) -> None:
    allowlist = {"tests/fixtures/plans/": Category.PLAN_FIXTURE_PENDING_RECAPTURE}
    _plant(scratch_repo, "tests/fixtures/plans/compliant.json", RETIRED_MODEL_TOKENS[0])
    _plant(scratch_repo, "tests/fixtures/plansx/other.json", RETIRED_MODEL_TOKENS[0])
    hits = files_containing(scratch_repo, RETIRED_MODEL_TOKENS, SCAN_ROOTS)
    assert violations(hits, allowlist) == ["tests/fixtures/plansx/other.json"]


def test_control_scope_is_tracked_files_under_the_scan_roots(scratch_repo: Path) -> None:
    _plant(scratch_repo, "src/untracked.py", "temperature\n", track=False)
    _plant(scratch_repo, "docs/history.md", "temperature\n")
    assert files_containing(scratch_repo, (REMOVED_TOKEN,), SCAN_ROOTS) == set()


@pytest.mark.parametrize(
    ("entry", "plant"),
    [
        pytest.param("src/legacy.py", None, id="entry-names-no-tracked-file"),
        pytest.param("src/legacy.py", "EFFORT = 'low'\n", id="file-no-longer-names-the-token"),
        pytest.param("tests/fixtures/plans/", None, id="directory-entry-covers-nothing"),
    ],
)
def test_control_a_stale_allowlist_entry_is_reported(
    scratch_repo: Path, entry: str, plant: str | None
) -> None:
    if plant is not None:
        _plant(scratch_repo, entry, plant)
    allowlist = {entry: Category.LEGACY_EVAL_HISTORY}
    hits = files_containing(scratch_repo, (REMOVED_TOKEN,), SCAN_ROOTS)
    assert stale_entries(hits, tracked_files(scratch_repo, SCAN_ROOTS), allowlist) == [entry]


def test_control_a_git_failure_raises_instead_of_reporting_no_hit(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git grep failed"):
        files_containing(tmp_path / "not-a-repo", (REMOVED_TOKEN,), SCAN_ROOTS)


def test_control_a_git_failure_raises_instead_of_reporting_no_occurrence(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git grep failed"):
        occurrence_counts(tmp_path / "not-a-repo", REMOVED_TOKEN, SCAN_ROOTS)


def test_control_occurrences_are_counted_per_match_in_any_case(scratch_repo: Path) -> None:
    _plant(scratch_repo, "src/tuning.py", "temperature = Temperature\nTEMPERATURE\n")
    assert occurrence_counts(scratch_repo, REMOVED_TOKEN, SCAN_ROOTS) == {"src/tuning.py": 3}


def test_control_a_count_at_the_pin_is_not_a_mismatch(scratch_repo: Path) -> None:
    _plant(scratch_repo, "src/tuning.py", "temperature\ntemperature\n")
    counts = occurrence_counts(scratch_repo, REMOVED_TOKEN, SCAN_ROOTS)
    assert pin_mismatches(counts, {"src/tuning.py": 2}) == []


def test_control_one_extra_occurrence_in_a_pinned_file_names_the_file_and_count(
    scratch_repo: Path,
) -> None:
    _plant(scratch_repo, "src/tuning.py", "temperature\ntemperature\ntemperature\n")
    counts = occurrence_counts(scratch_repo, REMOVED_TOKEN, SCAN_ROOTS)
    assert pin_mismatches(counts, {"src/tuning.py": 2}) == [
        "src/tuning.py: 3 occurrences, pinned 2"
    ]


def test_control_a_directory_pin_totals_its_subtree_only(scratch_repo: Path) -> None:
    _plant(scratch_repo, "tests/fixtures/plans/a.json", "temperature\n")
    _plant(scratch_repo, "tests/fixtures/plans/b.json", "temperature temperature\n")
    _plant(scratch_repo, "tests/fixtures/plansx/c.json", "temperature\n")
    counts = occurrence_counts(scratch_repo, REMOVED_TOKEN, SCAN_ROOTS)
    assert pin_mismatches(counts, {"tests/fixtures/plans/": 3}) == []
    assert pin_mismatches(counts, {"tests/fixtures/plans/": 2}) == [
        "tests/fixtures/plans/: 3 occurrences, pinned 2"
    ]
