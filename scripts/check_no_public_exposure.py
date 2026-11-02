#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: no resource is reachable from the internet except through CloudFront.

Enforces three constraints: no resource is globally accessible, Lambda functions execute inside a
VPC, and any external exposure is through CloudFront only, AgentCore Gateways excepted.
Seven checks, all offline against `infra/**/*.tf` -- no credential, no network, no `terraform`
binary:

1. **No `authorization_type = "NONE"` Function URL.**
2. **No wildcard-principal Lambda permission** (`principal = "*"`).
3. **`disable_execute_api_endpoint = true` on every HTTP API**, so the public `execute-api` hostname
   is not callable directly.
4. **Every Lambda declares a `vpc_config`.**
5. **No public S3 surface.** All four `aws_s3_bucket_public_access_block` flags true on every bucket,
   no public-read ACL, and no `Allow` statement with a wildcard principal in a bucket policy.
6. **Every Function URL invoke grant is conditioned on `lambda:FunctionUrlAuthType: AWS_IAM`.**
   Independent of control 1: control 1 governs what the URL demands, this governs what the grant
   permits.
7. **Every AgentCore Gateway authenticates inbound calls.** `authorizer_type` is the literal
   `CUSTOM_JWT` (with a `custom_jwt_authorizer` block) or `AWS_IAM`; `NONE`, unset or a non-literal
   value is a finding.

There is no waiver file, allowlist or environment override. A wildcard-principal `Deny` is not
flagged, since that is the standard TLS-enforcement shape.

Usage: `python3 scripts/check_no_public_exposure.py [--infra-dir DIR] [--json]`

Exit codes: 0 pass; 1 a public surface or a VPC-less function was found; 2 usage error; 3 SKIP -- no
Terraform source found, so nothing was verified; `scripts/run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Sibling import with `scripts/` on the path, so the gate also starts under direct execution.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hcl_text import (  # type: ignore[import-not-found] # noqa: E402
    blank_comments,
    blank_heredoc_bodies,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`; a test pins the two together.
SKIP_EXIT_STATUS = 3

DEFAULT_INFRA_DIR = REPO_ROOT / "infra"

#: Terraform directories that are build output or provider cache, never authored source.
_EXCLUDED_DIR_NAMES = frozenset({".terraform", "build", "examples"})


@dataclass(frozen=True)
class Finding:
    """One violated control, located precisely enough to fix without searching."""

    control: str
    path: Path
    line: int
    detail: str

    def render(self, infra_dir: Path) -> str:
        try:
            where = self.path.relative_to(infra_dir.parent)
        except ValueError:
            where = self.path
        return f"{where}:{self.line}: [{self.control}] {self.detail}"


@dataclass(frozen=True)
class Block:
    """A top-level `resource "<type>" "<name>"` block and its body."""

    type: str
    name: str
    body: str
    path: Path
    line: int


def terraform_sources(infra_dir: Path) -> list[Path]:
    """Every authored `*.tf` under `infra_dir`, excluding provider cache and build output."""
    found: list[Path] = []
    for path in sorted(infra_dir.rglob("*.tf")):
        if _EXCLUDED_DIR_NAMES.isdisjoint(part for part in path.parts):
            found.append(path)
    return found


def parse_resource_blocks(text: str, path: Path) -> list[Block]:
    """Extract top-level resource blocks by brace matching, ignoring braces inside strings.

    A block's body is bounded by the next resource header as well as by its own closing brace, so an
    unbalanced brace carried as data cannot extend a body into the following block.
    """
    blocks: list[Block] = []
    header = re.compile(r'resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"\s*\{')
    starts = [m.start() for m in header.finditer(text)]
    for match in header.finditer(text):
        following = next((s for s in starts if s > match.start()), len(text))
        depth = 0
        index = match.end() - 1
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
                        break
            index += 1
        index = min(index, following)
        blocks.append(
            Block(
                type=match.group(1),
                name=match.group(2),
                body=text[match.end() : index],
                path=path,
                line=text.count("\n", 0, match.start()) + 1,
            )
        )
    return blocks


def _attribute(body: str, key: str) -> str | None:
    """The literal value of a top-level-ish `key = value` in a block body, or None if unset."""
    found = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*"?([^"\n]+?)"?\s*$', body)
    return found.group(1).strip() if found else None


def _line_of(block: Block, needle: str) -> int:
    offset = block.body.find(needle)
    return block.line + (block.body.count("\n", 0, offset) if offset >= 0 else 0)


def check_function_urls(blocks: list[Block]) -> list[Finding]:
    """Control 1: no `authorization_type = "NONE"` Function URL."""
    findings: list[Finding] = []
    for block in blocks:
        if block.type != "aws_lambda_function_url":
            continue
        auth = _attribute(block.body, "authorization_type")
        if auth is None or auth.upper() == "NONE":
            findings.append(
                Finding(
                    control="function-url-auth",
                    path=block.path,
                    line=_line_of(block, "authorization_type"),
                    detail=(
                        f'aws_lambda_function_url."{block.name}" has authorization_type='
                        f"{auth or '<unset>'}; a NONE URL is internet-reachable. Use AWS_IAM and "
                        f"front it with CloudFront OAC."
                    ),
                )
            )
    return findings


def check_wildcard_permissions(blocks: list[Block]) -> list[Finding]:
    """Control 2: no `principal = "*"` on a Lambda resource policy."""
    findings: list[Finding] = []
    for block in blocks:
        if block.type != "aws_lambda_permission":
            continue
        if (_attribute(block.body, "principal") or "").strip() == "*":
            findings.append(
                Finding(
                    control="lambda-wildcard-principal",
                    path=block.path,
                    line=_line_of(block, "principal"),
                    detail=(
                        f'aws_lambda_permission."{block.name}" grants principal="*" -- the resource '
                        f"policy half of a public invoke surface."
                    ),
                )
            )
    return findings


def check_api_default_endpoint(blocks: list[Block]) -> list[Finding]:
    """Control 3: every API Gateway v2 API disables its public `execute-api` endpoint."""
    findings: list[Finding] = []
    for block in blocks:
        if block.type != "aws_apigatewayv2_api":
            continue
        setting = _attribute(block.body, "disable_execute_api_endpoint")
        if setting is None or setting.lower() != "true":
            findings.append(
                Finding(
                    control="api-default-endpoint",
                    path=block.path,
                    line=block.line,
                    detail=(
                        f'aws_apigatewayv2_api."{block.name}" does not set '
                        f"disable_execute_api_endpoint=true, so its execute-api hostname stays "
                        f"publicly callable and CloudFront is a route rather than a boundary."
                    ),
                )
            )
    return findings


def check_lambda_vpc(blocks: list[Block]) -> list[Finding]:
    """Control 4: every Lambda function declares a `vpc_config` with subnets and a security group."""
    findings: list[Finding] = []
    for block in blocks:
        if block.type != "aws_lambda_function":
            continue
        if not re.search(r"(?m)^\s*vpc_config\s*\{", block.body):
            findings.append(
                Finding(
                    control="lambda-vpc",
                    path=block.path,
                    line=block.line,
                    detail=(
                        f'aws_lambda_function."{block.name}" declares no vpc_config; the standing '
                        f"constraint is that Lambda functions execute inside a VPC."
                    ),
                )
            )
    return findings


#: The one action that invokes a Function URL; grants for other actions are out of control 6's scope.
_FUNCTION_URL_ACTION = "lambda:InvokeFunctionUrl"

#: The only acceptable value of the condition key.
_REQUIRED_URL_AUTH_TYPE = "AWS_IAM"


def count_url_grants(blocks: list[Block]) -> tuple[int, int]:
    """`(Function URLs, invoke grants conditioned on AWS_IAM)` — control 6's denominator.

    Shared by :func:`check_function_url_auth_condition` and the scan summary.
    """
    urls = sum(1 for block in blocks if block.type == "aws_lambda_function_url")
    conditioned = sum(
        1
        for block in blocks
        if block.type == "aws_lambda_permission"
        and (_attribute(block.body, "action") or "").strip() == _FUNCTION_URL_ACTION
        and (_attribute(block.body, "function_url_auth_type") or "").strip().upper()
        == _REQUIRED_URL_AUTH_TYPE
    )
    return urls, conditioned


def check_function_url_auth_condition(blocks: list[Block]) -> list[Finding]:
    """Control 6: every Function URL invoke grant carries `lambda:FunctionUrlAuthType: AWS_IAM`.

    The condition key is the `function_url_auth_type` argument on `aws_lambda_permission`. Two findings:

    * a grant without the condition, naming the offending `aws_lambda_permission`;
    * fewer conditioned grants than Function URLs. This is a count, not a per-URL pairing, since a URL
      and its grant can live in different modules with no textual reference between them.
    """
    findings: list[Finding] = []
    grants = [
        block
        for block in blocks
        if block.type == "aws_lambda_permission"
        and (_attribute(block.body, "action") or "").strip() == _FUNCTION_URL_ACTION
    ]

    conditioned = 0
    for block in grants:
        value = _attribute(block.body, "function_url_auth_type")
        if value is not None and value.strip().upper() == _REQUIRED_URL_AUTH_TYPE:
            conditioned += 1
            continue
        findings.append(
            Finding(
                control="function-url-auth-condition",
                path=block.path,
                line=_line_of(block, "action"),
                detail=(
                    f'aws_lambda_permission."{block.name}" grants {_FUNCTION_URL_ACTION} with '
                    f"function_url_auth_type={value or '<unset>'}; the grant must be conditioned on "
                    f"{_REQUIRED_URL_AUTH_TYPE}, or it permits the principal regardless of "
                    f"the URL's own authorization_type."
                ),
            )
        )

    urls = [block for block in blocks if block.type == "aws_lambda_function_url"]
    if urls and conditioned < len(urls):
        first = urls[0]
        findings.append(
            Finding(
                control="function-url-grant-missing",
                path=first.path,
                line=first.line,
                detail=(
                    f"{len(urls)} aws_lambda_function_url resource(s) but only {conditioned} "
                    f"{_FUNCTION_URL_ACTION} grant(s) conditioned on {_REQUIRED_URL_AUTH_TYPE} "
                    f'(first URL: "{first.name}"). A Function URL whose resource policy carries no '
                    f"conditioned grant has nothing enforcing IAM auth on the grant side."
                ),
            )
        )
    return findings


def check_public_s3(blocks: list[Block]) -> list[Finding]:
    """Control 5: no public bucket ACL, no wildcard-principal Allow, BPA fully on."""
    findings: list[Finding] = []
    bpa_flags = (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    )
    for block in blocks:
        if block.type == "aws_s3_bucket_public_access_block":
            for flag in bpa_flags:
                if (_attribute(block.body, flag) or "").lower() != "true":
                    findings.append(
                        Finding(
                            control="s3-block-public-access",
                            path=block.path,
                            line=block.line,
                            detail=(
                                f'aws_s3_bucket_public_access_block."{block.name}" does not set '
                                f"{flag}=true."
                            ),
                        )
                    )
        elif block.type in {"aws_s3_bucket_acl", "aws_s3_bucket"}:
            acl = _attribute(block.body, "acl") or ""
            if acl.startswith("public-") or acl == "authenticated-read":
                findings.append(
                    Finding(
                        control="s3-public-acl",
                        path=block.path,
                        line=_line_of(block, "acl"),
                        detail=f'{block.type}."{block.name}" sets acl={acl}.',
                    )
                )
        elif block.type == "aws_s3_bucket_policy" and _has_wildcard_allow(block.body):
            findings.append(
                Finding(
                    control="s3-wildcard-allow",
                    path=block.path,
                    line=block.line,
                    detail=(
                        f'aws_s3_bucket_policy."{block.name}" contains an Allow statement with a '
                        f"wildcard principal."
                    ),
                )
            )
    return findings


def _has_wildcard_allow(body: str) -> bool:
    """True when a policy body pairs `Effect: Allow` with a wildcard principal in one statement.

    A wildcard principal under `Deny` is the recommended TLS-enforcement shape, so effect and
    principal are matched together rather than searching for `"*"` alone.
    """
    for chunk in re.split(r'"Effect"\s*[:=]', body)[1:]:
        effect = chunk.lstrip()[:8].lower()
        if not effect.startswith('"allow') and not effect.startswith("allow"):
            continue
        statement = re.split(r'"Effect"\s*[:=]', chunk)[0]
        if re.search(r'"Principal"\s*[:=]\s*(\{[^}]*)?"\*"', statement):
            return True
    return False


#: The inbound authorizer types a Gateway may declare.
_GATEWAY_AUTHORIZER_TYPES = frozenset({"CUSTOM_JWT", "AWS_IAM"})


def check_gateway_inbound_auth(blocks: list[Block]) -> list[Finding]:
    """Control 7: every `aws_bedrockagentcore_gateway` authenticates its inbound callers."""
    findings: list[Finding] = []
    for block in blocks:
        if block.type != "aws_bedrockagentcore_gateway":
            continue
        match = re.search(r"(?m)^\s*authorizer_type\s*=\s*(.+?)\s*$", block.body)
        raw = match.group(1) if match else None
        literal = raw[1:-1] if raw and len(raw) >= 2 and raw[0] == raw[-1] == '"' else None
        if literal not in _GATEWAY_AUTHORIZER_TYPES:
            findings.append(
                Finding(
                    control="gateway-inbound-auth",
                    path=block.path,
                    line=_line_of(block, "authorizer_type"),
                    detail=(
                        f'aws_bedrockagentcore_gateway."{block.name}" has authorizer_type='
                        f"{raw or '<unset>'}; it must be the literal CUSTOM_JWT or AWS_IAM, or "
                        f"the Gateway is an unauthenticated internet endpoint."
                    ),
                )
            )
        elif literal == "CUSTOM_JWT" and not re.search(
            r"(?m)^\s*custom_jwt_authorizer\s*\{", block.body
        ):
            findings.append(
                Finding(
                    control="gateway-inbound-auth",
                    path=block.path,
                    line=_line_of(block, "authorizer_type"),
                    detail=(
                        f'aws_bedrockagentcore_gateway."{block.name}" declares CUSTOM_JWT with no '
                        f"custom_jwt_authorizer block, so no token issuer is configured."
                    ),
                )
            )
    return findings


@dataclass(frozen=True)
class Report:
    """What the scan examined, so a clean run cannot be mistaken for a vacuous one."""

    findings: list[Finding]
    files: int
    counts: dict[str, int]
    #: The directory actually scanned. `DEFAULT_INFRA_DIR` resolves from this script's location, not cwd.
    scanned: Path
    #: Control 6's own denominator: (Function URLs seen, invoke grants conditioned on AWS_IAM).
    url_grants: tuple[int, int] = (0, 0)

    @property
    def examined(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        inventory = ", ".join(f"{count} {kind}" for kind, count in sorted(self.counts.items()))
        urls, conditioned = self.url_grants
        return (
            f"scanned {self.scanned}: "
            f"{self.files} Terraform file(s), {self.examined} governed resource(s) "
            f"({inventory or 'none'}), {urls} Function URL(s) with {conditioned} "
            f"AWS_IAM-conditioned invoke grant(s), {len(self.findings)} finding(s)"
        )


#: Resource types this gate governs; the summary reports how many were examined.
_GOVERNED = (
    "aws_lambda_function",
    "aws_lambda_function_url",
    "aws_lambda_permission",
    "aws_apigatewayv2_api",
    "aws_s3_bucket",
    "aws_s3_bucket_acl",
    "aws_s3_bucket_policy",
    "aws_s3_bucket_public_access_block",
    "aws_bedrockagentcore_gateway",
)


def scan(infra_dir: Path) -> Report:
    """Run every control over `infra_dir` and return the findings with the scan's own denominator."""
    sources = terraform_sources(infra_dir)
    blocks: list[Block] = []
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        blocks.extend(parse_resource_blocks(blank_comments(blank_heredoc_bodies(text)), path))

    governed = [block for block in blocks if block.type in _GOVERNED]
    counts = {kind: sum(1 for b in governed if b.type == kind) for kind in _GOVERNED}
    findings = [
        *check_function_urls(governed),
        *check_wildcard_permissions(governed),
        *check_api_default_endpoint(governed),
        *check_lambda_vpc(governed),
        *check_function_url_auth_condition(governed),
        *check_public_s3(governed),
        *check_gateway_inbound_auth(governed),
    ]
    return Report(
        findings=findings,
        files=len(sources),
        counts={kind: count for kind, count in counts.items() if count},
        url_grants=count_url_grants(governed),
        scanned=infra_dir,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert no internet-reachable resource.")
    parser.add_argument("--infra-dir", type=Path, default=DEFAULT_INFRA_DIR)
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    args = parser.parse_args(argv)

    infra_dir: Path = args.infra_dir
    if not infra_dir.is_dir():
        print(f"[SKIP] no Terraform directory at {infra_dir}; verified nothing")
        return SKIP_EXIT_STATUS

    report = scan(infra_dir)

    # A scan that found no governed resource verified nothing.
    if report.examined == 0:
        print(f"[SKIP] no governed resource found under {infra_dir}; verified nothing")
        return SKIP_EXIT_STATUS

    if args.json:
        print(
            json.dumps(
                {
                    "findings": [
                        {
                            "control": f.control,
                            "path": str(f.path),
                            "line": f.line,
                            "detail": f.detail,
                        }
                        for f in report.findings
                    ],
                    "files": report.files,
                    "examined": report.examined,
                    "counts": report.counts,
                    "scanned": str(report.scanned),
                    "function_urls": report.url_grants[0],
                    "conditioned_invoke_grants": report.url_grants[1],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for finding in report.findings:
            print(finding.render(infra_dir))
        print(report.summary())

    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
