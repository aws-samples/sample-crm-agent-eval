# SPDX-License-Identifier: MIT
"""Tests for the license-header and toolspec-parity gates, plus registration and disposition tests
for the gates `scripts/run_gates.py` aggregates.

`scripts/check_cost_withheld.sh` is tested in `tests/unit/test_cost_withheld_gate.py`; here it is
only smoke-checked. Violating fixtures are built in scratch repositories or scratch copies, never
in the working tree.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_LICENSE_HEADERS = REPO_ROOT / "scripts" / "check_license_headers.sh"
#: Placeholder AWS credential for a subprocess that must not reach a real account; not a secret.
_UNREAL_CRED = "notreal"
CHECK_TOOLSPEC_PARITY = REPO_ROOT / "scripts" / "check_toolspec_parity.py"
CHECK_COST_WITHHELD = REPO_ROOT / "scripts" / "check_cost_withheld.sh"
CHECK_NO_ACCOUNT_ID = REPO_ROOT / "scripts" / "check_no_account_id.sh"


def _run_license_gate(root: Path, *subject: str) -> subprocess.CompletedProcess[str]:
    """Runs the license gate against `root` with an explicitly named subject."""
    return proc_run.run(
        ["bash", str(CHECK_LICENSE_HEADERS), *subject],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_parity_gate(root: Path, *, skip_frontend: bool = True) -> subprocess.CompletedProcess[str]:
    script = root / "scripts" / "check_toolspec_parity.py"
    args = [sys.executable, str(script)]
    if skip_frontend:
        args.append("--skip-frontend")
    return proc_run.run(
        args,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_scratch_repo(root: Path) -> None:
    proc_run.run(["git", "init", "-q"], cwd=root, check=True)
    proc_run.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    proc_run.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _stage_all(root: Path) -> None:
    proc_run.run(["git", "add", "-A"], cwd=root, check=True)


def _synthetic_account_id() -> str:
    """Return an account-id-shaped digit run for a scratch-repo violation fixture.

    Assembled at runtime because `check_no_account_id` scans this file.
    """
    return "374615" + "928403"


def _spdx_line() -> str:
    """Return the SPDX header line for a scratch-repo probe.

    Assembled at runtime because `check_license_headers.sh` scans this file.
    """
    return "SPDX-License-" + "Identifier: MIT"


def _gate_placeholder_account_ids() -> frozenset[str]:
    """Read `check_no_account_id.sh`'s `_PLACEHOLDER_ACCOUNT_IDS` alternation into a set."""
    source = CHECK_NO_ACCOUNT_ID.read_text(encoding="utf-8")
    match = re.search(r"^_PLACEHOLDER_ACCOUNT_IDS='\(([^)]+)\)'", source, re.MULTILINE)
    assert match is not None, "could not locate _PLACEHOLDER_ACCOUNT_IDS in check_no_account_id.sh"
    return frozenset(match.group(1).split("|"))


def _gate_benign_shapes() -> tuple[str, ...]:
    """Variable names inside `check_no_account_id.sh`'s `_BENIGN_SHAPES` array, in order.

    An empty tuple means the array could not be parsed.
    """
    source = CHECK_NO_ACCOUNT_ID.read_text(encoding="utf-8")
    match = re.search(r"^_BENIGN_SHAPES=\((.*?)^\)$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        return ()
    return tuple(re.findall(r'"\$(\w+)"', match.group(1)))


def _tracked_python_and_shell_sources() -> list[Path]:
    """Every tracked `*.py`/`*.sh` path, resolved through git."""
    listing = proc_run.run(
        ["git", "ls-files", "-z", "*.py", "*.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / name for name in listing.stdout.split("\0") if name]


# ---------------------------------------------------------------------------
# check_license_headers.sh
# ---------------------------------------------------------------------------


def test_license_headers_script_is_executable() -> None:
    assert CHECK_LICENSE_HEADERS.exists()
    assert CHECK_LICENSE_HEADERS.stat().st_mode & 0o111, "script must be executable"


class TestLicenseHeaderGateAgainstScratchRepo:
    """License-gate cases, each built in an isolated scratch git repository."""

    def _make_compliant_repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        (root / "src" / "crm").mkdir(parents=True)
        (root / "scripts").mkdir(parents=True)
        (root / "tests" / "unit").mkdir(parents=True)
        (root / "src" / "crm" / "widget.py").write_text(
            '# SPDX-License-Identifier: MIT\n"""A widget."""\n', encoding="utf-8"
        )
        (root / "scripts" / "run.sh").write_text(
            "#!/usr/bin/env bash\n# SPDX-License-Identifier: MIT\necho hi\n", encoding="utf-8"
        )
        (root / "tests" / "unit" / "__init__.py").write_text(
            "# SPDX-License-Identifier: MIT\n", encoding="utf-8"
        )
        (root / "tests" / "unit" / "test_widget.py").write_text(
            '# SPDX-License-Identifier: MIT\n"""Tests."""\n', encoding="utf-8"
        )
        _init_scratch_repo(root)
        _stage_all(root)
        return root

    def test_compliant_tree_passes(self, tmp_path: Path) -> None:
        root = self._make_compliant_repo(tmp_path)
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_python_file_missing_header_fails(self, tmp_path: Path) -> None:
        root = self._make_compliant_repo(tmp_path)
        (root / "src" / "crm" / "unheadered.py").write_text(
            '"""No header here."""\n', encoding="utf-8"
        )
        _stage_all(root)
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 1
        assert "src/crm/unheadered.py" in result.stderr
        assert "FAILED" in result.stderr

    def test_shell_script_missing_header_fails(self, tmp_path: Path) -> None:
        root = self._make_compliant_repo(tmp_path)
        (root / "scripts" / "unheadered.sh").write_text(
            "#!/usr/bin/env bash\necho hi\n", encoding="utf-8"
        )
        _stage_all(root)
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 1
        assert "scripts/unheadered.sh" in result.stderr

    def test_infra_shell_script_missing_header_fails(self, tmp_path: Path) -> None:
        root = self._make_compliant_repo(tmp_path)
        (root / "infra").mkdir()
        (root / "infra" / "bootstrap.sh").write_text(
            "#!/usr/bin/env bash\necho hi\n", encoding="utf-8"
        )
        _stage_all(root)
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 1
        assert "infra/bootstrap.sh" in result.stderr

    def test_empty_init_py_missing_header_fails(self, tmp_path: Path) -> None:
        """An empty `__init__.py` package marker is in scope and needs a header like any file."""
        root = self._make_compliant_repo(tmp_path)
        (root / "src" / "crm" / "__init__.py").write_text("", encoding="utf-8")
        _stage_all(root)
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "src/crm/__init__.py" in result.stderr

    def test_non_empty_init_py_is_not_exempt(self, tmp_path: Path) -> None:
        """An `__init__.py` carrying real code (e.g. re-exports) is not exempt merely by name."""
        root = self._make_compliant_repo(tmp_path)
        (root / "src" / "crm" / "__init__.py").write_text(
            "from .widget import Widget\n", encoding="utf-8"
        )
        _stage_all(root)
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 1
        assert "src/crm/__init__.py" in result.stderr

    def test_tf_and_root_level_ts_files_are_out_of_declared_scope(self, tmp_path: Path) -> None:
        """`.tf` files and `.ts` files outside the declared directories do not fail the gate."""
        root = self._make_compliant_repo(tmp_path)
        (root / "infra").mkdir()
        (root / "infra" / "main.tf").write_text(
            "# no header, by design out of scope\n", encoding="utf-8"
        )
        (root / "frontend_src.ts").write_text(
            "// no header, by design out of scope\n", encoding="utf-8"
        )
        _stage_all(root)
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "relpath",
        [
            "frontend/src/App.tsx",
            "frontend/src/api/client.ts",
            "frontend/src/vite-env.d.ts",
            "frontend/eslint.config.js",
            "frontend/scripts/generate-types.mjs",
            "frontend/tailwind.config.cjs",
            "tests/e2e/browser/journeys/01-signin.spec.ts",
            "tests/e2e/browser/fixtures/helper.mjs",
            "src/relay/src/index.ts",
            "src/relay/scripts/gen-events.mjs",
            "githooks/pre-commit",
        ],
    )
    def test_extended_scope_file_is_gated(self, tmp_path: Path, relpath: str) -> None:
        """Each extended-scope area fails on a header-less file and passes once it is headed."""
        root = self._make_compliant_repo(tmp_path)
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export const x = 1;\n", encoding="utf-8")
        _stage_all(root)
        caught = _run_license_gate(root, "--worktree")
        assert caught.returncode == 1, caught.stdout + caught.stderr
        assert relpath in caught.stderr

        target.write_text(f"// {_spdx_line()}\nexport const x = 1;\n", encoding="utf-8")
        _stage_all(root)
        passed = _run_license_gate(root, "--worktree")
        assert passed.returncode == 0, passed.stdout + passed.stderr

    @pytest.mark.parametrize(
        "relpath",
        [
            "frontend/src/types/generated/toolspec.ts",
            "frontend/node_modules/pkg/index.js",
            "src/relay/build/index.js",
        ],
    )
    def test_gitignored_output_in_extended_scope_is_not_scanned(
        self, tmp_path: Path, relpath: str
    ) -> None:
        """A gitignored header-less file under an extended-scope directory does not fail the gate."""
        root = self._make_compliant_repo(tmp_path)
        (root / ".gitignore").write_text(
            "node_modules/\nbuild/\nfrontend/src/types/generated/\n", encoding="utf-8"
        )
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export const x = 1;\n", encoding="utf-8")
        _stage_all(root)
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_untracked_extended_scope_file_missing_header_fails(self, tmp_path: Path) -> None:
        """An untracked, non-ignored frontend file is scanned under `--worktree` only."""
        root = self._make_compliant_repo(tmp_path)
        (root / "frontend" / "src").mkdir(parents=True)
        (root / "frontend" / "src" / "loose.tsx").write_text("export {};\n", encoding="utf-8")
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "frontend/src/loose.tsx" in result.stderr
        assert _run_license_gate(root, "--cached").returncode == 0

    def test_untracked_file_missing_header_fails_the_gate(self, tmp_path: Path) -> None:
        """Untracked in-scope content is scanned too, and a missing header fails the gate."""
        root = self._make_compliant_repo(tmp_path)
        (root / "src" / "crm" / "untracked.py").write_text('"""Not staged."""\n', encoding="utf-8")
        result = _run_license_gate(root, "--worktree")
        assert result.returncode == 1, result.stderr
        assert "src/crm/untracked.py" in result.stdout + result.stderr


class TestLicenseHeaderGateNamesItsSubject:
    """`--cached` judges staged content and `--worktree` judges the working tree.

    Probe content is built at runtime because this gate scans this file.
    """

    def _repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        (root / "src" / "crm").mkdir(parents=True)
        (root / "src" / "crm" / "widget.py").write_text(f"# {_spdx_line()}\n", encoding="utf-8")
        _init_scratch_repo(root)
        _stage_all(root)
        return root

    def test_no_subject_named_is_a_usage_error_not_a_pass(self, tmp_path: Path) -> None:
        """Running the gate with no subject is a usage error."""
        result = _run_license_gate(self._repo(tmp_path))
        assert result.returncode == 2, result.stdout + result.stderr
        assert "subject is REQUIRED" in result.stderr

    def test_all_three_subjects_pass_on_a_compliant_tree(self, tmp_path: Path) -> None:
        """A compliant tree passes under `--worktree`, `--cached` and `--ref HEAD`."""
        root = self._repo(tmp_path)
        proc_run.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
        for subject in (("--worktree",), ("--cached",), ("--ref", "HEAD")):
            result = _run_license_gate(root, *subject)
            assert result.returncode == 0, f"{subject}: {result.stderr}"

    def test_staged_without_header_is_caught_even_when_the_worktree_copy_has_one(
        self, tmp_path: Path
    ) -> None:
        """An unheadered staged blob fails `--cached` while its headered worktree copy passes."""
        root = self._repo(tmp_path)
        probe = root / "src" / "crm" / "probe.py"
        probe.write_text("x = 1\n", encoding="utf-8")
        proc_run.run(["git", "add", "src/crm/probe.py"], cwd=root, check=True)
        probe.write_text(f"# {_spdx_line()}\nx = 1\n", encoding="utf-8")

        cached = _run_license_gate(root, "--cached")
        assert cached.returncode == 1, (
            "staged content has no header, so the subject a commit is built from is in violation; "
            f"gate said {cached.returncode}: {cached.stdout + cached.stderr}"
        )
        assert "src/crm/probe.py" in cached.stderr
        assert "staged content" in cached.stderr, "the violation must name the subject it read"

        worktree = _run_license_gate(root, "--worktree")
        assert worktree.returncode == 0, (
            "the working-tree copy does carry a header, so --worktree must pass; failing both "
            f"subjects would mean the flag selects nothing: {worktree.stdout + worktree.stderr}"
        )

    def test_cached_does_not_scan_untracked_files(self, tmp_path: Path) -> None:
        """An untracked unheadered file is not reported under `--cached`."""
        root = self._repo(tmp_path)
        (root / "src" / "crm" / "loose.py").write_text("x = 1\n", encoding="utf-8")
        assert _run_license_gate(root, "--cached").returncode == 0
        assert _run_license_gate(root, "--worktree").returncode == 1

    def test_a_path_containing_a_space_is_still_scanned(self, tmp_path: Path) -> None:
        """An untracked path containing a space is scanned and named in full.

        An unheadered one is caught; a headered one passes.
        """
        root = self._repo(tmp_path)
        (root / "src" / "crm" / "a probe.py").write_text("x = 1\n", encoding="utf-8")
        caught = _run_license_gate(root, "--worktree")
        assert caught.returncode == 1, caught.stdout + caught.stderr
        assert "a probe.py" in caught.stderr, (
            f"the spaced path must be named in full, not truncated: {caught.stderr}"
        )

        (root / "src" / "crm" / "a probe.py").write_text(
            f"# {_spdx_line()}\nx = 1\n", encoding="utf-8"
        )
        assert _run_license_gate(root, "--worktree").returncode == 0

    def test_a_non_ascii_tracked_path_is_still_scanned(self, tmp_path: Path) -> None:
        """A tracked non-ASCII path, which `git ls-files` quotes, is scanned and named in full."""
        root = self._repo(tmp_path)
        (root / "src" / "crm" / "pröbe.py").write_text("x = 1\n", encoding="utf-8")
        proc_run.run(["git", "add", "-A"], cwd=root, check=True)
        result = _run_license_gate(root, "--cached")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "pröbe.py" in result.stderr, (
            f"the non-ASCII path must be named as itself, not as an escaped literal: {result.stderr}"
        )


def test_license_header_gate_passes_against_the_real_repository() -> None:
    """The real repository passes the gate with no path exempted."""
    result = _run_license_gate(REPO_ROOT, "--worktree")
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# check_toolspec_parity.py
# ---------------------------------------------------------------------------


def test_toolspec_parity_script_exists() -> None:
    assert CHECK_TOOLSPEC_PARITY.exists()


def test_toolspec_parity_passes_against_the_real_repository_terraform_and_count() -> None:
    """The real `infra/main.tf` transcription and the real `ALL_TOOLS` count agree."""
    result = _run_parity_gate(REPO_ROOT, skip_frontend=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@pytest.mark.skipif(
    not (REPO_ROOT / "frontend" / "node_modules").exists(),
    reason="frontend/node_modules not installed in this environment",
)
def test_toolspec_parity_passes_against_the_real_repository_including_frontend() -> None:
    """The full three-consumer check, including the frontend regeneration step."""
    result = _run_parity_gate(REPO_ROOT, skip_frontend=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_tools_names_are_unique_and_include_the_named_tools() -> None:
    """`ALL_TOOLS` names are distinct and include tools other modules key on by name.

    No count is pinned; the inventory's agreement with `infra/main.tf` is the parity gate's check.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from crm.toolspec.definitions import ALL_TOOLS

    names = [tool.name for tool in ALL_TOOLS]
    assert len(set(names)) == len(names)
    assert {
        "get_page_context",
        "search_accounts",
        "get_audit_record",
        "rebase_proposal",
    } <= set(names)


def tracked_copy(source_root: Path, dest: Path) -> Path:
    """Materialise `source_root`'s tracked files (`git ls-files`, working-tree content) at `dest`.

    Untracked and ignored files are never copied; tracked symlinks stay symlinks.
    """
    listing = proc_run.run(
        ["git", "-C", str(source_root), "ls-files", "-z"], capture_output=True, check=True
    )
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        relative = os.fsdecode(raw)
        source = source_root / relative
        target = dest / relative
        if not source.is_symlink() and not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(source.readlink())
        else:
            shutil.copy2(source, target)
    return dest


def test_a_tracked_copy_holds_tracked_files_only(tmp_path: Path) -> None:
    """A tracked file is copied with its working-tree content; an untracked one is not."""
    source = tmp_path / "source"
    source.mkdir()
    _init_scratch_repo(source)
    (source / "kept").mkdir()
    (source / "kept" / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _stage_all(source)
    (source / "kept" / "tracked.txt").write_text("edited\n", encoding="utf-8")
    (source / "planted-untracked.txt").write_text("not tracked\n", encoding="utf-8")

    dest = tracked_copy(source, tmp_path / "dest")

    assert (dest / "kept" / "tracked.txt").read_text(encoding="utf-8") == "edited\n"
    assert not (dest / "planted-untracked.txt").exists()
    assert not (dest / ".git").exists()


class TestToolspecParityGateAgainstScratchCopy:
    """Each case copies the repository's tracked files to a temp directory, mutates one thing, and
    expects failure. `.venv` is symlinked in rather than copied.
    """

    def _copy_repo(self, tmp_path: Path) -> Path:
        dest = tracked_copy(REPO_ROOT, tmp_path / "repo_copy")
        venv_src = REPO_ROOT / ".venv"
        if venv_src.exists():
            (dest / ".venv").symlink_to(venv_src)
        return dest

    def test_an_unmutated_copy_passes_the_gate(self, tmp_path: Path) -> None:
        """An unmutated copy passes the gate, the control for every mutation case in this class."""
        root = self._copy_repo(tmp_path)

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 0, (
            "the scratch copy does not pass the gate unmutated, so every sibling assertion of "
            f"returncode == 1 proves nothing about its mutation: {result.stdout + result.stderr}"
        )
        assert "OK" in result.stdout, result.stdout

    def test_the_copy_omits_the_provider_cache_but_keeps_the_lock_file(
        self, tmp_path: Path
    ) -> None:
        """The copy omits the provider cache and keeps `infra/.terraform.lock.hcl`."""
        root = self._copy_repo(tmp_path)

        assert not (root / "infra" / ".terraform").exists(), (
            "infra/.terraform was copied; that is the provider cache, and copying it per test is "
            "what exhausted the host's disk"
        )
        assert (root / "infra" / ".terraform.lock.hcl").is_file(), (
            "the exclusion widened past the provider cache and took the tracked lock file with it"
        )
        assert (root / "infra" / "main.tf").is_file(), "the copy is missing what the gate parses"

    def test_wrong_handler_in_main_tf_fails(self, tmp_path: Path) -> None:
        root = self._copy_repo(tmp_path)
        main_tf = root / "infra" / "main.tf"
        text = main_tf.read_text(encoding="utf-8")
        mutated = text.replace(
            "crm.handlers.search_opportunities.handler",
            "crm.handlers.WRONG_HANDLER.handler",
            1,
        )
        assert mutated != text, "fixture setup did not actually mutate main.tf"
        main_tf.write_text(mutated, encoding="utf-8")

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 1
        assert "search_opportunities" in result.stderr
        assert "WRONG_HANDLER" in result.stderr

    def test_missing_tool_in_main_tf_locals_tools_fails(self, tmp_path: Path) -> None:
        root = self._copy_repo(tmp_path)
        main_tf = root / "infra" / "main.tf"
        text = main_tf.read_text(encoding="utf-8")
        needle = '    get_contact          = { handler = "crm.handlers.get_contact.handler", '
        pruned, removed = re.subn(rf"^{re.escape(needle)}.*\n", "", text, count=1, flags=re.M)
        assert removed == 1, (
            "fixture setup expected a specific committed line; main.tf changed shape"
        )
        main_tf.write_text(pruned, encoding="utf-8")

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 1
        assert "get_contact" in result.stderr

    def test_extra_tool_in_main_tf_locals_tools_fails(self, tmp_path: Path) -> None:
        root = self._copy_repo(tmp_path)
        main_tf = root / "infra" / "main.tf"
        text = main_tf.read_text(encoding="utf-8")
        needle = (
            '    revert_audit_record = { handler = "crm.handlers.revert_audit_record.handler", '
        )
        assert needle in text, (
            "fixture setup expected a specific committed line; main.tf changed shape"
        )
        injected = (
            '    ghost_tool = { handler = "crm.handlers.ghost_tool.handler", '
            'description = "Does not exist in ALL_TOOLS." }\n' + needle
        )
        main_tf.write_text(text.replace(needle, injected, 1), encoding="utf-8")

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 1
        assert "ghost_tool" in result.stderr

    def test_missing_tool_in_tool_schema_properties_fails(self, tmp_path: Path) -> None:
        root = self._copy_repo(tmp_path)
        main_tf = root / "infra" / "main.tf"
        text = main_tf.read_text(encoding="utf-8")
        needle = (
            "    get_contact = [\n"
            '      { name = "contact_id", type = "string", required = true },\n'
            "    ]\n"
        )
        assert needle in text, (
            "fixture setup expected a specific committed block; main.tf changed shape"
        )
        main_tf.write_text(text.replace(needle, ""), encoding="utf-8")

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 1
        assert "tool_schema_properties" in result.stderr
        assert "get_contact" in result.stderr

    def test_wrong_property_type_fails(self, tmp_path: Path) -> None:
        """A property-type drift between the toolspec and a generated consumer fails the gate."""
        root = self._copy_repo(tmp_path)
        main_tf = root / "infra" / "main.tf"
        text = main_tf.read_text(encoding="utf-8")
        needle = (
            "    get_contact = [\n"
            '      { name = "contact_id", type = "string", required = true },\n'
            "    ]\n"
        )
        assert needle in text, (
            "fixture setup expected a specific committed block; main.tf changed shape"
        )
        main_tf.write_text(
            text.replace(needle, needle.replace('type = "string"', 'type = "number"'), 1),
            encoding="utf-8",
        )

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 1
        assert "contact_id" in result.stderr
        assert "number" in result.stderr

    def test_wrong_property_required_flag_fails(self, tmp_path: Path) -> None:
        root = self._copy_repo(tmp_path)
        main_tf = root / "infra" / "main.tf"
        text = main_tf.read_text(encoding="utf-8")
        needle = '      { name = "contact_id", type = "string", required = true },\n'
        assert needle in text
        main_tf.write_text(
            text.replace(needle, '      { name = "contact_id", type = "string" },\n', 1),
            encoding="utf-8",
        )

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 1
        assert "required" in result.stderr

    def test_extra_property_in_a_tool_schema_fails(self, tmp_path: Path) -> None:
        root = self._copy_repo(tmp_path)
        main_tf = root / "infra" / "main.tf"
        text = main_tf.read_text(encoding="utf-8")
        needle = '      { name = "contact_id", type = "string", required = true },\n'
        assert needle in text
        main_tf.write_text(
            text.replace(needle, needle + '      { name = "ghost_prop", type = "string" },\n', 1),
            encoding="utf-8",
        )

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 1
        assert "ghost_prop" in result.stderr

    def test_a_duplicated_tool_name_fails(self, tmp_path: Path) -> None:
        """A second definition reusing an existing tool's name fails the uniqueness check."""
        root = self._copy_repo(tmp_path)
        definitions = root / "src" / "crm" / "toolspec" / "definitions.py"
        text = definitions.read_text(encoding="utf-8")
        needle = 'name="rebase_proposal",'
        assert text.count(needle) == 1
        definitions.write_text(text.replace(needle, 'name="reject_proposal",', 1), encoding="utf-8")

        result = _run_parity_gate(root, skip_frontend=True)
        assert result.returncode == 1
        assert "'reject_proposal' more than once" in result.stderr

    def test_the_parity_script_pins_no_tool_count(self) -> None:
        """The gate carries no tool-count literal to bump."""
        text = (REPO_ROOT / "scripts" / "check_toolspec_parity.py").read_text(encoding="utf-8")
        assert "EXPECTED_TOOL_COUNT" not in text

    @pytest.mark.skipif(
        not (REPO_ROOT / "frontend" / "node_modules").exists(),
        reason="frontend/node_modules not installed in this environment",
    )
    def test_tampered_generated_frontend_types_fails(self, tmp_path: Path) -> None:
        """A tampered `toolSchemas.ts` fails the full gate; the untampered copy first passes it.

        The generated file is gitignored, so a fresh checkout has none until a first gate run
        regenerates it; that run is also this case's control.
        """
        root = self._copy_repo(tmp_path)
        node_modules_src = REPO_ROOT / "frontend" / "node_modules"
        (root / "frontend" / "node_modules").symlink_to(node_modules_src)
        generated = root / "frontend" / "src" / "types" / "generated" / "toolSchemas.ts"
        control = _run_parity_gate(root, skip_frontend=False)
        assert control.returncode == 0, control.stdout + control.stderr
        assert generated.is_file(), "the untampered gate run did not materialise toolSchemas.ts"
        with generated.open("a", encoding="utf-8") as f:
            f.write("\n// tampered by test\n")

        result = _run_parity_gate(root, skip_frontend=False)
        assert result.returncode == 1
        assert "toolSchemas.ts" in result.stderr
        assert "diverged" in result.stderr


# ---------------------------------------------------------------------------
# check_cost_withheld.sh — smoke check only.
# ---------------------------------------------------------------------------


def test_cost_withheld_gate_still_passes_unmodified() -> None:
    """The cost-withheld gate passes against the repository unmodified."""
    result = proc_run.run(
        ["bash", str(CHECK_COST_WITHHELD)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# check_no_account_id.sh — the UUID final-group exclusion and the fixtures it affects.
# ---------------------------------------------------------------------------


class TestAnAccountIdInsideAUuidFinalGroupIsCaught:
    """`check_no_account_id` reports a 12-digit run wherever it appears, a UUID's final group included.

    `_BENIGN_SHAPES` holds only placeholder shapes, so no UUID shape excuses a real id.
    """

    @staticmethod
    def _run_gate_over(tmp_path: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
        """Run the real gate over a scratch repo containing `files`, never the tracked tree."""
        _init_scratch_repo(tmp_path)
        for name, content in files.items():
            (tmp_path / name).write_text(content, encoding="utf-8")
        _stage_all(tmp_path)
        return proc_run.run(
            ["bash", str(CHECK_NO_ACCOUNT_ID), "--worktree"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_synthetic_id_is_one_the_gate_genuinely_detects(self, tmp_path: Path) -> None:
        """The synthetic id is reported by the gate when it is not inside a UUID."""
        account_id = _synthetic_account_id()
        assert len(account_id) == 12 and account_id.isdigit()
        result = self._run_gate_over(tmp_path, {"bare.txt": f'account = "{account_id}"\n'})
        assert result.returncode == 1, result.stdout + result.stderr
        assert account_id in result.stderr

    def test_the_benign_shape_derivation_is_not_vacuous(self) -> None:
        """`_gate_benign_shapes` parses a non-empty `_BENIGN_SHAPES` array."""
        shapes = _gate_benign_shapes()
        assert shapes, (
            "could not parse _BENIGN_SHAPES out of check_no_account_id.sh; the tests below would "
            "silently assert the wrong branch"
        )
        assert "_PLACEHOLDER_ACCOUNT_IDS" in shapes, (
            "the placeholder redaction is the one shape this gate has always had; its absence means "
            "the array was parsed from the wrong place"
        )

    def test_a_real_shaped_id_in_a_full_uuids_final_group_is_reported(self, tmp_path: Path) -> None:
        """An id as a full UUID's final group is reported, not excused by the UUID shape."""
        account_id = _synthetic_account_id()
        result = self._run_gate_over(
            tmp_path,
            {"session.ts": f'const sessionId = "11111111-2222-3333-4444-{account_id}";\n'},
        )
        assert result.returncode == 1, (
            "a 12-digit run in a full UUID's final group must be reported; no benign shape may "
            "excuse it:\n" + result.stdout + result.stderr
        )
        assert account_id in result.stderr

    def test_no_tracked_fixture_carries_an_all_digit_uuid_final_group(self) -> None:
        """No tracked fixture has an all-digit UUID final group, which the gate would report."""
        exempt = _gate_placeholder_account_ids()
        uuid_literal = re.compile(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-([0-9a-fA-F]{12})"
        )
        offenders = []
        for path in sorted(_tracked_python_and_shell_sources()):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                for final_group in uuid_literal.findall(line):
                    if final_group.isdigit() and final_group not in exempt:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "a synthetic UUID's final group is twelve all-digit characters, which this gate "
            "reads as an AWS account id; change the fixture so the group contains a "
            "hex letter -- never loosen the gate's pattern:\n" + "\n".join(offenders)
        )

    def test_the_same_id_one_character_outside_the_uuid_shape_is_still_caught(
        self, tmp_path: Path
    ) -> None:
        """An id one character outside the UUID shape is still reported."""
        account_id = _synthetic_account_id()
        result = self._run_gate_over(
            tmp_path,
            {"session.ts": f'const sessionId = "11111111-2222-3333-444-{account_id}";\n'},
        )
        assert result.returncode == 1, (
            "the id is one character outside the UUID shape and must still be reported; an "
            "exclusion this loose would excuse a real id in almost any hyphenated context:\n"
            + result.stdout
            + result.stderr
        )
        assert account_id in result.stderr

    def test_every_placeholder_id_the_gate_declares_is_excused_wherever_it_appears(
        self, tmp_path: Path
    ) -> None:
        """Every `_PLACEHOLDER_ACCOUNT_IDS` entry is excused wherever it appears."""
        placeholders = _gate_placeholder_account_ids()
        assert placeholders, "no placeholder ids were parsed out of the gate"
        files = {
            f"placeholder_{index}.tf": f'bucket = "crm-eval-v2-tfstate-{value}-us-east-1"\n'
            for index, value in enumerate(sorted(placeholders))
        }
        result = self._run_gate_over(tmp_path, files)
        assert result.returncode == 0, (
            "every id the gate declares as a placeholder must be excused wherever it appears:\n"
            + result.stdout
            + result.stderr
        )


class TestAccountIdGateFixtures:
    """An id in a resource name is reported, and the MFA user fixture is a UUID."""

    def test_a_genuine_twelve_digit_account_id_is_still_caught(self, tmp_path: Path) -> None:
        account_id = _synthetic_account_id()
        _init_scratch_repo(tmp_path)
        (tmp_path / "state.tf").write_text(
            f'bucket = "crm-eval-v2-tfstate-{account_id}-us-east-1"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = proc_run.run(
            ["bash", str(CHECK_NO_ACCOUNT_ID), "--worktree"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 1, result.stdout + result.stderr
        assert account_id in result.stderr

    def test_the_mfa_user_fixture_is_still_a_well_formed_uuid(self) -> None:
        """The MFA user fixture's username is a well-formed UUID."""
        source = (REPO_ROOT / "tests" / "unit" / "test_smoke_mfa_user.py").read_text(
            encoding="utf-8"
        )
        match = re.search(r'^_USERNAME = "([^"]+)"', source, re.MULTILINE)
        assert match is not None, "_USERNAME literal not found in test_smoke_mfa_user.py"
        uuid.UUID(match.group(1))


class TestPlaceholderResourceIdsAreNarrow:
    """`_PLACEHOLDER_RESOURCE_IDS` excuses only eight-leading-zero or documentation-form ids.

    Real-shaped ids are assembled at runtime because `check_no_account_id` scans this file.
    """

    @pytest.mark.parametrize(
        "resource_id",
        [
            "vpc-00000000aaaaaaaa1",
            "vpc-00000000",
            "vpc-0123456789abcdef0",
            "subnet-0123456789abcdef0",
            "subnet-00000000bbbbbbbb2",
            "rtb-00000000ccccccc03",
        ],
    )
    def test_a_placeholder_id_is_excused(self, tmp_path: Path, resource_id: str) -> None:
        result = TestAnAccountIdInsideAUuidFinalGroupIsCaught._run_gate_over(
            tmp_path, {"fixture.py": f'ID = "{resource_id}"\n'}
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "resource_id",
        [
            "vpc-" + "0a1b2c3d",
            "subnet-" + "0aaaaaaaaaaaaaaa1",
            "vpc-" + "0000000a1b2c3d4e5",
            "rtb-" + "0bbbbbbbbbbbbbbb1",
        ],
    )
    def test_a_real_shaped_id_is_still_caught(self, tmp_path: Path, resource_id: str) -> None:
        result = TestAnAccountIdInsideAUuidFinalGroupIsCaught._run_gate_over(
            tmp_path, {"fixture.py": f'ID = "{resource_id}"\n'}
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert resource_id in result.stderr

    def test_a_placeholder_does_not_excuse_a_real_id_on_the_same_line(self, tmp_path: Path) -> None:
        real = "subnet-" + "0aaaaaaaaaaaaaaa1"
        result = TestAnAccountIdInsideAUuidFinalGroupIsCaught._run_gate_over(
            tmp_path, {"fixture.py": f'IDS = ["vpc-00000000aaaaaaaa1", "{real}"]\n'}
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert real in result.stderr


# ---------------------------------------------------------------------------
# Gate registration in `scripts/run_gates.py`. Each gate's own behaviour is tested in its own
# `test_check_<name>.py`; here it is asserted that `run_gates.py` runs it.
# ---------------------------------------------------------------------------


#: Gates asserted here: registration, the exit-3 convention, a dedicated test file, SPDX header.
OWNED_GATE_REGISTRATIONS = (
    "check_sse_schema_parity",
    "check_relay_thinness",
    "check_identity_channel_parity",
    "check_page_context_no_rows",
    "check_design_tokens",
    "check_testid_preservation",
)

#: Alias for `OWNED_GATE_REGISTRATIONS`.
G1_GATES = OWNED_GATE_REGISTRATIONS


def _run_gates_module() -> ModuleType:
    """Import `scripts/run_gates.py` by path."""
    path = REPO_ROOT / "scripts" / "run_gates.py"
    spec = importlib.util.spec_from_file_location("run_gates_for_test_gates", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestG1GateRegistration:
    @pytest.mark.parametrize("name", G1_GATES)
    def test_gate_is_registered(self, name: str) -> None:
        run_gates = _run_gates_module()
        registered = {gate_name for gate_name, _ in run_gates.GATES}
        assert name in registered, f"{name} is not registered in run_gates.GATES"

    @pytest.mark.parametrize("name", G1_GATES)
    def test_gate_script_exists_and_is_the_one_registered(self, name: str) -> None:
        run_gates = _run_gates_module()
        argv = dict(run_gates.GATES)[name]
        script = REPO_ROOT / argv[-1]
        assert script.is_file(), f"{name} is registered as {argv[-1]}, which does not exist"
        assert script.name == f"{name}.py"

    #: Gates whose skip status is a bare `return 3` rather than a named `SKIP_EXIT_STATUS = 3`;
    #: each has its exit-3 path proven by execution elsewhere in this module.
    _SKIP_STATUS_LITERAL_EXEMPT = {
        "check_identity_channel_parity": (
            "uses a bare `return 3`; exit 3 proven by "
            "test_identity_channel_parity_skips_when_the_relay_is_absent"
        ),
    }

    @pytest.mark.parametrize("name", G1_GATES)
    def test_gate_declares_the_shared_skip_status(self, name: str) -> None:
        """Every gate declares `SKIP_EXIT_STATUS = 3`, matching `run_gates`."""
        run_gates = _run_gates_module()
        assert run_gates.GATE_SKIP_EXIT_STATUS == 3
        source = (REPO_ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
        if name in self._SKIP_STATUS_LITERAL_EXEMPT:
            # An exempt gate must still declare a return-3 path.
            assert "return 3" in source or "SKIP_EXIT_STATUS = 3" in source, (
                f"{name} is exempt from the named-constant requirement but declares no skip path"
            )
            return
        assert "SKIP_EXIT_STATUS = 3" in source, f"{name} does not declare the skip status"

    def test_every_skip_status_exemption_is_still_needed(self) -> None:
        """No gate in `_SKIP_STATUS_LITERAL_EXEMPT` declares the named constant."""
        stale = [
            name
            for name in self._SKIP_STATUS_LITERAL_EXEMPT
            if "SKIP_EXIT_STATUS = 3"
            in (REPO_ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
        ]
        assert stale == [], (
            f"gate(s) {stale} now declare a named SKIP_EXIT_STATUS; remove them from "
            "_SKIP_STATUS_LITERAL_EXEMPT rather than leaving an exemption with no reason left"
        )

    @pytest.mark.parametrize("name", G1_GATES)
    def test_gate_has_its_own_dedicated_test_file(self, name: str) -> None:
        """Every gate has a dedicated test file."""
        path = REPO_ROOT / "tests" / "unit" / f"test_{name}.py"
        assert path.is_file(), f"{name} has no dedicated test file at {path}"

    @pytest.mark.parametrize("name", G1_GATES)
    def test_gate_carries_the_spdx_header(self, name: str) -> None:
        source = (REPO_ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
        assert "# SPDX-License-Identifier: MIT" in source.splitlines()[1]

    def test_no_g1_gate_exits_zero_on_a_missing_artefact(self) -> None:
        """Each gate pointed at an absent artefact exits 3 and prints SKIP."""
        cases = {
            "check_sse_schema_parity": ["--consumer", "/nonexistent/events.ts"],
            "check_page_context_no_rows": ["--module-path", "/nonexistent/page_context.py"],
            "check_design_tokens": ["--root", "/nonexistent/src"],
            "check_testid_preservation": ["--root", "/nonexistent/src"],
            "check_relay_thinness": ["--relay-dir", "/nonexistent/relay"],
            # check_identity_channel_parity takes no path override; its exit-3 path is proven in
            # test_identity_channel_parity_skips_when_the_relay_is_absent.
        }
        for name, args in cases.items():
            result = proc_run.run(
                [sys.executable, str(REPO_ROOT / "scripts" / f"{name}.py"), *args],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 3, (
                f"{name} exited {result.returncode} on a missing artefact, not 3. Exit 0 there "
                f"would be a silent hole.\n{result.stdout}{result.stderr}"
            )
            assert "SKIP" in result.stdout


# ---------------------------------------------------------------------------
# Registration of gates owned elsewhere.
#
# Here it is asserted that `run_gates.py` invokes each gate and recognises its skip disposition.
# ---------------------------------------------------------------------------


def _plan_security_baseline_module() -> ModuleType:
    path = REPO_ROOT / "scripts" / "check_plan_security_baseline.py"
    spec = importlib.util.spec_from_file_location("check_plan_security_baseline_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_teardown_force_destroy_control_is_registered() -> None:
    """The teardown force-destroy control is registered in `CONTROLS` with a non-empty bucket
    classification.

    Its behaviour is tested in `tests/unit/test_plan_security_baseline.py::TestTeardownForceDestroy`.
    """
    gate = _plan_security_baseline_module()
    assert "teardown_force_destroy" in {control.control_id for control in gate.CONTROLS}
    assert gate.BUCKET_TEARDOWN_DISPOSITION, "no bucket is classified — the control is vacuous"
    assert set(gate.BUCKET_TEARDOWN_DISPOSITION.values()) <= {"required", "forbidden"}
    assert "required" in set(gate.BUCKET_TEARDOWN_DISPOSITION.values()), (
        "every bucket is 'forbidden', so the control asserts no force_destroy anywhere"
    )


class TestVerifyTerraformPlanWithKmsAbsentRegistration:
    """`scripts/verify_terraform_plan_with_kms_absent.sh`: registration and its exit statuses."""

    GATE_NAME = "verify_terraform_plan_with_kms_absent"
    SCRIPT = REPO_ROOT / "scripts" / "verify_terraform_plan_with_kms_absent.sh"

    def test_the_script_exists_and_is_registered(self) -> None:
        run_gates = _run_gates_module()
        registered = dict(run_gates.GATES)
        assert self.GATE_NAME in registered
        assert registered[self.GATE_NAME] == [
            "bash",
            "scripts/verify_terraform_plan_with_kms_absent.sh",
        ]
        assert self.SCRIPT.is_file()

    def test_it_exits_three_not_zero_when_terraform_is_absent(self) -> None:
        """Without terraform the script exits 3, rendered `[SKIP]`."""
        result = proc_run.run(
            ["bash", str(self.SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        )
        assert result.returncode == 3, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "SKIP" in combined
        assert "terraform" in combined

    def test_a_bad_argument_is_a_usage_error_not_a_skip(self) -> None:
        """A bad argument exits 2, distinct from the skip status 3."""
        result = proc_run.run(
            ["bash", str(self.SCRIPT), "--bogus"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2, result.stdout + result.stderr

    def test_run_gates_renders_its_skip_as_skip_and_not_as_pass(self) -> None:
        """`run_gates.py` dispositions the script's exit 3 as `skipped`."""
        run_gates = _run_gates_module()
        result = run_gates._run_gate(self.GATE_NAME, ["bash", "-c", "echo nope; exit 3"])
        assert result["skipped"] is True
        assert result["passed"] is False


class TestCanaryTenantEmptyRegistration:
    """`scripts/check_canary_tenant_empty.py`: registration and skip disposition.

    The gate's behaviour is tested in `tests/unit/test_check_canary_tenant_empty.py`.
    """

    GATE_NAME = "check_canary_tenant_empty"
    SCRIPT = REPO_ROOT / "scripts" / "check_canary_tenant_empty.py"

    def _module(self) -> ModuleType:
        spec = importlib.util.spec_from_file_location(
            "check_canary_tenant_empty_for_tests", self.SCRIPT
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_the_script_exists(self) -> None:
        """The gate script is present under `scripts/`."""
        assert self.SCRIPT.is_file()

    def test_it_is_registered(self) -> None:
        run_gates = _run_gates_module()
        registered = dict(run_gates.GATES)
        assert self.GATE_NAME in registered
        # Membership rather than position: the registered argv also carries `--table-prefix`.
        assert "scripts/check_canary_tenant_empty.py" in registered[self.GATE_NAME]

    def test_it_is_registered_without_require_store(self) -> None:
        """The registration does not pass `--require-store`."""
        run_gates = _run_gates_module()
        assert "--require-store" not in dict(run_gates.GATES)[self.GATE_NAME]

    def test_it_uses_the_exported_gate_name_rather_than_prose(self) -> None:
        module = self._module()
        assert module.GATE_NAME == "canary_tenant_empty"
        assert module.SKIP_EXIT_STATUS == _run_gates_module().GATE_SKIP_EXIT_STATUS

    def test_a_skip_is_not_counted_as_a_pass(self) -> None:
        """The registered argv against a closed local port exits 3, dispositioned `skipped`.

        This cell inherits the host's AWS profile; `test_an_absent_profile_is_dispositioned_as_a_skip`
        covers a host with no profile.
        """
        run_gates = _run_gates_module()
        registered = list(dict(run_gates.GATES)[self.GATE_NAME])
        result = run_gates._run_gate(
            self.GATE_NAME, [*registered, "--endpoint-url", "http://127.0.0.1:1"]
        )
        assert result["returncode"] in {0, 3}, (
            "the gate neither passed nor skipped. An exit 1 here is most likely an environment "
            "absence misclassified as a store that answered — the ProfileNotFound defect — "
            f"rather than a leaked canary tenant:\n{result}"
        )
        if result["returncode"] == 3:
            assert result["skipped"] is True
            assert result["passed"] is False, (
                "a gate that read no store reported `passed`; the store-reachability guard would be fictional"
            )
        else:
            assert result["passed"] is True and result["skipped"] is False

    def test_an_absent_profile_is_dispositioned_as_a_skip(self, tmp_path: Path) -> None:
        """With no resolvable AWS profile the gate renders `[SKIP]` and names `ProfileNotFound`."""
        run_gates = _run_gates_module()
        registered = list(dict(run_gates.GATES)[self.GATE_NAME])
        env = dict(os.environ)
        env["AWS_CONFIG_FILE"] = str(tmp_path / "absent-config")
        env["AWS_SHARED_CREDENTIALS_FILE"] = str(tmp_path / "absent-credentials")
        env["AWS_PROFILE"] = "no-such-profile"

        # The registered argv plus the closed port, run with a scrubbed environment.
        result = proc_run.run(
            [*registered, "--endpoint-url", "http://127.0.0.1:1"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        combined = result.stdout + result.stderr

        assert result.returncode == 3, combined
        # Prefix-anchored: the FAIL rendering's prose also contains the literal "[SKIP]".
        assert result.stdout.startswith("[SKIP]"), combined
        assert "ProfileNotFound" in combined, combined

    def test_it_never_exits_zero_without_reading_a_store(self) -> None:
        """With the endpoint on a closed port, the gate never exits 0."""
        env = dict(os.environ)
        env["CRM_DYNAMODB_ENDPOINT_URL"] = "http://127.0.0.1:1"
        env["AWS_ACCESS_KEY_ID"] = _UNREAL_CRED
        env["AWS_SECRET_ACCESS_KEY"] = _UNREAL_CRED
        env["AWS_DEFAULT_REGION"] = "us-east-1"
        result = proc_run.run(
            [sys.executable, str(self.SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode != 0, (
            "the gate exited 0 with no reachable store — it verified nothing and said it passed:\n"
            f"{result.stdout}{result.stderr}"
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "SKIP" in result.stdout + result.stderr


def test_identity_channel_parity_skips_when_the_relay_is_absent(tmp_path: Path) -> None:
    """`check_identity_channel_parity` exits 3 when run from a scratch tree with no `src/relay/`."""
    scratch = tmp_path / "repo"
    (scratch / "scripts").mkdir(parents=True)
    (scratch / "src").mkdir()
    shutil.copyfile(
        REPO_ROOT / "scripts" / "check_identity_channel_parity.py",
        scratch / "scripts" / "check_identity_channel_parity.py",
    )
    shutil.copytree(REPO_ROOT / "src" / "crm", scratch / "src" / "crm")

    result = proc_run.run(
        [sys.executable, str(scratch / "scripts" / "check_identity_channel_parity.py")],
        cwd=scratch,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "SKIP" in combined
    assert "verified nothing" in combined


def test_identity_channel_parity_asserts_a_superset_not_equality() -> None:
    """The relay's stripped keys need only be a superset of the keys Python reads."""
    result = proc_run.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_identity_channel_parity.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "_gateway_identity" in result.stdout, (
        "the relay-only extra key should be reported rather than silently tolerated, so a reader can "
        "see the superset direction is deliberate"
    )


# ---------------------------------------------------------------------------
# The PARTIAL disposition: a gate that verified most controls and could not assert the rest.
# ---------------------------------------------------------------------------

RUN_GATES_PY = REPO_ROOT / "scripts" / "run_gates.py"


def _inject(
    *gates: tuple[str, str], extra_args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """Run `run_gates.py` with a test-owned gate list, one `name<TAB>command` per line."""
    env = dict(os.environ)
    env["CRM_RUN_GATES_INJECT"] = "\n".join(f"{name}\t{command}" for name, command in gates)
    return proc_run.run(
        [sys.executable, str(RUN_GATES_PY), *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


class TestToolspecParityDeclaresItsSkippedHalf:
    """`check_toolspec_parity --skip-frontend` reports the frontend half as deferred, not passed.

    Runs the real gate with `--skip-frontend`, so no `npm` is invoked.
    """

    def _skip_mode(self) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            [sys.executable, str(CHECK_TOOLSPEC_PARITY), "--skip-frontend"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_skipped_frontend_half_is_declared_not_silent(self) -> None:
        marker = _run_gates_module().GATE_DEFER_MARKER
        result = self._skip_mode()

        assert result.returncode == 0, result.stdout + result.stderr
        deferred = [line for line in result.stdout.splitlines() if marker in line]
        assert len(deferred) == 1, f"expected exactly one deferred half, got {deferred}"

    def test_the_declaration_names_which_consumer_was_not_checked(self) -> None:
        """The defer line names which half was not checked."""
        marker = _run_gates_module().GATE_DEFER_MARKER
        line = next(line for line in self._skip_mode().stdout.splitlines() if marker in line)

        assert "toolSchemas.ts" in line, f"the deferred consumer is not named: {line}"
        assert "--skip-frontend" in line, f"the reason is not given: {line}"

    def test_run_gates_counts_it_so_it_cannot_fold_into_the_passed_count(self) -> None:
        """`run_gates.py` does not count the deferring gate as passed."""
        run_gates = _run_gates_module()

        assert run_gates.deferred_control_count(self._skip_mode().stdout) == 1

    def test_the_gate_uses_the_shared_marker_constant_rather_than_its_own_spelling(self) -> None:
        """The gate emits the exact defer marker `run_gates.py` reads."""
        source = CHECK_TOOLSPEC_PARITY.read_text(encoding="utf-8")

        assert "from run_gates import GATE_DEFER_MARKER" in source
        assert '"[DEFER]"' not in source, "the marker is re-spelled instead of imported"


class TestPartialDisposition:
    """The `[PARTIAL]` disposition, for a gate that verified most controls and deferred the rest."""

    _DEFERRING = 'bash -c "echo \\"[DEFER] one: reason\\"; echo \\"[DEFER] two: reason\\"; echo ok"'

    def test_a_deferring_gate_renders_partial_not_pass(self) -> None:
        result = _inject(("deferring", self._DEFERRING))
        assert "[PARTIAL] deferring" in result.stdout
        assert "[PASS] deferring" not in result.stdout

    def test_a_deferring_gate_does_not_fail_the_run(self) -> None:
        """A gate that only defers does not make the run exit non-zero."""
        result = _inject(("deferring", self._DEFERRING))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_aggregate_headline_carries_the_deferred_total(self) -> None:
        """The headline does not report all gates passed when any control was deferred."""
        result = _inject(("deferring", self._DEFERRING), ("clean", 'bash -c "echo fine"'))
        assert "OK: 2/2 gates passed" not in result.stdout
        assert "PARTIAL:" in result.stdout
        assert "2 control(s)" in result.stdout
        assert "NOT asserted" in result.stdout

    def test_the_deferred_control_detail_is_echoed(self) -> None:
        """The `[PARTIAL]` block lists each deferred control's reason."""
        result = _inject(("deferring", self._DEFERRING))
        assert "[DEFER] one: reason" in result.stdout
        assert "[DEFER] two: reason" in result.stdout

    def test_a_clean_gate_is_still_pass(self) -> None:
        """A gate that deferred nothing is not dispositioned `[PARTIAL]`."""
        result = _inject(("clean", 'bash -c "echo fine"'))
        assert "[PASS] clean" in result.stdout
        assert "OK: 1/1 gates passed" in result.stdout

    def test_a_failure_outranks_a_defer(self) -> None:
        """A gate that deferred and failed is dispositioned as a failure."""
        both = 'bash -c "echo \\"[DEFER] x: reason\\"; exit 1"'
        result = _inject(("broken", both))
        assert "[FAIL] broken" in result.stdout
        assert result.returncode == 1

    def test_a_skip_outranks_a_defer(self) -> None:
        """Exit 3 is a skip even if the gate also printed a defer line."""
        both = f'bash -c "echo \\"[DEFER] x: reason\\"; exit {_run_gates_module().GATE_SKIP_EXIT_STATUS}"'
        result = _inject(("skipping", both))
        assert "[SKIP] skipping" in result.stdout

    @staticmethod
    def _headline(stdout: str) -> str:
        """The aggregate verdict line, isolated from the per-gate lines and the detail blocks."""
        return next(
            line
            for line in stdout.splitlines()
            if line.startswith(("OK:", "PARTIAL:", "INCOMPLETE:", "FAILED:"))
        )

    def test_a_board_that_both_skips_and_defers_names_both_in_the_headline(self) -> None:
        """A board with a skip and a defer names both in the headline."""
        skipping = (
            f'bash -c "echo nothing to verify; exit {_run_gates_module().GATE_SKIP_EXIT_STATUS}"'
        )
        result = _inject(
            ("deferring", self._DEFERRING),
            ("skipping", skipping),
            ("clean", 'bash -c "echo fine"'),
        )
        headline = self._headline(result.stdout)

        assert "SKIPPED" in headline, f"the skip is not in the headline: {headline}"
        assert "2 control(s)" in headline, f"the deferred total is not in the headline: {headline}"
        assert "NOT asserted" in headline, headline

    def test_a_partial_only_board_still_names_the_deferred_total(self) -> None:
        """A defer-only board names the deferred total and reports no skip."""
        result = _inject(("deferring", self._DEFERRING), ("clean", 'bash -c "echo fine"'))
        headline = self._headline(result.stdout)

        assert "2 control(s)" in headline, headline
        assert "SKIPPED" not in headline, f"nothing skipped, yet a skip is reported: {headline}"

    def test_a_skip_only_board_still_names_the_skip(self) -> None:
        """A skip-only board names the skip and reports no defer."""
        skipping = (
            f'bash -c "echo nothing to verify; exit {_run_gates_module().GATE_SKIP_EXIT_STATUS}"'
        )
        result = _inject(("skipping", skipping), ("clean", 'bash -c "echo fine"'))
        headline = self._headline(result.stdout)

        assert "SKIPPED" in headline, headline
        assert "DEFERRED" not in headline, f"nothing deferred, yet a defer is reported: {headline}"

    def test_the_census_names_every_disposition_including_the_zeroes(self) -> None:
        """`--census` reports a count for every disposition, including zeroes."""
        result = _inject(
            ("clean", 'bash -c "echo fine"'),
            ("deferring", self._DEFERRING),
            extra_args=("--census",),
        )
        census = json.loads(result.stdout)
        assert census == {"PASS": 1, "PARTIAL": 1, "SKIP": 0, "FAIL": 0}
        assert sum(census.values()) == 2, "the census must account for every gate exactly once"

    def test_the_census_does_not_change_the_json_documents_shape(self) -> None:
        """The `--json` output is still a list; the census is a separate flag."""
        result = _inject(("clean", 'bash -c "echo fine"'), extra_args=("--json",))
        assert isinstance(json.loads(result.stdout), list)

    def test_the_marker_is_counted_per_line_not_per_occurrence(self) -> None:
        """A line mentioning the defer marker twice counts as one deferred control."""
        assert _run_gates_module().deferred_control_count("[DEFER] a [DEFER] still one line\n") == 1
        assert _run_gates_module().deferred_control_count("[DEFER] a\n[DEFER] b\n") == 2
        assert _run_gates_module().deferred_control_count("nothing here\n") == 0

    def test_the_disposition_vocabulary_is_exported_for_gates_to_import(self) -> None:
        """`run_gates.py` exports the defer marker for gates to import."""
        for name in (
            "GATE_DEFER_MARKER",
            "DISPOSITION_PASS",
            "DISPOSITION_PARTIAL",
            "DISPOSITION_SKIP",
            "DISPOSITION_FAIL",
            "deferred_control_count",
        ):
            assert name in _run_gates_module().__all__, (
                f"{name} is not exported for gates to import"
            )
        assert _run_gates_module().GATE_DEFER_MARKER == "[DEFER]"

    def test_the_partial_block_echoes_the_gates_own_summary_line(self) -> None:
        """The `[PARTIAL]` block echoes the gate's own summary line."""
        gate = (
            'bash -c "echo \\"[DEFER] one: reason\\"; echo \\"570 checks, 0 failed, 1 deferred\\""'
        )
        result = _inject(("deferring", gate))
        assert "[DEFER] one: reason" in result.stdout
        assert "570 checks, 0 failed, 1 deferred" in result.stdout

    def test_the_summary_line_is_the_last_non_marker_line(self) -> None:
        """`final_summary_line` skips defer lines and is None when only defers remain."""
        run_gates = _run_gates_module()
        assert run_gates.final_summary_line("[DEFER] a\nsummary line\n") == "summary line"
        assert run_gates.final_summary_line("summary line\n[DEFER] a\n") == "summary line"
        assert run_gates.final_summary_line("[DEFER] a\n[DEFER] b\n") is None
        assert run_gates.final_summary_line("") is None

    def test_a_registered_gate_has_its_count_read_from_its_structured_output(self) -> None:
        """A registered gate's deferred count is read from its structured `--json` output."""
        run_gates = _run_gates_module()
        assert "check_plan_security_baseline" in run_gates.GATE_STRUCTURED_DEFERRED_ARGV
        assert run_gates.GATE_STRUCTURED_DEFERRED_ARGV["check_plan_security_baseline"] == (
            "--json",
        )
        assert run_gates.GATE_STRUCTURED_DEFERRED_KEY == "deferred"

    def test_a_structured_count_disagreeing_with_the_rendered_one_is_a_failure(self) -> None:
        """A structured count disagreeing with the rendered one fails the run."""
        lying = (
            'bash -c \'if [ "$1" = "--json" ]; then echo "{\\"deferred\\": 7}"; '
            'else echo "[DEFER] only one"; fi\' _'
        )
        result = _inject(("check_plan_security_baseline", lying))
        assert "[FAIL] check_plan_security_baseline" in result.stdout
        assert "deferred-count defect" in result.stdout
        assert result.returncode == 1

    def test_an_unreadable_structured_count_is_a_failure_not_a_fallback(self) -> None:
        """An unparseable structured count fails the run rather than falling back."""
        no_json = "bash -c 'echo \"[DEFER] x: reason\"'"
        result = _inject(("check_plan_security_baseline", no_json))
        assert "[FAIL] check_plan_security_baseline" in result.stdout
        assert "no parseable JSON" in result.stdout
        assert result.returncode == 1

    def test_the_json_report_carries_the_disposition_and_count(self) -> None:
        """`--json` dispositions are the contract other tests read."""
        env = dict(os.environ)
        env["CRM_RUN_GATES_INJECT"] = f"deferring\t{self._DEFERRING}"
        result = proc_run.run(
            [sys.executable, str(RUN_GATES_PY), "--json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        report = json.loads(result.stdout)
        assert report[0]["disposition"] == _run_gates_module().DISPOSITION_PARTIAL
        assert report[0]["deferred_controls"] == 2
        assert report[0]["passed"] is True
        assert report[0]["skipped"] is False


class TestEveryLocaleDependentMatcherIsPinnedOrExempt:
    """An inventory, derived from tracked content, of first-party matchers whose answer depends on
    the locale.

    Every shell or Python file under `scripts/`, `infra/`, `githooks/` and `src/` that calls `grep`,
    `sed`, `awk`, `git grep` or bash `=~` must pin `LC_ALL=C` or appear in `_LOCALE_PIN_EXEMPTIONS`
    with a reason, and every exemption must still be needed. Bash `[[ == *glob* ]]`, `gitleaks`, and git
    plumbing commands are not locale-dependent matchers and are not counted. `tests/` is out of scope.
    """

    #: First-party directories holding controls.
    _CONTROL_DIRS = ("scripts/", "infra/", "githooks/", "src/")

    #: Shell: an external matcher invoked as a command, or bash's regex operator. The lookarounds
    #: keep `_grep`, `foo.sed` and `awk-like` from counting as invocations.
    _SHELL_MATCHER = re.compile(r"(?<![\w./-])(?:git\s+grep|grep|sed|awk)(?![\w.-])|=~")

    #: Python: a subprocess whose argv begins with a matcher; `"git"` plumbing does not count.
    _PY_MATCHER = re.compile(r'\[\s*"(?:grep|sed|awk)"|\[\s*"git"\s*,\s*"grep"')

    #: Files that reach a locale-dependent matcher and are not pinned, each with its reason.
    _LOCALE_PIN_EXEMPTIONS = {
        "scripts/deploy.sh": (
            "live billable deploy path; a locale change alters every child process in a run that "
            "creates AWS resources, and nobody has measured that. Its matcher parses tool output, so a "
            "miss shows up as a failed deploy step rather than as an undetected secret."
        ),
        "scripts/teardown.sh": (
            "live billable path, same reasoning as deploy.sh; a miss leaves a resource undeleted, which "
            "is loud and costs money rather than being silent."
        ),
        "infra/bootstrap_state_backend.sh": (
            "one-time live path that creates the Terraform state backend; a locale change there is "
            "unmeasured and the failure mode is a broken bootstrap, not a missed secret."
        ),
        "scripts/verify_security_baseline.sh": (
            "highest matcher count of the deferred set (6). Reads deployed-resource descriptions rather "
            "than tracked content, so its inputs are AWS API output and not operator-authored text; an "
            "invalid byte there is not a shape anyone has observed."
        ),
        "scripts/verify_terraform_plan_with_kms_absent.sh": (
            "parses a terraform plan document it generates itself, so its input is machine-written JSON "
            "rather than authored prose."
        ),
        "scripts/check_license_headers.sh": (
            "reaches a matcher ONLY through bash `=~`, and only to filter PATHS by extension -- the "
            "header comparison itself is `[[ == *substr* ]]`, measured locale-INSENSITIVE. So the "
            "exposure is a path containing an invalid byte being skipped by the scope filter, which is "
            "a missing licence header rather than a missing secret."
        ),
        "scripts/package_relay.sh": (
            "build-time packaging of the relay artefact; its matcher reads its own toolchain output."
        ),
        "scripts/run_local_dynamodb.sh": (
            "local developer convenience wrapper around Docker; its matcher reads `docker` CLI output "
            "to decide whether the container is already up, and runs no security control at all. A "
            "miss means the script tries to start a container that is already running, which fails "
            "loudly and immediately rather than passing something undetected."
        ),
        "scripts/preflight_adopted_vpc.sh": (
            "reaches a matcher once, through bash `=~`, to validate an adopted VPC id supplied as a "
            "command-line ARGUMENT rather than read from tracked content. A locale-dependent match there "
            "mis-accepts or mis-rejects an operator's own argument, so the failure surfaces as a refused "
            "or failed preflight on a deploy path — loud and immediate — and never as an undetected "
            "secret in the repository. Same category and same reasoning as deploy.sh and teardown.sh."
        ),
    }

    #: Files the signal must still see; a floor against the derivation going blind.
    _FLOOR = frozenset(
        {
            "scripts/check_no_account_id.sh",
            "scripts/check_no_vendor_keys.sh",
            "scripts/check_untracked_artifact_secrets.py",
            "scripts/check_license_headers.sh",
        }
    )

    @classmethod
    def _is_shell(cls, path: Path) -> bool:
        """Whether `path` is a shell script, judged by shebang rather than suffix."""
        try:
            first = path.read_bytes().split(b"\n", 1)[0]
        except OSError:
            return False
        return first.startswith(b"#!") and (b"sh" in first or b"bash" in first)

    @classmethod
    def _derived(cls) -> dict[str, tuple[int, bool]]:
        """Map `relative path -> (matcher call count, is pinned)` for every derived member."""
        listing = proc_run.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        found: dict[str, tuple[int, bool]] = {}
        for rel in listing.stdout.split("\0"):
            if not rel or not rel.startswith(cls._CONTROL_DIRS):
                continue
            path = REPO_ROOT / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            shell, python = cls._is_shell(path), path.suffix == ".py"
            if not (shell or python):
                continue
            # Comment lines are stripped so prose about a matcher does not count as a call.
            body = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith(("#", "//"))
            )
            pattern = cls._SHELL_MATCHER if shell else cls._PY_MATCHER
            calls = len(pattern.findall(body))
            if not calls:
                continue
            found[rel] = (calls, cls._is_pinned(text, is_shell=shell, body=body))
        return found

    @staticmethod
    def _is_pinned(text: str, *, is_shell: bool, body: str) -> bool:
        """Whether the file pins `LC_ALL=C` in code.

        Shell: an `export LC_ALL=C` line in the comment-stripped body. Python: an `"LC_ALL": "C"` dict
        literal in the parse tree, so a docstring mentioning the pin does not count.
        """
        if is_shell:
            return "\nexport LC_ALL=C\n" in f"\n{body}\n"
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # An unparseable file is reported as unpinned.
            return False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "LC_ALL"
                    and isinstance(value, ast.Constant)
                    and value.value == "C"
                ):
                    return True
        return False

    def test_the_derivation_is_not_vacuous(self) -> None:
        """The derived set is non-empty and contains every `_FLOOR` file."""
        derived = self._derived()
        assert derived, (
            "the locale-matcher derivation found NOTHING, so every assertion in this class is vacuous. "
            "Re-derive the signal from the current scripts rather than adjusting the expectation."
        )
        missing = self._FLOOR - set(derived)
        assert not missing, (
            f"the derivation no longer sees {sorted(missing)}, each of which reaches a locale-dependent "
            "matcher. The signal has gone blind; widen it rather than shrinking this floor."
        )

    def test_the_signal_detects_a_bash_regex_matcher(self) -> None:
        """The shell signal matches `=~`, `git grep` and `sed`, and not non-invocations."""
        assert self._SHELL_MATCHER.search('if [[ "$x" =~ $re ]]; then')
        assert self._SHELL_MATCHER.search("git grep -I -n -E -e foo")
        assert self._SHELL_MATCHER.search("sed -E 's/a/b/'")
        # ...and does not match things that are not invocations.
        assert not self._SHELL_MATCHER.search("_grep_helper()")
        assert not self._SHELL_MATCHER.search('[[ "$c" == *"SPDX"* ]]')
        assert not self._PY_MATCHER.search('["git", "rev-parse", "HEAD"]')
        assert self._PY_MATCHER.search('["grep", "-I", "-l", "-E"]')

    def test_every_derived_matcher_is_pinned_or_explicitly_exempt(self) -> None:
        unaccounted = {
            rel: calls
            for rel, (calls, pinned) in self._derived().items()
            if not pinned and rel not in self._LOCALE_PIN_EXEMPTIONS
        }
        assert not unaccounted, (
            "these tracked files reach a locale-dependent matcher with no LC_ALL=C pin and no recorded "
            f"exemption: {unaccounted}. Either pin the locale or add an entry to "
            "_LOCALE_PIN_EXEMPTIONS stating why it is deferred and what its failure would cost. Do not "
            "add a bare path: a deferral without a reason is indistinguishable from an oversight."
        )

    def test_every_exemption_is_still_needed_and_reasoned(self) -> None:
        """Every exemption names an existing, derived, unpinned file with a 12+ word reason."""
        derived = self._derived()
        for rel, reason in sorted(self._LOCALE_PIN_EXEMPTIONS.items()):
            assert (REPO_ROOT / rel).exists(), (
                f"_LOCALE_PIN_EXEMPTIONS names {rel}, which no longer exists. Remove the entry."
            )
            assert rel in derived, (
                f"{rel} is exempted but no longer reaches a locale-dependent matcher, so the exemption "
                "records a decision that no longer applies. Remove the entry."
            )
            assert not derived[rel][1], (
                f"{rel} is exempted AND pinned. One of the two is wrong; if it was pinned "
                "deliberately, remove the exemption so the pin is what the inventory reports."
            )
            assert len(reason.split()) >= 12, (
                f"the exemption for {rel} is {len(reason.split())} words. State what the matcher reads "
                "and what a miss would cost, not merely that it is deferred."
            )
