# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_lambda_adapter_isolation.py`.

Mutations run against a scratch copy of the first-party `src/` packages and `infra/main.tf`.
"""

from __future__ import annotations

import ast
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "check_lambda_adapter_isolation.py"
ZIP_GATE_PATH = REPO_ROOT / "scripts" / "check_lambda_zip_imports.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load(GATE_PATH, "check_lambda_adapter_isolation")

#: The `src/` packages the gate reads, copied into the scratch tree.
COPIED_SRC_PACKAGES = ("agents", "crm", "eval", "gateway", "providers")
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".mypy_cache")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for package in COPIED_SRC_PACKAGES:
        shutil.copytree(
            REPO_ROOT / "src" / package, tmp_path / "src" / package, ignore=_COPY_IGNORE
        )
    (tmp_path / "infra").mkdir()
    shutil.copy2(REPO_ROOT / "infra" / "main.tf", tmp_path / "infra" / "main.tf")
    return tmp_path


def _prepend(path: Path, line: str) -> None:
    text = path.read_text(encoding="utf-8")
    head, sep, rest = text.partition("from __future__ import annotations\n")
    if sep:
        path.write_text(head + sep + line + "\n" + rest, encoding="utf-8")
    else:
        path.write_text(line + "\n" + text, encoding="utf-8")


def _violations(root: Path) -> tuple[list[str], list[str]]:
    import_time, _count = gate.check_import_time(root)
    return (import_time, gate.check_stub_literal(root))


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, (path, old)
    path.write_text(text.replace(old, new), encoding="utf-8")


# --- controls ------------------------------------------------------------------------------------


def test_the_real_tree_passes() -> None:
    completed = proc_run.run(
        [sys.executable, str(GATE_PATH)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "[pass] import_time" in completed.stdout
    assert "[pass] stub_literal" in completed.stdout


def test_the_unmutated_copy_passes(tree: Path) -> None:
    assert _violations(tree) == ([], [])


def test_the_handler_set_is_the_zip_gates_inventory_and_includes_the_eval_handlers() -> None:
    """The handler set derived from infra/*.tf equals the zip gate's inventory; nothing is pending."""
    zip_gate = _load(ZIP_GATE_PATH, "check_lambda_zip_imports_for_isolation")
    derived = set(gate.derived_handlers(REPO_ROOT))
    assert frozenset() == gate.PENDING_HANDLER_MODULES
    assert {
        "eval.sfn_handlers.sweep_control_handler",
        "eval.sfn_handlers.sweep_task_handler",
    } <= derived
    assert derived == {spec.handler for spec in zip_gate.expected_zips()}
    assert {
        "crm.handlers.eval_trigger.handler",
        "crm.handlers.capabilities.handler",
    } <= derived
    assert not any("eval_worker" in handler for handler in derived)


def test_the_real_lazy_edge_is_outside_the_import_time_cell() -> None:
    """The factory's builder-body adapter imports exist and are not on an import-time path."""
    text = (REPO_ROOT / "src" / "providers" / "factory.py").read_text(encoding="utf-8")
    assert "    from providers.anthropic_adapter import" in text
    assert "    from providers.openai_adapter import" in text
    assert gate.import_time_path(REPO_ROOT / "src", "providers.factory") is None


# --- cell import_time: each must be red ----------------------------------------------------------


@pytest.mark.parametrize("adapter", sorted(gate.ADAPTER_MODULES))
def test_a_direct_import_time_import_in_a_handler_is_red(tree: Path, adapter: str) -> None:
    _prepend(tree / "src" / "crm" / "handlers" / "get_account.py", f"import {adapter}")
    import_time, _ = _violations(tree)
    assert any(
        "'crm.handlers.get_account.handler'" in v and f"crm.handlers.get_account -> {adapter}" in v
        for v in import_time
    ), import_time


def test_a_transitive_import_time_import_through_an_intermediate_module_is_red(
    tree: Path,
) -> None:
    (tree / "src" / "crm" / "planted_intermediate.py").write_text(
        "from providers import openai_adapter\n", encoding="utf-8"
    )
    _prepend(
        tree / "src" / "crm" / "handlers" / "get_contact.py", "import crm.planted_intermediate"
    )
    import_time, _ = _violations(tree)
    assert any(
        "crm.handlers.get_contact -> crm.planted_intermediate -> providers.openai_adapter" in v
        for v in import_time
    ), import_time


def test_an_import_in_the_package_init_above_a_handler_is_red(tmp_path: Path) -> None:
    """Importing `gateway.planted` executes `gateway/__init__.py` first, even though the handler
    module itself imports nothing from its package."""
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "main.tf").write_text(
        'handler = "gateway.planted.handler"\n', encoding="utf-8"
    )
    src = tmp_path / "src"
    (src / "gateway").mkdir(parents=True)
    (src / "providers").mkdir()
    (src / "providers" / "__init__.py").write_text("", encoding="utf-8")
    (src / "providers" / "anthropic_adapter.py").write_text("", encoding="utf-8")
    (src / "gateway" / "planted.py").write_text("def handler(e, c):\n    return None\n")
    init = src / "gateway" / "__init__.py"
    init.write_text("", encoding="utf-8")
    assert gate.check_import_time(tmp_path) == ([], 1)
    init.write_text("import providers.anthropic_adapter\n", encoding="utf-8")
    import_time, _ = gate.check_import_time(tmp_path)
    assert import_time == [
        "handler 'gateway.planted.handler' reaches an adapter on its executed path: "
        "gateway -> providers.anthropic_adapter"
    ], import_time


def test_an_import_inside_a_top_level_try_is_still_import_time(tree: Path) -> None:
    _prepend(
        tree / "src" / "crm" / "handlers" / "get_account.py",
        "try:\n    import providers.anthropic_adapter\nexcept ImportError:\n    pass",
    )
    import_time, _ = _violations(tree)
    assert any("crm.handlers.get_account" in v for v in import_time), import_time


# --- cell import_time: function bodies and module scope -----------------------------------------


def test_a_module_scope_import_in_eval_trigger_reds_the_eval_trigger_handler(tree: Path) -> None:
    """A module-scope adapter import in ``eval.trigger`` reds the handler that imports it."""
    _prepend(tree / TRIGGER, "import providers.anthropic_adapter")
    import_time, _ = _violations(tree)
    for handler in ("eval_trigger",):
        assert any(
            f"'crm.handlers.{handler}.handler'" in v
            and "eval.trigger -> providers.anthropic_adapter" in v
            for v in import_time
        ), (handler, import_time)
    assert not any("capabilities" in v for v in import_time), import_time


def test_the_handler_functions_import_eval_trigger_in_their_bodies() -> None:
    """The eval trigger handler imports ``eval.trigger`` inside a function body."""
    for name in ("eval_trigger",):
        text = (REPO_ROOT / "src" / "crm" / "handlers" / f"{name}.py").read_text(encoding="utf-8")
        assert (
            "\n    from eval.trigger import" in text or "\n        from eval.trigger import" in text
        )


def test_a_function_body_import_in_the_handler_module_is_red(tree: Path) -> None:
    """An adapter import in a handler module's function body is a violation."""
    path = tree / "src" / "crm" / "handlers" / "get_account.py"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n\ndef _planted() -> None:\n    import providers.anthropic_adapter  # noqa\n",
        encoding="utf-8",
    )
    import_time, _ = _violations(tree)
    assert any(
        "crm.handlers.get_account -> providers.anthropic_adapter" in v for v in import_time
    ), import_time


def test_a_function_body_import_in_a_module_the_handler_reaches_is_not_followed(
    tree: Path,
) -> None:
    """A function-body import in a module the handler reaches is not followed."""
    path = tree / "src" / "crm" / "envelope.py"
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original + "\n\ndef _planted() -> None:\n    import providers.anthropic_adapter  # noqa\n",
        encoding="utf-8",
    )
    assert _violations(tree) == ([], [])
    # Control: `crm.envelope` is on the walk, so the same import at its module scope is seen.
    path.write_text(original + "\nimport providers.anthropic_adapter  # noqa\n", encoding="utf-8")
    import_time, _ = _violations(tree)
    assert any("crm.envelope -> providers.anthropic_adapter" in v for v in import_time), import_time


def test_a_type_checking_import_is_not_import_time(tree: Path) -> None:
    _prepend(
        tree / "src" / "crm" / "handlers" / "get_account.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import providers.openai_adapter",
    )
    assert _violations(tree) == ([], [])


def test_a_composition_with_no_python_handler_is_a_violation_not_a_pass(tree: Path) -> None:
    (tree / "infra" / "main.tf").write_text("# nothing\n", encoding="utf-8")
    import_time, _ = _violations(tree)
    for path in (tree / "infra").glob("*.tf"):
        if path.name != "main.tf":
            path.unlink()
    import_time, _ = _violations(tree)
    assert import_time == ["no first-party Python handler string found in infra"]


# --- cell stub_literal: each must be red ---------------------------------------------------------

TRIGGER = Path("src") / "eval" / "trigger.py"


def _append(path: Path, source: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + "\n\n" + source, encoding="utf-8")


def _in_submit_sweep(path: Path, body: str) -> None:
    """Insert `body` as the first statements of `submit_sweep`, after its docstring."""
    text = path.read_text(encoding="utf-8")
    start = text.index("def submit_sweep(")
    doc_open = text.index('"""', start)
    doc_close = text.index('"""', doc_open + 3) + 3
    indented = "".join(f"\n    {line}" for line in body.splitlines())
    path.write_text(text[:doc_close] + indented + text[doc_close:], encoding="utf-8")


#: Five mutations, each of which the denylist passed with rc=0.
G2R_C_1_MUTATIONS: dict[str, tuple[str, str]] = {
    "run_sweep_for_ui-live": (
        "append",
        "def planted(store: object) -> object:\n"
        "    from eval.harness import run_sweep_for_ui\n\n"
        "    return run_sweep_for_ui(store=store, live=True)\n",
    ),
    "harness-main-live": (
        "append",
        'def planted() -> object:\n    import eval.harness as h\n\n    return h.main(["--live"])\n',
    ),
    "run_sweep_for_ui-kwargs": (
        "append",
        "def planted(opts: dict[str, object]) -> object:\n"
        "    from eval.harness import run_sweep_for_ui\n\n"
        "    return run_sweep_for_ui(**opts)\n",
    ),
    "body-import-eval-live": (
        "append",
        "def planted() -> object:\n"
        "    from eval.live import build_live_provider\n\n"
        "    return build_live_provider\n",
    ),
    "run_sweep_for_ui-in-submit_sweep": (
        "submit_sweep",
        "from eval.harness import run_sweep_for_ui\n"
        'run_sweep_for_ui(store=store, live=mode == "live")',
    ),
}


@pytest.mark.parametrize("mutation", sorted(G2R_C_1_MUTATIONS))
def test_each_g2r_c_1_mutation_is_red(tree: Path, mutation: str) -> None:
    where, source = G2R_C_1_MUTATIONS[mutation]
    if where == "append":
        _append(tree / TRIGGER, source)
    else:
        _in_submit_sweep(tree / TRIGGER, source)
    _, stub_literal = _violations(tree)
    assert any("outside the harness allowlist" in v for v in stub_literal), stub_literal


@pytest.mark.parametrize(
    "source",
    [
        "from eval import harness\n\nPLANTED = harness.run_sweep\n",
        "from eval.harness import run_sweep\n",
        "from eval.harness import *  # noqa: F403\n",
        "import eval.harness\n",
        "from . import harness\n",
        "from .harness import main\n",
    ],
    ids=[
        "from-eval-import-harness",
        "run_sweep",
        "star",
        "import-module",
        "relative",
        "relative-main",
    ],
)
def test_any_other_harness_binding_is_red(tree: Path, source: str) -> None:
    _append(tree / TRIGGER, source)
    _, stub_literal = _violations(tree)
    assert any("outside the harness allowlist" in v for v in stub_literal), stub_literal


@pytest.mark.parametrize(
    "source",
    [
        "import eval.live\n",
        "from eval import live\n",
        "import providers.factory\n",
        "from providers.factory import build_provider\n",
        "from providers import factory\n",
        "from eval.sweep_units import run_task\n",
        "from . import sweep_units\n",
        'import importlib\n\nPLANTED = importlib.import_module("eval.live")\n',
        "import eval.runs\n\nPLANTED = eval.live\n",
    ],
    ids=[
        "import-live",
        "from-eval-live",
        "import-factory",
        "from-factory",
        "from-providers-factory",
        "sweep_units",
        "relative-sweep_units",
        "string-literal",
        "dotted-name",
    ],
)
def test_any_reference_to_a_forbidden_module_is_red(tree: Path, source: str) -> None:
    _append(tree / TRIGGER, source)
    _, stub_literal = _violations(tree)
    assert any("outside the harness allowlist" in v for v in stub_literal), stub_literal


def test_an_allowlisted_harness_name_is_not_red(tree: Path) -> None:
    """The allowlist is the boundary: a body import of an allowed name stays clean."""
    _append(
        tree / TRIGGER,
        "def planted() -> object:\n    from eval.harness import EvalMode\n\n    return EvalMode\n",
    )
    assert _violations(tree) == ([], [])


def test_the_allowlist_is_exactly_what_trigger_takes_from_the_harness() -> None:
    """Pins the set; each name the real trigger imports from eval.harness, and no other."""
    tree = ast.parse((REPO_ROOT / TRIGGER).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == gate.HARNESS_MODULE
        for alias in node.names
    }
    assert (
        imported
        == gate.HARNESS_ALLOWED_NAMES
        == {
            "EvalMode",
            "SweepResult",
            "DEFAULT_ARM_SPECS",
            "_DEFAULT_GOLDEN_TASKS_PATH",
            "_load_golden_tasks",
        }
    )
    assert (
        frozenset({"eval.live", "providers.factory", "eval.sweep_units"}) == gate.FORBIDDEN_MODULES
    )


@pytest.mark.parametrize("parameter", sorted(gate.FORBIDDEN_PARAMETERS))
def test_a_mode_selecting_parameter_is_red(tree: Path, parameter: str) -> None:
    path = tree / TRIGGER
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"\n\ndef submit_planted(*, {parameter}: str) -> None:\n    return None\n",
        encoding="utf-8",
    )
    _, stub_literal = _violations(tree)
    assert any(f"declares a {parameter!r} parameter" in v for v in stub_literal), stub_literal


def test_submit_sweep_is_the_one_function_allowed_a_mode(tree: Path) -> None:
    """The real `submit_sweep` takes `mode` for the state machine's input and is not red."""
    assert frozenset({"submit_sweep"}) == gate.MODE_PARAMETER_FUNCTIONS
    assert "def submit_sweep(" in (tree / TRIGGER).read_text(encoding="utf-8")
    _, stub_literal = _violations(tree)
    assert stub_literal == [], stub_literal


def test_losing_submit_sweep_is_red(tree: Path) -> None:
    """Non-vacuity: the exemption's subject must exist."""
    _replace_once(tree / TRIGGER, "def submit_sweep(", "def submit_sweep_renamed(")
    _, stub_literal = _violations(tree)
    assert any("defines no submit_sweep()" in v for v in stub_literal), stub_literal


def test_a_lambda_taking_a_mode_is_red(tree: Path) -> None:
    path = tree / TRIGGER
    path.write_text(
        path.read_text(encoding="utf-8") + "\n\nPLANTED = lambda mode: mode\n",
        encoding="utf-8",
    )
    _, stub_literal = _violations(tree)
    assert any("<lambda>() declares a 'mode'" in v for v in stub_literal), stub_literal


def test_a_pending_handler_module_that_now_exists_is_red(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module still listed in PENDING_HANDLER_MODULES once its source exists is a violation."""
    shutil.copy2(REPO_ROOT / "infra" / "eval_sfn.tf", tree / "infra" / "eval_sfn.tf")
    monkeypatch.setattr(gate, "PENDING_HANDLER_MODULES", frozenset({"eval.sfn_handlers"}))
    (tree / "src" / "eval" / "sfn_handlers.py").unlink(missing_ok=True)
    import_time, _ = _violations(tree)
    assert import_time == [], import_time
    (tree / "src" / "eval" / "sfn_handlers.py").write_text("X = 1\n", encoding="utf-8")
    import_time, _ = _violations(tree)
    assert any("still in PENDING_HANDLER_MODULES" in v for v in import_time), import_time


def test_the_sweep_handlers_are_walked(tree: Path) -> None:
    """`eval.sfn_handlers` is walked, so an adapter import there reds both handlers."""
    shutil.copy2(REPO_ROOT / "infra" / "eval_sfn.tf", tree / "infra" / "eval_sfn.tf")
    assert _violations(tree) == ([], [])
    _prepend(tree / "src" / "eval" / "sfn_handlers.py", "import providers.anthropic_adapter")
    import_time, _ = _violations(tree)
    reddened = {v.split("'")[1] for v in import_time if v.startswith("handler '")}
    assert reddened == {
        "eval.sfn_handlers.sweep_control_handler",
        "eval.sfn_handlers.sweep_task_handler",
    }, import_time


def test_the_cli_exits_one_on_a_violation(tree: Path) -> None:
    _prepend(
        tree / "src" / "crm" / "handlers" / "get_account.py", "import providers.openai_adapter"
    )
    completed = proc_run.run(
        [sys.executable, str(GATE_PATH), "--repo-root", str(tree)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stdout
    assert "[FAIL] import_time" in completed.stdout
