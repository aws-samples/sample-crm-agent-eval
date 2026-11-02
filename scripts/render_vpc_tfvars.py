#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Write the six adopted-VPC keys of ``infra/terraform.tfvars`` from the bootstrap root's outputs.

Usage:
    terraform -chdir=infra/bootstrap_account output -json | scripts/render_vpc_tfvars.py \
        --outputs-json - --tfvars infra/terraform.tfvars --example infra/terraform.tfvars.example

``--outputs-json`` reads a ``terraform output -json`` document (``-`` for stdin). Each key in
:data:`OUTPUT_TO_TFVARS` has its existing assignment (scalar or list, indented or not, a list's ``[``
on the same line or a later one) rewritten in place, or appended if absent; a key assigned more than
once is refused. Every other line is left byte-identical. When ``--tfvars`` does not exist it is
first copied from ``--example``. The file is written atomically with mode 0600.

``--check`` writes nothing and exits 1 if a write would change the file.

Exit codes: 0 written or already current, 1 ``--check`` found a difference, 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

#: Bootstrap-root output name -> ``infra/terraform.tfvars`` key.
OUTPUT_TO_TFVARS: Final[dict[str, str]] = {
    "vpc_id": "vpc_id",
    "private_subnet_ids": "vpc_private_subnet_ids",
    "private_route_table_ids": "vpc_private_route_table_ids",
    "nat_public_subnet_id": "vpc_nat_public_subnet_id",
    "runtime_subnet_ids": "vpc_runtime_subnet_ids",
    "runtime_route_table_ids": "vpc_runtime_route_table_ids",
}

#: Output name -> (id prefix, whether the value is a list).
OUTPUT_SHAPE: Final[dict[str, tuple[str, bool]]] = {
    "vpc_id": ("vpc", False),
    "private_subnet_ids": ("subnet", True),
    "private_route_table_ids": ("rtb", True),
    "nat_public_subnet_id": ("subnet", False),
    "runtime_subnet_ids": ("subnet", True),
    "runtime_route_table_ids": ("rtb", True),
}

_ID_PATTERN: Final = re.compile(r"^(vpc|subnet|rtb)-[0-9a-f]{8}([0-9a-f]{9})?$")
_PLACEHOLDER: Final = "REPLACE_ME"
_LEADING_WHITESPACE: Final = re.compile(r"\s*")

Value = str | list[str]


class RenderError(Exception):
    """Bad input: the outputs document or the tfvars file cannot be rendered safely."""


def _validate_id(output: str, value: object, prefix: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise RenderError(f"terraform output {output!r} carries {value!r}, which is not an AWS id")
    if not value.startswith(f"{prefix}-"):
        raise RenderError(f"terraform output {output!r} carries {value!r}; expected a {prefix}- id")
    return value


def extract_values(outputs: dict[str, Any]) -> dict[str, Value]:
    """Return ``{tfvars key: value}`` for the six mapped outputs, validating each.

    Raises :class:`RenderError` on an absent or sensitive output, a type mismatch, an empty or
    duplicated list, or a value that is not an id of the expected kind.
    """
    values: dict[str, Value] = {}
    for output, key in OUTPUT_TO_TFVARS.items():
        entry = outputs.get(output)
        if not isinstance(entry, dict) or "value" not in entry:
            raise RenderError(f"terraform output {output!r} is absent; present: {sorted(outputs)}")
        if entry.get("sensitive", False) is not False:
            raise RenderError(
                f"terraform output {output!r} is marked sensitive; expected a plain id"
            )
        prefix, is_list = OUTPUT_SHAPE[output]
        raw = entry["value"]
        if is_list:
            if not isinstance(raw, list) or not raw:
                raise RenderError(
                    f"terraform output {output!r} is {raw!r}; expected a non-empty list"
                )
            ids = [_validate_id(output, item, prefix) for item in raw]
            if len(set(ids)) != len(ids):
                raise RenderError(f"terraform output {output!r} repeats an id: {ids}")
            values[key] = ids
        else:
            values[key] = _validate_id(output, raw, prefix)
    return values


def render_assignment(key: str, value: Value) -> str:
    """Render one assignment, without a trailing newline."""
    if isinstance(value, str):
        return f'{key} = "{value}"'
    items = "".join(f'  "{item}",\n' for item in value)
    return f"{key} = [\n{items}]"


def _assignment_span(text: str, key: str) -> tuple[int, int] | None:
    """Return the ``[start, end)`` span of ``key``'s assignment, excluding its trailing newline.

    The span starts at ``key``, so leading indentation is kept. A list may open on a later line.

    Raises:
        RenderError: ``key`` is assigned more than once, or its list has no closing ``]``.
    """
    matches = list(re.finditer(rf"^[ \t]*({re.escape(key)})[ \t]*=[ \t]*", text, re.MULTILINE))
    if not matches:
        return None
    if len(matches) > 1:
        raise RenderError(f"{key} is assigned {len(matches)} times; refusing to guess which one")
    start, cursor = matches[0].start(1), matches[0].end()
    list_open = _LEADING_WHITESPACE.match(text, cursor)
    if list_open is not None and text.startswith("[", list_open.end()):
        in_string = False
        position = list_open.end() + 1
        while position < len(text):
            char = text[position]
            if in_string:
                if char == "\\":
                    position += 1
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "#":
                newline = text.find("\n", position)
                position = len(text) if newline == -1 else newline
                continue
            elif char == "]":
                return start, position + 1
            position += 1
        raise RenderError(f"{key}'s list assignment has no closing ']'")
    newline = text.find("\n", cursor)
    return start, len(text) if newline == -1 else newline


def render_text(text: str, values: dict[str, Value]) -> str:
    """Return ``text`` with each key's assignment replaced by its rendered value, or appended."""
    for key, value in values.items():
        assignment = render_assignment(key, value)
        span = _assignment_span(text, key)
        if span is None:
            if text and not text.endswith("\n"):
                text += "\n"
            text += assignment + "\n"
        else:
            text = text[: span[0]] + assignment + text[span[1] :]
    return text


def refuse_placeholders(text: str) -> None:
    """Raise :class:`RenderError` if any mapped key's assignment still carries a placeholder."""
    for key in OUTPUT_TO_TFVARS.values():
        span = _assignment_span(text, key)
        if span is None or _PLACEHOLDER in text[span[0] : span[1]]:
            raise RenderError(f"{key} is unset or still carries {_PLACEHOLDER} after rendering")


def _write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` via a same-directory temp file (``mkstemp`` creates it 0600)."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).replace(path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _load_outputs(source: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        outputs = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"could not read terraform outputs from {source!r}: {exc}") from exc
    if not isinstance(outputs, dict):
        raise RenderError(f"expected a JSON object of outputs, got {type(outputs).__name__}")
    return outputs


def _load_base(tfvars: Path, example: Path | None) -> tuple[str, bool]:
    """Return the current tfvars text and whether it already exists on disk."""
    source = tfvars if tfvars.exists() else example
    if source is None:
        raise RenderError(f"{tfvars} does not exist and no --example was given")
    try:
        return source.read_text(encoding="utf-8"), source == tfvars
    except OSError as exc:
        raise RenderError(f"could not read {source}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--outputs-json", required=True, help="terraform output -json file, or -")
    parser.add_argument("--tfvars", required=True, type=Path, help="tfvars file to rewrite")
    parser.add_argument("--example", type=Path, help="template copied when --tfvars is absent")
    parser.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 on a difference"
    )
    args = parser.parse_args(argv)

    try:
        values = extract_values(_load_outputs(args.outputs_json))
        current, exists = _load_base(args.tfvars, args.example)
        rendered = render_text(current, values)
        refuse_placeholders(rendered)
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if exists and rendered == current:
        print(f"{args.tfvars} is current")
        return 0
    if args.check:
        print(f"{args.tfvars} would change", file=sys.stderr)
        return 1
    try:
        _write_atomically(args.tfvars, rendered)
    except OSError as exc:
        print(f"error: could not write {args.tfvars}: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.tfvars} ({len(values)} VPC keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
