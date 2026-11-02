# SPDX-License-Identifier: MIT
"""Behaviour of `scripts/bootstrap_account.sh`, run in a sandbox with stub `aws` and `terraform`.

The sandbox mirrors the repository layout the script needs: a copy of the real script, the real
`scripts/region_map.py` and `infra/regions.json`, stub
`scripts/render_vpc_tfvars.py` and `scripts/preflight_adopted_vpc.sh` (run by `bash`, so a shell shim
over a Python recorder), and `infra/backend.hcl`.
Every stub appends its argv (and, for the renderer, its stdin) to a JSON-lines log.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Final

import pytest
from scripts import proc_run

from tests.unit.test_bootstrap_outputs_contract import EVIDENCE_ONLY_OUTPUTS

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bootstrap_account.sh"
BOOTSTRAP_OUTPUTS_TF = REPO_ROOT / "infra" / "bootstrap_account" / "outputs.tf"
PREFLIGHT = REPO_ROOT / "scripts" / "preflight_adopted_vpc.sh"
REGION_MAP_SCRIPT = REPO_ROOT / "scripts" / "region_map.py"
REGION_MAP = REPO_ROOT / "infra" / "regions.json"

ACCOUNT: Final = "123456789012"
OTHER_ACCOUNT: Final = "111122223333"
PROFILE: Final = "crm-test"
#: The ambient profile the operator's shell exports; never the one the script must use.
AMBIENT_PROFILE: Final = "claude"
REGION: Final = "us-east-1"
STATE_BUCKET: Final = "crm-agent-eval-tfstate-test-bucket"
BOOTSTRAP_KEY: Final = "crm-agent-eval/bootstrap-account/terraform.tfstate"
MAIN_KEY: Final = "crm-agent-eval/terraform.tfstate"

#: `terraform output -json` the stub prints; ids are deliberately not real-id shaped.
OUTPUTS: Final[dict[str, Any]] = {
    "vpc_id": {"value": "vpc-0test", "type": "string", "sensitive": False},
    "private_subnet_ids": {"value": ["subnet-0p1", "subnet-0p2"], "type": ["list", "string"]},
    "private_route_table_ids": {"value": ["rtb-0p1", "rtb-0p2"], "type": ["list", "string"]},
    "nat_public_subnet_id": {"value": "subnet-0nat", "type": "string"},
    "runtime_subnet_ids": {"value": ["subnet-0r1", "subnet-0r2"], "type": ["list", "string"]},
    "runtime_route_table_ids": {"value": ["rtb-0rt"], "type": ["list", "string"]},
    "apigw_cloudwatch_role_arn": {"value": None, "type": "string"},
    "az_name_by_id": {"value": {"use1-az1": "us-east-1a"}, "type": ["map", "string"]},
}

#: Fake static credentials; each value is a unique marker that must never be printed.
CREDENTIAL_MARKERS: Final[dict[str, str]] = {
    "AWS_ACCESS_KEY_ID": "MARKER-AKID-7f3e",
    "AWS_SECRET_ACCESS_KEY": "MARKER-SECRET-9c1d",  # gitleaks:allow
    "AWS_SESSION_TOKEN": "MARKER-TOKEN-2b8a",
}

_STUB_PRELUDE = """\
import json, os, sys
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": TOOL, "argv": sys.argv[1:], "cwd": os.getcwd(),
                          "stdin": STDIN, "lc_all": os.environ.get("LC_ALL"),
                          "aws_profile": os.environ.get("AWS_PROFILE")}) + "\\n")
"""

AWS_STUB: Final = """
argv = sys.argv[1:]
if argv[:2] == ["sts", "get-caller-identity"]:
    print(os.environ["STUB_ACCOUNT"])
elif argv[:2] == ["s3api", "get-object"]:
    mode = os.environ.get("STUB_STATE", "absent")
    if mode == "absent":
        sys.stderr.write("An error occurred (NoSuchKey) when calling the GetObject operation\\n")
        sys.exit(254)
    if mode == "denied":
        sys.stderr.write("An error occurred (AccessDenied) when calling the GetObject operation\\n")
        sys.exit(254)
    outfile = argv[argv.index("--key") + 2]
    body = {"empty": {"resources": []}, "resources": {"resources": [{"type": "aws_vpc"}] * 3},
            "malformed": "not json"}[mode]
    with open(outfile, "w", encoding="utf-8") as handle:
        handle.write(body if isinstance(body, str) else json.dumps(body))
    print("{}")
elif argv[:2] == ["s3api", "list-object-versions"]:
    mode = os.environ.get("STUB_VERSIONS", "none")
    if mode == "denied":
        sys.stderr.write("An error occurred (AccessDenied) when calling ListObjectVersions\\n")
        sys.exit(254)
    key = argv[argv.index("--prefix") + 1]
    listing = {
        "none": None,
        "empty": {"Versions": [], "DeleteMarkers": []},
        "marker": {"DeleteMarkers": [{"Key": key, "IsLatest": True}]},
        "version": {"Versions": [{"Key": key, "IsLatest": False}]},
        "sibling": {"Versions": [{"Key": key + ".backup"}], "DeleteMarkers": [{"Key": key + "x"}]},
    }[mode]
    if listing is not None:
        print(json.dumps(listing))
else:
    sys.exit(99)
"""

TERRAFORM_STUB: Final = """
if "output" in sys.argv[1:]:
    with open(os.environ["STUB_OUTPUTS"], encoding="utf-8") as handle:
        sys.stdout.write(handle.read())
"""

RENDER_STUB: Final = "sys.exit(int(os.environ.get('STUB_RENDER_RC', '0')))\n"
PREFLIGHT_STUB: Final = "sys.exit(int(os.environ.get('STUB_PREFLIGHT_RC', '0')))\n"


@dataclass(frozen=True)
class Sandbox:
    root: Path
    bin_dir: Path
    log: Path
    outputs: Path

    def env(self, **overrides: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "HOME": str(self.root),
            "STUB_LOG": str(self.log),
            "STUB_OUTPUTS": str(self.outputs),
            "STUB_ACCOUNT": ACCOUNT,
            "CRM_EXPECTED_ACCOUNT_ID": ACCOUNT,
            "AWS_PROFILE": AMBIENT_PROFILE,
        }
        env.update(overrides)
        return {k: v for k, v in env.items() if v != ""}

    def run(self, *args: str, env: dict[str, str] | None = None) -> CompletedProcess[str]:
        return proc_run.run(
            ["bash", str(self.root / "scripts" / "bootstrap_account.sh"), *args],
            cwd=self.root,
            env=self.env() if env is None else env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [e for e in entries if tool is None or e["tool"] == tool]


def _write_stub(path: Path, tool: str, body: str, *, read_stdin: bool = False) -> None:
    stdin_expr = "sys.stdin.read()" if read_stdin else "None"
    prelude = _STUB_PRELUDE.replace("TOOL", repr(tool)).replace("STDIN", stdin_expr)
    path.write_text(f"#!{sys.executable}\n{prelude}{body}", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "infra" / "bootstrap_account").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "bootstrap_account.sh")
    shutil.copy2(REGION_MAP_SCRIPT, root / "scripts" / "region_map.py")
    shutil.copy2(REGION_MAP, root / "infra" / "regions.json")
    (root / "infra" / "backend.hcl").write_text(
        f'bucket         = "{STATE_BUCKET}"\nkey            = "{MAIN_KEY}"\n'
        f'region         = "{REGION}"\n',
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "aws", "aws", AWS_STUB)
    _write_stub(bin_dir / "terraform", "terraform", TERRAFORM_STUB)
    (bin_dir / "python3").symlink_to(sys.executable)
    _write_stub(root / "scripts" / "render_vpc_tfvars.py", "render", RENDER_STUB, read_stdin=True)
    recorder = tmp_path / "preflight_recorder.py"
    _write_stub(recorder, "preflight", PREFLIGHT_STUB)
    (root / "scripts" / "preflight_adopted_vpc.sh").write_text(
        f'exec "{sys.executable}" "{recorder}" "$@"\n', encoding="utf-8"
    )
    outputs = tmp_path / "outputs.json"
    outputs.write_text(json.dumps(OUTPUTS), encoding="utf-8")
    return Sandbox(root=root, bin_dir=bin_dir, log=tmp_path / "calls.jsonl", outputs=outputs)


class TestSandbox:
    """The sandbox reaches only the stubs, and the script under test is the real one."""

    def test_aws_and_terraform_resolve_to_the_stubs(self, sandbox: Sandbox) -> None:
        path = sandbox.env()["PATH"]
        for tool in ("aws", "terraform"):
            assert shutil.which(tool, path=path) == str(sandbox.bin_dir / tool)

    def test_the_script_is_a_byte_copy_of_the_repository_script(self, sandbox: Sandbox) -> None:
        copy = sandbox.root / "scripts" / "bootstrap_account.sh"
        assert copy.read_bytes() == SCRIPT.read_bytes()

    def test_an_unmutated_happy_run_records_calls(self, sandbox: Sandbox) -> None:
        result = sandbox.run("--profile", PROFILE, "--region", REGION)
        assert result.returncode == 0, result.stderr
        assert sandbox.calls("terraform"), "the stub log recorded no terraform call"


class TestRefusalsBeforeAnyTerraformCall:
    """Each refusal exits 2 and terraform is never invoked."""

    @pytest.mark.parametrize("variable", sorted(CREDENTIAL_MARKERS))
    def test_each_static_credential_variable_refuses(self, sandbox: Sandbox, variable: str) -> None:
        env = sandbox.env(**{variable: CREDENTIAL_MARKERS[variable]})
        result = sandbox.run("--profile", PROFILE, "--region", REGION, env=env)
        assert result.returncode == 2
        assert "Static credentials" in result.stderr
        assert sandbox.calls() == []

    def test_credential_values_are_never_printed(self, sandbox: Sandbox) -> None:
        env = sandbox.env(**CREDENTIAL_MARKERS)
        result = sandbox.run("--profile", PROFILE, "--region", REGION, env=env)
        assert result.returncode == 2
        for marker in CREDENTIAL_MARKERS.values():
            assert marker not in result.stdout
            assert marker not in result.stderr

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--region", REGION), id="no-profile"),
            pytest.param(("--profile", PROFILE), id="no-region"),
            pytest.param(("--profile",), id="profile-without-value"),
            pytest.param(("--profile", PROFILE, "--region", REGION, "--bogus"), id="unknown-arg"),
        ],
    )
    def test_argument_refusals(self, sandbox: Sandbox, args: tuple[str, ...]) -> None:
        result = sandbox.run(*args)
        assert result.returncode == 2, result.stderr
        assert "REFUSED" in result.stderr
        assert sandbox.calls() == []

    @pytest.mark.parametrize(
        "variable", ["AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_STS", "AWS_ENDPOINT_URL_S3"]
    )
    def test_each_endpoint_override_variable_refuses(self, sandbox: Sandbox, variable: str) -> None:
        marker = "https://endpoint-marker.example"
        env = sandbox.env(**{variable: marker})
        result = sandbox.run("--profile", PROFILE, "--region", REGION, env=env)
        assert result.returncode == 2
        assert "endpoint override" in result.stderr
        assert variable in result.stderr
        assert marker not in result.stdout + result.stderr
        assert sandbox.calls() == []

    def test_a_variable_merely_containing_the_endpoint_name_does_not_refuse(
        self, sandbox: Sandbox
    ) -> None:
        env = sandbox.env(NOT_AWS_ENDPOINT_URL="https://kept.example")
        result = sandbox.run("--profile", PROFILE, "--region", REGION, env=env)
        assert result.returncode == 0, result.stderr

    def test_unset_expected_account_refuses_before_any_aws_call(self, sandbox: Sandbox) -> None:
        result = sandbox.run(
            "--profile", PROFILE, "--region", REGION, env=sandbox.env(CRM_EXPECTED_ACCOUNT_ID="")
        )
        assert result.returncode == 2
        assert "CRM_EXPECTED_ACCOUNT_ID is not set" in result.stderr
        assert sandbox.calls() == []

    def test_missing_backend_config_refuses_before_any_aws_call(self, sandbox: Sandbox) -> None:
        (sandbox.root / "infra" / "backend.hcl").unlink()
        result = sandbox.run("--profile", PROFILE, "--region", REGION)
        assert result.returncode == 2
        assert "backend.hcl" in result.stderr
        assert sandbox.calls() == []

    @pytest.mark.parametrize("destroy", [False, True], ids=["apply", "destroy"])
    def test_wrong_account_refuses_after_sts_only(self, sandbox: Sandbox, destroy: bool) -> None:
        args = ["--profile", PROFILE, "--region", REGION] + (["--destroy"] if destroy else [])
        result = sandbox.run(*args, env=sandbox.env(STUB_ACCOUNT=OTHER_ACCOUNT))
        assert result.returncode == 2
        assert "WRONG ACCOUNT" in result.stderr
        assert OTHER_ACCOUNT in result.stderr and ACCOUNT in result.stderr
        assert [c["argv"][:2] for c in sandbox.calls()] == [["sts", "get-caller-identity"]]


class TestRegionGate:
    """`--region` must be in `infra/regions.json`, checked by the real map CLI before any aws call."""

    @pytest.mark.parametrize("region", ["us-west-2", "us-west-1", "us-east-2"])
    def test_a_mapped_region_passes_and_scopes_every_call(
        self, sandbox: Sandbox, region: str
    ) -> None:
        result = sandbox.run("--profile", PROFILE, "--region", region)
        assert result.returncode == 0, result.stderr
        aws_calls = sandbox.calls("aws")
        assert aws_calls[0]["argv"][:2] == ["sts", "get-caller-identity"]
        for call in aws_calls:
            argv = call["argv"]
            assert argv[argv.index("--region") + 1] == region, argv
        terraform_vars = [arg for c in sandbox.calls("terraform") for arg in c["argv"]]
        assert f"aws_region={region}" in terraform_vars
        assert f"aws_region={REGION}" not in terraform_vars

    @pytest.mark.parametrize("region", ["eu-west-1", "us-gov-west-1", "us-west-3"])
    def test_a_region_outside_the_map_refuses_before_any_aws_call(
        self, sandbox: Sandbox, region: str
    ) -> None:
        result = sandbox.run("--profile", PROFILE, "--region", region)
        assert result.returncode == 2, result.stderr
        assert "REFUSED" in result.stderr
        assert "infra/regions.json" in result.stderr
        assert "us-east-1, us-east-2, us-west-1, us-west-2" in result.stderr
        assert sandbox.calls() == []

    @pytest.mark.parametrize(
        "corruption",
        [
            pytest.param("{not json", id="malformed"),
            pytest.param(json.dumps({"schema_version": 1}), id="missing-keys"),
        ],
    )
    def test_an_invalid_map_refuses_even_for_a_mapped_region(
        self, sandbox: Sandbox, corruption: str
    ) -> None:
        (sandbox.root / "infra" / "regions.json").write_text(corruption, encoding="utf-8")
        result = sandbox.run("--profile", PROFILE, "--region", REGION)
        assert result.returncode == 2, result.stderr
        assert "cannot read the region map infra/regions.json (exit 1)" in result.stderr
        assert sandbox.calls() == []

    def test_an_absent_map_refuses(self, sandbox: Sandbox) -> None:
        (sandbox.root / "infra" / "regions.json").unlink()
        result = sandbox.run("--profile", PROFILE, "--region", REGION)
        assert result.returncode == 2, result.stderr
        assert "cannot read the region map" in result.stderr
        assert sandbox.calls() == []

    def test_the_usage_names_the_map(self, sandbox: Sandbox) -> None:
        result = sandbox.run("--help")
        assert result.returncode == 0
        assert "a region in infra/regions.json (default example: us-east-1)" in result.stdout


def _assert_aws_scoping(calls: list[dict[str, Any]]) -> None:
    assert calls, "no aws call was recorded"
    for call in calls:
        argv = call["argv"]
        assert argv[argv.index("--profile") + 1] == PROFILE, argv
        assert argv[argv.index("--region") + 1] == REGION, argv
        assert "--no-cli-pager" in argv, argv


EXPECTED_INIT: Final = [
    "-chdir=infra/bootstrap_account",
    "init",
    "-input=false",
    "-reconfigure",
    "-backend-config=../backend.hcl",
    f"-backend-config=key={BOOTSTRAP_KEY}",
]
BASE_VARS: Final = ["-var", f"aws_profile={PROFILE}", "-var", f"aws_region={REGION}"]


class TestApply:
    """The apply path: init, apply, render, preflight, in that order, with exact argv."""

    def test_the_call_sequence_and_argv_are_exact(self, sandbox: Sandbox) -> None:
        result = sandbox.run("--profile", PROFILE, "--region", REGION)
        assert result.returncode == 0, result.stderr
        sequence = [(c["tool"], c["argv"]) for c in sandbox.calls() if c["tool"] != "aws"]
        assert sequence == [
            ("terraform", EXPECTED_INIT),
            (
                "terraform",
                ["-chdir=infra/bootstrap_account", "apply", "-input=false", "-auto-approve"]
                + BASE_VARS,
            ),
            ("terraform", ["-chdir=infra/bootstrap_account", "output", "-json"]),
            (
                "render",
                [
                    "--outputs-json",
                    "-",
                    "--tfvars",
                    "infra/terraform.tfvars",
                    "--example",
                    "infra/terraform.tfvars.example",
                ],
            ),
            (
                "preflight",
                [
                    "--profile", PROFILE,
                    "--region", REGION,
                    "--vpc-id", "vpc-0test",
                    "--private-subnet-ids", "subnet-0p1,subnet-0p2",
                    "--route-table-ids", "rtb-0p1,rtb-0p2",
                    "--nat-subnet-id", "subnet-0nat",
                    "--runtime-subnet-ids", "subnet-0r1,subnet-0r2",
                    "--runtime-route-table-ids", "rtb-0rt",
                ],
            ),
        ]  # fmt: skip

    def test_every_call_runs_from_the_repository_root(self, sandbox: Sandbox) -> None:
        assert sandbox.run("--profile", PROFILE, "--region", REGION).returncode == 0
        assert {Path(c["cwd"]).resolve() for c in sandbox.calls()} == {sandbox.root.resolve()}

    def test_the_renderer_receives_the_terraform_outputs_on_stdin(self, sandbox: Sandbox) -> None:
        assert sandbox.run("--profile", PROFILE, "--region", REGION).returncode == 0
        (render,) = sandbox.calls("render")
        assert json.loads(render["stdin"]) == OUTPUTS

    def test_every_aws_call_is_scoped_and_unpaged(self, sandbox: Sandbox) -> None:
        assert sandbox.run("--profile", PROFILE, "--region", REGION).returncode == 0
        _assert_aws_scoping(sandbox.calls("aws"))

    def test_no_apigw_logging_adds_exactly_the_flag_variable(self, sandbox: Sandbox) -> None:
        result = sandbox.run("--profile", PROFILE, "--region", REGION, "--no-apigw-logging")
        assert result.returncode == 0, result.stderr
        (apply,) = [c for c in sandbox.calls("terraform") if "apply" in c["argv"]]
        assert apply["argv"][4:] == BASE_VARS + ["-var", "manage_apigw_account_logging=false"]

    def test_a_failing_preflight_fails_the_run(self, sandbox: Sandbox) -> None:
        result = sandbox.run(
            "--profile", PROFILE, "--region", REGION, env=sandbox.env(STUB_PREFLIGHT_RC="1")
        )
        assert result.returncode == 1
        assert "preflight refused" in result.stderr

    def test_an_empty_output_refuses_before_the_preflight(self, sandbox: Sandbox) -> None:
        outputs = dict(OUTPUTS, runtime_subnet_ids={"value": [], "type": ["list", "string"]})
        sandbox.outputs.write_text(json.dumps(outputs), encoding="utf-8")
        result = sandbox.run("--profile", PROFILE, "--region", REGION)
        assert result.returncode == 1
        assert "runtime_subnet_ids" in result.stderr
        assert sandbox.calls("preflight") == []

    def test_a_missing_output_refuses_before_the_preflight(self, sandbox: Sandbox) -> None:
        outputs = {k: v for k, v in OUTPUTS.items() if k != "nat_public_subnet_id"}
        sandbox.outputs.write_text(json.dumps(outputs), encoding="utf-8")
        result = sandbox.run("--profile", PROFILE, "--region", REGION)
        assert result.returncode == 1
        assert "nat_public_subnet_id" in result.stderr
        assert sandbox.calls("preflight") == []

    def test_the_apply_path_never_reads_the_main_state(self, sandbox: Sandbox) -> None:
        assert sandbox.run("--profile", PROFILE, "--region", REGION).returncode == 0
        assert all(c["argv"][:2] != ["s3api", "get-object"] for c in sandbox.calls("aws"))


class TestDestroy:
    """`--destroy` reads the main state object and refuses while it lists any resource."""

    def _run(self, sandbox: Sandbox, state: str) -> CompletedProcess[str]:
        return sandbox.run(
            "--profile", PROFILE, "--region", REGION, "--destroy", env=sandbox.env(STUB_STATE=state)
        )

    def _get_object(self, sandbox: Sandbox) -> list[str]:
        (call,) = [c for c in sandbox.calls("aws") if c["argv"][:2] == ["s3api", "get-object"]]
        argv: list[str] = call["argv"]
        return argv

    def test_it_refuses_while_the_main_state_lists_resources(self, sandbox: Sandbox) -> None:
        result = self._run(sandbox, "resources")
        assert result.returncode == 1
        assert "lists 3 resource(s)" in result.stderr
        assert sandbox.calls("terraform") == []

    def test_it_reads_the_main_state_key_in_the_backend_bucket(self, sandbox: Sandbox) -> None:
        self._run(sandbox, "resources")
        argv = self._get_object(sandbox)
        assert argv[argv.index("--bucket") + 1] == STATE_BUCKET
        assert argv[argv.index("--key") + 1] == MAIN_KEY
        _assert_aws_scoping(sandbox.calls("aws"))

    @pytest.mark.parametrize("state", ["empty", "absent"])
    def test_it_destroys_when_the_main_state_lists_nothing(
        self, sandbox: Sandbox, state: str
    ) -> None:
        result = self._run(sandbox, state)
        assert result.returncode == 0, result.stderr
        assert [c["argv"] for c in sandbox.calls("terraform")] == [
            EXPECTED_INIT,
            ["-chdir=infra/bootstrap_account", "destroy", "-input=false", "-auto-approve"]
            + BASE_VARS,
        ]
        assert sandbox.calls("render") == [] and sandbox.calls("preflight") == []

    @pytest.mark.parametrize("state", ["denied", "malformed"])
    def test_an_unreadable_main_state_refuses(self, sandbox: Sandbox, state: str) -> None:
        result = self._run(sandbox, state)
        assert result.returncode == 1
        assert "Nothing was destroyed" in result.stderr
        assert sandbox.calls("terraform") == []


def test_the_script_parses_and_is_executable() -> None:
    """`bash -n` accepts the script, and it is executable as tracked."""
    result = proc_run.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert os.access(SCRIPT, os.X_OK)


class TestLocale:
    """The script's own matchers run under `LC_ALL=C`; every child sees the caller's `LC_ALL`."""

    CALLER_LOCALE: Final = "en_US.UTF-8"

    def test_the_pin_precedes_every_matcher(self) -> None:
        body = [
            line
            for line in SCRIPT.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        pin = body.index("export LC_ALL=C")
        matchers = [i for i, line in enumerate(body) if re.search(r"\b(grep|sed)\b", line)]
        assert matchers, "the script no longer carries a matcher; this cell is vacuous"
        assert pin < min(matchers)

    @pytest.mark.parametrize("destroy", [False, True], ids=["apply", "destroy"])
    def test_children_inherit_a_set_caller_locale(self, sandbox: Sandbox, destroy: bool) -> None:
        args = ["--profile", PROFILE, "--region", REGION] + (["--destroy"] if destroy else [])
        env = sandbox.env(LC_ALL=self.CALLER_LOCALE, STUB_STATE="empty")
        result = sandbox.run(*args, env=env)
        assert result.returncode == 0, result.stderr
        expected_tools = {"aws", "terraform"} | (set() if destroy else {"render", "preflight"})
        assert {c["tool"] for c in sandbox.calls()} == expected_tools
        assert {c["lc_all"] for c in sandbox.calls()} == {self.CALLER_LOCALE}

    @pytest.mark.parametrize("destroy", [False, True], ids=["apply", "destroy"])
    def test_children_see_no_locale_when_the_caller_set_none(
        self, sandbox: Sandbox, destroy: bool
    ) -> None:
        args = ["--profile", PROFILE, "--region", REGION] + (["--destroy"] if destroy else [])
        env = sandbox.env(STUB_STATE="empty")
        assert "LC_ALL" not in env
        result = sandbox.run(*args, env=env)
        assert result.returncode == 0, result.stderr
        assert sandbox.calls()
        assert {c["lc_all"] for c in sandbox.calls()} == {None}


class TestChildIdentity:
    """Every child runs as `--profile`, not as the ambient `AWS_PROFILE`."""

    @pytest.mark.parametrize("destroy", [False, True], ids=["apply", "destroy"])
    def test_children_see_the_profile_flag_not_the_ambient_profile(
        self, sandbox: Sandbox, destroy: bool
    ) -> None:
        env = sandbox.env(STUB_STATE="empty")
        assert env["AWS_PROFILE"] == AMBIENT_PROFILE
        args = ["--profile", PROFILE, "--region", REGION] + (["--destroy"] if destroy else [])
        result = sandbox.run(*args, env=env)
        assert result.returncode == 0, result.stderr
        assert sandbox.calls("terraform")
        assert {c["aws_profile"] for c in sandbox.calls()} == {PROFILE}


class TestRendererFailure:
    """A renderer failure after a successful apply is a step failure, never a refusal."""

    def test_it_exits_1_says_the_root_is_applied_and_skips_the_preflight(
        self, sandbox: Sandbox
    ) -> None:
        result = sandbox.run(
            "--profile", PROFILE, "--region", REGION, env=sandbox.env(STUB_RENDER_RC="2")
        )
        assert result.returncode == 1
        assert "the bootstrap root IS applied" in result.stderr
        assert any("apply" in c["argv"] for c in sandbox.calls("terraform"))
        assert sandbox.calls("preflight") == []


class TestDestroyGuardKeyAndWorkspace:
    """The guard reads the key from `backend.hcl`, refuses non-default workspaces, and treats an
    absent object as never deployed only when no version or delete marker exists for that key."""

    MOVED_KEY: Final = "crm-agent-eval/moved/terraform.tfstate"

    def _write_backend(self, sandbox: Sandbox, key_line: str) -> None:
        (sandbox.root / "infra" / "backend.hcl").write_text(
            f'bucket = "{STATE_BUCKET}"\n{key_line}region = "{REGION}"\n', encoding="utf-8"
        )

    def _destroy(self, sandbox: Sandbox, **env: str) -> CompletedProcess[str]:
        return sandbox.run(
            "--profile", PROFILE, "--region", REGION, "--destroy", env=sandbox.env(**env)
        )

    def _s3_calls(self, sandbox: Sandbox) -> list[list[str]]:
        return [c["argv"] for c in sandbox.calls("aws") if c["argv"][0] == "s3api"]

    def test_a_drifted_key_is_the_one_read(self, sandbox: Sandbox) -> None:
        self._write_backend(sandbox, f'key = "{self.MOVED_KEY}"\n')
        result = self._destroy(sandbox, STUB_STATE="resources")
        assert result.returncode == 1
        assert "lists 3 resource(s)" in result.stderr
        (get_object,) = self._s3_calls(sandbox)
        assert get_object[get_object.index("--key") + 1] == self.MOVED_KEY
        assert sandbox.calls("terraform") == []

    def test_a_missing_key_refuses_before_any_state_read(self, sandbox: Sandbox) -> None:
        self._write_backend(sandbox, "")
        result = self._destroy(sandbox, STUB_STATE="empty")
        assert result.returncode == 2
        assert "names no key" in result.stderr
        assert self._s3_calls(sandbox) == [] and sandbox.calls("terraform") == []

    @pytest.mark.parametrize("source", ["env", "environment-file"])
    def test_a_non_default_workspace_refuses(self, sandbox: Sandbox, source: str) -> None:
        env: dict[str, str] = {"STUB_STATE": "empty"}
        if source == "env":
            env["TF_WORKSPACE"] = "staging"
        else:
            (sandbox.root / "infra" / ".terraform").mkdir()
            (sandbox.root / "infra" / ".terraform" / "environment").write_text(
                "staging", encoding="utf-8"
            )
        result = self._destroy(sandbox, **env)
        assert result.returncode == 2
        assert "staging" in result.stderr
        assert self._s3_calls(sandbox) == [] and sandbox.calls("terraform") == []

    @pytest.mark.parametrize("source", ["env", "environment-file"])
    def test_the_default_workspace_proceeds(self, sandbox: Sandbox, source: str) -> None:
        env: dict[str, str] = {"STUB_STATE": "empty"}
        if source == "env":
            env["TF_WORKSPACE"] = "default"
        else:
            (sandbox.root / "infra" / ".terraform").mkdir()
            (sandbox.root / "infra" / ".terraform" / "environment").write_text(
                "default", encoding="utf-8"
            )
        result = self._destroy(sandbox, **env)
        assert result.returncode == 0, result.stderr
        assert any("destroy" in c["argv"] for c in sandbox.calls("terraform"))

    @pytest.mark.parametrize("versions", ["marker", "version"])
    def test_an_absent_object_with_history_refuses(self, sandbox: Sandbox, versions: str) -> None:
        result = self._destroy(sandbox, STUB_STATE="absent", STUB_VERSIONS=versions)
        assert result.returncode == 1
        assert "deleted, not torn down" in result.stderr
        listing = self._s3_calls(sandbox)[-1]
        assert listing[:2] == ["s3api", "list-object-versions"]
        assert listing[listing.index("--bucket") + 1] == STATE_BUCKET
        assert listing[listing.index("--prefix") + 1] == MAIN_KEY
        _assert_aws_scoping(sandbox.calls("aws"))
        assert sandbox.calls("terraform") == []

    @pytest.mark.parametrize("versions", ["none", "empty", "sibling"])
    def test_an_absent_object_without_history_for_that_exact_key_proceeds(
        self, sandbox: Sandbox, versions: str
    ) -> None:
        result = self._destroy(sandbox, STUB_STATE="absent", STUB_VERSIONS=versions)
        assert result.returncode == 0, result.stderr
        assert any("destroy" in c["argv"] for c in sandbox.calls("terraform"))

    def test_an_unlistable_history_refuses(self, sandbox: Sandbox) -> None:
        result = self._destroy(sandbox, STUB_STATE="absent", STUB_VERSIONS="denied")
        assert result.returncode == 1
        assert "cannot list versions" in result.stderr
        assert sandbox.calls("terraform") == []

    def test_a_present_object_is_never_version_listed(self, sandbox: Sandbox) -> None:
        assert self._destroy(sandbox, STUB_STATE="empty").returncode == 0
        assert [a[1] for a in self._s3_calls(sandbox)] == ["get-object"]


#: Preflight flags that carry no bootstrap output.
PREFLIGHT_NON_OUTPUT_FLAGS: Final = frozenset(
    {"--profile", "--region", "--min-free-ips", "--managed-service-tag"}
)


def _output_names(outputs_tf: str) -> set[str]:
    return set(re.findall(r'^output "([a-z_]+)"', outputs_tf, flags=re.MULTILINE))


def _script_output_flag_pairs(script: str) -> dict[str, str]:
    loop = re.search(r"^for pair in (.*?); do$", script, flags=re.MULTILINE | re.DOTALL)
    assert loop is not None, "the output->flag loop is gone from bootstrap_account.sh"
    return dict(re.findall(r"([a-z_]+):(--[a-z-]+)", loop.group(1)))


def _preflight_flags(preflight: str) -> set[str]:
    return set(re.findall(r"^\s+(--[a-z-]+)\)", preflight, flags=re.MULTILINE))


def _output_flag_problems(outputs_tf: str, script: str, preflight: str) -> list[str]:
    """Disagreements between outputs.tf, the script's output->flag pairs and the preflight's flags."""
    outputs = _output_names(outputs_tf)
    pairs = _script_output_flag_pairs(script)
    flags = _preflight_flags(preflight)
    problems = [f"pair names absent output {name!r}" for name in sorted(set(pairs) - outputs)]
    problems += [
        f"pair passes unparsed flag {flag!r}" for flag in sorted(set(pairs.values()) - flags)
    ]
    problems += [
        f"preflight flag {flag!r} receives no output"
        for flag in sorted(flags - PREFLIGHT_NON_OUTPUT_FLAGS - set(pairs.values()))
    ]
    problems += [
        f"output {name!r} reaches neither a pair nor the evidence-only set"
        for name in sorted(outputs - set(pairs) - EVIDENCE_ONLY_OUTPUTS)
    ]
    return problems


class TestOutputFlagContract:
    """outputs.tf, the script's output->flag pairs and the preflight's parsed flags agree, all ways."""

    def _sources(self) -> tuple[str, str, str]:
        return (
            BOOTSTRAP_OUTPUTS_TF.read_text(encoding="utf-8"),
            SCRIPT.read_text(encoding="utf-8"),
            PREFLIGHT.read_text(encoding="utf-8"),
        )

    def test_the_three_sources_agree(self) -> None:
        outputs_tf, script, preflight = self._sources()
        assert len(_script_output_flag_pairs(script)) == 6
        assert _output_flag_problems(outputs_tf, script, preflight) == []

    @pytest.mark.parametrize(
        ("side", "old", "new", "expected"),
        [
            pytest.param(
                "outputs", 'output "vpc_id"', 'output "vpc_ident"', "pair names absent output",
                id="output-renamed",
            ),
            pytest.param(
                "script", "nat_public_subnet_id:--nat-subnet-id",
                "nat_public_subnet_id:--nat-subnet", "pair passes unparsed flag",
                id="script-flag-renamed",
            ),
            pytest.param(
                "script", " runtime_route_table_ids:--runtime-route-table-ids", "",
                "receives no output", id="script-pair-dropped",
            ),
            pytest.param(
                "preflight", "--runtime-subnet-ids)", "--runtime-subnets)",
                "pair passes unparsed flag", id="preflight-flag-renamed",
            ),
        ],
    )  # fmt: skip
    def test_a_one_sided_change_reds_the_contract(
        self, side: str, old: str, new: str, expected: str
    ) -> None:
        sources = dict(zip(("outputs", "script", "preflight"), self._sources(), strict=True))
        assert sources[side].count(old) == 1, old
        sources[side] = sources[side].replace(old, new)
        problems = _output_flag_problems(
            sources["outputs"], sources["script"], sources["preflight"]
        )
        assert any(expected in problem for problem in problems), problems
