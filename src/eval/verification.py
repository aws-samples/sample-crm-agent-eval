# SPDX-License-Identifier: MIT
"""The verification-inventory contract: the value types every Layer 4-6 artifact is written in.

`eval.harness.run_sweep` publishes its demo-tenant inventory in this shape, and
`scripts/smoke/harness.py` re-exports every name here, so smoke and sweep inventories share one
reader. This module imports neither `boto3` nor `argparse` and constructs no AWS client; it ships
in the Lambda archive, where `scripts/` is absent.

Status vocabulary:

* `pass`   - the control was verified live and holds.
* `fail`   - the control was verified live and does NOT hold.
* `skip`   - the check was not applicable or could not be attempted for a reason that says nothing
  about the control (a dry run, a local-stack limitation, a resource kind the architecture lacks).
* `defer`  - the control is NOT verified and NOT in force, for a named unmet prerequisite recorded
  in `CheckResult.defer_reason`.

`ResourceClassResult.ok` and `exit_code` are driven by `fail` only, so a `defer` never by itself
makes a run non-zero and never masks a `fail` on any other check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

#: The repository root. Inside a Lambda archive this is the task root, which has no git work tree.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

DEFAULT_PROFILE: Final[str] = "default"
DEFAULT_REGION: Final[str] = "us-east-1"

Status = Literal["pass", "fail", "skip", "defer"]

#: The status a deferred control carries.
DEFER: Final[Status] = "defer"

#: Value recorded for a provenance field that could not be determined (no git, not a work
#: tree, git unavailable).
UNKNOWN: Final[str] = "unknown"

_GIT_TIMEOUT_SECONDS: Final[float] = 10.0


def _run_git(*arguments: str) -> str | None:
    """Return stripped stdout of `git <arguments>`, or None if git could not answer.

    Never raises: a git binary absent from `PATH`, a non-work-tree, or a non-zero exit all yield
    None. git is invoked by the absolute path `shutil.which` resolves.
    """
    git = shutil.which("git")
    if git is None:
        return None
    launch = subprocess.run
    try:
        completed = launch(  # nosec B603 - absolute git path, fixed argv, no shell
            [git, *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_commit() -> str:
    """Return the full HEAD commit sha, or `UNKNOWN` if it cannot be determined."""
    return _run_git("rev-parse", "HEAD") or UNKNOWN


def git_tree_dirty() -> bool | None:
    """Return True/False for a dirty/clean work tree, or None if it cannot be determined."""
    status = _run_git("status", "--porcelain")
    if status is None:
        return None
    return status != ""


def utc_now_iso() -> str:
    """Return the current instant as a UTC ISO-8601 string with a `Z` suffix."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


#: The check name a coverage assertion is recorded under, imported by producers and readers.
COVERAGE_CHECK: Final[str] = "coverage_is_non_vacuous"


@dataclass(frozen=True)
class ExpectedCheckCount:
    """The fewest checks a resource class must emit, and how that number was derived.

    A minimum, not an exact count, because emission is outcome-dependent in several modules.
    `basis` states what the number was derived from and must be non-empty.
    """

    minimum: int
    basis: str

    def __post_init__(self) -> None:
        """Refuse an expectation that could not fail, or one that does not say where it came from."""
        if self.minimum < 1:
            raise ValueError(
                f"an ExpectedCheckCount minimum must be at least 1, got {self.minimum}: a floor of "
                "zero is indistinguishable from having no floor, which is the hole this closes"
            )
        if not self.basis.strip():
            raise ValueError(
                "an ExpectedCheckCount must state the basis it was derived from; a bare number goes "
                "stale the first time the inputs change and nobody can tell that it has"
            )


@dataclass(frozen=True)
class UnderivableCheckCount:
    """A deriver existed, ran, and could not produce a bound. Resolves to a `fail`, never to a floor.

    Distinct from the absent-deriver case (a string reason), which compares against 1.
    """

    reason: str

    def __post_init__(self) -> None:
        """Refuse a failure that does not say what failed; the reason is the whole payload."""
        if not self.reason.strip():
            raise ValueError(
                "an UnderivableCheckCount must state why the derivation failed: this becomes the only "
                "record of it, since the exception it came from is already caught"
            )


def coverage_check(
    resource_class: str,
    collected: Sequence[CheckResult],
    expected: ExpectedCheckCount | UnderivableCheckCount | str,
) -> CheckResult:
    """Assert `collected` is as large as `resource_class` promised, as an ordinary check.

    Returned as a `CheckResult` rather than applied to `ResourceClassResult.ok`, so it counts in
    `failed`, drives `exit_code`, and appears in the inventory like any other check.

    `expected` is one of:

    * a derived :class:`ExpectedCheckCount` — the count is compared against its minimum;
    * an :class:`UnderivableCheckCount` — no bound could be computed, so this is a `fail` regardless
      of how many checks were collected;
    * a string naming why no deriver exists — compared against 1.

    An empty `collected` is always a `fail`.
    """
    if isinstance(expected, UnderivableCheckCount):
        return CheckResult(
            resource=resource_class,
            check=COVERAGE_CHECK,
            status="fail",
            detail=(
                f"{resource_class} could not derive how many checks it must emit "
                f"({expected.reason}), so the {len(collected)} check(s) it did emit cannot be "
                "certified as complete. An unknown bound is a failure, not a floor: comparing "
                "against the weakest fallback here would make guarding a derivation weaker than "
                "not guarding it."
            ),
        )
    minimum = expected.minimum if isinstance(expected, ExpectedCheckCount) else 1
    basis = (
        expected.basis
        if isinstance(expected, ExpectedCheckCount)
        else (
            f"no expectation could be derived ({expected}), so this falls back to the weakest "
            "possible floor of 1 check. A class that should emit many can still collapse to a "
            "handful without failing here"
        )
    )
    if not collected:
        return CheckResult(
            resource=resource_class,
            check=COVERAGE_CHECK,
            status="fail",
            detail=(
                f"{resource_class} emitted NO checks at all, so it verified nothing. That is a "
                f"failure, never a pass and never a silent success. Expected at least {minimum} "
                f"({basis})."
            ),
        )
    if len(collected) < minimum:
        return CheckResult(
            resource=resource_class,
            check=COVERAGE_CHECK,
            status="fail",
            detail=(
                f"{resource_class} emitted {len(collected)} check(s) but must emit at least "
                f"{minimum} ({basis}). Checks that were never produced are not visible as failures, "
                "so a shortfall is reported here or not at all."
            ),
        )
    return CheckResult(
        resource=resource_class,
        check=COVERAGE_CHECK,
        status="pass",
        detail=f"{len(collected)} check(s) emitted, at least {minimum} required ({basis})",
    )


@dataclass(frozen=True)
class Provenance:
    """Provenance of one inventory artifact: when, from which code, and against which region.

    Carries no AWS account id and no profile name. `git_tree_dirty` is `True` (uncommitted changes
    were present), `False` (clean), or `None` when unknown.
    """

    generated_at: str
    git_commit: str
    git_tree_dirty: bool | None
    region: str
    resource_class: str

    @classmethod
    def capture(cls, *, region: str, resource_class: str) -> Provenance:
        """Capture provenance now, from the repository this module lives in."""
        return cls(
            generated_at=utc_now_iso(),
            git_commit=git_commit(),
            git_tree_dirty=git_tree_dirty(),
            region=region,
            resource_class=resource_class,
        )

    def to_json_dict(self) -> dict[str, Any]:
        """Return the pinned JSON shape for this provenance block."""
        return {
            "generated_at": self.generated_at,
            "git_commit": self.git_commit,
            "git_tree_dirty": UNKNOWN if self.git_tree_dirty is None else self.git_tree_dirty,
            "region": self.region,
            "resource_class": self.resource_class,
        }


@dataclass(frozen=True)
class CheckResult:
    """One (resource, check) outcome.

    `defer_reason` is a machine-readable snake_case token naming the unmet prerequisite, e.g.
    `"missing_custom_acm_certificate"`. It is required when `status == "defer"` and must be empty
    for every other status.
    """

    resource: str
    check: str
    status: Status
    detail: str = ""
    defer_reason: str = ""

    def __post_init__(self) -> None:
        """Enforce the `defer`/`defer_reason` correspondence in both directions."""
        if self.status == DEFER and not self.defer_reason:
            raise ValueError(
                f"a {DEFER!r} status requires a machine-readable defer_reason naming the unmet "
                f"prerequisite (check={self.check!r}, resource={self.resource!r})"
            )
        if self.status != DEFER and self.defer_reason:
            raise ValueError(
                f"defer_reason is only valid with status {DEFER!r}, got {self.status!r} "
                f"(check={self.check!r}, resource={self.resource!r})"
            )


@dataclass(frozen=True)
class ResourceClassResult:
    """The checks performed for one resource class within one verification layer."""

    layer: int
    resource_class: str
    checks: tuple[CheckResult, ...]
    provenance: Provenance | None = None

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        """Return the subset of checks with status `"fail"`."""
        return tuple(c for c in self.checks if c.status == "fail")

    @property
    def deferred(self) -> tuple[CheckResult, ...]:
        """Return the subset of checks with status `"defer"` (disclosed, unmet-prerequisite gaps)."""
        return tuple(c for c in self.checks if c.status == DEFER)

    @property
    def ok(self) -> bool:
        """True iff no check has status `"fail"`; a `"defer"` does not make a result non-ok."""
        return len(self.failed) == 0

    def to_json_dict(self) -> dict[str, Any]:
        """Return the pinned `--json` shape for this result.

        Each check emits `resource`/`check`/`status`/`detail`; a deferred check additionally emits
        `defer_reason`. A `provenance` key is emitted only when this result carries one.
        """
        return {
            "layer": self.layer,
            "resource_class": self.resource_class,
            **(
                {"provenance": self.provenance.to_json_dict()}
                if self.provenance is not None
                else {}
            ),
            "checks": [
                {
                    "resource": c.resource,
                    "check": c.check,
                    "status": c.status,
                    "detail": c.detail,
                    **({"defer_reason": c.defer_reason} if c.status == DEFER else {}),
                }
                for c in self.checks
            ],
        }

    @property
    def coverage(self) -> CheckResult | None:
        """The coverage check for this result (see :func:`coverage_check`), or `None` if none was asserted."""
        for check in self.checks:
            if check.check == COVERAGE_CHECK:
                return check
        return None

    def exit_code(self) -> int:
        """Return 0 if `ok` else 1."""
        return 0 if self.ok else 1


def resolve_profile(flag: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Resolve the AWS profile: flag -> `AWS_PROFILE` -> `DEFAULT_PROFILE`."""
    if flag:
        return flag
    environment = env if env is not None else os.environ
    return environment.get("AWS_PROFILE") or DEFAULT_PROFILE


def resolve_region(flag: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Resolve the AWS region: flag -> `AWS_REGION` -> `DEFAULT_REGION`."""
    if flag:
        return flag
    environment = env if env is not None else os.environ
    return environment.get("AWS_REGION") or DEFAULT_REGION
