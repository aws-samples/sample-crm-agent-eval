# SPDX-License-Identifier: MIT
"""Tests for the content-hygiene gates (scripts/check_no_account_id.sh,
scripts/check_no_vendor_keys.sh, scripts/check_log_redaction.py, the untracked local .gitleaks.toml)
and the tracked pre-commit hook.

Each gate is shown to fail on a violating fixture built in a scratch git repository and to pass on
a compliant counterpart. Gates are invoked via `subprocess` exactly as `make verify` and the
pre-commit hook invoke them. Whole-repository subjects are shared through module-scoped fixtures
where cells run the identical command; see the "Shared read-only subjects" section.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from scripts import proc_run

from tests.unit._gitleaks_config import (
    LOCAL_RULE_SET,
    install_local_rule_set,
    require_local_rule_set,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_NO_ACCOUNT_ID = REPO_ROOT / "scripts" / "check_no_account_id.sh"
CHECK_NO_VENDOR_KEYS = REPO_ROOT / "scripts" / "check_no_vendor_keys.sh"
CHECK_SECRET_SCAN = REPO_ROOT / "scripts" / "check_secret_scan.py"
PROC_RUN = REPO_ROOT / "scripts" / "proc_run.py"
CHECK_LOG_REDACTION = REPO_ROOT / "scripts" / "check_log_redaction.py"
GITLEAKS_CONFIG = LOCAL_RULE_SET
PRE_COMMIT_HOOK = REPO_ROOT / "githooks" / "pre-commit"
PRE_COMMIT_CONFIG_YAML = REPO_ROOT / ".pre-commit-config.yaml"

_SPEC = importlib.util.spec_from_file_location("check_log_redaction", CHECK_LOG_REDACTION)
assert _SPEC is not None and _SPEC.loader is not None
log_redaction_gate = importlib.util.module_from_spec(_SPEC)
sys.modules["check_log_redaction"] = log_redaction_gate
_SPEC.loader.exec_module(log_redaction_gate)


def _fake_private_key_block() -> str:
    """Synthesise a fake PEM-shaped private key block, assembled so no secret-shaped literal
    appears in source."""
    begin = "-----BEGIN " + "RSA" + " PRIVATE " + "KEY-----"
    end = "-----END " + "RSA" + " PRIVATE " + "KEY-----"
    body = "MII" + "BogIBAAJ" + "..."
    return f"{begin}\n{body}\n{end}\n"


#: The four git environment names the content gates unset. `GIT_INDEX_FILE` is validated instead;
#: see :class:`TestGitIndexFileIsValidatedNotRemoved`.
SCRUBBED_GIT_NAMES = "unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY"

#: Single-line mutations of a gate script, each reverting one remedy, applied only to a scratch copy.
#: Each needle is asserted to match exactly once at apply time.
MUT_REVERT_SCRUB = (SCRUBBED_GIT_NAMES, ": # mutation: environment scrub removed")

#: Unsets `GIT_INDEX_FILE` alongside the other four instead of validating it.
MUT_BLANKET_UNSET_INDEX = (SCRUBBED_GIT_NAMES, f"{SCRUBBED_GIT_NAMES} GIT_INDEX_FILE")

#: Accepts any `GIT_INDEX_FILE`, however foreign.
MUT_REVERT_INDEX_VALIDATION = (
    'if [[ "$_index_dir" != "$_own_git_dir" ]]; then',
    "if false; then",
)
MUT_REVERT_STATUS_CHECK = (
    'if [[ "$rc" -gt 1 || -s "$_GREP_STDERR" ]]; then',
    "if false; then",
)
MUT_REVERT_POSITIVE_CONTROL = (
    "\nassert_subject_is_readable\n",
    "\n: # mutation: positive control removed\n",
)


def _mutate_gate(gate: Path, *mutations: tuple[str, str]) -> str:
    """Apply single-line mutations to `gate`'s source, asserting each needle matches exactly once."""
    source = gate.read_text(encoding="utf-8")
    for needle, replacement in mutations:
        assert source.count(needle) == 1, (
            f"the mutation needle {needle!r} appears {source.count(needle)} times in {gate.name}, so "
            "this control is not mutating what it claims to. Re-derive it from the current script "
            "rather than adjusting the count."
        )
        source = source.replace(needle, replacement)
    return source


def _synthetic_account_id() -> str:
    """A syntactically valid, non-placeholder 12-digit AWS account id."""
    return "45816" + "3927401"


def _synthetic_bare_account_id() -> str:
    """A second distinct account id, for the bare-run-in-a-resource-name case."""
    return "60483" + "7291056"


#: A 17-hex resource-id body assembled at runtime; not the all-zero or documentation placeholder
#: that `check_no_account_id` excuses.
_SYNTHETIC_HEX_17 = "0a1b" * 4 + "0"


def _synthetic_vpc_id() -> str:
    return "vpc-" + _SYNTHETIC_HEX_17


def _synthetic_subnet_id() -> str:
    return "subnet-" + _SYNTHETIC_HEX_17


def _synthetic_security_group_id() -> str:
    return "sg-" + _SYNTHETIC_HEX_17


def _synthetic_vpc_endpoint_id() -> str:
    return "vpce-" + _SYNTHETIC_HEX_17


def _synthetic_route_table_id() -> str:
    return "rtb-" + _SYNTHETIC_HEX_17


def _synthetic_internet_gateway_id() -> str:
    return "igw-" + _SYNTHETIC_HEX_17


def _canonical_documentation_id_suffix() -> str:
    """The canonical AWS-documentation resource-id suffix, assembled so no matching literal appears
    in source."""
    return "0123456789" + "abcdef0"


def _synthetic_cloudfront_vpc_origin_id() -> str:
    """A CloudFront VPC origin id, which uses a `_` separator rather than `-`."""
    return "vo_" + "A1b2C3d4" + "E5f6G7h8"


def _iam_user_arn(user: str, account_id: str = "123456789012", partition: str = "aws") -> str:
    """An IAM user ARN, assembled so no user ARN literal appears in source."""
    return f"arn:{partition}:iam::{account_id}:" + "user/" + user


def _synthetic_home_path() -> str:
    """An absolute home-directory path outside the repository."""
    return "/Us" + "ers/someone/secret-project"


def _fake_vendor_key_line(var_name: str = "ANTHROPIC_API_KEY") -> str:
    """Synthesise a fake Anthropic-shaped API key assignment line, assembled so no secret-shaped
    literal appears in source."""
    prefix = "sk-" + "ant-" + "api03-"
    body = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
    return f'{var_name} = "{prefix}{body}"\n'


def _fake_openai_key_line(var_name: str = "OPENAI_API_KEY") -> str:
    """Synthesise a fake OpenAI-project-shaped key assignment line, assembled at runtime."""
    prefix = "sk-" + "proj-"
    body = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
    return f'{var_name} = "{prefix}{body}"\n'


def _fake_vendor_env_assignment(var_name: str = "ANTHROPIC_API_KEY") -> str:
    """A vendor-named env-var assignment whose value carries no known vendor prefix, exercising
    the generic assignment pattern."""
    body = "abcdefghijklmnop" + "0123456789ABCDEFGH"
    return f"{var_name}={body}\n"


def _fake_aws_access_key_id() -> str:
    """Synthesise an AWS-access-key-id-shaped value at runtime.

    Matched by the local `aws-access-key-id` rule and gitleaks' built-in `aws-access-token` rule,
    and by neither shell gate's `PATTERNS` array, so only the gitleaks gate can refuse it. It is not the allowlisted AWS
    example key and carries no 12-digit run.
    """
    prefix = "A" + "KIA"
    body = "QRSTUVWX" + "34567YZB"
    return f"{prefix}{body}"


#: A directory no rule set allowlists, so only `.gitignore` can exclude a plant beneath it.
_IGNORED_PLANT_DIR = "generated_output"


def _plant_ignored_secret(root: Path) -> None:
    """Write a `.gitignore` excluding :data:`_IGNORED_PLANT_DIR` and plant a built-in-rule match
    beneath it."""
    (root / ".gitignore").write_text(f"{_IGNORED_PLANT_DIR}/\n", encoding="utf-8")
    plant_dir = root / _IGNORED_PLANT_DIR
    plant_dir.mkdir()
    (plant_dir / "x.json").write_text(
        f'{{"cred": "{_fake_aws_access_key_id()}"}}\n', encoding="utf-8"
    )


def _init_scratch_repo(root: Path) -> None:
    """Initialise `root` as a real git repository so `git grep`-based gates have something to scan."""
    proc_run.run(["git", "init", "-q"], cwd=root, check=True)
    proc_run.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    proc_run.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _stage_all(root: Path) -> None:
    """Stage all files without committing; `git grep` scans a file once it is staged."""
    proc_run.run(["git", "add", "-A"], cwd=root, check=True)


#: Both gates require a subject argument. These helpers default it to `--worktree`; subject
#: selection itself is asserted in `TestSubjectSelection`.
def _run_account_id_gate(
    root: Path, subject: tuple[str, ...] = ("--worktree",)
) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        ["bash", str(CHECK_NO_ACCOUNT_ID), *subject],
        cwd=root,
        capture_output=True,
        text=True,
    )


def _run_vendor_keys_gate(
    root: Path, subject: tuple[str, ...] = ("--worktree",)
) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        ["bash", str(CHECK_NO_VENDOR_KEYS), *subject],
        cwd=root,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Shared read-only subjects
#
# Cells that run the identical command against the identical whole-repository subject share one
# run. The sharing is asserted by counting invocations in
# `test_the_shared_subjects_are_produced_exactly_once`.
# ---------------------------------------------------------------------------

#: Pins every cell that reads a shared subject to one pytest-xdist worker, so the module-scoped
#: fixtures below are built once per suite. Requires `--dist loadgroup`. Applied per test so the
#: mutating cells in the same classes stay distributable.
_SHARED_SUBJECT_GROUP = pytest.mark.xdist_group("hygiene_shared_subjects")

#: Expected count of each shared subject or invocation in the log written by `_invocation_log`.
_EXPECTED_INVOCATIONS = {
    "refmode_clean_clone": 1,
    "refmode_clean_worktree_scan": 1,
    "refmode_clean_ref_head_scan": 1,
    "subject_selection_readonly_clone": 1,
    "real_repository_hook_run": 1,
}


@pytest.fixture(scope="session")
def _invocation_log(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Path to the invocation log, in the run-scoped temp root shared by every xdist worker.

    Under xdist each worker's base temp is a `popen-gw*` subdirectory of that root; serially the
    root is the base temp.
    """
    base = tmp_path_factory.getbasetemp()
    root = base.parent if base.name.startswith("popen-gw") else base
    return root / "hygiene_shared_subject_invocations.log"


def _record_invocation(log: Path, label: str) -> None:
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"{label}\t{os.environ.get('PYTEST_XDIST_WORKER', 'serial')}\t{os.getpid()}\n")


def _repo_fingerprint(work: Path) -> str:
    """A shared read-only subject's exact state: HEAD, the index's tree, and the working-tree deltas.

    `git write-tree` writes an object into the subject's own store and changes nothing observable
    about HEAD, the index or the working tree.
    """
    parts = []
    for argv in (["rev-parse", "HEAD"], ["write-tree"], ["status", "--porcelain"]):
        parts.append(
            proc_run.run(
                ["git", "-C", str(work), *argv], capture_output=True, text=True, check=True
            ).stdout
        )
    return "\n".join(parts)


def _clone_for_ref_mode(work: Path) -> Path:
    """The clone recipe :class:`TestCheckNoAccountIdRefMode` reads, shared by the shared clean clone
    and the per-test clones."""
    proc_run.run(
        ["git", "clone", "--quiet", "--shared", str(REPO_ROOT), str(work)],
        check=True,
        capture_output=True,
    )
    for key, value in (("user.email", "probe@local"), ("user.name", "probe")):
        proc_run.run(["git", "-C", str(work), "config", key, value], check=True)
    # The gate under test is the worktree's copy, not the clone's HEAD.
    (work / "scripts" / "check_no_account_id.sh").write_bytes(
        (REPO_ROOT / "scripts" / "check_no_account_id.sh").read_bytes()
    )
    return work


def _clone_for_subject_selection(work: Path) -> Path:
    """The clone recipe :class:`TestSubjectSelection` reads: copies four worktree files into the
    clone and syncs the index to them."""
    proc_run.run(
        ["git", "clone", "--quiet", "--shared", str(REPO_ROOT), str(work)],
        check=True,
        capture_output=True,
    )
    for key, value in (("user.email", "probe@local"), ("user.name", "probe")):
        proc_run.run(["git", "-C", str(work), "config", key, value], check=True)
    for relative in (
        ("scripts", "check_no_account_id.sh"),
        ("scripts", "check_no_vendor_keys.sh"),
        ("githooks", "pre-commit"),
        ("tests", "unit", "test_hygiene_gates.py"),
    ):
        (work / Path(*relative)).write_bytes((REPO_ROOT / Path(*relative)).read_bytes())
    proc_run.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
    return work


def _shared_readonly_clone(
    log: Path, label: str, destination: Path, recipe: Callable[[Path], Path]
) -> Iterator[Path]:
    """Build a clone once, hand it out read-only, and assert at teardown that no consumer mutated
    it."""
    assert callable(recipe)
    work = recipe(destination)
    _record_invocation(log, label)
    fingerprint = _repo_fingerprint(work)
    yield work
    assert _repo_fingerprint(work) == fingerprint, (
        f"the shared read-only subject {label!r} was MUTATED by one of its consumers, so the other "
        "cells reading it were reading a different tree than the one this fixture built. A consumer "
        "that needs to mutate its subject must build its own clone instead of joining this fixture."
    )


@pytest.fixture(scope="module")
def refmode_clean_clone(
    _invocation_log: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """One pristine `TestCheckNoAccountIdRefMode` clone, shared by the clean-tree cells and the
    `--ref <metacharacter>` cells."""
    yield from _shared_readonly_clone(
        _invocation_log,
        "refmode_clean_clone",
        tmp_path_factory.mktemp("refmode_clean") / "clone",
        _clone_for_ref_mode,
    )


@pytest.fixture(scope="module")
def refmode_clean_worktree_scan(
    _invocation_log: Path, refmode_clean_clone: Path
) -> subprocess.CompletedProcess[str]:
    """`--worktree` over the pristine clone, recorded in the invocation log."""
    _record_invocation(_invocation_log, "refmode_clean_worktree_scan")
    return TestCheckNoAccountIdRefMode._run(refmode_clean_clone, "--worktree")


@pytest.fixture(scope="module")
def refmode_clean_ref_head_scan(
    _invocation_log: Path, refmode_clean_clone: Path
) -> subprocess.CompletedProcess[str]:
    """`--ref HEAD` over the pristine clone, read by two cells."""
    _record_invocation(_invocation_log, "refmode_clean_ref_head_scan")
    return TestCheckNoAccountIdRefMode._run(refmode_clean_clone, "--ref", "HEAD")


@pytest.fixture(scope="module")
def subject_selection_readonly_clone(
    _invocation_log: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """One `TestSubjectSelection` clone for its argument-validation cells, which exit 2 before
    reading anything."""
    yield from _shared_readonly_clone(
        _invocation_log,
        "subject_selection_readonly_clone",
        tmp_path_factory.mktemp("subject_selection_readonly") / "clone",
        _clone_for_subject_selection,
    )


@pytest.fixture(scope="module")
def real_repository_hook_run(_invocation_log: Path) -> subprocess.CompletedProcess[str]:
    """One `bash githooks/pre-commit` run against this repository, read by two cells."""
    _record_invocation(_invocation_log, "real_repository_hook_run")
    return proc_run.run(
        ["bash", str(PRE_COMMIT_HOOK)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# check_no_account_id.sh
# ---------------------------------------------------------------------------


class TestCheckNoAccountId:
    def test_passes_on_the_real_repository(self) -> None:
        """The real committed content contains no violation."""
        result = _run_account_id_gate(REPO_ROOT)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_fails_on_a_real_aws_account_id_in_an_arn(self, tmp_path: Path) -> None:
        account_id = _synthetic_account_id()
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.tf").write_text(
            f'role_arn = "arn:aws:iam::{account_id}:role/example"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "VIOLATION" in result.stderr
        assert account_id in result.stderr

    def test_fails_on_a_vpc_id(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.tf").write_text(f'vpc_id = "{_synthetic_vpc_id()}"\n', encoding="utf-8")
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "vpc-" in result.stderr

    def test_fails_on_a_subnet_id(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.tf").write_text(
            f'subnet_id = "{_synthetic_subnet_id()}"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "subnet-" in result.stderr

    def test_fails_on_a_security_group_id(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.tf").write_text(
            f'security_group_id = "{_synthetic_security_group_id()}"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "sg-" in result.stderr

    def test_fails_on_a_vpc_endpoint_id(self, tmp_path: Path) -> None:
        """A VPC endpoint id (`vpce-`) is a violation."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.tf").write_text(
            f'vpc_endpoint_id = "{_synthetic_vpc_endpoint_id()}"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "vpce-" in result.stderr

    def test_fails_on_a_route_table_id(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.tf").write_text(
            f'route_table_id = "{_synthetic_route_table_id()}"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "rtb-" in result.stderr

    def test_fails_on_an_internet_gateway_id(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.tf").write_text(
            f'internet_gateway_id = "{_synthetic_internet_gateway_id()}"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "igw-" in result.stderr

    def test_fails_on_a_cloudfront_vpc_origin_id(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.tf").write_text(
            f'vpc_origin_id = "{_synthetic_cloudfront_vpc_origin_id()}"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "vo_" in result.stderr

    def test_lowercase_identifiers_beginning_vo_are_not_violations(self, tmp_path: Path) -> None:
        """All-lowercase snake_case names beginning `vo_`, however long, are not violations."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "ok.py").write_text(
            "vo_id = 3\nvo_arn = None\nvo_configuration = {}\nvo_prefixed_thing = 1\n",
            encoding="utf-8",
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 0, result.stderr

    def test_a_single_uppercase_or_digit_is_not_enough_to_flag_a_vo_identifier(
        self, tmp_path: Path
    ) -> None:
        """A `vo_` name with only one character from `[A-Z0-9]` is not a violation."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "ok.py").write_text("vo_" + "origin2" + " = 1\n", encoding="utf-8")
        _stage_all(tmp_path)

        assert _run_account_id_gate(tmp_path).returncode == 0

    _PLACEHOLDER_PREFIXES = ("vpc", "subnet", "vpce", "rtb", "igw")

    #: Eight zeros then non-zero hex: excused only by `_PLACEHOLDER_RESOURCE_IDS`. An all-zero id
    #: would also be excused by the `000000000000` account-id placeholder, so it cannot discriminate.
    _ZERO_PREFIXED_SUFFIX = "0" * 8 + "1a2b3c4d5"

    def test_placeholder_ids_are_excused_for_the_placeholder_prefixes(self, tmp_path: Path) -> None:
        """Zero-prefixed and documentation-form ids pass under every placeholder prefix.

        The per-shape edges (eight zeros vs seven, same-line real ids) are pinned in
        `tests/unit/test_gates.py::TestPlaceholderResourceIdsAreNarrow`.
        """
        _init_scratch_repo(tmp_path)
        canonical = _canonical_documentation_id_suffix()
        lines = [
            f'{prefix.upper()} = ["{prefix}-{self._ZERO_PREFIXED_SUFFIX}", "{prefix}-{canonical}"]\n'
            for prefix in self._PLACEHOLDER_PREFIXES
        ]
        (tmp_path / "fixtures.py").write_text("".join(lines), encoding="utf-8")
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "resource_id",
        [
            _synthetic_vpc_id(),
            _synthetic_subnet_id(),
            "sg-" + _ZERO_PREFIXED_SUFFIX,
            "sg-" + _canonical_documentation_id_suffix(),
        ],
    )
    def test_a_real_shaped_id_or_an_unexcused_prefix_is_still_a_violation(
        self, tmp_path: Path, resource_id: str
    ) -> None:
        """A non-placeholder vpc/subnet id fails, and `sg-` stays strict even in placeholder form."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "fixtures.py").write_text(f'ID = "{resource_id}"\n', encoding="utf-8")
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1, (
            f"{resource_id} must still be a violation; the placeholder excuse leaked beyond its "
            f"prefixes or shapes: {result.stderr}"
        )
        assert resource_id in result.stderr

    def test_fails_on_a_private_key_block(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.pem").write_text(_fake_private_key_block(), encoding="utf-8")
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "PRIVATE KEY" in result.stderr

    def test_fails_on_an_absolute_local_path(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.md").write_text(
            f"State is stored at {_synthetic_home_path()}/state.tfstate\n", encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "/Users/" in result.stderr

    def test_placeholder_account_ids_are_not_flagged(self, tmp_path: Path) -> None:
        """000000000000 and 123456789012 are documented non-secret placeholders, not a real leak."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "fixture.py").write_text(
            'ARN_A = "arn:aws:iam::000000000000:role/test"\n'
            'ARN_B = "arn:aws:iam::123456789012:role/example"\n',
            encoding="utf-8",
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 0, result.stderr

    # -- IAM user names ----------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "arn",
        [
            _iam_user_arn("jdoe"),
            _iam_user_arn("ops/jdoe"),
            _iam_user_arn("jdoe", partition="aws-us-gov"),
            _iam_user_arn("example-deployer2"),
            _iam_user_arn("smoke-tester"),
        ],
    )
    def test_fails_on_a_non_placeholder_iam_user_name(self, tmp_path: Path, arn: str) -> None:
        """An IAM user ARN whose name is not a documentation placeholder is a violation, even
        under the placeholder account id."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "plan.json").write_text(f'{{"arn": "{arn}"}}\n', encoding="utf-8")
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "VIOLATION" in result.stderr
        assert arn in result.stderr

    @pytest.mark.parametrize("user", ["example-deployer", "smoke-test"])
    def test_placeholder_iam_user_names_are_not_flagged(self, tmp_path: Path, user: str) -> None:
        """The documentation placeholder user names pass under a placeholder account id."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "plan.json").write_text(
            f'{{"arn": "{_iam_user_arn(user)}"}}\n{_iam_user_arn(user)}\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 0, result.stderr

    def test_a_placeholder_user_name_does_not_excuse_a_real_account_id(
        self, tmp_path: Path
    ) -> None:
        """A real account id in a placeholder-named user ARN is still a violation."""
        account_id = _synthetic_account_id()
        _init_scratch_repo(tmp_path)
        (tmp_path / "plan.json").write_text(
            f'{{"arn": "{_iam_user_arn("example-deployer", account_id=account_id)}"}}\n',
            encoding="utf-8",
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1, result.stdout + result.stderr
        assert account_id in result.stderr

    def test_a_placeholder_user_does_not_excuse_a_real_user_on_the_same_line(
        self, tmp_path: Path
    ) -> None:
        """A non-placeholder user name sharing a line with a placeholder one is still a violation."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "doc.md").write_text(
            f"{_iam_user_arn('example-deployer')} {_iam_user_arn('jdoe')}\n", encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1, result.stdout + result.stderr
        assert _iam_user_arn("jdoe") in result.stderr

    def test_fails_on_a_bare_account_id_embedded_in_a_resource_name(self, tmp_path: Path) -> None:
        """A bare account id embedded in a resource name, neither an ARN field nor a named
        assignment, is a violation."""
        account_id = _synthetic_bare_account_id()
        _init_scratch_repo(tmp_path)
        (tmp_path / "backend.tf").write_text(
            f'bucket = "crm-agent-eval-tfstate-{account_id}-us-east-1"\n', encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 1
        assert "VIOLATION" in result.stderr
        assert account_id in result.stderr

    # -- the placeholder exclusion ---------------------------------------------------------------
    #
    # Placeholder ids are redacted from a matched line and the line is re-tested, rather than the
    # whole line being dropped.

    def test_a_real_id_sharing_a_line_with_the_documented_placeholder_still_fails(
        self, tmp_path: Path
    ) -> None:
        """A real account id on the same line as the documented placeholder is still a violation."""
        real_id = "98765" + "4321098"
        _init_scratch_repo(tmp_path)
        (tmp_path / "doc.md").write_text(
            f"example {'123456789012'} but also {real_id}\n", encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)
        assert result.returncode == 1
        assert real_id in result.stderr

    def test_a_private_key_sharing_a_line_with_a_placeholder_still_fails(
        self, tmp_path: Path
    ) -> None:
        """A private-key marker on the same line as a placeholder account id is still a violation."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "doc.md").write_text(
            f"placeholder {'123456789012'} and {_fake_private_key_block().splitlines()[0]}\n",
            encoding="utf-8",
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)
        assert result.returncode == 1
        assert "PRIVATE KEY" in result.stderr

    def test_bare_digit_pattern_does_not_flag_a_decimal_figure(self, tmp_path: Path) -> None:
        """A 12-digit substring embedded in a longer decimal (e.g. an eval-methodology figure like
        `0.7203907203907204`) does not match the bare-digit pattern."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "figures.md").write_text(
            "mean_correctness: 0.7203907203907204\n", encoding="utf-8"
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 0, result.stderr

    def test_bare_digit_pattern_does_not_flag_a_hex_digest_fragment(self, tmp_path: Path) -> None:
        """A 12-digit substring embedded in a longer hex digest (e.g. a Terraform provider lock
        hash) does not match the bare-digit pattern."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "lock.hcl").write_text(
            '    "zh:8d8596257453357c9f3fccaa7d2f04299e8d35b16f364adb8a2c829143a9c090",\n',
            encoding="utf-8",
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 0, result.stderr

    def test_bare_digit_pattern_does_not_flag_a_synthetic_repeated_digit_uuid(
        self, tmp_path: Path
    ) -> None:
        """A synthetic KMS-key-shaped UUID whose 12-digit segment is one repeated digit passes, via
        the exemption in `_PLACEHOLDER_ACCOUNT_IDS`."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "kms_fixture.py").write_text(
            'CMK_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"\n',
            encoding="utf-8",
        )
        _stage_all(tmp_path)

        result = _run_account_id_gate(tmp_path)

        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# check_no_vendor_keys.sh
# ---------------------------------------------------------------------------


class TestCheckNoVendorKeys:
    def test_passes_on_the_real_repository(self) -> None:
        result = _run_vendor_keys_gate(REPO_ROOT)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_fails_on_an_anthropic_key(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.py").write_text(_fake_vendor_key_line(), encoding="utf-8")
        _stage_all(tmp_path)

        result = _run_vendor_keys_gate(tmp_path)

        assert result.returncode == 1
        assert "VIOLATION" in result.stderr
        assert "sk-ant-" in result.stderr

    def test_fails_on_an_openai_key(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / "leak.py").write_text(_fake_openai_key_line(), encoding="utf-8")
        _stage_all(tmp_path)

        result = _run_vendor_keys_gate(tmp_path)

        assert result.returncode == 1
        assert "VIOLATION" in result.stderr

    def test_fails_on_a_vendor_env_var_assignment(self, tmp_path: Path) -> None:
        _init_scratch_repo(tmp_path)
        (tmp_path / ".env").write_text(_fake_vendor_env_assignment(), encoding="utf-8")
        _stage_all(tmp_path)

        result = _run_vendor_keys_gate(tmp_path)

        assert result.returncode == 1

    def test_ordinary_bedrock_model_id_is_not_flagged(self, tmp_path: Path) -> None:
        """A Bedrock model identifier is not vendor-key-shaped and must not false-positive."""
        _init_scratch_repo(tmp_path)
        (tmp_path / "config.yaml").write_text(
            "model_id: anthropic.claude-opus-5-5\ninvoke_profile: global.anthropic.claude-opus-5-5\n",
            encoding="utf-8",
        )
        _stage_all(tmp_path)

        result = _run_vendor_keys_gate(tmp_path)

        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# check_log_redaction.py
# ---------------------------------------------------------------------------


class TestCheckLogRedaction:
    def test_passes_on_the_real_src_tree(self) -> None:
        """Every real logging call site under src/ today is provably redacted or literal-safe."""
        exit_code = log_redaction_gate.main(["--src-root", str(REPO_ROOT / "src")])
        assert exit_code == 0

    def test_every_real_call_site_is_resolved_not_merely_present(self) -> None:
        """The gate finds at least one real call site under src/ and resolves all of them."""
        sites = log_redaction_gate.scan_tree(REPO_ROOT / "src")
        assert len(sites) > 0, "expected at least one real logging call site under src/"
        unresolved = [site for site in sites if site.classification == "unresolved"]
        assert unresolved == []

    def test_fails_on_a_bare_variable_passed_to_a_log_call(self, tmp_path: Path) -> None:
        """A log call whose argument is a bare variable (not redact() output, not a safe dict
        literal) is unresolved."""
        module = tmp_path / "leaky_module.py"
        module.write_text(
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(claims):\n"
            "    logger.info(claims)\n",
            encoding="utf-8",
        )

        exit_code = log_redaction_gate.main(["--src-root", str(tmp_path)])

        assert exit_code == 1

    def test_fails_on_a_dict_literal_with_an_unlisted_key(self, tmp_path: Path) -> None:
        """A literal dict containing a key outside the safe allowlist (e.g. "token") fails."""
        module = tmp_path / "leaky_module.py"
        module.write_text(
            "import json\n"
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(token):\n"
            '    logger.info(json.dumps({"event": "x", "token": token}))\n',
            encoding="utf-8",
        )

        exit_code = log_redaction_gate.main(["--src-root", str(tmp_path)])

        assert exit_code == 1

    def test_passes_on_a_call_through_redact(self, tmp_path: Path) -> None:
        """A log call whose argument is redact(...) passes regardless of its content."""
        module = tmp_path / "safe_module.py"
        module.write_text(
            "import json\n"
            "import logging\n"
            "from crm.metering import redact\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(payload):\n"
            "    logger.info(json.dumps(redact(payload)))\n",
            encoding="utf-8",
        )

        exit_code = log_redaction_gate.main(["--src-root", str(tmp_path)])

        assert exit_code == 0

    def test_passes_on_a_dict_literal_update_with_redacted_content(self, tmp_path: Path) -> None:
        """A safe-keyed literal later updated with redact() output resolves."""
        module = tmp_path / "safe_update.py"
        module.write_text(
            "import json\n"
            "import logging\n"
            "from crm.metering import redact\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(fields):\n"
            '    payload = {"event": "tool_call"}\n'
            "    payload.update(redact(fields))\n"
            "    logger.info(json.dumps(payload))\n",
            encoding="utf-8",
        )

        exit_code = log_redaction_gate.main(["--src-root", str(tmp_path)])

        assert exit_code == 0

    def test_fails_on_a_dict_literal_update_with_unredacted_content(self, tmp_path: Path) -> None:
        """Updating a safe literal with an unredacted variable fails."""
        module = tmp_path / "leaky_update.py"
        module.write_text(
            "import json\n"
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(fields):\n"
            '    payload = {"event": "tool_call"}\n'
            "    payload.update(fields)\n"
            "    logger.info(json.dumps(payload))\n",
            encoding="utf-8",
        )

        exit_code = log_redaction_gate.main(["--src-root", str(tmp_path)])

        assert exit_code == 1

    def test_debug_and_warning_levels_are_also_scanned(self, tmp_path: Path) -> None:
        """Every stdlib logging level, not only .info(), is scanned as a call site."""
        module = tmp_path / "other_levels.py"
        module.write_text(
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(secret_value):\n"
            "    logger.warning(secret_value)\n"
            "    logger.debug(secret_value)\n",
            encoding="utf-8",
        )

        exit_code = log_redaction_gate.main(["--src-root", str(tmp_path)])

        assert exit_code == 1
        sites = log_redaction_gate.scan_tree(tmp_path)
        assert {s.classification for s in sites} == {"unresolved"}
        assert len(sites) == 2

    # -- the gate classifies the whole call, not only args[0].

    def test_fails_on_a_secret_passed_as_a_later_positional_argument(self, tmp_path: Path) -> None:
        """`logger.info(json.dumps({"event": "x"}), token)` — a safe message plus a `%`-format
        interpolation argument the formatter splices into the emitted line."""
        module = tmp_path / "extra_positional.py"
        module.write_text(
            "import json\n"
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(token):\n"
            '    logger.info(json.dumps({"event": "x"}), token)\n',
            encoding="utf-8",
        )

        assert log_redaction_gate.main(["--src-root", str(tmp_path)]) == 1
        (site,) = log_redaction_gate.scan_tree(tmp_path)
        assert site.classification == "unresolved"
        assert "interpolation argument" in site.detail

    def test_fails_on_a_secret_passed_under_the_extra_keyword(self, tmp_path: Path) -> None:
        """`extra=` merges arbitrary caller data into the emitted record; it is never safe."""
        module = tmp_path / "extra_keyword.py"
        module.write_text(
            "import json\n"
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(token, record):\n"
            '    logger.info(json.dumps({"event": "y"}), extra={"token": token, "r": record})\n',
            encoding="utf-8",
        )

        assert log_redaction_gate.main(["--src-root", str(tmp_path)]) == 1
        (site,) = log_redaction_gate.scan_tree(tmp_path)
        assert site.classification == "unresolved"
        assert "'extra'" in site.detail

    def test_fails_on_logger_log_whose_message_is_the_second_positional(
        self, tmp_path: Path
    ) -> None:
        """`logger.log(level, msg)` is classified on its message at args[1], not the level."""
        module = tmp_path / "log_method.py"
        module.write_text(
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(record):\n"
            "    logger.log(logging.INFO, record)\n",
            encoding="utf-8",
        )

        assert log_redaction_gate.main(["--src-root", str(tmp_path)]) == 1

    def test_fails_on_a_star_kwargs_log_call(self, tmp_path: Path) -> None:
        """Keywords assembled at runtime cannot be resolved, so they must not pass."""
        module = tmp_path / "star_kwargs.py"
        module.write_text(
            "import json\n"
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler(options):\n"
            '    logger.info(json.dumps({"event": "z"}), **options)\n',
            encoding="utf-8",
        )

        assert log_redaction_gate.main(["--src-root", str(tmp_path)]) == 1

    def test_still_passes_a_safe_message_with_only_allowed_literal_keywords(
        self, tmp_path: Path
    ) -> None:
        """A safe message with the allowed literal keyword `exc_info=True` still resolves."""
        module = tmp_path / "safe_exc_info.py"
        module.write_text(
            "import json\n"
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def handler():\n"
            '    logger.error(json.dumps({"event": "failed"}), exc_info=True)\n',
            encoding="utf-8",
        )

        assert log_redaction_gate.main(["--src-root", str(tmp_path)]) == 0


# ---------------------------------------------------------------------------
# .gitleaks.toml — non-vacuity of the vendor-key rules and the account-id allowlist
# ---------------------------------------------------------------------------


class TestGitleaksConfig:
    @pytest.fixture(autouse=True)
    def _local_rule_set(self) -> None:
        require_local_rule_set()

    @staticmethod
    def _gitleaks_available() -> bool:
        return proc_run.run(["which", "gitleaks"], capture_output=True).returncode == 0

    def test_config_file_exists_and_is_valid_toml(self) -> None:
        import tomllib

        with GITLEAKS_CONFIG.open("rb") as handle:
            data = tomllib.load(handle)
        assert "rules" in data
        assert any(rule["id"] == "anthropic-api-key" for rule in data["rules"])
        assert any(rule["id"] == "openai-api-key" for rule in data["rules"])

    def test_passes_on_the_real_repository(self) -> None:
        """The local rule set reports nothing over the repository's non-ignored files."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        result = proc_run.run(
            [
                sys.executable,
                str(CHECK_SECRET_SCAN),
                "--source",
                str(REPO_ROOT),
                "--config",
                str(GITLEAKS_CONFIG),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_fails_on_an_injected_anthropic_key(self, tmp_path: Path) -> None:
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        (tmp_path / "leak.py").write_text(_fake_vendor_key_line(), encoding="utf-8")

        result = proc_run.run(
            [
                "gitleaks",
                "detect",
                "--no-git",
                "-c",
                str(GITLEAKS_CONFIG),
                "--source",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "leaks found: 3" in result.stderr, result.stdout + result.stderr

    def test_fails_on_a_private_key(self, tmp_path: Path) -> None:
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        (tmp_path / "leak.pem").write_text(_fake_private_key_block(), encoding="utf-8")

        result = proc_run.run(
            [
                "gitleaks",
                "detect",
                "--no-git",
                "-c",
                str(GITLEAKS_CONFIG),
                "--source",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "leaks found: 1" in result.stderr, result.stdout + result.stderr

    def test_placeholder_account_id_is_allowlisted(self, tmp_path: Path) -> None:
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        (tmp_path / "fixture.py").write_text(
            'ARN = "arn:aws:iam::123456789012:role/example"\n', encoding="utf-8"
        )

        result = proc_run.run(
            [
                "gitleaks",
                "detect",
                "--no-git",
                "-c",
                str(GITLEAKS_CONFIG),
                "--source",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Tracked pre-commit hook
# ---------------------------------------------------------------------------


class TestPreCommitHook:
    def test_hook_is_tracked_and_executable(self) -> None:
        tracked = proc_run.run(
            ["git", "ls-files", "--error-unmatch", str(PRE_COMMIT_HOOK)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert tracked.returncode == 0, "githooks/pre-commit must be tracked in git"
        assert PRE_COMMIT_HOOK.stat().st_mode & 0o111, "githooks/pre-commit must be executable"

    @_SHARED_SUBJECT_GROUP
    def test_hook_passes_on_the_real_repository(
        self, real_repository_hook_run: subprocess.CompletedProcess[str]
    ) -> None:
        assert real_repository_hook_run.returncode == 0, (
            real_repository_hook_run.stdout + real_repository_hook_run.stderr
        )

    def test_an_absent_gitleaks_fails_the_hook_rather_than_skipping(self) -> None:
        """With the gitleaks binary absent the hook exits non-zero and does not report a pass."""
        result = proc_run.run(
            ["bash", str(PRE_COMMIT_HOOK)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "GITLEAKS_BIN": "gitleaks-deliberately-not-installed"},
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, f"an absent scanner exited 0:\n{combined}"
        assert "FAILING rather than skipping" in combined, combined
        assert "commit blocked" in combined, combined
        assert "all content-hygiene gates passed" not in combined, combined

    def test_hook_is_installable_via_core_hooks_path(self, tmp_path: Path) -> None:
        """The hook installed via `core.hooksPath` runs on a real commit of compliant content and
        exits 0."""
        _init_scratch_repo(tmp_path)
        hooks_dir = tmp_path / "githooks"
        hooks_dir.mkdir()
        hook_dest = hooks_dir / "pre-commit"
        hook_dest.write_text(PRE_COMMIT_HOOK.read_text(encoding="utf-8"), encoding="utf-8")
        hook_dest.chmod(0o755)
        # The hook resolves its scripts and any local .gitleaks.toml relative to the repo root.
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        for name in (
            "check_no_account_id.sh",
            "check_no_vendor_keys.sh",
            "check_secret_scan.py",
            "proc_run.py",
        ):
            dest = scripts_dir / name
            dest.write_text(
                (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
            dest.chmod(0o755)
        install_local_rule_set(tmp_path)
        (tmp_path / "harmless.txt").write_text("nothing sensitive here\n", encoding="utf-8")

        proc_run.run(["git", "config", "core.hooksPath", "githooks"], cwd=tmp_path, check=True)
        _stage_all(tmp_path)

        # `core.hooksPath` hooks fire only on an actual commit.
        commit = proc_run.run(
            ["git", "commit", "-q", "-m", "scratch"], cwd=tmp_path, capture_output=True, text=True
        )
        assert commit.returncode == 0, commit.stdout + commit.stderr

        result = proc_run.run(
            ["bash", str(hook_dest)], cwd=tmp_path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_pre_commit_config_yaml_is_valid_and_wires_the_same_scripts(self) -> None:
        import yaml

        with PRE_COMMIT_CONFIG_YAML.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)

        hook_entries = [hook["entry"] for repo in config["repos"] for hook in repo["hooks"]]
        assert any("check_no_account_id.sh" in entry for entry in hook_entries)
        assert any("check_no_vendor_keys.sh" in entry for entry in hook_entries)
        assert any("check_log_redaction.py" in entry for entry in hook_entries)
        assert any("gitleaks" in entry for entry in hook_entries)


class TestThePreCommitHookScansTheStagedTree:
    """The hook's gitleaks gate reads the staged diff, not only the files on disk.

    Every probe is an AWS-access-key-id shape, which neither shell gate matches, so only the
    gitleaks gate can refuse it. Probes are planted in a scratch repository.
    """

    @staticmethod
    def _gitleaks_available() -> bool:
        return proc_run.run(["which", "gitleaks"], capture_output=True).returncode == 0

    def _scratch_repo_with_hook(self, root: Path, hook_text: str | None = None) -> Path:
        """Build a self-contained scratch repo with the real hook, gates and any local config.

        Returns the installed hook's path. `hook_text` overrides the hook body.
        """
        _init_scratch_repo(root)
        hooks_dir = root / "githooks"
        hooks_dir.mkdir()
        hook_dest = hooks_dir / "pre-commit"
        hook_dest.write_text(
            hook_text if hook_text is not None else PRE_COMMIT_HOOK.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        hook_dest.chmod(0o755)
        scripts_dir = root / "scripts"
        scripts_dir.mkdir()
        for name in (
            "check_no_account_id.sh",
            "check_no_vendor_keys.sh",
            "check_secret_scan.py",
            "proc_run.py",
        ):
            dest = scripts_dir / name
            dest.write_text(
                (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
            dest.chmod(0o755)
        install_local_rule_set(root)
        (root / "harmless.txt").write_text("nothing sensitive here\n", encoding="utf-8")
        proc_run.run(["git", "config", "core.hooksPath", "githooks"], cwd=root, check=True)
        _stage_all(root)
        proc_run.run(["git", "commit", "-q", "--no-verify", "-m", "base"], cwd=root, check=True)
        return hook_dest

    @staticmethod
    def _stage_then_remove_from_disk(root: Path, relative: str, content: str) -> None:
        """Stage `content` at `relative`, then delete the working-tree copy, leaving it present in
        the index and absent from disk."""
        target = root / relative
        target.write_text(content, encoding="utf-8")
        proc_run.run(["git", "add", relative], cwd=root, check=True)
        target.unlink()
        assert not target.exists(), "the probe must be absent from disk for this to test anything"
        staged = proc_run.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        assert relative in staged.stdout, f"probe was not staged: {staged.stdout!r}"

    def _run_hook(self, hook: Path, root: Path) -> subprocess.CompletedProcess[str]:
        return proc_run.run(["bash", str(hook)], cwd=root, capture_output=True, text=True)

    def test_a_staged_only_secret_is_refused(self, tmp_path: Path) -> None:
        """A secret staged but absent from the working tree is refused."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._scratch_repo_with_hook(tmp_path)
        self._stage_then_remove_from_disk(
            tmp_path, "probe.py", f'cred = "{_fake_aws_access_key_id()}"\n'
        )

        result = self._run_hook(hook, tmp_path)
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            "a secret staged but absent from the working tree was NOT refused, so the gitleaks gate "
            "is still reading the wrong subject:\n" + combined
        )
        assert "all content-hygiene gates passed" not in combined, combined

    def test_the_hook_passes_on_a_clean_staged_tree(self, tmp_path: Path) -> None:
        """The hook passes on a clean staged tree."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._scratch_repo_with_hook(tmp_path)
        (tmp_path / "more.txt").write_text("still nothing sensitive\n", encoding="utf-8")
        _stage_all(tmp_path)

        result = self._run_hook(hook, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "all content-hygiene gates passed" in result.stdout

    def test_the_pre_fix_invocation_would_have_passed_the_same_probe(self, tmp_path: Path) -> None:
        """With the staged scan neutralised, the identical staged-only probe passes the hook."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        staged_scan = '"$GITLEAKS_BIN" protect --staged --redact'
        hook_text = PRE_COMMIT_HOOK.read_text(encoding="utf-8")
        assert hook_text.count(staged_scan) == 1, (
            "the staged-scan invocation this test neutralises is not in the hook verbatim, so the "
            "test would silently stop discriminating; update the literal above"
        )
        pre_fix = hook_text.replace(staged_scan, "true")

        hook = self._scratch_repo_with_hook(tmp_path, hook_text=pre_fix)
        self._stage_then_remove_from_disk(
            tmp_path, "probe.py", f'cred = "{_fake_aws_access_key_id()}"\n'
        )

        result = self._run_hook(hook, tmp_path)
        assert result.returncode == 0, (
            "the pre-fix hook already refused this probe, so the test above is not attributable to "
            "the staged scan:\n" + result.stdout + result.stderr
        )

    def test_the_worktree_subject_is_still_scanned(self, tmp_path: Path) -> None:
        """An unstaged secret on disk is still reported by the hook's worktree scan."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._scratch_repo_with_hook(tmp_path)
        (tmp_path / "on_disk_only.py").write_text(
            f'cred = "{_fake_aws_access_key_id()}"\n', encoding="utf-8"
        )

        result = self._run_hook(hook, tmp_path)
        assert result.returncode != 0, (
            "an unstaged secret on disk was not reported, so the working-tree subject is gone:\n"
            + result.stdout
            + result.stderr
        )

    def test_a_real_commit_is_blocked_when_installed_via_core_hooks_path(
        self, tmp_path: Path
    ) -> None:
        """An ordinary `git commit` of a staged-only secret is refused by the installed hook."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        self._scratch_repo_with_hook(tmp_path)
        self._stage_then_remove_from_disk(
            tmp_path, "probe.py", f'cred = "{_fake_aws_access_key_id()}"\n'
        )

        commit = proc_run.run(
            ["git", "commit", "-q", "-m", "should be refused"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert commit.returncode != 0, (
            "the commit succeeded, so a staged secret still reaches history: "
            + commit.stdout
            + commit.stderr
        )
        recorded = proc_run.run(
            ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True
        )
        assert "should be refused" not in recorded.stdout, recorded.stdout

    def test_both_subjects_are_named_in_the_hook_and_the_declared_config(self) -> None:
        """Both the staged and worktree scans are named in the hook and in `.pre-commit-config.yaml`."""
        hook_text = PRE_COMMIT_HOOK.read_text(encoding="utf-8")
        assert "protect --staged" in hook_text
        assert "python3 scripts/check_secret_scan.py" in hook_text
        assert "detect --no-git" not in hook_text, "the hook still runs a raw working-tree scan"

        import yaml

        config = yaml.safe_load(PRE_COMMIT_CONFIG_YAML.read_text(encoding="utf-8"))
        entries = [hook["entry"] for repo in config["repos"] for hook in repo["hooks"]]
        assert any("protect --staged" in entry for entry in entries), entries
        assert any(entry.endswith("python3 scripts/check_secret_scan.py") for entry in entries), (
            entries
        )
        assert not any("detect --no-git" in entry for entry in entries), entries

    def test_a_gitignored_secret_does_not_block_the_hook(self, tmp_path: Path) -> None:
        """A built-in-rule match in a gitignored file does not fail the working-tree scan."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._scratch_repo_with_hook(tmp_path)
        _plant_ignored_secret(tmp_path)

        result = self._run_hook(hook, tmp_path)
        assert result.returncode == 0, (
            "a secret in a gitignored file blocked the hook, so the working-tree scan still reads "
            "ignored paths:\n" + result.stdout + result.stderr
        )
        assert "all content-hygiene gates passed" in result.stdout

    def test_the_same_secret_untracked_and_not_ignored_blocks_the_hook(
        self, tmp_path: Path
    ) -> None:
        """The identical plant, untracked but not ignored, fails the hook (control for the above)."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._scratch_repo_with_hook(tmp_path)
        _plant_ignored_secret(tmp_path)
        (tmp_path / ".gitignore").unlink()

        result = self._run_hook(hook, tmp_path)
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            "an untracked, non-ignored secret did not fail the hook:\n" + combined
        )
        assert f"{_IGNORED_PLANT_DIR}/x.json" in combined, combined
        assert "all content-hygiene gates passed" not in combined, combined

    def test_a_gate_that_verified_nothing_fails_the_hook(self, tmp_path: Path) -> None:
        """Gate exit 3 (gitleaks absent: verified nothing) fails the hook rather than passing it."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._scratch_repo_with_hook(tmp_path)
        (tmp_path / "scripts" / "check_secret_scan.py").write_text(
            "import sys\nsys.exit(3)\n", encoding="utf-8"
        )

        result = self._run_hook(hook, tmp_path)
        combined = result.stdout + result.stderr
        assert result.returncode != 0, f"gate exit 3 did not fail the hook:\n{combined}"
        assert "commit blocked" in combined, combined
        assert "all content-hygiene gates passed" not in combined, combined


class TestTheSecretScanDoesNotWalkTheGitDirectory:
    """`.git/` is excluded from the filesystem scan, and tracked content still is not."""

    @pytest.fixture(autouse=True)
    def _local_rule_set(self) -> None:
        require_local_rule_set()

    @staticmethod
    def _gitleaks_available() -> bool:
        return proc_run.run(["which", "gitleaks"], capture_output=True).returncode == 0

    def _scan(self, source: Path) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["gitleaks", "detect", "--no-git", "-c", str(GITLEAKS_CONFIG), "--source", str(source)],
            capture_output=True,
            text=True,
        )

    def test_a_secret_inside_the_git_directory_is_not_reported(self, tmp_path: Path) -> None:
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        # `lost-found/other/` is the path `git fsck --lost-found` writes.
        planted = tmp_path / ".git" / "lost-found" / "other"
        planted.mkdir(parents=True)
        (planted / ("0" * 40)).write_text(_fake_vendor_key_line(), encoding="utf-8")

        result = self._scan(tmp_path)
        assert result.returncode == 0, (
            "a secret inside .git/ was reported, so the gate still reds on a clean working tree: "
            + result.stdout
            + result.stderr
        )

    def test_the_same_secret_in_tracked_content_is_still_reported(self, tmp_path: Path) -> None:
        """The same content outside `.git/` is still reported."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        (tmp_path / "leak.py").write_text(_fake_vendor_key_line(), encoding="utf-8")

        result = self._scan(tmp_path)
        assert result.returncode == 1, (
            "the same content in tracked code was NOT reported, so the exclusion is too broad: "
            + result.stdout
            + result.stderr
        )

    def test_a_path_merely_containing_git_is_not_excluded(self, tmp_path: Path) -> None:
        """The `.git/` exclusion does not cover paths that merely contain `git`."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        for relative in ("legit/leak.py", ".github/workflows/leak.py", "notgit/leak.py"):
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_fake_vendor_key_line(), encoding="utf-8")
            result = self._scan(tmp_path)
            assert result.returncode == 1, f"{relative} was excluded: {result.stderr}"
            target.unlink()


class TestCheckNoAccountIdRefMode:
    """`check_no_account_id.sh --ref <ref>`, whose path exclusions are anchored on git grep's `<ref>:`
    output prefix.

    Every test plants into an isolated clone, never the shared worktree.
    """

    @staticmethod
    def _clone(tmp_path: Path) -> Path:
        """A per-test clone, for the cells that mutate their subject."""
        return _clone_for_ref_mode(tmp_path / "clone")

    @staticmethod
    def _run(work: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["bash", "scripts/check_no_account_id.sh", *args],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    @staticmethod
    def _commit(work: Path, relative: str, text: str) -> None:
        target = work / relative
        target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")
        proc_run.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
        proc_run.run(
            ["git", "-C", str(work), "commit", "-qm", "probe"], check=True, capture_output=True
        )

    @_SHARED_SUBJECT_GROUP
    def test_ref_mode_passes_on_a_clean_tree(
        self, refmode_clean_ref_head_scan: subprocess.CompletedProcess[str]
    ) -> None:
        """`--ref HEAD` passes on a clean tree."""
        result = refmode_clean_ref_head_scan
        assert result.returncode == 0, (
            "`--ref HEAD` fails on a clean tree. The path exclusions are almost certainly anchored with "
            f"a bare `^` again, which `git grep <ref>`'s `<ref>:` prefix defeats:\n{result.stderr}"
        )

    def test_ref_mode_still_catches_a_violation_committed_in_the_ref(self, tmp_path: Path) -> None:
        """`--ref HEAD` catches an account id committed in the ref and names it on stderr."""
        work = self._clone(tmp_path)
        planted = _synthetic_account_id()
        self._commit(work, "README.md", f"\nAccount: {planted}\n")

        result = self._run(work, "--ref", "HEAD")
        assert result.returncode == 1, "a real account id committed in the ref was not caught"
        assert planted in result.stderr, f"the violation is not named:\n{result.stderr}"

    @_SHARED_SUBJECT_GROUP
    def test_worktree_and_ref_mode_agree_on_a_clean_tree(
        self,
        refmode_clean_worktree_scan: subprocess.CompletedProcess[str],
        refmode_clean_ref_head_scan: subprocess.CompletedProcess[str],
    ) -> None:
        """`--worktree` and `--ref HEAD` agree on the same clean clone."""
        assert refmode_clean_worktree_scan.returncode == refmode_clean_ref_head_scan.returncode

    @_SHARED_SUBJECT_GROUP
    @pytest.mark.parametrize("bad_ref", ["evil$(x)", "a|b", "x*y", "[abc]"])
    def test_a_ref_name_with_a_regex_metacharacter_is_refused_loudly(
        self, refmode_clean_clone: Path, bad_ref: str
    ) -> None:
        """A `--ref` name containing a regex metacharacter is refused with exit 2."""
        result = self._run(refmode_clean_clone, "--ref", bad_ref)
        assert result.returncode == 2, f"`--ref {bad_ref}` was not refused: {result.stderr}"
        assert "regex metacharacter" in result.stderr

    @pytest.mark.parametrize("good_ref", ["HEAD", "HEAD~1", "HEAD^"])
    def test_ordinary_ref_names_are_accepted(self, good_ref: str, tmp_path: Path) -> None:
        """Ordinary ref names, including `~` and `^` forms, are accepted and pass on a clean tree.

        Each ref name builds a different exclusion anchor, so each runs its own scan.
        """
        work = self._clone(tmp_path)
        self._commit(work, "README.md", "\nprobe line\n")
        result = self._run(work, "--ref", good_ref)
        assert result.returncode == 0, f"`--ref {good_ref}` was refused or failed: {result.stderr}"


class TestSubjectSelection:
    """Subject selection for the hygiene gates: `--worktree`, `--cached` and `--ref HEAD` are
    distinct required subjects, and the pre-commit hook reads the staged tree.

    Every test works in an isolated clone and assembles its literal at runtime.
    """

    @staticmethod
    def _clone(tmp_path: Path) -> Path:
        """A per-test clone, for the cells that mutate their subject; built from the worktree's
        copies of the gates, the hook and this file."""
        return _clone_for_subject_selection(tmp_path / "clone")

    @staticmethod
    def _stage_then_remove_from_worktree(work: Path, relative: str, text: str) -> None:
        """Stage `text` at `relative`, then delete it from disk."""
        target = work / relative
        target.write_text(text, encoding="utf-8")
        proc_run.run(["git", "-C", str(work), "add", relative], check=True, capture_output=True)
        target.unlink()

    @staticmethod
    def _run(work: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["bash", f"scripts/{script}", *args],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    # -- the mandatory subject ------------------------------------------------------------------

    @_SHARED_SUBJECT_GROUP
    @pytest.mark.parametrize("script", ["check_no_account_id.sh", "check_no_vendor_keys.sh"])
    def test_a_bare_invocation_is_a_usage_error_not_a_silent_default(
        self, subject_selection_readonly_clone: Path, script: str
    ) -> None:
        """A bare invocation exits 2, names the three subjects, and reports no verdict."""
        result = self._run(subject_selection_readonly_clone, script)
        assert result.returncode == 2, (
            f"a bare invocation was accepted: {result.stdout}{result.stderr}"
        )
        for expected in ("--worktree", "--cached", "--ref"):
            assert expected in result.stderr, f"usage does not name {expected}:\n{result.stderr}"
        assert "OK" not in result.stdout

    @_SHARED_SUBJECT_GROUP
    @pytest.mark.parametrize("script", ["check_no_account_id.sh", "check_no_vendor_keys.sh"])
    @pytest.mark.parametrize(
        "bad", [["--bogus"], ["--cached", "extra"], ["--ref"], ["--worktree", "x"]]
    )
    def test_malformed_subject_arguments_are_refused(
        self, subject_selection_readonly_clone: Path, script: str, bad: list[str]
    ) -> None:
        result = self._run(subject_selection_readonly_clone, script, *bad)
        assert result.returncode == 2, f"{bad} was accepted: {result.stdout}{result.stderr}"

    # -- staged-only content ---------------------------------------------------------------------

    def test_cached_mode_catches_a_staged_literal_the_worktree_cannot_see(
        self, tmp_path: Path
    ) -> None:
        """A staged literal absent from disk is caught by `--cached` and not seen by `--worktree`."""
        work = self._clone(tmp_path)
        planted = _synthetic_vpc_id()
        self._stage_then_remove_from_worktree(work, "leak.tf", f'vpc_id = "{planted}"\n')

        worktree = self._run(work, "check_no_account_id.sh", "--worktree")
        cached = self._run(work, "check_no_account_id.sh", "--cached")

        assert worktree.returncode == 0, (
            "the worktree subject unexpectedly saw a file that is not on disk; this test no longer "
            f"reproduces the state it is about:\n{worktree.stderr}"
        )
        assert cached.returncode == 1, (
            f"--cached did not catch a literal staged for commit:\n{cached.stdout}{cached.stderr}"
        )
        assert planted in cached.stderr, f"the violation is not named:\n{cached.stderr}"

    def test_cached_mode_catches_a_staged_vendor_key_the_worktree_cannot_see(
        self, tmp_path: Path
    ) -> None:
        """`check_no_vendor_keys.sh --cached` catches a staged vendor key absent from disk."""
        work = self._clone(tmp_path)
        self._stage_then_remove_from_worktree(work, "leak.py", _fake_vendor_key_line())

        assert self._run(work, "check_no_vendor_keys.sh", "--worktree").returncode == 0
        cached = self._run(work, "check_no_vendor_keys.sh", "--cached")
        assert cached.returncode == 1, f"--cached missed a staged vendor key:\n{cached.stderr}"
        assert "sk-ant-" in cached.stderr

    def test_ref_head_cannot_see_the_staged_change(self, tmp_path: Path) -> None:
        """`--ref HEAD` reads the parent commit, so it does not see a staged change."""
        work = self._clone(tmp_path)
        self._stage_then_remove_from_worktree(
            work, "leak.tf", f'vpc_id = "{_synthetic_vpc_id()}"\n'
        )
        assert self._run(work, "check_no_account_id.sh", "--ref", "HEAD").returncode == 0, (
            "`--ref HEAD` saw a staged-but-uncommitted change, so this test no longer demonstrates "
            "why it is the wrong subject for a pre-commit hook"
        )

    # -- the hook end to end -------------------------------------------------------------------

    def test_the_real_hook_blocks_a_commit_carrying_a_staged_literal(self, tmp_path: Path) -> None:
        """A commit carrying a staged-only literal is blocked by the hook installed via
        `core.hooksPath`, and the literal does not reach HEAD.

        `GITLEAKS_BIN` points at a stub that always succeeds, isolating the account-id path.
        """
        work = self._clone(tmp_path)
        proc_run.run(["git", "-C", str(work), "config", "core.hooksPath", "githooks"], check=True)
        (work / "githooks" / "pre-commit").chmod(0o755)
        stub = work / "gitleaks-stub"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)

        planted = _synthetic_vpc_id()
        self._stage_then_remove_from_worktree(work, "leak.tf", f'vpc_id = "{planted}"\n')

        result = proc_run.run(
            ["git", "-C", str(work), "commit", "-m", "probe"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GITLEAKS_BIN": str(stub)},
        )
        combined = result.stdout + result.stderr

        assert result.returncode != 0, f"THE COMMIT WAS NOT BLOCKED:\n{combined}"
        assert planted in combined, f"the hook did not name the violation:\n{combined}"
        in_head = proc_run.run(
            ["git", "-C", str(work), "grep", "-q", planted, "HEAD"],
            capture_output=True,
            check=False,
        )
        assert in_head.returncode != 0, "the literal reached HEAD despite the hook running"

    def test_the_hook_still_permits_a_clean_commit(self, tmp_path: Path) -> None:
        """The installed hook permits a clean commit."""
        work = self._clone(tmp_path)
        proc_run.run(["git", "-C", str(work), "config", "core.hooksPath", "githooks"], check=True)
        (work / "githooks" / "pre-commit").chmod(0o755)
        stub = work / "gitleaks-stub"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)

        (work / "harmless.md").write_text("nothing sensitive here\n", encoding="utf-8")
        proc_run.run(["git", "-C", str(work), "add", "harmless.md"], check=True)

        result = proc_run.run(
            ["git", "-C", str(work), "commit", "-m", "clean probe"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GITLEAKS_BIN": str(stub)},
        )
        assert result.returncode == 0, (
            f"a clean commit was blocked, so the block above proves nothing:\n"
            f"{result.stdout}{result.stderr}"
        )


class TestTheSubjectIsNotDecidedByTheEnvironment:
    """Git environment variables cannot move the account-id gate's subject or make it report `OK`
    having verified nothing.

    Route A: `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR` and
    `GIT_OBJECT_DIRECTORY` substitute the subject; the gate unsets four and validates
    `GIT_INDEX_FILE`. Route B: a `git grep` fatal or non-empty stderr is refused rather than read as
    "no matches". Route C: config injection such as a `* binary` attributes file is caught by the
    positive control that reads back the gate's own source before printing OK.

    Each remedy is exercised against a scratch copy of the gate with that remedy reverted.
    """

    _REVERT_SCRUB = MUT_REVERT_SCRUB
    _REVERT_STATUS_CHECK = MUT_REVERT_STATUS_CHECK
    _REVERT_POSITIVE_CONTROL = MUT_REVERT_POSITIVE_CONTROL
    _REVERT_INDEX_VALIDATION = MUT_REVERT_INDEX_VALIDATION

    @staticmethod
    def _repo(tmp_path: Path, *, planted: str | None, gate_source: str | None = None) -> Path:
        """A two-file scratch repository holding the gate under test and, optionally, a planted
        violation."""
        work = tmp_path / "subject"
        (work / "scripts").mkdir(parents=True)
        source = (
            gate_source
            if gate_source is not None
            else CHECK_NO_ACCOUNT_ID.read_text(encoding="utf-8")
        )
        (work / "scripts" / "check_no_account_id.sh").write_text(source, encoding="utf-8")
        if planted is not None:
            (work / "tainted.txt").write_text(f'AccountId = "{planted}"\n', encoding="utf-8")
        proc_run.run(["git", "init", "--quiet", str(work)], check=True, capture_output=True)
        for key, value in (("user.email", "probe@local"), ("user.name", "probe")):
            proc_run.run(["git", "-C", str(work), "config", key, value], check=True)
        proc_run.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
        proc_run.run(
            ["git", "-C", str(work), "commit", "-qm", "probe"], check=True, capture_output=True
        )
        return work

    @staticmethod
    def _foreign(tmp_path: Path) -> Path:
        """A valid, clean git repository, used as the Route A substitute subject."""
        work = tmp_path / "foreign"
        work.mkdir(parents=True)
        (work / "clean.txt").write_text("nothing interesting here\n", encoding="utf-8")
        proc_run.run(["git", "init", "--quiet", str(work)], check=True, capture_output=True)
        for key, value in (("user.email", "probe@local"), ("user.name", "probe")):
            proc_run.run(["git", "-C", str(work), "config", key, value], check=True)
        proc_run.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
        proc_run.run(
            ["git", "-C", str(work), "commit", "-qm", "foreign"], check=True, capture_output=True
        )
        return work

    @classmethod
    def _mutate(cls, *mutations: tuple[str, str]) -> str:
        return _mutate_gate(CHECK_NO_ACCOUNT_ID, *mutations)

    @staticmethod
    def _run(
        work: Path, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["bash", "scripts/check_no_account_id.sh", *args],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
        )

    @staticmethod
    def _assert_never_reports_ok(result: subprocess.CompletedProcess[str], why: str) -> None:
        """Assert a non-zero status and no `: OK` on stdout."""
        assert result.returncode != 0, f"{why}\n{result.stdout}{result.stderr}"
        assert ": OK" not in result.stdout, (
            f"{why} — the gate exited non-zero but still printed OK:\n{result.stdout}"
        )

    # ---------------------------------------------------------------- the unmutated control

    def test_the_unmutated_gate_still_passes_a_clean_tree(self, tmp_path: Path) -> None:
        """The unmutated gate passes a clean tree in all three modes."""
        work = self._repo(tmp_path, planted=None)
        for args in (("--worktree",), ("--cached",), ("--ref", "HEAD")):
            result = self._run(work, *args)
            assert result.returncode == 0, (
                f"the unmutated gate rejects a clean tree in {args[0]} mode, so every "
                f"override test below is meaningless:\n{result.stdout}{result.stderr}"
            )
            assert ": OK" in result.stdout

    def test_the_unmutated_gate_still_reports_a_real_violation(self, tmp_path: Path) -> None:
        """The unmutated gate exits 1 on a planted account id in all three modes."""
        planted = _synthetic_account_id()
        work = self._repo(tmp_path, planted=planted)
        for args in (("--worktree",), ("--cached",), ("--ref", "HEAD")):
            result = self._run(work, *args)
            assert result.returncode == 1, (
                f"a planted account id was not reported as a violation in {args[0]} mode "
                f"(exit {result.returncode}):\n{result.stdout}{result.stderr}"
            )
            assert planted in result.stderr

    # ---------------------------------------------------------------- Route A

    @pytest.mark.parametrize("mode", [("--worktree",), ("--cached",), ("--ref", "HEAD")])
    def test_route_a_a_valid_foreign_repository_no_longer_answers(
        self, tmp_path: Path, mode: tuple[str, ...]
    ) -> None:
        """`GIT_DIR`/`GIT_WORK_TREE` pointing at a clean foreign repository do not yield OK in any
        mode."""
        planted = _synthetic_account_id()
        work = self._repo(tmp_path, planted=planted)
        foreign = self._foreign(tmp_path)
        env = {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign)}

        result = self._run(work, *mode, env=env)
        self._assert_never_reports_ok(
            result,
            f"a planted account id went unreported in {mode[0]} mode because GIT_DIR/GIT_WORK_TREE "
            "substituted a clean foreign repository as the subject",
        )

    @staticmethod
    def _sibling_clone(tmp_path: Path, subject: Path) -> Path:
        """A clone of the subject with the violation removed; it carries the gate's own source, so
        the positive control passes against it."""
        work = tmp_path / "sibling"
        proc_run.run(
            ["git", "clone", "--quiet", str(subject), str(work)], check=True, capture_output=True
        )
        for key, value in (("user.email", "probe@local"), ("user.name", "probe")):
            proc_run.run(["git", "-C", str(work), "config", key, value], check=True)
        proc_run.run(
            ["git", "-C", str(work), "rm", "-q", "tainted.txt"], check=True, capture_output=True
        )
        proc_run.run(
            ["git", "-C", str(work), "commit", "-qm", "clean sibling"],
            check=True,
            capture_output=True,
        )
        return work

    @classmethod
    def _assert_positive_control_passes_in(cls, repo: Path, *args: str) -> None:
        """Assert the gate's positive control would be satisfied in `repo`."""
        control = proc_run.run(
            [
                "git",
                "grep",
                *args,
                "-I",
                "-l",
                "-F",
                "-e",
                "check_no_account_id",
                "--",
                "scripts/check_no_account_id.sh",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert control.returncode == 0 and control.stdout.strip(), (
            "this cell must be one the positive control CANNOT catch, or it does not discriminate on "
            f"the environment scrub:\n{control.stdout}{control.stderr}"
        )

    def test_route_a_a_sibling_checkout_of_the_same_project_no_longer_answers(
        self, tmp_path: Path
    ) -> None:
        """A sibling checkout substituted via `GIT_DIR`/`GIT_WORK_TREE` does not yield OK, and with
        the scrub reverted it does."""
        planted = _synthetic_account_id()
        work = self._repo(tmp_path, planted=planted)
        sibling = self._sibling_clone(tmp_path, work)
        env = {"GIT_DIR": str(sibling / ".git"), "GIT_WORK_TREE": str(sibling)}
        self._assert_positive_control_passes_in(sibling)

        mutated = self._repo(
            tmp_path / "mutated", planted=planted, gate_source=self._mutate(self._REVERT_SCRUB)
        )
        mutated_sibling = self._sibling_clone(tmp_path / "mutated", mutated)
        before = self._run(
            mutated,
            "--worktree",
            env={"GIT_DIR": str(mutated_sibling / ".git"), "GIT_WORK_TREE": str(mutated_sibling)},
        )
        assert before.returncode == 0 and ": OK" in before.stdout, (
            "with the scrub removed the gate was expected to report OK over a planted account id, "
            "having read a sibling checkout instead. It did not, so this test no longer discriminates "
            f"on the scrub:\n{before.stdout}{before.stderr}"
        )

        result = self._run(work, "--worktree", env=env)
        self._assert_never_reports_ok(
            result,
            "a planted account id went unreported because GIT_DIR/GIT_WORK_TREE substituted a clean "
            "sibling checkout of the same project as the subject",
        )

    def test_route_a_the_index_alone_moves_the_answer(self, tmp_path: Path) -> None:
        """A foreign `GIT_INDEX_FILE` alone, which leaves the toplevel unchanged, does not yield OK
        unless both the scrub and the index validation are reverted."""
        planted = _synthetic_account_id()
        # Both remedies are reverted: the index validation alone would refuse a sibling index.
        mutated = self._repo(
            tmp_path / "mutated",
            planted=planted,
            gate_source=self._mutate(self._REVERT_SCRUB, self._REVERT_INDEX_VALIDATION),
        )
        sibling = self._sibling_clone(tmp_path / "mutated", mutated)
        index_env = {"GIT_INDEX_FILE": str(sibling / ".git" / "index")}

        toplevel = proc_run.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=mutated,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **index_env},
        ).stdout.strip()
        assert Path(toplevel).resolve() == mutated.resolve(), (
            "GIT_INDEX_FILE was expected to leave --show-toplevel pointing at the real repository; "
            "if it now moves, a toplevel cross-check would suffice and this remedy needs revisiting"
        )

        before = self._run(mutated, "--cached", env=index_env)
        assert before.returncode == 0 and ": OK" in before.stdout, (
            "GIT_INDEX_FILE alone was expected to hide the planted id from the unscrubbed gate:\n"
            f"{before.stdout}{before.stderr}"
        )

        work = self._repo(tmp_path / "fixed", planted=planted)
        fixed_sibling = self._sibling_clone(tmp_path / "fixed", work)
        result = self._run(
            work, "--cached", env={"GIT_INDEX_FILE": str(fixed_sibling / ".git" / "index")}
        )
        self._assert_never_reports_ok(
            result, "GIT_INDEX_FILE alone still hides a planted id from the fixed gate"
        )

    # ---------------------------------------------------------------- Route B

    def test_route_b_an_unmappable_index_no_longer_prints_ok(self, tmp_path: Path) -> None:
        """`GIT_INDEX_FILE` at a non-index path, where `git grep --cached` exits 128, does not yield
        OK."""
        work = self._repo(tmp_path, planted=_synthetic_account_id())
        not_an_index = tmp_path / "definitely-a-directory"
        not_an_index.mkdir()

        result = self._run(work, "--cached", env={"GIT_INDEX_FILE": str(not_an_index)})
        self._assert_never_reports_ok(
            result, "an unmappable index was swallowed and reported as a clean scan"
        )

    def test_route_b_a_diagnostic_arriving_with_a_success_status_is_refused(
        self, tmp_path: Path
    ) -> None:
        """A missing blob, where `git grep` prints `unable to read` and exits 1, does not yield OK
        and the diagnostic is surfaced."""
        planted = _synthetic_account_id()
        work = self._repo(tmp_path, planted=planted)
        blob = proc_run.run(
            ["git", "-C", str(work), "rev-parse", ":tainted.txt"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        loose = work / ".git" / "objects" / blob[:2] / blob[2:]
        assert loose.is_file(), f"expected a loose object at {loose}; the fixture needs rebuilding"
        loose.chmod(0o644)
        loose.unlink()

        # Precondition: git reports the diagnostic while exiting 0 or 1.
        bare = proc_run.run(
            ["git", "grep", "--cached", "-I", "-n", "-E", "-e", "AccountId", "--", "."],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )
        assert bare.returncode in (0, 1) and "unable to read" in bare.stderr, (
            "this cell needs git to report a diagnostic while exiting 0 or 1; it did not, so it no "
            f"longer exercises the stderr half of the guard (rc={bare.returncode}):\n{bare.stderr}"
        )

        result = self._run(work, "--cached")
        self._assert_never_reports_ok(
            result, "an unreadable blob made the scan blind and the gate called it clean"
        )
        assert "unable to read" in result.stderr, (
            f"the gate did not surface git's own diagnostic:\n{result.stderr}"
        )

    def test_route_b_reds_only_because_of_the_status_check(self, tmp_path: Path) -> None:
        """With the status/stderr guard reverted, the blind scan reports OK."""
        planted = _synthetic_account_id()
        work = self._repo(
            tmp_path, planted=planted, gate_source=self._mutate(self._REVERT_STATUS_CHECK)
        )
        blob = proc_run.run(
            ["git", "-C", str(work), "rev-parse", ":tainted.txt"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        loose = work / ".git" / "objects" / blob[:2] / blob[2:]
        loose.chmod(0o644)
        loose.unlink()

        result = self._run(work, "--cached")
        assert result.returncode == 0 and ": OK" in result.stdout, (
            "with the status/stderr guard neutralised the gate was expected to report OK over an "
            "unreadable planted account id. It did not, so this test no longer discriminates on that "
            f"guard:\n{result.stdout}{result.stderr}"
        )

    # ---------------------------------------------------------------- Route C

    def test_a_git_attribute_marking_tracked_text_as_binary_is_refused(
        self, tmp_path: Path
    ) -> None:
        """`core.attributesFile` injected via `GIT_CONFIG_*` marking every file binary does not yield
        OK."""
        work = self._repo(tmp_path, planted=_synthetic_account_id())
        attrs = tmp_path / "attrs"
        attrs.write_text("* binary\n", encoding="utf-8")
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.attributesFile",
            "GIT_CONFIG_VALUE_0": str(attrs),
        }

        result = self._run(work, "--worktree", env=env)
        self._assert_never_reports_ok(
            result, "every tracked file was treated as binary and the gate called the tree clean"
        )

    def test_the_binary_attribute_route_reds_only_because_of_the_positive_control(
        self, tmp_path: Path
    ) -> None:
        """With the positive control reverted, the binary-attribute route reports OK."""
        work = self._repo(
            tmp_path,
            planted=_synthetic_account_id(),
            gate_source=self._mutate(self._REVERT_POSITIVE_CONTROL),
        )
        attrs = tmp_path / "attrs"
        attrs.write_text("* binary\n", encoding="utf-8")

        result = self._run(
            work,
            "--worktree",
            env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.attributesFile",
                "GIT_CONFIG_VALUE_0": str(attrs),
            },
        )
        assert result.returncode == 0 and ": OK" in result.stdout, (
            "with the positive control removed the gate was expected to report OK over a planted "
            f"account id it could not read:\n{result.stdout}{result.stderr}"
        )

    # ---------------------------------------------------------------- the sibling gates

    def test_every_gate_carrying_the_idiom_scrubs_the_same_names(self) -> None:
        """Each of the four gate scripts unsets the same four git environment names."""
        for script in (
            "check_no_account_id.sh",
            "check_no_vendor_keys.sh",
            "check_license_headers.sh",
            "verify_fresh_clone.sh",
        ):
            source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
            assert SCRUBBED_GIT_NAMES in source, (
                f"scripts/{script} does not scrub the four measured illegitimate names"
            )

    def test_no_gate_still_swallows_git_grep_with_or_true(self) -> None:
        """No `git grep` line in the two content gates carries `|| true`."""
        for script in ("check_no_account_id.sh", "check_no_vendor_keys.sh"):
            source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
            offenders = [
                line.strip()
                for line in source.splitlines()
                if "git grep" in line and "|| true" in line
            ]
            assert not offenders, (
                f"scripts/{script} still swallows a git grep status:\n" + "\n".join(offenders)
            )

    def test_the_verified_nothing_status_is_not_the_skip_status(self) -> None:
        """The "verified nothing" status is 4, distinct from the `run_gates.py` skip status."""
        from scripts.run_gates import GATE_SKIP_EXIT_STATUS  # noqa: PLC0415

        for script in (
            "check_no_account_id.sh",
            "check_no_vendor_keys.sh",
            "check_license_headers.sh",
        ):
            source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
            assert "EXIT_UNREADABLE=4" in source, f"scripts/{script} lost its unreadable status"
            assert f"EXIT_UNREADABLE={GATE_SKIP_EXIT_STATUS}" not in source, (
                f"scripts/{script} uses the SKIP status for 'verified nothing', which run_gates.py "
                "does not fail the board on"
            )


class TestTheVendorKeyGateLocaleIsPinnedToo:
    """`check_no_vendor_keys.sh` pins `LC_ALL=C`, so a key on a line with an invalid UTF-8 byte is
    still reported under a UTF-8 locale.

    The gate has no redaction pass, so its only non-detection outcome is exit 0 printing `OK`.
    """

    _UTF8_LOCALE = "C.UTF-8"
    _MUT_DROP_PIN = ("\nexport LC_ALL=C\n", "\n")

    @staticmethod
    def _repo(work: Path, planted: str) -> Path:
        """A scratch repo whose only violating line carries an ill-formed UTF-8 sequence."""
        work.mkdir(parents=True)
        (work / "tainted.md").write_bytes(
            b"ordinary line\nbad \xe9\xe8 seq then " + planted.encode("ascii") + b" here\n"
        )
        _init_scratch_repo(work)
        _stage_all(work)
        return work

    @classmethod
    def _run(cls, work: Path, gate: Path) -> subprocess.CompletedProcess[str]:
        # `errors="replace"`: stderr echoing an invalid-byte line is not necessarily valid UTF-8.
        return proc_run.run(
            ["bash", str(gate), "--worktree"],
            cwd=work,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": cls._UTF8_LOCALE, "LANG": cls._UTF8_LOCALE},
        )

    def test_a_vendor_key_on_an_invalid_utf8_line_is_reported(self, tmp_path: Path) -> None:
        planted = "sk-" + "ant-" + "A1B2c3D4e5F6g7H8i9J0k1L2"

        caught = self._run(self._repo(tmp_path / "subject", planted), CHECK_NO_VENDOR_KEYS)
        assert caught.returncode == 1, (
            "the pinned vendor-key gate must REPORT the key (exit 1). It exited "
            f"{caught.returncode}.\n{caught.stdout}{caught.stderr}"
        )
        assert "tainted.md:2:" in caught.stderr, (
            f"the report must name the offending line:\n{caught.stderr}"
        )

        # Control: with the pin stripped, the same fixture is missed under the same locale.
        unpinned = tmp_path / "unpinned.sh"
        unpinned.write_text(
            _mutate_gate(CHECK_NO_VENDOR_KEYS, self._MUT_DROP_PIN), encoding="utf-8"
        )
        missed = self._run(self._repo(tmp_path / "control", planted), unpinned)
        assert missed.returncode == 0 and ": OK" in missed.stdout, (
            f"the pin-removed gate was expected to MISS this key under LC_ALL={self._UTF8_LOCALE}, so "
            f"the assertions above cannot be attributed to the pin. It exited {missed.returncode}. "
            "Re-derive the fixture from measurement; do not relax this into a skip.\n"
            f"{missed.stdout}{missed.stderr}"
        )

    def test_an_all_ascii_key_is_reported_in_every_locale(self, tmp_path: Path) -> None:
        """An all-ASCII vendor key is reported under both `C.UTF-8` and `C`."""
        planted = "sk-" + "ant-" + "Z9Y8x7W6v5U4t3S2r1Q0p9O8"
        work = tmp_path / "ascii"
        work.mkdir(parents=True)
        (work / "plain.md").write_text(f"key {planted} here\n", encoding="utf-8")
        _init_scratch_repo(work)
        _stage_all(work)
        for locale in (self._UTF8_LOCALE, "C"):
            result = proc_run.run(
                ["bash", str(CHECK_NO_VENDOR_KEYS), "--worktree"],
                cwd=work,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "LC_ALL": locale, "LANG": locale},
            )
            assert result.returncode == 1, (
                f"an all-ASCII vendor key must be reported under LC_ALL={locale}; it exited "
                f"{result.returncode}.\n{result.stdout}{result.stderr}"
            )

    def test_the_vendor_key_gate_pins_the_locale_to_c(self) -> None:
        """The vendor-key gate exports `LC_ALL=C` exactly once, and not `C.UTF-8`."""
        source = CHECK_NO_VENDOR_KEYS.read_text(encoding="utf-8")
        assert source.count("\nexport LC_ALL=C\n") == 1, (
            "scripts/check_no_vendor_keys.sh must export LC_ALL=C exactly once, at top level."
        )
        assert "export LC_ALL=C.UTF-8" not in source, (
            "C.UTF-8 is a UTF-8 charset and does NOT close the blindness. The workflows pin it job-wide "
            "for shellcheck's output encoding, which is a different subject."
        )


class TestTheLocaleDoesNotDecideWhatTheGateCanSee:
    """`check_no_account_id.sh` pins `LC_ALL=C`, so a violation on a line carrying an invalid UTF-8
    byte is reported under a UTF-8 locale.

    Without the pin there are two non-detection outcomes: class A, exit 0 printing `OK`; class B,
    exit 4 when `sed` dies in the redaction pass and `select_survivors` refuses. Which shape lands in
    which class depends on the host's `grep`/`sed`, so each cell derives the unpinned outcome per run
    rather than hardcoding it. The child is forced into `LC_ALL=C.UTF-8`.
    """

    #: A UTF-8 locale available on macOS and in the CI image, and the one the workflows pin job-wide.
    _UTF8_LOCALE = "C.UTF-8"

    #: Mutation removing the locale pin.
    _MUT_DROP_PIN = ("\nexport LC_ALL=C\n", "\n")

    #: The violating line for each outcome class, as bytes carrying an ill-formed UTF-8 sequence.
    #: `%s` is the planted id.
    _LINE_CLASS_A = b"bad \xe9\xe8 seq then %s here\n"
    _LINE_CLASS_B = b"%s then bad \xff byte\n"

    @staticmethod
    def _repo(work: Path, line: bytes, planted: str, *, gate_text: str | None = None) -> Path:
        """A scratch repo holding one tracked text file whose violating line carries an invalid byte."""
        work.mkdir(parents=True)
        (work / "scripts").mkdir()
        (work / "scripts" / "check_no_account_id.sh").write_text(
            gate_text if gate_text is not None else CHECK_NO_ACCOUNT_ID.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (work / "tainted.md").write_bytes(
            b"ordinary line\n" + line.replace(b"%s", planted.encode("ascii"))
        )
        _init_scratch_repo(work)
        _stage_all(work)
        return work

    @classmethod
    def _run(cls, work: Path, gate: Path) -> subprocess.CompletedProcess[str]:
        """Run `gate` under the UTF-8 locale; `errors="replace"` because its stderr echoes the
        invalid-byte line verbatim."""
        return proc_run.run(
            ["bash", str(gate), "--worktree"],
            cwd=work,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": cls._UTF8_LOCALE, "LANG": cls._UTF8_LOCALE},
        )

    #: The outcome classes the pin-removed gate is known to land in, derived per run.
    _UNPINNED_REPORTS = "REPORTS (not blind here; the pin is redundant on this host, not wrong)"
    _UNPINNED_MISSES_SILENTLY = "MISSES SILENTLY (exit 0 and OK -- blind, and the stealthier way)"
    _UNPINNED_REFUSES = "REFUSES (exit 4 -- blind, but loud about declining to vouch)"

    @classmethod
    def _classify_unpinned(cls, run: subprocess.CompletedProcess[str], planted: str) -> str:
        """Name what the pin-removed gate did from its exit status and whether it named the planted
        id; raise on any combination outside the three known classes."""
        named = planted in run.stderr
        if run.returncode == 1 and named:
            return cls._UNPINNED_REPORTS
        if run.returncode == 0 and not named and ": OK" in run.stdout:
            return cls._UNPINNED_MISSES_SILENTLY
        if run.returncode == 4 and not named:
            return cls._UNPINNED_REFUSES
        raise AssertionError(
            "the pin-removed gate produced a behaviour none of the three known outcome classes "
            f"describes: exit {run.returncode}, planted id {'named' if named else 'absent'}. That is "
            "a real change in this gate's failure modes, not a fixture problem -- classify it by "
            f"measurement before extending the list.\nstdout: {run.stdout}\nstderr: {run.stderr}"
        )

    def _assert_the_pinned_gate_reports(self, tmp_path: Path, line: bytes) -> str:
        """Assert the pinned gate reports `line` (exit 1, naming the id and the line), then run the
        pin-removed gate and return its classified outcome."""
        planted = _synthetic_security_group_id()

        caught = self._run(self._repo(tmp_path / "subject", line, planted), CHECK_NO_ACCOUNT_ID)
        assert caught.returncode == 1, (
            "the pinned gate must REPORT the violation (exit 1), not merely refuse to vouch for the "
            f"scan (exit 4) and not pass it (exit 0). It exited {caught.returncode}.\n"
            f"{caught.stdout}{caught.stderr}"
        )
        assert planted in caught.stderr, (
            "the pinned gate exited 1 but did not name the planted id, so it failed for some other "
            f"reason and this cell proves nothing about the invalid-byte line:\n{caught.stderr}"
        )
        assert "tainted.md:2:" in caught.stderr, (
            f"the report must name the offending line, not just the file:\n{caught.stderr}"
        )

        # Control: the pin-removed gate, whose outcome is classified rather than demanded.
        unpinned_gate = tmp_path / "unpinned.sh"
        unpinned_gate.write_text(
            _mutate_gate(CHECK_NO_ACCOUNT_ID, self._MUT_DROP_PIN), encoding="utf-8"
        )
        unpinned = self._run(self._repo(tmp_path / "control", line, planted), unpinned_gate)
        return self._classify_unpinned(unpinned, planted)

    def test_class_a_shape_is_reported_with_the_pin(self, tmp_path: Path) -> None:
        """The pinned gate reports the class A shape."""
        self._assert_the_pinned_gate_reports(tmp_path, self._LINE_CLASS_A)

    def test_class_b_shape_is_reported_with_the_pin(self, tmp_path: Path) -> None:
        """The pinned gate reports the class B shape."""
        self._assert_the_pinned_gate_reports(tmp_path, self._LINE_CLASS_B)

    def test_the_gate_pins_the_locale_to_c_and_not_to_a_utf8_charset(self) -> None:
        """The account-id gate exports `LC_ALL=C` exactly once, and not `C.UTF-8`."""
        source = CHECK_NO_ACCOUNT_ID.read_text(encoding="utf-8")
        assert source.count("\nexport LC_ALL=C\n") == 1, (
            "scripts/check_no_account_id.sh must export LC_ALL=C exactly once, at top level. Without "
            "it the gate is blind to a violation on any line carrying an invalid UTF-8 byte."
        )
        assert "export LC_ALL=C.UTF-8" not in source, (
            "LC_ALL=C.UTF-8 is a UTF-8 charset and does NOT close the blindness -- measured, it "
            "behaves exactly like en_US.UTF-8. The workflows pin C.UTF-8 for shellcheck's output "
            "encoding, which is a different subject; do not copy it here."
        )

    def test_the_workflow_level_utf8_pins_are_left_alone(self) -> None:
        """Both CI workflows keep their job-wide `LC_ALL: "C.UTF-8"` pin."""
        for workflow in (".gitlab-ci.yml", ".github/workflows/verify.yml"):
            text = (REPO_ROOT / workflow).read_text(encoding="utf-8")
            assert 'LC_ALL: "C.UTF-8"' in text, (
                f"{workflow} lost its job-wide C.UTF-8 pin, which shellcheck needs for its output "
                "encoding. The gate's own LC_ALL=C is process-scoped and does not replace it."
            )


class TestCheckLicenseHeadersSubjectSubstitution:
    """`check_license_headers.sh` refuses a substituted or unusable index, including in `--worktree`
    mode, where `list_tracked` also enumerates the index."""

    @staticmethod
    def _repo(tmp_path: Path, *, headerless: bool) -> Path:
        work = tmp_path / "subject"
        (work / "scripts").mkdir(parents=True)
        (work / "src").mkdir(parents=True)
        gate = REPO_ROOT / "scripts" / "check_license_headers.sh"
        (work / "scripts" / "check_license_headers.sh").write_text(
            gate.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (work / "src" / "ok.py").write_text(
            "# SPDX-License-Identifier: MIT\nprint(1)\n", encoding="utf-8"
        )
        if headerless:
            (work / "src" / "bad.py").write_text('print("no header")\n', encoding="utf-8")
        proc_run.run(["git", "init", "--quiet", str(work)], check=True, capture_output=True)
        for key, value in (("user.email", "probe@local"), ("user.name", "probe")):
            proc_run.run(["git", "-C", str(work), "config", key, value], check=True)
        proc_run.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
        proc_run.run(
            ["git", "-C", str(work), "commit", "-qm", "probe"], check=True, capture_output=True
        )
        return work

    @staticmethod
    def _run(
        work: Path, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["bash", "scripts/check_license_headers.sh", *args],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
        )

    def test_the_unmutated_gate_still_passes_and_still_fails(self, tmp_path: Path) -> None:
        """The unmutated gate passes a compliant tree and fails a headerless file."""
        clean = self._run(self._repo(tmp_path / "clean", headerless=False), "--worktree")
        assert clean.returncode == 0, f"a compliant tree was rejected:\n{clean.stderr}"
        dirty = self._run(self._repo(tmp_path / "dirty", headerless=True), "--worktree")
        assert dirty.returncode == 1, f"a headerless file was not reported:\n{dirty.stderr}"

    def test_a_foreign_index_no_longer_answers_in_worktree_mode(self, tmp_path: Path) -> None:
        """A foreign `GIT_INDEX_FILE` in `--worktree` mode does not yield OK."""
        work = self._repo(tmp_path, headerless=True)
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        (foreign / "clean.txt").write_text("nothing\n", encoding="utf-8")
        proc_run.run(["git", "init", "--quiet", str(foreign)], check=True, capture_output=True)
        for key, value in (("user.email", "probe@local"), ("user.name", "probe")):
            proc_run.run(["git", "-C", str(foreign), "config", key, value], check=True)
        proc_run.run(["git", "-C", str(foreign), "add", "-A"], check=True, capture_output=True)
        proc_run.run(
            ["git", "-C", str(foreign), "commit", "-qm", "f"], check=True, capture_output=True
        )

        result = self._run(
            work, "--worktree", env={"GIT_INDEX_FILE": str(foreign / ".git" / "index")}
        )
        assert result.returncode != 0, (
            "a headerless file went unreported because GIT_INDEX_FILE alone substituted another "
            f"repository's index as the enumeration:\n{result.stdout}{result.stderr}"
        )
        assert ": OK" not in result.stdout

    def test_an_empty_enumeration_is_refused_rather_than_passed(self, tmp_path: Path) -> None:
        """An empty enumeration is refused rather than reported as a pass."""
        work = self._repo(tmp_path, headerless=True)
        not_an_index = tmp_path / "definitely-a-directory"
        not_an_index.mkdir()

        result = self._run(work, "--cached", env={"GIT_INDEX_FILE": str(not_an_index)})
        assert result.returncode != 0 and ": OK" not in result.stdout, (
            f"an unusable index was reported as a clean scan:\n{result.stdout}{result.stderr}"
        )


class TestGitIndexFileIsValidatedNotRemoved:
    """The gates keep `GIT_INDEX_FILE` when it resolves inside the repository's own git directory and
    refuse it otherwise.

    For `git commit -- <path>` git exports a temporary index holding the only copy of the content
    being committed. Every test drives a real `git commit` through an installed hook.
    """

    @staticmethod
    def _repo(tmp_path: Path, *, gate_source: str | None = None) -> Path:
        """A scratch repository whose pre-commit hook runs only this gate."""
        work = tmp_path / "subject"
        (work / "scripts").mkdir(parents=True)
        (work / "githooks").mkdir(parents=True)
        source = (
            gate_source
            if gate_source is not None
            else CHECK_NO_ACCOUNT_ID.read_text(encoding="utf-8")
        )
        (work / "scripts" / "check_no_account_id.sh").write_text(source, encoding="utf-8")
        hook = work / "githooks" / "pre-commit"
        hook.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'cd "$(git rev-parse --show-toplevel)"\n'
            "bash scripts/check_no_account_id.sh --cached\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        proc_run.run(["git", "init", "--quiet", str(work)], check=True, capture_output=True)
        for key, value in (
            ("user.email", "probe@local"),
            ("user.name", "probe"),
            ("core.hooksPath", "githooks"),
        ):
            proc_run.run(["git", "-C", str(work), "config", key, value], check=True)
        (work / "tracked.txt").write_text("clean line\n", encoding="utf-8")
        proc_run.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
        proc_run.run(
            ["git", "-C", str(work), "commit", "-qm", "base"], check=True, capture_output=True
        )
        return work

    _BLANKET_UNSET_INDEX = MUT_BLANKET_UNSET_INDEX
    _REVERT_INDEX_VALIDATION = MUT_REVERT_INDEX_VALIDATION

    @classmethod
    def _mutate(cls, mutation: tuple[str, str]) -> str:
        return _mutate_gate(CHECK_NO_ACCOUNT_ID, mutation)

    @staticmethod
    def _partial_commit(work: Path, path: str, content: str) -> subprocess.CompletedProcess[str]:
        """Write `content` to `path` on disk only, then `git commit -- <path>`, so git exports a
        temporary index as `GIT_INDEX_FILE` for the hook."""
        (work / path).write_text(content, encoding="utf-8")
        return proc_run.run(
            ["git", "-C", str(work), "commit", "-m", "partial", "--", path],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    @staticmethod
    def _reached_head(work: Path, path: str) -> bool:
        return (
            proc_run.run(
                ["git", "-C", str(work), "grep", "-q", "-E", "[0-9]{12}", "HEAD", "--", path],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

    def test_a_partial_commit_of_a_planted_id_is_blocked(self, tmp_path: Path) -> None:
        """A partial commit of a planted account id is blocked and the id does not reach HEAD."""
        work = self._repo(tmp_path)
        planted = _synthetic_account_id()

        result = self._partial_commit(work, "tracked.txt", f'AccountId = "{planted}"\n')

        assert result.returncode != 0, (
            f"a partial commit carrying a planted account id was allowed:\n"
            f"{result.stdout}{result.stderr}"
        )
        assert not self._reached_head(work, "tracked.txt"), (
            "the planted account id reached HEAD — the gate read .git/index instead of the temporary "
            "index holding the content actually being committed"
        )

    def test_the_block_is_load_bearing_on_validating_rather_than_unsetting(
        self, tmp_path: Path
    ) -> None:
        """With `GIT_INDEX_FILE` unset instead of validated, the planted id reaches HEAD."""
        work = self._repo(tmp_path, gate_source=self._mutate(self._BLANKET_UNSET_INDEX))
        planted = _synthetic_account_id()

        result = self._partial_commit(work, "tracked.txt", f'AccountId = "{planted}"\n')

        assert result.returncode == 0, (
            "with GIT_INDEX_FILE unset alongside the other four the commit was expected to be "
            f"ALLOWED — the shipped defect. It was not:\n{result.stdout}{result.stderr}"
        )
        assert self._reached_head(work, "tracked.txt"), (
            "the blanket-unset mutation was expected to let the planted id reach HEAD; it did not, so "
            "this control no longer discriminates on validate-vs-unset"
        )

    def test_a_clean_partial_commit_still_succeeds(self, tmp_path: Path) -> None:
        """A clean partial commit still succeeds."""
        work = self._repo(tmp_path)

        result = self._partial_commit(work, "tracked.txt", "still clean\n")

        assert result.returncode == 0, (
            f"a clean partial commit was blocked, so the refusal above proves nothing:\n"
            f"{result.stdout}{result.stderr}"
        )

    @staticmethod
    def _index_outside_the_git_dir(work: Path, tmp_path: Path) -> Path:
        """An index file outside this repository's git directory holding `HEAD`'s clean tree, built
        from this repository's own object store so its blobs are readable."""
        outside = tmp_path / "outside"
        outside.mkdir()
        index = outside / "index"
        proc_run.run(
            ["git", "read-tree", "HEAD"],
            cwd=work,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_INDEX_FILE": str(index)},
        )
        return index

    @staticmethod
    def _stage_planted(work: Path, planted: str) -> None:
        (work / "tracked.txt").write_text(f'AccountId = "{planted}"\n', encoding="utf-8")
        proc_run.run(
            ["git", "-C", str(work), "add", "tracked.txt"], check=True, capture_output=True
        )

    def test_an_index_outside_the_git_dir_is_refused(self, tmp_path: Path) -> None:
        """An index outside the git directory is refused with a status that is neither 0 nor the
        skip status."""
        from scripts.run_gates import GATE_SKIP_EXIT_STATUS  # noqa: PLC0415

        work = self._repo(tmp_path)
        self._stage_planted(work, _synthetic_account_id())
        outside = self._index_outside_the_git_dir(work, tmp_path)

        result = proc_run.run(
            ["bash", "scripts/check_no_account_id.sh", "--cached"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_INDEX_FILE": str(outside)},
        )
        assert result.returncode not in (0, GATE_SKIP_EXIT_STATUS), (
            "an index outside this repository's git directory was accepted, or reported with the SKIP "
            f"status that leaves the board green (exit {result.returncode}):"
            f"\n{result.stdout}{result.stderr}"
        )
        assert ": OK" not in result.stdout

    def test_the_outside_index_refusal_is_load_bearing(self, tmp_path: Path) -> None:
        """With the index validation reverted, the outside index is scanned cleanly and reports OK."""
        work = self._repo(tmp_path, gate_source=self._mutate(self._REVERT_INDEX_VALIDATION))
        self._stage_planted(work, _synthetic_account_id())
        outside = self._index_outside_the_git_dir(work, tmp_path)

        bare = proc_run.run(
            ["git", "grep", "--cached", "-I", "-n", "-E", "-e", "AccountId", "--", "."],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GIT_INDEX_FILE": str(outside)},
        )
        assert "unable to read" not in bare.stderr, (
            "this fixture must have READABLE blobs, or the refusal below comes from the stderr guard "
            f"rather than from the index validation:\n{bare.stderr}"
        )

        result = proc_run.run(
            ["bash", "scripts/check_no_account_id.sh", "--cached"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_INDEX_FILE": str(outside)},
        )
        assert result.returncode == 0 and ": OK" in result.stdout, (
            "with the index validation neutralised the gate was expected to cleanly scan the wrong "
            f"index and report OK over a staged planted id:\n{result.stdout}{result.stderr}"
        )

    def test_a_symlinked_repository_path_does_not_refuse_every_commit(self, tmp_path: Path) -> None:
        """A clean commit through a symlinked repository path succeeds, since the git-dir comparison
        uses physical paths."""
        work = self._repo(tmp_path)
        link = tmp_path / "via-symlink"
        link.symlink_to(work, target_is_directory=True)

        (link / "tracked.txt").write_text("clean via symlink\n", encoding="utf-8")
        result = proc_run.run(
            ["git", "commit", "-m", "partial", "--", "tracked.txt"],
            cwd=link,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode == 0, (
            "a clean commit through a symlinked repository path was refused — the git-dir comparison "
            f"is almost certainly comparing logical rather than physical paths:\n"
            f"{result.stdout}{result.stderr}"
        )

    def test_every_content_gate_validates_rather_than_unsets_the_index(self) -> None:
        """All three content gates validate `GIT_INDEX_FILE` rather than unsetting it.

        `scripts/verify_fresh_clone.sh` is not in the set; its `--ref -` reads this repository's
        own index.
        """
        for script in (
            "check_no_account_id.sh",
            "check_no_vendor_keys.sh",
            "check_license_headers.sh",
        ):
            source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
            assert "unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY" in source, (
                f"scripts/{script} does not scrub the four illegitimate names"
            )
            assert "GIT_INDEX_FILE GIT_COMMON_DIR" not in source, (
                f"scripts/{script} unsets GIT_INDEX_FILE again. That was measured to let a planted "
                "secret reach HEAD through a partial commit — validate it instead"
            )
            assert '_own_git_dir="$(cd -P' in source, (
                f"scripts/{script} lost the physical-path git-dir comparison that validates "
                "GIT_INDEX_FILE"
            )


class TestThePreCommitHookSubjectIsNotDecidedByTheEnvironment:
    """`githooks/pre-commit` scrubs git's discovery environment and validates `GIT_INDEX_FILE`, so the
    environment cannot move the subject of its staged gitleaks scan.

    Every probe is staged and then deleted from disk, so only the staged scan can see it. Exit status
    4 (`EXIT_UNREADABLE`) means the hook refused before scanning; 1 means a gate reported a finding.
    """

    #: Mutation reverting the `check-attr` control (see :meth:`test_a_textconv_diff_driver_is_refused`).
    _REVERT_ATTR_CONTROL = ("  unspecified | set) ;;", "  *) ;; # mutation: attr control removed")

    #: The hook's own "could not establish the subject" status.
    _EXIT_UNREADABLE = 4

    @staticmethod
    def _gitleaks_available() -> bool:
        return proc_run.run(["which", "gitleaks"], capture_output=True).returncode == 0

    @staticmethod
    def _repo(root: Path, *, hook_text: str | None = None) -> Path:
        """A scratch repository with the real hook, both shell gates and any local gitleaks config,
        installed via `core.hooksPath`."""
        root.mkdir(parents=True, exist_ok=True)
        _init_scratch_repo(root)
        hooks = root / "githooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text(
            hook_text if hook_text is not None else PRE_COMMIT_HOOK.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        hook.chmod(0o755)
        scripts = root / "scripts"
        scripts.mkdir()
        for gate in (CHECK_NO_ACCOUNT_ID, CHECK_NO_VENDOR_KEYS, CHECK_SECRET_SCAN, PROC_RUN):
            dest = scripts / gate.name
            dest.write_text(gate.read_text(encoding="utf-8"), encoding="utf-8")
            dest.chmod(0o755)
        install_local_rule_set(root)
        (root / "tracked.txt").write_text("clean line\n", encoding="utf-8")
        proc_run.run(["git", "config", "core.hooksPath", "githooks"], cwd=root, check=True)
        _stage_all(root)
        proc_run.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)
        return hook

    @staticmethod
    def _stage_then_delete(root: Path) -> None:
        """Stage an AWS-key-shaped probe, then remove it from disk."""
        probe = root / "probe.txt"
        probe.write_text(f'aws_key = "{_fake_aws_access_key_id()}"\n', encoding="utf-8")
        proc_run.run(["git", "add", "probe.txt"], cwd=root, check=True)
        probe.unlink()
        assert not probe.exists()
        staged = proc_run.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "probe.txt" in staged.stdout, f"probe was not staged: {staged.stdout!r}"

    @staticmethod
    def _run(hook: Path, root: Path, **env: str) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["bash", str(hook)],
            cwd=root,
            capture_output=True,
            text=True,
            env={**os.environ, **env},
        )

    # ------------------------------------------------------------------ non-regression

    def test_the_hook_permits_a_clean_staged_tree(self, tmp_path: Path) -> None:
        """The hook passes a clean staged tree."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._repo(tmp_path / "subject")
        (tmp_path / "subject" / "more.txt").write_text("also clean\n", encoding="utf-8")
        _stage_all(tmp_path / "subject")

        result = self._run(hook, tmp_path / "subject")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "all content-hygiene gates passed" in result.stdout

    def test_a_staged_only_secret_is_refused_in_a_clean_environment(self, tmp_path: Path) -> None:
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._repo(tmp_path / "subject")
        self._stage_then_delete(tmp_path / "subject")

        result = self._run(hook, tmp_path / "subject")

        assert result.returncode != 0, result.stdout + result.stderr
        assert "all content-hygiene gates passed" not in result.stdout + result.stderr

    # ------------------------------------------------------------------ the environment scrub

    def test_a_foreign_git_dir_no_longer_answers(self, tmp_path: Path) -> None:
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._repo(tmp_path / "subject")
        self._stage_then_delete(tmp_path / "subject")
        self._repo(tmp_path / "foreign")

        result = self._run(hook, tmp_path / "subject", GIT_DIR=str(tmp_path / "foreign" / ".git"))

        assert result.returncode != 0, (
            "a staged secret was not refused while GIT_DIR named another repository, so the hook is "
            f"still reading whichever repository the environment names:\n{result.stdout}{result.stderr}"
        )

    def test_that_refusal_is_load_bearing_on_the_scrub(self, tmp_path: Path) -> None:
        """With the scrub reverted, a foreign `GIT_DIR` yields a clean pass over a staged secret."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._repo(
            tmp_path / "subject", hook_text=_mutate_gate(PRE_COMMIT_HOOK, MUT_REVERT_SCRUB)
        )
        self._stage_then_delete(tmp_path / "subject")
        self._repo(tmp_path / "foreign")

        result = self._run(hook, tmp_path / "subject", GIT_DIR=str(tmp_path / "foreign" / ".git"))

        assert result.returncode == 0, (
            "with the scrub removed the hook was expected to report a clean pass over a staged secret "
            f"it could not see; it refused instead, so the cell above is not the scrub's doing:\n"
            f"{result.stdout}{result.stderr}"
        )
        assert "all content-hygiene gates passed" in result.stdout

    # ------------------------------------------------- GIT_INDEX_FILE: validated, never removed

    def test_a_foreign_index_is_refused_before_anything_is_scanned(self, tmp_path: Path) -> None:
        """A foreign `GIT_INDEX_FILE` is refused by the hook itself with `EXIT_UNREADABLE`.

        Without the hook's validator the shell gates refuse and the hook exits 1, so the status is
        the observable.
        """
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        hook = self._repo(tmp_path / "subject")
        self._stage_then_delete(tmp_path / "subject")
        self._repo(tmp_path / "foreign")

        result = self._run(
            hook, tmp_path / "subject", GIT_INDEX_FILE=str(tmp_path / "foreign" / ".git" / "index")
        )

        assert result.returncode == self._EXIT_UNREADABLE, (
            f"expected the hook's own refusal ({self._EXIT_UNREADABLE}), got {result.returncode}:\n"
            f"{result.stdout}{result.stderr}"
        )
        assert "does not resolve inside this repository" in result.stderr

    def test_a_partial_commit_of_a_clean_path_is_permitted(self, tmp_path: Path) -> None:
        """A partial commit whose temporary index excludes a secret staged in `.git/index` is
        permitted."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        work = tmp_path / "subject"
        hook = self._repo(work)
        self._stage_then_delete(work)
        temp_index = self._build_partial_commit_index(work)

        result = self._run(hook, work, GIT_INDEX_FILE=str(temp_index))

        assert result.returncode == 0, (
            "the hook refused a partial commit that records nothing wrong -- the staged secret it "
            f"objected to is in .git/index and is not part of this commit:\n{result.stdout}{result.stderr}"
        )

    def test_blanket_unsetting_the_index_would_refuse_that_same_commit(
        self, tmp_path: Path
    ) -> None:
        """With `GIT_INDEX_FILE` blanket-unset, the hook reads `.git/index` and refuses the same
        clean partial commit."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        work = tmp_path / "subject"
        hook = self._repo(work, hook_text=_mutate_gate(PRE_COMMIT_HOOK, MUT_BLANKET_UNSET_INDEX))
        self._stage_then_delete(work)
        temp_index = self._build_partial_commit_index(work)

        result = self._run(hook, work, GIT_INDEX_FILE=str(temp_index))

        assert result.returncode != 0, (
            "with GIT_INDEX_FILE blanket-unset the hook was expected to read .git/index and refuse a "
            "commit that records nothing wrong; it permitted it, so the cell above is not attributable "
            f"to keeping the name:\n{result.stdout}{result.stderr}"
        )

    @staticmethod
    def _build_partial_commit_index(work: Path) -> Path:
        """Build the temporary index git constructs for `git commit -- <path>`: `HEAD` plus the
        working-tree copy of the named path."""
        (work / "tracked.txt").write_text("a fresh clean line\n", encoding="utf-8")
        temp_index = work / ".git" / "next-index-probe.lock"
        env = {**os.environ, "GIT_INDEX_FILE": str(temp_index)}
        proc_run.run(["git", "read-tree", "HEAD"], cwd=work, check=True, env=env)
        proc_run.run(["git", "add", "tracked.txt"], cwd=work, check=True, env=env)
        staged = proc_run.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=work,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        assert "probe.txt" not in staged.stdout, (
            "the temporary index holds the staged probe, so this fixture is not the partial-commit "
            f"shape it claims to be: {staged.stdout!r}"
        )
        return temp_index

    # ------------------------------------------------------------------ the blinded differ

    def test_a_textconv_diff_driver_is_refused(self, tmp_path: Path) -> None:
        """A `* diff=d` attribute with a `textconv` driver is refused with `EXIT_UNREADABLE`.

        The driver replaces the content gitleaks reads while leaving `git grep` readable. With one
        staged key:

        ==========================  =============  ====================  =====================
        rule                        `check-attr`   `protect --staged`    `git grep -I` files
        ==========================  =============  ====================  =====================
        (none)                      unspecified    leaks found: 1        3
        `* -diff` / `* binary`      unset          no leaks found        0  <- gates refuse too
        `* diff=d` + textconv       d              no leaks found        3  <- gates see nothing
        ==========================  =============  ====================  =====================

        The hook accepts only `unspecified` or `set`.
        """
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        work = tmp_path / "subject"
        hook = self._repo(work)
        self._stage_then_delete(work)
        attributes = tmp_path / "attrs"
        attributes.write_text("* diff=d\n", encoding="utf-8")

        result = self._run(hook, work, **self._blinding_env(attributes))

        assert result.returncode == self._EXIT_UNREADABLE, (
            f"expected {self._EXIT_UNREADABLE} (subject unreadable), got {result.returncode}:\n"
            f"{result.stdout}{result.stderr}"
        )
        assert "'diff' attribute" in result.stderr

    def test_the_textconv_refusal_is_load_bearing_on_the_check_attr_control(
        self, tmp_path: Path
    ) -> None:
        """With the `check-attr` control reverted, the textconv route yields a clean pass."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        work = tmp_path / "subject"
        hook = self._repo(work, hook_text=_mutate_gate(PRE_COMMIT_HOOK, self._REVERT_ATTR_CONTROL))
        self._stage_then_delete(work)
        attributes = tmp_path / "attrs"
        attributes.write_text("* diff=d\n", encoding="utf-8")

        result = self._run(hook, work, **self._blinding_env(attributes))

        assert result.returncode == 0, (
            "with the check-attr control removed the hook was expected to report a clean pass over a "
            f"staged secret read through a neutered differ:\n{result.stdout}{result.stderr}"
        )
        assert "all content-hygiene gates passed" in result.stdout

    def test_a_narrow_gitattributes_does_not_trip_the_control(self, tmp_path: Path) -> None:
        """A `.gitattributes` with narrow per-pattern rules does not trip the `check-attr` control."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        work = tmp_path / "subject"
        hook = self._repo(work)
        (work / ".gitattributes").write_text(
            "*.png binary\n*.md diff=markdown\n*.lock -diff\n", encoding="utf-8"
        )
        _stage_all(work)

        result = self._run(hook, work)

        assert result.returncode == 0, (
            "a legitimately narrow .gitattributes was treated as a blinded differ:\n"
            f"{result.stdout}{result.stderr}"
        )

    @staticmethod
    def _blinding_env(attributes: Path) -> dict[str, str]:
        return {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.attributesFile",
            "GIT_CONFIG_VALUE_0": str(attributes),
            "GIT_CONFIG_KEY_1": "diff.d.textconv",
            "GIT_CONFIG_VALUE_1": "/usr/bin/true",
        }

    # ------------------------------------------------------------------ invariants

    def test_the_hook_does_not_unset_git_index_file(self) -> None:
        """The hook unsets the four scrubbed names, not `GIT_INDEX_FILE`, and keeps the physical-path
        git-dir comparison."""
        source = PRE_COMMIT_HOOK.read_text(encoding="utf-8")
        assert SCRUBBED_GIT_NAMES in source, "githooks/pre-commit does not scrub the measured names"
        assert f"{SCRUBBED_GIT_NAMES} GIT_INDEX_FILE" not in source, (
            "githooks/pre-commit unsets GIT_INDEX_FILE. git sets it for a hook on purpose and for a "
            "partial commit it names the temporary index holding the only copy of the content being "
            "committed -- validate it instead"
        )
        assert 'if [[ "$_index_dir" != "$_own_git_dir" ]]; then' in source, (
            "githooks/pre-commit lost the physical-path git-dir comparison that validates "
            "GIT_INDEX_FILE. A plain string compare refuses every commit under /tmp on macOS"
        )

    def test_the_declared_gitleaks_entries_resist_a_foreign_git_dir(self, tmp_path: Path) -> None:
        """The `gitleaks-staged` entry from `.pre-commit-config.yaml`, run as declared, refuses under a
        foreign `GIT_DIR`; without its `env -u` prefix it does not."""
        if not self._gitleaks_available():
            pytest.skip("gitleaks not installed")
        import shlex  # noqa: PLC0415

        import yaml  # noqa: PLC0415

        config = yaml.safe_load(PRE_COMMIT_CONFIG_YAML.read_text(encoding="utf-8"))
        entries = {h["id"]: h["entry"] for repo in config["repos"] for h in repo["hooks"]}
        argv = shlex.split(entries["gitleaks-staged"])
        assert argv[0] == "env", f"the staged entry no longer scrubs the environment: {argv}"
        bare = argv[argv.index("gitleaks") :]

        work = tmp_path / "subject"
        self._repo(work)
        self._stage_then_delete(work)
        self._repo(tmp_path / "foreign")
        foreign = {"GIT_DIR": str(tmp_path / "foreign" / ".git")}

        def run(command: list[str], env: dict[str, str]) -> int:
            return proc_run.run(
                command, cwd=work, capture_output=True, text=True, env={**os.environ, **env}
            ).returncode

        assert run(argv, foreign) != 0, "the declared entry did not refuse under a foreign GIT_DIR"
        assert run(bare, foreign) == 0, (
            "the entry without its env -u prefix was expected to report a clean pass, so the cell "
            "above is not attributable to the prefix"
        )
        assert run(argv, {}) != 0, "the declared entry stopped refusing in a clean environment"


# ---------------------------------------------------------------------------
# The shared read-only subjects are produced once, and are still the tree they were built as.
# ---------------------------------------------------------------------------


@_SHARED_SUBJECT_GROUP
def test_the_shared_subjects_are_produced_exactly_once(
    _invocation_log: Path,
    refmode_clean_worktree_scan: subprocess.CompletedProcess[str],
    refmode_clean_ref_head_scan: subprocess.CompletedProcess[str],
    subject_selection_readonly_clone: Path,
    real_repository_hook_run: subprocess.CompletedProcess[str],
) -> None:
    """Each shared subject is built exactly once across all xdist workers, per the invocation log."""
    lines = _invocation_log.read_text(encoding="utf-8").splitlines()
    observed = Counter(line.split("\t")[0] for line in lines)
    assert dict(observed) == _EXPECTED_INVOCATIONS, (
        "the expensive shared subjects were not built once each. Observed "
        f"{dict(observed)}, expected {_EXPECTED_INVOCATIONS}; the log's worker/pid columns name who "
        "built what:\n" + "\n".join(lines)
    )


def test_the_shared_subject_fingerprint_detects_every_kind_of_mutation(tmp_path: Path) -> None:
    """`_repo_fingerprint` is stable on an unchanged clone and moves on a worktree edit, a staged
    edit and a commit."""
    work = _clone_for_ref_mode(tmp_path / "probe")
    baseline = _repo_fingerprint(work)
    assert _repo_fingerprint(work) == baseline, (
        "the fingerprint is not stable across two reads of an unchanged repository, so an inequality "
        "below would prove nothing about mutation"
    )

    (work / "README.md").write_text("mutated on disk\n", encoding="utf-8")
    worktree_moved = _repo_fingerprint(work)
    assert worktree_moved != baseline, "a working-tree edit did not move the fingerprint"

    proc_run.run(["git", "-C", str(work), "add", "README.md"], check=True, capture_output=True)
    index_moved = _repo_fingerprint(work)
    assert index_moved not in (baseline, worktree_moved), (
        "staging the edit did not move the fingerprint, so the index component is not being read"
    )

    proc_run.run(
        ["git", "-C", str(work), "commit", "-qm", "probe"], check=True, capture_output=True
    )
    assert _repo_fingerprint(work) not in (baseline, worktree_moved, index_moved), (
        "committing did not move the fingerprint, so the HEAD component is not being read"
    )
