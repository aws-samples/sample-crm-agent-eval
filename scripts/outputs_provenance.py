# SPDX-License-Identifier: MIT
"""Provenance of `results/tf-outputs.json`: who wrote it, from which commit, and is it still current.

`scripts/deploy.sh` stamps the snapshot with the commit it was captured from (:func:`stamp_document`);
:func:`assess_freshness` then asks whether any authored `infra/**/*.tf` differs from that commit. A
snapshot with no recorded commit falls back to an mtime comparison, and :class:`Freshness` reports
which comparison answered. An unanswerable comparison counts as stale.

Also holds :data:`OUTPUT_SHAPE_CONTRACT`, the pinned value shape of selected Terraform outputs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

INFRA_DIR: Final[Path] = REPO_ROOT / "infra"

#: The provenance key injected into the snapshot; underscore-prefixed so no Terraform output collides.
PROVENANCE_KEY: Final[str] = "_outputs_provenance"

#: Exit status meaning "this run verified nothing", shared with `scripts/smoke/public_exposure.py`
#: and `scripts/run_gates.py`.
EXIT_VERIFIED_NOTHING: Final[int] = 3

_GIT_TIMEOUT_SECONDS: Final[int] = 15

#: Which comparison answered. `commit` is authoritative; `mtime` is the declared fallback for
#: snapshots written before `PROVENANCE_KEY` existed.
FreshnessForm = Literal["commit", "mtime"]


@dataclass(frozen=True)
class Freshness:
    """The freshness verdict for an outputs snapshot, and which comparison produced it."""

    #: Why the snapshot must not be trusted, or `None` when it is usable.
    refusal: str | None
    form: FreshnessForm
    #: What the verdict was derived from, in one line, for a check's `detail`.
    basis: str

    @property
    def is_fresh(self) -> bool:
        return self.refusal is None

    @property
    def used_weaker_form(self) -> bool:
        """Whether this verdict rests on the mtime fallback rather than the commit comparison."""
        return self.form == "mtime"


def _git(*args: str) -> tuple[int, str]:
    """Run a read-only `git` command in the repository, returning `(returncode, stdout)`.

    Never raises: a missing binary, a non-work-tree, or a timeout all return non-zero, and every
    caller treats that as stale.
    """
    try:
        completed = proc_run.run(
            ["git", "--no-pager", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return completed.returncode, completed.stdout


def current_commit() -> str | None:
    """The repository's current `HEAD`, or `None` if it cannot be resolved."""
    code, out = _git("rev-parse", "HEAD")
    commit = out.strip()
    return commit if code == 0 and commit else None


def working_tree_is_dirty() -> bool:
    """Whether any tracked `infra/` file differs from `HEAD`, or an untracked `.tf` exists."""
    code, out = _git("status", "--porcelain", "--", "infra")
    if code != 0:
        return True
    return bool(out.strip())


#: The provenance `how` per recorder, selected by `--stamp ... --recorded-by`.
HOW_BY_RECORDER: Final[Mapping[str, str]] = {
    "deploy": "terraform output -json, recorded by scripts/deploy.sh after a successful apply",
    "refresh": "terraform output -json, recorded by scripts/refresh_outputs.sh with no apply",
}


def stamp_document(
    document: dict[str, Any],
    *,
    commit: str | None,
    dirty: bool,
    captured_at: str | None = None,
    recorded_by: str = "deploy",
) -> dict[str, Any]:
    """Return `document` with a provenance block added under :data:`PROVENANCE_KEY`.

    Pure and copy-returning. A `commit` of `None` is recorded rather than omitted. `recorded_by`
    selects the `how` string from :data:`HOW_BY_RECORDER`.
    """
    stamped = dict(document)
    stamped[PROVENANCE_KEY] = {
        "captured_from_commit": commit,
        "captured_from_dirty_tree": dirty,
        "captured_at": captured_at or datetime.now(UTC).isoformat(),
        "how": HOW_BY_RECORDER[recorded_by],
    }
    return stamped


def read_provenance(document: object) -> dict[str, Any] | None:
    """The provenance block in `document`, or `None` when it carries none."""
    if not isinstance(document, dict):
        return None
    provenance = document.get(PROVENANCE_KEY)
    return provenance if isinstance(provenance, dict) else None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _authored_tf_paths(git_output: str) -> set[str]:
    """Authored `*.tf` paths from a newline-separated git path list, excluding the provider cache.

    Splits on newlines only, since a path may contain a space.
    """
    return {
        line
        for line in (raw.strip() for raw in git_output.splitlines())
        if line.endswith(".tf") and ".terraform/" not in line
    }


def infra_changes_since(commit: str) -> tuple[bool, list[str], str | None]:
    """Whether any authored `infra/**/*.tf` differs from `commit`, as `(resolvable, changed, error)`.

    Compares against the working tree, so uncommitted edits and untracked `.tf` files count.
    `resolvable` is False when the question cannot be answered (no git, not a work tree, or `commit`
    absent from this clone); callers treat that as stale. The git pathspec is the plain `infra`
    directory and `*.tf` selection happens in Python, since git's default glob matching lets `*`
    match `/` and would drop root-level files.
    """
    resolved, _ = _git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if resolved != 0:
        return (
            False,
            [],
            (
                f"the provenance commit {commit!r} does not resolve in this clone (a shallow clone, "
                "a rewritten history, or a snapshot captured in a different repository)"
            ),
        )
    diff_code, diff_out = _git("diff", "--name-only", commit, "--", "infra")
    if diff_code != 0:
        return False, [], f"`git diff` against {commit!r} failed"
    untracked_code, untracked_out = _git(
        "ls-files", "--others", "--exclude-standard", "--", "infra"
    )
    if untracked_code != 0:
        return False, [], "`git ls-files --others` failed"
    changed = sorted(_authored_tf_paths(diff_out) | _authored_tf_paths(untracked_out))
    return True, changed, None


def _mtime_freshness(path: Path) -> Freshness:
    """The mtime fallback, used only for snapshots that cannot name a commit.

    It cannot distinguish a fresh clone from a current snapshot.
    """
    try:
        snapshot_mtime = path.stat().st_mtime
    except OSError as exc:
        return Freshness(
            refusal=f"the outputs snapshot {path} could not be read: {exc}",
            form="mtime",
            basis="stat() failed",
        )
    newer = sorted(
        source.relative_to(REPO_ROOT).as_posix()
        for source in INFRA_DIR.rglob("*.tf")
        if ".terraform" not in source.parts and source.stat().st_mtime > snapshot_mtime
    )
    weakness = (
        "compared by MTIME because the snapshot carries no "
        f"{PROVENANCE_KEY}.captured_from_commit; this comparison cannot see staleness on a fresh "
        "clone, so a pass here is weaker than a commit-form pass"
    )
    if newer:
        return Freshness(
            refusal=(
                f"the outputs snapshot {path} is STALE, not absent, which is why nothing else "
                f"complained: {len(newer)} infra source file(s) are newer than it "
                f"(e.g. {newer[:3]}). Every finding derived from it would describe an earlier stack"
            ),
            form="mtime",
            basis=weakness,
        )
    return Freshness(refusal=None, form="mtime", basis=weakness)


def assess_freshness(path: Path) -> Freshness:
    """Whether the outputs snapshot at `path` still describes the `infra/` on disk.

    Prefers the commit comparison and falls back to mtime only for a snapshot with no recorded
    commit, reporting which form answered either way. Fail-safe: an unanswerable comparison is stale.
    """
    if not path.exists():
        return Freshness(
            refusal=(
                f"the outputs snapshot {path} does not exist, so nothing could be discovered from "
                "it. Run scripts/deploy.sh, or set CRM_TF_OUTPUTS to a snapshot"
            ),
            form="commit",
            basis="the file is absent",
        )

    provenance = read_provenance(_load(path))
    commit = provenance.get("captured_from_commit") if provenance else None
    if not isinstance(commit, str) or not commit.strip():
        return _mtime_freshness(path)

    resolvable, changed, error = infra_changes_since(commit.strip())
    dirty_at_capture = bool(provenance.get("captured_from_dirty_tree")) if provenance else False
    basis = f"compared by COMMIT against {commit.strip()[:12]}"
    if dirty_at_capture:
        basis += " (captured from a DIRTY tree, so it describes no named revision)"

    if not resolvable:
        return Freshness(
            refusal=(
                f"the outputs snapshot {path} cannot be shown current: {error}. Treated as STALE "
                "because a comparison that cannot be made must not answer 'fine'"
            ),
            form="commit",
            basis=basis,
        )
    if changed:
        return Freshness(
            refusal=(
                f"the outputs snapshot {path} is STALE, not absent, which is why nothing else "
                f"complained: {len(changed)} authored infra/**/*.tf file(s) differ from the commit "
                f"it was captured at ({commit.strip()[:12]}), e.g. {changed[:3]}. Every finding "
                "derived from it would describe an earlier stack"
            ),
            form="commit",
            basis=basis,
        )
    if dirty_at_capture:
        return Freshness(
            refusal=(
                f"the outputs snapshot {path} was captured from a DIRTY working tree, so the commit "
                "it names does not describe the Terraform that was applied. Treated as STALE: the "
                "recorded provenance cannot be checked against anything"
            ),
            form="commit",
            basis=basis,
        )
    return Freshness(refusal=None, form="commit", basis=basis)


def _stamp_in_place(path: Path, *, recorded_by: str = "deploy") -> int:
    """Add provenance to the snapshot at `path`. Returns a process exit code."""
    document = _load(path)
    if document is None:
        print(
            f"{path} is not a readable non-empty JSON object; refusing to stamp it",
            file=sys.stderr,
        )
        return 1
    commit = current_commit()
    stamped = stamp_document(
        document, commit=commit, dirty=working_tree_is_dirty(), recorded_by=recorded_by
    )
    path.write_text(json.dumps(stamped, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if commit is None:
        print(
            f"warning: recorded {path.name} with captured_from_commit=null (HEAD did not resolve); "
            "its readers will fall back to the weaker mtime comparison",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI: stamp a snapshot, or report its freshness.

    `--check` is the freshness predicate other readers consult, including
    `tests/e2e/browser/playwright.config.ts`. It exits :data:`EXIT_VERIFIED_NOTHING` on a stale or
    unanswerable snapshot.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stamp", metavar="PATH", help="add provenance to this outputs snapshot")
    group.add_argument("--check", metavar="PATH", help="report whether this snapshot is current")
    group.add_argument(
        "--check-shapes",
        metavar="PATH",
        help="report whether each contracted output still has the KIND of value it is meant to",
    )
    parser.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    parser.add_argument(
        "--recorded-by",
        choices=sorted(HOW_BY_RECORDER),
        default="deploy",
        help="with --stamp: the script recording the snapshot, which selects the `how` string",
    )
    args = parser.parse_args(argv)

    if args.stamp:
        return _stamp_in_place(Path(args.stamp), recorded_by=args.recorded_by)

    if args.check_shapes:
        document = _load(Path(args.check_shapes))
        if document is None:
            print(f"{args.check_shapes} is not a readable JSON object", file=sys.stderr)
            return EXIT_VERIFIED_NOTHING
        flattened = {
            name: (entry["value"] if isinstance(entry, dict) and "value" in entry else entry)
            for name, entry in document.items()
        }
        shapes = check_output_shapes(flattened)
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": shapes.ok,
                        "checked": shapes.checked,
                        "absent": list(shapes.absent),
                        "unconstrained": shapes.unconstrained,
                        "violations": list(shapes.violations),
                    }
                )
            )
        else:
            for violation in shapes.violations:
                print(f"[FAIL] {violation}", file=sys.stderr)
            if shapes.absent:
                print(f"[note] contracted but absent: {list(shapes.absent)}", file=sys.stderr)
            print(
                f"{shapes.checked} contracted output(s) checked, "
                f"{shapes.unconstrained} unconstrained"
            )
        if shapes.checked == 0:
            return EXIT_VERIFIED_NOTHING
        return 0 if shapes.ok else 1

    verdict = assess_freshness(Path(args.check))
    if args.json:
        print(
            json.dumps(
                {
                    "fresh": verdict.is_fresh,
                    "form": verdict.form,
                    "basis": verdict.basis,
                    "refusal": verdict.refusal,
                    "used_weaker_form": verdict.used_weaker_form,
                }
            )
        )
    elif verdict.refusal:
        print(verdict.refusal, file=sys.stderr)
    else:
        print(f"fresh ({verdict.basis})")
    return EXIT_VERIFIED_NOTHING if verdict.refusal else 0


# ---------------------------------------------------------------------------
# Output value-shape contract
# ---------------------------------------------------------------------------

#: Shape names this module classifies an output value into.
OutputShape = Literal[
    "url", "hostname", "arn", "log_group_path", "s3_uri", "identifier", "mapping", "empty", "other"
]


def classify_shape(value: object) -> OutputShape:
    """Classify an output value's shape.

    Coarse: it distinguishes kinds of value, such as a URL, a bare hostname, an id or an ARN.
    """
    if isinstance(value, Mapping | list):
        return "mapping"
    text = str(value)
    if not text:
        return "empty"
    if text.startswith("arn:"):
        return "arn"
    if text.startswith("s3://"):
        return "s3_uri"
    if "://" in text:
        return "url"
    if text.startswith("/"):
        return "log_group_path"
    if "." in text and " " not in text and "/" not in text:
        return "hostname"
    return "identifier"


#: The pinned value shape of selected outputs. Not exhaustive: an unlisted output is unconstrained,
#: and coverage is reported.
OUTPUT_SHAPE_CONTRACT: Final[Mapping[str, OutputShape]] = {
    "agent_relay_function_name": "identifier",
    "agent_relay_function_url": "url",
    "agent_relay_log_group_name": "log_group_path",
    "agent_relay_role_arn": "arn",
    "agentcore_runtime_arn": "arn",
    "api_endpoint": "hostname",
    "api_id": "identifier",
    "api_stage_invoke_url": "url",
    "cloudfront_distribution_id": "identifier",
    "cloudfront_domain_name": "hostname",
    "cognito_user_pool_id": "identifier",
    "dynamodb_table_names": "mapping",
    "eval_trigger_function_name": "identifier",
}


@dataclass(frozen=True)
class ShapeReport:
    """The outcome of checking an outputs document against :data:`OUTPUT_SHAPE_CONTRACT`."""

    violations: tuple[str, ...]
    checked: int
    absent: tuple[str, ...]
    unconstrained: int

    @property
    def ok(self) -> bool:
        return not self.violations


def check_output_shapes(outputs: Mapping[str, object]) -> ShapeReport:
    """Check every contracted output's value shape. **Never echoes a value.**

    Values include account ids inside ARNs and a client secret, so messages name only the observed
    shape and length. A contracted output absent from the document is reported separately from a
    shape mismatch.
    """
    violations: list[str] = []
    absent: list[str] = []
    checked = 0
    for name, expected in sorted(OUTPUT_SHAPE_CONTRACT.items()):
        if name not in outputs:
            absent.append(name)
            continue
        checked += 1
        actual = classify_shape(outputs[name])
        if actual != expected:
            violations.append(
                f"{name}: expected a {expected!r} value, found {actual!r} "
                f"(length {len(str(outputs[name]))}; value withheld — these outputs carry account "
                f"ids and a client secret). An output's contract is its name AND its meaning; the "
                f"name is unchanged, so no name-based check can see this"
            )
    unconstrained = len([name for name in outputs if name not in OUTPUT_SHAPE_CONTRACT])
    return ShapeReport(
        violations=tuple(violations),
        checked=checked,
        absent=tuple(absent),
        unconstrained=unconstrained,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
