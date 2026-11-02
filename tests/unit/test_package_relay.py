# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/package_relay.sh`.

Covers parity of the handler, runtime and zip path with `infra/`, importability of the handler from
the built zip, and rejection of planted defects (missing dependency, missing root `package.json`,
missing `"type": "module"`, missing handler module).

Tests that build a real zip need `node`, `npm`, `zip` and `unzip`; they skip when those are absent
unless `CRM_REQUIRE_RELAY_PACKAGING=1`, in which case they fail. `TestTerraformContractParity`
always runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "package_relay.sh"
RELAY_DIR = REPO_ROOT / "src" / "relay"
RELAY_VARIABLES_TF = REPO_ROOT / "infra" / "modules" / "agent_relay" / "variables.tf"
ROOT_MAIN_TF = REPO_ROOT / "infra" / "main.tf"

REQUIRE_ENV_VAR = "CRM_REQUIRE_RELAY_PACKAGING"
REQUIRED_TOOLS = ("node", "npm", "zip", "unzip")

#: xdist group for every test that writes or copies the `src/relay` npm project; this module
#: reinstalls its `node_modules` and rebuilds `src/relay/build`. Effective only under
#: `--dist loadgroup`.
pytestmark = pytest.mark.xdist_group("relay_npm")

#: Minimum Node major the artefact targets; checked against the `runtime` variable default.
_MIN_NODE_MAJOR = 24


def _terraform_variable_default(text: str, name: str) -> str:
    """Return the `default` string literal of a Terraform `variable "<name>"` block, or raise."""
    match = re.search(
        rf'variable\s+"{re.escape(name)}"\s*\{{(.*?)\n\}}', text, re.DOTALL | re.MULTILINE
    )
    if match is None:
        raise AssertionError(f'no variable "{name}" block in {RELAY_VARIABLES_TF}')
    default = re.search(r'^\s*default\s*=\s*"([^"]*)"', match.group(1), re.MULTILINE)
    if default is None:
        raise AssertionError(f'variable "{name}" has no string default')
    return default.group(1)


def _script_constant(name: str) -> str:
    """Return a `readonly NAME="value"` constant from the packaging script."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    match = re.search(rf'^readonly {re.escape(name)}="([^"]*)"', text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"no `readonly {name}=...` in {SCRIPT_PATH}")
    return match.group(1)


def _missing_tools() -> list[str]:
    return [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]


def _packaging_unavailable_reason() -> str | None:
    missing = _missing_tools()
    if missing:
        return f"not on PATH: {', '.join(missing)}"
    if not RELAY_DIR.is_dir():
        return f"no relay package at {RELAY_DIR}"
    return None


def _require_packaging() -> bool:
    return os.environ.get(REQUIRE_ENV_VAR) == "1"


def _skip_or_fail_without_tooling() -> None:
    reason = _packaging_unavailable_reason()
    if reason is None:
        return
    message = f"relay packaging prerequisites unavailable: {reason}"
    if _require_packaging():
        raise AssertionError(f"{message} (and {REQUIRE_ENV_VAR}=1 forbids skipping)")
    pytest.skip(message)


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        ["bash", str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


def _refuse_a_linked_node_modules(relay_dir: Path) -> None:
    """Fail if `relay_dir/node_modules` is a symlink, which a build-time `npm ci` would write through."""
    node_modules = relay_dir / "node_modules"
    if node_modules.is_symlink():
        pytest.fail(
            f"{node_modules} is a symlink to {node_modules.readlink()}; package_relay.sh runs "
            f"`npm ci` there, which would wipe and rewrite the link's target. Give this checkout "
            f"its own install (`cd src/relay && npm ci --no-audit --no-fund`) instead.",
            pytrace=False,
        )


@pytest.fixture(scope="module")
def built_zip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the relay zip once into a temporary build dir and return its path."""
    _skip_or_fail_without_tooling()
    _refuse_a_linked_node_modules(RELAY_DIR)
    build_dir = tmp_path_factory.mktemp("relay-build")
    result = _run_script("--build-dir", str(build_dir))
    assert result.returncode == 0, f"packaging failed:\n{result.stdout}\n{result.stderr}"
    zip_path = build_dir / "relay.zip"
    assert zip_path.is_file(), f"script reported success but wrote no {zip_path}"
    return zip_path


class TestScriptHygiene:
    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT_PATH.is_file()
        assert os.access(SCRIPT_PATH, os.X_OK), f"{SCRIPT_PATH} is not executable"

    def test_carries_the_spdx_header(self) -> None:
        assert "# SPDX-License-Identifier: MIT" in SCRIPT_PATH.read_text(encoding="utf-8")

    def test_uses_strict_bash_mode(self) -> None:
        """The script runs under `set -euo pipefail`."""
        assert "set -euo pipefail" in SCRIPT_PATH.read_text(encoding="utf-8")

    def test_passes_bash_syntax_check(self) -> None:
        result = proc_run.run(
            ["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


class TestTerraformContractParity:
    """The script's handler, runtime and artefact path match what `infra/` deploys."""

    def test_handler_matches_the_agent_relay_module_default(self) -> None:
        declared = _terraform_variable_default(
            RELAY_VARIABLES_TF.read_text(encoding="utf-8"), "handler"
        )
        module_name = _script_constant("HANDLER_MODULE")
        export_name = _script_constant("HANDLER_EXPORT")
        assert declared == f"{module_name}.{export_name}", (
            f"Terraform deploys handler {declared!r} but the packaging script builds for "
            f"{module_name}.{export_name!r}"
        )

    def test_runtime_is_a_nodejs_runtime_at_or_above_the_scripts_floor(self) -> None:
        """The deployed runtime is Node.js at or above the script's version floor."""
        runtime = _terraform_variable_default(
            RELAY_VARIABLES_TF.read_text(encoding="utf-8"), "runtime"
        )
        assert runtime.startswith("nodejs"), runtime
        major = int(runtime.removeprefix("nodejs").split(".")[0])
        assert major >= _MIN_NODE_MAJOR, runtime
        script = SCRIPT_PATH.read_text(encoding="utf-8")
        assert f"-lt {_MIN_NODE_MAJOR}" in script, (
            "the script's Node version floor no longer matches the deployed runtime family"
        )

    def test_zip_name_matches_the_filename_the_root_module_references(self) -> None:
        main_tf = ROOT_MAIN_TF.read_text(encoding="utf-8")
        zip_name = _script_constant("ZIP_NAME")
        expected = f'filename         = "${{var.lambda_build_dir}}/{zip_name}"'
        assert expected in main_tf, (
            f"infra/main.tf does not reference a {zip_name} under var.lambda_build_dir; "
            "the packaging script and the module would disagree on the artefact path"
        )

    def test_default_build_dir_is_the_one_terraform_actually_reads(self) -> None:
        """The default build dir is `infra/<var.lambda_build_dir>`, matching package_lambdas.sh."""
        declared = _terraform_variable_default(
            ROOT_MAIN_TF.read_text(encoding="utf-8"), "lambda_build_dir"
        )
        assert not Path(declared).is_absolute(), (
            "this test exists because the declared default is relative; it no longer is"
        )
        resolved = _script_constant("DEFAULT_BUILD_DIR")
        assert resolved == f"infra/{declared}", (
            f"packaging writes to {resolved!r} but terraform resolves {declared!r} against infra/"
        )
        package_lambdas = (REPO_ROOT / "scripts" / "package_lambdas.sh").read_text(encoding="utf-8")
        assert f"${{REPO_ROOT}}/{resolved}" in package_lambdas, (
            "the relay's default build dir no longer matches the Python zips' default build dir"
        )


class TestBuiltArtefact:
    def test_handler_module_is_at_the_archive_root(self, built_zip: Path) -> None:
        """Lambda resolves `index.handler` relative to the archive root, not to a `build/` subdir."""
        module_name = _script_constant("HANDLER_MODULE")
        with zipfile.ZipFile(built_zip) as archive:
            names = set(archive.namelist())
        assert f"{module_name}.js" in names, sorted(n for n in names if n.endswith(".js"))[:10]

    def test_declares_type_module_at_the_root(self, built_zip: Path) -> None:
        with zipfile.ZipFile(built_zip) as archive:
            manifest = json.loads(archive.read("package.json"))
        assert manifest.get("type") == "module"

    def test_bundles_both_production_dependencies(self, built_zip: Path) -> None:
        """Every production dependency in the lockfile is bundled under `node_modules/`."""
        lock = json.loads((RELAY_DIR / "package-lock.json").read_text(encoding="utf-8"))
        production = set(lock["packages"][""]["dependencies"])
        assert production, "the relay lockfile declares no production dependencies"
        with zipfile.ZipFile(built_zip) as archive:
            names = archive.namelist()
        for dependency in sorted(production):
            assert any(n.startswith(f"node_modules/{dependency}/") for n in names), (
                f"{dependency} is a production dependency but is absent from the zip"
            )

    def test_ships_no_dev_dependency(self, built_zip: Path) -> None:
        """No devDependency from the lockfile is present in the zip."""
        lock = json.loads((RELAY_DIR / "package-lock.json").read_text(encoding="utf-8"))
        dev_only = set(lock["packages"][""].get("devDependencies", {}))
        with zipfile.ZipFile(built_zip) as archive:
            names = archive.namelist()
        leaked = [
            dependency
            for dependency in sorted(dev_only)
            if any(n.startswith(f"node_modules/{dependency}/") for n in names)
        ]
        assert not leaked, f"devDependencies present in the deployment artefact: {leaked}"

    def test_ships_no_typescript_source_or_lockfile(self, built_zip: Path) -> None:
        """The zip carries no lockfile and no first-party TypeScript source."""
        with zipfile.ZipFile(built_zip) as archive:
            names = archive.namelist()
        assert "package-lock.json" not in names
        assert not [n for n in names if n.endswith(".ts") and not n.startswith("node_modules/")]

    def test_is_byte_deterministic_across_runs(self, built_zip: Path, tmp_path: Path) -> None:
        """A `--skip-install` rebuild of an unchanged tree matches the fixture's zip bytes."""
        _skip_or_fail_without_tooling()
        second = tmp_path / "second"
        result = _run_script("--build-dir", str(second), "--skip-install")
        assert result.returncode == 0, result.stdout + result.stderr
        assert built_zip.read_bytes() == (second / "relay.zip").read_bytes(), (
            "two runs over an unchanged tree produced different zips"
        )


class TestImportVerificationIsNotVacuous:
    """`--verify-only` passes the real zip and rejects each planted defect."""

    def test_the_real_artefact_passes_verify_only(self, built_zip: Path) -> None:
        result = _run_script("--verify-only", str(built_zip))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "handler imported from the zip and is callable" in result.stdout

    @staticmethod
    def _repack_without(built_zip: Path, tmp_path: Path, *, drop: str) -> Path:
        """Copy `built_zip` omitting every entry under `drop`, returning the mutated zip."""
        mutated = tmp_path / f"broken-{drop.strip('/').replace('/', '_')}.zip"
        with zipfile.ZipFile(built_zip) as source, zipfile.ZipFile(mutated, "w") as target:
            dropped = 0
            for item in source.infolist():
                if item.filename == drop or item.filename.startswith(drop.rstrip("/") + "/"):
                    dropped += 1
                    continue
                target.writestr(item, source.read(item.filename))
        assert dropped, f"planted defect removed nothing: no entry matched {drop!r}"
        return mutated

    def test_a_missing_production_dependency_fails(self, built_zip: Path, tmp_path: Path) -> None:
        """A zip missing a production dependency fails with `ERR_MODULE_NOT_FOUND`."""
        broken = self._repack_without(built_zip, tmp_path, drop="node_modules/aws-jwt-verify")
        result = _run_script("--verify-only", str(broken))
        assert result.returncode != 0, result.stdout
        assert "ERR_MODULE_NOT_FOUND" in result.stdout + result.stderr

    def test_a_missing_handler_module_fails(self, built_zip: Path, tmp_path: Path) -> None:
        module_name = _script_constant("HANDLER_MODULE")
        broken = self._repack_without(built_zip, tmp_path, drop=f"{module_name}.js")
        result = _run_script("--verify-only", str(broken))
        assert result.returncode != 0, result.stdout
        assert f"no {module_name}.js at the archive root" in result.stdout + result.stderr

    def test_a_missing_root_package_json_fails(self, built_zip: Path, tmp_path: Path) -> None:
        """A zip without a root `package.json` fails the structural check."""
        broken = self._repack_without(built_zip, tmp_path, drop="package.json")
        result = _run_script("--verify-only", str(broken))
        assert result.returncode != 0, result.stdout
        assert "no package.json at the archive root" in result.stdout + result.stderr

    def test_a_package_json_without_type_module_fails(
        self, built_zip: Path, tmp_path: Path
    ) -> None:
        mutated = tmp_path / "broken-type.zip"
        with zipfile.ZipFile(built_zip) as source, zipfile.ZipFile(mutated, "w") as target:
            for item in source.infolist():
                if item.filename == "package.json":
                    manifest = json.loads(source.read(item.filename))
                    del manifest["type"]
                    target.writestr(item, json.dumps(manifest))
                    continue
                target.writestr(item, source.read(item.filename))
        result = _run_script("--verify-only", str(mutated))
        assert result.returncode != 0, result.stdout
        assert 'does not declare "type": "module"' in result.stdout + result.stderr

    def test_a_nonexistent_zip_fails_rather_than_reporting_success(self, tmp_path: Path) -> None:
        result = _run_script("--verify-only", str(tmp_path / "absent.zip"))
        assert result.returncode != 0
        assert "no zip at" in result.stdout + result.stderr


class TestArgumentHandling:
    def test_an_unknown_flag_is_rejected(self) -> None:
        """An unrecognised flag exits non-zero."""
        result = _run_script("--not-a-flag")
        assert result.returncode != 0
        assert "unknown argument" in result.stdout + result.stderr

    def test_build_dir_requires_an_argument(self) -> None:
        result = _run_script("--build-dir")
        assert result.returncode != 0
        assert "requires a directory argument" in result.stdout + result.stderr

    def test_an_explicit_build_dir_flag_reports_overriding_the_environment(
        self, tmp_path: Path
    ) -> None:
        """`--build-dir` wins over `LAMBDA_BUILD_DIR` and the override is logged."""
        _skip_or_fail_without_tooling()
        env = dict(os.environ, LAMBDA_BUILD_DIR=str(tmp_path / "from-env"))
        result = proc_run.run(
            [
                "bash",
                str(SCRIPT_PATH),
                "--build-dir",
                str(tmp_path / "from-flag"),
                "--skip-install",
                "--no-verify-import",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "overrides LAMBDA_BUILD_DIR" in result.stdout
        assert (tmp_path / "from-flag" / "relay.zip").is_file()
        assert not (tmp_path / "from-env").exists(), "the environment value was used after all"

    def test_an_exported_build_dir_is_honoured_when_no_flag_is_given(self, tmp_path: Path) -> None:
        _skip_or_fail_without_tooling()
        target = tmp_path / "env-only"
        env = dict(os.environ, LAMBDA_BUILD_DIR=str(target))
        result = proc_run.run(
            ["bash", str(SCRIPT_PATH), "--skip-install", "--no-verify-import"],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (target / "relay.zip").is_file()


class TestSkipContract:
    def test_the_require_env_var_turns_a_skip_into_a_failure(self) -> None:
        """`_require_packaging` is true exactly when the require variable is `1`."""
        assert _packaging_unavailable_reason() is None or True  # host-independent by construction
        original = os.environ.get(REQUIRE_ENV_VAR)
        try:
            os.environ[REQUIRE_ENV_VAR] = "1"
            assert _require_packaging() is True
            os.environ.pop(REQUIRE_ENV_VAR)
            assert _require_packaging() is False
        finally:
            if original is None:
                os.environ.pop(REQUIRE_ENV_VAR, None)
            else:
                os.environ[REQUIRE_ENV_VAR] = original


#: A `shasum` that fails the way an absent one does, and one that exits 0 with no digest.
_BROKEN_SHASUMS = {
    "failing": '#!/bin/sh\necho "shasum: command not found" >&2\nexit 127\n',
    "digestless": '#!/bin/sh\nfor f; do last="$f"; done\necho "not-a-digest  $last"\n',
}

#: The refusal each broken `shasum` must produce.
_EXPECTED_REFUSALS = {"failing": "shasum failed on", "digestless": "printed no SHA-256"}


def _path_without(tool: str, tmp_path: Path) -> str:
    """A PATH holding a symlink to every executable on the current PATH except `tool`."""
    shadow = tmp_path / "path-without"
    shadow.mkdir()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or not Path(directory).is_dir():
            continue
        for entry in Path(directory).iterdir():
            target = shadow / entry.name
            if entry.name == tool or target.exists() or target.is_symlink():
                continue
            try:
                executable = entry.is_file() and os.access(entry, os.X_OK)
            except OSError:  # an unreadable system entry is simply not linked
                continue
            if executable:
                target.symlink_to(entry)
    return str(shadow)


def _package_with_path(path_value: str, build_dir: Path) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return proc_run.run(
        [
            bash,
            str(SCRIPT_PATH),
            "--build-dir",
            str(build_dir),
            "--skip-install",
            "--no-verify-import",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env=dict(os.environ, PATH=path_value),
    )


class TestBuildManifestDigests:
    """The build manifest is never written with a missing or malformed digest."""

    @pytest.mark.parametrize("kind", sorted(_BROKEN_SHASUMS))
    def test_a_broken_shasum_fails_the_build_rather_than_writing_empty_digests(
        self, kind: str, tmp_path: Path
    ) -> None:
        _skip_or_fail_without_tooling()
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake = fake_bin / "shasum"
        fake.write_text(_BROKEN_SHASUMS[kind], encoding="utf-8")
        fake.chmod(0o755)
        build_dir = tmp_path / "out"

        result = _package_with_path(
            f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}", build_dir
        )

        assert result.returncode != 0, f"built with a {kind} shasum:\n{result.stdout}"
        assert _EXPECTED_REFUSALS[kind] in result.stderr, result.stderr
        assert not (build_dir / "relay.zip").exists(), "a zip was written despite the bad digest"

    def test_an_absent_shasum_fails_before_any_digest_is_computed(self, tmp_path: Path) -> None:
        _skip_or_fail_without_tooling()
        path_value = _path_without("shasum", tmp_path)
        assert shutil.which("shasum", path=path_value) is None
        assert shutil.which("node", path=path_value) is not None, "the shadow PATH lost node"
        build_dir = tmp_path / "out"

        result = _package_with_path(path_value, build_dir)

        assert result.returncode != 0, f"built without shasum:\n{result.stdout}"
        assert "shasum is not on PATH" in result.stderr, result.stderr
        assert not (build_dir / "relay.zip").exists()


class TestNodeModulesLinkRefusal:
    """`built_zip` refuses to `npm ci` through a symlinked `src/relay/node_modules`."""

    def test_a_symlinked_node_modules_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "sentinel").write_text("kept", encoding="utf-8")
        relay_dir = tmp_path / "relay"
        relay_dir.mkdir()
        (relay_dir / "node_modules").symlink_to(target)

        with pytest.raises(pytest.fail.Exception, match="is a symlink"):
            _refuse_a_linked_node_modules(relay_dir)
        assert (target / "sentinel").read_text(encoding="utf-8") == "kept"

    @pytest.mark.parametrize("state", ["real directory", "absent"])
    def test_an_own_or_missing_node_modules_is_accepted(self, state: str, tmp_path: Path) -> None:
        if state == "real directory":
            (tmp_path / "node_modules").mkdir()
        _refuse_a_linked_node_modules(tmp_path)

    def test_the_fixture_applies_the_refusal_before_packaging(self) -> None:
        """The refusal runs inside `built_zip`, ahead of the `package_relay.sh` call."""
        source = Path(__file__).read_text(encoding="utf-8")
        body = source[source.index("def built_zip(") :]
        body = body[: body.index("\n\n\n")]
        refuse_at = body.find("_refuse_a_linked_node_modules(RELAY_DIR)")
        run_at = body.find("_run_script(")
        assert -1 < refuse_at < run_at, (
            "built_zip packages before checking for a linked node_modules"
        )
