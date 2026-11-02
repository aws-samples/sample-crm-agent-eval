# SPDX-License-Identifier: MIT
"""Loader for the agent system prompts under ``config/agent_prompts/``.

Each ``<name>.yaml`` file holds ``{schema_version: 1, prompt: <str>}``; :func:`load_agent_prompts`
returns every prompt keyed by file stem. The text is sent identically to every model family.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: The directory holding one YAML file per agent prompt. Overridable for tests.
AGENT_PROMPTS_DIR: Final[Path] = _REPO_ROOT / "config" / "agent_prompts"

#: The schema version this loader understands; any other version is rejected.
_SUPPORTED_SCHEMA_VERSION: Final[int] = 1


def _load_prompt(path: Path) -> str:
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    if set(data) != {"schema_version", "prompt"}:
        raise ValueError(f"{path} must carry exactly the keys ['prompt', 'schema_version']")
    version = data["schema_version"]
    if version != _SUPPORTED_SCHEMA_VERSION or isinstance(version, bool):
        raise ValueError(
            f"{path} has schema_version {version!r}, expected {_SUPPORTED_SCHEMA_VERSION}"
        )
    prompt = data["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{path} is missing a non-empty string 'prompt'")
    return prompt


def load_agent_prompts(directory: Path = AGENT_PROMPTS_DIR) -> Mapping[str, str]:
    """Return every agent prompt in ``directory``, keyed by file stem, in sorted file order.

    Args:
        directory: The prompt directory. Overridable for tests.

    Returns:
        ``{stem: prompt}`` for every ``*.yaml`` file; empty when the directory is absent.

    Raises:
        ValueError: A file does not parse to exactly ``{schema_version: 1, prompt: <str>}``.
    """
    if not directory.is_dir():
        return {}
    return {path.stem: _load_prompt(path) for path in sorted(directory.glob("*.yaml"))}


__all__ = ["AGENT_PROMPTS_DIR", "load_agent_prompts"]
