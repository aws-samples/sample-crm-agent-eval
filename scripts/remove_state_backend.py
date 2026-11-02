#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Remove the Terraform state backend created by ``infra/bootstrap_state_backend.sh``.

Destructive, idempotent and non-interactive. It deletes the state bucket (every version and delete
marker), then the access-log bucket, then the DynamoDB lock table, then schedules the KMS key for
deletion with a 7-day window and deletes its alias. A resource that is already absent counts as
success. When the alias is gone, the key is found by its ``service`` tag and bootstrap description.

Usage:
    .venv/bin/python3 scripts/remove_state_backend.py --profile NAME --region REGION --yes
        [--json PATH]

Exit codes:
    0  every backend resource is absent or pending deletion;
    1  a precondition refused with nothing changed (a state file still lists resources, the state
       bucket holds a non-state key or lacks ``STATE_KEY`` while non-empty, a live or malformed lock
       item, an unexpected KMS key, several keys matching when the alias is absent), or an
       action failed;
    2  refused before any AWS call: ``--yes``, ``--profile`` or ``--region`` missing, an
       unwritable ``--json`` path, credential variables in the environment, or STS identity
       resolution failed.

Output and the ``--json`` report name resources with the account id replaced by ``<account>``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

PROJECT: Final = "crm-agent-eval"
STATE_KEY: Final = "crm-agent-eval/terraform.tfstate"

#: The tag the backend KMS key must carry before it is scheduled for deletion.
REQUIRED_KEY_TAG: Final[tuple[str, str]] = ("service", PROJECT)

#: The description ``bootstrap_state_backend.sh`` gives the backend KMS key.
KEY_DESCRIPTION: Final = f"CMK for {PROJECT} Terraform state (S3 + DynamoDB)"

#: Suffix of the state-file keys the precondition parses; any other key in the bucket refuses.
STATE_FILE_SUFFIX: Final = ".tfstate"

#: Suffix of the permanent digest items Terraform keeps in the lock table.
LOCK_DIGEST_SUFFIX: Final = "-md5"

KEY_PENDING_WINDOW_DAYS: Final = 7
DELETE_BATCH_SIZE: Final = 1000
BUCKET_NOT_EMPTY_ATTEMPTS: Final = 5
BUCKET_NOT_EMPTY_DELAY_SECONDS: Final = 30.0

#: Environment credential variables whose presence causes a refusal.
STATIC_CREDENTIAL_VARS: Final[tuple[str, ...]] = ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN")

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_REFUSED: Final = 2

_CLIENT_CONFIG: Final = Config(retries={"mode": "standard", "max_attempts": 5})
_BUCKET_ABSENT_CODES: Final = frozenset({"NoSuchBucket", "404"})
_OBJECT_ABSENT_CODES: Final = frozenset({"NoSuchKey", "NoSuchBucket", "404"})
_SETTLED_OUTCOMES: Final = frozenset({"deleted", "absent", "pending-deletion"})

_sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class BackendNames:
    """The four named backend resources for one account and region."""

    kms_alias: str
    state_bucket: str
    log_bucket: str
    lock_table: str


def backend_names(account_id: str, region: str) -> BackendNames:
    """The backend resource names ``bootstrap_state_backend.sh`` creates in one account/region."""
    return BackendNames(
        kms_alias=f"alias/{PROJECT}-tfstate",
        state_bucket=f"{PROJECT}-tfstate-{account_id}-{region}",
        log_bucket=f"{PROJECT}-tfstate-logs-{account_id}-{region}",
        lock_table=f"{PROJECT}-tflock",
    )


class PreconditionRefusedError(Exception):
    """A read-only precondition failed; nothing has been changed."""


class ActionFailedError(Exception):
    """A destructive action did not reach its end state."""


@dataclass
class Outcome:
    """The end state of one backend resource."""

    resource: str
    name: str
    outcome: str
    detail: str = ""


@dataclass
class Report:
    """Per-resource outcomes plus the overall result, serialisable to the ``--json`` report."""

    result: str = "refused"
    exit_code: int = EXIT_REFUSED
    message: str = ""
    resources: list[Outcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "exit_code": self.exit_code,
            "message": self.message,
            "resources": [vars(item) for item in self.resources],
        }


def _default_session(profile: str, region: str) -> boto3.Session:
    """A boto3 session bound to the named profile and region."""
    return boto3.Session(profile_name=profile, region_name=region)


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def lock_ids_refusing(lock_ids: Sequence[str]) -> list[str]:
    """The lock-table ``LockID`` values that are live or stale locks, not ``-md5`` digests."""
    return [lock_id for lock_id in lock_ids if not lock_id.endswith(LOCK_DIGEST_SUFFIX)]


def key_identity_problem(metadata: dict[str, Any], tags: Sequence[dict[str, str]]) -> str | None:
    """Why the key may not be scheduled for deletion, or ``None`` when it is the backend key."""
    manager = metadata.get("KeyManager")
    if manager != "CUSTOMER":
        return f"KeyManager is {manager!r}, not 'CUSTOMER'"
    tag_key, tag_value = REQUIRED_KEY_TAG
    values = [tag.get("TagValue") for tag in tags if tag.get("TagKey") == tag_key]
    if values != [tag_value]:
        return f"tag {tag_key}={tag_value} is absent (found {values!r})"
    return None


def key_lookup_match(metadata: dict[str, Any], tags: Sequence[dict[str, str]]) -> bool:
    """Whether a key found without its alias is the backend key (CUSTOMER, tag, description)."""
    return (
        key_identity_problem(metadata, tags) is None
        and metadata.get("Description") == KEY_DESCRIPTION
    )


def state_resource_count(body: bytes) -> int:
    """The number of entries in the state document's ``resources`` list.

    Raises:
        PreconditionRefusedError: the body is not JSON or has no ``resources`` list.
    """
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreconditionRefusedError(f"state object is not valid JSON: {exc}") from exc
    resources = document.get("resources") if isinstance(document, dict) else None
    if not isinstance(resources, list):
        raise PreconditionRefusedError("state object has no 'resources' list")
    return len(resources)


class BackendRemover:
    """Runs the preconditions and ordered removal actions against one session."""

    def __init__(
        self,
        session: Any,
        names: BackendNames,
        account_id: str,
        redact: Callable[[str], str],
    ) -> None:
        self._s3 = session.client("s3", config=_CLIENT_CONFIG)
        self._dynamodb = session.client("dynamodb", config=_CLIENT_CONFIG)
        self._kms = session.client("kms", config=_CLIENT_CONFIG)
        self._names = names
        self._owner = account_id
        self._redact = redact
        self._alias_present = False
        self._key_id: str | None = None
        self._key_state: str | None = None

    def check_preconditions(self) -> None:
        """Read-only checks; raises :class:`PreconditionRefusedError` on the first failure."""
        self._check_state_object()
        self._check_lock_table()
        self._check_key_identity()

    def _check_state_object(self) -> None:
        bucket = self._names.state_bucket
        shown = self._redact(bucket)
        try:
            keys = [
                str(entry["Key"])
                for page in self._s3.get_paginator("list_objects_v2").paginate(
                    Bucket=bucket, ExpectedBucketOwner=self._owner
                )
                for entry in page.get("Contents", [])
            ]
        except ClientError as exc:
            if _error_code(exc) in _BUCKET_ABSENT_CODES:
                return
            raise PreconditionRefusedError(f"cannot list bucket {shown}: {exc}") from exc
        if keys and STATE_KEY not in keys:
            raise PreconditionRefusedError(
                f"bucket {shown} holds {len(keys)} object(s) but no {STATE_KEY}"
            )
        unknown = [key for key in keys if not key.endswith(STATE_FILE_SUFFIX)]
        if unknown:
            raise PreconditionRefusedError(
                f"bucket {shown} holds {len(unknown)} non-state object(s), e.g. {unknown[0]!r}"
            )
        for key in keys:
            count = self._state_file_resource_count(key)
            if count:
                raise PreconditionRefusedError(
                    f"state file s3://{shown}/{key} still lists {count} resource(s); "
                    "run scripts/teardown.sh first"
                )

    def _state_file_resource_count(self, state_file: str) -> int:
        try:
            response = self._s3.get_object(
                Bucket=self._names.state_bucket,
                Key=state_file,
                ExpectedBucketOwner=self._owner,
            )
        except ClientError as exc:
            if _error_code(exc) in _OBJECT_ABSENT_CODES:
                return 0
            raise PreconditionRefusedError(f"cannot read state file {state_file}: {exc}") from exc
        try:
            return state_resource_count(response["Body"].read())
        except PreconditionRefusedError as exc:
            raise PreconditionRefusedError(f"{state_file}: {exc}") from exc

    def _check_lock_table(self) -> None:
        paginator = self._dynamodb.get_paginator("scan")
        lock_ids: list[str] = []
        try:
            for page in paginator.paginate(
                TableName=self._names.lock_table, ProjectionExpression="LockID"
            ):
                lock_ids.extend(str(item["LockID"]["S"]) for item in page.get("Items", []))
        except ClientError as exc:
            if _error_code(exc) == "ResourceNotFoundException":
                return
            raise PreconditionRefusedError(f"cannot scan the lock table: {exc}") from exc
        except (KeyError, TypeError) as exc:
            raise PreconditionRefusedError(
                f"lock table {self._names.lock_table} holds a malformed item: {exc!r}"
            ) from exc
        live = lock_ids_refusing(lock_ids)
        if live:
            raise PreconditionRefusedError(
                f"lock table {self._names.lock_table} holds {len(live)} live or stale lock "
                "item(s); release them with 'terraform force-unlock <LOCK_ID>' first"
            )

    def _key_tags(self, key_id: str) -> list[dict[str, str]]:
        try:
            return [
                tag
                for page in self._kms.get_paginator("list_resource_tags").paginate(KeyId=key_id)
                for tag in page.get("Tags", [])
            ]
        except ClientError as exc:
            raise PreconditionRefusedError(f"cannot list tags of key {key_id}: {exc}") from exc

    def _check_key_identity(self) -> None:
        try:
            metadata = self._kms.describe_key(KeyId=self._names.kms_alias)["KeyMetadata"]
        except ClientError as exc:
            if _error_code(exc) == "NotFoundException":
                self._find_key_without_alias()
                return
            raise PreconditionRefusedError(
                f"cannot describe {self._names.kms_alias}: {exc}"
            ) from exc
        self._alias_present = True
        key_id = str(metadata["KeyId"])
        problem = key_identity_problem(metadata, self._key_tags(key_id))
        if problem is not None:
            raise PreconditionRefusedError(
                f"key behind {self._names.kms_alias} ({key_id}) is not the backend key: {problem}"
            )
        self._key_id = key_id
        self._key_state = str(metadata.get("KeyState", ""))

    def _find_key_without_alias(self) -> None:
        """Resolve the backend key by tag and description; several candidates refuse."""
        candidates: list[str] = []
        try:
            for page in self._kms.get_paginator("list_keys").paginate():
                for entry in page.get("Keys", []):
                    key_id = str(entry["KeyId"])
                    metadata = self._kms.describe_key(KeyId=key_id)["KeyMetadata"]
                    if metadata.get("KeyManager") != "CUSTOMER":
                        continue
                    if metadata.get("KeyState") == "PendingDeletion":
                        continue
                    if key_lookup_match(metadata, self._key_tags(key_id)):
                        candidates.append(key_id)
        except ClientError as exc:
            raise PreconditionRefusedError(f"cannot look up the key by tag: {exc}") from exc
        if len(candidates) > 1:
            raise PreconditionRefusedError(
                f"alias {self._names.kms_alias} is absent and {len(candidates)} keys match the "
                f"tag and description ({', '.join(candidates)}); schedule the right one by key id"
            )
        if candidates:
            self._key_id = candidates[0]

    def remove_bucket(self, bucket: str) -> Outcome:
        """Delete every version and delete marker, then the bucket, retrying ``BucketNotEmpty``."""
        shown = self._redact(bucket)
        for attempt in range(1, BUCKET_NOT_EMPTY_ATTEMPTS + 1):
            try:
                self._empty_bucket(bucket)
                self._s3.delete_bucket(Bucket=bucket, ExpectedBucketOwner=self._owner)
            except ClientError as exc:
                code = _error_code(exc)
                if code in _BUCKET_ABSENT_CODES:
                    return Outcome("s3-bucket", shown, "absent", f"attempts={attempt}")
                if code == "BucketNotEmpty" and attempt < BUCKET_NOT_EMPTY_ATTEMPTS:
                    _sleep(BUCKET_NOT_EMPTY_DELAY_SECONDS)
                    continue
                if code == "BucketNotEmpty":
                    raise ActionFailedError(
                        f"bucket {shown} still not empty after {attempt} attempts; re-run later"
                    ) from exc
                raise ActionFailedError(f"bucket {shown}: {exc}") from exc
            return Outcome("s3-bucket", shown, "deleted", f"attempts={attempt}")
        raise AssertionError("unreachable")  # pragma: no cover

    def _object_versions(self, bucket: str) -> Iterator[dict[str, str]]:
        paginator = self._s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, ExpectedBucketOwner=self._owner):
            for entry in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
                yield {"Key": entry["Key"], "VersionId": entry["VersionId"]}

    def _empty_bucket(self, bucket: str) -> None:
        pending = list(self._object_versions(bucket))
        for start in range(0, len(pending), DELETE_BATCH_SIZE):
            batch = pending[start : start + DELETE_BATCH_SIZE]
            response = self._s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": batch, "Quiet": True},
                ExpectedBucketOwner=self._owner,
            )
            errors = response.get("Errors", [])
            if errors:
                first = errors[0]
                raise ActionFailedError(
                    f"bucket {self._redact(bucket)}: {len(errors)} object(s) not deleted "
                    f"(first: {first.get('Code')} {first.get('Message')})"
                )

    def remove_lock_table(self) -> Outcome:
        """Delete the lock table and wait until it no longer exists."""
        table = self._names.lock_table
        try:
            self._dynamodb.delete_table(TableName=table)
        except ClientError as exc:
            if _error_code(exc) == "ResourceNotFoundException":
                return Outcome("dynamodb-table", table, "absent")
            raise ActionFailedError(f"lock table {table}: {exc}") from exc
        try:
            self._dynamodb.get_waiter("table_not_exists").wait(TableName=table)
        except BotoCoreError as exc:
            raise ActionFailedError(f"lock table {table} did not disappear: {exc}") from exc
        return Outcome("dynamodb-table", table, "deleted")

    def remove_key(self) -> list[Outcome]:
        """Schedule the backend key for deletion, then delete its alias."""
        alias = self._names.kms_alias
        outcomes = [self._schedule_key()]
        if not self._alias_present:
            outcomes.append(Outcome("kms-alias", alias, "absent"))
            return outcomes
        try:
            self._kms.delete_alias(AliasName=alias)
        except ClientError as exc:
            if _error_code(exc) != "NotFoundException":
                raise ActionFailedError(f"alias {alias}: {exc}") from exc
            outcomes.append(Outcome("kms-alias", alias, "absent"))
            return outcomes
        outcomes.append(Outcome("kms-alias", alias, "deleted"))
        return outcomes

    def _schedule_key(self) -> Outcome:
        if self._key_id is None:
            return Outcome("kms-key", "", "absent", "no alias and no matching key")
        key_id = self._key_id
        if self._key_state == "PendingDeletion":
            return Outcome("kms-key", key_id, "pending-deletion", "already scheduled")
        try:
            response = self._kms.schedule_key_deletion(
                KeyId=key_id, PendingWindowInDays=KEY_PENDING_WINDOW_DAYS
            )
        except ClientError as exc:
            raise ActionFailedError(f"key {key_id}: schedule_key_deletion failed: {exc}") from exc
        window = response.get("PendingWindowInDays", KEY_PENDING_WINDOW_DAYS)
        return Outcome("kms-key", key_id, "pending-deletion", f"window_days={window}")


def build_arg_parser() -> argparse.ArgumentParser:
    """The CLI parser; missing required values are checked by :func:`main`, not argparse."""
    parser = argparse.ArgumentParser(
        description="Remove the crm-agent-eval Terraform state backend (destructive)."
    )
    parser.add_argument("--profile", help="AWS profile name (required)")
    parser.add_argument("--region", help="AWS region (required)")
    parser.add_argument("--yes", action="store_true", help="confirm the destructive removal")
    parser.add_argument("--json", type=Path, help="write the per-resource report here")
    return parser


def _emit(report: Report, json_path: Path | None) -> int:
    for item in report.resources:
        suffix = f" ({item.detail})" if item.detail else ""
        print(f"{item.resource} {item.name}: {item.outcome}{suffix}")
    stream = sys.stdout if report.exit_code == EXIT_OK else sys.stderr
    print(f"{report.result.upper()}: {report.message}", file=stream)
    if json_path is not None:
        json_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    return report.exit_code


def json_path_problem(path: Path) -> str | None:
    """Why the ``--json`` report could not be written to ``path``, or ``None`` when it can."""
    if path.is_dir():
        return f"--json {path} is a directory"
    if path.exists():
        return None if os.access(path, os.W_OK) else f"--json {path} is not writable"
    parent = path.parent
    if not parent.is_dir():
        return f"--json directory {parent} does not exist"
    return None if os.access(parent, os.W_OK) else f"--json directory {parent} is not writable"


def _refuse(report: Report, message: str, json_path: Path | None) -> int:
    report.result, report.exit_code = "refused", EXIT_REFUSED
    report.message = f"{message}. Nothing changed."
    return _emit(report, json_path)


def main(
    argv: Sequence[str] | None = None,
    session_factory: Callable[[str, str], boto3.Session] = _default_session,
) -> int:
    """Run the removal; returns the process exit code (0, 1 or 2, see the module docstring)."""
    args = build_arg_parser().parse_args(argv)
    report = Report()
    missing = [flag for flag in ("profile", "region") if not getattr(args, flag)]
    if missing:
        return _refuse(report, f"missing --{', --'.join(missing)}", args.json)
    if not args.yes:
        return _refuse(report, "--yes is required to remove the state backend", args.json)
    if args.json is not None:
        problem = json_path_problem(args.json)
        if problem is not None:
            return _refuse(report, problem, None)
    present = [name for name in STATIC_CREDENTIAL_VARS if os.environ.get(name)]
    if present:
        return _refuse(
            report,
            f"{'/'.join(present)} set in the environment; environment credential variables "
            "cause a refusal. Unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN",
            args.json,
        )
    try:
        session = session_factory(args.profile, args.region)
        identity = session.client("sts", config=_CLIENT_CONFIG).get_caller_identity()
        account_id = str(identity["Account"])
    except (BotoCoreError, ClientError, KeyError) as exc:
        return _refuse(report, f"cannot resolve the caller identity: {exc}", args.json)

    def redact(text: str) -> str:
        return text.replace(account_id, "<account>")

    names = backend_names(account_id, args.region)
    try:
        remover = BackendRemover(session, names, account_id, redact)
        remover.check_preconditions()
    except (PreconditionRefusedError, BotoCoreError, ClientError) as exc:
        report.result, report.exit_code = "refused", EXIT_FAILED
        report.message = f"precondition failed: {redact(str(exc))}. Nothing changed."
        return _emit(report, args.json)

    try:
        report.resources.append(remover.remove_bucket(names.state_bucket))
        report.resources.append(remover.remove_bucket(names.log_bucket))
        report.resources.append(remover.remove_lock_table())
        report.resources.extend(remover.remove_key())
    except Exception as exc:
        report.result, report.exit_code = "failed", EXIT_FAILED
        report.message = redact(f"{type(exc).__name__}: {exc}")
        return _emit(report, args.json)
    unsettled = [item for item in report.resources if item.outcome not in _SETTLED_OUTCOMES]
    if unsettled:
        report.result, report.exit_code = "failed", EXIT_FAILED
        report.message = f"{len(unsettled)} resource(s) not settled: " + ", ".join(
            f"{item.resource} {item.outcome}" for item in unsettled
        )
        return _emit(report, args.json)
    report.result, report.exit_code = "ok", EXIT_OK
    report.message = "every state-backend resource is absent or pending deletion"
    return _emit(report, args.json)


if __name__ == "__main__":
    sys.exit(main())
