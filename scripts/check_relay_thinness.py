#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: the streaming relay stays thin.

The relay validates a Cognito JWT, calls `InvokeAgentRuntime` with SigV4, and re-emits frames. It
holds no business logic, no tool dispatch, and constructs no `Identity`. Four checks:

1. **No domain import.** Nothing under `src/relay/` may import from `src/crm/`, `src/agents/`,
   `src/gateway/`, or `src/eval/`, or reach outside its own package by relative path.
2. **No `Identity` construction.** No `new Identity(...)`, no `Identity(...)` call, no
   `identity_from_claims`, and no assembly of an identity-shaped object literal.
3. **A size ceiling on production source** under `src/`. Test files and generated files are exempt
   from the ceiling; generated files are still subject to checks 1 and 2.
4. **No identity attribute of its own.** Production source under `src/` sets no `principal_type`,
   `rep_id` or `full_visibility` key, with exactly one admitted exception: `src/runtime.ts` sets
   `principal_type: RELAY_PRINCIPAL_TYPE` once and declares `RELAY_PRINCIPAL_TYPE = "user"` once.

Reading `frontend/package.json` as data is not an import and is not flagged.

Usage: `python3 scripts/check_relay_thinness.py [--relay-dir DIR] [--max-lines N]`

Exit codes: 0 pass; 1 the relay grew a domain import, an `Identity`, or past the ceiling; 2 usage
error; 3 SKIP — no relay source found, so nothing was verified. `scripts/run_gates.py` renders 3 as
`[SKIP]`.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`; a test pins the two together.
SKIP_EXIT_STATUS = 3

DEFAULT_RELAY_DIR = REPO_ROOT / "src" / "relay"

#: Per-file line ceiling for hand-written relay production source.
DEFAULT_MAX_LINES = 500

#: Python packages that constitute the CRM domain and the enforcement boundary.
DOMAIN_PACKAGES = ("crm", "agents", "gateway", "eval")

#: Directories under the relay whose contents are generated; exempt from the size ceiling only.
GENERATED_DIR_PARTS = frozenset({"generated"})

_SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts", ".js", ".mjs"})
_EXCLUDED_DIR_PARTS = frozenset({"node_modules", "dist", "build", "coverage"})

#: An ES module specifier: `import ... from "x"`, `export ... from "x"`, `import("x")`,
#: `require("x")`.
_MODULE_SPECIFIER = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)['"](?P<specifier>[^'"]+)['"]"""
)
#: A bare side-effect import: `import "./polyfill.js";`.
_BARE_IMPORT = re.compile(r"""^\s*import\s+['"](?P<specifier>[^'"]+)['"]""", re.MULTILINE)

#: `new Identity(...)` / `Identity(...)` / `identity_from_claims(...)` / `decode_identity(...)`.
#: Word-bounded, so `VerifiedClaims` and `_identity` payload keys do not match.
_IDENTITY_CONSTRUCTION = re.compile(
    r"\b(?:new\s+Identity\s*\(|Identity\s*\(|identity_from_claims\s*\(|decode_identity\s*\()"
)

#: An object literal assigning a tenant field beside a subject/scope field in one literal.
_IDENTITY_SHAPED_LITERAL = re.compile(
    r"\{[^{}]*\btenant\s*:[^{}]*\b(?:scopes?|subject|sub|user_id)\s*:[^{}]*\}"
    r"|\{[^{}]*\b(?:scopes?|subject|user_id)\s*:[^{}]*\btenant\s*:[^{}]*\}",
    re.DOTALL,
)

#: An object key naming an identity attribute, quoted or bare, with the expression it is set to.
_IDENTITY_ATTRIBUTE_KEY = re.compile(
    r"""(?<![\w$])['"]?(?P<key>principal_type|rep_id|full_visibility)['"]?\s*:\s*(?P<value>[^,}\n]*)"""
)

#: The single identity attribute the relay may set: its channel's declared principal type.
ADMITTED_ATTRIBUTE_FILE = Path("src") / "runtime.ts"
ADMITTED_ATTRIBUTE = ("principal_type", "RELAY_PRINCIPAL_TYPE")
_ADMITTED_CONSTANT = re.compile(r"""\bconst\s+RELAY_PRINCIPAL_TYPE\s*=\s*(?P<value>[^;\n]*)""")
ADMITTED_CONSTANT_VALUE = '"user"'

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True)
class Violation:
    """One way the relay stopped being thin."""

    path: Path
    kind: str
    detail: str

    def render(self, relay_dir: Path) -> str:
        try:
            shown = self.path.relative_to(relay_dir).as_posix()
        except ValueError:
            shown = self.path.as_posix()
        return f"{shown}: [{self.kind}] {self.detail}"


def collect_relay_sources(relay_dir: Path) -> list[Path]:
    """Return every hand-written or generated relay source file, excluding installed dependencies."""
    found: list[Path] = []
    for path in sorted(relay_dir.rglob("*")):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        if _EXCLUDED_DIR_PARTS & set(path.relative_to(relay_dir).parts[:-1]):
            continue
        found.append(path)
    return found


def strip_comments(text: str) -> str:
    """Remove `//` and `/* */` comments, preserving line count."""
    without_block = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group().count("\n"), text)
    return _LINE_COMMENT.sub("", without_block)


def module_specifiers(code: str) -> list[str]:
    """Return every module specifier imported or required by `code` (comments already stripped)."""
    found = [m.group("specifier") for m in _MODULE_SPECIFIER.finditer(code)]
    found += [m.group("specifier") for m in _BARE_IMPORT.finditer(code)]
    return found


def find_domain_imports(path: Path, code: str, relay_dir: Path) -> list[Violation]:
    """Report every import that reaches a CRM/domain module or escapes the relay package."""
    violations: list[Violation] = []
    for specifier in module_specifiers(code):
        offence = _domain_offence(specifier, path, relay_dir)
        if offence is not None:
            violations.append(Violation(path, "domain-import", offence))
    return violations


def _domain_offence(specifier: str, path: Path, relay_dir: Path) -> str | None:
    """Return why `specifier` is a domain import, or `None` if it is legitimate."""
    if specifier.startswith("."):
        resolved = (path.parent / specifier).resolve()
        try:
            resolved.relative_to(relay_dir.resolve())
        except ValueError:
            return (
                f"relative import `{specifier}` resolves outside src/relay/ (to "
                f"{resolved}). The relay is an isolated package with its own lockfile; "
                "reaching out of it is how domain logic gets in."
            )
        return None
    first = specifier.split("/")[0]
    if first in DOMAIN_PACKAGES:
        return (
            f"imports the domain module `{specifier}`. The relay holds no business "
            "logic and no tool dispatch. Authorization is enforced in the CRM handlers and nowhere "
            "else authoritatively — a relay reaching into the domain becomes a second enforcement "
            "point no handler test covers."
        )
    return None


def find_identity_construction(path: Path, code: str) -> list[Violation]:
    """Report any construction of an identity, by call or by assembling an identity-shaped literal."""
    violations: list[Violation] = []
    for match in _IDENTITY_CONSTRUCTION.finditer(code):
        violations.append(
            Violation(
                path,
                "identity-construction",
                f"constructs an identity (`{match.group().strip()}`). `decode_identity` has exactly "
                "three channels and the relay forwards verified claims into the existing `agent` "
                "one; a fourth branch breaks the security core.",
            )
        )
    for match in _IDENTITY_SHAPED_LITERAL.finditer(code):
        violations.append(
            Violation(
                path,
                "identity-construction",
                "assembles an identity-shaped object literal (a `tenant` beside a subject or scope "
                f"field): `{' '.join(match.group().split())[:120]}`. Forward the verified claims "
                "untouched instead of deriving an identity from them.",
            )
        )
    return violations


def find_identity_attributes(path: Path, code: str, relay_dir: Path) -> list[Violation]:
    """Report identity attribute keys in production source beyond the one admitted declaration.

    Only files under `src/` are scanned. In :data:`ADMITTED_ATTRIBUTE_FILE`, exactly one
    `principal_type: RELAY_PRINCIPAL_TYPE` and exactly one `RELAY_PRINCIPAL_TYPE = "user"` are
    required; anything else, there or elsewhere, is a violation.
    """
    relative = path.relative_to(relay_dir)
    if relative.parts[0] != "src":
        return []
    admitted_here = relative == ADMITTED_ATTRIBUTE_FILE
    violations: list[Violation] = []
    admitted_count = 0
    for match in _IDENTITY_ATTRIBUTE_KEY.finditer(code):
        key, value = match.group("key"), match.group("value").strip()
        if admitted_here and (key, value) == ADMITTED_ATTRIBUTE:
            admitted_count += 1
            continue
        violations.append(
            Violation(
                path,
                "identity-attribute",
                f"sets the identity attribute `{key}: {value}`. The relay forwards verified "
                "claims verbatim; the only attribute it declares is "
                f"`{ADMITTED_ATTRIBUTE[0]}: {ADMITTED_ATTRIBUTE[1]}` in {ADMITTED_ATTRIBUTE_FILE}.",
            )
        )
    if not admitted_here:
        return violations
    if admitted_count != 1:
        violations.append(
            Violation(
                path,
                "identity-attribute",
                f"sets `{ADMITTED_ATTRIBUTE[0]}: {ADMITTED_ATTRIBUTE[1]}` {admitted_count} times; "
                "the Runtime payload must declare it exactly once.",
            )
        )
    constants = [m.group("value").strip() for m in _ADMITTED_CONSTANT.finditer(code)]
    if constants != [ADMITTED_CONSTANT_VALUE]:
        violations.append(
            Violation(
                path,
                "identity-attribute",
                f"declares RELAY_PRINCIPAL_TYPE as {constants!r}; it must be declared exactly once "
                f"as {ADMITTED_CONSTANT_VALUE}, the only principal the relay serves.",
            )
        )
    return violations


def find_oversize(path: Path, text: str, relay_dir: Path, max_lines: int) -> list[Violation]:
    """Report a hand-written production relay file past the line ceiling.

    Only files under `src/` outside a generated directory are measured.
    """
    parts = path.relative_to(relay_dir).parts
    if parts[0] != "src" or GENERATED_DIR_PARTS & set(parts[:-1]):
        return []
    count = len(text.splitlines())
    if count <= max_lines:
        return []
    return [
        Violation(
            path,
            "oversize",
            f"{count} lines, over the {max_lines}-line ceiling. If a reviewer cannot "
            "read the relay in one sitting, it has grown wrong. Either the responsibility belongs "
            "elsewhere, or raise the ceiling deliberately.",
        )
    ]


def scan_relay(relay_dir: Path, max_lines: int) -> list[Violation]:
    """Run all four checks over every relay source file."""
    violations: list[Violation] = []
    for path in collect_relay_sources(relay_dir):
        text = path.read_text(encoding="utf-8", errors="replace")
        code = strip_comments(text)
        violations += find_domain_imports(path, code, relay_dir)
        violations += find_identity_construction(path, code)
        violations += find_identity_attributes(path, code, relay_dir)
        violations += find_oversize(path, text, relay_dir, max_lines)
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--relay-dir", type=Path, default=DEFAULT_RELAY_DIR, help="Relay package root."
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_LINES,
        help=f"Per-file line ceiling for hand-written source (default {DEFAULT_MAX_LINES}).",
    )
    args = parser.parse_args(argv)

    relay_dir: Path = args.relay_dir
    if not relay_dir.is_dir():
        print(
            f"[SKIP] check_relay_thinness: {relay_dir} does not exist, so there is no relay to "
            "scan. Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    sources = collect_relay_sources(relay_dir)
    if not sources:
        print(
            f"[SKIP] check_relay_thinness: no relay source file under {relay_dir}. "
            "Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    violations = scan_relay(relay_dir, args.max_lines)
    if violations:
        print("check_relay_thinness: VIOLATION — the relay is no longer thin:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation.render(relay_dir)}", file=sys.stderr)
        print("check_relay_thinness: FAILED", file=sys.stderr)
        return 1

    production = [
        p
        for p in sources
        if p.relative_to(relay_dir).parts[0] == "src"
        and not GENERATED_DIR_PARTS & set(p.relative_to(relay_dir).parts[:-1])
    ]
    longest = max((len(p.read_text(errors="replace").splitlines()) for p in production), default=0)
    print(
        f"check_relay_thinness: OK — {len(sources)} relay source file(s) scanned for domain imports "
        f"and `Identity` construction or attributes; {len(production)} hand-written production file(s), longest "
        f"{longest} lines (ceiling {args.max_lines}, {args.max_lines - longest} to spare)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
