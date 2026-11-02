# SPDX-License-Identifier: MIT
"""Offline assertions on the root's reserved-concurrency headroom guard's preconditions.

`terraform_data.reserved_concurrency_headroom_guard` asserts that `local.reserved_concurrency`'s keys
are exactly `local.tool_names` plus `local.non_tool_functions`, with no name in both, so a `merge`
collision cannot drop a claim silently. No count is pinned: `local.non_tool_functions` is the
key set of `local.non_tool_reserved_concurrency`. Every shape and the ceiling are parsed from
`infra/main.tf`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_ROOT_TF: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "main.tf"

#: The headroom precondition, capturing its ceiling on `local.reserved_concurrency_total`.
_HEADROOM_CONDITION: Final[re.Pattern[str]] = re.compile(
    r"condition\s*=\s*local\.reserved_concurrency_total\s*<=\s*(?P<ceiling>\d+)"
)

#: The key-set precondition: disjoint tool and non-tool names, and their union is the map's key set.
_KEY_SET_CONDITION: Final[re.Pattern[str]] = re.compile(
    r"length\(setintersection\(toset\(local\.tool_names\),\s*toset\(local\.non_tool_functions\)\)\)"
    r"\s*==\s*0\s*&&\s*toset\(keys\(local\.reserved_concurrency\)\)\s*==\s*"
    r"setunion\(toset\(local\.tool_names\),\s*toset\(local\.non_tool_functions\)\)"
)


def _source() -> str:
    text = _ROOT_TF.read_text(encoding="utf-8")
    assert "reserved_concurrency_headroom_guard" in text, (
        f"the headroom guard is not in {_ROOT_TF}. Failing rather than skipping: every assertion here "
        "is about that guard, so its absence makes this file green over nothing."
    )
    return text


def _tool_names(text: str) -> list[str]:
    """`local.tool_names`'s members, read from `local.tools`' own keys."""
    block = re.search(r"^  tools = \{$(?P<body>.*?)^  \}$", text, re.DOTALL | re.MULTILINE)
    assert block is not None, "local.tools was not found, so the tool count cannot be derived"
    names = re.findall(r"^    (\w+)\s*=\s*\{", block.group("body"), re.MULTILINE)
    assert names, "parsed no tool names from local.tools"
    return sorted(names)


def _non_tool_claims(text: str) -> dict[str, int]:
    """The literal entries of `local.non_tool_reserved_concurrency`."""
    block = re.search(
        r"^  non_tool_reserved_concurrency = \{(?P<body>.*?)\n  \}$",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert block is not None, (
        "`local.non_tool_reserved_concurrency` was not found, so the non-tool claims cannot be derived"
    )
    claims = {
        name: int(value)
        for name, value in re.findall(
            r"^\s*(\w+)\s*=\s*(\d+)\s*$", block.group("body"), re.MULTILINE
        )
    }
    assert claims, "parsed no non-tool concurrency claims from the merge"
    return claims


def test_the_guard_checks_the_key_set_rather_than_a_pinned_count() -> None:
    """The guard compares key sets built from the two named locals; no `+ N` literal remains."""
    text = _source()
    assert _KEY_SET_CONDITION.search(text) is not None, (
        "the guard's key-set precondition is not in the shape this file reads. If the guard was "
        "reshaped, port this check to it rather than deleting it."
    )
    assert re.search(r"length\(local\.tool_names\)\s*\+\s*\d+", text) is None, (
        "a pinned non-tool count is back in infra/main.tf"
    )


def test_the_non_tool_function_list_is_derived_from_the_claims() -> None:
    """`local.non_tool_functions` is the key set of the claim map, and the merge uses that map."""
    text = _source()
    assert re.search(
        r"non_tool_functions\s*=\s*sort\(keys\(local\.non_tool_reserved_concurrency\)\)", text
    ), "local.non_tool_functions is not derived from local.non_tool_reserved_concurrency"
    assert re.search(
        r"reserved_concurrency\s*=\s*merge\(\s*local\.tool_reserved_concurrency,\s*"
        r"local\.non_tool_reserved_concurrency,\s*\)",
        text,
    ), "local.reserved_concurrency is not the merge of the tool tier and the non-tool map"
    assert _non_tool_claims(text)


def test_the_guards_error_message_names_the_non_tool_functions_by_interpolation() -> None:
    """The error message lists the non-tool names from the local, so it cannot go stale."""
    text = _source()
    message = re.search(
        r"^\s*error_message = \"local\.reserved_concurrency keys must be(?P<line>.*)$",
        text,
        re.MULTILINE,
    )
    assert message is not None, "the key-set precondition's error message was not found"
    assert '${join(", ", local.non_tool_functions)}' in message.group("line")


def test_no_non_tool_claim_collides_with_a_tool_name() -> None:
    """No non-tool concurrency claim shares a key with a tool name, since `merge` would override it."""
    text = _source()
    collisions = sorted(set(_non_tool_claims(text)) & set(_tool_names(text)))
    assert not collisions, (
        f"non-tool concurrency claim(s) collide with tool name(s): {collisions}. `merge` would drop "
        "one silently."
    )


def test_the_reserved_concurrency_total_leaves_the_declared_headroom() -> None:
    """The total reserved concurrency derived from source is within the guard's parsed ceiling."""
    text = _source()
    ceiling_match = _HEADROOM_CONDITION.search(text)
    assert ceiling_match is not None, "the headroom precondition was not found"
    ceiling = int(ceiling_match.group("ceiling"))

    tool_block = re.search(r"tool_reserved_concurrency = \{(?P<body>.*?)\n  \}", text, re.DOTALL)
    assert tool_block is not None, "local.tool_reserved_concurrency was not found"
    high_list = re.search(r"contains\(\[(?P<names>[^\]]*)\]", tool_block.group("body"))
    values = re.search(r"\?\s*(?P<high>\d+)\s*:\s*(?P<low>\d+)", tool_block.group("body"))
    assert high_list is not None and values is not None, (
        "the per-tool concurrency expression is not in the shape this file reads"
    )
    high_names = set(re.findall(r'"(\w+)"', high_list.group("names")))
    high, low = int(values.group("high")), int(values.group("low"))

    tool_names = _tool_names(text)
    assert high_names <= set(tool_names), (
        f"the high-concurrency list names {sorted(high_names - set(tool_names))}, which are not tools"
    )
    total = sum(high if name in high_names else low for name in tool_names)
    total += sum(_non_tool_claims(text).values())

    assert total <= ceiling, (
        f"reserved concurrency claims {total} against a ceiling of {ceiling}; the account's default "
        "pool is 1,000 and the guard exists to keep 100 in reserve"
    )
