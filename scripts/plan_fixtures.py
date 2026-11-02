#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Builds the Terraform plan fixtures from the tracked text base, at test time.

`tests/fixtures/plans/compliant.base.ndjson` holds the compliant plan as JSON lines: line 1 is a
header object (`_fixture_provenance`, `format_version`, `configuration`, `variables`), then one
`resource_changes` element per line. `build(name)` reassembles a plan document from it and applies
that variant's mutation; `materialize(name)` writes the document to a JSON file and returns its path.

Usage:
    python3 scripts/plan_fixtures.py [--out DIR] [NAME ...]   # every name when none is given

Names: see `NAMES`. An unknown name, or a base that no longer carries what a mutation edits, raises
`ValueError`.

`infra_digest(root)` is the content digest of the tracked `infra/**/*.tf` that a capture records as
`_fixture_provenance.captured_infra_digest` and that `plan_freshness` recomputes.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import proc_run  # noqa: E402

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
BASE_PATH: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "plans" / "compliant.base.ndjson"
DEFAULT_OUT_DIR: Final[Path] = REPO_ROOT / "results" / "plan-fixtures"

#: Top-level fields kept out of the base so no scanner reads it as a plan; restored by `build`.
TERRAFORM_VERSION: Final[str] = "1.17.0"
TIMESTAMP: Final[str] = "2026-10-23T03:32:29Z"

HEADER_KEYS: Final[frozenset[str]] = frozenset(
    {"_fixture_provenance", "format_version", "configuration", "variables"}
)

GATEWAY_ROLE_ARN: Final[str] = "arn:aws:iam::123456789012:role/crm-agent-eval-gateway-role"
_GATEWAY_ROLE: Final[str] = "aws_iam_role.agentcore_gateway"
_GATEWAY_GRANT = re.compile(
    r'module\.lambda_action(\["[a-z_]+"\]|_interceptor|_pre_token_generation)'
    r"\.aws_lambda_permission\.gateway_target_role\[0\]"
)
_FLOW_LOGS: Final[str] = "module.vpc.aws_cloudwatch_log_group.flow_logs[0]"
_RELAY_URL: Final[str] = "module.agent_relay.aws_lambda_function_url.this"

Plan = dict[str, Any]

#: `git ls-files` pathspecs selecting the tracked Terraform `infra_digest` covers.
INFRA_TF_PATHSPECS: Final[tuple[str, ...]] = ("infra/*.tf", "infra/**/*.tf")
_GIT_TIMEOUT_SECONDS: Final[float] = 20.0


def infra_digest(root: Path) -> str:
    """Return the sha256 hex digest of every tracked `infra/**/*.tf` under the git work tree `root`.

    The digest is taken over the sorted lines `f"{path}\\0{sha256(bytes)}\\n"`, one per path that
    `git ls-files` lists (the index, never history; untracked files are excluded), with the bytes
    read from the working tree. A tracked path missing from the working tree contributes no line.

    Raises:
        ValueError: `git ls-files` fails or lists no `.tf` file.
    """
    try:
        completed = proc_run.run(
            ["git", "--no-pager", "ls-files", "-z", "--", *INFRA_TF_PATHSPECS],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"`git ls-files` could not run in {root}: {exc}") from exc
    if completed.returncode != 0:
        raise ValueError(f"`git ls-files` exited {completed.returncode} in {root}")
    paths = sorted({p.decode("utf-8") for p in completed.stdout.split(b"\0") if p})
    if not paths:
        raise ValueError(f"`git ls-files` lists no tracked infra .tf file in {root}")
    digest = hashlib.sha256()
    for path in paths:
        try:
            content = (root / path).read_bytes()
        except FileNotFoundError:
            continue
        digest.update(f"{path}\0{hashlib.sha256(content).hexdigest()}\n".encode())
    return digest.hexdigest()


def _then(mutation: str, earlier: object) -> str:
    """`mutation`, followed by the base's own `mutation` text when it has one."""
    return f"{mutation} Then: {earlier}" if isinstance(earlier, str) and earlier else mutation


def _change(plan: Plan, address: str) -> dict[str, Any]:
    matches = [rc["change"] for rc in plan["resource_changes"] if rc["address"] == address]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one resource_change {address!r}, found {len(matches)}")
    change: dict[str, Any] = matches[0]
    return change


def _known_gateway_role_arn(plan: Plan) -> None:
    role = _change(plan, _GATEWAY_ROLE)
    if role["after_unknown"].pop("arn", None) is not True:
        raise ValueError(f"{_GATEWAY_ROLE} arn is not plan-time unknown in the base")
    role["after"]["arn"] = GATEWAY_ROLE_ARN
    grants = [
        rc["change"] for rc in plan["resource_changes"] if _GATEWAY_GRANT.fullmatch(rc["address"])
    ]
    if not grants:
        raise ValueError("no Gateway #1 target-role grant found in the base")
    for change in grants:
        if change["after_unknown"].pop("principal", None) is not True:
            raise ValueError("a Gateway #1 target-role grant's principal is not plan-time unknown")
        change["after"]["principal"] = GATEWAY_ROLE_ARN
    plan["_fixture_provenance"]["how"] = (
        "Derived from compliant.json (same recapture) with the known-ARN branch applied: "
        "aws_iam_role.agentcore_gateway.arn is a KNOWN literal and every "
        "AllowGatewayTargetRoleInvoke permission whose principal is that role (every tool, the "
        "interceptor and pre-token-generation) carries that same literal, exercising the 'known "
        "literal that exactly matches the plan's own' branch of lambda_resource_policy_no_wildcard. "
        "The chat Gateway's two grants (chat_interceptor, chat_target) keep their plan-time-unknown "
        "principal. All other content is compliant.json's."
    )


def _log_group_no_kms(plan: Plan) -> None:
    change = _change(plan, _FLOW_LOGS)
    if change["after_unknown"].get("kms_key_id") is not True or "kms_key_id" in change["after"]:
        raise ValueError(f"{_FLOW_LOGS} kms_key_id is not plan-time unknown in the base")
    change["after"]["kms_key_id"] = None
    change["after_unknown"]["kms_key_id"] = False
    base = plan["_fixture_provenance"]
    plan["_fixture_provenance"] = {
        "derived_from": "tests/fixtures/plans/compliant.json",
        "captured_infra_digest": base["captured_infra_digest"],
        "captured_at": base["captured_at"],
        "mutation": _then(
            f"{_FLOW_LOGS}: kms_key_id set to null in change.after, and "
            "change.after_unknown.kms_key_id set to false, so the log group plans with no "
            "customer-managed key. Every other resource is unchanged from compliant.json.",
            base.get("mutation"),
        ),
    }


def _relay_function_url_buffered(plan: Plan) -> None:
    change = _change(plan, _RELAY_URL)
    if change["after"].get("invoke_mode") != "RESPONSE_STREAM":
        raise ValueError(f"{_RELAY_URL} invoke_mode is not RESPONSE_STREAM in the base")
    change["after"]["invoke_mode"] = "BUFFERED"
    provenance = plan["_fixture_provenance"]
    provenance["derived_from"] = "compliant.json"
    provenance["mutation"] = _then(
        "aws_lambda_function_url.invoke_mode RESPONSE_STREAM -> BUFFERED in resource_changes, so the fixture fails relay_function_url_streams on the buffered mode "
        "itself rather than on the two views disagreeing.",
        provenance.get("mutation"),
    )


_MUTATIONS: Final[Mapping[str, Callable[[Plan], None] | None]] = {
    "compliant": None,
    "compliant_gateway_role_known_arn": _known_gateway_role_arn,
    "violation_log_group_no_kms": _log_group_no_kms,
    "violation_relay_function_url_buffered": _relay_function_url_buffered,
}

NAMES: Final[tuple[str, ...]] = tuple(_MUTATIONS)


@functools.cache
def _base_lines() -> tuple[str, ...]:
    return tuple(BASE_PATH.read_text(encoding="utf-8").splitlines())


def build(name: str) -> dict[str, Any]:
    """Return a fresh plan document for `name`; callers may mutate it freely.

    Raises:
        ValueError: `name` is not in `NAMES`, the base header is malformed, or the base no longer
            carries what the variant's mutation edits.
    """
    if name not in _MUTATIONS:
        raise ValueError(f"unknown plan fixture {name!r}; expected one of {list(NAMES)}")
    header, *changes = (json.loads(line) for line in _base_lines() if line)
    if set(header) != HEADER_KEYS:
        raise ValueError(f"{BASE_PATH.name} header keys {sorted(header)} != {sorted(HEADER_KEYS)}")
    plan: Plan = {
        "_fixture_provenance": header["_fixture_provenance"],
        "format_version": header["format_version"],
        "terraform_version": TERRAFORM_VERSION,
        "variables": header["variables"],
        "resource_changes": changes,
        "configuration": header["configuration"],
        "timestamp": TIMESTAMP,
    }
    mutation = _MUTATIONS[name]
    if mutation is not None:
        mutation(plan)
    return plan


def materialize(name: str, out_dir: Path | None = None) -> Path:
    """Write `build(name)` to `<out_dir>/<name>.json` (default `DEFAULT_OUT_DIR`) and return it.

    The write is atomic (temporary file, then a rename) and skipped when the file already holds
    the same bytes, so concurrent callers never observe a partial file.
    """
    directory = out_dir if out_dir is not None else DEFAULT_OUT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.json"
    content = (json.dumps(build(name), indent=2) + "\n").encode("utf-8")
    digest = hashlib.sha256(content).digest()
    try:
        if hashlib.sha256(target.read_bytes()).digest() == digest:
            return target
    except FileNotFoundError:
        pass
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        Path(temporary).replace(target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """Materialize the named fixtures (all by default) and print one path per line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="output directory")
    parser.add_argument("names", nargs="*", help=f"fixture names (default: {' '.join(NAMES)})")
    args = parser.parse_args(argv)
    unknown = sorted(set(args.names) - set(NAMES))
    if unknown:
        parser.error(f"unknown fixture name(s) {unknown}; expected some of {list(NAMES)}")
    for name in args.names or NAMES:
        print(materialize(name, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
