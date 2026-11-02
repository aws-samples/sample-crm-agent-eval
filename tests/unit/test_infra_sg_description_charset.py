# SPDX-License-Identifier: MIT
"""Guard: every security-group (rule) `description` in infra/**/*.tf uses only the character
set AWS accepts for SG and SG-rule descriptions.

AWS rejects a description containing a character outside `a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*` at apply
time, which `terraform validate` does not detect. Only the three SG resource types are checked;
`variable`/`output`/other-resource descriptions are free text.
"""

from __future__ import annotations

import pathlib
import re

# The exact set AWS allows in a security-group or SG-rule description.
_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-:/()#,@[]+=&;{}!$*"
)

_SG_RESOURCE_TYPES = {
    "aws_security_group",
    "aws_vpc_security_group_ingress_rule",
    "aws_vpc_security_group_egress_rule",
}

_INFRA_DIR = pathlib.Path(__file__).resolve().parents[2] / "infra"

_RESOURCE_RE = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"\s*\{')
_DESCRIPTION_RE = re.compile(r'^\s*description\s*=\s*"((?:[^"\\]|\\.)*)"')


def _security_group_descriptions() -> list[tuple[pathlib.Path, int, str, str]]:
    """Return (file, line_no, resource_type, description) for every SG-type resource block.

    A single-pass, brace-depth block tracker: it enters a block on a top-level `resource "..."`
    line and captures the `description` argument while the block is open.
    """
    found: list[tuple[pathlib.Path, int, str, str]] = []
    for tf in sorted(_INFRA_DIR.rglob("*.tf")):
        depth = 0
        current_type: str | None = None
        for line_no, line in enumerate(tf.read_text().splitlines(), start=1):
            resource_match = _RESOURCE_RE.match(line)
            if resource_match and depth == 0:
                current_type = resource_match.group(1)
                depth = 1
                continue
            if current_type is not None:
                depth += line.count("{") - line.count("}")
                description_match = _DESCRIPTION_RE.match(line)
                if description_match and current_type in _SG_RESOURCE_TYPES:
                    found.append((tf, line_no, current_type, description_match.group(1)))
                if depth <= 0:
                    current_type = None
    return found


def _disallowed(description: str) -> list[str]:
    # Unescape the two HCL escapes so a literal `\"` is reported as `"`.
    text = description.replace('\\"', '"').replace("\\\\", "\\")
    return sorted({c for c in text if c not in _ALLOWED_CHARS})


def test_infra_has_security_group_descriptions() -> None:
    # Non-vacuity: if the parse finds nothing, the charset assertion below is meaningless.
    descriptions = _security_group_descriptions()
    assert descriptions, "found no security-group rule descriptions in infra/**/*.tf to check"


def test_every_security_group_description_uses_only_aws_allowed_characters() -> None:
    offenders: list[str] = []
    for tf, line_no, resource_type, description in _security_group_descriptions():
        bad = _disallowed(description)
        if bad:
            rel = tf.relative_to(_INFRA_DIR.parent)
            offenders.append(
                f"{rel}:{line_no} [{resource_type}] uses disallowed {bad!r} in {description!r}"
            )
    assert not offenders, (
        "security-group descriptions must use only a-zA-Z0-9 and ` ._-:/()#,@[]+=&;{}!$*` "
        "(AWS rejects anything else at apply time):\n" + "\n".join(offenders)
    )
