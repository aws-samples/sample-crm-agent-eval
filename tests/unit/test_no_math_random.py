# SPDX-License-Identifier: MIT
"""No tracked file under ``frontend/src`` or ``tests/e2e/browser`` calls ``Math.random``.

Tokens there must come from ``crypto.getRandomValues``. A mention that is not a call (a
``vi.spyOn(Math, "random")`` spy, a test title) is allowed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from scripts import proc_run

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCOPE: Final = ("frontend/src", "tests/e2e/browser")
MATH_RANDOM_CALL: Final = re.compile(
    r"\bMath\s*\.\s*random\s*\(|\bMath\s*\[\s*[\"']random[\"']\s*\]\s*\("
)


def math_random_calls(root: Path, relpaths: Iterable[str]) -> list[str]:
    """Return ``"path:line"`` for every ``Math.random`` call in the given files under ``root``."""
    hits: list[str] = []
    for relpath in relpaths:
        text = (root / relpath).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if MATH_RANDOM_CALL.search(line):
                hits.append(f"{relpath}:{lineno}")
    return hits


def tracked_files_in_scope() -> list[str]:
    """Tracked files under the scope directories; a git failure raises."""
    result = proc_run.run(
        ["git", "ls-files", "-z", "--", *SCOPE],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        timeout=60,
    )
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def test_tracked_tree_has_no_math_random_call() -> None:
    assert math_random_calls(REPO_ROOT, tracked_files_in_scope()) == []


def test_scope_is_not_vacuous() -> None:
    tracked = tracked_files_in_scope()
    assert "frontend/src/hooks/useAgentStream.ts" in tracked
    assert "tests/e2e/browser/fixtures/pageApi.ts" in tracked


def test_planted_calls_are_reported_with_path_and_line(tmp_path: Path) -> None:
    (tmp_path / "a.ts").write_text("const ok = 1;\nconst x = Math.random();\n", encoding="utf-8")
    (tmp_path / "b.tsx").write_text(
        "export const C = () => <i>{Math . random ()}</i>;\n", encoding="utf-8"
    )
    (tmp_path / "c.ts").write_text('\n\nconst y = Math["random"]();\n', encoding="utf-8")
    (tmp_path / "d.ts").write_text("const z = Math[ 'random' ] ();\n", encoding="utf-8")

    hits = math_random_calls(tmp_path, ["a.ts", "b.tsx", "c.ts", "d.ts"])

    assert hits == ["a.ts:2", "b.tsx:1", "c.ts:3", "d.ts:1"]


def test_mentions_that_are_not_calls_are_not_reported(tmp_path: Path) -> None:
    (tmp_path / "spy.test.ts").write_text(
        'const randomSpy = vi.spyOn(Math, "random");\n'
        'it("uses crypto.getRandomValues, not Math.random", () => {});\n'
        "expect(randomSpy).not.toHaveBeenCalled();\n"
        "const r = myMath.randomize;\n",
        encoding="utf-8",
    )

    assert math_random_calls(tmp_path, ["spy.test.ts"]) == []
