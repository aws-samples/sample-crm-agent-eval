# SPDX-License-Identifier: MIT
"""Pins the AgentCore runtime image's interpreter and pip to the same versions CI uses.

One interpreter version across the `Dockerfile` base tag, `.python-version` and the
`.gitlab-ci.yml` image tag; one pip version across the `Dockerfile` pin and `.gitlab-ci.yml`'s
`PIP_VERSION`. The `Dockerfile` must install that pip before `-r requirements.txt`, and install the
lock with `--no-deps`. `pin_drift` takes the repository root so each check is proved red against a
mutated copy.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PINNED_FILES = ("Dockerfile", ".python-version", ".gitlab-ci.yml")

_FROM = re.compile(r"^FROM\s+(?P<args>.*)$", re.M)
_BASE_TAG = re.compile(r"public\.ecr\.aws/docker/library/python:(?P<version>[^\s-]+)-slim\b")
_CI_IMAGE = re.compile(r"^python:(?P<version>[^\s-]+)-slim$")
_FULL_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_PIP_PIN = re.compile(r"\bpip install\b[^\n&]*?\bpip==(?P<version>\S+)")
_REQUIREMENTS_INSTALL = re.compile(r"\bpip install\b(?P<flags>[^\n&]*?)-r requirements\.txt")


def _dockerfile_base_version(dockerfile: str) -> tuple[str | None, list[str]]:
    froms = _FROM.findall(dockerfile)
    if len(froms) != 1:
        return None, [f"Dockerfile has {len(froms)} FROM lines, expected 1"]
    errors: list[str] = []
    if "--platform=linux/arm64" not in froms[0]:
        errors.append("Dockerfile FROM lacks --platform=linux/arm64")
    match = _BASE_TAG.search(froms[0])
    if match is None:
        return None, [
            *errors,
            f"Dockerfile FROM is not the ECR Public python slim image: {froms[0]}",
        ]
    version = match["version"]
    if not _FULL_VERSION.match(version):
        errors.append(f"Dockerfile base tag {version!r} is not a full x.y.z patch version")
    return version, errors


def _ci_pip_versions(ci: dict[str, Any]) -> set[str]:
    """Every `PIP_VERSION` defined in `.gitlab-ci.yml`, globally or in any job."""
    scopes: list[Any] = [ci.get("variables")]
    scopes += [job.get("variables") for job in ci.values() if isinstance(job, dict)]
    found: set[str] = set()
    for variables in scopes:
        if isinstance(variables, dict) and "PIP_VERSION" in variables:
            value = variables["PIP_VERSION"]
            found.add(str(value["value"] if isinstance(value, dict) else value))
    return found


def pin_drift(root: Path) -> list[str]:
    """Every interpreter/pip pin disagreement under `root`; empty when the pins agree."""
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    python_version = (root / ".python-version").read_text(encoding="utf-8").strip()
    ci = yaml.safe_load((root / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    base_version, errors = _dockerfile_base_version(dockerfile)
    if base_version is not None and base_version != python_version:
        errors.append(f"Dockerfile base {base_version} != .python-version {python_version}")

    ci_image = _CI_IMAGE.match(str(ci.get("image", "")))
    if ci_image is None:
        errors.append(f".gitlab-ci.yml image {ci.get('image')!r} is not python:<version>-slim")
    elif ci_image["version"] != python_version:
        errors.append(
            f".gitlab-ci.yml image {ci_image['version']} != .python-version {python_version}"
        )

    pip_pins = _PIP_PIN.findall(dockerfile)
    requirements_installs = list(_REQUIREMENTS_INSTALL.finditer(dockerfile))
    if len(pip_pins) != 1:
        errors.append(f"Dockerfile has {len(pip_pins)} pip==<version> installs, expected 1")
    if len(requirements_installs) != 1:
        errors.append(f"Dockerfile has {len(requirements_installs)} -r requirements.txt installs")
    else:
        install = requirements_installs[0]
        if "--no-deps" not in install["flags"].split():
            errors.append("Dockerfile requirements install lacks --no-deps")
        pin = _PIP_PIN.search(dockerfile)
        if pin is not None and pin.start() > install.start():
            errors.append("Dockerfile installs pinned pip after -r requirements.txt")

    ci_pip = _ci_pip_versions(ci)
    if len(ci_pip) != 1:
        errors.append(f".gitlab-ci.yml defines PIP_VERSION {sorted(ci_pip)}, expected one value")
    elif len(pip_pins) == 1 and pip_pins[0] != next(iter(ci_pip)):
        errors.append(
            f"Dockerfile pip {pip_pins[0]} != .gitlab-ci.yml PIP_VERSION {next(iter(ci_pip))}"
        )
    return errors


def test_repository_pins_agree() -> None:
    assert pin_drift(REPO_ROOT) == []


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"mutation anchor {old!r} absent from {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _swap_dockerfile_installs(root: Path) -> None:
    dockerfile = root / "Dockerfile"
    lines = dockerfile.read_text(encoding="utf-8").splitlines(keepends=True)
    pin = next(i for i, line in enumerate(lines) if "pip==" in line)
    lines[pin], lines[pin + 1] = lines[pin + 1], lines[pin]
    dockerfile.write_text("".join(lines), encoding="utf-8")


def _python_version(root: Path) -> str:
    return (root / ".python-version").read_text(encoding="utf-8").strip()


def _ci_pip_version(root: Path) -> str:
    ci = yaml.safe_load((root / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    return next(iter(_ci_pip_versions(ci)))


_MUTATIONS: dict[str, tuple[Callable[[Path], object], str]] = {
    "dockerfile-base-floating": (
        lambda r: _replace(
            r / "Dockerfile", f"python:{_python_version(r)}-slim", "python:3.13-slim"
        ),
        "not a full x.y.z",
    ),
    "dockerfile-base-other-patch": (
        lambda r: _replace(
            r / "Dockerfile", f"python:{_python_version(r)}-slim", "python:3.13.0-slim"
        ),
        "!= .python-version",
    ),
    "dockerfile-platform-dropped": (
        lambda r: _replace(r / "Dockerfile", "--platform=linux/arm64 ", ""),
        "--platform=linux/arm64",
    ),
    "python-version-moved": (
        lambda r: (r / ".python-version").write_text("3.13.10\n", encoding="utf-8"),
        "!= .python-version 3.13.10",
    ),
    "ci-image-moved": (
        lambda r: _replace(
            r / ".gitlab-ci.yml",
            f"image: python:{_python_version(r)}-slim",
            "image: python:3.13.0-slim",
        ),
        ".gitlab-ci.yml image 3.13.0",
    ),
    "dockerfile-pip-moved": (
        lambda r: _replace(r / "Dockerfile", f"pip=={_ci_pip_version(r)}", "pip==25.2"),
        "Dockerfile pip 25.2 !=",
    ),
    "dockerfile-pip-dropped": (
        lambda r: _replace(r / "Dockerfile", f"pip=={_ci_pip_version(r)}", "pip"),
        "0 pip==<version> installs",
    ),
    "ci-pip-moved": (
        lambda r: _replace(
            r / ".gitlab-ci.yml", f'PIP_VERSION: "{_ci_pip_version(r)}"', 'PIP_VERSION: "25.2"'
        ),
        "!= .gitlab-ci.yml PIP_VERSION 25.2",
    ),
    "dockerfile-no-deps-dropped": (
        lambda r: _replace(
            r / "Dockerfile", "--no-deps -r requirements.txt", "-r requirements.txt"
        ),
        "lacks --no-deps",
    ),
    "dockerfile-pip-after-requirements": (_swap_dockerfile_installs, "after -r requirements.txt"),
}


@pytest.mark.parametrize("mutation", sorted(_MUTATIONS))
def test_each_pin_drift_is_reported(mutation: str, tmp_path: Path) -> None:
    for name in PINNED_FILES:
        shutil.copyfile(REPO_ROOT / name, tmp_path / name)
    assert pin_drift(tmp_path) == []

    mutate, expected = _MUTATIONS[mutation]
    mutate(tmp_path)

    errors = pin_drift(tmp_path)
    assert any(expected in error for error in errors), errors
