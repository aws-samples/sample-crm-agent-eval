# SPDX-License-Identifier: MIT
"""`scripts/teardown.sh`'s RESIDUE notice names what destroy leaves behind and nothing it removes.

The notice must name the residue scan, the backend-removal script and every never-in-state
item, and must not claim that S3 buckets or the ECR repository fail destroy while the IaC declares
`force_destroy`/`force_delete` on each. The end-to-end cell reuses the fake-`aws`/`terraform`
sandbox from `test_teardown_deletion_protection.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_teardown_deletion_protection import TEARDOWN_SH, _teardown

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_DIR = REPO_ROOT / "infra"

_HEREDOC = re.compile(r"^cat <<'RESIDUE'\n(?P<body>.*?)^RESIDUE$", re.MULTILINE | re.DOTALL)

REQUIRED_NAMES = (
    "scripts/scan_residue.py",
    "scripts/remove_state_backend.py",
    "crm-agent-eval-eniprobe-",
    "BEFORE teardown",
    "CloudFront-VPCOrigins-Service-SG",
    "crm-agent-eval-bootstrap",
    "30 days",
    "7 for eval_artifacts",
)

FAILURE_CLAIMS = (
    re.compile(r"fail to delete the bucket", re.IGNORECASE),
    re.compile(r"may fail to remove", re.IGNORECASE),
    re.compile(r"non-empty (s3 )?bucket", re.IGNORECASE),
    re.compile(r"non-empty repository", re.IGNORECASE),
    re.compile(r"must itself be emptied", re.IGNORECASE),
)

_RESOURCE = re.compile(r'^resource\s+"(?P<type>[a-z0-9_]+)"\s+"(?P<name>[^"]+)"\s*\{', re.MULTILINE)


def _notice(script_text: str) -> str:
    match = _HEREDOC.search(script_text)
    assert match, "no quoted <<'RESIDUE' heredoc in teardown.sh"
    return match.group("body")


def _tf_files(root: Path) -> list[Path]:
    return sorted(tf for tf in root.rglob("*.tf") if ".terraform" not in tf.relative_to(root).parts)


def _resource_blocks(resource_type: str, root: Path = INFRA_DIR) -> list[tuple[str, str]]:
    """Every `resource "<type>"` block in `root`'s `*.tf` files, `.terraform/` excluded."""
    blocks: list[tuple[str, str]] = []
    for tf in _tf_files(root):
        text = tf.read_text(encoding="utf-8")
        for match in _RESOURCE.finditer(text):
            if match.group("type") != resource_type:
                continue
            body_end = text.find("\n}\n", match.end())
            assert body_end != -1, f"unterminated {resource_type} block in {tf}"
            label = f"{tf.relative_to(root)}:{match.group('name')}"
            blocks.append((label, text[match.end() : body_end]))
    return blocks


def _flag_set(blocks: list[tuple[str, str]], flag: str) -> list[str]:
    pattern = re.compile(rf"^\s*{flag}\s*=\s*true\s*$", re.MULTILINE)
    return [label for label, body in blocks if not pattern.search(body)]


def _failure_claims(notice: str) -> list[str]:
    return [p.pattern for p in FAILURE_CLAIMS if p.search(notice)]


def test_notice_names_every_residue_item_and_tool() -> None:
    notice = _notice(TEARDOWN_SH.read_text(encoding="utf-8"))
    missing = [name for name in REQUIRED_NAMES if name not in notice]
    assert not missing, f"RESIDUE notice does not name {missing}"


def test_notice_is_ascii_and_keeps_literal_placeholders() -> None:
    notice = _notice(TEARDOWN_SH.read_text(encoding="utf-8"))
    assert notice.isascii()
    assert "<p>" in notice and "<r>" in notice
    assert "$" not in notice


_BILLABLE_CLAIM = re.compile(r"\bbillable\b|\bbilled until\b|\bcharged until\b", re.IGNORECASE)


def test_notice_makes_no_billing_claim_for_keys_pending_deletion() -> None:
    notice = _notice(TEARDOWN_SH.read_text(encoding="utf-8"))
    assert "not billed while pending" in notice
    assert not _BILLABLE_CLAIM.search(notice)


def test_billing_claim_detector_reds_the_old_wording() -> None:
    assert _BILLABLE_CLAIM.search("and the key is billable until it\n    ends.")


def test_iac_force_flags_hold_for_every_bucket_and_repository() -> None:
    buckets = _resource_blocks("aws_s3_bucket")
    repos = _resource_blocks("aws_ecr_repository")
    assert buckets and repos, "IaC grep found no aws_s3_bucket or aws_ecr_repository"
    assert _flag_set(buckets, "force_destroy") == []
    assert _flag_set(repos, "force_delete") == []


def test_notice_makes_no_destroy_failure_claim_while_force_flags_hold() -> None:
    if _flag_set(_resource_blocks("aws_s3_bucket"), "force_destroy") or _flag_set(
        _resource_blocks("aws_ecr_repository"), "force_delete"
    ):
        pytest.fail("a force flag was reverted; the notice must then name the failing resource")
    notice = _notice(TEARDOWN_SH.read_text(encoding="utf-8"))
    assert _failure_claims(notice) == []


def test_failure_claim_detector_reds_a_scratch_copy_carrying_the_old_claim(tmp_path: Path) -> None:
    text = TEARDOWN_SH.read_text(encoding="utf-8")
    mutated = text.replace(
        "cat <<'RESIDUE'\n",
        "cat <<'RESIDUE'\n  - ECR images: `terraform destroy` may fail to remove a non-empty repository.\n",
        1,
    )
    assert mutated != text
    scratch = tmp_path / "teardown.sh"
    scratch.write_text(mutated, encoding="utf-8")
    assert _failure_claims(_notice(scratch.read_text(encoding="utf-8")))


def test_force_flag_grep_reds_a_scratch_copy_without_force_destroy(tmp_path: Path) -> None:
    scratch = tmp_path / "infra"
    for tf in _tf_files(INFRA_DIR):
        target = scratch / tf.relative_to(INFRA_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(tf.read_text(encoding="utf-8"), encoding="utf-8")
    assert _flag_set(_resource_blocks("aws_s3_bucket", scratch), "force_destroy") == []

    mutated_file = scratch / "modules" / "eval_artifacts" / "main.tf"
    text = mutated_file.read_text(encoding="utf-8")
    mutated = re.sub(r"^\s*force_destroy\s*=\s*true\s*$\n", "", text, count=1, flags=re.MULTILINE)
    assert mutated != text
    mutated_file.write_text(mutated, encoding="utf-8")
    assert _flag_set(_resource_blocks("aws_s3_bucket", scratch), "force_destroy") == [
        "modules/eval_artifacts/main.tf:this"
    ]


def test_notice_prints_on_a_successful_teardown(tmp_path: Path) -> None:
    run = _teardown(tmp_path)
    assert run.returncode == 0, run.stderr
    notice = _notice(TEARDOWN_SH.read_text(encoding="utf-8"))
    assert notice in run.stdout
    assert "See the manual-residue notice above." in run.stdout


def test_notice_is_not_printed_when_destroy_fails(tmp_path: Path) -> None:
    run = _teardown(tmp_path, destroy_rc=1)
    assert run.returncode == 1
    assert "scripts/scan_residue.py" not in run.stdout
