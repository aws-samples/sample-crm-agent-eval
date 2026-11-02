# SPDX-License-Identifier: MIT
"""Tests for `scripts/classify_post_apply_drift.py`, the post-apply drift classifier.

Each allowance is tested both ways: the churn it covers (from `scripts/package_lambdas.sh`,
`scripts/package_relay.sh` and known re-renders) is absorbed, and any other pending change still
fails `scripts/deploy.sh`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "classify_post_apply_drift.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("classify_post_apply_drift", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


classifier = _load()


def _planned_change(
    address: str,
    resource_type: str,
    action: str,
    *,
    resource_name: str | None = None,
    include_name: bool = True,
    reason: str | None = None,
) -> str:
    """One `planned_change` line shaped exactly as `terraform plan -json` emits it."""
    if resource_name is None:
        resource_name = address.rsplit(".", 1)[-1].split("[", 1)[0]
    resource: dict[str, object] = {
        "addr": address,
        "module": address.rsplit(f"{resource_type}.", 1)[0].rstrip("."),
        "implied_provider": "aws",
        "resource_type": resource_type,
        "resource_key": None,
    }
    if include_name:
        resource["resource_name"] = resource_name
    change: dict[str, object] = {"resource": resource, "action": action}
    if reason is not None:
        change["reason"] = reason
    return json.dumps(
        {
            "@level": "info",
            "@message": f"{address}: Plan to {action}",
            "type": "planned_change",
            "change": change,
        }
    )


def _stream(*lines: str) -> str:
    return "\n".join(lines) + "\n"


#: A representative full-`package_lambdas.sh` cascade: Lambda functions whose
#: `source_code_hash` moved, the two `agent-source.zip` hash consumers, the build trigger, the
#: Runtime whose `container_uri` embeds the derived image tag, and the CodeBuild IAM data reads.
LAMBDA_CHURN = [
    _planned_change(f'aws_lambda_function.this["tool_{i}"]', "aws_lambda_function", "update")
    for i in range(24)
]
AGENT_SOURCE_CASCADE = [
    _planned_change("aws_s3_object.agent_source", "aws_s3_object", "update"),
    _planned_change(
        "module.agentcore.aws_codebuild_project.agent_image", "aws_codebuild_project", "update"
    ),
    _planned_change("module.agentcore.null_resource.image_build", "null_resource", "replace"),
    _planned_change(
        "module.agentcore.aws_bedrockagentcore_agent_runtime.this",
        "aws_bedrockagentcore_agent_runtime",
        "update",
    ),
]
IAM_DATA_READS = [
    _planned_change(
        "module.agentcore.data.aws_iam_policy_document.codebuild_assume_role",
        "aws_iam_policy_document",
        "read",
    ),
    _planned_change(
        "module.agentcore.data.aws_iam_policy_document.codebuild_permissions",
        "aws_iam_policy_document",
        "read",
    ),
]


# ---------------------------------------------------------------------------
# A clean plan, and the degenerate inputs.
# ---------------------------------------------------------------------------


def test_an_empty_plan_stream_reports_nothing_pending() -> None:
    assert classifier.classify("") == ([], [])


def test_non_planned_change_messages_and_malformed_lines_are_ignored() -> None:
    stream = _stream(
        json.dumps({"type": "version", "@level": "info"}),
        "not json at all",
        "",
        json.dumps({"type": "apply_complete", "change": {"action": "update"}}),
    )
    assert classifier.classify(stream) == ([], [])


@pytest.mark.parametrize("action", ["noop", "read", ""])
def test_a_non_change_action_is_not_a_pending_change(action: str) -> None:
    stream = _stream(_planned_change("aws_dynamodb_table.reps", "aws_dynamodb_table", action))
    assert classifier.classify(stream) == ([], [])


# ---------------------------------------------------------------------------
# The packaging churn the allow-list absorbs.
# ---------------------------------------------------------------------------


def test_lambda_source_code_hash_churn_alone_is_absorbed() -> None:
    changed, unexpected = classifier.classify(_stream(*LAMBDA_CHURN))
    assert len(changed) == len(LAMBDA_CHURN)
    assert unexpected == []


def test_the_full_observed_cascade_is_absorbed_and_the_data_reads_are_not_counted() -> None:
    """The full cascade is absorbed and the IAM data reads are not counted as pending."""
    changed, unexpected = classifier.classify(
        _stream(*LAMBDA_CHURN, *AGENT_SOURCE_CASCADE, *IAM_DATA_READS)
    )
    assert unexpected == [], unexpected
    assert len(changed) == len(LAMBDA_CHURN) + len(AGENT_SOURCE_CASCADE), changed
    assert not any("aws_iam_policy_document" in address for address in changed)


def test_resource_name_is_recovered_from_the_address_when_terraform_omits_it() -> None:
    """Name-scoped entries still match when `resource_name` is absent from the JSON."""
    stream = _stream(
        _planned_change(
            "aws_s3_object.agent_source", "aws_s3_object", "update", include_name=False
        ),
        _planned_change(
            "module.agentcore.aws_codebuild_project.agent_image",
            "aws_codebuild_project",
            "update",
            include_name=False,
        ),
    )
    changed, unexpected = classifier.classify(stream)
    assert len(changed) == 2
    assert unexpected == []


# ---------------------------------------------------------------------------
# Genuine drift still fails the deploy.
# ---------------------------------------------------------------------------


def test_a_genuine_infrastructure_change_still_fails() -> None:
    """A DynamoDB table and an IAM policy change are not absorbed."""
    stream = _stream(
        *LAMBDA_CHURN,
        _planned_change("aws_dynamodb_table.contacts", "aws_dynamodb_table", "update"),
        _planned_change("aws_iam_role_policy.actions", "aws_iam_role_policy", "update"),
    )
    changed, unexpected = classifier.classify(stream)
    assert len(changed) == 26
    assert len(unexpected) == 2, unexpected
    assert any("aws_dynamodb_table.contacts" in u for u in unexpected)
    assert any("aws_iam_role_policy.actions" in u for u in unexpected)


def test_an_agentcore_runtime_change_without_the_packaging_cascade_fails() -> None:
    """A Runtime `container_uri` change without the agent-source cascade fails."""
    stream = _stream(
        *LAMBDA_CHURN,
        _planned_change(
            "module.agentcore.aws_bedrockagentcore_agent_runtime.this",
            "aws_bedrockagentcore_agent_runtime",
            "update",
        ),
    )
    changed, unexpected = classifier.classify(stream)
    assert len(unexpected) == 1, unexpected
    assert "aws_bedrockagentcore_agent_runtime.this" in unexpected[0]
    assert len(changed) == 25


@pytest.mark.parametrize("action", ["create", "delete", "replace"])
def test_a_runtime_action_other_than_update_fails_even_with_the_cascade_present(
    action: str,
) -> None:
    """Replacing or destroying the Runtime is never churn, whatever else is pending."""
    stream = _stream(
        _planned_change("aws_s3_object.agent_source", "aws_s3_object", "update"),
        _planned_change(
            "module.agentcore.aws_bedrockagentcore_agent_runtime.this",
            "aws_bedrockagentcore_agent_runtime",
            action,
        ),
    )
    _, unexpected = classifier.classify(stream)
    assert len(unexpected) == 1, unexpected
    assert action in unexpected[0]


@pytest.mark.parametrize("action", ["create", "delete"])
def test_creating_or_destroying_a_lambda_function_is_not_packaging_churn(action: str) -> None:
    """A Lambda create, delete or replace is not absorbed as packaging churn."""
    stream = _stream(
        _planned_change('aws_lambda_function.this["get_account"]', "aws_lambda_function", action)
    )
    _, unexpected = classifier.classify(stream)
    assert len(unexpected) == 1, unexpected
    assert action in unexpected[0]


def test_an_s3_object_other_than_agent_source_is_not_absorbed() -> None:
    """Only the `agent_source` S3 object is absorbed by the `(type, name)`-keyed cascade."""
    stream = _stream(_planned_change("aws_s3_object.frontend_index", "aws_s3_object", "update"))
    _, unexpected = classifier.classify(stream)
    assert len(unexpected) == 1, unexpected
    assert "frontend_index" in unexpected[0]


def test_a_second_codebuild_project_is_not_absorbed() -> None:
    stream = _stream(
        _planned_change(
            "module.other.aws_codebuild_project.something_else", "aws_codebuild_project", "update"
        )
    )
    _, unexpected = classifier.classify(stream)
    assert len(unexpected) == 1, unexpected


def test_the_allow_lists_are_narrow_enough_to_be_falsifiable() -> None:
    """Packaging types never absorb create/delete, and the keyed tables absorb only `update`."""
    assert set(classifier.PACKAGING_ATTRIBUTABLE_TYPES) == {
        "aws_lambda_function",
        "terraform_data",
        "null_resource",
    }
    for actions in classifier.PACKAGING_ATTRIBUTABLE_TYPES.values():
        assert actions and "create" not in actions and "delete" not in actions
    for table in (classifier.AGENT_SOURCE_CASCADE, classifier.CONDITIONAL_ON_AGENT_SOURCE):
        for (resource_type, name), actions in table.items():
            assert resource_type and name and actions == frozenset({"update"})
    assert set(classifier.SELF_NORMALISING_POLICY_RESOURCES) == {
        "module.api.aws_api_gateway_rest_api.this"
    }
    for address, entry in classifier.SELF_NORMALISING_POLICY_RESOURCES.items():
        assert address.startswith("module.") and entry.policy_attribute
        assert entry.actions == frozenset({"update"})


# ---------------------------------------------------------------------------
# The CLI process boundary that `scripts/deploy.sh` invokes.
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_the_process_exits_zero_and_summarises_when_all_churn_is_absorbed(tmp_path: Path) -> None:
    plan = tmp_path / "plan.ndjson"
    plan.write_text(_stream(*LAMBDA_CHURN, *AGENT_SOURCE_CASCADE, *IAM_DATA_READS), "utf-8")
    result = _run(str(plan))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pending_changes=28 unexpected=0" in result.stdout


def test_the_process_exits_one_and_names_the_offender_on_genuine_drift(tmp_path: Path) -> None:
    plan = tmp_path / "plan.ndjson"
    plan.write_text(
        _stream(_planned_change("aws_cognito_user_pool.this", "aws_cognito_user_pool", "update")),
        "utf-8",
    )
    result = _run(str(plan))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "unexpected=1" in result.stdout
    assert "aws_cognito_user_pool.this" in result.stderr
    assert "absorbed by NO allowance" in result.stderr
    for allowance in classifier.ALLOWANCES:
        assert allowance in result.stderr, allowance
    assert "OUTSIDE the Lambda-packaging allow-list" not in result.stderr


def test_an_unreadable_plan_and_a_bad_invocation_both_exit_two(tmp_path: Path) -> None:
    assert _run(str(tmp_path / "absent.ndjson")).returncode == 2
    assert _run().returncode == 2


# ---------------------------------------------------------------------------
# The deferred policy-document re-render.
# ---------------------------------------------------------------------------

#: A tracked, redacted, reduced slice of a real post-apply `terraform plan -json` stream; its
#: `_fixture_provenance` line records what was reduced and redacted.
RECORDED_POST_APPLY_PLAN = (
    REPO_ROOT / "tests" / "fixtures" / "plans" / "post_apply_drift_stream.ndjson"
)


def _recorded_plan_text() -> str:
    """The tracked slice's text; fails (does not skip) when the fixture is missing."""
    assert RECORDED_POST_APPLY_PLAN.is_file(), (
        f"the tracked recorded-plan fixture is missing at {RECORDED_POST_APPLY_PLAN}; it is "
        "committed evidence for the deferred-policy-document IAM rule and must not be deleted"
    )
    return RECORDED_POST_APPLY_PLAN.read_text(encoding="utf-8")


ROLE_ADDRESS = "module.agentcore.aws_iam_role.codebuild"
POLICY_ADDRESS = "module.agentcore.aws_iam_role_policy.codebuild"

DEFERRED_IAM_DATA_READS = [
    _planned_change(
        "module.agentcore.data.aws_iam_policy_document.codebuild_assume_role",
        "aws_iam_policy_document",
        "read",
        reason=classifier.DEFERRED_READ_REASON,
    ),
    _planned_change(
        "module.agentcore.data.aws_iam_policy_document.codebuild_permissions",
        "aws_iam_policy_document",
        "read",
        reason=classifier.DEFERRED_READ_REASON,
    ),
]

DEFERRED_IAM_CONSUMERS = [
    _planned_change(ROLE_ADDRESS, "aws_iam_role", "update"),
    _planned_change(POLICY_ADDRESS, "aws_iam_role_policy", "update"),
]

ASSUME_ROLE_JSON = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "codebuild.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)
PERMISSIONS_JSON = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["logs:PutLogEvents"], "Resource": "*"}],
    }
)


def _show_json(*entries: tuple[str, dict[str, object]]) -> str:
    """A `terraform show -json <planfile>` document carrying only the given resource changes."""
    return json.dumps(
        {
            "format_version": "1.2",
            "terraform_version": "1.5.7",
            "resource_changes": [
                {"address": address, "change": change} for address, change in entries
            ],
        }
    )


def _deferred_rerender_show(
    *, role_extra: dict[str, object] | None = None, policy_after: str | None = None
) -> str:
    """A `show -json` document where both policy documents are deferred and render unknown.

    `role_extra` adds attributes to the role's before/after pair; `policy_after` makes the inline
    policy's `after` a known value.
    """
    role_before: dict[str, object] = {
        "name": "crm-agent-eval-codebuild-role",
        "assume_role_policy": ASSUME_ROLE_JSON,
        "max_session_duration": 3600,
        "tags": {"environment": "dev"},
    }
    role_after: dict[str, object] = dict(role_before, assume_role_policy=None)
    if role_extra:
        role_before.update({k: v[0] for k, v in _pairs(role_extra).items()})
        role_after.update({k: v[1] for k, v in _pairs(role_extra).items()})
    policy_before = {"name": "crm-agent-eval-codebuild", "policy": PERMISSIONS_JSON}
    return _show_json(
        (
            ROLE_ADDRESS,
            {
                "actions": ["update"],
                "before": role_before,
                "after": role_after,
                "after_unknown": {"assume_role_policy": True},
            },
        ),
        (
            POLICY_ADDRESS,
            {
                "actions": ["update"],
                "before": policy_before,
                "after": {"name": "crm-agent-eval-codebuild", "policy": policy_after},
                "after_unknown": {} if policy_after is not None else {"policy": True},
            },
        ),
    )


def _pairs(extra: dict[str, object]) -> dict[str, tuple[object, object]]:
    """Normalise `{"attr": (before, after)}` entries."""
    normalised: dict[str, tuple[object, object]] = {}
    for key, value in extra.items():
        assert isinstance(value, tuple) and len(value) == 2
        normalised[key] = (value[0], value[1])
    return normalised


def test_the_recorded_real_post_apply_plan_reproduces_the_two_unexpected_iam_changes() -> None:
    """In the recorded plan both documents are deferred and, without detail, both consumers fail."""
    text = _recorded_plan_text()
    assert classifier.deferred_data_source_reads(text) == {
        ("aws_iam_policy_document", "codebuild_assume_role"),
        ("aws_iam_policy_document", "codebuild_permissions"),
    }
    changed, unexpected = classifier.classify(text)
    assert len(changed) == 8
    assert [u.split(" — ")[0] for u in unexpected] == [
        f"{ROLE_ADDRESS} (update)",
        f"{POLICY_ADDRESS} (update)",
    ]
    assert all("no structured plan was supplied" not in u for u in unexpected)
    assert all("--structured-plan was not supplied" in u for u in unexpected)


def test_the_recorded_real_plan_classifies_clean_with_the_deferred_rerender_detail() -> None:
    """The recorded plan plus a deferred-shape `show -json` document classifies clean."""
    changed, unexpected = classifier.classify(_recorded_plan_text(), _deferred_rerender_show())
    assert len(changed) == 8
    assert unexpected == []


def test_the_tracked_slice_carries_provenance_and_no_account_id() -> None:
    """The tracked slice carries a provenance line and no real account id or ARN."""
    text = _recorded_plan_text()
    provenance = json.loads(text.splitlines()[0])
    assert provenance["type"] == "_fixture_provenance"
    for field in ("purpose", "captured_at", "captured_by", "reduced", "redacted"):
        assert provenance[field], field
    # No ARN survives at all, and the only 12-digit run is the documented placeholder.
    assert "arn:aws" not in text
    assert set(re.findall(r"\b\d{12}\b", text)) <= {"123456789012"}


def test_a_real_policy_edit_alongside_the_same_cascade_still_fails() -> None:
    """A known added statement in the inline policy fails despite the deferred cascade."""
    widened = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["logs:PutLogEvents"], "Resource": "*"},
                {"Effect": "Allow", "Action": ["iam:*"], "Resource": "*"},
            ],
        }
    )
    _, unexpected = classifier.classify(
        _stream(*AGENT_SOURCE_CASCADE, *DEFERRED_IAM_DATA_READS, *DEFERRED_IAM_CONSUMERS),
        _deferred_rerender_show(policy_after=widened),
    )
    assert len(unexpected) == 1
    assert POLICY_ADDRESS in unexpected[0]
    assert "real policy edit and not a deferred re-render" in unexpected[0]


def test_a_changed_principal_written_where_terraform_can_see_it_still_fails() -> None:
    changed_principal = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "*"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )
    show = _show_json(
        (
            ROLE_ADDRESS,
            {
                "actions": ["update"],
                "before": {"assume_role_policy": ASSUME_ROLE_JSON},
                "after": {"assume_role_policy": changed_principal},
                "after_unknown": {},
            },
        ),
    )
    _, unexpected = classifier.classify(
        _stream(
            *AGENT_SOURCE_CASCADE,
            *DEFERRED_IAM_DATA_READS,
            _planned_change(ROLE_ADDRESS, "aws_iam_role", "update"),
        ),
        show,
    )
    assert len(unexpected) == 1
    assert "real policy edit" in unexpected[0]


def test_an_unrelated_attribute_change_on_the_same_role_still_fails() -> None:
    """An attribute delta on the role other than the deferred document fails."""
    _, unexpected = classifier.classify(
        _stream(
            *AGENT_SOURCE_CASCADE,
            *DEFERRED_IAM_DATA_READS,
            _planned_change(ROLE_ADDRESS, "aws_iam_role", "update"),
        ),
        _deferred_rerender_show(role_extra={"max_session_duration": (3600, 43200)}),
    )
    assert len(unexpected) == 1
    assert "'max_session_duration' changes" in unexpected[0]
    assert "no deferred policy document explains" in unexpected[0]


def test_either_named_address_without_the_agent_source_cascade_still_fails() -> None:
    """Either named address fails when the agent-source cascade is absent."""
    for address, resource_type in (
        (ROLE_ADDRESS, "aws_iam_role"),
        (POLICY_ADDRESS, "aws_iam_role_policy"),
    ):
        _, unexpected = classifier.classify(
            _stream(*DEFERRED_IAM_DATA_READS, _planned_change(address, resource_type, "update")),
            _deferred_rerender_show(),
        )
        assert len(unexpected) == 1, address
        assert "agent-source packaging cascade is not pending" in unexpected[0]


def test_a_consumer_whose_data_source_is_not_deferred_still_fails() -> None:
    """A consumer whose data source has no `read_because_dependency_pending` reason fails."""
    _, unexpected = classifier.classify(
        _stream(
            *AGENT_SOURCE_CASCADE,
            *IAM_DATA_READS,  # same addresses, no deferral reason
            _planned_change(ROLE_ADDRESS, "aws_iam_role", "update"),
        ),
        _deferred_rerender_show(),
    )
    assert len(unexpected) == 1
    assert classifier.DEFERRED_READ_REASON in unexpected[0]


def test_a_different_iam_role_of_the_same_name_elsewhere_is_never_absorbed() -> None:
    """A same-named IAM role at a different address is not absorbed."""
    _, unexpected = classifier.classify(
        _stream(
            *AGENT_SOURCE_CASCADE,
            *DEFERRED_IAM_DATA_READS,
            _planned_change("aws_iam_role.codebuild", "aws_iam_role", "update"),
            _planned_change(
                "module.other.aws_iam_role_policy.codebuild", "aws_iam_role_policy", "update"
            ),
        ),
        _deferred_rerender_show(),
    )
    assert len(unexpected) == 2
    assert all(" — " not in u for u in unexpected)


def test_a_create_or_delete_of_the_named_addresses_is_never_absorbed() -> None:
    for action in ("create", "delete", "replace"):
        _, unexpected = classifier.classify(
            _stream(
                *AGENT_SOURCE_CASCADE,
                *DEFERRED_IAM_DATA_READS,
                _planned_change(ROLE_ADDRESS, "aws_iam_role", action),
            ),
            _deferred_rerender_show(),
        )
        assert len(unexpected) == 1, action
        assert " — " not in unexpected[0], action


def test_a_structured_plan_that_omits_the_address_is_a_refusal_not_a_pass() -> None:
    _, unexpected = classifier.classify(
        _stream(
            *AGENT_SOURCE_CASCADE,
            *DEFERRED_IAM_DATA_READS,
            _planned_change(ROLE_ADDRESS, "aws_iam_role", "update"),
        ),
        _show_json(),
    )
    assert len(unexpected) == 1
    assert "carries no resource change for this address" in unexpected[0]


def test_an_unparseable_or_non_plan_structured_document_is_a_refusal_not_a_pass() -> None:
    """An unusable structured document is refused without being reported as an omitted flag."""
    expected = {
        "not json": "is not valid JSON",
        "[]": "carries no 'resource_changes' list",
        json.dumps({"format_version": "1.2"}): "carries no 'resource_changes' list",
        json.dumps({"nope": 1}): "carries no 'resource_changes' list",
    }
    stream = _stream(
        *AGENT_SOURCE_CASCADE,
        *DEFERRED_IAM_DATA_READS,
        _planned_change(ROLE_ADDRESS, "aws_iam_role", "update"),
    )
    for text, phrase in expected.items():
        _, unexpected = classifier.classify(stream, text)
        assert len(unexpected) == 1, text
        assert phrase in unexpected[0], (text, unexpected[0])
        assert "that was supplied" in unexpected[0], text
        assert "--structured-plan was not supplied" not in unexpected[0], text


def test_an_omitted_structured_plan_says_so_and_names_the_flag_to_pass() -> None:
    """Omitting the structured plan produces a message naming the flag to pass."""
    _, unexpected = classifier.classify(
        _stream(
            *AGENT_SOURCE_CASCADE,
            *DEFERRED_IAM_DATA_READS,
            _planned_change(ROLE_ADDRESS, "aws_iam_role", "update"),
        )
    )
    assert len(unexpected) == 1
    assert "--structured-plan was not supplied" in unexpected[0]
    assert "terraform show -json" in unexpected[0]
    assert "that was supplied" not in unexpected[0]


def test_an_empty_before_after_pair_is_a_refusal_not_a_pass() -> None:
    show = _show_json((ROLE_ADDRESS, {"actions": ["update"], "before": None, "after": None}))
    _, unexpected = classifier.classify(
        _stream(
            *AGENT_SOURCE_CASCADE,
            *DEFERRED_IAM_DATA_READS,
            _planned_change(ROLE_ADDRESS, "aws_iam_role", "update"),
        ),
        show,
    )
    assert len(unexpected) == 1
    assert "no before/after object pair" in unexpected[0]


def test_the_process_exits_zero_on_the_recorded_plan_with_the_structured_plan(
    tmp_path: Path,
) -> None:
    """The CLI exits 0 on the recorded plan when given the structured plan."""
    plan = tmp_path / "plan.ndjson"
    plan.write_text(_recorded_plan_text(), "utf-8")
    show = tmp_path / "show.json"
    show.write_text(_deferred_rerender_show(), "utf-8")
    result = _run(str(plan), "--structured-plan", str(show))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pending_changes=8 unexpected=0" in result.stdout

    result = _run(str(plan))
    assert result.returncode == 1
    assert "pending_changes=8 unexpected=2" in result.stdout


def test_the_deferred_consumer_table_is_scoped_to_the_two_known_addresses() -> None:
    """The deferred-consumer table contains exactly the two known addresses."""
    assert set(classifier.DEFERRED_POLICY_DOCUMENT_CONSUMERS) == {ROLE_ADDRESS, POLICY_ADDRESS}
    for consumer in classifier.DEFERRED_POLICY_DOCUMENT_CONSUMERS.values():
        assert consumer.actions == frozenset({"update"})
        assert len(consumer.deferred_data_sources) == 1
        assert len(consumer.document_attributes) == 1


# ---------------------------------------------------------------------------
# The Node relay zip packaging artifact.
# ---------------------------------------------------------------------------

RELAY_ADDRESS = "module.agent_relay.aws_lambda_function.this"

#: What a `scripts/package_relay.sh` rebuild leaves pending: the relay function's `source_code_hash`
#: and nothing else.
RELAY_CHURN = [_planned_change(RELAY_ADDRESS, "aws_lambda_function", "update")]


def test_relay_zip_churn_alone_is_absorbed() -> None:
    """A relay-only pending change classifies `warn`."""
    changed, unexpected = classifier.classify(_stream(*RELAY_CHURN))
    assert changed == [RELAY_ADDRESS]
    assert unexpected == []


def test_relay_churn_alongside_the_python_lambda_churn_is_absorbed() -> None:
    """Relay churn alongside Python Lambda churn is absorbed."""
    changed, unexpected = classifier.classify(_stream(*LAMBDA_CHURN, *RELAY_CHURN))
    assert len(changed) == 25
    assert unexpected == []


@pytest.mark.parametrize("action", ["create", "delete", "replace"])
def test_a_relay_action_other_than_update_is_not_packaging_churn(action: str) -> None:
    """A relay function create, delete or replace is not absorbed."""
    _, unexpected = classifier.classify(
        _stream(_planned_change(RELAY_ADDRESS, "aws_lambda_function", action))
    )
    assert len(unexpected) == 1, unexpected
    assert action in unexpected[0]


@pytest.mark.parametrize(
    ("address", "resource_type"),
    [
        ("module.agent_relay.aws_lambda_function_url.this", "aws_lambda_function_url"),
        ("module.agent_relay.aws_iam_role.this", "aws_iam_role"),
        ("module.agent_relay.aws_iam_role_policy.this", "aws_iam_role_policy"),
        ("module.agent_relay.aws_cloudwatch_log_group.this", "aws_cloudwatch_log_group"),
        ("module.agent_relay.aws_lambda_permission.function_url", "aws_lambda_permission"),
    ],
)
def test_the_relay_resources_that_are_not_hash_consumers_still_fail(
    address: str, resource_type: str
) -> None:
    """Relay resources other than the function's code hash still fail."""
    _, unexpected = classifier.classify(_stream(_planned_change(address, resource_type, "update")))
    assert len(unexpected) == 1, unexpected
    assert address in unexpected[0]


def test_a_non_packaging_change_alongside_relay_churn_still_fails_and_names_only_it() -> None:
    """Real drift alongside relay churn fails, and the report names only the offender."""
    offender = "module.agent_relay.aws_lambda_function_url.this"
    changed, unexpected = classifier.classify(
        _stream(
            *LAMBDA_CHURN,
            *RELAY_CHURN,
            _planned_change(offender, "aws_lambda_function_url", "update"),
        )
    )
    assert len(changed) == 26
    assert len(unexpected) == 1, unexpected
    assert offender in unexpected[0]
    assert RELAY_ADDRESS not in unexpected[0]


def test_a_second_lambda_module_is_not_absorbed_by_name() -> None:
    """No name- or address-keyed allow-list table carries a relay entry."""
    for table in (
        classifier.AGENT_SOURCE_CASCADE,
        classifier.CONDITIONAL_ON_AGENT_SOURCE,
        classifier.DEFERRED_POLICY_DOCUMENT_CONSUMERS,
    ):
        assert not any("relay" in str(key) for key in table)


def test_the_artifact_inventory_names_the_relay_and_grants_nothing() -> None:
    """`PACKAGING_ARTIFACTS` includes the relay zip and the packaging type table has three types."""
    assert set(classifier.PACKAGING_ARTIFACTS) == {
        "the Python Lambda zips",
        "relay.zip",
        "agent-source.zip",
    }
    assert set(classifier.PACKAGING_ATTRIBUTABLE_TYPES) == {
        "aws_lambda_function",
        "terraform_data",
        "null_resource",
    }


def test_the_process_absorbs_a_relay_only_plan_and_names_the_relay_when_it_fails(
    tmp_path: Path,
) -> None:
    """The CLI exits 0 on relay-only churn and, on real drift, names every artifact considered."""
    plan = tmp_path / "relay-only.ndjson"
    plan.write_text(_stream(*RELAY_CHURN), "utf-8")
    result = _run(str(plan))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pending_changes=1 unexpected=0" in result.stdout

    drift = tmp_path / "relay-drift.ndjson"
    drift.write_text(
        _stream(
            *RELAY_CHURN,
            _planned_change(
                "module.agent_relay.aws_lambda_function_url.this",
                "aws_lambda_function_url",
                "update",
            ),
        ),
        "utf-8",
    )
    result = _run(str(drift))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pending_changes=2 unexpected=1" in result.stdout
    assert "absorbed by NO allowance" in result.stderr
    for artifact in classifier.PACKAGING_ARTIFACTS:
        assert artifact in result.stderr
    assert "aws_lambda_function_url.this" in result.stderr
    assert "absorbed_by=packaging-zips:1" in result.stdout
    assert "absorbed by packaging-zips" in result.stderr


# ---------------------------------------------------------------------------
# API Gateway's resource-policy ARN normalisation.
#
# Every account id, VPC endpoint id and REST API id below is a placeholder.
# ---------------------------------------------------------------------------

API_ADDRESS = "module.api.aws_api_gateway_rest_api.this"
_PLACEHOLDER_API_ARN = "arn:aws:execute-api:us-east-1:123456789012:aaaaaaaaaa/*"
_PLACEHOLDER_VPCE = "vpce-00000000000000000"


def _api_policy(
    *,
    allow_resource: object = "execute-api:/*",
    deny_resource: object = "execute-api:/*",
    statements: list[dict[str, object]] | None = None,
    version: str = "2012-10-17",
) -> str:
    """A resource policy: a public `Allow` narrowed by a `sourceVpce`-scoped `Deny`."""
    if statements is None:
        statements = [
            {
                "Sid": "AllowInvokeFromVpcEndpoint",
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Action": "execute-api:Invoke",
                "Resource": allow_resource,
            },
            {
                "Sid": "DenyInvokeOutsideVpcEndpoint",
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": "execute-api:Invoke",
                "Resource": deny_resource,
                "Condition": {"StringNotEquals": {"aws:sourceVpce": _PLACEHOLDER_VPCE}},
            },
        ]
    return json.dumps({"Version": version, "Statement": statements})


def _api_show(
    *,
    before_policy: str | None = None,
    after_policy: str | None = None,
    extra_after: dict[str, object] | None = None,
    after_unknown: dict[str, object] | None = None,
) -> str:
    """A `show -json` document for the API row: stored ARN form before, relative form after."""
    before = {
        "name": "crm-agent-eval-api",
        "policy": before_policy
        if before_policy is not None
        else _api_policy(allow_resource=_PLACEHOLDER_API_ARN, deny_resource=_PLACEHOLDER_API_ARN),
    }
    after: dict[str, object] = {
        "name": "crm-agent-eval-api",
        "policy": after_policy if after_policy is not None else _api_policy(),
    }
    if extra_after:
        after.update(extra_after)
    return _show_json(
        (
            API_ADDRESS,
            {
                "actions": ["update"],
                "before": before,
                "after": after,
                "after_unknown": after_unknown or {},
            },
        ),
    )


API_CHANGE = [_planned_change(API_ADDRESS, "aws_api_gateway_rest_api", "update")]


def test_the_api_resource_policy_arn_normalisation_is_absorbed() -> None:
    """A qualified-ARN stored `Resource` against its relative rendered form is absorbed."""
    changed, unexpected = classifier.classify(_stream(*API_CHANGE), _api_show())
    assert changed == [API_ADDRESS]
    assert unexpected == []


def test_the_normalisation_allowance_is_the_only_reason_that_row_passes() -> None:
    """Without the structured plan the same API policy row is refused."""
    _, unexpected = classifier.classify(_stream(*API_CHANGE))
    assert len(unexpected) == 1
    assert "--structured-plan was not supplied" in unexpected[0]


def test_the_api_policy_row_is_attributed_to_arn_normalisation_and_not_to_packaging() -> None:
    """The API policy row is attributed to ARN normalisation and not to packaging."""
    result = classifier.classify_attributed(_stream(*API_CHANGE), _api_show())
    assert result.unexpected == []
    assert result.absorbed == {"arn-normalisation": [API_ADDRESS]}
    assert "packaging-zips" not in result.absorbed
    assert result.summary() == "pending_changes=1 unexpected=0 absorbed_by=arn-normalisation:1"


def test_every_allowance_reports_under_its_own_key_and_not_another() -> None:
    """Each allowance reports absorbed rows under its own key and not under another's."""
    # The agent-source.zip cascade. Rules match first-wins, so `null_resource.image_build` reports
    # as `packaging-zips` via PACKAGING_ATTRIBUTABLE_TYPES.
    cascade = classifier.classify_attributed(_stream(*AGENT_SOURCE_CASCADE))
    assert cascade.unexpected == []
    assert cascade.absorbed == {
        "packaging-zips": ["module.agentcore.null_resource.image_build"],
        "agent-source-cascade": [
            "aws_s3_object.agent_source",
            "module.agentcore.aws_codebuild_project.agent_image",
        ],
        # Absorbed only because agent-source.zip churned in the same plan.
        "agent-source-dependent": ["module.agentcore.aws_bedrockagentcore_agent_runtime.this"],
    }, cascade.absorbed

    # The deferred policy-document re-render, which needs the structured plan.
    deferred = classifier.classify_attributed(_recorded_plan_text(), _deferred_rerender_show())
    assert deferred.unexpected == []
    assert "deferred-policy-rerender" in deferred.absorbed, deferred.absorbed
    assert deferred.absorbed["deferred-policy-rerender"] == [
        "module.agentcore.aws_iam_role.codebuild",
        "module.agentcore.aws_iam_role_policy.codebuild",
    ], deferred.absorbed["deferred-policy-rerender"]

    # Packaging: the zips report under their own key only.
    packaging = classifier.classify_attributed(_stream(*LAMBDA_CHURN))
    assert packaging.unexpected == []
    assert set(packaging.absorbed) == {"packaging-zips"}
    assert len(packaging.absorbed["packaging-zips"]) == len(LAMBDA_CHURN)


def test_the_process_names_the_absorbing_allowance_on_stdout_and_stderr(tmp_path: Path) -> None:
    """The CLI names the absorbing allowance on a one-line stdout and per address on stderr."""
    plan = tmp_path / "plan.ndjson"
    plan.write_text(_stream(*API_CHANGE), "utf-8")
    show = tmp_path / "show.json"
    show.write_text(_api_show(), "utf-8")
    result = _run(str(plan), "--structured-plan", str(show))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (
        "pending_changes=1 unexpected=0 absorbed_by=arn-normalisation:1"
    ), result.stdout
    assert len(result.stdout.strip().splitlines()) == 1, result.stdout
    assert "absorbed by arn-normalisation" in result.stderr
    assert API_ADDRESS in result.stderr
    assert "packaging-zips" not in result.stdout


@pytest.mark.parametrize(
    ("label", "policy", "phrase"),
    [
        (
            "an added statement",
            _api_policy(
                statements=[
                    {
                        "Sid": "AllowInvokeFromVpcEndpoint",
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "execute-api:Invoke",
                        "Resource": "execute-api:/*",
                    },
                ]
            ),
            "statement count changes",
        ),
        (
            "a flipped Effect on the DENY",
            _api_policy(
                statements=[
                    {
                        "Sid": "AllowInvokeFromVpcEndpoint",
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "execute-api:Invoke",
                        "Resource": "execute-api:/*",
                    },
                    {
                        "Sid": "DenyInvokeOutsideVpcEndpoint",
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "execute-api:Invoke",
                        "Resource": "execute-api:/*",
                        "Condition": {"StringNotEquals": {"aws:sourceVpce": _PLACEHOLDER_VPCE}},
                    },
                ]
            ),
            "'Effect' changes",
        ),
        (
            "a dropped sourceVpce Condition",
            _api_policy(
                statements=[
                    {
                        "Sid": "AllowInvokeFromVpcEndpoint",
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "execute-api:Invoke",
                        "Resource": "execute-api:/*",
                    },
                    {
                        "Sid": "DenyInvokeOutsideVpcEndpoint",
                        "Effect": "Deny",
                        "Principal": {"AWS": "*"},
                        "Action": "execute-api:Invoke",
                        "Resource": "execute-api:/*",
                    },
                ]
            ),
            "keys differ",
        ),
        (
            "a changed Version",
            _api_policy(version="2008-10-17"),
            "'Version' changes",
        ),
    ],
)
def test_a_real_policy_change_on_the_api_still_fails(label: str, policy: str, phrase: str) -> None:
    """A semantic change to the API resource policy fails."""
    _, unexpected = classifier.classify(_stream(*API_CHANGE), _api_show(after_policy=policy))
    assert len(unexpected) == 1, label
    assert phrase in unexpected[0], (label, unexpected[0])


def test_a_widened_principal_on_the_api_still_fails() -> None:
    """A changed `Principal` on the API resource policy fails."""
    widened = _api_policy(
        statements=[
            {
                "Sid": "AllowInvokeFromVpcEndpoint",
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "execute-api:Invoke",
                "Resource": "execute-api:/*",
            },
            {
                "Sid": "DenyInvokeOutsideVpcEndpoint",
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": "execute-api:Invoke",
                "Resource": "execute-api:/*",
                "Condition": {"StringNotEquals": {"aws:sourceVpce": _PLACEHOLDER_VPCE}},
            },
        ]
    )
    assert widened != _api_policy(), "the mutation must actually change the document"
    _, unexpected = classifier.classify(_stream(*API_CHANGE), _api_show(after_policy=widened))
    assert len(unexpected) == 1
    assert "'Principal' changes" in unexpected[0]


@pytest.mark.parametrize(
    ("label", "after_resource"),
    [
        ("a different resource path", "execute-api:/prod/*"),
        ("a wildcard API id", "arn:aws:execute-api:us-east-1:123456789012:*/*"),
        ("a bare wildcard", "*"),
    ],
)
def test_a_resource_that_does_not_normalise_to_its_after_value_still_fails(
    label: str, after_resource: str
) -> None:
    """A stored `Resource` ARN that does not normalise to exactly the rendered value fails."""
    _, unexpected = classifier.classify(
        _stream(*API_CHANGE), _api_show(after_policy=_api_policy(allow_resource=after_resource))
    )
    assert len(unexpected) == 1, label
    assert "does not normalise to its own after-value" in unexpected[0], label


def test_a_resource_whose_before_value_is_not_an_execute_api_arn_still_fails() -> None:
    """A stored `Resource` that is not a qualified execute-api ARN fails."""
    _, unexpected = classifier.classify(
        _stream(*API_CHANGE),
        _api_show(before_policy=_api_policy(allow_resource="execute-api:/stage/*")),
    )
    assert len(unexpected) == 1
    assert "does not change from a fully-qualified execute-api ARN" in unexpected[0]


def test_an_unrelated_attribute_on_the_api_still_fails() -> None:
    _, unexpected = classifier.classify(
        _stream(*API_CHANGE), _api_show(extra_after={"name": "crm-agent-eval-api-renamed"})
    )
    assert len(unexpected) == 1
    assert "'name' changes" in unexpected[0]
    assert "does not explain" in unexpected[0]


def test_an_unknown_rendered_api_policy_is_refused_by_this_rule() -> None:
    """An unknown rendered API policy is refused by the ARN-normalisation rule."""
    _, unexpected = classifier.classify(
        _stream(*API_CHANGE), _api_show(after_unknown={"policy": True})
    )
    assert len(unexpected) == 1
    assert "renders unknown" in unexpected[0]


def test_a_non_update_action_on_the_api_is_never_absorbed() -> None:
    for action in ("create", "delete", "replace"):
        _, unexpected = classifier.classify(
            _stream(_planned_change(API_ADDRESS, "aws_api_gateway_rest_api", action)),
            _api_show(),
        )
        assert len(unexpected) == 1, action
        assert " — " not in unexpected[0], action


def test_a_different_rest_api_is_never_absorbed() -> None:
    """A different REST API address is not absorbed."""
    other = "module.public_api.aws_api_gateway_rest_api.this"
    _, unexpected = classifier.classify(
        _stream(_planned_change(other, "aws_api_gateway_rest_api", "update")),
        _show_json(
            (
                other,
                {
                    "actions": ["update"],
                    "before": {"policy": _api_policy(allow_resource=_PLACEHOLDER_API_ARN)},
                    "after": {"policy": _api_policy()},
                    "after_unknown": {},
                },
            ),
        ),
    )
    assert len(unexpected) == 1
    assert " — " not in unexpected[0]


# ---------------------------------------------------------------------------
# The module-level `depends_on` deferral cause.
#
# The root `module "agentcore"` call's `depends_on` defers every data source inside the module,
# so both CodeBuild IAM documents can re-render with no agent-source cascade pending.
# ---------------------------------------------------------------------------


def _with_module_depends_on(show: str, *, call: str = "agentcore") -> str:
    """Add the configuration graph terraform emits for a module call carrying `depends_on`."""
    document = json.loads(show)
    document["configuration"] = {
        "root_module": {
            "module_calls": {
                call: {
                    "source": "./modules/agentcore",
                    "depends_on": ["aws_s3_object.agent_source"],
                }
            }
        }
    }
    return json.dumps(document)


def test_the_module_depends_on_cause_absorbs_the_rerender_without_the_cascade() -> None:
    """A module `depends_on` absorbs the re-render with no agent-source cascade pending."""
    _, unexpected = classifier.classify(
        _stream(*DEFERRED_IAM_DATA_READS, *DEFERRED_IAM_CONSUMERS),
        _with_module_depends_on(_deferred_rerender_show()),
    )
    assert unexpected == []


def test_withdrawing_the_module_depends_on_restores_the_refusal() -> None:
    """Removing the module `depends_on` entry turns both rows back into refusals."""
    show = json.loads(_with_module_depends_on(_deferred_rerender_show()))
    show["configuration"]["root_module"]["module_calls"]["agentcore"].pop("depends_on")
    _, unexpected = classifier.classify(
        _stream(*DEFERRED_IAM_DATA_READS, *DEFERRED_IAM_CONSUMERS), json.dumps(show)
    )
    assert len(unexpected) == 2
    assert all("declares no depends_on" in u for u in unexpected)


def test_a_depends_on_declared_on_some_other_module_grants_nothing() -> None:
    """A `depends_on` on a different module call grants nothing."""
    _, unexpected = classifier.classify(
        _stream(*DEFERRED_IAM_DATA_READS, *DEFERRED_IAM_CONSUMERS),
        _with_module_depends_on(_deferred_rerender_show(), call="observability"),
    )
    assert len(unexpected) == 2
    assert all("declares no depends_on" in u for u in unexpected)


def test_the_module_depends_on_cause_does_not_relax_the_content_conditions() -> None:
    """A known IAM policy edit still fails under the module `depends_on` cause."""
    widened = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": ["iam:*"], "Resource": "*"}],
        }
    )
    _, unexpected = classifier.classify(
        _stream(*DEFERRED_IAM_DATA_READS, *DEFERRED_IAM_CONSUMERS),
        _with_module_depends_on(_deferred_rerender_show(policy_after=widened)),
    )
    assert len(unexpected) == 1
    assert "real policy edit and not a deferred re-render" in unexpected[0]


def test_module_call_dependencies_reads_the_configuration_graph() -> None:
    assert classifier.module_call_dependencies(
        _with_module_depends_on(_deferred_rerender_show())
    ) == {"agentcore": ["aws_s3_object.agent_source"]}


@pytest.mark.parametrize(
    "text", [None, "not json", "[]", json.dumps({"configuration": {}}), json.dumps({"nope": 1})]
)
def test_module_call_dependencies_refuses_rather_than_inventing_a_dependency(
    text: str | None,
) -> None:
    """An empty mapping is a refusal: no configuration graph means no established cause."""
    assert classifier.module_call_dependencies(text) == {}


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("module.agentcore.aws_iam_role.codebuild", "agentcore"),
        ("module.agentcore.aws_iam_role_policy.codebuild", "agentcore"),
        ('module.lambda_action["get_account"].aws_lambda_function.this', "lambda_action"),
        # A nested module call's depends_on is not returned for a root-level lookup.
        ("module.outer.module.inner.aws_iam_role.codebuild", None),
        ("aws_iam_role.codebuild", None),
        ("module.agentcore", None),
    ],
)
def test_the_module_call_name_is_parsed_and_fails_closed_when_nested(
    address: str, expected: str | None
) -> None:
    assert classifier._module_call_name(address) == expected
