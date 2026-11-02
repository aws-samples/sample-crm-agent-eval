# SPDX-License-Identifier: MIT
"""Tests for the identity-channel parity gate.

Violations are planted in a stand-in relay source tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_identity_channel_parity import (
    UnverifiableError,
    check,
    main,
    python_channel_keys,
    relay_channel_keys,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELAY_SRC = _REPO_ROOT / "src" / "relay" / "src"
_RELAY_REQUESTS_TS = _RELAY_SRC / "requests.ts"


def _relay_source_with(keys: list[str]) -> str:
    """Render a relay source file whose `IDENTITY_CHANNEL_KEYS` lists exactly `keys`."""
    members = "\n".join(f'  "{key}",' for key in keys)
    return (
        "// SPDX-License-Identifier: MIT\n"
        "/** Mirrors `crm.envelope.IDENTITY_CHANNEL_KEYS`. */\n"
        f"const IDENTITY_CHANNEL_KEYS: readonly string[] = [\n{members}\n];\n"
    )


def _write(tmp_path: Path, source: str, *, name: str = "index.ts") -> Path:
    """Plant `source` as one module in a stand-in relay source tree, and return the tree."""
    tree = tmp_path / "src"
    tree.mkdir(exist_ok=True)
    (tree / name).write_text(source, encoding="utf-8")
    return tree


# --------------------------------------------------------------------------------------
# The real repository passes
# --------------------------------------------------------------------------------------


def test_the_real_repository_passes() -> None:
    """The relay strips every key the Python decoder reads."""
    report = check()

    assert report["ok"] is True
    assert report["missing_from_relay"] == []


def test_the_pass_is_not_vacuous_because_both_sides_are_non_empty() -> None:
    """Both the Python and relay key sets are non-empty and contain `_agent_identity`."""
    python_keys = python_channel_keys()
    relay_keys = relay_channel_keys()

    assert len(python_keys) == 3, sorted(python_keys)
    assert len(relay_keys) >= 3, sorted(relay_keys)
    assert "_agent_identity" in python_keys
    assert "_agent_identity" in relay_keys


def test_the_relay_parser_reads_the_array_literal_not_the_type_annotation() -> None:
    """The relay parser reads the array literal, not the `readonly string[]` annotation."""
    assert relay_channel_keys(_RELAY_REQUESTS_TS) == frozenset(
        {"_identity", "_agent_identity", "requestContext", "_gateway_identity"}
    )


def test_the_gate_exits_zero_on_the_real_repository(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])

    assert exit_code == 0
    assert "OK" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# Planted violations
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("dropped", ["_identity", "_agent_identity", "requestContext"])
def test_dropping_any_python_channel_key_from_the_relay_is_detected(
    tmp_path: Path, dropped: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of the three Python channels, removed one at a time, is caught."""
    remaining = [
        key for key in ("_identity", "_agent_identity", "requestContext") if key != dropped
    ]
    planted = _write(tmp_path, _relay_source_with([*remaining, "_gateway_identity"]))
    monkeypatch.setattr("scripts.check_identity_channel_parity._RELAY_SRC", planted, raising=True)

    report = check()

    assert report["ok"] is False
    assert report["missing_from_relay"] == [dropped]


def test_a_planted_violation_makes_the_gate_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A planted violation makes the gate exit 1 and report FAILED."""
    planted = _write(tmp_path, _relay_source_with(["_identity", "requestContext"]))
    monkeypatch.setattr("scripts.check_identity_channel_parity._RELAY_SRC", planted, raising=True)

    exit_code = main([])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "FAILED" in output
    assert "_agent_identity" in output


def test_a_future_python_channel_key_the_relay_does_not_know_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key added on the Python side only is reported missing from the relay."""
    planted = _write(
        tmp_path, _relay_source_with(["_identity", "_agent_identity", "requestContext"])
    )
    monkeypatch.setattr("scripts.check_identity_channel_parity._RELAY_SRC", planted, raising=True)
    monkeypatch.setattr(
        "scripts.check_identity_channel_parity.python_channel_keys",
        lambda: frozenset({"_identity", "_agent_identity", "requestContext", "_future_channel"}),
        raising=True,
    )

    report = check()

    assert report["ok"] is False
    assert report["missing_from_relay"] == ["_future_channel"]


def test_a_relay_only_key_is_not_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key present only in the relay list is reported as relay-only and passes."""
    planted = _write(
        tmp_path,
        _relay_source_with(
            ["_identity", "_agent_identity", "requestContext", "_gateway_identity", "_extra"]
        ),
    )
    monkeypatch.setattr("scripts.check_identity_channel_parity._RELAY_SRC", planted, raising=True)

    report = check()

    assert report["ok"] is True
    assert "_extra" in report["relay_only"]


# --------------------------------------------------------------------------------------
# Unverifiable is exit 3, never a pass
# --------------------------------------------------------------------------------------


def test_an_absent_relay_source_is_unverifiable(tmp_path: Path) -> None:
    with pytest.raises(UnverifiableError, match="relay source not found"):
        relay_channel_keys(tmp_path / "nope.ts")


def test_a_relay_tree_without_the_list_anywhere_is_unverifiable(tmp_path: Path) -> None:
    """A relay tree in which no module declares the list is unverifiable."""
    tree = _write(tmp_path, "// SPDX-License-Identifier: MIT\nconst OTHER = 1;\n")
    (tree / "other.ts").write_text("export const UNRELATED = 1;\n", encoding="utf-8")
    with pytest.raises(UnverifiableError, match="no relay module declares an"):
        relay_channel_keys(tree)


def test_the_list_declared_in_two_modules_is_unverifiable(tmp_path: Path) -> None:
    """A list declared in two relay modules is unverifiable."""
    tree = _write(tmp_path, _relay_source_with(["_identity", "_agent_identity", "requestContext"]))
    (tree / "stale.ts").write_text(
        _relay_source_with(["_identity", "_agent_identity"]), encoding="utf-8"
    )
    with pytest.raises(UnverifiableError, match="declared in more than one relay module"):
        relay_channel_keys(tree)


def test_the_declaration_is_found_whichever_module_holds_it(tmp_path: Path) -> None:
    """The declaration is found whichever module in the relay tree holds it."""
    keys = ["_identity", "_agent_identity", "requestContext"]
    for name in ("index.ts", "requests.ts", "somewhere_else.ts"):
        holder = tmp_path / name.removesuffix(".ts")
        holder.mkdir()
        tree = _write(holder, _relay_source_with(keys), name=name)
        assert relay_channel_keys(tree) == frozenset(keys), name


def test_an_empty_list_is_unverifiable_rather_than_a_pass(tmp_path: Path) -> None:
    """An empty relay list is unverifiable."""
    path = _write(tmp_path, _relay_source_with([]))
    with pytest.raises(UnverifiableError, match="lists no keys"):
        relay_channel_keys(path)


def test_unverifiable_exits_three_and_says_it_verified_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unverifiable relay source makes the gate exit 3 and say it verified nothing."""
    monkeypatch.setattr(
        "scripts.check_identity_channel_parity._RELAY_SRC",
        tmp_path / "absent.ts",
        raising=True,
    )

    exit_code = main([])
    output = capsys.readouterr().out

    assert exit_code == 3
    assert "SKIP" in output
    assert "verified nothing" in output


def test_json_mode_reports_the_same_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    exit_code = main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["missing_from_relay"] == []
