# SPDX-License-Identifier: MIT
"""The AgentCore Runtime's config surface, asserted in both directions.

The set side is the `module "agentcore"` map in `infra/main.tf` that the
`aws_bedrockagentcore_agent_runtime` resource consumes; the read side is every `os.environ` access
in the transitive first-party import closure of `agents.runtime_server`. Every read in the closure
has a default, so `AGENT_PROVIDER` gets an explicit setter cell. AgentCore platform-injected
variables are not modelled; the gate covers declared variables only.
"""

from __future__ import annotations

import ast
import collections
import re
from pathlib import Path

import pytest
from scripts.hcl_text import blank_comments

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROOT_MAIN_TF = _REPO_ROOT / "infra" / "main.tf"
_AGENTCORE_RUNTIME_TF = _REPO_ROOT / "infra" / "modules" / "agentcore" / "runtime.tf"
_SRC = _REPO_ROOT / "src"

#: The container entrypoint whose import closure defines this gate's scope.
_ENTRYPOINT_MODULE = "agents.runtime_server"

#: One known member of each side, asserted present before any set arithmetic. Neither is the subject
#: of another cell, so the control and the checks fail independently.
_SENTINEL_SET_VARIABLE = "ACTION_LAMBDA_PREFIX"
_SENTINEL_READ_VARIABLE = "CRM_TABLE_PREFIX"

#: The variable whose setter `test_the_iac_sets_a_real_provider_and_not_the_stub_default` asserts.
_PROVIDER_VARIABLE = "AGENT_PROVIDER"

#: The provider family name that means "no model call at all" — `runtime_server._DEFAULT_PROVIDER`.
_STUB_PROVIDER = "stub"

#: Variables the IaC sets on the Runtime that nothing under `src/` reads; the list is exhaustive.
_SET_BUT_UNREAD_ACCEPTED = frozenset({"API_BASE_URL", "COGNITO_USER_POOL_ID"})

#: `KEY = ...` at the start of a line inside an HCL map body.
_HCL_MAP_KEY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def _blank_literals(text: str) -> str:
    """`text` with comments blanked and length preserved, so offsets index the original."""
    blanked = blank_comments(text)
    assert len(blanked) == len(text), "comment blanking must preserve offsets"
    return blanked


def _brace_balanced_body(text: str, index: int) -> str:
    """The brace-balanced body of the block whose `{` is at or after `index`.

    Braces are counted on a comment-blanked copy, so a brace inside a comment is ignored.
    """
    blanked = _blank_literals(text)
    start = blanked.index("{", index)
    depth = 0
    for position in range(start, len(blanked)):
        if blanked[position] == "{":
            depth += 1
        elif blanked[position] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : position]
    raise AssertionError(f"unbalanced braces from offset {index}")


def runtime_environment_map_variable() -> str:
    """Return the module input whose value becomes the Runtime's `environment_variables`.

    Read from `environment_variables = var.<name>` on the `aws_bedrockagentcore_agent_runtime`
    resource; raises AssertionError if the resource or the wiring is absent.
    """
    text = _AGENTCORE_RUNTIME_TF.read_text(encoding="utf-8")
    blanked = _blank_literals(text)
    resource = re.search(
        r'^resource\s+"aws_bedrockagentcore_agent_runtime"\s+"\w+"\s*\{', blanked, re.MULTILINE
    )
    if resource is None:
        raise AssertionError(
            f"no `aws_bedrockagentcore_agent_runtime` resource in {_AGENTCORE_RUNTIME_TF}; this gate "
            "cannot locate the Runtime and must not report an empty surface as a clean one"
        )
    body = _blank_literals(_brace_balanced_body(text, resource.start()))
    wiring = re.search(r"^\s*environment_variables\s*=\s*var\.(\w+)\s*$", body, re.MULTILINE)
    if wiring is None:
        raise AssertionError(
            "the Runtime resource does not assign `environment_variables` from a module variable, so "
            "whatever the root module passes reaches nothing and this gate has no subject"
        )
    return wiring.group(1)


def runtime_environment_variables() -> dict[str, str]:
    """Return the `NAME -> value expression` map the IaC sets on the Runtime.

    Locates the root's `module "agentcore"` block first and the map second, keyed by the input name
    from `runtime_environment_map_variable`. Raises AssertionError if either is absent.
    """
    attribute = runtime_environment_map_variable()
    text = _ROOT_MAIN_TF.read_text(encoding="utf-8")
    blanked = _blank_literals(text)
    match = re.search(r'^module\s+"agentcore"\s*\{', blanked, re.MULTILINE)
    if match is None:
        raise AssertionError(
            f'no `module "agentcore"` block in {_ROOT_MAIN_TF}; this gate cannot locate the '
            "Runtime's configuration and must not report an empty surface as a clean one"
        )
    module_body = _brace_balanced_body(text, match.start())
    inner = _blank_literals(module_body)
    env_match = re.search(rf"^\s*{re.escape(attribute)}\s*=\s*\{{", inner, re.MULTILINE)
    if env_match is None:
        raise AssertionError(
            f'`module "agentcore"` declares no `{attribute}` map; the Runtime would receive no '
            "configuration at all, which this gate must report rather than treat as an empty set"
        )
    map_body = _brace_balanced_body(module_body, env_match.start())
    raw_lines = map_body.split("\n")
    names: dict[str, str] = {}
    for index, line in enumerate(_blank_literals(map_body).split("\n")):
        key = _HCL_MAP_KEY.match(line)
        if key is not None:
            names[key.group(1)] = raw_lines[index].split("=", 1)[1].strip()
    return names


def runtime_import_closure() -> list[Path]:
    """Return every first-party source file the container entrypoint transitively imports."""
    packages = {
        entry.name
        for entry in _SRC.iterdir()
        if entry.is_dir() and (entry / "__init__.py").exists()
    }

    def resolve(module: str) -> Path | None:
        direct = _SRC / (module.replace(".", "/") + ".py")
        if direct.exists():
            return direct
        package = _SRC / module.replace(".", "/") / "__init__.py"
        return package if package.exists() else None

    seen: set[str] = set()
    queue = collections.deque([_ENTRYPOINT_MODULE])
    while queue:
        module = queue.popleft()
        if module in seen:
            continue
        seen.add(module)
        path = resolve(module)
        if path is None:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                queue.extend(
                    alias.name for alias in node.names if alias.name.split(".")[0] in packages
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.level == 0
                and node.module.split(".")[0] in packages
            ):
                queue.append(node.module)
                queue.extend(f"{node.module}.{alias.name}" for alias in node.names)
    resolved = {resolve(module) for module in seen}
    return sorted(path for path in resolved if path is not None)


def runtime_environment_reads() -> tuple[dict[str, bool], list[str]]:
    """Return `NAME -> read_has_a_default` for the closure, plus any unresolvable accesses.

    Covers `os.environ.get(...)` and `os.environ[...]`, keyed by a string literal or by a
    module-level string constant.
    """
    reads: dict[str, bool] = {}
    unresolved: list[str] = []
    for path in runtime_import_closure():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants: dict[str, str] = {}
        for statement in tree.body:
            target: str | None = None
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                target = statement.target.id
            elif (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                target = statement.targets[0].id
            else:
                continue
            assigned = statement.value
            if (
                target is not None
                and isinstance(assigned, ast.Constant)
                and isinstance(assigned.value, str)
            ):
                constants[target] = assigned.value

        for node in ast.walk(tree):
            key: ast.expr | None = None
            has_default = False
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
                and node.args
            ):
                key = node.args[0]
                has_default = len(node.args) > 1
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
            ):
                key = node.slice
            if key is None:
                continue
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                name = key.value
            elif isinstance(key, ast.Name) and key.id in constants:
                name = constants[key.id]
            else:
                unresolved.append(f"{path.relative_to(_REPO_ROOT)}:{key.lineno}")
                continue
            # A variable read in two places is optional only if EVERY read supplies a default.
            reads[name] = has_default and reads.get(name, True)
    return reads, unresolved


# ------------------------------------------------------------------------------------------------
# The positive control.
# ------------------------------------------------------------------------------------------------


def test_both_parsers_find_a_known_member() -> None:
    """Neither side may be silently empty, and neither sentinel is incidental."""
    set_names = runtime_environment_variables()
    read_names, _ = runtime_environment_reads()

    assert _SENTINEL_SET_VARIABLE in set_names, (
        f"the Terraform parser did not find {_SENTINEL_SET_VARIABLE}; it found {sorted(set_names)}"
    )
    assert _SENTINEL_READ_VARIABLE in read_names, (
        f"the source parser did not find {_SENTINEL_READ_VARIABLE}; it found {sorted(read_names)}"
    )
    assert len(set_names) >= 3 and len(read_names) >= 3


# ------------------------------------------------------------------------------------------------
# AGENT_PROVIDER is set, and not to the stub.
# ------------------------------------------------------------------------------------------------


def test_the_iac_sets_a_real_provider_and_not_the_stub_default() -> None:
    """The IaC sets `AGENT_PROVIDER` on the Runtime to something other than its `stub` default."""
    variables = runtime_environment_variables()

    assert _PROVIDER_VARIABLE in variables, (
        f'`module "agentcore"` does not set {_PROVIDER_VARIABLE}, so the Runtime falls back to '
        f"its {_STUB_PROVIDER!r} default and serves synthetic chat responses. Every other "
        "check in this file would still pass, which is why this one exists."
    )
    value = variables[_PROVIDER_VARIABLE].strip().strip('"').lower()
    assert value != _STUB_PROVIDER, (
        f"{_PROVIDER_VARIABLE} is set to {value!r}, which is the no-model-call provider; setting "
        "it explicitly to the stub is the same outcome as not setting it at all"
    )


def test_the_module_wires_the_map_through_to_the_resource() -> None:
    """The root module passes the map input that the Runtime resource consumes."""
    attribute = runtime_environment_map_variable()

    assert attribute, "the resource names no module variable for its environment_variables"
    root = _blank_literals(_ROOT_MAIN_TF.read_text(encoding="utf-8"))
    assert re.search(rf"^\s*{re.escape(attribute)}\s*=", root, re.MULTILINE), (
        f"the resource consumes `var.{attribute}` but the root module never passes it, so the Runtime "
        "is configured by the variable's default rather than by anything this gate can read"
    )


# ------------------------------------------------------------------------------------------------
# The two directions.
# ------------------------------------------------------------------------------------------------


def test_every_environment_access_in_the_closure_is_resolvable() -> None:
    """Every `os.environ` access in the closure has a resolvable key."""
    _, unresolved = runtime_environment_reads()

    assert not unresolved, (
        f"unresolvable os.environ accesses at {unresolved}; a variable reached through a computed key "
        "is invisible to both directions below"
    )


def test_no_mandatory_read_is_left_unset() -> None:
    """Every read without a default is set by the IaC; today there are no such reads."""
    reads, _ = runtime_environment_reads()
    variables = runtime_environment_variables()
    mandatory = {name for name, has_default in reads.items() if not has_default}

    unset = sorted(mandatory - set(variables))
    assert not unset, f"read without a default but never set by the IaC: {unset}"

    assert not mandatory, (
        "a read without a default now exists, so this cell is no longer vacuous — update this "
        f"docstring and the module docstring's claim that every read is optional. Mandatory: "
        f"{sorted(mandatory)}"
    )


def test_nothing_set_by_the_iac_goes_unread() -> None:
    """The set-but-unread variables equal `_SET_BUT_UNREAD_ACCEPTED` exactly."""
    variables = set(runtime_environment_variables())
    reads, _ = runtime_environment_reads()

    unread = variables - set(reads)
    assert unread == set(_SET_BUT_UNREAD_ACCEPTED), (
        f"set-but-unread on the Runtime is {sorted(unread)}, expected exactly "
        f"{sorted(_SET_BUT_UNREAD_ACCEPTED)}. A new name here is a variable the IaC sets that no code "
        "in the container's import closure reads; a name that disappeared means the discrepancy was "
        "resolved and should be dropped from _SET_BUT_UNREAD_ACCEPTED."
    )


def test_the_accepted_unread_variables_really_are_unread_across_all_of_src() -> None:
    """No file under `src/` mentions any name in `_SET_BUT_UNREAD_ACCEPTED`."""
    haystack = "\n".join(
        path.read_text(encoding="utf-8") for path in _SRC.rglob("*.py") if path.is_file()
    )
    for name in sorted(_SET_BUT_UNREAD_ACCEPTED):
        assert name not in haystack, (
            f"{name} IS referenced somewhere under src/, so accepting it as unread is wrong; find the "
            "reader and either widen the closure or drop it from _SET_BUT_UNREAD_ACCEPTED"
        )


# ------------------------------------------------------------------------------------------------
# The scope, pinned so a change to it is visible.
# ------------------------------------------------------------------------------------------------


#: The `eval` modules the container reaches for its `eval_turn` mode; none reads the
#: environment. Exact, so a new eval module entering the closure reds the scope cell.
_EVAL_MODULES_IN_CLOSURE = frozenset(
    {
        "eval/fault_injection.py",
        "eval/live.py",
        "eval/metrics.py",
        "eval/pricing.py",
        "eval/scoring.py",
        "eval/tenancy.py",
        "eval/turn_observation.py",
    }
)


def test_the_scope_is_the_measured_closure_and_not_a_hand_picked_set() -> None:
    """The closure spans `agents`, `crm`, `providers` and exactly the allowed `eval` modules only."""
    closure = runtime_import_closure()
    packages = {path.relative_to(_SRC).parts[0] for path in closure}
    eval_modules = {
        str(path.relative_to(_SRC)) for path in closure if path.relative_to(_SRC).parts[0] == "eval"
    }

    assert {"agents", "crm", "providers"} <= packages, (
        f"the container's import closure no longer spans the three packages it did; found {packages}"
    )
    assert "gateway" not in packages, (
        "the closure now reaches `gateway`, which sets variables this gate does not expect; "
        "widening the scope needs the two-direction expectations revisited"
    )
    assert eval_modules == _EVAL_MODULES_IN_CLOSURE, (
        f"the closure's eval modules changed: {sorted(eval_modules ^ _EVAL_MODULES_IN_CLOSURE)}; "
        "a module entering the closure needs its environment reads revisited"
    )


def test_the_closure_reaches_a_module_outside_the_agents_package() -> None:
    """The closure includes `crm/handlers/_common.py`, which reads `CRM_TABLE_PREFIX`."""
    closure = {str(path.relative_to(_SRC)) for path in runtime_import_closure()}

    assert "crm/handlers/_common.py" in closure, (
        "the closure no longer reaches crm/handlers/_common.py; the scope claim in this module's "
        "docstring needs re-measuring"
    )


# ------------------------------------------------------------------------------------------------
# The parsers' own failure modes.
# ------------------------------------------------------------------------------------------------


def test_the_terraform_parser_fails_loudly_when_the_module_block_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Terraform parser raises when `module "agentcore"` is absent."""
    stand_in = tmp_path / "main.tf"
    stand_in.write_text(
        'module "something_else" {\n  agent_runtime_environment_variables = {\n    X = "1"\n  }\n}\n'
    )
    monkeypatch.setattr("tests.unit.test_runtime_config_surface._ROOT_MAIN_TF", stand_in)

    with pytest.raises(AssertionError, match='no `module "agentcore"` block'):
        runtime_environment_variables()


def test_the_terraform_parser_fails_loudly_when_the_map_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Terraform parser raises when the module call carries no environment map."""
    stand_in = tmp_path / "main.tf"
    stand_in.write_text('module "agentcore" {\n  image_tag = "x"\n}\n')
    monkeypatch.setattr("tests.unit.test_runtime_config_surface._ROOT_MAIN_TF", stand_in)

    with pytest.raises(AssertionError, match="declares no `.*` map"):
        runtime_environment_variables()


def test_the_parser_reads_the_agentcore_map_and_not_a_neighbouring_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The parser reads the `agentcore` module's map and ignores a decoy module placed before it."""
    stand_in = tmp_path / "main.tf"
    stand_in.write_text(
        'module "decoy" {\n'
        "  agent_runtime_environment_variables = {\n"
        '    DECOY_ONLY = "1"\n'
        "  }\n"
        "}\n"
        'module "agentcore" {\n'
        "  agent_runtime_environment_variables = {\n"
        '    AGENT_PROVIDER = "anthropic"\n'
        '    CRM_TABLE_PREFIX = "p"\n'
        '    REAL_ONE = "2"\n'
        "  }\n"
        "}\n"
    )
    monkeypatch.setattr("tests.unit.test_runtime_config_surface._ROOT_MAIN_TF", stand_in)

    names = runtime_environment_variables()

    assert "DECOY_ONLY" not in names
    assert {"AGENT_PROVIDER", "CRM_TABLE_PREFIX", "REAL_ONE"} == set(names)


def test_a_commented_out_variable_is_not_counted_as_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A commented-out assignment is not counted as set."""
    stand_in = tmp_path / "main.tf"
    stand_in.write_text(
        'module "agentcore" {\n'
        "  agent_runtime_environment_variables = {\n"
        '    AGENT_PROVIDER = "anthropic"\n'
        '    # COMMENTED_OUT = "1"\n'
        '    CRM_TABLE_PREFIX = "p"\n'
        "  }\n"
        "}\n"
    )
    monkeypatch.setattr("tests.unit.test_runtime_config_surface._ROOT_MAIN_TF", stand_in)

    assert "COMMENTED_OUT" not in runtime_environment_variables()
