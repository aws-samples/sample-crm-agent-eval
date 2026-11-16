# SPDX-License-Identifier: MIT
"""Plan-time tests of the Runtime's conversation-history grant, `reps` TTL and history store env var.

* `agentcore_runtime_history` in `infra/main.tf`, planned offline with stand-in table, name and key
  values, renders exactly the designed statements. Each mutation of a scratch copy reds the
  statement it weakens.
* The write grant's `dynamodb:Attributes` list equals `HISTORY_ITEM_ATTRIBUTES`, the attributes the
  history store writes.
* The `dynamodb` module plans TTL on `expires_at` for `reps` and for no other table. A scratch copy
  without the block reds the TTL assertion.
* The Runtime environment sets `CONVERSATION_HISTORY_STORE = "dynamodb"`, and the Runtime role is in
  `local.dynamodb_data_plane_role_arns`.
* The compliant plan fixture carries the history policy, the TTL and the env var.
Terraform-backed cells skip when terraform is absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import plan_fixtures

from agents.conversation_history import HISTORY_ITEM_ATTRIBUTES
from crm.store.schema import TABLE_NAMES
from tests.unit._terraform_fixture import (
    _PROVIDER_BLOCK,
    MODULES_DIR,
    Plan,
    plan_directory,
    plan_module,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)
from tests.unit.test_infra_bedrock_grant import _top_level_blocks

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAIN_TF: Final[Path] = _REPO_ROOT / "infra" / "main.tf"
_ACCOUNT: Final[str] = "123456789012"
_REGION: Final[str] = "us-east-1"
_PREFIX: Final[str] = "crm-agent-eval"
_KEY_ARN: Final[str] = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/fixture-dynamodb-cmk"
_DOCUMENT: Final[str] = 'data "aws_iam_policy_document" "agentcore_runtime_history"'
_GET: Final[str] = "dynamodb:GetItem"
_PUT: Final[str] = "dynamodb:PutItem"


def _table_arn(table: str) -> str:
    return f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/{_PREFIX}{table}"


_REPS: Final[str] = _table_arn("reps")
_KEY_CONDITIONS: Final[dict[str, dict[str, frozenset[str]]]] = {
    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": frozenset({"history#*"})},
    "Null": {"dynamodb:LeadingKeys": frozenset({"false"})},
}

#: sid -> (effect, Action, NotAction, Resource, Condition), each set-valued, as designed.
EXPECTED: Final[dict[str, tuple[str, frozenset[str], frozenset[str], frozenset[str], Any]]] = {
    "ConversationHistoryRead": (
        "Allow",
        frozenset({_GET}),
        frozenset(),
        frozenset({_REPS}),
        _KEY_CONDITIONS,
    ),
    "ConversationHistoryWrite": (
        "Allow",
        frozenset({_PUT}),
        frozenset(),
        frozenset({_REPS}),
        {
            **_KEY_CONDITIONS,
            "ForAllValues:StringEquals": {
                "dynamodb:Attributes": frozenset(HISTORY_ITEM_ATTRIBUTES)
            },
        },
    ),
    "ConversationHistoryKms": (
        "Allow",
        frozenset({"kms:Decrypt", "kms:GenerateDataKey"}),
        frozenset(),
        frozenset({_KEY_ARN}),
        {
            "StringEquals": {
                "kms:ViaService": frozenset({f"dynamodb.{_REGION}.amazonaws.com"}),
                "kms:EncryptionContext:aws:dynamodb:tableName": frozenset({f"{_PREFIX}reps"}),
            }
        },
    ),
    "DenyDynamoDbOtherTables": (
        "Deny",
        frozenset({"dynamodb:*"}),
        frozenset(),
        frozenset(
            arn
            for table in TABLE_NAMES
            if table != "reps"
            for arn in (_table_arn(table), f"{_table_arn(table)}/index/*")
        ),
        {},
    ),
    "DenyRepsNonGrantedActions": (
        "Deny",
        frozenset(),
        frozenset({_GET, _PUT}),
        frozenset({_REPS, f"{_REPS}/index/*"}),
        {},
    ),
    "DenyRepsOutsideHistoryPrefix": (
        "Deny",
        frozenset({_GET, _PUT}),
        frozenset(),
        frozenset({_REPS}),
        {"ForAnyValue:StringNotLike": {"dynamodb:LeadingKeys": frozenset({"history#*"})}},
    ),
    "DenyCanaryHistory": (
        "Deny",
        frozenset({_GET, _PUT}),
        frozenset(),
        frozenset({_REPS}),
        {"ForAnyValue:StringLike": {"dynamodb:LeadingKeys": frozenset({"history#canary#*"})}},
    ),
}


def _as_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    return frozenset([value] if isinstance(value, str) else value)


def _conditions(
    raw: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, frozenset[str]]]:
    return {
        test: {key: _as_set(values) for key, values in pairs.items()}
        for test, pairs in (raw or {}).items()
    }


def statement_problems(statements: list[Mapping[str, Any]]) -> dict[str, str]:
    """sid -> what differs from :data:`EXPECTED`; an extra or missing sid is a problem too."""
    by_sid = {str(s.get("Sid")): s for s in statements}
    problems = {sid: "statement absent" for sid in EXPECTED if sid not in by_sid} | {
        sid: "unexpected statement" for sid in by_sid if sid not in EXPECTED
    }
    for sid, (effect, actions, not_actions, resources, conditions) in EXPECTED.items():
        statement = by_sid.get(sid)
        if statement is None:
            continue
        rendered = (
            statement.get("Effect"),
            _as_set(statement.get("Action")),
            _as_set(statement.get("NotAction")),
            _as_set(statement.get("Resource")),
            _conditions(statement.get("Condition")),
        )
        if rendered != (effect, actions, not_actions, resources, conditions):
            problems[sid] = f"rendered {rendered!r}"
    return problems


def _skip_without_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None and not require_terraform():
        pytest.skip(reason)


def _subject_hcl(mutate: tuple[str, str] | None = None) -> str:
    blocks = _top_level_blocks(_MAIN_TF.read_text(encoding="utf-8"))
    locals_blocks = [
        b for b in blocks if b.startswith("locals ") and "runtime_denied_table_arns" in b
    ]
    documents = [b for b in blocks if b.startswith(_DOCUMENT)]
    assert len(locals_blocks) == 1 and len(documents) == 1, (len(locals_blocks), len(documents))
    subject = f"{locals_blocks[0]}\n\n{documents[0]}\n"
    if mutate is not None:
        old, new = mutate
        assert old in subject, f"mutation anchor {old!r} is no longer in the subject"
        subject = subject.replace(old, new, 1)
    subject = subject.replace("module.dynamodb.", "local.stand_in_dynamodb.").replace(
        "var.aws_region", f'"{_REGION}"'
    )
    arns = ",\n".join(f'      {t} = "{_table_arn(t)}"' for t in sorted(TABLE_NAMES))
    names = ",\n".join(f'      {t} = "{_PREFIX}{t}"' for t in sorted(TABLE_NAMES))
    stand_in = (
        "locals {\n  stand_in_dynamodb = {\n"
        f"    table_arns = {{\n{arns}\n    }}\n"
        f"    table_names = {{\n{names}\n    }}\n"
        f'    kms_key_arn = "{_KEY_ARN}"\n  }}\n}}\n'
    )
    return (
        f"{stand_in}\n{subject}\n"
        'resource "terraform_data" "history" {\n'
        "  input = data.aws_iam_policy_document.agentcore_runtime_history.json\n}\n"
    )


def _rendered(work_dir: Path, mutate: tuple[str, str] | None = None) -> list[Mapping[str, Any]]:
    _skip_without_terraform()
    plan = plan_root(work_dir, _subject_hcl(mutate))
    document: Any = json.loads(plan.after("terraform_data.history")["input"])
    statements: list[Mapping[str, Any]] = document["Statement"]
    return statements


def test_the_history_document_renders_exactly_the_designed_statements(tmp_path: Path) -> None:
    assert statement_problems(_rendered(tmp_path)) == {}


def test_the_write_grant_names_exactly_the_attributes_the_store_writes() -> None:
    assert "tenant" not in HISTORY_ITEM_ATTRIBUTES
    match = re.search(
        r'variable = "dynamodb:Attributes"\s*\n\s*values\s*=\s*\[([^\]]*)\]',
        _MAIN_TF.read_text(encoding="utf-8"),
    )
    assert match is not None, "no dynamodb:Attributes condition in infra/main.tf"
    assert set(re.findall(r'"([^"]+)"', match.group(1))) == set(HISTORY_ITEM_ATTRIBUTES)


_NULL_CONDITION: Final[str] = (
    "    condition {\n"
    '      test     = "Null"\n'
    '      variable = "dynamodb:LeadingKeys"\n'
    '      values   = ["false"]\n'
    "    }\n"
)

#: id -> ((old, new) applied to the first match in the subject, the sid that must red).
MUTATIONS: Final[dict[str, tuple[tuple[str, str], str]]] = {
    "read_null_condition_dropped": ((_NULL_CONDITION, ""), "ConversationHistoryRead"),
    "read_action_widened": (
        ('actions   = ["dynamodb:GetItem"]', 'actions   = ["dynamodb:GetItem", "dynamodb:Query"]'),
        "ConversationHistoryRead",
    ),
    "write_attributes_dropped": (
        ('variable = "dynamodb:Attributes"', 'variable = "dynamodb:ReturnValues"'),
        "ConversationHistoryWrite",
    ),
    "kms_table_condition_dropped": (
        (
            'variable = "kms:EncryptionContext:aws:dynamodb:tableName"',
            'variable = "kms:EncryptionContext:other"',
        ),
        "ConversationHistoryKms",
    ),
    "kms_via_service_widened": (
        ('"dynamodb.${var.aws_region}.amazonaws.com"', '"dynamodb.*.amazonaws.com"'),
        "ConversationHistoryKms",
    ),
    "other_tables_deny_narrowed": (
        ('if table_name != "reps"', 'if table_name != "reps" && table_name != "accounts"'),
        "DenyDynamoDbOtherTables",
    ),
    "not_actions_widened": (
        (
            'not_actions = ["dynamodb:GetItem", "dynamodb:PutItem"]',
            'not_actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]',
        ),
        "DenyRepsNonGrantedActions",
    ),
    "prefix_deny_quantifier": (
        ('"ForAnyValue:StringNotLike"', '"ForAllValues:StringNotLike"'),
        "DenyRepsOutsideHistoryPrefix",
    ),
    "canary_deny_retargeted": (
        ('"history#canary#*"', '"history#none#*"'),
        "DenyCanaryHistory",
    ),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_each_weakening_reds_the_statement_it_guards(tmp_path: Path, mutation: str) -> None:
    edit, sid = MUTATIONS[mutation]
    assert set(statement_problems(_rendered(tmp_path, edit))) == {sid}


_MODULE_ARGUMENTS: Final[str] = (
    f'  name_prefix = "{_PREFIX}"\n'
    f'  account_id = "{_ACCOUNT}"\n'
    f'  caller_principal_arn = "arn:aws:iam::{_ACCOUNT}:role/fixture-operator"\n'
    f'  data_plane_principal_arns = ["arn:aws:iam::{_ACCOUNT}:role/fixture-data-plane"]\n'
    f'  administrator_principal_arns = ["arn:aws:iam::{_ACCOUNT}:role/fixture-operator"]'
)


def _ttl_by_table(plan: Plan) -> dict[str, list[Mapping[str, Any]]]:
    by_table: dict[str, list[Mapping[str, Any]]] = {}
    for address in plan.addresses_of_type("aws_dynamodb_table"):
        after = plan.after(address)
        by_table[str(after["name"]).removeprefix(_PREFIX)] = list(after.get("ttl") or [])
    return by_table


def _ttl_problems(by_table: Mapping[str, list[Mapping[str, Any]]]) -> list[str]:
    problems = []
    if by_table.get("reps") != [{"attribute_name": "expires_at", "enabled": True}]:
        problems.append(f"reps ttl is {by_table.get('reps')!r}")
    for table, ttl in sorted(by_table.items()):
        if table != "reps" and any(entry.get("enabled") for entry in ttl):
            problems.append(f"{table} has TTL enabled")
    return problems


def test_reps_alone_has_ttl_on_expires_at(tmp_path: Path) -> None:
    _skip_without_terraform()
    by_table = _ttl_by_table(plan_module(tmp_path, "dynamodb", _MODULE_ARGUMENTS))
    assert set(by_table) == set(TABLE_NAMES)
    assert _ttl_problems(by_table) == []


def test_without_the_ttl_block_the_ttl_check_reds(tmp_path: Path) -> None:
    _skip_without_terraform()
    modules = tmp_path / "modules"
    shutil.copytree(MODULES_DIR / "dynamodb", modules / "dynamodb")
    tables_tf = modules / "dynamodb" / "tables.tf"
    text = tables_tf.read_text(encoding="utf-8")
    block = '  ttl {\n    attribute_name = "expires_at"\n    enabled        = true\n  }\n'
    assert text.count(block) == 1, "the reps ttl block moved; update this control"
    tables_tf.write_text(text.replace(block, ""), encoding="utf-8")
    work = tmp_path / "root"
    work.mkdir()
    (work / "main.tf").write_text(
        f'{_PROVIDER_BLOCK}\nmodule "subject" {{\n  source = "{modules / "dynamodb"}"\n\n'
        f"{_MODULE_ARGUMENTS}\n}}\n",
        encoding="utf-8",
    )
    assert _ttl_problems(_ttl_by_table(plan_directory(work))) != []


def _runtime_environment_block(text: str) -> str:
    match = re.search(r"agent_runtime_environment_variables\s*=\s*\{(.*?)\n  \}", text, re.DOTALL)
    assert match is not None, "no agent_runtime_environment_variables map in infra/main.tf"
    return match.group(1)


def test_the_runtime_environment_selects_the_dynamodb_history_store() -> None:
    block = _runtime_environment_block(_MAIN_TF.read_text(encoding="utf-8"))
    assert re.search(r'^\s*CONVERSATION_HISTORY_STORE\s*=\s*"dynamodb"\s*$', block, re.MULTILINE)


def test_the_runtime_role_is_a_data_plane_principal() -> None:
    match = re.search(
        r"dynamodb_data_plane_role_arns\s*=\s*concat\((.*?)\n  \)",
        _MAIN_TF.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None
    assert re.findall(r"^\s*aws_iam_role\.agentcore_runtime\.arn,\s*$", match.group(1), re.M) == [
        "      aws_iam_role.agentcore_runtime.arn,"
    ]


def _fixture_changes() -> dict[str, Mapping[str, Any]]:
    document = plan_fixtures.build("compliant")
    return {str(change["address"]): change["change"] for change in document["resource_changes"]}


def test_the_compliant_plan_carries_the_history_policy_ttl_and_env_var() -> None:
    changes = _fixture_changes()
    policy = changes["aws_iam_role_policy.agentcore_runtime_history"]
    assert policy["after"]["name"] == "crm-agent-eval-runtime-history"
    reps = changes["module.dynamodb.aws_dynamodb_table.reps"]["after"]
    assert reps["ttl"] == [{"attribute_name": "expires_at", "enabled": True}]
    runtime = changes["module.agentcore.aws_bedrockagentcore_agent_runtime.this"]["after"]
    assert runtime["environment_variables"]["CONVERSATION_HISTORY_STORE"] == "dynamodb"
