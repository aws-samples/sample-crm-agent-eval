# SPDX-License-Identifier: MIT
"""The JWT header contract across the relay, the SPA, CloudFront and the Python live client.

The Cognito bearer token travels in `X-Crm-Authorization`, since under CloudFront OAC with
`signing_behavior = "always"` CloudFront's SigV4 overwrites `Authorization`. These tests read
`src/relay/src/jwt.ts`, `frontend/src/lib/agentClient.ts`, `infra/modules/frontend_cdn/cloudfront.tf`
and `live_source` in `scripts/smoke_agent_stream.py`. The relay↔SPA leg is also pinned in
`frontend/src/hooks/useAgentStream.test.ts`.

CloudFront forwards the header through `aws_cloudfront_origin_request_policy.agent_relay`'s
`header_behavior = "allExcept"` over `["host"]`, attached to the `/agent/*` behaviour. Header names are
compared case-insensitively: the relay's constant is lowercase and the SPA's is canonical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELAY_JWT_TS = _REPO_ROOT / "src" / "relay" / "src" / "jwt.ts"
_SPA_AGENT_CLIENT_TS = _REPO_ROOT / "frontend" / "src" / "lib" / "agentClient.ts"
_CDN_CLOUDFRONT_TF = _REPO_ROOT / "infra" / "modules" / "frontend_cdn" / "cloudfront.tf"

#: `export const JWT_HEADER = "…"`, the declaration in both the relay and the SPA.
_JWT_HEADER_CONSTANT = re.compile(r"""^export const JWT_HEADER = "([^"]+)";\s*$""", re.MULTILINE)

#: The relay's read site of `JWT_HEADER`.
_RELAY_READ_SITE = re.compile(r"\bheaderValue\(\s*event\.headers,\s*JWT_HEADER\s*\)")

#: Any `authorization` identifier or string in relay code, after comments are stripped. The lookbehind
#: excludes the `-authorization` suffix of `x-crm-authorization`.
_AUTHORIZATION_READ = re.compile(r"(?<![-\w])authorization\b", re.I)


def _strip_comments(text: str) -> str:
    r"""Blank `//` and `/* … */` comments in TypeScript, leaving markers inside string literals alone.

    `#` is not a comment marker, since in TypeScript it is private-field syntax.
    """
    out: list[str] = []
    in_block = False
    for line in text.split("\n"):
        rendered: list[str] = []
        quote: str | None = None
        position = 0
        while position < len(line):
            char = line[position]
            if in_block:
                closing = line.startswith("*/", position)
                rendered.append("  " if closing else " ")
                position += 2 if closing else 1
                in_block = not closing
                continue
            if quote is not None:
                if char == "\\" and position + 1 < len(line):
                    rendered.append(line[position : position + 2])
                    position += 2
                    continue
                if char == quote:
                    quote = None
                rendered.append(char)
                position += 1
                continue
            if char in "\"'`":
                quote = char
                rendered.append(char)
                position += 1
                continue
            if line.startswith("/*", position):
                in_block = True
                rendered.append("  ")
                position += 2
                continue
            if line.startswith("//", position):
                rendered.append(" " * (len(line) - position))
                break
            rendered.append(char)
            position += 1
        out.append("".join(rendered))
    return "\n".join(out)


#: The relay's origin-request policy block, and its `header_behavior` argument.
_ORIGIN_REQUEST_POLICY = re.compile(
    r"""resource\s+"aws_cloudfront_origin_request_policy"\s+"agent_relay"\s*\{(?P<body>.*?)\n\}""",
    re.DOTALL,
)
_HEADER_BEHAVIOUR = re.compile(r"""^\s*header_behavior\s*=\s*"([^"]+)"\s*$""", re.MULTILINE)

#: The `items = [...]` of the exclusion list, inside the policy's `headers_config`.
_HEADERS_CONFIG = re.compile(r"""headers_config\s*\{(?P<body>.*?)\n  \}""", re.DOTALL)
_ITEMS = re.compile(r"""items\s*=\s*\[(?P<items>[^\]]*)\]""", re.DOTALL)

#: The `/agent/*` ordered cache behaviour, and the origin-request policy it attaches.
_AGENT_BEHAVIOUR = re.compile(
    r"""ordered_cache_behavior\s*\{(?P<body>[^{}]*?path_pattern\s*=\s*"/agent/\*".*?)\n  \}""",
    re.DOTALL,
)
_ATTACHED_POLICY = re.compile(r"""origin_request_policy_id\s*=\s*(?P<ref>[A-Za-z0-9_.\[\]"-]+)""")


def _one(pattern: re.Pattern[str], text: str, label: str) -> str:
    """Return the single capture of `pattern` in `text`, failing loudly on absence or ambiguity."""
    matches = pattern.findall(text)
    if not matches:
        pytest.fail(f"{label}: no match, so this contract test verified nothing")
    assert len(matches) == 1, f"{label}: expected exactly one declaration, found {matches}"
    return str(matches[0])


def _relay_header_name() -> str:
    return _one(_JWT_HEADER_CONSTANT, _RELAY_JWT_TS.read_text(encoding="utf-8"), "relay jwt.ts")


def _spa_header_name() -> str:
    return _one(
        _JWT_HEADER_CONSTANT,
        _SPA_AGENT_CLIENT_TS.read_text(encoding="utf-8"),
        "frontend agentClient.ts",
    )


def test_the_relay_reads_the_header_the_spa_sends() -> None:
    """The relay's header name equals the SPA's, case-insensitively."""
    assert _relay_header_name().lower() == _spa_header_name().lower()


def test_the_two_sides_are_the_expected_header_and_not_authorization() -> None:
    """Both the relay and the SPA name `x-crm-authorization`, case-insensitively."""
    assert _relay_header_name().lower() == "x-crm-authorization"
    assert _spa_header_name().lower() == "x-crm-authorization"


def test_the_relay_side_is_a_lowercase_http_token() -> None:
    """The relay's header constant is a lowercase HTTP token."""
    name = _relay_header_name()
    assert re.fullmatch(r"[a-z0-9-]+", name), name


def test_the_relay_reads_the_constant_rather_than_a_literal() -> None:
    """The relay handler reads its header via `headerValue(event.headers, JWT_HEADER)`."""
    source = _RELAY_JWT_TS.read_text(encoding="utf-8")
    index = (_REPO_ROOT / "src" / "relay" / "src" / "index.ts").read_text(encoding="utf-8")
    assert "JWT_HEADER" in index, "the handler does not reference JWT_HEADER at all"
    assert _RELAY_READ_SITE.search(index) is not None, (
        "the handler does not read its header via `headerValue(event.headers, JWT_HEADER)`"
    )
    assert 'export const JWT_HEADER = "' in source


def test_the_relay_never_reads_authorization_for_identity() -> None:
    """No relay source file names the `authorization` header outside comments."""
    scanned = 0
    for path in sorted((_REPO_ROOT / "src" / "relay" / "src").rglob("*.ts")):
        scanned += 1
        found = _AUTHORIZATION_READ.search(_strip_comments(path.read_text(encoding="utf-8")))
        assert found is None, (
            f"{path.relative_to(_REPO_ROOT)} names the {found.group()} header. Under OAC that header "
            "carries CloudFront's SigV4 and a viewer value in it is overwritten at the edge."
        )
    assert scanned >= 5, f"only {scanned} relay source file(s) scanned; the glob is wrong"


def test_the_cdn_forwards_by_exclusion_so_the_name_cannot_drift_from_terraform() -> None:
    """The agent_relay origin-request policy forwards headers with `header_behavior = "allExcept"`."""
    block = _ORIGIN_REQUEST_POLICY.search(_CDN_CLOUDFRONT_TF.read_text(encoding="utf-8"))
    assert block is not None, "frontend_cdn declares no agent_relay origin-request policy"
    behaviour = _one(_HEADER_BEHAVIOUR, block.group("body"), "agent_relay header_behavior")
    assert behaviour == "allExcept", (
        f"header_behavior is {behaviour!r}, not 'allExcept' — the JWT header is now forwarded by an "
        f"explicit list and must be named in it: add {_relay_header_name()!r} to that list, and assert "
        "it here instead of this exclusion form"
    )


def test_host_is_the_only_excluded_header() -> None:
    """`host` is the only header the agent_relay origin-request policy excludes."""
    block = _ORIGIN_REQUEST_POLICY.search(_CDN_CLOUDFRONT_TF.read_text(encoding="utf-8"))
    assert block is not None, "frontend_cdn declares no agent_relay origin-request policy"
    config = _HEADERS_CONFIG.search(block.group("body"))
    assert config is not None, "the agent_relay policy declares no headers_config"
    raw = _one(_ITEMS, config.group("body"), "agent_relay headers_config items")
    excluded = {part.strip().strip('"').lower() for part in raw.split(",") if part.strip()}
    assert excluded == {"host"}, (
        f"the /agent/* origin-request policy excludes {sorted(excluded)}, not just ['host'] — every "
        "name added here stops reaching the relay with no error raised anywhere"
    )


def test_the_agent_behaviour_attaches_that_policy() -> None:
    """The `/agent/*` cache behaviour attaches the agent_relay origin-request policy."""
    behaviour = _AGENT_BEHAVIOUR.search(_CDN_CLOUDFRONT_TF.read_text(encoding="utf-8"))
    assert behaviour is not None, "frontend_cdn declares no /agent/* ordered_cache_behavior"
    ref = _one(_ATTACHED_POLICY, behaviour.group("body"), "/agent/* origin_request_policy_id")
    assert ref == "aws_cloudfront_origin_request_policy.agent_relay.id", (
        f"/agent/* attaches {ref!r} rather than this module's own agent_relay policy, so what it "
        "forwards is no longer an assertable property of source"
    )


@pytest.mark.parametrize(
    ("path", "label"),
    [
        (_RELAY_JWT_TS, "relay jwt.ts"),
        (_SPA_AGENT_CLIENT_TS, "frontend agentClient.ts"),
    ],
)
def test_each_side_is_parsed_from_its_real_source(path: Path, label: str) -> None:
    """The `JWT_HEADER` pattern matches each named source file."""
    assert path.is_file(), f"{label}: {path} does not exist"
    assert _JWT_HEADER_CONSTANT.search(path.read_text(encoding="utf-8")) is not None, label


def test_the_comment_stripper_does_not_blank_a_read_that_follows_a_url() -> None:
    """The stripper keeps code after a `//` inside a string and still blanks a real trailing comment."""
    violating = 'const u = "https://host/x"; headers.authorization = t;\n'
    assert _AUTHORIZATION_READ.search(_strip_comments(violating)) is not None, (
        "a string-borne // hid a real Authorization read; the stripper is string-blind again"
    )

    commented = "const a = 1; // headers.authorization is deliberately not read\n"
    assert _AUTHORIZATION_READ.search(_strip_comments(commented)) is None, (
        "a genuine trailing comment is no longer blanked, so prose discussing the property will trip"
    )


def test_the_comment_stripper_leaves_a_typescript_private_field_alone() -> None:
    """The stripper leaves a TypeScript `#` private field unchanged."""
    source = "class C { #token = 1; read() { return this.#token; } }\n"

    assert _strip_comments(source) == source


def test_a_block_comment_spanning_lines_is_blanked_without_eating_the_code_after_it() -> None:
    """A multi-line block comment is blanked without blanking the code after its terminator."""
    source = "/*\n  discusses Authorization at length\n*/\nheaders.authorization = t;\n"

    result = _strip_comments(source)

    assert "discusses" not in result
    assert _AUTHORIZATION_READ.search(result) is not None, "the block comment ran on into live code"


# ------------------------------------------------------------------------------------------------
# The Python live client: `live_source` in `scripts/smoke_agent_stream.py`. Its runtime behaviour is
# covered by `tests/unit/test_smoke_agent_stream.py`.
# ------------------------------------------------------------------------------------------------

_SMOKE_STREAM_PY = _REPO_ROOT / "scripts" / "smoke_agent_stream.py"

#: `export const BODY_DIGEST_HEADER = "…"` in the SPA, the payload-digest header OAC requires on a POST.
_BODY_DIGEST_CONSTANT = re.compile(
    r"""^export const BODY_DIGEST_HEADER = "([^"]+)";\s*$""", re.MULTILINE
)


def _live_source_header_names() -> set[str]:
    """Return the header names `live_source` sets, read from its `headers=` dict by AST."""
    import ast

    tree = ast.parse(_SMOKE_STREAM_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "live_source"):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            for keyword in call.keywords:
                if keyword.arg == "headers" and isinstance(keyword.value, ast.Dict):
                    return {
                        key.value
                        for key in keyword.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
    raise AssertionError("live_source sets no headers= dict; the contract has nothing to read")


def test_the_python_live_client_sends_the_header_the_relay_reads() -> None:
    """`live_source` sends the relay's header name, case-insensitively."""
    names = {name.lower() for name in _live_source_header_names()}

    assert _relay_header_name().lower() in names


def test_the_python_live_client_sends_no_bare_authorization_header() -> None:
    """`live_source` sends no `Authorization` header."""
    names = {name.lower() for name in _live_source_header_names()}

    assert "authorization" not in names


def test_the_python_live_client_sends_the_body_digest_header_the_spa_names() -> None:
    """`live_source` sends the SPA's `BODY_DIGEST_HEADER`."""
    digest_header = _one(
        _BODY_DIGEST_CONSTANT,
        _SPA_AGENT_CLIENT_TS.read_text(encoding="utf-8"),
        "SPA agentClient.ts",
    )
    names = {name.lower() for name in _live_source_header_names()}

    assert digest_header.lower() in names


def test_the_fourth_side_is_read_from_the_real_file_and_would_notice_its_absence() -> None:
    """The `live_source` header parse is non-empty and includes `Content-Type`."""
    names = _live_source_header_names()

    assert names, "the header set is empty, so the negative assertions prove nothing"
    assert "Content-Type" in names, (
        f"unexpected header set, parser may be reading the wrong dict: {names!r}"
    )
