#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Consolidated Layer 3 plan-time security baseline gate.

Parses one `terraform plan -json` document and asserts every security-baseline control that is
expressible as a property of the plan. Every control carries a module tag; `--only <module>`
restricts the run to one module's controls.

Controls that cannot be expressed against a plan are recorded in `PLAN_TIME_INEXPRESSIBLE` with
the layer each is deferred to. `--self-check` fails if any control has neither a registered
assertion nor a recorded inexpressible reason.

Usage:
    check_plan_security_baseline.py --plan <plan.json> [--only <module>] [--json]
    check_plan_security_baseline.py --self-check [--json]
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import functools
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# `scripts/` is not a package, so sibling modules are imported via its directory on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hcl_text import (  # type: ignore[import-not-found] # noqa: E402
    blank_comments,
    blank_heredoc_bodies,
    blank_literals,
    same_shape,
)
from run_gates import GATE_DEFER_MARKER  # type: ignore[import-not-found] # noqa: E402

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import plan_fixtures  # noqa: E402

# ---------------------------------------------------------------------------
# Plan loading helpers
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One assertion result for one resource."""

    control: str
    resource: str
    status: str  # "pass" | "fail" | "deferred"
    detail: str


@dataclass
class Plan:
    """A parsed `terraform plan -json` document, indexed for the assertions below."""

    resource_changes: list[dict[str, Any]]
    configuration: dict[str, Any] = field(default_factory=dict)

    def by_type(self, resource_type: str) -> list[dict[str, Any]]:
        return [r for r in self.resource_changes if r.get("type") == resource_type]

    def by_address_contains(self, needle: str) -> list[dict[str, Any]]:
        return [r for r in self.resource_changes if needle in r.get("address", "")]

    @staticmethod
    def after(resource: dict[str, Any]) -> dict[str, Any]:
        return resource.get("change", {}).get("after") or {}

    @staticmethod
    def after_unknown(resource: dict[str, Any]) -> dict[str, Any]:
        """The `change.after_unknown` sibling of `after`.

        Terraform omits from `after` any attribute referencing a same-apply resource and marks it
        `true` here instead.
        """
        return resource.get("change", {}).get("after_unknown") or {}

    def _config_resource_for_address(self, address: str) -> dict[str, Any] | None:
        """Find the `configuration...resources[]` entry for a resource-change address.

        Walks the address's module path through `root_module.module_calls[...]` and matches the
        final `<type>.<name>` segment, ignoring instance keys.
        """
        if not self.configuration:
            return None
        segments = re.sub(r"\[[^\]]*\]", "", address).split(".")
        mod: dict[str, Any] = self.configuration.get("root_module", {})
        i = 0
        while i < len(segments) and segments[i] == "module":
            call_name = segments[i + 1]
            module_calls = mod.get("module_calls") or {}
            if call_name not in module_calls:
                return None
            mod = module_calls[call_name].get("module", {})
            i += 2
        resource_addr = ".".join(segments[i:])
        for r in mod.get("resources", []):
            r_dict: dict[str, Any] = r
            if r_dict.get("address") == resource_addr:
                return r_dict
        return None

    def static_policy_statements_for(
        self, bucket_policy_address: str
    ) -> list[dict[str, Any]] | None:
        """Literal `statement` fields of the policy document a resource's `policy` references.

        Read from `configuration`. Returns `None` if `policy` does not resolve to a
        `data.aws_iam_policy_document` in the same module. Each statement dict uses the plan's key
        names and holds only `constant_value` fields; a field known only by reference is absent.
        """
        res_cfg = self._config_resource_for_address(bucket_policy_address)
        if res_cfg is None:
            return None
        policy_expr = (res_cfg.get("expressions") or {}).get("policy") or {}
        refs = policy_expr.get("references") or []
        doc_ref = next((r for r in refs if r.startswith("data.aws_iam_policy_document.")), None)
        if doc_ref is None:
            return None
        # Strip any `[N]` index and trailing attribute to get the bare block address.
        doc_address = re.sub(r"\[[^\]]*\]", "", doc_ref)
        doc_address = re.sub(r"^(data\.aws_iam_policy_document\.[^.]+).*$", r"\1", doc_address)
        doc_cfg = self._config_resource_in_same_module(bucket_policy_address, doc_address)
        if doc_cfg is None:
            return None
        statements = (doc_cfg.get("expressions") or {}).get("statement") or []
        result = []
        for stmt in statements:
            entry: dict[str, Any] = {}
            for key in ("sid", "effect"):
                if key in stmt and "constant_value" in stmt[key]:
                    entry[key] = stmt[key]["constant_value"]
            if "actions" in stmt and "constant_value" in stmt["actions"]:
                entry["actions"] = stmt["actions"]["constant_value"]
            resources_expr = stmt.get("resources")
            if isinstance(resources_expr, dict):
                if "constant_value" in resources_expr:
                    entry["resources"] = resources_expr["constant_value"]
                else:
                    references = resources_expr.get("references") or []
                    named = {ref for ref in references if isinstance(ref, str) and "." in ref}
                    # Deduplicated reference targets; the size is not a resource-entry count.
                    entry["resource_references"] = sorted(named)
            conditions = stmt.get("condition")
            if isinstance(conditions, list):
                entry["condition"] = [
                    {
                        key: block[key]["constant_value"]
                        for key in ("test", "variable", "values")
                        if isinstance(block.get(key), dict) and "constant_value" in block[key]
                    }
                    for block in conditions
                    if isinstance(block, dict)
                ]
            for principal_key in ("principals", "not_principals"):
                blocks = stmt.get(principal_key)
                if not isinstance(blocks, list):
                    continue
                entry[principal_key] = [
                    {
                        key: block[key]["constant_value"]
                        for key in ("type", "identifiers")
                        if isinstance(block.get(key), dict) and "constant_value" in block[key]
                    }
                    for block in blocks
                    if isinstance(block, dict)
                ]
            result.append(entry)
        return result

    def bucket_policy_bucket_references(self, bucket_policy_address: str) -> list[str] | None:
        """Resource addresses the policy's own `bucket` argument references, without attributes.

        `bucket = aws_s3_bucket.logs.id` yields `["aws_s3_bucket.logs"]`.

        Returns `None` when the resource has no `configuration` entry.
        """
        res_cfg = self._config_resource_for_address(bucket_policy_address)
        if res_cfg is None:
            return None
        refs = ((res_cfg.get("expressions") or {}).get("bucket") or {}).get("references") or []
        return sorted(
            {
                re.sub(r"^([^.]+\.[^.]+).*$", r"\1", re.sub(r"\[[^\]]*\]", "", ref))
                for ref in refs
                if isinstance(ref, str) and "." in ref
            }
        )

    def bucket_names_for_policy(self, bucket_policy_address: str) -> tuple[str, ...]:
        """Literal bucket *names* the policy attaches to, resolved from every plan location.

        Sources: the policy's own literal `bucket` argument, and the `bucket` attribute of each
        `aws_s3_bucket` resource change that argument references. Returns an empty tuple when no
        name is knowable at plan time.
        """
        names: set[str] = set()
        for r in self.resource_changes:
            if re.sub(r"\[[^\]]*\]", "", r.get("address", "")) == re.sub(
                r"\[[^\]]*\]", "", bucket_policy_address
            ):
                own = self.after(r).get("bucket")
                if isinstance(own, str) and own:
                    names.add(own)
        references = self.bucket_policy_bucket_references(bucket_policy_address) or []
        module_prefix = ".".join(bucket_policy_address.split(".")[:-2])
        for reference in references:
            wanted = f"{module_prefix}.{reference}" if module_prefix else reference
            for r in self.resource_changes:
                if re.sub(r"\[[^\]]*\]", "", r.get("address", "")) != wanted:
                    continue
                name = self.after(r).get("bucket")
                if isinstance(name, str) and name:
                    names.add(name)
        return tuple(sorted(names))

    def data_source_statements_for(self, bucket_policy_address: str) -> list[dict[str, Any]] | None:
        """Resolved `statement` values from the `data.aws_iam_policy_document`'s own resource change.

        A second view of the document read by :meth:`static_policy_statements_for`; same-apply
        ARNs under `resources` remain unknown.

        Returns `None` when the referenced data source has no resource change in this plan.
        """
        res_cfg = self._config_resource_for_address(bucket_policy_address)
        if res_cfg is None:
            return None
        policy_expr = (res_cfg.get("expressions") or {}).get("policy") or {}
        refs = policy_expr.get("references") or []
        doc_ref = next((r for r in refs if r.startswith("data.aws_iam_policy_document.")), None)
        if doc_ref is None:
            return None
        doc_address = re.sub(r"\[[^\]]*\]", "", doc_ref)
        doc_address = re.sub(r"^(data\.aws_iam_policy_document\.[^.]+).*$", r"\1", doc_address)
        module_address = ".".join(bucket_policy_address.split(".")[:-2])
        wanted = f"{module_address}.{doc_address}" if module_address else doc_address
        for r in self.resource_changes:
            if re.sub(r"\[[^\]]*\]", "", r.get("address", "")) != wanted:
                continue
            statements = self.after(r).get("statement")
            if isinstance(statements, list):
                return [stmt for stmt in statements if isinstance(stmt, dict)]
            return None
        return None

    def _config_resource_in_same_module(
        self, sibling_address: str, target_relative_address: str
    ) -> dict[str, Any] | None:
        """Resolve `target_relative_address` in the configuration module `sibling_address` is in."""
        if not self.configuration:
            return None
        segments = re.sub(r"\[[^\]]*\]", "", sibling_address).split(".")
        mod: dict[str, Any] = self.configuration.get("root_module", {})
        i = 0
        while i < len(segments) and segments[i] == "module":
            call_name = segments[i + 1]
            module_calls = mod.get("module_calls") or {}
            if call_name not in module_calls:
                return None
            mod = module_calls[call_name].get("module", {})
            i += 2
        for r in mod.get("resources", []):
            r_dict: dict[str, Any] = r
            if r_dict.get("address") == target_relative_address:
                return r_dict
        return None


def is_unknown(value: Any) -> bool:
    """True if a `change.after_unknown` entry marks its counterpart attribute as unknown.

    Handles the two shapes Terraform emits: a bare `true` for a scalar attribute, and a `true`
    nested inside the list-of-blocks shape (`[{"attr": True}]`) for a nested-block attribute.
    """
    if value is True:
        return True
    if isinstance(value, list):
        return any(is_unknown(item) for item in value)
    if isinstance(value, dict):
        return any(is_unknown(item) for item in value.values())
    return False


def unknown_block_attr(after_unknown_blocks: Any, index: int, attr: str) -> bool:
    """True if `attr` is marked unknown on the `index`-th entry of a nested-block attribute.

    `after_unknown_blocks` is the `after_unknown` value for a block-list attribute (e.g.
    `after_unknown.get("server_side_encryption")`), shaped as a list of per-block dicts mirroring
    `after`'s own list-of-blocks shape.
    """
    if not isinstance(after_unknown_blocks, list) or index >= len(after_unknown_blocks):
        return False
    block = after_unknown_blocks[index]
    if not isinstance(block, dict):
        return False
    return is_unknown(block.get(attr))


def load_plan(path: Path) -> Plan:
    data = json.loads(path.read_text(encoding="utf-8"))
    resource_changes = data.get("resource_changes", [])
    if not isinstance(resource_changes, list):
        raise ValueError(f"{path}: 'resource_changes' is not a list")
    configuration = data.get("configuration") or {}
    return Plan(resource_changes=resource_changes, configuration=configuration)


# ---------------------------------------------------------------------------
# Per-control assertion functions.
#
# Each returns a list[Finding], one per resource inspected, and is registered in CONTROLS under
# its control id and module tag. Presence-style checks accept an attribute marked unknown in
# `after_unknown`; checks needing a literal report "deferred" when the literal is unknown.
# ---------------------------------------------------------------------------

MANDATORY_TAGS = {
    "service",
    "environment",
    "owner",
    "cost-center",
    "data-classification",
    "AppName",
}
#: Mandatory tags whose value is fixed, not merely present.
MANDATORY_TAG_VALUES = {"AppName": "crm-agent-eval"}


def check_dynamodb_cmk(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_dynamodb_table"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        sse = (after.get("server_side_encryption") or [{}])[0]
        sse_unknown = after_unknown.get("server_side_encryption")
        arn = sse.get("kms_key_arn")
        arn_deferred = arn is None and unknown_block_attr(sse_unknown, 0, "kms_key_arn")
        ok = bool(sse.get("enabled")) and (bool(arn) or arn_deferred)
        detail = f"server_side_encryption={sse!r}"
        if arn_deferred:
            detail += (
                " (kms_key_arn unknown at plan time -- same-apply CMK reference, resolves at apply)"
            )
        findings.append(
            Finding(
                "dynamodb_cmk",
                r["address"],
                "pass" if ok else "fail",
                detail,
            )
        )
    return findings


def check_dynamodb_pitr(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_dynamodb_table"):
        after = plan.after(r)
        pitr = (after.get("point_in_time_recovery") or [{}])[0]
        ok = bool(pitr.get("enabled"))
        findings.append(
            Finding(
                "dynamodb_pitr",
                r["address"],
                "pass" if ok else "fail",
                f"point_in_time_recovery={pitr!r}",
            )
        )
    return findings


#: Every DynamoDB action, as an IAM action pattern.
_DYNAMODB_ACTION_SPACE = "dynamodb:*"


def _iam_globs_intersect(left: str, right: str) -> bool:
    """Whether two IAM action patterns (`*` and `?` wildcards, case-insensitive) share a match."""
    a, b = left.casefold(), right.casefold()

    @functools.cache
    def meet(i: int, j: int) -> bool:
        if i < len(a) and a[i] == "*":
            return meet(i + 1, j) or (j < len(b) and meet(i, j + 1))
        if j < len(b) and b[j] == "*":
            return meet(i, j + 1) or (i < len(a) and meet(i + 1, j))
        if i == len(a) or j == len(b):
            return i == len(a) and j == len(b)
        return (a[i] == b[j] or "?" in (a[i], b[j])) and meet(i + 1, j + 1)

    return meet(0, 0)


#: Every action name DynamoDB defines for IAM, from the AWS Service Authorization Reference
#: (`servicereference.us-east-1.amazonaws.com/v1/dynamodb/dynamodb.json`).
_DYNAMODB_ACTION_NAMES = frozenset(
    {
        "AssociateTableReplica", "BatchGetItem", "BatchWriteItem", "ConditionCheckItem",
        "CreateBackup", "CreateGlobalTable", "CreateGlobalTableWitness", "CreateTable",
        "CreateTableReplica", "DeleteBackup", "DeleteGlobalTableWitness", "DeleteItem",
        "DeleteResourcePolicy", "DeleteTable", "DeleteTableReplica", "DescribeBackup",
        "DescribeContinuousBackups", "DescribeContributorInsights", "DescribeEndpoints",
        "DescribeExport", "DescribeGlobalTable", "DescribeGlobalTableSettings",
        "DescribeImport", "DescribeKinesisStreamingDestination", "DescribeLimits",
        "DescribeReservedCapacity", "DescribeReservedCapacityOfferings", "DescribeStream",
        "DescribeTable", "DescribeTableReplicaAutoScaling", "DescribeTimeToLive",
        "DisableKinesisStreamingDestination", "EnableKinesisStreamingDestination",
        "ExportTableToPointInTime", "GetAbacStatus", "GetItem", "GetRecords",
        "GetResourcePolicy", "GetShardIterator", "ImportTable", "InjectError",
        "ListBackups", "ListContributorInsights", "ListExports", "ListGlobalTables",
        "ListImports", "ListStreams", "ListTables", "ListTagsOfResource", "PartiQLDelete",
        "PartiQLInsert", "PartiQLSelect", "PartiQLUpdate",
        "PurchaseReservedCapacityOfferings", "PutItem", "PutResourcePolicy", "Query",
        "ReadDataForReplication", "ReplicateSettings", "RestoreTableFromAwsBackup",
        "RestoreTableFromBackup", "RestoreTableToPointInTime", "Scan", "SearchVectors",
        "StartAwsBackupJob", "TagResource", "UntagResource", "UpdateAbacStatus",
        "UpdateContinuousBackups", "UpdateContributorInsights", "UpdateGlobalTable",
        "UpdateGlobalTableSettings", "UpdateGlobalTableVersion", "UpdateItem",
        "UpdateKinesisStreamingDestination", "UpdateTable", "UpdateTableReplicaAutoScaling",
        "UpdateTimeToLive", "WriteDataForReplication",
    }
)  # fmt: skip


def _action_matches_dynamodb(action: str) -> bool:
    """Whether an IAM action pattern names at least one DynamoDB action.

    A literal `dynamodb:` service prefix matches whatever the name. A wildcarded service prefix
    (`*:PutObject`, `dynamo*:GetItem`) matches only if its name matches one of
    :data:`_DYNAMODB_ACTION_NAMES`.
    """
    service, separator, _name = action.partition(":")
    if separator and service.casefold() == "dynamodb":
        return True
    if not _iam_globs_intersect(action, _DYNAMODB_ACTION_SPACE):
        return False
    return any(_iam_globs_intersect(action, f"dynamodb:{name}") for name in _DYNAMODB_ACTION_NAMES)


def _action_covers_dynamodb(action: str) -> bool:
    """Whether an IAM action pattern provably names every DynamoDB action.

    Conservative: true only for a pattern ending in `*` whose remainder matches a prefix of
    `dynamodb:`, so the trailing `*` absorbs any action name.
    """
    pattern = action.casefold()
    if not pattern.endswith("*"):
        return False
    head = pattern.rstrip("*")
    return any(
        _iam_globs_intersect(head, "dynamodb:"[:length]) for length in range(len("dynamodb:") + 1)
    )


def _statement_effect(statement: dict[str, Any]) -> str:
    return str(statement.get("effect") or "Allow")


def _statement_names_dynamodb(statement: dict[str, Any]) -> bool:
    """Whether a statement's `actions` match any DynamoDB action, or its `not_actions` fail to
    exclude every one."""
    if any(_action_matches_dynamodb(str(a)) for a in statement.get("actions") or []):
        return True
    not_actions = [str(a) for a in statement.get("not_actions") or []]
    return bool(not_actions) and not any(_action_covers_dynamodb(a) for a in not_actions)


def _statement_grants_dynamodb(statement: dict[str, Any]) -> bool:
    """An Allow statement that grants at least one DynamoDB action."""
    return _statement_effect(statement) != "Deny" and _statement_names_dynamodb(statement)


def _statement_grants_dynamodb_wildcard(statement: dict[str, Any]) -> list[str]:
    """The wildcard DynamoDB grants in an Allow: wildcard actions, or a `not_actions` grant."""
    if not _statement_grants_dynamodb(statement):
        return []
    wildcards = sorted(
        str(a)
        for a in statement.get("actions") or []
        if _action_matches_dynamodb(str(a)) and any(c in str(a) for c in "*?")
    )
    if statement.get("not_actions"):
        wildcards.append(f"not_actions={list(statement['not_actions'])!r}")
    return wildcards


#: The IAM condition key that scopes an item-level DynamoDB action to the caller's own partition key.
_LEADING_KEYS_CONDITION_VARIABLE = "dynamodb:LeadingKeys"

#: Matrix mechanisms that require a `dynamodb:LeadingKeys` condition on the grant: `leading_keys`
#: (the key pins the caller's identity) and `key_prefix` (prefix only; the handler confines the
#: row). Any other mechanism with a condition fails, as does either of these without one.
_CONDITIONED_MECHANISMS = frozenset({"leading_keys", "key_prefix"})

#: Sid prefix of each per-grant Allow statement in `lambda_action` (then table token and index).
_DYNAMODB_GRANT_SID_PREFIX = "DynamoDb"

#: `module.lambda_action_<suffix>` -> its `function` name in `table_action_role_matrix.json`.
_SINGLETON_FUNCTION_NAMES = {
    "interceptor": "identity-interceptor",
    "pre_token_generation": "pre-token-generation",
    "eval_trigger": "eval-trigger",
    "chat_interceptor": "chat-identity-interceptor",
    "chat_target": "chat-target",
    "user_settings": "user-settings",
    "sweep_control": "sweep-control",
    "sweep_task": "sweep-task",
    "capabilities": "capabilities",
}


def _matrix_table_token(table_name: str) -> str:
    """The sid fragment `lambda_action` builds from a table name.

    Reproduces `replace(title(table_name), "_", "")`: `audit_records` becomes `Auditrecords`.
    """
    return (table_name[:1].upper() + table_name[1:]).replace("_", "")


def _dynamodb_policy_documents(plan: Plan) -> list[tuple[str, list[dict[str, Any]]]]:
    """`(address, statements)` for every `aws_iam_policy_document` carrying a DynamoDB action.

    Read from the data source's own plan entry, whose `sid`, `effect`, `actions` and `condition`
    are known at plan time.
    """
    documents: list[tuple[str, list[dict[str, Any]]]] = []
    for resource in plan.by_type("aws_iam_policy_document"):
        statements = [
            statement
            for statement in (plan.after(resource).get("statement") or [])
            if any(_action_matches_dynamodb(str(a)) for a in statement.get("actions") or [])
            or _statement_grants_dynamodb(statement)
        ]
        if statements:
            documents.append((resource["address"], statements))
    return documents


def _managed_dynamodb_modules(plan: Plan) -> set[str]:
    """Module addresses rendering their DynamoDB document as managed `aws_iam_policy.dynamodb[...]`.

    One document per module, however many effect-split policies it renders.
    """
    return {
        str(r.get("module_address") or "")
        for r in plan.by_type("aws_iam_policy")
        if re.search(r"\.aws_iam_policy\.dynamodb\[", str(r.get("address") or ""))
    }


#: `module.lambda_action_<suffix>` singletons the committed fixture still carries and source has
#: removed, as `(matrix function name, reason)`. Their DynamoDB documents are excused from
#: `check_dynamodb_leading_keys_isolation`, which asserts each is absent from source and the matrix.
FIXTURE_STALE_SINGLETONS: dict[str, tuple[str, str]] = {}


def _lambda_action_function_name(address: str) -> str | None:
    """The matrix `function` name for a `module.lambda_action*` resource address."""
    keyed = re.match(r'module\.lambda_action\["([^"]+)"\]\.', address)
    if keyed is not None:
        return keyed.group(1)
    singleton = re.match(r"module\.lambda_action_([a-z_]+)\.", address)
    if singleton is not None:
        suffix = singleton.group(1)
        stale = FIXTURE_STALE_SINGLETONS.get(suffix)
        return _SINGLETON_FUNCTION_NAMES.get(suffix) or (stale[0] if stale else None)
    return None


def _root_declares_singleton(suffix: str) -> bool:
    """Whether any root `infra/*.tf` file declares `module "lambda_action_<suffix>"`."""
    pattern = re.compile(rf'^module\s+"lambda_action_{re.escape(suffix)}"\s*\{{', re.MULTILINE)
    return any(
        pattern.search(blank_comments(path.read_text(encoding="utf-8")))
        for path in sorted(INFRA_DIR.glob("*.tf"))
    )


#: IAM resource types that attach or govern role policies this gate has no reader for.
_UNREAD_POLICY_ATTACHMENT_TYPES = (
    "aws_iam_policy_attachment",
    "aws_iam_role_policies_exclusive",
    "aws_iam_role_policy_attachments_exclusive",
)


#: `aws_iam_policy_document` arguments that merge other documents in, whose statements the gate
#: does not read.
_MERGED_DOCUMENT_ARGUMENTS = (
    "source_policy_documents",
    "override_policy_documents",
    "source_json",
    "override_json",
)


def _unknown_dynamodb_action_names(statement: dict[str, Any]) -> list[str]:
    """The literal `dynamodb:<Name>` actions of an Allow whose Name is not in
    :data:`_DYNAMODB_ACTION_NAMES` (case-insensitive); wildcard patterns are not judged here."""
    if _statement_effect(statement) == "Deny":
        return []
    known = {name.casefold() for name in _DYNAMODB_ACTION_NAMES}
    unknown = []
    for action in (str(a) for a in statement.get("actions") or []):
        service, separator, name = action.partition(":")
        literal = separator and not any(c in name for c in "*?")
        if literal and service.casefold() == "dynamodb" and name.casefold() not in known:
            unknown.append(action)
    return sorted(unknown)


def _config_module(plan: Plan, path: Sequence[str]) -> dict[str, Any] | None:
    """The `configuration` module at `path` (module call names from the root), or None."""
    mod: dict[str, Any] = plan.configuration.get("root_module", {})
    for call in path:
        calls = mod.get("module_calls") or {}
        if call not in calls:
            return None
        mod = calls[call].get("module", {})
    return mod


def _module_path(address: str) -> list[str]:
    """`module.a["k"].module.b.aws_x.y` -> `["a", "b"]`."""
    return re.findall(r"module\.([\w-]+)", re.sub(r"\[[^\]]*\]", "", address))


def _policy_arn_origin(
    plan: Plan, path: list[str], expression: dict[str, Any], depth: int = 0
) -> str | None:
    """The `aws_iam_policy` address an ARN expression resolves to, through `var.*` and module
    outputs; `constant:<value>` for a literal; None when it resolves to neither."""
    if "constant_value" in expression:
        return f"constant:{expression['constant_value']}"
    if depth > 8:
        return None
    for reference in expression.get("references") or []:
        policy = re.match(r"aws_iam_policy\.([\w-]+)", reference)
        if policy is not None:
            prefix = "".join(f"module.{call}." for call in path)
            return f"{prefix}aws_iam_policy.{policy.group(1)}"
        variable = re.match(r"var\.([\w-]+)$", reference)
        if variable is not None and path:
            parent = _config_module(plan, path[:-1]) or {}
            call = (parent.get("module_calls") or {}).get(path[-1]) or {}
            inner = (call.get("expressions") or {}).get(variable.group(1))
            if isinstance(inner, dict):
                return _policy_arn_origin(plan, path[:-1], inner, depth + 1)
        output = re.match(r"module\.([\w-]+)\.([\w-]+)$", reference)
        if output is not None:
            child = [*path, output.group(1)]
            outputs = (_config_module(plan, child) or {}).get("outputs") or {}
            inner = (outputs.get(output.group(2)) or {}).get("expression")
            if isinstance(inner, dict):
                return _policy_arn_origin(plan, child, inner, depth + 1)
    return None


def _json_policy_grants_dynamodb(policy: str) -> list[str]:
    """The `Sid`s (or indexes) of the DynamoDB-granting Allow statements in a JSON policy.

    Raises:
        ValueError: The policy is not a JSON object whose `Statement` is an object or a list of
            objects.
    """
    document = json.loads(policy)
    if not isinstance(document, dict) or "Statement" not in document:
        raise ValueError("not a policy document with a Statement")
    statements = document["Statement"]
    statements = [statements] if isinstance(statements, dict) else statements
    if not isinstance(statements, list) or not all(isinstance(s, dict) for s in statements):
        raise ValueError("Statement is not an object or a list of objects")
    granting = []
    for index, statement in enumerate(statements):
        actions, not_actions = (
            [statement.get(key)] if isinstance(statement.get(key), str) else statement.get(key)
            for key in ("Action", "NotAction")
        )
        shaped = {"effect": statement.get("Effect"), "actions": actions, "not_actions": not_actions}
        if _statement_grants_dynamodb(shaped):
            granting.append(str(statement.get("Sid") or f"Statement[{index}]"))
    return granting


def _policy_source_findings(plan: Plan) -> list[Finding]:
    """One failing row per role-attached IAM policy source whose DynamoDB grants this gate cannot
    read, plus a coverage row. User and group policies are not examined.

    A role or managed policy is read when its `policy` references a `data.aws_iam_policy_document`
    (judged by the statement checks) or is plan-time JSON holding no DynamoDB Allow. An attachment
    is read when its `policy_arn` resolves to an `aws_iam_policy` in the plan. Any other form, an
    inline `jsonencode` unknown at plan time, a literal managed-policy ARN, a role's own
    `inline_policy`/`managed_policy_arns`, an attachment type in
    :data:`_UNREAD_POLICY_ATTACHMENT_TYPES`, or an `aws_iam_policy_document` setting any of
    :data:`_MERGED_DOCUMENT_ARGUMENTS` (set, unknown at plan time, or written in configuration),
    fails. A plan with no policy source yields no row; the document coverage row fails it.
    """
    control = "dynamodb_no_wildcard_action"
    unread: list[Finding] = []
    planned_policies = {
        re.sub(r"\[[^\]]*\]", "", str(r.get("address"))) for r in plan.by_type("aws_iam_policy")
    }
    for resource_type in ("aws_iam_role_policy", "aws_iam_policy"):
        for resource in plan.by_type(resource_type):
            address = str(resource.get("address"))
            config = plan._config_resource_for_address(address) or {}
            expression = (config.get("expressions") or {}).get("policy") or {}
            references = expression.get("references") or []
            if any(r.startswith("data.aws_iam_policy_document.") for r in references):
                continue
            policy = plan.after(resource).get("policy")
            if not isinstance(policy, str):
                problem = (
                    "its policy is neither a data.aws_iam_policy_document reference nor known at "
                    "plan time (an inline jsonencode over same-apply values), so its statements "
                    "cannot be read"
                )
            else:
                try:
                    granting = _json_policy_grants_dynamodb(policy)
                except ValueError as exc:
                    problem = f"its plan-time policy is not a readable policy document ({exc})"
                else:
                    if not granting:
                        continue
                    problem = (
                        f"its inline JSON policy grants DynamoDB in {granting}, outside every "
                        "document the statement checks judge"
                    )
            unread.append(Finding(control, address, "fail", problem))
    for resource in plan.by_type("aws_iam_role_policy_attachment"):
        address = str(resource.get("address"))
        config = plan._config_resource_for_address(address) or {}
        expression = (config.get("expressions") or {}).get("policy_arn") or {}
        origin = _policy_arn_origin(plan, _module_path(address), expression)
        if origin is not None and origin in planned_policies:
            continue
        unread.append(
            Finding(
                control,
                address,
                "fail",
                f"its policy_arn resolves to {origin!r}, not an aws_iam_policy in this plan, so "
                "the managed policy's statements cannot be read",
            )
        )
    for resource_type in _UNREAD_POLICY_ATTACHMENT_TYPES:
        for resource in plan.by_type(resource_type):
            unread.append(
                Finding(
                    control,
                    str(resource.get("address")),
                    "fail",
                    f"{resource_type} attaches policies this gate has no reader for",
                )
            )
    for resource in plan.by_type("aws_iam_role"):
        address = str(resource.get("address"))
        expressions = (plan._config_resource_for_address(address) or {}).get("expressions") or {}
        for key in ("inline_policy", "managed_policy_arns"):
            if key in expressions:
                unread.append(
                    Finding(
                        control,
                        address,
                        "fail",
                        f"the role sets {key}, a policy source this gate has no reader for",
                    )
                )
    for resource in plan.by_type("aws_iam_policy_document"):
        address = str(resource.get("address"))
        after, after_unknown = plan.after(resource), plan.after_unknown(resource)
        expressions = (plan._config_resource_for_address(address) or {}).get("expressions") or {}
        merged = [
            key
            for key in _MERGED_DOCUMENT_ARGUMENTS
            if after.get(key) or is_unknown(after_unknown.get(key)) or key in expressions
        ]
        if merged:
            unread.append(
                Finding(
                    control,
                    address,
                    "fail",
                    f"the document sets {merged}, whose merged statements the statement checks "
                    "do not read",
                )
            )
    examined = sum(
        len(plan.by_type(t))
        for t in (
            "aws_iam_policy_document",
            "aws_iam_role_policy",
            "aws_iam_policy",
            "aws_iam_role_policy_attachment",
            "aws_iam_role",
            *_UNREAD_POLICY_ATTACHMENT_TYPES,
        )
    )
    if not examined and not unread:
        return []
    return [
        *unread,
        Finding(
            control,
            "(policy sources) every IAM policy source is read",
            "fail" if unread else "pass",
            f"{len(unread)} unread row(s) over {examined} IAM resource(s); every aws_iam_role_policy, "
            "aws_iam_policy and "
            "aws_iam_role_policy_attachment must resolve to a policy document or a planned "
            "aws_iam_policy",
        ),
    ]


def check_dynamodb_no_wildcard_action(plan: Plan) -> list[Finding]:
    """No `dynamodb:*` and no bare `*` in any DynamoDB **grant**.

    Reads `data.aws_iam_policy_document` `statement[*].actions`. A wildcard under `Deny` passes,
    since it restricts rather than grants. Also asserts one grant document per inline dynamodb
    role policy.
    """
    findings: list[Finding] = []
    documents = _dynamodb_policy_documents(plan)
    for address, statements in documents:
        for index, statement in enumerate(statements):
            actions = list(statement.get("actions") or [])
            wildcards = _statement_grants_dynamodb_wildcard(statement)
            effect = statement.get("effect")
            subject = f"{address} statement[{index}] {statement.get('sid')!r}"
            if effect == "Deny":
                findings.append(
                    Finding(
                        "dynamodb_no_wildcard_action",
                        subject,
                        "pass",
                        f"Deny, so a wildcard here RESTRICTS rather than grants (actions={actions!r}). "
                        "Its absence would be the defect: an explicit Deny survives a later additive "
                        "Allow from any policy source, which deny-by-omission does not.",
                    )
                )
                continue
            findings.append(
                Finding(
                    "dynamodb_no_wildcard_action",
                    subject,
                    "fail" if wildcards else "pass",
                    f"effect={effect!r} actions={actions!r}"
                    + (f" -- wildcard GRANT: {wildcards!r}" if wildcards else ""),
                )
            )
            unknown = _unknown_dynamodb_action_names(statement)
            if unknown:
                findings.append(
                    Finding(
                        "dynamodb_no_wildcard_action",
                        f"{subject} action names",
                        "fail",
                        f"grants {unknown}, which DynamoDB does not define as IAM actions, so the "
                        "grant authorizes nothing and the access it was written for is unchecked",
                    )
                )

    role_policies = [
        r
        for r in plan.by_address_contains(".aws_iam_role_policy.dynamodb")
        if "dynamodb_kms" not in r["address"]
    ]
    managed_modules = _managed_dynamodb_modules(plan)
    findings.append(
        Finding(
            "dynamodb_no_wildcard_action",
            "(coverage) one grant document per dynamodb role policy",
            "pass"
            if documents and len(documents) == len(role_policies) + len(managed_modules)
            else "fail",
            f"documents={len(documents)} inline dynamodb role policies={len(role_policies)} "
            f"modules attaching it as managed policies={len(managed_modules)}; a "
            "mismatch means either a role policy renders from something this control does not read, "
            "or the documents were found while the policies were not -- in both directions the "
            "statements above are no longer a statement about every grant in the plan.",
        )
    )
    findings.extend(_policy_source_findings(plan))
    return findings


#: `<function>:<table>:<action>` grants the committed fixture still carries and source has since
#: removed, with the reason. `check_dynamodb_leading_keys_isolation` compares the plan less these
#: against the matrix, and separately asserts that neither the matrix row nor `infra/main.tf`'s
#: `tool_dynamodb_grants` still names the action.
FIXTURE_STALE_GRANTS: dict[str, str] = {}


#: `<function>:<table>:<action>` grants source and the matrix carry that the committed fixture
#: predates, as `(reason, root local holding the grant)`. Excused from the matrix-union equality
#: while absent from the plan; the local must still name the action.
FIXTURE_PENDING_GRANTS: dict[str, tuple[str, str]] = {}


#: `<function>:<table>` grants whose `dynamodb:LeadingKeys` condition source carries and the committed
#: fixture predates, as `(reason, root local holding the grant)`. While the plan lacks the condition,
#: the matrix's `leading_keys` mechanism is excused; the local must still carry the condition.
FIXTURE_PENDING_CONDITIONS: dict[str, tuple[str, str]] = {}


def _root_local_block(local_name: str) -> str | None:
    """The comment-blanked text of a two-space-indented `local_name = [ ... ]` block in
    `infra/main.tf`, or None."""
    source = blank_comments((INFRA_DIR / "main.tf").read_text(encoding="utf-8"))
    block = re.search(rf"(?ms)^  {re.escape(local_name)}\s*=\s*\[\n(.*?)^  \]$", source)
    return block.group(1) if block else None


def _root_local_entries(local_name: str) -> list[str] | None:
    """Each four-space-indented `{ ... }` element of `local_name`'s list, comment-blanked, or None."""
    block = _root_local_block(local_name)
    if block is None:
        return None
    return re.findall(r"(?ms)^    \{\n(.*?)^    \},?$", block)


def _source_tool_grant_actions(function: str, table: str) -> list[str] | None:
    """The actions `infra/main.tf`'s `tool_dynamodb_grants` gives `function` on `table`, or None."""
    source = (INFRA_DIR / "main.tf").read_text(encoding="utf-8")
    grants = re.search(r"(?ms)^  tool_dynamodb_grants = \{\n(.*?)^  \}$", source)
    if grants is None:
        return None
    block = re.search(rf"(?ms)^    {re.escape(function)} = \[\n(.*?)^    \]$", grants.group(1))
    if block is None:
        return None
    row = re.search(
        rf'\{{\s*table\s*=\s*"{re.escape(table)}",\s*actions\s*=\s*\[([^\]]*)\]', block.group(1)
    )
    return re.findall(r'"([^"]+)"', row.group(1)) if row else None


def check_dynamodb_leading_keys_isolation(plan: Plan) -> list[Finding]:
    """Every grant's `LeadingKeys` disposition matches `table_action_role_matrix.json` exactly.

    The matrix says, per (function, table), whether isolation is `leading_keys` or in application
    code; a pair may span several rows, split by action. Each statement is judged by the one row
    whose actions cover it, and fails when no row or more than one does. A condition where that row
    does not expect one fails, as does a missing one where it does. Granted actions must match the
    matrix. The table is resolved from the sid
    (`DynamoDb<Table><index>`) and the function from the module address; an unresolved one fails.
    """
    try:
        matrix = _load_table_action_role_matrix()
    except Exception as exc:  # pragma: no cover - defensive
        return [
            Finding(
                "dynamodb_leading_keys_isolation",
                "(matrix load)",
                "fail",
                f"could not load table_action_role_matrix.json: {exc!r}",
            )
        ]
    tables: dict[str, Any] = matrix.get("tables", {})
    token_to_table = {_matrix_table_token(name): name for name in tables}
    rows_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in matrix.get("grants", []):
        rows_by_pair.setdefault((row.get("function", ""), row.get("table", "")), []).append(row)

    findings: list[Finding] = []
    functions_seen: set[str] = set()
    asserted = 0
    # A table's grant may span several statements: each must be a subset of the matrix actions,
    # and their union must equal it.
    granted_union_by_pair: dict[tuple[str, str], set[str]] = {}
    stale_seen: set[str] = set()
    stale_singletons_seen: set[str] = set()
    stale_functions = {
        function: suffix for suffix, (function, _r) in FIXTURE_STALE_SINGLETONS.items()
    }
    pending_conditions_seen: set[str] = set()
    zero_grant = {str(f) for f in matrix.get("functions_without_dynamodb_grants", [])}

    for address, statements in _dynamodb_policy_documents(plan):
        function = _lambda_action_function_name(address)
        if function is None:
            findings.append(
                Finding(
                    "dynamodb_leading_keys_isolation",
                    address,
                    "fail",
                    "the document's address resolves to no matrix `function`, so none of its grants "
                    "can be compared with the matrix. Either the module was renamed or a new "
                    "singleton needs an entry in _SINGLETON_FUNCTION_NAMES.",
                )
            )
            continue
        if function in stale_functions:
            stale_singletons_seen.add(function)
            continue
        if function in zero_grant:
            allows = sorted(
                str(s.get("sid") or "(no sid)") for s in statements if _statement_grants_dynamodb(s)
            )
            findings.append(
                Finding(
                    "dynamodb_leading_keys_isolation",
                    f"(zero-grant) {function}",
                    "fail" if allows else "pass",
                    "declared in the matrix's functions_without_dynamodb_grants, so its document "
                    f"may hold only Deny statements; DynamoDB Allow statements found: {allows}",
                )
            )
            continue
        functions_seen.add(function)
        for statement in statements:
            sid = str(statement.get("sid") or "")
            if _statement_effect(statement) == "Deny":
                continue
            if not sid.startswith(_DYNAMODB_GRANT_SID_PREFIX):
                findings.append(
                    Finding(
                        "dynamodb_leading_keys_isolation",
                        f"{function} -> {sid or '(no sid)'}",
                        "fail",
                        f"an Allow statement whose sid is not a `{_DYNAMODB_GRANT_SID_PREFIX}<Table>` "
                        f"grant names DynamoDB (actions={statement.get('actions')!r}, "
                        f"not_actions={statement.get('not_actions')!r}), so its table cannot be "
                        "resolved and it is compared with no matrix row.",
                    )
                )
                continue
            token = re.sub(r"\d+$", "", sid[len(_DYNAMODB_GRANT_SID_PREFIX) :])
            table = token_to_table.get(token)
            subject = f"{function} -> {sid}"
            if table is None:
                findings.append(
                    Finding(
                        "dynamodb_leading_keys_isolation",
                        subject,
                        "fail",
                        f"sid token {token!r} names no table in the matrix "
                        f"(known: {sorted(token_to_table)}). A renamed table leaves this grant "
                        "compared against nothing.",
                    )
                )
                continue
            rows = rows_by_pair.get((function, table))
            if not rows:
                findings.append(
                    Finding(
                        "dynamodb_leading_keys_isolation",
                        subject,
                        "fail",
                        f"the plan grants {function!r} access to {table!r} and the matrix carries no "
                        "row for that pair, so its isolation mechanism is undocumented.",
                    )
                )
                continue
            has_leading_keys = any(
                condition.get("variable") == _LEADING_KEYS_CONDITION_VARIABLE
                for condition in (statement.get("condition") or [])
            )
            asserted += 1
            planned_actions = set(statement.get("actions") or [])
            lagging = {
                a for a in planned_actions if f"{function}:{table}:{a}" in FIXTURE_STALE_GRANTS
            }
            stale_seen.update(f"{function}:{table}:{a}" for a in lagging)
            granted = planned_actions - lagging
            documented = {action for row in rows for action in row.get("actions", [])}
            granted_union_by_pair.setdefault((function, table), set()).update(granted)
            covering = [row for row in rows if granted <= set(row.get("actions", []))]
            judged = covering if len(covering) == 1 else rows
            expects_leading_keys = any(
                row.get("isolation_mechanism") in _CONDITIONED_MECHANISMS for row in judged
            )
            prefix_only = all(row.get("isolation_mechanism") == "key_prefix" for row in judged)
            mechanisms = sorted({str(row.get("isolation_mechanism")) for row in judged})
            problems: list[str] = []
            pending_condition = (
                f"{function}:{table}" in FIXTURE_PENDING_CONDITIONS
                and expects_leading_keys
                and not has_leading_keys
            )
            if pending_condition:
                pending_conditions_seen.add(f"{function}:{table}")
            if has_leading_keys != expects_leading_keys and not pending_condition:
                problems.append(
                    f"LeadingKeys present={has_leading_keys} but the matrix mechanism is "
                    f"{mechanisms} -- "
                    + (
                        "a condition on a table whose partition key is a synthetic record id can "
                        "never match, so it reads as isolation and enforces nothing"
                        if has_leading_keys
                        else "the matrix claims a key-prefix condition on this grant and the grant "
                        "carries no condition"
                        if prefix_only
                        else "the matrix claims IAM enforces rep isolation on this grant and the "
                        "grant carries no condition"
                    )
                )
            if (
                has_leading_keys
                and not prefix_only
                and not tables.get(table, {}).get("leading_keys_can_enforce_rep_isolation")
            ):
                problems.append(
                    f"table {table!r} is marked leading_keys_can_enforce_rep_isolation=false"
                )
            if not granted.issubset(documented):
                problems.append(
                    f"granted actions {sorted(granted)} include actions the matrix does not "
                    f"document {sorted(documented)} (a widened grant)"
                )
            elif len(covering) != 1:
                problems.append(
                    f"granted actions {sorted(granted)} are covered by {len(covering)} of the "
                    f"pair's {len(rows)} matrix rows, so no single row's mechanism judges this "
                    "statement"
                )
            findings.append(
                Finding(
                    "dynamodb_leading_keys_isolation",
                    subject,
                    "fail" if problems else "pass",
                    "; ".join(problems)
                    if problems
                    else f"table={table!r} mechanism={mechanisms} leading_keys={has_leading_keys} "
                    f"actions={sorted(granted)} -- "
                    + (
                        "condition pending in source (FIXTURE_PENDING_CONDITIONS); actions match"
                        if pending_condition
                        else "matches the matrix in both directions"
                    ),
                )
            )

    # The union of granted actions across a table's statements must equal the matrix's set.
    for (fn, tbl), union in sorted(granted_union_by_pair.items()):
        documented = {a for row in rows_by_pair.get((fn, tbl), []) for a in row.get("actions", [])}
        pending = {
            entry.split(":", 2)[2]
            for entry in FIXTURE_PENDING_GRANTS
            if entry.startswith(f"{fn}:{tbl}:") and entry.split(":", 2)[2] not in union
        }
        union = union | pending
        findings.append(
            Finding(
                "dynamodb_leading_keys_isolation",
                f"{fn} -> {tbl} (action coverage)",
                "pass" if union == documented else "fail",
                f"union of granted actions {sorted(union)} "
                f"{'==' if union == documented else '!='} the matrix's {sorted(documented)}",
            )
        )

    for entry, (reason, local_name) in sorted(FIXTURE_PENDING_CONDITIONS.items()):
        fn, tbl = entry.split(":", 1)
        pair_mechanisms = {
            str(row.get("isolation_mechanism")) for row in rows_by_pair.get((fn, tbl), [])
        }
        entries = _root_local_entries(local_name)
        unconditioned = [
            index
            for index, entry_text in enumerate(entries or [])
            if not re.search(
                rf'leading_key_condition\s*=\s*\{{[^}}]*"{re.escape(_LEADING_KEYS_CONDITION_VARIABLE)}"',
                entry_text,
            )
        ]
        if not entries or unconditioned or not pair_mechanisms & _CONDITIONED_MECHANISMS:
            status, detail = (
                "fail",
                f"needs a matrix mechanism in {sorted(_CONDITIONED_MECHANISMS)} (got "
                f"{sorted(pair_mechanisms)}) and a {_LEADING_KEYS_CONDITION_VARIABLE} "
                f"`leading_key_condition` on EVERY grant in `local.{local_name}` (grants found="
                f"{len(entries or [])}, unconditioned indexes={unconditioned}), so the pending "
                "condition it declares is not real",
            )
        elif entry in pending_conditions_seen:
            status, detail = (
                "pass",
                f"`local.{local_name}` carries the condition; the fixture predates it "
                f"({reason[:40]}…)",
            )
        else:
            status, detail = (
                "deferred",
                "FIXTURE_PENDING_CONDITIONS entry is not needed by THIS plan -- its grants already "
                "carry the condition. Remove the entry once no plan the gate is run against needs it.",
            )
        findings.append(
            Finding(
                "dynamodb_leading_keys_isolation",
                f"(declared-pending-condition) {entry}",
                status,
                detail,
            )
        )

    for entry, (reason, local_name) in sorted(FIXTURE_PENDING_GRANTS.items()):
        fn, tbl, action = entry.split(":", 2)
        documented = {a for row in rows_by_pair.get((fn, tbl), []) for a in row.get("actions", [])}
        block = _root_local_block(local_name)
        if block is None or action not in documented or f'"{action}"' not in block:
            status, detail = (
                "fail",
                f"not granted by both the matrix ({sorted(documented)}) and `local.{local_name}` "
                f"(found={block is not None}), so the pending grant it declares is not real",
            )
        elif action in granted_union_by_pair.get((fn, tbl), set()):
            status, detail = (
                "deferred",
                "FIXTURE_PENDING_GRANTS entry is not needed by THIS plan -- it already grants the "
                "action. Remove the entry once no plan the gate is run against needs it.",
            )
        else:
            status, detail = (
                "pass",
                f"granted by the matrix and `local.{local_name}`; the fixture predates it "
                f"({reason[:40]}…)",
            )
        findings.append(
            Finding(
                "dynamodb_leading_keys_isolation", f"(declared-pending) {entry}", status, detail
            )
        )

    for entry, reason in sorted(FIXTURE_STALE_GRANTS.items()):
        fn, tbl, action = entry.split(":", 2)
        documented = {a for row in rows_by_pair.get((fn, tbl), []) for a in row.get("actions", [])}
        source_actions = _source_tool_grant_actions(fn, tbl)
        if source_actions is None or not rows_by_pair.get((fn, tbl)):
            status, detail = (
                "fail",
                "names a (function, table) pair absent from main.tf's tool_dynamodb_grants or the "
                "matrix, so the removal it declares cannot be checked",
            )
        elif action in documented or action in source_actions:
            status, detail = (
                "fail",
                f"source still grants it (matrix={sorted(documented)}, main.tf={source_actions}); "
                "the declaration would hide a live grant",
            )
        elif entry in stale_seen:
            status, detail = (
                "pass",
                f"removed from main.tf and the matrix; the fixture still carries it ({reason[:40]}…)",
            )
        else:
            status, detail = (
                "deferred",
                "FIXTURE_STALE_GRANTS entry is not needed by THIS plan -- the action is already "
                "absent from it. Remove the entry once no plan the gate is run against needs it.",
            )
        findings.append(
            Finding("dynamodb_leading_keys_isolation", f"(declared-stale) {entry}", status, detail)
        )

    matrix_functions = {
        str(row.get("function")) for row in matrix.get("grants", []) if row.get("function")
    }
    for function, suffix in sorted(stale_functions.items()):
        reason = FIXTURE_STALE_SINGLETONS[suffix][1]
        if function in matrix_functions or _root_declares_singleton(suffix):
            status, detail = (
                "fail",
                f"source still carries it (matrix row: {function in matrix_functions}, "
                f'module "lambda_action_{suffix}": {_root_declares_singleton(suffix)}); the '
                "declaration would hide a live function",
            )
        elif function in stale_singletons_seen:
            status, detail = (
                "pass",
                f"removed from source and the matrix; the fixture still carries it ({reason[:40]}…)",
            )
        else:
            status, detail = (
                "deferred",
                "FIXTURE_STALE_SINGLETONS entry is not needed by THIS plan -- the function is "
                "already absent from it. Remove the entry once no plan the gate is run against "
                "needs it.",
            )
        findings.append(
            Finding(
                "dynamodb_leading_keys_isolation", f"(declared-stale) {function}", status, detail
            )
        )
    declared_absent = _fixture_absent_functions() & matrix_functions
    covered = functions_seen <= matrix_functions and (
        matrix_functions - functions_seen <= declared_absent
    )
    findings.append(
        Finding(
            "dynamodb_leading_keys_isolation",
            "(coverage) the matrix's functions are the plan's functions, less declared fixture "
            "absences",
            "pass" if covered else "fail",
            f"declared absent from the fixture (FIXTURE_ABSENT_INSTANCES): {sorted(declared_absent)}; "
            f"in the matrix and not the plan: {sorted(matrix_functions - functions_seen)}; in the "
            f"plan and not the matrix: {sorted(functions_seen - matrix_functions)}. This is what "
            "makes the singleton name map falsifiable: a rename on either side lands here instead of "
            "silently matching no row.",
        )
    )
    findings.append(
        Finding(
            "dynamodb_leading_keys_isolation",
            "(coverage) grants compared",
            "pass" if asserted else "fail",
            f"{asserted} grant statement(s) compared with the matrix; zero would mean this control "
            "reported nothing while every finding above passed.",
        )
    )
    return findings


def check_lambda_role_uniqueness(plan: Plan) -> list[Finding]:
    roles = plan.by_type("aws_iam_role")
    lambda_roles = [
        r for r in roles if ".lambda_action" in r["address"] or "lambda_action_" in r["address"]
    ]
    seen: dict[str, str] = {}
    findings = []
    for r in lambda_roles:
        name = plan.after(r).get("name")
        if not isinstance(name, str):
            findings.append(
                Finding(
                    "lambda_role_uniqueness",
                    r["address"],
                    "fail",
                    f"role name is not a string: {name!r}",
                )
            )
            continue
        if name in seen:
            findings.append(
                Finding(
                    "lambda_role_uniqueness",
                    r["address"],
                    "fail",
                    f"role name {name!r} shared with {seen[name]!r}",
                )
            )
        else:
            seen[name] = r["address"]
            findings.append(
                Finding(
                    "lambda_role_uniqueness",
                    r["address"],
                    "pass",
                    f"role name {name!r} unique so far",
                )
            )
    return findings


def check_lambda_reserved_concurrency_positive(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_lambda_function"):
        after = plan.after(r)
        value = after.get("reserved_concurrent_executions")
        # 0 disables the function entirely, so the value must be positive.
        ok = isinstance(value, (int, float)) and value > 0
        findings.append(
            Finding(
                "lambda_reserved_concurrency_positive",
                r["address"],
                "pass" if ok else "fail",
                f"reserved_concurrent_executions={value!r}",
            )
        )
    return findings


def check_relay_function_url_streams(plan: Plan) -> list[Finding]:
    """The relay's Function URL must be `invoke_mode = RESPONSE_STREAM`, not `BUFFER`.

    `BUFFER` collapses the SSE stream into one buffered response. Does not assert
    `authorization_type`; the CloudFront OAC grant checks require `function_url_auth_type =
    "AWS_IAM"`. Zero Function URLs is a failure.
    """
    urls = plan.by_type("aws_lambda_function_url")
    if not urls:
        return [
            Finding(
                "relay_function_url_streams",
                "aws_lambda_function_url",
                "fail",
                "no aws_lambda_function_url in the plan, so the relay's streaming invoke_mode is "
                "unasserted -- exactly one is expected (the agent relay's)",
            )
        ]
    findings = []
    for r in urls:
        mode = plan.after(r).get("invoke_mode")
        findings.append(
            Finding(
                "relay_function_url_streams",
                r["address"],
                "pass" if mode == "RESPONSE_STREAM" else "fail",
                f"invoke_mode={mode!r}"
                + (
                    ""
                    if mode == "RESPONSE_STREAM"
                    else " -- BUFFER collapses the SSE stream into one buffered response, which is "
                    "the streaming contract is about"
                ),
            )
        )
    return findings


def check_lambda_reserved_concurrency_headroom(plan: Plan) -> list[Finding]:
    total: float = 0
    addresses = []
    for r in plan.by_type("aws_lambda_function"):
        value = plan.after(r).get("reserved_concurrent_executions")
        if isinstance(value, (int, float)):
            total += value
            addresses.append(r["address"])
    ok = total <= 900
    return [
        Finding(
            "lambda_reserved_concurrency_headroom",
            "sum(aws_lambda_function.*)",
            "pass" if ok else "fail",
            f"sum(reserved_concurrent_executions)={total} across {len(addresses)} functions "
            "(ceiling 900, leaving >=100 headroom in the 1,000 account pool)",
        )
    ]


def _load_dynamodb_schema_gsis() -> dict[str, frozenset[str]]:
    """Table name -> the set of GSI names `src/crm/store/schema.py::TABLES` declares."""
    repo_root = Path(__file__).resolve().parents[1]
    schema_path = repo_root / "src" / "crm" / "store" / "schema.py"
    spec = importlib.util.spec_from_file_location("crm_store_schema", schema_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {schema_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["crm_store_schema"] = module
    spec.loader.exec_module(module)
    return {name: frozenset(table.gsis) for name, table in module.TABLES.items()}


def check_dynamodb_gsi_parity(plan: Plan) -> list[Finding]:
    """Every `aws_dynamodb_table`'s declared GSI name set matches `schema.py::TABLES` exactly.

    The table's logical name is the last address segment, not the prefixed `name` attribute.
    """
    findings = []
    try:
        expected = _load_dynamodb_schema_gsis()
    except Exception as exc:  # pragma: no cover - defensive; surfaced as an explicit failure
        return [
            Finding(
                "dynamodb_gsi_parity",
                "(schema.py load)",
                "fail",
                f"could not load src/crm/store/schema.py::TABLES: {exc!r}",
            )
        ]
    for r in plan.by_type("aws_dynamodb_table"):
        table_name = r["address"].rsplit(".", 1)[-1]
        after = plan.after(r)
        actual_gsis = frozenset(g.get("name") for g in (after.get("global_secondary_index") or []))
        expected_gsis = expected.get(table_name)
        if expected_gsis is None:
            findings.append(
                Finding(
                    "dynamodb_gsi_parity",
                    r["address"],
                    "fail",
                    f"table name {table_name!r} not found in schema.py::TABLES "
                    f"(known: {sorted(expected)!r})",
                )
            )
            continue
        ok = actual_gsis == expected_gsis
        findings.append(
            Finding(
                "dynamodb_gsi_parity",
                r["address"],
                "pass" if ok else "fail",
                f"plan_gsis={sorted(actual_gsis)!r} schema.py_gsis={sorted(expected_gsis)!r}",
            )
        )
    return findings


def _load_table_action_role_matrix() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    matrix_path = (
        repo_root / "infra" / "modules" / "lambda_action" / "table_action_role_matrix.json"
    )
    result: dict[str, Any] = json.loads(matrix_path.read_text(encoding="utf-8"))
    return result


def check_lambda_env_kms(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_lambda_function"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        env_vars = after.get("environment") or []
        has_env = bool(env_vars and env_vars[0].get("variables"))
        kms = after.get("kms_key_arn")
        kms_deferred = kms is None and is_unknown(after_unknown.get("kms_key_arn"))
        if not has_env:
            findings.append(
                Finding(
                    "lambda_env_kms",
                    r["address"],
                    "pass",
                    "no environment variables declared",
                )
            )
            continue
        ok = bool(kms) or kms_deferred
        detail = f"kms_key_arn={kms!r}"
        if kms_deferred:
            detail += " (unknown at plan time -- same-apply CMK reference, resolves at apply)"
        findings.append(
            Finding(
                "lambda_env_kms",
                r["address"],
                "pass" if ok else "fail",
                detail,
            )
        )
    return findings


_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::(?P<account>\d{12}):role/(?P<name>[A-Za-z0-9+=,.@_-]+)$")


def _gateway_role_arns(plan: Plan) -> set[str]:
    """Known-literal ARNs of the root-module `aws_iam_role.agentcore_gateway` resource.

    Matched by exact root-module address, so a same-named role in another module is ignored.
    Returns an empty set if the `arn` is unknown, the role is absent, or more than one matches;
    callers fail closed on an empty set.
    """
    arns: set[str] = set()
    matches = [
        r
        for r in plan.by_type("aws_iam_role")
        if r.get("name") == "agentcore_gateway"
        and r.get("address") == "aws_iam_role.agentcore_gateway"
        and not r.get("module_address")
    ]
    if len(matches) != 1:
        return arns
    r = matches[0]
    after_unknown = plan.after_unknown(r)
    arn = plan.after(r).get("arn")
    if isinstance(arn, str) and not is_unknown(after_unknown.get("arn")):
        arns.add(arn)
    return arns


def check_lambda_resource_policy_no_wildcard(plan: Plan) -> list[Finding]:
    """Action Lambdas: resource policy admits only the Gateway target role and API Gateway.

    Every principal must be in the service allow-list, or be one of the sanctioned CloudFront OAC
    grants, or be the Gateway target-role grant (`statement_id = "AllowGatewayTargetRoleInvoke"`).
    For that grant an unknown `principal` passes; a known one must be a wildcard-free, non-root IAM
    role ARN exactly equal to the plan's own `aws_iam_role.agentcore_gateway` ARN, failing closed
    when that ARN cannot be resolved.
    """
    allowed_principals = {
        "apigateway.amazonaws.com",
        "cognito-idp.amazonaws.com",
        "bedrock-agentcore.amazonaws.com",
    }
    gateway_role_statement_id = "AllowGatewayTargetRoleInvoke"
    gateway_role_arns = _gateway_role_arns(plan)
    sanctioned_url_function_names = _sanctioned_oac_url_function_names(plan)
    findings = []
    for r in plan.by_type("aws_lambda_permission"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        principal = after.get("principal")
        statement_id = after.get("statement_id")
        principal_deferred = principal is None and is_unknown(after_unknown.get("principal"))
        is_gateway_statement = statement_id == gateway_role_statement_id

        gateway_role_grant_deferred = principal_deferred and is_gateway_statement

        gateway_role_grant_known = False
        known_reject_reason = ""
        if is_gateway_statement and isinstance(principal, str) and not principal_deferred:
            match = _ROLE_ARN_RE.match(principal)
            if "*" in principal:
                known_reject_reason = "contains a wildcard character"
            elif principal.endswith(":root"):
                known_reject_reason = "is an account-root principal"
            elif match is None:
                known_reject_reason = "is not a syntactically valid IAM role ARN"
            elif not gateway_role_arns:
                known_reject_reason = (
                    "the plan's own aws_iam_role.agentcore_gateway ARN could not be resolved "
                    "(failing closed)"
                )
            elif principal not in gateway_role_arns:
                gateway_account = next(iter(gateway_role_arns)).split(":")[4]
                if match.group("account") != gateway_account:
                    known_reject_reason = "is a cross-account role ARN"
                else:
                    known_reject_reason = "does not match the plan's own gateway role ARN"
            else:
                gateway_role_grant_known = True

        cloudfront_oac_grant = _is_sanctioned_cloudfront_oac_grant(after, after_unknown)
        cloudfront_oac_invoke_function_grant = _is_sanctioned_cloudfront_oac_invoke_function_grant(
            after, after_unknown, sanctioned_url_function_names
        )

        ok = (
            principal in allowed_principals
            or gateway_role_grant_deferred
            or gateway_role_grant_known
            or cloudfront_oac_grant
            or cloudfront_oac_invoke_function_grant
        )
        detail = f"principal={principal!r} statement_id={statement_id!r} allowed={sorted(allowed_principals)!r}"
        if cloudfront_oac_grant:
            detail += (
                " (sanctioned CloudFront OAC grant: lambda:InvokeFunctionUrl with "
                "function_url_auth_type=AWS_IAM and a distribution-scoped source_arn, so the origin "
                "still demands a SigV4 signature)"
            )
        if cloudfront_oac_invoke_function_grant:
            detail += (
                " (sanctioned CloudFront OAC grant: lambda:InvokeFunction, AWS-required alongside "
                "the InvokeFunctionUrl grant, cross-anchored to that paired AWS_IAM grant on the "
                "same function_name and scoped by source_arn)"
            )
        if gateway_role_grant_deferred:
            detail += " (principal unknown at plan time -- same-apply Gateway role ARN reference, identified by statement_id)"
        elif gateway_role_grant_known:
            detail += " (principal is a known literal that exactly matches the plan's own aws_iam_role.agentcore_gateway ARN)"
        elif is_gateway_statement and known_reject_reason:
            detail += f" (gateway-role statement id present, but principal {known_reject_reason})"
        findings.append(
            Finding(
                "lambda_resource_policy_no_wildcard",
                r["address"],
                "pass" if ok else "fail",
                detail,
            )
        )
    return findings


CLOUDFRONT_OAC_STATEMENT_ID = "AllowCloudFrontOacInvokeFunctionUrl"
CLOUDFRONT_OAC_INVOKE_FUNCTION_STATEMENT_ID = "AllowCloudFrontOacInvokeFunction"


def _source_arn_scoped(after: dict[str, Any], after_unknown: dict[str, Any]) -> bool:
    """Whether a permission's `source_arn` names one distribution rather than any-or-none.

    Accepts a wildcard-free literal ARN or an unknown same-apply reference; rejects an absent
    `source_arn` or one containing a wildcard.
    """
    source_arn = after.get("source_arn")
    if not source_arn and is_unknown(after_unknown.get("source_arn")):
        return True
    return isinstance(source_arn, str) and bool(source_arn) and "*" not in source_arn


def _is_sanctioned_cloudfront_oac_grant(
    after: dict[str, Any], after_unknown: dict[str, Any]
) -> bool:
    """Whether this is the CloudFront-to-relay Function URL grant, exempted on all conditions.

    Requires the exact statement id, `lambda:InvokeFunctionUrl`, `function_url_auth_type =
    "AWS_IAM"`, the `cloudfront.amazonaws.com` principal, and a distribution-scoped `source_arn`.
    """
    return (
        after.get("statement_id") == CLOUDFRONT_OAC_STATEMENT_ID
        and after.get("action") == "lambda:InvokeFunctionUrl"
        and after.get("function_url_auth_type") == "AWS_IAM"
        and after.get("principal") == "cloudfront.amazonaws.com"
        and _source_arn_scoped(after, after_unknown)
    )


def _sanctioned_oac_url_function_names(plan: Plan) -> set[str]:
    """`function_name`s that carry a sanctioned CloudFront OAC `InvokeFunctionUrl` grant.

    The anchor set for the paired `InvokeFunction` exemption. An unknown `function_name`
    contributes nothing.
    """
    names: set[str] = set()
    for r in plan.by_type("aws_lambda_permission"):
        after = plan.after(r)
        if _is_sanctioned_cloudfront_oac_grant(after, plan.after_unknown(r)):
            fn = after.get("function_name")
            if isinstance(fn, str) and fn:
                names.add(fn)
    return names


def _is_sanctioned_cloudfront_oac_invoke_function_grant(
    after: dict[str, Any],
    after_unknown: dict[str, Any],
    sanctioned_url_function_names: set[str],
) -> bool:
    """Whether this is the second AWS-required CloudFront OAC grant on the relay Function URL.

    A CloudFront OAC needs `lambda:InvokeFunction` alongside `lambda:InvokeFunctionUrl`. This grant
    is sanctioned only on a `function_name` in `sanctioned_url_function_names`, with the exact
    statement id, the CloudFront principal, and a distribution-scoped `source_arn`.
    """
    return (
        after.get("statement_id") == CLOUDFRONT_OAC_INVOKE_FUNCTION_STATEMENT_ID
        and after.get("action") == "lambda:InvokeFunction"
        and after.get("principal") == "cloudfront.amazonaws.com"
        and _source_arn_scoped(after, after_unknown)
        and after.get("function_name") in sanctioned_url_function_names
    )


#: The one `authorization` value admitted on a REST method.
_REST_COGNITO_AUTHORIZATION = "COGNITO_USER_POOLS"

#: `method_path` covering every resource and method.
_REST_ALL_METHODS_PATH = "*/*"


def check_api_authorizer_on_every_route(plan: Plan) -> list[Finding]:
    """Every method on the REST API demands the Cognito authorizer.

    Reads `aws_api_gateway_method`. An unknown `authorizer_id` alongside `COGNITO_USER_POOLS`
    passes; any other authorization, or no authorizer, fails.
    """
    findings = []
    for r in plan.by_type("aws_api_gateway_method"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        authorizer_id = after.get("authorizer_id")
        authorizer_deferred = not authorizer_id and is_unknown(after_unknown.get("authorizer_id"))
        ok = after.get("authorization") == _REST_COGNITO_AUTHORIZATION and (
            bool(authorizer_id) or authorizer_deferred
        )
        detail = (
            f"authorization={after.get('authorization')!r} authorizer_id={authorizer_id!r} "
            f"http_method={after.get('http_method')!r}"
        )
        if authorizer_deferred:
            detail += (
                " (unknown at plan time -- same-apply authorizer reference, resolves at apply)"
            )
        findings.append(
            Finding(
                "api_authorizer_on_every_route",
                r["address"],
                "pass" if ok else "fail",
                detail,
            )
        )
    return findings


def check_api_request_validators(plan: Plan) -> list[Finding]:
    """Every REST method references the request validator; every POST also carries the object model.

    An unknown `request_validator_id` passes; a missing one fails. Non-POST methods must carry no
    body model.
    """
    findings = []
    for r in plan.by_type("aws_api_gateway_method"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        validator_id = after.get("request_validator_id")
        validator_deferred = not validator_id and is_unknown(
            after_unknown.get("request_validator_id")
        )
        has_validator = bool(validator_id) or validator_deferred
        http_method = after.get("http_method")
        request_models = after.get("request_models") or {}
        models_deferred = not request_models and is_unknown(after_unknown.get("request_models"))
        if http_method == "POST":
            has_model = ("application/json" in request_models) or models_deferred
            model_detail = "POST must carry an application/json model"
        else:
            has_model = "application/json" not in request_models
            model_detail = "non-POST must carry no body model"
        ok = has_validator and has_model
        detail = (
            f"http_method={http_method!r} request_validator_id={validator_id!r} "
            f"request_models={request_models!r} ({model_detail})"
        )
        if validator_deferred:
            detail += (
                " (validator id unknown at plan time -- same-apply reference, resolves at apply)"
            )
        findings.append(
            Finding("api_request_validators", r["address"], "pass" if ok else "fail", detail)
        )
    return findings


def check_api_throttle(plan: Plan) -> list[Finding]:
    """Both throttle limits, applied to every method rather than to one path.

    Reads `aws_api_gateway_method_settings`, whose `method_path` must cover every method.
    """
    findings = []
    for r in plan.by_type("aws_api_gateway_method_settings"):
        after = plan.after(r)
        settings = (after.get("settings") or [{}])[0]
        method_path = after.get("method_path")
        ok = (
            method_path == _REST_ALL_METHODS_PATH
            and bool(settings.get("throttling_burst_limit"))
            and bool(settings.get("throttling_rate_limit"))
        )
        findings.append(
            Finding(
                "api_throttle",
                r["address"],
                "pass" if ok else "fail",
                f"method_path={method_path!r} (must be {_REST_ALL_METHODS_PATH!r}, or the limits "
                f"cover one route) settings={settings!r}",
            )
        )
    return findings


def check_api_access_logging(plan: Plan) -> list[Finding]:
    """The stage writes access logs to a destination that exists.

    An unknown `destination_arn` passes; an absent `access_log_settings` block fails.
    """
    findings = []
    for r in plan.by_type("aws_api_gateway_stage"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        settings = after.get("access_log_settings") or []
        settings_unknown = after_unknown.get("access_log_settings")
        destination_arn = settings[0].get("destination_arn") if settings else None
        destination_deferred = not destination_arn and (
            unknown_block_attr(settings_unknown, 0, "destination_arn")
        )
        ok = bool(settings) and (bool(destination_arn) or destination_deferred)
        detail = f"access_log_settings={settings!r}"
        if destination_deferred:
            detail += " (destination_arn unknown at plan time -- same-apply log-group reference, resolves at apply)"
        findings.append(
            Finding(
                "api_access_logging",
                r["address"],
                "pass" if ok else "fail",
                detail,
            )
        )
    return findings


def check_s3_bpa(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_s3_bucket_public_access_block"):
        after = plan.after(r)
        ok = all(
            after.get(flag) is True
            for flag in (
                "block_public_acls",
                "block_public_policy",
                "ignore_public_acls",
                "restrict_public_buckets",
            )
        )
        findings.append(
            Finding(
                "s3_block_public_access",
                r["address"],
                "pass" if ok else "fail",
                f"{after!r}",
            )
        )
    return findings


def check_s3_cmk_encryption(plan: Plan) -> list[Finding]:
    """Every non-log bucket's encryption configuration uses a CMK.

    Log and access-log buckets use SSE-S3 and are excluded by address suffix.
    """
    findings = []
    for r in plan.by_type("aws_s3_bucket_server_side_encryption_configuration"):
        excluded_suffixes = (".logs", "_access_logs")
        if any(r["address"].endswith(sfx) for sfx in excluded_suffixes):
            continue
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        rule = (after.get("rule") or [{}])[0]
        rule_unknown = (after_unknown.get("rule") or [{}])[0] if after_unknown.get("rule") else {}
        default = (rule.get("apply_server_side_encryption_by_default") or [{}])[0]
        default_unknown_blocks = rule_unknown.get("apply_server_side_encryption_by_default")
        key_id = default.get("kms_master_key_id")
        key_id_deferred = not key_id and unknown_block_attr(
            default_unknown_blocks, 0, "kms_master_key_id"
        )
        ok = default.get("sse_algorithm") == "aws:kms" and (bool(key_id) or key_id_deferred)
        detail = f"{default!r}"
        if key_id_deferred:
            detail += " (kms_master_key_id unknown at plan time -- same-apply CMK reference, resolves at apply)"
        findings.append(
            Finding(
                "s3_cmk_encryption",
                r["address"],
                "pass" if ok else "fail",
                detail,
            )
        )
    return findings


#: Sid the TLS-deny statement carries in every `infra/` bucket policy.
TLS_DENY_SID = "DenyInsecureTransport"

#: The `Bool aws:SecureTransport = false` condition that makes a statement a non-TLS deny, lowercased.
TLS_DENY_CONDITION_TEST = "bool"
TLS_DENY_CONDITION_VARIABLE = "aws:securetransport"
TLS_DENY_CONDITION_VALUE = "false"

#: Action coverage the deny must reach: a wildcard, or a superset of the minimum set.
TLS_DENY_WILDCARD_ACTIONS = frozenset({"s3:*", "*"})
TLS_DENY_MINIMUM_ACTIONS = frozenset(
    {"s3:getobject", "s3:putobject", "s3:deleteobject", "s3:listbucket"}
)

#: Resource entries the deny must scope: the bucket ARN and its object prefix.
TLS_DENY_MINIMUM_RESOURCE_ENTRIES = 2


@dataclass(frozen=True)
class PolicyCondition:
    """One normalised IAM condition block. `None` means the plan does not state it literally."""

    test: str | None
    variable: str | None
    values: tuple[str, ...] | None


@dataclass(frozen=True)
class PolicyStatement:
    """One IAM policy statement, normalised across the three plan locations it can be read from.

    `actions`/`resources` are `None` when the plan records only a reference for them.
    `resource_entries` is the number of resource entries, when the view can express it.
    """

    origin: str
    sid: str | None
    effect: str | None
    actions: tuple[str, ...] | None
    resources: tuple[str | None, ...] | None
    resource_entries: int | None
    conditions: tuple[PolicyCondition, ...]
    #: Resource addresses `resources` references (`configuration` view only).
    resource_references: tuple[str, ...] | None = None
    #: `(principal type, identifier)` pairs; type `""` is the bare `"*"` form; `None` means unstated.
    principals: tuple[tuple[str, str], ...] | None = None
    #: The same pairs under `NotPrincipal`/`not_principals`. `None` means unstated.
    not_principals: tuple[tuple[str, str], ...] | None = None


def _as_tuple(value: Any) -> tuple[Any, ...] | None:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, str):
        return (value,)
    return None


def _normalise_conditions(raw: Any, *, json_document: bool) -> tuple[PolicyCondition, ...]:
    """Normalise either IAM condition encoding into `PolicyCondition`s.

    Args:
        raw: `{"Bool": {"aws:SecureTransport": "false"}}` (a rendered policy document) or a list of
            `{"test": ..., "variable": ..., "values": [...]}` blocks (Terraform's own encoding).
        json_document: Whether `raw` uses the rendered-document mapping form.

    Returns:
        One entry per condition; an unstated field is `None`.
    """
    conditions: list[PolicyCondition] = []
    if json_document:
        if not isinstance(raw, dict):
            return ()
        for test, variables in raw.items():
            if not isinstance(variables, dict):
                continue
            for variable, values in variables.items():
                conditions.append(
                    PolicyCondition(
                        test=str(test),
                        variable=str(variable),
                        values=tuple(str(v) for v in _as_tuple(values) or ()),
                    )
                )
        return tuple(conditions)
    if not isinstance(raw, list):
        return ()
    for block in raw:
        if not isinstance(block, dict):
            continue
        values = _as_tuple(block.get("values"))
        conditions.append(
            PolicyCondition(
                test=block.get("test") if isinstance(block.get("test"), str) else None,
                variable=block.get("variable") if isinstance(block.get("variable"), str) else None,
                values=None if values is None else tuple(str(v) for v in values),
            )
        )
    return tuple(conditions)


def _normalise_json_principals(raw: Any) -> tuple[tuple[str, str], ...] | None:
    """Flatten a rendered document's `Principal`/`NotPrincipal` into `(type, identifier)` pairs.

    Accepts the bare `"*"` (type `""`), a single- or multi-type mapping, and list values. Returns
    `None` when the key is absent.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return (("", raw),)
    if isinstance(raw, list):
        return tuple(("", str(entry)) for entry in raw)
    if isinstance(raw, dict):
        pairs: list[tuple[str, str]] = []
        for principal_type, values in raw.items():
            pairs.extend((str(principal_type), str(v)) for v in _as_tuple(values) or ())
        return tuple(pairs)
    return ()


def _normalise_terraform_principals(raw: Any) -> tuple[tuple[str, str], ...] | None:
    """Flatten Terraform's `principals`/`not_principals` blocks into `(type, identifier)` pairs.

    A block whose `identifiers` is only a reference contributes no pair.
    """
    if not isinstance(raw, list):
        return None
    pairs: list[tuple[str, str]] = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        principal_type = block.get("type")
        pairs.extend(
            (str(principal_type) if isinstance(principal_type, str) else "", str(v))
            for v in _as_tuple(block.get("identifiers")) or ()
        )
    return tuple(pairs)


def _statement_from_json_document(stmt: dict[str, Any], *, origin: str) -> PolicyStatement:
    resources = _as_tuple(stmt.get("Resource"))
    return PolicyStatement(
        origin=origin,
        sid=stmt.get("Sid") if isinstance(stmt.get("Sid"), str) else None,
        effect=stmt.get("Effect") if isinstance(stmt.get("Effect"), str) else None,
        actions=None if _as_tuple(stmt.get("Action")) is None else _as_tuple(stmt.get("Action")),
        resources=resources,
        resource_entries=None if resources is None else len(resources),
        conditions=_normalise_conditions(stmt.get("Condition"), json_document=True),
        principals=_normalise_json_principals(stmt.get("Principal")),
        not_principals=_normalise_json_principals(stmt.get("NotPrincipal")),
    )


def _statement_from_terraform(stmt: dict[str, Any], *, origin: str) -> PolicyStatement:
    """Normalise a statement read from `configuration` literals or from a data source's `after`.

    Both use Terraform's lowercase key names. A data source's `after` keeps a `null` per unknown
    resource entry, so its length is the entry count.
    """
    resources = _as_tuple(stmt.get("resources"))
    known_resources = (
        None if resources is None or any(entry is None for entry in resources) else resources
    )
    # Reference targets are not an entry count; `None` means this view cannot express one.
    entries: int | None = None if resources is None else len(resources)
    raw_references = stmt.get("resource_references")
    references = (
        tuple(
            sorted(
                {
                    re.sub(r"^([^.]+\.[^.]+).*$", r"\1", ref)
                    for ref in raw_references
                    if isinstance(ref, str)
                }
            )
        )
        if isinstance(raw_references, list)
        else None
    )
    return PolicyStatement(
        origin=origin,
        sid=stmt.get("sid") if isinstance(stmt.get("sid"), str) else None,
        effect=stmt.get("effect") if isinstance(stmt.get("effect"), str) else None,
        actions=_as_tuple(stmt.get("actions")),
        resources=known_resources,
        resource_entries=entries,
        conditions=_normalise_conditions(stmt.get("condition"), json_document=False),
        resource_references=references,
        principals=_normalise_terraform_principals(stmt.get("principals")),
        not_principals=_normalise_terraform_principals(stmt.get("not_principals")),
    )


#: Reference prefixes the plan's `configuration` section cannot resolve to a resource address.
_UNRESOLVABLE_REFERENCE_PREFIXES: tuple[str, ...] = ("local.", "var.", "each.", "count.")


def _is_tls_deny_condition(condition: PolicyCondition) -> bool:
    """Whether `condition` is exactly the TLS test, the only condition a non-TLS deny may carry."""
    return (
        condition.test is not None
        and condition.test.lower() == TLS_DENY_CONDITION_TEST
        and condition.variable is not None
        and condition.variable.lower() == TLS_DENY_CONDITION_VARIABLE
        and condition.values is not None
        and tuple(v.lower() for v in condition.values) == (TLS_DENY_CONDITION_VALUE,)
    )


#: Principal identifier reaching every caller, and the types it may appear under (`""` is bare).
TLS_DENY_WILDCARD_PRINCIPAL = "*"
TLS_DENY_WILDCARD_PRINCIPAL_TYPES = frozenset({"", "aws"})


#: An S3 resource ARN, capturing the bucket segment.
_S3_ARN_PATTERN = re.compile(r"^arn:[^:]*:s3:[^:]*:[^:]*:(?P<bucket>[^/]+)(?P<key>/.*)?$")


def _arn_bucket_segment(entry: str) -> str | None:
    """Return the bucket segment of an S3 resource ARN, or `None` if `entry` is not one."""
    match = _S3_ARN_PATTERN.match(entry)
    return match.group("bucket") if match is not None else None


def _tls_deny_statement_problems(
    stmt: PolicyStatement,
    *,
    bucket_references: tuple[str, ...] = (),
    bucket_names: tuple[str, ...] = (),
) -> list[str]:
    """Return every way `stmt` fails to be a correct non-TLS deny (empty means it is one).

    The statement must reach every caller: only TLS-test conditions, a wildcard principal, and no
    `NotPrincipal`.

    Args:
        stmt: The normalised statement.
        bucket_references: Resource addresses the enclosing policy's `bucket` argument references;
            when known, the statement must scope at least one of them.
        bucket_names: Literal bucket names the enclosing policy attaches to; literal ARN resources
            must name one of them.
    """
    problems: list[str] = []
    if (stmt.effect or "").lower() != "deny":
        problems.append(f"effect is {stmt.effect!r}, not 'Deny'")
    matching = [c for c in stmt.conditions if _is_tls_deny_condition(c)]
    if not matching:
        problems.append(
            f"no condition asserting {TLS_DENY_CONDITION_TEST} "
            f"{TLS_DENY_CONDITION_VARIABLE}={TLS_DENY_CONDITION_VALUE} "
            f"(conditions present: {[(c.test, c.variable, c.values) for c in stmt.conditions]})"
        )
    narrowing = [c for c in stmt.conditions if not _is_tls_deny_condition(c)]
    if narrowing:
        problems.append(
            f"condition(s) {[(c.test, c.variable, c.values) for c in narrowing]} are not a "
            f"restatement of {TLS_DENY_CONDITION_TEST} "
            f"{TLS_DENY_CONDITION_VARIABLE}={TLS_DENY_CONDITION_VALUE}, so the deny does not apply "
            f"to every caller and plaintext HTTP stays permitted outside their reach"
        )
    if stmt.not_principals:
        problems.append(
            f"statement excludes principals {list(stmt.not_principals)} via NotPrincipal, so the "
            f"deny does not apply to every caller"
        )
    if stmt.principals is None:
        problems.append(
            "statement states no principal, so the callers it denies cannot be verified"
        )
    elif not any(
        identifier == TLS_DENY_WILDCARD_PRINCIPAL
        and principal_type.lower() in TLS_DENY_WILDCARD_PRINCIPAL_TYPES
        for principal_type, identifier in stmt.principals
    ):
        problems.append(
            f"statement denies only principals {list(stmt.principals)} rather than every caller "
            f"({TLS_DENY_WILDCARD_PRINCIPAL!r} under principal type "
            f"{sorted(TLS_DENY_WILDCARD_PRINCIPAL_TYPES)}), so plaintext HTTP stays permitted for "
            f"every other caller"
        )
    if stmt.actions is None:
        problems.append("actions are not a plan-time literal, so their coverage cannot be verified")
    else:
        lowered = {str(a).lower() for a in stmt.actions}
        if not (lowered & TLS_DENY_WILDCARD_ACTIONS) and not lowered >= TLS_DENY_MINIMUM_ACTIONS:
            problems.append(
                f"actions {sorted(lowered)} cover neither all S3 operations nor the minimum "
                f"object-and-bucket set {sorted(TLS_DENY_MINIMUM_ACTIONS)}"
            )
    # `None` means this view cannot express the count; the caller defers when no view can.
    if (
        stmt.resource_entries is not None
        and stmt.resource_entries < TLS_DENY_MINIMUM_RESOURCE_ENTRIES
    ):
        problems.append(
            f"statement scopes {stmt.resource_entries} resource entry/entries, fewer than the "
            f"{TLS_DENY_MINIMUM_RESOURCE_ENTRIES} needed to cover both the bucket and its objects"
        )
    if stmt.resources is not None:
        objects = [r for r in stmt.resources if isinstance(r, str) and r.endswith("/*")]
        buckets = [r for r in stmt.resources if isinstance(r, str) and not r.endswith("/*")]
        if not objects or not buckets:
            problems.append(
                f"resources {list(stmt.resources)} do not cover both the bucket and its objects"
            )
        literals = [r for r in stmt.resources if isinstance(r, str)]
        segments = {entry: _arn_bucket_segment(entry) for entry in literals}
        unparseable = sorted(entry for entry, segment in segments.items() if segment is None)
        if unparseable:
            problems.append(
                f"resource entries {unparseable} are not S3 resource ARNs, so the bucket they "
                f"scope cannot be determined"
            )
        elif not bucket_names:
            problems.append(
                f"resources {literals} are literal ARNs but no literal bucket name is knowable "
                f"for the bucket this policy attaches to, so their scope cannot be tied to it"
            )
        else:
            foreign = sorted(
                entry
                for entry, segment in segments.items()
                if segment is not None
                and not any(fnmatch.fnmatch(name, segment) for name in bucket_names)
            )
            if foreign:
                problems.append(
                    f"resource entries {foreign} name a bucket other than the one this policy "
                    f"attaches to ({list(bucket_names)})"
                )
    # The bucket tie is asserted only when the statement references a resource directly.
    statement_names_resources = stmt.resource_references is not None and any(
        not ref.startswith(_UNRESOLVABLE_REFERENCE_PREFIXES) for ref in stmt.resource_references
    )
    if (
        bucket_references
        and statement_names_resources
        and not set(bucket_references) & set(stmt.resource_references or ())
    ):
        problems.append(
            f"statement scopes {list(stmt.resource_references or ())}, which does not include the "
            f"bucket this policy attaches to ({list(bucket_references)})"
        )
    return problems


def _is_tls_deny_statement(stmt: PolicyStatement) -> bool:
    """Whether `stmt` is the TLS-deny statement, by the same `Sid` the view checks select on."""
    return stmt.sid == TLS_DENY_SID


def _tls_deny_view_problem(
    statements: list[PolicyStatement],
    *,
    origin: str,
    bucket_references: tuple[str, ...] = (),
    bucket_names: tuple[str, ...] = (),
) -> str | None:
    """Return why no statement in one view is a correct non-TLS deny, or `None` if one is.

    A statement carrying the expected `Sid` but failing an assertion reports that assertion's
    reason, so present-but-misconfigured drift is distinguishable from an absent statement.
    """
    if not statements:
        return f"{origin}: policy document has no statements"
    problems_by_statement = [
        (
            stmt,
            _tls_deny_statement_problems(
                stmt, bucket_references=bucket_references, bucket_names=bucket_names
            ),
        )
        for stmt in statements
    ]
    if any(not problems for _stmt, problems in problems_by_statement):
        return None
    labelled = [
        (stmt, problems) for stmt, problems in problems_by_statement if stmt.sid == TLS_DENY_SID
    ]
    if labelled:
        stmt, problems = labelled[0]
        return f"{origin}: statement Sid={TLS_DENY_SID!r} is not a valid non-TLS deny: {problems}"
    return (
        f"{origin}: no statement denies non-TLS access; no statement carries Sid="
        f"{TLS_DENY_SID!r} either (sids seen: {[s.sid for s, _ in problems_by_statement]})"
    )


def check_s3_tls_deny(plan: Plan) -> list[Finding]:
    """Assert every bucket policy denies non-TLS access to the bucket and its objects.

    Reads every view the plan states the policy in (the rendered `after.policy`, the
    `configuration` literals, and the data source's `change.after.statement`); each available view
    must carry a valid non-TLS deny (see `_tls_deny_statement_problems`) scoped to this policy's
    own bucket.
    """
    findings = []
    for r in plan.by_type("aws_s3_bucket_policy"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        address = r["address"]
        views: list[tuple[str, list[PolicyStatement]]] = []

        policy_raw = after.get("policy")
        if policy_raw:
            doc = json.loads(policy_raw)
            raw_statements = doc.get("Statement", [])
            views.append(
                (
                    "after.policy",
                    [
                        _statement_from_json_document(stmt, origin="after.policy")
                        for stmt in raw_statements
                        if isinstance(stmt, dict)
                    ],
                )
            )
        elif not is_unknown(after_unknown.get("policy")):
            findings.append(
                Finding(
                    "s3_tls_deny",
                    address,
                    "fail",
                    "policy is absent from `after` and not marked unknown in `after_unknown` -- "
                    "cannot be classified as either satisfied or deferred",
                )
            )
            continue

        static_statements = plan.static_policy_statements_for(address)
        if static_statements is not None:
            views.append(
                (
                    "configuration",
                    [
                        _statement_from_terraform(stmt, origin="configuration")
                        for stmt in static_statements
                    ],
                )
            )
        data_statements = plan.data_source_statements_for(address)
        if data_statements is not None:
            views.append(
                (
                    "data_source.after",
                    [
                        _statement_from_terraform(stmt, origin="data_source.after")
                        for stmt in data_statements
                    ],
                )
            )

        if not views:
            findings.append(
                Finding(
                    "s3_tls_deny",
                    address,
                    "fail",
                    "policy unknown at plan time and no matching "
                    "data.aws_iam_policy_document configuration entry or data-source change found "
                    "to read the literal statement from -- cannot verify the TLS-deny statement",
                )
            )
            continue

        bucket_references = tuple(plan.bucket_policy_bucket_references(address) or ())
        bucket_names = plan.bucket_names_for_policy(address)
        problems = [
            problem
            for origin, statements in views
            if (
                problem := _tls_deny_view_problem(
                    statements,
                    origin=origin,
                    bucket_references=bucket_references,
                    bucket_names=bucket_names,
                )
            )
            is not None
        ]
        # Records which views counted the resource entries, for the finding's detail.
        counted = [
            (origin, stmt.resource_entries)
            for origin, statements in views
            for stmt in statements
            if _is_tls_deny_statement(stmt) and stmt.resource_entries is not None
        ]
        if counted:
            witness = (
                f" Resource entries counted in {[origin for origin, _ in counted]} as "
                f"{sorted({entries for _, entries in counted})}."
            )
        else:
            witness = (
                " Resource entries NOT counted by any view, so this run does not independently "
                "witness that the deny covers the bucket's OBJECTS as well as the bucket -- "
                "terraform's `configuration` section records reference targets rather than "
                "expressions, and this policy's document has no resolved data-source view. The "
                "coverage requirement itself was still asserted above, from the statement's "
                "resources; only the corroborating count is missing."
            )
        findings.append(
            Finding(
                "s3_tls_deny",
                address,
                "fail" if problems else "pass",
                "; ".join(problems)
                if problems
                else (
                    "Deny on Bool aws:SecureTransport=false covering the bucket and its objects, "
                    f"verified in {[origin for origin, _ in views]}." + witness
                ),
            )
        )
    return findings


#: Attribute that makes a destroy of a non-empty container succeed, per resource type.
TEARDOWN_FLAG_ATTR: dict[str, str] = {
    "aws_s3_bucket": "force_destroy",
    "aws_ecr_repository": "force_delete",
}

#: Every S3 bucket in `infra/`, keyed `<module-dir>/<type>.<name>`, mapped to `required` (the
#: teardown flag must be set) or `forbidden` (it must not be). An unclassified bucket fails.
BUCKET_TEARDOWN_DISPOSITION: dict[str, str] = {
    "modules/observability/aws_s3_bucket.cloudtrail": "required",
    "modules/observability/aws_s3_bucket.cloudtrail_access_logs": "required",
    "modules/frontend_cdn/aws_s3_bucket.logs": "required",
    "modules/frontend_cdn/aws_s3_bucket.origin": "required",
    "modules/eval_artifacts/aws_s3_bucket.this": "required",
}


def _strip_tf_comments_and_strings(line: str) -> str:
    """Blank out `"..."` literals and `#`/`//` comments so brace counting is not fooled by them."""
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(line):
        char = line[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "#" or line[index : index + 2] == "//":
            break
        out.append(char)
        index += 1
    return "".join(out)


def tf_resource_blocks(resource_type: str, *, kind: str = "resource") -> list[tuple[str, str, str]]:
    """Return `(module_dir, resource_name, body)` for every `<kind> "<type>" "<name>"` in `infra/`.

    `kind` is `"resource"` or `"data"`. `module_dir` is the containing directory relative to
    `infra/`. The body is delimited by brace depth with string literals, comments and heredocs
    neutralised.
    """
    header = re.compile(
        rf'^\s*{re.escape(kind)}\s+"{re.escape(resource_type)}"\s+"([A-Za-z0-9_-]+)"'
    )
    blocks: list[tuple[str, str, str]] = []
    for path in sorted(INFRA_DIR.rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        module_dir = path.parent.relative_to(INFRA_DIR).as_posix() or "."
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines()
        # Depth is counted on the literal-blanked text; the body is sliced from the original lines.
        neutral = blank_literals(original)
        if not same_shape(original, neutral):
            raise RuntimeError(f"{path}: blank_literals changed the text's shape")
        neutral_lines = neutral.splitlines()
        index = 0
        while index < len(lines):
            match = header.match(lines[index])
            if not match:
                index += 1
                continue
            name = match.group(1)
            body: list[str] = []
            depth = 0
            while index < len(lines):
                code = _strip_tf_comments_and_strings(neutral_lines[index])
                depth += code.count("{") - code.count("}")
                body.append(lines[index])
                index += 1
                if depth <= 0:
                    break
            blocks.append((module_dir, name, "\n".join(body)))
    return blocks


def _declares_flag_true(body: str, attr: str) -> bool:
    """Whether `<attr> = true` appears as a literal argument in a resource body.

    Literal `true` only; a variable or expression does not count.
    """
    return re.search(rf"^\s*{re.escape(attr)}\s*=\s*true\s*(#.*)?$", body, re.MULTILINE) is not None


def check_no_json_syntax_terraform(_plan: Plan) -> list[Finding]:
    """No `*.tf.json` exists under `infra/`, excluding `.terraform/`.

    The source-reading controls read `*.tf` only. Each such file is a `fail`, as is a missing
    `infra/`.
    """
    if not INFRA_DIR.is_dir():
        return [
            Finding(
                "no_json_syntax_terraform",
                str(INFRA_DIR),
                "fail",
                "infra/ not found, so this control examined nothing",
            )
        ]
    found = sorted(
        path.relative_to(INFRA_DIR).as_posix()
        for path in INFRA_DIR.rglob("*.tf.json")
        if ".terraform" not in path.relative_to(INFRA_DIR).parts
    )
    if not found:
        return [
            Finding(
                "no_json_syntax_terraform",
                "(scope) infra/",
                "pass",
                "no *.tf.json under infra/ (excluding .terraform/), so the *.tf source readers see "
                "every Terraform file",
            )
        ]
    return [
        Finding(
            "no_json_syntax_terraform",
            f"infra/{path}",
            "fail",
            "Terraform JSON-syntax file: every source-reading control globs *.tf only, so resources "
            "declared here are invisible to them. Convert it to HCL.",
        )
        for path in found
    ]


def check_teardown_force_destroy(_plan: Plan) -> list[Finding]:
    """Every `required` bucket and every ECR repository carries its teardown flag.

    Read from Terraform source. Also fails a `forbidden` bucket with the flag, a bucket absent from
    `BUCKET_TEARDOWN_DISPOSITION`, and a stale entry there.
    """
    findings: list[Finding] = []

    bucket_attr = TEARDOWN_FLAG_ATTR["aws_s3_bucket"]
    declared: set[str] = set()
    for module_dir, name, body in tf_resource_blocks("aws_s3_bucket"):
        key = f"{module_dir}/aws_s3_bucket.{name}"
        declared.add(key)
        disposition = BUCKET_TEARDOWN_DISPOSITION.get(key)
        present = _declares_flag_true(body, bucket_attr)
        if disposition == "required":
            ok, detail = present, f"{bucket_attr}={present} disposition=required (log/artefact)"
        elif disposition == "forbidden":
            ok = not present
            detail = (
                f"{bucket_attr}={present} disposition=forbidden -- this bucket holds data, and "
                "the flag is scoped to log/artefact buckets only"
            )
        else:
            ok = False
            detail = (
                "declared in infra/ but absent from BUCKET_TEARDOWN_DISPOSITION; classify it "
                "`required` (log/artefact) or `forbidden` (holds data) -- there is no default"
            )
        findings.append(Finding("teardown_force_destroy", key, "pass" if ok else "fail", detail))

    for key in sorted(BUCKET_TEARDOWN_DISPOSITION):
        stale = key not in declared
        findings.append(
            Finding(
                "teardown_force_destroy",
                f"(classified) {key}",
                "fail" if stale else "pass",
                "BUCKET_TEARDOWN_DISPOSITION entry is stale: no such bucket is declared in infra/ "
                "-- remove the entry"
                if stale
                else f"disposition={BUCKET_TEARDOWN_DISPOSITION[key]}, bucket declared",
            )
        )

    repo_attr = TEARDOWN_FLAG_ATTR["aws_ecr_repository"]
    repositories = tf_resource_blocks("aws_ecr_repository")
    for module_dir, name, body in repositories:
        present = _declares_flag_true(body, repo_attr)
        findings.append(
            Finding(
                "teardown_force_destroy",
                f"{module_dir}/aws_ecr_repository.{name}",
                "pass" if present else "fail",
                f"{repo_attr}={present}; every ECR repository here holds build artefacts only "
                "(RepositoryNotEmptyException)",
            )
        )
    findings.append(
        Finding(
            "teardown_force_destroy",
            "(coverage) subjects found in infra/",
            "pass" if declared and repositories else "fail",
            f"buckets={len(declared)} ecr_repositories={len(repositories)}",
        )
    )
    return findings


# ---------------------------------------------------------------------------
# Source-derived IAM controls over the relay's execution role.
# ---------------------------------------------------------------------------

#: Directory, relative to `infra/`, whose policy documents belong to the relay's execution role.
RELAY_MODULE_DIR = "modules/agent_relay"
#: Sid of the harness Deny, excluded from `check_relay_denies_command_execution`'s Deny count.
_RELAY_HARNESS_DENY_SID = "DenyEveryHarness"

#: The one action the relay's `not_actions` Deny excepts; it must be the only exception.
RELAY_DENY_PERMITTED_ACTION = "bedrock-agentcore:InvokeAgentRuntime"

#: Actions the relay's role must never be granted.
RELAY_FORBIDDEN_ALLOW_ACTIONS = (
    "lambda:InvokeFunction",
    "bedrock:InvokeModel",
    "iam:PassRole",
    "secretsmanager:GetSecretValue",
)

#: Action prefixes/wildcards forbidden in an `Allow`, matched more loosely than the exact list above.
RELAY_FORBIDDEN_ALLOW_PREFIXES = ("dynamodb:",)
RELAY_FORBIDDEN_ALLOW_WILDCARDS = ("bedrock-agentcore:*", "*")

_STATEMENT_HEADER = re.compile(r"^\s*statement\s*\{", re.MULTILINE)


@dataclass(frozen=True)
class TerraformStatement:
    """One `statement { … }` block of a `data "aws_iam_policy_document"`, read from source.

    `actions_are_literal` is false when any action entry is an expression rather than a quoted
    string; callers report such a statement non-compliant.
    """

    sid: str
    effect: str
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    principal_identifiers: tuple[str, ...]
    actions_are_literal: bool
    #: The exclusion form: the statement matches every action except these.
    not_actions: tuple[str, ...] = ()
    not_actions_are_literal: bool = True


def _block_bodies(body: str, header: re.Pattern[str]) -> list[str]:
    """Every sub-block in `body` whose opening line matches `header`, delimited by brace depth."""
    bodies: list[str] = []
    for match in header.finditer(body):
        depth = 0
        index = match.start()
        started = False
        while index < len(body):
            code = _strip_tf_comments_and_strings(
                body[index : body.index("\n", index) + 1 if "\n" in body[index:] else len(body)]
            )
            depth += code.count("{") - code.count("}")
            started = started or "{" in code
            next_newline = body.find("\n", index)
            index = len(body) if next_newline == -1 else next_newline + 1
            if started and depth <= 0:
                break
        bodies.append(body[match.start() : index])
    return bodies


def _split_top_level(inner: str) -> list[str]:
    """Split a bracketed HCL list body on commas that are not inside a string, bracket or paren."""
    entries: list[str] = []
    depth = 0
    in_string = False
    current: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if in_string:
            current.append(char)
            if char == "\\":
                if index + 1 < len(inner):
                    current.append(inner[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            entries.append("".join(current))
            current = []
        elif char == "#":
            while index < len(inner) and inner[index] != "\n":
                index += 1
            continue
        else:
            current.append(char)
        index += 1
    entries.append("".join(current))
    return [entry.strip() for entry in entries if entry.strip()]


def _hcl_string_list(body: str, *names: str) -> tuple[tuple[str, ...], bool]:
    """Return `(values, all_quoted)` for the first `name = [...]` or `name = "..."` assignment.

    Each entry is its quoted content for a string literal, or its raw source text for a reference.
    `all_quoted` is false if any entry, or the whole attribute, is an expression.
    """
    for name in names:
        match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(.+?)$", body, re.MULTILINE)
        if match is None:
            continue
        raw = match.group(1).strip()
        if raw.startswith("["):
            end = body.index("]", match.start(1))
            inner = body[body.index("[", match.start(1)) + 1 : end]
            entries = _split_top_level(inner)
            values: list[str] = []
            all_quoted = True
            for entry in entries:
                quoted = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', entry)
                if quoted:
                    values.append(quoted.group(1))
                else:
                    all_quoted = False
                    values.append(entry)
            return tuple(values), all_quoted
        quoted_scalar = re.match(r'"((?:[^"\\]|\\.)*)"', raw)
        if quoted_scalar:
            return (quoted_scalar.group(1),), True
        return (raw,), False
    return (), True


def _hcl_scalar(body: str, name: str) -> str:
    match = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"', body, re.MULTILINE)
    return match.group(1) if match else ""


def policy_document_statements(
    module_dir: str | None = None,
) -> dict[str, list[TerraformStatement]]:
    """Parse every `data "aws_iam_policy_document"` in `infra/` (or one module) from source.

    Keyed `"<module_dir>/<name>"`.
    """
    documents: dict[str, list[TerraformStatement]] = {}
    for directory, name, body in tf_resource_blocks("aws_iam_policy_document", kind="data"):
        if module_dir is not None and directory != module_dir:
            continue
        statements: list[TerraformStatement] = []
        for statement_body in _block_bodies(body, _STATEMENT_HEADER):
            actions, actions_literal = _hcl_string_list(statement_body, "actions", "action")
            not_actions, not_actions_literal = _hcl_string_list(
                statement_body, "not_actions", "not_action"
            )
            resources, _ = _hcl_string_list(statement_body, "resources", "resource")
            identifiers: tuple[str, ...] = ()
            for principal_body in _block_bodies(
                statement_body, re.compile(r"^\s*principals\s*\{", re.MULTILINE)
            ):
                found, _ = _hcl_string_list(principal_body, "identifiers")
                identifiers += found
            statements.append(
                TerraformStatement(
                    sid=_hcl_scalar(statement_body, "sid"),
                    # Terraform defaults an omitted `effect` to Allow.
                    effect=_hcl_scalar(statement_body, "effect") or "Allow",
                    actions=actions,
                    resources=resources,
                    principal_identifiers=identifiers,
                    actions_are_literal=actions_literal,
                    not_actions=not_actions,
                    not_actions_are_literal=not_actions_literal,
                )
            )
        documents[f"{directory}/{name}"] = statements
    return documents


#: The service principal S3 server access logging delivers as.
S3_LOG_DELIVERY_PRINCIPAL = "logging.s3.amazonaws.com"

#: The all-Region service principal Elastic Load Balancing delivers access logs as.
ALB_LOG_DELIVERY_PRINCIPAL = "logdelivery.elasticloadbalancing.amazonaws.com"

#: `access_logs { ... }` inside an `aws_lb` body.
_ACCESS_LOGS_BLOCK = re.compile(r"^\s*access_logs\s*\{", re.MULTILINE)

#: `aws_s3_bucket.<name>.id` / `.bucket` / `.arn` -> `<name>`.
_BUCKET_REFERENCE = re.compile(r"aws_s3_bucket\.([A-Za-z0-9_-]+)\.(?:id|bucket|arn)")


def check_s3_access_log_delivery_grant(_plan: Plan) -> list[Finding]:
    """Every S3 access-logging destination bucket actually grants the log-delivery principal.

    A grant is a `logging.s3.amazonaws.com` `s3:PutObject` Allow statement or an
    `aws_s3_bucket_acl`. Read from source; ALB access-log grants are checked too.
    """
    findings: list[Finding] = []

    granted_buckets: set[tuple[str, str]] = set()
    for key, statements in policy_document_statements().items():
        module_dir = key.rsplit("/", 1)[0]
        for statement in statements:
            if statement.effect != "Allow":
                continue
            if S3_LOG_DELIVERY_PRINCIPAL not in statement.principal_identifiers:
                continue
            if not any(action.startswith("s3:PutObject") for action in statement.actions):
                continue
            for resource in statement.resources:
                for match in _BUCKET_REFERENCE.finditer(resource):
                    granted_buckets.add((module_dir, match.group(1)))
    for module_dir, name, _body in tf_resource_blocks("aws_s3_bucket_acl"):
        granted_buckets.add((module_dir, name))

    targets = 0
    for module_dir, name, body in tf_resource_blocks("aws_s3_bucket_logging"):
        targets += 1
        address = f"{module_dir}/aws_s3_bucket_logging.{name}"
        target_raw = _hcl_expression(body, "target_bucket")
        reference = _BUCKET_REFERENCE.search(target_raw)
        if reference is None:
            findings.append(
                Finding(
                    "s3_access_log_delivery_grant",
                    address,
                    "deferred",
                    f"target_bucket={target_raw!r} is a cross-module input, so the link from this "
                    "source to its destination bucket is not resolvable from this module's source. "
                    "The root wires it to module.frontend_cdn's logs bucket, whose delivery grant IS "
                    "asserted above and is scoped by aws:SourceAccount rather than by source ARN, so "
                    "it admits this source too -- but that wiring is read by a human here, not by "
                    "this control, which is why this is deferred and not a pass",
                )
            )
            continue
        target = (module_dir, reference.group(1))
        findings.append(
            Finding(
                "s3_access_log_delivery_grant",
                address,
                "pass" if target in granted_buckets else "fail",
                f"destination=aws_s3_bucket.{reference.group(1)}; delivery grant "
                f"({S3_LOG_DELIVERY_PRINCIPAL} s3:PutObject in its bucket policy, or an "
                f"aws_s3_bucket_acl) present={target in granted_buckets}. Logging configuration "
                "without a delivery grant delivers nothing while reading as enabled.",
            )
        )

    findings.append(
        Finding(
            "s3_access_log_delivery_grant",
            "(coverage) aws_s3_bucket_logging resources in infra/",
            "pass" if targets else "fail",
            f"found={targets}; zero means access logging was removed or renamed, in which case the "
            "access-logging row is not satisfied at all rather than trivially satisfied",
        )
    )
    findings.extend(_alb_access_log_grant_findings())
    return findings


def _alb_access_log_grant_findings() -> list[Finding]:
    """Every `aws_lb` with access logging enabled has an ELB log-delivery grant in the tree.

    Tree-wide: the destination bucket is a cross-module input, so the grant may be in any module.
    """
    findings: list[Finding] = []

    grants: list[tuple[str, TerraformStatement]] = []
    for key, statements in policy_document_statements().items():
        for statement in statements:
            if statement.effect != "Allow":
                continue
            if ALB_LOG_DELIVERY_PRINCIPAL not in statement.principal_identifiers:
                continue
            if not any(action.startswith("s3:PutObject") for action in statement.actions):
                continue
            grants.append((key, statement))

    logging_albs = 0
    for module_dir, name, body in tf_resource_blocks("aws_lb"):
        address = f"{module_dir}/aws_lb.{name}"
        blocks = _block_bodies(body, _ACCESS_LOGS_BLOCK)
        if not blocks:
            continue
        # `enabled` defaults to false, so only an explicit `true` requires a grant.
        if not any(_hcl_expression(block, "enabled").strip() == "true" for block in blocks):
            continue
        logging_albs += 1
        findings.append(
            Finding(
                "s3_access_log_delivery_grant",
                address,
                "pass" if grants else "fail",
                f"access_logs enabled=true; ELB delivery grant ({ALB_LOG_DELIVERY_PRINCIPAL} "
                f"s3:PutObject in a bucket policy) present in the tree={bool(grants)} "
                f"(granting documents={[key for key, _ in grants]}). Without it `aws_lb` creation "
                'FAILS with "Access Denied for bucket" -- this blocks the apply, it does not merely '
                "lose logs. The destination is a cross-module input, so the link from this load "
                "balancer to the granting bucket is read by a human here; the prefix halves are held "
                "together by the root's single alb_access_log_prefix local instead.",
            )
        )

    if not logging_albs and grants:
        findings.append(
            Finding(
                "s3_access_log_delivery_grant",
                "(coverage) aws_lb access_logs enabled in infra/",
                "fail",
                f"an ELB log-delivery grant exists ({[key for key, _ in grants]}) but no aws_lb "
                "enables access_logs. Either logging was turned off and the grant is now an "
                "unnecessary standing permission on the log bucket, or the block was renamed and this "
                "control has stopped watching the thing it was written for.",
            )
        )
    return findings


def check_relay_denies_command_execution(_plan: Plan) -> list[Finding]:
    """The relay's Deny is exclusion-shaped: every action on the runtime except one permitted invoke.

    Asserts the shape (`not_actions` and no `actions`), the exception set (exactly
    `RELAY_DENY_PERMITTED_ACTION`), and the resource scope (no `"*"`) of the one governing Deny.
    """
    findings: list[Finding] = []
    documents = policy_document_statements(RELAY_MODULE_DIR)
    denies = [
        (document, statement)
        for document, statements in sorted(documents.items())
        for statement in statements
        if statement.effect.lower() == "deny"
    ]

    # The separate harness Deny is excluded by Sid, leaving one governing Deny.
    governing = [(d, s) for (d, s) in denies if (s.sid or "") != _RELAY_HARNESS_DENY_SID]

    findings.append(
        Finding(
            "relay_denies_command_execution",
            f"(coverage) governing Deny statements in {RELAY_MODULE_DIR}",
            "pass" if len(governing) == 1 else "fail",
            f"documents={len(documents)}, deny statements={len(denies)}, "
            f"governing (non-harness) Deny statements={len(governing)}"
            + (
                ""
                if len(governing) == 1
                else " -- expected exactly one governing Deny besides the harness Deny; a second "
                "means this control cannot say which statement governs"
            ),
        )
    )
    if len(governing) != 1:
        return findings

    document, statement = governing[0]
    label = f"{document}#{statement.sid or '(no sid)'}"

    # `not_actions` must be literal; `resources` need only yield entries.
    if not statement.not_actions_are_literal or not statement.resources:
        findings.append(
            Finding(
                "relay_denies_command_execution",
                label,
                "fail",
                "the Deny's not_actions are not literal strings, or it declares no readable "
                "resources, so its exception set or its scope cannot be read from source",
            )
        )
        return findings

    findings.append(
        Finding(
            "relay_denies_command_execution",
            f"{label} (shape)",
            "pass" if statement.not_actions and not statement.actions else "fail",
            "exclusion-shaped (not_actions, no actions), so every action on the scoped resources "
            "except the exceptions below is denied -- including routes AWS has not shipped yet"
            if statement.not_actions and not statement.actions
            else f"expected not_actions and no actions; got actions={list(statement.actions)}, "
            f"not_actions={list(statement.not_actions)}. An enumerated Deny is only as complete as "
            "its enumeration on the day it was written",
        )
    )
    exceptions = set(statement.not_actions)
    findings.append(
        Finding(
            "relay_denies_command_execution",
            f"{label} (exception set)",
            "pass" if exceptions == {RELAY_DENY_PERMITTED_ACTION} else "fail",
            f"exactly {RELAY_DENY_PERMITTED_ACTION!r} is excepted"
            if exceptions == {RELAY_DENY_PERMITTED_ACTION}
            else f"expected exactly {{{RELAY_DENY_PERMITTED_ACTION!r}}}, got {sorted(exceptions)}. "
            "Every extra exception widens the runtime surface by one action, and dropping the "
            "permitted one denies the relay its only job",
        )
    )
    scoped = bool(statement.resources) and "*" not in statement.resources
    findings.append(
        Finding(
            "relay_denies_command_execution",
            f"{label} (resource scope)",
            "pass" if scoped else "fail",
            f"scoped to {list(statement.resources)}, not the whole account"
            if scoped
            else f"resources={list(statement.resources)}. An exclusion Deny on '*' denies every "
            "action but one ACCOUNT-WIDE, including the logs and ssm grants this same role holds",
        )
    )
    return findings


#: Blocks declared in `infra/**/*.tf`, as `block header -> (count, basis)`. Counts declared blocks,
#: whereas `EXPECTED_RESOURCE_COUNTS` counts plan instances.
DECLARED_INFRA_BLOCK_COUNTS: dict[str, tuple[int, str]] = {
    'resource "aws_iam_role_policy"': (
        22,
        "`eval_trigger_artifacts` is one. One was removed with eval_worker: "
        "`eval_trigger_invoke_worker`. Six more are `sweep_control_artifacts`, "
        "`sweep_task_invoke_runtime` and `eval_sweep` (`eval_sfn.tf`), `chat_target_invoke_runtime` "
        "and the mcp_chat_gateway module's `gateway`, and `eval_trigger_states`; the plan was "
        "re-captured with them. Before them, SIXTEEN, not seventeen: `eval_worker_invoke_tools` moved from an inline "
        "`aws_iam_role_policy` to a managed `aws_iam_policy` plus an "
        "`aws_iam_role_policy_attachment`, because IAM caps the AGGREGATE size of a role's inline "
        "policies at 10240 bytes and that role's four other inline policies already total ~8536 "
        "(dominated by the ~7766-byte DynamoDB grant, which enumerates every table and index ARN). "
        "Adding this document's ~1972 bytes inline made `PutRolePolicy` fail `LimitExceeded (409)` on "
        "a real apply. The permission set is unchanged -- only the attachment mechanism moved -- so "
        "this decrement is NOT a reduction in granted access, and both new types carry their own "
        "`EXPECTED_RESOURCE_COUNTS` rows. "
        "one `aws_iam_role_policy` block per distinct attachment site. TWO of them are the "
        "split of the AgentCore Runtime's permissions (`agentcore_runtime` holding ECR + Bedrock "
        "before the runtime exists, `agentcore_runtime_logs` holding CloudWatch after it does); the "
        "rest are the lambda_action module's four, the relay's four, the vpc module's flow-log "
        "delivery policy, and the root's gateway/eval attachments. THREE of the seventeen are VPC-related: "
        "the Hyperplane ENI grant each Lambda module's `vpc_config` requires (a green apply and a "
        "failure on every invocation without it) plus the flow-log role's. A block added here without a "
        "plan re-capture is invisible to EXPECTED_RESOURCE_COUNTS, which is why this count exists.",
    ),
    'resource "aws_kms_key_policy"': (
        3,
        "`aws_kms_key_policy.eval_artifacts` at the root, beside "
        "`dynamodb` and the frontend module's `frontend`; `EXPECTED_RESOURCE_COUNTS` expects the "
        "same three in the plan.",
    ),
    'data "aws_iam_policy_document"': (
        43,
        "`eval_trigger_artifacts` is one. Four were removed with eval_worker: "
        "`eval_trigger_invoke_worker`, "
        "`eval_worker_invoke_tools` and the deleted async_failure_destination module's two. "
        "Nine more are `sweep_control_artifacts`, `sweep_task_invoke_runtime`, "
        "`eval_sweep_assume_role` and `eval_sweep_permissions` (`eval_sfn.tf`), "
        "`chat_target_invoke_runtime` and the mcp_chat_gateway module's `assume_role` and "
        "`gateway_permissions`, and `states_endpoint` and `eval_trigger_states`; the plan was "
        "re-captured with them. Before them, 37: one `data aws_iam_policy_document` block per policy body authored in source. Includes the "
        "async_failure_destination module's two (the queue's non-TLS deny and the send grant), the "
        "eni_function_code_deny module's one, the "
        "second Runtime document (`agentcore_runtime_logs`), whose separation from "
        "`agentcore_runtime_permissions` is what breaks the runtime -> log-group -> policy ordering "
        "cycle, the four VPC documents (a `vpc_access` document in each of the two Lambda modules plus "
        "the vpc module's flow-log assume-role and delivery documents), and the "
        "private REST API's resource policy, which allows `execute-api:Invoke` from the named "
        "`execute-api` endpoints and explicitly DENIES it from any other `aws:sourceVpce`.",
    ),
}


def check_declared_infra_block_counts(_plan: Plan) -> list[Finding]:
    """Declared-block counts read from `infra/**/*.tf`, with no plan document involved."""
    findings: list[Finding] = []
    sources = [path for path in sorted(INFRA_DIR.rglob("*.tf")) if ".terraform" not in path.parts]
    if not sources:
        return [
            Finding(
                "declared_infra_block_counts",
                str(INFRA_DIR),
                "fail",
                "no infra/**/*.tf files were read, so every count below would be a vacuous zero",
            )
        ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    for block, (expected, basis) in sorted(DECLARED_INFRA_BLOCK_COUNTS.items()):
        count = len(re.findall(rf"^{re.escape(block)}", text, re.MULTILINE))
        findings.append(
            Finding(
                "declared_infra_block_counts",
                block,
                "pass" if count == expected else "fail",
                f"declared={count} expected={expected} -- basis: {basis}",
            )
        )
    return findings


#: The ARN path suffix of a runtime endpoint, wildcarded.
RUNTIME_ENDPOINTS_ARN_SUFFIX = "/runtime-endpoint/*"


def _module_string_locals(module_dir: str) -> dict[str, str]:
    """Simple `name = "literal"` assignments from every `locals` block under `module_dir`.

    `module_dir` is relative to `infra/`. Literal values only; one level of indirection.
    """
    resolved: dict[str, str] = {}
    for path in sorted((INFRA_DIR / module_dir).rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for block in re.finditer(r"^locals\s*\{(.*?)^\}", source, re.MULTILINE | re.DOTALL):
            for name, value in re.findall(
                r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"\n]*)"\s*$', block.group(1), re.MULTILINE
            ):
                resolved[name] = value
    return resolved


def _expand_local_references(reference: str, module_locals: dict[str, str]) -> str:
    """`local.x` (bare or interpolated) replaced by its literal, when one is knowable."""
    match = re.fullmatch(r"\$?\{?\s*local\.([A-Za-z_][A-Za-z0-9_]*)\s*\}?", reference.strip())
    if match is not None and match.group(1) in module_locals:
        return module_locals[match.group(1)]
    return reference


def check_relay_invoke_scoped_to_runtime_and_endpoint(_plan: Plan) -> list[Finding]:
    """The `InvokeAgentRuntime` Allow covers the runtime AND its endpoints, with no prefix wildcard.

    The relay addresses an endpoint, so both ARNs are needed; a trailing `*` directly on the ARN
    would match sibling runtimes.
    """
    findings: list[Finding] = []
    invoke_statements = [
        (document, statement)
        for document, statements in sorted(policy_document_statements(RELAY_MODULE_DIR).items())
        for statement in statements
        if statement.effect.lower() == "allow"
        and "bedrock-agentcore:InvokeAgentRuntime" in statement.actions
    ]
    if not invoke_statements:
        return [
            Finding(
                "relay_invoke_scoped_to_runtime_and_endpoint",
                RELAY_MODULE_DIR,
                "fail",
                "no Allow statement grants bedrock-agentcore:InvokeAgentRuntime; the relay cannot "
                "invoke the Runtime at all, or the grant moved out of this module unnoticed",
            )
        ]
    for document, statement in invoke_statements:
        if not statement.resources:
            findings.append(
                Finding(
                    "relay_invoke_scoped_to_runtime_and_endpoint",
                    document,
                    "fail",
                    "the statement declares no readable resources, so its scope cannot be checked",
                )
            )
            continue
        module_locals = _module_string_locals(RELAY_MODULE_DIR)
        resources = [_expand_local_references(r, module_locals) for r in statement.resources]
        suffix = RUNTIME_ENDPOINTS_ARN_SUFFIX
        runtime_refs = [r for r in resources if not r.endswith(suffix)]
        endpoint_refs = [r for r in resources if r.endswith(suffix)]
        # A trailing wildcard directly on the ARN, rather than under a `/` path separator.
        prefix_wildcards = [r for r in resources if r.endswith("*") and not r.endswith("/*")]
        # Compare base references with any `${...}` wrapper removed.
        same_base = {_normalise_reference(r.removesuffix(suffix)) for r in endpoint_refs} == {
            _normalise_reference(r) for r in runtime_refs
        }
        ok = bool(runtime_refs) and bool(endpoint_refs) and same_base and not prefix_wildcards
        findings.append(
            Finding(
                "relay_invoke_scoped_to_runtime_and_endpoint",
                document,
                "pass" if ok else "fail",
                f"runtime={runtime_refs!r} endpoint={endpoint_refs!r} "
                f"same_base={same_base} prefix_wildcards={prefix_wildcards!r}",
            )
        )
    return findings


def check_relay_no_forbidden_grants(_plan: Plan) -> list[Finding]:
    """The relay's role is granted none of the forbidden actions, prefixes or wildcards."""
    findings: list[Finding] = []
    documents = policy_document_statements(RELAY_MODULE_DIR)
    for document, statements in sorted(documents.items()):
        for statement in statements:
            if statement.effect.lower() != "allow":
                continue
            if not statement.actions_are_literal:
                findings.append(
                    Finding(
                        "relay_no_forbidden_grants",
                        f"{document}#{statement.sid or '(no sid)'}",
                        "fail",
                        "an Allow statement's actions are not literal strings, so the granted set "
                        "cannot be read from source; write the action names literally",
                    )
                )
                continue
            offending = [
                action
                for action in statement.actions
                if action in RELAY_FORBIDDEN_ALLOW_ACTIONS
                or action in RELAY_FORBIDDEN_ALLOW_WILDCARDS
                or any(action.startswith(prefix) for prefix in RELAY_FORBIDDEN_ALLOW_PREFIXES)
            ]
            findings.append(
                Finding(
                    "relay_no_forbidden_grants",
                    f"{document}#{statement.sid or '(no sid)'}",
                    "fail" if offending else "pass",
                    f"forbidden_actions_granted={offending!r}"
                    if offending
                    else f"actions={list(statement.actions)!r}",
                )
            )
    findings.append(
        Finding(
            "relay_no_forbidden_grants",
            f"(coverage) Allow statements in {RELAY_MODULE_DIR}",
            "pass" if documents else "fail",
            f"documents={len(documents)}",
        )
    )
    return findings


#: The root local both Runtime resource-policy documents take their principals from.
RUNTIME_INVOKER_LOCAL = "runtime_invoker_role_arns"

#: Exactly the roles that local may name: the relay, `sweep_task` and `chat_target`.
RUNTIME_INVOKER_ROLE_REFERENCES: tuple[str, ...] = (
    "module.agent_relay.role_arn",
    "module.lambda_action_sweep_task.role_arn",
    "module.lambda_action_chat_target.role_arn",
)


#: The `sid` of each Runtime document's principal Deny.
RUNTIME_PRINCIPAL_DENY_SID = "DenyRuntimeDataPlaneToAnyOtherPrincipal"

#: Every Runtime data-plane action AgentCore resource policies support; the Deny names all of them.
RUNTIME_DATA_PLANE_ACTIONS: tuple[str, ...] = (
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
    "bedrock-agentcore:StopRuntimeSession",
    "bedrock-agentcore:GetAgentCard",
)

_CONDITION_HEADER = re.compile(r"^\s*condition\s*\{", re.MULTILINE)


def _static_statement_conditions(
    document_key: str,
) -> list[tuple[str, tuple[tuple[str, str, tuple[str, ...]], ...]]]:
    """`(sid, ((test, variable, values), ...))` for each static `statement` of one document.

    `values` entries are quoted contents for literals and raw source text for references.
    """
    found: list[tuple[str, tuple[tuple[str, str, tuple[str, ...]], ...]]] = []
    for directory, name, body in tf_resource_blocks("aws_iam_policy_document", kind="data"):
        if f"{directory}/{name}" != document_key:
            continue
        for statement_body in _block_bodies(body, _STATEMENT_HEADER):
            conditions = tuple(
                (
                    _hcl_scalar(condition, "test"),
                    _hcl_scalar(condition, "variable"),
                    _hcl_string_list(condition, "values")[0],
                )
                for condition in _block_bodies(statement_body, _CONDITION_HEADER)
            )
            found.append((_hcl_scalar(statement_body, "sid"), conditions))
    return found


def _runtime_principal_deny_problems(
    statement: TerraformStatement,
    conditions: tuple[tuple[str, str, tuple[str, ...]], ...],
    resource_arn: str,
) -> list[str]:
    """Why `statement` is not the principal Deny: every data-plane action, `*`, invokers excluded."""
    problems: list[str] = []
    if tuple(statement.actions) != RUNTIME_DATA_PLANE_ACTIONS or not statement.actions_are_literal:
        problems.append(
            f"deny actions={list(statement.actions)!r}, expected {list(RUNTIME_DATA_PLANE_ACTIONS)!r}"
        )
    if list(statement.principal_identifiers) != ["*"]:
        problems.append(
            f"deny principals={list(statement.principal_identifiers)!r}, expected ['*']"
        )
    if list(statement.resources) != [resource_arn]:
        problems.append(
            f"deny resources={list(statement.resources)!r} != resource_arn {resource_arn!r}"
        )
    expected = (("ArnNotEquals", "aws:PrincipalArn", (f"local.{RUNTIME_INVOKER_LOCAL}",)),)
    if conditions != expected:
        problems.append(
            f"deny conditions={list(conditions)!r}, expected exactly ArnNotEquals aws:PrincipalArn "
            f"= local.{RUNTIME_INVOKER_LOCAL}; any other exclusion lets an unlisted principal "
            "invoke on an identity-policy Allow alone (G-8)"
        )
    return problems


def _root_hcl_reference_list(local_name: str) -> list[str]:
    """The unquoted reference elements of a `local.<name> = [...]` list in `infra/main.tf`.

    Comments are blanked before the list is located, and the list closes at the local's own
    two-space-indented `]`, so a bracket inside a comment can neither end nor extend it. Raises
    when the local is absent. Quoted literals keep their quotes.
    """
    source = blank_comments((INFRA_DIR / "main.tf").read_text(encoding="utf-8"))
    match = re.search(rf"(?ms)^  {re.escape(local_name)}\s*=\s*\[\n(.*?)^  \]$", source)
    if match is None:
        raise ValueError(f"infra/main.tf declares no `{local_name}` list local")
    return [entry.strip() for entry in match.group(1).split(",") if entry.strip()]


def check_runtime_resource_policy_sole_invoker(_plan: Plan) -> list[Finding]:
    """Two Runtime resource policies — runtime and endpoint — each admitting exactly the invoker roles.

    Each document's static statements are one Allow naming `local.runtime_invoker_role_arns` and one
    `RUNTIME_PRINCIPAL_DENY_SID` Deny of `RUNTIME_DATA_PLANE_ACTIONS` to `*` unless
    `aws:PrincipalArn` is in that local; the local is exactly `RUNTIME_INVOKER_ROLE_REFERENCES`.
    """
    findings: list[Finding] = []
    try:
        invokers = _root_hcl_reference_list(RUNTIME_INVOKER_LOCAL)
    except ValueError as exc:
        invokers = []
        findings.append(
            Finding(
                "runtime_resource_policy_sole_invoker",
                f"local.{RUNTIME_INVOKER_LOCAL}",
                "fail",
                str(exc),
            )
        )
    else:
        findings.append(
            Finding(
                "runtime_resource_policy_sole_invoker",
                f"local.{RUNTIME_INVOKER_LOCAL}",
                "pass" if sorted(invokers) == sorted(RUNTIME_INVOKER_ROLE_REFERENCES) else "fail",
                f"elements={invokers!r} expected exactly {list(RUNTIME_INVOKER_ROLE_REFERENCES)!r}; "
                "any other principal can invoke the Runtime and assert any identity",
            )
        )
    policies = tf_resource_blocks("aws_bedrockagentcore_resource_policy")
    documents = policy_document_statements()

    findings.append(
        Finding(
            "runtime_resource_policy_sole_invoker",
            "(count) aws_bedrockagentcore_resource_policy",
            "pass" if len(policies) == 2 else "fail",
            f"found={len(policies)} expected=2 (one for the runtime ARN, one for the endpoint ARN); "
            "a single policy leaves the other ARN open to every principal in the account",
        )
    )

    seen_arns: list[str] = []
    for module_dir, name, body in policies:
        address = f"{module_dir}/aws_bedrockagentcore_resource_policy.{name}"
        resource_arn = _hcl_expression(body, "resource_arn")
        seen_arns.append(resource_arn)
        document_ref = _hcl_expression(body, "policy")
        key = _policy_document_key(module_dir, document_ref, documents)
        if key is None:
            findings.append(
                Finding(
                    "runtime_resource_policy_sole_invoker",
                    address,
                    "fail",
                    f"could not resolve its policy document from {document_ref!r}",
                )
            )
            continue
        statements = documents[key]
        problems: list[str] = []
        conditions_by_sid = dict(_static_statement_conditions(key))
        denies = [s for s in statements if s.effect.lower() == "deny"]
        allows = [s for s in statements if s.effect.lower() != "deny"]
        if len(allows) != 1 or len(denies) != 1:
            problems.append(
                f"{len(allows)} Allow and {len(denies)} Deny statements, expected exactly 1 of each"
            )
        for statement in denies:
            if statement.sid != RUNTIME_PRINCIPAL_DENY_SID:
                problems.append(
                    f"deny sid={statement.sid!r}, expected {RUNTIME_PRINCIPAL_DENY_SID!r}"
                )
            problems.extend(
                _runtime_principal_deny_problems(
                    statement, conditions_by_sid.get(statement.sid, ()), resource_arn
                )
            )
        for statement in allows:
            if statement.effect.lower() != "allow":
                problems.append(f"effect={statement.effect}")
            if tuple(statement.actions) != ("bedrock-agentcore:InvokeAgentRuntime",):
                problems.append(f"actions={list(statement.actions)!r}")
            if "*" in statement.principal_identifiers:
                problems.append("principal '*' admits every principal in the account")
            if list(statement.principal_identifiers) != [f"local.{RUNTIME_INVOKER_LOCAL}"]:
                problems.append(
                    f"principals={list(statement.principal_identifiers)!r}, expected exactly "
                    f"local.{RUNTIME_INVOKER_LOCAL}"
                )
            if list(statement.resources) != [resource_arn]:
                problems.append(
                    f"resources={list(statement.resources)!r} != resource_arn {resource_arn!r}"
                )
        findings.append(
            Finding(
                "runtime_resource_policy_sole_invoker",
                address,
                "fail" if problems else "pass",
                "; ".join(problems)
                if problems
                else f"only local.{RUNTIME_INVOKER_LOCAL} ({len(invokers)} roles) on {resource_arn}; "
                f"{RUNTIME_PRINCIPAL_DENY_SID} denies {len(RUNTIME_DATA_PLANE_ACTIONS)} data-plane "
                "actions to every other principal",
            )
        )

    findings.append(
        Finding(
            "runtime_resource_policy_sole_invoker",
            "(distinct ARNs)",
            "pass" if len(set(seen_arns)) == len(seen_arns) and seen_arns else "fail",
            f"resource_arns={seen_arns!r}; two policies on the same ARN would leave one open",
        )
    )
    return findings


#: Where the table and CMK policies live, relative to `infra/`.
DYNAMODB_MODULE_DIR = "modules/dynamodb"

#: The module's two principal lists, as the policies reference them.
DATA_PLANE_PRINCIPALS_VAR = "var.data_plane_principal_arns"
ADMINISTRATOR_PRINCIPALS_VAR = "var.administrator_principal_arns"

#: Table resource-policy statements (ordered action tuples): sid -> ordered actions.
DYNAMODB_TABLE_DATA_PLANE_SID = "DenyDataPlaneToNonProjectPrincipals"
DYNAMODB_TABLE_DATA_PLANE_ACTIONS: tuple[str, ...] = (
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:ConditionCheckItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:PartiQLSelect",
    "dynamodb:PartiQLInsert",
    "dynamodb:PartiQLUpdate",
    "dynamodb:PartiQLDelete",
    "dynamodb:CreateBackup",
    "dynamodb:ExportTableToPointInTime",
    "dynamodb:RestoreTableToPointInTime",
    "dynamodb:EnableKinesisStreamingDestination",
)
DYNAMODB_TABLE_ADMIN_SID = "DenyTableAdministrationToNonOperators"
DYNAMODB_TABLE_ADMIN_ACTIONS: tuple[str, ...] = (
    "dynamodb:DeleteTable",
    "dynamodb:UpdateTable",
    "dynamodb:UpdateContinuousBackups",
    "dynamodb:UpdateTimeToLive",
    "dynamodb:PutResourcePolicy",
    "dynamodb:DeleteResourcePolicy",
    "dynamodb:TagResource",
    "dynamodb:UntagResource",
    "dynamodb:UpdateContributorInsights",
)

#: CMK key-policy statements (root and administration statements).
DYNAMODB_KEY_ROOT_SID = "Enable IAM User Permissions"
DYNAMODB_KEY_USE_SID = "DenyKeyUseToNonProjectPrincipals"
DYNAMODB_KEY_USE_ACTIONS: tuple[str, ...] = (
    "kms:Decrypt",
    "kms:Encrypt",
    "kms:GenerateDataKey*",
    "kms:ReEncrypt*",
    "kms:CreateGrant",
)
DYNAMODB_KEY_ADMIN_SID = "DenyKeyAdministrationToNonOperators"
DYNAMODB_KEY_ADMIN_ACTIONS: tuple[str, ...] = (
    "kms:PutKeyPolicy",
    "kms:ScheduleKeyDeletion",
    "kms:DisableKey",
    "kms:RevokeGrant",
    "kms:DisableKeyRotation",
)

#: The root local listing every data-plane Lambda role, and the operator variable.
DYNAMODB_DATA_PLANE_LOCAL = "dynamodb_data_plane_role_arns"
CRM_OPERATOR_PRINCIPALS_VAR = "var.crm_operator_principal_arns"
_LAMBDA_ACTION_SOURCE = "./modules/lambda_action"

_ROOT_ARN_TEMPLATE = re.compile(
    r"^arn:(aws|\$\{data\.aws_partition\.current\.partition\}):iam::\$\{var\.account_id\}:root$"
)


@dataclass(frozen=True)
class HclExpr:
    """An unquoted HCL expression (reference, function call, `true`/`false`), as written."""

    text: str


HclValue = Any  # str | HclExpr | list[HclValue] | dict[str, HclValue]


def _skip_hcl_string(text: str, index: int) -> int:
    """Index just past the quoted string starting at `text[index] == '"'`, templates included."""
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        if text.startswith("${", index):
            depth = 0
            while index < len(text):
                if text[index] == '"':
                    index = _skip_hcl_string(text, index)
                    continue
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            continue
        index += 1
    raise ValueError("unterminated string literal")


def _parse_hcl_value(text: str, index: int) -> tuple[HclValue, int]:
    """Parse one HCL value (object, tuple, string or bare expression) starting at `index`."""
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    if index >= len(text):
        raise ValueError("expected a value, found end of text")
    char = text[index]
    if char == "{":
        obj: dict[str, HclValue] = {}
        index += 1
        while True:
            while index < len(text) and text[index] in " \t\r\n,":
                index += 1
            if index >= len(text):
                raise ValueError("unterminated object")
            if text[index] == "}":
                return obj, index + 1
            if text[index] == '"':
                end = _skip_hcl_string(text, index)
                key = text[index + 1 : end - 1]
                index = end
            else:
                match = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*").match(text, index)
                if match is None:
                    raise ValueError(f"expected an object key at {text[index : index + 20]!r}")
                key = match.group(0)
                index = match.end()
            while index < len(text) and text[index] in " \t":
                index += 1
            if index >= len(text) or text[index] not in "=:":
                raise ValueError(f"expected '=' after key {key!r}")
            obj[key], index = _parse_hcl_value(text, index + 1)
    if char == "[":
        items: list[HclValue] = []
        index += 1
        while True:
            while index < len(text) and text[index] in " \t\r\n,":
                index += 1
            if index >= len(text):
                raise ValueError("unterminated tuple")
            if text[index] == "]":
                return items, index + 1
            item, index = _parse_hcl_value(text, index)
            items.append(item)
    if char == '"':
        end = _skip_hcl_string(text, index)
        return text[index + 1 : end - 1], end
    start = index
    depth = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _skip_hcl_string(text, index)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and char in ",\n":
            break
        index += 1
    return HclExpr(text[start:index].strip()), index


def _hcl_call_argument(body: str, attribute: str, function: str) -> HclValue | None:
    """The parsed first argument of `<attribute> = <function>(...)` in `body`, or None."""
    source = blank_comments(body)
    match = re.search(rf"^\s*{re.escape(attribute)}\s*=\s*{re.escape(function)}\(", source, re.M)
    if match is None:
        return None
    value, _ = _parse_hcl_value(source, match.end())
    return value


@dataclass(frozen=True)
class RenderedStatement:
    """One policy statement normalised to its rendered-JSON shape, whichever HCL form wrote it.

    `actions`/`resources` are tuples whatever the source wrote (a scalar or a list); condition
    values likewise. Literal strings stay `str`; references are `HclExpr`.
    """

    sid: str
    effect: str
    principal: HclValue
    actions: tuple[HclValue, ...]
    resources: tuple[HclValue, ...]
    conditions: dict[str, dict[str, tuple[HclValue, ...]]]
    extra_keys: tuple[str, ...] = ()


_STATEMENT_KEYS = frozenset({"Sid", "Effect", "Principal", "Action", "Resource", "Condition"})


def _hcl_as_tuple(value: HclValue) -> tuple[HclValue, ...]:
    return tuple(value) if isinstance(value, list) else (value,)


def _rendered_from_jsonencode(document: HclValue) -> list[RenderedStatement]:
    if not isinstance(document, dict) or not isinstance(document.get("Statement"), list):
        raise ValueError("the jsonencode document has no Statement list")
    statements: list[RenderedStatement] = []
    for raw in document["Statement"]:
        if not isinstance(raw, dict):
            raise ValueError("a Statement entry is not an object")
        conditions: dict[str, dict[str, tuple[HclValue, ...]]] = {}
        condition = raw.get("Condition", {})
        if not isinstance(condition, dict):
            raise ValueError("a Condition is not an object literal")
        for test, keys in condition.items():
            if not isinstance(keys, dict):
                raise ValueError(f"condition {test} is not an object literal")
            conditions[test] = {key: _hcl_as_tuple(value) for key, value in keys.items()}
        statements.append(
            RenderedStatement(
                sid=raw.get("Sid", "") if isinstance(raw.get("Sid", ""), str) else "",
                effect=raw.get("Effect", "") if isinstance(raw.get("Effect", ""), str) else "",
                principal=raw.get("Principal"),
                actions=_hcl_as_tuple(raw.get("Action", [])),
                resources=_hcl_as_tuple(raw.get("Resource", [])),
                conditions=conditions,
                extra_keys=tuple(sorted(set(raw) - _STATEMENT_KEYS)),
            )
        )
    return statements


def _hcl_list_values(body: str, name: str) -> tuple[HclValue, ...]:
    values, _ = _hcl_string_list(body, name)
    quoted = set(re.findall(r'"((?:[^"\\]|\\.)*)"', body))
    return tuple(v if v in quoted else HclExpr(v) for v in values)


def _rendered_from_policy_document(body: str) -> list[RenderedStatement]:
    statements: list[RenderedStatement] = []
    for statement_body in _block_bodies(body, _STATEMENT_HEADER):
        principals: dict[str, tuple[HclValue, ...]] = {}
        for principal_body in _block_bodies(
            statement_body, re.compile(r"^\s*principals\s*\{", re.MULTILINE)
        ):
            principals[_hcl_scalar(principal_body, "type")] = _hcl_list_values(
                principal_body, "identifiers"
            )
        principal: HclValue = principals
        if principals == {"*": ("*",)}:
            principal = "*"
        conditions: dict[str, dict[str, tuple[HclValue, ...]]] = {}
        for condition in _block_bodies(statement_body, _CONDITION_HEADER):
            test = _hcl_scalar(condition, "test")
            conditions.setdefault(test, {})[_hcl_scalar(condition, "variable")] = _hcl_list_values(
                condition, "values"
            )
        statements.append(
            RenderedStatement(
                sid=_hcl_scalar(statement_body, "sid"),
                effect=_hcl_scalar(statement_body, "effect") or "Allow",
                principal=principal,
                actions=_hcl_list_values(statement_body, "actions"),
                resources=_hcl_list_values(statement_body, "resources"),
                conditions=conditions,
            )
        )
    return statements


def rendered_policy_statements(module_dir: str, body: str) -> list[RenderedStatement]:
    """The statements a resource's `policy` argument renders, from `jsonencode` or a policy document.

    Raises `ValueError` when the argument is neither form or cannot be read.
    """
    document = _hcl_call_argument(body, "policy", "jsonencode")
    if document is not None:
        return _rendered_from_jsonencode(document)
    reference = _hcl_expression(body, "policy")
    for directory, name, document_body in tf_resource_blocks(
        "aws_iam_policy_document", kind="data"
    ):
        if directory == module_dir and re.fullmatch(
            rf"data\.aws_iam_policy_document\.{re.escape(name)}\.json", reference
        ):
            return _rendered_from_policy_document(document_body)
    raise ValueError(f"policy = {reference!r} is neither jsonencode(...) nor a policy document")


def _principal_condition(variable: str) -> dict[str, dict[str, tuple[HclValue, ...]]]:
    return {"ArnNotEquals": {"aws:PrincipalArn": (HclExpr(variable),)}}


def _deny_statement_problems(
    statement: RenderedStatement,
    actions: tuple[str, ...],
    conditions: dict[str, dict[str, tuple[HclValue, ...]]],
) -> list[str]:
    problems: list[str] = []
    if statement.effect != "Deny":
        problems.append(f"effect={statement.effect!r}, expected 'Deny'")
    if statement.principal != "*":
        problems.append(f"principal={statement.principal!r}, expected the string '*'")
    if statement.actions != actions:
        problems.append(f"actions={list(statement.actions)!r}, expected exactly {list(actions)!r}")
    if statement.conditions != conditions:
        problems.append(
            f"conditions={statement.conditions!r}, expected exactly {conditions!r}; an IfExists or "
            "negated operator, another key, or any extra exemption changes who the Deny reaches"
        )
    if statement.extra_keys:
        problems.append(f"unexpected statement keys {list(statement.extra_keys)!r}")
    return problems


def _table_resource_problems(resources: tuple[HclValue, ...]) -> list[str]:
    """The table policy's resources must be one per-table ARN and that ARN's `/index/*`."""
    if len(resources) != 2:
        return [f"resources={list(resources)!r}, expected the table ARN and its /index/*"]
    table, index = resources
    table_text = f"${{{table.text}}}" if isinstance(table, HclExpr) else str(table)
    if "each." not in table_text:
        return [f"resource {table_text!r} is not per-table (no each.key / each.value)"]
    if index != f"{table_text}/index/*":
        return [f"resources={list(resources)!r}: the second must be {table_text + '/index/*'!r}"]
    return []


def _lifecycle_guard_problems(body: str) -> list[str]:
    """The resource's precondition: the caller is a listed administrator and not the root."""
    source = re.sub(r"\s+", " ", blank_comments(body))
    if "precondition {" not in source:
        return ["no lifecycle precondition; an unlisted caller could apply and lock itself out"]
    problems = []
    if f"contains({ADMINISTRATOR_PRINCIPALS_VAR}, var.caller_principal_arn)" not in source:
        problems.append(
            f"the precondition does not require contains({ADMINISTRATOR_PRINCIPALS_VAR}, "
            "var.caller_principal_arn)"
        )
    if not re.search(
        r'var\.caller_principal_arn != "arn:[^"]*:iam::\$\{var\.account_id\}:root"', source
    ):
        problems.append("the precondition does not exclude the account root as the caller")
    return problems


def _flag_absent_or_false(body: str, attribute: str) -> str | None:
    """None when `attribute` is absent or a literal `false`; otherwise its value as written."""
    value = _hcl_expression(blank_comments(body), attribute)
    return None if value in ("", "false") else value


def _lambda_action_grant_modules() -> tuple[set[str], set[str]]:
    """`(with_grant, without_grant)`: root `lambda_action` module calls by `dynamodb_kms_grant_enabled`."""
    with_grant: set[str] = set()
    without_grant: set[str] = set()
    for path in sorted(INFRA_DIR.glob("*.tf")):
        source = blank_comments(path.read_text(encoding="utf-8"))
        for match in re.finditer(r'^module\s+"([A-Za-z0-9_]+)"\s*\{', source, re.MULTILINE):
            body = _block_bodies(source[match.start() :], re.compile(r"^module\s", re.MULTILINE))[0]
            if _hcl_expression(body, "source") != _LAMBDA_ACTION_SOURCE:
                continue
            granted = re.search(r"^\s*dynamodb_kms_grant_enabled\s*=\s*true\s*$", body, re.M)
            (with_grant if granted else without_grant).add(f"module.{match.group(1)}")
    return with_grant, without_grant


def _root_local_expression(local_name: str) -> str:
    """The right-hand side of `local_name = ...` in the root `.tf` files, comments blanked."""
    for path in sorted(INFRA_DIR.glob("*.tf")):
        source = blank_comments(path.read_text(encoding="utf-8"))
        match = re.search(rf"^  {re.escape(local_name)}\s*=", source, re.MULTILINE)
        if match is None:
            continue
        value, _ = _parse_hcl_value(source, match.end())
        return value.text if isinstance(value, HclExpr) else json.dumps(value)
    raise ValueError(f"no root .tf declares local `{local_name}`")


def _data_plane_local_modules(expression: str) -> tuple[set[str], list[str]]:
    """`(modules, unrecognised)`: the `module.<name>` role ARNs a data-plane local names."""
    modules: set[str] = set()
    for_expr = re.compile(r"\[\s*for\s+(\w+)\s+in\s+(module\.\w+)\s*:\s*\1\.role_arn\s*\]")
    modules.update(m.group(2) for m in for_expr.finditer(expression))
    rest = for_expr.sub(" ", expression)
    rest = re.sub(r"\bconcat\(|[\[\](),]", " ", rest)
    unrecognised: list[str] = []
    for token in rest.split():
        match = re.fullmatch(r"(module\.\w+)\.role_arn", token)
        if match:
            modules.add(match.group(1))
        else:
            unrecognised.append(token)
    return modules, unrecognised


def _module_call_body(name: str) -> str | None:
    for path in sorted(INFRA_DIR.glob("*.tf")):
        source = blank_comments(path.read_text(encoding="utf-8"))
        match = re.search(rf'^module\s+"{re.escape(name)}"\s*\{{', source, re.MULTILINE)
        if match:
            return _block_bodies(source[match.start() :], re.compile(r"^module\s", re.M))[0]
    return None


def _root_wiring_findings() -> list[Finding]:
    control = "dynamodb_data_plane_principal_deny"
    findings: list[Finding] = []
    body = _module_call_body("dynamodb")
    if body is None:
        return [
            Finding(
                control, 'module "dynamodb"', "fail", 'infra/*.tf declares no module "dynamodb"'
            )
        ]
    root_arn = '"arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"'
    expected = {
        "data_plane_principal_arns": (
            f"concat(local.{DYNAMODB_DATA_PLANE_LOCAL}, {CRM_OPERATOR_PRINCIPALS_VAR})"
        ),
        "administrator_principal_arns": f"concat({CRM_OPERATOR_PRINCIPALS_VAR}, [{root_arn}])",
        "caller_principal_arn": "data.aws_iam_session_context.current.issuer_arn",
    }
    for argument, wanted in expected.items():
        written = re.sub(r"\s+", " ", _hcl_expression(body, argument).strip())
        written = re.sub(r"\(\s+|\s+\)", lambda m: m.group(0).strip(), written)
        findings.append(
            Finding(
                control,
                f"module.dynamodb.{argument}",
                "pass" if written == wanted else "fail",
                f"written={written!r} expected={wanted!r}; the operators belong on both lists and "
                "the account root on the administrator list only",
            )
        )
    session = [
        b
        for d, n, b in tf_resource_blocks("aws_iam_session_context", kind="data")
        if d == "." and n == "current"
    ]
    session_ok = bool(session) and (
        _hcl_expression(session[0], "arn") == "data.aws_caller_identity.current.arn"
    )
    findings.append(
        Finding(
            control,
            "data.aws_iam_session_context.current",
            "pass" if session_ok else "fail",
            "resolves the caller's issuer ARN from data.aws_caller_identity.current.arn"
            if session_ok
            else "absent, or its arn is not data.aws_caller_identity.current.arn",
        )
    )
    root_variables = "\n".join(
        blank_comments(p.read_text(encoding="utf-8")) for p in sorted(INFRA_DIR.glob("*.tf"))
    )
    match = re.search(r'^variable\s+"crm_operator_principal_arns"\s*\{', root_variables, re.M)
    if match is None:
        problem = "infra/*.tf declares no variable crm_operator_principal_arns"
    else:
        block = _block_bodies(root_variables[match.start() :], re.compile(r"^variable\s", re.M))[0]
        problem = ""
        if re.search(r"^\s*default\s*=", block, re.M):
            problem = "it has a default; the operator list must be supplied per deployment"
        elif "validation {" not in re.sub(r"\s+", " ", block):
            problem = "it has no validation"
    findings.append(
        Finding(
            control,
            CRM_OPERATOR_PRINCIPALS_VAR,
            "fail" if problem else "pass",
            problem or "no default, validated",
        )
    )
    return findings


def _data_plane_local_finding() -> Finding:
    control = "dynamodb_data_plane_principal_deny"
    resource = f"local.{DYNAMODB_DATA_PLANE_LOCAL}"
    try:
        expression = _root_local_expression(DYNAMODB_DATA_PLANE_LOCAL)
    except ValueError as exc:
        return Finding(control, resource, "fail", str(exc))
    named, unrecognised = _data_plane_local_modules(expression)
    granted, _ = _lambda_action_grant_modules()
    problems = []
    if unrecognised:
        problems.append(f"entries that are not a module role ARN: {unrecognised!r}")
    if granted - named:
        problems.append(
            f"grant-holding modules missing from the local: {sorted(granted - named)!r}"
        )
    if named - granted:
        problems.append(
            f"entries without dynamodb_kms_grant_enabled = true: {sorted(named - granted)!r}"
        )
    if not granted:
        problems.append("no lambda_action call sets dynamodb_kms_grant_enabled = true")
    return Finding(
        control,
        resource,
        "fail" if problems else "pass",
        "; ".join(problems)
        if problems
        else f"names exactly the {len(granted)} lambda_action calls holding the DynamoDB/KMS grant",
    )


def _policy_findings(
    resource_type: str,
    name: str,
    expected: list[tuple[str, Callable[[RenderedStatement], list[str]]]],
) -> list[Finding]:
    control = "dynamodb_data_plane_principal_deny"
    blocks = [
        (d, n, b) for d, n, b in tf_resource_blocks(resource_type) if d == DYNAMODB_MODULE_DIR
    ]
    address = f"{DYNAMODB_MODULE_DIR}/{resource_type}.{name}"
    count_finding = Finding(
        control,
        f"(count) {DYNAMODB_MODULE_DIR}/{resource_type}",
        "pass" if [n for _, n, _ in blocks] == [name] else "fail",
        f"found={[n for _, n, _ in blocks]!r} expected exactly [{name!r}]",
    )
    if count_finding.status == "fail":
        return [count_finding]
    findings = [count_finding]
    body = blocks[0][2]
    try:
        statements = rendered_policy_statements(DYNAMODB_MODULE_DIR, body)
    except ValueError as exc:
        return [*findings, Finding(control, address, "fail", f"policy unreadable: {exc}")]
    sids = [s.sid for s in statements]
    wanted_sids = [sid for sid, _ in expected]
    findings.append(
        Finding(
            control,
            f"{address} (statements)",
            "pass" if sids == wanted_sids else "fail",
            f"sids={sids!r} expected exactly {wanted_sids!r}",
        )
    )
    by_sid = {s.sid: s for s in statements}
    for sid, check in expected:
        statement = by_sid.get(sid)
        problems = ["statement absent"] if statement is None else check(statement)
        findings.append(
            Finding(
                control,
                f"{address}#{sid}",
                "fail" if problems else "pass",
                "; ".join(problems) if problems else "matches the expected policy statements",
            )
        )
    guard = _lifecycle_guard_problems(body)
    findings.append(
        Finding(
            control,
            f"{address} (precondition)",
            "fail" if guard else "pass",
            "; ".join(guard)
            if guard
            else "refuses a plan whose caller is not a listed administrator, or is the account root",
        )
    )
    return findings


def check_dynamodb_data_plane_principal_deny(_plan: Plan) -> list[Finding]:
    """The table and CMK policies deny the data plane, key use and administration to every
    principal outside the project's Lambda roles and the operators. Read from source."""
    control = "dynamodb_data_plane_principal_deny"
    data_plane = _principal_condition(DATA_PLANE_PRINCIPALS_VAR)
    administrators = _principal_condition(ADMINISTRATOR_PRINCIPALS_VAR)
    key_use = {**data_plane, "Bool": {"aws:PrincipalIsAWSService": ("false",)}}

    def table_check(
        actions: tuple[str, ...], conditions: dict[str, dict[str, tuple[HclValue, ...]]]
    ) -> Callable[[RenderedStatement], list[str]]:
        return lambda s: (
            _deny_statement_problems(s, actions, conditions) + _table_resource_problems(s.resources)
        )

    def key_check(
        actions: tuple[str, ...], conditions: dict[str, dict[str, tuple[HclValue, ...]]]
    ) -> Callable[[RenderedStatement], list[str]]:
        def check(statement: RenderedStatement) -> list[str]:
            problems = _deny_statement_problems(statement, actions, conditions)
            if statement.resources != ("*",):
                problems.append(f"resources={list(statement.resources)!r}, expected ['*']")
            return problems

        return check

    def root_check(statement: RenderedStatement) -> list[str]:
        problems: list[str] = []
        if statement.effect != "Allow":
            problems.append(f"effect={statement.effect!r}, expected 'Allow'")
        principal = statement.principal
        if not isinstance(principal, dict) or set(principal) != {"AWS"}:
            problems.append(f"principal={principal!r}, expected {{'AWS': <account root>}}")
        else:
            arn = principal["AWS"]
            arn = arn[0] if isinstance(arn, (tuple, list)) and len(arn) == 1 else arn
            if not (isinstance(arn, str) and _ROOT_ARN_TEMPLATE.match(arn)):
                problems.append(f"principal AWS={arn!r} is not the account root")
        if statement.actions != ("kms:*",) or statement.resources != ("*",):
            problems.append(
                f"actions={list(statement.actions)!r} resources={list(statement.resources)!r}, "
                "expected kms:* on *"
            )
        if statement.conditions:
            problems.append(f"conditions={statement.conditions!r}, expected none")
        return problems

    findings = _policy_findings(
        "aws_dynamodb_resource_policy",
        "tables",
        [
            (
                DYNAMODB_TABLE_DATA_PLANE_SID,
                table_check(DYNAMODB_TABLE_DATA_PLANE_ACTIONS, data_plane),
            ),
            (DYNAMODB_TABLE_ADMIN_SID, table_check(DYNAMODB_TABLE_ADMIN_ACTIONS, administrators)),
        ],
    )
    table_blocks = [
        b
        for d, n, b in tf_resource_blocks("aws_dynamodb_resource_policy")
        if d == DYNAMODB_MODULE_DIR and n == "tables"
    ]
    if table_blocks:
        for_each = _hcl_expression(table_blocks[0], "for_each")
        findings.append(
            Finding(
                control,
                f"{DYNAMODB_MODULE_DIR}/aws_dynamodb_resource_policy.tables (for_each)",
                "pass" if for_each == "local.tables" else "fail",
                f"for_each={for_each!r} expected 'local.tables': one policy per table",
            )
        )
        confirm = _flag_absent_or_false(table_blocks[0], "confirm_remove_self_resource_access")
        findings.append(
            Finding(
                control,
                f"{DYNAMODB_MODULE_DIR}/aws_dynamodb_resource_policy.tables "
                "(confirm_remove_self_resource_access)",
                "fail" if confirm else "pass",
                f"set to {confirm!r}; it must be absent or false" if confirm else "absent or false",
            )
        )
    findings += _policy_findings(
        "aws_kms_key_policy",
        "dynamodb",
        [
            (DYNAMODB_KEY_ROOT_SID, root_check),
            (DYNAMODB_KEY_USE_SID, key_check(DYNAMODB_KEY_USE_ACTIONS, key_use)),
            (DYNAMODB_KEY_ADMIN_SID, key_check(DYNAMODB_KEY_ADMIN_ACTIONS, administrators)),
        ],
    )
    key_blocks = [
        b
        for d, n, b in tf_resource_blocks("aws_kms_key_policy")
        if d == DYNAMODB_MODULE_DIR and n == "dynamodb"
    ]
    if key_blocks:
        key_id = _hcl_expression(key_blocks[0], "key_id")
        bypass = _flag_absent_or_false(key_blocks[0], "bypass_policy_lockout_safety_check")
        problems = []
        if key_id != "aws_kms_key.dynamodb.id":
            problems.append(f"key_id={key_id!r}, expected aws_kms_key.dynamodb.id")
        if bypass:
            problems.append(
                f"bypass_policy_lockout_safety_check={bypass!r}, must be absent or false"
            )
        findings.append(
            Finding(
                control,
                f"{DYNAMODB_MODULE_DIR}/aws_kms_key_policy.dynamodb (key_id, lockout check)",
                "fail" if problems else "pass",
                "; ".join(problems) or "on the table CMK, lockout safety check kept",
            )
        )
    findings.append(_data_plane_local_finding())
    findings += _root_wiring_findings()
    return findings


#: Group G store-policy tuples, shared with `scripts/smoke/store_principal_deny.py` and the
#: bootstrap's rendered documents.
STORE_KEY_ADMIN_ACTIONS: tuple[str, ...] = (
    *DYNAMODB_KEY_ADMIN_ACTIONS,
    "kms:CreateAlias",
    "kms:UpdateAlias",
    "kms:DeleteAlias",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:UpdateKeyDescription",
)
S3_OBJECT_READ_ACTIONS: tuple[str, ...] = (
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:GetObjectAttributes",
    "s3:GetObjectVersionAttributes",
    "s3:GetObjectTagging",
    "s3:GetObjectVersionTagging",
    "s3:RestoreObject",
    "s3:ListBucket",
    "s3:ListBucketVersions",
    "s3:ListBucketMultipartUploads",
    "s3:ListMultipartUploadParts",
)
S3_OBJECT_WRITE_ACTIONS: tuple[str, ...] = (
    "s3:PutObject",
    "s3:PutObjectTagging",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:DeleteObjectTagging",
    "s3:AbortMultipartUpload",
    "s3:PutObjectVersionTagging",
    "s3:DeleteObjectVersionTagging",
)
S3_BUCKET_ADMIN_ACTIONS: tuple[str, ...] = (
    "s3:DeleteBucket",
    "s3:PutBucketPolicy",
    "s3:DeleteBucketPolicy",
    "s3:PutBucketVersioning",
    "s3:PutEncryptionConfiguration",
    "s3:PutLifecycleConfiguration",
    "s3:PutBucketLogging",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketOwnershipControls",
    "s3:PutBucketAcl",
    "s3:PutBucketTagging",
    "s3:PutReplicationConfiguration",
    "s3:PutInventoryConfiguration",
    "s3:PutBucketNotification",
    "s3:PutBucketObjectLockConfiguration",
    "s3:PutBucketRequestPayment",
    "s3:PutBucketWebsite",
    "s3:PutBucketCORS",
    "s3:PutAccelerateConfiguration",
    "s3:PutIntelligentTieringConfiguration",
    "s3:PutAnalyticsConfiguration",
    "s3:PutMetricsConfiguration",
)
S3_DATA_PLANE_SID = "DenyObjectDataPlaneToNonProjectPrincipals"
S3_WRITE_SID = "DenyObjectWritesToNonOperators"
S3_ADMIN_SID = "DenyBucketAdministrationToNonOperators"
#: Condition operators of the service-principal exemption: key-use Denies, then the two buckets.
STORE_KEY_EXEMPTION_OPERATOR = "Bool"
STORE_BUCKET_EXEMPTION_OPERATOR = "BoolIfExists"
_SERVICE_PRINCIPAL_KEY = "aws:PrincipalIsAWSService"

#: Root locals naming the logs-key users and the eval-artefacts principals.
LOGS_KEY_USERS_LOCAL = "logs_key_user_role_arns"
EVAL_ARTIFACTS_PRINCIPALS_LOCAL = "eval_artifacts_principal_arns"
LOGS_KMS_KEY_REFERENCE = "module.observability.cloudwatch_logs_kms_key_arn"
#: The only principals admitted to the eval-artefacts bucket and CMK besides the operators.
EVAL_ARTIFACTS_PRINCIPALS: tuple[str, ...] = (
    "module.lambda_action_eval_trigger.role_arn",
    "module.lambda_action_sweep_control.role_arn",
    "module.agentcore.codebuild_role_arn",
)
#: References through which an identity policy reaches the eval-artefacts bucket or CMK.
EVAL_ARTIFACTS_GRANT_REFERENCES: tuple[str, ...] = (
    "module.eval_artifacts.bucket_arn",
    "aws_kms_key.eval_artifacts.arn",
    "var.source_bucket_arn",
    "var.source_kms_key_arn",
)
_STORE_ADMIN_ROOT = (
    f"concat({CRM_OPERATOR_PRINCIPALS_VAR}, "
    '["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"])'
)
_STORE_ROOT_ARN = re.compile(
    r"^arn:aws:iam::\$\{(data\.aws_caller_identity\.current\.account_id|var\.account_id)\}:root$"
)
_STORE_CONTROL = "store_principal_deny"
STORE_PRINCIPAL_DENY_SPEC_ROW = (
    "Stores: S3 bucket and KMS key policies deny every principal outside the store allowlists and "
    "operators"
)


@dataclass(frozen=True)
class StoreDeny:
    """One expected Deny: its sid, ordered actions, resources, allowlist reference and exemption."""

    sid: str
    actions: tuple[str, ...]
    resources: tuple[HclValue, ...]
    principals: str
    exemption: str | None = None

    def conditions(self) -> dict[str, dict[str, tuple[HclValue, ...]]]:
        conditions = _principal_condition(self.principals)
        if self.exemption is not None:
            conditions[self.exemption] = {_SERVICE_PRINCIPAL_KEY: ("false",)}
        return conditions


@dataclass(frozen=True)
class StorePolicy:
    """One store's policy-bearing resource and the exact statements its policy must render.

    `root_sid` names the kept account-root `kms:*` Allow on a key. `root_wired` marks the root
    `aws_kms_key_policy`, whose allowlists and precondition are written inline rather than as
    module variables. `lockout_flag` marks a key resource that must keep the KMS lock-out check.
    """

    store: str
    resource_type: str
    module_dir: str
    name: str
    sids: tuple[str, ...]
    denies: tuple[StoreDeny, ...]
    root_sid: str | None = None
    root_wired: bool = False
    lockout_flag: bool = False


def _bucket_resources(arn: str) -> tuple[HclValue, ...]:
    return (HclExpr(arn), f"${{{arn}}}/*")


def _key_denies(use_principals: str, admin_principals: str) -> tuple[StoreDeny, ...]:
    return (
        StoreDeny(
            DYNAMODB_KEY_USE_SID,
            DYNAMODB_KEY_USE_ACTIONS,
            ("*",),
            use_principals,
            STORE_KEY_EXEMPTION_OPERATOR,
        ),
        StoreDeny(DYNAMODB_KEY_ADMIN_SID, STORE_KEY_ADMIN_ACTIONS, ("*",), admin_principals),
    )


def _bucket_denies(
    arn: str, data_plane_principals: str, *, exempt: bool = False, write_only: bool = False
) -> tuple[StoreDeny, ...]:
    data_plane = (
        StoreDeny(
            S3_WRITE_SID, S3_OBJECT_WRITE_ACTIONS, _bucket_resources(arn), data_plane_principals
        )
        if write_only
        else StoreDeny(
            S3_DATA_PLANE_SID,
            (*S3_OBJECT_READ_ACTIONS, *S3_OBJECT_WRITE_ACTIONS),
            _bucket_resources(arn),
            data_plane_principals,
            STORE_BUCKET_EXEMPTION_OPERATOR if exempt else None,
        )
    )
    admin = StoreDeny(
        S3_ADMIN_SID, S3_BUCKET_ADMIN_ACTIONS, (HclExpr(arn),), ADMINISTRATOR_PRINCIPALS_VAR
    )
    return (data_plane, admin)


_KEY_DENY_SIDS = (DYNAMODB_KEY_USE_SID, DYNAMODB_KEY_ADMIN_SID)

#: Every Group G store policy the gate reads from source.
STORE_POLICIES: tuple[StorePolicy, ...] = (
    StorePolicy(
        "logs_key",
        "aws_kms_key",
        "modules/observability",
        "logs",
        ("EnableIAMUserPermissions", "AllowCloudWatchLogsUseOfTheKey", *_KEY_DENY_SIDS),
        _key_denies("var.logs_key_user_principal_arns", ADMINISTRATOR_PRINCIPALS_VAR),
        root_sid="EnableIAMUserPermissions",
        lockout_flag=True,
    ),
    StorePolicy(
        "cloudtrail_key",
        "aws_kms_key",
        "modules/observability",
        "cloudtrail",
        (
            "EnableIAMUserPermissions",
            "AllowCloudTrailToEncryptLogs",
            "AllowCloudTrailToDescribeKey",
            "AllowPrincipalsInAccountToDecryptLogFiles",
            *_KEY_DENY_SIDS,
        ),
        _key_denies("var.operator_principal_arns", ADMINISTRATOR_PRINCIPALS_VAR),
        root_sid="EnableIAMUserPermissions",
        lockout_flag=True,
    ),
    StorePolicy(
        "cloudtrail_bucket",
        "aws_s3_bucket_policy",
        "modules/observability",
        "cloudtrail",
        (
            "DenyInsecureTransport",
            "AWSCloudTrailAclCheck",
            "AWSCloudTrailWrite",
            S3_DATA_PLANE_SID,
            S3_ADMIN_SID,
        ),
        _bucket_denies("aws_s3_bucket.cloudtrail.arn", "var.operator_principal_arns", exempt=True),
    ),
    StorePolicy(
        "frontend_key",
        "aws_kms_key_policy",
        "modules/frontend_cdn",
        "frontend",
        ("EnableIAMUserPermissions", "AllowCloudFrontOACDecrypt", *_KEY_DENY_SIDS),
        _key_denies("var.operator_principal_arns", ADMINISTRATOR_PRINCIPALS_VAR),
        root_sid="EnableIAMUserPermissions",
        lockout_flag=True,
    ),
    StorePolicy(
        "frontend_origin_bucket",
        "aws_s3_bucket_policy",
        "modules/frontend_cdn",
        "origin",
        ("DenyInsecureTransport", "AllowCloudFrontOACRead", S3_WRITE_SID, S3_ADMIN_SID),
        _bucket_denies("aws_s3_bucket.origin.arn", "var.operator_principal_arns", write_only=True),
    ),
    StorePolicy(
        "eval_artifacts_bucket",
        "aws_s3_bucket_policy",
        "modules/eval_artifacts",
        "this",
        ("DenyInsecureTransport", "DenyUnencryptedObjectUploads", S3_DATA_PLANE_SID, S3_ADMIN_SID),
        _bucket_denies("local.bucket_arn", "var.data_plane_principal_arns"),
    ),
    StorePolicy(
        "eval_artifacts_key",
        "aws_kms_key_policy",
        ".",
        "eval_artifacts",
        ("Enable IAM User Permissions", *_KEY_DENY_SIDS),
        _key_denies(
            f"concat(local.{EVAL_ARTIFACTS_PRINCIPALS_LOCAL}, {CRM_OPERATOR_PRINCIPALS_VAR})",
            _STORE_ADMIN_ROOT,
        ),
        root_sid="Enable IAM User Permissions",
        root_wired=True,
        lockout_flag=True,
    ),
)


def _store_root_problems(statement: RenderedStatement) -> list[str]:
    problems: list[str] = []
    if statement.effect != "Allow":
        problems.append(f"effect={statement.effect!r}, expected 'Allow'")
    principal = statement.principal
    arn = (
        principal.get("AWS") if isinstance(principal, dict) and set(principal) == {"AWS"} else None
    )
    arn = arn[0] if isinstance(arn, (tuple, list)) and len(arn) == 1 else arn
    if not (isinstance(arn, str) and _STORE_ROOT_ARN.match(arn)):
        problems.append(f"principal={principal!r} is not this account's root")
    if statement.actions != ("kms:*",) or statement.resources != ("*",):
        problems.append(
            f"actions={list(statement.actions)!r} resources={list(statement.resources)!r}, "
            "expected kms:* on *"
        )
    if statement.conditions:
        problems.append(f"conditions={statement.conditions!r}, expected none")
    return problems


def _store_deny_problems(statement: RenderedStatement, deny: StoreDeny) -> list[str]:
    problems = _deny_statement_problems(statement, deny.actions, deny.conditions())
    if statement.resources != deny.resources:
        problems.append(
            f"resources={list(statement.resources)!r}, expected exactly {list(deny.resources)!r}"
        )
    return problems


def _normalised_expression(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    return re.sub(r"\(\s+|\s+\)", lambda m: m.group(0).strip(), text)


def _store_precondition_problems(policy: StorePolicy, body: str) -> list[str]:
    source = _normalised_expression(blank_comments(body))
    if "precondition {" not in source:
        return ["no lifecycle precondition; an unlisted caller could apply and lock itself out"]
    if policy.root_wired:
        caller = "data.aws_iam_session_context.current.issuer_arn"
        administrators = _STORE_ADMIN_ROOT
    else:
        caller = "var.caller_principal_arn"
        administrators = ADMINISTRATOR_PRINCIPALS_VAR
    problems = []
    if f"contains({administrators}, {caller})" not in source:
        problems.append(f"the precondition does not require contains({administrators}, {caller})")
    root = r'"arn:aws:iam::\$\{(data\.aws_caller_identity\.current\.account_id|var\.account_id)\}:root"'
    if not re.search(rf"{re.escape(caller)} != {root}", source):
        problems.append("the precondition does not exclude the account root as the caller")
    return problems


def _store_policy_findings(policy: StorePolicy) -> list[Finding]:
    address = f"{policy.module_dir}/{policy.resource_type}.{policy.name}"
    blocks = [
        b
        for d, n, b in tf_resource_blocks(policy.resource_type)
        if d == policy.module_dir and n == policy.name
    ]
    if len(blocks) != 1:
        return [
            Finding(
                _STORE_CONTROL, f"(count) {address}", "fail", f"found {len(blocks)}, expected 1"
            )
        ]
    body = blocks[0]
    findings = [Finding(_STORE_CONTROL, f"(count) {address}", "pass", "exactly one block")]
    try:
        statements = rendered_policy_statements(policy.module_dir, body)
    except ValueError as exc:
        return [*findings, Finding(_STORE_CONTROL, address, "fail", f"policy unreadable: {exc}")]
    sids = tuple(s.sid for s in statements)
    findings.append(
        Finding(
            _STORE_CONTROL,
            f"{address} (statements)",
            "pass" if sids == policy.sids else "fail",
            f"sids={list(sids)!r} expected exactly {list(policy.sids)!r}",
        )
    )
    by_sid = {s.sid: s for s in statements}
    if policy.root_sid is not None:
        root = by_sid.get(policy.root_sid)
        problems = ["statement absent"] if root is None else _store_root_problems(root)
        findings.append(
            Finding(
                _STORE_CONTROL,
                f"{address}#{policy.root_sid}",
                "fail" if problems else "pass",
                "; ".join(problems) or "account-root kms:* Allow kept",
            )
        )
    for deny in policy.denies:
        statement = by_sid.get(deny.sid)
        problems = (
            ["statement absent"] if statement is None else _store_deny_problems(statement, deny)
        )
        findings.append(
            Finding(
                _STORE_CONTROL,
                f"{address}#{deny.sid}",
                "fail" if problems else "pass",
                "; ".join(problems) or "matches the expected Deny statement",
            )
        )
    deny_sids = {d.sid for d in policy.denies}
    stray = sorted(
        s.sid
        for s in statements
        if s.sid not in deny_sids
        and any(_SERVICE_PRINCIPAL_KEY in k for k in s.conditions.values())
    )
    findings.append(
        Finding(
            _STORE_CONTROL,
            f"{address} (service exemption)",
            "fail" if stray else "pass",
            f"{_SERVICE_PRINCIPAL_KEY} also conditions {stray!r}; the exemption belongs only on "
            "the Deny statements named above"
            if stray
            else "carried only where the expected Deny statements carry it",
        )
    )
    guard = _store_precondition_problems(policy, body)
    findings.append(
        Finding(
            _STORE_CONTROL,
            f"{address} (precondition)",
            "fail" if guard else "pass",
            "; ".join(guard)
            or "refuses a plan whose caller is not a listed administrator, or is the account root",
        )
    )
    if policy.lockout_flag:
        bypass = _flag_absent_or_false(body, "bypass_policy_lockout_safety_check")
        findings.append(
            Finding(
                _STORE_CONTROL,
                f"{address} (bypass_policy_lockout_safety_check)",
                "fail" if bypass else "pass",
                f"set to {bypass!r}; it must be absent or false" if bypass else "absent or false",
            )
        )
    return findings


def _root_module_calls() -> dict[str, str]:
    """Every root `module "<name>"` call, mapped to its body with comments blanked."""
    calls: dict[str, str] = {}
    for path in sorted(INFRA_DIR.glob("*.tf")):
        source = blank_comments(path.read_text(encoding="utf-8"))
        for match in re.finditer(r'^module\s+"([A-Za-z0-9_]+)"\s*\{', source, re.MULTILINE):
            calls[match.group(1)] = _block_bodies(
                source[match.start() :], re.compile(r"^module\s", re.MULTILINE)
            )[0]
    return calls


def _root_local_value(local_name: str) -> HclValue:
    for path in sorted(INFRA_DIR.glob("*.tf")):
        source = blank_comments(path.read_text(encoding="utf-8"))
        match = re.search(rf"^  {re.escape(local_name)}\s*=", source, re.MULTILINE)
        if match is not None:
            value, _ = _parse_hcl_value(source, match.end())
            return value
    raise ValueError(f"no root .tf declares local `{local_name}`")


def _logs_key_users_finding() -> Finding:
    """The logs-key local names exactly the module calls whose environment the logs CMK encrypts."""
    resource = f"local.{LOGS_KEY_USERS_LOCAL}"
    try:
        expression = _root_local_expression(LOGS_KEY_USERS_LOCAL)
        tools = _root_local_value("tools")
    except ValueError as exc:
        return Finding(_STORE_CONTROL, resource, "fail", str(exc))
    indexed_pattern = re.compile(r'module\.(\w+)\["([^"]+)"\]\.role_arn')
    named_pattern = re.compile(r"module\.(\w+)\.role_arn")
    indexed = [(m.group(1), m.group(2)) for m in indexed_pattern.finditer(expression)]
    rest = indexed_pattern.sub(" ", expression)
    named = [m.group(1) for m in named_pattern.finditer(rest)]
    rest = re.sub(r"\bconcat\(|[\[\](),]", " ", named_pattern.sub(" ", rest))
    problems: list[str] = []
    if rest.split():
        problems.append(f"entries that are not a module role ARN: {rest.split()!r}")
    entries = [f"module.{m}[{k!r}]" for m, k in indexed] + [f"module.{m}" for m in named]
    duplicates = sorted({e for e in entries if entries.count(e) > 1})
    if duplicates:
        problems.append(f"duplicate entries: {duplicates!r}")
    expected_named: set[str] = set()
    expected_indexed: set[tuple[str, str]] = set()
    for name, body in _root_module_calls().items():
        if _hcl_expression(body, "environment_kms_key_arn") != LOGS_KMS_KEY_REFERENCE:
            continue
        for_each = _hcl_expression(body, "for_each")
        if not for_each:
            expected_named.add(name)
        elif for_each == "local.tools" and isinstance(tools, dict):
            expected_indexed.update((name, key) for key in tools)
        else:
            problems.append(f"module.{name} sets for_each={for_each!r}, whose keys are unknowable")
    if expected_named - set(named):
        problems.append(
            f"call sites missing from the local: {sorted(expected_named - set(named))!r}"
        )
    if set(named) - expected_named:
        problems.append(
            f"entries without environment_kms_key_arn = {LOGS_KMS_KEY_REFERENCE}: "
            f"{sorted(set(named) - expected_named)!r}"
        )
    if expected_indexed - set(indexed):
        problems.append(
            f"instances missing from the local: {sorted(expected_indexed - set(indexed))!r}"
        )
    if set(indexed) - expected_indexed:
        problems.append(f"instances with no such call: {sorted(set(indexed) - expected_indexed)!r}")
    if not expected_named and not expected_indexed:
        problems.append(f"no module call sets environment_kms_key_arn = {LOGS_KMS_KEY_REFERENCE}")
    return Finding(
        _STORE_CONTROL,
        resource,
        "fail" if problems else "pass",
        "; ".join(problems)
        or f"names exactly the {len(expected_named)} named calls and {len(expected_indexed)} "
        "for_each instances whose environment the logs CMK encrypts",
    )


def _eval_artifacts_grant_principals() -> tuple[set[str], list[str]]:
    """`(principals, unresolved)`: the role each artefact-granting policy document is attached to."""
    principals: set[str] = set()
    unresolved: list[str] = []
    for module_dir in (".", "modules/agentcore"):
        attachments: dict[str, list[str]] = {}
        for resource_type in ("aws_iam_role_policy", "aws_iam_policy"):
            for d, _n, body in tf_resource_blocks(resource_type):
                match = re.fullmatch(
                    r"data\.aws_iam_policy_document\.(\w+)\.json", _hcl_expression(body, "policy")
                )
                if d == module_dir and match:
                    attachments.setdefault(match.group(1), []).append(_hcl_expression(body, "role"))
        for d, name, body in tf_resource_blocks("aws_iam_policy_document", kind="data"):
            source = blank_comments(body)
            if d != module_dir or not any(ref in source for ref in EVAL_ARTIFACTS_GRANT_REFERENCES):
                continue
            roles = attachments.get(name, [])
            if not roles:
                unresolved.append(f"{d}/{name} (no aws_iam_role_policy attaches it)")
            for role in roles:
                module_role = re.fullmatch(r"module\.(\w+)\.role_name", role)
                if module_dir == "." and module_role:
                    principals.add(f"module.{module_role.group(1)}.role_arn")
                elif module_dir == "modules/agentcore" and role in (
                    "aws_iam_role.codebuild.id",
                    "aws_iam_role.codebuild.name",
                ):
                    principals.add("module.agentcore.codebuild_role_arn")
                else:
                    unresolved.append(f"{d}/{name} attached to role {role!r}")
    return principals, unresolved


def _eval_artifacts_principals_findings() -> list[Finding]:
    resource = f"local.{EVAL_ARTIFACTS_PRINCIPALS_LOCAL}"
    problems: list[str] = []
    try:
        value = _root_local_value(EVAL_ARTIFACTS_PRINCIPALS_LOCAL)
    except ValueError as exc:
        return [Finding(_STORE_CONTROL, resource, "fail", str(exc))]
    entries = (
        [v.text if isinstance(v, HclExpr) else repr(v) for v in value]
        if isinstance(value, list)
        else [repr(value)]
    )
    if sorted(entries) != sorted(EVAL_ARTIFACTS_PRINCIPALS):
        problems.append(
            f"entries={entries!r}, expected exactly {list(EVAL_ARTIFACTS_PRINCIPALS)!r}"
        )
    granted, unresolved = _eval_artifacts_grant_principals()
    if unresolved:
        problems.append(f"artefact-granting documents with no listed principal: {unresolved!r}")
    if granted - set(entries):
        problems.append(f"granted outside the local: {sorted(granted - set(entries))!r}")
    if set(entries) - granted:
        problems.append(f"listed with no artefact grant: {sorted(set(entries) - granted)!r}")
    findings = [
        Finding(
            _STORE_CONTROL,
            resource,
            "fail" if problems else "pass",
            "; ".join(problems)
            or "names exactly the eval_trigger, sweep_control and CodeBuild roles, each of which "
            "holds an artefact grant, and no other role holds one",
        )
    ]
    output: list[str] = []
    for path in sorted((INFRA_DIR / "modules" / "agentcore").glob("*.tf")):
        source = blank_comments(path.read_text(encoding="utf-8"))
        match = re.search(r'^output\s+"codebuild_role_arn"\s*\{', source, re.MULTILINE)
        if match:
            output += _block_bodies(source[match.start() :], re.compile(r"^output\s", re.M))[:1]
    value_ok = len(output) == 1 and (
        _hcl_expression(output[0], "value") == "aws_iam_role.codebuild.arn"
    )
    findings.append(
        Finding(
            _STORE_CONTROL,
            "modules/agentcore output.codebuild_role_arn",
            "pass" if value_ok else "fail",
            "is aws_iam_role.codebuild.arn"
            if value_ok
            else "absent, or its value is not aws_iam_role.codebuild.arn",
        )
    )
    return findings


def _store_wiring_findings() -> list[Finding]:
    caller = "data.aws_iam_session_context.current.issuer_arn"
    expected = {
        "observability": {
            "caller_principal_arn": caller,
            "logs_key_user_principal_arns": (
                f"concat(local.{LOGS_KEY_USERS_LOCAL}, {CRM_OPERATOR_PRINCIPALS_VAR})"
            ),
            "operator_principal_arns": CRM_OPERATOR_PRINCIPALS_VAR,
            "administrator_principal_arns": _STORE_ADMIN_ROOT,
        },
        "frontend_cdn": {
            "caller_principal_arn": caller,
            "operator_principal_arns": CRM_OPERATOR_PRINCIPALS_VAR,
            "administrator_principal_arns": _STORE_ADMIN_ROOT,
        },
        "eval_artifacts": {
            "caller_principal_arn": caller,
            "data_plane_principal_arns": (
                f"concat(local.{EVAL_ARTIFACTS_PRINCIPALS_LOCAL}, {CRM_OPERATOR_PRINCIPALS_VAR})"
            ),
            "administrator_principal_arns": _STORE_ADMIN_ROOT,
        },
    }
    calls = _root_module_calls()
    findings: list[Finding] = []
    for module, arguments in expected.items():
        body = calls.get(module)
        for argument, wanted in arguments.items():
            written = (
                "" if body is None else _normalised_expression(_hcl_expression(body, argument))
            )
            findings.append(
                Finding(
                    _STORE_CONTROL,
                    f"module.{module}.{argument}",
                    "pass" if written == wanted else "fail",
                    f"written={written!r} expected={wanted!r}; the operators belong on every list "
                    "and the account root on the administrator list only",
                )
            )
    return findings


def check_store_principal_deny(_plan: Plan) -> list[Finding]:
    """The logs, CloudTrail, frontend and eval-artefacts key and bucket policies deny the data
    plane, key use and administration outside each store's allowlist and the operators. Read from
    source."""
    findings: list[Finding] = []
    for policy in STORE_POLICIES:
        findings += _store_policy_findings(policy)
    findings.append(_logs_key_users_finding())
    findings += _eval_artifacts_principals_findings()
    findings += _store_wiring_findings()
    return findings


#: The two runtime resource-policy documents the SourceVpce Allow/Deny live in.
_RUNTIME_POLICY_DOCUMENT_NAMES: tuple[str, ...] = (
    "agent_runtime_relay_only",
    "agent_runtime_endpoint_relay_only",
)
_SOURCE_VPCE_CONDITION_VARIABLE = "aws:SourceVpce"
_VIA_AWS_SERVICE_CONDITION_VARIABLE = "aws:ViaAWSService"


def check_runtime_network_mode_vpc(_plan: Plan) -> list[Finding]:
    """The AgentCore Runtime is deployed in VPC mode, read from the `module "agentcore"` call site.

    Also asserts the runtime subnet and security-group inputs are wired.
    """
    findings: list[Finding] = []
    source = (INFRA_DIR / "main.tf").read_text(encoding="utf-8")
    match = re.search(r'module\s+"agentcore"\s*\{', source)
    if match is None:
        return [
            Finding(
                "runtime_network_mode_vpc",
                "infra/main.tf",
                "fail",
                'no `module "agentcore"` block found, so this control asserted nothing',
            )
        ]
    body = _strip_tf_comments(_balanced_block_body(source, match.end() - 1))
    mode = _attribute_value(body, "runtime_network_mode")
    findings.append(
        Finding(
            "runtime_network_mode_vpc",
            'module "agentcore".runtime_network_mode',
            "pass" if mode == "VPC" else "fail",
            f'runtime_network_mode={mode!r}; must be the literal "VPC" (egress governed by '
            "the VPC, not the public internet)",
        )
    )
    subnets = _hcl_expression(body, "runtime_subnet_ids")
    sgs = _hcl_expression(body, "runtime_security_group_ids")
    findings.append(
        Finding(
            "runtime_network_mode_vpc",
            'module "agentcore" VPC-mode wiring',
            "pass" if (subnets and sgs) else "fail",
            f"runtime_subnet_ids={subnets!r} runtime_security_group_ids={sgs!r}; VPC mode needs both "
            "wired (empty means the runtime resource precondition fails the plan)",
        )
    )
    return findings


def _statement_conditions(statement_body: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """`(test, variable, values)` for every `condition { … }` block in a statement/content body."""
    conditions: list[tuple[str, str, tuple[str, ...]]] = []
    for condition_body in _block_bodies(
        statement_body, re.compile(r"^\s*condition\s*\{", re.MULTILINE)
    ):
        test = _hcl_scalar(condition_body, "test")
        variable = _hcl_scalar(condition_body, "variable")
        values, _ = _hcl_string_list(condition_body, "values")
        conditions.append((test, variable, values))
    return conditions


def check_runtime_resource_policy_source_vpce_deny(_plan: Plan) -> list[Finding]:
    """Both runtime resource policies carry the staged `aws:SourceVpce` Allow and Deny.

    Parses the `dynamic "statement"` blocks directly and asserts their authored shape, independent
    of the stage toggle values. An Allow granting principal `"*"` fails.
    """
    findings: list[Finding] = []
    blocks = {
        name: body
        for module_dir, name, body in tf_resource_blocks("aws_iam_policy_document", kind="data")
        if module_dir == "." and name in _RUNTIME_POLICY_DOCUMENT_NAMES
    }

    for name in _RUNTIME_POLICY_DOCUMENT_NAMES:
        body = blocks.get(name)
        if body is None:
            findings.append(
                Finding(
                    "runtime_resource_policy_source_vpce_deny",
                    f"data.aws_iam_policy_document.{name}",
                    "fail",
                    "document not found in infra/main.tf, so the SourceVpce Allow/Deny could not be "
                    "read; this control asserted nothing for it",
                )
            )
            continue

        deny_bodies: list[str] = []
        report_only_allow_seen = False
        wildcard_allow_seen = False
        for dynamic_body in _block_bodies(
            body, re.compile(r'^\s*dynamic\s+"statement"\s*\{', re.MULTILINE)
        ):
            effect = _hcl_scalar(dynamic_body, "effect")
            conditions = _statement_conditions(dynamic_body)
            has_source_vpce = any(
                variable == _SOURCE_VPCE_CONDITION_VARIABLE for _, variable, _ in conditions
            )
            allow_identifiers: tuple[str, ...] = ()
            for principal_body in _block_bodies(
                dynamic_body, re.compile(r"^\s*principals\s*\{", re.MULTILINE)
            ):
                found, _ = _hcl_string_list(principal_body, "identifiers")
                allow_identifiers += found
            if effect == "Deny":
                deny_bodies.append(dynamic_body)
            elif effect == "Allow":
                if "*" in allow_identifiers:
                    wildcard_allow_seen = True
                elif has_source_vpce:
                    report_only_allow_seen = True

        findings.append(
            Finding(
                "runtime_resource_policy_source_vpce_deny",
                f"{name}: report-only SourceVpce Allow present",
                "pass" if report_only_allow_seen else "fail",
                'a `dynamic "statement"` Allow keyed on aws:SourceVpce and scoped to the relay role '
                "(stage 1) " + ("is present" if report_only_allow_seen else "is MISSING"),
            )
        )
        findings.append(
            Finding(
                "runtime_resource_policy_source_vpce_deny",
                f"{name}: no wildcard-principal Allow",
                "fail" if wildcard_allow_seen else "pass",
                "an Allow statement grants Principal '*': scope it to the relay role — a "
                "network-only Allow admits every same-account principal reaching the endpoint"
                if wildcard_allow_seen
                else "no Allow in this document grants a wildcard principal",
            )
        )

        if len(deny_bodies) != 1:
            findings.append(
                Finding(
                    "runtime_resource_policy_source_vpce_deny",
                    f"{name}: SourceVpce Deny present",
                    "fail",
                    f'expected exactly one `dynamic "statement"` Deny; found {len(deny_bodies)}. '
                    "The inbound Deny must be authored on both ARNs — a Deny on one leaves the "
                    "other open",
                )
            )
            continue

        deny = deny_bodies[0]
        actions, actions_literal = _hcl_string_list(deny, "actions", "action")
        identifiers: tuple[str, ...] = ()
        for principal_body in _block_bodies(deny, re.compile(r"^\s*principals\s*\{", re.MULTILINE)):
            found, _ = _hcl_string_list(principal_body, "identifiers")
            identifiers += found
        conditions = _statement_conditions(deny)
        for_each = _hcl_expression(deny, "for_each")

        problems: list[str] = []
        if not (actions_literal and tuple(actions) == ("bedrock-agentcore:InvokeAgentRuntime",)):
            problems.append(f"actions={list(actions)!r} (must be [InvokeAgentRuntime])")
        if "*" not in identifiers:
            problems.append(f"principal identifiers={list(identifiers)!r} (Deny must apply to '*')")
        if "runtime_source_vpce_deny_enabled" not in for_each:
            problems.append(
                f"for_each={for_each!r} (must gate on var.runtime_source_vpce_deny_enabled)"
            )
        source_vpce_ok = any(
            test == "StringNotEquals"
            and variable == _SOURCE_VPCE_CONDITION_VARIABLE
            and values
            and "runtime_source_vpce_id" in values[0]
            for test, variable, values in conditions
        )
        if not source_vpce_ok:
            problems.append(
                "missing a StringNotEquals aws:SourceVpce condition on local.runtime_source_vpce_id"
            )
        via_service_ok = any(
            test == "Bool"
            and variable == _VIA_AWS_SERVICE_CONDITION_VARIABLE
            and tuple(v.lower() for v in values) == ("false",)
            for test, variable, values in conditions
        )
        if not via_service_ok:
            problems.append(
                "missing a Bool aws:ViaAWSService=false condition (keeps internal service "
                "calls from being denied)"
            )

        findings.append(
            Finding(
                "runtime_resource_policy_source_vpce_deny",
                f"{name}: SourceVpce Deny shape",
                "fail" if problems else "pass",
                "; ".join(problems)
                if problems
                else "Deny on '*' unless aws:SourceVpce is our endpoint, with aws:ViaAWSService=false",
            )
        )

    return findings


def _normalise_reference(value: str) -> str:
    """`"${var.x}"` and `var.x` are the same reference; return a comparable form of either."""
    match = re.fullmatch(r"\$\{([^{}]+)\}", value.strip())
    return match.group(1).strip() if match else value.strip()


def _hcl_expression(body: str, name: str) -> str:
    """The right-hand side of `name = <expr>` as written, quotes stripped."""
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(.+?)\s*$", body, re.MULTILINE)
    if match is None:
        return ""
    return match.group(1).strip().strip('"')


def _policy_document_key(
    module_dir: str, document_ref: str, documents: dict[str, list[TerraformStatement]]
) -> str | None:
    """Resolve `data.aws_iam_policy_document.<name>.json` to a key of `policy_document_statements`."""
    match = re.search(r"aws_iam_policy_document\.([A-Za-z0-9_-]+)", document_ref)
    if match is None:
        return None
    candidate = f"{module_dir}/{match.group(1)}"
    if candidate in documents:
        return candidate
    suffix = f"/{match.group(1)}"
    matches = [key for key in documents if key.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


#: Action prefix whose resource scoping `bedrock_inference_profile_scoping` asserts.
BEDROCK_INVOKE_ACTION_PREFIX = "bedrock:InvokeModel"

#: The ARN shapes a bedrock invoke grant may name: inference profiles, foundation models, projects.
_INFERENCE_PROFILE_ARN = re.compile(r"^arn:aws:bedrock:[^:]*:[^:]*:inference-profile/")
_FOUNDATION_MODEL_ARN = re.compile(r"^arn:aws:bedrock:[^:]*:[^:]*:foundation-model/")
_PROJECT_ARN = re.compile(r"^arn:aws:bedrock:[^:]*:[^:]*:project/")
#: The condition key a statement granting foundation-model ARNs must carry.
_INFERENCE_PROFILE_CONDITION_KEY = "bedrock:InferenceProfileArn"


def _strip_tf_comments(text: str) -> str:
    """Remove `#`/`//` comments, preserving string literals.

    Unlike `_strip_tf_comments_and_strings`, string contents are kept.
    """
    out: list[str] = []
    for line in text.splitlines():
        in_string = False
        index = 0
        kept: list[str] = []
        while index < len(line):
            char = line[index]
            if in_string:
                kept.append(char)
                if char == "\\" and index + 1 < len(line):
                    kept.append(line[index + 1])
                    index += 2
                    continue
                if char == '"':
                    in_string = False
                index += 1
                continue
            if char == '"':
                in_string = True
                kept.append(char)
                index += 1
                continue
            if char == "#" or line[index : index + 2] == "//":
                break
            kept.append(char)
            index += 1
        out.append("".join(kept))
    return "\n".join(out)


def check_bedrock_inference_profile_scoping(_plan: Plan) -> list[Finding]:
    """`bedrock:InvokeModel*` is scoped to named inference profiles, foundation models and the default
    project, never `*`, and every foundation-model grant is bound to a profile by condition.

    Read from source. The presence of both inference-profile and foundation-model ARNs is asserted
    in aggregate across the invoke statements. The resources scan stops at the first `condition`
    block, whose ARNs are match keys rather than granted resources.
    """
    findings: list[Finding] = []
    statements_seen = 0
    saw_profile_arn = False
    saw_foundation_model_arn = False

    for module_dir, name, body in tf_resource_blocks("aws_iam_policy_document", kind="data"):
        for statement_body in _block_bodies(body, _STATEMENT_HEADER):
            actions, actions_literal = _hcl_string_list(statement_body, "actions", "action")
            # Substring match, so a non-literal expression naming the action is still selected.
            if not any(BEDROCK_INVOKE_ACTION_PREFIX in a for a in actions):
                continue
            address = f"{module_dir}/{name}#{_hcl_scalar(statement_body, 'sid') or '(no sid)'}"
            statements_seen += 1

            if not actions_literal:
                findings.append(
                    Finding(
                        "bedrock_inference_profile_scoping",
                        address,
                        "fail",
                        "the statement's actions are not literal strings, so the granted set cannot "
                        "be read from source",
                    )
                )
                continue

            scannable = _strip_tf_comments(statement_body)
            problems: list[str] = []
            resources_at = scannable.find("resources")
            if resources_at == -1:
                problems.append("no `resources` attribute: an omitted resource set is unscoped")
                resources_text = ""
                condition_text = ""
            else:
                tail = scannable[resources_at:]
                condition_at = tail.find("condition")
                resources_text = tail[:condition_at] if condition_at != -1 else tail
                condition_text = tail[condition_at:] if condition_at != -1 else ""

            quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', resources_text)
            wildcards = [q for q in quoted if q == "*" or q.endswith(":*") or q.endswith("::*")]
            if wildcards:
                problems.append(
                    f"wildcard resource(s) {wildcards!r} — admits any model in the region "
                    "regardless of cost, which is the blast-radius half of the Bedrock row"
                )
            arns = [q for q in quoted if q.startswith("arn:")]
            profile_arns = [a for a in arns if _INFERENCE_PROFILE_ARN.match(a)]
            fm_arns = [a for a in arns if _FOUNDATION_MODEL_ARN.match(a)]
            project_arns = [a for a in arns if _PROJECT_ARN.match(a)]
            malformed = [
                a
                for a in arns
                if a not in profile_arns and a not in fm_arns and a not in project_arns
            ]
            if malformed:
                problems.append(
                    f"ARN(s) matching none of inference-profile/foundation-model/project: {malformed!r}"
                )
            if fm_arns and _INFERENCE_PROFILE_CONDITION_KEY not in condition_text:
                problems.append(
                    f"foundation-model resource(s) granted without a {_INFERENCE_PROFILE_CONDITION_KEY} "
                    "condition — the models are then reachable by any route, not only through the "
                    "named inference profiles"
                )
            saw_profile_arn = saw_profile_arn or bool(profile_arns)
            saw_foundation_model_arn = saw_foundation_model_arn or bool(fm_arns)

            findings.append(
                Finding(
                    "bedrock_inference_profile_scoping",
                    address,
                    "fail" if problems else "pass",
                    "; ".join(problems)
                    if problems
                    else f"scoped to {len(profile_arns)} inference-profile / {len(fm_arns)} "
                    f"foundation-model / {len(project_arns)} project ARN(s)"
                    + (
                        f", foundation models bound by a {_INFERENCE_PROFILE_CONDITION_KEY} condition"
                        if fm_arns
                        else ""
                    ),
                )
            )

    if statements_seen:
        findings.append(
            Finding(
                "bedrock_inference_profile_scoping",
                "(coverage) an inference-profile ARN is a granted resource",
                "pass" if saw_profile_arn else "fail",
                "at least one inference-profile ARN is granted as a resource"
                if saw_profile_arn
                else "no statement grants an inference-profile ARN as a resource — the Bedrock row names "
                "inference profiles specifically",
            )
        )
        findings.append(
            Finding(
                "bedrock_inference_profile_scoping",
                "(coverage) a foundation-model ARN is a granted resource",
                "pass" if saw_foundation_model_arn else "fail",
                "at least one foundation-model ARN is granted as a resource"
                if saw_foundation_model_arn
                else "no foundation-model ARN is granted — a global/cross-region inference profile also "
                "requires invoke permission on the models it routes to",
            )
        )

    findings.append(
        Finding(
            "bedrock_inference_profile_scoping",
            f"(coverage) statements granting {BEDROCK_INVOKE_ACTION_PREFIX}*",
            "pass" if statements_seen else "fail",
            f"found={statements_seen}; zero means the grant moved, was renamed, or this control "
            "stopped matching it",
        )
    )
    return findings


def check_cloudfront_oac(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_cloudfront_distribution"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        origins = after.get("origin") or []
        origins_unknown = after_unknown.get("origin") or []
        s3_origins = [o for o in origins if o.get("origin_id") == "s3-frontend-origin"]
        s3_origin_indices = [
            i for i, o in enumerate(origins) if o.get("origin_id") == "s3-frontend-origin"
        ]

        def oac_ok(
            index: int, origin: dict[str, Any], origins_unknown: Any = origins_unknown
        ) -> bool:
            if origin.get("origin_access_control_id"):
                return True
            return unknown_block_attr(origins_unknown, index, "origin_access_control_id")

        ok = bool(s3_origins) and all(
            oac_ok(i, o) for i, o in zip(s3_origin_indices, s3_origins, strict=True)
        )
        deferred = bool(s3_origins) and any(
            not o.get("origin_access_control_id")
            and unknown_block_attr(origins_unknown, i, "origin_access_control_id")
            for i, o in zip(s3_origin_indices, s3_origins, strict=True)
        )
        detail = f"s3_origins={s3_origins!r}"
        if deferred:
            detail += " (origin_access_control_id unknown at plan time -- same-apply OAC reference, resolves at apply)"
        findings.append(
            Finding(
                "cloudfront_oac",
                r["address"],
                "pass" if ok else "fail",
                detail,
            )
        )
    return findings


def check_cloudfront_behaviours(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_cloudfront_distribution"):
        after = plan.after(r)
        behaviours = {b.get("path_pattern") for b in (after.get("ordered_cache_behavior") or [])}
        ok = {"/api/*", "/agent/*"}.issubset(behaviours)
        findings.append(
            Finding(
                "cloudfront_three_behaviours",
                r["address"],
                "pass" if ok else "fail",
                f"ordered_cache_behavior path_patterns={behaviours!r}",
            )
        )
    return findings


#: Pool-level MFA settings that keep a second factor reachable.
_ACCEPTED_MFA_CONFIGURATIONS = frozenset({"ON", "OPTIONAL"})


def check_cognito_mfa(plan: Plan) -> list[Finding]:
    """The pool keeps a second factor available, and the software-token (TOTP) factor is enabled."""
    findings = []
    for r in plan.by_type("aws_cognito_user_pool"):
        after = plan.after(r)
        configuration = after.get("mfa_configuration")
        software_token = (after.get("software_token_mfa_configuration") or [{}])[0]
        totp_enabled = software_token.get("enabled") is True
        ok = configuration in _ACCEPTED_MFA_CONFIGURATIONS and totp_enabled
        findings.append(
            Finding(
                "cognito_mfa",
                r["address"],
                "pass" if ok else "fail",
                f"mfa_configuration={configuration!r} software_token_enabled={totp_enabled!r}",
            )
        )
    return findings


def check_cognito_password_policy(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_cognito_user_pool"):
        after = plan.after(r)
        policy = (after.get("password_policy") or [{}])[0]
        ok = (
            (policy.get("minimum_length") or 0) >= 14
            and policy.get("require_lowercase")
            and policy.get("require_uppercase")
            and policy.get("require_numbers")
            and policy.get("require_symbols")
        )
        findings.append(
            Finding(
                "cognito_password_policy",
                r["address"],
                "pass" if ok else "fail",
                f"{policy!r}",
            )
        )
    return findings


def check_cognito_account_recovery(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_cognito_user_pool"):
        after = plan.after(r)
        setting = (after.get("account_recovery_setting") or [{}])[0]
        mechanisms = setting.get("recovery_mechanism") or []
        ok = bool(mechanisms)
        findings.append(
            Finding(
                "cognito_account_recovery",
                r["address"],
                "pass" if ok else "fail",
                f"{setting!r}",
            )
        )
    return findings


_ADMIN_CREATE_USER_CONFIG_HEADER = re.compile(r"^\s*admin_create_user_config\s*\{")
_ADMIN_ONLY_LITERAL = re.compile(r"^\s*allow_admin_create_user_only\s*=\s*true\s*$")


def _pool_declares_admin_only_signup(body: str) -> bool:
    """Whether one `aws_cognito_user_pool` body has a STATIC top-level `admin_create_user_config`
    block whose own direct argument is a literal `allow_admin_create_user_only = true`.

    Comments and string contents are blanked first. A `dynamic "admin_create_user_config"` block does
    not match the header, and a literal nested deeper than the block's own arguments is not counted.
    """
    lines = blank_literals(blank_comments(body)).splitlines()
    depth = 0
    block_depth: int | None = None
    for line in lines:
        if block_depth is None and depth == 1 and _ADMIN_CREATE_USER_CONFIG_HEADER.match(line):
            block_depth = depth + 1
        elif block_depth is not None and depth == block_depth and _ADMIN_ONLY_LITERAL.match(line):
            return True
        depth += line.count("{") - line.count("}")
        if block_depth is not None and depth < block_depth:
            block_depth = None
    return False


def source_declares_admin_only_signup() -> bool:
    """Whether every `aws_cognito_user_pool` in `infra/` declares admin-only sign-up statically.

    False when `infra/` declares no pool at all.
    """
    pools = tf_resource_blocks("aws_cognito_user_pool")
    return bool(pools) and all(_pool_declares_admin_only_signup(body) for _, _, body in pools)


def check_cognito_admin_only_signup(plan: Plan) -> list[Finding]:
    """Every pool refuses public `SignUp`.

    Against a current plan a known `true` passes and anything else fails. Against a stale plan,
    `infra/` source must also declare the static literal, or every disposition fails; with it, a
    known `true` passes and an absent or unknown value is `deferred`. A known `false` always fails.
    """
    stale = plan_is_stale_against_source()
    source_ok = source_declares_admin_only_signup() if stale else True
    findings = []
    for r in plan.by_type("aws_cognito_user_pool"):
        after = plan.after(r)
        config = (after.get("admin_create_user_config") or [{}])[0]
        value = config.get("allow_admin_create_user_only")
        unknown = is_unknown(plan.after_unknown(r).get("admin_create_user_config"))
        detail = (
            f"allow_admin_create_user_only={value!r} unknown_at_plan={unknown} plan_stale={stale} "
            f"source_declares={source_ok if stale else 'not consulted'}"
        )
        if value is False:
            status = "fail"
            detail += " -- public SignUp is open"
        elif stale and not source_ok:
            status = "fail"
            detail += (
                " -- the plan is stale and infra/ source has no static admin_create_user_config "
                "block with a literal allow_admin_create_user_only = true"
            )
        elif value is True:
            status = "pass"
        elif stale:
            status = "deferred"
            detail += (
                " -- deferred: the stale plan predates the source fix that infra/ declares. It becomes "
                "a FAIL against a current plan, or if the source declaration is removed."
            )
        else:
            status = "fail"
            detail += (
                " -- Cognito's default is False, so an unset value means public SignUp is open"
            )
        findings.append(Finding("cognito_admin_only_signup", r["address"], status, detail))
    return findings


def check_cognito_tier_and_trigger_version(plan: Plan) -> list[Finding]:
    """The pool tier is ESSENTIALS or PLUS and the pre-token-generation trigger is V2_0."""
    findings = []
    for r in plan.by_type("aws_cognito_user_pool"):
        after = plan.after(r)
        tier = after.get("user_pool_tier")
        lambda_config = (after.get("lambda_config") or [{}])[0]
        trigger_cfg = (lambda_config.get("pre_token_generation_config") or [{}])[0]
        version = trigger_cfg.get("lambda_version")
        ok = tier in ("ESSENTIALS", "PLUS") and version == "V2_0"
        findings.append(
            Finding(
                "cognito_tier_and_trigger_version",
                r["address"],
                "pass" if ok else "fail",
                f"user_pool_tier={tier!r} lambda_version={version!r}",
            )
        )
    return findings


def check_cognito_write_attributes_exclude_scope(plan: Plan) -> list[Finding]:
    """No app client may write a scope-bearing attribute."""
    scope_attrs = {"custom:rep_id", "custom:book", "custom:full_visibility"}
    findings = []
    for r in plan.by_type("aws_cognito_user_pool_client"):
        after = plan.after(r)
        write_attrs = set(after.get("write_attributes") or [])
        if not write_attrs:
            findings.append(
                Finding(
                    "cognito_write_attributes_exclude_scope",
                    r["address"],
                    "fail",
                    "write_attributes is empty/unset (must be an explicit non-null allow-list)",
                )
            )
            continue
        leaked = write_attrs & scope_attrs
        ok = not leaked
        findings.append(
            Finding(
                "cognito_write_attributes_exclude_scope",
                r["address"],
                "pass" if ok else "fail",
                f"write_attributes={sorted(write_attrs)!r} leaked_scope_attrs={sorted(leaked)!r}",
            )
        )
    return findings


def check_gateway_custom_jwt(plan: Plan) -> list[Finding]:
    """Assert the Gateway authorizer is CUSTOM_JWT and genuinely constrains its callers.

    Requires `authorizer_type == "CUSTOM_JWT"`, a `discovery_url`, and `allowed_clients`.
    `allowed_audience` is not required: Cognito `client_credentials` access tokens carry no `aud`.
    """
    findings = []
    for r in plan.by_type("aws_bedrockagentcore_gateway"):
        after = plan.after(r)
        after_unknown = plan.after_unknown(r)
        ok = after.get("authorizer_type") == "CUSTOM_JWT"
        auth_cfg = (after.get("authorizer_configuration") or [{}])[0]
        jwt_cfg = (auth_cfg.get("custom_jwt_authorizer") or [{}])[0]
        auth_cfg_unknown = (after_unknown.get("authorizer_configuration") or [{}])[0]
        jwt_cfg_unknown = (auth_cfg_unknown.get("custom_jwt_authorizer") or [{}])[0]

        def present_or_deferred(
            field: str, jwt_cfg: dict[str, Any] = jwt_cfg, jwt_cfg_unknown: Any = jwt_cfg_unknown
        ) -> tuple[bool, bool]:
            """(satisfied, deferred) -- deferred means unknown-at-plan-time, not missing."""
            value = jwt_cfg.get(field)
            has_value = bool(value) and not (isinstance(value, list) and not any(value))
            if has_value:
                return True, False
            deferred = is_unknown(jwt_cfg_unknown.get(field))
            return deferred, deferred

        discovery_ok, discovery_deferred = present_or_deferred("discovery_url")
        clients_ok, clients_deferred = present_or_deferred("allowed_clients")
        audience_ok, _audience_deferred = present_or_deferred("allowed_audience")
        overall_ok = ok and discovery_ok and clients_ok
        deferred_bits = [
            name
            for name, d in (
                ("discovery_url", discovery_deferred),
                ("allowed_clients", clients_deferred),
            )
            if d
        ]
        detail = (
            f"authorizer_type={after.get('authorizer_type')!r} discovery_url_set={discovery_ok} "
            f"clients_set={clients_ok} audience_set={audience_ok} "
            "(allowed_audience deliberately not required: no Cognito token can satisfy it "
            "alongside allowed_clients/allowed_scopes)"
        )
        if deferred_bits:
            detail += (
                f" ({', '.join(deferred_bits)} unknown at plan time -- same-apply Cognito "
                "user-pool/client references, resolve at apply)"
            )
        findings.append(
            Finding(
                "gateway_custom_jwt",
                r["address"],
                "pass" if overall_ok else "fail",
                detail,
            )
        )
    return findings


def check_gateway_interceptor(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_bedrockagentcore_gateway"):
        after = plan.after(r)
        interceptor_cfg = after.get("interceptor_configuration") or [{}]
        if not interceptor_cfg or not interceptor_cfg[0]:
            findings.append(
                Finding(
                    "gateway_interceptor_present",
                    r["address"],
                    "fail",
                    "interceptor_configuration missing",
                )
            )
            continue
        cfg = interceptor_cfg[0]
        points = cfg.get("interception_points") or []
        input_cfg = (cfg.get("input_configuration") or [{}])[0]
        ok = "REQUEST" in points and input_cfg.get("pass_request_headers") is True
        findings.append(
            Finding(
                "gateway_interceptor_present",
                r["address"],
                "pass" if ok else "fail",
                f"interception_points={points!r} pass_request_headers={input_cfg.get('pass_request_headers')!r}",
            )
        )
    return findings


def check_no_public_ingress(plan: Plan) -> list[Finding]:
    """Nothing in the plan admits the internet: no public ingress rule, no internet-facing ALB.

    Reads `aws_vpc_security_group_ingress_rule` CIDRs, inline `ingress` blocks on
    `aws_security_group`, and `aws_lb.internal`, which must be `true` (absent means internet-facing).
    """
    findings = []
    for r in plan.by_type("aws_vpc_security_group_ingress_rule"):
        after = plan.after(r)
        public = [
            value
            for value in (after.get("cidr_ipv4"), after.get("cidr_ipv6"))
            if value in _PUBLIC_CIDRS
        ]
        findings.append(
            Finding(
                "no_public_ingress",
                r["address"],
                "pass" if not public else "fail",
                f"cidr_ipv4={after.get('cidr_ipv4')!r} cidr_ipv6={after.get('cidr_ipv6')!r} "
                f"referenced_security_group_id={after.get('referenced_security_group_id')!r} "
                f"public={public!r}",
            )
        )
    for r in plan.by_type("aws_security_group"):
        after = plan.after(r)
        ingress_rules = after.get("ingress") or []
        public = [
            rule
            for rule in ingress_rules
            if any(cidr in _PUBLIC_CIDRS for cidr in (rule.get("cidr_blocks") or []))
            or any(cidr in _PUBLIC_CIDRS for cidr in (rule.get("ipv6_cidr_blocks") or []))
        ]
        findings.append(
            Finding(
                "no_public_ingress",
                r["address"],
                "pass" if not public else "fail",
                f"inline_ingress_blocks={len(ingress_rules)} public_ingress_rules={public!r}",
            )
        )
    for r in plan.by_type("aws_lb"):
        after = plan.after(r)
        internal = after.get("internal")
        findings.append(
            Finding(
                "no_public_ingress",
                r["address"],
                "pass" if internal is True else "fail",
                f"internal={internal!r} (must be True; the attribute defaults to false, so absent is "
                f"internet-facing) load_balancer_type={after.get('load_balancer_type')!r}",
            )
        )
    return findings


#: The two any-source CIDRs.
_PUBLIC_CIDRS = frozenset({"0.0.0.0/0", "::/0"})

#: The only port an egress rule to `0.0.0.0/0` may open.
_SCOPED_EGRESS_PORT = 443


def _infra_tf_sources() -> list[tuple[Path, str]]:
    """Every authored `infra/**/*.tf` with heredoc bodies and comments blanked."""
    sources: list[tuple[Path, str]] = []
    for path in sorted(INFRA_DIR.rglob("*.tf")):
        if ".terraform" in path.parts or "build" in path.parts:
            continue
        # Heredoc bodies are blanked before comments; quoted strings are kept for the header regexes.
        sources.append(
            (path, blank_comments(blank_heredoc_bodies(path.read_text(encoding="utf-8"))))
        )
    return sources


def declared_lambda_permission_names(sources: list[tuple[Path, str]] | None = None) -> set[str]:
    """The resource NAMES of every `aws_lambda_permission` block authored in `infra/`.

    Names rather than addresses, so they compare directly with `planned_lambda_permission_names`.
    """
    names: set[str] = set()
    for _path, text in sources if sources is not None else _infra_tf_sources():
        names.update(re.findall(r'resource\s+"aws_lambda_permission"\s+"([A-Za-z0-9_-]+)"', text))
    return names


def planned_lambda_permission_names(plan: Plan) -> set[str]:
    """The resource names of every `aws_lambda_permission` instance in `plan`.

    Strips the module path and any `for_each`/`count` index, so
    `module.api.aws_lambda_permission.apigw_invoke["<tool>"]` and
    `module.lambda_action["<tool>"].aws_lambda_permission.api_gateway[0]` both reduce to their block
    name.
    """
    names: set[str] = set()
    for record in plan.by_type("aws_lambda_permission"):
        address = str(record.get("address", ""))
        match = re.search(r"aws_lambda_permission\.([A-Za-z0-9_-]+)", address)
        if match:
            names.add(match.group(1))
    return names


def check_lambda_permission_composition(plan: Plan) -> list[Finding]:
    """The SET of Lambda invoke grants, not the count of them.

    Compares planned grant names with those `infra/` declares, in both directions. A divergence is
    `deferred` while the plan is stale and `fail` against a current plan.
    """
    declared = declared_lambda_permission_names()
    planned = planned_lambda_permission_names(plan)

    if not declared:
        return [
            Finding(
                "lambda_permission_composition",
                str(INFRA_DIR),
                "fail",
                "no aws_lambda_permission block found in infra/**, so this control asserted nothing",
            )
        ]
    if not planned:
        return [
            Finding(
                "lambda_permission_composition",
                "(no aws_lambda_permission in plan)",
                "fail",
                "the plan contains no Lambda invoke grant at all, so the composition is unasserted",
            )
        ]

    planned_only = sorted(planned - declared)
    declared_only = sorted(declared - planned)
    if not planned_only and not declared_only:
        return [
            Finding(
                "lambda_permission_composition",
                "(scope) grant names",
                "pass",
                f"the plan's {len(planned)} distinct grant name(s) are exactly those declared in "
                f"infra/: {sorted(planned)}",
            )
        ]

    stale = plan_is_stale_against_source()
    detail = (
        f"the plan grants {planned_only or 'nothing'} that infra/ does not declare, and infra/ declares "
        f"{declared_only or 'nothing'} the plan does not grant. A count cannot see this: one grant "
        f"removed and one added leaves the total unchanged."
    )
    return [
        Finding(
            "lambda_permission_composition",
            "(scope) grant names",
            "deferred" if stale else "fail",
            detail
            + (
                " Deferred rather than failed because the committed plan is stale against infra/ (see "
                "plan_freshness), so this names a divergence already remediated in source rather than a "
                "live defect. It becomes a FAIL against a current plan."
                if stale
                else " The plan is current, so this is a live divergence."
            ),
        )
    ]


def plan_is_stale_against_source() -> bool:
    """Whether the committed fixture's recorded infra digest differs from the working tree's.

    An unanswerable comparison (no fixture, no recorded digest, no digest computable) counts as stale.
    """
    try:
        document = compliant_fixture()
    except (OSError, ValueError):
        return True
    recorded = recorded_infra_digest(document)
    if recorded is None:
        return True
    try:
        return recorded != plan_fixtures.infra_digest(REPO_ROOT)
    except ValueError:
        return True


def check_security_group_zero_inbound(_plan: Plan) -> list[Finding]:
    """Every security group in `infra/` declares NO inbound rule from any CIDR.

    Read from source: no inline `ingress` block on any `aws_security_group`, and every standalone
    `aws_vpc_security_group_ingress_rule` names a `referenced_security_group_id` and no CIDR or
    prefix list. Finding no group fails.
    """
    findings: list[Finding] = []
    group_blocks = 0
    ingress_rules = 0

    for path, text in _infra_tf_sources():
        where = str(path.relative_to(INFRA_DIR.parent))
        for match in re.finditer(
            r'resource\s+"aws_security_group"\s+"([A-Za-z0-9_-]+)"\s*\{', text
        ):
            group_blocks += 1
            body = _balanced_block_body(text, match.end() - 1)
            has_inline_ingress = re.search(r"(?m)^\s*ingress\s*\{", body) is not None
            findings.append(
                Finding(
                    "security_group_zero_inbound",
                    f"{where}: aws_security_group.{match.group(1)}",
                    "fail" if has_inline_ingress else "pass",
                    "declares an inline `ingress` block"
                    if has_inline_ingress
                    else "no inline ingress block",
                )
            )

        for match in re.finditer(
            r'resource\s+"aws_vpc_security_group_ingress_rule"\s+"([A-Za-z0-9_-]+)"\s*\{', text
        ):
            ingress_rules += 1
            body = _balanced_block_body(text, match.end() - 1)
            cidrs = re.findall(r"(?m)^\s*(cidr_ipv4|cidr_ipv6|prefix_list_id)\s*=", body)
            referenced = re.search(r"(?m)^\s*referenced_security_group_id\s*=", body) is not None
            ok = not cidrs and referenced
            findings.append(
                Finding(
                    "security_group_zero_inbound",
                    f"{where}: aws_vpc_security_group_ingress_rule.{match.group(1)}",
                    "pass" if ok else "fail",
                    "source is another security group"
                    if ok
                    else f"admits a network source rather than a security group: {cidrs or 'no referenced_security_group_id'}",
                )
            )

    if group_blocks == 0:
        return [
            Finding(
                "security_group_zero_inbound",
                str(INFRA_DIR),
                "fail",
                "no aws_security_group block found in infra/**, so this control asserted nothing. "
                "The no-ingress row must not be read as a pass on this run.",
            )
        ]
    findings.append(
        Finding(
            "security_group_zero_inbound",
            "(scope) groups and rules examined",
            "pass",
            f"{group_blocks} security group(s), {ingress_rules} standalone ingress rule(s)",
        )
    )
    return findings


def check_security_group_egress_scoped(_plan: Plan) -> list[Finding]:
    """Every egress rule in `infra/` is TCP on 443 and nothing wider.

    Each standalone egress rule must be `tcp` on exactly 443-443; an open CIDR is allowed only
    there. An inline `egress` block on an `aws_security_group` fails.
    """
    findings: list[Finding] = []
    examined = 0

    for path, text in _infra_tf_sources():
        where = str(path.relative_to(INFRA_DIR.parent))
        for match in re.finditer(
            r'resource\s+"aws_vpc_security_group_egress_rule"\s+"([A-Za-z0-9_-]+)"\s*\{', text
        ):
            examined += 1
            body = _balanced_block_body(text, match.end() - 1)
            subject = f"{where}: aws_vpc_security_group_egress_rule.{match.group(1)}"
            protocol = _attribute_value(body, "ip_protocol")
            from_port = _attribute_value(body, "from_port")
            to_port = _attribute_value(body, "to_port")
            open_cidr = bool(
                re.search(r'(?m)^\s*cidr_ipv4\s*=\s*"0\.0\.0\.0/0"', body)
                or re.search(r'(?m)^\s*cidr_ipv6\s*=\s*"::/0"', body)
            )

            problems: list[str] = []
            if protocol != "tcp":
                problems.append(f"ip_protocol={protocol!r} (must be tcp; -1 opens every protocol)")
            if (from_port, to_port) != (str(_SCOPED_EGRESS_PORT), str(_SCOPED_EGRESS_PORT)):
                problems.append(f"ports {from_port}-{to_port} (must be exactly 443-443)")
            if open_cidr and (from_port, to_port) != (
                str(_SCOPED_EGRESS_PORT),
                str(_SCOPED_EGRESS_PORT),
            ):
                problems.append("an open CIDR on a port other than 443")

            findings.append(
                Finding(
                    "security_group_egress_scoped",
                    subject,
                    "fail" if problems else "pass",
                    "; ".join(problems)
                    if problems
                    else f"tcp/443 to {'0.0.0.0/0 (the JWKS exception, K.3)' if open_cidr else 'a group or prefix list'}",
                )
            )

        for match in re.finditer(
            r'resource\s+"aws_security_group"\s+"([A-Za-z0-9_-]+)"\s*\{', text
        ):
            body = _balanced_block_body(text, match.end() - 1)
            if re.search(r"(?m)^\s*egress\s*\{", body):
                examined += 1
                findings.append(
                    Finding(
                        "security_group_egress_scoped",
                        f"{where}: aws_security_group.{match.group(1)}",
                        "fail",
                        "declares an inline `egress` block; egress must be standalone "
                        "aws_vpc_security_group_egress_rule resources so each destination is "
                        "separately reviewable and this control can read its port and protocol",
                    )
                )

    if examined == 0:
        return [
            Finding(
                "security_group_egress_scoped",
                str(INFRA_DIR),
                "fail",
                "no egress rule found in infra/**, so this control asserted nothing",
            )
        ]
    findings.append(
        Finding(
            "security_group_egress_scoped",
            "(scope) egress rules examined",
            "pass",
            f"{examined} rule(s)",
        )
    )
    return findings


def _balanced_block_body(text: str, brace_index: int) -> str:
    """The body of the `{`-delimited block whose opening brace is at `brace_index`."""
    depth = 0
    index = brace_index
    in_string = False
    while index < len(text):
        char = text[index]
        if char == '"' and text[index - 1] != "\\":
            in_string = not in_string
        elif not in_string:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_index + 1 : index]
        index += 1
    return text[brace_index + 1 :]


def _attribute_value(body: str, key: str) -> str | None:
    """The literal value of a top-level `key = value` in a block body, or None when unset."""
    found = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*"?([^"\n]+?)"?\s*$', body)
    return found.group(1).strip() if found else None


def check_log_group_retention(plan: Plan) -> list[Finding]:
    findings = []
    for r in plan.by_type("aws_cloudwatch_log_group"):
        after = plan.after(r)
        retention = after.get("retention_in_days")
        # 0 or unset means "never expire".
        ok = isinstance(retention, (int, float)) and retention > 0
        findings.append(
            Finding(
                "log_group_retention",
                r["address"],
                "pass" if ok else "fail",
                f"retention_in_days={retention!r}",
            )
        )
    return findings


def check_log_group_kms(plan: Plan) -> list[Finding]:
    """Every log group's `kms_key_id` is a known non-empty value or unknown at plan time.

    Null, absent-and-known, and empty or blank strings fail.
    """
    findings = []
    for r in plan.by_type("aws_cloudwatch_log_group"):
        kms = plan.after(r).get("kms_key_id")
        deferred = kms is None and plan.after_unknown(r).get("kms_key_id") is True
        ok = deferred or (isinstance(kms, str) and bool(kms.strip()))
        detail = f"kms_key_id={kms!r}"
        if deferred:
            detail += " (unknown at plan time; set from a same-apply key reference)"
        findings.append(Finding("log_group_kms", r["address"], "pass" if ok else "fail", detail))
    return findings


def check_cloudtrail_enabled(plan: Plan) -> list[Finding]:
    trails = plan.by_type("aws_cloudtrail")
    if not trails:
        return [
            Finding("cloudtrail_enabled", "(no aws_cloudtrail in plan)", "fail", "no trail present")
        ]
    findings = []
    for r in trails:
        findings.append(
            Finding("cloudtrail_enabled", r["address"], "pass", "trail present in plan")
        )
    return findings


def check_bedrock_no_static_credentials(plan: Plan) -> list[Finding]:
    """No `aws_iam_access_key` (static credential) is created anywhere in the plan."""
    access_keys = plan.by_type("aws_iam_access_key")
    if not access_keys:
        return [
            Finding(
                "bedrock_no_static_credentials",
                "(no aws_iam_access_key in plan)",
                "pass",
                "no static IAM access key resources present -- Bedrock access is role-based only",
            )
        ]
    return [
        Finding(
            "bedrock_no_static_credentials", r["address"], "fail", "static IAM access key created"
        )
        for r in access_keys
    ]


def check_mandatory_tags(plan: Plan) -> list[Finding]:
    """Every taggable resource carries every `MANDATORY_TAGS` key, at its fixed value where one is set."""
    findings = []
    for r in plan.resource_changes:
        after = plan.after(r)
        if "tags_all" not in after:
            continue  # not a taggable resource type
        tags_all = after.get("tags_all") or {}
        missing = MANDATORY_TAGS - set(tags_all.keys())
        wrong = {
            key: tags_all[key]
            for key, value in MANDATORY_TAG_VALUES.items()
            if key in tags_all and tags_all[key] != value
        }
        ok = not missing and not wrong
        findings.append(
            Finding(
                "mandatory_tags",
                r["address"],
                "pass" if ok else "fail",
                f"missing={sorted(missing)!r} wrong_value={wrong!r}",
            )
        )
    return findings


# Expected count per resource type, asserted by equality. `per_tool=k` multiplies the plan's own
# action-Lambda instance count; `fixed=n` needs a `basis` enumerating what it counts.
#: Minimum word count of an `ExpectedCount.basis`.
EXPECTED_COUNT_BASIS_MIN_WORDS = 4


@dataclass(frozen=True)
class ExpectedCount:
    """One resource type's expected cardinality, plus the basis a reader can check it against.

    `derive`, when set, computes the base count from an in-repo source at check time.
    `per_managed_dynamodb` is added once per module attaching its DynamoDB document as managed
    policies (`_managed_dynamodb_modules`).
    """

    basis: str
    fixed: int = 0
    per_tool: int = 0
    derive: Callable[[], int] | None = None
    per_managed_dynamodb: int = 0

    def resolve(self, action_lambda_instances: int, managed_dynamodb_modules: int = 0) -> int:
        base = self.derive() if self.derive is not None else self.fixed
        return (
            base
            + self.per_tool * action_lambda_instances
            + self.per_managed_dynamodb * managed_dynamodb_modules
        )

    def formula(self, action_lambda_instances: int, managed_dynamodb_modules: int = 0) -> str:
        base = "derived" if self.derive is not None else str(self.fixed)
        text = f"{self.per_tool}*{action_lambda_instances}+{base}" if self.per_tool else base
        if self.per_managed_dynamodb:
            text += f"{self.per_managed_dynamodb:+d}*{managed_dynamodb_modules}"
        return text


def _api_origin_target_count() -> int:
    """One ALB target-group attachment per `execute-api` endpoint ENI, i.e. per private subnet.

    Read from `infra/terraform.tfvars.example`'s `vpc_private_subnet_ids`. Raises when the list is
    absent or empty.
    """
    example = (INFRA_DIR / "terraform.tfvars.example").read_text(encoding="utf-8")
    listed = re.search(r"(?ms)^vpc_private_subnet_ids\s*=\s*\[(.*?)\]", example)
    if listed is None:
        raise ValueError(
            "infra/terraform.tfvars.example declares no `vpc_private_subnet_ids` list, so the ALB "
            "attachment count has no committed source. Restore it there rather than writing a literal "
            "here: the example is what tells an operator the intended topology."
        )
    entries = re.findall(r'"([^"]+)"', listed.group(1))
    if not entries:
        raise ValueError(
            "`vpc_private_subnet_ids` in terraform.tfvars.example is empty, so the derived attachment "
            "count would be 0 -- an ALB target group with no targets, which is a total outage of all 22 "
            "routes rather than a small miscount."
        )
    return len(entries)


def _root_hcl_list_length(local_name: str) -> int:
    """Number of elements in a `local.<name> = [...]` list literal in `infra/main.tf`.

    Raises when the local is absent or empty.
    """
    source = (INFRA_DIR / "main.tf").read_text(encoding="utf-8")
    match = re.search(rf"(?m)^\s*{re.escape(local_name)}\s*=\s*\[(.*?)\]", source, re.DOTALL)
    if match is None:
        raise ValueError(f"infra/main.tf declares no `{local_name}` list local")
    entries = re.findall(r'"([^"]+)"', match.group(1))
    if not entries:
        raise ValueError(
            f"`{local_name}` in infra/main.tf is an empty list, so it derives no count"
        )
    return len(entries)


#: The vpc module inputs that carry the adopted network; each must be wired to a `var.` reference.
_ADOPTED_NETWORK_INPUTS = (
    "vpc_id",
    "vpc_cidr_block",
    "private_subnet_ids",
    "private_route_table_ids",
    "runtime_subnet_ids",
    "runtime_route_table_ids",
)


def check_adopted_network_wiring(_plan: Plan) -> list[Finding]:
    """The adopted network's ids and CIDR arrive at `module "vpc"` as variables.

    Checks the VPC id and CIDR and the private and Runtime subnet and route-table ids. Fails when
    the call is missing, an adopted input is not passed or is not a `var.` reference, or the vpc
    module declares an adopted resource type itself. Asserts no count.
    """
    findings: list[Finding] = []
    source = (INFRA_DIR / "main.tf").read_text(encoding="utf-8")
    call = re.search(r'(?ms)^module\s+"vpc"\s*\{(.*?)^\}', source)
    if call is None:
        return [
            Finding(
                "adopted_network_wiring",
                'module "vpc"',
                "fail",
                'infra/main.tf declares no `module "vpc"` call, so the adopted network cannot be '
                "verified at all. This is the only place the adopted VPC is referenced.",
            )
        ]

    body = call.group(1)
    for input_name in _ADOPTED_NETWORK_INPUTS:
        wiring = re.search(rf"^\s*{input_name}\s*=\s*(\S+)", body, re.MULTILINE)
        if wiring is None:
            findings.append(
                Finding(
                    "adopted_network_wiring",
                    input_name,
                    "fail",
                    "required for the adopted VPC and not passed by the root. A missing adopted input means the "
                    "module is being called as though it still created its own network.",
                )
            )
            continue
        reference = wiring.group(1)
        findings.append(
            Finding(
                "adopted_network_wiring",
                input_name,
                "pass" if reference.startswith("var.") else "fail",
                f"wired to {reference}"
                + (
                    ""
                    if reference.startswith("var.")
                    else " -- a literal id in tracked content is forbidden and pins the stack to "
                    "one of the candidate VPCs"
                ),
            )
        )

    # Adopted resource types must not be declared in the vpc module's source.
    module_source = "\n".join(
        _strip_tf_comments(path.read_text(encoding="utf-8"))
        for path in sorted((INFRA_DIR / "modules" / "vpc").glob("*.tf"))
    )
    if not module_source.strip():
        return [
            Finding(
                "adopted_network_wiring",
                "modules/vpc/*.tf",
                "fail",
                "no vpc module source was read, so the adopted-type absence assertions below would be "
                "vacuous",
            )
        ]
    for resource_type, reason in sorted(ABSENCE_ASSERTED_TYPES.items()):
        if not resource_type.startswith(
            ("aws_vpc", "aws_subnet", "aws_route_table", "aws_default", "aws_internet")
        ):
            continue
        if resource_type in (
            "aws_vpc_endpoint",
            "aws_vpc_security_group_egress_rule",
            "aws_vpc_security_group_ingress_rule",
        ):
            continue
        declared = (
            re.search(rf'^resource\s+"{resource_type}"', module_source, re.MULTILINE) is not None
        )
        findings.append(
            Finding(
                "adopted_network_wiring",
                f"(must-not-create) {resource_type}",
                "fail" if declared else "pass",
                f"declared as a RESOURCE in modules/vpc/. {reason}"
                if declared
                else "not created, as the adopted VPC requires",
            )
        )

    if not findings:
        return [
            Finding(
                "adopted_network_wiring",
                'module "vpc"',
                "fail",
                "no adopted-network inputs were examined, so this control verified nothing",
            )
        ]
    return findings


#: Gateway endpoints: `s3` and `dynamodb`.
_GATEWAY_ENDPOINT_COUNT = 2


def _vpc_endpoint_count() -> int:
    return _root_hcl_list_length("vpc_interface_endpoint_services") + _GATEWAY_ENDPOINT_COUNT


EXPECTED_RESOURCE_COUNTS: dict[str, ExpectedCount] = {
    # Private REST API origin path; per-route quantities use `per_tool=1`.
    "aws_api_gateway_rest_api": ExpectedCount(
        fixed=1, basis="one PRIVATE REST API fronting every tool route."
    ),
    "aws_api_gateway_resource": ExpectedCount(
        fixed=2,
        per_tool=1,
        basis="one REST resource per route; the enforcement boundary's SPA plane. One per tool, plus 2: "
        "every tool except the agent-only `get_page_context` has a route, and eval_trigger, capabilities and user_settings add three more.",
    ),
    "aws_api_gateway_method": ExpectedCount(
        fixed=2,
        per_tool=1,
        basis="one POST method per route, each carrying the single Cognito authorizer with no exception. "
        "One per tool, plus 2: every tool except the agent-only `get_page_context` has a route, and eval_trigger, capabilities and user_settings add three more.",
    ),
    "aws_api_gateway_request_validator": ExpectedCount(
        fixed=1,
        basis="one body-and-params validator, referenced by every method so no route can omit "
        "it -- the same for_each-driven property the authorizer has.",
    ),
    "aws_api_gateway_model": ExpectedCount(
        fixed=1,
        basis='one {"type":"object"} envelope model, attached to POST methods only. A '
        "second would mean a second body schema -- the drift toward per-field models this design "
        "avoids by keeping the authoritative schema in-Lambda.",
    ),
    "aws_api_gateway_integration": ExpectedCount(
        fixed=2,
        per_tool=1,
        basis="one Lambda proxy integration per route, paired 1:1 with the method above: one per tool, "
        "plus 2.",
    ),
    "aws_api_gateway_authorizer": ExpectedCount(
        fixed=1,
        basis="one COGNITO_USER_POOLS authorizer. A second would mean some route validates tokens "
        "against a different pool.",
    ),
    "aws_api_gateway_deployment": ExpectedCount(
        fixed=1, basis="one deployment of the single stage."
    ),
    "aws_api_gateway_stage": ExpectedCount(
        fixed=1, basis="one stage; a second would be an unaudited parallel invoke surface."
    ),
    "aws_api_gateway_method_settings": ExpectedCount(
        fixed=1,
        basis="one stage-wide method-settings block carrying throttling and access logging.",
    ),
    "aws_lb": ExpectedCount(
        fixed=1,
        basis="one internal ALB behind the CloudFront VPC origin. `internal = true` is the property "
        "whose absence would expose it publicly in a co-tenanted VPC.",
    ),
    "aws_lb_listener": ExpectedCount(
        fixed=1, basis="one listener; the owner chose `http-only` for the in-VPC hop."
    ),
    "aws_lb_target_group": ExpectedCount(
        fixed=1, basis="one target group of the `execute-api` endpoint's ENI addresses."
    ),
    "aws_lb_target_group_attachment": ExpectedCount(
        derive=_api_origin_target_count,
        basis="one attachment per `execute-api` endpoint ENI, i.e. per private subnet. Derived rather "
        "than written as `2`: the subnet list is in the gitignored tfvars, so a literal would be a stale "
        "row the moment a third subnet is added.",
    ),
    "aws_cloudfront_vpc_origin": ExpectedCount(
        fixed=1,
        basis="one VPC origin. CloudFront routes to the load balancer by ARN, so this is the whole of "
        "the private `/api/*` path's edge-to-origin binding.",
    ),
    # --- one or more per CRM action tool ---
    "aws_lambda_function": ExpectedCount(
        per_tool=1,
        fixed=10,
        basis="one action Lambda per tool, never shared, plus 10: the nine singleton lambda_action instantiations (interceptor, pre-token-generation, capabilities, eval_trigger, chat_interceptor, chat_target, user_settings, sweep_control, sweep_task), and the agent relay "
        "(module.agent_relay, a Node.js Function URL streamer and the only one outside the "
        "lambda_action module).",
    ),
    "aws_iam_role": ExpectedCount(
        per_tool=1,
        fixed=16,
        basis="one execution role per action Lambda (never shared -- the security baseline's `one "
        "execution role per function` row), plus 16: agentcore_gateway, agentcore_runtime, the "
        "CodeBuild image builder, the agent relay, the VPC flow-log delivery role, the eval_sweep "
        "state machine, the chat Gateway (module.mcp_chat_gateway), and one each for the nine singleton lambda_action instantiations (interceptor, pre-token-generation, capabilities, eval_trigger, chat_interceptor, chat_target, user_settings, sweep_control, sweep_task). Every "
        "function has its own, so the total is aws_lambda_function's plus six non-Lambda roles.",
    ),
    "aws_cloudwatch_log_group": ExpectedCount(
        per_tool=1,
        fixed=15,
        basis="one log group per action Lambda, plus 15: CodeBuild, the AgentCore Runtime, the API "
        "access logs, the VPC flow logs, the eval_sweep state machine, the agent relay, and one each "
        "for the nine singleton lambda_action instantiations (interceptor, pre-token-generation, capabilities, eval_trigger, chat_interceptor, chat_target, user_settings, sweep_control, sweep_task). CodeBuild, the Runtime, the API, the flow logs and the state machine are not "
        "Lambda groups, so the total exceeds the function count by five.",
    ),
    "aws_iam_role_policy": ExpectedCount(
        per_tool=4,
        fixed=51,
        basis="FOUR per action Lambda -- `dynamodb`, `dynamodb_kms`, `logs`, and the `vpc_access` "
        "Hyperplane ENI grant, without which the apply is green and every invocation fails "
        "-- plus 51 elsewhere: four each on the interceptor, pre-token-generation, eval_trigger, "
        "chat_interceptor, chat_target, user_settings, sweep_control and sweep_task lambda_action "
        "instantiations (32), capabilities' `logs`, `vpc_access` and `dynamodb` (3), the relay's four "
        "(invoke-runtime, logs, read-audience-parameter, vpc_access), nine at the root "
        "(`agentcore_gateway`, the Runtime's `agentcore_runtime` holding ECR + Bedrock and "
        "`agentcore_runtime_logs` holding CloudWatch, `chat_target_invoke_runtime`, `eval_sweep`, "
        "`eval_trigger_artifacts`, `eval_trigger_states`, `sweep_control_artifacts`, `sweep_task_invoke_runtime`), the chat "
        "Gateway's `gateway`, CodeBuild's, and the flow-log delivery policy. The SPLIT is asserted, "
        "not only the total: a per-tool quantity written as `fixed` still matches the total while "
        "failing to scale, so removing one tool would drop four policies while the expectation "
        "dropped none. MINUS one per module attaching its DynamoDB document as managed policies "
        "instead (`dynamodb_policy_managed`: sweep_control, sweep_task).",
        per_managed_dynamodb=-1,
    ),
    "aws_iam_policy_document": ExpectedCount(
        per_tool=3,
        fixed=51,
        basis="one document behind each of an action Lambda's `dynamodb`, `dynamodb_kms` and `logs` "
        "role policies, plus 51: ten at the root (agentcore_gateway permissions, agentcore_runtime "
        "permissions and logs, the two agent-runtime relay-only resource policies, "
        "chat_target_invoke_runtime, eval_sweep_permissions, eval_trigger_artifacts, sweep_control_artifacts, "
        "sweep_task_invoke_runtime), two in agent_relay, two in agentcore, one in api (its resource "
        "policy), three in frontend_cdn (origin, frontend KMS, logs TLS-deny), three in observability "
        "(CloudTrail bucket, access-logs TLS-deny and the logs CMK's policy), two in eval_artifacts "
        "(writer and bucket), one in vpc (flow-log "
        "delivery), one in mcp_chat_gateway (gateway permissions), three each on the interceptor, "
        "pre-token-generation, eval_trigger, chat_interceptor, chat_target, user_settings, "
        "sweep_control and sweep_task instantiations (24), and capabilities' `logs` and `dynamodb` (2). "
        "`declared_infra_block_counts` asserts the Terraform side of the same quantity.",
    ),
    "aws_lambda_permission": ExpectedCount(
        per_tool=2,
        fixed=11,
        basis="two per tool plus 11, and the COMPOSITION is the assertion rather than the total: "
        "`gateway_target_role` (every tool plus the interceptor, capabilities, chat_interceptor and "
        "chat_target singletons) + `apigw_invoke` (every routed tool plus eval_trigger, capabilities "
        "and user_settings) + cognito's `pre_token_generation` + agentcore's and mcp_chat_gateway's "
        "`gateway_invoke_interceptor` + frontend_cdn's `agent_relay_function_url` and "
        "`agent_relay_invoke_function`. `fixed` is 11: the four singleton `gateway_target_role` "
        "grants, two net `apigw_invoke` (three singletons less the agent-only `get_page_context`, "
        "which has no route), and the five single grants. A count cannot see a SUBSTITUTION (one "
        "grant removed and another added leaves the total unchanged), so "
        "`check_lambda_permission_composition` compares grant NAMES in both directions against "
        "`infra/`.",
    ),
    "aws_bedrockagentcore_gateway_target": ExpectedCount(
        per_tool=1,
        fixed=1,
        basis="one Gateway MCP target per tool on the tool Gateway (the enforcement boundary's MCP "
        "plane), plus 1: the chat Gateway's single `chat` target",
    ),
    # --- derived from the DynamoDB schema ---
    "aws_dynamodb_table": ExpectedCount(
        derive=lambda: len(_load_dynamodb_schema_gsis()),
        basis="one per entry in `crm.store.schema`'s table registry, loaded from the real module "
        "rather than transcribed, so adding a table cannot leave this number behind",
    ),
    "aws_dynamodb_resource_policy": ExpectedCount(
        derive=lambda: len(_load_dynamodb_schema_gsis()),
        basis="one per table (`for_each = local.tables` in `modules/dynamodb/policy.tf`), "
        "derived from the same table registry as `aws_dynamodb_table`",
    ),
    # --- fixed by design, with the members enumerated ---
    "aws_s3_bucket": ExpectedCount(
        fixed=5,
        basis="frontend origin, frontend access logs, CloudTrail delivery, CloudTrail access logs, "
        "and the eval-artifacts bucket (module.eval_artifacts, which holds published sweep output)",
    ),
    "aws_s3_bucket_public_access_block": ExpectedCount(
        fixed=5, basis="one per bucket -- BPA is Critical-if-missing and admits no exception"
    ),
    "aws_s3_bucket_server_side_encryption_configuration": ExpectedCount(
        fixed=5, basis="one per bucket; SSE-KMS with a CMK on each"
    ),
    "aws_s3_bucket_policy": ExpectedCount(
        fixed=5,
        basis="one per bucket, each carrying the aws:SecureTransport TLS-deny statement; the "
        "CloudTrail, frontend-origin and eval-artefacts policies also carry the principal Denies "
        "on the data plane (writes only, for the origin) and on bucket administration that "
        "store_principal_deny reads from source",
    ),
    "aws_s3_bucket_ownership_controls": ExpectedCount(
        fixed=5,
        basis="one per bucket, each set to BucketOwnerEnforced so object ACLs cannot grant access "
        "around the bucket policy",
    ),
    "aws_s3_bucket_versioning": ExpectedCount(
        fixed=5,
        basis="one per bucket -- versioning is required on every bucket whose data-classification is "
        "confidential or internal, which is all five",
    ),
    "aws_s3_bucket_logging": ExpectedCount(
        fixed=3,
        basis="the frontend origin, the CloudTrail delivery bucket and the eval-artifacts bucket log "
        "to access-log buckets; the two access-log buckets do not log to themselves",
    ),
    "aws_s3_bucket_lifecycle_configuration": ExpectedCount(
        fixed=5,
        basis="expiry on the frontend access logs, both CloudTrail buckets and the eval-artifacts "
        "bucket, plus the frontend origin's, which expires only noncurrent versions and incomplete "
        "uploads so the current SPA build is never expired",
    ),
    "aws_kms_key": ExpectedCount(
        fixed=5,
        basis="one CMK each for DynamoDB, the frontend, CloudTrail, CloudWatch Logs, and the "
        "eval-artifacts bucket. The last is declared at the ROOT (aws_kms_key.eval_artifacts) rather "
        "than inside its module, so the module's policies render at plan time",
    ),
    "aws_kms_alias": ExpectedCount(
        fixed=5, basis="one alias per CMK above, so each key is addressable by a stable name"
    ),
    # --- relay, runtime, frontend and supporting resources ---
    "aws_bedrockagentcore_resource_policy": ExpectedCount(
        fixed=2,
        basis="two resource-based policies implementing the sole-invoker control -- one on the agent "
        "runtime and one on its endpoint -- each admitting only the relay role, so no other "
        "principal can invoke the Runtime even if its own identity policy allowed it",
    ),
    "aws_lambda_function_url": ExpectedCount(
        fixed=1,
        basis="one, on the agent relay: the only Lambda in the deployment reachable without going "
        "through API Gateway or the Gateway, and the reason the relay carries an origin-verification "
        "secret and a JWT check of its own",
    ),
    "aws_kms_key_policy": ExpectedCount(
        fixed=3,
        basis="three, each carrying the account-root statement and the Denies on key use and key "
        "administration outside its principal lists: the frontend CMK's, which also grants "
        "CloudFront's service principal decrypt against this distribution only; the DynamoDB table "
        "CMK's; and the eval-artefacts CMK's, separate from its key because an inline policy would "
        "cycle through the CodeBuild role. The logs and CloudTrail CMKs carry their policy inline on "
        "the key resource, which is why this is 3 rather than 5 -- a discrepancy worth keeping "
        "visible in the basis",
    ),
    "aws_cloudfront_function": ExpectedCount(
        fixed=2,
        basis="two viewer-request functions on the distribution: SPA history fallback (rewrites deep "
        "links to /index.html) and the /api path rewrite. Both run on every viewer request, so they "
        "are request-path code that no Lambda control covers",
    ),
    "aws_cognito_resource_server": ExpectedCount(
        fixed=2,
        basis="two, declaring the custom scopes each Gateway's CUSTOM_JWT authorizer checks: the tool "
        "Gateway's, and the chat Gateway's `supervisor` server -- the scope namespaces the MCP "
        "planes' authorization is expressed in",
    ),
    "aws_cognito_user_pool_domain": ExpectedCount(
        fixed=1,
        basis="one hosted-UI domain, the sign-in surface the SPA redirects to and the issuer of the "
        "tokens every plane accepts",
    ),
    "aws_ssm_parameter": ExpectedCount(
        fixed=1,
        basis="one: the relay's expected Cognito audience. The origin-verification secret is "
        "NOT a second one -- it is created outside terraform so its value never enters state, which "
        "is why this is 1 and not 2",
    ),
    "aws_s3_object": ExpectedCount(
        fixed=1,
        basis="one: the agent source archive CodeBuild reads to build the Runtime image. The SPA "
        "build is published by `deploy.sh` rather than by terraform, so it is not an object here",
    ),
    "terraform_data": ExpectedCount(
        fixed=3,
        basis="three plan-time lifecycle preconditions rather than cloud resources: the "
        "reserved-concurrency headroom guard (fails the apply if the sum of reserved concurrency "
        "would leave the account's unreserved pool below its floor), `agent_only_tools_guard` and "
        "`chat_scope_agreement`",
    ),
    "aws_cognito_user_pool": ExpectedCount(fixed=1, basis="one pool for both planes"),
    "aws_cognito_user_group": ExpectedCount(
        fixed=1,
        basis="one, the `admin` group whose `cognito:groups` claim gates live sweeps.",
    ),
    "aws_sfn_state_machine": ExpectedCount(
        fixed=1, basis="one, the Standard `eval_sweep` state machine."
    ),
    "aws_vpc_endpoint_policy": ExpectedCount(
        fixed=1,
        basis="one, on the `states` interface endpoint, scoping it to this state machine.",
    ),
    "aws_cognito_user_pool_client": ExpectedCount(
        fixed=3, basis="the SPA client, the MCP client and the MCP OAuth client"
    ),
    "aws_cloudfront_distribution": ExpectedCount(
        fixed=1, basis="one distribution fronting the SPA, the API and the agent relay"
    ),
    "aws_cloudfront_origin_access_control": ExpectedCount(
        fixed=2, basis="one OAC; the origin bucket is reachable only through it"
    ),
    "aws_bedrockagentcore_gateway": ExpectedCount(
        fixed=2,
        basis="two Gateways, each carrying a CUSTOM_JWT authorizer and a REQUEST interceptor: the tool "
        "Gateway (module.agentcore) and the chat Gateway (module.mcp_chat_gateway)",
    ),
    "aws_bedrockagentcore_agent_runtime": ExpectedCount(
        fixed=1, basis="one runtime hosting the Strands supervisor"
    ),
    "aws_cloudtrail": ExpectedCount(fixed=1, basis="one trail capturing API activity"),
    "aws_ecr_repository": ExpectedCount(
        fixed=1, basis="one repository for the agent runtime image"
    ),
    "aws_ecr_lifecycle_policy": ExpectedCount(fixed=1, basis="one policy on the repository above"),
    "aws_codebuild_project": ExpectedCount(
        fixed=1, basis="one project building and pushing the agent runtime image"
    ),
    "null_resource": ExpectedCount(
        fixed=1, basis="one build trigger for the CodeBuild project above"
    ),
    # --- private-only VPC; each basis states which group admits which source ---
    "aws_security_group": ExpectedCount(
        fixed=4,
        basis="four: the Lambda ENI group (no ingress, egress only to the endpoint group), the "
        "interface-endpoint group (ingress 443 from the Lambda group, the ALB and the Runtime group; "
        "no egress), the api_origin ALB group (ingress only from the CloudFront VPC origin group, "
        "egress 443 to the endpoint group), and the AgentCore Runtime group (no ingress, egress only "
        "to the endpoint group and the gateway prefix lists). A fifth group in the plan is a "
        "destination nothing in this design explains.",
    ),
    "aws_vpc_security_group_ingress_rule": ExpectedCount(
        fixed=4,
        basis="four, and the load-bearing property is which group has NONE. The endpoint group takes "
        "443 from the Lambda group, from the ALB, and from the Runtime group (the health-check ALB "
        "rule is the one whose absence fails every `/api/*` request behind a green apply). The ALB "
        "takes `listener_port` from the CloudFront VPC origin group only. The Lambda ENI group and "
        "the Runtime group take NOTHING: nothing initiates a connection to their ENIs, so a rule "
        "admitting anything to either is the unexplained surface this count exists to catch.",
    ),
    "aws_vpc_security_group_egress_rule": ExpectedCount(
        fixed=8,
        basis="eight: the Lambda group's rule to the interface-endpoint group, one per gateway "
        "endpoint's managed prefix list for the Lambda group (`s3`, `dynamodb`), `api_origin`'s ALB "
        "egress to the `execute-api` endpoint, the vpc module's NAT-conditional HTTPS rule, and the "
        "three Runtime-group rules (to the endpoint group, plus one per gateway endpoint's prefix "
        "list). The per-gateway rules are the ones whose absence is silent: a gateway endpoint is "
        "reached over the route table, not through the endpoint group, so without them every "
        "S3/DynamoDB (and ECR image-layer) call times out behind a green apply. Pinned rather than "
        "derived because the NAT rule exists only while `vpc_nat_gateway_enabled` is true, a "
        "gitignored variable no tracked source can derive. The committed fixture was captured with "
        "NAT enabled; see `_fixture_provenance`.",
    ),
    "aws_vpc_endpoint": ExpectedCount(
        derive=_vpc_endpoint_count,
        basis="one per entry in `local.vpc_interface_endpoint_services` plus the two gateway endpoints "
        "(`s3`, `dynamodb`). With no internet gateway and no NAT this set is the workload's entire "
        "reachable AWS surface, so the count is the reachability policy's own cardinality.",
    ),
    # --- managed policies and the egress path; NAT rows state the NAT-enabled capture ---
    "aws_iam_policy": ExpectedCount(
        fixed=2,
        basis="two, attached rather than inline: the ENI function-code Deny "
        "(module.eni_function_code_deny) shared by every VPC Lambda role, and sweep_control's "
        "second DynamoDB Allow chunk `-dynamodb-allow-2` (`dynamodb_allow_statements_per_policy`). "
        "A third one is a new managed grant and should be read as such, not absorbed by this row. "
        "PLUS two per module attaching its DynamoDB document as managed policies, split by effect "
        "into `-dynamodb-allow` and `-dynamodb-deny`.",
        per_managed_dynamodb=2,
    ),
    "aws_iam_role_policy_attachment": ExpectedCount(
        per_tool=1,
        fixed=11,
        basis="one per action Lambda, plus 11: the ENI function-code Deny on the nine singleton lambda_action instantiations (interceptor, pre-token-generation, capabilities, eval_trigger, chat_interceptor, chat_target, user_settings, sweep_control, sweep_task) and the relay, and sweep_control's `dynamodb_allow[1]` for its second Allow chunk. "
        "Every VPC Lambda role carries the ENI Deny. PLUS two per module attaching its DynamoDB "
        "document as managed policies (`dynamodb_deny`, `dynamodb_allow`).",
        per_managed_dynamodb=2,
    ),
    "aws_nat_gateway": ExpectedCount(
        fixed=1,
        basis="one, as captured with `vpc_nat_gateway_enabled = true` (single-AZ; a zonal resource, so "
        "AZ redundancy would double the bill). Source NAT for egress with no inbound path. Zero in the "
        "disabled branch, which no committed default selects.",
    ),
    "aws_eip": ExpectedCount(
        fixed=1,
        basis="one, as captured with `vpc_nat_gateway_enabled = true`, allocated explicitly for the NAT "
        "gateway rather than auto-assigned by its subnet. Zero in the disabled branch.",
    ),
    "aws_route": ExpectedCount(
        fixed=2,
        basis="two, as captured with `vpc_nat_gateway_enabled = true`: one per adopted private route "
        "table, each to the NAT. Zero in the disabled branch. Standalone resources rather than inline "
        "`route` attributes so an internet-bound route is separately counted and reviewable -- a route "
        "hidden in a route-table attribute is the shape that would slip past a reader.",
    ),
    "aws_flow_log": ExpectedCount(
        fixed=1,
        basis="one VPC-level flow log. The network layer is otherwise the only layer with no access "
        "record: a connection that never completed appears in neither CloudTrail nor a function's log.",
    ),
    "aws_network_interface": ExpectedCount(
        fixed=2,
        basis="two DATA-SOURCE reads, not managed resources: `data.aws_network_interface.execute_api`, "
        "one per adopted private subnet, resolving the `execute-api` endpoint's ENI addresses for the "
        "ALB target group. Counted because every type the plan contains must be accounted for, and a "
        "data read is where an unnoticed live API call would enter an otherwise offline plan. Pinned "
        "rather than derived: the count is `length(var.vpc_private_subnet_ids)`, and that variable has "
        "no tracked default because a subnet id may not appear in committed content -- so the number "
        "is a property of the capture, stated in `_fixture_provenance`, not of the repository.",
    ),
    "aws_cloudfront_origin_request_policy": ExpectedCount(
        fixed=1,
        basis="the `/agent/*` origin-request policy, authored in-module (rather than taken from the "
        "managed set) precisely so its forwarded header set -- which must include "
        "`X-Crm-Authorization` -- is readable from source instead of from a live API read.",
    ),
}

#: Resource types whose absence is asserted, each with the reason.
ABSENCE_ASSERTED_TYPES: dict[str, str] = {
    "aws_iam_access_key": "check_bedrock_no_static_credentials passes iff the plan creates no "
    "static IAM access key at all; a floor would demand one.",
    # Adopted network types the vpc module must not create.
    "aws_vpc": "The VPC is ADOPTED read-only. An `aws_vpc` resource would put a VPC this "
    "stack does not own under Terraform management, and `terraform destroy` would then delete it "
    "along with every co-tenant subnet inside it. Absence is the requirement, not a side effect.",
    "aws_subnet": "Subnets are adopted. Creating one inside a VPC this stack does not own "
    "enlarges the footprint in shared network for no gain, and a created subnet is one `destroy` "
    "removes from under whatever later lands in it.",
    "aws_route_table": "The adopted subnets' existing tables are used as they are. This "
    "stack adds a route to them (see aws_route) and owns no table.",
    "aws_route_table_association": "Re-associating an adopted subnet moves it to a table "
    "this stack controls, silently changing its egress. The NAT route is added to the table the "
    "subnet ALREADY uses instead, which is why no association is created.",
    "aws_default_route_table": "Adopting the default table to empty it "
    "REWRITES the table every subnet without an explicit association routes through -- including "
    "co-tenant subnets. Destructive on apply, not merely wide.",
    "aws_default_security_group": "Adopting the default group to empty it REVOKES "
    "every rule from every co-tenant resource currently sitting on it, at apply time, in an "
    "account that must be treated as production. The property it used to guarantee (the VPC's "
    "defaults are locked down) is now READ-ONLY -- see the preflight default-SG check, which refuses but "
    "never revokes -- and whether the adopted VPC's defaults are actually locked down is an open "
    "gap, NOT resolved by this deletion.",
    "aws_internet_gateway": "The NAT adopts an existing IGW-routed subnet rather than "
    "building a parallel public path, so no gateway is created. A second IGW in a VPC this stack "
    "does not own is both a larger footprint and a second internet-facing edge to reason about.",
}

#: Controls that derive no subject resource type, with the reason; checked both ways by
#: `check_control_subject_floors`.
NO_SUBJECT_TYPE_CONTROLS: dict[str, str] = {
    "no_json_syntax_terraform": "reads the file listing of `infra/` and no plan subject by "
    "construction: its subject is which Terraform files the `*.tf` source readers can see. Its "
    "non-vacuity floor is internal: a missing `infra/` is a `fail`.",
    "adopted_network_wiring": 'reads `infra/main.tf`\'s `module "vpc"` call directly and has no plan '
    "subject by construction: the VPC, subnets and route tables are ADOPTED, so none of them "
    "appears in a plan as a managed resource and a plan-derived control would have nothing to read. "
    "Re-pointed from `_vpc_zone_count` rather than deleted. Its non-vacuity floor is internal: "
    "examining zero adopted inputs is a `fail`, never a silent pass.",
    "security_group_zero_inbound": "reads `infra/**/*.tf` directly and has no plan subject by "
    "construction, deliberately: the committed fixture predates the VPC, so a plan-derived control "
    "would defer and assert nothing -- which is precisely how the no-ingress row stayed "
    "vacuous. `check_no_public_ingress` keeps the plan-side view. Its own non-vacuity floor is internal: "
    "finding zero security groups in source is a `fail`, never a silent pass.",
    "security_group_egress_scoped": "same source-derived subject and the same internal non-vacuity "
    "floor (zero egress rules found is a `fail`). Reading a security group by eye is the same defect "
    "class as the vacuous ingress pass, which is why the 443 scoping is a control rather than a "
    "reviewer's note.",
    "declared_infra_block_counts": "counts BLOCKS in `infra/**/*.tf` text, so it has no plan "
    "subject by construction -- that independence from the committed fixture is the whole reason it "
    "exists. Its own non-vacuity floor is internal: reading zero source files is a `fail`, "
    "never a silent pass over an empty count.",
    "mandatory_tags": "iterates every resource in `plan.resource_changes` and selects by the "
    "presence of `tags_all`, so it is type-agnostic by design; a floor on one type would narrow it.",
    "bedrock_inference_profile_scoping": "reads the `BedrockInference` statement's `resources` from "
    "source. Permanently source-derived rather than pending a fixture regeneration: the attribute is "
    "a `concat()` of two `for` comprehensions, so the document's computed `.json` is unknown at plan "
    "time -- the same limit `dynamodb_no_wildcard_action` records. Its `(coverage) statements "
    "granting bedrock:InvokeModel*` assertion is the floor: zero matches fails.",
    "s3_access_log_delivery_grant": "reads `aws_s3_bucket_logging` blocks and the policy documents "
    "of their destination buckets from source. Both destination policies interpolate their own "
    "bucket's ARN, so the computed `.json` is unknown at plan time regardless of fixture currency -- "
    "the same limit `dynamodb_no_wildcard_action` records. `aws_s3_bucket` is floored at 4 by the S3 "
    "controls; this control's own `(coverage) aws_s3_bucket_logging resources in infra/` assertion is "
    "its floor, so removing access logging entirely fails rather than emitting nothing. It also reads "
    "`aws_lb` bodies for `access_logs { enabled = true }` and requires an ELB log-delivery grant "
    "(`logdelivery.elasticloadbalancing.amazonaws.com`, a DIFFERENT principal from the S3 one) "
    "somewhere in the tree -- missing, `aws_lb` creation fails outright rather than losing logs. That "
    "arm is tree-wide rather than per-bucket because `access_logs.bucket` is a cross-module input, "
    "which source cannot resolve; the prefix half is held by the root's one "
    "`alb_access_log_prefix` local instead of by this control.",
    "relay_denies_command_execution": 'reads `data "aws_iam_policy_document"` bodies from '
    "`infra/modules/agent_relay/*.tf` rather than the plan. Two independent reasons, and the second "
    "holds even after the fixture is regenerated: the compliant fixture predates the relay, "
    "AND an `aws_iam_policy_document`'s computed `.json` is unknown at plan time whenever it "
    "concatenates a same-apply reference -- the same limit `dynamodb_no_wildcard_action` records. It "
    "carries its own coverage assertion (`(coverage) policy documents in modules/agent_relay` fails "
    "if it finds none) in place of an `EXPECTED_RESOURCE_COUNTS` entry.",
    "relay_invoke_scoped_to_runtime_and_endpoint": "same source-derived reason as "
    "`relay_denies_command_execution` above. Fails explicitly when no `InvokeAgentRuntime` Allow is "
    "found at all, so an emptied or relocated policy is a failure rather than zero findings.",
    "relay_no_forbidden_grants": "same source-derived reason as `relay_denies_command_execution` "
    "above, with its own `(coverage) Allow statements in modules/agent_relay` assertion.",
    "runtime_resource_policy_sole_invoker": "reads `aws_bedrockagentcore_resource_policy` blocks and "
    "their policy documents from source via `tf_resource_blocks`, not `plan.by_type`. The resource "
    "type appears in the fixture (2 instances), but a plan-time form still could not read the policy "
    "documents it asserts on, so source is the subject. Its `(count) "
    "aws_bedrockagentcore_resource_policy` assertion is the floor: exactly 2 are required.",
    "runtime_network_mode_vpc": 'reads the literal `runtime_network_mode = "VPC"` at the '
    '`module "agentcore"` call site in `infra/main.tf`, not `plan.by_type`: the module renders '
    "network_mode from a variable, so the value is a module input the committed fixture predates and a "
    "plan-derived form would defer. Its own non-vacuity floor is internal: a missing "
    '`module "agentcore"` block is a `fail`, never a silent pass.',
    "runtime_resource_policy_source_vpce_deny": "reads the two `agent_runtime_*` "
    "`data aws_iam_policy_document` bodies from source, because the SourceVpce Allow/Deny are "
    '`dynamic "statement"` blocks that `policy_document_statements` cannot see and the toggles gate '
    "them off by default. It asserts the AUTHORED shape (the live account state is a T3 check). Its "
    "non-vacuity floor is internal: a document not found, or no Deny found, is a `fail`.",
    "dynamodb_data_plane_principal_deny": "reads `aws_dynamodb_resource_policy.tables`, "
    '`aws_kms_key_policy.dynamodb`, the root local and the `module "dynamodb"` call from source '
    "rather than the plan: both policies interpolate role ARNs created in the same apply, so the "
    "rendered `policy` is unknown at plan time -- the same limit `dynamodb_no_wildcard_action` "
    "records. Its non-vacuity floor is internal: each `(count)` assertion requires exactly one "
    "policy resource, so removing either fails rather than emitting nothing.",
    "store_principal_deny": "reads the logs, CloudTrail, frontend and eval-artefacts key and bucket "
    "policy resources, the two root locals and the three module calls from source rather than the "
    "plan: every allowlist is a list of role ARNs created in the same apply, so the rendered "
    "`policy` is unknown at plan time. Its non-vacuity floor is internal: each `(count)` assertion "
    "requires exactly one policy resource, so removing one fails rather than emitting nothing.",
    "teardown_force_destroy": "reads the Terraform source under `infra/` rather than the plan, "
    "because source is the authority for whether the argument is written and a literal `true` cannot "
    "change at apply. It carries its own coverage "
    "assertion instead of a floor: `(coverage) subjects found in infra/` fails if it finds no "
    "bucket or no ECR repository, which is the vacuity an `EXPECTED_RESOURCE_COUNTS` entry would "
    "otherwise provide. `aws_s3_bucket` is floored at 4 by the S3 controls regardless.",
}


#: `<type>:<key>` inventory members the committed fixture cannot contain, with the reason each is
#: absent. Checked by `check_resource_inventory_identity`; an `aws_lambda_function` entry is also
#: excused from `check_dynamodb_leading_keys_isolation`'s matrix-vs-plan coverage, and an
#: `aws_vpc_endpoint` entry (an interface endpoint service) from `aws_vpc_endpoint`'s derived count.
FIXTURE_ABSENT_INSTANCES: dict[str, str] = {}


def _fixture_absent_functions() -> frozenset[str]:
    """Action-Lambda keys `FIXTURE_ABSENT_INSTANCES` declares absent from the committed fixture."""
    prefix = "aws_lambda_function:"
    return frozenset(
        key.removeprefix(prefix) for key in FIXTURE_ABSENT_INSTANCES if key.startswith(prefix)
    )


def _tool_keys_from_definitions() -> frozenset[str]:
    """Every tool key `crm.toolspec.definitions.ALL_TOOLS` declares."""
    repo_root = Path(__file__).resolve().parents[1]
    definitions_path = repo_root / "src" / "crm" / "toolspec" / "definitions.py"
    spec = importlib.util.spec_from_file_location("crm_toolspec_definitions", definitions_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {definitions_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["crm_toolspec_definitions"] = module
    spec.loader.exec_module(module)
    # The tool `name` is `module.lambda_action`'s `for_each` instance key.
    return frozenset(tool.name for tool in module.ALL_TOOLS)


_ACTION_LAMBDA_KEY = re.compile(r'module\.lambda_action\["([^"]+)"\]')

_INTERFACE_ENDPOINT_KEY = re.compile(r'aws_vpc_endpoint\.interface\["([^"]+)"\]$')


def _declared_interface_endpoint_services() -> frozenset[str]:
    """The entries of `local.vpc_interface_endpoint_services` in `infra/main.tf`."""
    return frozenset(
        entry.strip('"')
        for entry in _root_hcl_reference_list("vpc_interface_endpoint_services")
        if entry.startswith('"')
    )


def _planned_interface_endpoint_services(plan: Plan) -> frozenset[str]:
    """The `for_each` keys of every planned `aws_vpc_endpoint.interface[...]`."""
    return frozenset(
        match.group(1)
        for r in plan.by_type("aws_vpc_endpoint")
        if (match := _INTERFACE_ENDPOINT_KEY.search(str(r.get("address", ""))))
    )


def _absent_declared_endpoints(plan: Plan) -> frozenset[str]:
    """Interface endpoint services `FIXTURE_ABSENT_INSTANCES` declares that THIS plan lacks."""
    prefix = "aws_vpc_endpoint:"
    declared = {k.removeprefix(prefix) for k in FIXTURE_ABSENT_INSTANCES if k.startswith(prefix)}
    return frozenset(declared - _planned_interface_endpoint_services(plan))


def check_resource_inventory_identity(plan: Plan) -> list[Finding]:
    """Every member of a derived inventory is present in the plan, **by identity, not by count**.

    Two inventories are asserted:

    * **Action Lambdas** — one per key in `crm.toolspec.definitions.ALL_TOOLS`, matched from the
      plan's `module.lambda_action["<key>"]` address.
    * **DynamoDB tables** — one per entry in `crm.store.schema.TABLES`, matched on the planned table
      name's suffix (the name carries a deploy-time prefix).

    A required member missing from the plan fails, as does an unknown planned action Lambda.
    `FIXTURE_ABSENT_INSTANCES` carries the declared exceptions.
    """
    findings: list[Finding] = []

    expected_tools = _tool_keys_from_definitions()
    planned_tools = {
        match.group(1)
        for r in plan.by_type("aws_lambda_function")
        if (match := _ACTION_LAMBDA_KEY.search(r.get("address", "")))
    }
    findings.append(
        Finding(
            "resource_inventory_identity",
            "(source) ALL_TOOLS is non-empty",
            "pass" if expected_tools else "fail",
            f"tools_declared={len(expected_tools)}",
        )
    )
    planned_singletons = {
        name
        for r in plan.by_type("aws_lambda_function")
        if (name := _lambda_action_function_name(str(r.get("address", ""))))
        and name in _SINGLETON_FUNCTION_NAMES.values()
    }
    for key in sorted(expected_tools - planned_tools):
        reason = FIXTURE_ABSENT_INSTANCES.get(f"aws_lambda_function:{key}", "")
        findings.append(
            Finding(
                "resource_inventory_identity",
                f"aws_lambda_function:{key}",
                "pass" if reason else "fail",
                f"declared in ALL_TOOLS but absent from the plan; recorded_reason={bool(reason)}",
            )
        )
    for key in sorted(planned_tools - expected_tools):
        findings.append(
            Finding(
                "resource_inventory_identity",
                f"aws_lambda_function:{key}",
                "fail",
                "planned as an action Lambda but not declared in ALL_TOOLS -- either a tool was "
                "removed without removing its function, or the plan is from a different toolspec",
            )
        )

    expected_tables = frozenset(_load_dynamodb_schema_gsis())
    planned_tables = {
        name
        for r in plan.by_type("aws_dynamodb_table")
        if isinstance(name := plan.after(r).get("name"), str)
    }
    matched = {table for table in expected_tables if any(p.endswith(table) for p in planned_tables)}
    findings.append(
        Finding(
            "resource_inventory_identity",
            "(source) schema.TABLES is non-empty",
            "pass" if expected_tables else "fail",
            f"tables_declared={len(expected_tables)}",
        )
    )
    for table in sorted(expected_tables - matched):
        reason = FIXTURE_ABSENT_INSTANCES.get(f"aws_dynamodb_table:{table}", "")
        findings.append(
            Finding(
                "resource_inventory_identity",
                f"aws_dynamodb_table:{table}",
                "pass" if reason else "fail",
                f"declared in schema.TABLES but absent from the plan; recorded_reason={bool(reason)}",
            )
        )

    # Exception entries: member absent -> pass; present -> deferred; uncheckable -> fail.
    for member, reason in sorted(FIXTURE_ABSENT_INSTANCES.items()):
        resource_type, _, key = member.partition(":")
        if resource_type == "aws_lambda_function":
            unexercised = key in planned_tools or key in planned_singletons
            uncheckable = (
                key not in expected_tools and key not in _SINGLETON_FUNCTION_NAMES.values()
            )
        elif resource_type == "aws_dynamodb_table":
            unexercised = key in matched
            uncheckable = key not in expected_tables
        elif resource_type == "aws_vpc_endpoint":
            unexercised = key in _planned_interface_endpoint_services(plan)
            uncheckable = key not in _declared_interface_endpoint_services()
        else:
            unexercised = False
            uncheckable = True
        if uncheckable:
            status, detail = (
                "fail",
                "FIXTURE_ABSENT_INSTANCES entry can never be exercised: it names a member no "
                "in-repo inventory declares, or a resource type this control does not check. Wrong "
                "against every plan, so remove it.",
            )
        elif unexercised:
            status, detail = (
                "deferred",
                "FIXTURE_ABSENT_INSTANCES entry is not needed by THIS plan -- the member is "
                "present, so the allowance went unused. NOT reported as stale: this list is scoped "
                "to the committed fixture, and a run against one plan cannot see whether another "
                "still needs the entry. Remove it only once no plan the gate is run against "
                "requires it.",
            )
        else:
            status, detail = ("pass", f"still absent, reason recorded ({reason[:40]}…)")
        findings.append(
            Finding("resource_inventory_identity", f"(declared-absent) {member}", status, detail)
        )
    return findings


def _action_lambda_instance_count(plan: Plan) -> int:
    """How many `module.lambda_action[...]` Lambda instances the plan contains.

    The multiplier base for every `per_tool` expectation.
    """
    return len(
        {
            match.group(1)
            for resource in plan.by_type("aws_lambda_function")
            if (match := _ACTION_LAMBDA_KEY.search(resource.get("address", "")))
        }
    )


def check_resource_existence_counts(plan: Plan) -> list[Finding]:
    """Every resource type's count EQUALS its expected cardinality, and no type is unaccounted for.

    A planned type in neither `EXPECTED_RESOURCE_COUNTS` nor `ABSENCE_ASSERTED_TYPES` fails. A type
    absent from the plan and listed in `FIXTURE_ABSENT_TYPES` is `deferred`. A `FIXTURE_ABSENT_TYPES`
    entry with no expected count and no presence in the plan fails as orphaned.
    """
    findings: list[Finding] = []
    per_tool_base = _action_lambda_instance_count(plan)
    managed_dynamodb = len(_managed_dynamodb_modules(plan))

    findings.append(
        Finding(
            "resource_existence_counts",
            "(base) module.lambda_action instances",
            "pass" if per_tool_base else "fail",
            f"count={per_tool_base}; every per_tool expectation multiplies this, so a zero would "
            "make each of them expect its non-tool remainder and pass vacuously",
        )
    )

    for resource_type, expected in sorted(EXPECTED_RESOURCE_COUNTS.items()):
        actual = len(plan.by_type(resource_type))
        try:
            wanted = expected.resolve(per_tool_base, managed_dynamodb)
        except Exception as exc:  # noqa: BLE001 - reported as a finding
            findings.append(
                Finding(
                    "resource_existence_counts",
                    resource_type,
                    "fail",
                    f"could not resolve the expected count from its source: {exc!r}",
                )
            )
            continue
        lagging = _absent_declared_endpoints(plan) if resource_type == "aws_vpc_endpoint" else set()
        if lagging:
            findings.append(
                Finding(
                    "resource_existence_counts",
                    resource_type,
                    "pass" if actual == wanted - len(lagging) else "fail",
                    f"count={actual} expected={wanted} less {len(lagging)} declared fixture-absent "
                    f"instance(s) {sorted(lagging)} (FIXTURE_ABSENT_INSTANCES) -- basis: "
                    f"{expected.basis}",
                )
            )
            continue
        lagging_count = FIXTURE_LAGGING_COUNTS.get(resource_type)
        if lagging_count is not None and actual == lagging_count[0] != wanted:
            findings.append(
                Finding(
                    "resource_existence_counts",
                    resource_type,
                    "deferred",
                    f"count={actual}, the count declared for the committed fixture "
                    f"({lagging_count[1][:80]}…), so the expectation of {wanted} is UNEXERCISED "
                    "here rather than violated. Not a pass: nothing about this type's cardinality "
                    "was asserted by this run.",
                )
            )
            continue
        if actual == 0 and resource_type in FIXTURE_ABSENT_TYPES:
            findings.append(
                Finding(
                    "resource_existence_counts",
                    resource_type,
                    "deferred",
                    f"absent from this plan and declared absent from the committed fixture "
                    f"({FIXTURE_ABSENT_TYPES[resource_type][:80]}…), so the expectation of {wanted} "
                    f"is UNEXERCISED here rather than violated. Not a pass: nothing about this "
                    f"type's cardinality was asserted by this run.",
                )
            )
            continue
        findings.append(
            Finding(
                "resource_existence_counts",
                resource_type,
                "pass" if actual == wanted else "fail",
                f"count={actual} expected={wanted} "
                f"({expected.formula(per_tool_base, managed_dynamodb)}) -- "
                f"basis: {expected.basis}",
            )
        )

    planned_types = {r.get("type", "") for r in plan.resource_changes} - {""}
    accounted = set(EXPECTED_RESOURCE_COUNTS) | set(ABSENCE_ASSERTED_TYPES)
    unexpected = sorted(planned_types - accounted)
    findings.append(
        Finding(
            "resource_existence_counts",
            "(scope) every planned resource type is accounted for",
            "pass" if not unexpected else "fail",
            f"unaccounted={unexpected!r}; each must get an EXPECTED_RESOURCE_COUNTS entry with a "
            "basis, or an ABSENCE_ASSERTED_TYPES reason. A type no registry names is a resource no "
            "control has ever read.",
        )
    )

    orphaned = sorted(set(FIXTURE_ABSENT_TYPES) - set(EXPECTED_RESOURCE_COUNTS) - planned_types)
    findings.append(
        Finding(
            "resource_existence_counts",
            "(scope) every fixture-absent declaration is still reachable",
            "pass" if not orphaned else "fail",
            f"orphaned={orphaned!r}; each names a type with no expected count and no presence in "
            "this plan, so no run can ever exercise the declaration -- it documents an absence "
            "nothing will notice returning."
            if orphaned
            else f"all {len(FIXTURE_ABSENT_TYPES)} declaration(s) are either counted or present",
        )
    )
    return findings


# ---------------------------------------------------------------------------
# Control registry: control id -> (module tag, baseline row, assertion fn)
# ---------------------------------------------------------------------------


@dataclass
class ControlSpec:
    control_id: str
    module: str
    spec_row: str
    fn: Callable[[Plan], list[Finding]]
    #: Further control ids `fn` emits findings under, besides `control_id`.
    also_emits: tuple[str, ...] = ()


def control_modules() -> dict[str, str]:
    """Every control id this gate can emit, mapped to its module tag (including `also_emits`)."""
    modules: dict[str, str] = {}
    for control in CONTROLS:
        for control_id in (control.control_id, *control.also_emits):
            modules[control_id] = control.module
    return modules


CONTROLS: list[ControlSpec] = [
    ControlSpec("dynamodb_cmk", "dynamodb", "DynamoDB encryption at rest", check_dynamodb_cmk),
    ControlSpec(
        "relay_function_url_streams",
        "relay",
        "Relay Function URL streams rather than buffers",
        check_relay_function_url_streams,
    ),
    ControlSpec("dynamodb_pitr", "dynamodb", "DynamoDB PITR", check_dynamodb_pitr),
    ControlSpec(
        "lambda_permission_composition",
        "lambda",
        "The SET of Lambda invoke grants matches what infra/ declares, not merely the count",
        check_lambda_permission_composition,
    ),
    ControlSpec(
        "adopted_network_wiring",
        "vpc",
        "The adopted VPC id/CIDR and private and Runtime subnet/route-table ids arrive as "
        "variables, never as tracked literals",
        check_adopted_network_wiring,
    ),
    ControlSpec(
        "security_group_zero_inbound",
        "vpc",
        "Security groups admit no inbound rule from any network source",
        check_security_group_zero_inbound,
    ),
    ControlSpec(
        "security_group_egress_scoped",
        "vpc",
        "Security-group egress is tcp/443 only, with an open CIDR permitted at 443 alone",
        check_security_group_egress_scoped,
    ),
    ControlSpec(
        "dynamodb_no_wildcard_action",
        "dynamodb",
        "DynamoDB IAM: no dynamodb:*",
        check_dynamodb_no_wildcard_action,
    ),
    ControlSpec(
        "dynamodb_gsi_parity",
        "dynamodb",
        "DynamoDB IAM: GSIs match schema.py",
        check_dynamodb_gsi_parity,
    ),
    ControlSpec(
        "dynamodb_leading_keys_isolation",
        "dynamodb",
        "DynamoDB IAM: LeadingKeys against table_action_role_matrix.json",
        check_dynamodb_leading_keys_isolation,
    ),
    ControlSpec(
        "lambda_role_uniqueness",
        "lambda",
        "Lambda: one execution role per function",
        check_lambda_role_uniqueness,
    ),
    ControlSpec(
        "lambda_reserved_concurrency_positive",
        "lambda",
        "Lambda: reserved concurrency set on every function",
        check_lambda_reserved_concurrency_positive,
    ),
    ControlSpec(
        "lambda_reserved_concurrency_headroom",
        "lambda",
        "Lambda: reserved concurrency set on every function (sum headroom)",
        check_lambda_reserved_concurrency_headroom,
    ),
    ControlSpec(
        "lambda_env_kms",
        "lambda",
        "Lambda: KMS-encrypted env vars where sensitive",
        check_lambda_env_kms,
    ),
    ControlSpec(
        "lambda_resource_policy_no_wildcard",
        "lambda",
        "Action Lambdas: resource policy admits only Gateway target role and API Gateway",
        check_lambda_resource_policy_no_wildcard,
    ),
    ControlSpec(
        "api_authorizer_on_every_route",
        "api",
        "API Gateway: authorizer on every route",
        check_api_authorizer_on_every_route,
    ),
    ControlSpec(
        "api_throttle", "api", "API Gateway: throttle + burst on every stage", check_api_throttle
    ),
    ControlSpec(
        "api_access_logging", "api", "API Gateway: access logging", check_api_access_logging
    ),
    ControlSpec(
        "api_request_validators",
        "api",
        "API Gateway: request validators",
        check_api_request_validators,
    ),
    ControlSpec("s3_block_public_access", "s3", "S3 frontend bucket: BPA", check_s3_bpa),
    ControlSpec("s3_cmk_encryption", "s3", "S3 encryption: KMS CMK", check_s3_cmk_encryption),
    ControlSpec(
        "s3_tls_deny",
        "s3",
        "S3 frontend bucket: TLS-deny policy",
        check_s3_tls_deny,
    ),
    ControlSpec(
        "s3_access_log_delivery_grant",
        "s3",
        "S3 access logging: destination bucket grants the log-delivery principal (delivery, not "
        "merely configuration)",
        check_s3_access_log_delivery_grant,
    ),
    ControlSpec(
        "relay_denies_command_execution",
        "relay",
        "Relay role: explicit Deny on command-execution and on-behalf-of actions",
        check_relay_denies_command_execution,
    ),
    ControlSpec(
        "relay_invoke_scoped_to_runtime_and_endpoint",
        "relay",
        "Relay role: InvokeAgentRuntime on one runtime and its endpoints, no prefix wildcard",
        check_relay_invoke_scoped_to_runtime_and_endpoint,
    ),
    ControlSpec(
        "relay_no_forbidden_grants",
        "relay",
        "Relay role: no lambda/dynamodb/bedrock/iam:PassRole/secrets grant, no bedrock-agentcore wildcard",
        check_relay_no_forbidden_grants,
    ),
    ControlSpec(
        "runtime_resource_policy_sole_invoker",
        "relay",
        "Runtime resource policy: relay role is the sole invoker on both ARNs",
        check_runtime_resource_policy_sole_invoker,
    ),
    ControlSpec(
        "dynamodb_data_plane_principal_deny",
        "dynamodb",
        "DynamoDB IAM: table and CMK policies deny every principal outside the project roles and "
        "operators",
        check_dynamodb_data_plane_principal_deny,
    ),
    ControlSpec(
        "store_principal_deny",
        "store",
        STORE_PRINCIPAL_DENY_SPEC_ROW,
        check_store_principal_deny,
    ),
    ControlSpec(
        "teardown_force_destroy",
        "teardown",
        "Teardown: force_destroy on log/artefact buckets, force_delete on the ECR repo",
        check_teardown_force_destroy,
    ),
    ControlSpec(
        "no_json_syntax_terraform",
        "source",
        "Source: no *.tf.json under infra/, so every source-reading control sees all of it",
        check_no_json_syntax_terraform,
    ),
    ControlSpec(
        "cloudfront_oac", "cloudfront", "S3 frontend bucket: OAC-only", check_cloudfront_oac
    ),
    ControlSpec(
        "cloudfront_three_behaviours",
        "cloudfront",
        "Transport: CloudFront behaviours",
        check_cloudfront_behaviours,
    ),
    ControlSpec("cognito_mfa", "cognito", "Cognito: MFA", check_cognito_mfa),
    ControlSpec(
        "cognito_password_policy",
        "cognito",
        "Cognito: password policy",
        check_cognito_password_policy,
    ),
    ControlSpec(
        "cognito_account_recovery",
        "cognito",
        "Cognito: account recovery",
        check_cognito_account_recovery,
    ),
    ControlSpec(
        "cognito_admin_only_signup",
        "cognito",
        "Cognito: admin-only sign-up, no public SignUp",
        check_cognito_admin_only_signup,
    ),
    ControlSpec(
        "cognito_tier_and_trigger_version",
        "cognito",
        "Cognito: feature plan Essentials/Plus paired with V2_0 trigger",
        check_cognito_tier_and_trigger_version,
    ),
    ControlSpec(
        "cognito_write_attributes_exclude_scope",
        "cognito",
        "Cognito app clients: write_attributes excludes scope-bearing attrs",
        check_cognito_write_attributes_exclude_scope,
    ),
    ControlSpec(
        "gateway_custom_jwt",
        "agentcore",
        "Gateway: CUSTOM_JWT with discovery URL/clients/audiences/scopes",
        check_gateway_custom_jwt,
    ),
    ControlSpec(
        "gateway_interceptor_present",
        "agentcore",
        "Gateway: REQUEST interceptor with passRequestHeaders: true",
        check_gateway_interceptor,
    ),
    ControlSpec(
        "no_public_ingress",
        "network",
        "Network: no internet-facing SG ingress anywhere",
        check_no_public_ingress,
    ),
    ControlSpec(
        "bedrock_no_static_credentials",
        "bedrock",
        "Bedrock: assumed role, no static access key (auth half)",
        check_bedrock_no_static_credentials,
    ),
    ControlSpec(
        "bedrock_inference_profile_scoping",
        "bedrock",
        "Bedrock: InvokeModel* scoped to named inference profiles/models, never `*` "
        "(resource half)",
        check_bedrock_inference_profile_scoping,
    ),
    ControlSpec(
        "log_group_retention", "logs", "Logs: explicit retention", check_log_group_retention
    ),
    ControlSpec("log_group_kms", "logs", "Logs: KMS CMK on every log group", check_log_group_kms),
    ControlSpec(
        "cloudtrail_enabled",
        "cloudtrail",
        "CloudTrail: enabled for API activity",
        check_cloudtrail_enabled,
    ),
    ControlSpec(
        "runtime_network_mode_vpc",
        "agentcore",
        "Runtime: network_mode is VPC with subnets and SGs wired (egress governed by the VPC)",
        check_runtime_network_mode_vpc,
    ),
    ControlSpec(
        "runtime_resource_policy_source_vpce_deny",
        "relay",
        "Runtime resource policy: staged aws:SourceVpce report-only Allow + inbound Deny on both ARNs",
        check_runtime_resource_policy_source_vpce_deny,
    ),
    ControlSpec(
        "mandatory_tags", "tags", "Tags: mandatory tags on all resources", check_mandatory_tags
    ),
    ControlSpec(
        "resource_inventory_identity",
        "existence",
        "Existence: every derived-inventory member present by identity, not by count",
        check_resource_inventory_identity,
    ),
    ControlSpec(
        "resource_existence_counts",
        "existence",
        "Existence: expected resource counts (11 tables, 28 routes, 36 functions, 1 pool, 3 clients, "
        "2 gateways)",
        check_resource_existence_counts,
    ),
    ControlSpec(
        "declared_infra_block_counts",
        "existence",
        "Existence: declared block counts read from infra/**/*.tf, independent of the plan fixture",
        check_declared_infra_block_counts,
    ),
]

# ---------------------------------------------------------------------------
# Plan-time-inexpressible controls (self-check exemption list).
#
# Each entry names a baseline row with no plan-time assertion, why, and the layer that covers it.
# ---------------------------------------------------------------------------

PLAN_TIME_INEXPRESSIBLE: list[dict[str, str]] = [
    # A. Not in any plan, by construction -- the Terraform state backend.
    {
        "spec_row": "Terraform state bucket/CMK/access-log bucket/lock table "
        "(versioning, BPA, CMK, TLS-deny, access-logging, SSE+PITR on the lock table)",
        "reason": "bootstrap_state_backend.sh runs before `terraform init`, outside the root "
        "module. These resources exist in no "
        "plan, ever -- an absent control is not a control with a missing assertion, so "
        "the self-check cannot notice this by inspecting a plan; it is recorded here so "
        "the gap is visible instead of silent.",
        "layer": "Static assertion over bootstrap_state_backend.sh's own AWS CLI calls, plus a "
        "Layer 4 live re-read of the same six properties post-deploy.",
    },
    {
        "spec_row": "CloudFront standard access logging: delivery to the log bucket",
        "reason": "A DIFFERENT mechanism from the S3 server-access-logging grant that "
        "`s3_access_log_delivery_grant` asserts, and it is not expressible here. CloudFront "
        "standard (legacy) logging to S3 delivers via an ACL grant to the `awslogsdelivery` "
        "canonical user, not via a bucket-policy service principal, so no policy statement can "
        "satisfy it and no `aws_s3_bucket_acl` exists in `infra/`. Whether the grant is required at "
        "all depends on which logging generation the distribution actually uses, which is a property "
        "of the deployed distribution rather than of `logging_config`. Declared rather than fixed "
        "offline: writing an ACL on a guess would be a change to a shared file justified by nothing "
        "checkable. Same reporting shape as the S3 half -- the configuration is present "
        "(`cloudfront.tf:202`) and the delivery is unproven.",
        "layer": "Layer 4: after a real request through the distribution, assert an object "
        "appears under the `cloudfront-access-logs/` prefix in the log bucket. Nothing offline "
        "settles it.",
    },
    # B. Properties of a live resource, not of a declaration.
    {
        "spec_row": "API Gateway: authorizer on every route (the live behaviour half)",
        "reason": "The plan proves an authorizer id is attached to each route declaration; only "
        "a live unauthenticated call proves the actual 401 response.",
        "layer": "Layer 4 smoke/api_routes.py.",
    },
    {
        "spec_row": "Cognito: claims actually present in an issued token",
        "reason": "The plan proves the trigger is wired and the tier/version pairing is correct; "
        "only a decoded real token proves rep_id/book/full_visibility landed.",
        "layer": "Layer 4 smoke/cognito_claims.py.",
    },
    {
        "spec_row": "S3 frontend bucket: a direct-to-S3 request is actually refused",
        "reason": "Plan proves BPA + OAC + policy; refusal is behaviour, not a declaration.",
        "layer": "Layer 4 smoke/cloudfront.py.",
    },
    {
        "spec_row": "Gateway: tools/list matching the toolspec exactly (live inventory)",
        "reason": "The plan shows N gateway targets declared; it cannot show what the live "
        "Gateway actually returns from tools/list.",
        "layer": "Layer 4 smoke/gateway_tools.py.",
    },
    {
        "spec_row": "DynamoDB IAM: effective least-privilege (each role allowed exactly its own "
        "actions, denied every other function's)",
        "reason": "This is an IAM evaluation result, not policy text -- a role can over-grant via "
        "a resource wildcard, an extra attachment, or a missing permissions boundary "
        "while containing no forbidden string in the plan. The plan-expressible "
        "complementary properties (no dynamodb:*, specific table/index ARNs, one role "
        "per function) ARE asserted above; they must not be mistaken for this proof.",
        "layer": "Layer 4 smoke/iam_roles.py, the only place this claim becomes evidence.",
    },
    {
        "spec_row": "Transport: TLS 1.2+ everywhere",
        "reason": "The CloudFront viewer policy and the S3 aws:SecureTransport deny ARE plan "
        "properties and are asserted (cloudfront_oac's sibling checks / s3_tls_deny); "
        "the negotiated TLS version of every AWS API endpoint the Lambdas call is not.",
        "layer": "Scoped to the two plan-expressible declarations above; the row's wording should "
        "not be read as a live-TLS-negotiation proof.",
    },
    # C. Properties of code, not of infrastructure.
    {
        "spec_row": "_identity unconditionally stripped by the interceptor",
        "reason": "A code-level invariant of gateway/identity_interceptor.py, not a Terraform "
        "resource property.",
        "layer": "Layer 2, tests/unit/test_identity_interceptor.py against a genuinely signed token.",
    },
    {
        "spec_row": "Logs: no secrets, tokens, or full payloads (content redaction)",
        "reason": "Log-group retention and encryption ARE plan properties and are asserted "
        "(log_group_retention and log_group_kms here); log CONTENT is a property of "
        "call sites in application code, not of the log-group resource declaration.",
        "layer": "scripts/check_log_redaction.py.",
    },
    {
        "spec_row": "Secrets: no vendor API key anywhere, ever",
        "reason": "The plan can only prove no literal secret appears in the plan JSON itself, "
        "which is a much weaker claim than the row requires.",
        "layer": "gitleaks + scripts/check_no_vendor_keys.sh over the repo, plus a Layer 4 read of "
        "every function's live environment.",
    },
    # D. Properties of a document.
    {
        "spec_row": "KMS: key usage, rotation, access policy documented",
        "reason": "A documentation property, not a property of any Terraform resource.",
        "layer": "Manual review of the key policy and enable_key_rotation on every aws_kms_key "
        "under infra/, not this plan gate.",
    },
    # E. Behaviour only observable post-deploy.
    {
        "spec_row": "CloudFront passes the AgentCore SSE stream through incrementally, not buffered",
        "reason": "An open live-validation gate -- not a plan property under any circumstance.",
        "layer": "Post-deploy manual/automated SSE streaming check.",
    },
    {
        "spec_row": "The in-apply CodeBuild image build actually succeeding",
        "reason": "Triggered by null_resource.image_build's local-exec provisioner "
        "(infra/modules/agentcore/ecr_codebuild.tf) -- a provisioner's effects are not "
        "plan properties; the plan only shows the null_resource's trigger hash changing.",
        "layer": "Observed at apply time (the provisioner fails the apply on a non-SUCCEEDED "
        "build); no plan-time equivalent exists.",
    },
    # CloudFront viewer TLS minimum with the default certificate.
    {
        "spec_row": "Transport: CloudFront viewer TLS minimum of 1.2",
        "reason": "minimum_protocol_version is only honoured by CloudFront when a custom ACM "
        "certificate is also configured (viewer_certificate.acm_certificate_arn). This "
        "deployment uses the default *.cloudfront.net certificate "
        "(cloudfront_default_certificate = true, per infra/modules/frontend_cdn/ "
        "cloudfront.tf and its README), for which AWS fixes the effective minimum "
        "itself and minimum_protocol_version has no effect. Asserting a TLS-1.2 floor "
        "here would pass trivially regardless of the deployed configuration -- exactly "
        "the vacuity this gate exists to avoid -- so it is recorded honestly instead of "
        "asserted.",
        "layer": "Would become plan-expressible if a custom domain + ACM certificate is adopted "
        "(documented as an optional hardening path in the frontend_cdn README); not "
        "asserted while cloudfront_default_certificate = true.",
    },
    # Runtime inbound auth, asserted only from the deployed resource.
    {
        "spec_row": "Runtime inbound auth = IAM (asserted from the deployed resource, not from "
        "the Terraform source)",
        "reason": "The provider models IAM inbound auth as the ABSENCE of an authorizer block, not "
        "as a value: `aws_bedrockagentcore_agent_runtime` in hashicorp/aws 6.71.0 has no "
        "IAM enum and its only authorizer sub-block is `custom_jwt_authorizer` (verified "
        "against `terraform providers schema -json`). So source or plan "
        "inspection cannot distinguish `IAM` from a runtime whose authorizer block was "
        "dropped by mistake, or from a provider version that renamed it -- both look "
        "identical to correct configuration. An assertion on an absence is satisfied by "
        "every kind of absence, including the wrong ones, so it would be vacuous in "
        "precisely the direction that matters. Only the deployed resource reports the "
        "inbound auth mode it is actually enforcing. Note `check_gateway_custom_jwt` is "
        "unaffected and still asserted: it inspects `aws_bedrockagentcore_gateway`, which "
        "remains CUSTOM_JWT, and the two resources carry separate inbound-auth "
        "settings -- conflating them is a specific error.",
        "layer": "Layer 4 scripts/smoke/runtime_inbound_auth.py, reading inbound auth from the live "
        "Runtime. "
        "Paired there with the relay role's simulate-principal-policy check in "
        "scripts/smoke/iam_roles.py, which must show an ungranted action as implicitDeny.",
    },
]


REPO_ROOT = Path(__file__).resolve().parents[1]
SECURITY_BASELINE_PATH = REPO_ROOT / "docs" / "security-baseline.md"
SECURITY_BASELINE_HEADER = "| Control | Requirement | Verify | Class |"
INFRA_DIR = REPO_ROOT / "infra"
COMPLIANT_FIXTURE_NAME = "compliant"


def compliant_fixture() -> dict[str, Any]:
    """The compliant plan fixture, built by `scripts/plan_fixtures.py` from its tracked base.

    Raises `OSError`, `ValueError` (incl. `json.JSONDecodeError`) when the base is absent or malformed.
    """
    return plan_fixtures.build(COMPLIANT_FIXTURE_NAME)


#: Resource types declared in `infra/**/*.tf` and absent from the compliant plan fixture, each with
#: the reason. `check_fixture_currency` fails on an unlisted missing type and on a stale entry.
FIXTURE_ABSENT_TYPES: dict[str, str] = {}

#: Resource types the compliant fixture plans fewer of than `EXPECTED_RESOURCE_COUNTS` expects, as
#: `type -> (count in the fixture, reason)`. `check_fixture_currency` fails on a stale entry.
FIXTURE_LAGGING_COUNTS: dict[str, tuple[int, str]] = {}


def _module_call_graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """AST-parse this module: return `(by_type_literals, calls)` keyed by function name.

    `by_type_literals` holds the string literals passed to `.by_type(...)`; `calls` the names called.
    """
    tree = ast.parse(Path(__file__).resolve().read_text(encoding="utf-8"))
    literals: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        found: set[str] = set()
        called: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Attribute) and func.attr == "by_type":
                for arg in inner.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                called.add(func.attr)
        literals[node.name] = found
        calls[node.name] = called
    return literals, calls


def subject_resource_types() -> dict[str, tuple[str, ...]]:
    """Per control id, every resource type its assertion transitively reads via `plan.by_type`."""
    literals, calls = _module_call_graph()

    def closure(name: str, seen: set[str]) -> set[str]:
        if name in seen:
            return set()
        seen.add(name)
        found = set(literals.get(name, set()))
        for callee in calls.get(name, set()):
            if callee in literals:
                found |= closure(callee, seen)
        return found

    return {
        control.control_id: tuple(sorted(closure(control.fn.__name__, set())))
        for control in CONTROLS
    }


def check_control_subject_floors() -> list[Finding]:
    """Every control's subject resource type has an existence floor, or a recorded reason it cannot.

    A control deriving no subject type must be listed in `NO_SUBJECT_TYPE_CONTROLS`; a stale entry
    there fails.
    """
    findings: list[Finding] = []
    derived = subject_resource_types()
    for control_id, types in sorted(derived.items()):
        if not types:
            reason = NO_SUBJECT_TYPE_CONTROLS.get(control_id, "")
            findings.append(
                Finding(
                    "control_subject_floors",
                    f"{control_id} -> (no subject resource type)",
                    "pass" if reason else "fail",
                    "no `plan.by_type` literal derives from this control; it must be enumerated in "
                    f"NO_SUBJECT_TYPE_CONTROLS with a reason. recorded_reason={bool(reason)}",
                )
            )
            continue
        for resource_type in types:
            floored = resource_type in EXPECTED_RESOURCE_COUNTS
            absence = resource_type in ABSENCE_ASSERTED_TYPES
            findings.append(
                Finding(
                    "control_subject_floors",
                    f"{control_id} -> {resource_type}",
                    "pass" if (floored or absence) else "fail",
                    f"expected_count={EXPECTED_RESOURCE_COUNTS.get(resource_type) is not None} "
                    f"absence_asserted={absence}",
                )
            )
    for control_id in sorted(NO_SUBJECT_TYPE_CONTROLS):
        stale = control_id not in derived or bool(derived.get(control_id))
        findings.append(
            Finding(
                "control_subject_floors",
                f"(no-subject-type) {control_id}",
                "fail" if stale else "pass",
                "NO_SUBJECT_TYPE_CONTROLS entry is stale: this control now derives subject "
                "resource types, or is no longer registered in CONTROLS -- remove the entry so its "
                "types are floored like every other control's"
                if stale
                else "still derives no subject resource type, reason recorded",
            )
        )
    return findings


#: Separate Terraform roots under `infra/` whose resources are never in the stack plan.
SEPARATE_ROOT_DIRS: tuple[str, ...] = ("bootstrap_account",)


def declared_infra_resource_types() -> set[str]:
    """Every `resource "<type>"` declared in the stack root's `infra/**/*.tf`.

    `SEPARATE_ROOT_DIRS` are excluded: the compliant fixture is a plan of the stack root only.
    """
    pattern = re.compile(r'^\s*resource\s+"([a-z0-9_]+)"', re.MULTILINE)
    types: set[str] = set()
    for path in sorted(INFRA_DIR.rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        if path.relative_to(INFRA_DIR).parts[0] in SEPARATE_ROOT_DIRS:
            continue
        types.update(pattern.findall(path.read_text(encoding="utf-8")))
    return types


#: Provenance fields echoed into the `fixture_currency` finding; other fields are named, not printed.
_ASSERTED_PROVENANCE_FIELDS = (
    "captured_infra_digest",
    "captured_at",
    "how",
    "variables_at_capture",
)


def _provenance_basis(provenance: object) -> str:
    """The fixture's provenance rendered from the fields this gate asserts, not echoed wholesale."""
    if not isinstance(provenance, dict):
        return f"_fixture_provenance={provenance!r}"
    asserted = {
        field: provenance[field] for field in _ASSERTED_PROVENANCE_FIELDS if field in provenance
    }
    unasserted = sorted(set(provenance) - set(_ASSERTED_PROVENANCE_FIELDS))
    suffix = (
        f"; descriptive-only field(s) not asserted and not shown: {', '.join(unasserted)}"
        if unasserted
        else ""
    )
    return f"_fixture_provenance={asserted!r}{suffix}"


def check_fixture_currency() -> list[Finding]:
    """Every resource type declared in `infra/` is either in the compliant fixture or declared absent.

    A missing type absent from `FIXTURE_ABSENT_TYPES` fails, as does a stale entry there.
    """
    findings: list[Finding] = []
    try:
        fixture = compliant_fixture()
    except (OSError, ValueError) as exc:
        return [
            Finding(
                "fixture_currency",
                plan_fixtures.BASE_PATH.name,
                "fail",
                f"compliant plan fixture could not be built: {exc}",
            )
        ]
    fixture_types = {
        r.get("type") for r in fixture.get("resource_changes", []) if isinstance(r, dict)
    }
    declared = declared_infra_resource_types()
    missing = sorted(declared - fixture_types)
    for resource_type in missing:
        reason = FIXTURE_ABSENT_TYPES.get(resource_type, "")
        findings.append(
            Finding(
                "fixture_currency",
                resource_type,
                "pass" if reason else "fail",
                f"declared in infra/ but absent from the fixture; recorded_reason={bool(reason)}",
            )
        )
    for resource_type in sorted(FIXTURE_ABSENT_TYPES):
        stale = resource_type in fixture_types or resource_type not in declared
        findings.append(
            Finding(
                "fixture_currency",
                f"(declared-absent) {resource_type}",
                "fail" if stale else "pass",
                "FIXTURE_ABSENT_TYPES entry is stale: the type is now in the fixture, or no "
                "longer declared in infra/ -- remove the entry"
                if stale
                else "still absent from the fixture, reason recorded",
            )
        )
    for resource_type, (lagging, _reason) in sorted(FIXTURE_LAGGING_COUNTS.items()):
        planned = sum(
            1
            for r in fixture.get("resource_changes", [])
            if isinstance(r, dict) and r.get("type") == resource_type
        )
        expected = EXPECTED_RESOURCE_COUNTS.get(resource_type)
        stale = planned != lagging or expected is None or expected.fixed == lagging
        findings.append(
            Finding(
                "fixture_currency",
                f"(declared-lagging) {resource_type}",
                "fail" if stale else "pass",
                f"FIXTURE_LAGGING_COUNTS entry is stale: the fixture plans {planned}, the entry "
                f"declares {lagging} -- remove or correct the entry"
                if stale
                else f"the fixture still plans {lagging}, reason recorded",
            )
        )
    findings.append(
        Finding(
            "fixture_currency",
            "provenance",
            "pass" if fixture.get("_fixture_provenance") else "fail",
            _provenance_basis(fixture.get("_fixture_provenance")),
        )
    )
    return findings


def derive_spec_10_control_names() -> list[tuple[str, str]]:
    """Parse every row of the `docs/security-baseline.md` table as `(Control, Requirement)`.

    Several rows share one `Control` name, so every row is returned rather than one per name.
    """
    lines = SECURITY_BASELINE_PATH.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(SECURITY_BASELINE_HEADER)
    except ValueError:
        raise ValueError(
            f"{SECURITY_BASELINE_PATH}: could not locate the {SECURITY_BASELINE_HEADER!r} table"
        ) from None
    rows: list[tuple[str, str]] = []
    for line in lines[start + 1 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or set(cells[0]) <= {"-", " "}:
            continue
        control = re.sub(r"`", "", cells[0]).strip()
        requirement = re.sub(r"`|\*\*", "", cells[1]).strip()
        if not control:
            continue
        rows.append((control, requirement))
    return rows


def run_self_check() -> list[Finding]:
    """Fail if any security-baseline row lacks both a registered assertion and a recorded reason."""
    # Rows are keyed `"{control} | {requirement}"`, since several share one `Control` name.
    spec_10_rows = [
        f"{control} | {requirement}" for control, requirement in derive_spec_10_control_names()
    ]

    def row_matching(control_substr: str, requirement_substr: str) -> str:
        """Return the one `spec_10_rows` entry whose Control/Requirement contain both substrings.

        Raises if zero or more than one row matches.
        """
        matches = [
            row
            for row in spec_10_rows
            if control_substr in row.split(" | ", 1)[0]
            and requirement_substr in row.split(" | ", 1)[1]
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected exactly one security-baseline row matching control={control_substr!r} "
                f"requirement contains {requirement_substr!r}, found {len(matches)}: {matches!r}"
            )
        return matches[0]

    # Row -> the ControlSpec.spec_row asserting it; other rows must resolve via
    # `inexpressible_row_map`.
    control_row_map = {
        row_matching("DynamoDB encryption at rest", ""): "DynamoDB encryption at rest",
        row_matching("DynamoDB PITR", ""): "DynamoDB PITR",
        row_matching("DynamoDB IAM", "no dynamodb:*"): "DynamoDB IAM: no dynamodb:*",
        row_matching(
            "DynamoDB IAM", "table and CMK policies"
        ): "DynamoDB IAM: table and CMK policies deny every principal outside the project roles and "
        "operators",
        row_matching("S3 frontend bucket", ""): "S3 frontend bucket: BPA",
        row_matching("S3 encryption", "KMS CMK"): "S3 encryption: KMS CMK",
        row_matching("S3 encryption", "bucket policies deny"): STORE_PRINCIPAL_DENY_SPEC_ROW,
        row_matching("KMS", "CMK policies deny"): STORE_PRINCIPAL_DENY_SPEC_ROW,
        row_matching("API Gateway", "authorizer"): "API Gateway: authorizer on every route",
        row_matching("API Gateway", "throttle"): "API Gateway: throttle + burst on every stage",
        row_matching("Lambda", "one execution role"): "Lambda: one execution role per function",
        row_matching(
            "Lambda", "reserved concurrency"
        ): "Lambda: reserved concurrency set on every function",
        row_matching("Lambda", "KMS-encrypted"): "Lambda: KMS-encrypted env vars where sensitive",
        row_matching("Cognito", "MFA"): "Cognito: MFA",
        row_matching(
            "Gateway", "CUSTOM_JWT"
        ): "Gateway: CUSTOM_JWT with discovery URL/clients/audiences/scopes",
        row_matching(
            "Gateway", "REQUEST interceptor"
        ): "Gateway: REQUEST interceptor with passRequestHeaders: true",
        row_matching(
            "Action Lambdas", ""
        ): "Action Lambdas: resource policy admits only Gateway target role and API Gateway",
        row_matching("Network", ""): "Network: no internet-facing SG ingress anywhere",
        row_matching("Bedrock", ""): "Bedrock: assumed role, no static access key (auth half)",
        row_matching("Transport", ""): "Transport: CloudFront behaviours",
        row_matching("Tags", ""): "Tags: mandatory tags on all resources",
        row_matching("CloudTrail", ""): "CloudTrail: enabled for API activity",
        row_matching("Logs", "retention"): "Logs: explicit retention",
        row_matching("Logs", "KMS"): "Logs: KMS CMK on every log group",
    }
    cognito_mfa_row = row_matching("Cognito", "MFA")
    dynamodb_iam_row = row_matching("DynamoDB IAM", "no dynamodb:*")
    live_control_spec_rows = {c.spec_row for c in CONTROLS}
    # Rows asserted by several controls: each listed control must be registered for the row to pass.
    cognito_additional_controls = {
        "Cognito: feature plan Essentials/Plus paired with V2_0 trigger",
        "Cognito app clients: write_attributes excludes scope-bearing attrs",
        "Cognito: admin-only sign-up, no public SignUp",
    }
    dynamodb_iam_additional_controls = {
        "DynamoDB IAM: GSIs match schema.py",
        "DynamoDB IAM: LeadingKeys against table_action_role_matrix.json",
    }
    bedrock_row = row_matching("Bedrock", "")
    bedrock_additional_controls = {
        "Bedrock: InvokeModel* scoped to named inference profiles/models, never `*` "
        "(resource half)",
    }
    # Row -> the PLAN_TIME_INEXPRESSIBLE spec_row covering it.
    inexpressible_row_map = {
        row_matching("Secrets", ""): "Secrets: no vendor API key anywhere, ever",
        row_matching("KMS", "rotation"): "KMS: key usage, rotation, access policy documented",
    }
    recorded_reasons = {e["spec_row"] for e in PLAN_TIME_INEXPRESSIBLE}
    # Entries required regardless of the baseline table's rows.
    standing_inexpressible_reasons = {
        "Terraform state bucket/CMK/access-log bucket/lock table "
        "(versioning, BPA, CMK, TLS-deny, access-logging, SSE+PITR on the lock table)",
    }
    standing_live_controls = {
        "API Gateway: request validators",
        "Source: no *.tf.json under infra/, so every source-reading control sees all of it",
    }

    findings = []
    for row in spec_10_rows:
        mapped_control_row = control_row_map.get(row)
        has_control = (
            mapped_control_row is not None and mapped_control_row in live_control_spec_rows
        )
        mapped_reason = inexpressible_row_map.get(row)
        has_reason = mapped_reason is not None and mapped_reason in recorded_reasons
        ok = has_control or has_reason
        detail = f"has_control={has_control} has_recorded_inexpressible_reason={has_reason}"
        if row == cognito_mfa_row:
            missing_additional = cognito_additional_controls - live_control_spec_rows
            ok = ok and not missing_additional
            detail += f" missing_sa_review_controls={sorted(missing_additional)!r}"
        if row == dynamodb_iam_row:
            missing_additional = dynamodb_iam_additional_controls - live_control_spec_rows
            ok = ok and not missing_additional
            detail += f" missing_leading_keys_gsi_controls={sorted(missing_additional)!r}"
        if row == bedrock_row:
            missing_additional = bedrock_additional_controls - live_control_spec_rows
            ok = ok and not missing_additional
            detail += f" missing_resource_scoping_control={sorted(missing_additional)!r}"
        findings.append(
            Finding(
                "self_check",
                row,
                "pass" if ok else "fail",
                detail,
            )
        )
    for reason in standing_inexpressible_reasons:
        ok = reason in recorded_reasons
        findings.append(
            Finding(
                "self_check",
                f"(standing) {reason[:60]}",
                "pass" if ok else "fail",
                f"has_recorded_inexpressible_reason={ok}",
            )
        )
    for control_row in standing_live_controls:
        ok = control_row in live_control_spec_rows
        findings.append(
            Finding(
                "self_check",
                f"(standing live control) {control_row[:60]}",
                "pass" if ok else "fail",
                f"has_registered_control={ok}",
            )
        )
    findings.extend(check_control_subject_floors())
    findings.extend(check_fixture_currency())
    return findings


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_gate(plan: Plan, *, only: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for control in CONTROLS:
        if only is not None and control.module != only:
            continue
        findings.extend(control.fn(plan))
    return findings


def render_text(findings: list[Finding]) -> str:
    lines = []
    failed = [f for f in findings if f.status == "fail"]
    deferred = [f for f in findings if f.status == "deferred"]
    markers = {"pass": "[PASS]", "fail": "[FAIL]", "deferred": GATE_DEFER_MARKER}
    for f in findings:
        marker = markers.get(f.status, "[FAIL]")
        lines.append(f"{marker} {f.control}: {f.resource} -- {f.detail}")
    lines.append("")
    lines.append(
        f"{len(findings)} checks, {len(failed)} failed, {len(deferred)} deferred "
        "(deferred = NOT asserted at plan time; see the per-line DEFER detail for why)"
    )
    return "\n".join(lines)


def deferred_counts_by_control(findings: list[Finding]) -> dict[str, int]:
    """Per-control count of `deferred` findings."""
    counts: dict[str, int] = {}
    for f in findings:
        if f.status == "deferred":
            counts[f.control] = counts.get(f.control, 0) + 1
    return dict(sorted(counts.items()))


def render_json(findings: list[Finding]) -> str:
    return json.dumps(
        {
            "checks": [
                {
                    "control": f.control,
                    "resource": f.resource,
                    "status": f.status,
                    "detail": f.detail,
                }
                for f in findings
            ],
            "total": len(findings),
            "failed": sum(1 for f in findings if f.status == "fail"),
            "deferred": sum(1 for f in findings if f.status == "deferred"),
            "deferred_by_control": deferred_counts_by_control(findings),
        },
        indent=2,
    )


#: The `_fixture_provenance` key holding the capture's `plan_fixtures.infra_digest`.
INFRA_DIGEST_KEY = "captured_infra_digest"


def recorded_infra_digest(document: object) -> str | None:
    """The plan's `_fixture_provenance.captured_infra_digest`, or `None` when absent or blank."""
    provenance = document.get("_fixture_provenance") if isinstance(document, dict) else None
    digest = provenance.get(INFRA_DIGEST_KEY) if isinstance(provenance, dict) else None
    return digest.strip() if isinstance(digest, str) and digest.strip() else None


def plan_staleness_finding(plan_path: Path) -> Finding | None:
    """A `deferred` finding when `plan_path` describes Terraform the tree no longer has, else a `pass`.

    Compares `_fixture_provenance.captured_infra_digest` with `plan_fixtures.infra_digest` of the
    working tree; no git history is read. Any failure to answer (unreadable plan, no recorded
    digest, no digest computable) is `deferred` with the reason. Returns `None` when `plan_path`
    does not exist.
    """
    if not plan_path.exists():
        return None

    def deferred(detail: str) -> Finding:
        return Finding("plan_freshness", plan_path.name, "deferred", detail + " " + _RECAPTURE_HINT)

    try:
        document = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return deferred(f"the plan could not be read to find its provenance ({exc!r}).")

    recorded = recorded_infra_digest(document)
    if recorded is None:
        return deferred(
            f"the plan records no `_fixture_provenance.{INFRA_DIGEST_KEY}`, so which Terraform it "
            "describes cannot be established at all."
        )

    try:
        current = plan_fixtures.infra_digest(REPO_ROOT)
    except ValueError as exc:
        return deferred(
            f"the working tree's infra digest could not be computed ({exc}), so whether this plan "
            "matches the current infra/ cannot be established."
        )

    if recorded == current:
        return Finding(
            "plan_freshness",
            plan_path.name,
            "pass",
            f"{INFRA_DIGEST_KEY} {recorded} equals the working tree's tracked infra/**/*.tf "
            "digest, so every plan-derived control above validated the CURRENT infra/.",
        )

    return deferred(
        f"{INFRA_DIGEST_KEY} {recorded} differs from the working tree's tracked infra/**/*.tf "
        f"digest {current} — so every plan-derived control above validated that SNAPSHOT and not "
        "the current infra/."
    )


#: The remediation appended to every deferred `plan_freshness` finding.
_RECAPTURE_HINT = (
    "Regenerating requires credentials (a root plan configures the S3 backend and calls "
    "sts:GetCallerIdentity), so this cannot be resolved offline: re-capture the fixture with "
    "`terraform show -json`, scrub the account id, and set _fixture_provenance.captured_infra_digest "
    "to `plan_fixtures.infra_digest` of the tree it was captured from. Source-derived controls (teardown_force_destroy, the relay IAM "
    "controls, the security-group controls) read infra/ directly and ARE current; only the "
    "plan-derived ones are affected."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, help="Path to a `terraform plan -json` document.")
    parser.add_argument(
        "--only",
        choices=sorted({c.module for c in CONTROLS}),
        default=None,
        help="Run only the controls tagged with this module (debugging aid).",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Check that every docs/security-baseline.md control has an assertion or a recorded "
        "plan-time-inexpressible reason. Does not require --plan.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of text."
    )
    args = parser.parse_args(argv)

    if args.self_check:
        findings = run_self_check()
    else:
        if args.plan is None:
            parser.error("--plan is required unless --self-check is given")
        plan = load_plan(args.plan)
        findings = run_gate(plan, only=args.only)
        # A full run also includes the self-check and the plan-freshness finding.
        if args.only is None:
            findings = findings + run_self_check()
            staleness = plan_staleness_finding(args.plan)
            if staleness is not None:
                findings = findings + [staleness]

    if args.json:
        print(render_json(findings))
    else:
        print(render_text(findings))

    # Deferred findings exit 0; `run_gates.py` reads them from the GATE_DEFER_MARKER lines.
    return 1 if any(f.status == "fail" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
