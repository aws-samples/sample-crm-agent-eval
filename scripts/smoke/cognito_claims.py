# SPDX-License-Identifier: MIT
"""Layer 4 smoke script: asserts Cognito pool hardening and that the V2 trigger stamps claims.

Needs a live deployment. Checks `describe_user_pool` for MFA configuration, password policy,
account recovery, and `AllowAdminCreateUserOnly`; makes a real unauthenticated `SignUp` call with the
SPA client id and asserts it is refused (deleting and read-back-confirming the user if it is not);
checks that the pre-token-generation trigger is wired as `LambdaVersion`
`V2_0` (never the legacy V1 `PreTokenGeneration` config); then authenticates a smoke-tenant test
user, decodes the real issued ID token, and asserts `custom:rep_id`, `custom:book`, and
`custom:full_visibility` are present in it.

The SPA client must have token revocation enabled (`describe_user_pool_client`). For the provisioned
smoke user, the refresh token its sign-in issued is revoked with `RevokeToken`, and a following
`InitiateAuth REFRESH_TOKEN_AUTH` with it must be refused with `NotAuthorizedException`.

The Hosted UI sign-out endpoint is probed on the pool's own domain: `GET /logout` with the SPA client
and its first registered `LogoutURLs` entry must answer 302 to exactly that URL
(`hosted_ui_logout_registered_redirects`), and with an unregistered `logout_uri` must not redirect to
it (`hosted_ui_logout_unregistered_refused`). An unreachable endpoint or a pool with no domain is a
`defer` naming the unmet prerequisite, never a pass.

Every token read is signature-verified against the pool's JWKS by
`scripts.smoke.verified_claims`; tests point `jwks_url` at a local JWKS document.

`LiveSmokeUserProvisioner` answers an `MFA_SETUP` challenge if the pool issues one:
`associate_software_token` yields a TOTP secret, `totp_code` computes an RFC 6238 code from it, and
`verify_software_token` plus `admin_respond_to_auth_challenge` complete the challenge. Which factor
was used is reported per run by `auth_factor_detail`. The provisioned user and its `reps` row are
removed again, with deletion confirmed by a read-back, through `cleanup`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from scripts.smoke.harness import (
    CheckResult,
    SmokeContext,
    error_code,
    run_main,
    smoke_client_config,
)
from scripts.smoke.tenant import (
    SmokeTenant,
    allocate_smoke_tenant,
    seed_smoke_tenant,
    teardown_smoke_tenant,
)
from scripts.smoke.verified_claims import USE_ID, verified_claims

from crm.authz import DEMO_TENANT
from crm.envelope import (
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    _truthy,
    split_book,
)
from crm.store.store import Store

LAYER = 4
RESOURCE_CLASS = "cognito_claims"

#: Cognito's `MfaConfiguration` values this check accepts (anything but `OFF`).
_ACCEPTABLE_MFA_CONFIGURATIONS = frozenset({"ON", "OPTIONAL"})

#: Minimum acceptable `PasswordPolicy.MinimumLength`.
_MIN_PASSWORD_LENGTH = 8

#: Required character-class flags on `PasswordPolicy`.
_REQUIRED_PASSWORD_POLICY_FLAGS = (
    "RequireUppercase",
    "RequireLowercase",
    "RequireNumbers",
    "RequireSymbols",
)


def _check_mfa_configuration(pool: Mapping[str, Any]) -> CheckResult:
    """Assert `MfaConfiguration` is not `"OFF"`."""
    mfa = str(pool.get("MfaConfiguration") or "OFF")
    if mfa in _ACCEPTABLE_MFA_CONFIGURATIONS:
        return CheckResult(
            resource="user_pool", check="mfa_configuration", status="pass", detail=f"mfa={mfa}"
        )
    return CheckResult(
        resource="user_pool",
        check="mfa_configuration",
        status="fail",
        detail=f"MfaConfiguration is {mfa!r}, expected one of {sorted(_ACCEPTABLE_MFA_CONFIGURATIONS)}",
    )


def _check_password_policy(pool: Mapping[str, Any]) -> CheckResult:
    """Assert the password policy meets minimum length and character-class requirements.

    Checks the specific fields rather than merely the key's presence.
    """
    policy_config = pool.get("Policies")
    policy = policy_config.get("PasswordPolicy") if isinstance(policy_config, Mapping) else None
    if not isinstance(policy, Mapping):
        return CheckResult(
            resource="user_pool",
            check="password_policy",
            status="fail",
            detail="no Policies.PasswordPolicy present on the user pool",
        )

    problems: list[str] = []
    minimum_length = policy.get("MinimumLength")
    if not isinstance(minimum_length, int) or minimum_length < _MIN_PASSWORD_LENGTH:
        problems.append(f"MinimumLength={minimum_length!r} < {_MIN_PASSWORD_LENGTH}")
    for flag in _REQUIRED_PASSWORD_POLICY_FLAGS:
        if not policy.get(flag):
            problems.append(f"{flag}=False")

    if problems:
        return CheckResult(
            resource="user_pool",
            check="password_policy",
            status="fail",
            detail=f"weak password policy: {'; '.join(problems)}",
        )
    return CheckResult(
        resource="user_pool",
        check="password_policy",
        status="pass",
        detail=f"MinimumLength={minimum_length}, all character classes required",
    )


def _check_account_recovery(pool: Mapping[str, Any]) -> CheckResult:
    """Assert an email-based account recovery mechanism is configured."""
    recovery = pool.get("AccountRecoverySetting")
    mechanisms = (
        recovery.get("RecoveryMechanisms") if isinstance(recovery, Mapping) else None
    ) or []
    email_mechanisms = [
        mechanism
        for mechanism in mechanisms
        if isinstance(mechanism, Mapping) and mechanism.get("Name") == "verified_email"
    ]
    if email_mechanisms:
        return CheckResult(
            resource="user_pool",
            check="account_recovery",
            status="pass",
            detail=f"recovery mechanisms={mechanisms!r}",
        )
    return CheckResult(
        resource="user_pool",
        check="account_recovery",
        status="fail",
        detail=f"no verified_email recovery mechanism configured; found {mechanisms!r}",
    )


def _check_pre_token_generation_v2(pool: Mapping[str, Any]) -> CheckResult:
    """Assert the pre-token-generation trigger is wired as the V2 `LambdaConfig` shape.

    Requires `LambdaConfig.PreTokenGenerationConfig` with `LambdaVersion == "V2_0"`, not the legacy
    `LambdaConfig.PreTokenGeneration` ARN string.
    """
    lambda_config = pool.get("LambdaConfig")
    if not isinstance(lambda_config, Mapping):
        return CheckResult(
            resource="user_pool",
            check="pre_token_generation_v2",
            status="fail",
            detail="no LambdaConfig present on the user pool",
        )

    v2_config = lambda_config.get("PreTokenGenerationConfig")
    if isinstance(v2_config, Mapping):
        lambda_version = str(v2_config.get("LambdaVersion") or "")
        if lambda_version == "V2_0":
            return CheckResult(
                resource="user_pool",
                check="pre_token_generation_v2",
                status="pass",
                detail=f"LambdaArn={v2_config.get('LambdaArn')!r}",
            )
        return CheckResult(
            resource="user_pool",
            check="pre_token_generation_v2",
            status="fail",
            detail=f"PreTokenGenerationConfig.LambdaVersion is {lambda_version!r}, expected 'V2_0'",
        )

    legacy_arn = lambda_config.get("PreTokenGeneration")
    if legacy_arn:
        return CheckResult(
            resource="user_pool",
            check="pre_token_generation_v2",
            status="fail",
            detail=(
                f"found legacy V1 LambdaConfig.PreTokenGeneration={legacy_arn!r}; "
                "V2 PreTokenGenerationConfig with LambdaVersion=V2_0 is required"
            ),
        )

    return CheckResult(
        resource="user_pool",
        check="pre_token_generation_v2",
        status="fail",
        detail="no pre-token-generation trigger (V1 or V2) configured on the user pool",
    )


def _check_admin_create_user_only(pool: Mapping[str, Any]) -> CheckResult:
    """Assert `AdminCreateUserConfig.AllowAdminCreateUserOnly` is exactly `True`.

    A missing block or key fails rather than skips: Cognito's default is `False`, so absence is the
    open state.
    """
    config = pool.get("AdminCreateUserConfig")
    value = config.get("AllowAdminCreateUserOnly") if isinstance(config, Mapping) else None
    if value is True:
        return CheckResult(
            resource="user_pool",
            check="admin_create_user_only",
            status="pass",
            detail="AdminCreateUserConfig.AllowAdminCreateUserOnly=True",
        )
    found = "absent" if value is None else repr(value)
    return CheckResult(
        resource="user_pool",
        check="admin_create_user_only",
        status="fail",
        detail=f"AdminCreateUserConfig.AllowAdminCreateUserOnly is {found}, expected True",
    )


#: The only `SignUp` error code that means the pool refused self-registration.
_SIGNUP_REFUSED_CODE: Final[str] = "NotAuthorizedException"


class UnsignedSignUpClient:
    """A `cognito-idp` client that sends `SignUp` with no SigV4 signature, built on first use.

    Lazy so that wiring it into `main` builds no client until a live run actually calls it.
    """

    def __init__(self, ctx: SmokeContext) -> None:
        """Bind to `ctx`'s profile and region; nothing is constructed yet."""
        self._ctx = ctx
        self._client: Any = None

    def sign_up(self, **kwargs: Any) -> Any:
        """Call `SignUp` unauthenticated with `kwargs`, returning the raw response."""
        if self._client is None:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config

            session = boto3.session.Session(
                profile_name=self._ctx.profile, region_name=self._ctx.region
            )
            self._client = session.client(
                "cognito-idp",
                region_name=self._ctx.region,
                config=smoke_client_config().merge(Config(signature_version=UNSIGNED)),
            )
        return self._client.sign_up(**kwargs)


def _self_signup_checks(
    admin_idp: Any, signup_client: Any, *, user_pool_id: str, client_id: str
) -> Iterable[CheckResult]:
    """Attempt a real unauthenticated `SignUp` and assert the pool refuses it.

    Passes only on the `NotAuthorizedException` error code; any other code, or an accepted call, is
    a failure. Unless the refusal was confirmed, a `finally` deletes the probe user through
    `admin_idp` and confirms it is gone, so an accepted sign-up never leaves a principal behind.
    """
    email = f"selfsignup-smoke-{secrets.token_hex(8)}@example.invalid"
    refused = False
    try:
        try:
            response = signup_client.sign_up(
                ClientId=client_id,
                Username=email,
                Password=_generated_password(),
                UserAttributes=[{"Name": "email", "Value": email}],
            )
        except Exception as exc:  # noqa: BLE001 - classified by error code below
            code = error_code(exc)
            if code == _SIGNUP_REFUSED_CODE:
                refused = True
                yield CheckResult(
                    resource="user_pool",
                    check="self_signup_refused",
                    status="pass",
                    detail=f"unauthenticated SignUp refused with {code}",
                )
                return
            yield CheckResult(
                resource="user_pool",
                check="self_signup_refused",
                status="fail",
                detail=(
                    f"SignUp raised {code or type(exc).__name__}, not {_SIGNUP_REFUSED_CODE}; "
                    "an unrelated error is not evidence of a refusal"
                ),
            )
            return
        user_sub = response.get("UserSub") if isinstance(response, Mapping) else None
        yield CheckResult(
            resource="user_pool",
            check="self_signup_refused",
            status="fail",
            detail=f"unauthenticated SignUp was ACCEPTED (UserSub={user_sub!r})",
        )
    finally:
        if not refused:
            yield _delete_self_signup_user(admin_idp, user_pool_id=user_pool_id, username=email)


def _delete_self_signup_user(admin_idp: Any, *, user_pool_id: str, username: str) -> CheckResult:
    """Delete the `SignUp` probe user if it exists and confirm absence by `admin_get_user`."""
    delete_outcome = "deleted"
    try:
        try:
            admin_idp.admin_delete_user(UserPoolId=user_pool_id, Username=username)
        except Exception as exc:  # noqa: BLE001 - only UserNotFoundException is tolerated
            if error_code(exc) != "UserNotFoundException":
                raise
            delete_outcome = "nothing to delete"
        try:
            admin_idp.admin_get_user(UserPoolId=user_pool_id, Username=username)
        except Exception as exc:  # noqa: BLE001 - the expected outcome is an exception
            if error_code(exc) != "UserNotFoundException":
                raise
            return CheckResult(
                resource="smoke_user",
                check="self_signup_user_deleted",
                status="pass",
                detail=(
                    f"admin_delete_user: {delete_outcome}; admin_get_user read-back raised "
                    "UserNotFoundException"
                ),
            )
        return CheckResult(
            resource="smoke_user",
            check="self_signup_user_deleted",
            status="fail",
            detail=f"admin_get_user still resolves {username!r} after admin_delete_user",
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
        return CheckResult(
            resource="smoke_user",
            check="self_signup_user_deleted",
            status="fail",
            detail=(
                f"could not confirm deletion of {username!r}: "
                f"{error_code(exc) or type(exc).__name__}: {exc}"
            ),
        )


#: RFC 6238 parameters Cognito's software-token MFA uses (30-second step, 6-digit HMAC-SHA1 code).
_TOTP_STEP_SECONDS: Final[int] = 30
_TOTP_DIGITS: Final[int] = 6

#: Tenant-name prefixes a minted token's `custom:tenant` claim may legitimately carry. Any other
#: tenant (the trigger's fail-closed default is `demo`) is a failure.
_DISPOSABLE_TENANT_PREFIXES: Final[tuple[str, ...]] = ("smoke-", "eval-")

#: How many accounts of the seeded corpus the provisioned rep's book names.
_BOOK_SIZE: Final[int] = 2

#: How many authentication attempts `LiveSmokeUserProvisioner` makes. The second covers the
#: pre-token-generation Lambda's cold start, which can fail closed to tenant `demo`.
_AUTH_ATTEMPTS: Final[int] = 2


def totp_code(
    secret_base32: str,
    *,
    timestamp: float,
    step: int = _TOTP_STEP_SECONDS,
    digits: int = _TOTP_DIGITS,
) -> str:
    """Return the RFC 6238 TOTP code for `secret_base32` at `timestamp`.

    Computed from the standard library alone (`base64`/`hmac`/`hashlib`/`struct`).

    Args:
        secret_base32: The base32 secret, as `associate_software_token` returns it (Cognito omits
            the `=` padding, which is re-added here).
        timestamp: Unix seconds the code is computed for.
        step: The time-step width in seconds.
        digits: The number of decimal digits in the returned code.

    Returns:
        The zero-padded code.
    """
    padded = secret_base32.strip().upper()
    padded += "=" * (-len(padded) % 8)
    key = base64.b32decode(padded)
    counter = int(timestamp // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFF_FFFF
    return str(truncated % (10**digits)).zfill(digits)


def auth_factor_detail(*, mfa_completed: bool) -> str:
    """Describe how a sign-in was authenticated, for an inventory `detail` string.

    Callers pass `bool(secret)`: a non-empty TOTP secret exists only when an MFA challenge was
    answered.
    """
    if mfa_completed:
        return "MFA-completed"
    return "password-only (pool MfaConfiguration=OPTIONAL, so no MFA challenge was issued)"


def _generated_password() -> str:
    """Return a throwaway password satisfying the pool's 14-character all-classes policy.

    Assembled at runtime from `secrets`.
    """
    return "Aa1!" + secrets.token_urlsafe(16)


@dataclass(frozen=True, slots=True)
class ProvisionedSmokeUser:
    """A live, MFA-enrolled smoke user and the identity the `reps` table gives it.

    Attributes:
        username: The Cognito `Username` (a uuid, because the pool's `username_attributes` is
            `["email"]`), which is also what the V2 trigger reads as `event["userName"]` and
            therefore the `rep_id` its `reps` lookup is keyed by.
        rep_id: The rep id the trigger is expected to stamp — identical to `username`.
        tenant: The disposable tenant the seeded `reps` row names.
        book: The account ids the seeded `reps` row grants, all of which exist in the seeded corpus.
        id_token: The real ID token the pool issued, after whichever challenge it asked for.
        detail: How the token was obtained, for the inventory's `issue_and_decode` detail.
        refresh_token: The refresh token the same sign-in issued, or `""` when none was issued.
    """

    username: str
    rep_id: str
    tenant: str
    book: frozenset[str]
    id_token: str
    detail: str
    refresh_token: str = ""


class SmokeUserProvisioner(Protocol):
    """Provisions a token-issuing smoke user and removes every resource it created."""

    def provision(self) -> ProvisionedSmokeUser:
        """Return a provisioned, MFA-enrolled smoke user holding a real issued ID token."""

    def cleanup(self) -> Iterable[CheckResult]:
        """Remove everything `provision` created, yielding one check per removal."""


class LiveSmokeUserProvisioner:
    """Mints a real Cognito user under its own disposable tenant, then removes it.

    Uses the pool's `MfaConfiguration` as deployed. An MFA challenge the pool issues is answered —
    `MFA_SETUP` with a real RFC 6238 code, or `SOFTWARE_TOKEN_MFA` on a retry — and the returned
    `detail` says which factor was used (`auth_factor_detail`). The tenant comes from
    `scripts.smoke.tenant.allocate_smoke_tenant`, never `demo`.
    """

    def __init__(
        self,
        ctx: SmokeContext,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        smoke_tenant: SmokeTenant | None = None,
        jwks_url: str | None = None,
    ) -> None:
        """Build a provisioner, optionally bound to a tenant the caller already owns.

        Args:
            ctx: The smoke context supplying clients and stack outputs.
            clock: Time source, injectable for the offline self-test.
            sleeper: Sleep function, injectable for the offline self-test.
            smoke_tenant: A tenant the **caller** allocated, seeded, and will tear down. When given,
                :meth:`provision` neither allocates nor seeds, and :meth:`cleanup` leaves the tenant
                alone — the user and its ``reps`` row are still removed. When ``None`` this
                provisioner owns the whole lifecycle, which is what ``main`` does.
            jwks_url: JWKS endpoint the issued token is verified against; defaults to the pool's.
        """
        self._ctx = ctx
        self._clock = clock if clock is not None else time.time
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self._smoke_tenant: SmokeTenant | None = smoke_tenant
        self._owns_tenant: bool = smoke_tenant is None
        self._username: str | None = None
        self._email: str | None = None
        self._jwks_url = jwks_url
        self._refresh_token = ""  # nosec B105  # empty until sign-in, not a credential

    # -- wiring ----------------------------------------------------------------------------

    def _idp(self) -> Any:
        return self._ctx.client("cognito-idp")

    def _store(self) -> Store:
        return Store(
            client=self._ctx.client("dynamodb"), table_prefix=_live_table_prefix(self._ctx)
        )

    def _user_pool_id(self) -> str:
        return self._ctx.outputs.require_str("cognito_user_pool_id")

    def _client_id(self) -> str:
        return self._ctx.outputs.require_str("cognito_spa_client_id")

    @classmethod
    def for_existing_user(cls, ctx: SmokeContext, *, username: str) -> LiveSmokeUserProvisioner:
        """Build a provisioner bound to an already-created user, for cleanup only.

        For a cross-process teardown that holds only the username. Calling :meth:`provision` on the
        result is not supported. A placeholder tenant marks the tenant as caller-owned, so
        :meth:`cleanup` removes the user and its ``reps`` row and never touches a tenant.

        Args:
            ctx: The smoke context.
            username: The Cognito ``Username`` to remove.
        """
        placeholder = SmokeTenant(run_id="smoke-cleanup-only", tenant="smoke-cleanup-only")
        provisioner = cls(ctx, smoke_tenant=placeholder)
        provisioner._username = username
        provisioner._email = username
        return provisioner

    # -- provisioning ----------------------------------------------------------------------

    def provision(self) -> ProvisionedSmokeUser:
        """Create the user, enroll MFA, and return a real ID token under a disposable tenant.

        Allocates and seeds that tenant unless one was injected at construction, in which case the
        caller has already done both and owns the teardown.

        Raises:
            RuntimeError: Any step could not be completed, or the issued token names a tenant that
                is not the disposable one this provisioner is bound to.
        """
        store = self._store()
        if self._smoke_tenant is None:
            smoke_tenant = allocate_smoke_tenant(store)
            self._smoke_tenant = smoke_tenant
            _require_disposable_tenant(smoke_tenant.tenant)
            seed_smoke_tenant(store, smoke_tenant)
        else:
            # An injected tenant is checked by the same predicate before any `reps` write.
            smoke_tenant = self._smoke_tenant
            _require_disposable_tenant(smoke_tenant.tenant)

        book = self._seeded_book(store, smoke_tenant.tenant)

        idp = self._idp()
        # Run-scoped and per provisioner: several users may share one injected tenant.
        email = f"smoke-{smoke_tenant.run_id}-{secrets.token_hex(4)}@example.test"
        password = _generated_password()
        created = idp.admin_create_user(
            UserPoolId=self._user_pool_id(),
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        self._email = email
        username = str((created.get("User") or {}).get("Username") or email)
        self._username = username

        idp.admin_set_user_password(
            UserPoolId=self._user_pool_id(), Username=username, Password=password, Permanent=True
        )

        store.put_item(
            "reps",
            {
                "rep_id": username,
                "tenant": smoke_tenant.tenant,
                "book": sorted(book),
                "full_visibility": False,
                "name": "Layer 4 Cognito claim smoke",
                "status": "active",
            },
        )

        id_token, detail = self._authenticate_with_mfa(
            username=username, password=password, expected_tenant=smoke_tenant.tenant
        )
        return ProvisionedSmokeUser(
            username=username,
            rep_id=username,
            tenant=smoke_tenant.tenant,
            book=book,
            id_token=id_token,
            detail=detail,
            refresh_token=self._refresh_token,
        )

    def _seeded_book(self, store: Store, tenant: str) -> frozenset[str]:
        """Return `_BOOK_SIZE` account ids that genuinely exist in `tenant`'s seeded corpus.

        The ids are the fixture generator's own deterministic `acct-<tenant>-<index>` form, and each
        is read back from DynamoDB before being used.
        """
        account_ids: list[str] = []
        for index in range(_BOOK_SIZE):
            account_id = f"acct-{tenant}-{index:04d}"
            if store.get_item("accounts", {"account_id": account_id}) is None:
                raise RuntimeError(
                    f"seeded account {account_id!r} not found after seeding tenant {tenant!r}; "
                    "refusing to build a book naming an account that does not exist"
                )
            account_ids.append(account_id)
        return frozenset(account_ids)

    def _authenticate_with_mfa(
        self, *, username: str, password: str, expected_tenant: str
    ) -> tuple[str, str]:
        """Authenticate `username`, completing MFA enrollment if the pool challenges for it.

        Retries once when the issued token's `custom:tenant` is not `expected_tenant`: the
        pre-token-generation Lambda's cold start can time out its `reps` lookup and fail closed to
        `demo`. A mismatch that survives the retry raises rather than being accepted.
        """
        secret: str | None = None
        last_tenant = ""
        for attempt in range(1, _AUTH_ATTEMPTS + 1):
            if attempt > 1:
                self._wait_for_next_totp_step()
            id_token, secret = self._one_auth_attempt(
                username=username, password=password, secret=secret
            )
            claims = _decode_id_token(
                id_token,
                region=self._ctx.region,
                user_pool_id=self._user_pool_id(),
                client_id=self._client_id(),
                jwks_url=self._jwks_url,
            )
            last_tenant = str(claims.get(CLAIM_TENANT) or "")
            if last_tenant == expected_tenant:
                factor = auth_factor_detail(mfa_completed=bool(secret))
                return id_token, f"{factor} ID token issued on attempt {attempt}"
        raise RuntimeError(
            f"issued token names tenant {last_tenant!r}, expected the disposable tenant "
            f"{expected_tenant!r} after {_AUTH_ATTEMPTS} attempt(s); the pre-token-generation "
            "trigger's reps lookup did not resolve, so the token fell back to its fail-closed "
            "default and must not be used"
        )

    def _one_auth_attempt(
        self, *, username: str, password: str, secret: str | None
    ) -> tuple[str, str]:
        """Run one `ADMIN_USER_PASSWORD_AUTH` sign-in, answering whichever MFA challenge arrives.

        Returns the ID token and the TOTP secret (associated on the first attempt, reused after).
        """
        idp = self._idp()
        response = idp.admin_initiate_auth(
            UserPoolId=self._user_pool_id(),
            ClientId=self._client_id(),
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        challenge = str(response.get("ChallengeName") or "")

        if challenge == "MFA_SETUP":
            associated = idp.associate_software_token(Session=response["Session"])
            secret = str(associated["SecretCode"])
            verified = idp.verify_software_token(
                Session=associated["Session"],
                UserCode=totp_code(secret, timestamp=self._clock()),
                FriendlyDeviceName="crm-eval-smoke",
            )
            if str(verified.get("Status")) != "SUCCESS":
                raise RuntimeError(
                    f"verify_software_token returned Status={verified.get('Status')!r}, "
                    "expected 'SUCCESS'"
                )
            response = idp.admin_respond_to_auth_challenge(
                UserPoolId=self._user_pool_id(),
                ClientId=self._client_id(),
                ChallengeName="MFA_SETUP",
                Session=verified["Session"],
                ChallengeResponses={"USERNAME": username},
            )
        elif challenge == "SOFTWARE_TOKEN_MFA":
            if secret is None:
                raise RuntimeError(
                    "pool challenged SOFTWARE_TOKEN_MFA before any software token was associated"
                )
            response = idp.admin_respond_to_auth_challenge(
                UserPoolId=self._user_pool_id(),
                ClientId=self._client_id(),
                ChallengeName="SOFTWARE_TOKEN_MFA",
                Session=response["Session"],
                ChallengeResponses={
                    "USERNAME": username,
                    "SOFTWARE_TOKEN_MFA_CODE": totp_code(secret, timestamp=self._clock()),
                },
            )
        elif challenge:
            raise RuntimeError(
                f"unexpected challenge {challenge!r} from admin_initiate_auth; this script answers "
                "MFA_SETUP and SOFTWARE_TOKEN_MFA only and never weakens the pool to avoid one"
            )

        result = response.get("AuthenticationResult") or {}
        id_token = result.get("IdToken")
        if not id_token:
            raise RuntimeError(
                f"no IdToken issued for {username!r} "
                f"(ChallengeName={response.get('ChallengeName')!r})"
            )
        self._refresh_token = str(result.get("RefreshToken") or "")
        return str(id_token), secret or ""

    def _wait_for_next_totp_step(self) -> None:
        """Sleep into the next TOTP window, so a retry never replays an already-consumed code."""
        now = self._clock()
        self._sleeper(_TOTP_STEP_SECONDS - (now % _TOTP_STEP_SECONDS) + 1.0)

    # -- cleanup ----------------------------------------------------------------------------

    def cleanup(self) -> Iterable[CheckResult]:
        """Delete the Cognito user, its `reps` row, and the tenant, proving each removal by re-read.

        Every removal is attempted even when an earlier one failed, and each is reported as its own
        check.
        """
        yield from self._cleanup_cognito_user()
        yield from self._cleanup_reps_row()
        yield from self._cleanup_tenant()

    def _cleanup_cognito_user(self) -> Iterable[CheckResult]:
        username = self._username or self._email
        if username is None:
            return
        try:
            idp = self._idp()
            idp.admin_delete_user(UserPoolId=self._user_pool_id(), Username=username)
            try:
                idp.admin_get_user(UserPoolId=self._user_pool_id(), Username=username)
            except Exception as exc:  # noqa: BLE001 - the expected outcome is an exception
                if "UserNotFoundException" not in f"{type(exc).__name__}: {exc}":
                    raise
                yield CheckResult(
                    resource="smoke_user",
                    check="cognito_user_deleted",
                    status="pass",
                    detail="admin_get_user after deletion raised UserNotFoundException",
                )
                return
            yield CheckResult(
                resource="smoke_user",
                check="cognito_user_deleted",
                status="fail",
                detail="admin_get_user still resolves the smoke user after admin_delete_user",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource="smoke_user",
                check="cognito_user_deleted",
                status="fail",
                detail=f"could not confirm smoke user deletion: {type(exc).__name__}: {exc}",
            )

    def _cleanup_reps_row(self) -> Iterable[CheckResult]:
        if self._username is None:
            return
        try:
            store = self._store()
            store.delete_item("reps", {"rep_id": self._username})
            remaining = store.get_item("reps", {"rep_id": self._username})
            yield CheckResult(
                resource="smoke_user",
                check="reps_row_deleted",
                status="pass" if remaining is None else "fail",
                detail=(
                    "reps row absent on read-back"
                    if remaining is None
                    else "reps row still present after delete_item"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource="smoke_user",
                check="reps_row_deleted",
                status="fail",
                detail=f"could not delete the smoke reps row: {type(exc).__name__}: {exc}",
            )

    def _cleanup_tenant(self) -> Iterable[CheckResult]:
        smoke_tenant = self._smoke_tenant
        if smoke_tenant is None:
            return
        if not self._owns_tenant:
            # An injected tenant is torn down by the caller.
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="skip",
                detail=(
                    f"tenant {smoke_tenant.tenant!r} was injected by the caller, which owns its "
                    "teardown; this provisioner removed only the user and its reps row"
                ),
            )
            return
        try:
            deleted = teardown_smoke_tenant(self._store(), smoke_tenant)
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="pass",
                detail=f"tenant {smoke_tenant.tenant!r} torn down ({sum(deleted.values())} items)",
            )
        except Exception as exc:  # noqa: BLE001 - a failed teardown must never read as clean
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="fail",
                detail=f"teardown of {smoke_tenant.tenant!r} failed: {type(exc).__name__}: {exc}",
            )


def _require_disposable_tenant(tenant: str) -> None:
    """Raise unless `tenant` is a disposable smoke/eval tenant (never `demo`)."""
    if tenant == DEMO_TENANT or not tenant.startswith(_DISPOSABLE_TENANT_PREFIXES):
        raise RuntimeError(
            f"refusing to provision under tenant {tenant!r}: a smoke run must write only beneath a "
            f"disposable tenant named with one of {list(_DISPOSABLE_TENANT_PREFIXES)}"
        )


def _live_table_prefix(ctx: SmokeContext) -> str:
    """Derive the live DynamoDB table-name prefix from the `dynamodb_table_names` output.

    Mirrors `scripts/smoke/lambda_actions.py` and `scripts/smoke/api_routes.py`: the physical name for
    the `eval_runs` logical table is `<table_prefix>eval_runs`, so stripping that suffix recovers the
    prefix.
    """
    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    return str(table_names["eval_runs"]).removesuffix("eval_runs")


def _check_tenant_claim_is_disposable(claims: Mapping[str, Any], *, expected: str) -> CheckResult:
    """Assert the minted token's `custom:tenant` is the disposable tenant, never `demo`.

    The trigger's fail-closed default is `demo`, so a token carrying it means the `reps` lookup did
    not resolve; that is a failure.
    """
    actual = str(claims.get(CLAIM_TENANT) or "")
    if actual == DEMO_TENANT:
        return CheckResult(
            resource="id_token",
            check="tenant_claim_is_disposable",
            status="fail",
            detail=(
                f"{CLAIM_TENANT}={actual!r} is the fail-closed default, not the provisioned "
                f"tenant {expected!r}: the V2 trigger's reps lookup did not resolve"
            ),
        )
    if actual != expected:
        return CheckResult(
            resource="id_token",
            check="tenant_claim_is_disposable",
            status="fail",
            detail=f"{CLAIM_TENANT}={actual!r}, expected the provisioned tenant {expected!r}",
        )
    return CheckResult(
        resource="id_token",
        check="tenant_claim_is_disposable",
        status="pass",
        detail=f"{CLAIM_TENANT}={actual!r} (disposable, not {DEMO_TENANT!r})",
    )


def _decode_id_token(
    id_token: str, *, region: str, user_pool_id: str, client_id: str, jwks_url: str | None = None
) -> dict[str, Any]:
    """Return an ID token's claims once `verified_claims` has verified it for the SPA client.

    Raises:
        ClaimsVerificationError: Signature, expiry, issuer, `token_use` or audience is wrong.
    """
    return verified_claims(
        id_token,
        region=region,
        user_pool_id=user_pool_id,
        client_id=client_id,
        token_use=USE_ID,
        jwks_url=jwks_url,
    )


def _check_claim_present(claims: Mapping[str, Any], claim_name: str) -> CheckResult:
    """Assert `claim_name` is present in `claims` with a non-empty value.

    A claim that is present but an empty string (or an empty list for `custom:book`) is treated
    identically to an absent claim.
    """
    if claim_name not in claims:
        return CheckResult(
            resource="id_token",
            check=f"claim_present:{claim_name}",
            status="fail",
            detail=f"{claim_name} is absent from the issued ID token",
        )
    value = claims[claim_name]
    if value is None or (isinstance(value, str) and not value.strip()):
        return CheckResult(
            resource="id_token",
            check=f"claim_present:{claim_name}",
            status="fail",
            detail=f"{claim_name} is present but empty ({value!r})",
        )
    return CheckResult(
        resource="id_token",
        check=f"claim_present:{claim_name}",
        status="pass",
        detail=f"{claim_name}={value!r}",
    )


def _check_full_visibility_is_boolean_like(claims: Mapping[str, Any]) -> CheckResult:
    """Assert `custom:full_visibility` parses to an unambiguous boolean via `crm.envelope._truthy`.

    Cognito custom claims are always strings on the wire. `_truthy` treats anything unrecognised as
    `False`; a claim carrying an unrecognised value is still reported.
    """
    raw = claims.get(CLAIM_FULL_VISIBILITY)
    recognised = {"true", "false", "1", "0", "yes", "no"}
    if isinstance(raw, str) and raw.strip().lower() not in recognised:
        return CheckResult(
            resource="id_token",
            check="full_visibility_boolean_like",
            status="fail",
            detail=f"{CLAIM_FULL_VISIBILITY}={raw!r} is not a recognised boolean-like string",
        )
    return CheckResult(
        resource="id_token",
        check="full_visibility_boolean_like",
        status="pass",
        detail=f"{CLAIM_FULL_VISIBILITY}={raw!r} parses as {_truthy(raw)}",
    )


def _check_book_matches_seeded_rep(
    claims: Mapping[str, Any], *, expected_rep_id: str, expected_book: frozenset[str]
) -> CheckResult:
    """Assert the token's `custom:book` claim matches the seeded rep's actual owned accounts."""
    actual_rep_id = str(claims.get(CLAIM_REP_ID) or "")
    if actual_rep_id != expected_rep_id:
        return CheckResult(
            resource="id_token",
            check="book_matches_seeded_rep",
            status="fail",
            detail=f"{CLAIM_REP_ID}={actual_rep_id!r}, expected {expected_rep_id!r}",
        )
    actual_book = split_book(claims.get(CLAIM_BOOK))
    if actual_book != expected_book:
        return CheckResult(
            resource="id_token",
            check="book_matches_seeded_rep",
            status="fail",
            detail=(
                f"{CLAIM_BOOK} for rep {expected_rep_id!r} is {sorted(actual_book)!r}, "
                f"expected {sorted(expected_book)!r}"
            ),
        )
    return CheckResult(
        resource="id_token",
        check="book_matches_seeded_rep",
        status="pass",
        detail=f"book for {expected_rep_id!r} matches seeded corpus ({len(actual_book)} account(s))",
    )


def _authenticate_smoke_user(
    ctx: SmokeContext, *, username: str, password: str, client_id: str
) -> str:
    """Authenticate a smoke-tenant test user via `admin_initiate_auth` and return the ID token.

    Uses the admin (`ADMIN_USER_PASSWORD_AUTH`) flow rather than SRP.

    Raises:
        RuntimeError: The auth response carries no `IdToken` (for example, the user pool demands
            an interactive challenge this flow does not satisfy).
    """
    idp = ctx.client("cognito-idp")
    user_pool_id = ctx.outputs.require_str("cognito_user_pool_id")
    response = idp.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    result = response.get("AuthenticationResult") or {}
    id_token = result.get("IdToken")
    if not id_token:
        raise RuntimeError(
            f"admin_initiate_auth for {username!r} returned no IdToken "
            f"(ChallengeName={response.get('ChallengeName')!r})"
        )
    return str(id_token)


#: The error code `InitiateAuth REFRESH_TOKEN_AUTH` must raise for a revoked refresh token.
_REVOKED_REFRESH_CODE: Final[str] = "NotAuthorizedException"


def _check_token_revocation_enabled(idp: Any, *, user_pool_id: str, client_id: str) -> CheckResult:
    """The SPA app client has `EnableTokenRevocation` set to `true`."""
    try:
        described = idp.describe_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a single actionable failing check
        return CheckResult(
            resource="spa_client",
            check="token_revocation_enabled",
            status="fail",
            detail=f"describe_user_pool_client failed: {error_code(exc) or type(exc).__name__}",
        )
    client = described.get("UserPoolClient") if isinstance(described, Mapping) else None
    enabled = client.get("EnableTokenRevocation") if isinstance(client, Mapping) else None
    return CheckResult(
        resource="spa_client",
        check="token_revocation_enabled",
        status="pass" if enabled is True else "fail",
        detail=f"EnableTokenRevocation={enabled!r}",
    )


#: Answers one Hosted UI request without following redirects: `(HTTP status, Location header)`.
LogoutProbe = Callable[[str], tuple[int, str]]

#: A `logout_uri` the SPA client never registers.
UNREGISTERED_LOGOUT_URI: Final[str] = "https://smoke-unregistered.invalid/logout"

_LOGOUT_CHECKS: Final[tuple[str, str]] = (
    "hosted_ui_logout_registered_redirects",
    "hosted_ui_logout_unregistered_refused",
)


def _live_logout_probe(url: str) -> tuple[int, str]:
    """GET `url` once over HTTPS without following a redirect; return the status and `Location`."""
    import http.client
    import ssl
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"refusing a non-HTTPS Hosted UI URL: {url!r}")
    connection = http.client.HTTPSConnection(
        parts.hostname, timeout=10, context=ssl.create_default_context()
    )
    try:
        connection.request("GET", f"{parts.path}?{parts.query}")
        response = connection.getresponse()
        return int(response.status), str(response.getheader("Location") or "")
    finally:
        connection.close()


def _hosted_ui_logout_checks(
    idp: Any,
    pool: Mapping[str, Any],
    *,
    user_pool_id: str,
    client_id: str,
    region: str,
    probe: LogoutProbe | None,
) -> Iterable[CheckResult]:
    from urllib.parse import urlencode

    def _all(
        status: Literal["fail", "skip", "defer"], detail: str, reason: str = ""
    ) -> Iterable[CheckResult]:
        for check in _LOGOUT_CHECKS:
            yield CheckResult(
                resource="spa_client",
                check=check,
                status=status,
                detail=detail,
                **({"defer_reason": reason} if status == "defer" else {}),
            )

    if probe is None:
        yield from _all("skip", "no Hosted UI probe supplied")
        return
    domain = str(pool.get("Domain") or "")
    if not domain:
        yield from _all(
            "defer",
            "describe_user_pool reports no Domain, so there is no Hosted UI /logout to probe",
            "hosted_ui_domain_absent",
        )
        return
    try:
        described = idp.describe_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id)
        registered = list((described.get("UserPoolClient") or {}).get("LogoutURLs") or [])
    except Exception as exc:  # noqa: BLE001 - reported on both rows
        yield from _all(
            "fail", f"describe_user_pool_client failed: {error_code(exc) or type(exc).__name__}"
        )
        return
    if not registered:
        yield from _all("fail", "the SPA client registers no LogoutURLs")
        return
    base = f"https://{domain}.auth.{region}.amazoncognito.com/logout"
    target = str(registered[0])
    answers: list[tuple[int, str]] = []
    for uri in (target, UNREGISTERED_LOGOUT_URI):
        url = f"{base}?{urlencode({'client_id': client_id, 'logout_uri': uri})}"
        try:
            answers.append(probe(url))
        except Exception as exc:  # noqa: BLE001 - an unreachable endpoint verified nothing
            yield from _all(
                "defer",
                f"GET {base} raised {type(exc).__name__}: {exc}",
                "hosted_ui_unreachable",
            )
            return
    (status, location), (bad_status, bad_location) = answers
    yield CheckResult(
        resource="spa_client",
        check=_LOGOUT_CHECKS[0],
        status="pass" if status == 302 and location == target else "fail",
        detail=f"logout_uri={target!r}: HTTP {status} Location={location!r}; expected 302 to it",
    )
    leaked = bad_location.startswith(UNREGISTERED_LOGOUT_URI)
    yield CheckResult(
        resource="spa_client",
        check=_LOGOUT_CHECKS[1],
        status="pass" if bad_status in {302, 400} and not leaked else "fail",
        detail=f"logout_uri={UNREGISTERED_LOGOUT_URI!r}: HTTP {bad_status} "
        f"Location={bad_location!r}; expected an error answer that does not redirect to it",
    )


def _check_revoked_refresh_token_refused(
    idp: Any, *, client_id: str, refresh_token: str
) -> CheckResult:
    """Revoke `refresh_token`, then assert `REFRESH_TOKEN_AUTH` with it is refused.

    Passes only on `NotAuthorizedException`; a failed revoke, an accepted refresh, or any other
    error fails the row.
    """
    check = "revoked_refresh_token_refused"
    if not refresh_token:
        return CheckResult(
            resource="spa_client",
            check=check,
            status="fail",
            detail="the smoke user's sign-in issued no refresh token, so revocation is unproven",
        )
    try:
        idp.revoke_token(Token=refresh_token, ClientId=client_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a single actionable failing check
        return CheckResult(
            resource="spa_client",
            check=check,
            status="fail",
            detail=f"RevokeToken failed: {error_code(exc) or type(exc).__name__}",
        )
    try:
        idp.initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            ClientId=client_id,
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
    except Exception as exc:  # noqa: BLE001 - classified by error code below
        code = error_code(exc)
        return CheckResult(
            resource="spa_client",
            check=check,
            status="pass" if code == _REVOKED_REFRESH_CODE else "fail",
            detail=(
                f"REFRESH_TOKEN_AUTH after RevokeToken raised {code or type(exc).__name__}, "
                f"expected {_REVOKED_REFRESH_CODE}"
            ),
        )
    return CheckResult(
        resource="spa_client",
        check=check,
        status="fail",
        detail="REFRESH_TOKEN_AUTH with the revoked refresh token was ACCEPTED",
    )


def checks(
    ctx: SmokeContext,
    *,
    username: str = "",
    password: str = "",
    expected_rep_id: str = "",
    expected_book: frozenset[str] = frozenset(),
    decode_id_token: Any = None,
    provisioner: SmokeUserProvisioner | None = None,
    signup_client: Any = None,
    logout_probe: LogoutProbe | None = None,
) -> Iterable[CheckResult]:
    """Yield one `CheckResult` per (resource, check) pair.

    Never raises for an individual check failure; the pool-configuration checks, the trigger-
    version check, and the token-decode assertions all run and are all reported, even once an
    earlier one has already failed.

    Args:
        ctx: The smoke context. `ctx.client("cognito-idp")` provides the Cognito IDP client
            (injected for tests, live otherwise).
        username: An already-provisioned smoke-tenant test user to authenticate as. When supplied
            it takes precedence over `provisioner`.
        password: That user's password.
        expected_rep_id: The seeded rep id the smoke user is provisioned as.
        expected_book: The set of account ids that seeded rep actually owns, per the fixture
            corpus (`fixtures/generate.py`), for the book-claim-matches-seeded-rep assertion.
        decode_id_token: The claims decoder. Defaults to `_live_decode_for(ctx)`, which verifies
            against the pool's JWKS.
        provisioner: Mints an MFA-enrolled smoke user (and removes it again) when no `username` /
            `password` pair was supplied. The live entry point always supplies one.
        signup_client: Sends the unauthenticated `SignUp` probe (`sign_up(**kwargs)`). When `None`
            `self_signup_refused` reports `skip`; the live entry point always supplies an
            `UnsignedSignUpClient`.
        logout_probe: Sends the Hosted UI `/logout` requests. When `None` both logout rows report
            `skip`; the live entry point always supplies `_live_logout_probe`.
    """
    idp = ctx.client("cognito-idp")
    user_pool_id = ctx.outputs.require_str("cognito_user_pool_id")

    pool = idp.describe_user_pool(UserPoolId=user_pool_id).get("UserPool") or {}
    spa_client_id = ctx.outputs.require_str("cognito_spa_client_id")

    yield _check_mfa_configuration(pool)
    yield _check_password_policy(pool)
    yield _check_account_recovery(pool)
    yield _check_pre_token_generation_v2(pool)
    yield _check_admin_create_user_only(pool)
    yield _check_token_revocation_enabled(idp, user_pool_id=user_pool_id, client_id=spa_client_id)
    yield from _hosted_ui_logout_checks(
        idp,
        pool,
        user_pool_id=user_pool_id,
        client_id=spa_client_id,
        region=ctx.region,
        probe=logout_probe,
    )

    if signup_client is None:
        yield CheckResult(
            resource="user_pool",
            check="self_signup_refused",
            status="skip",
            detail="no unauthenticated sign-up client supplied",
        )
    else:
        yield from _self_signup_checks(
            idp,
            signup_client,
            user_pool_id=user_pool_id,
            client_id=ctx.outputs.require_str("cognito_spa_client_id"),
        )

    decoder = decode_id_token if decode_id_token is not None else _live_decode_for(ctx)

    if not username or not password:
        if provisioner is None:
            yield CheckResult(
                resource="id_token",
                check="issue_and_decode",
                status="skip",
                detail="no smoke-tenant test user credentials supplied",
            )
            return
        yield from _provisioned_token_checks(
            provisioner, decoder=decoder, idp=idp, client_id=spa_client_id
        )
        return

    client_id = ctx.outputs.require_str("cognito_spa_client_id")
    try:
        id_token = _authenticate_smoke_user(
            ctx, username=username, password=password, client_id=client_id
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a single actionable failing check
        yield CheckResult(
            resource="id_token",
            check="authenticate",
            status="fail",
            detail=f"authentication failed for {username!r}: {exc}",
        )
        return

    yield CheckResult(
        resource="id_token", check="authenticate", status="pass", detail=f"issued for {username!r}"
    )
    yield CheckResult(
        resource="spa_client",
        check="revoked_refresh_token_refused",
        status="skip",
        detail=(
            "a pre-provisioned user's refresh token is not revoked; the live entry point "
            "provisions its own user, whose refresh token is revoked and probed"
        ),
    )

    try:
        claims = decoder(id_token)
    except Exception as exc:  # noqa: BLE001 - surfaced as a single actionable failing check
        yield CheckResult(
            resource="id_token",
            check="decode",
            status="fail",
            detail=f"could not decode issued ID token: {exc}",
        )
        return

    yield from _claim_checks(claims, expected_rep_id=expected_rep_id, expected_book=expected_book)


def _claim_checks(
    claims: Mapping[str, Any], *, expected_rep_id: str, expected_book: frozenset[str]
) -> Iterable[CheckResult]:
    """Yield the per-claim assertions over an already-decoded token's `claims`."""
    for claim_name in (CLAIM_REP_ID, CLAIM_BOOK, CLAIM_FULL_VISIBILITY):
        yield _check_claim_present(claims, claim_name)

    yield _check_full_visibility_is_boolean_like(claims)

    if expected_rep_id:
        yield _check_book_matches_seeded_rep(
            claims, expected_rep_id=expected_rep_id, expected_book=expected_book
        )
    else:
        yield CheckResult(
            resource="id_token",
            check="book_matches_seeded_rep",
            status="skip",
            detail="no expected seeded rep supplied",
        )


def _provisioned_token_checks(
    provisioner: SmokeUserProvisioner, *, decoder: Any, idp: Any, client_id: str
) -> Iterable[CheckResult]:
    """Provision an MFA-enrolled user, assert the claims in its real token, prove its refresh token
    is refused once revoked, then always clean up.

    `issue_and_decode` is reported as `pass`/`fail`, never `skip`. Cleanup runs from a `finally`
    block, and each removal is reported as its own check.
    """
    try:
        try:
            provisioned = provisioner.provision()
        except Exception as exc:  # noqa: BLE001 - surfaced as a single actionable failing check
            yield CheckResult(
                resource="id_token",
                check="issue_and_decode",
                status="fail",
                detail=(
                    "could not provision an MFA-enrolled smoke user and issue a token: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            return

        try:
            claims = decoder(provisioned.id_token)
        except Exception as exc:  # noqa: BLE001 - surfaced as a single actionable failing check
            yield CheckResult(
                resource="id_token",
                check="issue_and_decode",
                status="fail",
                detail=f"could not decode the issued ID token: {type(exc).__name__}: {exc}",
            )
            return

        yield CheckResult(
            resource="id_token",
            check="issue_and_decode",
            status="pass",
            detail=provisioned.detail,
        )
        yield _check_tenant_claim_is_disposable(claims, expected=provisioned.tenant)
        yield from _claim_checks(
            claims, expected_rep_id=provisioned.rep_id, expected_book=provisioned.book
        )
        yield _check_revoked_refresh_token_refused(
            idp, client_id=client_id, refresh_token=provisioned.refresh_token
        )
    finally:
        yield from provisioner.cleanup()


def _smoke_credentials_from_env() -> tuple[str, str, str, frozenset[str]]:
    """Resolve smoke-tenant test-user credentials from the process environment.

    `CRM_SMOKE_COGNITO_USERNAME` / `CRM_SMOKE_COGNITO_PASSWORD` identify the already-provisioned
    smoke-tenant test user; `CRM_SMOKE_REP_ID` names the seeded rep that user maps to, and
    `CRM_SMOKE_REP_BOOK` is that rep's comma-joined expected book, matching the fixture corpus
    (`fixtures/generate.py`). All four are optional.
    """
    username = os.environ.get("CRM_SMOKE_COGNITO_USERNAME", "")
    password = os.environ.get("CRM_SMOKE_COGNITO_PASSWORD", "")
    expected_rep_id = os.environ.get("CRM_SMOKE_REP_ID", "")
    expected_book = split_book(os.environ.get("CRM_SMOKE_REP_BOOK", ""))
    return username, password, expected_rep_id, expected_book


def _live_decode_for(ctx: SmokeContext, *, jwks_url: str | None = None) -> Any:
    """Build a `checks`-compatible decoder that verifies against `ctx`'s pool JWKS.

    Reads `ctx.region` and the `cognito_user_pool_id` / `cognito_spa_client_id` outputs already
    loaded onto `ctx`; `jwks_url` overrides the pool's endpoint.
    """
    user_pool_id = ctx.outputs.require_str("cognito_user_pool_id")
    client_id = ctx.outputs.require_str("cognito_spa_client_id")

    def decode(id_token: str) -> dict[str, Any]:
        return _decode_id_token(
            id_token,
            region=ctx.region,
            user_pool_id=user_pool_id,
            client_id=client_id,
            jwks_url=jwks_url,
        )

    return decode


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Every token is verified against the pool JWKS before its claims are read.

    When no `CRM_SMOKE_COGNITO_USERNAME`/`CRM_SMOKE_COGNITO_PASSWORD` pair is exported, a
    `LiveSmokeUserProvisioner` mints a real user under its own disposable tenant and removes it
    again.
    """
    username, password, expected_rep_id, expected_book = _smoke_credentials_from_env()
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=lambda ctx: checks(
            ctx,
            username=username,
            password=password,
            expected_rep_id=expected_rep_id,
            expected_book=expected_book,
            decode_id_token=_live_decode_for(ctx),
            provisioner=LiveSmokeUserProvisioner(ctx),
            signup_client=UnsignedSignUpClient(ctx),
            logout_probe=_live_logout_probe,
        ),
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
