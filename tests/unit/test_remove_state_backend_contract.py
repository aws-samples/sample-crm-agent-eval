# SPDX-License-Identifier: MIT
"""The removal script names exactly the resources ``infra/bootstrap_state_backend.sh`` creates.

The bootstrap script's name assignments, KMS key description and ``backend.hcl`` ``key`` line are
parsed from the tracked file and compared with ``backend_names()``, ``STATE_KEY``,
``REQUIRED_KEY_TAG`` and ``KEY_DESCRIPTION``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import scripts.remove_state_backend as rsb

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BOOTSTRAP: Final = REPO_ROOT / "infra" / "bootstrap_state_backend.sh"
NAME_VARIABLES: Final = ("PROJECT", "KMS_ALIAS", "STATE_BUCKET", "LOG_BUCKET", "LOCK_TABLE")
ACCOUNT: Final = "123456789012"
REGION: Final = "eu-west-2"


def parse_bootstrap(text: str, account_id: str, region: str) -> dict[str, str]:
    """The bootstrap's names, ``TAG_SERVICE``, key description and backend ``key``, expanded."""
    substitutions = {"ACCOUNT_ID": account_id, "AWS_REGION_ARG": region}
    values: dict[str, str] = {}

    def expand(raw: str) -> str:
        def lookup(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            return {**substitutions, **values}[name]

        return re.sub(r"\$\{(\w+)\}|\$(\w+)", lookup, raw)

    for variable in (*NAME_VARIABLES, "TAG_SERVICE"):
        matches = re.findall(rf'^{variable}="([^"]*)"$', text, flags=re.MULTILINE)
        assert len(matches) == 1, f"expected one {variable}= assignment, found {len(matches)}"
        values[variable] = expand(matches[0])
    keys = re.findall(r'^key\s+=\s+"([^"]*)"$', text, flags=re.MULTILINE)
    assert len(keys) == 1, f"expected one backend.hcl key line, found {len(keys)}"
    values["key"] = expand(keys[0])
    descriptions = re.findall(r'^\s+--description "([^"]*)"', text, flags=re.MULTILINE)
    assert len(descriptions) == 1, f"expected one --description, found {len(descriptions)}"
    values["description"] = expand(descriptions[0])
    return values


def _assert_contract(values: dict[str, str]) -> None:
    names = rsb.backend_names(ACCOUNT, REGION)
    assert values["PROJECT"] == rsb.PROJECT
    assert values["KMS_ALIAS"] == names.kms_alias
    assert values["STATE_BUCKET"] == names.state_bucket
    assert values["LOG_BUCKET"] == names.log_bucket
    assert values["LOCK_TABLE"] == names.lock_table
    assert values["key"] == rsb.STATE_KEY
    assert ("service", values["TAG_SERVICE"]) == rsb.REQUIRED_KEY_TAG
    assert values["description"] == rsb.KEY_DESCRIPTION


def test_removal_names_equal_the_bootstrap_names() -> None:
    _assert_contract(parse_bootstrap(BOOTSTRAP.read_text(encoding="utf-8"), ACCOUNT, REGION))


@pytest.mark.parametrize(
    ("original", "mutated"),
    [
        ('LOCK_TABLE="${PROJECT}-tflock"', 'LOCK_TABLE="${PROJECT}-tf-lock"'),
        ('KMS_ALIAS="alias/${PROJECT}-tfstate"', 'KMS_ALIAS="alias/${PROJECT}-state"'),
        (
            'LOG_BUCKET="${PROJECT}-tfstate-logs-${ACCOUNT_ID}-${AWS_REGION_ARG}"',
            'LOG_BUCKET="${PROJECT}-tfstate-log-${ACCOUNT_ID}-${AWS_REGION_ARG}"',
        ),
        (
            'STATE_BUCKET="${PROJECT}-tfstate-${ACCOUNT_ID}-${AWS_REGION_ARG}"',
            'STATE_BUCKET="${PROJECT}-tfstate-${AWS_REGION_ARG}-${ACCOUNT_ID}"',
        ),
        ('key            = "crm-agent-eval/terraform.tfstate"', 'key = "crm/terraform.tfstate"'),
        ('TAG_SERVICE="$PROJECT"', 'TAG_SERVICE="crm"'),
        ("CMK for ${PROJECT} Terraform state", "CMK for ${PROJECT} state"),
    ],
    ids=[
        "lock-table",
        "kms-alias",
        "log-bucket",
        "state-bucket",
        "state-key",
        "service-tag",
        "key-description",
    ],
)
def test_a_drifted_bootstrap_copy_fails_the_contract(
    original: str, mutated: str, tmp_path: Path
) -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert text.count(original) == 1, f"control anchor {original!r} not found once"
    copy = tmp_path / "bootstrap_state_backend.sh"
    copy.write_text(text.replace(original, mutated), encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_contract(parse_bootstrap(copy.read_text(encoding="utf-8"), ACCOUNT, REGION))


def test_an_unmutated_copy_passes_the_contract(tmp_path: Path) -> None:
    copy = tmp_path / "bootstrap_state_backend.sh"
    copy.write_text(BOOTSTRAP.read_text(encoding="utf-8"), encoding="utf-8")
    _assert_contract(parse_bootstrap(copy.read_text(encoding="utf-8"), ACCOUNT, REGION))
