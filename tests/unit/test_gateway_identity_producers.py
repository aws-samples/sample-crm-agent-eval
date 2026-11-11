# SPDX-License-Identifier: MIT
"""Every gateway- and agent-channel identity producer, and every actor fixture, declares
`principal_type`.

The decoder refuses an `_identity` or `_agent_identity` block without `principal_type`, and
`actor_principal_type` refuses an actor without it. The guards walk the AST of every tracked Python
file under `src/`, `scripts/` and `tests/`. The interceptor's three identity builders are pinned
individually.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

import pytest
from scripts import proc_run

from crm.envelope import IDENTITY_KEY, decode_identity
from crm.errors import AuthenticationFailed
from gateway.identity_interceptor import (
    GatewayInterceptorConfig,
    _build_verified_identity,
    _service_identity,
    _unauthenticable_identity,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED_ROOTS = ("src", "scripts", "tests")
_INTERCEPTOR = "src/gateway/identity_interceptor.py"


def _gateway_literals_without_principal_type(tree: ast.AST) -> tuple[int, list[int]]:
    """Return the count of `"source": "gateway"` dict literals and the lines of those lacking
    a `"principal_type"` key."""
    found = 0
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            key.value: value
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        source = keys.get("source")
        if not (isinstance(source, ast.Constant) and source.value == "gateway"):
            continue
        found += 1
        if "principal_type" not in keys:
            missing.append(node.lineno)
    return found, missing


def _tracked_python_files() -> list[str]:
    listed = proc_run.run(
        ["git", "ls-files", "--", *_SCANNED_ROOTS],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [path for path in listed if path.endswith(".py")]


def _tracked_trees() -> list[tuple[str, ast.Module]]:
    return [
        (
            relative,
            ast.parse((_REPO_ROOT / relative).read_text(encoding="utf-8"), filename=relative),
        )
        for relative in _tracked_python_files()
    ]


def _scan_tracked_tree() -> tuple[dict[str, int], dict[str, list[int]]]:
    counts: dict[str, int] = {}
    offenders: dict[str, list[int]] = {}
    for relative, tree in _tracked_trees():
        found, missing = _gateway_literals_without_principal_type(tree)
        if found:
            counts[relative] = found
        if missing:
            offenders[relative] = missing
    return counts, offenders


def test_every_tracked_gateway_identity_literal_declares_principal_type() -> None:
    """No tracked `"source": "gateway"` dict literal omits `principal_type`."""
    counts, offenders = _scan_tracked_tree()

    assert _INTERCEPTOR in counts, "the scan did not reach the interceptor's identity builders"
    assert counts[_INTERCEPTOR] >= 3
    assert offenders == {}, (
        f"gateway identity literals without principal_type (file: lines): {offenders!r}"
    )


def test_the_guard_reports_a_gateway_literal_without_principal_type() -> None:
    """The walker flags the exact shape it exists to catch, and nothing else."""
    planted = ast.parse(
        "ok = {'rep_id': 'r', 'principal_type': 'user', 'source': 'gateway'}\n"
        "bad = {'rep_id': 'r', 'source': 'gateway'}\n"
        "agent = {'rep_id': 'r', 'source': 'agent'}\n"
        "nested = {'_identity': {'source': 'gateway', 'book': []}}\n"
    )

    found, missing = _gateway_literals_without_principal_type(planted)

    assert found == 3
    assert missing == [2, 4]


def test_the_guard_ignores_a_gateway_literal_inside_a_string() -> None:
    """A literal quoted in a string is not a producer; only parsed code is scanned."""
    planted = ast.parse("SNIPPET = \"{'rep_id': 'r', 'source': 'gateway'}\"\n")

    assert _gateway_literals_without_principal_type(planted) == (0, [])


_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/pool123"


def _config(rep_id: str = "svc-mcp-gateway") -> GatewayInterceptorConfig:
    return GatewayInterceptorConfig(
        discovery_url=f"{_ISSUER}/.well-known/openid-configuration",
        jwks_url=f"{_ISSUER}/.well-known/jwks.json",
        allowed_client_ids=frozenset({"client-1"}),
        allowed_scopes=frozenset({"crm/invoke"}),
        service_principal_rep_id=rep_id,
        table_prefix="crm-agent-eval",
    )


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "sub-1",
        "iss": _ISSUER,
        "client_id": "client-1",
        "token_use": "access",
        "scope": "crm/invoke",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return claims


def _rep_row(_rep_id: str) -> dict[str, Any]:
    return {
        "rep_id": "svc-mcp-gateway",
        "tenant": "eval-smoke-1",
        "book": ["acct-1"],
        "full_visibility": False,
    }


def test_the_user_builder_emits_a_user_principal_type_the_decoder_accepts() -> None:
    identity = _build_verified_identity(
        _claims(**{"custom:rep_id": "rep-1", "custom:book": "acct-1", "custom:tenant": "demo"}),
        session_id="s-1",
        config=_config(),
    )

    assert identity["principal_type"] == "user"
    assert decode_identity({IDENTITY_KEY: identity}).principal_type == "user"


def test_the_service_builder_emits_a_service_principal_type_the_decoder_accepts() -> None:
    identity = _service_identity(_config(), session_id="s-1", rep_lookup=_rep_row)

    assert identity is not None
    assert identity["principal_type"] == "service"
    assert decode_identity({IDENTITY_KEY: identity}).principal_type == "service"


def test_the_unauthenticable_builder_emits_principal_type_and_is_still_refused() -> None:
    """The refusal comes from the empty `rep_id`, not from a missing `principal_type`."""
    identity = _unauthenticable_identity("s-1")

    assert identity["principal_type"] == "user"
    assert identity["rep_id"] == ""
    with pytest.raises(AuthenticationFailed) as refused:
        decode_identity({IDENTITY_KEY: identity})
    assert "rep_id" in str(refused.value.detail)


_AGENT_CHANNEL_KEYS = frozenset({"_agent_identity", "AGENT_IDENTITY_KEY"})
_PRINCIPAL_TYPE_KEYS = frozenset({"principal_type", "AGENT_PRINCIPAL_TYPE_KEY"})
_APIGW_CLAIMS_CONTAINERS = frozenset({"authorizer", "jwt"})
_ACTOR_KEY = "actor"
_ACTOR_FUNCTION = "actor_principal_type"
_AGENT_PRODUCERS = (
    "src/agents/tool_dispatch.py",
    "src/eval/harness.py",
    "src/eval/sfn_handlers.py",
)


def _key_name(key: ast.expr | None) -> str | None:
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    if isinstance(key, ast.Name):
        return key.id
    if isinstance(key, ast.Attribute):
        return key.attr
    return None


def _keys_added_later(tree: ast.AST) -> set[int]:
    """Ids of dict literals bound to a name that a later ``name[<principal_type key>] = ...``
    in the same function (or module) completes."""
    completed: set[int] = set()
    scopes = [
        tree,
        *(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)),
    ]
    for scope in scopes:
        stored = {
            node.value.id
            for node in ast.walk(scope)
            if isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and _key_name(node.slice) in _PRINCIPAL_TYPE_KEYS
        }
        for node in ast.walk(scope):
            target: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            value = getattr(node, "value", None)
            if isinstance(target, ast.Name) and target.id in stored and isinstance(value, ast.Dict):
                completed.add(id(value))
    return completed


def _agent_and_actor_literals_without_principal_type(
    tree: ast.AST,
) -> tuple[dict[str, int], list[tuple[str, int]]]:
    """Count agent-identity and actor dict literals in `tree`; list ``(kind, line)`` of those that
    never declare `principal_type`.

    An agent-identity literal is the value of an `_agent_identity` key, or any dict carrying
    `claims` that is not an API Gateway `authorizer` / `jwt` container. An actor literal is the
    value of an `actor` key or a literal argument to `actor_principal_type`.
    """
    parent_key: dict[int, str | None] = {}
    actor_args: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(value, ast.Dict):
                    parent_key[id(value)] = _key_name(key)
        elif isinstance(node, ast.Call) and _key_name(node.func) == _ACTOR_FUNCTION:
            actor_args.update(id(arg) for arg in node.args if isinstance(arg, ast.Dict))

    completed = _keys_added_later(tree)
    counts = {"agent": 0, "actor": 0}
    missing: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        names = {_key_name(key) for key in node.keys}
        parent = parent_key.get(id(node))
        kind: str | None = None
        if parent in _AGENT_CHANNEL_KEYS or (
            "claims" in names
            and parent not in _APIGW_CLAIMS_CONTAINERS
            and not names & _APIGW_CLAIMS_CONTAINERS
        ):
            kind = "agent"
        elif parent == _ACTOR_KEY or id(node) in actor_args:
            kind = "actor"
        if kind is None:
            continue
        counts[kind] += 1
        if not names & _PRINCIPAL_TYPE_KEYS and id(node) not in completed:
            missing.append((kind, node.lineno))
    return counts, sorted(missing, key=lambda item: item[1])


def test_every_tracked_agent_identity_and_actor_literal_declares_principal_type() -> None:
    """No tracked agent-identity block or actor fixture omits `principal_type`."""
    reached: dict[str, dict[str, int]] = {}
    offenders: dict[str, list[tuple[str, int]]] = {}
    for relative, tree in _tracked_trees():
        counts, missing = _agent_and_actor_literals_without_principal_type(tree)
        reached[relative] = counts
        if missing:
            offenders[relative] = missing

    for producer in _AGENT_PRODUCERS:
        assert reached[producer]["agent"] >= 1, f"the scan did not reach {producer}'s agent block"
    assert sum(counts["actor"] for counts in reached.values()) >= 1, "no actor fixture was reached"
    assert offenders == {}, (
        f"agent-identity / actor literals without principal_type (file: [(kind, line)]): "
        f"{offenders!r}"
    )


def test_the_agent_guard_reports_each_shape_it_exists_to_catch() -> None:
    """Planted fixture: every flagged shape is reported, every excluded or completed one is not."""
    planted = ast.parse(
        "from crm.envelope import AGENT_IDENTITY_KEY, AGENT_PRINCIPAL_TYPE_KEY\n"
        "a = {AGENT_IDENTITY_KEY: {'claims': {}, 'principal_type': 'user'}}\n"
        "b = {'_agent_identity': {'session_id': 's'}}\n"
        "c = identity_event(AGENT_IDENTITY_KEY, {'claims': {}}, {})\n"
        "d = {'claims': {}, AGENT_PRINCIPAL_TYPE_KEY: 'service'}\n"
        "e = {'requestContext': {'authorizer': {'claims': {}}}}\n"
        "f = {'authorizer': {'jwt': {'claims': {}}}}\n"
        "g = {'actor': {'rep_id': 'r', 'book': [], 'full_visibility': False}}\n"
        "h = {'actor': {'rep_id': 'r', 'principal_type': 'service'}}\n"
        "i = actor_principal_type({'rep_id': 'r'})\n"
        "def build(pt):\n"
        "    block = {'claims': {}}\n"
        "    block[AGENT_PRINCIPAL_TYPE_KEY] = pt\n"
        "    return block\n"
        "def unfinished():\n"
        "    other = {'claims': {}}\n"
        "    return other\n"
    )

    counts, missing = _agent_and_actor_literals_without_principal_type(planted)

    assert missing == [("agent", 3), ("agent", 4), ("actor", 8), ("actor", 10), ("agent", 16)]
    assert counts == {"agent": 6, "actor": 3}


def test_the_agent_guard_alias_names_the_principal_type_field() -> None:
    """The guard accepts `AGENT_PRINCIPAL_TYPE_KEY` only because it is that field's name."""
    from crm.envelope import AGENT_PRINCIPAL_TYPE_KEY

    assert AGENT_PRINCIPAL_TYPE_KEY == "principal_type"
