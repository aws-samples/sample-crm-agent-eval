# SPDX-License-Identifier: MIT
"""Offline `terraform plan` harness for asserting plan-time properties of one infra module.

Writes a fixture root module to a tmp dir, runs `terraform init -backend=false` against the cached
provider plugins with mock credentials, and returns the `terraform show -json` resource changes.
`config/model_capabilities.yaml` is placed beside the root's parent, where the root's
`${path.module}/../config` resolves, unless a caller already wrote one there.
`terraform_unavailable_reason()` says why the harness cannot run; `require_terraform()` says whether
callers must fail instead of skip (`CRM_REQUIRE_TERRAFORM=1`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "infra" / ".terraform" / "providers"
MODULES_DIR = REPO_ROOT / "infra" / "modules"
CAPABILITIES_YAML = REPO_ROOT / "config" / "model_capabilities.yaml"

#: The aws provider version the fixture root pins.
AWS_PROVIDER_VERSION = "6.71.0"

REQUIRE_TERRAFORM_ENV_VAR = "CRM_REQUIRE_TERRAFORM"

_PROVIDER_BLOCK = f"""
terraform {{
  required_version = ">= 1.17.0"

  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "{AWS_PROVIDER_VERSION}"
    }}
  }}
}}

provider "aws" {{
  region                      = "us-east-1"
  access_key                  = "mock"
  secret_key                  = "mock" # pragma: allowlist secret
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
}}
"""


def terraform_unavailable_reason() -> str | None:
    """``None`` when the harness can run; else why it cannot, phrased for a skip message."""
    if shutil.which("terraform") is None:
        return "terraform is not on PATH"
    if not PLUGIN_DIR.is_dir():
        return f"no cached provider plugins at {PLUGIN_DIR} (run `cd infra && terraform init`)"
    return None


def require_terraform() -> bool:
    """Whether an unavailable terraform must fail rather than skip."""
    return os.environ.get(REQUIRE_TERRAFORM_ENV_VAR) == "1"


class TerraformError(RuntimeError):
    """A fixture `terraform` invocation failed; carries the captured output."""


@dataclass(frozen=True)
class Plan:
    """One planned module, indexed by resource address."""

    resource_changes: dict[str, dict[str, Any]]
    #: The plan document's `configuration` section (expressions, `depends_on`).
    configuration: dict[str, Any] = field(default_factory=dict)

    def after(self, address: str) -> dict[str, Any]:
        """The planned post-apply attribute map for `address`; raises if it is absent."""
        entry = self.resource_changes.get(address)
        if entry is None:
            raise AssertionError(
                f"{address} is not in the plan; addresses present: {sorted(self.resource_changes)}"
            )
        change = entry.get("change")
        after = change.get("after") if isinstance(change, dict) else None
        if not isinstance(after, dict):
            raise AssertionError(f"{address} has no planned `after` object: {entry!r}")
        return after

    def after_unknown(self, address: str) -> dict[str, Any]:
        """The `change.after_unknown` map for `address` (attributes unknown until apply).

        Returns `{}` when the resource has no unknown attributes; raises if the address is absent.
        """
        entry = self.resource_changes.get(address)
        if entry is None:
            raise AssertionError(
                f"{address} is not in the plan; addresses present: {sorted(self.resource_changes)}"
            )
        change = entry.get("change")
        after_unknown = change.get("after_unknown") if isinstance(change, dict) else None
        return after_unknown if isinstance(after_unknown, dict) else {}

    def addresses_of_type(self, resource_type: str) -> list[str]:
        return sorted(
            address
            for address, change in self.resource_changes.items()
            if change.get("type") == resource_type
        )


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return proc_run.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def plan_module(
    work_dir: Path,
    module_name: str,
    arguments: str,
    *,
    extra_hcl: str = "",
) -> Plan:
    """Plan `infra/modules/<module_name>` in `work_dir` and return the parsed plan.

    `arguments` is the raw HCL body of the `module` block; `extra_hcl` is appended to the fixture
    root for any supporting resource those arguments reference.
    """
    reason = terraform_unavailable_reason()
    if reason is not None:
        raise TerraformError(f"cannot plan: {reason}")

    module_dir = MODULES_DIR / module_name
    if not module_dir.is_dir():
        raise TerraformError(f"no module at {module_dir}")

    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "main.tf").write_text(
        f'{_PROVIDER_BLOCK}\n{extra_hcl}\nmodule "subject" {{\n'
        f'  source = "{module_dir}"\n\n{arguments}\n}}\n',
        encoding="utf-8",
    )
    return plan_directory(work_dir)


def plan_root(work_dir: Path, root_hcl: str) -> Plan:
    """Plan `root_hcl`, written as `main.tf` beneath the fixture provider block, in `work_dir`.

    `work_dir` may already hold module directories the root references.
    """
    reason = terraform_unavailable_reason()
    if reason is not None:
        raise TerraformError(f"cannot plan: {reason}")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "main.tf").write_text(f"{_PROVIDER_BLOCK}\n{root_hcl}\n", encoding="utf-8")
    return plan_directory(work_dir)


def place_capability_matrix(work_dir: Path) -> Path:
    """Copy the repository's capability matrix to `work_dir/../config/` unless one is already there.

    The copy is written atomically, so a concurrent plan sharing the parent never reads it partially.
    """
    target = work_dir.parent / "config" / CAPABILITIES_YAML.name
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(dir=target.parent, prefix=".capabilities-")
    with os.fdopen(fd, "wb") as handle:
        handle.write(CAPABILITIES_YAML.read_bytes())
    Path(scratch).replace(target)
    return target


def plan_directory(work_dir: Path) -> Plan:
    """Init, plan and `show -json` the root module already written in `work_dir`."""
    place_capability_matrix(work_dir)
    init = _run(
        [
            "terraform",
            "init",
            "-input=false",
            "-backend=false",
            "-no-color",
            f"-plugin-dir={PLUGIN_DIR}",
        ],
        work_dir,
    )
    if init.returncode != 0:
        raise TerraformError(f"terraform init failed ({init.returncode}):\n{init.stderr}")

    plan = _run(
        ["terraform", "plan", "-input=false", "-no-color", "-out=plan.bin"],
        work_dir,
    )
    if plan.returncode != 0:
        raise TerraformError(
            f"terraform plan failed ({plan.returncode}):\n{plan.stdout}\n{plan.stderr}"
        )

    show = _run(["terraform", "show", "-json", "plan.bin"], work_dir)
    if show.returncode != 0:
        raise TerraformError(f"terraform show failed ({show.returncode}):\n{show.stderr}")

    document = json.loads(show.stdout)
    changes: dict[str, dict[str, Any]] = {}
    for entry in document.get("resource_changes", []):
        address = entry.get("address")
        if isinstance(address, str):
            changes[address] = entry
    if not changes:
        raise TerraformError("the plan contains no resource changes, so it asserts nothing")
    configuration = document.get("configuration")
    return Plan(
        resource_changes=changes,
        configuration=configuration if isinstance(configuration, dict) else {},
    )
