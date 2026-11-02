# SPDX-License-Identifier: MIT
"""`scripts/check_no_untracked_refs.py` reports references to untracked content.

Every planted reference is assembled at runtime from fragments that are not references themselves,
so this file carries no reference the gate would report.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import check_no_untracked_refs as gate
from scripts import proc_run, run_gates

REPO_ROOT = Path(__file__).resolve().parents[2]

DECISION = "D-" + "42"
REQUIREMENT = "FR-" + "6.2"
FINDING = "E2E1" + "-W3"
SECTION = "design " + "\u00a7" + "2.4"
CLAUDE_DIR = "." + "claude/"
DECISIONS_FILE = "decisions" + ".md"


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    proc_run.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    if files:
        proc_run.run(["git", "-C", str(root), "add", "--", *files], check=True, capture_output=True)
    return root


def _check(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = gate.main(["--repo-root", str(root)])
    return rc, capsys.readouterr().out


@pytest.fixture
def no_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against a scratch repository with no allowlist or exemption entries to back."""
    monkeypatch.setattr(gate, "ALLOWED_IDS", ())
    monkeypatch.setattr(gate, "EXEMPTIONS", ())


@pytest.fixture
def comments_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan comments and docstrings only, so a test isolates the comment lexer."""
    monkeypatch.setattr(gate, "SCAN_STRING_LITERALS", False)


def _tokens(path: str, text: str) -> list[tuple[int, str, str]]:
    found = gate.scan_file(path, text)
    assert found is not None
    return [(f.line, f.token, f.kind) for f in found]


# ---------------------------------------------------------------------------------------------
# Comment and docstring text is scanned, per language
# ---------------------------------------------------------------------------------------------


def test_a_planted_python_comment_reference_reds_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_entries: None
) -> None:
    root = _git_repo(tmp_path / "r", {"a.py": f"x = 1  # see {DECISION}\n"})
    rc, out = _check(root, capsys)
    assert rc == 1
    assert f"a.py:1: {DECISION} (comment)" in out


def test_a_docstring_reference_is_reported() -> None:
    text = f'def f() -> None:\n    """Do it ({REQUIREMENT})."""\n'
    assert _tokens("m.py", text) == [(2, REQUIREMENT, "docstring")]


def test_a_python_string_literal_reference_reds_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_entries: None
) -> None:
    root = _git_repo(tmp_path / "r", {"a.py": f'NOTE = "see {DECISION}"\n'})
    rc, out = _check(root, capsys)
    assert rc == 1
    assert f"a.py:1: {DECISION} (string)" in out


def test_a_python_f_string_reference_is_reported() -> None:
    text = 'n = 1\nNOTE = f"see {n} ' + DECISION + '"\n'
    assert _tokens("a.py", text) == [(2, DECISION, "string")]


def test_a_neutral_token_in_a_string_literal_passes() -> None:
    assert _tokens("a.py", 'ALGO = "SHA-256 over UTF-8, PD-01"\n') == []


def test_turning_the_switch_off_skips_string_literals(comments_only: None) -> None:
    assert _tokens("a.py", f'NOTE = "see {DECISION}"\n') == []
    assert _tokens("x.ts", f'const s = "{DECISION}";\n') == []


def test_the_gates_own_pattern_literals_are_self_exempt() -> None:
    text = f'PATTERN = "{DECISION}"  # {REQUIREMENT}\n'
    assert _tokens(gate.SELF_PATH, text) == [(1, REQUIREMENT, "comment")]
    assert _tokens("scripts/other_gate.py", text) == [
        (1, DECISION, "string"),
        (1, REQUIREMENT, "comment"),
    ]


def test_typescript_comments_and_string_literals_are_scanned() -> None:
    text = (
        f"// line {DECISION}\n"
        f"/* block\n   {FINDING} */\n"
        f'const s = "{REQUIREMENT}";\n'
        f"const t = `{REQUIREMENT}`;\n"
    )
    assert _tokens("x.ts", text) == [
        (1, DECISION, "comment"),
        (3, FINDING, "comment"),
        (4, REQUIREMENT, "string"),
        (5, REQUIREMENT, "string"),
    ]


def test_an_unterminated_quote_does_not_shift_later_line_numbers(comments_only: None) -> None:
    text = f"const a = 'open\nconst b = 1;\n// {DECISION}\n"
    assert _tokens("x.ts", text) == [(3, DECISION, "comment")]


def test_a_line_comment_after_an_unterminated_quote_on_the_same_line_is_scanned(
    comments_only: None,
) -> None:
    text = f'<p>Don\'t</p> // {DECISION}\nconst s = "x // {REQUIREMENT}";\n'
    assert _tokens("x.tsx", text) == [(1, DECISION, "comment")]


def test_a_template_literal_spans_lines_and_keeps_line_numbers(comments_only: None) -> None:
    text = f"const t = `a\n// {REQUIREMENT}\nb`;\n// {DECISION}\n"
    assert _tokens("x.ts", text) == [(4, DECISION, "comment")]


def test_terraform_comments_and_string_literals_are_scanned() -> None:
    text = f'# a {DECISION}\nname = "{REQUIREMENT}" // b {FINDING}\n/* {SECTION} */\n'
    assert [token for _, token, _ in _tokens("main.tf", text)] == [
        DECISION,
        REQUIREMENT,
        FINDING,
        "\u00a7",
    ]


def test_a_yaml_value_reference_is_reported_with_its_comments() -> None:
    yaml_text = (
        f"# header {DECISION}\n"
        f"key: {REQUIREMENT}\n"
        f"quoted: 'x # {FINDING}'\n"
        f"notes: >-\n  folded text {DECISION}\n"
    )
    assert _tokens("c.yml", yaml_text) == [
        (1, DECISION, "comment"),
        (2, REQUIREMENT, "value"),
        (3, FINDING, "value"),
        (5, DECISION, "value"),
    ]


def test_a_yaml_value_reference_reds_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_entries: None
) -> None:
    root = _git_repo(tmp_path / "r", {"tasks.yaml": f"- id: t1\n  notes: see {DECISION}\n"})
    rc, out = _check(root, capsys)
    assert rc == 1
    assert f"tasks.yaml:2: {DECISION} (value)" in out


def test_a_json_value_reference_reds_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_entries: None
) -> None:
    body = '{\n  "Comment": "Sweep (' + DECISION + ')",\n  "n": 1\n}\n'
    root = _git_repo(tmp_path / "r", {"sweep.asl.json": body})
    rc, out = _check(root, capsys)
    assert rc == 1
    assert f"sweep.asl.json:2: {DECISION} (value)" in out


def test_json_and_ndjson_strings_are_scanned_and_numbers_are_not() -> None:
    assert _tokens("a.json", '{"k": "v ' + REQUIREMENT + ' \\" ' + DECISION + '", "D": 42}\n') == [
        (1, REQUIREMENT, "value"),
        (1, DECISION, "value"),
    ]
    assert _tokens("p.ndjson", '{"a": 1}\n{"d": "' + FINDING + '"}\n') == [(2, FINDING, "value")]


def test_shell_hash_comments_are_scanned_but_strings_are_not() -> None:
    shell_text = f'echo "{REQUIREMENT} $#" # count {DECISION}\n'
    assert _tokens("s.sh", shell_text) == [(1, DECISION, "comment")]


def test_markdown_prose_is_scanned_whole() -> None:
    assert _tokens("doc.md", f"# Title\n\nSee {DECISION}.\n") == [(3, DECISION, "doc")]


def test_markup_files_are_scanned_only_inside_html_comments() -> None:
    text = f"<svg><!-- {DECISION} --><text>{REQUIREMENT}</text></svg>\n"
    assert _tokens("f.svg", text) == [(1, DECISION, "comment")]


def test_untracked_file_names_and_spec_slugs_are_references() -> None:
    slug = gate.SPEC_SLUGS[0]
    text = f"# see {CLAUDE_DIR}specs/{slug}/{DECISIONS_FILE} and {slug}\n"
    tokens = [token for _, token, _ in _tokens("a.sh", text)]
    assert tokens == [CLAUDE_DIR, slug, DECISIONS_FILE, slug]


@pytest.mark.parametrize("text", ["# SHA-256 and UTF-8\n", "# ISO-8601 and B310\n", "# PD-01\n"])
def test_ordinary_hyphenated_tokens_are_not_references(text: str) -> None:
    assert _tokens("a.sh", text) == []


@pytest.mark.parametrize(
    "token",
    ["W" + "1", "H" + "12", "C-" + "2", "M-" + "1", "H-" + "11", "G2R-C" + "-1", "G2C1-W" + "1"],
)
def test_short_finding_ids_are_references(token: str) -> None:
    assert _tokens("a.sh", f"# ({token}) the property\n") == [(1, token, "comment")]


@pytest.mark.parametrize(
    "text",
    [
        "# C0/C1 controls\n",
        "# an S3 bucket\n",
        "# [l](src/app.py#L3)\n",
        "# prompt B-1 and A-1\n",
        "# disposition H-4\n",
    ],
)
def test_short_ids_that_are_not_references_pass(text: str) -> None:
    assert _tokens("a.sh", text) == []


def test_an_out_of_scope_file_type_is_not_scanned() -> None:
    assert gate.scan_file("data.csv", f"note,{DECISION}\n") is None


# ---------------------------------------------------------------------------------------------
# Allowlist and exemptions
# ---------------------------------------------------------------------------------------------


def test_an_allowlisted_id_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "ALLOWED_IDS", (gate.AllowedId(DECISION, "defs.md"),))
    monkeypatch.setattr(gate, "EXEMPTIONS", ())
    root = _git_repo(
        tmp_path / "r", {"defs.md": f"{DECISION} is defined here.\n", "a.py": f"# {DECISION}\n"}
    )
    rc, out = _check(root, capsys)
    assert rc == 0, out


def test_an_allowlist_entry_its_defining_file_no_longer_carries_reds_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "ALLOWED_IDS", (gate.AllowedId(DECISION, "defs.md"),))
    monkeypatch.setattr(gate, "EXEMPTIONS", ())
    root = _git_repo(tmp_path / "r", {"defs.md": "Nothing defined.\n"})
    rc, out = _check(root, capsys)
    assert rc == 1
    assert "stale allowlist entry" in out


def _exemption() -> gate.Exemption:
    return gate.Exemption("doc.md", DECISION, "reader.txt", "the reader asserts on it")


def test_an_exempt_reference_with_a_live_reader_passes_in_the_doc_and_the_reader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "ALLOWED_IDS", ())
    exemption = gate.Exemption("doc.md", DECISION, "reader.py", "the reader asserts on it")
    monkeypatch.setattr(gate, "EXEMPTIONS", (exemption,))
    root = _git_repo(
        tmp_path / "r",
        {"doc.md": f"Heading ({DECISION})\n", "reader.py": f'TOKEN = "{DECISION}"\n'},
    )
    rc, out = _check(root, capsys)
    assert rc == 0, out


def test_an_exemption_whose_reader_is_gone_reds_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "ALLOWED_IDS", ())
    monkeypatch.setattr(gate, "EXEMPTIONS", (_exemption(),))
    root = _git_repo(tmp_path / "r", {"doc.md": f"Heading ({DECISION})\n"})
    rc, out = _check(root, capsys)
    assert rc == 1
    assert f"reader.txt:0: {DECISION} (stale exemption reader)" in out


def test_an_exemption_covers_only_its_own_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "ALLOWED_IDS", ())
    monkeypatch.setattr(gate, "EXEMPTIONS", (_exemption(),))
    root = _git_repo(
        tmp_path / "r",
        {
            "doc.md": f"Heading ({DECISION})\n",
            "other.md": f"Also {DECISION}\n",
            "reader.txt": f'TOKEN = "{DECISION}"\n',
        },
    )
    rc, out = _check(root, capsys)
    assert rc == 1
    assert f"other.md:1: {DECISION} (doc)" in out
    assert "doc.md:1" not in out.replace("other.md", "")


def test_every_real_allowlist_and_exemption_entry_names_a_tracked_file() -> None:
    files = set(gate.tracked_files(REPO_ROOT))
    for allowed in gate.ALLOWED_IDS:
        assert allowed.defined_in in files, allowed
    for exemption in gate.EXEMPTIONS:
        assert exemption.path in files, exemption
        assert exemption.reader in files, exemption


# ---------------------------------------------------------------------------------------------
# Exit statuses and the real tree
# ---------------------------------------------------------------------------------------------


def test_a_repository_with_nothing_scannable_is_a_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_entries: None
) -> None:
    root = _git_repo(tmp_path / "r", {"data.csv": "a,b\n"})
    rc, out = _check(root, capsys)
    assert rc == gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS
    assert "[SKIP]" in out


def test_a_directory_that_is_not_a_git_work_tree_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "plain").mkdir()
    assert gate.main(["--repo-root", str(tmp_path / "plain")]) == 2


def test_the_real_tree_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _check(REPO_ROOT, capsys)
    assert rc == 0, out


def test_the_gate_is_registered_in_run_gates() -> None:
    argv = dict(run_gates.GATES)["check_no_untracked_refs"]
    assert argv[-1] == "scripts/check_no_untracked_refs.py"
