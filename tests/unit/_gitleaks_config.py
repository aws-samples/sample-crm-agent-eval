# SPDX-License-Identifier: MIT
"""The optional, untracked `.gitleaks.toml` rule set, for tests that assert on it.

`.gitleaks.toml` is local-only: present, gitleaks loads it; absent, gitleaks uses its built-in
rules. Tests about the local rule set call `require_local_rule_set()`, which skips when the file is
absent and fails instead under `CRM_REQUIRE_GITLEAKS_CONFIG=1`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_RULE_SET = REPO_ROOT / ".gitleaks.toml"

REQUIRE_GITLEAKS_CONFIG_ENV_VAR = "CRM_REQUIRE_GITLEAKS_CONFIG"


def require_local_rule_set() -> Path:
    """Return the local rule set's path; skip, or fail under the require flag, when it is absent."""
    if LOCAL_RULE_SET.is_file():
        return LOCAL_RULE_SET
    reason = f"no local gitleaks rule set at {LOCAL_RULE_SET}; it is untracked and optional"
    if os.environ.get(REQUIRE_GITLEAKS_CONFIG_ENV_VAR) == "1":
        pytest.fail(f"{reason}, and {REQUIRE_GITLEAKS_CONFIG_ENV_VAR}=1 forbids a skip")
    pytest.skip(reason)


def install_local_rule_set(root: Path) -> None:
    """Copy the local rule set into `root` when it exists; otherwise `root` uses built-in rules."""
    if LOCAL_RULE_SET.is_file():
        (root / ".gitleaks.toml").write_text(
            LOCAL_RULE_SET.read_text(encoding="utf-8"), encoding="utf-8"
        )
