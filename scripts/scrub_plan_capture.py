#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Scrubs a raw `terraform show -json` plan into the `plan_fixtures` base layout.

Usage:
    python3 scripts/scrub_plan_capture.py --plan RAW.json [--infra-root DIR]
        [--deny-token TOKEN ...] [--deny-tokens-file PATH] [--out PATH]

Keeps the header (`format_version`, `configuration`, `variables`) and `resource_changes`, then maps:
the caller's account id to `PLACEHOLDER_ACCOUNT_ID`; each adopted-network id named by a variable in
`NETWORK_PLACEHOLDERS` to its placeholder; the caller's IAM role and session names and every
`crm_operator_principal_arns` name to example names; IAM unique ids to `PLACEHOLDER_UNIQUE_ID`; and
the profile to `PLACEHOLDER_PROFILE`. A fresh `_fixture_provenance` (no `mutation`) is added, recording
`captured_infra_digest` = `plan_fixtures.infra_digest(--infra-root)` (default: this repository), the
git work tree whose tracked `infra/**/*.tf` the plan was captured from.

Before writing, `self_check` refuses on a residual 12-digit number other than the placeholder, a
real-shaped resource, IAM unique or CloudFront VPC-origin id, any deny-token (case-insensitive), or
any real value a mapping replaced (`derived_real_values`). At least one deny-token is required. Exit 0 when written
(default `plan_fixtures.BASE_PATH`); exit 2 on a refusal or unusable input, with nothing written.
Findings name the class, line and deny-token index, never the matched value.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import plan_fixtures  # noqa: E402

PLACEHOLDER_ACCOUNT_ID: Final[str] = "123456789012"
PLACEHOLDER_UNIQUE_ID: Final[str] = "AIDA-example-caller-user-id"
PLACEHOLDER_CALLER_NAME: Final[str] = "example-deployer"
PLACEHOLDER_SESSION_NAME: Final[str] = "example-session"
PLACEHOLDER_PROFILE: Final[str] = "example-deployer"
OPERATOR_NAME_PREFIX: Final[str] = "example-operator-"

#: Adopted-network variable -> placeholder per list index, applied in this order; an id already
#: mapped keeps its first placeholder.
NETWORK_PLACEHOLDERS: Final[Mapping[str, tuple[str, ...]]] = {
    "vpc_id": ("vpc-adoptedplaceholder",),
    "vpc_nat_public_subnet_id": ("subnet-natpublicplaceholder",),
    "vpc_private_subnet_ids": ("subnet-privateoneplaceholder", "subnet-privatetwoplaceholder"),
    "vpc_runtime_subnet_ids": ("subnet-runtimeoneplaceholder", "subnet-runtimetwoplaceholder"),
    "vpc_private_route_table_ids": ("rtb-privateoneplaceholder", "rtb-privatetwoplaceholder"),
    "vpc_runtime_route_table_ids": ("rtb-runtimeplaceholder",),
    "api_origin_ingress_security_group_ids": ("sg-cloudfrontvpcoriginplaceholder",),
}

RESOURCE_ID: Final[re.Pattern[str]] = re.compile(
    r"\b(?:vpc|subnet|rtb|sg|vpce|igw|eni|nat|eipalloc)-[0-9a-f]{8}(?:[0-9a-f]{9})?"
)
UNIQUE_ID: Final[re.Pattern[str]] = re.compile(
    r"\bA(?:IDA|ROA|KIA|SIA|NPA|NVA|GPA|PKA)[A-Z0-9]{12,}"
)
VPC_ORIGIN_ID: Final[re.Pattern[str]] = re.compile(
    r"\bvo_[A-Za-z0-9]*[A-Z0-9][A-Za-z0-9]*[A-Z0-9][A-Za-z0-9]*"
)
TWELVE_DIGITS: Final[re.Pattern[str]] = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
INFRA_DIGEST: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_IAM_NAME_CHAR: Final[str] = r"[A-Za-z0-9+=,.@_-]"

#: Profile names too generic to be identifying, so never derived as deny-tokens.
GENERIC_PROFILES: Final[frozenset[str]] = frozenset({"default"})

CAPTURE_PROCEDURE: Final[str] = (
    "terraform show -json of a create-everything plan captured against EMPTY LOCAL STATE in a "
    "scratch copy of infra/ (providers and build/ symlinked, backend block removed so state is "
    "local), no deploy."
)


class ScrubError(ValueError):
    """The raw plan cannot be scrubbed, or the scrubbed output fails `self_check`."""


@dataclass(frozen=True)
class Finding:
    """One self-check hit: its class and 1-based output line, never the matched text."""

    kind: str
    line: int


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _map_strings(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, dict):
        return {fn(key): _map_strings(item, fn) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_strings(item, fn) for item in value]
    if isinstance(value, str):
        return fn(value)
    return value


def _prior_resources(plan: Mapping[str, Any], resource_type: str) -> list[dict[str, Any]]:
    root = (plan.get("prior_state") or {}).get("values", {}).get("root_module", {})
    stack: list[dict[str, Any]] = [root]
    found: list[dict[str, Any]] = []
    while stack:
        module = stack.pop()
        stack.extend(module.get("child_modules", []))
        found.extend(r for r in module.get("resources", []) if r.get("type") == resource_type)
    return found


def caller_account_id(plan: Mapping[str, Any]) -> str:
    """The single account id every `aws_caller_identity` in `prior_state` reports."""
    ids = {
        r.get("values", {}).get("account_id") for r in _prior_resources(plan, "aws_caller_identity")
    }
    if len(ids) != 1 or not TWELVE_DIGITS.fullmatch(str(next(iter(ids)))):
        raise ScrubError(f"expected one caller account id in prior_state, found {len(ids)}")
    return str(next(iter(ids)))


def caller_names(plan: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """`(role_name, session_name)` from `aws_iam_session_context`, or `None` where absent."""
    contexts = _prior_resources(plan, "aws_iam_session_context")
    roles = {c.get("values", {}).get("issuer_name") for c in contexts} - {None, ""}
    sessions = {c.get("values", {}).get("session_name") for c in contexts} - {None, ""}
    if len(roles) > 1 or len(sessions) > 1:
        raise ScrubError("prior_state carries more than one caller session context")
    return (next(iter(roles), None), next(iter(sessions), None))


def network_mapping(variables: Mapping[str, Any]) -> dict[str, str]:
    """Real adopted-network id -> placeholder, from the variables in `NETWORK_PLACEHOLDERS`."""
    mapping: dict[str, str] = {}
    for name, placeholders in NETWORK_PLACEHOLDERS.items():
        value = (variables.get(name) or {}).get("value")
        ids = value if isinstance(value, list) else [value]
        real = [i for i in ids if isinstance(i, str) and RESOURCE_ID.fullmatch(i)]
        if len(real) > len(placeholders):
            raise ScrubError(f"variable {name} holds {len(real)} ids, {len(placeholders)} mapped")
        for resource_id, placeholder in zip(real, placeholders, strict=False):
            mapping.setdefault(resource_id, placeholder)
    return mapping


def iam_name_mapping(variables: Mapping[str, Any], caller_role: str | None) -> dict[str, str]:
    """IAM role/user name -> example name for the caller and every operator principal."""
    mapping = {caller_role: PLACEHOLDER_CALLER_NAME} if caller_role else {}
    operators = (variables.get("crm_operator_principal_arns") or {}).get("value") or []
    names = [str(arn).rsplit("/", 1)[-1] for arn in operators]
    for name in dict.fromkeys(n for n in names if n and n not in mapping):
        mapping[name] = f"{OPERATOR_NAME_PREFIX}{len(mapping) + 1 - bool(caller_role)}"
    return mapping


def _replacer(
    account_id: str,
    networks: Mapping[str, str],
    names: Mapping[str, str],
    session: str | None,
    profile: str | None,
) -> Callable[[str], str]:
    account = re.compile(rf"(?<![0-9]){re.escape(account_id)}(?![0-9])")
    name_patterns = [
        (
            re.compile(
                rf"(:role/|:user/|assumed-role/)((?:[^/\"\s]*/)*){re.escape(n)}(?!{_IAM_NAME_CHAR})"
            ),
            p,
        )
        for n, p in names.items()
    ]
    session_pattern = (
        re.compile(rf"(?<!{_IAM_NAME_CHAR}){re.escape(session)}(?!{_IAM_NAME_CHAR})")
        if session
        else None
    )
    profile_pattern = (
        re.compile(rf"(?<!{_IAM_NAME_CHAR}){re.escape(profile)}(?!{_IAM_NAME_CHAR})")
        if profile
        else None
    )

    def replace(text: str) -> str:
        text = account.sub(PLACEHOLDER_ACCOUNT_ID, text)
        text = RESOURCE_ID.sub(lambda m: networks.get(m.group(0), m.group(0)), text)
        text = UNIQUE_ID.sub(PLACEHOLDER_UNIQUE_ID, text)
        for pattern, placeholder in name_patterns:
            text = pattern.sub(rf"\g<1>\g<2>{placeholder}", text)
        if session_pattern is not None:
            text = session_pattern.sub(PLACEHOLDER_SESSION_NAME, text)
        if profile_pattern is not None:
            text = profile_pattern.sub(PLACEHOLDER_PROFILE, text)
        return text

    return replace


def _capture_date(plan: Mapping[str, Any]) -> str:
    timestamp = plan.get("timestamp")
    if not isinstance(timestamp, str) or not re.match(r"\d{4}-\d{2}-\d{2}T", timestamp):
        raise ScrubError("the plan carries no ISO-8601 timestamp")
    return timestamp[:10]


def derived_real_values(plan: Mapping[str, Any]) -> list[str]:
    """The real values the mappings replace, for `self_check` to refuse if any survives.

    Covers the account id, the caller's role and session, every operator principal name, the
    adopted-network ids and a non-generic profile. Raises `ScrubError` as `scrub` does.
    """
    try:
        variables = plan["variables"]
        role, session = caller_names(plan)
        values = [caller_account_id(plan), *network_mapping(variables)]
        values += [n for n in iam_name_mapping(variables, role) if n]
        profile = _identifying_profile(variables)
    except (KeyError, TypeError) as exc:
        raise ScrubError(f"the plan is missing a required field: {exc!r}") from exc
    return values + [v for v in (session, profile) if v]


def _identifying_profile(variables: Mapping[str, Any]) -> str | None:
    """The `aws_profile` value, unless absent or in `GENERIC_PROFILES`."""
    profile = (variables.get("aws_profile") or {}).get("value")
    if isinstance(profile, str) and profile and profile not in GENERIC_PROFILES:
        return profile
    return None


def _provenance(plan: Mapping[str, Any], infra_digest: str) -> dict[str, str]:
    changes = plan["resource_changes"]
    creates = sum(1 for rc in changes if rc["change"]["actions"] == ["create"])
    reads = sum(1 for rc in changes if rc["change"]["actions"] == ["read"])
    others = len(changes) - creates - reads
    if others:
        raise ScrubError(f"{others} resource_changes are neither create nor read")
    flags = "; ".join(
        f"{name} = {json.dumps(v['value'])}"
        for name, v in sorted(plan["variables"].items())
        if isinstance(v.get("value"), bool)
    )
    return {
        "captured_at": _capture_date(plan),
        "captured_infra_digest": infra_digest,
        "currency": (
            "Captured from a tree whose tracked infra/**/*.tf digest is captured_infra_digest. "
            "plan_freshness passes while plan_fixtures.infra_digest of the working tree equals it. "
            "The adopted network (VPC, subnets, route tables, the CloudFront VPC-origin security "
            "group) is created outside Terraform and adopted by the gitignored tfvars, so its ids "
            "are scrubbed to placeholders."
        ),
        "derived_from": "native capture; no earlier fixture content",
        "how": (
            f"{CAPTURE_PROCEDURE} Plan: {creates} to add, 0 to change, 0 to destroy; {reads} data "
            "reads. Scrubbed by scripts/scrub_plan_capture.py: account id -> "
            f"{PLACEHOLDER_ACCOUNT_ID}, IAM unique ids -> {PLACEHOLDER_UNIQUE_ID}, caller role and "
            f"session -> {PLACEHOLDER_CALLER_NAME}/{PLACEHOLDER_SESSION_NAME}, operator principals "
            f"-> {OPERATOR_NAME_PREFIX}<n>, aws_profile -> {PLACEHOLDER_PROFILE}, adopted-network "
            "ids -> non-hex <role>placeholder tokens."
        ),
        "variables_at_capture": (
            f"{flags}; adopted-network variables and crm_operator_principal_arns from the "
            "gitignored infra/terraform.tfvars. NONE has a tracked default that supplies a real id."
        ),
    }


def scrub(plan: Mapping[str, Any], infra_digest: str) -> list[str]:
    """Return the scrubbed base as sorted JSON lines: the header, then one resource change each.

    Raises:
        ScrubError: `infra_digest` is not a 64-character lowercase sha256 hex digest, or the plan
            lacks what a mapping is derived from.
    """
    if not INFRA_DIGEST.fullmatch(infra_digest):
        raise ScrubError("infra_digest must be a 64-character lowercase sha256 hex digest")
    try:
        variables = plan["variables"]
        account_id = caller_account_id(plan)
        role, session = caller_names(plan)
        replace = _replacer(
            account_id,
            network_mapping(variables),
            iam_name_mapping(variables, role),
            session,
            _identifying_profile(variables),
        )
        header = {
            "_fixture_provenance": _provenance(plan, infra_digest),
            "format_version": plan["format_version"],
            "configuration": plan["configuration"],
            "variables": variables,
        }
        changes = plan["resource_changes"]
    except (KeyError, TypeError) as exc:
        raise ScrubError(f"the plan is missing a required field: {exc!r}") from exc
    if set(header) != plan_fixtures.HEADER_KEYS:
        raise ScrubError(f"header keys {sorted(header)} != {sorted(plan_fixtures.HEADER_KEYS)}")
    scrubbed = _map_strings(header, replace)
    if "aws_profile" in scrubbed["variables"]:
        scrubbed["variables"]["aws_profile"]["value"] = PLACEHOLDER_PROFILE
    documents = [scrubbed, *(_map_strings(rc, replace) for rc in changes)]
    return [json.dumps(document, sort_keys=True) for document in documents]


def self_check(
    lines: Sequence[str], deny_tokens: Sequence[str], derived: Sequence[str] = ()
) -> list[Finding]:
    """Every residual identifier in `lines`: account ids, resource/unique ids, deny-tokens, and
    `derived` real values. Tokens match case-insensitively, on the raw and the JSON-decoded text."""
    findings: list[Finding] = []
    lowered = [token.lower() for token in deny_tokens]
    derived_lowered = [value.lower() for value in derived]
    for number, line in enumerate(lines, start=1):
        if any(m.group(0) != PLACEHOLDER_ACCOUNT_ID for m in TWELVE_DIGITS.finditer(line)):
            findings.append(Finding("twelve_digit_number", number))
        for kind, pattern in (
            ("resource_id", RESOURCE_ID),
            ("iam_unique_id", UNIQUE_ID),
            ("vpc_origin_id", VPC_ORIGIN_ID),
        ):
            if pattern.search(line):
                findings.append(Finding(kind, number))
        try:
            decoded = "\n".join(_strings(json.loads(line)))
        except json.JSONDecodeError:
            decoded = ""
        text = f"{line}\n{decoded}".lower()
        findings.extend(
            Finding(f"deny_token[{index}]", number)
            for index, token in enumerate(lowered)
            if token in text
        )
        if any(value in text for value in derived_lowered):
            findings.append(Finding("derived_real_value", number))
    return findings


def _write_atomic(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _deny_tokens(args: argparse.Namespace) -> list[str]:
    tokens = list(args.deny_token)
    if args.deny_tokens_file is not None:
        text = args.deny_tokens_file.read_text(encoding="utf-8")
        tokens.extend(line.strip() for line in text.splitlines())
    return [token for token in tokens if token]


def main(argv: Sequence[str] | None = None) -> int:
    """Scrub `--plan`, self-check it against the deny-tokens, and write `--out`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="raw terraform show -json output")
    parser.add_argument(
        "--infra-root",
        type=Path,
        default=plan_fixtures.REPO_ROOT,
        help="git work tree whose tracked infra/**/*.tf the plan was captured from",
    )
    parser.add_argument("--deny-token", action="append", default=[], help="a forbidden string")
    parser.add_argument("--deny-tokens-file", type=Path, help="forbidden strings, one per line")
    parser.add_argument("--out", type=Path, default=plan_fixtures.BASE_PATH, help="output path")
    args = parser.parse_args(argv)
    try:
        tokens = _deny_tokens(args)
        if not tokens:
            raise ScrubError("no deny-token given; pass --deny-token or --deny-tokens-file")
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        lines = scrub(plan, plan_fixtures.infra_digest(args.infra_root))
        derived = derived_real_values(plan)
    except (OSError, ValueError) as exc:
        print(f"scrub_plan_capture: refused: {exc}", file=sys.stderr)
        return 2
    findings = self_check(lines, tokens, derived)
    if findings:
        for finding in findings:
            print(
                f"scrub_plan_capture: residual {finding.kind} on line {finding.line}",
                file=sys.stderr,
            )
        print(
            f"scrub_plan_capture: refused: {len(findings)} residual(s); nothing written",
            file=sys.stderr,
        )
        return 2
    _write_atomic(args.out, lines)
    print(f"scrub_plan_capture: wrote {len(lines)} lines to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
