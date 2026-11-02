# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/check_vpc_endpoint_coverage.py`.

Most assertions run against a synthetic source tree and a synthetic `main.tf`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_vpc_endpoint_coverage.py"

if TYPE_CHECKING:
    import scripts.check_vpc_endpoint_coverage as gate
else:
    _SPEC = importlib.util.spec_from_file_location("check_vpc_endpoint_coverage", SCRIPT_PATH)
    assert _SPEC is not None and _SPEC.loader is not None
    gate = importlib.util.module_from_spec(_SPEC)
    sys.modules["check_vpc_endpoint_coverage"] = gate
    _SPEC.loader.exec_module(gate)


def _main_tf(
    interface: list[str], gateway: list[str] | None = None, *, runtime_vpc_mode: bool = False
) -> str:
    """Return a minimal `main.tf` carrying the endpoint lists and optional VPC-mode literal."""
    gateway_list = ["s3", "dynamodb"] if gateway is None else gateway
    interface_body = "".join(f'    "{name}",\n' for name in interface)
    gateway_body = ", ".join(f'"{name}"' for name in gateway_list)
    # The gate reads `runtime_network_mode = "VPC"` at the `module "agentcore"` call site.
    agentcore_block = (
        'module "agentcore" {\n  runtime_network_mode = "VPC"\n}\n\n' if runtime_vpc_mode else ""
    )
    return (
        "locals {\n"
        "  # a comment naming secretsmanager must not be read as a member\n"
        f"  vpc_interface_endpoint_services = [\n{interface_body}  ]\n"
        "}\n\n"
        f"{agentcore_block}"
        'module "vpc" {\n'
        "  interface_endpoint_service_names = local.vpc_interface_endpoint_services\n"
        f"  gateway_endpoint_service_names   = [{gateway_body}]\n"
        "}\n"
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A synthetic `src/` that constructs the sentinel plus one service under test."""
    src = tmp_path / "src"
    (src / "crm").mkdir(parents=True)
    (src / "crm" / "store.py").write_text(
        "import boto3\n_ddb = boto3.client('dynamodb')\n", encoding="utf-8"
    )
    return src


def test_passes_when_every_discovered_service_has_an_endpoint(tree: Path, tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    report = gate.check(tree_root=tree, main_tf=main_tf)
    assert report["ok"] is True
    assert report["missing_endpoints"] == {}


def test_fails_naming_the_service_and_the_file_that_constructs_it(
    tree: Path, tmp_path: Path
) -> None:
    (tree / "crm" / "origin.py").write_text(
        "import boto3\n_sm = boto3.client('secretsmanager')\n", encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")

    report = gate.check(tree_root=tree, main_tf=main_tf)

    assert report["ok"] is False
    assert "secretsmanager" in report["missing_endpoints"]
    assert any(path.endswith("origin.py") for path in report["missing_endpoints"]["secretsmanager"])


def test_the_same_tree_passes_once_the_endpoint_is_declared(tree: Path, tmp_path: Path) -> None:
    """A tree that fails without an endpoint passes once that endpoint is declared."""
    (tree / "crm" / "origin.py").write_text(
        "import boto3\n_sm = boto3.client('secretsmanager')\n", encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms", "secretsmanager"]), encoding="utf-8")

    report = gate.check(tree_root=tree, main_tf=main_tf)

    assert report["ok"] is True
    assert report["missing_endpoints"] == {}


def test_a_gateway_endpoint_satisfies_the_check_too(tmp_path: Path) -> None:
    """A service declared only as a gateway endpoint satisfies the check."""
    src = tmp_path / "src"
    (src / "crm").mkdir(parents=True)
    (src / "crm" / "store.py").write_text(
        "import boto3\n_ddb = boto3.client('dynamodb')\n", encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    # `dynamodb` appears ONLY in the gateway list here.
    main_tf.write_text(_main_tf(["kms"], gateway=["s3", "dynamodb"]), encoding="utf-8")
    assert gate.check(tree_root=src, main_tf=main_tf)["ok"] is True


def test_allowlisted_service_needs_no_endpoint(tree: Path, tmp_path: Path) -> None:
    """An allowlisted service such as `sts` needs no endpoint."""
    (tree / "providers.py").write_text("import boto3\n_s = boto3.client('sts')\n", encoding="utf-8")
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")

    report = gate.check(tree_root=tree, main_tf=main_tf)

    assert report["ok"] is True
    assert "sts" in report["allowlisted"]


def test_bedrock_runtime_now_requires_an_endpoint(tree: Path, tmp_path: Path) -> None:
    """A tree constructing a `bedrock-runtime` client requires its endpoint."""
    (tree / "providers.py").write_text(
        "import boto3\n_b = boto3.client('bedrock-runtime')\n", encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    report = gate.check(tree_root=tree, main_tf=main_tf)
    assert report["ok"] is False
    assert "bedrock-runtime" in report["missing_endpoints"]
    assert "bedrock-runtime" not in report["allowlisted"]

    # The other direction: declaring the endpoint satisfies it.
    main_tf.write_text(_main_tf(["kms", "bedrock-runtime"]), encoding="utf-8")
    assert gate.check(tree_root=tree, main_tf=main_tf)["ok"] is True


def test_vpc_mode_requires_the_ecr_image_pull_endpoints(tree: Path, tmp_path: Path) -> None:
    """In VPC mode the `ecr.api` and `ecr.dkr` endpoints are required without any SDK client."""
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(
        _main_tf(["kms", "bedrock-runtime"], runtime_vpc_mode=True), encoding="utf-8"
    )
    report = gate.check(tree_root=tree, main_tf=main_tf)
    assert report["ok"] is False
    assert set(report["non_sdk_missing_endpoints"]) == {"ecr.api", "ecr.dkr"}
    assert report["runtime_vpc_mode"] is True

    # Declaring both ECR endpoints satisfies it.
    main_tf.write_text(
        _main_tf(["kms", "bedrock-runtime", "ecr.api", "ecr.dkr"], runtime_vpc_mode=True),
        encoding="utf-8",
    )
    assert gate.check(tree_root=tree, main_tf=main_tf)["ok"] is True


def test_ecr_endpoints_are_not_required_outside_vpc_mode(tree: Path, tmp_path: Path) -> None:
    """Without the VPC-mode literal, a tree with no ECR endpoints passes."""
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"], runtime_vpc_mode=False), encoding="utf-8")
    report = gate.check(tree_root=tree, main_tf=main_tf)
    assert report["ok"] is True
    assert report["runtime_vpc_mode"] is False
    assert report["non_sdk_missing_endpoints"] == {}


def test_allowlist_is_not_a_blanket_exemption(tree: Path, tmp_path: Path) -> None:
    """A service absent from the allowlist still requires an endpoint."""
    (tree / "probe.py").write_text("import boto3\n_k = boto3.client('kinesis')\n", encoding="utf-8")
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    assert "kinesis" in gate.check(tree_root=tree, main_tf=main_tf)["missing_endpoints"]


def test_every_allowlist_entry_states_a_reason(tree: Path, tmp_path: Path) -> None:
    """Every allowlist entry carries a non-trivial reason."""
    for service, reason in gate._OUTSIDE_VPC_ALLOWLIST.items():
        assert reason.strip(), f"allowlist entry {service!r} carries no reason"
        assert len(reason) > 20, f"allowlist entry {service!r} has a reason too terse to act on"


def test_unused_allowlist_entry_is_reported_but_not_a_failure(tree: Path, tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    report = gate.check(
        tree_root=tree,
        main_tf=main_tf,
        allowlist={"glacier": "nothing calls this, but the exemption is written down"},
    )
    assert report["ok"] is True
    assert report["unused_allowlist_entries"] == ["glacier"]


def test_typescript_client_imports_are_discovered(tree: Path, tmp_path: Path) -> None:
    relay = tree / "relay" / "src"
    relay.mkdir(parents=True)
    (relay / "parameters.ts").write_text(
        'import { SSMClient } from "@aws-sdk/client-ssm";\n', encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    assert "ssm" in gate.check(tree_root=tree, main_tf=main_tf)["missing_endpoints"]


def test_typescript_tests_are_not_a_source_of_in_vpc_clients(tree: Path, tmp_path: Path) -> None:
    """A client imported only by a TypeScript test requires no endpoint."""
    relay = tree / "relay" / "tests"
    relay.mkdir(parents=True)
    (relay / "runtime.test.ts").write_text(
        'import { X } from "@aws-sdk/client-kinesis";\n', encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    assert gate.check(tree_root=tree, main_tf=main_tf)["ok"] is True


def test_a_service_named_only_in_a_docstring_is_not_discovered(tree: Path, tmp_path: Path) -> None:
    """A service named only inside a docstring is not discovered."""
    (tree / "prose.py").write_text(
        '"""Builds boto3.client("kinesis") in a docstring, not in code."""\n', encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    assert gate.check(tree_root=tree, main_tf=main_tf)["ok"] is True


def test_a_session_built_client_is_discovered(tree: Path, tmp_path: Path) -> None:
    """A client built through `boto3.Session().client(...)` is discovered."""
    (tree / "sess.py").write_text(
        "import boto3\nsession = boto3.Session()\n_c = session.client('kinesis')\n",
        encoding="utf-8",
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    assert "kinesis" in gate.check(tree_root=tree, main_tf=main_tf)["missing_endpoints"]


def test_a_multiline_client_construction_is_discovered(tree: Path, tmp_path: Path) -> None:
    (tree / "wrapped.py").write_text(
        "import boto3\n_c = boto3.client(\n    'kinesis',\n    region_name='us-east-1',\n)\n",
        encoding="utf-8",
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    assert "kinesis" in gate.check(tree_root=tree, main_tf=main_tf)["missing_endpoints"]


def test_a_service_alias_is_resolved_to_its_endpoint_name(tree: Path, tmp_path: Path) -> None:
    """`boto3.client("cloudwatch")` is satisfied by the `monitoring` endpoint."""
    (tree / "metrics.py").write_text(
        "import boto3\n_cw = boto3.client('cloudwatch')\n", encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms", "monitoring"]), encoding="utf-8")
    report = gate.check(tree_root=tree, main_tf=main_tf)
    assert report["ok"] is True, "the alias was not applied, so a reachable service read as missing"
    assert "monitoring" in report["discovered"]


def test_a_comment_in_the_hcl_list_is_not_read_as_a_member(tree: Path, tmp_path: Path) -> None:
    """A service named in a comment inside the HCL list is not read as declared."""
    (tree / "crm" / "origin.py").write_text(
        "import boto3\n_sm = boto3.client('secretsmanager')\n", encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    assert gate.check(tree_root=tree, main_tf=main_tf)["ok"] is False


# --- unverifiable inputs ------------------------------------------------------------------------


def test_empty_scan_is_unverifiable_not_a_pass(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "nothing.py").write_text("x = 1\n", encoding="utf-8")
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    with pytest.raises(gate.UnverifiableError, match="sentinel|no SDK client"):
        gate.check(tree_root=src, main_tf=main_tf)


def test_missing_sentinel_is_unverifiable_even_when_other_services_are_found(
    tmp_path: Path,
) -> None:
    """A scan that misses the sentinel is unverifiable even when it finds other services."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("import boto3\n_c = boto3.client('kms')\n", encoding="utf-8")
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    with pytest.raises(gate.UnverifiableError, match="sentinel"):
        gate.check(tree_root=src, main_tf=main_tf)


def test_absent_endpoint_list_is_unverifiable(tree: Path, tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text("locals {\n  something_else = []\n}\n", encoding="utf-8")
    with pytest.raises(gate.UnverifiableError, match="vpc_interface_endpoint_services"):
        gate.check(tree_root=tree, main_tf=main_tf)


def test_absent_gateway_list_is_unverifiable(tree: Path, tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(
        'locals {\n  vpc_interface_endpoint_services = [\n    "kms",\n  ]\n}\n', encoding="utf-8"
    )
    with pytest.raises(gate.UnverifiableError, match="gateway_endpoint_service_names"):
        gate.check(tree_root=tree, main_tf=main_tf)


def test_empty_interface_list_is_unverifiable(tree: Path, tmp_path: Path) -> None:
    """An empty interface endpoint list is unverifiable."""
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf([]), encoding="utf-8")
    with pytest.raises(gate.UnverifiableError, match="lists no services"):
        gate.check(tree_root=tree, main_tf=main_tf)


def test_unreadable_main_tf_is_unverifiable(tree: Path, tmp_path: Path) -> None:
    with pytest.raises(gate.UnverifiableError, match="cannot read"):
        gate.check(tree_root=tree, main_tf=tmp_path / "absent.tf")


def test_missing_source_tree_is_unverifiable(tmp_path: Path) -> None:
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")
    with pytest.raises(gate.UnverifiableError, match="not found"):
        gate.check(tree_root=tmp_path / "absent", main_tf=main_tf)


# --- the CLI surface ----------------------------------------------------------------------------


def test_cli_exit_code_is_one_against_the_real_tree_today() -> None:
    """The CLI exits 0 or 1 against the real tree, naming `secretsmanager` when it exits 1."""
    result = proc_run.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode in (0, 1), (
        f"expected a verdict, got {result.returncode}: {result.stdout}"
    )
    if result.returncode == 1:
        assert "secretsmanager" in result.stdout


def test_cli_json_is_parseable_and_carries_the_verdict() -> None:
    import json

    result = proc_run.run(
        [sys.executable, str(SCRIPT_PATH), "--json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is (result.returncode == 0)
    assert "discovered" in payload


def test_the_gate_is_registered_in_run_gates() -> None:
    """The gate is registered in `run_gates.py`."""
    registry = (REPO_ROOT / "scripts" / "run_gates.py").read_text(encoding="utf-8")
    assert "check_vpc_endpoint_coverage" in registry


def test_the_gate_script_is_tracked_by_git() -> None:
    """The gate script is tracked by git."""
    result = proc_run.run(
        ["git", "ls-files", "--error-unmatch", "scripts/check_vpc_endpoint_coverage.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, "the gate script is not tracked by git"


def test_neutered_predicate_is_caught(tree: Path, tmp_path: Path) -> None:
    """The gate's verdict differs from an unconditional pass on a tree with a missing endpoint."""
    (tree / "crm" / "origin.py").write_text(
        "import boto3\n_sm = boto3.client('secretsmanager')\n", encoding="utf-8"
    )
    main_tf = tmp_path / "main.tf"
    main_tf.write_text(_main_tf(["kms"]), encoding="utf-8")

    real = gate.check(tree_root=tree, main_tf=main_tf)["ok"]
    neutered = True  # what `return True` in place of the comparison would yield

    assert real != neutered, (
        "the gate's verdict matches an unconditional pass on a tree with a missing endpoint, "
        "so the comparison is not what these tests are measuring"
    )
