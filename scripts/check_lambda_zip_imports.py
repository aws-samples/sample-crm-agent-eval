#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prove every built Lambda zip can satisfy its own handler's import closure.

Usage:
    python3 scripts/check_lambda_zip_imports.py [--build-dir DIR] [--require-zips] [--json]

Checks each zip `scripts/package_lambdas.sh` builds for a Lambda function -- the action-tool zips
derived from `crm.toolspec.definitions.ALL_TOOLS`, plus `identity_interceptor.zip`,
`pre_token_generation.zip`, `chat_target.zip`, and `eval_trigger.zip` under each handler the infra
serves from it (`eval_trigger`, `capabilities`, `user_settings`, and the two `eval.sfn_handlers` sweep
handlers). `eval_trigger.zip`
and `chat_target.zip` nest their first-party packages under ``src/`` (reached at runtime through
``PYTHONPATH=/var/task/src``); every other zip puts them at the archive root. `ZipSpec.package_roots`
describes the nested layout (:data:`EVAL_TRIGGER_LAYOUT`). The checks run in seven phases:

1. ``closure`` -- resolve the handler module's transitive import graph against the zip's own
   contents, allowing the standard library and the runtime-provided boto3/botocore closure. In
   ``eval_trigger.zip`` the vendor-SDK roots imported only from the two model adapters may be
   unvendored (:data:`EVAL_ADAPTER_SDK_ALLOWANCE`).
2. ``vendored_record`` -- every file a vendored distribution's ``*.dist-info/RECORD`` lists is
   present in the zip.
3. ``wheel_platform`` -- every vendored wheel tag and compiled extension ABI tag targets the Lambda
   platform (``manylinux``/``linux_x86_64``, CPython 3.13 or ``abi3``).
4. ``marker_requires`` -- every vendored ``Requires-Dist`` whose environment marker holds for the
   Lambda environment (:data:`LAMBDA_MARKER_ENVIRONMENT`) names a distribution the zip carries or
   the runtime provides.
5. ``isolated_import`` -- import the handler module in a subprocess whose ``sys.path`` is the zip
   plus a sandbox of the runtime-provided distributions, under a discovered CPython 3.13 when one
   exists (``PYTHON313``, ``python3.13`` on ``PATH``, ``uv python find 3.13``, ``pyenv which
   python3.13``) and otherwise under the build host's interpreter. The status suffix names which:
   ``pass…_target_python313`` or ``pass…_host_python<XY>``. Compiled distributions that cannot load
   on the build host are substituted from ``infra/build/target-python-shadow`` at the zip's own
   vendored version; ``target_python313`` names the interpreter version, never the platform. On a
   build host that matches the Lambda platform the zip is extracted and imported from that
   directory, as the runtime does, so its own compiled extensions load.
6. ``source_parity`` -- every first-party module in the zip is byte-identical to the same path
   under ``src/``, and no shippable first-party file the zip's packages should carry is absent.
   It compares against the WORKING TREE, which includes untracked and uncommitted files.
7. ``tracked_provenance`` -- every first-party module in the zip is tracked in the git index and
   byte-identical to its tracked content, compared by git blob id. An artifact built from untracked
   or uncommitted source is unreproducible by construction.

A ``tracked_provenance`` mismatch is classified into one of :data:`PROVENANCE_CLASSES` by comparing
the member against both the git index (:data:`INDEX_STATES`) and the working tree
(:data:`WORKING_TREE_STATES`):

============================================== ============ ================================ =================================
class                                          vs git index working tree                     fix
============================================== ============ ================================ =================================
``untracked_code_baked_in``                    absent       readable, equals the zip         commit, then repackage
``untracked_and_stale``                        absent       readable, differs from the zip   commit AND repackage
``untracked_and_unreadable_in_working_tree``   absent       present but unreadable           repair, then commit and repackage
``untracked_and_absent_from_working_tree``     absent       absent                           restore and commit, or repackage
``packaged_over_uncommitted_edit``             differs      readable, equals the zip         commit, then repackage
``stale_artifact``                             differs      readable, equals the index       repackage
``stale_artifact_over_uncommitted_edit``       differs      readable, equals neither         commit FIRST, then repackage
``tracked_and_unreadable_in_working_tree``     differs      present but unreadable           repair, then repackage
``stale_and_absent_from_working_tree``         differs      absent                           restore, or repackage to drop
============================================== ============ ================================ =================================

When git cannot answer, the phase reports ``tracked_provenance=unavailable`` with a note.

Exit status is ``0`` only when no phase reports a violation. When no expected zip exists the
script reports ``SKIP`` and exits ``SKIP_EXIT_STATUS`` (3), or ``1`` with ``--require-zips``. When
some expected zips exist it fails on the missing ones.

``--print-missing-required-distributions DIR`` prints, one per line, the distributions the Lambda
target environment requires that a pip ``--target`` directory does not contain;
``scripts/package_lambdas.sh`` loops on it to close its vendored set. ``--print-target-python``
prints the discovered CPython 3.13.

Requires no AWS credential and makes no network call.
"""

from __future__ import annotations

import argparse
import ast
import functools
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

SRC_DIR = REPO_ROOT / "src"

#: Timeout for each git call the tracked_provenance phase makes.
_GIT_TIMEOUT_SECONDS = 15

#: Environment variables that redirect which repository or index git reads; dropped for git calls.
_GIT_DISCOVERY_OVERRIDES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

#: Default build directory, matching `scripts/package_lambdas.sh`'s default.
DEFAULT_BUILD_DIR = REPO_ROOT / "infra" / "build" / "lambda"

#: Exit status when no expected zip exists and `--require-zips` was not given; equals
#: `scripts/run_gates.py`'s `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS = 3

#: Top-level modules the Lambda Python 3.13 runtime provides: boto3 and its dependency closure.
RUNTIME_PROVIDED_MODULES = frozenset(
    {
        "boto3",
        "botocore",
        "dateutil",
        "jmespath",
        "s3transfer",
        "six",
        "urllib3",
    }
)

#: Top-level packages of this repository's own source; the closure walk recurses only through these.
FIRST_PARTY_PACKAGES = frozenset({"crm", "gateway", "agents", "providers", "eval", "fixtures"})

#: Wheel python tag and platform-tag prefixes the Lambda python3.13 x86_64 runtime can load.
TARGET_PYTHON_TAG = "cp313"
TARGET_PLATFORM_PREFIXES = ("manylinux", "linux_x86_64", "linux_i686")

#: The Lambda runtime's `(major, minor)` Python version.
TARGET_PYTHON_VERSION = (3, 13)

#: PEP 508 environment of the Lambda python3.13 x86_64 runtime, used to evaluate ``Requires-Dist``
#: markers; ``extra`` is empty, so extras-only dependencies are not required.
LAMBDA_MARKER_ENVIRONMENT: dict[str, str] = {
    "extra": "",
    "implementation_name": "cpython",
    "implementation_version": "3.13.0",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.13.0",
    "python_version": "3.13",
    "sys_platform": "linux",
}

#: PEP 503 normalised distribution names the Lambda python3.13 runtime provides.
RUNTIME_PROVIDED_DISTRIBUTIONS = frozenset(
    {
        "boto3",
        "botocore",
        "jmespath",
        "python-dateutil",
        "s3transfer",
        "setuptools",
        "six",
        "urllib3",
    }
)

#: Extension-module suffixes that mark compiled content in a zip.
EXTENSION_SUFFIXES = (".so", ".pyd", ".dylib")

_EXTENSION_ABI_PATTERN = re.compile(r"\.cpython-(\d+)-([^.]+)\.(?:so|pyd)$")


#: Layout of a zip whose first-party packages sit at the archive root and come from ``src/``.
FLAT_LAYOUT: tuple[tuple[str, str], ...] = (("", "src"),)

#: Layout of ``eval_trigger.zip`` and ``chat_target.zip``: first-party packages under ``src/`` (on
#: ``PYTHONPATH=/var/task/src``), and ``fixtures/`` at the archive root beside ``config/``.
EVAL_TRIGGER_LAYOUT: tuple[tuple[str, str], ...] = (("src", "src"), ("", "."))


@dataclass(frozen=True)
class UnvendoredImportAllowance:
    """Top-level modules a zip may leave unvendored when imported only from `importers`.

    "Only from" is measured on the closure walk itself: a module counts as reached only through the
    importers when every module that imports it is an importer or is itself reached only through them.
    A root that any other module imports is reported as usual.
    """

    importers: frozenset[str]
    roots: frozenset[str]


#: Model SDK roots `eval_trigger.zip` may leave unvendored when only the two model adapters import
#: them (gated by `scripts/check_lambda_adapter_isolation.py`).
EVAL_ADAPTER_SDK_ALLOWANCE = UnvendoredImportAllowance(
    importers=frozenset({"providers.anthropic_adapter", "providers.openai_adapter"}),
    roots=frozenset({"anthropic", "openai", "httpx2"}),
)


@dataclass
class ZipSpec:
    """One expected zip: its file name, the module its declared handler lives in, and its layout.

    `package_roots` pairs each in-zip directory that is on ``sys.path`` at runtime with the
    repository-relative directory whose content the first-party packages under it must equal, in
    ``sys.path`` order. Several specs share one `zip_name` when one archive serves several handlers.
    `unvendored_allowance` is the closure phase's declared exception, ``None`` for most zips.
    """

    zip_name: str
    handler: str
    package_roots: tuple[tuple[str, str], ...] = FLAT_LAYOUT
    unvendored_allowance: UnvendoredImportAllowance | None = None

    @property
    def module(self) -> str:
        """The module portion of a `module.path.function` Lambda handler string."""
        return self.handler.rsplit(".", 1)[0]

    @property
    def import_roots(self) -> tuple[str, ...]:
        """The in-zip directories that are on ``sys.path``, in search order."""
        return tuple(root for root, _ in self.package_roots)


#: Every value :attr:`ZipResult.tracked_provenance_status` can hold.
TRACKED_PROVENANCE_STATUSES: tuple[str, ...] = (
    "not_run",
    "not_applicable",
    "unavailable",
    "fail",
    "pass",
)

# The only spellings of the statuses in this module; order matters.
(
    PROVENANCE_NOT_RUN,
    PROVENANCE_NOT_APPLICABLE,
    PROVENANCE_UNAVAILABLE,
    PROVENANCE_FAIL,
    PROVENANCE_PASS,
) = TRACKED_PROVENANCE_STATUSES

#: Base values of :attr:`ZipResult.isolated_import_status`; :func:`_annotate_pass` extends ``pass``.
ISOLATED_IMPORT_STATUSES: tuple[str, ...] = ("not_run", "skip", "fail", "pass")

# The only spellings of the statuses in this module; order matters.
IMPORT_NOT_RUN, IMPORT_SKIP, IMPORT_FAIL, IMPORT_PASS = ISOLATED_IMPORT_STATUSES


@dataclass
class ZipResult:
    """Per-zip outcome across all seven phases."""

    zip_name: str
    handler: str
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    modules_resolved_in_zip: int = 0
    #: Unvendored modules the zip's `unvendored_allowance` accepted, by name.
    allowed_unvendored: list[str] = field(default_factory=list)
    records_verified: int = 0
    marker_requirements_resolved: int = 0
    first_party_files_matched: int = 0
    tracked_files_matched: int = 0
    #: One of :data:`TRACKED_PROVENANCE_STATUSES`.
    tracked_provenance_status: str = PROVENANCE_NOT_RUN
    #: Names of the `PROVENANCE_CLASSES` this zip tripped.
    tracked_provenance_classes: list[str] = field(default_factory=list)
    #: One of :data:`ISOLATED_IMPORT_STATUSES`; a passing one names the interpreter that ran.
    isolated_import_status: str = IMPORT_NOT_RUN

    @property
    def passed(self) -> bool:
        return not self.violations


def expected_zips() -> list[ZipSpec]:
    """Derive the full expected zip inventory from the toolspec source of truth.

    The action-tool zips come from `crm.toolspec.definitions.ALL_TOOLS`, plus the non-tool Lambda
    zips and every handler the infra serves from `eval_trigger.zip`.
    """
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from crm.toolspec.definitions import ALL_TOOLS

    specs = [ZipSpec(f"{tool.name}.zip", tool.handler) for tool in ALL_TOOLS]
    specs.append(ZipSpec("identity_interceptor.zip", "gateway.identity_interceptor.lambda_handler"))
    specs.append(
        ZipSpec("pre_token_generation.zip", "crm.auth.pre_token_generation.lambda_handler")
    )
    specs.append(
        ZipSpec(
            "eval_trigger.zip",
            "crm.handlers.eval_trigger.handler",
            EVAL_TRIGGER_LAYOUT,
            EVAL_ADAPTER_SDK_ALLOWANCE,
        )
    )
    specs.append(
        ZipSpec(
            "eval_trigger.zip",
            "crm.handlers.capabilities.handler",
            EVAL_TRIGGER_LAYOUT,
            EVAL_ADAPTER_SDK_ALLOWANCE,
        )
    )
    specs.append(
        ZipSpec(
            "eval_trigger.zip",
            "crm.handlers.user_settings.handler",
            EVAL_TRIGGER_LAYOUT,
        )
    )
    specs += [
        ZipSpec(
            "eval_trigger.zip",
            f"eval.sfn_handlers.{handler}",
            EVAL_TRIGGER_LAYOUT,
            EVAL_ADAPTER_SDK_ALLOWANCE,
        )
        for handler in ("sweep_control_handler", "sweep_task_handler")
    ]
    specs.append(
        ZipSpec("chat_target.zip", "gateway.chat_target.lambda_handler", EVAL_TRIGGER_LAYOUT)
    )
    return specs


# ---------------------------------------------------------------------------
# Phase 1: import-closure resolution against the zip's own contents
# ---------------------------------------------------------------------------


def _in_zip(root: str, relative: str) -> str:
    """The in-zip member path for `relative` under importable `root`."""
    return f"{root}/{relative}" if root else relative


def _in_zip_path(zip_path: Path, root: str) -> str:
    """The `sys.path` entry that makes `root` inside `zip_path` importable.

    `zipimport` accepts a path *into* an archive, so a zip whose packages sit under ``src/`` is put on
    the path as ``<archive>/src`` -- the same entry the deployed function gets from
    ``PYTHONPATH=/var/task/src``.
    """
    return f"{zip_path}/{root}" if root else str(zip_path)


class ZipModuleIndex:
    """Resolves dotted module names against the members of one zip archive.

    `import_roots` are the in-zip directories the archive puts on ``sys.path`` at runtime, in search
    order; the default is the flat layout.
    """

    def __init__(self, archive: zipfile.ZipFile, import_roots: tuple[str, ...] = ("",)) -> None:
        self._names = set(archive.namelist())
        self._archive = archive
        self._import_roots = import_roots
        # Longest first, so `src/crm/...` is attributed to the `src` root rather than to `""`.
        self._roots_longest_first = tuple(sorted(set(import_roots), key=len, reverse=True))

    def extension_members(self) -> list[str]:
        return sorted(n for n in self._names if n.endswith(EXTENSION_SUFFIXES))

    def wheel_metadata_members(self) -> list[str]:
        return sorted(n for n in self._names if n.endswith(".dist-info/WHEEL"))

    def record_members(self) -> list[str]:
        return sorted(n for n in self._names if n.endswith(".dist-info/RECORD"))

    def metadata_members(self) -> list[str]:
        return sorted(n for n in self._names if n.endswith(".dist-info/METADATA"))

    def distribution_names(self) -> set[str]:
        """PEP 503 normalised name of every distribution with a top-level ``dist-info``."""
        names: set[str] = set()
        for member in self._names:
            head = member.split("/", 1)[0]
            if head.endswith(".dist-info"):
                names.add(normalise_distribution_name(head[: -len(".dist-info")].rsplit("-", 1)[0]))
        return names

    def contains(self, member: str) -> bool:
        return member in self._names

    def platform_specific_distributions(self) -> dict[str, list[str]]:
        """Map each vendored distribution built for a specific platform to its top-level packages.

        Read from each ``*.dist-info/WHEEL`` tag (platform-specific when the platform component is
        not ``any``) and the matching ``RECORD``. The ``isolated_import`` phase substitutes these on
        a non-Lambda build host.
        """
        distributions: dict[str, list[str]] = {}
        for wheel_member in self.wheel_metadata_members():
            dist_info = wheel_member.rsplit("/", 1)[0]
            tags = [
                line.split(":", 1)[1].strip()
                for line in self.read_source(wheel_member).splitlines()
                if line.lower().startswith("tag:")
            ]
            if all(part.split("-")[-1] == "any" for tag in tags for part in tag.split(".")):
                continue
            record = f"{dist_info}/RECORD"
            if record not in self._names:
                continue
            top_levels = set()
            for line in self.read_source(record).splitlines():
                first = line.split(",", 1)[0].split("/", 1)[0]
                if not first or first.startswith("..") or first.endswith(".dist-info"):
                    continue
                if first in {"bin", "__pycache__"}:
                    continue
                stem = first.split(".", 1)[0]
                if stem:
                    top_levels.add(stem)
            dist_dir = dist_info.rsplit("/", 1)[-1]
            if dist_dir.endswith(".dist-info"):
                dist_dir = dist_dir[: -len(".dist-info")]
            distributions[dist_dir] = sorted(top_levels)
        return distributions

    def resolve(self, module: str) -> tuple[str, str] | None:
        """Return ``(kind, member)`` for `module` inside the zip, or ``None`` if absent.

        ``kind`` is ``"source"`` for a ``.py`` module or package ``__init__.py`` (parseable, so the
        walk recurses into it), or ``"extension"`` for a compiled extension module (an opaque leaf).
        """
        bases = [_in_zip(root, module.replace(".", "/")) for root in self._import_roots]
        for base in bases:
            for candidate in (f"{base}.py", f"{base}/__init__.py"):
                if candidate in self._names:
                    return ("source", candidate)
        for name in self._names:
            if not name.endswith(EXTENSION_SUFFIXES):
                continue
            stem = name.rsplit("/", 1)[-1].split(".", 1)[0]
            parent = name.rsplit("/", 1)[0] if "/" in name else ""
            reconstructed = f"{parent}/{stem}" if parent else stem
            if reconstructed in bases:
                return ("extension", name)
        return None

    def read_source(self, member: str) -> str:
        return self._archive.read(member).decode("utf-8")

    def read_bytes(self, member: str) -> bytes:
        return self._archive.read(member)

    def first_party_members(self) -> dict[tuple[str, str], list[str]]:
        """Map each ``(importable root, first-party top-level package)`` to its members.

        Member paths are relative to their root. Directory entries and bytecode caches are excluded.
        """
        members: dict[tuple[str, str], list[str]] = {}
        for name in sorted(self._names):
            if name.endswith("/") or name.endswith(".pyc") or "__pycache__/" in name:
                continue
            for root in self._roots_longest_first:
                if root and not name.startswith(f"{root}/"):
                    continue
                relative = name[len(root) + 1 :] if root else name
                top = relative.split("/", 1)[0]
                if top in FIRST_PARTY_PACKAGES:
                    members.setdefault((root, top), []).append(relative)
                break
        return members


def _is_type_checking_test(test: ast.expr) -> bool:
    """True for the exact guard ``TYPE_CHECKING`` / ``typing.TYPE_CHECKING`` and nothing else.

    ``if not TYPE_CHECKING:`` and any composite test are runtime branches.
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _import_targets(tree: ast.AST, package: str) -> list[tuple[str, bool]]:
    """Collect ``(absolute_module, is_optional)`` for every import in `tree`.

    Imports at every nesting level are collected. An import is optional when it sits inside a
    ``try`` that catches ``ImportError``/``ModuleNotFoundError`` or inside the body (not the
    ``else``) of an ``if TYPE_CHECKING:`` block; marking is per AST node.
    """
    optional_nodes: set[int] = set()

    def _mark_optional(body: list[ast.stmt]) -> None:
        for body_node in body:
            for inner in ast.walk(body_node):
                optional_nodes.add(id(inner))

    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            if _is_type_checking_test(node.test):
                _mark_optional(node.body)
            continue
        if not isinstance(node, ast.Try):
            continue
        handled = False
        for handler in node.handlers:
            names: list[str] = []
            if isinstance(handler.type, ast.Name):
                names = [handler.type.id]
            elif isinstance(handler.type, ast.Tuple):
                names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
            if {"ImportError", "ModuleNotFoundError"} & set(names):
                handled = True
        if not handled:
            continue
        _mark_optional(node.body)

    targets: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        optional = id(node) in optional_nodes
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((alias.name, optional))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                trimmed = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(trimmed)
                absolute = f"{base}.{node.module}" if node.module else base
            else:
                absolute = node.module or ""
            if not absolute:
                continue
            targets.append((absolute, optional))
            # `from pkg import name` may name a submodule; record that form too.
            for alias in node.names:
                if alias.name != "*":
                    targets.append((f"{absolute}.{alias.name}", optional))
    return targets


def _is_satisfied_elsewhere(module: str) -> bool:
    """True when `module`'s top-level package is stdlib or provided by the Lambda runtime."""
    top = module.split(".")[0]
    if top == "__future__" or top in sys.stdlib_module_names:
        return True
    return top in RUNTIME_PROVIDED_MODULES


def _reached_only_through(
    importers_of: dict[str, set[str]], resolved: set[str], allowance: UnvendoredImportAllowance
) -> set[str]:
    """First-party modules the walk reaches only through `allowance.importers`, the importers included.

    A fixed point: a module joins when every module importing it is already in the set. A module
    something else imports, or the handler module itself (which nothing imports), never joins.
    """
    through = {m for m in allowance.importers if m in resolved}
    changed = True
    while changed:
        changed = False
        for module in resolved - through:
            parents = importers_of.get(module, set())
            if parents and parents <= through:
                through.add(module)
                changed = True
    return through


def check_closure(spec: ZipSpec, index: ZipModuleIndex, result: ZipResult) -> None:
    """Walk `spec.module`'s transitive imports, recording every unresolvable module."""
    root = index.resolve(spec.module)
    if root is None:
        result.violations.append(
            f"declared handler module {spec.module!r} is not present in the zip at all "
            f"(handler={spec.handler})"
        )
        return

    seen: set[str] = set()
    first_party_resolved: set[str] = set()
    importers_of: dict[str, set[str]] = {}
    unresolved: dict[str, str] = {}
    pending: list[tuple[str, str | None]] = [(spec.module, None)]
    while pending:
        module, importer = pending.pop()
        if importer is not None:
            importers_of.setdefault(module, set()).add(importer)
        if module in seen:
            continue
        seen.add(module)
        found = index.resolve(module)
        if found is None:
            if _is_satisfied_elsewhere(module):
                continue
            # An unresolved `from pkg import name` form is an attribute when its parent resolves.
            parent = module.rsplit(".", 1)[0] if "." in module else ""
            if parent and (index.resolve(parent) is not None or _is_satisfied_elsewhere(parent)):
                continue
            unresolved[module] = importer or spec.module
            continue
        kind, member = found
        result.modules_resolved_in_zip += 1
        if kind != "source":
            continue
        if module.split(".")[0] not in FIRST_PARTY_PACKAGES:
            # Vendored distributions are not recursed into.
            continue
        first_party_resolved.add(module)
        try:
            tree = ast.parse(index.read_source(member), filename=member)
        except SyntaxError as exc:  # pragma: no cover - a corrupt zip member
            result.violations.append(f"member {member!r} does not parse: {exc}")
            continue
        package = module if member.endswith("/__init__.py") else module.rsplit(".", 1)[0]
        for target, optional in _import_targets(tree, package):
            if optional:
                continue
            pending.append((target, module))

    allowance = spec.unvendored_allowance
    through = (
        _reached_only_through(importers_of, first_party_resolved, allowance)
        if allowance is not None
        else set()
    )

    # Report only the shallowest unresolved module per chain.
    for module in sorted(unresolved):
        parent = module.rsplit(".", 1)[0] if "." in module else ""
        if parent and parent in unresolved:
            continue
        if (
            allowance is not None
            and module.split(".")[0] in allowance.roots
            and importers_of.get(module, {unresolved[module]}) <= through
        ):
            result.allowed_unvendored.append(module)
            continue
        result.violations.append(
            f"module {module!r} (imported by {unresolved[module]!r}) is neither vendored in the "
            "zip nor provided by the Lambda python3.13 runtime"
        )
    if result.allowed_unvendored:
        if allowance is None:
            raise RuntimeError("unvendored modules were allowed without a declared allowance")
        result.notes.append(
            f"closure accepted {len(result.allowed_unvendored)} unvendored module(s) under the "
            f"declared allowance for {sorted(allowance.importers)}: "
            f"{result.allowed_unvendored}"
        )


# ---------------------------------------------------------------------------
# Phase 2: every vendored distribution ships the files its own RECORD declares
# ---------------------------------------------------------------------------


def _record_entry_is_expected_in_zip(path: str) -> bool:
    """True when a ``RECORD`` entry names a file the zip must carry.

    Excluded: ``../``-prefixed paths outside the ``--target`` tree, ``bin/``, and bytecode caches.
    """
    if not path or path.startswith(".."):
        return False
    if path.endswith(".pyc") or "__pycache__/" in path:
        return False
    return path.split("/", 1)[0] != "bin"


def check_vendored_records(index: ZipModuleIndex, result: ZipResult) -> None:
    """Fail when a vendored distribution's ``RECORD`` declares files the zip does not contain.

    Catches a distribution whose ``dist-info`` ships without its package body or extension, which
    the ``isolated_import`` phase's substitute would otherwise satisfy. Platform-independent.
    """
    for record_member in index.record_members():
        dist_info = record_member.rsplit("/", 1)[0]
        declared = [
            line.split(",", 1)[0].strip()
            for line in index.read_source(record_member).splitlines()
            if line.strip()
        ]
        expected = [p for p in declared if _record_entry_is_expected_in_zip(p)]
        absent = sorted(p for p in expected if not index.contains(p))
        if not expected:
            result.violations.append(
                f"{record_member!r} declares no installed file the zip should carry, so the "
                "vendored distribution's contents cannot be verified"
            )
            continue
        if absent:
            shown = absent[:5]
            suffix = f" (+{len(absent) - len(shown)} more)" if len(absent) > len(shown) else ""
            result.violations.append(
                f"vendored distribution {dist_info.rsplit('/', 1)[-1]!r} declares "
                f"{len(expected)} installed file(s) in RECORD but {len(absent)} are missing from "
                f"the zip: {shown}{suffix}"
            )
        else:
            result.records_verified += 1


# ---------------------------------------------------------------------------
# Phase 3: vendored wheels and compiled extensions must target the Lambda platform
# ---------------------------------------------------------------------------


def check_wheel_platforms(index: ZipModuleIndex, result: ZipResult) -> None:
    """Fail on any vendored wheel or extension module built for a non-Lambda platform."""
    for member in index.wheel_metadata_members():
        tags = [
            line.split(":", 1)[1].strip()
            for line in index.read_source(member).splitlines()
            if line.lower().startswith("tag:")
        ]
        if not tags:
            result.violations.append(
                f"{member!r} declares no Tag: line, so its platform is unknown"
            )
            continue
        if not any(_tag_targets_lambda(tag) for tag in tags):
            result.violations.append(
                f"{member!r} declares only non-Lambda wheel tag(s) {tags!r}; expected a "
                f"{TARGET_PYTHON_TAG}/abi3 wheel for {'/'.join(TARGET_PLATFORM_PREFIXES)}"
            )

    for member in index.extension_members():
        match = _EXTENSION_ABI_PATTERN.search(member)
        if match is None:
            # abi3 / untagged extension: its platform is checked through its wheel's Tag: line.
            continue
        major_minor, abi_platform = match.groups()
        if major_minor != "313":
            result.violations.append(f"extension {member!r} targets CPython {major_minor}, not 313")
        if not any(abi_platform.startswith(p) for p in ("x86_64-linux", "linux")):
            result.violations.append(
                f"extension {member!r} targets platform {abi_platform!r}, not x86_64 Linux"
            )


def _tag_targets_lambda(tag: str) -> bool:
    """True when a wheel tag (``py3-none-any``, ``cp313-abi3-manylinux…``) can load on Lambda."""
    for single in tag.split("."):
        parts = single.split("-")
        if len(parts) != 3:
            continue
        python_tag, abi_tag, platform_tag = parts
        if platform_tag == "any":
            return True
        if not any(platform_tag.startswith(p) for p in TARGET_PLATFORM_PREFIXES):
            continue
        if python_tag == TARGET_PYTHON_TAG or abi_tag == "abi3" or python_tag.startswith("py3"):
            return True
    return False


# ---------------------------------------------------------------------------
# Phase 4: Requires-Dist closure, with every marker evaluated for the Lambda target
# ---------------------------------------------------------------------------


def normalise_distribution_name(name: str) -> str:
    """PEP 503 normalisation: separators collapse to ``-`` and case is insignificant."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _metadata_header(text: str) -> list[str]:
    """The RFC 822 header block of a ``METADATA`` file, stopping at the first blank line."""
    lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            break
        lines.append(line)
    return lines


def marker_required_distributions(metadata_text: str) -> tuple[list[str], list[str]]:
    """Resolve one ``METADATA``'s requirements for the Lambda target.

    Returns ``(required, unparseable)``: the PEP 503 normalised names whose ``Requires-Dist`` marker
    evaluates true under :data:`LAMBDA_MARKER_ENVIRONMENT` (or which carry no marker at all), and
    the raw specifier strings that could not be parsed.
    """
    required: list[str] = []
    unparseable: list[str] = []
    for line in _metadata_header(metadata_text):
        if not line.lower().startswith("requires-dist:"):
            continue
        raw = line.split(":", 1)[1].strip()
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            unparseable.append(raw)
            continue
        if requirement.marker is not None and not requirement.marker.evaluate(
            LAMBDA_MARKER_ENVIRONMENT
        ):
            continue
        required.append(normalise_distribution_name(requirement.name))
    return required, unparseable


def resolve_missing_required_distributions(
    present: set[str], metadata: dict[str, str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Cross-check a set of installed distributions against their own marker-resolved requirements.

    `present` is PEP 503 normalised distribution names; `metadata` maps a requiring distribution's
    normalised name to its ``METADATA`` text. Returns ``(missing, unparseable)`` where `missing` is
    ``(requirer, missing_distribution)`` pairs, sorted and de-duplicated.
    """
    satisfied = present | RUNTIME_PROVIDED_DISTRIBUTIONS
    missing: set[tuple[str, str]] = set()
    unparseable: list[tuple[str, str]] = []
    for requirer, text in sorted(metadata.items()):
        required, bad = marker_required_distributions(text)
        unparseable.extend((requirer, raw) for raw in bad)
        for name in required:
            if name not in satisfied:
                missing.add((requirer, name))
    return sorted(missing), unparseable


def scan_target_directory(directory: Path) -> tuple[set[str], dict[str, str]]:
    """Read ``(present_distribution_names, metadata_by_distribution)`` from a pip ``--target`` tree."""
    present: set[str] = set()
    metadata: dict[str, str] = {}
    for dist_info in sorted(directory.glob("*.dist-info")):
        name = normalise_distribution_name(dist_info.name[: -len(".dist-info")].rsplit("-", 1)[0])
        present.add(name)
        metadata_path = dist_info / "METADATA"
        if metadata_path.is_file():
            metadata[name] = metadata_path.read_text(encoding="utf-8", errors="replace")
    return present, metadata


def check_marker_required_distributions(index: ZipModuleIndex, result: ZipResult) -> None:
    """Fail when the Lambda target environment requires a distribution the zip does not carry.

    Markers are evaluated against :data:`LAMBDA_MARKER_ENVIRONMENT`, not the build host.
    """
    present = index.distribution_names()
    metadata: dict[str, str] = {}
    for member in index.metadata_members():
        dist_dir = member.rsplit("/", 1)[0].split("/")[0]
        name = normalise_distribution_name(dist_dir[: -len(".dist-info")].rsplit("-", 1)[0])
        metadata[name] = index.read_source(member)
    for dist_name in sorted(present - set(metadata)):
        result.violations.append(
            f"vendored distribution {dist_name!r} ships no dist-info/METADATA, so its own "
            "dependency requirements for the Lambda target cannot be resolved"
        )
    missing, unparseable = resolve_missing_required_distributions(present, metadata)
    for requirer, raw in unparseable:
        result.violations.append(
            f"vendored distribution {requirer!r} declares an unparseable Requires-Dist {raw!r}, so "
            "its requirement for the Lambda target cannot be resolved"
        )
    for requirer, name in missing:
        result.violations.append(
            f"distribution {name!r} is required by vendored {requirer!r} for the Lambda target "
            f"environment (python_version={LAMBDA_MARKER_ENVIRONMENT['python_version']}, "
            f"sys_platform={LAMBDA_MARKER_ENVIRONMENT['sys_platform']}, "
            f"platform_machine={LAMBDA_MARKER_ENVIRONMENT['platform_machine']}) but is neither "
            "vendored in the zip nor provided by the Lambda python3.13 runtime"
        )
    if not missing and not unparseable:
        result.marker_requirements_resolved = len(metadata)


# ---------------------------------------------------------------------------
# Phase 5: really import the handler with only the zip + runtime-provided modules on sys.path
# ---------------------------------------------------------------------------

_HOST_MATCHES_LAMBDA = (
    sys.platform.startswith("linux")
    and platform.machine() in {"x86_64", "AMD64"}
    and sys.version_info[:2] == TARGET_PYTHON_VERSION
)

#: True when the build host's CPython minor version is not the Lambda runtime's.
_HOST_PYTHON_DIFFERS = sys.version_info[:2] != TARGET_PYTHON_VERSION

#: Environment variable naming an interpreter for the ``isolated_import`` phase, tried first and
#: validated like every other candidate.
TARGET_PYTHON_ENV_VAR = "PYTHON313"

_TARGET_PROBE_PROGRAM = (
    "import json, sys, sysconfig\n"
    "paths = sysconfig.get_paths()\n"
    "print(json.dumps({'version': [sys.version_info[0], sys.version_info[1]], "
    "'stdlib': [paths['stdlib'], paths['platstdlib'], paths['stdlib'] + '/lib-dynload']}))\n"
)

#: Run inside a candidate interpreter to locate, by importing, the packages a shadow needs.
_RESOLVE_PROGRAM = (
    "import importlib, json, sys\n"
    "resolved = {}\n"
    "for name in json.loads(sys.argv[1]):\n"
    "    try:\n"
    "        module = importlib.import_module(name)\n"
    "    except BaseException:\n"
    "        continue\n"
    "    path = list(getattr(module, '__path__', []) or [])\n"
    "    location = path[0] if path else getattr(module, '__file__', None)\n"
    "    if location:\n"
    "        resolved[name] = location\n"
    "print(json.dumps(resolved))\n"
)


@dataclass(frozen=True)
class TargetInterpreter:
    """A validated CPython 3.13 the ``isolated_import`` phase can execute under."""

    executable: str
    discovered_via: str
    stdlib_paths: tuple[str, ...]


@dataclass(frozen=True)
class InterpreterDiscovery:
    """The outcome of looking for a target interpreter, including every candidate rejected."""

    interpreter: TargetInterpreter | None
    attempts: tuple[str, ...]

    def describe_attempts(self) -> str:
        return "; ".join(self.attempts) if self.attempts else "no candidate was tried"


def _host_stdlib_paths() -> tuple[str, ...]:
    paths = sysconfig.get_paths()
    return (
        paths["stdlib"],
        paths["platstdlib"],
        str(Path(paths["stdlib"]) / "lib-dynload"),
    )


def _run_capture(command: list[str], *, env: dict[str, str] | None = None) -> str | None:
    """Run `command`, returning its last stdout line, or ``None`` if it could not be run cleanly.

    Only the last line is returned, as a tool such as ``uv python find`` may print warnings first.
    """
    try:
        completed = proc_run.run(
            command, capture_output=True, text=True, check=False, timeout=60, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _candidate_from_tool(command: list[str]) -> str | None:
    """Ask a version manager for an interpreter path, skipping cleanly when it is not installed."""
    if shutil.which(command[0]) is None:
        return None
    candidate = _run_capture(command)
    if candidate is None or not Path(candidate).is_file():
        return None
    return candidate


def _probe_interpreter(executable: str) -> tuple[tuple[str, ...] | None, str]:
    """Validate `executable` is the target CPython; return ``(stdlib_paths, description)``.

    ``stdlib_paths`` is ``None`` -- with a description saying why -- whenever the candidate cannot
    be executed, does not report a readable version, or reports a version other than the target.
    """
    raw = _run_capture([executable, "-c", _TARGET_PROBE_PROGRAM])
    if raw is None:
        return None, f"{executable} could not be executed"
    try:
        probe = json.loads(raw)
        version = tuple(int(part) for part in probe["version"])
        stdlib = tuple(str(part) for part in probe["stdlib"])
    except (KeyError, TypeError, ValueError):
        return None, f"{executable} produced unreadable version output {raw!r}"
    if version != TARGET_PYTHON_VERSION:
        return None, (
            f"{executable} is CPython {version[0]}.{version[1]}, not the target "
            f"{TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]}"
        )
    return stdlib, f"{executable} is CPython {version[0]}.{version[1]}"


def _discovery_candidates() -> list[tuple[str, str | None]]:
    """Ordered ``(source, candidate_path)`` pairs to try, most explicit first.

    An operator override, then ``PATH``, then ``uv`` and ``pyenv`` when installed.
    """
    return [
        (f"env:{TARGET_PYTHON_ENV_VAR}", os.environ.get(TARGET_PYTHON_ENV_VAR) or None),
        ("PATH:python3.13", shutil.which("python3.13")),
        ("uv python find 3.13", _candidate_from_tool(["uv", "python", "find", "3.13"])),
        ("pyenv which python3.13", _candidate_from_tool(["pyenv", "which", "python3.13"])),
    ]


@functools.cache
def discover_target_interpreter() -> InterpreterDiscovery:
    """Find a CPython 3.13 to run the ``isolated_import`` phase under, or report why none was found.

    The build host interpreter is accepted only when it is itself the target version; every
    candidate tried is recorded in `attempts`.
    """
    if not _HOST_PYTHON_DIFFERS:
        return InterpreterDiscovery(
            TargetInterpreter(sys.executable, "host interpreter", _host_stdlib_paths()),
            (
                f"host interpreter: accepted — {sys.executable} is CPython "
                f"{sys.version_info[0]}.{sys.version_info[1]}",
            ),
        )
    attempts: list[str] = []
    for source, candidate in _discovery_candidates():
        if not candidate:
            attempts.append(f"{source}: not found")
            continue
        stdlib, description = _probe_interpreter(candidate)
        if stdlib is None:
            attempts.append(f"{source}: rejected — {description}")
            continue
        attempts.append(f"{source}: accepted — {description}")
        return InterpreterDiscovery(TargetInterpreter(candidate, source, stdlib), tuple(attempts))
    return InterpreterDiscovery(None, tuple(attempts))


@functools.cache
def _resolve_in_interpreter(
    executable: str, names: tuple[str, ...], extra_path: str = ""
) -> tuple[tuple[str, str], ...] | None:
    """Locate every name in `names` inside `executable`'s own environment, or ``None``.

    `extra_path` is prepended to the child's ``PYTHONPATH`` so a substitute directory (see
    :func:`target_shadow_dir`) can supply compiled distributions. Returns ``None`` unless every name
    resolves.
    """
    if not names:
        return ()
    env = dict(os.environ)
    if extra_path:
        env["PYTHONPATH"] = (
            f"{extra_path}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else extra_path
        )
    raw = _run_capture([executable, "-c", _RESOLVE_PROGRAM, json.dumps(list(names))], env=env)
    if raw is None:
        return None
    try:
        resolved = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(resolved, dict) or set(resolved) != set(names):
        return None
    located: list[tuple[str, str]] = []
    for name in sorted(names):
        location = resolved[name]
        if not isinstance(location, str) or not Path(location).exists():
            return None
        located.append((name, location))
    return tuple(located)


#: Verification-only directory `scripts/package_lambdas.sh` populates with the zips' pinned
#: distributions built for CPython 3.13 at the build host's platform; never packaged or deployed.
DEFAULT_TARGET_SHADOW_DIR = REPO_ROOT / "infra" / "build" / "target-python-shadow"


def target_shadow_dir() -> Path:
    return Path(os.environ.get("LAMBDA_TARGET_SHADOW_DIR", str(DEFAULT_TARGET_SHADOW_DIR)))


def _shadow_supplies(shadow: Path, distributions: list[str]) -> str | None:
    """``None`` when `shadow` carries every ``name-version`` in `distributions`, else why it cannot.

    `distributions` are the zip's own ``<name>-<version>`` dist-info stems; versions must match.
    """
    if not shadow.is_dir():
        return f"no target-version substitute directory exists at {shadow}"
    present: dict[str, str] = {}
    for path in shadow.glob("*.dist-info"):
        stem = path.name[: -len(".dist-info")]
        name, _, version = stem.rpartition("-")
        if name:
            present[normalise_distribution_name(name)] = version
    for distribution in distributions:
        name, _, version = distribution.rpartition("-")
        key = normalise_distribution_name(name)
        if key not in present:
            return f"{shadow} carries no {name} built for the target interpreter version"
        if present[key] != version:
            return (
                f"{shadow} carries {name} {present[key]} but the zip vendors {version}, so it is "
                "not the same pinned version and must not be substituted"
            )
    return None


def _annotate_pass(base: str, *, ran_on_target: bool) -> str:
    """Fold *which interpreter actually ran* into a passing ``isolated_import`` status.

    Returns ``…_target_python313`` when it ran on the target version, ``…_host_python<XY>`` when it
    ran on a different host version, and the bare base otherwise.
    """
    if ran_on_target:
        return f"{base}_target_python{TARGET_PYTHON_VERSION[0]}{TARGET_PYTHON_VERSION[1]}"
    if _HOST_PYTHON_DIFFERS:
        return f"{base}_host_python{sys.version_info[0]}{sys.version_info[1]}"
    return base


def _platform_class_note() -> str:
    """State that a target status names the interpreter version, not the platform."""
    return (
        "target_python"
        f"{TARGET_PYTHON_VERSION[0]}{TARGET_PYTHON_VERSION[1]} names the interpreter *version* "
        "only, never the platform: this build host is "
        f"{sys.platform}/{platform.machine()}, so no manylinux2014_x86_64 wheel has been loaded "
        "here. The version-semantics class (typing-extensions/PEP 696) is executed; the platform "
        "class is not."
    )


def _target_python_note(interpreter: TargetInterpreter) -> str:
    return (
        "isolated_import ran under a real CPython "
        f"{TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]} ({interpreter.executable}, "
        f"discovered via {interpreter.discovered_via}), the target runtime's own minor version, so "
        "a version-conditional defect such as the typing-extensions/PEP 696 class really would "
        f"fail here. {_platform_class_note()}"
    )


def _host_python_note(discovery: InterpreterDiscovery, reason: str) -> str:
    return (
        "isolated_import ran under the build host's CPython "
        f"{sys.version_info[0]}.{sys.version_info[1]}, not the target runtime's "
        f"{TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]}, because {reason}. It therefore "
        "neither confirms nor denies any version-conditional behaviour (the "
        "typing-extensions/PEP 696 class). The marker_requires phase covers that class "
        "host-independently; the status is reported as "
        f"{_annotate_pass('pass', ran_on_target=False)!r} rather than 'pass' so this is never read "
        f"as target-verified. Candidate interpreters: {discovery.describe_attempts()}."
    )


_CHILD_PROGRAM = """
import sys
sys.path[:] = [p for p in shadow_dirs if p] + list(zip_paths) + [runtime_dir] + [
    p for p in stdlib_paths if p
]
import importlib
importlib.import_module(module_name)
print("OK")
"""


def _build_runtime_sandbox(directory: Path) -> list[str]:
    """Symlink only the runtime-provided distributions into `directory`; return what was linked.

    Models the Lambda runtime image; the build host's site-packages is never put on the path.
    """
    linked: list[str] = []
    for name in sorted(RUNTIME_PROVIDED_MODULES):
        try:
            found = importlib.util.find_spec(name)
        except (ImportError, ValueError):  # pragma: no cover - defensive
            continue
        if found is None:
            continue
        origin = found.origin
        if found.submodule_search_locations:
            source = Path(list(found.submodule_search_locations)[0])
        elif origin:
            source = Path(origin)
        else:  # pragma: no cover - namespace package without a location
            continue
        target = directory / source.name
        if not target.exists():
            target.symlink_to(source)
        linked.append(name)
    return linked


def _resolve_host_native(top_levels: list[str]) -> tuple[tuple[str, str], ...] | None:
    """Locate the build host's own copy of each name in `top_levels`.

    Returns ``None`` if any name is unavailable on the host.
    """
    located: list[tuple[str, str]] = []
    for name in top_levels:
        try:
            found = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            return None
        if found is None:
            return None
        if found.submodule_search_locations:
            source = Path(list(found.submodule_search_locations)[0])
        elif found.origin:
            source = Path(found.origin)
        else:
            return None
        if not source.exists():
            return None
        located.append((name, str(source)))
    return tuple(located)


def _link_resolved(directory: Path, resolved: tuple[tuple[str, str], ...]) -> None:
    """Symlink already-resolved ``(name, location)`` pairs into `directory`."""
    for _name, location in resolved:
        source = Path(location)
        target = directory / source.name
        if not target.exists():
            target.symlink_to(source)


def _import_failed_on_substitute(output: str, shadow_names: list[str]) -> bool:
    """True when a child failure is the substituted compiled distribution failing to load.

    Only an ``ImportError``/``ModuleNotFoundError`` whose last line names a shadowed top-level
    package counts.
    """
    last = output.strip().splitlines()[-1] if output.strip() else ""
    if not any(marker in last for marker in ("ImportError", "ModuleNotFoundError")):
        return False
    return any(name in last for name in shadow_names)


@dataclass
class _ImportAttempt:
    """One execution of the child import: which interpreter, and what it produced."""

    executable: str
    ran_on_target: bool
    completed: subprocess.CompletedProcess[str]

    @property
    def succeeded(self) -> bool:
        return self.completed.returncode == 0 and self.completed.stdout.strip().endswith("OK")

    @property
    def detail(self) -> str:
        lines = (self.completed.stderr or self.completed.stdout).strip().splitlines()
        return lines[-1] if lines else "no output"


def _run_child_import(
    *,
    executable: str,
    ran_on_target: bool,
    stdlib_paths: tuple[str, ...],
    zip_path: Path,
    import_roots: tuple[str, ...],
    module: str,
    shadow: tuple[tuple[str, str], ...],
    extract: bool = False,
) -> _ImportAttempt:
    """Import `module` from `zip_path` in a clean subprocess of `executable`.

    `import_roots` become the child's in-archive `sys.path` entries, in order, modelling the deployed
    function's own path: the archive root, plus each directory the function's `PYTHONPATH` adds.
    With `extract`, the archive is first extracted to a sandbox directory, as the Lambda runtime
    extracts it to ``/var/task``, and the entries name that directory instead of the archive, so
    vendored compiled extensions load.
    """
    with tempfile.TemporaryDirectory(prefix="lambda_runtime_sandbox.") as sandbox:
        if extract:
            task_dir = Path(sandbox) / "task"
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(task_dir)
            code_paths = [str(task_dir / root) if root else str(task_dir) for root in import_roots]
        else:
            code_paths = [_in_zip_path(zip_path, root) for root in import_roots]
        runtime_dir = Path(sandbox) / "runtime"
        runtime_dir.mkdir()
        _build_runtime_sandbox(runtime_dir)
        shadow_dirs: list[str] = []
        if shadow:
            shadow_dir = Path(sandbox) / "host_native"
            shadow_dir.mkdir()
            _link_resolved(shadow_dir, shadow)
            shadow_dirs.append(str(shadow_dir))
        preamble = (
            f"zip_paths = {code_paths!r}\n"
            f"runtime_dir = {str(runtime_dir)!r}\n"
            f"shadow_dirs = {shadow_dirs!r}\n"
            f"stdlib_paths = {list(stdlib_paths)!r}\n"
            f"module_name = {module!r}\n"
        )
        completed = proc_run.run(
            [executable, "-E", "-S", "-c", preamble + _CHILD_PROGRAM],
            capture_output=True,
            text=True,
            check=False,
            cwd=sandbox,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    return _ImportAttempt(executable=executable, ran_on_target=ran_on_target, completed=completed)


def check_isolated_import(
    spec: ZipSpec,
    zip_path: Path,
    platform_distributions: dict[str, list[str]],
    result: ZipResult,
    *,
    discovery: InterpreterDiscovery | None = None,
) -> None:
    """Import the handler module in a clean subprocess with only the zip on `sys.path`.

    When the build host matches the Lambda platform, the zip is extracted first and imported from
    that directory, as the runtime does; otherwise it is imported in place with ``zipimport``.

    The child's `sys.path` is an optional shadow of substituted compiled distributions, the zip, a
    sandbox of the runtime-provided distributions, and the standard library. The child runs under
    the discovered CPython 3.13 when there is one and it can load any needed substitutes, and
    otherwise under the build host's interpreter. Substitutes come from :func:`target_shadow_dir`
    at the zip's vendored version on the target path, and from the build host's environment,
    unversioned, on the fallback; when the fallback has none either, the phase reports ``skip``.
    """
    if discovery is None:
        discovery = discover_target_interpreter()

    shadow_names: list[str] = []
    substituted: list[str] = []
    if platform_distributions and not _HOST_MATCHES_LAMBDA:
        for dist, top_levels in sorted(platform_distributions.items()):
            substituted.append(dist)
            shadow_names.extend(top_levels)

    host_stdlib = _host_stdlib_paths()
    interpreter = discovery.interpreter
    declined_reason: str | None = None
    attempt: _ImportAttempt | None = None

    if interpreter is not None:
        shadow_root = target_shadow_dir()
        unusable = _shadow_supplies(shadow_root, substituted) if shadow_names else None
        target_shadow = (
            None
            if unusable is not None
            else _resolve_in_interpreter(
                interpreter.executable,
                tuple(shadow_names),
                str(shadow_root) if shadow_names else "",
            )
        )
        if target_shadow is None:
            declined_reason = (
                f"the discovered CPython {TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]} "
                f"({interpreter.executable}) has no importable substitute for this zip's "
                f"platform-specific distribution(s) {substituted}"
                + (f": {unusable}" if unusable is not None else "")
            )
        else:
            attempt = _run_child_import(
                executable=interpreter.executable,
                ran_on_target=True,
                stdlib_paths=interpreter.stdlib_paths,
                zip_path=zip_path,
                import_roots=spec.import_roots,
                module=spec.module,
                shadow=target_shadow,
                extract=_HOST_MATCHES_LAMBDA,
            )
            if not attempt.succeeded and _import_failed_on_substitute(attempt.detail, shadow_names):
                declined_reason = (
                    f"the discovered CPython {TARGET_PYTHON_VERSION[0]}."
                    f"{TARGET_PYTHON_VERSION[1]} ({interpreter.executable}) could not load a "
                    f"substitute for this zip's platform-specific distribution(s) {substituted}: "
                    f"{attempt.detail}"
                )
                attempt = None
    else:
        declined_reason = "no CPython 3.13 was discoverable on this build host"

    if attempt is None:
        host_shadow: tuple[tuple[str, str], ...] = ()
        if shadow_names:
            located = _resolve_host_native(shadow_names)
            if located is None:
                result.isolated_import_status = IMPORT_SKIP
                result.notes.append(
                    "isolated_import skipped with reason "
                    "platform_mismatch_and_no_host_native_substitute: the zip vendors "
                    f"platform-specific distribution(s) {substituted} built for "
                    f"{TARGET_PYTHON_TAG}/x86_64-linux, this build host is "
                    f"{sys.platform}/{platform.machine()}/CPython "
                    f"{sys.version_info[0]}.{sys.version_info[1]}, and the host has no importable "
                    "equivalent to substitute. The closure and wheel_platform phases still "
                    "applied in full."
                )
                return
            host_shadow = located
        attempt = _run_child_import(
            executable=sys.executable,
            ran_on_target=not _HOST_PYTHON_DIFFERS,
            stdlib_paths=host_stdlib,
            zip_path=zip_path,
            import_roots=spec.import_roots,
            module=spec.module,
            shadow=host_shadow,
            extract=_HOST_MATCHES_LAMBDA,
        )

    if attempt.succeeded:
        base = f"{IMPORT_PASS}_host_native_binaries" if substituted else IMPORT_PASS
        result.isolated_import_status = _annotate_pass(base, ran_on_target=attempt.ran_on_target)
        if substituted:
            if attempt.ran_on_target:
                origin = (
                    f"the target-version substitute directory {target_shadow_dir()}, at the "
                    "version the zip itself vendors (verified against the zip's dist-info; a "
                    "mismatch is refused)"
                )
            else:
                origin = (
                    "the build host's own environment, at whatever version that environment "
                    "carries — this fallback performs no version check, so version equality with "
                    "the zip's vendored copy is not established here"
                )
            result.notes.append(
                f"isolated_import executed with substitutes for compiled distribution(s) "
                f"{substituted} supplied by {origin}, and built for "
                f"{sys.platform}/{platform.machine()} rather than "
                f"{TARGET_PYTHON_TAG}/x86_64-linux; every other module, including the whole "
                "pure-Python import graph, came from the zip itself."
            )
        if attempt.ran_on_target:
            if discovery.interpreter is None:
                raise RuntimeError("an on-target attempt ran without a discovered interpreter")
            result.notes.append(_target_python_note(discovery.interpreter))
        else:
            result.notes.append(
                _host_python_note(
                    discovery, declined_reason or "the host is not the target version"
                )
            )
        return
    result.isolated_import_status = IMPORT_FAIL
    result.violations.append(
        f"isolated import of {spec.module!r} from the zip failed under "
        f"{'the target CPython 3.13' if attempt.ran_on_target else 'the build host interpreter'} "
        f"(exit {attempt.completed.returncode}): {attempt.detail}"
    )


# ---------------------------------------------------------------------------
# Phase 6: the zip's first-party content is the source tree's current content
# ---------------------------------------------------------------------------


def _source_files_for_package(package: str, source_root: Path) -> dict[str, bytes]:
    """Every shippable file under ``<source_root>/<package>``, keyed by its in-zip path.

    Excludes ``__pycache__`` and ``*.pyc``, as `scripts/package_lambdas.sh` does.
    """
    root = source_root / package
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        files[path.relative_to(source_root).as_posix()] = path.read_bytes()
    return files


def _source_base(source_root: Path, repo_relative: str) -> Path:
    """Locate the tree an importable root's first-party packages must match.

    ``"src"`` maps to `source_root`; any other repository-relative directory (``"."`` for the
    repository root) is resolved against `source_root`'s parent.
    """
    if repo_relative == "src":
        return source_root
    if repo_relative == ".":
        return source_root.parent
    return source_root.parent / repo_relative


def check_source_parity(
    index: ZipModuleIndex,
    result: ZipResult,
    *,
    source_root: Path | None,
    package_roots: tuple[tuple[str, str], ...] = FLAT_LAYOUT,
) -> None:
    """Fail when the zip's first-party content is not ``source_root``'s current content.

    For each first-party package the zip carries, compares byte for byte in both directions: a
    member whose bytes differ from the tree, a member the tree no longer has, and a shippable file
    in the tree the zip does not carry are each a violation.

    Args:
        index: The zip under test.
        result: Accumulates violations and the matched-file count.
        source_root: The source tree the zip must match. ``None`` (a synthetic zip) records a note
            instead.
        package_roots: The zip's layout, as on :class:`ZipSpec`.
    """
    if source_root is None:
        result.notes.append(
            "source_parity not run: no source root was given, so this zip's first-party content "
            "was not compared to any tree. Only a synthetic zip is checked this way."
        )
        return
    bases = dict(package_roots)
    for (root, package), members in sorted(index.first_party_members().items()):
        base = _source_base(source_root, bases[root])
        expected = _source_files_for_package(package, base)
        in_zip = set(members)
        for member in members:
            source_bytes = expected.get(member)
            if source_bytes is None:
                result.violations.append(
                    f"source_parity (zip vs {WORKING_TREE_SUBJECT}): zip ships first-party module "
                    f"{_in_zip(root, member)!r}, which does not exist under {base} — the zip was "
                    "built from a different source tree than the one being deployed"
                )
                continue
            if index.read_bytes(_in_zip(root, member)) != source_bytes:
                result.violations.append(
                    f"source_parity (zip vs {WORKING_TREE_SUBJECT}): the zip's copy of first-party "
                    f"module {_in_zip(root, member)!r} differs from {base}/{member}, so deploying "
                    "it ships code that was never packaged. This phase says nothing about whether "
                    "the zip is reproducible from git — see the tracked_provenance violation(s) for "
                    "that, which distinguish a stale artifact from untracked code baked in. "
                    "Re-run scripts/package_lambdas.sh"
                )
                continue
            result.first_party_files_matched += 1
        absent = sorted(path for path in expected if path not in in_zip)
        if absent:
            result.violations.append(
                f"source_parity (zip vs {WORKING_TREE_SUBJECT}): {len(absent)} first-party file(s) "
                f"under {base}/{package} are absent from the zip, which ships that package: "
                f"{_truncated_paths(absent)}. Re-run scripts/package_lambdas.sh"
            )


# ---------------------------------------------------------------------------
# Phase 7: the zip's first-party content is TRACKED content
# ---------------------------------------------------------------------------


def _truncated_paths(paths: list[str], *, limit: int = 5) -> str:
    """Render a sorted path list for a violation message, eliding all but the first `limit`."""
    shown = sorted(paths)[:limit]
    suffix = f" (+{len(paths) - len(shown)} more)" if len(paths) > len(shown) else ""
    return f"{shown}{suffix}"


def _git_blob_id(payload: bytes) -> str:
    """The git blob id of `payload`, computed locally.

    Git hashes a blob as ``sha1("blob <bytelength>\\0" + content)``.
    """
    header = b"blob %d\0" % len(payload)
    return hashlib.new("sha1", header + payload, usedforsecurity=False).hexdigest()


#: The tree each of phases 6 and 7 compares a zip member against, as named in its output.
WORKING_TREE_SUBJECT = "the src/ WORKING TREE (includes untracked and uncommitted files)"
TRACKED_SUBJECT = "the GIT INDEX (tracked content only)"


@dataclass(frozen=True)
class ProvenanceClass:
    """One way a zip member can fail `tracked_provenance`, with the diagnosis it warrants.

    `contrast` names the class this one is *not*, so a message read in isolation is unambiguous.
    """

    headline: str
    explanation: str
    contrast: str
    fix: str

    def violation(self, paths: list[str]) -> str:
        return (
            f"tracked_provenance (zip vs {TRACKED_SUBJECT}): {len(paths)} first-party file(s) in "
            f"this zip are {self.headline}: {_truncated_paths(paths)}. {self.explanation} "
            f"NOT {self.contrast}. {self.fix}"
        )


#: Keyed by the class name reported in the JSON report's ``tracked_provenance_classes``.
PROVENANCE_CLASSES: Mapping[str, ProvenanceClass] = {
    "untracked_code_baked_in": ProvenanceClass(
        headline="UNTRACKED CODE BAKED IN — present in the working tree but absent from the git index",
        explanation=(
            "The zip's bytes are current with this disk; the source they came from exists on this "
            "disk only. The artifact is unreproducible by construction — a fresh clone cannot "
            "rebuild it and nothing records what went into it — and `terraform apply` pins the zip "
            "through filebase64sha256, never its source."
        ),
        contrast="a stale artifact: the zip's bytes match the tree, and the tree is not in git",
        fix="Commit the file(s), then re-run scripts/package_lambdas.sh.",
    ),
    "untracked_and_stale": ProvenanceClass(
        headline=(
            "UNTRACKED AND STALE — absent from the git index, and the zip's copy differs from the "
            "working tree as well"
        ),
        explanation=(
            "Two faults at once: the source is in no commit, and the disk holds different bytes "
            "from the ones the zip carries. Committing alone would leave the artifact and the disk "
            "disagreeing, and repackaging alone would leave it unreproducible."
        ),
        contrast=(
            "untracked code merely baked in: the zip's bytes are NOT current with this disk, so "
            "repackaging does change them"
        ),
        fix=(
            "Commit the working-tree file(s) AND re-run scripts/package_lambdas.sh — either step "
            "alone is insufficient."
        ),
    ),
    "untracked_and_unreadable_in_working_tree": ProvenanceClass(
        headline=(
            "UNTRACKED AND UNREADABLE IN THE WORKING TREE — absent from the git index, and the path "
            "exists on disk but cannot be read as a file"
        ),
        explanation=(
            "Something is at that path that is not a readable regular file — a directory standing "
            "where a file should be, a broken symlink or a symlink loop, a socket or a device node, a "
            "mode that denies reading, or anything else that is not a regular file — so its bytes "
            "cannot be compared with the zip's at all. It is in no commit either, so nothing records "
            "what went into the artifact."
        ),
        contrast=(
            "code absent from the working tree: the path IS present, so there is nothing to restore — "
            "it has to be made readable"
        ),
        fix=(
            "Repair the path so it is a readable regular file — a directory, an unresolvable symlink, "
            "a socket or device node, or a mode that denies reading will each produce this — then "
            "commit it and re-run scripts/package_lambdas.sh."
        ),
    ),
    "tracked_and_unreadable_in_working_tree": ProvenanceClass(
        headline=(
            "TRACKED BUT UNREADABLE IN THE WORKING TREE — tracked with different content, and the path "
            "exists on disk but cannot be read as a file"
        ),
        explanation=(
            "Something is at that path that is not a readable regular file, so the disk copy cannot be "
            "compared with either the zip or the index. The tracked content is recoverable, so the "
            "artifact remains attributable; what is unknown is what this disk currently holds."
        ),
        contrast=(
            "a path absent from the working tree: it IS present, so restoring it is not the fix"
        ),
        fix=(
            "Repair the path so it is a readable regular file — a directory, an unresolvable symlink, "
            "a socket or device node, or a mode that denies reading will each produce this — then "
            "re-run scripts/package_lambdas.sh."
        ),
    ),
    "untracked_and_absent_from_working_tree": ProvenanceClass(
        headline=(
            "UNTRACKED AND ABSENT FROM THE WORKING TREE — in neither the git index nor the source tree"
        ),
        explanation=(
            "The source these were built from exists nowhere now, so the artifact's content is "
            "both unreproducible and unattributable."
        ),
        contrast=(
            "code merely left uncommitted: there is no file on disk to commit, so committing "
            "fixes nothing"
        ),
        fix=(
            "Restore and commit the file(s), or re-run scripts/package_lambdas.sh from a clean tree "
            "to drop them. If a parent directory's name is occupied by a file, that reads as absent "
            "too and has to be cleared before the path can be restored."
        ),
    ),
    "packaged_over_uncommitted_edit": ProvenanceClass(
        headline=(
            "MATCHING THE WORKING TREE BUT NO COMMIT — byte-identical to the source tree and "
            "different from their tracked content"
        ),
        explanation=(
            "The zip's bytes match this disk and differ from what is committed, so the artifact is "
            "current with this disk and unreproducible from the repository."
        ),
        contrast="a stale artifact: repackaging will not change these bytes, because they already match",
        fix="Commit the edit(s), then re-run scripts/package_lambdas.sh.",
    ),
    "stale_artifact": ProvenanceClass(
        headline=(
            "a STALE ARTIFACT — the zip's copy differs from its tracked content, and the working tree "
            "AGREES with the git index"
        ),
        explanation=(
            "The zip's copy differs from the committed source and the disk matches what is committed, "
            "so the zip is the only one of the three copies that differs. That the working tree "
            "agrees with the index is what makes this the one-step case."
        ),
        contrast=(
            "untracked or uncommitted code baked in: the current source IS tracked, and there is "
            "nothing here to commit"
        ),
        fix="Re-run scripts/package_lambdas.sh to rebuild from the current tree.",
    ),
    "stale_artifact_over_uncommitted_edit": ProvenanceClass(
        headline=(
            "a STALE ARTIFACT OVER AN UNCOMMITTED EDIT — the zip's copy differs from its tracked "
            "content, and the working tree differs from BOTH"
        ),
        explanation=(
            "Two states at once: the zip's copy differs from its tracked content, and the source "
            "carries edits that are in no commit. Repackaging alone would bake those edits in and "
            "leave the artifact unreproducible — this gate would then report the member as packaged "
            "over an uncommitted edit, so a repackage-only fix trades one violation for another."
        ),
        contrast=(
            "a plain stale artifact: there IS something here to commit, so repackaging alone is not "
            "the fix"
        ),
        fix=(
            "Commit the working-tree edit(s) FIRST, then re-run scripts/package_lambdas.sh — "
            "repackaging alone converts this into the packaged_over_uncommitted_edit class."
        ),
    ),
    "stale_and_absent_from_working_tree": ProvenanceClass(
        headline=(
            "STALE AND ABSENT FROM THE WORKING TREE — tracked with different content, and no longer "
            "present in the source tree at all"
        ),
        explanation=(
            "The zip's copy differs from its tracked content and the path is not present in the "
            "working tree at all, so there is no source at it to rebuild from: repackaging drops "
            "the member rather than refreshing it."
        ),
        contrast=(
            "a plain stale artifact: repackaging will not bring these bytes into agreement with the "
            "index, because nothing on disk supplies them"
        ),
        fix=(
            "Restore the file(s) if the zip should still carry them, then re-run "
            "scripts/package_lambdas.sh; otherwise repackage to drop them deliberately."
        ),
    ),
}


@dataclass(frozen=True)
class TrackedContent:
    """The git index: which repository paths are tracked, and what each one's blob id is.

    `unavailable_reason` is non-empty when git could not answer. `repo_root` records which tree the
    index was read from; ``None`` skips :func:`check_tracked_provenance`'s root check.
    """

    blob_ids: Mapping[str, str]
    unavailable_reason: str = ""
    repo_root: Path | None = None

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    def is_tracked(self, repo_relative: str) -> bool:
        return repo_relative in self.blob_ids


def _git_env() -> dict[str, str]:
    """This process's environment without the git discovery overrides.

    Only the variables in :data:`_GIT_DISCOVERY_OVERRIDES` are dropped. A repository-local
    `core.worktree` is not affected.
    """
    return {k: v for k, v in os.environ.items() if k not in _GIT_DISCOVERY_OVERRIDES}


def _foreign_work_tree_reason(root: Path) -> str:
    """Why `root` cannot be asked a `git ls-files` question: git's work tree is a *different* tree.

    Reports a reason unless `git rev-parse --show-toplevel --show-prefix` answers with an empty
    prefix, since `git ls-files` below the top level answers from the enclosing repository's index.

    Returns the reason to report, or ``""``. Never raises; a probe that fails to run, exits
    non-zero, or names no toplevel returns ``""``, leaving the caller's git call to report it.
    """
    try:
        probe = proc_run.run(
            ["git", "--no-pager", "rev-parse", "--show-toplevel", "--show-prefix"],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=_git_env(),
        )
        if probe.returncode != 0:
            return ""
        # Two lines (toplevel, prefix); an empty prefix means `root` is the top level.
        fields = probe.stdout.decode("utf-8", "surrogateescape").removesuffix("\n").split("\n")
        named = fields[0]
        if not named or (len(fields) == 2 and not fields[1]):
            return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    return (
        f"`git rev-parse --show-toplevel` names {named} as the work tree containing {root}, which is "
        "a different tree, and `git ls-files` is scoped to the directory it runs in -- so the index "
        "readable here describes the enclosing repository, and its blob ids are that repository's "
        f"rather than {root}'s. Any tree sitting inside another checkout is in this state (an "
        "extracted tarball, a downloaded archive, a vendored copy), and no unresolved conflict is "
        "needed to reach it. Read provenance at the tree git names, or place this tree outside any "
        "other work tree, then re-run. `unavailable` already means the question was not asked -- "
        "never that it passed"
    )


def load_tracked_content(*, repo_root: Path | None = None) -> TrackedContent:
    """Read the git index once: every tracked path and its blob id.

    Never raises: a missing binary, a non-work-tree, a timeout, a `root` that is not the top level
    of its work tree, an unmerged path, or an unparsable entry each become an `unavailable_reason`.
    Git runs under :func:`_git_env`. `repo_root` defaults to :data:`REPO_ROOT` at call time.
    """
    root = repo_root if repo_root is not None else REPO_ROOT
    try:
        foreign = _foreign_work_tree_reason(root)
        if foreign:
            return TrackedContent(blob_ids={}, unavailable_reason=foreign, repo_root=root)
        completed = proc_run.run(
            ["git", "--no-pager", "ls-files", "-s", "-z"],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return TrackedContent(
            blob_ids={}, unavailable_reason=f"`git ls-files` could not run: {exc}", repo_root=root
        )
    if completed.returncode != 0:
        return TrackedContent(
            blob_ids={},
            unavailable_reason=(
                "`git ls-files` exited "
                f"{completed.returncode} (not a work tree, or git is unavailable)"
            ),
            repo_root=root,
        )
    blob_ids: dict[str, str] = {}
    unmerged: list[str] = []
    unparsable: list[str] = []
    for entry in completed.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not entry:
            continue
        # `<mode> <blob> <stage>\t<path>`; split on the tab, as a path may contain spaces.
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if not path or len(fields) < 3:
            unparsable.append(path or entry)
            continue
        # Stage 0 is a merged entry; any other stage is part of an unresolved conflict.
        if fields[2] != "0":
            unmerged.append(path)
            continue
        blob_ids[path] = fields[1]
    if unmerged:
        shown = sorted(set(unmerged))
        return TrackedContent(
            blob_ids={},
            unavailable_reason=(
                f"the git index has {len(shown)} path(s) with UNMERGED entries, so there is no single "
                f"tracked content to compare against: {_truncated_paths(shown)}. Any operation that "
                "leaves a conflict unresolved reaches this state (merge, rebase, cherry-pick, revert, "
                "`am -3` and `stash apply` were each measured to), and some of them leave no operation "
                "to abort -- so the remedy that always applies is to resolve the listed path(s) and "
                "`git add` them; aborting is an alternative only where an operation is in progress, "
                "using that operation's own --abort. Then re-run. This is deliberately whole-index "
                "rather than per-path: while any path is unmerged the repository has no resolved state "
                "to package from at all, and `unavailable` already means the question was not asked "
                "-- never that it passed"
            ),
            repo_root=root,
        )
    if unparsable:
        return TrackedContent(
            blob_ids={},
            unavailable_reason=(
                f"{len(unparsable)} `git ls-files -s` entry/entries could not be parsed, so the merge "
                "stage that says whether the path is resolved is unknown: "
                f"{_truncated_paths(sorted(unparsable))}"
            ),
            repo_root=root,
        )
    if not blob_ids:
        return TrackedContent(
            blob_ids={},
            unavailable_reason="`git ls-files` listed no tracked file at all, which cannot be right",
            repo_root=root,
        )
    return TrackedContent(blob_ids=blob_ids, repo_root=root)


#: The states a working-tree path can be in, as the classifier distinguishes them.
WORKING_TREE_STATES: tuple[str, ...] = ("absent", "unreadable", "readable")

#: The git-index states reachable at the classifier: a member equal to the zip is never classified,
#: and an unmerged index is reported ``unavailable`` by :func:`load_tracked_content`.
INDEX_STATES: tuple[str, ...] = ("absent", "differs")

# The only spellings of the states in this module; order matters.
WORK_TREE_ABSENT, WORK_TREE_UNREADABLE, WORK_TREE_READABLE = WORKING_TREE_STATES
INDEX_ABSENT, INDEX_DIFFERS = INDEX_STATES


@dataclass(frozen=True)
class WorkingTreeProbe:
    """What a working-tree path holds: absent, present-but-unreadable, or readable bytes.

    `blob_id` is set exactly when `state` is ``"readable"``.
    """

    state: str
    blob_id: str | None = None

    def __post_init__(self) -> None:
        """Refuse a state outside :data:`WORKING_TREE_STATES` or a blob id inconsistent with it."""
        if self.state not in WORKING_TREE_STATES:
            raise ValueError(
                f"state={self.state!r} is not one of WORKING_TREE_STATES {WORKING_TREE_STATES}"
            )
        if (self.state == WORK_TREE_READABLE) != (self.blob_id is not None):
            raise ValueError(
                "a WorkingTreeProbe carries a blob id exactly when it is readable: "
                f"state={self.state!r}, blob_id={self.blob_id!r}"
            )


def _probe_working_tree(path: Path) -> WorkingTreeProbe:
    """Observe `path`, distinguishing ABSENT from PRESENT-BUT-UNREADABLE from readable bytes.

    Presence uses :func:`os.path.lexists`, so a broken symlink, a directory, or a file whose mode
    denies reading is unreadable rather than absent.
    """
    try:
        if path.is_file():
            return WorkingTreeProbe(
                state=WORK_TREE_READABLE, blob_id=_git_blob_id(path.read_bytes())
            )
    except OSError:
        pass
    try:
        present = os.path.lexists(path)
    except OSError:
        present = False
    return WorkingTreeProbe(state=WORK_TREE_UNREADABLE if present else WORK_TREE_ABSENT)


def _index_state(tracked_blob: str | None) -> str:
    """Name which :data:`INDEX_STATES` member an index observation is, for a mismatching member.

    Called only for a member whose zip bytes differ from its tracked bytes.
    """
    return INDEX_ABSENT if tracked_blob is None else INDEX_DIFFERS


def _classify_provenance(
    *, zip_blob: str, tracked_blob: str | None, working_tree: WorkingTreeProbe
) -> str:
    """Name which of :data:`PROVENANCE_CLASSES` a mismatching member falls into.

    Called only for a member whose zip bytes differ from its tracked bytes. Compares the working
    tree against both the zip and the index.
    """
    if _index_state(tracked_blob) == INDEX_ABSENT:
        if working_tree.state == WORK_TREE_ABSENT:
            return "untracked_and_absent_from_working_tree"
        if working_tree.state == WORK_TREE_UNREADABLE:
            return "untracked_and_unreadable_in_working_tree"
        if working_tree.blob_id == zip_blob:
            return "untracked_code_baked_in"
        return "untracked_and_stale"
    if working_tree.state == WORK_TREE_ABSENT:
        return "stale_and_absent_from_working_tree"
    if working_tree.state == WORK_TREE_UNREADABLE:
        return "tracked_and_unreadable_in_working_tree"
    if working_tree.blob_id == zip_blob:
        return "packaged_over_uncommitted_edit"
    if working_tree.blob_id == tracked_blob:
        return "stale_artifact"
    return "stale_artifact_over_uncommitted_edit"


def check_tracked_provenance(
    index: ZipModuleIndex,
    result: ZipResult,
    *,
    source_root: Path | None,
    tracked: TrackedContent,
    package_roots: tuple[tuple[str, str], ...] = FLAT_LAYOUT,
    repo_root: Path | None = None,
) -> None:
    """Fail when the zip carries first-party content that is not tracked, or differs from tracked.

    Each mismatch is classified by :func:`_classify_provenance`. A synthetic zip (`source_root is
    None`) is ``not_applicable``; an unavailable index, or a run that compares zero members, is
    ``unavailable`` with a note.

    `repo_root` is the tree member paths are made relative to, defaulting to :data:`REPO_ROOT`.
    Raises `ValueError` when it differs from the tree `tracked` was read from.
    """
    provenance_root = repo_root if repo_root is not None else REPO_ROOT
    if tracked.repo_root is not None and tracked.repo_root.resolve() != provenance_root.resolve():
        raise ValueError(
            f"tracked content was read from {tracked.repo_root}, but member paths are being made "
            f"relative to {provenance_root}, so every member would look untracked. Pass the same "
            "repo_root to load_tracked_content and to this phase."
        )
    if source_root is None:
        result.notes.append(
            "tracked_provenance not run: this zip has no repository counterpart (synthetic)."
        )
        result.tracked_provenance_status = PROVENANCE_NOT_APPLICABLE
        return
    if not tracked.available:
        result.notes.append(
            f"tracked_provenance UNAVAILABLE: {tracked.unavailable_reason}. This run did NOT check "
            "that the artifact's first-party content is reproducible from tracked source; a green "
            "above covers the working tree only."
        )
        result.tracked_provenance_status = PROVENANCE_UNAVAILABLE
        return

    bases = dict(package_roots)
    classified: dict[str, list[str]] = {}
    matched = 0
    skipped = 0
    for (root, _package), members in sorted(index.first_party_members().items()):
        base = _source_base(source_root, bases[root])
        for member in members:
            try:
                repo_relative = (base / member).relative_to(provenance_root).as_posix()
            except ValueError:
                # A source root outside the repository has no tracked counterpart.
                result.notes.append(
                    f"tracked_provenance skipped {base / member}: it is outside "
                    f"{provenance_root}, so it has no tracked counterpart."
                )
                skipped += 1
                continue
            zip_blob = _git_blob_id(index.read_bytes(_in_zip(root, member)))
            tracked_blob = tracked.blob_ids.get(repo_relative)
            if tracked_blob == zip_blob:
                matched += 1
                continue
            name = _classify_provenance(
                zip_blob=zip_blob,
                tracked_blob=tracked_blob,
                working_tree=_probe_working_tree(base / member),
            )
            classified.setdefault(name, []).append(repo_relative)

    result.tracked_files_matched += matched
    for name in sorted(classified):
        result.violations.append(PROVENANCE_CLASSES[name].violation(classified[name]))
    result.tracked_provenance_classes = sorted(classified)
    if classified:
        result.tracked_provenance_status = PROVENANCE_FAIL
        return
    if not matched:
        # Zero comparisons establish nothing, so the status is unavailable rather than pass.
        why = (
            f"{skipped} first-party member(s) were skipped, leaving none compared"
            if skipped
            else "this zip carries no first-party member for the phase to compare"
        )
        result.notes.append(
            f"tracked_provenance UNAVAILABLE: {why}, so this run established nothing about whether "
            "the artifact's first-party content is reproducible from tracked source."
        )
        result.tracked_provenance_status = PROVENANCE_UNAVAILABLE
        return
    result.tracked_provenance_status = PROVENANCE_PASS


def check_zip(
    spec: ZipSpec,
    zip_path: Path,
    *,
    source_root: Path | None = SRC_DIR,
    discovery: InterpreterDiscovery | None = None,
    tracked: TrackedContent | None = None,
) -> ZipResult:
    """Run all seven phases against one built zip.

    Args:
        spec: The expected zip and its declared handler.
        zip_path: The built archive to check.
        source_root: Tree the zip's first-party content must match; ``None`` for a synthetic zip.
        discovery: The target-interpreter search result for ``isolated_import``. Defaults to
            :func:`discover_target_interpreter`.
        tracked: The git index for ``tracked_provenance``. Defaults to reading it.
    """
    result = ZipResult(zip_name=spec.zip_name, handler=spec.handler)
    with zipfile.ZipFile(zip_path) as archive:
        index = ZipModuleIndex(archive, import_roots=spec.import_roots)
        platform_distributions = index.platform_specific_distributions()
        check_closure(spec, index, result)
        check_vendored_records(index, result)
        check_wheel_platforms(index, result)
        check_marker_required_distributions(index, result)
        check_source_parity(
            index, result, source_root=source_root, package_roots=spec.package_roots
        )
        check_tracked_provenance(
            index,
            result,
            source_root=source_root,
            tracked=tracked if tracked is not None else load_tracked_content(),
            package_roots=spec.package_roots,
        )
    check_isolated_import(spec, zip_path, platform_distributions, result, discovery=discovery)
    return result


def run(
    build_dir: Path, require_zips: bool, *, source_root: Path | None = SRC_DIR
) -> tuple[int, dict[str, object]]:
    """Check every expected zip under `build_dir`; return `(exit_status, report)`.

    `source_root` is passed to each zip's checks; ``None`` for synthetic zips. `build_dir` is
    resolved to an absolute path, as the isolated import runs with a different `cwd`.
    """
    build_dir = build_dir.resolve()
    specs = expected_zips()
    present = [s for s in specs if (build_dir / s.zip_name).is_file()]
    missing = [s.zip_name for s in specs if not (build_dir / s.zip_name).is_file()]

    if not present:
        reason = (
            f"no expected Lambda zip exists under {build_dir} "
            f"({len(specs)} expected). Build them with scripts/package_lambdas.sh "
            "(needs network access for pip) and re-run. Nothing was verified."
        )
        report: dict[str, object] = {
            "build_dir": str(build_dir),
            "status": "fail" if require_zips else "skip",
            "reason": reason,
            "expected_zip_count": len(specs),
            "zips": [],
        }
        return (1 if require_zips else SKIP_EXIT_STATUS, report)

    if missing:
        return (
            1,
            {
                "build_dir": str(build_dir),
                "status": "fail",
                "reason": (
                    f"{len(present)} of {len(specs)} expected zips exist but "
                    f"{len(missing)} are missing, so the build directory is incomplete: {missing}"
                ),
                "expected_zip_count": len(specs),
                "zips": [],
            },
        )

    discovery = discover_target_interpreter()
    results = [
        check_zip(spec, build_dir / spec.zip_name, source_root=source_root, discovery=discovery)
        for spec in specs
    ]
    failed = [r for r in results if not r.passed]
    return (
        1 if failed else 0,
        {
            "build_dir": str(build_dir),
            "status": "fail" if failed else "pass",
            "host_matches_lambda_platform": _HOST_MATCHES_LAMBDA,
            "host_python_version": f"{sys.version_info[0]}.{sys.version_info[1]}",
            "target_python_version": f"{TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]}",
            "target_python_interpreter": (
                None
                if discovery.interpreter is None
                else {
                    "executable": discovery.interpreter.executable,
                    "discovered_via": discovery.interpreter.discovered_via,
                }
            ),
            "target_python_discovery_attempts": list(discovery.attempts),
            "marker_environment": dict(LAMBDA_MARKER_ENVIRONMENT),
            "expected_zip_count": len(specs),
            "zips": [
                {
                    "zip": r.zip_name,
                    "handler": r.handler,
                    "modules_resolved_in_zip": r.modules_resolved_in_zip,
                    "allowed_unvendored": r.allowed_unvendored,
                    "vendored_records_verified": r.records_verified,
                    "marker_requirements_resolved": r.marker_requirements_resolved,
                    "first_party_files_matched": r.first_party_files_matched,
                    "tracked_files_matched": r.tracked_files_matched,
                    "tracked_provenance": r.tracked_provenance_status,
                    "tracked_provenance_classes": r.tracked_provenance_classes,
                    "isolated_import": r.isolated_import_status,
                    "violations": r.violations,
                    "notes": r.notes,
                }
                for r in results
            ],
        },
    )


def _run_level_note(report: dict[str, object]) -> str | None:
    """The run-level ``NOTE:`` describing which interpreter really ran, and what is still unproved.

    Derived from the per-zip ``isolated_import`` statuses; ``None`` when none carries an annotation.
    """
    zips = report["zips"]
    if not isinstance(zips, list):
        raise TypeError(f"report['zips'] must be a list, got {type(zips).__name__}")
    target_suffix = f"_target_python{TARGET_PYTHON_VERSION[0]}{TARGET_PYTHON_VERSION[1]}"
    statuses = [str(e["isolated_import"]) for e in zips if isinstance(e, dict)]
    on_target = [s for s in statuses if s.endswith(target_suffix)]
    on_host = [s for s in statuses if "_host_python" in s]
    interpreter = report.get("target_python_interpreter")
    attempts = report.get("target_python_discovery_attempts")
    attempts_text = "; ".join(attempts) if isinstance(attempts, list) else ""

    if not on_target and not on_host:
        return None
    if isinstance(interpreter, dict) and on_target:
        note = (
            f"isolated_import executed under a real CPython "
            f"{TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]} "
            f"({interpreter['executable']}, discovered via {interpreter['discovered_via']}) for "
            f"{len(on_target)}/{len(statuses)} zip(s) — the target runtime's own minor version, so "
            "for those a version-conditional defect such as the typing-extensions/PEP 696 class "
            "really would fail here rather than being merely disclosed."
        )
        if on_host:
            note += (
                f" {len(on_host)}/{len(statuses)} zip(s) fell back to the build host's CPython "
                f"{sys.version_info[0]}.{sys.version_info[1]} because that 3.13 has no importable "
                "substitute for their vendored compiled distribution(s); each says so in its own "
                "status and note, and for them the marker_requires phase remains the "
                "host-independent cover for that class."
            )
        note += (
            " Separately, and for every zip regardless of interpreter: the PLATFORM class is "
            f"unexecuted. This host is {sys.platform}/{platform.machine()}, so no "
            "manylinux2014_x86_64 wheel has been loaded anywhere; "
            f"{target_suffix.lstrip('_')} names the interpreter version only, never the platform."
        )
        return note
    return (
        "isolated_import ran under the build host's CPython "
        f"{sys.version_info[0]}.{sys.version_info[1]}, not the target runtime's "
        f"{TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]}, for all {len(statuses)} zip(s), so "
        "it neither confirms nor denies any version-conditional behaviour (the "
        "typing-extensions/PEP 696 class); the marker_requires phase covers that class "
        "host-independently, and every passing status carries "
        f"'_host_python{sys.version_info[0]}{sys.version_info[1]}' so this is never read as "
        f"target-verified. Candidate interpreters: {attempts_text or 'none tried'}. Set "
        f"{TARGET_PYTHON_ENV_VAR}=/path/to/python3.13, or put python3.13 on PATH, to execute the "
        "phase under the target version. The platform class is unexecuted either way: this host is "
        f"{sys.platform}/{platform.machine()}."
    )


def _print_human(report: dict[str, object]) -> None:
    status = str(report["status"])
    if status in {"skip", "fail"} and not report["zips"]:
        marker = "SKIP" if status == "skip" else "FAIL"
        print(f"[{marker}] check_lambda_zip_imports: {report['reason']}")
        return
    zips = report["zips"]
    if not isinstance(zips, list):
        raise TypeError(f"report['zips'] must be a list, got {type(zips).__name__}")
    skipped_imports = 0
    for entry in zips:
        if not isinstance(entry, dict):
            raise TypeError(f"report['zips'] entry must be a dict, got {type(entry).__name__}")
        if entry["violations"]:
            print(f"[FAIL] {entry['zip']} ({entry['handler']})")
            for violation in entry["violations"]:
                print(f"         {violation}")
        else:
            print(
                f"[ pass ] {entry['zip']}: {entry['modules_resolved_in_zip']} module(s) resolved "
                f"in zip, {entry['vendored_records_verified']} vendored RECORD(s) complete, "
                f"{entry['marker_requirements_resolved']} METADATA requirement set(s) resolved for "
                f"the Lambda target, {entry['first_party_files_matched']} first-party file(s) "
                f"byte-identical to the src/ WORKING TREE, "
                f"{entry['tracked_files_matched']} byte-identical to TRACKED content "
                f"(tracked_provenance={entry['tracked_provenance']}), "
                f"isolated_import={entry['isolated_import']}"
            )
        if entry["isolated_import"] == IMPORT_SKIP:
            skipped_imports += 1
        for note in entry["notes"]:
            print(f"         note: {note}")
    print()
    failed = [e for e in zips if isinstance(e, dict) and e["violations"]]
    if failed:
        print(f"FAILED: {len(failed)}/{len(zips)} zip(s) cannot satisfy their handler's imports")
        return
    # Claim tracked parity only when all statuses are pass/not_applicable and one is pass.
    tracked_statuses = {str(e["tracked_provenance"]) for e in zips if isinstance(e, dict)}
    if (
        tracked_statuses <= {PROVENANCE_PASS, PROVENANCE_NOT_APPLICABLE}
        and PROVENANCE_PASS in tracked_statuses
    ):
        tracked_clause = " AND to tracked git content"
    else:
        tracked_clause = (
            " -- but NOT verified against tracked git content "
            f"(tracked_provenance={'/'.join(sorted(tracked_statuses))}), so this says nothing about "
            "whether the artifacts are reproducible from the repository"
        )
    print(
        f"OK: {len(zips)}/{len(zips)} zip(s) resolve their handler's full import closure, ship "
        f"every file their vendored distributions' RECORDs declare, carry every distribution "
        f"their METADATA requires under the Lambda target environment "
        f"(python_version={LAMBDA_MARKER_ENVIRONMENT['python_version']}), and hold first-party "
        f"content byte-identical to the current src/ working tree{tracked_clause}; "
        f"isolated_import executed for {len(zips) - skipped_imports}, "
        f"explicitly skipped for {skipped_imports}"
    )
    if _HOST_PYTHON_DIFFERS or report.get("target_python_interpreter") is not None:
        note = _run_level_note(report)
        if note is not None:
            print(f"NOTE: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=Path(os.environ.get("LAMBDA_BUILD_DIR", str(DEFAULT_BUILD_DIR))),
        help="Directory holding the built Lambda zips (default: infra/build/lambda).",
    )
    parser.add_argument(
        "--require-zips",
        action="store_true",
        help="Fail instead of skipping when no expected zip is present.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--print-missing-required-distributions",
        type=Path,
        metavar="TARGET_DIR",
        default=None,
        help=(
            "Resolution-only mode over a pip --target directory: print, one per line, every "
            "distribution the Lambda target environment requires and the directory does not "
            "contain. Builds nothing and checks no zip; scripts/package_lambdas.sh loops on this "
            "to close its vendored set for the target."
        ),
    )
    parser.add_argument(
        "--print-target-python",
        action="store_true",
        help=(
            "Resolution-only mode: print the discovered CPython "
            f"{TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]} the isolated_import phase would "
            "execute under and exit 0, or print every rejected candidate to stderr and exit 1 when "
            "none resolves. scripts/package_lambdas.sh uses this to decide whether a target-version "
            "substitute directory is worth building."
        ),
    )
    args = parser.parse_args(argv)

    if args.print_target_python:
        discovery = discover_target_interpreter()
        if discovery.interpreter is None:
            print(
                "check_lambda_zip_imports: no CPython "
                f"{TARGET_PYTHON_VERSION[0]}.{TARGET_PYTHON_VERSION[1]} resolved. Candidates: "
                f"{discovery.describe_attempts()}",
                file=sys.stderr,
            )
            return 1
        print(discovery.interpreter.executable)
        return 0

    if args.print_missing_required_distributions is not None:
        directory = args.print_missing_required_distributions
        if not directory.is_dir():
            print(f"check_lambda_zip_imports: {directory} is not a directory", file=sys.stderr)
            return 2
        present, metadata = scan_target_directory(directory)
        missing, unparseable = resolve_missing_required_distributions(present, metadata)
        for requirer, raw in unparseable:
            print(
                f"check_lambda_zip_imports: {requirer} declares an unparseable "
                f"Requires-Dist {raw!r}",
                file=sys.stderr,
            )
        if unparseable:
            return 2
        for name in sorted({name for _, name in missing}):
            print(name)
        return 0

    status, report = run(args.build_dir.resolve(), args.require_zips)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return status


if __name__ == "__main__":
    sys.exit(main())
