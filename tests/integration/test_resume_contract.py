# SPDX-License-Identifier: MIT
"""The reconnect fields `resume` and `resume_from` mean the same thing on every hop of a chat turn.

The two wire names are read as text from each of their owners: the SPA body builder
(`frontend/src/components/AssistantPanel/useAssistantTurn.ts`), the SPA reconnect bodies
(`frontend/src/hooks/useAgentStream.ts`), the relay decode (`src/relay/src/requests.ts`), the relay
payload (`src/relay/src/runtime.ts`) and the Runtime reader (`src/agents/runtime_server.py`). Each
read must find its declaration exactly once, so a rename or removal fails rather than skips.

Both enforcing sides then handle the same bodies. The real relay `decodeBody` and
`buildRuntimePayload` are bundled with the frontend's pinned esbuild, with the AWS SDK aliased to a
stub generated from `runtime.ts`'s own import names, and run under node to turn each SPA body into a
Runtime payload. That payload is posted to the real `agents.runtime_server.app` with a stub provider
that records each model request. Those cases need `node` and `frontend/node_modules`; without them
they skip, unless `CRM_REQUIRE_TS_CONTRACTS=1`, which turns the skip into a failure.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from scripts import proc_run
from starlette.testclient import TestClient

from agents import runtime_server
from agents.cancellation import InMemoryCancellationStore
from agents.conversation_history import InMemoryConversationHistory
from agents.runtime_entrypoint import InMemorySessionStore
from crm.envelope import CLAIM_BOOK, CLAIM_REP_ID, CLAIM_TENANT
from providers.base import ProviderEvent, ResolvedInvocation
from providers.stub import StubProvider

REPO_ROOT = Path(__file__).resolve().parents[2]

SPA_BUILDER = (
    REPO_ROOT / "frontend" / "src" / "components" / "AssistantPanel" / "useAssistantTurn.ts"
)
SPA_HOOK = REPO_ROOT / "frontend" / "src" / "hooks" / "useAgentStream.ts"
RELAY_REQUESTS = REPO_ROOT / "src" / "relay" / "src" / "requests.ts"
RELAY_RUNTIME = REPO_ROOT / "src" / "relay" / "src" / "runtime.ts"
RUNTIME_SERVER = REPO_ROOT / "src" / "agents" / "runtime_server.py"

FRONTEND = REPO_ROOT / "frontend"
ESBUILD = FRONTEND / "node_modules" / ".bin" / "esbuild"
REQUIRE_ENV_VAR = "CRM_REQUIRE_TS_CONTRACTS"

#: Owner -> (pattern capturing the `resume` wire name, pattern capturing the `resume_from` one).
_TS_READERS: dict[str, tuple[Path, re.Pattern[str], re.Pattern[str]]] = {
    "spa_builder": (
        SPA_BUILDER,
        re.compile(r"\{ (\w+): true as const \}"),
        re.compile(r"\{ ([a-z_]+): options\.resumeFrom \}"),
    ),
    "relay_decode": (
        RELAY_REQUESTS,
        re.compile(r'const resume = body\["(\w+)"\] === true;'),
        re.compile(r'const resumeFrom = typeof body\["(\w+)"\] === "number"'),
    ),
    "relay_payload": (
        RELAY_RUNTIME,
        re.compile(r'if \(request\.resume === true\) \{\s*payload\["(\w+)"\] = true;'),
        re.compile(
            r'if \(request\.resumeFrom !== undefined\) \{\s*payload\["(\w+)"\] = request\.resumeFrom;'
        ),
    ),
}

#: The SPA's two reconnect bodies (automatic and manual): the applied-event count, then the flag.
_SPA_RECONNECT_BODY = re.compile(r"(\w+): eventCountRef\.current,\s*(\w+): true,")


def _one(pattern: re.Pattern[str], source: str, where: str) -> str:
    matches = pattern.findall(source)
    assert len(matches) == 1, f"{where}: expected one match of {pattern.pattern!r}, found {matches}"
    return str(matches[0])


def ts_wire_names(owner: str) -> tuple[str, str]:
    """Return ``(resume name, resume_from name)`` as ``owner``'s TypeScript source spells them."""
    path, resume, resume_from = _TS_READERS[owner]
    source = path.read_text(encoding="utf-8")
    return _one(resume, source, owner), _one(resume_from, source, owner)


def spa_reconnect_wire_names(source: str) -> set[tuple[str, str]]:
    """Return the ``(resume_from name, resume name)`` pairs of the SPA's two reconnect bodies."""
    pairs = _SPA_RECONNECT_BODY.findall(source)
    assert len(pairs) == 2, f"expected the automatic and the manual reconnect body, found {pairs}"
    return {(str(a), str(b)) for a, b in pairs}


def runtime_wire_names(source: str) -> tuple[str, str]:
    """Return the Runtime's ``(resume name, resume_from name)``: `_RESUME_KEY` and the `.get` key."""
    tree = ast.parse(source)
    resume = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_RESUME_KEY"
        and isinstance(node.value, ast.Constant)
    ]
    resume_from = [
        node.value.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "resume_from_raw"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "get"
        and node.value.args
        and isinstance(node.value.args[0], ast.Constant)
    ]
    assert len(resume) == 1, f"expected one literal _RESUME_KEY, found {resume}"
    assert len(resume_from) == 1, (
        f"expected one resume_from_raw = payload.get(...), found {resume_from}"
    )
    return str(resume[0]), str(resume_from[0])


def test_every_owner_spells_resume_and_resume_from_the_same_way() -> None:
    names = {owner: ts_wire_names(owner) for owner in _TS_READERS}
    names["runtime"] = runtime_wire_names(RUNTIME_SERVER.read_text(encoding="utf-8"))
    assert len(set(names.values())) == 1, names
    resume, resume_from = names["runtime"]
    assert spa_reconnect_wire_names(SPA_HOOK.read_text(encoding="utf-8")) == {(resume_from, resume)}


def test_the_runtime_key_read_from_source_is_the_one_it_reads() -> None:
    assert runtime_wire_names(RUNTIME_SERVER.read_text(encoding="utf-8"))[0] == (
        runtime_server._RESUME_KEY
    )


@pytest.mark.parametrize(
    "source",
    [
        '_RESUME_KEY: Final[str] = "resume"\n_RESUME_KEY: Final[str] = "resume"\n'
        'resume_from_raw = payload.get("resume_from", 0)\n',
        'resume_from_raw = payload.get("resume_from", 0)\n',
        '_RESUME_KEY: Final[str] = "resume"\n',
    ],
    ids=["duplicated-key", "missing-key", "missing-resume-from"],
)
def test_the_runtime_reader_refuses_a_missing_or_duplicated_declaration(source: str) -> None:
    with pytest.raises(AssertionError):
        runtime_wire_names(source)


def test_the_spa_reader_refuses_a_single_reconnect_body() -> None:
    with pytest.raises(AssertionError):
        spa_reconnect_wire_names("resume_from: eventCountRef.current,\n  resume: true,\n")


# --------------------------------------------------------------------------- executing both sides

_SESSION = "s" * 40
#: The events the mid-turn reconnect has already applied.
_MID_TURN_APPLIED = 1
_CLAIMS = {CLAIM_REP_ID: "rep-ashford", CLAIM_BOOK: "acct-0000", CLAIM_TENANT: "demo"}

#: Case name -> the SPA body (the relay's input), without `prompt` and `session_id`.
_BODIES: dict[str, dict[str, object]] = {
    "first_send": {"resume_from": 0},
    "zero_frame_reconnect": {"resume_from": 0, "resume": True},
    "mid_turn_reconnect": {"resume_from": _MID_TURN_APPLIED, "resume": True},
    "follow_up": {"resume_from": 3},
    "string_true": {"resume_from": 0, "resume": "true"},
    "number_one": {"resume_from": 0, "resume": 1},
}

_DRIVER = """
const requests = await import(process.argv[2]);
const runtime = await import(process.argv[3]);
const bodies = JSON.parse(await new Promise((resolve) => {
  let text = "";
  process.stdin.on("data", (chunk) => (text += chunk));
  process.stdin.on("end", () => resolve(text));
}));
const out = {};
for (const [name, body] of Object.entries(bodies)) {
  const decoded = requests.decodeBody({ body: JSON.stringify(body), isBase64Encoded: false });
  out[name] = runtime.buildRuntimePayload({ ...decoded, claims: CLAIMS });
}
console.log(JSON.stringify(out));
"""

_SDK_IMPORT = re.compile(r'import \{([^}]*)\} from "(@aws-sdk/[\w-]+)";')


def _sdk_stub(runtime_source: str) -> tuple[str, str]:
    """Return ``(package, stub module text)``: one inert class per name `runtime.ts` imports."""
    (match,) = _SDK_IMPORT.findall(runtime_source)
    names = [name.strip() for name in match[0].split(",") if name.strip()]
    assert names, "runtime.ts imports nothing from the AWS SDK"
    return match[1], "".join(f"export class {name} {{}}\n" for name in names)


def _bundle(source: Path, outfile: Path, *, alias: str) -> None:
    proc_run.run(
        [
            str(ESBUILD),
            str(source),
            "--bundle",
            "--platform=node",
            "--format=esm",
            f"--alias:{alias}",
            f"--outfile={outfile}",
            "--log-level=error",
        ],
        cwd=source.parent,
        check=True,
    )


@pytest.fixture(scope="module")
def relay_payloads(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, dict[str, Any]]]:
    """The real relay's Runtime payload for every body in `_BODIES`, from the bundled TypeScript."""
    reason = None
    if shutil.which("node") is None:
        reason = "node is not on PATH"
    elif not ESBUILD.exists():
        reason = f"{ESBUILD} is absent (run `cd frontend && npm ci`)"
    if reason is not None:
        if os.environ.get(REQUIRE_ENV_VAR) == "1":
            pytest.fail(f"{REQUIRE_ENV_VAR}=1 and {reason}")
        pytest.skip(reason)
    work = tmp_path_factory.mktemp("resume-contract")
    package, stub_text = _sdk_stub(RELAY_RUNTIME.read_text(encoding="utf-8"))
    stub = work / "sdk-stub.mjs"
    stub.write_text(stub_text, encoding="utf-8")
    alias = f"{package}={stub}"
    _bundle(RELAY_REQUESTS, work / "requests.mjs", alias=alias)
    _bundle(RELAY_RUNTIME, work / "runtime.mjs", alias=alias)
    driver = work / "driver.mjs"
    driver.write_text(f"const CLAIMS = {json.dumps(_CLAIMS)};\n{_DRIVER}", encoding="utf-8")
    bodies = {
        name: {"prompt": f"prompt for {name}", "session_id": _SESSION, **fields}
        for name, fields in _BODIES.items()
    }
    completed = proc_run.run(
        ["node", str(driver), str(work / "requests.mjs"), str(work / "runtime.mjs")],
        input=json.dumps(bodies),
        capture_output=True,
        text=True,
        check=True,
    )
    yield json.loads(completed.stdout)


class _RecordingStub(StubProvider):
    """The stub provider, offered no tools so no Lambda is dispatched, counting its requests."""

    def __init__(self) -> None:
        self.requests = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.requests += 1
        async for event in super().stream(invocation, messages, []):
            yield event


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> _RecordingStub:
    """A fresh in-process Runtime: stub provider, in-memory session, history and cancel stores."""
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    stub = _RecordingStub()
    monkeypatch.setattr(runtime_server, "_stub_provider", stub)
    monkeypatch.setattr(runtime_server, "_session_store", InMemorySessionStore())
    monkeypatch.setattr(runtime_server, "_history_store", InMemoryConversationHistory())
    monkeypatch.setattr(runtime_server, "_cancellation_store", InMemoryCancellationStore())
    return stub


def _frames(client: TestClient, payload: dict[str, Any], session: str) -> list[dict[str, Any]]:
    """Post ``payload`` under ``session`` and return its decoded SSE frames."""
    retargeted = json.loads(json.dumps(payload).replace(_SESSION, session))
    response = client.post("/invocations", json=retargeted)
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def _seeded(
    client: TestClient, payloads: dict[str, dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Run the first send on a new session; return the session and its frames."""
    session = f"contract-{uuid.uuid4()}"
    first = _frames(client, payloads["first_send"], session)
    assert [frame["kind"] for frame in first][-1] == "completed"
    return session, first


def test_the_relay_forwards_resume_only_as_true_and_resume_from_unchanged(
    relay_payloads: dict[str, dict[str, Any]],
) -> None:
    resume, resume_from = runtime_wire_names(RUNTIME_SERVER.read_text(encoding="utf-8"))
    for name, fields in _BODIES.items():
        payload = relay_payloads[name]
        assert payload[resume_from] == fields["resume_from"], name
        expected = fields.get("resume") is True
        assert (payload.get(resume) is True) is expected, (name, payload)
        assert resume in payload or not expected, name


def test_a_zero_frame_reconnect_replays_the_whole_turn(
    relay_payloads: dict[str, dict[str, Any]], runtime: _RecordingStub
) -> None:
    with TestClient(runtime_server.app) as client:
        session, first = _seeded(client, relay_payloads)
        replay = _frames(client, relay_payloads["zero_frame_reconnect"], session)
    assert runtime.requests == 1
    assert replay == first


def test_a_mid_turn_reconnect_replays_only_what_follows_resume_from(
    relay_payloads: dict[str, dict[str, Any]], runtime: _RecordingStub
) -> None:
    with TestClient(runtime_server.app) as client:
        session, first = _seeded(client, relay_payloads)
        replay = _frames(client, relay_payloads["mid_turn_reconnect"], session)
    assert runtime.requests == 1
    assert replay == first[_MID_TURN_APPLIED:]


@pytest.mark.parametrize("case", ["follow_up", "string_true", "number_one"])
def test_a_body_without_a_true_resume_runs_a_fresh_turn(
    relay_payloads: dict[str, dict[str, Any]], runtime: _RecordingStub, case: str
) -> None:
    with TestClient(runtime_server.app) as client:
        session, _first = _seeded(client, relay_payloads)
        _frames(client, relay_payloads[case], session)
    assert runtime.requests == 2, case
