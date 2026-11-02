# SPDX-License-Identifier: MIT
"""Contract: every tier's provider request, as the vendor SDK serialises it.

Each test drives the real family adapter over the real vendor SDK (``AsyncAnthropicBedrock`` /
``AsyncBedrockOpenAI``) with an ``httpx2.MockTransport`` that records the signed HTTP request and
replays a recorded success stream (``tests/integration/fixtures/bedrock/``). No network, fake
credentials.

* One delegating ``drive_turn`` per family reaches all three tiers, and each tier's first request
  has the expected host, path, inference profile, per-tier tuning fields, and a SigV4
  ``Authorization`` header scoped to ``bedrock`` that does not sign ``connection``.
* Every tool schema the tool, subagent and supervisor render sites hand the provider reaches the
  serialised request with a non-empty ``description``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from anthropic.lib.bedrock import _auth
from botocore.credentials import Credentials

from agents.budget import EVAL_BUDGET
from agents.subagent_runner import _tool_schema
from agents.subagents import SUBAGENT_SPECS_BY_ROLE
from agents.supervisor import _direct_tools, subagent_tool_schemas
from agents.tool_actor import formulate_and_invoke
from agents.turn_driver import TurnRecords, _direct_tool_schemas, drive_turn, session_turn_tuning
from providers.anthropic_adapter import AnthropicAdapter
from providers.base import (
    ModelProvider,
    ResolvedInvocation,
    Tuning,
    resolve_session_model,
    validate_tuning,
)
from providers.capabilities import CapabilityMatrix, Family, Tier, load_capability_matrix
from providers.openai_adapter import OpenAIAdapter
from tests.integration._aws_eventstream import encode_chunk_event, load_wire_fixture, wire_body

_REGION = "us-east-1"
_EVENTSTREAM = "application/vnd.amazon.eventstream"
_SSE = "text/event-stream"
_MESSAGES: list[dict[str, object]] = [
    {"role": "user", "content": "Find the largest open opportunity."}
]
_SUBAGENT_ROLE = "research"  # a read role whose registry holds several leaf tools
_REPLAY: dict[Family, str] = {
    "anthropic": "anthropic_invoke_stream_haiku55_success.json",
    "openai": "openai_responses_luna_success.json",
}
_FAMILIES: tuple[Family, ...] = ("anthropic", "openai")
_TIERS: tuple[Tier, ...] = ("supervisor", "subagent", "tool")
_PROFILES: list[tuple[Family, Tier]] = list(itertools.product(_FAMILIES, _TIERS))


class _StaticSession:
    """A boto3-session stand-in resolving fixed fake credentials."""

    region_name = _REGION

    def get_credentials(self) -> Credentials:
        return Credentials("test", "test")


async def _no_sleep(*_: object, **__: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_BEDROCK_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(_auth, "_get_session", lambda **_: _StaticSession())
    monkeypatch.setattr(anthropic.AsyncAnthropicBedrock, "_sleep_for_retry", _no_sleep)
    monkeypatch.setattr(openai.AsyncBedrockOpenAI, "_sleep_for_retry", _no_sleep)
    yield


class _Capture:
    """A mock transport recording each signed request and replaying the family's success stream."""

    def __init__(self, family: Family) -> None:
        self.requests: list[httpx2.Request] = []
        fixture = load_wire_fixture(_REPLAY[family])
        self._content_type = fixture["response"]["content_type"]
        self._body = wire_body(fixture)
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(
            200, headers={"content-type": self._content_type}, content=self._body
        )

    def only_body(self) -> dict[str, Any]:
        assert len(self.requests) == 1, "expected exactly one bedrock-runtime request"
        body: dict[str, Any] = json.loads(self.requests[0].content)
        return body


def _adapter(family: Family, capture: _Capture) -> ModelProvider:
    if family == "anthropic":
        return AnthropicAdapter(
            region_name=_REGION, session=_StaticSession(), transport=capture.transport
        )
    return OpenAIAdapter(region_name=_REGION, session=_StaticSession(), transport=capture.transport)


#: The caller's supervisor effort: one that no tier's config/models.yaml effort uses, so a caller
#: effort applied to a lower tier shows in that tier's request body.
_CALLER_EFFORT = "high"


def _tuning_for(family: Family, tier: Tier) -> Tuning:
    """The tuning each tier resolves with: caller effort on the supervisor only."""
    supervisor = Tuning(source="test", family=family, effort=_CALLER_EFFORT)
    if tier == "supervisor":
        return supervisor
    return replace(supervisor, effort=None)


def _resolved(matrix: CapabilityMatrix, family: Family, tier: Tier) -> ResolvedInvocation:
    tuning = _tuning_for(family, tier)
    return validate_tuning(resolve_session_model(matrix, tier, tuning), tuning)


def _drain(stream: Any) -> None:
    async def _run() -> None:
        async for _ in stream:
            pass

    asyncio.run(_run())


def _capture(
    family: Family, resolved: ResolvedInvocation, tools: list[dict[str, object]]
) -> _Capture:
    capture = _Capture(family)
    _drain(_adapter(family, capture).stream(resolved, _MESSAGES, tools))
    return capture


def _capture_tool_tier(matrix: CapabilityMatrix, family: Family) -> tuple[_Capture, list[str]]:
    """Drive the tool tier through ``formulate_and_invoke``, returning the capture and the tool's description."""
    tool = SUBAGENT_SPECS_BY_ROLE[_SUBAGENT_ROLE].tools[0]
    capture = _Capture(family)
    adapter = _adapter(family, capture)

    async def _run() -> None:
        await formulate_and_invoke(
            tool,
            intent="find the largest open opportunity",
            role=_SUBAGENT_ROLE,
            provider=adapter,
            resolved=_resolved(matrix, family, "tool"),
            dispatch=lambda _t, _a: {},
        )

    asyncio.run(_run())
    return capture, [tool.description]


def _tier_tools(tier: Tier) -> list[dict[str, object]]:
    """The seam tool list each tier hands the provider, built by the real render helpers."""
    if tier == "supervisor":
        return [*_direct_tool_schemas(_direct_tools()), *subagent_tool_schemas()]
    if tier == "subagent":
        return [_tool_schema(tool) for tool in SUBAGENT_SPECS_BY_ROLE[_SUBAGENT_ROLE].tools]
    return [_tool_schema(SUBAGENT_SPECS_BY_ROLE[_SUBAGENT_ROLE].tools[0])]


@pytest.fixture(scope="module")
def matrix() -> CapabilityMatrix:
    return load_capability_matrix()


# --- The production request for every profile, as serialised ------------------------------

#: The per-tier body fields the product path sends: the supervisor carries the caller's effort;
#: subordinate tiers carry their config/models.yaml tier effort; no tier sends a sampling
#: parameter.
_EXPECTED_TUNING_FIELDS: dict[tuple[Family, Tier], dict[str, object]] = {
    ("anthropic", "supervisor"): {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": _CALLER_EFFORT},
    },
    ("anthropic", "subagent"): {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    },
    ("anthropic", "tool"): {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    },
    ("openai", "supervisor"): {"reasoning": {"effort": _CALLER_EFFORT}},
    ("openai", "subagent"): {"reasoning": {"effort": "low"}},
    ("openai", "tool"): {"reasoning": {"effort": "low"}},
}
_TUNING_KEYS = ("thinking", "output_config", "reasoning", "temperature", "top_p", "top_k")


@pytest.mark.parametrize("family", _FAMILIES)
def test_the_caller_effort_differs_from_every_configured_tier_effort(
    family: Family, matrix: CapabilityMatrix
) -> None:
    """The caller's supervisor effort is distinguishable from each tier's configured effort."""
    configured = {tier: matrix.tiers[family][tier].tuning["effort"] for tier in _TIERS}
    assert _CALLER_EFFORT not in configured.values(), configured
    assert _CALLER_EFFORT in matrix.model_for(family, "supervisor").effort.values


_DELEGATED_ROLE = "research"
_SUBAGENT_TOOL_NAMES = {str(schema["name"]) for schema in subagent_tool_schemas()}


def _anthropic_tool_use(name: str, arguments: dict[str, object]) -> bytes:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_contract",
                "type": "message",
                "role": "assistant",
                "model": "contract",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_contract",
                "name": name,
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    return b"".join(encode_chunk_event(event) for event in events)


def _openai_tool_use(name: str, arguments: dict[str, object]) -> bytes:
    events: list[dict[str, Any]] = [
        {
            "type": "response.output_item.done",
            "sequence_number": 1,
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": "fc_contract",
                "call_id": "call_contract",
                "name": name,
                "arguments": json.dumps(arguments),
                "status": "completed",
            },
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {
                "id": "resp_contract",
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]
    return ("".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n").encode()


class _ScriptedTurn:
    """A transport scripting one real delegating turn: every tier's first call requests the next.

    The supervisor delegates to ``research``, the subagent calls its first leaf tool, and the tool
    tier formulates that call; every later call answers with a recorded text stream. Each request is
    recorded by the tier its model profile belongs to; when the supervisor shares its profile with
    a lower tier, a request offering a subagent tool is the supervisor's.
    """

    def __init__(self, family: Family, matrix: CapabilityMatrix) -> None:
        self.family = family
        self.by_tier: dict[Tier, list[httpx2.Request]] = {tier: [] for tier in _TIERS}
        self._profile_of = {t: matrix.model_for(family, t).inference_profile for t in _TIERS}
        leaf = SUBAGENT_SPECS_BY_ROLE[_DELEGATED_ROLE].tools[0].name
        self._first_reply: dict[Tier, tuple[str, dict[str, object]]] = {
            "supervisor": (_DELEGATED_ROLE, {"intent": "find the largest open opportunity"}),
            "subagent": (leaf, {"query": "largest open opportunity"}),
            "tool": (leaf, {"query": "largest open opportunity"}),
        }
        fixture = load_wire_fixture(_REPLAY[family])
        self._text = (fixture["response"]["content_type"], wire_body(fixture))
        self.transport = httpx2.MockTransport(self._handle)

    def _tier(self, request: httpx2.Request) -> Tier:
        body = json.loads(request.content)
        profile = request.url.path.split("/")[2] if self.family == "anthropic" else body["model"]
        candidates = [t for t in _TIERS if self._profile_of[t] == profile]
        if len(candidates) > 1:
            delegates = bool(
                {tool["name"] for tool in body.get("tools", [])} & _SUBAGENT_TOOL_NAMES
            )
            candidates = [t for t in candidates if (t == "supervisor") == delegates]
        assert len(candidates) == 1, f"request to {profile} matches tiers {candidates}"
        return candidates[0]

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        tier = self._tier(request)
        self.by_tier[tier].append(request)
        if len(self.by_tier[tier]) == 1:
            name, arguments = self._first_reply[tier]
            if self.family == "anthropic":
                body, content_type = _anthropic_tool_use(name, arguments), _EVENTSTREAM
            else:
                body, content_type = _openai_tool_use(name, arguments), _SSE
            return httpx2.Response(200, headers={"content-type": content_type}, content=body)
        content_type, text = self._text
        return httpx2.Response(200, headers={"content-type": content_type}, content=text)


def _drive_production_turn(family: Family, matrix: CapabilityMatrix) -> _ScriptedTurn:
    """Run one real ``drive_turn`` whose supervisor delegates once, over the scripted transport."""
    script = _ScriptedTurn(family, matrix)
    provider: ModelProvider
    if family == "anthropic":
        provider = AnthropicAdapter(
            region_name=_REGION, session=_StaticSession(), transport=script.transport
        )
    else:
        provider = OpenAIAdapter(
            region_name=_REGION, session=_StaticSession(), transport=script.transport
        )

    async def _run() -> None:
        async for _ in drive_turn(
            prompt="Find the largest open opportunity.",
            session_id="contract",
            providers=lambda _family: provider,
            matrix=matrix,
            tuning=session_turn_tuning(Tuning(source="test", family=family, effort=_CALLER_EFFORT)),
            budget=EVAL_BUDGET,
            dispatch=lambda _tool, _input: {"statusCode": 200, "body": {"items": []}},
            records=TurnRecords(),
        ):
            pass

    asyncio.run(_run())
    return script


@pytest.mark.parametrize(("family", "tier"), _PROFILES)
def test_every_profile_sends_the_production_request_shape_signed_with_sigv4(
    family: Family, tier: Tier, matrix: CapabilityMatrix
) -> None:
    """In one delegating turn, each tier's first request has the expected shape and tuning."""
    script = _drive_production_turn(family, matrix)
    requests = script.by_tier[tier]
    assert requests, f"the {tier} tier was never called in a delegating turn"
    request = requests[0]
    body: dict[str, Any] = json.loads(request.content)
    profile = matrix.model_for(family, tier).inference_profile

    assert request.method == "POST"
    assert request.url.host == f"bedrock-runtime.{_REGION}.amazonaws.com"
    authorization = request.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=test/")
    assert f"/{_REGION}/bedrock/aws4_request" in authorization
    signed = authorization.split("SignedHeaders=")[1].split(",")[0].split(";")
    assert "connection" not in signed and "host" in signed
    assert "x-api-key" not in request.headers
    assert body["tools"] and all(tool["description"] for tool in body["tools"])

    sent_tuning = {key: body[key] for key in _TUNING_KEYS if key in body}
    assert sent_tuning == _EXPECTED_TUNING_FIELDS[(family, tier)]
    if family == "anthropic":
        assert request.url.path == f"/model/{profile}/invoke-with-response-stream"
        assert "model" not in body
        assert body["anthropic_version"] == "bedrock-2023-05-31"
    else:
        assert request.url.path == "/openai/v1/responses"
        assert body["model"] == profile
        assert body["stream"] is True
        assert "messages" not in body and isinstance(body["input"], list)


def test_the_anthropic_tool_tier_sends_its_effort_with_adaptive_thinking_and_no_sampling(
    matrix: CapabilityMatrix,
) -> None:
    """The tool tier's model takes ``output_config.effort`` beside adaptive thinking."""
    model = matrix.model_for("anthropic", "tool")
    resolved = validate_tuning(model, Tuning(source="test", family="anthropic", effort="low"))
    capture = _capture("anthropic", resolved, _tier_tools("tool"))
    body = capture.only_body()

    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low"}
    assert not {"temperature", "top_p", "top_k", "extra_body"} & body.keys()
    assert (
        capture.requests[0].url.path
        == f"/model/{model.inference_profile}/invoke-with-response-stream"
    )


#: Request-body keys that would ask for structured output on either API.
_STRUCTURED_OUTPUT_KEYS = ("output_format", "response_format", "text")


@pytest.mark.parametrize(("family", "tier"), _PROFILES)
def test_no_profile_calls_count_tokens_or_requests_structured_output(
    family: Family, tier: Tier, matrix: CapabilityMatrix
) -> None:
    """Each turn is one streaming call, never count-tokens, with no structured-output field."""
    capture = _capture(family, _resolved(matrix, family, tier), _tier_tools(tier))
    body = capture.only_body()
    path = capture.requests[0].url.path

    assert "count-tokens" not in path and "count_tokens" not in path
    assert path.endswith(
        "/invoke-with-response-stream" if family == "anthropic" else "/openai/v1/responses"
    )
    assert not set(_STRUCTURED_OUTPUT_KEYS) & body.keys()
    assert "format" not in body.get("output_config", {})


def test_the_tier_profiles_are_the_capability_matrix_profiles(matrix: CapabilityMatrix) -> None:
    profiles = {matrix.model_for(family, tier).inference_profile for family, tier in _PROFILES}

    assert profiles == {
        "global.anthropic.claude-sonnet-5-5",
        "global.anthropic.claude-haiku-5-5",
        "global.openai.gpt-6.1-sol",
        "global.openai.gpt-6.1-terra",
        "global.openai.gpt-6-luna",
    }


# --- Descriptions present on every render site, as serialised -----------------------------


def _direct_tool_descriptions(request_tools: list[dict[str, Any]]) -> list[str]:
    """Return the descriptions the request carries for the supervisor's direct tools only."""
    direct_names = {tool.name for tool in _direct_tools()}
    by_name = {tool["name"]: tool["description"] for tool in request_tools}
    missing = direct_names - by_name.keys()
    assert not missing, f"supervisor request omitted direct tools entirely: {sorted(missing)}"
    return [by_name[name] for name in direct_names]


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_subagent_tools_carry_descriptions(family: Family, matrix: CapabilityMatrix) -> None:
    body = _capture(
        family, _resolved(matrix, family, "subagent"), _tier_tools("subagent")
    ).only_body()
    descriptions = [tool["description"] for tool in body.get("tools", [])]
    assert descriptions, "subagent request carried no tools"
    assert all(descriptions), "a subagent leaf tool was sent with an empty description"


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_the_tool_tier_sends_its_tool_description(family: Family, matrix: CapabilityMatrix) -> None:
    capture, expected = _capture_tool_tier(matrix, family)
    body = capture.only_body()
    descriptions = [tool["description"] for tool in body.get("tools", [])]
    assert descriptions == expected
    assert all(descriptions), "the tool tier sent an empty tool description"
    if family == "anthropic":
        assert "haiku" in capture.requests[0].url.path


@pytest.mark.parametrize("family", ["anthropic", "openai"])
def test_supervisor_direct_tools_carry_descriptions(
    family: Family, matrix: CapabilityMatrix
) -> None:
    """The supervisor tier's direct read tools carry a non-empty description."""
    body = _capture(
        family, _resolved(matrix, family, "supervisor"), _tier_tools("supervisor")
    ).only_body()
    descriptions = _direct_tool_descriptions(body.get("tools", []))
    assert all(descriptions), "a supervisor direct tool was sent with an empty description"
