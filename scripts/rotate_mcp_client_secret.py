#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Rotate the MCP app client's secret in place, keeping its client id.

Usage:
    scripts/rotate_mcp_client_secret.py --outputs-json results/tf-outputs.json \\
        --secret-out ~/.local/state/crm-eval/<stack>/mcp-client-secret.new \\
        [--old-secret-file PATH] [--profile NAME] [--region REGION]

The old secret is read from ``--old-secret-file`` when given (its stripped contents), else from the
``cognito_mcp_client_secret`` output. After a rotation that output is empty, so a later rotation
passes the previous ``--secret-out`` file as ``--old-secret-file``. An empty output with no file,
or a file that disagrees with a non-empty output, is a usage error.

Profile and region resolve as flag, then ``AWS_PROFILE``/``AWS_REGION``, then the config default,
and the resolved pair is passed explicitly to the boto3 session. The session's account and region
must match the outputs document before anything is changed.

Sequence: refuse unless exactly one secret is active; ``AddUserPoolClientSecret`` (Cognito
generates the value), written 0600 to ``--secret-out``; mint a ``client_credentials`` token with
the new secret (200); mint with the old secret (200);
``DeleteUserPoolClientSecret`` on the old ``ClientSecretId``; mint with the old secret again,
expecting ``invalid_client``. The new-secret mint is retried up to ``PROPAGATION_ATTEMPTS``
times and that last check up to ``OLD_SECRET_REFUSAL_ATTEMPTS`` times. If the new secret never
mints, it is deleted again, its file is removed and the old secret is left intact.

Secret values are never printed or logged; output carries only ids, dates, HTTP statuses and
OAuth error codes. ``--secret-out`` must lie outside any git work tree and must not exist.

Exit status: ``0`` rotated; ``1`` refused or failed (the message names the step and which secrets
remain active); ``2`` usage error.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "src"):  # pragma: no cover - import bootstrap
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.provision_demo_user import (  # noqa: E402
    TargetMismatchError,
    assert_target_matches_outputs,
)

from crm.config import resolve_aws_config  # noqa: E402
from crm.errors import ValidationFailed  # noqa: E402

#: ``(url, form body, headers) -> (HTTP status, response body)``; HTTP error statuses are returned.
TokenTransport = Callable[[str, bytes, Mapping[str, str]], tuple[int, bytes]]

#: Attempts and spacing for the new-secret mint that waits on Cognito propagation.
PROPAGATION_ATTEMPTS: Final[int] = 6
PROPAGATION_DELAY_SECONDS: Final[float] = 5.0

#: Attempts and spacing for the deleted-secret mint that waits for ``invalid_client``.
OLD_SECRET_REFUSAL_ATTEMPTS: Final[int] = 30
OLD_SECRET_REFUSAL_DELAY_SECONDS: Final[float] = 10.0

_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_SECRET_FILE_MODE: Final[int] = 0o600


class OldSecretError(ValueError):
    """The old secret cannot be established from ``--old-secret-file`` and the outputs."""


class RotationError(RuntimeError):
    """A rotation step failed or was refused; the message names the step and the remaining state."""


@dataclass(frozen=True)
class RotationTarget:
    """The client to rotate, read from the outputs document."""

    user_pool_id: str
    client_id: str
    domain: str
    old_secret: str

    def token_endpoint(self, region: str) -> str:
        """The Cognito prefix domain's ``/oauth2/token`` URL in ``region``."""
        return f"https://{self.domain}.auth.{region}.amazoncognito.com/oauth2/token"


@dataclass(frozen=True)
class MintResult:
    """The outcome of one ``client_credentials`` request; never holds the token."""

    status: int
    error: str | None

    @property
    def ok(self) -> bool:
        """The endpoint answered 200 with an access token."""
        return self.status == 200 and self.error is None

    def describe(self) -> str:
        """``HTTP <status>`` plus the OAuth error code, if any."""
        return f"HTTP {self.status}" + (f" ({self.error})" if self.error else "")


def _output_str(outputs: Mapping[str, Any], name: str) -> str:
    entry = outputs.get(name)
    value = entry.get("value") if isinstance(entry, dict) else entry
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"terraform output {name!r} is absent or not a non-empty string")
    return value


def old_secret_from(outputs: Mapping[str, Any], old_secret_file: Path | None) -> str:
    """The current secret: ``old_secret_file``'s stripped contents, else the outputs' secret.

    Raises:
        OldSecretError: the output is empty and no file is given; the file is unreadable or empty;
            or the file and a non-empty output disagree. The message never carries a value.
    """
    entry = outputs.get("cognito_mcp_client_secret")
    value = entry.get("value") if isinstance(entry, dict) else entry
    from_outputs = value.strip() if isinstance(value, str) and value.strip() else None
    if old_secret_file is None:
        if from_outputs is None:
            raise OldSecretError(
                "terraform output 'cognito_mcp_client_secret' is absent or empty; pass "
                "--old-secret-file with the file the previous rotation wrote to --secret-out"
            )
        return from_outputs
    path = old_secret_file.expanduser()
    try:
        from_file = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OldSecretError(
            f"--old-secret-file {path} is unreadable ({type(exc).__name__})"
        ) from None
    if not from_file:
        raise OldSecretError(f"--old-secret-file {path} is empty")
    if from_outputs is not None and from_outputs != from_file:
        raise OldSecretError(
            f"--old-secret-file {path} and terraform output 'cognito_mcp_client_secret' hold "
            "different secrets; pass only the one that is active"
        )
    return from_file


def target_from_outputs(
    outputs: Mapping[str, Any], old_secret_file: Path | None = None
) -> RotationTarget:
    """Read the user pool id, MCP client id and Cognito domain from ``outputs``, and the old secret.

    Raises:
        ValueError: an output is absent or empty. The message never carries a value.
        OldSecretError: see :func:`old_secret_from`.
    """
    return RotationTarget(
        user_pool_id=_output_str(outputs, "cognito_user_pool_id"),
        client_id=_output_str(outputs, "cognito_mcp_client_id"),
        domain=_output_str(outputs, "cognito_domain"),
        old_secret=old_secret_from(outputs, old_secret_file),
    )


def assert_outputs_name_target(outputs: Mapping[str, Any]) -> None:
    """Raise :class:`TargetMismatchError` unless ``outputs`` names an account and a region."""
    from scripts.smoke.harness import TerraformOutputs, outputs_accounts, outputs_regions

    described = TerraformOutputs(values=dict(outputs))
    if not outputs_accounts(described) or not outputs_regions(described):
        raise TargetMismatchError(
            "the outputs document names no account or no region in any ARN, so the target cannot "
            "be verified. Nothing has been changed."
        )


def _inside_git_work_tree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in path.parents)


def validate_secret_out(raw: Path) -> Path:
    """Return the absolute ``--secret-out`` path, or raise ``ValueError`` if it may not be used.

    Refused: a path inside any git work tree, an existing path (a dangling symlink included), and
    a parent that is not an existing directory.
    """
    path = raw.expanduser().absolute()
    if _inside_git_work_tree(path.parent.resolve()) or _inside_git_work_tree(path):
        raise ValueError(f"--secret-out {path} lies inside a git work tree")
    if os.path.lexists(path):
        raise ValueError(f"--secret-out {path} already exists; it is never overwritten")
    if not path.parent.is_dir():
        raise ValueError(f"--secret-out's directory {path.parent} does not exist")
    return path


def write_secret_file(path: Path, value: str) -> None:
    """Create ``path`` with mode 0600 and write ``value``; fails if ``path`` already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _SECRET_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    path.chmod(_SECRET_FILE_MODE)


def https_transport(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
    """POST ``body`` to ``url`` over HTTPS only; an HTTP error status is returned, not raised."""
    from scripts.smoke.harness import open_https

    request = urllib.request.Request(  # noqa: S310 - https-only opener; URL from outputs
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with open_https(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def mint(transport: TokenTransport, endpoint: str, client_id: str, secret: str) -> MintResult:
    """Request a ``client_credentials`` token and classify the answer; the token is discarded.

    Raises:
        RotationError: no complete answer came back (``URLError``, ``OSError``, ``HTTPException``).
    """
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic}",
    }
    try:
        status, raw = transport(endpoint, body, headers)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise RotationError(f"token endpoint unreachable: {type(exc).__name__}") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return MintResult(status=status, error="unparseable_response")
    error = payload.get("error")
    if isinstance(error, str) and error:
        return MintResult(status=status, error=error)
    if status == 200 and not payload.get("access_token"):
        return MintResult(status=status, error="no_access_token")
    return MintResult(status=status, error=None)


def _mint_until(
    predicate: Callable[[MintResult], bool],
    attempt: Callable[[], MintResult],
    sleep: Callable[[float], None],
    attempts: int,
    delay_seconds: float,
) -> MintResult:
    result = attempt()
    for _ in range(attempts - 1):
        if predicate(result):
            break
        sleep(delay_seconds)
        result = attempt()
    return result


def list_secret_ids(idp: Any, target: RotationTarget) -> list[tuple[str, str]]:
    """``(ClientSecretId, create date)`` of every active secret, following ``NextToken``."""
    found: list[tuple[str, str]] = []
    kwargs: dict[str, str] = {"UserPoolId": target.user_pool_id, "ClientId": target.client_id}
    while True:
        page = idp.list_user_pool_client_secrets(**kwargs)
        for item in page.get("ClientSecrets", []):
            found.append((str(item["ClientSecretId"]), str(item.get("ClientSecretCreateDate", ""))))
        token = page.get("NextToken")
        if not token:
            return found
        kwargs["NextToken"] = token


def rotate(
    idp: Any,
    target: RotationTarget,
    *,
    region: str,
    secret_out: Path,
    transport: TokenTransport,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> None:
    """Run the rotation sequence against ``idp`` (a ``cognito-idp`` client).

    Raises:
        RotationError: a refusal or a failed step; the message names it and the remaining state.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    endpoint = target.token_endpoint(region)

    try:
        active = list_secret_ids(idp, target)
    except (BotoCoreError, ClientError) as exc:
        raise RotationError(f"step list: ListUserPoolClientSecrets failed: {exc}") from None
    if len(active) != 1:
        raise RotationError(
            f"step list: {len(active)} active secret(s) on client {target.client_id}; exactly 1 "
            f"is required. Ids: {', '.join(i for i, _ in active) or '(none)'}. Nothing changed."
        )
    old_id, old_created = active[0]
    log(f"active secret: {old_id} (created {old_created})")

    try:
        added = idp.add_user_pool_client_secret(
            UserPoolId=target.user_pool_id, ClientId=target.client_id
        )["ClientSecretDescriptor"]
        new_id = str(added["ClientSecretId"])
        new_value = str(added["ClientSecretValue"])
    except (BotoCoreError, ClientError, KeyError) as exc:
        raise RotationError(
            f"step add: AddUserPoolClientSecret failed ({type(exc).__name__}); "
            f"check the client's secrets before re-running. Old secret {old_id} untouched."
        ) from None
    log(f"added secret: {new_id} (created {added.get('ClientSecretCreateDate', '')})")

    def roll_back_new(step: str, reason: str, *, remove_file: bool = True) -> RotationError:
        detail = f"step {step}: {reason}."
        try:
            idp.delete_user_pool_client_secret(
                UserPoolId=target.user_pool_id, ClientId=target.client_id, ClientSecretId=new_id
            )
        except (BotoCoreError, ClientError) as exc:
            return RotationError(
                f"{detail} Deleting new secret {new_id} ALSO failed ({type(exc).__name__}); "
                f"both {old_id} and {new_id} remain active."
            )
        try:
            if remove_file:
                secret_out.unlink(missing_ok=True)
        except OSError as exc:
            return RotationError(
                f"{detail} New secret {new_id} deleted; old secret {old_id} intact; removing the "
                f"dead {secret_out} failed ({type(exc).__name__})."
            )
        return RotationError(f"{detail} New secret {new_id} deleted; old secret {old_id} intact.")

    try:
        write_secret_file(secret_out, new_value)
    except OSError as exc:
        raise roll_back_new(
            "write",
            f"writing --secret-out failed ({type(exc).__name__})",
            remove_file=not isinstance(exc, FileExistsError),
        ) from None
    log(f"new secret written to {secret_out} (mode 0600)")

    try:
        new_mint = _mint_until(
            lambda r: r.ok,
            lambda: mint(transport, endpoint, target.client_id, new_value),
            sleep,
            PROPAGATION_ATTEMPTS,
            PROPAGATION_DELAY_SECONDS,
        )
    except RotationError as exc:
        raise roll_back_new("mint-new", str(exc)) from None
    log(f"mint with new secret: {new_mint.describe()}")
    if not new_mint.ok:
        raise roll_back_new("mint-new", f"new secret did not mint ({new_mint.describe()})")

    def remaining(step: str) -> RotationError:
        return RotationError(
            f"step {step}: token endpoint unreachable. Both {old_id} and {new_id} remain "
            f"active; the new value is in {secret_out}."
        )

    try:
        old_mint = mint(transport, endpoint, target.client_id, target.old_secret)
    except RotationError:
        raise remaining("mint-old") from None
    log(f"mint with old secret: {old_mint.describe()}")
    if not old_mint.ok:
        raise RotationError(
            f"step mint-old: the outputs' secret did not mint ({old_mint.describe()}), so it may "
            f"not be secret {old_id}. Both {old_id} and {new_id} remain active; the new value is "
            f"in {secret_out}."
        )

    try:
        idp.delete_user_pool_client_secret(
            UserPoolId=target.user_pool_id, ClientId=target.client_id, ClientSecretId=old_id
        )
    except (BotoCoreError, ClientError) as exc:
        raise RotationError(
            f"step delete-old: DeleteUserPoolClientSecret failed ({type(exc).__name__}). Both "
            f"{old_id} and {new_id} remain active; the new value is in {secret_out}."
        ) from None
    log(f"deleted secret: {old_id}")

    try:
        dead = _mint_until(
            lambda r: r.error == "invalid_client",
            lambda: mint(transport, endpoint, target.client_id, target.old_secret),
            sleep,
            OLD_SECRET_REFUSAL_ATTEMPTS,
            OLD_SECRET_REFUSAL_DELAY_SECONDS,
        )
    except RotationError:
        raise RotationError(
            f"step verify-old-refused: token endpoint unreachable after deleting {old_id}. "
            f"Secret {new_id} is active; the new value is in {secret_out}."
        ) from None
    log(f"mint with deleted secret: {dead.describe()}")
    if dead.error != "invalid_client":
        raise RotationError(
            f"step verify-old-refused: expected invalid_client after deleting {old_id}, got "
            f"{dead.describe()}. Secret {new_id} is active; the new value is in {secret_out}."
        )


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outputs-json", type=Path, required=True, help="File holding `terraform output -json`"
    )
    parser.add_argument(
        "--secret-out",
        type=Path,
        required=True,
        help="New file (outside any git work tree) that receives the new secret, mode 0600",
    )
    parser.add_argument(
        "--old-secret-file",
        type=Path,
        help="File holding the active secret (the previous rotation's --secret-out); required "
        "when the cognito_mcp_client_secret output is empty",
    )
    parser.add_argument("--profile", help="AWS profile (default: AWS_PROFILE, then config)")
    parser.add_argument("--region", help="AWS region (default: AWS_REGION, then config)")
    return parser


def _default_session(profile: str, region: str) -> Any:
    import boto3

    return boto3.session.Session(profile_name=profile, region_name=region)


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[str, str], Any] = _default_session,
    transport: TokenTransport = https_transport,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """CLI entry point; see the module docstring for the exit status."""
    args = build_arg_parser().parse_args(argv)

    try:
        outputs = json.loads(args.outputs_json.read_text(encoding="utf-8"))
        if not isinstance(outputs, dict):
            raise ValueError("the outputs document is not a JSON object")
        target = target_from_outputs(outputs, args.old_secret_file)
        assert_outputs_name_target(outputs)
        secret_out = validate_secret_out(args.secret_out)
        aws = resolve_aws_config(profile=args.profile, region=args.region)
    except OldSecretError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TargetMismatchError, ValidationFailed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from botocore.exceptions import BotoCoreError, ClientError

    try:
        session = session_factory(aws.profile, aws.region)
        assert_target_matches_outputs(
            session, outputs=outputs, region=aws.region, profile=aws.profile
        )
        idp = session.client("cognito-idp", region_name=aws.region)
    except (BotoCoreError, ClientError, TargetMismatchError) as exc:
        print(f"error: target check failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"target: user pool {target.user_pool_id}, client {target.client_id}, region "
        f"{aws.region} ({aws.region_source}), profile {aws.profile} ({aws.profile_source})"
    )

    try:
        rotate(
            idp, target, region=aws.region, secret_out=secret_out, transport=transport, sleep=sleep
        )
    except RotationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"rotated: client {target.client_id} keeps its id; new secret in {secret_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
