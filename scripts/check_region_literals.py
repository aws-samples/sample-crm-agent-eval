#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: no hardwired default-region literal or AZ id outside the region map.

Checks, selected with `--check` (repeatable; `all` is the default):

- `literals`: every tracked file outside `EXEMPT_CLASSES` is scanned for `REGION_LITERAL` and for
  AZ ids (`AZ_ID_PATTERN`). A file with hits must be pinned in `EXEMPT_FILES` with its exact hit
  count; a count that differs from its pin either way fails, as does a pin for a file that is no
  longer tracked. AZ ids belong in `infra/regions.json` only.
- `phrases`: no tracked `*.md` matches `BANNED_PHRASE_PATTERN`.
- `plan`: every string in each `--plan` JSON document that contains `REGION_LITERAL` must sit at
  a JSON path matching `PLAN_ALLOWED_PATHS`. Every hit is printed with its path. `all` includes
  this check only when `--plan` is given.

Usage: `python3 scripts/check_region_literals.py [--root DIR] [--check literals|phrases|plan|all]...
[--plan PATH]...`

Exit codes: 0 pass; 1 violation; 2 usage or environment error (not a git work tree, unreadable
plan); 3 a selected check scanned nothing (never a pass).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS = 3

REGION_LITERAL: Final[str] = "us-east-1"
AZ_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:use|usw)[0-9]-az[0-9]+\b")
LITERAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"{re.escape(REGION_LITERAL)}|{AZ_ID_PATTERN.pattern}"
)
BANNED_PHRASE_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?i){re.escape(REGION_LITERAL)} only|only supported region"
)
PLAN_ALLOWED_PATHS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"^configuration\.(root_module|.*module_calls\.[^.]+\.module)"
        r"\.variables\.aws_region\.default$"
    ),
)

#: `(glob, reason)`: tracked paths never scanned by `literals`, matched with `PurePath.full_match`.
EXEMPT_CLASSES: Final[tuple[tuple[str, str], ...]] = (
    ("tests/**", "test fixtures and recorded plans"),
    ("src/relay/tests/**", "relay test fixtures"),
    ("frontend/**/*.test.ts", "frontend tests"),
    ("frontend/**/*.test.tsx", "frontend tests"),
    ("frontend/src/test/**", "frontend test helpers"),
    ("frontend/src/mocks/**", "frontend mock data"),
    ("**/*.md", "docs keep labelled examples; `phrases` checks them"),
    ("docs/figures/**", "figure sources"),
    ("results/**", "generated artifacts"),
    ("infra/regions.json", "the region map itself"),
)

CATEGORIES: Final[frozenset[str]] = frozenset(
    {"default", "aws-semantics", "provenance", "example", "placeholder"}
)


@dataclass(frozen=True)
class Exemption:
    """A pinned per-file exemption: the exact hit count, its category and why."""

    count: int
    category: str
    reason: str


#: Per-file exemptions, pinned as `LITERAL_PATTERN` occurrence counts, not lines.
EXEMPT_FILES: Final[Mapping[str, Exemption]] = {
    ".github/workflows/verify.yml": Exemption(
        1, "placeholder", "AWS_DEFAULT_REGION for offline CI jobs"
    ),
    ".gitlab-ci.yml": Exemption(1, "placeholder", "AWS_DEFAULT_REGION for offline CI jobs"),
    "config/app.yaml.example": Exemption(1, "example", "example config values"),
    "config/model_capabilities.yaml": Exemption(
        2, "provenance", "region the model capabilities were measured in"
    ),
    "config/models.yaml": Exemption(1, "provenance", "unread `region:` key kept"),
    "config/pricing.yaml": Exemption(3, "provenance", "Price List region the rates were read from"),
    "frontend/scripts/export_response_shapes.py": Exemption(
        1, "placeholder", "region of the in-process moto DynamoDB"
    ),
    "frontend/src/config/runtimeConfig.ts": Exemption(2, "example", "doc-comment examples"),
    "infra/backend.hcl.example": Exemption(1, "example", "example backend config"),
    "infra/bootstrap_account/variables.tf": Exemption(1, "default", "`aws_region` default"),
    "infra/bootstrap_state_backend.sh": Exemption(
        4,
        "default",
        "flag -> AWS_REGION -> default, plus the no-LocationConstraint branch (aws-semantics)",
    ),
    "infra/modules/agentcore/variables.tf": Exemption(1, "default", "`aws_region` default"),
    "infra/terraform.tfvars.example": Exemption(6, "example", "example tfvars and commands"),
    "infra/variables.tf": Exemption(1, "default", "`aws_region` default"),
    "scripts/bootstrap_account.sh": Exemption(2, "example", "usage text example"),
    "scripts/check_canary_tenant_empty.py": Exemption(1, "example", "usage example"),
    "scripts/check_plan_security_baseline.py": Exemption(
        1, "example", "service-reference URL in a comment"
    ),
    "scripts/check_region_literals.py": Exemption(1, "placeholder", "this gate's own pattern"),
    "scripts/consolidate_inventory.py": Exemption(1, "example", "usage example"),
    "scripts/deploy.sh": Exemption(4, "default", "flag -> AWS_REGION -> default"),
    "scripts/preflight_adopted_vpc.sh": Exemption(1, "example", "usage example"),
    "scripts/provision_demo_user.py": Exemption(1, "example", "usage example"),
    "scripts/provision_gateway_principal.py": Exemption(1, "example", "usage example"),
    "scripts/refresh_outputs.sh": Exemption(2, "default", "flag -> AWS_REGION -> default"),
    "scripts/region_map.py": Exemption(1, "default", "the required-region rule"),
    "scripts/render_frontend_config.py": Exemption(2, "example", "usage and help examples"),
    "scripts/residue/global_services.py": Exemption(
        3, "aws-semantics", "the CloudFront/IAM global API is served from this region"
    ),
    "scripts/residue/regional_data.py": Exemption(
        1, "aws-semantics", "a null S3 LocationConstraint means this region"
    ),
    "scripts/smoke/eval_sfn.py": Exemption(2, "placeholder", "row-count placeholder ARNs"),
    "scripts/teardown.sh": Exemption(3, "default", "flag -> AWS_REGION -> default"),
    "scripts/verify_deployment.sh": Exemption(3, "default", "flag -> AWS_REGION -> default"),
    "scripts/verify_security_baseline.sh": Exemption(
        4, "default", "flag -> AWS_REGION -> default, plus one example endpoint comment"
    ),
    "scripts/verify_terraform_plan_with_kms_absent.sh": Exemption(
        4, "placeholder", "plan-time fixture values"
    ),
    "src/agents/runtime_server.py": Exemption(1, "default", "`_DEFAULT_REGION`, env-first"),
    "src/crm/config.py": Exemption(2, "default", "`DEFAULT_REGION` and its docstring"),
    "src/eval/harness.py": Exemption(1, "placeholder", "DynamoDB Local placeholder region"),
    "src/eval/verification.py": Exemption(1, "default", "`DEFAULT_REGION`, env-first"),
}

_CHECKS: Final[tuple[str, ...]] = ("literals", "phrases", "plan", "all")


class GateError(Exception):
    """The gate could not establish its scope or read an input."""


@dataclass
class CheckResult:
    """One check's outcome: rendered lines, whether it failed, and whether it scanned anything."""

    name: str
    lines: list[str]
    failed: bool
    scanned: int


def tracked_files(root: Path) -> list[str]:
    """Tracked paths under `root` that exist as files; raise `GateError` if git cannot list them."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_INDEX_FILE")}
    try:
        listed = proc_run.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, env=env
        )
    except OSError as exc:
        raise GateError(f"could not run git: {exc}") from exc
    if listed.returncode != 0:
        stderr = listed.stderr.decode("utf-8", errors="replace").strip()
        raise GateError(f"git ls-files exited {listed.returncode}: {stderr[:400]}")
    paths = (os.fsdecode(raw) for raw in listed.stdout.split(b"\0") if raw)
    return [path for path in paths if (root / path).is_file()]


def is_class_exempt(path: str) -> bool:
    """Whether `path` falls in one of `EXEMPT_CLASSES`."""
    pure = PurePosixPath(path)
    return any(pure.full_match(glob) for glob, _ in EXEMPT_CLASSES)


def _read(root: Path, path: str) -> str:
    try:
        return (root / path).read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise GateError(f"cannot read {path}: {exc}") from exc


def _hit_lines(text: str, pattern: re.Pattern[str]) -> list[int]:
    return [text.count("\n", 0, match.start()) + 1 for match in pattern.finditer(text)]


def check_literals(
    root: Path, files: Sequence[str], exempt: Mapping[str, Exemption] = EXEMPT_FILES
) -> CheckResult:
    """Compare each scanned file's `LITERAL_PATTERN` hit count with its `EXEMPT_FILES` pin."""
    scanned = [path for path in files if not is_class_exempt(path)]
    if not scanned:
        return CheckResult("literals", ["[SKIP] literals: no tracked file in scope"], False, 0)
    lines: list[str] = []
    for path in scanned:
        hits = _hit_lines(_read(root, path), LITERAL_PATTERN)
        pin = exempt.get(path)
        if pin is None:
            if hits:
                lines.append(
                    f"[FAIL] literals: {path}: {len(hits)} region literal(s) or AZ id(s) at "
                    f"line(s) {sorted(set(hits))}; derive the region, or move AZ ids to "
                    "infra/regions.json"
                )
        elif len(hits) != pin.count:
            lines.append(
                f"[FAIL] literals: {path}: {len(hits)} hit(s) at line(s) {sorted(set(hits))}, "
                f"pinned {pin.count} ({pin.category}); update the code or the pin"
            )
    tracked = set(scanned)
    for path in sorted(set(exempt) - tracked):
        lines.append(f"[FAIL] literals: {path}: pinned in EXEMPT_FILES but not a scanned file")
    for path, pin in sorted(exempt.items()):
        if pin.category not in CATEGORIES:
            lines.append(f"[FAIL] literals: {path}: unknown category {pin.category!r}")
    failed = bool(lines)
    if not failed:
        lines.append(
            f"[PASS] literals: {len(scanned)} tracked file(s) scanned; "
            f"{len(exempt)} pinned exemption(s) match"
        )
    return CheckResult("literals", lines, failed, len(scanned))


def check_phrases(root: Path, files: Sequence[str]) -> CheckResult:
    """Fail on every `BANNED_PHRASE_PATTERN` match in a tracked `*.md`."""
    docs = [path for path in files if path.endswith(".md")]
    if not docs:
        return CheckResult("phrases", ["[SKIP] phrases: no tracked Markdown file"], False, 0)
    lines: list[str] = []
    for path in docs:
        text = _read(root, path)
        for match in BANNED_PHRASE_PATTERN.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            lines.append(f"[FAIL] phrases: {path}:{line}: banned phrase {match.group(0)!r}")
    failed = bool(lines)
    if not failed:
        lines.append(f"[PASS] phrases: {len(docs)} tracked Markdown file(s), no banned phrase")
    return CheckResult("phrases", lines, failed, len(docs))


def _strings(node: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _strings(value, f"{path}.{index}" if path else str(index))


def check_plan(plan_paths: Sequence[Path]) -> CheckResult:
    """Fail on every plan string containing `REGION_LITERAL` outside `PLAN_ALLOWED_PATHS`."""
    lines: list[str] = []
    failed = False
    strings_seen = 0
    for plan_path in plan_paths:
        try:
            document = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GateError(f"cannot read plan {plan_path}: {exc}") from exc
        for json_path, value in _strings(document, ""):
            strings_seen += 1
            if REGION_LITERAL not in value:
                continue
            if any(pattern.fullmatch(json_path) for pattern in PLAN_ALLOWED_PATHS):
                lines.append(f"[ALLOWED] plan: {plan_path}: {json_path} = {value!r}")
            else:
                failed = True
                lines.append(f"[FAIL] plan: {plan_path}: {json_path} = {value!r}")
    if not failed:
        lines.append(
            f"[PASS] plan: {len(plan_paths)} plan(s), {strings_seen} string(s); "
            "no region literal outside PLAN_ALLOWED_PATHS"
        )
    return CheckResult("plan", lines, failed, strings_seen)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="git work tree to scan")
    parser.add_argument(
        "--check", action="append", choices=_CHECKS, help="check to run (repeatable)"
    )
    parser.add_argument(
        "--plan", action="append", type=Path, default=[], help="plan JSON (repeatable)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected checks; return the exit status."""
    args = _parser().parse_args(argv)
    selected = set(args.check or ["all"])
    if "all" in selected:
        selected = {"literals", "phrases"} | ({"plan"} if args.plan else set())
    if "plan" in selected and not args.plan:
        print(
            "check_region_literals: ERROR — --check plan needs at least one --plan", file=sys.stderr
        )
        return 2
    if args.plan and "plan" not in selected:
        print("check_region_literals: ERROR — --plan given but plan not selected", file=sys.stderr)
        return 2
    try:
        results: list[CheckResult] = []
        if selected & {"literals", "phrases"}:
            files = tracked_files(args.root)
            if "literals" in selected:
                results.append(check_literals(args.root, files, EXEMPT_FILES))
            if "phrases" in selected:
                results.append(check_phrases(args.root, files))
        if "plan" in selected:
            results.append(check_plan(args.plan))
    except GateError as exc:
        print(f"check_region_literals: ERROR — {exc}", file=sys.stderr)
        return 2
    for result in results:
        print("\n".join(result.lines))
    if any(result.failed for result in results):
        return 1
    empty = [result.name for result in results if result.scanned == 0]
    if empty:
        print(
            f"[SKIP] check_region_literals: {', '.join(empty)} scanned nothing. Nothing was verified."
        )
        return SKIP_EXIT_STATUS
    return 0


if __name__ == "__main__":
    sys.exit(main())
