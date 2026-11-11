# SPDX-License-Identifier: MIT
"""Plan time: the frontend CDN's response-headers policy enforces the expected
`Content-Security-Policy`, keeps the standard security headers, and allows the SPA's inline
theme-boot script by the hash of the script that `frontend/index.html` actually carries.

`frontend_cdn` reads live AWS data, so the policy resource, the `locals` block that renders the CSP
and the variables it reads are copied from the committed HCL into a scratch module and planned
offline (`_terraform_fixture`). The script hashes are the root module's own `csp_script_hashes`
expression, evaluated over a copy of `index.html` and compared with the hash a browser computes for
each inline script. Skips when terraform or the cached providers are absent, unless
`CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments

from tests.unit._terraform_fixture import (
    _PROVIDER_BLOCK,
    MODULES_DIR,
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_directory,
    require_terraform,
    terraform_unavailable_reason,
)

MODULE_DIR: Final = MODULES_DIR / "frontend_cdn"
CLOUDFRONT_TF: Final = MODULE_DIR / "cloudfront.tf"
VARIABLES_TF: Final = MODULE_DIR / "variables.tf"
ROOT_TF: Final = REPO_ROOT / "infra" / "main.tf"
INDEX_HTML: Final = REPO_ROOT / "frontend" / "index.html"

POLICY_HEADER: Final = 'resource "aws_cloudfront_response_headers_policy" "security_headers"'
POLICY_ADDRESS: Final = "module.subject.aws_cloudfront_response_headers_policy.security_headers"
CSP_VARIABLES: Final = ("csp_script_hashes", "csp_connect_src_extra")
EXTRACTED_VARIABLES: Final = ("name_prefix", *CSP_VARIABLES)

FIXTURE_CONNECT: Final = (
    "https://cognito-idp.us-east-1.amazonaws.com",
    "https://fixture-domain.auth.us-east-1.amazoncognito.com",
)

#: An inline `<script>` with no attributes: the only kind a `script-src` hash governs here.
_INLINE_SCRIPT: Final = re.compile(r"<script>(.*?)</script>", re.DOTALL)


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


def _csp_hash(script: str) -> str:
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def _inline_script_hashes(html: str) -> list[str]:
    return [_csp_hash(body) for body in _INLINE_SCRIPT.findall(html)]


def _hcl_list(values: tuple[str, ...] | list[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


def _block(text: str, header: str) -> str:
    """The one column-0 block starting with `header`, brace-matched."""
    starts = [m.start() for m in re.finditer(rf"(?m)^{re.escape(header)}", text)]
    assert len(starts) == 1, f"expected one {header!r} block, got {len(starts)}"
    depth = 0
    opened = False
    for index in range(starts[0], len(text)):
        if text[index] == "{":
            depth += 1
            opened = True
        elif text[index] == "}":
            depth -= 1
            if opened and depth == 0:
                return text[starts[0] : index + 1]
    raise AssertionError(f"unterminated {header!r}")


def _scratch_module_hcl() -> str:
    cloudfront = blank_comments(CLOUDFRONT_TF.read_text(encoding="utf-8"))
    variables = blank_comments(VARIABLES_TF.read_text(encoding="utf-8"))
    blocks = [_block(cloudfront, "locals {"), _block(cloudfront, POLICY_HEADER)]
    blocks += [_block(variables, f'variable "{name}"') for name in EXTRACTED_VARIABLES]
    text = "\n\n".join(blocks) + "\n"
    references = set(re.findall(r"\b(?:data\.)?aws_[a-z0-9_]+\.[a-z0-9_]+", text))
    assert references == set(), f"the extracted blocks reference other resources: {references}"
    return text


def _bracketed(text: str, start: int) -> str:
    """The bracket-balanced `[...]` starting at the first `[` at or after `start`."""
    opening = text.index("[", start)
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return text[opening : index + 1]
    raise AssertionError(f"unbalanced brackets from offset {start}")


def _root_script_hashes_expression() -> str:
    """The HCL expression the root module passes as `module.frontend_cdn.csp_script_hashes`.

    A `local.<name>` reference is resolved to that local's list expression.
    """
    root = blank_comments(ROOT_TF.read_text(encoding="utf-8"))
    module = _block(root, 'module "frontend_cdn"')
    argument = re.search(r"(?m)^\s*csp_script_hashes\s*=\s*(\S.*)$", module)
    assert argument is not None, "the root passes no csp_script_hashes to frontend_cdn"
    value = argument.group(1).strip()
    reference = re.fullmatch(r"local\.([a-z0-9_]+)", value)
    if reference is None:
        return _bracketed(module, argument.start(1))
    definitions = list(re.finditer(rf"(?m)^\s*{reference.group(1)}\s*=\s*\[", root))
    assert len(definitions) == 1, f"expected one definition of local.{reference.group(1)}"
    return _bracketed(root, definitions[0].start())


def _write_root(
    work_dir: Path,
    overrides: dict[str, str] | None = None,
    *,
    index_html: str | None = None,
) -> None:
    """Write the fixture root, evaluating the root's hash expression over a copy of `index.html`.

    The copy sits at `<work_dir>/../frontend/index.html`, where the root's `${path.module}/..`
    resolves; `index_html` replaces its content.
    """
    frontend = work_dir.parent / "frontend"
    frontend.mkdir(parents=True, exist_ok=True)
    html = INDEX_HTML.read_text(encoding="utf-8") if index_html is None else index_html
    (frontend / INDEX_HTML.name).write_text(html, encoding="utf-8")
    subject = work_dir / "subject"
    subject.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MODULE_DIR / "versions.tf", subject / "versions.tf")
    (subject / "policy.tf").write_text(_scratch_module_hcl(), encoding="utf-8")
    arguments = {
        "name_prefix": '"fixture-"',
        "csp_script_hashes": "local.root_csp_script_hashes",
        "csp_connect_src_extra": _hcl_list(FIXTURE_CONNECT),
        **(overrides or {}),
    }
    body = "\n".join(f"  {key} = {value}" for key, value in arguments.items())
    (work_dir / "main.tf").write_text(
        f"{_PROVIDER_BLOCK}\n"
        f"locals {{\n  root_csp_script_hashes = {_root_script_hashes_expression()}\n}}\n\n"
        f'module "subject" {{\n  source = "./subject"\n{body}\n}}\n',
        encoding="utf-8",
    )


def _plan(
    work_dir: Path,
    overrides: dict[str, str] | None = None,
    *,
    index_html: str | None = None,
) -> Plan:
    _write_root(work_dir / "root", overrides, index_html=index_html)
    return plan_directory(work_dir / "root")


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan(tmp_path_factory.mktemp("frontend-csp"))


def _headers_config(plan: Plan) -> dict[str, Any]:
    configs = plan.after(POLICY_ADDRESS)["security_headers_config"]
    assert isinstance(configs, list) and len(configs) == 1, configs
    config = configs[0]
    assert isinstance(config, dict)
    return config


def _only(config: dict[str, Any], key: str) -> dict[str, Any]:
    entries = config[key]
    assert isinstance(entries, list) and len(entries) == 1, (key, entries)
    entry = entries[0]
    assert isinstance(entry, dict)
    return entry


def _csp(plan: Plan) -> str:
    value = _only(_headers_config(plan), "content_security_policy")["content_security_policy"]
    assert isinstance(value, str)
    return value


def _directives(policy: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for directive in policy.split(";"):
        name, *sources = directive.split()
        assert name not in parsed, f"directive {name} appears twice"
        parsed[name] = sources
    return parsed


# ---------------------------------------------------------------------------
# The inline script the hash pins.
# ---------------------------------------------------------------------------


def test_index_html_carries_exactly_one_inline_script() -> None:
    """The control for the hash test: the parser finds the theme-boot script and nothing else."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    bodies = _INLINE_SCRIPT.findall(html)
    assert len(bodies) == 1, bodies
    assert "data-theme" in bodies[0]
    attributed = re.findall(r"<script\s[^>]*>", html)
    assert all(re.search(r"\bsrc=", tag) for tag in attributed), attributed


def test_a_changed_inline_script_changes_its_hash() -> None:
    """The hash discriminates on the script body, so an edited script no longer matches."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    edited = html.replace("data-theme", "data-theme-x", 1)
    assert _inline_script_hashes(edited) != _inline_script_hashes(html)


def test_the_root_passes_the_hash_expression_or_list_for_csp_script_hashes() -> None:
    """The parse finds a list expression for the root's `csp_script_hashes`."""
    expression = _root_script_hashes_expression()
    assert expression.startswith("[") and expression.endswith("]"), expression


# ---------------------------------------------------------------------------
# The planned policy.
# ---------------------------------------------------------------------------


def test_the_planned_csp_is_exactly_the_designed_directive_set(plan: Plan) -> None:
    (script_hash,) = _inline_script_hashes(INDEX_HTML.read_text(encoding="utf-8"))
    assert _csp(plan) == "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' '{script_hash}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self' " + " ".join(FIXTURE_CONNECT),
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "form-action 'self'",
        ]
    )


def test_the_planned_script_src_allows_the_served_inline_script(plan: Plan) -> None:
    hashes = _inline_script_hashes(INDEX_HTML.read_text(encoding="utf-8"))
    script_src = _directives(_csp(plan))["script-src"]
    for script_hash in hashes:
        assert f"'{script_hash}'" in script_src, script_src


def test_the_planned_script_hashes_track_an_edited_inline_script(tmp_path: Path) -> None:
    """With the root's value evaluated over an edited `index.html`, the plan carries the new hash
    and not the old one, so the policy cannot keep allowing a script the page no longer serves."""
    _skip_unless_terraform()
    html = INDEX_HTML.read_text(encoding="utf-8")
    edited = html.replace('"data-theme", theme', '"data-theme", theme || "light"', 1)
    assert edited != html, "the edit did not apply; the control would be vacuous"
    script_src = _directives(_csp(_plan(tmp_path, index_html=edited)))["script-src"]
    assert [f"'{h}'" for h in _inline_script_hashes(edited)] == script_src[1:], script_src
    assert all(f"'{h}'" not in script_src for h in _inline_script_hashes(html)), script_src


def test_no_directive_allows_eval_and_only_styles_allow_inline(plan: Plan) -> None:
    directives = _directives(_csp(plan))
    assert all("'unsafe-eval'" not in sources for sources in directives.values()), directives
    inline = {name for name, sources in directives.items() if "'unsafe-inline'" in sources}
    assert inline == {"style-src"}, directives
    assert all("*" not in source for sources in directives.values() for source in sources)


def test_the_csp_is_enforced_and_overrides_the_origin(plan: Plan) -> None:
    """Delivered as `Content-Security-Policy` (the enforcing header), never Report-Only."""
    entry = _only(_headers_config(plan), "content_security_policy")
    assert entry["override"] is True
    assert "Report-Only" not in CLOUDFRONT_TF.read_text(encoding="utf-8")


def test_the_managed_security_headers_are_kept(plan: Plan) -> None:
    config = _headers_config(plan)
    assert _only(config, "content_type_options")["override"] is True
    assert _only(config, "frame_options") == {"frame_option": "DENY", "override": True}
    assert _only(config, "referrer_policy") == {
        "referrer_policy": "strict-origin-when-cross-origin",
        "override": True,
    }
    xss = _only(config, "xss_protection")
    assert (xss["protection"], xss["mode_block"], xss["override"]) == (True, True, True)


def test_hsts_is_one_year_with_include_subdomains_and_preload(plan: Plan) -> None:
    assert _only(_headers_config(plan), "strict_transport_security") == {
        "access_control_max_age_sec": 31536000,
        "include_subdomains": True,
        "preload": True,
        "override": True,
    }


def test_the_policy_adds_no_cors_or_custom_headers(plan: Plan) -> None:
    after = plan.after(POLICY_ADDRESS)
    assert not after.get("cors_config"), after.get("cors_config")
    assert not after.get("custom_headers_config"), after.get("custom_headers_config")
    assert not after.get("remove_headers_config"), after.get("remove_headers_config")


def test_the_policy_name_is_prefixed(plan: Plan) -> None:
    assert plan.after(POLICY_ADDRESS)["name"] == "fixture-frontend-security-headers"


# ---------------------------------------------------------------------------
# Variable contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variable", CSP_VARIABLES)
def test_csp_inputs_have_no_default(variable: str) -> None:
    block = _block(blank_comments(VARIABLES_TF.read_text("utf-8")), f'variable "{variable}"')
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), block
    assert re.search(r"^\s*type\s*=\s*list\(string\)\s*$", block, re.MULTILINE), block


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("csp_script_hashes", "[\"'sha256-zKvj1tkJ70GikBqJ1UMWp8MS4dUXm9hbi2a8Yo3ItNs='\"]"),
        ("csp_script_hashes", '["sha256-short="]'),
        ("csp_script_hashes", '["unsafe-inline"]'),
        ("csp_connect_src_extra", '["https://*.amazonaws.com"]'),
        ("csp_connect_src_extra", '["http://cognito-idp.us-east-1.amazonaws.com"]'),
        ("csp_connect_src_extra", '["https://example.test/path"]'),
        ("csp_connect_src_extra", '["https://example.test; script-src *"]'),
        ("csp_connect_src_extra", '["*"]'),
    ],
)
def test_module_validation_rejects(tmp_path: Path, variable: str, value: str) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, {variable: value})
    message = str(raised.value)
    assert "Invalid value for variable" in message, message
    assert variable in message, message


def test_module_validation_admits_empty_extras(tmp_path: Path) -> None:
    """The control for the rejections: valid shapes plan, and no extra leaves `connect-src 'self'`."""
    _skip_unless_terraform()
    planned = _plan(tmp_path, {"csp_connect_src_extra": "[]"})
    assert _directives(_csp(planned))["connect-src"] == ["'self'"]
