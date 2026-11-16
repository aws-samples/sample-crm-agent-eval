# SPDX-License-Identifier: MIT
"""Offline guard on the completeness of `local.dynamodb_asserted_actions`.

Every role's explicit same-table Deny is `setsubtract(local.dynamodb_asserted_actions, <grant>)`, so
an action missing from the asserted set appears in no Deny. The scan reads every `dynamodb:` action
literal in the root `infra/*.tf` files, including grants written inline as module arguments, and
asserts the asserted set equals the granted set in both directions. A `Deny` statement grants
nothing and a condition `variable` names a context key, not an action, so neither is read as a grant.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from scripts.hcl_text import blank_comments

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


#: A policy-document condition's context-key line, `variable = "dynamodb:..."`.
_CONDITION_VARIABLE: Final[re.Pattern[str]] = re.compile(r'^\s*variable\s*=\s*"[^"]*"\s*$', re.M)

_STATEMENT_START: Final[re.Pattern[str]] = re.compile(r"^[ \t]*statement[ \t]*\{", re.M)

#: A statement's own `effect = "Deny"` line, judged on the statement's depth-1 text only.
_DENY_EFFECT: Final[re.Pattern[str]] = re.compile(r'^\s*effect\s*=\s*"Deny"\s*$', re.M)


def _statement_blocks(text: str) -> list[tuple[int, int, str]]:
    """`(start, end, own_text)` of every `statement { ... }` block, brace-balanced.

    Braces are counted on a comment-blanked copy, so a brace in a comment is ignored. `own_text` is
    the block's depth-1 text, without its nested blocks, so an attribute belongs to this statement.
    """
    blanked = blank_comments(text)
    assert len(blanked) == len(text), "comment blanking must preserve offsets"
    blocks: list[tuple[int, int, str]] = []
    position = 0
    for match in _STATEMENT_START.finditer(blanked):
        if match.start() < position:
            continue
        opening = blanked.index("{", match.start())
        depth = 0
        own: list[str] = []
        for index in range(opening, len(blanked)):
            char = blanked[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append((match.start(), index + 1, "".join(own)))
                    position = index + 1
                    break
            elif depth == 1:
                own.append(char)
        else:
            raise AssertionError(f"unbalanced statement block at offset {match.start()}")
    return blocks


def _without_denies(text: str) -> str:
    """`text` with every `statement { ... }` block whose own effect is `Deny` removed."""
    pieces = []
    position = 0
    for start, end, own in _statement_blocks(text):
        if _DENY_EFFECT.search(own):
            pieces.append(text[position:start])
            position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _grant_text(text: str) -> str:
    """The root text that can grant: Deny statements and condition context keys removed."""
    return _CONDITION_VARIABLE.sub("", _without_denies(text))


def _all_granted_actions(text: str) -> set[str]:
    return set(_ACTION_LITERAL.findall(_grant_text(text)))


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
    wildcards = sorted(set(_WILDCARD_LITERAL.findall(_grant_text(text))))
    assert not wildcards, (
        f"{wildcards} is a wildcard dynamodb action literal. The equality assertions above treat every "
        "literal as a grant, which a wildcard is not — teach this file the distinction rather than "
        "widening the exclusion."
    )


def test_a_deny_statement_and_a_condition_key_grant_nothing() -> None:
    """Only the Allow's action is read; the Deny's wildcard and the condition key are not."""
    text = """
  statement {
    effect  = "Allow"
    actions = ["dynamodb:GetItem"]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:Attributes"
      values   = ["rep_id"]
    }
  }

  statement {
    effect    = "Deny"
    actions   = ["dynamodb:*"]
    resources = ["*"]
  }
"""
    assert _all_granted_actions(text) == {"dynamodb:GetItem"}
    assert not _WILDCARD_LITERAL.findall(_grant_text(text))


def test_a_wildcard_in_an_allow_statement_is_still_seen() -> None:
    text = """
  statement {
    effect  = "Allow"
    actions = ["dynamodb:*"]
  }
"""
    assert _WILDCARD_LITERAL.findall(_grant_text(text)) == ['"dynamodb:*"']


def test_the_real_tree_has_deny_statements_to_exclude() -> None:
    """Control: the exclusion acts on the real tree, so the wildcard check above is not vacuous."""
    text = _source()
    assert _WILDCARD_LITERAL.findall(text), "no wildcard anywhere: the Deny exclusion tests nothing"
    assert _grant_text(text) != text


_ALLOW_GET: Final[str] = """
  statement {
    effect  = "Allow"
    actions = ["dynamodb:GetItem"]
  }
"""

_ALLOW_WILDCARD: Final[str] = """
  statement {
    effect  = "Allow"
    actions = ["dynamodb:*"]
  }
"""

_DENY_WILDCARD: Final[str] = """
  statement {
    effect    = "Deny"
    actions   = ["dynamodb:*"]
    resources = ["*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
"""

#: Layouts `terraform fmt` accepts. The middle statement of each is the Allow under test.
_LAYOUTS: Final[dict[str, tuple[str, str]]] = {
    "blank_line_after_open": ("  statement {\n", "  statement {\n\n"),
    "comment_line_inside": ("  statement {\n", "  statement {\n    # a { brace in a comment\n"),
}


def _document(*statements: str) -> str:
    """The statements inside a policy-document block, as they appear in the root files."""
    return 'data "aws_iam_policy_document" "subject" {' + "".join(statements) + "}\n"


def _layout(statement: str, layout: str) -> str:
    old, new = _LAYOUTS[layout]
    assert statement.count(old) == 1
    return statement.replace(old, new)


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_an_allow_between_two_denies_is_read_in_every_layout(layout: str) -> None:
    text = _document(_DENY_WILDCARD, _layout(_ALLOW_GET, layout), _DENY_WILDCARD)
    assert _all_granted_actions(text) == {"dynamodb:GetItem"}


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_an_allow_followed_by_a_deny_is_read_in_every_layout(layout: str) -> None:
    text = _document(_layout(_ALLOW_GET, layout), _DENY_WILDCARD)
    assert _all_granted_actions(text) == {"dynamodb:GetItem"}


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_an_allow_wildcard_is_caught_in_every_layout(layout: str) -> None:
    for text in (
        _document(_DENY_WILDCARD, _layout(_ALLOW_WILDCARD, layout), _DENY_WILDCARD),
        _document(_layout(_ALLOW_WILDCARD, layout), _DENY_WILDCARD),
    ):
        assert _WILDCARD_LITERAL.findall(_grant_text(text)) == ['"dynamodb:*"'], text


def test_a_deny_effect_in_a_nested_block_does_not_make_its_statement_a_deny() -> None:
    """Only the statement's own depth-1 `effect` counts, not text inside a nested block."""
    text = _document(
        """
  statement {
    effect  = "Allow"
    actions = ["dynamodb:PutItem"]
    nested {
      effect = "Deny"
    }
  }
"""
    )
    assert _all_granted_actions(text) == {"dynamodb:PutItem"}
