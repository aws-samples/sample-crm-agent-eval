#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: every `data-testid` a test reaches for still exists in component source.

Two checks:

1. **`data-testid` resolution.** Every id referenced by a `*ByTestId(...)` call or a
   `[data-testid="..."]` selector, including the CSS attribute-operator forms (`^=`, `$=`, `*=`,
   `~=`, `|=`; see :func:`unresolved_pattern_references`), must be defined in non-test source.
   References are read from the frontend root and from the reference-only roots in
   :data:`DEFAULT_EXTRA_TEST_ROOTS`; definitions are read from the frontend root alone. A definition
   may be a literal, either branch of a conditional, a template literal (resolving any id carrying
   its literal prefix), or an exported string constant (`data-testid={PROPOSAL_CARD_TESTID}`)
   resolved through a repository-wide constant table. Fewer than :data:`MINIMUM_REFERENCE_FLOOR`
   references on a substantial tree reports `[SKIP]`; an unresolved reference takes precedence.

2. **ARIA landmark floor.** Per-file counts of `role="status"`, `role="alert"` and `aria-live` must
   not drop below the floors in `scripts/aria_landmark_baseline.json`.

Implicit roles (`getByRole("button")` and similar) are not checked.

Usage: `python3 scripts/check_testid_preservation.py [--root DIR] [--extra-test-root DIR]...
[--baseline FILE] [--write-baseline]`

Exit codes: 0 pass; 1 an unresolved reference or a dropped landmark; 2 usage error; 3 SKIP — no test
(in any root) or no component source was found, or the reference floor was not met.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Exit status for "ran, verified nothing"; equals `scripts/run_gates.py`'s `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS = 3

DEFAULT_ROOT = REPO_ROOT / "frontend" / "src"
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "aria_landmark_baseline.json"

#: Additional roots scanned for **references only** (the T4 browser journeys). A missing root
#: contributes nothing.
DEFAULT_EXTRA_TEST_ROOTS: tuple[Path, ...] = (REPO_ROOT / "tests" / "e2e" / "browser" / "journeys",)

_TEST_SUFFIXES = (".test.tsx", ".test.ts", ".spec.tsx", ".spec.ts")

#: Suffixes a `data-testid` **definition** may live in (JSX only; `.ts` is read for references only).
_SOURCE_SUFFIXES = frozenset({".tsx", ".jsx"})
_EXCLUDED_DIR_PARTS = frozenset({"generated", "node_modules", "__snapshots__"})

#: `getByTestId("x")`, `queryAllByTestId('x')`, `findByTestId(`x`)` — every Testing Library variant.
_BY_TEST_ID = re.compile(r"(?:get|query|find)(?:All)?ByTestId\(\s*['\"`]([^'\"`]+)['\"`]")

#: A raw attribute selector, as used by `container.querySelector`. The optional backslashes admit
#: the escaped form a double-quoted JavaScript string requires (`"[data-testid=\"x\"]"`).
_TESTID_SELECTOR = re.compile(r"""\[data-testid=\\?['"]([^'"\\]+)\\?['"]\]""")

#: The same selector with a CSS attribute **operator** — `^=` prefix, `$=` suffix, `*=` substring,
#: `~=` whitespace-separated word, `|=` dash-separated prefix. Resolved by
#: :func:`unresolved_pattern_references`.
_TESTID_OPERATOR_SELECTOR = re.compile(
    r"""\[data-testid(?P<op>[\^$*~|])=\\?['"](?P<value>[^'"\\]+)\\?['"]\]"""
)

#: A literal definition: `data-testid="x"` or `data-testid={"x"}`. The negative lookbehind excludes
#: the `[data-testid="x"]` selector form.
_TESTID_LITERAL = re.compile(r"""(?<!\[)data-testid=\{?\s*['"]([^'"]+)['"]""")

#: A template-literal definition, captured up to its first interpolation, which becomes a prefix:
#: `data-testid={`pipeline-column-${stage}`}` resolves `pipeline-column-Qualification`.
_TESTID_TEMPLATE_PREFIX = re.compile(r"data-testid=\{\s*`([^`$]*)\$\{")

#: Every string literal inside a `data-testid={cond ? "a" : "b"}` expression, so both branches count.
_TESTID_CONDITIONAL = re.compile(r"data-testid=\{([^}]*\?[^}]*)\}")
_STRING_LITERAL = re.compile(r"""['"]([^'"]+)['"]""")

#: Fewest resolved references a substantial run may report before the gate declares it verified
#: nothing.
MINIMUM_REFERENCE_FLOOR = 5

#: Test-file count above which the floor applies at all.
FLOOR_APPLIES_ABOVE_TEST_FILES = 10

#: An exported string constant: `export const PROPOSAL_CARD_TESTID = "proposal-card";`.
_EXPORTED_STRING_CONST = re.compile(
    r"""export\s+const\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*['"]([^'"]+)['"]"""
)

#: `getByTestId(NAME)` / `queryAllByTestId(NAME)` — the identifier form, resolved via the constant
#: table above.
_BY_TEST_ID_IDENTIFIER = re.compile(
    r"(?:get|query|find)(?:All)?ByTestId\(\s*([A-Za-z_$][\w$]*)\s*\)"
)

#: `data-testid={NAME}` — the identifier form of a definition.
_TESTID_IDENTIFIER = re.compile(r"data-testid=\{\s*([A-Za-z_$][\w$]*)\s*\}")

#: Accessibility attributes counted by the landmark floor.
ARIA_LANDMARK_PATTERNS: dict[str, re.Pattern[str]] = {
    'role="status"': re.compile(r'role="status"'),
    'role="alert"': re.compile(r'role="alert"'),
    "aria-live": re.compile(r"aria-live="),
}


def _is_test(path: Path) -> bool:
    return path.name.endswith(_TEST_SUFFIXES)


def _in_excluded_dir(path: Path, root: Path) -> bool:
    return bool(_EXCLUDED_DIR_PARTS & set(path.relative_to(root).parts[:-1]))


def collect_files(root: Path) -> tuple[list[Path], list[Path]]:
    """Return `(test_files, source_files)` under `root`, partitioned by filename convention."""
    tests: list[Path] = []
    sources: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _in_excluded_dir(path, root):
            continue
        if _is_test(path):
            if path.suffix in {".tsx", ".ts", ".jsx"}:
                tests.append(path)
        elif path.suffix in _SOURCE_SUFFIXES:
            sources.append(path)
    return tests, sources


def collect_extra_test_files(roots: tuple[Path, ...]) -> list[Path]:
    """Test files from reference-only roots. A root that does not exist contributes nothing."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and _is_test(path) and path.suffix in {".tsx", ".ts", ".jsx"}:
                found.append(path)
    return found


def referenced_test_id_patterns(test_files: list[Path]) -> dict[tuple[str, str], list[str]]:
    """Map each `(operator, value)` attribute-selector reference to the test files using it.

    An interpolated value is skipped.
    """
    patterns: dict[tuple[str, str], list[str]] = {}
    for path in test_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _TESTID_OPERATOR_SELECTOR.finditer(text):
            value = match.group("value")
            if "${" in value:
                continue
            patterns.setdefault((match.group("op"), value), []).append(path.name)
    return patterns


def _pattern_is_satisfied(
    operator: str, value: str, literals: set[str], prefixes: set[str]
) -> bool:
    """Whether any definition can produce an id matching `operator`/`value`.

    One rule per CSS operator:

    * `^=` — some literal starts with `value`; or a template prefix that either extends `value` or is
      extended by it, since `data-testid={`mode-label-${x}`}` can produce `mode-label-anything`.
    * `|=` — as `^=`, plus the bare value itself (`[a|="x"]` matches `x` and `x-…`).
    * `$=` — some literal ends with `value`. A template definition is not accepted.
    * `*=` — some literal contains `value`; or a template prefix overlapping it in either direction.
    * `~=` — the attribute as a whitespace-separated word list; treated as exact membership of one of
      the words a literal declares.
    """
    if operator in {"^", "|"}:
        if any(literal.startswith(value) for literal in literals):
            return True
        return any(prefix.startswith(value) or value.startswith(prefix) for prefix in prefixes)
    if operator == "$":
        return any(literal.endswith(value) for literal in literals)
    if operator == "*":
        if any(value in literal for literal in literals):
            return True
        return any(value in prefix or prefix in value for prefix in prefixes)
    if operator == "~":
        return any(value in literal.split() for literal in literals)
    return False


def unresolved_pattern_references(
    patterns: dict[tuple[str, str], list[str]],
    literals: set[str],
    prefixes: set[str],
    local_definitions: dict[str, tuple[set[str], set[str]]] | None = None,
) -> list[str]:
    """Report every attribute-operator reference no definition can satisfy.

    Mirrors :func:`unresolved_references`, including per-test-file local definitions.
    """
    messages: list[str] = []
    local = local_definitions or {}
    for (operator, value), files in sorted(patterns.items()):
        if _pattern_is_satisfied(operator, value, literals, prefixes):
            continue
        unresolved_in = [
            name
            for name in sorted(set(files))
            if not _pattern_is_satisfied(operator, value, *local.get(name, (set(), set())))
        ]
        if not unresolved_in:
            continue
        messages.append(
            f'`[data-testid{operator}="{value}"]` is referenced by {", ".join(unresolved_in)} but no '
            f"`data-testid` defined in component source can match it. The selector form is why this "
            f"was previously invisible: an exact-string check does not see it, so deleting the "
            f"attribute left the gate green. Restore the attribute in the markup, or remove the "
            f"orphaned reference if the component was deliberately retired."
        )
    return messages


def exported_string_constants(files: list[Path]) -> dict[str, str]:
    """Map every `export const NAME = "value"` across `files` to its value.

    A name defined twice with different values is dropped.
    """
    seen: dict[str, str] = {}
    conflicting: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, value in _EXPORTED_STRING_CONST.findall(text):
            if name in seen and seen[name] != value:
                conflicting.add(name)
            seen[name] = value
    for name in conflicting:
        del seen[name]
    return seen


def referenced_test_ids(
    test_files: list[Path], constants: dict[str, str] | None = None
) -> dict[str, list[str]]:
    """Map each referenced `data-testid` to the test files referencing it.

    `constants` resolves the identifier form (`getByTestId(WIDGET_TESTID)`); an identifier that is
    not a known exported string constant contributes no reference.
    """
    resolved = constants or {}
    references: dict[str, list[str]] = {}
    for path in test_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (_BY_TEST_ID, _TESTID_SELECTOR):
            for test_id in pattern.findall(text):
                if "${" in test_id:
                    continue
                references.setdefault(test_id, []).append(path.name)
        for name in _BY_TEST_ID_IDENTIFIER.findall(text):
            value = resolved.get(name)
            if value is not None:
                references.setdefault(value, []).append(path.name)
    return references


def defined_test_ids(
    source_files: list[Path], constants: dict[str, str] | None = None
) -> tuple[set[str], set[str]]:
    """Return `(literal_ids, template_prefixes)` defined across `source_files`.

    `constants` resolves `data-testid={NAME}` to the value `NAME` holds.
    """
    resolved = constants or {}
    literals: set[str] = set()
    prefixes: set[str] = set()
    for path in source_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        literals.update(_TESTID_LITERAL.findall(text))
        for conditional in _TESTID_CONDITIONAL.findall(text):
            literals.update(_STRING_LITERAL.findall(conditional))
        for prefix in _TESTID_TEMPLATE_PREFIX.findall(text):
            if prefix:
                prefixes.add(prefix)
        for name in _TESTID_IDENTIFIER.findall(text):
            value = resolved.get(name)
            if value is not None:
                literals.add(value)
    return literals, prefixes


def unresolved_references(
    references: dict[str, list[str]],
    literals: set[str],
    prefixes: set[str],
    local_definitions: dict[str, tuple[set[str], set[str]]] | None = None,
) -> list[str]:
    """Report every referenced id that no definition can satisfy.

    Args:
        references: Referenced id -> the test files referencing it.
        literals: Literal ids defined in component source.
        prefixes: Template-literal prefixes defined in component source.
        local_definitions: Per-test-file `(literals, prefixes)` for ids defined by a stub component
            *inside* that same test file. A test file's local definitions resolve only its own
            references.
    """
    messages: list[str] = []
    local = local_definitions or {}
    for test_id, files in sorted(references.items()):
        if test_id in literals or any(test_id.startswith(p) for p in prefixes):
            continue
        unresolved_in = [
            name
            for name in sorted(set(files))
            if not _resolves_locally(test_id, local.get(name, (set(), set())))
        ]
        if not unresolved_in:
            continue
        messages.append(
            f'`data-testid="{test_id}"` is referenced by {", ".join(unresolved_in)} but is defined '
            "in no component source, and not by a stub inside that test file either. Two "
            "resolutions are valid: a retained test is not modified, so restore the "
            "attribute in the markup — or, if the component was deliberately retired, "
            "remove the now-orphaned reference from the test."
        )
    return messages


def _resolves_locally(test_id: str, definitions: tuple[set[str], set[str]]) -> bool:
    literals, prefixes = definitions
    return test_id in literals or any(test_id.startswith(p) for p in prefixes)


def count_aria_landmarks(source_files: list[Path], root: Path) -> dict[str, dict[str, int]]:
    """Count each landmark attribute per source file, keyed by root-relative POSIX path."""
    counts: dict[str, dict[str, int]] = {}
    for path in source_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        found = Counter(
            {name: len(pattern.findall(text)) for name, pattern in ARIA_LANDMARK_PATTERNS.items()}
        )
        present = {name: total for name, total in found.items() if total}
        if present:
            counts[path.relative_to(root).as_posix()] = present
    return counts


def landmark_regressions(
    baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]]
) -> list[str]:
    """Report every file whose landmark count dropped below its floor, or that vanished entirely."""
    messages: list[str] = []
    for relpath, expected in sorted(baseline.items()):
        actual = current.get(relpath)
        if actual is None:
            messages.append(
                f"{relpath}: file is gone or now carries no ARIA landmark, but the baseline "
                f"requires {expected}. If the component legitimately moved, refresh the baseline "
                "with --write-baseline in the same change."
            )
            continue
        for attribute, floor in sorted(expected.items()):
            found = actual.get(attribute, 0)
            if found < floor:
                messages.append(
                    f"{relpath}: `{attribute}` count dropped from {floor} to {found}. Every "
                    "ARIA landmark is part of the contract — a re-skin preserves them."
                )
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Frontend source root.")
    parser.add_argument(
        "--extra-test-root",
        type=Path,
        action="append",
        dest="extra_test_roots",
        help=(
            "Additional root scanned for test references only (no markup is read from it). "
            "Repeatable. Defaults to the T4 browser journeys; pass at least one to override."
        ),
    )
    parser.add_argument(
        "--baseline", type=Path, default=DEFAULT_BASELINE, help="ARIA landmark floor file."
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Rewrite the baseline from the current tree instead of checking against it.",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    if not root.is_dir():
        print(f"[SKIP] check_testid_preservation: {root} does not exist. Nothing was verified.")
        return SKIP_EXIT_STATUS

    tests, sources = collect_files(root)
    extra_roots = tuple(args.extra_test_roots or DEFAULT_EXTRA_TEST_ROOTS)
    extra_tests = collect_extra_test_files(extra_roots)

    # The reference side is test files from either kind of root.
    if not (tests or extra_tests) or not sources:
        print(
            f"[SKIP] check_testid_preservation: found {len(tests)} test file(s) under {root}, "
            f"{len(extra_tests)} in reference-only root(s), and {len(sources)} component file(s); "
            "both sides are needed to compare. Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    current_landmarks = count_aria_landmarks(sources, root)

    if args.write_baseline:
        args.baseline.write_text(
            json.dumps(current_landmarks, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            f"check_testid_preservation: wrote {args.baseline.name} with "
            f"{len(current_landmarks)} file(s)."
        )
        return 0

    # The constant table is built from both component and test sources.
    all_tests = tests + extra_tests
    constants = exported_string_constants(sources + all_tests)
    references = referenced_test_ids(all_tests, constants)
    patterns = referenced_test_id_patterns(all_tests)
    literals, prefixes = defined_test_ids(sources, constants)
    # Local (stub-component) definitions come from the root's test files only, never the extra roots.
    local_definitions = {path.name: defined_test_ids([path], constants) for path in tests}
    problems = unresolved_references(references, literals, prefixes, local_definitions)
    problems += unresolved_pattern_references(patterns, literals, prefixes, local_definitions)

    if args.baseline.is_file():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        problems += landmark_regressions(baseline, current_landmarks)
    else:
        print(
            f"check_testid_preservation: note — no ARIA landmark baseline at {args.baseline}; "
            "the data-testid check ran, the landmark floor did not. Create it with "
            "--write-baseline.",
            file=sys.stderr,
        )

    if problems:
        print("check_testid_preservation: VIOLATION", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("check_testid_preservation: FAILED", file=sys.stderr)
        return 1

    # The floor is checked only after unresolved references, which take precedence.
    if (
        len(all_tests) > FLOOR_APPLIES_ABOVE_TEST_FILES
        and (len(references) + len(patterns)) < MINIMUM_REFERENCE_FLOOR
    ):
        print(
            f"[SKIP] testid_preservation: resolved only "
            f"{len(references) + len(patterns)} data-testid reference(s) "
            f"across {len(all_tests)} test file(s), below the floor of {MINIMUM_REFERENCE_FLOOR}, and "
            "found nothing unresolved. An empty or near-empty reference set means references and "
            "definitions cancelled rather than agreed, so nothing was verified. Teach the gate the "
            "new mediation form — do NOT switch call sites back to string literals to satisfy it."
        )
        return SKIP_EXIT_STATUS

    landmark_total = sum(sum(v.values()) for v in current_landmarks.values())
    print(
        f"check_testid_preservation: OK — {len(references)} exact and {len(patterns)} "
        f"attribute-operator data-testid reference(s) across {len(all_tests)} test file(s) "
        f"({len(extra_tests)} from {len(extra_roots)} reference-only root(s)) all resolve against "
        f"{len(literals)} literal and {len(prefixes)} template definition(s) in {len(sources)} "
        f"component file(s); {landmark_total} ARIA landmark(s) across {len(current_landmarks)} "
        "file(s) at or above their floor."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
