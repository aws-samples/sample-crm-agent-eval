# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.mcp_chat_gateway`: Gateway #2's auth, inventory and conversation checks.

A fake Gateway stands in for the deployed one: it authenticates bearer tokens it issued, lists its
tools, and runs `chat_with_supervisor` with conversation ownership. Provisioners are fakes; no
AWS. Tokens are RS256-signed by `tests.unit._jwks_test_key` and verified against its loopback JWKS.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest
from scripts.smoke import mcp_chat_gateway as module
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.three_tier_chat import MODEL_TURN_ALLOTMENTS

from gateway import chat_target
from tests.unit import _jwks_test_key as test_key

REPO_ROOT = Path(__file__).resolve().parents[2]
_URL = "https://gw2.gateway.bedrock-agentcore.us-east-1.amazonaws.com"
_REPS = "crm-agent-evalreps"
_OUTPUTS: dict[str, Any] = {
    "mcp_chat_gateway_url": _URL,
    "agentcore_gateway_url": "https://gw1.gateway.bedrock-agentcore.us-east-1.amazonaws.com",
    "cognito_gateway_scope": "gateway/invoke",
    "cognito_user_pool_id": test_key.USER_POOL_ID,
    "cognito_spa_client_id": test_key.CLIENT_ID,
    "cognito_mcp_client_id": "test-mcp-client",
    "dynamodb_table_names": {"reps": _REPS},
}
_ADVERTISED = f"chat-with-supervisor___{module.CHAT_TOOL}"


def _token(sub: str, scope: str, *, client_output: str = "cognito_spa_client_id") -> str:
    """An access token the test pool issued to the app client named by `client_output`."""
    return test_key.access_token(
        sub=sub, scope=scope, username=sub, client_id=_OUTPUTS[client_output]
    )


def _machine_token(scope: str) -> str:
    return _token("service", scope, client_output="cognito_mcp_client_id")


_USER_A = _token("user-a", "openid supervisor/chat")
_USER_B = _token("user-b", "openid supervisor/chat")
_SERVICE = _machine_token("gateway/invoke supervisor/chat")
_INVOKE_ONLY = _machine_token("gateway/invoke")
_OWNER_OF = {_USER_A: "user-a", _USER_B: "user-b", _SERVICE: "service"}


class _FakeGateway:
    """Authenticates exactly the issued tokens, lists `tools`, owns conversations per token."""

    def __init__(self, *, tools: Iterable[str] = (_ADVERTISED,), share: bool = False) -> None:
        self.tokens = {_USER_A, _USER_B, _SERVICE}
        self.honour_forged_identity = False
        #: The `reps` keys the deployed target would write; the DynamoDB fake shares this set.
        self.store: dict[str, str] = {}
        #: When set, a chat call writes its item and then fails without returning the id.
        self.fail_after_write = False
        self.tools = list(tools)
        self.share = share
        self.owner: dict[str, str] = {}
        self.model_calls = 0
        #: When set, an argument outside the target's accepted set is ignored, not refused.
        self.accept_unknown_arguments = False
        self.calls: list[tuple[str, Mapping[str, Any], str | None]] = []

    def __call__(
        self, method: str, params: Mapping[str, Any], token: str | None
    ) -> module.McpResponse:
        self.calls.append((method, params, token))
        if token not in self.tokens:
            return 401, {"message": "Unauthorized"}
        if method == "tools/list":
            return 200, {"result": {"tools": [{"name": n} for n in self.tools]}}
        arguments = dict(params["arguments"])
        forged = arguments.pop("_agent_identity", None)
        unknown = set(arguments) - chat_target._ARGUMENT_KEYS
        if unknown and not self.accept_unknown_arguments:
            return 200, self._error("invalid_request", f"Unexpected argument(s): {sorted(unknown)}")
        caller = token
        if forged is not None and self.honour_forged_identity:
            caller = self.owner.get(str(arguments.get("conversation_id")), token)
        conversation = arguments.get("conversation_id")
        if conversation is not None and self.owner.get(conversation) != caller and not self.share:
            return 200, self._error("denied", "That conversation belongs to someone else.")
        conversation = conversation or f"conv-{uuid.uuid4().hex}"
        self.owner.setdefault(conversation, caller)
        self.store[f"conversation#{conversation}"] = _OWNER_OF.get(str(caller), "unknown")
        if self.fail_after_write:
            return 200, self._error("model_failure", "The assistant could not answer.")
        self.model_calls += 1
        return 200, self._content({"answer": "ready", "conversation_id": conversation})

    @staticmethod
    def _error(kind: str, text: str) -> dict[str, Any]:
        """`gateway.chat_target`'s error result, passed through as the MCP `result` itself."""
        return {
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": text}],
                "error_kind": kind,
            }
        }

    @staticmethod
    def _content(result: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "result": {"isError": False, "content": [{"type": "text", "text": json.dumps(result)}]}
        }


class _FakeUser:
    def __init__(self, token: str) -> None:
        self.token = token
        self.cleaned = False

    def provision(self) -> str:
        return self.token

    def cleanup(self) -> Iterable[CheckResult]:
        self.cleaned = True
        yield CheckResult(resource="smoke_user", check="user_removed", status="pass", detail="")


class _FakeService:
    def __init__(self, token: str = _SERVICE) -> None:
        self.token = token
        self.cleaned = False

    def provision(self) -> Any:
        return type("Fixture", (), {"token": self.token, "rep_id": "service"})()

    def cleanup(self) -> Iterable[CheckResult]:
        self.cleaned = True
        yield CheckResult(resource="service", check="service_removed", status="pass", detail="")


class _FakeGateway1:
    """Gateway #1: authenticates only `gateway/invoke` tokens, so a chat user token gets 401."""

    def __init__(self, *, admits_users: bool = False) -> None:
        self.admits_users = admits_users
        self.calls: list[tuple[str, str | None]] = []

    def __call__(
        self, method: str, params: Mapping[str, Any], token: str | None
    ) -> module.McpResponse:
        self.calls.append((method, token))
        if token in (_USER_A, _USER_B) and not self.admits_users:
            return 401, {"message": "Unauthorized"}
        return 200, {"result": {"tools": [{"name": "crm___get_account"}]}}


class _FakeDynamo:
    """`reps` stand-in: `items` maps each key to its `owner_rep_id`; `scan` honours the filter."""

    def __init__(self, *, sticky: bool = False, page_size: int = 1) -> None:
        self.items: dict[str, str] = {}
        self.sticky = sticky
        self.page_size = page_size
        self.deleted: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str, bool]] = []
        self.scans: list[dict[str, Any]] = []

    def scan(self, **request: Any) -> dict[str, Any]:
        self.scans.append(request)
        values = request["ExpressionAttributeValues"]
        prefix = values[":prefix"]["S"]
        owners = {v["S"] for k, v in values.items() if k.startswith(":o")}
        assert request["ConsistentRead"] is True
        matching = sorted(k for k, o in self.items.items() if k.startswith(prefix) and o in owners)
        start = int(request.get("ExclusiveStartKey", {}).get("i", 0))
        page = matching[start : start + self.page_size]
        more = start + self.page_size < len(matching)
        return {
            "Items": [{"rep_id": {"S": key}} for key in page],
            **({"LastEvaluatedKey": {"i": start + self.page_size}} if more else {}),
        }

    def delete_item(self, *, TableName: str, Key: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        key = Key["rep_id"]["S"]
        self.deleted.append((TableName, key))
        if not self.sticky:
            self.items.pop(key, None)
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool) -> Any:  # noqa: N803
        key = Key["rep_id"]["S"]
        self.reads.append((TableName, key, ConsistentRead))
        return {"Item": {"rep_id": {"S": key}}} if key in self.items else {}


def _run(
    gateway: _FakeGateway,
    *,
    service_token: str = _SERVICE,
    user_tokens: tuple[str, str] = (_USER_A, _USER_B),
    gateway1: _FakeGateway1 | None = None,
    invoke_only_token: str = _INVOKE_ONLY,
    dynamo: _FakeDynamo | None = None,
    outputs: dict[str, Any] | None = None,
) -> tuple[dict[str, CheckResult], list[Any]]:
    users = iter(user_tokens)
    made: list[Any] = []

    def user_factory(_ctx: Any) -> _FakeUser:
        user = _FakeUser(next(users))
        made.append(user)
        return user

    def service_factory(_ctx: Any) -> _FakeService:
        service = _FakeService(service_token)
        made.append(service)
        return service

    minted: list[str] = []

    def minter(_ctx: Any, scope: str) -> str:
        minted.append(scope)
        return invoke_only_token

    dynamodb = dynamo or _FakeDynamo()
    dynamodb.items = gateway.store
    ctx = context_for_test(
        outputs=_OUTPUTS if outputs is None else outputs, clients={"dynamodb": dynamodb}
    )
    rows = list(
        module.checks(
            ctx,
            mcp_caller=gateway,
            gateway1_caller=gateway1 or _FakeGateway1(),
            user_provisioner_factory=user_factory,
            service_provisioner_factory=service_factory,
            scoped_token_minter=minter,
            jwks_url=test_key.jwks_url(),
        )
    )
    assert minted in ([], ["gateway/invoke"])
    return {r.check: r for r in rows}, made


def test_a_compliant_gateway_passes_every_row_and_cleans_up() -> None:
    gateway = _FakeGateway()
    rows, made = _run(gateway)
    assert {c: r.status for c, r in rows.items()} == {
        "no_jwt_refused_401": "pass",
        "invalid_jwt_refused": "pass",
        "tools_list_exactly_chat_with_supervisor": "pass",
        "user_token_carries_chat_scope": "pass",  # nosec B105  # test placeholder, not a credential
        "temperature_argument_refused": "pass",
        "user_call_answered": "pass",
        "foreign_conversation_denied": "pass",
        "forged_agent_identity_refused": "pass",
        "gateway1_refuses_chat_user_token": "pass",  # nosec B105  # test placeholder, not a credential
        "gateway2_refuses_invoke_only_token": "pass",  # nosec B105  # test placeholder, not a credential
        "service_token_carries_chat_scope": "pass",  # nosec B105  # test placeholder, not a credential
        "service_call_answered": "pass",
        "model_backed_turns_within_envelope": "pass",
        "conversation_items_removed": "pass",
        "user_removed": "pass",
        "service_removed": "pass",
    }
    assert all(p.cleaned for p in made) and len(made) == 3


def test_the_model_backed_calls_are_exactly_this_modules_share() -> None:
    gateway = _FakeGateway()
    rows, _ = _run(gateway)
    assert gateway.model_calls == MODEL_TURN_ALLOTMENTS[module.RESOURCE_CLASS] == 2
    assert "2 model-backed turn(s)" in rows["model_backed_turns_within_envelope"].detail


def test_the_unauthenticated_probes_send_no_token_and_a_tampered_one() -> None:
    gateway = _FakeGateway()
    _run(gateway)
    probes = [token for method, _p, token in gateway.calls[:3]]
    assert probes[0] is None
    assert probes[1] == _USER_A, "the untampered control call"
    assert probes[2] is not None and probes[2] != _USER_A
    assert probes[2].rpartition(".")[0] == _USER_A.rpartition(".")[0], "same claims, new signature"


def test_a_gateway_that_accepts_any_token_reds_the_invalid_jwt_row() -> None:
    class _Lax(_FakeGateway):
        def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> Any:
            if token is not None:
                self.tokens.add(token)
            return super().__call__(method, params, token)

    rows, _ = _run(_Lax())
    assert rows["invalid_jwt_refused"].status == "fail"
    assert rows["no_jwt_refused_401"].status == "pass"


@pytest.mark.parametrize(
    "tools",
    [(), (_ADVERTISED, "other___get_account"), ("chat-with-supervisor___something_else",)],
    ids=["none", "two", "wrong-name"],
)
def test_any_other_inventory_reds_the_inventory_row(tools: tuple[str, ...]) -> None:
    rows, _ = _run(_FakeGateway(tools=tools))
    assert rows["tools_list_exactly_chat_with_supervisor"].status == "fail"


def test_a_shared_conversation_reds_the_foreign_row() -> None:
    rows, _ = _run(_FakeGateway(share=True))
    assert rows["foreign_conversation_denied"].status == "fail"


def test_a_token_without_the_chat_scope_reds_the_scope_row() -> None:
    underscoped = _machine_token("gateway/invoke")
    gateway = _FakeGateway()
    gateway.tokens.add(underscoped)
    rows, _ = _run(gateway, service_token=underscoped)
    assert rows["service_token_carries_chat_scope"].status == "fail"


def test_an_absent_url_output_fails_once() -> None:
    ctx = context_for_test(outputs={}, clients={})
    rows = list(module.checks(ctx, mcp_caller=_FakeGateway()))
    assert [(r.check, r.status) for r in rows] == [("terraform_output_present", "fail")]


def test_the_contract_constants_follow_their_sources() -> None:
    assert module.CHAT_TOOL == chat_target.TOOL_NAME
    module_tf = (REPO_ROOT / "infra" / "modules" / "mcp_chat_gateway" / "main.tf").read_text()
    assert f'name        = "{module.CHAT_TOOL}"' in module_tf
    main_tf = (REPO_ROOT / "infra" / "main.tf").read_text()
    scope = re.search(r'chat_scope_identifier\s*=\s*"([^"]+)"', main_tf)
    assert scope is not None and scope.group(1).endswith(module.CHAT_SCOPE_SUFFIX)


def test_the_machine_token_request_omits_the_scope_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[bytes] = []

    class _Response:
        status = 200

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"access_token": _SERVICE}).encode()

    def fake_open(request: Any, timeout: float) -> _Response:
        sent.append(request.data)
        return _Response()

    monkeypatch.setattr(module, "open_https", fake_open)
    ctx = context_for_test(
        outputs={
            "cognito_token_endpoint": "https://auth.example/oauth2/token",  # nosec B105  # test placeholder, not a credential
            "cognito_mcp_client_id": "client",
            "cognito_mcp_client_secret": "secret",  # pragma: allowlist secret  # nosec B105  # test placeholder, not a credential
        },
        clients={},
    )
    assert module.mint_chat_machine_token(ctx) == _SERVICE
    assert sent == [b"grant_type=client_credentials"]


def test_the_chat_machine_token_uses_the_secret_file_and_an_empty_output_alone_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import base64

    headers: list[str] = []

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"access_token": _SERVICE}).encode()

    def fake_open(request: Any, timeout: float) -> _Response:
        headers.append(request.get_header("Authorization"))
        return _Response()

    monkeypatch.setattr(module, "open_https", fake_open)
    ctx = context_for_test(
        outputs={
            "cognito_token_endpoint": "https://auth.example/oauth2/token",  # nosec B105  # test placeholder, not a credential
            "cognito_mcp_client_id": "client",
            "cognito_mcp_client_secret": "",
        },
        clients={},
    )
    monkeypatch.delenv("CRM_MCP_CLIENT_SECRET_FILE", raising=False)
    with pytest.raises(RuntimeError, match="CRM_MCP_CLIENT_SECRET_FILE"):
        module.mint_chat_machine_token(ctx)
    assert headers == []

    secret_file = tmp_path / "mcp-client-secret"
    secret_file.write_text("rotated-secret\n", encoding="utf-8")  # pragma: allowlist secret
    monkeypatch.setenv("CRM_MCP_CLIENT_SECRET_FILE", str(secret_file))
    assert module.mint_chat_machine_token(ctx) == _SERVICE
    expected = base64.b64encode(b"client:rotated-secret").decode("ascii")
    assert headers == [f"Basic {expected}"]


def test_a_target_that_honours_a_forged_identity_reds_only_the_forged_row() -> None:
    """User B naming user A in `_agent_identity` must be refused."""
    gateway = _FakeGateway()
    gateway.honour_forged_identity = True
    rows, _ = _run(gateway)
    assert rows["forged_agent_identity_refused"].status == "fail"
    assert rows["foreign_conversation_denied"].status == "pass"


def test_the_forged_call_carries_the_forged_block_and_user_bs_token() -> None:
    gateway = _FakeGateway()
    _run(gateway)
    forged = [
        (params["arguments"], token)
        for method, params, token in gateway.calls
        if method == "tools/call" and module.FORGED_IDENTITY_KEY in params["arguments"]
    ]
    assert len(forged) == 1
    arguments, token = forged[0]
    assert token == _USER_B
    assert arguments[module.FORGED_IDENTITY_KEY]["claims"]["sub"] == "user-a"


def test_neither_scope_probe_nor_the_refusals_spend_a_model_turn() -> None:
    gateway = _FakeGateway()
    rows, _ = _run(gateway)
    assert gateway.model_calls == 2
    assert "2 model-backed turn(s)" in rows["model_backed_turns_within_envelope"].detail


def test_a_target_that_ignores_a_temperature_argument_reds_only_its_row() -> None:
    gateway = _FakeGateway()
    gateway.accept_unknown_arguments = True
    rows, _ = _run(gateway)
    assert rows["temperature_argument_refused"].status == "fail"
    assert [c for c, r in rows.items() if r.status == "fail"] == ["temperature_argument_refused"]


def test_the_temperature_call_sends_only_the_refused_argument_beside_the_message() -> None:
    gateway = _FakeGateway()
    _run(gateway)
    refused = [
        (params["arguments"], token)
        for method, params, token in gateway.calls
        if method == "tools/call" and module.REFUSED_ARGUMENT in params["arguments"]
    ]
    assert refused == [({"message": module.SMOKE_MESSAGE, module.REFUSED_ARGUMENT: 0.5}, _USER_A)]
    assert module.REFUSED_ARGUMENT not in chat_target._ARGUMENT_KEYS


def test_the_refusal_kind_is_the_targets_argument_refusal() -> None:
    """The real target refuses the argument with the kind the row asserts, calling no Runtime."""
    with pytest.raises(chat_target.ChatTargetError) as raised:
        chat_target._arguments({"message": module.SMOKE_MESSAGE, module.REFUSED_ARGUMENT: 0.5})
    assert raised.value.kind == module.ARGUMENT_REFUSAL_KIND


def test_a_gateway1_that_admits_the_chat_user_token_reds_its_row() -> None:
    """The chat-scoped user token is not a Gateway #1 credential."""
    gateway1 = _FakeGateway1(admits_users=True)
    rows, _ = _run(_FakeGateway(), gateway1=gateway1)
    assert rows["gateway1_refuses_chat_user_token"].status == "fail"
    assert gateway1.calls == [("tools/list", _USER_A)]


def test_a_gateway2_that_admits_an_invoke_only_token_reds_its_row() -> None:
    gateway = _FakeGateway()
    gateway.tokens.add(_INVOKE_ONLY)
    rows, _ = _run(gateway)
    assert rows["gateway2_refuses_invoke_only_token"].status == "fail"


def test_an_invoke_only_token_that_carries_the_chat_scope_proves_nothing() -> None:
    rows, _ = _run(_FakeGateway(), invoke_only_token=_SERVICE)
    row = rows["gateway2_refuses_invoke_only_token"]
    assert row.status == "fail" and "would not show" in row.detail


def test_an_absent_gateway1_output_fails_its_row_without_crashing() -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "agentcore_gateway_url"}
    gateway = _FakeGateway()
    users = iter((_USER_A, _USER_B))
    ctx = context_for_test(outputs=outputs, clients={"dynamodb": _FakeDynamo()})
    rows = {
        r.check: r
        for r in module.checks(
            ctx,
            mcp_caller=gateway,
            user_provisioner_factory=lambda _c: _FakeUser(next(users)),
            service_provisioner_factory=lambda _c: _FakeService(),
            scoped_token_minter=lambda _c, _s: _INVOKE_ONLY,
            jwks_url=test_key.jwks_url(),
        )
    }
    assert rows["gateway1_refuses_chat_user_token"].status == "fail"
    assert "agentcore_gateway_url" in rows["gateway1_refuses_chat_user_token"].detail


def test_both_conversation_items_are_deleted_and_reread_consistently() -> None:
    """The two ownership items carry no tenant, so the module removes them itself."""
    dynamo = _FakeDynamo()
    gateway = _FakeGateway()
    rows, _ = _run(gateway, dynamo=dynamo)
    assert rows["conversation_items_removed"].status == "pass"
    assert len(dynamo.deleted) == 2 and {t for t, _k in dynamo.deleted} == {_REPS}
    assert all(k.startswith("conversation#conv-") for _t, k in dynamo.deleted)
    assert [(k, consistent) for _t, k, consistent in dynamo.reads] == [
        (k, True) for _t, k in dynamo.deleted
    ]
    assert gateway.store == {}


def test_an_item_that_survives_its_delete_reds_the_cleanup_row() -> None:
    rows, _ = _run(_FakeGateway(), dynamo=_FakeDynamo(sticky=True))
    row = rows["conversation_items_removed"]
    assert row.status == "fail" and "still present" in row.detail


def test_the_cleanup_row_is_emitted_even_when_a_check_raises() -> None:
    class _Boom(_FakeGateway):
        def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> Any:
            first_call_done = method == "tools/call" and self.model_calls
            if first_call_done and "conversation_id" not in params["arguments"]:
                raise RuntimeError("transport died")
            return super().__call__(method, params, token)

    dynamo = _FakeDynamo()
    gateway = _Boom()
    with pytest.raises(RuntimeError):
        _run(gateway, dynamo=dynamo)
    assert len(dynamo.deleted) == 1, "user A's conversation is removed despite the raise"


def test_the_floor_counts_the_two_finally_rows() -> None:
    assert module.expected_check_count(context_for_test(outputs=_OUTPUTS, clients={})).minimum == 2
    assert module.expected_check_count(context_for_test(outputs={}, clients={})).minimum == 1


def test_a_failed_turn_that_wrote_an_item_is_found_by_owner_and_fails_the_row() -> None:
    """No returned id must not read as nothing to remove."""
    gateway = _FakeGateway()
    gateway.fail_after_write = True
    dynamo = _FakeDynamo()
    rows, _ = _run(gateway, dynamo=dynamo)
    row = rows["conversation_items_removed"]
    assert row.status == "fail" and "returned no conversation id" in row.detail
    assert gateway.store == {}, "the leaked item is still removed, found by its owner"
    assert {k for _t, k in dynamo.deleted} and all(
        k.startswith("conversation#conv-") for _t, k in dynamo.deleted
    )


def test_the_scan_filters_by_every_smoke_principal_and_pages() -> None:
    dynamo = _FakeDynamo(page_size=1)
    gateway = _FakeGateway()
    gateway.store["conversation#conv-" + "f" * 32] = "user-b"
    gateway.store["conversation#conv-" + "e" * 32] = "someone-else"
    rows, _ = _run(gateway, dynamo=dynamo)
    assert rows["conversation_items_removed"].status == "pass"
    owners = {
        v["S"]
        for k, v in dynamo.scans[0]["ExpressionAttributeValues"].items()
        if k.startswith(":o")
    }
    assert owners == {"user-a", "user-b", "service"}
    assert len(dynamo.scans) >= 2, "a multi-page scan must follow LastEvaluatedKey"
    assert gateway.store == {"conversation#conv-" + "e" * 32: "someone-else"}


def test_an_unknown_owner_rep_id_fails_the_row() -> None:
    token = test_key.access_token(sub="user-a", scope="openid supervisor/chat")
    gateway = _FakeGateway()
    gateway.tokens.add(token)
    _OWNER_OF[token] = "user-a"
    try:
        rows, _ = _run(gateway, user_tokens=(token, _USER_B))
    finally:
        del _OWNER_OF[token]
    row = rows["conversation_items_removed"]
    assert row.status == "fail" and "no known rep id" in row.detail


class _Gateway403(_FakeGateway):
    """Answers every refused token 403 insufficient_scope, as the deployed AgentCore Gateway does."""

    def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> Any:
        status, body = super().__call__(method, params, token)
        if status == 401 and token is not None:
            return 403, {"error": {"code": -32002, "message": "insufficient_scope - ..."}}
        return status, body


def test_a_403_for_a_replaced_signature_passes_and_records_the_finding() -> None:
    """Refused, and the control shows the signature alone made the difference."""
    rows, _ = _run(_Gateway403())
    row = rows["invalid_jwt_refused"]
    assert row.status == "pass", row.detail
    assert "FINDING" in row.detail and "403 insufficient_scope" in row.detail
    assert "valid signature -> 200" in row.detail


def test_the_invalid_signature_row_fails_when_its_control_is_not_admitted() -> None:
    class _RefuseUserA(_FakeGateway):
        def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> Any:
            if method == "tools/list" and token == _USER_A:
                return 403, {"error": {"code": -32002}}
            return super().__call__(method, params, token)

    rows, _ = _run(_RefuseUserA())
    row = rows["invalid_jwt_refused"]
    assert row.status == "fail" and "control" in row.detail


@pytest.mark.parametrize("status", [401, 403])
def test_wrong_scope_refusals_pass_on_401_or_403(status: int) -> None:
    class _Scoped(_FakeGateway):
        def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> Any:
            if token == _INVOKE_ONLY:
                return status, {"error": {"code": -32002}}
            return super().__call__(method, params, token)

    gateway1 = _FakeGateway1()
    rows, _ = _run(_Scoped(), gateway1=gateway1)
    assert rows["gateway2_refuses_invoke_only_token"].status == "pass"
    assert rows["gateway1_refuses_chat_user_token"].status == "pass"


def test_the_denials_read_error_kind_from_the_mcp_result_itself() -> None:
    """The target's error is the MCP `result`; its content text is prose, not JSON."""
    rows, _ = _run(_FakeGateway())
    for check in ("foreign_conversation_denied", "forged_agent_identity_refused"):
        assert rows[check].status == "pass", rows[check].detail
        assert "error_kind='denied'" in rows[check].detail


def test_a_refusal_status_that_still_carries_a_tools_list_result_fails() -> None:
    """Refused means 401/403 AND no `tools/list` result in the body."""

    class _LeakyRefusal(_FakeGateway):
        def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> Any:
            if token is not None and token not in self.tokens:
                return 403, {"result": {"tools": [{"name": _ADVERTISED}]}}
            return super().__call__(method, params, token)

    rows, _ = _run(_LeakyRefusal())
    row = rows["invalid_jwt_refused"]
    assert row.status == "fail" and "no tools/list result" in row.detail


@pytest.mark.parametrize(
    "check", ["gateway1_refuses_chat_user_token", "gateway2_refuses_invoke_only_token"]
)
def test_a_scope_refusal_that_still_carries_a_tools_list_result_fails(check: str) -> None:
    """The scope rows apply `invalid_jwt_refused`'s no-`result` leak predicate too."""
    leak = {"result": {"tools": [{"name": "crm___get_account"}]}}

    class _LeakyGateway2(_FakeGateway):
        def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> Any:
            if token == _INVOKE_ONLY:
                return 403, leak
            return super().__call__(method, params, token)

    class _LeakyGateway1(_FakeGateway1):
        def __call__(self, method: str, params: Mapping[str, Any], token: str | None) -> Any:
            if token == _USER_A:
                return 403, leak
            return super().__call__(method, params, token)

    rows, _ = _run(_LeakyGateway2(), gateway1=_LeakyGateway1())
    assert rows[check].status == "fail" and "no tools/list result" in rows[check].detail


class TestEveryReadTokenIsVerified:
    """Claims are read only from tokens whose signature, iss and client_id verify."""

    def test_a_user_token_signed_by_a_key_outside_the_jwks_fails_its_rows(self) -> None:
        stranger = test_key.TestKey(kid="not-in-the-jwks").sign(
            test_key.access_token_claims(
                sub="user-a", scope="openid supervisor/chat", username="user-a"
            )
        )
        gateway = _FakeGateway()
        gateway.tokens.add(stranger)
        _OWNER_OF[stranger] = "user-a"
        try:
            rows, _ = _run(gateway, user_tokens=(stranger, _USER_B))
        finally:
            del _OWNER_OF[stranger]
        scope_row = rows["user_token_carries_chat_scope"]
        assert scope_row.status == "fail" and "signing_key" in scope_row.detail
        forged = rows["forged_agent_identity_refused"]
        assert forged.status == "fail" and "failed claims verification" in forged.detail
        assert rows["conversation_items_removed"].status == "fail"

    def test_a_user_token_issued_to_the_machine_client_fails_the_user_scope_row(self) -> None:
        wrong_client = _token(
            "user-a", "openid supervisor/chat", client_output="cognito_mcp_client_id"
        )
        gateway = _FakeGateway()
        gateway.tokens.add(wrong_client)
        _OWNER_OF[wrong_client] = "user-a"
        try:
            rows, _ = _run(gateway, user_tokens=(wrong_client, _USER_B))
        finally:
            del _OWNER_OF[wrong_client]
        row = rows["user_token_carries_chat_scope"]
        assert row.status == "fail" and "client_id" in row.detail

    def test_a_service_token_with_a_tampered_signature_fails_its_scope_row(self) -> None:
        tampered = module._tampered(_SERVICE)
        gateway = _FakeGateway()
        gateway.tokens.add(tampered)
        rows, _ = _run(gateway, service_token=tampered)
        row = rows["service_token_carries_chat_scope"]
        assert row.status == "fail" and "signature" in row.detail

    def test_an_invoke_only_token_from_another_pool_fails_its_row(self) -> None:
        foreign = test_key.access_token(
            iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other",
            client_id="test-mcp-client",
            scope="gateway/invoke",
        )
        rows, _ = _run(_FakeGateway(), invoke_only_token=foreign)
        row = rows["gateway2_refuses_invoke_only_token"]
        assert row.status == "fail" and "iss" in row.detail
