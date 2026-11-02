# SPDX-License-Identifier: MIT
"""Every Terraform output a smoke check reads is declared at the root of `infra/`.

"The root" is every `infra/*.tf`, not only `outputs.tf`: a root output may live beside the
resources it exports (e.g. `eval_state_machine_arn` in `infra/eval_sfn.tf`). Module outputs under
`infra/modules/` are not root outputs and do not count.

The read names are discovered from the `scripts/smoke/` modules themselves. Only the read direction
is asserted: root outputs that no smoke check reads (for example ones `scripts/deploy.sh` uses) are
allowed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_DIR = REPO_ROOT / "infra"
SMOKE_DIR = REPO_ROOT / "scripts" / "smoke"

#: The `outputs` accessor methods whose first literal argument names a Terraform output.
_OUTPUT_ACCESSORS = frozenset({"require", "require_str", "require_mapping", "get"})


def _declared_root_outputs(infra_dir: Path = INFRA_DIR) -> set[str]:
    """Every `output "<name>"` declared in a root `infra_dir/*.tf` file."""
    declared: set[str] = set()
    for path in sorted(infra_dir.glob("*.tf")):
        declared.update(
            re.findall(r'^output\s+"([^"]+)"', path.read_text(encoding="utf-8"), re.MULTILINE)
        )
    return declared


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "..."` and `NAME: Final[str] = "..."` assignments."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            constants[target.id] = value.value
    return constants


def _outputs_read_by(path: Path) -> set[str]:
    """Return every output name `path` reads through a literal or a module-level string constant."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = _module_string_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _OUTPUT_ACCESSORS or not node.args:
            continue
        # The receiver must be an `outputs` attribute or name; a plain dict `.get("x")` is ignored.
        receiver = node.func.value
        receiver_name = (
            receiver.attr
            if isinstance(receiver, ast.Attribute)
            else receiver.id
            if isinstance(receiver, ast.Name)
            else ""
        )
        if receiver_name != "outputs":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
        elif isinstance(first, ast.Name) and first.id in constants:
            names.add(constants[first.id])
    return names


_SMOKE_MODULES = sorted(p for p in SMOKE_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", _SMOKE_MODULES, ids=lambda p: p.name)
def test_every_output_a_smoke_check_reads_is_declared_at_the_root(path: Path) -> None:
    declared = _declared_root_outputs()
    missing = sorted(_outputs_read_by(path) - declared)
    assert missing == [], (
        f"{path.relative_to(REPO_ROOT)} reads Terraform output(s) {missing} that "
        f"no root {INFRA_DIR.relative_to(REPO_ROOT)}/*.tf declares. The check will report a `fail` "
        "naming the output against every deployed stack until the root exports it."
    )


def test_the_discovery_is_not_vacuous() -> None:
    """The discovery finds at least ten output names, including two the relay-IAM check reads.

    `scripts/smoke/access_log_delivery.py` reads its output names from a dataclass field, so it
    contributes nothing to this set.
    """
    found: set[str] = set()
    for path in _SMOKE_MODULES:
        found |= _outputs_read_by(path)
    assert len(found) >= 10, sorted(found)
    assert "agent_relay_log_group_name" in found
    assert "agentcore_runtime_role_arn" in found
    # `public_exposure.py` reads this one only through its constant `KNOWN_PRESENT_OUTPUT`.
    assert "agent_relay_function_name" in _outputs_read_by(SMOKE_DIR / "public_exposure.py")


def test_the_detection_finds_a_planted_undeclared_output() -> None:
    """A planted literal output name that no root output declares is found and is not declared."""
    planted = ast.parse('ctx.outputs.require_str("no_such_output_exists_anywhere")\n')
    names: set[str] = set()
    for node in ast.walk(planted):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "require_str"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            names.add(str(node.args[0].value))
    assert names == {"no_such_output_exists_anywhere"}
    assert not (names & _declared_root_outputs())


def _missing_by_module(infra_dir: Path) -> dict[str, list[str]]:
    declared = _declared_root_outputs(infra_dir)
    return {
        path.name: missing
        for path in _SMOKE_MODULES
        if (missing := sorted(_outputs_read_by(path) - declared))
    }


def _copy_root_tf(target: Path) -> None:
    """Copy only the root `infra/*.tf` set, the subject `_declared_root_outputs` reads."""
    target.mkdir()
    for path in INFRA_DIR.glob("*.tf"):
        (target / path.name).write_bytes(path.read_bytes())


def test_an_output_outside_outputs_tf_counts_and_its_removal_reds_only_its_reader(
    tmp_path: Path,
) -> None:
    """Scratch copy: unmutated passes; removing `eval_state_machine_arn` reds exactly its reader."""
    copy = tmp_path / "infra"
    _copy_root_tf(copy)
    assert _missing_by_module(copy) == {}, "the unmutated copy must pass"

    source = copy / "eval_sfn.tf"
    text = source.read_text(encoding="utf-8")
    block = re.search(r'(?ms)^output "eval_state_machine_arn" \{\n.*?^\}\n', text)
    assert block is not None, "eval_sfn.tf no longer declares eval_state_machine_arn"
    source.write_text(text.replace(block.group(0), ""), encoding="utf-8")
    readers = sorted(
        p.name for p in _SMOKE_MODULES if "eval_state_machine_arn" in _outputs_read_by(p)
    )
    assert readers, "no smoke module reads eval_state_machine_arn, so the control proves nothing"
    assert _missing_by_module(copy) == {name: ["eval_state_machine_arn"] for name in readers}


def test_a_module_output_is_not_a_root_output(tmp_path: Path) -> None:
    copy = tmp_path / "infra"
    _copy_root_tf(copy)
    (copy / "modules" / "x").mkdir(parents=True)
    (copy / "modules" / "x" / "outputs.tf").write_text(
        'output "only_inside_a_module" {\n  value = 1\n}\n', encoding="utf-8"
    )
    assert "only_inside_a_module" not in _declared_root_outputs(copy)
