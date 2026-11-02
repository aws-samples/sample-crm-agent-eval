# SPDX-License-Identifier: MIT
"""Explicit invariant guards and absolute-path ``git`` resolution in ``src/``.

Covers the ``RuntimeError`` guards in ``agents.supervisor.synthesize_reply`` and
``crm.proposals.approve_proposal``, the ``eval.harness`` raw-records writer factory, and the
``shutil.which`` resolution of ``git`` in ``eval.harness._git_revision`` and
``eval.verification._run_git``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from agents.supervisor import SubagentOutcome, synthesize_reply
from crm import proposals
from crm.proposals import approve_proposal, submit_proposal
from crm.store.keys import derive
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness, verification

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_NOW = "2025-01-01T00:00:00Z"
_SHA = re.compile(r"[0-9a-f]{40}")


def _outcome_bypassing_post_init(**fields: object) -> SubagentOutcome:
    """Build a ``SubagentOutcome`` whose fields never passed ``__post_init__``."""
    outcome = object.__new__(SubagentOutcome)
    defaults: dict[str, object] = {
        "kind": "data",
        "data": None,
        "missing_scope": None,
        "denial_message": None,
        "error_message": None,
    }
    defaults.update(fields)
    for name, value in defaults.items():
        object.__setattr__(outcome, name, value)
    return outcome


# -- agents.supervisor.synthesize_reply ---------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "denied", "missing_scope": "opportunities:write"},
        {"kind": "denied", "denial_message": "Not allowed."},
        {"kind": "tool_failure"},
    ],
)
def test_synthesize_reply_raises_runtime_error_when_required_field_is_missing(
    fields: dict[str, object],
) -> None:
    with pytest.raises(RuntimeError, match="reached synthesis without"):
        synthesize_reply(_outcome_bypassing_post_init(**fields))


def test_synthesize_reply_renders_well_formed_denied_and_tool_failure_outcomes() -> None:
    denied = SubagentOutcome(
        kind="denied", missing_scope="opportunities:write", denial_message="Not allowed."
    )
    failed = SubagentOutcome(kind="tool_failure", error_message="timed out")

    assert synthesize_reply(denied) == "Not allowed. (missing scope: opportunities:write)"
    assert synthesize_reply(failed) == "That request could not be completed: timed out"


# -- crm.proposals.approve_proposal -------------------------------------------------------------


@pytest.fixture
def store() -> Iterator[Store]:
    """A Store over a moto-mocked DynamoDB with every declared table created."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _seed_update_proposal(store: Store) -> dict[str, object]:
    item = derive(
        "opportunities",
        {
            "opportunity_id": "opp-1",
            "tenant": _TENANT,
            "account_id": "acct-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "owner_rep_id": _REP,
            "value": 1000,
            "close_date": "2025-06-01",
            "primary_contact_id": None,
            "created_at": _NOW,
            "updated_at": _NOW,
            "created_by": _REP,
            "schema_version": 1,
        },
    )
    store.put_item("opportunities", item)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )
    return item


def test_approve_proposal_raises_runtime_error_and_writes_nothing_when_update_has_no_snapshot(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _seed_update_proposal(store)
    monkeypatch.setattr(proposals, "_apply_operation", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="produced no after-snapshot"):
        approve_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            actor_rep_id=_REP,
            transaction_key="txn-approve",
            now="2025-01-02T00:00:00Z",
        )

    assert store.get_item("opportunities", {"opportunity_id": "opp-1"}) == original
    stored = store.get_item("proposals", {"proposal_id": "prop-1"})
    assert stored is not None
    assert stored["status"] == "pending"


# -- eval.harness raw-records writer ------------------------------------------------------------


def test_raw_records_writer_writes_sanitised_file_name_into_its_directory(tmp_path: Path) -> None:
    writer = harness._raw_records_writer(tmp_path)

    writer(2, "us.anthropic/claude:v1", {"b": 1, "a": 2})

    written = tmp_path / "pre-aggregation-arm-2-us.anthropic_claude_v1.json"
    assert written.read_text(encoding="utf-8") == '{\n  "a": 2,\n  "b": 1\n}\n'
    assert json.loads(written.read_text(encoding="utf-8")) == {"a": 2, "b": 1}


# -- absolute-path git resolution ---------------------------------------------------------------


def _forbid_subprocess(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("subprocess.run must not be called when git is absent from PATH")


def test_git_revision_returns_unknown_without_spawning_when_git_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)

    assert harness._git_revision() == "unknown"


def test_run_git_returns_none_without_spawning_when_git_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)

    assert verification._run_git("rev-parse", "HEAD") is None
    assert verification.git_commit() == verification.UNKNOWN
    assert verification.git_tree_dirty() is None


@pytest.mark.parametrize(
    "call",
    [harness._git_revision, lambda: verification._run_git("rev-parse", "HEAD")],
    ids=["harness", "verification"],
)
def test_git_is_spawned_by_absolute_path(call: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH on this host")
    executables: list[str] = []

    class RecordingPopen(subprocess.Popen[Any]):
        def __init__(self, args: list[str], *rest: Any, **kwargs: Any) -> None:
            executables.append(args[0])
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)

    revision = call()

    assert len(executables) == 1
    assert Path(executables[0]).is_absolute()
    assert Path(executables[0]).name == "git"
    if (Path(__file__).resolve().parents[2] / ".git").exists():
        assert revision is not None
        assert _SHA.fullmatch(revision)
