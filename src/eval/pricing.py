# SPDX-License-Identifier: MIT
"""Eval-side pricing lookups: per-model cache thresholds and the provisional-cost flag.

Token cost itself is computed by :func:`crm.metering.cost_for_usage`. This module adds:

1. Per-model prompt-cache minimum checkpoint sizes, read from ``config/model_capabilities.yaml``.
2. :func:`is_cost_provisional`: while ``config/pricing.yaml`` carries ``provisional: true``,
   absolute currency figures are withheld from published eval output.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEFAULT_PRICING_PATH: Final[Path] = _REPO_ROOT / "config" / "pricing.yaml"
_DEFAULT_CAPABILITIES_PATH: Final[Path] = _REPO_ROOT / "config" / "model_capabilities.yaml"


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Parse a YAML file into a mapping, raising if the top level is not a mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


@dataclass(frozen=True, slots=True)
class PriceTableStatus:
    """Whether the price table is safe to publish absolute currency from.

    Attributes:
        price_table_version: The table's version string, carried into every result record's
            provenance.
        provisional: Whether the table is marked provisional. While ``True``, absolute currency
            figures are withheld from published output.
    """

    price_table_version: str
    provisional: bool


def load_price_table_status(*, pricing_path: Path = _DEFAULT_PRICING_PATH) -> PriceTableStatus:
    """Read the price table's version and provisional flag, without touching its rates.

    Args:
        pricing_path: Path to ``config/pricing.yaml``. Overridable for tests.

    Returns:
        The table's version and provisional status.
    """
    pricing = _load_yaml_mapping(pricing_path)
    return PriceTableStatus(
        price_table_version=str(pricing["price_table_version"]),
        provisional=bool(pricing["provisional"]),
    )


def is_cost_provisional(*, pricing_path: Path = _DEFAULT_PRICING_PATH) -> bool:
    """Return whether absolute currency figures must be withheld from published output.

    Args:
        pricing_path: Path to ``config/pricing.yaml``. Overridable for tests.

    Returns:
        ``True`` while the price table is marked provisional.
    """
    return load_price_table_status(pricing_path=pricing_path).provisional


def cache_min_checkpoint_tokens(
    model_id: str, *, capabilities_path: Path = _DEFAULT_CAPABILITIES_PATH
) -> int | None:
    """Return ``model_id``'s prompt-cache minimum checkpoint size in tokens.

    Read from ``config/model_capabilities.yaml``.

    Args:
        model_id: The Bedrock model identifier to look up.
        capabilities_path: Path to ``config/model_capabilities.yaml``. Overridable for tests.

    Returns:
        The minimum checkpoint size in tokens, or ``None`` when the model documents no threshold.

    Raises:
        KeyError: When ``model_id`` has no entry in the capability matrix.
    """
    capabilities = _load_yaml_mapping(capabilities_path)
    models = capabilities["models"]
    if model_id not in models:
        raise KeyError(f"{model_id} has no entry in {capabilities_path}")
    threshold = models[model_id].get("prompt_cache_min_checkpoint_tokens")
    return int(threshold) if threshold is not None else None
