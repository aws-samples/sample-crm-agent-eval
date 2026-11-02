# SPDX-License-Identifier: MIT
"""Tests for `scripts/comment_sweep_check.py`, the control that proves an edit touched no code.

Each case pairs a comment-or-docstring-only edit, which must report `OK`, with a code change on the
same file, which must report `FAIL`. A ``*``-leading line inside an HCL ``description`` heredoc or a
``local-exec`` shell ``case`` branch is configuration, not comment, and so is a comment-shaped line
inside a heredoc, a multi-line quoted shell string, or a JS/TS template literal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL = REPO_ROOT / "scripts" / "comment_sweep_check.py"


def _git(repo: Path, *args: str) -> None:
    proc_run.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository whose HEAD carries one file of each surface the control handles."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "mod.py").write_text(
        '"""Module docstring."""\n\n\ndef f(x: int) -> int:\n    """Doc."""\n    # a comment\n'
        "    return x + 1\n",
        encoding="utf-8",
    )
    (tmp_path / "s.sh").write_text(
        "#!/usr/bin/env bash\n# a comment\nexport A=1\n", encoding="utf-8"
    )
    (tmp_path / "m.tf").write_text(
        '/**\n * A header comment.\n */\n# a line comment\nvariable "v" {\n'
        "  description = <<-EOT\n    **DELIBERATELY NO DEFAULT**\n  EOT\n"
        "  default = null\n}\n"
        'resource "null_resource" "r" {\n  provisioner "local-exec" {\n'
        '    command = "case $x in\\n      *) echo other ;;\\n    esac"\n  }\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "hook").write_text("#!/bin/sh\n# a comment\necho hi\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def _run(repo: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(CONTROL), "HEAD", *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_comment_only_python_edit_is_reported_identical(repo: Path) -> None:
    path = repo / "mod.py"
    path.write_text(path.read_text(encoding="utf-8").replace("# a comment", "# reworded"), "utf-8")
    result = _run(repo, "mod.py")
    assert result.returncode == 0, result.stdout
    assert "checked=1 failed=0" in result.stdout


def test_a_docstring_only_python_edit_is_reported_identical(repo: Path) -> None:
    path = repo / "mod.py"
    path.write_text(path.read_text(encoding="utf-8").replace('"""Doc."""', '"""Other."""'), "utf-8")
    result = _run(repo, "mod.py")
    assert result.returncode == 0, result.stdout
    assert "checked=1 failed=0" in result.stdout


def test_a_python_code_change_fails(repo: Path) -> None:
    path = repo / "mod.py"
    path.write_text(path.read_text(encoding="utf-8") + "\nMUTANT = 1\n", encoding="utf-8")
    result = _run(repo, "mod.py")
    assert result.returncode == 1, result.stdout
    assert "checked=1 failed=1" in result.stdout
    assert "code shape CHANGED" in result.stdout


def test_a_shell_comment_edit_passes_and_a_code_append_fails(repo: Path) -> None:
    path = repo / "s.sh"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("# a comment", "# reworded"), encoding="utf-8")
    assert "checked=1 failed=0" in _run(repo, "s.sh").stdout

    path.write_text(original + "export B=2\n", encoding="utf-8")
    failed = _run(repo, "s.sh")
    assert failed.returncode == 1, failed.stdout
    assert "code lines CHANGED" in failed.stdout


def test_a_terraform_block_header_rewrite_passes(repo: Path) -> None:
    path = repo / "m.tf"
    path.write_text(
        path.read_text(encoding="utf-8").replace(" * A header comment.", " * Reworded header."),
        encoding="utf-8",
    )
    result = _run(repo, "m.tf")
    assert result.returncode == 0, result.stdout
    assert "checked=1 failed=0" in result.stdout


def test_an_hcl_description_edit_fails_although_the_line_starts_with_an_asterisk(
    repo: Path,
) -> None:
    """A `description` heredoc line starting with markdown bold is code, so editing it fails."""
    path = repo / "m.tf"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "**DELIBERATELY NO DEFAULT**", "**PROBE** edited description string"
        ),
        encoding="utf-8",
    )
    result = _run(repo, "m.tf")
    assert result.returncode == 1, result.stdout
    assert "code lines CHANGED" in result.stdout


def test_a_shell_case_branch_inside_a_command_string_is_not_treated_as_a_comment(
    repo: Path,
) -> None:
    """`*)` is executable code inside a `local-exec` command, not a block-comment continuation."""
    path = repo / "m.tf"
    path.write_text(
        path.read_text(encoding="utf-8").replace("*) echo other ;;", "*) echo mutated ;;"),
        encoding="utf-8",
    )
    result = _run(repo, "m.tf")
    assert result.returncode == 1, result.stdout
    assert "code lines CHANGED" in result.stdout


def test_an_extensionless_shell_file_is_covered_rather_than_skipped(repo: Path) -> None:
    """A file with no suffix and a shell shebang is checked rather than skipped."""
    path = repo / "hook"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("# a comment", "# reworded"), encoding="utf-8")
    passing = _run(repo, "hook")
    assert "checked=1 failed=0" in passing.stdout, passing.stdout
    assert "SKIP" not in passing.stdout

    path.write_text(original + "echo appended\n", encoding="utf-8")
    assert _run(repo, "hook").returncode == 1


def test_the_checked_count_equals_the_paths_given(repo: Path) -> None:
    """`checked=` equals the number of paths given."""
    result = _run(repo, "mod.py", "s.sh", "m.tf", "hook")
    assert "checked=4 failed=0" in result.stdout, result.stdout


def test_it_refuses_to_report_a_pass_having_checked_nothing(repo: Path) -> None:
    """A bare invocation must not exit 0: `checked=0 failed=0` reads exactly like a pass."""
    bare = proc_run.run(
        [sys.executable, str(CONTROL)], cwd=repo, capture_output=True, text=True, check=False
    )
    assert bare.returncode == 2, bare.stdout + bare.stderr
    assert "refusing to report a pass having checked nothing" in bare.stderr

    ref_only = proc_run.run(
        [sys.executable, str(CONTROL), "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ref_only.returncode == 2, ref_only.stdout + ref_only.stderr


def test_a_path_absent_from_the_ref_is_skipped_not_counted(repo: Path) -> None:
    (repo / "new.py").write_text("X = 1\n", encoding="utf-8")
    result = _run(repo, "new.py")
    assert result.returncode == 0, result.stdout
    assert "checked=0 failed=0" in result.stdout
    assert "SKIP" in result.stdout


_SHELL_WITH_LITERALS = (
    "#!/usr/bin/env bash\n"
    "# a real comment\n"
    "cat <<EOF\n"
    "# heredoc body line\n"
    "EOF\n"
    "cat <<'QUOTED'\n"
    "# quoted heredoc body line\n"
    "QUOTED\n"
    "cat <<-TABBED\n"
    "\t# tabbed heredoc body line\n"
    "\tTABBED\n"
    "echo 'first\n"
    "# single-quoted continuation line\n"
    "last'\n"
    'echo "${#x} $# a#b"  # trailing comment\n'
    "# a closing comment\n"
)

_HCL_WITH_LITERALS = (
    "# a real comment\n"
    'resource "null_resource" "r" {\n'
    "  triggers = {\n"
    "    script = <<EOF\n"
    "# hcl heredoc body line\n"
    "EOF\n"
    '    command = "HOST=$${URI%%/*}"\n'
    "  }\n"
    "}\n"
    "# a closing comment\n"
)

_TS_WITH_LITERALS = (
    "// a real comment\n"
    "const sql = `\n"
    "// template body line\n"
    "${`nested ${1}`}\n"
    "`;\n"
    'const apostrophe = "it\'s"; // trailing comment\n'
    "// a closing comment\n"
    "export { sql, apostrophe };\n"
)


@pytest.fixture
def literal_repo(tmp_path: Path) -> Path:
    """A git repository whose HEAD carries comment-shaped lines inside string literals."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "lit.sh").write_text(_SHELL_WITH_LITERALS, encoding="utf-8")
    (tmp_path / "lit.tf").write_text(_HCL_WITH_LITERALS, encoding="utf-8")
    (tmp_path / "lit.ts").write_text(_TS_WITH_LITERALS, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("lit.sh", "# heredoc body line"),
        ("lit.sh", "# quoted heredoc body line"),
        ("lit.sh", "# tabbed heredoc body line"),
        ("lit.sh", "# single-quoted continuation line"),
        ("lit.tf", "# hcl heredoc body line"),
        ("lit.ts", "// template body line"),
    ],
    ids=["sh-heredoc", "sh-quoted-heredoc", "sh-dash-heredoc", "sh-quoted-string", "hcl", "ts"],
)
def test_editing_a_comment_shaped_line_inside_a_literal_fails(
    literal_repo: Path, name: str, line: str
) -> None:
    """A `#` or `//` line inside a heredoc, quoted string or template literal is code."""
    _replace(literal_repo / name, line, line + " edited")
    result = _run(literal_repo, name)
    assert result.returncode == 1, result.stdout
    assert "code lines CHANGED" in result.stdout


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("lit.sh", "# a real comment"),
        ("lit.sh", "# a closing comment"),
        ("lit.tf", "# a real comment"),
        ("lit.tf", "# a closing comment"),
        ("lit.ts", "// a real comment"),
        ("lit.ts", "// a closing comment"),
    ],
)
def test_editing_a_real_comment_beside_literals_passes(
    literal_repo: Path, name: str, line: str
) -> None:
    """A whole-line comment before or after a closed literal region is still a comment."""
    _replace(literal_repo / name, line, line + " reworded")
    result = _run(literal_repo, name)
    assert result.returncode == 0, result.stdout
    assert "checked=1 failed=0" in result.stdout


def test_a_trailing_comment_is_counted_but_a_hash_inside_an_expansion_is_not(
    literal_repo: Path,
) -> None:
    """`${#x}`, `$#` and `a#b` are not comments; the trailing `# trailing comment` is."""
    result = _run(literal_repo, "lit.sh")
    assert "1 trailing-comment line(s) UNVERIFIED" in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("open.sh", "#!/bin/sh\ncat <<EOF\n# body never closed\n"),
        ("open.ts", "const t = `\n// body never closed\n"),
        ("open.tf", "x = <<EOT\n# body never closed\n"),
    ],
)
def test_an_unterminated_literal_is_reported_unverified_not_passed(
    literal_repo: Path, name: str, text: str
) -> None:
    """A literal region still open at end of file is reported UNVERIFIED with a non-zero exit."""
    path = literal_repo / name
    path.write_text(text, encoding="utf-8")
    _git(literal_repo, "add", name)
    _git(literal_repo, "commit", "-q", "-m", "open literal")
    result = _run(literal_repo, name)
    assert result.returncode != 0, result.stdout
    assert f"UNVERIFIED {name}" in result.stdout
    assert "unverified=1" in result.stdout


def test_index_mode_reads_the_staged_content_not_the_worktree(literal_repo: Path) -> None:
    """`--index` compares the ref with the staged blob, so a staged code change fails it."""
    path = literal_repo / "lit.sh"
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "export STAGED=1\n", encoding="utf-8")
    _git(literal_repo, "add", "lit.sh")
    path.write_text(original, encoding="utf-8")

    worktree = _run(literal_repo, "lit.sh")
    assert worktree.returncode == 0, worktree.stdout

    index = proc_run.run(
        [sys.executable, str(CONTROL), "--index", "HEAD", "lit.sh"],
        cwd=literal_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert index.returncode == 1, index.stdout
    assert "code lines CHANGED" in index.stdout


def test_index_mode_passes_a_staged_comment_edit_despite_a_worktree_code_change(
    literal_repo: Path,
) -> None:
    """`--index` ignores unstaged worktree content."""
    path = literal_repo / "lit.sh"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("# a real comment", "# reworded"), encoding="utf-8")
    _git(literal_repo, "add", "lit.sh")
    path.write_text(original + "export UNSTAGED=1\n", encoding="utf-8")

    index = proc_run.run(
        [sys.executable, str(CONTROL), "--index", "HEAD", "lit.sh"],
        cwd=literal_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert index.returncode == 0, index.stdout
    assert "checked=1 failed=0" in index.stdout


_TYPESCRIPT_PACKAGE = REPO_ROOT / "frontend" / "node_modules" / "typescript"

_TSX = (
    "// a real comment\n"
    "export function Panel() {\n"
    "  return (\n"
    "    <div>\n"
    "      {/* a jsx comment */}\n"
    "      <p>\n"
    "        // rendered text\n"
    "      </p>\n"
    "    </div>\n"
    "  );\n"
    "}\n"
    "export const a = <p>/* not a comment</p>;\n"
    "const b = 2;\n"
    "export const c = <p>*/</p>;\n"
    "export function f() {\n"
    "  return /* inline */ b;\n"
    "}\n"
)


def _typescript_unavailable_reason() -> str | None:
    missing: list[str] = []
    if shutil.which("node") is None:
        missing.append("`node` is not on PATH")
    if not _TYPESCRIPT_PACKAGE.is_dir():
        missing.append(
            f"no TypeScript package at {_TYPESCRIPT_PACKAGE} — run `cd frontend && npm ci`"
        )
    return "; ".join(missing) if missing else None


requires_typescript = pytest.mark.skipif(
    _typescript_unavailable_reason() is not None,
    reason=f"the .tsx comparison needs node and TypeScript: {_typescript_unavailable_reason()}",
)


@pytest.fixture
def tsx_repo(tmp_path: Path) -> Path:
    """A git repository whose HEAD carries a .tsx file with JSX text shaped like comments."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "p.tsx").write_text(_TSX, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


@requires_typescript
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("// rendered text", "// rendered text edited"),
        ("const b = 2;", "const b = 3;"),
        ("      {/* a jsx comment */}\n", ""),
        ("return /* inline */ b;", "return /*\n */ b;"),
    ],
    ids=["jsx-text-slashes", "code-after-jsx-text-block-open", "jsx-comment-removed", "asi"],
)
def test_a_tsx_code_or_rendered_text_change_fails(tsx_repo: Path, old: str, new: str) -> None:
    """JSX text is rendered content, and a newline in a comment can change automatic semicolons."""
    _replace(tsx_repo / "p.tsx", old, new)
    result = _run(tsx_repo, "p.tsx")
    assert result.returncode == 1, result.stdout
    assert "syntax tree CHANGED" in result.stdout


@requires_typescript
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("// a real comment", "// a real comment reworded"),
        ("{/* a jsx comment */}", "{/* a reworded jsx comment */}"),
        ("/* inline */", "/* reworded */"),
    ],
    ids=["line-comment", "jsx-comment", "block-comment"],
)
def test_a_tsx_comment_edit_passes(tsx_repo: Path, old: str, new: str) -> None:
    _replace(tsx_repo / "p.tsx", old, new)
    result = _run(tsx_repo, "p.tsx")
    assert result.returncode == 0, result.stdout
    assert "checked=1 failed=0 unverified=0" in result.stdout


@requires_typescript
def test_a_tsx_file_that_does_not_parse_is_unverified_not_passed(tsx_repo: Path) -> None:
    path = tsx_repo / "p.tsx"
    path.write_text(_TSX + "export const broken = <p>;\n", encoding="utf-8")
    _git(tsx_repo, "commit", "-q", "-am", "syntax error")
    _replace(path, "// a real comment", "// a real comment reworded")
    result = _run(tsx_repo, "p.tsx")
    assert result.returncode == 3, result.stdout
    assert "UNVERIFIED p.tsx" in result.stdout


def test_a_tsx_file_is_unverified_when_node_is_unavailable(tsx_repo: Path, tmp_path: Path) -> None:
    """Without `node` the .tsx comparison cannot run, so the file is reported UNVERIFIED."""
    git = shutil.which("git")
    assert git is not None
    bin_dir = tmp_path / "bin-without-node"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(git)
    _replace(tsx_repo / "p.tsx", "// a real comment", "// a real comment reworded")
    result = proc_run.run(
        [sys.executable, str(CONTROL), "HEAD", "p.tsx"],
        cwd=tsx_repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": str(bin_dir)},
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "UNVERIFIED p.tsx" in result.stdout
    assert "node" in result.stdout
