#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Export `src/crm/toolspec/definitions.py` as a JSON Schema bundle for TypeScript generation.

Invoked by `frontend/scripts/generate-types.mjs` (via `npm run gen:types`) to produce the
frontend's generated types (`frontend/src/types/generated/`). It reflects
`crm.toolspec.definitions.ALL_TOOLS` and nothing else.

Emits one JSON Schema definition per tool, named `<ToolNamePascalCase>Input`, plus a companion
`_meta` object (risk class, table name, and agent roles) that is not itself a schema but travels
alongside it for the generator script to attach as an exported TypeScript const.

Run from the repository root with the project's pinned interpreter (`.venv`):

    python3 frontend/scripts/export_tool_schemas.py > frontend/scripts/.tool-schemas.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# `src/` is not on `sys.path` when this script is invoked directly by path.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from crm.toolspec.definitions import ALL_TOOLS  # noqa: E402


def _pascal_case(tool_name: str) -> str:
    """`search_opportunities` -> `SearchOpportunities`."""
    return "".join(part.capitalize() for part in tool_name.split("_"))


def build_bundle() -> dict[str, object]:
    """Render every tool definition into a JSON-serialisable bundle.

    Returns:
        A dict with two top-level keys:

        * ``schemas`` — mapping of ``<ToolNamePascalCase>Input`` to a JSON Schema object, fed
          directly to ``json-schema-to-typescript``.
        * ``meta`` — mapping of the original snake_case tool name to
          ``{toolName, display_label, typeName, risk, agents, mutates}``, consumed by
          ``generate-types.mjs`` to emit the ``TOOL_METADATA`` const and the ``ToolName`` union.
          ``display_label`` is UI-only and never enters the MCP projection.
    """
    schemas: dict[str, object] = {}
    meta: dict[str, object] = {}
    for tool in ALL_TOOLS:
        type_name = f"{_pascal_case(tool.name)}Input"
        schemas[type_name] = {**tool.input_schema, "title": type_name}
        meta[tool.name] = {
            "toolName": tool.name,
            "display_label": tool.display_label,
            "typeName": type_name,
            "risk": tool.risk,
            "agents": sorted(tool.agents),
            "mutates": tool.mutates,
        }
    return {"schemas": schemas, "meta": meta}


def main() -> int:
    json.dump(build_bundle(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
