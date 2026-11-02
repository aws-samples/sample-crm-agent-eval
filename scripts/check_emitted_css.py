#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: every static `className` token in first-party source resolves to a rule in the built CSS.

The defined set is the class selectors in the stylesheet compiled from the real Tailwind entry point
(`frontend/src/styles/tailwind.css` and its `frontend/tailwind.config.ts`), unioned with every
hand-written `.css` file under the root. For the default root the entry compiles over its own
`@source` paths, as the build does, after asserting they cover every component file; for any other
`--root` they are swapped for that root alone. Class names in CSS comments are not definitions, and
comparison is set membership, never substring containment.

Every static string literal in first-party `.tsx`/`.jsx` (test files excluded) is read, and a
literal is treated as a class list only when it already looks like one (see
:func:`extract_class_tokens`). A class assembled at runtime — an interpolated template literal, a
prop, a concatenation — is not visible, so the pass message reports a count rather than totality.

Two anti-vacuity floors are computed by scans independent of the extractor:
:func:`independent_class_attribute_floor` and :func:`starved_class_attributes`.

Usage: ``python3 scripts/check_emitted_css.py [--root DIR] [--css FILE] [--json]``

Exit codes: 0 pass; 1 unresolved classes, a token floor breach, a component file no `@source`
covers, or an entry point that cannot be bounded to `--root`; 2 usage error; 3 SKIP — the
stylesheet could not be built, defines no class selector at all, or does not define the codebase's
most-used class (see :func:`derived_positive_control`), so **nothing was verified**.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePath

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

DEFAULT_ROOT = REPO_ROOT / "frontend" / "src"
FRONTEND = REPO_ROOT / "frontend"

#: Kept identical to `run_gates.py`'s `GATE_SKIP_EXIT_STATUS`; a test pins the two together.
SKIP_EXIT_STATUS = 3

#: Source extensions that can carry a `className`.
SOURCE_SUFFIXES = frozenset({".tsx", ".jsx"})

#: Directories with no first-party component source.
EXCLUDED_DIR_NAMES = frozenset({"node_modules", "generated", "dist", "__snapshots__"})

#: Every single-quoted, double-quoted or backtick string literal on a line.
_STRING_LITERAL = re.compile(r"""["'`]([^"'`\n]{2,})["'`]""")

#: `className=` occurrences, for the independent anti-vacuity floor only.
_CLASS_ATTR = re.compile(r"""className=\{?["']([^"'{}]+)["']\}?""")

#: The shape a Tailwind utility or hand-written class name can take. Excludes uppercase, so prose
#: does not match. The leading character may be `[` or `!` (e.g. `[&[hidden]]:hidden`).
_UTILITY_SHAPED = re.compile(r"^[a-z0-9\[!-][a-z0-9\-:\[\]/.%&!(),'\"#*>~+_]*$")

#: A token carrying Tailwind's bracket grammar is treated as a class without corroborating siblings:
#: a bracketed variant (`[hidden]:`, `[&[hidden]]:`, `data-[open=false]:`) or an arbitrary value
#: (`-[…]`). A single-token variant without a bracket (`"lg:w-panel"`) is not admitted.
_UNAMBIGUOUS_CLASS_SYNTAX = re.compile(r"(?:^|:)\[|-\[")

#: The independent scan backing the anti-vacuity floor.
_CLASS_ATTR_OCCURRENCE = re.compile(r"className\s*=")

#: A Tailwind class may legally contain `:[]/.%!&>~*+()#,'"` once escaped; treat anything
#: non-whitespace as a candidate token and let the stylesheet decide.
_TOKEN_SPLIT = re.compile(r"\s+")

#: Tokens that are never class names: JSX/template artefacts that survive a naive split.
_NON_CLASS_TOKENS = frozenset({"", "&&", "||", "?", ":", "undefined", "null"})

#: A class name inside a selector. CSS's numeric escape (`\2c ` — hex plus an optional single
#: trailing space) is tried before the single-character escape, so it is not truncated at the space.
_CLASS_IN_SELECTOR = re.compile(r"\.((?:\\[0-9a-fA-F]{1,6}[ \t\n]?|\\.|[^\s.,>+~:\[\]{}()'\"])+)")


def _strip_source_comments(text: str) -> str:
    """Blank out TS/JSX comments, preserving line count so reported line numbers stay correct.

    A ``//`` preceded by ``:`` (as in ``https://``) is not treated as a line comment.
    """
    without_blocks = re.sub(
        r"/\*.*?\*/", lambda match: re.sub(r"[^\n]", " ", match.group(0)), text, flags=re.DOTALL
    )
    return re.sub(r"(?<!:)//[^\n]*", lambda match: " " * len(match.group(0)), without_blocks)


def _unescape_css_identifier(name: str) -> str:
    """Resolve CSS escapes in a class name, numeric form included."""

    def replace(match: re.Match[str]) -> str:
        numeric = match.group(1)
        return chr(int(numeric, 16)) if numeric else match.group(2)

    return re.sub(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n]?|(.))", replace, name)


def collect_sources(root: Path) -> list[Path]:
    """Return first-party component sources under ``root``, excluding generated and vendored trees.

    Test files (``.test.`` in the name) are excluded.
    """
    return sorted(
        path
        for path in root.rglob("*")
        if path.suffix in SOURCE_SUFFIXES
        and path.is_file()
        and ".test." not in path.name
        and not any(part in EXCLUDED_DIR_NAMES for part in path.relative_to(root).parts)
    )


def extract_class_tokens(
    sources: list[Path], root: Path, defined: set[str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Map each class token found in a string literal to the ``relpath:line`` locations using it.

    A literal is treated as a class list only when every token is utility-shaped and either at least
    half of its tokens are already defined in the stylesheet or one token matches
    :data:`_UNAMBIGUOUS_CLASS_SYNTAX`. A literal whose tokens are mostly undefined is declined.

    Returns the token-to-locations map and the ``relpath:line`` of every declined literal.
    """
    locations: dict[str, list[str]] = {}
    declined: list[str] = []
    for path in sources:
        relpath = path.relative_to(root).as_posix()
        source = _strip_source_comments(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(source.splitlines(), start=1):
            for literal in _STRING_LITERAL.findall(line):
                tokens = [
                    t for t in _TOKEN_SPLIT.split(literal.strip()) if t not in _NON_CLASS_TOKENS
                ]
                if not tokens or not all(_UTILITY_SHAPED.match(token) for token in tokens):
                    continue
                resolved = sum(1 for token in tokens if token in defined)
                unambiguous = any(_UNAMBIGUOUS_CLASS_SYNTAX.search(token) for token in tokens)
                if resolved * 2 < len(tokens) and not unambiguous:
                    declined.append(f"{relpath}:{lineno}")
                    continue
                for token in tokens:
                    locations.setdefault(token, []).append(f"{relpath}:{lineno}")
    return locations, declined


def independent_class_attribute_floor(sources: list[Path]) -> int:
    """Count ``className=`` occurrences with a scan independent of :func:`extract_class_tokens`.

    The anti-vacuity floor: a non-zero count with no extracted token is a failure.
    """
    return sum(
        len(_CLASS_ATTR_OCCURRENCE.findall(path.read_text(encoding="utf-8"))) for path in sources
    )


def parse_class_selectors(css: str) -> set[str]:
    """Return every class name the stylesheet defines, unescaped.

    ``/* */`` comments are stripped and only selector text is scanned, so a class name in a comment or
    a property value is not a definition. A backslash escapes any next character (Tailwind writes
    `.sm\\:grid-cols-2`).
    """
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)

    selectors: set[str] = set()
    for selector_text in re.findall(r"(^|[}\n])\s*([^{}@]+?)\s*\{", css, flags=re.MULTILINE):
        for match in re.finditer(_CLASS_IN_SELECTOR, selector_text[1]):
            selectors.add(_unescape_css_identifier(match.group(1)))
    return selectors


#: Every `@source …;` statement in an entry point, with its argument.
_SOURCE_STATEMENT = re.compile(r"@source\b([^;]*);")

#: A `@source` content path: one quoted string.
_QUOTED_PATH = re.compile(r"""^(["'])(.+)\1$""")

#: Tailwind imported with automatic source detection disabled.
_SOURCE_NONE_IMPORT = re.compile(r"""@import\s+(["'])tailwindcss\1\s+source\(none\)""")

#: A single-level brace alternation in a glob, e.g. `{ts,tsx}`.
_BRACE = re.compile(r"\{([^{}]*)\}")

#: Compiles the stylesheet on stdin through the frontend's own PostCSS + Tailwind plugin.
_COMPILE_SCRIPT = (
    'import { readFileSync } from "node:fs";'
    'import postcss from "postcss";'
    'import tailwind from "@tailwindcss/postcss";'
    "const result = await postcss([tailwind()])"
    '.process(readFileSync(0, "utf8"), { from: process.argv[1] });'
    "process.stdout.write(result.css);"
)


#: A CSS string literal or a comment; strings are matched first so `/**/` inside a glob survives.
_CSS_STRING_OR_COMMENT = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|/\*.*?\*/""", re.DOTALL)


def _strip_css_comments(css: str) -> str:
    return _CSS_STRING_OR_COMMENT.sub(
        lambda match: "" if match.group(0).startswith("/*") else match.group(0), css
    )


def entry_content_paths(entry_css: str) -> list[str]:
    """Return the content paths of every `@source "<path>";` statement, comments ignored.

    `@source not …` and `@source inline(…)` are not content paths. Raises ``ValueError`` on any other
    `@source` argument, so a statement this parser misreads cannot pass unexamined.
    """
    paths: list[str] = []
    for match in _SOURCE_STATEMENT.finditer(_strip_css_comments(entry_css)):
        argument = match.group(1).strip()
        if argument.startswith(("not ", "inline(")):
            continue
        quoted = _QUOTED_PATH.match(argument)
        if quoted is None:
            raise ValueError(f"unparsed `@source {argument};` in the Tailwind entry point")
        paths.append(quoted.group(2))
    return paths


def entry_over_sources(entry_css: str, content_globs: list[str]) -> str:
    """Return ``entry_css`` with its `@source` content paths replaced by ``content_globs``.

    Raises ``ValueError`` when the entry names no content path, carries a `@source` it cannot parse,
    or does not import Tailwind with `source(none)`, since then the compile would not be bounded to
    the globs.
    """
    css = _strip_css_comments(entry_css)
    if not _SOURCE_NONE_IMPORT.search(css):
        raise ValueError("the Tailwind entry point does not import Tailwind with `source(none)`")
    if not entry_content_paths(css):
        raise ValueError("the Tailwind entry point declares no `@source` content path")
    directives = "\n".join(f"@source {json.dumps(glob)};" for glob in content_globs)
    content = [
        match
        for match in _SOURCE_STATEMENT.finditer(css)
        if _QUOTED_PATH.match(match.group(1).strip())
    ]
    head, tail = css[: content[0].start()], css[content[0].start() :]
    return (
        head
        + directives
        + _SOURCE_STATEMENT.sub(
            lambda match: "" if _QUOTED_PATH.match(match.group(1).strip()) else match.group(0), tail
        )
    )


def _expand_braces(glob: str) -> list[str]:
    match = _BRACE.search(glob)
    if match is None:
        return [glob]
    return [
        expanded
        for option in match.group(1).split(",")
        for expanded in _expand_braces(glob[: match.start()] + option + glob[match.end() :])
    ]


def uncovered_sources(entry: Path, sources: list[Path]) -> list[Path]:
    """The ``sources`` that no `@source` content path of the ``entry`` stylesheet matches.

    Paths resolve against the entry's directory; `**` and single-level `{a,b}` are honoured.
    """
    patterns = [
        os.path.normpath(entry.parent.resolve() / expanded)
        for path in entry_content_paths(entry.read_text(encoding="utf-8"))
        for expanded in _expand_braces(path)
    ]
    return [
        source
        for source in sources
        if not any(PurePath(str(source.resolve())).full_match(pattern) for pattern in patterns)
    ]


def build_stylesheet(content_globs: list[str] | None) -> str | None:
    """Compile the real Tailwind entry point; ``None`` if it cannot run.

    Uses `frontend/src/styles/tailwind.css` and, through its `@config`, `frontend/tailwind.config.ts`.
    ``content_globs=None`` compiles the entry's own `@source` paths, exactly as the build does;
    otherwise they are swapped for ``content_globs``, raising ``ValueError`` as
    :func:`entry_over_sources` does when the entry cannot be bounded.
    """
    plugin = FRONTEND / "node_modules" / "@tailwindcss" / "postcss"
    if not plugin.exists() or shutil.which("node") is None:
        return None
    entry = FRONTEND / "src" / "styles" / "tailwind.css"
    if not entry.exists():
        return None
    css = entry.read_text(encoding="utf-8")
    if content_globs is not None:
        css = entry_over_sources(css, content_globs)
    command = ["node", "--input-type=module", "--eval", _COMPILE_SCRIPT, str(entry)]
    try:
        result = proc_run.run(
            command,
            cwd=FRONTEND,
            input=css,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


def starved_class_attributes(
    sources: list[Path], root: Path, tokens: dict[str, list[str]]
) -> list[str]:
    """Static `className="…"` attributes that contributed **no** token to the analysis.

    Asserts per attribute rather than in aggregate. ``_CLASS_ATTR`` matches only a static quoted
    value, so dynamic attributes are not considered.
    """
    starved: list[str] = []
    for path in sources:
        relpath = path.relative_to(root).as_posix()
        source = _strip_source_comments(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(source.splitlines(), start=1):
            for value in _CLASS_ATTR.findall(line):
                place = f"{relpath}:{lineno}"
                candidates = [
                    token
                    for token in _TOKEN_SPLIT.split(value.strip())
                    if token not in _NON_CLASS_TOKENS
                ]
                if candidates and not any(place in tokens.get(token, []) for token in candidates):
                    starved.append(f"{place} — {value.strip()[:60]}")
    return starved


def derived_positive_control(sources: list[Path]) -> str | None:
    """The most-used utility-shaped token in source, which a healthy build must define.

    The gate's positive control against a partially degraded stylesheet. Returns ``None`` when the
    tree has no token used more than once.
    """
    counts: dict[str, int] = {}
    for path in sources:
        for literal in _STRING_LITERAL.findall(
            _strip_source_comments(path.read_text(encoding="utf-8"))
        ):
            for token in _TOKEN_SPLIT.split(literal.strip()):
                if token not in _NON_CLASS_TOKENS and _UTILITY_SHAPED.match(token):
                    counts[token] = counts.get(token, 0) + 1
    if not counts:
        return None
    control = max(sorted(counts), key=lambda token: counts[token])
    return control if counts[control] > 1 else None


def unresolved_classes(tokens: dict[str, list[str]], defined: set[str]) -> dict[str, list[str]]:
    """Tokens with no class selector in the stylesheet.

    Set membership, never containment: `.h-control` does not match `.h-control-lg`.
    """
    return {token: places for token, places in sorted(tokens.items()) if token not in defined}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Component source root.")
    parser.add_argument(
        "--css",
        type=Path,
        default=None,
        help="Pre-built stylesheet to read instead of compiling (used by this gate's own tests).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args(argv)

    root: Path = args.root
    if not root.is_dir():
        print(f"[SKIP] check_emitted_css: {root} does not exist. Nothing was verified.")
        return SKIP_EXIT_STATUS

    sources = collect_sources(root)
    if not sources:
        print(f"[SKIP] check_emitted_css: no .tsx/.jsx source under {root}. Nothing was verified.")
        return SKIP_EXIT_STATUS

    floor = independent_class_attribute_floor(sources)

    if args.css is not None:
        css = args.css.read_text(encoding="utf-8") if args.css.exists() else None
    else:
        production_root = root.resolve() == DEFAULT_ROOT.resolve()
        entry = FRONTEND / "src" / "styles" / "tailwind.css"
        try:
            if production_root and entry.exists():
                missed = uncovered_sources(entry, sources)
                if missed:
                    print(
                        "[FAIL] check_emitted_css: no `@source` in the Tailwind entry point covers "
                        f"{len(missed)} component file(s), so the build emits no class from them:"
                    )
                    for path in missed:
                        print(f"  {path.relative_to(root).as_posix()}")
                    return 1
            globs = None if production_root else [f"{root}/**/*.{{ts,tsx,jsx}}"]
            css = build_stylesheet(globs)
        except ValueError as error:
            print(f"[FAIL] check_emitted_css: {error}")
            return 1

    if css is None:
        print(
            "[SKIP] check_emitted_css: could not build the stylesheet (no `@tailwindcss/postcss`, no "
            "node, or the compile failed), so no class was checked. Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    # The defined set is the compiled Tailwind output plus every hand-written stylesheet.
    defined = parse_class_selectors(css)
    for stylesheet in sorted(root.rglob("*.css")):
        if any(part in EXCLUDED_DIR_NAMES for part in stylesheet.relative_to(root).parts):
            continue
        defined |= parse_class_selectors(stylesheet.read_text(encoding="utf-8"))

    if not defined:
        print(
            "[SKIP] check_emitted_css: the compiled stylesheet defines no class selector at all, so "
            "every class would report unresolved. Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    control = derived_positive_control(sources)
    control_note = (
        f"positive control `{control}` present"
        if control is not None
        else "no positive control applied — no class in this tree repeats, so a degraded build "
        "would not be detected here"
    )
    if control is not None and control not in defined:
        print(
            f"[SKIP] check_emitted_css: the most-used class in source (`{control}`) is absent from the "
            "compiled stylesheet, so the build is degraded rather than the class inert. Every "
            "comparison below would be measured against a partial stylesheet. Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    tokens, declined = extract_class_tokens(sources, root, defined)

    starved = starved_class_attributes(sources, root, tokens)
    if starved:
        print(
            "check_emitted_css: FAILED — static className attribute(s) contributed no token at all,"
        )
        print("so the extractor or the class-list heuristic has stopped seeing them:")
        for place in starved[:10]:
            print(f"  - {place}")
        if len(starved) > 10:
            print(f"  (+{len(starved) - 10} more)")
        return 1

    if floor > 0 and not tokens:
        print(
            f"check_emitted_css: FAILED — {floor} `className=` attribute(s) exist in source but the "
            "extractor produced no tokens, so this gate would have verified nothing. Its extraction "
            "has stopped matching the source's shape."
        )
        return 1

    unresolved = unresolved_classes(tokens, defined)

    if args.json:
        print(
            json.dumps(
                {
                    "sources": len(sources),
                    "tokens": len(tokens),
                    "class_attributes": floor,
                    "defined_selectors": len(defined),
                    "unresolved": unresolved,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if unresolved else 0

    if unresolved:
        print("check_emitted_css: VIOLATION")
        for token, places in unresolved.items():
            print(
                f"  - `{token}` is used but the compiled stylesheet defines no rule for it, so it "
                f"styles nothing. Used at: {', '.join(places[:4])}"
                + (f" (+{len(places) - 4} more)" if len(places) > 4 else "")
            )
        print("check_emitted_css: FAILED")
        return 1

    print(
        f"check_emitted_css: OK — {len(tokens)} static class token(s) across {len(sources)} "
        f"component file(s) all resolve against {len(defined)} emitted selector(s)."
    )
    print(
        "  coverage limit: static string literals, including hoisted class constants and `cn()` "
        "arguments. A class assembled at runtime (interpolated template literal, prop, concatenation) "
        f"is not visible here, so this count is a subset; {floor} `className=` attribute(s) were "
        "counted by an independent scan, and every one contributed at least one token."
    )
    print(f"  {control_note}.")
    print(
        f"  {len(declined)} string literal(s) declined as not class lists (prose, identifiers, test ids). "
        "A sharp rise here means the heuristic is refusing real class lists — see `extract_class_tokens`."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
