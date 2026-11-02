# SPDX-License-Identifier: MIT
""":func:`eval.matrix.write_matrix_artifacts` refuses an all-stub matrix aimed at the tracked
``<repo>/results`` directory and writes every other combination.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from eval import matrix
from eval.matrix import (
    TRACKED_RESULTS_DIR,
    ArmResult,
    MatrixResult,
    StubMatrixOverwriteRefused,
    build_matrix_result,
    render_markdown,
    write_matrix_artifacts,
)
from tests.unit.test_matrix import _sample_arm

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _arm(mode: str, model_id: str = "us.anthropic.claude-opus-5-v1:0") -> ArmResult:
    arm = _sample_arm()
    return dataclasses.replace(
        arm, provenance=dataclasses.replace(arm.provenance, mode=mode, model_id=model_id)
    )


def _stub_matrix() -> MatrixResult:
    return build_matrix_result([_arm("stub"), _arm("stub", "openai.gpt-6-1-sol")])


def _live_matrix() -> MatrixResult:
    return build_matrix_result([_arm("live"), _arm("live", "openai.gpt-6-1-sol")])


@pytest.fixture
def tracked_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the guard at a scratch stand-in for ``<repo>/results`` so no test touches the repo."""
    stand_in = tmp_path / "repo" / "results"
    monkeypatch.setattr(matrix, "TRACKED_RESULTS_DIR", stand_in.resolve())
    return stand_in


def test_the_tracked_dir_is_the_repository_results_directory() -> None:
    assert (_REPO_ROOT / "results").resolve() == TRACKED_RESULTS_DIR
    assert (TRACKED_RESULTS_DIR / "matrix.md").is_file()


def test_the_refusal_is_a_runtime_error() -> None:
    assert issubclass(StubMatrixOverwriteRefused, RuntimeError)


def test_an_all_stub_matrix_into_the_tracked_dir_is_refused_and_writes_nothing(
    tracked_results: Path,
) -> None:
    with pytest.raises(StubMatrixOverwriteRefused, match="all-stub matrix"):
        write_matrix_artifacts(_stub_matrix(), tracked_results)
    assert not tracked_results.exists()


def test_an_all_stub_matrix_into_an_existing_tracked_dir_leaves_its_files_untouched(
    tracked_results: Path,
) -> None:
    tracked_results.mkdir(parents=True)
    published = tracked_results / "matrix.md"
    published.write_text("live sweep\n", encoding="utf-8")
    with pytest.raises(StubMatrixOverwriteRefused):
        write_matrix_artifacts(_stub_matrix(), tracked_results)
    assert published.read_text(encoding="utf-8") == "live sweep\n"
    assert sorted(p.name for p in tracked_results.iterdir()) == ["matrix.md"]


def test_the_refusal_matches_the_tracked_dir_through_an_unresolved_path(
    tracked_results: Path,
) -> None:
    roundabout = tracked_results.parent / "stub" / ".." / "results"
    with pytest.raises(StubMatrixOverwriteRefused):
        write_matrix_artifacts(_stub_matrix(), roundabout)


def test_a_live_matrix_is_written_into_the_tracked_dir(tracked_results: Path) -> None:
    live = _live_matrix()
    paths = write_matrix_artifacts(live, tracked_results)
    assert paths["md"] == tracked_results / "matrix.md"
    assert paths["md"].read_text(encoding="utf-8") == render_markdown(live)
    assert {path.name for path in tracked_results.iterdir()} == {
        "matrix.json",
        "matrix.md",
        "matrix.csv",
    }


def test_a_mixed_stub_and_live_matrix_is_written_into_the_tracked_dir(
    tracked_results: Path,
) -> None:
    mixed = build_matrix_result([_arm("stub"), _arm("live", "openai.gpt-6-1-sol")])
    write_matrix_artifacts(mixed, tracked_results)
    assert (tracked_results / "matrix.md").is_file()


def test_a_stub_matrix_is_written_into_tmp_path(tmp_path: Path) -> None:
    paths = write_matrix_artifacts(_stub_matrix(), tmp_path)
    assert paths["json"].is_file()


def test_a_stub_matrix_is_written_into_results_stub(tracked_results: Path) -> None:
    stub_dir = tracked_results / "stub"
    write_matrix_artifacts(_stub_matrix(), stub_dir)
    assert (stub_dir / "matrix.md").read_text(encoding="utf-8") == render_markdown(_stub_matrix())


def test_the_re_render_cli_inherits_the_refusal(
    tracked_results: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(matrix, "load_raw_archives", lambda *_args, **_kwargs: [_arm("stub")])
    with pytest.raises(StubMatrixOverwriteRefused):
        matrix.main(["--raw-dir", str(tmp_path), "--output-dir", str(tracked_results)])
    assert not tracked_results.exists()
