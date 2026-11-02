#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prove that a comment or docstring edit changed no code, against a named git ref.

Usage: ``comment_sweep_check.py [--index] <ref> <path>...``

The new side is the worktree file, or with ``--index`` the staged blob (``git show :<path>``).

For ``.py`` it compares the AST with every docstring blanked; any difference is a code change and
fails.

For ``.tsx`` it compares the syntax trees the TypeScript parser builds, with comments excluded and
JSX text kept verbatim. The braces of a JSX ``{/* */}`` comment are kept, so editing its text
passes but adding or removing one fails. The parser is ``frontend/node_modules/typescript``, run
under ``node``. The file is reported ``UNVERIFIED`` and not passed when ``node`` cannot be run, the
TypeScript package is absent or fails to load, the comparison does not complete, or the parser
reports a syntax error on either side.

For ``.sh`` / ``.tf`` / ``.ts`` / ``.mjs`` it compares the file with whole-line comments and blank
lines removed. Lines inside a heredoc, a quoted string or a JS/TS template literal are kept
verbatim, so a comment-shaped line there counts as code. A trailing comment on a code line is
reported ``UNVERIFIED`` but passed. The file is reported ``UNVERIFIED`` and not passed when a
heredoc, string or comment is still open at end of file, a ``<<`` has no heredoc delimiter, or (in
``.tf`` / ``.ts`` / ``.mjs``) a quoted string is not closed on its line.

An extensionless file carrying a shell shebang is treated as ``.sh``. Exit status is 1 if any file
failed, 3 if none failed but a file is ``UNVERIFIED``, 2 if the arguments do not name a ref and at
least one path, and 0 otherwise. The ``checked=`` count must equal the number of paths intended: a
path that is absent at the ref is skipped and not counted.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Lexer dialect per extension.
DIALECT: dict[str, str] = {
    ".sh": "sh",
    ".tf": "hcl",
    ".ts": "js",
    ".mjs": "js",
}

_TYPESCRIPT_PACKAGE = (
    Path(__file__).resolve().parents[1] / "frontend" / "node_modules" / "typescript"
)

#: Reads ``{"path", "old", "new"}`` on stdin and prints whether the two TSX syntax trees match.
_TSX_COMPARE_JS = r"""
const ts = require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
function shape(src) {
  const sf = ts.createSourceFile(input.path, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  if (!Array.isArray(sf.parseDiagnostics)) throw new Error("parseDiagnostics is unavailable");
  const out = [];
  (function walk(n) {
    if (n.kind >= ts.SyntaxKind.FirstJSDocNode && n.kind <= ts.SyntaxKind.LastJSDocNode) return;
    const kind = ts.SyntaxKind[n.kind];
    if (n.kind === ts.SyntaxKind.JsxText) { out.push(kind + ":" + src.slice(n.pos, n.end)); return; }
    const kids = n.getChildren(sf);
    if (kids.length === 0) { out.push(kind + ":" + n.getText(sf)); return; }
    out.push("(" + kind);
    for (const k of kids) walk(k);
    out.push(")");
  })(sf);
  return { out, errors: sf.parseDiagnostics.length };
}
const a = shape(input.old), b = shape(input.new);
const same = a.out.length === b.out.length && a.out.every((t, i) => t === b.out[i]);
process.stdout.write(JSON.stringify({ same, errors: a.errors + b.errors }));
"""

_BLANKED_DOCSTRING = "<docstring>"

#: Lexer states that are part of a string literal, or code nested inside one.
_LITERAL_STATES = frozenset({"sq", "dq", "ansi", "bt", "tmpl", "subst", "interp"})

_SH_WORD_BREAK = frozenset(" \t;&|()<>")
_JS_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")
_JS_REGEX_KEYWORD = re.compile(r"\b(?:return|typeof|case|void|delete|in|of|new|throw|yield|await)$")
_HCL_HEREDOC_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def text_at_ref(ref: str, path: str) -> str | None:
    """Return `path`'s content at `ref`, or None when the ref does not carry that path."""
    result = proc_run.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def blank_docstrings(tree: ast.AST) -> None:
    """Replace every module, class and function docstring in `tree` with a fixed placeholder."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value.value = _BLANKED_DOCSTRING


def py_shape(src: str) -> str:
    """Return a canonical dump of `src`'s AST with docstrings blanked."""
    tree = ast.parse(src)
    blank_docstrings(tree)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


@dataclass
class _Heredoc:
    """A heredoc whose body starts on the next line."""

    delimiter: str
    strip_tabs: bool


@dataclass
class _Lexer:
    """Line-by-line lexer that tracks comment and literal regions for one dialect."""

    dialect: str
    stack: list[tuple[str, int]] = field(default_factory=lambda: [("code", 0)])
    heredocs: list[_Heredoc] = field(default_factory=list)
    uncertain: str | None = None

    def in_literal(self) -> bool:
        """Return whether the lexer is inside a heredoc body or a string-literal region."""
        return bool(self.heredocs) or any(state in _LITERAL_STATES for state, _ in self.stack)

    def heredoc_line(self, line: str) -> None:
        """Consume one heredoc body line, closing the heredoc on its delimiter line."""
        current = self.heredocs[0]
        if self.dialect == "hcl":
            candidate = line.strip()
        elif current.strip_tabs:
            candidate = line.lstrip("\t")
        else:
            candidate = line
        if candidate == current.delimiter:
            self.heredocs.pop(0)

    def code_line(self, line: str) -> tuple[bool, bool]:
        """Lex one line outside any heredoc body. Returns ``(has_code, has_comment)``."""
        has_code = has_comment = False
        i, n = 0, len(line)
        while i < n:
            state, depth = self.stack[-1]
            if state == "block":
                has_comment = True
                if line[i : i + 2] == "*/":
                    self.stack.pop()
                    i += 2
                else:
                    i += 1
                continue
            if state in ("code", "subst", "interp"):
                step, comment = self._code_char(line, i, state, depth)
                if comment == "line":
                    has_comment = True
                    break
                has_comment = has_comment or comment == "block"
                has_code = has_code or (not comment and not line[i].isspace())
                i += step
                continue
            has_code = has_code or not line[i].isspace()
            i += self._literal_char(line, i, state)
        state = self.stack[-1][0]
        if state in ("sq", "dq") and self.dialect != "sh" and not line.endswith("\\"):
            self.stack.pop()
            self.uncertain = self.uncertain or "a quoted string is not closed on its line"
        return has_code, has_comment

    def _code_char(self, line: str, i: int, state: str, depth: int) -> tuple[int, str]:
        """Lex one code position. Returns ``(chars consumed, comment kind or "")``."""
        char, pair = line[i], line[i : i + 2]
        if char == "\\":
            return 2, ""
        if self.dialect == "sh":
            if char == "#" and (i == 0 or line[i - 1] in _SH_WORD_BREAK):
                return 0, "line"
            if pair == "<<":
                return self._sh_heredoc(line, i), ""
            if pair == "$'":
                self.stack.append(("ansi", 0))
                return 2, ""
            pushes = {"'": "sq", '"': "dq", "`": "bt"}
        else:
            if pair == "//" or (self.dialect == "hcl" and char == "#"):
                return 0, "line"
            if pair == "/*":
                self.stack.append(("block", 0))
                return 2, "block"
            if self.dialect == "hcl" and pair == "<<":
                return self._hcl_heredoc(line, i), ""
            if self.dialect != "hcl" and char == "/":
                return self._js_slash(line, i), ""
            pushes = {'"': "dq"} if self.dialect == "hcl" else {"'": "sq", '"': "dq", "`": "tmpl"}
        if char in pushes:
            self.stack.append((pushes[char], 0))
            return 1, ""
        opener, closer = ("(", ")") if state == "subst" else ("{", "}")
        if state != "code" and char == opener:
            self.stack[-1] = (state, depth + 1)
        elif state != "code" and char == closer:
            if depth == 0:
                self.stack.pop()
            else:
                self.stack[-1] = (state, depth - 1)
        return 1, ""

    def _literal_char(self, line: str, i: int, state: str) -> int:
        """Lex one position inside a string literal and return the chars consumed."""
        char, pair = line[i], line[i : i + 2]
        if char == "\\" and not (state == "sq" and self.dialect == "sh"):
            return 2
        closers = {"sq": "'", "dq": '"', "ansi": "'", "bt": "`", "tmpl": "`"}
        if char == closers[state]:
            self.stack.pop()
            return 1
        if state == "dq" and self.dialect == "hcl" and line[i : i + 3] in ("$${", "%%{"):
            return 3
        if state == "dq" and self.dialect == "sh" and pair == "$(":
            self.stack.append(("subst", 0))
            return 2
        if state == "dq" and self.dialect == "sh" and char == "`":
            self.stack.append(("bt", 0))
            return 1
        interpolates = (state == "tmpl" and pair == "${") or (
            state == "dq" and self.dialect == "hcl" and pair in ("${", "%{")
        )
        if interpolates:
            self.stack.append(("interp", 0))
            return 2
        return 1

    def _sh_heredoc(self, line: str, i: int) -> int:
        """Register the heredoc opened by the ``<<`` at `i` and return the chars consumed."""
        if line[i + 2 : i + 3] == "<":
            return 3
        j = i + 2
        strip_tabs = line[j : j + 1] == "-"
        j += int(strip_tabs)
        while j < len(line) and line[j] in " \t":
            j += 1
        start = j
        while j < len(line) and line[j] not in _SH_WORD_BREAK:
            j += 1
        delimiter = re.sub(r"[\"'\\]", "", line[start:j])
        if not delimiter:
            self.uncertain = self.uncertain or "a `<<` has no heredoc delimiter"
            return 2
        self.heredocs.append(_Heredoc(delimiter, strip_tabs))
        return j - i

    def _hcl_heredoc(self, line: str, i: int) -> int:
        """Register the HCL heredoc opened by the ``<<`` at `i` and return the chars consumed."""
        j = i + 2 + int(line[i + 2 : i + 3] == "-")
        match = _HCL_HEREDOC_ID.match(line, j)
        if match is None:
            self.uncertain = self.uncertain or "a `<<` has no heredoc delimiter"
            return 2
        self.heredocs.append(_Heredoc(match.group(), strip_tabs=False))
        return match.end() - i

    def _js_slash(self, line: str, i: int) -> int:
        """Return the chars consumed by a ``/`` at `i`: a whole regex literal, or one operator."""
        before = line[:i].rstrip()
        is_regex = (
            not before
            or before[-1] in _JS_REGEX_PRECEDERS
            or bool(_JS_REGEX_KEYWORD.search(before))
        )
        if not is_regex:
            return 1
        j, in_class = i + 1, False
        while j < len(line):
            char = line[j]
            if char == "\\":
                j += 2
                continue
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                return j + 1 - i
            j += 1
        return 1


def code_lines(src: str, dialect: str) -> tuple[str, int, str | None]:
    """Return `src` without whole-line comments or blank lines outside literal regions.

    Returns the kept lines, the count of kept lines carrying a trailing comment, and a reason
    string when the literal regions could not be delimited (``None`` otherwise).
    """
    lexer = _Lexer(dialect)
    keep: list[str] = []
    trailing = 0
    for line in src.splitlines():
        if lexer.heredocs:
            lexer.heredoc_line(line)
            keep.append(line.rstrip())
            continue
        starts_in_literal = lexer.in_literal()
        has_code, has_comment = lexer.code_line(line)
        if starts_in_literal:
            keep.append(line.rstrip())
        elif has_code:
            keep.append(line.rstrip())
            trailing += int(has_comment)
    if lexer.heredocs or len(lexer.stack) > 1:
        lexer.uncertain = lexer.uncertain or "a heredoc, string or comment is open at end of file"
    return "\n".join(keep), trailing, lexer.uncertain


def compare_tsx(old: str, new: str, path: str) -> tuple[str, str]:
    """Compare two TSX sources' syntax trees with the TypeScript parser.

    Returns ``(outcome, detail)``; `outcome` is ``"ok"``, ``"fail"`` or ``"unverified"``.
    """
    if not _TYPESCRIPT_PACKAGE.is_dir():
        return "unverified", f"no TypeScript package at {_TYPESCRIPT_PACKAGE}"
    payload = json.dumps({"path": path, "old": old, "new": new})
    try:
        result = proc_run.run(
            ["node", "-e", _TSX_COMPARE_JS, str(_TYPESCRIPT_PACKAGE)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unverified", f"`node` could not run the TypeScript comparison: {exc}"
    try:
        report = json.loads(result.stdout) if result.returncode == 0 else None
    except json.JSONDecodeError:
        report = None
    if not isinstance(report, dict) or not isinstance(report.get("same"), bool):
        stderr = result.stderr.strip().splitlines()
        reason = stderr[-1] if stderr else f"exit {result.returncode}"
        return "unverified", f"the TypeScript comparison did not complete: {reason}"
    if not report["same"]:
        return "fail", "syntax tree CHANGED"
    if report.get("errors") != 0:
        return (
            "unverified",
            "syntax tree identical, but the TypeScript parser reported a syntax error",
        )
    return "ok", "syntax tree identical"


def resolve_extension(path: str, src: str) -> str:
    """Return the extension to key the control on, mapping a shell shebang to ``.sh``."""
    suffix = Path(path).suffix
    if suffix:
        return suffix
    lines = src.splitlines()
    first = lines[0] if lines else ""
    if first.startswith("#!") and "sh" in first:
        return ".sh"
    return suffix


def text_in_index(path: str) -> str | None:
    """Return `path`'s staged content, or None when the index does not carry that path."""
    return text_at_ref("", path)


def check_path(ref: str, path: str, index: bool = False) -> tuple[str, int]:
    """Check one path. Returns (outcome, trailing_unverified).

    `outcome` is ``"skip"``, ``"ok"``, ``"fail"`` or ``"unverified"``.
    """
    old = text_at_ref(ref, path)
    if old is None:
        print(f"SKIP  {path} (not at {ref})")
        return "skip", 0
    new = text_in_index(path) if index else Path(path).read_text(encoding="utf-8")
    if new is None:
        print(f"FAIL  {path} (absent from the index)")
        return "fail", 0
    ext = resolve_extension(path, new)
    if ext == ".py":
        try:
            same = py_shape(old) == py_shape(new)
        except SyntaxError as exc:
            print(f"FAIL  {path} does not parse: {exc}")
            return "fail", 0
        verdict = "identical" if same else "CHANGED"
        print(f"{'OK    ' if same else 'FAIL  '}{path} (code shape {verdict})")
        return ("ok" if same else "fail"), 0
    if ext == ".tsx":
        outcome, detail = compare_tsx(old, new, path)
        label = {"ok": "OK    ", "fail": "FAIL  ", "unverified": "UNVERIFIED "}[outcome]
        print(f"{label}{path} ({detail})")
        return outcome, 0
    if ext in DIALECT:
        old_code, _, old_uncertain = code_lines(old, DIALECT[ext])
        new_code, trailing, new_uncertain = code_lines(new, DIALECT[ext])
        if old_code != new_code:
            print(f"FAIL  {path} (code lines CHANGED)")
            return "fail", trailing
        uncertain = new_uncertain or old_uncertain
        if uncertain:
            print(f"UNVERIFIED {path} (code lines identical, but {uncertain})")
            return "unverified", trailing
        note = f", {trailing} trailing-comment line(s) UNVERIFIED" if trailing else ""
        print(f"OK    {path} (code lines identical){note}")
        return "ok", trailing
    print(f"SKIP  {path} (no control for {ext!r})")
    return "skip", 0


def main(argv: list[str] | None = None) -> int:
    """Check every path against `ref` and report the aggregate."""
    args = sys.argv[1:] if argv is None else argv
    index = bool(args) and args[0] == "--index"
    args = args[1:] if index else args
    if len(args) < 2:
        print(
            "usage: comment_sweep_check.py [--index] <ref> <path>...\n"
            "refusing to report a pass having checked nothing",
            file=sys.stderr,
        )
        return 2
    ref, paths = args[0], args[1:]
    outcomes: list[str] = []
    trailing_total = 0
    for path in paths:
        outcome, trailing = check_path(ref, path, index)
        outcomes.append(outcome)
        trailing_total += trailing
    checked = len(outcomes) - outcomes.count("skip")
    failed, unverified = outcomes.count("fail"), outcomes.count("unverified")
    print(
        f"\nchecked={checked} failed={failed} unverified={unverified} "
        f"trailing-comment-lines-unverified={trailing_total}"
    )
    if failed:
        return 1
    return 3 if unverified else 0


if __name__ == "__main__":
    raise SystemExit(main())
