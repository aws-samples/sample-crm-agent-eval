# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_emitted_css.py`.

Tests that compile the real stylesheet need the frontend's Tailwind toolchain (`@tailwindcss/postcss`
and `node`). They skip when it is
absent unless `CRM_REQUIRE_EMITTED_CSS=1`, in which case they fail instead.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_emitted_css.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_emitted_css", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

REQUIRE_TOOLCHAIN_ENV_VAR = "CRM_REQUIRE_EMITTED_CSS"

#: The path parts of the plugin directory and entry file `build_stylesheet` requires.
_TAILWIND_PLUGIN_PARTS = ("node_modules", "@tailwindcss", "postcss")
_TAILWIND_ENTRY_PARTS = ("src", "styles", "tailwind.css")

#: The exact wording of the `node`-on-PATH clause.
_NODE_ABSENT_CLAUSE = "`node` is not on PATH"

#: The number of environmental early-exits `build_stylesheet` has.
_EXPECTED_BUILD_BAILOUTS = 3


def stylesheet_toolchain_unavailable_reason(frontend: Path = gate.FRONTEND) -> str | None:
    """``None`` when `build_stylesheet` can compile; else every missing precondition, joined.

    Checks the `@tailwindcss/postcss` plugin, `node` on PATH, and the Tailwind entry point.
    """
    missing: list[str] = []
    binary = frontend.joinpath(*_TAILWIND_PLUGIN_PARTS)
    if not binary.exists():
        missing.append(f"no `@tailwindcss/postcss` at {binary} — run `cd frontend && npm ci`")
    if shutil.which("node") is None:
        missing.append(_NODE_ABSENT_CLAUSE)
    entry = frontend.joinpath(*_TAILWIND_ENTRY_PARTS)
    if not entry.exists():
        missing.append(f"no Tailwind entry point at {entry}")
    return "; ".join(missing) if missing else None


def _require_toolchain() -> bool:
    """Whether an absent toolchain must fail rather than skip."""
    return os.environ.get(REQUIRE_TOOLCHAIN_ENV_VAR) == "1"


def _skip_or_fail_without_toolchain(frontend: Path = gate.FRONTEND) -> None:
    reason = stylesheet_toolchain_unavailable_reason(frontend)
    if reason is None:
        return
    message = f"cannot compile the frontend stylesheet: {reason}"
    if _require_toolchain():
        pytest.fail(f"{message} (and {REQUIRE_TOOLCHAIN_ENV_VAR}=1 forbids skipping)")
    pytest.skip(message)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(GATE), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def stylesheet(tmp_path: Path) -> Path:
    """A stand-in for Tailwind's output, including the escape forms the real one emits."""
    css = tmp_path / "built.css"
    css.write_text(
        "\n".join(
            [
                ".flex { display: flex }",
                ".h-control-lg { height: 3rem }",
                ".h-control-sm { height: 2rem }",
                # Variant-prefixed, exactly as Tailwind escapes them.
                ".sm\\:grid-cols-2 { grid-template-columns: repeat(2, 1fr) }",
                ".hover\\:bg-surface-hover:hover { background: red }",
                # The arbitrary-variant form whose bare sibling emits nothing.
                ".\\[\\&\\[hidden\\]\\]\\:hidden[hidden] { display: none }",
                # Numerically-escaped comma, exactly as Tailwind writes an arbitrary value.
                ".sm\\:grid-cols-\\[minmax\\(0\\2c 10rem\\)_1fr\\] { display: grid }",
                # `@keyframes` are emitted unconditionally by tailwindcss-animate, so a stylesheet
                # being non-empty is not evidence that any utility compiled.
                "@keyframes spin { to { transform: rotate(360deg) } }",
            ]
        ),
        encoding="utf-8",
    )
    return css


def _source(root: Path, relpath: str, class_value: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'export function C() {{ return <div className="{class_value}" />; }}\n', encoding="utf-8"
    )


class TestRealTree:
    def test_the_real_frontend_passes(self) -> None:
        """The gate passes on the real frontend tree."""
        _skip_or_fail_without_toolchain()
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert "check_emitted_css: OK" in result.stdout

    def test_it_reports_how_many_tokens_it_actually_checked(self) -> None:
        """The JSON output reports the token and selector counts it checked."""
        _skip_or_fail_without_toolchain()
        result = _run("--json")
        payload = json.loads(result.stdout)
        assert payload["tokens"] > 50, payload
        assert payload["defined_selectors"] > 100, payload
        assert payload["unresolved"] == {}, payload

    def test_it_states_its_coverage_limit(self) -> None:
        _skip_or_fail_without_toolchain()
        result = _run()
        assert "coverage limit" in result.stdout
        assert "cn()" in result.stdout


class TestDetection:
    def test_a_class_with_no_rule_fails(self, tmp_path: Path, stylesheet: Path) -> None:
        root = tmp_path / "src"
        _source(root, "Widget.tsx", "flex max-w-panel")
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 1, result.stdout
        assert "max-w-panel" in result.stdout
        assert "Widget.tsx:1" in result.stdout

    def test_the_inert_guard_form_is_caught_and_the_working_one_is_not(
        self, tmp_path: Path, stylesheet: Path
    ) -> None:
        """`[hidden]:hidden` is reported unresolved and `[&[hidden]]:hidden` resolves."""
        root = tmp_path / "src"
        _source(root, "Inert.tsx", "flex [hidden]:hidden")
        inert = _run("--root", str(root), "--css", str(stylesheet))
        assert inert.returncode == 1
        assert "[hidden]:hidden" in inert.stdout

        _source(root, "Inert.tsx", "flex [&[hidden]]:hidden")
        working = _run("--root", str(root), "--css", str(stylesheet))
        assert working.returncode == 0, working.stdout

    def test_a_variant_prefixed_class_resolves(self, tmp_path: Path, stylesheet: Path) -> None:
        """A variant-prefixed class resolves against its escaped-colon selector."""
        root = tmp_path / "src"
        _source(root, "Responsive.tsx", "sm:grid-cols-2 hover:bg-surface-hover")
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout

    def test_a_strict_prefix_of_a_real_class_is_still_unresolved(
        self, tmp_path: Path, stylesheet: Path
    ) -> None:
        """A strict prefix of a defined class is still reported unresolved."""
        root = tmp_path / "src"
        _source(root, "Skeletonish.tsx", "h-control flex")
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 1, result.stdout
        assert "h-control" in result.stdout

    def test_a_hand_written_plain_css_class_resolves(
        self, tmp_path: Path, stylesheet: Path
    ) -> None:
        """A class defined by a hand-written stylesheet rather than by Tailwind resolves."""
        root = tmp_path / "src"
        _source(root, "Hidden.tsx", "flex visually-hidden")
        (root / "index.css").write_text(
            ".visually-hidden { position: absolute }\n", encoding="utf-8"
        )
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout


class TestAntiVacuity:
    def test_class_attributes_present_but_no_tokens_extracted_fails(self, tmp_path: Path) -> None:
        """Class attributes present with no extracted token report "verified nothing" and fail."""
        # Passes no `--css`, so it compiles the real toolchain over a fixture tree.
        _skip_or_fail_without_toolchain()
        root = tmp_path / "src"
        path = root / "Composed.tsx"
        path.parent.mkdir(parents=True)
        # An interpolated template literal: the extractor reads no token from it.
        path.write_text(
            "export function C() { return <div className={`flex ${size}`} />; }\n", encoding="utf-8"
        )
        result = _run("--root", str(root))
        assert result.returncode == 1, result.stdout
        assert "verified nothing" in result.stdout

    def test_an_unbuildable_stylesheet_skips_rather_than_passes(self, tmp_path: Path) -> None:
        root = tmp_path / "src"
        _source(root, "Widget.tsx", "flex")
        result = _run("--root", str(root), "--css", str(tmp_path / "absent.css"))
        assert result.returncode == gate.SKIP_EXIT_STATUS, result.stdout
        assert "Nothing was verified" in result.stdout

    def test_a_stylesheet_with_no_class_selectors_skips(self, tmp_path: Path) -> None:
        """A stylesheet holding only `@keyframes` exits with the skip status."""
        root = tmp_path / "src"
        _source(root, "Widget.tsx", "flex")
        empty = tmp_path / "keyframes-only.css"
        empty.write_text("@keyframes spin { to { transform: rotate(360deg) } }\n", encoding="utf-8")
        result = _run("--root", str(root), "--css", str(empty))
        assert result.returncode == gate.SKIP_EXIT_STATUS, result.stdout

    def test_a_missing_root_skips(self, tmp_path: Path) -> None:
        result = _run("--root", str(tmp_path / "nope"))
        assert result.returncode == gate.SKIP_EXIT_STATUS

    def test_no_component_source_skips(self, tmp_path: Path) -> None:
        root = tmp_path / "src"
        root.mkdir()
        result = _run("--root", str(root))
        assert result.returncode == gate.SKIP_EXIT_STATUS


class TestConventions:
    def test_skip_status_matches_the_runner(self) -> None:
        runner = (REPO_ROOT / "scripts" / "run_gates.py").read_text(encoding="utf-8")
        assert "GATE_SKIP_EXIT_STATUS = 3" in runner
        assert gate.SKIP_EXIT_STATUS == 3

    def test_it_pins_no_file_by_name(self) -> None:
        """The gate discovers sources by walk and names no component file."""
        source = GATE.read_text(encoding="utf-8")
        assert "rglob" in source
        for pinned in ("AppShell.tsx", "PipelineBoard.tsx", "SettingsPage.tsx"):
            assert pinned not in source.split('"""', 2)[2], pinned

    def test_selector_parsing_unescapes_every_escape_form(self) -> None:
        parsed = gate.parse_class_selectors(".a\\:b { x: y } .c\\[\\&\\]d { x: y } .plain { x: y }")
        assert parsed == {"a:b", "c[&]d", "plain"}

    def test_a_class_named_in_a_comment_is_not_a_definition(self) -> None:
        parsed = gate.parse_class_selectors("/* .documented-only is nice */ .real { x: y }")
        assert "documented-only" not in parsed
        assert "real" in parsed


class TestExtractionReach:
    def test_a_class_in_a_hoisted_constant_is_seen(self, tmp_path: Path, stylesheet: Path) -> None:
        """An unresolved class inside a hoisted string constant is reported."""
        root = tmp_path / "src"
        path = root / "Hoisted.tsx"
        path.parent.mkdir(parents=True)
        path.write_text(
            'const shell = "flex max-w-panel";\n'
            "export function C() { return <div className={shell} />; }\n",
            encoding="utf-8",
        )
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 1, result.stdout
        assert "max-w-panel" in result.stdout

    def test_prose_is_not_mistaken_for_a_class_list(self, tmp_path: Path, stylesheet: Path) -> None:
        """Prose string literals are not read as class lists."""
        root = tmp_path / "src"
        path = root / "Prose.tsx"
        path.parent.mkdir(parents=True)
        path.write_text(
            'const label = "Loading the pipeline";\n'
            'const aria = "Pipeline board";\n'
            'const testid = "pipeline-loading";\n'
            'export function C() { return <div className="flex" aria-label={aria} />; }\n',
            encoding="utf-8",
        )
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout

    def test_a_numerically_escaped_selector_resolves(
        self, tmp_path: Path, stylesheet: Path
    ) -> None:
        """A class whose selector uses a numeric escape (`\\2c `) resolves."""
        root = tmp_path / "src"
        _source(root, "Grid.tsx", "flex sm:grid-cols-[minmax(0,10rem)_1fr]")
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout

    def test_unescaping_handles_the_numeric_form(self) -> None:
        parsed = gate.parse_class_selectors(".a\\2c b { x: y }")
        assert parsed == {"a,b"}

    def test_a_test_file_is_not_scanned(self, tmp_path: Path, stylesheet: Path) -> None:
        root = tmp_path / "src"
        path = root / "Thing.test.tsx"
        path.parent.mkdir(parents=True)
        path.write_text('const r = "route not-a-class-at-all";\n', encoding="utf-8")
        _source(root, "Thing.tsx", "flex")
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout


class TestPositiveControl:
    """A stylesheet missing the tree's most-used class is treated as degraded."""

    def test_a_stylesheet_missing_the_most_used_class_skips(self, tmp_path: Path) -> None:
        # A real stylesheet with real rules, but not this app's utilities.
        unrelated = tmp_path / "unrelated.css"
        unrelated.write_text(".some-unrelated-rule { color: red }\n", encoding="utf-8")
        result = _run("--css", str(unrelated))
        assert result.returncode == gate.SKIP_EXIT_STATUS, result.stdout
        assert "degraded" in result.stdout
        assert "Nothing was verified" in result.stdout

    def test_the_control_is_derived_not_named(self) -> None:
        source = GATE.read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        assert 'CONTROL_CLASS = "flex"' not in body
        assert "derived_positive_control" in body

    def test_no_control_when_nothing_repeats(self, tmp_path: Path, stylesheet: Path) -> None:
        """A tree in which no class repeats gets no positive control."""
        root = tmp_path / "src"
        _source(root, "Tiny.tsx", "flex")
        assert gate.derived_positive_control(gate.collect_sources(root)) is None
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout


class TestSingleTokenBlindSpot:
    """Single-token and mostly-unresolved class literals are still checked."""

    def test_a_single_token_hoisted_const_with_variant_syntax_is_caught(
        self, tmp_path: Path, stylesheet: Path
    ) -> None:
        root = tmp_path / "src"
        path = root / "Guard.tsx"
        path.parent.mkdir(parents=True)
        path.write_text(
            'export const GUARD = "[hidden]:hidden";\n'
            'export function G() { return <aside className="flex" />; }\n',
            encoding="utf-8",
        )
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 1, result.stdout
        assert "[hidden]:hidden" in result.stdout

    def test_a_mostly_inert_list_is_caught(self, tmp_path: Path, stylesheet: Path) -> None:
        # 3 tokens, 1 resolved.
        root = tmp_path / "src"
        _source(root, "Both.tsx", "[hidden]:hidden max-w-panel flex")
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 1, result.stdout
        assert "[hidden]:hidden" in result.stdout
        assert "max-w-panel" in result.stdout

    def test_prose_with_a_colon_in_a_comment_is_not_a_usage(
        self, tmp_path: Path, stylesheet: Path
    ) -> None:
        """Prose in a code comment is not read as a class usage."""
        root = tmp_path / "src"
        path = root / "Commented.tsx"
        path.parent.mkdir(parents=True)
        path.write_text(
            "/* `--pending-*` carries `border-style: dashed` and a hatch, so state survives greyscale. */\n"
            "// see https://example.invalid/docs for why operation: update is special\n"
            'export function C() { return <div className="flex" />; }\n',
            encoding="utf-8",
        )
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout

    def test_stripping_line_comments_does_not_eat_a_url(self) -> None:
        stripped = gate._strip_source_comments('const u = "https://example.invalid/x"; // note\n')
        assert "https://example.invalid/x" in stripped

    def test_comment_stripping_preserves_line_numbers(self) -> None:
        stripped = gate._strip_source_comments("/* a\nb\nc */\nconst x = 1;\n")
        assert len(stripped.splitlines()) == 4


class TestPartialBlindness:
    """A static class attribute that contributes no token is reported."""

    def test_a_static_attribute_contributing_no_token_fails(
        self, tmp_path: Path, stylesheet: Path
    ) -> None:
        root = tmp_path / "src"
        path = root / "Starved.tsx"
        path.parent.mkdir(parents=True)
        # A static attribute whose only token is undefined and carries no Tailwind syntax.
        path.write_text(
            'export function A() { return <div className="totallyunknownclass" />; }\n'
            'export function B() { return <div className="flex" />; }\n',
            encoding="utf-8",
        )
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 1, result.stdout
        assert "contributed no token" in result.stdout
        assert "Starved.tsx:1" in result.stdout

    def test_the_declined_count_is_reported(self) -> None:
        """The output reports how many literals were declined as not class lists."""
        _skip_or_fail_without_toolchain()
        result = _run()
        assert result.returncode == 0, result.stdout
        assert "declined as not class lists" in result.stdout


class TestUnambiguousSyntaxRule:
    """Single tokens are admitted as class usages only when they carry bracket grammar."""

    ADMITTED = [
        "[hidden]:hidden",
        "[&[hidden]]:hidden",
        "max-w-[var(--layout-panel-width)]",
        "sm:grid-cols-[minmax(0,10rem)_1fr]",
        "data-[open=false]:hidden",
        "aria-[invalid=true]:border-danger-border",
        "w-[calc(100%-2rem)]",
    ]

    REJECTED = [
        "12:30",
        "tenant:demo",
        "items[0]",
        "Owner: unassigned",
        "/accounts/:accountId",
        "eval-run:2026-01-01",
        # Bracket-less variants are not admitted.
        "lg:w-panel",
        "hover:bg-surface-hover",
    ]

    @pytest.mark.parametrize("token", ADMITTED)
    def test_bracket_grammar_is_admitted(self, token: str) -> None:
        assert gate._UNAMBIGUOUS_CLASS_SYNTAX.search(token), token

    @pytest.mark.parametrize("token", REJECTED)
    def test_prose_and_identifiers_are_rejected(self, token: str) -> None:
        assert not gate._UNAMBIGUOUS_CLASS_SYNTAX.search(token), token

    def test_a_composite_key_literal_does_not_become_a_finding(
        self, tmp_path: Path, stylesheet: Path
    ) -> None:
        """Composite-key and time literals produce no finding end to end."""
        root = tmp_path / "src"
        path = root / "Keys.tsx"
        path.parent.mkdir(parents=True)
        path.write_text(
            'const tenantKey = "tenant:demo";\n'
            'const at = "12:30";\n'
            'export function K() { return <div className="flex" />; }\n',
            encoding="utf-8",
        )
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout


class TestControlVisibility:
    def test_the_output_says_whether_a_control_was_applied(self) -> None:
        """The output names the positive control it applied."""
        _skip_or_fail_without_toolchain()
        result = _run()
        assert result.returncode == 0, result.stdout
        assert "positive control `" in result.stdout

    def test_a_tree_with_no_repeating_class_says_so(self, tmp_path: Path, stylesheet: Path) -> None:
        root = tmp_path / "src"
        _source(root, "Tiny.tsx", "flex")
        result = _run("--root", str(root), "--css", str(stylesheet))
        assert result.returncode == 0, result.stdout
        assert "no positive control applied" in result.stdout


_ENTRY = """@import "./tokens.css";
@import "tailwindcss" source(none);
@config "../../tailwind.config.ts";
@source "../**/*.{ts,tsx}";
@source "../../index.html";
@source not inline("max-w-[var(…)]");
@layer base { body { margin: 0 } }
"""


class TestSourceBounding:
    """The compile reads the root it is given and nothing else, as `--content` did under Tailwind 3."""

    def test_every_content_source_is_replaced_and_exclusions_are_kept(self) -> None:
        css = gate.entry_over_sources(_ENTRY, ["/fixture/**/*.{ts,tsx,jsx}", "/fixture/index.html"])
        assert '@source "../**/*.{ts,tsx}";' not in css
        assert '@source "../../index.html";' not in css
        assert '@source "/fixture/**/*.{ts,tsx,jsx}";' in css
        assert '@source "/fixture/index.html";' in css
        assert '@source not inline("max-w-[var(…)]");' in css
        assert css.index('@config "../../tailwind.config.ts";') < css.index('@source "/fixture/')
        assert css.endswith("@layer base { body { margin: 0 } }\n")

    def test_an_entry_without_source_none_is_refused(self) -> None:
        with pytest.raises(ValueError, match="source\\(none\\)"):
            gate.entry_over_sources(_ENTRY.replace(" source(none)", ""), ["/fixture/**/*.tsx"])

    def test_an_entry_without_a_content_source_is_refused(self) -> None:
        entry = "\n".join(line for line in _ENTRY.splitlines() if not line.startswith('@source "'))
        with pytest.raises(ValueError, match="no `@source`"):
            gate.entry_over_sources(entry, ["/fixture/**/*.tsx"])

    def test_the_real_entry_point_can_be_bounded(self) -> None:
        """The committed `tailwind.css` carries the two directives the bounding depends on."""
        entry = gate.FRONTEND.joinpath(*_TAILWIND_ENTRY_PARTS).read_text(encoding="utf-8")
        bounded = gate.entry_over_sources(entry, ["/fixture/**/*.tsx"])
        assert '@source "/fixture/**/*.tsx";' in bounded
        assert '@source "../' not in bounded

    def test_a_fixture_root_compiles_without_the_real_tree(self, tmp_path: Path) -> None:
        """Compiling over a one-class fixture defines a handful of selectors, not the real tree's."""
        _skip_or_fail_without_toolchain()
        root = tmp_path / "src"
        _source(root, "Widget.tsx", "flex gap-3")
        result = _run("--root", str(root), "--json")
        payload = json.loads(result.stdout)
        assert result.returncode == 0, result.stdout
        assert payload["unresolved"] == {}, payload
        assert 0 < payload["defined_selectors"] < 50, payload


class TestEntryParsing:
    """`@source` and `source(none)` are read from the entry's CSS, not from its comments."""

    def test_a_glob_containing_a_comment_opener_is_kept(self) -> None:
        assert gate.entry_content_paths(_ENTRY) == ["../**/*.{ts,tsx}", "../../index.html"]

    def test_source_none_in_a_comment_does_not_count(self) -> None:
        entry = _ENTRY.replace(" source(none)", "") + '/* @import "tailwindcss" source(none); */\n'
        with pytest.raises(ValueError, match="source\\(none\\)"):
            gate.entry_over_sources(entry, ["/fixture/**/*.tsx"])

    def test_a_source_in_a_comment_is_not_a_content_path(self) -> None:
        entry = _ENTRY + '/* @source "../../elsewhere/**/*.tsx"; */\n'
        assert gate.entry_content_paths(entry) == ["../**/*.{ts,tsx}", "../../index.html"]

    def test_single_quotes_and_spacing_are_parsed_and_replaced(self) -> None:
        entry = _ENTRY.replace('@source "../../index.html";', "  @source   '../../index.html' ;")
        assert gate.entry_content_paths(entry) == ["../**/*.{ts,tsx}", "../../index.html"]
        assert "index.html" not in gate.entry_over_sources(entry, ["/fixture/**/*.tsx"])

    def test_an_unparsed_source_argument_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unparsed"):
            gate.entry_content_paths(_ENTRY + "@source ../**/*.tsx;\n")


#: What `_scratch_frontend` leaves out of its `src/` copy: nothing the gate or the Tailwind compile
#: reads. The gate skips `generated/`, `node_modules/`, `dist/` and `__snapshots__/` (see
#: `EXCLUDED_DIR_NAMES`); `coverage/` and `.vite/` are tool output. All are absent or about 20 KB
#: today; the list bounds whatever untracked content later lands under `frontend/src`.
_SCRATCH_SRC_IGNORE = shutil.ignore_patterns(
    "node_modules", "dist", "coverage", ".vite", "__snapshots__", "generated"
)


class TestProductionCoverage:
    """For the default root the gate compiles the entry's own `@source` paths and checks they reach
    every component file, so a narrowed production glob fails the gate."""

    def test_an_uncovered_extension_is_reported(self, tmp_path: Path) -> None:
        entry = tmp_path / "src" / "styles" / "tailwind.css"
        entry.parent.mkdir(parents=True)
        entry.write_text('@source "../**/*.{ts,tsx}";\n', encoding="utf-8")
        _source(tmp_path / "src", "pages/Covered.tsx", "flex")
        _source(tmp_path / "src", "pages/Missed.jsx", "flex")
        sources = gate.collect_sources(tmp_path / "src")
        assert [p.name for p in gate.uncovered_sources(entry, sources)] == ["Missed.jsx"]

    def test_the_real_entry_covers_every_component_file(self) -> None:
        entry = gate.FRONTEND.joinpath(*_TAILWIND_ENTRY_PARTS)
        sources = gate.collect_sources(gate.DEFAULT_ROOT)
        assert sources, "no component source found"
        assert gate.uncovered_sources(entry, sources) == []

    @staticmethod
    def _scratch_frontend(tmp_path: Path) -> Path:
        """A copy of the frontend's `src/` and config with `node_modules` symlinked, not copied."""
        frontend = tmp_path / "frontend"
        shutil.copytree(gate.FRONTEND / "src", frontend / "src", ignore=_SCRATCH_SRC_IGNORE)
        for name in ("tailwind.config.ts", "index.html"):
            shutil.copy2(gate.FRONTEND / name, frontend / name)
        (frontend / "node_modules").symlink_to(gate.FRONTEND / "node_modules")
        return frontend

    def _main(self, frontend: Path, monkeypatch: pytest.MonkeyPatch) -> int:
        monkeypatch.setattr(gate, "FRONTEND", frontend)
        monkeypatch.setattr(gate, "DEFAULT_ROOT", frontend / "src")
        return int(gate.main([]))

    def test_the_unmutated_copy_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Control: the scratch copy passes, so the mutation below is what reds it."""
        _skip_or_fail_without_toolchain()
        frontend = self._scratch_frontend(tmp_path)
        assert self._main(frontend, monkeypatch) == 0, capsys.readouterr().out

    def test_a_narrowed_production_source_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _skip_or_fail_without_toolchain()
        frontend = self._scratch_frontend(tmp_path)
        entry = frontend.joinpath(*_TAILWIND_ENTRY_PARTS)
        css = entry.read_text(encoding="utf-8")
        assert '@source "../**/*.{ts,tsx,jsx}";' in css
        entry.write_text(css.replace("../**/*.{ts,tsx,jsx}", "../pages/**/*.{ts,tsx,jsx}"), "utf-8")
        assert self._main(frontend, monkeypatch) == 1
        output = capsys.readouterr().out
        assert "no `@source`" in output
        assert "components/ui/button.tsx" in output


def _fake_frontend(root: Path, *, binary: bool = True, entry: bool = True) -> Path:
    """Return a scratch frontend tree for the toolchain probe, with either precondition omitted."""
    frontend = root / "frontend"
    if binary:
        path = frontend.joinpath(*_TAILWIND_PLUGIN_PARTS)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    if entry:
        path = frontend.joinpath(*_TAILWIND_ENTRY_PARTS)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    frontend.mkdir(parents=True, exist_ok=True)
    return frontend


def _pretend_node(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    """Pin the `node`-on-PATH clause independently of the host."""
    monkeypatch.setattr(shutil, "which", lambda _name: "/fixture/bin/node" if present else None)


class TestToolchainRequirement:
    """The `CRM_REQUIRE_EMITTED_CSS` contract and the toolchain probe deciding when it applies."""

    def test_a_complete_tree_reports_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A complete scratch tree with `node` present reports the toolchain available."""
        _pretend_node(monkeypatch, present=True)
        assert stylesheet_toolchain_unavailable_reason(_fake_frontend(tmp_path)) is None

    def test_an_absent_binary_is_named_with_its_remedy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Node is pinned present so the reason names this clause alone.
        _pretend_node(monkeypatch, present=True)
        reason = stylesheet_toolchain_unavailable_reason(_fake_frontend(tmp_path, binary=False))
        assert reason is not None
        assert "tailwindcss" in reason
        assert "npm ci" in reason
        assert _NODE_ABSENT_CLAUSE not in reason, f"reported an unrelated clause: {reason!r}"

    def test_an_absent_entry_point_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pretend_node(monkeypatch, present=True)
        reason = stylesheet_toolchain_unavailable_reason(_fake_frontend(tmp_path, entry=False))
        assert reason is not None
        assert "tailwind.css" in reason
        assert _NODE_ABSENT_CLAUSE not in reason, f"reported an unrelated clause: {reason!r}"

    def test_an_absent_node_is_named(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _pretend_node(monkeypatch, present=False)
        reason = stylesheet_toolchain_unavailable_reason(_fake_frontend(tmp_path))
        assert reason == _NODE_ABSENT_CLAUSE, f"expected the node clause alone, got {reason!r}"

    def test_every_failing_clause_is_reported_not_only_the_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pretend_node(monkeypatch, present=False)
        frontend = _fake_frontend(tmp_path, binary=False, entry=False)
        reason = stylesheet_toolchain_unavailable_reason(frontend)
        assert reason is not None
        for expected in ("tailwindcss", _NODE_ABSENT_CLAUSE, "tailwind.css"):
            assert expected in reason, f"{expected} unreported in {reason!r}"

    def test_an_absent_toolchain_skips_when_the_variable_is_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pretend_node(monkeypatch, present=True)
        monkeypatch.delenv(REQUIRE_TOOLCHAIN_ENV_VAR, raising=False)
        with pytest.raises(pytest.skip.Exception) as excinfo:
            _skip_or_fail_without_toolchain(_fake_frontend(tmp_path, binary=False))
        assert "tailwindcss" in str(excinfo.value)
        assert "npm ci" in str(excinfo.value)

    def test_an_absent_toolchain_fails_rather_than_skips_when_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pretend_node(monkeypatch, present=True)
        monkeypatch.setenv(REQUIRE_TOOLCHAIN_ENV_VAR, "1")
        with pytest.raises(pytest.fail.Exception) as excinfo:
            _skip_or_fail_without_toolchain(_fake_frontend(tmp_path, binary=False))
        assert REQUIRE_TOOLCHAIN_ENV_VAR in str(excinfo.value)
        assert "tailwindcss" in str(excinfo.value)

    def test_a_present_toolchain_neither_skips_nor_fails_whatever_the_variable_says(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A present toolchain neither skips nor fails, with or without the variable."""
        _pretend_node(monkeypatch, present=True)
        frontend = _fake_frontend(tmp_path)
        monkeypatch.setenv(REQUIRE_TOOLCHAIN_ENV_VAR, "1")
        _skip_or_fail_without_toolchain(frontend)
        monkeypatch.delenv(REQUIRE_TOOLCHAIN_ENV_VAR)
        _skip_or_fail_without_toolchain(frontend)

    def test_only_an_exact_1_forces_the_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value, expected in (("1", True), ("0", False), ("true", False), ("", False)):
            monkeypatch.setenv(REQUIRE_TOOLCHAIN_ENV_VAR, value)
            assert _require_toolchain() is expected, value
        monkeypatch.delenv(REQUIRE_TOOLCHAIN_ENV_VAR)
        assert _require_toolchain() is False

    def test_the_probe_names_the_preconditions_the_gate_actually_has(self) -> None:
        """The probe's preconditions and bailout count match `build_stylesheet`'s source."""
        source = inspect.getsource(gate.build_stylesheet)
        for parts in (_TAILWIND_PLUGIN_PARTS, _TAILWIND_ENTRY_PARTS):
            rendered = " / ".join(f'"{part}"' for part in parts)
            assert rendered in source, f"{rendered} is no longer how the gate builds its path"
        assert 'shutil.which("node")' in source
        assert source.count("return None") == _EXPECTED_BUILD_BAILOUTS, (
            "`build_stylesheet` has gained or lost a bailout; re-derive "
            "`stylesheet_toolchain_unavailable_reason` before adjusting this count"
        )

    def test_every_test_needing_a_compile_is_gated(self) -> None:
        """Every test in this file that compiles the real stylesheet is toolchain-gated.

        Tests that pass `--css` or expect the skip exit status are exempt.
        """
        compiling, supplied_css = _tests_by_compile_need(Path(__file__).read_text(encoding="utf-8"))
        assert compiling, "no test was classified as compiling — the AST walk has stopped matching"
        assert supplied_css, "no test was classified as `--css`-supplied — same failure, other side"
        ungated = sorted(name for name, gated in compiling.items() if not gated)
        assert not ungated, (
            f"these tests compile the real stylesheet but do not call "
            f"`_skip_or_fail_without_toolchain()`, so they fail on a host without the frontend "
            f"toolchain instead of skipping: {ungated}"
        )

    def test_non_vacuity_the_classifier_separates_all_four_shapes(self) -> None:
        """The classifier separates ungated, gated, `--css`-supplied, and skip-expecting tests."""
        compiling, supplied_css = _tests_by_compile_need(
            "def test_needs_a_build():\n"
            "    _run()\n"
            "def test_supplies_its_own_css():\n"
            '    _run("--root", "r", "--css", "c")\n'
            "def test_expects_a_skip():\n"
            "    assert _run().returncode == gate.SKIP_EXIT_STATUS\n"
            "def test_is_gated():\n"
            "    _skip_or_fail_without_toolchain()\n"
            "    _run()\n"
            "def test_never_invokes_the_gate():\n"
            "    assert True\n"
        )
        assert compiling == {"test_needs_a_build": False, "test_is_gated": True}
        assert supplied_css == ["test_supplies_its_own_css"]


def _tests_by_compile_need(source: str) -> tuple[dict[str, bool], list[str]]:
    """Partition ``source``'s tests by whether they make the gate compile.

    Returns ``({name: is_gated} for tests that compile, [names that pass --css])``. Non-literal
    ``_run`` arguments are ignored, so such a call counts as compiling.
    """
    tree = ast.parse(source)
    compiling: dict[str, bool] = {}
    supplied_css: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        calls = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_run"
        ]
        if not calls:
            continue
        literals = {
            arg.value
            for call in calls
            for arg in call.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        }
        if "--css" in literals:
            supplied_css.append(node.name)
            continue
        if "SKIP_EXIT_STATUS" in ast.dump(node):
            continue
        compiling[node.name] = "_skip_or_fail_without_toolchain" in ast.dump(node)
    return compiling, supplied_css
