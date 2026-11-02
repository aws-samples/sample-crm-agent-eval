# SPDX-License-Identifier: MIT
"""Adversarial proof that no secret, token, or full record payload can appear in a log line.

Each test builds a payload carrying token-shaped, secret-shaped, and full-record values and asserts
none survives :func:`crm.metering.redact`; a companion test asserts the unredacted payload does
contain them. The gate tests assert which `.update()` patterns make a logged dict name safe in
`scripts/check_log_redaction.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from crm.metering import redact

_GATE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_log_redaction.py"


def _load_gate() -> ModuleType:
    """Import `scripts/check_log_redaction.py`, reusing an already-registered copy."""
    existing = sys.modules.get("check_log_redaction")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("check_log_redaction", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_log_redaction"] = module
    spec.loader.exec_module(module)
    return module


log_redaction_gate = _load_gate()

# A JWT-shaped string: three base64url segments joined by dots. Not a real credential.
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9"
    + "."
    + "eyJzdWIiOiJyZXAtMSIs"
    + "ImJvb2siOiJhY2MtMSxh"
    + "Y2MtMiJ9"
    + "."
    + "4Q1zX9k2n7pW8mR3vB6c"
    + "D0eF5gH1iJ4kL7nO2pQ9"
    + "rS"
)
# An opaque secret-shaped string: a long run with no separators, resembling an API key.
FAKE_OPAQUE_KEY = "sk_live_" + "ABCDEF0123456789" + "abcdef0123456789" + "ABCDEF01"
# A full record payload, which must never be logged in full.
FULL_RECORD = {
    "account_id": "acc-42",
    "name": "Northwind Traders",
    "owner_rep_id": "rep-9",
    "annual_revenue": 4_200_000,
    "notes": "Renewal at risk; primary contact departed.",
}


def _adversarial_payload() -> dict[str, object]:
    """Build a payload carrying a token-shaped string, a secret-shaped string, and a full record."""
    return {
        "access_token": FAKE_JWT,
        "api_key": FAKE_OPAQUE_KEY,
        "record": FULL_RECORD,
        "unlisted_field_holding_a_secret": FAKE_OPAQUE_KEY,
        "unlisted_field_holding_a_jwt": FAKE_JWT,
        "nested": {"before_snapshot": FULL_RECORD, "token": FAKE_JWT},
        "book": ["acc-1", "acc-2", "acc-3"],
        "rep_id": "rep-1",
    }


def test_redact_strips_token_secret_and_full_record() -> None:
    """The three adversarial values are absent from the redacted output, by key or by shape."""
    payload = _adversarial_payload()
    cleaned = redact(payload)
    assert isinstance(cleaned, dict)
    serialised = json.dumps(cleaned)

    assert FAKE_JWT not in serialised
    assert FAKE_OPAQUE_KEY not in serialised
    assert "Northwind Traders" not in serialised
    assert "4200000" not in serialised
    assert "Renewal at risk" not in serialised
    # The book is reported as a count, never the raw account list.
    assert "acc-1" not in serialised
    assert "acc-2" not in serialised
    # A non-secret identifier is untouched — redaction is not a blanket wipe.
    assert cleaned["rep_id"] == "rep-1"


def test_redact_catches_secret_under_an_unlisted_key_name() -> None:
    """A credential surviving under a key name absent from the denylist is still caught by shape."""
    payload = {
        "unlisted_field_holding_a_secret": FAKE_OPAQUE_KEY,
        "unlisted_field_holding_a_jwt": FAKE_JWT,
    }
    cleaned = redact(payload)
    assert isinstance(cleaned, dict)
    assert cleaned["unlisted_field_holding_a_secret"] != FAKE_OPAQUE_KEY
    assert cleaned["unlisted_field_holding_a_jwt"] != FAKE_JWT
    assert FAKE_OPAQUE_KEY not in json.dumps(cleaned)
    assert FAKE_JWT not in json.dumps(cleaned)


def test_redact_is_load_bearing_not_vacuous() -> None:
    """The unredacted adversarial payload contains every raw value the other tests assert absent."""
    payload = _adversarial_payload()
    raw_serialised = json.dumps(payload)
    assert FAKE_JWT in raw_serialised
    assert FAKE_OPAQUE_KEY in raw_serialised
    assert "Northwind Traders" in raw_serialised
    assert "acc-1" in raw_serialised


def test_redact_recurses_into_nested_structures() -> None:
    """A secret or full record nested inside another mapping is still caught."""
    payload = _adversarial_payload()
    cleaned = redact(payload)
    assert isinstance(cleaned, dict)
    nested = cleaned["nested"]
    assert isinstance(nested, dict)
    serialised_nested = json.dumps(nested)
    assert FAKE_JWT not in serialised_nested
    assert "Northwind Traders" not in serialised_nested


def test_redact_handles_lists_of_secrets() -> None:
    """A list containing secret-shaped strings is redacted element-wise."""
    payload = {"tokens": [FAKE_JWT, FAKE_OPAQUE_KEY, "plain-value"]}
    cleaned = redact(payload)
    assert isinstance(cleaned, dict)
    tokens = cleaned["tokens"]
    assert isinstance(tokens, list)
    assert FAKE_JWT not in tokens
    assert FAKE_OPAQUE_KEY not in tokens


def test_redact_catches_an_aws_access_key_id_under_an_unlisted_key_name() -> None:
    """A 20-character AKIA/ASIA-style AWS access key ID is absent after redaction."""
    fake_access_key_id = "AKIA" + "IOSFODNN7EXAMPLE"
    payload = {"unlisted_field_holding_an_access_key": fake_access_key_id}
    raw_serialised = json.dumps(payload)
    assert fake_access_key_id in raw_serialised

    cleaned = redact(payload)
    cleaned_serialised = json.dumps(cleaned)
    assert fake_access_key_id not in cleaned_serialised


def test_redact_does_not_flag_an_unrelated_twenty_character_identifier() -> None:
    """A 20-character string with no AKIA/ASIA prefix is not mistaken for a credential."""
    payload = {"reference_id": "user-1234567890123456"}
    cleaned = redact(payload)
    assert isinstance(cleaned, dict)
    assert cleaned["reference_id"] == "user-1234567890123456"


def test_redact_does_not_mutate_the_input() -> None:
    """Redaction returns a new structure; the caller's original payload is left intact."""
    payload = _adversarial_payload()
    original_token = payload["access_token"]
    redact(payload)
    assert payload["access_token"] == original_token


def _gate_exit_code(tmp_path: Path, updates: list[str]) -> int:
    """Run the gate over one module that logs a safe-keyed dict after the given `.update()` lines."""
    body = "".join(f"    payload.update({update})\n" for update in updates)
    (tmp_path / "subject.py").write_text(
        "import json\n"
        "import logging\n"
        "from crm.metering import redact\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "def handler(fields, extra):\n"
        '    payload = {"event": "tool_call"}\n'
        f"{body}"
        "    logger.info(json.dumps(payload))\n",
        encoding="utf-8",
    )
    exit_code: int = log_redaction_gate.main(["--src-root", str(tmp_path)])
    return exit_code


@pytest.mark.parametrize(
    "updates",
    [
        ["redact(fields)", "extra"],
        ["extra", "redact(fields)"],
    ],
    ids=["redacted-then-raw", "raw-then-redacted"],
)
def test_gate_rejects_a_dict_with_one_raw_update_among_redacted_ones(
    tmp_path: Path, updates: list[str]
) -> None:
    """A safe-keyed dict updated once with redacted and once with raw data is not safe to log."""
    assert _gate_exit_code(tmp_path, updates) == 1


def test_gate_accepts_a_dict_whose_every_update_is_redacted(tmp_path: Path) -> None:
    """A safe-keyed dict whose every `.update()` takes redact() output is safe to log."""
    assert _gate_exit_code(tmp_path, ["redact(fields)", "redact(extra)"]) == 0


@pytest.mark.parametrize(
    "update",
    ["token=extra", "**extra", "*extra", "", "redact(fields), token=extra"],
    ids=["keyword", "double-star", "star", "no-arg", "redacted-positional-plus-keyword"],
)
def test_gate_rejects_a_dict_with_a_keyword_starstar_or_empty_update(
    tmp_path: Path, update: str
) -> None:
    """A keyword, `**` or argument-less `.update()` counts as not redacted, even beside a redacted one."""
    assert _gate_exit_code(tmp_path, ["redact(fields)", update]) == 1


def test_gate_rejects_a_safe_keyed_dict_name_with_no_update(tmp_path: Path) -> None:
    """A safe-keyed dict logged by name without any `.update()` is not bound as safe."""
    assert _gate_exit_code(tmp_path, []) == 1


def _module_gate_exit_code(tmp_path: Path, body: str) -> int:
    """Run the gate over one module made of the shared imports followed by `body`."""
    (tmp_path / "subject.py").write_text(
        "import json\n"
        "import logging\n"
        "from crm.metering import redact\n"
        "logger = logging.getLogger(__name__)\n"
        "\n" + body,
        encoding="utf-8",
    )
    exit_code: int = log_redaction_gate.main(["--src-root", str(tmp_path)])
    return exit_code


_REDACTED_THEN = (
    "def handler(fields, extra):\n    payload = redact(fields)\n{store}    logger.info(payload)\n"
)


@pytest.mark.parametrize(
    "store",
    [
        '    payload["raw"] = extra\n',
        "    payload.raw = extra\n",
        "    payload |= extra\n",
        "    payload = extra\n",
        '    payload.setdefault("raw", extra)\n',
        "    payload.update(extra)\n",
    ],
    ids=["subscript-store", "attribute-store", "augmented-or", "rebind", "setdefault", "update"],
)
def test_gate_rejects_a_redacted_name_with_a_later_raw_store(tmp_path: Path, store: str) -> None:
    """A name bound from redact() and then stored to with raw data in the same scope is not safe."""
    assert _module_gate_exit_code(tmp_path, _REDACTED_THEN.format(store=store)) == 1


def test_gate_rejects_augmented_or_on_a_safe_literal_dict(tmp_path: Path) -> None:
    """A safe-keyed dict with only redacted `.update()` calls but a raw `|=` merge is not safe."""
    body = (
        "def handler(fields, extra):\n"
        '    payload = {"event": "tool_call"}\n'
        "    payload.update(redact(fields))\n"
        "    payload |= extra\n"
        "    logger.info(json.dumps(payload))\n"
    )
    assert _module_gate_exit_code(tmp_path, body) == 1


@pytest.mark.parametrize(
    "consumer",
    [
        "def handler(extra):\n    payload = extra\n    logger.info(payload)\n",
        "def handler(payload):\n    logger.info(payload)\n",
        "def handler():\n    logger.info(payload)\n",
    ],
    ids=["raw-assign", "parameter", "no-store"],
)
def test_gate_does_not_carry_a_binding_across_functions(tmp_path: Path, consumer: str) -> None:
    """A name bound from redact() in one function is not safe to log in another function."""
    producer = "def build(fields):\n    payload = redact(fields)\n    return payload\n\n"
    assert _module_gate_exit_code(tmp_path, producer + consumer) == 1


def test_gate_does_not_carry_a_module_binding_into_a_function(tmp_path: Path) -> None:
    """A module-scope name bound from redact() is not safe to log inside a function."""
    body = "payload = redact({})\n\ndef handler():\n    logger.info(payload)\n"
    assert _module_gate_exit_code(tmp_path, body) == 1


@pytest.mark.parametrize(
    "body",
    [
        "def handler(fields):\n    logger.info(redact(fields))\n",
        "def handler(fields):\n    payload = redact(fields)\n    logger.info(payload)\n",
        "def handler(fields):\n    payload = redact(fields)\n    logger.info(json.dumps(payload))\n",
        "def handler(fields):\n"
        "    payload = redact(fields)\n"
        "    payload.update(redact(fields))\n"
        "    logger.info(payload)\n",
        "def first(fields):\n    payload = redact(fields)\n    logger.info(payload)\n\n"
        "def second(extra):\n    payload = redact(extra)\n    logger.info(payload)\n",
        "payload = redact({})\nlogger.info(payload)\n",
    ],
    ids=[
        "direct-call",
        "bound-name",
        "bound-name-dumps",
        "bound-name-redacted-update",
        "same-name-redacted-in-each-function",
        "module-scope",
    ],
)
def test_gate_accepts_a_name_whose_every_store_in_scope_is_redacted(
    tmp_path: Path, body: str
) -> None:
    """A redact() call, or a name whose every store in its own scope is redacted, is safe to log."""
    assert _module_gate_exit_code(tmp_path, body) == 0


def test_gate_reports_every_log_call_exactly_once_across_nested_scopes(tmp_path: Path) -> None:
    """Calls in decorators, defaults, nested defs, class bodies, lambdas and comprehensions all count."""
    (tmp_path / "subject.py").write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "@wrap(logger.info(a))\n"
        "def outer(x=logger.info(b)):\n"
        "    def inner():\n"
        "        logger.info(c)\n"
        "    class Inner:\n"
        "        logger.info(d)\n"
        "    f = lambda: logger.info(e)\n"
        "    return [logger.info(g) for _ in x]\n",
        encoding="utf-8",
    )
    sites = log_redaction_gate.scan_tree(tmp_path)
    assert [site.line for site in sites] == [4, 5, 7, 9, 10, 11]


def _module_site_classification(tmp_path: Path, body: str) -> str:
    """Return the classification of the single `logger.info` call site in `body`."""
    (tmp_path / "subject.py").write_text(
        "import logging\n"
        "from crm.metering import redact\n"
        "logger = logging.getLogger(__name__)\n"
        "\n" + body,
        encoding="utf-8",
    )
    sites = log_redaction_gate.scan_tree(tmp_path)
    assert len(sites) == 1
    classification: str = sites[0].classification
    return classification


@pytest.mark.parametrize(
    "body",
    [
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    def add():\n"
        '        ctx["secret"] = raw\n'
        "    add()\n"
        "    logger.info(ctx)\n",
        "def handler(raw, dispatch):\n"
        "    ctx = redact(raw)\n"
        "    def observe(result):\n"
        "        if isinstance(ctx, dict):\n"
        '            ctx.update({"last_result": result})\n'
        "    dispatch(observe)\n"
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        '    ctx = {"event": "x"}\n'
        "    ctx.update(redact(raw))\n"
        "    def add():\n"
        "        ctx.update(raw)\n"
        "    add()\n"
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    def add():\n"
        "        ctx.append(raw)\n"
        "    add()\n"
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    def add():\n"
        "        nonlocal ctx\n"
        "        ctx = raw\n"
        "    add()\n"
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    def outer():\n"
        "        def inner():\n"
        '            ctx["secret"] = raw\n'
        "        inner()\n"
        "    outer()\n"
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    class Holder:\n"
        '        ctx["secret"] = raw\n'
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    add = lambda: ctx.append(raw)\n"
        "    add()\n"
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    [ctx.append(item) for item in raw]\n"
        "    logger.info(ctx)\n",
        'ctx = redact({})\ndef add(raw):\n    ctx["secret"] = raw\nlogger.info(ctx)\n',
    ],
    ids=[
        "closure-subscript-store",
        "closure-update-mypy-strict-shape",
        "closure-raw-update-on-rule-2-name",
        "closure-append",
        "closure-nonlocal-rebind",
        "doubly-nested-closure",
        "class-body-store",
        "lambda-append",
        "comprehension-append",
        "function-mutates-module-name",
    ],
)
def test_gate_rejects_a_name_mutated_from_a_nested_scope(tmp_path: Path, body: str) -> None:
    """A name that a nested function, class, lambda or comprehension stores to is not safe."""
    assert _module_site_classification(tmp_path, body) == "unresolved"


@pytest.mark.parametrize(
    "store",
    [
        '    ctx["items"].append(raw)\n',
        '    ctx["details"].update(raw)\n',
        '    ctx.get("details")["secret"] = raw\n',
        '    ctx.get("details").update(raw)\n',
        '    ctx["k"]["s"] = raw\n',
        '    if isinstance(ctx, dict):\n        ctx["items"].append(raw)\n',
    ],
    ids=[
        "subscript-then-append",
        "subscript-then-update",
        "call-then-subscript-store",
        "call-then-update",
        "nested-subscript-store",
        "mypy-strict-subscript-append",
    ],
)
def test_gate_rejects_a_mutation_reached_through_a_subscript_or_call(
    tmp_path: Path, store: str
) -> None:
    """A store or mutating method reached through `name[...]` or `name.method()` is not safe."""
    body = f"def handler(raw):\n    ctx = redact(raw)\n{store}    logger.info(ctx)\n"
    assert _module_site_classification(tmp_path, body) == "unresolved"


@pytest.mark.parametrize(
    "body",
    [
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    def build():\n"
        "        ctx = {}\n"
        '        ctx["secret"] = raw\n'
        "        return ctx\n"
        "    build()\n"
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        "    def peek():\n"
        '        return ctx.get("k")\n'
        "    peek()\n"
        "    logger.info(ctx)\n",
        "def handler(raw):\n"
        "    ctx = redact(raw)\n"
        '    ctx["a"].get("b")\n'
        '    ctx.get("a").items()\n'
        "    logger.info(ctx)\n",
    ],
    ids=["nested-scope-binds-its-own-name", "nested-scope-only-reads", "chained-reads"],
)
def test_gate_accepts_reads_and_shadowing_from_nested_scopes(tmp_path: Path, body: str) -> None:
    """Reading a redacted name, or binding the same name locally in a nested scope, keeps it safe."""
    assert _module_site_classification(tmp_path, body) == "redacted"
