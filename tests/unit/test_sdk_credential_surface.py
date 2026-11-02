# SPDX-License-Identifier: MIT
"""Pins every credential and endpoint source the installed SDKs' Bedrock clients can read.

An AST walk over each SDK's Bedrock client modules collects every environment-variable read,
whatever its syntax, and the constructor parameter sets are pinned beside it. A new source in an
SDK release changes a pinned set and reds a cell.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import pkgutil
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropicBedrock
from botocore.credentials import Credentials
from openai import AsyncBedrockOpenAI

from providers import sdk_transport
from providers.sdk_transport import (
    AnthropicClientKwargs,
    OpenAIClientKwargs,
    assert_runtime_client,
    build_http_client,
    openai_client_kwargs,
    session_credentials_provider,
)

REGION = "us-east-1"

#: A dynamic read: an ``environ`` access or env-reader call whose key is not a string literal.
DYNAMIC = "<dynamic>"

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")

#: A helper whose name says it reads the environment: ``getenv``, ``_read_env``, ``get_environ_value``.
_ENV_READER_NAME = re.compile(
    r"(?:^|_)(?:get|read|load|fetch)_?env(?:iron(?:ment)?)?(?:_|$)", re.IGNORECASE
)

#: ``os.environ`` methods taking a variable name as their first argument.
_KEYED_ENVIRON_METHODS = frozenset({"get", "pop", "setdefault", "__getitem__", "__contains__"})

#: Modules on the Anthropic Bedrock client's construction, request and signing path. ``_mantle``
#: is excluded: ``AnthropicBedrockMantle`` is never constructed, and its endpoint is refused.
ANTHROPIC_SCAN_PACKAGES: tuple[tuple[str, frozenset[str]], ...] = (
    ("anthropic.lib.bedrock", frozenset({"_mantle"})),
)
ANTHROPIC_SCAN_MODULES: tuple[str, ...] = ("anthropic._base_client",)

#: Modules on the OpenAI Bedrock client's construction, request and signing path.
OPENAI_SCAN_MODULES: tuple[str, ...] = (
    "openai.lib.bedrock",
    "openai.lib._bedrock_auth",
    "openai.providers.bedrock",
    "openai._provider",
    "openai._client",
    "openai.lib._client_auth",
    "openai._base_client",
)

#: Region reads; they choose the signing region, never a credential.
REGION_READS = frozenset({"AWS_REGION", "AWS_DEFAULT_REGION"})

#: ``openai.lib._client_auth`` reads skipped whenever a provider runtime is set, as on Bedrock.
#: ``test_openai_client_ignores_every_provider_gated_variable`` proves the gating.
OPENAI_PROVIDER_GATED_READS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_CUSTOM_HEADERS",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
    }
)

#: Read on every OpenAI client; it verifies inbound webhooks and never authenticates a request.
OPENAI_WEBHOOK_READS = frozenset({"OPENAI_WEBHOOK_SECRET"})

ANTHROPIC_ENV_READS = REGION_READS | {"AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL"}
OPENAI_ENV_READS = (
    REGION_READS
    | {"AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL"}
    | OPENAI_PROVIDER_GATED_READS
    | OPENAI_WEBHOOK_READS
)

#: Constructor parameters that select no credential, endpoint or header.
_NON_AUTH_PARAMS = frozenset(
    {"self", "timeout", "max_retries", "open_telemetry", "_strict_response_validation"}
)

ANTHROPIC_AUTH_PARAMS = frozenset(
    {
        "aws_secret_key",
        "aws_access_key",
        "aws_region",
        "aws_profile",
        "aws_session_token",
        "api_key",
        "base_url",
        "http_client",
        "middleware",
        "default_headers",
        "default_query",
    }
)
OPENAI_AUTH_PARAMS = frozenset(
    {
        "api_key",
        "bedrock_token_provider",
        "aws_region",
        "aws_profile",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "aws_credentials_provider",
        "organization",
        "project",
        "webhook_secret",
        "base_url",
        "websocket_base_url",
        "default_headers",
        "default_query",
        "http_client",
        "_enforce_credentials",
        "_provider",
        "_state",
        "_region_was_explicit",
    }
)


def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _callee_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _env_key(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if _ENV_NAME.match(node.value) else DYNAMIC
    return DYNAMIC


def env_reads(source: str) -> frozenset[str]:
    """Return every environment variable ``source`` reads, ``DYNAMIC`` for a non-literal key.

    Recognised: ``os.environ.get(...)`` (any arity), ``os.environ[...]``, ``"X" in os.environ``,
    ``os.getenv(...)``, any call to a helper named like an env reader (``_read_env("X")``), and any
    other use of ``environ`` (iteration, ``copy()``, passing it whole), which counts as dynamic.
    """
    tree = ast.parse(source)
    reads: set[str] = set()
    keyed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and _is_environ(func.value)
                and func.attr in _KEYED_ENVIRON_METHODS
            ):
                keyed.add(id(func.value))
                reads.add(_env_key(node.args[0] if node.args else None))
            elif _ENV_READER_NAME.search(_callee_name(func)) and node.args:
                reads.add(_env_key(node.args[0]))
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            keyed.add(id(node.value))
            reads.add(_env_key(node.slice))
        elif isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
            keyed.update(id(c) for c in node.comparators)
            reads.add(_env_key(node.left))
    if any(_is_environ(node) and id(node) not in keyed for node in ast.walk(tree)):
        reads.add(DYNAMIC)
    return frozenset(reads)


def _module_paths(
    packages: tuple[tuple[str, frozenset[str]], ...], modules: tuple[str, ...]
) -> list[Path]:
    names = list(modules)
    for package, excluded in packages:
        names.append(package)
        path = importlib.import_module(package).__path__
        names.extend(
            f"{package}.{m.name}" for m in pkgutil.iter_modules(path) if m.name not in excluded
        )
    paths = []
    for name in names:
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin is not None, name
        paths.append(Path(spec.origin))
    return paths


def _scan(paths: list[Path]) -> frozenset[str]:
    reads: set[str] = set()
    for path in paths:
        reads |= env_reads(path.read_text(encoding="utf-8"))
    return frozenset(reads)


def _auth_params(cls: type) -> frozenset[str]:
    return frozenset(inspect.signature(cls).parameters) - _NON_AUTH_PARAMS


# -- the reader itself ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        'os.environ.get("NEW_SECRET")',
        "os.environ.get('NEW_SECRET')",
        'os.environ.get("NEW_SECRET", None)',
        'os.environ.get("NEW_SECRET", default="")',
        'os.getenv("NEW_SECRET")',
        'getenv("NEW_SECRET")',
        'os.environ["NEW_SECRET"]',
        'environ["NEW_SECRET"]',
        '"NEW_SECRET" in os.environ',
        'os.environ.pop("NEW_SECRET")',
        '_read_env("NEW_SECRET")',
        'read_environment_value("NEW_SECRET")',
    ],
)
def test_every_env_read_form_is_recognised(line: str) -> None:
    assert env_reads(f"import os\nvalue = {line}\n") == {"NEW_SECRET"}


@pytest.mark.parametrize(
    "line",
    [
        "os.environ.get(name)",
        "os.getenv(name)",
        "os.environ[name]",
        "dict(os.environ)",
        "os.environ.copy()",
        "[k for k in os.environ]",
        "_read_env(name)",
    ],
)
def test_a_non_literal_or_whole_environ_read_is_dynamic(line: str) -> None:
    assert DYNAMIC in env_reads(f"import os\nname = 'X'\nvalue = {line}\n")


def test_control_non_env_constants_are_not_reads() -> None:
    source = (
        "import socket\n"
        "h = x509_safe_environment_headers(parsed, identity)\n"
        'x = getattr(socket, "TCP_KEEPALIVE")\n'
        'y = headers.get("ID")\n'
        'z = {"ACCEPT": 1}["ACCEPT"]\n'
    )
    assert env_reads(source) == frozenset()


# -- Anthropic --------------------------------------------------------------------------------


def test_anthropic_bedrock_path_reads_only_region_and_refused_variables() -> None:
    reads = _scan(_module_paths(ANTHROPIC_SCAN_PACKAGES, ANTHROPIC_SCAN_MODULES))
    assert reads == ANTHROPIC_ENV_READS
    assert reads - REGION_READS <= set(sdk_transport.FORBIDDEN_ENV_VARS)


def test_anthropic_bedrock_constructor_parameters_are_pinned() -> None:
    assert _auth_params(AsyncAnthropicBedrock) == ANTHROPIC_AUTH_PARAMS
    passed = set(AnthropicClientKwargs.__annotations__) & ANTHROPIC_AUTH_PARAMS
    assert passed == {"aws_region", "aws_profile", "base_url", "http_client", "default_headers"}


# -- OpenAI -----------------------------------------------------------------------------------


def test_openai_bedrock_path_reads_only_region_refused_gated_and_webhook_variables() -> None:
    reads = _scan(_module_paths((), OPENAI_SCAN_MODULES))
    assert reads == OPENAI_ENV_READS
    refused = reads - REGION_READS - OPENAI_PROVIDER_GATED_READS - OPENAI_WEBHOOK_READS
    assert refused <= set(sdk_transport.FORBIDDEN_ENV_VARS)


def test_openai_bedrock_constructor_parameters_are_pinned() -> None:
    assert _auth_params(AsyncBedrockOpenAI) == OPENAI_AUTH_PARAMS
    passed = set(OpenAIClientKwargs.__annotations__) & OPENAI_AUTH_PARAMS
    assert passed == {"aws_region", "base_url", "aws_credentials_provider", "http_client"}


class _StaticSession:
    region_name = REGION

    def get_credentials(self) -> Credentials:
        return Credentials("AKIDEXAMPLE", "fake-secret-key")


@pytest.fixture
def gated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    values = {name: f"planted-{name.lower()}" for name in OPENAI_PROVIDER_GATED_READS}
    values["OPENAI_BASE_URL"] = "https://api.example.invalid/v1"
    values["OPENAI_CUSTOM_HEADERS"] = "Authorization: Bearer planted-custom-header"
    for name in sdk_transport.FORBIDDEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    yield values


def test_openai_client_ignores_every_provider_gated_variable(gated_env: dict[str, str]) -> None:
    client = AsyncBedrockOpenAI(
        **openai_client_kwargs(
            region=REGION,
            http_client=build_http_client(
                transport=httpx2.MockTransport(lambda _: httpx2.Response(200))
            ),
            credentials_provider=session_credentials_provider(_StaticSession()),
        )
    )
    assert_runtime_client(client, region=REGION)
    assert client.api_key == ""
    assert client.organization is None
    assert client.project is None
    assert client.base_url.host == f"bedrock-runtime.{REGION}.amazonaws.com"
    headers: Any = client.default_headers
    assert not any("planted" in str(value) for value in headers.values())


def test_control_a_plain_openai_client_adopts_the_gated_variables(
    gated_env: dict[str, str],
) -> None:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(http_client=build_http_client())
    assert client.api_key == gated_env["OPENAI_API_KEY"]
    assert client.organization == gated_env["OPENAI_ORG_ID"]
    assert client.project == gated_env["OPENAI_PROJECT_ID"]
    assert client.base_url.host == "api.example.invalid"
    headers: Any = client.default_headers
    assert any("planted" in str(value) for value in headers.values())
