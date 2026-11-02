# SPDX-License-Identifier: MIT
"""Tests for domain entities and field-level update allowlist enforcement."""

from __future__ import annotations

import json
from typing import Literal, Union

import pytest

from crm.domain import (
    ACTIVITY_KINDS,
    CONTACT_STATUSES,
    Account,
    Activity,
    Contact,
    Opportunity,
    is_known_table,
    validate_update_fields,
)
from crm.domain.allowlists import (
    FIELD_VALUE_TYPES,
    UNKNOWN_FIELD_SOURCE,
    _field_value_rule,
    validate_proposal_payload,
)
from crm.errors import ValidationFailed
from crm.toolspec.definitions import (
    HIGH_RISK_OPPORTUNITY_FIELDS,
    TABLES,
    UPDATE_ALLOWLISTS,
    allowlist_for,
)


class TestAccount:
    def test_minimal_construction(self) -> None:
        account = Account(account_id="acc-1", tenant="t1", name="Acme")
        assert account.account_id == "acc-1"
        assert account.tenant == "t1"
        assert account.name == "Acme"
        assert account.industry is None

    def test_is_frozen(self) -> None:
        account = Account(account_id="acc-1", tenant="t1", name="Acme")
        with pytest.raises(AttributeError):
            account.name = "Other"  # type: ignore[misc]


class TestContact:
    @pytest.mark.parametrize("status", CONTACT_STATUSES)
    def test_accepts_every_declared_status(self, status: str) -> None:
        contact = Contact(
            contact_id="c-1",
            tenant="t1",
            account_id="acc-1",
            name="Jane",
            status=status,  # type: ignore[arg-type]
        )
        assert contact.status == status

    def test_defaults_to_active(self) -> None:
        contact = Contact(contact_id="c-1", tenant="t1", account_id="acc-1", name="Jane")
        assert contact.status == "active"


class TestOpportunity:
    def test_accepts_every_declared_stage(self) -> None:
        for stage in (
            "Discovery",
            "Qualification",
            "Security Review",
            "Negotiation",
            "Closed Won",
            "Closed Lost",
        ):
            opp = Opportunity(
                opportunity_id="o-1",
                tenant="t1",
                account_id="acc-1",
                name="Deal",
                stage=stage,
                owner_rep_id="rep-1",
            )
            assert opp.stage == stage

    def test_rejects_unrecognised_stage(self) -> None:
        with pytest.raises(ValueError, match="unrecognised stage"):
            Opportunity(
                opportunity_id="o-1",
                tenant="t1",
                account_id="acc-1",
                name="Deal",
                stage="Not A Stage",
                owner_rep_id="rep-1",
            )

    def test_is_frozen(self) -> None:
        opp = Opportunity(
            opportunity_id="o-1",
            tenant="t1",
            account_id="acc-1",
            name="Deal",
            stage="Discovery",
            owner_rep_id="rep-1",
        )
        with pytest.raises(AttributeError):
            opp.stage = "Closed Won"  # type: ignore[misc]


class TestActivity:
    @pytest.mark.parametrize("kind", ACTIVITY_KINDS)
    def test_accepts_every_declared_kind(self, kind: str) -> None:
        activity = Activity(
            activity_id="a-1",
            tenant="t1",
            opportunity_id="o-1",
            kind=kind,  # type: ignore[arg-type]
            subject="Subject",
            occurred_at="2026-01-01T00:00:00Z",
        )
        assert activity.kind == kind

    def test_rejects_unrecognised_kind(self) -> None:
        with pytest.raises(ValueError, match="unrecognised activity kind"):
            Activity(
                activity_id="a-1",
                tenant="t1",
                opportunity_id="o-1",
                kind="not_a_kind",  # type: ignore[arg-type]
                subject="Subject",
                occurred_at="2026-01-01T00:00:00Z",
            )


class TestIsKnownTable:
    @pytest.mark.parametrize("table", sorted(TABLES))
    def test_true_for_every_member_of_tables(self, table: str) -> None:
        assert is_known_table(table) is True

    def test_false_for_unrecognised_name(self) -> None:
        assert is_known_table("not_a_table") is False


class TestValidateUpdateFields:
    """Field-level allowlists govern updates and are enforced before any write."""

    #: One type-valid value per allowlisted field across all four tables, stated independently of
    #: `crm.domain.allowlists.FIELD_VALUE_TYPES`.
    VALID_VALUES: dict[str, object] = {
        "employee_count": 500,
        "status": "active",
        "occurred_at": "2026-01-01T00:00:00Z",
    }

    @pytest.mark.parametrize("table", sorted(TABLES))
    def test_accepts_every_field_on_that_table_allowlist(self, table: str) -> None:
        fields: dict[str, object] = {
            field: self.VALID_VALUES.get(field, "value") for field in allowlist_for(table)
        }
        # Must not raise.
        validate_update_fields(table, fields)

    def test_accepts_empty_payload(self) -> None:
        validate_update_fields("accounts", {})

    def test_rejects_field_absent_from_the_allowlist(self) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            validate_update_fields("accounts", {"name": "Acme", "not_a_real_field": "x"})
        assert excinfo.value.source == UNKNOWN_FIELD_SOURCE
        assert excinfo.value.code == "validation_failed"
        assert excinfo.value.http_status == 400

    def test_rejects_high_risk_opportunity_field_before_any_write(self) -> None:
        """A high-risk field update on update_opportunity is refused by validate_update_fields
        alone, before any store write is attempted."""
        writes_attempted: list[dict[str, object]] = []

        def fake_store_write(table: str, fields: dict[str, object]) -> None:
            writes_attempted.append(fields)

        def update_opportunity(table: str, fields: dict[str, object]) -> None:
            validate_update_fields(table, fields)
            fake_store_write(table, fields)

        high_risk_field = next(iter(HIGH_RISK_OPPORTUNITY_FIELDS))
        assert high_risk_field not in UPDATE_ALLOWLISTS["opportunities"]

        with pytest.raises(ValidationFailed) as excinfo:
            update_opportunity("opportunities", {high_risk_field: "new-value"})

        assert excinfo.value.source == high_risk_field
        assert writes_attempted == []  # no write occurred

    def test_rejects_first_offending_field_in_payload_order(self) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            validate_update_fields(
                "opportunities", {"name": "ok", "stage": "Closed Won", "value": 1}
            )
        assert excinfo.value.source == "stage"

    def test_propagates_value_error_for_unknown_table(self) -> None:
        with pytest.raises(ValueError, match="unrecognised entity"):
            validate_update_fields("not_a_table", {"anything": "x"})

    def test_detail_never_shown_in_api_response(self) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            validate_update_fields("contacts", {"status": "active", "bogus": "x"})
        response = excinfo.value.to_api_response()
        assert excinfo.value.detail is not None
        assert "rejected_fields" not in str(response)
        assert excinfo.value.detail not in str(response)
        assert response["error"]["source"] == UNKNOWN_FIELD_SOURCE

    def test_a_known_schema_field_outside_the_allowlist_is_named_without_a_proposal_route(
        self,
    ) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            validate_update_fields("accounts", {"owner_rep_id": "rep-2"})
        assert excinfo.value.source == "owner_rep_id"
        assert "'owner_rep_id'" in excinfo.value.message
        assert "rep-2" not in excinfo.value.message
        assert "submit_proposal" not in excinfo.value.message

    def test_a_planted_unknown_key_appears_nowhere_in_the_error_payload(self) -> None:
        planted = "PLANTED-unknown-key"
        with pytest.raises(ValidationFailed) as excinfo:
            validate_update_fields("opportunities", {planted: "PLANTED-value"})
        error = excinfo.value
        rendered = json.dumps(error.to_api_response()) + json.dumps(error.to_mcp_error())
        assert "PLANTED" not in rendered
        assert "PLANTED" not in error.message
        assert error.source == UNKNOWN_FIELD_SOURCE
        assert "Permitted: " in error.message


class TestEnumTypedFieldValuesOnUpdate:
    """An enum-typed field's closed set governs the update and proposal paths, whose input schema
    leaves `fields` open."""

    @pytest.mark.parametrize("status", ["admin", "DEPARTED", "inactive", "", "departed "])
    def test_a_status_outside_the_declared_literal_is_refused_on_update(self, status: str) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            validate_update_fields("contacts", {"status": status})
        assert excinfo.value.source == "status"
        assert excinfo.value.code == "validation_failed"
        assert excinfo.value.http_status == 400

    @pytest.mark.parametrize("status", CONTACT_STATUSES)
    def test_every_declared_status_still_succeeds(self, status: str) -> None:
        validate_update_fields("contacts", {"status": status})
        validate_proposal_payload("contacts", {"status": status})

    @pytest.mark.parametrize("status", ["admin", "Active"])
    def test_the_proposal_after_payload_path_is_governed_identically(self, status: str) -> None:
        """The proposal after-payload path refuses an out-of-set status the same way."""
        with pytest.raises(ValidationFailed) as excinfo:
            validate_proposal_payload("contacts", {"status": status})
        assert excinfo.value.source == "status"

    def test_the_rejection_message_names_the_permitted_members_and_leaks_nothing_else(self) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            validate_update_fields("contacts", {"status": "admin"})
        response = excinfo.value.to_api_response()
        assert "active" in response["error"]["message"]
        assert "departed" in response["error"]["message"]
        assert excinfo.value.detail is not None
        assert excinfo.value.detail not in str(response)

    def test_contacts_status_is_the_only_enum_constrained_allowlisted_field(self) -> None:
        """`contacts.status` is the only allowlisted field with a closed set of permitted values."""
        constrained = {
            (table, field)
            for table, rules in FIELD_VALUE_TYPES.items()
            for field, rule in rules.items()
            if rule.permitted_values is not None
        }
        assert constrained == {("contacts", "status")}

    def test_a_literal_annotation_yields_its_members_and_a_plain_type_yields_none(self) -> None:
        """A Literal annotation resolves to its members and a plain type resolves to no closed set."""
        assert _field_value_rule(Literal["a", "b"]).permitted_values == frozenset({"a", "b"})
        assert _field_value_rule(str).permitted_values is None
        optional_enum = _field_value_rule(Literal["a"] | None)
        assert optional_enum.permitted_values == frozenset({"a"})
        assert optional_enum.optional is True
        # A Literal unioned with a bare type admits values outside the literal, so no closed set.
        assert _field_value_rule(Union[Literal["a"], int]).permitted_values is None  # noqa: UP007

    def test_an_optional_enum_field_may_still_be_cleared_to_none(self) -> None:
        """The closed set constrains values, not presence — `None` removes the value itself."""
        rule = _field_value_rule(Literal["a", "b"] | None)
        assert rule.optional is True
        assert rule.permitted_values is not None and None not in rule.permitted_values
