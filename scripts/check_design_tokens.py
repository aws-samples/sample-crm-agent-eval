#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: component source references design tokens, never literal values.

`frontend/src/styles/tokens.css` is the single source of colour, type, spacing, radius, elevation,
and motion. Four violation classes:

* **Raw colour** — a hex, `rgb()`, `rgba()`, `hsl()`, or `hsla()` literal. Legal only in a
  custom-property declaration (`--surface-raised: #fff`); illegal in any ordinary property or in
  TypeScript.
* **`px` font size** — `font-size: 13px`, `fontSize: "13px"`, or a Tailwind arbitrary
  `text-[13px]`. The type scale is `--text-*`, in `rem`.
* **Magic spacing** — a bare length in a spacing property that is neither a `var(--space-*)`
  reference nor a scale-free keyword (`0`, `auto`, `100%`, `inherit`, ...).
* **Series carried by hue alone** — a file referencing a `--series-N` model-family token without
  also wiring its fill or dash channel. See :func:`find_series_channel_violations`.

Scope is component source: `.tsx`, `.jsx`, `.ts`, and `.css`/`.module.css` under `frontend/src`,
excluding the token layer (`styles/tokens.css`, `styles/theme.ts`), tests, and generated files.

**Pending migration.** `scripts/design_token_migration_pending.txt` lists files not yet migrated to
the token layer; violations there are reported but do not fail. The gate fails when a listed file no
longer exists or no longer violates, so the list can only shrink.

Usage: `python3 scripts/check_design_tokens.py [--root DIR] [--pending FILE] [--no-pending]`

Exit codes: 0 pass; 1 violations outside the pending list, or a stale pending entry; 2 usage error;
3 SKIP — no component source was found, so nothing was verified.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS = 3

DEFAULT_ROOT = REPO_ROOT / "frontend" / "src"
DEFAULT_PENDING = REPO_ROOT / "scripts" / "design_token_migration_pending.txt"

#: Source suffixes in scope.
COMPONENT_SUFFIXES = frozenset({".tsx", ".jsx", ".ts", ".css"})

#: Token-layer paths (relative to the scanned root), where literals are declared rather than consumed.
EXCLUDED_RELPATHS = frozenset({"styles/tokens.css", "styles/theme.ts"})

#: Directory names excluded anywhere in a path: generated code, dependencies, and snapshots.
EXCLUDED_DIR_PARTS = frozenset({"generated", "node_modules", "__snapshots__"})

#: Test-file suffixes excluded from the scan.
_TEST_SUFFIXES = (".test.tsx", ".test.ts", ".test.jsx", ".spec.tsx", ".spec.ts")

#: CSS properties whose value carries spacing. `width`/`height` are not included.
SPACING_PROPERTIES = frozenset(
    {
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "margin-block",
        "margin-inline",
        "padding",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",
        "padding-block",
        "padding-inline",
        "gap",
        "row-gap",
        "column-gap",
        "top",
        "right",
        "bottom",
        "left",
        "inset",
    }
)

#: Spacing values that reference no scale and so cannot be "magic".
_SPACING_KEYWORDS = frozenset(
    {"0", "auto", "inherit", "initial", "unset", "revert", "100%", "none"}
)

#: A number carrying a CSS length unit.
_LENGTH_TERM = re.compile(r"-?[0-9.]+(?:px|rem|em|ch|ex|vh|vw|vmin|vmax|pt|pc|in|cm|mm|%)")

#: A 3-, 6-, or 8-digit hex colour. TypeScript is matched against the 6/8-digit form only, since a
#: 3-digit run is indistinguishable from a fragment identifier (`href="#abc"`).
_HEX_ANY = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-fA-F])")
_HEX_LONG = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6})(?![0-9a-fA-F])")
_FUNCTIONAL_COLOUR = re.compile(r"\b(?:rgba?|hsla?)\s*\(", re.IGNORECASE)

#: `font-size: 13px` in CSS, `fontSize: "13px"` in TS, and Tailwind's arbitrary `text-[13px]`.
_CSS_PX_FONT_SIZE = re.compile(r"font-size\s*:\s*[^;{}]*?[0-9.]+px", re.IGNORECASE)
_TS_PX_FONT_SIZE = re.compile(r"fontSize\s*:\s*['\"`]?\s*[0-9.]+px", re.IGNORECASE)
_TW_ARBITRARY_PX_TEXT = re.compile(r"\btext-\[[^\]]*?[0-9.]+px\]")
_TW_ARBITRARY_HEX = re.compile(
    r"\b(?:text|bg|border|ring|shadow|fill|stroke)-\[#[0-9a-fA-F]{3,8}\]"
)
_TW_ARBITRARY_SPACING = re.compile(
    r"\b(?:m|p)[trblxy]?-\[[^\]]*?[0-9.]+(?:px|rem|em)\]|\bgap-\[[^\]]*?[0-9.]+(?:px|rem|em)\]"
)

#: One `property: value` declaration, matched line-wise.
_CSS_DECLARATION = re.compile(r"(?P<property>[-a-zA-Z]+)\s*:\s*(?P<value>[^;{}]*)")

_CSS_COMMENTS = re.compile(r"/\*.*?\*/", re.DOTALL)
_TS_LINE_COMMENT = re.compile(r"//[^\n]*")


@dataclass(frozen=True)
class Violation:
    """One literal value found where a token reference belongs."""

    path: Path
    line: int
    kind: str
    detail: str

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line}: [{self.kind}] {self.detail}"


def collect_component_sources(root: Path) -> list[Path]:
    """Return every component source file under `root`, excluding the token layer and tests."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in COMPONENT_SUFFIXES:
            continue
        if EXCLUDED_DIR_PARTS & set(path.relative_to(root).parts[:-1]):
            continue
        if path.relative_to(root).as_posix() in EXCLUDED_RELPATHS:
            continue
        if path.name.endswith(_TEST_SUFFIXES):
            continue
        found.append(path)
    return found


def scan_css(path: Path, text: str) -> list[Violation]:
    """Report literal colour, `px` font size, and magic spacing in a stylesheet.

    A hex or functional colour inside a custom-property declaration (`--token: #fff`) is the token
    layer doing its job and is not reported; the same literal in an ordinary property is.
    """
    violations: list[Violation] = []
    stripped = _CSS_COMMENTS.sub(lambda m: "\n" * m.group().count("\n"), text)
    for lineno, line in enumerate(stripped.splitlines(), start=1):
        for match in _CSS_DECLARATION.finditer(line):
            prop = match.group("property").strip().lower()
            value = match.group("value")
            if prop.startswith("--"):
                continue
            colour = _HEX_ANY.search(value) or _FUNCTIONAL_COLOUR.search(value)
            if colour:
                violations.append(
                    Violation(
                        path,
                        lineno,
                        "raw-colour",
                        f"`{prop}: {value.strip()}` hardcodes {colour.group().rstrip('(')}; "
                        "reference a colour token (e.g. `var(--border-subtle)`) instead.",
                    )
                )
            if prop in SPACING_PROPERTIES:
                magic = _magic_spacing_value(value)
                if magic is not None:
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            "magic-spacing",
                            f"`{prop}: {value.strip()}` uses the off-scale length `{magic}`; "
                            "reference a spacing token (e.g. `var(--space-5)`) instead.",
                        )
                    )
        if _CSS_PX_FONT_SIZE.search(line):
            violations.append(
                Violation(
                    path,
                    lineno,
                    "px-font-size",
                    f"`{line.strip()}` sets a `px` font size; the type scale is `--text-*` in "
                    "`rem`, so a `px` size ignores the user's browser font-size setting.",
                )
            )
    return violations


def scan_typescript(path: Path, text: str) -> list[Violation]:
    """Report literal colour, `px` font size, and arbitrary Tailwind values in component code."""
    violations: list[Violation] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _TS_LINE_COMMENT.sub("", raw)
        hex_match = _HEX_LONG.search(line)
        if hex_match:
            violations.append(
                Violation(
                    path,
                    lineno,
                    "raw-colour",
                    f"hardcodes the colour `{hex_match.group()}`; reference a colour token or a "
                    "Tailwind utility bound to one.",
                )
            )
        if _FUNCTIONAL_COLOUR.search(line):
            violations.append(
                Violation(path, lineno, "raw-colour", f"hardcodes a colour: `{line.strip()}`")
            )
        if _TS_PX_FONT_SIZE.search(line) or _TW_ARBITRARY_PX_TEXT.search(line):
            violations.append(
                Violation(
                    path,
                    lineno,
                    "px-font-size",
                    f"sets a `px` font size: `{line.strip()}`; use a `--text-*` token.",
                )
            )
        if _TW_ARBITRARY_HEX.search(line):
            violations.append(
                Violation(
                    path,
                    lineno,
                    "raw-colour",
                    f"Tailwind arbitrary colour bypasses the theme: `{line.strip()}`",
                )
            )
        spacing = _TW_ARBITRARY_SPACING.search(line)
        if spacing:
            violations.append(
                Violation(
                    path,
                    lineno,
                    "magic-spacing",
                    f"Tailwind arbitrary spacing `{spacing.group()}` bypasses the spacing scale.",
                )
            )
    return violations


def _magic_spacing_value(value: str) -> str | None:
    """Return the first off-scale length in a spacing value, or `None` if every term is on-scale.

    A term is on-scale when it is a `var(--token)` reference, a calc over one, or a keyword that
    names no scale. A bare `11px`/`0.7rem` is not.
    """
    text = value.strip().lower()
    if not text or text in _SPACING_KEYWORDS:
        return None
    without_vars = re.sub(r"var\(\s*--[a-z0-9-]+\s*(?:,[^)]*)?\)", " ", text)
    # Only a unit-bearing term counts; a bare number is a `calc()` multiplier.
    for match in _LENGTH_TERM.finditer(without_vars):
        term = match.group()
        if re.fullmatch(r"-?0(?:\.0+)?[a-z%]*", term):
            continue
        return term
    return None


#: A reference to a model-family series token: the bare hue, or one of its channel variants.
_SERIES_REFERENCE = re.compile(r"--series-(?P<index>\d+)(?P<variant>-surface|-fill|-dash)?\b")

#: Channel variants that carry a series distinction by something other than hue.
_SERIES_NON_HUE_CHANNELS = frozenset({"-fill", "-dash"})


def find_series_channel_violations(path: Path, text: str) -> list[Violation]:
    """Report a file that carries a model-family series by hue alone.

    A file referencing a series token must also reference that series' `-fill` or `-dash` channel,
    even where its value is `none`. A file referencing only `--series-N-surface` is exempt.

    Args:
        path: File being scanned, for the message.
        text: Its source.

    Returns:
        One message per series carried by hue alone in this file; empty when every series referenced
        here also references a fill or dash channel.
    """
    channels: dict[str, set[str]] = {}
    for match in _SERIES_REFERENCE.finditer(text):
        index = match.group("index")
        channels.setdefault(index, set()).add(match.group("variant") or "")
    violations: list[Violation] = []
    for index, variants in sorted(channels.items()):
        if variants & _SERIES_NON_HUE_CHANNELS:
            continue
        if variants == {"-surface"}:
            continue
        violations.append(
            Violation(
                path,
                0,
                "series-hue-only",
                f"references `--series-{index}` (and/or its surface) but no "
                f"`--series-{index}-fill` or `--series-{index}-dash`. A model-family series carried "
                "by colour alone is unreadable in greyscale, and colour must not be "
                "the sole carrier of a between-family distinction.",
            )
        )
    return violations


def scan_file(path: Path) -> list[Violation]:
    """Dispatch `path` to the scanner for its language, plus the language-agnostic series check."""
    text = path.read_text(encoding="utf-8", errors="replace")
    language = scan_css(path, text) if path.suffix == ".css" else scan_typescript(path, text)
    return language + find_series_channel_violations(path, text)


def read_pending(pending_path: Path) -> list[str]:
    """Read the migration ratchet: one repo-relative path per line, `#` comments ignored."""
    if not pending_path.is_file():
        return []
    entries: list[str] = []
    for line in pending_path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            entries.append(stripped)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT, help="Component source root to scan."
    )
    parser.add_argument(
        "--pending", type=Path, default=DEFAULT_PENDING, help="Migration ratchet file."
    )
    parser.add_argument(
        "--no-pending",
        action="store_true",
        help="Ignore the ratchet and fail on every violation (used by the ratchet's own test).",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    if not root.is_dir():
        print(
            f"[SKIP] check_design_tokens: {root} does not exist, so no component source was "
            "found. Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    sources = collect_component_sources(root)
    if not sources:
        print(
            f"[SKIP] check_design_tokens: no component source (.tsx/.jsx/.css) under {root}. "
            "Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    pending = [] if args.no_pending else read_pending(args.pending)
    pending_set = set(pending)

    blocking: list[Violation] = []
    deferred: dict[str, list[Violation]] = {entry: [] for entry in pending}
    for path in sources:
        try:
            relpath = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            relpath = path.as_posix()
        for violation in scan_file(path):
            if relpath in pending_set:
                deferred[relpath].append(violation)
            else:
                blocking.append(violation)

    stale = [entry for entry, found in deferred.items() if not found]

    if blocking or stale:
        if blocking:
            print(
                f"check_design_tokens: VIOLATION — {len(blocking)} literal value(s) where a token "
                "reference belongs:",
                file=sys.stderr,
            )
            for violation in blocking:
                print(f"  - {violation.render(REPO_ROOT)}", file=sys.stderr)
        for entry in stale:
            print(
                f"check_design_tokens: STALE RATCHET ENTRY — `{entry}` is listed in "
                f"{args.pending.name} but has no violations (or does not exist). Remove the line: "
                "the list may only shrink, and a dead entry hides the next real violation in that "
                "file.",
                file=sys.stderr,
            )
        print("check_design_tokens: FAILED", file=sys.stderr)
        return 1

    print(
        f"check_design_tokens: OK — {len(sources)} component source file(s) scanned, no raw "
        "colour, `px` font size, or magic spacing value outside the token layer."
    )
    series_files = [p for p in sources if _SERIES_REFERENCE.search(p.read_text(errors="replace"))]
    if series_files:
        print(
            f"  series channels: {len(series_files)} file(s) reference a model-family series, each "
            "wiring a fill or dash channel as well as a hue."
        )
    else:
        print(
            "  series channels: no file references a `--series-*` token yet, so that check verified "
            "nothing. It becomes live when the eval dashboard's chart configuration lands (G11)."
        )
    for entry, found in deferred.items():
        print(
            f"  pending migration: {entry} carries {len(found)} known violation(s), deferred to "
            "its own re-skin task."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
