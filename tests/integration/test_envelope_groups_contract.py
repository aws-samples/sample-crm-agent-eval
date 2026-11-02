# SPDX-License-Identifier: MIT
"""The `cognito:groups` claim means the same thing to the server and to the SPA.

Server side: the recorded REST v1 authorizer event (`tests/fixtures/events/`), with each claim
rendering written into its `requestContext.authorizer.claims`, decoded by the real
`crm.envelope.decode_identity`. SPA side: the real `frontend/src/auth/adminGroup.ts`, bundled with
the frontend's pinned esbuild and run under node on the same raw values. Both must agree on "admin
or not" for every rendering, and both must match the expected table, so an identical mistake on both
sides still fails. The SPA side never grants anything; the server decides.

Needs `node` and `frontend/node_modules`. Without them the module skips, unless
`CRM_REQUIRE_TS_CONTRACTS=1`, which turns the skip into a failure.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from scripts import proc_run

from crm.authz import ADMIN_GROUP
from crm.envelope import CLAIM_GROUPS, decode_identity
from crm.errors import AuthenticationFailed

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _REPO_ROOT / "frontend"
_ESBUILD = _FRONTEND / "node_modules" / ".bin" / "esbuild"
_ADMIN_GROUP_TS = _FRONTEND / "src" / "auth" / "adminGroup.ts"
_V1_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "events" / "apigw_v1_proxy_post_get_account.json"

_REQUIRE_ENV_VAR = "CRM_REQUIRE_TS_CONTRACTS"

#: Raw claim value -> whether its holder is an admin. Absent is `None`; malformed renderings are
#: refused by the server and "not admin" in the SPA, which agree on the answer that matters.
_RENDERINGS: tuple[tuple[object, bool], ...] = (
    (None, False),
    (["admin"], True),
    (["reps", "admin"], True),
    (["reps"], False),
    ('["admin"]', True),
    ('["reps","admin"]', True),
    ("[admin]", True),
    ("[reps, admin]", True),
    ("admin", True),
    ("reps,admin", True),
    (" admin ", True),
    ("reps", False),
    ("", False),
    ("[admin reps]", False),
    ("Admin", False),
    ("administrators", False),
    (7, False),
    ([1], False),
    ('{"admin": true}', False),
)

_DRIVER = """
const mod = await import(process.argv[2]);
const cases = JSON.parse(await new Promise((resolve) => {
  let text = "";
  process.stdin.on("data", (chunk) => (text += chunk));
  process.stdin.on("end", () => resolve(text));
}));
const answers = cases.map((raw) => mod.parseGroupsClaim(raw === null ? undefined : raw).has(mod.ADMIN_GROUP));
console.log(JSON.stringify({ADMIN_GROUP: mod.ADMIN_GROUP, GROUPS_CLAIM: mod.GROUPS_CLAIM, answers}));
"""


def _unavailable_reason() -> str | None:
    if shutil.which("node") is None:
        return "node is not on PATH"
    if not _ESBUILD.exists():
        return f"{_ESBUILD} is absent (run `cd frontend && npm ci`)"
    return None


@pytest.fixture(scope="module")
def spa(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """The SPA module's verdict for every rendering, from the real TypeScript under node."""
    reason = _unavailable_reason()
    if reason is not None:
        if os.environ.get(_REQUIRE_ENV_VAR) == "1":
            pytest.fail(f"{_REQUIRE_ENV_VAR}=1 and {reason}")
        pytest.skip(reason)
    work = tmp_path_factory.mktemp("groups")
    bundle = work / "adminGroup.mjs"
    proc_run.run(
        [
            str(_ESBUILD),
            str(_ADMIN_GROUP_TS),
            "--bundle",
            "--platform=node",
            "--format=esm",
            f"--outfile={bundle}",
            "--log-level=error",
        ],
        cwd=_FRONTEND,
        check=True,
    )
    driver = work / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    completed = proc_run.run(
        ["node", str(driver), str(bundle)],
        input=json.dumps([raw for raw, _ in _RENDERINGS]),
        capture_output=True,
        text=True,
        check=True,
    )
    yield json.loads(completed.stdout)


def _server_is_admin(raw: object) -> bool:
    event = json.loads(_V1_FIXTURE.read_text(encoding="utf-8"))
    claims = event["requestContext"]["authorizer"]["claims"]
    claims.pop(CLAIM_GROUPS, None)
    if raw is not None:
        claims[CLAIM_GROUPS] = copy.deepcopy(raw)
    try:
        return decode_identity(event).is_admin
    except AuthenticationFailed:
        return False


def test_both_sides_name_the_same_claim_and_group(spa: dict[str, Any]) -> None:
    assert spa["GROUPS_CLAIM"] == CLAIM_GROUPS
    assert spa["ADMIN_GROUP"] == ADMIN_GROUP


def test_the_recorded_event_without_groups_is_a_non_admin_user_control() -> None:
    """Control: the fixture decodes at all, so a False below is the claim's doing."""
    event = json.loads(_V1_FIXTURE.read_text(encoding="utf-8"))
    identity = decode_identity(event)
    assert identity.source == "apigw"
    assert identity.principal_type == "user"
    assert identity.is_admin is False


@pytest.mark.parametrize(
    ("index", "raw", "expected"),
    [(i, raw, expected) for i, (raw, expected) in enumerate(_RENDERINGS)],
    ids=[repr(raw) for raw, _ in _RENDERINGS],
)
def test_server_and_spa_agree_with_the_expected_verdict(
    spa: dict[str, Any], index: int, raw: object, expected: bool
) -> None:
    assert _server_is_admin(raw) is expected
    assert spa["answers"][index] is expected
