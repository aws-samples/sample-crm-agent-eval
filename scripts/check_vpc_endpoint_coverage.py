#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assert every AWS service `src/` builds an SDK client for is reachable from inside the VPC.

The VPC has no internet gateway, so `local.vpc_interface_endpoint_services` and the gateway endpoint
list in `infra/main.tf` define which AWS services its handlers can reach. The required set is derived
from the SDK clients `src/` constructs; a discovered service passes by appearing in either endpoint
list. `_OUTSIDE_VPC_ALLOWLIST` names services called only from outside the VPC, with a reason per entry.

When the Runtime is in VPC mode, `_NON_SDK_VPC_MODE_REQUIREMENTS` (the ECR image-pull endpoints
AgentCore uses, which no `src/` client constructs) is required as well.

Scanner limitations:

- Python detection is AST-based and matches `<receiver>.client("svc")` / `.resource("svc")` where the
  receiver's source text mentions `boto3` or `session`. A client built through a receiver named
  neither — `c = get_thing(); c.client("kinesis")` — is not detected.
- Only a string literal service name is detected. `boto3.client(name_from_config)` is invisible.
- TypeScript detection reads `@aws-sdk/client-<svc>` import specifiers in non-test `.ts` sources under
  `src/`. Package manifests are not read.

Usage: `python3 scripts/check_vpc_endpoint_coverage.py [--json]`

Exit codes: 0 pass; 1 a discovered service is reachable from nowhere; 2 usage/environment error;
3 nothing was verified — no SDK client was found, the endpoint lists could not be read, or the
sentinel service was missed. `run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SRC: Final[Path] = _REPO_ROOT / "src"
_INFRA_MAIN: Final[Path] = _REPO_ROOT / "infra" / "main.tf"

#: Services whose only callers execute outside the VPC, so no endpoint is owed; each value is the reason.
_OUTSIDE_VPC_ALLOWLIST: Final[dict[str, str]] = {
    "sts": "no in-VPC caller; the account id is resolved by the operator's CLI during deploy",
}

#: Endpoints AgentCore uses to pull the Runtime image in VPC mode, required only in that mode. Checked
#: by presence in the endpoint lists only.
_NON_SDK_VPC_MODE_REQUIREMENTS: Final[dict[str, str]] = {
    "ecr.api": "AgentCore pulls the Runtime container image; no src/ SDK client constructs it",
    "ecr.dkr": "AgentCore pulls the Runtime container image layers; no src/ SDK client constructs it",
}

#: Matches the literal `runtime_network_mode = "VPC"` at the `module "agentcore"` call site.
_RUNTIME_VPC_MODE: Final[re.Pattern[str]] = re.compile(
    r'runtime_network_mode\s*=\s*"VPC"', re.MULTILINE
)

#: boto3 client names that differ from the VPC endpoint service short name.
_SERVICE_ALIASES: Final[dict[str, str]] = {
    "cloudwatch": "monitoring",  # CloudWatch metrics; `logs` is a separate service and needs no alias
    "stepfunctions": "states",
    "sfn": "states",
    "efs": "elasticfilesystem",
}

#: A service the repository both calls and declares; the scanner failing to find it means it is broken.
_SENTINEL_SERVICE: Final[str] = "dynamodb"

_INTERFACE_LIST: Final[re.Pattern[str]] = re.compile(
    r"vpc_interface_endpoint_services\s*=\s*\[(?P<body>[^\]]*)\]", re.MULTILINE
)
_GATEWAY_LIST: Final[re.Pattern[str]] = re.compile(
    r"gateway_endpoint_service_names\s*=\s*\[(?P<body>[^\]]*)\]", re.MULTILINE
)
_TS_CLIENT_IMPORT: Final[re.Pattern[str]] = re.compile(r"@aws-sdk/client-(?P<service>[a-z0-9-]+)")


class UnverifiableError(Exception):
    """Raised when the gate cannot establish one of its two sides, so it verified nothing (exit 3)."""


def _hcl_string_members(body: str) -> frozenset[str]:
    """Return the double- or single-quoted string members of an HCL list body, ignoring `#` comments."""
    without_comments = re.sub(r"#[^\n]*", "", body)
    return frozenset(re.findall(r"[\"']([^\"']+)[\"']", without_comments))


def endpoint_services(main_tf: Path | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """Return the (interface, gateway) endpoint service names declared in `infra/main.tf`.

    Args:
        main_tf: The Terraform root module to read. Resolved at call time when omitted, so a test can
            redirect it.

    Raises:
        UnverifiableError: The file is unreadable, either list is absent, or the interface list is
            empty.
    """
    target = _INFRA_MAIN if main_tf is None else main_tf
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnverifiableError(f"cannot read {target}: {exc}") from exc

    interface_match = _INTERFACE_LIST.search(text)
    if interface_match is None:
        raise UnverifiableError(
            f"no `vpc_interface_endpoint_services = [ … ]` list in {target.name} "
            "(or its shape changed)"
        )
    gateway_match = _GATEWAY_LIST.search(text)
    if gateway_match is None:
        raise UnverifiableError(
            f"no `gateway_endpoint_service_names = [ … ]` list in {target.name} "
            "(or its shape changed)"
        )

    interface = _hcl_string_members(interface_match.group("body"))
    gateway = _hcl_string_members(gateway_match.group("body"))
    if not interface:
        raise UnverifiableError(
            f"`vpc_interface_endpoint_services` in {target.name} lists no services"
        )
    return interface, gateway


def runtime_in_vpc_mode(main_tf: Path | None = None) -> bool:
    """Whether `infra/main.tf` puts the AgentCore Runtime in VPC mode.

    Reads the `runtime_network_mode = "VPC"` literal at the `module "agentcore"` call site. When true,
    the ECR image-pull endpoints AgentCore needs (`_NON_SDK_VPC_MODE_REQUIREMENTS`) become required even
    though no `src/` SDK client constructs them.

    Raises:
        UnverifiableError: the file is unreadable, so the requirement cannot be established either way.
    """
    target = _INFRA_MAIN if main_tf is None else main_tf
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnverifiableError(f"cannot read {target}: {exc}") from exc
    return _RUNTIME_VPC_MODE.search(_strip_hcl_comments(text)) is not None


def _strip_hcl_comments(text: str) -> str:
    """Blank `#`/`//` line comments so a commented-out `runtime_network_mode = "VPC"` is not matched."""
    return re.sub(r"(#|//)[^\n]*", "", text)


def _label(path: Path) -> str:
    """Repository-relative path for a message, falling back to the absolute one outside the repository."""
    try:
        return path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _receiver_text(node: ast.expr) -> str:
    """Best-effort source text for a call's receiver, used to recognise a boto3/session client build."""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - an unparseable receiver simply does not match below
        return ""


def python_sdk_services(tree_root: Path | None = None) -> dict[str, set[str]]:
    """Map each service named in a `boto3` client/resource construction to the files constructing it.

    AST-based, so a service named in a docstring or comment is not counted and a call split across
    lines is. See the module docstring for what this does not detect.

    Args:
        tree_root: Directory to walk. Resolved at call time when omitted.

    Raises:
        UnverifiableError: The tree is missing or holds no Python files.
    """
    target = _SRC if tree_root is None else tree_root
    if not target.is_dir():
        raise UnverifiableError(f"source tree not found at {target}")
    sources = sorted(target.rglob("*.py"))
    if not sources:
        raise UnverifiableError(f"no Python sources under {target}")

    found: dict[str, set[str]] = {}
    for path in sources:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            raise UnverifiableError(f"cannot parse {path}: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"client", "resource"}:
                continue
            receiver = _receiver_text(node.func.value).lower()
            if "boto3" not in receiver and "session" not in receiver:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            service = first.value.strip()
            if service:
                rel = _label(path)
                found.setdefault(service, set()).add(rel)
    return found


def typescript_sdk_services(tree_root: Path | None = None) -> dict[str, set[str]]:
    """Map each `@aws-sdk/client-<svc>` service to the non-test TypeScript sources importing it.

    Args:
        tree_root: Directory to walk. Resolved at call time when omitted.

    Raises:
        UnverifiableError: The tree is missing.
    """
    target = _SRC if tree_root is None else tree_root
    if not target.is_dir():
        raise UnverifiableError(f"source tree not found at {target}")

    found: dict[str, set[str]] = {}
    for path in sorted(target.rglob("*.ts")):
        parts = set(path.parts)
        if "node_modules" in parts or "tests" in parts or path.name.endswith(".test.ts"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in _TS_CLIENT_IMPORT.finditer(text):
            rel = _label(path)
            found.setdefault(match.group("service"), set()).add(rel)
    return found


def check(
    *,
    tree_root: Path | None = None,
    main_tf: Path | None = None,
    allowlist: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare the services `src/` constructs clients for against the declared endpoint lists.

    Returns:
        A report dict. `ok` is true when every discovered, non-allowlisted service appears in one of
        the two endpoint lists.

    Raises:
        UnverifiableError: Nothing could be verified — see the module docstring's exit codes.
    """
    exempt = _OUTSIDE_VPC_ALLOWLIST if allowlist is None else allowlist
    interface, gateway = endpoint_services(main_tf)
    reachable = interface | gateway
    vpc_mode = runtime_in_vpc_mode(main_tf)
    non_sdk_missing = (
        {
            service: reason
            for service, reason in _NON_SDK_VPC_MODE_REQUIREMENTS.items()
            if service not in reachable
        }
        if vpc_mode
        else {}
    )

    discovered: dict[str, set[str]] = {}
    for source_map in (python_sdk_services(tree_root), typescript_sdk_services(tree_root)):
        for service, files in source_map.items():
            discovered.setdefault(_SERVICE_ALIASES.get(service, service), set()).update(files)

    if not discovered:
        raise UnverifiableError(
            "found no SDK client construction anywhere under the source tree; this repository "
            "certainly builds several, so the scanner is broken rather than the tree being clean"
        )
    if _SENTINEL_SERVICE not in discovered:
        raise UnverifiableError(
            f"the scanner did not find the sentinel service {_SENTINEL_SERVICE!r}, which this "
            "repository certainly constructs a client for; a count above zero is not evidence the "
            "scan discriminates"
        )

    missing = {
        service: sorted(files)
        for service, files in sorted(discovered.items())
        if service not in reachable and service not in exempt
    }
    return {
        "ok": not missing and not non_sdk_missing,
        "discovered": {service: sorted(files) for service, files in sorted(discovered.items())},
        "interface_endpoints": sorted(interface),
        "gateway_endpoints": sorted(gateway),
        "allowlisted": sorted(service for service in discovered if service in exempt),
        # An allowlist entry nothing constructs: reported, never a failure.
        "unused_allowlist_entries": sorted(set(exempt) - set(discovered)),
        "missing_endpoints": missing,
        # Non-SDK requirements, required only when the Runtime is in VPC mode.
        "runtime_vpc_mode": vpc_mode,
        "non_sdk_required": sorted(_NON_SDK_VPC_MODE_REQUIREMENTS) if vpc_mode else [],
        "non_sdk_missing_endpoints": non_sdk_missing,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the gate. See the module docstring for exit codes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    try:
        report = check()
    except UnverifiableError as exc:
        if args.json:
            sys.stdout.write(json.dumps({"ok": False, "skipped": True, "reason": str(exc)}) + "\n")
        else:
            sys.stdout.write(f"check_vpc_endpoint_coverage: SKIP — verified nothing ({exc})\n")
        return 3

    if args.json:
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0 if report["ok"] else 1

    if report["ok"]:
        sys.stdout.write(
            "check_vpc_endpoint_coverage: OK — every in-VPC SDK client has an endpoint "
            f"({len(report['discovered'])} services discovered; "
            f"allowlisted as outside-VPC: {report['allowlisted'] or 'none'})\n"
        )
        if report["runtime_vpc_mode"]:
            sys.stdout.write(
                "  runtime in VPC mode: non-SDK image-pull endpoints required and present: "
                f"{report['non_sdk_required']} (presence-only; the endpoint actually serving the "
                "Runtime is a T3 live check)\n"
            )
        if report["unused_allowlist_entries"]:
            sys.stdout.write(
                "  note: allowlist entries with no caller in the tree: "
                f"{report['unused_allowlist_entries']}\n"
            )
        return 0

    sys.stdout.write(
        "check_vpc_endpoint_coverage: FAILED — reachable from nowhere inside the VPC:\n"
    )
    for service, files in report["missing_endpoints"].items():
        sys.stdout.write(f"  {service}: constructed in {', '.join(files)}\n")
    for service, reason in report["non_sdk_missing_endpoints"].items():
        sys.stdout.write(f"  {service}: required because the Runtime is in VPC mode — {reason}\n")
    sys.stdout.write(
        "Each service above is called from inside the VPC but appears in neither "
        "`vpc_interface_endpoint_services` nor `gateway_endpoint_service_names` in infra/main.tf. "
        "With NAT enabled the call leaves the VPC over the internet; with NAT disabled it hangs to "
        "the SDK retry budget and fail-closes. Add the endpoint, or — if the caller does not execute "
        "in a Lambda — add the service to this gate's allowlist with the reason.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
