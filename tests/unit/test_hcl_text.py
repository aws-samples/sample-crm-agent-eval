# SPDX-License-Identifier: MIT
"""Tests for `scripts/hcl_text.py` — the length-preserving HCL text transforms.

Each test asserts one of two properties: braces, `#`, `//`, `/* */` and quotes inside literals or
comments stop looking like syntax; and every transform preserves length and newline positions
(`same_shape`), so a caller can slice and report line numbers from the original.
"""

from __future__ import annotations

import itertools
import re

from scripts.hcl_text import (
    blank_comments,
    blank_heredoc_bodies,
    blank_literals,
    blank_string_literals,
    same_shape,
)

_WITH_HEREDOC = """resource "x" "y" {
  note = <<-EOT
    an unbalanced brace {
    # not a comment
    // also not a comment
    a "quote that never closes
  EOT
  real = "value"
}
"""


def _assert_shape(original: str, transformed: str) -> None:
    assert same_shape(original, transformed), "length or newline positions changed"


def test_heredoc_body_braces_are_blanked() -> None:
    """A brace inside a heredoc body is blanked, leaving only structural braces."""
    result = blank_heredoc_bodies(_WITH_HEREDOC)
    _assert_shape(_WITH_HEREDOC, result)
    assert result.count("{") == 1, "only the block's own opening brace should survive"
    assert result.count("}") == 1


def test_heredoc_body_comment_markers_are_blanked() -> None:
    """`#` and `//` inside a heredoc body are blanked."""
    result = blank_heredoc_bodies(_WITH_HEREDOC)
    assert "#" not in result
    assert "//" not in result


def test_heredoc_body_unbalanced_quote_is_blanked() -> None:
    """An unterminated quote inside a heredoc body is blanked."""
    result = blank_heredoc_bodies(_WITH_HEREDOC)
    assert result.count('"') == _WITH_HEREDOC.count('"') - 1


def test_the_opener_and_terminator_lines_survive() -> None:
    """The heredoc opener and terminator lines, and content after the heredoc, are kept verbatim."""
    result = blank_heredoc_bodies(_WITH_HEREDOC)
    assert "<<-EOT" in result
    assert "EOT" in result
    assert 'real = "value"' in result, "content after the heredoc must be untouched"


def test_a_mentioned_heredoc_inside_a_string_is_not_treated_as_an_opener() -> None:
    """A `<<-EOT` mentioned inside a quoted string opens no heredoc."""
    source = 'resource "x" "y" {\n  description = "pass it with <<-EOT"\n  brace = "{"\n}\n'
    result = blank_heredoc_bodies(source)
    _assert_shape(source, result)
    assert result == source


def test_two_heredocs_in_one_file_are_both_blanked() -> None:
    source = (
        'resource "a" "b" {\n  x = <<-EOT\n    brace {\n  EOT\n}\n'
        'resource "c" "d" {\n  y = <<EOF\n    brace {\n  EOF\n}\n'
    )
    result = blank_heredoc_bodies(source)
    _assert_shape(source, result)
    assert result.count("{") == 2, "only the two block-opening braces should remain"


def test_an_unterminated_heredoc_blanks_to_end_of_file_rather_than_half_parsing() -> None:
    """A heredoc with no terminator is blanked to end of file."""
    source = 'resource "x" "y" {\n  note = <<-EOT\n    a brace {\n'
    result = blank_heredoc_bodies(source)
    _assert_shape(source, result)
    assert result.count("{") == 1


def test_text_with_no_heredoc_is_returned_unchanged() -> None:
    """Text with no heredoc is returned byte-for-byte unchanged."""
    source = 'resource "x" "y" {\n  a = "b"\n}\n'
    assert blank_heredoc_bodies(source) == source


def test_string_literal_contents_are_blanked_but_delimiters_survive() -> None:
    source = 'a = "brace { inside"\nb = "plain"\n'
    result = blank_string_literals(source)
    _assert_shape(source, result)
    assert "{" not in result
    assert result.count('"') == 4


def test_escaped_quote_does_not_end_a_string_literal() -> None:
    r"""`"he said \"hi\" and {"` is one string containing a brace, not two strings around one."""
    source = 'a = "he said \\"hi\\" and {"\n'
    result = blank_string_literals(source)
    _assert_shape(source, result)
    assert "{" not in result


def test_same_shape_rejects_a_transform_that_changes_length() -> None:
    """`same_shape` rejects any change in length or newline position."""
    assert same_shape("ab\ncd", "xy\nzw")
    assert not same_shape("ab\ncd", "ab cd")
    assert not same_shape("ab\ncd", "abc\nd")
    assert not same_shape("ab\ncd", "ab\ncde")


_BOTH_KINDS = """resource "x" "y" {
  note = <<-EOT
    he said "hi and left the quote open, plus a brace {
  EOT
  a = "live { code"
  b = "more } code"
}
"""


def test_blank_literals_removes_braces_from_both_kinds_of_literal() -> None:
    """`blank_literals` blanks braces in both heredoc bodies and quoted strings."""
    result = blank_literals(_BOTH_KINDS)
    _assert_shape(_BOTH_KINDS, result)
    assert result.count("{") == 1, "only the block's own opening brace should remain"
    assert result.count("}") == 1


def test_blank_literals_is_order_independent() -> None:
    """Heredoc and string-literal blanking commute, since string state resets at each line boundary."""
    assert blank_string_literals(blank_heredoc_bodies(_BOTH_KINDS)) == blank_heredoc_bodies(
        blank_string_literals(_BOTH_KINDS)
    )


def test_an_unterminated_quote_cannot_blank_a_later_line() -> None:
    """An unterminated quote does not carry string state onto the next line."""
    source = 'a = "unterminated\nb = "live { code"\n'
    result = blank_string_literals(source)
    _assert_shape(source, result)
    assert 'b = "' in result, "the second line's own literal must still be delimited"


_RESOURCE_HEADER = re.compile(r'resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"\s*\{')


def test_blank_literals_erases_quoted_labels_and_blank_heredoc_bodies_does_not() -> None:
    """`blank_literals` erases a resource header's quoted labels; `blank_heredoc_bodies` keeps them."""
    source = 'resource "aws_lambda_function_url" "relay" {\n  authorization_type = "NONE"\n}\n'

    composed = blank_literals(source)
    _assert_shape(source, composed)
    assert _RESOURCE_HEADER.search(composed) is None, (
        "blank_literals no longer erases quoted labels; the warning on it is now wrong"
    )

    heredoc_only = blank_heredoc_bodies(source)
    _assert_shape(source, heredoc_only)
    match = _RESOURCE_HEADER.search(heredoc_only)
    assert match is not None, "blank_heredoc_bodies must leave a header matchable"
    assert match.groups() == ("aws_lambda_function_url", "relay")


# A stray closer in a comment, for each opener/closer pairing.
_STRAY_CLOSER_CASES = {
    ("[", "]"): 'l = [\n  "a", # note ]\n  "b",\n]\n',
    ("[", "}"): 'l = [\n  "a", # note }\n  "b",\n]\n',
    ("{", "}"): "m = {\n  a = 1 # note }\n  b = 2\n}\n",
    ("{", "]"): "m = {\n  a = 1 # note ]\n  b = 2\n}\n",
}


def _depth_balanced(text: str) -> bool:
    """Whether openers and closers balance, counting `[`/`{` and `]`/`}` interchangeably."""
    depth = 0
    for char in text:
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth == 0


def test_blank_comments_neutralises_a_hash_and_a_double_slash_comment() -> None:
    """`#` and `//` comments are blanked together with their markers."""
    source = "a = 1 # trailing ]\n// whole line }\nb = 2\n"

    result = blank_comments(source)

    _assert_shape(source, result)
    assert (
        result
        == "a = 1 " + " " * len("# trailing ]") + "\n" + " " * len("// whole line }") + "\nb = 2\n"
    )


def test_a_marker_inside_a_quoted_string_is_data_and_survives() -> None:
    """Comment markers inside quoted strings, such as `https://`, are kept."""
    source = 'value = replace(replace(url, "https://", ""), "/", "")\nother = "a # b"\n'

    result = blank_comments(source)

    _assert_shape(source, result)
    assert result == source


def test_a_marker_inside_a_heredoc_body_is_data_and_survives() -> None:
    """`blank_comments` keeps a `#` inside a heredoc body."""
    source = "p = <<EOT\n# not a comment ]\nsee https://x/y\nEOT\nq = [1]\n"

    result = blank_comments(source)

    _assert_shape(source, result)
    assert result == source


def test_a_comment_that_merely_mentions_a_heredoc_opener_starts_no_phantom_body() -> None:
    """A `<<EOT` mentioned inside a `#` comment opens no heredoc body in any transform."""
    source = "# the body is passed with <<EOT\nlive = { a = 1 }\nEOT\nafter = 1\n"

    for transform in (blank_comments, blank_heredoc_bodies, blank_literals):
        result = transform(source)
        _assert_shape(source, result)
        assert "live = { a = 1 }" in result, (
            f"{transform.__name__} blanked live code on the strength of a comment"
        )


def test_every_stray_closer_in_a_comment_is_neutralised_for_both_opener_shapes() -> None:
    """Each stray closer inside a comment is blanked, for both opener shapes."""
    for (opener, closer), source in _STRAY_CLOSER_CASES.items():
        assert not _depth_balanced(source), (
            f"the {opener}-opened fixture with a stray {closer} is already balanced, so this case "
            "asserts nothing"
        )
        result = blank_comments(source)
        _assert_shape(source, result)
        assert _depth_balanced(result), (
            f"a stray {closer} in a comment still unbalances a {opener}-opened value"
        )


def test_every_composition_order_of_all_three_transforms_agrees() -> None:
    """All six composition orders of the three transforms produce the same result."""
    cases = [
        'value = replace(replace(url, "https://", ""), "/", "")\n',
        'x = "a # b"  # real comment ]\n',
        "# the body is passed with <<EOT\nlive = { a = 1 }\nEOT\nafter = 1\n",
        "p = <<EOT\n# not a comment ]\nEOT\nq = [1]\n",
        'x = 1 # don"t\ny = "z"\n',
        "/*\n  passed as x = <<EOT\n*/\nlive = { a = 1 }\nEOT\nafter = 2\n",
        "locals {\n  /* shape: { table, actions } and [a, b] */\n  g = [1, 2]\n}\n",
        'a = ["${bucket}/index/*"]\np = <<EOT\n/* not a comment {\nEOT\nq = [1]\n',
        *_STRAY_CLOSER_CASES.values(),
    ]
    transforms = {
        "C": blank_comments,
        "H": blank_heredoc_bodies,
        "S": blank_string_literals,
    }

    for source in cases:
        outputs = set()
        for order in itertools.permutations("CHS"):
            result = source
            for key in order:
                result = transforms[key](result)
            _assert_shape(source, result)
            outputs.add(result)
        assert len(outputs) == 1, (
            f"composition order changed the result for {source!r}: {outputs!r}"
        )


def test_blank_comments_leaves_a_resource_header_matchable() -> None:
    """`blank_comments` leaves a resource header with a trailing comment matchable."""
    source = 'resource "aws_lambda_function_url" "relay" { # a trailing comment\n'

    result = blank_comments(source)

    _assert_shape(source, result)
    match = re.search(r'resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"\s*\{', result)
    assert match is not None
    assert match.groups() == ("aws_lambda_function_url", "relay")


def test_a_single_line_block_comment_is_neutralised() -> None:
    """A single-line `/* … */` comment is blanked."""
    source = "a = 1 /* a block comment ] */\nb = 2\n"

    result = blank_comments(source)

    _assert_shape(source, result)
    assert result == "a = 1 " + " " * len("/* a block comment ] */") + "\nb = 2\n"


def test_a_block_comment_spanning_lines_is_neutralised_to_its_terminator_and_no_further() -> None:
    """A multi-line block comment is blanked up to its terminator and no further."""
    source = "/*\n  shape: { table, actions } and [a, b]\n*/\nlive = { a = 1 }\n"

    result = blank_comments(source)

    _assert_shape(source, result)
    assert "table" not in result and "[a, b]" not in result, "the block's text was not neutralised"
    assert "live = { a = 1 }" in result, "the transform ran past the terminator into live code"


def test_a_block_marker_inside_a_literal_is_data_and_survives() -> None:
    """A `/*` inside a quoted string or a heredoc body is not treated as a block-comment opener."""
    source = 'a = ["${bucket}/index/*"]\np = <<EOT\n/* not a comment {\nEOT\nq = [1]\n'

    result = blank_comments(source)

    _assert_shape(source, result)
    assert result == source


def test_a_heredoc_opener_inside_a_block_comment_starts_no_phantom_body() -> None:
    """A `<<EOT` mentioned inside a block comment opens no heredoc body in any transform."""
    source = "/*\n  passed as x = <<EOT\n*/\nlive = { a = 1 }\nEOT\nafter = 2\n"

    for transform in (blank_comments, blank_heredoc_bodies, blank_literals):
        result = transform(source)
        _assert_shape(source, result)
        assert "live = { a = 1 }" in result, (
            f"{transform.__name__} opened a heredoc body from a mention inside a block comment"
        )
