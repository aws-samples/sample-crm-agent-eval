# SPDX-License-Identifier: MIT
"""Machine-enforces the non-drift property `scripts/package_lambdas.sh` claims.

Three independent assertions, keyed on the property rather than on the five distributions vendored
today: the script carries no version literal at all; every `pip install` in it takes its packages
from a derived array rather than from operands written in place; and `--print-pins` resolves each
declared distribution to exactly the version `requirements.txt` pins, failing when it cannot.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "package_lambdas.sh"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

#: `name==version` as pip would accept it.
_VERSION_LITERAL = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*==\s*\d")


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _declared_seed_scopes() -> set[str]:
    """Scope labels implied by the script's `*_SEED_NAMES` arrays: `FOO_BAR_SEED_NAMES` -> `foo_bar`.

    Read from the seed arrays, not from the `printf` lines that emit `--print-pins` output.
    """
    return {
        name.removesuffix("_SEED_NAMES").lower()
        for name in re.findall(r"^([A-Z_]+_SEED_NAMES)=\(", _script_text(), re.M)
    }


def _requirement_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        name, version = line.split("==", 1)
        pins[re.sub(r"[-_.]+", "-", name).lower()] = version.strip()
    return pins


def _print_pins(*, requirements: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if requirements is not None:
        env["REQUIREMENTS_FILE"] = str(requirements)
    return proc_run.run(
        ["bash", str(SCRIPT), "--print-pins"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def test_script_carries_no_version_literal() -> None:
    """No `name==version` may appear in the script; versions come from the manifest only."""
    offenders = [
        f"{lineno}: {line.strip()}"
        for lineno, line in enumerate(_script_text().splitlines(), start=1)
        if _VERSION_LITERAL.search(line)
    ]
    assert offenders == [], (
        "package_lambdas.sh must derive every vendored version from requirements.txt; "
        f"version literal(s) found: {offenders}"
    )


def test_every_pip_install_takes_its_packages_from_a_derived_array() -> None:
    """A `pip install` operand written in place would bypass the derivation entirely."""
    # Comment lines are excluded; prose mentioning `pip install` is not an invocation.
    text = "\n".join(
        line for line in _script_text().splitlines() if not line.lstrip().startswith("#")
    )
    installs = [
        match.group(0) for match in re.finditer(r"pip install(?:[^\n\\]*\\\n)*[^\n]*", text)
    ]
    assert installs, "expected at least one pip install invocation to inspect"
    for invocation in installs:
        operands = re.findall(r'"\$\{_?[A-Z_]+\[@\]\}"', invocation)
        assert operands, (
            "every pip install must take its packages from a requirements-derived array; "
            f"found none in: {invocation!r}"
        )


def test_print_pins_resolves_every_declared_distribution_to_the_manifest_pin() -> None:
    result = _print_pins()
    assert result.returncode == 0, result.stderr
    pins = _requirement_pins()
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "expected --print-pins to emit at least one pin"
    expected_scopes = _declared_seed_scopes()
    assert expected_scopes, "no *_SEED_NAMES arrays found in the script"
    emitted_scopes = {line.split(" ", 1)[0] for line in lines}
    assert emitted_scopes == expected_scopes, (
        f"--print-pins emitted scopes {sorted(emitted_scopes)} but the script declares seed arrays "
        f"for {sorted(expected_scopes)}; a seed array that prints nothing is a zip whose vendored "
        "closure is never checked against requirements.txt"
    )
    for line in lines:
        scope, spec = line.split(" ", 1)
        assert scope in expected_scopes
        name, version = spec.split("==", 1)
        normalised = re.sub(r"[-_.]+", "-", name).lower()
        assert normalised in pins, f"{name} is vendored but absent from requirements.txt"
        assert version == pins[normalised], (
            f"{name} vendored at {version} but requirements.txt pins {pins[normalised]}"
        )


def test_declared_names_cover_the_jsonschema_and_interceptor_closures() -> None:
    """The printed seeds name the distributions each zip's own source imports.

    The rest of each closure is resolved at build time by `_install_target_closure`.
    """
    result = _print_pins()
    assert result.returncode == 0, result.stderr
    emitted = {
        line.split(" ", 1)[1].split("==", 1)[0].lower().replace("_", "-")
        for line in result.stdout.splitlines()
        if line.strip()
    }
    assert "jsonschema" in emitted
    assert {"pyjwt", "cryptography"} <= emitted
    # eval_trigger.zip needs PyYAML, plus starlette and python-multipart for agents.runtime_server.
    assert {"pyyaml", "starlette", "python-multipart"} <= emitted


def test_the_script_declares_seeds_rather_than_an_enumerated_closure() -> None:
    """Each seed array lists exactly its seeds, with transitive distributions left to resolution."""
    text = _script_text()
    seed_block = re.search(r"TOOL_ZIP_SEED_NAMES=\((.*?)\)", text, re.S)
    assert seed_block is not None, "expected a TOOL_ZIP_SEED_NAMES array"
    seeds = re.findall(r"'([^']+)'", seed_block.group(1))
    assert seeds == ["jsonschema"], seeds
    interceptor_block = re.search(r"INTERCEPTOR_SEED_NAMES=\((.*?)\)", text, re.S)
    assert interceptor_block is not None
    assert re.findall(r"'([^']+)'", interceptor_block.group(1)) == ["PyJWT", "cryptography"]
    eval_trigger_block = re.search(r"EVAL_TRIGGER_SEED_NAMES=\((.*?)\)", text, re.S)
    assert eval_trigger_block is not None, "expected an EVAL_TRIGGER_SEED_NAMES array"
    assert re.findall(r"'([^']+)'", eval_trigger_block.group(1)) == [
        "jsonschema",
        "PyYAML",
        "starlette",
        "python-multipart",
    ]
    for absent in ("anyio", "httpx", "httpx2", "anthropic", "openai", "boto3"):
        assert f"'{absent}'" not in eval_trigger_block.group(1), (
            f"{absent} must not be a seed: it is either resolved (anyio) or unreachable from the "
            "eval Lambdas"
        )


def test_the_build_closes_its_vendored_set_over_the_lambda_target_environment() -> None:
    """The build closes its vendored set via the gate's target-environment resolution, constrained
    by requirements.txt."""
    text = _script_text()
    assert "--print-missing-required-distributions" in text
    assert "_install_target_closure" in text
    assert "--constraint" in text, "every pip-resolved version must be bound by requirements.txt"
    assert "_verify_vendor_pins" in text, "an unpinned resolved distribution must fail the build"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda text: text.replace("jsonschema==", "# removed=="), "absent"),
        (
            lambda text: re.sub(r"^cryptography==.*$", "cryptography>=1.0", text, flags=re.M),
            "not exact",
        ),
    ],
)
def test_a_manifest_that_cannot_supply_a_pin_fails_the_build(
    tmp_path: Path, mutation: object, reason: str
) -> None:
    manifest = tmp_path / "requirements.txt"
    assert callable(mutation)
    manifest.write_text(mutation(REQUIREMENTS.read_text(encoding="utf-8")), encoding="utf-8")
    result = _print_pins(requirements=manifest)
    assert result.returncode != 0, f"a {reason} pin must fail, got: {result.stdout}"
    assert "not exact-pinned" in result.stderr


def test_distribution_names_resolve_under_pep503_normalisation(tmp_path: Path) -> None:
    """A seed spelled with different case in the manifest still resolves to its pin."""
    manifest = tmp_path / "requirements.txt"
    manifest.write_text(
        re.sub(
            r"^jsonschema==",
            "JSONSchema==",
            REQUIREMENTS.read_text(encoding="utf-8"),
            flags=re.M,
        ).replace("PyJWT==", "pyjwt=="),
        encoding="utf-8",
    )
    result = _print_pins(requirements=manifest)
    assert result.returncode == 0, result.stderr
    assert f"jsonschema=={_requirement_pins()['jsonschema']}" in result.stdout
    assert f"PyJWT=={_requirement_pins()['pyjwt']}" in result.stdout


def test_a_separator_variant_of_a_resolved_name_finds_its_manifest_pin(tmp_path: Path) -> None:
    """`_requirement_pin typing-extensions` finds the manifest's `typing_extensions==` pin."""
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "set -euo pipefail\n"
        "extracted=\"$(sed -n '/^_normalise_distribution_name()/,/^}/p;"
        '/^_requirement_pin()/,/^}/p\' "$1")"\n'
        'eval "${extracted}"\n'
        'REQUIREMENTS_FILE="$2"\n'
        "_requirement_pin typing-extensions\n",
        encoding="utf-8",
    )
    result = proc_run.run(
        ["bash", str(probe), str(SCRIPT), str(REQUIREMENTS)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"typing-extensions=={_requirement_pins()['typing-extensions']}"
