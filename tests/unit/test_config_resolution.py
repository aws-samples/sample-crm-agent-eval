# SPDX-License-Identifier: MIT
"""Tests for AWS profile/region resolution and offline profile validation (crm.config)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts import proc_run

from crm.config import DEFAULT_PROFILE, DEFAULT_REGION, resolve_aws_config, validate_profile_name
from crm.errors import ValidationFailed

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_TEMPLATE = _REPO_ROOT / "config" / "app.yaml.example"

_ACCOUNT_ID_PATTERNS = (
    re.compile(r"arn:aws[a-zA-Z-]*:[a-zA-Z0-9-]+:[a-zA-Z0-9-]*:(0{12}|[0-9]{12}):"),
    re.compile(r"""(account[_-]?id|AccountId)["']?\s*[:=]\s*["']?[0-9]{12}"""),
)


def _write_app_yaml(tmp_path: Path, *, profile: str = "default", region: str = "us-east-1") -> Path:
    path = tmp_path / "app.yaml"
    path.write_text(f"schema_version: 1\naws:\n  profile: {profile}\n  region: {region}\n")
    return path


class TestNoAccountIdInTemplate:
    """The committed template must never carry an AWS account identifier."""

    def test_example_template_has_no_account_id(self) -> None:
        text = _EXAMPLE_TEMPLATE.read_text(encoding="utf-8")
        for pattern in _ACCOUNT_ID_PATTERNS:
            assert not pattern.search(text), f"account id pattern matched: {pattern.pattern}"

    def test_example_template_declares_aws_block(self) -> None:
        text = _EXAMPLE_TEMPLATE.read_text(encoding="utf-8")
        assert "profile: default" in text
        assert "region: us-east-1" in text


class TestResolutionOrder:
    """Flag, then environment, then config default — tested at all three levels."""

    def test_flag_wins_over_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_PROFILE", "env-profile")
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        app_yaml = _write_app_yaml(tmp_path, profile="config-profile", region="ap-south-1")

        result = resolve_aws_config(
            profile="flag-profile",
            region="flag-region",
            app_config_path=app_yaml,
            validate=False,
        )

        assert result.profile == "flag-profile"
        assert result.profile_source == "flag"
        assert result.region == "flag-region"
        assert result.region_source == "flag"

    def test_environment_wins_when_no_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_PROFILE", "env-profile")
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        app_yaml = _write_app_yaml(tmp_path, profile="config-profile", region="ap-south-1")

        result = resolve_aws_config(app_config_path=app_yaml, validate=False)

        assert result.profile == "env-profile"
        assert result.profile_source == "environment"
        assert result.region == "eu-west-1"
        assert result.region_source == "environment"

    def test_config_default_wins_when_no_flag_or_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        app_yaml = _write_app_yaml(tmp_path, profile="config-profile", region="ap-south-1")

        result = resolve_aws_config(app_config_path=app_yaml, validate=False)

        assert result.profile == "config-profile"
        assert result.profile_source == "config"
        assert result.region == "ap-south-1"
        assert result.region_source == "config"

    def test_all_three_sources_omitted_yields_documented_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        missing_app_yaml = tmp_path / "does-not-exist.yaml"

        result = resolve_aws_config(app_config_path=missing_app_yaml, validate=False)

        assert result.profile == DEFAULT_PROFILE == "default"
        assert result.profile_source == "config"
        assert result.region == DEFAULT_REGION == "us-east-1"
        assert result.region_source == "config"

    def test_independent_precedence_per_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flag for one field and nothing for the other resolve independently."""
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        app_yaml = _write_app_yaml(tmp_path, profile="config-profile", region="ap-south-1")

        result = resolve_aws_config(
            profile="flag-profile", app_config_path=app_yaml, validate=False
        )

        assert result.profile == "flag-profile"
        assert result.profile_source == "flag"
        assert result.region == "eu-west-1"
        assert result.region_source == "environment"


class TestProfileValidation:
    """Offline validation: format check always; local-catalogue check when files exist."""

    def test_malformed_profile_name_rejected_with_source(self) -> None:
        with pytest.raises(ValidationFailed) as exc_info:
            validate_profile_name("bad profile!", source="flag")
        assert exc_info.value.source == "flag"
        assert "flag" in exc_info.value.message

    def test_unknown_profile_rejected_naming_environment_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aws_config = tmp_path / "config"
        aws_config.write_text("[profile known-profile]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
        monkeypatch.setenv(
            "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials-does-not-exist")
        )

        with pytest.raises(ValidationFailed) as exc_info:
            validate_profile_name("unknown-profile", source="environment")

        assert exc_info.value.source == "environment"
        assert "unknown-profile" in exc_info.value.message
        assert "environment" in exc_info.value.message

    def test_unknown_profile_rejected_naming_config_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aws_config = tmp_path / "config"
        aws_config.write_text("[default]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
        monkeypatch.setenv(
            "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials-does-not-exist")
        )

        with pytest.raises(ValidationFailed) as exc_info:
            validate_profile_name("nonexistent", source="config")

        assert exc_info.value.source == "config"

    def test_known_profile_from_config_file_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aws_config = tmp_path / "config"
        aws_config.write_text("[profile deploy]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
        monkeypatch.setenv(
            "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials-does-not-exist")
        )

        validate_profile_name("deploy", source="flag")  # must not raise

    def test_default_profile_bare_section_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aws_config = tmp_path / "config"
        aws_config.write_text("[default]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
        monkeypatch.setenv(
            "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials-does-not-exist")
        )

        validate_profile_name("default", source="config")  # must not raise

    def test_known_profile_from_credentials_file_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        credentials = tmp_path / "credentials"
        credentials.write_text("[ci-profile]\naws_access_key_id = x\naws_secret_access_key = y\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config-does-not-exist"))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))

        validate_profile_name("ci-profile", source="environment")  # must not raise

    def test_no_local_catalogue_falls_back_to_format_check_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither ~/.aws/config nor ~/.aws/credentials exists: format-valid names pass."""
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config-does-not-exist"))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "creds-does-not-exist"))

        validate_profile_name("anything-well-formed", source="config")  # must not raise

    def test_resolve_aws_config_propagates_validation_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aws_config = tmp_path / "config"
        aws_config.write_text("[profile known]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
        monkeypatch.setenv(
            "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials-does-not-exist")
        )
        monkeypatch.setenv("AWS_PROFILE", "not-known")
        monkeypatch.delenv("AWS_REGION", raising=False)
        app_yaml = _write_app_yaml(tmp_path)

        with pytest.raises(ValidationFailed) as exc_info:
            resolve_aws_config(app_config_path=app_yaml)

        assert exc_info.value.source == "environment"

    def test_validate_false_skips_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aws_config = tmp_path / "config"
        aws_config.write_text("[profile known]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
        monkeypatch.setenv(
            "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials-does-not-exist")
        )
        monkeypatch.setenv("AWS_PROFILE", "not-known")
        monkeypatch.delenv("AWS_REGION", raising=False)
        app_yaml = _write_app_yaml(tmp_path)

        result = resolve_aws_config(app_config_path=app_yaml, validate=False)
        assert result.profile == "not-known"  # no raise


class TestCheckNoAccountIdGate:
    """The gate script itself passes against the committed template."""

    def test_check_no_account_id_script_passes(self) -> None:
        result = proc_run.run(
            # The gate requires an explicit subject; the working tree holds the committed template.
            ["bash", str(_REPO_ROOT / "scripts" / "check_no_account_id.sh"), "--worktree"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
