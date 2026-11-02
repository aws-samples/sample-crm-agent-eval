# SPDX-License-Identifier: MIT
"""Tests for `scripts/session_profile.sh`.

The helper writes the current STS session into an isolated profile directory and runs commands with
every ambient credential variable removed and the AWS config/credentials files pointed at that
directory. Every run here uses fake credential values carrying unique markers, and no marker may
reach stdout or stderr. The script runs under a sandbox `PATH` of symlinked tools, so no host
`tmutil` or `aws` is ever reached.
"""

from __future__ import annotations

import datetime
import json
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "scripts" / "session_profile.sh"

PROFILE: Final = "crm-test"
REGION: Final = "us-east-1"

AKID_MARKER: Final = "FAKEAKIDMARKERq7Zr"
SECRET_MARKER: Final = "fakeSecretMarker/x9Kp+Lm"
TOKEN_MARKER: Final = "fakeTokenMarkerW3vN"
#: The ambient key `exec` must discard; distinct from the key `write` puts in the file.
AMBIENT_AKID_MARKER: Final = "FAKEAMBIENTAKIDk4Tw"
MARKERS: Final = (AKID_MARKER, SECRET_MARKER, TOKEN_MARKER, AMBIENT_AKID_MARKER)

#: The entries `write` leaves in the session directory, sorted.
SESSION_ENTRIES: Final = [".session-profile", "config", "credentials", "expiration"]

#: Tools the script calls by name, symlinked into the sandbox `PATH`.
SANDBOX_TOOLS: Final = (
    "python3",
    "mktemp",
    "chmod",
    "mv",
    "mkdir",
    "cat",
    "rm",
    "rmdir",
    "dirname",
    "grep",
)

#: Unset by `exec`.
UNSET_ON_EXEC: Final = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "AWS_CREDENTIAL_EXPIRATION",
    "AWS_VAULT",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_STS",
)

_PRINT_ENV_PY: Final = "import json, os; print(json.dumps(dict(os.environ)))"

_BOTOCORE_PROBE_PY: Final = """
import json, sys
import botocore.exceptions, botocore.session
session = botocore.session.Session()
credentials = session.get_credentials()
try:
    botocore.session.Session(profile="default").get_credentials()
    default = "resolved"
except botocore.exceptions.ProfileNotFound:
    default = "ProfileNotFound"
print(json.dumps({
    "profiles": session.available_profiles,
    "key_matches": credentials is not None and credentials.access_key == sys.argv[1],
    "ambient_key_used": credentials is not None and credentials.access_key == sys.argv[2],
    "region": session.get_config_variable("region"),
    "default": default,
}))
"""


def _iso(minutes: float, offset_hours: int = 0) -> str:
    """An ISO-8601 timestamp `minutes` from now, written with a `+HH:00` offset or `Z`."""
    zone = datetime.timezone(datetime.timedelta(hours=offset_hours))
    moment = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=minutes)
    text = moment.astimezone(zone).isoformat(timespec="seconds")
    return text.replace("+00:00", "Z") if offset_hours == 0 else text


@pytest.fixture
def sandbox_bin(tmp_path: Path) -> Path:
    """A `PATH` directory holding only the tools the script needs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in SANDBOX_TOOLS:
        found = shutil.which(tool)
        assert found is not None, f"{tool} is not on the host PATH"
        (bin_dir / tool).symlink_to(found)
    return bin_dir


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """A session directory path outside any git work tree (it does not exist yet)."""
    assert not any((parent / ".git").exists() for parent in [tmp_path, *tmp_path.parents])
    return tmp_path / "state" / "cust"


def _session_env(minutes: float = 120) -> dict[str, str]:
    return {
        "AWS_ACCESS_KEY_ID": AKID_MARKER,
        "AWS_SECRET_ACCESS_KEY": SECRET_MARKER,
        "AWS_SESSION_TOKEN": TOKEN_MARKER,
        "AWS_CREDENTIAL_EXPIRATION": _iso(minutes),
    }


def _run(
    bin_dir: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the script under the sandbox PATH and assert no credential marker reached its output."""
    interpreter = shutil.which("bash")
    assert interpreter is not None
    result = proc_run.run(
        [interpreter, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(bin_dir), "HOME": str(bin_dir.parent), **(env or {})},
        cwd=str(bin_dir.parent),
    )
    for marker in MARKERS:
        assert marker not in result.stdout, f"a credential marker reached stdout: {args[:1]}"
        assert marker not in result.stderr, f"a credential marker reached stderr: {args[:1]}"
    return result


def _write(bin_dir: Path, directory: Path, **env: str) -> subprocess.CompletedProcess[str]:
    session = {**_session_env(), **env}
    return _run(
        bin_dir,
        "write",
        "--dir",
        str(directory),
        "--profile",
        PROFILE,
        "--region",
        REGION,
        env={k: v for k, v in session.items() if v},
    )


def _exec(
    bin_dir: Path, directory: Path, *command: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return _run(
        bin_dir,
        "exec",
        "--dir",
        str(directory),
        "--profile",
        PROFILE,
        "--region",
        REGION,
        "--",
        *command,
        env=env,
    )


def _tree_bytes(directory: Path) -> dict[str, bytes]:
    """Every entry under `directory`, relative path -> bytes (directories as a fixed token)."""
    return {
        str(p.relative_to(directory)): p.read_bytes() if p.is_file() else b"<dir>"
        for p in sorted(directory.rglob("*"))
    }


def _aws_shaped_dir(root: Path) -> Path:
    """A 0700 directory laid out like `~/.aws`: credentials, config and `sso/cache/`."""
    aws_dir = root / "home" / ".aws"
    (aws_dir / "sso" / "cache").mkdir(parents=True)
    aws_dir.chmod(0o700)
    (aws_dir / "credentials").write_text("[default]\naws_access_key_id = x\n", encoding="utf-8")
    (aws_dir / "config").write_text("[default]\nregion = us-east-1\n", encoding="utf-8")
    (aws_dir / "sso" / "cache" / "token.json").write_text("{}", encoding="utf-8")
    return aws_dir


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class TestWrite:
    def test_it_writes_the_three_files_with_owner_only_modes(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        result = _write(sandbox_bin, state_dir)

        assert result.returncode == 0, result.stderr
        assert _mode(state_dir) == 0o700
        assert sorted(p.name for p in state_dir.iterdir()) == SESSION_ENTRIES
        for name in SESSION_ENTRIES:
            assert _mode(state_dir / name) == 0o600

    def test_the_files_hold_exactly_one_profile(self, sandbox_bin: Path, state_dir: Path) -> None:
        expiration = _iso(120)
        assert _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=expiration).returncode == 0

        assert (state_dir / "credentials").read_text(encoding="utf-8") == (
            f"[{PROFILE}]\n"
            f"aws_access_key_id = {AKID_MARKER}\n"
            f"aws_secret_access_key = {SECRET_MARKER}\n"
            f"aws_session_token = {TOKEN_MARKER}\n"
        )
        assert (state_dir / "config").read_text(encoding="utf-8") == (
            f"[profile {PROFILE}]\nregion = {REGION}\n"
        )
        assert (state_dir / "expiration").read_text(encoding="utf-8") == f"{expiration}\n"

    def test_stdout_names_the_dir_profile_expiry_and_minutes(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        expiration = _iso(120.5)
        result = _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=expiration)

        assert f"dir={state_dir}" in result.stdout
        assert f"profile={PROFILE}" in result.stdout
        assert f"expires={expiration}" in result.stdout
        assert "minutes_left=120" in result.stdout

    @pytest.mark.parametrize("relative", ["state/cust", "../{parent}/state/cust"])
    def test_a_relative_dir_is_written_and_reported_as_an_absolute_path(
        self, sandbox_bin: Path, state_dir: Path, relative: str
    ) -> None:
        cwd = sandbox_bin.parent
        assert state_dir == cwd / "state" / "cust"
        given = relative.format(parent=cwd.name)

        result = _write(sandbox_bin, Path(given))

        assert result.returncode == 0, result.stderr
        assert sorted(p.name for p in state_dir.iterdir()) == SESSION_ENTRIES
        reported = Path(result.stdout.split("dir=", 1)[1].split(" ", 1)[0])
        assert reported.is_absolute(), result.stdout
        assert reported.resolve() == state_dir.resolve()

    def test_a_rewrite_into_an_existing_0700_dir_replaces_the_files(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        later = _iso(600)
        assert _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=later).returncode == 0

        assert (state_dir / "expiration").read_text(encoding="utf-8") == f"{later}\n"
        assert sorted(p.name for p in state_dir.iterdir()) == SESSION_ENTRIES

    def test_the_dir_is_excluded_from_time_machine_when_tmutil_exists(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        log = sandbox_bin.parent / "tmutil.log"
        stub = sandbox_bin / "tmutil"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n', encoding="utf-8")
        stub.chmod(0o755)

        assert _write(sandbox_bin, state_dir).returncode == 0
        assert log.read_text(encoding="utf-8") == f"addexclusion {state_dir}\n"

    def test_a_failing_tmutil_warns_and_does_not_fail_the_write(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        stub = sandbox_bin / "tmutil"
        stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)

        result = _write(sandbox_bin, state_dir)

        assert result.returncode == 0
        assert "warning: tmutil addexclusion" in result.stderr
        assert (state_dir / "credentials").is_file()

    def test_no_tmutil_means_no_warning(self, sandbox_bin: Path, state_dir: Path) -> None:
        result = _write(sandbox_bin, state_dir)

        assert result.returncode == 0
        assert result.stderr == ""


class TestWriteRefusals:
    @pytest.mark.parametrize(
        "missing",
        [
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_CREDENTIAL_EXPIRATION",
        ],
    )
    def test_a_missing_session_variable_is_refused(
        self, sandbox_bin: Path, state_dir: Path, missing: str
    ) -> None:
        result = _write(sandbox_bin, state_dir, **{missing: ""})

        assert result.returncode == 2
        assert missing in result.stderr
        assert not state_dir.exists()

    @pytest.mark.parametrize(
        ("minutes", "expiration", "reason"),
        [
            (30.0, None, "min left"),
            (-5.0, None, "min left"),
            (None, "2026-13-45T99:00:00Z", "ISO-8601"),
            (None, "2099-01-01T00:00:00", "ISO-8601"),
            (None, "tomorrow", "ISO-8601"),
        ],
    )
    def test_a_short_expired_or_malformed_expiry_is_refused(
        self,
        sandbox_bin: Path,
        state_dir: Path,
        minutes: float | None,
        expiration: str | None,
        reason: str,
    ) -> None:
        value = expiration if minutes is None else _iso(minutes)
        assert value is not None
        result = _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=value)

        assert result.returncode == 2
        assert reason in result.stderr
        assert not state_dir.exists()

    def test_sixty_one_minutes_is_accepted(self, sandbox_bin: Path, state_dir: Path) -> None:
        assert _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=_iso(61.5)).returncode == 0

    def test_exactly_sixty_whole_minutes_is_accepted(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        result = _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=_iso(60.5))

        assert result.returncode == 0, result.stderr
        assert "minutes_left=60" in result.stdout

    def test_fifty_nine_minutes_is_refused(self, sandbox_bin: Path, state_dir: Path) -> None:
        result = _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=_iso(59.5))

        assert result.returncode == 2
        assert "59 min left" in result.stderr

    @pytest.mark.parametrize("nested", ["", "a/b/c"])
    def test_a_dir_inside_a_git_work_tree_is_refused(
        self, sandbox_bin: Path, tmp_path: Path, nested: str
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        git = shutil.which("git")
        assert git is not None
        proc_run.run([git, "init", "-q", str(repo)], check=True)
        target = repo / nested / "session" if nested else repo / "session"

        result = _write(sandbox_bin, target)

        assert result.returncode == 2
        assert "git work tree" in result.stderr
        assert not target.exists()

    def test_a_symlinked_parent_pointing_into_a_work_tree_is_refused(
        self, sandbox_bin: Path, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        (repo / "sub").mkdir(parents=True)
        git = shutil.which("git")
        assert git is not None
        proc_run.run([git, "init", "-q", str(repo)], check=True)
        link = tmp_path / "lnk"
        link.symlink_to(repo / "sub")

        result = _write(sandbox_bin, link / "sess")

        assert result.returncode == 2
        assert "git work tree" in result.stderr
        assert list((repo / "sub").iterdir()) == []

    def test_a_symlinked_parent_pointing_outside_any_work_tree_is_accepted(
        self, sandbox_bin: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        link = tmp_path / "lnk"
        link.symlink_to(outside)

        assert _write(sandbox_bin, link / "sess").returncode == 0
        assert sorted(p.name for p in (outside / "sess").iterdir()) == SESSION_ENTRIES

    def test_a_git_worktree_file_marker_is_also_refused(
        self, sandbox_bin: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")

        assert _write(sandbox_bin, worktree / "session").returncode == 2

    @pytest.mark.parametrize("mode", [0o755, 0o750, 0o701, 0o1700])
    def test_an_existing_dir_that_is_not_0700_is_refused(
        self, sandbox_bin: Path, state_dir: Path, mode: int
    ) -> None:
        state_dir.mkdir(parents=True)
        state_dir.chmod(mode)

        result = _write(sandbox_bin, state_dir)

        assert result.returncode == 2
        assert "not mode 0700" in result.stderr
        assert list(state_dir.iterdir()) == []

    def test_a_symlinked_dir_is_refused(self, sandbox_bin: Path, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(real)

        assert _write(sandbox_bin, link).returncode == 2
        assert list(real.iterdir()) == []

    def test_an_aws_shaped_dir_is_refused_with_every_file_untouched(
        self, sandbox_bin: Path, tmp_path: Path
    ) -> None:
        aws_dir = _aws_shaped_dir(tmp_path)
        before = _tree_bytes(aws_dir)

        result = _write(sandbox_bin, aws_dir)

        assert result.returncode == 2
        assert "nothing was written" in result.stderr
        assert _tree_bytes(aws_dir) == before

    def test_an_unmarked_dir_holding_only_the_known_names_is_refused(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        state_dir.mkdir(parents=True, mode=0o700)
        (state_dir / "credentials").write_text("[default]\nkeep = me\n", encoding="utf-8")
        (state_dir / "config").write_text("[default]\n", encoding="utf-8")
        before = _tree_bytes(state_dir)

        result = _write(sandbox_bin, state_dir)

        assert result.returncode == 2
        assert "marker" in result.stderr
        assert _tree_bytes(state_dir) == before

    def test_a_marked_dir_with_a_foreign_file_is_refused(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        (state_dir / "notes.txt").write_text("mine", encoding="utf-8")
        before = _tree_bytes(state_dir)

        result = _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=_iso(600))

        assert result.returncode == 2
        assert "'notes.txt'" in result.stderr
        assert _tree_bytes(state_dir) == before

    def test_an_empty_existing_0700_dir_is_accepted(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        state_dir.mkdir(parents=True, mode=0o700)

        assert _write(sandbox_bin, state_dir).returncode == 0
        assert sorted(p.name for p in state_dir.iterdir()) == SESSION_ENTRIES

    def test_a_marker_owned_dir_is_overwritten(self, sandbox_bin: Path, state_dir: Path) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        later = _iso(600)

        result = _write(
            sandbox_bin,
            state_dir,
            AWS_SESSION_TOKEN=TOKEN_MARKER + "Two",
            AWS_CREDENTIAL_EXPIRATION=later,
        )

        assert result.returncode == 0, result.stderr
        assert f"aws_session_token = {TOKEN_MARKER}Two\n" in (state_dir / "credentials").read_text(
            encoding="utf-8"
        )
        assert (state_dir / "expiration").read_text(encoding="utf-8") == f"{later}\n"
        assert sorted(p.name for p in state_dir.iterdir()) == SESSION_ENTRIES

    def test_a_value_with_whitespace_is_refused(self, sandbox_bin: Path, state_dir: Path) -> None:
        result = _write(sandbox_bin, state_dir, AWS_SESSION_TOKEN=f"{TOKEN_MARKER}\n[default]")

        assert result.returncode == 2
        assert "AWS_SESSION_TOKEN contains whitespace" in result.stderr
        assert not state_dir.exists()

    @pytest.mark.parametrize(
        "args",
        [
            ("write", "--profile", PROFILE, "--region", REGION),
            ("write", "--dir", "X", "--region", REGION),
            ("write", "--dir", "X", "--profile", PROFILE),
            ("write", "--dir", "X", "--profile", "bad]name", "--region", REGION),
            ("write", "--dir", "X", "--profile", PROFILE, "--region", "us_east_1"),
            ("write", "--dir", "X", "--profile", PROFILE, "--region", REGION, "--", "true"),
            ("write", "--dir"),
            ("write", "--bogus"),
            ("frobnicate", "--dir", "X"),
            (),
        ],
    )
    def test_usage_errors_exit_2(self, sandbox_bin: Path, args: tuple[str, ...]) -> None:
        result = _run(sandbox_bin, *args, env=_session_env())

        assert result.returncode == 2
        assert not (sandbox_bin.parent / "X").exists()


class TestExec:
    @pytest.fixture
    def written(self, sandbox_bin: Path, state_dir: Path) -> Path:
        assert _write(sandbox_bin, state_dir).returncode == 0
        return state_dir

    def _ambient(self) -> dict[str, str]:
        ambient = {name: f"ambient-{name.lower()}" for name in UNSET_ON_EXEC}
        ambient.update(_session_env())
        ambient["AWS_ACCESS_KEY_ID"] = AMBIENT_AKID_MARKER
        ambient["AWS_PROFILE"] = "claude"
        ambient["AWS_REGION"] = "us-west-2"
        ambient["AWS_DEFAULT_REGION"] = "us-west-2"
        ambient["AWS_CONFIG_FILE"] = "/elsewhere/config"
        ambient["AWS_SHARED_CREDENTIALS_FILE"] = "/elsewhere/credentials"
        ambient["UNRELATED"] = "kept"
        return ambient

    def _child_env(self, sandbox_bin: Path, written: Path) -> dict[str, str]:
        result = _exec(
            sandbox_bin, written, sys.executable, "-c", _PRINT_ENV_PY, env=self._ambient()
        )
        assert result.returncode == 0, result.stderr
        child: dict[str, str] = json.loads(result.stdout)
        return child

    def test_the_parent_carries_every_name_it_must_remove(self) -> None:
        assert set(UNSET_ON_EXEC) <= set(self._ambient())

    @pytest.mark.parametrize("name", UNSET_ON_EXEC)
    def test_each_ambient_credential_variable_is_removed(
        self, sandbox_bin: Path, written: Path, name: str
    ) -> None:
        child = self._child_env(sandbox_bin, written)

        if name == "AWS_PROFILE":
            assert child[name] == PROFILE
        else:
            assert name not in child

    def test_every_endpoint_override_is_removed_and_nothing_else(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        ambient = {
            "AWS_ENDPOINT_URL_S3": "https://s3.example",
            "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": "https://br.example",
            "AWS_ENDPOINT_URLX": "odd",
            "NOT_AWS_ENDPOINT_URL": "kept",
        }
        result = _exec(sandbox_bin, written, sys.executable, "-c", _PRINT_ENV_PY, env=ambient)

        child = json.loads(result.stdout)
        assert not [name for name in child if name.startswith("AWS_ENDPOINT_URL")]
        assert child["NOT_AWS_ENDPOINT_URL"] == "kept"

    def test_the_isolation_variables_are_set(self, sandbox_bin: Path, written: Path) -> None:
        child = self._child_env(sandbox_bin, written)

        assert child["AWS_CONFIG_FILE"] == str(written / "config")
        assert child["AWS_SHARED_CREDENTIALS_FILE"] == str(written / "credentials")
        assert child["AWS_PROFILE"] == PROFILE
        assert child["AWS_REGION"] == REGION
        assert child["AWS_DEFAULT_REGION"] == REGION
        assert child["AWS_SDK_LOAD_CONFIG"] == "1"
        assert child["UNRELATED"] == "kept"

    def test_the_instance_metadata_fallback_is_disabled(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        result = _exec(
            sandbox_bin,
            written,
            sys.executable,
            "-c",
            _PRINT_ENV_PY,
            env={"AWS_EC2_METADATA_DISABLED": "false"},
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["AWS_EC2_METADATA_DISABLED"] == "true"

    @pytest.mark.parametrize("relative", ["state/cust", "../{parent}/state/cust"])
    def test_a_relative_dir_is_exported_as_an_absolute_path(
        self, sandbox_bin: Path, written: Path, relative: str
    ) -> None:
        cwd = sandbox_bin.parent
        assert written == cwd / "state" / "cust"
        given = relative.format(parent=cwd.name)

        result = _exec(sandbox_bin, Path(given), sys.executable, "-c", _PRINT_ENV_PY)

        assert result.returncode == 0, result.stderr
        child = json.loads(result.stdout)
        for name, file in (
            ("AWS_CONFIG_FILE", "config"),
            ("AWS_SHARED_CREDENTIALS_FILE", "credentials"),
        ):
            assert Path(child[name]).is_absolute(), child[name]
            assert Path(child[name]).resolve() == (written / file).resolve()

    def test_only_the_one_profile_resolves_and_default_is_not_found(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        result = _exec(
            sandbox_bin,
            written,
            sys.executable,
            "-c",
            _BOTOCORE_PROBE_PY,
            AKID_MARKER,
            AMBIENT_AKID_MARKER,
            env=self._ambient(),
        )

        assert result.returncode == 0, result.stderr
        probe = json.loads(result.stdout)
        assert probe == {
            "profiles": [PROFILE],
            "key_matches": True,
            "ambient_key_used": False,
            "region": REGION,
            "default": "ProfileNotFound",
        }

    def test_the_callers_lc_all_reaches_the_command_unchanged(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        env = {"LC_ALL": "en_US.UTF-8"}
        result = _exec(sandbox_bin, written, sys.executable, "-c", _PRINT_ENV_PY, env=env)

        assert json.loads(result.stdout)["LC_ALL"] == "en_US.UTF-8"

    def test_an_unset_lc_all_stays_unset_for_the_command(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        result = _exec(sandbox_bin, written, sys.executable, "-c", _PRINT_ENV_PY)

        assert "LC_ALL" not in json.loads(result.stdout)

    def test_the_command_exit_status_passes_through(self, sandbox_bin: Path, written: Path) -> None:
        result = _exec(sandbox_bin, written, sys.executable, "-c", "raise SystemExit(7)")

        assert result.returncode == 7

    def test_fewer_than_fifteen_minutes_is_refused_with_4(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        (written / "expiration").write_text(f"{_iso(10)}\n", encoding="utf-8")
        canary = written.parent / "ran"

        result = _exec(sandbox_bin, written, sys.executable, "-c", f"open({str(canary)!r}, 'w')")

        assert result.returncode == 4
        assert "min left" in result.stderr
        assert not canary.exists()

    def test_sixteen_minutes_still_runs(self, sandbox_bin: Path, written: Path) -> None:
        (written / "expiration").write_text(f"{_iso(16.5)}\n", encoding="utf-8")

        assert _exec(sandbox_bin, written, sys.executable, "-c", "pass").returncode == 0

    def test_exactly_fifteen_whole_minutes_still_runs(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        (written / "expiration").write_text(f"{_iso(15.5)}\n", encoding="utf-8")

        result = _exec(sandbox_bin, written, sys.executable, "-c", "pass")

        assert result.returncode == 0, result.stderr

    def test_a_bad_region_is_a_usage_error_and_the_command_never_runs(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        canary = written.parent / "ran"

        result = _run(
            sandbox_bin,
            "exec",
            "--dir",
            str(written),
            "--profile",
            PROFILE,
            "--region",
            "us_east_1",
            "--",
            sys.executable,
            "-c",
            f"open({str(canary)!r}, 'w')",
        )

        assert result.returncode == 2
        assert "--region must be an AWS region name" in result.stderr
        assert not canary.exists()

    @pytest.mark.parametrize("name", ["credentials", "config", "expiration"])
    def test_an_absent_session_file_is_refused_with_4(
        self, sandbox_bin: Path, written: Path, name: str
    ) -> None:
        (written / name).unlink()

        assert _exec(sandbox_bin, written, sys.executable, "-c", "pass").returncode == 4

    def test_a_never_written_dir_is_refused_with_4(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        assert _exec(sandbox_bin, state_dir, sys.executable, "-c", "pass").returncode == 4

    def test_a_profile_the_dir_does_not_hold_is_refused(
        self, sandbox_bin: Path, written: Path
    ) -> None:
        result = _run(
            sandbox_bin,
            "exec",
            "--dir",
            str(written),
            "--profile",
            "other",
            "--region",
            REGION,
            "--",
            sys.executable,
        )

        assert result.returncode == 2
        assert "holds no profile 'other'" in result.stderr

    def test_a_missing_command_is_a_usage_error(self, sandbox_bin: Path, written: Path) -> None:
        assert _exec(sandbox_bin, written).returncode == 2


class TestStatus:
    def _status(self, sandbox_bin: Path, directory: Path) -> subprocess.CompletedProcess[str]:
        return _run(sandbox_bin, "status", "--dir", str(directory), "--profile", PROFILE)

    def test_it_reports_the_minutes_left(self, sandbox_bin: Path, state_dir: Path) -> None:
        assert _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=_iso(90.5)).returncode == 0

        result = self._status(sandbox_bin, state_dir)

        assert result.returncode == 0
        assert "minutes_left=90" in result.stdout

    def test_the_offset_is_honoured_in_the_arithmetic(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        expiration = _iso(90.5, offset_hours=2)
        assert expiration.endswith("+02:00")
        assert _write(sandbox_bin, state_dir, AWS_CREDENTIAL_EXPIRATION=expiration).returncode == 0

        assert "minutes_left=90" in self._status(sandbox_bin, state_dir).stdout

    def test_exactly_fifteen_whole_minutes_exits_0(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        (state_dir / "expiration").write_text(f"{_iso(15.5)}\n", encoding="utf-8")

        result = self._status(sandbox_bin, state_dir)

        assert result.returncode == 0, result.stderr
        assert "minutes_left=15" in result.stdout

    def test_below_fifteen_minutes_exits_4(self, sandbox_bin: Path, state_dir: Path) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        (state_dir / "expiration").write_text(f"{_iso(14.5)}\n", encoding="utf-8")

        result = self._status(sandbox_bin, state_dir)

        assert result.returncode == 4
        assert "minutes_left=14" in result.stdout

    def test_a_corrupt_expiration_exits_4(self, sandbox_bin: Path, state_dir: Path) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        (state_dir / "expiration").write_text("garbage\n", encoding="utf-8")

        assert self._status(sandbox_bin, state_dir).returncode == 4

    def test_absent_files_exit_4(self, sandbox_bin: Path, state_dir: Path) -> None:
        assert self._status(sandbox_bin, state_dir).returncode == 4


class TestDelete:
    def _delete(self, sandbox_bin: Path, directory: Path) -> subprocess.CompletedProcess[str]:
        return _run(sandbox_bin, "delete", "--dir", str(directory))

    def test_it_removes_the_files_and_the_dir(self, sandbox_bin: Path, state_dir: Path) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0

        result = self._delete(sandbox_bin, state_dir)

        assert result.returncode == 0
        assert not state_dir.exists()
        assert state_dir.parent.is_dir()

    def test_a_leftover_temp_file_is_removed_too(self, sandbox_bin: Path, state_dir: Path) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        (state_dir / ".credentials.AbC123").write_text("partial", encoding="utf-8")

        assert self._delete(sandbox_bin, state_dir).returncode == 0
        assert not state_dir.exists()

    def test_an_absent_dir_is_a_no_op(self, sandbox_bin: Path, state_dir: Path) -> None:
        result = self._delete(sandbox_bin, state_dir)

        assert result.returncode == 0
        assert "already absent" in result.stdout

    def test_a_foreign_file_refuses_with_every_file_left_in_place(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        (state_dir / "notes.txt").write_text("mine", encoding="utf-8")
        before = _tree_bytes(state_dir)

        result = self._delete(sandbox_bin, state_dir)

        assert result.returncode == 2
        assert "'notes.txt'" in result.stderr
        assert "nothing was removed" in result.stderr
        assert _tree_bytes(state_dir) == before
        assert len(before) == 5

    def test_an_aws_shaped_directory_is_refused_with_every_file_intact(
        self, sandbox_bin: Path, tmp_path: Path
    ) -> None:
        aws_dir = _aws_shaped_dir(tmp_path)
        before = _tree_bytes(aws_dir)

        result = self._delete(sandbox_bin, aws_dir)

        assert result.returncode == 2
        assert "nothing was removed" in result.stderr
        assert _tree_bytes(aws_dir) == before

    def test_the_known_names_alone_without_the_marker_are_refused(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        (state_dir / ".session-profile").unlink()
        before = _tree_bytes(state_dir)

        result = self._delete(sandbox_bin, state_dir)

        assert result.returncode == 2
        assert "marker" in result.stderr
        assert _tree_bytes(state_dir) == before

    def test_a_marked_dir_that_is_not_0700_is_refused(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        state_dir.chmod(0o755)
        before = _tree_bytes(state_dir)

        result = self._delete(sandbox_bin, state_dir)

        assert result.returncode == 2
        assert "not mode 0700" in result.stderr
        assert _tree_bytes(state_dir) == before

    def test_a_subdirectory_under_a_known_name_is_refused(
        self, sandbox_bin: Path, state_dir: Path
    ) -> None:
        assert _write(sandbox_bin, state_dir).returncode == 0
        (state_dir / "credentials").unlink()
        (state_dir / "credentials").mkdir()
        before = _tree_bytes(state_dir)

        assert self._delete(sandbox_bin, state_dir).returncode == 2
        assert _tree_bytes(state_dir) == before

    def test_a_symlinked_dir_is_refused(self, sandbox_bin: Path, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        (real / "credentials").write_text("x", encoding="utf-8")
        link = tmp_path / "link"
        link.symlink_to(real)

        assert self._delete(sandbox_bin, link).returncode == 2
        assert (real / "credentials").exists()


def test_the_script_is_executable_and_strict() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    assert text.startswith("#!/usr/bin/env bash\n# SPDX-License-Identifier: MIT\n")
    assert "\nset -euo pipefail\n" in text
