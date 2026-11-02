# SPDX-License-Identifier: MIT
"""Offline assertions on the API Gateway invoke grant: its scope, and that there is exactly one of it.

The subject is `modules/api`'s `aws_lambda_permission.apigw_invoke`, whose `for_each` is the route map
and whose method is read from `local.route_methods`. These tests read Terraform source because
`source_arn` is unknown at plan time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_INFRA: Final[Path] = Path(__file__).resolve().parents[2] / "infra"
_ROOT_TF: Final[Path] = _INFRA / "main.tf"
_API_TF: Final[Path] = _INFRA / "modules" / "api" / "main.tf"
_LAMBDA_ACTION_DIR: Final[Path] = _INFRA / "modules" / "lambda_action"

_API_GATEWAY_PRINCIPAL: Final[str] = "apigateway.amazonaws.com"


def _api_module() -> str:
    return _API_TF.read_text(encoding="utf-8")


def _root() -> str:
    return _ROOT_TF.read_text(encoding="utf-8")


def _resource_block(text: str, kind: str, name: str) -> str:
    """The body of `resource "<kind>" "<name>"` in `text`, or fail naming what was searched for."""
    m = re.search(
        rf'^resource "{re.escape(kind)}" "{re.escape(name)}" \{{\n(?P<body>.*?)^\}}$',
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None, f'no `resource "{kind}" "{name}"` block was found'
    return m.group("body")


def _apigw_invoke_block() -> str:
    return _resource_block(_api_module(), "aws_lambda_permission", "apigw_invoke")


def _source_arn(body: str) -> str:
    """The raw `source_arn` expression of a permission block."""
    m = re.search(r'^\s*source_arn\s*=\s*(?P<value>".*")$', body, re.MULTILINE)
    assert m is not None, f"the permission block declares no single-line `source_arn`: {body!r}"
    return m.group("value")


def test_the_only_api_gateway_grant_is_not_method_or_path_agnostic() -> None:
    """`/*/*` spans stage, method AND path, so it lets every route invoke every function."""
    arn = _source_arn(_apigw_invoke_block())

    assert "/*/*" not in arn, (
        f"the API Gateway invoke grant is method- and path-agnostic: {arn}. A trailing wildcard applies "
        "to the rest of the expression, so `/*/*` lets every route on the API invoke every function."
    )
    # Non-vacuity: a grant that named no path at all would also contain no `/*/*`.
    assert "each.key" in arn, (
        f"the grant does not interpolate `each.key`, so it is not scoped to one path: {arn}"
    )


def test_the_grants_method_is_derived_from_the_route_table_and_never_a_literal() -> None:
    """The grant's method is read from `local.route_methods[each.key]`, never a literal method."""
    arn = _source_arn(_apigw_invoke_block())

    assert "local.route_methods[each.key]" in arn, (
        f"the grant does not read `local.route_methods[each.key]` for its method: {arn}. A literal "
        "method would grant a method the route may not serve, and nothing else now covers that."
    )
    assert not re.search(r"/\*/(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)/", arn), (
        f"the grant hardcodes an HTTP method instead of deriving it: {arn}"
    )


def test_the_grant_covers_exactly_the_routed_functions() -> None:
    """The grant iterates `local.route_function_names`, so an unrouted function receives no grant."""
    body = _apigw_invoke_block()
    m = re.search(r"^\s*for_each\s*=\s*(?P<value>\S+)$", body, re.MULTILINE)

    assert m is not None, f"the grant declares no `for_each`: {body!r}"
    assert m.group("value") == "local.route_function_names", (
        f"the grant iterates {m.group('value')!r} rather than `local.route_function_names`. Only the set "
        "of functions the API actually routes to may receive an API Gateway principal."
    )


def test_the_root_excludes_agent_only_tools_from_the_route_maps_by_reference() -> None:
    """The root's route maps exclude agent-only tools by reference to `local.agent_only_tools`."""
    root = _root()

    m = re.search(
        r"routed_tool_names\s*=\s*\[for name in local\.tool_names\s*:\s*name if\s*(?P<cond>[^\]]+)\]",
        root,
    )
    assert m is not None, (
        "`local.routed_tool_names` is no longer a comprehension over `local.tool_names`"
    )
    assert "local.agent_only_tools" in m.group("cond"), (
        f"`routed_tool_names` does not exclude by reference to `local.agent_only_tools`: {m.group('cond')!r}"
    )

    # And the maps handed to `module.api` must be built from that derived list, not from every tool.
    for argument in ("tool_lambda_invoke_arns", "tool_lambda_function_names"):
        m = re.search(rf"{argument}\s*=\s*\{{\s*for name in (?P<src>[\w.]+)\s*:", root)
        assert m is not None, f"`module.api`'s `{argument}` is no longer a comprehension"
        assert m.group("src") == "local.routed_tool_names", (
            f"`{argument}` is built from {m.group('src')!r} rather than `local.routed_tool_names`, so an "
            "agent-only tool would be given an HTTP route and a grant for it."
        )


def test_lambda_action_declares_no_api_gateway_permission_at_all() -> None:
    """No `aws_lambda_permission` in any `modules/lambda_action/*.tf` grants the API Gateway principal."""
    sources = sorted(_LAMBDA_ACTION_DIR.glob("*.tf"))
    assert sources, f"no .tf files found under {_LAMBDA_ACTION_DIR}, so this cell scanned nothing"

    principals: dict[str, str] = {}
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r'^resource "aws_lambda_permission" "(?P<name>[^"]+)" \{\n(?P<body>.*?)^\}$',
            text,
            re.DOTALL | re.MULTILINE,
        ):
            principal = re.search(
                r"^\s*principal\s*=\s*(?P<value>.+)$", m.group("body"), re.MULTILINE
            )
            principals[f"{path.name}:{m.group('name')}"] = (
                principal.group("value").strip() if principal else "<none>"
            )

    # The module declares a Gateway-role permission, so an empty parse means the regex stopped matching.
    assert principals, (
        f"no aws_lambda_permission block was parsed from {[p.name for p in sources]}, so this cell "
        "asserted nothing about grants"
    )

    offenders = {
        where: value for where, value in principals.items() if _API_GATEWAY_PRINCIPAL in value
    }
    assert not offenders, (
        f"{sorted(offenders)} grant {_API_GATEWAY_PRINCIPAL!r} in modules/lambda_action. The API "
        "Gateway invoke grant belongs to `modules/api` alone; a second grant here duplicates it "
        "and masks whether the per-route one is correct."
    )
