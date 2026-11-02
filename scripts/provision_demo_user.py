#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Provision one Cognito user that can sign in to the deployed SPA and see populated data.

It does three things:

1. Creates (or reuses) a Cognito user in the deployed pool with a permanent password.
2. Writes a ``reps`` item keyed by that user's **Cognito username**, carrying
   ``tenant``/``book``/``full_visibility``. The pre-token-generation trigger
   (``src/crm/auth/pre_token_generation.py``) looks the item up by ``event["userName"]``; with no
   item the user gets an empty book and no full visibility. The pool sets
   ``username_attributes = ["email"]``, so the username is a pool-assigned UUID read back from
   Cognito, not the email.
3. Seeds the ``demo`` tenant via the idempotent ``fixtures.generate.seed_tenant``, unless
   ``--skip-seed``.
4. With ``--admin``, adds the user to the admin group (``crm.authz.ADMIN_GROUP``, which the
   ``admin_group_name`` output must equal when the outputs document carries it).

Usage:
    export CRM_DEMO_USER_PASSWORD='...'
    terraform -chdir=infra output -json | scripts/provision_demo_user.py \\
        --email demo@example.test --region us-east-1 --profile default

The resolved account and region are checked against the outputs document before anything is
written. Re-running is safe: an existing user is reused, the password is reset, the ``reps`` item is
overwritten, and the seeder is idempotent. The password is read from ``CRM_DEMO_USER_PASSWORD``,
never from an argument.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Make `crm` (under `src/`) and `fixtures` (at the repo root) importable when run directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "src"):  # pragma: no cover - import bootstrap
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from crm.authz import ADMIN_GROUP, DEMO_TENANT  # noqa: E402
from crm.store.store import Store  # noqa: E402

PASSWORD_ENV_VAR = "CRM_DEMO_USER_PASSWORD"  # noqa: S105 - an environment variable name, not a credential  # nosec B105  # pragma: allowlist secret

#: The address ``tests/e2e/browser/fixtures/auth.ts`` signs in with. :func:`main` refuses to run when
#: it is set and differs from ``--email``.
EMAIL_ENV_VAR = "CRM_DEMO_USER_EMAIL"

#: The ``name`` :func:`write_rep` gives a ``reps`` row unless told otherwise.
DEMO_USER_REP_NAME = "Demo UI user"


def table_prefix_from_outputs(outputs: dict[str, Any]) -> str:
    """Derive the live DynamoDB table-name prefix from the ``dynamodb_table_names`` output.

    The physical name of the ``eval_runs`` logical table is ``<prefix>eval_runs``; stripping that
    suffix recovers the prefix.
    """
    names = _output_value(outputs, "dynamodb_table_names")
    if not isinstance(names, dict) or "eval_runs" not in names:
        raise ValueError(
            "terraform output 'dynamodb_table_names' is not a mapping containing 'eval_runs'; "
            f"got {names!r}"
        )
    return str(names["eval_runs"]).removesuffix("eval_runs")


def _output_value(outputs: dict[str, Any], name: str) -> Any:
    """Return one ``terraform output -json`` entry's value, unwrapping the ``{"value": ...}`` form."""
    if name not in outputs:
        raise KeyError(
            f"terraform output {name!r} is absent. Has `terraform apply` completed for this "
            f"workspace? Present outputs: {sorted(outputs)}"
        )
    entry = outputs[name]
    return entry.get("value") if isinstance(entry, dict) else entry


def _require_str(outputs: dict[str, Any], name: str) -> str:
    value = _output_value(outputs, name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"terraform output {name!r} is {value!r}; expected a non-empty string")
    return value


def ensure_cognito_user(idp: Any, *, user_pool_id: str, email: str, password: str) -> str:
    """Create (or reuse) the pool user for ``email`` and return its Cognito username.

    The returned username is the pool-assigned identifier (a UUID under
    ``username_attributes = ["email"]``), which the pre-token-generation trigger reports as
    ``rep_id``. Raises ``RuntimeError`` when Cognito returns no username.
    """
    try:
        created = idp.admin_create_user(
            UserPoolId=user_pool_id,
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        username = str((created.get("User") or {}).get("Username") or "")
    except idp.exceptions.UsernameExistsException:
        existing = idp.admin_get_user(UserPoolId=user_pool_id, Username=email)
        username = str(existing.get("Username") or "")

    if not username:
        raise RuntimeError(
            f"Cognito returned no Username for {email!r}; refusing to key a reps item on a "
            "guessed identifier, which would leave the signed-in user with an empty book"
        )

    idp.admin_set_user_password(
        UserPoolId=user_pool_id, Username=username, Password=password, Permanent=True
    )
    return username


def ensure_group_membership(idp: Any, *, user_pool_id: str, username: str, group: str) -> None:
    """Add ``username`` to ``group``; Cognito accepts a repeat add, so a re-run is a no-op."""
    idp.admin_add_user_to_group(UserPoolId=user_pool_id, Username=username, GroupName=group)


def admin_group_from_outputs(outputs: dict[str, Any]) -> str:
    """The group ``--admin`` joins: :data:`crm.authz.ADMIN_GROUP`, checked against the outputs.

    Raises ``ValueError`` when the outputs carry an ``admin_group_name`` that is not that group.
    """
    if "admin_group_name" in outputs:
        named = _output_value(outputs, "admin_group_name")
        if named != ADMIN_GROUP:
            raise ValueError(
                f"terraform output 'admin_group_name' is {named!r}, but crm.authz grants admin to "
                f"members of {ADMIN_GROUP!r}; joining the output's group would grant nothing"
            )
    return ADMIN_GROUP


def write_rep(
    store: Store,
    *,
    rep_id: str,
    tenant: str,
    book: list[str],
    full_visibility: bool,
    name: str = DEMO_USER_REP_NAME,
) -> dict[str, Any]:
    """Write the ``reps`` item the trigger reads, and return it.

    ``full_visibility`` must be a real ``bool`` and ``book`` a list of strings: the trigger's
    ``_coerce_full_visibility`` treats anything that is not literally ``True`` as ``False``,
    and ``_coerce_book`` falls back to an empty book for a non-sequence. ``name`` is the row's
    display name.
    """
    item: dict[str, Any] = {
        "rep_id": rep_id,
        "tenant": tenant,
        "book": sorted(book),
        "full_visibility": full_visibility,
        "name": name,
        "status": "active",
    }
    store.put_item("reps", item)
    return item


def provision(
    *,
    idp: Any,
    store: Store,
    user_pool_id: str,
    email: str,
    password: str,
    book: list[str],
    full_visibility: bool,
    seeder: Any,
    admin_group: str | None = None,
) -> dict[str, Any]:
    """Seed the demo tenant, create the user, and bind the two together via a ``reps`` item.

    Seeding runs first, when a ``seeder`` is given. With ``admin_group``, the user joins that group.
    """
    if seeder is not None:
        seeder(store, tenant=DEMO_TENANT)
    username = ensure_cognito_user(idp, user_pool_id=user_pool_id, email=email, password=password)
    rep = write_rep(
        store,
        rep_id=username,
        tenant=DEMO_TENANT,
        book=book,
        full_visibility=full_visibility,
    )
    if admin_group is not None:
        ensure_group_membership(
            idp, user_pool_id=user_pool_id, username=username, group=admin_group
        )
    return {"username": username, "email": email, "rep": rep, "admin_group": admin_group}


def _default_seeder() -> Any:
    """Resolve ``fixtures.generate.seed_tenant`` lazily so importing this module is side-effect free."""
    from fixtures.generate import seed_tenant

    return seed_tenant


class TargetMismatchError(RuntimeError):
    """The resolved account or region is not the one the outputs describe."""


def assert_target_matches_outputs(
    session: Any,
    *,
    outputs: dict[str, Any],
    region: str | None,
    profile: str | None,
) -> None:
    """Raise :class:`TargetMismatchError` unless the session's account and region match ``outputs``.

    The expected regions and accounts are derived from the ARNs in ``outputs`` via
    ``scripts.smoke.harness``. An outputs document naming no account skips the account check.
    """
    from scripts.smoke.harness import TerraformOutputs, outputs_accounts, outputs_regions

    described = TerraformOutputs(values=dict(outputs))
    expected_regions = outputs_regions(described)
    if region and expected_regions and region not in expected_regions:
        raise TargetMismatchError(
            f"resolved region {region!r}, but these outputs describe "
            f"{', '.join(sorted(expected_regions))}. Nothing has been written. Pass --region "
            f"explicitly, or unset AWS_REGION."
        )
    expected_accounts = outputs_accounts(described)
    if not expected_accounts:
        return
    actual = str(session.client("sts").get_caller_identity().get("Account", ""))
    if actual and actual not in expected_accounts:
        raise TargetMismatchError(
            f"resolved account {actual} in region {region!r} via profile {profile!r}, but these "
            f"outputs describe account {', '.join(sorted(expected_accounts))}. Nothing has been "
            f"written. Pass --profile explicitly, or unset AWS_PROFILE."
        )


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outputs-json",
        type=Path,
        help="File holding `terraform output -json` (default: read stdin)",
    )
    parser.add_argument("--email", required=True, help="Sign-in email for the demo user")
    parser.add_argument(
        "--region", help="AWS region for the Cognito and DynamoDB clients (default: AWS_REGION)"
    )
    parser.add_argument(
        "--profile",
        help=(
            "AWS profile for the Cognito and DynamoDB clients (default: AWS_PROFILE). This script "
            "WRITES the demo tenant and creates a Cognito user, and before this flag existed it ran "
            "on wholly ambient credentials with no way for an operator to name the target."
        ),
    )
    parser.add_argument(
        "--book",
        default="",
        help=(
            "Comma-separated account ids to grant. Empty (the default) pairs with "
            "--full-visibility to grant the whole demo tenant."
        ),
    )
    parser.add_argument(
        "--no-full-visibility",
        dest="full_visibility",
        action="store_false",
        help="Grant only --book instead of the whole demo tenant.",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Do not seed the demo tenant (it is already seeded).",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Also add the user to the admin group (idempotent).",
    )
    parser.set_defaults(full_visibility=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    password = os.environ.get(PASSWORD_ENV_VAR, "")
    if not password.strip():
        print(
            f"error: {PASSWORD_ENV_VAR} is unset or empty. Export the demo user's password "
            "there rather than passing it as an argument.",
            file=sys.stderr,
        )
        return 1

    # Exact comparison, not case-folded.
    journeys_email = os.environ.get(EMAIL_ENV_VAR, "").strip()
    if journeys_email and journeys_email != args.email.strip():
        print(
            f"error: --email is {args.email.strip()!r} but {EMAIL_ENV_VAR} is {journeys_email!r}. "
            "The browser journeys sign in with the environment variable and nothing else reads it, "
            "so provisioning a different address fails at sign-in with a credentials error that "
            f"names neither cause. Make them identical, or unset {EMAIL_ENV_VAR} if you are "
            "provisioning without running the journeys.",
            file=sys.stderr,
        )
        return 1

    raw = args.outputs_json.read_text(encoding="utf-8") if args.outputs_json else sys.stdin.read()
    try:
        outputs = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: could not parse terraform outputs as JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(outputs, dict):
        print(
            "error: expected a JSON object from `terraform output -json`, got "
            f"{type(outputs).__name__}",
            file=sys.stderr,
        )
        return 1

    book = [part.strip() for part in args.book.split(",") if part.strip()]
    if not book and not args.full_visibility:
        print(
            "error: --no-full-visibility with an empty --book would provision a user who can "
            "see nothing. Pass --book, or drop --no-full-visibility.",
            file=sys.stderr,
        )
        return 1

    try:
        user_pool_id = _require_str(outputs, "cognito_user_pool_id")
        prefix = table_prefix_from_outputs(outputs)
        admin_group = admin_group_from_outputs(outputs) if args.admin else None
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    import boto3

    region = args.region or os.environ.get("AWS_REGION")
    profile = args.profile or os.environ.get("AWS_PROFILE")
    session = boto3.session.Session(profile_name=profile, region_name=region)
    try:
        assert_target_matches_outputs(session, outputs=outputs, region=region, profile=profile)
    except TargetMismatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    result = provision(
        idp=session.client("cognito-idp"),
        store=Store(client=session.client("dynamodb"), table_prefix=prefix),
        user_pool_id=user_pool_id,
        email=args.email,
        password=password,
        book=book,
        full_visibility=args.full_visibility,
        seeder=None if args.skip_seed else _default_seeder(),
        admin_group=admin_group,
    )

    print(
        f"provisioned {result['email']} in {user_pool_id}\n"
        f"  cognito username / rep_id: {result['username']}\n"
        f"  tenant: {result['rep']['tenant']}  "
        f"full_visibility: {result['rep']['full_visibility']}  "
        f"book: {result['rep']['book'] or '(all, via full_visibility)'}\n"
        f"  admin group: {result['admin_group'] or '(none)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
