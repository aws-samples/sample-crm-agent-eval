# SPDX-License-Identifier: MIT
"""`scripts/check_doc_links.py` reports broken Markdown links and unreachable documents, and only those.

Every cell builds a scratch git repository under `tmp_path` and plants one defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import check_doc_links as gate
from scripts import proc_run, run_gates


def _repo(root: Path, files: dict[str, str], *, untracked: dict[str, str] | None = None) -> Path:
    """A git work tree holding `files` (tracked) and `untracked` (on disk only)."""
    root.mkdir(parents=True, exist_ok=True)
    proc_run.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for rel, body in {**files, **(untracked or {})}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    if files:
        proc_run.run(["git", "-C", str(root), "add", "--", *files], check=True, capture_output=True)
    return root


def _check(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    rc = gate.main(["--repo-root", str(root)])
    lines = capsys.readouterr().out.splitlines()
    return rc, [line for line in lines if not line.startswith("check_doc_links:")]


def _findings(root: Path) -> list[tuple[str, int, str, str]]:
    _, findings = gate.run_check(root)
    return sorted((f.path, f.line, f.rule, f.detail) for f in findings)


# ---------------------------------------------------------------------------------------------
# Exit codes and the clean case
# ---------------------------------------------------------------------------------------------


def test_a_clean_tree_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(
        tmp_path,
        {
            "README.md": "# Top\n\nSee [docs](docs/guide.md#usage) and [code](src/app.py).\n",
            "docs/guide.md": "# Guide\n\n## Usage\n\nBack to [top](../README.md#top).\n",
            "src/app.py": "print()\n",
        },
    )
    assert _check(root, capsys) == (0, [])


def test_the_skip_status_matches_run_gates() -> None:
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS


def test_no_tracked_markdown_is_a_skip_even_with_an_untracked_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"src/app.py": "x = 1\n"}, untracked={"README.md": "[x](gone.md)\n"})
    rc = gate.main(["--repo-root", str(root)])
    assert rc == gate.SKIP_EXIT_STATUS
    assert "[SKIP]" in capsys.readouterr().out


def test_a_directory_that_is_not_a_work_tree_is_an_environment_error(tmp_path: Path) -> None:
    assert gate.main(["--repo-root", str(tmp_path)]) == 2


def test_findings_are_reported_as_path_line_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": "# T\n\nline\n[gone](missing.md)\n"})
    assert _check(root, capsys) == (1, ["README.md:4: missing-target: missing.md"])


# ---------------------------------------------------------------------------------------------
# Rule 1: relative targets exist, case-sensitively, among tracked files
# ---------------------------------------------------------------------------------------------


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "[a](docs/nope.md)\n![i](img/x.png)\n"})
    assert _findings(root) == [
        ("README.md", 1, "missing-target", "docs/nope.md"),
        ("README.md", 2, "missing-target", "img/x.png"),
    ]


def test_a_wrong_case_target_is_reported_even_on_a_case_insensitive_filesystem(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, {"README.md": "[a](Docs/Guide.md)\n", "docs/guide.md": "# G\n"})
    assert ("README.md", 1, "missing-target", "Docs/Guide.md") in _findings(root)


def test_an_untracked_file_on_disk_is_not_a_valid_target(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "[a](local.md)\n"}, untracked={"local.md": "# L\n"})
    assert _findings(root) == [("README.md", 1, "missing-target", "local.md")]


def test_a_directory_target_and_a_root_absolute_target_resolve(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "README.md": "[d](docs/) [r](/docs/guide.md) [here](./)\n",
            "docs/guide.md": "# G\n\n[home](/README.md)\n",
        },
    )
    assert _findings(root) == []


def test_a_target_outside_the_repository_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "[up](../elsewhere.md)\n"})
    rule = _findings(root)[0][2:]
    assert rule == ("missing-target", "../elsewhere.md (outside the repository)")


def test_a_reference_definition_target_is_checked_and_a_footnote_is_not(tmp_path: Path) -> None:
    root = _repo(
        tmp_path, {"README.md": "Text[^1] and [ref][r].\n\n[r]: nope.md\n[^1]: a footnote note.\n"}
    )
    assert _findings(root) == [("README.md", 3, "missing-target", "nope.md")]


def test_a_url_encoded_target_is_decoded(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "[a](my%20doc.md)\n", "my doc.md": "# D\n"})
    assert _findings(root) == []


# ---------------------------------------------------------------------------------------------
# Rule 2: #anchors resolve to GitHub heading slugs
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Quick start", "quick-start"),
        ("The `foo_bar` API: v2 (beta)!", "the-foo_bar-api-v2-beta"),
        ("**Bold** and _emphasis_", "bold-and-emphasis"),
        ("snake_case stays", "snake_case-stays"),
        ("A [linked](x.md) word", "a-linked-word"),
        ("Two  spaces", "two--spaces"),
        ("Café — naïve", "café--naïve"),
        ("T3 / T4: tiers", "t3--t4-tiers"),
    ],
)
def test_slugs_follow_the_github_rule(heading: str, slug: str) -> None:
    assert gate.slugify(heading) == slug


def test_a_missing_anchor_is_reported_and_a_present_one_is_not(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "README.md": "[ok](docs/g.md#set-up) [bad](docs/g.md#nowhere)\n",
            "docs/g.md": "# G\n\n## Set up\n\n[home](../README.md)\n",
        },
    )
    assert _findings(root) == [("README.md", 1, "missing-anchor", "docs/g.md#nowhere")]


def test_a_same_file_anchor_is_resolved_against_its_own_headings(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "# Top\n\n## Install\n\n[i](#install) [x](#gone)\n"})
    assert _findings(root) == [("README.md", 5, "missing-anchor", "#gone")]


def test_a_repeated_heading_gets_a_numbered_suffix(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"README.md": "## Setup\n\n## Setup\n\n[a](#setup) [b](#setup-1) [c](#setup-2)\n"},
    )
    assert _findings(root) == [("README.md", 5, "missing-anchor", "#setup-2")]


def test_setext_headings_and_html_anchors_define_anchors(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "README.md": 'Title\n=====\n\nPart two\n--------\n\n<a id="custom"></a>\n\n'
            "[a](#title) [b](#part-two) [c](#custom)\n"
        },
    )
    assert _findings(root) == []


def test_a_heading_inside_a_code_fence_defines_no_anchor(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "```sh\n# not a heading\n```\n\n[a](#not-a-heading)\n"})
    assert _findings(root) == [("README.md", 5, "missing-anchor", "#not-a-heading")]


def test_a_fragment_on_a_non_markdown_target_is_not_checked(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "[l](src/app.py#L3)\n", "src/app.py": "x = 1\n"})
    assert _findings(root) == []


# ---------------------------------------------------------------------------------------------
# What is ignored
# ---------------------------------------------------------------------------------------------


def test_links_in_code_comments_and_external_schemes_are_ignored(tmp_path: Path) -> None:
    body = (
        "```md\n[a](fenced-missing.md)\n```\n\n"
        "~~~~\n[b](tilde-missing.md)\n~~~~\n\n"
        "Inline `[c](inline-missing.md)` code.\n\n"
        "<!-- [d](comment-missing.md) -->\n\n"
        "[e](https://example.com/x.md) [f](mailto:a@example.com) [g](//cdn.example.com/y.md)\n"
    )
    root = _repo(tmp_path, {"README.md": body})
    assert _findings(root) == []


def test_the_fence_control_reports_the_same_link_outside_a_fence(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "[a](fenced-missing.md)\n"})
    assert _findings(root) == [("README.md", 1, "missing-target", "fenced-missing.md")]


# ---------------------------------------------------------------------------------------------
# Rule 3: every tracked .md is reachable from README.md
# ---------------------------------------------------------------------------------------------


def test_an_orphan_document_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "# Top\n", "docs/orphan.md": "# O\n"})
    assert _findings(root) == [("docs/orphan.md", 1, "unreachable", "no link chain from README.md")]


def test_reachability_follows_chains_and_directory_readmes(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "README.md": "[a](docs/a.md) [m](infra/module/)\n",
            "docs/a.md": "[b](b.md)\n",
            "docs/b.md": "# B\n",
            "infra/module/README.md": "# M\n",
        },
    )
    assert _findings(root) == []


def test_a_link_only_from_an_orphan_does_not_make_a_document_reachable(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"README.md": "# Top\n", "a.md": "[b](b.md)\n", "b.md": "[a](a.md)\n"})
    assert [finding[0] for finding in _findings(root)] == ["a.md", "b.md"]


def test_an_exempt_orphan_is_not_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path, {"README.md": "# Top\n", "docs/orphan.md": "# O\n"})
    monkeypatch.setitem(gate.EXEMPT_UNREACHABLE, "docs/orphan.md", "test exemption")
    assert _findings(root) == []


def test_the_exemption_list_starts_empty() -> None:
    assert gate.EXEMPT_UNREACHABLE == {}


def test_an_untracked_root_document_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/a.md": "# A\n"}, untracked={"README.md": "[a](docs/a.md)\n"})
    assert _findings(root) == [
        ("README.md", 1, "unreachable", "the root document is not tracked"),
        ("docs/a.md", 1, "unreachable", "no link chain from README.md"),
    ]


def test_the_gate_is_registered_in_run_gates() -> None:
    registered = dict(run_gates.GATES)
    assert registered["check_doc_links"][1:] == ["scripts/check_doc_links.py"]
