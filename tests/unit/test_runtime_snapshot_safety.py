# SPDX-License-Identifier: MIT
"""Snapshot-safety tests for the AgentCore Runtime container.

Properties:

* no module in the static import closure of :mod:`agents.runtime_server` makes a random,
  clock, host/PID, or credential/client-building call directly at import time (an AST scan, with
  mutation cells proving the scan sees each forbidden call and ignores function bodies);
* importing that closure in a fresh interpreter with the random, clock and host/PID callables
  wrapped records no call a first-party module triggers, including one reached through a
  first-party helper the AST scan cannot follow;
* per-request ids come from the request, not from process state: two fresh interpreter
  processes serving turns emit ids derived only from each request's session id;
* a turn's wall-clock deadline is started inside the request, not carried over from import.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import proc_run
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
import crm.dispatch as crm_dispatch
from agents.budget import TurnBudget, TurnBudgetSpec
from agents.cancellation import InMemoryCancellationStore
from agents.conversation_history import InMemoryConversationHistory
from agents.runtime_entrypoint import InMemorySessionStore

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SRC_ROOT: Final[Path] = REPO_ROOT / "src"

#: The runtime container's entry module, the root of the closure.
ENTRY_MODULE: Final[str] = "agents.runtime_server"
#: First-party top-level packages the closure follows.
FIRST_PARTY: Final[frozenset[str]] = frozenset({"agents", "providers", "crm", "eval"})

#: Fully qualified callables that must never run at import time.
FORBIDDEN_CALLS: Final[frozenset[str]] = frozenset(
    {
        "uuid.uuid1",
        "uuid.uuid4",
        "os.urandom",
        "os.getpid",
        "os.getppid",
        "socket.gethostname",
        "socket.getfqdn",
        "platform.node",
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "boto3.client",
        "boto3.resource",
        "boto3.Session",
        "boto3.session.Session",
        "botocore.session.get_session",
        "functools.lru_cache",
        "functools.cache",
    }
)
#: Modules every attribute call of which is forbidden at import time.
FORBIDDEN_MODULES: Final[frozenset[str]] = frozenset({"random", "secrets"})


#: Classified-safe caches, ``"<module> <decorator> on <function>"``: each caches a pure function
#: of fixed inputs, computed on first call, never at import.
SNAPSHOT_SAFE_CACHES: Final[frozenset[str]] = frozenset(
    {
        "eval.scoring functools.cache on sweep_seed_key_patterns",
        "agents.subagent_runner functools.cache on _subagent_prompt_template",
        "providers.prompt_caching functools.cache on min_prefix_tokens",
    }
)


def _strip_line(finding: str) -> str:
    """Return ``finding`` without its ``:<line>``, for matching against an allowlist."""
    location, _, rest = finding.partition(" ")
    return f"{location.rpartition(':')[0]} {rest}"


def _module_file(name: str) -> Path | None:
    path = SRC_ROOT.joinpath(*name.split("."))
    if (path / "__init__.py").is_file():
        return path / "__init__.py"
    if path.with_suffix(".py").is_file():
        return path.with_suffix(".py")
    return None


def _imported_names(module: str, tree: ast.Module, *, is_package: bool) -> Iterator[str]:
    """Yield every absolute module name ``tree`` imports, function-body imports included."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = module.split(".") if is_package else module.split(".")[:-1]
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            yield base
            yield from (f"{base}.{alias.name}" for alias in node.names)


def runtime_import_closure() -> dict[str, Path]:
    """Return every first-party module reachable by import from :data:`ENTRY_MODULE`."""
    seen: dict[str, Path] = {}
    pending = [ENTRY_MODULE]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        path = _module_file(name)
        if path is None:
            continue
        seen[name] = path
        parts = name.split(".")
        pending.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imported_names(name, tree, is_package=path.name == "__init__.py"):
            if imported.split(".")[0] in FIRST_PARTY:
                pending.append(imported)
    return seen


def _alias_map(tree: ast.Module) -> dict[str, str]:
    """Map each locally bound import name in ``tree`` to its fully qualified target."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _dotted(node.value)
        return None if owner is None else f"{owner}.{node.attr}"
    return None


def _qualified(node: ast.expr, aliases: dict[str, str]) -> str | None:
    dotted = _dotted(node)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    target = aliases.get(head, head)
    return f"{target}.{rest}" if rest else target


def _is_forbidden(name: str) -> bool:
    return name in FORBIDDEN_CALLS or name.split(".")[0] in FORBIDDEN_MODULES


def _import_time_nodes(body: list[ast.stmt]) -> Iterator[tuple[ast.AST, str | None]]:
    """Yield ``(node, decorated)`` for every node evaluated when ``body`` runs at import time.

    Function bodies are skipped; their decorators and defaults are not. Class bodies, and
    ``if``/``try``/``with``/loop blocks at module scope, are followed. ``decorated`` names the
    function or class a decorator node applies to, and is ``None`` for every other node.
    """
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from ((d, stmt.name) for d in stmt.decorator_list)
            yield from ((d, None) for d in stmt.args.defaults)
            yield from ((d, None) for d in stmt.args.kw_defaults if d is not None)
        elif isinstance(stmt, ast.ClassDef):
            yield from ((d, stmt.name) for d in stmt.decorator_list)
            yield from ((b, None) for b in (*stmt.bases, *stmt.keywords))
            yield from _import_time_nodes(stmt.body)
        elif isinstance(stmt, (ast.If, ast.While)):
            yield stmt.test, None
            yield from _import_time_nodes(stmt.body)
            yield from _import_time_nodes(stmt.orelse)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            yield stmt.iter, None
            yield from _import_time_nodes(stmt.body)
            yield from _import_time_nodes(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            yield from _import_time_nodes(stmt.body)
            for handler in stmt.handlers:
                yield from _import_time_nodes(handler.body)
            yield from _import_time_nodes(stmt.orelse)
            yield from _import_time_nodes(stmt.finalbody)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            yield from ((item.context_expr, None) for item in stmt.items)
            yield from _import_time_nodes(stmt.body)
        else:
            yield stmt, None


def import_time_violations(source: str, *, filename: str = "<source>") -> list[str]:
    """Return ``"<file>:<line> <callable>"`` for each forbidden call ``source`` makes at import.

    A finding on a decorator ends `` on <decorated name>``.

    A forbidden callable applied as a bare decorator (``@functools.cache``) counts as a call; a
    bare reference elsewhere (``clock=time.monotonic``) does not.
    """
    tree = ast.parse(source, filename=filename)
    aliases = _alias_map(tree)
    found: list[str] = []
    for root, decorated in _import_time_nodes(tree.body):
        suffix = "" if decorated is None else f" on {decorated}"
        for node in ast.walk(root):
            if not isinstance(node, ast.Call):
                continue
            name = _qualified(node.func, aliases)
            if name is not None and _is_forbidden(name):
                found.append(f"{filename}:{node.lineno} {name}{suffix}")
        if decorated is not None and isinstance(root, (ast.Name, ast.Attribute)):
            name = _qualified(root, aliases)
            if name is not None and _is_forbidden(name):
                found.append(f"{filename}:{root.lineno} {name}{suffix}")
    return found


# --- static scan --------------------------------------------------------------------------------


def test_the_closure_covers_the_runtime_server_and_both_provider_adapters() -> None:
    """The closure walk follows lazy imports, so it reaches both adapters and the transport."""
    closure = runtime_import_closure()
    for expected in (
        ENTRY_MODULE,
        "agents.budget",
        "agents.turn_driver",
        "providers.factory",
        "providers.anthropic_adapter",
        "providers.openai_adapter",
        "providers.sdk_transport",
        "providers.stub",
        "crm.dispatch",
    ):
        assert expected in closure, f"closure walk missed {expected}"


def test_no_module_in_the_runtime_closure_makes_a_forbidden_call_at_import_time() -> None:
    """No random, clock, host/PID, credential or cache value is computed at import."""
    findings: list[str] = []
    for name, path in sorted(runtime_import_closure().items()):
        findings.extend(import_time_violations(path.read_text(encoding="utf-8"), filename=name))
    assert [f for f in findings if _strip_line(f) not in SNAPSHOT_SAFE_CACHES] == []


def test_every_classified_safe_cache_still_exists() -> None:
    """Each allowlist entry matches a live finding, so a removed cache cannot leave a stale pass."""
    findings = {
        _strip_line(finding)
        for name, path in runtime_import_closure().items()
        for finding in import_time_violations(path.read_text(encoding="utf-8"), filename=name)
    }
    assert findings >= SNAPSHOT_SAFE_CACHES


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("import uuid\nBOOT = uuid.uuid4().hex\n", "uuid.uuid4"),
        ("from uuid import uuid4 as mk\nBOOT = mk()\n", "uuid.uuid4"),
        ("import random\nSEED = random.random()\n", "random.random"),
        ("import secrets\nTOKEN = secrets.token_hex(8)\n", "secrets.token_hex"),
        ("import os\nNONCE = os.urandom(8)\n", "os.urandom"),
        ("import os\nPID = os.getpid()\n", "os.getpid"),
        ("import socket\nHOST = socket.gethostname()\n", "socket.gethostname"),
        ("import time\nSTARTED = time.monotonic()\n", "time.monotonic"),
        ("from time import time as now\nSTARTED = now()\n", "time.time"),
        ("import datetime\nT = datetime.datetime.now()\n", "datetime.datetime.now"),
        ("import boto3\nCLIENT = boto3.client('lambda')\n", "boto3.client"),
        ("import boto3\nSESSION = boto3.Session()\n", "boto3.Session"),
        ("import functools\n@functools.lru_cache\ndef f() -> int:\n    return 1\n", "lru_cache"),
        ("from functools import cache\n@cache\ndef f() -> int:\n    return 1\n", "cache on f"),
        (
            "import functools\n@functools.lru_cache(maxsize=1)\ndef g() -> int:\n    return 1\n",
            "functools.lru_cache on g",
        ),
        ("import time\ndef f(t: float = time.time()) -> float:\n    return t\n", "time.time"),
        ("import time\nclass C:\n    STARTED = time.monotonic()\n", "time.monotonic"),
        ("import uuid\nif True:\n    BOOT = uuid.uuid4()\n", "uuid.uuid4"),
        ("import uuid\ntry:\n    BOOT = uuid.uuid4()\nexcept Exception:\n    pass\n", "uuid4"),
    ],
)
def test_the_scan_reports_each_forbidden_import_time_call(snippet: str, expected: str) -> None:
    """Mutation cell: each forbidden kind, however imported or nested at import time, is seen."""
    violations = import_time_violations(snippet)
    assert len(violations) == 1, violations
    assert expected in violations[0]


@pytest.mark.parametrize(
    "snippet",
    [
        "import uuid\ndef handler() -> str:\n    return uuid.uuid4().hex\n",
        "import time\nasync def handler() -> float:\n    return time.monotonic()\n",
        "import time\nclass C:\n    def m(self) -> float:\n        return time.monotonic()\n",
        "import time\nCLOCK = time.monotonic\n",
        "import boto3\ndef client() -> object:\n    return boto3.client('lambda')\n",
    ],
)
def test_the_scan_ignores_request_path_calls_and_uncalled_references(snippet: str) -> None:
    """Control: calls inside function bodies and bare references to a clock are not flagged."""
    assert import_time_violations(snippet) == []


@pytest.mark.parametrize(
    ("module", "injected"),
    [
        ("agents.runtime_server", "\nimport uuid\n_BOOT_ID = uuid.uuid4().hex\n"),
        ("agents.budget", "\n_IMPORTED_AT = time.monotonic()\n"),
        ("providers.sdk_transport", "\n_DEADLINE = time.monotonic() + PROVIDER_TIMEOUT_SECONDS\n"),
        ("providers.anthropic_adapter", "\n_SESSION = boto3.Session()\n"),
    ],
)
def test_the_scan_reds_when_a_real_closure_module_gains_an_import_time_value(
    module: str, injected: str
) -> None:
    """Mutation cell on the real sources: appending one import-time value reds the scan."""
    path = runtime_import_closure()[module]
    source = path.read_text(encoding="utf-8")
    assert import_time_violations(source, filename=module) == []
    assert len(import_time_violations(source + injected, filename=module)) == 1


# --- dynamic import probe -----------------------------------------------------------------------

#: Entries of :data:`SNAPSHOT_SAFE_CACHES` the dynamic probe would also report, as
#: ``"<module> <callable>"``. Empty: the only AST entry is a ``functools.cache`` decorator, and the
#: probe does not wrap ``functools``.
DYNAMIC_PROBE_ALLOWLIST: Final[frozenset[str]] = frozenset()

#: Program run in a fresh interpreter: ``argv[1]`` is the first-party source root, the rest are
#: module names. Wraps the forbidden callables, imports each module, and prints JSON with the
#: imported names and one ``"<module>:<line> <callable>"`` finding per recorded call. A call is
#: recorded when the nearest first-party frame on its stack is reached without crossing an import
#: of a non-first-party module, so a third-party package's own import-time work is not counted.
_IMPORT_PROBE_PROGRAM: Final[str] = """
import importlib, inspect, json, os, platform, random, secrets, socket, sys, time, uuid

src_root = os.path.realpath(sys.argv[1]) + os.sep
names = sys.argv[2:]
findings = []
state = {"recording": True}

def _first_party_module(filename):
    real = os.path.realpath(filename)
    if not real.startswith(src_root):
        return None
    relative = real[len(src_root):].removesuffix(".py").removesuffix(os.sep + "__init__")
    return relative.replace(os.sep, ".")

def _attribute(frame):
    crossed_import = False
    while frame is not None:
        filename = frame.f_code.co_filename
        module = _first_party_module(filename)
        if module is not None:
            return None if crossed_import else f"{module}:{frame.f_lineno}"
        if filename.startswith("<frozen importlib"):
            crossed_import = True
        frame = frame.f_back
    return None

def _wrap(owner, attr, label):
    original = getattr(owner, attr)
    def recorder(*args, **kwargs):
        if state["recording"]:
            where = _attribute(sys._getframe(1))
            if where is not None:
                findings.append(f"{where} {label}")
        return original(*args, **kwargs)
    setattr(owner, attr, recorder)

for owner, attrs in (
    (uuid, ("uuid1", "uuid4")),
    (os, ("urandom", "getpid", "getppid")),
    (socket, ("gethostname", "getfqdn")),
    (platform, ("node",)),
    (time, ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns")),
):
    for attr in attrs:
        _wrap(owner, attr, f"{owner.__name__}.{attr}")
for owner in (random, secrets):
    for attr in dir(owner):
        value = getattr(owner, attr)
        if not attr.startswith("_") and callable(value) and not inspect.isclass(value):
            _wrap(owner, attr, f"{owner.__name__}.{attr}")

imported = []
for name in names:
    importlib.import_module(name)
    imported.append(name)
state["recording"] = False
print(json.dumps({"imported": imported, "findings": findings}))
"""


def _probe_env(pythonpath: list[Path]) -> dict[str, str]:
    """A child environment with no AWS, agent or CRM setting, in stub mode, on ``pythonpath``."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AWS_", "AGENT_", "CRM_", "ACTION_LAMBDA"))
    }
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(str(path) for path in pythonpath),
            "AGENT_PROVIDER": "stub",
            "AWS_REGION": "us-east-1",
            "ACTION_LAMBDA_PREFIX": "crm-snapshot-unit",
        }
    )
    return env


def import_probe(
    src_root: Path, modules: list[str], *, extra_path: tuple[Path, ...] = ()
) -> dict[str, list[str]]:
    """Import ``modules`` from ``src_root`` in a fresh interpreter under :data:`_IMPORT_PROBE_PROGRAM`.

    Returns its ``imported`` and ``findings`` lists. Fails the test if the child exits non-zero.
    """
    completed = proc_run.run(
        [sys.executable, "-c", _IMPORT_PROBE_PROGRAM, str(src_root), *modules],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=_probe_env([src_root, *extra_path, REPO_ROOT]),
        check=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, list[str]] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


def test_importing_the_runtime_closure_triggers_no_forbidden_call_from_first_party_code() -> None:
    """Dynamic probe: no first-party module reaches a random, clock or host/PID call at import.

    Covers what the AST scan cannot see: a forbidden call reached through a first-party helper or
    constructor at module scope.
    """
    closure = sorted(runtime_import_closure())
    result = import_probe(SRC_ROOT, closure)
    assert result["imported"] == closure
    assert [f for f in result["findings"] if _strip_line(f) not in DYNAMIC_PROBE_ALLOWLIST] == []


def _write_probe_fixture(root: Path) -> tuple[Path, Path]:
    """Lay out a first-party ``probe_pkg`` and a non-first-party ``probe_vendor`` under ``root``."""
    src, vendor = root / "src", root / "vendor"
    (src / "probe_pkg").mkdir(parents=True)
    vendor.mkdir()
    (src / "probe_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (vendor / "probe_vendor.py").write_text(
        "import time\nLOADED_AT = time.time()\n\n\ndef read_clock() -> float:\n"
        "    return time.time()\n",
        encoding="utf-8",
    )
    (src / "probe_pkg" / "helper.py").write_text(
        "import time\n\n\ndef boot_reading() -> float:\n    return time.monotonic()\n",
        encoding="utf-8",
    )
    (src / "probe_pkg" / "uses_vendor.py").write_text(
        "import probe_vendor\n\nVALUE = probe_vendor.LOADED_AT\n", encoding="utf-8"
    )
    (src / "probe_pkg" / "calls_vendor.py").write_text(
        "import probe_vendor\n\nVALUE = probe_vendor.read_clock()\n", encoding="utf-8"
    )
    (src / "probe_pkg" / "indirect.py").write_text(
        "from probe_pkg.helper import boot_reading\n\n_IMPORTED_AT = boot_reading()\n",
        encoding="utf-8",
    )
    return src, vendor


def test_the_probe_records_a_call_reached_through_a_first_party_helper(tmp_path: Path) -> None:
    """Mutation cell: a module-scope call into a first-party helper is attributed to the helper."""
    src, vendor = _write_probe_fixture(tmp_path)
    result = import_probe(src, ["probe_pkg.indirect"], extra_path=(vendor,))
    assert result["findings"] == ["probe_pkg.helper:5 time.monotonic"]


def test_the_probe_records_a_third_party_call_made_from_first_party_module_scope(
    tmp_path: Path,
) -> None:
    """Mutation cell: a first-party module calling a third-party clock reader at import is seen."""
    src, vendor = _write_probe_fixture(tmp_path)
    result = import_probe(src, ["probe_pkg.calls_vendor"], extra_path=(vendor,))
    assert result["findings"] == ["probe_pkg.calls_vendor:3 time.time"]


def test_the_probe_ignores_a_non_first_party_modules_own_import_time_call(tmp_path: Path) -> None:
    """Control: a third-party module's import-time clock read is not charged to its importer."""
    src, vendor = _write_probe_fixture(tmp_path)
    result = import_probe(src, ["probe_pkg.uses_vendor"], extra_path=(vendor,))
    assert result["imported"] == ["probe_pkg.uses_vendor"]
    assert result["findings"] == []


# --- restore simulation -------------------------------------------------------------------------

#: Program run in a fresh interpreter: import the server, pass `/ping`, then serve one stub turn
#: per session id in argv and print each session's streamed `event_id`s as JSON.
_RESTORE_PROGRAM: Final[str] = """
import io, json, sys
import agents.runtime_server as rs
import crm.dispatch as crm_dispatch
from starlette.testclient import TestClient

class _FakeLambda:
    def invoke(self, **kwargs):
        return {"Payload": io.BytesIO(b'{"statusCode": 200, "body": {}}')}

crm_dispatch.lambda_client = lambda: _FakeLambda()
client = TestClient(rs.app)
assert client.get("/ping").json() == {"status": "Healthy"}
ids = {}
for sid in sys.argv[1:]:
    body = {
        "prompt": "What is the status of Acme?",
        "session_id": sid,
        "_agent_identity": {
            "claims": {"custom:rep_id": "rep-1", "custom:book": "acc-1",
                       "custom:tenant": "eval-snapshot-unit"},
            "session_id": sid,
        },
    }
    response = client.post("/invocations", json=body)
    assert response.status_code == 200, response.text
    frames = [json.loads(line[len("data: "):]) for line in response.text.splitlines()
              if line.startswith("data: ")]
    assert frames and frames[-1]["kind"] == "completed", frames
    ids[sid] = [f["event_id"] for f in frames if f.get("event_id")]
print(json.dumps(ids))
"""


def _fresh_process_ids(*session_ids: str) -> dict[str, list[str]]:
    """Serve one stub turn per session in a fresh interpreter; return its emitted event ids."""
    completed = proc_run.run(
        [sys.executable, "-c", _RESTORE_PROGRAM, *session_ids],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=_probe_env([SRC_ROOT, REPO_ROOT]),
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    ids: dict[str, list[str]] = json.loads(completed.stdout.strip().splitlines()[-1])
    return ids


def test_two_fresh_processes_emit_per_request_ids_derived_only_from_the_request() -> None:
    """Restore simulation: ids differ per request and carry no process-local component.

    Two fresh interpreters stand in for two restores of one snapshot. Each serves two sessions;
    one session id is shared. Ids differ across distinct sessions within and across processes,
    and the shared session yields the same ids in both, so nothing fixed at import feeds them.
    """
    first = _fresh_process_ids("snap-shared", "snap-a")
    second = _fresh_process_ids("snap-shared", "snap-b")

    for ids in (*first.values(), *second.values()):
        assert ids, "a stub turn streamed no text_delta event_id"

    assert set(first["snap-a"]).isdisjoint(first["snap-shared"])
    assert set(second["snap-b"]).isdisjoint(second["snap-shared"])
    assert set(first["snap-a"]).isdisjoint(second["snap-b"])
    assert first["snap-shared"] == second["snap-shared"]
    for sid, ids in (*first.items(), *second.items()):
        assert all(event_id.startswith(f"{sid}:") for event_id in ids), (sid, ids)


# --- deadline taken in the handler --------------------------------------------------------------


@dataclass
class _FakeLambdaClient:
    def invoke(self, **_kwargs: Any) -> dict[str, Any]:
        import io

        return {"Payload": io.BytesIO(b'{"statusCode": 200, "body": {}}')}


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A test client over a fresh stub-mode server whose tool dispatch never leaves the process."""
    monkeypatch.setenv("AGENT_PROVIDER", "stub")
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-snapshot-unit")
    monkeypatch.setattr(crm_dispatch, "lambda_client", lambda: _FakeLambdaClient())
    monkeypatch.setattr(runtime_server, "_stub_provider", None)
    monkeypatch.setattr(runtime_server, "_matrix", None)
    monkeypatch.setattr(runtime_server, "_session_store", InMemorySessionStore())
    monkeypatch.setattr(runtime_server, "_cancellation_store", InMemoryCancellationStore())
    monkeypatch.setattr(runtime_server, "_history_store", InMemoryConversationHistory())
    yield TestClient(runtime_server.app)


def _turn_body(session_id: str) -> dict[str, Any]:
    return {
        "prompt": "What is the status of Acme?",
        "session_id": session_id,
        "_agent_identity": {
            "claims": {
                "custom:rep_id": "rep-1",
                "custom:book": "acc-1",
                "custom:tenant": "eval-snapshot-unit",
            },
            "session_id": session_id,
        },
    }


def test_each_chat_turn_starts_its_wall_clock_inside_the_request(
    stub_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every budget a turn builds starts after that request began, never at import or earlier.

    A deadline origin cached at module scope, or reused from a previous turn, reds this.
    """
    started: list[float] = []
    real_from_spec = TurnBudget.from_spec

    def recording_from_spec(spec: TurnBudgetSpec, **kwargs: Any) -> TurnBudget:
        budget = real_from_spec(spec, **kwargs)
        started.append(budget.started_at)
        return budget

    monkeypatch.setattr(TurnBudget, "from_spec", recording_from_spec)

    for session_id in ("deadline-1", "deadline-2"):
        time.sleep(0.01)
        before = time.monotonic()
        started.clear()
        response = stub_client.post("/invocations", json=_turn_body(session_id))
        after = time.monotonic()
        assert response.status_code == 200
        assert started, "the turn built no budget"
        assert all(before <= origin <= after for origin in started), (before, started, after)


def test_a_budget_origin_is_read_from_its_clock_at_construction() -> None:
    """`TurnBudget` reads its clock per instance, so two turns get two origins."""
    readings = iter([100.0, 250.0])
    spec = TurnBudgetSpec(max_tool_calls=1, max_wall_clock_seconds=10.0)
    first = TurnBudget.from_spec(spec, clock=lambda: next(readings))
    second = TurnBudget.from_spec(spec, clock=lambda: next(readings))
    assert (first.started_at, second.started_at) == (100.0, 250.0)
