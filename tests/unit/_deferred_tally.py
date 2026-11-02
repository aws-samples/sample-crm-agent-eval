# SPDX-License-Identifier: MIT
"""The plan-security gate's pinned per-control structural deferred tally, shared by its two readers.

`tests/unit/test_plan_security_baseline.py` asserts it against `run_gate` in-process, and
`tests/unit/test_verify_gate_aggregation.py` asserts it against the gate's `--json` document.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

#: Deferred findings per control on the compliant fixture, excluding the fixture-dependent ones
#: (`plan_freshness`, `resource_existence_counts`) that each reader derives itself.
EXPECTED_DEFERRED_BY_CONTROL: Final[Mapping[str, int]] = MappingProxyType(
    {
        # `modules/eval_artifacts`' logging destination is a module input, so the link to its
        # destination bucket is deferred while the destination's grant is asserted.
        "s3_access_log_delivery_grant": 1,
    }
)
