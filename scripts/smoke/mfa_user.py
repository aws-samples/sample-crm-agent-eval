# SPDX-License-Identifier: MIT
"""A bare Cognito end user, minted only to obtain an access token lacking the `gateway/invoke` scope.

Unlike the other smoke user provisioners it seeds no tenant, corpus or `reps` row: the token is
meant to be refused by the Gateway's authorizer, so the only live resource is one Cognito user,
deleted and confirmed absent on the way out.

The pool's `MfaConfiguration` is used as deployed; an `MFA_SETUP` challenge, if issued, is completed
with a real RFC 6238 code (`cognito_claims.totp_code`). Nothing here asserts MFA.

An access token is returned, not an ID token: only an access token carries the `scope` claim the
Gateway's `customJWTAuthorizer` matches against its `allowedScopes`.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final

from scripts.smoke.cognito_claims import auth_factor_detail, totp_code
from scripts.smoke.harness import CheckResult, SmokeContext

__all__ = [
    "BareMfaUser",
    "LiveBareMfaUserProvisioner",
]

#: Resource this provisioner's cleanup checks are filed under in the inventory.
_RESOURCE: Final[str] = "underscoped_smoke_user"


def _generated_password() -> str:
    """Return a throwaway password satisfying the pool's 14-character all-classes policy.

    Assembled at runtime from `secrets`.
    """
    return "Aa1!" + secrets.token_urlsafe(16)


@dataclass(frozen=True, slots=True)
class BareMfaUser:
    """A live Cognito user from the deployed pool, and the access token it was issued.

    Attributes:
        username: The Cognito `Username` (a uuid, since `username_attributes` is `["email"]`).
        access_token: The real access token the pool issued. Held in memory only — never written
            to a file, logged, or placed in a `CheckResult.detail`.
        auth_factor: How this sign-in was authenticated, from `cognito_claims.auth_factor_detail`.
            Defaults to `""`.
    """

    username: str
    access_token: str
    auth_factor: str = ""


class LiveBareMfaUserProvisioner:
    """Creates one Cognito user, returns its access token, and deletes the user again.

    Writes nothing to DynamoDB, allocates no tenant, and seeds no corpus. Any MFA challenge the pool
    issues is completed; which factor was used is reported through `BareMfaUser.auth_factor`.
    """

    def __init__(
        self,
        ctx: SmokeContext,
        *,
        clock: Callable[[], float] | None = None,
        label: str = "smoke-underscoped",
    ) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else time.time
        self._label = label
        self._username: str | None = None

    def _idp(self) -> Any:
        return self._ctx.client("cognito-idp")

    def _user_pool_id(self) -> str:
        return self._ctx.outputs.require_str("cognito_user_pool_id")

    def _client_id(self) -> str:
        return self._ctx.outputs.require_str("cognito_spa_client_id")

    def provision(self) -> BareMfaUser:
        """Create the user, answer any MFA challenge, and return its issued access token.

        Raises:
            RuntimeError: The pool issued no access token, or issued a challenge this provisioner
                does not handle.
        """
        idp = self._idp()
        email = f"{self._label}-{secrets.token_hex(8)}@example.test"
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
        username = str((created.get("User") or {}).get("Username") or email)
        self._username = username
        idp.admin_set_user_password(
            UserPoolId=self._user_pool_id(), Username=username, Password=password, Permanent=True
        )
        access_token, auth_factor = self._authenticate_with_mfa(
            username=username, password=password
        )
        return BareMfaUser(username=username, access_token=access_token, auth_factor=auth_factor)

    def _authenticate_with_mfa(self, *, username: str, password: str) -> tuple[str, str]:
        """Sign in once, completing MFA enrollment if challenged.

        Returns:
            The access token, and how the sign-in was authenticated — derived from whether a TOTP
            secret was actually associated, never from the pool's configured setting.
        """
        idp = self._idp()
        response = idp.admin_initiate_auth(
            UserPoolId=self._user_pool_id(),
            ClientId=self._client_id(),
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        challenge = str(response.get("ChallengeName") or "")
        secret = ""  # nosec B105  # empty initial value, not a credential

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
        elif challenge:
            raise RuntimeError(
                f"unexpected challenge {challenge!r} from admin_initiate_auth; this provisioner "
                "completes MFA_SETUP when the pool asks for it, tolerates no challenge at all "
                "under MfaConfiguration=OPTIONAL, and never weakens the pool to avoid one"
            )

        access_token = (response.get("AuthenticationResult") or {}).get("AccessToken")
        if not access_token:
            raise RuntimeError(
                f"no AccessToken issued for {username!r} "
                f"(ChallengeName={response.get('ChallengeName')!r})"
            )
        return str(access_token), auth_factor_detail(mfa_completed=bool(secret))

    def cleanup(self) -> Iterable[CheckResult]:
        """Delete the Cognito user, proving removal by an `admin_get_user` that must raise.

        Yields nothing when no user was ever created, so a provisioning failure before
        `admin_create_user` does not publish a removal check for a resource that never existed.
        """
        username = self._username
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
                    resource=_RESOURCE,
                    check="cognito_user_deleted",
                    status="pass",
                    detail="admin_get_user after deletion raised UserNotFoundException",
                )
                return
            yield CheckResult(
                resource=_RESOURCE,
                check="cognito_user_deleted",
                status="fail",
                detail="admin_get_user still resolves the smoke user after admin_delete_user",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource=_RESOURCE,
                check="cognito_user_deleted",
                status="fail",
                detail=f"could not confirm smoke user deletion: {type(exc).__name__}: {exc}",
            )
