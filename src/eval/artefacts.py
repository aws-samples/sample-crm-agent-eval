# SPDX-License-Identifier: MIT
"""Short-lived presigned GET URLs for one eval run's artefacts.

:func:`artefact_url` authorizes the caller, checks the run and the requested key, and presigns a
``GetObject`` for exactly ``s3://<EVAL_ARTIFACTS_BUCKET>/<EVAL_ARTIFACTS_PREFIX>/<run_id>/<key>``,
valid for :data:`ARTEFACT_URL_EXPIRES_SECONDS`. The bucket stays private: the URL carries the
signer's authority for that one object only.

A key is accepted only when it is one of the paths the run recorded in ``artifact_links``
(``matrix_json``, ``matrix_md``, ``matrix_csv``, ``raw[]``); traversal, absolute, encoded or
empty keys are refused before that lookup.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from crm.authz import Identity
from crm.errors import CrmError, NotFound, ScopeDenied, ValidationFailed
from crm.store.store import Store

#: How long a presigned artefact URL stays valid.
ARTEFACT_URL_EXPIRES_SECONDS: Final[int] = 300

#: The scope a caller lacks when it may not read a run's artefacts.
EVAL_ARTEFACTS_SCOPE: Final[str] = "eval:artefacts"

BUCKET_ENV_VAR: Final[str] = "EVAL_ARTIFACTS_BUCKET"
PREFIX_ENV_VAR: Final[str] = "EVAL_ARTIFACTS_PREFIX"

_MAX_KEY_LENGTH: Final[int] = 512

#: Characters a recorded artefact path is made of; anything else is refused unread.
_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*")

#: The single-path entries of a run's ``artifact_links``; ``raw`` is a list of paths.
_LINK_FIELDS: Final[tuple[str, ...]] = ("matrix_json", "matrix_md", "matrix_csv")


def artefact_object_key(prefix: str, run_id: str, relative: str) -> str:
    """The S3 key of a run artefact: ``<prefix>/<run_id>/<relative>``.

    The one layout both the writer (``eval.sfn_handlers``) and :func:`artefact_url` use. ``prefix``
    has no leading or trailing slash; an empty ``relative`` gives the run's root,
    ``<prefix>/<run_id>/``.
    """
    return f"{prefix}/{run_id}/{relative}"


class ArtefactsNotConfiguredError(CrmError):
    """Raised when :data:`BUCKET_ENV_VAR` or :data:`PREFIX_ENV_VAR` is unset on this deployment."""


class Presigner(Protocol):
    """The S3 client surface :func:`artefact_url` uses."""

    def generate_presigned_url(self, **kwargs: Any) -> str: ...


@dataclass(frozen=True, slots=True)
class ArtefactsConfig:
    """Where run artefacts live: ``bucket`` and ``prefix`` (no leading or trailing slash)."""

    bucket: str
    prefix: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ArtefactsConfig:
        """Read :data:`BUCKET_ENV_VAR` and :data:`PREFIX_ENV_VAR` from ``env`` (default ``os.environ``).

        Raises:
            ArtefactsNotConfiguredError: Either variable is unset or empty.
        """
        source = os.environ if env is None else env
        bucket = source.get(BUCKET_ENV_VAR, "").strip()
        prefix = source.get(PREFIX_ENV_VAR, "").strip().strip("/")
        if not bucket or not prefix:
            raise ArtefactsNotConfiguredError(
                "Run artefacts are not available on this deployment.",
                detail=f"{BUCKET_ENV_VAR} and {PREFIX_ENV_VAR} must both be set",
            )
        return cls(bucket=bucket, prefix=prefix)

    def object_key(self, run_id: str, key: str) -> str:
        """The S3 key of artefact ``key`` of ``run_id``, by :func:`artefact_object_key`."""
        return artefact_object_key(self.prefix, run_id, key)


def require_artefact_reader(identity: Identity) -> None:
    """Raise :class:`ScopeDenied` unless ``identity`` may read run artefacts.

    An admin, or a user principal holding full tenant visibility, may.
    """
    if identity.is_admin or (identity.principal_type == "user" and identity.full_visibility):
        return
    raise ScopeDenied(
        "Reading evaluation artefacts requires full tenant visibility.",
        missing_scope=EVAL_ARTEFACTS_SCOPE,
        detail=f"operation=eval_trigger:artefact_url {identity.scope_description()}",
    )


def validate_artefact_key(key: object) -> str:
    """Return ``key`` when it is a plain relative artefact path.

    Raises:
        ValidationFailed: ``key`` is not a string, is empty or too long, or carries anything other
            than ``/``-separated segments of letters, digits, ``.``, ``_``, ``+`` and ``-``, or a
            ``.``/``..`` segment.
    """
    if not isinstance(key, str) or not key or len(key) > _MAX_KEY_LENGTH:
        raise ValidationFailed("An artefact key is required.", source="key")
    if _KEY_PATTERN.fullmatch(key) is None or any(seg in (".", "..") for seg in key.split("/")):
        raise ValidationFailed(
            "The artefact key is not a run artefact path.",
            source="key",
            detail="keys are relative paths with no traversal, encoding or absolute segments",
        )
    return key


def recorded_artefact_keys(links: Mapping[str, Any] | None) -> frozenset[str]:
    """The artefact paths a run recorded in ``artifact_links``; empty when it recorded none."""
    if not links:
        return frozenset()
    keys = {links[field] for field in _LINK_FIELDS if isinstance(links.get(field), str)}
    raw = links.get("raw")
    if isinstance(raw, list):
        keys.update(path for path in raw if isinstance(path, str))
    return frozenset(keys)


def _tenant_belongs_to_run(tenant: str, run_id: str) -> bool:
    """Whether ``tenant`` is ``run_id``'s base tenant or one of its unit tenants."""
    from eval.tenancy import run_id_for_tenant

    try:
        return bool(run_id) and run_id_for_tenant(tenant) == run_id
    except ValueError:
        return False


def download_disposition(run_id: str, key: str) -> str:
    """The attachment ``Content-Disposition`` of artefact ``key``: ``matrix-<run_id>.<ext>``.

    ``ext`` is the suffix of ``key``'s last segment; a segment with no suffix names
    ``matrix-<run_id>``.
    """
    name = key.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[1] if "." in name else ""
    filename = f"matrix-{run_id}.{suffix}" if suffix else f"matrix-{run_id}"
    return f'attachment; filename="{filename}"'


def artefact_url(
    store: Store,
    s3: Presigner,
    identity: Identity,
    *,
    run_id: str,
    key: object,
    config: ArtefactsConfig,
    download: bool = False,
) -> dict[str, Any]:
    """Authorize and presign one run artefact GET.

    Order: the caller's scope, the key's shape, the run's record and tenant, the key's membership in
    the run's recorded artefacts, then the presign. The signed S3 key is built from the record's own
    run id and recorded path, not from the request strings. With ``download`` the URL carries
    ``ResponseContentDisposition`` from :func:`download_disposition`.

    Returns:
        ``{"run_id", "key", "url", "expires_in"}``.

    Raises:
        ScopeDenied: The caller may not read artefacts, or the run's record names a tenant that is
            neither the run's base tenant nor one of its unit tenants.
        ValidationFailed: ``key`` is not a plain relative path.
        NotFound: No such run, or ``key`` is not one of the run's recorded artefacts.
    """
    from eval.runs import require_run

    require_artefact_reader(identity)
    valid_key = validate_artefact_key(key)
    record = require_run(store, run_id)
    if not _tenant_belongs_to_run(record.tenant, run_id):
        raise ScopeDenied(
            "That evaluation run belongs to another tenant.",
            missing_scope=EVAL_ARTEFACTS_SCOPE,
            detail=f"run {run_id!r} records tenant {record.tenant!r}",
        )
    recorded = next(
        (path for path in recorded_artefact_keys(record.artifact_links) if path == valid_key), None
    )
    if recorded is None:
        raise NotFound(f"Run {run_id!r} has no artefact {valid_key!r}.")
    params = {"Bucket": config.bucket, "Key": config.object_key(record.run_id, recorded)}
    if download:
        params["ResponseContentDisposition"] = download_disposition(record.run_id, recorded)
    url = s3.generate_presigned_url(
        ClientMethod="get_object", Params=params, ExpiresIn=ARTEFACT_URL_EXPIRES_SECONDS
    )
    return {
        "run_id": record.run_id,
        "key": recorded,
        "url": url,
        "expires_in": ARTEFACT_URL_EXPIRES_SECONDS,
    }


__all__ = [
    "ARTEFACT_URL_EXPIRES_SECONDS",
    "BUCKET_ENV_VAR",
    "EVAL_ARTEFACTS_SCOPE",
    "PREFIX_ENV_VAR",
    "ArtefactsConfig",
    "ArtefactsNotConfiguredError",
    "artefact_object_key",
    "Presigner",
    "artefact_url",
    "download_disposition",
    "recorded_artefact_keys",
    "require_artefact_reader",
    "validate_artefact_key",
]
