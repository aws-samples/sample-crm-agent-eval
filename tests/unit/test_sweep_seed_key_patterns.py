# SPDX-License-Identifier: MIT
"""Contract: `infra/eval_sfn.tf`'s `local.sweep_seed_key_patterns` against the keys a sweep uses.

Every primary key `fixtures.generate.seed_tenant` writes for an `eval-<run_id>` tenant, and every
record id a golden task names once composed by `eval.task_ids`, matches its table's
`dynamodb:LeadingKeys` pattern; no `demo` or `canary` corpus key and no agent-created id does.
The patterns are parsed from `eval_sfn.tf`, never restated here.
"""

from __future__ import annotations

import fnmatch
import re
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from fixtures.generate import generate_dataset

from crm.authz import DEMO_TENANT
from crm.handlers._common import idempotent_id
from crm.store.schema import table_schema
from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks
from eval.task_ids import localize_tasks
from eval.tenancy import CANARY_TENANT, allocate_run_id, tenant_for_run

EVAL_SFN_TF: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "eval_sfn.tf"

_PATTERNS_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"^  sweep_seed_key_patterns = \{\n(?P<body>[\s\S]{0,4096}?)^  \}$", re.MULTILINE
)
_PATTERN_ENTRY: Final[re.Pattern[str]] = re.compile(r"^    (\w+)\s*=\s*\"([^\"]*)\"$", re.MULTILINE)


def parse_sweep_seed_key_patterns(text: str) -> dict[str, str]:
    """`{table: LeadingKeys pattern}` as `local.sweep_seed_key_patterns` declares it in `text`."""
    matches = _PATTERNS_BLOCK.findall(text)
    assert len(matches) == 1, f"expected one `sweep_seed_key_patterns` block, found {len(matches)}"
    lines = matches[0].splitlines()
    malformed = [line for line in lines if not _PATTERN_ENTRY.fullmatch(line)]
    assert lines and not malformed, f"`sweep_seed_key_patterns` has non-entry lines: {malformed}"
    return dict(_PATTERN_ENTRY.findall(matches[0]))


def sweep_seed_key_patterns() -> dict[str, str]:
    """The patterns `infra/eval_sfn.tf` declares."""
    return parse_sweep_seed_key_patterns(EVAL_SFN_TF.read_text(encoding="utf-8"))


def _corpus_keys(tenant: str) -> dict[str, list[str]]:
    """`{table: [primary key]}` for every item `seed_tenant` would write for `tenant`."""
    return {
        table: [str(item[table_schema(table).partition_key]) for item in items]
        for table, items in generate_dataset(tenant).as_mapping().items()
    }


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _assertion_ids(task: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """`(table, id)` for every record a task's assertions name by table."""
    assertions = task.get("assertions") or {}
    for entry in [
        *(assertions.get("must_not_change") or []),
        *(assertions.get("expected_field_changes") or []),
    ]:
        if entry.get("id") is not None:
            yield str(entry["table"]), str(entry["id"])
    proposal = assertions.get("expected_proposal")
    if proposal:
        yield str(proposal["target_table"]), str(proposal["target_id"])


@pytest.fixture(scope="module")
def tenant() -> str:
    run_id = allocate_run_id()
    assert re.fullmatch(r"[0-9a-f]{32}", run_id), run_id
    return tenant_for_run(run_id)


@pytest.fixture(scope="module")
def patterns() -> dict[str, str]:
    return sweep_seed_key_patterns()


@pytest.fixture(scope="module")
def kinds(tenant: str) -> dict[str, str]:
    """`{id kind: table}`, derived from the generated corpus's own ids."""
    derived: dict[str, set[str]] = {}
    for table, keys in _corpus_keys(tenant).items():
        for key in keys:
            derived.setdefault(key.split(f"-{tenant}-", 1)[0], set()).add(table)
    assert all(len(tables) == 1 for tables in derived.values()), derived
    return {kind: tables.pop() for kind, tables in derived.items()}


def test_the_patterns_cover_exactly_the_seeded_tables(
    tenant: str, patterns: dict[str, str]
) -> None:
    assert set(patterns) == set(generate_dataset(tenant).as_mapping())


def test_every_seeded_key_matches_its_tables_pattern(tenant: str, patterns: dict[str, str]) -> None:
    for table, keys in _corpus_keys(tenant).items():
        assert keys, table
        for key in keys:
            assert fnmatch.fnmatchcase(key, patterns[table]), (table, key)


def test_every_golden_task_id_matches_its_tables_pattern(
    tenant: str, patterns: dict[str, str], kinds: dict[str, str]
) -> None:
    """Every composed id, in prompts and assertions alike, against the table its kind names."""
    tasks = localize_tasks(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH), tenant=tenant)
    token = re.compile(
        r"\b(" + "|".join(map(re.escape, kinds)) + r")-" + re.escape(tenant) + r"-[\w-]+"
    )
    composed = 0
    named = 0
    for task in tasks:
        for text in _strings(task):
            for match in token.finditer(text):
                composed += 1
                table = kinds[match.group(1)]
                assert fnmatch.fnmatchcase(match.group(0), patterns[table]), (task["id"], match)
        for table, record_id in _assertion_ids(task):
            if table in patterns:
                named += 1
                assert fnmatch.fnmatchcase(record_id, patterns[table]), (task["id"], table)
    assert composed and named, (composed, named)


@pytest.mark.parametrize("protected", [DEMO_TENANT, CANARY_TENANT])
def test_no_protected_tenant_key_matches_any_pattern(
    protected: str, patterns: dict[str, str]
) -> None:
    keys = [key for table_keys in _corpus_keys(protected).values() for key in table_keys]
    assert keys
    for key in keys:
        assert not any(fnmatch.fnmatchcase(key, p) for p in patterns.values()), key


def test_no_agent_created_id_matches_any_pattern(
    tenant: str, patterns: dict[str, str], kinds: dict[str, str]
) -> None:
    """Tool Lambdas key new rows by `idempotent_id` or a uuid, which carry no tenant."""
    created = [str(uuid.uuid4()), *(idempotent_id(kind, f"{tenant}:tx-1") for kind in kinds)]
    for key in created:
        assert not any(fnmatch.fnmatchcase(key, p) for p in patterns.values()), key


def test_the_parser_reads_a_changed_pattern() -> None:
    """Control: the patterns come from the file, so editing one changes what the tests compare."""
    text = EVAL_SFN_TF.read_text(encoding="utf-8")
    mutated = text.replace('"acct-eval-*"', '"acct-*"', 1)
    assert mutated != text
    assert parse_sweep_seed_key_patterns(mutated)["accounts"] == "acct-*"
    assert not fnmatch.fnmatchcase("acct-demo-0000", sweep_seed_key_patterns()["accounts"])
    assert fnmatch.fnmatchcase("acct-demo-0000", parse_sweep_seed_key_patterns(mutated)["accounts"])
