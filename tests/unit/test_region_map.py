# SPDX-License-Identifier: MIT
"""Tests for `scripts/region_map.py` and the committed `infra/regions.json`.

Every validation rule has a passing cell and a refusing cell built from one valid document, so a
rule that stops checking turns its refusing cell red. The CLI is run as a child process, as the
shell consumers run it.
"""

from __future__ import annotations

import copy
import datetime
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from scripts import proc_run, region_map

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "region_map.py"

VALID_DOCUMENT: dict[str, Any] = {
    "schema_version": 1,
    "source": {
        "agentcore_vpc_zones_url": "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html",
        "agentcore_regions_url": "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html",
        "checked": "2026-10-22",
    },
    "regions": {
        "us-east-1": {
            "agentcore_az_ids": ["use1-az1", "use1-az2", "use1-az4"],
            "bootstrap": {
                "private_az_ids": ["use1-az1", "use1-az2"],
                "runtime_az_ids": ["use1-az1", "use1-az2"],
                "nat_az_id": "use1-az1",
            },
        },
        "us-west-2": {
            "agentcore_az_ids": ["usw2-az1", "usw2-az2", "usw2-az3"],
            "bootstrap": {
                "private_az_ids": ["usw2-az1", "usw2-az2"],
                "runtime_az_ids": ["usw2-az1", "usw2-az2"],
                "nat_az_id": "usw2-az1",
            },
        },
    },
}

Mutation = Callable[[dict[str, Any]], None]


def _write(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "regions.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _mutated(mutate: Mutation) -> dict[str, Any]:
    document = copy.deepcopy(VALID_DOCUMENT)
    mutate(document)
    return document


def _run_cli(*args: str) -> Any:
    return proc_run.run(
        [sys.executable, str(SCRIPT_PATH), *args], capture_output=True, text=True, timeout=30
    )


def _set(path: tuple[str, ...], value: Any) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        node = document
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


def _delete(path: tuple[str, ...]) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        node = document
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return mutate


def _add_region(name: str, prefix: str) -> Mutation:
    zones = [f"{prefix}-az1", f"{prefix}-az2"]
    entry = {
        "agentcore_az_ids": zones,
        "bootstrap": {"private_az_ids": zones, "runtime_az_ids": zones, "nat_az_id": zones[0]},
    }
    return _set(("regions", name), entry)


def _unchanged(document: dict[str, Any]) -> None:
    """The valid document as is."""


USE1 = ("regions", "us-east-1")
USE1_BOOT = (*USE1, "bootstrap")

# (rule id, valid variant, invalid variant, expected message fragment)
RULE_CELLS: list[tuple[str, Mutation, Mutation, str]] = [
    (
        "schema_version",
        _set(("schema_version",), 1),
        _set(("schema_version",), 2),
        "schema_version",
    ),
    (
        "schema_version_bool",
        _set(("schema_version",), 1),
        _set(("schema_version",), True),
        "schema_version",
    ),
    ("unknown_top_key", _unchanged, _set(("extra",), 1), "unknown key"),
    ("unknown_source_key", _unchanged, _set(("source", "extra"), 1), "unknown key"),
    ("unknown_region_key", _unchanged, _set((*USE1, "extra"), 1), "unknown key"),
    ("unknown_bootstrap_key", _unchanged, _set((*USE1_BOOT, "extra"), 1), "unknown key"),
    ("missing_bootstrap_key", _unchanged, _delete((*USE1_BOOT, "nat_az_id")), "missing key"),
    (
        "checked_date",
        _set(("source", "checked"), "2027-01-31"),
        _set(("source", "checked"), "2026-13-01"),
        "source.checked",
    ),
    (
        "checked_basic_format",
        _set(("source", "checked"), "2026-10-22"),
        _set(("source", "checked"), "20261022"),
        "ISO date",
    ),
    (
        "docs_url",
        _unchanged,
        _set(("source", "agentcore_vpc_zones_url"), "https://example.com/agentcore-vpc.html"),
        "docs.aws.amazon.com",
    ),
    (
        "docs_url_http",
        _unchanged,
        _set(("source", "agentcore_regions_url"), "http://docs.aws.amazon.com/x"),
        "docs.aws.amazon.com",
    ),
    (
        "non_us_key",
        _add_region("us-east-2", "use2"),
        _add_region("eu-west-1", "euw1"),
        "does not match",
    ),
    (
        "govcloud_key",
        _add_region("us-west-1", "usw1"),
        _add_region("us-gov-west-1", "usgw1"),
        "does not match",
    ),
    (
        "us_east_1_present",
        _delete(("regions", "us-west-2")),
        _delete(USE1),
        "us-east-1 must be present",
    ),
    (
        "one_zone_region",
        _set((*USE1, "agentcore_az_ids"), ["use1-az1", "use1-az2"]),
        _set((*USE1, "agentcore_az_ids"), ["use1-az1"]),
        "at least 2",
    ),
    (
        "distinct_zones",
        _unchanged,
        _set((*USE1, "agentcore_az_ids"), ["use1-az1", "use1-az1", "use1-az2"]),
        "not distinct",
    ),
    (
        "foreign_prefix_zone",
        _unchanged,
        _set((*USE1, "agentcore_az_ids"), ["use1-az1", "use1-az2", "usw2-az1"]),
        "does not match",
    ),
    (
        "bootstrap_private_subset",
        _set((*USE1_BOOT, "private_az_ids"), ["use1-az2", "use1-az4"]),
        _set((*USE1_BOOT, "private_az_ids"), ["use1-az1", "use1-az3"]),
        "not in agentcore_az_ids",
    ),
    (
        "bootstrap_runtime_subset",
        _set((*USE1_BOOT, "runtime_az_ids"), ["use1-az1", "use1-az4"]),
        _set((*USE1_BOOT, "runtime_az_ids"), ["use1-az1", "use1-az5"]),
        "not in agentcore_az_ids",
    ),
    (
        "bootstrap_private_two",
        _unchanged,
        _set((*USE1_BOOT, "private_az_ids"), ["use1-az1"]),
        "at least 2",
    ),
    (
        "bootstrap_nat_member",
        _set((*USE1_BOOT, "nat_az_id"), "use1-az4"),
        _set((*USE1_BOOT, "nat_az_id"), "use1-az3"),
        "not in agentcore_az_ids",
    ),
    (
        "zone_list_type",
        _unchanged,
        _set((*USE1, "agentcore_az_ids"), "use1-az1"),
        "expected a list",
    ),
    (
        "zone_trailing_newline",
        _unchanged,
        _set((*USE1, "agentcore_az_ids"), ["use1-az1", "use1-az2", "use1-az4\n"]),
        "does not match",
    ),
    (
        "checked_trailing_newline",
        _unchanged,
        _set(("source", "checked"), "2026-10-22\n"),
        "is not an ISO date",
    ),
    ("region_key_trailing_newline", _unchanged, _add_region("us-east-2\n", "use2"), "regions: key"),
]


@pytest.mark.parametrize(
    ("valid", "invalid", "fragment"),
    [cell[1:] for cell in RULE_CELLS],
    ids=[cell[0] for cell in RULE_CELLS],
)
def test_each_rule_accepts_its_valid_variant_and_refuses_its_invalid_one(
    tmp_path: Path, valid: Mutation, invalid: Mutation, fragment: str
) -> None:
    region_map.load(_write(tmp_path, _mutated(valid)))
    with pytest.raises(region_map.RegionMapError, match=fragment):
        region_map.load(_write(tmp_path, _mutated(invalid)))


def test_duplicate_json_key_is_refused(tmp_path: Path) -> None:
    text = json.dumps(VALID_DOCUMENT).replace(
        '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
    )
    path = tmp_path / "regions.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(region_map.RegionMapError, match="duplicate key"):
        region_map.load(path)


def test_unreadable_and_malformed_maps_are_refused(tmp_path: Path) -> None:
    with pytest.raises(region_map.RegionMapError, match="cannot read"):
        region_map.load(tmp_path / "absent.json")
    path = tmp_path / "regions.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(region_map.RegionMapError, match="not valid JSON"):
        region_map.load(path)
    with pytest.raises(region_map.RegionMapError, match="expected an object"):
        region_map.load(_write(tmp_path, []))


def test_load_reads_the_module_map_path_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic = _mutated(_delete(("regions", "us-west-2")))
    monkeypatch.setattr(region_map, "MAP_PATH", _write(tmp_path, synthetic))
    assert sorted(region_map.load().regions) == ["us-east-1"]


def test_loaded_entries_carry_every_field(tmp_path: Path) -> None:
    loaded = region_map.load(_write(tmp_path, VALID_DOCUMENT))
    assert loaded.checked == datetime.date(2026, 10, 22)
    assert loaded.source_urls == (
        VALID_DOCUMENT["source"]["agentcore_vpc_zones_url"],
        VALID_DOCUMENT["source"]["agentcore_regions_url"],
    )
    assert loaded.entry("us-west-2") == region_map.RegionEntry(
        name="us-west-2",
        agentcore_az_ids=("usw2-az1", "usw2-az2", "usw2-az3"),
        private_az_ids=("usw2-az1", "usw2-az2"),
        runtime_az_ids=("usw2-az1", "usw2-az2"),
        nat_az_id="usw2-az1",
    )


@pytest.mark.parametrize("region", ["eu-west-1", "us-gov-west-1", "us-west-3", ""])
def test_entry_refuses_a_region_outside_the_map_naming_it(tmp_path: Path, region: str) -> None:
    loaded = region_map.load(_write(tmp_path, VALID_DOCUMENT))
    with pytest.raises(region_map.UnsupportedRegion, match="infra/regions.json") as raised:
        loaded.entry(region)
    assert "us-east-1, us-west-2" in str(raised.value)


@pytest.mark.parametrize(
    ("region", "prefix"),
    [("us-east-1", "use1"), ("us-east-2", "use2"), ("us-west-1", "usw1"), ("us-west-2", "usw2")],
)
def test_az_prefix_for_each_us_region(region: str, prefix: str) -> None:
    assert region_map.az_prefix(region) == prefix


@pytest.mark.parametrize(
    "region", ["eu-west-1", "us-gov-west-1", "us-east-10", "US-EAST-1", "us-east-1\n"]
)
def test_az_prefix_refuses_a_non_us_region(region: str) -> None:
    with pytest.raises(region_map.UnsupportedRegion):
        region_map.az_prefix(region)


def test_committed_map_is_the_c1_contract() -> None:
    loaded = region_map.load()
    assert sorted(loaded.regions) == ["us-east-1", "us-east-2", "us-west-1", "us-west-2"]
    assert loaded.entry("us-east-1") == region_map.RegionEntry(
        "us-east-1",
        ("use1-az1", "use1-az2", "use1-az4"),
        ("use1-az1", "use1-az2"),
        ("use1-az1", "use1-az2"),
        "use1-az1",
    )
    assert loaded.entry("us-east-2").agentcore_az_ids == ("use2-az1", "use2-az2", "use2-az3")
    assert loaded.entry("us-west-1") == region_map.RegionEntry(
        "us-west-1",
        ("usw1-az1", "usw1-az3"),
        ("usw1-az1", "usw1-az3"),
        ("usw1-az1", "usw1-az3"),
        "usw1-az1",
    )
    assert loaded.entry("us-west-2").agentcore_az_ids == ("usw2-az1", "usw2-az2", "usw2-az3")
    assert loaded.checked == datetime.date(2026, 10, 22)


def test_cli_check_reports_the_committed_map() -> None:
    result = _run_cli("check")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "region map OK: 4 regions (checked 2026-10-22)\n"


def test_cli_check_exits_1_on_an_invalid_map(tmp_path: Path) -> None:
    path = _write(tmp_path, _mutated(_delete(USE1)))
    result = _run_cli("check", "--map", str(path))
    assert result.returncode == 1
    assert "us-east-1 must be present" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("region", "field", "expected"),
    [
        ("us-east-1", "agentcore", "use1-az1 use1-az2 use1-az4"),
        ("us-west-1", "agentcore", "usw1-az1 usw1-az3"),
        ("us-west-2", "private", "usw2-az1 usw2-az2"),
        ("us-west-2", "runtime", "usw2-az1 usw2-az2"),
        ("us-east-2", "nat", "use2-az1"),
    ],
)
def test_cli_az_ids_prints_the_requested_field(region: str, field: str, expected: str) -> None:
    result = _run_cli("az-ids", "--region", region, "--field", field)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"


def test_cli_az_ids_defaults_to_the_agentcore_field() -> None:
    result = _run_cli("az-ids", "--region", "us-east-2")
    assert (result.returncode, result.stdout) == (0, "use2-az1 use2-az2 use2-az3\n")


@pytest.mark.parametrize("region", ["eu-west-1", "us-gov-west-1", "us-west-3"])
def test_cli_az_ids_exits_2_for_a_region_outside_the_map(region: str) -> None:
    result = _run_cli("az-ids", "--region", region)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "infra/regions.json" in result.stderr
    assert "us-east-1, us-east-2, us-west-1, us-west-2" in result.stderr


def test_cli_az_ids_exits_1_on_an_invalid_map_even_for_a_mapped_region(tmp_path: Path) -> None:
    path = _write(tmp_path, _mutated(_set((*USE1, "agentcore_az_ids"), ["use1-az1"])))
    result = _run_cli("az-ids", "--region", "us-east-1", "--map", str(path))
    assert result.returncode == 1
    assert result.stdout == ""


def test_entry_reports_a_newline_suffixed_region_as_not_us(tmp_path: Path) -> None:
    loaded = region_map.load(_write(tmp_path, VALID_DOCUMENT))
    with pytest.raises(region_map.UnsupportedRegion, match="not a US region"):
        loaded.entry("us-west-2\n")


@pytest.mark.parametrize(
    "args",
    [
        pytest.param((), id="no-command"),
        pytest.param(("bogus",), id="unknown-command"),
        pytest.param(("az-ids",), id="az-ids-without-region"),
        pytest.param(("az-ids", "--region", "us-east-1", "--field", "bogus"), id="bad-field"),
        pytest.param(("check", "--nope"), id="unknown-flag"),
    ],
)
def test_cli_usage_errors_exit_64_not_the_unsupported_region_code(args: tuple[str, ...]) -> None:
    result = _run_cli(*args)
    assert result.returncode == region_map.USAGE_EXIT_STATUS == 64
    assert "usage:" in result.stderr
    assert result.stdout == ""


def test_cli_regions_lists_one_per_line(tmp_path: Path) -> None:
    assert _run_cli("regions").stdout == "us-east-1\nus-east-2\nus-west-1\nus-west-2\n"
    synthetic = _write(tmp_path, VALID_DOCUMENT)
    assert _run_cli("regions", "--map", str(synthetic)).stdout == "us-east-1\nus-west-2\n"
