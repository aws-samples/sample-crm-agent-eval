# SPDX-License-Identifier: MIT
"""`scripts/check_doc_figures.py`: documentation figures are linked, plain-text and self-contained."""

from __future__ import annotations

import base64
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "check_doc_figures.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_figures_for_test", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

_GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
}


def _git(root: Path, *args: str) -> None:
    proc_run.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A scratch git repository with `files` written and tracked."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if files:
        _git(root, "add", "-A")
    return root


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8"/></svg>'
)

_DRAWIO = (
    '<mxfile host="Electron" compressed="false"><diagram id="a" name="Page-1">'
    '<mxGraphModel background="#FFFFFF"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
    '<mxCell id="2" value="AWS Lambda – action handlers" style="rounded=1;html=0;" vertex="1" '
    'parent="1"><mxGeometry x="10" y="10" width="160" height="40" as="geometry"/></mxCell>'
    f'<mxCell id="3" value="" style="shape=image;html=0;image=data:image/svg+xml,{_b64(_ICON)};" '
    'vertex="1" parent="1"><mxGeometry width="8" height="8" as="geometry"/></mxCell>'
    "</root></mxGraphModel></diagram></mxfile>\n"
)


def _svg(
    extra: str = "", label: str = "<text x='80' y='25'>AWS Lambda – action handlers</text>"
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'version="1.1" width="162px" height="42px" viewBox="0 0 162 42">'
        "<!-- https://www.drawio.com -->"
        f'<g><rect width="160" height="40"/><g>{label}</g>'
        f'<image xlink:href="data:image/svg+xml;base64,{_b64(_ICON)}" width="8" height="8"/>'
        f"{extra}</g></svg>\n"
    )


_README = "# Demo\n\n![The user plane on AWS](docs/figures/plane.svg)\n"


def _green() -> dict[str, str]:
    return {
        "README.md": _README,
        "docs/guide.md": "# Guide\n\n![Plane again](figures/plane.svg)\n",
        "docs/figures/plane.drawio": _DRAWIO,
        "docs/figures/plane.svg": _svg(),
        "docs/figures/README.md": "Figures are drawn with draw.io; see https://www.drawio.com.\n",
    }


def _run(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    status = gate.main(["--repo-root", str(root)])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _rules(err: str) -> set[str]:
    """The distinct rule ids reported in the gate's stderr."""
    rules = set()
    for line in err.splitlines():
        if line.startswith("  - "):
            rules.add(
                next(tok for tok in line.split() if tok.startswith("R") and tok[1:].isdigit())
            )
    return rules


def test_the_green_fixture_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A linked, uncompressed, plain-text figure pair with an embedded icon passes."""
    status, out, err = _run(_repo(tmp_path, _green()), capsys)
    assert status == 0, err
    assert "OK — 1 figure pair(s) checked, 2 image link(s)" in out


def _with(changes: dict[str, str | None]) -> dict[str, str]:
    """The green fixture with each path in `changes` replaced, or removed where the value is None."""
    files = _green()
    for path, value in changes.items():
        if value is None:
            files.pop(path)
        else:
            files[path] = value
    return files


def _svg_embed(inner: str, prefix: str = "data:image/svg+xml;base64,") -> str:
    """An `<image>` whose href embeds `inner` as base64 after `prefix`."""
    return f'<image xlink:href="{prefix}{_b64(inner)}"/>'


_SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'
_DRAWIO_PATH = "docs/figures/plane.drawio"
_SVG_PATH = "docs/figures/plane.svg"


def _nested(depth: int) -> str:
    """An SVG embedding itself `depth` times."""
    inner = f"<svg {_SVG_NS}/>"
    for _ in range(depth):
        inner = f'<svg {_SVG_NS}><image href="data:image/svg+xml;base64,{_b64(inner)}"/></svg>'
    return inner


_COMPRESSED = (
    '<mxfile host="Electron"><diagram id="a" name="Page-1">'
    "7ZZNb5swGMc/jY+dgg0kO4Z0bbVq0qRo6tmFJ2DN2Mg4Tfrt99jYJCRdT+12qcQB/P/5ebAfnhcJiI3U/xw7L7cb"
    "</diagram></mxfile>\n"
)
_TWO_MODELS_PLUS_COMPRESSED = _DRAWIO.replace(
    "</root></mxGraphModel></diagram>",
    '</root></mxGraphModel><mxGraphModel/></diagram><diagram id="b">7ZZNb5sw</diagram>',
)

_UNLABELLED_DRAWIO = _DRAWIO.replace('value="AWS Lambda – action handlers"', 'value=""')

#: (rule, reason substring, files): each is the green fixture with one defect.
_CELLS: dict[str, tuple[str, str, dict[str, str]]] = {
    "R1-unresolved": (
        "R1",
        "does not resolve",
        _with({"README.md": _README + "\n![Missing](docs/figures/absent.svg)\n"}),
    ),
    "R1-reference-style": (
        "R1",
        "do not parse",
        _with({"README.md": _README + "\n![Plane][plane]\n\n[plane]: docs/figures/plane.svg\n"}),
    ),
    "R1-nested-brackets": (
        "R1",
        "do not parse",
        _with({"README.md": _README + "\n![a [b]](docs/figures/plane.svg)\n"}),
    ),
    "R1-html-img": (
        "R1",
        "HTML <img>",
        _with({"README.md": _README + '\n<img src="docs/figures/plane.svg" alt="Plane">\n'}),
    ),
    "R1-figures-readme-link": (
        "R1",
        "does not resolve",
        _with({"docs/figures/README.md": "Figures.\n\n![Gone](gone.svg)\n"}),
    ),
    "R2-not-svg": (
        "R2",
        "is not an .svg",
        _with({"README.md": _README + "\n![Source](docs/figures/plane.drawio)\n"}),
    ),
    "R3-no-drawio": ("R3", ".drawio sibling", _with({_DRAWIO_PATH: None})),
    "R4-unreferenced": (
        "R4",
        "referenced by no markdown",
        _with({"README.md": "# Demo\n", "docs/guide.md": "# Guide\n"}),
    ),
    "R5-stray-file": ("R5", "is not an .svg", _with({"docs/figures/notes.txt": "scratch\n"})),
    "R6-deflated-diagram": ("R6", "exactly one <mxGraphModel>", _with({_DRAWIO_PATH: _COMPRESSED})),
    "R6-two-models-plus-compressed": (
        "R6",
        "exactly one <mxGraphModel>",
        _with({_DRAWIO_PATH: _TWO_MODELS_PLUS_COMPRESSED}),
    ),
    "R6-text-beside-model": (
        "R6",
        "exactly one <mxGraphModel>",
        _with(
            {
                _DRAWIO_PATH: _DRAWIO.replace(
                    "</mxGraphModel></diagram>", "</mxGraphModel>eJz</diagram>"
                )
            }
        ),
    ),
    "R6-compressed-attr": (
        "R6",
        'compressed="true"',
        _with({_DRAWIO_PATH: _DRAWIO.replace('"false"', '"true"')}),
    ),
    "R6-html-label": (
        "R6",
        "carries html=1",
        _with({_DRAWIO_PATH: _DRAWIO.replace("rounded=1;html=0", "html=1")}),
    ),
    "R6-whitespace-wrap": (
        "R6",
        "carries whiteSpace=wrap",
        _with(
            {_DRAWIO_PATH: _DRAWIO.replace("rounded=1;html=0", "rounded=1;html=0;whiteSpace=wrap")}
        ),
    ),
    "R6-root-not-mxfile": (
        "R6",
        "not <mxfile>",
        _with(
            {
                _DRAWIO_PATH: _DRAWIO.replace("<mxfile ", "<mxlibrary ").replace(
                    "</mxfile>", "</mxlibrary>"
                )
            }
        ),
    ),
    "R6-unparsable": ("R6", "does not parse", _with({_DRAWIO_PATH: _DRAWIO[:-20]})),
    "R7-foreignObject": ("R7", "<foreignObject>", _with({_SVG_PATH: _svg("<foreignObject/>")})),
    "R7-script": ("R7", "<script>", _with({_SVG_PATH: _svg("<script>alert(1)</script>")})),
    "R7-onload": ("R7", "event attribute", _with({_SVG_PATH: _svg('<rect onload="x()"/>')})),
    "R7-javascript-href": (
        "R7",
        "javascript:",
        _with({_SVG_PATH: _svg('<a xlink:href="javascript:x()"/>')}),
    ),
    "R7-protocol-relative-href": (
        "R7",
        "protocol-relative",
        _with({_SVG_PATH: _svg('<image href="//evil.example/x.png"/>')}),
    ),
    "R7-protocol-relative-url": (
        "R7",
        "protocol-relative",
        _with({_SVG_PATH: _svg('<rect style="fill: url(//evil.example/p)"/>')}),
    ),
    "R7-light-dark": (
        "R7",
        "light-dark(",
        _with({_SVG_PATH: _svg('<rect style="fill: light-dark(#fff, #121212)"/>')}),
    ),
    "R7-no-text": (
        "R7",
        "no <text> element with text",
        _with({_SVG_PATH: _svg(label=""), _DRAWIO_PATH: _UNLABELLED_DRAWIO}),
    ),
    "R7-empty-text": (
        "R7",
        "no <text> element with text",
        _with(
            {
                _SVG_PATH: _svg(label="<text x='1'> </text><text/>"),
                _DRAWIO_PATH: _UNLABELLED_DRAWIO,
            }
        ),
    ),
    "R7-unparsable": ("R7", "does not parse", _with({_SVG_PATH: _svg()[:-10]})),
    "R7-script-in-embedded-svg": (
        "R7",
        "<script> (in an embedded image)",
        _with({_SVG_PATH: _svg(_svg_embed(f"<svg {_SVG_NS}><script/></svg>"))}),
    ),
    "R7-script-behind-quoted-charset": (
        "R7",
        "<script> (in an embedded image)",
        _with(
            {
                _SVG_PATH: _svg(
                    _svg_embed(
                        f"<svg {_SVG_NS}><script/></svg>",
                        "data:image/svg+xml;charset=&quot;utf-8&quot;;base64,",
                    )
                )
            }
        ),
    ),
    "R7-unmatched-embed-params": (
        "R7",
        "cannot be fully decoded",
        _with(
            {
                _SVG_PATH: _svg(
                    _svg_embed(f"<svg {_SVG_NS}/>", "data:image/svg+xml;charset='x';base64,")
                )
            }
        ),
    ),
    "R7-empty-embed": (
        "R7",
        "cannot be fully decoded",
        _with({_SVG_PATH: _svg('<image xlink:href="data:image/svg+xml;base64, "/>')}),
    ),
    "R7-undecodable-embed-in-drawio": (
        "R7",
        "cannot be fully decoded",
        _with({_DRAWIO_PATH: _DRAWIO.replace(f",{_b64(_ICON)};", ",!!!;")}),
    ),
    "R7-embeds-too-deep": (
        "R7",
        "nest deeper than",
        _with({_SVG_PATH: _svg(_svg_embed(_nested(gate.MAX_EMBED_DEPTH + 1)))}),
    ),
    "R8-external-url": (
        "R8",
        "https://example.com/x",
        _with({_SVG_PATH: _svg('<a href="https://example.com/x"/>')}),
    ),
    "R8-url-in-embedded-svg": (
        "R8",
        "https://e.test/i",
        _with(
            {_SVG_PATH: _svg(_svg_embed(f'<svg {_SVG_NS}><image href="https://e.test/i"/></svg>'))}
        ),
    ),
    "R8-url-in-whitespace-split-embed": (
        "R8",
        "https://e.test/split",
        _with(
            {
                _SVG_PATH: _svg(
                    '<image xlink:href="data:image/svg+xml;base64, '
                    + "&#10;".join(
                        re.findall(
                            ".{1,8}",
                            _b64(f'<svg {_SVG_NS}><image href="https://e.test/split"/></svg>'),
                        )
                    )
                    + '"/>'
                )
            }
        ),
    ),
    "R8-url-in-drawio": (
        "R8",
        "http://example.com",
        _with(
            {
                _DRAWIO_PATH: _DRAWIO.replace(
                    'value="AWS Lambda', 'link="http://example.com" value="AWS Lambda'
                )
            }
        ),
    ),
    "R8-host-value-elsewhere": (
        "R8",
        "https://evil.test",
        _with(
            {
                _DRAWIO_PATH: _DRAWIO.replace(
                    'host="Electron"', 'host="https://evil.test"'
                ).replace('value="AWS Lambda', 'link="https://evil.test" value="AWS Lambda')
            }
        ),
    ),
    "R8-remote-image-link": (
        "R8",
        "is not a local file",
        _with({"README.md": _README + "\n![Badge](https://img.example/badge.svg)\n"}),
    ),
    "R9-empty-alt": (
        "R9",
        "empty alt text",
        _with({"README.md": _README.replace("The user plane on AWS", " ")}),
    ),
    "R9-empty-alt-in-figures-readme": (
        "R9",
        "empty alt text",
        _with({"docs/figures/README.md": "Figures.\n\n![](plane.svg)\n"}),
    ),
    "R10-label-edited-without-reexport": (
        "R10",
        "'AWS Lambda – CRM action handlers' is missing from the SVG",
        _with({_DRAWIO_PATH: _DRAWIO.replace("– action handlers", "– CRM action handlers")}),
    ),
    "R10-svg-text-not-in-source": (
        "R10",
        "'Stale label' is not a label in the .drawio source",
        _with({_SVG_PATH: _svg("<text x='1' y='1'>Stale label</text>")}),
    ),
}


@pytest.mark.parametrize("cell", sorted(_CELLS))
def test_each_rule_is_red_for_its_own_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], cell: str
) -> None:
    """One defect in the green fixture fails the gate with exactly its rule, for its reason."""
    rule, reason, files = _CELLS[cell]
    status, _out, err = _run(_repo(tmp_path, files), capsys)
    assert status == 1, err
    assert _rules(err) == {rule}, err
    assert reason in err, err


@pytest.mark.parametrize(
    "link",
    [
        "![Plane](docs/figures/plane.svg 'Title')",
        '![Plane](docs/figures/plane.svg "Title")',
        "![Plane](docs/figures/plane.svg (Title))",
        "![Plane](<docs/figures/plane.svg>)",
        "![Plane](/docs/figures/plane.svg)",
        "Inline code `![](x.png)` and `<img src=x>` is not a link. ![Plane](docs/figures/plane.svg)",
    ],
    ids=[
        "single-quoted-title",
        "double-quoted-title",
        "paren-title",
        "angled",
        "root-relative",
        "inline-code",
    ],
)
def test_accepted_link_forms_resolve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], link: str
) -> None:
    """Titles, angle brackets and root-relative targets parse and resolve; inline code is skipped."""
    status, out, err = _run(_repo(tmp_path, _with({"README.md": f"# Demo\n\n{link}\n"})), capsys)
    assert status == 0, err
    assert "2 image link(s)" in out


def test_a_multi_line_label_matches_one_svg_text_per_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A label broken with `&#10;` is fresh when each line is its own SVG `<text>`, spacing aside."""
    files = _with(
        {
            _DRAWIO_PATH: _DRAWIO.replace(
                'value="AWS Lambda – action handlers"',
                'value="AWS Lambda –&#10;  action   handlers"',
            ),
            _SVG_PATH: _svg(
                label="<text x='1' y='1'>AWS Lambda –</text><text x='1' y='9'>action handlers</text>"
            ),
        }
    )
    status, _out, err = _run(_repo(tmp_path, files), capsys)
    assert status == 0, err


def test_a_link_from_the_figures_readme_counts_as_a_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """docs/figures/README.md is scanned as markdown, so its link satisfies R4."""
    files = _with(
        {
            "README.md": "# Demo\n",
            "docs/guide.md": "# Guide\n",
            "docs/figures/README.md": "Figures.\n\n![Plane](plane.svg)\n",
        }
    )
    status, out, err = _run(_repo(tmp_path, files), capsys)
    assert status == 0, err
    assert "1 image link(s)" in out


def test_the_mxfile_host_attribute_is_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The draw.io `host` attribute is not an R8 violation, even when it is a URL."""
    files = _with(
        {_DRAWIO_PATH: _DRAWIO.replace('host="Electron"', 'host="https://app.diagrams.net"')}
    )
    status, _out, err = _run(_repo(tmp_path, files), capsys)
    assert status == 0, err


def test_a_subdirectory_repo_root_is_an_environment_error(tmp_path: Path) -> None:
    """A --repo-root below the work-tree top would read a partial scope, so the gate exits 2."""
    root = _repo(tmp_path, _green())
    assert gate.main(["--repo-root", str(root / "docs")]) == 2


def test_git_environment_redirection_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIT_DIR, GIT_WORK_TREE and GIT_COMMON_DIR pointing at another repo do not change the subject."""
    root = _repo(tmp_path, _green())
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    for name in ("GIT_DIR", "GIT_COMMON_DIR"):
        monkeypatch.setenv(name, str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    status, out, err = _run(root, capsys)
    assert status == 0, err
    assert "1 figure pair(s)" in out


def test_violations_name_the_markdown_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A link violation is reported as path:line."""
    files = _with({"README.md": _README + "\n![Missing](docs/figures/absent.svg)\n"})
    _status, _out, err = _run(_repo(tmp_path, files), capsys)
    assert "README.md:5: R1" in err


def test_the_allowed_urls_are_exactly_the_namespaces_dtd_and_export_comment() -> None:
    """The R8 allowlist is the named constant, including the SVG 1.1 DTD URL."""
    expected = {
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/1999/xlink",
        "http://www.w3.org/1999/xhtml",
        "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd",
        "https://www.drawio.com",
    }
    assert expected == gate.ALLOWED_URLS


def test_a_url_hidden_behind_xml_escaping_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entity-escaped userinfo trick does not reduce an external URL to an allowed one."""
    files = _with(
        {"docs/figures/plane.svg": _svg('<a href="https://www.drawio.com&amp;@evil.test/"/>')}
    )
    status, _out, err = _run(_repo(tmp_path, files), capsys)
    assert status == 1
    assert "evil.test" in err


def test_an_entity_declaration_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A figure that declares an XML entity fails as unparsable rather than being expanded."""
    hostile = _svg().replace('svg11.dtd">', 'svg11.dtd" [<!ENTITY x "https://evil.test">]>')
    status, _out, err = _run(_repo(tmp_path, _with({"docs/figures/plane.svg": hostile})), capsys)
    assert status == 1
    assert "R7 does not parse" in err


def test_parse_elements_strips_prefixes_and_aggregates_text_into_text_elements() -> None:
    """Local names, parent indices, attributes and `<text>` aggregation are what the rules read."""
    elements = gate.parse_elements(
        '<s:svg xmlns:s="u" a="1"><s:text>a&amp;b<t>c</t>d</s:text><g>e</g></s:svg>'
    )
    assert [(el.name, el.parent, el.attrs) for el in elements] == [
        ("svg", None, {"xmlns:s": "u", "a": "1"}),
        ("text", 0, {}),
        ("t", 1, {}),
        ("g", 0, {}),
    ]
    assert "".join(elements[1].text) == "a&bcd"
    assert "".join(elements[2].text) == "c"
    assert "".join(elements[3].text) == "e"


@pytest.mark.parametrize(
    "document",
    [
        pytest.param("<svg><a></svg>", id="mismatched-tag"),
        pytest.param("<svg>", id="truncated"),
        pytest.param('<!DOCTYPE svg [<!ENTITY x "e">]><svg>&x;</svg>', id="internal-entity"),
        pytest.param(
            '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/hosts">]><svg>&x;</svg>',
            id="external-entity",
        ),
        pytest.param(
            '<!DOCTYPE svg [<!ENTITY % p SYSTEM "file:///etc/hosts"> %p;]><svg/>',
            id="external-parameter-entity",
        ),
    ],
)
def test_parse_elements_raises_value_error(document: str) -> None:
    """Malformed XML and every entity declaration surface as `ValueError`, never another type."""
    with pytest.raises(ValueError):
        gate.parse_elements(document)


def test_an_external_dtd_subset_is_not_fetched(tmp_path: Path) -> None:
    """A DOCTYPE naming an external DTD parses without reading it; reading it would fail here."""
    dtd = tmp_path / "hostile.dtd"
    dtd.write_text('<!ENTITY x "expanded"> this is not a DTD <', encoding="utf-8")
    elements = gate.parse_elements(
        f'<!DOCTYPE svg PUBLIC "-//X//EN" "{dtd.as_uri()}"><svg>plain</svg>'
    )
    assert [(el.name, "".join(el.text)) for el in elements] == [("svg", "plain")]


def test_a_link_in_fenced_code_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Image links inside ``` and ~~~ fences are not links."""
    fenced = "\n```md\n![](docs/figures/absent.png)\n```\n\n~~~\n![](https://x.test/a.png)\n~~~\n"
    status, out, err = _run(_repo(tmp_path, _with({"README.md": _README + fenced})), capsys)
    assert status == 0, err
    assert "2 image link(s)" in out


def test_untracked_files_are_not_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An untracked stray file under docs/figures/ is outside the scope."""
    root = _repo(tmp_path, _green())
    (root / "docs" / "figures" / "scratch.png").write_bytes(b"\x89PNG")
    status, _out, err = _run(root, capsys)
    assert status == 0, err


def test_an_empty_scope_skips_with_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No tracked image link and no tracked figure means nothing was verified: exit 3."""
    root = _repo(tmp_path, {"README.md": "# Demo\n", "docs/figures/README.md": "Figures.\n"})
    status, out, _err = _run(root, capsys)
    assert status == gate.SKIP_EXIT_STATUS == 3
    assert "[SKIP]" in out


def test_a_non_git_directory_is_an_environment_error(tmp_path: Path) -> None:
    """Without a git index the scope is unknown, so the gate exits 2."""
    assert gate.main(["--repo-root", str(tmp_path)]) == 2


#: The documentation figure pairs the repository is known to track.
EXPECTED_MIN_FIGURE_PAIRS = 12


def test_the_current_tree_is_green_with_every_figure_pair(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The repository passes, checking at least `EXPECTED_MIN_FIGURE_PAIRS` figure pairs."""
    status, out, err = _run(REPO_ROOT, capsys)
    assert status == 0, err
    pairs = int(out.split("OK — ", 1)[1].split(" ", 1)[0])
    assert pairs >= EXPECTED_MIN_FIGURE_PAIRS, out


def test_the_skip_status_matches_run_gates() -> None:
    """The gate's skip status is the one `run_gates.py` renders as [SKIP], and it is registered."""
    spec = importlib.util.spec_from_file_location(
        "run_gates_for_doc_figures_test", REPO_ROOT / "scripts" / "run_gates.py"
    )
    assert spec is not None and spec.loader is not None
    run_gates = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_gates)
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS
    assert ("check_doc_figures", [sys.executable, "scripts/check_doc_figures.py"]) in [
        (name, argv) for name, argv in run_gates.GATES
    ]
