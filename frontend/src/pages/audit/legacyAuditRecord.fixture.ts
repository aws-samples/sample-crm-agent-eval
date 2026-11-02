// SPDX-License-Identifier: MIT
/**
 * A `get_audit_record` response for a record stored before audit snapshots existed: the item that
 * `crm.audit.AuditRecord.to_item` writes, without `before_snapshot`/`after_snapshot`, plus the
 * `action` that `crm.audit.audit_action` derives for it (`create`, since `before_snapshot` is absent).
 */
export const legacyAuditRecordResponse = {
  audit_record: {
    account_id: "acct-demo-0001",
    action: "create",
    actor_rep_id: "rep-1",
    audit_id: "audit-legacy-1",
    created_at: "2026-01-01T00:00:00Z",
    reversed_at: null,
    reversed_by: null,
    reversed_reason: null,
    status: "active",
    target_id: "opp-legacy-1",
    target_table: "opportunities",
    tenant: "demo",
    tenant_actor_rep_id: "demo#rep-1",
    tenant_target_id: "demo#opp-legacy-1",
  },
} as const;
