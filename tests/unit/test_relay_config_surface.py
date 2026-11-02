# SPDX-License-Identifier: MIT
"""The relay's IaC-to-code contracts, each asserted from both sides.

1. The environment surface: the variables `infra/modules/agent_relay` sets against the ones
   `src/relay` reads, with Lambda runtime-provided variables classified separately.
2. The runtime API surface: the `nodejs<major>.x` the function deploys on against the `@types/node`
   major the code is compiled against.

Fully offline: parses the committed Terraform and TypeScript.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import pytest
from scripts.hcl_text import blank_literals, same_shape

REPO_ROOT = Path(__file__).resolve().parents[2]
RELAY_MODULE_DIR = REPO_ROOT / "infra" / "modules" / "agent_relay"
RELAY_MAIN_TF = RELAY_MODULE_DIR / "main.tf"
RELAY_OUTPUTS_TF = RELAY_MODULE_DIR / "outputs.tf"
RELAY_SRC_DIR = REPO_ROOT / "src" / "relay" / "src"
RELAY_DIR = REPO_ROOT / "src" / "relay"
RELAY_VARIABLES_TF = RELAY_MODULE_DIR / "variables.tf"

#: Lambda-reserved environment variables this relay could read. The Lambda API rejects a function
#: configuration that sets one, so "read but not set by Terraform" is correct for these. Source:
#: "Defined runtime environment variables", AWS Lambda Developer Guide.
RUNTIME_PROVIDED = frozenset(
    {
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_LAMBDA_FUNCTION_NAME",
        "AWS_LAMBDA_FUNCTION_VERSION",
        "AWS_LAMBDA_LOG_GROUP_NAME",
        "AWS_LAMBDA_LOG_STREAM_NAME",
        "AWS_EXECUTION_ENV",
        "LAMBDA_TASK_ROOT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)

#: One known member of each side, asserted present before any set arithmetic.
_SENTINEL_SET_VARIABLE = "AGENT_RUNTIME_ARN"
_SENTINEL_READ_VARIABLE = "COGNITO_ISSUER"


def _blank_literals(text: str) -> str:
    """`text` with every heredoc body and string literal replaced by spaces, length preserved.

    Offsets into the result index the original, so structure is located on the blanked copy and the
    real text is sliced.
    """
    blanked = blank_literals(text)
    assert same_shape(text, blanked), "hcl_text must preserve offsets"
    return blanked


def _brace_balanced_body(text: str, index: int) -> str:
    """The brace-balanced body of the block whose `{` is at or after `index`.

    Braces are counted on a literal-blanked copy, so a brace inside a string or heredoc is ignored.
    """
    blanked = _blank_literals(text)
    assert len(blanked) == len(text), "literal blanking must preserve offsets"
    start = blanked.index("{", index)
    depth = 0
    for position in range(start, len(blanked)):
        character = blanked[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : position]
    raise AssertionError(f"unbalanced braces from offset {index}")


def terraform_environment_variables(main_tf: Path = RELAY_MAIN_TF) -> set[str]:
    """Names of the environment variables the module sets, from `local.environment_variables`."""
    text = main_tf.read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*environment_variables\s*=\s*\{", text)
    assert match is not None, (
        f"no `environment_variables = {{...}}` local found in {main_tf}; the parser must fail "
        f"loudly rather than return an empty set that would make this whole module vacuous"
    )
    body = _brace_balanced_body(text, match.start())
    return set(re.findall(r"(?m)^\s*([A-Z][A-Z0-9_]*)\s*=", body))


def _relay_sources() -> list[Path]:
    return sorted(RELAY_SRC_DIR.rglob("*.ts"))


def _display(path: Path) -> str:
    """Repo-relative when it is under the repo, absolute otherwise."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _env_name_constants(text: str) -> dict[str, str]:
    """`const ENV_X = "NAME"` declarations, so a constant-mediated read resolves to its literal."""
    return dict(
        re.findall(
            r'(?m)^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=\s*"([^"]+)"',
            text,
        )
    )


def relay_environment_reads() -> tuple[set[str], list[str]]:
    """Environment variables the relay source reads, plus any access it could not resolve.

    Returns `(names, unresolved)`, where `unresolved` lists `env[expr]` accesses whose key is not a
    known constant.
    """
    names: set[str] = set()
    unresolved: list[str] = []

    for path in _relay_sources():
        text = path.read_text(encoding="utf-8")
        constants = _env_name_constants(text)
        # Only string-valued constants that look like an environment variable name are eligible, so a
        # `const DEFAULT_QUALIFIER = "DEFAULT"` cannot be mistaken for one.
        env_constants = {
            name: value
            for name, value in constants.items()
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", value)
        }

        # `env.FOO` / `process.env.FOO` — a direct literal read.
        names.update(re.findall(r"(?:process\.)?env\.([A-Z][A-Z0-9_]*)\b", text))

        # `env["FOO"]` / `process.env["FOO"]` — a direct quoted read.
        names.update(re.findall(r'(?:process\.)?env\[\s*"([A-Z][A-Z0-9_]*)"\s*\]', text))

        # `env[SOME_CONST]` — resolved through the constant table, or reported unresolved.
        for reference in re.findall(
            r"(?:process\.)?env\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]",
            text,
        ):
            if reference in env_constants:
                names.add(env_constants[reference])
            else:
                unresolved.append(f"{_display(path)}: env[{reference}]")

    return names, unresolved


def module_declared_output_names() -> set[str]:
    """The `local.environment_variables` references in `output "environment_variable_names"`.

    Searches the output body with string literals and heredocs blanked, so a `description` cannot
    satisfy the check and a `value` expression is found however it is wrapped. An empty set means
    the value is not derived from the local.
    """
    text = RELAY_OUTPUTS_TF.read_text(encoding="utf-8")
    match = re.search(r'(?m)^output\s+"environment_variable_names"\s*\{', text)
    assert match is not None, (
        'the module no longer declares `output "environment_variable_names"`, which is the contract '
        "a consumer reads to learn this surface"
    )
    body = _brace_balanced_body(text, match.start())
    assert re.search(r"(?m)^\s*value\s*=", body) is not None, (
        'output "environment_variable_names" declares no `value`, so it promises consumers nothing'
    )
    expressions = _blank_literals(body)
    return set(re.findall(r"local\.environment_variables", expressions))


# ---------------------------------------------------------------------------
# Non-vacuity controls.
# ---------------------------------------------------------------------------


def test_both_parsers_find_a_known_member() -> None:
    """Both parsers find their sentinel and at least three names."""
    set_names = terraform_environment_variables()
    read_names, _ = relay_environment_reads()
    assert _SENTINEL_SET_VARIABLE in set_names, (
        f"the Terraform parser did not find {_SENTINEL_SET_VARIABLE}; it found {sorted(set_names)}"
    )
    assert _SENTINEL_READ_VARIABLE in read_names, (
        f"the source parser did not find {_SENTINEL_READ_VARIABLE}; it found {sorted(read_names)}"
    )
    assert len(set_names) >= 3 and len(read_names) >= 3


def test_every_environment_access_in_the_relay_is_resolvable() -> None:
    """Every `env[expr]` access in the relay source resolves to a variable name."""
    _, unresolved = relay_environment_reads()
    assert not unresolved, (
        f"environment accesses this gate cannot resolve to a name: {unresolved}. Put the name behind "
        f'a `const ENV_X = "NAME"` literal so both sides of the contract stay checkable.'
    )


# ---------------------------------------------------------------------------
# The contract itself, both directions.
# ---------------------------------------------------------------------------


def test_nothing_the_relay_reads_is_left_unset() -> None:
    """Every variable the relay reads is set by the module or is in `RUNTIME_PROVIDED`."""
    read_names, _ = relay_environment_reads()
    missing = read_names - terraform_environment_variables() - RUNTIME_PROVIDED
    assert not missing, (
        f"the relay reads {sorted(missing)}, which `infra/modules/agent_relay` does not set and which "
        f"the Lambda runtime does not inject. The relay will throw on its first request."
    )


def test_nothing_set_by_the_iac_goes_unread() -> None:
    """Every variable the module sets is read by the relay source."""
    read_names, _ = relay_environment_reads()
    unread = terraform_environment_variables() - read_names
    assert not unread, (
        f"`infra/modules/agent_relay` sets {sorted(unread)}, which no file under {RELAY_SRC_DIR} "
        f"reads. Remove it together with whatever grant or parameter exists to serve it."
    )


def test_no_runtime_reserved_variable_is_set_by_the_iac() -> None:
    """The module sets no Lambda-reserved variable, which the Lambda API would reject."""
    overlap = terraform_environment_variables() & RUNTIME_PROVIDED
    assert not overlap, (
        f"`infra/modules/agent_relay` sets the Lambda-reserved variable(s) {sorted(overlap)}; the "
        f"CreateFunction/UpdateFunctionConfiguration API rejects this and the apply fails"
    )


def test_the_module_output_reports_the_real_surface_rather_than_a_second_copy() -> None:
    """`output "environment_variable_names"` derives from `local.environment_variables`."""
    assert module_declared_output_names(), (
        'output "environment_variable_names" does not derive from `local.environment_variables`; a '
        "hand-listed copy can drift from the environment block it claims to describe"
    )


def _output_with_value(
    tmp_path: Path, value_expression: str, description: str | None = None
) -> Path:
    """Write an `output "environment_variable_names"` block with the given `value` and `description`."""
    described = (
        "Names of every environment variable this module sets."
        if description is None
        else description
    )
    path = tmp_path / "outputs.tf"
    path.write_text(
        'output "environment_variable_names" {\n'
        f'  description = "{described}"\n'
        f"{value_expression}\n"
        "}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("label", "value_expression"),
    [
        ("single line, as committed", "  value       = sort(keys(local.environment_variables))"),
        ("terraform fmt wraps it", "  value = sort(keys(\n    local.environment_variables\n  ))"),
        (
            "a for expression",
            "  value = [\n    for k in keys(local.environment_variables) : k\n  ]",
        ),
        (
            "try() with a fallback",
            "  value = try(\n    sort(keys(local.environment_variables)),\n    []\n  )",
        ),
        ("keys(merge(...))", "  value = keys(merge(\n    local.environment_variables,\n  ))"),
    ],
)
def test_a_derived_value_is_recognised_however_it_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, value_expression: str
) -> None:
    r"""A derived `value` is recognised whether it fits one line or is wrapped across several."""
    monkeypatch.setitem(
        globals(), "RELAY_OUTPUTS_TF", _output_with_value(tmp_path, value_expression)
    )
    assert module_declared_output_names(), label


def test_a_hand_listed_value_beside_a_mentioning_description_is_not_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-listed `value` is not recognised because its `description` names the local."""
    monkeypatch.setitem(
        globals(),
        "RELAY_OUTPUTS_TF",
        _output_with_value(
            tmp_path,
            '  value = ["AGENT_RUNTIME_ARN", "COGNITO_ISSUER"]',
            description="Derived from local.environment_variables, honestly it is",
        ),
    )
    assert not module_declared_output_names()


def test_a_heredoc_description_cannot_satisfy_the_claim_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heredoc `description` naming the local does not make a hand-listed `value` count."""
    path = tmp_path / "outputs.tf"
    path.write_text(
        'output "environment_variable_names" {\n'
        "  description = <<-EOT\n"
        "    Derived from local.environment_variables.\n"
        "  EOT\n"
        '  value       = ["AGENT_RUNTIME_ARN"]\n'
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "RELAY_OUTPUTS_TF", path)
    assert not module_declared_output_names()


def test_an_output_with_no_value_at_all_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An output with no `value` raises AssertionError."""
    path = tmp_path / "outputs.tf"
    path.write_text(
        'output "environment_variable_names" {\n  description = "nothing here"\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "RELAY_OUTPUTS_TF", path)
    with pytest.raises(AssertionError, match="declares no `value`"):
        module_declared_output_names()


def test_the_environment_block_is_the_single_local_and_nothing_else() -> None:
    """The function's one `environment` block takes `variables` from the local."""
    text = RELAY_MAIN_TF.read_text(encoding="utf-8")
    blocks = re.findall(r"(?m)^\s*environment\s*\{", text)
    assert len(blocks) == 1, f"expected exactly one `environment` block, found {len(blocks)}"
    body = _brace_balanced_body(text, text.index(blocks[0]))
    assignment = re.search(r"(?m)^\s*variables\s*=\s*(\S+)\s*$", body)
    assert assignment is not None, f"no `variables = ...` in the environment block: {body!r}"
    assert assignment.group(1) == "local.environment_variables", (
        f"the function's environment variables come from {assignment.group(1)}, not from the local "
        f"this gate parses — so the surface it checks is not the surface that gets applied"
    )


# ---------------------------------------------------------------------------
# Retired config keys, pinned by name.
# ---------------------------------------------------------------------------


#: Config keys removed from the IaC, which no consumer may still look up. Add a line here whenever a
#: config key is removed.
RETIRED_CONFIG_KEYS: Final[frozenset[str]] = frozenset({"ORIGIN_VERIFICATION_SECRET_PARAMETER"})

#: Trees searched for a retired key used as a string literal; comment lines are ignored.
_RETIRED_KEY_SEARCH_GLOBS: Final[tuple[str, ...]] = (
    "scripts/**/*.py",
    "src/**/*.py",
    "src/relay/src/**/*.ts",
)


def _string_literals(text: str, suffix: str) -> list[str]:
    """Every single/double-quoted literal, with comment lines removed first."""
    marker = "#" if suffix == ".py" else "//"
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(marker))
    return re.findall(r"['\"]([^'\"\n]+)['\"]", body)


def _retired_keys_set_in_terraform(main_tf: Path) -> list[str]:
    """Every retired config key the relay module's `local.environment_variables` still sets."""
    return sorted(RETIRED_CONFIG_KEYS & terraform_environment_variables(main_tf))


def test_no_consumer_anywhere_still_looks_up_a_retired_config_key() -> None:
    """No retired config key is set by the relay module or named by a string literal in scripts,
    src or the relay source."""
    offenders = [
        f"{_display(RELAY_MAIN_TF)}: {key} (set)"
        for key in _retired_keys_set_in_terraform(RELAY_MAIN_TF)
    ]
    for pattern in _RETIRED_KEY_SEARCH_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path.resolve() == Path(__file__).resolve():
                continue
            literals = set(_string_literals(path.read_text(encoding="utf-8"), path.suffix))
            for key in sorted(RETIRED_CONFIG_KEYS & literals):
                offenders.append(f"{_display(path)}: {key}")
    assert not offenders, (
        f"retired config key(s) still set or used as a lookup key: {offenders}. Remove the setting "
        f"or the consumer, not the entry in RETIRED_CONFIG_KEYS."
    )


def test_the_retired_key_scan_would_catch_a_reintroduction(tmp_path: Path) -> None:
    """The retired-key scan detects a string literal and ignores a mention in a comment."""
    key = next(iter(RETIRED_CONFIG_KEYS))
    planted = tmp_path / "consumer.py"
    planted.write_text(f'NAME = "{key}"\n', encoding="utf-8")
    assert key in set(_string_literals(planted.read_text(encoding="utf-8"), ".py"))

    discussed_py = tmp_path / "prose.py"
    discussed_py.write_text(f"# {key} is gone\n", encoding="utf-8")
    assert key not in set(_string_literals(discussed_py.read_text(encoding="utf-8"), ".py"))

    discussed_ts = tmp_path / "prose.ts"
    discussed_ts.write_text(f"// no 503, because {key} is retired\n", encoding="utf-8")
    assert key not in set(_string_literals(discussed_ts.read_text(encoding="utf-8"), ".ts"))


def test_the_retired_key_check_reds_when_terraform_sets_a_retired_key(tmp_path: Path) -> None:
    """A retired key set in a copy of the module's `environment_variables` is reported."""
    key = next(iter(RETIRED_CONFIG_KEYS))
    text = RELAY_MAIN_TF.read_text(encoding="utf-8")
    anchor = re.search(r"(?m)^\s*environment_variables\s*=\s*\{\n", text)
    assert anchor is not None
    planted = tmp_path / "main.tf"
    planted.write_text(
        f'{text[: anchor.end()]}    {key} = "x"\n{text[anchor.end() :]}', encoding="utf-8"
    )
    assert _retired_keys_set_in_terraform(RELAY_MAIN_TF) == []
    assert _retired_keys_set_in_terraform(planted) == [key]


# ---------------------------------------------------------------------------
# Contract 2: the Node API surface the relay is compiled against vs the one it deploys on.
# ---------------------------------------------------------------------------


def lambda_runtime_major() -> int:
    """The major version of the `runtime` default in the relay module (`nodejs24.x` -> 24)."""
    text = RELAY_VARIABLES_TF.read_text(encoding="utf-8")
    match = re.search(r'(?ms)^variable\s+"runtime"\s*\{.*?default\s*=\s*"nodejs(\d+)\.x"', text)
    assert match is not None, (
        f'no `variable "runtime"` with a `default = "nodejs<major>.x"` in {RELAY_VARIABLES_TF}; the '
        f"parser must fail loudly rather than let this contract pass against a default it never found"
    )
    return int(match.group(1))


def types_node_major() -> int:
    """The major version of `@types/node` the relay's lock file installs.

    Read from `package-lock.json`, the relay's install authority.
    """
    lock = json.loads((RELAY_DIR / "package-lock.json").read_text(encoding="utf-8"))
    entry = lock["packages"]["node_modules/@types/node"]
    version = str(entry["version"])
    match = re.match(r"(\d+)\.", version)
    assert match is not None, f"cannot read a major out of @types/node version {version!r}"
    return int(match.group(1))


def test_the_relay_is_typechecked_against_the_node_version_it_deploys_on() -> None:
    """`@types/node`'s major equals the Lambda runtime's major.

    `scripts/package_relay.sh` checks the build interpreter's version; this checks the type surface.
    """
    runtime_major = lambda_runtime_major()
    types_major = types_node_major()
    assert types_major == runtime_major, (
        f"src/relay compiles against @types/node {types_major}.x but the function deploys on "
        f"nodejs{runtime_major}.x. If types are newer, `tsc` now accepts APIs the runtime lacks and the "
        f"failure is a TypeError on the first invocation behind a green apply. Change whichever is "
        f"wrong — the runtime default in {RELAY_VARIABLES_TF.name}, or the @types/node pin."
    )


def test_the_relay_tsconfig_actually_consumes_the_node_types() -> None:
    """The relay's `tsconfig.json` `types` array includes `node`."""
    tsconfig = (RELAY_DIR / "tsconfig.json").read_text(encoding="utf-8")
    match = re.search(r'"types"\s*:\s*\[([^\]]*)\]', tsconfig)
    assert match is not None, "src/relay/tsconfig.json declares no `types` array"
    declared = [item.strip().strip('"') for item in match.group(1).split(",") if item.strip()]
    assert "node" in declared, (
        f"src/relay/tsconfig.json's `types` is {declared}, which does not include `node` — so the "
        f"@types/node pin constrains nothing and the runtime-major contract above is vacuous"
    )


class TestBraceCountingIgnoresProse:
    """`_brace_balanced_body` delimits on structure, not on braces inside strings."""

    HAND_LISTED_WITH_A_PROSE_BRACE = """output "environment_variable_names" {
  description = "the opening { of the environment block"
  value       = ["A", "B"]
}

output "other" {
  value = sort(keys(local.environment_variables))
}

output "third" {
  value = "x}"
}
"""

    def _derives(self, text: str) -> bool:
        match = re.search(r'(?m)^output\s+"environment_variable_names"\s*\{', text)
        assert match is not None
        body = _brace_balanced_body(text, match.start())
        expressions = _blank_literals(body)
        return bool(re.search(r"local\.environment_variables", expressions))

    def test_a_brace_in_prose_does_not_move_the_block_boundary(self) -> None:
        """A prose brace in a description does not make the body absorb a neighbouring output."""
        assert not self._derives(self.HAND_LISTED_WITH_A_PROSE_BRACE)

    def test_the_body_does_not_absorb_the_following_block(self) -> None:
        """The extracted body does not contain the following `output` block."""
        match = re.search(
            r'(?m)^output\s+"environment_variable_names"\s*\{', self.HAND_LISTED_WITH_A_PROSE_BRACE
        )
        assert match is not None
        body = _brace_balanced_body(self.HAND_LISTED_WITH_A_PROSE_BRACE, match.start())
        assert 'output "other"' not in body, f"the body swallowed the next block: {body!r}"

    def test_blanking_preserves_length_so_offsets_stay_valid(self) -> None:
        """`_blank_literals` preserves the text's length."""
        for text in (
            self.HAND_LISTED_WITH_A_PROSE_BRACE,
            'a = "x"\nb = <<-EOT\n  body\nEOT\n',
            'escaped = "a \\" b"\n',
        ):
            assert len(_blank_literals(text)) == len(text), text

    def test_a_real_brace_still_delimits(self) -> None:
        """A block whose `value` derives from the local is recognised."""
        text = 'output "environment_variable_names" {\n  value = sort(keys(local.environment_variables))\n}\n'
        assert self._derives(text)


class TestTheHeaderAnchorMustNotRunOnBlankedText:
    """`module_declared_output_names` searches its quoted-label anchor on the raw text.

    `blank_literals` erases quoted labels, so the anchor would not match a blanked copy.
    """

    def test_blanking_erases_this_gates_own_anchor(self) -> None:
        """The anchor matches the committed outputs.tf and does not match its blanked copy."""
        text = RELAY_OUTPUTS_TF.read_text(encoding="utf-8")
        anchor = re.compile(r'(?m)^output\s+"environment_variable_names"\s*\{')
        assert anchor.search(text) is not None, (
            "the anchor must match the real committed outputs.tf"
        )
        assert anchor.search(blank_literals(text)) is None, (
            "blank_literals no longer erases quoted labels, so this gate's ordering hazard is gone and "
            "this test is stale — check hcl_text before deleting it"
        )

    def test_the_parser_still_locates_the_output_and_reads_its_derivation(self) -> None:
        """On the committed tree the parser locates the output and finds the local reference."""
        assert module_declared_output_names() == {"local.environment_variables"}
