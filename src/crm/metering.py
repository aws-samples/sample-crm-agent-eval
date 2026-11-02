# SPDX-License-Identifier: MIT
"""Per-invocation usage metering and redacted structured logging.

Builds a :class:`UsageRecord` per model invocation, carrying correlation identifiers and a
:class:`Cost` priced from ``config/pricing.yaml`` with cache reads and writes kept separate.
:data:`TOKEN_ACCOUNTING` declares per family whether ``input_tokens`` includes cache tokens.
:func:`redact` removes secrets, tokens, and record payloads from a log value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml

Family = Literal["anthropic", "openai"]
Tier = Literal["supervisor", "subagent", "tool"]

#: How a family's reported ``input_tokens`` relates to its cache tokens: ``inclusive`` (cache
#: counts are a subset of it), ``exclusive`` (disjoint), or ``unconfirmed`` (not established).
CacheReadAccounting = Literal["inclusive", "exclusive", "unconfirmed"]


@dataclass(frozen=True, slots=True)
class TokenAccounting:
    """One provider family's declared cached-read accounting, with the evidence behind it.

    Attributes:
        family: The provider family this declaration applies to.
        cache_read_usage_field: Where the adapter reads the cached-read count from on the wire.
        cache_read_accounting: The accounting :func:`cost_for_usage` prices under.
        evidence_direction: Which reading the available evidence points toward; may differ from
            :attr:`cache_read_accounting`.
        evidence: The evidence itself.
    """

    family: Family
    cache_read_usage_field: str
    cache_read_accounting: CacheReadAccounting
    evidence_direction: CacheReadAccounting
    evidence: str

    def to_report_fields(self) -> dict[str, object]:
        """Render this declaration as a flat, JSON-safe mapping in a fixed key order."""
        return {
            "family": self.family,
            "cache_read_usage_field": self.cache_read_usage_field,
            "cache_read_accounting": self.cache_read_accounting,
            "evidence_direction": self.evidence_direction,
            "evidence": self.evidence,
        }


#: Per-family cached-read accounting, declared for every member of :data:`Family`.
TOKEN_ACCOUNTING: Final[dict[Family, TokenAccounting]] = {
    "anthropic": TokenAccounting(
        family="anthropic",
        cache_read_usage_field="usage.cache_read_input_tokens (Messages API message_start)",
        cache_read_accounting="exclusive",
        evidence_direction="exclusive",
        evidence=(
            "Exclusive (disjoint): inputTokens excludes both cacheReadInputTokens and "
            "cacheWriteInputTokens; the total input is their sum. AWS's own Bedrock prompt-caching "
            "walkthrough shows a cache-read response as "
            '{"inputTokens": 28, "outputTokens": 294, "cacheWriteInputTokens": 0, '
            '"cacheReadInputTokens": 1898} and states "Only 28 tokens (the question itself) were '
            'processed as standard input" while the 1,898 reused tokens are reported separately in '
            "cacheReadInputTokens — so the cached count is not inside inputTokens. Source: "
            "https://aws.amazon.com/blogs/machine-learning/optimizing-cost-and-latency-with-amazon-"
            "bedrock-prompt-caching/ (verified 2026-09-28). The Messages API message_start path "
            "the adapter reads carries the same disjoint cache_read_input_tokens / "
            "cache_creation_input_tokens siblings."
        ),
    ),
    "openai": TokenAccounting(
        family="openai",
        cache_read_usage_field=(
            "usage.input_tokens_details.cached_tokens (Responses API response.completed)"
        ),
        cache_read_accounting="inclusive",
        evidence_direction="inclusive",
        evidence=(
            "Inclusive (subset): input_tokens is the full input count and both the cached reads and "
            "the cache writes are part of it, not added on top. AWS's Bedrock explicit-caching "
            "documentation for the OpenAI models states verbatim "
            '"input_tokens = cached_tokens + cache_write_tokens + non-cached remainder" and '
            '"Because the cached and written counts are part of input_tokens rather than added on '
            "top of it, a token served from the cache is counted once and billed once at the "
            'cache-read rate. You are never charged twice for the same token." Its worked example '
            "confirms the arithmetic (input_tokens 3682 = cached 0 + cache_write 3626 + new 56). "
            "Source: https://aws.amazon.com/blogs/machine-learning/introducing-explicit-prompt-"
            "caching-for-openai-gpt-5-6-models-on-amazon-bedrock/ (verified 2026-09-28). So both "
            "cached_tokens and cache_write_tokens must be subtracted from input_tokens to obtain "
            "the tokens billed at the standard input rate. CAVEAT: that AWS source describes "
            "the GPT-5.6 models; the OpenAI models in the matrix use the same Responses API usage "
            "shape (input_tokens with input_tokens_details.cached_tokens), and no vendor statement "
            "of this accounting specific to them was found, so the reading is carried forward by "
            "shape. The first --live sweep that records a non-zero cached read should be "
            "reconciled against the actual Bedrock bill to confirm it holds for them."
        ),
    ),
}

#: Maps a price-table model id's leading segment to its provider family.
_MODEL_ID_FAMILY_PREFIXES: Final[dict[str, Family]] = {
    "anthropic.": "anthropic",
    "openai.": "openai",
}


class UnconfirmedTokenAccountingError(Exception):
    """Raised when a publishable currency figure is requested under unconfirmed accounting.

    Raised by :func:`cost_for_usage` when ``config/pricing.yaml`` is not ``provisional`` and the
    family's accounting is ``unconfirmed``.
    """


def token_accounting_for(family: Family) -> TokenAccounting:
    """Return ``family``'s declared cached-read accounting.

    Raises:
        KeyError: When ``family`` carries no declaration.
    """
    return TOKEN_ACCOUNTING[family]


def family_for_model_id(model_id: str) -> Family:
    """Return the provider family a price-table model id belongs to.

    Args:
        model_id: A model identifier as keyed in ``config/pricing.yaml``.

    Returns:
        The family whose prefix ``model_id`` carries.

    Raises:
        ValueError: When no declared prefix matches.
    """
    for prefix, family in _MODEL_ID_FAMILY_PREFIXES.items():
        if model_id.startswith(prefix):
            return family
    raise ValueError(f"{model_id!r} carries no recognised provider-family prefix")


_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEFAULT_PRICING_PATH: Final[Path] = _REPO_ROOT / "config" / "pricing.yaml"

#: Keys whose values are always replaced by ``[REDACTED]`` in a log value.
_REDACT_VALUE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "authorization",
        "secret",
        "api_key",
        "apikey",
        "password",
        "record",
        "payload",
        "before_snapshot",
        "after_snapshot",
        "after_payload",
        "book",
    }
)

#: Value shapes redacted under any key: a JWT, a long opaque token run, and an AWS access key ID.
_JWT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}$"
)
_OPAQUE_SECRET_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-+/=]{32,}$")
#: An AWS access key ID: ``AKIA`` or ``ASIA`` followed by 16 uppercase letters or digits.
_AWS_ACCESS_KEY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16}$")

_REDACTED: Final[str] = "[REDACTED]"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Raw token counts for one invocation, cache reads and writes kept separate.

    Attributes:
        input_tokens: The input-token count exactly as the provider reported it; see
            :data:`TOKEN_ACCOUNTING` for whether it includes cache tokens.
        output_tokens: Output tokens, including any reasoning or thinking tokens.
        cache_write_tokens: Tokens written to a new prompt-cache checkpoint.
        cache_read_tokens: Tokens served from an existing prompt-cache checkpoint.
    """

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def __post_init__(self) -> None:
        """Reject a negative token count, which cannot arise from a real provider response."""
        for name in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must not be negative, got {value}")


@dataclass(frozen=True, slots=True)
class Cost:
    """Cost broken out by billing category, in the price table's configured currency and unit.

    Attributes:
        input: Cost of the input tokens charged at the input rate, as determined by
            :func:`billable_input_tokens` under the family's declared accounting.
        output: Cost of output tokens, including reasoning tokens billed as output.
        cache_write: Cost of tokens written to a new cache checkpoint.
        cache_read: Cost of tokens served from an existing cache checkpoint.
        price_table_version: The ``price_table_version`` the rates were read from.
        cache_read_accounting: The :class:`TokenAccounting` reading :attr:`input` was computed
            under.
    """

    input: float
    output: float
    cache_write: float
    cache_read: float
    price_table_version: str
    cache_read_accounting: CacheReadAccounting

    @property
    def total(self) -> float:
        """Sum of all four cost categories."""
        return self.input + self.output + self.cache_write + self.cache_read


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One model invocation's attributed usage, cost, and correlation identifiers.

    Attributes:
        correlation_id: Ties this invocation to the chat turn that produced it. Shared across
            every subagent invocation and tool call within one turn.
        turn_id: The chat turn this invocation belongs to.
        invocation_id: Identifier for this specific invocation, unique even within one turn.
        rep_id: The rep the invocation is attributed to, for cost-by-user rollups.
        model_id: The Bedrock model identifier invoked.
        family: Provider family the model belongs to.
        tier: The tier slot the model was invoked for.
        usage: Raw token counts, cache reads and writes kept separate.
        cost: Cost broken out by category, computed from :data:`usage` against the price table.
        cache_min_checkpoint_tokens: The invoked model's prompt-cache minimum checkpoint size in
            tokens, or ``None`` when the model does not document one.
    """

    correlation_id: str
    turn_id: str
    invocation_id: str
    rep_id: str
    model_id: str
    family: Family
    tier: Tier
    usage: TokenUsage
    cost: Cost
    cache_min_checkpoint_tokens: int | None = None

    def __post_init__(self) -> None:
        """Reject a record that cannot be attributed to a turn, invocation, or rep."""
        for name in ("correlation_id", "turn_id", "invocation_id", "rep_id", "model_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")


def _load_pricing(path: Path) -> dict[str, Any]:
    """Parse the price table YAML into a mapping, raising if the top level is not a mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def billable_input_tokens(usage: TokenUsage, family: Family) -> int:
    """Return the token count to charge at the standard input rate for ``family``.

    Under ``inclusive`` accounting the cache-read and cache-write counts are subtracted from
    ``input_tokens``, floored at zero; otherwise ``input_tokens`` is charged whole.
    """
    accounting = token_accounting_for(family).cache_read_accounting
    if accounting == "inclusive":
        return max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
    return usage.input_tokens


def cost_for_usage(
    usage: TokenUsage,
    model_id: str,
    *,
    family: Family | None = None,
    pricing_path: Path = _DEFAULT_PRICING_PATH,
) -> Cost:
    """Compute per-category cost for ``usage`` against the versioned price table.

    Rates are per token category, per 1M tokens. The input count is
    :func:`billable_input_tokens` under the family's :class:`TokenAccounting`.

    Args:
        usage: Token counts to price, cache reads and writes kept separate.
        model_id: The Bedrock model identifier to price against.
        family: The provider family whose accounting applies. Derived from ``model_id`` by
            :func:`family_for_model_id` when omitted.
        pricing_path: Path to ``config/pricing.yaml``. Overridable for tests.

    Returns:
        A :class:`Cost` with the four categories broken out.

    Raises:
        KeyError: When ``model_id`` has no entry in the price table.
        ValueError: When ``model_id`` names no recognised provider family and ``family`` was not
            supplied.
        UnconfirmedTokenAccountingError: When the price table is not provisional and the family's
            accounting is unconfirmed.
    """
    pricing = _load_pricing(pricing_path)
    models = pricing["models"]
    if model_id not in models:
        raise KeyError(f"{model_id} has no entry in {pricing_path}")
    rates = models[model_id]
    unit_tokens = 1_000_000.0  # gitleaks:allow

    resolved_family = family if family is not None else family_for_model_id(model_id)
    accounting = token_accounting_for(resolved_family)
    if accounting.cache_read_accounting == "unconfirmed" and not bool(
        pricing.get("provisional", False)
    ):
        raise UnconfirmedTokenAccountingError(
            f"{pricing_path} is no longer provisional, so its cost figures are publishable, but "
            f"the {resolved_family!r} family's cached-read accounting is still declared "
            f"unconfirmed. Confirm whether its reported input_tokens includes its cached reads and "
            f"set crm.metering.TOKEN_ACCOUNTING accordingly before any currency figure is "
            f"published from it."
        )

    return Cost(
        input=(billable_input_tokens(usage, resolved_family) / unit_tokens) * float(rates["input"]),
        output=(usage.output_tokens / unit_tokens) * float(rates["output"]),
        cache_write=(usage.cache_write_tokens / unit_tokens) * float(rates["cache_write"]),
        cache_read=(usage.cache_read_tokens / unit_tokens) * float(rates["cache_read"]),
        price_table_version=str(pricing["price_table_version"]),
        cache_read_accounting=accounting.cache_read_accounting,
    )


def build_usage_record(
    *,
    correlation_id: str,
    turn_id: str,
    invocation_id: str,
    rep_id: str,
    model_id: str,
    family: Family,
    tier: Tier,
    usage: TokenUsage,
    cache_min_checkpoint_tokens: int | None = None,
    pricing_path: Path = _DEFAULT_PRICING_PATH,
) -> UsageRecord:
    """Construct a :class:`UsageRecord`, pricing ``usage`` against the price table.

    Args:
        correlation_id: Ties this invocation to its chat turn.
        turn_id: The chat turn this invocation belongs to.
        invocation_id: Identifier unique to this invocation.
        rep_id: The rep this invocation is attributed to.
        model_id: The Bedrock model identifier invoked.
        family: Provider family the model belongs to.
        tier: The tier slot the model was invoked for.
        usage: Raw token counts, cache reads and writes kept separate.
        cache_min_checkpoint_tokens: The model's prompt-cache minimum checkpoint size.
        pricing_path: Path to ``config/pricing.yaml``. Overridable for tests.

    Returns:
        A fully priced :class:`UsageRecord`.
    """
    cost = cost_for_usage(usage, model_id, family=family, pricing_path=pricing_path)
    return UsageRecord(
        correlation_id=correlation_id,
        turn_id=turn_id,
        invocation_id=invocation_id,
        rep_id=rep_id,
        model_id=model_id,
        family=family,
        tier=tier,
        usage=usage,
        cost=cost,
        cache_min_checkpoint_tokens=cache_min_checkpoint_tokens,
    )


def _looks_like_secret(value: str) -> bool:
    """Return whether a string value has the shape of a token or opaque secret."""
    return bool(
        _JWT_PATTERN.match(value)
        or _OPAQUE_SECRET_PATTERN.match(value)
        or _AWS_ACCESS_KEY_ID_PATTERN.match(value)
    )


def _record_primary_key_attributes() -> frozenset[str]:
    """Return every table's primary partition-key attribute name from the schema, minus ``rep_id``.

    ``rep_id`` is excluded: every :class:`UsageRecord` log line carries it.
    """
    from crm.store.schema import TABLES

    return frozenset(
        schema.partition_key for schema in TABLES.values() if schema.partition_key != "rep_id"
    )


_RECORD_MARKER_ATTRIBUTES: Final[frozenset[str]] = _record_primary_key_attributes()


def _looks_like_record(value: object) -> bool:
    """Return whether ``value`` is shaped like a full entity record rather than a small field set.

    A mapping is a record when one of its top-level keys is in :data:`_RECORD_MARKER_ATTRIBUTES`.
    """
    return isinstance(value, dict) and not _RECORD_MARKER_ATTRIBUTES.isdisjoint(value)


def redact(value: object, *, _depth: int = 0) -> object:
    """Recursively redact secrets, tokens, and record payloads from a log value.

    Replaces with ``[REDACTED]``: values under :data:`_REDACT_VALUE_KEYS`, secret-shaped strings
    (:func:`_looks_like_secret`), record-shaped mappings (:func:`_looks_like_record`), and anything
    nested deeper than 20 levels.

    Args:
        value: Any JSON-serialisable value that might appear in a log field.
        _depth: Internal recursion guard; callers should not pass this.

    Returns:
        A value of the same shape with every secret-shaped, denylisted-key, or record-shaped value
        replaced by ``[REDACTED]``.
    """
    if _depth > 20:
        return _REDACTED
    if _looks_like_record(value):
        return _REDACTED
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str.lower() in _REDACT_VALUE_KEYS:
                cleaned[key_str] = _REDACTED
            else:
                cleaned[key_str] = redact(item, _depth=_depth + 1)
        return cleaned
    if isinstance(value, list | tuple):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str) and _looks_like_secret(value):
        return _REDACTED
    return value
