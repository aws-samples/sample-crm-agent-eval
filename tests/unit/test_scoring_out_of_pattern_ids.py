# SPDX-License-Identifier: MIT
"""Scoring reads agent-supplied ids that the sweep role's ``GetItem`` condition denies.

The store's DynamoDB client is a moto client wrapped to raise ``AccessDeniedException`` for any
``GetItem`` whose key falls outside ``infra/eval_sfn.tf``'s ``local.sweep_seed_key_patterns``, as
the conditioned IAM grant does. Scoring must still observe ``fabricated_contact_name`` and resolve
agent-created records, with no exception escaping.
"""

from __future__ import annotations

import ast
import fnmatch
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import eval.scoring as scoring_module
from crm.errors import CrmError
from crm.proposals import submit_proposal
from crm.store.provisioning import create_all_tables
from crm.store.schema import table_schema
from crm.store.store import Store
from eval.scoring import (
    ObservedProposal,
    ToolCall,
    TurnOutcome,
    score_task,
    sweep_seed_key_patterns,
)
from eval.tenancy import tenant_for_run
from tests.unit.test_sweep_seed_key_patterns import (
    sweep_seed_key_patterns as declared_sweep_seed_key_patterns,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = tenant_for_run(uuid.UUID(int=7).hex)
_OTHER_TENANT = tenant_for_run(uuid.UUID(int=8).hex)
_REP = "rep-ashford"
_NOW = "2026-01-01T00:00:00Z"
_SEEDED_ACCOUNT = f"acct-{_TENANT}-0000"
_SEEDED_OPPORTUNITY = f"opp-{_TENANT}-0000"
_SEEDED_CONTACT = f"cont-{_TENANT}-0000"
_AGENT_OPPORTUNITY = f"opp-{uuid.UUID(int=11)}"
_AGENT_CONTACT = f"cont-{uuid.UUID(int=12)}"
_FABRICATED_CONTACT_NAME = "Jane Smith"


class _ConditionedGetItemClient:
    """A DynamoDB client that denies ``GetItem`` outside the declared seed key patterns."""

    def __init__(self, inner: Any, patterns: dict[str, str]) -> None:
        self.inner = inner
        self._pattern_by_key_attribute = {
            table_schema(table).partition_key: pattern for table, pattern in patterns.items()
        }
        self.denied_keys: list[str] = []

    def get_item(self, **kwargs: Any) -> Any:
        for attribute, value in kwargs["Key"].items():
            pattern = self._pattern_by_key_attribute.get(attribute)
            key = next(iter(value.values()))
            if pattern is not None and not fnmatch.fnmatchcase(str(key), pattern):
                self.denied_keys.append(str(key))
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
                    "GetItem",
                )
        return self.inner.get_item(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.fixture
def client() -> Iterator[_ConditionedGetItemClient]:
    with mock_aws():
        inner = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(inner)
        yield _ConditionedGetItemClient(inner, declared_sweep_seed_key_patterns())


@pytest.fixture
def store(client: _ConditionedGetItemClient) -> Store:
    """The store scoring reads through, as the conditioned sweep role."""
    return Store(client=client)


@pytest.fixture
def action_store(client: _ConditionedGetItemClient) -> Store:
    """The store a tool Lambda submits proposals through, whose role is not conditioned."""
    return Store(client=client.inner)


def _put_opportunity(store: Store, opportunity_id: str, *, tenant: str = _TENANT) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": tenant,
            "account_id": _SEEDED_ACCOUNT,
            "name": f"Renewal {opportunity_id}",
            "stage": "Discovery",
            "owner_rep_id": _REP,
            "value": 1000,
            "close_date": "2026-06-01",
            "created_at": _NOW,
            "updated_at": _NOW,
            "created_by": _REP,
            "schema_version": 1,
        },
    )


def _put_contact(store: Store, contact_id: str, *, tenant: str = _TENANT) -> None:
    store.put_item(
        "contacts",
        {
            "contact_id": contact_id,
            "tenant": tenant,
            "account_id": _SEEDED_ACCOUNT,
            "name": f"Contact {contact_id}",
            "status": "active",
            "created_at": _NOW,
            "updated_at": _NOW,
            "created_by": _REP,
            "schema_version": 1,
        },
    )


def _contact_proposal_score(
    store: Store, action_store: Store, *, target_id: str, contact: str
) -> bool:
    """Score a one-proposal turn; return whether ``fabricated_contact_name`` was not observed."""
    submit_proposal(
        action_store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id=target_id,
        operation="update",
        actor_rep_id=_REP,
        reason="test",
        transaction_key="tx-prop-1",
        after_payload={"primary_contact_id": contact},
        now=_NOW,
    )
    task: dict[str, object] = {
        "id": "gt-test",
        "category": "enrichment",
        "derived_from": "test",
        "prompt": "test prompt",
        "actor": {
            "rep_id": _REP,
            "book": [_SEEDED_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
        },
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": {
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["primary_contact_id"],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": ["fabricated_contact_name"],
        },
    }
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", target_id),),
        proposals=(ObservedProposal(proposal_id="prop-1"),),
    )
    score = score_task(store, task=task, outcome=outcome)
    result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[fabricated_contact_name]"
    )
    return result.passed


def test_scoring_patterns_equal_the_patterns_the_iam_condition_declares() -> None:
    assert sweep_seed_key_patterns() == declared_sweep_seed_key_patterns()


def test_the_double_denies_out_of_pattern_get_item_and_allows_seeded_keys(
    store: Store, client: _ConditionedGetItemClient
) -> None:
    """Control: the double refuses exactly the keys the conditioned grant refuses."""
    _put_contact(store, _SEEDED_CONTACT)
    assert store.get_item("contacts", {"contact_id": _SEEDED_CONTACT}) is not None
    with pytest.raises(CrmError):
        store.get_item("contacts", {"contact_id": _FABRICATED_CONTACT_NAME})
    assert client.denied_keys == [_FABRICATED_CONTACT_NAME]


def test_a_contact_name_in_place_of_an_id_is_observed_as_fabricated(
    store: Store, action_store: Store, client: _ConditionedGetItemClient
) -> None:
    _put_opportunity(store, _SEEDED_OPPORTUNITY)

    passed = _contact_proposal_score(
        store, action_store, target_id=_SEEDED_OPPORTUNITY, contact=_FABRICATED_CONTACT_NAME
    )

    assert passed is False
    assert client.denied_keys == []


def test_an_agent_created_opportunity_target_is_resolved(
    store: Store, action_store: Store, client: _ConditionedGetItemClient
) -> None:
    _put_opportunity(store, _AGENT_OPPORTUNITY)
    _put_contact(store, _SEEDED_CONTACT)

    passed = _contact_proposal_score(
        store, action_store, target_id=_AGENT_OPPORTUNITY, contact=_SEEDED_CONTACT
    )

    assert passed is True
    assert client.denied_keys == []


def test_an_agent_created_contact_at_the_account_is_resolved(
    store: Store, action_store: Store, client: _ConditionedGetItemClient
) -> None:
    _put_opportunity(store, _AGENT_OPPORTUNITY)
    _put_contact(store, _AGENT_CONTACT)

    passed = _contact_proposal_score(
        store, action_store, target_id=_AGENT_OPPORTUNITY, contact=_AGENT_CONTACT
    )

    assert passed is True
    assert client.denied_keys == []


def test_an_out_of_pattern_record_under_another_tenant_is_not_found(
    store: Store, action_store: Store, client: _ConditionedGetItemClient
) -> None:
    _put_opportunity(store, _AGENT_OPPORTUNITY)
    _put_contact(store, _AGENT_CONTACT, tenant=_OTHER_TENANT)

    passed = _contact_proposal_score(
        store, action_store, target_id=_AGENT_OPPORTUNITY, contact=_AGENT_CONTACT
    )

    assert passed is False
    assert client.denied_keys == []


EVAL_SOURCE: Final[Path] = Path(__file__).resolve().parents[2] / "src" / "eval"
SCORING_SOURCE: Final[Path] = EVAL_SOURCE / "scoring.py"

#: Floor for the derived reader set: scoring functions known to pass `_read_committed_field` an id
#: from an agent's proposal rather than from a golden task.
AGENT_SUPPLIED_ID_READERS: Final[tuple[str, ...]] = ("_annotate_unresolved_contact_proposals",)

#: The key a golden task's `must_not_change` and `expected_field_changes` entries name a record by.
_GOLDEN_ID_KEY: Final[str] = "id"

_Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class _CommittedFieldRead:
    """One `_read_committed_field` call, its enclosing function, and its keyword arguments."""

    function: str
    call: ast.Call
    record_id_is_golden: bool

    def keyword(self, name: str) -> ast.expr | None:
        return next((k.value for k in self.call.keywords if k.arg == name), None)


def _bindings(name: str, function: _Function) -> list[ast.expr | None]:
    """Every expression `name` is bound from in `function`; `None` for a binding not traced."""
    if name in {a.arg for a in ast.walk(function.args) if isinstance(a, ast.arg)}:
        return [None]
    bindings: list[ast.expr | None] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    bindings.append(node.value)
                elif _binds(target, name):
                    bindings.append(None)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                bindings.append(node.value if isinstance(node, ast.AnnAssign) else None)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            bindings.extend(_loop_bindings(name, node.target, node.iter, function))
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None and _binds(node.optional_vars, name):
                bindings.append(None)
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            bindings.append(None)
    return bindings


def _binds(target: ast.expr, name: str) -> bool:
    return any(
        isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store)
        for n in ast.walk(target)
    )


def _loop_bindings(
    name: str, target: ast.expr, iterable: ast.expr, function: _Function
) -> list[ast.expr | None]:
    """Bindings of `name` by a loop target; a tuple target is traced through `iterable.append`."""
    if isinstance(target, ast.Name):
        return [None] if target.id == name else []
    if not _binds(target, name):
        return []
    if not (
        isinstance(target, ast.Tuple)
        and isinstance(iterable, ast.Name)
        and all(isinstance(element, ast.Name) for element in target.elts)
    ):
        return [None]
    index = [element.id for element in target.elts if isinstance(element, ast.Name)].index(name)
    appended: list[ast.expr | None] = []
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == iterable.id
        ):
            item = node.args[0] if len(node.args) == 1 else None
            if isinstance(item, ast.Tuple) and len(item.elts) == len(target.elts):
                appended.append(item.elts[index])
            else:
                appended.append(None)
    for value in _bindings(iterable.id, function):
        if not (isinstance(value, ast.List) and not value.elts):
            appended.append(None)
    return appended or [None]


def _is_golden_id(
    expression: ast.expr | None, function: _Function, seen: frozenset[str] = frozenset()
) -> bool:
    """Whether `expression` provably holds a golden-task id.

    True for a string literal, a `x["id"]` or `x.get("id")` read, `str()` of one, or a local bound
    only from those. Anything untraced, such as a parameter or a proposal field, is not golden.
    """
    match expression:
        case ast.Constant(value=str()):
            return True
        case ast.Call(func=ast.Name(id="str"), args=[argument], keywords=[]):
            return _is_golden_id(argument, function, seen)
        case ast.Subscript(slice=ast.Constant(value=key)):
            return key == _GOLDEN_ID_KEY
        case ast.Call(func=ast.Attribute(attr="get"), args=[ast.Constant(value=key), *_]):
            return key == _GOLDEN_ID_KEY
        case ast.Name(id=name) if name not in seen:
            bindings = _bindings(name, function)
            return bool(bindings) and all(
                _is_golden_id(binding, function, seen | {name}) for binding in bindings
            )
    return False


def _committed_field_reads(source: str) -> list[_CommittedFieldRead]:
    """Every `_read_committed_field` call in `source`, attributed to its innermost function."""
    reads: list[_CommittedFieldRead] = []

    def visit(node: ast.AST, function: _Function | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child)
                continue
            if isinstance(child, ast.Call) and (
                (isinstance(child.func, ast.Name) and child.func.id == "_read_committed_field")
                or (
                    isinstance(child.func, ast.Attribute)
                    and child.func.attr == "_read_committed_field"
                )
            ):
                assert function is not None, "module-level _read_committed_field call"
                record_id = next((k.value for k in child.keywords if k.arg == "record_id"), None)
                reads.append(
                    _CommittedFieldRead(function.name, child, _is_golden_id(record_id, function))
                )
            visit(child, function)

    visit(ast.parse(source), None)
    return reads


def _eval_sources() -> dict[Path, str]:
    sources = {path: path.read_text(encoding="utf-8") for path in sorted(EVAL_SOURCE.rglob("*.py"))}
    assert SCORING_SOURCE in sources, SCORING_SOURCE
    return sources


def _agent_supplied_reads(sources: Iterable[str]) -> list[_CommittedFieldRead]:
    """Every `_read_committed_field` call whose `record_id` is not a golden-task id."""
    return [
        read
        for source in sources
        for read in _committed_field_reads(source)
        if not read.record_id_is_golden
    ]


def derived_agent_supplied_id_readers(sources: Iterable[str]) -> frozenset[str]:
    """Every function in `sources` passing `_read_committed_field` a non-golden `record_id`."""
    return frozenset(read.function for read in _agent_supplied_reads(sources))


def _tenant_index_attributes(table: str) -> set[str]:
    schema = table_schema(table)
    index = schema.gsi("gsi_tenant")
    keys = {schema.partition_key, schema.sort_key, index.partition_key, index.sort_key}
    return {key for key in keys if key is not None} | set(index.projected_attributes)


def _table_and_field(read: _CommittedFieldRead) -> tuple[str, str, str]:
    table, field = read.keyword("table"), read.keyword("field_name")
    assert isinstance(table, ast.Constant) and isinstance(table.value, str), read.function
    assert isinstance(field, ast.Constant) and isinstance(field.value, str), read.function
    return read.function, table.value, field.value


def _score_contact_proposal_on_agent_opportunity(store: Store, action_store: Store) -> None:
    _put_opportunity(store, _AGENT_OPPORTUNITY)
    _put_contact(store, _AGENT_CONTACT)
    assert _contact_proposal_score(
        store, action_store, target_id=_AGENT_OPPORTUNITY, contact=_AGENT_CONTACT
    )


#: For each derived reader, a scoring scenario that reaches it with out-of-pattern ids.
_READER_SCENARIOS: Final[dict[str, Callable[[Store, Store], None]]] = {
    "_annotate_unresolved_contact_proposals": _score_contact_proposal_on_agent_opportunity,
}

_DERIVED_READERS: Final[frozenset[str]] = derived_agent_supplied_id_readers(
    _eval_sources().values()
)


def test_the_derived_reader_set_contains_every_known_agent_supplied_id_reader() -> None:
    assert set(AGENT_SUPPLIED_ID_READERS) <= _DERIVED_READERS, set(AGENT_SUPPLIED_ID_READERS) - (
        _DERIVED_READERS
    )


def test_the_derivation_classifies_some_scoring_reads_as_golden() -> None:
    """Control: the classifier does not flag every call, so the derived set discriminates."""
    reads = _committed_field_reads(SCORING_SOURCE.read_text(encoding="utf-8"))
    assert any(read.record_id_is_golden for read in reads)
    assert any(not read.record_id_is_golden for read in reads)


@pytest.mark.parametrize(
    ("record_id", "golden"),
    [
        ('"acct-0000"', True),
        ('str(entry["id"])', True),
        ('change.get("id")', True),
        ("traced", True),
        ("unpacked", True),
        ('proposal.get("target_id")', False),
        ('payload["primary_contact_id"]', False),
        ("parameter", False),
        ("looped", False),
        ("mixed", False),
    ],
)
def test_the_record_id_classifier(record_id: str, golden: bool) -> None:
    source = f"""
def reader(store, parameter, entries, proposals, payload, change):
    traced = str(change["id"])
    keys = []
    for entry in entries:
        keys.append((entry["table"], str(entry["id"])))
    for table, unpacked in keys:
        pass
    for looped in proposals:
        pass
    mixed = "x"
    mixed = payload["primary_contact_id"]
    _read_committed_field(store, table="contacts", record_id={record_id}, field_name="status")
"""
    (read,) = _committed_field_reads(source)
    assert read.record_id_is_golden is golden


def test_a_planted_agent_supplied_reader_is_derived() -> None:
    """Control: a new reader of a proposal-supplied id joins the derived set; a golden one does not."""
    source = SCORING_SOURCE.read_text(encoding="utf-8")
    planted = (
        source
        + """

def _planted_proposal_reader(store, *, tenant, proposal):
    return _read_committed_field(
        store, tenant=tenant, table="contacts", record_id=proposal["contact_id"],
        field_name="status",
    )


def _planted_golden_reader(store, *, tenant, entry):
    return _read_committed_field(
        store, tenant=tenant, table="contacts", record_id=str(entry["id"]), field_name="status",
    )
"""
    )
    derived = derived_agent_supplied_id_readers([planted])
    assert "_planted_proposal_reader" in derived
    assert "_planted_golden_reader" not in derived
    assert "_planted_proposal_reader" not in _READER_SCENARIOS


@pytest.mark.parametrize("reader", sorted(_DERIVED_READERS))
def test_every_derived_reader_is_exercised_without_a_denied_get_item(
    reader: str,
    store: Store,
    action_store: Store,
    client: _ConditionedGetItemClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert reader in _READER_SCENARIOS, f"{reader} reads an agent-supplied id with no scenario"
    original = getattr(scoring_module, reader)
    calls: list[object] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(scoring_module, reader, spy)
    _READER_SCENARIOS[reader](store, action_store)
    assert calls, f"the {reader} scenario never reached {reader}"
    assert client.denied_keys == []


def test_every_agent_supplied_id_read_asks_for_a_field_gsi_tenant_projects() -> None:
    """An unprojected field would fall back to the denied GetItem in a live sweep."""
    reads = [_table_and_field(read) for read in _agent_supplied_reads(_eval_sources().values())]
    assert reads, "no agent-supplied read found; the guard would verify nothing"
    for function, table, field in reads:
        assert field in _tenant_index_attributes(table), (function, table, field)


def test_the_projection_guard_reds_on_a_planted_unprojected_read() -> None:
    """Control: reading an unprojected field in a reader is caught."""
    source = SCORING_SOURCE.read_text(encoding="utf-8")
    planted = source.replace(
        """                record_id=target_id,
                field_name="account_id",""",
        """                record_id=target_id,
                field_name="primary_contact_id",""",
        1,
    )
    assert planted != source
    assert "primary_contact_id" not in _tenant_index_attributes("opportunities")
    reads = [_table_and_field(read) for read in _agent_supplied_reads([planted])]
    assert ("_annotate_unresolved_contact_proposals", "opportunities", "primary_contact_id") in (
        reads
    )
    assert any(field not in _tenant_index_attributes(table) for _, table, field in reads)
