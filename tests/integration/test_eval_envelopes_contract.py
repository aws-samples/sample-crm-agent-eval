# SPDX-License-Identifier: MIT
"""The eval list, estimate and cancel envelopes are what the Evals page's parsers accept.

Producer: the real `crm.handlers.eval_trigger.handler` on the REST v1 proxy channel, against a
`moto` store and a recording Step Functions stand-in, driven through submit, list, cancel (all three
dispositions) and estimate. The proxy `body` string is exactly what the SPA's `fetch` reads.
Consumer: the real `HttpEvalRunClient` in `frontend/src/pages/evals/EvalRunClient.ts`, bundled with
the frontend's pinned esbuild and run under node with `fetch` answering each recorded body. Nothing
is hand-copied between the sides.

A run carrying `matrix` is produced by driving a one-task stub sweep through the real
`eval.sfn_handlers` steps, via `tests/unit/test_sfn_handlers.py`'s in-process `Sweep` driver, then
listed through the same handler. The SPA's `usageLowerBoundNote` and `erroredItemsCostNote` in
`frontend/src/pages/evals/types.ts` are run the same way and compared byte for byte with
`eval.matrix`'s notes. Needs `node` and `frontend/node_modules`; skips without them unless
`CRM_REQUIRE_TS_CONTRACTS=1`.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from scripts import proc_run

from crm.envelope import CLAIM_FULL_VISIBILITY, CLAIM_GROUPS, CLAIM_REP_ID
from crm.handlers import eval_trigger as handler_module
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import runs
from eval.matrix import ArmResult, errored_items_cost_note, usage_lower_bound_note
from eval.trigger import EVAL_STATE_MACHINE_ARN_ENV_VAR

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _REPO_ROOT / "frontend"
_ESBUILD = _FRONTEND / "node_modules" / ".bin" / "esbuild"
_CLIENT_TS = _FRONTEND / "src" / "pages" / "evals" / "EvalRunClient.ts"
_V1_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "events" / "apigw_v1_proxy_post_get_account.json"
_STATE_MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:eval_sweep"
_REQUIRE_ENV_VAR = "CRM_REQUIRE_TS_CONTRACTS"
_TYPES_TS = _FRONTEND / "src" / "pages" / "evals" / "types.ts"
_NOTE_COUNTS = (0, 1, 3)
_LOWER_BOUND_TASK_IDS = ("gt-01-investigation-x", "gt-04-enrichment-y", "gt-07-forecast-z")

#: Calls the client method `op` names with `fetch` answering `body`; prints one verdict per case.
_DRIVER = """
const { HttpEvalRunClient } = await import(process.argv[2]);
const cases = JSON.parse(await new Promise((resolve) => {
  let text = "";
  process.stdin.on("data", (chunk) => (text += chunk));
  process.stdin.on("end", () => resolve(text));
}));
const verdicts = [];
for (const { op, body } of cases) {
  let sent = null;
  globalThis.fetch = async (_url, init) => {
    sent = JSON.parse(init.body);
    return { ok: true, status: 200, json: async () => JSON.parse(body) };
  };
  const client = new HttpEvalRunClient("https://api.test", { getAccessToken: async () => "t" });
  try {
    const value =
      op === "list" ? await client.listRuns()
      : op === "estimate" ? await client.estimateLiveRun()
      : await client.cancelRun("run-from-test");
    verdicts.push({ ok: true, sent, value });
  } catch (error) {
    verdicts.push({ ok: false, sent, error: String(error?.message ?? error) });
  }
}
console.log(JSON.stringify(verdicts));
"""


class _Sfn:
    """Records `start_execution`; every execution describes as RUNNING."""

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "executionArn": f"{_STATE_MACHINE.replace('stateMachine', 'execution')}:{kwargs['name']}"
        }

    def describe_execution(self, *, executionArn: str) -> dict[str, Any]:  # noqa: N803
        return {"executionArn": executionArn, "status": "RUNNING"}


def _event(arguments: dict[str, Any]) -> dict[str, Any]:
    event: dict[str, Any] = copy.deepcopy(json.loads(_V1_FIXTURE.read_text(encoding="utf-8")))
    claims = event["requestContext"]["authorizer"]["claims"]
    claims[CLAIM_REP_ID] = "rep-admin"
    claims[CLAIM_FULL_VISIBILITY] = "true"
    claims[CLAIM_GROUPS] = "admin"
    event["path"] = "/eval_trigger"
    event["body"] = json.dumps(arguments)
    return event


def _body(store: Store, arguments: dict[str, Any]) -> str:
    response = handler_module.handler(_event(arguments), None, store=store, sfn=_Sfn())
    assert response["statusCode"] == 200, response
    body = response["body"]
    assert isinstance(body, str)
    return body


@pytest.fixture(scope="module")
def recorded() -> Iterator[list[dict[str, Any]]]:
    """The proxy bodies the real handler returns, one per `(op, label)`."""
    with mock_aws(), pytest.MonkeyPatch.context() as patch:
        patch.setenv(EVAL_STATE_MACHINE_ARN_ENV_VAR, _STATE_MACHINE)
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        store = Store(client=client)
        cases: list[dict[str, Any]] = [
            {"op": "list", "label": "empty", "body": _body(store, {"action": "list"})}
        ]
        first = json.loads(_body(store, {"action": "submit"}))["run_id"]
        cases.append(
            {"op": "list", "label": "one_queued", "body": _body(store, {"action": "list"})}
        )
        for label in ("accepted", "already_requested"):
            cases.append(
                {
                    "op": "cancel",
                    "label": label,
                    "body": _body(store, {"action": "cancel", "run_id": first}),
                }
            )
        runs.finalize(store, run_id=first, teardown_ok=True)
        cases.append(
            {
                "op": "cancel",
                "label": "already_terminal",
                "body": _body(store, {"action": "cancel", "run_id": first}),
            }
        )
        _body(store, {"action": "submit"})
        cases.append({"op": "list", "label": "two_runs", "body": _body(store, {"action": "list"})})
        cases.append(
            {"op": "estimate", "label": "estimate", "body": _body(store, {"action": "estimate"})}
        )
    yield cases


@pytest.fixture(scope="module")
def matrix_case() -> Iterator[dict[str, Any]]:
    """A `list` body whose one run is a completed sweep the aggregate step gave a `matrix`."""
    from tests.unit.test_sfn_handlers import make_sweep, mocked_aws

    with pytest.MonkeyPatch.context() as patch, mocked_aws(patch):
        sweep = make_sweep(patch, tasks=1)
        assert sweep.drive()["status"] == "completed"
        case = {
            "op": "list",
            "label": "completed_with_matrix",
            "body": _body(sweep.store, {"action": "list"}),
        }
    yield case


def _unavailable_reason() -> str | None:
    if shutil.which("node") is None:
        return "node is not on PATH"
    if not _ESBUILD.exists():
        return f"{_ESBUILD} is absent (run `cd frontend && npm ci`)"
    return None


def _bundle(source: Path, bundle: Path) -> None:
    proc_run.run(
        [
            str(_ESBUILD),
            str(source),
            "--bundle",
            "--platform=node",
            "--format=esm",
            f"--outfile={bundle}",
            "--log-level=error",
        ],
        cwd=_FRONTEND,
        check=True,
    )


def _parse_in_spa(work: Path, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bundle = work / "EvalRunClient.mjs"
    _bundle(_CLIENT_TS, bundle)
    driver = work / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    completed = proc_run.run(
        ["node", str(driver), str(bundle)],
        input=json.dumps([{"op": c["op"], "body": c["body"]} for c in cases]),
        capture_output=True,
        text=True,
        check=True,
    )
    verdicts: list[dict[str, Any]] = json.loads(completed.stdout)
    return verdicts


#: Prints the SPA's two cost notes for each case; an absent note prints as `null`.
_NOTES_DRIVER = """
const { usageLowerBoundNote, erroredItemsCostNote } = await import(process.argv[2]);
const cases = JSON.parse(process.argv[3]);
console.log(JSON.stringify({
  usage: cases.arms.map((arm) => usageLowerBoundNote(arm) ?? null),
  errored: cases.errored.map((count) => erroredItemsCostNote(count) ?? null),
}));
"""


def _cost_notes_in_spa(
    work: Path, arms: list[dict[str, object]], errored: list[int]
) -> dict[str, list[str | None]]:
    bundle = work / "types.mjs"
    _bundle(_TYPES_TS, bundle)
    driver = work / "notes_driver.mjs"
    driver.write_text(_NOTES_DRIVER, encoding="utf-8")
    completed = proc_run.run(
        ["node", str(driver), str(bundle), json.dumps({"arms": arms, "errored": errored})],
        capture_output=True,
        text=True,
        check=True,
    )
    notes: dict[str, list[str | None]] = json.loads(completed.stdout)
    return notes


def _lower_bound_arms() -> list[ArmResult]:
    from tests.unit.test_matrix import _sample_arm

    return [
        dataclasses.replace(_sample_arm(), usage_lower_bound_task_ids=_LOWER_BOUND_TASK_IDS[:n])
        for n in _NOTE_COUNTS
    ]


@pytest.fixture(scope="module")
def spa(
    tmp_path_factory: pytest.TempPathFactory, recorded: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    reason = _unavailable_reason()
    if reason is not None:
        if os.environ.get(_REQUIRE_ENV_VAR) == "1":
            pytest.fail(f"{_REQUIRE_ENV_VAR}=1 and {reason}")
        pytest.skip(reason)
    return _parse_in_spa(tmp_path_factory.mktemp("eval-envelopes"), recorded)


_LABELS = [
    "empty",
    "one_queued",
    "accepted",
    "already_requested",
    "already_terminal",
    "two_runs",
    "estimate",
]


def test_the_handler_produced_every_case(recorded: list[dict[str, Any]]) -> None:
    """The cases below exist; a producer change that drops one fails here, not silently."""
    assert [case["label"] for case in recorded] == _LABELS


@pytest.mark.parametrize("label", _LABELS)
def test_the_spa_parses_what_the_handler_returns(
    label: str, recorded: list[dict[str, Any]], spa: list[dict[str, Any]]
) -> None:
    index = [case["label"] for case in recorded].index(label)
    verdict = spa[index]
    assert verdict["ok"], f"{label}: the SPA refused the handler's body: {verdict['error']}"
    produced = json.loads(recorded[index]["body"])
    if recorded[index]["op"] == "list":
        assert [run["run_id"] for run in verdict["value"]["runs"]] == [
            run["run_id"] for run in produced["runs"]
        ]
    elif recorded[index]["op"] == "cancel":
        assert verdict["value"]["disposition"] == label == produced["disposition"]
        assert verdict["value"]["run"]["run_id"] == produced["run"]["run_id"]
    else:
        assert verdict["value"]["estimate_usd"] == produced["estimate_usd"]


def test_the_list_cases_cover_the_statuses_the_page_branches_on(
    recorded: list[dict[str, Any]],
) -> None:
    statuses = {
        run["status"]
        for case in recorded
        if case["op"] == "list"
        for run in json.loads(case["body"])["runs"]
    }
    assert {"queued", "completed"} <= statuses


def test_the_spa_actions_are_ones_the_handler_recognises(spa: list[dict[str, Any]]) -> None:
    """The request side of the seam: every action the client sends is a handler action."""
    sent_actions = {verdict["sent"]["action"] for verdict in spa}
    assert sent_actions == {
        handler_module.ACTION_LIST,
        handler_module.ACTION_ESTIMATE,
        handler_module.ACTION_CANCEL,
    }
    for verdict in spa:
        assert set(verdict["sent"]) <= handler_module._RECOGNISED_ARGUMENTS


def test_a_body_missing_a_field_the_page_needs_is_refused_control(
    tmp_path: Path, recorded: list[dict[str, Any]]
) -> None:
    """Control: the SPA parsers are strict, so the agreement above is not vacuous."""
    if _unavailable_reason() is not None:
        pytest.skip(str(_unavailable_reason()))
    broken = []
    for case in recorded:
        body = json.loads(case["body"])
        if case["op"] == "estimate":
            body.pop("estimate_usd")
        elif case["op"] == "cancel":
            body["run"].pop("is_terminal")
        else:
            body["runs"] = [{**run, "status": "unknown"} for run in body["runs"]] or [{}]
        broken.append({**case, "body": json.dumps(body)})

    verdicts = _parse_in_spa(tmp_path, broken)

    assert [verdict["ok"] for verdict in verdicts] == [False] * len(broken)


def test_a_run_with_the_aggregate_steps_matrix_parses_in_the_spa(
    tmp_path: Path, matrix_case: dict[str, Any]
) -> None:
    """The `matrix` the aggregate step writes passes `isRenderableMatrix`."""
    reason = _unavailable_reason()
    if reason is not None:
        if os.environ.get(_REQUIRE_ENV_VAR) == "1":
            pytest.fail(f"{_REQUIRE_ENV_VAR}=1 and {reason}")
        pytest.skip(reason)
    (run,) = json.loads(matrix_case["body"])["runs"]
    assert run["status"] == "completed" and len(run["matrix"]["arms"]) == 2

    (verdict,) = _parse_in_spa(tmp_path, [matrix_case])

    assert verdict["ok"], verdict.get("error")
    assert verdict["value"]["runs"][0]["matrix"] == run["matrix"]

    del run["matrix"]["arms"][0]["task_scores"]
    broken = {**matrix_case, "body": json.dumps({"runs": [run], "next_cursor": None})}
    (refused,) = _parse_in_spa(tmp_path, [broken])
    assert not refused["ok"], "control: an arm without task_scores must be refused"


@pytest.fixture(scope="module")
def spa_cost_notes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str | None]]:
    """The SPA's notes for each lower-bound arm's rendered fields and each errored-item count."""
    reason = _unavailable_reason()
    if reason is not None:
        if os.environ.get(_REQUIRE_ENV_VAR) == "1":
            pytest.fail(f"{_REQUIRE_ENV_VAR}=1 and {reason}")
        pytest.skip(reason)
    return _cost_notes_in_spa(
        tmp_path_factory.mktemp("cost-notes"),
        [arm.to_report_fields() for arm in _lower_bound_arms()],
        list(_NOTE_COUNTS),
    )


@pytest.mark.parametrize("index", range(len(_NOTE_COUNTS)), ids=[f"n{n}" for n in _NOTE_COUNTS])
def test_the_spa_usage_lower_bound_note_is_the_matrix_note_byte_for_byte(
    index: int, spa_cost_notes: dict[str, list[str | None]]
) -> None:
    """The SPA reads the arm's rendered `matrix.json` fields; the backend reads the arm."""
    expected = usage_lower_bound_note(_lower_bound_arms()[index])
    assert (expected is None) == (_NOTE_COUNTS[index] == 0)
    assert spa_cost_notes["usage"][index] == expected


@pytest.mark.parametrize("index", range(len(_NOTE_COUNTS)), ids=[f"n{n}" for n in _NOTE_COUNTS])
def test_the_spa_errored_items_cost_note_is_the_matrix_note_byte_for_byte(
    index: int, spa_cost_notes: dict[str, list[str | None]]
) -> None:
    expected = errored_items_cost_note(_NOTE_COUNTS[index])
    assert (expected is None) == (_NOTE_COUNTS[index] == 0)
    assert spa_cost_notes["errored"][index] == expected
