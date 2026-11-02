# SPDX-License-Identifier: MIT
"""Tests for `scripts/render_frontend_config.py`.

The renderer maps `terraform output -json` onto the seven `config.json` keys and refuses to write a
partial file. The written key set must equal `REQUIRED_KEYS` in
`frontend/src/config/runtimeConfig.ts` and the committed `public/config.example.json` template.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from scripts.render_frontend_config import main, render

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG_TS = REPO_ROOT / "frontend" / "src" / "config" / "runtimeConfig.ts"
CONFIG_EXAMPLE = REPO_ROOT / "frontend" / "public" / "config.example.json"

REGION = "us-east-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm_agent_eval-AbCdEf"


def _outputs(**overrides: object) -> dict[str, object]:
    """A `terraform output -json` object with the three outputs the renderer reads."""
    outputs: dict[str, object] = {
        "cognito_user_pool_id": {"value": "us-east-1_ABCDEFGHI", "sensitive": False},
        "cognito_spa_client_id": {"value": "1a2b3c4d5e6f7g8h9i0j", "sensitive": False},
        "agentcore_runtime_arn": {"value": RUNTIME_ARN, "sensitive": False},
        # Present in the real output set and ignored by the renderer.
        "cloudfront_domain_name": {"value": "d111111abcdef8.cloudfront.net"},
    }
    outputs.update(overrides)
    return outputs


def _required_keys_from_typescript() -> set[str]:
    """Parse `REQUIRED_KEYS` out of runtimeConfig.ts."""
    source = RUNTIME_CONFIG_TS.read_text(encoding="utf-8")
    match = re.search(r"const REQUIRED_KEYS:[^=]*=\s*\[(?P<body>.*?)\];", source, re.DOTALL)
    assert match, "could not locate REQUIRED_KEYS in runtimeConfig.ts"
    keys = set(re.findall(r'"([A-Za-z0-9_]+)"', match.group("body")))
    assert keys, "REQUIRED_KEYS parsed as empty — the regex above has drifted"
    return keys


class TestConfigKeyContract:
    """The rendered key set, the SPA's required key set, and the template agree."""

    def test_rendered_keys_equal_the_spa_required_keys(self) -> None:
        assert set(render(_outputs(), REGION)) == _required_keys_from_typescript()

    def test_rendered_keys_equal_the_committed_example_template(self) -> None:
        example = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        assert set(render(_outputs(), REGION)) == set(example)

    def test_the_typescript_key_set_is_non_vacuous(self) -> None:
        assert len(_required_keys_from_typescript()) == 7

    def test_every_rendered_value_is_a_string(self) -> None:
        # runtimeConfig.ts rejects a non-string value for any key.
        assert all(isinstance(v, str) for v in render(_outputs(), REGION).values())


class TestRenderedValues:
    def test_maps_each_terraform_output_onto_its_config_key(self) -> None:
        config = render(_outputs(), REGION)
        assert config["cognitoUserPoolId"] == "us-east-1_ABCDEFGHI"
        assert config["cognitoClientId"] == "1a2b3c4d5e6f7g8h9i0j"
        assert config["cognitoRegion"] == REGION

    def test_runtime_arn_is_written_raw_not_url_encoded(self) -> None:
        # The SPA hooks call encodeURIComponent() on this value themselves.
        config = render(_outputs(), REGION)
        assert config["agentRuntimeArn"] == RUNTIME_ARN
        assert "%3A" not in config["agentRuntimeArn"]

    def test_paths_are_cloudfront_relative_and_hosted_ui_is_empty(self) -> None:
        config = render(_outputs(), REGION)
        assert config["apiBaseUrl"] == "/api"
        assert config["agentBaseUrl"] == "/agent"
        # SRP-only auth: the key must exist as a string, and be empty.
        assert config["cognitoHostedUiDomain"] == ""

    def test_no_placeholder_survives_into_a_rendered_config(self) -> None:
        assert not any("REPLACE_AT_DEPLOY_TIME" in v for v in render(_outputs(), REGION).values())


class TestRefusesPartialOutput:
    """A config.json missing a key fails at SPA load; fail at render time instead."""

    @pytest.mark.parametrize(
        "missing",
        ["cognito_user_pool_id", "cognito_spa_client_id", "agentcore_runtime_arn"],
    )
    def test_absent_output_raises_and_names_it(self, missing: str) -> None:
        outputs = _outputs()
        del outputs[missing]
        with pytest.raises(KeyError, match=missing):
            render(outputs, REGION)

    @pytest.mark.parametrize("bad", ["", "   ", None, 42, ["x"]])
    def test_empty_or_non_string_output_raises(self, bad: object) -> None:
        with pytest.raises(ValueError, match="cognito_user_pool_id"):
            render(_outputs(cognito_user_pool_id={"value": bad}), REGION)

    def test_bare_value_without_the_terraform_wrapper_is_accepted(self) -> None:
        # An already-unwrapped map, as from `terraform output -json | jq 'map_values(.value)'`.
        config = render(_outputs(cognito_user_pool_id="us-east-1_PLAIN"), REGION)
        assert config["cognitoUserPoolId"] == "us-east-1_PLAIN"


class TestCli:
    def test_writes_the_file_and_exits_zero(self, tmp_path: Path) -> None:
        outputs_path = tmp_path / "outputs.json"
        outputs_path.write_text(json.dumps(_outputs()), encoding="utf-8")
        out = tmp_path / "nested" / "config.json"

        rc = main(
            [
                "--outputs-json",
                str(outputs_path),
                "--region",
                REGION,
                "--out",
                str(out),
            ]
        )

        assert rc == 0
        assert json.loads(out.read_text(encoding="utf-8")) == render(_outputs(), REGION)

    def test_writes_nothing_and_exits_one_on_a_missing_output(self, tmp_path: Path) -> None:
        outputs = _outputs()
        del outputs["agentcore_runtime_arn"]
        outputs_path = tmp_path / "outputs.json"
        outputs_path.write_text(json.dumps(outputs), encoding="utf-8")
        out = tmp_path / "config.json"

        assert (
            main(["--outputs-json", str(outputs_path), "--region", REGION, "--out", str(out)]) == 1
        )
        assert not out.exists()

    def test_exits_one_on_unparseable_input(self, tmp_path: Path) -> None:
        outputs_path = tmp_path / "outputs.json"
        outputs_path.write_text("not json", encoding="utf-8")
        out = tmp_path / "config.json"

        assert (
            main(["--outputs-json", str(outputs_path), "--region", REGION, "--out", str(out)]) == 1
        )
        assert not out.exists()

    def test_exits_one_when_the_outputs_are_a_json_array(self, tmp_path: Path) -> None:
        outputs_path = tmp_path / "outputs.json"
        outputs_path.write_text("[]", encoding="utf-8")
        out = tmp_path / "config.json"

        assert (
            main(["--outputs-json", str(outputs_path), "--region", REGION, "--out", str(out)]) == 1
        )
        assert not out.exists()
