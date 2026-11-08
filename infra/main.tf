/**
 * Root composition: wires the seven core modules plus `vpc`, `api_origin`, `agent_relay` and
 * `eval_artifacts` (`infra/modules/*`) into one `terraform apply`.
 *
 * `local.tools` transcribes `crm.toolspec.definitions.ALL_TOOLS` (one entry per tool) with their DynamoDB
 * grants and MCP input schemas; `scripts/check_toolspec_parity.py` fails on drift between the two.
 * Lambda zips are read from `var.lambda_build_dir` and must exist for plan/apply, not for validate.
 */

variable "lambda_build_dir" {
  description = <<-EOT
    Directory holding the pre-built archives this composition reads, resolved
    relative to `infra/`: one Lambda zip per `local.tool_names` entry, plus
    `identity_interceptor.zip`, `pre_token_generation.zip`, `eval_trigger.zip`
    (shared by the eval trigger, capabilities, user-settings and sweep functions),
    `chat_target.zip` (the chat Gateway's target) and `relay.zip` — and
    `agent-source.zip`, the CodeBuild source for the AgentCore container image. `scripts/package_lambdas.sh` and `scripts/package_relay.sh`
    populate the default location, `infra/build/lambda`.
  EOT
  type        = string
  default     = "build/lambda"
}



# ---------------------------------------------------------------------------
# Tool inventory — transcribed from crm.toolspec.definitions.ALL_TOOLS and
# infra/modules/lambda_action/table_action_role_matrix.json.
# ---------------------------------------------------------------------------

locals {
  # One entry per tool in `ALL_TOOLS`; `handler` and `description` are copied from each ToolDefinition.
  tools = {
    search_opportunities = { handler = "crm.handlers.search_opportunities.handler", description = "Find opportunities in your book by name words, stage, account, or owner. Use this to find ids; use get_opportunity for one record's full detail. If the result has truncated: true, pass its next_cursor as cursor to continue." }
    search_accounts      = { handler = "crm.handlers.search_accounts.handler", description = "Find accounts in your book by name words or owner. Use this to find ids; use get_account for one account's contact list. If the result has truncated: true, pass its next_cursor as cursor to continue." }
    get_opportunity      = { handler = "crm.handlers.get_opportunity.handler", description = "Retrieve one opportunity by id, with its account's contacts, its notes, and its activity timeline. Use search_opportunities to find an id." }
    get_account          = { handler = "crm.handlers.get_account.handler", description = "Retrieve one account by id with its full contact list, including each contact's status. Use search_accounts first if you only have the account's name." }
    get_contact          = { handler = "crm.handlers.get_contact.handler", description = "Retrieve one contact by id, including its status." }
    list_activities      = { handler = "crm.handlers.list_activities.handler", description = "List one opportunity's activities, optionally filtered by kind, most recent first within each page, with next_cursor for the next page. Use for a complete or filtered history; get_opportunity already includes the timeline." }
    pipeline_aggregate   = { handler = "crm.handlers.pipeline_aggregate.handler", description = "Totals across your book's opportunities: count per stage, total value, and value grouped by close date. Closed stages are excluded unless include_closed is true. No per-account breakdown; for one account, use search_opportunities with account_id." }
    list_proposals       = { handler = "crm.handlers.list_proposals.handler", description = "List proposals in your book. Set status to filter (pending = awaiting a decision); omit it to list every status. Returns up to 50 per call with next_cursor for the next page." }
    list_audit_records   = { handler = "crm.handlers.list_audit_records.handler", description = "List audited changes within your book, most recent first, with revert state." }
    get_audit_record     = { handler = "crm.handlers.get_audit_record.handler", description = "Retrieve one audited change within your book: actor, timestamp, action, target, revert state, and the full before and after snapshots." }
    draft_message        = { handler = "crm.handlers.draft_message.handler", description = "Draft an email or message for a contact. Read-only: returns draft text and persists nothing." }

    create_account     = { handler = "crm.handlers.create_account.handler", description = "Create a new account. Commits immediately." }
    update_account     = { handler = "crm.handlers.update_account.handler", description = "Update an account's fields. Commits immediately." }
    create_contact     = { handler = "crm.handlers.create_contact.handler", description = "Create a contact under an account. Commits immediately." }
    update_contact     = { handler = "crm.handlers.update_contact.handler", description = "Update a contact's fields, including its status. Commits immediately." }
    create_opportunity = { handler = "crm.handlers.create_opportunity.handler", description = "Create a new opportunity under an account. Commits immediately. Creating an opportunity is low risk; later changing its stage, value, close date, owner, or primary contact is not, and requires a proposal." }
    update_opportunity = { handler = "crm.handlers.update_opportunity.handler", description = "Update low-risk opportunity fields such as name, description, source, and competitor. Stage, value, close date, owner, and primary contact are not changeable here — use submit_proposal for those." }
    log_activity       = { handler = "crm.handlers.log_activity.handler", description = "Log an activity against an opportunity. Commits immediately and updates the opportunity's last-touch timestamp in the same operation." }
    update_activity    = { handler = "crm.handlers.update_activity.handler", description = "Update a logged activity's subject, body, or timestamp. Commits immediately." }
    create_note        = { handler = "crm.handlers.create_note.handler", description = "Attach a note to an opportunity. Commits immediately." }

    submit_proposal     = { handler = "crm.handlers.submit_proposal.handler", description = "Propose a high-risk change — an opportunity stage transition, value change, close-date change, owner reassignment, primary-contact reassignment, or any delete. Nothing changes until the proposal is approved. Use this, not update_*, for those changes." }
    approve_proposal    = { handler = "crm.handlers.approve_proposal.handler", description = "Approve a pending proposal. The stored before-snapshot is re-checked against current state; if the record has changed since the proposal was raised, approval is refused as a conflict rather than overwriting the newer value." }
    reject_proposal     = { handler = "crm.handlers.reject_proposal.handler", description = "Reject a pending proposal. Terminal: a rejected proposal cannot be approved." }
    rebase_proposal     = { handler = "crm.handlers.rebase_proposal.handler", description = "Re-capture a pending proposal's before-snapshot from the record's current state, so a proposal refused as a conflict can be reviewed against what the record holds now. Changes nothing on the record itself and does not approve the proposal." }
    revert_audit_record = { handler = "crm.handlers.revert_audit_record.handler", description = "Reverse an audited change, restoring the recorded before-snapshot. Refused as a conflict if the record changed after the audit entry was written, or if this audit record has already been reverted." }

    get_page_context = { handler = "crm.handlers.get_page_context.handler", description = "Resolve the page-context descriptor you were given into confirmed facts: the record in view, the query a list or board shows, or a statement that the view cannot be summarised. It returns no rows; run a list or board query yourself with the search tools." }
  }

  tool_names = sort(keys(local.tools))

  # Tools the agents and the Gateway can call that get no HTTP route.
  agent_only_tools = ["get_page_context"]

  routed_tool_names = [for name in local.tool_names : name if !contains(local.agent_only_tools, name)]

  # Name prefix shared by every module and by each Lambda's `CRM_TABLE_PREFIX`.
  name_prefix = "crm-agent-eval"

  # ALB access-log prefix, used by both the load balancer (`api_origin`) and the log bucket's
  # delivery grant (`frontend_cdn`); the two must match.
  alb_access_log_prefix = "${local.name_prefix}-api-origin"

  # Gateway #2's scope, as a literal for the pre-token trigger: reading
  # `module.cognito.chat_scope_identifier` there would be a user pool -> trigger -> pool cycle.
  # `terraform_data.chat_scope_agreement` holds the two equal.
  chat_scope_identifier = "supervisor/chat"

  # `rep_id` of the service principal that external MCP `client_credentials` tokens map to. Its book
  # and tenant are read from that rep's `reps` item at request time.
  gateway_service_principal_rep_id = "svc-mcp-gateway"

  # Agent image tag, used by both the buildspec and `module.agentcore`: `sha-` plus the first 12 hex
  # characters of the agent source archive's SHA-256. Content-derived because ECR tags are immutable.
  agent_image_tag = "sha-${substr(filesha256("${var.lambda_build_dir}/agent-source.zip"), 0, 12)}"

  # Agent ECR repository name, passed into `module.agentcore` and also used to build the repository
  # ARN here without a reference back into that module, which would be a cycle.
  agent_ecr_repository_name = "crm-agent-eval-agent"

  # Inline CodeBuild buildspec: Docker build and ECR push. It reads only `ECR_REPOSITORY_URI` and
  # `AWS_DEFAULT_REGION` from the CodeBuild environment; the registry host is the URI up to its first
  # `/`, and the image tag is fixed at plan time from `local.agent_image_tag`.
  agent_buildspec = yamlencode({
    version = "0.2"
    phases = {
      pre_build = {
        commands = [
          "REGISTRY_HOST=$${ECR_REPOSITORY_URI%%/*}",
          "aws ecr get-login-password --region $${AWS_DEFAULT_REGION} | docker login --username AWS --password-stdin $${REGISTRY_HOST}",
        ]
      }
      build = {
        commands = [
          "docker build -t $${ECR_REPOSITORY_URI}:${local.agent_image_tag} .",
        ]
      }
      post_build = {
        commands = [
          "docker push $${ECR_REPOSITORY_URI}:${local.agent_image_tag}",
        ]
      }
    }
  })

  # Per-tool DynamoDB grants, transcribed from `lambda_action/table_action_role_matrix.json`. Every
  # grant's isolation is `application_code`, so none sets `leading_key_condition`.
  tool_dynamodb_grants = {
    search_opportunities = [
      { table = "opportunities", actions = ["dynamodb:Query"] },
    ]
    search_accounts = [
      { table = "accounts", actions = ["dynamodb:Query"] },
    ]
    get_opportunity = [
      { table = "opportunities", actions = ["dynamodb:GetItem"] },
      { table = "activities", actions = ["dynamodb:Query"] },
      { table = "notes", actions = ["dynamodb:Query"] },
      { table = "contacts", actions = ["dynamodb:Query"] },
    ]
    get_account = [
      { table = "accounts", actions = ["dynamodb:GetItem"] },
      { table = "contacts", actions = ["dynamodb:Query"] },
    ]
    get_contact = [
      { table = "contacts", actions = ["dynamodb:GetItem"] },
    ]
    list_activities = [
      { table = "opportunities", actions = ["dynamodb:GetItem"] },
      { table = "activities", actions = ["dynamodb:Query"] },
    ]
    pipeline_aggregate = [
      { table = "opportunities", actions = ["dynamodb:Query", "dynamodb:BatchGetItem"] },
    ]
    # BatchGetItem re-reads the base items, because `gsi_status`/`gsi_tenant` do not project
    # `before_snapshot`, which the scope check reads.
    list_proposals = [
      { table = "proposals", actions = ["dynamodb:Query", "dynamodb:BatchGetItem"] },
    ]
    list_audit_records = [
      { table = "audit_records", actions = ["dynamodb:Query"] },
    ]
    get_audit_record = [
      { table = "audit_records", actions = ["dynamodb:GetItem"] },
    ]
    draft_message = [
      { table = "contacts", actions = ["dynamodb:GetItem"] },
      { table = "opportunities", actions = ["dynamodb:GetItem"] },
    ]

    create_account = [
      { table = "accounts", actions = ["dynamodb:GetItem", "dynamodb:PutItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    update_account = [
      { table = "accounts", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    create_contact = [
      { table = "contacts", actions = ["dynamodb:GetItem", "dynamodb:PutItem"] },
      { table = "accounts", actions = ["dynamodb:GetItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    update_contact = [
      { table = "contacts", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    create_opportunity = [
      { table = "opportunities", actions = ["dynamodb:GetItem", "dynamodb:PutItem"] },
      { table = "accounts", actions = ["dynamodb:GetItem"] },
      { table = "contacts", actions = ["dynamodb:GetItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    update_opportunity = [
      { table = "opportunities", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    log_activity = [
      { table = "activities", actions = ["dynamodb:PutItem"] },
      { table = "opportunities", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    update_activity = [
      { table = "activities", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    create_note = [
      { table = "notes", actions = ["dynamodb:GetItem", "dynamodb:PutItem"] },
      { table = "opportunities", actions = ["dynamodb:GetItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]

    submit_proposal = [
      # GetItem because `Store.put_idempotent` re-reads the key before writing.
      { table = "proposals", actions = ["dynamodb:GetItem", "dynamodb:PutItem"] },
      { table = "accounts", actions = ["dynamodb:GetItem"] },
      { table = "contacts", actions = ["dynamodb:GetItem"] },
      # Query for the cascade checks a delete proposal on an account or contact runs against
      # `opportunities.gsi_account`.
      { table = "opportunities", actions = ["dynamodb:GetItem", "dynamodb:Query"] },
      { table = "activities", actions = ["dynamodb:GetItem"] },
    ]
    approve_proposal = [
      { table = "proposals", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"] },
      { table = "accounts", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"] },
      { table = "contacts", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"] },
      { table = "opportunities", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"] },
      { table = "activities", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
      # The cascade re-check before approving an account delete; scoped to the indexes in
      # `local.tool_query_indexes`, never the base table.
      { table = "opportunities", actions = ["dynamodb:Query"] },
    ]
    reject_proposal = [
      { table = "proposals", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"] },
      # Read-only: rejecting re-reads the proposal's target to check scope.
      { table = "accounts", actions = ["dynamodb:GetItem"] },
      { table = "contacts", actions = ["dynamodb:GetItem"] },
      { table = "opportunities", actions = ["dynamodb:GetItem"] },
      { table = "activities", actions = ["dynamodb:GetItem"] },
    ]
    # Read-only on the targets: the rebase re-reads the target to check scope and capture its
    # state; the only writes are the proposal Update and the audit Put, in one transaction.
    rebase_proposal = [
      { table = "proposals", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"] },
      { table = "accounts", actions = ["dynamodb:GetItem"] },
      { table = "contacts", actions = ["dynamodb:GetItem"] },
      { table = "opportunities", actions = ["dynamodb:GetItem"] },
      { table = "activities", actions = ["dynamodb:GetItem"] },
      { table = "audit_records", actions = ["dynamodb:PutItem"] },
    ]
    revert_audit_record = [
      { table = "audit_records", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"] },
      { table = "accounts", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:DeleteItem"] },
      { table = "contacts", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:DeleteItem"] },
      { table = "opportunities", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:DeleteItem"] },
      { table = "activities", actions = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:DeleteItem"] },
      # The child check before reverting a create; scoped to the indexes in
      # `local.tool_query_indexes`, never the base table.
      { table = "contacts", actions = ["dynamodb:Query"] },
      { table = "opportunities", actions = ["dynamodb:Query"] },
      { table = "activities", actions = ["dynamodb:Query"] },
      { table = "notes", actions = ["dynamodb:Query"] },
      # Reverting a note create deletes the note; nothing else on notes.
      { table = "notes", actions = ["dynamodb:DeleteItem"] },
    ]
    # GetItem only: the `entity` variant fetches one record for the scope check, and the handler
    # never runs the described query itself.
    get_page_context = [
      { table = "accounts", actions = ["dynamodb:GetItem"] },
      { table = "contacts", actions = ["dynamodb:GetItem"] },
      { table = "opportunities", actions = ["dynamodb:GetItem"] },
      { table = "activities", actions = ["dynamodb:GetItem"] },
    ]
  }

  # A Query-only grant listed here names exactly these GSIs and omits the table ARN; any other Query
  # grant covers all of its table's GSIs. Mirrors `crm.audit._CHILD_INDEXES`.
  tool_query_indexes = {
    approve_proposal = {
      opportunities = ["gsi_account"]
    }
    revert_audit_record = {
      contacts      = ["gsi_account"]
      opportunities = ["gsi_account"]
      activities    = ["gsi_account", "gsi_opportunity"]
      notes         = ["gsi_opportunity"]
    }
  }

  # Every DynamoDB action granted anywhere in this file, including the non-tool grants. Each role is
  # explicitly denied every action in this set that its grant on the same table does not name.
  # `tests/unit/test_infra_dynamodb_asserted_actions.py` checks the coverage.
  dynamodb_asserted_actions = distinct(flatten([
    [for tool, grants in local.tool_dynamodb_grants : [for grant in grants : grant.actions]],
    [for grant in local.sweep_control_dynamodb_grants : grant.actions],
  ]))

  # Reserved concurrency per tool Lambda: 40 for the three busiest reads, 20 otherwise.
  tool_reserved_concurrency = {
    for name in local.tool_names :
    name => contains(["search_opportunities", "get_opportunity", "pipeline_aggregate"], name) ? 40 : 20
  }

  # Per-tool top-level input properties, transcribed from each ToolDefinition.input_schema.
  # `type` values are the AgentCore `SchemaDefinition` enum. `SchemaDefinition` cannot carry
  # `minLength`, `enum`, `minimum`, `maximum` or `additionalProperties`; each handler's validator,
  # built from the same toolspec, enforces those.
  tool_schema_properties = {
    search_opportunities = [
      { name = "query", type = "string", description = "Words matched case-insensitively against the record name; every word must appear." },
      { name = "stage", type = "string" },
      { name = "owner_rep_id", type = "string" },
      { name = "account_id", type = "string" },
      { name = "cursor", type = "string", description = "The next_cursor from the previous call, with the same other arguments. Omit for the first page." },
      { name = "limit", type = "integer", description = "Maximum records to return. Default: no limit beyond the examination budget." },
    ]
    search_accounts = [
      { name = "query", type = "string", description = "Words matched case-insensitively against the record name; every word must appear." },
      { name = "owner_rep_id", type = "string" },
      { name = "cursor", type = "string", description = "The next_cursor from the previous call, with the same other arguments. Omit for the first page." },
      { name = "limit", type = "integer", description = "Maximum records to return. Default: no limit beyond the examination budget." },
    ]
    get_opportunity = [
      { name = "opportunity_id", type = "string", required = true },
    ]
    get_account = [
      { name = "account_id", type = "string", required = true },
    ]
    get_contact = [
      { name = "contact_id", type = "string", required = true },
    ]
    list_activities = [
      { name = "opportunity_id", type = "string", required = true },
      { name = "kind", type = "string" },
      { name = "cursor", type = "string", description = "The next_cursor from the previous call, with the same other arguments. Omit for the first page." },
      { name = "limit", type = "integer", description = "Maximum activities per page. Default 50." },
    ]
    pipeline_aggregate = [
      { name = "owner_rep_id", type = "string" },
      { name = "close_before", type = "string", description = "Exclusive bound on close_date, YYYY-MM-DD." },
      { name = "close_after", type = "string", description = "Exclusive bound on close_date, YYYY-MM-DD." },
      { name = "include_closed", type = "boolean", description = "Include Closed Won and Closed Lost. Default false." },
    ]
    list_proposals = [
      { name = "status", type = "string", description = "pending: awaiting a decision; approved/rejected: decided." },
      { name = "cursor", type = "string", description = "The next_cursor from the previous call, with the same other arguments. Omit for the first page." },
    ]
    list_audit_records = [
      { name = "target_id", type = "string" },
      { name = "cursor", type = "string", description = "The next_cursor from the previous call, with the same other arguments. Omit for the first page." },
    ]
    get_audit_record = [
      { name = "audit_id", type = "string", required = true },
    ]
    draft_message = [
      { name = "contact_id", type = "string", required = true },
      { name = "opportunity_id", type = "string" },
      { name = "intent", type = "string", required = true },
      { name = "tone", type = "string" },
    ]

    create_account = [
      { name = "name", type = "string", required = true },
      { name = "industry", type = "string" },
      { name = "region", type = "string" },
      { name = "website", type = "string" },
      { name = "employee_count", type = "integer" },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    update_account = [
      { name = "account_id", type = "string", required = true },
      { name = "fields", type = "object", required = true, description = "Fields to change, keyed by field name. Allowed keys: employee_count, industry, name, notes, region, website. Omit any field you are not changing." },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    create_contact = [
      { name = "account_id", type = "string", required = true },
      { name = "name", type = "string", required = true },
      { name = "title", type = "string" },
      { name = "email", type = "string" },
      { name = "phone", type = "string" },
      { name = "status", type = "string" },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    update_contact = [
      { name = "contact_id", type = "string", required = true },
      { name = "fields", type = "object", required = true, description = "Fields to change, keyed by field name. Allowed keys: email, name, phone, status, title. Omit any field you are not changing." },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    create_opportunity = [
      { name = "account_id", type = "string", required = true },
      { name = "name", type = "string", required = true },
      { name = "value", type = "number", description = "Deal value in whole US dollars." },
      { name = "close_date", type = "string", description = "Expected close date, YYYY-MM-DD." },
      { name = "stage", type = "string" },
      { name = "primary_contact_id", type = "string" },
      { name = "description", type = "string" },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    update_opportunity = [
      { name = "opportunity_id", type = "string", required = true },
      { name = "fields", type = "object", required = true, description = "Fields to change, keyed by field name. Allowed keys: competitor, description, name, source. Omit any field you are not changing. close_date, owner_rep_id, primary_contact_id, stage, value are not accepted here; use submit_proposal." },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    log_activity = [
      { name = "opportunity_id", type = "string", required = true },
      { name = "kind", type = "string", required = true },
      { name = "subject", type = "string", required = true },
      { name = "body", type = "string" },
      { name = "occurred_at", type = "string", description = "When it happened, ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ). Default: now." },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    update_activity = [
      { name = "activity_id", type = "string", required = true },
      { name = "fields", type = "object", required = true, description = "Fields to change, keyed by field name. Allowed keys: body, occurred_at, subject. Omit any field you are not changing." },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    create_note = [
      { name = "opportunity_id", type = "string", required = true },
      { name = "body", type = "string", required = true },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]

    submit_proposal = [
      { name = "target_table", type = "string", required = true },
      { name = "target_id", type = "string", required = true },
      { name = "operation", type = "string", required = true },
      { name = "after_payload", type = "object", description = "Intended field values. Omitted for a delete." },
      { name = "reason", type = "string", required = true, description = "Why this change is being made. Recorded on the proposal and the audit record." },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    approve_proposal = [
      { name = "proposal_id", type = "string", required = true },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    reject_proposal = [
      { name = "proposal_id", type = "string", required = true },
      { name = "reason", type = "string" },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    rebase_proposal = [
      { name = "proposal_id", type = "string", required = true },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    revert_audit_record = [
      { name = "audit_id", type = "string", required = true },
      { name = "reason", type = "string" },
      { name = "transaction_key", type = "string", required = true, description = "Idempotency key. Use a new unique value (for example a random UUID) for each distinct change. Reuse a key only to retry the exact same change: a retry is never applied twice and returns the original result or a conflict. Never reuse a key for a different change; it can be refused as a conflict." },
    ]
    get_page_context = [
      { name = "page_context", type = "object", required = true, description = "The PageContext descriptor for the view the user is currently looking at. Set `kind` to exactly one of the following, supplying that variant's fields: \"entity\" (requires `route`, `entity_type`, `entity_id`); \"list\" (requires `route`, `entity_type`; optional `query`); \"board\" (requires `route`; optional `query`); \"opaque\" (requires `route`, `reason`). An optional `query` carries `filters`/`sort`/`page`. Carries no retrieved rows." },
    ]
  }
}

# A misspelt `agent_only_tools` entry would silently route every tool.
resource "terraform_data" "agent_only_tools_guard" {
  lifecycle {
    precondition {
      condition     = length(setsubtract(toset(local.agent_only_tools), toset(local.tool_names))) == 0
      error_message = "Every local.agent_only_tools entry must be a tool in local.tool_names; not found: ${join(", ", setsubtract(toset(local.agent_only_tools), toset(local.tool_names)))}."
    }
  }
}

# ---------------------------------------------------------------------------
# dynamodb — 11 tables, on-demand, KMS CMK, PITR.
# ---------------------------------------------------------------------------

module "dynamodb" {
  source = "./modules/dynamodb"

  name_prefix         = local.name_prefix
  data_classification = var.data_classification

  account_id                   = data.aws_caller_identity.current.account_id
  caller_principal_arn         = data.aws_iam_session_context.current.issuer_arn
  data_plane_principal_arns    = concat(local.dynamodb_data_plane_role_arns, var.crm_operator_principal_arns)
  administrator_principal_arns = concat(var.crm_operator_principal_arns, ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"])
}

# The execution role of every `lambda_action` call with `dynamodb_kms_grant_enabled = true`.
locals {
  dynamodb_data_plane_role_arns = concat(
    [for tool in module.lambda_action : tool.role_arn],
    [
      module.lambda_action_interceptor.role_arn,
      module.lambda_action_eval_trigger.role_arn,
      module.lambda_action_pre_token_generation.role_arn,
      module.lambda_action_sweep_control.role_arn,
      module.lambda_action_sweep_task.role_arn,
      module.lambda_action_chat_interceptor.role_arn,
      module.lambda_action_chat_target.role_arn,
      module.lambda_action_user_settings.role_arn,
    ],
  )
}

data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

# ---------------------------------------------------------------------------
# observability — CloudWatch Logs CMK (shared by the log groups below),
# CloudTrail, eval metric namespace.
# ---------------------------------------------------------------------------

module "observability" {
  source = "./modules/observability"

  name_prefix         = local.name_prefix
  data_classification = var.data_classification
  log_retention_days  = 365

  caller_principal_arn         = data.aws_iam_session_context.current.issuer_arn
  logs_key_user_principal_arns = concat(local.logs_key_user_role_arns, var.crm_operator_principal_arns)
  operator_principal_arns      = var.crm_operator_principal_arns
  administrator_principal_arns = concat(var.crm_operator_principal_arns, ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"])
}

# The execution role of every module call that encrypts its Lambda environment with the logs CMK.
# Each `local.tools` key is listed by a static index: referencing the whole `module.lambda_action`
# object would make the key wait on the functions it encrypts.
locals {
  logs_key_user_role_arns = concat(
    [
      module.lambda_action["search_opportunities"].role_arn,
      module.lambda_action["search_accounts"].role_arn,
      module.lambda_action["get_opportunity"].role_arn,
      module.lambda_action["get_account"].role_arn,
      module.lambda_action["get_contact"].role_arn,
      module.lambda_action["list_activities"].role_arn,
      module.lambda_action["pipeline_aggregate"].role_arn,
      module.lambda_action["list_proposals"].role_arn,
      module.lambda_action["list_audit_records"].role_arn,
      module.lambda_action["get_audit_record"].role_arn,
      module.lambda_action["draft_message"].role_arn,
      module.lambda_action["create_account"].role_arn,
      module.lambda_action["update_account"].role_arn,
      module.lambda_action["create_contact"].role_arn,
      module.lambda_action["update_contact"].role_arn,
      module.lambda_action["create_opportunity"].role_arn,
      module.lambda_action["update_opportunity"].role_arn,
      module.lambda_action["log_activity"].role_arn,
      module.lambda_action["update_activity"].role_arn,
      module.lambda_action["create_note"].role_arn,
      module.lambda_action["submit_proposal"].role_arn,
      module.lambda_action["approve_proposal"].role_arn,
      module.lambda_action["reject_proposal"].role_arn,
      module.lambda_action["rebase_proposal"].role_arn,
      module.lambda_action["revert_audit_record"].role_arn,
      module.lambda_action["get_page_context"].role_arn,
    ],
    [
      module.lambda_action_interceptor.role_arn,
      module.lambda_action_eval_trigger.role_arn,
      module.lambda_action_pre_token_generation.role_arn,
      module.lambda_action_sweep_control.role_arn,
      module.lambda_action_sweep_task.role_arn,
      module.lambda_action_chat_interceptor.role_arn,
      module.lambda_action_chat_target.role_arn,
      module.lambda_action_user_settings.role_arn,
      module.lambda_action_capabilities.role_arn,
      module.agent_relay.role_arn,
    ],
  )
}

# ---------------------------------------------------------------------------
# vpc — the private network every Lambda below runs in. No internet gateway or
# public subnet; the NAT path is optional (`vpc_nat_gateway_enabled`). A call to
# an AWS service with no endpoint listed here hangs until the SDK times out.
# ---------------------------------------------------------------------------

locals {
  # Interface endpoints for every AWS API called from inside the VPC:
  #   logs               every Lambda's log group
  #   kms                table and log CMKs, and env-var decryption at init
  #   lambda             action-Lambda invocations from inside the VPC
  #   ssm                the relay's audience parameter
  #   bedrock-agentcore  the relay's InvokeAgentRuntime data plane (not -control or .gateway)
  # cognito-idp is absent: a user pool with a domain cannot be reached over PrivateLink, so the
  # relay's JWKS fetch needs internet egress. dynamodb and s3 use the gateway endpoints below.
  vpc_interface_endpoint_services = [
    "bedrock-agentcore",
    #   bedrock-runtime    model calls from the AgentCore Runtime
    #   ecr.api / ecr.dkr  container image pulls by AgentCore for the Runtime
    "bedrock-runtime",
    "ecr.api",
    "ecr.dkr",
    #   execute-api        associated with the private REST API; CloudFront's VPC origin is the caller
    "execute-api",
    "kms",
    "lambda",
    "logs",
    "ssm",
    #   states             eval_trigger's StartExecution / DescribeExecution
    "states",
  ]
}

module "vpc" {
  source = "./modules/vpc"

  name_prefix = local.name_prefix
  region      = var.aws_region

  # Network ids arrive through the gitignored tfvars, never as tracked content.
  vpc_id                  = var.vpc_id
  vpc_cidr_block          = var.vpc_cidr_block
  private_subnet_ids      = var.vpc_private_subnet_ids
  private_route_table_ids = var.vpc_private_route_table_ids

  # AgentCore Runtime subnets, kept out of the interface-endpoint subnet set and the NAT route.
  runtime_subnet_ids      = var.vpc_runtime_subnet_ids
  runtime_route_table_ids = var.vpc_runtime_route_table_ids

  interface_endpoint_service_names = local.vpc_interface_endpoint_services
  gateway_endpoint_service_names   = ["s3", "dynamodb"]

  # No default: the plan fails until a value is set. See `modules/vpc/nat.tf`.
  nat_gateway_enabled  = var.vpc_nat_gateway_enabled
  nat_public_subnet_id = var.vpc_nat_public_subnet_id

  flow_log_enabled     = var.vpc_flow_log_enabled
  flow_log_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  tags = {
    "data-classification" = var.data_classification
  }
}

# ---------------------------------------------------------------------------
# lambda_action — one instantiation per tool, plus the identity
# interceptor, the pre-token-generation trigger, the two eval functions and the
# capabilities handler below. The API Gateway invoke permission for these
# functions lives in `modules/api`.
# ---------------------------------------------------------------------------

module "lambda_action" {
  for_each = local.tools
  source   = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "crm-agent-eval-${each.key}"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = each.value.handler
  runtime                = "python3.13"
  filename               = "${var.lambda_build_dir}/${each.key}.zip"
  source_code_hash       = filebase64sha256("${var.lambda_build_dir}/${each.key}.zip")

  timeout_seconds = 30
  memory_size_mb  = 256

  environment_variables = merge(
    { CRM_TABLE_PREFIX = local.name_prefix },
    contains(local.routed_tool_names, each.key) ? { CRM_API_ALLOWED_CLIENT_IDS_PARAMETER = local.api_allowed_client_ids_parameter_name } : {},
    contains(local.cursor_tools, each.key) ? { CRM_CURSOR_KMS_KEY_ARN = aws_kms_key.cursor.arn } : {},
  )
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  config_parameter_arns = contains(local.routed_tool_names, each.key) ? [local.api_allowed_client_ids_parameter_arn] : []

  cursor_kms_key_arn       = contains(local.cursor_tools, each.key) ? aws_kms_key.cursor.arn : null
  cursor_kms_grant_enabled = contains(local.cursor_tools, each.key)

  reserved_concurrent_executions = local.reserved_concurrency[each.key]

  dynamodb_grants = [
    for grant in local.tool_dynamodb_grants[each.key] : {
      table_name = grant.table
      table_arn  = module.dynamodb.table_arns[grant.table]
      actions    = grant.actions
      # Every Query targets a GSI, so a Query grant covers all of that table's GSI ARNs unless
      # `local.tool_query_indexes` scopes a Query-only grant to named indexes.
      index_arns = !contains(grant.actions, "dynamodb:Query") ? [] : (
        length(grant.actions) == 1 && try(local.tool_query_indexes[each.key][grant.table], null) != null
        ? [for index in local.tool_query_indexes[each.key][grant.table] : module.dynamodb.gsi_arns[grant.table][index]]
        : values(module.dynamodb.gsi_arns[grant.table])
      )
      include_table_arn = !(length(grant.actions) == 1 && contains(grant.actions, "dynamodb:Query") && try(local.tool_query_indexes[each.key][grant.table], null) != null)
    }
  ]

  # Explicit Deny on every table this tool is not granted. `table/X` does not match
  # `table/X/index/Y`, so each table's index wildcard is listed too.
  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if !contains([for grant in local.tool_dynamodb_grants[each.key] : grant.table], table_name)
  ])

  # Explicit Deny, on each granted table, of every asserted action this tool's grants on that table
  # do not name. Grants are unioned per table first.
  denied_own_table_actions = [
    for table_name in distinct([for grant in local.tool_dynamodb_grants[each.key] : grant.table]) : {
      table_name = table_name
      table_arn  = module.dynamodb.table_arns[table_name]
      actions = setsubtract(local.dynamodb_asserted_actions, flatten([
        for grant in local.tool_dynamodb_grants[each.key] : grant.actions if grant.table == table_name
      ]))
      # `table/X` does not match `table/X/index/Y`, so the Deny also names the index wildcard.
      index_arns = ["${module.dynamodb.table_arns[table_name]}/index/*"]
    }
  ]

  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true

  table_action_role_matrix_entries = [
    for grant in local.tool_dynamodb_grants[each.key] : {
      table                 = grant.table
      action                = grant.actions[0]
      role                  = "crm-agent-eval-${each.key}-role"
      leading_keys_enforced = false
      isolation_mechanism   = "application_code"
    }
  ]

  gateway_target_role_arn     = aws_iam_role.agentcore_gateway.arn
  attach_gateway_target_grant = true
}

# The Gateway REQUEST interceptor Lambda.
module "lambda_action_interceptor" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "crm-agent-eval-identity-interceptor"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "gateway.identity_interceptor.lambda_handler"
  runtime                = "python3.13"
  filename               = "${var.lambda_build_dir}/identity_interceptor.zip"
  source_code_hash       = filebase64sha256("${var.lambda_build_dir}/identity_interceptor.zip")

  timeout_seconds = 10
  memory_size_mb  = 256

  environment_variables = {
    COGNITO_DISCOVERY_URL      = "https://cognito-idp.${var.aws_region}.amazonaws.com/${module.cognito.user_pool_id}/.well-known/openid-configuration"
    COGNITO_ALLOWED_CLIENT_IDS = join(",", module.cognito.allowed_client_ids)
    # `gateway.identity_interceptor` reads all of these and fails every request when
    # `COGNITO_JWKS_URL` is empty. `COGNITO_ALLOWED_SCOPES` re-checks the Gateway authorizer's scope.
    COGNITO_JWKS_URL       = "https://cognito-idp.${var.aws_region}.amazonaws.com/${module.cognito.user_pool_id}/.well-known/jwks.json"
    COGNITO_ALLOWED_SCOPES = module.cognito.gateway_scope_identifier
    # The service principal external MCP `client_credentials` tokens map to. Cognito does not run
    # the pre-token-generation trigger for that grant, so such tokens carry no `custom:rep_id`.
    # Unset, every machine token is refused.
    GATEWAY_SERVICE_PRINCIPAL_REP_ID = local.gateway_service_principal_rep_id
    CRM_TABLE_PREFIX                 = local.name_prefix
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  reserved_concurrent_executions = local.reserved_concurrency["identity_interceptor"]

  # GetItem on `reps` only, pinned by `LeadingKeys` to the service principal's `rep_id`.
  dynamodb_grants = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = ["dynamodb:GetItem"]
      leading_key_condition = {
        attribute = "rep_id"
        values    = [local.gateway_service_principal_rep_id]
      }
    },
  ]
  table_action_role_matrix_entries = [
    {
      table                 = "reps"
      action                = "dynamodb:GetItem"
      role                  = "crm-agent-eval-identity-interceptor-role"
      leading_keys_enforced = true
      isolation_mechanism   = "leading_keys"
    },
  ]

  # Explicit Deny on every other table.
  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if table_name != "reps"
  ])
  # Explicit Deny of every other asserted action on `reps`.
  denied_own_table_actions = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = setsubtract(local.dynamodb_asserted_actions, ["dynamodb:GetItem"])
      index_arns = ["${module.dynamodb.table_arns["reps"]}/index/*"]
    },
  ]
  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true

  # No API Gateway route invokes the interceptor. The Gateway does, through this grant and
  # `aws_lambda_permission.gateway_invoke_interceptor` in `modules/agentcore`.
  gateway_target_role_arn     = aws_iam_role.agentcore_gateway.arn
  attach_gateway_target_grant = true
}

# The ENI function-code Deny, attached to every VPC Lambda role as a managed policy.
module "eni_function_code_deny" {
  source = "./modules/eni_function_code_deny"

  name_prefix = local.name_prefix
}

# ---------------------------------------------------------------------------
# eval_trigger — the API-facing half of an eval sweep: submit, status, list and
# cancel over `eval_runs`, and `states:StartExecution` of the sweep state machine
# (`eval_sfn.tf`), which runs the sweep itself.
#
# One zip serves it, the capabilities and user-settings handlers, and the state
# machine's two functions. Its packages sit under `src/` inside the archive and
# `PYTHONPATH` puts them on the path; config YAML resolves relative to that
# layout (see `scripts/package_lambdas.sh`).
# ---------------------------------------------------------------------------

locals {
  eval_lambda_zip = "${var.lambda_build_dir}/eval_trigger.zip"

  # eval_trigger's `eval_runs` access: base-table rows keyed `job#*`,
  # `lock#sweep` and the daily live quota `livequota#*`; `DeleteItem` only on `lock#sweep`;
  # `gsi_status` only under its `job` partition.
  eval_trigger_eval_runs_grants = [
    {
      table_name = "eval_runs"
      table_arn  = module.dynamodb.table_arns["eval_runs"]
      actions    = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:BatchGetItem"]
      leading_key_condition = {
        attribute = "dynamodb:LeadingKeys"
        operator  = "StringLike"
        values    = ["job#*", "lock#sweep", "livequota#*"]
      }
    },
    {
      table_name = "eval_runs"
      table_arn  = module.dynamodb.table_arns["eval_runs"]
      actions    = ["dynamodb:DeleteItem"]
      leading_key_condition = {
        attribute = "dynamodb:LeadingKeys"
        operator  = "StringEquals"
        values    = ["lock#sweep"]
      }
    },
    {
      table_name        = "eval_runs"
      table_arn         = module.dynamodb.table_arns["eval_runs"]
      actions           = ["dynamodb:Query"]
      include_table_arn = false
      index_arns        = [module.dynamodb.gsi_arns["eval_runs"]["gsi_status"]]
      leading_key_condition = {
        attribute = "dynamodb:LeadingKeys"
        values    = ["job"]
      }
    },
  ]
}

module "lambda_action_eval_trigger" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "crm-agent-eval-eval-trigger"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "crm.handlers.eval_trigger.handler"
  runtime                = "python3.13"
  filename               = local.eval_lambda_zip
  source_code_hash       = filebase64sha256(local.eval_lambda_zip)

  timeout_seconds = 30
  memory_size_mb  = 512

  environment_variables = {
    CRM_TABLE_PREFIX                     = local.name_prefix
    CRM_API_ALLOWED_CLIENT_IDS_PARAMETER = local.api_allowed_client_ids_parameter_name
    # Required for this zip's layout: the first-party packages live under `src/`.
    PYTHONPATH = "/var/task/src"
    # The state machine `submit_sweep` starts. Constructed: the resource reference is an
    # eval_trigger -> state machine -> sweep_task -> Runtime -> API -> eval_trigger cycle.
    EVAL_STATE_MACHINE_ARN = local.eval_sweep_state_machine_arn
    # The artefact root `artefact_url` presigns under, as sweep_control writes it.
    EVAL_ARTIFACTS_BUCKET = module.eval_artifacts.bucket_name
    EVAL_ARTIFACTS_PREFIX = module.eval_artifacts.artifact_prefix
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn
  config_parameter_arns   = [local.api_allowed_client_ids_parameter_arn]

  reserved_concurrent_executions = local.reserved_concurrency["eval_trigger"]

  dynamodb_grants = local.eval_trigger_eval_runs_grants

  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if table_name != "eval_runs"
  ])

  denied_own_table_actions = [
    {
      table_name = "eval_runs"
      table_arn  = module.dynamodb.table_arns["eval_runs"]
      actions    = setsubtract(local.dynamodb_asserted_actions, flatten([for g in local.eval_trigger_eval_runs_grants : g.actions]))
      index_arns = ["${module.dynamodb.table_arns["eval_runs"]}/index/*"]
    },
  ]

  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true

  # Routed at `POST /eval_trigger` through `module.api`'s `extra_routes`; not a Gateway target.
  gateway_target_role_arn = null
}

# capabilities — `GET /api/capabilities`, the model/effort surface the SPA reads on load.
#
# The handler reads `config/model_capabilities.yaml` from its own archive and touches no table, so
# `dynamodb_grants = []` and the role's only inline DynamoDB policy is an explicit Deny on every
# table and index. It is a third handler on `local.eval_lambda_zip`.
module "lambda_action_capabilities" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "crm-agent-eval-capabilities"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "crm.handlers.capabilities.handler"
  runtime                = "python3.13"
  filename               = local.eval_lambda_zip
  source_code_hash       = filebase64sha256(local.eval_lambda_zip)

  timeout_seconds = 10
  memory_size_mb  = 256

  environment_variables = {
    CRM_API_ALLOWED_CLIENT_IDS_PARAMETER = local.api_allowed_client_ids_parameter_name
    # Required for this zip's layout: the first-party packages live under `src/`.
    PYTHONPATH = "/var/task/src"
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn
  config_parameter_arns   = [local.api_allowed_client_ids_parameter_arn]

  reserved_concurrent_executions = local.reserved_concurrency["capabilities"]

  dynamodb_grants = []

  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
  ])

  # Routed at `GET /capabilities` through `module.api`'s `extra_routes`; not a Gateway target.
  gateway_target_role_arn = null
}

# The `states` endpoint carries only the eval sweep's two calls, from this account's principals.
# `Principal = "*"` scopes traffic through the endpoint; it grants nothing on any resource.
resource "aws_vpc_endpoint_policy" "states" {
  vpc_endpoint_id = module.vpc.states_vpc_endpoint_id
  policy          = data.aws_iam_policy_document.states_endpoint.json
}

data "aws_iam_policy_document" "states_endpoint" {
  statement {
    sid       = "StartTheEvalSweep"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [local.eval_sweep_state_machine_arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid       = "DescribeEvalSweepExecutions"
    effect    = "Allow"
    actions   = ["states:DescribeExecution"]
    resources = [local.eval_sweep_execution_arn_pattern]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# Start the eval sweep and read its executions; never `StopExecution`.
resource "aws_iam_role_policy" "eval_trigger_states" {
  name = "${local.name_prefix}-eval-trigger-states"
  role = module.lambda_action_eval_trigger.role_name

  policy = data.aws_iam_policy_document.eval_trigger_states.json
}

data "aws_iam_policy_document" "eval_trigger_states" {
  statement {
    sid       = "StartTheEvalSweep"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [local.eval_sweep_state_machine_arn]
  }

  statement {
    sid       = "DescribeEvalSweepExecutions"
    effect    = "Allow"
    actions   = ["states:DescribeExecution"]
    resources = [local.eval_sweep_execution_arn_pattern]
  }
}

# Read artefacts under the single artefact prefix, so `artefact_url` can presign a GET as this role.
# No ListBucket, no write, no other prefix.
resource "aws_iam_role_policy" "eval_trigger_artifacts" {
  name = "${local.name_prefix}-eval-trigger-artifacts"
  role = module.lambda_action_eval_trigger.role_name

  policy = data.aws_iam_policy_document.eval_trigger_artifacts.json
}

data "aws_iam_policy_document" "eval_trigger_artifacts" {
  statement {
    sid       = "ReadArtifactPrefix"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${module.eval_artifacts.bucket_arn}/${module.eval_artifacts.artifact_prefix}/*"]
  }

  statement {
    sid       = "DecryptArtifactsViaS3"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.eval_artifacts.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.aws_region}.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# cognito — user pool, SPA + MCP app clients, pre-token-generation V2 trigger.
#
# The trigger handler is `src/crm/auth/pre_token_generation.py:lambda_handler`,
# packaged as `pre_token_generation.zip` by `scripts/package_lambdas.sh`.
# ---------------------------------------------------------------------------

# The pre-token-generation trigger runs in the private VPC like every other Lambda, inside Cognito's
# token-issuance path, which enforces a short trigger timeout. Its dependencies (`reps`, Logs, KMS)
# are all reached through `module.vpc`'s endpoints. A sign-in can fail with `ENINotReadyException`
# while the ENI warms up after an apply.
module "lambda_action_pre_token_generation" {
  source = "./modules/lambda_action"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name = "crm-agent-eval-pre-token-generation"

  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids
  handler                = "crm.auth.pre_token_generation.lambda_handler"
  runtime                = "python3.13"
  filename               = "${var.lambda_build_dir}/pre_token_generation.zip"
  source_code_hash       = filebase64sha256("${var.lambda_build_dir}/pre_token_generation.zip")

  timeout_seconds = 10
  memory_size_mb  = 128

  environment_variables = {
    CRM_TABLE_PREFIX = local.name_prefix
    # Added to user access tokens through `scopesToAdd`.
    CHAT_SCOPE = local.chat_scope_identifier
  }
  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  reserved_concurrent_executions = local.reserved_concurrency["pre_token_generation"]

  dynamodb_grants = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = ["dynamodb:GetItem"]
    },
  ]
  denied_table_arns = flatten([
    for table_name, table_arn in module.dynamodb.table_arns : [table_arn, "${table_arn}/index/*"]
    if table_name != "reps"
  ])
  # Explicit Deny of every other asserted action on `reps`.
  denied_own_table_actions = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = setsubtract(local.dynamodb_asserted_actions, ["dynamodb:GetItem"])
      index_arns = ["${module.dynamodb.table_arns["reps"]}/index/*"]
    },
  ]
  dynamodb_kms_key_arn       = module.dynamodb.kms_key_arn
  dynamodb_kms_grant_enabled = true
  # No `leading_key_condition`: the trigger reads the row of whichever user is signing in
  # (`event["userName"]`), so there is no static value to pin.
  table_action_role_matrix_entries = [
    {
      table                 = "reps"
      action                = "dynamodb:GetItem"
      role                  = "crm-agent-eval-pre-token-generation-role"
      leading_keys_enforced = false
      isolation_mechanism   = "not_applicable"
    },
  ]

  # No API Gateway route; the user pool invokes this function through
  # `aws_lambda_permission.pre_token_generation` in `modules/cognito`.
  gateway_target_role_arn     = aws_iam_role.agentcore_gateway.arn
  attach_gateway_target_grant = true
}

module "cognito" {
  source = "./modules/cognito"

  user_pool_name = "crm-agent-eval"

  pre_token_generation_lambda_arn           = module.lambda_action_pre_token_generation.function_arn
  pre_token_generation_lambda_function_name = module.lambda_action_pre_token_generation.function_name

  mcp_client_generate_secret = true

  spa_callback_urls = ["https://${module.frontend_cdn.distribution_domain_name}/callback"]
  spa_logout_urls   = ["https://${module.frontend_cdn.distribution_domain_name}/logout"]
}

resource "terraform_data" "chat_scope_agreement" {
  lifecycle {
    precondition {
      condition     = module.cognito.chat_scope_identifier == local.chat_scope_identifier
      error_message = "local.chat_scope_identifier (the pre-token trigger's CHAT_SCOPE) must equal module.cognito.chat_scope_identifier, the scope Gateway #2 allows and the interceptor checks."
    }

    precondition {
      condition     = contains(keys(jsondecode(file("${path.module}/regions.json")).regions), var.aws_region)
      error_message = "aws_region ${var.aws_region} is not in infra/regions.json; supported: ${join(", ", sort(keys(jsondecode(file("${path.module}/regions.json")).regions)))}."
    }
  }
}

# ---------------------------------------------------------------------------
# api — PRIVATE REST API, one route per `local.routed_tool_names` entry plus `extra_routes`, a
# COGNITO_USER_POOLS authorizer on every one.
# ---------------------------------------------------------------------------

module "api" {
  source = "./modules/api"

  api_name   = "crm-agent-eval-api"
  stage_name = "prod"

  # Every tool except `local.agent_only_tools`.
  tool_lambda_invoke_arns = {
    for name in local.routed_tool_names : name => module.lambda_action[name].invoke_arn
  }
  tool_lambda_function_names = {
    for name in local.routed_tool_names : name => module.lambda_action[name].function_name
  }

  # The non-tool routes, kept out of the tool maps so `modules/api`'s tool-map key parity holds.
  # Both carry the same Cognito authorizer. Neither is in the toolspec, so neither is published to
  # MCP clients. `capabilities` is `GET` because `frontend/src/components/tuning/capabilityMatrix.ts`
  # fetches it with GET.
  extra_routes = {
    eval_trigger = {
      invoke_arn    = module.lambda_action_eval_trigger.invoke_arn
      function_name = module.lambda_action_eval_trigger.function_name
    }
    capabilities = {
      invoke_arn    = module.lambda_action_capabilities.invoke_arn
      function_name = module.lambda_action_capabilities.function_name
      method        = "GET"
    }
    user_settings = {
      invoke_arn    = module.lambda_action_user_settings.invoke_arn
      function_name = module.lambda_action_user_settings.function_name
    }
  }

  # A COGNITO_USER_POOLS authorizer takes the pool ARN, not an issuer and audience.
  cognito_user_pool_arn = module.cognito.user_pool_arn
  vpc_endpoint_ids      = [module.vpc.execute_api_vpc_endpoint_id]

  log_retention_days     = 365
  access_log_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn
}

# ---------------------------------------------------------------------------
# api_origin — CloudFront VPC origin -> internal ALB -> the `execute-api`
# interface endpoint's ENI IPs -> the private REST API.
#
# The ENI addresses are looked up here and passed in as `execute_api_target_ips`,
# so the module itself plans without credentials. `count` rather than `for_each`:
# the ENI ids are unknown on a first plan, but their count (one per private
# subnet) is known.
# ---------------------------------------------------------------------------

data "aws_network_interface" "execute_api" {
  count = length(var.vpc_private_subnet_ids)

  id = module.vpc.execute_api_network_interface_ids[count.index]
}

module "api_origin" {
  source = "./modules/api_origin"

  name_prefix = local.name_prefix

  vpc_id = module.vpc.vpc_id
  # Read from `module.vpc` so the ALB's subnets match the endpoint's; the target group indexes the
  # ENI list positionally against this list.
  private_subnet_ids = module.vpc.lambda_subnet_ids

  execute_api_security_group_id = module.vpc.execute_api_security_group_id
  execute_api_target_ips        = [for eni in data.aws_network_interface.execute_api : eni.private_ip]

  alb_ingress_security_group_ids = var.api_origin_ingress_security_group_ids

  access_logs_bucket = module.frontend_cdn.logs_bucket_name
  access_logs_prefix = local.alb_access_log_prefix

  tags = {
    "data-classification" = var.data_classification
  }
}

# The ALB's 443 ingress on the endpoint security group. Declared at the root because it needs an
# output from each of `api_origin` and `vpc`; inside either module it would create a cycle. Without
# it the ALB's health checks fail and every `/api/*` request fails at the edge.
resource "aws_vpc_security_group_ingress_rule" "endpoints_from_api_origin_alb" {
  description                  = "HTTPS from the private-origin ALB. The only ingress any endpoint has besides the Lambda group."
  security_group_id            = module.vpc.execute_api_security_group_id
  referenced_security_group_id = module.api_origin.alb_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

# ---------------------------------------------------------------------------
# frontend_cdn — private S3 + OAC + CloudFront, routing to the api and agent
# relay origins wired from their module outputs.
# ---------------------------------------------------------------------------

module "frontend_cdn" {
  source = "./modules/frontend_cdn"

  name_prefix         = local.name_prefix
  data_classification = var.data_classification

  # This module's logs bucket is also the ALB's access-log destination; its policy grants log
  # delivery under exactly this prefix.
  alb_access_log_prefix = local.alb_access_log_prefix

  # Sent only as the `Host` header: a CloudFront VPC origin routes to the load balancer by ARN and
  # does no DNS lookup on this value.
  api_gateway_domain_name = module.api.private_origin_domain_name
  api_gateway_stage_name  = module.api.stage_name

  # Attaches the VPC origin to the `/api/*` behaviour.
  api_origin_vpc_origin_id = module.api_origin.vpc_origin_id

  # `/agent/*` reaches the Runtime only through the relay. The relay origin uses OAC: CloudFront
  # SigV4-signs each origin request to the `AWS_IAM` Function URL, and the invoke grant is scoped to
  # this distribution.
  agent_relay_function_url_domain = module.agent_relay.function_url_domain
  agent_relay_function_name       = module.agent_relay.function_name

  log_retention_days = 90

  origin_noncurrent_version_retention_days = 30

  caller_principal_arn         = data.aws_iam_session_context.current.issuer_arn
  operator_principal_arns      = var.crm_operator_principal_arns
  administrator_principal_arns = concat(var.crm_operator_principal_arns, ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"])

  csp_script_hashes     = local.spa_inline_script_hashes
  csp_connect_src_extra = local.spa_connect_src_extra
}

# The SPA's Content-Security-Policy sources: the hash of each attribute-less inline `<script>` in
# `frontend/index.html`, and the Cognito origins the browser calls directly.
locals {
  spa_inline_script_hashes = [
    for script in regexall("(?s)<script>(.*?)</script>", file("${path.module}/../frontend/index.html")) :
    "sha256-${base64sha256(script[0])}"
  ]
  spa_connect_src_extra = [
    "https://cognito-idp.${var.aws_region}.amazonaws.com",
    "https://${module.cognito.domain}.auth.${var.aws_region}.amazoncognito.com",
  ]
}

# ---------------------------------------------------------------------------
# eval_artifacts — dedicated bucket for eval sweep artefacts, read only through
# a presigned GET. Not a prefix in `frontend_cdn`'s origin bucket, which is
# anonymously readable through the distribution, and not a CloudFront origin.
# ---------------------------------------------------------------------------

# The artefacts bucket's CMK, declared at the root so the module's bucket policy and writer grant
# render at plan time. A dedicated key, separate from the observability CMK the log groups use.
resource "aws_kms_key" "eval_artifacts" {
  # checkov:skip=CKV2_AWS_64: policy managed by aws_kms_key_policy.eval_artifacts
  description             = "${local.name_prefix} eval sweep artefacts (S3 SSE-KMS)"
  enable_key_rotation     = true
  deletion_window_in_days = 7

  tags = {
    "data-classification" = var.data_classification
  }
}

resource "aws_kms_alias" "eval_artifacts" {
  name          = "alias/${local.name_prefix}-eval-artifacts"
  target_key_id = aws_kms_key.eval_artifacts.key_id
}

# The roles that read or write the artefacts bucket: the sweep writer, the presigned-GET signer, and
# the image build reading the agent-source object.
locals {
  eval_artifacts_principal_arns = [
    module.lambda_action_eval_trigger.role_arn,
    module.lambda_action_sweep_control.role_arn,
    module.agentcore.codebuild_role_arn,
  ]
}

# Keeps the account-root statement and denies key use and key administration outside the lists.
resource "aws_kms_key_policy" "eval_artifacts" {
  key_id                             = aws_kms_key.eval_artifacts.id
  bypass_policy_lockout_safety_check = false

  lifecycle {
    precondition {
      condition     = contains(concat(var.crm_operator_principal_arns, ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]), data.aws_iam_session_context.current.issuer_arn) && data.aws_iam_session_context.current.issuer_arn != "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      error_message = "The caller running this plan is not an operator. Add its role or user ARN to crm_operator_principal_arns in terraform.tfvars, or run as a listed operator; nothing was applied."
    }
  }

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "Enable IAM User Permissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "DenyKeyUseToNonProjectPrincipals"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
          "kms:CreateGrant",
        ]
        Resource = "*"
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = concat(local.eval_artifacts_principal_arns, var.crm_operator_principal_arns) }
          Bool         = { "aws:PrincipalIsAWSService" = "false" }
        }
      },
      {
        Sid       = "DenyKeyAdministrationToNonOperators"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "kms:PutKeyPolicy",
          "kms:ScheduleKeyDeletion",
          "kms:DisableKey",
          "kms:RevokeGrant",
          "kms:DisableKeyRotation",
          "kms:CreateAlias",
          "kms:UpdateAlias",
          "kms:DeleteAlias",
          "kms:TagResource",
          "kms:UntagResource",
          "kms:UpdateKeyDescription",
        ]
        Resource = "*"
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = concat(var.crm_operator_principal_arns, ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]) }
        }
      },
    ]
  })
}

module "eval_artifacts" {
  source = "./modules/eval_artifacts"

  kms_key_arn = aws_kms_key.eval_artifacts.arn

  name_prefix         = local.name_prefix
  data_classification = var.data_classification
  account_id          = data.aws_caller_identity.current.account_id

  # The CDN module's SSE-S3 access-log bucket; S3 server access logging cannot deliver to an
  # SSE-KMS bucket.
  access_log_bucket_id = module.frontend_cdn.logs_bucket_name

  # The writer is `sweep_control`, whose own policy grants the prefix (`eval_sfn.tf`).
  writer_role_name = null

  caller_principal_arn         = data.aws_iam_session_context.current.issuer_arn
  data_plane_principal_arns    = concat(local.eval_artifacts_principal_arns, var.crm_operator_principal_arns)
  administrator_principal_arns = concat(var.crm_operator_principal_arns, ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"])
}

# ---------------------------------------------------------------------------
# cursor — the CMK sealing pagination cursors (KMS Encrypt/Decrypt under the
# `purpose = crm-cursor` encryption context), used by the five paginating tools.
# ---------------------------------------------------------------------------

locals {
  cursor_tools = ["search_opportunities", "search_accounts", "list_activities", "list_audit_records", "list_proposals"]

  # Listed by static index, so the key policy does not wait on every `module.lambda_action` instance.
  cursor_key_principal_arns = [
    module.lambda_action["search_opportunities"].role_arn,
    module.lambda_action["search_accounts"].role_arn,
    module.lambda_action["list_activities"].role_arn,
    module.lambda_action["list_audit_records"].role_arn,
    module.lambda_action["list_proposals"].role_arn,
  ]
}

resource "aws_kms_key" "cursor" {
  # checkov:skip=CKV2_AWS_64: policy managed by aws_kms_key_policy.cursor
  description             = "${local.name_prefix} pagination cursor sealing"
  enable_key_rotation     = true
  deletion_window_in_days = 7

  tags = {
    "data-classification" = "internal"
  }
}

resource "aws_kms_alias" "cursor" {
  name          = "alias/${local.name_prefix}-cursor"
  target_key_id = aws_kms_key.cursor.key_id
}

# Keeps the account-root statement, refuses any use outside the `crm-cursor` context and every
# operation the cursor codec does not call, and denies key use and key administration outside the
# lists.
resource "aws_kms_key_policy" "cursor" {
  key_id                             = aws_kms_key.cursor.id
  bypass_policy_lockout_safety_check = false

  lifecycle {
    precondition {
      condition     = contains(concat(var.crm_operator_principal_arns, ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]), data.aws_iam_session_context.current.issuer_arn) && data.aws_iam_session_context.current.issuer_arn != "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      error_message = "The caller running this plan is not an operator. Add its role or user ARN to crm_operator_principal_arns in terraform.tfvars, or run as a listed operator; nothing was applied."
    }
  }

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableIAMUserPermissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "DenyCursorKeyUseOutsidePurpose"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
        ]
        Resource = "*"
        Condition = {
          StringNotEquals = { "kms:EncryptionContext:purpose" = "crm-cursor" }
        }
      },
      {
        Sid       = "DenyCursorKeyUnusedOperations"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
          "kms:CreateGrant",
        ]
        Resource = "*"
      },
      {
        Sid       = "DenyKeyUseToNonProjectPrincipals"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
          "kms:CreateGrant",
        ]
        Resource = "*"
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = concat(local.cursor_key_principal_arns, var.crm_operator_principal_arns) }
          Bool         = { "aws:PrincipalIsAWSService" = "false" }
        }
      },
      {
        Sid       = "DenyKeyAdministrationToNonOperators"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "kms:PutKeyPolicy",
          "kms:ScheduleKeyDeletion",
          "kms:DisableKey",
          "kms:RevokeGrant",
          "kms:DisableKeyRotation",
          "kms:CreateAlias",
          "kms:UpdateAlias",
          "kms:DeleteAlias",
          "kms:TagResource",
          "kms:UntagResource",
          "kms:UpdateKeyDescription",
        ]
        Resource = "*"
        Condition = {
          ArnNotEquals = { "aws:PrincipalArn" = concat(var.crm_operator_principal_arns, ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]) }
        }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# agent_relay — the browser's only agent endpoint: a Node.js Lambda with a
# RESPONSE_STREAM Function URL, built by `scripts/package_relay.sh`.
#
# The URL is `authorization_type = AWS_IAM`, reached through the distribution's
# OAC, which SigV4-signs every origin request and so overwrites the viewer's
# `Authorization` header. The user's JWT therefore travels in
# `X-Crm-Authorization` and the relay verifies it.
# ---------------------------------------------------------------------------

# The relay's expected JWT audience, delivered through an SSM parameter so the relay module does not
# depend on the Cognito SPA client.
locals {
  relay_cognito_audience_parameter_name = "/${local.name_prefix}/relay/cognito-audience"
}

# The app client ids `crm.envelope` admits on the API Gateway channel. Delivered through SSM, by a
# constructed name and ARN, because a function attribute referencing the SPA client would cycle
# through the distribution.
locals {
  api_allowed_client_ids_parameter_name = "/${local.name_prefix}/api/allowed-client-ids"
  api_allowed_client_ids_parameter_arn  = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.api_allowed_client_ids_parameter_name}"
}

resource "aws_ssm_parameter" "api_allowed_client_ids" {
  # checkov:skip=CKV2_AWS_34: holds the SPA client id, a public value, not a secret
  name  = local.api_allowed_client_ids_parameter_name
  type  = "String"
  value = module.cognito.spa_client_id

  tags = {
    "data-classification" = "internal"
  }
}

resource "aws_ssm_parameter" "relay_cognito_audience" {
  # checkov:skip=CKV2_AWS_34: holds the SPA client id, a public value, not a secret
  name  = local.relay_cognito_audience_parameter_name
  type  = "String"
  value = module.cognito.spa_client_id

  tags = {
    "data-classification" = "internal"
  }
}

module "agent_relay" {
  source = "./modules/agent_relay"

  eni_function_code_deny_policy_arn = module.eni_function_code_deny.policy_arn

  function_name    = "${local.name_prefix}-agent-relay"
  filename         = "${var.lambda_build_dir}/relay.zip"
  source_code_hash = filebase64sha256("${var.lambda_build_dir}/relay.zip")

  agent_runtime_arn       = module.agentcore.agent_runtime_arn
  agent_runtime_qualifier = local.relay_runtime_qualifier

  cognito_issuer = "https://cognito-idp.${var.aws_region}.amazonaws.com/${module.cognito.user_pool_id}"

  # Passed by parameter name, with the ARN constructed rather than read from the parameter resource;
  # either a value or a resource reference would create a cognito -> cloudfront -> relay -> cognito cycle.
  cognito_audience_parameter_name = local.relay_cognito_audience_parameter_name
  cognito_audience_parameter_arn = format(
    "arn:aws:ssm:%s:%s:parameter%s",
    var.aws_region,
    data.aws_caller_identity.current.account_id,
    local.relay_cognito_audience_parameter_name,
  )

  environment_kms_key_arn = module.observability.cloudwatch_logs_kms_key_arn

  # Above the 600 s chat turn wall clock.
  timeout_seconds = 660

  # Stated rather than defaulted so it is counted in `local.reserved_concurrency_total`.
  reserved_concurrent_executions = local.reserved_concurrency["agent_relay"]

  # The relay's outbound calls all leave through `module.vpc`'s endpoints.
  vpc_subnet_ids         = module.vpc.lambda_subnet_ids
  vpc_security_group_ids = module.vpc.lambda_security_group_ids

  log_retention_days = 365

  tags = {
    "data-classification" = var.data_classification
  }
}

# The Runtime's resource-based policies: exactly `local.runtime_invoker_role_arns` may invoke it.
# One policy on the runtime ARN and one on its DEFAULT endpoint ARN, since a policy's `Resource` must
# equal the ARN it is attached to. Declared at the root because they need outputs from both
# `agentcore` and `agent_relay`. The endpoint ARN is constructed: the provider has no data source
# for the auto-created endpoint.
locals {
  relay_runtime_qualifier = "DEFAULT"

  # The segment is `runtime-endpoint`, not `endpoint`:
  #   arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>/runtime-endpoint/DEFAULT
  # With the wrong form `PutResourcePolicy` fails with `AccessDeniedException`.
  agent_runtime_endpoint_arn = "${module.agentcore.agent_runtime_arn}/runtime-endpoint/${local.relay_runtime_qualifier}"

  # The bedrock-agentcore interface endpoint the relay's InvokeAgentRuntime traverses; the
  # aws:SourceVpce statements below key on it.
  runtime_source_vpce_id = module.vpc.bedrock_agentcore_vpc_endpoint_id

  # The relay, the eval sweep's task Lambda and Gateway #2's target.
  runtime_invoker_role_arns = [
    module.agent_relay.role_arn,
    module.lambda_action_sweep_task.role_arn,
    module.lambda_action_chat_target.role_arn,
  ]
}

resource "aws_bedrockagentcore_resource_policy" "agent_runtime_relay_only" {
  resource_arn = module.agentcore.agent_runtime_arn
  policy       = data.aws_iam_policy_document.agent_runtime_relay_only.json
}

resource "aws_bedrockagentcore_resource_policy" "agent_runtime_endpoint_relay_only" {
  resource_arn = local.agent_runtime_endpoint_arn
  policy       = data.aws_iam_policy_document.agent_runtime_endpoint_relay_only.json
}

data "aws_iam_policy_document" "agent_runtime_relay_only" {
  statement {
    sid     = "OnlyTheInvokerRolesMayInvokeThisRuntime"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]

    principals {
      type        = "AWS"
      identifiers = local.runtime_invoker_role_arns
    }

    resources = [module.agentcore.agent_runtime_arn]
  }

  # Inbound Deny: every Runtime data-plane action from any principal outside the invoker roles.
  statement {
    sid    = "DenyRuntimeDataPlaneToAnyOtherPrincipal"
    effect = "Deny"
    actions = [
      "bedrock-agentcore:InvokeAgentRuntime",
      "bedrock-agentcore:InvokeAgentRuntimeForUser",
      "bedrock-agentcore:InvokeAgentRuntimeCommand",
      "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
      "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
      "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
      "bedrock-agentcore:StopRuntimeSession",
      "bedrock-agentcore:GetAgentCard",
    ]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    resources = [module.agentcore.agent_runtime_arn]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = local.runtime_invoker_role_arns
    }
  }

  # Report-only Allow for the invoker roles arriving through our bedrock-agentcore VPC endpoint.
  dynamic "statement" {
    for_each = var.runtime_source_vpce_report_only_enabled ? [1] : []
    content {
      sid     = "AllowInvokeViaOurVpcEndpoint"
      effect  = "Allow"
      actions = ["bedrock-agentcore:InvokeAgentRuntime"]

      # The invoker roles only: an Allow keyed only on a network condition would admit every
      # same-account principal that reaches the endpoint.
      principals {
        type        = "AWS"
        identifiers = local.runtime_invoker_role_arns
      }

      resources = [module.agentcore.agent_runtime_arn]

      condition {
        test     = "StringEquals"
        variable = "aws:SourceVpce"
        values   = [local.runtime_source_vpce_id]
      }
    }
  }

  # Inbound Deny: InvokeAgentRuntime from any principal not arriving through our bedrock-agentcore
  # endpoint. `aws:ViaAWSService = false` exempts AgentCore's own service calls, which carry no
  # aws:SourceVpce. Off by default.
  dynamic "statement" {
    for_each = var.runtime_source_vpce_deny_enabled ? [1] : []
    content {
      sid     = "DenyInvokeUnlessViaOurVpcEndpoint"
      effect  = "Deny"
      actions = ["bedrock-agentcore:InvokeAgentRuntime"]

      principals {
        type        = "AWS"
        identifiers = ["*"]
      }

      resources = [module.agentcore.agent_runtime_arn]

      condition {
        test     = "StringNotEquals"
        variable = "aws:SourceVpce"
        values   = [local.runtime_source_vpce_id]
      }

      condition {
        test     = "Bool"
        variable = "aws:ViaAWSService"
        values   = ["false"]
      }
    }
  }
}

data "aws_iam_policy_document" "agent_runtime_endpoint_relay_only" {
  statement {
    sid     = "OnlyTheInvokerRolesMayInvokeThisEndpoint"
    effect  = "Allow"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]

    principals {
      type        = "AWS"
      identifiers = local.runtime_invoker_role_arns
    }

    resources = [local.agent_runtime_endpoint_arn]
  }

  # The same principal Deny on the endpoint ARN.
  statement {
    sid    = "DenyRuntimeDataPlaneToAnyOtherPrincipal"
    effect = "Deny"
    actions = [
      "bedrock-agentcore:InvokeAgentRuntime",
      "bedrock-agentcore:InvokeAgentRuntimeForUser",
      "bedrock-agentcore:InvokeAgentRuntimeCommand",
      "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
      "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
      "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
      "bedrock-agentcore:StopRuntimeSession",
      "bedrock-agentcore:GetAgentCard",
    ]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    resources = [local.agent_runtime_endpoint_arn]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = local.runtime_invoker_role_arns
    }
  }

  # Report-only aws:SourceVpce Allow on the endpoint ARN, as on the runtime document above.
  dynamic "statement" {
    for_each = var.runtime_source_vpce_report_only_enabled ? [1] : []
    content {
      sid     = "AllowInvokeViaOurVpcEndpoint"
      effect  = "Allow"
      actions = ["bedrock-agentcore:InvokeAgentRuntime"]

      # The invoker roles only: an Allow keyed only on a network condition would admit every
      # same-account principal that reaches the endpoint.
      principals {
        type        = "AWS"
        identifiers = local.runtime_invoker_role_arns
      }

      resources = [local.agent_runtime_endpoint_arn]

      condition {
        test     = "StringEquals"
        variable = "aws:SourceVpce"
        values   = [local.runtime_source_vpce_id]
      }
    }
  }

  # Inbound Deny on the endpoint ARN, mirroring the runtime document's Deny. Off by default.
  dynamic "statement" {
    for_each = var.runtime_source_vpce_deny_enabled ? [1] : []
    content {
      sid     = "DenyInvokeUnlessViaOurVpcEndpoint"
      effect  = "Deny"
      actions = ["bedrock-agentcore:InvokeAgentRuntime"]

      principals {
        type        = "AWS"
        identifiers = ["*"]
      }

      resources = [local.agent_runtime_endpoint_arn]

      condition {
        test     = "StringNotEquals"
        variable = "aws:SourceVpce"
        values   = [local.runtime_source_vpce_id]
      }

      condition {
        test     = "Bool"
        variable = "aws:ViaAWSService"
        values   = ["false"]
      }
    }
  }
}

# ---------------------------------------------------------------------------
# agentcore — ECR + CodeBuild image build, Runtime, Gateway, one target per
# action Lambda (every tool, including `get_page_context`).
# ---------------------------------------------------------------------------

# Uploads the local agent-source archive to the S3 key `aws_codebuild_project.agent_image` reads.
# `source_hash` is the same content hash that triggers `module.agentcore`'s image build, so a changed
# archive re-uploads and rebuilds together. Encryption is the bucket's default.
#
# The object lives in the eval-artefacts bucket, not the CloudFront origin bucket, where the
# distribution's default behaviour would serve it anonymously.
resource "aws_s3_object" "agent_source" {
  bucket      = module.eval_artifacts.bucket_name
  key         = "agent-source/agent.zip"
  source      = "${var.lambda_build_dir}/agent-source.zip"
  source_hash = filebase64sha256("${var.lambda_build_dir}/agent-source.zip")
  kms_key_id  = aws_kms_key.eval_artifacts.arn

  tags = {
    "data-classification" = "internal"
  }
}

module "agentcore" {
  source = "./modules/agentcore"

  # `aws_s3_object.agent_source` must exist before the image build reads it, and
  # `aws_iam_role_policy.agentcore_runtime` must be applied before the runtime, because
  # CreateAgentRuntime checks that the execution role can pull the ECR image.
  depends_on = [aws_s3_object.agent_source, aws_iam_role_policy.agentcore_runtime]

  aws_profile = var.aws_profile
  aws_region  = var.aws_region
  name_prefix = local.name_prefix

  data_classification = var.data_classification

  ecr_repository_name   = local.agent_ecr_repository_name
  kms_key_arn           = module.observability.cloudwatch_logs_kms_key_arn
  image_retention_count = 10
  image_tag             = local.agent_image_tag
  source_bucket_arn     = module.eval_artifacts.bucket_arn
  source_key            = aws_s3_object.agent_source.key
  source_kms_key_arn    = aws_kms_key.eval_artifacts.arn
  source_content_hash   = filebase64sha256("${var.lambda_build_dir}/agent-source.zip")
  buildspec             = local.agent_buildspec
  log_retention_days    = 365

  # Must match [a-zA-Z][a-zA-Z0-9_]{0,47}: no hyphens.
  agent_runtime_name     = "crm_agent_eval_runtime"
  agent_runtime_role_arn = aws_iam_role.agentcore_runtime.arn

  # A literal rather than a variable so the plan-security-baseline `runtime_network_mode_vpc` check
  # can read it from source.
  runtime_network_mode       = "VPC"
  runtime_subnet_ids         = module.vpc.runtime_subnet_ids
  runtime_security_group_ids = [module.vpc.runtime_security_group_id]

  agent_runtime_environment_variables = {
    COGNITO_USER_POOL_ID = module.cognito.user_pool_id
    API_BASE_URL         = module.api.stage_invoke_url
    CRM_TABLE_PREFIX     = local.name_prefix

    # The stub/live switch, not the family selector: `stub` serves the zero-cost StubProvider, and
    # `anthropic`, `openai` and `live` all mean live, with the adapter chosen per invocation from the
    # resolved model's family. Unset means `stub`.
    AGENT_PROVIDER = "anthropic"

    # Prefix of every `module.lambda_action` function name, used to resolve a tool to its Lambda.
    ACTION_LAMBDA_PREFIX = local.name_prefix
  }

  cognito_discovery_url      = "https://cognito-idp.${var.aws_region}.amazonaws.com/${module.cognito.user_pool_id}/.well-known/openid-configuration"
  cognito_allowed_client_ids = module.cognito.allowed_client_ids
  cognito_allowed_audiences  = module.cognito.allowed_client_ids
  # CreateGateway rejects an empty `allowedScopes`. Only the MCP `client_credentials` client's tokens
  # carry this scope; SPA tokens carry `openid`/`email`/`profile` only.
  cognito_allowed_scopes         = [module.cognito.gateway_scope_identifier]
  cognito_required_custom_claims = []

  gateway_name     = "crm-agent-eval-gateway"
  gateway_role_arn = aws_iam_role.agentcore_gateway.arn

  identity_interceptor_lambda_arn           = module.lambda_action_interceptor.function_arn
  identity_interceptor_lambda_function_name = module.lambda_action_interceptor.function_name

  tool_lambda_arns = { for name, m in module.lambda_action : name => m.function_arn }

  tool_schemas = {
    for name in local.tool_names : name => {
      description = local.tools[name].description
      properties  = local.tool_schema_properties[name]
    }
  }
}

# ---------------------------------------------------------------------------
# Execution roles for the AgentCore Runtime and Gateway, passed into
# `module.agentcore` as `agent_runtime_role_arn` and `gateway_role_arn`.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "agentcore_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

data "aws_caller_identity" "current" {}

# The Runtime invokes each action Lambda directly and reaches Bedrock models through inference
# profiles; no vendor key is used.
resource "aws_iam_role" "agentcore_runtime" {
  name               = "crm-agent-eval-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume_role.json
}

locals {
  # The tools whose toolspec `agents` set is empty; the Runtime role may not invoke them.
  # `tests/unit/test_infra_runtime_invoke_scope.py` pins this set to the toolspec.
  runtime_uninvocable_tools = toset(["approve_proposal", "reject_proposal", "revert_audit_record"])

  # Every `inference_profile` in the capability matrix, sorted and distinct.
  bedrock_capability_models  = values(yamldecode(file("${path.module}/../config/model_capabilities.yaml")).models)
  bedrock_inference_profiles = sort(distinct([for m in local.bedrock_capability_models : m.inference_profile]))
  # A global profile is `global.<foundation-model-id>`.
  bedrock_foundation_models = [for p in local.bedrock_inference_profiles : trimprefix(p, "global.")]
}

data "aws_iam_policy_document" "agentcore_runtime_permissions" {
  lifecycle {
    precondition {
      condition = length(local.bedrock_capability_models) > 0 && alltrue([
        for m in local.bedrock_capability_models :
        can(regex("^[a-z0-9]+$", m.family)) && can(regex("^global\\.${m.family}\\.[a-z0-9][a-z0-9.:-]*$", m.inference_profile))
      ])
      error_message = "Every inference_profile in config/model_capabilities.yaml must be global.<family>.<model-id> for its own entry's family, and there must be at least one."
    }
  }

  statement {
    sid       = "InvokeActionLambdas"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [for name, m in module.lambda_action : m.function_arn if !contains(local.runtime_uninvocable_tools, name)]
  }

  # Scoped to the inference profiles above and this account's default Bedrock project, which the
  # OpenAI models require alongside the profile.
  statement {
    sid     = "BedrockInference"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(
      [
        for profile in local.bedrock_inference_profiles :
        "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${profile}"
      ],
      ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/default"],
    )
  }

  # A `global.` profile can route to the region-less foundation-model ARN as well as any regional
  # one, so both are granted. The bedrock:InferenceProfileArn condition makes these models reachable
  # only through the profiles above. A separate sid because `scripts/smoke/relay_iam.py` reads the
  # profile ARNs from "BedrockInference".
  statement {
    sid     = "BedrockInferenceFoundationModels"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(
      [for model in local.bedrock_foundation_models : "arn:aws:bedrock:::foundation-model/${model}"],
      [for model in local.bedrock_foundation_models : "arn:aws:bedrock:*::foundation-model/${model}"],
    )
    condition {
      test     = "StringEquals"
      variable = "bedrock:InferenceProfileArn"
      values = [
        for profile in local.bedrock_inference_profiles :
        "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${profile}"
      ]
    }
  }

  # CreateAgentRuntime validates that the execution role can pull the container image
  # (`ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`).
  # `ecr:GetAuthorizationToken` requires `Resource: "*"`; the others are scoped to this repository.
  # The ARN is constructed rather than read from `module.agentcore` so this policy can be applied
  # before that module.
  statement {
    sid     = "EcrImageAccess"
    effect  = "Allow"
    actions = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [
      "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${local.agent_ecr_repository_name}",
    ]
  }

  statement {
    sid       = "EcrTokenAccess"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
}

# The Runtime's writes to its own log group and streams. `logs:CreateLogGroup` is included because
# some AgentCore code paths call it even when the group exists. A separate policy from the ECR
# grants because the log group's name embeds the runtime id, so this policy can only be applied
# after the runtime.
data "aws_iam_policy_document" "agentcore_runtime_logs" {
  statement {
    sid    = "CloudWatchLogsAccess"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "${module.agentcore.agent_runtime_log_group_arn}",
      "${module.agentcore.agent_runtime_log_group_arn}:*",
    ]
  }
}

# Must exist before the runtime; `module "agentcore"` depends_on it, so this document must not
# reference anything inside that module.
resource "aws_iam_role_policy" "agentcore_runtime" {
  name   = "crm-agent-eval-runtime-permissions"
  role   = aws_iam_role.agentcore_runtime.id
  policy = data.aws_iam_policy_document.agentcore_runtime_permissions.json
}

# Applied after the runtime, because the log group it scopes to is named from the runtime's id.
resource "aws_iam_role_policy" "agentcore_runtime_logs" {
  name   = "crm-agent-eval-runtime-logs"
  role   = aws_iam_role.agentcore_runtime.id
  policy = data.aws_iam_policy_document.agentcore_runtime_logs.json
}

# The Gateway's own execution role, assumed to invoke each target Lambda
# (`credential_provider_configuration.gateway_iam_role`) and the identity
# interceptor.
resource "aws_iam_role" "agentcore_gateway" {
  name               = "crm-agent-eval-gateway-role"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume_role.json
}

data "aws_iam_policy_document" "agentcore_gateway_permissions" {
  statement {
    sid       = "InvokeActionLambdas"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [for m in module.lambda_action : m.function_arn]
  }

  statement {
    sid       = "InvokeInterceptor"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [module.lambda_action_interceptor.function_arn]
  }
}

resource "aws_iam_role_policy" "agentcore_gateway" {
  name   = "crm-agent-eval-gateway-permissions"
  role   = aws_iam_role.agentcore_gateway.id
  policy = data.aws_iam_policy_document.agentcore_gateway_permissions.json
}

# ---------------------------------------------------------------------------
# Reserved concurrency headroom: the sum over `local.reserved_concurrency` must
# leave at least 100 of the account's 1,000-per-region concurrency pool. A
# function whose value is not taken from that map is not counted here;
# `check_plan_security_baseline`'s `lambda_reserved_concurrency_headroom` sums
# over the plan.
# ---------------------------------------------------------------------------

locals {
  # Every reserved concurrency this deployment claims, keyed by consuming module instantiation.
  non_tool_reserved_concurrency = {
    identity_interceptor = 20
    pre_token_generation = 20
    eval_trigger         = 5
    # Fetched by the SPA on every page load.
    capabilities = 10
    # Passed explicitly into `module.agent_relay` so it is counted here.
    agent_relay = 20
    # The eval state machine's two functions, Gateway #2's target and its own
    # interceptor, and settings.
    sweep_control    = 2
    sweep_task       = 1
    chat_target      = 5
    chat_interceptor = 5
    user_settings    = 5
  }

  # Every function outside `local.tool_names` that claims reserved concurrency.
  non_tool_functions = sort(keys(local.non_tool_reserved_concurrency))

  reserved_concurrency = merge(
    local.tool_reserved_concurrency,
    local.non_tool_reserved_concurrency,
  )

  reserved_concurrency_total = sum(values(local.reserved_concurrency))
}

check "reserved_concurrency_headroom" {
  assert {
    condition     = local.reserved_concurrency_total <= 900
    error_message = "Reserved concurrency claimed by this composition (${local.reserved_concurrency_total} across ${length(local.reserved_concurrency)} functions, derived from local.reserved_concurrency) must leave at least 100 of headroom below the account's 1,000 default concurrency pool. This sum covers every function whose reserved_concurrent_executions is wired from that map; the plan-derived sum in check_plan_security_baseline's lambda_reserved_concurrency_headroom is the complete one."
  }
}

# A failing `check` only warns; this precondition fails `plan`/`apply` outright.
resource "terraform_data" "reserved_concurrency_headroom_guard" {
  lifecycle {
    precondition {
      condition     = local.reserved_concurrency_total <= 900
      error_message = "Reserved concurrency claimed by this composition (${local.reserved_concurrency_total} across ${length(local.reserved_concurrency)} functions, derived from local.reserved_concurrency) must leave at least 100 of headroom below the account's 1,000 default concurrency pool."
    }

    # Non-vacuity floor: `merge` silently drops a duplicate key, which would lower the sum.
    precondition {
      condition = (
        length(setintersection(toset(local.tool_names), toset(local.non_tool_functions))) == 0 &&
        toset(keys(local.reserved_concurrency)) == setunion(toset(local.tool_names), toset(local.non_tool_functions))
      )
      error_message = "local.reserved_concurrency keys must be exactly local.tool_names plus local.non_tool_functions (${join(", ", local.non_tool_functions)}), with no name in both. Got ${length(local.reserved_concurrency)} keys for ${length(local.tool_names)} tools: either a non-tool key collided with a tool name and merge silently dropped one, or a tool is missing from local.tool_reserved_concurrency."
    }
  }
}
