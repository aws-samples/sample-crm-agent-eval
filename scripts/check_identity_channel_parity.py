#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assert the relay's identity-channel key list covers every Python channel key.

`crm.envelope.IDENTITY_CHANNEL_KEYS` lists every caller-controllable identity channel on the Python
side. The relay's own `IDENTITY_CHANNEL_KEYS` list, under `src/relay/src/`, is what
`stripIdentityChannels()` removes from the `page_context` and `tuning` values it forwards to the
Runtime. The relay list must be a superset of the Python one; relay-only keys are reported but allowed.

Usage: `python3 scripts/check_identity_channel_parity.py [--json]`

Exit codes: 0 pass; 1 the relay's list is missing a Python channel key; 2 usage/environment error;
**3 nothing was verified** — the relay source or the Python constant could not be read, which is a
`[SKIP]` and never a pass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: The relay's source tree, searched for the declaration.
_RELAY_SRC: Final[Path] = _REPO_ROOT / "src" / "relay" / "src"

#: Name of the relay's list (the same name as the Python constant).
_RELAY_CONST_NAME: Final[str] = "IDENTITY_CHANNEL_KEYS"

#: The declaration this gate reads. Anchored on `= [ … ]` so a `readonly string[]` type annotation
#: between the name and the literal cannot be mistaken for the literal itself.
_RELAY_DECLARATION: Final[re.Pattern[str]] = re.compile(
    rf"\b{_RELAY_CONST_NAME}\b[^=\n]*=\s*\[(?P<body>[^\]]*)\]",
    re.MULTILINE,
)


class UnverifiableError(Exception):
    """Raised when the gate cannot read one of the two sides, so it verified nothing (exit 3)."""


def python_channel_keys() -> frozenset[str]:
    """Return `crm.envelope.IDENTITY_CHANNEL_KEYS`.

    Raises:
        UnverifiableError: The module or the constant cannot be imported.
    """
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    try:
        from crm.envelope import IDENTITY_CHANNEL_KEYS
    except Exception as exc:  # noqa: BLE001 - any import failure means nothing was verified
        raise UnverifiableError(f"cannot import crm.envelope.IDENTITY_CHANNEL_KEYS: {exc}") from exc
    keys = frozenset(str(key) for key in IDENTITY_CHANNEL_KEYS)
    if not keys:
        raise UnverifiableError("crm.envelope.IDENTITY_CHANNEL_KEYS is empty")
    return keys


def relay_channel_keys(source_path: Path | None = None) -> frozenset[str]:
    """Return the string members of the relay's `IDENTITY_CHANNEL_KEYS` array.

    Parsed out of the TypeScript source text.

    Args:
        source_path: A relay module, or a tree to search. When omitted, :data:`_RELAY_SRC` is
            searched at call time. A directory or a single file is accepted.

    Raises:
        UnverifiableError: No relay module declares the list, more than one does, or the
            declaration has no members.
    """
    target = _RELAY_SRC if source_path is None else source_path
    if target.is_dir():
        candidates = sorted(target.rglob("*.ts"))
        if not candidates:
            raise UnverifiableError(f"no TypeScript sources under {target}")
    elif target.is_file():
        candidates = [target]
    else:
        raise UnverifiableError(f"relay source not found at {target}")

    found: list[tuple[Path, re.Match[str]]] = []
    for candidate in candidates:
        match = _RELAY_DECLARATION.search(candidate.read_text(encoding="utf-8"))
        if match is not None:
            found.append((candidate, match))
    if not found:
        searched = ", ".join(str(path.name) for path in candidates)
        raise UnverifiableError(
            f"no relay module declares an {_RELAY_CONST_NAME} array literal "
            f"(or its shape changed); searched: {searched}"
        )
    if len(found) > 1:
        where = ", ".join(str(path.name) for path, _ in found)
        raise UnverifiableError(
            f"{_RELAY_CONST_NAME} is declared in more than one relay module ({where}); "
            "one of them is a stale copy"
        )
    source_path, match = found[0]
    body = match.group("body")
    keys = frozenset(part.strip().strip('"').strip("'") for part in body.split(",") if part.strip())
    keys = frozenset(key for key in keys if key)
    if not keys:
        raise UnverifiableError(f"{_RELAY_CONST_NAME} in {source_path} lists no keys")
    return keys


def _relay_source_label() -> str:
    """Name the relay source tree in a message, repository-relative when it is inside the repository."""
    try:
        return str(_RELAY_SRC.relative_to(_REPO_ROOT))
    except ValueError:
        return str(_RELAY_SRC)


def check() -> dict[str, Any]:
    """Compare both lists and return a JSON-serialisable report.

    Returns:
        A report carrying both key sets and the Python keys the relay fails to strip.

    Raises:
        UnverifiableError: Either side could not be read.
    """
    python_keys = python_channel_keys()
    relay_keys = relay_channel_keys()
    missing = sorted(python_keys - relay_keys)
    return {
        "python_keys": sorted(python_keys),
        "relay_keys": sorted(relay_keys),
        "missing_from_relay": missing,
        "relay_only": sorted(relay_keys - python_keys),
        "ok": not missing,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the gate. See the module docstring for exit codes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    try:
        report = check()
    except UnverifiableError as exc:
        message = f"check_identity_channel_parity: SKIP — verified nothing ({exc})"
        if args.json:
            sys.stdout.write(json.dumps({"ok": False, "skipped": True, "reason": str(exc)}) + "\n")
        else:
            sys.stdout.write(message + "\n")
        return 3

    if args.json:
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0 if report["ok"] else 1

    if report["ok"]:
        sys.stdout.write(
            "check_identity_channel_parity: OK — the relay strips every Python identity channel key "
            f"{report['python_keys']}"
            + (f", plus {report['relay_only']}" if report["relay_only"] else "")
            + "\n"
        )
        return 0

    sys.stdout.write(
        "check_identity_channel_parity: FAILED — the relay does not strip "
        f"{report['missing_from_relay']}, which crm.envelope.decode_identity reads as an identity "
        f"channel. A caller could smuggle one into the Runtime payload through page_context or "
        f"tuning. Add it to IDENTITY_CHANNEL_KEYS in {_relay_source_label()}.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
