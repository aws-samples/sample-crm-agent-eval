#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reader and validator for `infra/regions.json`, the supported-region map.

Library use (`from scripts import region_map`): `load()` returns a validated `RegionMap`, and
`RegionMap.entry(region)` returns that region's `RegionEntry` or raises `UnsupportedRegion`.
`load` raises `RegionMapError` for an unreadable or invalid map. Stdlib only.

Usage:
    region_map.py check [--map P]
    region_map.py az-ids --region R [--field agentcore|private|runtime|nat] [--map P]
    region_map.py regions [--map P]

Exit codes: 0 ok; 1 invalid or unreadable map (reason on stderr); 2 region not US or not in the
map (stderr names the map and lists the supported regions); 64 usage error.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
MAP_PATH: Final[Path] = REPO_ROOT / "infra" / "regions.json"
MAP_DISPLAY_NAME: Final[str] = "infra/regions.json"
US_REGION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^us-(east|west)-[1-9]$")
SCHEMA_VERSION: Final[int] = 1
DOCS_URL_PREFIX: Final[str] = "https://docs.aws.amazon.com/"
REQUIRED_REGION: Final[str] = "us-east-1"
USAGE_EXIT_STATUS: Final[int] = 64

_TOP_KEYS: Final[frozenset[str]] = frozenset({"schema_version", "source", "regions"})
_SOURCE_KEYS: Final[frozenset[str]] = frozenset(
    {"agentcore_vpc_zones_url", "agentcore_regions_url", "checked"}
)
_REGION_KEYS: Final[frozenset[str]] = frozenset({"agentcore_az_ids", "bootstrap"})
_BOOTSTRAP_KEYS: Final[frozenset[str]] = frozenset(
    {"private_az_ids", "runtime_az_ids", "nat_az_id"}
)
_ISO_DATE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_FIELDS: Final[tuple[str, ...]] = ("agentcore", "private", "runtime", "nat")


class RegionMapError(ValueError):
    """The region map is unreadable or breaks a validation rule."""


class UnsupportedRegion(RegionMapError):  # noqa: N818
    """The region is not a US commercial region, or is not in the map."""


@dataclass(frozen=True)
class RegionEntry:
    """One region's AgentCore VPC zone ids and its bootstrap placement defaults."""

    name: str
    agentcore_az_ids: tuple[str, ...]
    private_az_ids: tuple[str, ...]
    runtime_az_ids: tuple[str, ...]
    nat_az_id: str


@dataclass(frozen=True)
class RegionMap:
    """A validated region map; `regions` is keyed by region name."""

    regions: Mapping[str, RegionEntry]
    checked: datetime.date
    source_urls: tuple[str, str]

    def entry(self, region: str) -> RegionEntry:
        """Return the entry for `region`; raise `UnsupportedRegion` if it is not in the map."""
        found = self.regions.get(region)
        if found is None:
            kind = "not in" if US_REGION_PATTERN.fullmatch(region) else "not a US region and not in"
            raise UnsupportedRegion(
                f"region {region!r} is {kind} {MAP_DISPLAY_NAME}; "
                f"supported regions: {', '.join(sorted(self.regions))}"
            )
        return found


def az_prefix(region: str) -> str:
    """Return the AZ-id prefix of a US region (`us-west-2` -> `usw2`); raise `UnsupportedRegion`."""
    match = US_REGION_PATTERN.fullmatch(region)
    if match is None:
        raise UnsupportedRegion(
            f"region {region!r} does not match {US_REGION_PATTERN.pattern} ({MAP_DISPLAY_NAME})"
        )
    return f"us{match.group(1)[0]}{region[-1]}"


def _refuse_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegionMapError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _object(value: Any, where: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegionMapError(f"{where}: expected an object")
    unknown = sorted(set(value) - keys)
    if unknown:
        raise RegionMapError(f"{where}: unknown key(s) {unknown}")
    missing = sorted(keys - set(value))
    if missing:
        raise RegionMapError(f"{where}: missing key(s) {missing}")
    return value


def _zone_id(value: Any, where: str, zone_pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not zone_pattern.fullmatch(value):
        raise RegionMapError(f"{where}: {value!r} does not match {zone_pattern.pattern}")
    return value


def _zone_list(value: Any, where: str, zone_pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RegionMapError(f"{where}: expected a list of zone ids")
    zones = tuple(_zone_id(item, where, zone_pattern) for item in value)
    if len(zones) < 2:
        raise RegionMapError(f"{where}: needs at least 2 zone ids, got {len(zones)}")
    if len(set(zones)) != len(zones):
        raise RegionMapError(f"{where}: zone ids are not distinct")
    return zones


def _subset(zones: Sequence[str], allowed: Sequence[str], where: str) -> None:
    outside = sorted(set(zones) - set(allowed))
    if outside:
        raise RegionMapError(f"{where}: {outside} not in agentcore_az_ids")


def _region_entry(name: str, value: Any) -> RegionEntry:
    if not US_REGION_PATTERN.fullmatch(name):
        raise RegionMapError(f"regions: key {name!r} does not match {US_REGION_PATTERN.pattern}")
    zone_pattern = re.compile(rf"^{az_prefix(name)}-az[0-9]+$")
    body = _object(value, f"regions.{name}", _REGION_KEYS)
    agentcore = _zone_list(
        body["agentcore_az_ids"], f"regions.{name}.agentcore_az_ids", zone_pattern
    )
    bootstrap = _object(body["bootstrap"], f"regions.{name}.bootstrap", _BOOTSTRAP_KEYS)
    where = f"regions.{name}.bootstrap"
    private = _zone_list(bootstrap["private_az_ids"], f"{where}.private_az_ids", zone_pattern)
    runtime = _zone_list(bootstrap["runtime_az_ids"], f"{where}.runtime_az_ids", zone_pattern)
    nat = _zone_id(bootstrap["nat_az_id"], f"{where}.nat_az_id", zone_pattern)
    _subset(private, agentcore, f"{where}.private_az_ids")
    _subset(runtime, agentcore, f"{where}.runtime_az_ids")
    _subset((nat,), agentcore, f"{where}.nat_az_id")
    return RegionEntry(name, agentcore, private, runtime, nat)


def _checked_date(value: Any) -> datetime.date:
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise RegionMapError(f"source.checked: {value!r} is not an ISO date (YYYY-MM-DD)")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise RegionMapError(f"source.checked: {value!r} is not a valid date") from exc


def _docs_url(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.startswith(DOCS_URL_PREFIX):
        raise RegionMapError(f"{where}: {value!r} does not start with {DOCS_URL_PREFIX}")
    return value


def _parse(document: Any) -> RegionMap:
    top = _object(document, "map", _TOP_KEYS)
    version = top["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise RegionMapError(f"schema_version: expected {SCHEMA_VERSION}, got {version!r}")
    source = _object(top["source"], "source", _SOURCE_KEYS)
    urls = (
        _docs_url(source["agentcore_vpc_zones_url"], "source.agentcore_vpc_zones_url"),
        _docs_url(source["agentcore_regions_url"], "source.agentcore_regions_url"),
    )
    checked = _checked_date(source["checked"])
    regions = top["regions"]
    if not isinstance(regions, dict):
        raise RegionMapError("regions: expected an object")
    entries = {name: _region_entry(name, value) for name, value in regions.items()}
    if REQUIRED_REGION not in entries:
        raise RegionMapError(f"regions: {REQUIRED_REGION} must be present")
    return RegionMap(types.MappingProxyType(entries), checked, urls)


def load(path: Path | None = None) -> RegionMap:
    """Read and validate the map at `path` (default `MAP_PATH`); raise `RegionMapError`."""
    target = MAP_PATH if path is None else path
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegionMapError(f"cannot read {target}: {exc}") from exc
    try:
        document = json.loads(text, object_pairs_hook=_refuse_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise RegionMapError(f"{target}: not valid JSON: {exc}") from exc
    try:
        return _parse(document)
    except RegionMapError as exc:
        raise RegionMapError(f"{target}: {exc}") from exc


def _field_value(entry: RegionEntry, field: str) -> tuple[str, ...]:
    if field == "agentcore":
        return entry.agentcore_az_ids
    if field == "private":
        return entry.private_az_ids
    if field == "runtime":
        return entry.runtime_az_ids
    return (entry.nat_az_id,)


class _UsageExitParser(argparse.ArgumentParser):
    """An `ArgumentParser` whose usage errors exit `USAGE_EXIT_STATUS`, keeping 2 for a region."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(USAGE_EXIT_STATUS, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _UsageExitParser(description="Read and validate infra/regions.json.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "az-ids", "regions"):
        command = commands.add_parser(name)
        command.add_argument(
            "--map", type=Path, default=None, help=f"map path (default: {MAP_DISPLAY_NAME})"
        )
        if name == "az-ids":
            command.add_argument("--region", required=True)
            command.add_argument("--field", choices=_FIELDS, default="agentcore")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI; return the exit status."""
    args = _parser().parse_args(argv)
    try:
        region_map = load(args.map)
    except RegionMapError as exc:
        print(f"region_map: invalid map: {exc}", file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"region map OK: {len(region_map.regions)} regions (checked {region_map.checked})")
    elif args.command == "regions":
        print("\n".join(sorted(region_map.regions)))
    else:
        try:
            entry = region_map.entry(args.region)
        except UnsupportedRegion as exc:
            print(f"region_map: {exc}", file=sys.stderr)
            return 2
        print(" ".join(_field_value(entry, args.field)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
