# SPDX-License-Identifier: MIT
"""Tests for `check_lambda_zip_imports`'s `tracked_provenance` phase.

``source_parity`` compares a zip's first-party content with the working tree; ``tracked_provenance``
compares it with the git index, so a green result means a fresh clone could rebuild the artifact. A
mismatch is classified into one of the `PROVENANCE_CLASSES` by also reading the working tree, and
every class's message must be true of every input that reaches it.

Classifier tests drive a fixture tree and a fixture index via `repo_root=`/`tracked=`; loader tests
build real git repositories under `tmp_path`, and a few read this repository's own index.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import unicodedata
import warnings
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import scripts.check_lambda_zip_imports as gate
from scripts import proc_run

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _FakeCompleted[Out: (str, bytes)]:
    """Stand-in for a finished process: the attributes the code under test reads; nothing runs."""

    args: Sequence[str]
    returncode: int
    stdout: Out
    stderr: Out


_HANDLER_SOURCE: Final[str] = "def handler(event, context):\n    return {}\n"
_HELPER_SOURCE: Final[str] = "VALUE = 1\n"


def _index(tmp_path: Path, members: dict[str, str]) -> gate.ZipModuleIndex:
    """A `ZipModuleIndex` over a zip whose first-party package is `crm`, at the archive root."""
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    # Held open for the life of the index, which reads members lazily.
    archive_handle = zipfile.ZipFile(path)
    return gate.ZipModuleIndex(archive_handle, import_roots=("",))


def _tracked(paths: dict[str, str]) -> gate.TrackedContent:
    """A tracked-content fixture mapping repo-relative paths to the blob ids of the given text."""
    return gate.TrackedContent(
        blob_ids={path: gate._git_blob_id(text.encode()) for path, text in paths.items()}
    )


def _result() -> gate.ZipResult:
    return gate.ZipResult(zip_name="fixture.zip", handler="crm.handlers.h.handler")


def _write_working_tree(tmp_path: Path, files: dict[str, str]) -> None:
    """Materialise repo-relative `files` on disk as the working tree the phase reads.

    A path omitted here probes as absent; a test wanting the unreadable branch must put something
    unreadable at the path.
    """
    for repo_relative, text in files.items():
        path = tmp_path / repo_relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _run(
    tmp_path: Path,
    *,
    members: dict[str, str],
    tracked: gate.TrackedContent,
    source_root: Path | None = None,
    working_tree: dict[str, str] | None = None,
) -> gate.ZipResult:
    if working_tree:
        _write_working_tree(tmp_path, working_tree)
    result = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, members),
        result,
        source_root=source_root if source_root is not None else tmp_path / "src",
        tracked=tracked,
        repo_root=tmp_path,
    )
    return result


# ---------------------------------------------------------------------------
# The contract with git itself
# ---------------------------------------------------------------------------


def test_the_blob_id_agrees_with_git_itself() -> None:
    """`_git_blob_id` computes the same id as `git hash-object --stdin` for arbitrary bytes."""
    payload = b'resource "null" "a" {}\n\x00binary\xff'
    completed = proc_run.run(
        ["git", "hash-object", "--stdin"],
        cwd=str(_REPO_ROOT),
        input=payload,
        capture_output=True,
        check=True,
    )
    assert gate._git_blob_id(payload) == completed.stdout.decode().strip()


def test_the_blob_id_distinguishes_content(tmp_path: Path) -> None:
    """`_git_blob_id` gives distinct content distinct ids."""
    assert gate._git_blob_id(b"a") != gate._git_blob_id(b"b")


def test_load_tracked_content_reads_the_real_index() -> None:
    """`load_tracked_content` returns the real git index: the gate's own script is tracked."""
    tracked = gate.load_tracked_content()
    assert tracked.available, tracked.unavailable_reason
    assert tracked.is_tracked("scripts/check_lambda_zip_imports.py")
    assert not tracked.is_tracked("results/definitely-not-tracked-xyz.json")


# ---------------------------------------------------------------------------
# The pass, and the failure classes
# ---------------------------------------------------------------------------


def test_a_zip_whose_first_party_content_is_all_tracked_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/helper.py": _HELPER_SOURCE},
        tracked=_tracked(
            {"src/crm/handlers/h.py": _HANDLER_SOURCE, "src/crm/helper.py": _HELPER_SOURCE}
        ),
    )
    assert result.violations == []
    assert result.tracked_provenance_status == "pass"
    assert result.tracked_provenance_classes == []
    assert result.tracked_files_matched == 2


def test_untracked_code_baked_in_is_reported_as_that_class(tmp_path: Path) -> None:
    """A zip member present on disk and in no commit is reported as untracked code baked in."""
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/origin.py": _HELPER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        working_tree={
            "src/crm/handlers/h.py": _HANDLER_SOURCE,
            "src/crm/origin.py": _HELPER_SOURCE,
        },
    )
    assert result.tracked_provenance_status == "fail"
    assert result.tracked_provenance_classes == ["untracked_code_baked_in"]
    assert len(result.violations) == 1
    assert "UNTRACKED CODE BAKED IN" in result.violations[0]
    assert "src/crm/origin.py" in result.violations[0]
    assert "unreproducible" in result.violations[0]


def test_a_file_untracked_and_absent_from_disk_is_its_own_class(tmp_path: Path) -> None:
    """A zip member that is untracked and absent from disk has its own class."""
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/origin.py": _HELPER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE},
    )
    assert result.tracked_provenance_classes == ["untracked_and_absent_from_working_tree"]
    assert "ABSENT FROM THE WORKING TREE" in result.violations[0]


def test_an_untracked_file_edited_after_packaging_is_not_reported_as_current_with_the_disk(
    tmp_path: Path,
) -> None:
    """An untracked member that differs from the disk is `untracked_and_stale`, and its message
    states that the zip is not current with the disk."""
    on_disk = _HELPER_SOURCE + "# edited after packaging\n"
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/origin.py": _HELPER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE, "src/crm/origin.py": on_disk},
    )
    assert result.tracked_provenance_classes == ["untracked_and_stale"]
    violation = result.violations[0]
    # The affirmative phrase; a bare "current with this disk" also matches the class's negation.
    assert "bytes are current with this disk" not in violation, (
        f"the zip is behind the working tree, so this must not be asserted: {violation}"
    )
    assert "NOT current with this disk" in violation, (
        f"the message must state the staleness rather than omit it: {violation}"
    )
    assert "NOT a stale artifact" not in violation, (
        f"the zip IS behind the working tree, so this must not be asserted: {violation}"
    )
    assert "src/crm/origin.py" in violation


def test_a_tracked_file_deleted_from_disk_is_not_reported_as_rebuildable_from_it(
    tmp_path: Path,
) -> None:
    """A tracked member with different content and no working-tree copy is
    `stale_and_absent_from_working_tree`, not the plain stale class."""
    committed = _HELPER_SOURCE + "# committed after packaging\n"
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/gone.py": _HELPER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE, "src/crm/gone.py": committed}),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE},
    )
    assert result.tracked_provenance_classes == ["stale_and_absent_from_working_tree"]
    violation = result.violations[0]
    assert "ABSENT FROM THE WORKING TREE" in violation
    assert "behind both trees" not in violation, (
        f"there is no working-tree copy for it to be behind: {violation}"
    )


def test_packaging_over_an_uncommitted_edit_is_reported_as_that_class(tmp_path: Path) -> None:
    """A member matching the disk but not the index is `packaged_over_uncommitted_edit`."""
    edited = _HANDLER_SOURCE + "# uncommitted edit\n"
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": edited},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        working_tree={"src/crm/handlers/h.py": edited},
    )
    assert result.tracked_provenance_classes == ["packaged_over_uncommitted_edit"]
    assert "MATCHING THE WORKING TREE BUT NO COMMIT" in result.violations[0]
    assert "Commit the edit(s)" in result.violations[0]


def test_a_stale_artifact_is_reported_as_stale_and_not_as_an_uncommitted_edit(
    tmp_path: Path,
) -> None:
    """A member differing from both the index and an index-matching disk is `stale_artifact`, and
    its message does not prescribe committing."""
    committed = _HANDLER_SOURCE + "# committed after packaging\n"
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": committed}),
        working_tree={"src/crm/handlers/h.py": committed},
    )
    assert result.tracked_provenance_classes == ["stale_artifact"]
    assert "STALE ARTIFACT" in result.violations[0]
    assert "Re-run scripts/package_lambdas.sh" in result.violations[0]
    assert "Commit the edit(s)" not in result.violations[0]


def test_the_stale_and_untracked_classes_are_not_confusable_in_the_output(tmp_path: Path) -> None:
    """The stale and baked-in messages each name the other class as not applying."""
    stale = gate.PROVENANCE_CLASSES["stale_artifact"].violation(["src/a.py"])
    baked_in = gate.PROVENANCE_CLASSES["untracked_code_baked_in"].violation(["src/b.py"])
    assert "NOT untracked or uncommitted code baked in" in stale
    assert "NOT a stale artifact" in baked_in
    # And both say which tree produced the comparison, read in isolation.
    for message in (stale, baked_in):
        assert "GIT INDEX" in message


def test_no_class_narrates_a_time_order_that_a_blob_comparison_cannot_establish() -> None:
    """No registered class's message states a time order; a blob comparison cannot establish one."""
    directional = (
        "predates",
        "behind",
        "moved past",
        "has since",
        "up to date",
        "was packaged over",
    )
    for name, provenance_class in gate.PROVENANCE_CLASSES.items():
        message = provenance_class.violation(["src/a.py"])
        for phrase in directional:
            assert phrase not in message, (
                f"{name} narrates a time order blob inequality cannot establish ({phrase!r}): {message}"
            )


def test_each_class_carries_a_distinct_headline_and_fix() -> None:
    """Every provenance class has a distinct headline and a distinct fix."""
    headlines = [c.headline for c in gate.PROVENANCE_CLASSES.values()]
    fixes = [c.fix for c in gate.PROVENANCE_CLASSES.values()]
    assert len(set(headlines)) == len(headlines)
    assert len(set(fixes)) == len(fixes)


def _index_states() -> dict[str, str | None]:
    """The index observations (`None` absent, `"t"` differing), keyed by `gate._index_state`'s label."""
    return {gate._index_state(blob): blob for blob in (None, "t")}


def test_the_index_state_producer_emits_the_declared_spellings() -> None:
    """`_index_state` emits the literal labels, and its range equals `gate.INDEX_STATES`."""
    assert gate._index_state(None) == "absent"
    assert gate._index_state("t") == "differs"
    assert {gate._index_state(None), gate._index_state("t")} == set(gate.INDEX_STATES), (
        f"the producer emits {sorted({gate._index_state(None), gate._index_state('t')})}, "
        f"the implementation declares {sorted(gate.INDEX_STATES)}"
    )


def test_the_enumeration_covers_every_declared_index_state() -> None:
    """Every declared index state has an observation in `_index_states()`."""
    assert set(_index_states()) == set(gate.INDEX_STATES), (
        f"observations cover {sorted(_index_states())}, implementation declares "
        f"{sorted(gate.INDEX_STATES)}"
    )


def _probes() -> dict[str, gate.WorkingTreeProbe]:
    """The working-tree observations, keyed by a label naming each one.

    One per `gate.WORKING_TREE_STATES` value, except `readable`, which fans out over the zip's blob,
    the index's blob, and a blob belonging to neither.
    """
    return {
        "absent": gate.WorkingTreeProbe(state="absent"),
        "unreadable": gate.WorkingTreeProbe(state="unreadable"),
        "readable=zip": gate.WorkingTreeProbe(state="readable", blob_id="z"),
        "readable=index": gate.WorkingTreeProbe(state="readable", blob_id="t"),
        "readable=neither": gate.WorkingTreeProbe(state="readable", blob_id="w"),
    }


def test_every_declared_working_tree_state_is_one_the_probe_actually_emits(tmp_path: Path) -> None:
    """`_probe_working_tree` over three real paths emits exactly `gate.WORKING_TREE_STATES`.

    An undeclared state is refused by `WorkingTreeProbe.__post_init__`.
    """
    base = tmp_path / "src" / "crm"
    base.mkdir(parents=True)
    readable = base / "ok.py"
    readable.write_text(_HELPER_SOURCE)
    (base / "as_dir.py").mkdir()

    emitted = {
        gate._probe_working_tree(readable).state,
        gate._probe_working_tree(base / "as_dir.py").state,
        gate._probe_working_tree(base / "never_existed.py").state,
    }
    assert emitted == {"readable", "unreadable", "absent"}
    assert emitted == set(gate.WORKING_TREE_STATES), (
        f"the probe emits {sorted(emitted)}, the implementation declares "
        f"{sorted(gate.WORKING_TREE_STATES)}"
    )


def test_the_enumeration_covers_every_declared_working_tree_state() -> None:
    """Every declared working-tree state has a probe in `_probes()`."""
    covered = {p.state for p in _probes().values()}
    assert covered == set(gate.WORKING_TREE_STATES), (
        f"probe set covers {sorted(covered)}, implementation declares "
        f"{sorted(gate.WORKING_TREE_STATES)}"
    )


def test_the_classifier_maps_every_input_to_the_class_that_is_true_of_it() -> None:
    """Each (index state, working-tree probe) cell maps to its hand-written expected class.

    The cells are the cross product of `_index_states()` and `_probes()`; every registered class is
    reached.
    """
    expected = {
        ("absent", "absent"): "untracked_and_absent_from_working_tree",
        ("absent", "unreadable"): "untracked_and_unreadable_in_working_tree",
        ("absent", "readable=zip"): "untracked_code_baked_in",
        # With no index entry these two cells are the same observation and share a class.
        ("absent", "readable=index"): "untracked_and_stale",
        ("absent", "readable=neither"): "untracked_and_stale",
        ("differs", "absent"): "stale_and_absent_from_working_tree",
        ("differs", "unreadable"): "tracked_and_unreadable_in_working_tree",
        ("differs", "readable=zip"): "packaged_over_uncommitted_edit",
        ("differs", "readable=index"): "stale_artifact",
        ("differs", "readable=neither"): "stale_artifact_over_uncommitted_edit",
    }
    probes = _probes()
    index_states = _index_states()
    observed = {
        (index_label, probe_label): gate._classify_provenance(
            zip_blob="z", tracked_blob=index_blob, working_tree=probe
        )
        for index_label, index_blob in index_states.items()
        for probe_label, probe in probes.items()
    }
    assert observed == expected

    # The enumeration itself is the cross product, asserted structurally rather than counted.
    assert set(expected) == {(i, p) for i in index_states for p in probes}

    # No registered class is unreached, and no input reaches an unregistered name.
    assert set(observed.values()) == set(gate.PROVENANCE_CLASSES)


def test_the_only_shared_class_is_the_one_the_index_cannot_disambiguate() -> None:
    """`untracked_and_stale` is the only class reached by more than one cell."""
    probes = _probes()
    by_class: dict[str, list[tuple[str, str]]] = {}
    for index_label, index_blob in _index_states().items():
        for probe_label, probe in probes.items():
            name = gate._classify_provenance(
                zip_blob="z", tracked_blob=index_blob, working_tree=probe
            )
            by_class.setdefault(name, []).append((index_label, probe_label))

    shared = {name: cells for name, cells in by_class.items() if len(cells) > 1}
    assert shared == {
        "untracked_and_stale": [("absent", "readable=index"), ("absent", "readable=neither")]
    }, f"an unjustified collapse appeared: {shared}"


_ROOT_MODE_SHAPE_REASON: Final[str] = (
    "the effective uid is 0, and root bypasses discretionary read permission, so a 0o000 file probes "
    "as readable — the mode-denied shape cannot be observed here. It is exercised only as a non-root "
    "user; CI containers run as root. The other two present-but-unreadable shapes are unaffected by "
    "the euid and stay asserted."
)


def _mode_denied_shape_is_measurable(euid: int) -> tuple[bool, str]:
    """Whether a mode-``0o000`` file can be observed as unreadable by this effective uid.

    Args:
        euid: The effective uid to judge; callers pass ``os.geteuid()``.

    Returns:
        ``(True, "")`` when the shape is measurable, otherwise ``(False, <reason naming the cause>)``.
    """
    if euid == 0:
        return False, _ROOT_MODE_SHAPE_REASON
    return True, ""


def test_the_mode_denied_shape_is_declared_unmeasurable_only_for_root() -> None:
    """The mode-denied shape is declared unmeasurable for euid 0 only, with root named as the cause."""
    assert _mode_denied_shape_is_measurable(0) == (False, _ROOT_MODE_SHAPE_REASON)
    assert "root" in _ROOT_MODE_SHAPE_REASON
    for euid in (1, 501, 1000):
        assert _mode_denied_shape_is_measurable(euid) == (True, "")


def test_a_present_but_unreadable_path_is_not_reported_as_absent(tmp_path: Path) -> None:
    """A directory, a broken symlink, and a mode-denied file all probe as unreadable, not absent.

    Under ``euid == 0`` the mode-denied shape is skipped with a warning; see
    :func:`_mode_denied_shape_is_measurable`.
    """
    base = tmp_path / "src" / "crm"
    base.mkdir(parents=True)

    readable = base / "ok.py"
    readable.write_text(_HELPER_SOURCE)
    # Positive control: a regular file probes as readable.
    probe = gate._probe_working_tree(readable)
    assert probe.state == "readable"
    assert probe.blob_id == gate._git_blob_id(_HELPER_SOURCE.encode())

    (base / "as_dir.py").mkdir()
    broken = base / "broken.py"
    broken.symlink_to(base / "nonexistent.py")

    unreadable_shapes = ["as_dir.py", "broken.py"]
    mode_shape_measurable, reason = _mode_denied_shape_is_measurable(os.geteuid())
    no_perm = base / "no_perm.py"
    if mode_shape_measurable:
        no_perm.write_text(_HELPER_SOURCE)
        no_perm.chmod(0o000)
        unreadable_shapes.append("no_perm.py")
    else:
        warnings.warn(f"the mode-denied shape was not measured: {reason}", stacklevel=1)

    try:
        for present in unreadable_shapes:
            observed = gate._probe_working_tree(base / present)
            assert observed.state == "unreadable", f"{present} read as {observed.state}"
            assert observed.blob_id is None
    finally:
        if mode_shape_measurable:
            no_perm.chmod(0o600)

    absent = gate._probe_working_tree(base / "never_existed.py")
    assert absent.state == "absent"
    assert absent.blob_id is None


def test_the_unreadable_explanation_covers_every_shape_measured_to_reach_it(tmp_path: Path) -> None:
    """A symlink loop, a socket and a character device probe as unreadable, and the unreadable
    classes' messages cover them by category.

    The socket is bound under `tempfile.mkdtemp()` because a `tmp_path`-length path exceeds the
    `AF_UNIX` path limit.
    """
    base = tmp_path / "src" / "crm"
    base.mkdir(parents=True)
    loop = base / "loop.py"
    loop.symlink_to(loop)
    assert gate._probe_working_tree(loop).state == "unreadable"

    chardev = Path(os.devnull)
    assert chardev.exists(), "the character-device case needs a device node to read"
    assert gate._probe_working_tree(chardev).state == "unreadable"

    socket_dir = Path(tempfile.mkdtemp())
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(socket_dir / "sock.py"))
        assert gate._probe_working_tree(socket_dir / "sock.py").state == "unreadable"
    finally:
        sock.close()
        shutil.rmtree(socket_dir, ignore_errors=True)

    for name in (
        "untracked_and_unreadable_in_working_tree",
        "tracked_and_unreadable_in_working_tree",
    ):
        message = gate.PROVENANCE_CLASSES[name].violation(["src/a.py"])
        assert "not a readable regular file" in message, message
        assert "socket or device node" in message, message

    # A file occupying a parent directory's name probes as absent, so the absent class's fix
    # mentions it.
    occupied = tmp_path / "src" / "crm" / "pkg"
    occupied.write_text("not a directory\n")
    assert gate._probe_working_tree(occupied / "mod.py").state == "absent"
    absent_fix = gate.PROVENANCE_CLASSES["untracked_and_absent_from_working_tree"].violation(
        ["a.py"]
    )
    assert "occupied by a file" in absent_fix, absent_fix


def test_an_unreadable_path_is_told_to_repair_rather_than_to_restore(tmp_path: Path) -> None:
    """An untracked, unreadable path is told to repair rather than restore, end to end."""
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/origin.py": _HELPER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE},
    )
    # Baseline: a genuinely absent path still reports the absent class.
    assert result.tracked_provenance_classes == ["untracked_and_absent_from_working_tree"]

    (tmp_path / "src" / "crm" / "origin.py").mkdir(parents=True)
    repaired = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/origin.py": _HELPER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
    )
    assert repaired.tracked_provenance_classes == ["untracked_and_unreadable_in_working_tree"]
    violation = repaired.violations[0]
    assert "exists nowhere now" not in violation, (
        f"the path is present on disk, so this must not be asserted: {violation}"
    )
    assert "nothing to restore" in violation
    assert "Repair the path" in violation


def test_a_tracked_but_unreadable_path_is_told_to_repair_not_restore(tmp_path: Path) -> None:
    """A tracked, unreadable path is told to repair rather than restore, end to end."""
    committed = _HELPER_SOURCE + "# committed after packaging\n"
    (tmp_path / "src" / "crm" / "blocked.py").mkdir(parents=True)
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/blocked.py": _HELPER_SOURCE},
        tracked=_tracked(
            {"src/crm/handlers/h.py": _HANDLER_SOURCE, "src/crm/blocked.py": committed}
        ),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE},
    )
    assert result.tracked_provenance_classes == ["tracked_and_unreadable_in_working_tree"]
    violation = result.violations[0]
    assert "restoring it is not the fix" in violation
    assert "Repair the path" in violation
    # It must not borrow the untracked half's instruction: there is nothing to commit here.
    assert "commit it and re-run" not in violation, (
        f"the content is recoverable from the index, so no commit is required: {violation}"
    )


def test_a_stale_zip_over_an_uncommitted_edit_is_not_told_to_merely_repackage(
    tmp_path: Path,
) -> None:
    """A member differing from the index, with a disk differing from both, is
    `stale_artifact_over_uncommitted_edit` and is told to commit first."""
    committed = _HELPER_SOURCE + "# committed after packaging\n"
    on_disk = _HELPER_SOURCE + "# and then edited, uncommitted\n"
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/drifted.py": _HELPER_SOURCE},
        tracked=_tracked(
            {"src/crm/handlers/h.py": _HANDLER_SOURCE, "src/crm/drifted.py": committed}
        ),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE, "src/crm/drifted.py": on_disk},
    )
    assert result.tracked_provenance_classes == ["stale_artifact_over_uncommitted_edit"]
    violation = result.violations[0]
    assert "nothing here to commit" not in violation, (
        f"there IS an uncommitted edit, so this must not be asserted: {violation}"
    )
    assert "Commit the working-tree edit(s) FIRST" in violation

    # The clean sub-case keeps the plain stale-artifact diagnosis.
    clean = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/clean.py": _HELPER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE, "src/crm/clean.py": committed}),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE, "src/crm/clean.py": committed},
    )
    assert clean.tracked_provenance_classes == ["stale_artifact"]
    assert "nothing here to commit" in clean.violations[0]


def test_following_the_stale_artifact_fix_does_not_land_in_another_violation() -> None:
    """Repackaging alone over an uncommitted edit lands in `packaged_over_uncommitted_edit`.

    Repackaging sets the zip's blob to the disk's, which resolves a genuine `stale_artifact` but not
    the uncommitted-edit sub-case.
    """
    probes = _probes()

    # Genuine stale artifact: disk agrees with the index, so repackaging makes zip == index == disk.
    assert (
        gate._classify_provenance(
            zip_blob="z", tracked_blob="t", working_tree=probes["readable=index"]
        )
        == "stale_artifact"
    )
    # After a repackage the member does not mismatch at all, so the classifier is not reached;
    # asserted at the phase level rather than restated here.

    # The other sub-case, repackage-only: zip becomes the disk's blob 'w', index still 't'.
    assert (
        gate._classify_provenance(
            zip_blob="z", tracked_blob="t", working_tree=probes["readable=neither"]
        )
        == "stale_artifact_over_uncommitted_edit"
    )
    after_repackage_only = gate._classify_provenance(
        zip_blob="w", tracked_blob="t", working_tree=gate.WorkingTreeProbe("readable", "w")
    )
    assert after_repackage_only == "packaged_over_uncommitted_edit", (
        "repackaging alone must be shown to land in the next violation, which is why this cell's fix "
        "commits first"
    )


def _conflicted_repo(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    """A real git repo left mid-merge with an unresolved conflict at `src/crm/helper.py`.

    Returns the repo root and the three stage blobs' bytes, keyed by stage number.
    `commit.gpgsign` and `core.hooksPath` are neutralised per invocation.
    """
    root = tmp_path / "conflicted"
    (root / "src" / "crm").mkdir(parents=True)

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return proc_run.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
            cwd=root,
            capture_output=True,
            check=True,
        )

    git("init", "-q", ".")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    target = root / "src" / "crm" / "helper.py"
    target.write_text("VALUE = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", "theirs")
    target.write_text("VALUE = 3\n")
    git("commit", "-qam", "theirs")
    git("checkout", "-q", "-")
    target.write_text("VALUE = 2\n")
    git("commit", "-qam", "ours")
    merged = proc_run.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", "merge", "theirs"],
        cwd=root,
        capture_output=True,
    )
    assert merged.returncode != 0, "the fixture did not actually produce a conflict"

    listing = proc_run.run(
        ["git", "ls-files", "-s", "src/crm/helper.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    stages: dict[str, bytes] = {}
    for line in listing:
        if not line.strip():
            continue
        fields = line.split()
        stages[fields[2]] = proc_run.run(
            ["git", "cat-file", "blob", fields[1]], cwd=root, capture_output=True, check=True
        ).stdout
    assert sorted(stages) == ["1", "2", "3"], (
        f"a content conflict must emit base/ours/theirs, got stages {sorted(stages)}"
    )
    return root, stages


def test_a_conflicted_index_is_unavailable_rather_than_silently_one_side(tmp_path: Path) -> None:
    """A conflicted index is unavailable and contributes no blob id for the conflicted path.

    `ls-files -s` emits a conflicted path once per stage.
    """
    root, _stages = _conflicted_repo(tmp_path)
    tracked = gate.load_tracked_content(repo_root=root)

    assert not tracked.available, (
        "a conflicted index must not report as available: there is no single tracked content"
    )
    assert "UNMERGED" in tracked.unavailable_reason
    assert "src/crm/helper.py" in tracked.unavailable_reason
    # The conflicted path contributes NO blob id, so nothing downstream can compare against one side.
    assert "src/crm/helper.py" not in tracked.blob_ids


def test_the_phase_does_not_pass_over_a_conflicted_index(tmp_path: Path) -> None:
    """Over a conflicted index the phase reports `unavailable` and matches nothing, even for a zip
    carrying the stage-3 bytes."""
    root, stages = _conflicted_repo(tmp_path)
    theirs = stages["3"]
    tracked = gate.load_tracked_content(repo_root=root)

    result = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, {"crm/helper.py": theirs.decode()}),
        result,
        source_root=root / "src",
        tracked=tracked,
        repo_root=root,
    )

    assert result.tracked_provenance_status == "unavailable"
    assert result.tracked_provenance_status != "pass"
    assert result.tracked_files_matched == 0, (
        "a member must not be counted as matching tracked content that could not be determined"
    )
    assert any("UNMERGED" in note for note in result.notes), result.notes


def test_the_unmerged_refusal_describes_the_state_rather_than_prescribing_a_merge_abort(
    tmp_path: Path,
) -> None:
    """The unmerged-index reason prescribes resolving and `git add` rather than a merge abort.

    A merge, cherry-pick, revert, rebase, `am -3` and `stash apply` can each leave an unmerged index.
    """
    root, _stages = _conflicted_repo(tmp_path)
    reason = gate.load_tracked_content(repo_root=root).unavailable_reason

    assert "mid-merge" not in reason, reason
    assert "Resolve the merge" not in reason, reason
    assert "`git merge --abort`" not in reason, reason
    assert "`git add`" in reason, reason
    assert "while any path is unmerged" in reason, reason
    assert "only where an operation is in progress" in reason, reason


def test_the_composed_unavailable_note_reads_as_one_sentence_boundary(tmp_path: Path) -> None:
    """The composed unavailable note has a single period where the reason meets the appended text."""
    root, _stages = _conflicted_repo(tmp_path)
    result = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, {"crm/handlers/h.py": _HANDLER_SOURCE}),
        result,
        source_root=root / "src",
        tracked=gate.load_tracked_content(repo_root=root),
        repo_root=root,
    )
    note = next(n for n in result.notes if "UNAVAILABLE" in n)
    assert ".." not in note, note
    assert "never that it passed. This run did NOT check" in note, note


def test_the_index_states_the_classifier_never_sees_are_intercepted_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmerged and equal-to-the-zip index states never reach the classifier.

    An unmerged index contributes no blob id, and a member equal to its tracked content is matched
    without a classifier call; a mismatching member is the positive control.
    """
    root, _stages = _conflicted_repo(tmp_path)
    tracked = gate.load_tracked_content(repo_root=root)
    assert not tracked.available, tracked.unavailable_reason
    assert tracked.blob_ids == {}, (
        f"an unmerged index must contribute no blob id for the classifier to read: {tracked.blob_ids}"
    )

    reached: list[tuple[str, str | None]] = []
    real_classify = gate._classify_provenance

    def _observed(
        *, zip_blob: str, tracked_blob: str | None, working_tree: gate.WorkingTreeProbe
    ) -> str:
        reached.append((zip_blob, tracked_blob))
        return real_classify(
            zip_blob=zip_blob, tracked_blob=tracked_blob, working_tree=working_tree
        )

    monkeypatch.setattr(gate, "_classify_provenance", _observed)

    matched = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE},
    )
    assert matched.tracked_files_matched == 1
    assert matched.tracked_provenance_status == "pass"
    assert reached == [], (
        f"a member equal to its tracked content must not reach the classifier: {reached}"
    )

    mismatched = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE + "# differs from tracked\n"},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        working_tree={"src/crm/handlers/h.py": _HANDLER_SOURCE},
    )
    assert mismatched.tracked_provenance_status == "fail"
    assert len(reached) == 1, (
        f"the positive control must reach the classifier exactly once, or the assertion above is "
        f"about a monkeypatch that did not take: {reached}"
    )


def test_a_working_tree_probe_cannot_encode_an_impossible_state() -> None:
    """`WorkingTreeProbe` carries a `blob_id` exactly when it is readable, and refuses otherwise."""
    assert gate.WorkingTreeProbe(state="absent").blob_id is None
    assert gate.WorkingTreeProbe(state="unreadable").blob_id is None
    assert gate.WorkingTreeProbe(state="readable", blob_id="z").blob_id == "z"

    with pytest.raises(ValueError, match="exactly when it is readable"):
        gate.WorkingTreeProbe(state="readable")
    with pytest.raises(ValueError, match="exactly when it is readable"):
        gate.WorkingTreeProbe(state="unreadable", blob_id="z")


def _tracked_from_ls_files(monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> gate.TrackedContent:
    """`load_tracked_content` reading exactly `stdout` as `git ls-files -s -z` output.

    `subprocess.run` is stubbed per git subcommand; an unstubbed subcommand raises naming itself.
    """

    def _run_stub(args: Sequence[str], **_kwargs: object) -> _FakeCompleted[bytes]:
        command = list(args)
        if "rev-parse" in command:
            # `--show-toplevel --show-prefix` at a top level: the root, then an empty prefix line.
            return _FakeCompleted(
                args=command, returncode=0, stdout=f"{_REPO_ROOT}\n\n".encode(), stderr=b""
            )
        if "ls-files" in command:
            return _FakeCompleted(args=command, returncode=0, stdout=stdout, stderr=b"")
        raise AssertionError(f"this harness stubs no answer for {command}")

    monkeypatch.setattr(subprocess, "run", _run_stub)
    return gate.load_tracked_content(repo_root=_REPO_ROOT)


def test_an_unparsable_index_entry_pushes_toward_unavailable_not_toward_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An index entry missing its stage field or path makes the index unavailable, not tracked."""
    blob = "a" * 40
    control = _tracked_from_ls_files(monkeypatch, f"100644 {blob} 0\tsrc/crm/h.py\0".encode())
    assert control.available, control.unavailable_reason
    assert control.blob_ids == {"src/crm/h.py": blob}

    degraded = {
        "no stage field": f"100644 {blob}\tsrc/crm/h.py\0",
        "blob only": f"{blob}\tsrc/crm/h.py\0",
        "no tab, so no path": f"100644 {blob} 0\0",
    }
    for label, stdout in degraded.items():
        observed = _tracked_from_ls_files(monkeypatch, stdout.encode())
        assert not observed.available, f"{label}: {observed.blob_ids}"
        assert "could not be parsed" in observed.unavailable_reason, f"{label}: {observed}"
        assert observed.blob_ids == {}, (
            f"{label}: an entry whose merge stage is unknown must not be recorded as tracked"
        )


def test_the_two_repo_roots_must_name_the_same_tree(tmp_path: Path) -> None:
    """The phase refuses a `tracked` loaded from a different root than its `repo_root`.

    An agreeing root and an unrecorded root are both accepted.
    """
    members = {"crm/handlers/h.py": _HANDLER_SOURCE}
    at_tmp = gate.TrackedContent(
        blob_ids={"src/crm/handlers/h.py": gate._git_blob_id(_HANDLER_SOURCE.encode())},
        repo_root=tmp_path,
    )

    agreeing = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, members),
        agreeing,
        source_root=tmp_path / "src",
        tracked=at_tmp,
        repo_root=tmp_path,
    )
    assert agreeing.tracked_provenance_status == "pass"

    # A fixture that records no root is accepted with any `repo_root`.
    unbound = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, members),
        unbound,
        source_root=tmp_path / "src",
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        repo_root=tmp_path,
    )
    assert unbound.tracked_provenance_status == "pass"

    with pytest.raises(ValueError, match="every member would look untracked"):
        gate.check_tracked_provenance(
            _index(tmp_path, members),
            _result(),
            source_root=tmp_path / "src",
            tracked=at_tmp,
            repo_root=tmp_path / "elsewhere",
        )


def _clean_repo(tmp_path: Path) -> Path:
    """A real git repo with one committed file and a fully merged index.

    `commit.gpgsign` and `core.hooksPath` are neutralised per invocation.
    """
    root = tmp_path / "clean"
    (root / "src" / "crm").mkdir(parents=True)

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return proc_run.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
            cwd=root,
            capture_output=True,
            check=True,
        )

    git("init", "-q", ".")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (root / "src" / "crm" / "helper.py").write_text(_HELPER_SOURCE)
    git("add", "-A")
    git("commit", "-qm", "base")
    return root


def test_the_loader_records_the_tree_it_read(tmp_path: Path) -> None:
    """The loader records the root it read on both the available and the unavailable return."""
    clean = _clean_repo(tmp_path)
    available = gate.load_tracked_content(repo_root=clean)
    assert available.available, available.unavailable_reason
    assert available.is_tracked("src/crm/helper.py")
    assert available.repo_root == clean

    conflicted, _stages = _conflicted_repo(tmp_path)
    unavailable = gate.load_tracked_content(repo_root=conflicted)
    assert not unavailable.available
    assert unavailable.repo_root == conflicted

    assert gate.load_tracked_content().repo_root == _REPO_ROOT


# ---------------------------------------------------------------------------
# The root has to be its own work tree's top level, or the index read is another repository's
# ---------------------------------------------------------------------------


def _outer_repo_with_inner_subtree(tmp_path: Path, *, conflict_at: str | None) -> tuple[Path, Path]:
    """An OUTER git repository tracking `sub/`, plus the inner directory `sub/`, which has no `.git`.

    Returns `(outer, inner)`. `conflict_at` is an outer-relative path to leave unmerged, or ``None``
    for a wholly clean parent. `commit.gpgsign` and `core.hooksPath` are neutralised per invocation.
    """
    outer = tmp_path / "outer"
    inner = outer / "sub"
    (inner / "src" / "crm").mkdir(parents=True)

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return proc_run.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
            cwd=outer,
            capture_output=True,
            check=True,
        )

    git("init", "-q", ".")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (outer / "outside.py").write_text(_HELPER_SOURCE)
    (inner / "src" / "crm" / "inside.py").write_text(_HELPER_SOURCE)
    git("add", "-A")
    git("commit", "-qm", "base")
    assert not (inner / ".git").exists(), "the inner tree must carry no repository of its own"
    if conflict_at is None:
        return outer, inner

    target = outer / conflict_at
    git("checkout", "-q", "-b", "theirs")
    target.write_text("VALUE = 3\n")
    git("commit", "-qam", "theirs")
    git("checkout", "-q", "-")
    target.write_text("VALUE = 2\n")
    git("commit", "-qam", "ours")
    merged = proc_run.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", "merge", "theirs"],
        cwd=outer,
        capture_output=True,
    )
    assert merged.returncode != 0, "the fixture did not actually produce a conflict"
    unmerged = proc_run.run(
        ["git", "ls-files", "--unmerged"], cwd=outer, capture_output=True, text=True, check=True
    ).stdout
    assert conflict_at in unmerged, f"expected {conflict_at} unmerged, got {unmerged!r}"
    return outer, inner


def test_a_tree_nested_in_a_clean_parent_checkout_is_unavailable_rather_than_certified(
    tmp_path: Path,
) -> None:
    """A root nested inside a clean parent checkout is unavailable, naming the enclosing tree.

    `git ls-files` run from a non-top-level directory answers with the enclosing repository's index.
    """
    outer, inner = _outer_repo_with_inner_subtree(tmp_path, conflict_at=None)

    # Precondition: git answers from here, with the enclosing repository's index.
    answered = proc_run.run(
        ["git", "ls-files"], cwd=inner, capture_output=True, text=True, check=True
    )
    assert answered.stdout.split() == ["src/crm/inside.py"]

    tracked = gate.load_tracked_content(repo_root=inner)
    assert not tracked.available, f"certified {tracked.blob_ids} read from {outer}"
    assert str(outer.resolve()) in tracked.unavailable_reason
    assert "different tree" in tracked.unavailable_reason
    assert tracked.blob_ids == {}
    assert tracked.repo_root == inner


def test_a_conflict_inside_the_nested_prefix_names_the_tree_git_answered_from(
    tmp_path: Path,
) -> None:
    """A conflict inside the nested prefix is refused with a reason naming the enclosing tree."""
    outer, inner = _outer_repo_with_inner_subtree(tmp_path, conflict_at="sub/src/crm/inside.py")

    tracked = gate.load_tracked_content(repo_root=inner)
    assert not tracked.available
    assert str(outer.resolve()) in tracked.unavailable_reason
    assert "different tree" in tracked.unavailable_reason


def test_a_conflict_outside_the_nested_prefix_is_not_answered_as_a_clean_index(
    tmp_path: Path,
) -> None:
    """A conflict outside the nested prefix, invisible to a prefix-scoped listing, still makes the
    index unavailable."""
    outer, inner = _outer_repo_with_inner_subtree(tmp_path, conflict_at="outside.py")

    scoped = proc_run.run(
        ["git", "ls-files", "--unmerged"], cwd=inner, capture_output=True, text=True, check=True
    )
    assert scoped.stdout == "", (
        f"the conflict is visible from the prefix, so this is Cell A: {scoped.stdout!r}"
    )

    tracked = gate.load_tracked_content(repo_root=inner)
    assert not tracked.available
    assert str(outer.resolve()) in tracked.unavailable_reason
    assert tracked.blob_ids == {}


def test_a_root_in_no_repository_at_all_keeps_the_git_failure_it_already_reported(
    tmp_path: Path,
) -> None:
    """A root in no repository reports git's own failure, not the nested-tree reason."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    probe = proc_run.run(["git", "rev-parse", "--show-toplevel"], cwd=outside, capture_output=True)
    assert probe.returncode == 128, f"the temp dir is inside a work tree: {probe.stdout!r}"

    tracked = gate.load_tracked_content(repo_root=outside)
    assert not tracked.available
    assert "exited 128" in tracked.unavailable_reason
    assert "different tree" not in tracked.unavailable_reason


def test_a_host_with_no_git_binary_keeps_the_reason_it_already_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no `git` on PATH the loader reports `could not run`, not the nested-tree reason."""
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    assert gate._foreign_work_tree_reason(_REPO_ROOT) == ""

    tracked = gate.load_tracked_content()
    assert not tracked.available
    assert "`git ls-files` could not run" in tracked.unavailable_reason
    assert "rev-parse" not in tracked.unavailable_reason


def test_the_work_tree_probe_never_raises_on_an_answer_that_is_not_one_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `rev-parse` answer that is not one path becomes an unavailable reason, not an exception."""

    def _run_stub(args: Sequence[str], **_kwargs: object) -> _FakeCompleted[bytes]:
        return _FakeCompleted(args=list(args), returncode=0, stdout=b"one\0two\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _run_stub)
    tracked = gate.load_tracked_content(repo_root=_REPO_ROOT)
    assert not tracked.available
    assert "different tree" in tracked.unavailable_reason


def test_this_repositorys_own_root_is_the_top_level_git_names_for_it() -> None:
    """This repository's root passes the work-tree probe and its index loads as available."""
    assert gate._foreign_work_tree_reason(_REPO_ROOT) == ""
    tracked = gate.load_tracked_content()
    assert tracked.available, tracked.unavailable_reason
    assert tracked.blob_ids


# ---------------------------------------------------------------------------
# How the caller spelled the root is not whether the root is a top level
#
# The top-level predicate is git's computed prefix, which is empty at a top level however the root
# is spelled: through a symlink, or in another case or Unicode normal form.
# ---------------------------------------------------------------------------

#: `(spelling created on disk, second spelling addressing that same directory)`. NFC and NFD `café`
#: are different byte strings. Only the first spelling is ever created.
_SECOND_SPELLINGS: Final[dict[str, tuple[str, str]]] = {
    "case": ("spelled", "SPELLED"),
    "nfd": (unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café")),
}


def _git_names_as_toplevel(root: Path) -> str:
    """What `git rev-parse --show-toplevel` answers from `root`, read directly rather than via the gate."""
    toplevel: subprocess.CompletedProcess[str] = proc_run.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return toplevel.stdout.strip()


def test_a_root_reached_through_a_symlink_is_its_own_top_level(tmp_path: Path) -> None:
    """A root reached through a symlink, where git answers the physical path, is its own top level.

    The symlink is created explicitly because `tmp_path` is already the physical spelling.
    """
    physical = tmp_path / "physical"
    physical.mkdir()
    root = _clean_repo(physical)
    (tmp_path / "link").symlink_to(physical, target_is_directory=True)
    logical = tmp_path / "link" / root.name

    # Controls: two spellings, one directory, and git's answer differs from the spelling asked.
    assert str(logical) != str(root)
    assert logical.samefile(root), "the two spellings must name one directory"
    assert _git_names_as_toplevel(logical) != str(logical)

    assert gate._foreign_work_tree_reason(logical) == ""
    tracked = gate.load_tracked_content(repo_root=logical)
    assert tracked.available, tracked.unavailable_reason
    assert tracked.is_tracked("src/crm/helper.py")


@pytest.mark.parametrize("spelling", sorted(_SECOND_SPELLINGS))
def test_a_root_spelled_in_another_case_or_normal_form_is_its_own_top_level(
    tmp_path: Path, spelling: str
) -> None:
    """A root spelled in another case or Unicode normal form, where git answers the on-disk
    spelling, is its own top level.

    Skipped where the volume cannot address one directory by two spellings.
    """
    created, second = _SECOND_SPELLINGS[spelling]
    holder = tmp_path / created
    holder.mkdir()
    alias = tmp_path / second
    if not alias.is_dir():
        pytest.skip(
            f"this volume distinguishes {created!r} from {second!r}, so one directory cannot be "
            f"addressed by both spellings here and the {spelling} axis is unreachable; the case- and "
            "normalization-insensitive behaviour this cell needs is not a property of every host"
        )

    root = _clean_repo(holder)
    logical = alias / root.name

    assert str(logical) != str(root)
    assert logical.samefile(root), "the two spellings must name one directory"
    assert _git_names_as_toplevel(logical) != str(logical)

    assert gate._foreign_work_tree_reason(logical) == ""
    tracked = gate.load_tracked_content(repo_root=logical)
    assert tracked.available, tracked.unavailable_reason
    assert tracked.is_tracked("src/crm/helper.py")


# ---------------------------------------------------------------------------
# The same axis with no filesystem participating
#
# These cells hand the predicate a fabricated `rev-parse` answer for a `root` that does not exist on
# disk, so the result is the same on every host.
# ---------------------------------------------------------------------------


def _probe_reason_for(
    monkeypatch: pytest.MonkeyPatch, root: Path, *, named: str, prefix: str
) -> str:
    """`_foreign_work_tree_reason(root)` when git answers exactly `named` then `prefix`.

    Any git subcommand other than `rev-parse` raises naming itself.
    """

    def _run_stub(args: Sequence[str], **_kwargs: object) -> _FakeCompleted[bytes]:
        command = list(args)
        if "rev-parse" in command:
            return _FakeCompleted(
                args=command, returncode=0, stdout=f"{named}\n{prefix}\n".encode(), stderr=b""
            )
        raise AssertionError(f"this harness stubs no answer for {command}")

    monkeypatch.setattr(subprocess, "run", _run_stub)
    return gate._foreign_work_tree_reason(root)


def _absent_pair(tmp_path: Path, spelling: str) -> tuple[Path, str]:
    """`(root, named)` — two spellings of one path, neither of which exists on disk.

    Rooted at `tmp_path.resolve()` so `root.resolve() == root` holds on any host.
    """
    created, second = _SECOND_SPELLINGS[spelling]
    parent = tmp_path.resolve() / "absent"
    root = parent / created
    named = str(parent / second)

    assert not root.exists(), "the axis must not be reachable through any filesystem property"
    assert not Path(named).exists()
    assert root.resolve() == root, (
        f"{root} carries a resolvable component, so the axis is confounded"
    )
    assert named != str(root)
    return root, named


@pytest.mark.parametrize("spelling", sorted(_SECOND_SPELLINGS))
def test_a_differently_spelled_toplevel_at_an_empty_prefix_is_still_a_top_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spelling: str
) -> None:
    """A differently spelled top level with an empty prefix is not reported as foreign."""
    root, named = _absent_pair(tmp_path, spelling)
    assert _probe_reason_for(monkeypatch, root, named=named, prefix="") == ""


def test_an_identically_spelled_toplevel_at_an_empty_prefix_is_the_fabrication_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control: an identically spelled top level with an empty prefix is not reported as foreign."""
    root = tmp_path.resolve() / "absent" / "spelled"
    assert root.resolve() == root
    assert _probe_reason_for(monkeypatch, root, named=str(root), prefix="") == ""


@pytest.mark.parametrize("spelling", sorted(_SECOND_SPELLINGS))
def test_a_differently_spelled_toplevel_at_a_non_empty_prefix_is_still_foreign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spelling: str
) -> None:
    """Control: a non-empty prefix is reported as foreign, with a reason naming git's top level."""
    root, named = _absent_pair(tmp_path, spelling)
    reason = _probe_reason_for(monkeypatch, root, named=named, prefix="sub/")
    assert named in reason, reason
    assert "a different tree" in reason, reason


# ---------------------------------------------------------------------------
# `repo_root` alone must decide which index answers
#
# An exported `GIT_DIR`, `GIT_WORK_TREE` or `GIT_INDEX_FILE` overrides git's discovery from the
# environment; the loader removes them before every git call:
#
#   GIT_DIR         --show-toplevel unchanged, the index read becomes the foreign repository's
#   GIT_INDEX_FILE  --show-toplevel unchanged, the index read becomes the foreign repository's
#   GIT_WORK_TREE   --show-toplevel becomes foreign, the index read is unchanged
#
# A repository-local `core.worktree` is not covered by the scrub.
# ---------------------------------------------------------------------------


def _repo_tracking(root: Path, tracked_relative: str) -> Path:
    """A real git repo at `root` whose index holds exactly `tracked_relative`.

    Repos track different paths, so the index's key set identifies which one answered.
    """
    (root / Path(tracked_relative).parent).mkdir(parents=True)

    def git(*args: str) -> None:
        proc_run.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
            cwd=root,
            capture_output=True,
            check=True,
        )

    git("init", "-q", ".")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (root / tracked_relative).write_text(_HELPER_SOURCE)
    git("add", "-A")
    git("commit", "-qm", "base")
    return root


#: The two overrides that move which INDEX answers while leaving `--show-toplevel` byte-identical,
#: mapped to how the value is spelled for a foreign repository root.
_INDEX_REDIRECTS: Final[dict[str, str]] = {
    "GIT_DIR": ".git",
    "GIT_INDEX_FILE": ".git/index",
}


@pytest.mark.parametrize("variable", sorted(_INDEX_REDIRECTS))
def test_an_exported_index_redirect_does_not_move_which_index_the_loader_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """An exported `GIT_DIR` or `GIT_INDEX_FILE` does not change which index the loader reads."""
    subject = _repo_tracking(tmp_path / "subject", "src/crm/subject_file.py")
    foreign = _repo_tracking(tmp_path / "foreign", "src/crm/foreign_file.py")

    # Control: with nothing exported, the loader reads the subject's own index.
    before = gate.load_tracked_content(repo_root=subject)
    assert sorted(before.blob_ids) == ["src/crm/subject_file.py"], sorted(before.blob_ids)

    monkeypatch.setenv(variable, str(foreign / _INDEX_REDIRECTS[variable]))

    # Positive control: the override redirects a raw git call on this host.
    raw = proc_run.run(
        ["git", "--no-pager", "ls-files"], cwd=subject, capture_output=True, text=True, check=True
    )
    assert raw.stdout.split() == ["src/crm/foreign_file.py"], (
        f"{variable} did not redirect a raw git call, so this cell measures nothing: {raw.stdout!r}"
    )

    tracked = gate.load_tracked_content(repo_root=subject)
    assert tracked.available, tracked.unavailable_reason
    assert sorted(tracked.blob_ids) == ["src/crm/subject_file.py"], (
        f"{variable} in the environment moved which index answered: the loader offered "
        f"{sorted(tracked.blob_ids)} as {subject}'s tracked content"
    )


def test_an_exported_work_tree_redirect_does_not_make_a_top_level_look_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported `GIT_WORK_TREE` pointing at an enclosing checkout does not make a top level
    look nested."""
    outer = _repo_tracking(tmp_path / "outer", "outer_file.py")
    subject = _repo_tracking(outer / "subject", "src/crm/subject_file.py")

    before = gate.load_tracked_content(repo_root=subject)
    assert sorted(before.blob_ids) == ["src/crm/subject_file.py"], sorted(before.blob_ids)

    monkeypatch.setenv("GIT_WORK_TREE", str(outer))

    # Positive control: raw git answers a non-empty prefix for a directory that is its own top level.
    raw = proc_run.run(
        ["git", "--no-pager", "rev-parse", "--show-toplevel", "--show-prefix"],
        cwd=subject,
        capture_output=True,
        text=True,
        check=True,
    )
    assert raw.stdout.splitlines()[1:] == ["subject/"], (
        f"GIT_WORK_TREE did not redirect the prefix, so this cell measures nothing: {raw.stdout!r}"
    )

    assert gate._foreign_work_tree_reason(subject) == ""
    tracked = gate.load_tracked_content(repo_root=subject)
    assert tracked.available, tracked.unavailable_reason
    assert sorted(tracked.blob_ids) == ["src/crm/subject_file.py"], sorted(tracked.blob_ids)


def test_the_scrub_removes_the_discovery_overrides_and_keeps_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_git_env` removes exactly the three discovery overrides and keeps every other variable."""
    monkeypatch.setenv("GIT_DIR", "/nonexistent/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/nonexistent")
    monkeypatch.setenv("GIT_INDEX_FILE", "/nonexistent/.git/index")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/nonexistent")

    scrubbed = gate._git_env()

    assert set(gate._GIT_DISCOVERY_OVERRIDES) == {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    assert not set(gate._GIT_DISCOVERY_OVERRIDES) & set(scrubbed)
    assert scrubbed["GIT_CEILING_DIRECTORIES"] == "/nonexistent", (
        "a variable measured NOT to move either answer was dropped anyway, so the scrub is wider "
        "than what it was measured to need"
    )
    assert scrubbed.get("PATH") == os.environ.get("PATH")
    assert set(scrubbed) == set(os.environ) - set(gate._GIT_DISCOVERY_OVERRIDES)


def test_a_long_violation_list_is_truncated_with_a_count(tmp_path: Path) -> None:
    """A long violation path list is truncated and keeps the total count."""
    members = {f"crm/mod{i}.py": _HELPER_SOURCE for i in range(9)}
    members["crm/handlers/h.py"] = _HANDLER_SOURCE
    result = _run(
        tmp_path, members=members, tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE})
    )
    assert "9 first-party file(s)" in result.violations[0]
    assert "+4 more" in result.violations[0]


# ---------------------------------------------------------------------------
# The weak/absent answers must never read as a pass
# ---------------------------------------------------------------------------


def test_every_declared_provenance_status_is_reachable_through_the_phase(tmp_path: Path) -> None:
    """Every status in `TRACKED_PROVENANCE_STATUSES` is produced by some path, under its literal
    spelling."""
    reachable: set[str] = set()

    # not_run — the dataclass default, before the phase has run at all.
    reachable.add(_result().tracked_provenance_status)

    # not_applicable — a synthetic zip has no repository counterpart to compare against.
    synthetic = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, {"crm/handlers/h.py": _HANDLER_SOURCE}),
        synthetic,
        source_root=None,
        tracked=gate.TrackedContent(blob_ids={}),
    )
    reachable.add(synthetic.tracked_provenance_status)

    # unavailable — git cannot answer, so the question is not asked.
    reachable.add(
        _run(
            tmp_path,
            members={"crm/handlers/h.py": _HANDLER_SOURCE},
            tracked=gate.TrackedContent(blob_ids={}, unavailable_reason="git is not on PATH"),
        ).tracked_provenance_status
    )

    # pass — every first-party member matches its tracked content.
    reachable.add(
        _run(
            tmp_path,
            members={"crm/handlers/h.py": _HANDLER_SOURCE},
            tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        ).tracked_provenance_status
    )

    # fail — one member does not.
    reachable.add(
        _run(
            tmp_path,
            members={"crm/handlers/h.py": _HANDLER_SOURCE, "crm/extra.py": _HELPER_SOURCE},
            tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
        ).tracked_provenance_status
    )

    assert reachable == {"not_run", "not_applicable", "unavailable", "pass", "fail"}
    assert reachable == set(gate.TRACKED_PROVENANCE_STATUSES), (
        f"the phase produces {sorted(reachable)}, the implementation declares "
        f"{sorted(gate.TRACKED_PROVENANCE_STATUSES)}"
    )


def test_an_unavailable_git_index_is_declared_and_is_not_a_pass(tmp_path: Path) -> None:
    """An unavailable git index yields an `unavailable` status and a note, not a pass."""
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE},
        tracked=gate.TrackedContent(blob_ids={}, unavailable_reason="git is not on PATH"),
    )
    assert result.tracked_provenance_status == "unavailable"
    assert result.violations == []
    assert any("UNAVAILABLE" in note for note in result.notes)
    assert any("working tree only" in note for note in result.notes)


def test_an_unavailable_status_is_never_the_string_pass(tmp_path: Path) -> None:
    """An unavailable index never yields the status `pass`."""
    result = _run(
        tmp_path,
        members={"crm/handlers/h.py": _HANDLER_SOURCE},
        tracked=gate.TrackedContent(blob_ids={}, unavailable_reason="no work tree"),
    )
    assert result.tracked_provenance_status != "pass"


def test_a_source_root_outside_the_provenance_root_compares_nothing_and_is_not_a_pass(
    tmp_path: Path,
) -> None:
    """A `source_root` outside the provenance root skips every member and reports `unavailable`.

    Consistent roots are the control and reach `pass` with one member compared.
    """
    members = {"crm/handlers/h.py": _HANDLER_SOURCE}
    tracked = _tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE})

    # Control: consistent roots compare one member and pass.
    consistent = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, members),
        consistent,
        source_root=tmp_path / "src",
        tracked=tracked,
        repo_root=tmp_path,
    )
    assert consistent.tracked_provenance_status == "pass"
    assert consistent.tracked_files_matched == 1

    outside = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, members),
        outside,
        source_root=tmp_path / "elsewhere" / "src",
        tracked=tracked,
        repo_root=tmp_path / "provenance",
    )
    assert outside.tracked_provenance_status == "unavailable", (
        f"a run that compared 0 members reported {outside.tracked_provenance_status!r}"
    )
    assert outside.tracked_files_matched == 0
    assert outside.violations == []
    assert any("UNAVAILABLE" in note and "skipped" in note for note in outside.notes), outside.notes


def test_a_zip_with_no_first_party_member_compares_nothing_and_is_not_a_pass(
    tmp_path: Path,
) -> None:
    """A zip with no first-party member reports `unavailable` with its own note, distinct from the
    skipped-members note."""
    result = _run(
        tmp_path,
        members={"boto3/client.py": _HELPER_SOURCE},
        tracked=_tracked({"src/crm/handlers/h.py": _HANDLER_SOURCE}),
    )
    assert result.tracked_provenance_status == "unavailable"
    assert result.tracked_files_matched == 0
    assert result.violations == []
    assert any("no first-party member" in note for note in result.notes), result.notes
    assert not any("skipped" in note for note in result.notes), result.notes


def test_a_synthetic_zip_is_not_applicable_rather_than_passing(tmp_path: Path) -> None:
    """`source_root=None` (a zip with no repository counterpart) yields `not_applicable`."""
    result = _result()
    gate.check_tracked_provenance(
        _index(tmp_path, {"crm/handlers/h.py": _HANDLER_SOURCE}),
        result,
        source_root=None,
        tracked=gate.load_tracked_content(),
    )
    assert result.tracked_provenance_status == "not_applicable"
    assert result.violations == []


def test_an_empty_index_listing_is_treated_as_unavailable_not_as_nothing_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty `git ls-files` listing makes the loader report unavailable, not all untracked.

    `TrackedContent` itself makes no judgement, so the guard is in the loader.
    """
    assert gate.TrackedContent(blob_ids={}, unavailable_reason="").available

    empty = _tracked_from_ls_files(monkeypatch, b"")
    assert not empty.available
    assert "listed no tracked file" in empty.unavailable_reason
    assert empty.blob_ids == {}


# ---------------------------------------------------------------------------
# The output must say which tree it compared against
# ---------------------------------------------------------------------------


def test_the_per_zip_pass_line_names_both_trees(capsys: pytest.CaptureFixture[str]) -> None:
    """The per-zip pass line names both the working tree and tracked content."""
    report: dict[str, object] = {
        "status": "pass",
        "zips": [
            {
                "zip": "fixture.zip",
                "handler": "crm.handlers.h.handler",
                "violations": [],
                "notes": [],
                "modules_resolved_in_zip": 1,
                "vendored_records_verified": 0,
                "marker_requirements_resolved": 0,
                "first_party_files_matched": 55,
                "tracked_files_matched": 55,
                "tracked_provenance": "pass",
                "isolated_import": "pass",
            }
        ],
    }
    gate._print_human(report)
    out = capsys.readouterr().out
    assert "byte-identical to the src/ WORKING TREE" in out
    assert "byte-identical to TRACKED content" in out
    assert "tracked_provenance=pass" in out


def test_the_run_level_line_withholds_the_tracked_claim_when_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run-level line withholds the tracked-content claim when provenance is unavailable."""
    report: dict[str, object] = {
        "status": "pass",
        "zips": [
            {
                "zip": "fixture.zip",
                "handler": "crm.handlers.h.handler",
                "violations": [],
                "notes": [],
                "modules_resolved_in_zip": 1,
                "vendored_records_verified": 0,
                "marker_requirements_resolved": 0,
                "first_party_files_matched": 55,
                "tracked_files_matched": 0,
                "tracked_provenance": "unavailable",
                "isolated_import": "pass",
            }
        ],
    }
    gate._print_human(report)
    out = capsys.readouterr().out
    assert "NOT verified against tracked git content" in out
    assert "AND to tracked git content" not in out


def test_the_run_level_line_makes_the_tracked_claim_when_established(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run-level line makes the tracked-content claim when provenance passed."""
    report: dict[str, object] = {
        "status": "pass",
        "zips": [
            {
                "zip": "fixture.zip",
                "handler": "crm.handlers.h.handler",
                "violations": [],
                "notes": [],
                "modules_resolved_in_zip": 1,
                "vendored_records_verified": 0,
                "marker_requirements_resolved": 0,
                "first_party_files_matched": 55,
                "tracked_files_matched": 55,
                "tracked_provenance": "pass",
                "isolated_import": "pass",
            }
        ],
    }
    gate._print_human(report)
    out = capsys.readouterr().out
    assert "AND to tracked git content" in out
    assert "NOT verified against tracked git content" not in out


def test_the_json_report_exposes_the_phase() -> None:
    """`ZipResult` exposes the provenance status and classes as machine-readable fields."""
    assert (
        "tracked_provenance"
        in gate.ZipResult.__dataclass_fields__["tracked_provenance_status"].name
    )
    assert gate.ZipResult(zip_name="z", handler="h").tracked_provenance_status == "not_run"
    assert "tracked_provenance_classes" in gate.ZipResult.__dataclass_fields__
    assert gate.ZipResult(zip_name="z", handler="h").tracked_provenance_classes == []


def test_the_gate_docstring_states_which_tree_each_phase_compares() -> None:
    """The gate's module docstring states which tree each phase compares."""
    doc = gate.__doc__ or ""
    assert "WORKING TREE" in doc
    assert "tracked_provenance" in doc
    assert "unreproducible by construction" in doc
