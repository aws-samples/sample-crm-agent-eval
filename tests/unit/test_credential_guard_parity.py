# SPDX-License-Identifier: MIT
"""Both credentialed terraform scripts refuse static keys, and both export the pair they report.

`deploy.sh` and `teardown.sh` each pass a resolved profile/region pair to terraform's provider as
`-var`. The terraform backend authenticates from the environment, so each script exports the pair and
refuses static keys, which take precedence over `AWS_PROFILE` in every AWS SDK.

Each case runs the real script inside a `PATH` that reaches neither `aws` nor `terraform`, so the
furthest either gets is its own `command -v aws` pre-flight.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: The two scripts, with the argument each needs to reach the guard. `teardown.sh` refuses without a
#: TTY unless given `--auto-approve`.
SCRIPTS: Final[dict[str, tuple[str, ...]]] = {
    "deploy.sh": (),
    "teardown.sh": ("--auto-approve",),
}

#: Either variable alone must trigger the refusal.
STATIC_CREDENTIAL_VARS: Final[tuple[str, ...]] = ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN")

#: The marker proving a run got past the guard: both scripts probe for the CLI after it.
PROGRESS_MARKER: Final[str] = "aws CLI not found on PATH"

REFUSAL_MARKER: Final[str] = "Static credentials take precedence over AWS_PROFILE"

#: Stripped from the inherited environment for every case; the refusal cases add back exactly the one
#: variable under test.
_STRIPPED: Final[tuple[str, ...]] = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


def _sandbox_path(tmp_path: Path) -> str:
    """A `PATH` with the ordinary utilities the scripts need and neither cloud CLI."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    return f"{bin_dir}:/bin:/usr/bin"


def _run(script: str, tmp_path: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    path_env = _sandbox_path(tmp_path)
    for executable in ("aws", "terraform"):
        if shutil.which(executable, path=path_env) is not None:
            pytest.skip(
                f"{executable} is reachable from {path_env!r}, so this host cannot run the guard "
                "cases without risking a real cloud call. Nothing was verified."
            )
    # The parent environment minus the credential variables, with the sandboxed PATH; `HOME` is kept
    # because `deploy.sh`'s git preflight reads it.
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    env["PATH"] = path_env
    env.update(extra_env)
    return proc_run.run(
        ["bash", str(REPO_ROOT / "scripts" / script), *SCRIPTS[script]],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.parametrize("script", sorted(SCRIPTS))
@pytest.mark.parametrize("variable", STATIC_CREDENTIAL_VARS)
def test_static_credentials_are_refused(script: str, variable: str, tmp_path: Path) -> None:
    result = _run(script, tmp_path, **{variable: "EXAMPLE-VALUE-NOT-A-CREDENTIAL"})
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert REFUSAL_MARKER in combined, combined
    assert PROGRESS_MARKER not in combined, (
        f"{script} got past the credential guard with {variable} set, which is the divergence the "
        f"guard exists to refuse: {combined}"
    )


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_the_guard_is_not_a_blanket_refusal(script: str, tmp_path: Path) -> None:
    """With no static credentials set, each script gets past the guard to its CLI pre-flight."""
    result = _run(script, tmp_path)
    combined = result.stdout + result.stderr
    assert REFUSAL_MARKER not in combined, combined
    assert PROGRESS_MARKER in combined, (
        f"{script} did not reach its `command -v aws` pre-flight, so this case did not exercise the "
        f"path past the guard and proves nothing about it: {combined}"
    )


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_the_refusal_names_what_was_not_done(script: str, tmp_path: Path) -> None:
    """Each script's refusal states that nothing was applied or destroyed."""
    expected = {"deploy.sh": "Nothing was applied", "teardown.sh": "Nothing has been destroyed"}[
        script
    ]
    result = _run(script, tmp_path, AWS_ACCESS_KEY_ID="EXAMPLE-VALUE-NOT-A-CREDENTIAL")
    assert expected in result.stdout + result.stderr


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_each_script_exports_the_pair_it_reports(script: str) -> None:
    """Each script's source exports `AWS_PROFILE`, `AWS_REGION` and `AWS_DEFAULT_REGION`."""
    source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
    for variable, value in (
        ("AWS_PROFILE", "$RESOLVED_PROFILE"),
        ("AWS_REGION", "$RESOLVED_REGION"),
        ("AWS_DEFAULT_REGION", "$RESOLVED_REGION"),
    ):
        assert f'export {variable}="{value}"' in source, (
            f"{script} does not export {variable}, so terraform's backend authenticates from the "
            "ambient environment while its provider uses the -var pair this script reports"
        )


def test_the_two_scripts_have_not_drifted_apart() -> None:
    """Both scripts carry exactly one copy of the same refusal condition."""
    condition = 'if [[ -n "${AWS_ACCESS_KEY_ID:-}" || -n "${AWS_SESSION_TOKEN:-}" ]]; then'
    for script in SCRIPTS:
        source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert source.count(condition) == 1, (
            f"{script} does not carry exactly one copy of the shared refusal condition, so the two "
            "scripts no longer refuse the same thing"
        )
