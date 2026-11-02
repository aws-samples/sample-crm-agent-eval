#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Length-preserving text transforms for scanning HCL without an HCL parser.

The offline Terraform gates read `*.tf` as text and find a block's extent by matching braces. These
transforms blank the text that carries braces as data — comments, heredoc bodies and quoted strings —
so a caller can match braces on the result.

Every transform preserves length and newline positions: callers match braces on the transformed text,
slice the original, and report `file:line` from offsets into it. :func:`same_shape` asserts this.

This is not an HCL parser; a gate that needs HCL semantics reads `terraform show -json` instead.
"""

from __future__ import annotations

import re

#: A heredoc opener: `<<NAME` or `<<-NAME` as the last token on its line.
_OPENER = re.compile(r"<<(-?)([A-Za-z_][A-Za-z0-9_]*)[ \t]*$")


def _in_string(line: str, index: int) -> bool:
    r"""Whether `index` sits inside a double-quoted string on `line`.

    Counts unescaped quotes, treating `\\` as an escaped backslash rather than an escape of the quote
    that follows it.
    """
    quotes = 0
    position = 0
    while position < index and position < len(line):
        char = line[position]
        if char == "\\":
            position += 2
            continue
        if char == '"':
            quotes += 1
        position += 1
    return quotes % 2 == 1


def comment_start(line: str) -> int | None:
    r"""Index of the first `#` or `//` on `line` that is a comment marker, or `None` if there is none.

    A marker inside a double-quoted string is not a comment (`"https://…"`, `"a # b"`), so this scans
    quote state left to right. `/*` is reported too; only :func:`blank_comments` tracks a `/* … */`
    block across lines.
    """
    inside = False
    position = 0
    while position < len(line):
        char = line[position]
        if char == "\\" and inside and position + 1 < len(line):
            position += 2
            continue
        if char == '"':
            inside = not inside
        elif not inside:
            if char == "#":
                return position
            if char == "/" and (line.startswith("//", position) or line.startswith("/*", position)):
                return position
        position += 1
    return None


def _opener_terminator(line: str) -> str | None:
    r"""The heredoc terminator `line` opens, or `None` if it opens none.

    An opener is ignored when it sits inside a string (`description = "pass <<-EOT"`) or inside a
    comment (`# the body is passed with <<EOT`).
    """
    match = _OPENER.search(line)
    if match is None or _in_string(line, match.start()):
        return None
    marker = comment_start(line)
    if marker is not None and marker < match.start():
        return None
    return match.group(2)


def _blank_line(line: str, in_block: bool) -> tuple[str, bool]:
    r"""Blank `line`'s comment text given an open `/* … */` state; return it and the new state."""
    rendered: list[str] = []
    inside_string = False
    position = 0
    while position < len(line):
        char = line[position]
        if in_block:
            closing = line.startswith("*/", position)
            rendered.append("  " if closing else " ")
            position += 2 if closing else 1
            in_block = not closing
            continue
        if inside_string:
            if char == "\\" and position + 1 < len(line):
                rendered.append(line[position : position + 2])
                position += 2
                continue
            if char == '"':
                inside_string = False
            rendered.append(char)
            position += 1
            continue
        if char == '"':
            inside_string = True
            rendered.append(char)
            position += 1
            continue
        if line.startswith("/*", position):
            in_block = True
            rendered.append("  ")
            position += 2
            continue
        if char == "#" or line.startswith("//", position):
            rendered.append(" " * (len(line) - position))
            return "".join(rendered), False
        rendered.append(char)
        position += 1
    return "".join(rendered), in_block


def blank_comments(text: str) -> str:
    r"""Replace `#`, `//` and `/* … */` comment text with spaces, preserving length and line structure.

    The marker is blanked along with the text after it. A marker inside a quoted string or inside a
    heredoc body is data and is left alone, so `/*` in an ARN wildcard such as `bucket/*` does not open
    a block. Composition with :func:`blank_heredoc_bodies` and :func:`blank_string_literals` is
    order-independent.
    """
    out: list[str] = []
    terminator: str | None = None
    in_block = False
    for line in text.split("\n"):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            out.append(line)
            continue
        if not in_block:
            terminator = _opener_terminator(line)
        blanked, in_block = _blank_line(line, in_block)
        out.append(blanked)
    return "\n".join(out)


def blank_heredoc_bodies(text: str) -> str:
    r"""Replace every heredoc **body** with spaces, preserving length and line structure.

    The opener and terminator lines are kept verbatim. Quoted block labels are left intact, so a parser
    that keys on a quoted label uses this transform rather than :func:`blank_literals`.
    """
    lines = text.split("\n")
    out: list[str] = []
    terminator: str | None = None
    in_block = False
    for line in lines:
        if terminator is None:
            if not in_block:
                terminator = _opener_terminator(line)
            _, in_block = _blank_line(line, in_block)
            out.append(line)
            continue
        if line.strip() == terminator:
            terminator = None
            out.append(line)
            continue
        out.append(" " * len(line))
    return "\n".join(out)


def blank_string_literals(text: str) -> str:
    r"""Replace the *contents* of double-quoted strings with spaces, preserving length.

    The quotes themselves are kept. String state resets at every line boundary, since a literal newline
    inside `"` is not valid HCL.
    """
    out: list[str] = []
    for line in text.split("\n"):
        rendered: list[str] = []
        inside = False
        position = 0
        while position < len(line):
            char = line[position]
            if char == "\\" and inside and position + 1 < len(line):
                rendered.append("  ")
                position += 2
                continue
            if char == '"':
                inside = not inside
                rendered.append('"')
            else:
                rendered.append(" " if inside else char)
            position += 1
        out.append("".join(rendered))
    return "\n".join(out)


def blank_literals(text: str) -> str:
    r"""Blank heredoc bodies **and** quoted-string contents: everything that carries braces as data.

    Order-independent: composing the two halves the other way round gives identical output.

    This also erases quoted block labels: `resource "aws_lambda_function_url" "relay" {` keeps only its
    quotes. Use it only where the matched identifiers are unquoted, or where values are read from the
    original text rather than the blanked copy.
    """
    return blank_string_literals(blank_heredoc_bodies(text))


def same_shape(original: str, transformed: str) -> bool:
    r"""Whether `transformed` preserved `original`'s length and every newline position.

    Offsets into the transformed text then address the same characters in the original.
    """
    return len(original) == len(transformed) and [
        index for index, char in enumerate(original) if char == "\n"
    ] == [index for index, char in enumerate(transformed) if char == "\n"]
