# SPDX-License-Identifier: MIT
"""`scripts/check_no_commit_ids.py` reports commit ids in tracked text, and only those.

Every planted id is assembled at runtime from fragments shorter than seven characters, so this file
carries no token the gate would report.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import check_no_commit_ids as gate
from scripts import proc_run, run_gates

REPO_ROOT = Path(__file__).resolve().parents[2]

FULL = "9aa0cf" + "a5a217" + "cb599c" + "60d060" + "8a265b" + "14f0f6" + "53ae"
SHORT = "9f0e" + "2c6"
SHORT8 = "74c5" + "18b7"
SHA256 = "5e88" * 16
UUID = "aad3" + "3024" + "-75a9-47c0-8166-" + "6eef92" + "6b83c7"


def _git(root: Path, *args: str) -> None:
    proc_run.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _git_repo(root: Path, files: dict[str, bytes | str], *, commit: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    proc_run.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    if files:
        _git(root, "add", "--", *files)
    if commit:
        _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "c")
    return root


def _check(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = gate.main(["--repo-root", str(root)])
    return rc, capsys.readouterr().out


def _rules(text: str) -> list[tuple[str, str]]:
    return [(token, rule) for _, token, rule in gate.scan_text(text)]


# ---------------------------------------------------------------------------------------------
# Each rule fires
# ---------------------------------------------------------------------------------------------


def test_a_full_length_id_is_reported_without_any_context() -> None:
    assert _rules(f"value {FULL} here\n") == [(FULL, "full-length")]


def test_a_short_id_is_reported_beside_a_commit_keyword() -> None:
    assert _rules(f"see commit {SHORT} for the change\n") == [(SHORT, "commit-context")]
    assert _rules(f"main at {SHORT}.\n") == [(SHORT, "commit-context")]


def test_a_short_id_with_no_commit_context_is_not_reported() -> None:
    assert _rules(f"label {SHORT} value\n") == []


@pytest.mark.parametrize(
    "line",
    [
        f'"git_revision": "{SHORT}",',
        f'"git_commit": "{SHORT8}",',
        f"captured_from_commit={SHORT}",
        f"base: {SHORT}",
    ],
)
def test_the_value_of_a_commit_naming_key_is_reported(line: str) -> None:
    token = SHORT8 if SHORT8 in line else SHORT
    assert _rules(f"{line}\n") == [(token, "commit-context")]


def test_a_keyword_on_the_previous_line_is_context() -> None:
    assert _rules(f"rows left in the tree (measured at\n{SHORT}) fall into two groups\n") == [
        (SHORT, "commit-context")
    ]


def test_findings_carry_their_line_number() -> None:
    assert gate.scan_text(f"one\ntwo\ncommit {SHORT}\n") == [(3, SHORT, "commit-context")]


# ---------------------------------------------------------------------------------------------
# Exclusions hold even in commit context
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f"commit sha256 {SHA256}",
        f"commit scan {UUID}",
        f"commit image sha256:{FULL}",
        f"commit layer sha256-{FULL}",
        "commit account 123456789012",
        "commit run " + "0852" + "146",
        "commit 40-digit " + "1234567890" * 4,
        "commit colour #a1b2" + "c3d4",
        f"commit word x{SHORT}y",
        *(f"commit {fake}" for fake in sorted(gate.FAKE_COMMIT_IDS)),
    ],
    ids=[
        "sha256",
        "uuid",
        "sha256-colon-digest",
        "sha256-dash-digest",
        "12-digit-placeholder",
        "all-digit",
        "all-digit-40",
        "css-colour",
        "inside-a-word",
        *sorted(gate.FAKE_COMMIT_IDS),
    ],
)
def test_non_commit_hex_is_not_reported(line: str) -> None:
    assert _rules(f"{line}\n") == []


def test_every_fake_id_is_used_by_a_tracked_test() -> None:
    tests_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (REPO_ROOT / "tests").rglob("*.py")
    )
    unused = sorted(fake for fake in gate.FAKE_COMMIT_IDS if fake not in tests_text)
    assert unused == []


def test_every_exemption_names_a_tracked_path_with_a_reason() -> None:
    tracked = set(gate.tracked_files(REPO_ROOT))
    for path, reason in gate.EXEMPT.items():
        assert path in tracked, path
        assert reason.strip(), path


# ---------------------------------------------------------------------------------------------
# CLI over a scratch repository
# ---------------------------------------------------------------------------------------------


def test_the_cli_reports_each_tracked_finding_as_path_line_token_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _git_repo(
        tmp_path / "repo",
        {
            "docs/a.md": f"intro\nmain at {SHORT}\n",
            "data.json": f'{{"git_revision": "{FULL}"}}\n',
            "clean.txt": "nothing here\n",
            "image.bin": b"\0" + FULL.encode(),
        },
    )
    (root / "untracked.md").write_text(f"commit {SHORT}\n", encoding="utf-8")
    rc, out = _check(root, capsys)
    assert rc == 1
    lines = out.splitlines()
    assert lines[:-1] == [
        f"data.json:1: {FULL} (full-length)",
        f"docs/a.md:2: {SHORT} (commit-context)",
    ]
    assert lines[-1] == "check_no_commit_ids: FAIL — 2 commit id(s) in 3 text files"


def test_a_clean_tree_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _git_repo(tmp_path / "repo", {"a.md": f"label {SHORT}\n"})
    rc, out = _check(root, capsys)
    assert rc == 0
    assert out.strip() == "check_no_commit_ids: OK — 1 text files, no commit id"


def test_the_result_does_not_depend_on_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same tracked content, one repo uncommitted and one with history: identical output."""
    files: dict[str, bytes | str] = {"a.md": f"commit {SHORT}\n", "b.txt": f"{FULL}\n"}
    fresh = _git_repo(tmp_path / "fresh", files)
    with_history = _git_repo(tmp_path / "history", files, commit=True)
    _git(
        with_history,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "second",
    )
    fresh_result = _check(fresh, capsys)
    history_result = _check(with_history, capsys)
    assert fresh_result == history_result
    assert fresh_result[0] == 1


def test_an_empty_scope_exits_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _git_repo(tmp_path / "repo", {})
    rc, out = _check(root, capsys)
    assert rc == gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS
    assert out.startswith("[SKIP] check_no_commit_ids")


def test_a_binary_only_scope_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _git_repo(tmp_path / "repo", {"blob.bin": b"\0\1\2" + FULL.encode()})
    rc, _ = _check(root, capsys)
    assert rc == gate.SKIP_EXIT_STATUS


def test_a_directory_that_is_not_a_work_tree_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = gate.main(["--repo-root", str(tmp_path)])
    assert rc == 2
    assert "ERROR" in capsys.readouterr().err


def test_the_gate_is_registered_in_run_gates() -> None:
    registered = dict(run_gates.GATES)
    assert registered["check_no_commit_ids"][1:] == ["scripts/check_no_commit_ids.py"]


# ---------------------------------------------------------------------------------------------
# Third-party GitHub Action pins
# ---------------------------------------------------------------------------------------------

PIN = "11bd71" + "901bbe" + "5b1630" + "ceea73" + "d27597" + "364c9a" + "f683"


@pytest.mark.parametrize(
    "line",
    [
        f"        uses: actions/checkout@{PIN} # v4.2.2",
        f"      - uses: hashicorp/setup-terraform@{PIN}",
        f"        uses: 'actions/cache/restore@{PIN}'",
        f'        "uses: actions/checkout@{PIN}"',
    ],
    ids=["workflow-step", "list-item", "quoted-subpath", "python-literal"],
)
def test_an_action_pin_is_not_reported(line: str) -> None:
    assert _rules(f"{line}\n") == []


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (f"        run: git checkout {FULL}", "full-length"),
        (f"        # pinned to commit {SHORT}", "commit-context"),
        (f"        ref: {FULL}", "full-length"),
        (f"        uses: ./local-action@{FULL}", "full-length"),
        (f"        uses: actions/checkout@v4 # was {FULL}", "full-length"),
    ],
    ids=[
        "bare-sha-in-run",
        "commit-comment",
        "ref-key",
        "local-path-not-owner-repo",
        "sha-after-tag",
    ],
)
def test_other_ids_in_a_workflow_file_still_red(line: str, rule: str) -> None:
    pin_line = f"        uses: actions/checkout@{PIN} # v4.2.2"
    text = f"steps:\n{pin_line}\n{line}\n"
    assert [(n, r) for n, _, r in gate.scan_text(text)] == [(3, rule)]


def test_the_tracked_workflow_and_ci_parity_pins_are_not_reported() -> None:
    paths = [
        *sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")),
        REPO_ROOT / "tests" / "unit" / "test_ci_parity.py",
    ]
    pinned = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        pinned += len(gate._ACTION_PIN_RE.findall(text))
        assert gate.scan_text(text) == [], path
    assert pinned >= 11


# ---------------------------------------------------------------------------------------------
# The `sha-<12 hex>` container image tag (`infra/main.tf` `agent_image_tag`)
# ---------------------------------------------------------------------------------------------

TAG_HEX = "ee70e1" + "37221f"
BUILD_LINE = f'    - \\"docker build -t ${{ECR_REPOSITORY_URI}}:sha-{TAG_HEX} .\\"'
PUSH_LINE = f'    - \\"docker push ${{ECR_REPOSITORY_URI}}:sha-{TAG_HEX}\\"'


def test_the_captured_buildspec_image_tag_is_not_reported() -> None:
    assert _rules(f'  \\"build\\":\\n{BUILD_LINE}\\n  \\"post_build\\":\\n{PUSH_LINE}\\n') == []


@pytest.mark.parametrize(
    "line",
    [f"rev {TAG_HEX}", f'"git_revision": "{TAG_HEX}",', f"pinned at {TAG_HEX}"],
    ids=["rev-keyword", "commit-key", "pinned-at"],
)
def test_a_bare_12_hex_commit_reference_beside_the_tag_still_reds(line: str) -> None:
    filler = "x" * (gate.CONTEXT_BEFORE + gate.CONTEXT_AFTER)
    text = f"{BUILD_LINE}\n{filler}\n{line}\n"
    assert [(n, t, r) for n, t, r in gate.scan_text(text)] == [(3, TAG_HEX, "commit-context")]


def test_a_tag_named_as_a_commit_still_reds() -> None:
    assert _rules(f"built from commit sha-{TAG_HEX} yesterday\n") == [(TAG_HEX, "commit-context")]


@pytest.mark.parametrize(
    "tag",
    [f"sha-{TAG_HEX[:11]}", f"sha-{TAG_HEX}a", f"sha-{TAG_HEX.upper()}"],
    ids=["11-hex", "13-hex", "uppercase"],
)
def test_only_the_exact_tag_shape_is_exempt(tag: str) -> None:
    assert [r for _, r in _rules(f"image :{tag} .\n")] == ["commit-context"]


def test_the_tracked_compliant_plan_bases_image_tags_are_not_reported() -> None:
    path = REPO_ROOT / "tests" / "fixtures" / "plans" / "compliant.base.ndjson"
    text = path.read_text(encoding="utf-8")
    tags = {match.group(0).removeprefix("sha-") for match in gate._IMAGE_TAG_RE.finditer(text)}
    assert tags, "the fixture no longer carries a sha-<12 hex> image tag"
    assert {token for _, token, _ in gate.scan_text(text)} & tags == set()
