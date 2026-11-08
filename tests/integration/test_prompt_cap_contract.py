# SPDX-License-Identifier: MIT
"""The chat prompt cap is one number on every path a chat message takes.

Reads the cap from the source of each of its three owners: the relay (`src/relay/src/requests.ts`
`PROMPT_MAX_CHARS`), the Runtime (`src/agents/runtime_entrypoint.py` `PROMPT_MAX_CHARS`) and the
chat Gateway target (`src/gateway/chat_target.py` `MESSAGE_MAX_CHARS`). Each is read as text, so
a renamed or removed constant fails rather than skips.

Both enforcing sides then judge the same prompts, including astral characters: the real relay
`requests.ts`, bundled with the frontend's pinned esbuild and run under node, and the real
`decode_invocation` on the agent channel the relay forwards. Those cases need `node` and
`frontend/node_modules`; without them they skip, unless `CRM_REQUIRE_TS_CONTRACTS=1`, which turns
the skip into a failure.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from scripts import proc_run

from agents.runtime_entrypoint import decode_invocation
from crm.envelope import AGENT_IDENTITY_KEY, CLAIM_REP_ID, CLAIM_TENANT
from crm.errors import ValidationFailed
from providers.capabilities import load_capability_matrix

REPO_ROOT = Path(__file__).resolve().parents[2]

RELAY_SOURCE = REPO_ROOT / "src" / "relay" / "src" / "requests.ts"
RUNTIME_SOURCE = REPO_ROOT / "src" / "agents" / "runtime_entrypoint.py"
CHAT_TARGET_SOURCE = REPO_ROOT / "src" / "gateway" / "chat_target.py"

FRONTEND = REPO_ROOT / "frontend"
ESBUILD = FRONTEND / "node_modules" / ".bin" / "esbuild"
REQUIRE_ENV_VAR = "CRM_REQUIRE_TS_CONTRACTS"

_TS_CONSTANT = re.compile(r"^export const PROMPT_MAX_CHARS(?:: number)? = (\d[\d_]*);$", re.M)


def relay_cap(source: str) -> int:
    """Return the relay's `PROMPT_MAX_CHARS`, failing unless it is declared exactly once."""
    matches = _TS_CONSTANT.findall(source)
    assert len(matches) == 1, f"expected one PROMPT_MAX_CHARS declaration, found {len(matches)}"
    return int(matches[0].replace("_", ""))


def python_cap(source: str, name: str) -> int:
    """Return the integer literal module-level `name` is assigned, failing unless there is one."""
    values: list[int] = []
    for node in ast.parse(source).body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if isinstance(target, ast.Name) and target.id == name and isinstance(value, ast.Constant):
            assert isinstance(value.value, int) and not isinstance(value.value, bool)
            values.append(value.value)
    assert len(values) == 1, f"expected one integer assignment to {name}, found {len(values)}"
    return values[0]


def test_relay_runtime_and_chat_target_caps_are_equal() -> None:
    caps = {
        "relay": relay_cap(RELAY_SOURCE.read_text()),
        "runtime": python_cap(RUNTIME_SOURCE.read_text(), "PROMPT_MAX_CHARS"),
        "chat_target": python_cap(CHAT_TARGET_SOURCE.read_text(), "MESSAGE_MAX_CHARS"),
    }
    assert len(set(caps.values())) == 1, caps


def test_the_runtime_constant_read_from_source_is_the_one_it_enforces() -> None:
    from agents.runtime_entrypoint import PROMPT_MAX_CHARS
    from gateway.chat_target import MESSAGE_MAX_CHARS

    assert python_cap(RUNTIME_SOURCE.read_text(), "PROMPT_MAX_CHARS") == PROMPT_MAX_CHARS
    assert python_cap(CHAT_TARGET_SOURCE.read_text(), "MESSAGE_MAX_CHARS") == MESSAGE_MAX_CHARS


@pytest.mark.parametrize(
    "source",
    [
        "export const PROMPT_MAX_CHARS = 8000;\nexport const PROMPT_MAX_CHARS = 8000;\n",
        "const PROMPT_MAX_CHARS = 8000;\n",
        "",
    ],
)
def test_the_relay_reader_refuses_a_missing_or_duplicated_declaration(source: str) -> None:
    with pytest.raises(AssertionError):
        relay_cap(source)


def test_the_python_reader_refuses_a_non_literal_assignment() -> None:
    with pytest.raises(AssertionError):
        python_cap("PROMPT_MAX_CHARS = 4000 * 2\n", "PROMPT_MAX_CHARS")


#: Prompt -> whether a turn carrying it is accepted. Astral characters are one UTF-16 surrogate pair
#: each, so a cap counted in UTF-16 units would refuse the second case.
_PROMPTS: tuple[tuple[str, bool], ...] = (
    ("x" * 8000, True),
    ("\U0001f600" * 8000, True),
    ("x" * 8001, False),
    ("\U0001f600" * 8000 + "x", False),
    ("\U0001f600" * 4001, True),
)

_DRIVER = """
const mod = await import(process.argv[2]);
const prompts = JSON.parse(await new Promise((resolve) => {
  let text = "";
  process.stdin.on("data", (chunk) => (text += chunk));
  process.stdin.on("end", () => resolve(text));
}));
console.log(JSON.stringify(prompts.map((p) => mod.isPromptWithinCap(p))));
"""


@pytest.fixture(scope="module")
def relay_verdicts(tmp_path_factory: pytest.TempPathFactory) -> Iterator[list[bool]]:
    """The relay's accept/refuse verdict for every prompt in `_PROMPTS`, from the real TypeScript."""
    reason = None
    if shutil.which("node") is None:
        reason = "node is not on PATH"
    elif not ESBUILD.exists():
        reason = f"{ESBUILD} is absent (run `cd frontend && npm ci`)"
    if reason is not None:
        if os.environ.get(REQUIRE_ENV_VAR) == "1":
            pytest.fail(f"{REQUIRE_ENV_VAR}=1 and {reason}")
        pytest.skip(reason)
    work = tmp_path_factory.mktemp("prompt-cap")
    bundle = work / "requests.mjs"
    proc_run.run(
        [
            str(ESBUILD),
            str(RELAY_SOURCE),
            "--bundle",
            "--platform=node",
            "--format=esm",
            f"--outfile={bundle}",
            "--log-level=error",
        ],
        cwd=RELAY_SOURCE.parent,
        check=True,
    )
    driver = work / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    completed = proc_run.run(
        ["node", str(driver), str(bundle)],
        input=json.dumps([prompt for prompt, _ in _PROMPTS]),
        capture_output=True,
        text=True,
        check=True,
    )
    yield json.loads(completed.stdout)


def _runtime_accepts(prompt: str) -> bool:
    payload = {
        AGENT_IDENTITY_KEY: {
            "claims": {CLAIM_REP_ID: "rep-1", CLAIM_TENANT: "demo", "custom:book": "acc-1"},
            "session_id": "s" * 40,
        },
        "prompt": prompt,
    }
    try:
        decode_invocation(payload, load_capability_matrix())
    except ValidationFailed as exc:
        assert exc.source == "agent.invocation.prompt", exc
        return False
    return True


def test_the_runtime_judges_every_prompt_as_expected() -> None:
    assert [_runtime_accepts(prompt) for prompt, _ in _PROMPTS] == [ok for _, ok in _PROMPTS]


def test_the_relay_judges_every_prompt_as_the_runtime_does(relay_verdicts: list[bool]) -> None:
    assert relay_verdicts == [_runtime_accepts(prompt) for prompt, _ in _PROMPTS]
    assert relay_verdicts == [ok for _, ok in _PROMPTS]
