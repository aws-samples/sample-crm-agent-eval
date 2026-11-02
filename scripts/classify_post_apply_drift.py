#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Classify a post-apply `terraform plan -json` stream against the packaging-drift allow-list.

`scripts/package_lambdas.sh` is not byte-deterministic, so an apply leaves resources derived from a
built artifact's hash showing pending changes. `scripts/deploy.sh` runs the post-apply plan and pipes
it here; the deploy passes only if every pending change is absorbed by one of these allowances:

* **Lambda zips** (`packaging-zips`): `update` on `aws_lambda_function` (which covers the relay's
  `source_code_hash`), and `update`/`replace` on the `terraform_data` / `null_resource` trigger
  carriers.
* **`agent-source.zip` cascade** (`agent-source-cascade`): `update` on `aws_s3_object.agent_source`
  and `aws_codebuild_project.agent_image`.
* **AgentCore Runtime** (`agent-source-dependent`): `update` on the Runtime, only when
  `aws_s3_object.agent_source` or `null_resource.image_build` is pending in the same plan.
* **Deferred policy documents** (`deferred-policy-rerender`): `update` on the two CodeBuild IAM
  addresses in :data:`DEFERRED_POLICY_DOCUMENT_CONSUMERS`, only when a deferral cause is present (the
  agent-source cascade is pending, or the address's root module call declares `depends_on`), each
  declared data source is a `read_because_dependency_pending` read, and the `terraform show -json`
  plan shows every delta is an unknown re-render of the document attribute.
* **ARN normalisation** (`arn-normalisation`): `update` on the REST API in
  :data:`SELF_NORMALISING_POLICY_RESOURCES`, only when the stored and configured policies are the
  same statement set and each differing `Resource` normalises from a fully-qualified `execute-api`
  ARN to exactly its configured relative form.

Every other action (including any `create` or `delete`) is unexpected. Data-source addresses are
excluded from the pending count.

Usage:
    classify_post_apply_drift.py PLAN_NDJSON_PATH [--structured-plan SHOW_JSON_PATH]

Exit codes: 0 = no pending change, or every pending change is absorbed by an allowance;
1 = at least one pending change is absorbed by no allowance (addresses printed to stderr);
2 = an input file could not be read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Non-deterministic build artifacts mapped to the pending changes they explain. Named in the failure
#: message only; it grants nothing — the tables below are the allow-list.
PACKAGING_ARTIFACTS: dict[str, str] = {
    "the Python Lambda zips": "aws_lambda_function.source_code_hash and its trigger-hash carriers",
    "relay.zip": "module.agent_relay.aws_lambda_function.this.source_code_hash",
    "agent-source.zip": (
        "aws_s3_object.agent_source, the CodeBuild project's baked-in IMAGE_TAG, "
        "null_resource.image_build, and the AgentCore Runtime's container_uri"
    ),
}

#: Resource types whose Lambda-zip packaging churn is absorbed, mapped to the allowed actions.
PACKAGING_ATTRIBUTABLE_TYPES: dict[str, frozenset[str]] = {
    "aws_lambda_function": frozenset({"update"}),
    "terraform_data": frozenset({"update", "replace"}),
    "null_resource": frozenset({"update", "replace"}),
}

#: The `agent-source.zip` hash cascade, keyed on `(resource_type, resource_name)`.
#: `null_resource.image_build` is covered by the type table above.
AGENT_SOURCE_CASCADE: dict[tuple[str, str], frozenset[str]] = {
    ("aws_s3_object", "agent_source"): frozenset({"update"}),
    ("aws_codebuild_project", "agent_image"): frozenset({"update"}),
}

#: Absorbed only when an :data:`_AGENT_SOURCE_EVIDENCE` resource is pending in the same plan.
CONDITIONAL_ON_AGENT_SOURCE: dict[tuple[str, str], frozenset[str]] = {
    ("aws_bedrockagentcore_agent_runtime", "this"): frozenset({"update"}),
}

#: Resources whose pending change enables :data:`CONDITIONAL_ON_AGENT_SOURCE`.
_AGENT_SOURCE_EVIDENCE: frozenset[tuple[str, str]] = frozenset(
    {("aws_s3_object", "agent_source"), ("null_resource", "image_build")}
)

_NON_CHANGE_ACTIONS = frozenset({"", "noop", "read"})

#: Terraform's reason code for a data source re-read during apply because a dependency is pending.
DEFERRED_READ_REASON = "read_because_dependency_pending"


@dataclass(frozen=True)
class DeferredPolicyDocumentConsumer:
    """An IAM resource whose pending change may be a deferred policy-document re-render.

    Absorption conditions are checked by :func:`_deferred_rerender_rejection`.
    """

    actions: frozenset[str]
    deferred_data_sources: frozenset[tuple[str, str]]
    document_attributes: frozenset[str]


#: Deferred policy-document consumers, keyed on the full resource address.
DEFERRED_POLICY_DOCUMENT_CONSUMERS: dict[str, DeferredPolicyDocumentConsumer] = {
    "module.agentcore.aws_iam_role.codebuild": DeferredPolicyDocumentConsumer(
        actions=frozenset({"update"}),
        deferred_data_sources=frozenset({("aws_iam_policy_document", "codebuild_assume_role")}),
        document_attributes=frozenset({"assume_role_policy"}),
    ),
    "module.agentcore.aws_iam_role_policy.codebuild": DeferredPolicyDocumentConsumer(
        actions=frozenset({"update"}),
        deferred_data_sources=frozenset({("aws_iam_policy_document", "codebuild_permissions")}),
        document_attributes=frozenset({"policy"}),
    ),
}


@dataclass(frozen=True)
class SelfNormalisingPolicyResource:
    """A resource whose policy attribute the service rewrites on read, so its diff never converges.

    Absorption requires :func:`_policy_normalisation_rejection` to find the delta is ARN
    normalisation and nothing else.
    """

    actions: frozenset[str]
    policy_attribute: str


#: Self-normalising policy resources, keyed on the full resource address.
SELF_NORMALISING_POLICY_RESOURCES: dict[str, SelfNormalisingPolicyResource] = {
    "module.api.aws_api_gateway_rest_api.this": SelfNormalisingPolicyResource(
        actions=frozenset({"update"}),
        policy_attribute="policy",
    ),
}

#: Every allowance, keyed by the short name reported, mapped to what it absorbs. Declaration order
#: is the reporting order.
ALLOWANCES: dict[str, str] = {
    "packaging-zips": ("package_lambdas.sh non-determinism in the Lambda-zip half of the build"),
    "agent-source-cascade": "the agent-source.zip hash cascade",
    "agent-source-dependent": (
        "a consumer absorbed only because agent-source.zip itself churned in this plan"
    ),
    "deferred-policy-rerender": ("a policy document deferred to apply time, re-rendered unknown"),
    "arn-normalisation": (
        "API Gateway's permanent resource-policy ARN normalisation, which no apply converges"
    ),
}

#: A fully-qualified `execute-api` ARN, capturing the resource path after the API id.
_EXECUTE_API_ARN = re.compile(r"^arn:[^:]+:execute-api:[^:]*:[^:]*:[^:/]+(?P<path>/.*)$")


def _relative_execute_api_form(arn: str) -> str | None:
    """The relative `execute-api:<path>` spelling of a qualified ARN, or ``None`` if it is not one."""
    match = _EXECUTE_API_ARN.match(arn)
    if match is None:
        return None
    return f"execute-api:{match.group('path')}"


@dataclass(frozen=True)
class PlannedChange:
    """One `planned_change` message reduced to what classification needs."""

    address: str
    resource_type: str
    resource_name: str
    action: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.resource_type, self.resource_name)

    def describe(self) -> str:
        return f"{self.address} ({self.action})"


def _resource_name(resource: dict[str, object], address: str, resource_type: str) -> str:
    """The resource's local name, falling back to parsing it out of the address.

    Any `[index]` suffix is stripped.
    """
    declared = resource.get("resource_name")
    if isinstance(declared, str) and declared:
        return declared.split("[", 1)[0]
    marker = f"{resource_type}."
    if marker in address:
        return address.split(marker, 1)[1].split(".", 1)[0].split("[", 1)[0]
    return "(unknown name)"


def _parse(stream_text: str) -> list[PlannedChange]:
    """Extract every real pending change from a `terraform plan -json` stream.

    Messages with action `""`, `noop` or `read`, and data-source addresses, are dropped.
    """
    changes: list[PlannedChange] = []
    for line in stream_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("type") != "planned_change":
            continue
        change = message.get("change") or {}
        action = str(change.get("action") or "")
        if action in _NON_CHANGE_ACTIONS:
            continue
        resource = change.get("resource") or {}
        address = str(resource.get("addr") or resource.get("address") or "(unknown address)")
        if address.startswith("data.") or ".data." in address:
            continue
        resource_type = str(resource.get("resource_type") or "(unknown type)")
        changes.append(
            PlannedChange(
                address=address,
                resource_type=resource_type,
                resource_name=_resource_name(resource, address, resource_type),
                action=action,
            )
        )
    return changes


def deferred_data_source_reads(stream_text: str) -> set[tuple[str, str]]:
    """Every data source terraform will re-read during apply because a dependency is pending.

    Returns `(resource_type, resource_name)` pairs for reads carrying :data:`DEFERRED_READ_REASON`.
    """
    deferred: set[tuple[str, str]] = set()
    for line in stream_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("type") != "planned_change":
            continue
        change = message.get("change") or {}
        if str(change.get("action") or "") != "read":
            continue
        if str(change.get("reason") or "") != DEFERRED_READ_REASON:
            continue
        resource = change.get("resource") or {}
        address = str(resource.get("addr") or resource.get("address") or "")
        if not (address.startswith("data.") or ".data." in address):
            continue
        resource_type = str(resource.get("resource_type") or "")
        name = str(resource.get("resource_name") or "").split("[", 1)[0]
        if resource_type and name:
            deferred.add((resource_type, name))
    return deferred


def structured_resource_changes(text: str | None) -> dict[str, dict[str, object]] | None:
    """Index a ``terraform show -json <planfile>`` document by resource address.

    Returns ``None`` when no document was supplied or it is not a plan document; callers then
    refuse the change, and :func:`structured_plan_defect` explains which case applies.
    """
    if text is None:
        return None
    try:
        document = json.loads(text)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    entries = document.get("resource_changes")
    if not isinstance(entries, list):
        return None
    indexed: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        change = entry.get("change")
        if isinstance(address, str) and isinstance(change, dict):
            indexed[address] = change
    return indexed


def module_call_dependencies(text: str | None) -> dict[str, list[str]]:
    """Each root-level module call's declared `depends_on`, from a `terraform show -json` document.

    Returns an empty mapping when no document or configuration graph is available.
    """
    if text is None:
        return {}
    try:
        document = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(document, dict):
        return {}
    configuration = document.get("configuration")
    root = configuration.get("root_module") if isinstance(configuration, dict) else None
    calls = root.get("module_calls") if isinstance(root, dict) else None
    if not isinstance(calls, dict):
        return {}
    dependencies: dict[str, list[str]] = {}
    for name, call in calls.items():
        if not isinstance(call, dict):
            continue
        declared = call.get("depends_on")
        if isinstance(declared, list):
            dependencies[str(name)] = [str(entry) for entry in declared]
    return dependencies


def _module_call_name(address: str) -> str | None:
    """The root-level module call an address belongs to, or ``None``.

    A nested address (`module.a.module.b.…`) returns ``None``.
    """
    if not address.startswith("module."):
        return None
    parts = address.split(".", 2)
    if len(parts) < 3 or parts[2].startswith("module."):
        return None
    return parts[1].split("[", 1)[0]


def structured_plan_defect(text: str | None) -> str:
    """Why :func:`structured_resource_changes` returned ``None``, phrased for the operator.

    Distinguishes a missing ``--structured-plan`` from one that is not a plan document.
    """
    if text is None:
        return (
            "--structured-plan was not supplied, so this change's semantic delta could not be "
            "checked (pass --structured-plan from `terraform show -json <planfile>`)"
        )
    try:
        document = json.loads(text)
    except ValueError as exc:
        return (
            "the --structured-plan document that was supplied is not valid JSON "
            f"({exc}), so this change's semantic delta could not be checked (it must be the "
            "output of `terraform show -json <planfile>`)"
        )
    if not isinstance(document, dict) or not isinstance(document.get("resource_changes"), list):
        return (
            "the --structured-plan document that was supplied parses but carries no "
            "'resource_changes' list, so it is not a `terraform show -json <planfile>` plan and "
            "this change's semantic delta could not be checked"
        )
    return (
        "the --structured-plan document that was supplied could not be indexed, so this change's "
        "semantic delta could not be checked"
    )


def _semantic_delta(change: dict[str, object], document_attributes: frozenset[str]) -> str | None:
    """``None`` when a change's only delta is a deferred re-render; else why it is not.

    Rejects any differing attribute outside `document_attributes`, and any attribute inside it
    that changes to a known value. A content edit inside a deferred document is not visible here.
    """
    before = change.get("before")
    after = change.get("after")
    unknown = change.get("after_unknown")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "the structured plan carries no before/after object pair for this address"
    unknown_map = unknown if isinstance(unknown, dict) else {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        if key not in document_attributes:
            return (
                f"attribute {key!r} changes, which no deferred policy document explains "
                f"(deferred attributes here: {sorted(document_attributes)})"
            )
        if not unknown_map.get(key):
            return (
                f"attribute {key!r} changes to a value terraform already knows, so this is a real "
                "policy edit and not a deferred re-render"
            )
    return None


def _resource_arn_normalisation_rejection(
    index: int, before_value: object, after_value: object
) -> str | None:
    """``None`` when a statement's `Resource` delta is purely ARN-to-relative normalisation."""
    if isinstance(before_value, list) != isinstance(after_value, list):
        return f"statement {index}'s 'Resource' changes between a single value and a list"
    before_items = before_value if isinstance(before_value, list) else [before_value]
    after_items = after_value if isinstance(after_value, list) else [after_value]
    if len(before_items) != len(after_items):
        return (
            f"statement {index}'s 'Resource' count changes "
            f"({len(before_items)} -> {len(after_items)}), so a resource was added or removed"
        )
    for before_item, after_item in zip(before_items, after_items, strict=True):
        if before_item == after_item:
            continue
        if not isinstance(before_item, str) or not isinstance(after_item, str):
            return f"statement {index}'s 'Resource' is not a string on both sides"
        relative = _relative_execute_api_form(before_item)
        if relative is None:
            return (
                f"statement {index}'s 'Resource' does not change from a fully-qualified execute-api "
                "ARN, so this is not the service's ARN normalisation"
            )
        if relative != after_item:
            return (
                f"statement {index}'s 'Resource' does not normalise to its own after-value, so the "
                "statement targets a different resource rather than the same one written relatively"
            )
    return None


def _statement_set_arn_normalisation(before_doc: object, after_doc: object) -> str | None:
    """``None`` when two policy documents differ only by `Resource` ARN normalisation."""
    if not isinstance(before_doc, dict) or not isinstance(after_doc, dict):
        return "the policy is not a JSON object on both sides"
    if set(before_doc) != set(after_doc):
        return (
            "the policy's top-level keys differ "
            f"({sorted(set(before_doc) ^ set(after_doc))}), which is not ARN normalisation"
        )
    for key in sorted(set(before_doc) - {"Statement"}):
        if before_doc[key] != after_doc[key]:
            return f"the policy's {key!r} changes, which is not ARN normalisation"
    before_statements = before_doc.get("Statement")
    after_statements = after_doc.get("Statement")
    if not isinstance(before_statements, list) or not isinstance(after_statements, list):
        return "the policy carries no 'Statement' list on both sides"
    if len(before_statements) != len(after_statements):
        return (
            f"the statement count changes ({len(before_statements)} -> {len(after_statements)}), so "
            "a statement was added or removed"
        )
    for index, (before_statement, after_statement) in enumerate(
        zip(before_statements, after_statements, strict=True)
    ):
        if not isinstance(before_statement, dict) or not isinstance(after_statement, dict):
            return f"statement {index} is not a JSON object on both sides"
        if set(before_statement) != set(after_statement):
            return (
                f"statement {index}'s keys differ "
                f"({sorted(set(before_statement) ^ set(after_statement))}), which is not ARN "
                "normalisation"
            )
        for key in sorted(before_statement):
            if before_statement[key] == after_statement[key]:
                continue
            if key != "Resource":
                return (
                    f"statement {index}'s {key!r} changes, which is a real policy edit -- only "
                    "'Resource' may differ under ARN normalisation"
                )
            rejection = _resource_arn_normalisation_rejection(
                index, before_statement[key], after_statement[key]
            )
            if rejection is not None:
                return rejection
    return None


def _policy_normalisation_rejection(
    change: PlannedChange,
    resource: SelfNormalisingPolicyResource,
    *,
    structured: dict[str, dict[str, object]] | None,
    structured_plan_text: str | None,
) -> str | None:
    """``None`` to absorb `change` as service-side ARN normalisation; else why it must not be."""
    if structured is None:
        return structured_plan_defect(structured_plan_text)
    detail = structured.get(change.address)
    if detail is None:
        return "the structured plan carries no resource change for this address"
    before = detail.get("before")
    after = detail.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "the structured plan carries no before/after object pair for this address"
    attribute = resource.policy_attribute
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        if key != attribute:
            return (
                f"attribute {key!r} changes, which ARN normalisation of {attribute!r} does not "
                "explain"
            )
    if before.get(attribute) == after.get(attribute):
        return f"attribute {attribute!r} does not differ, so there is nothing to attribute"
    unknown = detail.get("after_unknown")
    unknown_map = unknown if isinstance(unknown, dict) else {}
    if unknown_map.get(attribute):
        return (
            f"attribute {attribute!r} renders unknown, so the plan cannot show it to be ARN "
            "normalisation -- an unknown re-render is a different rule"
        )
    try:
        before_doc = json.loads(before[attribute])
        after_doc = json.loads(after[attribute])
    except (TypeError, ValueError) as exc:
        return f"attribute {attribute!r} is not parseable JSON on both sides ({exc})"
    return _statement_set_arn_normalisation(before_doc, after_doc)


def _deferred_rerender_rejection(
    change: PlannedChange,
    consumer: DeferredPolicyDocumentConsumer,
    *,
    agent_source_churned: bool,
    module_depends_on: list[str],
    deferred_reads: set[tuple[str, str]],
    structured: dict[str, dict[str, object]] | None,
    structured_plan_text: str | None,
) -> str | None:
    """``None`` to absorb `change` as a deferred re-render; otherwise why it must not be absorbed."""
    if not agent_source_churned and not module_depends_on:
        return (
            "the agent-source packaging cascade is not pending in this plan and this address's "
            "module call declares no depends_on that would defer its data sources, so nothing "
            "explains a deferred policy-document re-render"
        )
    absent = sorted(consumer.deferred_data_sources - deferred_reads)
    if absent:
        return (
            f"terraform is not re-reading {absent} with reason {DEFERRED_READ_REASON!r} in this "
            "plan, so this change is not a deferred re-render"
        )
    if structured is None:
        return structured_plan_defect(structured_plan_text)
    detail = structured.get(change.address)
    if detail is None:
        return "the structured plan carries no resource change for this address"
    return _semantic_delta(detail, consumer.document_attributes)


@dataclass
class Classification:
    """A classified plan: what changed, what was refused, and which allowance absorbed each row.

    `absorbed` maps an :data:`ALLOWANCES` key to the addresses that key absorbed, in
    :data:`ALLOWANCES` declaration order.
    """

    changed: list[str]
    unexpected: list[str]
    absorbed: dict[str, list[str]]

    def summary(self) -> str:
        """The one-line operator summary printed on stdout.

        Format: `pending_changes=N unexpected=M`, followed by `absorbed_by=<key>:<n>,…` when any
        change was absorbed.
        """
        line = f"pending_changes={len(self.changed)} unexpected={len(self.unexpected)}"
        if self.absorbed:
            attributed = ",".join(
                f"{key}:{len(addresses)}" for key, addresses in self.absorbed.items()
            )
            return f"{line} absorbed_by={attributed}"
        return line


def classify(
    stream_text: str, structured_plan_text: str | None = None
) -> tuple[list[str], list[str]]:
    """Return `(changed_addresses, unexpected_addresses)` from a `terraform plan -json` stream.

    Delegates to :func:`classify_attributed` and drops the attribution.
    """
    result = classify_attributed(stream_text, structured_plan_text)
    return result.changed, result.unexpected


def classify_attributed(
    stream_text: str, structured_plan_text: str | None = None
) -> Classification:
    """Classify a plan and record which allowance absorbed each pending change.

    `structured_plan_text` is a ``terraform show -json <planfile>`` document. Without it, entries in
    :data:`DEFERRED_POLICY_DOCUMENT_CONSUMERS` and :data:`SELF_NORMALISING_POLICY_RESOURCES` are
    refused.
    """
    changes = _parse(stream_text)
    deferred_reads = deferred_data_source_reads(stream_text)
    structured = structured_resource_changes(structured_plan_text)
    module_dependencies = module_call_dependencies(structured_plan_text)
    agent_source_churned = any(c.key in _AGENT_SOURCE_EVIDENCE for c in changes)

    changed: list[str] = []
    unexpected: list[str] = []
    # Seeded in ALLOWANCES order and pruned of empty entries at the end.
    absorbed: dict[str, list[str]] = {key: [] for key in ALLOWANCES}
    for change in changes:
        changed.append(change.address)
        if change.action in PACKAGING_ATTRIBUTABLE_TYPES.get(change.resource_type, frozenset()):
            absorbed["packaging-zips"].append(change.address)
            continue
        if change.action in AGENT_SOURCE_CASCADE.get(change.key, frozenset()):
            absorbed["agent-source-cascade"].append(change.address)
            continue
        if agent_source_churned and change.action in CONDITIONAL_ON_AGENT_SOURCE.get(
            change.key, frozenset()
        ):
            absorbed["agent-source-dependent"].append(change.address)
            continue
        consumer = DEFERRED_POLICY_DOCUMENT_CONSUMERS.get(change.address)
        if consumer is not None and change.action in consumer.actions:
            call_name = _module_call_name(change.address)
            rejection = _deferred_rerender_rejection(
                change,
                consumer,
                agent_source_churned=agent_source_churned,
                module_depends_on=module_dependencies.get(call_name or "", []),
                deferred_reads=deferred_reads,
                structured=structured,
                structured_plan_text=structured_plan_text,
            )
            if rejection is None:
                absorbed["deferred-policy-rerender"].append(change.address)
                continue
            unexpected.append(f"{change.describe()} — {rejection}")
            continue
        normalising = SELF_NORMALISING_POLICY_RESOURCES.get(change.address)
        if normalising is not None and change.action in normalising.actions:
            rejection = _policy_normalisation_rejection(
                change,
                normalising,
                structured=structured,
                structured_plan_text=structured_plan_text,
            )
            if rejection is None:
                absorbed["arn-normalisation"].append(change.address)
                continue
            unexpected.append(f"{change.describe()} — {rejection}")
            continue
        unexpected.append(change.describe())
    return Classification(
        changed=changed,
        unexpected=sorted(set(unexpected)),
        absorbed={key: addresses for key, addresses in absorbed.items() if addresses},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plan_ndjson_path", help="`terraform plan -json` stream, one message a line"
    )
    parser.add_argument(
        "--structured-plan",
        default=None,
        help=(
            "A `terraform show -json <planfile>` document. Required to absorb a deferred "
            "policy-document re-render: without it those changes are reported as unexpected."
        ),
    )
    args = parser.parse_args(argv)

    try:
        text = Path(args.plan_ndjson_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"classify_post_apply_drift: cannot read {args.plan_ndjson_path}: {exc}",
            file=sys.stderr,
        )
        return 2
    structured_text: str | None = None
    if args.structured_plan is not None:
        try:
            structured_text = Path(args.structured_plan).read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"classify_post_apply_drift: cannot read {args.structured_plan}: {exc}",
                file=sys.stderr,
            )
            return 2

    result = classify_attributed(text, structured_text)
    # Stdout is one summary line (captured by deploy.sh); per-address detail goes to stderr.
    print(result.summary())
    for key, addresses in result.absorbed.items():
        print(f"classify_post_apply_drift: absorbed by {key} — {ALLOWANCES[key]}:", file=sys.stderr)
        for address in addresses:
            print(f"  - {address}", file=sys.stderr)
    if result.unexpected:
        print(
            f"classify_post_apply_drift: {len(result.unexpected)} pending change(s) absorbed by NO "
            f"allowance ({', '.join(ALLOWANCES)}); the packaging artifacts considered were "
            f"{', '.join(PACKAGING_ARTIFACTS)}:",
            file=sys.stderr,
        )
        for address in result.unexpected:
            print(f"  - {address}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
