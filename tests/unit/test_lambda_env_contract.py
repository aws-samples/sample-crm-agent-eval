# SPDX-License-Identifier: MIT
"""Seams 7 and 8: each new Lambda's environment, asserted in both directions, and its IaC wiring.

The set side is the `environment_variables` map of each `module "lambda_action_<name>"` block,
parsed from HCL. The read side is every `os.environ` access in the transitive first-party import
closure of the module the same block names as its `handler`, including reads through a local alias
of `os.environ` (`source = os.environ if env is None else env`). A read that is not set must be in
that handler's exhaustive accepted list; a set variable nothing reads fails, apart from the
Lambda-runtime variables in :data:`_RUNTIME_CONSUMED`.

Seam 7 cells pin the value expressions the consumers depend on: the state machine ARN on
`eval_trigger`, the Runtime ARN on `sweep_task`/`chat_target`, and every Terraform output a smoke
script requires.
"""

from __future__ import annotations

import ast
import collections
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from scripts.hcl_text import blank_comments

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INFRA = _REPO_ROOT / "infra"
_SRC = _REPO_ROOT / "src"


@dataclass(frozen=True)
class _Function:
    """One new or changed Lambda: where its module block lives and what it may read unset."""

    module: str
    tf_file: str
    #: Variables the closure reads that the IaC does not set, each optional with a default or
    #: supplied by the Lambda platform. Exhaustive: a new unset read fails until added here.
    read_unset_accepted: frozenset[str] = frozenset()


#: Supplied by the Lambda platform on every function, never by the module's map.
_PLATFORM_SUPPLIED: frozenset[str] = frozenset({"AWS_REGION"})

#: Set on the function for the Python runtime itself, not read by first-party code.
_RUNTIME_CONSUMED: frozenset[str] = frozenset({"PYTHONPATH"})

#: Local-CLI or optional reads in `eval_trigger`'s closure (`eval.harness`, `eval.verification`,
#: `crm.config`), each read with a default and unused on the Lambda path.
_EVAL_TRIGGER_OPTIONAL: frozenset[str] = frozenset(
    {
        "ACTION_LAMBDA_PREFIX",
        "AGENT_PROVIDER",
        "AWS_CONFIG_FILE",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "EVAL_SUBSTRATE",
        "LOG_LEVEL",
    }
)

#: The `eval.sfn_handlers` closure (via `eval.harness`/`eval.runs`) also reads `eval_trigger`'s
#: optional set, and `eval.runs` reads `EVAL_STATE_MACHINE_ARN` with a `None` fallback.
_SWEEP_OPTIONAL: frozenset[str] = _EVAL_TRIGGER_OPTIONAL | {"EVAL_STATE_MACHINE_ARN"}

#: `eval.sfn_handlers` serves both sweep functions, so each closure also contains the other's
#: config class; `test_each_sweep_config_loads_from_exactly_its_iac_names` pins which it uses.
_SWEEP_TASK_CONFIG = frozenset({"AGENT_RUNTIME_ARN", "AGENT_RUNTIME_QUALIFIER"})
_SWEEP_CONTROL_CONFIG = frozenset({"EVAL_ARTIFACTS_BUCKET", "EVAL_ARTIFACTS_PREFIX"})

#: `crm.envelope` reads the API client-allowlist parameter name only on the API Gateway channel, which
#: these functions never serve.
_NOT_API_CHANNEL: frozenset[str] = frozenset({"CRM_API_ALLOWED_CLIENT_IDS_PARAMETER"})

_FUNCTIONS: tuple[_Function, ...] = (
    _Function(
        "lambda_action_sweep_control",
        "eval_sfn.tf",
        _SWEEP_OPTIONAL | _SWEEP_TASK_CONFIG | _NOT_API_CHANNEL,
    ),
    _Function(
        "lambda_action_sweep_task",
        "eval_sfn.tf",
        _SWEEP_OPTIONAL | _SWEEP_CONTROL_CONFIG | _NOT_API_CHANNEL,
    ),
    _Function("lambda_action_chat_target", "mcp_chat_gateway.tf", _NOT_API_CHANNEL),
    _Function("lambda_action_user_settings", "user_settings.tf"),
    _Function("lambda_action_eval_trigger", "main.tf", _EVAL_TRIGGER_OPTIONAL),
)

_HCL_MAP_KEY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.+?)\s*$")


# -- HCL ------------------------------------------------------------------------------------------


def _brace_body(text: str, index: int) -> str:
    """The brace-balanced body of the block whose `{` is at or after `index`, comments ignored."""
    blanked = blank_comments(text)
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


def _module_body(function: _Function) -> str:
    text = (_INFRA / function.tf_file).read_text(encoding="utf-8")
    match = re.search(rf'^module\s+"{function.module}"\s*\{{', blank_comments(text), re.MULTILINE)
    assert match is not None, f'no module "{function.module}" in infra/{function.tf_file}'
    return _brace_body(text, match.start())


def module_environment(function: _Function) -> dict[str, str]:
    """`NAME -> value expression` from the module's `environment_variables` map."""
    body = _module_body(function)
    match = re.search(r"^\s*environment_variables\s*=\s*\{", blank_comments(body), re.MULTILINE)
    assert match is not None, f"{function.module} sets no environment_variables map"
    names: dict[str, str] = {}
    for line in blank_comments(_brace_body(body, match.start())).split("\n"):
        entry = _HCL_MAP_KEY.match(line)
        if entry is not None:
            names[entry.group(1)] = entry.group(2)
    return names


def module_handler_module(function: _Function) -> str:
    """The Python module the block's `handler = "<module>.<function>"` names."""
    match = re.search(r'^\s*handler\s*=\s*"([\w.]+)"', _module_body(function), re.MULTILINE)
    assert match is not None, f"{function.module} declares no handler"
    return match.group(1).rsplit(".", 1)[0]


def _tf_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(_INFRA.glob("*.tf")))


def _local_value(name: str) -> str:
    match = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*$", blank_comments(_tf_text()), re.MULTILINE)
    assert match is not None, f"no local {name!r} under infra/*.tf"
    return match.group(1)


# -- Python reads ---------------------------------------------------------------------------------


def _resolve(module: str) -> Path | None:
    direct = _SRC / (module.replace(".", "/") + ".py")
    if direct.exists():
        return direct
    package = _SRC / module.replace(".", "/") / "__init__.py"
    return package if package.exists() else None


def import_closure(entry: str) -> list[Path]:
    """Every first-party source file `entry` transitively imports, function bodies included."""
    packages = {p.name for p in _SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    seen: set[str] = set()
    queue = collections.deque([entry])
    while queue:
        module = queue.popleft()
        if module in seen:
            continue
        seen.add(module)
        path = _resolve(module)
        if path is None:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                queue.extend(a.name for a in node.names if a.name.split(".")[0] in packages)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.level == 0
                and node.module.split(".")[0] in packages
            ):
                queue.append(node.module)
                queue.extend(f"{node.module}.{a.name}" for a in node.names)
    return sorted({p for p in (_resolve(m) for m in seen) if p is not None})


def _string_constants(trees: list[ast.Module]) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments across the closure, by name."""
    constants: dict[str, str] = {}
    for tree in trees:
        for statement in tree.body:
            target = None
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                target = statement.target.id
            elif (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                target = statement.targets[0].id
            value = statement.value if isinstance(statement, ast.AnnAssign | ast.Assign) else None
            if target and isinstance(value, ast.Constant) and isinstance(value.value, str):
                constants[target] = value.value
    return constants


def _is_os_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def _environ_aliases(tree: ast.Module) -> set[str]:
    """Names assigned from an expression that mentions `os.environ` (e.g. a conditional)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(_is_os_environ(n) for n in ast.walk(node.value)):
            aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return aliases


def environment_reads(entry: str) -> tuple[set[str], list[str]]:
    """Every variable name read in `entry`'s closure, plus any access whose key is unresolvable."""
    paths = import_closure(entry)
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in paths]
    constants = _string_constants(trees)
    reads: set[str] = set()
    unresolved: list[str] = []
    for path, tree in zip(paths, trees, strict=True):
        aliases = _environ_aliases(tree)

        def is_environ(node: ast.expr, aliases: set[str] = aliases) -> bool:
            return _is_os_environ(node) or (isinstance(node, ast.Name) and node.id in aliases)

        for node in ast.walk(tree):
            key: ast.expr | None = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and is_environ(node.func.value)
                and node.args
            ):
                key = node.args[0]
            elif isinstance(node, ast.Subscript) and is_environ(node.value):
                key = node.slice
            if key is None:
                continue
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                reads.add(key.value)
            elif isinstance(key, ast.Name) and key.id in constants:
                reads.add(constants[key.id])
            else:
                unresolved.append(f"{path.relative_to(_REPO_ROOT)}:{key.lineno}")
    return reads, unresolved


# -- seam 8: both directions -----------------------------------------------------------------------

_IDS = [function.module for function in _FUNCTIONS]


def test_the_parsers_find_known_members_control() -> None:
    """Neither side may be silently empty: a known set and a known read are found."""
    chat_target = _FUNCTIONS[2]
    assert "AGENT_RUNTIME_ARN" in module_environment(chat_target)
    reads, _ = environment_reads(module_handler_module(chat_target))
    assert {"AGENT_RUNTIME_ARN", "AGENT_RUNTIME_QUALIFIER", "CRM_TABLE_PREFIX"} <= reads


@pytest.mark.parametrize("function", _FUNCTIONS, ids=_IDS)
def test_the_handler_module_exists(function: _Function) -> None:
    module = module_handler_module(function)
    assert _resolve(module) is not None, f"{function.module}'s handler module {module} is absent"


@pytest.mark.parametrize("function", _FUNCTIONS, ids=_IDS)
def test_every_read_is_resolvable(function: _Function) -> None:
    _, unresolved = environment_reads(module_handler_module(function))
    assert unresolved == [], f"environment reads with a computed key: {unresolved}"


@pytest.mark.parametrize("function", _FUNCTIONS, ids=_IDS)
def test_nothing_is_read_but_unset(function: _Function) -> None:
    reads, _ = environment_reads(module_handler_module(function))
    unset = reads - set(module_environment(function)) - _PLATFORM_SUPPLIED
    assert unset == function.read_unset_accepted, (
        f"{function.module}: read but not set {sorted(unset - function.read_unset_accepted)}; "
        f"accepted but no longer read {sorted(function.read_unset_accepted - unset)}"
    )


@pytest.mark.parametrize("function", _FUNCTIONS, ids=_IDS)
def test_nothing_is_set_but_unread(function: _Function) -> None:
    reads, _ = environment_reads(module_handler_module(function))
    unread = set(module_environment(function)) - reads - _RUNTIME_CONSUMED
    assert unread == set(), f"{function.module}: set but never read {sorted(unread)}"


# -- seam 7: IaC values the consumers depend on -----------------------------------------------------


def _environment_of(module: str) -> dict[str, str]:
    return module_environment(next(f for f in _FUNCTIONS if f.module == module))


def test_eval_trigger_receives_the_state_machine_it_starts() -> None:
    from eval.runs import EVAL_STATE_MACHINE_ARN_ENV_VAR as RUNS_ENV
    from eval.trigger import EVAL_STATE_MACHINE_ARN_ENV_VAR as TRIGGER_ENV

    environment = _environment_of("lambda_action_eval_trigger")
    assert RUNS_ENV == TRIGGER_ENV
    assert environment[TRIGGER_ENV] == "local.eval_sweep_state_machine_arn"
    assert _local_value("eval_sweep_state_machine_arn").endswith(
        ':stateMachine:${local.eval_sweep_state_machine_name}"'
    )
    machine = re.search(
        r'^resource\s+"aws_sfn_state_machine"\s+"eval_sweep"\s*\{', _tf_text(), re.MULTILINE
    )
    assert machine is not None
    assert re.search(
        r"^\s*name\s*=\s*local\.eval_sweep_state_machine_name\s*$",
        _brace_body(_tf_text(), machine.start()),
        re.MULTILINE,
    )


@pytest.mark.parametrize("module", ["lambda_action_sweep_task", "lambda_action_chat_target"])
def test_runtime_callers_receive_the_runtime_output_and_the_relays_qualifier(module: str) -> None:
    environment = _environment_of(module)
    assert environment["AGENT_RUNTIME_ARN"] == "module.agentcore.agent_runtime_arn"
    assert environment["AGENT_RUNTIME_QUALIFIER"] == "local.relay_runtime_qualifier"
    outputs = (_INFRA / "modules" / "agentcore" / "outputs.tf").read_text(encoding="utf-8")
    assert re.search(r'^output\s+"agent_runtime_arn"\s*\{', outputs, re.MULTILINE)


def test_the_chat_target_config_loads_from_exactly_the_iac_names() -> None:
    from gateway.chat_target import ChatTargetConfig

    names = _environment_of("lambda_action_chat_target")
    config = ChatTargetConfig.from_env({name: f"value-of-{name}" for name in names})

    assert config.runtime_arn == "value-of-AGENT_RUNTIME_ARN"
    assert config.runtime_qualifier == "value-of-AGENT_RUNTIME_QUALIFIER"
    assert config.table_prefix == "value-of-CRM_TABLE_PREFIX"


def test_every_output_a_smoke_script_requires_is_declared() -> None:
    """Includes `mcp_chat_gateway_url`; an output renamed in IaC fails here, not at T3."""
    declared = set(re.findall(r'^output\s+"(\w+)"', _tf_text(), re.MULTILINE))
    required: dict[str, set[str]] = {}
    for path in sorted((_REPO_ROOT / "scripts" / "smoke").glob("*.py")):
        for name in re.findall(r'require_[a-z]+\(\s*"(\w+)"', path.read_text(encoding="utf-8")):
            required.setdefault(name, set()).add(path.name)
    assert "mcp_chat_gateway_url" in required
    missing = {name: sorted(files) for name, files in required.items() if name not in declared}
    assert missing == {}, f"smoke scripts require undeclared outputs: {missing}"


@pytest.mark.parametrize(
    ("module", "config_class", "required"),
    [
        ("lambda_action_sweep_control", "SweepControlConfig", _SWEEP_CONTROL_CONFIG),
        ("lambda_action_sweep_task", "SweepTaskConfig", _SWEEP_TASK_CONFIG),
    ],
)
def test_each_sweep_config_loads_from_exactly_its_iac_names(
    module: str, config_class: str, required: frozenset[str]
) -> None:
    """Each function's own config class loads from its IaC map, and needs every required name."""
    import eval.sfn_handlers as handlers

    names = _environment_of(module)
    loader = getattr(handlers, config_class).from_env
    loader({name: f"value-of-{name}" for name in names})
    assert required <= set(names)
    for missing in required:
        with pytest.raises(ValueError):
            loader({name: "v" for name in names if name != missing})
