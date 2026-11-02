# SPDX-License-Identifier: MIT
"""The family-neutral answer-style system prompt and its provenance hash.

The prompt text lives in ``config/answer_style.yaml`` and is sent identically to every model
family, ahead of the page-context briefing, in production and in the eval.
:func:`answer_style_prompt_sha256` hashes the exact prompt text, and the harness records that
digest in run provenance.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Final

import yaml

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: The single source of the answer-style prompt. Overridable for tests.
ANSWER_STYLE_PATH: Final[Path] = _REPO_ROOT / "config" / "answer_style.yaml"

#: The schema version this loader understands; any other version is rejected.
_SUPPORTED_SCHEMA_VERSION: Final[int] = 1

#: The role the answer-style message is sent under.
_ANSWER_STYLE_ROLE: Final[str] = "system"


def load_answer_style_prompt(path: Path = ANSWER_STYLE_PATH) -> str:
    """Return the answer-style prompt text, verbatim from ``path``.

    Args:
        path: Path to ``config/answer_style.yaml``. Overridable for tests.

    Returns:
        The prompt text.

    Raises:
        ValueError: When the file does not parse to a mapping, carries an unsupported
            ``schema_version``, or is missing a non-empty string ``prompt``.
    """
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    version = data.get("schema_version")
    if version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"{path} has schema_version {version!r}, expected {_SUPPORTED_SCHEMA_VERSION}"
        )
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{path} is missing a non-empty string 'prompt'")
    return prompt


def answer_style_system_message(path: Path = ANSWER_STYLE_PATH) -> dict[str, object]:
    """Return the answer-style message in the provider seam's ``{role, content}`` shape.

    A ``system`` message carrying the prompt, ready to prepend to a turn's ``messages`` ahead of
    the page-context briefing.
    """
    return {"role": _ANSWER_STYLE_ROLE, "content": load_answer_style_prompt(path)}


def answer_style_prompt_sha256(path: Path = ANSWER_STYLE_PATH) -> str:
    """Return the SHA-256 hex digest of the prompt text.

    Hashes the prompt string encoded as UTF-8, not the YAML file, so a comment or formatting change
    that leaves the prompt unchanged leaves the digest unchanged.
    """
    return hashlib.sha256(load_answer_style_prompt(path).encode("utf-8")).hexdigest()


__all__ = [
    "ANSWER_STYLE_PATH",
    "answer_style_prompt_sha256",
    "answer_style_system_message",
    "load_answer_style_prompt",
]
