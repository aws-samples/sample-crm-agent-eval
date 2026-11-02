#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read-only scan of one AWS account/region for crm-agent-eval residue.

Usage:
    .venv/bin/python3 scripts/scan_residue.py --profile NAME --region REGION
        [--name-prefix P]... [--tag KEY=VALUE] [--families f1,f2] [--json PATH]

Runs every family scanner in `scripts/residue/` (or the `--families` subset) plus the tag-index
scan, prints one `RESIDUE|ACCEPTED|STALE family kind name [reason]` line per item and a
measured/unmeasured summary, and optionally writes a JSON report (it contains the account id and
ARNs; write it only under a gitignored or untracked path). Stdout never carries the account id.

Exit codes: 0 every selected family measured and no residue; 1 residue found; 2 a family
unmeasured, a usage error, static env credentials, or a failed identity lookup; 3 empty selection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import BotoCoreError  # noqa: E402
from scripts.residue import compute_network, global_services, regional_data  # noqa: E402
from scripts.residue.core import (  # noqa: E402
    ClientFactory,
    FamilyName,
    FamilyScanner,
    Resource,
    ScanConfig,
    ScanResult,
    TagScanner,
    Unmeasurable,
    arn_service,
    call,
    exit_code,
    run_scan,
)

SCHEMA_VERSION: Final = 1
USAGE_EXIT: Final = 2
STATIC_CREDENTIAL_VARS: Final[tuple[str, ...]] = ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN")
RETRY_CONFIG: Final = Config(retries={"mode": "standard", "max_attempts": 5})
ACCOUNT_PLACEHOLDER: Final = "<account>"


def merge_registries(*registries: Mapping[FamilyName, FamilyScanner]) -> dict[str, FamilyScanner]:
    """Union of family registries, in argument order; a family key seen twice is a ValueError."""
    merged: dict[FamilyName, FamilyScanner] = {}
    for registry in registries:
        for family, scanner in registry.items():
            if family in merged:
                raise ValueError(f"duplicate residue family {family!r}")
            merged[family] = scanner
    return merged


def build_registry() -> dict[FamilyName, FamilyScanner]:
    """The full scanner registry: regional data, compute/network, then global services."""
    return merge_registries(
        regional_data.SCANNERS, compute_network.SCANNERS, global_services.SCANNERS
    )


def merge_arn_types(
    *tables: Mapping[FamilyName, frozenset[str]],
) -> dict[FamilyName, frozenset[str]]:
    """Union of `ENUMERATED_ARN_TYPES` tables; a family key seen twice is a ValueError."""
    merged: dict[FamilyName, frozenset[str]] = {}
    for table in tables:
        for family, types in table.items():
            if family in merged:
                raise ValueError(f"duplicate ENUMERATED_ARN_TYPES family {family!r}")
            merged[family] = types
    return merged


def build_enumerated_arn_types() -> dict[FamilyName, frozenset[str]]:
    """The ARN types each registered family's scanner enumerates, from all three modules."""
    return merge_arn_types(
        regional_data.ENUMERATED_ARN_TYPES,
        compute_network.ENUMERATED_ARN_TYPES,
        global_services.ENUMERATED_ARN_TYPES,
    )


def arn_type(arn: str) -> str:
    """The principal type of an STS caller ARN: user, assumed-role, federated-user, root, other."""
    resource = arn.split(":", 5)[5] if arn.count(":") >= 5 else ""
    if resource == "root":
        return "root"
    kind = resource.split("/", 1)[0]
    return kind if kind in {"user", "assumed-role", "federated-user"} else "other"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only crm-agent-eval residue scan.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--name-prefix", action="append", dest="name_prefixes", metavar="P")
    parser.add_argument("--tag", metavar="KEY=VALUE")
    parser.add_argument("--families", metavar="f1,f2")
    parser.add_argument("--json", type=Path, dest="json_path", metavar="PATH")
    return parser


def _scan_config(args: argparse.Namespace) -> ScanConfig:
    kwargs: dict[str, Any] = {"region": args.region}
    if args.name_prefixes:
        kwargs["name_prefixes"] = tuple(args.name_prefixes)
    if args.tag is not None:
        key, sep, value = args.tag.partition("=")
        if not sep or not key or not value:
            raise ValueError(f"--tag must be KEY=VALUE, got {args.tag!r}")
        kwargs["tag_key"], kwargs["tag_value"] = key, value
    return ScanConfig(**kwargs)


def _select(
    registry: Mapping[FamilyName, FamilyScanner], families: str | None
) -> dict[FamilyName, FamilyScanner]:
    if families is None:
        return dict(registry)
    wanted = [name.strip() for name in families.split(",") if name.strip()]
    unknown = sorted(set(wanted) - set(registry))
    if unknown:
        raise ValueError(f"unknown families: {', '.join(unknown)}")
    return {family: scanner for family, scanner in registry.items() if family in wanted}


def _selected_tag_scanner(
    registry: Mapping[FamilyName, FamilyScanner], selected: Mapping[FamilyName, FamilyScanner]
) -> TagScanner:
    """The tag-index scan, minus hits belonging to a registered family left out of `selected`."""
    excluded = set(registry) - set(selected)
    mapping = global_services.ARN_SERVICE_TO_FAMILY

    def scan(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
        hits = global_services.tag_index_scan(factory, config)
        return [
            hit
            for hit in hits
            if mapping.get(arn_service(hit.arn or ""), hit.family) not in excluded
        ]

    return scan


def _session_factory(profile: str, region: str) -> ClientFactory:
    session = boto3.Session(profile_name=profile, region_name=region)

    def factory(service_name: str, region_name: str) -> Any:
        return session.client(service_name, region_name=region_name, config=RETRY_CONFIG)

    return factory


def _resolve_caller(factory: ClientFactory, region: str) -> dict[str, str]:
    sts = factory("sts", region)
    identity = call(sts, "sts", "get_caller_identity")
    return {"Account": str(identity["Account"]), "Arn": str(identity["Arn"])}


def _resource_json(resource: Resource) -> dict[str, Any]:
    return {
        "family": resource.family,
        "kind": resource.kind,
        "name": resource.name,
        "arn": resource.arn,
        "region": resource.region,
        "detail": dict(resource.detail),
    }


def build_report(
    result: ScanResult, config: ScanConfig, caller: Mapping[str, str], code: int
) -> dict[str, Any]:
    """The `--json` report document for one scan."""
    return {
        "schema_version": SCHEMA_VERSION,
        "region": config.region,
        "caller": {"account": caller["Account"], "arn_type": arn_type(caller["Arn"])},
        "families": {
            "measured": list(result.measured),
            "unmeasured": {
                family: {"operation": exc.operation, "code": exc.code, "message": exc.message}
                for family, exc in result.unmeasured.items()
            },
        },
        "residue": [_resource_json(r) for r in result.residue],
        "accepted": [{**_resource_json(r), "reason": reason} for r, reason in result.accepted],
        "stale_tag_index": [_resource_json(r) for r in result.stale_tag_index],
        "exit_code": code,
    }


def render_lines(result: ScanResult, account: str) -> list[str]:
    """Stdout lines for one scan, with every occurrence of `account` replaced by a placeholder."""
    lines = [f"RESIDUE {r.family} {r.kind} {r.name}" for r in result.residue]
    lines += [f"ACCEPTED {r.family} {r.kind} {r.name} {reason}" for r, reason in result.accepted]
    lines += [f"STALE {r.family} {r.kind} {r.name}" for r in result.stale_tag_index]
    lines.append(f"measured: {','.join(result.measured) or '-'}")
    lines += [
        f"unmeasured: {family} {exc.code} {exc.operation}: {exc.message}"
        for family, exc in result.unmeasured.items()
    ]
    return [line.replace(account, ACCOUNT_PLACEHOLDER) if account else line for line in lines]


def _err(message: str) -> None:
    print(f"scan_residue: {message}", file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    client_factory: ClientFactory | None = None,
    caller: Mapping[str, str] | None = None,
) -> int:
    """Run the scan and return its exit code.

    `client_factory` replaces the boto3 session's client factory and `caller` the
    `sts get_caller_identity` response (`Account`, `Arn`); both exist for tests.
    """
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else USAGE_EXIT

    present = [var for var in STATIC_CREDENTIAL_VARS if os.environ.get(var)]
    if present:
        _err(
            f"refusing: {', '.join(present)} set. Static credentials take precedence over "
            "AWS_PROFILE, so the scan would measure a different identity; unset them."
        )
        return USAGE_EXIT

    try:
        registry = build_registry()
        enumerated = build_enumerated_arn_types()
        config = _scan_config(args)
        selected = _select(registry, args.families)
    except ValueError as exc:
        _err(str(exc))
        return USAGE_EXIT

    try:
        factory = client_factory or _session_factory(args.profile, args.region)
        identity = dict(caller) if caller is not None else _resolve_caller(factory, args.region)
    except (BotoCoreError, Unmeasurable) as exc:
        _err(f"cannot establish the caller identity: {exc}")
        return USAGE_EXIT
    account = identity["Account"]
    print(f"caller: {arn_type(identity['Arn'])} (account resolved)")

    try:
        result = run_scan(
            factory,
            config,
            selected,
            _selected_tag_scanner(registry, selected),
            global_services.ARN_SERVICE_TO_FAMILY,
            enumerated_arn_types=enumerated,
        )
    except Exception as exc:  # noqa: BLE001
        _err(f"scan aborted, nothing measured reliably: {type(exc).__name__}: {exc}")
        return USAGE_EXIT

    code = exit_code(result)
    for line in render_lines(result, account):
        print(line)
    if args.json_path is not None:
        try:
            args.json_path.write_text(
                json.dumps(build_report(result, config, identity, code), indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _err(f"cannot write {args.json_path}: {exc}")
            return USAGE_EXIT
    print(f"exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
