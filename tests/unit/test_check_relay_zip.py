# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/check_relay_zip.py`.

Covers parity of the gate's constants with `scripts/package_relay.sh`, and every pass, fail and skip
path against a synthetic source tree and zip.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_relay_zip.py"
PACKAGE_RELAY_SH = REPO_ROOT / "scripts" / "package_relay.sh"

if TYPE_CHECKING:
    import scripts.check_relay_zip as gate
else:
    _SPEC = importlib.util.spec_from_file_location("check_relay_zip", SCRIPT_PATH)
    assert _SPEC is not None and _SPEC.loader is not None
    gate = importlib.util.module_from_spec(_SPEC)
    # Dataclass processing resolves annotations through `sys.modules[cls.__module__]`.
    sys.modules["check_relay_zip"] = gate
    _SPEC.loader.exec_module(gate)


def _shell_constant(name: str) -> str:
    text = PACKAGE_RELAY_SH.read_text(encoding="utf-8")
    match = re.search(rf'^readonly {re.escape(name)}="([^"]*)"', text, re.MULTILINE)
    assert match is not None, f"no `readonly {name}=...` in {PACKAGE_RELAY_SH}"
    return match.group(1)


def _shell_array(name: str) -> list[str]:
    """Parse a `readonly NAME=( "a" "b" )` bash array from the packaging script."""
    text = PACKAGE_RELAY_SH.read_text(encoding="utf-8")
    match = re.search(rf"^readonly {re.escape(name)}=\((.*?)\n\)", text, re.DOTALL | re.MULTILINE)
    assert match is not None, f"no `readonly {name}=(...)` array in {PACKAGE_RELAY_SH}"
    return re.findall(r'"([^"]+)"', match.group(1))


def _write_source_tree(root: Path) -> None:
    """Write a minimal `src/relay`-shaped tree covering every input root the manifest names."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "index.ts").write_text("export const handler = 1;\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"r"}\n', encoding="utf-8")
    (root / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (root / "tsconfig.build.json").write_text("{}\n", encoding="utf-8")


def _zip_with_manifest(path: Path, source_dir: Path, *, manifest: object | None = None) -> Path:
    """Build a zip carrying a manifest for `source_dir`, or `manifest` verbatim when given."""
    if manifest is None:
        manifest = {
            "schema": 1,
            "input_roots": sorted(gate.INPUT_ROOTS),
            "sources": gate.current_source_digests(source_dir),
        }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("index.js", "export const handler = () => {};\n")
        if manifest is not _OMIT:
            body = manifest if isinstance(manifest, str) else json.dumps(manifest)
            archive.writestr(gate.MANIFEST_NAME, body)
    return path


_OMIT = object()


class TestParityWithThePackagingScript:
    """The gate's constants agree with the packaging script's."""

    def test_manifest_filename_matches(self) -> None:
        assert _shell_constant("BUILD_MANIFEST") == gate.MANIFEST_NAME

    def test_input_roots_match(self) -> None:
        assert sorted(gate.INPUT_ROOTS) == sorted(_shell_array("MANIFEST_INPUT_PATHS"))

    def test_default_build_dir_is_the_one_terraform_reads(self) -> None:
        """The default build dir is `infra/build/lambda`, matching the packaging script's default."""
        assert gate.DEFAULT_BUILD_DIR == REPO_ROOT / "infra" / "build" / "lambda"
        assert gate.DEFAULT_BUILD_DIR.as_posix().endswith(_shell_constant("DEFAULT_BUILD_DIR"))

    def test_the_zip_name_matches(self) -> None:
        assert _shell_constant("ZIP_NAME") == gate.RELAY_ZIP_NAME


class TestCurrentSourceDigests:
    def test_covers_every_input_root(self, tmp_path: Path) -> None:
        _write_source_tree(tmp_path)
        digests = gate.current_source_digests(tmp_path)
        assert set(digests) == {
            "src/index.ts",
            "package.json",
            "package-lock.json",
            "tsconfig.json",
            "tsconfig.build.json",
        }

    def test_excludes_node_modules(self, tmp_path: Path) -> None:
        """Source digests exclude every `node_modules` path."""
        _write_source_tree(tmp_path)
        (tmp_path / "src" / "node_modules" / "x").mkdir(parents=True)
        (tmp_path / "src" / "node_modules" / "x" / "i.js").write_text("1", encoding="utf-8")
        assert not [p for p in gate.current_source_digests(tmp_path) if "node_modules" in p]


class TestGateIsNotVacuous:
    def test_a_matching_zip_passes(self, tmp_path: Path) -> None:
        source = tmp_path / "relay"
        _write_source_tree(source)
        zip_path = _zip_with_manifest(tmp_path / "relay.zip", source)
        result = gate.check(zip_path, require_zip=True, source_dir=source)
        assert result.status == "pass", result.detail
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda s: (s / "src" / "index.ts").write_text("changed\n"), id="changed"),
            pytest.param(lambda s: (s / "src" / "new.ts").write_text("added\n"), id="added"),
            pytest.param(lambda s: (s / "tsconfig.json").unlink(), id="removed"),
        ],
    )
    def test_any_source_change_fails_and_is_named(self, tmp_path: Path, mutate: object) -> None:
        source = tmp_path / "relay"
        _write_source_tree(source)
        zip_path = _zip_with_manifest(tmp_path / "relay.zip", source)
        mutate(source)  # type: ignore[operator]
        result = gate.check(zip_path, require_zip=True, source_dir=source)
        assert result.status == "fail", result.detail
        assert result.exit_code == 1
        assert re.search(r"\.ts|tsconfig", result.detail), result.detail

    def test_a_zip_without_a_manifest_fails_closed(self, tmp_path: Path) -> None:
        """A zip without a manifest fails and the detail names the manifest."""
        source = tmp_path / "relay"
        _write_source_tree(source)
        zip_path = _zip_with_manifest(tmp_path / "relay.zip", source, manifest=_OMIT)
        result = gate.check(zip_path, require_zip=True, source_dir=source)
        assert result.status == "fail"
        assert gate.MANIFEST_NAME in result.detail

    @pytest.mark.parametrize(
        "manifest",
        [
            pytest.param("not json at all", id="malformed"),
            pytest.param([1, 2, 3], id="not-an-object"),
            pytest.param({"schema": 1, "sources": {}}, id="empty-sources"),
            pytest.param({"schema": 1}, id="no-sources-key"),
            pytest.param({"schema": 1, "sources": {"a": 1}}, id="non-string-digest"),
        ],
    )
    def test_an_unusable_manifest_fails(self, tmp_path: Path, manifest: object) -> None:
        source = tmp_path / "relay"
        _write_source_tree(source)
        zip_path = _zip_with_manifest(tmp_path / "relay.zip", source, manifest=manifest)
        result = gate.check(zip_path, require_zip=True, source_dir=source)
        assert result.status == "fail", result.detail

    def test_an_absent_zip_fails_under_require_zip(self, tmp_path: Path) -> None:
        source = tmp_path / "relay"
        _write_source_tree(source)
        result = gate.check(tmp_path / "absent.zip", require_zip=True, source_dir=source)
        assert result.status == "fail"
        assert result.exit_code == 1

    def test_an_absent_zip_without_require_zip_skips_rather_than_passing(
        self, tmp_path: Path
    ) -> None:
        """An absent zip without `--require-zip` is a skip with exit 3, not a pass."""
        source = tmp_path / "relay"
        _write_source_tree(source)
        result = gate.check(tmp_path / "absent.zip", require_zip=False, source_dir=source)
        assert result.status == "skip", result.detail
        assert result.exit_code == gate.GATE_SKIP_EXIT_STATUS == 3
        assert result.exit_code != 0, "a skip must not be indistinguishable from a pass"

    def test_the_same_absence_is_a_failure_under_require_zip(self, tmp_path: Path) -> None:
        """The same absent zip is a skip without `--require-zip` and a fail with it."""
        source = tmp_path / "relay"
        _write_source_tree(source)
        absent = tmp_path / "absent.zip"
        assert gate.check(absent, require_zip=False, source_dir=source).status == "skip"
        assert gate.check(absent, require_zip=True, source_dir=source).status == "fail"

    def test_an_absent_source_tree_verifies_nothing_and_is_not_a_pass(self, tmp_path: Path) -> None:
        """An absent source tree is a skip with exit 3."""
        result = gate.check(tmp_path / "any.zip", require_zip=True, source_dir=tmp_path / "nope")
        assert result.status == "skip"
        assert result.exit_code == gate.GATE_SKIP_EXIT_STATUS == 3
        assert result.exit_code != 0


class TestCommandLine:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )

    def test_exit_code_reaches_the_shell(self, tmp_path: Path) -> None:
        """The CLI exits 1 when a required zip is absent."""
        result = self._run("--require-zip", "--zip", str(tmp_path / "absent.zip"))
        assert result.returncode == 1, result.stdout + result.stderr

    def test_json_output_is_machine_readable(self) -> None:
        result = self._run("--json")
        assert result.returncode in (0, 1, 3)
        payload = json.loads(result.stdout)
        assert "status" in payload

    def test_help_does_not_require_a_zip(self) -> None:
        result = self._run("--help")
        assert result.returncode == 0
        assert "--require-zip" in result.stdout
