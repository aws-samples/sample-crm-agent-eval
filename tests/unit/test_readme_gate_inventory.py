# SPDX-License-Identifier: MIT
"""Every `.PHONY` Makefile target and every `run_gates.py` gate is named in docs/development.md.

The inventory is derived from the `Makefile` and from `GATES`, and the derivation is asserted equal
to the registry.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE_PATH = REPO_ROOT / "Makefile"
RUN_GATES_PATH = REPO_ROOT / "scripts" / "run_gates.py"
DEV_DOC_PATH = REPO_ROOT / "docs" / "development.md"


def _parse_make_targets(makefile_text: str) -> dict[str, list[str]]:
    """Return target name -> recipe lines, parsed from a GNU Makefile."""
    targets: dict[str, list[str]] = {}
    current: str | None = None
    header_pattern = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:(?!=)")
    for line in makefile_text.splitlines():
        if line.startswith("\t") and current is not None:
            targets[current].append(line[1:])
            continue
        header_match = header_pattern.match(line)
        if header_match:
            current = header_match.group(1)
            targets.setdefault(current, [])
            continue
        if line.strip() == "":
            current = None
    return targets


def _phony_target_names(makefile_text: str) -> list[str]:
    """Return every target named on a `.PHONY:` line, in declared order."""
    names: list[str] = []
    for line in makefile_text.splitlines():
        match = re.match(r"^\.PHONY:\s*(.+)$", line.strip())
        if match:
            names.extend(match.group(1).split())
    return names


def _registered_gate_names() -> list[str]:
    """Return every gate name in `scripts/run_gates.py`'s `GATES`, read by importing the module.

    Importing rather than parsing also sees registrations preceded by a comment. Importing makes no
    AWS call.
    """
    spec = importlib.util.spec_from_file_location("run_gates_for_inventory", RUN_GATES_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {RUN_GATES_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_gates_for_inventory"] = module
    spec.loader.exec_module(module)
    return [str(name) for name, _argv in module.GATES]


def _all_documented_gate_names() -> set[str]:
    """The complete, from-source gate inventory the development doc is held to."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    phony = set(_phony_target_names(makefile_text))
    internal = set(_registered_gate_names())
    return phony | internal


def _assert_every_gate_documented(doc_text: str, gate_names: set[str]) -> None:
    """Raise ``AssertionError`` naming every gate in `gate_names` absent from `doc_text`.

    A name counts as documented in its literal form or with `_` and `-` swapped, so either the
    `make` target name or the script name satisfies it.
    """
    missing = set()
    for name in gate_names:
        alt = name.replace("_", "-")
        alt2 = name.replace("-", "_")
        if name in doc_text or alt in doc_text or alt2 in doc_text:
            continue
        missing.add(name)
    assert not missing, f"gate(s) not documented in docs/development.md: {missing}"


# ---------------------------------------------------------------------------
# 1. The derivation itself.
# ---------------------------------------------------------------------------


def test_makefile_phony_targets_include_the_known_top_level_gates() -> None:
    """The `.PHONY` parser finds at least the long-standing top-level targets."""
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    phony = set(_phony_target_names(makefile_text))
    expected = {
        "typecheck",
        "lint",
        "format-check",
        "test",
        "test-stub",
        "test-integration",
        "check-cost-withheld",
        "verify-gates",
        "verify-shellcheck",
        "verify",
        "verify-deploy",
    }
    assert expected <= phony, f"expected .PHONY targets missing from Makefile: {expected - phony}"


def test_run_gates_internal_names_include_the_known_offline_gates() -> None:
    """The `GATES` derivation finds at least the long-standing gates; completeness is tested below."""
    internal = set(_registered_gate_names())
    expected = {
        "check_no_account_id",
        "check_no_vendor_keys",
        "check_log_redaction",
        "check_license_headers",
        "check_toolspec_parity",
        "check_pin_drift",
        "check_cost_withheld",
        "verify_fixtures",
        "check_plan_security_baseline",
    }
    assert expected <= internal, f"expected run_gates.py entries missing: {expected - internal}"


def test_the_derivation_sees_every_registered_gate() -> None:
    """The derived gate set equals every gate-name literal registered in `run_gates.py`."""
    derived = _registered_gate_names()
    registry_text = RUN_GATES_PATH.read_text(encoding="utf-8")

    assert len(derived) > 20, f"derivation returned only {len(derived)} gates; it is broken"
    assert len(derived) == len(set(derived)), f"a gate is registered twice: {derived}"

    for name in derived:
        assert f'"{name}"' in registry_text, f"{name} is not a literal in run_gates.py"

    literals = set(
        re.findall(
            r'^\s*"(check_[a-z0-9_]+|verify_[a-z0-9_]+)"\s*,\s*$', registry_text, re.MULTILINE
        )
    )
    literals |= set(re.findall(r'\(\s*"([a-z0-9_]+)"\s*,\s*\[', registry_text))
    undiscovered = literals - set(derived)
    assert not undiscovered, (
        f"these gate names appear in run_gates.py but the derivation missed them: {undiscovered}"
    )


def test_a_comment_registered_gate_is_discovered() -> None:
    """Gates whose registration is preceded by a comment are discovered."""
    derived = set(_registered_gate_names())
    comment_registered = {
        "check_vpc_endpoint_coverage",
        "check_identity_channel_parity",
        "check_canary_tenant_empty",
        "verify_terraform_plan_with_kms_absent",
    }
    missing = comment_registered - derived
    assert not missing, (
        f"gate(s) registered with a preceding comment were not discovered: {missing}"
    )


def test_every_registered_gate_names_a_script_that_exists() -> None:
    """Every `scripts/...` path in a registered gate's argv exists in the tree."""
    spec = importlib.util.spec_from_file_location("run_gates_for_argv", RUN_GATES_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_gates_for_argv"] = module
    spec.loader.exec_module(module)

    missing: list[str] = []
    for name, argv in module.GATES:
        for token in argv:
            if (
                isinstance(token, str)
                and token.startswith("scripts/")
                and not (REPO_ROOT / token).exists()
            ):
                missing.append(f"{name} -> {token}")
    assert not missing, f"registered gate(s) name a script that does not exist: {missing}"


# ---------------------------------------------------------------------------
# 2. Every derived gate is documented in docs/development.md.
# ---------------------------------------------------------------------------


def test_every_makefile_and_run_gates_gate_is_documented_in_the_dev_doc() -> None:
    doc_text = DEV_DOC_PATH.read_text(encoding="utf-8")
    gate_names = _all_documented_gate_names()
    assert gate_names, "gate derivation produced an empty set — the parser itself is broken"
    _assert_every_gate_documented(doc_text, gate_names)


# ---------------------------------------------------------------------------
# 3. Non-vacuity.
# ---------------------------------------------------------------------------


def test_removing_a_documented_gate_from_the_dev_doc_fails_the_check() -> None:
    """Deleting every line that mentions `test-stub` makes the check fail and name `test-stub`."""
    doc_text = DEV_DOC_PATH.read_text(encoding="utf-8")
    stripped_lines = [
        line
        for line in doc_text.splitlines()
        if "test-stub" not in line and "test_stub" not in line
    ]
    stripped_doc_text = "\n".join(stripped_lines)

    gate_names = _all_documented_gate_names()
    assert "test-stub" in gate_names, "fixture assumption broken: test-stub is no longer a gate"

    with pytest.raises(AssertionError, match=r"test-stub"):
        _assert_every_gate_documented(stripped_doc_text, gate_names)

    _assert_every_gate_documented(doc_text, gate_names)


def test_an_entirely_undocumented_synthetic_gate_is_also_caught() -> None:
    """A synthetic gate name absent from the document is reported missing."""
    doc_text = DEV_DOC_PATH.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match=r"totally_synthetic_gate_name_zz"):
        _assert_every_gate_documented(
            doc_text, {"totally_synthetic_gate_name_zz", "check_no_account_id"}
        )
