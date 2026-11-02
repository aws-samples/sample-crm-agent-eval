# SPDX-License-Identifier: MIT
"""Application configuration loading, including AWS profile/region resolution.

Resolution order for both ``profile`` and ``region``: an explicit override (typically a CLI flag),
then ``AWS_PROFILE``/``AWS_REGION``, then ``config/app.yaml``'s ``aws:`` block, then
``default``/``us-east-1``. Profile validation is offline and never calls AWS.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from crm.errors import ValidationFailed

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEFAULT_APP_CONFIG_PATH: Final[Path] = _REPO_ROOT / "config" / "app.yaml"

DEFAULT_PROFILE: Final[str] = "default"
DEFAULT_REGION: Final[str] = "us-east-1"

# AWS shared-config-file profile-name grammar (format only).
_PROFILE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class AwsConfig:
    """Resolved AWS profile and region, plus which source supplied each.

    Attributes:
        profile: The resolved profile name.
        region: The resolved region.
        profile_source: One of ``"flag"``, ``"environment"``, or ``"config"``.
        region_source: One of ``"flag"``, ``"environment"``, or ``"config"``.
    """

    profile: str
    region: str
    profile_source: str
    region_source: str


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Parse a YAML file into a mapping, raising if the top level is not a mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def _config_defaults(*, app_config_path: Path) -> tuple[str, str]:
    """Read the ``aws.profile``/``aws.region`` defaults from ``config/app.yaml`` if present.

    Falls back to :data:`DEFAULT_PROFILE`/:data:`DEFAULT_REGION` when the file or block is absent.
    """
    if not app_config_path.is_file():
        return DEFAULT_PROFILE, DEFAULT_REGION
    data = _load_yaml_mapping(app_config_path)
    aws_block = data.get("aws")
    if not isinstance(aws_block, dict):
        return DEFAULT_PROFILE, DEFAULT_REGION
    profile = aws_block.get("profile", DEFAULT_PROFILE)
    region = aws_block.get("region", DEFAULT_REGION)
    return str(profile), str(region)


def _known_profile_names() -> set[str] | None:
    """Collect profile names from ``~/.aws/config`` and ``~/.aws/credentials``, if present.

    Returns ``None`` when neither file exists.
    """
    config_path = Path(os.environ.get("AWS_CONFIG_FILE", Path.home() / ".aws" / "config"))
    credentials_path = Path(
        os.environ.get("AWS_SHARED_CREDENTIALS_FILE", Path.home() / ".aws" / "credentials")
    )

    if not config_path.is_file() and not credentials_path.is_file():
        return None

    names: set[str] = set()

    if config_path.is_file():
        parser = configparser.ConfigParser()
        parser.read(config_path)
        for section in parser.sections():
            # Named profiles are stored as "profile <name>"; the implicit default profile is a
            # bare "[default]" section.
            if section == "default":
                names.add("default")
            elif section.startswith("profile "):
                names.add(section[len("profile ") :])

    if credentials_path.is_file():
        parser = configparser.ConfigParser()
        parser.read(credentials_path)
        names.update(parser.sections())

    return names


def validate_profile_name(profile: str, *, source: str) -> None:
    """Validate a profile name without making any AWS call.

    The name must match ``[A-Za-z0-9_.-]+`` and, when ``~/.aws/config`` or
    ``~/.aws/credentials`` exists, appear as a profile section in one of them. Credential validity
    is not checked.

    Args:
        profile: The profile name to validate.
        source: Which input supplied ``profile`` — ``"flag"``, ``"environment"``, or ``"config"``.

    Raises:
        ValidationFailed: The name fails the format check, or fails the local catalogue check
            when a local catalogue is available.
    """
    if not _PROFILE_NAME_PATTERN.match(profile):
        raise ValidationFailed(
            f"AWS profile name {profile!r} from {source} is not a valid profile name.",
            source=source,
        )

    known = _known_profile_names()
    if known is not None and profile not in known:
        raise ValidationFailed(
            f"AWS profile {profile!r} from {source} is not defined in ~/.aws/config or "
            "~/.aws/credentials.",
            source=source,
        )


def resolve_aws_config(
    *,
    profile: str | None = None,
    region: str | None = None,
    app_config_path: Path = _DEFAULT_APP_CONFIG_PATH,
    validate: bool = True,
) -> AwsConfig:
    """Resolve the AWS profile and region: flag, then environment, then config default.

    Args:
        profile: An explicit override, typically from a CLI flag. Takes precedence over
            ``AWS_PROFILE`` and the config default.
        region: An explicit override, typically from a CLI flag. Takes precedence over
            ``AWS_REGION`` and the config default.
        app_config_path: Path to ``config/app.yaml``.
        validate: Whether to validate the resolved profile name with
            :func:`validate_profile_name`.

    Returns:
        The resolved :class:`AwsConfig`, naming which source supplied each value.

    Raises:
        ValidationFailed: ``validate`` is ``True`` and the resolved profile name is invalid.
    """
    config_profile, config_region = _config_defaults(app_config_path=app_config_path)

    if profile is not None:
        resolved_profile, profile_source = profile, "flag"
    elif os.environ.get("AWS_PROFILE"):
        resolved_profile, profile_source = os.environ["AWS_PROFILE"], "environment"
    else:
        resolved_profile, profile_source = config_profile, "config"

    if region is not None:
        resolved_region, region_source = region, "flag"
    elif os.environ.get("AWS_REGION"):
        resolved_region, region_source = os.environ["AWS_REGION"], "environment"
    else:
        resolved_region, region_source = config_region, "config"

    if validate:
        validate_profile_name(resolved_profile, source=profile_source)

    return AwsConfig(
        profile=resolved_profile,
        region=resolved_region,
        profile_source=profile_source,
        region_source=region_source,
    )
