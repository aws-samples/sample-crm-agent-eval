# SPDX-License-Identifier: MIT
"""Assert the browser journey's cancellation token matches the one the product emits.

The Python constant :data:`agents.supervisor.CANCELLATION_TEXT_PREFIX` is authoritative;
``journeys/07-panel-cancel.spec.ts`` mirrors it as ``CANCELLATION_PREFIX``. Failing to locate either
side is a failure, never a skip.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: The journeys tree searched for the journey-side declaration.
_JOURNEYS_DIR: Final[Path] = _REPO_ROOT / "tests" / "e2e" / "browser" / "journeys"

#: The journey-side constant's name.
_JOURNEY_CONST_NAME: Final[str] = "CANCELLATION_PREFIX"

#: The journey-side declaration pattern; it tolerates a type annotation and never matches a use.
_JOURNEY_DECLARATION: Final[re.Pattern[str]] = re.compile(
    rf"\b{_JOURNEY_CONST_NAME}\b[^=\n]*=\s*(?P<quote>[\"'`])(?P<value>[^\"'`]*)(?P=quote)"
)


class TokenNotFoundError(Exception):
    """Raised when a side of the contract cannot be read, which is a failure and never a skip."""


def python_cancellation_prefix() -> str:
    """Return :data:`agents.supervisor.CANCELLATION_TEXT_PREFIX`, the authoritative token.

    Raises:
        TokenNotFoundError: The module or the constant cannot be imported, or the token is empty.
    """
    try:
        from agents.supervisor import CANCELLATION_TEXT_PREFIX
    except Exception as exc:  # noqa: BLE001 - any import failure means nothing was verified
        raise TokenNotFoundError(
            f"cannot import agents.supervisor.CANCELLATION_TEXT_PREFIX: {exc}. The token may have "
            "been renamed or moved; journeys/07-panel-cancel.spec.ts hand-copies it and nothing "
            "else compares the two."
        ) from exc
    token = str(CANCELLATION_TEXT_PREFIX)
    if not token.strip():
        raise TokenNotFoundError("agents.supervisor.CANCELLATION_TEXT_PREFIX is empty")
    return token


def journey_cancellation_prefix(journeys_dir: Path | None = None) -> str:
    """Return the token declared as ``CANCELLATION_PREFIX`` somewhere under the journeys tree.

    Args:
        journeys_dir: The tree to search. Defaults to :data:`_JOURNEYS_DIR`.

    Raises:
        TokenNotFoundError: The tree is missing or holds no TypeScript sources, no source or more
            than one source declares the constant, or the declared value is empty.
    """
    target = _JOURNEYS_DIR if journeys_dir is None else journeys_dir
    if not target.is_dir():
        raise TokenNotFoundError(f"journeys tree not found at {target}")

    candidates = sorted(target.rglob("*.ts"))
    if not candidates:
        raise TokenNotFoundError(f"no TypeScript sources under {target}")

    found: list[tuple[Path, str]] = []
    for candidate in candidates:
        match = _JOURNEY_DECLARATION.search(candidate.read_text(encoding="utf-8"))
        if match is not None:
            found.append((candidate, match.group("value")))

    if not found:
        searched = ", ".join(path.name for path in candidates)
        raise TokenNotFoundError(
            f"no journey declares {_JOURNEY_CONST_NAME} (renamed, moved, or reshaped past the "
            f"pattern); searched: {searched}"
        )
    if len(found) > 1:
        where = ", ".join(path.name for path, _ in found)
        raise TokenNotFoundError(
            f"{_JOURNEY_CONST_NAME} is declared in more than one journey ({where}); one is a stale "
            "copy, and which one journey 07 compares against would be a guess"
        )

    source, value = found[0]
    if not value.strip():
        raise TokenNotFoundError(f"{_JOURNEY_CONST_NAME} in {source.name} declares an empty token")
    return value


def _journey_source(token: str, *, name: str = _JOURNEY_CONST_NAME, annotated: bool = False) -> str:
    """Render a stand-in journey module declaring `name` as `token`."""
    annotation = ": string" if annotated else ""
    return (
        "/** Stand-in journey for the parity test's own coverage. */\n"
        'import { expect, test } from "../fixtures/auth";\n\n'
        f'const {name}{annotation} = "{token}";\n\n'
        "test('stands in', async ({ page }) => {\n"
        f"  await expect(page.getByTestId('x')).toContainText({name});\n"
        "});\n"
    )


def _tree(tmp_path: Path, *sources: tuple[str, str]) -> Path:
    """Plant `(filename, source)` pairs as a stand-in journeys *tree*, and return it."""
    tree = tmp_path / "journeys"
    tree.mkdir(exist_ok=True)
    for filename, source in sources:
        (tree / filename).write_text(source, encoding="utf-8")
    return tree


# --------------------------------------------------------------------------------------------------
# The contract itself
# --------------------------------------------------------------------------------------------------


def test_the_journey_token_matches_the_token_the_product_emits() -> None:
    """The journey token equals the token the supervisor emits."""
    authoritative = python_cancellation_prefix()
    mirrored = journey_cancellation_prefix()
    assert mirrored == authoritative, (
        f"the browser journey compares against {mirrored!r} but the supervisor emits "
        f"{authoritative!r}, so journey 07 would never recognise a cancelled turn. The Python side is "
        "authoritative (it is what the Runtime emits): update CANCELLATION_PREFIX in "
        "tests/e2e/browser/journeys/07-panel-cancel.spec.ts, not agents.supervisor."
    )


def test_both_sides_declare_a_non_empty_token() -> None:
    """Both the product and the journey declare a non-empty token."""
    assert python_cancellation_prefix().strip()
    assert journey_cancellation_prefix().strip()


def test_the_token_is_a_fixed_marker_rather_than_a_sentence() -> None:
    """The product token contains no whitespace."""
    token = python_cancellation_prefix()
    assert " " not in token.strip(), (
        f"the cancellation marker {token!r} now contains whitespace. It is a fixed token so "
        "consumers can branch on it deterministically; a prose marker cannot be mirrored safely."
    )


# --------------------------------------------------------------------------------------------------
# The check's own failure modes
# --------------------------------------------------------------------------------------------------


def test_a_differing_journey_token_is_detected(tmp_path: Path) -> None:
    """A journey token that differs from the product token is read as differing."""
    tree = _tree(tmp_path, ("07-panel-cancel.spec.ts", _journey_source("[canceled]")))
    assert journey_cancellation_prefix(tree) == "[canceled]"
    assert journey_cancellation_prefix(tree) != python_cancellation_prefix(), (
        "the fixture's token must differ from the product's, or this test proves nothing about "
        "detection"
    )


def test_a_rename_on_the_authoritative_side_fails_rather_than_passing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing supervisor constant raises ``TokenNotFoundError``."""
    import agents.supervisor

    monkeypatch.delattr(agents.supervisor, "CANCELLATION_TEXT_PREFIX")
    with pytest.raises(TokenNotFoundError, match="cannot import"):
        python_cancellation_prefix()


def test_an_empty_authoritative_token_fails_rather_than_passing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty supervisor token raises ``TokenNotFoundError``."""
    import agents.supervisor

    monkeypatch.setattr(agents.supervisor, "CANCELLATION_TEXT_PREFIX", "")
    with pytest.raises(TokenNotFoundError, match="is empty"):
        python_cancellation_prefix()


def test_a_renamed_declaration_fails_rather_than_passing(tmp_path: Path) -> None:
    """A renamed journey declaration raises ``TokenNotFoundError``."""
    tree = _tree(
        tmp_path,
        ("07-panel-cancel.spec.ts", _journey_source("[cancelled]", name="CANCEL_MARKER")),
    )
    with pytest.raises(TokenNotFoundError, match="no journey declares"):
        journey_cancellation_prefix(tree)


def test_an_absent_declaration_fails_rather_than_passing(tmp_path: Path) -> None:
    """A journeys tree with sources but no declaration raises ``TokenNotFoundError``."""
    tree = _tree(tmp_path, ("09-unrelated.spec.ts", 'const OTHER = "x";\n'))
    with pytest.raises(TokenNotFoundError, match="no journey declares"):
        journey_cancellation_prefix(tree)


def test_an_empty_tree_fails_rather_than_passing(tmp_path: Path) -> None:
    """A journeys tree with no TypeScript sources raises ``TokenNotFoundError``."""
    tree = _tree(tmp_path)
    with pytest.raises(TokenNotFoundError, match="no TypeScript sources"):
        journey_cancellation_prefix(tree)


def test_a_missing_tree_fails_rather_than_passing(tmp_path: Path) -> None:
    """A missing journeys tree raises ``TokenNotFoundError``."""
    with pytest.raises(TokenNotFoundError, match="journeys tree not found"):
        journey_cancellation_prefix(tmp_path / "does-not-exist")


def test_two_declarations_are_refused_as_a_stale_copy(tmp_path: Path) -> None:
    """Two journey declarations raise ``TokenNotFoundError``."""
    tree = _tree(
        tmp_path,
        ("07-panel-cancel.spec.ts", _journey_source("[cancelled]")),
        ("10-other.spec.ts", _journey_source("[canceled]")),
    )
    with pytest.raises(TokenNotFoundError, match="more than one journey"):
        journey_cancellation_prefix(tree)


def test_an_empty_declared_token_fails_rather_than_passing(tmp_path: Path) -> None:
    """An empty declared journey token raises ``TokenNotFoundError``."""
    tree = _tree(tmp_path, ("07-panel-cancel.spec.ts", _journey_source("")))
    with pytest.raises(TokenNotFoundError, match="empty token"):
        journey_cancellation_prefix(tree)


def test_a_type_annotation_is_not_mistaken_for_the_value(tmp_path: Path) -> None:
    """An annotated declaration yields its value, not the annotation."""
    tree = _tree(
        tmp_path,
        ("07-panel-cancel.spec.ts", _journey_source("[cancelled]", annotated=True)),
    )
    assert journey_cancellation_prefix(tree) == "[cancelled]"


def test_a_use_of_the_constant_is_not_read_as_a_declaration(tmp_path: Path) -> None:
    """Uses of the constant are not counted as declarations."""
    source = _journey_source("[cancelled]") + (
        "\ntest('uses it again', async ({ page }) => {\n"
        "  expect(await page.innerText('body')).toContain(CANCELLATION_PREFIX);\n"
        "});\n"
    )
    tree = _tree(tmp_path, ("07-panel-cancel.spec.ts", source))
    assert journey_cancellation_prefix(tree) == "[cancelled]"


def test_this_file_can_never_skip() -> None:
    """This file's source after the module docstring contains no skip or xfail call."""
    source = Path(__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    # Assembled from fragments so the denylist does not match this file's own text.
    prefix = "pytest."
    banned_names = [prefix + name for name in ("skip", "importorskip", "xfail")]
    banned_names.append("skip" + "if")
    for banned in banned_names:
        assert banned not in body, (
            f"{banned} appears in this file. Failing to locate either side of the cancellation-token "
            "contract must be a failure: a parity check that skips reports green over an unverified "
            "contract."
        )
