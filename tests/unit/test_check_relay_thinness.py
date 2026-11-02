# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_relay_thinness.py`.

Covers the domain-import, `Identity`-construction and size checks on synthetic relays and on a
scratch copy of the real relay, plus the false positives each check must not report.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_relay_thinness.py"
RELAY_DIR = REPO_ROOT / "src" / "relay"


def _load_module(name: str, path: Path) -> ModuleType:
    """Import a script from `scripts/` without requiring it to be importable as a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_relay_thinness", GATE)
run_gates = _load_module("run_gates_for_relay_test", REPO_ROOT / "scripts" / "run_gates.py")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(GATE), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _relay(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a minimal synthetic relay package under `tmp_path`."""
    root = tmp_path / "relay"
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The real relay, and the skip disposition.
# ---------------------------------------------------------------------------


def test_the_real_relay_is_thin() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "domain imports" in result.stdout
    assert "`Identity` construction" in result.stdout


def test_skips_with_exit_three_when_the_relay_is_absent(tmp_path: Path) -> None:
    """An absent relay directory exits with the skip status."""
    result = _run("--relay-dir", str(tmp_path / "absent"))
    assert result.returncode == gate.SKIP_EXIT_STATUS == 3
    assert "SKIP" in result.stdout
    assert "Nothing was verified" in result.stdout


def test_skips_when_the_relay_directory_holds_no_source(tmp_path: Path) -> None:
    root = tmp_path / "relay"
    root.mkdir()
    (root / "README.md").write_text("no source here\n", encoding="utf-8")
    result = _run("--relay-dir", str(root))
    assert result.returncode == 3
    assert "SKIP" in result.stdout


def test_skip_status_matches_run_gates_contract() -> None:
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS


def test_installed_dependencies_are_never_scanned(tmp_path: Path) -> None:
    """Files under `node_modules` are not scanned."""
    root = _relay(
        tmp_path,
        {
            "src/index.ts": "export const handler = 1;\n",
            "node_modules/pkg/index.js": 'const x = require("crm/authz");\n',
        },
    )
    result = _run("--relay-dir", str(root))
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Check 1 — no domain import.
# ---------------------------------------------------------------------------


class TestDomainImports:
    @pytest.mark.parametrize("package", ["crm", "agents", "gateway", "eval"])
    def test_an_import_of_each_domain_package_fails(self, tmp_path: Path, package: str) -> None:
        root = _relay(
            tmp_path, {"src/index.ts": f'import {{ thing }} from "{package}/module.js";\n'}
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 1
        assert "domain-import" in result.stderr
        assert package in result.stderr

    def test_a_relative_import_escaping_the_package_fails(self, tmp_path: Path) -> None:
        """A relative import that resolves outside the relay package fails."""
        root = _relay(tmp_path, {"src/index.ts": 'import { x } from "../../agents/events.js";\n'})
        result = _run("--relay-dir", str(root))
        assert result.returncode == 1
        assert "resolves outside" in result.stderr

    def test_a_relative_import_inside_the_package_passes(self, tmp_path: Path) -> None:
        root = _relay(
            tmp_path,
            {
                "src/index.ts": 'import { verify } from "./jwt.js";\n',
                "src/jwt.ts": "export const verify = 1;\n",
            },
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_ordinary_third_party_imports_pass(self, tmp_path: Path) -> None:
        root = _relay(
            tmp_path,
            {
                "src/index.ts": (
                    'import { JwtVerifier } from "aws-jwt-verify";\n'
                    'import type { LambdaFunctionURLEvent } from "aws-lambda";\n'
                    'import { SignatureV4 } from "@aws-sdk/signature-v4";\n'
                )
            },
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "statement",
        [
            'import { x } from "crm/authz.js";',
            'export { x } from "crm/authz.js";',
            'const m = await import("crm/authz.js");',
            'const m = require("crm/authz.js");',
            'import "crm/authz.js";',
        ],
    )
    def test_every_import_form_is_recognised(self, tmp_path: Path, statement: str) -> None:
        """Every static, dynamic, re-export, require and side-effect import form is recognised."""
        root = _relay(tmp_path, {"src/index.ts": statement + "\n"})
        result = _run("--relay-dir", str(root))
        assert result.returncode == 1, f"{statement} was not recognised"

    def test_a_domain_name_in_a_comment_is_not_an_import(self, tmp_path: Path) -> None:
        """A domain module named only in a comment is not an import."""
        root = _relay(
            tmp_path,
            {
                "src/index.ts": (
                    "/*\n * Claim semantics mirror src/gateway/identity_interceptor.py, and\n"
                    ' * authorization stays in crm/authz. Never `from "crm/authz"` here.\n */\n'
                    "// see agents/events.py for the frame contract\n"
                    "export const handler = 1;\n"
                )
            },
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_reading_a_sibling_package_json_as_data_is_not_an_import(self, tmp_path: Path) -> None:
        """Reading a sibling `package.json` as a JSON file is not an import."""
        root = _relay(
            tmp_path,
            {
                "tests/toolchain.test.ts": (
                    'import { readFileSync } from "node:fs";\n'
                    'const pkg = JSON.parse(readFileSync("../../frontend/package.json", "utf8"));\n'
                )
            },
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Check 2 — no `Identity` construction.
# ---------------------------------------------------------------------------


class TestIdentityConstruction:
    @pytest.mark.parametrize(
        "statement",
        [
            "const who = new Identity(claims);",
            "const who = Identity(claims);",
            "const who = identity_from_claims(claims);",
            "const who = decode_identity(event);",
        ],
    )
    def test_every_construction_form_fails(self, tmp_path: Path, statement: str) -> None:
        root = _relay(tmp_path, {"src/index.ts": statement + "\n"})
        result = _run("--relay-dir", str(root))
        assert result.returncode == 1, f"{statement} was not caught"
        assert "identity-construction" in result.stderr

    def test_an_identity_shaped_object_literal_fails(self, tmp_path: Path) -> None:
        """An object literal deriving identity fields from claims fails."""
        root = _relay(
            tmp_path,
            {
                "src/index.ts": (
                    "const who = { tenant: claims['custom:tenant'], scopes: parseScopes(claims) };\n"
                )
            },
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 1
        assert "identity-shaped" in result.stderr or "identity-construction" in result.stderr

    def test_forwarding_verified_claims_untouched_passes(self, tmp_path: Path) -> None:
        """Forwarding verified claims unchanged under `_agent_identity` passes."""
        root = _relay(
            tmp_path,
            {
                "src/runtime.ts": (
                    "export interface TurnRequest {\n"
                    "  readonly claims: Readonly<Record<string, unknown>>;\n"
                    "}\n"
                    "const payload = { _agent_identity: request.claims };\n"
                )
            },
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_word_identity_in_a_comment_or_a_type_name_passes(self, tmp_path: Path) -> None:
        """`Identity` in a comment, a type name or a payload key passes."""
        root = _relay(
            tmp_path,
            {
                "src/jwt.ts": (
                    "// It builds no `Identity` and derives no scope.\n"
                    "export type VerifiedClaims = Record<string, unknown>;\n"
                    "const IDENTITY_KEY = '_agent_identity';\n"
                )
            },
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_real_relay_constructs_no_identity(self) -> None:
        """The real relay has no `identity-construction` violation."""
        violations = [
            v
            for v in gate.scan_relay(RELAY_DIR, gate.DEFAULT_MAX_LINES)
            if v.kind == "identity-construction"
        ]
        assert violations == []


# ---------------------------------------------------------------------------
# Check 3 — the size ceiling, and what it must not punish.
# ---------------------------------------------------------------------------


class TestSizeCeiling:
    def test_an_oversize_production_file_fails(self, tmp_path: Path) -> None:
        root = _relay(tmp_path, {"src/index.ts": "const x = 1;\n" * 40})
        result = _run("--relay-dir", str(root), "--max-lines", "20")
        assert result.returncode == 1
        assert "oversize" in result.stderr
        assert "one sitting" in result.stderr

    def test_a_long_test_file_is_not_punished(self, tmp_path: Path) -> None:
        """A test file over the line ceiling is not a violation."""
        root = _relay(
            tmp_path,
            {
                "src/index.ts": "const x = 1;\n",
                "tests/index.test.ts": "it('works', () => {});\n" * 200,
            },
        )
        result = _run("--relay-dir", str(root), "--max-lines", "50")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_generated_file_is_not_punished(self, tmp_path: Path) -> None:
        """A generated file over the line ceiling is not a violation."""
        root = _relay(
            tmp_path,
            {
                "src/index.ts": "const x = 1;\n",
                "src/generated/events.ts": "export const K = 1;\n" * 200,
            },
        )
        result = _run("--relay-dir", str(root), "--max-lines", "50")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_generated_file_is_still_checked_for_domain_imports(self, tmp_path: Path) -> None:
        """A generated file is still checked for domain imports."""
        root = _relay(
            tmp_path,
            {"src/generated/events.ts": 'import { x } from "crm/authz.js";\n'},
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 1
        assert "domain-import" in result.stderr

    #: Minimum lines between the longest production relay file and the line ceiling.
    MINIMUM_HEADROOM_LINES = 50

    def test_the_ceiling_leaves_room_for_an_ordinary_change(self) -> None:
        production = [
            p
            for p in gate.collect_relay_sources(RELAY_DIR)
            if p.relative_to(RELAY_DIR).parts[0] == "src"
            and "generated" not in p.relative_to(RELAY_DIR).parts[:-1]
        ]
        assert production, "no production relay source found"
        longest = max(len(p.read_text(encoding="utf-8").splitlines()) for p in production)
        headroom = gate.DEFAULT_MAX_LINES - longest
        assert headroom > 0, (
            f"the longest production file is {longest} lines against a {gate.DEFAULT_MAX_LINES} "
            "ceiling — the gate cannot pass, so the ceiling is un-satisfiable"
        )
        assert headroom >= self.MINIMUM_HEADROOM_LINES, (
            f"only {headroom} lines of headroom below the {gate.DEFAULT_MAX_LINES} ceiling "
            f"(longest file {longest}). Either the relay has grown a second responsibility and the "
            "work belongs elsewhere, or the ceiling genuinely needs raising — and raising it is a "
            "deliberate decision, not a number to nudge."
        )


# ---------------------------------------------------------------------------
# End-to-end against a scratch copy of the real relay.
# ---------------------------------------------------------------------------


# Copies `src/relay`, which `test_package_relay.py` reinstalls and rebuilds; same xdist group.
@pytest.mark.xdist_group("relay_npm")
class TestEndToEndAgainstTheRealRelay:
    def _scratch(self, tmp_path: Path) -> Path:
        dest = tmp_path / "relay"
        shutil.copytree(
            RELAY_DIR, dest, ignore=shutil.ignore_patterns("node_modules", "dist", "coverage")
        )
        return dest

    def test_the_unmodified_copy_passes(self, tmp_path: Path) -> None:
        result = _run("--relay-dir", str(self._scratch(tmp_path)))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_planted_domain_import_fails(self, tmp_path: Path) -> None:
        root = self._scratch(tmp_path)
        index = root / "src" / "index.ts"
        text = index.read_text(encoding="utf-8")
        needle = 'import type { LambdaFunctionURLEvent } from "aws-lambda";'
        assert needle in text, "fixture setup did not find the import block"
        index.write_text(
            text.replace(needle, needle + '\nimport { filterToScope } from "crm/authz";', 1),
            encoding="utf-8",
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 1
        assert "crm/authz" in result.stderr

    def test_a_planted_identity_construction_fails(self, tmp_path: Path) -> None:
        root = self._scratch(tmp_path)
        index = root / "src" / "index.ts"
        text = index.read_text(encoding="utf-8")
        needle = "const TRANSPORT_ERROR_KIND"
        assert needle in text
        index.write_text(
            text.replace(needle, "const who = new Identity(claims);\n" + needle, 1),
            encoding="utf-8",
        )
        result = _run("--relay-dir", str(root))
        assert result.returncode == 1
        assert "identity-construction" in result.stderr
