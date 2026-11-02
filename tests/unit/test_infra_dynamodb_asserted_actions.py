# SPDX-License-Identifier: MIT
"""Offline guard on the completeness of `local.dynamodb_asserted_actions`.

Every role's explicit same-table Deny is `setsubtract(local.dynamodb_asserted_actions, <grant>)`, so
an action missing from the asserted set appears in no Deny. The scan reads every `dynamodb:` action
literal in the root `infra/*.tf` files, including grants written inline as module arguments, and
asserts the asserted set equals the granted set in both directions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_INFRA: Final[Path] = Path(__file__).resolve().parents[2] / "infra"

#: Any `"dynamodb:Something"` literal, excluding wildcards.
_ACTION_LITERAL: Final[re.Pattern[str]] = re.compile(r'"(dynamodb:[A-Za-z]+)"')

_WILDCARD_LITERAL: Final[re.Pattern[str]] = re.compile(r'"dynamodb:[A-Za-z]*\*[A-Za-z]*"')

#: The expression under test, up to the closing `]))` of its `flatten`.
_ASSERTED_EXPR: Final[re.Pattern[str]] = re.compile(
    r"dynamodb_asserted_actions = distinct\(flatten\(\[(?P<body>.*?)\n  \]\)\)", re.DOTALL
)


def _source() -> str:
    """Every root `.tf` file, `main.tf` first, since the expression reads locals from several."""
    paths = sorted(_INFRA.glob("*.tf"), key=lambda path: (path.name != "main.tf", path.name))
    assert paths[0].name == "main.tf", paths
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _asserted_expression(text: str) -> str:
    match = _ASSERTED_EXPR.search(text)
    assert match is not None, (
        "`local.dynamodb_asserted_actions` is not a `distinct(flatten([...]))` in the shape this file "
        "reads. If it was reshaped, port this derivation to the new shape rather than deleting it — the "
        "property it guards is not about the shape."
    )
    return match.group("body")


def _local_block(text: str, name: str) -> str:
    """The literal text of `local.<name>`'s value, brace/bracket-balanced."""
    start = re.search(rf"^  {re.escape(name)}\s*=\s*", text, re.MULTILINE)
    assert start is not None, f"local.{name} was not found in the root `.tf` files"
    rest = text[start.end() :]
    opener = rest[0]
    closer = {"[": "]", "{": "}"}.get(opener)
    assert closer is not None, f"local.{name} does not open with a list or object: {rest[:40]!r}"
    depth = 0
    for i, ch in enumerate(rest):
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return rest[: i + 1]
    raise AssertionError(f"local.{name}'s value has unbalanced {opener!r}")


def _sources_read_by_the_expression(text: str) -> list[str]:
    """The `local.<name>` values the asserted-actions expression unions."""
    names = sorted(set(re.findall(r"local\.(\w+)", _asserted_expression(text))))
    assert names, "the asserted-actions expression reads no locals at all"
    return names


def _asserted_actions(text: str) -> set[str]:
    actions: set[str] = set()
    for name in _sources_read_by_the_expression(text):
        actions |= set(_ACTION_LITERAL.findall(_local_block(text, name)))
    return actions


def _all_granted_actions(text: str) -> set[str]:
    return set(_ACTION_LITERAL.findall(text))


def test_the_asserted_set_covers_every_action_granted_anywhere_in_the_file() -> None:
    """Every dynamodb action granted anywhere in the root `.tf` files is in the asserted set."""
    text = _source()
    asserted = _asserted_actions(text)
    granted = _all_granted_actions(text)
    missing = sorted(granted - asserted)
    assert not missing, (
        f"{missing} are granted somewhere in the root `.tf` files but absent from "
        "`local.dynamodb_asserted_actions`, so `setsubtract(asserted, granted)` can never place them "
        "in any role's explicit same-table Deny. Every role lacking the action falls back to "
        "deny-by-default and `simulate-principal-policy` reports `implicitDeny only`. Add the grant's "
        "own local to the asserted-actions expression; do not add the action as a literal."
    )


def test_the_expression_hardcodes_no_action_literal_of_its_own() -> None:
    """The asserted-actions expression body names no action literal; the set is only derived.

    Checked on the expression body, since `asserted` is a subset of `granted` by construction.
    """
    body = _asserted_expression(_source())
    hardcoded = sorted(set(_ACTION_LITERAL.findall(body)))
    assert not hardcoded, (
        f"the asserted-actions expression names {hardcoded} literally instead of reading it from the "
        "grant that confers it. Add that grant's own local to the union; a literal here is true only "
        "until the grant changes underneath it."
    )


def test_the_expression_reads_more_than_the_tool_grants() -> None:
    """The expression reads `tool_dynamodb_grants` and at least one other grant local."""
    names = _sources_read_by_the_expression(_source())
    assert "tool_dynamodb_grants" in names, f"the tool grants are no longer read at all: {names}"
    assert len(names) > 1, (
        "the asserted-actions expression reads only the tool grants again. Non-tool functions "
        "(the sweep control function and the eval-runs writer) hold actions the tools do not, and an action "
        f"reachable from no source here cannot be denied anywhere. Sources read: {names}"
    )


def test_the_scan_is_not_vacuous_and_sees_no_wildcard() -> None:
    """The scan parses a non-trivial set of action literals and finds no `dynamodb:*` wildcard."""
    text = _source()
    granted = _all_granted_actions(text)
    assert len(granted) >= 5, (
        f"only {len(granted)} dynamodb action literal(s) were parsed from the root `.tf` files, which is far "
        "fewer than this file grants. The scan is broken, and a broken scan makes the assertions above "
        f"vacuous: {sorted(granted)}"
    )
    assert _asserted_actions(text), "no actions were parsed from the asserted-actions sources"
    wildcards = sorted(set(_WILDCARD_LITERAL.findall(text)))
    assert not wildcards, (
        f"{wildcards} is a wildcard dynamodb action literal. The equality assertions above treat every "
        "literal as a grant, which a wildcard is not — teach this file the distinction rather than "
        "widening the exclusion."
    )
