#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Write the ``reps`` item of the AgentCore Gateway service principal ``svc-mcp-gateway``.

External MCP ``client_credentials`` tokens carry no ``custom:rep_id``; the Gateway interceptor
(``src/gateway/identity_interceptor.py``) maps them to this rep and reads its ``book`` and
``tenant`` from the item written here. The item is: tenant ``demo``, book = every account id the
``demo`` seed writes (derived from the seed generator), ``full_visibility`` false, status active,
name "MCP gateway service principal".

Usage:
    terraform -chdir=infra output -json | scripts/provision_gateway_principal.py \\
        --outputs-json - --profile NAME --region us-east-1 [--table-prefix PREFIX]

The resolved account and region are checked against the outputs document before anything is
written. Re-running overwrites the item with identical content.

Exit status: ``0`` written; ``1`` refused or failed (nothing written unless the message says so);
``2`` usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "src"):  # pragma: no cover - import bootstrap
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fixtures.demo_seed import demo_account_ids  # noqa: E402
from scripts.provision_demo_user import (  # noqa: E402
    TargetMismatchError,
    assert_target_matches_outputs,
    table_prefix_from_outputs,
    write_rep,
)

from crm.authz import DEMO_TENANT  # noqa: E402
from crm.errors import CrmError  # noqa: E402
from crm.store.store import Store  # noqa: E402

#: Must equal ``local.gateway_service_principal_rep_id`` in ``infra/main.tf``.
GATEWAY_SERVICE_PRINCIPAL_REP_ID: Final[str] = "svc-mcp-gateway"

#: The ``name`` of the service principal's ``reps`` row.
GATEWAY_SERVICE_PRINCIPAL_NAME: Final[str] = "MCP gateway service principal"


def demo_book() -> list[str]:
    """Every account id the ``demo`` seed writes, sorted: the generated corpus plus the empty account."""
    return demo_account_ids(DEMO_TENANT)


def provision_gateway_principal(store: Store, *, tenant: str = DEMO_TENANT) -> dict[str, Any]:
    """Write the service principal's ``reps`` item into ``store`` and return it.

    Raises:
        ValueError: ``tenant`` is not :data:`crm.authz.DEMO_TENANT`; nothing is written.
    """
    if tenant != DEMO_TENANT:
        raise ValueError(
            f"the gateway service principal is provisioned only in tenant {DEMO_TENANT!r}, "
            f"not {tenant!r}; nothing was written"
        )
    return write_rep(
        store,
        rep_id=GATEWAY_SERVICE_PRINCIPAL_REP_ID,
        tenant=tenant,
        book=demo_book(),
        full_visibility=False,
        name=GATEWAY_SERVICE_PRINCIPAL_NAME,
    )


def resolve_table_prefix(outputs: dict[str, Any], flag: str | None) -> str:
    """The table prefix the outputs describe; ``flag``, when given, must agree with it.

    Raises:
        ValueError: the outputs carry no ``dynamodb_table_names`` and no ``flag`` was given, the
            output is malformed, or ``flag`` differs from the prefix the outputs describe.
    """
    try:
        derived = table_prefix_from_outputs(outputs)
    except KeyError:
        if flag is None:
            raise ValueError(
                "the outputs carry no 'dynamodb_table_names' and no --table-prefix was given"
            ) from None
        return flag
    if flag is not None and flag != derived:
        raise ValueError(
            f"--table-prefix {flag!r} differs from the prefix the outputs describe ({derived!r})"
        )
    return derived


def assert_outputs_name_target(outputs: dict[str, Any]) -> None:
    """Raise :class:`TargetMismatchError` unless ``outputs`` names at least one account and region.

    ``assert_target_matches_outputs`` skips a check the outputs give it nothing to compare against;
    for a write that is refused instead.
    """
    from scripts.smoke.harness import TerraformOutputs, outputs_accounts, outputs_regions

    described = TerraformOutputs(values=dict(outputs))
    if not outputs_accounts(described) or not outputs_regions(described):
        raise TargetMismatchError(
            "the outputs document names no account or no region in any ARN, so the target cannot "
            "be verified. Nothing has been written."
        )


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outputs-json",
        required=True,
        help="File holding `terraform output -json`, or '-' for stdin",
    )
    parser.add_argument("--profile", required=True, help="AWS profile for the DynamoDB write")
    parser.add_argument("--region", required=True, help="AWS region for the DynamoDB write")
    parser.add_argument(
        "--table-prefix",
        help="DynamoDB table prefix (default: derived from the outputs; must agree with them)",
    )
    return parser


def _read_outputs(source: str) -> dict[str, Any]:
    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    outputs = json.loads(raw)
    if not isinstance(outputs, dict):
        raise ValueError(
            f"expected a JSON object from `terraform output -json`, got {type(outputs).__name__}"
        )
    return outputs


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        outputs = _read_outputs(args.outputs_json)
        prefix = resolve_table_prefix(outputs, args.table_prefix)
        assert_outputs_name_target(outputs)
    except (OSError, ValueError, TargetMismatchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        session = boto3.session.Session(profile_name=args.profile, region_name=args.region)
        assert_target_matches_outputs(
            session, outputs=outputs, region=args.region, profile=args.profile
        )
    except (BotoCoreError, ClientError, TargetMismatchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        rep = provision_gateway_principal(
            Store(client=session.client("dynamodb"), table_prefix=prefix)
        )
    except (BotoCoreError, ClientError, CrmError) as exc:
        print(f"error: writing the reps item failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"wrote reps item {rep['rep_id']} in {prefix}reps\n"
        f"  tenant: {rep['tenant']}  full_visibility: {rep['full_visibility']}  "
        f"status: {rep['status']}  book: {len(rep['book'])} account(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
