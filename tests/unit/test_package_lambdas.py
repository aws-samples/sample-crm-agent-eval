# SPDX-License-Identifier: MIT
"""Contents of the zips `scripts/package_lambdas.sh` builds, `chat_target.zip` in detail.

Reads the real build directory. Skips when nothing has been built, unless
`CRM_REQUIRE_RELAY_PACKAGING=1`, which turns the skip into a failure. Two directions are checked:
every zip the infra references is built, and each new zip carries what its handler loads at runtime
and no model SDK.
"""

from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BUILD_DIR: Final = REPO_ROOT / "infra" / "build" / "lambda"
SCRIPT: Final = REPO_ROOT / "scripts" / "package_lambdas.sh"
REQUIRE_ENV: Final = "CRM_REQUIRE_RELAY_PACKAGING"

#: Top-level directories a model SDK would add to an archive root.
MODEL_SDK_ROOTS: Final = frozenset({"anthropic", "openai", "httpx2"})

#: Archives built by `scripts/package_relay.sh` or not Lambda code, so not this script's.
_NOT_THIS_SCRIPTS: Final = frozenset({"relay.zip"})


def _zip_members(name: str) -> frozenset[str]:
    path = BUILD_DIR / name
    if not path.is_file():
        message = f"no built {name} under {BUILD_DIR}; run scripts/package_lambdas.sh"
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail(f"{message} ({REQUIRE_ENV}=1 forbids skipping)")
        pytest.skip(message)
    with zipfile.ZipFile(path) as archive:
        return frozenset(archive.namelist())


def _roots(members: frozenset[str]) -> frozenset[str]:
    return frozenset(member.split("/", 1)[0] for member in members)


def _infra_zip_names() -> frozenset[str]:
    """Every `<name>.zip` the root composition reads from the build directory, literal or local."""
    names: set[str] = set()
    for path in sorted((REPO_ROOT / "infra").glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        names.update(re.findall(r"\$\{var\.lambda_build_dir\}/([A-Za-z0-9_.-]+\.zip)", text))
    return frozenset(names)


def test_the_infra_reads_chat_target_zip_and_the_script_builds_it() -> None:
    """The composition references `chat_target.zip`, and the script names it as an output."""
    assert "chat_target.zip" in _infra_zip_names()
    assert '"${BUILD_DIR}/chat_target.zip"' in SCRIPT.read_text(encoding="utf-8")


def test_every_zip_the_infra_reads_is_built() -> None:
    """No `filebase64sha256` call site names an archive the last build did not produce."""
    if not BUILD_DIR.is_dir() or not any(BUILD_DIR.glob("*.zip")):
        _zip_members("chat_target.zip")
    built = {path.name for path in BUILD_DIR.glob("*.zip")}
    expected_here = {
        name for name in _infra_zip_names() if name not in _NOT_THIS_SCRIPTS and "${" not in name
    }
    assert expected_here - built == set(), sorted(expected_here - built)


def test_chat_target_zip_carries_its_runtime_closure() -> None:
    """`src/` layout with crm/, gateway/ and providers/; config/ at the root; jsonschema and yaml."""
    members = _zip_members("chat_target.zip")
    for required in (
        "src/gateway/chat_target.py",
        "src/crm/conversations.py",
        "src/crm/user_settings.py",
        "src/providers/capabilities.py",
        "src/providers/tier_tuning.py",
        "config/models.yaml",
        "config/model_capabilities.yaml",
    ):
        assert required in members, required
    roots = _roots(members)
    assert {"yaml", "jsonschema"} <= roots, sorted(roots)


def test_chat_target_zip_carries_no_model_sdk_and_no_agent_or_eval_code() -> None:
    members = _zip_members("chat_target.zip")
    roots = _roots(members)
    assert roots & MODEL_SDK_ROOTS == set(), sorted(roots & MODEL_SDK_ROOTS)
    shipped = {
        member.split("/")[1] for member in members if member.startswith("src/") and member != "src/"
    }
    assert shipped == {"crm", "gateway", "providers"}, sorted(shipped)


def test_the_eval_zip_carries_the_user_settings_handler() -> None:
    members = _zip_members("eval_trigger.zip")
    assert "src/crm/handlers/user_settings.py" in members
    assert "src/crm/user_settings.py" in members
    assert "config/models.yaml" in members


#: A Python Lambda runtime literal in Terraform: `runtime = "python3.13"` or a variable default.
_TF_PYTHON_RUNTIME: Final = re.compile(r'^\s*(?:runtime|default)\s*=\s*"python(\d+\.\d+)"', re.M)


def test_every_python_runtime_names_the_version_packaging_and_the_gate_target() -> None:
    """Infra runtimes, the packager's `--python-version`, the zip gate's target and the ENI smoke's
    throwaway function all name one Python minor version."""
    infra_versions = {
        match
        for path in sorted((REPO_ROOT / "infra").rglob("*.tf"))
        if ".terraform" not in path.parts
        for match in _TF_PYTHON_RUNTIME.findall(path.read_text(encoding="utf-8"))
    }
    packager_versions = set(re.findall(r"--python-version (\d+\.\d+)", SCRIPT.read_text("utf-8")))
    gate_text = (REPO_ROOT / "scripts" / "check_lambda_zip_imports.py").read_text("utf-8")
    gate_match = re.search(r"^TARGET_PYTHON_VERSION = \((\d+), (\d+)\)$", gate_text, re.M)
    assert gate_match is not None
    smoke_text = (REPO_ROOT / "scripts" / "smoke" / "eni_function_code_deny.py").read_text("utf-8")
    smoke_versions = set(re.findall(r'Runtime="python(\d+\.\d+)"', smoke_text))
    target = f"{gate_match[1]}.{gate_match[2]}"
    assert infra_versions == {target}, infra_versions
    assert packager_versions == {target}, packager_versions
    assert smoke_versions == {target}, smoke_versions
    assert target == "3.13"
