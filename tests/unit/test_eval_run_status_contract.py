# SPDX-License-Identifier: MIT
"""Assert the eval-run status contract agrees between the handler that emits it and the browser
client that reads it.

``crm.handlers.eval_trigger.handler`` returns :meth:`eval.runs.RunRecord.to_payload` for ``submit`` and
``status``, and ``frontend/src/pages/evals/EvalRunClient.ts`` declares what it reads in its
``EvalRunStatusResponse`` and ``StartEvalRunResponse`` interfaces. Both directions are asserted:
nothing the client reads is unemitted, and nothing emitted is unread except the keys named in
:data:`PENDING_CLIENT_FIELDS_G2_3`, which must be emitted and not yet read. The client's request
paths and methods are also checked against the routes ``infra/`` deploys.

Emitted keys are measured by calling ``to_payload`` on every ``RunRecord`` shape, since it omits
absent optionals. Failing to locate either side is a failure, never a skip.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Final

from eval.harness import SweepResult
from eval.matrix import MatrixResult
from eval.runs import RunRecord
from eval.trigger import _artifact_links

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Emitted run keys the client does not read yet. Empty.
PENDING_CLIENT_FIELDS_G2_3: Final[frozenset[str]] = frozenset()

#: The one consumer of the eval-run status payload.
_CLIENT_TS: Final[Path] = _REPO_ROOT / "frontend" / "src" / "pages" / "evals" / "EvalRunClient.ts"

#: The interface declaring what the browser reads from a status response.
_STATUS_INTERFACE: Final[str] = "EvalRunStatusResponse"

#: The interface declaring what the browser reads from a submit response.
_START_INTERFACE: Final[str] = "StartEvalRunResponse"

#: One property line inside an interface body: an optional `readonly`, the name, an optional `?`, then
#: the `:` that begins the type. Nesting is excluded by brace depth in
#: :func:`_interface_properties`.
_PROPERTY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:readonly\s+)?([A-Za-z_$][\w$]*)(\?)?\s*:",
)

#: The deployed API Gateway route mechanism and the routes it is instantiated with.
_API_MODULE_TF: Final[Path] = _REPO_ROOT / "infra" / "modules" / "api" / "main.tf"
_ROOT_TF: Final[Path] = _REPO_ROOT / "infra" / "main.tf"

#: The merged route map every routed resource in the api module is generated from.
_ROUTE_MAP: Final[str] = "local.route_invoke_arns"

#: The route map's definition: a ``merge(`` whose arguments, before any ``)``, include the tool
#: Lambda invoke-ARN map.
_ROUTE_MAP_MERGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"route_invoke_arns\s*=\s*merge\([^)]*var\.tool_lambda_invoke_arns"
)

#: The three facts that together fix the servable path shape to a single root-level segment.
_RESOURCE_FACTS: Final[tuple[tuple[str, str], ...]] = (
    (rf"for_each\s*=\s*{re.escape(_ROUTE_MAP)}\b", f"for_each = {_ROUTE_MAP}"),
    (r"path_part\s*=\s*each\.key\b", "path_part = each.key"),
    (
        r"parent_id\s*=\s*aws_api_gateway_rest_api\.this\.root_resource_id\b",
        "parent_id = aws_api_gateway_rest_api.this.root_resource_id",
    ),
)

#: The map the method is read from, parallel to :data:`_ROUTE_MAP`.
_METHOD_MAP: Final[str] = "local.route_methods"

#: The method facts: the same key set, and the method taken from :data:`_METHOD_MAP` per route.
_METHOD_FACTS: Final[tuple[tuple[str, str], ...]] = (
    (rf"for_each\s*=\s*{re.escape(_ROUTE_MAP)}\b", f"for_each = {_ROUTE_MAP}"),
    (
        rf"http_method\s*=\s*{re.escape(_METHOD_MAP)}\[each\.key\]",
        f"http_method = {_METHOD_MAP}[each.key]",
    ),
)

#: The tool half of `local.route_methods`: every key of `var.tool_lambda_invoke_arns` mapped to POST.
_METHOD_MAP_FACTS: Final[tuple[tuple[str, str], ...]] = (
    (
        r"route_methods\s*=\s*merge\(",
        "route_methods = merge(",
    ),
    (
        r"for\s+name\s+in\s+keys\(var\.tool_lambda_invoke_arns\)\s*:\s*name\s*=>\s*\"POST\"",
        'for name in keys(var.tool_lambda_invoke_arns) : name => "POST"',
    ),
    (
        r"for\s+name,\s*route\s+in\s+var\.extra_routes\s*:\s*name\s*=>\s*route\.method",
        "for name, route in var.extra_routes : name => route.method",
    ),
)

#: The default `method` an `extra_routes` entry takes when it declares none.
_EXTRA_ROUTE_METHOD_DEFAULT: Final[str] = "POST"

#: The path segments the client requests, read off its own `request(...)` call sites.
_CLIENT_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"""request<[^>]*>\(\s*[`"']([^`"']*)""")

#: A client call site with its declared HTTP method.
_CLIENT_CALL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""request<[^>]*>\(\s*[`"'](?P<path>[^`"']*)[`"']\s*,\s*\{[^}]*?method:\s*[`"'](?P<method>\w+)""",
    re.DOTALL,
)


def _display(path: Path) -> str:
    """Return ``path`` rendered relative to the repo root, or verbatim when it is outside it."""
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _read(path: Path, *, what: str) -> str:
    """Return ``path``'s text, failing with a diagnostic when it is not there."""
    assert path.is_file(), (
        f"cannot locate {what} at {_display(path)}. Failing rather than skipping: "
        "a contract check that cannot find one of its two sides reports green over an unverified "
        "contract. If the file moved, update this test to follow it."
    )
    return path.read_text(encoding="utf-8")


def _interface_properties(source: str, name: str) -> dict[str, bool]:
    """Return the top-level property names of TypeScript interface ``name``, each mapped to
    whether it is optional (``?``).

    Brace depth is tracked so a nested object type contributes none of its own keys.

    Raises:
        AssertionError: The interface is absent, unterminated, or declares no properties.
    """
    match = re.search(rf"\binterface\s+{re.escape(name)}\s*\{{", source)
    assert match is not None, (
        f"interface {name} was not found in {_CLIENT_TS.name}. Failing rather than skipping: if it "
        "was renamed or reshaped, this test must be pointed at the new declaration, because the "
        "contract is unverified until it is."
    )

    depth = 0
    properties: dict[str, bool] = {}
    body_started = False
    for line in source[match.end() - 1 :].splitlines():
        opened = line.count("{")
        closed = line.count("}")
        if depth == 1 and body_started:
            found = _PROPERTY_PATTERN.match(line)
            if found is not None:
                properties[found.group(1)] = found.group(2) is not None
        depth += opened - closed
        body_started = True
        if depth <= 0:
            break
    else:  # pragma: no cover - defensive: an unterminated interface body
        raise AssertionError(
            f"interface {name} in {_CLIENT_TS.name} has no closing brace; its properties cannot be "
            "read, so this side of the contract is unverified."
        )

    assert properties, (
        f"interface {name} in {_CLIENT_TS.name} yielded no properties. Failing rather than "
        "skipping: an empty expected set would make every assertion below trivially true."
    )
    return properties


def _emitted_keys(statuses: frozenset[str] | None = None) -> set[str]:
    """Return every key :meth:`RunRecord.to_payload` actually emits, across all run shapes.

    Args:
        statuses: When given, only variants whose ``status`` is in this set are unioned.
    """
    base = RunRecord(
        run_id="run-1",
        tenant="eval-run-1",
        mode="stub",
        status="queued",
        started_at="2026-01-01T00:00:00+00:00",
        started_by_rep_id="rep-1",
        tasks_total=40,
    )
    artifacts = _artifact_links(
        SweepResult(
            run_id="run-1",
            tenant="eval-run-1",
            mode="stub",
            matrix=MatrixResult(),
            artifact_paths={
                "json": Path("/out/matrix.json"),
                "md": Path("/out/matrix.md"),
                "csv": Path("/out/matrix.csv"),
            },
            raw_archive_paths=(Path("/out/raw/arm.json"),),
        ),
        output_dir=Path("/out"),
    )
    variants = (
        base,
        replace(base, mode="live", estimate_usd=4.05, estimate_basis="static"),
        replace(base, status="running", running_at="2026-01-01T00:01:00+00:00", tasks_done=3),
        replace(
            base,
            status="cancelling",
            cancel_requested=True,
            cancel_requested_by="adm",
            cancel_requested_at="2026-01-01T00:02:00+00:00",
        ),
        replace(
            base,
            status="completed",
            finished_at="2026-01-01T00:05:00+00:00",
            teardown_status="ok",
            matrix_report=MatrixResult().to_report_fields(),
            artifact_links=artifacts,
        ),
        replace(
            base, status="cancelled", finished_at="2026-01-01T00:05:00+00:00", tasks_skipped=37
        ),
        replace(
            base,
            status="failed",
            finished_at="2026-01-01T00:05:00+00:00",
            teardown_status="failed",
            failure_summary="teardown failed",
        ),
    )
    selected = [run for run in variants if statuses is None or run.status in statuses]
    assert selected, (
        f"no run variant carries a status in {sorted(statuses or ())}; the filter selected nothing, "
        "so the emitted set would be empty and every assertion over it vacuous."
    )
    keys: set[str] = set()
    for run in selected:
        keys.update(run.to_payload())
    assert keys, (
        "RunRecord.to_payload() emitted no keys for any run shape. Failing rather than skipping: an "
        "empty emitted set would make the client-reads-nothing-unemitted assertion vacuous."
    )
    return keys


def _resource_block(source: str, resource_type: str, name: str) -> str:
    """Return the body of the top-level ``resource "<type>" "<name>"`` block in ``source``.

    Matched from the block's opening line to the first column-zero ``}``. A block that cannot be
    located is a failure.
    """
    pattern = re.compile(
        rf'^resource\s+"{re.escape(resource_type)}"\s+"{re.escape(name)}"\s*\{{\n(.*?)^\}}',
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(source)
    assert match is not None, (
        f'resource "{resource_type}" "{name}" was not found in '
        f"{_display(_API_MODULE_TF)}. Failing rather than skipping: the set of servable "
        "paths cannot be derived, so the client's paths are unverified. If the route mechanism "
        "changed again, port this derivation to it."
    )
    return match.group(1)


def _extra_route_methods(root_source: str) -> dict[str, str]:
    """Return ``{route name: HTTP method}`` for the root's ``extra_routes``, defaults resolved.

    An entry declaring no ``method`` takes :data:`_EXTRA_ROUTE_METHOD_DEFAULT`.
    """
    block = re.search(r"extra_routes\s*=\s*\{(.*?)\n  \}", root_source, re.DOTALL)
    assert block is not None, (
        "the root module's `extra_routes` block was not found, so the non-tool route names cannot be "
        "read. Failing rather than skipping."
    )
    body = block.group(1)
    methods: dict[str, str] = {}
    # Each entry is `    name = {` ... up to the matching `    }` at the same indent.
    for entry in re.finditer(
        r"^\s{4}(?P<name>\w+)\s*=\s*\{(?P<inner>.*?)^\s{4}\}", body, re.DOTALL | re.MULTILINE
    ):
        declared = re.search(r'method\s*=\s*"(?P<method>\w+)"', entry.group("inner"))
        methods[entry.group("name")] = (
            declared.group("method") if declared else _EXTRA_ROUTE_METHOD_DEFAULT
        )
    assert methods, (
        "no route names were parsed from `extra_routes`; an empty servable set would make the "
        "assertion below fail for the wrong reason."
    )
    return methods


def _assert_route_mechanism(api_source: str) -> None:
    """Assert the api module serves each route map key as exactly ``<method> /<key>``."""
    resource_body = _resource_block(api_source, "aws_api_gateway_resource", "tool")
    method_body = _resource_block(api_source, "aws_api_gateway_method", "tool")

    for body, facts, subject in (
        (resource_body, _RESOURCE_FACTS, 'resource "aws_api_gateway_resource" "tool"'),
        (method_body, _METHOD_FACTS, 'resource "aws_api_gateway_method" "tool"'),
        (api_source, _METHOD_MAP_FACTS, f"the `{_METHOD_MAP}` locals value"),
    ):
        for pattern, literal in facts:
            assert re.search(pattern, body) is not None, (
                f"{subject} no longer declares `{literal}`, which this test reads to derive the "
                "servable path shape. Failing rather than skipping: the set of servable paths cannot "
                "be derived, so the client's paths are unverified."
            )

    # The key set itself.
    assert (
        _ROUTE_MAP_MERGE_PATTERN.search(api_source) is not None and "var.extra_routes" in api_source
    ), (
        f"{_ROUTE_MAP} is no longer the merge of `var.tool_lambda_invoke_arns` and "
        "`var.extra_routes`, so the servable route names cannot be derived from `extra_routes`. "
        "Failing rather than skipping."
    )


def _near_miss(name: str, candidates: set[str]) -> str:
    """Return a hint naming a candidate that differs from ``name`` only by naming convention.

    Diagnostic only; never consulted by an assertion.
    """
    flattened = name.replace("_", "").lower()
    matches = sorted(other for other in candidates if other.replace("_", "").lower() == flattened)
    return f" (near miss, differing only in naming convention: {matches})" if matches else ""


def test_every_field_the_client_reads_is_emitted_by_the_handler() -> None:
    """Every required property the client reads is emitted by the handler."""
    properties = _interface_properties(
        _read(_CLIENT_TS, what="the eval-run client"), _STATUS_INTERFACE
    )
    emitted = _emitted_keys()

    required = {name for name, optional in properties.items() if not optional}
    missing = sorted(name for name in required if name not in emitted)
    assert not missing, (
        "the browser reads required status fields the handler never emits: "
        + ", ".join(f"{name}{_near_miss(name, emitted)}" for name in missing)
        + f". RunRecord.to_payload() emits {sorted(emitted)}."
    )


def test_every_optional_field_the_client_reads_is_producible() -> None:
    """Every optional property the client reads is emitted by some job shape."""
    properties = _interface_properties(
        _read(_CLIENT_TS, what="the eval-run client"), _STATUS_INTERFACE
    )
    emitted = _emitted_keys()

    optional = {name for name, is_optional in properties.items() if is_optional}
    assert optional, (
        "no optional properties were parsed from "
        f"{_STATUS_INTERFACE}; the parser is not reading `?` markers, so this assertion would be "
        "vacuous."
    )
    unproducible = sorted(name for name in optional if name not in emitted)
    assert not unproducible, (
        "the browser reads optional status fields no job shape ever emits: "
        + ", ".join(f"{name}{_near_miss(name, emitted)}" for name in unproducible)
        + f". RunRecord.to_payload() emits {sorted(emitted)}."
    )


def test_every_field_the_handler_emits_is_read_by_the_client() -> None:
    """Every key the handler emits is declared by the client's status interface."""
    properties = _interface_properties(
        _read(_CLIENT_TS, what="the eval-run client"), _STATUS_INTERFACE
    )
    emitted = _emitted_keys()

    ignored = sorted(
        name
        for name in emitted
        if name not in properties and name not in PENDING_CLIENT_FIELDS_G2_3
    )
    assert not ignored, (
        "the handler emits status fields the browser never reads: "
        + ", ".join(f"{name}{_near_miss(name, set(properties))}" for name in ignored)
        + f". {_STATUS_INTERFACE} declares {sorted(properties)}."
    )


def test_the_submit_response_agrees_in_both_directions() -> None:
    """The submit response agrees with ``StartEvalRunResponse`` in both directions.

    Measured against the submit-reachable job shape only.
    """
    source = _read(_CLIENT_TS, what="the eval-run client")
    properties = _interface_properties(source, _START_INTERFACE)
    emitted = _emitted_keys(frozenset({"queued"}))

    required = {name for name, optional in properties.items() if not optional}
    missing = sorted(name for name in required if name not in emitted)
    ignored = sorted(
        name
        for name in emitted
        if name not in properties and name not in PENDING_CLIENT_FIELDS_G2_3
    )
    assert not missing and not ignored, (
        f"the submit response disagrees with {_START_INTERFACE}. "
        f"read-but-unemitted: {[f'{n}{_near_miss(n, emitted)}' for n in missing]}; "
        f"emitted-but-unread: {ignored}. "
        f"{_START_INTERFACE} declares {sorted(properties)}; to_payload() emits {sorted(emitted)}."
    )


def test_every_pending_field_is_emitted_and_not_yet_read() -> None:
    """`PENDING_CLIENT_FIELDS_G2_3` names only emitted keys the client does not read.

    A key the client starts reading must leave the set, so the set cannot hide a read field.
    """
    source = _read(_CLIENT_TS, what="the eval-run client")
    read = set(_interface_properties(source, _STATUS_INTERFACE)) | set(
        _interface_properties(source, _START_INTERFACE)
    )
    emitted = _emitted_keys()
    assert sorted(PENDING_CLIENT_FIELDS_G2_3 - emitted) == [], "a pending key is never emitted"
    assert sorted(PENDING_CLIENT_FIELDS_G2_3 & read) == [], (
        "the client now reads a pending key; remove it from PENDING_CLIENT_FIELDS_G2_3"
    )


def test_the_handler_emits_the_run_record_payload() -> None:
    """The handler's `submit`/`status` payload is `RunRecord.to_payload`, the subject measured here."""
    source = (_REPO_ROOT / "src" / "crm" / "handlers" / "eval_trigger.py").read_text(
        encoding="utf-8"
    )
    assert "return record.to_payload()" in source
    assert ".to_payload()" in source and "SweepJob" not in source


def test_the_client_requests_a_route_the_deployment_serves() -> None:
    """Every path and method the client requests is served by an ``extra_routes`` route."""
    client_source = _read(_CLIENT_TS, what="the eval-run client")
    api_source = _read(_API_MODULE_TF, what="the api module")
    root_source = _read(_ROOT_TF, what="the root Terraform module")

    _assert_route_mechanism(api_source)

    servable = _extra_route_methods(root_source)
    route_names = set(servable)

    requested = sorted(set(_CLIENT_PATH_PATTERN.findall(client_source)))
    assert requested, (
        f"no request paths were parsed from {_CLIENT_TS.name}; the client side of the route contract "
        "is unverified."
    )

    # A client path is servable when its first segment is a route name. The `run_id` a status call
    # appends is compared as a segment count, not matched literally.
    unservable = sorted(
        path
        for path in requested
        if path.lstrip("/").split("/")[0].split("$")[0] not in route_names
    )
    assert not unservable, (
        f"the client requests paths no deployed route can serve: {unservable}. "
        f"`extra_routes` serves {sorted(f'{method} /{name}' for name, method in servable.items())}."
    )

    # The method half.
    calls = [
        (m.group("method"), m.group("path")) for m in _CLIENT_CALL_PATTERN.finditer(client_source)
    ]
    assert calls, (
        f"no (method, path) call sites were parsed from {_CLIENT_TS.name}; the method half of the "
        "route contract is unverified. Failing rather than skipping."
    )
    mismatched = sorted(
        f"{method} {path} (route serves {servable[path.lstrip('/').split('/')[0].split('$')[0]]})"
        for method, path in calls
        if (route := path.lstrip("/").split("/")[0].split("$")[0]) in servable
        and method.upper() != servable[route]
    )
    assert not mismatched, (
        f"the client calls a method the route does not serve: {mismatched}. API Gateway answers an "
        "unmatched method with 403, which is indistinguishable at the edge from an authorization "
        "failure."
    )


def test_the_route_map_pattern_matches_the_real_api_module() -> None:
    """The route-map pattern finds the merge in the api module's source."""
    api_source = _read(_API_MODULE_TF, what="the api module")
    assert _ROUTE_MAP_MERGE_PATTERN.search(api_source) is not None


def test_the_route_map_pattern_matches_a_multi_line_merge() -> None:
    """Arguments spread over several lines before the tool map still match."""
    source = (
        "  route_invoke_arns = merge(\n"
        "    local.other_routes,\n"
        "\n"
        "    var.tool_lambda_invoke_arns,\n"
        "  )\n"
    )
    assert _ROUTE_MAP_MERGE_PATTERN.search(source) is not None


def test_the_route_map_pattern_rejects_a_token_after_the_merge_closes() -> None:
    """A tool map named only after the merge's closing parenthesis is not part of the merge."""
    source = "  route_invoke_arns = merge(local.other_routes)\n  x = var.tool_lambda_invoke_arns\n"
    assert _ROUTE_MAP_MERGE_PATTERN.search(source) is None


def test_the_route_map_pattern_is_linear_on_an_unterminated_merge() -> None:
    """An unterminated merge of 5000 newlines is rejected in well under two seconds."""
    source = "route_invoke_arns=merge(" + "\n" * 5000
    started = time.perf_counter()
    assert _ROUTE_MAP_MERGE_PATTERN.search(source) is None
    assert time.perf_counter() - started < 2.0


def test_this_file_can_never_skip() -> None:
    """No skip or xfail marker appears anywhere in this file after the module docstring."""
    source = Path(__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    # Assembled from fragments rather than written out: a self-scanning test that spells its own
    # denylist matches itself and fails on a clean file.
    prefix = "pytest."
    banned = [prefix + name for name in ("skip", "importorskip", "xfail")]
    banned.append("skip" + "if")
    for name in banned:
        assert name not in body, (
            f"{name} appears in this file. Failing to locate either side of the eval-run status "
            "contract must be a failure: a contract check that skips reports green over an "
            "unverified contract."
        )
