// SPDX-License-Identifier: MPL-2.0
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export interface ProjectRef { root: string; project_id: string; workspace_format: number; name: string; description: string; import_source?: string | null }
export interface ProviderStatus { provider: string; available: boolean; command: string[] }
export interface RunRef { run_id: string; project_id: string }
export interface GuiOperation {
  operation_id: string; kind: "create" | "import" | "conversation" | "execute" | "action";
  status: "STARTING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "INTERRUPTED";
  phase: string; project: ProjectRef | null; run: RunRef | null;
  message?: string; error?: string; cancel_requested?: boolean;
  result?: { message?: string; applied_count?: number; integrated_paths?: string[]; discarded?: string; cancelled?: string };
}
export interface ChatMessage { role: "user" | "assistant"; text: string; status?: string; error?: string; operation_id?: string; run_id?: string; truncated?: boolean }
export interface Conversation { conversation_id: string; kind: "main" | "project"; project: ProjectRef | null; messages: ChatMessage[]; omitted: number }
export interface RunStatus { run_id: string; status: GuiOperation["status"]; integrated: boolean; error: string | null; output_tail?: string }
export interface ProductUpdateStatus {
  operation_id: string | null; installed_version: string; candidate_version: string | null;
  phase: "IDLE" | "UNSUPPORTED" | "CHECKING" | "AVAILABLE" | "NO_UPDATE" | "STAGING" | "READY" | "WAITING_FOR_EXIT" | "INSTALLING" | "SUCCEEDED" | "ROLLED_BACK" | "RECOVERY_REQUIRED" | "FAILED" | "BUSY";
  progress: number; message: string; error: { code: string; message: string } | null; can_start: boolean; can_finish: boolean;
}
export type BackendSnapshot = { desktop: { version: string; identifier: string } } & (
  { connection: "ready"; handshake: { protocol_version: number; version: string; methods: string[] }; providers: { providers: ProviderStatus[] } } |
  { connection: "unavailable" | "disconnected"; error?: string; failure_origin?: string }
);
let pending: Promise<unknown> = Promise.resolve();
function request<T>(method: string, params: object): Promise<T> {
  const result = pending.then(() => invoke<T>("workspace_request", { method, params }));
  pending = result.catch(() => undefined);
  return result;
}
async function gui<T>(method: string, params: object): Promise<T> {
  const reply = await request<{ ok: boolean; value: T; error?: string }>(method, params);
  if (!reply.ok) throw new Error(reply.error || "The requested operation could not be completed.");
  return reply.value;
}
export const bridge = {
  snapshot: () => invoke<BackendSnapshot>("backend_snapshot"),
  restart: () => invoke<BackendSnapshot>("restart_backend"),
  selectProjectDirectory: () => invoke<string | null>("select_project_directory"),
  projects: () => gui<{projects: (ProjectRef & { available: boolean })[]; base: string}>("gui.projects", {}),
  createProject: (name: string, description: string, provider: string) => gui<GuiOperation>("gui.create", { name, description, provider }),
  openProject: (path: string) => gui<ProjectRef>("gui.open", { path }),
  importProject: (path: string) => gui<{project?: ProjectRef; operation?: GuiOperation}>("gui.import", { path }),
  operation: (operation_id: string) => gui<GuiOperation>("gui.operation.get", { operation_id }),
  cancelOperation: (operation_id: string) => gui<GuiOperation>("gui.operation.cancel", { operation_id }),
  conversation: (project: ProjectRef | null) => gui<Conversation>("gui.conversation.open", { project }),
  conversationGet: (conversation_id: string) => gui<Conversation>("gui.conversation.get", { conversation_id }),
  send: (conversation_id: string, message: string, provider: string) => gui<GuiOperation>("gui.conversation.send", { conversation_id, message, provider }),
  output: (conversation_id: string, run_id: string, cursor: number) => gui<{text: string; cursor: number; complete: boolean}>("gui.conversation.output", { conversation_id, run_id, cursor }),
  summary: (project: ProjectRef, kind: "ideas" | "tasks") => gui<{kind: string; empty: boolean; items: string[]}>("gui.summary", { project, kind }),
  execute: (project: ProjectRef, provider: string) => gui<{empty: boolean; message?: string; operation?: GuiOperation}>("gui.execute", { project, provider }),
  run: (project: ProjectRef, run_id: string) => gui<RunStatus>("gui.run.get", { project, run_id }),
  runs: (project: ProjectRef) => gui<{runs: (RunStatus & {skill: string})[]}>("gui.runs", { project }),
  runAction: (project: ProjectRef, run_id: string, action: "apply" | "discard" | "cancel", summary = "") => gui<GuiOperation>("gui.run.action", { project, run_id, action, summary }),
  providerConfigure: (provider: string, command: string[]) => gui<{providers: ProviderStatus[]}>("gui.provider.configure", { provider, command }),
  updateCheck: () => request<ProductUpdateStatus>("update.check", {}),
  updateStart: (operation_id: string) => request<ProductUpdateStatus>("update.start", { operation_id }),
  updateStatus: (operation_id?: string) => request<ProductUpdateStatus>("update.status", operation_id ? { operation_id } : {}),
  updateFinish: (operationId: string) => invoke<ProductUpdateStatus>("finish_update", { operationId }),
  onDisconnect: (handler: () => void) => listen<{method: string}>("backend-lifecycle", ({payload}) => {
    if (payload.method === "backend.disconnected" || payload.method === "system.stopping") handler();
  }),
};
