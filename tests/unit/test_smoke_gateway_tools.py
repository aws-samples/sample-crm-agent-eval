# SPDX-License-Identifier: MIT
"""Tests for `scripts/smoke/gateway_tools.py` against a fake `McpCaller`, with no network call.

Covers tool inventory parity with `crm.toolspec.definitions.ALL_TOOLS`, per-tool invocation, the
outcome-based `_identity` discard check in both directions, and user-safe token refusals.
"""

from __future__ import annotations

import copy
import functools
import json
import time
from collections.abc import Mapping
from typing import Any

import pytest
from scripts.smoke.gateway_tools import (
    _FORGED_REP_ID,
    _IN_BOOK_ACCOUNT,
    _OUT_OF_BOOK_ACCOUNT_INDEX,
    _SERVICE_BOOK_SIZE,
    McpResult,
    RawResult,
    ServicePrincipalFixture,
    _live_table_prefix,
    _require_disposable_tenant,
)
from scripts.smoke.gateway_tools import checks as _module_checks
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.verified_claims import (
    USE_ACCESS,
    USE_ID,
    ClaimsVerificationError,
    verified_claims,
)

from crm.authz import DEMO_TENANT
from crm.toolspec.definitions import ALL_TOOLS, BY_NAME
from tests.unit import _jwks_test_key as test_key

_MCP_CLIENT_ID = "test-mcp-client"
_OUTPUTS = {
    "agentcore_gateway_url": "https://gateway.example.invalid/mcp",
    "cognito_user_pool_id": test_key.USER_POOL_ID,
    "cognito_spa_client_id": test_key.CLIENT_ID,
    "cognito_mcp_client_id": _MCP_CLIENT_ID,
}

#: Stand-in for the token the live provisioner mints; a low-entropy phrase secret scanners ignore.
_FAKE_MINTED_TOKEN = "not a real credential"  # nosec B105  # test placeholder, not a credential


def _tools_list_result(tool_overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a faithful `tools/list` result mirroring every `ALL_TOOLS` entry.

    `tool_overrides` maps a tool name to a replacement MCP tool dict, letting a test corrupt one
    entry (wrong schema, extra tool, missing tool) while leaving the rest faithful.
    """
    overrides = tool_overrides or {}
    tools = []
    for tool in ALL_TOOLS:
        if tool.name in overrides:
            tools.append(overrides[tool.name])
        else:
            tools.append(tool.to_mcp_tool())
    return {"jsonrpc": "2.0", "id": "smoke-1", "result": {"tools": tools}}


def _tool_call_success(rep_id: str = "verified-rep") -> dict[str, Any]:
    """Build a faithful `tools/call` success result whose content reveals which rep_id acted."""
    return {
        "jsonrpc": "2.0",
        "id": "smoke-1",
        "result": {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps({"rep_id": rep_id})}],
        },
    }


def _scope_denied(account_id: str) -> dict[str, Any]:
    """Build a faithful tool-call denial carrying `crm.errors.ScopeDenied`'s taxonomy code."""
    return {
        "jsonrpc": "2.0",
        "id": "smoke-1",
        "result": {
            "isError": True,
            "error": {
                "code": "scope_denied",
                "message": "You do not have access to this account.",
                "missing_scope": account_id,
            },
            "content": [{"type": "text", "text": "You do not have access to this account."}],
        },
    }


def _rpc_refusal(*, extra_keys: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a faithful user-safe JSON-RPC refusal: `error.{code, message}` and nothing else."""
    error: dict[str, Any] = {
        "code": -32001,
        "message": "Authentication failed. Sign in again to continue.",
    }
    if extra_keys:
        error.update(extra_keys)
    return {"jsonrpc": "2.0", "id": "smoke-1", "error": error}


class _FakeCaller:
    """A typed, injected fake standing in for a real Gateway MCP endpoint. No network access.

    Routes a `get_account` call by which account was requested and whether the caller supplied a
    forged `_identity`, mirroring a real Gateway REQUEST interceptor's allow/deny behaviour so the
    identity-discard check's widen/narrow outcomes are genuinely exercised.
    """

    def __init__(
        self,
        *,
        tools_list: McpResult,
        invoke_result: McpResult | None = None,
        invoke_results_by_tool: Mapping[str, McpResult] | None = None,
        expired_refusal: McpResult | None = None,
        underscoped_refusal: McpResult | None = None,
        merges_identity: bool = False,
    ) -> None:
        self.tools_list = tools_list
        self.invoke_result = invoke_result if invoke_result is not None else _tool_call_success()
        self.invoke_results_by_tool = dict(invoke_results_by_tool or {})
        self.expired_refusal = expired_refusal
        self.underscoped_refusal = underscoped_refusal
        self.merges_identity = merges_identity
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        self.calls.append((method, dict(params), token))

        if method == "tools/list":
            return self.tools_list

        if token == "expired-token":  # nosec B105  # test placeholder, not a credential
            if self.expired_refusal is not None:
                return self.expired_refusal
            return _rpc_refusal()
        if token == "underscoped-token":  # nosec B105  # test placeholder, not a credential
            if self.underscoped_refusal is not None:
                return self.underscoped_refusal
            return _rpc_refusal()

        arguments_raw = params.get("arguments")
        arguments: Mapping[str, Any] = arguments_raw if isinstance(arguments_raw, Mapping) else {}
        tool_name = str(params.get("name") or "")
        if tool_name == "get_account" and tool_name not in self.invoke_results_by_tool:
            return self._get_account_result(arguments)

        return self.invoke_results_by_tool.get(tool_name, self.invoke_result)

    def _get_account_result(self, arguments: Mapping[str, Any]) -> McpResult:
        account_id = str(arguments.get("account_id") or "")
        forged = (
            arguments.get("_identity") if isinstance(arguments.get("_identity"), Mapping) else None
        )

        if forged is None:
            in_book = account_id == _IN_BOOK_ACCOUNT
            return _tool_call_success() if in_book else _scope_denied(account_id)

        if not self.merges_identity:
            # A correct interceptor replaces _identity with the verified one: same outcome as an
            # unforged call.
            in_book = account_id == _IN_BOOK_ACCOUNT
            return _tool_call_success() if in_book else _scope_denied(account_id)

        # A merging interceptor honours the caller-supplied _identity's book/full_visibility.
        book = forged.get("book") if isinstance(forged.get("book"), list) else []
        full_visibility = bool(forged.get("full_visibility"))
        allowed = full_visibility or account_id in book
        return _tool_call_success(rep_id=_FORGED_REP_ID) if allowed else _scope_denied(account_id)


class _FakeRawCaller:
    """A raw-body Gateway fake: refuses a JSON-RPC array body the way the interceptor does."""

    def __init__(self, reply: RawResult | None = None) -> None:
        self.reply = reply if reply is not None else _rpc_refusal()
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, body: str, token: str | None) -> RawResult:
        self.calls.append((body, token))
        return self.reply


#: `scripts.smoke.gateway_tools.checks` with the refusing raw-body fake unless one is given.
checks = functools.partial(_module_checks, raw_caller=_FakeRawCaller())


def _run(caller: _FakeCaller, **kwargs: Any) -> list[CheckResult]:
    ctx = context_for_test(outputs=_OUTPUTS)
    return list(checks(ctx, mcp_caller=caller, **kwargs))


def _find(results: list[CheckResult], *, resource: str, check: str) -> CheckResult:
    for result in results:
        if result.resource == resource and result.check == check:
            return result
    raise AssertionError(f"no CheckResult for resource={resource!r} check={check!r}: {results!r}")


# --------------------------------------------------------------------------------------
# Inventory: no extra, no missing, schemas match
# --------------------------------------------------------------------------------------


def test_faithful_inventory_passes_all_three_inventory_checks() -> None:
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource="gateway_tools", check="no_missing_tool").status == "pass"
    assert _find(results, resource="gateway_tools", check="no_extra_tool").status == "pass"
    for tool in ALL_TOOLS:
        assert _find(results, resource=f"tool:{tool.name}", check="schema_matches").status == "pass"


def test_missing_tool_is_detected_and_named() -> None:
    dropped = ALL_TOOLS[0]
    tools = [t.to_mcp_tool() for t in ALL_TOOLS if t.name != dropped.name]
    caller = _FakeCaller(tools_list={"jsonrpc": "2.0", "id": "smoke-1", "result": {"tools": tools}})

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    missing_check = _find(results, resource="gateway_tools", check="no_missing_tool")
    assert missing_check.status == "fail"
    assert dropped.name in missing_check.detail
    schema_check = _find(results, resource=f"tool:{dropped.name}", check="schema_matches")
    assert schema_check.status == "fail"


def test_extra_tool_is_detected_and_named() -> None:
    tools = [t.to_mcp_tool() for t in ALL_TOOLS]
    tools.append({"name": "delete_everything", "description": "not real", "inputSchema": {}})
    caller = _FakeCaller(tools_list={"jsonrpc": "2.0", "id": "smoke-1", "result": {"tools": tools}})

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    extra_check = _find(results, resource="gateway_tools", check="no_extra_tool")
    assert extra_check.status == "fail"
    assert "delete_everything" in extra_check.detail


def test_schema_mismatch_is_detected_by_real_structural_comparison() -> None:
    """A tool whose schema differs by exactly one property fails `schema_matches`."""
    target = ALL_TOOLS[0]
    corrupted = target.to_mcp_tool()
    corrupted_schema = dict(corrupted["inputSchema"])
    corrupted_properties = dict(corrupted_schema.get("properties", {}))
    corrupted_properties["smoke_injected_property"] = {"type": "string"}
    corrupted_schema["properties"] = corrupted_properties
    corrupted["inputSchema"] = corrupted_schema

    caller = _FakeCaller(tools_list=_tools_list_result({target.name: corrupted}))
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    schema_check = _find(results, resource=f"tool:{target.name}", check="schema_matches")
    assert schema_check.status == "fail"
    assert "smoke_injected_property" in schema_check.detail

    # Every other tool's schema is untouched and must still pass.
    for tool in ALL_TOOLS:
        if tool.name != target.name:
            assert (
                _find(results, resource=f"tool:{tool.name}", check="schema_matches").status
                == "pass"
            )


# --------------------------------------------------------------------------------------
# Property descriptions: byte-identical to the toolspec, deferred when unreadable
# --------------------------------------------------------------------------------------

_DESCRIPTIONS_CHECK = "property_descriptions_match_toolspec"
_DESCRIBED_TOOL = BY_NAME["search_opportunities"]


def _with_property(tool_name: str, prop: str, replacement: Mapping[str, Any]) -> dict[str, Any]:
    """`tool_name`'s MCP tool dict with one property's schema replaced."""
    mcp_tool = copy.deepcopy(BY_NAME[tool_name].to_mcp_tool())
    mcp_tool["inputSchema"]["properties"][prop] = dict(replacement)
    return mcp_tool


def _descriptions_result(results: list[CheckResult], tool_name: str) -> CheckResult:
    return _find(results, resource=f"tool:{tool_name}", check=_DESCRIPTIONS_CHECK)


_TOOL_DESCRIPTION_CHECK = "description_matches_toolspec"


def test_faithful_tool_descriptions_pass_for_every_tool() -> None:
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    for tool in ALL_TOOLS:
        check = _find(results, resource=f"tool:{tool.name}", check=_TOOL_DESCRIPTION_CHECK)
        assert check.status == "pass", tool.name


def test_a_one_character_tool_description_drift_fails_only_that_tool() -> None:
    drifted = copy.deepcopy(_DESCRIBED_TOOL.to_mcp_tool())
    drifted["description"] = drifted["description"][:-1] + "!"
    overrides = {_DESCRIBED_TOOL.name: drifted}
    results = _run(_FakeCaller(tools_list=_tools_list_result(overrides)), token="tok")  # nosec B106

    for tool in ALL_TOOLS:
        check = _find(results, resource=f"tool:{tool.name}", check=_TOOL_DESCRIPTION_CHECK)
        assert check.status == ("fail" if tool.name == _DESCRIBED_TOOL.name else "pass")
    failed = _find(results, resource=f"tool:{_DESCRIBED_TOOL.name}", check=_TOOL_DESCRIPTION_CHECK)
    assert "differs from the toolspec" in failed.detail


def test_a_deployed_tool_with_no_description_fails() -> None:
    stripped = {
        key: value for key, value in _DESCRIBED_TOOL.to_mcp_tool().items() if key != "description"
    }
    overrides = {_DESCRIBED_TOOL.name: stripped}
    results = _run(_FakeCaller(tools_list=_tools_list_result(overrides)), token="tok")  # nosec B106

    check = _find(results, resource=f"tool:{_DESCRIBED_TOOL.name}", check=_TOOL_DESCRIPTION_CHECK)
    assert check.status == "fail"
    assert "carries no description" in check.detail


def test_an_absent_deployed_tool_defers_its_tool_description_rather_than_passing() -> None:
    tools = [t.to_mcp_tool() for t in ALL_TOOLS if t.name != _DESCRIBED_TOOL.name]
    caller = _FakeCaller(tools_list={"jsonrpc": "2.0", "id": "smoke-1", "result": {"tools": tools}})
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    check = _find(results, resource=f"tool:{_DESCRIBED_TOOL.name}", check=_TOOL_DESCRIPTION_CHECK)
    assert check.status == "defer"
    assert check.defer_reason == "deployed_tool_absent"


def test_described_fixture_property_is_non_vacuous() -> None:
    """The tool the drift cells corrupt really declares a described and an undescribed property."""
    properties = _DESCRIBED_TOOL.input_schema["properties"]
    assert "description" in properties["query"]
    assert "description" not in properties["owner_rep_id"]


def test_faithful_property_descriptions_pass_for_every_tool() -> None:
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    statuses = {tool.name: _descriptions_result(results, tool.name).status for tool in ALL_TOOLS}
    assert set(statuses.values()) == {"pass"}, statuses
    assert " 0 with a description" not in _descriptions_result(results, _DESCRIBED_TOOL.name).detail


def test_a_one_character_description_drift_fails_and_names_the_property() -> None:
    want = _DESCRIBED_TOOL.input_schema["properties"]["query"]
    drifted = {**want, "description": want["description"][:-1] + "!"}
    overrides = {_DESCRIBED_TOOL.name: _with_property(_DESCRIBED_TOOL.name, "query", drifted)}
    results = _run(_FakeCaller(tools_list=_tools_list_result(overrides)), token="tok")  # nosec B106

    check = _descriptions_result(results, _DESCRIBED_TOOL.name)
    assert check.status == "fail"
    assert "description differs from toolspec: ['query']" in check.detail
    assert _find(
        results, resource=f"tool:{_DESCRIBED_TOOL.name}", check="schema_matches"
    ).status == ("pass")
    for tool in ALL_TOOLS:
        if tool.name != _DESCRIBED_TOOL.name:
            assert _descriptions_result(results, tool.name).status == "pass"


def test_a_description_missing_from_the_deployed_property_fails() -> None:
    want = _DESCRIBED_TOOL.input_schema["properties"]["query"]
    stripped = {key: value for key, value in want.items() if key != "description"}
    overrides = {_DESCRIBED_TOOL.name: _with_property(_DESCRIBED_TOOL.name, "query", stripped)}
    results = _run(_FakeCaller(tools_list=_tools_list_result(overrides)), token="tok")  # nosec B106

    check = _descriptions_result(results, _DESCRIBED_TOOL.name)
    assert check.status == "fail"
    assert "description missing from deployed property: ['query']" in check.detail


def test_a_deployed_description_the_toolspec_lacks_fails() -> None:
    want = _DESCRIBED_TOOL.input_schema["properties"]["owner_rep_id"]
    added = {**want, "description": "not in the toolspec"}
    overrides = {_DESCRIBED_TOOL.name: _with_property(_DESCRIBED_TOOL.name, "owner_rep_id", added)}
    results = _run(_FakeCaller(tools_list=_tools_list_result(overrides)), token="tok")  # nosec B106

    check = _descriptions_result(results, _DESCRIBED_TOOL.name)
    assert check.status == "fail"
    assert "deployed description the toolspec does not declare: ['owner_rep_id']" in check.detail


def test_an_absent_deployed_tool_defers_its_descriptions_rather_than_passing() -> None:
    dropped = _DESCRIBED_TOOL
    tools = [t.to_mcp_tool() for t in ALL_TOOLS if t.name != dropped.name]
    caller = _FakeCaller(tools_list={"jsonrpc": "2.0", "id": "smoke-1", "result": {"tools": tools}})
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    check = _descriptions_result(results, dropped.name)
    assert check.status == "defer"
    assert check.defer_reason == "deployed_properties_unreadable"


def test_a_deployed_schema_sharing_no_property_defers_rather_than_passing() -> None:
    emptied = copy.deepcopy(_DESCRIBED_TOOL.to_mcp_tool())
    emptied["inputSchema"]["properties"] = {}
    overrides = {_DESCRIBED_TOOL.name: emptied}
    results = _run(_FakeCaller(tools_list=_tools_list_result(overrides)), token="tok")  # nosec B106

    check = _descriptions_result(results, _DESCRIBED_TOOL.name)
    assert check.status == "defer"
    assert check.defer_reason == "no_shared_property"


# --------------------------------------------------------------------------------------
# Per-tool invocation: full inventory checked even under partial failure
# --------------------------------------------------------------------------------------


def test_every_tool_is_invoked_individually() -> None:
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    invoke_calls = [c for c in caller.calls if c[0] == "tools/call" and c[2] == "tok"]
    invoked_tool_names = {c[1]["name"] for c in invoke_calls}
    assert invoked_tool_names >= {tool.name for tool in ALL_TOOLS}

    for tool in ALL_TOOLS:
        assert _find(results, resource=f"tool:{tool.name}", check="invoke").status == "pass"


def test_one_failing_tool_invocation_does_not_stop_the_rest_of_the_inventory() -> None:
    failing_tool = ALL_TOOLS[1].name
    caller = _FakeCaller(
        tools_list=_tools_list_result(),
        invoke_results_by_tool={
            failing_tool: {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "error": {"code": -32000, "message": "boom"},
            }
        },
    )

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource=f"tool:{failing_tool}", check="invoke").status == "fail"
    for tool in ALL_TOOLS:
        if tool.name != failing_tool:
            assert _find(results, resource=f"tool:{tool.name}", check="invoke").status == "pass"


def test_invocation_leaking_internal_detail_fails() -> None:
    leaking_tool = ALL_TOOLS[0].name
    caller = _FakeCaller(
        tools_list=_tools_list_result(),
        invoke_results_by_tool={
            leaking_tool: {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "denied"}],
                    "detail": "internal: table=opportunities condition failed",
                },
            }
        },
    )

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource=f"tool:{leaking_tool}", check="invoke").status == "fail"


def test_no_token_supplied_fails_invocation_checks() -> None:
    """A missing token fails the invoke check for every tool rather than skipping it."""
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token=None)

    for tool in ALL_TOOLS:
        assert _find(results, resource=f"tool:{tool.name}", check="invoke").status == "fail"


# --------------------------------------------------------------------------------------
# The outcome-based `_identity` discard check
# --------------------------------------------------------------------------------------


def test_identity_discard_passes_against_a_correctly_behaving_interceptor() -> None:
    """A fake that replaces the caller-supplied `_identity` makes the discard check pass."""
    caller = _FakeCaller(tools_list=_tools_list_result(), merges_identity=False)
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    discard_check = _find(results, resource="gateway_tools", check="identity_discard")
    assert discard_check.status == "pass"


def test_identity_discard_fails_when_a_forged_identity_widens_scope() -> None:
    """A fake that honours a forged out-of-book `book`/`full_visibility` claim fails the check."""
    caller = _FakeCaller(tools_list=_tools_list_result(), merges_identity=True)
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    discard_check = _find(results, resource="gateway_tools", check="identity_discard")
    assert discard_check.status == "fail"
    assert "widened scope" in discard_check.detail


def test_identity_discard_fails_when_a_forged_identity_narrows_scope() -> None:
    """A fake honouring a forged empty `book`, denying the token's own account, fails the check."""

    class _NarrowingCaller(_FakeCaller):
        def _get_account_result(self, arguments: Mapping[str, Any]) -> McpResult:
            account_id = str(arguments.get("account_id") or "")
            forged = arguments.get("_identity")
            if isinstance(forged, Mapping) and forged.get("rep_id") == _FORGED_REP_ID:
                return _scope_denied(account_id)
            return (
                _tool_call_success()
                if account_id == _IN_BOOK_ACCOUNT
                else _scope_denied(account_id)
            )

    caller = _NarrowingCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    discard_check = _find(results, resource="gateway_tools", check="identity_discard")
    assert discard_check.status == "fail"
    assert "narrowed scope" in discard_check.detail


def test_identity_discard_fails_rather_than_passes_when_it_cannot_observe_the_answer() -> None:
    """A widen-attempt response that is neither a success nor a `scope_denied` denial fails."""
    caller = _FakeCaller(
        tools_list=_tools_list_result(),
        invoke_results_by_tool={
            "get_account": {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "result": {
                    "isError": True,
                    "error": {"code": "internal_error", "message": "boom"},
                    "content": [{"type": "text", "text": "boom"}],
                },
            }
        },
    )
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    discard_check = _find(results, resource="gateway_tools", check="identity_discard")
    assert discard_check.status == "fail"
    assert "cannot prove" in discard_check.detail


def test_identity_discard_fails_without_a_token() -> None:
    """A missing token fails `identity_discard` rather than skipping it."""
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token=None)

    discard_check = _find(results, resource="gateway_tools", check="identity_discard")
    assert discard_check.status == "fail"


# --------------------------------------------------------------------------------------
# Expired / underscoped token refusal — user-safe body only
# --------------------------------------------------------------------------------------


def test_expired_token_is_refused_with_a_user_safe_body_only() -> None:
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok", expired_token="expired-token")  # nosec B106  # test placeholder, not a credential

    check = _find(results, resource="gateway_tools", check="expired_token_refused")
    assert check.status == "pass"


def test_underscoped_token_is_refused_with_a_user_safe_body_only() -> None:
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok", underscoped_token="underscoped-token")  # nosec B106  # test placeholder, not a credential

    check = _find(results, resource="gateway_tools", check="underscoped_token_refused")
    assert check.status == "pass"


def test_expired_token_refusal_leaking_internal_detail_fails() -> None:
    """A refusal carrying an internal `detail` key fails the check."""
    caller = _FakeCaller(
        tools_list=_tools_list_result(),
        expired_refusal=_rpc_refusal(extra_keys={"detail": "jwt exp=1690000000 < now"}),
    )
    results = _run(caller, token="tok", expired_token="expired-token")  # nosec B106  # test placeholder, not a credential

    check = _find(results, resource="gateway_tools", check="expired_token_refused")
    assert check.status == "fail"
    assert "extra key" in check.detail
    assert "detail" in check.detail


def test_underscoped_token_refusal_leaking_an_arn_fails() -> None:
    """A refusal with an extra key beyond code/message (a leaked ARN) fails the check."""
    caller = _FakeCaller(
        tools_list=_tools_list_result(),
        underscoped_refusal=_rpc_refusal(
            extra_keys={"resource_arn": "arn:aws:lambda:us-east-1:123456789012:function:crm-x"}
        ),
    )
    results = _run(caller, token="tok", underscoped_token="underscoped-token")  # nosec B106  # test placeholder, not a credential

    check = _find(results, resource="gateway_tools", check="underscoped_token_refused")
    assert check.status == "fail"
    assert "extra key" in check.detail


def test_expired_token_refusal_that_is_not_error_shaped_fails() -> None:
    """A "refusal" that is actually a normal success response must fail, not pass."""
    caller = _FakeCaller(tools_list=_tools_list_result(), expired_refusal=_tool_call_success())
    results = _run(caller, token="tok", expired_token="expired-token")  # nosec B106  # test placeholder, not a credential

    check = _find(results, resource="gateway_tools", check="expired_token_refused")
    assert check.status == "fail"


def test_expired_and_underscoped_checks_fail_when_no_such_token_supplied() -> None:
    """A missing expired/underscoped token fails those checks rather than skipping them."""
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource="gateway_tools", check="expired_token_refused").status == "fail"
    assert (
        _find(results, resource="gateway_tools", check="underscoped_token_refused").status == "fail"
    )


# --------------------------------------------------------------------------------------
# Transport failure surfaces as a fail, never a crash
# --------------------------------------------------------------------------------------


def test_transport_failure_on_tools_list_is_a_fail_not_a_crash() -> None:
    def _broken_caller(method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        return "transport error calling https://gateway.example.invalid/mcp: connection refused"

    ctx = context_for_test(outputs=_OUTPUTS)
    results = list(checks(ctx, mcp_caller=_broken_caller, token="tok"))  # nosec B106  # test placeholder, not a credential

    reachable_check = _find(results, resource="gateway_tools", check="tools_list_reachable")
    assert reachable_check.status == "fail"
    assert "connection refused" in reachable_check.detail


# --------------------------------------------------------------------------------------
# CLI-level flag handling: the harness rejects undeclared flags
# --------------------------------------------------------------------------------------


def test_main_accepts_the_gateway_token_flag_and_reaches_the_outputs_stage(tmp_path: Any) -> None:
    """`run_main` accepts `--gateway-token` and reaches the missing-outputs failure (exit 1)."""
    from scripts.smoke.gateway_tools import main

    missing = tmp_path / "does-not-exist.json"
    exit_code = main(["--gateway-token", "X", "--outputs", str(missing)])
    assert exit_code == 1


@pytest.mark.parametrize(
    "argv",
    [["--totally-bogus-flag", "x"], ["--token", "X"]],
    ids=["bogus-flag", "bare-token"],
)
def test_main_rejects_an_undeclared_flag(argv: list[str]) -> None:
    """A flag this script never declared, a bare `--token` included, is a usage error (exit 2)."""
    from scripts.smoke.gateway_tools import main

    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


# --------------------------------------------------------------------------------------
# AgentCore's `${target_name}___${tool_name}` prefixing, and what `invoke` must not pass on
# --------------------------------------------------------------------------------------


def _prefixed_tools_list() -> dict[str, Any]:
    """A `tools/list` result shaped the way the real Gateway shapes it: target-prefixed names."""
    result = copy.deepcopy(_tools_list_result())
    for entry in result["result"]["tools"]:
        entry["name"] = f"{entry['name'].replace('_', '-')}___{entry['name']}"
    return result


def test_target_prefixed_inventory_matches_the_toolspec() -> None:
    """AgentCore advertises `${target_name}___${tool_name}`; parity must survive the prefix."""
    caller = _FakeCaller(tools_list=_prefixed_tools_list())

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource="gateway_tools", check="no_missing_tool").status == "pass"
    assert _find(results, resource="gateway_tools", check="no_extra_tool").status == "pass"
    for tool in ALL_TOOLS:
        assert _find(results, resource=f"tool:{tool.name}", check="schema_matches").status == "pass"


def test_tools_call_sends_the_target_prefixed_name_not_the_bare_toolspec_name() -> None:
    """A `tools/call` naming the bare toolspec name would not resolve on the real Gateway."""
    caller = _FakeCaller(tools_list=_prefixed_tools_list())

    _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    called = {params["name"] for method, params, _ in caller.calls if method == "tools/call"}
    assert called, "no tools/call was made"
    for name in called:
        assert "___" in name, (
            f"tools/call used an unprefixed name the Gateway cannot resolve: {name}"
        )


def test_an_unprefixed_inventory_still_matches_so_injected_stubs_are_unaffected() -> None:
    """A tool name without a `___` target prefix passes through unchanged."""
    caller = _FakeCaller(tools_list=_tools_list_result())

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource="gateway_tools", check="no_missing_tool").status == "pass"
    assert _find(results, resource="gateway_tools", check="no_extra_tool").status == "pass"


def test_invocation_answering_an_untyped_error_fails_rather_than_passing_vacuously() -> None:
    """An invocation answered by the Gateway's untyped platform error fails."""
    opaque_tool = ALL_TOOLS[0].name
    caller = _FakeCaller(
        tools_list=_tools_list_result(),
        invoke_results_by_tool={
            opaque_tool: {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "result": {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": "An internal error occurred. Please retry later."}
                    ],
                },
            }
        },
    )

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource=f"tool:{opaque_tool}", check="invoke").status == "fail"


def test_invocation_refused_with_authentication_failed_fails() -> None:
    """An invocation refused with `authentication_failed` fails."""
    auth_tool = ALL_TOOLS[0].name
    caller = _FakeCaller(
        tools_list=_tools_list_result(),
        invoke_results_by_tool={
            auth_tool: {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "result": {
                    "isError": True,
                    "error": {
                        "code": "authentication_failed",
                        "message": "Authentication is required.",
                    },
                    "content": [{"type": "text", "text": "Authentication is required."}],
                },
            }
        },
    )

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource=f"tool:{auth_tool}", check="invoke").status == "fail"


def test_invocation_answering_a_typed_domain_error_passes() -> None:
    """An invocation answering a typed domain error such as `not_found` passes."""
    typed_tool = ALL_TOOLS[0].name
    caller = _FakeCaller(
        tools_list=_tools_list_result(),
        invoke_results_by_tool={
            typed_tool: {
                "jsonrpc": "2.0",
                "id": "smoke-1",
                "result": {
                    "isError": True,
                    "error": {"code": "not_found", "message": "That record was not found."},
                    "content": [{"type": "text", "text": "That record was not found."}],
                },
            }
        },
    )

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource=f"tool:{typed_tool}", check="invoke").status == "pass"


def test_schema_check_fails_on_a_wrong_property_type() -> None:
    """A wrong property type fails the schema check."""
    tool = next(
        t
        for t in ALL_TOOLS
        if any(p.get("type") == "integer" for p in t.input_schema.get("properties", {}).values())
    )
    prop = next(
        name
        for name, spec in tool.input_schema["properties"].items()
        if spec.get("type") == "integer"
    )
    result = copy.deepcopy(_tools_list_result())
    for entry in result["result"]["tools"]:
        if entry["name"] == tool.name:
            entry["inputSchema"]["properties"][prop] = {"type": "number"}
    caller = _FakeCaller(tools_list=result)

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    check = _find(results, resource=f"tool:{tool.name}", check="schema_matches")
    assert check.status == "fail"
    assert prop in str(check.detail)


def test_schema_check_fails_on_a_wrong_required_set() -> None:
    """A dropped required field fails the schema check."""
    tool = next(t for t in ALL_TOOLS if t.input_schema.get("required"))
    result = copy.deepcopy(_tools_list_result())
    for entry in result["result"]["tools"]:
        if entry["name"] == tool.name:
            entry["inputSchema"]["required"] = []
    caller = _FakeCaller(tools_list=result)

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    assert _find(results, resource=f"tool:{tool.name}", check="schema_matches").status == "fail"


def test_unexpressible_constraint_keywords_are_disclosed_as_skip_never_pass() -> None:
    """Constraints AgentCore cannot advertise are named and counted, never silently passed."""
    caller = _FakeCaller(tools_list=_tools_list_result())

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    check = _find(
        results, resource="gateway_tools", check="schema_constraint_keywords_unexpressible"
    )
    assert check.status == "skip"
    assert "minLength" in str(check.detail)
    assert "enum" in str(check.detail)


def test_unexpressible_constraint_skip_reason_separates_source_from_deployment() -> None:
    """The reason may not claim the *deployed* Lambdas apply the validator."""
    caller = _FakeCaller(tools_list=_tools_list_result())

    results = _run(caller, token="tok")  # nosec B106  # test placeholder, not a credential

    detail = str(
        _find(
            results, resource="gateway_tools", check="schema_constraint_keywords_unexpressible"
        ).detail
    )
    assert "every action Lambda's source in this repository applies" in detail
    assert "not about the code currently running in the deployed functions" in detail
    # The stronger present-tense claim about the deployed functions must not appear.
    assert "which every action Lambda applies" not in detail


# --------------------------------------------------------------------------------------
# Service-principal provisioning: the fixture the live run authenticates as, and the
# guarantee that its cleanup runs whatever happens.
# --------------------------------------------------------------------------------------


class _FakeProvisioner:
    """A `ServicePrincipalProvisioner` that records whether cleanup ran, with no AWS call."""

    def __init__(self, *, fail: bool = False, cleanup_fails: bool = False) -> None:
        self._fail = fail
        self._cleanup_fails = cleanup_fails
        self.cleaned_up = False

    def provision(self) -> ServicePrincipalFixture:
        if self._fail:
            raise RuntimeError("no service principal configured")
        return ServicePrincipalFixture(
            rep_id="svc-mcp-gateway",
            tenant="eval-smoke-deadbeef",
            book=frozenset({_IN_BOOK_ACCOUNT}),
            in_book_account=_IN_BOOK_ACCOUNT,
            out_of_book_account="smoke-account-out-of-book",
            token=_FAKE_MINTED_TOKEN,
        )

    def cleanup(self) -> list[CheckResult]:
        self.cleaned_up = True
        return [
            CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="fail" if self._cleanup_fails else "pass",
                detail="teardown outcome",
            )
        ]


def test_provisioned_run_authenticates_with_the_minted_service_principal_token() -> None:
    """The provisioner's token reaches every call, so the invoke checks genuinely execute."""
    caller = _FakeCaller(tools_list=_tools_list_result())
    provisioner = _FakeProvisioner()

    results = list(
        checks(context_for_test(outputs=_OUTPUTS), mcp_caller=caller, provisioner=provisioner)
    )

    assert _find(results, resource="gateway_tools", check="provision_service_principal").status == (
        "pass"
    )
    assert all(sent == _FAKE_MINTED_TOKEN for _m, _p, sent in caller.calls)
    assert _find(results, resource="gateway_tools", check="identity_discard").status == "pass"
    assert provisioner.cleaned_up


def test_provisioning_failure_is_a_named_failing_check_not_a_crash() -> None:
    """An unconfigured service principal is reported, never worked around."""
    caller = _FakeCaller(tools_list=_tools_list_result())
    provisioner = _FakeProvisioner(fail=True)

    results = list(
        checks(context_for_test(outputs=_OUTPUTS), mcp_caller=caller, provisioner=provisioner)
    )

    provision = _find(results, resource="gateway_tools", check="provision_service_principal")
    assert provision.status == "fail"
    assert provisioner.cleaned_up


def test_a_failed_teardown_is_reported_as_a_failure_never_as_clean() -> None:
    """A stuck tenant must make the run non-zero rather than reading as cleaned up."""
    caller = _FakeCaller(tools_list=_tools_list_result())

    results = list(
        checks(
            context_for_test(outputs=_OUTPUTS),
            mcp_caller=caller,
            provisioner=_FakeProvisioner(cleanup_fails=True),
        )
    )

    assert _find(results, resource="smoke_tenant", check="tenant_left_clean").status == "fail"


def test_an_explicitly_supplied_token_bypasses_the_provisioner_entirely() -> None:
    """An operator-supplied token takes precedence, so nothing is seeded or torn down."""
    caller = _FakeCaller(tools_list=_tools_list_result())
    provisioner = _FakeProvisioner()

    results = list(
        checks(
            context_for_test(outputs=_OUTPUTS),
            mcp_caller=caller,
            token="operator-token",  # nosec B106  # test placeholder, not a credential
            provisioner=provisioner,
        )
    )

    assert not provisioner.cleaned_up
    assert all(result.check != "provision_service_principal" for result in results), (
        "no provisioning check belongs in a run that provisioned nothing"
    )


def test_out_of_book_account_index_is_outside_the_seeded_book() -> None:
    """The out-of-book account index lies outside the seeded book."""
    assert _OUT_OF_BOOK_ACCOUNT_INDEX >= _SERVICE_BOOK_SIZE


def test_disposable_tenant_guard_refuses_the_demo_tenant() -> None:
    """The disposable-tenant guard refuses `demo` before anything is written."""
    with pytest.raises(RuntimeError):
        _require_disposable_tenant(DEMO_TENANT)
    with pytest.raises(RuntimeError):
        _require_disposable_tenant("production")
    _require_disposable_tenant("eval-smoke-deadbeef")


def test_live_table_prefix_is_derived_from_the_outputs_not_hardcoded() -> None:
    """The interceptor Lambda's name and every table name share this one derived prefix."""
    ctx = context_for_test(
        outputs={**_OUTPUTS, "dynamodb_table_names": {"eval_runs": "crm-agent-evaleval_runs"}}
    )

    assert _live_table_prefix(ctx) == "crm-agent-eval"


# --------------------------------------------------------------------------------------
# Self-minted expired and underscoped tokens on the provisioned path
# --------------------------------------------------------------------------------------

#: Start of the simulated clock the expiry wait advances; real time, since verification reads it.
_T0 = float(int(time.time()))


def _fresh_machine_token(*, now: float, client_id: str = _MCP_CLIENT_ID) -> str:
    """A machine token as Cognito mints it: 5-minute lifetime, carrying `*/invoke`."""
    return test_key.access_token(
        iat=int(now),
        exp=int(now) + 300,
        scope="crm-agent-eval-gateway/invoke",
        client_id=client_id,
    )


def _end_user_access_token() -> str:
    return test_key.access_token(scope="aws.cognito.signin.user.admin", exp=2**31 - 1)


def _verifiers() -> Any:
    from scripts.smoke.gateway_tools import token_verifiers

    return token_verifiers(context_for_test(outputs=_OUTPUTS), jwks_url=test_key.jwks_url())


@pytest.mark.parametrize("ahead", [1, 120])
def test_the_underscoped_verifier_allows_the_default_clock_skew(ahead: int) -> None:
    """A user token whose `iat` is 1s ahead verifies; one 120s ahead (beyond the skew) does not."""
    token = test_key.access_token(
        iat=int(time.time()) + ahead, scope="aws.cognito.signin.user.admin"
    )
    if ahead == 1:
        assert _verifiers().underscoped(token)["token_use"] == USE_ACCESS
    else:
        with pytest.raises(ClaimsVerificationError, match="iat"):
            _verifiers().underscoped(token)


def test_the_expired_verifier_allows_a_token_issued_1s_ahead() -> None:
    token = _fresh_machine_token(now=time.time() + 1)
    assert _verifiers().expired(token)["client_id"] == _MCP_CLIENT_ID


class _SimClock:
    """A clock the injected sleeper advances, so the expiry wait is observable without sleeping."""

    def __init__(self, start: float = _T0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _authorizer_claims(token: str) -> Mapping[str, Any]:
    """Verify `token` as the CUSTOM_JWT authorizer would, for either app client; `exp` is left to
    the caller's simulated clock."""
    for client_id in (_MCP_CLIENT_ID, test_key.CLIENT_ID):
        try:
            return verified_claims(
                token,
                region=test_key.REGION,
                user_pool_id=test_key.USER_POOL_ID,
                client_id=client_id,
                token_use=USE_ACCESS,
                jwks_url=test_key.jwks_url(),
                leeway_seconds=86_400,
            )
        except ClaimsVerificationError as exc:
            if exc.check != "client_id":
                raise
    raise AssertionError("token issued to neither app client")


class _AuthorizingCaller(_FakeCaller):
    """A fake Gateway whose authorizer reads each JWT's own claims, like the CUSTOM_JWT authorizer.

    A JWT is refused when it is past its `exp` at the simulated clock or lacks `*/invoke`; a valid
    JWT is admitted. Non-JWT tokens (the provisioner's stand-in) fall through to `_FakeCaller`.
    """

    def __init__(self, clock: _SimClock, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._clock = clock

    def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> McpResult:
        if token is not None and token.count(".") == 2 and method == "tools/call":
            self.calls.append((method, dict(params), token))
            claims = _authorizer_claims(token)
            scopes = str(claims.get("scope") or "").split()
            if claims["exp"] <= self._clock() or not any(s.endswith("/invoke") for s in scopes):
                return _rpc_refusal()
            return _tool_call_success()
        return super().__call__(method, params, token)


class _OrderedProvisioner(_FakeProvisioner):
    """Records into a shared event log when it provisions."""

    def __init__(self, events: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._events = events

    def provision(self) -> ServicePrincipalFixture:
        self._events.append("provision_service_principal")
        return super().provision()


class _FakeUserProvisioner:
    """Stands in for `LiveBareMfaUserProvisioner`, returning a fixed access token."""

    def __init__(self, events: list[str], token: str, *, fail: bool = False) -> None:
        self._events = events
        self._token = token
        self._fail = fail
        self.cleaned_up = False

    def provision(self) -> Any:
        from scripts.smoke.mfa_user import BareMfaUser

        self._events.append("provision_underscoped_user")
        if self._fail:
            raise RuntimeError("MFA enrollment failed")
        return BareMfaUser(
            username="uuid-user",
            access_token=self._token,
            auth_factor="password-only (pool MfaConfiguration=OPTIONAL, so no MFA challenge "
            "was issued)",
        )

    def cleanup(self) -> list[CheckResult]:
        self.cleaned_up = True
        return [
            CheckResult(
                resource="underscoped_smoke_user", check="cognito_user_deleted", status="pass"
            )
        ]


def _self_minting_run(
    *,
    minter_fails: bool = False,
    user_fails: bool = False,
    caller_factory: Any = None,
) -> tuple[dict[str, CheckResult], list[str], _SimClock, _FakeUserProvisioner, _FakeProvisioner]:
    events: list[str] = []
    clock = _SimClock()

    def _minter() -> str:
        events.append("mint_expired_token")
        if minter_fails:
            raise RuntimeError(
                "token endpoint refused the client_credentials request with HTTP 400"
            )
        return _fresh_machine_token(now=clock())

    provisioner = _OrderedProvisioner(events)
    user_provisioner = _FakeUserProvisioner(events, _end_user_access_token(), fail=user_fails)
    caller = (caller_factory or _AuthorizingCaller)(clock, tools_list=_tools_list_result())
    results = list(
        checks(
            context_for_test(outputs=_OUTPUTS),
            mcp_caller=caller,
            provisioner=provisioner,
            user_provisioner=user_provisioner,  # type: ignore[arg-type]
            expired_token_minter=_minter,
            sleeper=clock.sleep,
            clock=clock,
            jwks_url=test_key.jwks_url(),
        )
    )
    by_check = {result.check: result for result in results}
    return by_check, events, clock, user_provisioner, provisioner


def test_the_provisioned_path_mints_both_tokens_and_both_refusals_pass() -> None:
    """With no operator token, both refusal rows observe a real refusal of a real token."""
    results, events, clock, user_provisioner, provisioner = _self_minting_run()

    for check in (
        "provision_service_principal",
        "provision_underscoped_user",
        "expired_token_genuinely_expired",
        "expired_token_refused",
        "underscoped_token_lacks_gateway_scope",
        "underscoped_token_refused",
        "cognito_user_deleted",
        "tenant_left_clean",
    ):
        assert results[check].status == "pass", (check, results[check].detail)
    assert "supplied" not in results["expired_token_refused"].detail
    assert clock.slept and clock.now > _T0 + 300, "the expiry check never waited out the lifetime"
    assert user_provisioner.cleaned_up and provisioner.cleaned_up


def test_the_expired_token_is_minted_before_anything_is_provisioned() -> None:
    """The to-be-expired token is minted first, so the run's own work ages it."""
    _results, events, _clock, _u, _p = _self_minting_run()
    assert events[0] == "mint_expired_token", events
    assert set(events) == {
        "mint_expired_token",
        "provision_underscoped_user",
        "provision_service_principal",
    }


def test_a_gateway_that_accepts_an_expired_token_fails_the_refusal() -> None:
    """The refusal row is not vacuous: an authorizer that ignores `exp` turns it red."""

    class _IgnoresExpiry(_AuthorizingCaller):
        def __init__(self, clock: _SimClock, **kwargs: Any) -> None:
            super().__init__(_SimClock(start=0.0), **kwargs)

    results, *_ = _self_minting_run(caller_factory=_IgnoresExpiry)
    assert results["expired_token_genuinely_expired"].status == "pass"
    assert results["expired_token_refused"].status == "fail"


def test_a_failed_expired_token_mint_is_reported_in_the_refusal_detail() -> None:
    results, *_ = _self_minting_run(minter_fails=True)
    assert results["expired_token_genuinely_expired"].status == "fail"
    refusal = results["expired_token_refused"]
    assert refusal.status == "fail"
    assert "HTTP 400" in refusal.detail


def test_a_failed_underscoped_user_provision_is_reported_and_still_cleaned_up() -> None:
    results, _events, _clock, user_provisioner, _p = _self_minting_run(user_fails=True)
    assert results["provision_underscoped_user"].status == "fail"
    assert "MFA enrollment failed" in results["provision_underscoped_user"].detail
    assert results["underscoped_token_refused"].status == "fail"
    assert user_provisioner.cleaned_up


def test_a_still_valid_token_fails_the_expiry_premise() -> None:
    """Presenting a token inside its lifetime would test nothing about expiry."""
    from scripts.smoke.gateway_tools import token_genuinely_expired_check

    token = _fresh_machine_token(now=_T0)
    result = token_genuinely_expired_check(
        token, now=_T0 + 10, resource="gateway_tools", verifier=_verifiers().expired
    )
    assert result.status == "fail"
    assert "still valid" in result.detail


def test_a_token_carrying_the_gateway_scope_fails_the_underscoped_premise() -> None:
    from scripts.smoke.gateway_tools import underscoped_token_lacks_gateway_scope_check

    result = underscoped_token_lacks_gateway_scope_check(
        _fresh_machine_token(now=_T0, client_id=test_key.CLIENT_ID),
        resource="gateway_tools",
        verifier=_verifiers().underscoped,
    )
    assert result.status == "fail"


def test_no_check_detail_carries_a_minted_token() -> None:
    results, *_ = _self_minting_run()
    underscoped = _end_user_access_token()
    for result in results.values():
        assert underscoped not in result.detail
        assert ".ey" not in result.detail


def test_main_wires_the_minter_and_the_user_provisioner(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no operator tokens, `main` hands `checks` both token sources and the provisioner."""
    import scripts.smoke.gateway_tools as module
    from scripts.smoke.gateway_tools import LiveServicePrincipalProvisioner
    from scripts.smoke.mfa_user import LiveBareMfaUserProvisioner

    for env_var in (
        "CRM_SMOKE_GATEWAY_TOKEN",
        "CRM_SMOKE_EXPIRED_TOKEN",
        "CRM_SMOKE_UNDERSCOPED_TOKEN",
    ):
        monkeypatch.delenv(env_var, raising=False)
    outputs_path = tmp_path / "tf-outputs.json"
    outputs_path.write_text(json.dumps({k: {"value": v} for k, v in _OUTPUTS.items()}))
    captured: dict[str, Any] = {}

    def _record(_ctx: Any, **kwargs: Any) -> list[CheckResult]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(module, "checks", _record)
    module.main(["--json", "--outputs", str(outputs_path)])
    assert captured, "main never reached checks"
    assert captured["token"] is None
    assert isinstance(captured["provisioner"], LiveServicePrincipalProvisioner)
    assert isinstance(captured["user_provisioner"], LiveBareMfaUserProvisioner)
    assert callable(captured["expired_token_minter"])
    assert callable(captured["denial_log_reader"])


def test_the_underscoped_user_is_cleaned_up_even_when_its_provision_is_interrupted() -> None:
    """The end user is provisioned inside the cleanup `finally`, so an interrupt raised after
    `admin_create_user` still deletes it and still tears the tenant down.
    """
    events: list[str] = []

    class _Interrupted(_FakeUserProvisioner):
        def provision(self) -> Any:
            raise KeyboardInterrupt

    user_provisioner = _Interrupted(events, _end_user_access_token())
    provisioner = _OrderedProvisioner(events)
    clock = _SimClock()
    with pytest.raises(KeyboardInterrupt):
        list(
            checks(
                context_for_test(outputs=_OUTPUTS),
                mcp_caller=_AuthorizingCaller(clock, tools_list=_tools_list_result()),
                provisioner=provisioner,
                user_provisioner=user_provisioner,  # type: ignore[arg-type]
                expired_token_minter=lambda: _fresh_machine_token(now=clock()),
                sleeper=clock.sleep,
                clock=clock,
                jwks_url=test_key.jwks_url(),
            )
        )
    assert user_provisioner.cleaned_up and provisioner.cleaned_up


# --------------------------------------------------------------------------------------
# The premise checks read only verified claims
# --------------------------------------------------------------------------------------


def test_an_expired_token_within_the_leeway_is_verified_and_passes() -> None:
    from scripts.smoke.gateway_tools import token_genuinely_expired_check

    now = time.time()
    token = test_key.access_token(
        iat=int(now) - 3900, exp=int(now) - 3600, client_id=_MCP_CLIENT_ID
    )
    result = token_genuinely_expired_check(
        token, now=now, resource="gateway_tools", verifier=_verifiers().expired
    )
    assert result.status == "pass", result.detail
    assert "signature, iss, token_use and client_id verified" in result.detail


def test_a_token_older_than_the_leeway_fails_with_a_named_reason() -> None:
    from scripts.smoke.gateway_tools import (
        EXPIRED_TOKEN_MAX_AGE_SECONDS,
        token_genuinely_expired_check,
    )

    now = time.time()
    exp = int(now) - EXPIRED_TOKEN_MAX_AGE_SECONDS - 60
    token = test_key.access_token(iat=exp - 300, exp=exp, client_id=_MCP_CLIENT_ID)
    result = token_genuinely_expired_check(
        token, now=now, resource="gateway_tools", verifier=_verifiers().expired
    )
    assert result.status == "fail"
    assert result.detail.startswith("older_than_verification_leeway")
    assert EXPIRED_TOKEN_MAX_AGE_SECONDS == 7 * 24 * 3600


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda: test_key.TestKey(kid="stranger").sign(
            test_key.access_token_claims(exp=int(time.time()) - 60, client_id=_MCP_CLIENT_ID)
        ),
        lambda: test_key.access_token(exp=int(time.time()) - 60),
        lambda: test_key.access_token(
            exp=int(time.time()) - 60,
            client_id=_MCP_CLIENT_ID,
            token_use=USE_ID,
        ),
    ],
    ids=["unknown-key", "spa-client", "id-token-use"],
)
def test_an_expired_token_that_fails_verification_fails_the_premise(token_factory: Any) -> None:
    from scripts.smoke.gateway_tools import token_genuinely_expired_check

    result = token_genuinely_expired_check(
        token_factory(), now=time.time(), resource="gateway_tools", verifier=_verifiers().expired
    )
    assert result.status == "fail"
    assert "failed verification" in result.detail


def test_an_underscoped_token_with_a_tampered_signature_fails_the_premise() -> None:
    from scripts.smoke.gateway_tools import underscoped_token_lacks_gateway_scope_check

    head, _, _sig = _end_user_access_token().rpartition(".")
    result = underscoped_token_lacks_gateway_scope_check(
        f"{head}.AAAA", resource="gateway_tools", verifier=_verifiers().underscoped
    )
    assert result.status == "fail"
    assert "could not verify" in result.detail


def test_the_expiry_wait_reads_exp_through_the_verifier() -> None:
    from scripts.smoke.gateway_tools import wait_out_token_lifetime

    clock = _SimClock()
    forged = test_key.TestKey(kid="stranger").sign(
        test_key.access_token_claims(exp=int(_T0) + 300, client_id=_MCP_CLIENT_ID)
    )
    wait_out_token_lifetime(forged, sleeper=clock.sleep, clock=clock, verifier=_verifiers().expired)
    assert clock.slept == [], "an unverifiable token's exp must not drive the wait"
    genuine = _fresh_machine_token(now=_T0)
    wait_out_token_lifetime(
        genuine, sleeper=clock.sleep, clock=clock, verifier=_verifiers().expired
    )
    assert clock.slept and clock.now > _T0 + 300


# ---------------------------------------------------------------------------
# The service principal's `reps` row survives a smoke run: a pre-existing row is restored
# exactly, an absent one is deleted, and an unreadable one stops the run before any write.
# ---------------------------------------------------------------------------

_REPS_TABLE = "crm-agent-evalreps"
_PRINCIPAL = "svc-mcp-gateway"
_OPERATOR_ROW: dict[str, Any] = {
    "rep_id": {"S": _PRINCIPAL},
    "tenant": {"S": "demo"},
    "book": {"L": [{"S": "acct-demo-0001"}, {"S": "acct-demo-0002"}]},
    "full_visibility": {"BOOL": False},
    "status": {"S": "active"},
    "name": {"S": "Operator-written service principal"},
}


class _RawDynamo:
    """The `reps` table at the wire level; each failure switch breaks exactly one call."""

    def __init__(self, existing: Mapping[str, Any] | None = None) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        if existing is not None:
            self.items[_PRINCIPAL] = copy.deepcopy(dict(existing))
        self.fail_first_get = False
        self.fail_restore_put = False
        self.calls: list[str] = []
        self._puts = 0

    def get_item(self, *, TableName: str, Key: Mapping[str, Any], **_: Any) -> dict[str, Any]:  # noqa: N803
        assert TableName == _REPS_TABLE
        self.calls.append("get_item")
        if self.fail_first_get and self.calls.count("get_item") == 1:
            raise RuntimeError("simulated read failure")
        item = self.items.get(Key["rep_id"]["S"])
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, *, TableName: str, Item: Mapping[str, Any], **_: Any) -> dict[str, Any]:  # noqa: N803
        assert TableName == _REPS_TABLE
        self.calls.append("put_item")
        self._puts += 1
        if self.fail_restore_put and self._puts == 2:
            raise RuntimeError("simulated restore failure")
        self.items[Item["rep_id"]["S"]] = copy.deepcopy(dict(Item))
        return {}

    def delete_item(self, *, TableName: str, Key: Mapping[str, Any], **_: Any) -> dict[str, Any]:  # noqa: N803
        assert TableName == _REPS_TABLE
        self.calls.append("delete_item")
        self.items.pop(Key["rep_id"]["S"], None)
        return {}


class _FakeLambda:
    def get_function_configuration(self, **_: Any) -> dict[str, Any]:
        return {"Environment": {"Variables": {"GATEWAY_SERVICE_PRINCIPAL_REP_ID": _PRINCIPAL}}}


def _live_provisioner(monkeypatch: pytest.MonkeyPatch, dynamo: _RawDynamo) -> tuple[Any, list[str]]:
    """A real `LiveServicePrincipalProvisioner` over `dynamo`, tenant helpers stubbed."""
    from scripts.smoke import gateway_tools
    from scripts.smoke.tenant import SmokeTenant

    events: list[str] = []
    tenant = SmokeTenant(run_id="smoke-0123456789ab", tenant="eval-smoke-0123456789ab")

    def allocate(_store: Any) -> SmokeTenant:
        events.append("allocate")
        return tenant

    def seed(*_args: Any, **_kwargs: Any) -> None:
        events.append("seed")

    def teardown(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        events.append("teardown")
        return {}

    monkeypatch.setattr(gateway_tools, "allocate_smoke_tenant", allocate)
    monkeypatch.setattr(gateway_tools, "seed_smoke_tenant", seed)
    monkeypatch.setattr(gateway_tools, "teardown_smoke_tenant", teardown)
    ctx = context_for_test(
        outputs={"dynamodb_table_names": {"eval_runs": "crm-agent-evaleval_runs"}},
        clients={"dynamodb": dynamo, "lambda": _FakeLambda()},
    )
    provisioner = gateway_tools.LiveServicePrincipalProvisioner(ctx)
    monkeypatch.setattr(provisioner, "_seeded_account", lambda _s, t, i: f"acct-{t}-{i:04d}")
    monkeypatch.setattr(provisioner, "_mint_machine_token", lambda: "machine-token")
    return provisioner, events


def test_a_pre_existing_principal_row_is_restored_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dynamo = _RawDynamo(existing=_OPERATOR_ROW)
    provisioner, _ = _live_provisioner(monkeypatch, dynamo)
    fixture = provisioner.provision()
    assert dynamo.items[_PRINCIPAL]["tenant"] == {"S": fixture.tenant}, "the run's own row is live"
    rows = list(provisioner.cleanup())
    restored = next(r for r in rows if r.resource == "service_principal")
    assert (restored.check, restored.status) == ("reps_row_restored", "pass"), restored.detail
    assert dynamo.items[_PRINCIPAL] == _OPERATOR_ROW
    assert "delete_item" not in dynamo.calls


def test_an_absent_principal_row_is_deleted_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    dynamo = _RawDynamo()
    provisioner, _ = _live_provisioner(monkeypatch, dynamo)
    provisioner.provision()
    rows = list(provisioner.cleanup())
    deleted = next(r for r in rows if r.resource == "service_principal")
    assert (deleted.check, deleted.status) == ("reps_row_deleted", "pass"), deleted.detail
    assert _PRINCIPAL not in dynamo.items


def test_an_unreadable_snapshot_stops_the_run_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dynamo = _RawDynamo(existing=_OPERATOR_ROW)
    dynamo.fail_first_get = True
    provisioner, events = _live_provisioner(monkeypatch, dynamo)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        provisioner.provision()
    assert events == [] and dynamo.calls == ["get_item"], (events, dynamo.calls)
    assert list(provisioner.cleanup()) == []
    assert dynamo.items[_PRINCIPAL] == _OPERATOR_ROW


def test_a_failed_restore_fails_and_names_the_row_to_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dynamo = _RawDynamo(existing=_OPERATOR_ROW)
    dynamo.fail_restore_put = True
    provisioner, _ = _live_provisioner(monkeypatch, dynamo)
    provisioner.provision()
    rows = list(provisioner.cleanup())
    restored = next(r for r in rows if r.resource == "service_principal")
    assert (restored.check, restored.status) == ("reps_row_restored", "fail")
    assert "simulated restore failure" in restored.detail
    for fragment in ('"tenant": "demo"', '"acct-demo-0001"', '"full_visibility": false'):
        assert fragment in restored.detail, restored.detail
    assert any(r.check == "tenant_left_clean" for r in rows), "the tenant teardown still runs"


def test_a_restore_whose_read_back_differs_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    dynamo = _RawDynamo(existing=_OPERATOR_ROW)
    provisioner, _ = _live_provisioner(monkeypatch, dynamo)
    provisioner.provision()
    real_put = dynamo.put_item

    def lossy_put(*, TableName: str, Item: Mapping[str, Any], **kw: Any) -> dict[str, Any]:  # noqa: N803
        return real_put(TableName=TableName, Item={k: v for k, v in Item.items() if k != "name"})

    monkeypatch.setattr(dynamo, "put_item", lossy_put)
    restored = next(r for r in provisioner.cleanup() if r.resource == "service_principal")
    assert restored.status == "fail" and "differs from the snapshot" in restored.detail


@pytest.mark.parametrize("existing", [_OPERATOR_ROW, None], ids=["row-present", "row-absent"])
def test_a_run_that_fails_before_writing_the_row_leaves_it_untouched(
    monkeypatch: pytest.MonkeyPatch, existing: Mapping[str, Any] | None
) -> None:
    """Cleanup touches the `reps` row only if this run wrote it."""
    from scripts.smoke import gateway_tools

    dynamo = _RawDynamo(existing=existing)
    provisioner, _ = _live_provisioner(monkeypatch, dynamo)

    def broken_seed(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated seeding failure")

    monkeypatch.setattr(gateway_tools, "seed_smoke_tenant", broken_seed)
    with pytest.raises(RuntimeError, match="seeding"):
        provisioner.provision()
    assert not [r for r in provisioner.cleanup() if r.resource == "service_principal"]
    assert dynamo.calls == ["get_item"], dynamo.calls


_SECRET_ENV = "CRM_MCP_CLIENT_SECRET_FILE"  # nosec B105  # environment variable name, not a credential
_OUTPUT_SECRET = "output-secret-SENTINEL-4444"  # gitleaks:allow  # pragma: allowlist secret  # nosec B105  # planted test sentinel, not a credential
_FILE_SECRET = "file-secret-SENTINEL-5555"  # gitleaks:allow  # pragma: allowlist secret  # nosec B105  # planted test sentinel, not a credential


def _secret_ctx(secret: object = _OUTPUT_SECRET) -> Any:
    outputs: dict[str, Any] = {
        "cognito_token_endpoint": "https://auth.example/oauth2/token",  # nosec B105  # fake token endpoint URL, not a credential
        "cognito_mcp_client_id": "client",
    }
    if secret is not None:
        outputs["cognito_mcp_client_secret"] = secret
    return context_for_test(outputs=outputs, clients={})


def test_the_secret_file_named_by_the_env_var_wins_over_the_output_and_is_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from scripts.smoke import gateway_tools

    secret_file = tmp_path / "mcp-client-secret"
    secret_file.write_text(f"  {_FILE_SECRET}\n", encoding="utf-8")
    monkeypatch.setenv(_SECRET_ENV, str(secret_file))
    assert gateway_tools.MCP_CLIENT_SECRET_FILE_ENV == _SECRET_ENV
    assert gateway_tools.mcp_client_secret(_secret_ctx()) == _FILE_SECRET


def test_with_the_env_var_unset_the_output_supplies_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.smoke import gateway_tools

    monkeypatch.delenv(_SECRET_ENV, raising=False)
    assert gateway_tools.mcp_client_secret(_secret_ctx()) == _OUTPUT_SECRET


@pytest.mark.parametrize("contents", [None, "", "  \n"], ids=["missing", "empty", "blank"])
def test_an_unreadable_or_empty_secret_file_fails_naming_the_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, contents: str | None
) -> None:
    from scripts.smoke import gateway_tools

    secret_file = tmp_path / "mcp-client-secret"
    if contents is not None:
        secret_file.write_text(contents, encoding="utf-8")
    monkeypatch.setenv(_SECRET_ENV, str(secret_file))
    with pytest.raises(RuntimeError, match=_SECRET_ENV) as raised:
        gateway_tools.mcp_client_secret(_secret_ctx())
    assert _OUTPUT_SECRET not in str(raised.value)


@pytest.mark.parametrize("secret", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_an_empty_or_absent_output_secret_fails_naming_the_env_var(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    from scripts.smoke import gateway_tools

    monkeypatch.delenv(_SECRET_ENV, raising=False)
    with pytest.raises(RuntimeError, match=_SECRET_ENV):
        gateway_tools.mcp_client_secret(_secret_ctx(secret))


def test_the_machine_token_is_minted_with_the_file_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import base64

    from scripts.smoke import gateway_tools

    secret_file = tmp_path / "mcp-client-secret"
    secret_file.write_text(_FILE_SECRET, encoding="utf-8")
    monkeypatch.setenv(_SECRET_ENV, str(secret_file))
    sent: list[str] = []

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"access_token": "machine-token"}).encode()  # nosec B105  # fake token-endpoint response, not a credential

    def fake_open(request: Any, timeout: float) -> _Response:
        sent.append(request.get_header("Authorization"))
        return _Response()

    monkeypatch.setattr(gateway_tools, "open_https", fake_open)
    assert gateway_tools.mint_machine_token(_secret_ctx("")) == "machine-token"
    expected = base64.b64encode(f"client:{_FILE_SECRET}".encode()).decode("ascii")
    assert sent == [f"Basic {expected}"]


def test_an_empty_output_with_no_secret_file_never_reaches_the_token_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.smoke import gateway_tools

    monkeypatch.delenv(_SECRET_ENV, raising=False)

    def refuse_open(request: Any, timeout: float) -> Any:
        raise AssertionError("the token endpoint must not be called without a secret")

    monkeypatch.setattr(gateway_tools, "open_https", refuse_open)
    with pytest.raises(RuntimeError, match=_SECRET_ENV):
        gateway_tools.mint_machine_token(_secret_ctx(""))


# --------------------------------------------------------------------------------------
# A JSON-RPC array body answers no tool call
# --------------------------------------------------------------------------------------

_ARRAY_ROW = "array_body_answers_no_tool_call"
_ARRAY_ROWS = (_ARRAY_ROW, "array_body_reaches_no_tool")


def _array_rows(raw: _FakeRawCaller, *, token: str | None = "tok") -> dict[str, CheckResult]:  # nosec B107  # test placeholder, not a credential
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token=token, raw_caller=raw)
    rows = {
        check: [r for r in results if r.resource == "gateway_tools" and r.check == check]
        for check in _ARRAY_ROWS
    }
    for check, matches in rows.items():
        assert len(matches) == 1, (check, matches)
    return {check: matches[0] for check, matches in rows.items()}


def test_the_old_interceptor_refusal_row_is_gone() -> None:
    caller = _FakeCaller(tools_list=_tools_list_result())
    results = _run(caller, token="tok", raw_caller=_FakeRawCaller())  # nosec B106  # test placeholder, not a credential
    assert not [r for r in results if r.check == "array_body_refused_by_interceptor"]


def test_an_error_reply_passes_records_its_code_and_no_tool_is_deferred() -> None:
    rows = _array_rows(_FakeRawCaller())
    assert rows[_ARRAY_ROW].status == "pass"
    assert "error.code=-32001" in rows[_ARRAY_ROW].detail
    assert rows["array_body_reaches_no_tool"].status == "defer"
    assert rows["array_body_reaches_no_tool"].defer_reason == "no_per_request_invocation_signal"


def test_the_array_body_is_one_well_formed_tool_call_sent_with_the_token() -> None:
    raw = _FakeRawCaller()
    _array_rows(raw)
    assert len(raw.calls) == 1
    body, token = raw.calls[0]
    assert token == "tok"  # nosec B105  # test placeholder, not a credential
    decoded = json.loads(body)
    assert isinstance(decoded, list) and len(decoded) == 1
    (call,) = decoded
    assert call["method"] == "tools/call"
    assert call["params"]["arguments"]["account_id"] == _IN_BOOK_ACCOUNT


@pytest.mark.parametrize(
    ("reply", "recorded"),
    [
        (
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
            "error.code=-32600",
        ),
        ([_rpc_refusal()], "array[1]"),
        ({"jsonrpc": "2.0", "id": "smoke-array-body", "result": {}}, "result:object{}"),
        ([], "array[0]"),
        (
            {
                "jsonrpc": "2.0",
                "id": "smoke-array-body",
                "result": {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": "InternalServerException - Tool execution failed"}
                    ],
                },
            },
            "result.isError=True",
        ),
        (
            [
                {"jsonrpc": "2.0", "id": "a", "result": {"isError": True, "content": []}},
                _rpc_refusal(),
            ],
            "array[2]",
        ),
    ],
    ids=[
        "other-error-code",
        "batch-of-errors",
        "result-without-tool-output",
        "empty-batch",
        "gateway-tool-error",
        "batch-tool-error-and-rpc-error",
    ],
)
def test_a_reply_without_tool_output_passes_and_records_its_shape(
    reply: RawResult, recorded: str
) -> None:
    rows = _array_rows(_FakeRawCaller(reply))
    assert rows[_ARRAY_ROW].status == "pass", rows[_ARRAY_ROW].detail
    assert recorded in rows[_ARRAY_ROW].detail


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        ([_tool_call_success()], "tool-call output"),
        (_tool_call_success(), "tool-call output"),
        (
            {"jsonrpc": "2.0", "id": "x", "result": {"structuredContent": {"a": 1}}},
            "structuredContent",
        ),
        (
            {"jsonrpc": "2.0", "id": "x", "result": {"isError": False, "content": []}},
            "without `isError: true`",
        ),
        ({"jsonrpc": "2.0", "id": "x", "result": {"content": [{"type": "text"}]}}, "content"),
        (
            [
                {"jsonrpc": "2.0", "id": "a", "result": {"isError": True, "content": []}},
                _tool_call_success(),
            ],
            "tool-call output",
        ),
        ("transport error calling https://gateway.example.invalid/mcp: reset", "transport"),
        (["not-an-object"], "not a JSON object"),
    ],
    ids=[
        "batch-answered",
        "answered",
        "structured",
        "is-error-false",
        "content-without-is-error",
        "batch-one-refused-one-answered",
        "transport",
        "scalar",
    ],
)
def test_a_reply_with_tool_output_or_none_to_judge_fails_only_the_array_row(
    reply: RawResult, fragment: str
) -> None:
    rows = _array_rows(_FakeRawCaller(reply))
    assert rows[_ARRAY_ROW].status == "fail"
    assert fragment in rows[_ARRAY_ROW].detail
    assert rows["array_body_reaches_no_tool"].status == "defer"


def test_a_gateway_tool_error_reply_records_no_error_text() -> None:
    reply = {
        "jsonrpc": "2.0",
        "id": "smoke-array-body",
        "result": {
            "isError": True,
            "content": [
                {"type": "text", "text": "InternalServerException - Tool execution failed"}
            ],
        },
    }
    detail = _array_rows(_FakeRawCaller(reply))[_ARRAY_ROW].detail
    assert "InternalServerException" not in detail
    assert "Tool execution failed" not in detail


def test_the_recorded_shape_carries_no_reply_values() -> None:
    rows = _array_rows(_FakeRawCaller([_tool_call_success(rep_id="rep-secret-value")]))
    detail = rows[_ARRAY_ROW].detail
    assert "rep-secret-value" not in detail
    assert _IN_BOOK_ACCOUNT not in detail
    assert "Authentication failed" not in _array_rows(_FakeRawCaller())[_ARRAY_ROW].detail


def test_no_token_fails_the_array_body_row_without_sending_it() -> None:
    raw = _FakeRawCaller()
    rows = _array_rows(raw, token=None)
    assert rows[_ARRAY_ROW].status == "fail"
    assert raw.calls == []


# --------------------------------------------------------------------------------------
# The interceptor's denial decisions are logged at WARNING.
# --------------------------------------------------------------------------------------

from scripts.smoke import gateway_tools as _gateway_tools  # noqa: E402

_WARN_LINE = (
    '[WARNING]\t2026-11-08T00:00:00Z\treq-1\t{"event": "gateway_auth_denied", "reason": "x"}'
)
_INFO_LINE = '[INFO]\t2026-11-08T00:00:00Z\treq-1\t{"event": "gateway_auth_denied", "reason": "x"}'


class _Reader:
    def __init__(self, pages: list[list[str]]) -> None:
        self.pages = pages
        self.starts: list[int] = []

    def __call__(self, start_ms: int) -> list[str]:
        self.starts.append(start_ms)
        return self.pages[min(len(self.starts) - 1, len(self.pages) - 1)]


def _denial_row(reader: Any) -> CheckResult:
    sleeps: list[float] = []
    return _gateway_tools._check_interceptor_denial_logged(
        reader, start_ms=1_000, sleeper=sleeps.append
    )


def test_a_denial_at_warning_passes() -> None:
    row = _denial_row(_Reader([[_WARN_LINE]]))
    assert row.status == "pass"


def test_a_warning_that_appears_on_a_later_poll_passes() -> None:
    reader = _Reader([[], [], [_WARN_LINE]])
    assert _denial_row(reader).status == "pass"
    assert reader.starts == [1_000, 1_000, 1_000]


def test_denials_only_at_info_fail() -> None:
    row = _denial_row(_Reader([[_INFO_LINE]]))
    assert row.status == "fail"
    assert "INFO" in row.detail


def test_a_json_format_warning_passes() -> None:
    line = '{"level": "WARN", "message": "{\\"event\\": \\"gateway_service_identity_absent\\"}"}'
    assert _denial_row(_Reader([[line]])).status == "pass"


def test_no_denial_line_defers_naming_the_unreached_path() -> None:
    reader = _Reader([["[WARNING]\tunrelated line"]])
    row = _denial_row(reader)
    assert row.status == "defer"
    assert row.defer_reason == "interceptor_denial_not_reached"
    assert len(reader.starts) == _gateway_tools.DENIAL_LOG_POLL_ATTEMPTS


def test_the_unreached_defer_states_the_observation_not_a_cause() -> None:
    row = _denial_row(_Reader([["[WARNING]\tunrelated line"]]))
    window = (
        _gateway_tools.DENIAL_LOG_POLL_ATTEMPTS - 1
    ) * _gateway_tools.DENIAL_LOG_POLL_INTERVAL_SECONDS
    assert row.detail.startswith(f"no interceptor denial line observed within {window:g} s")
    assert "expected to be refused before the interceptor" in row.detail
    assert "were refused" not in row.detail
    assert "was reached" not in row.detail


def test_an_unreadable_log_group_fails() -> None:
    def _broken(start_ms: int) -> list[str]:
        raise RuntimeError("AccessDenied")

    assert _denial_row(_broken).status == "fail"


def test_no_reader_skips_never_passes() -> None:
    assert _denial_row(None).status == "skip"


def test_checks_emit_the_denial_row_after_the_probes() -> None:
    reader = _Reader([[_WARN_LINE]])
    results = list(
        checks(
            context_for_test(outputs=_OUTPUTS),
            mcp_caller=_FakeCaller(tools_list=_tools_list_result()),
            token="tok",  # nosec B106  # test placeholder, not a credential
            denial_log_reader=reader,
        )
    )
    assert results[-1].check == "interceptor_denial_logged_at_warning"
    assert results[-1].status == "pass"


def test_the_live_entry_point_supplies_the_denial_reader() -> None:
    import inspect

    assert "denial_log_reader=live_denial_log_reader(ctx)" in inspect.getsource(_gateway_tools.main)


def test_the_array_body_premise_says_the_gateway_refuses_it_itself() -> None:
    doc = _gateway_tools.__doc__ or ""
    assert "Gateway refuses an array body itself" in doc
    assert "the Gateway refuses it itself" in (
        _gateway_tools._check_array_body_answers_no_tool_call.__doc__ or ""
    )
