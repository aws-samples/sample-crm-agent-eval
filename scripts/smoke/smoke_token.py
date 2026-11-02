#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Mint a `CRM_SMOKE_TOKEN` bound to the orchestrator's own smoke tenant.

`scripts/verify_deployment.sh` allocates and seeds one `eval-smoke-<hex>` tenant per run and threads
`--token` to every module that declares it; those modules write beneath the token's `custom:tenant`
claim. This module binds `scripts.smoke.cognito_claims.LiveSmokeUserProvisioner` to that tenant and
raises, stopping the run, when the minted claim is not this run's tenant.

Usage:
    python3 -m scripts.smoke.smoke_token mint --run-id ID --tenant TENANT [--profile P] [--region R]
                                             [--outputs PATH]
    python3 -m scripts.smoke.smoke_token delete-user --username U [--profile P] [--region R]
                                             [--outputs PATH]

`mint` prints one JSON object (`id_token`, `username`, `email`, `tenant`) on stdout and every
diagnostic on stderr. `delete-user` removes the user from the caller's trap, in a separate process,
so the username travels through the caller.

The tenant check is equality against this run's tenant, not a disposable-prefix check: a token
minted with no `reps` row carries the `demo` fallback tenant.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Final

from scripts.smoke.cognito_claims import LiveSmokeUserProvisioner, _live_decode_for
from scripts.smoke.harness import SmokeContext, smoke_context
from scripts.smoke.tenant import SmokeTenant, is_smoke_run_id

from crm.authz import DEMO_TENANT

#: The claim the CRM handlers scope every write by (`crm.envelope.CLAIM_TENANT`).
TENANT_CLAIM: Final[str] = "custom:tenant"


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A real ID token proven to carry the run's own tenant.

    Attributes:
        id_token: The token to pass as `--token`.
        username: The Cognito `Username`, needed by `delete-user` in the caller's trap.
        email: The sign-in address the provisioner created, for diagnostics only.
        tenant: The tenant the token's `custom:tenant` claim actually carries.
    """

    id_token: str
    username: str
    email: str
    tenant: str


def assert_token_tenant(claims: dict[str, Any], *, expected: str) -> str:
    """Return the token's tenant claim, raising unless it equals ``expected``.

    Args:
        claims: Decoded ID-token claims.
        expected: The tenant this run allocated, seeded, and will tear down.

    Returns:
        The asserted tenant.

    Raises:
        RuntimeError: The claim is absent, is ``demo``, or names any tenant other than ``expected``.
    """
    actual = str(claims.get(TENANT_CLAIM) or "").strip()
    if not actual:
        raise RuntimeError(
            f"the minted token carries no {TENANT_CLAIM!r} claim, so every module would write "
            "beneath an unknown tenant. Refusing to hand it on."
        )
    if actual == DEMO_TENANT:
        raise RuntimeError(
            f"the minted token names the {DEMO_TENANT!r} tenant. This is what an absent `reps` row "
            "produces, because crm.auth.pre_token_generation falls back to it — the user's row was "
            "not written, or was written after the mint. Refusing to hand it on."
        )
    if actual != expected:
        raise RuntimeError(
            f"the minted token names tenant {actual!r} but this run owns {expected!r}. Rows written "
            "beneath the former would not be reaped by this run's teardown. Refusing to hand it on."
        )
    return actual


def mint_run_token(ctx: SmokeContext, smoke_tenant: SmokeTenant) -> MintedToken:
    """Provision a smoke user beneath ``smoke_tenant`` and return its asserted token.

    Args:
        ctx: The smoke context, already asserted against the outputs document.
        smoke_tenant: The tenant the caller allocated and seeded, and whose teardown it owns.

    Returns:
        The minted token, its claim already checked against ``smoke_tenant.tenant``.

    Raises:
        RuntimeError: The run id is not a smoke run id, provisioning failed, or the claim check
            refused. On a refusal the user is removed before the exception leaves.
    """
    if not is_smoke_run_id(smoke_tenant.run_id):
        raise RuntimeError(
            f"run id {smoke_tenant.run_id!r} is not a smoke run id, so this tenant was not "
            "allocated by scripts.smoke.tenant and its teardown owner is unknown"
        )

    provisioner = LiveSmokeUserProvisioner(ctx, smoke_tenant=smoke_tenant)
    try:
        # `provision` creates the Cognito user and `reps` row before its own tenant check runs.
        provisioned = provisioner.provision()
        # `_live_decode_for` derives the JWKS URL from `ctx` and verifies the signature.
        claims = _live_decode_for(ctx)(provisioned.id_token)
        tenant = assert_token_tenant(claims, expected=smoke_tenant.tenant)
    except Exception:
        # `cleanup` removes the user and skips the caller's injected tenant.
        for result in provisioner.cleanup():
            print(
                f"cleanup after refusal: {result.check}={result.status} {result.detail}",
                file=sys.stderr,
            )
        raise

    return MintedToken(
        id_token=provisioned.id_token,
        username=provisioned.username,
        email=provisioner._email or provisioned.username,
        tenant=tenant,
    )


def assert_supplied_token(ctx: SmokeContext, *, id_token: str, expected_tenant: str) -> str:
    """Decode a caller-supplied token and assert it names ``expected_tenant``.

    The `--token`/`CRM_SMOKE_TOKEN` half of the guard. The signature and expiry are verified.

    Args:
        ctx: The smoke context, used to derive the pool JWKS URL.
        id_token: The token the caller supplied.
        expected_tenant: The tenant this run allocated and will tear down.

    Returns:
        The asserted tenant.

    Raises:
        RuntimeError: The token could not be decoded, or names any other tenant.
    """
    try:
        claims = _live_decode_for(ctx)(id_token)
    except Exception as exc:
        raise RuntimeError(
            f"the supplied token could not be decoded or verified against the pool: "
            f"{type(exc).__name__}: {exc}. A token that cannot be verified cannot be shown to name "
            "this run's tenant, so it is refused rather than passed on."
        ) from exc
    return assert_token_tenant(claims, expected=expected_tenant)


def delete_smoke_user(ctx: SmokeContext, username: str) -> int:
    """Remove the smoke Cognito user and its ``reps`` row, proving each deletion by read-back.

    Returns 0 when every removal was confirmed and 1 otherwise, and never raises: it runs from the
    caller's `EXIT`/`INT`/`TERM` trap.

    Args:
        ctx: The smoke context.
        username: The Cognito `Username` returned by ``mint``.
    """
    provisioner = LiveSmokeUserProvisioner.for_existing_user(ctx, username=username)
    failed = False
    for result in provisioner.cleanup():
        print(f"{result.resource}: {result.check}={result.status} {result.detail}", file=sys.stderr)
        if result.status == "fail":
            failed = True
    return 1 if failed else 0


def _context(args: argparse.Namespace) -> SmokeContext:
    return smoke_context(profile=args.profile, region=args.region, outputs=args.outputs)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. See the module docstring for the two subcommands."""
    parser = argparse.ArgumentParser(prog="scripts.smoke.smoke_token", description=__doc__)
    parser.add_argument("--profile", default=None, help="AWS profile (no ambient default)")
    parser.add_argument("--region", default=None, help="AWS region (no ambient default)")
    parser.add_argument("--outputs", default=None, help="Path to the terraform outputs document")
    sub = parser.add_subparsers(dest="command", required=True)

    mint = sub.add_parser("mint", help="provision a smoke user and print its asserted token")
    mint.add_argument("--run-id", required=True)
    mint.add_argument("--tenant", required=True)

    delete = sub.add_parser("delete-user", help="remove the smoke user and its reps row")
    delete.add_argument("--username", required=True)

    check = sub.add_parser(
        "assert-token", help="verify a caller-supplied token names this run's tenant"
    )
    check.add_argument("--token", required=True)
    check.add_argument("--tenant", required=True)

    args = parser.parse_args(argv)
    ctx = _context(args)

    if args.command == "assert-token":
        assert_supplied_token(ctx, id_token=args.token, expected_tenant=args.tenant)
        print(json.dumps({"tenant": args.tenant}))
        return 0

    if args.command == "mint":
        minted = mint_run_token(ctx, SmokeTenant(run_id=args.run_id, tenant=args.tenant))
        # stdout carries the JSON and nothing else, so the caller can capture it directly.
        print(
            json.dumps(
                {
                    "id_token": minted.id_token,
                    "username": minted.username,
                    "email": minted.email,
                    "tenant": minted.tenant,
                }
            )
        )
        return 0

    return delete_smoke_user(ctx, args.username)


__all__ = [
    "TENANT_CLAIM",
    "MintedToken",
    "assert_supplied_token",
    "assert_token_tenant",
    "delete_smoke_user",
    "main",
    "mint_run_token",
]


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
