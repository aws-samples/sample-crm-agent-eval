# SPDX-License-Identifier: MIT
"""Test-only AWS event-stream encoder and wire-fixture loader for the Bedrock SDK adapters.

Encodes the binary frames Bedrock's ``invoke-with-response-stream`` returns (prelude, typed headers,
payload, two CRC32s) so tests can feed ``anthropic.lib.bedrock._stream_decoder`` real-shaped bytes
through an ``httpx2.MockTransport``. ``tests/unit/test_aws_eventstream.py`` round-trips the encoder
through that decoder.

Usage::

    body = encode_chunk_event({"type": "message_start", ...}) + encode_exception(
        "modelStreamErrorException", "boom"
    )
    fixture = load_wire_fixture("anthropic_invoke_stream_opus55_success.json")
    raw = wire_body(fixture)  # the exact recorded response bytes
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "bedrock"

HeaderValue = str | bytes | bool | int

_PRELUDE_LENGTH = 12
_MESSAGE_CRC_LENGTH = 4
_HEADER_BOOL_TRUE = 0
_HEADER_BOOL_FALSE = 1
_HEADER_INT32 = 4
_HEADER_BYTES = 6
_HEADER_STRING = 7


def _crc32(data: bytes, seed: int = 0) -> int:
    return binascii.crc32(data, seed) & 0xFFFFFFFF


def _encode_header(name: str, value: HeaderValue) -> bytes:
    encoded_name = name.encode("utf-8")
    if len(encoded_name) > 255:
        raise ValueError(f"header name longer than 255 bytes: {name!r}")
    prefix = struct.pack(">B", len(encoded_name)) + encoded_name
    if isinstance(value, bool):
        return prefix + struct.pack(">B", _HEADER_BOOL_TRUE if value else _HEADER_BOOL_FALSE)
    if isinstance(value, int):
        return prefix + struct.pack(">Bi", _HEADER_INT32, value)
    raw, header_type = (
        (value.encode("utf-8"), _HEADER_STRING)
        if isinstance(value, str)
        else (value, _HEADER_BYTES)
    )
    if len(raw) > 0xFFFF:
        raise ValueError(f"header value longer than 65535 bytes: {name!r}")
    return prefix + struct.pack(">BH", header_type, len(raw)) + raw


def encode_message(headers: Mapping[str, HeaderValue], payload: bytes) -> bytes:
    """Encode one event-stream message: ``prelude | headers | payload | message CRC32``.

    The prelude is ``total length, headers length, CRC32(first 8 bytes)``; the message CRC32 covers
    every byte before it. Header order follows the mapping's iteration order.
    """
    header_bytes = b"".join(_encode_header(name, value) for name, value in headers.items())
    total_length = _PRELUDE_LENGTH + len(header_bytes) + len(payload) + _MESSAGE_CRC_LENGTH
    lengths = struct.pack(">II", total_length, len(header_bytes))
    prelude = lengths + struct.pack(">I", _crc32(lengths))
    body = prelude + header_bytes + payload
    return body + struct.pack(">I", _crc32(body))


def encode_chunk_event(event: Mapping[str, Any], padding: str = "") -> bytes:
    """Encode one Messages API stream event as Bedrock's ``chunk`` frame.

    The frame payload is ``{"bytes": base64(json(event))}`` (plus Bedrock's ``p`` padding field when
    ``padding`` is non-empty), with ``:event-type chunk`` headers.
    """
    inner = base64.b64encode(json.dumps(event, separators=(",", ":")).encode("utf-8"))
    payload_obj: dict[str, str] = {"bytes": inner.decode("ascii")}
    if padding:
        payload_obj["p"] = padding
    headers: dict[str, HeaderValue] = {
        ":event-type": "chunk",
        ":content-type": "application/json",
        ":message-type": "event",
    }
    return encode_message(headers, json.dumps(payload_obj, separators=(",", ":")).encode("utf-8"))


def encode_exception(exception_type: str, message: str, **extra: Any) -> bytes:
    """Encode an in-band ``:message-type exception`` frame, e.g. ``modelStreamErrorException``.

    ``extra`` adds members to the JSON payload beside ``message`` (``originalStatusCode``, …).
    """
    headers: dict[str, HeaderValue] = {
        ":exception-type": exception_type,
        ":content-type": "application/json",
        ":message-type": "exception",
    }
    payload = json.dumps({"message": message, **extra}, separators=(",", ":")).encode("utf-8")
    return encode_message(headers, payload)


def split_frames(data: bytes) -> list[bytes]:
    """Split a concatenated event-stream body into its frames by the prelude's total length.

    Raises:
        ValueError: if the body ends inside a frame.
    """
    frames: list[bytes] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < _PRELUDE_LENGTH:
            raise ValueError(f"truncated prelude at offset {offset}")
        (total_length,) = struct.unpack_from(">I", data, offset)
        if total_length < _PRELUDE_LENGTH + _MESSAGE_CRC_LENGTH or offset + total_length > len(
            data
        ):
            raise ValueError(f"frame at offset {offset} overruns the body")
        frames.append(data[offset : offset + total_length])
        offset += total_length
    return frames


def load_wire_fixture(name: str) -> dict[str, Any]:
    """Load one recorded or synthesized wire fixture from ``tests/integration/fixtures/bedrock/``."""
    data: dict[str, Any] = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    if "_provenance" not in data or "response" not in data:
        raise ValueError(f"{name} is not a wire fixture (missing _provenance or response)")
    return data


def wire_body(fixture: Mapping[str, Any]) -> bytes:
    """Return the exact response body bytes a wire fixture carries."""
    return base64.b64decode(fixture["response"]["body_b64"], validate=True)
