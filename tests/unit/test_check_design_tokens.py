# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_design_tokens.py`.

Covers raw colours, `px` font sizes, magic spacing, the migration ratchet, and series channels.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_design_tokens.py"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
PENDING_FILE = REPO_ROOT / "scripts" / "design_token_migration_pending.txt"


def _load_module(name: str, path: Path) -> ModuleType:
    """Import a script from `scripts/` by file path and return the module."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_design_tokens", GATE)
run_gates = _load_module("run_gates_for_test", REPO_ROOT / "scripts" / "run_gates.py")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(GATE), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _component(tmp_path: Path, name: str, body: str) -> Path:
    root = tmp_path / "src"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_text(body, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The real tree, and the skip disposition.
# ---------------------------------------------------------------------------


def test_passes_on_the_real_component_tree() -> None:
    """The gate passes on the real frontend component tree."""
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_skips_with_exit_three_when_the_root_does_not_exist(tmp_path: Path) -> None:
    result = _run("--root", str(tmp_path / "absent"))
    assert result.returncode == gate.SKIP_EXIT_STATUS == 3
    assert "SKIP" in result.stdout
    assert "Nothing was verified" in result.stdout


def test_skips_with_exit_three_when_no_component_source_is_found(tmp_path: Path) -> None:
    """A present tree with no component source exits 3 with SKIP."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "notes.md").write_text("no components here\n", encoding="utf-8")
    result = _run("--root", str(tmp_path / "src"))
    assert result.returncode == 3
    assert "SKIP" in result.stdout


def test_skip_status_matches_run_gates_contract() -> None:
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS


# ---------------------------------------------------------------------------
# Class 1 — raw colour.
# ---------------------------------------------------------------------------


class TestRawColour:
    def test_hex_colour_in_a_css_property_fails(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "card.css", ".card {\n  border: 1px solid #d0d0d0;\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "raw-colour" in result.stderr
        assert "#d0d0d0" in result.stderr

    def test_hex_colour_in_a_custom_property_declaration_passes(self, tmp_path: Path) -> None:
        """A hex literal in a custom-property declaration is allowed."""
        root = _component(tmp_path, "theme.css", ":root {\n  --surface-raised: #ffffff;\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_rgba_literal_in_css_fails(self, tmp_path: Path) -> None:
        """An `rgba(...)` literal in CSS is reported as a raw colour."""
        root = _component(
            tmp_path, "card.css", ".card {\n  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);\n}\n"
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "raw-colour" in result.stderr

    def test_hex_colour_in_a_tsx_inline_style_fails(self, tmp_path: Path) -> None:
        root = _component(
            tmp_path,
            "Card.tsx",
            'export const Card = () => <div style={{ color: "#1a1a1a" }} />;\n',
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "#1a1a1a" in result.stderr

    def test_tailwind_arbitrary_hex_fails(self, tmp_path: Path) -> None:
        root = _component(
            tmp_path, "Card.tsx", 'export const C = () => <div className="bg-[#ff0000]" />;\n'
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "raw-colour" in result.stderr

    def test_a_fragment_href_is_not_mistaken_for_a_colour(self, tmp_path: Path) -> None:
        """Fragment hrefs such as `href="#abc"` are not reported as colours."""
        root = _component(
            tmp_path,
            "Skip.tsx",
            'export const S = () => <a href="#main-content">Skip</a>;\n'
            'export const T = () => <a href="#abc">Anchor</a>;\n',
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_hex_in_a_css_comment_is_not_a_violation(self, tmp_path: Path) -> None:
        root = _component(
            tmp_path,
            "note.css",
            "/* #1a1a1a on #ffffff is ~18.4:1 */\n.card {\n  color: var(--text-primary);\n}\n",
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Class 2 — px font size.
# ---------------------------------------------------------------------------


class TestPxFontSize:
    def test_css_px_font_size_fails(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "t.css", ".t {\n  font-size: 13px;\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "px-font-size" in result.stderr

    def test_rem_font_size_via_token_passes(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "t.css", ".t {\n  font-size: var(--text-sm);\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_tsx_fontsize_px_fails(self, tmp_path: Path) -> None:
        root = _component(
            tmp_path, "T.tsx", 'export const T = () => <p style={{ fontSize: "13px" }} />;\n'
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "px-font-size" in result.stderr

    def test_tailwind_arbitrary_px_text_size_fails(self, tmp_path: Path) -> None:
        root = _component(
            tmp_path, "T.tsx", 'export const T = () => <p className="text-[13px]" />;\n'
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "px-font-size" in result.stderr

    def test_a_tailwind_named_text_size_passes(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "T.tsx", 'export const T = () => <p className="text-sm" />;\n')
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_px_border_width_is_not_reported_as_a_font_size(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "t.css", ".t {\n  border-width: 1px;\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Class 3 — magic spacing.
# ---------------------------------------------------------------------------


class TestMagicSpacing:
    def test_magic_padding_fails(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "t.css", ".t {\n  padding: 11px;\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "magic-spacing" in result.stderr
        assert "11px" in result.stderr

    def test_token_padding_passes(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "t.css", ".t {\n  padding: var(--space-5);\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_multi_term_padding_with_one_magic_value_fails(self, tmp_path: Path) -> None:
        """A shorthand with one off-scale term fails."""
        root = _component(tmp_path, "t.css", ".t {\n  padding: var(--space-4) 7px;\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "7px" in result.stderr

    def test_zero_and_auto_pass(self, tmp_path: Path) -> None:
        """`0` and `auto` spacing values pass."""
        root = _component(
            tmp_path, "t.css", ".t {\n  margin: 0 auto;\n  inset: 0;\n  padding: 0;\n}\n"
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_calc_over_a_token_passes(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "t.css", ".t {\n  padding: calc(var(--space-5) * 2);\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_width_is_out_of_scope(self, tmp_path: Path) -> None:
        """`width` values are not checked for magic spacing."""
        root = _component(tmp_path, "t.css", ".t {\n  width: 37px;\n}\n")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_tailwind_arbitrary_spacing_fails(self, tmp_path: Path) -> None:
        root = _component(
            tmp_path, "T.tsx", 'export const T = () => <div className="p-[11px]" />;\n'
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "magic-spacing" in result.stderr


# ---------------------------------------------------------------------------
# Scope — what the gate must not scan.
# ---------------------------------------------------------------------------


class TestScope:
    def test_the_token_layer_itself_is_excluded(self) -> None:
        """`styles/tokens.css` is excluded from the scanned sources."""
        sources = gate.collect_component_sources(FRONTEND_SRC)
        assert not any(p.name == "tokens.css" for p in sources)

    def test_test_files_are_excluded(self, tmp_path: Path) -> None:
        """Test files are excluded from the scan."""
        root = _component(
            tmp_path,
            "Card.test.tsx",
            'it("is dark", () => expect(c).toBe("#1a1a1a"));\n',
        )
        (root / "Card.tsx").write_text("export const C = () => null;\n", encoding="utf-8")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_generated_directories_are_excluded(self, tmp_path: Path) -> None:
        root = tmp_path / "src"
        (root / "types" / "generated").mkdir(parents=True)
        (root / "types" / "generated" / "x.tsx").write_text(
            'export const C = "#abcdef";\n', encoding="utf-8"
        )
        (root / "Real.tsx").write_text("export const R = () => null;\n", encoding="utf-8")
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The migration ratchet — tested in both directions.
# ---------------------------------------------------------------------------


class TestMigrationRatchet:
    def test_a_listed_file_defers_rather_than_fails(self, tmp_path: Path) -> None:
        """A listed violating file defers and passes, and the same file unlisted fails."""
        root = _component(tmp_path, "legacy.css", ".a { color: #abcdef; }\n")
        fixture = (root / "legacy.css").as_posix()
        pending = tmp_path / "pending.txt"
        pending.write_text(f"{fixture}\n", encoding="utf-8")

        deferred = _run("--root", str(root), "--pending", str(pending))
        assert deferred.returncode == 0, deferred.stdout + deferred.stderr
        assert f"pending migration: {fixture}" in deferred.stdout

        without_ratchet = _run("--root", str(root), "--no-pending")
        assert without_ratchet.returncode == 1
        assert fixture in without_ratchet.stderr
        assert "#abcdef" in without_ratchet.stderr

    def test_read_pending_ignores_comments_and_blank_lines(self, tmp_path: Path) -> None:
        pending = tmp_path / "p.txt"
        pending.write_text(
            "# header\n\nfrontend/src/a.css\nfrontend/src/b.css # trailing\n", encoding="utf-8"
        )
        assert gate.read_pending(pending) == ["frontend/src/a.css", "frontend/src/b.css"]

    def test_read_pending_on_a_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert gate.read_pending(tmp_path / "nope.txt") == []

    def test_a_stale_ratchet_entry_fails_the_gate(self, tmp_path: Path) -> None:
        """A ratchet entry whose file no longer violates fails the gate."""
        pending = tmp_path / "pending.txt"
        pending.write_text("frontend/src/main.tsx\n", encoding="utf-8")
        result = _run("--pending", str(pending))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "STALE RATCHET ENTRY" in result.stderr
        assert "frontend/src/main.tsx" in result.stderr

    def test_the_committed_ratchet_has_no_stale_entry(self) -> None:
        """The committed ratchet produces no stale-entry failure."""
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert "STALE RATCHET ENTRY" not in result.stderr

    def test_every_committed_ratchet_entry_exists_and_violates(self) -> None:
        for entry in gate.read_pending(PENDING_FILE):
            path = REPO_ROOT / entry
            assert path.is_file(), f"ratchet entry {entry} does not exist"
            assert gate.scan_file(path), f"ratchet entry {entry} no longer violates — remove it"

    def test_every_ratchet_entry_is_justified_and_an_empty_ratchet_is_the_finished_state(
        self, tmp_path: Path
    ) -> None:
        """Every ratchet entry still violates, and an empty ratchet over a clean tree passes."""
        entries = gate.read_pending(PENDING_FILE)
        for entry in entries:
            path = REPO_ROOT / entry
            assert path.is_file(), f"ratchet entry {entry} does not exist"
            assert gate.scan_file(path), f"ratchet entry {entry} no longer violates — remove it"

        clean = _component(tmp_path, "Clean.tsx", "export const C = () => <p>ok</p>;\n")
        empty = tmp_path / "empty.txt"
        empty.write_text("# every file has been migrated\n", encoding="utf-8")
        result = _run("--root", str(clean), "--pending", str(empty))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "STALE RATCHET ENTRY" not in result.stderr
        assert gate.read_pending(empty) == []


@pytest.mark.parametrize(
    ("body", "expected_kind"),
    [
        (".a { color: #abcdef; }", "raw-colour"),
        (".a { font-size: 12px; }", "px-font-size"),
        (".a { margin: 13px; }", "magic-spacing"),
    ],
)
def test_each_named_violation_class_is_detected(body: str, expected_kind: str) -> None:
    """Each of the three violation classes is detected in CSS."""
    violations = gate.scan_css(Path("x.css"), body)
    assert [v.kind for v in violations] == [expected_kind]


# ---------------------------------------------------------------------------
# Plain `.ts` scope.
# ---------------------------------------------------------------------------


class TestPlainTypeScriptScope:
    def test_a_raw_hex_in_a_plain_ts_module_fails(self, tmp_path: Path) -> None:
        root = _component(
            tmp_path,
            "chartConfig.ts",
            'export const chart = { anthropic: "#1f6fb2", openai: "#8a5620" };\n',
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "raw-colour" in result.stderr
        assert "#1f6fb2" in result.stderr

    def test_a_px_font_size_in_a_plain_ts_module_fails(self, tmp_path: Path) -> None:
        root = _component(tmp_path, "labels.ts", 'export const s = { fontSize: "11px" };\n')
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "px-font-size" in result.stderr

    def test_plain_ts_is_in_the_scanned_suffix_set(self) -> None:
        assert ".ts" in gate.COMPONENT_SUFFIXES

    def test_the_real_plain_ts_modules_are_scanned(self) -> None:
        """The real tree's scanned sources include plain `.ts` modules."""
        sources = gate.collect_component_sources(FRONTEND_SRC)
        assert any(p.suffix == ".ts" for p in sources)

    def test_the_token_layer_theme_map_is_excluded_by_name(self) -> None:
        """`styles/theme.ts` is excluded by an explicit relative-path entry."""
        assert "styles/theme.ts" in gate.EXCLUDED_RELPATHS
        sources = gate.collect_component_sources(FRONTEND_SRC)
        assert not any(p.name == "theme.ts" for p in sources)

    def test_the_token_property_test_is_excluded_as_a_test(self) -> None:
        """`styles/tokens.test.ts` is excluded as a test file."""
        sources = gate.collect_component_sources(FRONTEND_SRC)
        assert not any(p.name == "tokens.test.ts" for p in sources)


# ---------------------------------------------------------------------------
# Series channels.
# ---------------------------------------------------------------------------


class TestSeriesChannels:
    def test_a_series_carried_by_hue_alone_fails(self, tmp_path: Path) -> None:
        root = _component(
            tmp_path,
            "chartConfig.ts",
            'export const c = { a: "var(--series-1)", b: "var(--series-2)" };\n',
        )
        result = _run("--root", str(root), "--no-pending")
        assert result.returncode == 1
        assert "series-hue-only" in result.stderr
        assert "--series-1-fill" in result.stderr
        assert "--series-2-fill" in result.stderr

    def test_hue_plus_surface_is_still_hue_alone(self) -> None:
        """A hue token paired only with its surface tint is still reported."""
        violations = gate.find_series_channel_violations(
            Path("c.ts"), 'a: "var(--series-1)", bg: "var(--series-1-surface)"'
        )
        assert len(violations) == 1

    def test_hue_plus_fill_passes(self) -> None:
        violations = gate.find_series_channel_violations(
            Path("c.ts"), 'a: "var(--series-1)", fill: "var(--series-1-fill)"'
        )
        assert violations == []

    def test_hue_plus_dash_passes(self) -> None:
        violations = gate.find_series_channel_violations(
            Path("c.ts"), 'a: "var(--series-2)", dash: "var(--series-2-dash)"'
        )
        assert violations == []

    def test_each_series_is_judged_independently(self) -> None:
        """Wiring series 2's pattern does not excuse a bare series 1 hue."""
        violations = gate.find_series_channel_violations(
            Path("c.ts"),
            'a: "var(--series-1)", b: "var(--series-2)", d: "var(--series-2-dash)"',
        )
        assert len(violations) == 1
        assert "--series-1" in violations[0].detail

    def test_series_one_being_the_unpatterned_arm_does_not_exempt_it(self) -> None:
        """A bare `--series-1` hue is reported even though its fill and dash tokens are `none`."""
        violations = gate.find_series_channel_violations(Path("c.ts"), 'a: "var(--series-1)"')
        assert len(violations) == 1

    def test_a_file_with_no_series_reference_is_not_flagged(self) -> None:
        violations = gate.find_series_channel_violations(
            Path("c.tsx"), 'className="text-sm" style={{ color: "var(--text-primary)" }}'
        )
        assert violations == []

    def test_the_real_tree_reports_the_series_check_as_not_yet_live(self) -> None:
        """The real tree reports the series check either as verifying nothing or as satisfied."""
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert "series channels:" in result.stdout
        if "no file references" in result.stdout:
            assert "verified nothing" in result.stdout
        else:
            assert "wiring a fill or dash channel" in result.stdout


class TestSeriesSurfaceExemption:
    """A `--series-N-surface` tint alone is exempt from the series check; the hue token is not."""

    def test_surface_alone_is_allowed(self) -> None:
        violations = gate.find_series_channel_violations(
            Path("row.tsx"), 'style={{ background: "var(--series-1-surface)" }}'
        )
        assert violations == []

    def test_both_surfaces_alone_are_allowed(self) -> None:
        violations = gate.find_series_channel_violations(
            Path("rows.tsx"),
            'a: "var(--series-1-surface)", b: "var(--series-2-surface)"',
        )
        assert violations == []

    def test_the_hue_token_is_still_not_exempted_by_a_surface(self) -> None:
        """A hue token named alongside its surface tint is still reported."""
        violations = gate.find_series_channel_violations(
            Path("legend.tsx"),
            'swatch: "var(--series-1)", bg: "var(--series-1-surface)"',
        )
        assert len(violations) == 1
        assert "--series-1" in violations[0].detail

    def test_the_exemption_is_per_series(self) -> None:
        """A surface-only series 1 does not excuse series 2's bare hue."""
        violations = gate.find_series_channel_violations(
            Path("mixed.tsx"),
            'a: "var(--series-1-surface)", b: "var(--series-2)"',
        )
        assert len(violations) == 1
        assert "--series-2" in violations[0].detail
