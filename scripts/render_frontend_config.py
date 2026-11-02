#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render the SPA's deploy-time ``/config.json`` from ``terraform output -json``.

The frontend reads its Cognito ids and endpoint paths from ``/config.json`` at app
start, and ``frontend/src/config/runtimeConfig.ts`` rejects any missing or non-string
key. ``frontend/public/config.example.json`` is a placeholder template and is never
uploaded.

Usage:
    terraform -chdir=infra output -json | scripts/render_frontend_config.py \
        --region us-east-1 --out dist/config.json

Reads the ``terraform output -json`` object (stdin, or ``--outputs-json PATH``).
Exits non-zero, writing nothing, if any required output is absent or empty.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Terraform root output name -> config.json key; each must be present and non-empty.
_OUTPUT_TO_KEY = {
    "cognito_user_pool_id": "cognitoUserPoolId",
    "cognito_spa_client_id": "cognitoClientId",
    "agentcore_runtime_arn": "agentRuntimeArn",
}

# Fixed values. `apiBaseUrl`/`agentBaseUrl` are same-origin CloudFront paths, not hosts.
# `cognitoHostedUiDomain` is empty: sign-in uses SRP in-app, not the Hosted UI.
_FIXED = {
    "cognitoHostedUiDomain": "",
    "apiBaseUrl": "/api",
    "agentBaseUrl": "/agent",
}


def _extract(outputs: dict[str, Any], name: str) -> str:
    """Return the ``value`` of one ``terraform output -json`` entry as a non-empty str."""
    if name not in outputs:
        raise KeyError(
            f"terraform output {name!r} is absent. Has `terraform apply` completed "
            f"for this workspace? Present outputs: {sorted(outputs)}"
        )
    entry = outputs[name]
    value = entry.get("value") if isinstance(entry, dict) else entry
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"terraform output {name!r} is {value!r}; expected a non-empty string. "
            "Refusing to write a config.json the SPA would reject at load."
        )
    return value


def render(outputs: dict[str, Any], region: str) -> dict[str, str]:
    """Build the config.json body. Raises on any absent or empty required output."""
    config = {key: _extract(outputs, name) for name, key in sorted(_OUTPUT_TO_KEY.items())}
    # The raw ARN: the SPA URL-encodes it itself when building the `/agent/<arn>` path.
    config["cognitoRegion"] = region
    config.update(_FIXED)
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outputs-json",
        type=Path,
        help="File holding `terraform output -json` (default: read stdin)",
    )
    parser.add_argument(
        "--region",
        required=True,
        help="Cognito user pool region, e.g. us-east-1 (config.json cognitoRegion)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Path to write config.json to",
    )
    args = parser.parse_args(argv)

    raw = args.outputs_json.read_text(encoding="utf-8") if args.outputs_json else sys.stdin.read()
    try:
        outputs = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: could not parse terraform outputs as JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(outputs, dict):
        print(
            f"error: expected a JSON object from `terraform output -json`, got "
            f"{type(outputs).__name__}",
            file=sys.stderr,
        )
        return 1

    try:
        config = render(outputs, args.region)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(config)} keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
