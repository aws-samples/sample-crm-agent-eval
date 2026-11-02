# SPDX-License-Identifier: MIT
"""Unit tests for the test-only AWS event-stream encoder and the vendor-SDK wire fixtures.

Frames are checked against the Anthropic SDK's Bedrock decoder, and the fixtures under
``tests/integration/fixtures/bedrock/`` are replayed through the real SDKs over a mock transport.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Iterator
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from anthropic import AnthropicBedrock
from anthropic.lib.bedrock._stream_decoder import AWSEventStreamDecoder
from botocore.eventstream import ChecksumMismatch, EventStreamBuffer
from openai import AsyncBedrockOpenAI, BedrockOpenAI

from tests.integration._aws_eventstream import (
    FIXTURES_DIR,
    HeaderValue,
    encode_chunk_event,
    encode_exception,
    encode_message,
    load_wire_fixture,
    split_frames,
    wire_body,
)

_REGION = "us-east-1"
#: Placeholder credential for a client whose calls never leave the process; not a secret.
_STUB_CRED = "test"
_OPENAI_BASE_URL = f"https://bedrock-runtime.{_REGION}.amazonaws.com/openai/v1"

_ANTHROPIC_RECORDED = {
    "anthropic_invoke_stream_opus55_success.json": "end_turn",
    "anthropic_invoke_stream_opus55_tool_use.json": "tool_use",
    "anthropic_invoke_stream_sonnet55_success.json": "end_turn",
    "anthropic_invoke_stream_sonnet55_tool_use.json": "tool_use",
    "anthropic_invoke_stream_haiku55_success.json": "end_turn",
    "anthropic_invoke_stream_haiku55_tool_use.json": "tool_use",
}
_ANTHROPIC_SYNTH_ERRORS = {
    "anthropic_invoke_stream_synth_model_stream_error.json": "modelStreamErrorException",
    "anthropic_invoke_stream_synth_internal_server_exception.json": "internalServerException",
}
_OPENAI_RECORDED = (
    "openai_responses_astra_success.json",
    "openai_responses_astra_tool_use.json",
    "openai_responses_sol_success.json",
    "openai_responses_sol_tool_use.json",
    "openai_responses_terra_success.json",
    "openai_responses_terra_tool_use.json",
    "openai_responses_luna_success.json",
    "openai_responses_luna_tool_use.json",
    "openai_responses_luna_live_server_error.json",
)
_WIRE_FIXTURES = (
    *_ANTHROPIC_RECORDED,
    *_ANTHROPIC_SYNTH_ERRORS,
    *_OPENAI_RECORDED,
    "openai_responses_luna_synth_response_failed.json",
)
#: Fixtures under a wire-fixture name pattern that are not wire recordings: an adapter-level event
#: list with no ``request``/``response``, replayed by ``tests/integration/test_adapters_recorded.py``.
_NOT_WIRE_FIXTURES = frozenset({"openai_responses_malformed_stream.json"})
#: Filename patterns every wire recording on disk matches, whatever its model.
_WIRE_FIXTURE_PATTERNS = ("anthropic_invoke_stream_*.json", "openai_responses_*.json")

#: Same bounded 12-digit shape `scripts/check_no_account_id.sh` scans for.
_TWELVE_DIGIT_RUN = re.compile(r"(?:^|[^0-9a-fA-F])([0-9]{12})(?:[^0-9a-fA-F]|$)")


@pytest.fixture(autouse=True)
def _no_ambient_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_BEDROCK_BASE_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def _decode(body: bytes, chunk_size: int = 7) -> list[tuple[str | None, str]]:
    chunks: Iterator[bytes] = (body[i : i + chunk_size] for i in range(0, len(body), chunk_size))
    return [(sse.event, sse.data) for sse in AWSEventStreamDecoder().iter_bytes(chunks)]


def _replay(fixture: dict[str, Any]) -> httpx2.Client:
    response = fixture["response"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            response["status"],
            headers={"content-type": response["content_type"]},
            content=wire_body(fixture),
        )

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _anthropic_client(fixture: dict[str, Any]) -> AnthropicBedrock:
    return AnthropicBedrock(
        aws_access_key=_STUB_CRED,
        aws_secret_key=_STUB_CRED,
        aws_region=_REGION,
        http_client=_replay(fixture),
        max_retries=0,
    )


def _openai_client(fixture: dict[str, Any]) -> BedrockOpenAI:
    return BedrockOpenAI(
        aws_access_key_id=_STUB_CRED,
        aws_secret_access_key=_STUB_CRED,
        aws_region=_REGION,
        base_url=_OPENAI_BASE_URL,
        http_client=_replay(fixture),
        max_retries=0,
    )


def _stream_anthropic(fixture: dict[str, Any]) -> tuple[list[str], anthropic.types.Message]:
    client = _anthropic_client(fixture)
    with client.messages.stream(
        model=fixture["model_profile"],
        max_tokens=16,
        messages=[{"role": "user", "content": "replay"}],
    ) as stream:
        types: list[str] = [event.type for event in stream]
        final: anthropic.types.Message = stream.get_final_message()
        return types, final


# --- encoder round trip -------------------------------------------------------------------------


def test_a_synthesized_chunk_frame_round_trips_through_the_sdk_decoder() -> None:
    event = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "hé"},
    }

    decoded = _decode(encode_chunk_event(event, padding="abcdefgh"))

    assert len(decoded) == 1
    assert decoded[0][0] == "content_block_delta"
    assert json.loads(decoded[0][1]) == event


def test_concatenated_frames_decode_in_order_across_arbitrary_chunk_boundaries() -> None:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"id": "m"}},
        {"type": "message_stop"},
    ]
    body = b"".join(encode_chunk_event(e) for e in events)

    for chunk_size in (1, 5, 64, len(body)):
        assert [json.loads(data) for _, data in _decode(body, chunk_size)] == events
    assert len(split_frames(body)) == 2


def test_every_header_type_the_encoder_emits_parses_back_through_botocore() -> None:
    headers: dict[str, HeaderValue] = {
        ":flag-on": True,
        ":flag-off": False,
        ":count": -7,
        ":blob": b"\x00\x01",
        ":s": "v",
    }
    buffer = EventStreamBuffer()
    buffer.add_data(encode_message(headers, b"payload"))

    (message,) = list(buffer)

    assert message.headers == headers
    assert message.payload == b"payload"


@pytest.mark.parametrize("name", sorted(_ANTHROPIC_RECORDED))
def test_the_encoder_reproduces_every_recorded_bedrock_frame_byte_for_byte(name: str) -> None:
    frames = split_frames(wire_body(load_wire_fixture(name)))
    assert frames

    for frame in frames:
        buffer = EventStreamBuffer()
        buffer.add_data(frame)
        (message,) = list(buffer)
        assert encode_message(message.headers, message.payload) == frame


# --- corruption controls: the round-trip tests above can fail ---------------------------------


@pytest.mark.parametrize("offset_from_end", [5, 20], ids=["payload-tail", "payload-mid"])
def test_a_flipped_payload_byte_is_rejected_by_the_message_crc(offset_from_end: int) -> None:
    frame = bytearray(encode_chunk_event({"type": "message_stop"}, padding="x" * 16))
    frame[-offset_from_end] ^= 0x01

    with pytest.raises(ChecksumMismatch):
        _decode(bytes(frame))


def test_a_flipped_prelude_byte_is_rejected_by_the_prelude_crc() -> None:
    frame = bytearray(encode_chunk_event({"type": "message_stop"}))
    frame[6] ^= 0x01

    with pytest.raises(ChecksumMismatch):
        _decode(bytes(frame))


def test_a_flipped_byte_in_a_recorded_fixture_is_rejected() -> None:
    body = bytearray(wire_body(load_wire_fixture("anthropic_invoke_stream_opus55_success.json")))
    body[len(body) // 2] ^= 0x01

    with pytest.raises(ChecksumMismatch):
        _decode(bytes(body))


def test_split_frames_refuses_a_truncated_body() -> None:
    body = encode_chunk_event({"type": "message_stop"})

    with pytest.raises(ValueError, match="overruns"):
        split_frames(body[:-1])


# --- recorded Anthropic fixtures through the real decoder and SDK ------------------------------


@pytest.mark.parametrize(("name", "stop_reason"), sorted(_ANTHROPIC_RECORDED.items()))
def test_a_recorded_anthropic_fixture_decodes_to_a_complete_messages_stream(
    name: str, stop_reason: str
) -> None:
    fixture = load_wire_fixture(name)
    decoded = [json.loads(data) for _, data in _decode(wire_body(fixture))]

    assert fixture["kind"] == "recorded"
    assert fixture["response"]["content_type"] == "application/vnd.amazon.eventstream"
    assert decoded == [frame["event"] for frame in fixture["decoded_events"]]
    assert decoded[0]["type"] == "message_start"
    assert decoded[-1]["type"] == "message_stop"
    deltas = [e for e in decoded if e["type"] == "message_delta"]
    assert [d["delta"]["stop_reason"] for d in deltas] == [stop_reason]
    tool_blocks = [
        e
        for e in decoded
        if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"
    ]
    assert bool(tool_blocks) is (stop_reason == "tool_use")


@pytest.mark.parametrize(("name", "stop_reason"), sorted(_ANTHROPIC_RECORDED.items()))
def test_a_recorded_anthropic_fixture_replays_through_anthropic_bedrock(
    name: str, stop_reason: str
) -> None:
    fixture = load_wire_fixture(name)

    types, final = _stream_anthropic(fixture)

    assert final.stop_reason == stop_reason == fixture["sdk_observed"]["stop_reason"]
    assert [block.type for block in final.content] == fixture["sdk_observed"]["blocks"]
    assert final.usage.input_tokens > 0
    assert types[-1] == "message_stop"
    if stop_reason == "tool_use":
        (tool,) = [b for b in final.content if b.type == "tool_use"]
        assert tool.name == "get_account"
        assert tool.input == {"account_id": "a-1"}


def test_recorded_anthropic_request_bodies_match_the_per_model_tuning_rules() -> None:
    for name in _ANTHROPIC_RECORDED:
        fixture = load_wire_fixture(name)
        body = fixture["request"]["body"]
        assert (
            fixture["request"]["path"]
            == f"/model/{fixture['model_profile']}/invoke-with-response-stream"
        )
        assert body["max_tokens"] <= 256
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"] == {"effort": "low"}
        assert not {"temperature", "top_p", "top_k"} & body.keys()


# --- synthesized in-band errors: what the SDK surfaces -----------------------------------------


@pytest.mark.parametrize(("name", "exception_type"), sorted(_ANTHROPIC_SYNTH_ERRORS.items()))
def test_a_synthesized_anthropic_fixture_re_encodes_from_its_decoded_events(
    name: str, exception_type: str
) -> None:
    fixture = load_wire_fixture(name)
    *events, error = fixture["decoded_events"]
    payload = dict(error["payload"])
    message = payload.pop("message")

    expected = b"".join(encode_chunk_event(e["event"]) for e in events) + encode_exception(
        error["exception_type"], message, **payload
    )

    assert error["exception_type"] == exception_type
    assert wire_body(fixture) == expected


@pytest.mark.parametrize(("name", "exception_type"), sorted(_ANTHROPIC_SYNTH_ERRORS.items()))
def test_an_in_band_bedrock_exception_surfaces_as_a_base_api_status_error(
    name: str, exception_type: str
) -> None:
    fixture = load_wire_fixture(name)

    with pytest.raises(anthropic.APIStatusError) as caught:
        _stream_anthropic(fixture)

    assert type(caught.value) is anthropic.APIStatusError
    assert caught.value.status_code == 200
    assert caught.value.body == {
        "type": "error",
        "error": {
            "type": exception_type,
            "message": fixture["decoded_events"][-1]["payload"]["message"],
        },
    }


# --- OpenAI SSE fixtures -------------------------------------------------------------------------


def _sse_data(body: bytes) -> list[Any]:
    out: list[Any] = []
    for block in body.decode("utf-8").split("\n\n"):
        lines = [line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")]
        if lines:
            data = "\n".join(lines)
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


@pytest.mark.parametrize(
    "name", [*_OPENAI_RECORDED, "openai_responses_luna_synth_response_failed.json"]
)
def test_an_openai_fixture_body_is_the_sse_form_of_its_decoded_events(name: str) -> None:
    fixture = load_wire_fixture(name)

    assert fixture["response"]["content_type"] == "text/event-stream"
    assert _sse_data(wire_body(fixture)) == fixture["decoded_events"]
    assert fixture["decoded_events"][-1] == "[DONE]"


def test_the_recorded_luna_success_replays_through_bedrock_openai() -> None:
    fixture = load_wire_fixture("openai_responses_luna_success.json")
    body = fixture["request"]["body"]

    events = list(
        _openai_client(fixture).responses.create(model=body["model"], input="replay", stream=True)
    )

    assert fixture["request"]["path"] == "/openai/v1/responses"
    assert body["reasoning"] == {"effort": "low"} and body["stream"] is True
    assert events[-1].type == "response.completed"
    assert any(e.type == "response.output_text.delta" for e in events)
    completed = events[-1].response
    assert completed.status == "completed" and completed.usage is not None
    assert completed.usage.input_tokens > 0


def test_a_live_in_band_server_error_raises_api_error_before_response_failed() -> None:
    fixture = load_wire_fixture("openai_responses_luna_live_server_error.json")
    seen: list[str] = []

    with pytest.raises(openai.APIError) as caught:
        for event in _openai_client(fixture).responses.create(
            model=fixture["model_profile"], input="replay", stream=True
        ):
            seen.append(event.type)

    assert type(caught.value) is openai.APIError
    assert caught.value.code == "server_error"
    assert "response.failed" not in seen
    assert any(e != "[DONE]" and e["type"] == "response.failed" for e in fixture["decoded_events"])


def test_the_live_server_error_metadata_matches_its_wire_sequence() -> None:
    fixture = load_wire_fixture("openai_responses_luna_live_server_error.json")
    wire = [e if e == "[DONE]" else e["type"] for e in fixture["decoded_events"]]

    assert fixture["sse_event_sequence"] == wire
    assert wire[-3:] == ["error", "response.failed", "[DONE]"]


def test_the_async_client_raises_the_live_server_error_inside_the_iteration_not_at_create() -> None:
    fixture = load_wire_fixture("openai_responses_luna_live_server_error.json")
    body = wire_body(fixture)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    async def run() -> tuple[list[str], openai.APIError]:
        client = AsyncBedrockOpenAI(
            aws_access_key_id=_STUB_CRED,
            aws_secret_access_key=_STUB_CRED,
            aws_region=_REGION,
            base_url=_OPENAI_BASE_URL,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            max_retries=0,
        )
        stream = await client.responses.create(
            model=fixture["model_profile"], input="replay", stream=True
        )
        seen: list[str] = []
        try:
            async for event in stream:
                seen.append(event.type)
        except openai.APIError as exc:
            return seen, exc
        raise AssertionError("the stream completed without raising")

    seen, exc = asyncio.run(run())
    surfaces = fixture["sdk_surfaces"]

    assert seen == surfaces["events_yielded_before_raise"]
    assert type(exc) is openai.APIError and not isinstance(exc, openai.APIStatusError)
    assert (exc.code, exc.type, exc.param) == (
        surfaces["code"],
        surfaces["type"],
        surfaces["param"],
    )
    assert isinstance(exc.body, dict) and sorted(exc.body) == surfaces["body_keys"]


def test_the_recorded_luna_tool_use_replays_a_function_call() -> None:
    fixture = load_wire_fixture("openai_responses_luna_tool_use.json")

    events = list(
        _openai_client(fixture).responses.create(
            model=fixture["model_profile"], input="replay", stream=True
        )
    )

    assert fixture["request"]["body"]["tools"][0]["name"] == "get_account"
    assert events[-1].type == "response.completed"
    done = [e for e in events if e.type == "response.output_item.done"]
    assert [e.item.type for e in done] == ["function_call"]
    call = done[0].item
    assert call.type == "function_call"
    assert call.name == "get_account"
    assert json.loads(call.arguments) == {"account_id": "a-1"}


def test_a_bare_response_failed_is_yielded_as_an_event_not_raised() -> None:
    fixture = load_wire_fixture("openai_responses_luna_synth_response_failed.json")

    events = list(
        _openai_client(fixture).responses.create(
            model=fixture["model_profile"], input="replay", stream=True
        )
    )

    assert [e.type for e in events] == ["response.created", "response.failed"]
    failed = events[-1]
    assert failed.type == "response.failed"
    assert failed.response.error is not None
    assert failed.response.error.code == "server_error"


# --- scrub guards --------------------------------------------------------------------------------


def _every_decoded_layer(fixture: dict[str, Any]) -> list[str]:
    body = wire_body(fixture)
    if fixture["family"] == "openai":
        return [body.decode("utf-8")]
    layers: list[str] = []
    buffer = EventStreamBuffer()
    buffer.add_data(body)
    for message in buffer:
        layers.append(json.dumps({k: str(v) for k, v in message.headers.items()}))
        payload_text = message.payload.decode("utf-8")
        layers.append(payload_text)
        inner = json.loads(payload_text).get("bytes")
        if inner is not None:
            layers.append(base64.b64decode(inner).decode("utf-8"))
    return layers


@pytest.mark.parametrize("name", _WIRE_FIXTURES)
def test_no_wire_fixture_carries_an_account_id_or_request_id_in_any_decoded_layer(
    name: str,
) -> None:
    fixture = load_wire_fixture(name)
    layers = [*_every_decoded_layer(fixture), json.dumps(fixture.get("request", {}))]

    assert not [m.group(1) for text in layers for m in _TWELVE_DIGIT_RUN.finditer(text)]
    assert not re.search(r"requestid|request-id", json.dumps(fixture.get("response", {})), re.I)


def test_the_scrub_scan_detects_an_account_id_hidden_inside_a_frame() -> None:
    fixture = {
        "family": "anthropic",
        "response": {
            "body_b64": base64.b64encode(
                encode_chunk_event({"type": "ping", "arn": "arn:aws:iam::123456789012:root"})
            ).decode("ascii")
        },
    }

    assert any(_TWELVE_DIGIT_RUN.search(text) for text in _every_decoded_layer(fixture))


def test_every_wire_fixture_on_disk_is_covered_by_this_module() -> None:
    on_disk = {
        p.name for pattern in _WIRE_FIXTURE_PATTERNS for p in FIXTURES_DIR.glob(pattern)
    } - _NOT_WIRE_FIXTURES

    assert on_disk == set(_WIRE_FIXTURES)


def test_every_excluded_fixture_exists_and_is_not_a_wire_recording() -> None:
    for name in _NOT_WIRE_FIXTURES:
        assert (FIXTURES_DIR / name).is_file(), name
        assert "response" not in json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8")), name
