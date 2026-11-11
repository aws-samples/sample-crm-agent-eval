#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: every generated SSE consumer still matches the Python schema.

`src/agents/events.py` is the SSE contract and `src/agents/event_schema.py` renders it as a JSON
Schema; generation flows Python -> JSON Schema -> {relay TS, frontend TS}. Both TypeScript consumers
are generated and never hand-edited.

Parity is asserted two ways:

1. **Regenerate and byte-diff**, by running the consumer's own generator (`npm run gen:types`) and
   comparing bytes. Every file the generator writes is restored afterwards.
2. **Structural comparison**, which is the only check for a consumer whose generator cannot run
   here (no `node_modules`). The output names each consumer that got only this check.

For each generated consumer on disk, (2) asserts:

* the `EventKind` set in the TypeScript equals the schema's `kind` enum exactly;
* likewise the `ErrorKind` set against the schema's `error_kind` enum;
* every schema property appears as a declared field;
* the consumer declares `SCHEMA_VERSION` equal to the schema version.

Union and interface names are discovered by overlap with the schema rather than assumed.

Usage: `python3 scripts/check_sse_schema_parity.py [--consumer PATH ...] [--no-regenerate]`

Exit codes: 0 pass; 1 a consumer diverged; 2 usage/environment error; 3 SKIP — the schema cannot be
imported, or no generated consumer exists, so nothing was verified. `scripts/run_gates.py` renders 3
as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`; a test pins the two together.
SKIP_EXIT_STATUS = 3

#: Generated TypeScript consumers of the SSE schema. Each is checked only if present.
DEFAULT_CONSUMERS: tuple[Path, ...] = (
    REPO_ROOT / "src" / "relay" / "src" / "generated" / "events.ts",
    REPO_ROOT / "frontend" / "src" / "types" / "generated" / "agentEvents.ts",
)

#: A TypeScript string-literal union: `type EventKind = "tool_call" | "usage";`, across lines.
_TS_UNION = re.compile(
    r"(?:type|enum)\s+(?P<name>\w+)\s*=\s*(?P<body>(?:\s*(?:\|\s*)?['\"][^'\"]+['\"])+)\s*;",
    re.MULTILINE,
)

#: The `as const` array form — `const EVENT_KINDS = ["tool_call", "usage"] as const;` — from which a
#: generator then derives `type EventKind = (typeof EVENT_KINDS)[number]`.
_TS_CONST_ARRAY = re.compile(
    r"(?:const|let|var)\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*\[(?P<body>[^\]]*)\]\s*(?:as\s+const)?\s*;",
    re.MULTILINE,
)

_TS_STRING_LITERAL = re.compile(r"['\"]([^'\"]+)['\"]")

#: A declared field in an interface or type-literal body: `tool_name: string | null;`.
_TS_FIELD = re.compile(r"^\s*(?:readonly\s+)?(?P<name>[A-Za-z_$][\w$]*)\??\s*:", re.MULTILINE)


def schema_enum(schema: dict[str, Any], property_name: str) -> set[str]:
    """Return the non-null enum members declared for one schema property.

    Args:
        schema: The rendered event-frame schema.
        property_name: Property whose `enum` to read (`kind` or `error_kind`).

    Returns:
        The declared string members, with the nullable `None` dropped.
    """
    declared = schema.get("properties", {}).get(property_name, {}).get("enum", [])
    return {member for member in declared if isinstance(member, str)}


def schema_properties(schema: dict[str, Any]) -> set[str]:
    """Return every wire field name the schema declares."""
    return set(schema.get("properties", {}))


def find_matching_union(text: str, expected: set[str]) -> tuple[str | None, set[str]]:
    """Find the union in `text` that is meant to be `expected`, and return its actual members.

    Both the bare-union and the `as const` array forms are considered; the candidate sharing the
    most members with `expected` is the one under test.

    Returns:
        `(declaration_name, members)`, or `(None, set())` when nothing overlaps `expected`.
    """
    best_name: str | None = None
    best_members: set[str] = set()
    best_overlap = 0
    for pattern in (_TS_UNION, _TS_CONST_ARRAY):
        for match in pattern.finditer(text):
            members = set(_TS_STRING_LITERAL.findall(match.group("body")))
            overlap = len(members & expected)
            if overlap > best_overlap:
                best_name, best_members, best_overlap = match.group("name"), members, overlap
    return best_name, best_members


def compare_consumer(
    relpath: str, text: str, schema: dict[str, Any], schema_version: str
) -> list[str]:
    """Report every way `text` diverges from `schema`.

    Args:
        relpath: Consumer path, for messages.
        text: The generated TypeScript source.
        schema: The rendered event-frame schema.
        schema_version: Version string the consumer must name.

    Returns:
        One message per divergence; empty when the consumer matches.
    """
    problems: list[str] = []

    for property_name, label in (("kind", "EventKind"), ("error_kind", "ErrorKind")):
        expected = schema_enum(schema, property_name)
        if not expected:
            continue
        name, actual = find_matching_union(text, expected)
        if name is None:
            problems.append(
                f"{relpath}: no string-literal union corresponds to the schema's `{property_name}` "
                f"enum ({sorted(expected)}). A consumer that does not enumerate {label} cannot "
                "surface an unrecognised frame; regenerate it."
            )
            continue
        missing = expected - actual
        extra = actual - expected
        if missing:
            problems.append(
                f"{relpath}: union `{name}` is missing {sorted(missing)}, which "
                f"`agents.events` declares. A frame of a kind the consumer does not list is "
                "invisible at both ends; regenerate rather than hand-edit."
            )
        if extra:
            problems.append(
                f"{relpath}: union `{name}` declares {sorted(extra)}, which `agents.events` does "
                "not. That is a hand-written member, which this check exists to prevent."
            )

    declared_fields = set(_TS_FIELD.findall(text))
    missing_fields = schema_properties(schema) - declared_fields
    if missing_fields:
        problems.append(
            f"{relpath}: does not declare the wire field(s) {sorted(missing_fields)} that the "
            "schema requires on every frame; regenerate it."
        )

    problems += _compare_declared_version(relpath, text, schema_version)

    return problems


#: A `SCHEMA_VERSION` declaration, capturing the value it is assigned. The leading `\b` rejects
#: longer names such as `PAGE_CONTEXT_SCHEMA_VERSION`.
_TS_VERSION_DECLARATION = re.compile(r"\bSCHEMA_VERSION\b\s*(?::[^=]+)?=\s*['\"]([^'\"]+)['\"]")


def _compare_declared_version(relpath: str, text: str, schema_version: str) -> list[str]:
    """Compare the consumer's declared `SCHEMA_VERSION` against the schema's, by value.

    Compares the declared value for equality, not whether the version string appears in the file.

    Args:
        relpath: Consumer path, for messages.
        text: The generated TypeScript source.
        schema_version: Version the schema declares.

    Returns:
        One message when the declaration is absent or its value differs; empty when they agree.
    """
    declared = _TS_VERSION_DECLARATION.search(text)
    if declared is None:
        return [
            f"{relpath}: declares no `SCHEMA_VERSION` constant. The relay asserts the version at "
            "its hop, so a version that exists only in a comment cannot be checked at "
            f'runtime — declare `SCHEMA_VERSION = "{schema_version}"`.'
        ]
    if declared.group(1) != schema_version:
        return [
            f"{relpath}: declares SCHEMA_VERSION = `{declared.group(1)}` but `agents.event_schema` "
            f"is at `{schema_version}`. Regenerate rather than hand-edit; a stale declared version "
            "makes the relay's own runtime check compare against the wrong value."
        ]
    return []


#: Regeneration commands, one per generated consumer: `(consumer_path, cwd, argv, also_written)`.
#: `also_written` names the other files the same generator emits, which are restored but not diffed.
REGENERATORS: tuple[tuple[Path, Path, list[str], tuple[Path, ...]], ...] = (
    (
        REPO_ROOT / "src" / "relay" / "src" / "generated" / "events.ts",
        REPO_ROOT / "src" / "relay",
        ["npm", "run", "--silent", "gen:types"],
        (),
    ),
    (
        REPO_ROOT / "frontend" / "src" / "types" / "generated" / "agentEvents.ts",
        REPO_ROOT / "frontend",
        ["npm", "run", "--silent", "gen:types"],
        (
            REPO_ROOT / "frontend" / "src" / "types" / "generated" / "toolSchemas.ts",
            REPO_ROOT / "frontend" / "src" / "types" / "generated" / "pageContext.ts",
        ),
    ),
)


#: Lock file serialising regeneration across processes. It is never removed.
_REGEN_LOCK_PATH = REPO_ROOT / "infra" / "build" / ".sse-parity-regen.lock"


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data` so no concurrent reader can observe a partial file.

    Writes a pid-named sibling temp file and renames it onto `path`, mirroring
    `writeFileAtomic` in `src/relay/scripts/gen-events.mjs`. The temp file is removed on failure.
    A no-op when `path` already holds exactly `data`.
    """
    if path.is_file() and path.read_bytes() == data:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _regeneration_lock() -> Iterator[None]:
    """Hold an exclusive advisory lock for the duration of a regenerate-and-restore cycle.

    Runs unlocked when `fcntl` is unavailable (a non-POSIX host).
    """
    _REGEN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX-only concern
        yield
        return
    with _REGEN_LOCK_PATH.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def regenerate_and_diff(
    consumer: Path, cwd: Path, argv: list[str], also_written: tuple[Path, ...] = ()
) -> tuple[str | None, str | None]:
    """Regenerate `consumer` via its own generator and compare with what was on disk.

    Every file the generator writes -- `consumer` and `also_written` -- is restored unconditionally.

    Args:
        consumer: The generated file to verify.
        cwd: Directory to run the generator from.
        argv: The generator command.
        also_written: Other files this generator emits, restored but not diffed.

    Returns:
        `(violation, unavailable_reason)`. Exactly one is non-`None`: a divergence message, or the
        reason this stronger check could not run (in which case the structural check still applies).
    """
    if not (cwd / "node_modules").is_dir():
        return None, f"{_display(cwd)}/node_modules is absent, so `{' '.join(argv)}` cannot run"

    # The lock serialises concurrent runs of this gate only; other processes that run the
    # generators (e.g. `npm run typecheck`) are not excluded.
    with _regeneration_lock():
        snapshots = {
            path: path.read_bytes() for path in (consumer, *also_written) if path.is_file()
        }
        before = snapshots.get(consumer)
        try:
            result = proc_run.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                return None, (
                    f"`{' '.join(argv)}` exited {result.returncode}, so the generator is "
                    f"unavailable:\n{result.stdout}\n{result.stderr}"
                )
            after = consumer.read_bytes() if consumer.is_file() else None
        finally:
            for path, original in snapshots.items():
                write_bytes_atomic(path, original)

    if after is None:
        return f"{_display(consumer)}: the generator ran but produced no file", None
    if before is None:
        return None, f"{_display(consumer)} did not exist before regeneration; nothing to diff"
    if before != after:
        return (
            f"{_display(consumer)} diverged from `agents.event_schema`: re-running "
            f"`{' '.join(argv)}` produced different bytes than what is on disk. The file is "
            "generated and must never be hand-edited — regenerate and commit the result."
        ), None
    return None, None


def _display(path: Path) -> str:
    """Render `path` relative to the repository when possible, else absolutely.

    A `--consumer` may point outside the repository.
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _load_schema() -> tuple[dict[str, Any], str]:
    """Render the schema and return it with its version. Raises `ImportError` when unavailable."""
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from agents.event_schema import SCHEMA_VERSION, build_event_schema

    return build_event_schema(), SCHEMA_VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--consumer",
        type=Path,
        action="append",
        dest="consumers",
        help="Generated consumer to check (repeatable). Defaults to the relay and frontend types.",
    )
    parser.add_argument(
        "--no-regenerate",
        action="store_true",
        help=(
            "Skip the regenerate-and-byte-diff pass, leaving only the structural check. Used by this "
            "gate's own tests, which point --consumer at a synthetic fixture no generator produces."
        ),
    )
    args = parser.parse_args(argv)
    consumers: tuple[Path, ...] = tuple(args.consumers or DEFAULT_CONSUMERS)

    try:
        schema, schema_version = _load_schema()
    except ImportError as exc:
        print(
            f"[SKIP] check_sse_schema_parity: cannot import agents.event_schema ({exc}), so the "
            "generator is unavailable. Nothing was verified."
        )
        return SKIP_EXIT_STATUS
    except Exception as exc:  # noqa: BLE001 — a generator that raises is a failure, not a skip.
        print(
            "check_sse_schema_parity: VIOLATION — rendering the event schema raised "
            f"{type(exc).__name__}: {exc}. The generator is the source of truth; if it cannot run, "
            "no consumer can be trusted.",
            file=sys.stderr,
        )
        return 1

    present = [path for path in consumers if path.is_file()]
    if not present:
        listed = ", ".join(_display(p) for p in consumers)
        print(
            f"[SKIP] check_sse_schema_parity: the generator renders "
            f"{len(schema_enum(schema, 'kind'))} event kind(s) at schema version "
            f"{schema_version}, but no generated consumer exists yet ({listed}). Parity needs both "
            "sides, so nothing was verified — this gate turns into a real check when the relay and "
            "frontend types land."
        )
        return SKIP_EXIT_STATUS

    problems: list[str] = []
    for path in present:
        relpath = _display(path)
        problems += compare_consumer(
            relpath, path.read_text(encoding="utf-8", errors="replace"), schema, schema_version
        )

    regenerated: list[str] = []
    unavailable: list[str] = []
    if not args.no_regenerate:
        for consumer, cwd, regen_argv, also_written in REGENERATORS:
            if consumer not in present:
                continue
            violation, reason = regenerate_and_diff(consumer, cwd, regen_argv, also_written)
            if violation is not None:
                problems.append(violation)
            elif reason is not None:
                unavailable.append(reason)
            else:
                regenerated.append(_display(consumer))

    if problems:
        print("check_sse_schema_parity: VIOLATION", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("check_sse_schema_parity: FAILED", file=sys.stderr)
        return 1

    skipped = [p for p in consumers if not p.is_file()]
    print(
        f"check_sse_schema_parity: OK — {len(present)} generated consumer(s) match the schema at "
        f"version {schema_version}: {sorted(schema_enum(schema, 'kind'))} / "
        f"{sorted(schema_enum(schema, 'error_kind'))}."
    )
    for relpath in regenerated:
        print(f"  byte-identical to a fresh regeneration: {relpath}")
    for reason in unavailable:
        print(f"  structural check only (byte diff unavailable): {reason}")
    for path in skipped:
        print(f"  not yet generated (unchecked): {_display(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
