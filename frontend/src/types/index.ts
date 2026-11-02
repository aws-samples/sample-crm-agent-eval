// SPDX-License-Identifier: MIT
/**
 * Barrel for hand-authored types plus the generated tool schemas. Pages import from `../types`
 * rather than from `types/generated/toolSchemas` directly.
 */
export type {
  RiskClass,
  ToolMetadata,
  ToolName,
} from "./generated/toolSchemas";
export { TOOL_METADATA } from "./generated/toolSchemas";
export type {
  ApproveProposalInput,
  CreateAccountInput,
  CreateContactInput,
  CreateNoteInput,
  CreateOpportunityInput,
  DraftMessageInput,
  GetAccountInput,
  GetContactInput,
  GetOpportunityInput,
  ListActivitiesInput,
  ListAuditRecordsInput,
  ListProposalsInput,
  LogActivityInput,
  PipelineAggregateInput,
  RejectProposalInput,
  RevertAuditRecordInput,
  SearchOpportunitiesInput,
  SubmitProposalInput,
  UpdateAccountInput,
  UpdateActivityInput,
  UpdateContactInput,
  UpdateOpportunityInput,
} from "./generated/toolSchemas";

export type { Identity, RawIdentityClaims } from "../auth/identity";
export type { RuntimeConfig } from "../config/runtimeConfig";
