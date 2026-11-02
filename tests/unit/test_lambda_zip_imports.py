# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_lambda_zip_imports.py`, the gate over a built zip's import closure.

Each phase is proven in both directions against synthetic zips built in `tmp_path` and, when a real
build directory exists, against the actual built zips.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from scripts import proc_run

from crm.toolspec.definitions import ALL_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "check_lambda_zip_imports.py"
REAL_BUILD_DIR = REPO_ROOT / "infra" / "build" / "lambda"


def _load_gate() -> ModuleType:
    """Import the gate script as a module without requiring it to be importable as a package."""
    spec = importlib.util.spec_from_file_location("check_lambda_zip_imports", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


LINUX_WHEEL = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\nTag: cp313-cp313-manylinux2014_x86_64\n"
DARWIN_WHEEL = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\nTag: cp313-cp313-macosx_11_0_arm64\n"
PURE_WHEEL = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def _metadata(name: str, version: str = "1.0", *requires: str) -> str:
    """A ``METADATA`` file with an RFC 822 header block and a description body after it."""
    header = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    for requirement in requires:
        header += f"Requires-Dist: {requirement}\n"
    return header + "\nThis is the long description, which may itself mention Requires-Dist:.\n"


def _write_zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def _handler_zip(
    path: Path, *, vendor: bool, wheel: str = PURE_WHEEL, requires: tuple[str, ...] = ()
) -> Path:
    """A minimal zip whose handler imports one vendored third-party module plus stdlib and boto3."""
    members = {
        "crm/__init__.py": "",
        "crm/handlers/__init__.py": "",
        "crm/handlers/synthetic.py": (
            "import json\nimport boto3\nfrom crm.support import helper\n"
            "def handler(event, context):\n    return helper(json, boto3)\n"
        ),
        "crm/support.py": "import vendormod\ndef helper(a, b):\n    return vendormod\n",
    }
    if vendor:
        members["vendormod/__init__.py"] = "VALUE = 1\n"
        members["vendormod-1.0.dist-info/WHEEL"] = wheel
        members["vendormod-1.0.dist-info/METADATA"] = _metadata("vendormod", "1.0", *requires)
        members["vendormod-1.0.dist-info/RECORD"] = "vendormod/__init__.py,,\n"
    return _write_zip(path, members)


SYNTHETIC_SPEC = gate.ZipSpec("synthetic.zip", "crm.handlers.synthetic.handler")


def _write_synthetic_build_dir(build_dir: Path) -> None:
    """Write one stub zip per expected zip *name*, carrying every handler module that name serves.

    Members are grouped by zip name because several handlers share `eval_trigger.zip`.
    """
    by_name: dict[str, dict[str, str]] = {}
    for spec in gate.expected_zips():
        members = by_name.setdefault(spec.zip_name, {})
        module_path = spec.module.replace(".", "/")
        members[f"{module_path}.py"] = "def handler(event, context):\n    return None\n"
        parts = spec.module.split(".")
        for depth in range(1, len(parts)):
            members["/".join(parts[:depth]) + "/__init__.py"] = ""
    for name, members in by_name.items():
        _write_zip(build_dir / name, members)


# ---------------------------------------------------------------------------
# 1. Inventory: the gate covers every Lambda zip package_lambdas.sh builds.
# ---------------------------------------------------------------------------


_NON_TOOL_ZIPS = {
    "identity_interceptor.zip",
    "pre_token_generation.zip",
    "eval_trigger.zip",
    "chat_target.zip",
}


def test_expected_inventory_is_every_tool_plus_the_non_tool_lambda_handlers() -> None:
    specs = gate.expected_zips()
    names = [s.zip_name for s in specs]
    # One zip per tool + each non-tool zip, with eval_trigger.zip counted once per handler it
    # serves. `specs` counts (zip, handler) pairs and `names` counts archives.
    shared_handlers = sum(1 for s in specs if s.zip_name == "eval_trigger.zip")
    assert len(specs) == len(ALL_TOOLS) + len(_NON_TOOL_ZIPS) - 1 + shared_handlers, names
    assert len(set(names)) == len(ALL_TOOLS) + len(_NON_TOOL_ZIPS), (
        "an unexpected zip name collision in the inventory"
    )
    for name in _NON_TOOL_ZIPS:
        assert name in names, names
    tool_zips = [n for n in names if n not in _NON_TOOL_ZIPS]
    assert sorted(tool_zips) == sorted(f"{tool.name}.zip" for tool in ALL_TOOLS), tool_zips
    # An exact set, so a handler leaving the shared archive fails as well as one arriving.
    assert sorted(s.handler for s in specs if s.zip_name == "eval_trigger.zip") == [
        "crm.handlers.capabilities.handler",
        "crm.handlers.eval_trigger.handler",
        "crm.handlers.user_settings.handler",
        "eval.sfn_handlers.sweep_control_handler",
        "eval.sfn_handlers.sweep_task_handler",
    ]
    assert [s.handler for s in specs if s.zip_name == "chat_target.zip"] == [
        "gateway.chat_target.lambda_handler"
    ]


def test_every_expected_spec_declares_a_module_bearing_handler() -> None:
    for spec in gate.expected_zips():
        assert spec.handler.count(".") >= 2, spec
        assert spec.module and not spec.module.endswith(".handler") or spec.module


def test_every_python_lambda_handler_the_composition_declares_is_in_the_inventory() -> None:
    """Every first-party Python Lambda handler the root `infra/*.tf` files declare is in the inventory.

    The relay is a Node.js Lambda and is out of scope.
    """
    root = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((REPO_ROOT / "infra").glob("*.tf"))
    )
    declared = {
        match.group(1)
        for match in re.finditer(
            r'handler\s*=\s*"((?:crm|gateway|agents|providers|eval)\.[^"]+)"', root
        )
    }
    assert declared, "no Python Lambda handler found in infra/*.tf — the pattern has drifted"
    assert "eval.sfn_handlers.sweep_task_handler" in declared
    covered = {spec.handler for spec in gate.expected_zips()}
    assert sorted(declared - covered) == []


#: The zips nested under `src/` (`PYTHONPATH=/var/task/src`) so `config/` resolves at the root.
_NESTED_LAYOUT_ZIPS = frozenset({"eval_trigger.zip", "chat_target.zip"})


def test_only_the_nested_zips_depart_from_the_flat_archive_layout() -> None:
    """Only `eval_trigger.zip` and `chat_target.zip` use the nested layout; every other zip is flat."""
    for spec in gate.expected_zips():
        expected = (
            gate.EVAL_TRIGGER_LAYOUT if spec.zip_name in _NESTED_LAYOUT_ZIPS else gate.FLAT_LAYOUT
        )
        assert spec.package_roots == expected, spec
    assert gate.EVAL_TRIGGER_LAYOUT[0] == ("src", "src"), (
        "src must be searched before the archive root: it is what PYTHONPATH=/var/task/src does"
    )


# ---------------------------------------------------------------------------
# 2. Closure phase — both directions on an unvendored third-party import.
# ---------------------------------------------------------------------------


def test_a_zip_that_vendors_its_third_party_closure_passes(tmp_path: Path) -> None:
    result = gate.check_zip(
        SYNTHETIC_SPEC, _handler_zip(tmp_path / "synthetic.zip", vendor=True), source_root=None
    )
    assert result.violations == []
    assert result.isolated_import_status.startswith("pass")


def test_a_zip_missing_its_third_party_module_fails_in_both_phases(tmp_path: Path) -> None:
    """First-party source importing a module the zip never vendored fails both phases."""
    result = gate.check_zip(
        SYNTHETIC_SPEC, _handler_zip(tmp_path / "synthetic.zip", vendor=False), source_root=None
    )
    assert result.violations, "an unvendored third-party import must be a violation"
    assert any("vendormod" in v for v in result.violations)
    assert result.isolated_import_status == "fail"
    assert any("isolated import" in v for v in result.violations)


def test_a_zip_missing_its_declared_handler_module_entirely_fails(tmp_path: Path) -> None:
    path = _write_zip(tmp_path / "synthetic.zip", {"crm/__init__.py": ""})
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert any("is not present in the zip at all" in v for v in result.violations)


def test_boto3_is_accepted_as_runtime_provided_but_an_arbitrary_module_is_not(
    tmp_path: Path,
) -> None:
    """Guards the allowlist itself: it must not be so wide that the check cannot fail."""
    assert gate._is_satisfied_elsewhere("boto3.session")
    assert gate._is_satisfied_elsewhere("json")
    assert not gate._is_satisfied_elsewhere("jsonschema")
    assert not gate._is_satisfied_elsewhere("yaml")


def test_an_import_guarded_by_try_except_importerror_is_not_a_violation(tmp_path: Path) -> None:
    path = _write_zip(
        tmp_path / "synthetic.zip",
        {
            "crm/__init__.py": "",
            "crm/handlers/__init__.py": "",
            "crm/handlers/synthetic.py": (
                "try:\n    import optionalmod\nexcept ImportError:\n    optionalmod = None\n"
                "def handler(event, context):\n    return optionalmod\n"
            ),
        },
    )
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert result.violations == [], result.violations


def test_an_import_nested_in_a_function_is_still_part_of_the_closure(tmp_path: Path) -> None:
    """A deferred import breaks the Lambda at first invocation rather than at init — still a defect."""
    path = _write_zip(
        tmp_path / "synthetic.zip",
        {
            "crm/__init__.py": "",
            "crm/handlers/__init__.py": "",
            "crm/handlers/synthetic.py": (
                "def handler(event, context):\n    import lazymod\n    return lazymod\n"
            ),
        },
    )
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert any("lazymod" in v for v in result.violations), result.violations


# ---------------------------------------------------------------------------
# 3. Wheel-platform phase — host-platform wheels and extension modules.
# ---------------------------------------------------------------------------


def test_a_host_platform_wheel_tag_fails_even_when_every_module_is_present(tmp_path: Path) -> None:
    """`rpds-py` is compiled: a zip built without --platform looks complete and fails on Lambda."""
    path = _handler_zip(tmp_path / "synthetic.zip", vendor=True, wheel=DARWIN_WHEEL)
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert any("non-Lambda wheel tag" in v for v in result.violations), result.violations


def test_a_linux_wheel_tag_and_a_pure_python_tag_both_pass(tmp_path: Path) -> None:
    for wheel in (LINUX_WHEEL, PURE_WHEEL):
        path = _handler_zip(tmp_path / f"synthetic_{hash(wheel)}.zip", vendor=True, wheel=wheel)
        result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
        assert result.violations == [], (wheel, result.violations)


@pytest.mark.parametrize(
    "members",
    [
        {"vendormod-1.0.dist-info/WHEEL": LINUX_WHEEL.replace("cp313-cp313", "cp312-cp312")},
        {"ext/__init__.py": "", "ext/ext.cpython-312-x86_64-linux-gnu.so": "\x00binary"},
    ],
    ids=["cp312_wheel_tag", "cpython_312_extension"],
)
def test_an_artifact_built_for_the_previous_runtime_minor_version_fails(
    tmp_path: Path, members: dict[str, str]
) -> None:
    """A cp312 wheel tag or a cpython-312 extension does not load on the python3.13 runtime."""
    zip_members = {
        "crm/__init__.py": "",
        "crm/handlers/__init__.py": "",
        "crm/handlers/synthetic.py": "def handler(event, context):\n    return None\n",
        **members,
    }
    result = gate.check_zip(
        SYNTHETIC_SPEC, _write_zip(tmp_path / "synthetic.zip", zip_members), source_root=None
    )
    assert any("cp312" in v or "CPython 312, not 313" in v for v in result.violations), (
        result.violations
    )


def test_a_darwin_extension_module_filename_fails(tmp_path: Path) -> None:
    members = {
        "crm/__init__.py": "",
        "crm/handlers/__init__.py": "",
        "crm/handlers/synthetic.py": "def handler(event, context):\n    return None\n",
        "ext/__init__.py": "",
        "ext/ext.cpython-313-darwin.so": "\x00binary",
    }
    result = gate.check_zip(
        SYNTHETIC_SPEC, _write_zip(tmp_path / "synthetic.zip", members), source_root=None
    )
    assert any("not x86_64 Linux" in v for v in result.violations), result.violations


def test_a_cpython_314_extension_module_fails(tmp_path: Path) -> None:
    members = {
        "crm/__init__.py": "",
        "crm/handlers/__init__.py": "",
        "crm/handlers/synthetic.py": "def handler(event, context):\n    return None\n",
        "ext/__init__.py": "",
        "ext/ext.cpython-314-x86_64-linux-gnu.so": "\x00binary",
    }
    result = gate.check_zip(
        SYNTHETIC_SPEC, _write_zip(tmp_path / "synthetic.zip", members), source_root=None
    )
    assert any("targets CPython 314" in v for v in result.violations), result.violations


def test_a_wheel_with_no_tag_line_is_a_violation_rather_than_a_pass(tmp_path: Path) -> None:
    members = {
        "crm/__init__.py": "",
        "crm/handlers/__init__.py": "",
        "crm/handlers/synthetic.py": "def handler(event, context):\n    return None\n",
        "mystery-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\n",
    }
    result = gate.check_zip(
        SYNTHETIC_SPEC, _write_zip(tmp_path / "synthetic.zip", members), source_root=None
    )
    assert any("declares no Tag: line" in v for v in result.violations), result.violations


# ---------------------------------------------------------------------------
# 4. Missing-zip behaviour must never read as a silent pass.
# ---------------------------------------------------------------------------


def test_an_empty_build_directory_skips_with_a_stated_reason_and_fails_under_require_zips(
    tmp_path: Path,
) -> None:
    status, report = gate.run(tmp_path, require_zips=False)
    assert status == gate.SKIP_EXIT_STATUS
    assert status != 0, "a skip must not share an exit status with a pass"
    assert report["status"] == "skip"
    assert "Nothing was verified" in str(report["reason"])

    status, report = gate.run(tmp_path, require_zips=True)
    assert status == 1
    assert report["status"] == "fail"


def test_a_partially_built_directory_always_fails(tmp_path: Path) -> None:
    """Some zips present and some absent is an incomplete build, never a partial pass."""
    first = gate.expected_zips()[0]
    _handler_zip(tmp_path / first.zip_name, vendor=True)
    status, report = gate.run(tmp_path, require_zips=False)
    assert status == 1
    assert report["status"] == "fail"
    assert "missing" in str(report["reason"])


# ---------------------------------------------------------------------------
# 6. Vendored-RECORD phase — a compiled distribution missing from the zip.
# ---------------------------------------------------------------------------

#: A `RECORD` for a compiled distribution: a pure-Python `__init__.py` plus an extension module,
#: shaped like the real `rpds_py-2026.6.3.dist-info/RECORD` (including the two entry classes a
#: correctly-built zip legitimately drops — a `../../bin/` console script and a `__pycache__` entry).
COMPILED_RECORD = (
    "vendormod/__init__.py,sha256=aaa,10\n"
    "vendormod/vendormod.cpython-313-x86_64-linux-gnu.so,sha256=bbb,20\n"
    "vendormod/__pycache__/__init__.cpython-313.pyc,,\n"
    "../../bin/vendorcli,sha256=ccc,30\n"
    "vendormod-1.0.dist-info/WHEEL,sha256=ddd,40\n"
    "vendormod-1.0.dist-info/RECORD,,\n"
)


def _compiled_vendor_zip(path: Path, *, body: bool, extension: bool) -> Path:
    """A zip whose handler imports `vendormod`, a *compiled* vendored distribution.

    `body`/`extension` control whether the package body and its `.so` are shipped; the `dist-info`
    (WHEEL + RECORD) is always present and correctly tagged.
    """
    members = {
        "crm/__init__.py": "",
        "crm/handlers/__init__.py": "",
        "crm/handlers/synthetic.py": (
            "from crm.support import helper\ndef handler(event, context):\n    return helper\n"
        ),
        "crm/support.py": "import vendormod\ndef helper():\n    return vendormod\n",
        "vendormod-1.0.dist-info/WHEEL": LINUX_WHEEL,
        "vendormod-1.0.dist-info/METADATA": _metadata("vendormod"),
        "vendormod-1.0.dist-info/RECORD": COMPILED_RECORD,
    }
    if body:
        members["vendormod/__init__.py"] = "VALUE = 1\n"
    if extension:
        members["vendormod/vendormod.cpython-313-x86_64-linux-gnu.so"] = "\x00binary"
    return _write_zip(path, members)


def test_a_compiled_vendored_distribution_present_in_full_passes(tmp_path: Path) -> None:
    """Control: a compiled distribution shipping everything its RECORD declares passes.

    The exempt `../../bin/` console script and `__pycache__` RECORD entries cause no false failure.
    """
    path = _compiled_vendor_zip(tmp_path / "synthetic.zip", body=True, extension=True)
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert result.violations == [], result.violations
    assert result.records_verified == 1


def test_a_vendored_distribution_whose_package_body_is_missing_fails(tmp_path: Path) -> None:
    """A distribution whose package body is missing while its `dist-info` remains fails.

    The closure walk stops at a vendored distribution's boundary, so only the RECORD cross-check
    sees this.
    """
    path = _compiled_vendor_zip(tmp_path / "synthetic.zip", body=False, extension=False)
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert result.violations, "a vendored distribution missing its whole body must be a violation"
    assert any("declares" in v and "missing from the zip" in v for v in result.violations), (
        result.violations
    )
    assert any("vendormod/__init__.py" in v for v in result.violations), result.violations
    assert result.records_verified == 0


def test_a_vendored_distribution_missing_only_its_compiled_leaf_fails(tmp_path: Path) -> None:
    """A distribution missing only its compiled `.so`, with its pure-Python body kept, fails."""
    path = _compiled_vendor_zip(tmp_path / "synthetic.zip", body=True, extension=False)
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert any("vendormod.cpython-313-x86_64-linux-gnu.so" in v for v in result.violations), (
        result.violations
    )


def test_a_dist_info_declaring_no_shippable_file_is_a_violation_not_a_pass(tmp_path: Path) -> None:
    """A RECORD containing only exempt entries is a violation rather than a pass."""
    members = {
        "crm/__init__.py": "",
        "crm/handlers/__init__.py": "",
        "crm/handlers/synthetic.py": "def handler(event, context):\n    return None\n",
        "vendormod-1.0.dist-info/WHEEL": LINUX_WHEEL,
        "vendormod-1.0.dist-info/RECORD": "../../bin/vendorcli,sha256=ccc,30\n",
    }
    result = gate.check_zip(
        SYNTHETIC_SPEC, _write_zip(tmp_path / "synthetic.zip", members), source_root=None
    )
    assert any("cannot be verified" in v for v in result.violations), result.violations


@pytest.mark.parametrize(
    ("case", "drop_suffixes"),
    [("E1_whole_package_body", ("",)), ("E1b_only_the_so", (".so",))],
)
def test_the_real_zip_with_a_vendored_distribution_stripped_fails(
    tmp_path: Path, case: str, drop_suffixes: tuple[str, ...]
) -> None:
    """A real `get_account.zip` rebuilt without `rpds/*` (or `rpds/*.so`), keeping its `dist-info`,
    fails.

    Skipped when no zip has been built.
    """
    source = REAL_BUILD_DIR / "get_account.zip"
    if not source.is_file():
        pytest.skip(f"no built {source.name}; run scripts/package_lambdas.sh to make this live")

    target = tmp_path / source.name
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dest:
        for info in src.infolist():
            if info.filename.startswith("rpds/") and any(
                info.filename.endswith(s) for s in drop_suffixes
            ):
                continue
            dest.writestr(info, src.read(info.filename))

    spec = next(s for s in gate.expected_zips() if s.zip_name == source.name)
    result = gate.check_zip(spec, target)
    assert any("rpds_py" in v and "missing from the zip" in v for v in result.violations), (
        case,
        result.violations,
    )


# ---------------------------------------------------------------------------
# 7. Marker-aware Requires-Dist phase — the typing-extensions defect class.
# ---------------------------------------------------------------------------


def test_the_marker_environment_is_the_lambda_runtime_and_not_the_build_host() -> None:
    """Markers are evaluated against the Lambda runtime's environment, not the build host's."""
    assert gate.LAMBDA_MARKER_ENVIRONMENT["python_version"] == "3.13"
    assert gate.LAMBDA_MARKER_ENVIRONMENT["sys_platform"] == "linux"
    assert gate.LAMBDA_MARKER_ENVIRONMENT["platform_machine"] == "x86_64"
    assert gate.TARGET_PYTHON_VERSION == (3, 13)


@pytest.mark.parametrize(
    ("marker", "required"),
    [
        ("", True),
        ('; python_version < "3.14"', True),
        ('; python_version >= "3.14"', False),
        ('; python_version < "3.13"', False),
        ('; python_version < "3.9"', False),
        ('; sys_platform == "linux"', True),
        ('; sys_platform == "win32"', False),
        ('; platform_machine == "x86_64"', True),
        ('; platform_machine == "aarch64"', False),
        ('; platform_python_implementation != "PyPy"', True),
        ('; extra == "crypto"', False),
    ],
)
def test_markers_are_evaluated_for_the_lambda_target(marker: str, required: bool) -> None:
    """Requires-Dist markers resolve to required or not required for the Lambda target."""
    metadata = _metadata("requirer", "1.0", f"someother>=1.0{marker}")
    resolved, unparseable = gate.marker_required_distributions(metadata)
    assert unparseable == []
    assert ("someother" in resolved) is required, (marker, resolved)


def test_a_requires_dist_in_the_description_body_is_not_read_as_a_requirement() -> None:
    """The header block ends at the first blank line; a changelog quoting its own metadata is prose."""
    metadata = (
        "Metadata-Version: 2.4\nName: requirer\nVersion: 1.0\n"
        "\nChangelog\n---------\nRequires-Dist: notreally>=1.0\n"
    )
    resolved, _ = gate.marker_required_distributions(metadata)
    assert resolved == []


def test_names_are_pep503_normalised_so_a_spelling_difference_is_not_a_false_failure() -> None:
    metadata = _metadata("requirer", "1.0", "Typing_Extensions>=4.4.0")
    resolved, _ = gate.marker_required_distributions(metadata)
    assert resolved == ["typing-extensions"]
    present = {"typing-extensions"}
    missing, _ = gate.resolve_missing_required_distributions(present, {"requirer": metadata})
    assert missing == []


def test_a_requirement_the_lambda_runtime_provides_is_not_reported_missing() -> None:
    metadata = _metadata("requirer", "1.0", "botocore>=1.0", "python-dateutil>=2.0")
    missing, _ = gate.resolve_missing_required_distributions(set(), {"requirer": metadata})
    assert missing == []


def test_a_zip_missing_a_marker_required_distribution_fails(tmp_path: Path) -> None:
    """A zip missing a distribution required under the 3.13 target's markers fails."""
    path = _handler_zip(
        tmp_path / "synthetic.zip",
        vendor=True,
        requires=('typing-extensions>=4.4.0; python_version < "3.14"',),
    )
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert result.violations, "a marker-required distribution absent from the zip must fail"
    assert any(
        "typing-extensions" in v and "required by vendored 'vendormod'" in v
        for v in result.violations
    ), result.violations
    assert any("python_version=3.13" in v for v in result.violations), result.violations
    # The host's own interpreter satisfies the import, so only the marker phase sees the gap.
    assert result.isolated_import_status.startswith("pass")


def test_a_zip_whose_marker_does_not_apply_to_the_target_does_not_false_fail(
    tmp_path: Path,
) -> None:
    """A dependency whose marker does not apply to the 3.13 Lambda target is not required."""
    path = _handler_zip(
        tmp_path / "synthetic.zip",
        vendor=True,
        requires=(
            'futuremod>=1.0; python_version >= "3.14"',
            'windowsmod>=1.0; sys_platform == "win32"',
            'armmod>=1.0; platform_machine == "aarch64"',
            'extrasmod>=1.0; extra == "docs"',
        ),
    )
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert result.violations == [], result.violations
    assert result.marker_requirements_resolved == 1


def test_a_satisfied_marker_requirement_present_in_the_zip_passes(tmp_path: Path) -> None:
    """Control case: the same conditional requirement, with the distribution actually vendored."""
    path = _handler_zip(
        tmp_path / "synthetic.zip",
        vendor=True,
        requires=('typing-extensions>=4.4.0; python_version < "3.13"',),
    )
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("typing_extensions.py", "TYPE = 1\n")
        archive.writestr("typing_extensions-4.16.0.dist-info/WHEEL", PURE_WHEEL)
        archive.writestr(
            "typing_extensions-4.16.0.dist-info/METADATA", _metadata("typing_extensions", "4.16.0")
        )
        archive.writestr("typing_extensions-4.16.0.dist-info/RECORD", "typing_extensions.py,,\n")
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert result.violations == [], result.violations
    assert result.marker_requirements_resolved == 2


def test_a_vendored_distribution_with_no_metadata_cannot_be_resolved_and_fails(
    tmp_path: Path,
) -> None:
    """A vendored distribution with no METADATA is a violation."""
    path = _handler_zip(tmp_path / "synthetic.zip", vendor=True)
    stripped = tmp_path / "stripped.zip"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(stripped, "w") as dest:
        for info in src.infolist():
            if info.filename.endswith(".dist-info/METADATA"):
                continue
            dest.writestr(info, src.read(info.filename))
    result = gate.check_zip(SYNTHETIC_SPEC, stripped, source_root=None)
    assert any("ships no dist-info/METADATA" in v for v in result.violations), result.violations


def test_an_unparseable_requires_dist_is_a_violation_rather_than_a_silent_skip(
    tmp_path: Path,
) -> None:
    path = _handler_zip(tmp_path / "synthetic.zip", vendor=True, requires=("!!! not a spec",))
    result = gate.check_zip(SYNTHETIC_SPEC, path, source_root=None)
    assert any("unparseable Requires-Dist" in v for v in result.violations), result.violations


def test_the_real_referencing_metadata_needs_no_typing_extensions_on_the_313_target() -> None:
    """`referencing`'s installed METADATA gates `typing-extensions` below 3.13, so the target
    does not require it.

    Skipped if `referencing` is not installed.
    """
    try:
        metadata_text = importlib.metadata.distribution("referencing").read_text("METADATA")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - not installed
        pytest.skip("referencing is not installed in this environment")
    assert metadata_text is not None
    resolved, unparseable = gate.marker_required_distributions(metadata_text)
    assert unparseable == []
    assert "typing-extensions" not in resolved, resolved


def test_the_real_built_eval_zip_vendors_typing_extensions() -> None:
    """The real built eval zip vendors `typing_extensions`, which `anyio` requires below 3.15."""
    zip_path = REAL_BUILD_DIR / "eval_trigger.zip"
    if not zip_path.is_file():
        pytest.skip(f"no built {zip_path.name}; run scripts/package_lambdas.sh to make this live")
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert "typing_extensions.py" in names, "the typing-extensions defect has regressed"
    assert any(
        n.startswith("typing_extensions-") and n.endswith(".dist-info/METADATA") for n in names
    ), [n for n in names if "typing" in n]


def test_the_real_built_eval_zip_with_typing_extensions_stripped_fails_the_gate(
    tmp_path: Path,
) -> None:
    """A real built zip with `typing_extensions` stripped fails the marker phase."""
    source = REAL_BUILD_DIR / "eval_trigger.zip"
    if not source.is_file():
        pytest.skip(f"no built {source.name}; run scripts/package_lambdas.sh to make this live")
    target = tmp_path / source.name
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dest:
        for info in src.infolist():
            if info.filename.startswith("typing_extensions"):
                continue
            dest.writestr(info, src.read(info.filename))
    spec = next(s for s in gate.expected_zips() if s.zip_name == source.name)
    result = gate.check_zip(spec, target)
    assert any(
        "typing-extensions" in v and "required by vendored 'anyio'" in v for v in result.violations
    ), result.violations


# ---------------------------------------------------------------------------
# 7b. Which interpreter the isolated import ran under, and the PEP 696 defect it catches.
# ---------------------------------------------------------------------------

NO_TARGET_INTERPRETER = gate.InterpreterDiscovery(None, ("test: deliberately none",))

#: `referencing/typing.py`'s PEP 696 construct: it prefers `typing_extensions.TypeVar`, falls back
#: to `typing.TypeVar`, and passes `default=`, which only CPython 3.13+'s `typing` accepts.
#: `test_the_embedded_referencing_reproduction_matches_the_real_vendored_module` pins it to the
#: real vendored file.
REFERENCING_TYPING_SOURCE = (
    "from __future__ import annotations\n"
    "\n"
    "from typing import TYPE_CHECKING, Any, Protocol\n"
    "\n"
    "try:\n"
    "    from typing_extensions import TypeVar\n"
    "except ImportError:  # pragma: no cover\n"
    "    from typing import TypeVar\n"
    "\n"
    'D = TypeVar("D", default=Any)\n'
    "\n"
    "\n"
    "class Retrieve(Protocol[D]):\n"
    "    def __call__(self, uri: str) -> D: ...\n"
)


def _pep696_zip(path: Path, *, vendor_typing_extensions: bool) -> Path:
    """A zip with vendored `referencing` present and `typing_extensions` optional.

    `referencing/__init__.py` is empty so the real package's compiled `rpds` import cannot decide
    the outcome.
    """
    members = {
        "crm/__init__.py": "",
        "crm/handlers/__init__.py": "",
        "crm/handlers/synthetic.py": (
            "import referencing.typing\ndef handler(event, context):\n"
            "    return referencing.typing.D\n"
        ),
        "referencing/__init__.py": "",
        "referencing/typing.py": REFERENCING_TYPING_SOURCE,
    }
    if vendor_typing_extensions:
        real = REAL_BUILD_DIR / "get_account.zip"
        with zipfile.ZipFile(real) as archive:
            members["typing_extensions.py"] = archive.read("typing_extensions.py").decode("utf-8")
    return _write_zip(path, members)


def _isolated_import_only(path: Path, discovery: Any) -> Any:
    """Run phase 5 alone against `path` under `discovery`, so nothing else colours the outcome."""
    result = gate.ZipResult(zip_name=path.name, handler=SYNTHETIC_SPEC.handler)
    gate.check_isolated_import(SYNTHETIC_SPEC, path, {}, result, discovery=discovery)
    return result


def test_the_embedded_referencing_reproduction_matches_the_real_vendored_module() -> None:
    """The embedded reproduction matches the real vendored `referencing/typing.py`."""
    zip_path = REAL_BUILD_DIR / "get_account.zip"
    if not zip_path.is_file():
        pytest.skip(f"no built {zip_path.name}; run scripts/package_lambdas.sh to make this live")
    with zipfile.ZipFile(zip_path) as archive:
        real = archive.read("referencing/typing.py").decode("utf-8")
    assert "try:\n    from typing_extensions import TypeVar\nexcept ImportError:" in real
    assert 'D = TypeVar("D", default=Any)' in real
    for line in REFERENCING_TYPING_SOURCE.splitlines():
        if line.strip().startswith(("D = TypeVar", "from typing_extensions", "except ImportError")):
            assert line in real, line


def test_the_same_zip_passes_on_the_313_build_host_which_is_why_the_outage_shipped(
    tmp_path: Path,
) -> None:
    """Without a 3.13 interpreter the same zip imports cleanly and reports a host status."""
    if sys.version_info[:2] == gate.TARGET_PYTHON_VERSION:
        pytest.skip("this host is itself CPython 3.13, so there is no non-target host to contrast")
    path = _pep696_zip(tmp_path / "pep696.zip", vendor_typing_extensions=False)
    result = _isolated_import_only(path, NO_TARGET_INTERPRETER)
    assert result.isolated_import_status == (
        f"pass_host_python{sys.version_info[0]}{sys.version_info[1]}"
    ), result.violations
    assert result.violations == []
    assert any("neither confirms nor denies" in n for n in result.notes), result.notes


# ---------------------------------------------------------------------------
# 7b-iii. The target-version substitute directory, and its version binding.
# ---------------------------------------------------------------------------


def test_a_substitute_at_a_different_version_than_the_zip_vendors_is_refused(
    tmp_path: Path,
) -> None:
    """A substitute is matched to the zip's vendored ``name-version`` and refused at any other."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "rpds_py-1.0.0.dist-info").mkdir()
    reason = gate._shadow_supplies(shadow, ["rpds_py-2026.6.3"])
    assert reason is not None
    assert "1.0.0" in reason and "2026.6.3" in reason
    assert "not the same pinned version" in reason

    (shadow / "rpds_py-2026.6.3.dist-info").mkdir()
    (shadow / "rpds_py-1.0.0.dist-info").rmdir()
    assert gate._shadow_supplies(shadow, ["rpds_py-2026.6.3"]) is None


def test_an_absent_substitute_directory_is_a_stated_reason_not_a_pass(tmp_path: Path) -> None:
    reason = gate._shadow_supplies(tmp_path / "nonexistent", ["rpds_py-2026.6.3"])
    assert reason is not None and "no target-version substitute directory exists" in reason


def test_the_print_target_python_mode_agrees_with_discovery(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--print-target-python` prints exactly the interpreter discovery finds."""
    discovery = gate.discover_target_interpreter()
    status = gate.main(["--print-target-python"])
    printed = capsys.readouterr().out.strip()
    if discovery.interpreter is None:
        assert status == 1
        assert printed == ""
    else:
        assert status == 0
        assert printed == discovery.interpreter.executable


# ---------------------------------------------------------------------------
# 7b-ii. Discovery: order, validation, and the refusal to fall back silently.
# ---------------------------------------------------------------------------


def test_the_discovery_order_is_env_override_then_path_then_uv_then_pyenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(gate.TARGET_PYTHON_ENV_VAR, "/nonexistent/python3.13")
    sources = [source for source, _ in gate._discovery_candidates()]
    assert sources == [
        f"env:{gate.TARGET_PYTHON_ENV_VAR}",
        "PATH:python3.13",
        "uv python find 3.13",
        "pyenv which python3.13",
    ]


def test_an_interpreter_reporting_another_minor_version_is_rejected_not_used(
    tmp_path: Path,
) -> None:
    """A candidate reporting CPython 3.12 is rejected as a target candidate for the 3.13 runtime."""
    fake = tmp_path / "python3.12"
    fake.write_text(
        '#!/bin/sh\necho \'{"version": [3, 12], "stdlib": ["/x", "/x", "/x/lib-dynload"]}\'\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    stdlib, description = gate._probe_interpreter(str(fake))
    assert stdlib is None
    assert "is CPython 3.12, not the target 3.13" in description


def test_the_host_interpreter_is_accepted_only_when_it_is_the_target_version() -> None:
    """Probing the host accepts it exactly when the host is the runtime's minor version."""
    stdlib, _ = gate._probe_interpreter(sys.executable)
    assert (stdlib is not None) is (sys.version_info[:2] == gate.TARGET_PYTHON_VERSION)


def test_a_candidate_that_cannot_be_executed_is_rejected_with_a_reason() -> None:
    stdlib, description = gate._probe_interpreter("/nonexistent/python3.13")
    assert stdlib is None
    assert "could not be executed" in description


def test_no_discoverable_interpreter_keeps_the_non_target_status_and_names_what_was_tried(
    tmp_path: Path,
) -> None:
    """With no discoverable 3.13 the phase still passes, keeps a non-target status, and names the
    candidates tried."""
    if sys.version_info[:2] == gate.TARGET_PYTHON_VERSION:
        pytest.skip("this host is itself CPython 3.13, which discovery always accepts")
    result = _isolated_import_only(
        _handler_zip(tmp_path / "synthetic.zip", vendor=True),
        gate.InterpreterDiscovery(None, ("env:PYTHON313: not found", "PATH:python3.13: not found")),
    )
    assert result.violations == []
    assert "target_python" not in result.isolated_import_status
    note = " ".join(result.notes)
    assert "PATH:python3.13: not found" in note
    assert gate.TARGET_PYTHON_ENV_VAR in note


def test_the_not_run_default_is_the_status_a_report_carries_before_the_phase_runs() -> None:
    """`ZipResult.isolated_import_status` defaults to `not_run`."""
    assert gate.ZipResult(zip_name="z", handler="m.h").isolated_import_status == "not_run"


def test_a_skipped_isolated_import_is_counted_as_skipped_and_never_as_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skipped isolated import reports `skip`, and the run summary counts it as skipped.

    `_HOST_MATCHES_LAMBDA` is patched so the skip path is reached on any host; the unresolvable
    top-level name makes `_resolve_host_native` decline.
    """
    monkeypatch.setattr(gate, "_HOST_MATCHES_LAMBDA", False)
    result = gate.ZipResult(zip_name="synthetic.zip", handler=SYNTHETIC_SPEC.handler)
    gate.check_isolated_import(
        SYNTHETIC_SPEC,
        _handler_zip(tmp_path / "synthetic.zip", vendor=True),
        {"rpds_py-2026.6.3": ["a_top_level_no_build_host_provides"]},
        result,
        discovery=gate.InterpreterDiscovery(None, ("env:PYTHON313: not found",)),
    )
    assert result.isolated_import_status == "skip"
    assert result.violations == []
    assert any("platform_mismatch_and_no_host_native_substitute" in n for n in result.notes)

    def _report(status: str) -> dict[str, object]:
        return {
            "status": "pass",
            "zips": [
                {
                    "zip": "synthetic.zip",
                    "handler": SYNTHETIC_SPEC.handler,
                    "violations": [],
                    "notes": [],
                    "modules_resolved_in_zip": 1,
                    "vendored_records_verified": 0,
                    "marker_requirements_resolved": 0,
                    "first_party_files_matched": 0,
                    "tracked_files_matched": 0,
                    "tracked_provenance": "pass",
                    "isolated_import": status,
                }
            ],
        }

    gate._print_human(_report(result.isolated_import_status))
    assert "explicitly skipped for 1" in capsys.readouterr().out

    # Control: a non-skip status is not counted as skipped.
    gate._print_human(_report("pass"))
    assert "explicitly skipped for 0" in capsys.readouterr().out


def test_the_report_records_the_discovered_interpreter_and_every_rejected_candidate(
    tmp_path: Path,
) -> None:
    _write_synthetic_build_dir(tmp_path)
    status, report = gate.run(tmp_path, require_zips=True, source_root=None)
    assert status == 0, report
    assert isinstance(report["target_python_discovery_attempts"], list)
    interpreter = report["target_python_interpreter"]
    if interpreter is not None:
        assert isinstance(interpreter, dict)
        assert set(interpreter) == {"executable", "discovered_via"}
        note = gate._run_level_note(report)
        assert note is not None
        assert "the PLATFORM class is " in note or "platform class is unexecuted" in note


def test_a_first_party_module_missing_from_the_zip_is_never_downgraded_to_a_substitute_gap(
    tmp_path: Path,
) -> None:
    """Only a shadowed compiled distribution failing to load downgrades a failure; a missing
    first-party or unvendored module stays a failure."""
    assert not gate._import_failed_on_substitute(
        "ModuleNotFoundError: No module named 'crm'", ["rpds"]
    )
    assert not gate._import_failed_on_substitute("ValueError: something else", ["rpds"])
    assert gate._import_failed_on_substitute(
        "ModuleNotFoundError: No module named 'rpds.rpds'", ["rpds"]
    )
    result = _isolated_import_only(
        _handler_zip(tmp_path / "synthetic.zip", vendor=False), gate.discover_target_interpreter()
    )
    assert result.isolated_import_status == "fail"
    assert any("vendormod" in v for v in result.violations), result.violations


def test_the_reports_json_states_both_the_host_and_the_target_python_version(
    tmp_path: Path,
) -> None:
    _write_synthetic_build_dir(tmp_path)
    # Synthetic zips with no counterpart on disk, so the source_parity phase has no tree to
    # compare against; the report fields under test are independent of it.
    status, report = gate.run(tmp_path, require_zips=True, source_root=None)
    assert status == 0, report
    assert report["target_python_version"] == "3.13"
    assert report["host_python_version"] == f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert isinstance(report["marker_environment"], dict)


# ---------------------------------------------------------------------------
# 7c. The build-driving resolution mode `scripts/package_lambdas.sh` loops on.
# ---------------------------------------------------------------------------


def test_the_missing_required_distributions_mode_names_what_the_target_still_needs(
    tmp_path: Path,
) -> None:
    """`--print-missing-required-distributions` names what the target still needs, then nothing."""
    (tmp_path / "requirer-1.0.dist-info").mkdir()
    (tmp_path / "requirer-1.0.dist-info" / "METADATA").write_text(
        _metadata(
            "requirer",
            "1.0",
            'typing-extensions>=4.4.0; python_version < "3.14"',
            'futuremod>=1.0; python_version >= "3.14"',
        ),
        encoding="utf-8",
    )
    result = proc_run.run(
        [
            sys.executable,
            str(GATE_PATH),
            "--print-missing-required-distributions",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.split() == ["typing-extensions"], result.stdout

    (tmp_path / "typing_extensions-4.16.0.dist-info").mkdir()
    (tmp_path / "typing_extensions-4.16.0.dist-info" / "METADATA").write_text(
        _metadata("typing_extensions", "4.16.0"), encoding="utf-8"
    )
    closed = proc_run.run(
        [
            sys.executable,
            str(GATE_PATH),
            "--print-missing-required-distributions",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert closed.stdout.strip() == "", closed.stdout


def test_the_missing_required_distributions_mode_rejects_a_nonexistent_directory(
    tmp_path: Path,
) -> None:
    result = proc_run.run(
        [
            sys.executable,
            str(GATE_PATH),
            "--print-missing-required-distributions",
            str(tmp_path / "absent"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 2, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 8. The skip disposition must be distinguishable from a pass in `run_gates.py`.
# ---------------------------------------------------------------------------

RUN_GATES = REPO_ROOT / "scripts" / "run_gates.py"


def _load_run_gates() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_gates_for_test", RUN_GATES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_gates_skip_status_is_the_one_run_gates_recognises() -> None:
    """The two constants must agree, or the skip silently reads as an ordinary failure or pass."""
    run_gates = _load_run_gates()
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS
    assert gate.SKIP_EXIT_STATUS != 0


def test_the_gate_process_really_exits_the_skip_status_on_an_empty_build_dir(
    tmp_path: Path,
) -> None:
    """Proven through the real process boundary, since that is what `run_gates.py` observes."""
    result = proc_run.run(
        [sys.executable, str(GATE_PATH), "--build-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == gate.SKIP_EXIT_STATUS, result.stdout + result.stderr
    assert "[SKIP]" in result.stdout
    assert "Nothing was verified" in result.stdout


def test_run_gates_renders_a_skipping_gate_as_skip_and_never_as_pass() -> None:
    """A gate exiting the skip status renders `[SKIP]` with its reason and is not counted as passed."""
    injected = (
        "GATE_OK\tsh -c 'echo fine; exit 0'\n"
        f"GATE_SKIPPED\tsh -c 'echo NOTHING_WAS_VERIFIED_REASON; exit {gate.SKIP_EXIT_STATUS}'\n"
    )
    result = proc_run.run(
        [sys.executable, str(RUN_GATES)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "CRM_RUN_GATES_INJECT": injected},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[SKIP] GATE_SKIPPED" in result.stdout, result.stdout
    assert "[PASS] GATE_SKIPPED" not in result.stdout, result.stdout
    assert "INCOMPLETE" in result.stdout, result.stdout
    assert "OK: 2/2 gates passed" not in result.stdout, result.stdout
    # The skip reason is echoed even on a non-failing run.
    assert "NOTHING_WAS_VERIFIED_REASON" in result.stdout, result.stdout


def test_run_gates_json_marks_a_skipping_gate_skipped_and_not_passed() -> None:
    injected = f"GATE_SKIPPED\tsh -c 'exit {gate.SKIP_EXIT_STATUS}'\n"
    result = proc_run.run(
        [sys.executable, str(RUN_GATES), "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "CRM_RUN_GATES_INJECT": injected},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    entries = json.loads(result.stdout)
    assert entries[0]["skipped"] is True
    assert entries[0]["passed"] is False


def test_run_gates_shows_the_zip_gate_as_skipped_when_no_zip_has_been_built(
    tmp_path: Path,
) -> None:
    """On an empty `LAMBDA_BUILD_DIR` the real zip gate renders `[SKIP]` with its reason, is not
    counted in an `OK: n/n gates passed` headline, and leaves the run at rc 0.

    The zip gate is injected as the run's only gate, so no other gate's state affects the outcome.
    """
    empty_build_dir = tmp_path / "empty_build"
    empty_build_dir.mkdir()
    injected = f"check_lambda_zip_imports\t{sys.executable} scripts/check_lambda_zip_imports.py\n"
    result = proc_run.run(
        [sys.executable, str(RUN_GATES)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        env={
            **os.environ,
            "LAMBDA_BUILD_DIR": str(empty_build_dir),
            "CRM_RUN_GATES_INJECT": injected,
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[SKIP] check_lambda_zip_imports" in result.stdout, result.stdout
    assert "[PASS] check_lambda_zip_imports" not in result.stdout, result.stdout
    assert "Nothing was verified" in result.stdout, result.stdout
    assert not re.search(r"^OK: \d+/\d+ gates passed", result.stdout, re.MULTILINE), result.stdout


# ---------------------------------------------------------------------------
# 8a. `if TYPE_CHECKING:` imports are not runtime dependencies; runtime imports still are.
# ---------------------------------------------------------------------------


def _targets_for(source: str) -> dict[str, bool]:
    """`{module: is_optional}` for every import `_import_targets` finds in `source`."""
    return dict(gate._import_targets(ast.parse(source), "crm.handlers.synthetic"))


class TestTypeCheckingImportsAreOptional:
    def test_a_type_only_import_is_marked_optional(self) -> None:
        """An import under `if TYPE_CHECKING:` is marked optional."""
        targets = _targets_for(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from typeonly.mod import Thing\n"
        )
        assert targets["typeonly.mod"] is True

    def test_the_fully_qualified_guard_is_recognised(self) -> None:
        targets = _targets_for("import typing\nif typing.TYPE_CHECKING:\n    import typeonly\n")
        assert targets["typeonly"] is True

    def test_a_type_only_import_is_not_reported_as_a_violation(self, tmp_path: Path) -> None:
        """End to end through the closure phase: the zip vendors nothing for `typeonly`."""
        path = _write_zip(
            tmp_path / "synthetic.zip",
            {
                "crm/__init__.py": "",
                "crm/handlers/__init__.py": "",
                "crm/handlers/synthetic.py": (
                    "from typing import TYPE_CHECKING\n"
                    "if TYPE_CHECKING:\n"
                    "    from typeonly.mod import Thing\n"
                    "def handler(event, context):\n    return None\n"
                ),
            },
        )
        result = gate.ZipResult(zip_name=path.name, handler=SYNTHETIC_SPEC.handler)
        with zipfile.ZipFile(path) as archive:
            gate.check_closure(SYNTHETIC_SPEC, gate.ZipModuleIndex(archive), result)
        assert result.violations == [], result.violations

    def test_the_same_module_imported_at_runtime_is_still_reported(self, tmp_path: Path) -> None:
        """The same module imported at runtime is still a violation; the exemption is per import
        node, not per module."""
        path = _write_zip(
            tmp_path / "synthetic.zip",
            {
                "crm/__init__.py": "",
                "crm/handlers/__init__.py": "",
                "crm/handlers/synthetic.py": (
                    "from typing import TYPE_CHECKING\n"
                    "if TYPE_CHECKING:\n"
                    "    from typeonly.mod import Thing\n"
                    "def handler(event, context):\n"
                    "    from typeonly.mod import Thing as Real\n"
                    "    return Real\n"
                ),
            },
        )
        result = gate.ZipResult(zip_name=path.name, handler=SYNTHETIC_SPEC.handler)
        with zipfile.ZipFile(path) as archive:
            gate.check_closure(SYNTHETIC_SPEC, gate.ZipModuleIndex(archive), result)
        assert any("typeonly" in v for v in result.violations), result.violations

    @pytest.mark.parametrize(
        "source",
        [
            "if not TYPE_CHECKING:\n    import runtimeonly\n",
            "if TYPE_CHECKING:\n    pass\nelse:\n    import runtimeonly\n",
            "if TYPE_CHECKING and other:\n    import runtimeonly\n",
            "if SOMETHING_ELSE:\n    import runtimeonly\n",
        ],
        ids=["negated", "else_branch", "composite_test", "unrelated_guard"],
    )
    def test_a_branch_that_does_execute_is_never_exempted(self, source: str) -> None:
        """A branch that executes at import time is never exempted."""
        assert _targets_for(source)["runtimeonly"] is False

    def test_the_try_except_importerror_exemption_still_works(self) -> None:
        """The `try/except ImportError` exemption is independent of the `TYPE_CHECKING` rule."""
        source = "try:\n    import optionaldep\nexcept ImportError:\n    optionaldep = None\n"
        assert _targets_for(source)["optionaldep"] is True


# ---------------------------------------------------------------------------
# 8b. Archive layout — the roots a zip puts on `sys.path`.
# ---------------------------------------------------------------------------


def _nested_zip(path: Path) -> Path:
    """A zip in `eval_trigger.zip`'s shape: handler under `src/`, a sibling package at the root."""
    return _write_zip(
        path,
        {
            "src/crm/__init__.py": "",
            "src/crm/handlers/__init__.py": "",
            "src/crm/handlers/synthetic.py": (
                "import fixtures\ndef handler(event, context):\n    return fixtures.VALUE\n"
            ),
            "fixtures/__init__.py": "VALUE = 1\n",
        },
    )


def test_a_nested_layout_resolves_a_handler_the_flat_layout_cannot_see(tmp_path: Path) -> None:
    """The declared nested layout resolves a `src/`-nested handler that the flat layout cannot."""
    path = _nested_zip(tmp_path / "nested.zip")

    flat = gate.ZipResult(zip_name=path.name, handler=SYNTHETIC_SPEC.handler)
    with zipfile.ZipFile(path) as archive:
        gate.check_closure(SYNTHETIC_SPEC, gate.ZipModuleIndex(archive), flat)
    assert any("is not present in the zip at all" in v for v in flat.violations), flat.violations

    nested_spec = gate.ZipSpec(path.name, SYNTHETIC_SPEC.handler, gate.EVAL_TRIGGER_LAYOUT)
    nested = gate.ZipResult(zip_name=path.name, handler=nested_spec.handler)
    with zipfile.ZipFile(path) as archive:
        index = gate.ZipModuleIndex(archive, import_roots=nested_spec.import_roots)
        gate.check_closure(nested_spec, index, nested)
    assert nested.violations == [], nested.violations
    # `fixtures` lives at the archive root, not under `src/`, so both roots are searched.
    assert nested.modules_resolved_in_zip >= 2


def test_a_nested_packages_members_are_attributed_to_their_own_root(tmp_path: Path) -> None:
    with zipfile.ZipFile(_nested_zip(tmp_path / "nested.zip")) as archive:
        roots = gate.ZipSpec("n.zip", SYNTHETIC_SPEC.handler, gate.EVAL_TRIGGER_LAYOUT).import_roots
        members = gate.ZipModuleIndex(archive, import_roots=roots).first_party_members()
    assert set(members) == {("src", "crm"), ("", "fixtures")}
    # Paths are root-relative, which is how the source tree behind each root is keyed.
    assert members[("src", "crm")] == [
        "crm/__init__.py",
        "crm/handlers/__init__.py",
        "crm/handlers/synthetic.py",
    ]
    assert members[("", "fixtures")] == ["fixtures/__init__.py"]


def test_source_parity_compares_a_nested_package_against_its_own_tree(tmp_path: Path) -> None:
    """Source parity compares a nested first-party package against its own tree, both directions."""
    path = _nested_zip(tmp_path / "nested.zip")
    tree = tmp_path / "tree"
    for relative in (
        "src/crm/__init__.py",
        "src/crm/handlers/__init__.py",
        "fixtures/__init__.py",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("" if "fixtures" not in relative else "VALUE = 1\n", encoding="utf-8")
    handler = tree / "src/crm/handlers/synthetic.py"
    handler.write_text(
        "import fixtures\ndef handler(event, context):\n    return fixtures.VALUE\n",
        encoding="utf-8",
    )

    def parity(source_root: Path) -> Any:
        result = gate.ZipResult(zip_name=path.name, handler=SYNTHETIC_SPEC.handler)
        with zipfile.ZipFile(path) as archive:
            gate.check_source_parity(
                gate.ZipModuleIndex(archive, import_roots=("src", "")),
                result,
                source_root=source_root,
                package_roots=gate.EVAL_TRIGGER_LAYOUT,
            )
        return result

    matched = parity(tree / "src")
    assert matched.violations == [], matched.violations
    assert matched.first_party_files_matched == 4, matched.first_party_files_matched

    # The other direction: a byte difference in the NESTED package must be reported, naming its
    # in-zip path rather than its root-relative one.
    handler.write_text("def handler(event, context):\n    return None\n", encoding="utf-8")
    drifted = parity(tree / "src")
    assert len(drifted.violations) == 1, drifted.violations
    assert "'src/crm/handlers/synthetic.py'" in drifted.violations[0], drifted.violations


def test_the_isolated_import_puts_every_declared_root_on_the_child_path(tmp_path: Path) -> None:
    """The child's `sys.path` holds the archive root plus each declared root; with only the archive
    root the nested handler is unimportable."""
    path = _nested_zip(tmp_path / "nested.zip")
    discovery = gate.discover_target_interpreter()

    nested_spec = gate.ZipSpec(path.name, SYNTHETIC_SPEC.handler, gate.EVAL_TRIGGER_LAYOUT)
    nested = gate.ZipResult(zip_name=path.name, handler=nested_spec.handler)
    gate.check_isolated_import(nested_spec, path, {}, nested, discovery=discovery)
    assert nested.isolated_import_status.startswith("pass"), nested.violations

    flat = gate.ZipResult(zip_name=path.name, handler=SYNTHETIC_SPEC.handler)
    gate.check_isolated_import(SYNTHETIC_SPEC, path, {}, flat, discovery=discovery)
    assert flat.isolated_import_status == "fail", flat.notes


# ---------------------------------------------------------------------------
# 9. The real built zips, when they exist.
# ---------------------------------------------------------------------------


def test_the_real_build_directory_satisfies_the_gate_when_it_has_been_built() -> None:
    """Whatever the last build produced satisfies the gate.

    Skipped when no zip has been built.
    """
    specs = gate.expected_zips()
    if not any((REAL_BUILD_DIR / s.zip_name).is_file() for s in specs):
        pytest.skip(
            f"no built Lambda zip under {REAL_BUILD_DIR}; run scripts/package_lambdas.sh "
            "(needs network for pip) to make this assertion live"
        )
    status, report = gate.run(REAL_BUILD_DIR, require_zips=True)
    assert status == 0, report


# ---------------------------------------------------------------------------
# 10. source_parity — the zip's first-party content matches the source tree.
# ---------------------------------------------------------------------------


def _parity_source_tree(root: Path, files: dict[str, str]) -> Path:
    """Write `files` (in-zip paths) under `root` as a stand-in source tree."""
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


_PARITY_FILES = {
    "crm/__init__.py": "",
    "crm/handlers/__init__.py": "",
    "crm/handlers/synthetic.py": "def handler(event, context):\n    return None\n",
}


def _parity_violations(
    tmp_path: Path, *, zip_files: dict[str, str], source_files: dict[str, str]
) -> list[str]:
    """Run only the source_parity phase over a synthetic zip and a synthetic source tree."""
    path = _write_zip(tmp_path / "synthetic.zip", zip_files)
    source_root = _parity_source_tree(tmp_path / "src", source_files)
    result = gate.ZipResult(zip_name="synthetic.zip", handler=SYNTHETIC_SPEC.handler)
    with zipfile.ZipFile(path) as archive:
        gate.check_source_parity(gate.ZipModuleIndex(archive), result, source_root=source_root)
    return [str(violation) for violation in result.violations]


def test_a_zip_whose_first_party_content_matches_the_tree_passes(tmp_path: Path) -> None:
    """Control: without this every mutation below could pass for an unrelated reason."""
    assert _parity_violations(tmp_path, zip_files=_PARITY_FILES, source_files=_PARITY_FILES) == []


def test_a_zip_carrying_an_older_copy_of_a_first_party_module_fails(tmp_path: Path) -> None:
    """A zip carrying an older copy of a first-party module than the tree fails."""
    changed = dict(_PARITY_FILES)
    changed["crm/handlers/synthetic.py"] = (
        "import requests\n\n\ndef handler(event, context):\n    return None\n"
    )

    violations = _parity_violations(tmp_path, zip_files=_PARITY_FILES, source_files=changed)

    assert any("differs from" in v and "synthetic.py" in v for v in violations), violations


def test_a_first_party_file_added_to_the_tree_but_absent_from_the_zip_fails(
    tmp_path: Path,
) -> None:
    """A first-party file present in the tree and absent from the zip fails."""
    grown = {**_PARITY_FILES, "crm/handlers/added.py": "VALUE = 1\n"}

    violations = _parity_violations(tmp_path, zip_files=_PARITY_FILES, source_files=grown)

    assert any("absent from the zip" in v and "added.py" in v for v in violations), violations


def test_a_first_party_module_in_the_zip_that_the_tree_no_longer_has_fails(
    tmp_path: Path,
) -> None:
    """A deleted module still shipping means the zip was built from a different tree."""
    shrunk = {k: v for k, v in _PARITY_FILES.items() if k != "crm/handlers/synthetic.py"}
    shrunk["crm/handlers/other.py"] = "def handler(event, context):\n    return None\n"

    violations = _parity_violations(tmp_path, zip_files=_PARITY_FILES, source_files=shrunk)

    assert any("does not exist under" in v and "synthetic.py" in v for v in violations), violations


def test_bytecode_caches_are_not_compared_since_the_build_never_ships_them(
    tmp_path: Path,
) -> None:
    """`package_lambdas.sh` excludes `__pycache__`/`*.pyc`, so their absence is not staleness."""
    with_cache = {**_PARITY_FILES, "crm/handlers/__pycache__/synthetic.cpython-313.pyc": "x"}

    assert _parity_violations(tmp_path, zip_files=_PARITY_FILES, source_files=with_cache) == []


def test_the_source_root_default_is_the_repository_src_tree() -> None:
    """`check_zip`'s `source_root` defaults to the repository `src/` tree."""
    import inspect

    assert inspect.signature(gate.check_zip).parameters["source_root"].default == gate.SRC_DIR


def test_the_real_zips_carry_this_repositorys_current_first_party_source() -> None:
    """The real built zips carry the repository's current first-party source.

    Skipped when no zip has been built.
    """
    specs = gate.expected_zips()
    built = [s for s in specs if (REAL_BUILD_DIR / s.zip_name).is_file()]
    if not built:
        pytest.skip(
            f"no built Lambda zip under {REAL_BUILD_DIR}; run scripts/package_lambdas.sh "
            "(needs network for pip) to make this assertion live"
        )
    for spec in built:
        result = gate.ZipResult(zip_name=spec.zip_name, handler=spec.handler)
        with zipfile.ZipFile(REAL_BUILD_DIR / spec.zip_name) as archive:
            gate.check_source_parity(
                gate.ZipModuleIndex(archive, import_roots=spec.import_roots),
                result,
                source_root=gate.SRC_DIR,
                package_roots=spec.package_roots,
            )
        assert result.violations == [], (spec.zip_name, result.violations)
        assert result.first_party_files_matched > 0, spec.zip_name


def test_deploy_sh_runs_the_zip_gate_before_terraform_apply() -> None:
    """`scripts/deploy.sh` runs the zip gate before `terraform apply`, and a red gate fails the
    deploy."""
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    check_at = text.find("check_lambda_zip_imports.py --require-zips")
    # Matches the apply invocation line itself.
    apply_at = text.find('terraform apply "${TF_APPLY_ARGS[@]}"')
    assert check_at != -1, "scripts/deploy.sh does not run the zip gate at all"
    assert apply_at != -1, "scripts/deploy.sh no longer runs terraform apply"
    assert check_at < apply_at, "the zip gate must run before terraform apply, not after"
    assert "|| die" in text[check_at : check_at + 400], (
        "a failing zip gate must fail the deploy, not warn"
    )


# ---------------------------------------------------------------------------
# 12. The closure allowance: unvendored SDK roots reached only through the two adapters.
# ---------------------------------------------------------------------------

ADAPTER_ALLOWANCE_SPEC = gate.ZipSpec(
    "sdk.zip",
    SYNTHETIC_SPEC.handler,
    gate.EVAL_TRIGGER_LAYOUT,
    gate.EVAL_ADAPTER_SDK_ALLOWANCE,
)


def _adapter_zip(path: Path, overrides: dict[str, str] | None = None) -> Path:
    """A nested-layout zip shaped like eval_trigger's adapter path: handler -> factory (lazy) -> adapter.

    The adapter imports its SDK and the shared transport, which imports httpx2; nothing is vendored.
    `overrides` replaces members' source, keyed by archive path.
    """
    members = {
        "src/crm/__init__.py": "",
        "src/crm/handlers/__init__.py": "",
        "src/crm/handlers/synthetic.py": (
            "def handler(event, context):\n"
            "    from providers.factory import build\n"
            "    return build()\n"
        ),
        "src/providers/__init__.py": "",
        "src/providers/factory.py": (
            "def build():\n"
            "    from providers.anthropic_adapter import Adapter\n"
            "    return Adapter\n"
        ),
        "src/providers/anthropic_adapter.py": (
            "import anthropic\nfrom anthropic.types import Event\nimport httpx2\n"
            "from providers.sdk_transport import client\nAdapter = client\n"
        ),
        "src/providers/sdk_transport.py": "import httpx2\nclient = httpx2\n",
    }
    return _write_zip(path, {**members, **(overrides or {})})


def _closure_only(spec: Any, path: Path) -> Any:
    result = gate.ZipResult(zip_name=spec.zip_name, handler=spec.handler)
    with zipfile.ZipFile(path) as archive:
        gate.check_closure(spec, gate.ZipModuleIndex(archive, spec.import_roots), result)
    return result


def test_the_allowance_names_exactly_the_two_adapters_and_the_three_sdk_roots() -> None:
    allowance = gate.EVAL_ADAPTER_SDK_ALLOWANCE
    assert allowance.importers == {"providers.anthropic_adapter", "providers.openai_adapter"}
    assert allowance.roots == {"anthropic", "openai", "httpx2"}


#: Handlers served from `eval_trigger.zip` that reach no adapter, so carry no allowance.
_EVAL_ZIP_HANDLERS_WITHOUT_ALLOWANCE = frozenset({"crm.handlers.user_settings.handler"})


def test_only_eval_trigger_zip_carries_the_allowance_and_only_where_an_adapter_is_reachable() -> (
    None
):
    for spec in gate.expected_zips():
        expected = (
            gate.EVAL_ADAPTER_SDK_ALLOWANCE
            if spec.zip_name == "eval_trigger.zip"
            and spec.handler not in _EVAL_ZIP_HANDLERS_WITHOUT_ALLOWANCE
            else None
        )
        assert spec.unvendored_allowance is expected, spec
    handlers = {spec.handler for spec in gate.expected_zips()}
    assert handlers >= _EVAL_ZIP_HANDLERS_WITHOUT_ALLOWANCE


def test_sdk_roots_reached_only_through_an_adapter_are_accepted(tmp_path: Path) -> None:
    result = _closure_only(ADAPTER_ALLOWANCE_SPEC, _adapter_zip(tmp_path / "sdk.zip"))
    assert result.violations == []
    assert sorted(result.allowed_unvendored) == ["anthropic", "httpx2"]


def test_the_same_zip_without_the_allowance_fails(tmp_path: Path) -> None:
    """The control that makes the acceptance above evidence: the allowance is what turns it green."""
    spec = gate.ZipSpec("sdk.zip", SYNTHETIC_SPEC.handler, gate.EVAL_TRIGGER_LAYOUT)
    result = _closure_only(spec, _adapter_zip(tmp_path / "sdk.zip"))
    assert any("'anthropic'" in v for v in result.violations), result.violations
    assert any("'httpx2'" in v for v in result.violations), result.violations
    assert result.allowed_unvendored == []


def test_an_sdk_root_imported_by_another_module_is_not_allowed(tmp_path: Path) -> None:
    path = _adapter_zip(
        tmp_path / "sdk.zip",
        {
            "src/crm/handlers/synthetic.py": (
                "import openai\ndef handler(event, context):\n    return openai\n"
            )
        },
    )
    result = _closure_only(ADAPTER_ALLOWANCE_SPEC, path)
    assert any("'openai' (imported by 'crm.handlers.synthetic')" in v for v in result.violations), (
        result.violations
    )


def test_a_root_outside_the_allowance_imported_by_an_adapter_is_not_allowed(
    tmp_path: Path,
) -> None:
    path = _adapter_zip(
        tmp_path / "sdk.zip",
        {
            "src/providers/anthropic_adapter.py": (
                "import anthropic\nimport requests\nfrom providers.sdk_transport import client\n"
                "Adapter = client\n"
            )
        },
    )
    result = _closure_only(ADAPTER_ALLOWANCE_SPEC, path)
    assert any("'requests'" in v for v in result.violations), result.violations
    assert "anthropic" in result.allowed_unvendored


def test_a_transport_module_that_something_else_imports_loses_the_allowance(
    tmp_path: Path,
) -> None:
    """`sdk_transport` qualifies only while the adapters alone import it."""
    path = _adapter_zip(
        tmp_path / "sdk.zip",
        {
            "src/crm/handlers/synthetic.py": (
                "from providers.sdk_transport import client\n"
                "def handler(event, context):\n"
                "    from providers.factory import build\n"
                "    return build(), client\n"
            )
        },
    )
    result = _closure_only(ADAPTER_ALLOWANCE_SPEC, path)
    assert any("'httpx2'" in v for v in result.violations), result.violations
    assert result.allowed_unvendored == ["anthropic"]


def test_the_real_eval_zip_serves_user_settings_with_no_unvendored_import() -> None:
    """`user_settings`' closure in the built zip needs no allowance."""
    source = REAL_BUILD_DIR / "eval_trigger.zip"
    if not source.is_file():
        pytest.skip(f"no built {source.name}; run scripts/package_lambdas.sh to make this live")
    (spec,) = [s for s in gate.expected_zips() if s.handler in _EVAL_ZIP_HANDLERS_WITHOUT_ALLOWANCE]
    assert spec.unvendored_allowance is None
    result = _closure_only(spec, source)
    assert result.violations == [], result.violations
    assert result.allowed_unvendored == []


def test_the_real_eval_zip_accepts_exactly_the_adapter_sdk_roots() -> None:
    source = REAL_BUILD_DIR / "eval_trigger.zip"
    if not source.is_file():
        pytest.skip(f"no built {source.name}; run scripts/package_lambdas.sh to make this live")
    for spec in (s for s in gate.expected_zips() if s.zip_name == source.name):
        result = _closure_only(spec, source)
        assert result.violations == [], (spec.handler, result.violations)
        assert set(result.allowed_unvendored) <= gate.EVAL_ADAPTER_SDK_ALLOWANCE.roots, (
            spec.handler,
            result.allowed_unvendored,
        )


def _compiled_handler_zip(path: Path, *, with_extension: bool) -> Path:
    """A zip whose handler imports the host venv's real `rpds`, with or without its compiled `.so`."""
    found = importlib.util.find_spec("rpds")
    assert found is not None and found.submodule_search_locations
    package_dir = Path(list(found.submodule_search_locations)[0])
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("crm/__init__.py", "")
        archive.writestr("crm/handlers/__init__.py", "")
        archive.writestr(
            "crm/handlers/synthetic.py",
            "import rpds\ndef handler(event, context):\n    return rpds.HashTrieMap()\n",
        )
        for file in sorted(package_dir.rglob("*")):
            if not file.is_file() or "__pycache__" in file.parts:
                continue
            if file.suffix in {".so", ".pyd"} and not with_extension:
                continue
            archive.write(file, f"rpds/{file.relative_to(package_dir).as_posix()}")
    return path


def _host_discovery() -> Any:
    return gate.InterpreterDiscovery(
        gate.TargetInterpreter(sys.executable, "host interpreter", gate._host_stdlib_paths()), ()
    )


@pytest.mark.parametrize("with_extension", [True, False], ids=["extension", "no-extension"])
def test_a_host_matching_lambda_imports_a_vendored_compiled_extension_from_the_extracted_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_extension: bool
) -> None:
    """On a Lambda-matching host the zip's own compiled extension loads, as the runtime loads it.

    The control without the `.so` must still fail, so the pass is the extension's, not a host leak.
    """
    if sys.version_info[:2] != gate.TARGET_PYTHON_VERSION:
        pytest.skip("the host venv's rpds extension must match the target interpreter version")
    monkeypatch.setattr(gate, "_HOST_MATCHES_LAMBDA", True)
    path = _compiled_handler_zip(tmp_path / "synthetic.zip", with_extension=with_extension)
    result = gate.ZipResult(zip_name=path.name, handler=SYNTHETIC_SPEC.handler)
    gate.check_isolated_import(
        SYNTHETIC_SPEC, path, {"rpds_py-0": ["rpds"]}, result, discovery=_host_discovery()
    )
    if with_extension:
        assert result.violations == []
        assert result.isolated_import_status.startswith("pass")
    else:
        assert result.isolated_import_status == "fail"
        assert any("rpds" in violation for violation in result.violations)
