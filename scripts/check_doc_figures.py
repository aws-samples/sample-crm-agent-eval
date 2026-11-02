#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: documentation figures are tracked, linked, plain-text draw.io sources with safe SVG exports.

Reads the tracked `README.md`, `docs/**/*.md` (including `docs/figures/README.md`) and
`docs/figures/*` (`git ls-files`). Image links are `![alt](path)` or `![alt](path "title")` outside
fenced code blocks and inline code spans. Each violation is reported as `path[:line]: R<n> message`:

- R1: a local image link does not resolve to a tracked file under `docs/figures/` (a target
  starting with `/` resolves from the repository root, any other from its markdown file's
  directory); or a `![` that does not parse as an inline image link (reference-style, nested
  brackets), or an HTML `<img>`, appears in markdown;
- R2: a local image link does not name an `.svg`;
- R3: a `docs/figures/*.svg` has no tracked `.drawio` sibling, or a `.drawio` has no `.svg`;
- R4: a tracked `docs/figures/*.svg` is linked from no markdown file;
- R5: a tracked file under `docs/figures/` is neither `.svg`, `.drawio` nor `docs/figures/README.md`;
- R6: a `.drawio` does not parse; its root is not `<mxfile>`; it has no `<diagram>`; an element
  carries `compressed="true"`; a `<diagram>` does not hold exactly one child `<mxGraphModel>` and
  no other content (compressed content); or any element's style carries `html=1` or
  `whiteSpace=wrap`;
- R7: an `.svg` does not parse; it, or an embedded `data:image/svg+xml` image, contains
  `<foreignObject>`, `<script>`, an `on*=` event attribute, `javascript:`, a protocol-relative
  (`//`) `href`, `src` or `url()`, or `light-dark(`; or it has no `<text>` element with non-blank
  text. In a `.svg` or a `.drawio`, an embedded `data:image/svg+xml` image that cannot be fully
  decoded, or embeds nested deeper than `MAX_EMBED_DEPTH`, are R7 too;
- R8: a `.drawio` or `.svg` (embedded images included) contains an `http://` or `https://` URL
  outside `ALLOWED_URLS`, the `<mxfile>` `host` attribute value excepted; or a markdown image link
  has a scheme;
- R9: a markdown image link has empty alt text;
- R10: an `.svg` is stale against its `.drawio`: a non-empty line of an `<mxCell>` `value` (XML-
  unescaped, whitespace-normalised) is not the text of any SVG `<text>` element, or an SVG `<text>`
  line is not among the source's label lines. R10 is not evaluated against a `.drawio` that fails
  R6.

Usage: `python3 scripts/check_doc_figures.py [--repo-root DIR]`

Exit codes: 0 pass; 1 a violation, or a file in scope cannot be read; 2 usage or environment error
(not the top of a git work tree, git unavailable); 3 SKIP: no tracked image link and no tracked
figure, so nothing was verified. `scripts/run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html
import os
import posixpath
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Protocol

import defusedxml
import defusedxml.expatreader

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS = 3

#: The `git ls-files` pathspecs that make up the scope.
SCOPE_PATHSPECS: tuple[str, ...] = ("README.md", "docs/*.md", "docs/**/*.md", "docs/figures/*")

#: The directory every linked figure must live in.
FIGURES_DIR = "docs/figures/"

#: The one non-figure file allowed under `FIGURES_DIR`.
FIGURES_README = "docs/figures/README.md"

#: Git environment variables that would redirect `git -C` away from `--repo-root`.
GIT_ENV_SCRUB: frozenset[str] = frozenset(
    {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR"}
)

#: The only `http://` / `https://` URLs a figure may contain.
ALLOWED_URLS: frozenset[str] = frozenset(
    {
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/1999/xlink",
        "http://www.w3.org/1999/xhtml",
        "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd",
        "https://www.drawio.com",
    }
)

#: The deepest chain of images embedded in images that is still inspected.
MAX_EMBED_DEPTH = 4

#: `.drawio` style entries that fail R6.
FORBIDDEN_STYLE_ENTRIES: frozenset[str] = frozenset({"html=1", "whiteSpace=wrap"})

_IMAGE_LINK_RE = re.compile(
    r"!\[(?P<alt>[^\[\]]*)\]\(\s*(?:<(?P<angled>[^<>\n]*)>|(?P<bare>[^\s()<>]+))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)"
)
_INLINE_CODE_RE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
_IMG_TAG_RE = re.compile(r"<img\b", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_URL_RE = re.compile(r"https?://[^\s\"'<>()]+", re.IGNORECASE)
_MXFILE_HOST_RE = re.compile(r"(<mxfile\b[^>]*?\shost\s*=\s*)(\"[^\"]*\"|'[^']*')")
_EMBED_START_RE = re.compile(r"data:image/svg\+xml", re.IGNORECASE)
_EMBED_RE = re.compile(
    r"data:image/svg\+xml(?P<params>(?:;[\w.+-]+(?:=(?:[\w.+-]+|\"[^\"]*\"))?)*),"
    r"(?P<payload>[A-Za-z0-9+/=%\s]*)",
    re.IGNORECASE,
)
_EMBED_END = frozenset({"", '"', "'", ";", ")"})

#: Unsafe SVG content (R7), each a (description, pattern) pair.
_UNSAFE_SVG: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("<foreignObject>", re.compile(r"<\s*(?:[\w.-]+:)?foreignObject\b", re.IGNORECASE)),
    ("<script>", re.compile(r"<\s*(?:[\w.-]+:)?script\b", re.IGNORECASE)),
    ("an on* event attribute", re.compile(r"[\s\"'/]on[a-z]+\s*=", re.IGNORECASE)),
    ("javascript:", re.compile(r"javascript\s*:", re.IGNORECASE)),
    (
        "a protocol-relative reference",
        re.compile(r"(?:\b(?:href|src)\s*=\s*[\"']\s*|url\(\s*[\"']?\s*)//", re.IGNORECASE),
    ),
    ("light-dark(", re.compile(r"light-dark\(", re.IGNORECASE)),
)


class ScopeError(Exception):
    """The scope could not be listed."""


@dataclass(frozen=True)
class ImageLink:
    """One `![alt](target)` in a markdown file, outside fenced and inline code."""

    source: str
    line: int
    alt: str
    target: str


def tracked_scope(repo_root: Path) -> list[str]:
    """The tracked paths matching :data:`SCOPE_PATHSPECS`, repository-relative, in git's order.

    Raises :class:`ScopeError` when `repo_root` is not the top of a git work tree or git cannot run.
    """
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_SCRUB}

    def git(*args: str) -> bytes:
        try:
            result = proc_run.run(
                ["git", "-C", str(repo_root), *args], capture_output=True, check=False, env=env
            )
        except OSError as exc:
            raise ScopeError(f"could not run git: {exc}") from exc
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise ScopeError(f"git {args[0]} exited {result.returncode}: {stderr[:400]}")
        return bytes(result.stdout)

    toplevel = Path(os.fsdecode(git("rev-parse", "--show-toplevel").strip()))
    if toplevel.resolve() != repo_root.resolve():
        raise ScopeError(f"{repo_root} is not the top of its work tree ({toplevel})")
    listed = git("ls-files", "-z", "--", *SCOPE_PATHSPECS)
    return [os.fsdecode(raw) for raw in listed.split(b"\0") if raw]


def scan_markdown(text: str, source: str) -> tuple[list[ImageLink], list[str]]:
    """The image links in markdown `text`, and R1 violations for image syntax that does not parse.

    Fenced code blocks (backtick and tilde) and inline code spans are skipped.
    """
    links: list[ImageLink] = []
    violations: list[str] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        opener = _FENCE_RE.match(line)
        if fence is not None:
            marker = opener.group(1) if opener else ""
            if marker[:1] == fence[0] and len(marker) >= len(fence) and line.strip() == marker:
                fence = None
            continue
        if opener:
            fence = opener.group(1)
            continue
        prose = _INLINE_CODE_RE.sub("", line)
        parsed_at: set[int] = set()
        for match in _IMAGE_LINK_RE.finditer(prose):
            parsed_at.add(match.start())
            target = match["angled"] if match["angled"] is not None else match["bare"]
            links.append(ImageLink(source, number, match["alt"], target))
        where = f"{source}:{number}"
        unparsed = [m.start() for m in re.finditer(r"!\[", prose) if m.start() not in parsed_at]
        if unparsed:
            violations.append(
                f"{where}: R1 {len(unparsed)} image reference(s) do not parse as ![alt](path), "
                "so they are unverified"
            )
        if _IMG_TAG_RE.search(prose):
            violations.append(f"{where}: R1 an HTML <img> is not allowed; use ![alt](path)")
    return links, violations


def resolve_link(source: str, target: str) -> str | None:
    """The repository-relative path a local link `target` in `source` names, or `None` if outside.

    A target starting with `/` resolves from the repository root, any other from `source`'s
    directory.
    """
    path = urllib.parse.unquote(_link_path(target))
    if path.startswith("/"):
        joined = path.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(source), path)
    normal = posixpath.normpath(joined)
    if normal == ".." or normal.startswith("../") or normal.startswith("/"):
        return None
    return normal


def _link_path(target: str) -> str:
    return target.split("#", 1)[0].split("?", 1)[0]


def _decode_embed(payload: str, is_base64: bool) -> str | None:
    """The SVG text of one embedded payload, or `None` if it does not decode to markup.

    Whitespace inside the payload is ignored. A payload without `;base64` (draw.io style syntax)
    is tried as base64, then as percent-encoding.
    """
    compact = re.sub(r"\s+", "", payload)
    if not compact:
        return None
    try:
        text: str | None = base64.b64decode(urllib.parse.unquote(compact), validate=True).decode(
            "utf-8"
        )
    except (binascii.Error, ValueError):
        text = None if is_base64 else urllib.parse.unquote(compact)
    return text if text is not None and "<" in text else None


def embedded_svgs(text: str, path: str) -> tuple[list[str], list[str]]:
    """Every decoded `data:image/svg+xml` image in `text`, recursively, and R7 decode failures.

    Every occurrence of `data:image/svg+xml` must decode completely; one that does not is an R7
    failure, never skipped.
    """
    decoded: list[str] = []
    failures: list[str] = []
    frontier = [text]
    for _depth in range(MAX_EMBED_DEPTH + 1):
        found: set[str] = set()
        for body in frontier:
            unescaped = html.unescape(body)
            for start in _EMBED_START_RE.finditer(unescaped):
                match = _EMBED_RE.match(unescaped, start.start())
                after = unescaped[match.end() : match.end() + 1] if match else ""
                result = (
                    _decode_embed(match["payload"], ";base64" in match["params"].lower())
                    if match and after in _EMBED_END
                    else None
                )
                if result is None:
                    failures.append(
                        f"{path}: R7 an embedded data:image/svg+xml image cannot be fully "
                        "decoded, so it is unverified"
                    )
                else:
                    found.add(result)
        if not found:
            return decoded, failures
        decoded += sorted(found)
        frontier = sorted(found)
    failures.append(f"{path}: R7 embedded images nest deeper than {MAX_EMBED_DEPTH}")
    return decoded, failures


@dataclass
class Element:
    """One parsed XML element: local name, attributes, parent index and direct character data."""

    name: str
    attrs: dict[str, str]
    parent: int | None
    text: list[str] = field(default_factory=list)


def parse_elements(text: str) -> list[Element]:
    """Every element of XML document `text`, in document order.

    `Element.text` holds the element's direct character data, except that a `<text>` element also
    collects the character data of its descendants. Entity declarations are refused and no external
    entity is fetched. Raises `ValueError` when `text` is not well-formed or declares an entity.
    """
    collector = _ElementCollector()
    parser = defusedxml.expatreader.create_parser(forbid_entities=True, forbid_external=False)
    parser.setFeature(_SAX_EXTERNAL_GENERAL_ENTITIES, False)
    parser.setContentHandler(collector)
    parser.setErrorHandler(_RaiseValueError())
    try:
        parser.feed(text)
        parser.close()
    except defusedxml.DefusedXmlException as exc:
        raise ValueError(f"entity declarations are not allowed: {exc}") from exc
    return collector.elements


_SAX_EXTERNAL_GENERAL_ENTITIES = "http://xml.org/sax/features/external-general-entities"


class _RaiseValueError:
    """SAX error handler that turns every parse error and warning into a `ValueError`."""

    def error(self, exception: BaseException) -> NoReturn:
        raise ValueError(str(exception)) from exception

    def fatalError(self, exception: BaseException) -> NoReturn:  # noqa: N802
        raise ValueError(str(exception)) from exception

    def warning(self, exception: BaseException) -> None:
        raise ValueError(str(exception)) from exception


class _Attributes(Protocol):
    def items(self) -> list[tuple[str, str]]: ...


class _ElementCollector:
    """SAX content handler that builds the `parse_elements` element list."""

    def __init__(self) -> None:
        self.elements: list[Element] = []
        self._stack: list[int] = []

    def startElement(self, name: str, attrs: _Attributes) -> None:  # noqa: N802
        parent = self._stack[-1] if self._stack else None
        self.elements.append(Element(name.rsplit(":", 1)[-1], dict(attrs.items()), parent))
        self._stack.append(len(self.elements) - 1)

    def endElement(self, _name: str) -> None:  # noqa: N802
        self._stack.pop()

    def characters(self, content: str) -> None:
        if not self._stack:
            return
        self.elements[self._stack[-1]].text.append(content)
        owner = next(
            (i for i in reversed(self._stack[:-1]) if self.elements[i].name == "text"), None
        )
        if owner is not None:
            self.elements[owner].text.append(content)

    def setDocumentLocator(self, locator: object) -> None:  # noqa: N802
        pass

    def startDocument(self) -> None:  # noqa: N802
        pass

    def endDocument(self) -> None:  # noqa: N802
        pass

    def startPrefixMapping(self, prefix: str | None, uri: str) -> None:  # noqa: N802
        pass

    def endPrefixMapping(self, prefix: str | None) -> None:  # noqa: N802
        pass

    def startElementNS(  # noqa: N802
        self, name: tuple[str | None, str], qname: str | None, attrs: object
    ) -> None:
        pass

    def endElementNS(self, name: tuple[str | None, str], qname: str | None) -> None:  # noqa: N802
        pass

    def ignorableWhitespace(self, whitespace: str) -> None:  # noqa: N802
        pass

    def processingInstruction(self, target: str, data: str) -> None:  # noqa: N802
        pass

    def skippedEntity(self, name: str) -> None:  # noqa: N802
        pass


def decoded_views(body: str) -> list[str]:
    """`body` as written, XML-unescaped, and XML-unescaped then percent-decoded."""
    unescaped = html.unescape(body)
    return [body, unescaped, urllib.parse.unquote(unescaped)]


def url_violations(path: str, bodies: list[str]) -> list[str]:
    """R8 violations for every disallowed URL in `bodies` (a file's text plus its embeds)."""
    seen = {url for body in bodies for view in decoded_views(body) for url in _URL_RE.findall(view)}
    return [f"{path}: R8 contains the external URL {url}" for url in sorted(seen - ALLOWED_URLS)]


def unsafe_svg_violations(path: str, bodies: list[tuple[str, str]]) -> list[str]:
    """R7 violations for unsafe content in each `(where, body)` of an SVG and its embeds."""
    violations: list[str] = []
    for where, body in bodies:
        views = decoded_views(body)
        violations += [
            f"{path}: R7 contains {label}{where}"
            for label, pattern in _UNSAFE_SVG
            if any(pattern.search(view) for view in views)
        ]
    return violations


def check_drawio(path: str, text: str) -> list[str]:
    """R6, R7 (embedded images) and R8 violations in one `.drawio` source."""
    try:
        elements = parse_elements(text)
    except ValueError as exc:
        return [f"{path}: R6 does not parse as XML ({exc}), so it is unverified"]
    violations: list[str] = []
    if elements[0].name != "mxfile":
        violations.append(f"{path}: R6 root element is <{elements[0].name}>, not <mxfile>")
    diagrams = [i for i, el in enumerate(elements) if el.name == "diagram"]
    if not diagrams:
        violations.append(f"{path}: R6 has no <diagram>")
    for index in diagrams:
        children = [el.name for el in elements if el.parent == index]
        content = "".join(elements[index].text).strip()
        if children != ["mxGraphModel"] or content:
            violations.append(
                f"{path}: R6 <diagram id={elements[index].attrs.get('id')!r}> does not hold exactly "
                "one <mxGraphModel> and nothing else (compressed content)"
            )
    for el in elements:
        if el.attrs.get("compressed", "").lower() == "true":
            violations.append(f'{path}: R6 <{el.name}> is compressed="true"')
        entries = {part.strip() for part in el.attrs.get("style", "").split(";")}
        violations += [
            f"{path}: R6 <{el.name} id={el.attrs.get('id')!r}> style carries {entry}"
            for entry in sorted(entries & FORBIDDEN_STYLE_ENTRIES)
        ]
    scanned = _MXFILE_HOST_RE.sub(r'\1""', text, count=1) if elements[0].name == "mxfile" else text
    embeds, failures = embedded_svgs(scanned, path)
    return violations + failures + url_violations(path, [scanned, *embeds])


def _normalised(text: str) -> str:
    return " ".join(text.split())


def freshness_violations(path: str, svg_text: str, drawio_text: str) -> list[str]:
    """R10 violations where the labels of `.svg` `path` and its `.drawio` source disagree.

    Returns nothing when either file does not parse; R6 and R7 report that. The caller skips R10
    for a source that fails R6.
    """
    try:
        source = parse_elements(drawio_text)
        export = parse_elements(svg_text)
    except ValueError:
        return []
    labels = {
        line
        for el in source
        if el.name == "mxCell"
        for line in map(_normalised, el.attrs.get("value", "").split("\n"))
        if line
    }
    rendered = {
        line for el in export if el.name == "text" if (line := _normalised("".join(el.text)))
    }
    violations = [
        f"{path}: R10 source label line {line!r} is missing from the SVG; re-export it"
        for line in sorted(labels - rendered)
    ]
    violations += [
        f"{path}: R10 SVG text {line!r} is not a label in the .drawio source; re-export it"
        for line in sorted(rendered - labels)
    ]
    return violations


def check_svg(path: str, text: str) -> list[str]:
    """R7 and R8 violations in one exported `.svg`."""
    try:
        elements = parse_elements(text)
    except ValueError as exc:
        return [f"{path}: R7 does not parse as XML ({exc}), so it is unverified"]
    embeds, failures = embedded_svgs(text, path)
    bodies = [("", text)] + [(" (in an embedded image)", embed) for embed in embeds]
    violations = unsafe_svg_violations(path, bodies)
    if not any(el.name == "text" and "".join(el.text).strip() for el in elements):
        violations.append(
            f"{path}: R7 has no <text> element with text, so labels are not real text"
        )
    return violations + failures + url_violations(path, [text, *embeds])


@dataclass(frozen=True)
class Report:
    """The outcome of one run: violations, image links seen, figures seen, and complete pairs."""

    violations: list[str]
    links: int
    figures: int
    pairs: int


def check(repo_root: Path, scope: list[str]) -> Report:
    """Apply R1-R10 to the tracked `scope` read from the work tree at `repo_root`."""
    tracked = set(scope)
    markdown = [p for p in scope if p.endswith(".md")]
    figures = [p for p in scope if p.startswith(FIGURES_DIR) and p != FIGURES_README]
    violations: list[str] = []
    texts: dict[str, str] = {}

    for relative in markdown + figures:
        if relative.endswith((".md", ".svg", ".drawio")):
            try:
                texts[relative] = (repo_root / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                violations.append(f"{relative}: cannot be read ({exc}), so it is unverified")

    links: list[ImageLink] = []
    for relative in markdown:
        if relative in texts:
            found, unparsed = scan_markdown(texts[relative], relative)
            links += found
            violations += unparsed

    referenced: set[str] = set()
    for link in links:
        where = f"{link.source}:{link.line}"
        if not link.alt.strip():
            violations.append(f"{where}: R9 image link to {link.target!r} has empty alt text")
        if _SCHEME_RE.match(link.target):
            violations.append(f"{where}: R8 image link {link.target[:80]!r} is not a local file")
            continue
        resolved = resolve_link(link.source, link.target)
        if resolved is None or resolved not in tracked or not resolved.startswith(FIGURES_DIR):
            violations.append(
                f"{where}: R1 image link {link.target!r} does not resolve to a tracked file "
                f"under {FIGURES_DIR}"
            )
        else:
            referenced.add(resolved)
        if not _link_path(link.target).lower().endswith(".svg"):
            violations.append(f"{where}: R2 image link {link.target!r} is not an .svg")

    drawio_results = {
        relative: check_drawio(relative, texts[relative])
        for relative in figures
        if relative.endswith(".drawio") and relative in texts
    }
    pairs = 0
    for relative in figures:
        stem, suffix = posixpath.splitext(relative)
        name = posixpath.basename(stem)
        if suffix == ".svg":
            if f"{stem}.drawio" in tracked:
                pairs += 1
            else:
                violations.append(f"{relative}: R3 has no tracked {name}.drawio sibling")
            if relative not in referenced:
                violations.append(f"{relative}: R4 is referenced by no markdown file")
            if relative in texts:
                violations += check_svg(relative, texts[relative])
                if f"{stem}.drawio" in texts and not drawio_results[f"{stem}.drawio"]:
                    violations += freshness_violations(
                        relative, texts[relative], texts[f"{stem}.drawio"]
                    )
        elif suffix == ".drawio":
            if f"{stem}.svg" not in tracked:
                violations.append(f"{relative}: R3 has no tracked {name}.svg sibling")
            violations += drawio_results.get(relative, [])
        else:
            violations.append(f"{relative}: R5 is not an .svg, .drawio or {FIGURES_README}")

    return Report(violations, len(links), len(figures), pairs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="The top of the git work tree to check (default: this repository).",
    )
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root

    try:
        scope = tracked_scope(repo_root)
    except ScopeError as exc:
        print(f"check_doc_figures: ERROR — {exc}", file=sys.stderr)
        return 2

    report = check(repo_root, scope)
    if report.violations:
        print("check_doc_figures: VIOLATION(S) FOUND:", file=sys.stderr)
        for violation in report.violations:
            print(f"  - {violation}", file=sys.stderr)
        print("check_doc_figures: FAILED", file=sys.stderr)
        return 1
    if not report.links and not report.figures:
        print(
            f"[SKIP] check_doc_figures: no tracked image link and no tracked figure in "
            f"{repo_root}. Nothing was verified."
        )
        return SKIP_EXIT_STATUS
    print(
        f"check_doc_figures: OK — {report.pairs} figure pair(s) checked, {report.links} image "
        f"link(s) resolve to tracked SVGs under {FIGURES_DIR}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
