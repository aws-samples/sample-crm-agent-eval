# SPDX-License-Identifier: MIT
"""Residue-scan core: the resource model, read-only AWS call helpers, accept rules and the scan.

Family modules (``regional_data``, ``compute_network``, ``global_services``) each export
``SCANNERS: dict[FamilyName, FamilyScanner]`` and issue every AWS call through :func:`call` or
:func:`paginate` with the operation as a string literal. :func:`run_scan` executes a registry of
scanners plus an optional tag-index scanner and returns a :class:`ScanResult`; :func:`exit_code`
maps it to the CLI exit status (0 clean, 1 residue, 2 unmeasured, 3 nothing executed).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import (
    ClientError,
    HTTPClientError,
    NoCredentialsError,
)
from botocore.exceptions import (
    ConnectionError as BotocoreConnectionError,
)

FamilyName = str

TAG_INDEX_FAMILY: FamilyName = "tag-index"
"""Key under which a failed tag-index scan is recorded in ``ScanResult.unmeasured``."""


@dataclass(frozen=True)
class Resource:
    """One project-owned resource found by a scanner.

    ``region`` is a region name or ``"global"``; ``detail`` carries scanner-specific facts such
    as ``{"key_state": "PendingDeletion"}``.
    """

    family: FamilyName
    kind: str
    name: str
    arn: str | None
    region: str
    detail: Mapping[str, str] = field(default_factory=dict)


class Unmeasurable(Exception):  # noqa: N818
    """A list/describe call could not complete; the family is unmeasured, never empty."""

    def __init__(self, family: FamilyName, operation: str, code: str, message: str) -> None:
        super().__init__(f"{family}: {operation} failed: {code}: {message}")
        self.family = family
        self.operation = operation
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ScanConfig:
    """Scan region and the ownership rules (name prefixes, project tag)."""

    region: str
    name_prefixes: tuple[str, ...] = ("crm-agent-eval", "crm_agent_eval")
    tag_key: str = "service"
    tag_value: str = "crm-agent-eval"


ClientFactory = Callable[[str, str], Any]
FamilyScanner = Callable[[ClientFactory, ScanConfig], list[Resource]]
TagScanner = Callable[[ClientFactory, ScanConfig], list[Resource]]

READ_ONLY_OPERATION = re.compile(r"^(list|describe|get)_[a-z0-9_]+$")


def _require_read_only(operation: str) -> None:
    if not READ_ONLY_OPERATION.fullmatch(operation):
        raise ValueError(f"refusing non-read-only operation {operation!r}")


def _unmeasurable(family: FamilyName, operation: str, exc: Exception) -> Unmeasurable:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code") or "ClientError")
        message = str(error.get("Message") or exc)
        return Unmeasurable(family, operation, code, message)
    return Unmeasurable(family, operation, type(exc).__name__, str(exc))


_TRANSLATED_ERRORS = (
    ClientError,
    NoCredentialsError,
    BotocoreConnectionError,
    HTTPClientError,
)


def call(client: Any, family: FamilyName, operation: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke one read-only client operation and return its response.

    Raises ``ValueError`` if ``operation`` does not match :data:`READ_ONLY_OPERATION`, and
    :class:`Unmeasurable` (carrying the AWS error code, or the botocore exception class name)
    on a client error, missing credentials, a connection/endpoint failure or a read timeout.
    Throttling that survives the client's retries arrives as a ``ClientError`` and keeps its code.
    """
    _require_read_only(operation)
    try:
        response: dict[str, Any] = getattr(client, operation)(**kwargs)
    except _TRANSLATED_ERRORS as exc:
        raise _unmeasurable(family, operation, exc) from exc
    return response


def _extract(page: Mapping[str, Any], result_key: str) -> list[Any]:
    node: Any = page
    for part in result_key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return []
        node = node[part]
    return list(node) if isinstance(node, list) else []


def paginate(
    client: Any, family: FamilyName, operation: str, result_key: str, **kwargs: Any
) -> list[Any]:
    """Run a read-only paginated operation and return the concatenated ``result_key`` items.

    ``result_key`` may be dotted (``"DistributionList.Items"``); a page missing it contributes
    nothing. Items are dicts, or strings for name-list results (``TableNames``). Refusal and
    error translation are as for :func:`call`.
    """
    _require_read_only(operation)
    items: list[Any] = []
    try:
        paginator = client.get_paginator(operation)
        for page in paginator.paginate(**kwargs):
            items.extend(_extract(page, result_key))
    except _TRANSLATED_ERRORS as exc:
        raise _unmeasurable(family, operation, exc) from exc
    return items


def matches_prefix(name: str, config: ScanConfig) -> bool:
    """True if ``name`` starts with any configured name prefix (no separator boundary)."""
    return any(name.startswith(prefix) for prefix in config.name_prefixes)


def has_project_tag(tags: Mapping[str, str], config: ScanConfig) -> bool:
    """True if ``tags`` carries the configured project tag key with the configured value."""
    return tags.get(config.tag_key) == config.tag_value


def arn_service(arn: str) -> str:
    """The service segment of an ARN (``arn:aws:<service>:...``), or ``""`` if malformed."""
    parts = arn.split(":", 5)
    return parts[2] if len(parts) == 6 and parts[0] == "arn" else ""


def arn_resource_type(arn: str) -> str:
    """The resource-type part of an ARN's resource (``vpc`` in ``...:vpc/vpc-1``).

    The type ends at the first ``/`` or ``:``, after one leading ``/`` is dropped, so
    ``log-group:/aws/lambda/x`` gives ``log-group`` and API Gateway's ``/restapis/abc`` gives
    ``restapis``. Returns ``""`` for a malformed ARN and for type-less resources such as S3
    buckets (``arn:aws:s3:::name``), so a family enumerating those lists ``""`` as its type.
    """
    parts = arn.split(":", 5)
    if len(parts) != 6:
        return ""
    head = re.split(r"[/:]", parts[5].removeprefix("/"), maxsplit=1)
    return head[0] if len(head) == 2 else ""


@dataclass(frozen=True)
class AcceptRule:
    """A named allowlist entry: a resource of ``family``/``kind`` satisfying ``predicate``."""

    family: FamilyName
    kind: str
    reason: str
    predicate: Callable[[Resource], bool]


VPC_ORIGINS_SERVICE_SG = "CloudFront-VPCOrigins-Service-SG"
ADOPTED_NETWORK_KINDS = frozenset(
    {"vpc", "subnet", "route-table", "internet-gateway", "network-acl"}
)

ACCEPTED_RESIDUE: tuple[AcceptRule, ...] = (
    AcceptRule(
        family="kms",
        kind="key",
        reason=(
            "kms-pending-deletion: scheduled for deletion; the waiting period is AWS-enforced, "
            "minimum 7 days"
        ),
        predicate=lambda r: r.detail.get("key_state") == "PendingDeletion",
    ),
    AcceptRule(
        family="ec2",
        kind="security-group",
        reason=(
            "vpc-origins-service-sg: AWS service-managed group for CloudFront VPC origins; "
            "a VPC origin still using it is reported by the cloudfront family"
        ),
        predicate=lambda r: r.name == VPC_ORIGINS_SERVICE_SG,
    ),
    AcceptRule(
        family="ec2",
        kind="tagged",
        reason="adopted-network: adopted VPC network resource, out of scope",
        predicate=lambda r: r.arn is not None and arn_resource_type(r.arn) in ADOPTED_NETWORK_KINDS,
    ),
)


def classify(resource: Resource) -> str | None:
    """The reason of the first :data:`ACCEPTED_RESIDUE` rule accepting ``resource``, else None."""
    for rule in ACCEPTED_RESIDUE:
        if (rule.family, rule.kind) == (resource.family, resource.kind) and rule.predicate(
            resource
        ):
            return rule.reason
    return None


@dataclass
class ScanResult:
    """Outcome of :func:`run_scan`; ``accepted`` pairs each resource with its rule's reason."""

    measured: list[FamilyName]
    unmeasured: dict[FamilyName, Unmeasurable]
    residue: list[Resource]
    accepted: list[tuple[Resource, str]]
    stale_tag_index: list[Resource]


def _seen_by_scanner(hit_arn: str, found: list[Resource]) -> bool:
    for resource in found:
        if resource.arn == hit_arn:
            return True
        if resource.name and (
            hit_arn.endswith("/" + resource.name) or hit_arn.endswith(":" + resource.name)
        ):
            return True
    return False


def run_scan(
    factory: ClientFactory,
    config: ScanConfig,
    scanners: Mapping[FamilyName, FamilyScanner],
    tag_scanner: TagScanner | None,
    arn_service_to_family: Mapping[str, FamilyName],
    *,
    enumerated_arn_types: Mapping[FamilyName, frozenset[str]],
) -> ScanResult:
    """Run every scanner, then merge the tag index.

    A scanner raising :class:`Unmeasurable` marks its family unmeasured. Each tag hit is mapped
    to a family via its ARN service and classified in this order: a hit already returned by that
    family's scanner is dropped; a hit matching an accept rule goes to ``accepted``; a hit whose
    family was measured and whose :func:`arn_resource_type` is in
    ``enumerated_arn_types[family]`` goes to ``stale_tag_index``; any other hit is residue of kind
    ``tagged-unscanned``. A failed tag scan is recorded under :data:`TAG_INDEX_FAMILY`. With no
    scanners nothing is executed. Exceptions other than ``Unmeasurable`` propagate.
    """
    result = ScanResult(measured=[], unmeasured={}, residue=[], accepted=[], stale_tag_index=[])
    found: dict[FamilyName, list[Resource]] = {}
    for family, scanner in scanners.items():
        try:
            resources = scanner(factory, config)
        except Unmeasurable as exc:
            result.unmeasured[family] = exc
            continue
        result.measured.append(family)
        found[family] = resources
        for resource in resources:
            reason = classify(resource)
            if reason is None:
                result.residue.append(resource)
            else:
                result.accepted.append((resource, reason))

    if not scanners or tag_scanner is None:
        return result
    try:
        hits = tag_scanner(factory, config)
    except Unmeasurable as exc:
        result.unmeasured[TAG_INDEX_FAMILY] = exc
        return result

    for hit in hits:
        arn = hit.arn or ""
        service = arn_service(arn)
        family = arn_service_to_family.get(service, service or hit.family)
        if family in found and _seen_by_scanner(arn, found[family]):
            continue
        tagged = Resource(
            family=family,
            kind="tagged",
            name=hit.name,
            arn=hit.arn,
            region=hit.region,
            detail=hit.detail,
        )
        reason = classify(tagged)
        if reason is not None:
            result.accepted.append((tagged, reason))
        elif family in found and arn_resource_type(arn) in enumerated_arn_types.get(
            family, frozenset()
        ):
            result.stale_tag_index.append(tagged)
        else:
            result.residue.append(
                Resource(
                    family=family,
                    kind="tagged-unscanned",
                    name=hit.name,
                    arn=hit.arn,
                    region=hit.region,
                    detail=hit.detail,
                )
            )
    return result


def exit_code(result: ScanResult) -> int:
    """3 if nothing executed, else 2 if anything unmeasured, else 1 if residue, else 0."""
    if not result.measured and not result.unmeasured:
        return 3
    if result.unmeasured:
        return 2
    if result.residue:
        return 1
    return 0
