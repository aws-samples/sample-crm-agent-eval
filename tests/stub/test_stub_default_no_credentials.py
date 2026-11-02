# SPDX-License-Identifier: MIT
"""The default CLI entry point runs credential-free.

Calls :func:`eval.harness.main` with every AWS credential environment variable removed, no
``mock_aws()``, and no injected store, against DynamoDB Local on loopback. Fails rather than skips
when DynamoDB Local is unreachable.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from eval.harness import _DEFAULT_LOCAL_ENDPOINT_URL, main
from tests._dynamodb_local import ENDPOINT_URL, START_SCRIPT, ensure_started, start_lock_dir

#: Placeholder credential DynamoDB Local accepts; not a secret.
_LOCAL_ENGINE_CRED = "local"
#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"nocred-{uuid.uuid4().hex[:12]}-"

#: Every AWS credential/config environment variable botocore's ambient chain could consult.
_AWS_CREDENTIAL_ENV_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
)


@pytest.fixture(scope="module", autouse=True)
def _require_dynamodb_local(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Fail outright (never skip) if DynamoDB Local is unreachable and cannot be started."""
    if ensure_started(start_lock_dir(tmp_path_factory)):
        return
    pytest.fail(
        f"DynamoDB Local is not reachable at {ENDPOINT_URL} and could not be started "
        f"automatically. This module proves the credential-free default path and must run "
        f"against the real engine, not skip. Run '{START_SCRIPT} start' (requires Docker) first."
    )


@pytest.fixture(autouse=True)
def _strip_every_aws_credential_and_config_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every AWS credential/config environment variable, including region and profile.

    No dummy credential is set back.
    """
    for var in _AWS_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def _local_tables() -> Iterator[None]:
    """Create every declared table on the real local engine under this module's own prefix.

    Uses a throwaway ``boto3`` client owned by this fixture to provision and drop the tables.
    """
    import boto3

    client: Any = boto3.client(
        "dynamodb",
        endpoint_url=_DEFAULT_LOCAL_ENDPOINT_URL,
        region_name="us-east-1",
        aws_access_key_id=_LOCAL_ENGINE_CRED,
        aws_secret_access_key=_LOCAL_ENGINE_CRED,
    )
    create_all_tables(client, table_prefix=_TABLE_PREFIX)
    try:
        yield
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(client.exceptions.ResourceNotFoundException):
                client.delete_table(TableName=f"{_TABLE_PREFIX}{table}")


def test_default_entry_point_runs_with_zero_aws_credentials_present(
    _local_tables: None, tmp_path: Path
) -> None:
    """``main()`` with only ``--output-dir`` and ``--table-prefix`` and no AWS credential exits 0
    and writes the json/md/csv matrix artifacts."""
    exit_code = main(
        [
            "--output-dir",
            str(tmp_path),
            "--table-prefix",
            _TABLE_PREFIX,
        ]
    )

    assert exit_code == 0
    assert (tmp_path / "matrix.json").exists()
    assert (tmp_path / "matrix.md").exists()
    assert (tmp_path / "matrix.csv").exists()


def test_default_entry_point_never_contacts_a_real_aws_endpoint(
    _local_tables: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default path completes without consulting botocore's credential-resolution chain,
    which is patched to raise."""
    import botocore.credentials

    def _explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "the default CLI entry point must never resolve AWS credentials through botocore's "
            "ambient credential-resolution chain"
        )

    monkeypatch.setattr(
        botocore.credentials.CredentialResolver, "load_credentials", _explode, raising=False
    )

    exit_code = main(
        [
            "--output-dir",
            str(tmp_path),
            "--table-prefix",
            _TABLE_PREFIX,
        ]
    )

    assert exit_code == 0
