// SPDX-License-Identifier: MPL-2.0
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export interface ProviderStatus {
  provider: string;
  supported: boolean;
  configuration: "operational" | "initial" | "global" | "default";
  executable: string | null;
  observation: "OBSERVED" | "NOT_OBSERVED" | "UNKNOWN";
  capabilities: {
    available: boolean;
    version: string | null;
    json_events: boolean;
    stdin_prompt: boolean;
    native_resume: boolean;
    read_only: boolean;
  } | null;
}

export interface ProjectStatus {
  root: string;
  exists: boolean;
  is_directory: boolean;
  initialized: boolean;
  storage: "ABSENT" | "CURRENT" | "LEGACY" | "INVALID" | "UNVERIFIED";
  schema_version: number | null;
}

export interface ProjectEntry {
  root: string;
  display_name: string;
  last_accessed_at: string | null;
  availability: "AVAILABLE" | "MISSING" | "UNAVAILABLE";
  initialized: boolean;
  storage: ProjectStatus["storage"];
  schema_version: number | null;
}

export interface ProjectSummary extends ProjectStatus {
  display_name: string;
  active_project: ContractReference | null;
  active_workflow: ContractReference | null;
  work_items: {
    total: number;
    by_status: Record<string, number>;
    by_scope: Record<string, number>;
  } | null;
}

interface ImportAmbiguity {
  code: string;
  explanation: string;
  required: boolean;
  options: string[];
}

export interface ImportDiscovery {
  root: string;
  label: "DISCOVERED";
  head: string | null;
  branch: string | null;
  dirty: boolean;
  commit_allowed: boolean;
  existing_state: boolean;
  tracked_count: number;
  languages: Record<string, number>;
  manifests: { path: string; ecosystem: string }[];
  docs: string[];
  tests: string[];
  candidates: { path: string; ecosystem: string }[];
  exclusions: string[];
  ambiguities: ImportAmbiguity[];
  discovery_digest: string;
}

export type ImportResult = {
  root: string;
  status: "IMPORTED";
  label: "CONFIRMED";
  project_id: string;
  snapshot_digest: string;
  discovery_digest: string;
  error: string | null;
} | {
  root: string;
  status: "REJECTED" | "ALREADY_MANAGED" | "INVALID" | "INCOMPLETE" | "NOT_IMPORTED";
  error: string | null;
};

interface ContractReference {
  contract_id: string;
  revision_id: string;
}

export interface WorkItem {
  work_item_id: string;
  parent_work_item_id: string | null;
  objective: string;
  scope_level: string;
  status: string;
  updated_at: string;
  allowed_transitions: string[];
}

export interface WorkNode extends Omit<WorkItem, "updated_at" | "allowed_transitions"> {
  depth: number;
}

export const workScopes = ["TASK", "CHANGE", "PROJECT", "CRITICAL"] as const;
export type WorkScope = typeof workScopes[number];

export interface ScopeAssessment {
  work_item_id: string;
  selected_scope: WorkScope;
  recommended_scope: WorkScope;
  minimum_required_scope: WorkScope;
  policy_version: number | string;
  signals: string[];
  unknowns: string[];
}

export interface FlowView extends ScopeAssessment {
  flow_kind: WorkScope;
  stage: string;
  gates: { id: string; state: string }[];
  blockers: string[];
  next_actions: string[];
  validation_state: string;
  validation_policy: string | null;
  workflow_ref: ContractReference | null;
  execution: { execution_id: string; state: string } | null;
  latest_session: { session_id: string; provider: string; state: string; run_generation: number } | null;
  latest_projection: { projection_id: string; projection_digest: string } | null;
  latest_handoff: { handoff_id: string; digest: string } | null;
  provider_continuity: { source_provider: string; target_provider: string; handoff_id: string } | null;
  report_status: string;
  can_start: boolean;
  can_complete: boolean;
}

export interface WorkReport {
  work_item_id: string;
  flow: FlowView;
  objective: string;
  acceptance_criteria: string[];
  overall_status: string;
}

interface WorkInput {
  objective: string;
  scope_level: string;
  acceptance_criteria: string[];
  parent_work_item_id?: string;
}

export interface AgentSession {
  session_id: string;
  work_item_id: string;
  project_id: string;
  provider: string;
  provider_version: string | null;
  model: string | null;
  state: string;
  run_generation: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  duration_ms: number | null;
  reported_cost: string | null;
  reported_cost_currency: string | null;
  timeout_seconds: number;
  read_only: boolean;
  can_cancel: boolean;
  can_recover: boolean;
  cleanup_unconfirmed: boolean;
}

export interface SessionEvent {
  sequence: number;
  kind: string;
  occurred_at: string;
  payload: Record<string, unknown>;
  native_kind: string | null;
  session_id: string;
}

interface SessionStart {
  provider: string;
  work_item_id: string;
  timeout_seconds: number;
  read_only: boolean;
  instruction?: string;
}

export interface ContextProjection {
  projection_id: string;
  project_id: string;
  work_item_id: string;
  target_provider: string;
  selector_policy_version: number;
  redaction_policy_version: number;
  projection_digest: string;
  source_set_digest: string;
  created_at: string;
}

export interface ContextSource {
  ordinal: number;
  kind: string;
  authority: "CANONICAL" | "OPERATIONAL" | "DERIVED";
  identity: string;
  revision: string | null;
  digest: string;
  reason: string;
  redaction: string[];
}

export interface SessionHandoff {
  handoff_id: string;
  project_id: string;
  work_item_id: string;
  source_session_id: string;
  run_generation: number;
  event_cutoff: number;
  provider: string;
  terminal_state: string;
  digest: string;
  created_at: string;
}

export interface SwitchView {
  switch_key: string;
  project_id: string;
  work_item_id: string;
  source_session_id: string;
  source_provider: string;
  source_generation: number;
  source_state: string;
  cleanup_unconfirmed: boolean;
  target_provider: string;
  stage: "SOURCE_ACTIVE" | "STOPPING_SOURCE" | "SOURCE_TERMINAL" | "HANDOFF_READY" |
    "PROJECTION_READY" | "TARGET_SESSION_ACTIVE" | "TARGET_SESSION_TERMINAL" | "BLOCKED";
  handoff_id: string | null;
  handoff_digest: string | null;
  projection_id: string | null;
  projection_digest: string | null;
  target_session_id: string | null;
  target_state: string | null;
  blocker: string | null;
}

let pending: Promise<unknown> = Promise.resolve();

function request<T>(method: string, params: object) {
  const reply = pending.then(() => invoke<T>("workspace_request", { method, params }));
  pending = reply.catch(() => undefined);
  return reply;
}

export type BackendSnapshot =
  | {
      connection: "ready";
      handshake: { protocol_version: number; version: string; methods: string[] };
      providers: { providers: ProviderStatus[] };
    }
  | { connection: "disconnected"; error?: string };

export const bridge = {
  snapshot: () => invoke<BackendSnapshot>("backend_snapshot"),
  projects: (offset: number) =>
    request<{ projects: ProjectEntry[]; next_offset: number | null }>("projects.list", { offset }),
  project: (root: string, open: boolean) =>
    request<ProjectSummary>(open ? "project.open" : "project.summary", { root }),
  importInspect: (root: string) => request<ImportDiscovery>("import.inspect", { root }),
  importStatus: (root: string) => request<ImportResult>("import.status", { root }),
  importCommit: (root: string, discovery_digest: string, confirmations: string[]) =>
    request<ImportResult>("import.commit", { root, discovery_digest, confirmations }),
  workTree: (root: string, offset: number) =>
    request<{ nodes: WorkNode[]; next_offset: number | null }>("work.tree", { root, offset }),
  workGet: (root: string, work_item_id: string) => request<WorkItem>("work.get", { root, work_item_id }),
  workCreate: (root: string, input: WorkInput) => request<WorkItem>("work.create", { root, ...input }),
  workStatus: (root: string, item: WorkItem, status: string) =>
    request<WorkItem>("work.status", {
      root,
      work_item_id: item.work_item_id,
      expected_updated_at: item.updated_at,
      status,
    }),
  scopeSet: (root: string, item: WorkItem, scope_level: WorkScope, confirm_downgrade: boolean) =>
    request<ScopeAssessment & { updated_at: string }>("scope.set", {
      root, work_item_id: item.work_item_id, expected_updated_at: item.updated_at, scope_level, confirm_downgrade,
    }),
  flowStatus: (root: string, work_item_id: string) =>
    request<FlowView>("flow.status", { root, work_item_id }),
  flowStart: (root: string, work_item_id: string, options: Omit<SessionStart, "work_item_id">) =>
    request<FlowView>("flow.start", { root, work_item_id, ...options }),
  flowComplete: (root: string, item: WorkItem) =>
    request<FlowView>("flow.complete", {
      root, work_item_id: item.work_item_id, expected_updated_at: item.updated_at,
    }),
  workReport: (root: string, work_item_id: string) =>
    request<WorkReport>("work.report", { root, work_item_id }),
  sessions: (root: string, offset: number) =>
    request<{ sessions: AgentSession[]; next_offset: number | null }>("session.list", { root, offset }),
  sessionGet: (root: string, session_id: string) =>
    request<AgentSession>("session.get", { root, session_id }),
  sessionEvents: (root: string, session_id: string, after_sequence: number, limit: number) =>
    request<{ events: SessionEvent[]; next_after_sequence: number }>("session.events", { root, session_id, after_sequence, limit }),
  sessionStart: (root: string, input: SessionStart) =>
    request<AgentSession>("session.start", { root, ...input }),
  sessionCancel: (root: string, session_id: string, generation: number) =>
    request<AgentSession>("session.cancel", { root, session_id, generation }),
  sessionRecover: (root: string, session_id: string, instruction?: string) =>
    request<AgentSession>("session.recover", { root, session_id, ...(instruction ? { instruction } : {}) }),
  projections: (root: string, offset: number, work_item_id?: string) =>
    request<{ projections: ContextProjection[]; next_offset: number | null }>("context.list", {
      root, offset, ...(work_item_id ? { work_item_id } : {}),
    }),
  contextCreate: (root: string, work_item_id: string, provider: string) =>
    request<ContextProjection>("context.create", { root, work_item_id, provider }),
  contextGet: (root: string, projection_id: string) =>
    request<ContextProjection>("context.get", { root, projection_id }),
  contextVerify: (root: string, projection_id: string) =>
    request<ContextProjection & { verified: boolean }>("context.verify", { root, projection_id }),
  contextSources: (root: string, projection_id: string, offset: number) =>
    request<{ sources: ContextSource[]; next_offset: number | null }>("context.sources", { root, projection_id, offset }),
  contextForSession: (root: string, session_id: string) =>
    request<{ projection: ContextProjection | null }>("context.for_session", { root, session_id }),
  handoffs: (root: string, offset: number, work_item_id?: string) =>
    request<{ handoffs: SessionHandoff[]; next_offset: number | null }>("handoff.list", {
      root, offset, ...(work_item_id ? { work_item_id } : {}),
    }),
  handoffCreate: (root: string, session_id: string) =>
    request<SessionHandoff>("handoff.create", { root, session_id }),
  handoffGet: (root: string, handoff_id: string) =>
    request<SessionHandoff>("handoff.get", { root, handoff_id }),

  switchStatus: (root: string, source_session_id: string, target_provider: string, source_generation: number) =>
    request<SwitchView>("switch.status", { root, source_session_id, target_provider, source_generation }),
  switchExecute: (root: string, source_session_id: string, target_provider: string, source_generation: number) =>
    request<SwitchView>("switch.execute", { root, source_session_id, target_provider, source_generation }),
  restart: () => invoke<BackendSnapshot>("restart_backend"),
  onDisconnect: (handler: () => void) =>
    listen<{ method: string }>("backend-lifecycle", ({ payload }) => {
      if (payload.method === "backend.disconnected" || payload.method === "system.stopping") {
        handler();
      }
    }),
};
