# SPDX-License-Identifier: MIT
"""A full stub sweep against DynamoDB Local, re-priced by `eval.reprice`, reproduces its matrix.

Also pins the pre-aggregation contract between the harness writer and the re-price reader: the
keys the writer adds to each archived call are exactly the keys the reader prices from.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

from agents.tier_attribution import InvocationAttribution
from crm.store.store import Store
from eval.harness import DEFAULT_ARM_SPECS, run_sweep
from eval.reprice import ARCHIVED_PRICING_KEYS, PRE_AGGREGATION_DIRNAME, main

_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+00:00")


def _without_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "<ts>" if k == "timestamp" else _without_timestamps(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


def _sweep(store: Store, out: Path) -> None:
    run_sweep(store=store, mode="stub", output_dir=out, archive_raw=True)


def test_a_repriced_stub_sweep_reproduces_its_matrix_except_timestamp(
    dynamodb_local_store: tuple[Store, str], tmp_path: Path
) -> None:
    store, _prefix = dynamodb_local_store
    sweep = tmp_path / "sweep"
    _sweep(store, sweep)
    out = tmp_path / "repriced"

    assert main(["--raw-dir", str(sweep / "raw"), "--output-dir", str(out)]) == 0

    original = json.loads((sweep / "matrix.json").read_text(encoding="utf-8"))
    repriced = json.loads((out / "matrix.json").read_text(encoding="utf-8"))
    assert len(original["arms"]) == len(DEFAULT_ARM_SPECS)
    assert _without_timestamps(repriced) == _without_timestamps(original)
    for name in ("matrix.md", "matrix.csv"):
        assert _ISO_TIMESTAMP.sub("<ts>", (out / name).read_text(encoding="utf-8")) == (
            _ISO_TIMESTAMP.sub("<ts>", (sweep / name).read_text(encoding="utf-8"))
        ), name


def test_a_profile_less_archive_reprices_identically_under_the_asserted_global_prefix(
    dynamodb_local_store: tuple[Store, str], tmp_path: Path
) -> None:
    store, _prefix = dynamodb_local_store
    sweep = tmp_path / "sweep"
    _sweep(store, sweep)
    for path in (sweep / "raw" / PRE_AGGREGATION_DIRNAME).glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["invocation_attributions"] = [
            {k: v for k, v in entry.items() if k not in ARCHIVED_PRICING_KEYS}
            for entry in payload["invocation_attributions"]
        ]
        path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "repriced"
    argv = ["--raw-dir", str(sweep / "raw"), "--output-dir", str(out)]

    assert main(argv) == 2
    assert main([*argv, "--assume-inference-profile-prefix", "global."]) == 0
    original = json.loads((sweep / "matrix.json").read_text(encoding="utf-8"))
    repriced = json.loads((out / "matrix.json").read_text(encoding="utf-8"))
    assert _without_timestamps(repriced) == _without_timestamps(original)


def test_the_writer_adds_exactly_the_keys_the_reader_prices_from(
    dynamodb_local_store: tuple[Store, str], tmp_path: Path
) -> None:
    store, _prefix = dynamodb_local_store
    sweep = tmp_path / "sweep"
    _sweep(store, sweep)
    attribution_keys = {f.name for f in dataclasses.fields(InvocationAttribution)}
    paths = sorted((sweep / "raw" / PRE_AGGREGATION_DIRNAME).glob("*.json"))
    assert len(paths) == len(DEFAULT_ARM_SPECS)
    for path in paths:
        entries = json.loads(path.read_text(encoding="utf-8"))["invocation_attributions"]
        assert entries, path
        for entry in entries:
            assert set(entry) - attribution_keys == set(ARCHIVED_PRICING_KEYS)
