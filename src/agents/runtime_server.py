# SPDX-License-Identifier: MIT
"""AgentCore Runtime HTTP adapter for :mod:`agents.runtime_entrypoint`.

Implements the AgentCore Runtime HTTP protocol contract on ``0.0.0.0:8080``: ``GET /ping`` for
health and ``POST /invocations`` for the agent interaction, with a ``text/event-stream`` response.
``/invocations`` also accepts ``mode`` values ``isolation_probe``, ``delegation``, ``cancel`` and
``eval_turn`` (JSON, eval tenants only); every mode goes through :func:`_authenticated_payload`, which keeps only the
:data:`crm.envelope.AGENT_IDENTITY_KEY` channel in the SigV4-authenticated payload. Identity, tuning
and event framing are delegated to :mod:`agents.runtime_entrypoint`, and turns are driven by
:func:`agents.turn_driver.drive_turn` with each tier's provider resolved by that tier's family. A
chat turn's stream carries a ``heartbeat`` event after every :data:`HEARTBEAT_INTERVAL_SECONDS`
without another event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, Iterator, Mapping
from typing import Any, Final

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agents.budget import CHAT_BUDGET, EVAL_TURN_HARD_DEADLINE_SECONDS
from agents.cancellation import (
    CancellationStore,
    InMemoryCancellationStore,
    SessionOwner,
)
from agents.conversation_history import (
    ConversationHistoryStore,
    HistoryTurn,
    InMemoryConversationHistory,
)
from agents.events import AgentEvent, ErrorKind
from agents.registry import RegisteredTool, tool_registry_for
from agents.runtime_entrypoint import (
    InMemorySessionStore,
    RuntimeInvocation,
    decode_invocation,
    event_payload,
    format_sse_line,
    resolve_identity,
    resolve_tuning,
    run_turn,
)
from agents.subagents import ALL_SUBAGENT_SPECS, SUBAGENT_SPECS_BY_ROLE
from agents.supervisor import (
    DIRECT_TOOL_NAMES,
    SUPERVISOR_ROLE,
    classify_tool_result,
    describe_truncation,
    is_cancellation_event,
    proposal_event_for,
    synthesize_reply,
)
from agents.tool_dispatch import (
    action_function_name,
    agent_identity_event,
    dispatch_tool_call,
    production_dispatch,
)
from agents.turn_driver import ProviderResolver, TurnRecords, drive_turn
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    Identity,
    strip_identity,
)
from crm.errors import (
    Conflict,
    CrmError,
    ProviderUnavailable,
    ScopeDenied,
    TurnTimeout,
    ValidationFailed,
)
from crm.handlers._common import ok
from crm.store.store import Store
from providers.base import ModelProvider
from providers.capabilities import CapabilityMatrix, Family, load_capability_matrix
from providers.sdk_loggers import quiet_sdk_loggers
from providers.tier_tuning import TurnTuning

logger = logging.getLogger(__name__)

#: Environment variable naming the root log level this container starts with, and its default.
_LOG_LEVEL_ENV_VAR: Final[str] = "LOG_LEVEL"
_DEFAULT_LOG_LEVEL: Final[str] = "INFO"


def configure_logging() -> None:
    """Attach a root log handler so this module's structured lines reach the container's log stream.

    Reads ``LOG_LEVEL`` (default ``INFO``; an unrecognised value falls back to the default). The
    root handler is left alone when one is already attached. Every logger in
    :data:`providers.sdk_loggers.SDK_LOGGERS` is then held at WARNING whatever ``LOG_LEVEL`` says.
    """
    requested = os.environ.get(_LOG_LEVEL_ENV_VAR, _DEFAULT_LOG_LEVEL).strip().upper()
    level = logging.getLevelNamesMapping().get(requested, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    quiet_sdk_loggers()


#: Environment variable naming how this Runtime process invokes models. ``stub`` (the default)
#: never makes a model call. ``anthropic``, ``openai`` and ``live`` are all live modes, in which the
#: adapter is chosen per invocation from the resolved model's family.
_PROVIDER_ENV_VAR: Final[str] = "AGENT_PROVIDER"
_DEFAULT_PROVIDER: Final[str] = "stub"
_STUB_PROVIDER: Final[str] = "stub"
_LIVE_PROVIDER_MODES: Final[frozenset[str]] = frozenset({"anthropic", "openai", "live"})

#: The region a live adapter's ``bedrock-runtime`` client targets and signs against, read from
#: ``AWS_REGION`` with a default.
_AWS_REGION_ENV_VAR: Final[str] = "AWS_REGION"
_DEFAULT_REGION: Final[str] = "us-east-1"

#: The ``error_kind`` a turn-level validation failure (rejected tuning or absent prompt) is reported
#: under, as a single ``error`` event inside a 200 SSE stream; AgentCore drops a container 4xx body.
_VALIDATION_ERROR_KIND: Final[ErrorKind] = "invalid_request"

#: The ``source`` namespace of a page-context descriptor validation failure (``page_context`` /
#: ``page_context.*``). Such a failure is returned as an HTTP 400 rather than an SSE error stream.
_PAGE_CONTEXT_SOURCE_PREFIX: Final[str] = "page_context"


def _is_page_context_validation(exc: ValidationFailed) -> bool:
    """Return whether ``exc`` is a page-context descriptor failure, which is returned as a 400."""
    source = exc.source or ""
    return source == _PAGE_CONTEXT_SOURCE_PREFIX or source.startswith(
        f"{_PAGE_CONTEXT_SOURCE_PREFIX}."
    )


#: Seconds a chat turn's stream may go without an event before a ``heartbeat`` event is emitted.
HEARTBEAT_INTERVAL_SECONDS: Final[float] = 20.0

#: Process-local session event store, shared across requests.
_session_store = InMemorySessionStore()
#: Process-local cooperative cancellation markers, read between agent steps.
_cancellation_store: CancellationStore = InMemoryCancellationStore()
#: Process-local per-session conversation history, scoped to ``(rep_id, tenant)``.
_history_store: ConversationHistoryStore = InMemoryConversationHistory()
_matrix: CapabilityMatrix | None = None
#: Provider selection state: one process-wide stub, and one cached live adapter per family.
_stub_provider: ModelProvider | None = None
_live_providers_by_family: dict[str, ModelProvider] = {}
_store: Store | None = None


def _capability_matrix() -> CapabilityMatrix:
    """Return the process-wide :class:`CapabilityMatrix`, loading it once per process."""
    global _matrix
    if _matrix is None:
        _matrix = load_capability_matrix()
    return _matrix


def _provider_mode() -> str:
    """Return this process's ``AGENT_PROVIDER`` mode, lower-cased.

    Raises:
        RuntimeError: ``AGENT_PROVIDER`` names a mode this module does not recognise.
    """
    mode = os.environ.get(_PROVIDER_ENV_VAR, _DEFAULT_PROVIDER).strip().lower()
    if mode != _STUB_PROVIDER and mode not in _LIVE_PROVIDER_MODES:
        raise RuntimeError(f"unrecognised {_PROVIDER_ENV_VAR} value: {mode!r}")
    return mode


def _shared_stub_provider() -> ModelProvider:
    """Return the one process-wide :class:`providers.stub.StubProvider`, built on first use."""
    global _stub_provider
    if _stub_provider is None:
        from providers.stub import StubProvider

        _stub_provider = StubProvider()
    return _stub_provider


def _model_provider(family: Family | None = None) -> ModelProvider:
    """Return the :class:`ModelProvider` for one invocation, selected by ``AGENT_PROVIDER``.

    ``stub`` (the default) returns one process-wide :class:`providers.stub.StubProvider` and ignores
    ``family``. Any mode in :data:`_LIVE_PROVIDER_MODES` returns the adapter for ``family`` from
    :func:`providers.factory.build_provider_for_family`, built once per family in ``AWS_REGION``
    (default :data:`_DEFAULT_REGION`).

    Args:
        family: The resolved model's family, required in a live mode and ignored for the stub.

    Raises:
        RuntimeError: ``AGENT_PROVIDER`` names a mode this module does not recognise, or a live
            mode is active but no ``family`` was supplied to select an adapter.
    """
    mode = _provider_mode()
    if mode == _STUB_PROVIDER:
        return _shared_stub_provider()
    if family is None:
        raise RuntimeError(
            f"{_PROVIDER_ENV_VAR}={mode!r} is a live mode; a resolved model family is required "
            "to select an adapter per invocation."
        )
    cached = _live_providers_by_family.get(family)
    if cached is None:
        from providers.factory import build_provider_for_family

        region = os.environ.get(_AWS_REGION_ENV_VAR, _DEFAULT_REGION)
        cached = build_provider_for_family(family, region_name=region)
        _live_providers_by_family[family] = cached
    return cached


def _turn_provider_resolver() -> ProviderResolver:
    """Return one chat turn's per-family resolver over :func:`_model_provider`.

    Each family is resolved once per turn, so every tier of that family shares one provider.

    Raises:
        RuntimeError: ``AGENT_PROVIDER`` names a mode this module does not recognise, raised here
            before the turn starts rather than at the first model call.
    """
    _provider_mode()
    by_family: dict[Family, ModelProvider] = {}

    def resolve(family: Family) -> ModelProvider:
        if family not in by_family:
            by_family[family] = _model_provider(family)
        return by_family[family]

    return resolve


def _as_resolver(providers: ProviderResolver | ModelProvider) -> ProviderResolver:
    """Return ``providers`` as a resolver; a single :class:`ModelProvider` serves every family."""
    if isinstance(providers, ModelProvider):
        single = providers
        return lambda _family: single
    return providers


def _record_store() -> Store:
    """Return the process-wide :class:`Store`, constructing its DynamoDB client once per process.

    Reads ``CRM_TABLE_PREFIX`` from the environment, matching ``crm.handlers._common.default_store``.
    Not on the tool-dispatch path, where each action Lambda owns its own store.
    """
    global _store
    if _store is None:
        _store = Store(table_prefix=os.environ.get("CRM_TABLE_PREFIX", ""))
    return _store


#: Module-level, patchable binding of :func:`agents.tool_dispatch.dispatch_tool_call`, used by the
#: delegation mode; the turn path passes an identity-bound dispatch to the turn driver instead.
_dispatch_tool_call = dispatch_tool_call


def _all_tools() -> tuple[RegisteredTool, ...]:
    """Return every tool the supervisor or a subagent may reach, supervisor tools first.

    Supervisor direct tools are listed first, then every subagent's tools, deduplicated by name.
    """
    seen: dict[str, RegisteredTool] = {}
    for tool in tool_registry_for(SUPERVISOR_ROLE):
        seen.setdefault(tool.name, tool)
    for spec in SUBAGENT_SPECS_BY_ROLE.values():
        for tool in spec.tools:
            seen.setdefault(tool.name, tool)
    return tuple(seen.values())


def proposal_event_or_none(tool_output: Mapping[str, Any]) -> AgentEvent | None:
    """Return the structured ``proposal`` event ``tool_output`` carries, or ``None`` if it carries none.

    Rendering is delegated to :func:`agents.supervisor.proposal_event_for`.

    Args:
        tool_output: One action Lambda's own wire response, from :func:`agents.tool_dispatch.dispatch_tool_call`.

    Returns:
        The rendered event, or ``None`` when the response carries no proposal at all — an error
        response, or any tool other than ``submit_proposal``.

    Raises:
        ValueError: The response carries a ``proposal`` block that is not renderable.
    """
    body = tool_output.get("body", tool_output)
    payload = body if isinstance(body, Mapping) else {}
    if not isinstance(payload.get("proposal"), Mapping):
        return None
    return proposal_event_for(dict(tool_output))


def _history_messages(session_id: str, identity: Identity) -> list[dict[str, object]]:
    """Read the session's prior turns for ``identity`` and render them as `{role, content}` dicts.

    Each prior :class:`agents.conversation_history.HistoryTurn` becomes a user message then an
    assistant message, oldest first. A session owned by a different principal yields no messages.
    """
    try:
        turns: tuple[HistoryTurn, ...] = _history_store.history_for(session_id, identity=identity)
    except ScopeDenied:
        logger.info(json.dumps({"event": "history_read_denied"}))
        return []
    messages: list[dict[str, object]] = []
    for turn in turns:
        messages.append({"role": "user", "content": turn.prompt})
        messages.append({"role": "assistant", "content": turn.answer})
    return messages


def _record_history(invocation: RuntimeInvocation, *, answer: str) -> None:
    """Record this turn's ``(prompt, answer)`` for the session, scoped to the caller's identity.

    Nothing is recorded when the ``session_id`` belongs to a different principal.
    """
    try:
        _history_store.record_turn(
            invocation.session_id,
            identity=invocation.identity,
            prompt=invocation.prompt,
            answer=answer,
        )
    except ScopeDenied:
        logger.info(json.dumps({"event": "history_write_denied"}))


async def _agent_events_for_turn(
    invocation: RuntimeInvocation,
    providers: ProviderResolver | ModelProvider,
    *,
    matrix: CapabilityMatrix,
    max_rounds: int | None = None,
    cancellation: CancellationStore | None = None,
) -> AsyncGenerator[AgentEvent]:
    """Drive one turn to completion by delegating to :func:`agents.turn_driver.drive_turn`.

    Supplies the identity-bound :func:`agents.tool_dispatch.production_dispatch`, the decoded
    per-tier ``tuning``, the per-family ``providers`` resolver (a single :class:`ModelProvider` is
    used for every family), :data:`agents.budget.CHAT_BUDGET`, and the session's prior turns, then
    re-yields the driver's events. On a terminal ``completed`` event the turn's
    ``(prompt, answer)`` is recorded to history, unless the turn was cancelled.
    """
    records = TurnRecords()
    history_messages = _history_messages(invocation.session_id, invocation.identity)
    final_answer: str | None = None
    async for event in drive_turn(
        prompt=invocation.prompt,
        session_id=invocation.session_id,
        providers=_as_resolver(providers),
        matrix=matrix,
        tuning=invocation.tuning,
        budget=CHAT_BUDGET,
        dispatch=production_dispatch(invocation.identity),
        records=records,
        page_context=invocation.page_context,
        history_messages=history_messages,
        max_rounds=max_rounds,
        cancellation=cancellation,
    ):
        if event.kind == "completed" and event.text is not None:
            final_answer = None if is_cancellation_event(event) else event.text
        yield event

    if final_answer is not None:
        _record_history(invocation, answer=final_answer)


async def _next_event(agen: AsyncGenerator[AgentEvent]) -> AgentEvent | None:
    """Advance ``agen`` by one event, returning ``None`` once it is exhausted."""
    try:
        return await agen.__anext__()
    except StopAsyncIteration:
        return None


def _sync_agent_events(
    invocation: RuntimeInvocation,
    providers: ProviderResolver | ModelProvider,
    *,
    matrix: CapabilityMatrix,
    cancellation: CancellationStore | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> Iterator[AgentEvent]:
    """Bridge the async :func:`_agent_events_for_turn` generator into a plain sync iterator.

    Drives the generator inside one new event loop, for
    :func:`agents.runtime_entrypoint.run_turn`'s synchronous ``Iterator[AgentEvent]`` input. Each
    step is awaited for at most ``heartbeat_interval`` seconds; on timeout a ``heartbeat`` event is
    yielded and the same pending step is awaited again, so the turn itself is never interrupted.
    """
    loop = asyncio.new_event_loop()
    agen = _agent_events_for_turn(invocation, providers, matrix=matrix, cancellation=cancellation)
    pending: asyncio.Task[AgentEvent | None] | None = None
    try:
        while True:
            if pending is None:
                pending = loop.create_task(_next_event(agen))
            done, _ = loop.run_until_complete(asyncio.wait({pending}, timeout=heartbeat_interval))
            if not done:
                yield AgentEvent(kind="heartbeat")
                continue
            event = pending.result()
            pending = None
            if event is None:
                return
            yield event
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            loop.run_until_complete(asyncio.gather(pending, return_exceptions=True))
        loop.run_until_complete(agen.aclose())
        loop.close()


async def ping(_request: Request) -> Response:
    """``GET /ping`` -- AgentCore Runtime health check.

    Always reports ``Healthy``.
    """
    return JSONResponse({"status": "Healthy"})


#: Payload field selecting a mode, and the value selecting the read-only isolation probe.
_MODE_KEY: Final[str] = "mode"
_ISOLATION_PROBE_MODE: Final[str] = "isolation_probe"

#: Payload field naming the subagent role to isolate. ``None`` (or an absent field) selects the
#: supervisor.
_ROLE_KEY: Final[str] = "role"

#: Payload field naming which synthetic outcome shape the probe should classify.
_TRIGGER_KEY: Final[str] = "trigger"


def _advertised_tools_for(role: str | None) -> list[str] | None:
    """Return the tool names ``role`` advertises, or ``None`` when ``role`` is not a subagent.

    Read from :func:`agents.registry.tool_registry_for` and
    :data:`agents.subagents.SUBAGENT_SPECS_BY_ROLE`. The supervisor's surface is its direct tools
    plus every subagent role name.

    Args:
        role: The subagent role to report on, or ``None`` for the supervisor.

    Returns:
        The advertised tool names, sorted; or ``None`` when ``role`` names no known subagent.
    """
    if role is None:
        direct = {tool.name for tool in tool_registry_for(SUPERVISOR_ROLE)}
        return sorted(direct | set(SUBAGENT_SPECS_BY_ROLE))
    spec = SUBAGENT_SPECS_BY_ROLE.get(role)
    if spec is None:
        return None
    return sorted(spec.tool_names)


def _probe_trigger_payload(trigger: str) -> dict[str, Any] | None:
    """Return the canonical handler wire payload ``trigger`` names, or ``None`` if unrecognised.

    Each payload is built with :meth:`crm.errors.ScopeDenied.to_api_response`,
    :func:`crm.handlers._common.ok`, or :meth:`crm.errors.Conflict.to_api_response`; no handler is
    invoked.

    Args:
        trigger: ``"scope_denial"``, ``"empty_result"``, or ``"handler_error"``.

    Returns:
        The wire payload, or ``None`` when ``trigger`` is not one of the three.
    """
    if trigger == "scope_denial":
        return ScopeDenied(
            "That account is outside your book.", missing_scope="account:isolation-probe"
        ).to_api_response()
    if trigger == "empty_result":
        return dict(ok({"items": []}))
    if trigger == "handler_error":
        return Conflict("The record changed before your request completed.").to_api_response()
    return None


def _isolation_probe_response(payload: dict[str, Any]) -> Response:
    """Answer a read-only isolation probe: which tools a role advertises, and nothing else.

    Invokes no tool or model and touches no table. Reports the role's tool surface and, when a
    ``trigger`` is supplied, the :func:`agents.supervisor.classify_tool_result` outcome kind for
    that trigger's canonical payload. Identity resolves through
    :func:`agents.runtime_entrypoint.resolve_identity`; with no usable identity it returns ``401``.

    Args:
        payload: The inbound body, already reduced to the ``agent`` identity channel.

    Returns:
        A JSON response carrying ``mode``, ``role``, ``advertised_tools``, and — only when a
        ``trigger`` was supplied — ``outcome``; or a taxonomy-shaped error response.
    """
    try:
        identity = resolve_identity(payload)
    except CrmError as exc:
        logger.info(json.dumps({"event": "isolation_probe_rejected", "reason": exc.code}))
        return JSONResponse(exc.to_api_response(), status_code=exc.http_status)

    role_raw = payload.get(_ROLE_KEY)
    role = None if role_raw is None else str(role_raw)
    advertised = _advertised_tools_for(role)
    if advertised is None:
        unknown_role = ValidationFailed(
            "That agent role is not recognised.",
            source=f"agent.invocation.{_ROLE_KEY}",
            detail=f"unknown role {role!r}; known roles {sorted(SUBAGENT_SPECS_BY_ROLE)}",
        )
        return JSONResponse(unknown_role.to_api_response(), status_code=unknown_role.http_status)

    body: dict[str, Any] = {
        _MODE_KEY: _ISOLATION_PROBE_MODE,
        _ROLE_KEY: role,
        "advertised_tools": advertised,
    }

    trigger_raw = payload.get(_TRIGGER_KEY)
    if trigger_raw is not None:
        tool_output = _probe_trigger_payload(str(trigger_raw))
        if tool_output is None:
            unknown_trigger = ValidationFailed(
                "That outcome trigger is not recognised.",
                source=f"agent.invocation.{_TRIGGER_KEY}",
                detail=f"unknown trigger {trigger_raw!r}",
            )
            return JSONResponse(
                unknown_trigger.to_api_response(), status_code=unknown_trigger.http_status
            )
        body["outcome"] = classify_tool_result(tool_output).kind

    # Log keys are limited to `scripts/check_log_redaction.py`'s allowlist.
    logger.info(
        json.dumps(
            {
                "event": "isolation_probe",
                "rep_id": identity.rep_id,
                "reason": f"role={role or 'supervisor'} trigger={trigger_raw or 'none'}",
            }
        )
    )
    return JSONResponse(body)


#: Payload field value selecting the delegation mode, and the fields it reads. This mode invokes the
#: named tool's action Lambda for real, without a model call.
_DELEGATION_MODE: Final[str] = "delegation"
_TOOL_NAME_KEY: Final[str] = "tool_name"
_TOOL_INPUT_KEY: Final[str] = "tool_input"


def resolve_delegation_role(tool_name: str) -> str | None:
    """Return the subagent role that owns ``tool_name``, or ``None`` if the supervisor holds it.

    A tool in :data:`agents.supervisor.DIRECT_TOOL_NAMES` resolves to ``None``; any other resolves to
    the first role in :data:`agents.subagents.ALL_SUBAGENT_SPECS` order that holds it.

    Args:
        tool_name: The tool the turn wants called.

    Returns:
        The owning subagent role, or ``None`` when the supervisor holds ``tool_name`` directly or
        when no role holds it at all (the caller distinguishes the two by checking the tool exists).
    """
    if tool_name in DIRECT_TOOL_NAMES:
        return None
    for spec in ALL_SUBAGENT_SPECS:
        if tool_name in spec.tool_names:
            return spec.role
    return None


def _delegation_response(payload: dict[str, Any]) -> Response:
    """Answer a delegation request: route it, dispatch it for real, and classify the outcome.

    Identity resolves through :func:`resolve_identity`; with no usable identity it returns ``401``.
    The tool is routed by :func:`resolve_delegation_role`, dispatched by
    :func:`agents.tool_dispatch.dispatch_tool_call` on the ``agent`` identity channel, and its
    response classified by :func:`agents.supervisor.classify_tool_result`. A ``denied`` or
    ``tool_failure`` outcome carries no ``reported_result``.

    Args:
        payload: The inbound body, already reduced to the ``agent`` identity channel. Reads
            ``tool_name`` and an optional ``tool_input`` object.

    Returns:
        A JSON response carrying ``mode``, ``delegated``, ``delegation``, ``outcome``,
        ``reported_result``, ``proposal_event`` (the proposal event payload when the dispatched
        tool raised a proposal, else ``None``), ``reply_text``, and the Lambda's own
        ``tool_result``; or a taxonomy-shaped error response.
    """
    try:
        identity = resolve_identity(payload)
    except CrmError as exc:
        logger.info(json.dumps({"event": "delegation_rejected", "reason": exc.code}))
        return JSONResponse(exc.to_api_response(), status_code=exc.http_status)

    tool_name = str(payload.get(_TOOL_NAME_KEY) or "")
    tool = {registered.name: registered for registered in _all_tools()}.get(tool_name)
    if tool is None:
        unknown_tool = ValidationFailed(
            "That tool is not recognised.",
            source=f"agent.invocation.{_TOOL_NAME_KEY}",
            detail=f"unknown tool {tool_name!r}",
        )
        return JSONResponse(unknown_tool.to_api_response(), status_code=unknown_tool.http_status)

    raw_input = payload.get(_TOOL_INPUT_KEY)
    tool_input: dict[str, object] = dict(raw_input) if isinstance(raw_input, dict) else {}

    role = resolve_delegation_role(tool_name)
    try:
        tool_result = _dispatch_tool_call(tool, tool_input, identity=identity)
    except Exception:
        logger.exception(json.dumps({"event": "delegation_failed", "reason": "dispatch_failed"}))
        failure = CrmError("The request could not be completed.")
        return JSONResponse(failure.to_api_response(), status_code=failure.http_status)

    outcome = classify_tool_result(tool_result)
    try:
        proposal_event = proposal_event_or_none(tool_result)
    except ValueError as exc:
        logger.warning(json.dumps({"event": "proposal_render_failed", "reason": str(exc)[:200]}))
        unrenderable = CrmError("The proposed change could not be presented for approval.")
        return JSONResponse(unrenderable.to_api_response(), status_code=unrenderable.http_status)
    body: dict[str, Any] = {
        _MODE_KEY: _DELEGATION_MODE,
        "delegated": role is not None,
        "delegation": (
            None
            if role is None
            else {_ROLE_KEY: role, _TOOL_NAME_KEY: tool_name, _TOOL_INPUT_KEY: tool_input}
        ),
        "direct_tool": tool_name if role is None else None,
        "outcome": {
            "kind": outcome.kind,
            "missing_scope": outcome.missing_scope,
            "denial_message": outcome.denial_message,
            "error_message": outcome.error_message,
        },
        "reported_result": outcome.data,
        "proposal_event": None if proposal_event is None else event_payload(proposal_event),
        "reply_text": synthesize_reply(
            outcome,
            truncation_notice=describe_truncation(tool_result, event_truncated=False),
        ),
        "tool_result": tool_result,
    }

    # Log keys are limited to `scripts/check_log_redaction.py`'s allowlist.
    logger.info(
        json.dumps(
            {
                "event": "delegation",
                "rep_id": identity.rep_id,
                "reason": f"role={role or SUPERVISOR_ROLE} tool={tool_name} outcome={outcome.kind}",
            }
        )
    )
    return JSONResponse(body)


#: Payload field value selecting the cooperative-cancellation mode, and the field naming which
#: conversation to stop.
_CANCEL_MODE: Final[str] = "cancel"
_SESSION_ID_KEY: Final[str] = "session_id"


def _authenticated_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` carrying only the ``agent`` identity channel, every other channel removed.

    The identity-preparation step for every mode of :func:`invocations`. The relay-written
    :data:`crm.envelope.AGENT_IDENTITY_KEY` block is preserved and the ``_identity`` and
    ``requestContext`` channels are stripped. An absent or malformed block is passed through
    unrepaired.
    """
    agent_identity = payload.get(AGENT_IDENTITY_KEY)
    sanitised = strip_identity(payload)
    if AGENT_IDENTITY_KEY in payload:
        sanitised[AGENT_IDENTITY_KEY] = agent_identity
    return sanitised


def _register_cancellable_turn(invocation: RuntimeInvocation) -> CancellationStore | None:
    """Bind this turn's session to its caller so a later ``cancel`` mode can be authorized.

    Args:
        invocation: The decoded invocation whose session is being registered.

    Returns:
        The store the turn's guard should consult, or ``None`` when the invocation carries no usable
        ``session_id``.

    Raises:
        crm.errors.ScopeDenied: The session id is already bound to a different principal.
    """
    if not invocation.session_id.strip():
        return None
    _cancellation_store.register_turn(
        invocation.session_id, owner=SessionOwner.of(invocation.identity)
    )
    return _cancellation_store


def _cancel_response(payload: dict[str, Any]) -> Response:
    """Answer a ``{"mode": "cancel"}`` payload by writing the cancellation marker for a session.

    Identity resolves through :func:`agents.runtime_entrypoint.resolve_identity`; authorization is
    :meth:`agents.cancellation.CancellationStore.request_cancellation`'s owner check.

    Args:
        payload: The inbound body, already reduced to the ``agent`` identity channel.

    Returns:
        ``{"session_id": …, "disposition": …}`` where ``disposition`` is ``accepted``,
        ``already_requested``, or ``already_terminal``. A failure is a taxonomy-shaped error
        response: ``401`` with no usable identity, ``403`` for a session the caller does not own.
    """
    try:
        identity = resolve_identity(payload)
        session_id = str(payload.get(_SESSION_ID_KEY) or "")
        disposition = _cancellation_store.request_cancellation(
            session_id, requester=SessionOwner.of(identity)
        )
    except CrmError as exc:
        logger.info(json.dumps({"event": "cancel_rejected", "reason": exc.code}))
        return JSONResponse(exc.to_api_response(), status_code=exc.http_status)

    # Log keys are limited to `scripts/check_log_redaction.py`'s allowlist.
    logger.info(
        json.dumps(
            {
                "event": "cancel_requested",
                "rep_id": identity.rep_id,
                "session_id": session_id,
                "reason": f"disposition={disposition}",
            }
        )
    )
    return JSONResponse({_SESSION_ID_KEY: session_id, "disposition": disposition})


#: Payload field value selecting the eval-turn mode, and the payload keys it reads beyond
#: ``prompt`` and ``tuning``.
_EVAL_TURN_MODE: Final[str] = "eval_turn"
_TASK_ID_KEY: Final[str] = "task_id"
_PROVIDER_MODE_KEY: Final[str] = "provider_mode"
#: The accepted ``provider_mode`` values.
_EVAL_PROVIDER_MODES: Final[frozenset[str]] = frozenset({"stub", "live"})
#: The ``source`` recorded on an eval turn's tuning when the block names none.
_EVAL_TUNING_SOURCE: Final[str] = "eval.arm"
#: Hard wall-clock bound on one eval turn, after which the turn is cancelled at its next await. It
#: lies past the soft cap, which normally ends the turn first as a ``budget_exhausted`` observation.
_EVAL_TURN_DEADLINE_SECONDS: Final[float] = EVAL_TURN_HARD_DEADLINE_SECONDS


def _eval_tenant_or_denied(identity: Identity) -> str:
    """Return ``identity.tenant`` when it is an ``eval-<run_id>`` tenant.

    Raises:
        crm.errors.ScopeDenied: The tenant is not an eval tenant, or names no run.
    """
    from eval.tenancy import run_id_for_tenant

    try:
        run_id = run_id_for_tenant(identity.tenant)
    except ValueError:
        run_id = ""
    if not run_id:
        raise ScopeDenied(
            "Eval turns run only in an eval tenant.",
            missing_scope="tenant:eval",
            detail=f"tenant {identity.tenant!r} is not an eval tenant",
        )
    return identity.tenant


def _eval_turn_providers(provider_mode: str) -> ProviderResolver:
    """Return the eval turn's per-family resolver for ``provider_mode``.

    ``stub`` always resolves to the process-wide stub. ``live`` requires this process's
    ``AGENT_PROVIDER`` to be a live mode, and wraps each family's adapter in a fresh
    :class:`eval.live.ThrottleRetryingProvider` per turn so exclusions never cross turns.

    Raises:
        crm.errors.ValidationFailed: ``provider_mode`` is not ``stub`` or ``live``, or is ``live``
            while ``AGENT_PROVIDER`` is not.
        RuntimeError: ``provider_mode`` is ``live`` and ``AGENT_PROVIDER`` names a mode this module
            does not recognise.
    """
    if provider_mode not in _EVAL_PROVIDER_MODES:
        raise ValidationFailed(
            "provider_mode must be 'stub' or 'live'.",
            source=f"agent.invocation.{_PROVIDER_MODE_KEY}",
            detail=f"got {provider_mode!r}",
        )
    if provider_mode == _STUB_PROVIDER:
        stub = _shared_stub_provider()
        return lambda _family: stub
    if _provider_mode() == _STUB_PROVIDER:
        raise ValidationFailed(
            "This Runtime does not run live eval turns.",
            source=f"agent.invocation.{_PROVIDER_MODE_KEY}",
            detail=f"{_PROVIDER_ENV_VAR} is {_STUB_PROVIDER!r}",
        )
    from eval.live import ThrottleRetryingProvider

    retrying: dict[Family, ModelProvider] = {}

    def resolve(family: Family) -> ModelProvider:
        if family not in retrying:
            retrying[family] = ThrottleRetryingProvider(_model_provider(family))
        return retrying[family]

    return resolve


def _required_string(payload: dict[str, Any], key: str) -> str:
    """Return ``payload[key]`` as a non-blank string.

    Raises:
        crm.errors.ValidationFailed: The field is absent, not a string, or blank.
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(
            f"{key} is required.", source=f"agent.invocation.{key}", detail=f"got {value!r}"
        )
    return value


def _run_eval_turn(
    *,
    prompt: str,
    task_id: str,
    tenant: str,
    providers: ProviderResolver,
    matrix: CapabilityMatrix,
    tuning: TurnTuning,
    identity: Identity,
) -> dict[str, Any]:
    """Drive one eval turn on a fresh event loop and return its serialised observation.

    Raises:
        TimeoutError: The turn outlived :data:`_EVAL_TURN_DEADLINE_SECONDS`.
        eval.live.LiveInvocationError: A live provider failed after its bounded retries.
    """
    from eval.turn_observation import observe_turn, serialise_turn

    turn = asyncio.run(
        asyncio.wait_for(
            observe_turn(
                prompt=prompt,
                task_id=task_id,
                tenant=tenant,
                providers=providers,
                matrix=matrix,
                tuning=tuning,
                dispatch=production_dispatch(identity),
                turn_clock=time.monotonic,
            ),
            timeout=_EVAL_TURN_DEADLINE_SECONDS,
        )
    )
    return serialise_turn(turn)


def _eval_turn_failure(exc: Exception) -> CrmError:
    """Classify an exception raised by :func:`_run_eval_turn` as a taxonomy error."""
    from eval.live import LiveInvocationError

    if isinstance(exc, TimeoutError):
        return TurnTimeout(
            "The eval turn exceeded its time limit.",
            detail=f"deadline {_EVAL_TURN_DEADLINE_SECONDS:.0f}s",
        )
    if isinstance(exc, LiveInvocationError):
        return ProviderUnavailable(
            "The model provider failed after retries.", detail=type(exc).__name__
        )
    return CrmError("The eval turn could not be completed.", detail=type(exc).__name__)


async def _eval_turn_response(payload: dict[str, Any]) -> Response:
    """Answer a ``{"mode": "eval_turn"}`` payload: drive one eval turn and return it as JSON.

    Identity resolves through :func:`resolve_identity`, and its tenant must be ``eval-<run_id>``.
    The arm's schema-3 ``tuning`` is required and resolved before any model call. The turn runs
    under :data:`agents.budget.EVAL_BUDGET` via :func:`eval.turn_observation.observe_turn`, off the
    server's event loop.

    Args:
        payload: The inbound body, already reduced to the ``agent`` identity channel. Reads
            ``prompt``, ``task_id``, ``provider_mode`` and ``tuning``.

    Returns:
        200 JSON :func:`eval.turn_observation.serialise_turn` body; or a taxonomy-shaped error:
        ``401`` with no usable identity, ``403`` for a non-eval tenant, ``400`` for a missing or
        invalid field, ``503`` when a live provider fails after retries, ``504`` when the turn
        outlives :data:`_EVAL_TURN_DEADLINE_SECONDS`, and ``500`` for any other failure.
    """
    try:
        identity = resolve_identity(payload)
        tenant = _eval_tenant_or_denied(identity)
        prompt = _required_string(payload, "prompt")
        task_id = _required_string(payload, _TASK_ID_KEY)
        provider_mode = _required_string(payload, _PROVIDER_MODE_KEY)
        if payload.get("tuning") is None:
            raise ValidationFailed(
                "An eval turn requires the arm's tuning.", source="agent.invocation.tuning"
            )
        matrix = _capability_matrix()
        tuning, _ = resolve_tuning(
            matrix, raw_tuning=payload["tuning"], default_source=_EVAL_TUNING_SOURCE
        )
        providers = _eval_turn_providers(provider_mode)
    except CrmError as exc:
        _log_invocation_rejected(exc)
        return JSONResponse(exc.to_api_response(), status_code=exc.http_status)
    except Exception as exc:
        logger.exception(
            json.dumps({"event": "eval_turn_setup_failed", "reason": type(exc).__name__})
        )
        failure = CrmError("The eval turn could not be started.", detail=type(exc).__name__)
        return JSONResponse(failure.to_api_response(), status_code=failure.http_status)

    session_id = identity.session_id or task_id

    try:
        body = await run_in_threadpool(
            lambda: _run_eval_turn(
                prompt=prompt,
                task_id=task_id,
                tenant=tenant,
                providers=providers,
                matrix=matrix,
                tuning=tuning,
                identity=identity,
            )
        )
    except Exception as exc:
        failure = _eval_turn_failure(exc)
        # Log keys are limited to `scripts/check_log_redaction.py`'s allowlist.
        logger.exception(
            json.dumps(
                {
                    "event": "eval_turn_failed",
                    "session_id": session_id,
                    "correlation_id": tenant,
                    "reason": f"{failure.code} {type(exc).__name__}",
                }
            )
        )
        return JSONResponse(failure.to_api_response(), status_code=failure.http_status)

    logger.info(
        json.dumps(
            {
                "event": "eval_turn",
                "rep_id": identity.rep_id,
                "session_id": session_id,
                "correlation_id": tenant,
                "reason": f"provider_mode={provider_mode}",
            }
        )
    )
    return JSONResponse(body)


def _log_invocation_rejected(exc: CrmError) -> None:
    """Log a rejected invocation, folding the offending control ``source`` into ``reason``.

    ``source`` is a control id such as ``agent.invocation.tuning``, or ``None`` for errors that
    carry none.
    """
    logger.info(
        json.dumps(
            {
                "event": "invocation_rejected",
                "reason": f"{exc.code} source={getattr(exc, 'source', None)}",
            }
        )
    )


def _validation_error_stream(exc: ValidationFailed) -> StreamingResponse:
    """Return a 200 SSE stream carrying exactly one ``error`` event, then a normal end.

    The event carries :data:`_VALIDATION_ERROR_KIND` and ``exc.message``; ``exc.detail`` is excluded.
    """
    event = AgentEvent(
        kind="error",
        error_kind=_VALIDATION_ERROR_KIND,
        error_message=exc.message,
    )

    def _body() -> Iterator[bytes]:
        yield format_sse_line(event).encode("utf-8")

    return StreamingResponse(_body(), media_type="text/event-stream")


async def invocations(request: Request) -> Response:
    """``POST /invocations`` -- the primary agent interaction endpoint (JSON in; SSE or JSON out).

    Reduces the body with :func:`_authenticated_payload`, answers the ``isolation_probe``,
    ``delegation``, ``cancel`` and ``eval_turn`` modes, and otherwise decodes a turn with
    :func:`agents.runtime_entrypoint.decode_invocation` and streams
    :func:`agents.runtime_entrypoint.run_turn`'s already-framed SSE lines verbatim.

    A turn-level :class:`crm.errors.ValidationFailed` (rejected tuning or absent prompt) becomes a
    200 SSE stream with one ``error`` event. A page-context descriptor failure and every other
    :class:`crm.errors.CrmError` keep their HTTP status. A provider that cannot be resolved before
    the stream starts is a taxonomy-shaped 500 JSON response; an exception after the stream starts
    is reported as a terminal ``error`` event. The non-turn modes, ``eval_turn`` included, answer
    in JSON.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"code": "validation_failed", "message": "Request body was not valid JSON."}},
            status_code=400,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {
                "error": {
                    "code": "validation_failed",
                    "message": "Request body must be a JSON object.",
                }
            },
            status_code=400,
        )

    payload = _authenticated_payload(payload)

    if payload.get(_MODE_KEY) == _ISOLATION_PROBE_MODE:
        return _isolation_probe_response(payload)

    if payload.get(_MODE_KEY) == _DELEGATION_MODE:
        return _delegation_response(payload)

    # Dispatched before decoding, which requires a prompt.
    if payload.get(_MODE_KEY) == _CANCEL_MODE:
        return _cancel_response(payload)

    if payload.get(_MODE_KEY) == _EVAL_TURN_MODE:
        return await _eval_turn_response(payload)

    matrix = _capability_matrix()
    try:
        invocation = decode_invocation(payload, matrix)
    except ValidationFailed as exc:
        _log_invocation_rejected(exc)
        if _is_page_context_validation(exc):
            return JSONResponse(exc.to_api_response(), status_code=exc.http_status)
        return _validation_error_stream(exc)
    except CrmError as exc:
        _log_invocation_rejected(exc)
        return JSONResponse(exc.to_api_response(), status_code=exc.http_status)

    # Every tier's provider is built before the session is bound or a header is sent.
    try:
        providers = _turn_provider_resolver()
        for family in sorted({tier.model.family for tier in invocation.resolved_tiers.values()}):
            providers(family)
    except Exception as exc:
        logger.exception(
            json.dumps(
                {
                    "event": "provider_unavailable",
                    "session_id": invocation.session_id,
                    "reason": type(exc).__name__,
                }
            )
        )
        failure = CrmError("The assistant is not available right now.", detail=type(exc).__name__)
        return JSONResponse(failure.to_api_response(), status_code=failure.http_status)

    # Registered before the response headers are committed, so a collision returns a 403.
    try:
        cancellation = _register_cancellable_turn(invocation)
    except CrmError as exc:
        _log_invocation_rejected(exc)
        return JSONResponse(exc.to_api_response(), status_code=exc.http_status)

    resume_from_raw = payload.get("resume_from", 0)
    resume_from = int(resume_from_raw) if isinstance(resume_from_raw, int | str) else 0

    events = _sync_agent_events(invocation, providers, matrix=matrix, cancellation=cancellation)

    def _sse_body() -> Iterator[bytes]:
        try:
            for line in run_turn(invocation, events, store=_session_store, resume_from=resume_from):
                yield line.encode("utf-8")
        except Exception:
            # Headers are already sent, so the failure is reported as a terminal `error` event.
            logger.exception(
                json.dumps({"event": "turn_failed", "session_id": invocation.session_id})
            )
            failure = AgentEvent(
                kind="error",
                error_kind="model_failure",
                error_message="The assistant could not complete this turn.",
            )
            yield format_sse_line(failure).encode("utf-8")

    logger.info(
        json.dumps(
            {
                "event": "invocation_started",
                "rep_id": invocation.identity.rep_id,
                "session_id": invocation.session_id,
            }
        )
    )

    return StreamingResponse(_sse_body(), media_type="text/event-stream")


configure_logging()

app = Starlette(
    routes=[
        Route("/ping", ping, methods=["GET"]),
        Route("/invocations", invocations, methods=["POST"]),
    ]
)


__all__ = [
    "action_function_name",
    "agent_identity_event",
    "app",
    "configure_logging",
    "invocations",
    "ping",
    "resolve_delegation_role",
]
