#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render account-specific values into the AgentCore config template, and restore it.

The template contains `{{PLACEHOLDER}}`-style tokens. `render` backs up the template, then writes
a substituted copy to the output path (default: the template path itself); `restore` puts the
backed-up template bytes back. The module makes no AWS call: the caller passes every substitution
value in.

CLI usage:
    render_agentcore_env.py render --template PATH [--output PATH] [--backup PATH] \\
        --values-json '{"ACCOUNT_ID": "..."}'
    render_agentcore_env.py restore --template PATH [--backup PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

TOKEN_PATTERN = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


def render(template_path: Path, output_path: Path, values: dict[str, str]) -> set[str]:
    """Substitute every `{{TOKEN}}` in `template_path` with `values[TOKEN]`, writing to `output_path`.

    Returns the set of token names found in the template but absent from `values`, which are left
    unsubstituted in the output.
    """
    text = template_path.read_text(encoding="utf-8")
    missing: set[str] = set()

    def _substitute(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in values:
            missing.add(token)
            return match.group(0)
        return values[token]

    rendered = TOKEN_PATTERN.sub(_substitute, text)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return missing


def backup(template_path: Path, backup_path: Path) -> None:
    """Copy the current (pre-render) template bytes to `backup_path`.

    Raises `FileExistsError` if a backup already exists, since the template on disk may then
    already be rendered.
    """
    if backup_path.exists():
        raise FileExistsError(
            f"backup already exists at {backup_path}; a previous run did not restore "
            f"{template_path}. Restore first "
            f"(render_agentcore_env.py restore --template {template_path} "
            f"--backup {backup_path}), then re-run."
        )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, backup_path)


def restore(template_path: Path, backup_path: Path) -> None:
    """Overwrite `template_path` with the bytes saved at `backup_path`.

    No check is made that a render happened. Raises `FileNotFoundError` if no backup exists.
    """
    if not backup_path.exists():
        raise FileNotFoundError(f"no backup found at {backup_path}; cannot restore {template_path}")
    shutil.copyfile(backup_path, template_path)


def _cmd_render(args: argparse.Namespace) -> int:
    template_path = Path(args.template)
    output_path = Path(args.output) if args.output else template_path
    backup_path = (
        Path(args.backup)
        if args.backup
        else template_path.with_suffix(template_path.suffix + ".orig")
    )

    if not template_path.exists():
        print(f"render_agentcore_env: template not found: {template_path}", file=sys.stderr)
        return 2

    try:
        values = json.loads(args.values_json) if args.values_json else {}
    except json.JSONDecodeError as exc:
        print(f"render_agentcore_env: invalid --values-json: {exc}", file=sys.stderr)
        return 2

    if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
        print(
            "render_agentcore_env: --values-json must be a flat JSON object of string values",
            file=sys.stderr,
        )
        return 2

    try:
        backup(template_path, backup_path)
    except FileExistsError as exc:
        print(f"render_agentcore_env: {exc}", file=sys.stderr)
        return 2

    missing = render(template_path, output_path, values)
    if missing:
        print(
            f"render_agentcore_env: WARNING — unsubstituted tokens remain: {sorted(missing)}",
            file=sys.stderr,
        )

    print(
        f"render_agentcore_env: rendered {template_path} -> {output_path} (backup: {backup_path})"
    )
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    template_path = Path(args.template)
    backup_path = (
        Path(args.backup)
        if args.backup
        else template_path.with_suffix(template_path.suffix + ".orig")
    )
    try:
        restore(template_path, backup_path)
    except FileNotFoundError as exc:
        print(f"render_agentcore_env: {exc}", file=sys.stderr)
        return 1
    print(f"render_agentcore_env: restored {template_path} from {backup_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    sub = parser.add_subparsers(dest="command", required=True)

    render_parser = sub.add_parser(
        "render", help="Render account-specific values into the template."
    )
    render_parser.add_argument(
        "--template", required=True, help="Path to the account-agnostic template file."
    )
    render_parser.add_argument(
        "--output",
        default=None,
        help="Path to write the rendered file (default: --template, in place).",
    )
    render_parser.add_argument(
        "--backup",
        default=None,
        help="Path to save the pre-render template bytes (default: <template>.orig).",
    )
    render_parser.add_argument(
        "--values-json", default="{}", help="JSON object mapping token name to substitution value."
    )
    render_parser.set_defaults(func=_cmd_render)

    restore_parser = sub.add_parser("restore", help="Restore the template from its backup.")
    restore_parser.add_argument("--template", required=True, help="Path to the file to restore.")
    restore_parser.add_argument(
        "--backup", default=None, help="Path the backup was saved to (default: <template>.orig)."
    )
    restore_parser.set_defaults(func=_cmd_restore)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
