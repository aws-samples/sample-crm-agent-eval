# SPDX-License-Identifier: MIT
"""`package_lambdas.sh`'s clean step preserves `package_relay.sh`'s artefact.

Asserts that the two scripts agree on the zip filename, and that the clean command preserves that
file and removes everything else. The clean command runs in a scratch directory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PACKAGE_LAMBDAS: Final[Path] = REPO_ROOT / "scripts" / "package_lambdas.sh"
PACKAGE_RELAY: Final[Path] = REPO_ROOT / "scripts" / "package_relay.sh"


def _constant(script: Path, name: str) -> str:
    """The value of a `NAME="value"` shell assignment, `readonly` or not."""
    match = re.search(
        rf'(?m)^(?:readonly\s+)?{re.escape(name)}="([^"]+)"$', script.read_text(encoding="utf-8")
    )
    assert match is not None, f"{script.name} declares no {name}"
    return match.group(1)


def test_the_two_scripts_name_the_same_artefact() -> None:
    """`RELAY_ZIP_NAME` in package_lambdas.sh equals `ZIP_NAME` in package_relay.sh."""
    assert _constant(PACKAGE_LAMBDAS, "RELAY_ZIP_NAME") == _constant(PACKAGE_RELAY, "ZIP_NAME")


def test_the_clean_command_is_still_the_scoped_form() -> None:
    """The script uses the scoped `find ... ! -name` clean, not a blanket `rm -rf`."""
    source = PACKAGE_LAMBDAS.read_text(encoding="utf-8")
    assert '\nrm -rf "${BUILD_DIR}"\n' not in source, (
        "the blanket wipe is back, so a `relay.zip` in the shared build directory is deleted again"
    )
    assert re.search(
        r'find "\$\{BUILD_DIR\}" -mindepth 1 -maxdepth 1 ! -name "\$\{RELAY_ZIP_NAME\}"', source
    ), "the scoped clean command is gone or reshaped; re-read it and re-point this test"


def test_the_clean_command_preserves_only_the_relay_zip(tmp_path: Path) -> None:
    """The clean command leaves only the relay zip, removing sibling files and subdirectories."""
    build_dir = tmp_path / "lambda"
    build_dir.mkdir()
    relay_name = _constant(PACKAGE_LAMBDAS, "RELAY_ZIP_NAME")
    keep = build_dir / relay_name
    keep.write_text("relay", encoding="utf-8")
    (build_dir / "get_account.zip").write_text("stale tool zip", encoding="utf-8")
    (build_dir / "notes.txt").write_text("unrelated", encoding="utf-8")
    staging = build_dir / "stage"
    staging.mkdir()
    (staging / "nested").write_text("nested", encoding="utf-8")

    result = proc_run.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'find "${BUILD_DIR}" -mindepth 1 -maxdepth 1 ! -name "${RELAY_ZIP_NAME}" -exec rm -rf {} +',
        ],
        env={"BUILD_DIR": str(build_dir), "RELAY_ZIP_NAME": relay_name, "PATH": "/bin:/usr/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert sorted(p.name for p in build_dir.iterdir()) == [relay_name]
    assert keep.read_text(encoding="utf-8") == "relay", "the preserved artefact was rewritten"


def test_a_missing_build_directory_is_not_an_error() -> None:
    """The clean step is guarded on the build directory existing."""
    source = PACKAGE_LAMBDAS.read_text(encoding="utf-8")
    guarded = re.search(
        r'if \[\[ -d "\$\{BUILD_DIR\}" \]\]; then\s*\n\s*find "\$\{BUILD_DIR\}"', source
    )
    assert guarded is not None, (
        "the clean step is no longer guarded on the build directory existing, so a fresh clone fails "
        "in `find` before packaging anything"
    )


@pytest.mark.parametrize("script", [PACKAGE_LAMBDAS, PACKAGE_RELAY])
def test_both_scripts_parse(script: Path) -> None:
    """Both packaging scripts pass `bash -n`."""
    result = proc_run.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
